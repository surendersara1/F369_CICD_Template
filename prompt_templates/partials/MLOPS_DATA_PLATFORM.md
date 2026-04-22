# SOP — MLOps Data Platform (Glue, Athena, Lake Formation, Redshift, EMR)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · S3 data lake (4 zones) · Glue 4.0 (Spark 3.3 / Python 3.10) · Athena · Lake Formation · Redshift Serverless · EMR Serverless 6.15

---

## 1. Purpose

- Provision the ML data platform foundation: four-zone S3 lake (raw → processed → curated → features) with EventBridge notifications, intelligent tiering, retention by zone.
- Codify Glue: Data Catalog database, daily crawler on the processed zone, Spark ETL job (`raw → processed`) with Iceberg format + security configuration (KMS on S3, CloudWatch, job bookmarks).
- Codify Athena: cost-controlled workgroup with a 10 GB scan cutoff, KMS on query results, 30-day result expiry.
- Codify Lake Formation: default admins, remove `IAMAllowedPrincipals`, enforce explicit grants.
- Codify Redshift Serverless (namespace + workgroup, RPU sized by stage) and EMR Serverless (Spark app with pre-init capacity + auto-stop).
- Include when the SOW mentions data science, ML, feature engineering, analytics, data warehouse, or data lake.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns VPC + KMS + lake buckets + Glue + Athena + Redshift + EMR | **§3 Monolith Variant** |
| `NetworkStack` (VPC), `SecurityStack` (KMS), `DataLakeStack` (buckets + Glue + Athena), `WarehouseStack` (Redshift), `ComputeStack` (EMR) | **§4 Micro-Stack Variant** |

**Why the split matters.** Glue / Athena / Redshift / EMR all need `s3:*` on lake buckets and `kms:Encrypt/Decrypt` on the lake KMS key. In monolith, `bucket.grant_read_write(glue_role)` is a local L2 grant. Cross-stack it mutates the bucket policy in another stack → cycle. Micro-Stack uses identity-side grants with bucket ARNs from SSM. Redshift's `kms_key_id=` and Glue's `security_configuration kms_key_arn=` must be string ARNs (fifth non-negotiable) when the CMK is in another stack.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Architecture

```
Raw Sources                 Ingestion              Storage              Query / Analysis
─────────────              ──────────             ─────────            ────────────────
S3 raw data        ──►   Glue ETL Jobs    ──►   S3 Data Lake    ──►   Athena SQL
Kinesis streams    ──►   Glue Streaming   ──►   Glue Catalog    ──►   SageMaker Studio
RDS / Aurora       ──►   Glue Connectors  ──►   Redshift DW     ──►   QuickSight BI
DynamoDB Streams   ──►   EMR Serverless   ──►   Feature Store   ──►   Jupyter Notebooks
External APIs      ──►   Lambda ETL       ──►   Iceberg tables  ──►   Spark on EMR

Four-zone lake:  raw → processed → curated → features
```

### 3.2 S3 data lake — four zones

```python
from typing import Dict
import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_s3 as s3,
    aws_iam as iam,
    aws_glue as glue,
    aws_athena as athena,
    aws_lakeformation as lakeformation,
    aws_redshiftserverless as redshift,
    aws_emrserverless as emr,
)


def _create_ml_data_platform(self, stage_name: str) -> None:
    """Assumes self.{vpc, kms_key, db_secret, lambda_sg, aurora_sg} exist."""

    lake_zones = {
        "raw":       {"retention_days": 365 * 7, "intelligent_after_days": 0},
        "processed": {"retention_days": 365 * 3, "intelligent_after_days": 30},
        "curated":   {"retention_days": 365 * 2, "intelligent_after_days": 90},
        "features":  {"retention_days": 365,     "intelligent_after_days": 30},
    }

    self.lake_buckets: Dict[str, s3.Bucket] = {}
    for zone, cfg in lake_zones.items():
        self.lake_buckets[zone] = s3.Bucket(
            self, f"DataLake{zone.capitalize()}",
            bucket_name=f"{{project_name}}-datalake-{zone}-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,             # same-stack KMS — safe in monolith
            versioned=True,
            event_bridge_enabled=True,               # S3 → EventBridge pattern
            lifecycle_rules=[s3.LifecycleRule(
                id=f"{zone}-retention",
                enabled=True,
                expiration=Duration.days(cfg["retention_days"]),
                transitions=[s3.Transition(
                    storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                    transition_after=Duration.days(cfg["intelligent_after_days"]),
                )] if cfg["intelligent_after_days"] > 0 else [],
            )],
            removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        )
```

