# SOP — Amazon Athena (workgroups, federation, ML/Bedrock integration, text-to-SQL prep)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `aws_cdk.aws_athena` L1 (no L2 in stable; `aws_cdk.aws_athena_alpha` exists but is churny) · Athena engine version 3 (PrestoDB + Trino + Iceberg DML) · Athena Data Catalog types (`GLUE`, `HIVE`, `LAMBDA`, `FEDERATED`) · Prepared statements · Named queries · Capacity reservations (dedicated cluster) · Athena-for-Apache-Spark · `AthenaExecuteOptimisticLockingConfiguration` for Iceberg

---

## 1. Purpose

- Provide the deep-dive for **Amazon Athena** as the **query plane** for the lakehouse — serverless SQL over S3 (parquet / JSON / CSV / ORC / Avro), over Iceberg tables (S3 Tables or self-managed), over Delta/Hudi via federation, over JDBC (Snowflake, Redshift, Postgres, MySQL via Athena Federated Query), and over Bedrock (model invocation from SQL).
- Codify the **workgroup → query → result bucket** contract. One workgroup per environment / team / cost bucket. Workgroup-level config is the only **enforceable** config; client-side overrides exist but are opt-out via `enforce_workgroup_configuration=True`.
- Codify the **engine-version pin** — `selected_engine_version="Athena engine version 3"` — required for Iceberg DML (`MERGE INTO`, `UPDATE`, `DELETE`, time travel), for prepared-statement parameter binding, and for PartiQL nested-field queries. Never default to v2 on new work; v2 is EOL-soon.
- Codify **federation** — Athena Federated Query via Lambda connectors (`CfnDataCatalog(Type="LAMBDA")`) vs the newer **Glue Catalog Federation** via `CfnCatalog` (see `DATA_GLUE_CATALOG.md`). The partial prefers Glue Federation where possible (visible from EMR + Redshift too); Lambda connectors only for sources without Glue support (mainframe DB2, legacy ODBC).
- Codify **capacity reservations** — `CfnCapacityReservation` pre-allocates DPU capacity for predictable latency, at the cost of minimum-reservation billing. Use only when query queue depth exceeds 5 at p95.
- Codify the **text-to-SQL prep** — Athena's `EXPLAIN` plan is the grounding signal for Wave 3's `PATTERN_TEXT_TO_SQL`: the agent writes SQL, runs `EXPLAIN` (metadata only, free), and self-corrects before paying for the scan. Also: `QueryExecution.Statistics.DataScannedInBytes` is the cost signal; reject queries over a threshold.
- Codify **result encryption + result-bucket separation**. The result bucket MUST be same-account, KMS-encrypted; workgroup config enforces it. The bucket lifecycle policy MUST aggressively clean old results (default: 30-day). Otherwise it grows unbounded.
- Codify **Bedrock-from-SQL** via `USING FUNCTION invoke_model(...)` (Athena + Bedrock integration GA 2025). Call an LLM inline inside SQL: `SELECT customer_id, invoke_model('...') FROM fact_customer WHERE ...`. Powerful but billed per-invoke — gate behind a budget filter.
- Include when the SOW signals: "Athena", "serverless SQL", "query lakehouse", "text-to-SQL", "federated query", "cross-source SQL", "Bedrock from SQL", "analyst self-service", "ad-hoc SQL on S3".
- This partial is the **query engine** for all lakehouse variants. Pairs with `DATA_GLUE_CATALOG` (metadata), `DATA_ICEBERG_S3_TABLES` / `DATA_LAKEHOUSE_ICEBERG` (storage), `DATA_LAKE_FORMATION` (governance — enforced at query time).

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the workgroup, result bucket, KMS, sample named queries, and the consumer Lambda | **§3 Monolith Variant** |
| `AthenaStack` owns workgroups + result bucket + CMK + capacity reservation + federated `CfnDataCatalog`s; `ComputeStack` / `AgentStack` own caller Lambdas with identity-side grants | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Result buckets + KMS cycle on cross-stack grant.** The result bucket receives writes from any role that runs a query — query callers come from many stacks. If the result bucket lives in `AthenaStack` and callers live elsewhere, calling `bucket.grant_read_write(caller_role)` mutates the bucket policy with the caller's role ARN → bidirectional export cycle. Fix: bucket + CMK + `CfnBucketPolicy` all in `AthenaStack`, callers grant themselves identity-side against the bucket's SSM-published name.
2. **Workgroup enforcement is binary per workgroup.** `enforce_workgroup_configuration=True` means the workgroup's result-bucket + KMS override any client-side setting. Without it, a misconfigured Lambda can write to an unencrypted bucket and you violate compliance silently. Always `True` in prod.
3. **`CfnDataCatalog` (Lambda federation) registers a Lambda ARN**. If the Lambda connector is in a separate `FederationStack`, the catalog registration references its ARN; cross-stack string token is fine, but deletion order matters — delete `AthenaStack` before `FederationStack` or the catalog fails to resolve its Lambda at cleanup time.
4. **Capacity reservations live at the account level.** A single reservation can be assigned to multiple workgroups via `CfnCapacityAssignmentConfiguration`. Ownership: `AthenaStack` owns the reservation; workgroup stacks (if split further) read the reservation ARN via SSM and assign themselves.
5. **Named queries + prepared statements are workgroup-scoped artifacts.** If an analytics team has their own queries in a separate `AnalyticsStack`, the queries reference the workgroup name (string, from SSM). Keep the workgroup in one place.

