# SOP — Enterprise Data Lakehouse (S3 Iceberg · Athena v3 · Redshift Spectrum · Lake Formation)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Apache Iceberg · Glue 4.0 · Athena engine version 3 · Redshift Serverless · Lake Formation

---

## 1. Purpose

Provision an enterprise data lakehouse:

- **Zoned S3 storage** — raw / processed (Iceberg) / curated / served / audit — with lifecycle transitions and intelligent tiering.
- **Apache Iceberg tables** via Glue Data Catalog — ACID (INSERT/UPDATE/DELETE/MERGE), time travel, schema evolution, hidden partitioning.
- **Athena v3 workgroup** — Iceberg DML-enabled, result encryption, cost cutoff.
- **Redshift Serverless + Spectrum** — federated BI queries joining warehouse tables with lake Iceberg data via external schemas.
- **Lake Formation governance** (column masking, row filters) + **Glue Data Quality rules** + **Glue crawlers** + **EventBridge-triggered incremental pipeline** (Lambda → Glue ETL → Athena MERGE).

Include when SOW signals: "data lakehouse", "Iceberg", "ACID on S3", "Athena DML / MERGE", "Redshift Spectrum", "federated queries", "Lake Formation", "data mesh", "enterprise analytics platform".

### Lakehouse vs Data Warehouse vs Data Lake

| Pattern                       | Storage              | ACID?  | Schema?          | Best for                         |
|-------------------------------|----------------------|--------|------------------|----------------------------------|
| **Data Lake** (S3 only)       | S3 raw files         | No     | Schema-on-read   | Cheap storage, ML training       |
| **Data Warehouse** (Redshift) | Proprietary columnar | Yes    | Schema-on-write  | Fast BI dashboards               |
| **Lakehouse** — this SOP      | S3 + Iceberg         | Yes    | Schema evolution | Both — ACID on S3, open format   |

### Iceberg vs Hudi vs Delta Lake on AWS

| Format             | AWS-native | Athena support   | Redshift support | Best for                             |
|--------------------|------------|------------------|------------------|--------------------------------------|
| **Apache Iceberg** | Best       | v3 full DML      | Spectrum         | AWS-first, Lake Formation integrated |
| Delta Lake         | No         | Limited          | No               | Databricks-first                     |
| Apache Hudi        | Partial    | Read-only        | No               | Streaming upserts only               |

**Decision: use Apache Iceberg on AWS.** The CDK surface is `aws_glue.CfnDatabase` + `aws_glue.CfnTable(table_type="ICEBERG")` + S3 bucket; "Iceberg" is expressed through Glue ETL job arguments (`--datalake-formats=iceberg`) and the Athena workgroup's `selected_engine_version="Athena engine version 3"`.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC / data-only stack where zones + catalog + Athena + Redshift + Glue ETL all live together | **§3 Monolith** |
| `LakehouseStack` separate from `ComputeStack` / `ComplianceStack`, consumed by ML training / BI / application Lambdas in other stacks | **§4 Micro-Stack** |

**Why the split matters.** Cross-stack grants that will **cycle** once you split:

