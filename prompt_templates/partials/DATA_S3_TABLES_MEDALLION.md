# SOP — Amazon S3 Tables for Medallion (Bronze + Silver)

**Version:** 1.0 · **Last-reviewed:** 2026-05-12 · **Status:** Active
**Applies to:** Amazon S3 Tables · AWS Glue 5.0+ · Apache Iceberg 1.5+ · AWS Glue Data Catalog (federated `s3tablescatalog`) · AWS Lake Formation · Amazon Redshift Serverless · CDKTF `cdktf-cdktf-provider-aws ~> 19.x` (`aws_s3tables_*` resources)

**Validated against AWS docs 2026-05-12** via the AWS Documentation MCP. S3 Tables GA in eu-west-1 (Ireland) and all major regions; managed compaction / snapshot expiry / orphan removal; federated catalog wires Glue/Athena/Redshift/EMR/SageMaker Lakehouse.

---

## 1. Purpose

Codify how the team uses **Amazon S3 Tables (managed Iceberg)** as the storage layer for Bronze and Silver in a medallion lakehouse. Distinct from self-managed Iceberg on regular S3 — S3 Tables means AWS owns compaction, snapshot expiration, and unreferenced-file removal as managed jobs.

When the SOW signals: "lakehouse for BI", "single source of truth across 3+ sources", "Power BI / Tableau consumer", "we don't want to operate our own compaction" → **use S3 Tables for Bronze + Silver**. Pair with `DATA_DBT_REDSHIFT_SERVERLESS` for Silver→Gold when Gold is Redshift-native.

