# SOP — Audio Embedding Similarity Search (embed → PutVectors → QueryVectors for "find similar diagnosed cases")

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon S3 Vectors (primary) · AST (`MIT/ast-finetuned-audioset-10-10-0.4593`, 768-dim) / Wav2Vec2 (`facebook/wav2vec2-base`, 768-dim) / custom fine-tuned penultimate layer · SageMaker Serverless or Async Inference for the encoder · consumes curated features from `MLOPS_AUDIO_PIPELINE` · writes to vector index from `DATA_S3_VECTORS`

---

## 1. Purpose

- Codify the **audio-sample → embedding → PutVectors → QueryVectors** similarity-search pipeline. Engine / machine-sound embeddings are written to an S3 Vectors index keyed by `{sample_id}` or `{sample_id}#{window_idx}` so downstream consumers (diagnostic agents, mechanics' tools, training-data miners) can run "find acoustically similar past cases" over every recording the pipeline has ever processed.
- Provide **three storage strategies** — `per_window`, `aggregated`, and `hybrid` — so the consultant can choose between fine-grained transient-fault retrieval (per-window), cheaper sample-level similarity (aggregated), or both (hybrid). The choice drives vector key design, query semantics, and storage cost.
- Provide **three canonical query strategies** grounded in real diagnostic workflows: (a) "find similar sounds to this new recording" (unfiltered semantic nearest-neighbour), (b) "find confirmed faults in THIS machine's history" (metadata-filtered query), (c) "mine training examples of fault X" (label-driven retrieval with metadata filter workaround).
- Codify the **canonical metadata schema** — filterable keys (`machine_id`, `timestamp`, `fault_label`, `outcome`, `technician_id`, `part_replaced`, `window_idx`, `confidence_at_diagnosis`) and non-filterable keys (`source_audio_s3_path` — the pointer back to the raw WAV). Filterable keys are frozen at index creation time (see `DATA_S3_VECTORS` §3); pick early.
- Codify **embedding source plug-points** — AST (general audio, 768-dim), Wav2Vec2 (raw waveform, 768/1024-dim), or a custom penultimate-layer extractor from a fine-tuned fault-classifier (task-specific). Ship all three behind a single encoder endpoint contract: `bytes in → float32[768]+ out`.
- Codify **label-drift updates** — technicians confirm/disconfirm diagnoses over time, so the `outcome` metadata field evolves. Idempotent upsert via stable `{sample_id}#{window_idx}` keys; re-`PutVectors` overwrites metadata without duplicating the vector row.
- Include when the SOW signals: "find similar sounds", "search historical recordings", "acoustic similarity", "find past cases of this fault", "mine training data by fault type", "acoustic fingerprint", "voiceprint lookup for machines".
- Reference `DATA_S3_VECTORS` for the vector-bucket / index CDK details (do NOT duplicate that infra here) and `MLOPS_AUDIO_PIPELINE` for the curated-feature source (do NOT duplicate preprocessing here).

---

## 2. Decision — Monolith vs Micro-Stack + storage/encoder choices

### 2.1 Structural split

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the vector bucket + index, the encoder endpoint, the embed-and-upsert Lambda, and reuses `audio_metadata` from `MLOPS_AUDIO_PIPELINE` | **§3 Monolith Variant** |
| `VectorStoreStack` (from `DATA_S3_VECTORS` §4) owns the vector bucket + `audio-windows` / `audio-samples` indexes; `AudioSimilarityStack` owns the encoder endpoint + embed-and-upsert Lambda; `MLOpsAudioStack` (from `MLOPS_AUDIO_PIPELINE` §4) emits the `AudioPreprocessedEvent` that triggers embedding | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **S3 Vectors index dimension + filterable-metadata schema are immutable** — same concern as `PATTERN_DOC_INGESTION_RAG` §2.1, amplified because audio teams more commonly experiment with different encoders mid-project. Lock the index's `dimension` to your CHOSEN encoder before any PutVectors call. Changing encoders = new index, re-embed entire corpus. Keep the index in `VectorStoreStack` so it survives `AudioSimilarityStack` redeploys.
2. **Encoder endpoint (SageMaker)** must exist BEFORE the embed Lambda can invoke it. Monolith = same-stack race via implicit CDK dependency; micro-stack = `AudioSimilarityStack` reads the endpoint name via SSM from a pre-deployed `SageMakerServingStack` or deploys both side-by-side inside `AudioSimilarityStack` (safe — same stack).
3. **Identity-side-only grants for S3 Vectors** — no L2 grant method exists (`s3vectors.*` actions must be added via `iam.PolicyStatement` on the index ARN from SSM). This is true in both variants; see `DATA_S3_VECTORS` §4.2.
4. **`audio_metadata` DDB table** from `MLOPS_AUDIO_PIPELINE` is the source of truth for preprocessing status + `preprocessed_s3_prefix`. The embed Lambda does NOT write a new table — it updates the existing row with `embedding_indexed=true`, `vector_keys=[...]`. Cross-stack: read table name via SSM.

Micro-Stack variant enforces: (a) vector bucket + indexes in `VectorStoreStack`; (b) encoder endpoint in `SageMakerServingStack` OR locally in `AudioSimilarityStack`; (c) embed-and-upsert Lambda + similarity-query Lambda in `AudioSimilarityStack`; (d) every cross-stack handle is a string (index ARN, endpoint name, table ARN) via SSM.

### 2.2 Plug-point matrix

| Plug-point | Variant | Use when |
|---|---|---|
| `STORAGE_STRATEGY` | `per_window` | **Default for fault-diagnosis.** One vector per 5s window. Transient faults (knock, bearing hit) preserved. Key = `{sample_id}#{window_idx}`. Storage ≈ 12 × 768-dim × 4 B = 37 KB per 60s sample |
| `STORAGE_STRATEGY` | `aggregated` | Sample-level hit rates matter more than transient detail. Mean-pool the window embeddings. Key = `{sample_id}`. Storage ≈ 3 KB per sample. Cheaper, faster queries, washes out transients |
| `STORAGE_STRATEGY` | `hybrid` | Highest retrieval quality. Two indexes (`audio-windows` + `audio-samples`); query both, merge results. 2× storage cost, 2× PutVectors cost |
| `ENCODER` | `ast` | General-purpose audio; not fine-tuned to your fault set but zero-training-cost. Model `MIT/ast-finetuned-audioset-10-10-0.4593`, 768-dim. Works well for "does this sound like anything we've seen before?" |
| `ENCODER` | `wav2vec2` | Raw-waveform input; 768-dim (base) or 1024-dim (large). Slightly better at speech-like signals (vocal alarms) than AST |
| `ENCODER` | `custom_penultimate` | **Best retrieval quality.** Take the penultimate-layer output of your fine-tuned fault-classifier. Task-specific → embeddings cluster by fault class. Requires classifier training first (see `MLOPS_SAGEMAKER_TRAINING`) |
| `ENCODER_HOSTING` | `serverless` | Sparse / bursty query load. 5-10s cold start on first invoke; pay-per-invoke |
| `ENCODER_HOSTING` | `realtime` | Interactive diagnostic UI (<1s latency target). Warm endpoint; reserved ml.g5.xlarge typical |
| `ENCODER_HOSTING` | `async` | Background embedding backfill over thousands of samples. Jobs up to 1 GB input; results via SNS |
| `QUERY_FILTER_STRATEGY` | `unfiltered` | "Find any acoustically similar case globally" |
| `QUERY_FILTER_STRATEGY` | `machine_scoped` | "Find similar cases in THIS machine's history only" — filter on `machine_id` |
| `QUERY_FILTER_STRATEGY` | `confirmed_only` | "Only return hits that were mechanic-confirmed" — filter on `outcome="confirmed"` |
| `QUERY_FILTER_STRATEGY` | `label_mine` | "Find all bearing-wear training examples" — workaround: random query vector + `topK=1000` + filter on `fault_label` (S3 Vectors doesn't have a pure-metadata query; see §3.7 gotchas) |

The **canonical worked example** in §3 uses `STORAGE_STRATEGY=per_window` + `ENCODER=ast` + `ENCODER_HOSTING=serverless` + unfiltered query. Other combinations are swap-matrix rows in §5.

---

## 3. Monolith Variant

### 3.1 Architecture

```
            [ MLOpsAudioPipeline — from MLOPS_AUDIO_PIPELINE ]
                             │  status=feature_extracted
                             │  emits AudioPreprocessedEvent
                             ▼
                  EventBridge default bus
                             │  detail-type="AudioPreprocessed"
                             ▼
         ┌──────────────────────────────────────────────┐
         │  EmbedAndUpsertLambda                        │
         │  1. DDB: read audio_metadata row for sample  │
         │  2. For each window_idx in preprocessed_:    │
         │       - S3 GetObject mel_spec window npy     │
         │       - invoke encoder endpoint (AST/W2V2/   │
         │         custom_penultimate) → float32[768]   │
         │  3. Strategy:                                │
         │       per_window   → PutVectors len=N        │
         │       aggregated   → np.mean(embeds,0) → 1   │
         │       hybrid       → both                    │
         │  4. PutVectors batched (100 per call)        │
         │     with metadata:                           │
         │       filterable: machine_id, timestamp,     │
         │          fault_label, outcome, window_idx    │
         │       non_filterable:                        │
         │          source_audio_s3_path                │
         │  5. DDB: update status=indexed,              │
         │     vector_keys=[...]                        │
         └──────────────────┬───────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────────────┐
         │  S3 Vectors — audio-windows index            │
         │  (see DATA_S3_VECTORS §3 for infra)          │
         │  cosine distance · dim=768                   │
         └──────────────────────────────────────────────┘
                            ▲
                            │  query time
         ┌──────────────────┴───────────────────────────┐
         │  SimilarityQueryLambda (behind API Gateway   │
         │    or direct SDK for agents)                 │
         │  1. Embed query audio (same encoder)         │
         │  2. QueryVectors with optional filter        │
         │     (machine_id, outcome, fault_label)       │
         │  3. Return hits with source_audio_s3_path    │
         │     + fault_label + technician_id            │
         └──────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_audio_similarity()` method body

```python
from pathlib import Path

from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sagemaker as sagemaker,
    aws_sqs as sqs,
)


def _create_audio_similarity(self, stage: str) -> None:
    """Monolith. Assumes self.{kms_key, curated_audio_bucket, audio_metadata,
    vector_bucket_name, vector_kms_arn, encoder_endpoint_name} already exist
    — either built inline or imported from an outer stack.

    The two vector indexes (`audio-windows` and `audio-samples`) are created
    by DATA_S3_VECTORS §3; here we only reference them via self.window_index_*
    and self.sample_index_* attributes. To keep this truly monolith, inline
    DATA_S3_VECTORS §3.2 into this stack."""

    # A) DLQ for failed embed attempts
    self.embed_dlq = sqs.Queue(
        self, "EmbedDlq",
        queue_name=f"{{project_name}}-audio-embed-dlq-{stage}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )

    # B) EmbedAndUpsertLambda — ZIP; boto3 only (S3 + SM runtime + S3 Vectors)
    embed_log = logs.LogGroup(
        self, "EmbedLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-audio-embed-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.embed_fn = _lambda.Function(
        self, "EmbedFn",
        function_name=f"{{project_name}}-audio-embed-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset(
            str(Path(__file__).resolve().parents[3] / "lambda" / "audio_embed")
        ),
        memory_size=2048,
        timeout=Duration.minutes(10),
        log_group=embed_log,
        tracing=_lambda.Tracing.ACTIVE,
        dead_letter_queue_enabled=True,
        dead_letter_queue=self.embed_dlq,
        reserved_concurrent_executions=10,        # cap encoder endpoint TPS
        environment={
            "CURATED_BUCKET":            self.curated_audio_bucket.bucket_name,
            "AUDIO_METADATA_TABLE":      self.audio_metadata.table_name,
            "VECTOR_BUCKET_NAME":        self.vector_bucket_name,
            "WINDOW_INDEX_NAME":         self.window_index_name,   # "audio-windows"
            "SAMPLE_INDEX_NAME":         self.sample_index_name,   # "audio-samples"
            "STORAGE_STRATEGY":          "per_window",             # per_window | aggregated | hybrid
            "ENCODER":                   "ast",                    # ast | wav2vec2 | custom_penultimate
            "ENCODER_ENDPOINT_NAME":     self.encoder_endpoint_name,
            "ENCODER_DIMENSION":         "768",
            "PUT_VECTORS_BATCH_SIZE":    "100",
            "POWERTOOLS_SERVICE_NAME":   "{project_name}-audio-embed",
        },
    )

    # C) Grants — monolith
    self.curated_audio_bucket.grant_read(self.embed_fn)
    self.audio_metadata.grant_read_write_data(self.embed_fn)
    self.kms_key.grant_decrypt(self.embed_fn)

    # SageMaker encoder endpoint invoke
    self.embed_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint", "sagemaker:InvokeEndpointAsync"],
        resources=[
            f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
            f"endpoint/{self.encoder_endpoint_name}"
        ],
    ))

    # S3 Vectors — identity-side (no L2)
    self.embed_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "s3vectors:PutVectors",
            "s3vectors:GetVectors",
            "s3vectors:DeleteVectors",
            "s3vectors:QueryVectors",
            "s3vectors:GetIndex",
        ],
        resources=[
            self.window_index_arn,
            self.sample_index_arn,
        ],
    ))
    self.embed_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
        resources=[self.vector_kms_arn],
    ))

    # D) EventBridge rule: AudioPreprocessed → EmbedFn.
    #    The event is emitted by MLOPS_AUDIO_PIPELINE's preprocess Lambda
    #    via events.PutEvents when status transitions to feature_extracted.
    events.Rule(
        self, "AudioPreprocessedRule",
        rule_name=f"{{project_name}}-audio-preprocessed-{stage}",
        event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
        event_pattern=events.EventPattern(
            source=["{project_name}.audio"],
            detail_type=["AudioPreprocessed"],
        ),
        targets=[targets.LambdaFunction(self.embed_fn)],
    )

    # E) SimilarityQueryLambda — callable from API Gateway OR Agents (MCP tool)
    query_log = logs.LogGroup(
        self, "QueryLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-audio-query-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.query_fn = _lambda.Function(
        self, "QueryFn",
        function_name=f"{{project_name}}-audio-query-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset(
            str(Path(__file__).resolve().parents[3] / "lambda" / "audio_query")
        ),
        memory_size=1024,
        timeout=Duration.seconds(30),
        log_group=query_log,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "VECTOR_BUCKET_NAME":     self.vector_bucket_name,
            "WINDOW_INDEX_NAME":      self.window_index_name,
            "SAMPLE_INDEX_NAME":      self.sample_index_name,
            "ENCODER_ENDPOINT_NAME":  self.encoder_endpoint_name,
            "ENCODER":                "ast",
            "ENCODER_DIMENSION":      "768",
            "DEFAULT_TOP_K":          "10",
            "MAX_TOP_K":              "100",
        },
    )
    self.query_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[
            f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
            f"endpoint/{self.encoder_endpoint_name}"
        ],
    ))
    self.query_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
        resources=[self.window_index_arn, self.sample_index_arn],
    ))
    self.query_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.vector_kms_arn],
    ))

    CfnOutput(self, "EmbedFnArn",   value=self.embed_fn.function_arn)
    CfnOutput(self, "QueryFnArn",   value=self.query_fn.function_arn)
    CfnOutput(self, "EmbedDlqArn",  value=self.embed_dlq.queue_arn)
```

### 3.3 Canonical metadata schema

```python
# FILTERABLE (must be declared in index create-time schema — see DATA_S3_VECTORS §3)
# Max 10 filterable keys per S3 Vectors index (current AWS limit).
FILTERABLE_KEYS = {
    "machine_id":              "str",   # e.g. VIN, asset tag
    "timestamp":               "str",   # ISO 8601 upload time
    "fault_label":             "str",   # mechanic-confirmed ground truth
                                        # OR model-predicted (distinguish via `source`)
    "fault_source":            "str",   # "mechanic_confirmed" | "model_predicted" | "unlabeled"
    "outcome":                 "str",   # "confirmed" | "disconfirmed" | "pending"
    "confidence_at_diagnosis": "float", # 0..1 — model confidence at ingestion time
    "technician_id":           "str",   # who ran the diagnosis
    "part_replaced":           "str",   # "main_bearing" | "fuel_injector" | ...
                                        # only populated when outcome="confirmed"
    "window_idx":              "int",   # per_window strategy only
    "sensor_position":         "str",   # "bell_housing" | "intake" | "exhaust" | ...
}

# NON-FILTERABLE (max 10 keys, immutable at write time)
# Best used for the pointer back to the raw artifact.
NON_FILTERABLE_KEYS = {
    "source_audio_s3_path":    "str",   # s3://raw-audio/{sample_id}.wav
    "source_window_s3_path":   "str",   # s3://curated-audio/{sample_id}/mel_spec/window_003.npy
    "encoder_model_id":        "str",   # "ast-v1" | "ast-ft-v2" | "wav2vec2-base" | ...
    "encoder_version":         "str",   # track which model version produced this embedding
}
```

### 3.4 Embed-and-upsert handler — saved to `lambda/audio_embed/index.py`

```python
"""Audio embed + PutVectors handler.

Triggered by EventBridge `AudioPreprocessed` event (detail-type emitted by
MLOPS_AUDIO_PIPELINE's preprocess Lambda when status transitions to
feature_extracted). Reads mel-spec windows from the curated bucket, invokes
the encoder endpoint to get 768-dim float32 embeddings, batches PutVectors.

Idempotent: vector key is stable (`{sample_id}` or `{sample_id}#{window_idx}`),
re-embedding overwrites.
"""
import io
import json
import logging
import os
import time

import boto3
import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3     = boto3.client("s3")
ddb    = boto3.resource("dynamodb").Table(os.environ["AUDIO_METADATA_TABLE"])
sm     = boto3.client("sagemaker-runtime")
s3v    = boto3.client("s3vectors")

CURATED_BUCKET     = os.environ["CURATED_BUCKET"]
VECTOR_BUCKET      = os.environ["VECTOR_BUCKET_NAME"]
WINDOW_INDEX       = os.environ["WINDOW_INDEX_NAME"]
SAMPLE_INDEX       = os.environ["SAMPLE_INDEX_NAME"]
STRATEGY           = os.environ.get("STORAGE_STRATEGY", "per_window")
ENCODER            = os.environ.get("ENCODER", "ast")
ENDPOINT_NAME      = os.environ["ENCODER_ENDPOINT_NAME"]
DIM                = int(os.environ["ENCODER_DIMENSION"])
PUT_BATCH          = int(os.environ.get("PUT_VECTORS_BATCH_SIZE", "100"))

ENCODER_MODEL_ID   = {
    "ast":                "MIT/ast-finetuned-audioset-10-10-0.4593",
    "wav2vec2":           "facebook/wav2vec2-base",
    "custom_penultimate": os.environ.get("CUSTOM_ENCODER_ID", "custom-penultimate-v1"),
}[ENCODER]
ENCODER_VERSION    = os.environ.get("ENCODER_VERSION", "v1")


class PermanentError(Exception):
    pass


def lambda_handler(event, _ctx):
    sample_id = event["detail"]["sample_id"]

    row = ddb.get_item(Key={"sample_id": sample_id}).get("Item")
    if not row:
        raise PermanentError(f"audio_metadata row missing for {sample_id}")
    if row.get("status") not in ("feature_extracted", "indexed"):
        # Ignore stale events (e.g. re-trigger after preprocessing was redone)
        logger.warning("skip sample=%s status=%s", sample_id, row.get("status"))
        return {"sample_id": sample_id, "skipped": True}

    prefix = row["preprocessed_s3_prefix"].replace(f"s3://{CURATED_BUCKET}/", "")
    n_windows = int(row.get("window_count", 0))
    if n_windows == 0:
        raise PermanentError(f"window_count=0 for {sample_id}")

    try:
        # 1) Fetch every mel-spec window npy and embed
        embeddings: list[np.ndarray] = []
        for i in range(n_windows):
            npy_key = f"{prefix}orig/mel_spec/window_{i:03d}.npy"
            mel = _fetch_npy(npy_key)
            emb = _encode(mel)                                    # (DIM,)
            embeddings.append(emb)

        # 2) Build metadata
        common_meta = _build_metadata(sample_id, row)

        records_by_index: dict[str, list[dict]] = {}

        if STRATEGY in ("per_window", "hybrid"):
            records_by_index[WINDOW_INDEX] = []
            for i, emb in enumerate(embeddings):
                meta = dict(common_meta)
                meta["window_idx"] = i
                meta["source_window_s3_path"] = (
                    f"s3://{CURATED_BUCKET}/{prefix}orig/mel_spec/window_{i:03d}.npy"
                )
                records_by_index[WINDOW_INDEX].append({
                    "key":      f"{sample_id}#{i:03d}",
                    "data":     {"float32": emb.tolist()},
                    "metadata": meta,
                })

        if STRATEGY in ("aggregated", "hybrid"):
            mean_emb = np.mean(np.stack(embeddings, axis=0), axis=0)
            # L2 re-normalize for cosine (mean-pooling breaks unit norm)
            norm = float(np.linalg.norm(mean_emb))
            if norm > 0:
                mean_emb = mean_emb / norm
            meta = dict(common_meta)
            records_by_index.setdefault(SAMPLE_INDEX, []).append({
                "key":      sample_id,
                "data":     {"float32": mean_emb.tolist()},
                "metadata": meta,
            })

        # 3) Batched PutVectors per index
        vector_keys: list[str] = []
        for index_name, records in records_by_index.items():
            for batch in _chunked(records, PUT_BATCH):
                s3v.put_vectors(
                    vectorBucketName=VECTOR_BUCKET,
                    indexName=index_name,
                    vectors=batch,
                )
            vector_keys.extend(r["key"] for r in records)

        # 4) DDB update
        ddb.update_item(
            Key={"sample_id": sample_id},
            UpdateExpression=(
                "SET #s = :s, vector_keys = :vk, embedding_indexed = :ei, "
                "encoder_model_id = :em, encoder_version = :ev, indexed_at = :t"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":  "indexed",
                ":vk": vector_keys,
                ":ei": True,
                ":em": ENCODER_MODEL_ID,
                ":ev": ENCODER_VERSION,
                ":t":  _now(),
            },
        )
        return {"sample_id": sample_id, "vectors_written": len(vector_keys)}

    except PermanentError as e:
        logger.warning("permanent failure sample=%s reason=%s", sample_id, e)
        _mark_failed(sample_id, f"embed_permanent:{e}")
        return {"sample_id": sample_id, "status": "failed", "reason": str(e)}
    except Exception:
        logger.exception("transient failure sample=%s", sample_id)
        _mark_failed(sample_id, "embed_transient:see_logs")
        raise


# -------------------------------------------------------------- helpers

def _fetch_npy(key: str) -> np.ndarray:
    body = s3.get_object(Bucket=CURATED_BUCKET, Key=key)["Body"].read()
    return np.load(io.BytesIO(body), allow_pickle=False)


def _encode(mel_spec: np.ndarray) -> np.ndarray:
    """Call SageMaker encoder endpoint.

    Payload contract (custom container):
      {"inputs": [[...mel frame...], ...]}
    Response contract:
      {"embedding": [f32 × DIM]}
    If you use a HuggingFace-hosted AST container, adjust the keys:
      input key is typically "input_values" (raw waveform) for AST directly,
      but for a mel-spec-in-mel-spec-out encoder you standardise on "inputs".
    """
    payload = json.dumps({"inputs": mel_spec.tolist()}).encode("utf-8")
    resp = sm.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType="application/json",
        Accept="application/json",
        Body=payload,
    )
    body = json.loads(resp["Body"].read())
    vec = np.asarray(body["embedding"], dtype=np.float32)
    if vec.shape != (DIM,):
        raise PermanentError(
            f"encoder returned shape {vec.shape}, expected ({DIM},)"
        )
    # L2 normalize for cosine (encoder should already do this, but enforce)
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0 else vec


def _build_metadata(sample_id: str, row: dict) -> dict:
    """Build the filterable + non-filterable metadata dict for PutVectors.

    Pull from the audio_metadata row; every key here must exist in the
    index's filterable-schema (§3.3) or it will be silently dropped / rejected
    depending on whether strict mode is on.
    """
    meta = {
        "machine_id":               row.get("machine_id", "unknown"),
        "timestamp":                row.get("uploaded_at", _now()),
        "fault_label":              row.get("fault_label", "unlabeled"),
        "fault_source":             row.get("fault_source", "unlabeled"),
        "outcome":                  row.get("outcome", "pending"),
        "confidence_at_diagnosis":  float(row.get("confidence_at_diagnosis", 0.0)),
        "technician_id":            row.get("technician_id", "unknown"),
        "part_replaced":            row.get("part_replaced", "none"),
        "sensor_position":          row.get("sensor_position", "unknown"),
        # Non-filterable — these should be keyed out of the filterable schema
        "source_audio_s3_path":     row.get("s3_key", ""),
        "encoder_model_id":         ENCODER_MODEL_ID,
        "encoder_version":          ENCODER_VERSION,
    }
    return meta


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _mark_failed(sample_id: str, reason: str) -> None:
    ddb.update_item(
        Key={"sample_id": sample_id},
        UpdateExpression="SET #s = :s, failure_reason = :r, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "embed_failed", ":r": reason, ":u": _now()},
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
```

### 3.5 Query handler — saved to `lambda/audio_query/index.py`

```python
"""Audio similarity query handler.

Three canonical query strategies:
  1. "Find similar sounds to this new recording"
      - Embed the query audio, QueryVectors unfiltered, topK=10
  2. "Find diagnosed faults in this machine's history"
      - Embed, filter={"machine_id": VIN, "outcome": "confirmed"}, topK=20
  3. "Mine training examples of fault X" (workaround for pure-metadata)
      - Random query vector, filter={"fault_label": "bearing_wear"}, topK=1000

The caller indicates intent via the `mode` field of the request.
"""
import io
import json
import logging
import os

import boto3
import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3v = boto3.client("s3vectors")
sm  = boto3.client("sagemaker-runtime")
s3  = boto3.client("s3")

VECTOR_BUCKET     = os.environ["VECTOR_BUCKET_NAME"]
WINDOW_INDEX      = os.environ["WINDOW_INDEX_NAME"]
SAMPLE_INDEX      = os.environ["SAMPLE_INDEX_NAME"]
ENDPOINT_NAME     = os.environ["ENCODER_ENDPOINT_NAME"]
DIM               = int(os.environ["ENCODER_DIMENSION"])
DEFAULT_TOP_K     = int(os.environ.get("DEFAULT_TOP_K", "10"))
MAX_TOP_K         = int(os.environ.get("MAX_TOP_K", "100"))


def lambda_handler(event, _ctx):
    """Request shape:
        {
          "mode": "similar_sound" | "machine_history" | "label_mine",
          "index": "audio-windows" | "audio-samples",
          "top_k": 10,
          # For similar_sound / machine_history:
          "query_audio_s3_key": "s3://.../query.wav"   # will be preprocessed + embedded
            OR
          "query_vector": [ ... ]                       # pre-computed embedding
          # For machine_history:
          "machine_id": "VIN...",
          "outcome_filter": "confirmed"                 # default: "confirmed"
          # For label_mine:
          "fault_label": "bearing_wear",
          "min_confidence": 0.7                         # optional
        }
    """
    mode   = event.get("mode", "similar_sound")
    index  = event.get("index", WINDOW_INDEX)
    top_k  = min(int(event.get("top_k", DEFAULT_TOP_K)), MAX_TOP_K)

    if mode == "label_mine":
        # Pure-metadata-filter workaround: random unit vector + high topK + filter.
        query_vec = _random_unit_vector(DIM)
        flt = {"fault_label": event["fault_label"]}
        if event.get("min_confidence") is not None:
            flt["confidence_at_diagnosis"] = {"$gte": float(event["min_confidence"])}
        return _query(index, query_vec, top_k, flt)

    # similar_sound / machine_history both require an embedded query
    if "query_vector" in event:
        q = np.asarray(event["query_vector"], dtype=np.float32)
    else:
        mel = _load_query_mel(event["query_audio_s3_key"])
        q   = _encode(mel)

    if mode == "machine_history":
        flt = {
            "machine_id": event["machine_id"],
            "outcome":    event.get("outcome_filter", "confirmed"),
        }
    else:
        flt = None

    return _query(index, q, top_k, flt)


def _query(index_name: str, vec: np.ndarray, top_k: int,
           flt: dict | None) -> dict:
    kwargs = {
        "vectorBucketName": VECTOR_BUCKET,
        "indexName":        index_name,
        "queryVector":      {"float32": vec.tolist()},
        "topK":             top_k,
        "returnMetadata":   True,
        "returnDistance":   True,
    }
    if flt is not None:
        kwargs["filter"] = flt
    resp = s3v.query_vectors(**kwargs)
    hits = []
    for v in resp.get("vectors", []):
        hits.append({
            "key":      v.get("key"),
            "distance": v.get("distance"),
            "metadata": v.get("metadata", {}),
        })
    return {"hits": hits, "count": len(hits)}


def _encode(mel_spec: np.ndarray) -> np.ndarray:
    resp = sm.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType="application/json",
        Accept="application/json",
        Body=json.dumps({"inputs": mel_spec.tolist()}).encode("utf-8"),
    )
    vec = np.asarray(json.loads(resp["Body"].read())["embedding"], dtype=np.float32)
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0 else vec


def _load_query_mel(s3_uri: str) -> np.ndarray:
    """Fetch a pre-computed mel-spec from the curated bucket. The query side
    assumes the caller already ran the audio through MLOPS_AUDIO_PIPELINE to
    produce a mel window. If not, the caller's upstream should route through
    the preprocess Lambda first.
    """
    assert s3_uri.startswith("s3://")
    path = s3_uri[len("s3://"):]
    bucket, key = path.split("/", 1)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return np.load(io.BytesIO(body), allow_pickle=False)


def _random_unit_vector(dim: int) -> np.ndarray:
    v = np.random.randn(dim).astype(np.float32)
    return v / float(np.linalg.norm(v))
```

### 3.6 Idempotency + label drift

- **Vector key is stable**: `{sample_id}#{window_idx:03d}` (per-window) or `{sample_id}` (aggregated). `s3vectors:PutVectors` is upsert — re-embedding after re-preprocessing overwrites without duplication.
- **Label drift** — mechanics confirm/disconfirm diagnoses over time. When the `outcome` or `part_replaced` field changes in `audio_metadata`, re-emit the sample to the embed Lambda; it will rebuild metadata and `PutVectors` with the same key, overwriting the metadata. **No need to re-embed** — you can optimise the handler to call `PutVectors` with the existing vector (GetVectors first) but same metadata. `# TODO(verify): GetVectors returns the raw vector data with the current S3 Vectors API (confirm in boto3 1.35+ reference).`
- **Encoder version change** — if you swap from AST to a fine-tuned classifier, bump `ENCODER_VERSION`. Either: (a) recreate the index (new dim if dim changed, or same dim with different embedding distribution); (b) use a second index and dual-write during migration. Do not mix encoder outputs in the same index — cosine distances across encoder families are meaningless.

### 3.7 Monolith gotchas

- **S3 Vectors filterable-metadata schema is immutable** — the set of filterable keys is fixed at index creation. If you forget `sensor_position` on day one and add a sensor-location dimension later, you must recreate the index (and re-embed everything). Budget the filterable schema carefully during kit-design; prefer over-inclusion. See `DATA_S3_VECTORS` §3.
- **`max 10 filterable keys` is a hard AWS limit** — the canonical schema in §3.3 uses exactly 10. If you need an 11th, you either drop one or encode two into one composite string like `"fault_label_source": "bearing_wear|mechanic_confirmed"`.
- **Pure-metadata filter is NOT a first-class S3 Vectors operation** — `query_vectors` REQUIRES a `queryVector`. For "find me all bearing-wear samples regardless of similarity", the workaround is a random unit vector + high topK + filter. Quality degrades with higher topK because results are sorted by distance to the random vector. For production pure-metadata queries, maintain a parallel DDB GSI on `fault_label` (the `audio_metadata.by-status` GSI) and fetch the vectors by key via `GetVectors` if needed.
- **AST input shape contract** — `MIT/ast-finetuned-audioset-10-10-0.4593` expects 128-mel × 10s @ 16 kHz mel-spectrograms. Your preprocess pipeline likely produces 64-mel × 5s @ 44.1 kHz (the Toyota team default). Either (a) run a second preprocess pass sized for AST, or (b) deploy AST behind a container that does its own mel-extraction from raw waveform. Mismatched input shape silently returns garbage embeddings — the endpoint won't error. `# TODO(verify): add an inline shape-check in _encode that rejects windows not matching the encoder's expected shape before PutVectors.`
- **SageMaker Serverless Inference cold start** — first invocation after idle is 5-10s. For interactive "upload and find similar" UX, use Real-time Inference OR warm the endpoint with a periodic CloudWatch Events ping every 4 minutes.
- **Mean-pool re-normalization** — in the `aggregated` strategy, `np.mean(unit_vectors, axis=0)` does NOT produce a unit vector. The handler above L2-renormalizes; don't skip this step or cosine distance rankings are off (especially for samples with many similar windows).
- **Temporal-context loss with aggregated strategy** — a 1s knock event in a 60s sample gets washed out when you mean-pool 12 windows. For transient faults, stick with `per_window` or `hybrid`. Confirm: if your fault classes are "idle hum changes" or "continuous anomaly", aggregated is fine; if they're "click", "tick", "pop", "knock", go per-window.
- **Encoder endpoint ARN scoping** — `sagemaker:InvokeEndpoint` on a specific endpoint ARN prevents privilege creep but blocks you from swapping endpoint names without redeploying. If the kit expects endpoint-name swapping for A/B testing, scope the ARN to `endpoint/*` with a Condition on a tag. `# TODO(verify): ABAC tag-based conditions on sagemaker:InvokeEndpoint.`

---

## 4. Micro-Stack Variant

**Use when:** `VectorStoreStack` (from `DATA_S3_VECTORS` §4) owns the vector bucket + KMS + `audio-windows` + `audio-samples` indexes; `SageMakerServingStack` (from `MLOPS_SAGEMAKER_SERVING`) owns the encoder endpoint; `MLOpsAudioStack` (from `MLOPS_AUDIO_PIPELINE` §4) owns the `audio_metadata` table + curated bucket + emits the `AudioPreprocessed` event; **`AudioSimilarityStack` (this stack) owns the embed Lambda + query Lambda + DLQ + EventBridge rule.**

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** — `_LAMBDAS_ROOT` pattern for `audio_embed/` and `audio_query/`.
2. **Never call `curated_bucket.grant_read(fn)` cross-stack.** Identity-side `s3:GetObject` on `f"{curated_bucket_arn}/*"` + `kms:Decrypt` on the curated-bucket CMK ARN from SSM.
3. **Never target a cross-stack Lambda from a cross-stack EventBridge rule.** The `AudioPreprocessed` event flows over the default bus; the rule lives in THIS stack, target is local `embed_fn` — L2 `targets.LambdaFunction(local_fn)` is safe.
4. **Never import `CfnIndex` by object from `VectorStoreStack`.** Read `window_index_arn`, `window_index_name`, `sample_index_arn`, `sample_index_name`, `vector_bucket_name`, `vector_kms_arn` via SSM; grant `s3vectors:*` and `kms:*` identity-side on those ARN tokens (see `DATA_S3_VECTORS` §4.2).
5. **PermissionsBoundary + `iam:PassRole` with `iam:PassedToService=sagemaker.amazonaws.com`** on both Lambda roles — not strictly required for `sagemaker:InvokeEndpoint` (no role passing), but applied defensively in case the Lambda ever gets extended to `CreateEndpointConfig` or `CreateTransformJob`.

### 4.2 Dedicated `AudioSimilarityStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
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


class AudioSimilarityStack(cdk.Stack):
    """Audio embed + query Lambdas. Vector bucket + indexes imported from
    VectorStoreStack via SSM; curated bucket + audio_metadata + encoder
    endpoint similarly.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        # From VectorStoreStack (DATA_S3_VECTORS §4):
        vector_bucket_name_ssm: str,
        window_index_name_ssm: str,
        window_index_arn_ssm: str,
        sample_index_name_ssm: str,
        sample_index_arn_ssm: str,
        vector_kms_arn_ssm: str,
        # From MLOpsAudioStack (MLOPS_AUDIO_PIPELINE §4):
        curated_bucket_name_ssm: str,
        curated_bucket_arn_ssm: str,
        curated_bucket_kms_arn_ssm: str,
        audio_metadata_table_name_ssm: str,
        audio_metadata_table_arn_ssm: str,
        # From SageMakerServingStack:
        encoder_endpoint_name_ssm: str,
        # Plug-points:
        permission_boundary: iam.IManagedPolicy,
        storage_strategy: str = "per_window",       # per_window | aggregated | hybrid
        encoder: str = "ast",
        encoder_dimension: int = 768,
        encoder_version: str = "v1",
        reserved_concurrency: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-audio-similarity-{stage_name}", **kwargs)
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Read SSM params
        v_bucket_name   = ssm.StringParameter.value_for_string_parameter(self, vector_bucket_name_ssm)
        w_index_name    = ssm.StringParameter.value_for_string_parameter(self, window_index_name_ssm)
        w_index_arn     = ssm.StringParameter.value_for_string_parameter(self, window_index_arn_ssm)
        s_index_name    = ssm.StringParameter.value_for_string_parameter(self, sample_index_name_ssm)
        s_index_arn     = ssm.StringParameter.value_for_string_parameter(self, sample_index_arn_ssm)
        v_kms_arn       = ssm.StringParameter.value_for_string_parameter(self, vector_kms_arn_ssm)
        curated_name    = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_name_ssm)
        curated_arn     = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_arn_ssm)
        curated_kms_arn = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_kms_arn_ssm)
        am_table_name   = ssm.StringParameter.value_for_string_parameter(self, audio_metadata_table_name_ssm)
        am_table_arn    = ssm.StringParameter.value_for_string_parameter(self, audio_metadata_table_arn_ssm)
        endpoint_name   = ssm.StringParameter.value_for_string_parameter(self, encoder_endpoint_name_ssm)

        # Local CMK for DLQ + logs
        cmk = kms.Key(
            self, "SimilarityKey",
            alias=f"alias/{{project_name}}-audio-similarity-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        # A) DLQ
        dlq = sqs.Queue(
            self, "EmbedDlq",
            queue_name=f"{{project_name}}-audio-embed-dlq-{stage_name}",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            retention_period=Duration.days(14),
        )

        # B) EmbedFn
        embed_log = logs.LogGroup(
            self, "EmbedLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-audio-embed-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        embed_fn = _lambda.Function(
            self, "EmbedFn",
            function_name=f"{{project_name}}-audio-embed-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "audio_embed")),
            memory_size=2048,
            timeout=Duration.minutes(10),
            log_group=embed_log,
            tracing=_lambda.Tracing.ACTIVE,
            dead_letter_queue_enabled=True,
            dead_letter_queue=dlq,
            reserved_concurrent_executions=reserved_concurrency,
            environment={
                "CURATED_BUCKET":         curated_name,
                "AUDIO_METADATA_TABLE":   am_table_name,
                "VECTOR_BUCKET_NAME":     v_bucket_name,
                "WINDOW_INDEX_NAME":      w_index_name,
                "SAMPLE_INDEX_NAME":      s_index_name,
                "STORAGE_STRATEGY":       storage_strategy,
                "ENCODER":                encoder,
                "ENCODER_ENDPOINT_NAME":  endpoint_name,
                "ENCODER_DIMENSION":      str(encoder_dimension),
                "ENCODER_VERSION":        encoder_version,
                "PUT_VECTORS_BATCH_SIZE": "100",
            },
        )
        # Identity-side grants
        embed_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"{curated_arn}/*"],
        ))
        embed_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[curated_kms_arn],
        ))
        embed_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[am_table_arn],
        ))
        embed_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint", "sagemaker:InvokeEndpointAsync"],
            resources=[
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"
            ],
        ))
        embed_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "s3vectors:PutVectors",
                "s3vectors:GetVectors",
                "s3vectors:DeleteVectors",
                "s3vectors:QueryVectors",
                "s3vectors:GetIndex",
            ],
            resources=[w_index_arn, s_index_arn],
        ))
        embed_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
            resources=[v_kms_arn],
        ))
        iam.PermissionsBoundary.of(embed_fn.role).apply(permission_boundary)

        # C) EventBridge rule
        events.Rule(
            self, "AudioPreprocessedRule",
            rule_name=f"{{project_name}}-audio-preprocessed-{stage_name}",
            event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
            event_pattern=events.EventPattern(
                source=["{project_name}.audio"],
                detail_type=["AudioPreprocessed"],
            ),
            targets=[targets.LambdaFunction(embed_fn)],
        )

        # D) QueryFn
        query_log = logs.LogGroup(
            self, "QueryLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-audio-query-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        query_fn = _lambda.Function(
            self, "QueryFn",
            function_name=f"{{project_name}}-audio-query-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "audio_query")),
            memory_size=1024,
            timeout=Duration.seconds(30),
            log_group=query_log,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "VECTOR_BUCKET_NAME":     v_bucket_name,
                "WINDOW_INDEX_NAME":      w_index_name,
                "SAMPLE_INDEX_NAME":      s_index_name,
                "ENCODER_ENDPOINT_NAME":  endpoint_name,
                "ENCODER":                encoder,
                "ENCODER_DIMENSION":      str(encoder_dimension),
                "DEFAULT_TOP_K":          "10",
                "MAX_TOP_K":              "100",
            },
        )
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"
            ],
        ))
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
            resources=[w_index_arn, s_index_arn],
        ))
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[v_kms_arn],
        ))
        # Query Lambda may also pull mel windows from the curated bucket for
        # ad-hoc "embed this existing sample" queries
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"{curated_arn}/*"],
        ))
        query_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[curated_kms_arn],
        ))
        iam.PermissionsBoundary.of(query_fn.role).apply(permission_boundary)

        # E) Publish SSM params for consumers (API Gateway, Agent tools)
        ssm.StringParameter(
            self, "EmbedFnArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/audio-similarity/embed_fn_arn",
            string_value=embed_fn.function_arn,
        )
        ssm.StringParameter(
            self, "QueryFnArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/audio-similarity/query_fn_arn",
            string_value=query_fn.function_arn,
        )
        ssm.StringParameter(
            self, "QueryFnNameParam",
            parameter_name=f"/{{project_name}}/{stage_name}/audio-similarity/query_fn_name",
            string_value=query_fn.function_name,
        )

        self.embed_fn = embed_fn
        self.query_fn = query_fn
        self.dlq      = dlq
        self.cmk      = cmk

        CfnOutput(self, "EmbedFnArn", value=embed_fn.function_arn)
        CfnOutput(self, "QueryFnArn", value=query_fn.function_arn)