- `lake_bucket.grant_read_write(glue_role)` — if the Glue role is in a different stack, the bucket policy gets mutated with the role ARN, and CloudFormation rejects the bidirectional export.
- `kms_key.grant_encrypt_decrypt(glue_role)` — same mechanism; CDK edits the KMS key policy with the external role ARN.
- `bucket.grant_read(ml_training_role)` — any downstream Lambda / SageMaker / EMR consumer that lives outside LakehouseStack will trigger the same cycle.
- Lake Formation `CfnPrincipalPermissions` that reference external role ARNs do not cycle (they live in LF's catalog) but still require the ARN to be resolvable via SSM or string, not L1-on-L1 refs.

The Micro-Stack variant fixes this by: (a) owning the CMK, lake buckets, Glue databases, Athena workgroup and Redshift workgroup all inside `LakehouseStack`; (b) publishing names + ARNs via `ssm.StringParameter`; (c) consumer stacks grant identity-side only, reading those SSM values with `ssm.StringParameter.value_for_string_parameter`.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` class holds VPC + data + Glue + Athena + Redshift + alarms together.

### 3.1 Architecture

```
Raw Data Sources (S3 raw zone)
    ┌─────────────────────────────────────────────┐
    │  Ingestion Layer                            │
    │  Kinesis Firehose → S3 raw                  │
    │  DMS (DB → S3 CDC) → Glue Streaming ETL     │
    └─────────────────────────────────────────────┘
                     │
                     ▼
         S3 Iceberg Tables (processed zone)
             ├── ACID (INSERT/UPDATE/DELETE/MERGE)
             ├── Time travel (FOR TIMESTAMP AS OF ...)
             ├── Schema evolution
             └── Hidden partitioning

             ├──► Athena v3   — Ad-hoc SQL, DML, MERGE INTO (incremental)
             ├──► Redshift Spectrum — JOIN warehouse with lake
             ├──► EMR Serverless — Spark batch
             └──► SageMaker  — ML feature engineering reads Iceberg

         Lake Formation (security layer)
             ├── Column-level security (mask PII)
             ├── Row-level filters (tenant isolation)
             └── Cross-account data sharing
```

### 3.2 CDK — `_create_enterprise_lakehouse()` method body

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

    import json
    from aws_cdk import (
        Duration, RemovalPolicy, CfnOutput,
        aws_s3 as s3,
        aws_s3_notifications as s3n,
        aws_kms as kms,
        aws_ec2 as ec2,
        aws_iam as iam,
        aws_lambda as _lambda,
        aws_events as events,
        aws_events_targets as targets,
        aws_secretsmanager as sm,
        aws_ssm as ssm,
        aws_cloudwatch as cw,
        aws_cloudwatch_actions as cw_actions,
    )
    import aws_cdk.aws_glue as glue
    import aws_cdk.aws_athena as athena
    import aws_cdk.aws_redshiftserverless as redshift
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
            owner="{project_name}-data-team",
            parameters={
                "table_type": "iceberg",
                "format": "parquet",
                "write.parquet.compression-codec": "zstd",       # Better than snappy for cold data
                "write.metadata.metrics.default": "full",         # Full column statistics
                "history.expire.max-snapshot-age-ms": "604800000",  # 7 day snapshot retention
                "write.target-file-size-bytes": "134217728",     # 128MB target file size
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
        tags=[{"key": "Project", "value": "{project_name}"}],
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
        tags={"Project": "{project_name}", "Stage": stage_name},
    )

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
        tags=[{"key": "Project", "value": "{project_name}"}],
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
        tags=[{"key": "Project", "value": "{project_name}"}],
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
    Completeness "event_id"  >= 0.99,
    Completeness "user_id"   >= 0.98,
    Completeness "event_ts"  = 1.0,
    Uniqueness   "event_id"  = 1.0,
    ColumnValues "event_type" in [ "click", "view", "purchase", "cancel", "signup" ],
    ColumnLength "event_id"  between 36 and 36,
    ColumnValues "amount"    >= 0 WHERE "amount" IS NOT NULL,
    Freshness    "event_ts"  <= 3 HOURS,
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

    # Lambda trigger lives in a local asset; handler code shown in §3.3.
    pipeline_fn = _lambda.Function(
        self, "LakehousePipelineFn",
        function_name=f"{{project_name}}-lakehouse-pipeline-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/lakehouse_pipeline"),
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
            delete_behavior="LOG",
        ),
        recrawl_policy=glue.CfnCrawler.RecrawlPolicyProperty(
            recrawl_behavior="CRAWL_NEW_FOLDERS_ONLY",
        ),
        schedule=glue.CfnCrawler.ScheduleProperty(
            schedule_expression="cron(0 * * * ? *)",  # Hourly
        ) if IS_PROD else None,
        configuration=json.dumps({
            "Version": 1.0,
            "CrawlerOutput": {"Partitions": {"AddOrUpdateBehavior": "InheritFromTable"}},
            "Grouping": {"TableGroupingPolicy": "CombineCompatibleSchemas"},
        }),
        tags={"Project": "{project_name}", "Stage": stage_name},
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

### 3.3 Pipeline handler (`lambda/lakehouse_pipeline/index.py`)

```python
"""Incremental lakehouse pipeline — triggers Glue ETL raw → Iceberg."""
import os, logging
from datetime import datetime, timezone
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

glue_client   = boto3.client('glue')
athena_client = boto3.client('athena')

JOB_NAME  = os.environ['GLUE_JOB_NAME']
WORKGROUP = os.environ['ATHENA_WORKGROUP']
DATABASE  = os.environ['GLUE_DATABASE']
RESULTS   = os.environ['ATHENA_RESULTS_BUCKET']


def handler(event, context):
    trigger_source = event.get('source', 'scheduled')
    logger.info(f"Pipeline triggered by: {trigger_source}")

    # Step 1: Start Glue ETL (raw → Iceberg)
    glue_run = glue_client.start_job_run(
        JobName=JOB_NAME,
        Arguments={
            '--run_date':       datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            '--trigger_source': trigger_source,
        },
    )
    logger.info(f"Glue job started: {glue_run['JobRunId']}")

    return {
        'status':      'started',
        'glue_run_id': glue_run['JobRunId'],
        'timestamp':   datetime.now(timezone.utc).isoformat(),
    }
```

### 3.4 Glue ETL script (`glue-scripts/raw_to_iceberg.py`)

Deploy to `s3://<raw-bucket>/glue-scripts/raw_to_iceberg.py` via `BucketDeployment` or CodeBuild.

```python
# [Claude: deploy this script to S3 via BucketDeployment or CodeBuild]
import sys
import datetime as dt
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
RAW_PATH = f"s3://{args['RAW_BUCKET']}/events/dt={dt.date.today().strftime('%Y-%m-%d')}/"

# Read raw data (Parquet/JSON from Kinesis Firehose delivery)
raw_df = (spark.read
    .option("mergeSchema", "true")            # Handle schema evolution in raw files
    .parquet(RAW_PATH)
    .withColumn("event_date", to_date(col("event_ts")))
    .withColumn("event_id", expr("uuid()"))   # Generate surrogate key if missing
    .dropDuplicates(["event_id"])             # Idempotent deduplication
    .filter(col("event_ts").isNotNull())
)

logger.info(f"Read {raw_df.count()} records from raw zone")

# Write to Iceberg table using MERGE (upsert — idempotent, safe to re-run)
raw_df.createOrReplaceTempView("incoming_events")

spark.sql(f"""
    MERGE INTO glue_catalog.{DATABASE}.events AS t
    USING incoming_events AS s
    ON t.event_id = s.event_id
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

# Iceberg table maintenance (run weekly in prod)
if args['STAGE'] == 'prod':
    # Compact small files into 128MB files
    spark.sql(f"CALL glue_catalog.system.rewrite_data_files("
              f"table => '{DATABASE}.events', strategy => 'binpack', "
              f"options => map('target-file-size-bytes','134217728'))")
    # Remove old snapshots (keep 7 days for time travel)
    older_than = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    spark.sql(f"CALL glue_catalog.system.expire_snapshots("
              f"table => '{DATABASE}.events', older_than => TIMESTAMP '{older_than}')")

logger.info("Iceberg MERGE complete")
job.commit()
```

### 3.5 Key Design Decisions

| Decision                | Choice                                     | Why                                                                 |
|-------------------------|--------------------------------------------|---------------------------------------------------------------------|
| **Table format**        | Apache Iceberg                             | Native AWS support, Athena v3 full DML, Lake Formation integrated   |
| **Compaction strategy** | Bin-pack (128MB files)                     | Optimal for Athena (fewer S3 LIST calls = faster + cheaper)         |
| **Upsert pattern**      | Athena / Spark `MERGE INTO`                | ACID, idempotent, handles late arrivals and corrections             |
| **Partition strategy**  | Hidden partitioning on `event_date`        | No physical folder, no partition evolution pain                     |
| **Query engine**        | Athena v3 for ad-hoc, Redshift for BI      | Athena = pay-per-scan; Redshift = fixed RPU for heavy BI            |
| **Schema evolution**    | Glue Crawler + Iceberg schema evolution    | New columns auto-detected; no ETL breaks                            |
| **Time travel**         | Iceberg snapshots (7-day retention)        | Debug bad loads, point-in-time compliance queries                   |
| **Access control**      | Lake Formation column + row filters        | PII masking, tenant isolation — no application-level logic needed   |
| **Cost**                | `INTELLIGENT_TIERING` + Iceberg compaction | Auto-moves cold data to cheaper tiers; small files = higher S3 cost |

### 3.6 Monolith gotchas

- **Athena engine version 3 is required for Iceberg DML.** Version 2 only supports `SELECT` on Iceberg; `UPDATE / DELETE / MERGE INTO` silently fail with a non-obvious parse error. Always set `selected_engine_version="Athena engine version 3"`.
- **`table_type="ICEBERG"` + storage descriptor** — Glue requires both the parameter `table_type=iceberg` and the Iceberg SerDe. Omitting either causes Athena to treat the table as plain Parquet (DML silently unavailable).
- **`event_bridge_enabled=True`** is a **custom resource** (`Custom::S3BucketNotifications`) — tests asserting on the bucket's `NotificationConfiguration` property will miss it.
- **`redshift.CfnNamespace.admin_user_password`** accepts a plain string — cdk-nag flags the `unsafe_unwrap()` pattern. Preferred: set `manage_admin_password=True` so Redshift manages the secret itself.
- **Glue 4.0 worker sizing** — `G.1X` = 4 vCPU/16GB; `G.2X` = 8 vCPU/32GB. Small tables on `G.2X` waste money; very wide tables on `G.1X` OOM.
- **Glue job `--datalake-formats=iceberg`** must be set explicitly; without it Spark cannot resolve the Iceberg catalog, even if the rest of the Iceberg configs are present.
- **Redshift Spectrum `CREATE EXTERNAL SCHEMA`** must be run inside Redshift after deploy — CDK cannot do this declaratively. Either hand-run the SQL from SSM, or add a `CustomResource` with `AwsSdkCall` to `redshift-data:ExecuteStatement`.

---

## 4. Micro-Stack Variant

**Use when:** a dedicated `LakehouseStack` owns the zones + catalog + Athena + Redshift + Glue ETL + pipeline Lambda, and consumer stacks (application Lambdas, ML training, BI reports) live elsewhere and read from Iceberg or write to the raw zone.

### 4.1 The five non-negotiables

Memorize these (reference: `LAYER_BACKEND_LAMBDA` §4.1). Every cross-stack lakehouse failure reduces to one of them.

1. **Anchor asset paths to `__file__`, never relative-to-CWD.** Both the pipeline Lambda asset and the Glue script asset (`BucketDeployment` source) must use `Path(__file__).resolve().parents[N]`.
2. **Never use `X.grant_*(role)` on a cross-stack resource X.** No `bucket.grant_read_write(external_glue_role)`; no `key.grant_encrypt_decrypt(external_role)`. Always identity-side `PolicyStatement` on the role.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** Not relevant for this SOP directly, but if the pipeline ever switches to SQS-driven, use L1 `CfnRule` with a raw target dict.
4. **Never own a bucket in one stack and attach its CloudFront OAC in another.** Lakehouse buckets are private; no CDN.
5. **Never set `encryption_key=ext_key` where `ext_key` came from another stack.** The lakehouse CMK is **owned by `LakehouseStack`** (local reference). Consumers grant identity-side `kms:Encrypt` / `kms:Decrypt` / `kms:GenerateDataKey` on its ARN string published via SSM.

Also: `iam:PassRole` with the `iam:PassedToService` Condition (`glue.amazonaws.com`) anywhere a role ARN is handed to Glue or Redshift; and every role in `LakehouseStack` carries the shared `permission_boundary`.

### 4.2 `LakehouseStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_kms as kms,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_events as events,
    aws_events_targets as targets,
    aws_secretsmanager as sm,
    aws_ssm as ssm,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
)
import aws_cdk.aws_glue as glue
import aws_cdk.aws_athena as athena
import aws_cdk.aws_redshiftserverless as redshift
import aws_cdk.aws_lakeformation as lf
from constructs import Construct
import json

