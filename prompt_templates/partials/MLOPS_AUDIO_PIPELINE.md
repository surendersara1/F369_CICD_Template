# SOP — Audio ML Pipeline (ingest → preprocess → feature-extract → curated)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · S3 raw-audio + EventBridge · `librosa` 0.10.x · `torchaudio` 2.x (optional) · `pywavelets` 1.6+ · `scipy` 1.13+ · `soundfile` 0.12+ · Lambda container image OR SageMaker Processing Fargate · DynamoDB `audio_metadata` · KMS CMK per stack

---

## 1. Purpose

- Codify the canonical **audio upload → resample → trim → segment → feature-extract → curated-zone save** pipeline for ML classifier training AND inference. Raw WAV/MP3/M4A lands in an S3 raw-audio bucket; S3 → EventBridge → either `PreprocessLambda` (low-volume) or a SageMaker Processing job (batch / high-volume) materialises per-window feature tensors into a curated S3 zone.
- Provide **two compute branches** wired to the same feature-extraction script:
  - `PROCESSING_MODE=lambda` — container-image Lambda with `librosa` + `pywt` + `scipy` baked in. Good for <10 uploads/min, small (<60s) samples.
  - `PROCESSING_MODE=sagemaker_processing` — Fargate-backed Processing job with the same Dockerfile. Good for bulk backfills, long samples, or augmentation jobs that would blow the Lambda 15 min / 10 GB memory ceiling.
- Codify the **feature-set plug-points** grounded in Toyota acoustic-fault experiments + the "Hybrid CNN+LSTM for vehicle type identification" paper — mel-spectrogram, MFCC, PCP (12-bin chromagram from CQT), short-term energy, wavelet CWT. Each is a toggle, not a fork — the same handler emits any subset the `FEATURES` env var requests.
- Provide the **`audio_metadata` DynamoDB table** — one row per uploaded sample — with lifecycle `uploaded → preprocessed → feature_extracted → indexed → failed`, `window_count`, `feature_keys`, and `preprocessed_s3_prefix` for downstream classifier / similarity-search consumers.
- Codify **optional augmentation** (SpecAugment, ESC-50 noise superimposition at 0/5/10/15/20 dB SNR, time-stretch, pitch-shift) behind a single `AUGMENTATION_ENABLED` env toggle so the same stack trains one way and infers another.
- Include when the SOW signals: "acoustic fault detection", "engine sound classification", "industrial anomaly sound", "MIMII / DCASE", "Toyota audio", "voice-of-the-machine", "predictive maintenance from sound", "ingest recordings and train a classifier".
- **Does not** cover classifier training (→ `MLOPS_SAGEMAKER_TRAINING`), serving (→ `MLOPS_SAGEMAKER_SERVING`), batch scoring (→ `MLOPS_BATCH_TRANSFORM`), or similarity search on embeddings (→ `PATTERN_AUDIO_SIMILARITY_SEARCH`). This partial stops at "curated features on S3 + DDB metadata row marked `feature_extracted`".

---

## 2. Decision — Monolith vs Micro-Stack + compute/feature choices

### 2.1 Structural split

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the raw-audio bucket, curated bucket, KMS CMK, `audio_metadata` table, `PreprocessLambda`, and (optionally) the SageMaker Processing job definition | **§3 Monolith Variant** |
| `StorageStack` owns raw + curated buckets; `MLOpsAudioStack` owns pipeline Lambda + Processing job definition + `audio_metadata` table + DLQ; downstream `TrainingStack` + `VectorStoreStack` consume `preprocessed_s3_prefix` via SSM | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Raw + curated buckets + EventBridge rule**: same cycle risk as `EVENT_DRIVEN_PATTERNS` §5 — never split a bucket + its S3 notification across stacks. Buckets in `StorageStack` with `event_bridge_enabled=True`, rule owned in `MLOpsAudioStack` using L2 `events.Rule` with `targets.LambdaFunction(local_fn)` (safe because target is in THIS stack).
2. **SageMaker Processing role**: Processing jobs need a role that can read the raw bucket, write the curated bucket, decrypt both KMS CMKs, pull the container image from ECR, and write CloudWatch logs. If you put the role in a different stack from the Processing job definition, you must pass the role ARN via SSM — and the Lambda that kicks off the job needs `iam:PassRole` with `iam:PassedToService=sagemaker.amazonaws.com`.
3. **Container image (ECR)** for `librosa` + `torchaudio` + `pywt` is often 1.5–2.5 GB — too big for Lambda ZIP-layer deployment. You must use `_lambda.DockerImageFunction` (Lambda container image mode, 10 GB limit) OR a Processing job. The Dockerfile is the single source of truth and lives under `docker/audio_preprocess/` regardless of variant.
4. **Curated-zone KMS CMK**: if training ever runs in a different account (AWS-team spoke pattern), the CMK policy needs `kms:Decrypt` for the training role's account. Keep the CMK in the same stack as the curated bucket; expose the KMS ARN via SSM.

Micro-Stack variant fixes all of this via: (a) buckets + KMS in `StorageStack` with `event_bridge_enabled=True`; (b) `MLOpsAudioStack` owns the Lambda + Processing job + DDB + DLQ + the role that runs the Processing job; (c) every cross-stack handle is a string (bucket name, bucket ARN, KMS ARN, Processing image URI) via SSM.

### 2.2 Plug-point matrix

| Plug-point | Variant | Use when |
|---|---|---|
| `PROCESSING_MODE` | `lambda` | POC; short samples (<60s); <10 uploads/min; no torchaudio needed |
| `PROCESSING_MODE` | `sagemaker_processing` | Bulk backfill; samples >60s; torchaudio / AST embeddings; augmentation workload |
| `PROCESSING_MODE` | `hybrid` | Small samples → Lambda; long samples → SageMaker (branch by duration in `IngestionLambda`) |
| `FEATURES` | `mel_spectrogram` | Default. Input to AST / CNN / VGGish. 64 or 128 mel bins |
| `FEATURES` | `mfcc` | Classical classifier; speech-like characteristics; 40 coefficients |
| `FEATURES` | `pcp` | Pitch-class profile (12-bin CQT chromagram) — useful for harmonic / timbral fault signatures (bearing hum, misfire) |
| `FEATURES` | `short_term_energy` | Scalar per frame; cheap anomaly baseline; pair with MFCC+PCP in the novel 65-dim fusion feature (see §3.4) |
| `FEATURES` | `wavelet_cwt` | Transient faults (knock, injector tick, bearing hit) — CWT catches impulsive events that FFT-based features smooth over |
| `FEATURES` | `raw_waveform` | Downstream model is Wav2Vec2 or an end-to-end 1D CNN; skip spectral extraction |
| `AUGMENTATION_ENABLED` | `false` | Inference path; fault-diagnosis consumer wants deterministic features |
| `AUGMENTATION_ENABLED` | `true` | Training set expansion; writes N augmented copies per sample (SpecAugment + noise + time-stretch + pitch-shift) |
| `PREPROCESS_SAMPLE_RATE` | `44100` | Default. CD quality; reliable for engine sounds (Toyota team standard) |
| `PREPROCESS_SAMPLE_RATE` | `16000` | AST / Wav2Vec2 downstream (those models are trained at 16 kHz; resample to match) |
| `PREPROCESS_WINDOW_SECONDS` | `5` | Default. Balance between context + segmentation granularity |
| `PREPROCESS_WINDOW_SECONDS` | `10` | AST 10s-input model; DCASE 2020 Task 2 convention |

The **canonical worked example** in §3 uses `PROCESSING_MODE=lambda` + `FEATURES=mel_spectrogram,mfcc` + `AUGMENTATION_ENABLED=false` + 44.1 kHz + 5s windows. Other combinations are swap-matrix rows in §5.

---

## 3. Monolith Variant

### 3.1 Architecture

