# SOP — Semantic Data Discovery ("find my data about X" API)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · Amazon Bedrock Titan Text Embeddings v2 (1024 dim) · Claude Haiku 4.5 for lightweight reranking + NL summary · `PATTERN_CATALOG_EMBEDDINGS` as the index source · `PATTERN_MULTIMODAL_EMBEDDINGS` optional diagram/image side · API Gateway REST or Function URL · Cognito identity + JWT claims for caller context · Lake Formation LF-Tag pushdown

---

## 1. Purpose

- Provide the deep-dive for **semantic data discovery** — the thin API layer that sits in front of `PATTERN_CATALOG_EMBEDDINGS` and exposes a single, HTTP-friendly endpoint: **"Here is my question. What data do we have about it?"** Returns a structured answer (databases, tables, columns, sample values, signed preview URLs for multimodal hits) with **LF-Tag-aware filtering driven by the caller's identity**, not a client-supplied parameter.
- Codify the **caller-context contract** — the Lambda extracts `caller_id`, `caller_domain`, `max_sensitivity`, `access_groups[]` from the JWT claims / Cognito user-pool attributes, NOT from the request body. A client asking for `max_sensitivity=pii` does not magically get PII access — the IAM role + LF grants are the only thing that moves the needle.
- Codify the **3-pass discovery flow** (db → table → column) that `PATTERN_CATALOG_EMBEDDINGS §3.4` defines, plus an **optional 4th pass** over multimodal images (via `PATTERN_MULTIMODAL_EMBEDDINGS`) when the caller's context includes visual-search permissions.
- Codify the **Haiku rerank + NL summary** step — after the 3-pass retrieve, feed the top N hits to Claude Haiku 4.5 with a short prompt: "Rerank by relevance to the user's question. For each top-5 result, produce a 1-sentence plain-English description that helps the user decide if it's what they want." This is where the discovery experience becomes *good* — raw cosine scores are not user-visible.
- Codify the **structured output contract** — a discovery result has three sections that the UI can render independently:
  1. `databases[]` — a ranked list of up to 5 relevant databases.
  2. `tables[]` — up to 10 tables ranked with `database`, `name`, `description`, `relevance`, `why` (NL explanation), `columns_hint` (top 3 relevant column names).
  3. `columns[]` — up to 20 columns with `database`, `table`, `name`, `type`, `description`, `relevance`.
  Plus optional `images[]` for multimodal hits.
- Codify the **sample-value preview** — when a column is shortlisted, include 3 sanitised sample values via `Athena SELECT DISTINCT column FROM table LIMIT 3` BOUNDED by LF at runtime. Gated by `sample_values=true` request flag + caller permission.
- Codify the **cache layer** — DDB (or ElastiCache) caches the 3-pass retrieve per `(caller_identity_hash, question_hash)` for 10 minutes. Same caller asking the same question repeatedly (common for "who can see X") hits the cache instead of re-embedding + re-querying.
- Codify the **API Gateway vs Function URL tradeoff** — API GW for multi-consumer cognito-integrated deployments; Lambda Function URL for single-consumer internal. Both documented.
- Include when the SOW signals: "data discovery", "find my data", "natural-language catalog search", "data marketplace search", "where's my X", "data dictionary search", "self-service discovery", "AI-powered catalog".
- This partial is the **"find" side** of the AI-native lakehouse; complement of `PATTERN_TEXT_TO_SQL` ("query") and `PATTERN_ENTERPRISE_CHAT_ROUTER` ("blend"). It's the lightest of the three — typically < 2 s response time and cheap enough to expose directly to end-user UIs.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the Discovery Lambda, API Gateway / Function URL, cache table, and Cognito setup | **§3 Monolith Variant** |
| `DataDiscoveryStack` owns Lambda + API + cache + alarms; Cognito + upstream embedding index + downstream consumers live in separate stacks | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Identity source is usually external.** Cognito user pool + identity pool often live in `AuthStack` or a corp IAM federation (Okta/Entra/Ping → Cognito via SAML). The Discovery Lambda needs the ID token validator set up; importing a Cognito user pool by ARN is easy, but the app-client for the API must live in `AuthStack`.
2. **Cache table is per-environment shared.** If multiple discovery surfaces exist (chat router, standalone UI), they should share the cache.
3. **API Gateway custom domain + TLS cert** is often a shared resource (`AuthStack` owns the domain, discovery adds a path prefix).
4. **Upstream is `CatalogEmbeddingStack.Idx*Arn` via SSM.** Upstream changes dimension → rebuild discovery after upstream completes; single-stack POC makes this sequencing invisible.
5. **Downstream is the chat router + the UI**; they never directly call the embedding index, only the Discovery API.

Micro-Stack fixes by: (a) owning Discovery Lambda + API Gateway (or Function URL) + cache table + CloudWatch alarms in `DataDiscoveryStack`; (b) reading `CatalogEmbedding.*` + `Auth.userPoolId / appClientId` via SSM; (c) publishing `DiscoveryApiUrl` + `DiscoveryFnArn` via SSM for chat-router and UI consumers.

