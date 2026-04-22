# SOP — MLOps SageMaker Clarify (Bias Detection + SHAP Explainability)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Clarify ProcessingJob image v1+ · `aws_sagemaker` L1 · `aws_lambda` Python 3.13 · `sagemaker-runtime` · CloudWatch custom metrics

---

## 1. Purpose

- Provision the Clarify bias-analysis pipeline (pre-training + post-training) and SHAP explainability round-trip for regulated workloads (HIPAA, financial, EU AI Act, SOC 2 with ML).
- Codify the **Clarify results bucket** with 7-year retention, KMS encryption, `RETAIN` removal policy — compliance artifact store.
- Provide a **bias-trigger Lambda** that creates a `ProcessingJob` with `--analysis-type bias`, the canonical config (sensitive attribute + positive label + pre/post metrics), and the SageMaker Clarify ECR image URI.
- Provide an **explainability Lambda** that forwards `CustomAttributes={"explain": true}` to the live endpoint, returning per-feature SHAP values.
- Wire a **bias-violation alarm** on a custom CloudWatch metric (`<project>/Clarify BiasViolationDetected`) which a downstream Lambda emits when the processing job report flags the model as biased.
- Include when the SOW mentions model explainability, SHAP, feature importance, bias detection, fairness, or regulatory reporting.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns clarify bucket + triggers + role + endpoint | **§3 Monolith Variant** |
| `MLPlatformStack` owns SageMaker role, `ServingStack` owns endpoint, `ClarityStack` owns bucket + Lambdas | **§4 Micro-Stack Variant** |

**Why the split matters.** The bias-trigger Lambda needs `iam:PassRole` on the SageMaker execution role (owned by `MLPlatformStack`), and the explainability Lambda needs `sagemaker:InvokeEndpoint` on the endpoint ARN (owned by `ServingStack`). In monolith those are local; across stacks use identity-side grants with ARNs read from SSM. The 7-year-retention Clarify bucket is best owned by the compliance stack (`ClarifyStack`) and its name published via SSM so reports flow into a single audit lake.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 What Clarify does

```
Two jobs, both run as SageMaker ProcessingJobs:

1. BIAS ANALYSIS (pre-training + post-training):
   Pre-training   → is my TRAINING DATA biased? (before any model exists)
   Post-training  → does my TRAINED MODEL produce biased predictions?
   Metrics        : DPL (demographic parity), DI (disparate impact), FTd, KL divergence

2. EXPLAINABILITY (SHAP):
   For every prediction: "which features contributed most to this outcome?"
   Uses Kernel SHAP — model-agnostic, works with any model type
   Output: per-feature SHAP values + global feature importance
```

### 3.2 CDK — Clarify bucket + bias trigger + explainability + alarm

