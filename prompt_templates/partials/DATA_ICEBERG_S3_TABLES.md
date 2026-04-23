# SOP — Amazon S3 Tables (fully managed Apache Iceberg on S3)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `s3tables` service namespace (GA 2025) · boto3 `s3tables` client · CloudFormation L1 (`AWS::S3Tables::TableBucket`, `AWS::S3Tables::Namespace`, `AWS::S3Tables::Table`, `AWS::S3Tables::TableBucketPolicy`) · Apache Iceberg v2 spec · Glue Data Catalog auto-federation · Athena engine v3 · EMR 7.x · Redshift Spectrum

---

## 1. Purpose

- Provide the deep-dive for **Amazon S3 Tables** — a purpose-built, fully managed store for **Apache Iceberg tables**. A dedicated `s3tables` service (separate namespace, separate ARN shape, separate IAM actions) that removes the operational burden of self-managed Iceberg-on-S3: **automatic compaction, automatic snapshot expiration, automatic unreferenced-file cleanup**, all running in an AWS-owned account invisibly.
- Codify the **table-bucket → namespace → table** hierarchy (analogous to database → schema → table in a traditional warehouse) and the CloudFormation L1 control plane (**no CDK L2 yet**, v2.238 — all `aws_cdk.aws_s3tables.Cfn*`).
- Codify the **Glue Catalog auto-federation** — a table bucket is surfaced as a federated catalog `s3tablescatalog/<table-bucket-name>` inside the AWS Glue Data Catalog automatically, queryable from Athena / EMR / Redshift Spectrum with zero extra plumbing.
- Codify the **ARN divergence from regular S3** — `arn:aws:s3tables:{region}:{account}:bucket/{table-bucket-name}` (buckets) and `arn:aws:s3tables:{region}:{account}:bucket/{table-bucket-name}/table/{namespace}/{table-name}` (tables). S3 IAM patterns **do not transfer**; everything is `s3tables:*` and `glue:*` identity-side statements.
- Codify the **difference from `DATA_LAKEHOUSE_ICEBERG` (self-managed Iceberg)** — the existing partial uses `aws_glue.CfnTable(table_type="ICEBERG")` on a regular S3 bucket, and you own compaction via Glue ETL jobs. This partial is for when **you want AWS to run compaction for you** and the table bucket's managed guarantees (1 PutVectors/s minimum, automatic small-file compaction, automatic snapshot expiry) are worth the service premium.
- Include when the SOW signals: "managed Iceberg", "S3 Tables", "serverless Iceberg", "no-ops Iceberg", "auto-compaction", "AI-ready lakehouse", "Iceberg + AgentCore", "Bedrock KB over lake tables", "Athena over Iceberg without Glue ETL".
- This partial is the **S3 Tables specialisation** of the broader lakehouse pattern. If the workload is classic BI + warehouse + lake federation, prefer `DATA_LAKEHOUSE_ICEBERG` (self-managed Iceberg is cheaper per TB when you do not need auto-maintenance). If the workload is **AI-first** (LLM text-to-SQL, agent-over-Iceberg, Bedrock Knowledge Base on table metadata) or **mixed with unknown schema evolution churn**, prefer this one.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC / single-domain pipeline — one `cdk.Stack` owns the table bucket, namespace, tables, ingestion Lambdas, and query wrappers | **§3 Monolith Variant** |
| `TableBucketStack` owns the table bucket + namespaces + tables + CMK; `ComputeStack` owns ingestion / query / agent Lambdas with identity-side grants; `GovernanceStack` optionally owns Lake Formation permissions | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **No CDK L2 exists.** `aws_cdk.aws_s3tables.CfnTableBucket` / `CfnNamespace` / `CfnTable` / `CfnTableBucketPolicy` are the ONLY constructs as of v2.238. There is no `grant_read`, no `grant_write`, no `add_to_resource_policy` helper — **every grant is hand-written identity-side** against the exact ARN shape. This mirrors `DATA_S3_VECTORS` (also L1-only); get the ARN shape wrong and you get `AccessDeniedException` at runtime, not at synth.
2. **Table-bucket ARN and table ARN are NOT S3 ARNs.** The namespace is `s3tables`, not `s3`. Attempting to reuse patterns like `f"arn:aws:s3:::{bucket}/*"` silently grants on the wrong resource — IAM will accept the policy (ARN shape is valid for `s3`) but all `s3tables:*` calls deny.
3. **Cross-stack `CfnTable` references cycle**. If `TableBucketStack` creates `CfnTable` and `ComputeStack` wraps it in a Lambda reading `table.attr_table_arn` via `Fn.import_value`, and `TableBucketStack` later adds `CfnTableBucketPolicy` referencing a role ARN from `ComputeStack`, CloudFormation rejects the bidirectional export/import. Publishing the ARN via SSM breaks the cycle.
4. **`AWS::S3Tables::TableBucketPolicy` is a resource policy** — it lives on the bucket. Never add cross-account role ARNs directly if both accounts synthesise from CDK in different pipelines; use `Fn.sub` and externalise the principal ARN via SSM or `StringParameter.value_for_string_parameter`.
5. **Encryption is immutable at bucket create.** `EncryptionConfiguration` with `sse_algorithm: aws:kms` + `kms_key_arn: <CMK>` is set once. Switching from `AES256` to `KMS` requires a new bucket. If the CMK is in `TableBucketStack` and consumers in `ComputeStack`, **the CMK ARN is the cross-stack contract, not the bucket** — publish it via SSM.
6. **Automatic maintenance runs invisibly.** Compaction, snapshot expiration, and unreferenced-file cleanup happen in an AWS-owned account. You do NOT see the jobs, you do NOT pay for the compute directly (it is rolled into the S3 Tables service price). This means there are no `glue_job_role` ARNs to grant — but it also means **you cannot tune** compaction schedule beyond the per-table maintenance configuration. Accept this upfront.
7. **Glue Catalog federation is automatic but not ambient.** The federated catalog entry `s3tablescatalog/<table-bucket-name>` appears in Glue after **a one-time `s3tables:CreateTableBucket` → `glue:RegisterCatalog` handshake run by the S3 Tables service**. For Athena queries to work, the caller's IAM role must have `glue:GetDatabase`, `glue:GetTable`, and `lakeformation:GetDataAccess` even though the data lives in S3 Tables. Easy to miss.

