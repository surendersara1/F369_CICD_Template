# SOP — Document Ingestion RAG Pipeline (parse → chunk → embed → store)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · S3 raw + EventBridge · Textract / Unstructured / PyPDF · Bedrock Titan Text Embeddings v2 (1024 / 512 / 256 dims) · Amazon S3 Vectors (primary) · Bedrock Knowledge Base (managed alternative) · DynamoDB doc-metadata table · SQS DLQ · SHA256-deterministic idempotency key

---

## 1. Purpose

- Codify the **document → parse → chunk → embed → PutVectors** RAG ingestion pipeline end-to-end: raw-doc S3 upload triggers an EventBridge-driven Lambda that parses, chunks, generates Titan v2 embeddings, and batches `PutVectors` calls into a vector index.
- Provide **decision branches** for the three plug-points:
  - **Parser**: Textract (OCR + layout) · Unstructured (markdown / HTML / office formats) · PyPDF (cheap text-only PDF).
  - **Chunker**: fixed (token + overlap) · semantic (sentence-boundary + similarity) · by_section (heading-aware).
  - **Vector store**: S3 Vectors (default, see `DATA_S3_VECTORS`) · Bedrock KB on S3 Vectors (managed) · Bedrock KB on OpenSearch Serverless (high-QPS).
- Provide a **canonical `doc_metadata` DynamoDB table** — one row per document — updated on ingestion start + complete, with `status ∈ {uploaded, parsing, embedding, indexed, failed}` and `chunk_count` for downstream retrieval validation.
- Codify **idempotency**: vector key = `sha256(doc_id + "#" + chunk_index)`; re-ingestion overwrites rather than duplicates.
- Codify **partial-failure handling**: per-document DLQ (failed parse or embed); metadata row marked `failed` with `failure_reason`; redrive via `EVENT_DRIVEN_PATTERNS` §6 DLQ-reprocessor pattern.
- Include when the SOW signals: "RAG", "chatbot over documents", "semantic search over PDFs", "knowledge base", "ingest policy docs", "embed uploaded files", "answer questions about our internal docs".
- Reference `DATA_S3_VECTORS` for storage details — do not duplicate the vector-bucket / index CDK here.

---

## 2. Decision — Monolith vs Micro-Stack + parser/chunker/store choices

### 2.1 Structural split

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the raw bucket, ingestion Lambda, doc-metadata table, and (via `DATA_S3_VECTORS` §3) the vector bucket + indexes | **§3 Monolith Variant** |
| `StorageStack` owns raw bucket; `VectorStoreStack` owns S3 Vectors (per `DATA_S3_VECTORS` §4); `IngestionStack` owns parser/chunker/embedder Lambda + doc-metadata table | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Raw-doc bucket + EventBridge rule**: same cycle risk as `EVENT_DRIVEN_PATTERNS` §5 — never split bucket + its S3 notification across stacks. Bucket in `StorageStack` with `event_bridge_enabled=True`, rule owned in `IngestionStack` using L1 `CfnRule`.
2. **Bedrock invoke**: `bedrock-runtime:InvokeModel` on Titan v2 requires identity-side policy; the model ARN is regional (`arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0`) and doesn't belong to any stack.
3. **Vector-store grants** are all identity-side anyway (no L2 exists — see `DATA_S3_VECTORS`), so cross-stack doesn't introduce new cycles but does require reading the `rag_main_index_arn` SSM parameter.
4. **Doc-metadata table** is local to `IngestionStack`; the query-side RAG Lambda (in `ComputeStack`) reads it for "has this doc finished indexing?" checks — identity-side `dynamodb:GetItem` with the table ARN.

Micro-Stack variant fixes all of this via: (a) bucket in `StorageStack` with `event_bridge_enabled=True`; (b) `VectorStoreStack` from `DATA_S3_VECTORS` owns all vector resources; (c) `IngestionStack` owns the pipeline Lambda + metadata table + DLQ; (d) every cross-stack handle is a string (bucket name, index arn, kms arn) via SSM.

### 2.2 Plug-point matrix

| Plug-point | Variant | Use when |
|---|---|---|
| Parser | `textract` | Scanned docs, images, mixed OCR + layout, table extraction needed |
| Parser | `unstructured` | Heterogeneous formats (docx, pptx, html, md), rich metadata (headings, footers) |
| Parser | `pypdf` | Text-only PDFs, low cost, single-file simple extraction |
| Chunker | `fixed` | Default / worked example. 512 tokens, 64 overlap. Predictable, cheap |
| Chunker | `semantic` | Long-form narrative docs; preserves semantic boundaries but requires an embed-per-candidate-split step → 2-5× embedding cost |
| Chunker | `by_section` | Structured docs (policy manuals, legal); uses Unstructured's heading metadata |
| Store | `s3_vectors` | Default. Cost-optimised, direct control. See `DATA_S3_VECTORS` |
| Store | `bedrock_kb_s3_vectors` | Managed ingest — trade chunking control for zero-code ops. Bedrock chunks + embeds + writes |
| Store | `bedrock_kb_opensearch` | High-QPS retrieval requirement + managed ingest combined |

The **canonical worked example** in §3 uses `textract + fixed + s3_vectors`. Other combinations are swap-matrix rows in §5.

---

## 3. Monolith Variant

### 3.1 Architecture

