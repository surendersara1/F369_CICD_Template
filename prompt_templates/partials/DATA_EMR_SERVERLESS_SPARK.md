# SOP — EMR Serverless + Spark on Iceberg/Hudi/Delta (heavy transforms · Lake Formation integration)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · EMR Serverless 7.12+ (latest LTS, 2026-04) · Apache Spark 3.5.x · Apache Iceberg 1.10 · Apache Hudi 0.15 · Delta Lake 3.x · Glue Data Catalog as metastore · Lake Formation TBAC enforcement · S3 Tables managed Iceberg integration

---

## 1. Purpose

- Codify the **EMR Serverless pattern** for heavy data transforms that exceed Athena's capabilities: complex UDFs, ML feature engineering, large-scale Iceberg compaction, multi-hop pipelines.
- Provide the **Spark-on-Iceberg-via-Glue-Catalog** integration that gives EMR Serverless jobs read/write parity with Athena and Redshift Spectrum.
- Codify the **Hudi vs Delta vs Iceberg** decision tree (when running Spark on the lakehouse).
- Codify the **Lake Formation enforcement** path: Spark job runs with an LF-aware IAM role, Lake Formation enforces row/column filters during table reads.
- Cover **EMR Serverless 7.12 features** (Iceberg Materialized Views, Hudi LF integration, Iceberg 1.10 upgrade) that change architectural choices vs older EMR.
- This is the **heavy-transform specialisation**. Athena (`DATA_ATHENA`) covers ad-hoc + DML; Glue ETL covers scheduled jobs ≤ 60 min; EMR Serverless covers Spark-only / 60-min+ jobs / UDF-intensive workloads.

When the SOW signals: "complex Spark job", "Iceberg table compaction", "Hudi upserts at scale", "ML feature engineering pipeline", "we need PySpark", "merge-into doesn't fit our pipeline".

---

## 2. Decision tree — Spark on the lakehouse

```
Job type?
├── Ad-hoc query, single table, simple WHERE/GROUP BY → Athena (NOT EMR)
├── Scheduled ETL, ≤ 60 min, ≤ 100 GB → Glue ETL job (NOT EMR)
├── Custom Spark, UDFs, > 60 min OR > 100 GB → §3 EMR Serverless
└── Streaming Spark → §5 EMR Serverless w/ Spark Streaming OR Kinesis Data Analytics

Table format?
├── Iceberg (recommended for new builds, 2026)
│   ├── Want managed maintenance → S3 Tables (see DATA_ICEBERG_S3_TABLES)
│   └── Self-managed → §3 (Spark + Iceberg 1.10)
├── Hudi (existing or upserts-heavy workload) → §4
├── Delta Lake (Databricks portability requirement) → §5
└── No table format (raw Parquet) → migrate to Iceberg before Spark; raw Parquet limits MERGE / time-travel

Compute model?
├── Pre-initialized capacity (warm pool, < 1 sec startup) → for high-frequency / scheduled jobs
└── On-demand (~30-60 sec startup) → for sporadic / dev jobs
```

### 2.1 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC — EMR Serverless app + Spark job script + S3 + Glue Catalog all in one stack | **§3 Monolith Variant** |
| `ComputeStack` owns EMR Serverless app; `JobsStack` owns individual job definitions + scripts | **§6 Micro-Stack Variant** |

**Why the split.** EMR Serverless application is a long-lived resource (warm pool). Job runs are ephemeral. You want to redeploy job logic without recreating the application.

---

