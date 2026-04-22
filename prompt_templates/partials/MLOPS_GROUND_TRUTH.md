# SOP — MLOps SageMaker Ground Truth (Human Labeling Pipeline)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws_sagemaker` L1 (`CfnWorkforce`) · `aws_cognito` · SageMaker Ground Truth `CreateLabelingJob` API · Python 3.13 Lambda · SNS

---

## 1. Purpose

- Provision a Ground Truth labeling pipeline: **private Cognito-backed workforce** (for confidential data), labeling bucket with KMS + RETAIN, task-type → algorithm-ARN mapping, active-learning config.
- Provide the **labeling-trigger Lambda** that wraps `create_labeling_job` with per-task-type algorithm ARN selection (`IMAGE_CLASSIFICATION`, `TEXT_CLASSIFICATION`, `BOUNDING_BOX`, `SEMANTIC_SEGMENTATION`, `NAMED_ENTITY`), majority-vote quality config (3 labelers / item), and active-learning seed.
- Provide the **labeling-complete Lambda** — SNS-triggered; receives the output manifest URI and kicks off the downstream SageMaker training pipeline.
- Enforce compliance: MFA-required Cognito for labelers, `RETAIN` buckets, KMS encryption on volume + output.
- Include when the SOW mentions data labeling, annotation pipeline, active learning, or human-in-the-loop data prep.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns labeler Cognito pool + labeling bucket + triggers | **§3 Monolith Variant** |
| MS02-Identity already has a Cognito tenant pool; `GroundTruthStack` is separate with a dedicated LABELER pool; KMS + training pipeline in other stacks | **§4 Micro-Stack Variant** |

**Why the split matters.** Labeling produces sensitive training data. Production usually keeps the labeler workforce pool **separate** from the end-user auth pool, in a compliance-owned stack. The labeling bucket must be KMS-encrypted; in micro-stack its KMS key should live alongside the bucket (not in a shared data-lake stack) to avoid the fifth non-negotiable. The labeling-complete Lambda starts a training pipeline whose ARN comes from SSM — identity-side `states:StartExecution` / `sagemaker:StartPipelineExecution`.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Ground Truth at a glance

```
Raw data (images / text / audio / video) → Labeling job → Labeled dataset → Training

Workforce options:
  1. Amazon Mechanical Turk  — large, fast, low cost (~$0.012/label)
  2. AWS Marketplace vendors — specialized (medical, legal, language)
  3. Private workforce       — YOUR employees (confidential data)
  4. Automated labeling      — active-learning model labels easy examples (~$0.001/label)

Active learning (semi-automated):
  Round 1: humans label 10%
  Round 2: train small model, auto-label high-confidence
  Round 3: humans only label uncertain items
  Result: 70–80% automation at matching quality
```

### 3.2 CDK — labeling bucket, private workforce, trigger + complete Lambdas