Micro-Stack fixes by: (a) owning workgroup + result bucket + CMK + federated catalogs + capacity reservation in `AthenaStack`; (b) publishing `WorkgroupName`, `ResultBucketName`, `ResultBucketArn`, `CmkArn`, `CapacityReservationArn`, federated `CatalogName` (per source) via SSM; (c) caller Lambdas grant themselves identity-side.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  Athena Workgroup: lakehouse-analyst-{stage}                     │
  │    enforced:                                                     │
  │      engine_version = "Athena engine version 3"                  │
  │      result_bucket  = s3://...-athena-results-{stage}/           │
  │      result_kms     = arn:aws:kms:...:alias/athena-{stage}       │
  │      bytes_scanned_cutoff = 10 GB  (kills runaway queries)       │
  │      publish_cloudwatch_metrics = true                           │
  │                                                                  │
  │  Named queries (workgroup-scoped reusables):                     │
  │    - q_top10_customers_last_quarter                              │
  │    - q_revenue_by_region_yoy                                     │
  │                                                                  │
  │  Prepared statements:                                            │
  │    - top_n_by_region(?region VARCHAR, ?n INT)                    │
  │                                                                  │
  │  Federated data catalogs (visible as extra top-level DBs):       │
  │    - snowflake_prod      (Glue Federation via CfnCatalog)        │
  │    - mainframe_db2       (Lambda connector via CfnDataCatalog)   │
  │    - s3tablescatalog/xyz (auto-registered by S3 Tables)          │
  │                                                                  │
  │  Capacity reservation (optional, prod only):                     │
  │    - 24-DPU dedicated, 1-hour min commit                         │
  └──────────────────────────────────────────────────────────────────┘
          ▲                                      │
          │  START_QUERY_EXECUTION               │  INVOKE_MODEL (Bedrock)
          │  (SQL + result via polling or EB)    ▼
    Consumer Fn / Agent                 Bedrock model
```

### 3.2 CDK — `_create_athena()` method body

```python
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_athena as athena,                # L1
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)