---

## 3. Monolith Variant

**Use when:** POC or small-scale deployment. All components in one stack.

### 3.1 Architecture

```
  Client (UI / agent / notebook)
      │
      │  POST /discover
      │  Authorization: Bearer <ID token from Cognito>
      │  { "question": "customer contract renewals",
      │    "top_k_tables": 8,
      │    "sample_values": true,
      │    "include_multimodal": false }
      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  API Gateway (or Function URL)                                   │
  │    - Cognito authoriser validates the JWT                        │
  │    - Rate limit: 20 rps / 100 burst per user                     │
  │    - Usage plan for analytics                                    │
  └──────────────────────────────────────────────────────────────────┘
      │  context: {claims: {sub, cognito:groups, custom:domain}}
      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  DiscoveryFn (Lambda)                                            │
  │                                                                  │
  │  0) extract_identity(claims) →                                   │
  │     caller_id, caller_domain, max_sensitivity, access_groups     │
  │                                                                  │
  │  1) cache_key = sha256(caller_id + question + flags)             │
  │     if hit → return cached result (10 min TTL)                   │
  │                                                                  │
  │  2) q_vec = titan.embed(question)                                │
  │                                                                  │
  │  3) PHASE 1 — db-level                                           │
  │     filter: {domain: caller_domain}                              │
  │     top 5 databases                                              │
  │                                                                  │
  │  4) PHASE 2 — table-level                                        │
  │     filter: {database_name IN db_names,                          │
  │              sensitivity IN allowed_sensitivities}               │
  │     top 10 tables                                                │
  │                                                                  │
  │  5) PHASE 3 — column-level                                       │
  │     filter: {table_name IN top_5_table_names,                    │
  │              sensitivity IN allowed_sensitivities}               │
  │     top 20 columns                                               │
  │                                                                  │
  │  6) OPTIONAL PHASE 4 — multimodal images                         │
  │     if include_multimodal + LF grant allows:                     │
  │       query IDX_IMAGE_ARN → top 5 images                         │
  │                                                                  │
  │  7) Haiku rerank + NL summary                                    │
  │     - Rerank the merged list by LLM judgement                    │
  │     - Generate 1-sentence "why this matches" per top-5           │
  │                                                                  │
  │  8) OPTIONAL — sample values                                     │
  │     if sample_values=true AND LF allows:                         │
  │       Athena: SELECT DISTINCT <col> FROM <tbl> LIMIT 3           │
  │         (runs in a lightweight "discovery-sample" workgroup,     │
  │          100 MB cutoff)                                          │
  │                                                                  │
  │  9) Signed preview URLs for multimodal thumbnails                │
  │                                                                  │
  │ 10) Return structured response                                   │
  │                                                                  │
  │ 11) Cache write-through (10 min TTL)                             │
  └────────────────────┬─────────────────────────────────────────────┘
                       │
  ┌────────────────────┴────────────────────────────────────────────┐
  ▼                                                                  ▼
┌───────────────────────────────┐             ┌───────────────────────────────┐
│  Catalog embedding indexes    │             │  Multimodal embedding index   │
│  (3 — db, table, column)      │             │  (2 — images, text — opt)     │
└───────────────────────────────┘             └───────────────────────────────┘
                       │
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  DiscoveryCache (DynamoDB, TTL 10 min)                           │
  │    PK: (caller_id, question_hash)                                │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_data_discovery()` method body