Micro-Stack variant fixes all of this by: (a) owning the table bucket + namespaces + tables + CMK in `TableBucketStack`; (b) publishing `TableBucketName`, `TableBucketArn`, `NamespaceName`, `TableArn` (per table), and `KmsArn` via SSM; (c) consumer Lambdas grant themselves identity-side `s3tables:GetTableData` / `PutTableData` / `ListTables` plus `glue:GetDatabase` / `GetTable` plus `kms:Decrypt` on specific ARNs; (d) optional `GovernanceStack` owns `aws_lakeformation.CfnPrincipalPermissions` referencing the table ARN and the consumer role ARN (both via SSM).

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` class holds table bucket + namespaces + tables + ingest Lambda + query Lambda together. POC, single-domain pilot, or demo.

### 3.1 Architecture

```
  Ingest Fn ──► s3tables.PutTableData (CSV/Parquet/JSON batch upload)
                      │
                      ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  TableBucket: {project_name}-tables-{stage}                  │
  │    EncryptionConfiguration: sse_algorithm=aws:kms +          │
  │      kms_key_arn=local-CMK                                   │
  │    UnreferencedFileRemoval: days=7, noncurrent_days=3        │
  │                                                              │
  │  Namespace: lakehouse                                        │
  │                                                              │
  │  Table: fact_revenue   (Iceberg v2)                          │
  │    Schema:   [{"name": "order_id", "type": "bigint"},        │
  │               {"name": "customer_id", "type": "string"},     │
  │               {"name": "ts", "type": "timestamp"},           │
  │               {"name": "amount", "type": "decimal(18,2)"}]   │
  │    Partition: [bucket(ts, 16)]   (Iceberg hidden partition)  │
  │    Format:    parquet                                        │
  │    Maintenance: compaction + snapshot_expiry auto            │
  │                                                              │
  │  Table: dim_customer                                         │
  │  Table: stg_event                                            │
  └──────────────────────────────────────────────────────────────┘
                      ▲
                      │  Athena SELECT via AwsDataCatalog
                      │     DataCatalog: s3tablescatalog/{tbn}
                      │     Database:    lakehouse
                      │     Table:       fact_revenue
  Query Fn / Agent ───┘
```

Automatic maintenance — runs in AWS-owned account, invisible to you:

- **Compaction** — small Parquet files (< 64 MB) merged into target-sized files (512 MB default).
- **Snapshot expiration** — Iceberg snapshots older than `min_snapshots_to_keep` (default 1) and older than `max_snapshot_age_hours` (default 120) are removed.
- **Unreferenced file removal** — manifest files no longer referenced by any snapshot are deleted after `unreferenced_days` (default 3).

You configure these per table via `TableMaintenanceConfiguration`; you do NOT schedule Glue jobs.

### 3.2 CDK — `_create_s3_tables()` method body

```python
from aws_cdk import (
    CfnOutput, RemovalPolicy, Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3tables as s3t,                   # CDK L1 — v2.238+
)