```
                    [ User / API / upstream batch ]
                              │  PUT s3://raw-docs/{doc_id}.pdf
                              ▼
                    ┌─────────────────────┐
                    │  raw-docs bucket    │  event_bridge_enabled=True
                    │  SSE-KMS            │
                    └──────────┬──────────┘
                              │  S3 ObjectCreated
                              ▼
                    EventBridge default bus
                              │  filter: detail.object.key suffix in {.pdf, .docx, .txt, .md}
                              ▼
                    ┌─────────────────────────────────────────┐
                    │  IngestionLambda                        │
                    │  1. DDB: upsert status=parsing          │
                    │  2. S3 GetObject(raw bucket)            │
                    │  3. Parse: textract.start_document_..   │
                    │                  + textract.get_...     │
                    │                    (async; poll or SNS) │
                    │  4. Chunk: fixed 512 tokens, 64 overlap │
                    │  5. Embed: bedrock.invoke_model Titan v2│
                    │              batched 25 chunks / call   │
                    │  6. DDB: status=embedding               │
                    │  7. PutVectors batched (100/call)       │
                    │  8. DDB: status=indexed, chunk_count=N  │
                    │                                         │
                    │  On any failure → DLQ + DDB status=failed │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │  S3 Vectors index "rag-main"            │
                    │  (see DATA_S3_VECTORS §3 for details)   │
                    └─────────────────────────────────────────┘

                              │  on ingest failure
                              ▼
                    ┌─────────────────────┐
                    │  DLQ (SQS)          │  redrive via standard DLQ-reprocessor
                    └─────────────────────┘
```

### 3.2 CDK — `_create_doc_ingestion_rag()` method body

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
)


def _create_doc_ingestion_rag(self, stage: str) -> None:
    """Monolith variant. Assumes self.{kms_key, raw_docs_bucket (with
    event_bridge_enabled=True), vector_bucket_name, vector_index_name,
    vector_index_arn, vector_kms_arn} already exist — either built inline
    in this stack or imported from an outer stack in the monolith app."""

    # A) doc_metadata table — one row per doc
    self.doc_metadata = ddb.Table(
        self, "DocMetadata",
        table_name=f"{{project_name}}-doc-metadata-{stage}",
        partition_key=ddb.Attribute(name="doc_id", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=self.kms_key,
        time_to_live_attribute="ttl",
        stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
        point_in_time_recovery=(stage == "prod"),
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )
    self.doc_metadata.add_global_secondary_index(
        index_name="by-status",
        partition_key=ddb.Attribute(name="status",     type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(     name="uploaded_at", type=ddb.AttributeType.STRING),
        projection_type=ddb.ProjectionType.KEYS_ONLY,
    )

    # B) DLQ for permanently-failed docs
    self.ingest_dlq = sqs.Queue(
        self, "IngestDlq",
        queue_name=f"{{project_name}}-ingest-dlq-{stage}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )

    # C) Ingestion Lambda — parse + chunk + embed + PutVectors
    log = logs.LogGroup(
        self, "IngestionLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-doc-ingestion-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.ingestion_fn = _lambda.Function(
        self, "IngestionFn",
        function_name=f"{{project_name}}-doc-ingestion-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset("lambda/doc_ingestion"),
        memory_size=3008,                      # parsing + embedding is memory-heavy
        timeout=Duration.minutes(15),          # Textract async + Titan batching
        log_group=log,
        tracing=_lambda.Tracing.ACTIVE,
        dead_letter_queue_enabled=True,
        dead_letter_queue=self.ingest_dlq,
        reserved_concurrent_executions=10,     # cap Bedrock TPS blast radius
        environment={
            "RAW_BUCKET":         self.raw_docs_bucket.bucket_name,
            "DOC_METADATA_TABLE": self.doc_metadata.table_name,
            "VECTOR_BUCKET_NAME": self.vector_bucket_name,
            "VECTOR_INDEX_NAME":  self.vector_index_name,
            "EMBED_MODEL_ID":     "amazon.titan-embed-text-v2:0",
            "EMBED_DIMENSION":    "1024",
            "CHUNK_SIZE_TOKENS":  "512",
            "CHUNK_OVERLAP":      "64",
            "CHUNK_STRATEGY":     "fixed",      # fixed | semantic | by_section
            "PARSER":             "textract",   # textract | unstructured | pypdf
            "PUT_VECTORS_BATCH_SIZE": "100",
            "EMBED_BATCH_SIZE":   "25",
            "POWERTOOLS_SERVICE_NAME": "{project_name}-doc-ingestion",
            "POWERTOOLS_LOG_LEVEL":    "INFO",
        },
    )

    # D) Grants.
    #   Raw bucket: L2 safe in monolith.
    self.raw_docs_bucket.grant_read(self.ingestion_fn)
    #   DDB: L2 safe in monolith.
    self.doc_metadata.grant_read_write_data(self.ingestion_fn)
    #   Bedrock: identity-side always — no L2 grant exists for foundation model ARNs.
    self.ingestion_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel"],
        resources=[
            f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
        ],
    ))
    #   Textract async.
    self.ingestion_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "textract:StartDocumentAnalysis",
            "textract:StartDocumentTextDetection",
            "textract:GetDocumentAnalysis",
            "textract:GetDocumentTextDetection",
            "textract:AnalyzeDocument",
            "textract:DetectDocumentText",
        ],
        resources=["*"],                       # Textract uses service-level ARN
    ))
    #   S3 Vectors: identity-side (no L2). Uses index ARN not bucket ARN.
    self.ingestion_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "s3vectors:PutVectors",
            "s3vectors:GetVectors",
            "s3vectors:DeleteVectors",
            "s3vectors:QueryVectors",
            "s3vectors:GetIndex",
        ],
        resources=[self.vector_index_arn],
    ))
    #   KMS for vector bucket CMK
    self.ingestion_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
        resources=[self.vector_kms_arn],
    ))

    # E) EventBridge rule: raw-doc ObjectCreated → IngestionFn
    events.Rule(
        self, "DocUploadedRule",
        rule_name=f"{{project_name}}-doc-uploaded-{stage}",
        event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
        event_pattern=events.EventPattern(
            source=["aws.s3"],
            detail_type=["Object Created"],
            detail={
                "bucket": {"name": [self.raw_docs_bucket.bucket_name]},
                "object": {"key": [
                    {"suffix": ".pdf"},
                    {"suffix": ".docx"},
                    {"suffix": ".txt"},
                    {"suffix": ".md"},
                ]},
            },
        ),
        targets=[targets.LambdaFunction(self.ingestion_fn)],
    )

    CfnOutput(self, "DocMetadataTable", value=self.doc_metadata.table_name)
    CfnOutput(self, "IngestDlqArn",     value=self.ingest_dlq.queue_arn)
