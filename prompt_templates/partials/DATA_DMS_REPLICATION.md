# SOP — AWS Database Migration Service (DMS Serverless homogeneous + classic heterogeneous CDC + S3 lakehouse landing)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · DMS Serverless replication (homogeneous, GA 2024) · Classic DMS replication instance + tasks (heterogeneous + CDC) · S3 target endpoint (Parquet for lakehouse) · Postgres / MySQL / MongoDB / Oracle / SQL Server / DocumentDB / Aurora sources

---

## 1. Purpose

- Provide a definitive guide to AWS DMS in 2026, when there are now **two products under the DMS umbrella**:
  - **Homogeneous Data Migrations** (DMS Serverless, GA 2024) — Postgres→Postgres, MySQL→MySQL, MongoDB→DocumentDB. Uses native source tools (`pg_dump`/`pg_restore`, `mydumper`/`myloader`, `mongodump`/`mongorestore`). No replication instance to manage.
  - **Classic DMS** — heterogeneous (Oracle→Postgres, SQL Server→Aurora, MySQL→Redshift) and CDC-only flows. Uses a managed replication instance + replication tasks + JSON table mappings + transformation rules.
- Codify the **decision tree** between Aurora zero-ETL · DMS Serverless · classic DMS · custom Glue ETL.
- Provide the **S3 target endpoint** pattern for landing source-system change events into a lakehouse (raw zone → Iceberg curated zone via Glue ETL or Athena CTAS).
- Provide the **CDC start point** pattern (LSN for Postgres, binlog position for MySQL, ChangeStream resume token for MongoDB) so a migration can be paused/resumed.
- Give a complete CDK pattern for both DMS Serverless and classic replication instance, with VPC, IAM, KMS, and CloudWatch wiring.
- This is the **migration + ongoing CDC specialisation**. Aurora zero-ETL (`DATA_ZERO_ETL`) covers the no-config CDC path *for Aurora/RDS sources only*; DMS covers everything else.

When the SOW signals: "migrate from Oracle / SQL Server / on-prem MySQL / on-prem Postgres", "lift-and-shift database", "ongoing CDC into the lakehouse", "MongoDB to DocumentDB", "log-based replication", "validation rules between source and target schema", "transformation during migration".

---

## 2. Decision — which DMS path? (and when to NOT use DMS)

| You have… | Use this | Why |
|---|---|---|
| Aurora MySQL/Postgres source + Redshift target | **Aurora zero-ETL** (`DATA_ZERO_ETL`) | Native CDC, 5-15 min lag, no infrastructure |
| RDS MySQL source + Redshift target | **RDS zero-ETL** (`DATA_ZERO_ETL`) | Same, but for non-Aurora RDS |
| DDB → OpenSearch / Redshift | **DDB zero-ETL** (`DATA_ZERO_ETL`) | Stream-based, managed |
| **Postgres → Postgres** (different version, Multi-AZ, encryption upgrade) | **§3 DMS Serverless homogeneous** | Native `pg_dump`/`pg_restore` + logical replication CDC, no replication instance to size |
| **MySQL → MySQL** (Aurora, RDS, or self-managed source) | **§3 DMS Serverless homogeneous** | mydumper/myloader + binlog CDC |
| **MongoDB → DocumentDB** | **§3 DMS Serverless homogeneous** | mongodump/mongorestore + ChangeStream CDC |
| **Oracle → Postgres / Aurora** (heterogeneous) | **§4 Classic DMS** + Schema Conversion Tool (SCT) | Schema conversion + ongoing CDC; SCT runs separately for DDL conversion |
| **SQL Server → Aurora / Postgres** | **§4 Classic DMS** + SCT | Same |
| **Any RDBMS → S3 lakehouse (Parquet)** | **§4 Classic DMS** + S3 endpoint | Parquet output with date partitioning, drops into raw zone |
| **Any RDBMS → Iceberg directly** | **§4 Classic DMS to S3** + Glue/EMR job | DMS doesn't write Iceberg natively; land in S3 then Iceberg-ize |
| Real-time streaming with sub-second SLA | **NOT DMS** | Use Kinesis/MSK + DMS source as fallback only |
| One-shot full export, no CDC needed | **NOT DMS** | Use AWS Schema Conversion Tool data extraction agents OR Glue connector job |
| Vendor migration (e.g. Salesforce to S3) | **NOT DMS** | Use AppFlow (`DATA_APPFLOW_SAAS_INGEST`) |

### 2.1 Which DMS variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC where the DMS instance/Serverless rep, source+target endpoints, and consumer Lambdas all live in one `cdk.Stack` | **§3 / §4 Monolith Variant** |
| `MigrationStack` owns DMS infra; `ConsumerStack` owns Lambdas that read S3 raw / query the migrated DB | **§5 Micro-Stack Variant** |

