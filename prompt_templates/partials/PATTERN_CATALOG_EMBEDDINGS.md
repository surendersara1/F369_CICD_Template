# SOP — Catalog embeddings (semantic index over Glue Data Catalog metadata)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · Glue Data Catalog 3.0 · Amazon Titan Text Embeddings v2 (1024 / 512 / 256 dims) · Amazon S3 Vectors (GA 2025) primary storage · OpenSearch Serverless hybrid search secondary · EventBridge Glue-Catalog-Change events · Bedrock `InvokeModel` for embedding generation

---

## 1. Purpose

- Provide the deep-dive for **catalog embeddings** — the pattern that turns the Glue Data Catalog (databases, tables, columns, descriptions, comments, parameters) into a **vector index** that agents + UIs can semantically query. "Find data about customer billing" returns `fact_revenue` + `dim_customer` + `stg_invoices` — ranked by meaning, not keyword match.
- Codify the **three-level embedding model** — database-level vectors, table-level vectors, column-level vectors — each in its own S3 Vectors index with filterable metadata (`database_name`, `table_name`, `column_name`, `data_type`, `domain`, `sensitivity`) for post-filter ranking and LF-Tag-aware scoping.
- Codify **what to embed, specifically**:
  - **Database**: `description` + user-facing `parameters` values (owner, domain, cost_center).
  - **Table**: `description` + `parameters` (classification, metadata_location, domain tags) + a **column-summary sentence** ("Columns: order_id (bigint), customer_id (string, FK to dim_customer), ...").
  - **Column**: `name` + `comment` + `type` + optional **sampled value signature** (5-10 distinct sample values, hashed or sanitized).
- Codify the **refresh contract** — `glue:CreateTable`, `glue:UpdateTable`, `glue:DeleteTable`, `glue:CreatePartition`, `glue:UpdatePartition` all emit EventBridge events on the default bus. A `CatalogEmbeddingRefreshFn` consumes the events, re-embeds only changed resources (fingerprint diff), and upserts into the S3 Vectors index. Bulk re-embed on-demand via a `BulkRefreshFn` triggered by Step Functions.
- Codify the **query-side pattern** — semantic search in three passes:
  1. Database-level: "which domain / database has this?" (coarse gate, 5-10 DBs)
  2. Table-level (filtered by shortlisted DBs): "which tables match?"
  3. Column-level (filtered by shortlisted tables): "which columns are relevant?"
  The agent then composes SQL against the ranked columns. Wave 3's `PATTERN_TEXT_TO_SQL` consumes this output.
- Codify the **LF-Tag-aware filter pushdown** — the catalog embedding index stores `domain` / `sensitivity` / `environment` LF-Tag values as filterable metadata. Queries from a role with `domain=finance` access see only `domain=finance` vectors — **before** the similarity sort, not after. This prevents the "semantic search leaks sensitive tables" failure mode.
- Codify the **PII handling contract** — column sample values are sanitised (string pattern → `<STRING>`, number → `<NUMBER>`, email → `<EMAIL>`, UUID → `<UUID>`) OR dropped entirely for `sensitivity=pii` columns. The column NAME and COMMENT are always indexable (they are metadata, not data); sample values need scrubbing.
- Include when the SOW signals: "semantic catalog search", "AI data discovery", "find data by meaning", "LLM agent over catalog", "natural-language data dictionary", "text-to-SQL grounding", "self-service analytics with search".
- This partial is the **INDEX layer** that sits between `DATA_GLUE_CATALOG` (source of truth) and the agent-side consumers (`PATTERN_TEXT_TO_SQL`, `PATTERN_ENTERPRISE_CHAT_ROUTER`, `PATTERN_SEMANTIC_DATA_DISCOVERY`). It stores vectors in `DATA_S3_VECTORS`-backed indexes. LF-aware filters come from `DATA_LAKE_FORMATION` tags.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the three S3 Vectors indexes, the refresh Lambda, the EB rules, and the bulk-reindex Step Function | **§3 Monolith Variant** |
| `CatalogEmbeddingStack` owns the vector bucket + 3 indexes + refresh Lambda + EB rules + Step Function; `AgentStack` owns the query-side Lambdas with identity-side grants | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **S3 Vectors indexes are immutable on 4 properties** (see `DATA_S3_VECTORS §2`): `IndexName`, `Dimension`, `DistanceMetric`, `NonFilterableMetadataKeys`. A dimension change (Titan v2 1024 → v2 512) requires a replacement; keeping the index in `CatalogEmbeddingStack` scopes the blast radius.
2. **Refresh Lambda fans out on every catalog mutation.** In a busy lake, 100s of `glue:UpdateTable` events per hour. The Lambda must be idempotent, batch-aware, and DLQ-backed. If it lives with consumers, restart storms take down agent query paths.
3. **Bulk reindex is Step Functions + Glue-paged scans.** Walking 10,000 tables takes 15+ minutes of `glue:GetTables` pagination + Bedrock embedding calls; this MUST be a SFN workflow, not a Lambda. Keep the SFN and its state machine in CatalogEmbeddingStack.
4. **The vector bucket + CMK are owned by this stack**. Consumers get query-only grants via identity-side policies read from SSM.
5. **EventBridge rules are producer-side**. The rules live in `CatalogEmbeddingStack` (the producer of embedding refreshes); consumer Lambdas never subscribe to raw Glue events.

