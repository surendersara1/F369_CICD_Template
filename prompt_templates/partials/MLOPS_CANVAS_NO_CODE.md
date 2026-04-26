# SOP — SageMaker Canvas (no-code ML for citizen data scientists · AutoML · JumpStart UI · forecast/classify/regress)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Canvas (GA, ongoing 2024-2026 enhancements) · AutoML behind the scenes (formerly Autopilot) · JumpStart UI · Time-series forecasting · Tabular classification / regression · Foundation models for text generation · Generative AI Q&A on uploaded data · Model deployment to SageMaker endpoint

---

## 1. Purpose

- Codify the **Canvas pattern** — the no-code UI for business analysts, citizen data scientists, and domain experts to build ML models without writing code.
- Codify the **enable-Canvas in Studio domain** + per-user opt-in pattern.
- Cover the **integration paths**: Canvas → Model Registry → DeployToEndpoint → MLOps team picks up.
- Cover the **data sources** Canvas connects to: S3, Redshift, Snowflake, Databricks, Athena, Aurora, RDS, etc.
- Cover the **time-series forecasting**, **tabular ML**, **JumpStart foundation model fine-tune**, and **GenAI Q&A** flows.
- This is the **no-code ML specialisation**. `MLOPS_SAGEMAKER_TRAINING` covers code-driven workflows; this partial enables business users to self-serve.

When the SOW signals: "business users want to build models", "no-code ML", "citizen data scientists", "Canvas", "we have analysts who know SQL but not Python".

---

## 2. Decision tree — Canvas vs Code

```
User profile?
├── Business analyst, SQL+Excel → §3 Canvas
├── Data scientist, Python → MLOPS_SAGEMAKER_TRAINING
├── Junior analyst doing ad-hoc analysis → §3 Canvas
├── Production ML engineering → MLOPS_SAGEMAKER_TRAINING + Pipelines
└── Mix (Canvas for prototype → handoff to ML team for prod) → §3 + §4 handoff

Use case?
├── Tabular classification (will customer churn?) → Canvas standard
├── Tabular regression (sales forecast) → Canvas standard
├── Time-series forecasting → Canvas time-series
├── Image classification → Canvas (built-in)
├── Text classification → Canvas (built-in)
├── Custom GenAI Q&A on uploaded docs → Canvas Generative AI
├── Custom NLP / multimodal → MLOPS_SAGEMAKER_TRAINING (Canvas can't customize architecture)
└── Reinforcement learning → MLOPS_SAGEMAKER_TRAINING (not in Canvas)
```

---

## 3. Enable Canvas + per-user opt-in

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  SageMaker Studio Domain (Classic or Unified)                    │
   │     - DomainSettings.CanvasAppSettings.EnableCanvas = True        │
   │     - Per-user execution role with Canvas-required perms          │
   │     - VPC-only network access                                      │
   └──────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  User Profile: maria@example.com                                 │
   │     - CanvasAppSettings.WorkspaceSettings (storage, IAM)          │
   │     - User opens Canvas via Studio launcher                        │
   └──────────────────────────────────────────────────────────────────┘
                      │
                      ▼
   Canvas UI → user uploads CSV / connects Redshift → builds model →
   model auto-registered to Model Registry → deployed to endpoint OR
   exported as artifact for MLOps team
```

### 3.2 CDK — `_enable_canvas_in_domain()`

```python
from aws_cdk import (
    aws_iam as iam,
    aws_sagemaker as sagemaker,
)


