# SOP — Text-to-SQL over Athena (catalog-grounded, EXPLAIN-preflighted, LF-safe)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · Amazon Bedrock Claude Sonnet 4.7 for SQL generation · Claude Haiku 4.5 for cheap classification · Amazon Athena engine v3 (Iceberg DML, prepared statements, `EXPLAIN (FORMAT JSON)`) · `PATTERN_CATALOG_EMBEDDINGS` as the grounding index · Lake Formation for row/column enforcement (defence-in-depth) · Bedrock Guardrails optional content policy · CloudWatch metrics for cost guard

---

## 1. Purpose

- Provide the deep-dive for **text-to-SQL on a lakehouse** — the pattern where a business user's natural-language question ("revenue by region for renewing customers last quarter") becomes an executable Athena query, runs against governed data, and returns a typed result + the SQL + the data lineage. The *responsible* version: grounded on the real catalog, safety-gated at three checkpoints, LF-policy-enforced at execution, and cost-capped.
- Codify the **four-phase pipeline** that every production text-to-SQL must implement:
  1. **DISCOVER** — query `PATTERN_CATALOG_EMBEDDINGS` 3-pass (db → table → column) with the user's question + LF-Tag filter from identity. Produces a shortlist of tables + columns.
  2. **GENERATE** — prompt Claude Sonnet 4.7 with the shortlisted schema snippet + the user question + prior conversation turns (if agentic). Model returns SQL only, no prose.
  3. **PREFLIGHT** — run `EXPLAIN (FORMAT JSON)` against Athena. Validates the SQL parses, lists touched tables, estimates scan. Feed errors back to the LLM (up to N retries).
  4. **EXECUTE** — run the query in a scan-capped workgroup; Lake Formation enforces row/column filters at runtime (defence-in-depth — even if the LLM hallucinates a column the caller cannot see, LF kills it). Return results with the SQL for transparency.
- Codify the **three safety gates** — (a) table allowlist from DISCOVER; (b) syntax + touched-table check from PREFLIGHT; (c) LF runtime enforcement. Any one of these can veto; all three together make "LLM-wrote-a-DROP-TABLE" harmless.
- Codify the **prompt grounding contract** — the system prompt embeds: (i) the database + region dialect (Athena/Presto syntax), (ii) the shortlisted tables as a compact DDL stub (`CREATE TABLE fact_revenue (order_id bigint, ...);`), (iii) the column comments (the AI substrate — see `DATA_GLUE_CATALOG`), (iv) business rules (cost-centre mapping, currency conventions), (v) conversational context (prior turns, if any), (vi) the question. Output format: `sql` field only, no prose.
- Codify the **prepared-statement fast path** — if the question matches a catalogued named query (via semantic similarity > 0.85 on the question embedding vs the statement description), execute the prepared statement with extracted parameters instead of generating new SQL. Faster, cheaper, deterministic.
- Codify the **error-correction loop** — on Athena `SYNTAX_ERROR`, `COLUMN_NOT_FOUND`, `TABLE_NOT_FOUND`, feed the error message back to the LLM with a shorter system prompt ("Your SQL produced: {error}. Rewrite.") and retry. Cap at 3 retries; bail out to the user with the last error.
- Codify **cost guardrails** — workgroup `BytesScannedCutoffPerQuery=1 GB` for agent workgroups (tighter than human-analyst 10 GB); per-caller daily scan budget tracked in DynamoDB; CloudWatch alarm on `DataScannedInBytes` p95 spike.
- Codify **PII leakage prevention** — Column-level `sensitivity=pii` is carried through `PATTERN_CATALOG_EMBEDDINGS`; the generator prompt is instructed to NEVER SELECT pii columns unless the caller's `max_sensitivity` allows. Defence-in-depth: LF `DataCellsFilter` strips the column at runtime regardless.
- Include when the SOW signals: "text-to-SQL", "natural language query", "ask questions of data", "LLM-powered analytics", "conversational BI", "self-service analytics with LLM", "NL2SQL", "Athena + Bedrock", "analyst assistant".
- This partial is the **CENTRAL AGENT PATTERN** of the AI-Native Lakehouse kit. Consumed by `PATTERN_ENTERPRISE_CHAT_ROUTER` (blended Q&A), `kits/ai-native-lakehouse` (hero demo), and any BI-over-LLM UI. Depends on: `PATTERN_CATALOG_EMBEDDINGS` (grounding), `DATA_ATHENA` (execution), `DATA_LAKE_FORMATION` (enforcement), `DATA_GLUE_CATALOG` (source of truth).

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the text-to-SQL Lambda, DynamoDB budget table, CloudWatch alarm, and the agent workgroup | **§3 Monolith Variant** |
| `TextToSqlStack` owns the Lambda + DDB + alarms; `AthenaStack` already owns the workgroup; `CatalogEmbeddingStack` already owns the grounding index; integration via SSM-read ARNs | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Dependencies cross two prior stacks.** The text-to-SQL Lambda queries both `PATTERN_CATALOG_EMBEDDINGS` (read catalog vectors) AND `DATA_ATHENA` (execute queries). In a monolith POC both live in-stack; in production they are separate stacks with their own lifecycles.
2. **DDB budget table is per-caller.** If the same caller hits text-to-SQL from multiple surfaces (chat router, standalone NL2SQL UI, agent SDK), the budget table is a shared resource. Owner: `TextToSqlStack`.
3. **Agent workgroup is 1 GB scan cutoff, human workgroup is 10 GB.** Different workgroups for different callers. The agent workgroup is typically defined in `DATA_ATHENA §3.2` — but the PREPARED STATEMENTS for text-to-SQL are better owned in `TextToSqlStack` (they evolve with the prompt library).
4. **Bedrock model ARNs cross-region.** If the region does not have Claude Sonnet 4.7 deployed, the Lambda must call a remote region — add `bedrock:InvokeModel` grant on the remote-region ARN + VPC endpoint in the remote region.
5. **Prompt library versioning.** System prompts evolve frequently. Keep them in `prompts/` as versioned assets, deployed alongside the Lambda; Bedrock Prompt Management is an option (see `DATA_BEDROCK_PROMPT_MGMT` sibling).