def _create_athena(self, stage: str) -> None:
    """Monolith variant. Owns result bucket + CMK + workgroup + a couple of
    named queries + one Lambda federation catalog."""

    # A) CMK for result encryption.
    self.athena_cmk = kms.Key(
        self, "AthenaCmk",
        alias=f"alias/{{project_name}}-athena-{stage}",
        enable_key_rotation=True,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Result bucket — SSE-KMS, block public access, 30-day lifecycle.
    self.athena_results = s3.Bucket(
        self, "AthenaResultsBucket",
        bucket_name=f"{{project_name}}-athena-results-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.athena_cmk,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=False,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
        auto_delete_objects=(stage != "prod"),
        lifecycle_rules=[
            s3.LifecycleRule(
                id="expire-old-results",
                enabled=True,
                expiration=Duration.days(30),
                noncurrent_version_expiration=Duration.days(7),
                abort_incomplete_multipart_upload_after=Duration.days(1),
            ),
        ],
    )

    # C) Workgroup with ENFORCED config — client cannot override.
    self.workgroup = athena.CfnWorkGroup(
        self, "LakehouseAnalystWg",
        name=f"lakehouse-analyst-{stage}",
        description=(
            "Interactive analyst workgroup — Iceberg DML-enabled, "
            "10 GB scan cutoff, CloudWatch metrics on."
        ),
        state="ENABLED",
        work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
            enforce_work_group_configuration=True,         # critical
            publish_cloud_watch_metrics_enabled=True,
            bytes_scanned_cutoff_per_query=10 * 1024**3,    # 10 GB
            requester_pays_enabled=False,
            engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                selected_engine_version="Athena engine version 3",
            ),
            result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                output_location=f"s3://{self.athena_results.bucket_name}/queries/",
                encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                    encryption_option="SSE_KMS",
                    kms_key=self.athena_cmk.key_arn,
                ),
                expected_bucket_owner=Stack.of(self).account,
            ),
        ),
    )

    # D) Named queries — reusable, workgroup-scoped.
    athena.CfnNamedQuery(
        self, "QTopCustomersLastQuarter",
        name="q_top10_customers_last_quarter",
        database="lakehouse_{stage}",
        work_group=self.workgroup.ref,
        description="Top 10 customers by revenue in the last quarter.",
        query_string=(
            "SELECT customer_id, SUM(amount) AS total "
            "FROM fact_revenue "
            "WHERE ts >= date_trunc('quarter', current_date) "
            "  AND ts <  date_trunc('quarter', current_date + INTERVAL '3' MONTH) "
            "GROUP BY customer_id "
            "ORDER BY total DESC LIMIT 10"
        ),
    )

    # E) Prepared statement — parameterised.
    athena.CfnPreparedStatement(
        self, "PsTopNByRegion",
        statement_name="top_n_by_region",
        work_group=self.workgroup.ref,
        description="Top-N customers by revenue in a given region.",
        query_statement=(
            "SELECT c.customer_id, c.name, SUM(f.amount) AS total "
            "FROM   fact_revenue f "
            "JOIN   dim_customer c ON c.customer_id = f.customer_id "
            "WHERE  c.region = ? "
            "GROUP  BY c.customer_id, c.name "
            "ORDER  BY total DESC "
            "LIMIT  ?"
        ),
    )

    # F) Lambda federation catalog — for a DB2 mainframe (no Glue support).
    #    Assumes self.db2_connector_lambda.function_arn is the DeployedAthena
    #    Federation connector (from the AWS SAR app).
    athena.CfnDataCatalog(
        self, "Db2FederatedCatalog",
        name="mainframe_db2",
        type="LAMBDA",
        description="DB2 mainframe via Athena Federated Query.",
        parameters={
            "function": self.db2_connector_lambda.function_arn,
        },
    )

    # G) Capacity reservation (prod only) — optional.
    if stage == "prod":
        self.capacity_reservation = athena.CfnCapacityReservation(
            self, "ProdCapacityReservation",
            name=f"{{project_name}}-reserved-{stage}",
            target_dpus=24,               # 24 DPU minimum, 24-DPU increments
        )
        athena.CfnCapacityAssignmentConfiguration(
            self, "ProdCapacityAssignment",
            capacity_reservation_name=self.capacity_reservation.ref,
            capacity_assignments=[
                athena.CfnCapacityAssignmentConfiguration.CapacityAssignmentProperty(
                    work_group_names=[self.workgroup.ref],
                ),
            ],
        )

    # H) Outputs — cross-stack contract.
    CfnOutput(self, "AthenaWorkgroupName", value=self.workgroup.ref)
    CfnOutput(self, "AthenaResultsBucket", value=self.athena_results.bucket_name)
    CfnOutput(self, "AthenaCmkArn",        value=self.athena_cmk.key_arn)
