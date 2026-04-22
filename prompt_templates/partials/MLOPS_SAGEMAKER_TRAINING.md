# SOP — MLOps SageMaker Training Platform

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws_sagemaker` L1 constructs · SageMaker Studio (VPC-only) · Feature Store · Model Registry · MLflow 2.13+ · SageMaker Pipelines (`sagemaker` SDK ≥ 2.220) · Python 3.13 Lambda

---

## 1. Purpose

- Provision the three-domain SageMaker MLOps platform: **Data Science** (free exploration), **Staging** (ML-engineer-reviewed pipelines), **Production** (model-committee-approved models only).
- Codify the six platform components: Studio Domain (VPC-only), Feature Store (online + offline), Experiments, Pipelines trigger Lambda, Model Registry with approval gating, optional MLflow Tracking Server.
- Provide the canonical SageMaker Pipeline Python (`ml/pipelines/training_pipeline.py`): processing → training (spot + checkpoint) → evaluation → conditional register (AccuracyThreshold gate).
- Enforce the EventBridge-driven deployment flow — registering a model with `ModelApprovalStatus=Approved` triggers the deployer Lambda from `MLOPS_SAGEMAKER_SERVING`.
- Include when the SOW mentions ML model training, data-science platform, feature engineering, model registry, MLflow, or experiment tracking.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single CDK stack owns Studio + Feature Store + Registry + Pipelines + Lambda trigger + data-lake buckets / KMS | **§3 Monolith Variant** |
| Separate stacks: `DataLakeStack` owns buckets/KMS, `DataScienceStack` owns Studio, `MLPlatformStack` owns Feature Store + Registry + MLflow, `OrchestrationStack` owns the trigger Lambda + schedule | **§4 Micro-Stack Variant** |

**Why the split matters.** The SageMaker execution role needs `s3:*` on data-lake buckets and `kms:Encrypt/Decrypt` on their KMS key. Monolith: `bucket.grant_read_write(sagemaker_role)` and `key.grant_encrypt_decrypt(sagemaker_role)` work locally. Micro-stack: those L2 helpers edit the bucket policy / key policy in the data-lake stack referencing the role ARN from the ML platform stack → circular export. Identity-side `PolicyStatement` on the SageMaker role + SSM-published bucket names keeps dependencies unidirectional. Feature Store KMS follows the fifth non-negotiable: do not set `encryption_key=ext_key` on an `aws_sagemaker.CfnFeatureGroup` when `ext_key` is in another stack — use the KMS key's ARN string instead.

---

## 3. Monolith Variant

**Use when:** POC / single-stack layout.

### 3.1 Three-domain ML environment strategy

This is **different from software dev/staging/prod**:

| Domain                   | Who uses it     | What happens                                                   | Approval gate                                        |
| ------------------------ | --------------- | -------------------------------------------------------------- | ---------------------------------------------------- |
| **Data Science (DS)**    | Data scientists | Experimentation, EDA, prototype models, any notebook           | None — free exploration                              |
| **Staging (ML Staging)** | ML Engineers    | Validated training pipelines, model evaluation, A/B test setup | ML Engineer reviews                                  |
| **Production (ML Prod)** | Model Ops       | Only approved models serve traffic, monitored 24/7             | Model Committee approval in SageMaker Model Registry |

### 3.2 Architecture

```
Data Scientists          ML Pipeline                    Model Registry
─────────────────       ──────────────                  ───────────────
SageMaker Studio  ───►  Processing Job                  Model Package
  (Jupyter)               (feature eng)           ───►  Version N (pending)
  (Data Wrangler)  ───►  Training Job                   Version N (approved) ───► Endpoint
  (Experiments)           (Spot + checkpoint)    ───►   Version N+1 (staging)
  (MLflow)         ───►  HPO Tuning Job                 Version N+1 (rejected)
                   ───►  Evaluation Step
                   ───►  ConditionStep (AccuracyGate)
                   ───►  Register Step               ───► Model Registry
                                                      ───► EventBridge on "Approved"
                                                      ───► MLOPS_SAGEMAKER_SERVING deployer Lambda
```

### 3.3 CDK — `_create_sagemaker_training` method body

```python
from typing import Dict
from aws_cdk import (
    CfnOutput, Duration,
    aws_sagemaker as sagemaker,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_events as events,
    aws_events_targets as targets,
)


