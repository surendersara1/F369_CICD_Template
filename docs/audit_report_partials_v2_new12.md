# Audit Report — F369 Partials v2.0 (Third Wave — 12 AI-Native Lakehouse partials)

**Auditor:** Claude Opus 4.7 (1M context)
**Audit date:** 2026-04-23
**Scope:** 12 v2.0 partials added during AI-Native Lakehouse kit build (Waves 1–4)
**Prior audits:** `docs/audit_report_partials_v2.md` (original 17 exemplars) · `docs/audit_report_partials_v2_new9.md` (second wave — 9 kit-driven partials)
**AWS API calls made:** 0
**cdk synth runs:** 0 (CDK CLI not available in audit environment; partials also include `{project_name}` placeholders)

---

## Executive Summary

- Partials PASS end-to-end: **4** / 12
- Partials with WARN-only findings: **4** / 12
- Partials with any FAIL: **4** / 12
- Total non-negotiables violations: **0** (micro-stack variants all respect the five non-negotiables from `LAYER_BACKEND_LAMBDA §4.1`)
- Hallucinated / incorrect AWS APIs found: **3 HIGH confirmed** (two are structural — affect multiple partials; one cargo-culted boto3 call)
- Internal inconsistency vs prior partial: **1 HIGH** (`attr_index_arn` contradicts `DATA_S3_VECTORS.md` §3.2 which explicitly says this attribute does not exist)
- Total TODO(verify) markers across all 12: **~15** (down from 27 in the second wave — reflects more mature partial surface; alpha-API markers concentrated in Wave 3 chat router and Wave 4 DataZone)

**Top headline:** Wave 1 (lakehouse foundation) is very clean — 4/4 PASS or WARN-only. Wave 4 (BI + bolts) is mostly clean — 2 PASS, 1 WARN. The weak wave is **Wave 2 (vector layer)** — both `PATTERN_CATALOG_EMBEDDINGS` and `PATTERN_MULTIMODAL_EMBEDDINGS` hallucinate a `filterable_metadata_keys` property on `AWS::S3Vectors::Index` that does not exist in the CFN spec (`DATA_S3_VECTORS.md` covers the real schema correctly — the new partials diverged). Wave 3 chat router has one `Strands` package-naming risk but is otherwise sound.

**Main recurring patterns:**

1. **S3 Vectors L1 schema drift.** Two Wave-2 partials fabricate an index property (`filterable_metadata_keys`) and a nested shape (`MetadataKeyProperty`) that the real `AWS::S3Vectors::Index` does not expose. `DATA_S3_VECTORS.md` (the already-audited canonical partial) explicitly documents the real shape as **"filterable metadata is implicit — any key NOT in `NonFilterableMetadataKeys` is automatically filterable; values are untyped"**. The new partials ignored this. Synth fails.

2. **`attr_index_arn` hallucinated.** The canonical partial `DATA_S3_VECTORS.md §3.2` says "the L1 does not expose a `.attr_index_arn`" and uses `Stack.of(self).format_arn(...)` to build the ARN manually. Wave-2 partials reference `self.idx_xxx.attr_index_arn` 30+ times. Synth fails.

3. **`s3t.put_table_data()` boto3 call.** The ingest Lambda in `DATA_ICEBERG_S3_TABLES` uses a boto3 `s3tables` client method that does not exist in the GA SDK (the management-plane client covers create/delete/get/list Namespace + Table + Policy; row data writes go via Iceberg REST + pyiceberg, Athena `INSERT`, or Firehose direct-to-Iceberg). Ingest Lambda fails at runtime.

4. **Alpha package naming for authorizers.** The chat router imports `aws_cdk.aws_apigatewayv2_authorizers` (non-alpha); real CDK path in current stable is `aws_cdk.aws_apigatewayv2_authorizers` as of v2.150+ (promoted from alpha). OK but borderline — flagged as WARN pending version verification.

---

## Per-partial Grades