## 3. Monolith Variant — EMR Serverless + Iceberg compaction job

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────┐
   │  EMR Serverless Application                              │
   │   - Type: Spark                                           │
   │   - Release: emr-7.12.0                                  │
   │   - Pre-initialized capacity: 4 driver + 8 executor       │
   │   - Network: VPC subnets + SGs                           │
   │   - Auto-stop after 60 min idle                          │
   └────────────────────┬─────────────────────────────────────┘
                        │
                        │  StartJobRun (CLI / Step Functions / EventBridge)
                        │
                        ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Spark Job: Iceberg compact-and-rewrite                   │
   │   - Reads:  s3://qra-curated/orders/ (Iceberg)           │
   │   - Catalog: Glue Data Catalog (default)                 │
   │   - Op: rewrite_data_files() + expire_snapshots()        │
   │   - Auth: IAM execution role + LF permissions on table    │
   │   - Writes: same Iceberg table (atomic)                  │
   └──────────────────────────────────────────────────────────┘
                        │
                        ▼
   Job result → S3 logs bucket + EMR Serverless console run history
```

### 3.2 CDK — `_create_emr_serverless_app()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_emrserverless as emrs,             # L1
    aws_ec2 as ec2,
)


def _create_emr_serverless_app(self, stage: str) -> None:
    """Monolith. EMR Serverless Spark app w/ Iceberg + Glue Catalog support.
    Includes pre-initialized capacity for low-latency job starts."""

    # A) Logs bucket — required by EMR Serverless
    self.emr_logs_bucket = s3.Bucket(self, "EmrLogsBucket",
        bucket_name=f"{{project_name}}-emr-logs-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="DeleteLogsAfter90Days",
            expiration=Duration.days(90),
        )],
        removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
    )

    # B) Job execution role — Spark jobs run as this
    self.emr_job_role = iam.Role(self, "EmrJobRole",
        assumed_by=iam.ServicePrincipal("emr-serverless.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    # S3 read/write on raw + curated zones
    self.raw_bucket.grant_read(self.emr_job_role)
    self.curated_bucket.grant_read_write(self.emr_job_role)
    self.emr_logs_bucket.grant_write(self.emr_job_role)
    self.kms_key.grant_encrypt_decrypt(self.emr_job_role)
    # Glue Catalog read/write for Iceberg tables
    self.emr_job_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "glue:GetDatabase", "glue:GetDatabases",
            "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
            "glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable",
            "glue:CreatePartition", "glue:UpdatePartition", "glue:BatchCreatePartition",
        ],
        resources=[
            f"arn:aws:glue:{self.region}:{self.account}:catalog",
            f"arn:aws:glue:{self.region}:{self.account}:database/lakehouse_*",
            f"arn:aws:glue:{self.region}:{self.account}:table/lakehouse_*/*",
        ],
    ))
    # Lake Formation: GetDataAccess for LF-governed tables
    self.emr_job_role.add_to_policy(iam.PolicyStatement(
        actions=["lakeformation:GetDataAccess"],
        resources=["*"],                                    # LF restricts at API level
    ))
    # CloudWatch logs
    self.emr_job_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "logs:CreateLogStream", "logs:PutLogEvents",
            "logs:CreateLogGroup", "logs:DescribeLogGroups",
        ],
        resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/emr-serverless/*"],
    ))

    # C) The EMR Serverless application
    self.emr_app = emrs.CfnApplication(self, "EmrApp",
        name=f"{{project_name}}-emr-{stage}",
        type="Spark",
        release_label="emr-7.12.0",                         # latest LTS as of 2026-04
        architecture="ARM64",                                # cheaper, faster
        # Pre-initialized capacity for warm-pool starts
        initial_capacity=[
            emrs.CfnApplication.InitialCapacityConfigKeyValuePairProperty(
                key="DRIVER",
                value=emrs.CfnApplication.InitialCapacityConfigProperty(
                    worker_count=2,
                    worker_configuration=emrs.CfnApplication.WorkerConfigurationProperty(
                        cpu="4 vCPU",
                        memory="16 GB",
                        disk="20 GB",
                    ),
                ),
            ),
            emrs.CfnApplication.InitialCapacityConfigKeyValuePairProperty(
                key="EXECUTOR",
                value=emrs.CfnApplication.InitialCapacityConfigProperty(
                    worker_count=4,
                    worker_configuration=emrs.CfnApplication.WorkerConfigurationProperty(
                        cpu="4 vCPU",
                        memory="16 GB",
                        disk="50 GB",
                    ),
                ),
            ),
        ],
        # Auto-scale up to this max
        maximum_capacity=emrs.CfnApplication.MaximumAllowedResourcesProperty(
            cpu="200 vCPU",
            memory="800 GB",
            disk="1000 GB",
        ),
        auto_start_configuration=emrs.CfnApplication.AutoStartConfigurationProperty(
            enabled=True,
        ),
        auto_stop_configuration=emrs.CfnApplication.AutoStopConfigurationProperty(
            enabled=True,
            idle_timeout_minutes=60,                        # cost guard
        ),
        network_configuration=emrs.CfnApplication.NetworkConfigurationProperty(
            subnet_ids=[s.subnet_id for s in self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
            security_group_ids=[self.emr_sg.security_group_id],
        ),
        # Iceberg / Hudi / Delta JARs are bundled in 7.12 — no custom dependencies needed
        runtime_configuration=[
            emrs.CfnApplication.ConfigurationObjectProperty(
                classification="spark-defaults",
                properties={
                    "spark.sql.catalog.glue":              "org.apache.iceberg.spark.SparkCatalog",
                    "spark.sql.catalog.glue.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
                    "spark.sql.catalog.glue.warehouse":    f"s3://{self.curated_bucket.bucket_name}/iceberg/",
                    "spark.sql.catalog.glue.io-impl":      "org.apache.iceberg.aws.s3.S3FileIO",
                    "spark.sql.extensions":                "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
                    "spark.serializer":                    "org.apache.spark.serializer.KryoSerializer",
                },
            ),
            # Lake Formation enforcement
            emrs.CfnApplication.ConfigurationObjectProperty(
                classification="emr-serverless-spark-driver-defaults",
                properties={
                    "spark.executorEnv.AWS_LAKEFORMATION_ENABLED": "true",
                },
            ),
        ],
    )

    CfnOutput(self, "EmrAppId", value=self.emr_app.attr_application_id)
    CfnOutput(self, "EmrJobRoleArn", value=self.emr_job_role.role_arn)
```

### 3.3 Spark job script — Iceberg compaction

`scripts/iceberg_compact.py`:

```python
"""Iceberg table compaction + snapshot expiry. Run nightly via Step Functions
or EventBridge schedule. Args:
  --table          glue.lakehouse_curated.orders
  --target-file-mb 256
"""
from pyspark.sql import SparkSession
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True)
    parser.add_argument("--target-file-mb", type=int, default=256)
    args = parser.parse_args()

    spark = (
        SparkSession.builder
            .appName(f"compact-{args.table}")
            .getOrCreate()
    )

    catalog, db, table = args.table.split(".")

    # 1) Rewrite small data files into target-sized files
    spark.sql(f"""
        CALL {catalog}.system.rewrite_data_files(
            table => '{db}.{table}',
            options => map(
                'target-file-size-bytes', '{args.target_file_mb * 1024 * 1024}',
                'min-input-files', '5'
            )
        )
    """).show()

    # 2) Rewrite manifests (consolidate metadata)
    spark.sql(f"""
        CALL {catalog}.system.rewrite_manifests(
            table => '{db}.{table}'
        )
    """).show()

    # 3) Expire old snapshots (keep last 7 days)
    spark.sql(f"""
        CALL {catalog}.system.expire_snapshots(
            table => '{db}.{table}',
            older_than => TIMESTAMP '{ (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S") }',
            retain_last => 5
        )
    """).show()

    # 4) Remove orphan files (defragment storage)
    spark.sql(f"""
        CALL {catalog}.system.remove_orphan_files(
            table => '{db}.{table}'
        )
    """).show()

    spark.stop()


if __name__ == "__main__":
    main()
```

Upload to S3, then trigger via Step Functions:

```python
# In OrchestrationStack — SFN that runs the compaction nightly
sfn_workflow = sfn.StateMachine(self, "IcebergCompact",
    definition=sfn_tasks.CallAwsService(
        self, "RunCompaction",
        service="emrserverless",
        action="startJobRun",
        parameters={
            "ApplicationId": self.emr_app.attr_application_id,
            "ExecutionRoleArn": self.emr_job_role.role_arn,
            "JobDriver": {
                "SparkSubmit": {
                    "EntryPoint": f"s3://{self.scripts_bucket.bucket_name}/iceberg_compact.py",
                    "EntryPointArguments.$": "States.Array('--table', $.tableName, '--target-file-mb', '256')",
                    "SparkSubmitParameters": (
                        "--conf spark.executor.cores=4 "
                        "--conf spark.executor.memory=12g "
                        "--conf spark.executor.instances=4"
                    ),
                },
            },
            "ConfigurationOverrides": {
                "MonitoringConfiguration": {
                    "S3MonitoringConfiguration": {
                        "LogUri": f"s3://{self.emr_logs_bucket.bucket_name}/spark-logs/",
                    },
                },
            },
        },
        iam_resources=["*"],
    ),
)
```

---

## 4. Hudi-on-Spark variant (when source is upsert-heavy)

Use Hudi when:
- Most writes are upserts (CDC-driven), not appends
- You need MERGE-INTO performance optimized for high update rates
- 7.12 supports Hudi LF access

Key Spark conf differences for Hudi:

```python
spark_conf = {
    "spark.serializer": "org.apache.spark.serializer.KryoSerializer",
    "spark.sql.catalog.glue": "org.apache.iceberg.spark.SparkCatalog",  # not used for Hudi
    "spark.sql.extensions":   "org.apache.spark.sql.hudi.HoodieSparkSessionExtension",
    "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.hudi.catalog.HoodieCatalog",
}
```

Hudi tables are written via Spark's DataFrame writer with `format("hudi")` rather than via `CALL glue.system.*` procedures.

---

## 5. Delta Lake variant (when Databricks portability matters)

Use Delta when:
- The customer is Databricks-aligned and wants tables that work in both EMR and Databricks
- Spark Structured Streaming → Delta sink (well-tuned)
- 7.12 supports Delta 3.x

```python
spark_conf = {
    "spark.sql.extensions":            "io.delta.sql.DeltaSparkSessionExtension",
    "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
}
```

---

## 6. Micro-Stack variant (cross-stack via SSM)

```python
# In ComputeStack
ssm.StringParameter(self, "EmrAppId",
    parameter_name=f"/{{project_name}}/{stage}/emr/app-id",
    string_value=self.emr_app.attr_application_id)
ssm.StringParameter(self, "EmrJobRoleArn",
    parameter_name=f"/{{project_name}}/{stage}/emr/job-role-arn",
    string_value=self.emr_job_role.role_arn)

# In JobsStack — uploads job scripts to scripts bucket, SFN runs them
emr_app_id = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/emr/app-id")
job_role_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/emr/job-role-arn")

# SFN picks up the IDs at runtime; identity-side perms
sfn_role.add_to_policy(iam.PolicyStatement(
    actions=["emr-serverless:StartJobRun", "emr-serverless:GetJobRun"],
    resources=[f"arn:aws:emr-serverless:{self.region}:{self.account}:/applications/{emr_app_id}",
               f"arn:aws:emr-serverless:{self.region}:{self.account}:/applications/{emr_app_id}/jobruns/*"],
))
sfn_role.add_to_policy(iam.PolicyStatement(
    actions=["iam:PassRole"],
    resources=[job_role_arn],
    conditions={"StringEquals": {"iam:PassedToService": "emr-serverless.amazonaws.com"}},
))
```

---

## 7. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Job stuck in PENDING for > 5 min | Pre-init capacity hit, no auto-scale headroom | Increase `maximum_capacity.cpu/memory`; check ApplicationState |
| "Lake Formation: User does not have permission" | EMR job role missing LF grant | `aws lakeformation grant-permissions --principal '{DataLakePrincipalIdentifier:<role_arn>}' --resource '{...}' --permissions SELECT` |
| Iceberg writes succeed but Athena can't read | Iceberg version mismatch | EMR 7.12 = Iceberg 1.10. Athena engine v3 supports up to 1.10. Older Athena may fail on newer Iceberg metadata |
| Job costs spike unexpectedly | Over-provisioned executors | Review `spark.executor.instances` vs actual job DAG; Spark UI in EMR Serverless console |
| Glue Catalog deadlock during concurrent writes | Two jobs MERGE into same table | Use Iceberg's optimistic concurrency: retry on `CommitFailedException`; OR serialize writes via SFN |
| Driver OOM on `collect()` | Pulling > 1 GB to driver | Use `df.write` to S3 instead; never `collect()` large results |
| Architecture mismatch (ARM64 vs x86) | JAR built for x86 | Specify `architecture="X86_64"` if using x86-only JARs |

### 7.1 Cost model

| Component | Cost |
|---|---|
| Pre-initialized capacity (idle) | $0.052624 / vCPU-hour + $0.0057785 / GB-hour |
| Active job capacity | Same rate, billed on used resources |
| ARM64 vs x86 | ARM64 ~20% cheaper |
| S3 (read + write) | $0.0004 / 1000 PUT, $0.0000004 / 1000 GET |

For 4-hour daily Iceberg compaction at 100 GB scan: ~$3 / day = $90 / month per pipeline.

---

## 8. Five non-negotiables

1. **Always set `auto_stop_configuration.idle_timeout_minutes`.** Without it, a pre-initialized application bills for warm capacity 24/7. 60 min is a sane default.

2. **Use ARM64 (`architecture="ARM64"`) unless you have a hard x86 dependency.** ~20% cost savings, equivalent performance.

3. **Glue Catalog as Iceberg metastore — non-negotiable.** Don't use Hive Metastore in production. EMR + Athena + Redshift Spectrum + S3 Tables all integrate with Glue Catalog uniformly.

4. **Lake Formation enforcement on EMR jobs requires `lakeformation.GetDataAccess` IAM perm + `AWS_LAKEFORMATION_ENABLED=true` env.** Without both, your job bypasses LF and reads everything.

5. **Iceberg compaction MUST run nightly.** Without compaction, table read perf degrades 5x within a month. Schedule via Step Functions + EventBridge.

---

## 9. References

- `docs/template_params.md` — `EMR_RELEASE_LABEL`, `EMR_ARCHITECTURE`, `EMR_INITIAL_CAPACITY_DRIVER`, `EMR_INITIAL_CAPACITY_EXECUTOR`, `EMR_MAX_CPU`, `EMR_AUTO_STOP_MINUTES`
- AWS docs:
  - [Using Iceberg with EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/using-iceberg.html)
  - [EMR Serverless 7.12.0 release](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/release-version-7120.html)
  - [Working with Iceberg in EMR (Prescriptive Guidance)](https://docs.aws.amazon.com/prescriptive-guidance/latest/apache-iceberg-on-aws/iceberg-emr.html)
  - [Configure Spark on EMR](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-configure.html)
- Related SOPs:
  - `DATA_ICEBERG_S3_TABLES` — managed Iceberg (lower ops; consider before EMR)
  - `DATA_LAKEHOUSE_ICEBERG` — self-managed Iceberg patterns
  - `DATA_GLUE_CATALOG` — metastore for EMR jobs
  - `DATA_LAKE_FORMATION` — LF-TBAC enforcement
  - `DATA_ATHENA` — when EMR is overkill (ad-hoc / DML only)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — EMR Serverless 7.12 (latest LTS) for heavy Spark transforms on Iceberg/Hudi/Delta. Decision tree vs Athena vs Glue ETL. Pre-initialized capacity for warm starts. Iceberg compaction Spark job + nightly SFN trigger. Glue Catalog as universal metastore. Lake Formation enforcement integration. ARM64 cost optimization. 5 non-negotiables. Created to fill F369 audit gap (2026-04-26): EMR Serverless was 0% covered despite being mandatory for any non-Athena Spark work. |