def _create_sagemaker_training(self, stage_name: str) -> None:
    """SageMaker MLOps Training Platform.

    Assumes self.{vpc, lambda_sg, kms_key, db_secret, lake_buckets, lambda_functions}
    were created earlier in THIS stack class.

    Components:
      A) Studio Domain (VPC-only)
      B) Feature Store (online + offline)
      C) Model Registry (MANUAL approval in staging/prod)
      D) Optional MLflow Tracking Server
      E) Pipeline trigger Lambda + schedule
    """

    # -- SageMaker execution role --------------------------------------------
    self.sagemaker_role = iam.Role(
        self, "SageMakerExecutionRole",
        role_name=f"{{project_name}}-sagemaker-execution-{stage_name}",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
        ],
    )
    for bucket in self.lake_buckets.values():
        bucket.grant_read_write(self.sagemaker_role)       # L2 safe in monolith
    self.kms_key.grant_encrypt_decrypt(self.sagemaker_role)
    self.db_secret.grant_read(self.sagemaker_role)

    # -- A) Studio Domain (VPC-only) -----------------------------------------
    self.studio_domain = sagemaker.CfnDomain(
        self, "StudioDomain",
        domain_name=f"{{project_name}}-studio-{stage_name}",
        auth_mode="IAM",            # or "SSO" if using IAM Identity Center
        vpc_id=self.vpc.vpc_id,
        subnet_ids=[s.subnet_id for s in self.vpc.private_subnets],
        app_network_access_type="VpcOnly",   # no direct internet — required for regulated data
        default_user_settings=sagemaker.CfnDomain.UserSettingsProperty(
            execution_role=self.sagemaker_role.role_arn,
            security_groups=[self.lambda_sg.security_group_id],
            kernel_gateway_app_settings=sagemaker.CfnDomain.KernelGatewayAppSettingsProperty(
                default_resource_spec=sagemaker.CfnDomain.ResourceSpecProperty(
                    instance_type="ml.t3.medium" if stage_name == "ds" else "ml.m5.xlarge",
                ),
            ),
            sharing_settings=sagemaker.CfnDomain.SharingSettingsProperty(
                notebook_output_option="Disabled" if stage_name == "prod" else "Allowed",
                s3_output_path=f"s3://{self.lake_buckets['curated'].bucket_name}/notebook-outputs/",
                s3_kms_key_id=self.kms_key.key_arn,
            ),
        ),
        domain_settings=sagemaker.CfnDomain.DomainSettingsProperty(
            security_group_ids=[self.lambda_sg.security_group_id],
        ),
        kms_key_id=self.kms_key.key_arn,
        tags=[
            {"key": "Project",     "value": "{project_name}"},
            {"key": "Environment", "value": stage_name},
            {"key": "Domain",      "value": "DataScience"},
        ],
    )

    # User Profiles per role
    for profile_name in ("data-scientist", "ml-engineer", "ml-ops"):
        sagemaker.CfnUserProfile(
            self, f"StudioUser{profile_name.replace('-', '').title()}",
            domain_id=self.studio_domain.attr_domain_id,
            user_profile_name=profile_name,
            user_settings=sagemaker.CfnDomain.UserSettingsProperty(
                execution_role=self.sagemaker_role.role_arn,
            ),
        )

    # -- B) Feature Store ----------------------------------------------------
    FEATURE_GROUPS = [
        {
            "name":       "user-features",
            "record_id":  "user_id",
            "event_time": "event_time",
            "features":   [
                {"name": "user_id",             "type": "String"},
                {"name": "event_time",          "type": "String"},
                {"name": "total_spend_30d",     "type": "Fractional"},
                {"name": "session_count_7d",    "type": "Integral"},
                {"name": "preferred_category",  "type": "String"},
                {"name": "churn_risk_score",    "type": "Fractional"},
            ],
            "online_enabled":  True,
            "offline_enabled": True,
        },
        # [Claude: add more feature groups per SOW Architecture Map]
    ]
    self.feature_groups: Dict[str, sagemaker.CfnFeatureGroup] = {}
    for fg in FEATURE_GROUPS:
        self.feature_groups[fg["name"]] = sagemaker.CfnFeatureGroup(
            self, f"FeatureGroup{fg['name'].replace('-', '').title()}",
            feature_group_name=f"{{project_name}}-{fg['name']}-{stage_name}",
            record_identifier_feature_name=fg["record_id"],
            event_time_feature_name=fg["event_time"],
            feature_definitions=[
                sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(
                    feature_name=f["name"], feature_type=f["type"],
                ) for f in fg["features"]
            ],
            online_store_config=sagemaker.CfnFeatureGroup.OnlineStoreConfigProperty(
                enable_online_store=fg["online_enabled"],
                security_config=sagemaker.CfnFeatureGroup.OnlineStoreSecurityConfigProperty(
                    kms_key_id=self.kms_key.key_arn,
                ),
            ) if fg["online_enabled"] else None,
            offline_store_config=sagemaker.CfnFeatureGroup.OfflineStoreConfigProperty(
                s3_storage_config=sagemaker.CfnFeatureGroup.S3StorageConfigProperty(
                    s3_uri=f"s3://{self.lake_buckets['features'].bucket_name}/feature-store/{fg['name']}/",
                    kms_key_id=self.kms_key.key_arn,
                ),
                disable_glue_table_creation=False,
                data_catalog_config=sagemaker.CfnFeatureGroup.DataCatalogConfigProperty(
                    catalog=self.account,
                    database=f"{{project_name}}_{stage_name}_catalog",
                    table_name=fg["name"].replace("-", "_"),
                ),
            ) if fg["offline_enabled"] else None,
            role_arn=self.sagemaker_role.role_arn,
        )

    # -- C) Model Registry ---------------------------------------------------
    ML_MODEL_GROUPS = [
        {"name": "churn-prediction", "description": "Customer churn risk scoring"},
        {"name": "recommendation",   "description": "Product recommendation"},
        # [Claude: add one group per ML use case from Architecture Map]
    ]
    self.model_package_groups: Dict[str, sagemaker.CfnModelPackageGroup] = {}
    for g in ML_MODEL_GROUPS:
        self.model_package_groups[g["name"]] = sagemaker.CfnModelPackageGroup(
            self, f"ModelGroup{g['name'].replace('-', '').title()}",
            model_package_group_name=f"{{project_name}}-{g['name']}-{stage_name}",
            model_package_group_description=g["description"],
        )

    # EventBridge: trigger deployment Lambda (from MLOPS_SAGEMAKER_SERVING) on approval
    events.Rule(
        self, "ModelApprovedRule",
        rule_name=f"{{project_name}}-model-approved-{stage_name}",
        event_pattern=events.EventPattern(
            source=["aws.sagemaker"],
            detail_type=["SageMaker Model Package State Change"],
            detail={
                "ModelApprovalStatus": ["Approved"],
                "ModelPackageGroupName": [
                    f"{{project_name}}-{g['name']}-{stage_name}" for g in ML_MODEL_GROUPS
                ],
            },
        ),
        targets=[targets.LambdaFunction(
            self.lambda_functions["ModelDeployer"],
            retry_attempts=2,
        )],
    )

    # -- D) MLflow Tracking Server (optional) --------------------------------
    # [Include only when SOW says "MLflow" or "open-source experiment tracking"]
    sagemaker.CfnMlflowTrackingServer(
        self, "MLflowServer",
        tracking_server_name=f"{{project_name}}-mlflow-{stage_name}",
        artifact_store_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/mlflow-artifacts/",
        role_arn=self.sagemaker_role.role_arn,
        tracking_server_size="Small" if stage_name != "prod" else "Medium",
        mlflow_version="2.13.0",
        automatic_model_registration=True,
    )

    # -- E) Pipeline trigger Lambda + schedule -------------------------------
    pipeline_trigger_fn = _lambda.Function(
        self, "MLPipelineTrigger",
        function_name=f"{{project_name}}-ml-pipeline-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/ml_pipeline_trigger"),
        timeout=Duration.seconds(30),
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "PIPELINE_NAME": f"{{project_name}}-training-pipeline-{stage_name}",
            "PROCESSING_INSTANCE": "ml.m5.xlarge" if stage_name != "prod" else "ml.m5.4xlarge",
            "TRAINING_INSTANCE":   "ml.m5.2xlarge" if stage_name != "prod" else "ml.p3.2xlarge",
        },
    )
    pipeline_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution", "sagemaker:DescribePipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}*"],
    ))

    events.Rule(
        self, "MLRetrainingSchedule",
        rule_name=f"{{project_name}}-ml-retrain-schedule-{stage_name}",
        schedule=events.Schedule.cron(
            hour="2", minute="0",
            week_day="MON" if stage_name == "prod" else "*",
        ),
        enabled=stage_name != "ds",
        targets=[targets.LambdaFunction(pipeline_trigger_fn)],
    )

    CfnOutput(self, "StudioDomainId", value=self.studio_domain.attr_domain_id)
    CfnOutput(self, "MLPipelineTriggerArn", value=pipeline_trigger_fn.function_arn)