### 3.3 Glue — role, catalog, crawler, Spark ETL, security config

```python
# Glue execution role
glue_role = iam.Role(
    self, "GlueRole",
    role_name=f"{{project_name}}-glue-role-{stage_name}",
    assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
    managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole")],
)
for bucket in self.lake_buckets.values():
    bucket.grant_read_write(glue_role)            # L2 safe in monolith
self.kms_key.grant_encrypt_decrypt(glue_role)

# Catalog database
glue.CfnDatabase(self, "GlueDatabase",
    catalog_id=Aws.ACCOUNT_ID,
    database_input=glue.CfnDatabase.DatabaseInputProperty(
        name=f"{{project_name}}_{stage_name}_catalog",
        description=f"{{project_name}} data catalog for {stage_name}",
    ),
)

# Daily crawler on processed zone
glue.CfnCrawler(self, "ProcessedDataCrawler",
    name=f"{{project_name}}-processed-crawler-{stage_name}",
    role=glue_role.role_arn,
    database_name=f"{{project_name}}_{stage_name}_catalog",
    targets=glue.CfnCrawler.TargetsProperty(
        s3_targets=[glue.CfnCrawler.S3TargetProperty(
            path=f"s3://{self.lake_buckets['processed'].bucket_name}/",
            sample_size=10,
        )],
    ),
    schedule=glue.CfnCrawler.ScheduleProperty(schedule_expression="cron(0 6 * * ? *)") if stage_name != "dev" else None,
    configuration=(
        '{"Version": 1.0, '
        '"CrawlerOutput": {"Partitions": {"AddOrUpdateBehavior": "InheritFromTable"}, '
        '"Tables": {"AddOrUpdateBehavior": "MergeNewColumns"}}, '
        '"Grouping": {"TableGroupingPolicy": "CombineCompatibleSchemas"}}'
    ),
)

# Glue security configuration (KMS encryption for job S3 / logs / bookmarks)
glue.CfnSecurityConfiguration(self, "GlueSecurityConfig",
    name=f"{{project_name}}-glue-security-{stage_name}",
    encryption_configuration=glue.CfnSecurityConfiguration.EncryptionConfigurationProperty(
        s3_encryptions=[glue.CfnSecurityConfiguration.S3EncryptionProperty(
            kms_key_arn=self.kms_key.key_arn, s3_encryption_mode="SSE-KMS",
        )],
        cloud_watch_encryption=glue.CfnSecurityConfiguration.CloudWatchEncryptionProperty(
            cloud_watch_encryption_mode="SSE-KMS", kms_key_arn=self.kms_key.key_arn,
        ),
        job_bookmarks_encryption=glue.CfnSecurityConfiguration.JobBookmarksEncryptionProperty(
            job_bookmarks_encryption_mode="CSE-KMS", kms_key_arn=self.kms_key.key_arn,
        ),
    ),
)

# Spark ETL job — raw → processed, Iceberg format
glue.CfnJob(self, "RawToProcessedJob",
    name=f"{{project_name}}-raw-to-processed-{stage_name}",
    role=glue_role.role_arn,
    description="Transform raw → cleaned Parquet/Iceberg in processed zone",
    glue_version="4.0",
    command=glue.CfnJob.JobCommandProperty(
        name="glueetl",
        script_location=f"s3://{self.lake_buckets['raw'].bucket_name}/glue-scripts/raw_to_processed.py",
        python_version="3",
    ),
    default_arguments={
        "--job-language":                    "python",
        "--enable-metrics":                  "true",
        "--enable-continuous-cloudwatch-log": "true",
        "--enable-spark-ui":                 "true",
        "--spark-event-logs-path":           f"s3://{self.lake_buckets['raw'].bucket_name}/spark-logs/",
        "--TempDir":                         f"s3://{self.lake_buckets['raw'].bucket_name}/glue-temp/",
        "--source_bucket":                   self.lake_buckets["raw"].bucket_name,
        "--target_bucket":                   self.lake_buckets["processed"].bucket_name,
        "--stage":                           stage_name,
        "--enable-glue-datacatalog":         "true",
        "--datalake-formats":                "iceberg",
        "--conf":                            "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    },
    number_of_workers=2  if stage_name == "dev" else 10,
    worker_type="G.1X"   if stage_name == "dev" else "G.2X",
    max_retries=1,
    timeout=120,
    security_configuration=f"{{project_name}}-glue-security-{stage_name}",
    execution_property=glue.CfnJob.ExecutionPropertyProperty(max_concurrent_runs=3),
    tags={"Project": "{project_name}", "Environment": stage_name, "Layer": "DataPlatform"},
)
```

