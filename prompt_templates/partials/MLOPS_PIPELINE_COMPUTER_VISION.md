# SOP — MLOps Pipeline: Computer Vision (Async Inference, YOLOv8 / ViT / SAM)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Async Inference · GPU inference (`ml.g4dn.*` / `ml.g5.*`) · S3 image store · SNS completion notifications · YOLOv8 / Detectron2 / SAM training

---

## 1. Purpose

- Provision an async-inference CV endpoint: image submitted to S3 → `InvokeEndpointAsync` → result written to output S3 → SNS notification on success/error.
- Codify the **async endpoint config**: `max_concurrent_invocations_per_instance=4`, output KMS-encrypted, SNS success + error topics; managed instance scaling (1 → 4).
- Codify the **output bucket** with 7-day lifecycle expiration (async results are ephemeral by design).
- Codify the **submission Lambda**: accept either `image_s3_uri` or `image_base64`, upload to input bucket if needed, call `invoke_endpoint_async`, return inference ID + output location.
- Codify the YOLOv8 training script (`ml/scripts/cv_train.py`) using the ultralytics package and ONNX export for faster inference.
- Include when the SOW mentions image analysis, object detection, quality inspection, medical imaging, document OCR, video analysis, or visual QA/QC.

**CV task → model selection:**

| Task | Model | GPU instance |
|---|---|---|
| Image Classification | ResNet-50, EfficientNet-B4, ViT | `ml.g4dn.xlarge` |
| Object Detection | YOLOv8, Detectron2 Faster-RCNN | `ml.g4dn.2xlarge` |
| Instance Segmentation | Mask R-CNN, SAM (Meta) | `ml.g4dn.4xlarge` |
| Medical Imaging | nnU-Net, Med-SAM | `ml.g5.2xlarge` |
| Document OCR + Layout | Amazon Textract (managed), LayoutLMv3 | API / `ml.g4dn` |
| Video Frame Analysis | VideoMAE, X3D | `ml.g5.xlarge` |
| Defect Detection (manufacturing) | Anomalib, PatchCore | `ml.g4dn.xlarge` |

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns endpoint config + output bucket + submission Lambda + input bucket grant | **§3 Monolith Variant** |
| `DataLakeStack` owns input/image bucket, `ServingStack` owns endpoint, `CVPipelineStack` owns endpoint config + output bucket + submit Lambda | **§4 Micro-Stack Variant** |

**Why the split matters.** The submission Lambda needs `sagemaker:InvokeEndpointAsync` on the CV endpoint (owned by `ServingStack`) and `s3:PutObject` on the input bucket (owned by `DataLakeStack`). Monolith: `bucket.grant_read_write(fn)` and `endpoint.grant_invoke(fn)` are local. Micro-stack: both would cycle — use identity-side grants with bucket names / endpoint names read from SSM. The output bucket ideally lives in `CVPipelineStack` (L2 grants stay local to async endpoint config, which takes KMS ARN string — fifth non-negotiable).

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Architecture

```
INPUT:
  Client → CVInferenceFn
    ├── image_base64  → S3 put (lake_buckets['raw']/cv-inputs/<uuid>.jpg)
    └── image_s3_uri  → direct

ASYNC INFERENCE:
  CVInferenceFn → invoke_endpoint_async(InputLocation=s3 uri)
    → returns { inference_id, output_location }

ENDPOINT (GPU):
  CfnEndpointConfig
    async_inference_config
      client_config: max_concurrent_invocations_per_instance=4
      output_config
        s3_output_path → async_output_bucket
        success_topic / error_topic → self.alert_topic
    production_variants: g4dn.xlarge / g4dn.2xlarge (prod)
    managed_instance_scaling: 1 → 4

COMPLETION:
  Endpoint writes result JSON to output S3 → SNS notification fires
  Subscribers fetch result from output_location
```

### 3.2 CDK — `_create_computer_vision_pipeline` method body

