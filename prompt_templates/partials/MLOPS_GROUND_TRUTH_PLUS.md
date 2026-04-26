# SOP — SageMaker Ground Truth Plus (managed labeling service · vendor workforce · expert reviewers)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Ground Truth Plus (managed service, GA 2022) · vendor + expert workforces · custom labeling templates · QC review workflow · pricing model: per-object (vs per-task in self-managed Ground Truth)

---

## 1. Purpose

- Codify the **Ground Truth Plus pattern** — when self-managed Ground Truth (`MLOPS_GROUND_TRUTH`) is too operationally heavy, GT Plus offloads workforce management entirely to AWS.
- Codify the **decision matrix** vs self-managed: GT (cheap, you manage), GT Plus (expensive per-object, AWS manages workforce + project mgmt + QC).
- Codify the **engagement flow**: kickoff → labeling guidelines → pilot → production batches → QC review → final delivery.
- Cover the **integration with downstream training**: GT Plus output → S3 → SageMaker training job.
- This is the **managed-labeling specialisation**. `MLOPS_GROUND_TRUTH` is the self-managed alternative.

When the SOW signals: "we have data scientists, not labelers", "need expert labelers (medical, legal)", "outsource labeling to AWS", "high-volume labeling project", "GT Plus".

---

## 2. Decision tree — GT vs GT Plus

| Need | Self-managed GT | GT Plus |
|---|---|---|
| Have own labeling workforce (Mechanical Turk, contractor company) | ✅ best | ❌ unnecessary |
| Need expert labelers (radiologists, lawyers) | ❌ hard to source | ✅ AWS sources |
| Project management overhead (instructions, QC) | You handle | ✅ AWS handles |
| Pricing transparency | Per-task ($0.012-$5/task) | Per-object ($0.05-$50/object based on task) |
| Time-to-first-batch | Days (build instructions, recruit) | 4-6 weeks (AWS onboarding) |
| Volume | Any | Min 5K objects typically |
| Custom data types | Full flex | Pre-defined templates |

```
Volume + complexity?
├── < 1K objects → use intern team or Mechanical Turk directly (NOT GT)
├── 1K - 10K objects, simple task → §3 self-managed GT (MLOPS_GROUND_TRUTH)
├── 10K+ objects, simple task → either; GT Plus saves ops time
├── 10K+ objects, expert labelers needed → §4 GT Plus
└── 1M+ objects → §4 GT Plus (volume discounts)
```

---

## 3. CDK + workflow setup

### 3.1 GT Plus is NOT primarily CDK-managed

GT Plus engagements are managed through the AWS Console + your AWS account team. CDK only manages the supporting infrastructure (S3 buckets, IAM, output triggers).

### 3.2 Architecture

```
   You ────► AWS account team kickoff (4-6 weeks)
      │
      ▼
   GT Plus project created in console
      │
      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Input bucket: s3://qra-gt-plus-input/                            │
   │     - Customer uploads raw data                                    │
   │     - GT Plus reads + assigns to workforce                         │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   GT Plus workforce labels objects (with QC review by AWS PM)
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Output bucket: s3://qra-gt-plus-output/                          │
   │     - Labels in JSON Manifest format                                │
   │     - QC report per batch                                            │
   │     - Trigger S3 → EventBridge → Lambda → training pipeline         │
   └──────────────────────────────────────────────────────────────────┘
```

### 3.3 CDK — supporting buckets + downstream trigger

```python
from aws_cdk import (
    Duration, RemovalPolicy,
    aws_iam as iam,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
    aws_s3_notifications as s3n,
)


def _create_gt_plus_buckets_and_trigger(self, stage: str) -> None:
    """Supporting infra for a GT Plus engagement."""

    # A) Input bucket — customer uploads here, AWS GT Plus role reads
    self.gt_plus_input = s3.Bucket(self, "GtPlusInput",
        bucket_name=f"{{project_name}}-gtplus-input-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=RemovalPolicy.RETAIN,
    )
    # GT Plus AWS service role reads input
    self.gt_plus_input.add_to_resource_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        principals=[iam.ServicePrincipal("ground-truth-labeling.sagemaker.amazonaws.com")],
        actions=["s3:GetObject", "s3:ListBucket"],
        resources=[
            self.gt_plus_input.bucket_arn,
            f"{self.gt_plus_input.bucket_arn}/*",
        ],
        conditions={
            "StringEquals": {
                "aws:SourceAccount": self.account,
            },
        },
    ))
    # GT Plus needs KMS decrypt
    self.kms_key.grant_decrypt(
        iam.ServicePrincipal("ground-truth-labeling.sagemaker.amazonaws.com"))

    # B) Output bucket — GT Plus writes labels here
    self.gt_plus_output = s3.Bucket(self, "GtPlusOutput",
        bucket_name=f"{{project_name}}-gtplus-output-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
        removal_policy=RemovalPolicy.RETAIN,
    )
    self.gt_plus_output.add_to_resource_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        principals=[iam.ServicePrincipal("ground-truth-labeling.sagemaker.amazonaws.com")],
        actions=["s3:PutObject", "s3:PutObjectAcl", "s3:ListBucket"],
        resources=[
            self.gt_plus_output.bucket_arn,
            f"{self.gt_plus_output.bucket_arn}/*",
        ],
    ))
    self.kms_key.grant_encrypt(
        iam.ServicePrincipal("ground-truth-labeling.sagemaker.amazonaws.com"))

    # C) Trigger Lambda — when batch completes, kick off training pipeline
    batch_complete_fn = lambda_.Function(self, "GtPlusBatchCompleteFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "gt_plus_batch_complete")),
        timeout=Duration.minutes(5),
        environment={
            "PIPELINE_NAME":       f"{{project_name}}-train-pipeline-{stage}",
            "OUTPUT_BUCKET":       self.gt_plus_output.bucket_name,
            "MIN_BATCH_SIZE":      "5000",                     # only kick off training if enough labels
        },
    )
    self.gt_plus_output.grant_read(batch_complete_fn)
    batch_complete_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/*"],
    ))

    # Trigger on `manifest.jsonl` in batch folder
    self.gt_plus_output.add_event_notification(
        s3.EventType.OBJECT_CREATED,
        s3n.LambdaDestination(batch_complete_fn),
        s3.NotificationKeyFilter(suffix="manifest.jsonl"),
    )
```

