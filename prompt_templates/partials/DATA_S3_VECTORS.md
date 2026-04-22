# SOP — Amazon S3 Vectors (cost-optimized vector store for RAG)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `s3vectors` service namespace (GA 2025) · boto3 `s3vectors` client · CloudFormation L1 (`AWS::S3Vectors::VectorBucket`, `AWS::S3Vectors::Index`) · Titan Text Embeddings v2 (1024 / 512 / 256 dims) · KMS SSE-KMS · 14-region GA footprint

---

## 1. Purpose

- Provide a deep-dive for **Amazon S3 Vectors** — a purpose-built, durable vector store with a separate service namespace (`s3vectors`) optimised for **less frequent queries** (sub-second for infrequent, ~100 ms for frequent) at a fraction of in-memory vector DB cost.
- Codify the **vector bucket → vector index → `PutVectors` / `QueryVectors`** data plane, plus the **CloudFormation L1 / CDK L1** control plane (no L2 constructs exist yet).
- Codify the **non-filterable metadata = `source_text`** idiom (chunk source text stored alongside embedding for one-hop retrieval) and the **filterable metadata = query attributes** idiom (`doc_id`, `page`, `section`, `access_group`).
- Codify the **immutability contract**: `IndexName`, `Dimension`, `DistanceMetric`, and `NonFilterableMetadataKeys` cannot be changed after create — recreating requires deleting the index. Make this a first-class design concern.
- Codify the **hot-path swap** — S3 Vectors as cost-optimised primary + scheduled snapshot export to OpenSearch Serverless for high-QPS paths. And the **Bedrock Knowledge Base** managed-ingest alternative.
- Include when the SOW signals: "RAG", "semantic search", "vector DB", "embeddings store", "knowledge base", "doc retrieval", "chatbot over documents", "cost-optimised vector store", "infrequent vector queries".
- This partial is the **S3 Vectors specialisation** for RAG workloads. `PATTERN_DOC_INGESTION_RAG` covers the document → chunk → embed pipeline and references this partial for storage details — do not duplicate.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the vector bucket, indexes, and the embedding / query Lambdas | **§3 Monolith Variant** |
| `VectorStoreStack` owns the vector bucket + indexes + local CMK; `ComputeStack` owns the ingestion + query Lambdas | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **No CDK L2 exists.** `aws_cdk.aws_s3vectors.CfnVectorBucket` / `CfnIndex` are the ONLY CDK resources (as of v2.238). There is no `grant_read`, no `grant_write`, no helper — everything is identity-side `PolicyStatement` with `s3vectors:*` actions on the ARN. This makes cross-stack grant cycles structurally impossible but also means **you must hand-write every grant** and **get the ARN shape exactly right**.
2. **Vector bucket ARN is NOT an `arn:aws:s3:::` ARN.** It is `arn:aws:s3vectors:{region}:{account}:bucket/{bucket-name}`. Attempting to reuse S3 bucket IAM patterns silently fails at runtime (`AccessDeniedException`, not an obvious synth error).
3. **`EncryptionConfiguration` is immutable** on both bucket and index. If the bucket is in `VectorStoreStack` using a local CMK and the ingestion role is in `ComputeStack`, the role still needs `kms:GenerateDataKey` on that CMK ARN — publish the CMK ARN via SSM, grant identity-side.
4. **`Ref` returns the bucket ARN** (not the name) on `AWS::S3Vectors::VectorBucket` — subtle inversion of the S3 pattern where `Ref` returns the name. Always grab `.attr_vector_bucket_arn` / `.attr_vector_bucket_name` explicitly; never rely on `.ref`.
5. **Per-index immutability forces deployment discipline.** `Dimension: 1024` is baked in — switching to Titan v2 512-dim variant means a new index resource, a new logical ID, a replacement, and a one-shot reindex. Micro-stack scopes the blast radius to `VectorStoreStack` only.

