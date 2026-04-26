# SOP — SageMaker Unified Studio (DataZone-integrated workspace · MLflow Apps · Bedrock · S3 Tables · Trusted Identity Propagation)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Unified Studio (GA 2024, ongoing 2025-2026 enhancements) · DataZone domain + projects · Glue Catalog data sources · Athena + Redshift connections · S3 Tables in IAM mode · MLflow Apps (managed tracking servers) · EMR Serverless integration · Bedrock model integration · Trusted Identity Propagation (TIP) for SSO context

---

## 1. Purpose

- Codify the **SageMaker Unified Studio pattern** — the 2024+ replacement for classic SageMaker Studio that integrates DataZone (data mesh) + Glue (catalog) + EMR Serverless (Spark) + Athena (SQL) + Redshift (DW) + MLflow (experiments) + Bedrock (FMs) into one workspace.
- Distinguish **Unified Studio vs classic Studio** (different domain types, different IAM model, different feature set).
- Codify the **DataZone domain + project + environment** structure that Unified Studio sits inside.
- Cover the **MLflow Apps** pattern (per-project managed MLflow tracking server, replaces classic MLflow Tracking Servers).
- Cover the **Trusted Identity Propagation** (TIP) — SSO context propagates through Glue → Athena → Redshift → S3 Tables for end-user-attributed queries.
- Cover the **Bedrock integration** — invoke models directly from Studio notebooks with project IAM.
- This is the **modern Studio specialisation**. `MLOPS_SAGEMAKER_TRAINING` covers classic Studio domain creation; this partial covers Unified Studio domain + projects + environments.

When the SOW signals: "modern data mesh", "team needs Unified Studio", "DataZone integration", "MLflow Apps", "we want one workspace for SQL + Spark + ML + Bedrock", "users authenticate via SSO not IAM".

---

## 2. Decision tree — Unified Studio vs Classic Studio vs Notebook Instances

```
Engagement style?
├── Self-service data + ML for 10+ users → §3 Unified Studio (this partial)
├── ML team only, simple JupyterLab → MLOPS_SAGEMAKER_TRAINING §classic Studio
├── 1-3 ML engineers, just need a notebook → SageMaker Notebook Instance (legacy, NOT recommended)
└── Want browser-only, free tier → Studio Lab (deprecated 2024)

Identity model?
├── IAM Identity Center (SSO + MFA) → §3 Unified Studio (TIP-aware)
├── IAM federation (SAML) → §3 Unified Studio (with custom blueprint)
└── IAM users (no SSO) → MLOPS_SAGEMAKER_TRAINING §classic Studio (IAM mode)

Data scope?
├── Multi-domain mesh (finance + HR + ops) → §3 Unified Studio (DataZone domains)
├── Single domain, simple Glue catalog → §3 Unified Studio (single domain)
└── Tightly-scoped to one team → classic Studio (lighter weight)

Studio features needed?
├── MLflow Apps → §3 Unified Studio (Apps replace classic Tracking Servers)
├── EMR Serverless from notebooks → §3 Unified Studio
├── Athena SQL alongside Spark → §3 Unified Studio
├── Bedrock chat from notebook → §3 Unified Studio
├── JumpStart UI → both Studios support; prefer Unified
└── Canvas UI → both; prefer Unified for new builds
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — DataZone domain + Unified Studio domain + 1 project all in one stack | **§3 Monolith Variant** |
| `IdentityStack` owns IAM Identity Center + KMS; `DomainStack` owns DataZone + Studio; `ProjectsStack` owns per-team projects | **§5 Micro-Stack Variant** |

---

## 3. Monolith Variant — Unified Studio with DataZone domain + 2 projects

### 3.1 Architecture

```
   ┌────────────────────────────────────────────────────────────────────┐
   │  IAM Identity Center (account-level SSO, set up out-of-band)        │
   └──────────────────┬─────────────────────────────────────────────────┘
                      │  TIP — propagates user identity downstream
                      ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  DataZone Domain: acme-data-domain                                 │
   │     - Domain execution role (IAM)                                   │
   │     - KMS CMK for domain                                            │
   │     - Single sign-on identity (IAM Identity Center)                 │
   └──────────────────┬─────────────────────────────────────────────────┘
                      │
                      ├──── Project: finance-team ─────┐
                      │     - Members: SSO users via group                                              │
                      │     - Environments: 1 dev + 1 prod                                              │
                      │     - Data sources: Glue Catalog (finance_*), Athena, Redshift cluster          │
                      │     - MLflow App: finance-mlflow-prod                                           │
                      │
                      └──── Project: ml-platform-team ─┐
                            - Members: ML engineers                                                     │
                            - Environments: 1 dev + 1 prod                                              │
                            - Data sources: Glue Catalog (curated_*), S3 Tables (lakehouse)            │
                            - MLflow App: ml-platform-mlflow-prod                                       │
                            - EMR Serverless app: spark-cluster                                         │
                            - Bedrock model access: claude-3-7-sonnet                                   │
                            - SageMaker AI domain: associated for training/serving                      │
   ┌────────────────────────────────────────────────────────────────────┐
   │  SageMaker Unified Studio Domain (associated with DataZone)         │
   │     - Single Studio URL: studio.{region}.aws                         │
   │     - User picks project on landing → project-scoped IAM role         │
   │     - Notebooks, SQL, Visual Workflows, Data Agent, MLflow UI         │
   └────────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_unified_studio_domain()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_datazone as datazone,           # L1 + some L2
    aws_sagemaker as sagemaker,
    aws_ec2 as ec2,
)