**Why the split matters for DMS specifically.** DMS endpoints reference Secrets Manager secrets (for source/target DB creds). If consumer code outside the migration stack needs to verify migration state via DMS API calls, `replication.grant_*` patterns mutate IAM policies on the replication resource → cyclic export the same way Aurora secrets do. Always publish DMS replication ARN + endpoint ARNs via SSM and have consumers grant themselves identity-side `dms:Describe*` on those ARNs.

---

## 3. DMS Serverless homogeneous variant (modern, GA 2024)

### 3.1 When this is the right tool

- Source and target engine are the **same family** (Postgres↔Postgres, MySQL↔MySQL, MongoDB↔DocumentDB).
- You want **no replication instance to size** — DMS Serverless auto-scales DCUs (Data Capacity Units).
- You want **native source-engine tools** — pg_dump's logical replication is more resilient than DMS's logical reader for Postgres-specific features (composite types, arrays, custom enums).
- Migration types: full-load · CDC-only · full-load + CDC.

### 3.2 Architecture

```
  Source DB (self-managed Postgres / on-prem MySQL / Aurora MongoDB)
      │  TCP 5432 / 3306 / 27017 over PrivateLink or VPN
      ▼
  ┌─────────────────────────────────────────────────────────┐
  │  DMS Serverless Replication                             │
  │     - Auto-scaling DCU (1 DCU = 2 GB RAM, ~2 vCPU)      │
  │     - Native tools: pg_dump/pg_restore | mydumper |     │
  │       mongodump → CDC start point capture →             │
  │       logical/binlog/ChangeStream replication            │
  │     - State stored in DMS-managed S3 bucket              │
  └─────────────────────────────────────────────────────────┘
      │
      ▼
  Target DB (Aurora Postgres v16 / Aurora MySQL / DocumentDB)
      │
      ▼  (Optional) cutover: app DNS swap to target
```

### 3.3 CDK — `_create_dms_serverless_homogeneous()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_dms as dms,                         # L1 only — DMS Serverless not yet L2 in CDK
    aws_secretsmanager as sm,
)