```

### 3.3 Identity-side grant on the same stack

```python
def _grant_caller_athena_access(
    self, caller_role: iam.IRole, *, glue_db: str,
) -> None:
    """Grant a consumer role the IAM side of Athena. LF grants the data side
    separately (see DATA_LAKE_FORMATION)."""
    caller_role.add_to_principal_policy(iam.PolicyStatement(
        actions=[
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:GetQueryResultsStream",
            "athena:StopQueryExecution",
            "athena:ListQueryExecutions",
            "athena:BatchGetQueryExecution",
            "athena:GetNamedQuery", "athena:ListNamedQueries",
            "athena:GetPreparedStatement", "athena:ListPreparedStatements",
        ],
        resources=[
            f"arn:aws:athena:{Stack.of(self).region}:"
            f"{Stack.of(self).account}:workgroup/{self.workgroup.ref}",
        ],
    ))
    caller_role.add_to_principal_policy(iam.PolicyStatement(
        actions=[
            "glue:GetDatabase", "glue:GetDatabases",
            "glue:GetTable",    "glue:GetTables",
            "glue:GetPartitions",
            "lakeformation:GetDataAccess",            # handshake for LF-governed
        ],
        resources=["*"],                              # LF grants scope it
    ))
    # Result bucket reads + KMS.
    caller_role.add_to_principal_policy(iam.PolicyStatement(
        actions=[
            "s3:GetBucketLocation", "s3:GetObject", "s3:PutObject",
            "s3:ListBucket", "s3:ListMultipartUploadParts",
            "s3:AbortMultipartUpload",
        ],
        resources=[
            self.athena_results.bucket_arn,
            f"{self.athena_results.bucket_arn}/*",
        ],
    ))
    caller_role.add_to_principal_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.athena_cmk.key_arn],
    ))
    # If the query reads an Iceberg / S3 Tables / lake bucket, that bucket's
    # READ grants are added separately (see DATA_ICEBERG_S3_TABLES §3.3).
```

### 3.4 Caller Lambda — run a parameterised prepared statement

```python
# lambda/athena_caller/handler.py
import os, time, boto3

WORKGROUP = os.environ["WORKGROUP"]
DATABASE  = os.environ["DATABASE"]
ath = boto3.client("athena")


def _start(sql: str, execution_parameters: list[str] | None = None) -> str:
    kw = dict(
        QueryString=sql,
        QueryExecutionContext={"Database": DATABASE},
        WorkGroup=WORKGROUP,
    )
    if execution_parameters:
        kw["ExecutionParameters"] = execution_parameters
    return ath.start_query_execution(**kw)["QueryExecutionId"]