```python
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
)


def _create_clarify(self, stage_name: str) -> None:
    """Assumes self.{kms_key, sagemaker_role, lake_buckets, alert_topic} set earlier."""

    # -- Compliance bucket (7-year retention, KMS-encrypted) ------------------
    clarify_bucket = s3.Bucket(
        self, "ClarifyResults",
        bucket_name=f"{{project_name}}-clarify-{stage_name}-{Aws.ACCOUNT_ID}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="retain-audit-trail",
            enabled=True,
            expiration=Duration.days(365 * 7),
        )],
        removal_policy=RemovalPolicy.RETAIN,
    )
    clarify_bucket.grant_read_write(self.sagemaker_role)      # same-stack L2

    # -- Bias trigger Lambda --------------------------------------------------
    clarify_bias_fn = _lambda.Function(
        self, "ClarifyBiasFn",
        function_name=f"{{project_name}}-clarify-bias-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/clarify_bias"),
        timeout=Duration.minutes(5),
        environment={
            "DEFAULT_MODEL_NAME":    f"{{project_name}}-model-{stage_name}",
            "DATASET_URI":           f"s3://{self.lake_buckets['processed'].bucket_name}/validation/",
            "CLARIFY_BUCKET":        f"s3://{clarify_bucket.bucket_name}",
            "SAGEMAKER_ROLE_ARN":    self.sagemaker_role.role_arn,
            "FEATURE_HEADERS":       '["age","income","gender","loan_amount","credit_score","label"]',
            "LABEL_COLUMN":          "label",
            "SENSITIVE_ATTRIBUTE":   "gender",   # [Claude: update per SOW]
            "SENSITIVE_VALUES":      '["female","non-binary"]',
            "POSITIVE_LABEL_VALUES": '[1]',
            "CLARIFY_IMAGE_URI":     f"306415355426.dkr.ecr.{Aws.REGION}.amazonaws.com/sagemaker-clarify-processing:1",
            "PROJECT_NAME":          "{project_name}",
        },
    )
    clarify_bias_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob"],
        resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:processing-job/clarify-*"],
    ))
    clarify_bias_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.sagemaker_role.role_arn],
        conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
    ))
    clarify_bucket.grant_read_write(clarify_bias_fn)

    # -- Bias-violation alarm (custom metric) ---------------------------------
    cw.Alarm(
        self, "BiasViolationAlarm",
        alarm_name=f"{{project_name}}-bias-violation-{stage_name}",
        alarm_description="Clarify detected bias — review before deployment",
        metric=cw.Metric(
            namespace=f"{{project_name}}/Clarify",
            metric_name="BiasViolationDetected",
            dimensions_map={"Stage": stage_name},
            period=Duration.hours(1),
            statistic="Maximum",
        ),
        threshold=0, evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )

    # -- SHAP explainability Lambda (live endpoint) ---------------------------
    explain_fn = _lambda.Function(
        self, "SHAPExplainFn",
        function_name=f"{{project_name}}-explain-prediction-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/shap_explain"),
        timeout=Duration.seconds(30),
        environment={"ENDPOINT_NAME": f"{{project_name}}-inference-{stage_name}"},
    )
    explain_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{{project_name}}-inference-{stage_name}"],
    ))

    CfnOutput(self, "ClarifyResultsBucket", value=clarify_bucket.bucket_name)
    CfnOutput(self, "ClarifyBiasFnArn",     value=clarify_bias_fn.function_arn)
    CfnOutput(self, "SHAPExplainFnArn",     value=explain_fn.function_arn)
```

### 3.3 Bias-trigger handler (`lambda/clarify_bias/index.py`)