def _create_dms_serverless_homogeneous(self, stage: str) -> None:
    """Monolith variant. Assumes self.{vpc, dms_sg, kms_key,
    source_secret, target_secret} exist. Source + target are Postgres."""

    # A) Subnet group — DMS Serverless lives in your VPC, sees both source
    #    (via VPC peering/PrivateLink) and target (Aurora in same VPC).
    sn_group = dms.CfnReplicationSubnetGroup(
        self, "DmsSubnetGroup",
        replication_subnet_group_description=f"DMS subnet group {stage}",
        replication_subnet_group_identifier=f"{{project_name}}-dms-sg-{stage}",
        subnet_ids=[s.subnet_id for s in self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets],
    )

    # B) Source data provider — wraps secret + connection details. New
    #    "Data Provider" abstraction (2024+) is required for homogeneous mode.
    #    NOTE: CfnDataProvider was added to aws-cdk-lib in v2.150+. If the
    #    L1 isn't available in your CDK version, use raw CloudFormation via
    #    cdk.CfnResource("AWS::DMS::DataProvider", ...).
    src_dp = dms.CfnDataProvider(
        self, "SrcDataProvider",
        engine="POSTGRES",
        data_provider_name=f"{{project_name}}-src-{stage}",
        settings=dms.CfnDataProvider.SettingsProperty(
            postgre_sql_settings=dms.CfnDataProvider.PostgreSqlSettingsProperty(
                server_name="source-postgres.example.com",
                port=5432,
                database_name="appdb",
                ssl_mode="require",
            )
        ),
    )

    tgt_dp = dms.CfnDataProvider(
        self, "TgtDataProvider",
        engine="POSTGRES",
        data_provider_name=f"{{project_name}}-tgt-{stage}",
        settings=dms.CfnDataProvider.SettingsProperty(
            postgre_sql_settings=dms.CfnDataProvider.PostgreSqlSettingsProperty(
                server_name=self.aurora_writer_endpoint,
                port=5432,
                database_name="appdb",
                ssl_mode="require",
            )
        ),
    )

    # C) Migration project — top-level container that ties source DP + target
    #    DP + IAM role + instance profile.
    mig_role = iam.Role(
        self, "DmsMigRole",
        assumed_by=iam.ServicePrincipal("dms.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonDMSCloudWatchLogsRole"),
        ],
        permissions_boundary=self.permission_boundary,
    )
    # Source secret + target secret — read-only for DMS service principal.
    self.source_secret.grant_read(mig_role)
    self.target_secret.grant_read(mig_role)
    self.kms_key.grant_decrypt(mig_role)

    instance_profile = dms.CfnInstanceProfile(
        self, "DmsInstanceProfile",
        instance_profile_name=f"{{project_name}}-dms-ip-{stage}",
        kms_key_arn=self.kms_key.key_arn,
        network_type="IPV4",
        subnet_group_identifier=sn_group.replication_subnet_group_identifier,
        vpc_security_group_ids=[self.dms_sg.security_group_id],
        publicly_accessible=False,
    )

    project = dms.CfnMigrationProject(
        self, "DmsProject",
        instance_profile_identifier=instance_profile.instance_profile_name,
        migration_project_name=f"{{project_name}}-mig-{stage}",
        source_data_provider_descriptors=[
            dms.CfnMigrationProject.DataProviderDescriptorProperty(
                data_provider_identifier=src_dp.data_provider_name,
                secrets_manager_secret_id=self.source_secret.secret_arn,
                secrets_manager_access_role_arn=mig_role.role_arn,
            ),
        ],
        target_data_provider_descriptors=[
            dms.CfnMigrationProject.DataProviderDescriptorProperty(
                data_provider_identifier=tgt_dp.data_provider_name,
                secrets_manager_secret_id=self.target_secret.secret_arn,
                secrets_manager_access_role_arn=mig_role.role_arn,
            ),
        ],
    )

    # D) Data migration — the actual run. data_migration_type =
    #    full-load | cdc | full-load-and-cdc.
    self.dms_migration = dms.CfnDataMigration(
        self, "DmsMigration",
        data_migration_name=f"{{project_name}}-mig-run-{stage}",
        migration_project_identifier=project.migration_project_name,
        data_migration_type="full-load-and-cdc",
        service_access_role_arn=mig_role.role_arn,
        data_migration_settings=dms.CfnDataMigration.DataMigrationSettingsProperty(
            number_of_jobs=4,                # parallel workers
            cloudwatch_logs_enabled=True,
            selection_rules=open("./dms_rules/selection.json").read(),
        ),
        # CDC start position: LSN for Postgres, binlog file:position for MySQL.
        # Leave None for "from now"; specify for replay.
        # source_data_settings=dms.CfnDataMigration.SourceDataSettingsProperty(
        #     cdc_start_position="0/00000000",
        # ),
    )

    CfnOutput(self, "DmsMigrationName",
              value=self.dms_migration.data_migration_name)
    CfnOutput(self, "DmsProjectName",
              value=project.migration_project_name)
```

### 3.4 Selection rules JSON (`dms_rules/selection.json`)

```json
{
  "rules": [
    {
      "rule-type": "selection",
      "rule-id": "1",
      "rule-name": "include-public-schema",
      "object-locator": {
        "schema-name": "public",
        "table-name": "%"
      },
      "rule-action": "include",
      "filters": []
    },
    {
      "rule-type": "selection",
      "rule-id": "2",
      "rule-name": "exclude-staging-tables",
      "object-locator": {
        "schema-name": "public",
        "table-name": "tmp_%"
      },
      "rule-action": "exclude",
      "filters": []
    },
    {
      "rule-type": "transformation",
      "rule-id": "3",
      "rule-name": "rename-target-schema",
      "rule-target": "schema",
      "object-locator": {"schema-name": "public"},
      "rule-action": "rename",
      "value": "imported"
    }
  ]
}
```

### 3.5 Engine-specific gotchas

| Engine | Gotcha |
|---|---|
| **Postgres source** | Must set `wal_level=logical`, `max_replication_slots≥4`, `max_wal_senders≥4`, `rds.logical_replication=1` (RDS only). DMS will create a dedicated replication slot — monitor `pg_replication_slots` to ensure it's not falling behind |
| **MySQL source** | Must enable binlog: `binlog_format=ROW`, `binlog_row_image=FULL`, `binlog_checksum=NONE` (DMS doesn't validate checksums), `binlog_retention_hours≥24` |
| **MongoDB source** | ChangeStream-based; source must be a replica set (not standalone). For sharded source, DMS reads from each shard's primary — DCU sizing scales linearly with shard count |
| **Postgres target** | Auto-creates schemas + tables matching source DDL. Custom types (composites, enums) must exist in target before migration starts — pre-deploy via Flyway/Alembic |
| **CDC restart** | Save the `cdc_start_position` value (LSN / binlog position) to SSM after every successful run so you can resume if the migration fails |

### 3.6 IAM — minimum policy for DMS service role

```python
mig_role.add_to_policy(iam.PolicyStatement(
    actions=[
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
    ],
    resources=[
        self.source_secret.secret_arn,
        self.target_secret.secret_arn,
    ],
))

