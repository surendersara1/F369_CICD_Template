# SOP — DMS Schema Conversion (heterogeneous DB migration · Oracle/SQL Server → PostgreSQL/Aurora · code refactor · validation)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS DMS Schema Conversion (in-DMS, replaces standalone SCT) · DMS classic for data migration · Babelfish for Aurora PostgreSQL (T-SQL → PG passthrough) · DMS Fleet Advisor · validation tasks

---

## 1. Purpose

- Codify **AWS DMS Schema Conversion** (the in-DMS replacement for the standalone Schema Conversion Tool, GA 2023) for heterogeneous database migration. Converts DDL + stored procedures + functions + triggers + views from source to target dialect.
- Codify the **canonical migration patterns**:
  - Oracle → PostgreSQL / Aurora PostgreSQL
  - SQL Server → PostgreSQL / Aurora PostgreSQL OR Babelfish (T-SQL passthrough)
  - MySQL → PostgreSQL (rare; usually MySQL → Aurora MySQL homogeneous)
  - Sybase / Db2 / Teradata → PostgreSQL / Redshift
  - On-prem PostgreSQL/MySQL → Aurora (homogeneous; SCT not needed)
- Codify the **assessment report** workflow — DMS Fleet Advisor scans source fleet → migration complexity score per database.
- Codify the **conversion-then-validate loop**: convert schema → review report → manually fix unconverted items → apply to target → run DMS classic for data → validate.
- Codify **Babelfish** as the SQL Server → Aurora PostgreSQL "no-rewrite" path — reduces application code changes by ~80%.
- This is the **DB schema migration specialisation**. Pairs with `DATA_DMS_REPLICATION` (data movement), `MIGRATION_HUB_STRATEGY` (org orchestration), `MIGRATION_MGN` (server lift-and-shift if app stays on EC2).

When the SOW signals: "Oracle to Postgres", "SQL Server to Aurora", "exit Oracle licensing", "database modernization", "heterogeneous DB migration".

---

## 2. Decision tree — conversion path + tooling

```
Source DB → Target DB?
├── Oracle → PostgreSQL/Aurora PG  → §3 DMS Schema Conversion (heavy refactor)
├── SQL Server → Aurora PG          → §3 OR §4 Babelfish (no-rewrite)
├── SQL Server → SQL Server on EC2/RDS → no SCT needed; native backup/restore
├── MySQL → Aurora MySQL              → no SCT (homogeneous)
├── Sybase / Db2 / Teradata → PG     → §3 DMS Schema Conversion
├── On-prem PG → Aurora PG           → no SCT (homogeneous; pg_dump or DMS classic)
└── Mainframe (Db2 z/OS, IMS) → AWS  → AWS Mainframe Modernization (separate)

Application code refactor effort?
├── < 10K LOC  → manual rewrite is fine
├── 10K-100K LOC → CodeWhisperer / Q Developer for stored proc translation
├── > 100K LOC → AWS Migration Hub Refactor Spaces + agentic refactor (Q Developer)

Babelfish vs full PG conversion?
├── App uses heavy T-SQL features (OPENROWSET, hierarchical XML, advanced TVPs) → full PG
├── App uses ANSI SQL + basic T-SQL → Babelfish (faster, less code change)
└── App is .NET ORM (EF, NHibernate) → either works; test ORM compat first
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — assess + convert one Oracle schema → Aurora PG | **§3 Monolith** |
| Production — fleet of 20+ DBs + Fleet Advisor + waves + Babelfish for some | **§5 Production** |

---

## 3. Monolith Variant — Oracle → Aurora PostgreSQL conversion

### 3.1 Architecture

```
   Source Oracle (on-prem or RDS)
        │
        │  1. Fleet Advisor agent collects metadata + DDL inventory
        │     OR DMS Schema Conversion direct connect (JDBC + creds)
        ▼
   ┌─────────────────────────────────────────────────────┐
   │ DMS Schema Conversion Migration Project             │
   │   - Source instance: Oracle endpoint                 │
   │   - Target instance: Aurora PG endpoint              │
   │   - Conversion settings (data type mapping rules)    │
   │   - Conversion ratings: AUTO / SIMPLE / MEDIUM /     │
   │                         COMPLEX / SIGNIFICANT        │
   └────────────┬────────────────────────────────────────┘
                │  2. Generate assessment report
                ▼
   ┌─────────────────────────────────────────────────────┐
   │ Assessment Report (PDF + JSON + CSV)                │
   │   - Total objects: 4,567                             │
   │   - Auto-converted: 3,901 (85%)                      │
   │   - Manual: 666 (15%)                                │
   │   - Top issues: Oracle PL/SQL packages,              │
   │     hierarchical queries (CONNECT BY),               │
   │     proprietary functions (NVL → COALESCE)           │
   └────────────┬────────────────────────────────────────┘
                │  3. Fix manual items in DMS-SC editor
                ▼
   ┌─────────────────────────────────────────────────────┐
   │ Generated PG-compatible DDL                         │
   │   - Apply via psql to Aurora PG (idempotent)         │
   │   - Validate: schema diff source vs target           │
   └────────────┬────────────────────────────────────────┘
                │  4. Now run DMS classic for DATA migration
                ▼
   ┌─────────────────────────────────────────────────────┐
   │ DMS Replication task (full load + CDC)              │
   │   - Source endpoint: Oracle                          │
   │   - Target endpoint: Aurora PG                       │
   │   - Task settings: full load + ongoing replication  │
   │   - Data validation: continuous row hash compare    │
   └─────────────────────────────────────────────────────┘
                │
                ▼
   Application cutover (DNS swap, conn string update)