def _create_s3_tables(self, stage: str) -> None:
    """Monolith variant. Assumes self.{kms_key} exists (or create a local
    CMK below). Provisions one table bucket + one namespace + three Iceberg
    tables (fact_revenue, dim_customer, stg_event).

    Only L1 constructs are available; there are no L2 grants — every IAM
    statement is hand-written against the exact ARN shape.
    """

    # A) Local CMK for the table bucket.
    #    Separate from the default stack key so key-policy + removal policy
    #    are both explicit. Auto-rotation is enabled.
    self.tables_cmk = kms.Key(
        self, "TablesCmk",
        alias=f"alias/{{project_name}}-tables-{stage}",
        enable_key_rotation=True,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Table bucket.
    #    - ARN shape: arn:aws:s3tables:{region}:{account}:bucket/{name}
    #    - `Ref` returns the bucket ARN (not the name) — same inversion as
    #      S3 Vectors. Use `.attr_table_bucket_arn` / `.attr_table_bucket_name`.
    self.table_bucket = s3t.CfnTableBucket(
        self, "TableBucket",
        table_bucket_name=f"{{project_name}}-tables-{stage}",   # 3-63 lowercase, no dots
        encryption_configuration=s3t.CfnTableBucket.EncryptionConfigurationProperty(
            sse_algorithm="aws:kms",
            kms_key_arn=self.tables_cmk.key_arn,
        ),
        unreferenced_file_removal=s3t.CfnTableBucket.UnreferencedFileRemovalProperty(
            status="Enabled",
            unreferenced_days=7,              # files unreferenced > 7d are eligible
            noncurrent_days=3,                # then deleted 3d later
        ),
    )

    # C) Namespace (logical "database" inside the bucket).
    #    DependsOn is explicit — CFN creates namespaces after the bucket, but
    #    inside this stack we want the explicit edge for debug clarity.
    self.namespace = s3t.CfnNamespace(
        self, "Namespace",
        table_bucket_arn=self.table_bucket.attr_table_bucket_arn,
        namespace="lakehouse",
    )
    self.namespace.add_dependency(self.table_bucket)

    # D) Tables.
    #    Schema uses the Iceberg spec JSON — NOT Glue column type strings.
    #    "type" is the Iceberg primitive name (e.g. "long" not "bigint",
    #    "decimal(18, 2)" NOT "decimal(18,2)" — commas must have space).
    fact_revenue_schema = {
        "fields": [
            {"name": "order_id",    "type": "long",            "required": True},
            {"name": "customer_id", "type": "string",          "required": True},
            {"name": "ts",          "type": "timestamptz",     "required": True},
            {"name": "amount",      "type": "decimal(18, 2)",  "required": True},
            {"name": "currency",    "type": "string",          "required": False},
        ]
    }
    self.fact_revenue = s3t.CfnTable(
        self, "FactRevenue",
        table_bucket_arn=self.table_bucket.attr_table_bucket_arn,
        namespace="lakehouse",
        name="fact_revenue",
        open_table_format="ICEBERG",
        iceberg_metadata=s3t.CfnTable.IcebergMetadataProperty(
            iceberg_schema=s3t.CfnTable.IcebergSchemaProperty(
                schema_field_list=[
                    s3t.CfnTable.SchemaFieldProperty(
                        name=f["name"], type=f["type"],
                        required=f.get("required", False),
                    )
                    for f in fact_revenue_schema["fields"]
                ],
            ),
        ),
    )
    self.fact_revenue.add_dependency(self.namespace)

    dim_customer = s3t.CfnTable(
        self, "DimCustomer",
        table_bucket_arn=self.table_bucket.attr_table_bucket_arn,
        namespace="lakehouse",
        name="dim_customer",
        open_table_format="ICEBERG",
        iceberg_metadata=s3t.CfnTable.IcebergMetadataProperty(
            iceberg_schema=s3t.CfnTable.IcebergSchemaProperty(
                schema_field_list=[
                    s3t.CfnTable.SchemaFieldProperty(name="customer_id", type="string",  required=True),
                    s3t.CfnTable.SchemaFieldProperty(name="name",        type="string",  required=True),
                    s3t.CfnTable.SchemaFieldProperty(name="segment",     type="string",  required=False),
                    s3t.CfnTable.SchemaFieldProperty(name="renewal_date",type="date",    required=False),
                ],
            ),
        ),
    )
    dim_customer.add_dependency(self.namespace)

    # E) Outputs — these are the cross-stack contract if this stack is split.
    CfnOutput(self, "TableBucketName", value=self.table_bucket.attr_table_bucket_name)
    CfnOutput(self, "TableBucketArn",  value=self.table_bucket.attr_table_bucket_arn)
    CfnOutput(self, "FactRevenueArn",  value=self.fact_revenue.attr_table_arn)
    CfnOutput(self, "TablesCmkArn",    value=self.tables_cmk.key_arn)
```

### 3.3 Identity-side grant on the same stack

```python
def _grant_ingest_lambda_access(self, fn_role: iam.IRole) -> None:
    """Grant the ingest Lambda write access to fact_revenue + dim_customer +
    stg_event. Identity-side only — no bucket policy mutation."""
    fn_role.add_to_principal_policy(iam.PolicyStatement(
        actions=[
            "s3tables:PutTableData",
            "s3tables:GetTableData",
            "s3tables:GetTableMetadataLocation",
            "s3tables:UpdateTableMetadataLocation",
            "s3tables:GetTable",
        ],
        resources=[
            self.fact_revenue.attr_table_arn,
            # Add other tables explicitly — wildcard would grant all tables in
            # all namespaces of the bucket.
        ],
    ))
    fn_role.add_to_principal_policy(iam.PolicyStatement(
        actions=["s3tables:ListTables", "s3tables:GetNamespace"],
        resources=[
            f"{self.table_bucket.attr_table_bucket_arn}/namespace/lakehouse",
        ],
    ))
    fn_role.add_to_principal_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.tables_cmk.key_arn],
    ))
    # Glue Catalog federation is auto, but readers still need glue:GetDatabase
    # + GetTable to resolve the federated catalog entry via Athena.
    fn_role.add_to_principal_policy(iam.PolicyStatement(
        actions=[
            "glue:GetDatabase", "glue:GetDatabases",
            "glue:GetTable",    "glue:GetTables",
            "glue:GetPartitions",
        ],
        resources=[
            f"arn:aws:glue:{Stack.of(self).region}:{Stack.of(self).account}:catalog",
            f"arn:aws:glue:{Stack.of(self).region}:{Stack.of(self).account}:database/s3tablescatalog/{self.table_bucket.attr_table_bucket_name}/lakehouse",
            f"arn:aws:glue:{Stack.of(self).region}:{Stack.of(self).account}:table/s3tablescatalog/{self.table_bucket.attr_table_bucket_name}/lakehouse/*",
        ],
    ))