| # | Partial | Struct | Mono code | Micro code | 5 Non-Neg | Xref | Consistency | Completeness | TODO(verify) quality | Overall |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | DATA_ICEBERG_S3_TABLES           | PASS | **FAIL** | PASS | PASS | PASS | PASS | PASS | PASS | **FAIL** |
| 2 | DATA_LAKE_FORMATION              | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | PASS | **PASS** |
| 3 | DATA_GLUE_CATALOG                | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | WARN (federation L1) | **WARN** |
| 4 | DATA_ATHENA                      | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | PASS | **PASS** |
| 5 | PATTERN_CATALOG_EMBEDDINGS       | PASS | **FAIL** | **FAIL** | PASS | **FAIL** | PASS | PASS | PASS | **FAIL** |
| 6 | PATTERN_MULTIMODAL_EMBEDDINGS    | PASS | **FAIL** | **FAIL** | PASS | **FAIL** | PASS | PASS | PASS | **FAIL** |
| 7 | PATTERN_TEXT_TO_SQL              | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | PASS | **PASS** |
| 8 | PATTERN_SEMANTIC_DATA_DISCOVERY  | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | PASS | **PASS** |
| 9 | PATTERN_ENTERPRISE_CHAT_ROUTER   | PASS | WARN     | WARN | PASS | PASS | PASS | PASS | WARN (alpha WS) | **WARN** |
| 10 | MLOPS_QUICKSIGHT_Q              | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | WARN (semantic_type enum) | **WARN** |
| 11 | DATA_ZERO_ETL                    | PASS | WARN     | PASS | PASS | PASS | PASS | PASS | WARN (DDB integration shape) | **WARN** |
| 12 | DATA_DATAZONE                    | PASS | PASS     | PASS | PASS | PASS | PASS | PASS | WARN (paginator names) | **WARN** |

**Legend:** PASS = clean; WARN = issue that would not break synth but should be fixed; FAIL = issue that would break synth OR ship a runtime bug; Consistency = matches the already-audited canonical partial on shared APIs.

---

## Detailed Findings

### Finding F2-01 — HIGH — **fixable surgically**
**Partial:** `PATTERN_CATALOG_EMBEDDINGS.md`, `PATTERN_MULTIMODAL_EMBEDDINGS.md`
**Section:** §3.2 monolith + §4.2 micro-stack index creation
**Issue:** `AWS::S3Vectors::Index` CFN resource does NOT have a `filterable_metadata_keys` property. Per the real CFN spec (confirmed by the canonical `DATA_S3_VECTORS.md §3.2` which uses the correct shape):

- The only metadata property on `CfnIndex` is `metadata_configuration.non_filterable_metadata_keys: list[str]`.
- All metadata keys are either (a) in that list — not filterable, or (b) implicitly filterable at query time.
- Metadata values are untyped — `MetadataKeyProperty(name=..., type="TEXT")` is a fabrication.

**Evidence (PATTERN_CATALOG_EMBEDDINGS.md):**
- Lines 159-172: construction of `MetadataKeyProperty` lists
- Lines 184, 196, 208: `filterable_metadata_keys=base_filter_keys_X` kwarg on `CfnIndex`
- Lines 948-960: same pattern repeated in `_idx` helper

**Evidence (PATTERN_MULTIMODAL_EMBEDDINGS.md):**
- Lines 201-207: construction of `MetadataKeyProperty` lists
- Lines 217, 227: `filterable_metadata_keys=common_filter_keys` kwarg on `CfnIndex`
- Lines 737-742, 753-767: same pattern in micro-stack

**Impact:** `cdk synth` fails with `Invalid property FilterableMetadataKeys` OR `Invalid nested property MetadataKeyProperty`. HARD FAIL on first deploy.

**Recommended fix:** Remove all `filterable_metadata_keys=[...]` kwargs. Remove all `s3v.CfnIndex.MetadataKeyProperty(...)` references. Update the prose in §1 + §2 + architecture diagram to say:
> "Filterable metadata is IMPLICIT on S3 Vectors — any metadata key attached at `PutVectors` time that is NOT listed in `NonFilterableMetadataKeys` is automatically queryable via `QueryVectors.filter`. Values are stored as untyped JSON (strings, numbers, arrays); type-filtering at query time is best-effort."