Micro-Stack variant fixes all of this by: (a) owning the vector bucket + CMK + all indexes in `VectorStoreStack`; (b) publishing `VectorBucketName`, `VectorBucketArn`, `IndexArn` (per index), and `KmsArn` via SSM; (c) consumer Lambdas grant themselves `s3vectors:PutVectors` / `QueryVectors` / `GetVectors` on specific index ARNs — identity-side only.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Ingestion Fn ──► s3vectors.PutVectors (batched, 1 call / 100 vectors)
                        │
                        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  VectorBucket: {project_name}-vectors-{stage}            │
  │    EncryptionConfiguration: SseType=aws:kms + local CMK  │
  │    BlockPublicAccess: always-on (non-configurable)       │
  │                                                          │
  │  Index: rag-main                                         │
  │    DataType:        float32  (only option)               │
  │    Dimension:       1024    (Titan v2 default)           │
  │    DistanceMetric:  cosine  (or "euclidean")             │
  │    NonFilterableMetadataKeys: ["source_text"]            │
  │                                                          │
  │    Filterable metadata (auto-indexed):                   │
  │      doc_id:TEXT, page:NUMBER, section:TEXT,             │
  │      access_group:TEXT, uploaded_at:NUMBER (epoch)       │
  │                                                          │
  │  (Optional: Index: rag-eval — smaller, for eval corpus)  │
  └──────────────────────────────────────────────────────────┘
                        ▲
                        │  s3vectors.QueryVectors
                        │    topK=5, filter={"doc_id": "..."},
                        │    returnDistance=True, returnMetadata=True
  Query Fn  ────────────┘
```

### 3.2 CDK — `_create_s3_vectors()` method body

```python
from aws_cdk import (
    CfnOutput, RemovalPolicy, Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3vectors as s3v,                  # CDK L1 — v2.238+
)


def _create_s3_vectors(self, stage: str) -> None:
    """Monolith variant. Assumes self.{kms_key} exists (or create a local CMK
    below). Provisions one vector bucket + two indexes (main + eval).

    Only L1 constructs are available; there are no L2 grants — every IAM
    statement is hand-written against the exact ARN shape.
    """

    # A) Vector bucket.
    #    Ref of this resource is the bucket ARN, not the name:
    #      arn:aws:s3vectors:{region}:{account}:bucket/{bucket-name}
    #    `.attr_vector_bucket_arn` / `.attr_vector_bucket_name` are the
    #    canonical accessors; use them, not `.ref`.
    self.vector_bucket = s3v.CfnVectorBucket(
        self, "VectorBucket",
        vector_bucket_name=f"{{project_name}}-vectors-{stage}",   # 3-63 lowercase
        encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
            sse_type="aws:kms",
            kms_key_arn=self.kms_key.key_arn,
        ),
        tags=[{"key": "Project", "value": "{project_name}"},
              {"key": "Stage",   "value": stage}],
    )
    # BlockPublicAccess is always-on on vector buckets — cannot be disabled.
    # No `block_public_access` property exists on the L1 at all.

    # B) Main RAG index — Titan Text Embeddings v2 default = 1024 dims, cosine.
    #    NonFilterableMetadataKeys are IMMUTABLE after create — cannot add,
    #    remove, or reorder. Pick carefully. "source_text" is the canonical
    #    key that holds the chunk's raw text for one-hop retrieval after
    #    QueryVectors. All other metadata keys are filterable by default.
    self.index_main = s3v.CfnIndex(
        self, "RagMainIndex",
        vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn,
        index_name="rag-main",                      # 3-63 lowercase + dots
        data_type="float32",                        # only allowed value
        dimension=1024,                             # Titan v2 default
        distance_metric="cosine",                   # or "euclidean"
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_text"],
        ),
        # encryption_configuration omitted -> inherits from bucket
    )
    # Explicit DependsOn. The L1 wires this via Ref chaining, but being
    # explicit avoids the rare case where CFN orders the index before the
    # bucket's encryption is fully applied.
    self.index_main.add_dependency(self.vector_bucket)

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

    # C) Optional eval index — smaller corpus, Titan v2 reduced-dim (512).
    #    Useful for CI regression tests on retrieval quality.
    self.index_eval = s3v.CfnIndex(
        self, "RagEvalIndex",
        vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn,
        index_name="rag-eval",
        data_type="float32",
        dimension=512,
        distance_metric="cosine",
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_text"],
        ),
    )
    self.index_eval.add_dependency(self.vector_bucket)

    eval_index_arn = Stack.of(self).format_arn(
        service="s3vectors",
        resource="bucket",
        resource_name=(
            f"{self.vector_bucket.vector_bucket_name}/index/{self.index_eval.index_name}"
        ),
    )

    # Expose for consumer stacks / downstream code.
    self.vector_bucket_arn = self.vector_bucket.attr_vector_bucket_arn
    self.vector_bucket_name = self.vector_bucket.vector_bucket_name
    self.index_main_arn = main_index_arn
    self.index_eval_arn = eval_index_arn

    CfnOutput(self, "VectorBucketName", value=self.vector_bucket_name)
    CfnOutput(self, "VectorBucketArn",  value=self.vector_bucket_arn)
    CfnOutput(self, "RagMainIndexArn",  value=main_index_arn)
    CfnOutput(self, "RagEvalIndexArn",  value=eval_index_arn)