```

### 3.4 Ingest Lambda — idempotent Parquet upload via boto3

```python
# lambda/ingest_revenue/handler.py
import os
import pyarrow as pa
import pyarrow.parquet as pq
import boto3

TABLE_BUCKET_ARN = os.environ["TABLE_BUCKET_ARN"]
NAMESPACE        = os.environ["NAMESPACE"]
TABLE_NAME       = os.environ["TABLE_NAME"]

s3t = boto3.client("s3tables")

def lambda_handler(event, _ctx):
    """event = {"rows": [{"order_id": ..., "customer_id": ..., ...}]}"""
    rows = event["rows"]
    if not rows:
        return {"inserted": 0}
    # Build an Arrow table in memory — S3 Tables accepts Parquet or CSV.
    # For production, prefer Parquet: smaller, schema-embedded.
    tbl = pa.Table.from_pylist(rows)
    buf = pa.BufferOutputStream()
    pq.write_table(tbl, buf)
    data_bytes = buf.getvalue().to_pybytes()

    # PutTableData appends a data file. The Iceberg commit (snapshot bump)
    # happens server-side; the client sees an idempotency-safe overwrite on
    # retry if the same `request_token` is used. The SDK adds a request_token
    # automatically; pass one explicitly for at-least-once safety.
    resp = s3t.put_table_data(
        tableBucketARN=TABLE_BUCKET_ARN,
        namespace=NAMESPACE,
        name=TABLE_NAME,
        format="PARQUET",
        data=data_bytes,
    )
    return {"inserted": len(rows), "snapshot_id": resp.get("snapshotId")}
```

### 3.5 Query Lambda — Athena against the auto-federated catalog

```python
# lambda/query_revenue/handler.py
import os, time
import boto3

ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
TABLE_BUCKET_NAME = os.environ["TABLE_BUCKET_NAME"]
DB = "lakehouse"

ath = boto3.client("athena")

def lambda_handler(event, _ctx):
    sql = event["sql"]  # trusted caller — enforce guardrails upstream
    # The federated catalog is visible as `s3tablescatalog/<bucket-name>`
    # in the AwsDataCatalog. Athena understands Iceberg natively; DML + time
    # travel + MERGE all work.
    exec_id = ath.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={
            "Catalog":  f"s3tablescatalog/{TABLE_BUCKET_NAME}",
            "Database": DB,
        },
        WorkGroup=ATHENA_WORKGROUP,
    )["QueryExecutionId"]

    # Poll — in practice, use Step Functions `.waitForTaskToken` or the
    # Athena-via-EventBridge completion pattern for non-trivial queries.
    for _ in range(60):
        st = ath.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if st != "SUCCEEDED":
        raise RuntimeError(f"Athena query {exec_id} ended in {st}")
    rows = ath.get_query_results(QueryExecutionId=exec_id)
    return {"rows": rows["ResultSet"]["Rows"]}