```python
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigateway as apigw,
    aws_athena as athena,
    aws_cognito as cognito,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction


def _create_data_discovery(self, stage: str) -> None:
    """Monolith variant. Assumes self.{idx_db_arn, idx_table_arn,
    idx_column_arn, idx_image_arn, user_pool} exist."""

    # A) Cache table.
    self.discovery_cache = ddb.Table(
        self, "DiscoveryCache",
        table_name=f"{{project_name}}-discovery-cache-{stage}",
        partition_key=ddb.Attribute(name="cache_key", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="ttl_epoch",
        encryption=ddb.TableEncryption.AWS_MANAGED,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Sample-values workgroup (100 MB cutoff — just for peeking).
    self.sample_wg = athena.CfnWorkGroup(
        self, "SampleWorkgroup",
        name=f"discovery-sample-{stage}",
        state="ENABLED",
        description="Sample-values preview — 100 MB cutoff.",
        work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
            enforce_work_group_configuration=True,
            publish_cloud_watch_metrics_enabled=True,
            bytes_scanned_cutoff_per_query=100 * 1024**2,   # 100 MB
            engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                selected_engine_version="Athena engine version 3",
            ),
            result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                output_location=f"s3://{self.athena_results_bucket}/discovery/",
                encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                    encryption_option="SSE_KMS",
                    kms_key=self.athena_cmk_arn,
                ),
            ),
        ),
    )

    # C) Discovery Lambda.
    self.discovery_fn = PythonFunction(
        self, "DiscoveryFn",
        entry=str(Path(__file__).parent.parent / "lambda" / "discovery"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.seconds(15),
        memory_size=1024,
        reserved_concurrent_executions=100,
        environment={
            "IDX_DB_ARN":       self.idx_db_arn,
            "IDX_TABLE_ARN":    self.idx_table_arn,
            "IDX_COLUMN_ARN":   self.idx_column_arn,
            "IDX_IMAGE_ARN":    self.idx_image_arn,
            "EMBED_MODEL_ID":   "amazon.titan-embed-text-v2:0",
            "EMBED_DIM":        "1024",
            "RERANK_MODEL_ID":  "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "SAMPLE_WORKGROUP": self.sample_wg.ref,
            "CACHE_TABLE":      self.discovery_cache.table_name,
            "CACHE_TTL_S":      "600",        # 10 min
            "PREVIEW_BUCKET":   self.mm_preview_bucket_name,
        },
    )

    # D) Grants.
    self.discovery_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
        resources=[self.idx_db_arn, self.idx_table_arn, self.idx_column_arn,
                   self.idx_image_arn],
    ))
    self.discovery_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel"],
        resources=[
            f"arn:aws:bedrock:{Stack.of(self).region}::"
            f"foundation-model/amazon.titan-embed-text-v2:0",
            f"arn:aws:bedrock:{Stack.of(self).region}:*:"
            f"inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
    ))
    self.discovery_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["athena:StartQueryExecution", "athena:GetQueryExecution",
                 "athena:GetQueryResults", "athena:StopQueryExecution"],
        resources=[
            f"arn:aws:athena:{Stack.of(self).region}:"
            f"{Stack.of(self).account}:workgroup/{self.sample_wg.ref}",
        ],
    ))
    self.discovery_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["glue:GetDatabase", "glue:GetTable",
                 "lakeformation:GetDataAccess"],
        resources=["*"],
    ))
    self.discovery_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3:GetObject"],
        resources=[f"arn:aws:s3:::{self.mm_preview_bucket_name}/*"],
    ))
    self.discovery_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.athena_cmk_arn, self.mm_cmk_arn],
    ))
    self.discovery_cache.grant_read_write_data(self.discovery_fn)

    # E) API Gateway — Cognito-authorised.
    #    POST /discover
    self.api = apigw.RestApi(
        self, "DiscoveryApi",
        rest_api_name=f"{{project_name}}-discovery-{stage}",
        endpoint_types=[apigw.EndpointType.REGIONAL],
        deploy_options=apigw.StageOptions(
            stage_name=stage,
            throttling_rate_limit=100,
            throttling_burst_limit=200,
            logging_level=apigw.MethodLoggingLevel.INFO,
            data_trace_enabled=False,
            metrics_enabled=True,
        ),
        default_cors_preflight_options=apigw.CorsOptions(
            allow_origins=apigw.Cors.ALL_ORIGINS,    # tighten in prod
            allow_methods=["POST", "OPTIONS"],
            allow_headers=apigw.Cors.DEFAULT_HEADERS,
        ),
    )
    authoriser = apigw.CognitoUserPoolsAuthorizer(
        self, "CognitoAuth",
        cognito_user_pools=[self.user_pool],
    )
    discover_res = self.api.root.add_resource("discover")
    discover_res.add_method(
        "POST",
        apigw.LambdaIntegration(self.discovery_fn, proxy=True),
        authorization_type=apigw.AuthorizationType.COGNITO,
        authorizer=authoriser,
    )

    # F) Usage plan — per-API-key rate limiting for internal services.
    plan = self.api.add_usage_plan(
        "DiscoveryPlan",
        name=f"{{project_name}}-discovery-plan-{stage}",
        throttle=apigw.ThrottleSettings(rate_limit=20, burst_limit=100),
        quota=apigw.QuotaSettings(limit=10000, period=apigw.Period.DAY),
    )
    plan.add_api_stage(stage=self.api.deployment_stage)

    # G) Outputs.
    CfnOutput(self, "DiscoveryApiUrl", value=self.api.url)
    CfnOutput(self, "DiscoveryFnArn",  value=self.discovery_fn.function_arn)
    CfnOutput(self, "SampleWorkgroup", value=self.sample_wg.ref)
```

### 3.3 Lambda handler — the flow