mig_role.add_to_policy(iam.PolicyStatement(
    actions=[
        "kms:Decrypt", "kms:GenerateDataKey",
        "kms:DescribeKey",
    ],
    resources=[self.kms_key.key_arn],
    conditions={"StringEquals": {
        "kms:ViaService": [
            f"secretsmanager.{self.region}.amazonaws.com",
        ]
    }},
))

mig_role.add_to_policy(iam.PolicyStatement(
    actions=[
        "logs:CreateLogStream", "logs:PutLogEvents",
        "logs:DescribeLogGroups", "logs:DescribeLogStreams",
    ],
    resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:dms-tasks-*"],
))
```

---

## 4. Classic DMS variant (heterogeneous + S3 lakehouse target)

### 4.1 When this is the right tool

- Source and target are **different engines** (Oracle → Aurora, SQL Server → Postgres, MySQL → Redshift).
- Target is **S3** (raw lakehouse landing for any RDBMS source).
- You need **transformation rules** during migration (column renames, type coercion, derived columns).
- You're stuck with an **older source engine** (Oracle 11g, SQL Server 2014) that DMS Serverless homogeneous doesn't support.

### 4.2 Architecture

```
  Source DB (Oracle 12c / SQL Server 2019 / on-prem MySQL)
      │
      ▼
  ┌─────────────────────────────────────────────────────────┐
  │  DMS Replication Instance (managed EC2)                 │
  │     - Class: dms.t3.medium → dms.r5.4xlarge             │
  │     - Multi-AZ: prod yes, dev no                         │
  │     - Storage: 100-6000 GB gp3                           │
  │     - Engine: 3.5.x (latest LTS)                        │
  └─────────────────────────────────────────────────────────┘
      │
      │  Replication Task: full-load | cdc | full-load-and-cdc
      │  Table mappings: selection + transformation rules
      │
      ▼
  Target Endpoint (one of):
    a) Aurora Postgres / RDS Postgres
    b) Redshift Serverless
    c) S3 (Parquet output, date-partitioned, optional Iceberg via Glue)
    d) Kinesis Data Streams (for downstream stream processing)
    e) DocumentDB / MongoDB
    f) OpenSearch