def _await(exec_id: str, timeout_s: int = 90) -> dict:
    end = time.time() + timeout_s
    while time.time() < end:
        q = ath.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]
        st = q["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return q
        time.sleep(1)
    ath.stop_query_execution(QueryExecutionId=exec_id)
    raise TimeoutError(f"Athena query {exec_id} timed out")


def lambda_handler(event, _ctx):
    # event = {"statement": "top_n_by_region", "params": ["EMEA", "5"]}
    ps = event.get("statement")
    if ps:
        sql = f"EXECUTE {ps}"
        params = event.get("params", [])
        exec_id = _start(sql, execution_parameters=params)
    else:
        exec_id = _start(event["sql"])

    q = _await(exec_id)
    st = q["Status"]["State"]
    if st != "SUCCEEDED":
        return {"ok": False, "state": st, "reason": q["Status"].get("StateChangeReason")}

    # Stats — surface to the caller for cost accounting.
    stats = q.get("Statistics", {})
    rows = ath.get_query_results(QueryExecutionId=exec_id)
    return {
        "ok":              True,
        "exec_id":         exec_id,
        "scanned_bytes":   stats.get("DataScannedInBytes"),
        "engine_ms":       stats.get("EngineExecutionTimeInMillis"),
        "total_ms":        stats.get("TotalExecutionTimeInMillis"),
        "rows":            rows["ResultSet"]["Rows"],
    }
```

### 3.5 Text-to-SQL safety net — `EXPLAIN` before `SELECT`

For agent workflows (see Wave 3 `PATTERN_TEXT_TO_SQL`), the cheapest pre-flight is to run `EXPLAIN (FORMAT JSON) <sql>` — it returns the plan without scanning data. The agent checks:

1. **Did the query parse?** If `FAILED`, ask the LLM to regenerate with the error string.
2. **What tables does it touch?** If a table is not in the allowed set, deny.
3. **How selective is the plan?** A full-table scan on `fact_revenue` (100 M+ rows) with no partition filter → ask for a time filter before running.

```python
def preflight_explain(sql: str, allowed_tables: set[str]) -> dict:
    exec_id = _start(f"EXPLAIN (FORMAT JSON) {sql}")
    q = _await(exec_id, timeout_s=15)
    if q["Status"]["State"] != "SUCCEEDED":
        return {"ok": False, "reason": q["Status"].get("StateChangeReason")}
    rows = ath.get_query_results(QueryExecutionId=exec_id)
    plan_json = rows["ResultSet"]["Rows"][1]["Data"][0]["VarCharValue"]
    # Parse the plan, pull out table names, verify against allowlist.
    import json
    plan = json.loads(plan_json)
    touched = _walk_plan_for_tables(plan)       # implementation elided
    if not touched.issubset(allowed_tables):
        return {"ok": False, "reason": "touches disallowed tables", "touched": list(touched)}
    return {"ok": True, "touched": list(touched), "plan": plan}
```

### 3.6 Bedrock-from-SQL — `USING FUNCTION invoke_model`

Athena engine v3 supports inline LLM calls. The function binds an IAM role that must have `bedrock:InvokeModel` on the target model ARN. The workgroup role (for query execution) is the principal; grant it identity-side.

```sql
-- Classify customer complaints with Claude Haiku 4.5.
USING FUNCTION invoke_model(
    prompt VARCHAR
) RETURNS VARCHAR
TYPE BEDROCK
WITH (
    'model_id' = 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    'max_tokens' = 200,
    'temperature' = 0.1
)
SELECT
    complaint_id,
    invoke_model(
        'Classify this complaint into [billing, product, shipping, other]: '
        || complaint_text
    ) AS label
FROM stg_complaints
WHERE received_date >= current_date - INTERVAL '7' DAY
LIMIT 100;
```

**Budget guardrails:**

- Bedrock calls are billed per-token; 100 M rows × 500 tokens each is ~$50-150 per run on Haiku 4.5.
- Gate behind a row-count filter (`LIMIT 100`) or a WHERE clause before calling invoke_model.
- Monitor `bedrock:InvokeModel` CloudWatch metrics against a budget alarm.

### 3.7 Monolith gotchas

1. **`enforce_workgroup_configuration=False` is the default.** The caller can override result bucket, KMS, engine version. This silently violates compliance. Always set `True` for any workgroup touching production data.
2. **`bytes_scanned_cutoff_per_query` kills the query MID-RUN.** It is not a warning. Set generously for exploratory workgroups (100 GB), tightly for prod (10 GB), very tightly for agent workgroups (1 GB). The killed-query still bills for bytes scanned up to the cutoff.
3. **Result bucket lifecycle MUST be set.** Without expiration, query results pile up at ~100 MB per heavy query; in a month of agent traffic, you have hundreds of GB of unread CSVs.
4. **`CfnNamedQuery` and `CfnPreparedStatement` are per-workgroup.** Moving them to another workgroup requires delete + recreate. If you have 50 shared queries across 3 workgroups, put them in a separate CDK construct that stamps them on each workgroup.
5. **`engine_version="AUTO"` will downgrade.** Athena occasionally defaults to v2 for "AUTO" on specific regions during engine rollouts. Pin `"Athena engine version 3"` explicitly.
6. **Capacity reservations are per-region + minimum 24 DPUs + 1-hour commitment.** A 24-DPU reservation costs ~$8/hour even if idle. Use only when p95 queue depth > 5 or for guaranteed latency windows; otherwise on-demand is cheaper.
7. **Lambda-federation connectors are maintained by AWS Samples.** The JAR ships via Serverless Application Repository (SAR). Pin a specific version — the `master` branch occasionally breaks compatibility. `CfnDataCatalog(Type="LAMBDA", parameters={"function": arn})` references the deployed Lambda; if SAR drift breaks the Lambda, the catalog stops resolving.
8. **Text-to-SQL's biggest failure mode is hallucinated column names.** Grounding the prompt with `glue:GetTable` output (columns + comments) is mandatory. Athena cannot fix a hallucinated column — the query fails at `startQueryExecution` with `SYNTAX_ERROR`. Pre-flight `EXPLAIN` catches this before wasting scan budget.

---

## 4. Micro-Stack Variant

**Use when:** the workgroup + result bucket are a shared horizontal; many caller stacks (agent, analytics app, notebook env) consume.

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** — on any pre-flight / result-poller Lambda entry in callers.
2. **Identity-side grants** — every caller grants itself `athena:*` on the workgroup ARN + result-bucket grants on the SSM-read bucket name; NEVER modify the workgroup resource policy from a consumer stack.
3. **`CfnRule` cross-stack EventBridge** — `Athena Query State Change` events from EB optionally feed a post-query Lambda (for audit / result-hook); the rule lives in `AthenaStack`, target ARN is a string from SSM.
4. **Same-stack bucket + OAC** — if you serve Athena result CSVs through CloudFront (dashboard download), the result bucket + OAC live together. For API-only results, ignore.
5. **KMS ARNs as strings** — the `AthenaCmk.key_arn` is SSM-published. Callers read the string, grant `kms:Decrypt` on the string ARN.

### 4.2 AthenaStack — owns workgroup, result bucket, federation

```python
# stacks/athena_stack.py
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_athena as athena,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct


class AthenaStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) CMK + result bucket (same-stack, breaks the grant cycle).
        cmk = kms.Key(
            self, "AthenaCmk",
            alias=f"alias/{{project_name}}-athena-{stage}",
            enable_key_rotation=True,
            removal_policy=(
                RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
            ),
        )
        results = s3.Bucket(
            self, "AthenaResultsBucket",
            bucket_name=f"{{project_name}}-athena-results-{stage}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=(
                RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
            ),
            auto_delete_objects=(stage != "prod"),
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-results",
                    enabled=True,
                    expiration=Duration.days(30),
                ),
            ],
        )

        # B) Workgroup — enforced, v3, 10 GB cutoff.
        wg = athena.CfnWorkGroup(
            self, "LakehouseAnalystWg",
            name=f"lakehouse-analyst-{stage}",
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=10 * 1024**3,
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3",
                ),
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{results.bucket_name}/queries/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=cmk.key_arn,
                    ),
                    expected_bucket_owner=self.account,
                ),
            ),
        )

        # C) A single prepared statement — shared by all callers.
        athena.CfnPreparedStatement(
            self, "PsTopNByRegion",
            statement_name="top_n_by_region",
            work_group=wg.ref,
            description="Top-N customers by revenue in a given region.",
            query_statement=(
                "SELECT c.customer_id, c.name, SUM(f.amount) AS total "
                "FROM   fact_revenue f "
                "JOIN   dim_customer c ON c.customer_id = f.customer_id "
                "WHERE  c.region = ? "
                "GROUP  BY c.customer_id, c.name "
                "ORDER  BY total DESC "
                "LIMIT  ?"
            ),
        )

        # D) Publish the cross-stack contract.
        ssm.StringParameter(
            self, "WorkgroupNameParam",
            parameter_name=f"/{{project_name}}/{stage}/athena/workgroup_name",
            string_value=wg.ref,
        )
        ssm.StringParameter(
            self, "ResultBucketNameParam",
            parameter_name=f"/{{project_name}}/{stage}/athena/result_bucket_name",
            string_value=results.bucket_name,
        )
        ssm.StringParameter(
            self, "ResultBucketArnParam",
            parameter_name=f"/{{project_name}}/{stage}/athena/result_bucket_arn",
            string_value=results.bucket_arn,
        )
        ssm.StringParameter(
            self, "CmkArnParam",
            parameter_name=f"/{{project_name}}/{stage}/athena/cmk_arn",
            string_value=cmk.key_arn,
        )

        CfnOutput(self, "AthenaWorkgroupName", value=wg.ref)
        CfnOutput(self, "AthenaResultsBucket", value=results.bucket_name)