Micro-Stack fixes by: (a) owning text-to-SQL Lambda + DDB budget + retry-logic + prompt-library assets + agent workgroup in `TextToSqlStack`; (b) reading `CatalogEmbeddingStack.IdxTableArn` / `IdxColumnArn` + `AthenaStack.WorkgroupName` / `CmkArn` via SSM; (c) exposing `TextToSqlFn.function_arn` via SSM for the chat-router consumer.

---

## 3. Monolith Variant

**Use when:** single-stack POC. All components live in one `cdk.Stack`.

### 3.1 Architecture

```
                       ┌────────────────────────────────────────┐
                       │           Caller (UI, agent)           │
                       │  POST /ask {question, caller_id,       │
                       │             caller_domain,             │
                       │             max_sensitivity}           │
                       └─────────────────────┬──────────────────┘
                                             │
                                             ▼
                            ┌─────────────────────────────────┐
                            │  TextToSqlFn (Lambda)           │
                            │                                 │
                            │  ─── PHASE 1: DISCOVER ─────    │
                            │  PATTERN_CATALOG_EMBEDDINGS:    │
                            │    - 3-pass (db → tbl → col)    │
                            │    - LF-Tag filter from caller  │
                            │  → tables[], columns[]          │
                            │                                 │
                            │  ─── PHASE 2: FAST PATH ────    │
                            │  Similarity(question, PS desc): │
                            │    if > 0.85 → run Prepared     │
                            │       Statement, skip to EXEC   │
                            │                                 │
                            │  ─── PHASE 3: GENERATE ────     │
                            │  Claude Sonnet 4.7:             │
                            │    system = DDL stub + rules    │
                            │    user   = question            │
                            │  → sql (string)                 │
                            │                                 │
                            │  ─── PHASE 4: PREFLIGHT ───     │
                            │  Athena EXPLAIN (FORMAT JSON):  │
                            │    - validates syntax           │
                            │    - lists touched tables       │
                            │    - checks allowlist           │
                            │  If fail → feed error to LLM,   │
                            │   retry up to 3×                │
                            │                                 │
                            │  ─── PHASE 5: BUDGET CHECK ─    │
                            │  DDB read: daily_scanned_gb     │
                            │    if + estimated > budget →    │
                            │      reject                     │
                            │                                 │
                            │  ─── PHASE 6: EXECUTE ────      │
                            │  Athena StartQueryExecution:    │
                            │    WorkGroup = agent-1gb-cutoff │
                            │    LF enforces row/column       │
                            │                                 │
                            │  ─── PHASE 7: RESPOND ────      │
                            │  {sql, columns, rows,           │
                            │   scanned_bytes, lineage,       │
                            │   confidence, retries}          │
                            └──────────────┬──────────────────┘
                                           │
    ┌──────────────────────────────────────┼──────────────────────────────────┐
    ▼                                      ▼                                  ▼
┌─────────────┐      ┌──────────────────────────┐         ┌──────────────────────┐
│ Catalog     │      │ Bedrock                  │         │ DynamoDB             │
│ Embed Idx   │      │   Claude Sonnet 4.7      │         │ daily_budget_<caller>│
│ (query)     │      │   Claude Haiku 4.5 (FT)  │         │ (scan tracking)      │
└─────────────┘      └──────────────────────────┘         └──────────────────────┘
                                │
                                ▼
                        ┌────────────────────┐
                        │  Athena WG: agent  │
                        │   1 GB cutoff      │
                        │   LF-enforced      │
                        └────────────────────┘
```

### 3.2 CDK — `_create_text_to_sql()` method body