def _grant_s3_vectors_write(self, fn, index_arns: list[str]) -> None:
    """Grant a Lambda function write (+ read) on specific index ARNs.
    Hand-written because there is no L2 grant_* helper."""
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "s3vectors:PutVectors",
            "s3vectors:GetVectors",
            "s3vectors:ListVectors",
            "s3vectors:DeleteVectors",
            "s3vectors:QueryVectors",
            "s3vectors:GetIndex",           # describe the index schema
        ],
        resources=index_arns,
    ))
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "kms:Decrypt",
            "kms:GenerateDataKey",
            "kms:DescribeKey",
        ],
        resources=[self.kms_key.key_arn],
    ))


def _grant_s3_vectors_read(self, fn, index_arns: list[str]) -> None:
    """Grant a Lambda function read-only on specific index ARNs."""
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "s3vectors:QueryVectors",
            "s3vectors:GetVectors",
            "s3vectors:ListVectors",
            "s3vectors:GetIndex",
        ],
        resources=index_arns,
    ))
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.kms_key.key_arn],
    ))
```

### 3.3 Ingestion handler — `lambda/vector_writer/index.py`

```python
"""S3 Vectors batched PutVectors writer.

Called by the chunk-embed pipeline (see PATTERN_DOC_INGESTION_RAG). Takes a
list of {key, embedding, metadata} records and writes them in batches of
<=100 vectors per PutVectors call (AWS best practice — minimize round trips).

Input event shape:
{
  "doc_id": "doc-123",
  "records": [
    {"chunk_index": 0,
     "embedding": [0.1, 0.2, ...],                # 1024 floats
     "source_text": "The quick brown fox ...",
     "page": 1,
     "section": "intro",
     "access_group": "public"},
    ...
  ]
}
"""
import hashlib
import json
import logging
import os
import time

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The s3vectors client is separate from the s3 client — different namespace.
s3v = boto3.client(
    "s3vectors",
    region_name=os.environ.get("AWS_REGION", "us-west-2"),
    config=Config(retries={"max_attempts": 5, "mode": "standard"}),
)

BUCKET_NAME   = os.environ["VECTOR_BUCKET_NAME"]
INDEX_NAME    = os.environ["VECTOR_INDEX_NAME"]         # e.g. "rag-main"
BATCH_SIZE    = int(os.environ.get("PUT_VECTORS_BATCH_SIZE", "100"))


def lambda_handler(event, _ctx):
    doc_id  = event["doc_id"]
    records = event["records"]

    if not records:
        return {"doc_id": doc_id, "written": 0}

    vectors = [_to_put_record(doc_id, r) for r in records]

    written = 0
    for batch in _chunked(vectors, BATCH_SIZE):
        # s3vectors.put_vectors — note parameter casing: vectorBucketName,
        # indexName, vectors (lowerCamelCase, despite Python conventions).
        s3v.put_vectors(
            vectorBucketName=BUCKET_NAME,
            indexName=INDEX_NAME,
            vectors=batch,
        )
        written += len(batch)
        logger.info("put_vectors doc=%s batch_size=%d cumulative=%d",
                    doc_id, len(batch), written)

    return {"doc_id": doc_id, "written": written, "index": INDEX_NAME}


def _to_put_record(doc_id: str, r: dict) -> dict:
    """Build the PutVectors record.

    - key: deterministic — sha256(doc_id + chunk_index). Idempotent re-runs
      overwrite the same vector rather than duplicating.
    - data.float32: the raw embedding.
    - metadata.source_text: the chunk's raw text (NON-filterable,
      retrieved alongside QueryVectors via returnMetadata=True).
    - metadata.<other>: FILTERABLE — queryable in filter={"doc_id": "..."}.
      Supported types: string, number, boolean, list.
    """
    chunk_index = r["chunk_index"]
    key = _deterministic_key(doc_id, chunk_index)

    metadata = {
        "source_text":  r["source_text"],     # non-filterable (see index schema)
        "doc_id":       doc_id,
        "chunk_index":  chunk_index,
        "page":         int(r.get("page", 0)),
        "section":      str(r.get("section", "")),
        "access_group": str(r.get("access_group", "public")),
        "uploaded_at":  int(r.get("uploaded_at", time.time())),
    }
    # Strip empty-string filterable values (they still index but add noise).
    metadata = {k: v for k, v in metadata.items() if v != ""}

    return {
        "key":      key,
        "data":     {"float32": r["embedding"]},
        "metadata": metadata,
    }