```

### 4.3 Consumer pattern — caller Lambda in another stack

```python
# stacks/compute_stack.py (an agent / dashboard caller).
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

        # A) SSM contract — all strings/tokens; do not materialise at synth.
        workgroup = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/workgroup_name"
        )
        results_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/result_bucket_name"
        )
        cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/cmk_arn"
        )
        database = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog/database_name"
        )

        # B) Caller Lambda.
        fn = PythonFunction(
            self, "AthenaCallerFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "athena_caller"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(2),
            memory_size=1024,
            environment={
                "WORKGROUP": workgroup,
                "DATABASE":  database,
            },
        )

        # C) Identity-side grants — workgroup, result bucket, KMS, Glue, LF.
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "athena:StartQueryExecution",
                "athena:GetQueryExecution",
                "athena:GetQueryResults",
                "athena:GetQueryResultsStream",
                "athena:StopQueryExecution",
                "athena:GetPreparedStatement",
                "athena:ListPreparedStatements",
            ],
            resources=[
                f"arn:aws:athena:{self.region}:{self.account}:workgroup/{workgroup}",
            ],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetDatabases",
                     "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
                     "lakeformation:GetDataAccess"],
            resources=["*"],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetBucketLocation", "s3:GetObject", "s3:PutObject",
                     "s3:ListBucket", "s3:ListMultipartUploadParts",
                     "s3:AbortMultipartUpload"],
            resources=[
                f"arn:aws:s3:::{results_bucket_name}",
                f"arn:aws:s3:::{results_bucket_name}/*",
            ],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[cmk_arn],
        ))