```python
"""Start a SageMaker Clarify ProcessingJob for bias analysis."""
import boto3, json, logging, os
from datetime import datetime

logger = logging.getLogger()
sm     = boto3.client('sagemaker')


def handler(event, context):
    model_name     = event.get('model_name',   os.environ['DEFAULT_MODEL_NAME'])
    dataset_s3_uri = event.get('dataset_uri',  os.environ['DATASET_URI'])
    predictions_uri = event.get('predictions_uri', os.environ.get('PREDICTIONS_URI', ''))
    job_name = f"clarify-bias-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    clarify_config = {
        "version":      "1.0",
        "dataset_type": "text/csv",
        "dataset_uri":  dataset_s3_uri,
        "headers":      json.loads(os.environ['FEATURE_HEADERS']),
        "label":        os.environ['LABEL_COLUMN'],
        "label_values_or_threshold": json.loads(os.environ.get('POSITIVE_LABEL_VALUES', '[1]')),
        "facet": [{
            "name_or_index":      os.environ['SENSITIVE_ATTRIBUTE'],
            "value_or_threshold": json.loads(os.environ.get('SENSITIVE_VALUES', '[]')),
        }],
        "pre_training_bias_methods":  "all",
        "post_training_bias_methods": "all",
        "predicted_label":            "prediction",
        "predicted_label_dataset_uri": predictions_uri,
        "probability":                "probability",
        "probability_threshold":      0.5,
    }
    # [Claude: in production, write this config to s3://bucket/configs/{job_name}/analysis_config.json
    #  before creating the job — omitted here for brevity]

    resp = sm.create_processing_job(
        ProcessingJobName=job_name,
        ProcessingResources={
            "ClusterConfig": {
                "InstanceCount":  1,
                "InstanceType":   "ml.m5.xlarge",
                "VolumeSizeInGB": 20,
            }
        },
        AppSpecification={
            "ImageUri": os.environ['CLARIFY_IMAGE_URI'],
            "ContainerArguments": ["--analysis-type", "bias"],
        },
        ProcessingInputs=[{
            "InputName": "analysis_config",
            "S3Input": {
                "S3Uri":       f"{os.environ['CLARIFY_BUCKET']}/configs/{job_name}/",
                "LocalPath":   "/opt/ml/processing/input/config",
                "S3DataType":  "S3Prefix",
                "S3InputMode": "File",
            },
        }],
        ProcessingOutputs={
            "Outputs": [{
                "OutputName": "analysis_result",
                "S3Output": {
                    "S3Uri":        f"{os.environ['CLARIFY_BUCKET']}/results/{job_name}/",
                    "LocalPath":    "/opt/ml/processing/output",
                    "S3UploadMode": "EndOfJob",
                },
            }],
        },
        RoleArn=os.environ['SAGEMAKER_ROLE_ARN'],
        Tags=[
            {"Key": "Project",      "Value": os.environ.get('PROJECT_NAME', 'unknown')},
            {"Key": "ModelName",    "Value": model_name},
            {"Key": "AnalysisType", "Value": "bias"},
        ],
    )
    logger.info("Clarify bias job started: %s", job_name)
    return {"job_name": job_name, "job_arn": resp['ProcessingJobArn']}
```

### 3.4 SHAP handler (`lambda/shap_explain/index.py`)

```python
"""Invoke endpoint with CustomAttributes={explain: true} to get SHAP values."""
import boto3, json, logging, os

logger     = logging.getLogger()
sm_runtime = boto3.client('sagemaker-runtime')
ENDPOINT_NAME = os.environ['ENDPOINT_NAME']


def handler(event, context):
    payload = event.get('features', event.get('body', '{}'))
    if isinstance(payload, dict):
        payload = json.dumps(payload)

    response = sm_runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType='application/json',
        Body=payload,
        Accept='application/json',
        CustomAttributes=json.dumps({"explain": True}),   # endpoint must support this
    )
    result = json.loads(response['Body'].read())
    logger.info("Explanation generated: %s", json.dumps(result)[:200])
    return {'statusCode': 200, 'body': json.dumps(result)}
```

### 3.5 Monolith gotchas

- **`--analysis-type bias` vs `explainability`** — same Clarify image, two job runs. Run explainability separately (post-training, once per model version).
- **`analysis_config.json`** must exist in the S3 config prefix before the job starts. Either pre-write it in the bias-trigger Lambda, or generate it from a Step Function state. Missing config = immediate job failure.
- **`CustomAttributes={"explain": True}`** is a contract between the caller and the inference container. If the model doesn't implement the explain path, the endpoint ignores the header and returns a plain prediction. Validate inside the inference script.
- **7-year retention** is for financial / healthcare. For EU AI Act "high-risk" systems, retention requirements vary by jurisdiction; verify with legal before deploy.
- **Bias thresholds** — DPL close to 0, DI close to 1 = fair. Clarify flags violations when DPL > 0.1 or DI < 0.8 by default; tune per regulator guidance.

---

## 4. Micro-Stack Variant

**Use when:** production layout — `ClarifyStack` separate from `MLPlatformStack` and `ServingStack`.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `bucket.grant_read_write(sm_role)`** across stacks — identity-side on the SageMaker role from `MLPlatformStack`. Here the Clarify bucket is in `ClarifyStack`, so grant read/write on the SageMaker role via SSM-read of the bucket name + identity-side `PolicyStatement` in the `MLPlatformStack` (one-way upstream).
3. **Never target cross-stack queues** — not relevant.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** — clarify bucket owns its own CMK; reads from SSM the project's lake key only when referenced as ARN string.