def _deterministic_key(doc_id: str, chunk_index: int) -> str:
    """Idempotent vector key. Re-ingesting the same chunk overwrites."""
    h = hashlib.sha256(f"{doc_id}#{chunk_index}".encode("utf-8")).hexdigest()
    return f"{doc_id}#{chunk_index}#{h[:12]}"


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
```

### 3.4 Query handler — `lambda/vector_query/index.py`

```python
"""S3 Vectors QueryVectors handler.

Event shape (from API GW or direct invoke):
{
  "query_embedding": [0.1, 0.2, ...],       # 1024 floats, Titan v2
  "top_k": 5,
  "filter": {"doc_id": "doc-123"},          # optional; filterable metadata
  "return_distance": true,
  "return_metadata": true
}

Returns:
{
  "hits": [
    {"key": "...", "distance": 0.12, "source_text": "...",
     "doc_id": "...", "page": 3, "section": "intro"},
    ...
  ]
}
"""
import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3v = boto3.client(
    "s3vectors",
    region_name=os.environ.get("AWS_REGION", "us-west-2"),
    config=Config(retries={"max_attempts": 3, "mode": "standard"}),
)

BUCKET_NAME = os.environ["VECTOR_BUCKET_NAME"]
INDEX_NAME  = os.environ["VECTOR_INDEX_NAME"]
DEFAULT_TOP_K = int(os.environ.get("DEFAULT_TOP_K", "5"))


def lambda_handler(event, _ctx):
    body = event if isinstance(event, dict) and "query_embedding" in event \
           else json.loads(event.get("body", "{}"))

    query_vec = body["query_embedding"]
    top_k     = int(body.get("top_k", DEFAULT_TOP_K))
    flt       = body.get("filter")

    kwargs = dict(
        vectorBucketName=BUCKET_NAME,
        indexName=INDEX_NAME,
        queryVector={"float32": query_vec},
        topK=top_k,
        returnDistance=bool(body.get("return_distance", True)),
        returnMetadata=bool(body.get("return_metadata", True)),
    )
    # `filter` keys must be FILTERABLE metadata keys (i.e. NOT in the
    # nonFilterableMetadataKeys list on the index). Passing a non-filterable
    # key here fails with ValidationException at query time.
    if flt:
        kwargs["filter"] = flt

    resp = s3v.query_vectors(**kwargs)

    hits = []
    for v in resp.get("vectors", []) or []:
        hit = {"key": v["key"]}
        if "distance" in v:
            hit["distance"] = float(v["distance"])
        md = v.get("metadata") or {}
        # Promote well-known metadata fields to top level for client ease.
        for k in ("source_text", "doc_id", "page", "section", "access_group"):
            if k in md:
                hit[k] = md[k]
        hits.append(hit)

    logger.info("query_vectors index=%s top_k=%d hits=%d filter=%s",
                INDEX_NAME, top_k, len(hits), flt)

    return {
        "statusCode": 200,
        "body": json.dumps({"hits": hits}, default=str),
    }
```

### 3.5 Dev-loop aid — `s3vectors-embed-cli`

```bash
# AWS's official CLI that bundles Bedrock embedding + PutVectors in one
# command. Useful for prototyping before the CDK pipeline is wired.
# https://github.com/awslabs/s3vectors-embed-cli
#
# Install:
pip install s3vectors-embed-cli

# Embed + index a local file in one shot (Titan v2, 1024 dims):
s3vectors-embed-cli put \
    --vector-bucket-name {project_name}-vectors-dev \
    --index-name rag-main \
    --embedding-model amazon.titan-embed-text-v2:0 \
    --region us-west-2 \
    --file ./docs/my-document.pdf

# Query:
s3vectors-embed-cli query \
    --vector-bucket-name {project_name}-vectors-dev \
    --index-name rag-main \
    --embedding-model amazon.titan-embed-text-v2:0 \
    --query "What is the retention policy?" \
    --top-k 5