# stacks/lakehouse_stack.py  ->  stacks/  ->  cdk/  ->  infrastructure/  ->  <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"
_GLUE_SCRIPTS_ROOT: Path = Path(__file__).resolve().parents[3] / "glue_scripts"


class LakehouseStack(cdk.Stack):
    """Owns S3 zones + Glue catalog + Iceberg tables + Athena v3 workgroup +
    Redshift Serverless + Glue ETL + pipeline Lambda + alarms.

    Cross-stack values exposed via SSM parameters so consumer stacks can read
    without creating CloudFormation import/export cycles.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        alert_topic_arn_ssm: str,
        data_lake_admin_role_arn_ssm: str,
        redshift_spectrum_role_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-lakehouse-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk", "Layer": "Lakehouse"}.items():
            cdk.Tags.of(self).add(k, v)

        IS_PROD = stage_name == "prod"

        # --- Cross-stack inputs read from SSM (no import/export cycles) -----
        alert_topic = sns.Topic.from_topic_arn(self, "AlertTopic",
            ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm),
        )
        data_lake_admin_role_arn = ssm.StringParameter.value_for_string_parameter(
            self, data_lake_admin_role_arn_ssm,
        )
        redshift_spectrum_role_arn = ssm.StringParameter.value_for_string_parameter(
            self, redshift_spectrum_role_arn_ssm,
        )

        # --- Local CMK (honors 5th non-negotiable) --------------------------
        cmk = kms.Key(self, "LakehouseKey",
            alias=f"alias/{{project_name}}-lakehouse-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # =================================================================
        # A) S3 lakehouse zones (owned here)
        # =================================================================
        ZONE_CONFIGS = [
            ("raw",       None, True,  1),
            ("processed", None, True,  30),
            ("curated",   None, True,  30),
            ("served",    90,   False, 7),
            ("audit",     None, True,  1),
        ]
        lake_buckets: dict[str, s3.Bucket] = {}
        for zone, retention, versioned, transition_days in ZONE_CONFIGS:
            lake_buckets[zone] = s3.Bucket(self, f"Lake{zone.title()}Bucket",
                bucket_name=f"{{project_name}}-lake-{zone}-{stage_name}-{Aws.ACCOUNT_ID}",
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                encryption=s3.BucketEncryption.KMS,
                encryption_key=cmk,                   # LOCAL key — safe
                versioned=versioned,
                enforce_ssl=True,
                lifecycle_rules=[s3.LifecycleRule(
                    id="transition-to-ia",
                    transitions=[
                        s3.Transition(storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                                      transition_after=Duration.days(transition_days)),
                        s3.Transition(storage_class=s3.StorageClass.GLACIER,
                                      transition_after=Duration.days(365)),
                    ],
                    expiration=Duration.days(retention) if retention else None,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    enabled=True,
                )],
                event_bridge_enabled=True,
                removal_policy=RemovalPolicy.RETAIN,
            )

        # =================================================================
        # B) Glue databases + Iceberg table (owned here)
        # =================================================================
        glue_db_raw = glue.CfnDatabase(self, "GlueDatabaseRaw",
            catalog_id=Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=f"{{project_name}}_{stage_name}_raw",
                description="Raw ingestion zone",
                location_uri=f"s3://{lake_buckets['raw'].bucket_name}/",
            ),
        )
        glue_db_processed = glue.CfnDatabase(self, "GlueDatabaseProcessed",
            catalog_id=Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=f"{{project_name}}_{stage_name}_processed",
                description="Iceberg processed zone — ACID, time travel, schema evolution",
                location_uri=f"s3://{lake_buckets['processed'].bucket_name}/",
            ),
        )
        glue_db_curated = glue.CfnDatabase(self, "GlueDatabaseCurated",
            catalog_id=Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=f"{{project_name}}_{stage_name}_curated",
                description="Curated business-ready datasets",
                location_uri=f"s3://{lake_buckets['curated'].bucket_name}/",
            ),
        )

        events_table = glue.CfnTable(self, "GlueIcebergEventsTable",
            catalog_id=Aws.ACCOUNT_ID,
            database_name=glue_db_processed.ref,
            table_input=glue.CfnTable.TableInputProperty(
                name="events",
                description="User events — Iceberg format with daily hidden partitioning",
                table_type="ICEBERG",
                owner="{project_name}-data-team",
                parameters={
                    "table_type": "iceberg",
                    "format": "parquet",
                    "write.parquet.compression-codec": "zstd",
                    "write.metadata.metrics.default": "full",
                    "history.expire.max-snapshot-age-ms": "604800000",
                    "write.target-file-size-bytes": "134217728",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{lake_buckets['processed'].bucket_name}/events/",
                    input_format="org.apache.iceberg.mr.mapred.IcebergInputFormat",
                    output_format="org.apache.iceberg.mr.mapred.IcebergOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.iceberg.mr.serde.IcebergSerDe",
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="event_id",   type="string"),
                        glue.CfnTable.ColumnProperty(name="user_id",    type="string"),
                        glue.CfnTable.ColumnProperty(name="event_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="event_ts",   type="timestamp"),
                        glue.CfnTable.ColumnProperty(name="session_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="properties", type="map<string,string>"),
                        glue.CfnTable.ColumnProperty(name="amount",     type="double"),
                        glue.CfnTable.ColumnProperty(name="event_date", type="date"),
                    ],
                ),
            ),
        )

        # =================================================================
        # C) Lake Formation settings
        # =================================================================
        lf.CfnDataLakeSettings(self, "LakeFormationSettings",
            admins=[lf.CfnDataLakeSettings.DataLakePrincipalProperty(
                data_lake_principal_identifier=data_lake_admin_role_arn,
            )],
            allow_external_data_filtering=True,
            create_database_default_permissions=[],
            create_table_default_permissions=[],
        )
        for zone, bucket in lake_buckets.items():
            lf.CfnResource(self, f"LFResource{zone.title()}",
                resource_arn=bucket.bucket_arn,
                use_service_linked_role=True,
            )
        lf.CfnDataCellsFilter(self, "PIIColumnMask",
            table_catalog_id=Aws.ACCOUNT_ID,
            database_name=glue_db_processed.ref,
            table_name="events",
            name="mask_pii_columns",
            column_names=["event_id", "event_type", "event_ts", "event_date",
                          "session_id", "properties", "amount"],
        )
        lf.CfnDataCellsFilter(self, "TenantRowFilter",
            table_catalog_id=Aws.ACCOUNT_ID,
            database_name=glue_db_processed.ref,
            table_name="events",
            name="tenant_row_filter",
            row_filter=lf.CfnDataCellsFilter.RowFilterProperty(
                filter_expression="tenant_id = SESSION_USER()",
            ),
            column_wildcard=lf.CfnDataCellsFilter.ColumnWildcardProperty(
                excluded_column_names=[],
            ),
        )

        # =================================================================
        # D) Athena v3 workgroup (owned here)
        # =================================================================
        athena_results = s3.Bucket(self, "AthenaResultsBucket",
            bucket_name=f"{{project_name}}-athena-results-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30), enabled=True)],
            removal_policy=RemovalPolicy.DESTROY if not IS_PROD else RemovalPolicy.RETAIN,
        )
        athena_wg = athena.CfnWorkGroup(self, "AthenaWorkgroup",
            name=f"{{project_name}}-{stage_name}",
            description=f"{{project_name}} lakehouse queries — Iceberg DML enabled",
            recursive_delete_option=not IS_PROD,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3",
                ),
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{athena_results.bucket_name}/query-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=cmk.key_arn,
                    ),
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=10 * 1024**4 if IS_PROD else 1 * 1024**4,
            ),
        )

        # =================================================================
        # E) Glue ETL role + job (local — identity-side grants on LOCAL resources)
        # =================================================================
        glue_role = iam.Role(self, "GlueETLRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            role_name=f"{{project_name}}-glue-etl-{stage_name}",
            permissions_boundary=permission_boundary,
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
            ],
        )
        # All buckets are local — L2 grants are safe here (no cross-stack cycle)
        for bucket in lake_buckets.values():
            bucket.grant_read_write(glue_role)
        cmk.grant_encrypt_decrypt(glue_role)
        glue_role.add_to_policy(iam.PolicyStatement(
            actions=["lakeformation:GetDataAccess", "glue:GetTable", "glue:GetDatabase",
                     "glue:UpdateTable", "glue:CreateTable"],
            resources=["*"],
        ))

        raw_to_iceberg_job = glue.CfnJob(self, "RawToIcebergJob",
            name=f"{{project_name}}-raw-to-iceberg-{stage_name}",
            role=glue_role.role_arn,
            description="Glue Spark ETL: raw S3 → Iceberg processed zone",
            glue_version="4.0",
            worker_type="G.1X" if not IS_PROD else "G.2X",
            number_of_workers=5 if not IS_PROD else 20,
            timeout=120,
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
                "--conf3":                       f"spark.sql.catalog.glue_catalog.warehouse=s3://{lake_buckets['processed'].bucket_name}/",
                "--conf4":                       "spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog",
                "--conf5":                       "spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
                "--RAW_BUCKET":                  lake_buckets["raw"].bucket_name,
                "--PROCESSED_BUCKET":            lake_buckets["processed"].bucket_name,
                "--GLUE_DATABASE":               glue_db_processed.ref,
                "--STAGE":                       stage_name,
            },
            command=glue.CfnJob.JobCommandProperty(
                name="glueetl",
                python_version="3",
                script_location=f"s3://{lake_buckets['raw'].bucket_name}/glue-scripts/raw_to_iceberg.py",
            ),
            execution_property=glue.CfnJob.ExecutionPropertyProperty(max_concurrent_runs=3),
        )

        # =================================================================
        # F) Redshift Serverless + Spectrum (owned here)
        # =================================================================
        redshift_sg = ec2.SecurityGroup(self, "RedshiftSG",
            vpc=vpc,
            security_group_name=f"{{project_name}}-redshift-{stage_name}",
            description="Redshift Serverless — VPC access only",
            allow_all_outbound=True,
        )
        redshift_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(5439),
            description="Redshift port from VPC only",
        )

        redshift_admin_secret = sm.Secret(self, "RedshiftAdminSecret",
            secret_name=f"/{{project_name}}/{stage_name}/redshift/admin",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template='{"username": "admin"}',
                generate_string_key="password",
                exclude_characters='"@/\\\'',
                password_length=32,
            ),
        )

        redshift_namespace = redshift.CfnNamespace(self, "RedshiftNamespace",
            namespace_name=f"{{project_name}}-{stage_name}",
            admin_username="admin",
            admin_user_password=redshift_admin_secret.secret_value_from_json("password").unsafe_unwrap(),
            db_name=f"{{project_name}}_{stage_name}",
            kms_key_id=cmk.key_arn,
            iam_roles=[redshift_spectrum_role_arn],
            log_exports=["userlog", "connectionlog", "useractivitylog"],
        )
        redshift_workgroup = redshift.CfnWorkgroup(self, "RedshiftWorkgroup",
            workgroup_name=f"{{project_name}}-{stage_name}",
            namespace_name=redshift_namespace.ref,
            base_capacity=8 if not IS_PROD else 32,
            max_capacity=64 if not IS_PROD else 512,
            enhanced_vpc_routing=True,
            publicly_accessible=False,
            subnet_ids=[s.subnet_id for s in vpc.isolated_subnets[:2]],
            security_group_ids=[redshift_sg.security_group_id],
            config_parameters=[
                redshift.CfnWorkgroup.ConfigParameterProperty(
                    parameter_key="enable_user_activity_logging", parameter_value="true",
                ),
                redshift.CfnWorkgroup.ConfigParameterProperty(
                    parameter_key="max_concurrency_scaling_clusters", parameter_value="10",
                ),
            ],
        )

        SPECTRUM_SETUP_SQL = f"""