```python
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_athena as athena,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_sns as sns,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction


def _create_text_to_sql(self, stage: str) -> None:
    """Monolith variant. Assumes self.{idx_table_arn, idx_column_arn,
    athena_workgroup_name, athena_cmk_arn, lake_bucket_name} exist."""

    # A) Agent-scoped Athena workgroup — 1 GB scan cutoff (tighter than
    #    human workgroup's 10 GB). Workgroup-level `EnforceWorkGroup
    #    Configuration=True` overrides any client override attempt.
    self.agent_workgroup = athena.CfnWorkGroup(
        self, "AgentWorkgroup",
        name=f"lakehouse-agent-{stage}",
        state="ENABLED",
        description="Text-to-SQL agent workgroup — 1 GB scan cutoff.",
        work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
            enforce_work_group_configuration=True,
            publish_cloud_watch_metrics_enabled=True,
            bytes_scanned_cutoff_per_query=1024**3,          # 1 GB
            engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                selected_engine_version="Athena engine version 3",
            ),
            result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                output_location=f"s3://{self.athena_results_bucket}/agent/",
                encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                    encryption_option="SSE_KMS",
                    kms_key=self.athena_cmk_arn,
                ),
                expected_bucket_owner=Stack.of(self).account,
            ),
        ),
    )

    # B) Per-caller daily budget table — tracks scan bytes.
    #    Partition: caller_id (HASH) + date (RANGE).
    #    TTL: rows auto-expire after 35 days.
    self.budget_table = ddb.Table(
        self, "ScanBudgetTable",
        table_name=f"{{project_name}}-agent-scan-budget-{stage}",
        partition_key=ddb.Attribute(name="caller_id", type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name="date",         type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="ttl_epoch",
        encryption=ddb.TableEncryption.AWS_MANAGED,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # C) Text-to-SQL Lambda.
    self.t2s_fn = PythonFunction(
        self, "TextToSqlFn",
        entry=str(Path(__file__).parent.parent / "lambda" / "text_to_sql"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.minutes(2),
        memory_size=1024,
        reserved_concurrent_executions=50,
        environment={
            "IDX_TABLE_ARN":          self.idx_table_arn,
            "IDX_COLUMN_ARN":         self.idx_column_arn,
            "EMBED_MODEL_ID":         "amazon.titan-embed-text-v2:0",
            "EMBED_DIM":              "1024",
            "GEN_MODEL_ID":           "us.anthropic.claude-sonnet-4-7-20260109-v1:0",
            "CLASSIFY_MODEL_ID":      "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "ATHENA_WORKGROUP":       self.agent_workgroup.ref,
            "BUDGET_TABLE":           self.budget_table.table_name,
            "DAILY_SCAN_BUDGET_GB":   "100",       # per-caller, per-day
            "MAX_RETRIES":            "3",
            "DEFAULT_DATABASE":       f"lakehouse_{stage}",
            "PROMPT_LIBRARY_VERSION": "v1",
        },
    )

    # D) Identity-side grants — this pattern is THE SPINE.
    self.t2s_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
        resources=[self.idx_table_arn, self.idx_column_arn],
    ))
    self.t2s_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[
            f"arn:aws:bedrock:{Stack.of(self).region}::"
            f"foundation-model/amazon.titan-embed-text-v2:0",
            f"arn:aws:bedrock:{Stack.of(self).region}:*:"
            f"inference-profile/us.anthropic.claude-sonnet-4-7-20260109-v1:0",
            f"arn:aws:bedrock:{Stack.of(self).region}:*:"
            f"inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
    ))
    self.t2s_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "athena:StartQueryExecution", "athena:GetQueryExecution",
            "athena:GetQueryResults", "athena:StopQueryExecution",
            "athena:GetPreparedStatement", "athena:ListPreparedStatements",
            "athena:ListNamedQueries", "athena:GetNamedQuery",
        ],
        resources=[
            f"arn:aws:athena:{Stack.of(self).region}:"
            f"{Stack.of(self).account}:workgroup/{self.agent_workgroup.ref}",
        ],
    ))
    self.t2s_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["glue:GetDatabase", "glue:GetDatabases",
                 "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
                 "lakeformation:GetDataAccess"],
        resources=["*"],
    ))
    self.t2s_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket",
                 "s3:GetBucketLocation"],
        resources=[
            f"arn:aws:s3:::{self.athena_results_bucket}",
            f"arn:aws:s3:::{self.athena_results_bucket}/*",
            f"arn:aws:s3:::{self.lake_bucket_name}",
            f"arn:aws:s3:::{self.lake_bucket_name}/*",
        ],
    ))
    self.t2s_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.athena_cmk_arn],
    ))
    self.budget_table.grant_read_write_data(self.t2s_fn)

    # E) CloudWatch alarm — budget-breach alert (account-wide summary).
    breach_topic = sns.Topic(
        self, "T2SBudgetBreachTopic",
        topic_name=f"{{project_name}}-t2s-budget-breach-{stage}",
    )
    cw.Alarm(
        self, "DailyScanSpikeAlarm",
        alarm_description="Agent workgroup scanned > 500 GB in one day.",
        metric=cw.Metric(
            namespace="AWS/Athena",
            metric_name="DataScannedInBytes",
            dimensions_map={"WorkGroup": self.agent_workgroup.ref},
            statistic="Sum",
            period=Duration.hours(1),
        ),
        threshold=500 * 1024**3,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    ).add_alarm_action(cw_actions.SnsAction(breach_topic))

    # F) Outputs.
    CfnOutput(self, "TextToSqlFnArn", value=self.t2s_fn.function_arn)
    CfnOutput(self, "AgentWorkgroup", value=self.agent_workgroup.ref)
    CfnOutput(self, "BudgetTable",    value=self.budget_table.table_name)
```

### 3.3 Lambda handler — the four-phase pipeline

```python
# lambda/text_to_sql/handler.py
"""
Text-to-SQL handler — four-phase pipeline.

In:  {
  "question":        "revenue by region for renewing customers last quarter",
  "caller_id":       "user:alice",
  "caller_domain":   "finance",
  "max_sensitivity": "internal",     # public | internal | confidential | pii
  "conversation":    [               # optional prior turns
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
Out: {
  "ok":              true,
  "sql":             "SELECT ...",
  "columns":         [{"name":"region","type":"varchar"},{"name":"total","type":"decimal"}],
  "rows":            [...],
  "scanned_bytes":   3_500_000_000,
  "engine_ms":       420,
  "retries":         1,
  "lineage": {                       # for UI/audit
    "databases": ["lakehouse_prod"],
    "tables":    ["fact_revenue","dim_customer"],
    "columns":   ["amount","region","renewal_date","customer_id"]
  }
}
"""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3

IDX_TABLE_ARN        = os.environ["IDX_TABLE_ARN"]
IDX_COLUMN_ARN       = os.environ["IDX_COLUMN_ARN"]
EMBED_MODEL_ID       = os.environ["EMBED_MODEL_ID"]
EMBED_DIM            = int(os.environ["EMBED_DIM"])
GEN_MODEL_ID         = os.environ["GEN_MODEL_ID"]
ATHENA_WORKGROUP     = os.environ["ATHENA_WORKGROUP"]
BUDGET_TABLE         = os.environ["BUDGET_TABLE"]
DAILY_SCAN_BUDGET_GB = int(os.environ["DAILY_SCAN_BUDGET_GB"])
MAX_RETRIES          = int(os.environ["MAX_RETRIES"])
DEFAULT_DATABASE     = os.environ["DEFAULT_DATABASE"]

bedrock = boto3.client("bedrock-runtime")
s3v     = boto3.client("s3vectors")
athena  = boto3.client("athena")
ddb     = boto3.client("dynamodb")


# ---- Phase 1: DISCOVER ----------------------------------------------------

def _embed_text(text: str) -> list[float]:
    body = json.dumps({"inputText": text, "dimensions": EMBED_DIM})
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=body, accept="application/json", contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


_SENSITIVITY_RANK = {
    "public": ["public"],
    "internal": ["public", "internal"],
    "confidential": ["public", "internal", "confidential"],
    "pii": ["public", "internal", "confidential", "pii"],
}