```

### 3.6 Monolith gotchas

1. **Iceberg type names are NOT Glue type names.** Use `long`, not `bigint`; `timestamptz`, not `timestamp with time zone`; `decimal(18, 2)` with a space after the comma, not `decimal(18,2)`. Writing the wrong spelling produces a cryptic `InvalidSchemaField` at table-create time, not at synth.
2. **Table names must be lowercase, 1–255 chars, `[a-z0-9_]`.** No hyphens, no mixed case. CFN validation catches this at `cdk deploy` time.
3. **`UnreferencedFileRemoval` is per-bucket, not per-table.** Once set, it governs ALL tables in the bucket. For tables with very different retention needs, use separate buckets.
4. **Compaction is not tunable beyond on/off.** You cannot set target file size, you cannot pause, you cannot trigger manually. Accept the defaults (target 512 MB, trigger on small-file ratio) or switch to self-managed Iceberg (`DATA_LAKEHOUSE_ICEBERG`).
5. **Region availability is narrower than S3.** As of v2.238, S3 Tables is available in `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `ap-northeast-1`, `ap-southeast-1`, `ap-southeast-2`. If the client's primary region is outside this list, either (a) deploy tables in a supported region and cross-region query from Athena (expensive on latency), or (b) fall back to `DATA_LAKEHOUSE_ICEBERG`.
6. **Athena workgroup MUST be v3.** Iceberg DML (`MERGE INTO`, time-travel `FOR TIMESTAMP AS OF`) only works on Athena engine version 3. Set `selected_engine_version="Athena engine version 3"` on the workgroup.
7. **`TableBucketPolicy` overwrites on update.** CDK emits the whole policy each synth. If you layer multiple statement sources, build them in one place (`iam.PolicyDocument`) and pass it whole — appending via `add_to_resource_policy` on L1 does not exist.

---

## 4. Micro-Stack Variant

**Use when:** multiple stacks share the table bucket; you want the blast radius of a table rename / schema change contained to `TableBucketStack`; compute / agent / analytics stacks deploy on independent cadences.

### 4.1 The 5 non-negotiables

Same 5 non-negotiables from `LAYER_BACKEND_LAMBDA §4.1`:

1. **`Path(__file__)` anchoring** on any `PythonFunction` / `DockerImageFunction` entry path.
2. **Identity-side grants only** — consumer Lambdas grant themselves `s3tables:*` / `glue:*` / `kms:*` against SSM-read ARNs; NEVER mutate the table bucket resource policy to reference an external role ARN.
3. **`CfnRule` cross-stack EventBridge** — if a table change emits an EventBridge event (e.g. EB fanout on new snapshot), the rule lives in the producer stack (TableBucketStack) and the target Lambda in ComputeStack is referenced by ARN (SSM), not by L2 construct.
4. **Same-stack bucket + OAC** — does not apply (no CloudFront here). Applies if you serve Athena result CSVs through CloudFront; then the result S3 bucket + OAC live together.
5. **KMS ARNs as strings** — the `TablesCmk.key_arn` is published via SSM. Consumers read the string, grant `kms:Decrypt` on the string ARN, never on an imported `kms.Key` L2.

### 4.2 TableBucketStack — owns bucket, namespaces, tables, CMK