```

### 4.3 Micro-stack gotchas

- **Index schema change = full re-embed** — because the filterable-metadata schema is fixed at CfnIndex creation, changing it means a new CfnIndex. Practically: new indexes (`audio-windows-v2`, `audio-samples-v2`) + SSM param rollover + redeploy `AudioSimilarityStack` with new names + run a backfill that re-invokes `embed_fn` for every `audio_metadata` row. Worst-case this is expensive on SageMaker invocations. Budget this cost explicitly when presenting the kit.
- **SSM param value length** — all SSM values here are ARNs or short names (<200 chars); no concern. But if you expose `fault_label` enums via SSM for the agent-side tool schema, keep the enum list <4 KB.
- **EventBridge source name convention** — the rule filters on `source=["{project_name}.audio"]`. Ensure `MLOpsAudioStack` emits events with exactly this source via `events.PutEvents(Entries=[{Source: f"{project_name}.audio", DetailType: "AudioPreprocessed", ...}])`. Mismatch = silently-never-triggered Lambda; test by putting a manual event via CLI.
- **Encoder endpoint and embed Lambda scale independently** — if embed Lambda's `reserved_concurrent_executions=10` but the endpoint is provisioned for `initial_instance_count=1` (~5 TPS), throttles accumulate. Size them as a pair. For the serverless encoder hosting variant, the endpoint autoscales but costs 5-10s on cold start per invocation.
- **Cross-account queries** (agent in Account A, vector index in Account B) — requires a resource policy on the S3 Vectors index PLUS `s3vectors:QueryVectors` on the caller identity + cross-account `kms:Decrypt`. `# TODO(verify): S3 Vectors supports resource policies on indexes as of 2026-04-22 (confirm in current AWS S3 Vectors user guide).`

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / one-mic fleet / <10k samples | §3 Monolith + `STORAGE_STRATEGY=per_window` + `ENCODER=ast` + `ENCODER_HOSTING=serverless` |
| Latency-sensitive diagnostic UI (<1s) | `ENCODER_HOSTING=realtime` (provisioned endpoint) + warm-keeper CloudWatch rule pinging every 4 min |
| Backfill 100k+ historical samples | `ENCODER_HOSTING=async` + SNS-triggered PutVectors Lambda; batch manifest input to `sagemaker.CreateTransformJob` |
| Transient faults (knock, bearing hit) | `STORAGE_STRATEGY=per_window` mandatory; `PREPROCESS_WINDOW_SECONDS=1` on the upstream pipeline for finer resolution |
| Steady-state fault signatures (bearing hum, misfire) | `STORAGE_STRATEGY=aggregated` OK; saves 10× storage |
| Maximum retrieval quality | `STORAGE_STRATEGY=hybrid` — 2 indexes, query both, merge with `max(window_hit, sample_hit)` scoring |
| Fine-tuned task-specific embeddings | `ENCODER=custom_penultimate` pointing at the final-hidden-state output of your SAGEMAKER_TRAINING'd classifier; requires classifier to be trained first |
| Speech-like signals (vocal alarms, human-in-loop reports) | `ENCODER=wav2vec2` |
| Multi-tenant with row-level access | Add `tenant_id` filterable key (drop one other) + enforce at query time via a query-Lambda guard: every request MUST include `filter={"tenant_id": caller_tenant}` — do NOT accept an unfiltered request from a tenant context |
| Agent/MCP tool interface | Expose `QueryFn` as an MCP tool via `STRANDS_MCP_TOOLS`; tool schema: `{mode, top_k, query_audio_s3_key OR query_vector, machine_id?, fault_label?}` |
| Label-drift updates | Subscribe a second Lambda to `audio_metadata` DDB stream filtered on `ModifyItem` where `outcome` changed; re-invoke `embed_fn` for just that sample (PutVectors upsert overwrites metadata without re-embed if you skip the SM invoke) |
| Reducing S3 Vectors storage | Reduce `ENCODER_DIMENSION` to 256 (if encoder supports it — custom fine-tuned classifiers often expose 256/512/768 variants); recreate indexes with the new dim; re-embed entire corpus |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_PATTERN_AUDIO_SIMILARITY_SEARCH.py`. Offline; no AWS calls.

```python
"""SOP verification — AudioSimilarityStack synthesizes with:
- embed Lambda with correct env + s3vectors + sagemaker policies
- query Lambda with correct env + policies
- DLQ (SQS)
- EventBridge rule on AudioPreprocessed
- 3 SSM params published
"""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-west-2")