When **NOT** to use S3 Tables (fall back to self-managed Iceberg via `DATA_LAKEHOUSE_ICEBERG`):
- Customer's region doesn't support S3 Tables yet.
- Need `brotli` / `lz4` compression on compacted files (S3 Tables doesn't support).
- Need Iceberg `Fixed` data type.
- Need branch/tag-based snapshot retention via `ALTER TABLE TBLPROPERTIES`.
- Need >10 table buckets per account per region without raising a quota.

---

## 2. The S3 Tables resource model

```
aws_s3tables_table_bucket          (1 per medallion layer per env)
   │
   ├── aws_s3tables_namespace      (1 per source system per layer)
   │       │
   │       └── (tables — created on first write by Glue/Iceberg DDL; NOT defined in CDKTF)
   │
   ├── aws_s3tables_table_bucket_maintenance_configuration   (default-on; tune `unreferencedDays`)
   ├── aws_s3tables_table_bucket_policy                       (cross-account access if needed)
   └── encryption_configuration → CMK ARN (SSE-KMS)
```

| Resource | Created by | Lifecycle |
|---|---|---|
| Table buckets | CDKTF (`infra/constructs/lakehouse_s3tables.py`) | One per (layer × env). Stable. |
| Namespaces | CDKTF, same construct | One per source system. Stable. |
| Tables | Glue jobs on first write via Iceberg `CREATE TABLE` | Per spec; lifecycle is the spec lifecycle. |
| Maintenance config | CDKTF | Per bucket OR per table. Defaults are good for most cases. |

---

## 3. Catalog federation (the line that wires it all together)

S3 Tables exposes a single Glue federated catalog: **`s3tablescatalog`**. All engines (Glue, Athena, Redshift, EMR, SageMaker Lakehouse) see S3 Tables namespaces as if they were Glue databases.

```python
# infra/constructs/lakehouse_glue.py — register the federated catalog (one-time per account)
S3tablesNamespace(self, "register_bronze", ...)
# After CDKTF apply, Spark/Athena/Redshift can address tables as:
#   SELECT * FROM "s3tablescatalog/<bucket-name>"."<namespace>"."<table>"
# In Spark / Iceberg SQL:
#   SELECT * FROM s3tablescatalog.<bucket>.<namespace>.<table>
```

**Important:** Glue 5.0 auto-mounts the S3 Tables catalog as a Spark `SparkCatalog`. No additional JARs or client libraries required. Glue 4.0 needs the explicit `S3TablesCatalog` impl-class JAR — prefer Glue 5.0.

---

## 4. Managed maintenance (the value prop)

Three jobs run automatically by AWS on every table bucket; all configurable via `aws_s3tables_table_bucket_maintenance_configuration`:

| Job | Default | Tune via |
|---|---|---|
| **Compaction** | Auto strategy (`binpack` by default, target 512 MB) | `aws_s3tables_table_maintenance_configuration` per table to override strategy (`sort`, `z-order`) or target size (64 MB–512 MB) |
| **Snapshot expiration** | Keep min 1 snapshot / max 120h age | `minimumSnapshots` (≥1), `maximumSnapshotAge` (≥1h) |
| **Unreferenced file removal** | 3 days `unreferencedDays`, 10 days `nonCurrentDays` | Per bucket |

### Compaction strategy choice (per table)

| Strategy | When to use | Cost |
|---|---|---|
| `auto` (default) | Most tables — S3 picks `sort` if table has sort_order, else `binpack` | Lowest |
| `binpack` | Append-heavy tables with no consistent filter column (Bronze raw logs, event streams) | Low |
| `sort` | Tables filtered hot on a few columns (sales by date+store; orders by customer_id) | Medium |
| `z-order` | Tables filtered across many dimensions (analytics-grade Silver/Gold facts) | Higher |

**Sort key requires** `sort_order` defined as a table property + `s3tables:GetTableData` permission on the maintenance service principal. Wire this in the Glue job's `CREATE TABLE` DDL: `... TBLPROPERTIES ('sort_order' = 'transaction_date ASC NULLS LAST, store_id ASC NULLS LAST')`.

### When to disable maintenance per table

Almost never. Cases:
- The table is wide-but-write-once and you don't want compaction churn (e.g. one-time backfill).
- You're using Iceberg `ALTER TABLE TBLPROPERTIES` branch retention — S3 Tables snapshot management disables itself when it sees branches.

Disable via `aws_s3tables_table_maintenance_configuration` `{ status: "disabled" }`.

---

## 5. Encryption (SOC 2 / PII)

S3 Tables supports **SSE-KMS with customer-managed CMKs**. Required permissions to grant on your KMS key (per `aws-s3tables-kms-permissions` doc):

```jsonc
// Resource-policy snippet on your `bronze` CMK
{
  "Statement": [
    {
      "Sid": "AllowS3TablesMaintenanceService",
      "Effect": "Allow",
      "Principal": { "Service": "maintenance.s3tables.amazonaws.com" },
      "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
      "Resource": "*"
    },
    {
      "Sid": "AllowS3MetadataService",
      "Effect": "Allow",
      "Principal": { "Service": "metadata.s3.amazonaws.com" },
      "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
      "Resource": "*"
    },
    {
      "Sid": "AllowGlueAndRedshiftReaders",
      "Effect": "Allow",
      "Principal": { "AWS": [GLUE_ROLE_ARN, REDSHIFT_ROLE_ARN] },
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": "*"
    }
  ]
}
```

**Anti-pattern:** trying to use AWS-managed (default) keys for a SOC 2 environment. Audit will fail.

---

## 6. Lake Formation integration (LF-TBAC on S3 Tables)

LF principal ARNs for S3 Tables use the `s3tables` ARN shape, not `s3`:

```
arn:aws:s3tables:<region>:<account>:bucket/<bucket-name>/table/<namespace>/<table>
```

Tag expression grants (Tamimi example):

```python
# infra/constructs/lakehouse_lakeformation.py
LakeformationPermissions(
    self, "finance_analyst_read_silver",
    principal={"data_lake_principal_identifier": finance_analyst_role_arn},
    lf_tag_policy={
        "resource_type": "TABLE",
        "expression": [
            {"key": "domain",       "values": ["finance"]},
            {"key": "sensitivity",  "values": ["public", "internal"]},
            {"key": "environment",  "values": ["prod"]},
        ],
    },
    permissions=["SELECT", "DESCRIBE"],
)
```

**Strict mode in Prod:** `CreateDatabaseDefaultPermissions=[]` + `CreateTableDefaultPermissions=[]` so new namespaces/tables created by Glue jobs are denied-by-default and require an explicit LF grant.

---

## 7. Replication

S3 Tables supports two granularities:

- **Bucket-level replication** — all tables in a source bucket replicate to a target bucket in another region/account.
- **Table-level replication** — selective. Per `aws_s3tables_table_replication` config.

DR use-case: Prod Bronze in eu-west-1 → DR target in eu-west-2. Lag: ~minutes (AWS-managed; no public SLA).

**Anti-pattern:** trying to use classic S3 Cross-Region Replication (`aws_s3_bucket_replication_configuration`) on an S3 Tables bucket. The bucket type doesn't accept it — use the S3 Tables replication API.

---

## 8. Quotas (verify at engagement scoping)

| Quota | Default | Raisable? |
|---|---|---|
| Table buckets per account per region | **10** | Yes — Support ticket |
| Namespaces per bucket | 10,000 | Yes |
| Tables per bucket | 10,000 | Yes |
| `minimumSnapshots` | min 1 | n/a |
| `maximumSnapshotAge` | 1 hour minimum | n/a |
| `unreferencedDays` | 1 day minimum | n/a |

Three medallion layers × three envs (Dev/QA/Prod) in account-per-env model = 2 buckets per env account (Bronze + Silver; Gold is Redshift). Easily under quota.

---

## 9. Required CDKTF resources (canonical shape)

```python
# infra/constructs/lakehouse_s3tables.py
from cdktf_cdktf_provider_aws.s3tables_table_bucket import S3tablesTableBucket
from cdktf_cdktf_provider_aws.s3tables_namespace import S3tablesNamespace
from cdktf_cdktf_provider_aws.s3tables_table_bucket_maintenance_configuration import \
    S3tablesTableBucketMaintenanceConfiguration

class LakehouseS3Tables(Construct):
    def __init__(self, scope, id, *, cfg, kms):
        super().__init__(scope, id)
        self.bronze = self._make_bucket("bronze", cfg, kms.bronze_key_arn)
        self.silver = self._make_bucket("silver", cfg, kms.silver_key_arn)
        self._make_namespaces(self.bronze, ["sap", "ncr", "ecommerce"], cfg, layer="bronze")
        self._make_namespaces(self.silver, ["sap", "ncr", "ecommerce"], cfg, layer="silver")

    def _make_bucket(self, layer, cfg, kms_arn):
        bucket = S3tablesTableBucket(
            self, f"{layer}_bucket",
            name=f"{cfg.client_slug}-dlh-{layer}-{cfg.env}",
            encryption_configuration={
                "sse_algorithm": "aws:kms",
                "kms_key_arn": kms_arn,
            },
        )
        S3tablesTableBucketMaintenanceConfiguration(
            self, f"{layer}_maint",
            table_bucket_arn=bucket.arn,
            type="icebergUnreferencedFileRemoval",
            value={
                "status": "enabled",
                "settings": {
                    "iceberg_unreferenced_file_removal": {
                        "unreferenced_days": cfg.s3tables_unreferenced_days,    # 3 default; 7 in Prod
                        "non_current_days": cfg.s3tables_noncurrent_days,       # 10 default
                    },
                },
            },
        )
        return bucket

    def _make_namespaces(self, bucket, names, cfg, layer):
        for name in names:
            S3tablesNamespace(
                self, f"{layer}_{name}",
                namespace=name,
                table_bucket_arn=bucket.arn,
            )
```

---

## 10. Patterns

- **Use `s3tablescatalog` everywhere.** Glue jobs, Athena workgroups, Redshift external schemas, Spark `SparkCatalog` config — all the same name.
- **Tables register themselves on first write.** Don't predeclare in CDKTF. The Glue engine's `CREATE TABLE ... USING iceberg` in spec-driven runs creates them lazily, and the federated catalog surfaces them automatically.
- **Sort key in Iceberg `TBLPROPERTIES`** to leverage `sort` compaction.
- **Lake Formation strict** in QA + Prod; tag-based grants only.
- **One CMK per layer**, never share. Bronze CMK ≠ Silver CMK.
- **Iceberg format-version 2** mandatory — required for row-level deletes / MERGE.
- **30-day snapshot retention on Silver** for reconciliation drills (`maximumSnapshotAge: 720`).
- **3-day retention on Bronze** snapshots (`maximumSnapshotAge: 72`) — Bronze is replayable from source; long retention is wasteful.
- **Preserve source column names verbatim at Bronze, including typos** (added 2026-05-19). If the source SAP / NCR / SaaS API returns columns named `Prom Catergory`, `Site Descriptio`, `Deprtment Name`, those names land in Bronze as-is. **Renames happen at Silver, never at Bronze.** Why: (1) reconciliation against source row-counts requires byte-identical schemas; (2) Bronze is the audit trail — a future "where did this number come from?" trace through CloudTrail + Bronze should produce the exact source-system column names; (3) source teams often own the schema and a "fixed" name in Bronze diverges from their docs / SAP transport.

---

## 11. Anti-patterns

- **Don't use S3 SDK / `boto3.client('s3')` to touch S3 Tables objects.** Iceberg APIs only. Direct overwrites corrupt the table.
- **Don't enable classic S3 bucket versioning.** S3 Tables manages versioning via Iceberg snapshots; the classic versioning is on the underlying S3 object store and would cause duplicate billing + confused snapshot expiration.
- **Don't use Glue Crawlers** against S3 Tables. The federated catalog tracks schemas natively; crawlers are for the legacy self-managed Iceberg path.
- **Don't set `sort_order` via `ALTER TABLE TBLPROPERTIES`** AND rely on S3 Tables snapshot management — branch-based properties disable S3 Tables auto-management; either use S3 Tables maintenance config OR Iceberg native, never both.
- **Don't use classic S3 Cross-Region Replication.** Use S3 Tables-native replication.
- **Don't predeclare tables in CDKTF.** First-write registration via Iceberg DDL is the right pattern; CDKTF only owns table buckets, namespaces, maintenance config.
- **Don't share CMKs across layers.** Blast-radius isolation matters.
- **Don't use RA3/DC2 Redshift to query S3 Tables.** RA3 incurs Spectrum surcharge. Use Redshift Serverless (workgroup compute, no surcharge).
- **Don't grant `IAMAllowedPrincipals` on S3 Tables resources.** Strict-mode LF only.
- **Don't fix SAP / source column-name typos at Bronze.** `Prom Catergory` stays `Prom Catergory` at Bronze; rename to `prom_category` happens at Silver. "Fixing" at Bronze breaks reconciliation against source-system row counts and diverges from source-system documentation.

---

## 12. Composes with

- `LAYER_BACKEND_LAMBDA` — Lambda non-negotiables (for `bronze_arrival` event router).
- `SERVERLESS_LAMBDA_POWERTOOLS` — Powertools on the bronze-arrival Lambda.
- `DATA_LAKE_FORMATION` — LF-TBAC strict mode (Prod).
- `DATA_ICEBERG_S3_TABLES` — Iceberg specifics (format-version 2, partition transforms).
- `IAC_CDKTF_PYTHON` — construct anatomy + state.
- `DATA_DBT_REDSHIFT_SERVERLESS` — companion for Silver→Gold when Gold is Redshift-native.
- `data/14_dynamic_glue_pyspark_medallion.md` — the engine that writes to these S3 Tables.
- `iac/05_cdktf_python_lakehouse.md` — the IaC template that emits the constructs above.

---

## 13. Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Glue 4.0 job can't see `s3tablescatalog` | S3 Tables catalog client not in Glue 4.0 | Upgrade to Glue 5.0 (auto-mounted) |
| Compaction never runs | Maintenance disabled at table or bucket level | `aws s3tables get-table-maintenance-configuration ...`; re-enable |
| Snapshot expiration not happening | Iceberg branch/tag retention overriding | Choose one: S3 Tables managed OR Iceberg native |
| KMS Decrypt errors in Glue job | Maintenance service principal missing from key policy | Add `maintenance.s3tables.amazonaws.com` to key policy |
| Redshift external schema can't read | RA3/DC2 incurring Spectrum surcharge OR Lake Formation grant missing | Switch to Redshift Serverless; verify LF principal grant |
| Iceberg time-travel queries fail from Redshift | Redshift doesn't support `TIMESTAMP AS OF` on Iceberg today | Use Athena or Spark for time-travel queries |
| Quota exceeded — "10 table buckets" | At the default quota | Raise via Support ticket; rare for engagement under 3 layers × 3 envs |
| Maintenance jobs running too aggressively (cost spike) | Default 120h snapshot age + frequent writes | Tune `minimumSnapshots` and `maximumSnapshotAge` per workload |

---

## 14. Acceptance criteria

A CDKTF stack composing this partial passes ALL of:

1. `aws s3tables list-table-buckets --region <region>` returns the Bronze + Silver buckets.
2. `aws s3tables list-namespaces --table-bucket-arn <bronze-arn>` lists `sap`, `ncr`, `ecommerce`.
3. `aws s3tables get-table-bucket-encryption --table-bucket-arn <bronze-arn>` returns the customer-managed CMK ARN — NOT `aws:kms` AWS-managed.
4. Glue federated catalog `s3tablescatalog` is registered: `aws glue get-catalogs` returns it.
5. Glue 5.0 job with `--conf spark.sql.catalog.s3tablescatalog=...` succeeds with `CREATE TABLE s3tablescatalog.<bucket>.<namespace>.<table> ...`.
6. From Redshift Serverless: `CREATE EXTERNAL SCHEMA bronze FROM DATA CATALOG DATABASE 's3tablescatalog/<bucket>'` succeeds, and `SELECT * FROM bronze.<namespace>.<table> LIMIT 1` returns data.
7. Lake Formation: an unauthorised principal's `SELECT` is denied with a CloudTrail event.
8. Maintenance config: `aws s3tables get-table-bucket-maintenance-configuration` returns the expected `unreferencedDays` per env.
9. Prod stack: `force_destroy` not set on any S3 Tables resource (S3 Tables doesn't have it as a knob — but verify nothing in CDKTF tries to delete a bucket with tables).
10. SOC 2 evidence: KMS key rotation enabled + 30-day deletion window + key policy grants the right service principals.