Micro-Stack fixes all of this by: (a) owning the vector bucket + 3 indexes (db / table / column) + refresh Lambda + EB rules + bulk-reindex SFN + local CMK in `CatalogEmbeddingStack`; (b) publishing `VectorBucketName`, `DbIndexArn`, `TableIndexArn`, `ColumnIndexArn`, `QueryFnRoleArn`-contract via SSM; (c) `AgentStack` / `ApiStack` consumers grant themselves `s3vectors:QueryVectors` on specific index ARNs + `bedrock:InvokeModel` for the embedding model (to embed the user query before searching).

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Glue Data Catalog (source of truth — see DATA_GLUE_CATALOG)
         │
         ├── EventBridge: aws.glue events
         │     detail-type: "Glue Data Catalog Table State Change"
         │     detail-type: "Glue Data Catalog Database State Change"
         │     detail-type: "Glue Data Catalog Partition State Change"
         │
         ▼
  CatalogEmbeddingRefreshFn (Lambda)
         │
         ├── 1) glue:GetDatabase / GetTable / GetColumns for the changed resource
         ├── 2) Build 3 embedding prompts (db-level, table-level, column-level)
         ├── 3) Fingerprint each (sha256 over description + columns+comments)
         ├── 4) Skip if fingerprint unchanged (idempotent refresh)
         ├── 5) bedrock.invoke_model(titan-embed-v2, input=prompt) × N
         ├── 6) s3vectors.PutVectors(index_arn, vectors)
         └── 7) Log CloudWatch metric "CatalogEmbeddingRefreshed" per level
                      │
                      ▼
  S3 Vectors Bucket: {project}-catalog-vectors-{stage}
    Index: catalog-db-level          (dim 1024, cosine)
      FilterableMetadata:
        database_name     TEXT
        domain            TEXT        ← LF-Tag
        environment       TEXT        ← LF-Tag
      NonFilterable: source_text, fingerprint

    Index: catalog-table-level       (dim 1024, cosine)
      FilterableMetadata:
        database_name     TEXT
        table_name        TEXT
        table_type        TEXT        (EXTERNAL_TABLE / ICEBERG / VIRTUAL_VIEW)
        domain            TEXT        ← LF-Tag
        sensitivity       TEXT        ← LF-Tag
        environment       TEXT
      NonFilterable: source_text, fingerprint, columns_json

    Index: catalog-column-level      (dim 1024, cosine)
      FilterableMetadata:
        database_name     TEXT
        table_name        TEXT
        column_name       TEXT
        data_type         TEXT
        sensitivity       TEXT        ← LF-Tag, column-level (PII, etc.)
      NonFilterable: source_text, fingerprint

         ▲
         │   s3vectors.QueryVectors(index_arn,
         │                          queryVector,
         │                          topK=20,
         │                          filter={"domain": "finance",
         │                                  "sensitivity": {"$ne": "pii"}},
         │                          returnMetadata=true,
         │                          returnDistance=true)
  SemanticDiscoveryFn (query-side)
```

### 3.2 CDK — `_create_catalog_embeddings()` method body

```python
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_s3vectors as s3v,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction


def _create_catalog_embeddings(self, stage: str) -> None:
    """Monolith variant. Assumes self.{glue_db_name, ci_deploy_role_arn}
    exist. Builds the vector bucket + 3 indexes + refresh Lambda + EB rules
    + bulk-reindex SFN."""

    # A) Local CMK (separate from other stack keys — immutable encryption
    #    on S3 Vectors indexes, so this is a long-lived boundary).
    self.embedding_cmk = kms.Key(
        self, "EmbeddingCmk",
        alias=f"alias/{{project_name}}-catalog-embed-{stage}",
        enable_key_rotation=True,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Vector bucket — follows DATA_S3_VECTORS §3 pattern.
    self.vector_bucket = s3v.CfnVectorBucket(
        self, "CatalogVectorBucket",
        vector_bucket_name=f"{{project_name}}-catalog-vectors-{stage}",
        encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
            sse_type="aws:kms",
            kms_key_arn=self.embedding_cmk.key_arn,
        ),
    )

    # C) Three indexes — db, table, column. All 1024-dim cosine.
    #    `non_filterable_metadata_keys=["source_text","fingerprint"]` stores
    #    the raw embedded text + fingerprint for one-hop retrieval without
    #    creating filter-table overhead.
    base_filter_keys_db = [
        s3v.CfnIndex.MetadataKeyProperty(name="database_name",   type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="domain",          type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="environment",     type="TEXT"),
    ]
    base_filter_keys_table = base_filter_keys_db + [
        s3v.CfnIndex.MetadataKeyProperty(name="table_name",      type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="table_type",      type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="sensitivity",     type="TEXT"),
    ]
    base_filter_keys_column = base_filter_keys_table + [
        s3v.CfnIndex.MetadataKeyProperty(name="column_name",     type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="data_type",       type="TEXT"),
    ]

    self.idx_db = s3v.CfnIndex(
        self, "IdxDbLevel",
        vector_bucket_name=self.vector_bucket.attr_vector_bucket_name,
        index_name="catalog-db-level",
        data_type="float32",
        dimension=1024,
        distance_metric="cosine",
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_text", "fingerprint"],
        ),
        filterable_metadata_keys=base_filter_keys_db,
    )
    self.idx_table = s3v.CfnIndex(
        self, "IdxTableLevel",
        vector_bucket_name=self.vector_bucket.attr_vector_bucket_name,
        index_name="catalog-table-level",
        data_type="float32",
        dimension=1024,
        distance_metric="cosine",
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_text", "fingerprint", "columns_json"],
        ),
        filterable_metadata_keys=base_filter_keys_table,
    )
    self.idx_column = s3v.CfnIndex(
        self, "IdxColumnLevel",
        vector_bucket_name=self.vector_bucket.attr_vector_bucket_name,
        index_name="catalog-column-level",
        data_type="float32",
        dimension=1024,
        distance_metric="cosine",
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_text", "fingerprint"],
        ),
        filterable_metadata_keys=base_filter_keys_column,
    )
    for idx in (self.idx_db, self.idx_table, self.idx_column):
        idx.add_dependency(self.vector_bucket)

    # D) DLQ for the refresh Lambda — catalog mutation events are loss-prone.
    self.refresh_dlq = sqs.Queue(
        self, "CatalogEmbedRefreshDlq",
        encryption=sqs.QueueEncryption.KMS_MANAGED,
        retention_period=Duration.days(14),
    )

    # E) Refresh Lambda — consumes EB events, upserts vectors.
    self.refresh_fn = PythonFunction(
        self, "CatalogEmbedRefreshFn",
        entry=str(Path(__file__).parent.parent / "lambda" / "catalog_embed_refresh"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.minutes(5),
        memory_size=2048,
        reserved_concurrent_executions=10,       # burst-bounded
        dead_letter_queue=self.refresh_dlq,
        environment={
            "VECTOR_BUCKET_NAME": self.vector_bucket.attr_vector_bucket_name,
            "IDX_DB_ARN":         self.idx_db.attr_index_arn,
            "IDX_TABLE_ARN":      self.idx_table.attr_index_arn,
            "IDX_COLUMN_ARN":     self.idx_column.attr_index_arn,
            "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
            "EMBEDDING_DIM":      "1024",
        },
    )

    # Identity-side grants for the refresh Lambda.
    self.refresh_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["glue:GetDatabase", "glue:GetDatabases",
                 "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
                 "glue:GetTags"],
        resources=["*"],        # metadata reads across the catalog
    ))
    self.refresh_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["lakeformation:GetResourceLFTags",
                 "lakeformation:ListLFTags"],
        resources=["*"],
    ))
    self.refresh_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel"],
        resources=[
            f"arn:aws:bedrock:{Stack.of(self).region}::"
            f"foundation-model/amazon.titan-embed-text-v2:0",
        ],
    ))
    self.refresh_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3vectors:PutVectors", "s3vectors:DeleteVectors",
                 "s3vectors:GetVectors", "s3vectors:QueryVectors"],
        resources=[
            self.idx_db.attr_index_arn,
            self.idx_table.attr_index_arn,
            self.idx_column.attr_index_arn,
        ],
    ))
    self.refresh_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.embedding_cmk.key_arn],
    ))

    # F) EventBridge rule — Glue Catalog state changes.
    #    Note: the Glue event source has multiple detail-types; match all.
    self.rule_catalog_change = events.Rule(
        self, "RuleGlueCatalogChange",
        description="Trigger catalog-embedding refresh on any Glue DDL event.",
        event_pattern=events.EventPattern(
            source=["aws.glue"],
            detail_type=[
                "Glue Data Catalog Database State Change",
                "Glue Data Catalog Table State Change",
                "Glue Data Catalog Partition State Change",
            ],
        ),
        targets=[targets.LambdaFunction(self.refresh_fn)],
    )

    # G) Bulk-reindex Step Function — walks the entire catalog on demand.
    #    Triggered manually, by CloudWatch schedule, or by a deploy hook.
    self.bulk_fn_page = PythonFunction(
        self, "BulkReindexPageFn",
        entry=str(Path(__file__).parent.parent / "lambda" / "bulk_reindex_page"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.minutes(10),
        memory_size=2048,
        environment={
            "VECTOR_BUCKET_NAME": self.vector_bucket.attr_vector_bucket_name,
            "IDX_DB_ARN":         self.idx_db.attr_index_arn,
            "IDX_TABLE_ARN":      self.idx_table.attr_index_arn,
            "IDX_COLUMN_ARN":     self.idx_column.attr_index_arn,
            "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
            "EMBEDDING_DIM":      "1024",
        },
    )
    # Same grants as refresh_fn.
    for stmt in self.refresh_fn.role.assume_role_policy.statements:
        pass    # noop — Principal policy already shared via Lambda SP
    # Re-attach the same policies explicitly to the bulk Lambda:
    for stmt in (
        iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetDatabases",
                     "glue:GetTable",    "glue:GetTables",
                     "glue:GetPartitions", "glue:GetTags"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            actions=["lakeformation:GetResourceLFTags", "lakeformation:ListLFTags"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{Stack.of(self).region}::"
                f"foundation-model/amazon.titan-embed-text-v2:0",
            ],
        ),
        iam.PolicyStatement(
            actions=["s3vectors:PutVectors", "s3vectors:DeleteVectors",
                     "s3vectors:GetVectors", "s3vectors:QueryVectors"],
            resources=[
                self.idx_db.attr_index_arn,
                self.idx_table.attr_index_arn,
                self.idx_column.attr_index_arn,
            ],
        ),
        iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[self.embedding_cmk.key_arn],
        ),
    ):
        self.bulk_fn_page.add_to_role_policy(stmt)

    # Step Function: list_databases → map(list_tables) → map(refresh_table).
    list_dbs_task = sfn_tasks.LambdaInvoke(
        self, "ListDatabases",
        lambda_function=self.bulk_fn_page,
        payload=sfn.TaskInput.from_object({"op": "list_databases"}),
        output_path="$.Payload",
    )
    map_dbs = sfn.Map(
        self, "PerDatabase",
        items_path="$.databases",
        max_concurrency=5,        # throttle Bedrock invoke rate
        result_path="$.refreshed",
    ).item_processor(
        sfn_tasks.LambdaInvoke(
            self, "RefreshOneDatabase",
            lambda_function=self.bulk_fn_page,
            payload=sfn.TaskInput.from_object({
                "op":            "refresh_database",
                "database_name": sfn.JsonPath.string_at("$"),
            }),
            output_path="$.Payload",
        )
    )

    self.bulk_sfn = sfn.StateMachine(
        self, "BulkCatalogReindexSfn",
        definition_body=sfn.DefinitionBody.from_chainable(
            list_dbs_task.next(map_dbs)
        ),
        timeout=Duration.hours(2),
    )

    # H) Outputs — cross-stack contract.
    CfnOutput(self, "CatalogVectorBucket", value=self.vector_bucket.attr_vector_bucket_name)
    CfnOutput(self, "IdxDbArn",            value=self.idx_db.attr_index_arn)
    CfnOutput(self, "IdxTableArn",         value=self.idx_table.attr_index_arn)
    CfnOutput(self, "IdxColumnArn",        value=self.idx_column.attr_index_arn)
    CfnOutput(self, "EmbeddingCmkArn",     value=self.embedding_cmk.key_arn)
    CfnOutput(self, "BulkReindexSfnArn",   value=self.bulk_sfn.state_machine_arn)
```

### 3.3 Refresh Lambda — delta-aware upsert

```python
# lambda/catalog_embed_refresh/handler.py
import hashlib
import json
import os
from typing import Any

import boto3

VECTOR_BUCKET_NAME  = os.environ["VECTOR_BUCKET_NAME"]
IDX_DB_ARN          = os.environ["IDX_DB_ARN"]
IDX_TABLE_ARN       = os.environ["IDX_TABLE_ARN"]
IDX_COLUMN_ARN      = os.environ["IDX_COLUMN_ARN"]
EMBEDDING_MODEL_ID  = os.environ["EMBEDDING_MODEL_ID"]
EMBEDDING_DIM       = int(os.environ["EMBEDDING_DIM"])

glue    = boto3.client("glue")
lf      = boto3.client("lakeformation")
bedrock = boto3.client("bedrock-runtime")
s3v     = boto3.client("s3vectors")


# ---- sanitisers ------------------------------------------------------------