```
                    [ Technician / telematics / batch uploader ]
                              │  PUT s3://raw-audio/{sample_id}.wav
                              ▼
                    ┌─────────────────────┐
                    │  raw-audio bucket   │  event_bridge_enabled=True
                    │  SSE-KMS            │
                    └──────────┬──────────┘
                              │  S3 ObjectCreated
                              ▼
                    EventBridge default bus
                              │  filter: detail.object.key suffix in {.wav, .mp3, .m4a, .flac}
                              ▼
                    ┌─────────────────────────────────────────┐
                    │  IngestionLambda (small, ZIP)           │
                    │  1. Validates extension + HEAD size     │
                    │  2. DDB: upsert status=uploaded         │
                    │  3. Branches on PROCESSING_MODE:        │
                    │       lambda            → invoke sync   │
                    │       sagemaker_processing → start job  │
                    │       hybrid            → duration fork │
                    └─────────┬─────────────────┬─────────────┘
                              │                 │
                              ▼                 ▼
        ┌─────────────────────────────┐   ┌──────────────────────────────┐
        │ PreprocessLambda            │   │ SageMaker Processing Job     │
        │  (DockerImageFunction,      │   │  (Fargate, same container    │
        │   librosa + pywt + scipy)   │   │   image URI)                 │
        │ • librosa.load(resample)    │   │ • same preprocess.py script  │
        │ • librosa.effects.trim      │   │ • ProcessingInput reads raw  │
        │ • segment windows           │   │ • ProcessingOutput writes    │
        │ • extract_features(FEATURES)│   │   curated                    │
        │ • optional augmentation     │   │ • emits CW metrics           │
        │ • save .npy / parquet       │   │                              │
        └─────────┬───────────────────┘   └──────────┬───────────────────┘
                  │                                  │
                  └──────────────┬───────────────────┘
                                 ▼
                    ┌─────────────────────────────────────────┐
                    │  curated-audio bucket                   │
                    │  s3://curated-audio/{sample_id}/        │
                    │     mel_spec/window_{i:03d}.npy         │
                    │     mfcc/window_{i:03d}.npy             │
                    │     pcp/window_{i:03d}.npy              │
                    │     energy/window_{i:03d}.npy           │
                    │     cwt/window_{i:03d}.npy              │
                    │     metadata.json                       │
                    └─────────────────────────────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────────────────────┐
                    │  audio_metadata DDB                     │
                    │  status=feature_extracted               │
                    │  window_count=N, feature_keys=[...]     │
                    └─────────────────────────────────────────┘
                                 │  on failure
                                 ▼
                    ┌─────────────────────┐
                    │  DLQ (SQS)          │  redrive via standard DLQ reprocessor
                    └─────────────────────┘
```

### 3.2 CDK — `_create_audio_pipeline()` method body

```python
from pathlib import Path

from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_dynamodb as ddb,
    aws_ecr_assets as ecr_assets,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
)


def _create_audio_pipeline(self, stage: str) -> None:
    """Monolith. Assumes self.{kms_key, raw_audio_bucket (with
    event_bridge_enabled=True), curated_audio_bucket} already exist — either
    built inline in this stack or imported from an outer stack."""

    # A) audio_metadata table — one row per uploaded sample
    self.audio_metadata = ddb.Table(
        self, "AudioMetadata",
        table_name=f"{{project_name}}-audio-metadata-{stage}",
        partition_key=ddb.Attribute(name="sample_id", type=ddb.AttributeType.STRING),
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
    self.audio_metadata.add_global_secondary_index(
        index_name="by-machine",
        partition_key=ddb.Attribute(name="machine_id",   type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(     name="uploaded_at",  type=ddb.AttributeType.STRING),
        projection_type=ddb.ProjectionType.KEYS_ONLY,
    )
    self.audio_metadata.add_global_secondary_index(
        index_name="by-status",
        partition_key=ddb.Attribute(name="status",       type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(     name="uploaded_at",  type=ddb.AttributeType.STRING),
        projection_type=ddb.ProjectionType.KEYS_ONLY,
    )

    # B) DLQ
    self.preprocess_dlq = sqs.Queue(
        self, "PreprocessDlq",
        queue_name=f"{{project_name}}-audio-preprocess-dlq-{stage}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )

    # C) Container image — librosa + pywt + scipy + soundfile.
    #    Dockerfile at docker/audio_preprocess/Dockerfile (see §3.3).
    preprocess_image = ecr_assets.DockerImageAsset(
        self, "AudioPreprocessImage",
        directory=str(Path(__file__).resolve().parents[3] / "docker" / "audio_preprocess"),
        platform=ecr_assets.Platform.LINUX_ARM64,
    )

    # D) PreprocessLambda — container image, ARM64, 10 GB memory
    log = logs.LogGroup(
        self, "PreprocessLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-audio-preprocess-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.preprocess_fn = _lambda.DockerImageFunction(
        self, "PreprocessFn",
        function_name=f"{{project_name}}-audio-preprocess-{stage}",
        code=_lambda.DockerImageCode.from_ecr(
            repository=preprocess_image.repository,
            tag_or_digest=preprocess_image.image_tag,
        ),
        architecture=_lambda.Architecture.ARM_64,
        memory_size=10240,                     # 10 GB — feature extraction is memory-heavy
        ephemeral_storage_size=Duration.seconds(0) and None,  # default /tmp 512 MB is enough
        timeout=Duration.minutes(15),
        log_group=log,
        tracing=_lambda.Tracing.ACTIVE,
        dead_letter_queue_enabled=True,
        dead_letter_queue=self.preprocess_dlq,
        reserved_concurrent_executions=20,
        environment={
            "RAW_BUCKET":           self.raw_audio_bucket.bucket_name,
            "CURATED_BUCKET":       self.curated_audio_bucket.bucket_name,
            "AUDIO_METADATA_TABLE": self.audio_metadata.table_name,
            "FEATURES":             "mel_spectrogram,mfcc",   # comma-sep toggles
            "PREPROCESS_SAMPLE_RATE":   "44100",
            "PREPROCESS_WINDOW_SECONDS": "5",
            "PREPROCESS_OVERLAP":        "0.5",                # 50%
            "N_FFT":                "1024",
            "HOP_LENGTH":           "512",
            "N_MELS":               "64",
            "N_MFCC":               "40",
            "TRIM_TOP_DB":          "40",                      # conservative; see gotchas
            "AUGMENTATION_ENABLED": "false",
            "PROCESSING_MODE":      "lambda",
            "POWERTOOLS_SERVICE_NAME": "{project_name}-audio-preprocess",
            "POWERTOOLS_LOG_LEVEL":    "INFO",
        },
    )

    # E) Grants — monolith, L2 safe.
    self.raw_audio_bucket.grant_read(self.preprocess_fn)
    self.curated_audio_bucket.grant_read_write(self.preprocess_fn)
    self.audio_metadata.grant_read_write_data(self.preprocess_fn)
    # KMS — L2 encryption-key grants are identity-side under the hood.
    self.kms_key.grant_encrypt_decrypt(self.preprocess_fn)

    # F) IngestionLambda — tiny router. Pure-Python ZIP; no librosa here.
    self.ingest_fn = _lambda.Function(
        self, "AudioIngestFn",
        function_name=f"{{project_name}}-audio-ingest-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset(
            str(Path(__file__).resolve().parents[3] / "lambda" / "audio_ingest")
        ),
        memory_size=512,
        timeout=Duration.minutes(1),
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "AUDIO_METADATA_TABLE": self.audio_metadata.table_name,
            "PREPROCESS_FN_NAME":   self.preprocess_fn.function_name,
            "PROCESSING_MODE":      "lambda",
            "MAX_DURATION_SECONDS": "600",           # reject anything longer than 10 min
            "ALLOWED_EXTENSIONS":   ".wav,.mp3,.m4a,.flac",
        },
    )
    self.audio_metadata.grant_read_write_data(self.ingest_fn)
    self.preprocess_fn.grant_invoke(self.ingest_fn)
    self.raw_audio_bucket.grant_read(self.ingest_fn)

    # G) EventBridge rule: raw-audio ObjectCreated → IngestionLambda
    events.Rule(
        self, "AudioUploadedRule",
        rule_name=f"{{project_name}}-audio-uploaded-{stage}",
        event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
        event_pattern=events.EventPattern(
            source=["aws.s3"],
            detail_type=["Object Created"],
            detail={
                "bucket": {"name": [self.raw_audio_bucket.bucket_name]},
                "object": {"key": [
                    {"suffix": ".wav"},
                    {"suffix": ".mp3"},
                    {"suffix": ".m4a"},
                    {"suffix": ".flac"},
                ]},
            },
        ),
        targets=[targets.LambdaFunction(self.ingest_fn)],
    )

    CfnOutput(self, "AudioMetadataTable", value=self.audio_metadata.table_name)
    CfnOutput(self, "PreprocessFnArn",    value=self.preprocess_fn.function_arn)
    CfnOutput(self, "PreprocessImageUri", value=preprocess_image.image_uri)
```