```

Use only for dev/prototyping — production ingestion must go through the CDK-managed Lambda with its identity-side IAM policy and DLQ.

### 3.6 Monolith gotchas

- **Immutability is permanent.** `IndexName`, `Dimension`, `DistanceMetric`, and `NonFilterableMetadataKeys` cannot be updated after create. Changing any of these in CDK forces a replacement: CFN deletes the index (and all vectors in it) then creates a new one. Always treat dimension + metric + non-filterable keys as a schema contract; version them into the index name if you need to evolve (`rag-main-v2` rather than mutating `rag-main`).
- **`source_text` on non-filterable is the canonical RAG idiom.** Filterable metadata has tighter size limits and its values are indexed for filter evaluation; non-filterable values skip that indexing and are only returned via `returnMetadata=True`. Putting chunk text on filterable metadata inflates index size and can be rejected for length. `# TODO(verify): exact per-vector filterable-metadata byte cap in your region — confirm via AWS docs before shipping long-text fields as filterable.`
- **`PutVectors` batching: up to 100 vectors/call is the documented best practice.** Writing one at a time multiplies cost and latency 10-100×. The ingestion handler above batches at 100. `# TODO(verify): exact upper bound of vectors per PutVectors call and per-vector size cap — check boto3 reference before tuning higher.`
- **Client service name is `s3vectors`, not `s3`.** `boto3.client("s3")` will not find the API. Typos produce `EndpointConnectionError` rather than `UnknownServiceError`, which is misleading.
- **BlockPublicAccess is always-on** on vector buckets — cannot be disabled, no L1 property exists. Good security default; just know there is nothing to toggle.
- **Filter keys that reference a non-filterable key fail.** If `source_text` is in `NonFilterableMetadataKeys`, `filter={"source_text": {"$contains": "foo"}}` will be rejected. Use filterable keys (`doc_id`, `section`, ...) for filters; use separate full-text search if you need source-text filtering.
- **Strong consistency on writes, no eventual-consistency gotcha.** `PutVectors` → immediately queryable on the next `QueryVectors`. This is different from S3 object eventual consistency in the pre-2020 era, and different from many vector DBs that require an index build.
- **KMS CMK permissions**: any ingestion role needs `kms:GenerateDataKey` (for writes) and `kms:Decrypt` (for reads / queries). Without `GenerateDataKey`, `PutVectors` fails with `KMSAccessDeniedException` that manifests as a generic `500` upstream.
- **`Ref` on `AWS::S3Vectors::VectorBucket` returns the ARN, not the name.** Don't `Fn::Sub "${VectorBucket}/${Index}"` assuming you'll get the name — use `.attr_vector_bucket_name` explicitly.
- **Regional limitation as of 2025-04**: 14 regions listed in §0 header. Deploying outside those regions fails at stack-create with `ResourceNotSupportedInRegion`. Check `docs/template_params.md` → `AWS_REGION` against the list before wiring S3 Vectors into a new project.

---

## 4. Micro-Stack Variant

**Use when:** `VectorStoreStack` owns the vector bucket + all indexes + the local CMK; `ComputeStack` (or `IngestionStack`) owns the ingestion + query Lambdas that read/write those indexes.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** — `_LAMBDAS_ROOT` pattern.
2. **There are no L2 grants, ever.** Every `s3vectors:*` permission is an identity-side `PolicyStatement` on the exact index ARN. This is the same rule as elsewhere — here it's a physical constraint, not a best-practice choice.
3. **Never share the KMS CMK across stacks via object reference.** Own the CMK inside `VectorStoreStack`; publish `key_arn` via SSM; consumer role grants itself `kms:Decrypt` / `kms:GenerateDataKey` identity-side against the ARN token.
4. **Never pass the `CfnIndex` object itself into `ComputeStack`.** Pass `index_arn` as an SSM parameter string. Consumer Lambdas only need the ARN to build the IAM statement and the `indexName` / `vectorBucketName` strings to call the API.
5. **`iam:PassRole` with `iam:PassedToService` condition** on any role the ingestion Lambda can hand off (e.g. to Step Functions for a re-embed workflow). PermissionsBoundary on every role.