### 4.2 `ClarifyStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class ClarifyStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        sagemaker_role_arn_ssm: str,
        processed_bucket_ssm: str,
        serving_endpoint_name_ssm: str,
        alert_topic_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-clarify-{stage_name}", **kwargs)

        sagemaker_role_arn = ssm.StringParameter.value_for_string_parameter(self, sagemaker_role_arn_ssm)
        processed_bucket   = ssm.StringParameter.value_for_string_parameter(self, processed_bucket_ssm)
        endpoint_name      = ssm.StringParameter.value_for_string_parameter(self, serving_endpoint_name_ssm)
        alert_topic_arn    = ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm)

        # Local CMK for the compliance bucket (avoids fifth non-negotiable)
        cmk = kms.Key(self, "ClarifyKey",
            alias=f"alias/{{project_name}}-clarify-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        clarify_bucket = s3.Bucket(self, "ClarifyResults",
            bucket_name=f"{{project_name}}-clarify-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            versioned=True,
            lifecycle_rules=[s3.LifecycleRule(
                id="retain-audit-trail",
                enabled=True,
                expiration=Duration.days(365 * 7),
            )],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Bias trigger Lambda
        bias_log = logs.LogGroup(self, "BiasLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-clarify-bias-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        bias_fn = _lambda.Function(self, "ClarifyBiasFn",
            function_name=f"{{project_name}}-clarify-bias-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "clarify_bias")),
            timeout=Duration.minutes(5),
            log_group=bias_log,
            environment={
                "DEFAULT_MODEL_NAME":    f"{{project_name}}-model-{stage_name}",
                "DATASET_URI":           f"s3://{processed_bucket}/validation/",
                "CLARIFY_BUCKET":        f"s3://{clarify_bucket.bucket_name}",
                "SAGEMAKER_ROLE_ARN":    sagemaker_role_arn,
                "FEATURE_HEADERS":       '["age","income","gender","loan_amount","credit_score","label"]',
                "LABEL_COLUMN":          "label",
                "SENSITIVE_ATTRIBUTE":   "gender",
                "SENSITIVE_VALUES":      '["female","non-binary"]',
                "POSITIVE_LABEL_VALUES": '[1]',
                "CLARIFY_IMAGE_URI":     f"306415355426.dkr.ecr.{Aws.REGION}.amazonaws.com/sagemaker-clarify-processing:1",
                "PROJECT_NAME":          "{project_name}",
            },
        )
        bias_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:processing-job/clarify-*"],
        ))
        bias_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[sagemaker_role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))
        clarify_bucket.grant_read_write(bias_fn)          # same-stack L2 safe
        iam.PermissionsBoundary.of(bias_fn.role).apply(permission_boundary)

        # Bias violation alarm
        cw.Alarm(self, "BiasViolationAlarm",
            alarm_name=f"{{project_name}}-bias-violation-{stage_name}",
            metric=cw.Metric(
                namespace=f"{{project_name}}/Clarify",
                metric_name="BiasViolationDetected",
                dimensions_map={"Stage": stage_name},
                period=Duration.hours(1),
                statistic="Maximum",
            ),
            threshold=0, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(
                sns.Topic.from_topic_arn(self, "AlertTopic", alert_topic_arn),
            )],
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        # SHAP Lambda
        explain_log = logs.LogGroup(self, "ExplainLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-explain-prediction-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        explain_fn = _lambda.Function(self, "SHAPExplainFn",
            function_name=f"{{project_name}}-explain-prediction-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "shap_explain")),
            timeout=Duration.seconds(30),
            log_group=explain_log,
            environment={"ENDPOINT_NAME": endpoint_name},
        )
        explain_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"],
        ))
        iam.PermissionsBoundary.of(explain_fn.role).apply(permission_boundary)

        cdk.CfnOutput(self, "ClarifyResultsBucket", value=clarify_bucket.bucket_name)