_PII_RE = {
    "email":     r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "uuid":      r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    "ssn":       r"\d{3}-\d{2}-\d{4}",
    "ccn":       r"\b(?:\d[ -]*?){13,16}\b",
}

def _sanitise(value: str) -> str:
    import re
    out = value
    for label, pat in _PII_RE.items():
        out = re.sub(pat, f"<{label.upper()}>", out)
    return out


# ---- LF-Tag helpers --------------------------------------------------------

def _get_lf_tags(resource: dict[str, Any]) -> dict[str, str]:
    """Return {tag_key: tag_value} for a Glue resource — aggregate Table and
    LFTagOnDatabase scopes. One value per tag assumed (take first)."""
    try:
        resp = lf.get_resource_lf_tags(Resource=resource)
    except lf.exceptions.EntityNotFoundException:
        return {}
    tags: dict[str, str] = {}
    for scope_key in ("LFTagOnDatabase", "LFTagsOnTable", "LFTagsOnColumns"):
        for entry in resp.get(scope_key, []) or []:
            # LFTagsOnColumns has Name+LFTags shape; others have TagKey+TagValues.
            if scope_key == "LFTagsOnColumns":
                for t in entry.get("LFTags", []):
                    tags[t["TagKey"]] = (t["TagValues"] or [""])[0]
            else:
                tags[entry["TagKey"]] = (entry["TagValues"] or [""])[0]
    return tags


# ---- embedding helpers -----------------------------------------------------

def _embed(text: str) -> list[float]:
    body = json.dumps({"inputText": text, "dimensions": EMBEDDING_DIM})
    resp = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        body=body,
        accept="application/json",
        contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


def _fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---- build embedding text --------------------------------------------------

def _db_text(db: dict) -> str:
    parts = [f"Database: {db['Name']}."]
    if db.get("Description"):
        parts.append(f"Description: {db['Description']}")
    params = db.get("Parameters") or {}
    if params:
        pstr = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
        parts.append(f"Parameters: {pstr}")
    return " ".join(parts)


def _table_text(tbl: dict) -> tuple[str, str]:
    """Return (embedding_text, columns_json_str)."""
    cols = tbl.get("StorageDescriptor", {}).get("Columns", []) or []
    col_summaries = [
        f"{c['Name']} ({c.get('Type','')}): "
        f"{_sanitise(c.get('Comment','') or '')}"
        for c in cols
    ]
    parts = [f"Table: {tbl['Name']} in database {tbl['DatabaseName']}."]
    if tbl.get("Description"):
        parts.append(f"Description: {tbl['Description']}")
    params = tbl.get("Parameters") or {}
    if params:
        # Filter out Iceberg internal metadata_location (noisy, changes each commit).
        params_clean = {
            k: v for k, v in params.items() if k != "metadata_location"
        }
        parts.append(
            f"Parameters: {', '.join(f'{k}={v}' for k,v in sorted(params_clean.items()))}"
        )
    if col_summaries:
        parts.append("Columns: " + "; ".join(col_summaries))
    return " ".join(parts), json.dumps([
        {"name": c["Name"], "type": c.get("Type", ""), "comment": c.get("Comment", "")}
        for c in cols
    ])


def _column_text(tbl: dict, col: dict) -> str:
    return (
        f"Column: {col['Name']} "
        f"(type={col.get('Type','')}) "
        f"in table {tbl['DatabaseName']}.{tbl['Name']}. "
        f"Comment: {_sanitise(col.get('Comment','') or '')}"
    )


# ---- refresh entry points -------------------------------------------------

def _refresh_database(db_name: str) -> None:
    db = glue.get_database(Name=db_name)["Database"]
    lf_tags = _get_lf_tags({"Database": {"Name": db_name}})

    text = _db_text(db)
    fp   = _fingerprint(text)
    vec  = _embed(text)
    s3v.put_vectors(
        indexArn=IDX_DB_ARN,
        vectors=[{
            "key":      db_name,
            "data":     vec,
            "metadata": {
                "database_name": db_name,
                "domain":        lf_tags.get("domain",      "unknown"),
                "environment":   lf_tags.get("environment", "unknown"),
                "source_text":   text,
                "fingerprint":   fp,
            },
        }],
    )


def _refresh_table(db_name: str, tbl_name: str) -> None:
    tbl = glue.get_table(DatabaseName=db_name, Name=tbl_name)["Table"]
    table_lf_tags = _get_lf_tags({
        "Table": {"DatabaseName": db_name, "Name": tbl_name}
    })

    text, cols_json = _table_text(tbl)
    fp   = _fingerprint(text + cols_json)
    vec  = _embed(text)
    key  = f"{db_name}.{tbl_name}"
    s3v.put_vectors(
        indexArn=IDX_TABLE_ARN,
        vectors=[{
            "key":      key,
            "data":     vec,
            "metadata": {
                "database_name": db_name,
                "table_name":    tbl_name,
                "table_type":    tbl.get("TableType", "EXTERNAL_TABLE"),
                "domain":        table_lf_tags.get("domain",      "unknown"),
                "sensitivity":   table_lf_tags.get("sensitivity", "internal"),
                "environment":   table_lf_tags.get("environment", "unknown"),
                "source_text":   text,
                "fingerprint":   fp,
                "columns_json":  cols_json,
            },
        }],
    )

    # Column vectors. Columns tagged sensitivity=pii are indexed by name +
    # comment only; sample values and inferred patterns are never embedded
    # for those.
    col_tags = _get_lf_tags({
        "TableWithColumns": {
            "DatabaseName": db_name, "Name": tbl_name,
            "ColumnNames": [c["Name"] for c in tbl.get("StorageDescriptor", {}).get("Columns", [])],
        }
    })

    col_vectors = []
    for c in tbl.get("StorageDescriptor", {}).get("Columns", []) or []:
        col_text = _column_text(tbl, c)
        col_fp   = _fingerprint(col_text)
        col_vec  = _embed(col_text)
        col_vectors.append({
            "key":      f"{db_name}.{tbl_name}.{c['Name']}",
            "data":     col_vec,
            "metadata": {
                "database_name": db_name,
                "table_name":    tbl_name,
                "table_type":    tbl.get("TableType", "EXTERNAL_TABLE"),
                "column_name":   c["Name"],
                "data_type":     c.get("Type", ""),
                "domain":        table_lf_tags.get("domain",      "unknown"),
                "sensitivity":   col_tags.get("sensitivity",
                                  table_lf_tags.get("sensitivity", "internal")),
                "environment":   table_lf_tags.get("environment", "unknown"),
                "source_text":   col_text,
                "fingerprint":   col_fp,
            },
        })
    # Batch — PutVectors supports up to 100/call.
    for i in range(0, len(col_vectors), 100):
        s3v.put_vectors(indexArn=IDX_COLUMN_ARN, vectors=col_vectors[i:i+100])