```python
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_sagemaker as sagemaker,
)


def _create_computer_vision_pipeline(self, stage_name: str) -> None:
    """
    Computer Vision ML Pipeline.

    Assumes self.{lake_buckets, kms_key, alert_topic} set.

    Pipeline Steps:
      1. Image Preprocessing — resize, normalize, augment, convert to RecordIO/TFRecord
      2. Model Training — GPU-based training with SageMaker
      3. Model Evaluation — precision, recall, mAP (for detection), IoU (for segmentation)
      4. Model Registration + Endpoint Deployment
      5. Async Inference Configuration (for large images / video frames)
    """

    # =========================================================================
    # CV ASYNC INFERENCE ENDPOINT
    # (Async = better for large images, video frames, batch OCR)
    # =========================================================================

    # Output bucket for async inference results
    async_output_bucket = s3.Bucket(
        self, "CVAsyncOutput",
        bucket_name=f"{{project_name}}-cv-async-output-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7), enabled=True)],
        removal_policy=RemovalPolicy.DESTROY,
    )

    # Async endpoint config
    cv_endpoint_config = sagemaker.CfnEndpointConfig(
        self, "CVEndpointConfig",
        endpoint_config_name=f"{{project_name}}-cv-{stage_name}",
        async_inference_config=sagemaker.CfnEndpointConfig.AsyncInferenceConfigProperty(
            client_config=sagemaker.CfnEndpointConfig.AsyncInferenceClientConfigProperty(
                max_concurrent_invocations_per_instance=4,  # 4 parallel image inferences
            ),
            output_config=sagemaker.CfnEndpointConfig.AsyncInferenceOutputConfigProperty(
                s3_output_path=f"s3://{async_output_bucket.bucket_name}/results/",
                kms_key_id=self.kms_key.key_arn,
                notification_config=sagemaker.CfnEndpointConfig.AsyncInferenceNotificationConfigProperty(
                    success_topic=self.alert_topic.topic_arn,  # Notify on completion
                    error_topic=self.alert_topic.topic_arn,    # Notify on failure
                ),
            ),
        ),
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="AllTraffic",
                model_name=f"{{project_name}}-cv-model-{stage_name}",
                instance_type="ml.g4dn.xlarge" if stage_name != "prod" else "ml.g4dn.2xlarge",
                initial_instance_count=1,
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    status="ENABLED",
                    min_instance_count=1,
                    max_instance_count=4,
                ),
            )
        ],
        kms_key_id=self.kms_key.key_arn,
    )

    # =========================================================================
    # IMAGE SUBMISSION LAMBDA — Async inference submission
    # =========================================================================

    cv_submit_fn = _lambda.Function(
        self, "CVInferenceFn",
        function_name=f"{{project_name}}-cv-inference-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/cv_inference_submit"),
        environment={
            "ENDPOINT_NAME": f"{{project_name}}-cv-{stage_name}",
            "INPUT_BUCKET":  self.lake_buckets["raw"].bucket_name,
        },
        memory_size=256,
        timeout=Duration.seconds(30),
        tracing=_lambda.Tracing.ACTIVE,
    )
    cv_submit_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpointAsync"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-cv-{stage_name}"],
    ))
    self.lake_buckets["raw"].grant_read_write(cv_submit_fn)

    CfnOutput(self, "CVInferenceFnArn",
        value=cv_submit_fn.function_arn,
        description="Submit image for CV inference (async)",
        export_name=f"{{project_name}}-cv-inference-{stage_name}",
    )
    CfnOutput(self, "CVAsyncOutputBucket",
        value=async_output_bucket.bucket_name,
        description="Async inference output bucket (7-day expiry)",
        export_name=f"{{project_name}}-cv-async-output-{stage_name}",
    )
```

### 3.3 Submission handler (`lambda/cv_inference_submit/index.py`)