def _create_unified_studio_domain(self, stage: str) -> None:
    """Monolith. DataZone domain + Unified Studio domain + 2 projects."""

    # A) Domain execution role
    self.dz_exec_role = iam.Role(self, "DzExecRole",
        assumed_by=iam.ServicePrincipal("datazone.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonDataZoneFullAccess"),
        ],
        permissions_boundary=self.permission_boundary,
    )

    # B) DataZone domain
    self.dz_domain = datazone.CfnDomain(self, "DzDomain",
        name=f"{{project_name}}-domain-{stage}",
        description=f"DataZone domain for {{project_name}} {stage}",
        domain_execution_role=self.dz_exec_role.role_arn,
        kms_key_identifier=self.kms_key.key_arn,
        single_sign_on=datazone.CfnDomain.SingleSignOnProperty(
            type="IAM_IDC",                                  # IAM Identity Center
            user_assignment="AUTOMATIC",
        ),
        domain_version="V2",                                  # 2024+ Unified Studio compatible
    )

    # C) Default blueprint enable (required for Studio + Athena + Glue)
    # NOTE: Blueprint enablement is via custom resource since CFN coverage is partial
    bp_enable_cr = cr.AwsCustomResource(self, "BlueprintEnable",
        on_create=cr.AwsSdkCall(
            service="DataZone",
            action="enableEnvironmentConfiguration",
            parameters={
                "domainIdentifier": self.dz_domain.attr_id,
                "environmentBlueprintIdentifier": "DefaultDataLake",  # built-in
            },
            physical_resource_id=cr.PhysicalResourceId.of("BlueprintEnable"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
            resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE,
        ),
    )
    bp_enable_cr.node.add_dependency(self.dz_domain)

    # D) Project: finance-team
    finance_project = datazone.CfnProject(self, "FinanceProject",
        domain_identifier=self.dz_domain.attr_id,
        name="finance-team",
        description="Finance analytics + ML",
        glossary_terms=[],
    )
    finance_project.node.add_dependency(bp_enable_cr)

    # E) Project: ml-platform-team
    ml_project = datazone.CfnProject(self, "MlProject",
        domain_identifier=self.dz_domain.attr_id,
        name="ml-platform-team",
        description="ML platform team",
    )
    ml_project.node.add_dependency(bp_enable_cr)

    # F) Studio domain — associates DataZone domain with SageMaker AI
    # NOTE: Unified Studio uses a separate SageMaker domain creation flow vs classic
    # As of 2026-04, the recommended path is the DataZone "DataLake" environment
    # blueprint which auto-provisions a Studio domain per project.
    # CDK coverage is partial; many setups use a mix of CFN + custom resource.

    self.studio_domain = sagemaker.CfnDomain(self, "StudioDomain",
        domain_name=f"{{project_name}}-studio-{stage}",
        auth_mode="SSO",                                      # IAM Identity Center
        default_user_settings=sagemaker.CfnDomain.UserSettingsProperty(
            execution_role=self._user_default_role.role_arn,
            sharing_settings=sagemaker.CfnDomain.SharingSettingsProperty(
                notebook_output_option="Allowed",
                s3_output_path=f"s3://{self.studio_artifacts.bucket_name}/notebooks/",
            ),
            studio_web_portal="ENABLED",
            default_landing_uri="studio::",                    # Unified Studio
        ),
        subnet_ids=[s.subnet_id for s in self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
        vpc_id=self.vpc.vpc_id,
        app_network_access_type="VpcOnly",                    # security: VPC-only
        kms_key_id=self.kms_key.key_arn,
        # ───── UNIFIED STUDIO ASSOCIATION ─────
        # The integration happens via DataZone project's environment, not directly here.
    )

    CfnOutput(self, "DataZoneDomainId", value=self.dz_domain.attr_id)
    CfnOutput(self, "FinanceProjectId", value=finance_project.attr_id)
    CfnOutput(self, "MlProjectId",      value=ml_project.attr_id)
    CfnOutput(self, "StudioPortalUrl",  value=f"https://{self.dz_domain.attr_portal_url}")
```

### 3.3 Project environment — wires up data sources + compute

Each DataZone project needs an "environment" — the actual compute + data resources. CFN coverage is partial; most setups bootstrap via DataZone API after deploy:

```python
"""scripts/bootstrap_project_env.py — runs once after CDK deploy."""
import boto3

dz = boto3.client("datazone")

def create_finance_dev_env(domain_id, project_id):
    """Creates a DataLake environment with Glue + Athena + Redshift access."""
    response = dz.create_environment(
        domainIdentifier=domain_id,
        projectIdentifier=project_id,
        environmentProfileIdentifier="DefaultDataLakeProfile",      # built-in
        name="finance-dev",
        description="Finance dev environment",
        userParameters=[
            # Configure Athena workgroup
            {"name": "consumerGlueDbName", "value": "finance_consumer"},
            # Configure Redshift access
            {"name": "redshiftDbName",     "value": "finance"},
        ],
    )
    return response["id"]
```

### 3.4 MLflow Apps — per-project managed MLflow

Unified Studio's MLflow Apps replace classic Tracking Servers. Each project gets its own MLflow app:

```python
# In ml-platform-team project, after environment creation, add MLflow app
# via DataZone project's "blueprint" or "applications" feature

# MLflow App enables:
# - Tracking server in project IAM scope (project members only)
# - Auto-registers models to SageMaker Model Registry
# - Cross-account sharing of experiments
# - GenAI tracing for Bedrock model evaluations

# In notebook:
import mlflow
mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])         # auto-set in Unified Studio
with mlflow.start_run(run_name="lora-llama-3-70b-v1"):
    mlflow.log_params({"lora_rank": 16, "learning_rate": 2e-5})
    mlflow.log_metric("eval_loss", 0.42)
    mlflow.transformers.log_model(model, "lora-adapter")