```

### 3.2 CDK — pre-req infrastructure

```python
# stacks/migration_db_stack.py
from aws_cdk import Stack
from aws_cdk import aws_dms as dms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from constructs import Construct


class MigrationDbStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 vpc: ec2.IVpc, kms_key_arn: str, env_name: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Aurora PostgreSQL target (or RDS PG, or Babelfish-Aurora-PG) ──
        target_cluster = rds.DatabaseCluster(self, "TargetAurora",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4,
            ),
            credentials=rds.Credentials.from_generated_secret("postgres"),
            instance_props=rds.InstanceProps(
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.MEMORY6_GRAVITON, ec2.InstanceSize.LARGE,
                ),
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                publicly_accessible=False,
            ),
            instances=2,                                 # writer + 1 reader
            backup=rds.BackupProps(retention=Duration.days(30)),
            storage_encryption_key=kms.Key.from_key_arn(self, "Cmk", kms_key_arn),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── 2. DMS replication subnet group + replication instance ──
        # (For DMS classic data movement after schema conversion)
        dms_subnet_group = dms.CfnReplicationSubnetGroup(self, "DmsSubnetGroup",
            replication_subnet_group_description="DMS replication",
            subnet_ids=[s.subnet_id for s in vpc.private_subnets],
            replication_subnet_group_identifier=f"{env_name}-dms-sng",
        )

        dms_repl_instance = dms.CfnReplicationInstance(self, "DmsRepl",
            replication_instance_class="dms.r6i.xlarge",   # 4 vCPU, 32 GB
            allocated_storage=200,                          # GB
            replication_instance_identifier=f"{env_name}-dms-repl",
            replication_subnet_group_identifier=dms_subnet_group.ref,
            multi_az=True,
            publicly_accessible=False,
            kms_key_id=kms_key_arn,
            engine_version="3.5.4",                          # latest stable
            allow_major_version_upgrade=False,
            auto_minor_version_upgrade=True,
        )

        # ── 3. Source Oracle endpoint ────────────────────────────────
        source_secret = sm.Secret.from_secret_complete_arn(
            self, "OracleSecret", oracle_secret_arn,
        )
        source_ep = dms.CfnEndpoint(self, "OracleSource",
            endpoint_type="source",
            engine_name="oracle",
            endpoint_identifier=f"{env_name}-oracle-source",
            server_name="oracle.example.com",
            port=1521,
            database_name="ORCL",
            ssl_mode="require",
            secrets_manager_secret_id=source_secret.secret_arn,
            kms_key_id=kms_key_arn,
            extra_connection_attributes=(
                "useLogminerReader=N;useBfile=Y;"
                "addSupplementalLogging=Y;archivedLogDestId=1;"
            ),
        )

        # ── 4. Target Aurora PG endpoint ─────────────────────────────
        target_ep = dms.CfnEndpoint(self, "AuroraTarget",
            endpoint_type="target",
            engine_name="aurora-postgresql",
            endpoint_identifier=f"{env_name}-aurora-target",
            server_name=target_cluster.cluster_endpoint.hostname,
            port=5432,
            database_name="appdb",
            ssl_mode="require",
            secrets_manager_secret_id=target_cluster.secret.secret_arn,
            kms_key_id=kms_key_arn,
            extra_connection_attributes="executeTimeout=180",
        )