def discover(question: str, caller_domain: str, max_sensitivity: str) -> dict[str, Any]:
    q_vec = _embed_text(question)
    allowed = _SENSITIVITY_RANK.get(max_sensitivity, _SENSITIVITY_RANK["internal"])

    # Pass 2: table-level (we skip db-level for compactness in this pattern —
    # a 3-pass version is in PATTERN_SEMANTIC_DATA_DISCOVERY).
    tables = s3v.query_vectors(
        indexArn=IDX_TABLE_ARN,
        queryVector=q_vec,
        topK=8,
        filter={
            "domain":      caller_domain,
            "sensitivity": {"$in": allowed},
        },
        returnMetadata=True, returnDistance=True,
    )["matches"]

    if not tables:
        return {"ok": False, "reason": "no-accessible-tables"}

    table_names = [m["metadata"]["table_name"] for m in tables[:5]]

    # Pass 3: column-level, scoped to the shortlisted tables.
    columns = s3v.query_vectors(
        indexArn=IDX_COLUMN_ARN,
        queryVector=q_vec,
        topK=40,
        filter={
            "table_name":  {"$in": table_names},
            "sensitivity": {"$in": allowed},
        },
        returnMetadata=True, returnDistance=True,
    )["matches"]

    # Compose a DDL stub string for the generator prompt.
    tables_by_name: dict[str, dict] = {}
    for t in tables[:5]:
        m = t["metadata"]
        tables_by_name[m["table_name"]] = {
            "database":    m["database_name"],
            "description": m["source_text"],
            "table_type":  m["table_type"],
            "columns":     json.loads(m.get("columns_json") or "[]"),
        }
    # Override the "columns" from the table-level vectors with the ranked
    # column-level matches (they preserve the best-matching sensitivity gate
    # + column comments are the embedding substrate we actually want).
    for c in columns:
        cm = c["metadata"]
        tn = cm["table_name"]
        if tn not in tables_by_name:
            continue
        cols = tables_by_name[tn]["columns"]
        match = next((x for x in cols if x["name"] == cm["column_name"]), None)
        if match:
            match["comment"] = cm.get("source_text", "")
            match["sensitivity"] = cm.get("sensitivity", "internal")

    return {
        "ok":     True,
        "tables": tables_by_name,
        "allowed_tables":   set(tables_by_name.keys()),
        "lineage_columns":  [(c["metadata"]["table_name"], c["metadata"]["column_name"])
                             for c in columns[:10]],
    }


# ---- Phase 3: GENERATE ----------------------------------------------------

_SYSTEM_PROMPT = """\
You are a SQL generator for Amazon Athena engine version 3 (Presto/Trino
syntax). Given a natural-language question and a list of tables with their
columns and comments, produce a single SELECT statement that answers the
question.

HARD RULES:
1. Return ONLY JSON in the shape {"sql": "<query>"}. No prose, no code fences.
2. Use ONLY the tables + columns listed below. Do NOT invent names.
3. Never SELECT columns flagged sensitivity=pii unless the caller explicitly asks.
4. DML (INSERT, UPDATE, DELETE, MERGE), DDL (CREATE, DROP, ALTER), TCL and
   any other non-SELECT statement is FORBIDDEN.
5. Include a time filter on the partition column whenever the table has one
   (look for 'ts' or 'date' columns with partition-like comments).
6. For Iceberg tables, prefer `FOR TIMESTAMP AS OF` only if the question asks
   about a historical snapshot.
7. Always qualify columns with the table alias when there is a JOIN.
8. Prefer Athena function names (e.g. date_trunc, current_date, interval '3' day).

AVAILABLE TABLES (DDL stubs):
{ddl_stub}

BUSINESS RULES:
- Revenue is stored in fact_revenue.amount in the 'currency' column; sum in
  USD only if the currency is USD.
- Renewal date lives in dim_customer.renewal_date.
- "Last quarter" means the previous full calendar quarter.
"""

_USER_TEMPLATE_FIRST   = "QUESTION: {question}"
_USER_TEMPLATE_REPAIR  = (
    "Your previous SQL failed with the error:\n{error}\n\n"
    "Original question: {question}\n"
    "Regenerate the SQL, fixing the error. Same rules apply."
)


def _ddl_stub(discover_out: dict) -> str:
    lines: list[str] = []
    for name, info in discover_out["tables"].items():
        db = info["database"]
        cols = []
        for c in info["columns"]:
            note = c.get("comment", "").replace("\n", " ")
            sens = c.get("sensitivity")
            tag = f" /* sensitivity={sens} */" if sens else ""
            cols.append(f"  {c['name']} {c.get('type','string')}{tag}  -- {note}")
        lines.append(f"-- {info['description']}")
        lines.append(f"CREATE TABLE {db}.{name} (")
        lines.extend(cols)
        lines.append(");")
    return "\n".join(lines)


def generate_sql(
    question: str,
    discover_out: dict,
    *,
    prior_error: str | None = None,
    conversation: list[dict] | None = None,
) -> str:
    sys_prompt = _SYSTEM_PROMPT.format(ddl_stub=_ddl_stub(discover_out))
    msgs: list[dict] = list(conversation or [])
    if prior_error:
        user = _USER_TEMPLATE_REPAIR.format(question=question, error=prior_error)
    else:
        user = _USER_TEMPLATE_FIRST.format(question=question)
    msgs.append({"role": "user", "content": [{"type": "text", "text": user}]})

    resp = bedrock.invoke_model(
        modelId=GEN_MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens":        1024,
            "temperature":       0.1,
            "system":            sys_prompt,
            "messages":          msgs,
        }),
        accept="application/json",
        contentType="application/json",
    )
    raw = json.loads(resp["body"].read())
    text = raw["content"][0]["text"].strip()
    # Model should return JSON; guard against accidental code fences.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    obj = json.loads(text)
    return obj["sql"].strip()


# ---- Phase 4: PREFLIGHT ---------------------------------------------------

_FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP",
              "CREATE", "ALTER", "TRUNCATE", "GRANT", "REVOKE",
              "CALL", "COPY", "USE")


class PreflightError(Exception):
    def __init__(self, reason: str, details: str = ""):
        super().__init__(reason)
        self.reason  = reason
        self.details = details


def _lex_veto(sql: str) -> None:
    upper = sql.upper()
    for kw in _FORBIDDEN:
        # word-boundary check
        import re
        if re.search(rf"\b{kw}\b", upper):
            raise PreflightError("forbidden-keyword", f"contains {kw}")
    if not upper.lstrip().startswith(("SELECT", "WITH")):
        raise PreflightError("not-a-select", sql[:100])