```

### 3.3 Ingestion handler — saved to `lambda/doc_ingestion/index.py`

```python
"""Document ingestion handler — parse, chunk, embed (Titan v2), PutVectors.

Triggered by S3 → EventBridge on raw-doc ObjectCreated. Idempotent via
deterministic vector keys (sha256(doc_id + "#" + chunk_index)).

Failure contract:
  - Transient errors re-raise → Lambda invocation fails → EventBridge retries
    (configurable) → ultimately lands in the function's DLQ.
  - Permanent errors (corrupt doc, parse-rejected format) → caught, DDB
    marked status=failed with failure_reason, NOT re-raised.

DDB doc_metadata row shape:
  doc_id          STRING  (partition key)
  filename        STRING
  content_type    STRING
  s3_key          STRING
  chunk_count     NUMBER   (set on success)
  status          STRING   (uploaded | parsing | embedding | indexed | failed)
  failure_reason  STRING   (only when status=failed)
  uploaded_at     STRING   (ISO 8601)
  indexed_at      STRING   (ISO 8601, only when status=indexed)
  ttl             NUMBER   (epoch, 30 days out)
"""
import hashlib
import io
import json
import logging
import os
import time

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client   = boto3.client("s3")
ddb         = boto3.resource("dynamodb")
bedrock     = boto3.client(
    "bedrock-runtime",
    config=Config(retries={"max_attempts": 5, "mode": "standard"}),
)
s3v         = boto3.client(
    "s3vectors",
    config=Config(retries={"max_attempts": 5, "mode": "standard"}),
)
textract    = boto3.client("textract")

RAW_BUCKET         = os.environ["RAW_BUCKET"]
DOC_METADATA       = ddb.Table(os.environ["DOC_METADATA_TABLE"])
VECTOR_BUCKET      = os.environ["VECTOR_BUCKET_NAME"]
VECTOR_INDEX       = os.environ["VECTOR_INDEX_NAME"]
EMBED_MODEL_ID     = os.environ["EMBED_MODEL_ID"]
EMBED_DIMENSION    = int(os.environ["EMBED_DIMENSION"])
CHUNK_SIZE_TOKENS  = int(os.environ["CHUNK_SIZE_TOKENS"])
CHUNK_OVERLAP      = int(os.environ["CHUNK_OVERLAP"])
CHUNK_STRATEGY     = os.environ.get("CHUNK_STRATEGY", "fixed")   # fixed|semantic|by_section
PARSER             = os.environ.get("PARSER", "textract")        # textract|unstructured|pypdf
PUT_BATCH          = int(os.environ.get("PUT_VECTORS_BATCH_SIZE", "100"))
EMBED_BATCH        = int(os.environ.get("EMBED_BATCH_SIZE", "25"))


class PermanentError(Exception):
    """Unrecoverable — DDB marked failed, NOT re-raised so DLQ isn't triggered."""


def lambda_handler(event, _ctx):
    detail   = event["detail"]
    bucket   = detail["bucket"]["name"]
    key      = detail["object"]["key"]
    doc_id   = _doc_id_from_key(key)
    filename = key.rsplit("/", 1)[-1]
    now      = _now()

    # 1) Upsert metadata row as 'parsing'.
    DOC_METADATA.update_item(
        Key={"doc_id": doc_id},
        UpdateExpression=(
            "SET filename = :fn, s3_key = :k, #s = :s, uploaded_at = :u, "
            "#t = if_not_exists(#t, :ttl)"
        ),
        ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
        ExpressionAttributeValues={
            ":fn":  filename, ":k": key, ":s": "parsing", ":u": now,
            ":ttl": int(time.time()) + 30 * 86400,
        },
    )

    try:
        # 2) Parse.
        text = _parse(bucket, key)
        if not text.strip():
            raise PermanentError("parser returned empty text")

        # 3) Chunk.
        chunks = _chunk(text, filename)
        if not chunks:
            raise PermanentError("chunker returned zero chunks")

        # 4) Embed (batched).
        _mark_status(doc_id, "embedding")
        embeddings = _embed_all(chunks)

        # 5) PutVectors (batched).
        records = []
        for i, (chunk_text, embedding) in enumerate(zip(chunks, embeddings)):
            records.append({
                "key": _vector_key(doc_id, i),
                "data": {"float32": embedding},
                "metadata": {
                    "source_text":  chunk_text,           # NON-filterable
                    "doc_id":       doc_id,
                    "chunk_index":  i,
                    "filename":     filename,
                    "uploaded_at":  int(time.time()),
                },
            })

        for batch in _chunked(records, PUT_BATCH):
            s3v.put_vectors(
                vectorBucketName=VECTOR_BUCKET,
                indexName=VECTOR_INDEX,
                vectors=batch,
            )

        # 6) Mark indexed.
        DOC_METADATA.update_item(
            Key={"doc_id": doc_id},
            UpdateExpression="SET #s = :s, chunk_count = :c, indexed_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "indexed", ":c": len(chunks), ":t": _now(),
            },
        )
        logger.info("ingestion complete doc=%s chunks=%d", doc_id, len(chunks))
        return {"doc_id": doc_id, "chunks": len(chunks), "status": "indexed"}

    except PermanentError as e:
        logger.warning("permanent failure doc=%s reason=%s", doc_id, e)
        _mark_failed(doc_id, f"permanent:{e}")
        return {"doc_id": doc_id, "status": "failed", "reason": str(e)}
    except Exception:
        # Transient — let Lambda retry / land in DLQ.
        logger.exception("transient failure doc=%s", doc_id)
        _mark_failed(doc_id, "transient:see_logs")
        raise