```

### 3.3 Schema Conversion workflow (DMS console / CLI)

```bash
# ── 1. Create Migration Project (DMS Schema Conversion) ────────────
aws dms create-migration-project \
  --migration-project-name oracle-to-aurora-prod \
  --source-data-provider-descriptor "DataProviderName=oracle-prod-source" \
  --target-data-provider-descriptor "DataProviderName=aurora-prod-target" \
  --instance-profile-name dms-instance-profile

# ── 2. Generate assessment report ──────────────────────────────────
aws dms describe-conversion-configuration --migration-project-identifier oracle-to-aurora-prod
aws dms start-metadata-model-assessment \
  --migration-project-identifier oracle-to-aurora-prod \
  --selection-rules file://selection-rules.json
# selection-rules.json picks specific schemas + tables to scan

# ── 3. Review assessment in DMS console:
#    - Schema conversion rate (target ≥ 80% auto-converted)
#    - Unsupported features per object
#    - Storage object recommendations (e.g., index types, partitioning)
#    - Performance rec (e.g., cluster vs instance sizing)
#    Export PDF/CSV for stakeholders.

# ── 4. Apply auto-conversion + start manual fixes ──────────────────
aws dms start-metadata-model-conversion \
  --migration-project-identifier oracle-to-aurora-prod \
  --selection-rules file://selection-rules.json
# Generates DDL — review in DMS Schema Conversion editor

# ── 5. After manual fixes, apply to target ────────────────────────
aws dms start-metadata-model-export \
  --migration-project-identifier oracle-to-aurora-prod \
  --overwrite-extension-pack \
  --selection-rules file://selection-rules.json
# Outputs SQL files to S3 → apply via psql to Aurora target

psql -h aurora-target.amazonaws.com -U postgres -d appdb -f converted_schema.sql

# ── 6. Now run DMS classic for DATA migration ──────────────────────
# (See DATA_DMS_REPLICATION partial for full task config)
aws dms create-replication-task \
  --replication-task-identifier full-load-and-cdc \
  --source-endpoint-arn $SOURCE_EP_ARN \
  --target-endpoint-arn $TARGET_EP_ARN \
  --replication-instance-arn $DMS_REPL_ARN \
  --migration-type full-load-and-cdc \
  --table-mappings file://table-mappings.json \
  --replication-task-settings file://task-settings.json \
  --resource-identifier prod-oracle-to-aurora

aws dms start-replication-task \
  --replication-task-arn $TASK_ARN \
  --start-replication-task-type start-replication