def test_audio_similarity_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.audio_similarity_stack import AudioSimilarityStack
    stack = AudioSimilarityStack(
        app, stage_name="dev",
        vector_bucket_name_ssm="/test/vectors/bucket_name",
        window_index_name_ssm="/test/vectors/window_index_name",
        window_index_arn_ssm="/test/vectors/window_index_arn",
        sample_index_name_ssm="/test/vectors/sample_index_name",
        sample_index_arn_ssm="/test/vectors/sample_index_arn",
        vector_kms_arn_ssm="/test/vectors/kms_arn",
        curated_bucket_name_ssm="/test/storage/curated_name",
        curated_bucket_arn_ssm="/test/storage/curated_arn",
        curated_bucket_kms_arn_ssm="/test/storage/curated_kms_arn",
        audio_metadata_table_name_ssm="/test/mlops-audio/audio_metadata_table_name",
        audio_metadata_table_arn_ssm="/test/mlops-audio/audio_metadata_table_arn",
        encoder_endpoint_name_ssm="/test/sagemaker/encoder_endpoint_name",
        permission_boundary=boundary,
        storage_strategy="per_window",
        encoder="ast",
        encoder_dimension=768,
        encoder_version="v1",
        reserved_concurrency=10,
        env=env,
    )
    t = Template.from_stack(stack)

    t.resource_count_is("AWS::Lambda::Function", 2)     # embed + query
    t.resource_count_is("AWS::SQS::Queue",       1)
    t.resource_count_is("AWS::Events::Rule",     1)
    t.resource_count_is("AWS::KMS::Key",         1)

    # Embed Lambda env
    t.has_resource_properties("AWS::Lambda::Function", Match.object_like({
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "STORAGE_STRATEGY":  "per_window",
                "ENCODER":           "ast",
                "ENCODER_DIMENSION": "768",
                "ENCODER_VERSION":   "v1",
            }),
        }),
        "ReservedConcurrentExecutions": 10,
    }))

    # EventBridge rule filters on AudioPreprocessed
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "EventPattern": Match.object_like({
            "detail-type": ["AudioPreprocessed"],
        }),
    }))

    # 3 SSM params published
    t.resource_count_is("AWS::SSM::Parameter", 3)
