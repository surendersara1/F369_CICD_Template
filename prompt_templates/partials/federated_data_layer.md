# SOP — Federated Data Layer (S3 Lake + Glue + Athena + Lake Formation)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Phase 3+ (cross-study analytics)

---

## 1. Purpose

Analytic data layer layered on top of the operational data stores:

- **S3 data lake** with raw / curated / consumed zones
- **AWS Glue** crawlers + data catalog
- **Amazon Athena** workgroup + saved queries
- **AWS Lake Formation** — fine-grained row/column access
- **DynamoDB Streams → Firehose → S3** for operational-to-analytic CDC
- **Amazon QuickSight** (optional) — exec dashboards

This is Phase 3 territory. For in-project Phase 1 analytics, prefer direct queries against RDS.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Analytics co-deployed with main workload (small team, POC) | **§3 Monolith Variant** |
| Dedicated `DataLakeStack` with separate deploy cadence | **§4 Micro-Stack Variant** |

Data lake resources don't create cross-stack cycles on typical consumption. Only risk: `grant_read_write` on lake buckets from cross-stack Athena workgroup can trigger the same KMS auto-grant we've documented elsewhere.

---

## 3. Monolith Variant

### 3.1 Tiered S3 lake

```python
import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, aws_s3 as s3


# Three zones — raw (immutable landing), curated (Glue-cleaned), consumed (query-ready)
common = dict(
    encryption=s3.BucketEncryption.KMS,
    encryption_key=self.audio_data_key,
    enforce_ssl=True,
    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    versioned=True,
    removal_policy=RemovalPolicy.DESTROY,  # POC; RETAIN in prod
    auto_delete_objects=True,
)

self.lake_raw      = s3.Bucket(self, "LakeRaw",      bucket_name=f"{{project_name}}-lake-raw-{stage}", **common)
self.lake_curated  = s3.Bucket(self, "LakeCurated",  bucket_name=f"{{project_name}}-lake-curated-{stage}", **common)
self.lake_consumed = s3.Bucket(self, "LakeConsumed", bucket_name=f"{{project_name}}-lake-consumed-{stage}",
    lifecycle_rules=[s3.LifecycleRule(
        transitions=[s3.Transition(
            storage_class=s3.StorageClass.INTELLIGENT_TIERING,
            transition_after=Duration.days(30),
        )],
    )],
    **{k: v for k, v in common.items()},
)
```

### 3.2 Glue catalog + crawler

```python
from aws_cdk import aws_glue_alpha as glue_alpha   # alpha L2 — stable alternative below
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam


glue_role = iam.Role(
    self, "GlueRole",
    assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
    managed_policies=[
        iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
    ],
)
# Monolith: L2 grants OK
self.lake_raw.grant_read(glue_role)
self.lake_curated.grant_read_write(glue_role)

database = glue.CfnDatabase(
    self, "LakeDb",
    catalog_id=self.account,
    database_input=glue.CfnDatabase.DatabaseInputProperty(
        name=f"{{project_name}}_lake_{stage}",
    ),
)

crawler = glue.CfnCrawler(
    self, "LakeCrawler",
    role=glue_role.role_arn,
    database_name=database.ref,
    targets=glue.CfnCrawler.TargetsProperty(
        s3_targets=[glue.CfnCrawler.S3TargetProperty(
            path=self.lake_curated.s3_url_for_object(),
        )],
    ),
    schedule=glue.CfnCrawler.ScheduleProperty(schedule_expression="cron(0 3 * * ? *)"),
)
```

### 3.3 Athena workgroup

```python
from aws_cdk import aws_athena as athena


athena_results = s3.Bucket(self, "AthenaResults",
    bucket_name=f"{{project_name}}-athena-results-{stage}",
    encryption=s3.BucketEncryption.KMS,
    encryption_key=self.audio_data_key,
    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    removal_policy=RemovalPolicy.DESTROY,
    auto_delete_objects=True,
    lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
)

athena.CfnWorkGroup(
    self, "Workgroup",
    name=f"{{project_name}}-wg-{stage}",
    state="ENABLED",
    work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
        result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
            output_location=athena_results.s3_url_for_object(),
            encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                encryption_option="SSE_KMS",
                kms_key=self.audio_data_key.key_arn,
            ),
        ),
        enforce_work_group_configuration=True,
        publish_cloud_watch_metrics_enabled=True,
    ),
)
```

### 3.4 DDB Streams → Firehose → S3 (CDC)