# ---------------------------------------------------------------- parsers

def _parse(bucket: str, key: str) -> str:
    if PARSER == "textract":
        return _parse_textract(bucket, key)
    if PARSER == "pypdf":
        return _parse_pypdf(bucket, key)
    if PARSER == "unstructured":
        return _parse_unstructured(bucket, key)
    raise PermanentError(f"unknown PARSER={PARSER}")


def _parse_textract(bucket: str, key: str) -> str:
    """Async Textract — suitable for multi-page PDFs and images. For small
    (<5 MB) synchronous docs, swap to `textract.detect_document_text` with
    `Document={"Bytes": ...}`.
    """
    # Start async job.
    start = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
    )
    job_id = start["JobId"]

    # Poll until SUCCEEDED | FAILED (best practice in prod: use SNS async
    # completion notification instead of polling to avoid long Lambda time).
    deadline = time.time() + 600     # 10 min cap
    status   = "IN_PROGRESS"
    while status == "IN_PROGRESS" and time.time() < deadline:
        time.sleep(3)
        resp = textract.get_document_text_detection(JobId=job_id, MaxResults=1)
        status = resp["JobStatus"]

    if status != "SUCCEEDED":
        raise PermanentError(f"textract job {job_id} status={status}")

    # Page through results.
    lines: list[str] = []
    next_token: str | None = None
    while True:
        kwargs = {"JobId": job_id, "MaxResults": 1000}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = textract.get_document_text_detection(**kwargs)
        for block in resp.get("Blocks", []):
            if block.get("BlockType") == "LINE":
                lines.append(block.get("Text", ""))
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return "\n".join(lines)


def _parse_pypdf(bucket: str, key: str) -> str:
    """Cheap text-only PDF extraction — assumes `pypdf` in deployment bundle.
    Raises PermanentError on scanned (no-text) PDFs — caller should swap to
    textract variant for OCR.
    """
    from pypdf import PdfReader     # bundled in lambda layer

    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    reader = PdfReader(io.BytesIO(body))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    if not text.strip():
        raise PermanentError("pypdf extracted empty text (scanned PDF?)")
    return text


def _parse_unstructured(bucket: str, key: str) -> str:
    """Unstructured.io — handles docx / pptx / html / md / pdf. Expects the
    `unstructured` package in a Lambda layer.
    """
    # TODO(verify): Lambda layer size for unstructured + its transitive deps
    # (nltk, python-docx, python-pptx, lxml) — often exceeds 250 MB unzipped.
    # Consider container-image Lambda if layer won't fit.
    from unstructured.partition.auto import partition  # bundled in layer

    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    elements = partition(file=io.BytesIO(body), metadata_filename=key)
    return "\n".join(str(el) for el in elements)


# ---------------------------------------------------------------- chunkers

def _chunk(text: str, filename: str) -> list[str]:
    if CHUNK_STRATEGY == "fixed":
        return _chunk_fixed(text, CHUNK_SIZE_TOKENS, CHUNK_OVERLAP)
    if CHUNK_STRATEGY == "semantic":
        return _chunk_semantic(text)
    if CHUNK_STRATEGY == "by_section":
        return _chunk_by_section(text)
    raise PermanentError(f"unknown CHUNK_STRATEGY={CHUNK_STRATEGY}")


def _chunk_fixed(text: str, size_tokens: int, overlap: int) -> list[str]:
    """Token-approximate fixed chunking. Uses whitespace-word approximation
    (1 word ≈ 1.3 tokens for English). For precise token counts, swap in
    tiktoken or the Bedrock tokeniser — at ingestion time the approximation
    is fine because embed model is the source of truth.
    """
    words = text.split()
    if not words:
        return []
    # convert tokens → approximate words
    step_words  = max(1, int(size_tokens / 1.3) - overlap)
    chunk_words = max(1, int(size_tokens / 1.3))
    chunks: list[str] = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + chunk_words])
        if chunk.strip():
            chunks.append(chunk)
        i += step_words
    return chunks