### 4.2 Dedicated `VectorStoreStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, CfnOutput, Duration, RemovalPolicy, Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3vectors as s3v,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class VectorStoreStack(cdk.Stack):
    """Owns the Amazon S3 Vectors vector bucket, all indexes, and the local
    KMS CMK. Publishes vector-bucket name + each index ARN via SSM so
    downstream compute stacks can import by parameter name without creating
    CloudFormation exports.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        permission_boundary: iam.IManagedPolicy,
        embedding_dimension: int = 1024,      # Titan v2 default
        distance_metric: str = "cosine",      # or "euclidean"
        include_eval_index: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(
            scope, f"{{project_name}}-vector-store-{stage_name}", **kwargs,
        )
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # A) Local CMK — never imported from SecurityStack (non-negotiable #3)
        cmk = kms.Key(
            self, "VectorKey",
            alias=f"alias/{{project_name}}-vectors-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
        )
        # Allow S3 Vectors service principal to use the key.
        cmk.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowS3VectorsService",
            principals=[iam.ServicePrincipal("s3vectors.amazonaws.com")],
            actions=["kms:GenerateDataKey*", "kms:Decrypt", "kms:DescribeKey"],
            resources=["*"],
            conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
        ))

        # B) Vector bucket
        vector_bucket = s3v.CfnVectorBucket(
            self, "VectorBucket",
            vector_bucket_name=f"{{project_name}}-vectors-{stage_name}",
            encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="aws:kms",
                kms_key_arn=cmk.key_arn,
            ),
            tags=[{"key": "Project", "value": "{project_name}"},
                  {"key": "Stage",   "value": stage_name}],
        )

        # C) Main index — production RAG corpus
        index_main = s3v.CfnIndex(
            self, "RagMainIndex",
            vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
            index_name="rag-main",
            data_type="float32",
            dimension=embedding_dimension,
            distance_metric=distance_metric,
            metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["source_text"],
            ),
        )
        index_main.add_dependency(vector_bucket)

        main_index_arn = Stack.of(self).format_arn(
            service="s3vectors",
            resource="bucket",
            resource_name=(
                f"{vector_bucket.vector_bucket_name}/index/{index_main.index_name}"
            ),
        )

        # D) Optional eval index — for CI retrieval regression tests
        eval_index_arn: str | None = None
        if include_eval_index:
            index_eval = s3v.CfnIndex(
                self, "RagEvalIndex",
                vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
                index_name="rag-eval",
                data_type="float32",
                dimension=embedding_dimension,
                distance_metric=distance_metric,
                metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                    non_filterable_metadata_keys=["source_text"],
                ),
            )
            index_eval.add_dependency(vector_bucket)
            eval_index_arn = Stack.of(self).format_arn(
                service="s3vectors",
                resource="bucket",
                resource_name=(
                    f"{vector_bucket.vector_bucket_name}/index/{index_eval.index_name}"
                ),
            )

        # E) Publish for ComputeStack.
        ssm.StringParameter(
            self, "VectorBucketNameParam",
            parameter_name=f"/{{project_name}}/{stage_name}/vectors/bucket_name",
            string_value=vector_bucket.vector_bucket_name,
        )
        ssm.StringParameter(
            self, "VectorBucketArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/vectors/bucket_arn",
            string_value=vector_bucket.attr_vector_bucket_arn,
        )
        ssm.StringParameter(
            self, "RagMainIndexNameParam",
            parameter_name=f"/{{project_name}}/{stage_name}/vectors/rag_main_index_name",
            string_value=index_main.index_name,
        )
        ssm.StringParameter(
            self, "RagMainIndexArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/vectors/rag_main_index_arn",
            string_value=main_index_arn,
        )
        if eval_index_arn is not None:
            ssm.StringParameter(
                self, "RagEvalIndexArnParam",
                parameter_name=f"/{{project_name}}/{stage_name}/vectors/rag_eval_index_arn",
                string_value=eval_index_arn,
            )
        ssm.StringParameter(
            self, "VectorKmsArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/vectors/kms_arn",
            string_value=cmk.key_arn,
        )

        self.vector_bucket   = vector_bucket
        self.index_main      = index_main
        self.index_main_arn  = main_index_arn
        self.eval_index_arn  = eval_index_arn
        self.cmk             = cmk
        self.permission_boundary = permission_boundary

        CfnOutput(self, "VectorBucketName", value=vector_bucket.vector_bucket_name)
        CfnOutput(self, "RagMainIndexArn",  value=main_index_arn)
```

### 4.3 Consumer pattern — identity-side grants in `ComputeStack`

```python
# Inside ComputeStack. No CfnIndex / VectorBucket references — ARNs from SSM.
from aws_cdk import aws_ssm as ssm, aws_iam as iam, aws_lambda as _lambda

vector_bucket_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/vectors/bucket_name"
)
rag_main_index_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/vectors/rag_main_index_name"
)
rag_main_index_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/vectors/rag_main_index_arn"
)
vector_kms_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/vectors/kms_arn"
)

ingestion_fn = _lambda.Function(
    self, "VectorIngestionFn",
    # ... standard config ...
    environment={
        "VECTOR_BUCKET_NAME": vector_bucket_name,
        "VECTOR_INDEX_NAME":  rag_main_index_name,
        "PUT_VECTORS_BATCH_SIZE": "100",
    },
)

# Write grants — identity-side only.
ingestion_fn.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:ListVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:QueryVectors",
        "s3vectors:GetIndex",
    ],
    resources=[rag_main_index_arn],
))
ingestion_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
    resources=[vector_kms_arn],
))

# Read-only query function.
query_fn = _lambda.Function(
    self, "VectorQueryFn",
    # ... standard config ...
    environment={
        "VECTOR_BUCKET_NAME": vector_bucket_name,
        "VECTOR_INDEX_NAME":  rag_main_index_name,
        "DEFAULT_TOP_K":      "5",
    },
)
query_fn.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
        "s3vectors:ListVectors",
        "s3vectors:GetIndex",
    ],
    resources=[rag_main_index_arn],
))
query_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kms:Decrypt", "kms:DescribeKey"],
    resources=[vector_kms_arn],
))

iam.PermissionsBoundary.of(ingestion_fn.role).apply(self.permission_boundary)
iam.PermissionsBoundary.of(query_fn.role).apply(self.permission_boundary)
```

### 4.4 Micro-stack gotchas

- **`ssm.StringParameter.value_for_string_parameter`** returns a token. It can be used inside `resources=[...]` and environment variables directly — do NOT `.split(":")` it in Python; CloudFormation resolves `{{resolve:ssm:...}}` at deploy time.
- **Per-tenant isolation via many indexes**: one vector bucket supports up to 10,000 indexes. Multi-tenant pattern is one index per tenant (`rag-main-tenant-{id}`) — keeps queries scoped and IAM policies per-index. Even though this kit is single-tenant, plan index names so you can add tenant suffixes without renaming.
- **Regional VPC endpoint**: as of GA, S3 Vectors does NOT have a VPC Interface Endpoint (`com.amazonaws.*.s3vectors`) in all 14 regions. `# TODO(verify): VPC endpoint availability in your target region`. If missing, Lambdas in `PRIVATE_ISOLATED` subnets cannot reach S3 Vectors — either put them in `PRIVATE_WITH_EGRESS` with NAT, or fall back to Bedrock KB + S3 Vectors which uses Bedrock's managed networking.
- **Cross-stack deletion order**: if `VectorStoreStack` is deleted while `ComputeStack` still references `RagMainIndexArn` via SSM, the SSM parameter disappears and the consumer stack updates fail on next deploy. Deploy order: `VectorStoreStack` first; delete order: `ComputeStack` first.
- **Dimension as a stack prop**: passing `embedding_dimension` as a Python arg to `VectorStoreStack.__init__` locks the bucket's indexes to that dim. Changing it later forces index replacement (loses all vectors). Pipe it from `docs/template_params.md` → `EMBEDDING_DIMENSION` and treat changes as explicit schema migrations.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / small corpus (< 10 M vectors) with infrequent queries | §3 Monolith + single `rag-main` index, dim 1024, cosine |
| Production RAG with moderate query volume | §4 Micro-Stack, one index per tenant or one `rag-main` + regular snapshot export |
| High-QPS hot path (> 100 QPS sustained) | Keep S3 Vectors as cost-optimised primary + scheduled export → OpenSearch Serverless vector collection for hot-path query. Blue-green index promotion via `rag-main-v{N}` naming |
| Want fully managed ingestion (no chunking code) | Use **Bedrock Knowledge Base with S3 Vectors backing store** — Bedrock chunks + embeds from an S3 source bucket and writes to the vector bucket; trade control for managed ops. See AWS docs link below |
| Reduced-dim variant for lower cost | Titan v2 supports 512 or 256 dim output; create a new index (`rag-main-512`) with matching `dimension`; re-embed is mandatory (dims are immutable) |
| Switch embedding model (Titan v2 → Cohere v3) | New index with new dimension; one-shot reindex; atomic swap via SSM `rag_main_index_arn` parameter update + Lambda restart |
| Multi-tenant SaaS | One index per tenant up to 10,000; prefix `rag-tenant-{id}`; IAM per-tenant on `index_arn` |
| Need filter on numeric range (e.g. `uploaded_at > X`) | Filterable metadata + numeric type; `filter={"uploaded_at": {"$gte": 1714000000}}`. `# TODO(verify): exact operator syntax supported — `$gte` vs `gte` vs `>=` varies across AWS services; confirm with boto3 docs before shipping.` |
| Corpus > 10 M vectors with query latency requirements | Split corpus across shards (multiple indexes), query in parallel, client-side merge by distance. OR move to OpenSearch Serverless |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_DATA_S3_VECTORS.py`. Offline; `cdk.Stack` as deps stub.

```python
"""SOP verification — VectorStoreStack synth contains the expected
resources and the vector-bucket + index properties are wired correctly."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-west-2")