```

### 3.5 Data source connections — Glue + Athena + Redshift + S3 Tables

After environment creation, add data sources to the project's catalog:

```python
# Glue Catalog data source — auto-imports tables matching the pattern
finance_glue_source = dz.create_data_source(
    domainIdentifier=domain_id,
    projectIdentifier=finance_project_id,
    environmentIdentifier=finance_dev_env_id,
    type="GLUE",
    name="finance-glue-tables",
    configuration={
        "glueRunConfiguration": {
            "relationalFilterConfigurations": [{
                "databaseName": "finance_curated",
                "filterExpressions": [
                    {"type": "INCLUDE", "expression": "fct_*"},
                    {"type": "INCLUDE", "expression": "dim_*"},
                ],
                "schemaName": "finance_curated",
            }],
            "autoImportDataQualityResult": True,
            "dataAccessRole": dz_data_access_role.role_arn,
        },
    },
    schedule={
        "schedule": "cron(0 1 * * ? *)",                              # nightly 1am
        "timezone": "UTC",
    },
    publishOnImport=True,
    enableSetting="ENABLED",
)

# S3 Tables data source (newer 2024+) — auto-imports table metadata
ml_s3tables_source = dz.create_data_source(
    domainIdentifier=domain_id,
    projectIdentifier=ml_project_id,
    environmentIdentifier=ml_dev_env_id,
    type="S3_TABLES",
    name="ml-s3tables",
    configuration={
        "s3TablesConfiguration": {
            "tableBucketArn": self.lakehouse_bucket.attr_arn,
            "iamMode": True,                                          # IAM mode (not LF)
        },
    },
)
```

### 3.6 Trusted Identity Propagation (TIP) — end-user attribution

Unified Studio supports TIP — SSO user identity propagates through queries, so Athena scan logs show the actual user (not the Studio service role):

```python
# Glue connection w/ TIP enabled
glue_tip_conn = glue.CfnConnection(self, "GlueTip",
    catalog_id=self.account,
    connection_input=glue.CfnConnection.ConnectionInputProperty(
        name="finance-tip-connection",
        connection_type="JDBC",
        authentication_configuration={
            "AuthenticationType": "TRUSTED_IDENTITY_PROPAGATION",
            "TrustedIdentityProperties": {"identityProvider": "IAM_IDENTITY_CENTER"},
        },
        # ... rest of connection config ...
    ),
)
```

---

## 4. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Unified Studio portal blank after login | DataZone domain not associated with Studio | Re-run blueprint enable + wait for domain `available` state |
| MLflow App can't register model | Model Registry not in same DataZone domain | Either create model registry inside the project OR cross-domain via RAM |
| TIP not propagating user identity | Glue connection auth type wrong | Set `AuthenticationType: TRUSTED_IDENTITY_PROPAGATION`; verify IAM Identity Center has user assigned |
| Project members can't see Glue tables | Data source not published / not subscribed | `publishOnImport: True` + project member subscribes via Studio Catalog UI |
| EMR Serverless app fails to launch from notebook | EMR app not in project environment | Add EMR Serverless to project blueprint via DataZone API |
| Notebook can't access S3 Tables | S3 Tables IAM mode not set | `s3TablesConfiguration.iamMode: True` (not Lake Formation mode) |
| Bedrock invoke fails | Project IAM lacks `bedrock:InvokeModel` | Add to project's user role; Bedrock model access must be enabled per region |
| Cross-project sharing fails | Asset not subscribed via DataZone | Subscribe through Catalog UI, not direct IAM |

### 4.1 Cost ballpark

| Component | $/mo |
|---|---|
| DataZone domain (1 domain) | $0 (free tier first 6 mo, then ~$50/mo) |
| MLflow App (per project, always-on) | ~$30 (small) - $200 (large) |
| Studio user execution (per user × hours) | $0.25/hr per t3.medium notebook |
| EMR Serverless app (idle, prewarmed) | ~$50 (idle) + per-second usage |
| TIP propagation overhead | $0 |

---

## 5. Micro-Stack variant (cross-stack via SSM)

```python
# In DomainStack
ssm.StringParameter(self, "DzDomainId",
    parameter_name=f"/{{project_name}}/{stage}/datazone/domain-id",
    string_value=self.dz_domain.attr_id)