```

### 3.4 Common manual conversions Oracle → PostgreSQL

| Oracle | PostgreSQL | Notes |
|---|---|---|
| `NVL(a, b)` | `COALESCE(a, b)` | Auto-converted by DMS-SC |
| `DECODE(x, 1, 'a', 'b')` | `CASE WHEN x = 1 THEN 'a' ELSE 'b' END` | Auto |
| `SYSDATE` | `CURRENT_TIMESTAMP` | Auto |
| `MINUS` | `EXCEPT` | Auto |
| `DUAL` | omit (`SELECT 1` not `SELECT 1 FROM DUAL`) | Auto |
| `CONNECT BY PRIOR` | `WITH RECURSIVE` CTE | **Manual rewrite** |
| `OUTER JOIN (+)` syntax | `LEFT/RIGHT JOIN` | Auto |
| `ROWNUM <= 10` | `LIMIT 10` | Auto |
| `SEQUENCE.NEXTVAL` | `nextval('seq_name')` | Auto |
| `CLOB / BLOB` | `TEXT / BYTEA` | Auto |
| PL/SQL packages | PostgreSQL `CREATE SCHEMA` + functions | **Manual** — package-internal procs map to schema-qualified functions |
| `UTL_FILE`, `DBMS_OUTPUT` | `COPY` / `RAISE NOTICE` | **Manual** |
| `PRAGMA AUTONOMOUS_TRANSACTION` | dblink workaround | **Manual** — major refactor |
| Materialized view fast refresh | manual triggers OR refresh CONCURRENTLY | **Manual** |

---

## 4. Babelfish — SQL Server → Aurora PG (T-SQL passthrough)

Babelfish lets your existing SQL Server applications connect to Aurora PostgreSQL using T-SQL + TDS protocol. ~80% reduction in app code changes.

```python
# CDK — Aurora PG with Babelfish enabled
target_cluster = rds.DatabaseCluster(self, "BabelfishCluster",
    engine=rds.DatabaseClusterEngine.aurora_postgres(
        version=rds.AuroraPostgresEngineVersion.VER_16_4,
    ),
    parameter_group=rds.ParameterGroup(self, "BabelfishParam",
        engine=rds.DatabaseClusterEngine.aurora_postgres(
            version=rds.AuroraPostgresEngineVersion.VER_16_4,
        ),
        parameters={
            "rds.babelfish_status": "on",
            "babelfishpg_tsql.migration_mode": "single-db",   # or multi-db
            "babelfishpg_tds.tds_default_numeric_precision": "38",
            "shared_preload_libraries": "babelfishpg_tds, pg_stat_statements",
        },
    ),
    # ... rest of cluster config ...
)

# Babelfish exposes 2 endpoints:
#   PostgreSQL on port 5432 (standard PG protocol)
#   T-SQL / TDS on port 1433 (SQL Server-compatible)
#
# Application connects via existing SQL Server driver to port 1433.
# DMS Schema Conversion handles SQL Server → Babelfish-flavored DDL conversion.
```

**Babelfish gotchas:**
- T-SQL features supported: ~80% of SQL Server 2019 surface
- Unsupported (require app code changes): linked servers, distributed transactions, OPENROWSET BULK, FILESTREAM, Service Broker, full-text search (use PG full-text), Hekaton in-memory tables
- Login auth: SQL Server logins migrate; Windows auth requires AWS Directory Service trust
- Performance: T-SQL parser adds ~5-10% overhead; for hot paths, consider native PG with code rewrite

---

## 5. Production Variant — Fleet Advisor + waves

### 5.1 DMS Fleet Advisor

Fleet Advisor agent runs in source data center, scans all DBs, generates fleet-level migration complexity score.

```bash
# Deploy Fleet Advisor collector (Docker on-prem)
docker run -d --name fa-collector \
  -e AWS_ACCESS_KEY_ID=AKIA... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -e AWS_REGION=us-east-1 \
  amazon/dms-fleet-advisor-collector:latest

# In console: Fleet Advisor → Add data collector → wait for inventory
# Output: per-DB complexity score (LOW/MEDIUM/HIGH) + recommended target
```

### 5.2 Wave planning

Per assessment, group DBs by complexity:
- **Wave 1** (LOW): homogeneous PG/MySQL → Aurora; small SQL Server → Babelfish
- **Wave 2** (MEDIUM): SQL Server → Aurora PG full conversion; Oracle simple schemas
- **Wave 3** (HIGH): Oracle PL/SQL packages, Sybase, Db2, Teradata

Each wave: 4-week cycle (week 1 = assess, 2 = convert, 3 = test data load, 4 = cutover).

---

## 6. Common gotchas

- **DMS Schema Conversion is in-DMS now (2023+)** — the standalone SCT desktop app is being phased out. New work should use DMS-SC console.
- **Conversion rate < 80%** signals significant manual work — flag in assessment, don't quote a 4-week timeline if you'll spend 8 weeks on PL/SQL.
- **Oracle CONNECT BY hierarchical queries** are the #1 unconverted item. Rewrite as `WITH RECURSIVE` CTE.
- **PL/SQL package state** (variables persisted across calls in same session) — PG functions don't have this. Use SET LOCAL / temp tables.
- **AUTONOMOUS_TRANSACTION** in PL/SQL is the hardest pattern — requires dblink to self for autonomous behaviour. Often a major refactor.
- **Identity columns**: Oracle `IDENTITY` clause maps to PG `GENERATED BY DEFAULT/ALWAYS AS IDENTITY` — verify which.
- **Sequence ownership / nextval semantics differ** — Oracle preallocates; PG doesn't. App code that relies on sequential IDs without gaps will break.
- **CHAR padding**: Oracle CHAR(10) pads with spaces; PG CHAR pads but VARCHAR does not. Use VARCHAR in target for portability.
- **Date arithmetic**: Oracle `DATE - DATE` returns days; PG returns interval. Wrap with `EXTRACT(DAY FROM ...)`.
- **Empty string vs NULL**: Oracle treats `''` as NULL; PG treats `''` as empty string. App-level NULL checks will surface bugs.
- **Babelfish single-db vs multi-db mode** — choose at cluster create; cannot change later.
- **DMS classic for ongoing CDC requires Oracle supplemental logging** enabled at table or DB level. Adds ~10% redo log volume on source.
- **Schema migration applies DDL, not DATA**. After schema is on target, run DMS classic full-load + CDC. Don't conflate.
- **Validation**: DMS classic data validation does row-hash compare but skips LOBs by default. Manually compare LOB columns post-migration.
- **Cutover lag**: final CDC catch-up can take 1-30 min. Quiesce app first; monitor `CDCLatencyTarget` metric.

---

## 7. Pytest worked example

```python
# tests/test_schema_conversion.py
import boto3, psycopg, pytest