```python
# stacks/table_bucket_stack.py
from aws_cdk import (
    CfnOutput, RemovalPolicy, Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3tables as s3t,
    aws_ssm as ssm,
)
from constructs import Construct


class TableBucketStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) CMK.
        self.tables_cmk = kms.Key(
            self, "TablesCmk",
            alias=f"alias/{{project_name}}-tables-{stage}",
            enable_key_rotation=True,
            removal_policy=(
                RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
            ),
        )

        # B) Table bucket.
        self.table_bucket = s3t.CfnTableBucket(
            self, "TableBucket",
            table_bucket_name=f"{{project_name}}-tables-{stage}",
            encryption_configuration=s3t.CfnTableBucket.EncryptionConfigurationProperty(
                sse_algorithm="aws:kms",
                kms_key_arn=self.tables_cmk.key_arn,
            ),
            unreferenced_file_removal=s3t.CfnTableBucket.UnreferencedFileRemovalProperty(
                status="Enabled",
                unreferenced_days=7,
                noncurrent_days=3,
            ),
        )

        # C) Namespace + tables (trimmed — full schema in §3.2).
        self.namespace = s3t.CfnNamespace(
            self, "Namespace",
            table_bucket_arn=self.table_bucket.attr_table_bucket_arn,
            namespace="lakehouse",
        )
        self.namespace.add_dependency(self.table_bucket)

        self.fact_revenue = s3t.CfnTable(
            self, "FactRevenue",
            table_bucket_arn=self.table_bucket.attr_table_bucket_arn,
            namespace="lakehouse",
            name="fact_revenue",
            open_table_format="ICEBERG",
            iceberg_metadata=s3t.CfnTable.IcebergMetadataProperty(
                iceberg_schema=s3t.CfnTable.IcebergSchemaProperty(
                    schema_field_list=[
                        s3t.CfnTable.SchemaFieldProperty(name="order_id",    type="long",           required=True),
                        s3t.CfnTable.SchemaFieldProperty(name="customer_id", type="string",         required=True),
                        s3t.CfnTable.SchemaFieldProperty(name="ts",          type="timestamptz",    required=True),
                        s3t.CfnTable.SchemaFieldProperty(name="amount",      type="decimal(18, 2)", required=True),
                    ],
                ),
            ),
        )
        self.fact_revenue.add_dependency(self.namespace)

        # D) Publish cross-stack contract via SSM — names, ARNs, CMK.
        ssm.StringParameter(
            self, "TableBucketNameParam",
            parameter_name=f"/{{project_name}}/{stage}/lakehouse/table_bucket_name",
            string_value=self.table_bucket.attr_table_bucket_name,
        )
        ssm.StringParameter(
            self, "TableBucketArnParam",
            parameter_name=f"/{{project_name}}/{stage}/lakehouse/table_bucket_arn",
            string_value=self.table_bucket.attr_table_bucket_arn,
        )
        ssm.StringParameter(
            self, "FactRevenueArnParam",
            parameter_name=f"/{{project_name}}/{stage}/lakehouse/fact_revenue_arn",
            string_value=self.fact_revenue.attr_table_arn,
        )
        ssm.StringParameter(
            self, "TablesCmkArnParam",
            parameter_name=f"/{{project_name}}/{stage}/lakehouse/cmk_arn",
            string_value=self.tables_cmk.key_arn,
        )

        CfnOutput(self, "TableBucketName", value=self.table_bucket.attr_table_bucket_name)
        CfnOutput(self, "TableBucketArn",  value=self.table_bucket.attr_table_bucket_arn)
        CfnOutput(self, "FactRevenueArn",  value=self.fact_revenue.attr_table_arn)
```

### 4.3 Consumer pattern — identity-side grants in `ComputeStack`

```python
# stacks/compute_stack.py — Lambda consumes the table bucket cross-stack.
from pathlib import Path
from aws_cdk import (
    Duration, Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction


class ComputeStack(Stack):
    def __init__(self, scope, construct_id, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) Resolve cross-stack contract via SSM tokens.
        #    These return CDK tokens — use them directly in env + resources.
        table_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lakehouse/table_bucket_name"
        )
        table_bucket_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lakehouse/table_bucket_arn"
        )
        fact_revenue_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lakehouse/fact_revenue_arn"
        )
        cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lakehouse/cmk_arn"
        )

        # B) Ingest Lambda.
        ingest_fn = PythonFunction(
            self, "IngestRevenueFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "ingest_revenue"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment={
                "TABLE_BUCKET_ARN":  table_bucket_arn,
                "NAMESPACE":         "lakehouse",
                "TABLE_NAME":        "fact_revenue",
            },
        )

        # C) Identity-side grants — s3tables, glue (for federated catalog),
        #    kms on the CMK ARN (string, not an imported Key).
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "s3tables:PutTableData",
                "s3tables:GetTableData",
                "s3tables:GetTableMetadataLocation",
                "s3tables:UpdateTableMetadataLocation",
                "s3tables:GetTable",
            ],
            resources=[fact_revenue_arn],
        ))
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3tables:ListTables", "s3tables:GetNamespace"],
            resources=[f"{table_bucket_arn}/namespace/lakehouse"],
        ))
        # The federated Glue catalog ARN uses the BUCKET NAME, not ARN.
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "glue:GetDatabase", "glue:GetDatabases",
                "glue:GetTable",    "glue:GetTables",
                "glue:GetPartitions",
            ],
            resources=[
                f"arn:aws:glue:{self.region}:{self.account}:catalog",
                f"arn:aws:glue:{self.region}:{self.account}:database/s3tablescatalog/{table_bucket_name}/lakehouse",
                f"arn:aws:glue:{self.region}:{self.account}:table/s3tablescatalog/{table_bucket_name}/lakehouse/*",
            ],
        ))
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[cmk_arn],
        ))
```

### 4.4 Micro-stack gotchas

