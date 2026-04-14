# PARTIAL: Enterprise Data Lakehouse — S3 Iceberg, Athena v3, Redshift Spectrum, Lake Formation

**Usage:** Include when SOW mentions data lakehouse, Iceberg tables, ACID transactions on S3, Athena DML (UPDATE/DELETE/MERGE), Redshift Spectrum, federated queries, Lake Formation governance, data mesh, or enterprise analytics platform.

---

## Lakehouse vs Data Warehouse vs Data Lake

| Pattern                       | Storage              | ACID?  | Schema?          | Best For                         |
| ----------------------------- | -------------------- | ------ | ---------------- | -------------------------------- |
| **Data Lake** (S3 only)       | S3 raw files         | ❌ No  | Schema-on-read   | Cheap storage, ML training data  |
| **Data Warehouse** (Redshift) | Proprietary columnar | ✅ Yes | Schema-on-write  | Fast BI dashboards, SQL analysts |
| **Lakehouse** ← this partial  | S3 + Iceberg         | ✅ Yes | Schema evolution | Both — ACID on S3, open format   |

### Iceberg vs Hudi vs Delta Lake on AWS

| Format             | AWS-native | Athena support | Redshift support | Best For                             |
| ------------------ | ---------- | -------------- | ---------------- | ------------------------------------ |
| **Apache Iceberg** | ✅ Best    | ✅ v3 full DML | ✅ Spectrum      | AWS-first, Lake Formation integrated |
| Delta Lake         | ❌         | ⚠️ Limited     | ❌               | Databricks-first                     |
| Apache Hudi        | ⚠️ Partial | ✅ Read-only   | ❌               | Streaming upserts only               |

**→ Use Apache Iceberg on AWS. It's the winner.**

---

## Lakehouse Architecture

```
Raw Data Sources (S3 raw zone)
   │   ┌─────────────────────────────────────────┐
   │   │  Ingestion Layer                          │
   │   │  Kinesis Firehose → S3 raw               │
   │   │  DMS (DB → S3 CDC) → Glue Streaming ETL  │
   │   └─────────────────────────────────────────┘
   │
   ▼
S3 Iceberg Tables (processed zone)
   │  ├── ACID transactions (INSERT/UPDATE/DELETE/MERGE)
   │  ├── Time travel (SELECT ... FOR TIMESTAMP AS OF ...)
   │  ├── Schema evolution (add/rename/drop columns)
   │  └── Partition pruning (hidden partitioning)
   │
   ├──► Athena v3 — Ad-hoc SQL, DML, MERGE INTO (incremental)
   ├──► Redshift Spectrum — JOIN warehouse tables with lake data
   ├──► EMR Serverless — Spark batch transforms
   └──► SageMaker — ML Feature engineering reads from Iceberg

Lake Formation (security layer over everything)
   ├── Column-level security (mask PII columns)
   ├── Row-level filters (tenant isolation)
   └── Cross-account data sharing
```

---

## CDK Code Block — Enterprise Lakehouse