def _enable_canvas_in_domain(self, stage: str) -> None:
    """Enable Canvas at domain level + configure required IAM."""

    # A) Domain-level Canvas settings
    self.studio_domain.add_property_override(
        "DomainSettings.CanvasAppSettings",
        {
            "TimeSeriesForecastingSettings": {
                "Status":            "ENABLED",
                "AmazonForecastRoleArn": self.canvas_forecast_role.role_arn,
            },
            "ModelRegisterSettings": {
                "Status":               "ENABLED",
                "CrossAccountModelRegisterRoleArn": self.canvas_register_role.role_arn,
            },
            "DirectDeploySettings": {
                "Status": "ENABLED",                          # users can DeployToEndpoint
            },
            "GenerativeAiSettings": {
                "AmazonBedrockRoleArn": self.bedrock_invoke_role.role_arn,
            },
            "WorkspaceSettings": {
                "S3ArtifactPath":   f"s3://{{project_name}}-canvas-{stage}/",
                "S3KmsKeyId":       self.kms_key.key_arn,
            },
            "IdentityProviderOAuthSettings": [],            # for Snowflake/Salesforce SSO if used
            "KendraSettings": {
                "Status": "DISABLED",                         # enable for RAG-with-Canvas
            },
        },
    )

    # B) Canvas execution role per user
    self.canvas_user_role = iam.Role(self, "CanvasUserRole",
        role_name=f"{{project_name}}-canvas-user-{stage}",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerCanvasFullAccess"),
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerCanvasAIServicesAccess"),
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerCanvasDataPrepFullAccess"),
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerCanvasForecastAccess"),
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerCanvasBedrockAccess"),       # for Generative AI features
        ],
        permissions_boundary=self.permission_boundary,
    )
    self.canvas_data_bucket.grant_read_write(self.canvas_user_role)
    self.kms_key.grant_encrypt_decrypt(self.canvas_user_role)

    # C) Forecast role (Canvas calls Forecast service for time-series)
    self.canvas_forecast_role = iam.Role(self, "CanvasForecastRole",
        assumed_by=iam.ServicePrincipal("forecast.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonForecastFullAccess"),
        ],
    )

    # D) Bedrock invoke role (for Generative AI features in Canvas)
    self.bedrock_invoke_role = iam.Role(self, "CanvasBedrockRole",
        assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
    )
    self.bedrock_invoke_role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel"],
        resources=[
            f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-text-*",
            f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-3-*",
        ],
    ))

    # E) Per-user opt-in via UserProfile
    canvas_user_profile = sagemaker.CfnUserProfile(self, "CanvasMariaProfile",
        domain_id=self.studio_domain.attr_domain_id,
        user_profile_name="maria-canvas",
        user_settings=sagemaker.CfnUserProfile.UserSettingsProperty(
            execution_role=self.canvas_user_role.role_arn,
            canvas_app_settings=sagemaker.CfnUserProfile.CanvasAppSettingsProperty(
                workspace_settings=sagemaker.CfnUserProfile.WorkspaceSettingsProperty(
                    s3_artifact_path=f"s3://{{project_name}}-canvas-{stage}/maria/",
                    s3_kms_key_id=self.kms_key.key_arn,
                ),
                model_register_settings=sagemaker.CfnUserProfile.ModelRegisterSettingsProperty(
                    status="ENABLED",
                    cross_account_model_register_role_arn=self.canvas_register_role.role_arn,
                ),
                direct_deploy_settings=sagemaker.CfnUserProfile.DirectDeploySettingsProperty(
                    status="ENABLED",
                ),
            ),
        ),
    )
```

### 3.3 Canvas data source connections

Canvas users connect to data sources via the UI. Behind the scenes:

| Source | Setup |
|---|---|
| S3 | Auto-discovered from user's IAM (drag-drop CSV/Parquet/JSON) |
| Athena | User picks workgroup; results returned as table |
| Redshift | Glue Connection + IAM auth or username/password |
| Snowflake | SSO or static creds via Secrets Manager |
| Databricks | Personal access token in Secrets Manager |
| Aurora / RDS | Glue Connection w/ IAM auth |
| Salesforce | OAuth via AppFlow (see DATA_APPFLOW_SAAS_INGEST) |

Pre-populate connections via custom resource so Canvas users see them on day 1:

```python
# Example: Snowflake connection
snowflake_secret = sm.Secret.from_secret_name_v2(
    self, "SfCred",
    secret_name=f"{{project_name}}-snowflake-{stage}",
)