def _chunk_semantic(text: str) -> list[str]:
    """Placeholder — semantic chunking typically requires sentence splitting
    + embedding adjacent sentence pairs + thresholding similarity. For
    correctness, see LangChain's SemanticChunker or Llama-Index's
    SemanticSplitterNodeParser. Here we degrade to paragraph-splitting.
    # TODO(verify): production-grade semantic chunker — this placeholder
    # only splits on blank-line paragraph boundaries.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return paragraphs


def _chunk_by_section(text: str) -> list[str]:
    """Heading-aware chunking. Splits on Markdown-style headings (#..######)
    OR lines that look like a heading (ALL CAPS + short). For production
    use Unstructured's element types (`Title`, `NarrativeText`) directly.
    # TODO(verify): use Unstructured element metadata instead of regex
    # when PARSER=unstructured — current impl ignores parser choice.
    """
    import re
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        is_heading = bool(re.match(r"^#{1,6}\s+.+", line)) or (
            line.isupper() and 3 <= len(line.split()) <= 12
        )
        if is_heading and current:
            sections.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    return [s for s in sections if s]


# ---------------------------------------------------------------- embedding

def _embed_all(chunks: list[str]) -> list[list[float]]:
    """Titan Text Embeddings v2 — invoke_model. Note: Titan v2 is a SINGLE
    input model — you invoke once per chunk. Throughput comes from Lambda
    concurrency + boto3 retries, not from batch API.

    For true batch embedding, consider Cohere Embed (accepts up to 96
    strings per call) — see swap matrix.
    """
    embeddings: list[list[float]] = []
    for chunk in chunks:
        body = json.dumps({
            "inputText":    chunk,
            "dimensions":   EMBED_DIMENSION,      # 1024 | 512 | 256
            "normalize":    True,                 # cosine-friendly
        })
        resp = bedrock.invoke_model(
            modelId=EMBED_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        payload = json.loads(resp["body"].read())
        embeddings.append(payload["embedding"])
    return embeddings


# ---------------------------------------------------------------- helpers

def _vector_key(doc_id: str, chunk_index: int) -> str:
    h = hashlib.sha256(f"{doc_id}#{chunk_index}".encode("utf-8")).hexdigest()
    return f"{doc_id}#{chunk_index}#{h[:12]}"


def _doc_id_from_key(key: str) -> str:
    # strip extension + any nested path
    base = key.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] or base


def _mark_status(doc_id: str, status: str) -> None:
    DOC_METADATA.update_item(
        Key={"doc_id": doc_id},
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": _now()},
    )


def _mark_failed(doc_id: str, reason: str) -> None:
    DOC_METADATA.update_item(
        Key={"doc_id": doc_id},
        UpdateExpression=(
            "SET #s = :s, failure_reason = :r, updated_at = :u"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "failed", ":r": reason, ":u": _now()},
    )


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
```

### 3.4 Monolith gotchas

- **EventBridge suffix filter array** — `object.key` suffix matches ANY of the listed entries. Easy to forget: missing `.DOCX` (uppercase) means upper-cased uploads silently skip ingestion. Either enforce lowercase at upload, or add both cases.
- **Textract async requires polling OR SNS completion** — the handler above polls for simplicity but burns Lambda time (up to 10 min). For production, use the SNS completion pattern: `NotificationChannel={"SNSTopicArn": ..., "RoleArn": ...}` → SNS triggers a second Lambda that reads results. Cuts average ingestion Lambda runtime from minutes to seconds.
- **Titan v2 is single-input**, NOT batch. `invoke_model` accepts one `inputText` per call. The `EMBED_BATCH_SIZE` env var is a misleading knob — it's only used for semantic-chunker candidate-similarity calls, not for ingestion. For true batch embedding, swap to Cohere (see swap matrix).
- **`EMBED_DIMENSION` must match the index `Dimension`.** Index was created with `dimension=1024` (immutable) — if you change `EMBED_DIMENSION` env var to 512 without recreating the index, every `PutVectors` call fails with `ValidationException`. Wire the dimension through `docs/template_params.md` → both stacks read the same value.
- **PutVectors is a hot path during ingestion** — a 500-chunk document = 500 Titan calls + 5 PutVectors batches. Titan TPS quota (default 2000 TPS in most regions, but soft-capped per account) is the rate limiter. Cap `reserved_concurrent_executions` to stay under the quota.
- **`bedrock:InvokeModel` resource ARN is regional + model-specific.** `arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0` — note the DOUBLE `::` (no account segment). This is easy to get wrong; mistyped ARNs produce `AccessDeniedException` at invoke time.
- **Transient vs permanent error classification matters.** A bad PDF (raises PermanentError) should NOT re-trigger the Lambda, or the DLQ fills with uploads that will never succeed. Re-raising `Exception` (transient) is the escape hatch.
- **30-day TTL on `doc_metadata`** is aggressive — if you need audit history longer than 30d, remove the TTL or mirror to a dedicated audit table. Vectors in S3 Vectors have no TTL themselves (must be deleted explicitly via `DeleteVectors`).
- **Idempotency is at the vector level, NOT at the ingestion level.** Two concurrent invocations on the same `doc_id` will both parse + embed + write the same vectors — wasted cost, and the race on the DDB status column means "indexed" can briefly revert to "parsing". If concurrent invocation is plausible (e.g. S3 → SQS fan-out), add a conditional `UpdateItem` gate on the first status write.

---

## 4. Micro-Stack Variant

**Use when:** `StorageStack` owns the raw bucket (with `event_bridge_enabled=True`); `VectorStoreStack` (from `DATA_S3_VECTORS` §4) owns the vector bucket + indexes + local CMK; `IngestionStack` owns the pipeline Lambda + `doc_metadata` table + DLQ.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** — `_LAMBDAS_ROOT` pattern.
2. **Never call `raw_bucket.grant_read(fn)` cross-stack.** Identity-side `s3:GetObject` on `f"{bucket_arn}/*"` + `kms:Decrypt` on the bucket's CMK ARN (from SSM).
3. **Never target cross-stack Lambda from a cross-stack EventBridge rule.** If the rule source bus is the account default bus, the rule can live in this stack (not `StorageStack`) and target the local Lambda — L2 `targets.LambdaFunction(local_fn)` is safe.
4. **Never import `CfnIndex` by object.** Read `vector_index_arn`, `vector_index_name`, `vector_bucket_name`, `vector_kms_arn` via SSM; grant `s3vectors:*` and `kms:*` identity-side on those ARN tokens.
5. **PermissionsBoundary + `iam:PassRole` with `iam:PassedToService`** on every role; especially if the ingestion Lambda ever passes a role to Step Functions (for a re-embed workflow) or Textract (for SNS completion channel — Textract's `RoleArn` MUST be passed with PassRole permission).

### 4.2 Dedicated `IngestionStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_dynamodb as ddb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class IngestionStack(cdk.Stack):
    """Doc-ingestion pipeline Lambda + doc_metadata table + DLQ.

    Cross-stack resources (raw bucket, raw bucket KMS, vector index, vector
    KMS) are imported by ARN via SSM parameter names. No cross-stack grant_*
    calls — identity-side only.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        raw_bucket_name_ssm: str,
        raw_bucket_arn_ssm: str,
        raw_bucket_kms_arn_ssm: str,
        vector_bucket_name_ssm: str,
        vector_index_name_ssm: str,
        vector_index_arn_ssm: str,
        vector_kms_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        embed_model_id: str = "amazon.titan-embed-text-v2:0",
        embed_dimension: int = 1024,
        chunk_size_tokens: int = 512,
        chunk_overlap: int = 64,
        chunk_strategy: str = "fixed",
        parser: str = "textract",
        reserved_concurrency: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-ingestion-{stage_name}", **kwargs)
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        raw_bucket_name  = ssm.StringParameter.value_for_string_parameter(
            self, raw_bucket_name_ssm
        )
        raw_bucket_arn   = ssm.StringParameter.value_for_string_parameter(
            self, raw_bucket_arn_ssm
        )
        raw_kms_arn      = ssm.StringParameter.value_for_string_parameter(
            self, raw_bucket_kms_arn_ssm
        )
        v_bucket_name    = ssm.StringParameter.value_for_string_parameter(
            self, vector_bucket_name_ssm
        )
        v_index_name     = ssm.StringParameter.value_for_string_parameter(
            self, vector_index_name_ssm
        )
        v_index_arn      = ssm.StringParameter.value_for_string_parameter(
            self, vector_index_arn_ssm
        )
        v_kms_arn        = ssm.StringParameter.value_for_string_parameter(
            self, vector_kms_arn_ssm
        )

        # Local CMK for this stack's DDB + SQS. Never share the vector CMK.
        cmk = kms.Key(
            self, "IngestionKey",
            alias=f"alias/{{project_name}}-ingestion-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        # A) doc_metadata table
        doc_metadata = ddb.Table(
            self, "DocMetadata",
            table_name=f"{{project_name}}-doc-metadata-{stage_name}",
            partition_key=ddb.Attribute(name="doc_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            time_to_live_attribute="ttl",
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery=(stage_name == "prod"),
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
        )
        doc_metadata.add_global_secondary_index(
            index_name="by-status",
            partition_key=ddb.Attribute(name="status",      type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="uploaded_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.KEYS_ONLY,
        )

        # B) DLQ
        dlq = sqs.Queue(
            self, "IngestDlq",
            queue_name=f"{{project_name}}-ingest-dlq-{stage_name}",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            retention_period=Duration.days(14),
        )

        # C) Ingestion Lambda
        log = logs.LogGroup(
            self, "IngestionLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-doc-ingestion-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        ingestion_fn = _lambda.Function(
            self, "IngestionFn",
            function_name=f"{{project_name}}-doc-ingestion-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "doc_ingestion")),
            memory_size=3008,
            timeout=Duration.minutes(15),
            log_group=log,
            tracing=_lambda.Tracing.ACTIVE,
            dead_letter_queue_enabled=True,
            dead_letter_queue=dlq,
            reserved_concurrent_executions=reserved_concurrency,
            environment={
                "RAW_BUCKET":         raw_bucket_name,
                "DOC_METADATA_TABLE": doc_metadata.table_name,
                "VECTOR_BUCKET_NAME": v_bucket_name,
                "VECTOR_INDEX_NAME":  v_index_name,
                "EMBED_MODEL_ID":     embed_model_id,
                "EMBED_DIMENSION":    str(embed_dimension),
                "CHUNK_SIZE_TOKENS":  str(chunk_size_tokens),
                "CHUNK_OVERLAP":      str(chunk_overlap),
                "CHUNK_STRATEGY":     chunk_strategy,
                "PARSER":             parser,
                "PUT_VECTORS_BATCH_SIZE": "100",
            },
        )

        # D) Identity-side grants.
        # Raw bucket — cross-stack, so identity-side S3 + KMS.
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[f"{raw_bucket_arn}/*"],
        ))
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[raw_kms_arn],
        ))
        # DDB — local, L2 safe.
        doc_metadata.grant_read_write_data(ingestion_fn)
        # Bedrock InvokeModel — resource ARN is regional + model-specific.
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{Aws.REGION}::foundation-model/{embed_model_id}",
            ],
        ))
        # Textract — all service-level ARNs.
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "textract:StartDocumentTextDetection",
                "textract:StartDocumentAnalysis",
                "textract:GetDocumentTextDetection",
                "textract:GetDocumentAnalysis",
                "textract:DetectDocumentText",
                "textract:AnalyzeDocument",
            ],
            resources=["*"],
        ))
        # S3 Vectors — identity-side, no L2 exists.
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "s3vectors:PutVectors",
                "s3vectors:GetVectors",
                "s3vectors:DeleteVectors",
                "s3vectors:QueryVectors",
                "s3vectors:GetIndex",
            ],
            resources=[v_index_arn],
        ))
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
            resources=[v_kms_arn],
        ))
        # DLQ
        dlq.grant_send_messages(ingestion_fn)

        iam.PermissionsBoundary.of(ingestion_fn.role).apply(permission_boundary)

        # E) EventBridge rule: S3 raw-doc ObjectCreated → ingestion.
        #    Rule lives in THIS stack; target is a local Lambda — L2 safe.
        events.Rule(
            self, "DocUploadedRule",
            rule_name=f"{{project_name}}-doc-uploaded-{stage_name}",
            event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [raw_bucket_name]},
                    "object": {"key": [
                        {"suffix": ".pdf"}, {"suffix": ".docx"},
                        {"suffix": ".txt"}, {"suffix": ".md"},
                    ]},
                },
            ),
            targets=[targets.LambdaFunction(ingestion_fn)],
        )

        # F) Publish the doc-metadata table ARN so RAG query Lambdas in
        #    ComputeStack can check "is this doc indexed yet?".
        ssm.StringParameter(
            self, "DocMetadataTableArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ingestion/doc_metadata_table_arn",
            string_value=doc_metadata.table_arn,
        )
        ssm.StringParameter(
            self, "DocMetadataTableNameParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ingestion/doc_metadata_table_name",
            string_value=doc_metadata.table_name,
        )
        ssm.StringParameter(
            self, "IngestDlqArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ingestion/dlq_arn",
            string_value=dlq.queue_arn,
        )

        self.ingestion_fn   = ingestion_fn
        self.doc_metadata   = doc_metadata
        self.dlq            = dlq
        self.cmk            = cmk

        CfnOutput(self, "IngestionFnArn",   value=ingestion_fn.function_arn)
        CfnOutput(self, "DocMetadataTable", value=doc_metadata.table_name)
        CfnOutput(self, "IngestDlqArn",     value=dlq.queue_arn)