def lambda_handler(event, _ctx):
    """EventBridge event shape — 'detail' has typeOfChange + databaseName +
    tableName keys per type. Idempotent — re-running on the same event is
    safe (same key overwrites)."""
    detail = event.get("detail", {})
    db_name = detail.get("databaseName")
    tbl_name = detail.get("tableName")
    change = detail.get("typeOfChange", "")

    if change in ("CreateTable", "UpdateTable"):
        _refresh_table(db_name, tbl_name)
    elif change == "DeleteTable":
        s3v.delete_vectors(indexArn=IDX_TABLE_ARN, keys=[f"{db_name}.{tbl_name}"])
        # Column vectors for this table — best-effort pattern-delete.
        # S3 Vectors does not have a prefix-delete; retrieve via QueryVectors
        # with metadata filter, then delete by key. Elided here; see §3.6 gotchas.
    elif change in ("CreateDatabase", "UpdateDatabase"):
        _refresh_database(db_name)
    elif change == "DeleteDatabase":
        s3v.delete_vectors(indexArn=IDX_DB_ARN, keys=[db_name])
    else:
        # Partition changes don't affect catalog embeddings.
        return {"skipped": change}

    return {"refreshed": change, "database": db_name, "table": tbl_name}
```

### 3.4 Query-side — semantic 3-pass discovery

```python
# lambda/semantic_discovery/handler.py
import json
import os

import boto3

IDX_DB_ARN     = os.environ["IDX_DB_ARN"]
IDX_TABLE_ARN  = os.environ["IDX_TABLE_ARN"]
IDX_COLUMN_ARN = os.environ["IDX_COLUMN_ARN"]
EMBEDDING_MODEL_ID = os.environ["EMBEDDING_MODEL_ID"]
EMBEDDING_DIM = int(os.environ["EMBEDDING_DIM"])

bedrock = boto3.client("bedrock-runtime")
s3v     = boto3.client("s3vectors")


def _embed(text: str) -> list[float]:
    resp = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        body=json.dumps({"inputText": text, "dimensions": EMBEDDING_DIM}),
        accept="application/json", contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


def lambda_handler(event, _ctx):
    """event = {
        'question':      'revenue for renewing customers in EMEA',
        'caller_domain': 'finance',               # from JWT / Cognito claims
        'max_sensitivity': 'internal',            # deny pii+confidential by default
        'top_k_db':      5,
        'top_k_table':   15,
        'top_k_column':  30,
    }"""
    q_vec = _embed(event["question"])

    # -- Pass 1: database-level, LF-Tag filter pushdown.
    db_filter = {"domain": event["caller_domain"]}
    dbs = s3v.query_vectors(
        indexArn=IDX_DB_ARN,
        queryVector=q_vec,
        topK=event.get("top_k_db", 5),
        filter=db_filter,
        returnMetadata=True,
        returnDistance=True,
    )["matches"]
    db_names = [m["metadata"]["database_name"] for m in dbs]
    if not db_names:
        return {"error": "no accessible databases match the query"}

    # -- Pass 2: table-level, filtered by shortlisted DBs + sensitivity gate.
    allowed_sensitivities = {
        "public":       ["public"],
        "internal":     ["public", "internal"],
        "confidential": ["public", "internal", "confidential"],
        "pii":          ["public", "internal", "confidential", "pii"],
    }[event.get("max_sensitivity", "internal")]
    table_filter = {
        "database_name": {"$in": db_names},
        "sensitivity":   {"$in": allowed_sensitivities},
    }
    tables = s3v.query_vectors(
        indexArn=IDX_TABLE_ARN,
        queryVector=q_vec,
        topK=event.get("top_k_table", 15),
        filter=table_filter,
        returnMetadata=True,
        returnDistance=True,
    )["matches"]
    # Shortlist — top 5 tables.
    table_keys = [m["metadata"]["table_name"] for m in tables[:5]]
    db_for_tables = {m["metadata"]["table_name"]: m["metadata"]["database_name"] for m in tables}

    # -- Pass 3: column-level, filtered to shortlisted tables.
    column_filter = {
        "table_name":  {"$in": table_keys},
        "sensitivity": {"$in": allowed_sensitivities},
    }
    columns = s3v.query_vectors(
        indexArn=IDX_COLUMN_ARN,
        queryVector=q_vec,
        topK=event.get("top_k_column", 30),
        filter=column_filter,
        returnMetadata=True,
        returnDistance=True,
    )["matches"]

    return {
        "databases": [
            {"name": m["metadata"]["database_name"],
             "distance": m["distance"],
             "description": m["metadata"]["source_text"]}
            for m in dbs
        ],
        "tables": [
            {"database":   m["metadata"]["database_name"],
             "name":       m["metadata"]["table_name"],
             "table_type": m["metadata"]["table_type"],
             "distance":   m["distance"],
             "description": m["metadata"]["source_text"],
             "columns_json": m["metadata"].get("columns_json")}
            for m in tables
        ],
        "columns": [
            {"database":  m["metadata"]["database_name"],
             "table":     m["metadata"]["table_name"],
             "column":    m["metadata"]["column_name"],
             "data_type": m["metadata"]["data_type"],
             "distance":  m["distance"],
             "description": m["metadata"]["source_text"]}
            for m in columns
        ],
    }
```

### 3.5 Bulk-reindex page Lambda

```python
# lambda/bulk_reindex_page/handler.py
import json
import os
import boto3

glue = boto3.client("glue")
# (vars and client setup identical to refresh Lambda — elided)


def _list_databases_paginated() -> list[str]:
    out = []
    paginator = glue.get_paginator("get_databases")
    for page in paginator.paginate():
        out.extend(d["Name"] for d in page["DatabaseList"])
    return out