```python
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_sagemaker as sagemaker,
)


def _create_ground_truth_labeling(self, stage_name: str) -> None:
    """Assumes self.{kms_key, sagemaker_role} set earlier."""

    # -- Labeling bucket ------------------------------------------------------
    labeling_bucket = s3.Bucket(
        self, "LabelingBucket",
        bucket_name=f"{{project_name}}-labeling-{stage_name}-{Aws.ACCOUNT_ID}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        removal_policy=RemovalPolicy.RETAIN,
    )
    labeling_bucket.grant_read_write(self.sagemaker_role)

    # -- Cognito pool for labelers (MFA-required, admin-created) --------------
    labeler_pool = cognito.UserPool(
        self, "LabelerUserPool",
        user_pool_name=f"{{project_name}}-labelers-{stage_name}",
        self_sign_up_enabled=False,
        sign_in_aliases=cognito.SignInAliases(email=True, username=True),
        password_policy=cognito.PasswordPolicy(
            min_length=12, require_uppercase=True, require_digits=True, require_symbols=True,
        ),
        mfa=cognito.Mfa.REQUIRED,
        mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
        account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
        removal_policy=RemovalPolicy.RETAIN,
    )
    labeler_client = labeler_pool.add_client("LabelerClient", generate_secret=True)

    sagemaker.CfnWorkforce(
        self, "LabelingWorkforce",
        workforce_name=f"{{project_name}}-labelers-{stage_name}",
        cognito_config=sagemaker.CfnWorkforce.CognitoConfigProperty(
            client_id=labeler_client.user_pool_client_id,
            user_pool=labeler_pool.user_pool_id,
        ),
    )

    # -- Labeling trigger Lambda ---------------------------------------------
    labeling_trigger_fn = _lambda.Function(
        self, "LabelingJobTrigger",
        function_name=f"{{project_name}}-labeling-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/labeling_trigger"),
        timeout=Duration.minutes(5),
        environment={
            "DEFAULT_TASK_TYPE":    "TEXT_CLASSIFICATION",
            "DEFAULT_MANIFEST_URI": f"s3://{labeling_bucket.bucket_name}/unlabeled-manifest.jsonl",
            "LABELING_BUCKET":      labeling_bucket.bucket_name,
            "KMS_KEY_ID":           self.kms_key.key_arn,
            "SAGEMAKER_ROLE_ARN":   self.sagemaker_role.role_arn,
            "WORKTEAM_ARN":         f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:workteam/private-crowd/{{project_name}}-labelers-{stage_name}",
            "AWS_REGION":           Aws.REGION,
            "STAGE":                stage_name,
        },
    )
    labeling_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateLabelingJob", "sagemaker:DescribeLabelingJob"],
        resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:labeling-job/{{project_name}}*"],
    ))
    labeling_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.sagemaker_role.role_arn],
        conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
    ))
    labeling_bucket.grant_read(labeling_trigger_fn)

    # -- Labeling complete Lambda (SNS subscriber → pipeline trigger) --------
    labeling_complete_fn = _lambda.Function(
        self, "LabelingCompleteFn",
        function_name=f"{{project_name}}-labeling-complete-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/labeling_complete"),
        timeout=Duration.seconds(30),
        environment={
            "TRAINING_PIPELINE_NAME": f"{{project_name}}-training-pipeline-{stage_name}",
        },
    )
    labeling_complete_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:pipeline/{{project_name}}-training-pipeline-{stage_name}"],
    ))

    CfnOutput(self, "LabelingBucketName",   value=labeling_bucket.bucket_name)
    CfnOutput(self, "LabelingTriggerFnArn", value=labeling_trigger_fn.function_arn)
    CfnOutput(self, "LabelerUserPoolId",    value=labeler_pool.user_pool_id)
```

### 3.3 Trigger handler (`lambda/labeling_trigger/index.py`)