### 3.3 Dockerfile — saved to `docker/audio_preprocess/Dockerfile`

```dockerfile
# Base: AWS Lambda Python 3.12 ARM64 — same image used for both Lambda and
# SageMaker Processing (SageMaker Processing accepts any container exposing
# a /opt/ml/processing/input + /opt/ml/processing/output contract; our
# script reads from env vars, not the container contract, so the same
# image works in both modes).
FROM public.ecr.aws/lambda/python:3.12-arm64

# OS deps for librosa / soundfile / scipy
RUN dnf install -y --setopt=install_weak_deps=False \
        gcc gcc-c++ make \
        libsndfile \
        && dnf clean all

# Python deps — pin versions for reproducibility.
# librosa 0.10.x is the Toyota team's verified version (preprocessing.py).
# pywavelets 1.6+ is the current maintained line.
# torchaudio is OPTIONAL — only include if you plan to compute Wav2Vec2 /
# AST embeddings here. Drop it for a ~1.5 GB smaller image.
COPY docker/audio_preprocess/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the preprocessing script (shared with SageMaker Processing).
COPY lambda/audio_preprocess/ ${LAMBDA_TASK_ROOT}/

CMD ["index.lambda_handler"]
```

`docker/audio_preprocess/requirements.txt`:

```
# Audio I/O + DSP
librosa==0.10.2
soundfile==0.12.1
scipy==1.13.1
numpy==1.26.4

# Transient-fault features
PyWavelets==1.6.0

# AWS
boto3>=1.34.0
aws-lambda-powertools==3.2.0

# OPTIONAL — only for AST / Wav2Vec2 embedding paths. Remove to shrink
# image from ~2.3 GB to ~700 MB.
# torch==2.3.0
# torchaudio==2.3.0
```

### 3.4 Preprocessing handler — saved to `lambda/audio_preprocess/index.py`

```python
"""Audio preprocessing handler — resample, trim, segment, extract features.

Triggered by IngestionLambda (sync invoke) or SageMaker Processing job (run
as a plain Python script with the same env contract). Idempotent: the
curated S3 prefix is `{sample_id}/` so re-processing overwrites.

Feature set is driven by the FEATURES env var (comma-separated). Supported:
  - mel_spectrogram  : librosa.feature.melspectrogram → dB
  - mfcc             : librosa.feature.mfcc (n_mfcc)
  - pcp              : librosa.feature.chroma_cqt + first-order diff (24-dim)
  - short_term_energy: sum(x^2) per frame
  - wavelet_cwt      : pywt.cwt with Morlet wavelet
  - raw_waveform     : dump the resampled, trimmed mono signal as .npy

Verified reference implementation: Toyota Car Sounds Team's preprocessing.py
(Joint Generative-Contrastive Representation experiment) + the Nature
Scientific Reports 2021 hybrid CNN+LSTM vehicle-ID paper for the 40-dim
MFCC + 24-dim PCP + 1-dim STE = 65-dim fusion feature.
"""
import io
import json
import logging
import os
import time
from pathlib import Path

import boto3
import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3     = boto3.client("s3")
ddb    = boto3.resource("dynamodb").Table(os.environ["AUDIO_METADATA_TABLE"])

RAW_BUCKET       = os.environ["RAW_BUCKET"]
CURATED_BUCKET   = os.environ["CURATED_BUCKET"]
FEATURES         = [f.strip() for f in os.environ.get("FEATURES", "mel_spectrogram").split(",")]
SAMPLE_RATE      = int(os.environ.get("PREPROCESS_SAMPLE_RATE", "44100"))
WINDOW_SECONDS   = float(os.environ.get("PREPROCESS_WINDOW_SECONDS", "5"))
OVERLAP          = float(os.environ.get("PREPROCESS_OVERLAP", "0.5"))
N_FFT            = int(os.environ.get("N_FFT", "1024"))
HOP_LENGTH       = int(os.environ.get("HOP_LENGTH", "512"))
N_MELS           = int(os.environ.get("N_MELS", "64"))
N_MFCC           = int(os.environ.get("N_MFCC", "40"))
TRIM_TOP_DB      = float(os.environ.get("TRIM_TOP_DB", "40"))
AUGMENTATION     = os.environ.get("AUGMENTATION_ENABLED", "false").lower() == "true"
CWT_SCALES       = np.arange(1, int(os.environ.get("CWT_MAX_SCALE", "64")))
CWT_WAVELET      = os.environ.get("CWT_WAVELET", "morl")


class PermanentError(Exception):
    """Unrecoverable — DDB marked failed, NOT re-raised."""


def lambda_handler(event, _ctx):
    """Event shape (from IngestionLambda.invoke):
         {"sample_id": "...", "bucket": "...", "key": "..."}
    """
    sample_id = event["sample_id"]
    key       = event["key"]
    bucket    = event.get("bucket", RAW_BUCKET)
    now       = _now()

    _mark_status(sample_id, "preprocessing", extra={"started_at": now})

    try:
        # Defer the heavy import so the Lambda cold-start stays bounded.
        import librosa
        import pywt
        import soundfile as sf

        # 1) Download raw audio
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

        # 2) Load + resample. sr=SAMPLE_RATE forces consistent rate across
        #    heterogeneous uploads. mono=True → single channel (industrial
        #    mics are typically mono; stereo channels aren't separately
        #    meaningful for this classifier family).
        signal, sr = librosa.load(io.BytesIO(body), sr=SAMPLE_RATE, mono=True)
        if signal.size == 0:
            raise PermanentError("librosa loaded zero-length signal")

        # 3) Normalize amplitude (prevents clipping-dependent feature drift).
        peak = float(np.max(np.abs(signal)))
        if peak > 0:
            signal = signal / peak

        # 4) Trim silence. top_db=40 is conservative (engines idle quietly);
        #    top_db=20 strips idle segments — for QA/fault-diagnosis prefer 40.
        trimmed, _ = librosa.effects.trim(signal, top_db=TRIM_TOP_DB)
        if trimmed.size < int(SAMPLE_RATE * 0.5):     # <0.5s post-trim
            raise PermanentError("trimmed signal shorter than 0.5s")

        # 5) Segment into overlapping windows.
        windows = _segment(trimmed, sr, WINDOW_SECONDS, OVERLAP)
        if not windows:
            raise PermanentError("segmentation produced zero windows")

        # 6) Per-window feature extraction. Emit one npy per feature per window.
        feature_keys: list[str] = []
        for i, window in enumerate(windows):
            sample_uris = _extract_and_save(sample_id, i, window, sr, librosa, pywt)
            feature_keys.extend(sample_uris)

        # 7) Optional augmentation (training path only).
        if AUGMENTATION:
            from audio_augment import augment_window
            for i, window in enumerate(windows):
                for variant_idx, aug in enumerate(augment_window(window, sr)):
                    aug_uris = _extract_and_save(
                        sample_id, i, aug, sr, librosa, pywt,
                        variant=f"aug{variant_idx:02d}",
                    )
                    feature_keys.extend(aug_uris)

        # 8) Write a single metadata.json to the curated prefix.
        s3.put_object(
            Bucket=CURATED_BUCKET,
            Key=f"{sample_id}/metadata.json",
            Body=json.dumps({
                "sample_id":     sample_id,
                "sample_rate":   sr,
                "duration_sec":  float(trimmed.size / sr),
                "n_windows":     len(windows),
                "features":      FEATURES,
                "augmented":     AUGMENTATION,
                "created_at":    _now(),
            }).encode("utf-8"),
            ContentType="application/json",
        )

        # 9) DDB: mark feature_extracted
        ddb.update_item(
            Key={"sample_id": sample_id},
            UpdateExpression=(
                "SET #s = :s, window_count = :n, feature_keys = :fk, "
                "preprocessed_s3_prefix = :p, completed_at = :t"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":  "feature_extracted",
                ":n":  len(windows),
                ":fk": FEATURES,
                ":p":  f"s3://{CURATED_BUCKET}/{sample_id}/",
                ":t":  _now(),
            },
        )
        logger.info("preprocess ok sample=%s windows=%d features=%s",
                    sample_id, len(windows), FEATURES)
        return {"sample_id": sample_id, "windows": len(windows), "status": "feature_extracted"}

    except PermanentError as e:
        logger.warning("permanent failure sample=%s reason=%s", sample_id, e)
        _mark_failed(sample_id, f"permanent:{e}")
        return {"sample_id": sample_id, "status": "failed", "reason": str(e)}
    except Exception:
        logger.exception("transient failure sample=%s", sample_id)
        _mark_failed(sample_id, "transient:see_logs")
        raise


# -------------------------------------------------------------- segmentation

def _segment(signal: np.ndarray, sr: int, window_sec: float,
             overlap_frac: float) -> list[np.ndarray]:
    window_samples = int(window_sec * sr)
    hop_samples    = max(1, int(window_samples * (1.0 - overlap_frac)))
    if signal.size < window_samples:
        # Pad short signals with zeros to exactly one window
        padded = np.zeros(window_samples, dtype=signal.dtype)
        padded[: signal.size] = signal
        return [padded]
    windows = []
    for start in range(0, signal.size - window_samples + 1, hop_samples):
        windows.append(signal[start : start + window_samples])
    return windows


# -------------------------------------------------------------- features

def _extract_and_save(sample_id: str, window_idx: int, window: np.ndarray,
                      sr: int, librosa, pywt, variant: str = "orig") -> list[str]:
    uris: list[str] = []
    prefix_base = f"{sample_id}/{variant}"

    if "mel_spectrogram" in FEATURES:
        mel = librosa.feature.melspectrogram(
            y=window, sr=sr, n_fft=N_FFT, hop_length=HOP_LENGTH, n_mels=N_MELS,
        )
        mel = np.clip(mel, a_min=1e-10, a_max=None)
        mel_db = librosa.power_to_db(mel, ref=np.max)
        uris.append(_put_npy(f"{prefix_base}/mel_spec/window_{window_idx:03d}.npy",
                             mel_db.T.astype(np.float32)))

    if "mfcc" in FEATURES:
        mfcc = librosa.feature.mfcc(
            y=window, sr=sr, n_mfcc=N_MFCC, n_fft=N_FFT, hop_length=HOP_LENGTH,
        )
        uris.append(_put_npy(f"{prefix_base}/mfcc/window_{window_idx:03d}.npy",
                             mfcc.T.astype(np.float32)))

    if "pcp" in FEATURES:
        # PCP = 12-bin CQT chromagram + first-order diff → 24 dim.
        chroma = librosa.feature.chroma_cqt(y=window, sr=sr, hop_length=HOP_LENGTH)
        diff   = np.diff(chroma, axis=1, prepend=chroma[:, :1])
        pcp    = np.concatenate([chroma, diff], axis=0)          # (24, T)
        uris.append(_put_npy(f"{prefix_base}/pcp/window_{window_idx:03d}.npy",
                             pcp.T.astype(np.float32)))

    if "short_term_energy" in FEATURES:
        # Frame-wise sum of squares. Matches the hybrid CNN+LSTM paper spec.
        frames = librosa.util.frame(window, frame_length=N_FFT,
                                    hop_length=HOP_LENGTH, axis=0)
        energy = np.sum(frames ** 2, axis=-1).astype(np.float32)  # (T,)
        uris.append(_put_npy(f"{prefix_base}/energy/window_{window_idx:03d}.npy",
                             energy))

    if "wavelet_cwt" in FEATURES:
        # pywt.cwt returns (scales, n_samples) — memory-heavy. Cap scales
        # at CWT_MAX_SCALE (default 64); chunk internally if window is long.
        coeffs, _freqs = pywt.cwt(window, CWT_SCALES, CWT_WAVELET, 1.0 / sr)
        uris.append(_put_npy(f"{prefix_base}/cwt/window_{window_idx:03d}.npy",
                             coeffs.astype(np.float32)))

    if "raw_waveform" in FEATURES:
        uris.append(_put_npy(f"{prefix_base}/raw/window_{window_idx:03d}.npy",
                             window.astype(np.float32)))

    return uris


def _put_npy(key: str, arr: np.ndarray) -> str:
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    s3.put_object(Bucket=CURATED_BUCKET, Key=key, Body=buf.getvalue(),
                  ContentType="application/octet-stream")
    return f"s3://{CURATED_BUCKET}/{key}"


# -------------------------------------------------------------- helpers

def _mark_status(sample_id: str, status: str, extra: dict | None = None) -> None:
    expr = "SET #s = :s, updated_at = :u"
    names = {"#s": "status"}
    values = {":s": status, ":u": _now()}
    if extra:
        for k, v in extra.items():
            expr += f", {k} = :{k}"
            values[f":{k}"] = v
    ddb.update_item(
        Key={"sample_id": sample_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _mark_failed(sample_id: str, reason: str) -> None:
    ddb.update_item(
        Key={"sample_id": sample_id},
        UpdateExpression="SET #s = :s, failure_reason = :r, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "failed", ":r": reason, ":u": _now()},
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
```