```

### 4.3 Micro-stack gotchas

- **SageMaker role ARN from SSM** is the correct shape for `iam:PassRole`. The Condition `iam:PassedToService=sagemaker.amazonaws.com` is non-negotiable — without it the Lambda can pass the role to any service.
- **Alert topic via `from_topic_arn`** — wraps the cross-stack ARN into an `ITopic` for `SnsAction`. Identity-side grant not needed because CloudWatch Alarms are granted separately by the action creator.
- **Clarify bucket owns its CMK** — avoids passing a cross-stack key. Bucket and key live in the same stack; other stacks only read the bucket *name* if they need to deposit data (rare).
- **Seven-year retention** means `RemovalPolicy.RETAIN` — if a stack is ever destroyed, the compliance artifacts survive. Budget for orphan cleanup after environment teardowns.
- **Endpoint name from SSM** — the SHAP Lambda resolves it at deploy time; at synth time it's a token. If the endpoint name changes (new environment), redeploy the Clarify stack.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack — `ClarifyStack` independent |
| Pre-training-only bias check | Run bias-trigger before training pipeline; no endpoint → skip SHAP |
| Many models | Wrap the bias-trigger in a Step Function loop invoked per model group |
| Per-region compliance | One `ClarifyStack` per region; reports stay regional |
| Custom Clarify image (internal algo) | Replace `CLARIFY_IMAGE_URI`; interface stays the same |
| No live explainability (offline only) | Remove SHAP Lambda; keep bias-trigger path |

---

## 6. Worked example — ClarifyStack synthesizes

Save as `tests/sop/test_MLOPS_CLARIFY_EXPLAINABILITY.py`. Offline.

```python
"""SOP verification — ClarifyStack synthesizes bucket + CMK + bias/SHAP Lambdas + alarm."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_clarify_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.clarify_stack import ClarifyStack
    stack = ClarifyStack(
        app, stage_name="prod",
        sagemaker_role_arn_ssm="/test/ml/sagemaker_role_arn",
        processed_bucket_ssm="/test/lake/processed_bucket",
        serving_endpoint_name_ssm="/test/ml/endpoint_name",
        alert_topic_arn_ssm="/test/obs/alert_topic_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::KMS::Key",         1)
    t.resource_count_is("AWS::S3::Bucket",       1)
    t.resource_count_is("AWS::Lambda::Function", 2)      # bias + SHAP
    t.resource_count_is("AWS::CloudWatch::Alarm", 1)
```

---

## 7. References

- `docs/template_params.md` — `SENSITIVE_ATTRIBUTE`, `SENSITIVE_VALUES`, `POSITIVE_LABEL_VALUES`, `CLARIFY_IMAGE_URI`, `CLARIFY_BUCKET_SSM`, `ENDPOINT_NAME_SSM`
- `docs/Feature_Roadmap.md` — feature IDs `ML-22` (bias), `ML-23` (explainability), `GOV-07` (regulated ML)
- SageMaker Clarify: https://docs.aws.amazon.com/sagemaker/latest/dg/clarify-configure-processing-jobs.html
- SHAP integration: https://docs.aws.amazon.com/sagemaker/latest/dg/clarify-online-explainability.html
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (pipeline integration point), `MLOPS_SAGEMAKER_SERVING` (endpoint that implements `CustomAttributes[explain]`), `COMPLIANCE_HIPAA_PCIDSS` (retention policy), `LAYER_SECURITY` (KMS), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `ClarifyStack` owns its CMK (avoids fifth non-negotiable), reads SageMaker role ARN / endpoint name / alert topic ARN / processed bucket name via SSM; identity-side grants with `iam:PassedToService` Condition on `iam:PassRole`. Extracted Lambda handlers to assets. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — Clarify bucket (7-year retention), bias trigger Lambda, bias alarm, SHAP Lambda. |