```python
"""Create a SageMaker Ground Truth labeling job with active-learning config."""
import boto3, logging, os
from datetime import datetime

logger = logging.getLogger()
sm     = boto3.client('sagemaker')


def handler(event, context):
    task_type    = event.get('task_type',   os.environ['DEFAULT_TASK_TYPE'])
    manifest_uri = event.get('manifest_uri', os.environ['DEFAULT_MANIFEST_URI'])
    job_name     = f"{{project_name}}-label-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    # Ground Truth algorithm-specification ARNs (Amazon-owned account 027400017018)
    region = os.environ['AWS_REGION']
    ALGORITHM_ARNS = {
        "IMAGE_CLASSIFICATION":  f"arn:aws:sagemaker:{region}:027400017018:labeling-job-algorithm-specification/image-classification",
        "TEXT_CLASSIFICATION":   f"arn:aws:sagemaker:{region}:027400017018:labeling-job-algorithm-specification/text-classification",
        "BOUNDING_BOX":          f"arn:aws:sagemaker:{region}:027400017018:labeling-job-algorithm-specification/object-detection",
        "SEMANTIC_SEGMENTATION": f"arn:aws:sagemaker:{region}:027400017018:labeling-job-algorithm-specification/semantic-segmentation",
        "NAMED_ENTITY":          f"arn:aws:sagemaker:{region}:027400017018:labeling-job-algorithm-specification/named-entity-recognition",
    }

    resp = sm.create_labeling_job(
        LabelingJobName=job_name,
        LabelAttributeName="{project_name}-label",
        InputConfig={
            'DataSource':     {'S3DataSource': {'ManifestS3Uri': manifest_uri}},
            'DataAttributes': {'ContentClassifiers': ['FreeOfPersonallyIdentifiableInformation']},
        },
        OutputConfig={
            'S3OutputPath': f"s3://{os.environ['LABELING_BUCKET']}/completed-labels/",
            'KmsKeyId':     os.environ['KMS_KEY_ID'],
        },
        RoleArn=os.environ['SAGEMAKER_ROLE_ARN'],
        LabelCategoryConfigS3Uri=f"s3://{os.environ['LABELING_BUCKET']}/label-config/{task_type}/label_categories.json",
        HumanTaskConfig={
            'WorkteamArn': os.environ['WORKTEAM_ARN'],
            'UiConfig': {
                'UiTemplateS3Uri': f"s3://{os.environ['LABELING_BUCKET']}/ui-templates/{task_type}/template.html",
            },
            'PreHumanTaskLambdaArn': os.environ.get('PRE_ANNOTATION_LAMBDA_ARN', ''),
            'TaskTitle':             f"{{project_name}} — {task_type.replace('_', ' ').title()} Task",
            'TaskDescription':       f"Please review and annotate the following {task_type} item.",
            'TaskKeywords':          ['{project_name}', task_type.lower()],
            'TaskTimeLimitInSeconds':            300,
            'TaskAvailabilityLifetimeInSeconds': 60 * 60 * 24 * 7,
            'NumberOfHumanWorkersPerDataObject': 3,           # majority vote
            'AnnotationConsolidationConfig': {
                'AnnotationConsolidationLambdaArn': os.environ.get('CONSOLIDATION_LAMBDA_ARN', ''),
            },
        },
        LabelingJobAlgorithmsConfig={
            'LabelingJobAlgorithmSpecificationArn': ALGORITHM_ARNS.get(task_type, ALGORITHM_ARNS['TEXT_CLASSIFICATION']),
            'InitialActiveLearningModelArn':        event.get('seed_model_arn', ''),
            'LabelingJobResourceConfig': {
                'VolumeKmsKeyId': os.environ['KMS_KEY_ID'],
            },
        },
        Tags=[
            {'Key': 'Project',     'Value': '{project_name}'},
            {'Key': 'TaskType',    'Value': task_type},
            {'Key': 'Environment', 'Value': os.environ['STAGE']},
        ],
    )
    logger.info("Labeling job created: %s", resp['LabelingJobArn'])
    return {'job_name': job_name, 'job_arn': resp['LabelingJobArn']}
```

### 3.4 Complete handler (`lambda/labeling_complete/index.py`)

```python
"""On SNS notification that labeling is done, start the training pipeline."""
import boto3, json, logging, os

logger = logging.getLogger()
sm     = boto3.client('sagemaker')


def handler(event, context):
    message         = json.loads(event['Records'][0]['Sns']['Message'])
    output_manifest = message.get('LabelingJobOutput', {}).get('OutputDatasetS3Uri')
    logger.info("Labeling complete. Output manifest: %s", output_manifest)

    if not output_manifest:
        return {"status": "no_output_manifest"}

    resp = sm.start_pipeline_execution(
        PipelineName=os.environ['TRAINING_PIPELINE_NAME'],
        PipelineParameters=[
            {"Name": "DatasetS3Uri", "Value": output_manifest},
            {"Name": "DataSource",   "Value": "ground_truth"},
        ],
        PipelineExecutionDescription="Triggered by Ground Truth labeling completion",
        ClientRequestToken=context.aws_request_id,
    )
    logger.info("Training pipeline started: %s", resp['PipelineExecutionArn'])
    return {"pipeline_execution_arn": resp['PipelineExecutionArn']}
```

### 3.5 Monolith gotchas

- **Algorithm-specification ARN belongs to Amazon account `027400017018`.** Don't replace the account ID; each region has its own ARN.
- **`WorkteamArn` must be created outside the stack OR the workforce must exist.** `CfnWorkforce` creates the workforce *record* but the default workteam (`private-crowd/<name>`) is auto-created by SageMaker — verify via `aws sagemaker list-workteams` post-deploy.
- **`NumberOfHumanWorkersPerDataObject=3`** triples label cost. Use 1 in dev, 3 in prod for quality.
- **Consolidation Lambda** is required for majority-vote resolution. The default built-in ARN (Amazon-owned) works for built-in task types; custom tasks need your own consolidation function.
- **SNS subscription to labeling-complete topic** is not provisioned here — the SNS topic is Amazon-side and the subscription is configured as part of `HumanTaskConfig` or via `create_labeling_job`'s notification target (omitted for brevity; see SageMaker docs).
- **UI templates and category configs** must exist in S3 *before* the first labeling job starts. Keep them in the repo and upload via CI/CD.