```

### 3.4 Pipeline code — `ml/pipelines/training_pipeline.py`

Not CDK — SageMaker SDK. The trigger Lambda above invokes it.

```python
"""SageMaker Training Pipeline — processing → training → eval → conditional register."""
from sagemaker.workflow.pipeline    import Pipeline
from sagemaker.workflow.steps       import ProcessingStep, TrainingStep
from sagemaker.workflow.model_step  import ModelStep
from sagemaker.workflow.parameters  import ParameterString, ParameterInteger
from sagemaker.workflow.conditions  import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.sklearn.processing   import SKLearnProcessor
from sagemaker.estimator            import Estimator


def create_pipeline(sagemaker_session, role_arn, pipeline_name, model_package_group_name):
    processing_instance = ParameterString("ProcessingInstanceType", default_value="ml.m5.xlarge")
    training_instance   = ParameterString("TrainingInstanceType",   default_value="ml.m5.2xlarge")
    approval_status     = ParameterString("ModelApprovalStatus",    default_value="PendingManualApproval")
    accuracy_threshold  = ParameterInteger("AccuracyThreshold",     default_value=80)

    # Step 1 — processing + feature eng
    processor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type=processing_instance, instance_count=1,
    )
    process_step = ProcessingStep(
        name="FeatureEngineering",
        processor=processor,
        code="ml/scripts/feature_engineering.py",
        inputs=[], outputs=[],
    )

    # Step 2 — training (Spot + checkpoints)
    estimator = Estimator(
        image_uri="...",
        role=role_arn,
        instance_type=training_instance,
        instance_count=1,
        use_spot_instances=True,
        max_wait=7200, max_run=3600,
        checkpoint_s3_uri="s3://{project_name}-features/checkpoints/",
        hyperparameters={"n-estimators": 200, "max-depth": 6},
        sagemaker_session=sagemaker_session,
    )
    train_step = TrainingStep(
        name="ModelTraining", estimator=estimator,
        inputs={"train": process_step.properties.ProcessingOutputConfig},
    )

    # Step 3 — evaluation
    eval_step = ProcessingStep(name="ModelEvaluation", processor=processor, code="ml/scripts/evaluate.py")

    # Step 4 — conditional register
    accuracy_condition = ConditionGreaterThanOrEqualTo(
        left=eval_step.properties.ProcessingOutputConfig,
        right=accuracy_threshold,
    )
    model_step = ModelStep(
        name="RegisterModel",
        model_approval_status=approval_status,
        model_package_group_name=model_package_group_name,
    )
    condition_step = ConditionStep(
        name="AccuracyCheck",
        conditions=[accuracy_condition],
        if_steps=[model_step],
        else_steps=[],
    )

    return Pipeline(
        name=pipeline_name,
        parameters=[processing_instance, training_instance, approval_status, accuracy_threshold],
        steps=[process_step, train_step, eval_step, condition_step],
        sagemaker_session=sagemaker_session,
    )