```python
"""Submit an image for async CV inference. Accepts either S3 URI or base64 payload."""
import base64, boto3, logging, os, uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)
sm_runtime = boto3.client('sagemaker-runtime')
s3 = boto3.client('s3')

ENDPOINT_NAME = os.environ['ENDPOINT_NAME']
INPUT_BUCKET  = os.environ['INPUT_BUCKET']


def handler(event, context):
    image_s3_uri = event.get('image_s3_uri')
    image_base64 = event.get('image_base64')

    # If image sent directly, upload to S3 first (async needs S3 input)
    if image_base64:
        key = f"cv-inputs/{uuid.uuid4()}.jpg"
        s3.put_object(Bucket=INPUT_BUCKET, Key=key, Body=base64.b64decode(image_base64))
        image_s3_uri = f"s3://{INPUT_BUCKET}/{key}"

    # Submit async inference — returns immediately with inference ID
    response = sm_runtime.invoke_endpoint_async(
        EndpointName=ENDPOINT_NAME,
        InputLocation=image_s3_uri,        # S3 URI of the image
        ContentType='application/x-image',
        Accept='application/json',
        InferenceId=str(uuid.uuid4()),     # Unique ID to correlate result
    )
    return {
        'statusCode': 202,                 # Accepted — not yet complete
        'inference_id':     response['InferenceId'],
        'output_location':  response['OutputLocation'],  # Poll this S3 URI for result
    }
```

### 3.4 Training script (`ml/scripts/cv_train.py`)

```python
# ml/scripts/cv_train.py — Example: YOLOv8 object detection on SageMaker
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "ultralytics", "-q"])

from ultralytics import YOLO
import argparse, os


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model",      default="yolov8n.pt")  # nano=fast, x=accurate
    p.add_argument("--epochs",     type=int, default=100)
    p.add_argument("--imgsz",      type=int, default=640)
    p.add_argument("--batch",      type=int, default=16)
    p.add_argument("--device",     default="0")            # GPU 0
    p.add_argument("--data",       default="/opt/ml/input/data/train/dataset.yaml")
    p.add_argument("--output_dir", default="/opt/ml/model")
    p.add_argument("--project",    default="/opt/ml/output")
    return p.parse_args()


def main():
    args = parse_args()
    model = YOLO(args.model)
    results = model.train(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=args.project,
        patience=20,              # Early stopping
        save=True,
        plots=True,               # Saves confusion matrix, PR curve
        augment=True,             # Random flips, mosaic, mixup
    )
    # Export to ONNX for SageMaker serving (faster than PyTorch)
    model.export(format="onnx", dynamic=True, simplify=True)


if __name__ == "__main__":
    main()
```

### 3.5 Monolith gotchas

- **Async inference requires the input in S3** — payload is not supported directly; the submission Lambda must upload first (handled via the base64 branch above).
- **`ContentType='application/x-image'`** is the default; custom containers should use `application/json` with base64-embedded image for flexibility.
- **SNS notification fan-out** — both success and error topics in the snippet point to `self.alert_topic` (one topic). Production usually splits into `cv-success-topic` and `cv-error-topic` with separate subscriptions.
- **Lifecycle 7-day expiration** on the output bucket is non-negotiable for cost — async results pile up fast; consumers must fetch within 7 days or lose them.
- **GPU cold-start latency** — `g4dn.xlarge` takes ~2 minutes to boot a new instance; `managed_instance_scaling` reacts to backlog, so first burst sees that latency. Pre-warm in prod by setting `min_instance_count=2`.
- **YOLOv8 `pip install ultralytics` at runtime** works but adds 15–30 s to every cold start; for production, bake ultralytics into a custom Docker image.
- **ONNX export `dynamic=True`** enables variable batch sizes; drop it only if the serving container pins a specific batch size.

---

## 4. Micro-Stack Variant