---

## 4. Micro-Stack Variant

**Use when:** `GroundTruthStack` is dedicated; labeler Cognito pool is kept separate from end-user pool.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `bucket.grant_read_write(sm_role)`** across stacks — identity-side on the SageMaker role.
3. **Never target cross-stack queues** — use SNS → Lambda subscription owned locally.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** — labeling bucket owns a local CMK.

### 4.2 `GroundTruthStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy,
    aws_cognito as cognito,
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


class GroundTruthStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        sagemaker_role_arn_ssm: str,
        training_pipeline_name_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-ground-truth-{stage_name}", **kwargs)

        sagemaker_role_arn    = ssm.StringParameter.value_for_string_parameter(self, sagemaker_role_arn_ssm)
        training_pipeline_nm  = ssm.StringParameter.value_for_string_parameter(self, training_pipeline_name_ssm)

        cmk = kms.Key(self, "LabelingKey",
            alias=f"alias/{{project_name}}-labeling-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        labeling_bucket = s3.Bucket(self, "LabelingBucket",
            bucket_name=f"{{project_name}}-labeling-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        labeler_pool = cognito.UserPool(self, "LabelerUserPool",
            user_pool_name=f"{{project_name}}-labelers-{stage_name}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_uppercase=True, require_digits=True, require_symbols=True,
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        labeler_client = labeler_pool.add_client("LabelerClient", generate_secret=True)
        sagemaker.CfnWorkforce(self, "LabelingWorkforce",
            workforce_name=f"{{project_name}}-labelers-{stage_name}",
            cognito_config=sagemaker.CfnWorkforce.CognitoConfigProperty(
                client_id=labeler_client.user_pool_client_id,
                user_pool=labeler_pool.user_pool_id,
            ),
        )

        trig_log = logs.LogGroup(self, "TriggerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-labeling-trigger-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        trigger_fn = _lambda.Function(self, "LabelingJobTrigger",
            function_name=f"{{project_name}}-labeling-trigger-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "labeling_trigger")),
            timeout=Duration.minutes(5),
            log_group=trig_log,
            environment={
                "DEFAULT_TASK_TYPE":    "TEXT_CLASSIFICATION",
                "DEFAULT_MANIFEST_URI": f"s3://{labeling_bucket.bucket_name}/unlabeled-manifest.jsonl",
                "LABELING_BUCKET":      labeling_bucket.bucket_name,
                "KMS_KEY_ID":           cmk.key_arn,
                "SAGEMAKER_ROLE_ARN":   sagemaker_role_arn,
                "WORKTEAM_ARN":         f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:workteam/private-crowd/{{project_name}}-labelers-{stage_name}",
                "AWS_REGION":           Aws.REGION,
                "STAGE":                stage_name,
            },
        )
        trigger_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:CreateLabelingJob", "sagemaker:DescribeLabelingJob"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:labeling-job/{{project_name}}*"],
        ))
        trigger_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[sagemaker_role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))
        labeling_bucket.grant_read(trigger_fn)   # same-stack L2 safe
        iam.PermissionsBoundary.of(trigger_fn.role).apply(permission_boundary)

        complete_log = logs.LogGroup(self, "CompleteLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-labeling-complete-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        complete_fn = _lambda.Function(self, "LabelingCompleteFn",
            function_name=f"{{project_name}}-labeling-complete-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "labeling_complete")),
            timeout=Duration.seconds(30),
            log_group=complete_log,
            environment={"TRAINING_PIPELINE_NAME": training_pipeline_nm},
        )
        complete_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:StartPipelineExecution"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:pipeline/{training_pipeline_nm}"],
        ))
        iam.PermissionsBoundary.of(complete_fn.role).apply(permission_boundary)

        cdk.CfnOutput(self, "LabelingBucketName", value=labeling_bucket.bucket_name)
        cdk.CfnOutput(self, "LabelerUserPoolId",  value=labeler_pool.user_pool_id)