### 3.5 Ingestion Lambda — saved to `lambda/audio_ingest/index.py`

```python
"""Thin router Lambda — S3 → EventBridge → here → PreprocessFn.invoke.

Separating ingestion from preprocessing keeps the hot image (~2 GB with
librosa + pywt) off the per-event cold-start path for simple upload
validations. This handler is ~50 ms; PreprocessFn does the heavy lifting.
"""
import json
import logging
import os
import time
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3     = boto3.client("s3")
lam    = boto3.client("lambda")
ddb    = boto3.resource("dynamodb").Table(os.environ["AUDIO_METADATA_TABLE"])
sm     = boto3.client("sagemaker")

PREPROCESS_FN_NAME  = os.environ["PREPROCESS_FN_NAME"]
PROCESSING_MODE     = os.environ.get("PROCESSING_MODE", "lambda")
MAX_DURATION        = int(os.environ.get("MAX_DURATION_SECONDS", "600"))
ALLOWED_EXTS        = tuple(os.environ.get("ALLOWED_EXTENSIONS", ".wav,.mp3,.m4a,.flac").split(","))


def lambda_handler(event, _ctx):
    detail    = event["detail"]
    bucket    = detail["bucket"]["name"]
    key       = detail["object"]["key"]

    if not key.lower().endswith(ALLOWED_EXTS):
        logger.info("skipping non-audio key=%s", key)
        return {"skipped": True, "reason": "extension"}

    sample_id = _sample_id_from_key(key)

    # HEAD — reject obviously too-large files before download.
    head = s3.head_object(Bucket=bucket, Key=key)
    size_bytes = head["ContentLength"]
    if size_bytes > 500 * 1024 * 1024:       # 500 MB ceiling
        _upsert(sample_id, key, bucket, size_bytes, status="failed",
                failure_reason="size_exceeded")
        return {"sample_id": sample_id, "status": "rejected", "reason": "size"}

    _upsert(sample_id, key, bucket, size_bytes, status="uploaded")

    if PROCESSING_MODE == "lambda":
        lam.invoke(
            FunctionName=PREPROCESS_FN_NAME,
            InvocationType="Event",                       # async
            Payload=json.dumps({
                "sample_id": sample_id, "bucket": bucket, "key": key,
            }).encode("utf-8"),
        )
        return {"sample_id": sample_id, "mode": "lambda", "dispatched": True}

    # sagemaker_processing / hybrid mode — caller wires the job name + role
    # ARN via env; we don't hard-code it here because it depends on the
    # deployed Processing job definition.
    raise NotImplementedError("configure PROCESSING_MODE=sagemaker_processing "
                              "via the SageMaker Processing job in the stack")


def _sample_id_from_key(key: str) -> str:
    base = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return base or f"sample-{uuid.uuid4().hex[:12]}"


def _upsert(sample_id: str, key: str, bucket: str, size_bytes: int,
            status: str, failure_reason: str | None = None) -> None:
    expr_parts = [
        "#s = :s", "s3_key = :k", "s3_bucket = :b", "size_bytes = :sz",
        "uploaded_at = :u",
        "#t = if_not_exists(#t, :ttl)",
    ]
    names  = {"#s": "status", "#t": "ttl"}
    values = {
        ":s": status, ":k": key, ":b": bucket, ":sz": size_bytes,
        ":u": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ":ttl": int(time.time()) + 90 * 86400,
    }
    if failure_reason:
        expr_parts.append("failure_reason = :r")
        values[":r"] = failure_reason
    ddb.update_item(
        Key={"sample_id": sample_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
```