### 3.4 Output manifest format (what GT Plus delivers)

```jsonl
{"source-ref": "s3://input/img-001.jpg", "label-bbox": {"image_size": [{"width":1920,"height":1080,"depth":3}], "annotations": [{"class_id": 0, "left": 100, "top": 200, "width": 50, "height": 75}]}, "label-bbox-metadata": {"objects": [{"confidence": 0.99}], "class-map": {"0": "tumor"}, "human-annotated": "yes", "creation-date": "2026-04-15T10:23:45.000Z", "type": "groundtruth/object-detection"}}
{"source-ref": "s3://input/img-002.jpg", ...}
```

This format is the **exact same** as self-managed Ground Truth — direct drop-in for `sagemaker.image_uris.retrieve` algorithms or HF training.

### 3.5 Downstream training trigger Lambda

```python
"""gt_plus_batch_complete/index.py"""
import os, json, boto3

s3 = boto3.client("s3")
sm = boto3.client("sagemaker")


def handler(event, context):
    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    key    = event["Records"][0]["s3"]["object"]["key"]

    # Count labels in manifest
    obj = s3.get_object(Bucket=bucket, Key=key)
    manifest = obj["Body"].read().decode()
    label_count = sum(1 for _ in manifest.splitlines() if _.strip())

    if label_count < int(os.environ["MIN_BATCH_SIZE"]):
        print(f"Skipping pipeline trigger: only {label_count} labels (< {os.environ['MIN_BATCH_SIZE']})")
        return

    # Parse batch folder from key — expected format batches/<batch_id>/manifest.jsonl
    batch_id = key.split("/")[1]

    # Trigger training pipeline
    response = sm.start_pipeline_execution(
        PipelineName=os.environ["PIPELINE_NAME"],
        PipelineParameters=[
            {"Name": "InputDataUri", "Value": f"s3://{bucket}/{key}"},
            {"Name": "BatchId",      "Value": batch_id},
        ],
        PipelineExecutionDisplayName=f"gt-plus-batch-{batch_id}",
    )

    return {"pipelineExecutionArn": response["PipelineExecutionArn"]}
```

---

## 4. Engagement flow + cost expectations

### 4.1 Typical timeline

| Phase | Duration | What happens |
|---|---|---|
| Kickoff | 1 week | Sales engineer + AWS account team scope project |
| Onboarding | 2-4 weeks | AWS PM finalizes labeling guidelines, sets up workforce, signs SOW |
| Pilot | 1-2 weeks | 100-1,000 objects labeled; you review quality, refine guidelines |
| Production batches | Ongoing | Regular batches (10K-100K each); QC review by AWS PM |
| Project closeout | 1 week | Final deliverable manifest |

### 4.2 Cost expectations

| Task type | Per-object price | Volume discount |
|---|---|---|
| Image classification (single label) | $0.05-$0.12 | 50% at 100K+ |
| Image bounding box (1-3 boxes/image) | $0.30-$0.80 | 30% at 100K+ |
| Image semantic segmentation | $1-$5 | 20% at 100K+ |
| NER (named entity recognition) | $0.10-$0.50 | 30% at 100K+ |
| Medical imaging (radiologist) | $5-$50 | Negotiated |
| Legal contract review (lawyer) | $5-$50 | Negotiated |

For comparison: self-managed GT with Mechanical Turk runs ~$0.012-$0.10 per task — but includes none of AWS's PM, QC, or workforce management.

---

## 5. Five non-negotiables

1. **GT Plus is NOT a self-serve service — engage the AWS account team first.** Don't try to provision it via console alone — you'll get stuck on workforce assignment.

2. **Output manifest schema is binding.** The training pipeline downstream MUST handle the exact GT-format JSON. Test with sample manifest before going live.

3. **KMS grant on input + output buckets to `ground-truth-labeling.sagemaker.amazonaws.com`.** Without it, GT Plus workforce can't read encrypted data.

4. **Min batch trigger threshold (`MIN_BATCH_SIZE`).** Without it, training pipeline kicks off on tiny batches (e.g. 50 objects), wasting compute.

5. **Audit trail of who labeled what.** GT Plus output includes `worker-id` per label — preserve in Pipeline + Model Card for regulator audits.

---

## 6. References

- AWS docs:
  - [Ground Truth Plus overview](https://docs.aws.amazon.com/sagemaker/latest/dg/gtp.html)
  - [GT Plus pricing](https://aws.amazon.com/sagemaker/groundtruth/pricing/)
  - [Output manifest format](https://docs.aws.amazon.com/sagemaker/latest/dg/sms-output.html)
- Related SOPs:
  - `MLOPS_GROUND_TRUTH` — self-managed alternative
  - `MLOPS_SAGEMAKER_TRAINING` — pipeline that consumes GT Plus output

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — supporting infra for GT Plus engagement (input + output buckets, IAM grants for GT Plus service principal, batch-complete trigger to training pipeline). Cost + timeline expectations. Created Wave 7 (2026-04-26). |