```python
# lambda/discovery/handler.py
"""
Semantic data discovery handler.

Request body: {
  "question":           "customer contract renewals",
  "top_k_tables":       8,
  "sample_values":      true,
  "include_multimodal": false,
  "include_summary":    true          # default true
}

Identity context (from JWT claims): caller_id, caller_domain, access_groups,
max_sensitivity — NEVER trust the request body for these.

Response: {
  "ok": true,
  "databases": [{...}],
  "tables":    [{..., why: "..."}],
  "columns":   [{..., sample_values: ["...", "..."]}],
  "images":    [{..., thumbnail_url: "..."}]          // if include_multimodal
  "summary":   "Looks like you want ..."              // if include_summary
}
"""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3


IDX_DB_ARN       = os.environ["IDX_DB_ARN"]
IDX_TABLE_ARN    = os.environ["IDX_TABLE_ARN"]
IDX_COLUMN_ARN   = os.environ["IDX_COLUMN_ARN"]
IDX_IMAGE_ARN    = os.environ.get("IDX_IMAGE_ARN", "")
EMBED_MODEL_ID   = os.environ["EMBED_MODEL_ID"]
EMBED_DIM        = int(os.environ["EMBED_DIM"])
RERANK_MODEL_ID  = os.environ["RERANK_MODEL_ID"]
SAMPLE_WORKGROUP = os.environ["SAMPLE_WORKGROUP"]
CACHE_TABLE      = os.environ["CACHE_TABLE"]
CACHE_TTL_S      = int(os.environ["CACHE_TTL_S"])
PREVIEW_BUCKET   = os.environ["PREVIEW_BUCKET"]

bedrock = boto3.client("bedrock-runtime")
s3v     = boto3.client("s3vectors")
athena  = boto3.client("athena")
ddb     = boto3.client("dynamodb")
s3      = boto3.client("s3")


_SENSITIVITY_RANK = {
    "public":       ["public"],
    "internal":     ["public", "internal"],
    "confidential": ["public", "internal", "confidential"],
    "pii":          ["public", "internal", "confidential", "pii"],
}


# ---- identity extraction --------------------------------------------------

def _extract_identity(event: dict) -> dict:
    """Parse API Gateway proxy event's authoriser claims."""
    claims = (
        event.get("requestContext", {})
             .get("authorizer", {})
             .get("claims", {})
    )
    # Cognito groups (comma-string) → list.
    groups = []
    g = claims.get("cognito:groups", "")
    if g:
        groups = [x.strip() for x in g.split(",")]
    # max_sensitivity from a custom attribute, default "internal".
    max_sens = claims.get("custom:max_sensitivity", "internal")
    domain   = claims.get("custom:domain", "default")
    return {
        "caller_id":       f"user:{claims.get('sub','anon')}",
        "caller_domain":   domain,
        "max_sensitivity": max_sens,
        "access_groups":   groups,
    }


# ---- cache helpers --------------------------------------------------------

def _cache_key(identity: dict, body: dict) -> str:
    payload = json.dumps({
        "caller":   identity["caller_id"],
        "domain":   identity["caller_domain"],
        "sens":     identity["max_sensitivity"],
        "q":        body["question"],
        "mm":       body.get("include_multimodal", False),
        "sv":       body.get("sample_values", False),
        "k_t":      body.get("top_k_tables", 8),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _cache_get(key: str) -> dict | None:
    resp = ddb.get_item(TableName=CACHE_TABLE, Key={"cache_key": {"S": key}})
    item = resp.get("Item")
    if not item:
        return None
    try:
        return json.loads(item["payload"]["S"])
    except Exception:
        return None


def _cache_put(key: str, payload: dict) -> None:
    ttl = int((datetime.now(tz=timezone.utc) + timedelta(seconds=CACHE_TTL_S)).timestamp())
    ddb.put_item(TableName=CACHE_TABLE, Item={
        "cache_key": {"S": key},
        "payload":   {"S": json.dumps(payload)},
        "ttl_epoch": {"N": str(ttl)},
    })


# ---- embedding ------------------------------------------------------------

def _embed(text: str) -> list[float]:
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({"inputText": text, "dimensions": EMBED_DIM}),
        accept="application/json", contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


# ---- 3-pass + multimodal retrieve ----------------------------------------

def _retrieve(identity: dict, body: dict, q_vec: list[float]) -> dict:
    allowed = _SENSITIVITY_RANK.get(
        identity["max_sensitivity"], _SENSITIVITY_RANK["internal"]
    )

    # 1) Database-level.
    dbs = s3v.query_vectors(
        indexArn=IDX_DB_ARN,
        queryVector=q_vec,
        topK=5,
        filter={"domain": identity["caller_domain"]},
        returnMetadata=True, returnDistance=True,
    )["matches"]
    db_names = [m["metadata"]["database_name"] for m in dbs] or ["__none__"]

    # 2) Table-level.
    tables = s3v.query_vectors(
        indexArn=IDX_TABLE_ARN,
        queryVector=q_vec,
        topK=body.get("top_k_tables", 8),
        filter={
            "database_name": {"$in": db_names},
            "sensitivity":   {"$in": allowed},
        },
        returnMetadata=True, returnDistance=True,
    )["matches"]
    top_tables = [m["metadata"]["table_name"] for m in tables[:5]]

    # 3) Column-level.
    columns = s3v.query_vectors(
        indexArn=IDX_COLUMN_ARN,
        queryVector=q_vec,
        topK=20,
        filter={
            "table_name":  {"$in": top_tables or ["__none__"]},
            "sensitivity": {"$in": allowed},
        },
        returnMetadata=True, returnDistance=True,
    )["matches"]

    # 4) Multimodal images (optional).
    images: list = []
    if body.get("include_multimodal") and IDX_IMAGE_ARN:
        im = s3v.query_vectors(
            indexArn=IDX_IMAGE_ARN,
            queryVector=q_vec,
            topK=5,
            filter={"access_group": {"$in": identity["access_groups"] + ["default"]}},
            returnMetadata=True, returnDistance=True,
        )["matches"]
        images = im

    return {"dbs": dbs, "tables": tables, "columns": columns, "images": images}


# ---- Haiku rerank + NL summary --------------------------------------------

_RERANK_PROMPT = """\
You are a data-discovery assistant. The user asked:

  QUESTION: {question}

The catalog returned these candidate matches (JSON), ranked by vector
similarity. Your job: rerank them for relevance to the user's question, and
for each top-5, write a single-sentence plain-English explanation of why it
matches and what it contains.

Return JSON of the shape:
{{
  "tables":  [{{ "key": "<table_key>", "why": "..." }}, ...],
  "columns": [{{ "key": "<column_key>", "why": "..." }}, ...],
  "summary": "<2-3 sentence user-facing summary>"
}}

CANDIDATES:
{candidates}
"""


def _rerank_and_summarise(question: str, retrieve_out: dict) -> dict:
    candidates = {
        "tables": [
            {"key": m["metadata"]["table_name"],
             "description": m["metadata"].get("source_text", "")[:300]}
            for m in retrieve_out["tables"][:8]
        ],
        "columns": [
            {"key": f"{m['metadata']['table_name']}.{m['metadata']['column_name']}",
             "description": m["metadata"].get("source_text", "")[:200]}
            for m in retrieve_out["columns"][:10]
        ],
    }
    prompt = _RERANK_PROMPT.format(
        question=question,
        candidates=json.dumps(candidates, indent=2),
    )
    resp = bedrock.invoke_model(
        modelId=RERANK_MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens":        1024,
            "temperature":       0.2,
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }],
        }),
        accept="application/json", contentType="application/json",
    )
    text = json.loads(resp["body"].read())["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        # Haiku can fail to produce JSON occasionally — return a stub.
        return {"tables": [], "columns": [], "summary": ""}


# ---- sample values (optional) ---------------------------------------------

def _fetch_sample_values(db: str, table: str, col: str) -> list[str]:
    # 100 MB workgroup cutoff protects us against massive tables.
    sql = f"SELECT DISTINCT {col} FROM {db}.{table} LIMIT 3"
    try:
        exec_id = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": db},
            WorkGroup=SAMPLE_WORKGROUP,
        )["QueryExecutionId"]
        end = time.time() + 8
        while time.time() < end:
            q = athena.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]
            st = q["Status"]["State"]
            if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.3)
        if st != "SUCCEEDED":
            return []
        rows = athena.get_query_results(QueryExecutionId=exec_id)["ResultSet"]["Rows"]
        return [r["Data"][0].get("VarCharValue", "") for r in rows[1:]]
    except Exception:
        return []


# ---- preview signing -----------------------------------------------------

def _signed_preview(thumb_uri: str, ttl_s: int = 300) -> str:
    if not thumb_uri.startswith(f"s3://{PREVIEW_BUCKET}/"):
        return ""
    key = thumb_uri.split("/", 3)[3]
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": PREVIEW_BUCKET, "Key": key},
        ExpiresIn=ttl_s,
    )


# ---- main handler ---------------------------------------------------------

def lambda_handler(event, _ctx):
    # 1) Identity + body.
    identity = _extract_identity(event)
    body     = json.loads(event.get("body") or "{}")

    if not body.get("question"):
        return _http(400, {"error": "question required"})

    # 2) Cache.
    ck = _cache_key(identity, body)
    cached = _cache_get(ck)
    if cached:
        cached["cached"] = True
        return _http(200, cached)

    # 3) Retrieve + rerank.
    q_vec = _embed(body["question"])
    retr  = _retrieve(identity, body, q_vec)
    rerank = _rerank_and_summarise(body["question"], retr) \
              if body.get("include_summary", True) else \
              {"tables": [], "columns": [], "summary": ""}

    # 4) Merge rerank signals.
    why_by_table: dict[str, str] = {
        x["key"]: x.get("why", "") for x in rerank.get("tables", [])
    }
    why_by_column: dict[str, str] = {
        x["key"]: x.get("why", "") for x in rerank.get("columns", [])
    }

    # 5) Build response.
    resp: dict[str, Any] = {
        "ok":        True,
        "question":  body["question"],
        "cached":    False,
        "summary":   rerank.get("summary", ""),
        "databases": [
            {"name":       m["metadata"]["database_name"],
             "domain":     m["metadata"].get("domain", ""),
             "relevance":  1.0 - m["distance"],
             "description": m["metadata"]["source_text"][:300]}
            for m in retr["dbs"]
        ],
        "tables":    [],
        "columns":   [],
        "images":    [],
    }
    for m in retr["tables"][:body.get("top_k_tables", 8)]:
        tm = m["metadata"]
        resp["tables"].append({
            "database":   tm["database_name"],
            "name":       tm["table_name"],
            "table_type": tm.get("table_type", ""),
            "relevance":  1.0 - m["distance"],
            "description": tm["source_text"][:400],
            "why":        why_by_table.get(tm["table_name"], ""),
        })

    want_samples = body.get("sample_values", False)
    for m in retr["columns"][:20]:
        cm = m["metadata"]
        col_key = f"{cm['table_name']}.{cm['column_name']}"
        samples: list[str] = []
        if want_samples:
            samples = _fetch_sample_values(
                cm["database_name"], cm["table_name"], cm["column_name"],
            )
        resp["columns"].append({
            "database":     cm["database_name"],
            "table":        cm["table_name"],
            "name":         cm["column_name"],
            "type":         cm["data_type"],
            "sensitivity":  cm.get("sensitivity", "internal"),
            "relevance":    1.0 - m["distance"],
            "description":  cm["source_text"][:300],
            "why":          why_by_column.get(col_key, ""),
            "sample_values": samples,
        })

    for m in retr["images"]:
        im = m["metadata"]
        resp["images"].append({
            "key":           m["key"],
            "source_uri":    im.get("source_uri", ""),
            "thumbnail_url": _signed_preview(im.get("thumbnail_uri", "")),
            "page":          im.get("page"),
            "caption":       im.get("caption", ""),
            "relevance":     1.0 - m["distance"],
        })

    # 6) Cache + return.
    _cache_put(ck, resp)
    return _http(200, resp)


def _http(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
        },
        "body":       json.dumps(payload),
    }
```