### 3.6 Optional augmentation — saved to `lambda/audio_preprocess/audio_augment.py`

```python
"""SpecAugment + noise superimposition + time-stretch + pitch-shift.

Loaded ONLY when AUGMENTATION_ENABLED=true. Verified augmentation recipe
from the Nature Scientific Reports 2021 hybrid CNN+LSTM paper:
 - Noise superimposition at 0/5/10/15/20 dB SNR from ESC-50 dataset
   (wind, rain, thunderstorm, helicopter) raised recognition accuracy
   94.5% → 98.46% at 0 dB SNR.
 - Time-stretch and pitch-shift via librosa.effects.
 - SpecAugment (time + frequency masking) is applied on the downstream
   mel-spectrogram side — see HuggingFace SpecAugment pattern.
"""
import os
import random
import urllib.parse

import boto3
import librosa
import numpy as np
import soundfile as sf

s3 = boto3.client("s3")

ESC50_BUCKET = os.environ.get("ESC50_BUCKET", "")   # optional; empty → noise off
ESC50_PREFIX = os.environ.get("ESC50_PREFIX", "esc50/")
SNR_DB_LIST  = [float(x) for x in os.environ.get("AUG_SNR_DB_LIST", "0,5,10,15,20").split(",")]


def augment_window(signal: np.ndarray, sr: int) -> list[np.ndarray]:
    """Yield N augmented copies of a single window."""
    variants: list[np.ndarray] = []

    # Time-stretch
    for rate in (0.9, 1.1):
        variants.append(librosa.effects.time_stretch(signal, rate=rate)[: signal.size])

    # Pitch-shift (±2 semitones)
    for n_steps in (-2, 2):
        variants.append(librosa.effects.pitch_shift(signal, sr=sr, n_steps=n_steps))

    # Noise superimposition at multiple SNRs (if ESC-50 bucket configured)
    if ESC50_BUCKET:
        noise = _load_random_esc50_clip(sr, len_samples=signal.size)
        for snr_db in SNR_DB_LIST:
            variants.append(_add_noise_at_snr(signal, noise, snr_db))

    return variants


def _add_noise_at_snr(clean: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    sig_power   = np.mean(clean ** 2) + 1e-12
    noise_power = np.mean(noise ** 2) + 1e-12
    snr_linear  = 10 ** (snr_db / 10)
    scale       = np.sqrt(sig_power / (snr_linear * noise_power))
    return (clean + scale * noise).astype(np.float32)


def _load_random_esc50_clip(sr: int, len_samples: int) -> np.ndarray:
    """Pick one ESC-50 clip at random from S3 and return a len_samples chunk."""
    resp = s3.list_objects_v2(Bucket=ESC50_BUCKET, Prefix=ESC50_PREFIX,
                              MaxKeys=1000)
    contents = resp.get("Contents", [])
    if not contents:
        return np.zeros(len_samples, dtype=np.float32)
    key = random.choice(contents)["Key"]
    body = s3.get_object(Bucket=ESC50_BUCKET, Key=key)["Body"].read()
    y, _ = librosa.load(__file_buf(body), sr=sr, mono=True)
    if y.size < len_samples:
        y = np.tile(y, int(np.ceil(len_samples / max(1, y.size))))
    start = random.randint(0, max(0, y.size - len_samples))
    return y[start : start + len_samples].astype(np.float32)


def __file_buf(body: bytes):
    import io
    return io.BytesIO(body)
```

### 3.7 Monolith gotchas

- **`librosa.load(sr=None)` preserves native sample rate** — the Toyota team's reference code passes `sr=None`. That's fine for analysis notebooks but a bug in a pipeline where downstream models expect a fixed rate. ALWAYS pass `sr=SAMPLE_RATE` explicitly. The handler above does this.
- **Librosa's `librosa.effects.trim(top_db=20)` is aggressive** — engines idle quietly, and idle segments contain useful diagnostic signal. The default `top_db=40` in the env var is the safer floor for industrial-fault data. For speech / command detection use 20; for engine diagnosis use 40 or skip trimming entirely (`TRIM_TOP_DB=90`).
- **`librosa.power_to_db(ref=np.max)` matters** — without `ref=np.max` you get absolute dB values dependent on signal scale; with it you get a normalized `[-80, 0]` dB range that plays well with ImageNet-pretrained CNNs. The reference paper and the Toyota team's code both use `ref=np.max` — preserve it.
- **`n_fft=1024 @ 44.1 kHz = 23.2 ms window`; `n_fft=2048 = 46.4 ms`** — tradeoff is time vs frequency resolution. For transient-fault detection (knock, bearing hit) 1024 preserves time resolution; for steady-state fault signatures (gear whine) 2048 buys frequency resolution. Pick one and document in `docs/template_params.md`.
- **Wavelet CWT memory footprint** — `pywt.cwt(signal, scales=np.arange(1,64), wavelet='morl')` on a 5s @ 44.1 kHz signal = `(63, 220500)` float64 = ~110 MB per window. Cap `CWT_MAX_SCALE` at 64 and chunk long signals. Container-image Lambda at 10 GB is comfortable; a ZIP-layer 512 MB Lambda will OOM instantly. `# TODO(verify): exact memory for 60s signal at scale=128.`
- **Container image size budget** — `librosa + scipy + pywavelets + soundfile + numpy` totals ~400 MB installed. Adding `torch + torchaudio` pushes it to ~2.3 GB (AMD64) or ~1.9 GB (ARM64). Stay under the 10 GB Lambda container limit but **watch cold-start**: a 2 GB image typically cold-starts in 3-6s. Use Provisioned Concurrency for sync-invoke paths.
- **Silence-padded short signals** — `_segment` zero-pads signals shorter than one window. This is usually what you want (training on 1s snippets), but be aware that zero-padded regions look like silence to feature extractors — the model may learn "ends with silence = short sample" instead of the acoustic fault pattern. For training, either drop samples shorter than one window or document the zero-pad behaviour.
- **ESC-50 noise superimposition requires an external bucket** — the augmentation module reads ESC-50 clips from `ESC50_BUCKET`. The dataset itself (Piczak, 2015) is CC BY-NC 3.0 licensed — **do not redistribute in commercial deliverables**. Either require the customer to stage ESC-50 in their own account or swap to the Apache-licensed FSD50K subset. `# TODO(verify): customer-specific noise library license before shipping.`
- **`librosa.effects.time_stretch` changes length** — a 5s input at `rate=0.9` becomes 5.56s. The augment module truncates back to the original length via `[: signal.size]`, but this means a portion of the stretched signal is discarded. For training, feed the full stretched signal through the segmenter again rather than truncating.

---

## 4. Micro-Stack Variant