```

### 3.5 Monolith gotchas

- **Studio Domain DELETE is destructive** — `CfnDomain` removal deletes user profiles, notebooks, and any unsaved work. Set `removal_policy=RETAIN` in prod.
- **`app_network_access_type="VpcOnly"`** requires VPC endpoints for SageMaker API, Runtime, Studio, S3. Validate `LAYER_NETWORKING §3/§4` covers them.
- **Feature Store online store charges per GB-hour.** Enable only for features that truly need real-time retrieval.
- **MLflow Tracking Server `Small`** is ~$120/month and backs a 2-vCPU / 4-GB EC2. Start there; scale to `Medium` only on contention.
- **`ModelApprovalStatus=Approved` event is one-way** — rejecting later does NOT trigger an "unapprove" event. The deployer Lambda must idempotently handle re-invocation of the same model version.
- **Spot instances** — `max_wait ≥ max_run` is a correctness constraint: exceed it and the pipeline step fails immediately.

---

## 4. Micro-Stack Variant

**Use when:** production layout — data lake in `DataLakeStack`, Studio in `DataScienceStack`, Feature Store + Registry + MLflow in `MLPlatformStack`, pipeline trigger in `OrchestrationStack`.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `bucket.grant_read_write(sagemaker_role)`** across stacks — use identity-side `PolicyStatement` on the SageMaker role, granting `s3:*` on the bucket ARN read from SSM.
3. **Never target a cross-stack queue** with `targets.SqsQueue` for pipeline events; use `CfnRule` with static-ARN Lambda target.
4. **Never split a bucket + OAC** — not relevant here.
5. **Never set `encryption_key=ext_key`** on Feature Store / Studio when the key is from another stack. Pass the KMS ARN string (from SSM) into `kms_key_id=` rather than a cross-stack construct.

### 4.2 `MLPlatformStack` — Feature Store + Registry + MLflow

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration,
    aws_iam as iam,
    aws_sagemaker as sagemaker,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


def _s3_grant(role: iam.IRole, bucket_arn: str, actions: list[str]) -> None:
    """Identity-side S3 grant — safe for cross-stack bucket."""
    obj_actions    = [a for a in actions if a != "s3:ListBucket"]
    bucket_actions = [a for a in actions if a == "s3:ListBucket"]
    if obj_actions:
        role.add_to_policy(iam.PolicyStatement(actions=obj_actions, resources=[f"{bucket_arn}/*"]))
    if bucket_actions:
        role.add_to_policy(iam.PolicyStatement(actions=bucket_actions, resources=[bucket_arn]))


def _kms_grant(role: iam.IRole, key_arn: str, actions: list[str]) -> None:
    role.add_to_policy(iam.PolicyStatement(actions=actions, resources=[key_arn]))


class MLPlatformStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        lake_bucket_ssm_names: dict[str, str],     # {"curated": "/proj/lake/curated_bucket", ...}
        lake_key_arn_ssm_name: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-ml-platform-{stage_name}", **kwargs)

        # Resolve cross-stack data-lake ARNs via SSM (deploy-time)
        curated_bucket = ssm.StringParameter.value_for_string_parameter(
            self, lake_bucket_ssm_names["curated"])
        features_bucket = ssm.StringParameter.value_for_string_parameter(
            self, lake_bucket_ssm_names["features"])
        lake_key_arn = ssm.StringParameter.value_for_string_parameter(
            self, lake_key_arn_ssm_name)

        # SageMaker execution role — identity-side grants only
        self.sagemaker_role = iam.Role(
            self, "SageMakerExecutionRole",
            role_name=f"{{project_name}}-sagemaker-{stage_name}",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
            ],
        )
        _s3_grant(self.sagemaker_role,
                  f"arn:aws:s3:::{curated_bucket}",  ["s3:*"])
        _s3_grant(self.sagemaker_role,
                  f"arn:aws:s3:::{features_bucket}", ["s3:*"])
        _kms_grant(self.sagemaker_role, lake_key_arn,
                   ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"])
        iam.PermissionsBoundary.of(self.sagemaker_role).apply(permission_boundary)

        # Feature Store — pass KMS ARN as STRING (fifth non-negotiable)
        self.feature_group = sagemaker.CfnFeatureGroup(
            self, "UserFeatures",
            feature_group_name=f"{{project_name}}-user-features-{stage_name}",
            record_identifier_feature_name="user_id",
            event_time_feature_name="event_time",
            feature_definitions=[
                sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(feature_name="user_id",             feature_type="String"),
                sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(feature_name="event_time",          feature_type="String"),
                sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(feature_name="total_spend_30d",     feature_type="Fractional"),
                sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(feature_name="churn_risk_score",    feature_type="Fractional"),
            ],
            online_store_config=sagemaker.CfnFeatureGroup.OnlineStoreConfigProperty(
                enable_online_store=True,
                security_config=sagemaker.CfnFeatureGroup.OnlineStoreSecurityConfigProperty(
                    kms_key_id=lake_key_arn,             # STRING, not construct ref
                ),
            ),
            offline_store_config=sagemaker.CfnFeatureGroup.OfflineStoreConfigProperty(
                s3_storage_config=sagemaker.CfnFeatureGroup.S3StorageConfigProperty(
                    s3_uri=f"s3://{features_bucket}/feature-store/user-features/",
                    kms_key_id=lake_key_arn,
                ),
                disable_glue_table_creation=False,
            ),
            role_arn=self.sagemaker_role.role_arn,
        )

        # Model Package Group
        self.model_group = sagemaker.CfnModelPackageGroup(
            self, "ChurnGroup",
            model_package_group_name=f"{{project_name}}-churn-prediction-{stage_name}",
            model_package_group_description="Customer churn risk scoring",
        )
        ssm.StringParameter(
            self, "ModelGroupNameParam",
            parameter_name=f"/{{project_name}}/ml/model_group/churn_prediction",
            string_value=self.model_group.model_package_group_name,
        )

        # MLflow Tracking Server (optional)
        self.mlflow_server = sagemaker.CfnMlflowTrackingServer(
            self, "MLflowServer",
            tracking_server_name=f"{{project_name}}-mlflow-{stage_name}",
            artifact_store_uri=f"s3://{curated_bucket}/mlflow-artifacts/",
            role_arn=self.sagemaker_role.role_arn,
            tracking_server_size="Small" if stage_name != "prod" else "Medium",
            mlflow_version="2.13.0",
            automatic_model_registration=True,
        )

        # Publish the SageMaker role ARN for downstream consumers
        ssm.StringParameter(
            self, "SageMakerRoleArnParam",
            parameter_name=f"/{{project_name}}/ml/sagemaker_role_arn",
            string_value=self.sagemaker_role.role_arn,
        )
```