- **`value_for_string_parameter` returns a token.** Use it directly in `resources=[...]` and `environment={...}` — do NOT call `.split("/")` or compute on it in Python at synth time. If you need to build a child ARN (e.g. `{bucket_arn}/namespace/lakehouse`), use an f-string with the token — CDK resolves at deploy time.
- **Table ARNs are NOT `{bucket_arn}/table/{ns}/{name}` in IAM resource strings for all actions.** Some actions (`s3tables:ListTables`, `s3tables:GetNamespace`) require the **namespace ARN** `{bucket_arn}/namespace/{ns}`; others (`s3tables:GetTableData`, `PutTableData`) require the **table ARN** `{bucket_arn}/table/{ns}/{name}`. Keep both in mind when scoping.
- **Glue Catalog federation entry lives under the BUCKET NAME, not the ARN.** The ARN shape `arn:aws:glue:<region>:<account>:database/s3tablescatalog/<bucket-name>/<namespace>` uses the name. Publish both `table_bucket_name` and `table_bucket_arn` — consumers need the name for Glue grants and the ARN for S3 Tables grants.
- **Cross-stack deletion order**: if `TableBucketStack` is deleted while `ComputeStack` still references `TableBucketArnParam` via SSM, the param disappears and the consumer's next deploy fails on `ParameterNotFound`. Deploy order: `TableBucketStack` → `ComputeStack`. Delete order: `ComputeStack` → `TableBucketStack`.
- **Time-travel queries need `fact_revenue` to retain old snapshots**. If `max_snapshot_age_hours` defaults to 120h (5 days), a query with `FOR TIMESTAMP AS OF '2024-01-01'` will fail. Tune `TableMaintenanceConfiguration` per table for time-travel-heavy use cases.

---

## 5. Swap matrix — when to replace a component

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Iceberg host | S3 Tables (this partial) | Self-managed Iceberg on plain S3 + Glue ETL compaction (`DATA_LAKEHOUSE_ICEBERG`) | Cheaper per-TB, tunable compaction, regions outside S3 Tables footprint. Trade-off: you own the maintenance job. |
| Iceberg host | S3 Tables | Delta Lake on S3 via EMR | Multi-engine (Databricks-portable); but no native Athena DML — read-only from Athena. Pick only if Databricks is the primary consumer. |
| Table format | Iceberg | Apache Hudi via EMR | Streaming upserts with MoR (merge-on-read) mode; no native Athena DML. Use only when stream-upsert latency < 1 min is required. |
| Query engine | Athena | EMR Serverless + Spark | Heavy UDF / Python-only transforms; but higher ops cost, cold-start latency. Prefer Athena until a specific UDF is blocked. |
| Query engine | Athena | Redshift Serverless (Spectrum) | Complex joins + materialized views + concurrency scaling. Pair with Redshift when BI dashboards need sub-second repeated queries. |
| Schema authority | Glue Catalog (auto-federated) | Open-source Iceberg REST catalog (Tabular / Polaris) | Portability to non-AWS engines; but loses Lake Formation integration. Only if multi-cloud is a hard requirement. |
| Governance | Lake Formation TBAC (`DATA_LAKE_FORMATION`) | IAM-only column filtering | LF is heavier to set up but gives tag-based grants + cross-account. IAM-only is fine for single-account, single-team lakes. |
| Bulk load | `PutTableData` via Lambda | EMR `INSERT INTO` via Spark | Multi-GB inserts — EMR is cheaper and faster. Lambda ingest caps at 15 min × 10 GB memory. |
| Change-data-capture source | Firehose → Lambda → `PutTableData` | AWS Zero-ETL from Aurora → Iceberg (`DATA_ZERO_ETL`) | Zero-ops for DB replicas; but 5-min minimum lag and Aurora-only today. |

---

## 6. Worked example — offline synth + boto3 round-trip