**Use when:** `StorageStack` owns raw-audio + curated-audio buckets (with `event_bridge_enabled=True`); `MLOpsAudioStack` owns the ingestion + preprocess Lambdas + DDB + DLQ + SageMaker Processing role; downstream `TrainingStack` + `VectorStoreStack` + similarity-search Lambdas consume `preprocessed_s3_prefix` + `audio_metadata_table_arn` via SSM.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** — `_LAMBDAS_ROOT` + `_DOCKER_ROOT` patterns. Critical for the Docker build context — `ecr_assets.DockerImageAsset` reads from a directory arg; a CWD-relative path breaks `cdk synth` from subdirectories.
2. **Never call `raw_bucket.grant_read(fn)` cross-stack.** Identity-side `s3:GetObject` on `f"{raw_bucket_arn}/*"` + `kms:Decrypt` on the bucket's CMK ARN (from SSM). Same for the curated bucket on the write side.
3. **Never target a cross-stack Lambda from a cross-stack EventBridge rule.** If the source bus is the account default bus, the rule lives in THIS stack and targets the local `IngestFn` — L2 `targets.LambdaFunction(local_fn)` is safe.
4. **SageMaker Processing role + `iam:PassRole`** — the Lambda that kicks off the Processing job must have `iam:PassRole` on the Processing role ARN with `iam:PassedToService=sagemaker.amazonaws.com`. Omit this and `CreateProcessingJob` fails with `AccessDeniedException`. Easy to miss because the error surfaces at runtime, not synth time.
5. **PermissionsBoundary** on every role (IngestFn role, PreprocessFn role, ProcessingJob role). Especially important for the Processing job role because it has broader S3 + ECR permissions than a typical Lambda role.

### 4.2 Dedicated `MLOpsAudioStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_dynamodb as ddb,
    aws_ecr_assets as ecr_assets,
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
_DOCKER_ROOT:  Path = Path(__file__).resolve().parents[3] / "docker"