def test_vector_store_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.vector_store_stack import VectorStoreStack
    stack = VectorStoreStack(
        app, stage_name="dev",
        permission_boundary=boundary,
        embedding_dimension=1024,
        distance_metric="cosine",
        include_eval_index=True,
        env=env,
    )
    t = Template.from_stack(stack)

    # Vector bucket with SSE-KMS
    t.resource_count_is("AWS::S3Vectors::VectorBucket", 1)
    t.has_resource_properties("AWS::S3Vectors::VectorBucket", Match.object_like({
        "EncryptionConfiguration": {
            "SseType":    "aws:kms",
            "KmsKeyArn":  Match.any_value(),
        },
    }))

    # 2 indexes — main + eval
    t.resource_count_is("AWS::S3Vectors::Index", 2)
    t.has_resource_properties("AWS::S3Vectors::Index", Match.object_like({
        "IndexName":      "rag-main",
        "DataType":       "float32",
        "Dimension":      1024,
        "DistanceMetric": "cosine",
        "MetadataConfiguration": {
            "NonFilterableMetadataKeys": ["source_text"],
        },
    }))
    t.has_resource_properties("AWS::S3Vectors::Index", Match.object_like({
        "IndexName":      "rag-eval",
        "Dimension":      1024,
    }))

    # Local KMS CMK
    t.resource_count_is("AWS::KMS::Key", 1)

    # SSM parameters published (bucket name + arn, main idx name + arn,
    # eval idx arn, kms arn => 6)
    t.resource_count_is("AWS::SSM::Parameter", 6)