**Use when:** `CVPipelineStack` is separate from `DataLakeStack` (owns input/image bucket) and `ServingStack` (owns endpoint).

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)` via `_LAMBDAS_ROOT`.
2. **Never call `bucket.grant_read_write(fn)`** on the input bucket across stacks — identity-side `s3:PutObject` + `s3:GetObject` scoped to the bucket ARN (from SSM).
3. **Never target cross-stack queues** — async completion goes to SNS, not SQS.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on the output bucket — use a local CMK. `CfnEndpointConfig.kms_key_id` accepts the ARN string, so cross-stack keys use the string form.

### 4.2 `CVPipelineStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sagemaker as sagemaker,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class CVPipelineStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        cv_endpoint_name_ssm: str,          # /proj/ml/cv_endpoint_name
        cv_model_name_ssm: str,             # /proj/ml/cv_model_name
        input_bucket_name_ssm: str,         # /proj/lake/raw_bucket
        success_topic_arn_ssm: str,
        error_topic_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-cv-pipeline-{stage_name}", **kwargs)

        endpoint_name       = ssm.StringParameter.value_for_string_parameter(self, cv_endpoint_name_ssm)
        model_name          = ssm.StringParameter.value_for_string_parameter(self, cv_model_name_ssm)
        input_bucket_name   = ssm.StringParameter.value_for_string_parameter(self, input_bucket_name_ssm)
        success_topic_arn   = ssm.StringParameter.value_for_string_parameter(self, success_topic_arn_ssm)
        error_topic_arn     = ssm.StringParameter.value_for_string_parameter(self, error_topic_arn_ssm)

        # Local CMK for output bucket (fifth non-negotiable)
        cmk = kms.Key(self, "CVOutputKey",
            alias=f"alias/{{project_name}}-cv-output-{stage_name}",
            enable_key_rotation=True, rotation_period=Duration.days(365),
        )

        # Output bucket (local)
        async_output_bucket = s3.Bucket(self, "CVAsyncOutput",
            bucket_name=f"{{project_name}}-cv-async-output-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7), enabled=True)],
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Async endpoint config — KMS ARN as STRING
        endpoint_config = sagemaker.CfnEndpointConfig(
            self, "CVEndpointConfig",
            endpoint_config_name=f"{{project_name}}-cv-{stage_name}",
            async_inference_config=sagemaker.CfnEndpointConfig.AsyncInferenceConfigProperty(
                client_config=sagemaker.CfnEndpointConfig.AsyncInferenceClientConfigProperty(
                    max_concurrent_invocations_per_instance=4,
                ),
                output_config=sagemaker.CfnEndpointConfig.AsyncInferenceOutputConfigProperty(
                    s3_output_path=f"s3://{async_output_bucket.bucket_name}/results/",
                    kms_key_id=cmk.key_arn,
                    notification_config=sagemaker.CfnEndpointConfig.AsyncInferenceNotificationConfigProperty(
                        success_topic=success_topic_arn,
                        error_topic=error_topic_arn,
                    ),
                ),
            ),
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=model_name,
                    instance_type="ml.g4dn.xlarge" if stage_name != "prod" else "ml.g4dn.2xlarge",
                    initial_instance_count=1,
                    managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                        status="ENABLED",
                        min_instance_count=1,
                        max_instance_count=4,
                    ),
                )
            ],
            kms_key_id=cmk.key_arn,
        )

        # Submission Lambda
        log_group = logs.LogGroup(self, "SubmitLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-cv-inference-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        cv_submit_fn = _lambda.Function(self, "CVInferenceFn",
            function_name=f"{{project_name}}-cv-inference-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "cv_inference_submit")),
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=log_group,
            environment={
                "ENDPOINT_NAME": endpoint_name,
                "INPUT_BUCKET":  input_bucket_name,
            },
        )
        # Identity-side: invoke async CV endpoint (cross-stack endpoint)
        cv_submit_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpointAsync"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"],
        ))
        # Identity-side: put/get on the cross-stack input bucket
        cv_submit_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[f"arn:aws:s3:::{input_bucket_name}/cv-inputs/*"],
        ))
        iam.PermissionsBoundary.of(cv_submit_fn.role).apply(permission_boundary)

        cdk.CfnOutput(self, "CVInferenceFnArn",
            value=cv_submit_fn.function_arn,
            export_name=f"{{project_name}}-cv-inference-{stage_name}",
        )
        cdk.CfnOutput(self, "CVAsyncOutputBucket",
            value=async_output_bucket.bucket_name,
            export_name=f"{{project_name}}-cv-async-output-{stage_name}",
        )