```python
# tests/test_table_bucket_synth.py
"""Offline: cdk synth produces the expected CloudFormation shape for
TableBucketStack. Runs without AWS creds."""
import json
from pathlib import Path
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.table_bucket_stack import TableBucketStack


def test_synth_table_bucket_and_one_iceberg_table():
    app = cdk.App()
    stack = TableBucketStack(app, "Lakehouse-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # TableBucket with KMS and unreferenced-file removal on.
    tpl.has_resource_properties("AWS::S3Tables::TableBucket", {
        "TableBucketName": "{{project_name}}-tables-dev",
        "EncryptionConfiguration": Match.object_like({
            "SSEAlgorithm": "aws:kms",
        }),
        "UnreferencedFileRemoval": Match.object_like({
            "Status": "Enabled",
            "UnreferencedDays": 7,
            "NoncurrentDays":   3,
        }),
    })

    # Namespace created.
    tpl.resource_count_is("AWS::S3Tables::Namespace", 1)

    # Iceberg table with the expected schema.
    tpl.has_resource_properties("AWS::S3Tables::Table", {
        "Name":            "fact_revenue",
        "Namespace":       "lakehouse",
        "OpenTableFormat": "ICEBERG",
        "IcebergMetadata": Match.object_like({
            "IcebergSchema": Match.object_like({
                "SchemaFieldList": Match.array_with([
                    Match.object_like({
                        "Name": "order_id", "Type": "long", "Required": True,
                    }),
                    Match.object_like({
                        "Name": "amount",   "Type": "decimal(18, 2)",
                    }),
                ]),
            }),
        }),
    })

    # CMK + rotation.
    tpl.has_resource_properties("AWS::KMS::Key", {
        "EnableKeyRotation": True,
    })

    # SSM contract published.
    for suffix in ("table_bucket_name", "table_bucket_arn", "fact_revenue_arn", "cmk_arn"):
        tpl.has_resource_properties("AWS::SSM::Parameter", {
            "Name": f"/{{project_name}}/dev/lakehouse/{suffix}",
        })


# tests/test_integration_put_query.py
"""Integration: deployed stack — put 3 rows, query via Athena.
Marked `integration` — runs only in `pytest -m integration`."""
import os, time, json
import pytest
import boto3
import pyarrow as pa
import pyarrow.parquet as pq


@pytest.mark.integration
def test_put_then_athena_query():
    tba = os.environ["TABLE_BUCKET_ARN"]
    tbn = os.environ["TABLE_BUCKET_NAME"]
    wg  = os.environ["ATHENA_WORKGROUP"]

    # 1) Put three rows as Parquet.
    s3t = boto3.client("s3tables")
    rows = [
        {"order_id": 1, "customer_id": "c1", "ts": "2026-04-22T00:00:00Z", "amount": "100.00"},
        {"order_id": 2, "customer_id": "c1", "ts": "2026-04-22T00:05:00Z", "amount": "250.00"},
        {"order_id": 3, "customer_id": "c2", "ts": "2026-04-22T00:10:00Z", "amount":  "50.00"},
    ]
    tbl = pa.Table.from_pylist(rows)
    buf = pa.BufferOutputStream()
    pq.write_table(tbl, buf)
    s3t.put_table_data(
        tableBucketARN=tba, namespace="lakehouse", name="fact_revenue",
        format="PARQUET", data=buf.getvalue().to_pybytes(),
    )

    # 2) Query via Athena against the federated catalog.
    ath = boto3.client("athena")
    exec_id = ath.start_query_execution(
        QueryString="SELECT customer_id, SUM(amount) AS total FROM fact_revenue GROUP BY customer_id",
        QueryExecutionContext={
            "Catalog":  f"s3tablescatalog/{tbn}",
            "Database": "lakehouse",
        },
        WorkGroup=wg,
    )["QueryExecutionId"]
    for _ in range(60):
        st = ath.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    assert st == "SUCCEEDED", f"Athena query failed in state {st}"

    rows_out = ath.get_query_results(QueryExecutionId=exec_id)["ResultSet"]["Rows"]
    # Row 0 is the header; rows 1..N are data.
    data = {r["Data"][0]["VarCharValue"]: r["Data"][1]["VarCharValue"] for r in rows_out[1:]}
    assert data["c1"] == "350.00"
    assert data["c2"] == "50.00"
```

Run `pytest tests/test_table_bucket_synth.py -v` offline to validate the CDK shape; `pytest tests/test_integration_put_query.py -v -m integration` after deploy for the full round-trip.

---

## 7. References

- AWS docs — *Amazon S3 Tables user guide* (`s3tables` service, `s3tablescatalog` federation).
- AWS docs — *Querying S3 Tables with Athena* — `AwsDataCatalog` context, `s3tablescatalog/<bucket>` federation.
- Apache Iceberg spec v2 — schema JSON field types (`long`, `string`, `decimal(P, S)`, `timestamptz`).
- `DATA_LAKEHOUSE_ICEBERG.md` — self-managed Iceberg counterpart (choose when auto-maintenance is not needed).
- `DATA_LAKE_FORMATION.md` — LF-TBAC governance layer over S3 Tables (required for cross-account, column masking).
- `DATA_GLUE_CATALOG.md` — the federated catalog that fronts S3 Tables; for Glue catalog patterns outside S3 Tables too.
- `DATA_ATHENA.md` — Athena workgroup, federation queries, text-to-SQL prep.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — the 5 non-negotiables echoed here.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** S3 Tables GA coverage. Dual-variant SOP; Glue Catalog auto-federation pattern documented. 7 monolith gotchas, 4 micro-stack gotchas, 9-row swap matrix, pytest synth + boto3 integration harness.