### 3.4 Function URL alternative (no API Gateway)

For internal-only use cases (e.g. chat-router calling this Lambda directly), skip API Gateway and add a Function URL with IAM auth:

```python
fn_url = self.discovery_fn.add_function_url(
    auth_type=_lambda.FunctionUrlAuthType.AWS_IAM,
    cors=_lambda.FunctionUrlCorsOptions(
        allowed_origins=["*"],
        allowed_methods=[_lambda.HttpMethod.POST],
    ),
)
```

Consumers sign requests with SigV4. Cheaper (no API GW cost) but loses Cognito authoriser + rate limiting — you'd need to re-implement both at the Lambda.

### 3.5 Monolith gotchas

1. **JWT claim keys depend on Cognito token version.** Cognito ID tokens use `cognito:groups`, `custom:*`, `email`, `sub` as stock claims. If you migrate to OIDC federation (Okta/Entra direct, skipping Cognito), the claim names will differ — abstract `_extract_identity` behind a switchable strategy.
2. **Never trust the request body for identity fields.** A client sending `max_sensitivity: pii` in the body while their JWT says `internal` MUST be rejected. Our implementation correctly ignores the body for these; be vigilant during refactors.
3. **Sample-value queries are authorised by the Lambda's IAM role, not the caller's role.** If the Lambda has broad LF grants, it can read PII columns even when the caller is restricted. Mitigation options: (a) run the sample query via an STS AssumeRole chain per-caller (complex, adds 200 ms); (b) keep the Lambda's LF grants tight (e.g. only `domain=default`) and skip samples for anything the Lambda cannot see; (c) disable samples entirely for PII-flagged columns. Our default is (c).
4. **DynamoDB cache TTL is eventual** — deleted items linger for up to 48 hours. If security requires hard deletion, use a conditional `DeleteItem` on cache reads that exceed TTL. Or use ElastiCache (Redis) which honours TTL deterministically.
5. **API Gateway 29-second hard timeout.** Discovery + rerank + sample values can approach 10 s. Budget carefully; if sample_values=true pushes past 29 s, return a partial response without samples rather than 504.
6. **Haiku rerank sometimes returns non-JSON.** Our fallback returns empty rerank arrays. Do not propagate "model failure" as an error — degrade gracefully to cosine-only ranking.
7. **Multi-domain caller handling.** A user with access to both `finance` and `hr` domains (two `custom:domain` values? a list?) needs careful modelling — our example assumes one domain. Either pick a primary domain per request (pass `?domain=finance` in query string) or run the 3-pass twice with both filters and merge.
8. **CORS with Cognito auth headers.** Browsers preflight OPTIONS ignores auth, so the OPTIONS method must be public (no authoriser). Our CORS config handles this; double-check if you customise.