canvas_snowflake_conn = glue.CfnConnection(self, "CanvasSfConn",
    catalog_id=self.account,
    connection_input=glue.CfnConnection.ConnectionInputProperty(
        name=f"canvas-snowflake-{stage}",
        connection_type="SNOWFLAKE",
        connection_properties={
            "JDBC_CONNECTION_URL": "jdbc:snowflake://account.snowflakecomputing.com/?db=ANALYTICS",
            "USERNAME": "{{resolve:secretsmanager:" + snowflake_secret.secret_arn + ":SecretString:user}}",
            "PASSWORD": "{{resolve:secretsmanager:" + snowflake_secret.secret_arn + ":SecretString:password}}",
        },
    ),
)
```

---

## 4. Canvas → MLOps handoff pattern

When Canvas users build a "good enough" model, MLOps team takes it over:

```
Canvas user builds model → Click "Add to Model Registry" →
   Model Package created in default group →
   MLOps team's EventBridge rule fires (see MLOPS_LLM_FINETUNING_PROD §3.2 F) →
   Auto-deploy to staging endpoint OR copy to production MPG →
   MLOps continues with Pipelines / monitoring / etc.
```

CDK for the handoff Lambda:

```python
canvas_handoff_fn = lambda_.Function(self, "CanvasHandoffFn",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="index.handler",
    code=lambda_.Code.from_asset(str(LAMBDA_SRC / "canvas_handoff")),
    environment={
        "TARGET_MPG": "qra-prod-mpg",
        "STAGING_DEPLOYER_TOPIC": staging_topic.topic_arn,
    },
)
canvas_handoff_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["sagemaker:CreateModelPackage", "sagemaker:DescribeModelPackage"],
    resources=["*"],
))

# Trigger on Canvas-registered package
events.Rule(self, "CanvasPkgRule",
    event_pattern=events.EventPattern(
        source=["aws.sagemaker"],
        detail_type=["SageMaker Model Package State Change"],
        detail={
            "ModelPackageGroupName": [{"prefix": "canvas-"}],   # Canvas naming
            "ModelApprovalStatus":   ["PendingManualApproval"],
        },
    ),
    targets=[targets.LambdaFunction(canvas_handoff_fn)],
)
```

---

## 5. Five non-negotiables

1. **Per-user execution role for Canvas.** Default-shared roles let one user's Canvas read another's S3 prefix. Always per-user.

2. **Canvas KMS encryption mandatory.** `s3_kms_key_id` on workspace settings. Without it, datasets encrypt with default S3-SSE — fails compliance audits.

3. **Disable `DirectDeploySettings` in regulated industries.** Canvas users shouldn't deploy to production endpoints directly. Force handoff via Model Registry + MLOps approval.

4. **Bedrock + Canvas via dedicated invoke role.** Not the user's Canvas role. The Bedrock role's permissions can be more permissive (specific FMs only) without granting users direct Bedrock access.

5. **Storage scope per user with prefix-based bucket policy.** `s3://qra-canvas/maria/` only writable by maria's role. Default `s3://qra-canvas/` shared across users defeats isolation.

---

## 6. References

- AWS docs:
  - [Canvas overview](https://docs.aws.amazon.com/sagemaker/latest/dg/canvas.html)
  - [Canvas IAM setup](https://docs.aws.amazon.com/sagemaker/latest/dg/canvas-set-up.html)
  - [Generative AI in Canvas](https://docs.aws.amazon.com/sagemaker/latest/dg/canvas-fm-chat-fine-tune.html)
  - [Time-series forecasting](https://docs.aws.amazon.com/sagemaker/latest/dg/canvas-time-series.html)
  - [Canvas data connections](https://docs.aws.amazon.com/sagemaker/latest/dg/canvas-import-data.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — code-driven alternative
  - `MLOPS_LLM_FINETUNING_PROD` — JumpStart UI fine-tune (Canvas UI for that)
  - `MLOPS_SAGEMAKER_UNIFIED_STUDIO` — Canvas in Unified Studio context

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Canvas no-code ML enablement at domain + per-user level. CDK for IAM + workspace + Bedrock invoke role + handoff Lambda. Time-series + tabular + GenAI Q&A flows covered. Created Wave 7 (2026-04-26). |