```

### 4.3 Micro-stack gotchas

- **Local CMK for the output bucket** keeps the fifth non-negotiable honoured. The async endpoint config accepts the KMS ARN string via `kms_key_id=` — don't try to pass `cmk` directly when the CMK is cross-stack (here it's local, but the pattern stays identity/string-based).
- **Success/error topic ARNs as strings** — CDK's `AsyncInferenceNotificationConfigProperty` accepts ARN strings. No construct ref needed, so cross-stack SNS topics work fine.
- **Input bucket ARN scoping** — `s3:PutObject/GetObject` on `arn:aws:s3:::{bucket}/cv-inputs/*` (prefix-scoped); never grant bucket-wide.
- **Endpoint is NOT created here** — `CfnEndpointConfig` belongs to this stack; the `CfnEndpoint` is created by the deployer Lambda after model approval (see `MLOPS_SAGEMAKER_SERVING`). Deploy order: ServingStack (endpoint) → MLPlatformStack (model approval → creates endpoint) → this stack reads endpoint name from SSM.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx | §4 Micro-Stack |
| Switch from async → real-time (sub-100 ms latency) | Drop `async_inference_config`; use `InvokeEndpoint` instead of async variant |
| Swap YOLOv8 → SAM (segmentation) | Change model container + instance to `ml.g4dn.4xlarge` |
| Video frame analysis | Add a pre-processor Lambda that extracts frames; fan out to multiple async calls |
| OCR + layout | Replace with Amazon Textract SDK call (no endpoint needed) |
| Very large images (satellite, medical) | Use `MaxPayloadInMB=100` on async invoke; scale instance memory |

---

## 6. Worked example — CVPipelineStack synthesizes

Save as `tests/sop/test_MLOPS_PIPELINE_COMPUTER_VISION.py`. Offline.

```python
"""SOP verification — CVPipelineStack synthesizes endpoint config + submit Lambda + output bucket."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_cv_pipeline_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.cv_pipeline_stack import CVPipelineStack
    stack = CVPipelineStack(
        app, stage_name="prod",
        cv_endpoint_name_ssm="/test/ml/cv_endpoint_name",
        cv_model_name_ssm="/test/ml/cv_model_name",
        input_bucket_name_ssm="/test/lake/raw_bucket",
        success_topic_arn_ssm="/test/obs/cv_success_topic_arn",
        error_topic_arn_ssm="/test/obs/cv_error_topic_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function",          1)
    t.resource_count_is("AWS::S3::Bucket",                1)
    t.resource_count_is("AWS::SageMaker::EndpointConfig", 1)
    t.resource_count_is("AWS::KMS::Key",                  1)
```

---

## 7. References

- `docs/template_params.md` — `CV_ENDPOINT_NAME_SSM`, `CV_MODEL_NAME_SSM`, `CV_INPUT_BUCKET_SSM`, `CV_SUCCESS_TOPIC_ARN_SSM`, `CV_ERROR_TOPIC_ARN_SSM`, `CV_ASYNC_MAX_CONCURRENT`
- `docs/Feature_Roadmap.md` — feature IDs `ML-60` (CV async inference), `ML-61` (YOLOv8 training), `ML-62` (SAM segmentation)
- SageMaker Async Inference: https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference.html
- Ultralytics YOLOv8: https://docs.ultralytics.com/
- Segment Anything (SAM): https://github.com/facebookresearch/segment-anything
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (Model Group), `MLOPS_SAGEMAKER_SERVING` (endpoint deployer), `LAYER_DATA` (image bucket), `LAYER_BACKEND_LAMBDA` (five non-negotiables), `LAYER_OBSERVABILITY` (SNS success/error topics)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `CVPipelineStack` reads endpoint name + model name + input bucket + SNS topic ARNs via SSM; identity-side `sagemaker:InvokeEndpointAsync` scoped to endpoint ARN; identity-side `s3:PutObject/GetObject` scoped to `cv-inputs/*` prefix; local CMK for output bucket (5th non-negotiable). Extracted inline submit Lambda to `lambda/cv_inference_submit/` asset. Kept async endpoint config, output bucket lifecycle, YOLOv8 training script. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — CV task table, async inference endpoint, image submission Lambda, YOLOv8 training script. |