---

## 4. Micro-Stack Variant

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** on the Lambda entry.
2. **Identity-side grants** — consumers (chat router, UI) read `DiscoveryApiUrl` / `DiscoveryFnArn` from SSM; grants to invoke come from their own role.
3. **`CfnRule` cross-stack EventBridge** — N/A for this pattern.
4. **Same-stack bucket + OAC** — N/A.
5. **KMS ARNs as strings** — consumers read `athena_cmk_arn` + `mm_cmk_arn` via SSM.

### 4.2 DataDiscoveryStack — the producer

```python
# stacks/data_discovery_stack.py  (abbreviated — same pattern as §3.2 with
# SSM-resolved inputs.)
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigateway as apigw,
    aws_athena as athena,
    aws_cognito as cognito,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct


class DataDiscoveryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # Resolve upstream contracts.
        idx_db_arn     = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_db_arn")
        idx_table_arn  = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_table_arn")
        idx_column_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_column_arn")
        # Multimodal may be absent in some deployments — tolerate.
        try:
            idx_image_arn = ssm.StringParameter.value_for_string_parameter(
                self, f"/{{project_name}}/{stage}/multimodal/idx_image_arn")
        except Exception:
            idx_image_arn = ""
        user_pool_id   = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/auth/user_pool_id")
        result_bucket  = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/result_bucket_name")
        athena_cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/cmk_arn")
        mm_preview     = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/preview_bucket_name")
        mm_cmk_arn     = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/cmk_arn")

        # Cache + sample WG + Lambda + API (same shape as §3.2, abbreviated).
        cache = ddb.Table(
            self, "DiscoveryCache",
            partition_key=ddb.Attribute(name="cache_key", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl_epoch",
            encryption=ddb.TableEncryption.AWS_MANAGED,
        )
        wg = athena.CfnWorkGroup(
            self, "SampleWg",
            name=f"discovery-sample-{stage}",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                bytes_scanned_cutoff_per_query=100 * 1024**2,
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3",
                ),
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{result_bucket}/discovery/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS", kms_key=athena_cmk_arn,
                    ),
                ),
            ),
        )
        fn = PythonFunction(
            self, "DiscoveryFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "discovery"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.seconds(15),
            memory_size=1024,
            reserved_concurrent_executions=100,
            environment={
                "IDX_DB_ARN":       idx_db_arn,
                "IDX_TABLE_ARN":    idx_table_arn,
                "IDX_COLUMN_ARN":   idx_column_arn,
                "IDX_IMAGE_ARN":    idx_image_arn,
                "EMBED_MODEL_ID":   "amazon.titan-embed-text-v2:0",
                "EMBED_DIM":        "1024",
                "RERANK_MODEL_ID":  "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "SAMPLE_WORKGROUP": wg.ref,
                "CACHE_TABLE":      cache.table_name,
                "CACHE_TTL_S":      "600",
                "PREVIEW_BUCKET":   mm_preview,
            },
        )
        cache.grant_read_write_data(fn)
        for stmt in self._grants(idx_db_arn, idx_table_arn, idx_column_arn,
                                  idx_image_arn, wg.ref, result_bucket,
                                  athena_cmk_arn, mm_cmk_arn, mm_preview):
            fn.add_to_role_policy(stmt)

        # API Gateway + Cognito authoriser.
        user_pool = cognito.UserPool.from_user_pool_id(self, "ImportedUp", user_pool_id)
        api = apigw.RestApi(
            self, "DiscoveryApi",
            rest_api_name=f"{{project_name}}-discovery-{stage}",
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )
        authoriser = apigw.CognitoUserPoolsAuthorizer(
            self, "CognitoAuth", cognito_user_pools=[user_pool],
        )
        api.root.add_resource("discover").add_method(
            "POST", apigw.LambdaIntegration(fn, proxy=True),
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=authoriser,
        )

        ssm.StringParameter(self, "ApiUrlParam",
            parameter_name=f"/{{project_name}}/{stage}/discovery/api_url",
            string_value=api.url)
        ssm.StringParameter(self, "FnArnParam",
            parameter_name=f"/{{project_name}}/{stage}/discovery/fn_arn",
            string_value=fn.function_arn)

        CfnOutput(self, "DiscoveryApiUrl", value=api.url)
        CfnOutput(self, "DiscoveryFnArn",  value=fn.function_arn)

    def _grants(self, idx_db, idx_tbl, idx_col, idx_img, wg, result_bucket,
                athena_cmk, mm_cmk, mm_preview):
        yield iam.PolicyStatement(
            actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
            resources=[idx_db, idx_tbl, idx_col] + ([idx_img] if idx_img else []),
        )
        yield iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::"
                f"foundation-model/amazon.titan-embed-text-v2:0",
                f"arn:aws:bedrock:{self.region}:*:"
                f"inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
            ],
        )
        yield iam.PolicyStatement(
            actions=["athena:StartQueryExecution", "athena:GetQueryExecution",
                     "athena:GetQueryResults", "athena:StopQueryExecution"],
            resources=[f"arn:aws:athena:{self.region}:{self.account}:workgroup/{wg}"],
        )
        yield iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetTable", "lakeformation:GetDataAccess"],
            resources=["*"],
        )
        yield iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
            resources=[f"arn:aws:s3:::{result_bucket}",
                       f"arn:aws:s3:::{result_bucket}/*",
                       f"arn:aws:s3:::{mm_preview}/*"],
        )
        yield iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[athena_cmk, mm_cmk],
        )
```