```

### 4.4 Micro-stack gotchas

- **The workgroup-level `expected_bucket_owner=self.account`** is a safeguard; if a caller somehow ends up writing to a bucket in a different account, the PUT fails. Always set it.
- **Pre-flight `EXPLAIN` consumes workgroup metrics but not scan cost.** For agent workflows with 10× explain overhead, the CloudWatch `QueryCount` metric inflates. Budget accordingly.
- **Cross-region query is NOT supported.** Athena reads S3 in its own region; cross-region requires either S3 cross-region replication or a federated Lambda connector that proxies. Plan data locality.
- **`invoke_model` inside SQL requires the WORKGROUP execution role (not the caller role) to have `bedrock:InvokeModel` on the model ARN.** Easy to miss — the IAM error surfaces as "Bedrock access denied" inside an Athena query result, not as an Athena-level permission.
- **Deletion order** — AthenaStack → ComputeStack (deploy); ComputeStack → AthenaStack (delete). If AthenaStack is deleted with callers still present, their next invocation fails on SSM `ParameterNotFound`.

---

## 5. Swap matrix — when to replace or supplement

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Query engine | Athena v3 (this) | Redshift Serverless + Spectrum | Heavy concurrency + dashboards, materialized views, sub-second repeat queries. Pair: Athena for ad-hoc, Redshift for BI. |
| Query engine | Athena | EMR Serverless + Spark | Heavy Python UDFs, streaming, or Delta/Hudi that Athena does not support in DML. Higher ops cost. |
| Federation | `CfnCatalog` (Glue Federation) | `CfnDataCatalog(Type="LAMBDA")` (Athena Federated Query) | Sources without Glue support (DB2, legacy ODBC). Lambda connector only. Narrower: visible only from Athena. |
| Result cache | Athena result reuse (`AthenaReuseConfiguration`, v3) | External cache (Redis / DynamoDB) | Agent-heavy workloads where same question asked 20×/min. Athena's built-in reuse (up to 7 days) covers most; external cache only when reuse-key is custom. |
| Capacity | On-demand | `CfnCapacityReservation` | p95 queue depth > 5, or regulatory latency SLA. Minimum 24 DPU + 1-hour commit. |
| LLM calls | `USING FUNCTION invoke_model` | External Lambda that calls Bedrock + writes back | Complex prompts, multi-turn, budget enforcement at row-level. Inline invoke_model is simpler for stateless classification. |
| Text-to-SQL pre-flight | `EXPLAIN (FORMAT JSON)` | Dry-run via `CREATE OR REPLACE VIEW ... AS <sql>` then drop | EXPLAIN is cheap (metadata only), supported in v3. View dry-run tests DDL path too; heavier. |
| Workgroup isolation | One workgroup per role / team | One workgroup per environment | Simpler, less sprawl. Accept that per-team cost attribution needs tagging at the query level. |

---

## 6. Worked example — offline synth + agent pre-flight

```python
# tests/test_athena_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.athena_stack import AthenaStack