def _run_explain(sql: str) -> dict:
    exec_id = athena.start_query_execution(
        QueryString=f"EXPLAIN (FORMAT JSON) {sql}",
        QueryExecutionContext={"Database": DEFAULT_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
    )["QueryExecutionId"]
    end = time.time() + 15
    while time.time() < end:
        q = athena.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]
        st = q["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            if st != "SUCCEEDED":
                raise PreflightError(
                    "explain-failed",
                    q["Status"].get("StateChangeReason", ""),
                )
            rows = athena.get_query_results(QueryExecutionId=exec_id)["ResultSet"]["Rows"]
            plan_json = rows[1]["Data"][0]["VarCharValue"]
            return json.loads(plan_json)
        time.sleep(0.5)
    athena.stop_query_execution(QueryExecutionId=exec_id)
    raise PreflightError("explain-timeout", "")


def _extract_tables_from_plan(plan: dict) -> set[str]:
    """Walk the plan JSON for all TableScan node.relation.name values."""
    found: set[str] = set()
    def walk(node):
        if isinstance(node, dict):
            if node.get("name", "").lower().startswith("tablescan") or "relation" in node:
                rel = node.get("relation") or {}
                if isinstance(rel, dict) and "name" in rel:
                    found.add(rel["name"].split(".")[-1])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(plan)
    return found


def preflight(sql: str, allowed_tables: set[str]) -> dict:
    _lex_veto(sql)
    plan = _run_explain(sql)
    touched = _extract_tables_from_plan(plan)
    if not touched.issubset(allowed_tables):
        raise PreflightError(
            "table-not-in-allowlist",
            f"touched={touched}, allowed={allowed_tables}",
        )
    return {"plan": plan, "touched": touched}


# ---- Phase 5: BUDGET CHECK ------------------------------------------------

def _today_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def _budget_ttl() -> int:
    return int((datetime.now(tz=timezone.utc) + timedelta(days=35)).timestamp())


def check_budget(caller_id: str, estimated_gb: float = 0.0) -> tuple[bool, float]:
    resp = ddb.get_item(
        TableName=BUDGET_TABLE,
        Key={"caller_id": {"S": caller_id}, "date": {"S": _today_utc_iso()}},
    )
    used_bytes = int(resp.get("Item", {}).get("scanned_bytes", {}).get("N", "0"))
    used_gb = used_bytes / 1024**3
    if used_gb + estimated_gb > DAILY_SCAN_BUDGET_GB:
        return False, used_gb
    return True, used_gb


def update_budget(caller_id: str, scanned_bytes: int) -> None:
    ddb.update_item(
        TableName=BUDGET_TABLE,
        Key={"caller_id": {"S": caller_id}, "date": {"S": _today_utc_iso()}},
        UpdateExpression="SET scanned_bytes = if_not_exists(scanned_bytes, :zero) + :delta, "
                         "ttl_epoch = :ttl",
        ExpressionAttributeValues={
            ":zero":  {"N": "0"},
            ":delta": {"N": str(scanned_bytes)},
            ":ttl":   {"N": str(_budget_ttl())},
        },
    )


# ---- Phase 6: EXECUTE -----------------------------------------------------

def execute(sql: str) -> dict:
    exec_id = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": DEFAULT_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
    )["QueryExecutionId"]
    end = time.time() + 60
    while time.time() < end:
        q = athena.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]
        st = q["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if st != "SUCCEEDED":
        raise PreflightError("execution-failed",
                              q["Status"].get("StateChangeReason", ""))
    stats = q.get("Statistics", {})
    results = athena.get_query_results(QueryExecutionId=exec_id)
    rows = results["ResultSet"]["Rows"]
    col_info = results["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    columns = [{"name": c["Name"], "type": c["Type"]} for c in col_info]
    # row[0] is the header; row[1..] are data.
    data = [
        {columns[i]["name"]: cell.get("VarCharValue")
         for i, cell in enumerate(r["Data"])}
        for r in rows[1:]
    ]
    return {
        "exec_id":       exec_id,
        "columns":       columns,
        "rows":          data,
        "scanned_bytes": stats.get("DataScannedInBytes", 0),
        "engine_ms":     stats.get("EngineExecutionTimeInMillis", 0),
    }


# ---- Orchestration --------------------------------------------------------

def lambda_handler(event, _ctx):
    question        = event["question"]
    caller_id       = event["caller_id"]
    caller_domain   = event["caller_domain"]
    max_sensitivity = event.get("max_sensitivity", "internal")
    conversation    = event.get("conversation")

    # Phase 1
    disc = discover(question, caller_domain, max_sensitivity)
    if not disc["ok"]:
        return {"ok": False, "reason": disc["reason"], "phase": "discover"}

    # Phase 5 (part a) — pre-execution budget check (cheap; heuristic 0 GB).
    ok, used = check_budget(caller_id, estimated_gb=0.0)
    if not ok:
        return {"ok": False, "reason": "budget-exceeded", "used_gb": used}

    # Phase 3 + 4 — generate + preflight with retry loop.
    prior_error: str | None = None
    sql: str = ""
    retries = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sql = generate_sql(
                question, disc,
                prior_error=prior_error,
                conversation=conversation,
            )
            pre = preflight(sql, disc["allowed_tables"])
            break
        except PreflightError as e:
            prior_error = f"{e.reason}: {e.details}"
            retries = attempt
            if attempt == MAX_RETRIES:
                return {
                    "ok":      False,
                    "reason":  "preflight-failed-after-retries",
                    "sql":     sql,
                    "error":   prior_error,
                    "retries": retries,
                }
        except Exception as e:      # noqa: BLE001
            return {"ok": False, "reason": "generate-failed", "error": str(e)}

    # Phase 6 — execute.
    try:
        exe = execute(sql)
    except PreflightError as e:
        return {"ok": False, "reason": "execute-failed",
                "sql": sql, "error": f"{e.reason}: {e.details}"}

    # Phase 5 (part b) — post-execution budget update.
    update_budget(caller_id, exe["scanned_bytes"])

    return {
        "ok":             True,
        "sql":            sql,
        "columns":        exe["columns"],
        "rows":           exe["rows"],
        "scanned_bytes":  exe["scanned_bytes"],
        "engine_ms":      exe["engine_ms"],
        "retries":        retries,
        "lineage": {
            "databases": list({info["database"] for info in disc["tables"].values()}),
            "tables":    list(disc["allowed_tables"]),
            "columns":   [f"{t}.{c}" for (t, c) in disc["lineage_columns"]],
        },
    }
```

### 3.4 Prompt-library versioning

Prompts live at `lambda/text_to_sql/prompts/v1/system.txt`. When you iterate:

```
lambda/text_to_sql/prompts/
├── v1/
│   ├── system.txt
│   └── user_repair.txt
└── v2/
    ├── system.txt                ← new
    └── user_repair.txt
```

Flip `PROMPT_LIBRARY_VERSION=v2` in the Lambda env. Keep v1 in the bundle for rollback. For more structured prompt governance, migrate to Bedrock Prompt Management (see `mlops/24_bedrock_prompt_management.md`) — but a POC kit can stay in-source.

### 3.5 Prepared-statement fast path

If the client has a curated library of ~50 named queries / prepared statements with descriptive names + `description` fields, skip the LLM entirely for matching questions:

```python
def try_prepared_fast_path(question: str, db: str) -> dict | None:
    # List prepared statements in the workgroup.
    ps = athena.list_prepared_statements(WorkGroup=ATHENA_WORKGROUP)
    best = None
    best_score = 0.85            # hard floor — below = regenerate
    q_vec = _embed_text(question)
    for s in ps.get("PreparedStatements", []):
        desc = s.get("Description", "")
        if not desc:
            continue
        d_vec = _embed_text(desc)
        # cosine: Titan v2 vectors are unit-normalised, so dot product = cosine
        score = sum(a * b for a, b in zip(q_vec, d_vec))
        if score > best_score:
            best_score = score
            best = s
    if not best:
        return None
    # Extract parameters from the question with Haiku — one quick LLM call.
    params = extract_params_with_haiku(question, best)
    # Execute.
    exec_id = athena.start_query_execution(
        QueryString=f"EXECUTE {best['StatementName']}",
        ExecutionParameters=params,
        QueryExecutionContext={"Database": db},
        WorkGroup=ATHENA_WORKGROUP,
    )["QueryExecutionId"]
    # ... await + return ...
```

### 3.6 Monolith gotchas

1. **`bedrock-runtime.invoke_model` on Claude requires an INFERENCE PROFILE ARN**, not a foundation-model ARN. For Claude 4.x, the ARN shape is `arn:aws:bedrock:<region>:<account>:inference-profile/us.anthropic.claude-sonnet-4-7-20260109-v1:0`. Using the foundation-model ARN directly works for Titan embed models but fails for Claude with cryptic "Operation not allowed" errors.
2. **`invoke_model` response body is `StreamingBody` — read ONCE.** If you call `.read()` twice, the second returns `b""`. Parse and cache.
3. **Claude sometimes wraps JSON in code fences** despite "no prose" instruction. Strip ` ``` ` and ` ```json ` at parse time.
4. **`EXPLAIN` against a non-existent table** returns `TABLE_NOT_FOUND` in the `StateChangeReason`. Feed the table name back to the LLM: "Your query references `{table}`, but it does not exist. Available tables: {allowed_tables}." Haiku-quality prompts often recover on attempt 2.
5. **Touched-tables extraction from the EXPLAIN plan is AST-walk-heavy.** The plan JSON uses different node shapes for TableScan, IndexScan, PartitionedTableScan. Our implementation is minimal; production should handle at least: TableScan, Project, Filter, Aggregate, Join. Hallucinated `WITH` CTEs are flattened to their base tables in the plan — do not deny CTEs; look at the leaf nodes.
6. **LF row/column filters silently drop rows/columns at execution time.** If the LLM asks `SELECT ssn FROM dim_customer` and LF strips `ssn`, the result is an empty-string column, not an error. For clarity, instruct the LLM to not ask for pii-flagged columns in the first place. LF is the last line of defence, not the only one.
7. **`DAILY_SCAN_BUDGET_GB` is per-caller, per-day.** Aggregated across ALL text-to-SQL calls (no workgroup separation). Chose 100 GB as a sensible default for analysts; for agent-only workgroups, drop to 20 GB.
8. **Prepared statements require ExecutionParameters in ORDER.** If the PS has two `?` placeholders, the array has two strings. Extracting parameters from the NL question with Haiku is dependent on the question phrasing — test it with sample questions.
9. **Conversation history grows the prompt fast.** Cap at last 4 turns (8 messages) to stay under Claude's 200k-token ceiling at reasonable cost. Longer context should use Bedrock Prompt Caching (see `mlops/18_prompt_caching_patterns.md`).
10. **Long-running queries (> 60 s Lambda timeout)** need the poll-to-Step-Functions pattern. For this POC we cap at 60 s execute; upgrade to SFN-orchestrated for > 30-second queries as a hard requirement.

---

## 4. Micro-Stack Variant

**Use when:** text-to-SQL is one pattern among many agent tools (chat router, standalone NL UI, notebook env).

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** on the Lambda entry + prompt-library asset directory.
2. **Identity-side grants** — consumer stacks that invoke this Lambda (e.g. `ChatRouterStack`) grant themselves `lambda:InvokeFunction` on the SSM-read `TextToSqlFnArn`; never reach into this stack to attach a resource policy.
3. **`CfnRule` cross-stack EventBridge** — if long-running queries publish `QueryStateChange` EB events for downstream audit, the rule lives in this producer stack.
4. **Same-stack bucket + OAC** — N/A.
5. **KMS ARNs as strings** — consumers read `athena_cmk_arn` via SSM, grant `kms:Decrypt` on the string.

### 4.2 TextToSqlStack — the producer

```python
# stacks/text_to_sql_stack.py
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_athena as athena,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct


class TextToSqlStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) Resolve upstream contracts.
        idx_table_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_table_arn"
        )
        idx_column_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_column_arn"
        )
        result_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/result_bucket_name"
        )
        athena_cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/cmk_arn"
        )
        lake_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lake/bucket_name"
        )

        # B) Agent workgroup — 1 GB scan cutoff. (The human-analyst WG stays
        #    in AthenaStack; this one is agent-only, narrower.)
        agent_wg = athena.CfnWorkGroup(
            self, "AgentWorkgroup",
            name=f"lakehouse-agent-{stage}",
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=1024**3,
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3",
                ),
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{result_bucket_name}/agent/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=athena_cmk_arn,
                    ),
                    expected_bucket_owner=self.account,
                ),
            ),
        )

        # C) Per-caller budget table.
        budget_table = ddb.Table(
            self, "ScanBudgetTable",
            table_name=f"{{project_name}}-agent-scan-budget-{stage}",
            partition_key=ddb.Attribute(name="caller_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="date",         type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl_epoch",
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=(
                RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
            ),
        )

        # D) Lambda.
        t2s_fn = PythonFunction(
            self, "TextToSqlFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "text_to_sql"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(2),
            memory_size=1024,
            reserved_concurrent_executions=50,
            environment={
                "IDX_TABLE_ARN":          idx_table_arn,
                "IDX_COLUMN_ARN":         idx_column_arn,
                "EMBED_MODEL_ID":         "amazon.titan-embed-text-v2:0",
                "EMBED_DIM":              "1024",
                "GEN_MODEL_ID":           "us.anthropic.claude-sonnet-4-7-20260109-v1:0",
                "CLASSIFY_MODEL_ID":      "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "ATHENA_WORKGROUP":       agent_wg.ref,
                "BUDGET_TABLE":           budget_table.table_name,
                "DAILY_SCAN_BUDGET_GB":   "100",
                "MAX_RETRIES":            "3",
                "DEFAULT_DATABASE":       f"lakehouse_{stage}",
                "PROMPT_LIBRARY_VERSION": "v1",
            },
        )

        # E) Grants (same as §3.2 — elided here for brevity; identical
        #    policies against SSM-resolved ARNs).
        for stmt in self._grants(
            idx_table_arn=idx_table_arn,
            idx_column_arn=idx_column_arn,
            agent_wg_name=agent_wg.ref,
            result_bucket_name=result_bucket_name,
            lake_bucket_name=lake_bucket_name,
            athena_cmk_arn=athena_cmk_arn,
        ):
            t2s_fn.add_to_role_policy(stmt)
        budget_table.grant_read_write_data(t2s_fn)

        # F) Publish the consumer contract.
        ssm.StringParameter(
            self, "T2sFnArnParam",
            parameter_name=f"/{{project_name}}/{stage}/text_to_sql/fn_arn",
            string_value=t2s_fn.function_arn,
        )
        ssm.StringParameter(
            self, "AgentWorkgroupNameParam",
            parameter_name=f"/{{project_name}}/{stage}/text_to_sql/workgroup_name",
            string_value=agent_wg.ref,
        )

        CfnOutput(self, "TextToSqlFnArn", value=t2s_fn.function_arn)
        CfnOutput(self, "AgentWorkgroup", value=agent_wg.ref)

    def _grants(self, *, idx_table_arn, idx_column_arn, agent_wg_name,
                result_bucket_name, lake_bucket_name, athena_cmk_arn):
        yield iam.PolicyStatement(
            actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
            resources=[idx_table_arn, idx_column_arn],
        )
        yield iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                f"arn:aws:bedrock:{self.region}::"
                f"foundation-model/amazon.titan-embed-text-v2:0",
                f"arn:aws:bedrock:{self.region}:*:"
                f"inference-profile/us.anthropic.claude-sonnet-4-7-20260109-v1:0",
                f"arn:aws:bedrock:{self.region}:*:"
                f"inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
            ],
        )
        yield iam.PolicyStatement(
            actions=["athena:StartQueryExecution", "athena:GetQueryExecution",
                     "athena:GetQueryResults", "athena:StopQueryExecution",
                     "athena:GetPreparedStatement", "athena:ListPreparedStatements"],
            resources=[
                f"arn:aws:athena:{self.region}:{self.account}:workgroup/{agent_wg_name}",
            ],
        )
        yield iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetDatabases",
                     "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
                     "lakeformation:GetDataAccess"],
            resources=["*"],
        )
        yield iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket",
                     "s3:GetBucketLocation"],
            resources=[
                f"arn:aws:s3:::{result_bucket_name}",
                f"arn:aws:s3:::{result_bucket_name}/*",
                f"arn:aws:s3:::{lake_bucket_name}",
                f"arn:aws:s3:::{lake_bucket_name}/*",
            ],
        )
        yield iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[athena_cmk_arn],
        )