```

### 4.3 Micro-stack gotchas

- **`training_pipeline_name_ssm`** is the pipeline *name*, not an ARN. The identity-side grant builds the ARN with `Aws.REGION` and `Aws.ACCOUNT_ID` to stay unique per deploy target.
- **Labeler Cognito pool is SEPARATE from the end-user pool** (MS02). Don't share — labelers shouldn't inherit end-user app access.
- **`CfnWorkforce`** is account-wide; there can be only ONE private workforce per region per account. If you need two labeling tracks (e.g., medical + legal), use workteams (subsets of one workforce).
- **Manifest + UI template assets** — stage a CI step that uploads `ui-templates/*/template.html` and `label-config/*/label_categories.json` to the labeling bucket before the first labeling job. These are static assets, not managed by CDK.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack |
| Mechanical Turk (public workforce) | Replace private `WorkteamArn` with MTurk's `arn:aws:sagemaker:{region}:394669845002:workteam/public-crowd/default` |
| Marketplace vendor | Pick a vendor workteam ARN from AWS Marketplace; swap the env var |
| Custom task UI | Provide a custom `UiTemplateS3Uri` and `PreHumanTaskLambdaArn` + `AnnotationConsolidationLambdaArn` |
| HIPAA / PHI labeling | Private workforce only; bucket KMS + CloudTrail audit on `s3:GetObject`; Labeler Cognito MFA required |
| Pilot → Active learning | Set `InitialActiveLearningModelArn` from a prior labeling job's output |

---

## 6. Worked example — GroundTruthStack synthesizes

Save as `tests/sop/test_MLOPS_GROUND_TRUTH.py`. Offline.

```python
"""SOP verification — GroundTruthStack synthesizes pool + workforce + triggers."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ground_truth_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ground_truth_stack import GroundTruthStack
    stack = GroundTruthStack(
        app, stage_name="staging",
        sagemaker_role_arn_ssm="/test/ml/sagemaker_role_arn",
        training_pipeline_name_ssm="/test/ml/training_pipeline_name",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::KMS::Key",               1)
    t.resource_count_is("AWS::S3::Bucket",             1)
    t.resource_count_is("AWS::Cognito::UserPool",      1)
    t.resource_count_is("AWS::SageMaker::Workforce",   1)
    t.resource_count_is("AWS::Lambda::Function",       2)
```

---

## 7. References

- `docs/template_params.md` — `LABELING_BUCKET_SSM`, `LABELER_USER_POOL_ID_SSM`, `WORKTEAM_ARN`, `TRAINING_PIPELINE_NAME_SSM`, `SAGEMAKER_ROLE_ARN_SSM`, `NUM_LABELERS_PER_OBJECT`
- `docs/Feature_Roadmap.md` — feature IDs `ML-24` (labeling), `ML-25` (active learning), `GOV-08` (labeler MFA)
- Ground Truth `CreateLabelingJob`: https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_CreateLabelingJob.html
- Task types + ARNs: https://docs.aws.amazon.com/sagemaker/latest/dg/sms-task-types.html
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (pipeline trigger target), `AGENTCORE_IDENTITY` (Cognito pattern), `LAYER_SECURITY` (KMS), `COMPLIANCE_HIPAA_PCIDSS` (labeler audit requirements), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `GroundTruthStack` owns its own CMK + labeler Cognito pool; reads SageMaker role ARN and training pipeline name via SSM; identity-side grants (`sagemaker:CreateLabelingJob` with name prefix, `iam:PassRole` with `iam:PassedToService` Condition, `sagemaker:StartPipelineExecution` on named pipeline). Extracted handlers to `lambda/labeling_trigger` and `lambda/labeling_complete`. Added Swap matrix (§5), Worked example (§6), Gotchas on algorithm-spec ARNs, workforce singletonness, and Cognito separation. |
| 1.0 | 2026-03-05 | Initial — labeling bucket, private workforce, trigger + complete Lambdas inline. |