### 4.3 `OrchestrationStack` — trigger Lambda + EventBridge rules

```python
from pathlib import Path
from aws_cdk import (
    Aws, Duration,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_logs as logs,
    aws_events as events,
    aws_events_targets as targets,
    aws_ssm as ssm,
)
from constructs import Construct
import aws_cdk as cdk

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class OrchestrationStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        model_deployer_fn_name: str,         # read identity-side via name
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-ml-orchestration-{stage_name}", **kwargs)

        log_group = logs.LogGroup(
            self, "TriggerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-ml-pipeline-trigger-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        trigger_fn = _lambda.Function(
            self, "MLPipelineTriggerFn",
            function_name=f"{{project_name}}-ml-pipeline-trigger-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "ml_pipeline_trigger")),
            timeout=Duration.seconds(30),
            log_group=log_group,
            environment={
                "PIPELINE_NAME": f"{{project_name}}-training-pipeline-{stage_name}",
            },
        )
        trigger_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:StartPipelineExecution", "sagemaker:DescribePipelineExecution"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:pipeline/{{project_name}}*"],
        ))
        iam.PermissionsBoundary.of(trigger_fn.role).apply(permission_boundary)

        events.Rule(
            self, "MLRetrainingSchedule",
            rule_name=f"{{project_name}}-ml-retrain-schedule-{stage_name}",
            schedule=events.Schedule.cron(hour="2", minute="0",
                week_day="MON" if stage_name == "prod" else "*"),
            enabled=stage_name != "ds",
            targets=[targets.LambdaFunction(trigger_fn)],
        )

        # Model approval → deployer Lambda, via L1 CfnRule (cross-stack safe)
        events.CfnRule(
            self, "ModelApprovedRule",
            name=f"{{project_name}}-model-approved-{stage_name}",
            event_pattern={
                "source":      ["aws.sagemaker"],
                "detail-type": ["SageMaker Model Package State Change"],
                "detail":      {"ModelApprovalStatus": ["Approved"]},
            },
            targets=[{
                "arn": f"arn:aws:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:function:{model_deployer_fn_name}",
                "id":  "ModelDeployerTarget",
            }],
        )
```