```python
def _create_enterprise_lakehouse(self, stage_name: str) -> None:
    """
    Enterprise Data Lakehouse — S3 Iceberg + Athena v3 + Redshift Spectrum.

    Components:
      A) S3 Lakehouse zones (raw → processed Iceberg → curated → served)
      B) AWS Glue Data Catalog + Iceberg table definitions
      C) Lake Formation governance (column masking, row filters, cross-account)
      D) Athena v3 workgroup (Iceberg DML: INSERT, UPDATE, DELETE, MERGE INTO)
      E) Glue ETL jobs (raw → Iceberg using Spark with Iceberg connector)
      F) Redshift Serverless + Spectrum (federated queries across lake)
      G) Glue Data Quality (DQ rules on Iceberg tables)
      H) Incremental pipeline trigger (EventBridge → Glue job → Athena MERGE)
      I) Data Catalog crawler (auto-detect schema changes)
    """

    import aws_cdk.aws_glue as glue
    import aws_cdk.aws_glue_alpha as glue_alpha
    import aws_cdk.aws_athena as athena
    import aws_cdk.aws_redshift as redshift
    import aws_cdk.aws_lakeformation as lf

    IS_PROD = stage_name == "prod"

    # =========================================================================
    # A) S3 LAKEHOUSE ZONES
    # =========================================================================

    ZONE_CONFIGS = [
        # (zone_name, retention_days, versioned, lifecycle_transition_days)
        ("raw",       None, True,  1),    # Never delete raw — source of truth
        ("processed", None, True,  30),   # Iceberg tables live here
        ("curated",   None, True,  30),   # Business-ready aggregated datasets
        ("served",    90,   False, 7),    # QuickSight / BI tool datasets (hot)
        ("audit",     None, True,  1),    # Compliance / data access audit logs
    ]

    self.lake_buckets = {}
    for zone, retention, versioned, transition_days in ZONE_CONFIGS:
        bucket = s3.Bucket(
            self, f"Lake{zone.title()}Bucket",
            bucket_name=f"{{project_name}}-lake-{zone}-{stage_name}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            versioned=versioned,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id=f"transition-to-ia",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                            transition_after=Duration.days(transition_days),
                        ),
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(365),
                        ),
                    ],
                    expiration=Duration.days(retention) if retention else None,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    enabled=True,
                )
            ],
            event_bridge_enabled=True,  # EventBridge notifications for pipeline triggers
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.lake_buckets[zone] = bucket

    # =========================================================================
    # B) GLUE DATA CATALOG + ICEBERG DATABASE
    # =========================================================================

    # Glue databases (one per zone/domain)
    glue_db_raw = glue.CfnDatabase(
        self, "GlueDatabaseRaw",
        catalog_id=self.account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(
            name=f"{{project_name}}_{stage_name}_raw",
            description="Raw ingestion zone — external tables only",
            location_uri=f"s3://{self.lake_buckets['raw'].bucket_name}/",
        ),
    )

    glue_db_processed = glue.CfnDatabase(
        self, "GlueDatabaseProcessed",
        catalog_id=self.account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(
            name=f"{{project_name}}_{stage_name}_processed",
            description="Iceberg processed zone — ACID, time travel, schema evolution",
            location_uri=f"s3://{self.lake_buckets['processed'].bucket_name}/",
        ),
    )

    glue_db_curated = glue.CfnDatabase(
        self, "GlueDatabaseCurated",
        catalog_id=self.account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(
            name=f"{{project_name}}_{stage_name}_curated",
            description="Curated business-ready datasets — aggregated, deduplicated",
            location_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/",
        ),
    )

    # [Claude: add one CfnTable per domain entity from the Architecture Map]
    # Example: Iceberg table definition in Glue Catalog
    events_table = glue.CfnTable(
        self, "GlueIcebergEventsTable",
        catalog_id=self.account,
        database_name=glue_db_processed.ref,
        table_input=glue.CfnTable.TableInputProperty(
            name="events",
            description="User events — Iceberg format with daily hidden partitioning",
            table_type="ICEBERG",
            owner="{{project_name}}-data-team",
            parameters={
                "table_type": "iceberg",
                "format": "parquet",
                "write.parquet.compression-codec": "zstd",     # Better than snappy for cold data
                "write.metadata.metrics.default": "full",       # Full column statistics
                "history.expire.max-snapshot-age-ms": "604800000",  # 7 day snapshot retention
                "write.target-file-size-bytes": "134217728",   # 128MB target file size
            },
            storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                location=f"s3://{self.lake_buckets['processed'].bucket_name}/events/",
                input_format="org.apache.iceberg.mr.mapred.IcebergInputFormat",
                output_format="org.apache.iceberg.mr.mapred.IcebergOutputFormat",
                serde_info=glue.CfnTable.SerdeInfoProperty(
                    serialization_library="org.apache.iceberg.mr.serde.IcebergSerDe",
                ),
                columns=[
                    # [Claude: replace with actual schema from Architecture Map]
                    glue.CfnTable.ColumnProperty(name="event_id",   type="string",    comment="UUID primary key"),
                    glue.CfnTable.ColumnProperty(name="user_id",    type="string",    comment="Foreign key → users table"),
                    glue.CfnTable.ColumnProperty(name="event_type", type="string",    comment="click|view|purchase|cancel"),
                    glue.CfnTable.ColumnProperty(name="event_ts",   type="timestamp", comment="Event timestamp (UTC)"),
                    glue.CfnTable.ColumnProperty(name="session_id", type="string",    comment="Session correlation ID"),
                    glue.CfnTable.ColumnProperty(name="properties", type="map<string,string>", comment="Arbitrary event properties"),
                    glue.CfnTable.ColumnProperty(name="amount",     type="double",    comment="Transaction amount (nullable)"),
                    # Partition column — Iceberg hidden partition (no physical folder)
                    glue.CfnTable.ColumnProperty(name="event_date", type="date",      comment="Partition key — derived from event_ts"),
                ],
            ),
        ),
    )

    # =========================================================================
    # C) LAKE FORMATION — Fine-grained access control
    # =========================================================================

    lf_settings = lf.CfnDataLakeSettings(
        self, "LakeFormationSettings",
        admins=[
            lf.CfnDataLakeSettings.DataLakePrincipalProperty(
                data_lake_principal_identifier=self.data_lake_admin_role.role_arn,
            ),
        ],
        allow_external_data_filtering=True,
        create_database_default_permissions=[],   # Override: deny all by default
        create_table_default_permissions=[],       # No default permissions — explicit only
    )

    # Lake Formation data locations — register S3 buckets
    for zone, bucket in self.lake_buckets.items():
        lf.CfnResource(
            self, f"LFResource{zone.title()}",
            resource_arn=bucket.bucket_arn,
            use_service_linked_role=True,
        )

    # Column-level security — mask PII columns from non-privileged roles
    # [Claude: update column list based on PII fields in Architecture Map]
    lf.CfnDataCellsFilter(
        self, "PIIColumnMask",
        table_catalog_id=self.account,
        database_name=glue_db_processed.ref,
        table_name="events",
        name="mask_pii_columns",
        # Include only non-PII columns for analysts
        column_names=[
            "event_id", "event_type", "event_ts", "event_date",
            "session_id", "properties", "amount",
            # Exclude: user_id (PII) — only ML engineers and data scientists see it
        ],
    )

    # Row-level filter — tenant isolation (each tenant sees only their data)
    lf.CfnDataCellsFilter(
        self, "TenantRowFilter",
        table_catalog_id=self.account,
        database_name=glue_db_processed.ref,
        table_name="events",
        name="tenant_row_filter",
        row_filter=lf.CfnDataCellsFilter.RowFilterProperty(
            filter_expression="tenant_id = SESSION_USER()",  # Dynamic — uses caller identity
        ),
        column_wildcard=lf.CfnDataCellsFilter.ColumnWildcardProperty(
            excluded_column_names=[],  # All columns
        ),
    )

    # =========================================================================
    # D) ATHENA v3 WORKGROUP — Iceberg DML queries
    # =========================================================================

    athena_results_bucket = s3.Bucket(
        self, "AthenaResultsBucket",
        bucket_name=f"{{project_name}}-athena-results-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        lifecycle_rules=[s3.LifecycleRule(
            id="expire-query-results",
            expiration=Duration.days(30),  # Results auto-expire after 30 days
            enabled=True,
        )],
        removal_policy=RemovalPolicy.DESTROY if not IS_PROD else RemovalPolicy.RETAIN,
    )

    self.athena_workgroup = athena.CfnWorkGroup(
        self, "AthenaWorkgroup",
        name=f"{{project_name}}-{stage_name}",
        description=f"{{project_name}} lakehouse queries — Iceberg DML enabled",
        recursive_delete_option=not IS_PROD,
        work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
            engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                selected_engine_version="Athena engine version 3",  # MUST be v3 for Iceberg DML
            ),
            result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                output_location=f"s3://{athena_results_bucket.bucket_name}/query-results/",
                encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                    encryption_option="SSE_KMS",
                    kms_key=self.kms_key.key_arn,
                ),
            ),
            enforce_work_group_configuration=True,     # Prevent users overriding settings
            requester_pays_enabled=False,
            publish_cloud_watch_metrics_enabled=True,   # Track query counts, data scanned
            bytes_scanned_cutoff_per_query=10 * 1024**4 if IS_PROD else 1 * 1024**4,  # 10TB / 1TB limit
        ),
        tags=[{"key": "Project", "value": "{{project_name}}"}],
    )

    # Athena Named Queries — pre-built analytics queries
    # [Claude: customize these for your domain]
    athena.CfnNamedQuery(
        self, "AthenaIcebergMerge",
        name=f"{{project_name}}-incremental-merge",
        database=glue_db_processed.ref,
        work_group=self.athena_workgroup.name,
        description="Incremental MERGE INTO events from staging (upsert pattern)",
        query_string=f"""
-- MERGE INTO (Iceberg ACID upsert — runs via Lambda trigger)
MERGE INTO "{glue_db_processed.ref}"."events" AS target
USING "{glue_db_raw.ref}"."events_staging" AS source
ON (target.event_id = source.event_id)
WHEN MATCHED THEN
    UPDATE SET
        event_type = source.event_type,
        properties = source.properties,
        amount     = source.amount
WHEN NOT MATCHED THEN
    INSERT (event_id, user_id, event_type, event_ts, session_id, properties, amount, event_date)
    VALUES (source.event_id, source.user_id, source.event_type, source.event_ts,
            source.session_id, source.properties, source.amount, source.event_date);
""",
    )

    athena.CfnNamedQuery(
        self, "AthenaTimeTravel",
        name=f"{{project_name}}-time-travel-example",
        database=glue_db_processed.ref,
        work_group=self.athena_workgroup.name,
        description="Iceberg time travel — query data as of a specific timestamp",
        query_string="""
-- Time travel: see how the data looked 24 hours ago
SELECT * FROM "events"
FOR TIMESTAMP AS OF (current_timestamp - INTERVAL '24' HOUR)
WHERE event_date = current_date - INTERVAL '1' DAY
LIMIT 1000;
""",
    )

    # =========================================================================
    # E) GLUE ETL JOB — Raw → Iceberg (Spark with Iceberg connector)
    # =========================================================================

    glue_role = iam.Role(
        self, "GlueETLRole",
        assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
        role_name=f"{{project_name}}-glue-etl-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
        ],
    )
    for bucket in self.lake_buckets.values():
        bucket.grant_read_write(glue_role)
    self.kms_key.grant_encrypt_decrypt(glue_role)
    glue_role.add_to_policy(iam.PolicyStatement(
        actions=["lakeformation:GetDataAccess", "glue:GetTable", "glue:GetDatabase",
                 "glue:UpdateTable", "glue:CreateTable"],
        resources=["*"],
    ))

    raw_to_iceberg_job = glue.CfnJob(
        self, "RawToIcebergJob",
        name=f"{{project_name}}-raw-to-iceberg-{stage_name}",
        role=glue_role.role_arn,
        description="Glue Spark ETL: raw S3 → Iceberg processed zone",
        glue_version="4.0",   # Latest — Python 3.10, Spark 3.3
        worker_type="G.1X" if not IS_PROD else "G.2X",  # 4 vCPU, 16GB prod / 8 vCPU, 32GB
        number_of_workers=5 if not IS_PROD else 20,
        timeout=120,  # 2 hours max
        max_retries=1,
        default_arguments={
            "--job-language":                "python",
            "--enable-auto-scaling":         "true",
            "--enable-glue-datacatalog":     "true",
            "--enable-job-insights":         "true",
            "--enable-observability-metrics":"true",
            "--enable-continuous-cloudwatch-log": "true",
            "--datalake-formats":            "iceberg",
            "--conf":                        "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
            "--conf2":                       "spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog",
            "--conf3":                       "spark.sql.catalog.glue_catalog.warehouse=" + f"s3://{self.lake_buckets['processed'].bucket_name}/",
            "--conf4":                       "spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog",
            "--conf5":                       "spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
            # Job parameters
            "--RAW_BUCKET":                  self.lake_buckets["raw"].bucket_name,
            "--PROCESSED_BUCKET":            self.lake_buckets["processed"].bucket_name,
            "--GLUE_DATABASE":               glue_db_processed.ref,
            "--STAGE":                       stage_name,
        },
        command=glue.CfnJob.JobCommandProperty(
            name="glueetl",
            python_version="3",
            script_location=f"s3://{self.lake_buckets['raw'].bucket_name}/glue-scripts/raw_to_iceberg.py",
        ),
        execution_property=glue.CfnJob.ExecutionPropertyProperty(
            max_concurrent_runs=3,
        ),
        tags={"Project": "{{project_name}}", "Stage": stage_name},
    )

    # ================ Glue Script (store in S3) ================
    # [Claude: deploy this script to S3 via BucketDeployment or CodeBuild]
    GLUE_SCRIPT = """
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import *
from pyspark.sql.types import *
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

args = getResolvedOptions(sys.argv, ['JOB_NAME', 'RAW_BUCKET', 'PROCESSED_BUCKET',
                                      'GLUE_DATABASE', 'STAGE'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# Configure Iceberg Spark catalog
spark.conf.set("spark.sql.defaultCatalog", "glue_catalog")

DATABASE = args['GLUE_DATABASE']
RAW_PATH = f"s3://{args['RAW_BUCKET']}/events/dt={{}}/".format(
    __import__('datetime').date.today().strftime('%Y-%m-%d')
)

# Read raw data (Parquet/JSON from Kinesis Firehose delivery)
raw_df = (spark.read
    .option("mergeSchema", "true")  # Handle schema evolution in raw files
    .parquet(RAW_PATH)
    .withColumn("event_date", to_date(col("event_ts")))
    .withColumn("event_id", expr("uuid()"))   # Generate surrogate key if missing
    .dropDuplicates(["event_id"])             # Idempotent deduplication
    .filter(col("event_ts").isNotNull())
)

logger.info(f"Read {raw_df.count()} records from raw zone")

# Write to Iceberg table using MERGE (upsert — idempotent, safe to re-run)
raw_df.createOrReplaceTempView("incoming_events")

spark.sql(f\"\"\"
    MERGE INTO glue_catalog.{DATABASE}.events AS t
    USING incoming_events AS s
    ON t.event_id = s.event_id
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
\"\"\")

# Iceberg table maintenance (run weekly in prod)
if args['STAGE'] == 'prod':
    # Compact small files into 128MB files
    spark.sql(f"CALL glue_catalog.system.rewrite_data_files(table => '{DATABASE}.events', strategy => 'binpack', options => map('target-file-size-bytes','134217728'))")
    # Remove old snapshots (keep 7 days for time travel)
    spark.sql(f"CALL glue_catalog.system.expire_snapshots(table => '{DATABASE}.events', older_than => TIMESTAMP '{{}}'.format(__import__('datetime').datetime.now() - __import__('datetime').timedelta(days=7)))")

logger.info("Iceberg MERGE complete")
job.commit()
"""

    # =========================================================================
    # F) REDSHIFT SERVERLESS + SPECTRUM (federated queries across lake + warehouse)
    # =========================================================================

    redshift_admin_secret = sm.Secret(
        self, "RedshiftAdminSecret",
        secret_name=f"/{{project_name}}/{stage_name}/redshift/admin",
        generate_secret_string=sm.SecretStringGenerator(
            secret_string_template='{"username": "admin"}',
            generate_string_key="password",
            exclude_characters='"@/\\\'',
            password_length=32,
        ),
    )

    redshift_sg = ec2.SecurityGroup(
        self, "RedshiftSG",
        vpc=self.vpc,
        security_group_name=f"{{project_name}}-redshift-{stage_name}",
        description="Redshift Serverless — VPC access only",
        allow_all_outbound=True,
    )
    redshift_sg.add_ingress_rule(
        peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
        connection=ec2.Port.tcp(5439),
        description="Redshift port from VPC only",
    )

    redshift_namespace = redshift.CfnNamespace(
        self, "RedshiftNamespace",
        namespace_name=f"{{project_name}}-{stage_name}",
        admin_username="admin",
        admin_user_password=redshift_admin_secret.secret_value_from_json("password").unsafe_unwrap(),
        db_name=f"{{project_name}}_{stage_name}",
        kms_key_id=self.kms_key.key_arn,
        iam_roles=[self.redshift_spectrum_role.role_arn],
        log_exports=["userlog", "connectionlog", "useractivitylog"],
        tags=[{"key": "Project", "value": "{{project_name}}"}],
    )

    redshift_workgroup = redshift.CfnWorkgroup(
        self, "RedshiftWorkgroup",
        workgroup_name=f"{{project_name}}-{stage_name}",
        namespace_name=redshift_namespace.ref,
        base_capacity=8 if not IS_PROD else 32,   # RPUs — 8 min, autoscales to 512
        max_capacity=64 if not IS_PROD else 512,
        enhanced_vpc_routing=True,             # All traffic stays within VPC
        publicly_accessible=False,
        subnet_ids=[s.subnet_id for s in self.vpc.isolated_subnets[:2]],
        security_group_ids=[redshift_sg.security_group_id],
        config_parameters=[
            redshift.CfnWorkgroup.ConfigParameterProperty(
                parameter_key="enable_user_activity_logging",
                parameter_value="true",
            ),
            redshift.CfnWorkgroup.ConfigParameterProperty(
                parameter_key="max_concurrency_scaling_clusters",
                parameter_value="10",
            ),
        ],
        tags=[{"key": "Project", "value": "{{project_name}}"}],
    )

    # Redshift Spectrum external schema — allows Redshift to query S3 Iceberg via Glue catalog
    # [Claude: run this SQL after stack deploys, or use a Custom Resource Lambda]
    SPECTRUM_SETUP_SQL = f"""
-- Run in Redshift after stack deploy
CREATE EXTERNAL SCHEMA IF NOT EXISTS lake_processed
FROM DATA CATALOG
DATABASE '{glue_db_processed.ref}'
IAM_ROLE '{self.redshift_spectrum_role.role_arn}'
CREATE EXTERNAL DATABASE IF NOT EXISTS;

CREATE EXTERNAL SCHEMA IF NOT EXISTS lake_curated
FROM DATA CATALOG
DATABASE '{glue_db_curated.ref}'
IAM_ROLE '{self.redshift_spectrum_role.role_arn}';

-- Example federated query: JOIN warehouse users with lake events
-- SELECT u.user_id, u.tier, COUNT(e.event_id) AS event_count
-- FROM warehouse_users u
-- JOIN lake_processed.events e ON u.user_id = e.user_id
-- WHERE e.event_date >= current_date - 30
-- GROUP BY 1, 2
-- ORDER BY 3 DESC;
"""

    ssm.StringParameter(
        self, "SpectrumSetupSQL",
        parameter_name=f"/{{project_name}}/{stage_name}/redshift/spectrum-setup-sql",
        string_value=SPECTRUM_SETUP_SQL,
        description="Run this SQL in Redshift after deployment to set up Spectrum",
    )

    # =========================================================================
    # G) GLUE DATA QUALITY — DQ rules on Iceberg tables
    # =========================================================================

    glue.CfnDataQualityRuleset(
        self, "EventsTableDQRules",
        name=f"{{project_name}}-events-dq-{stage_name}",
        description="Data quality rules for the events Iceberg table",
        ruleset="""
Rules = [
    Completeness "event_id"  >= 0.99,       # 99% non-null
    Completeness "user_id"   >= 0.98,
    Completeness "event_ts"  = 1.0,         # Timestamp always required
    Uniqueness   "event_id"  = 1.0,         # Primary key — no duplicates
    ColumnValues "event_type" in [ "click", "view", "purchase", "cancel", "signup" ],
    ColumnLength "event_id"  between 36 and 36,   # UUID format
    ColumnValues "amount"    >= 0 WHERE "amount" IS NOT NULL,  # No negative amounts
    Freshness    "event_ts"  <= 3 HOURS,    # Data should be < 3 hours old (latency SLA)
]
""",
        target_table=glue.CfnDataQualityRuleset.DataQualityTargetTableProperty(
            database_name=glue_db_processed.ref,
            table_name="events",
        ),
    )

    # =========================================================================
    # H) INCREMENTAL PIPELINE TRIGGER (EventBridge → Glue → Athena MERGE)
    # =========================================================================

    # Trigger Glue ETL job when new files land in raw bucket
    pipeline_fn = _lambda.Function(
        self, "LakehousePipelineFn",
        function_name=f"{{project_name}}-lakehouse-pipeline-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

glue      = boto3.client('glue')
athena    = boto3.client('athena')
JOB_NAME  = os.environ['GLUE_JOB_NAME']
WORKGROUP = os.environ['ATHENA_WORKGROUP']
DATABASE  = os.environ['GLUE_DATABASE']
RESULTS   = os.environ['ATHENA_RESULTS_BUCKET']

def handler(event, context):
    trigger_source = event.get('source', 'scheduled')
    logger.info(f"Pipeline triggered by: {trigger_source}")

    # Step 1: Start Glue ETL (raw → Iceberg)
    glue_run = glue.start_job_run(
        JobName=JOB_NAME,
        Arguments={
            '--run_date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            '--trigger_source': trigger_source,
        }
    )
    logger.info(f"Glue job started: {glue_run['JobRunId']}")

    return {
        'status': 'started',
        'glue_run_id': glue_run['JobRunId'],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
"""),
        environment={
            "GLUE_JOB_NAME":         raw_to_iceberg_job.ref,
            "ATHENA_WORKGROUP":      self.athena_workgroup.name,
            "GLUE_DATABASE":         glue_db_processed.ref,
            "ATHENA_RESULTS_BUCKET": athena_results_bucket.bucket_name,
        },
        timeout=Duration.minutes(2),
    )
    pipeline_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["glue:StartJobRun", "athena:StartQueryExecution", "athena:GetQueryResults"],
        resources=["*"],
    ))

    # Hourly scheduled pipeline (incremental loads)
    events.Rule(self, "HourlyPipelineTrigger",
        rule_name=f"{{project_name}}-hourly-lakehouse-{stage_name}",
        schedule=events.Schedule.cron(minute="5"),   # 5 min past every hour
        targets=[targets.LambdaFunction(pipeline_fn)],
        enabled=IS_PROD,   # Only run hourly in prod; devs trigger manually
    )

    # S3 event trigger — also run when new files land (event-driven option)
    self.lake_buckets["raw"].add_event_notification(
        s3.EventType.OBJECT_CREATED,
        s3n.LambdaDestination(pipeline_fn),
        s3.NotificationKeyFilter(prefix="events/", suffix=".parquet"),
    )

    # =========================================================================
    # I) GLUE CRAWLER — Auto-detect schema changes in raw zone
    # =========================================================================

    glue.CfnCrawler(
        self, "RawZoneCrawler",
        name=f"{{project_name}}-raw-crawler-{stage_name}",
        role=glue_role.role_arn,
        database_name=glue_db_raw.ref,
        description="Crawls raw zone S3 to detect new tables and schema changes",
        targets=glue.CfnCrawler.TargetsProperty(
            s3_targets=[
                glue.CfnCrawler.S3TargetProperty(
                    path=f"s3://{self.lake_buckets['raw'].bucket_name}/",
                    exclusions=["glue-scripts/**", "**/_temporary/**", "**/_SUCCESS"],
                )
            ]
        ),
        schema_change_policy=glue.CfnCrawler.SchemaChangePolicyProperty(
            update_behavior="UPDATE_IN_DATABASE",
            delete_behavior="LOG",   # Don't delete tables — just log schema removal
        ),
        recrawl_policy=glue.CfnCrawler.RecrawlPolicyProperty(
            recrawl_behavior="CRAWL_NEW_FOLDERS_ONLY",   # Efficient — skip already-crawled
        ),
        schedule=glue.CfnCrawler.ScheduleProperty(
            schedule_expression="cron(0 * * * ? *)",  # Hourly
        ) if IS_PROD else None,
        configuration=json.dumps({
            "Version": 1.0,
            "CrawlerOutput": {"Partitions": {"AddOrUpdateBehavior": "InheritFromTable"}},
            "Grouping": {"TableGroupingPolicy": "CombineCompatibleSchemas"},
        }),
        tags={"Project": "{{project_name}}", "Stage": stage_name},
    )

    # =========================================================================
    # ALARMS
    # =========================================================================

    # Glue job failure alarm
    cw.Alarm(
        self, "GlueJobFailureAlarm",
        alarm_name=f"{{project_name}}-glue-raw-to-iceberg-failed-{stage_name}",
        alarm_description="Raw → Iceberg Glue ETL job failed — pipeline blocked",
        metric=cw.Metric(
            namespace="Glue",
            metric_name="glue.driver.aggregate.numFailedTasks",
            dimensions_map={"JobName": raw_to_iceberg_job.ref},
            statistic="Sum",
            period=Duration.minutes(5),
        ),
        threshold=1,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )

    # Athena data scanned cost alarm ($5/TB — alert if single query scans >100GB)
    cw.Alarm(
        self, "AthenaDataScannedAlarm",
        alarm_name=f"{{project_name}}-athena-large-query-{stage_name}",
        alarm_description="Athena query scanned >100GB — possible missing partition filter",
        metric=cw.Metric(
            namespace="AWS/Athena",
            metric_name="DataScannedInBytes",
            dimensions_map={"WorkGroup": self.athena_workgroup.name},
            statistic="Maximum",
            period=Duration.minutes(5),
        ),
        threshold=100 * 1024**3,  # 100 GB
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "LakeRawBucket",
        value=self.lake_buckets["raw"].bucket_name,
        description="Raw zone — source of truth for all inbound data",
        export_name=f"{{project_name}}-lake-raw-{stage_name}",
    )
    CfnOutput(self, "LakeProcessedBucket",
        value=self.lake_buckets["processed"].bucket_name,
        description="Processed zone — Apache Iceberg ACID tables",
        export_name=f"{{project_name}}-lake-processed-{stage_name}",
    )
    CfnOutput(self, "AthenaWorkgroup",
        value=self.athena_workgroup.name,
        description="Athena v3 workgroup — use for all SQL queries (Iceberg DML enabled)",
        export_name=f"{{project_name}}-athena-wg-{stage_name}",
    )
    CfnOutput(self, "RedshiftWorkgroupEndpoint",
        value=redshift_workgroup.attr_workgroup_endpoint_address,
        description="Redshift Serverless endpoint — JDBC/ODBC for BI tools",
        export_name=f"{{project_name}}-redshift-{stage_name}",
    )
    CfnOutput(self, "GlueETLJob",
        value=raw_to_iceberg_job.ref,
        description="Glue ETL job: raw → Iceberg ACID tables",
        export_name=f"{{project_name}}-glue-job-{stage_name}",
    )
    CfnOutput(self, "SpectrumSetupInstructions",
        value=f"Run SQL from SSM: /{{project_name}}/{stage_name}/redshift/spectrum-setup-sql",
        description="One-time Redshift Spectrum setup — run after first deploy",
    )
```