### 3.4 Athena workgroup (cost-controlled)

```python
athena_results_bucket = s3.Bucket(self, "AthenaResults",
    bucket_name=f"{{project_name}}-athena-results-{stage_name}-{Aws.ACCOUNT_ID}",
    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    encryption=s3.BucketEncryption.KMS,
    encryption_key=self.kms_key,
    lifecycle_rules=[s3.LifecycleRule(id="expire-query-results",
        expiration=Duration.days(30), enabled=True)],
    removal_policy=RemovalPolicy.DESTROY,
)

athena.CfnWorkGroup(self, "DataScienceWorkgroup",
    name=f"{{project_name}}-datascience-{stage_name}",
    description="Athena workgroup for data scientists and ML pipelines",
    state="ENABLED",
    work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
        result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
            output_location=f"s3://{athena_results_bucket.bucket_name}/query-results/",
            encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                encryption_option="SSE_KMS", kms_key=self.kms_key.key_arn,
            ),
        ),
        bytes_scanned_cutoff_per_query=10 * 1024 * 1024 * 1024,   # 10 GB
        enforce_work_group_configuration=True,
        publish_cloud_watch_metrics_enabled=True,
        requester_pays_enabled=False,
    ),
)
```

### 3.5 Lake Formation settings

```python
lakeformation.CfnDataLakeSettings(self, "LakeFormationSettings",
    admins=[lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty(
        data_lake_principal_identifier=glue_role.role_arn,
    )],
    # Remove legacy IAMAllowedPrincipals; enforce explicit LF grants
    create_database_default_permissions=[],
    create_table_default_permissions=[],
)
```

### 3.6 Redshift Serverless + EMR Serverless

```python
if stage_name != "dev":
    redshift_namespace = redshift.CfnNamespace(self, "RedshiftNamespace",
        namespace_name=f"{{project_name}}-{stage_name}",
        db_name="{project_name}_dw",
        admin_username="admin",
        admin_user_password=self.db_secret.secret_value_from_json("password").unsafe_unwrap(),
        kms_key_id=self.kms_key.key_arn,
        log_exports=["userlog", "connectionlog", "useractivitylog"],
    )
    redshift.CfnWorkgroup(self, "RedshiftWorkgroup",
        workgroup_name=f"{{project_name}}-{stage_name}",
        namespace_name=redshift_namespace.namespace_name,
        base_capacity=8 if stage_name == "staging" else 32,     # RPUs
        enhanced_vpc_routing=True,
        subnet_ids=[s.subnet_id for s in self.vpc.isolated_subnets],
        security_group_ids=[self.aurora_sg.security_group_id],
        publicly_accessible=False,
    )

# EMR Serverless (Spark)
emr_role = iam.Role(self, "EMRServerlessRole",
    role_name=f"{{project_name}}-emr-role-{stage_name}",
    assumed_by=iam.ServicePrincipal("emr-serverless.amazonaws.com"),
)
for bucket in self.lake_buckets.values():
    bucket.grant_read_write(emr_role)
self.kms_key.grant_encrypt_decrypt(emr_role)

emr.CfnApplication(self, "EMRServerlessApp",
    name=f"{{project_name}}-spark-{stage_name}",
    type="SPARK",
    release_label="emr-6.15.0",
    initial_capacity={
        "Driver":   emr.CfnApplication.InitialCapacityConfigProperty(
            worker_count=1,
            worker_configuration=emr.CfnApplication.WorkerConfigurationProperty(cpu="4vCPU", memory="16gb"),
        ),
        "Executor": emr.CfnApplication.InitialCapacityConfigProperty(
            worker_count=4,
            worker_configuration=emr.CfnApplication.WorkerConfigurationProperty(cpu="4vCPU", memory="16gb", disk="200gb"),
        ),
    } if stage_name == "prod" else {},
    auto_stop_configuration=emr.CfnApplication.AutoStopConfigurationProperty(
        enabled=True, idle_timeout_minutes=15,
    ),
    network_configuration=emr.CfnApplication.NetworkConfigurationProperty(
        subnet_ids=[s.subnet_id for s in self.vpc.private_subnets],
        security_group_ids=[self.lambda_sg.security_group_id],
    ),
    tags=[{"key": "Project", "value": "{project_name}"}, {"key": "Environment", "value": stage_name}],
)
```

### 3.7 Monolith gotchas