### 4.4 Micro-stack gotchas

- **KMS key ARN as string** in `kms_key_id=` is what keeps the fifth non-negotiable honoured. `CfnFeatureGroup` accepts ARN strings; don't reach for a cross-stack `kms.Key.from_key_arn(...)` construct.
- **`CfnRule` (L1) instead of `events.Rule`** for cross-stack Lambda targets — `events.Rule` + `LambdaFunction` target adds a Lambda resource policy by name reference only if both are in the same stack. L1 plus a one-time `AddPermission` on the target Lambda from its owning stack is the safe pattern (see `EVENT_DRIVEN_PATTERNS §4`).
- **Studio Domain in its own stack** — deploy once; do not include in every release pipeline. Its lifecycle is "create once per environment".
- **Model-group name collisions** — `model_package_group_name` is account-wide unique. Include `{stage_name}` in the name.
- **MLflow server cold start** — ~5 minutes on first deploy; retries on create are idempotent.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack, one pipeline | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack |
| Separate data-lake team | §4 — data-lake stack publishes bucket ARNs via SSM |
| On-prem training + cloud serving | Keep §3 for serving only; training moves to separate non-CDK infra |
| `cdk synth` cyclic reference | Switch to §4 + identity-side + SSM-published ARNs |
| Very large feature catalog | Split Feature Store into a dedicated stack; tune online-store enablement per group |
| Stop MLflow | Remove `CfnMlflowTrackingServer`; MLflow is optional |