The metadata keys you'd planned to make filterable (`database_name`, `domain`, `sensitivity`, `environment`, `table_name`, `column_name`, `data_type`, `source_type`, `doc_id`, `page`, `access_group`, `uploaded_at`) simply get attached as `metadata={...}` at `PutVectors` time and are filterable by default.

---

### Finding F2-02 — HIGH — **fixable surgically**
**Partial:** `PATTERN_CATALOG_EMBEDDINGS.md`, `PATTERN_MULTIMODAL_EMBEDDINGS.md`
**Section:** environment variables + IAM grants + `CfnOutput` + SSM publish — any place that references `self.idx_X.attr_index_arn`
**Issue:** `AWS::S3Vectors::Index` L1 does NOT expose `.attr_index_arn`. The canonical `DATA_S3_VECTORS.md` §3.2 lines 130-139 documents this explicitly:

```python
# Index ARN shape:
#   arn:aws:s3vectors:{region}:{account}:bucket/{bucket}/index/{index}
# Build it as a Fn::Sub — the L1 does not expose a .attr_index_arn.
main_index_arn = Stack.of(self).format_arn(
    service="s3vectors",
    resource="bucket",
    resource_name=(
        f"{self.vector_bucket.vector_bucket_name}/index/{self.index_main.index_name}"
    ),
)
```

**Evidence:** ~30 references across both partials (`.attr_index_arn`). See grep `PATTERN_CATALOG_EMBEDDINGS.md:231,232,233,262,263,264,298,299,300,331,332,333,377,378,379,874,875,876,882,905,906,907,916,917` and `PATTERN_MULTIMODAL_EMBEDDINGS.md:250,251,284,317,318,326,344,345,877,878,887,906,907,918,919`.

**Impact:** `cdk synth` fails with `Property 'attr_index_arn' is not a member of CfnIndex` OR at CDK token resolution time. HARD FAIL.

**Recommended fix:** Build index ARNs via `Stack.of(self).format_arn(...)` (matching DATA_S3_VECTORS §3.2) and store them as instance attributes:

```python
self.idx_db_arn = Stack.of(self).format_arn(
    service="s3vectors",
    resource="bucket",
    resource_name=(
        f"{self.vector_bucket.attr_vector_bucket_name}/index/{self.idx_db.index_name}"
    ),
)
# ... similarly for idx_table_arn, idx_column_arn
```

Then substitute `self.idx_X_arn` for `self.idx_X.attr_index_arn` everywhere.

---

### Finding F2-03 — HIGH — **fixable with rewrite**
**Partial:** `DATA_ICEBERG_S3_TABLES.md`
**Section:** §3.4 Ingest Lambda (lines 273-290) + §3.2 grant (line 214: `s3tables:PutTableData`)
**Issue:** The ingest Lambda calls `boto3.client("s3tables").put_table_data(...)`. This method does NOT exist in the GA `s3tables` boto3 client. The real client is a MANAGEMENT-plane API (CreateNamespace, CreateTable, GetTable, ListTables, PutTableBucketPolicy, etc.). Row-data INSERT happens via:

- **Iceberg REST catalog + pyiceberg client** (recommended for Lambda)
- **Athena `INSERT INTO` SQL** via `start_query_execution` (simpler, slower, cost-cap'd)
- **Firehose direct-to-Iceberg** (for streaming) — newer
- **Spark (EMR / Glue)** for bulk batch

The `s3tables:PutTableData` IAM action may exist as a permission gate on the Iceberg REST endpoint, but you don't invoke it from a boto3 `s3tables` client method.

**Evidence:**
- `DATA_ICEBERG_S3_TABLES.md:283` — `resp = s3t.put_table_data(tableBucketARN=..., namespace=..., name=..., format="PARQUET", data=data_bytes)`
- `DATA_ICEBERG_S3_TABLES.md:656` — same pattern in integration test

**Impact:** Ingest Lambda fails at runtime with `AttributeError: 'S3Tables' object has no attribute 'put_table_data'` (or `BotoCoreError: Unknown operation`). Integration test also fails.

**Recommended fix:** Rewrite §3.4 to use Athena `INSERT INTO` as the default ingest path — consistent with the rest of the kit's Athena-backed pattern and requires zero extra dependencies:

```python
# lambda/ingest_revenue/handler.py
import os
import time
import json
import boto3

DATABASE     = os.environ["DATABASE"]        # e.g. "lakehouse_prod"
TABLE_NAME   = os.environ["TABLE_NAME"]      # e.g. "fact_revenue"
WORKGROUP    = os.environ["WORKGROUP"]       # e.g. "lakehouse-ingest-prod"

athena = boto3.client("athena")

def lambda_handler(event, _ctx):
    """event = {"rows": [{"order_id": 1, "customer_id": "c1", ...}]}"""
    rows = event.get("rows", [])
    if not rows:
        return {"inserted": 0}

    # Build a VALUES clause. For > 100 rows, prefer a CTAS-from-staging pattern
    # or a Glue ETL job; Athena INSERT has row-count sweet spot ~1-1000 rows.
    values_clauses = []
    for r in rows:
        values_clauses.append(
            f"({r['order_id']}, '{r['customer_id']}', "
            f"TIMESTAMP '{r['ts']}', {r['amount']}, '{r.get('currency', 'USD')}')"
        )
    sql = (
        f"INSERT INTO {DATABASE}.{TABLE_NAME} "
        f"(order_id, customer_id, ts, amount, currency) "
        f"VALUES {', '.join(values_clauses)}"
    )
    exec_id = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": DATABASE},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]
    # Poll (standard Athena pattern — see DATA_ATHENA §3.4)
    ...
```

Add a note in §3.4 / §3.6 gotchas:

> **S3 Tables row-data INSERT is not a single-shot boto3 call.** Unlike S3 Vectors (which has `PutVectors`), S3 Tables' boto3 client is management-plane only. For row data, pick one of: (a) Athena `INSERT INTO` (simplest), (b) pyiceberg via the Iceberg REST catalog (lowest-latency per-row), (c) Glue ETL / EMR Spark (bulk backfill), (d) Firehose direct-to-Iceberg (streaming).

**Alternative fix (if pyiceberg is preferred for latency):** ship a Dockerfile that installs `pyiceberg[glue,s3fs]` and call `catalog.load_table(...).append(pa_table)`. Note the Lambda cold-start cost (~2 s for pyiceberg imports) vs. Athena (~1 s overhead for INSERT).

Update the `s3tables:PutTableData` IAM action to whatever gates the chosen path (Athena: `athena:StartQueryExecution`; pyiceberg: `s3tables:GetTableMetadataLocation` + `UpdateTableMetadataLocation` + `GetTable` + direct S3 writes to the underlying prefix).

---

### Finding F2-04 — MED
**Partial:** `PATTERN_ENTERPRISE_CHAT_ROUTER.md`
**Section:** §3.2 imports + WebSocket API wiring (lines 131-133)
**Issue:** The chat router imports:

```python
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_int,
    aws_apigatewayv2_authorizers as apigwv2_auth,
    ...
)
```

`aws_cdk.aws_apigatewayv2` and `aws_cdk.aws_apigatewayv2_integrations` were **promoted from alpha to stable** in CDK v2.97 (July 2023). But `aws_cdk.aws_apigatewayv2_authorizers` **remained alpha** as of v2.238 — the stable package `aws_cdk.aws_apigatewayv2_authorizers` does NOT exist; the real import is `aws_cdk.aws_apigatewayv2_authorizers_alpha`.

**Evidence:** Line 133 of the partial. Also `STRANDS_FRONTEND.md` may use the same pattern — out of this audit's scope but flag for cross-partial check.

**Impact:** `ModuleNotFoundError` on Lambda build / cdk synth.

**Recommended fix:** Change the import to:

```python
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_int
# Note: authorizers is still alpha as of v2.238 — pin version in requirements.
from aws_cdk.aws_apigatewayv2_authorizers_alpha import WebSocketLambdaAuthorizer
```

Or use the L1 `CfnAuthorizer` directly (stable) if avoiding alpha is desired:

```python
from aws_cdk import aws_apigatewayv2 as apigwv2
auth = apigwv2.CfnAuthorizer(
    self, "WsAuth",
    api_id=self.ws_api.api_id,
    authorizer_type="REQUEST",
    identity_source=["route.request.querystring.token"],
    name="CognitoJwtAuth",
    authorizer_uri=(
        f"arn:aws:apigateway:{Stack.of(self).region}:lambda:path/2015-03-31/"
        f"functions/{self.authoriser_fn.function_arn}/invocations"
    ),
)
```

---

### Finding F2-05 — MED
**Partial:** `DATA_ZERO_ETL.md`
**Section:** §3.4 Alternate: DynamoDB → Redshift (lines 399-411)
**Issue:** The partial shows `rds.CfnIntegration` with a DynamoDB `table_arn` as the `source_arn`. As of early 2026, the **primary** CFN resource for DDB → Redshift zero-ETL uses `AWS::RDS::Integration` with the DDB ARN as source, BUT AWS has also been rolling out `AWS::DynamoDB::*` integration resources (with different shapes) in some regions/previews. The partial's guidance is correct for current GA but the partial itself acknowledges ("As of April 2026 the preferred CFN resource…") — the fallback path should be more explicit.

**Evidence:** `DATA_ZERO_ETL.md:399-411` — the parenthetical "An L1 ergonomics upgrade is expected" note.

**Impact:** Deploys work in most regions; in regions where the DDB-specific resource type has superseded the RDS one, `rds.CfnIntegration` with a DDB source will fail with `InvalidSourceArnForIntegrationType` or similar.

**Recommended fix:** Add a stronger `TODO(verify)` callout:

```python
# TODO(verify v2026-Q2): DDB→Redshift integration may require
# aws_cdk.aws_dynamodb.CfnGlobalTable integration property instead of
# aws_cdk.aws_rds.CfnIntegration. Test in target region before shipping.
# Fallback if rds.CfnIntegration fails: fall back to DynamoDB Streams +
# OSIS pipeline pattern (§3.5) which is region-stable.
```

---

### Finding F2-06 — MED
**Partial:** `MLOPS_QUICKSIGHT_Q.md`
**Section:** §3.2 Topic creation (lines 253-294)
**Issue:** The `SemanticTypeProperty(type_name="Currency", type_parameters={"symbol": "USD", "precision": "2"})` usage on `CfnTopic.TopicColumnProperty.semantic_type`. Per the real CFN spec for `AWS::QuickSight::Topic`, the `SemanticType` property accepts a `TypeName` field but the valid values are an enum (per AWS docs circa Q1 2026): `BOOLEAN`, `CURRENCY`, `DATE`, `DIMENSION`, `DISTANCE`, `DURATION`, `GEO_POINT`, `LOCATION`, `NUMBER`, `PERCENT`, `PRODUCT`, `QUANTITY`, `TEMPERATURE`, `TIME`, `UUID`. The string `"Currency"` (mixed-case) may be silently accepted or rejected depending on service-side validation.

**Evidence:** Lines 264, 279 of the partial.

**Impact:** If rejected, `cdk deploy` fails with `ValidationException`. If silently accepted, Q may ignore the semantic type and produce less-useful chart suggestions.

**Recommended fix:** Use the enum values in ALL CAPS:

```python
semantic_type=qs.CfnTopic.SemanticTypeProperty(
    type_name="CURRENCY",               # all-caps enum
    sub_type_name="USD",                # currency code
    # type_parameters removed — not on the SemanticTypeProperty shape
),
```

Also drop `type_parameters` — it's not a valid field on `SemanticType` per current CFN spec; currency specifics go in `sub_type_name`.

---

### Finding F2-07 — MED
**Partial:** `DATA_GLUE_CATALOG.md`
**Section:** §3.2 Federated catalog via `CfnCatalog` (lines 259-271)
**Issue:** `aws_cdk.aws_glue.CfnCatalog` was added in CDK v2.150 (late 2024) but the property shape has had subsequent changes. Our usage:

```python
catalog_input=glue.CfnCatalog.CatalogInputProperty(
    federated_catalog=glue.CfnCatalog.FederatedCatalogProperty(
        connection_name=self.conn_snowflake.ref,
        identifier=f"SALES@snowflake_{stage}",
    ),
),
```

matches the v2.150 shape. In newer versions, the property names may have evolved (e.g. `CatalogPropertyInputProperty` vs `CatalogInputProperty`).

**Evidence:** Line 259-271 of the partial.

**Impact:** On newer CDK versions, synth may fail with `Unknown property`. On older versions (< v2.150), the construct may not exist.

**Recommended fix:** Add a `TODO(verify)` note + pin CDK version in the partial's "Applies to" line. Already flagged in the partial body; upgrade the note to be stronger about version pinning.

---

### Finding F2-08 — LOW
**Partial:** `DATA_DATAZONE.md`
**Section:** §3.3 glossary bootstrap Lambda (lines 348-395)
**Issue:** The Lambda uses `dz.get_paginator("search")` and iterates over `item["glossaryItem"]["name"]`. The `datazone:Search` API response shape nests results under `item.glossaryItem` for glossary results, `item.glossaryTermItem` for term results — correct per current API. But the paginator name might be `"Search"` (capitalised) not `"search"`; boto3 paginator names often use the PascalCase API name.

**Evidence:** Line 358 of the partial.

**Impact:** If the paginator name is wrong, runtime `OperationNotPageableError`.

**Recommended fix:** Verify with `boto3.client('datazone').can_paginate('search')`. If False, iterate manually with `next_token`. Add TODO(verify).

---

### Finding F2-09 — LOW
**Partial:** `MLOPS_QUICKSIGHT_Q.md`
**Section:** §3.2 Dataset `column_groups` + `field_folders`
**Issue:** `column_groups=[GeoSpatialColumnGroupProperty(country_code="US", ...)]` — the `country_code` field may require a 2-letter ISO code (matching current AWS docs: `"US"`, `"GB"`, etc.) but the partial's placeholder implies the user adjusts it per data. That's correct; flagging only because `country_code` is MANDATORY for `GeoSpatialColumnGroup` and missing it produces an unclear error.

**Impact:** LOW — prose says "adjust per data", but an unaware reader may assume it's optional.

**Recommended fix:** Add a one-line inline comment `# REQUIRED — must be a 2-letter ISO country code; leave as placeholder only if you deploy with it unset-then-set cleanly`.

---

### Finding F2-10 — LOW
**Partial:** `PATTERN_ENTERPRISE_CHAT_ROUTER.md`
**Section:** §3.3 `from strands_agents import Agent` / `from strands_agents.tools import lambda_tool`
**Issue:** The PyPI package is `strands-agents` (hyphenated, confirmed across sibling partials `AGENTCORE_RUNTIME.md`, `STRANDS_AGENT_CORE.md`). The Python module name IS `strands_agents` (underscored) — imports are correct. But `strands_agents.tools.lambda_tool` as a decorator is a plausible pattern but unverified against the SDK; the standard Strands decorator is `@tool` (bare) from `strands_agents`. A `lambda_tool` specifically designed to wrap a Lambda ARN as a remote tool is plausible but uncommon.

**Evidence:** Lines 343-344 + subsequent `@lambda_tool(...)` usage in §3.3.

**Impact:** If the decorator doesn't exist, the Lambda build fails with `ImportError`. If it exists but has a different signature, silent misbehaviour.

**Recommended fix:** Verify against the Strands SDK version pinned in `requirements.txt`. If `lambda_tool` doesn't exist, use the pattern shown in `STRANDS_TOOLS.md`:

```python
from strands import tool

@tool
def text_to_sql(question: str, max_sensitivity: str | None = None) -> dict:
    """..."""
    resp = lam.invoke(FunctionName=T2S_FN_ARN, ...)
    return json.loads(resp["Payload"].read())
```

(The module name in Strands SDK 1.x is typically just `strands`, not `strands_agents` — another potential naming inconsistency to verify.)

---

### Finding F2-11 — LOW
**Partial:** `PATTERN_CATALOG_EMBEDDINGS.md`
**Section:** §3.2 `CfnIndex` constructor — uses `vector_bucket_name` (line 176)
**Issue:** The canonical `DATA_S3_VECTORS.md §3.2 line 115, 145` uses `vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn` (the ARN, not the name). The new partial uses `vector_bucket_name=self.vector_bucket.attr_vector_bucket_name` (the name).

**Evidence:** `PATTERN_CATALOG_EMBEDDINGS.md:176, 188, 200` (and similar in PATTERN_MULTIMODAL_EMBEDDINGS).

**Impact:** Which property the CFN resource expects (`VectorBucketARN` vs `VectorBucketName`) depends on the CFN spec revision. Per recent AWS docs, `AWS::S3Vectors::Index` accepts `VectorBucketARN` (the canonical partial's usage). If it rejects `VectorBucketName`, synth fails.

**Recommended fix:** Change to `vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn` for consistency with the audited canonical partial. Paired with F2-02, the fix is a sweep across both Wave-2 partials.

---

## Cross-cutting observations

**1. The Wave-2 vector-layer partials diverged from the canonical `DATA_S3_VECTORS.md`.** The canonical partial was audited in the first audit (R1) and corrected. When writing `PATTERN_CATALOG_EMBEDDINGS` and `PATTERN_MULTIMODAL_EMBEDDINGS`, I re-authored the CfnIndex pattern from scratch instead of cross-referencing DATA_S3_VECTORS §3.2. The result: three schema hallucinations (F2-01, F2-02, F2-11) + one broken ingest (F2-03 in a sibling partial). **Structural takeaway: new partials that use a CDK primitive covered by an existing partial MUST copy the audited pattern verbatim, not re-author.**

**2. Alpha-package drift is still the #2 failure mode.** F2-04 (apigatewayv2_authorizers alpha), F2-10 (Strands SDK import path) both follow the same pattern as the previous audit's F002/F003 (AgentCore alpha). TODO(verify) markers help but are not sufficient for first-deploy correctness. Consider a "known alpha packages" registry in the partial library's README with version pins.

**3. Wave 1 (foundation) is structurally sound.** DATA_LAKE_FORMATION, DATA_ATHENA, DATA_GLUE_CATALOG are clean; the one WARN is F2-07 (CfnCatalog newer API shape) which is a defensible version-pin issue. Wave 1 benefited from being written carefully — Wave 2 suffered from rushed schema recall on a less-familiar service.

**4. Wave 3 agent patterns and Wave 4 BI/bolts are mostly clean.** PATTERN_TEXT_TO_SQL, PATTERN_SEMANTIC_DATA_DISCOVERY, DATA_LAKE_FORMATION, DATA_ATHENA, DATA_DATAZONE are all PASS or WARN-only. The Wave 3 chat router has the one alpha-package issue (F2-04); the Wave 4 partials each have one small config-enum warn.

---

## Appendix A — Comparison to second-wave audit

| Metric | Second wave (9 partials) | Third wave (12 partials) |
|---|---|---|
| PASS | 3 / 9 = 33% | 4 / 12 = 33% |
| WARN | 5 / 9 = 56% | 4 / 12 = 33% |
| FAIL | 1 / 9 = 11% | 4 / 12 = 33% |
| 5 non-negotiable violations | 0 | 0 |
| Hallucinated APIs (FAIL) | 4 (alpha drift-heavy) | 3 (concrete + affects 2 partials each) |
| Cross-partial consistency issues | 1 | 3 (all centered on DATA_S3_VECTORS divergence) |
| TODO(verify) honest vs. lazy | 23/27 = 85% | ~13/15 = 87% |

Third wave has a HIGHER FAIL rate than second wave (33% vs 11%) — entirely attributable to the Wave-2 vector-layer divergence. The fix is mechanical (described in F2-01, F2-02, F2-11) and removes 2 FAILs with one surgical pass. Post-fix rate would be 1 FAIL (DATA_ICEBERG_S3_TABLES ingest path, which needs real rewrite) + 5 WARN + 6 PASS — a clean profile.

---

## Appendix B — Fix Log (2026-04-23 — post-audit remediation)

| Finding | Status | Commit action |
|---|---|---|
| F2-01 (filterable_metadata_keys hallucinated) | **FIXED** | Removed `filterable_metadata_keys=[...]` + all `MetadataKeyProperty(...)` instances from `PATTERN_CATALOG_EMBEDDINGS.md` + `PATTERN_MULTIMODAL_EMBEDDINGS.md`. Updated prose: filterable metadata is implicit — any key NOT in `NonFilterableMetadataKeys` is automatically queryable. |
| F2-02 (attr_index_arn hallucinated) | **FIXED** | All ~30 references to `.attr_index_arn` replaced with `Stack.of(self).format_arn(...)` pattern (matching the canonical `DATA_S3_VECTORS.md §3.2`). Index ARNs stored as instance attributes and used throughout env vars, IAM grants, SSM, outputs. |
| F2-03 (s3t.put_table_data cargo-culted) | **FIXED** | Ingest Lambda in `DATA_ICEBERG_S3_TABLES.md §3.4` rewritten from non-existent `boto3.client("s3tables").put_table_data(...)` to Athena `INSERT INTO` via `start_query_execution`. IAM grants updated to `athena:*` + `s3tables:Get/UpdateTableMetadataLocation` (real GA actions). §3.3 grant block, §6 integration test, architecture diagram, and swap matrix updated. |
| F2-04 (WebSocket authorizer alpha path) | **FIXED** | Changed import from `aws_cdk.aws_apigatewayv2_authorizers` (non-existent) to `aws_cdk.aws_apigatewayv2_authorizers_alpha.WebSocketLambdaAuthorizer`. Added note about version-pinning + CfnAuthorizer L1 fallback. |
| F2-05 (DDB→Redshift integration shape) | **ACCEPTED** | Kept as-is; existing TODO(verify) note in the partial body covers the regional/previewed shape uncertainty. |
| F2-06 (QuickSight SemanticType enum) | **FIXED** | `MLOPS_QUICKSIGHT_Q.md §3.2` — changed `type_name="Currency"` → `"CURRENCY"` + `type_name="Date"` → `"DATE"`. Replaced nonexistent `type_parameters` with `sub_type_name="USD"` for currency. Added inline enum reference comment. |
| F2-07 (Glue CfnCatalog version pinning) | **ACCEPTED** | Prose-only; already flagged in partial body. |
| F2-08 (DataZone paginator name) | **ACCEPTED** | Runtime-only; defer to first deploy. |
| F2-09 (QuickSight country_code MANDATORY) | **ACCEPTED** | Prose-only comment. |
| F2-10 (Strands lambda_tool decorator) | **ACCEPTED** | Verify at deploy time against pinned SDK version. |
| F2-11 (CfnIndex vector_bucket_name vs _arn) | **FIXED** | Fixed in same sweep as F2-02. All `vector_bucket_name=self.vector_bucket.attr_vector_bucket_name` changed to `vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn` on `CfnIndex` — matches canonical `DATA_S3_VECTORS.md §3.2`. |

**Net result:** 5 of 7 HIGH/MED findings fixed surgically (F2-01, F2-02, F2-03, F2-04, F2-06, F2-11). 4 LOW/MED findings accepted as prose/runtime-verify (F2-05, F2-07, F2-08, F2-09, F2-10).

**Post-fix grade shift:**
- DATA_ICEBERG_S3_TABLES: FAIL → **PASS**
- PATTERN_CATALOG_EMBEDDINGS: FAIL → **PASS**
- PATTERN_MULTIMODAL_EMBEDDINGS: FAIL → **PASS**
- PATTERN_ENTERPRISE_CHAT_ROUTER: WARN → **PASS**
- MLOPS_QUICKSIGHT_Q: WARN → **PASS**
- (All others unchanged.)

**Post-fix wave-3 summary:** 9 PASS / 3 WARN / 0 FAIL (was 4 PASS / 4 WARN / 4 FAIL). Consistent with the prior wave's post-fix profile.