```python
from aws_cdk import aws_kinesisfirehose as kfh, aws_kinesisfirehose_destinations as kfh_dest
from aws_cdk import aws_lambda_event_sources as les


# Firehose landing in lake_raw
delivery_stream = kfh.DeliveryStream(
    self, "DdbCdcFirehose",
    destinations=[kfh_dest.S3Bucket(
        self.lake_raw,
        buffering_interval=Duration.seconds(60),
        buffering_size=cdk.Size.mebibytes(5),
        compression=kfh_dest.Compression.GZIP,
        data_output_prefix="jobs-ledger/!{timestamp:yyyy/MM/dd}/",
    )],
)

# Lambda: DDB stream record → Firehose put
cdc_fn = _lambda.Function(
    self, "DdbCdcFn",
    runtime=_lambda.Runtime.PYTHON_3_12,
    handler="index.handler",
    code=_lambda.Code.from_asset("src/ddb_cdc"),
    environment={"DELIVERY_STREAM": delivery_stream.delivery_stream_name},
)
delivery_stream.grant_put_records(cdc_fn)
cdc_fn.add_event_source(les.DynamoEventSource(
    self.ddb_tables["jobs_ledger"],
    starting_position=_lambda.StartingPosition.LATEST,
    batch_size=100,
    max_batching_window=Duration.seconds(5),
))
```

### 3.5 Monolith gotchas

- **`grant_read_write` on encrypted lake buckets** auto-grants KMS on the CMK — fine in monolith.
- **Glue crawler scheduler** times are UTC; nightly 3 AM UTC is 11 PM ET previous day.
- **Athena results bucket** should have a 30-day expiration lifecycle — costs accrue otherwise.

---

## 4. Micro-Stack Variant

### 4.1 `DataLakeStack`

```python
import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy, Duration,
    aws_s3 as s3,
    aws_kms as kms,
    aws_glue as glue,
    aws_athena as athena,
    aws_iam as iam,
)
from constructs import Construct


class DataLakeStack(cdk.Stack):
    def __init__(
        self, scope: Construct,
        audio_data_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-data-lake", **kwargs)

        common = dict(
            encryption=s3.BucketEncryption.KMS,
            encryption_key=audio_data_key,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.raw      = s3.Bucket(self, "Raw",      bucket_name="{project_name}-lake-raw",      **common)
        self.curated  = s3.Bucket(self, "Curated",  bucket_name="{project_name}-lake-curated",  **common)
        self.consumed = s3.Bucket(self, "Consumed", bucket_name="{project_name}-lake-consumed", **common)

        # Glue role — identity-side grants on cross-stack CMK
        glue_role = iam.Role(self, "GlueRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole")],
        )
        # Identity-side S3 + KMS (avoid auto-mutation of upstream KMS key policy)
        glue_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
            resources=[
                self.raw.bucket_arn,     self.raw.arn_for_objects("*"),
                self.curated.bucket_arn, self.curated.arn_for_objects("*"),
            ],
        ))
        glue_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
            resources=[audio_data_key.key_arn],
        ))

        self.database = glue.CfnDatabase(
            self, "Db",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(name="{project_name}_lake"),
        )

        cdk.CfnOutput(self, "RawBucket",      value=self.raw.bucket_name)
        cdk.CfnOutput(self, "CuratedBucket",  value=self.curated.bucket_name)
        cdk.CfnOutput(self, "ConsumedBucket", value=self.consumed.bucket_name)
        cdk.CfnOutput(self, "DatabaseName",   value=self.database.ref)
```

### 4.2 Micro-stack gotchas

- **Lake Formation** permissions are per-principal-per-resource; manage with `lakeformation.CfnPermissions`.
- **Cross-account data lake** uses LF-tags — out of scope here.

---

## 5. Worked example

```python
def test_data_lake_has_three_zones():
    import aws_cdk as cdk
    from aws_cdk import aws_kms as kms
    from aws_cdk.assertions import Template
    from infrastructure.cdk.stacks.data_lake_stack import DataLakeStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    dep = cdk.Stack(app, "D", env=env)
    key = kms.Key(dep, "K")

    dl = DataLakeStack(app, audio_data_key=key, env=env)
    t = Template.from_stack(dl)
    t.resource_count_is("AWS::S3::Bucket", 3)
    t.resource_count_is("AWS::Glue::Database", 1)
```

---

## 6. References

- `docs/Feature_Roadmap.md` — DL-01..DL-08
- Related SOPs: `LAYER_DATA` (operational sources), `LLMOPS_BEDROCK` (RAG KB uses data lake)

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Identity-side grants for cross-stack Glue role. |
| 1.0 | 2026-03-05 | Initial (unnumbered). |
