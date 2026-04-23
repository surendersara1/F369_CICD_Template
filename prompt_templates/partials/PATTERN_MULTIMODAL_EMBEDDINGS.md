# SOP — Multimodal embeddings (images, diagrams, documents via Titan Multimodal)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · Amazon Titan Multimodal Embeddings G1 (1024 / 384 / 256 dims) · Amazon S3 Vectors (GA 2025) as primary storage · OpenSearch Serverless k-NN for hybrid search secondary · Amazon Textract for PDF OCR pre-processing · `bedrock-runtime.invoke_model` · Amazon Rekognition PII-redaction auxiliary · `aws_cdk.aws_s3vectors` L1 (no L2)

---

## 1. Purpose

- Provide the deep-dive for **multimodal embeddings** — the pattern that turns images (PNG / JPEG), PDF pages, engineering diagrams, whiteboard photos, and scanned documents into **vectors in the same semantic space as text queries**. The killer capability: cross-modal search — **text query → image results** ("show me transformer schematics") and **image query → text results** ("find the maintenance notes that describe this diagram").
- Codify the **Titan Multimodal Embeddings G1** API — single endpoint accepting `inputText` OR `inputImage` (base64) OR both simultaneously; returns a unit-normalised vector in one of three dims (1024 / 384 / 256). Because text and image embeddings live in the same space, similarity works cross-modally at cosine.
- Codify the **dimension decision** — 1024 for high-fidelity visual search (engineering drawings, dense-text slides); 384 for product-catalog / stock-photo search; 256 for mobile / on-device / archival-scale.
- Codify the **image pre-processing contract** — max 25 MB, max 2048×2048 px (larger auto-downsampled; quality loss). For PDFs: use Textract `DetectDocumentText` or `AnalyzeDocument` first, OR rasterise each page and embed as image. For diagrams with text (flowcharts, schematics): embed TWICE (once as image, once as OCR'd text) and store BOTH vectors with a shared `document_id` — hybrid query gets both hits.
- Codify **PII and EXIF scrubbing** — images often carry GPS / device / owner metadata in EXIF. Strip before embedding (Pillow's `Image.getexif` + nuke). Photos may contain human faces → run Rekognition `DetectFaces` and blur/crop before embedding for GDPR compliance. Whiteboard photos may contain whiteboard-attached sticky notes with PII.
- Codify the **batch ingestion pipeline** — S3 upload event → Lambda → (optionally) Textract → Titan embed → S3 Vectors. For bulk backfill: Step Functions + Lambda map for parallelism; rate-limited to Titan's account quota.
- Codify the **hybrid query pattern** — text query + optional image query fused into a single vector (weighted average OR concatenation), with metadata filter pushdown (`source_type`, `doc_id`, `section`, `access_group`).
- Codify the **cross-kit use cases** integral to the lakehouse + other kits:
  - **AI-Native Lakehouse (this kit)**: engineering diagrams embedded alongside table metadata; agent can find a diagram referenced in a doc + the table it describes.
  - **Acoustic Fault Diagnostic** (`kits/acoustic-fault-diagnostic-agent`): equipment photos embedded; audio similarity search finds the matching equipment photo.
  - **RAG Chatbot** (`kits/rag-chatbot-per-client`): document pages with figures; image search augments text RAG ("the chart on page 12").
  - **Deep Research Agent** (`kits/deep-research-agent`): research papers with figures; agent's BrowserTool captures a chart → embeds → finds related papers.
- Include when the SOW signals: "multimodal search", "image search", "find images by text", "diagram search", "engineering drawings", "document figures", "visual similarity", "reverse image search", "product image catalog".
- This partial is the **VISUAL/MULTIMODAL index layer**. Pairs with `DATA_S3_VECTORS` (storage), `PATTERN_CATALOG_EMBEDDINGS` (text-only catalog sibling), `PATTERN_DOC_INGESTION_RAG` (document chunking for text-side), and kit-level consumers.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the vector bucket + single image index + ingestion Lambda + query Lambda | **§3 Monolith Variant** |
| `MultimodalIndexStack` owns the vector bucket + indexes + ingestion pipeline + Textract policies; `AppStack` / `AgentStack` consume query-side | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Titan Multimodal's image size + rate limits.** Base64-encoded images in `inputImage` inflate Lambda payload ~33%; for a 25 MB image, the invoke body is ~35 MB (under Lambda's 6 MB sync limit means you need async or direct-from-S3 read). Production pipelines stream from S3 inside the Lambda; never marshal images via API Gateway.
2. **Textract is request-per-page, not per-document.** A 200-page PDF is 200 Textract jobs. Ownership of the Textract output bucket + KMS + the Textract IAM role belongs in the producer stack.
3. **Vector index `Dimension` immutability.** Picking 1024 vs 384 vs 256 is a one-time decision — changing it means recreating the index. Keep the index in the producer stack so the decision is not spread across consumers.
4. **EXIF / PII scrubbing is a cross-cutting concern.** The scrubbing + embedding Lambda lives in one place (producer); consumers never see a raw image bytestream, only vectors + metadata + redacted preview S3 URLs.
5. **Preview (thumbnail) generation is an optional storage-side artifact.** The producer generates a 256×256 thumbnail and stores it alongside the original in a dedicated `previews/` prefix; consumers reference the S3 URL in query results. If the producer owns the generation, deletion stays consistent.

Micro-Stack fixes by: (a) owning vector bucket + index(es) + Titan + Textract + Rekognition invocation Lambdas + preview generation + local CMK in `MultimodalIndexStack`; (b) publishing `VectorBucketName`, `IndexArn` (per modality), `PreviewBucketName`, `CmkArn` via SSM; (c) consumers grant themselves `s3vectors:QueryVectors` + `bedrock:InvokeModel` + `s3:GetObject` (preview bucket).

---

## 3. Monolith Variant

### 3.1 Architecture

```
  ┌──────────────────────────────────────────────────────────────────┐
  │   S3 Upload Bucket: {project}-multimodal-raw-{stage}             │
  │     prefix: images/    (PNG, JPEG, WEBP)                         │
  │     prefix: pdfs/      (multi-page)                              │
  │     prefix: diagrams/  (engineering, flowcharts)                 │
  │     event: s3:ObjectCreated:* → EB rule → IngestFn               │
  └──────────────────────────────────────────────────────────────────┘
                  │
                  ▼
  IngestFn (Lambda, Docker image — Pillow + PyMuPDF + boto3)
    │
    ├── 1) Sniff content-type, route to branch:
    │     image         → branch A
    │     pdf           → branch B (rasterise pages → branch A per page)
    │     diagram       → branch A + branch C (OCR text too)
    │
    ├── branch A: pure image
    │     ├── Pillow: strip EXIF, auto-orient, resize to 2048px max
    │     ├── Rekognition DetectFaces → if faces, blur regions
    │     ├── Compute SHA256 for idempotent key
    │     ├── bedrock.invoke_model(titan-mm-g1, input_image=b64(img))
    │     └── s3vectors.PutVectors(IDX_IMAGE_ARN, [vector])
    │
    ├── branch B: pdf-to-pages
    │     ├── PyMuPDF: render each page at 200 dpi
    │     ├── Textract AnalyzeDocument: extract text blocks per page
    │     └── per-page: branch A (image vector) + branch C (text vector)
    │
    └── branch C: text from OCR / caption
          ├── Build embedding text ("page 12 of doc.pdf: {extracted}")
          ├── bedrock.invoke_model(titan-mm-g1, input_text=text)
          └── s3vectors.PutVectors(IDX_TEXT_ARN, [vector])
                  │
                  ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Vector Bucket: {project}-multimodal-vectors-{stage}             │
  │                                                                  │
  │  Index: images            (dim 1024, cosine)                     │
  │    FilterableMetadata:                                           │
  │      source_type   TEXT    (image | pdf_page | diagram)          │
  │      doc_id        TEXT                                          │
  │      page          NUMBER  (1-indexed, NULL for standalone img)  │
  │      access_group  TEXT    (LF-Tag-aligned)                      │
  │      uploaded_at   NUMBER  (epoch)                               │
  │    NonFilterable:  source_uri, thumbnail_uri, caption            │
  │                                                                  │
  │  Index: text              (dim 1024, cosine)                     │
  │    FilterableMetadata: same shape (without thumbnail)            │
  │    NonFilterable:  source_text, source_uri                       │
  │                                                                  │
  │  (Same dimension across indexes → single query vector can match  │
  │   both modalities if the consumer wants cross-modal.)            │
  └──────────────────────────────────────────────────────────────────┘
                  ▲
                  │  s3vectors.QueryVectors(index_arn, queryVector,
                  │                         topK=10, filter={...},
                  │                         returnMetadata=true)
  QueryFn / AgentFn (text OR image OR both)

  ┌──────────────────────────────────────────────────────────────────┐
  │  Preview Bucket: {project}-multimodal-previews-{stage}           │
  │    256×256 thumbnail per image (consumer reads via signed URL)   │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_multimodal_embeddings()` method body

```python
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_s3vectors as s3v,
    aws_sqs as sqs,
)


def _create_multimodal_embeddings(self, stage: str) -> None:
    """Monolith variant. Provisions raw + preview buckets, 2 vector indexes
    (image + text), ingest Lambda (Docker image — Pillow + PyMuPDF + boto3),
    query Lambda."""

    # A) Local CMK.
    self.mm_cmk = kms.Key(
        self, "MmCmk",
        alias=f"alias/{{project_name}}-multimodal-{stage}",
        enable_key_rotation=True,
        removal_policy=(RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY),
    )

    # B) Raw bucket + preview bucket. Separate buckets — raw retention and
    #    preview retention differ; raw often immutable (compliance), previews
    #    regenerable.
    self.raw_bucket = s3.Bucket(
        self, "MmRawBucket",
        bucket_name=f"{{project_name}}-multimodal-raw-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.mm_cmk,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,                       # allow PII-redaction rollback
        removal_policy=RemovalPolicy.RETAIN,  # never auto-delete user data
        event_bridge_enabled=True,
    )
    self.preview_bucket = s3.Bucket(
        self, "MmPreviewBucket",
        bucket_name=f"{{project_name}}-multimodal-previews-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.mm_cmk,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=(RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY),
        auto_delete_objects=(stage != "prod"),
        lifecycle_rules=[
            s3.LifecycleRule(
                id="expire-old-previews",
                enabled=True,
                expiration=Duration.days(180),
            ),
        ],
    )

    # C) Vector bucket + 2 indexes (image + text). Same dim so
    #    cross-modal similarity works at cosine.
    self.mm_vector_bucket = s3v.CfnVectorBucket(
        self, "MmVectorBucket",
        vector_bucket_name=f"{{project_name}}-multimodal-vectors-{stage}",
        encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
            sse_type="aws:kms",
            kms_key_arn=self.mm_cmk.key_arn,
        ),
    )

    common_filter_keys = [
        s3v.CfnIndex.MetadataKeyProperty(name="source_type",  type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="doc_id",       type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="page",         type="NUMBER"),
        s3v.CfnIndex.MetadataKeyProperty(name="access_group", type="TEXT"),
        s3v.CfnIndex.MetadataKeyProperty(name="uploaded_at",  type="NUMBER"),
    ]
    self.idx_image = s3v.CfnIndex(
        self, "IdxImage",
        vector_bucket_name=self.mm_vector_bucket.attr_vector_bucket_name,
        index_name="images",
        data_type="float32",
        dimension=1024,
        distance_metric="cosine",
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_uri", "thumbnail_uri", "caption"],
        ),
        filterable_metadata_keys=common_filter_keys,
    )
    self.idx_text = s3v.CfnIndex(
        self, "IdxText",
        vector_bucket_name=self.mm_vector_bucket.attr_vector_bucket_name,
        index_name="text",
        data_type="float32",
        dimension=1024,
        distance_metric="cosine",
        metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=["source_text", "source_uri"],
        ),
        filterable_metadata_keys=common_filter_keys,
    )
    for idx in (self.idx_image, self.idx_text):
        idx.add_dependency(self.mm_vector_bucket)

    # D) DLQ for ingest (images can fail Textract, rate-limits, corrupt files).
    self.ingest_dlq = sqs.Queue(
        self, "MmIngestDlq",
        encryption=sqs.QueueEncryption.KMS_MANAGED,
        retention_period=Duration.days(14),
    )

    # E) Ingest Lambda — Docker image (Pillow + PyMuPDF + boto3).
    #    Docker because Pillow requires platform-specific binaries and
    #    PyMuPDF is not in a Lambda layer by default.
    self.ingest_image = _lambda.DockerImageFunction(
        self, "MmIngestFn",
        function_name=f"{{project_name}}-multimodal-ingest-{stage}",
        code=_lambda.DockerImageCode.from_image_asset(
            directory=str(Path(__file__).parent.parent / "lambda_docker" / "mm_ingest"),
            platform=_lambda.Platform.LINUX_AMD64,
        ),
        architecture=_lambda.Architecture.X86_64,     # Textract SDK quirk on ARM
        memory_size=6144,                              # 6 GB — image processing is RAM-hungry
        timeout=Duration.minutes(10),
        reserved_concurrent_executions=20,
        dead_letter_queue=self.ingest_dlq,
        environment={
            "RAW_BUCKET":          self.raw_bucket.bucket_name,
            "PREVIEW_BUCKET":      self.preview_bucket.bucket_name,
            "VECTOR_BUCKET_NAME":  self.mm_vector_bucket.attr_vector_bucket_name,
            "IDX_IMAGE_ARN":       self.idx_image.attr_index_arn,
            "IDX_TEXT_ARN":        self.idx_text.attr_index_arn,
            "EMBED_MODEL_ID":      "amazon.titan-embed-image-v1",
            "EMBED_DIM":           "1024",
            "TEXTRACT_MAX_PAGES":  "500",
            "MAX_IMAGE_DIM_PX":    "2048",
        },
    )

    # Identity-side grants — S3, Bedrock (Titan), Rekognition, Textract, KMS, s3vectors.
    self.raw_bucket.grant_read(self.ingest_image)
    self.preview_bucket.grant_write(self.ingest_image)
    self.ingest_image.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel"],
        resources=[
            f"arn:aws:bedrock:{Stack.of(self).region}::"
            f"foundation-model/amazon.titan-embed-image-v1",
        ],
    ))
    self.ingest_image.add_to_role_policy(iam.PolicyStatement(
        actions=["rekognition:DetectFaces", "rekognition:DetectModerationLabels"],
        resources=["*"],     # Rekognition requires "*" for these
    ))
    self.ingest_image.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "textract:AnalyzeDocument", "textract:DetectDocumentText",
            "textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis",
            "textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection",
        ],
        resources=["*"],
    ))
    self.ingest_image.add_to_role_policy(iam.PolicyStatement(
        actions=["s3vectors:PutVectors", "s3vectors:DeleteVectors",
                 "s3vectors:GetVectors", "s3vectors:QueryVectors"],
        resources=[self.idx_image.attr_index_arn, self.idx_text.attr_index_arn],
    ))
    self.ingest_image.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.mm_cmk.key_arn],
    ))

    # F) Wire S3 → EventBridge → Ingest (one rule, matches all prefixes).
    events.Rule(
        self, "RuleMmRawUpload",
        description="Any new object in raw bucket triggers multimodal ingest.",
        event_pattern=events.EventPattern(
            source=["aws.s3"],
            detail_type=["Object Created"],
            detail={
                "bucket": {"name": [self.raw_bucket.bucket_name]},
            },
        ),
        targets=[targets.LambdaFunction(self.ingest_image, dead_letter_queue=self.ingest_dlq)],
    )

    # G) Query Lambda — stateless, zero-dep (no Docker).
    self.query_fn = _lambda.Function(
        self, "MmQueryFn",
        function_name=f"{{project_name}}-multimodal-query-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        code=_lambda.Code.from_asset(
            str(Path(__file__).parent.parent / "lambda" / "mm_query"),
        ),
        handler="handler.lambda_handler",
        memory_size=1024,
        timeout=Duration.seconds(30),
        environment={
            "IDX_IMAGE_ARN":   self.idx_image.attr_index_arn,
            "IDX_TEXT_ARN":    self.idx_text.attr_index_arn,
            "EMBED_MODEL_ID":  "amazon.titan-embed-image-v1",
            "EMBED_DIM":       "1024",
            "PREVIEW_BUCKET":  self.preview_bucket.bucket_name,
        },
    )
    self.query_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
        resources=[self.idx_image.attr_index_arn, self.idx_text.attr_index_arn],
    ))
    self.query_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel"],
        resources=[
            f"arn:aws:bedrock:{Stack.of(self).region}::"
            f"foundation-model/amazon.titan-embed-image-v1",
        ],
    ))
    self.preview_bucket.grant_read(self.query_fn)
    self.query_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.mm_cmk.key_arn],
    ))

    # H) Outputs.
    CfnOutput(self, "MmRawBucket",      value=self.raw_bucket.bucket_name)
    CfnOutput(self, "MmPreviewBucket",  value=self.preview_bucket.bucket_name)
    CfnOutput(self, "IdxImageArn",      value=self.idx_image.attr_index_arn)
    CfnOutput(self, "IdxTextArn",       value=self.idx_text.attr_index_arn)
```

### 3.3 Ingest Lambda — Docker image contents

```dockerfile
# lambda_docker/mm_ingest/Dockerfile
FROM public.ecr.aws/lambda/python:3.12

RUN microdnf install -y gcc libjpeg-turbo-devel zlib-devel && microdnf clean all
RUN pip install --no-cache-dir \
    "Pillow>=10.2" \
    "PyMuPDF>=1.24" \
    "boto3>=1.34"

COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
```

```python
# lambda_docker/mm_ingest/handler.py
import base64
import hashlib
import io
import json
import os
import time
from typing import Any
from urllib.parse import quote_plus

import boto3
from PIL import Image, ImageFilter
try:
    import fitz               # PyMuPDF
except ImportError:
    fitz = None


RAW_BUCKET         = os.environ["RAW_BUCKET"]
PREVIEW_BUCKET     = os.environ["PREVIEW_BUCKET"]
IDX_IMAGE_ARN      = os.environ["IDX_IMAGE_ARN"]
IDX_TEXT_ARN       = os.environ["IDX_TEXT_ARN"]
EMBED_MODEL_ID     = os.environ["EMBED_MODEL_ID"]
EMBED_DIM          = int(os.environ["EMBED_DIM"])
MAX_IMAGE_DIM_PX   = int(os.environ["MAX_IMAGE_DIM_PX"])

s3          = boto3.client("s3")
s3v         = boto3.client("s3vectors")
bedrock     = boto3.client("bedrock-runtime")
rekognition = boto3.client("rekognition")
textract    = boto3.client("textract")


# ---- scrubbers -------------------------------------------------------------

def _strip_exif_and_orient(img: Image.Image) -> Image.Image:
    # Rotate per EXIF orientation tag then drop all EXIF.
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img)
    data = img.getdata()
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(data))
    return clean


def _blur_faces(img: Image.Image, bucket: str, key: str) -> Image.Image:
    # Rekognition DetectFaces operates on the S3 object (up to 5 MB JPG) —
    # for this function the image is ALREADY in S3 (raw bucket), so pass ref.
    try:
        resp = rekognition.detect_faces(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            Attributes=["DEFAULT"],
        )
    except Exception:
        return img  # if Rekognition fails (unsupported format), skip and continue

    w, h = img.size
    for face in resp.get("FaceDetails", []):
        b = face["BoundingBox"]
        l, t = int(b["Left"] * w), int(b["Top"] * h)
        rw, rh = int(b["Width"] * w), int(b["Height"] * h)
        region = img.crop((l, t, l + rw, t + rh)).filter(ImageFilter.GaussianBlur(radius=30))
        img.paste(region, (l, t))
    return img


def _resize_to_max(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    ratio = max_dim / max(w, h)
    return img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)


def _thumbnail(img: Image.Image) -> Image.Image:
    t = img.copy()
    t.thumbnail((256, 256), Image.LANCZOS)
    return t


# ---- embedding calls -------------------------------------------------------

def _embed_image(img: Image.Image) -> list[float]:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    body = json.dumps({
        "inputImage": base64.b64encode(buf.getvalue()).decode(),
        "embeddingConfig": {"outputEmbeddingLength": EMBED_DIM},
    })
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=body,
        accept="application/json",
        contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


def _embed_text(text: str) -> list[float]:
    body = json.dumps({
        "inputText": text,
        "embeddingConfig": {"outputEmbeddingLength": EMBED_DIM},
    })
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=body,
        accept="application/json",
        contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


# ---- branches --------------------------------------------------------------

def _process_image(bucket: str, key: str, doc_id: str, meta: dict[str, Any]) -> None:
    obj = s3.get_object(Bucket=bucket, Key=key)
    img = Image.open(io.BytesIO(obj["Body"].read()))
    img = _strip_exif_and_orient(img)
    img = _blur_faces(img, bucket, key)
    img = _resize_to_max(img, MAX_IMAGE_DIM_PX)

    # Preview — 256×256 thumbnail in preview bucket, KMS-encrypted.
    thumb = _thumbnail(img)
    thumb_buf = io.BytesIO()
    thumb.convert("RGB").save(thumb_buf, format="JPEG", quality=85)
    thumb_key = f"thumbnails/{doc_id}.jpg"
    s3.put_object(
        Bucket=PREVIEW_BUCKET, Key=thumb_key,
        Body=thumb_buf.getvalue(), ContentType="image/jpeg",
        ServerSideEncryption="aws:kms",
    )

    # Embedding.
    vec = _embed_image(img)
    s3v.put_vectors(
        indexArn=IDX_IMAGE_ARN,
        vectors=[{
            "key":      doc_id,
            "data":     vec,
            "metadata": {
                "source_type":   meta["source_type"],
                "doc_id":        doc_id,
                "page":          meta.get("page", 0),   # 0 = standalone
                "access_group":  meta.get("access_group", "default"),
                "uploaded_at":   int(time.time()),
                "source_uri":    f"s3://{bucket}/{key}",
                "thumbnail_uri": f"s3://{PREVIEW_BUCKET}/{thumb_key}",
                "caption":       meta.get("caption", ""),
            },
        }],
    )


def _process_pdf(bucket: str, key: str, doc_id: str, meta: dict[str, Any]) -> None:
    if not fitz:
        raise RuntimeError("PyMuPDF not installed — rebuild the Docker image")

    obj = s3.get_object(Bucket=bucket, Key=key)
    pdf_bytes = obj["Body"].read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    # Textract — one async job for the whole PDF (max 500 pages).
    start = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
    )
    job_id = start["JobId"]
    # Poll — in practice use Textract SNS completion callback.
    for _ in range(120):
        s = textract.get_document_text_detection(JobId=job_id)
        if s["JobStatus"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(2)
    if s["JobStatus"] != "SUCCEEDED":
        raise RuntimeError(f"Textract job {job_id} failed: {s.get('StatusMessage')}")

    # Aggregate blocks by page.
    text_by_page: dict[int, list[str]] = {}
    next_token = None
    while True:
        args = {"JobId": job_id}
        if next_token:
            args["NextToken"] = next_token
        resp = textract.get_document_text_detection(**args)
        for b in resp["Blocks"]:
            if b["BlockType"] == "LINE":
                text_by_page.setdefault(b.get("Page", 1), []).append(b["Text"])
        next_token = resp.get("NextToken")
        if not next_token:
            break

    # Per page: embed text + embed page render.
    for page_num, page in enumerate(doc, start=1):
        page_text = "\n".join(text_by_page.get(page_num, []))

        # --- text vector
        if page_text.strip():
            t_vec = _embed_text(page_text[:4000])     # Titan has a text cap
            s3v.put_vectors(
                indexArn=IDX_TEXT_ARN,
                vectors=[{
                    "key":  f"{doc_id}#p{page_num}#text",
                    "data": t_vec,
                    "metadata": {
                        "source_type":  "pdf_page",
                        "doc_id":       doc_id,
                        "page":         page_num,
                        "access_group": meta.get("access_group", "default"),
                        "uploaded_at":  int(time.time()),
                        "source_text":  page_text[:4000],
                        "source_uri":   f"s3://{bucket}/{key}#page={page_num}",
                    },
                }],
            )

        # --- image vector (page render at 200 dpi)
        pix = page.get_pixmap(matrix=fitz.Matrix(200/72, 200/72))
        img = Image.open(io.BytesIO(pix.tobytes(output="jpeg")))
        img = _resize_to_max(img, MAX_IMAGE_DIM_PX)
        i_vec = _embed_image(img)
        s3v.put_vectors(
            indexArn=IDX_IMAGE_ARN,
            vectors=[{
                "key":  f"{doc_id}#p{page_num}#image",
                "data": i_vec,
                "metadata": {
                    "source_type":   "pdf_page",
                    "doc_id":        doc_id,
                    "page":          page_num,
                    "access_group":  meta.get("access_group", "default"),
                    "uploaded_at":   int(time.time()),
                    "source_uri":    f"s3://{bucket}/{key}#page={page_num}",
                    "thumbnail_uri": "",    # page renders aren't thumbnailed
                    "caption":       "",
                },
            }],
        )


def lambda_handler(event, _ctx):
    """EventBridge S3 ObjectCreated event."""
    bucket = event["detail"]["bucket"]["name"]
    key    = event["detail"]["object"]["key"]
    # doc_id — stable across re-runs. SHA256 of the key; replace on overwrite.
    doc_id = hashlib.sha256(key.encode()).hexdigest()[:24]

    # Minimal metadata — in practice, read x-amz-meta-* or a sidecar JSON.
    meta = {
        "access_group": "default",
        "caption":      "",
    }

    prefix = key.split("/", 1)[0] if "/" in key else ""
    if prefix == "images" or prefix == "diagrams":
        meta["source_type"] = prefix.rstrip("s")      # image | diagram
        _process_image(bucket, key, doc_id, meta)
    elif prefix == "pdfs":
        meta["source_type"] = "pdf_page"
        _process_pdf(bucket, key, doc_id, meta)
    else:
        return {"skipped": True, "reason": f"unknown prefix {prefix}"}

    return {"ingested": True, "doc_id": doc_id, "key": key}
```

### 3.4 Query Lambda — text OR image OR both (hybrid)

```python
# lambda/mm_query/handler.py
import base64
import json
import os
from typing import Any

import boto3


IDX_IMAGE_ARN   = os.environ["IDX_IMAGE_ARN"]
IDX_TEXT_ARN    = os.environ["IDX_TEXT_ARN"]
EMBED_MODEL_ID  = os.environ["EMBED_MODEL_ID"]
EMBED_DIM       = int(os.environ["EMBED_DIM"])
PREVIEW_BUCKET  = os.environ["PREVIEW_BUCKET"]

s3      = boto3.client("s3")
s3v     = boto3.client("s3vectors")
bedrock = boto3.client("bedrock-runtime")


def _embed(*, text: str | None = None, image_b64: str | None = None) -> list[float]:
    body: dict[str, Any] = {"embeddingConfig": {"outputEmbeddingLength": EMBED_DIM}}
    if text:       body["inputText"]  = text
    if image_b64:  body["inputImage"] = image_b64
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps(body),
        accept="application/json",
        contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


def _signed_preview(thumb_uri: str, ttl_s: int = 300) -> str:
    if not thumb_uri.startswith(f"s3://{PREVIEW_BUCKET}/"):
        return ""
    key = thumb_uri.split("/", 3)[3]
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": PREVIEW_BUCKET, "Key": key},
        ExpiresIn=ttl_s,
    )


def lambda_handler(event, _ctx):
    """event = {
        'text':           'transformer one-line schematic',
        'image_b64':      None,                 # or base64 image bytes
        'modalities':     ['image','text'],     # which indexes to hit
        'top_k':          10,
        'filter':         {'access_group': 'engineering'},
    }"""
    q_vec = _embed(
        text=event.get("text"),
        image_b64=event.get("image_b64"),
    )

    out: dict[str, list] = {"image": [], "text": []}

    if "image" in event.get("modalities", ["image"]):
        im = s3v.query_vectors(
            indexArn=IDX_IMAGE_ARN, queryVector=q_vec,
            topK=event.get("top_k", 10),
            filter=event.get("filter", {}),
            returnMetadata=True, returnDistance=True,
        )["matches"]
        out["image"] = [
            {"key":           m["key"],
             "distance":      m["distance"],
             "source_uri":    m["metadata"]["source_uri"],
             "thumbnail_url": _signed_preview(m["metadata"].get("thumbnail_uri", "")),
             "source_type":   m["metadata"]["source_type"],
             "page":          m["metadata"].get("page"),
             "caption":       m["metadata"].get("caption")}
            for m in im
        ]

    if "text" in event.get("modalities", []):
        tx = s3v.query_vectors(
            indexArn=IDX_TEXT_ARN, queryVector=q_vec,
            topK=event.get("top_k", 10),
            filter=event.get("filter", {}),
            returnMetadata=True, returnDistance=True,
        )["matches"]
        out["text"] = [
            {"key":         m["key"],
             "distance":    m["distance"],
             "source_uri":  m["metadata"]["source_uri"],
             "source_text": m["metadata"].get("source_text", "")[:400],
             "page":        m["metadata"].get("page")}
            for m in tx
        ]

    return out
```

### 3.5 Monolith gotchas

1. **`inputImage` is base64-encoded.** A 10 MB PNG inflates to ~14 MB body; Lambda sync-invoke ceiling is 6 MB REQUEST / 6 MB RESPONSE. If the Lambda reads from S3 (as shown) the payload stays small, but if you are tempted to pass the image through API Gateway, you will hit the 10 MB API-GW body cap first. Always stream from S3.
2. **Titan Multimodal auto-downsamples > 2048×2048 before embedding.** If your use case depends on fine detail (engineering drawings with tiny text), pre-tile the image — slice into 2048² tiles with 10% overlap and embed each tile separately with `(doc_id, tile_x, tile_y)` in metadata.
3. **Text length cap for `inputText` in multimodal is 128 tokens.** Multimodal is tuned for captions + short queries, not long documents. For long text (page OCR), either truncate to 128 tokens OR use `titan-embed-text-v2` for long-text and `titan-embed-image-v1` for image/short-text side-by-side — same 1024-dim space is NOT guaranteed cross-model. Our default above uses multimodal for both, with truncation at `page_text[:4000]` (Titan tokeniser averages ~4 chars/token; 4000 chars ≈ 1000 tokens, which multimodal will silently chop).
4. **Rekognition `DetectFaces` operates on S3 references, not in-memory bytes, for images > 5 MB.** Pass the S3 ref always (our code does). But: Rekognition supports JPEG/PNG only — for WEBP or HEIC inputs, convert to JPEG first in Pillow before calling Rekognition.
5. **Textract rate limits are async-friendly but sync-hostile.** `StartDocumentAnalysis` is the right call for > 1-page PDFs; `AnalyzeDocument` is sync and capped at 1 page. Poll via `GetDocumentAnalysis` or wire an SNS completion callback for no-poll.
6. **Thumbnails in a separate bucket is more than cosmetic.** Previews need different retention (expire at 180d — regenerable), different cross-account sharing rules (consumers get read-only), and different encryption posture. Putting thumbnails in the raw bucket blocks cross-account read because raw is typically LF-governed.
7. **Re-ingestion on S3 overwrite doesn't delete old vectors.** The S3 event fires on every PUT; the Lambda uses the SHA256(key) as doc_id, so a PUT to the same key overwrites the existing vector — BUT for PDFs with variable page counts, the old page 50 vector persists if the new PDF only has 30 pages. Solution: on PDF ingest, query `{"doc_id": doc_id}` first, collect existing keys, issue `delete_vectors` for keys not in the new set.
8. **Preview URLs are signed per-call (5 min default TTL).** For UI that displays 100 thumbnails, generating 100 signed URLs adds 100 ms to the request. Consider a CloudFront distribution in front of the preview bucket with OAC (see `LAYER_FRONTEND_CLOUDFRONT`), serving previews directly — pair with a short query-response caching policy.
9. **Multimodal vectors drift with model version.** If AWS releases `titan-embed-image-v2`, mixing v1 and v2 vectors in the same index is silently broken (different semantic space). Gate the model ID behind an env var + rebuild the index on version upgrade; do NOT attempt to mix.

---

## 4. Micro-Stack Variant

**Use when:** the multimodal index is a shared horizontal consumed by multiple apps (lakehouse diagrams, kit acoustic-fault equipment photos, rag-chatbot document figures).

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** on the ingest Docker function entry (asset directory).
2. **Identity-side grants** for consumers — `s3vectors:QueryVectors` + `bedrock:InvokeModel` + preview-bucket `s3:GetObject`. Raw bucket never readable by consumers (only the producer ingestion Lambda).
3. **`CfnRule` cross-stack EventBridge** — S3 → Ingest rule lives in the producer `MultimodalIndexStack`.
4. **Same-stack bucket + OAC** — if previews are served via CloudFront, the preview bucket + OAC + distribution live together in `MultimodalIndexStack`. Do not split.
5. **KMS ARNs as strings** — the `MmCmk.key_arn` is SSM-published. Consumers read the string, grant `kms:Decrypt` on it.

### 4.2 MultimodalIndexStack — the producer

```python
# stacks/multimodal_index_stack.py
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3vectors as s3v,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct


class MultimodalIndexStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) CMK
        cmk = kms.Key(
            self, "MmCmk",
            alias=f"alias/{{project_name}}-multimodal-{stage}",
            enable_key_rotation=True,
            removal_policy=(RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY),
        )

        # B) Raw + preview buckets (same-stack, no cross-stack grant cycles)
        raw = s3.Bucket(
            self, "MmRawBucket",
            bucket_name=f"{{project_name}}-multimodal-raw-{stage}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            event_bridge_enabled=True,
        )
        preview = s3.Bucket(
            self, "MmPreviewBucket",
            bucket_name=f"{{project_name}}-multimodal-previews-{stage}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=(RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY),
            auto_delete_objects=(stage != "prod"),
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-previews",
                    enabled=True,
                    expiration=Duration.days(180),
                ),
            ],
        )

        # C) Vector bucket + 2 indexes (image + text)
        vb = s3v.CfnVectorBucket(
            self, "MmVectorBucket",
            vector_bucket_name=f"{{project_name}}-multimodal-vectors-{stage}",
            encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="aws:kms", kms_key_arn=cmk.key_arn,
            ),
        )
        common_filter_keys = [
            s3v.CfnIndex.MetadataKeyProperty(name="source_type",  type="TEXT"),
            s3v.CfnIndex.MetadataKeyProperty(name="doc_id",       type="TEXT"),
            s3v.CfnIndex.MetadataKeyProperty(name="page",         type="NUMBER"),
            s3v.CfnIndex.MetadataKeyProperty(name="access_group", type="TEXT"),
            s3v.CfnIndex.MetadataKeyProperty(name="uploaded_at",  type="NUMBER"),
        ]
        idx_image = s3v.CfnIndex(
            self, "IdxImage",
            vector_bucket_name=vb.attr_vector_bucket_name,
            index_name="images",
            data_type="float32", dimension=1024, distance_metric="cosine",
            metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["source_uri", "thumbnail_uri", "caption"],
            ),
            filterable_metadata_keys=common_filter_keys,
        )
        idx_text = s3v.CfnIndex(
            self, "IdxText",
            vector_bucket_name=vb.attr_vector_bucket_name,
            index_name="text",
            data_type="float32", dimension=1024, distance_metric="cosine",
            metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["source_text", "source_uri"],
            ),
            filterable_metadata_keys=common_filter_keys,
        )
        for i in (idx_image, idx_text):
            i.add_dependency(vb)

        # D) Ingest Docker Lambda
        dlq = sqs.Queue(
            self, "MmIngestDlq",
            encryption=sqs.QueueEncryption.KMS_MANAGED,
            retention_period=Duration.days(14),
        )
        ingest = _lambda.DockerImageFunction(
            self, "MmIngestFn",
            function_name=f"{{project_name}}-multimodal-ingest-{stage}",
            code=_lambda.DockerImageCode.from_image_asset(
                directory=str(Path(__file__).parent.parent / "lambda_docker" / "mm_ingest"),
                platform=_lambda.Platform.LINUX_AMD64,
            ),
            architecture=_lambda.Architecture.X86_64,
            memory_size=6144,
            timeout=Duration.minutes(10),
            reserved_concurrent_executions=20,
            dead_letter_queue=dlq,
            environment={
                "RAW_BUCKET":         raw.bucket_name,
                "PREVIEW_BUCKET":     preview.bucket_name,
                "VECTOR_BUCKET_NAME": vb.attr_vector_bucket_name,
                "IDX_IMAGE_ARN":      idx_image.attr_index_arn,
                "IDX_TEXT_ARN":       idx_text.attr_index_arn,
                "EMBED_MODEL_ID":     "amazon.titan-embed-image-v1",
                "EMBED_DIM":          "1024",
                "MAX_IMAGE_DIM_PX":   "2048",
            },
        )
        raw.grant_read(ingest)
        preview.grant_write(ingest)
        for stmt in self._ingest_grants(cmk.key_arn,
                                        [idx_image.attr_index_arn, idx_text.attr_index_arn]):
            ingest.add_to_role_policy(stmt)

        # E) S3 → EB → Ingest rule (producer-side).
        events.Rule(
            self, "RuleMmRawUpload",
            description="Any new object in raw bucket triggers multimodal ingest.",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [raw.bucket_name]}},
            ),
            targets=[targets.LambdaFunction(ingest, dead_letter_queue=dlq)],
        )

        # F) SSM cross-stack contract.
        for name, value in (
            ("raw_bucket_name",     raw.bucket_name),
            ("preview_bucket_name", preview.bucket_name),
            ("idx_image_arn",       idx_image.attr_index_arn),
            ("idx_text_arn",        idx_text.attr_index_arn),
            ("cmk_arn",             cmk.key_arn),
            ("embed_model_id",      "amazon.titan-embed-image-v1"),
            ("embed_dim",           "1024"),
        ):
            ssm.StringParameter(
                self, f"Param{name.title().replace('_','')}",
                parameter_name=f"/{{project_name}}/{stage}/multimodal/{name}",
                string_value=value,
            )

        CfnOutput(self, "IdxImageArn",     value=idx_image.attr_index_arn)
        CfnOutput(self, "IdxTextArn",      value=idx_text.attr_index_arn)
        CfnOutput(self, "MmPreviewBucket", value=preview.bucket_name)

    def _ingest_grants(self, cmk_arn: str, index_arns: list[str]):
        yield iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::"
                f"foundation-model/amazon.titan-embed-image-v1",
            ],
        )
        yield iam.PolicyStatement(
            actions=["rekognition:DetectFaces", "rekognition:DetectModerationLabels"],
            resources=["*"],
        )
        yield iam.PolicyStatement(
            actions=["textract:AnalyzeDocument", "textract:DetectDocumentText",
                     "textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis",
                     "textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"],
            resources=["*"],
        )
        yield iam.PolicyStatement(
            actions=["s3vectors:PutVectors", "s3vectors:DeleteVectors",
                     "s3vectors:GetVectors", "s3vectors:QueryVectors"],
            resources=index_arns,
        )
        yield iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[cmk_arn],
        )
```

### 4.3 Consumer pattern — query-side Lambda in another stack

```python
# stacks/agent_stack.py — consumer.
from pathlib import Path
from aws_cdk import (
    Duration, Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)


class MultimodalConsumerStack(Stack):
    def __init__(self, scope, construct_id, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) SSM contract — strings/tokens throughout.
        idx_image_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/idx_image_arn"
        )
        idx_text_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/idx_text_arn"
        )
        preview_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/preview_bucket_name"
        )
        cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/cmk_arn"
        )
        embed_model = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/embed_model_id"
        )

        # B) Query Lambda (small, zero-dep).
        query_fn = _lambda.Function(
            self, "MmQueryFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            code=_lambda.Code.from_asset(
                str(Path(__file__).parent.parent / "lambda" / "mm_query"),
            ),
            handler="handler.lambda_handler",
            memory_size=1024,
            timeout=Duration.seconds(30),
            environment={
                "IDX_IMAGE_ARN":  idx_image_arn,
                "IDX_TEXT_ARN":   idx_text_arn,
                "EMBED_MODEL_ID": embed_model,
                "EMBED_DIM":      "1024",
                "PREVIEW_BUCKET": preview_bucket_name,
            },
        )

        # C) Query-only grants.
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
            resources=[idx_image_arn, idx_text_arn],
        ))
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::"
                f"foundation-model/amazon.titan-embed-image-v1",
            ],
        ))
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{preview_bucket_name}/*"],
        ))
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[cmk_arn],
        ))
```

### 4.4 Micro-stack gotchas

- **Consumer never reads from raw bucket.** Raw is the producer's private working space; consumers only see: (a) vectors, (b) preview thumbnails. If a consumer needs the full-resolution original, add a dedicated `view-full-resolution` Lambda in the producer that signs a URL and publishes the URL back — the raw bucket policy stays tight.
- **Cross-stack deletion order**: MultimodalIndexStack → consumer (deploy); consumer → MultimodalIndexStack (delete). If producer is deleted while consumers still query, the SSM params disappear and consumer lookup breaks.
- **Ingest Lambda concurrency cap of 20 is a soft rate-limit on Bedrock invoke.** For bulk backfill, temporarily raise to 100 + rely on Bedrock's own throttle backoff. Monitor `bedrock:ThrottlingException` CloudWatch metric.
- **Embedding model upgrade** (v1 → v2) requires a new `IdxImage_v2` / `IdxText_v2`, parallel population, cutover at the consumer via env-var flip, then old index deletion. Budget 2 weeks + double-storage during the transition.

---

## 5. Swap matrix — when to replace or supplement

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Embedding model | Titan Multimodal G1 1024-dim | Titan Multimodal G1 384-dim | Mobile/edge delivery; smaller index; ~5% recall loss on complex scenes. |
| Embedding model | Titan Multimodal G1 | Cohere Embed Multilingual v3 (text only) + CLIP (images via SageMaker) | Multi-language text captions + visually distinct domain (medical imaging). Two-model setup; slightly different similarity scale. |
| Embedding model | Titan Multimodal | OpenCLIP / SigLIP (self-hosted via SageMaker endpoint) | Fully offline / air-gap requirements; more control; ops overhead. |
| Vector store | S3 Vectors (this) | OpenSearch Serverless k-NN with multimodal | Hybrid BM25-over-captions + k-NN-over-images in one query; heavier ops; higher cost. |
| PDF page rendering | PyMuPDF at 200 dpi | pdf2image (Poppler) | Licence concerns (PyMuPDF is AGPL for commercial use without dual-licence). Switch if legal demands. |
| OCR | Textract AnalyzeDocument | Amazon Bedrock Nova Vision (LLM-based OCR) | Tables / handwriting / complex layouts — LLM OCR is more accurate but per-page more expensive. Use selectively. |
| Face blur | Rekognition DetectFaces → Pillow blur | Presidio / AWS Comprehend PII | PII redaction on TEXT in the image (after OCR), not just faces. Layer both. |
| Preview delivery | Signed URL per-call | CloudFront + OAC (`LAYER_FRONTEND_CLOUDFRONT`) | UI with 100+ thumbnails per query; signed-URL latency kills perf. |
| Tiling for high-detail | No tiling (full image → 2048 max) | 2048² tiles with 10% overlap | Engineering drawings with tiny text; schematic detail. ~4× storage + compute. |
| Video input | Not supported in multimodal | Rekognition Video + SageMaker VideoClip embedding | Video search ("find clips of workers in red") — separate pipeline. |

---

## 6. Worked example — offline synth + round-trip integration

```python
# tests/test_multimodal_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.multimodal_index_stack import MultimodalIndexStack


def test_synth_raw_preview_vector_ingest_query():
    app = cdk.App()
    stack = MultimodalIndexStack(app, "Mm-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # 2 buckets (raw + preview), both KMS, raw is versioned.
    tpl.resource_count_is("AWS::S3::Bucket", 2)
    tpl.has_resource_properties("AWS::S3::Bucket", {
        "BucketName":        "{project_name}-multimodal-raw-dev",
        "VersioningConfiguration": {"Status": "Enabled"},
        "NotificationConfiguration": Match.object_like({
            "EventBridgeConfiguration": {"EventBridgeEnabled": True},
        }),
    })

    # 2 indexes (image + text) — both 1024-dim cosine.
    tpl.resource_count_is("AWS::S3Vectors::Index", 2)
    tpl.has_resource_properties("AWS::S3Vectors::Index", {
        "IndexName":      "images",
        "Dimension":      1024,
        "DistanceMetric": "cosine",
        "NonFilterableMetadataKeys": Match.array_with([
            "thumbnail_uri", "caption",
        ]),
    })

    # Ingest Docker Lambda at 6 GB, 10 min, 20 concurrency.
    tpl.has_resource_properties("AWS::Lambda::Function", {
        "MemorySize":                    6144,
        "Timeout":                       600,
        "ReservedConcurrentExecutions": 20,
        "PackageType":                   "Image",
    })

    # EB rule wires S3 Object Created → Ingest.
    tpl.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": Match.object_like({
            "source":      ["aws.s3"],
            "detail-type": ["Object Created"],
        }),
    })


# tests/test_integration_roundtrip.py
"""Integration: upload a test PNG, wait for ingest, then query by text."""
import json, os, time, base64
import pytest, boto3


@pytest.mark.integration
def test_upload_png_and_find_via_text():
    s3 = boto3.client("s3")
    bedrock = boto3.client("bedrock-runtime")
    s3v = boto3.client("s3vectors")

    raw = os.environ["RAW_BUCKET"]
    idx_img = os.environ["IDX_IMAGE_ARN"]

    # 1) Upload a test PNG (checkerboard)
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (800, 800), "white")
    d = ImageDraw.Draw(img)
    for y in range(0, 800, 100):
        for x in range(0, 800, 100):
            if (x // 100 + y // 100) % 2 == 0:
                d.rectangle([x, y, x+100, y+100], fill="black")
    import io
    buf = io.BytesIO(); img.save(buf, format="PNG")
    key = "images/testcheck.png"
    s3.put_object(Bucket=raw, Key=key, Body=buf.getvalue(), ContentType="image/png")

    # 2) Poll for vector availability (ingest is async).
    for _ in range(60):
        res = s3v.query_vectors(
            indexArn=idx_img,
            queryVector=[0.0] * 1024,   # dummy query — we'll query for real next
            topK=1,
            filter={"source_type": "image"},
            returnMetadata=True,
        )["matches"]
        if res:
            break
        time.sleep(2)
    assert res, "ingest did not produce a vector within 2 min"

    # 3) Real text query — embed "chequered pattern" and find our PNG.
    body = json.dumps({"inputText": "chequered pattern",
                       "embeddingConfig": {"outputEmbeddingLength": 1024}})
    q_vec = json.loads(bedrock.invoke_model(
        modelId="amazon.titan-embed-image-v1",
        body=body, accept="application/json", contentType="application/json",
    )["body"].read())["embedding"]

    matches = s3v.query_vectors(
        indexArn=idx_img, queryVector=q_vec, topK=5,
        filter={"source_type": "image"},
        returnMetadata=True, returnDistance=True,
    )["matches"]

    # Assert our key is in the top 5.
    keys = [m["metadata"]["source_uri"].split("/")[-1] for m in matches]
    assert "testcheck.png" in keys
```

Run `pytest tests/test_multimodal_synth.py -v` offline; `pytest tests/test_integration_roundtrip.py -v -m integration` after deploy.

---

## 7. References

- AWS docs — *Amazon Titan Multimodal Embeddings G1* (input shapes, dimensions, normalisation).
- AWS docs — *Amazon Textract AnalyzeDocument / StartDocumentAnalysis*.
- AWS docs — *Amazon Rekognition DetectFaces*.
- `DATA_S3_VECTORS.md` — underlying vector storage (same API, same ARN shapes).
- `PATTERN_CATALOG_EMBEDDINGS.md` — text-only sibling for Glue metadata; same topology.
- `PATTERN_DOC_INGESTION_RAG.md` — document chunking counterpart (text-only).
- `LAYER_FRONTEND_CLOUDFRONT.md` — preview-bucket + OAC for UI thumbnails.
- `kits/acoustic-fault-diagnostic-agent.md` — equipment-photo cross-modal consumer.
- `kits/rag-chatbot-per-client.md` — document-figure consumer.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Titan Multimodal G1 as the default model. Cross-modal (text ↔ image) same-space indexing. PDF → Textract + PyMuPDF page-render dual-embed. EXIF strip + Rekognition face-blur + Pillow resize pre-processing. Thumbnail preview bucket with 180-day lifecycle. 9 monolith gotchas, 4 micro-stack gotchas, 10-row swap matrix, pytest synth + PNG roundtrip integration.