```

### 4.3 CDK — `_create_dms_classic_to_s3()`

```python
def _create_dms_classic_to_s3(self, stage: str) -> None:
    """Monolith variant. Classic DMS replication instance + Oracle source +
    S3 target with Parquet output + ongoing CDC. Lands raw events in
    s3://qra-raw-{stage}/dms/{schema}/{table}/<date_partition>/<file>.parquet."""

    # A) Replication instance
    sn_group = dms.CfnReplicationSubnetGroup(
        self, "DmsSubnetGroup",
        replication_subnet_group_description=f"DMS subnet group {stage}",
        replication_subnet_group_identifier=f"{{project_name}}-dms-sg-{stage}",
        subnet_ids=[s.subnet_id for s in self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets],
    )

    rep_instance = dms.CfnReplicationInstance(
        self, "DmsRepInstance",
        replication_instance_class="dms.r5.large" if stage == "prod" else "dms.t3.medium",
        replication_instance_identifier=f"{{project_name}}-dms-{stage}",
        allocated_storage=200,                     # GB; CDC-heavy → more
        engine_version="3.5.4",                    # LTS as of 2026-04
        multi_az=(stage == "prod"),
        publicly_accessible=False,
        kms_key_id=self.kms_key.key_arn,
        replication_subnet_group_identifier=sn_group.replication_subnet_group_identifier,
        vpc_security_group_ids=[self.dms_sg.security_group_id],
        preferred_maintenance_window="sun:04:30-sun:05:30",
        auto_minor_version_upgrade=True,
    )

    # B) IAM service role for DMS to write to S3
    s3_role = iam.Role(
        self, "DmsS3Role",
        assumed_by=iam.ServicePrincipal("dms.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.raw_bucket.grant_read_write(s3_role)
    self.kms_key.grant_encrypt_decrypt(s3_role)

    # C) Source endpoint (Oracle)
    src_endpoint = dms.CfnEndpoint(
        self, "OracleSourceEndpoint",
        endpoint_type="source",
        engine_name="oracle",
        endpoint_identifier=f"{{project_name}}-oracle-src-{stage}",
        server_name="oracle.example.com",
        port=1521,
        database_name="ORCL",
        username=self.source_secret.secret_value_from_json("username").unsafe_unwrap(),
        password=self.source_secret.secret_value_from_json("password").unsafe_unwrap(),
        ssl_mode="verify-full",
        certificate_arn=self._oracle_ssl_cert.attr_certificate_arn,
        kms_key_id=self.kms_key.key_arn,
        # Oracle-specific tuning
        oracle_settings=dms.CfnEndpoint.OracleSettingsProperty(
            asm_server="asm.example.com",
            asm_user="asm_user",
            asm_password_secret_id=self.asm_secret.secret_arn,
            access_alternate_directly=False,
            archived_logs_only=False,
            use_logminer_reader=False,
            use_bfile=True,                        # Faster CDC for 12c+
        ),
    )

    # D) Target endpoint (S3 with Parquet)
    s3_target = dms.CfnEndpoint(
        self, "S3TargetEndpoint",
        endpoint_type="target",
        engine_name="s3",
        endpoint_identifier=f"{{project_name}}-s3-tgt-{stage}",
        s3_settings=dms.CfnEndpoint.S3SettingsProperty(
            bucket_name=self.raw_bucket.bucket_name,
            bucket_folder="dms",
            service_access_role_arn=s3_role.role_arn,
            data_format="parquet",                 # Iceberg-friendly
            parquet_version="parquet-2-0",
            parquet_timestamp_in_millisecond=True,
            enable_statistics=True,
            include_op_for_full_load=True,
            cdc_inserts_only=False,                # We want updates+deletes too
            cdc_inserts_and_updates=False,
            cdc_path="cdc",
            preserve_transactions=False,           # Set True if order matters
            date_partition_enabled=True,
            date_partition_sequence="YYYYMMDD",
            date_partition_delimiter="SLASH",
            timestamp_column_name="dms_timestamp",
            add_column_name=True,                  # Adds source column metadata
            compression_type="GZIP",
            encryption_mode="SSE_KMS",
            server_side_encryption_kms_key_id=self.kms_key.key_arn,
        ),
    )

    # E) Replication task
    self.dms_task = dms.CfnReplicationTask(
        self, "DmsTask",
        migration_type="full-load-and-cdc",
        replication_instance_arn=rep_instance.ref,
        replication_task_identifier=f"{{project_name}}-task-{stage}",
        source_endpoint_arn=src_endpoint.ref,
        target_endpoint_arn=s3_target.ref,
        table_mappings=open("./dms_rules/oracle_to_s3_mappings.json").read(),
        replication_task_settings=open("./dms_rules/task_settings.json").read(),
        # cdc_start_position can be specified for replay (Oracle SCN, MySQL binlog, etc.)
    )

    CfnOutput(self, "DmsTaskArn", value=self.dms_task.ref)
```

### 4.4 Table mappings JSON — Oracle → S3 with column transformations

```json
{
  "rules": [
    {
      "rule-type": "selection",
      "rule-id": "100",
      "rule-name": "include-orders-schema",
      "object-locator": {
        "schema-name": "ORDERS",
        "table-name": "%"
      },
      "rule-action": "include"
    },
    {
      "rule-type": "selection",
      "rule-id": "101",
      "rule-name": "exclude-audit-tables",
      "object-locator": {
        "schema-name": "ORDERS",
        "table-name": "AUDIT_%"
      },
      "rule-action": "exclude"
    },
    {
      "rule-type": "transformation",
      "rule-id": "200",
      "rule-name": "lowercase-schema",
      "rule-target": "schema",
      "object-locator": {"schema-name": "ORDERS"},
      "rule-action": "convert-lowercase"
    },
    {
      "rule-type": "transformation",
      "rule-id": "201",
      "rule-name": "lowercase-tables",
      "rule-target": "table",
      "object-locator": {"schema-name": "ORDERS", "table-name": "%"},
      "rule-action": "convert-lowercase"
    },
    {
      "rule-type": "transformation",
      "rule-id": "300",
      "rule-name": "redact-customer-email",
      "rule-target": "column",
      "object-locator": {
        "schema-name": "ORDERS",
        "table-name": "CUSTOMERS",
        "column-name": "EMAIL"
      },
      "rule-action": "remove-column"
    },
    {
      "rule-type": "transformation",
      "rule-id": "301",
      "rule-name": "add-source-system-column",
      "rule-target": "column",
      "object-locator": {
        "schema-name": "ORDERS",
        "table-name": "%"
      },
      "rule-action": "add-column",
      "value": "src_system",
      "expression": "'oracle_orders_db'",
      "data-type": {"type": "string", "length": 32}
    }
  ]
}
```

### 4.5 Task settings JSON (`task_settings.json`)

Key tunables — see [DMS task settings reference](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.html):

```json
{
  "TargetMetadata": {
    "ParallelLoadThreads": 8,
    "ParallelLoadBufferSize": 500,
    "BatchApplyEnabled": true
  },
  "FullLoadSettings": {
    "TargetTablePrepMode": "DROP_AND_CREATE",
    "MaxFullLoadSubTasks": 8,
    "TransactionConsistencyTimeout": 600,
    "CommitRate": 50000
  },
  "ChangeProcessingTuning": {
    "BatchApplyTimeoutMin": 1,
    "BatchApplyTimeoutMax": 30,
    "BatchApplyMemoryLimit": 500,
    "BatchSplitSize": 0,
    "MinTransactionSize": 1000,
    "CommitTimeout": 1
  },
  "ValidationSettings": {
    "EnableValidation": true,
    "ValidationMode": "ROW_LEVEL",
    "ThreadCount": 5,
    "PartitionSize": 10000,
    "FailureMaxCount": 10,
    "RecordFailureDelayLimitInMinutes": 0,
    "RecordSuspendDelayInMinutes": 30,
    "MaxKeyColumnSize": 8096
  },
  "Logging": {
    "EnableLogging": true,
    "LogComponents": [
      {"Id": "DATA_STRUCTURE", "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "COMMUNICATION",  "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "IO",             "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "COMMON",         "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "FILE_FACTORY",   "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "FILE_TRANSFER",  "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "REST_SERVER",    "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "ADDONS",         "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "TARGET_LOAD",    "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "TARGET_APPLY",   "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "SOURCE_UNLOAD",  "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "SOURCE_CAPTURE", "Severity": "LOGGER_SEVERITY_DEFAULT"},
      {"Id": "TRANSFORMATION", "Severity": "LOGGER_SEVERITY_DEFAULT"}
    ]
  }
}
```

### 4.6 Lakehouse landing — DMS S3 output → Iceberg

DMS writes Parquet to `s3://raw/dms/<schema>/<table>/<YYYYMMDD>/LOAD<seq>.parquet` (full load) and `s3://raw/dms/cdc/<schema>/<table>/<YYYYMMDD>/<seq>.parquet` (CDC). The CDC files include a `Op` column with `I`/`U`/`D` for insert/update/delete.

Recommended pattern: hourly Glue ETL job (or EMR Serverless Spark) that reads the new CDC files since last watermark, applies them to the target Iceberg table via MERGE, and advances the watermark in DDB. This pattern is what `DATA_LAKEHOUSE_ICEBERG` calls "MERGE-based CDC replay."

```sql
-- Athena MERGE for Iceberg target (run hourly via Glue trigger)
MERGE INTO curated.orders AS target
USING (
  SELECT * FROM raw_cdc.orders
  WHERE dms_timestamp > '{last_watermark}'
) AS source
ON target.order_id = source.order_id
WHEN MATCHED AND source.Op = 'D' THEN DELETE
WHEN MATCHED AND source.Op = 'U' THEN UPDATE SET *
WHEN NOT MATCHED AND source.Op = 'I' THEN INSERT *;
```

---

## 5. Micro-Stack variant (cross-stack via SSM)

When `MigrationStack` owns DMS infra and `ConsumerStack` (Glue jobs, Lambdas) reads from it, publish via SSM:

```python
# In MigrationStack
ssm.StringParameter(self, "DmsRepArn",
    parameter_name=f"/{{project_name}}/{stage}/dms/replication-arn",
    string_value=rep_instance.ref)
ssm.StringParameter(self, "DmsTaskArn",
    parameter_name=f"/{{project_name}}/{stage}/dms/task-arn",
    string_value=self.dms_task.ref)
ssm.StringParameter(self, "DmsRawPrefix",
    parameter_name=f"/{{project_name}}/{stage}/dms/raw-prefix",
    string_value=f"s3://{self.raw_bucket.bucket_name}/dms/")

# In ConsumerStack — Glue job role grants itself dms:Describe on specific ARN
dms_task_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/dms/task-arn")
glue_role.add_to_policy(iam.PolicyStatement(
    actions=["dms:DescribeReplicationTasks", "dms:DescribeTableStatistics"],
    resources=[dms_task_arn],
))
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| "Source endpoint connection test failed" | VPC peering/security group | Verify source SG allows 5432/3306 from `self.dms_sg.security_group_id`; for on-prem use Direct Connect or Site-to-Site VPN |
| "Target endpoint failed: insufficient privileges" | Target user lacks DDL | DMS auto-creates schemas; target user needs CREATE on database. For Postgres: `GRANT CREATE ON DATABASE appdb TO dms_user;` |
| Postgres CDC stops after a few hours | Replication slot bloat | Set `wal_keep_size=2048` (MB) and monitor `pg_replication_slots.confirmed_flush_lsn` lag. If slot can't keep up, increase DCU. |
| MySQL CDC stops with "binlog purged" | Binlog retention too short | `CALL mysql.rds_set_configuration('binlog retention hours', 72);` (RDS) or `expire_logs_days = 7` (self-managed) |
| Validation reports row mismatches | Source had open transaction at full-load cutoff | Re-run validation; if persists, check for clock skew between source + target |
| S3 target writes 0-byte files | `BatchApplyEnabled=true` + low write rate | Set `BatchApplyTimeoutMax=300` so DMS waits longer to fill batches |
| DMS Serverless DCU cost spike | One large table dominates parallelism | Add a selection rule with parallel-load partition: `parallel-load.type=ranges` with explicit ranges |
| LOB columns truncated | Default LOB mode is "limited 32KB" | Set `Inline LOB Max Size: 64` and `LOB column settings: Full LOB Mode` (slower but complete) |

### 6.1 Decision matrix vs alternatives

| Need | DMS | Aurora zero-ETL | Glue Connector | AppFlow |
|---|---|---|---|---|
| Aurora→Redshift CDC | ✅ works | ✅ **best** | ❌ | ❌ |
| Oracle→Aurora migration | ✅ **best** | ❌ no | ⚠️ JDBC-only, no CDC | ❌ |
| MySQL→S3 (Iceberg landing) | ✅ **best** | ❌ no | ✅ scheduled snapshots | ❌ |
| MongoDB→DocumentDB | ✅ **best** | ❌ no | ❌ | ❌ |
| Salesforce→S3 | ❌ no | ❌ no | ⚠️ deprecated | ✅ **best** |
| Snowflake→S3 | ❌ no | ❌ no | ✅ JDBC | ✅ **best** |
| Real-time stream (sub-second) | ❌ 5-min lag | ❌ 5-15 min | ❌ batch | ❌ |
| Schema conversion required | ⚠️ table mappings only | ❌ no | ❌ | ❌ |
| → use Schema Conversion Tool (SCT) for DDL conversion | | | | |

---

## 7. Worked example — pytest synth harness

Save as `tests/sop/test_DATA_DMS_REPLICATION.py`:

```python
"""SOP verification — DmsClassicToS3Stack synth contains rep instance,
S3 target endpoint, replication task, and IAM service role."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam, aws_kms as kms, aws_s3 as s3
from aws_cdk.assertions import Template, Match


def test_dms_classic_to_s3_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=2,
                  subnet_configuration=[ec2.SubnetConfiguration(
                      name="iso", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)])
    dms_sg = ec2.SecurityGroup(deps, "DmsSg", vpc=vpc)
    key = kms.Key(deps, "Key")
    raw = s3.Bucket(deps, "Raw")
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.dms_classic_to_s3_stack import DmsClassicToS3Stack
    stack = DmsClassicToS3Stack(
        app, stage_name="dev",
        vpc=vpc, dms_sg=dms_sg, kms_key=key,
        raw_bucket=raw, permission_boundary=boundary,
        env=env,
    )
    t = Template.from_stack(stack)

    # Replication instance
    t.has_resource_properties("AWS::DMS::ReplicationInstance", Match.object_like({
        "EngineVersion": "3.5.4",
        "MultiAZ": False,                     # dev
        "PubliclyAccessible": False,
    }))

    # Source + target endpoints (2 total)
    t.resource_count_is("AWS::DMS::Endpoint", 2)
    t.has_resource_properties("AWS::DMS::Endpoint", Match.object_like({
        "EndpointType": "target",
        "EngineName":   "s3",
        "S3Settings": Match.object_like({
            "DataFormat":            "parquet",
            "ParquetVersion":        "parquet-2-0",
            "DatePartitionEnabled":  True,
            "EncryptionMode":        "SSE_KMS",
        }),
    }))

    # Replication task with full-load-and-cdc
    t.has_resource_properties("AWS::DMS::ReplicationTask", Match.object_like({
        "MigrationType": "full-load-and-cdc",
    }))

    # IAM service role for DMS to S3
    t.resource_count_is("AWS::IAM::Role", Match.greater_than_or_equal(1))


def test_dms_serverless_homogeneous_synthesizes():
    """Verifies the DMS Serverless homogeneous CFN resources."""
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    # ... similar setup ...

    from infrastructure.cdk.stacks.dms_serverless_stack import DmsServerlessStack
    stack = DmsServerlessStack(app, stage_name="dev", env=env, ...)
    t = Template.from_stack(stack)

    t.resource_count_is("AWS::DMS::DataProvider", 2)        # source + target
    t.resource_count_is("AWS::DMS::MigrationProject", 1)
    t.resource_count_is("AWS::DMS::DataMigration", 1)
    t.has_resource_properties("AWS::DMS::DataMigration", Match.object_like({
        "DataMigrationType": "full-load-and-cdc",
    }))
```

---

## 8. Five non-negotiables (cite in every Claude-generated DMS code)

These extend the global `LAYER_BACKEND_LAMBDA §4.1` non-negotiables.

1. **Subnet group MUST use isolated subnets.** DMS replication instance + Serverless replication need to talk to source via PrivateLink/VPN and target via VPC-internal endpoint. Public subnets create cross-account / cross-vpc footguns. `subnet_type=ec2.SubnetType.PRIVATE_ISOLATED` always.

2. **Source + target credentials MUST come from Secrets Manager**, never inline. `oracle_settings.username`/`password` accept inline strings — DO NOT use them. Use `secrets_manager_secret_id` + `secrets_manager_access_role_arn` even though it's verbose.

3. **For S3 target, KMS-encrypted output is mandatory.** Set `encryption_mode="SSE_KMS"` + `server_side_encryption_kms_key_id`. Never use SSE-S3 for DMS-landed data — it's likely PII-bearing source data.

4. **CDC start position MUST be persisted to SSM after each successful run.** DMS won't resume from "last successful" automatically — if the task fails and you restart without `cdc_start_position`, it starts from "now" and you lose hours of changes. Wrap the DescribeReplicationTask response in a Lambda that writes `cdc_start_position` to SSM.

5. **Validation MUST be enabled in production.** `ValidationSettings.EnableValidation=true` with `ValidationMode=ROW_LEVEL`. Without validation, DMS silently drops rows if it can't apply them — you find out months later in a client audit.

---

## 9. References

- `docs/template_params.md` — `DMS_ENGINE_VERSION`, `DMS_INSTANCE_CLASS`, `DMS_MULTI_AZ`, `DMS_S3_DATE_PARTITION`, `DMS_VALIDATION_MODE`, `DMS_BATCH_APPLY_ENABLED`
- `docs/Feature_Roadmap.md` — feature IDs `DMS-01` (Serverless homogeneous), `DMS-02` (classic heterogeneous), `DMS-03` (S3 lakehouse target), `DMS-04` (CDC checkpoint persistence)
- AWS docs:
  - [DMS Serverless homogeneous data migrations](https://docs.aws.amazon.com/dms/latest/userguide/dm-migrating-data.html)
  - [DMS classic replication task](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.html)
  - [DMS S3 target endpoint](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Target.S3.html)
  - [DMS task settings reference](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.html)
  - [Postgres source prerequisites](https://docs.aws.amazon.com/dms/latest/userguide/dm-migrating-data-postgresql.html)
  - [MySQL source prerequisites](https://docs.aws.amazon.com/dms/latest/userguide/dm-migrating-data-mysql.html)
- Related SOPs:
  - `DATA_ZERO_ETL` — Aurora/RDS/DDB → Redshift/OpenSearch (managed CDC, prefer when applicable)
  - `DATA_LAKEHOUSE_ICEBERG` — MERGE-based CDC replay from raw zone into Iceberg curated tables
  - `DATA_GLUE_CATALOG` — auto-crawl DMS S3 output into Glue Catalog
  - `DATA_AURORA_SERVERLESS_V2` — typical target engine for heterogeneous migrations
  - `LAYER_NETWORKING` — VPC peering / PrivateLink for source connectivity
  - `LAYER_SECURITY` — KMS CMK + Secrets Manager rotation
  - `OPS_ADVANCED_MONITORING` — DMS task CloudWatch metrics (CDCLatencySource, CDCLatencyTarget, CDCThroughputBandwidthSource, FullLoadThroughputBandwidthTarget)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — covers BOTH the modern DMS Serverless homogeneous path (GA 2024, native pg_dump/mydumper/mongodump) AND the classic replication-instance path for heterogeneous + S3 lakehouse landing. Decision tree vs Aurora zero-ETL / Glue Connector / AppFlow. Full CDK for both variants, table-mappings + transformation-rules JSON examples, Iceberg MERGE pattern for CDC replay, engine-specific gotchas (Postgres logical replication, MySQL binlog, MongoDB ChangeStream), 5 DMS-specific non-negotiables, pytest synth harness. Created to fill the highest-priority gap surfaced by the F369 data-ecosystem audit (2026-04-26): traditional BI/multi-DB/migration engagements blocked at 40% coverage. |