class MLOpsAudioStack(cdk.Stack):
    """Audio ingestion + preprocess Lambdas + Processing role + DDB + DLQ.

    Cross-stack resources (raw bucket, curated bucket, their KMS CMKs) are
    imported by ARN via SSM. No cross-stack grant_* calls — identity-side
    only.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        raw_bucket_name_ssm: str,
        raw_bucket_arn_ssm: str,
        raw_bucket_kms_arn_ssm: str,
        curated_bucket_name_ssm: str,
        curated_bucket_arn_ssm: str,
        curated_bucket_kms_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        features: str = "mel_spectrogram,mfcc",
        sample_rate_hz: int = 44100,
        window_seconds: float = 5.0,
        overlap: float = 0.5,
        augmentation_enabled: bool = False,
        reserved_concurrency: int = 20,
        include_torchaudio: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-mlops-audio-{stage_name}", **kwargs)
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        raw_bucket_name   = ssm.StringParameter.value_for_string_parameter(self, raw_bucket_name_ssm)
        raw_bucket_arn    = ssm.StringParameter.value_for_string_parameter(self, raw_bucket_arn_ssm)
        raw_kms_arn       = ssm.StringParameter.value_for_string_parameter(self, raw_bucket_kms_arn_ssm)
        curated_name      = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_name_ssm)
        curated_arn       = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_arn_ssm)
        curated_kms_arn   = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_kms_arn_ssm)

        # Local CMK for this stack's DDB + SQS. Never share raw/curated CMKs.
        cmk = kms.Key(
            self, "MLOpsAudioKey",
            alias=f"alias/{{project_name}}-mlops-audio-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        # A) audio_metadata table
        audio_metadata = ddb.Table(
            self, "AudioMetadata",
            table_name=f"{{project_name}}-audio-metadata-{stage_name}",
            partition_key=ddb.Attribute(name="sample_id", type=ddb.AttributeType.STRING),
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
        audio_metadata.add_global_secondary_index(
            index_name="by-machine",
            partition_key=ddb.Attribute(name="machine_id",  type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="uploaded_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.KEYS_ONLY,
        )
        audio_metadata.add_global_secondary_index(
            index_name="by-status",
            partition_key=ddb.Attribute(name="status",      type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="uploaded_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.KEYS_ONLY,
        )

        # B) DLQ
        dlq = sqs.Queue(
            self, "PreprocessDlq",
            queue_name=f"{{project_name}}-audio-preprocess-dlq-{stage_name}",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            retention_period=Duration.days(14),
        )

        # C) Container image — librosa + pywt + scipy + soundfile [+ torchaudio].
        #    Path anchored to __file__, NOT CWD.
        preprocess_image = ecr_assets.DockerImageAsset(
            self, "AudioPreprocessImage",
            directory=str(_DOCKER_ROOT / "audio_preprocess"),
            platform=ecr_assets.Platform.LINUX_ARM64,
            build_args={"INCLUDE_TORCHAUDIO": "true" if include_torchaudio else "false"},
        )

        # D) PreprocessFn (container image)
        preprocess_log = logs.LogGroup(
            self, "PreprocessLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-audio-preprocess-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        preprocess_fn = _lambda.DockerImageFunction(
            self, "PreprocessFn",
            function_name=f"{{project_name}}-audio-preprocess-{stage_name}",
            code=_lambda.DockerImageCode.from_ecr(
                repository=preprocess_image.repository,
                tag_or_digest=preprocess_image.image_tag,
            ),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=10240,
            timeout=Duration.minutes(15),
            log_group=preprocess_log,
            tracing=_lambda.Tracing.ACTIVE,
            dead_letter_queue_enabled=True,
            dead_letter_queue=dlq,
            reserved_concurrent_executions=reserved_concurrency,
            environment={
                "RAW_BUCKET":             raw_bucket_name,
                "CURATED_BUCKET":         curated_name,
                "AUDIO_METADATA_TABLE":   audio_metadata.table_name,
                "FEATURES":               features,
                "PREPROCESS_SAMPLE_RATE": str(sample_rate_hz),
                "PREPROCESS_WINDOW_SECONDS": str(window_seconds),
                "PREPROCESS_OVERLAP":     str(overlap),
                "N_FFT":                  "1024",
                "HOP_LENGTH":             "512",
                "N_MELS":                 "64",
                "N_MFCC":                 "40",
                "TRIM_TOP_DB":            "40",
                "AUGMENTATION_ENABLED":   "true" if augmentation_enabled else "false",
            },
        )

        # Identity-side grants for PreprocessFn
        preprocess_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[f"{raw_bucket_arn}/*"],
        ))
        preprocess_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:PutObjectTagging"],
            resources=[f"{curated_arn}/*"],
        ))
        preprocess_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[raw_kms_arn],
        ))
        preprocess_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
            resources=[curated_kms_arn],
        ))
        audio_metadata.grant_read_write_data(preprocess_fn)
        iam.PermissionsBoundary.of(preprocess_fn.role).apply(permission_boundary)

        # E) IngestFn (ZIP)
        ingest_fn = _lambda.Function(
            self, "IngestFn",
            function_name=f"{{project_name}}-audio-ingest-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "audio_ingest")),
            memory_size=512,
            timeout=Duration.minutes(1),
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "AUDIO_METADATA_TABLE": audio_metadata.table_name,
                "PREPROCESS_FN_NAME":   preprocess_fn.function_name,
                "PROCESSING_MODE":      "lambda",
                "MAX_DURATION_SECONDS": "600",
                "ALLOWED_EXTENSIONS":   ".wav,.mp3,.m4a,.flac",
            },
        )
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:HeadObject", "s3:GetObject"],
            resources=[f"{raw_bucket_arn}/*"],
        ))
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[raw_kms_arn],
        ))
        audio_metadata.grant_read_write_data(ingest_fn)
        preprocess_fn.grant_invoke(ingest_fn)
        iam.PermissionsBoundary.of(ingest_fn.role).apply(permission_boundary)

        # F) SageMaker Processing role — for bulk backfill path.
        #    Separate from any Lambda role. Trust policy = sagemaker.amazonaws.com.
        processing_role = iam.Role(
            self, "AudioProcessingRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            permissions_boundary=permission_boundary,
            role_name=f"{{project_name}}-audio-processing-{stage_name}",
        )
        processing_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"],
            resources=[raw_bucket_arn, f"{raw_bucket_arn}/*"],
        ))
        processing_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:ListBucket"],
            resources=[curated_arn, f"{curated_arn}/*"],
        ))
        processing_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"],
            resources=[raw_kms_arn, curated_kms_arn],
        ))
        processing_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
                     "ecr:GetAuthorizationToken"],
            resources=["*"],
        ))
        processing_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogStream", "logs:PutLogEvents",
                     "logs:CreateLogGroup"],
            resources=[f"arn:aws:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:log-group:/aws/sagemaker/ProcessingJobs:*"],
        ))

        # Allow IngestFn to start Processing jobs (hybrid/sagemaker modes).
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:CreateProcessingJob",
                     "sagemaker:DescribeProcessingJob",
                     "sagemaker:StopProcessingJob"],
            resources=[
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:processing-job/*"
            ],
        ))
        ingest_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[processing_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))

        # G) EventBridge rule: raw-audio ObjectCreated → IngestFn.
        events.Rule(
            self, "AudioUploadedRule",
            rule_name=f"{{project_name}}-audio-uploaded-{stage_name}",
            event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [raw_bucket_name]},
                    "object": {"key": [
                        {"suffix": ".wav"}, {"suffix": ".mp3"},
                        {"suffix": ".m4a"}, {"suffix": ".flac"},
                    ]},
                },
            ),
            targets=[targets.LambdaFunction(ingest_fn)],
        )

        # H) Publish SSM params for downstream consumers.
        ssm.StringParameter(
            self, "AudioMetadataTableArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/mlops-audio/audio_metadata_table_arn",
            string_value=audio_metadata.table_arn,
        )
        ssm.StringParameter(
            self, "AudioMetadataTableNameParam",
            parameter_name=f"/{{project_name}}/{stage_name}/mlops-audio/audio_metadata_table_name",
            string_value=audio_metadata.table_name,
        )
        ssm.StringParameter(
            self, "PreprocessImageUriParam",
            parameter_name=f"/{{project_name}}/{stage_name}/mlops-audio/preprocess_image_uri",
            string_value=preprocess_image.image_uri,
        )
        ssm.StringParameter(
            self, "ProcessingRoleArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/mlops-audio/processing_role_arn",
            string_value=processing_role.role_arn,
        )

        self.preprocess_fn   = preprocess_fn
        self.ingest_fn       = ingest_fn
        self.audio_metadata  = audio_metadata
        self.dlq             = dlq
        self.processing_role = processing_role
        self.cmk             = cmk

        CfnOutput(self, "PreprocessFnArn",  value=preprocess_fn.function_arn)
        CfnOutput(self, "AudioMetadataTbl", value=audio_metadata.table_name)
        CfnOutput(self, "ProcessingRole",   value=processing_role.role_arn)
```

### 4.3 Micro-stack gotchas

- **`ecr_assets.DockerImageAsset` CWD trap** — `DockerImageAsset(directory=...)` must be an absolute path built from `Path(__file__)`. A relative path like `"../docker/audio_preprocess"` works locally but breaks CI where CDK runs from a different CWD. Always use `_DOCKER_ROOT`.
- **ARM64 container + SageMaker Processing instance type must match** — if the Dockerfile is `linux/arm64` (as above), the Processing job must request a Graviton instance (`ml.c6g.xlarge`, `ml.m6g.xlarge`, etc.). Mixing x86_64 image + arm64 instance or vice versa silently fails at job start with an opaque `InternalServerError`.
- **`iam:PassedToService=sagemaker.amazonaws.com` on `iam:PassRole`** — REQUIRED on the IngestFn's role when `PROCESSING_MODE` is ever `sagemaker_processing` or `hybrid`. Without it, `CreateProcessingJob` fails with `AccessDenied`; the error is runtime-only, not caught at synth.
- **Large asset uploads at `cdk deploy`** — a 2 GB container image takes minutes to push to ECR on first deploy. Consider running `cdk deploy` with a cached Docker buildx builder and `docker layer caching` in CI. `# TODO(verify): exact first-deploy time in customer's CI runner with their bandwidth.`
- **SSM parameter string length** — `preprocess_image_uri` is short (~100 chars) but `feature_keys` across many samples could exceed 4 KB. Do NOT marshal collections into a single SSM param; keep per-feature lists in DDB rows.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / single mic / small corpus | §3 Monolith + `FEATURES=mel_spectrogram` + `PROCESSING_MODE=lambda` |
| Large backfill (1000s of historical recordings) | `PROCESSING_MODE=sagemaker_processing` — same container image; IngestFn calls `CreateProcessingJob`; `# TODO(verify): Processing-job-per-file cost vs batched Processing job with S3 manifest input` |
| 65-dim fusion feature (MFCC + PCP + STE, per hybrid CNN+LSTM paper) | `FEATURES=mfcc,pcp,short_term_energy` + downstream concat step |
| Transient fault focus (knock, bearing hit) | `FEATURES=wavelet_cwt,mel_spectrogram` — CWT catches impulsive events; cap `CWT_MAX_SCALE=64` |
| AST / Wav2Vec2 downstream | `PREPROCESS_SAMPLE_RATE=16000` + `FEATURES=mel_spectrogram` + `N_MELS=128` + `PREPROCESS_WINDOW_SECONDS=10` — matches AST input contract |
| End-to-end 1D CNN on raw waveform | `FEATURES=raw_waveform` only; skip spectral extraction entirely |
| Training set expansion | `AUGMENTATION_ENABLED=true` + stage ESC-50 in customer's `ESC50_BUCKET`; expect 8-10× storage growth |
| Multi-machine / multi-tenant | Partition curated-bucket keys by `{tenant_id}/{machine_id}/{sample_id}/` and grant row-level access via S3 prefix policies |
| Real-time streaming (Kinesis Video / WebRTC) | Replace S3 upload trigger with KVS → KVS fragment consumer Lambda; same preprocess.py script; `# TODO(verify): KVS fragment-to-numpy conversion via `aws-kvs-producer` or `amazon-kinesis-video-streams-parser-library`` |
| On-device edge preprocessing | Run the same Dockerfile on AWS IoT Greengrass v2 as a component; identical feature artifacts sync up to S3 curated bucket |
| Migrating off Lookout for Equipment (EOL 2026-10-07) | Replicate L4E's `inference` + `training-data-scheme` S3 layouts in the curated bucket; Lookout's numeric-sensor features map cleanly to `FEATURES=short_term_energy`; acoustic features are NEW capability L4E never had |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_MLOPS_AUDIO_PIPELINE.py`. Offline; no AWS calls; no real Docker build (use `cdk.DockerImage.from_registry` fallback pattern or stub via `CDK_DOCKER`).

```python
"""SOP verification — MLOpsAudioStack synthesizes with:
- preprocess Lambda (DockerImageFunction) with correct env + policies
- ingest Lambda (ZIP) with sagemaker:CreateProcessingJob + iam:PassRole
- audio_metadata DDB table with 2 GSIs
- DLQ
- Processing role with ECR + S3 + KMS + CW Logs
- EventBridge rule on raw-audio ObjectCreated
- 4 SSM params published
"""
import os

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-west-2")


def test_mlops_audio_stack_synthesizes():
    # Skip real Docker build — uses BUNDLING_STACKS exclusion.
    app = cdk.App(context={
        "aws:cdk:bundling-stacks": [],
    })
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.mlops_audio_stack import MLOpsAudioStack
    stack = MLOpsAudioStack(
        app, stage_name="dev",
        raw_bucket_name_ssm="/test/storage/raw_audio_name",
        raw_bucket_arn_ssm="/test/storage/raw_audio_arn",
        raw_bucket_kms_arn_ssm="/test/storage/raw_audio_kms_arn",
        curated_bucket_name_ssm="/test/storage/curated_audio_name",
        curated_bucket_arn_ssm="/test/storage/curated_audio_arn",
        curated_bucket_kms_arn_ssm="/test/storage/curated_audio_kms_arn",
        permission_boundary=boundary,
        features="mel_spectrogram,mfcc",
        sample_rate_hz=44100,
        window_seconds=5.0,
        overlap=0.5,
        augmentation_enabled=False,
        reserved_concurrency=20,
        include_torchaudio=False,
        env=env,
    )
    t = Template.from_stack(stack)

    # One container-image Lambda + one ZIP Lambda
    t.resource_count_is("AWS::Lambda::Function", 2)
    t.resource_count_is("AWS::DynamoDB::Table",  1)
    t.resource_count_is("AWS::SQS::Queue",       1)
    t.resource_count_is("AWS::Events::Rule",     1)
    t.resource_count_is("AWS::KMS::Key",         1)

    # Preprocess Lambda env
    t.has_resource_properties("AWS::Lambda::Function", Match.object_like({
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "FEATURES":               "mel_spectrogram,mfcc",
                "PREPROCESS_SAMPLE_RATE": "44100",
                "N_FFT":                  "1024",
                "N_MELS":                 "64",
                "N_MFCC":                 "40",
                "AUGMENTATION_ENABLED":   "false",
            }),
        }),
        "ReservedConcurrentExecutions": 20,
    }))

    # EventBridge rule filters on Object Created
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "EventPattern": Match.object_like({
            "source":      ["aws.s3"],
            "detail-type": ["Object Created"],
        }),
    }))

    # Processing role exists
    t.has_resource_properties("AWS::IAM::Role", Match.object_like({
        "AssumeRolePolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({
                    "Principal": {"Service": "sagemaker.amazonaws.com"},
                }),
            ]),
        }),
    }))

    # 4 SSM params published
    t.resource_count_is("AWS::SSM::Parameter", 4)
```

---

## 7. References

- `docs/template_params.md` — `FEATURES` (`mel_spectrogram` | `mfcc` | `pcp` | `short_term_energy` | `wavelet_cwt` | `raw_waveform`, comma-separable), `PREPROCESS_SAMPLE_RATE`, `PREPROCESS_WINDOW_SECONDS`, `PREPROCESS_OVERLAP`, `N_FFT`, `HOP_LENGTH`, `N_MELS`, `N_MFCC`, `TRIM_TOP_DB`, `CWT_MAX_SCALE`, `CWT_WAVELET`, `AUGMENTATION_ENABLED`, `ESC50_BUCKET`, `PROCESSING_MODE` (`lambda` | `sagemaker_processing` | `hybrid`), `AUDIO_PREPROCESS_RESERVED_CONCURRENCY`, `INCLUDE_TORCHAUDIO`
- `docs/Feature_Roadmap.md` — feature IDs `AP-10` (raw-audio bucket + EventBridge wiring), `AP-11` (ingest Lambda + validation), `AP-12` (preprocess container image), `AP-13` (feature extraction — mel/MFCC/PCP/STE/CWT toggles), `AP-14` (augmentation — SpecAugment + ESC-50 noise + time-stretch + pitch-shift), `AP-15` (SageMaker Processing branch), `AP-16` (audio_metadata table + status lifecycle), `AP-17` (DLQ + reprocessor)
- Library docs:
  - [librosa — feature module (melspectrogram, mfcc, chroma_cqt, util.frame)](https://librosa.org/doc/latest/feature.html)
  - [librosa.effects (trim, time_stretch, pitch_shift)](https://librosa.org/doc/latest/effects.html)
  - [PyWavelets — Continuous Wavelet Transform](https://pywavelets.readthedocs.io/en/latest/ref/cwt.html)
  - [torchaudio — transforms + models](https://pytorch.org/audio/stable/index.html)
  - [scipy.signal (reference DSP primitives)](https://docs.scipy.org/doc/scipy/reference/signal.html)
  - [soundfile — libsndfile Python bindings](https://python-soundfile.readthedocs.io/)
- Papers (verified — sourced from the Toyota team's reference library + NBS IIoT white paper):
  - [Müller et al. 2020 — Acoustic Anomaly Detection for Machine Sounds based on Image Transfer Learning (arXiv 2006.03429)](https://arxiv.org/pdf/2006.03429)
  - [Dohi et al. 2024 — DCASE 2024 Task 2 baseline: Mobile-FaceNet + Gamma Distribution (arXiv 2403.00379)](https://arxiv.org/pdf/2403.00379)
  - [GeCo — Generative-Contrastive Learning for Anomalous Sound Detection (arXiv 2305.12111)](https://arxiv.org/pdf/2305.12111)
  - Nature Scientific Reports 2021 — "Hybrid neural network based on novel audio feature for vehicle type identification" (MFCC 40 + PCP 24 + STE 1 → 65-dim fusion feature for CNN+LSTM)
- Datasets:
  - [MIMII baseline (Hitachi) — 4 machine types × 7 SNR levels](https://github.com/MIMII-hitachi/mimii_baseline/)
  - [DCASE 2020 Task 2 — 6 machine types](https://github.com/AlexandrineRibeiro/DCASE-2020-Task-2)
- AWS:
  - [SageMaker Processing — bring-your-own-container](https://docs.aws.amazon.com/sagemaker/latest/dg/build-your-own-processing-container.html)
  - [Lambda container image packaging](https://docs.aws.amazon.com/lambda/latest/dg/images-create.html)
  - [Fine-tune and deploy Wav2Vec2 on SageMaker (HuggingFace blog)](https://aws.amazon.com/blogs/machine-learning/fine-tune-and-deploy-a-wav2vec2-model-for-speech-recognition-with-hugging-face-and-amazon-sagemaker/)
  - [Amazon Lookout for Equipment (EOL 2026-10-07) — migration context](https://aws.amazon.com/lookout-for-equipment/)
  - [AWS IoT SiteWise anomaly detection — alternative tabular-sensor path](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/anomaly-detection.html)
- HuggingFace audio models (for downstream embedding):
  - [MIT/ast-finetuned-audioset-10-10-0.4593 — AST model card](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593)
  - [AST docs (transformers)](https://huggingface.co/docs/transformers/en/model_doc/audio-spectrogram-transformer)
  - [Wav2Vec2 docs (transformers)](https://huggingface.co/docs/transformers/en/model_doc/wav2vec2)
- Related SOPs:
  - `PATTERN_AUDIO_SIMILARITY_SEARCH` — consumes this pipeline's curated features for embedding + S3 Vectors similarity search
  - `MLOPS_SAGEMAKER_TRAINING` — consumes curated features for classifier fine-tuning
  - `MLOPS_SAGEMAKER_SERVING` — real-time inference on mel-spec inputs; reuses the SAME preprocess.py in the inference container
  - `MLOPS_BATCH_TRANSFORM` — batch scoring over curated features
  - `DATA_S3_VECTORS` — downstream vector store for similarity search
  - `EVENT_DRIVEN_PATTERNS` — S3 → EventBridge → Lambda canonical wiring; DLQ reprocessor (§6) for failed-sample redrive
  - `LAYER_DATA` — raw + curated bucket defaults, `event_bridge_enabled=True`, KMS
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers, PermissionsBoundary, `_LAMBDAS_ROOT` pattern
  - `LAYER_SECURITY` — KMS CMK per stack, permission boundary
  - `LAYER_OBSERVABILITY` — CloudWatch metrics for preprocess throughput / failure rate / avg segmentation count

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — audio upload → preprocess → feature-extract pipeline for ML classifier training/inference. Plug-points for compute (Lambda container image / SageMaker Processing / hybrid), features (mel-spec / MFCC / PCP / STE / wavelet CWT / raw waveform, comma-separable toggles), and augmentation (SpecAugment + ESC-50 noise superimposition at 0/5/10/15/20 dB SNR + time-stretch + pitch-shift). Canonical `audio_metadata` DDB table with status lifecycle (uploaded → preprocessing → feature_extracted / failed) and by-machine + by-status GSIs. Container image shared between Lambda and SageMaker Processing via the same Dockerfile (librosa 0.10 + pywt 1.6 + scipy 1.13 + soundfile 0.12, optional torchaudio 2.3). 50%-overlap 5s-window default; 44.1 kHz default sample rate (Toyota team verified); `ref=np.max` for power-to-dB normalization. Reserved-concurrency cap on preprocess. Worked example uses `mel_spectrogram+mfcc` + lambda mode + no augmentation. Grounded in Toyota Car Sounds Team preprocessing.py, the Nature Scientific Reports 2021 hybrid CNN+LSTM vehicle-ID paper, DCASE 2024 Task 2 baseline, and the arXiv 2006.03429 / 2305.12111 anomaly-detection references. Created to fill gap surfaced by the Acoustic Fault Diagnostic Agent kit design (no preceding SOP covered audio-specific ingestion + DSP feature extraction). |