def lambda_handler(event, _ctx):
    op = event["op"]
    if op == "list_databases":
        return {"databases": _list_databases_paginated()}
    if op == "refresh_database":
        db_name = event["database_name"]
        # 1) Refresh database vector.
        from catalog_embed_refresh.handler import _refresh_database, _refresh_table
        _refresh_database(db_name)
        # 2) Refresh every table in the database.
        paginator = glue.get_paginator("get_tables")
        for page in paginator.paginate(DatabaseName=db_name):
            for t in page["TableList"]:
                _refresh_table(db_name, t["Name"])
        return {"database": db_name, "status": "ok"}
    raise ValueError(f"unknown op {op}")
```

### 3.6 Monolith gotchas

1. **`s3vectors:PutVectors` is strictly idempotent by `key`.** Re-issuing with the same key REPLACES the old vector. This is what we want for refresh, but it means a "refresh one table" event must include ALL columns — a delta (only-changed columns) is NOT meaningful because missing keys are not deleted, only explicit `delete_vectors` removes them. Solution: when a column is dropped from the table schema, the refresh must compute the DIFF between old and new column keys and issue a `delete_vectors` for the removed set.
2. **Delete-by-prefix does not exist.** To delete all column vectors of a dropped table, you must FIRST `query_vectors` with a metadata filter `{"table_name": dropped_table}` to harvest keys, THEN `delete_vectors` with those keys. For tables with 100+ columns this is 2 API calls; for tables with 10000+ columns, paginate. Alternatively, prefix the vector key with the table name and query-before-delete is mandatory.
3. **`ColumnComment` is 255 chars max in Glue.** Fat embeddings (column with a multi-sentence rationale) lose the tail. Mitigation: put the long form in Glue `TableInput.Parameters` as a custom key `col__customer_id__long_comment=...`, and have the refresh Lambda concatenate `{comment} {params.get('col__<name>__long_comment', '')}` before embedding.
4. **Bedrock embedding rate-limit is the bottleneck during bulk reindex.** Titan embed v2 has an account-wide rate limit (~1000 rpm per region by default). A bulk reindex of 10k tables × 10 columns each = 110k invokes; spread across a 2-hour SFN with `max_concurrency=5` to stay under 500 rpm.
5. **LF-Tag resolution adds 1-2 Glue/LF API calls per table.** For a 10k-table catalog, that's 10-20k `lakeformation:GetResourceLFTags` calls per bulk reindex. Cache by `(database, table)` for 10-minute TTL in-memory, or bulk-fetch via `ListLFTags` + local dict at job start.
6. **Fingerprint-based skip is mandatory in production.** Without it, every Glue `UpdateTable` (which fires on partition adds, stats updates, etc.) triggers full re-embed. Compute SHA256 over the embedding text + store as filterable metadata `fingerprint`; in `_refresh_table`, compare to the existing vector's fingerprint via `get_vectors` — skip if match.
7. **Query-side `{"$in": [...]}` filter is limited to ~100 values.** For the pass-2 → pass-3 shortlist, cap at 100 tables or use a different indexing strategy (shard by database).
8. **Vector version drift on dimension change is catastrophic.** Changing `dimension=1024` to `dimension=512` requires a new index (immutable) and a full reindex — the old index is stale during the transition. Plan the dimension carefully; 1024 is the default for new work, 256 is fine for smaller catalogs (< 1000 tables) and saves ~75% storage.

---

## 4. Micro-Stack Variant

**Use when:** the catalog embedding index is a shared horizontal consumed by multiple agent stacks, chatbot stacks, discovery UIs.

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** on the refresh Lambda + bulk-reindex page Lambda entries.
2. **Identity-side grants** — consumer Lambdas grant themselves `s3vectors:QueryVectors` on SSM-read index ARNs + `bedrock:InvokeModel` for the embedding model; NEVER modify the vector-bucket resource policy (there isn't one — only identity-side exists) or reach into CatalogEmbeddingStack to attach policies.
3. **`CfnRule` cross-stack EventBridge** — the Glue Catalog Change rule lives in CatalogEmbeddingStack; target (refresh Lambda) is same-stack.
4. **Same-stack bucket + OAC** — N/A.
5. **KMS ARNs as strings** — the `EmbeddingCmk.key_arn` is SSM-published. Consumers read as string, grant `kms:Decrypt` on it.

### 4.2 CatalogEmbeddingStack — the producer

```python
# stacks/catalog_embedding_stack.py
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_s3vectors as s3v,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct


class CatalogEmbeddingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # --- A) CMK
        cmk = kms.Key(
            self, "EmbeddingCmk",
            alias=f"alias/{{project_name}}-catalog-embed-{stage}",
            enable_key_rotation=True,
            removal_policy=(
                RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
            ),
        )

        # --- B) Vector bucket + 3 indexes (see §3.2 for full schemas)
        vb = s3v.CfnVectorBucket(
            self, "CatalogVectorBucket",
            vector_bucket_name=f"{{project_name}}-catalog-vectors-{stage}",
            encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="aws:kms", kms_key_arn=cmk.key_arn,
            ),
        )
        idx_db     = self._idx(vb, "IdxDbLevel",     "catalog-db-level",     level="db")
        idx_table  = self._idx(vb, "IdxTableLevel",  "catalog-table-level",  level="table")
        idx_column = self._idx(vb, "IdxColumnLevel", "catalog-column-level", level="column")

        # --- C) Refresh Lambda
        dlq = sqs.Queue(
            self, "RefreshDlq",
            encryption=sqs.QueueEncryption.KMS_MANAGED,
            retention_period=Duration.days(14),
        )
        refresh_fn = PythonFunction(
            self, "CatalogEmbedRefreshFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "catalog_embed_refresh"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(5),
            memory_size=2048,
            reserved_concurrent_executions=10,
            dead_letter_queue=dlq,
            environment={
                "VECTOR_BUCKET_NAME": vb.attr_vector_bucket_name,
                "IDX_DB_ARN":         idx_db.attr_index_arn,
                "IDX_TABLE_ARN":      idx_table.attr_index_arn,
                "IDX_COLUMN_ARN":     idx_column.attr_index_arn,
                "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
                "EMBEDDING_DIM":      "1024",
            },
        )
        self._grant(refresh_fn.role, index_arns=[
            idx_db.attr_index_arn, idx_table.attr_index_arn, idx_column.attr_index_arn,
        ], cmk_arn=cmk.key_arn)

        # --- D) EB rule (producer-side)
        events.Rule(
            self, "RuleGlueCatalogChange",
            description="Glue catalog DDL → catalog embedding refresh",
            event_pattern=events.EventPattern(
                source=["aws.glue"],
                detail_type=[
                    "Glue Data Catalog Database State Change",
                    "Glue Data Catalog Table State Change",
                ],
            ),
            targets=[targets.LambdaFunction(refresh_fn)],
        )

        # --- E) Publish cross-stack contract
        ssm.StringParameter(self, "VectorBucketNameParam",
            parameter_name=f"/{{project_name}}/{stage}/catalog_embed/vector_bucket_name",
            string_value=vb.attr_vector_bucket_name,
        )
        for name, arn in (
            ("idx_db_arn",     idx_db.attr_index_arn),
            ("idx_table_arn",  idx_table.attr_index_arn),
            ("idx_column_arn", idx_column.attr_index_arn),
            ("cmk_arn",        cmk.key_arn),
        ):
            ssm.StringParameter(
                self, f"Param{name.title().replace('_','')}",
                parameter_name=f"/{{project_name}}/{stage}/catalog_embed/{name}",
                string_value=arn,
            )

        CfnOutput(self, "IdxTableArn",  value=idx_table.attr_index_arn)
        CfnOutput(self, "IdxColumnArn", value=idx_column.attr_index_arn)

    # ---- helpers ----------------------------------------------------------

    def _idx(
        self, vb: s3v.CfnVectorBucket, logical_id: str,
        index_name: str, *, level: str,
    ) -> s3v.CfnIndex:
        filter_keys = {
            "db": [
                ("database_name", "TEXT"),
                ("domain",        "TEXT"),
                ("environment",   "TEXT"),
            ],
            "table": [
                ("database_name", "TEXT"), ("domain",      "TEXT"),
                ("environment",   "TEXT"), ("table_name",  "TEXT"),
                ("table_type",    "TEXT"), ("sensitivity", "TEXT"),
            ],
            "column": [
                ("database_name", "TEXT"), ("domain",       "TEXT"),
                ("environment",   "TEXT"), ("table_name",   "TEXT"),
                ("table_type",    "TEXT"), ("sensitivity",  "TEXT"),
                ("column_name",   "TEXT"), ("data_type",    "TEXT"),
            ],
        }[level]
        non_filterable = ["source_text", "fingerprint"]
        if level == "table":
            non_filterable.append("columns_json")
        idx = s3v.CfnIndex(
            self, logical_id,
            vector_bucket_name=vb.attr_vector_bucket_name,
            index_name=index_name,
            data_type="float32",
            dimension=1024,
            distance_metric="cosine",
            metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=non_filterable,
            ),
            filterable_metadata_keys=[
                s3v.CfnIndex.MetadataKeyProperty(name=k, type=t)
                for k, t in filter_keys
            ],
        )
        idx.add_dependency(vb)
        return idx

    def _grant(self, role: iam.IRole, *, index_arns: list[str], cmk_arn: str) -> None:
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetDatabases",
                     "glue:GetTable", "glue:GetTables", "glue:GetTags"],
            resources=["*"],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["lakeformation:GetResourceLFTags", "lakeformation:ListLFTags"],
            resources=["*"],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::"
                f"foundation-model/amazon.titan-embed-text-v2:0",
            ],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["s3vectors:PutVectors", "s3vectors:DeleteVectors",
                     "s3vectors:GetVectors", "s3vectors:QueryVectors"],
            resources=index_arns,
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[cmk_arn],
        ))
```

### 4.3 Consumer pattern — agent Lambda queries catalog embeddings

```python
# stacks/agent_stack.py — a query-side consumer.
from pathlib import Path
from aws_cdk import (
    Duration, Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction


class AgentStack(Stack):
    def __init__(self, scope, construct_id, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) Resolve CatalogEmbeddingStack contract via SSM.
        idx_db_arn     = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_db_arn"
        )
        idx_table_arn  = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_table_arn"
        )
        idx_column_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/idx_column_arn"
        )
        cmk_arn        = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog_embed/cmk_arn"
        )

        # B) Semantic discovery Lambda.
        discovery_fn = PythonFunction(
            self, "SemanticDiscoveryFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "semantic_discovery"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.seconds(30),
            memory_size=1024,
            environment={
                "IDX_DB_ARN":        idx_db_arn,
                "IDX_TABLE_ARN":     idx_table_arn,
                "IDX_COLUMN_ARN":    idx_column_arn,
                "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
                "EMBEDDING_DIM":      "1024",
            },
        )

        # C) Identity-side grants — query-only.
        discovery_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
            resources=[idx_db_arn, idx_table_arn, idx_column_arn],
        ))
        discovery_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::"
                f"foundation-model/amazon.titan-embed-text-v2:0",
            ],
        ))
        discovery_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[cmk_arn],
        ))