- **`event_bridge_enabled=True`** on every lake bucket — required for S3 → EventBridge → downstream Lambdas, otherwise CDK falls back to direct-notification patterns that won't work in micro-stack.
- **Glue 4.0** is Spark 3.3 / Python 3.10. Using features from Spark 3.4+ (e.g., new connectors) requires Glue 5.0+.
- **Athena `bytes_scanned_cutoff_per_query=10GB`** is a *hard* cutoff; queries exceeding it are killed mid-flight. Raise for ad-hoc analyst workgroups, keep tight for CI/scheduled queries.
- **Lake Formation `create_database/table_default_permissions=[]`** is the critical setting — without it you stay on legacy `IAMAllowedPrincipals` and LF grants become advisory.
- **Redshift `admin_user_password=secret.secret_value_from_json("password").unsafe_unwrap()`** — CDK-idiomatic, but the secret value appears in the CFN template. Use `redshift.User` resource or Secrets Manager rotation for production secrets hygiene.
- **EMR initial capacity** pre-warms the Spark app (no cold start) but costs $0 only when `auto_stop_configuration.idle_timeout_minutes` has elapsed. Tune per workload cadence.

---

## 4. Micro-Stack Variant

**Use when:** production MSxx layout — `DataLakeStack` owns buckets + Glue + Athena; `WarehouseStack` owns Redshift; `ComputeStack` owns EMR.

### 4.1 The five non-negotiables

1. **Anchor Glue script paths** via SSM-published bucket names, not construct refs.
2. **Never call `bucket.grant_read_write(cross_stack_role)`** — identity-side `PolicyStatement` on the role.
3. **Never target cross-stack queues** — not relevant here, but downstream Lambdas consuming Glue output use `CfnRule` via EventBridge.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on Redshift / Glue security config / Athena workgroup — pass KMS ARN **as a string** from SSM.

### 4.2 `DataLakeStack` (buckets + Glue + Athena)

Same code as §3.2–§3.5 moved into a `DataLakeStack(cdk.Stack)`. Key differences:

- KMS is read as string ARN from SSM: `lake_key_arn = ssm.StringParameter.value_for_string_parameter(self, "/.../lake_key_arn")`.
- Buckets use `encryption=s3.BucketEncryption.KMS` + `encryption_key=kms.Key.from_key_arn(self, "LakeKey", lake_key_arn)` — this is a same-stack reference once imported, so the bucket policy stays local.
- Publish bucket names + catalog name + glue role ARN via SSM:

```python
for zone, bkt in self.lake_buckets.items():
    ssm.StringParameter(self, f"LakeBucket{zone.capitalize()}Param",
        parameter_name=f"/{{project_name}}/lake/{zone}_bucket",
        string_value=bkt.bucket_name,
    )
ssm.StringParameter(self, "GlueRoleArnParam",
    parameter_name=f"/{{project_name}}/lake/glue_role_arn",
    string_value=glue_role.role_arn,
)
ssm.StringParameter(self, "AthenaWorkgroupParam",
    parameter_name=f"/{{project_name}}/lake/athena_workgroup",
    string_value=f"{{project_name}}-datascience-{stage_name}",
)
ssm.StringParameter(self, "CatalogDatabaseParam",
    parameter_name=f"/{{project_name}}/lake/catalog_database",
    string_value=f"{{project_name}}_{stage_name}_catalog",
)
```

### 4.3 `WarehouseStack` (Redshift)

Reads VPC, KMS ARN, secret ARN, subnet IDs, security-group ID via SSM. Creates its own namespace + workgroup. `kms_key_id=lake_key_arn` (string) avoids the fifth non-negotiable.

### 4.4 `ComputeStack` (EMR Serverless)

Reads the lake bucket ARNs + KMS ARN + VPC subnets / SG from SSM. EMR role is scoped identity-side:

```python
emr_role.add_to_policy(iam.PolicyStatement(
    actions=["s3:*Object", "s3:ListBucket"],
    resources=[f"arn:aws:s3:::{raw_bucket}/*", f"arn:aws:s3:::{raw_bucket}",
               f"arn:aws:s3:::{processed_bucket}/*", f"arn:aws:s3:::{processed_bucket}",
               f"arn:aws:s3:::{curated_bucket}/*", f"arn:aws:s3:::{curated_bucket}",
               f"arn:aws:s3:::{features_bucket}/*", f"arn:aws:s3:::{features_bucket}"],
))
emr_role.add_to_policy(iam.PolicyStatement(
    actions=["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
    resources=[lake_key_arn],
))
```