ssm.StringParameter(self, "StudioPortalUrl",
    parameter_name=f"/{{project_name}}/{stage}/studio/portal-url",
    string_value=f"https://{self.dz_domain.attr_portal_url}")

# In ProjectsStack — references SSM-resolved domain ID
domain_id = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/datazone/domain-id")

datazone.CfnProject(self, "NewProject",
    domain_identifier=domain_id,
    name="research-team",
)
```

---

## 6. Worked example — pytest

```python
def test_unified_studio_domain_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    from infrastructure.cdk.stacks.unified_studio_stack import UnifiedStudioStack
    stack = UnifiedStudioStack(app, stage_name="dev", env=env, ...)
    t = Template.from_stack(stack)

    # DataZone domain
    t.has_resource_properties("AWS::DataZone::Domain", Match.object_like({
        "Name": Match.string_like_regexp(r".*domain-dev"),
        "DomainVersion": "V2",
        "SingleSignOn": Match.object_like({"Type": "IAM_IDC"}),
    }))
    # 2 projects
    t.resource_count_is("AWS::DataZone::Project", 2)
    # SageMaker Studio domain in SSO mode
    t.has_resource_properties("AWS::SageMaker::Domain", Match.object_like({
        "AuthMode": "SSO",
        "AppNetworkAccessType": "VpcOnly",
    }))
```

---

## 7. Five non-negotiables

1. **`auth_mode="SSO"` for Unified Studio.** IAM mode (auth_mode="IAM") works but defeats the SSO + TIP value. SSO is the modern default; IAM is for legacy compat.

2. **`app_network_access_type="VpcOnly"`.** Public mode exposes notebook traffic to internet — security review will reject. Always VPC-only for production.

3. **Blueprint enablement BEFORE project creation.** DataZone projects fail without a blueprint. Run the custom resource that calls `enableEnvironmentConfiguration` once per domain.

4. **MLflow Apps replace Tracking Servers in 2024+.** New deploys use Apps; old Tracking Servers are still supported but no new features. Migrate within 12 months.

5. **TIP requires IAM Identity Center + Glue 5.0 connections.** Older Glue connections fall back to project IAM role — no per-user attribution. Upgrade Glue connections to use TIP auth type.

---

## 8. References

- `docs/template_params.md` — `DOMAIN_VERSION`, `STUDIO_AUTH_MODE`, `STUDIO_NETWORK_ACCESS_TYPE`, `MLFLOW_APP_SIZE`, `TIP_ENABLED`
- AWS docs:
  - [Unified Studio data sources](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/data-source-glue.html)
  - [Unified Studio release notes](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/release-notes.html)
  - [Connect to data sources](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/connect-data-sources.html)
  - [MLflow Apps for experiment tracking](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/use-mlflow-experiments.html)
  - [Managed MLflow on SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/mlflow.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — classic Studio domain (older)
  - `DATA_DATAZONE` — DataZone domain mesh patterns
  - `DATA_GLUE_CATALOG` — Glue catalog underlying
  - `DATA_ICEBERG_S3_TABLES` — S3 Tables that Unified Studio consumes

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Unified Studio (2024+) with DataZone domain + projects + environments + MLflow Apps + TIP. CDK monolith with custom-resource blueprint enablement. Bootstrap script for project environment creation. Glue + S3 Tables + EMR Serverless + Bedrock data sources. 5 non-negotiables incl. SSO + VPC-only + TIP. Created to fill F369 audit gap (2026-04-26): Unified Studio was 0% covered as CDK; replaces classic Studio for new builds. |