CREATE EXTERNAL SCHEMA IF NOT EXISTS lake_processed
FROM DATA CATALOG
DATABASE '{glue_db_processed.ref}'
IAM_ROLE '{redshift_spectrum_role_arn}'
CREATE EXTERNAL DATABASE IF NOT EXISTS;

CREATE EXTERNAL SCHEMA IF NOT EXISTS lake_curated
FROM DATA CATALOG
DATABASE '{glue_db_curated.ref}'
IAM_ROLE '{redshift_spectrum_role_arn}';
"""
        ssm.StringParameter(self, "SpectrumSetupSQL",
            parameter_name=f"/{{project_name}}/{stage_name}/redshift/spectrum-setup-sql",
            string_value=SPECTRUM_SETUP_SQL,
            description="Run this SQL in Redshift after deployment to set up Spectrum",
        )

        # =================================================================
        # G) Glue Data Quality ruleset (owned here)
        # =================================================================
        glue.CfnDataQualityRuleset(self, "EventsTableDQRules",
            name=f"{{project_name}}-events-dq-{stage_name}",
            description="Data quality rules for the events Iceberg table",
            ruleset="""
Rules = [
    Completeness "event_id"  >= 0.99,
    Completeness "user_id"   >= 0.98,
    Completeness "event_ts"  = 1.0,
    Uniqueness   "event_id"  = 1.0,
    ColumnValues "event_type" in [ "click", "view", "purchase", "cancel", "signup" ],
    ColumnLength "event_id"  between 36 and 36,
    ColumnValues "amount"    >= 0 WHERE "amount" IS NOT NULL,
    Freshness    "event_ts"  <= 3 HOURS,
]
""",
            target_table=glue.CfnDataQualityRuleset.DataQualityTargetTableProperty(
                database_name=glue_db_processed.ref,
                table_name="events",
            ),
        )

        # =================================================================
        # H) Pipeline Lambda (anchored asset, identity-side grants)
        # =================================================================
        pipeline_log = logs.LogGroup(self, "PipelineLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-lakehouse-pipeline-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        pipeline_fn = _lambda.Function(self, "LakehousePipelineFn",
            function_name=f"{{project_name}}-lakehouse-pipeline-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "lakehouse_pipeline")),
            environment={
                "GLUE_JOB_NAME":         raw_to_iceberg_job.ref,
                "ATHENA_WORKGROUP":      athena_wg.name,
                "GLUE_DATABASE":         glue_db_processed.ref,
                "ATHENA_RESULTS_BUCKET": athena_results.bucket_name,
            },
            timeout=Duration.minutes(2),
            log_group=pipeline_log,
        )
        pipeline_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "glue:StartJobRun",
                "athena:StartQueryExecution",
                "athena:GetQueryResults",
                "athena:GetQueryExecution",
            ],
            resources=[
                f"arn:aws:glue:{self.region}:{Aws.ACCOUNT_ID}:job/{raw_to_iceberg_job.ref}",
                f"arn:aws:athena:{self.region}:{Aws.ACCOUNT_ID}:workgroup/{athena_wg.name}",
            ],
        ))
        # PassRole for starting the Glue job with the Glue ETL role
        pipeline_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[glue_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "glue.amazonaws.com"}},
        ))
        iam.PermissionsBoundary.of(pipeline_fn.role).apply(permission_boundary)

        events.Rule(self, "HourlyPipelineTrigger",
            rule_name=f"{{project_name}}-hourly-lakehouse-{stage_name}",
            schedule=events.Schedule.cron(minute="5"),
            targets=[targets.LambdaFunction(pipeline_fn)],
            enabled=IS_PROD,
        )
        lake_buckets["raw"].add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(pipeline_fn),
            s3.NotificationKeyFilter(prefix="events/", suffix=".parquet"),
        )

        # =================================================================
        # I) Glue crawler (owned here)
        # =================================================================
        glue.CfnCrawler(self, "RawZoneCrawler",
            name=f"{{project_name}}-raw-crawler-{stage_name}",
            role=glue_role.role_arn,
            database_name=glue_db_raw.ref,
            targets=glue.CfnCrawler.TargetsProperty(
                s3_targets=[glue.CfnCrawler.S3TargetProperty(
                    path=f"s3://{lake_buckets['raw'].bucket_name}/",
                    exclusions=["glue-scripts/**", "**/_temporary/**", "**/_SUCCESS"],
                )],
            ),
            schema_change_policy=glue.CfnCrawler.SchemaChangePolicyProperty(
                update_behavior="UPDATE_IN_DATABASE",
                delete_behavior="LOG",
            ),
            recrawl_policy=glue.CfnCrawler.RecrawlPolicyProperty(
                recrawl_behavior="CRAWL_NEW_FOLDERS_ONLY",
            ),
            schedule=glue.CfnCrawler.ScheduleProperty(
                schedule_expression="cron(0 * * * ? *)",
            ) if IS_PROD else None,
            configuration=json.dumps({
                "Version": 1.0,
                "CrawlerOutput": {"Partitions": {"AddOrUpdateBehavior": "InheritFromTable"}},
                "Grouping": {"TableGroupingPolicy": "CombineCompatibleSchemas"},
            }),
        )

        # =================================================================
        # Alarms
        # =================================================================
        cw.Alarm(self, "GlueJobFailureAlarm",
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
            alarm_actions=[cw_actions.SnsAction(alert_topic)],
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        cw.Alarm(self, "AthenaDataScannedAlarm",
            alarm_name=f"{{project_name}}-athena-large-query-{stage_name}",
            alarm_description="Athena query scanned >100GB — possible missing partition filter",
            metric=cw.Metric(
                namespace="AWS/Athena",
                metric_name="DataScannedInBytes",
                dimensions_map={"WorkGroup": athena_wg.name},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=100 * 1024**3,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(alert_topic)],
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        # =================================================================
        # Publish consumer-facing names/ARNs via SSM (no CFN exports → no cycles)
        # =================================================================
        for pid, pname, pval in [
            ("RawBucketParam",       f"/{{project_name}}/{stage_name}/lakehouse/raw_bucket",        lake_buckets["raw"].bucket_name),
            ("ProcessedBucketParam", f"/{{project_name}}/{stage_name}/lakehouse/processed_bucket",  lake_buckets["processed"].bucket_name),
            ("CuratedBucketParam",   f"/{{project_name}}/{stage_name}/lakehouse/curated_bucket",    lake_buckets["curated"].bucket_name),
            ("ServedBucketParam",    f"/{{project_name}}/{stage_name}/lakehouse/served_bucket",     lake_buckets["served"].bucket_name),
            ("LakehouseKmsArn",      f"/{{project_name}}/{stage_name}/lakehouse/kms_key_arn",       cmk.key_arn),
            ("ProcessedDbName",      f"/{{project_name}}/{stage_name}/lakehouse/processed_db",      glue_db_processed.ref),
            ("AthenaWorkgroupParam", f"/{{project_name}}/{stage_name}/lakehouse/athena_workgroup",  athena_wg.name),
            ("RedshiftEndpointParam",f"/{{project_name}}/{stage_name}/lakehouse/redshift_endpoint", redshift_workgroup.attr_workgroup_endpoint_address),
        ]:
            ssm.StringParameter(self, pid, parameter_name=pname, string_value=pval)

        CfnOutput(self, "LakeRawBucket",       value=lake_buckets["raw"].bucket_name)
        CfnOutput(self, "LakeProcessedBucket", value=lake_buckets["processed"].bucket_name)
        CfnOutput(self, "AthenaWorkgroupName", value=athena_wg.name)
        CfnOutput(self, "GlueETLJobName",      value=raw_to_iceberg_job.ref)
```

Consumer stacks (e.g. an ML training stack that reads `processed` Iceberg) read SSM parameters and grant identity-side only:

```python
# inside consumer stack
processed_bucket_name = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/lakehouse/processed_bucket",
)
lakehouse_kms_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/lakehouse/kms_key_arn",
)

consumer_role.add_to_policy(iam.PolicyStatement(
    actions=["s3:GetObject", "s3:ListBucket"],
    resources=[
        f"arn:aws:s3:::{processed_bucket_name}",
        f"arn:aws:s3:::{processed_bucket_name}/*",
    ],
))
consumer_role.add_to_policy(iam.PolicyStatement(
    actions=["kms:Decrypt", "kms:DescribeKey"],
    resources=[lakehouse_kms_arn],
))
# For Athena query submission
consumer_role.add_to_policy(iam.PolicyStatement(
    actions=["athena:StartQueryExecution", "athena:GetQueryResults", "glue:GetTable", "glue:GetDatabase"],
    resources=["*"],
))
```

### 4.3 Micro-stack gotchas

- **Redshift Spectrum IAM role** — the role ARN is embedded in `CfnNamespace.iam_roles=[...]` and in the `CREATE EXTERNAL SCHEMA` SQL. If this role lives in another stack and is read via SSM, the stack cannot reference it at synth time as an L2 `IRole`. Use the string ARN directly (as shown) and grant `iam:PassRole` with a `PassedToService` condition on `redshift.amazonaws.com` from the invoking principal.
- **Lake Formation `CfnDataLakeSettings`** uses `data_lake_principal_identifier=<arn_string>`. Strings read from SSM are resolved at deploy time — no cycle even if the role lives elsewhere. Do NOT pass an `iam.IRole` from another stack.
- **SSM `value_for_string_parameter` returns a token** — you cannot use string operations on it at synth time. If a Lambda needs the value as a template-compiled string, inject via environment variable (resolved at deploy), not via Python f-string against a token.
- **`redshift.CfnNamespace.admin_user_password`** — `unsafe_unwrap()` is needed because CFN requires a string, not a secret reference. Rotation must be driven by Redshift-managed admin passwords (`manage_admin_password=True`) if rotation compliance is in scope. `# TODO(verify): admin_user_password vs manage_admin_password precedence on CfnNamespace` — confirm against latest CDK release.
- **Iceberg table parameters must be set on creation** — changing `write.target-file-size-bytes` later via CFN update may not propagate. For an in-place change, run `ALTER TABLE ... SET TBLPROPERTIES` through Athena.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / one-stack project, < 400 resources | §3 Monolith |
| Separate data-platform team owns lakehouse lifecycle | §4 Micro-Stack — `LakehouseStack` exports via SSM |
| Need Delta Lake or Hudi (non-AWS-native) | Swap `--datalake-formats=iceberg` → `delta` or `hudi`; drop `table_type=ICEBERG`; lose Athena v3 full DML |
| Storage > 10TB, query volume > 100 GB/day | Add Glue DataBrew + Redshift RA3 (provisioned); keep Athena for ad-hoc |
| Streaming ingest (Kinesis/MSK) | Add `DATA_MSK_KAFKA` or Kinesis Firehose → raw bucket; keep this SOP for downstream |
| Multi-account data mesh | Lake Formation cross-account grants (`CfnPrincipalPermissions`); SSM parameters published into each consumer account's param store |
| FedRAMP / HIPAA | Swap `object_lock_enabled=True` on all zones, move `cmk` to `ComplianceStack` and read ARN via SSM; Athena results bucket retention ≥ 7 years |

---

## 6. Worked example

Save as `tests/sop/test_DATA_LAKEHOUSE_ICEBERG.py`. Offline — no AWS credentials needed.

```python
"""SOP verification — LakehouseStack synthesizes with stub SSM params + boundary."""
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_sns as sns,
    aws_ssm as ssm,
)
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_lakehouse_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=2,
        subnet_configuration=[
            ec2.SubnetConfiguration(name="priv", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24),
            ec2.SubnetConfiguration(name="iso",  subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,    cidr_mask=24),
        ],
    )
    topic = sns.Topic(deps, "AlertTopic")
    ssm.StringParameter(deps, "AlertTopicArn",
        parameter_name="/test/obs/alert_topic_arn",
        string_value=topic.topic_arn,
    )

    # Stub role ARNs via SSM (strings — no cross-stack L2 references)
    ssm.StringParameter(deps, "LfAdminRoleArn",
        parameter_name="/test/lake/lf_admin_role_arn",
        string_value=f"arn:aws:iam::{env.account}:role/lf-admin",
    )
    ssm.StringParameter(deps, "SpectrumRoleArn",
        parameter_name="/test/lake/spectrum_role_arn",
        string_value=f"arn:aws:iam::{env.account}:role/redshift-spectrum",
    )

    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.lakehouse_stack import LakehouseStack
    stack = LakehouseStack(
        app, stage_name="dev",
        vpc=vpc,
        alert_topic_arn_ssm="/test/obs/alert_topic_arn",
        data_lake_admin_role_arn_ssm="/test/lake/lf_admin_role_arn",
        redshift_spectrum_role_arn_ssm="/test/lake/spectrum_role_arn",
        permission_boundary=boundary,
        env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::KMS::Key",                           1)
    t.resource_count_is("AWS::S3::Bucket",                         6)   # 5 zones + athena results
    t.resource_count_is("AWS::Glue::Database",                     3)
    t.resource_count_is("AWS::Glue::Table",                        1)
    t.resource_count_is("AWS::Glue::Job",                          1)
    t.resource_count_is("AWS::Glue::Crawler",                      1)
    t.resource_count_is("AWS::Athena::WorkGroup",                  1)
    t.resource_count_is("AWS::RedshiftServerless::Namespace",      1)
    t.resource_count_is("AWS::RedshiftServerless::Workgroup",      1)
    t.resource_count_is("AWS::Lambda::Function",                   1)   # pipeline_fn
    # Assert Athena v3 enforced
    t.has_resource_properties("AWS::Athena::WorkGroup", {
        "WorkGroupConfiguration": {
            "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
        },
    })
```

---

## 7. References

- `docs/template_params.md` — `LAKEHOUSE_KMS_KEY_ARN_SSM`, `LAKE_RAW_BUCKET_SSM`, `LAKE_PROCESSED_BUCKET_SSM`, `LAKE_CURATED_BUCKET_SSM`, `LAKE_SERVED_BUCKET_SSM`, `ATHENA_WORKGROUP_SSM`, `REDSHIFT_ENDPOINT_SSM`, `PROCESSED_DB_NAME_SSM`, `GLUE_ETL_ROLE_ARN_SSM`, `REDSHIFT_SPECTRUM_ROLE_ARN_SSM`, `LF_ADMIN_ROLE_ARN_SSM`, `STAGE_NAME`
- `docs/Feature_Roadmap.md` — feature IDs `DL-01..DL-18` (lakehouse), `DL-19..DL-25` (federation), `DQ-01..DQ-07` (data quality), `LF-01..LF-09` (Lake Formation)
- Apache Iceberg on AWS: https://docs.aws.amazon.com/prescriptive-guidance/latest/apache-iceberg-on-aws/introduction.html
- Athena Iceberg DML: https://docs.aws.amazon.com/athena/latest/ug/querying-iceberg.html
- Lake Formation: https://docs.aws.amazon.com/lake-formation/latest/dg/what-is-lake-formation.html
- Glue 4.0 Iceberg connector: https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-etl-format-iceberg.html
- Redshift Spectrum external schemas: https://docs.aws.amazon.com/redshift/latest/dg/c-using-spectrum.html
- Related SOPs: `LAYER_DATA` (S3 + RDS + DDB patterns), `MLOPS_DATA_PLATFORM` (Glue/Athena/Redshift/EMR micro-stack pattern), `LAYER_NETWORKING` (VPC isolated subnets for Redshift), `LAYER_SECURITY` (KMS, permission boundary), `LAYER_BACKEND_LAMBDA` (five non-negotiables, identity-side grant helpers), `DATA_MSK_KAFKA` (streaming ingestion into raw zone), `OPS_ADVANCED_MONITORING` (Glue + Athena CloudWatch alarms)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `LakehouseStack` owns local CMK (honors 5th non-negotiable), all S3 zones, Glue catalog, Athena v3 workgroup, Redshift Serverless, Glue ETL job/crawler, pipeline Lambda; publishes bucket names + KMS ARN + processed DB + Athena workgroup + Redshift endpoint via SSM; consumer stacks grant identity-side `s3:GetObject` / `kms:Decrypt` / `athena:StartQueryExecution`. Extracted inline pipeline Lambda from `Code.from_inline` to `Code.from_asset(_LAMBDAS_ROOT / "lakehouse_pipeline")` with explicit `LogGroup`. Extracted Glue Spark ETL script to `glue_scripts/raw_to_iceberg.py` with fixed `datetime` import. Added permissions boundary + `iam:PassRole` with `iam:PassedToService` Condition on Glue job. Added Swap matrix (§5), Worked example (§6), Monolith gotchas (§3.6), Micro-stack gotchas (§4.3). Preserved all v1.0 content: zone configs, Glue DB/table/crawler, Lake Formation settings + column/row filters, Athena named queries, Glue ETL args + Spark MERGE script, Redshift Serverless + Spectrum setup SQL, Glue DQ ruleset, alarms. |
| 1.0 | 2026-03-05 | Initial monolith — 5 S3 zones, Glue databases + Iceberg events table, Lake Formation settings + PII mask + tenant row filter, Athena v3 workgroup with Iceberg MERGE named query and time-travel example, Glue 4.0 raw→Iceberg ETL job with Spark MERGE script, Redshift Serverless namespace+workgroup + Spectrum setup SQL, Glue DQ ruleset, EventBridge + S3 triggered pipeline Lambda, Glue crawler, Glue failure + Athena scan alarms. |