### 4.5 Micro-stack gotchas

- **`kms.Key.from_key_arn`** gives you an `IKey` that compiles, but attempting `bucket.grant_read_write(fn)` with the imported key + cross-stack role still adds a bucket-policy statement referencing the role. The *bucket* is local in `DataLakeStack`; L2 grants from here to roles in other stacks do NOT work. Always identity-side.
- **Glue script bucket** — pin the script location to a specific S3 prefix (not "just the raw bucket") so CI/CD uploads are idempotent and versioned.
- **Redshift admin password** from `db_secret.secret_value_from_json(...)` leaks into the CloudFormation template. In production use Redshift's `admin_password_secret_arn` (available in newer CDK) to keep the secret out of CFN.
- **Lake Formation admin role** — in micro-stack the Glue role ARN comes from SSM. `CfnDataLakeSettings` can be in `DataLakeStack` since Glue role is local.
- **EMR `initial_capacity={}`** (empty) means no pre-warming — cold start on first job can be 60-90 s. Trade-off between cost and latency.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack |
| Add new zone (e.g. `archived`) | New bucket + lifecycle rule; update crawler targets; publish via SSM |
| Switch from Iceberg → Delta | Change `--datalake-formats=delta` in Glue default args; add `io.delta.sql.DeltaSparkSessionExtension` |
| Heavy Spark workload | Increase EMR initial capacity; switch ETL jobs from Glue to EMR Serverless |
| Analyst BI workload | Enable Redshift even in staging; add QuickSight → Redshift datasource |
| Real-time streaming | Add Glue Streaming job + Kinesis source in `DataLakeStack` |
| PII column masking | Lake Formation column filters; enforce via `CfnPrincipalPermissions` |

---

## 6. Worked example — DataLakeStack synthesizes

Save as `tests/sop/test_MLOPS_DATA_PLATFORM.py`. Offline.

```python
"""SOP verification — DataLakeStack synthesizes 4 buckets + Glue + Athena."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_data_lake_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.data_lake_stack import DataLakeStack
    stack = DataLakeStack(
        app, stage_name="staging",
        lake_key_arn_ssm="/test/security/lake_key_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::S3::Bucket",             5)    # 4 zones + athena-results
    t.resource_count_is("AWS::Glue::Database",         1)
    t.resource_count_is("AWS::Glue::Crawler",          1)
    t.resource_count_is("AWS::Glue::Job",              1)
    t.resource_count_is("AWS::Glue::SecurityConfiguration", 1)
    t.resource_count_is("AWS::Athena::WorkGroup",      1)
```

---

## 7. References

- `docs/template_params.md` — `LAKE_*_BUCKET_SSM`, `LAKE_KMS_KEY_ARN_SSM`, `CATALOG_DB_SSM`, `ATHENA_WORKGROUP_SSM`, `GLUE_ROLE_ARN_SSM`, `REDSHIFT_BASE_CAPACITY_RPU`, `EMR_RELEASE_LABEL`
- `docs/Feature_Roadmap.md` — feature IDs `ML-26..ML-34` (data platform), `S-03..S-08` (lake storage), `DA-01..DA-12` (analytics)
- Glue security configuration: https://docs.aws.amazon.com/glue/latest/dg/encryption-security-configuration.html
- Athena workgroups: https://docs.aws.amazon.com/athena/latest/ug/workgroups-create-update-delete.html
- Lake Formation default permissions: https://docs.aws.amazon.com/lake-formation/latest/dg/change-settings.html
- Related SOPs: `LAYER_DATA` (generic data stack), `MLOPS_SAGEMAKER_TRAINING` (Feature Store consumer), `DATA_LAKEHOUSE_ICEBERG` (Iceberg-specific detail), `LAYER_NETWORKING` (VPC endpoints for Glue / Athena / Redshift), `LAYER_SECURITY` (KMS + permission boundary), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `DataLakeStack`/`WarehouseStack`/`ComputeStack` read KMS ARN + VPC + SG via SSM; identity-side grants on Glue / Redshift / EMR roles; `kms_key_id=` as string ARN (fifth non-negotiable); `event_bridge_enabled=True` on all lake buckets for cross-stack EB fan-out. Added Swap matrix (§5), Worked example (§6), Gotchas on Lake Formation defaults, Redshift secrets leakage, EMR initial capacity trade-offs. |
| 1.0 | 2026-03-05 | Initial — 4-zone lake, Glue catalog/crawler/ETL + security config, Athena, Lake Formation, Redshift Serverless, EMR Serverless. |