```

### 4.3 Consumer pattern — chat router invokes this Lambda

```python
# stacks/chat_router_stack.py (subset)
from aws_cdk import Stack, aws_iam as iam, aws_lambda as _lambda, aws_ssm as ssm


class ChatRouterStack(Stack):
    def __init__(self, scope, construct_id, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        t2s_fn_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/text_to_sql/fn_arn"
        )
        # Resolve to L2 reference for grant_invoke — safer than a raw
        # PolicyStatement in case CDK's contract changes.
        t2s_fn = _lambda.Function.from_function_arn(
            self, "ImportedT2sFn", t2s_fn_arn,
        )
        # The chat router Lambda (not shown) — grant invoke.
        t2s_fn.grant_invoke(self.chat_router_fn)
```

### 4.4 Micro-stack gotchas

- **Consumer grants are `lambda:InvokeFunction`, not a direct Athena grant**. The chat router does NOT query Athena directly — it calls the Text-to-SQL Lambda, which has its own role. This is the whole point of decoupling.
- **`Lambda.from_function_arn` does NOT import the security-group / VPC config.** If the text-to-SQL Lambda runs in a VPC (for a VPC-private Bedrock endpoint), consumers calling it from outside the VPC still work — the invocation API is outside the VPC. If both caller + callee share a VPC concern, skip the from_function_arn pattern and use IAM identity-side statements instead.
- **Deletion order**: TextToSqlStack → ConsumerStack (deploy); Consumer → TextToSqlStack (delete). A consumer still using the Lambda after deletion sees `ResourceNotFoundException`.
- **Budget table stays alive across stack deletes** (RemovalPolicy.RETAIN in prod). If the caller IDs remain stable across redeploys, the budget history persists — which you often want.

---

## 5. Swap matrix

| Concern | Default | Swap with | Why |
|---|---|---|---|
| SQL-gen model | Claude Sonnet 4.7 | Claude Opus 4.7 | Hardest queries (complex joins, window functions); better accuracy at 5× cost. Prefer for human-escalation path only. |
| SQL-gen model | Claude Sonnet 4.7 | Claude Haiku 4.5 | Trivial questions (single-table SELECT). Gate with a complexity classifier first; saves 60% on cost. |
| Fast path | Prepared-statement similarity > 0.85 | No fast path | Small catalogue, no curated PS library. Accept the full 4-phase cost. |
| Fast path | Prepared-statement | Named-query + parameter-extraction via regex | Lower accuracy but no embedding cost on short-turn questions. |
| Preflight | EXPLAIN (FORMAT JSON) | Dry-run via `CREATE OR REPLACE VIEW` + drop | Tests DDL path; 2× cost. Use for write-path rehearsals, not read-only agents. |
| Preflight | EXPLAIN + touched-table check | Regex on table names | Dumber but free. Misses CTE-hidden table references. |
| Budget tracking | DDB per-caller, per-day | Athena CloudWatch Sum metric + scheduled check | Simpler; but less granular (no per-user attribution). |
| Retry | 3 retries with error feedback | Single attempt + bail | Deterministic latency; much worse UX. |
| Guardrails | Lex veto + preflight + LF | Bedrock Guardrails content policy | Blocks toxic / sensitive questions pre-generation. Pair with lex veto for SQL-specific rules. |
| Prompt versioning | File-system `prompts/v1/` | Bedrock Prompt Management | Centralised A/B, rollback, audit. See `mlops/24_bedrock_prompt_management.md`. |
| Conversation memory | In-request `conversation[]` | AgentCore Memory | Multi-session / long-term memory with per-user isolation. See `kits/deep-research-agent`. |
| Long-running queries | Poll in Lambda, 60 s cap | Step Functions waitForTaskToken | Queries > 30 s need SFN — Lambda timeout at 15 min is a hard ceiling. |

---

## 6. Worked example — offline synth + end-to-end round trip

```python
# tests/test_text_to_sql_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.text_to_sql_stack import TextToSqlStack