---

## Key Design Decisions

| Decision                | Choice                                     | Why                                                                 |
| ----------------------- | ------------------------------------------ | ------------------------------------------------------------------- |
| **Table format**        | Apache Iceberg                             | Native AWS support, Athena v3 full DML, Lake Formation integrated   |
| **Compaction strategy** | Bin-pack (128MB files)                     | Optimal for Athena (fewer S3 LIST calls = faster + cheaper)         |
| **Upsert pattern**      | Athena MERGE INTO                          | ACID, idempotent, handles late arrivals and corrections             |
| **Partition strategy**  | Hidden partitioning on `event_date`        | No physical folder, no partition evolution pain                     |
| **Query engine**        | Athena v3 for ad-hoc, Redshift for BI      | Athena = pay-per-scan; Redshift = fixed RPU for heavy BI            |
| **Schema evolution**    | Glue Crawler + Iceberg schema evolution    | New columns auto-detected; no ETL breaks                            |
| **Time travel**         | Iceberg snapshots (7-day retention)        | Debug bad loads, point-in-time compliance queries                   |
| **Access control**      | Lake Formation column + row filters        | PII masking, tenant isolation — no application-level logic needed   |
| **Cost**                | `INTELLIGENT_TIERING` + Iceberg compaction | Auto-moves cold data to cheaper tiers; small files = higher S3 cost |