def test_synth_workgroup_has_enforced_v3_and_kms_and_lifecycle():
    app = cdk.App()
    stack = AthenaStack(app, "Athena-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # Workgroup enforces config, uses v3, KMS on results, 10 GB cutoff.
    tpl.has_resource_properties("AWS::Athena::WorkGroup", {
        "Name": "lakehouse-analyst-dev",
        "State": "ENABLED",
        "WorkGroupConfiguration": Match.object_like({
            "EnforceWorkGroupConfiguration": True,
            "BytesScannedCutoffPerQuery":   10 * 1024**3,
            "EngineVersion": Match.object_like({
                "SelectedEngineVersion": "Athena engine version 3",
            }),
            "ResultConfiguration": Match.object_like({
                "EncryptionConfiguration": Match.object_like({
                    "EncryptionOption": "SSE_KMS",
                }),
                "ExpectedBucketOwner": Match.any_value(),
            }),
        }),
    })

    # Result bucket has a 30-day expire.
    tpl.has_resource_properties("AWS::S3::Bucket", {
        "LifecycleConfiguration": Match.object_like({
            "Rules": Match.array_with([
                Match.object_like({
                    "ExpirationInDays": 30,
                    "Id":               "expire-old-results",
                }),
            ]),
        }),
    })

    # Prepared statement published.
    tpl.has_resource_properties("AWS::Athena::PreparedStatement", {
        "StatementName": "top_n_by_region",
        "QueryStatement": Match.string_like_regexp(".*fact_revenue.*\\?.*\\?.*"),
    })


# tests/test_explain_preflight.py
"""Integration test — spins the real workgroup, checks EXPLAIN shape."""
import os, json, boto3, pytest, time


@pytest.mark.integration
def test_explain_on_fact_revenue_does_not_scan():
    ath = boto3.client("athena")
    wg  = os.environ["WORKGROUP"]
    db  = os.environ["DATABASE"]

    sql = "SELECT customer_id, SUM(amount) FROM fact_revenue GROUP BY customer_id"
    exec_id = ath.start_query_execution(
        QueryString=f"EXPLAIN (FORMAT JSON) {sql}",
        QueryExecutionContext={"Database": db},
        WorkGroup=wg,
    )["QueryExecutionId"]
    for _ in range(30):
        q = ath.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(0.5)
    assert q["Status"]["State"] == "SUCCEEDED"
    # EXPLAIN must NOT scan data — DataScannedInBytes should be 0.
    assert q["Statistics"]["DataScannedInBytes"] == 0
    # Result rows contain the plan JSON on row 1.
    rows = ath.get_query_results(QueryExecutionId=exec_id)["ResultSet"]["Rows"]
    plan_json = rows[1]["Data"][0]["VarCharValue"]
    plan = json.loads(plan_json)
    # Plan references fact_revenue.
    assert "fact_revenue" in json.dumps(plan)
```

---

## 7. References

- AWS docs — *Amazon Athena user guide* (workgroups, engine v3, Iceberg DML).
- AWS docs — *Athena Federated Query + Glue Catalog Federation via `CfnCatalog`*.
- AWS docs — *Athena + Bedrock `USING FUNCTION invoke_model`* (v3 GA 2025).
- AWS docs — *Capacity Reservations* (`CfnCapacityReservation`, `CfnCapacityAssignmentConfiguration`).
- `DATA_GLUE_CATALOG.md` — the metadata plane Athena queries against; comments = grounding for text-to-SQL.
- `DATA_ICEBERG_S3_TABLES.md` — the managed-Iceberg store; reached via `s3tablescatalog/<bucket>` from Athena.
- `DATA_LAKEHOUSE_ICEBERG.md` — self-managed Iceberg; also reached via Athena default catalog.
- `DATA_LAKE_FORMATION.md` — LF row/column filters applied at query time by Athena.
- `PATTERN_TEXT_TO_SQL.md` (Wave 3) — Bedrock-powered SQL generation grounded on this workgroup.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Engine v3 pin, enforced workgroup config, result bucket lifecycle, 10 GB scan cutoff, `invoke_model` Bedrock-from-SQL, EXPLAIN-based agent pre-flight, capacity reservations (prod-only). 8 monolith gotchas, 5 micro-stack gotchas, 8-row swap matrix, pytest synth + EXPLAIN integration harness.