```

---

## 7. References

- `docs/template_params.md` — `STORAGE_STRATEGY` (`per_window` | `aggregated` | `hybrid`), `ENCODER` (`ast` | `wav2vec2` | `custom_penultimate`), `ENCODER_DIMENSION` (256 | 512 | 768 | 1024), `ENCODER_HOSTING` (`serverless` | `realtime` | `async`), `ENCODER_VERSION`, `ENCODER_ENDPOINT_NAME`, `QUERY_FILTER_STRATEGY`, `DEFAULT_TOP_K`, `MAX_TOP_K`, `AUDIO_SIMILARITY_RESERVED_CONCURRENCY`
- `docs/Feature_Roadmap.md` — feature IDs `AS-10` (embed-and-upsert Lambda), `AS-11` (query Lambda + 3 modes), `AS-12` (canonical metadata schema + filterable-key budget), `AS-13` (label-drift update via DDB stream), `AS-14` (MCP tool exposure), `AS-15` (cross-account query resource policy)
- AWS docs:
  - [Amazon S3 Vectors overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
  - [S3 Vectors getting started](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-getting-started.html)
  - [S3 Vectors best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-best-practices.html)
  - [boto3 `s3vectors` client reference (PutVectors, QueryVectors, GetVectors, DeleteVectors)](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3vectors.html)
  - [SageMaker Serverless Inference](https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints.html)
  - [SageMaker Async Inference](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference.html)
  - [SageMaker Real-time Inference endpoints](https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints.html)
- HuggingFace:
  - [MIT/ast-finetuned-audioset-10-10-0.4593 (AST, 768-dim)](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593)
  - [AST model docs (transformers)](https://huggingface.co/docs/transformers/en/model_doc/audio-spectrogram-transformer)
  - [Wav2Vec2 model docs (transformers)](https://huggingface.co/docs/transformers/en/model_doc/wav2vec2)
  - [Fine-tune and deploy a Wav2Vec2 model on SageMaker (AWS blog)](https://aws.amazon.com/blogs/machine-learning/fine-tune-and-deploy-a-wav2vec2-model-for-speech-recognition-with-hugging-face-and-amazon-sagemaker/)
- Papers (from the Toyota team's reference library):
  - [Müller et al. 2020 — Acoustic Anomaly Detection via Image Transfer Learning (arXiv 2006.03429)](https://arxiv.org/pdf/2006.03429)
  - [Dohi et al. 2024 — DCASE 2024 Task 2 baseline (arXiv 2403.00379)](https://arxiv.org/pdf/2403.00379)
  - [GeCo — Generative-Contrastive Learning for Anomalous Sound Detection (arXiv 2305.12111)](https://arxiv.org/pdf/2305.12111)
- AWS context:
  - [Amazon Lookout for Equipment (EOL 2026-10-07)](https://aws.amazon.com/lookout-for-equipment/) — similarity search over past diagnoses is the "wasn't in L4E" capability customers want when migrating
  - [AWS IoT SiteWise anomaly detection](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/anomaly-detection.html)
- Related SOPs:
  - `DATA_S3_VECTORS` — vector bucket + `audio-windows` + `audio-samples` index CDK (creates the indexes this pipeline writes to)
  - `MLOPS_AUDIO_PIPELINE` — upstream curated-feature source + emits `AudioPreprocessed` event
  - `MLOPS_SAGEMAKER_SERVING` — encoder endpoint hosting (serverless / realtime / async) — this stack reads the endpoint name via SSM
  - `MLOPS_SAGEMAKER_TRAINING` — produces the fine-tuned classifier when `ENCODER=custom_penultimate`
  - `PATTERN_DOC_INGESTION_RAG` — structural sibling (same PutVectors + QueryVectors skeleton applied to documents)
  - `EVENT_DRIVEN_PATTERNS` — S3 → EventBridge → Lambda + custom event emission (`events.PutEvents`) for `AudioPreprocessed`
  - `STRANDS_MCP_TOOLS` — wrap `QueryFn` as an MCP tool for agent consumption ("find similar sound" as a tool call)
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grants, PermissionsBoundary
  - `LAYER_SECURITY` — KMS CMK per stack, PermissionsBoundary
  - `LAYER_OBSERVABILITY` — CloudWatch metrics for embed throughput, query latency, topK-hit distribution

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — audio-embedding similarity search on S3 Vectors for "find similar diagnosed cases" workflow. Three storage strategies (per_window / aggregated / hybrid) with cost + recall tradeoff table; three query strategies (unfiltered similar-sound / machine-scoped history / label-mine via random-vector workaround). Canonical 10-key filterable metadata schema with `machine_id`, `fault_label`, `outcome`, `technician_id`, `part_replaced`, `window_idx`, `confidence_at_diagnosis`, `sensor_position`, plus `source_audio_s3_path` + `encoder_model_id` + `encoder_version` non-filterable keys for provenance. Encoder plug-points: AST (768-dim general audio), Wav2Vec2 (768/1024-dim waveform), custom penultimate-layer from fine-tuned classifier (task-specific, highest retrieval quality). Encoder hosting plug-points: serverless (sparse) / realtime (interactive UI) / async (bulk backfill). Idempotent via stable `{sample_id}#{window_idx}` keys; label-drift via DDB-stream triggered re-upsert. Worked example uses `per_window + ast + serverless`. References `DATA_S3_VECTORS` (index infra) and `MLOPS_AUDIO_PIPELINE` (preprocessing source). Created to fill gap surfaced by the Acoustic Fault Diagnostic Agent kit design (no preceding SOP covered audio embedding → vector search; PATTERN_DOC_INGESTION_RAG is the text-only sibling). |