def test_vector_store_stack_without_eval_index_publishes_5_ssm_params():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.vector_store_stack import VectorStoreStack
    stack = VectorStoreStack(
        app, stage_name="prod",
        permission_boundary=boundary,
        embedding_dimension=1024,
        distance_metric="cosine",
        include_eval_index=False,
        env=env,
    )
    t = Template.from_stack(stack)

    t.resource_count_is("AWS::S3Vectors::Index", 1)
    t.resource_count_is("AWS::SSM::Parameter", 5)
```

---

## 7. References

- `docs/template_params.md` — `EMBEDDING_DIMENSION` (1024 | 512 | 256), `VECTOR_DISTANCE_METRIC` (`cosine` | `euclidean`), `VECTOR_INDEX_NAME_MAIN`, `VECTOR_INDEX_NAME_EVAL`, `PUT_VECTORS_BATCH_SIZE`, `DEFAULT_TOP_K`
- `docs/Feature_Roadmap.md` — feature IDs `VS-10` (vector bucket), `VS-11` (main index), `VS-12` (eval index), `VS-13` (ingestion Lambda), `VS-14` (query Lambda), `VS-15` (OpenSearch Serverless hot-path export)
- AWS docs:
  - [S3 Vectors overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
  - [S3 Vectors getting started](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-getting-started.html)
  - [S3 Vectors indexes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-indexes.html)
  - [S3 Vectors buckets](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-buckets.html)
  - [S3 Vectors best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-best-practices.html)
  - [S3 Vectors with Bedrock Knowledge Base](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html)
  - [`AWS::S3Vectors::VectorBucket` CFN ref](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-s3vectors-vectorbucket.html)
  - [`AWS::S3Vectors::Index` CFN ref](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-s3vectors-index.html)
  - [`aws_cdk.aws_s3vectors` CDK module](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_s3vectors.html)
  - [boto3 `s3vectors` client reference](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3vectors.html)
  - [`awslabs/s3vectors-embed-cli` (dev-loop aid)](https://github.com/awslabs/s3vectors-embed-cli)
- Related SOPs:
  - `PATTERN_DOC_INGESTION_RAG` — end-to-end doc → chunk → embed → PutVectors pipeline that depends on this SOP for storage
  - `LLMOPS_BEDROCK` — Titan v2 `invoke_model` for embedding generation
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers
  - `LAYER_SECURITY` — KMS CMK policy patterns, service-principal conditions
  - `LAYER_NETWORKING` — VPC endpoint considerations (S3 Vectors not universally available as interface endpoint yet)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — Amazon S3 Vectors GA deep-dive. Vector bucket + vector index L1 CDK (`aws_s3vectors.CfnVectorBucket`, `CfnIndex`), boto3 `s3vectors` client PutVectors (batched) + QueryVectors (with filterable metadata), canonical non-filterable `source_text` idiom, immutability contract on index dimension/metric/non-filterable keys, Titan v2 1024-dim default with 512/256 reduced-dim swap-matrix row, Bedrock Knowledge Base + OpenSearch Serverless hot-path swap rows, regional-availability + KMS + BlockPublicAccess-always-on gotchas. Created to fill gap surfaced by RAG-chatbot kit design, grounded in AWS S3 Vectors GA documentation. |