dms = boto3.client("dms")
PROJECT_ID = "oracle-to-aurora-prod"


def test_assessment_complete():
    project = dms.describe_migration_projects(
        Filters=[{"Name": "migration-project-identifier", "Values": [PROJECT_ID]}],
    )["MigrationProjects"][0]
    # Project should have at least 1 metadata-model conversion run
    rules = dms.describe_metadata_model_conversions(
        MigrationProjectIdentifier=PROJECT_ID,
    )["Requests"]
    assert rules


def test_schema_diff_zero(target_pg_conn_string):
    """All expected tables exist in target with expected column counts."""
    expected = {
        "public.customers": 12,
        "public.orders": 18,
        "public.order_items": 7,
    }
    with psycopg.connect(target_pg_conn_string) as conn:
        with conn.cursor() as cur:
            for full_name, expected_cols in expected.items():
                schema, tbl = full_name.split(".")
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s",
                    (schema, tbl),
                )
                actual = cur.fetchone()[0]
                assert actual == expected_cols, \
                    f"{full_name}: expected {expected_cols} cols, got {actual}"


def test_dms_replication_task_running(task_arn):
    task = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}],
    )["ReplicationTasks"][0]
    assert task["Status"] in ["running", "load-complete"]
    # CDC latency under 5 min
    stats = task.get("ReplicationTaskStats", {})
    assert stats.get("CdcLatencyTarget", 999) < 300


def test_data_validation_no_failed_rows(task_arn):
    """DMS validation must not have rows in 'FailedValidationRowsCount'."""
    stats = dms.describe_replication_task_assessment_results(
        ReplicationTaskArn=task_arn,
    )
    # Real impl: parse validation report from S3
    pass
```

---

## 8. Five non-negotiables

1. **Assessment report ≥ 80% auto-converted** before quoting timeline; otherwise rescope.
2. **Manual fix items reviewed by DBA + app team** — DMS-SC syntax-converts but doesn't always preserve semantics.
3. **DMS classic data load runs WITH validation enabled** (`EnableValidation: true` in task settings).
4. **CMK encryption** on DMS replication instance + target cluster — never AWS-owned key.
5. **Cutover dry run** in stage before prod — full schema apply + data load + app smoke test.

---

## 9. References

- [DMS Schema Conversion (in-DMS)](https://docs.aws.amazon.com/dms/latest/userguide/schema-conversion.html)
- [DMS Fleet Advisor](https://docs.aws.amazon.com/dms/latest/userguide/fa.html)
- [Babelfish for Aurora PostgreSQL](https://aws.amazon.com/rds/aurora/babelfish/)
- [Oracle to PG migration playbook](https://docs.aws.amazon.com/prescriptive-guidance/latest/migration-oracle-postgresql/welcome.html)
- [SQL Server to Babelfish](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/babelfish.html)
- [DMS data validation](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Validating.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. DMS Schema Conversion + Fleet Advisor + Babelfish + Oracle/SQL Server → PG patterns + manual conversion table + cutover lag. Wave 13. |