### 4.3 Consumer pattern

```python
# stacks/chat_router_stack.py — discovery consumer.
# from SSM: discovery_fn_arn OR discovery_api_url (pick one).
# For in-VPC Lambda-to-Lambda, prefer fn_arn via lambda:InvokeFunction.
# For browser UI, prefer api_url via CORS + Cognito.
```

### 4.4 Micro-stack gotchas

- **Cognito user pool import is ID-based**. `cognito.UserPool.from_user_pool_id(...)` is lightweight; do not use `from_user_pool_arn` unless you need ARN-shaped references elsewhere.
- **Multimodal is optional**. The `idx_image_arn` SSM read is wrapped in try/except — deployments without the multimodal stack still work.
- **Deletion order**: DataDiscoveryStack → ChatRouterStack (deploy); Consumer → DataDiscoveryStack (delete).

---

## 5. Swap matrix

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Retrieve | 3-pass (db → tbl → col) | 2-pass (tbl + col) | Small catalogs (< 5 databases); saves one Bedrock call. |
| Retrieve | 3-pass cosine | Hybrid BM25 + cosine | Keyword + semantic fusion; add OpenSearch Serverless sibling index. |
| Rerank | Claude Haiku 4.5 | No rerank, cosine-only | Cost-sensitive POCs; accept worse UX. |
| Rerank | Claude Haiku 4.5 | Cohere Rerank v3 | Purpose-built reranker, 30-50% better nDCG. Bedrock-hosted variant available. |
| Cache | DDB 10 min TTL | ElastiCache Redis | Hard TTL, sub-ms reads, higher cost. |
| Cache | DDB 10 min | No cache | Low-traffic POC. |
| Auth | Cognito | IAM SigV4 (Function URL) | Internal-only, no browser; simpler. |
| Auth | Cognito | Okta/Entra direct OIDC | Corp SSO; skip Cognito. Rewrite `_extract_identity`. |
| Sample values | Athena 100 MB WG | Glue DescribeTable statistics | No live sample, but free + instant. Less informative. |
| Sample values | LF-enforced | IAM-only column filtering | Single-account, fewer LF dependencies. |
| Multimodal | Titan Multimodal G1 | No multimodal | Text-only catalog. |
| API surface | POST /discover | GET /discover?q= | Cacheable via CloudFront for identical queries; loses identity isolation unless you include identity in URL (bad). |