```

### 4.3 Micro-stack gotchas

- **SSM token in `f"{raw_bucket_arn}/*"`** — works because CloudFormation resolves the `{{resolve:ssm:...}}` token inline before the `/*` suffix. Do NOT try to parse it in Python.
- **Textract SNS completion channel + `iam:PassRole`** — if you switch from polling to SNS-async completion, Textract needs a role it can assume to publish to SNS; the ingestion Lambda must have `iam:PassRole` on that role with `iam:PassedToService=textract.amazonaws.com` Condition. Easy to forget; fails silently (the SNS notification never arrives).
- **Lambda layer size budget** — `unstructured` + its deps typically exceed the 250 MB unzipped layer cap. Use Lambda container-image packaging (`_lambda.DockerImageFunction`) when PARSER=unstructured.
- **Bedrock model ARN double-colon** — `arn:aws:bedrock:{region}::foundation-model/...` has `::` (no account segment). CDK token resolution can mask this; eyeball the synth'd template.
- **DLQ retention 14 days** — after that, poisoned docs are gone. Pair with a DLQ-drain Lambda (see `EVENT_DRIVEN_PATTERNS` §6) that copies to an S3 audit bucket before redrive.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / single-format corpus (PDFs only, text-based) | §3 Monolith + `PARSER=pypdf` + `CHUNK_STRATEGY=fixed` + S3 Vectors |
| Scanned / image-heavy PDFs | `PARSER=textract` (OCR); add SNS completion channel; `# TODO(verify): Textract OCR cost per page in your region` |
| Heterogeneous corpus (pdf, docx, pptx, html, md) | `PARSER=unstructured` with container-image Lambda (layer size); optionally `CHUNK_STRATEGY=by_section` to exploit heading metadata |
| Long-form narrative docs, want semantic boundaries | `CHUNK_STRATEGY=semantic` — doubles+ embedding cost; prototype with LangChain SemanticChunker before committing |
| Structured docs (policy manuals, legal) with headings | `CHUNK_STRATEGY=by_section` + `PARSER=unstructured` to leverage element metadata |
| Want fully managed ingestion (no custom Lambda) | Swap to **Bedrock Knowledge Base on S3 Vectors** — Bedrock chunks + embeds + writes. CDK: `aws_bedrock.CfnKnowledgeBase` + `aws_bedrock.CfnDataSource`. Trade chunking control for zero-ops. See `LLMOPS_BEDROCK` |
| High-QPS query + managed ingest | Bedrock Knowledge Base on **OpenSearch Serverless** vector collection. See `LLMOPS_BEDROCK` |
| Reduced embedding cost | Titan v2 `dimensions=512` or `256` — recreate vector index with matching dim; re-embed whole corpus |
| True batch embedding | Swap embedding model to **Cohere Embed v3** (`cohere.embed-multilingual-v3`) — accepts up to 96 texts per `invoke_model` call; ~5x faster than Titan for large ingests |
| Audit / reproducibility requirement | Mirror every doc to a versioned audit bucket; write `ingested_embedding_model` + `ingested_chunker_version` to `doc_metadata`; never delete rows |
| Re-embed whole corpus (model change) | Iterate `doc_metadata` via by-status GSI → for each doc re-send to ingestion queue. Deterministic keys mean `PutVectors` overwrites; zero-downtime swap via SSM-driven index-name switch |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_PATTERN_DOC_INGESTION_RAG.py`. Offline; `cdk.Stack` as deps stub.

```python
"""SOP verification — IngestionStack synthesizes with:
- ingestion Lambda with correct env + policies
- doc_metadata DDB table
- DLQ
- EventBridge rule on raw-doc ObjectCreated"""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-west-2")


def test_ingestion_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.ingestion_stack import IngestionStack
    stack = IngestionStack(
        app, stage_name="dev",
        raw_bucket_name_ssm="/test/storage/raw_bucket_name",
        raw_bucket_arn_ssm="/test/storage/raw_bucket_arn",
        raw_bucket_kms_arn_ssm="/test/storage/raw_bucket_kms_arn",
        vector_bucket_name_ssm="/test/vectors/bucket_name",
        vector_index_name_ssm="/test/vectors/rag_main_index_name",
        vector_index_arn_ssm="/test/vectors/rag_main_index_arn",
        vector_kms_arn_ssm="/test/vectors/kms_arn",
        permission_boundary=boundary,
        embed_model_id="amazon.titan-embed-text-v2:0",
        embed_dimension=1024,
        chunk_size_tokens=512,
        chunk_overlap=64,
        chunk_strategy="fixed",
        parser="textract",
        reserved_concurrency=10,
        env=env,
    )
    t = Template.from_stack(stack)

    t.resource_count_is("AWS::Lambda::Function", 1)
    t.resource_count_is("AWS::DynamoDB::Table",  1)
    t.resource_count_is("AWS::SQS::Queue",       1)
    t.resource_count_is("AWS::Events::Rule",     1)
    t.resource_count_is("AWS::KMS::Key",         1)

    # Lambda env wired to SSM tokens
    t.has_resource_properties("AWS::Lambda::Function", Match.object_like({
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "EMBED_MODEL_ID":    "amazon.titan-embed-text-v2:0",
                "EMBED_DIMENSION":   "1024",
                "CHUNK_SIZE_TOKENS": "512",
                "CHUNK_OVERLAP":     "64",
                "CHUNK_STRATEGY":    "fixed",
                "PARSER":            "textract",
            }),
        }),
        "ReservedConcurrentExecutions": 10,
    }))

    # EventBridge rule filters on Object Created
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "EventPattern": Match.object_like({
            "source":      ["aws.s3"],
            "detail-type": ["Object Created"],
        }),
    }))

    # 3 SSM params published (doc_metadata table arn + name + dlq arn)
    t.resource_count_is("AWS::SSM::Parameter", 3)
```

---

## 7. References

- `docs/template_params.md` — `EMBED_MODEL_ID`, `EMBED_DIMENSION`, `CHUNK_SIZE_TOKENS`, `CHUNK_OVERLAP`, `CHUNK_STRATEGY` (`fixed`|`semantic`|`by_section`), `PARSER` (`textract`|`unstructured`|`pypdf`), `INGESTION_RESERVED_CONCURRENCY`, `DOC_METADATA_TTL_DAYS`
- `docs/Feature_Roadmap.md` — feature IDs `DI-10` (raw bucket + EventBridge wiring), `DI-11` (parser abstraction), `DI-12` (chunker abstraction), `DI-13` (Titan v2 embedding), `DI-14` (PutVectors batched write), `DI-15` (doc_metadata table + status tracking), `DI-16` (DLQ + reprocessor)
- AWS docs:
  - [Amazon S3 Vectors overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
  - [S3 Vectors getting started (Titan v2 + PutVectors + QueryVectors tutorial)](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-getting-started.html)
  - [S3 Vectors best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-best-practices.html)
  - [S3 Vectors as Bedrock Knowledge Base store](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html)
  - [boto3 `s3vectors` client reference](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3vectors.html)
  - [Amazon Textract asynchronous operations](https://docs.aws.amazon.com/textract/latest/dg/async.html)
  - [Bedrock Titan Text Embeddings v2 model reference](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)
  - [`awslabs/s3vectors-embed-cli` (prototyping)](https://github.com/awslabs/s3vectors-embed-cli)
- Related SOPs:
  - `DATA_S3_VECTORS` — vector bucket + index CDK, IAM, KMS (storage details live here)
  - `LLMOPS_BEDROCK` — Bedrock `invoke_model` patterns, Titan + Cohere model IDs, Bedrock Knowledge Base alternative
  - `EVENT_DRIVEN_PATTERNS` — S3 → EventBridge → Lambda canonical wiring; DLQ reprocessor (§6) for failed-doc redrive
  - `PATTERN_BATCH_UPLOAD` — many-to-one intake pattern when docs arrive as a ZIP + manifest (batches of documents into this pipeline)
  - `LAYER_DATA` — raw bucket `event_bridge_enabled=True` + KMS defaults
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers, PermissionsBoundary

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — document → parse → chunk → embed → PutVectors RAG ingestion pipeline. Plug-points for parser (Textract async poll / Unstructured / PyPDF), chunker (fixed token-approximate / semantic placeholder / heading-aware by_section), and vector store (S3 Vectors primary with Bedrock KB + OpenSearch Serverless swap rows). Canonical `doc_metadata` DDB table with status lifecycle (uploaded → parsing → embedding → indexed / failed) and `by-status` GSI. Deterministic sha256(doc_id#chunk_index) vector key for idempotent re-ingest. Titan v2 1024-dim embedding with EMBED_DIMENSION=index.Dimension contract. Reserved-concurrency Bedrock TPS cap. Worked example uses Textract + fixed + S3 Vectors. Created to fill gap surfaced by RAG-chatbot kit design, grounded in AWS S3 Vectors GA + Bedrock + Textract documentation. |