def test_synth_has_wg_fn_ddb_grants():
    app = cdk.App()
    stack = TextToSqlStack(app, "T2S-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # Agent workgroup at 1 GB cutoff, v3.
    tpl.has_resource_properties("AWS::Athena::WorkGroup", {
        "Name": "lakehouse-agent-dev",
        "WorkGroupConfiguration": Match.object_like({
            "BytesScannedCutoffPerQuery": 1024**3,
            "EnforceWorkGroupConfiguration": True,
            "EngineVersion": Match.object_like({
                "SelectedEngineVersion": "Athena engine version 3",
            }),
        }),
    })

    # Lambda env.
    tpl.has_resource_properties("AWS::Lambda::Function", {
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "GEN_MODEL_ID":         "us.anthropic.claude-sonnet-4-7-20260109-v1:0",
                "MAX_RETRIES":          "3",
                "DAILY_SCAN_BUDGET_GB": "100",
            }),
        }),
        "Timeout":     120,
        "MemorySize":  1024,
    })

    # DDB budget table with TTL.
    tpl.has_resource_properties("AWS::DynamoDB::Table", {
        "TableName": "{project_name}-agent-scan-budget-dev",
        "KeySchema": Match.array_with([
            Match.object_like({"AttributeName": "caller_id", "KeyType": "HASH"}),
            Match.object_like({"AttributeName": "date",      "KeyType": "RANGE"}),
        ]),
        "TimeToLiveSpecification": Match.object_like({
            "AttributeName": "ttl_epoch",
            "Enabled": True,
        }),
    })