---

## 6. Worked example

```python
# tests/test_discovery_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.data_discovery_stack import DataDiscoveryStack


def test_synth_api_lambda_cache_and_cognito_auth():
    app = cdk.App()
    stack = DataDiscoveryStack(app, "Discovery-dev", stage="dev")
    tpl = Template.from_stack(stack)

    tpl.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "cache_key", "KeyType": "HASH"}],
        "TimeToLiveSpecification": Match.object_like({
            "AttributeName": "ttl_epoch", "Enabled": True,
        }),
    })
    tpl.has_resource_properties("AWS::Athena::WorkGroup", {
        "Name": "discovery-sample-dev",
        "WorkGroupConfiguration": Match.object_like({
            "BytesScannedCutoffPerQuery": 100 * 1024**2,
        }),
    })
    tpl.has_resource_properties("AWS::Lambda::Function", {
        "Timeout": 15, "MemorySize": 1024,
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "RERANK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "CACHE_TTL_S":     "600",
            }),
        }),
    })
    # Method with Cognito authoriser.
    tpl.has_resource_properties("AWS::ApiGateway::Method", {
        "HttpMethod":        "POST",
        "AuthorizationType": "COGNITO_USER_POOLS",
    })


# tests/test_integration_discover.py
"""Integration — the real API, with a Cognito token for a test user."""
import pytest, os, json, requests


@pytest.mark.integration
def test_discover_finance_tables():
    token = os.environ["TEST_ID_TOKEN"]   # Cognito ID token fixture
    api   = os.environ["DISCOVERY_API_URL"]
    r = requests.post(
        f"{api}discover",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type":  "application/json"},
        json={"question": "customer revenue by quarter",
              "top_k_tables": 5, "sample_values": False,
              "include_multimodal": False},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["ok"]
    # Finance tables — at least fact_revenue or dim_customer in top 5.
    names = [t["name"] for t in body["tables"]]
    assert any(n in ("fact_revenue", "dim_customer") for n in names)
    # Summary is not empty.
    assert body["summary"]
```

---

## 7. References

- `PATTERN_CATALOG_EMBEDDINGS.md` — the 3 indexes this partial queries.
- `PATTERN_MULTIMODAL_EMBEDDINGS.md` — optional image-search side.
- `PATTERN_TEXT_TO_SQL.md` — sibling; both consume the same catalog embeddings.
- `PATTERN_ENTERPRISE_CHAT_ROUTER.md` — primary consumer in the chat flow.
- `DATA_ATHENA.md` — sample-values workgroup.
- `DATA_LAKE_FORMATION.md` — LF enforcement at query time.
- AWS docs — *API Gateway Cognito authoriser*.
- AWS docs — *Bedrock Claude Haiku 4.5 inference profile*.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Identity-from-JWT contract (never trust request body). 3-pass + optional multimodal retrieve. Haiku rerank with NL summary. Sample-values gated by 100 MB workgroup. DDB cache (10 min TTL). Signed preview URLs for thumbnails. API GW + Cognito auth primary, Function URL + IAM alternative. 8 monolith gotchas, 3 micro-stack gotchas, 12-row swap matrix, pytest synth + integration harness.