```

### 4.4 Micro-stack gotchas

- **Consumer sees a STALE index during bulk reindex.** A bulk-reindex run may take 1-2 hours; during that time, partial results are visible. Agents tolerating stale metadata (deep-research, eventual-consistency workflows) are fine; transactional UIs should show a "reindexing" banner based on the SFN status.
- **No `s3vectors:ListIndexes` needed for consumers.** Consumers know the 3 index ARNs via SSM; they never enumerate.
- **`QueryVectors` cost is per-call, not per-result.** topK=100 costs the same as topK=5. Prefer larger topK + post-rerank in-memory (cheaper than multiple queries).
- **Deletion order**: CatalogEmbeddingStack → AgentStack (deploy); AgentStack → CatalogEmbeddingStack (delete).

---

## 5. Swap matrix — when to replace or supplement

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Vector store | S3 Vectors (this) | OpenSearch Serverless k-NN | Hybrid BM25 + vector search for keyword+meaning fusion. Pair, don't replace — mirror from S3 Vectors via nightly export. See `DATA_OPENSEARCH_KNN` (if added). |
| Vector store | S3 Vectors | Aurora Postgres + pgvector | Existing Aurora footprint; but pgvector's ANN is weaker for > 1M vectors. Avoid for large catalogs. |
| Embedding model | Titan v2 1024-dim | Cohere Embed v3 Multilingual | Cross-language catalog (mixed English + French + German descriptions). Titan v2 is English-biased. |
| Embedding model | Titan v2 1024-dim | Titan v2 256-dim | Smaller catalog (< 1000 tables); saves 75% storage. Loses ~5-10% recall. |
| Level count | 3 (db + table + column) | 2 (table + column) | Small catalog with < 5 databases — database-level embedding is redundant. |
| Level count | 3 | 4 (db + table + column + sample-value) | Value-level search ("find tables with 'AOVU' currency code") — but heavy PII risk. Opt-in, LF-gated. |
| Refresh trigger | EB `Glue Catalog State Change` | Polling via Glue API every hour | EB is near-realtime; polling handles cases where EB events are dropped (rare) or catalog federation events don't fire. Pair, don't replace. |
| LF-Tag resolution | Per-table at refresh time | Pre-computed table → tags dict, refreshed hourly | For 10k+ table catalogs — saves 10-20k LF API calls per bulk reindex. |
| Query flow | 3-pass (db → table → column) | 1-pass (column only) + join back to table | Faster for short questions ("customer_id"). 3-pass is better for long natural-language questions. |
| Result bucket for Athena | Separate from catalog embeddings | Shared with other result artifacts | No benefit to sharing; catalog embeddings have different lifecycle and KMS. Keep separate. |

---

## 6. Worked example — offline synth + round-trip integration

```python
# tests/test_catalog_embedding_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.catalog_embedding_stack import CatalogEmbeddingStack


def test_synth_three_indexes_and_refresh_lambda():
    app = cdk.App()
    stack = CatalogEmbeddingStack(app, "CatEmbed-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # 3 S3 Vectors indexes, all cosine + 1024 dim.
    tpl.resource_count_is("AWS::S3Vectors::Index", 3)
    tpl.has_resource_properties("AWS::S3Vectors::Index", {
        "IndexName":      "catalog-table-level",
        "Dimension":      1024,
        "DistanceMetric": "cosine",
        "DataType":       "float32",
    })
    tpl.has_resource_properties("AWS::S3Vectors::Index", {
        "IndexName":  "catalog-column-level",
        "FilterableMetadataKeys": Match.array_with([
            Match.object_like({"Name": "column_name"}),
            Match.object_like({"Name": "sensitivity"}),
        ]),
    })

    # Refresh Lambda has the correct env + DLQ wired.
    tpl.has_resource_properties("AWS::Lambda::Function", {
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
                "EMBEDDING_DIM":      "1024",
            }),
        }),
        "DeadLetterConfig": Match.any_value(),
        "ReservedConcurrentExecutions": 10,
    })

    # EB rule listens for both Database + Table state changes.
    tpl.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": Match.object_like({
            "source":      ["aws.glue"],
            "detail-type": Match.array_with([
                "Glue Data Catalog Table State Change",
                "Glue Data Catalog Database State Change",
            ]),
        }),
    })

    # SSM contract published.
    for suffix in ("idx_db_arn", "idx_table_arn", "idx_column_arn",
                   "cmk_arn", "vector_bucket_name"):
        tpl.has_resource_properties("AWS::SSM::Parameter", {
            "Name": f"/{{project_name}}/dev/catalog_embed/{suffix}",
        })


# tests/test_integration_discovery.py
"""Integration: refresh a test table, query embeddings, assert top hit."""
import json, os, time
import pytest
import boto3


@pytest.mark.integration
def test_refresh_and_discover_fact_revenue():
    glue = boto3.client("glue")
    bedrock = boto3.client("bedrock-runtime")
    s3v = boto3.client("s3vectors")

    # 1) Make sure a test table is catalogued (assumed fixture).
    db, tbl = "lakehouse_dev", "fact_revenue"
    glue.get_table(DatabaseName=db, Name=tbl)        # fails if not present

    # 2) Kick the bulk reindex SFN for this one database.
    sfn = boto3.client("stepfunctions")
    sfn_arn = os.environ["BULK_SFN_ARN"]
    exec_arn = sfn.start_execution(
        stateMachineArn=sfn_arn,
        input=json.dumps({"single_database": db}),
    )["executionArn"]
    # Poll for completion (abbreviated).
    for _ in range(120):
        st = sfn.describe_execution(executionArn=exec_arn)["status"]
        if st in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            break
        time.sleep(5)
    assert st == "SUCCEEDED"

    # 3) Discovery query — expect fact_revenue near the top.
    body = json.dumps({"inputText": "customer revenue by quarter",
                       "dimensions": 1024})
    vec = json.loads(bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=body, accept="application/json", contentType="application/json",
    )["body"].read())["embedding"]

    matches = s3v.query_vectors(
        indexArn=os.environ["IDX_TABLE_ARN"],
        queryVector=vec,
        topK=5,
        filter={"database_name": db},
        returnMetadata=True, returnDistance=True,
    )["matches"]

    assert any(m["metadata"]["table_name"] == "fact_revenue" for m in matches[:3])
```

Run `pytest tests/test_catalog_embedding_synth.py -v` offline to validate the CDK shape; `pytest tests/test_integration_discovery.py -v -m integration` after deploy.

---

## 7. References

- `DATA_GLUE_CATALOG.md` — source of truth; comments + descriptions + parameters are the embedding substrate.
- `DATA_S3_VECTORS.md` — underlying vector storage primitive.
- `DATA_LAKE_FORMATION.md` — LF-Tag propagation source (domain, sensitivity).
- AWS docs — *Titan Text Embeddings v2* (1024 / 512 / 256 dimensions, normalisation).
- AWS docs — *Glue Data Catalog state-change events* on EventBridge.
- `PATTERN_TEXT_TO_SQL.md` (Wave 3) — the primary consumer of this index.
- `PATTERN_ENTERPRISE_CHAT_ROUTER.md` (Wave 3) — uses the 3-pass flow to decide SQL vs RAG routing.
- `PATTERN_SEMANTIC_DATA_DISCOVERY.md` (Wave 3) — wraps this pattern behind a "find my data" API.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Three-level index (db / table / column) with LF-Tag-aware filterable metadata. Fingerprint-diff idempotent refresh. 3-pass semantic discovery query flow. PII-sanitised source_text. 8 monolith gotchas, 4 micro-stack gotchas, 9-row swap matrix, pytest synth + SFN-driven integration harness.