# tests/test_preflight_logic.py
"""Unit-test the preflight guard in isolation."""
import pytest
from text_to_sql.handler import _lex_veto, PreflightError


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "DROP TABLE dim_customer",
    "UPDATE fact_revenue SET amount = 0",
    "CREATE TABLE x AS SELECT 1",
    "  MERGE INTO fact_revenue USING src ON 1=1",
])
def test_lex_veto_kills_dml_ddl(sql):
    with pytest.raises(PreflightError) as e:
        _lex_veto(sql)
    assert e.value.reason in ("forbidden-keyword", "not-a-select")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM dim_customer",
    "WITH q AS (SELECT 1) SELECT * FROM q",
    "  SELECT amount FROM fact_revenue WHERE ts > current_date - INTERVAL '3' DAY",
])
def test_lex_veto_accepts_select(sql):
    _lex_veto(sql)   # should not raise


# tests/test_end_to_end.py
"""Integration — against a populated catalog + seeded fact_revenue."""
import boto3, os, json, pytest


@pytest.mark.integration
def test_revenue_question_end_to_end():
    lam = boto3.client("lambda")
    resp = lam.invoke(
        FunctionName=os.environ["T2S_FN_ARN"],
        Payload=json.dumps({
            "question":        "total revenue by customer in the last 30 days",
            "caller_id":       "test:alice",
            "caller_domain":   "finance",
            "max_sensitivity": "internal",
        }).encode(),
    )
    body = json.loads(resp["Payload"].read())
    assert body["ok"], body.get("reason", "no-reason")
    # SQL references fact_revenue + has a time filter.
    assert "fact_revenue" in body["sql"].lower()
    assert "current_date" in body["sql"].lower() or "interval" in body["sql"].lower()
    # Lineage populated.
    assert "fact_revenue" in body["lineage"]["tables"]
```

---

## 7. References

- AWS docs — *Athena engine v3 release notes* (Iceberg DML, prepared statements, EXPLAIN plan JSON format).
- AWS docs — *Bedrock Claude Sonnet 4.7 inference profile ARNs*.
- AWS docs — *Bedrock prompt management* for centralised prompt versioning.
- `PATTERN_CATALOG_EMBEDDINGS.md` — the grounding index; 3-pass discovery flow.
- `DATA_ATHENA.md` — workgroup configuration, `EXPLAIN` pattern, `invoke_model` SQL integration.
- `DATA_LAKE_FORMATION.md` — runtime row/column enforcement; defence-in-depth layer.
- `DATA_GLUE_CATALOG.md` — column comments are the prompt substrate.
- `PATTERN_ENTERPRISE_CHAT_ROUTER.md` (next) — primary consumer of this pattern.
- `mlops/18_prompt_caching_patterns.md` — Bedrock prompt caching for long system prompts.
- `mlops/24_bedrock_prompt_management.md` — centralised prompt versioning.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Four-phase pipeline (discover → generate → preflight → execute) with retry loop. Three safety gates (table allowlist + EXPLAIN + LF). Prepared-statement fast path. Per-caller DDB budget. Lex veto + touched-table AST walk. Claude Sonnet 4.7 + Haiku 4.5 model pairing. 10 monolith gotchas, 4 micro-stack gotchas, 12-row swap matrix, pytest unit + integration harness.