---

## 6. Worked example — MLPlatformStack synthesizes

Save as `tests/sop/test_MLOPS_SAGEMAKER_TRAINING.py`. Offline.

```python
"""SOP verification — MLPlatformStack synthesizes with identity-side grants only."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ml_platform_stack():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ml_platform import MLPlatformStack
    stack = MLPlatformStack(
        app,
        stage_name="staging",
        lake_bucket_ssm_names={
            "curated":  "/test/lake/curated_bucket",
            "features": "/test/lake/features_bucket",
        },
        lake_key_arn_ssm_name="/test/lake/kms_key_arn",
        permission_boundary=boundary,
        env=env,
    )

    template = Template.from_stack(stack)
    template.resource_count_is("AWS::SageMaker::FeatureGroup",          1)
    template.resource_count_is("AWS::SageMaker::ModelPackageGroup",     1)
    template.resource_count_is("AWS::SageMaker::MlflowTrackingServer",  1)
    template.resource_count_is("AWS::SSM::Parameter",                   2)  # model group + role ARN
```

---

## 7. References

- `docs/template_params.md` — `ML_STAGE_NAME`, `SAGEMAKER_ROLE_ARN_SSM`, `MODEL_GROUP_SSM_PREFIX`, `LAKE_BUCKET_SSM_*`, `LAKE_KMS_KEY_ARN_SSM`, `MLFLOW_VERSION`, `MLFLOW_SIZE`
- `docs/Feature_Roadmap.md` — feature IDs `ML-01..ML-08` (training platform), `ML-20` (model registry), `ML-21` (MLflow)
- SageMaker Studio VPC-only: https://docs.aws.amazon.com/sagemaker/latest/dg/studio-notebooks-and-internet-access.html
- SageMaker Pipelines: https://docs.aws.amazon.com/sagemaker/latest/dg/pipelines.html
- Model Registry: https://docs.aws.amazon.com/sagemaker/latest/dg/model-registry.html
- Related SOPs: `LAYER_DATA` (lake buckets + KMS), `LAYER_SECURITY` (execution role permission boundary), `MLOPS_SAGEMAKER_SERVING` (deployer Lambda receives the approval event), `MLOPS_CLARIFY_EXPLAINABILITY` (evaluation step integration), `MLOPS_DATA_PLATFORM` (upstream feature engineering jobs), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MLPlatformStack uses identity-side S3/KMS grants on the SageMaker role with lake bucket names + key ARN read from SSM; `CfnFeatureGroup.kms_key_id=` takes the ARN string (fifth non-negotiable). OrchestrationStack uses `CfnRule` (L1) for cross-stack EventBridge → Lambda target. Kept Three-Domain strategy table. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — Studio Domain, Feature Store, Model Registry, MLflow, pipeline trigger Lambda, pipeline code template. |
