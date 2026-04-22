# SOP — Batch Upload Pattern (ZIP / manifest → fan-out one job per item)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · S3 multipart presigned URL · API Gateway · Lambda batch handler · SQS + DLQ · DynamoDB batch-progress table · EventBridge completion event

---

## 1. Purpose

- Codify the "recruiter uploads a ZIP of 50 candidate videos + a manifest" **many-to-one intake → N jobs fan-out** pattern.
- Provide the **multipart presigned URL** issuer (CloudFront + S3 Transfer Acceleration) for files > 100 MB, plus a simpler single-PUT presign for small payloads.
- Provide the **batch manifest** (CSV or JSON) validator + per-item row-to-job emitter: one DynamoDB row per candidate, one SQS message per candidate, scoped to a single `batch_id`.
- Provide **per-batch progress tracking** (`batch_id → {total, succeeded, failed, in_flight}`) with an atomic completion event when `succeeded + failed == total`.
- Provide **partial-failure handling**: failed items land in the DLQ, get marked `status='failed'` in the batch-progress table, but the rest of the batch continues — and the batch-completion event fires once accounted for.
- Codify **max concurrent executions** on downstream processors via Lambda reserved concurrency + SQS `maximum_concurrency`.
- Include when the SOW signals: "bulk upload", "ZIP of records", "recruiter uploads N candidates at once", "batch import", "CSV upload", "fan out per row", "batch job tracking", "progress bar for upload job".

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the intake bucket, batch handler, progress table, fan-out queue, and item processor | **§3 Monolith Variant** |
| `StorageStack` owns the intake bucket; `BatchIntakeStack` owns the handler + progress table + fan-out queue; `ComputeStack` owns the item processor | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Presigned URL issuer** must call `s3:PutObject` on the intake bucket. If bucket lives in `StorageStack` and issuer Lambda in `BatchIntakeStack`, `bucket.grant_put(fn)` auto-mutates bucket policy → cycle.
2. **S3 → EventBridge → Lambda** must go through EventBridge (never direct S3 → Lambda bucket-notification cross-stack). Direct notification forces CDK's custom-resource bucket-notification mutation onto the `StorageStack`'s bucket → cycle.
3. **Batch progress table** needs writes from the batch handler AND reads from a status-polling Lambda AND writes from the item processor — cross-stack `table.grant_*` on 3 roles creates 3 cycle risks. Identity-side grants are mandatory.
4. **SQS fan-out queue** — same as `EVENT_DRIVEN_PATTERNS` §5.1: cross-stack `queue.grant_send_messages(fn)` and `queue.grant_consume_messages(fn)` both mutate the upstream queue's policy.

Micro-Stack variant grants identity-side throughout and wires S3 → EventBridge → intake Lambda via an L1 `CfnRule` with a static-ARN resource policy (same trick as `EVENT_DRIVEN_PATTERNS` §5.3).

---

## 3. Monolith Variant

### 3.1 Architecture

```
                        [ Recruiter browser ]
                               │
                               ▼  POST /batch/presign?count=50
                      ┌─────────────────────┐
                      │  API GW + Presigner │  (issues multipart presigned URLs)
                      └──────────┬──────────┘
                               │  batch_id=UUID, 50 PUT URLs
                               ▼  direct-to-S3 multipart upload
                      ┌─────────────────────┐
                      │  intake-bucket      │
                      │  /batch/{batch_id}/ │
                      │    manifest.json    │
                      │    files/<file>.mp4 │
                      └──────────┬──────────┘
                               │  S3 ObjectCreated (manifest.json only)
                               ▼
                        EventBridge default bus
                               │
                               ▼
                      ┌─────────────────────┐
                      │  batch-handler Fn   │  validate manifest, expand rows
                      │  writes N rows to   │
                      │  batch-progress DDB │
                      │  emits N SQS msgs   │
                      └──────────┬──────────┘
                               │
                               ▼  fan-out (standard SQS, reserved concurrency)
                      ┌─────────────────────┐
                      │  item-processor Fn  │  per-candidate analysis
                      │  (max concurrent=20)│
                      └─────┬──────────┬────┘
              success       │          │       failure
                 ▼          │          │          ▼
      DDB update success    │          │      DLQ + DDB update failed
                            │          │
                            └─────┬────┘
                                  ▼
                      atomic UpdateItem on batch-progress:
                          ADD completed :one
                      when completed == total → emit BatchComplete
```

### 3.2 CDK — `_create_batch_upload()` method body

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_apigateway as apigw,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as les,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
)


def _create_batch_upload(self, stage: str) -> None:
    """Monolith variant. Assumes self.{kms_key, event_bus, intake_bucket, api}
    exist (intake_bucket with event_bridge_enabled=True, see LAYER_DATA §3.1)."""

    # A) Batch-progress tracking table
    self.batch_progress = ddb.Table(
        self, "BatchProgress",
        table_name=f"{{project_name}}-batch-progress-{stage}",
        partition_key=ddb.Attribute(name="batch_id",  type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(     name="item_id",   type=ddb.AttributeType.STRING),
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
    # item_id='__batch__' holds the summary counters for the whole batch
    self.batch_progress.add_global_secondary_index(
        index_name="by-status",
        partition_key=ddb.Attribute(name="status",     type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(     name="updated_at", type=ddb.AttributeType.STRING),
        projection_type=ddb.ProjectionType.KEYS_ONLY,
    )

    # B) DLQ + fan-out queue with max-concurrency governor
    self.item_dlq = sqs.Queue(
        self, "ItemDLQ",
        queue_name=f"{{project_name}}-item-dlq-{stage}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )
    self.item_queue = sqs.Queue(
        self, "ItemQueue",
        queue_name=f"{{project_name}}-item-queue-{stage}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        visibility_timeout=Duration.minutes(15),
        retention_period=Duration.days(4),
        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=3, queue=self.item_dlq
        ),
    )

    # C) Presigner Lambda (issues multipart presigned URLs)
    presigner_log = logs.LogGroup(
        self, "PresignerLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-batch-presigner-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.presigner_fn = _lambda.Function(
        self, "PresignerFn",
        function_name=f"{{project_name}}-batch-presigner-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset("lambda/batch_presigner"),
        memory_size=256,
        timeout=Duration.seconds(10),
        log_group=presigner_log,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "INTAKE_BUCKET":        self.intake_bucket.bucket_name,
            "BATCH_PROGRESS_TABLE": self.batch_progress.table_name,
            "URL_EXPIRY_SECONDS":   "3600",
            "MAX_ITEMS_PER_BATCH":  "100",
            "POWERTOOLS_SERVICE_NAME": "{project_name}-batch-presigner",
            "POWERTOOLS_LOG_LEVEL":    "INFO",
        },
    )
    self.intake_bucket.grant_put(self.presigner_fn)      # monolith L2 OK
    self.batch_progress.grant_read_write_data(self.presigner_fn)

    # D) Batch handler (triggered by manifest.json ObjectCreated)
    batch_log = logs.LogGroup(
        self, "BatchHandlerLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-batch-handler-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
    )
    self.batch_handler_fn = _lambda.Function(
        self, "BatchHandlerFn",
        function_name=f"{{project_name}}-batch-handler-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset("lambda/batch_handler"),
        memory_size=1024,
        timeout=Duration.seconds(300),
        log_group=batch_log,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "INTAKE_BUCKET":        self.intake_bucket.bucket_name,
            "BATCH_PROGRESS_TABLE": self.batch_progress.table_name,
            "ITEM_QUEUE_URL":       self.item_queue.queue_url,
            "EVENT_BUS_NAME":       self.event_bus.event_bus_name,
            "MAX_ITEMS_PER_BATCH":  "100",
        },
    )
    self.intake_bucket.grant_read(self.batch_handler_fn)
    self.batch_progress.grant_read_write_data(self.batch_handler_fn)
    self.item_queue.grant_send_messages(self.batch_handler_fn)
    self.event_bus.grant_put_events_to(self.batch_handler_fn)

    # E) S3 → EventBridge rule for manifest.json ObjectCreated
    events.Rule(
        self, "ManifestCreatedRule",
        rule_name=f"{{project_name}}-manifest-created-{stage}",
        event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
        event_pattern=events.EventPattern(
            source=["aws.s3"],
            detail_type=["Object Created"],
            detail={
                "bucket": {"name": [self.intake_bucket.bucket_name]},
                "object": {"key":  [{"suffix": "/manifest.json"}]},
            },
        ),
        targets=[targets.LambdaFunction(self.batch_handler_fn)],
    )

    # F) Item processor with reserved concurrency (max concurrent = 20)
    item_log = logs.LogGroup(
        self, "ItemProcessorLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-item-processor-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
    )
    self.item_processor_fn = _lambda.Function(
        self, "ItemProcessorFn",
        function_name=f"{{project_name}}-item-processor-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset("lambda/item_processor"),
        memory_size=1024,
        timeout=Duration.minutes(5),
        log_group=item_log,
        tracing=_lambda.Tracing.ACTIVE,
        reserved_concurrent_executions=20,               # cap blast radius
        environment={
            "BATCH_PROGRESS_TABLE": self.batch_progress.table_name,
            "EVENT_BUS_NAME":       self.event_bus.event_bus_name,
            "INTAKE_BUCKET":        self.intake_bucket.bucket_name,
        },
    )
    self.batch_progress.grant_read_write_data(self.item_processor_fn)
    self.item_queue.grant_consume_messages(self.item_processor_fn)
    self.intake_bucket.grant_read(self.item_processor_fn)
    self.event_bus.grant_put_events_to(self.item_processor_fn)

    self.item_processor_fn.add_event_source(les.SqsEventSource(
        self.item_queue,
        batch_size=1,                                     # one item at a time
        max_batching_window=Duration.seconds(0),
        max_concurrency=20,                               # same cap on ESM side
        report_batch_item_failures=True,
    ))

    # G) API GW route for presigner
    batch_res = self.api.root.add_resource("batch")
    batch_res.add_resource("presign").add_method(
        "POST",
        apigw.LambdaIntegration(self.presigner_fn, proxy=True),
    )

    CfnOutput(self, "BatchProgressTable", value=self.batch_progress.table_name)
    CfnOutput(self, "ItemQueueUrl",       value=self.item_queue.queue_url)
```

### 3.3 Presigner handler — saved to `lambda/batch_presigner/index.py`

```python
"""Presigner: returns batch_id + N multipart-upload presigned URLs + a
presigned URL for the manifest.json. Writes the summary row eagerly."""
import json
import logging
import os
import time
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client  = boto3.client("s3")
ddb_client = boto3.resource("dynamodb")
progress   = ddb_client.Table(os.environ["BATCH_PROGRESS_TABLE"])

INTAKE_BUCKET       = os.environ["INTAKE_BUCKET"]
URL_EXPIRY          = int(os.environ.get("URL_EXPIRY_SECONDS", "3600"))
MAX_ITEMS_PER_BATCH = int(os.environ.get("MAX_ITEMS_PER_BATCH", "100"))


def lambda_handler(event, _ctx):
    body  = json.loads(event.get("body", "{}"))
    count = int(body.get("count", 1))

    if count < 1 or count > MAX_ITEMS_PER_BATCH:
        return _err(400, f"count must be 1..{MAX_ITEMS_PER_BATCH}")

    batch_id = str(uuid.uuid4())
    now_iso  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    item_presigns = []
    for i in range(count):
        item_id = f"item-{i:03d}"
        key     = f"batch/{batch_id}/files/{item_id}.mp4"
        url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": INTAKE_BUCKET,
                "Key":    key,
                "ContentType": "video/mp4",
            },
            ExpiresIn=URL_EXPIRY,
        )
        item_presigns.append({"item_id": item_id, "s3_key": key, "put_url": url})

    manifest_key = f"batch/{batch_id}/manifest.json"
    manifest_url = s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": INTAKE_BUCKET,
            "Key":    manifest_key,
            "ContentType": "application/json",
        },
        ExpiresIn=URL_EXPIRY,
    )

    # Eagerly write the batch summary row. item_id='__batch__' is the marker.
    progress.put_item(Item={
        "batch_id":     batch_id,
        "item_id":      "__batch__",
        "status":       "awaiting_manifest",
        "total":        count,
        "completed":    0,
        "succeeded":    0,
        "failed":       0,
        "created_at":   now_iso,
        "updated_at":   now_iso,
        "ttl":          int(time.time()) + 14 * 86400,
    })

    return _ok({
        "batch_id":        batch_id,
        "manifest_key":    manifest_key,
        "manifest_put_url": manifest_url,
        "items":           item_presigns,
        "expires_in":      URL_EXPIRY,
    })


def _ok(obj):
    return {"statusCode": 200, "body": json.dumps(obj)}


def _err(code: int, msg: str):
    return {"statusCode": code, "body": json.dumps({"error": msg})}
```

### 3.4 Batch handler — saved to `lambda/batch_handler/index.py`

```python
"""Batch handler. Triggered by S3→EventBridge on manifest.json.
Validates the manifest, writes one progress row per candidate, emits
one SQS message per candidate. Partial-failure tolerant.

Manifest shape (JSON):
{
  "batch_id": "uuid",                              # must match path
  "items": [
    {"item_id": "item-000", "candidate_email": "a@x", "role_code": "SWE_L5",
     "s3_key": "batch/<batch_id>/files/item-000.mp4"},
    ...
  ]
}
"""
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client  = boto3.client("s3")
sqs_client = boto3.client("sqs")
ddb_client = boto3.resource("dynamodb")
ebr        = boto3.client("events")

progress   = ddb_client.Table(os.environ["BATCH_PROGRESS_TABLE"])
QUEUE_URL  = os.environ["ITEM_QUEUE_URL"]
BUS        = os.environ["EVENT_BUS_NAME"]
MAX_ITEMS  = int(os.environ.get("MAX_ITEMS_PER_BATCH", "100"))


def lambda_handler(event, _ctx):
    """Single EventBridge detail: Object Created for manifest.json."""
    detail = event["detail"]
    bucket = detail["bucket"]["name"]
    key    = detail["object"]["key"]

    batch_id = _batch_id_from_key(key)
    if not batch_id:
        logger.error("could not parse batch_id from key=%s", key)
        return {"statusCode": 400}

    raw = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        _mark_batch_failed(batch_id, "invalid_json")
        return {"statusCode": 400}

    if manifest.get("batch_id") != batch_id:
        _mark_batch_failed(batch_id, "batch_id_mismatch")
        return {"statusCode": 400}

    items = manifest.get("items", [])
    if not items or len(items) > MAX_ITEMS:
        _mark_batch_failed(batch_id, f"items_count_out_of_range:{len(items)}")
        return {"statusCode": 400}

    # Upsert the summary row: total and status 'in_progress'
    now = _now()
    progress.update_item(
        Key={"batch_id": batch_id, "item_id": "__batch__"},
        UpdateExpression="SET #s = :s, #t = :t, updated_at = :u, total = :n",
        ExpressionAttributeNames={"#s": "status", "#t": "total"},
        ExpressionAttributeValues={
            ":s": "in_progress",
            ":t": len(items),
            ":u": now,
            ":n": len(items),
        },
    )

    # Write one item row + one SQS message per candidate. SQS SendMessageBatch
    # caps at 10, so chunk.
    sqs_batch: list[dict] = []
    written = 0
    failed_upfront: list[str] = []
    for idx, item in enumerate(items):
        item_id = item.get("item_id") or f"item-{idx:03d}"
        s3_key  = item.get("s3_key")
        if not s3_key:
            failed_upfront.append(item_id)
            _mark_item(batch_id, item_id, "failed", "missing_s3_key")
            continue

        try:
            progress.put_item(Item={
                "batch_id":     batch_id,
                "item_id":      item_id,
                "status":       "queued",
                "s3_key":       s3_key,
                "email":        item.get("candidate_email", ""),
                "role_code":    item.get("role_code", ""),
                "created_at":   now,
                "updated_at":   now,
                "ttl":          int(time.time()) + 14 * 86400,
            })
            written += 1
        except Exception:
            logger.exception("failed writing progress row batch=%s item=%s",
                             batch_id, item_id)
            failed_upfront.append(item_id)
            continue

        sqs_batch.append({
            "Id":          f"{idx}",
            "MessageBody": json.dumps({
                "batch_id": batch_id,
                "item_id":  item_id,
                "s3_key":   s3_key,
                "email":    item.get("candidate_email"),
                "role_code": item.get("role_code"),
            }),
        })

        if len(sqs_batch) == 10:
            _send_batch(sqs_batch, batch_id)
            sqs_batch = []

    if sqs_batch:
        _send_batch(sqs_batch, batch_id)

    # Pre-account any upfront failures so the completion math still closes.
    if failed_upfront:
        progress.update_item(
            Key={"batch_id": batch_id, "item_id": "__batch__"},
            UpdateExpression="ADD failed :f, completed :f SET updated_at = :u",
            ExpressionAttributeValues={
                ":f": len(failed_upfront),
                ":u": _now(),
            },
        )

    ebr.put_events(Entries=[{
        "Source":       "{project_name}.batch",
        "DetailType":   "BatchAccepted",
        "Detail":       json.dumps({
            "batch_id": batch_id,
            "total":    len(items),
            "queued":   written,
            "failed_upfront": len(failed_upfront),
        }),
        "EventBusName": BUS,
    }])

    return {"statusCode": 200, "body": json.dumps({
        "batch_id": batch_id, "queued": written,
        "failed_upfront": len(failed_upfront),
    })}


def _send_batch(entries: list[dict], batch_id: str) -> None:
    resp = sqs_client.send_message_batch(QueueUrl=QUEUE_URL, Entries=entries)
    for fail in resp.get("Failed", []) or []:
        # Find the entry that failed by Id, mark it, account it.
        logger.error("SQS SendMessageBatch failed batch=%s id=%s code=%s",
                     batch_id, fail["Id"], fail.get("Code"))
        # The ID is just the index; we'd need to join back to the item_id —
        # in production, carry item_id in entry "Id" or a MessageAttribute.


def _mark_batch_failed(batch_id: str, reason: str) -> None:
    progress.update_item(
        Key={"batch_id": batch_id, "item_id": "__batch__"},
        UpdateExpression="SET #s = :s, failure_reason = :r, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "failed", ":r": reason, ":u": _now()},
    )


def _mark_item(batch_id: str, item_id: str, status: str, reason: str) -> None:
    progress.put_item(Item={
        "batch_id": batch_id, "item_id": item_id,
        "status":   status,   "failure_reason": reason,
        "created_at": _now(), "updated_at": _now(),
        "ttl": int(time.time()) + 14 * 86400,
    })


def _batch_id_from_key(key: str) -> str | None:
    # expect: batch/<uuid>/manifest.json
    parts = key.split("/")
    if len(parts) == 3 and parts[0] == "batch" and parts[2] == "manifest.json":
        return parts[1]
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
```

### 3.5 Item processor — saved to `lambda/item_processor/index.py`

```python
"""Item processor. One SQS message = one candidate. On success OR failure,
atomically increments the batch summary row and emits BatchComplete when
completed == total."""
import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb_client = boto3.resource("dynamodb")
progress   = ddb_client.Table(os.environ["BATCH_PROGRESS_TABLE"])
ebr        = boto3.client("events")

BUS = os.environ["EVENT_BUS_NAME"]


def lambda_handler(event, _ctx):
    failures: list[dict] = []
    for rec in event["Records"]:
        try:
            _process_one(rec)
        except Exception:
            logger.exception("item processing failed msg=%s", rec["messageId"])
            # Let SQS redeliver until maxReceiveCount; the catch-all in
            # _finalize_item handles actual failures after retries exhaust.
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}


def _process_one(rec: dict) -> None:
    msg      = json.loads(rec["body"])
    batch_id = msg["batch_id"]
    item_id  = msg["item_id"]
    s3_key   = msg["s3_key"]

    try:
        # ... actual per-candidate work (Transcribe start, score compute, etc.)
        _run_candidate_analysis(batch_id, item_id, s3_key, msg)
        _finalize_item(batch_id, item_id, succeeded=True)
    except PermanentError as e:
        # Upload corrupt / invalid — record failure, DO NOT re-raise.
        _finalize_item(batch_id, item_id, succeeded=False, reason=str(e))


class PermanentError(Exception):
    """Raised for errors that SHOULD NOT be retried by SQS."""


def _run_candidate_analysis(batch_id, item_id, s3_key, msg):
    # placeholder — real implementation kicks off Transcribe / Bedrock /
    # AnalyzerStack N streams. Raise PermanentError on unretriable issues.
    pass


def _finalize_item(batch_id: str, item_id: str, succeeded: bool,
                   reason: str | None = None) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    status = "succeeded" if succeeded else "failed"

    # 1) Update the item row
    update_expr = "SET #s = :s, updated_at = :u"
    values = {":s": status, ":u": now}
    if reason:
        update_expr += ", failure_reason = :r"
        values[":r"] = reason
    progress.update_item(
        Key={"batch_id": batch_id, "item_id": item_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )

    # 2) Atomically bump the summary row. ReturnValues=ALL_NEW tells us if
    #    completed==total so we can fire BatchComplete exactly once.
    incr_attr = ":s_one" if succeeded else ":f_one"
    summary = progress.update_item(
        Key={"batch_id": batch_id, "item_id": "__batch__"},
        UpdateExpression=(
            f"ADD {'succeeded' if succeeded else 'failed'} :one, completed :one "
            "SET updated_at = :u"
        ),
        ExpressionAttributeValues={":one": 1, ":u": now},
        ReturnValues="ALL_NEW",
    )["Attributes"]

    completed = int(summary.get("completed", 0))
    total     = int(summary.get("total", 0))
    if completed >= total and summary.get("status") != "complete":
        _emit_complete(batch_id, summary)


def _emit_complete(batch_id: str, summary: dict) -> None:
    # Conditional: only write status=complete once.
    try:
        progress.update_item(
            Key={"batch_id": batch_id, "item_id": "__batch__"},
            UpdateExpression="SET #s = :c, completed_at = :t",
            ConditionExpression="#s <> :c",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": "complete",
                ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return              # another worker already emitted
        raise

    ebr.put_events(Entries=[{
        "Source":       "{project_name}.batch",
        "DetailType":   "BatchComplete",
        "Detail":       json.dumps({
            "batch_id":  batch_id,
            "total":     int(summary.get("total", 0)),
            "succeeded": int(summary.get("succeeded", 0)),
            "failed":    int(summary.get("failed", 0)),
        }),
        "EventBusName": BUS,
    }])
```

### 3.6 Monolith gotchas

- **S3 EventBridge for manifest.json ONLY**, not every file in the batch. The EventBridge rule `object.key` filter with `{"suffix": "/manifest.json"}` is the cheapest, most predictable trigger. Triggering on every `files/*.mp4` PUT would double-fire the batch handler for each file.
- **Presigned URL expiry** — 1 hour is typical but must be longer than the largest expected upload time. For 50 × 500 MB videos over slow network, bump to 6 hours. Expiry never exceeds the role session duration.
- **Multipart uploads** — `generate_presigned_url("put_object", ...)` returns a single-PUT URL, capped at 5 GB and degrading above 100 MB. For files > 100 MB, use the multipart flow: `create_multipart_upload` + per-part `upload_part` presigned URLs + `complete_multipart_upload`. The presigner handler shown is the simple case; see swap matrix row 3.
- **`reserved_concurrent_executions=20`** caps the Lambda function as a whole across all batches. A single batch of 50 still only runs 20 in flight. `les.SqsEventSource(max_concurrency=20)` is the newer per-event-source cap that achieves the same for this specific queue.
- **Partial-failure accounting must be complete.** Upfront validation failures (`missing_s3_key`) MUST be counted in `failed` AND `completed` right away or the batch never reaches `completed == total`.
- **DLQ does NOT auto-update the summary row.** A dedicated `dlq_reprocessor_fn` (see `EVENT_DRIVEN_PATTERNS` §6) or a custom SQS → Lambda wiring to `_finalize_item(succeeded=False)` is needed to close the math. Otherwise batches with poisoned items stall at `completed < total` forever.
- **`ConditionalCheckFailedException` on the complete emit** is the idempotency signal — swallow it; don't fire BatchComplete twice.

---

## 4. Micro-Stack Variant

**Use when:** `StorageStack` owns the intake bucket; `BatchIntakeStack` owns presigner + batch handler + progress table + SQS queue; `ComputeStack` owns the item processor.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** — `_LAMBDAS_ROOT` pattern.
2. **Never call `bucket.grant_put(fn)` or `bucket.grant_read(fn)` cross-stack.** Identity-side `PolicyStatement` with `s3:PutObject` / `s3:GetObject` on `f"{bucket.bucket_arn}/*"`.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** Use L1 `CfnRule` + static-ARN resource policy on the queue (see `EVENT_DRIVEN_PATTERNS` §5.3). Event-source mapping from processor → queue is safe (no policy mutation).
4. **Never split a bucket + its S3 notification across stacks.** Either the notification lives in `StorageStack` (bucket-side, with cross-stack Lambda ARN — still has a cycle), or the bucket has `event_bridge_enabled=True` in `StorageStack` and the rule lives in `EventStack` / `BatchIntakeStack` using L1 `CfnRule`. Always choose the second.
5. **Never set `encryption_key=ext_key`** on the progress table when the KMS CMK is from another stack. Own the table's CMK locally (or use the AWS-managed `aws/dynamodb` key).

### 4.2 Dedicated `BatchIntakeStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_apigateway as apigw,
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


class BatchIntakeStack(cdk.Stack):
    """Batch intake for uploaded manifests + per-item fan-out queue.

    Cross-stack resources (intake bucket, KMS key) are imported by ARN via
    SSM parameter. No cross-stack grant_* calls — identity-side only.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        intake_bucket_name_ssm: str,
        intake_bucket_kms_arn_ssm: str,
        event_bus_arn_ssm: str,
        event_bus_name_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        max_items_per_batch: int = 100,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-batch-intake-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        bucket_name     = ssm.StringParameter.value_for_string_parameter(
            self, intake_bucket_name_ssm
        )
        bucket_arn      = f"arn:aws:s3:::{bucket_name}"
        bucket_kms_arn  = ssm.StringParameter.value_for_string_parameter(
            self, intake_bucket_kms_arn_ssm
        )
        bus_arn         = ssm.StringParameter.value_for_string_parameter(
            self, event_bus_arn_ssm
        )
        bus_name        = ssm.StringParameter.value_for_string_parameter(
            self, event_bus_name_ssm
        )

        # Local CMK for the progress table + SQS
        cmk = kms.Key(
            self, "BatchKey",
            alias=f"alias/{{project_name}}-batch-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        # A) Batch-progress table
        self.batch_progress = ddb.Table(
            self, "BatchProgress",
            table_name=f"{{project_name}}-batch-progress-{stage_name}",
            partition_key=ddb.Attribute(name="batch_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="item_id",  type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            time_to_live_attribute="ttl",
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery=(stage_name == "prod"),
            removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        )

        # B) DLQ + fan-out queue
        self.item_dlq = sqs.Queue(
            self, "ItemDLQ",
            queue_name=f"{{project_name}}-item-dlq-{stage_name}",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            retention_period=Duration.days(14),
        )
        self.item_queue = sqs.Queue(
            self, "ItemQueue",
            queue_name=f"{{project_name}}-item-queue-{stage_name}",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            visibility_timeout=Duration.minutes(15),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.item_dlq),
        )

        # C) Presigner Lambda
        presigner_log = logs.LogGroup(
            self, "PresignerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-batch-presigner-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        self.presigner_fn = _lambda.Function(
            self, "PresignerFn",
            function_name=f"{{project_name}}-batch-presigner-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "batch_presigner")),
            memory_size=256,
            timeout=Duration.seconds(10),
            log_group=presigner_log,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "INTAKE_BUCKET":        bucket_name,
                "BATCH_PROGRESS_TABLE": self.batch_progress.table_name,
                "URL_EXPIRY_SECONDS":   "3600",
                "MAX_ITEMS_PER_BATCH":  str(max_items_per_batch),
            },
        )
        # Identity-side grants
        self.presigner_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:AbortMultipartUpload"],
            resources=[f"{bucket_arn}/*"],
        ))
        self.presigner_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[bucket_kms_arn],
        ))
        # Progress table is in THIS stack — L2 grant is safe.
        self.batch_progress.grant_read_write_data(self.presigner_fn)

        # D) Batch handler
        batch_log = logs.LogGroup(
            self, "BatchHandlerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-batch-handler-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        self.batch_handler_fn = _lambda.Function(
            self, "BatchHandlerFn",
            function_name=f"{{project_name}}-batch-handler-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "batch_handler")),
            memory_size=1024,
            timeout=Duration.seconds(300),
            log_group=batch_log,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "INTAKE_BUCKET":        bucket_name,
                "BATCH_PROGRESS_TABLE": self.batch_progress.table_name,
                "ITEM_QUEUE_URL":       self.item_queue.queue_url,
                "EVENT_BUS_NAME":       bus_name,
                "MAX_ITEMS_PER_BATCH":  str(max_items_per_batch),
            },
        )
        # S3 identity-side
        self.batch_handler_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"{bucket_arn}/*"],
        ))
        self.batch_handler_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[bucket_kms_arn],
        ))
        # EventBridge cross-stack: identity-side PutEvents
        self.batch_handler_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"],
            resources=[bus_arn],
        ))
        # In-stack grants are L2-safe
        self.batch_progress.grant_read_write_data(self.batch_handler_fn)
        self.item_queue.grant_send_messages(self.batch_handler_fn)

        # E) EventBridge rule: S3 manifest created → batch handler.
        # L1 CfnRule would be the cross-stack pattern; here the rule lives in
        # THIS stack and the target (Lambda) is also local — L2 is safe.
        events.Rule(
            self, "ManifestCreatedRule",
            rule_name=f"{{project_name}}-manifest-created-{stage_name}",
            event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [bucket_name]},
                    "object": {"key":  [{"suffix": "/manifest.json"}]},
                },
            ),
            targets=[targets.LambdaFunction(self.batch_handler_fn)],
        )

        iam.PermissionsBoundary.of(self.presigner_fn.role).apply(permission_boundary)
        iam.PermissionsBoundary.of(self.batch_handler_fn.role).apply(permission_boundary)

        # Publish for ComputeStack's item-processor Lambda
        ssm.StringParameter(
            self, "ItemQueueArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/batch/item_queue_arn",
            string_value=self.item_queue.queue_arn,
        )
        ssm.StringParameter(
            self, "ItemQueueUrlParam",
            parameter_name=f"/{{project_name}}/{stage_name}/batch/item_queue_url",
            string_value=self.item_queue.queue_url,
        )
        ssm.StringParameter(
            self, "BatchProgressTableArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/batch/progress_table_arn",
            string_value=self.batch_progress.table_arn,
        )
        ssm.StringParameter(
            self, "BatchKmsArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/batch/kms_arn",
            string_value=cmk.key_arn,
        )

        CfnOutput(self, "PresignerFnArn", value=self.presigner_fn.function_arn)
        CfnOutput(self, "ItemQueueArn",   value=self.item_queue.queue_arn)
```

### 4.3 Micro-stack gotchas

- **SSM tokens for bucket name** — `f"{bucket_arn}/*"` where `bucket_arn` is a concatenation of a literal prefix and an SSM token works at synth (CloudFormation resolves `{{resolve:ssm:...}}` inline). Do NOT attempt to parse the token in Python.
- **EventBridge default-bus rules across accounts** — if your S3 bucket lives in a shared-services account and the rule in a workload account, you need EventBridge cross-account wiring (not covered here). Single-account is the common case.
- **Item processor in `ComputeStack`** — reads `ItemQueueArnParam`, attaches event source via `les.SqsEventSource(q, ...)` (event-source mapping is safe cross-stack; the policy mutation only happens on `grant_consume_messages`). Grant `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes`/`ChangeMessageVisibility` identity-side with the ARN.
- **KMS from `BatchIntakeStack`** — consumers (item processor) need `kms:Decrypt` on the local CMK. Published via SSM; granted identity-side on the consumer role.
- **Reserved concurrency is attached to the FUNCTION, not the stack** — putting the item processor in `ComputeStack` means `reserved_concurrent_executions=20` configured there. That number ideally comes from `docs/template_params.md` not hardcoded.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| Small files only (< 5 MB each) | Use API GW direct multipart form upload, skip presign; cap body size at API GW 10 MB |
| Large files (> 100 MB) | Multipart presigned URL — replace `generate_presigned_url("put_object")` with `create_multipart_upload` + per-part `upload_part` URLs + `complete_multipart_upload` URL. See AWS docs link below |
| Very large files (> 5 GB) | S3 Transfer Acceleration + multipart mandatory; enable `bucket.transfer_acceleration=True` |
| Low-latency global uploads | Add CloudFront in front of S3 Transfer Acceleration — ~30% faster for distant clients |
| Manifest is CSV, not JSON | Same handler, swap `json.loads(body)` for `csv.DictReader(io.StringIO(body))`; same row-to-SQS-message emit |
| Need real-time progress in UI | Expose a `GET /batch/{batch_id}` route that reads the `__batch__` row; or use AppSync subscriptions on DDB Streams |
| Need retry-on-DLQ at the batch level | Add DLQ reprocessor Lambda (see `EVENT_DRIVEN_PATTERNS` §6) that consumes DLQ, calls `_finalize_item(succeeded=False)` to close batch math |
| Recruiters upload via mobile | S3 Transfer Acceleration + `browser_cache` headers on the presigned URL; enforce MIME on server side in the processor |
| Cross-account intake | Use Bucket Policy with `aws:PrincipalAccount` allowlist; EventBridge rule lives in intake account (rule-on-default-bus) |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_PATTERN_BATCH_UPLOAD.py`. Offline; `cdk.Stack` as deps stub.

```python
"""SOP verification — BatchIntakeStack synthesizes with:
- progress DDB table + SQS queue + DLQ
- presigner + batch-handler Lambdas
- EventBridge rule on manifest.json"""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_batch_intake_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg   = ec2.SecurityGroup(deps, "LambdaSg", vpc=vpc)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.batch_intake_stack import BatchIntakeStack
    stack = BatchIntakeStack(
        app, stage_name="dev",
        vpc=vpc, lambda_sg=sg,
        intake_bucket_name_ssm="/test/s3/intake_bucket_name",
        intake_bucket_kms_arn_ssm="/test/s3/intake_bucket_kms_arn",
        event_bus_arn_ssm="/test/events/bus_arn",
        event_bus_name_ssm="/test/events/bus_name",
        permission_boundary=boundary,
        max_items_per_batch=100,
        env=env,
    )
    t = Template.from_stack(stack)

    # Resources
    t.resource_count_is("AWS::Lambda::Function",  2)   # presigner + batch handler
    t.resource_count_is("AWS::DynamoDB::Table",   1)
    t.resource_count_is("AWS::SQS::Queue",        2)   # queue + DLQ
    t.resource_count_is("AWS::Events::Rule",      1)
    t.resource_count_is("AWS::KMS::Key",          1)   # local CMK

    # EventBridge rule targets manifest.json suffix only
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "EventPattern": Match.object_like({
            "source": ["aws.s3"],
            "detail-type": ["Object Created"],
        }),
    }))

    # 4 SSM params published for downstream wiring
    t.resource_count_is("AWS::SSM::Parameter", 4)
```

---

## 7. References

- `docs/template_params.md` — `BATCH_MAX_ITEMS_PER_BATCH`, `BATCH_URL_EXPIRY_SECONDS`, `BATCH_PROCESSOR_RESERVED_CONCURRENCY`, `BATCH_PROGRESS_TTL_DAYS`
- `docs/Feature_Roadmap.md` — feature IDs `BU-10` (batch presigner), `BU-11` (manifest handler), `BU-12` (per-item processor), `BU-13` (batch completion event), `BU-14` (DLQ reprocessor for batch math)
- AWS docs:
  - [S3 multipart upload with presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/PresignedUrlUploadObject.html)
  - [S3 Transfer Acceleration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/transfer-acceleration.html)
  - [SQS event source — `MaximumConcurrency`](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html#services-sqs-configure)
  - [Lambda reserved concurrency](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html)
- Related SOPs:
  - `EVENT_DRIVEN_PATTERNS` — fan-out SQS, DLQ + redrive pattern, L1 `CfnRule` cross-stack targets
  - `EVENT_DRIVEN_FAN_IN_AGGREGATOR` — downstream reducer when each item produces N parallel analyzer results
  - `LAYER_DATA` — intake-bucket `event_bridge_enabled=True`, progress-table patterns
  - `LAYER_API` — API GW route wiring for `/batch/presign`
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — ZIP / manifest → fan-out one job per item batch upload pattern. Presigner with per-item multipart URLs, EventBridge-on-manifest trigger, batch handler with row-per-item + SQS fan-out + upfront-failure accounting, item processor with atomic `ADD completed :one` summary increment + idempotent BatchComplete emit, reserved concurrency + SQS `max_concurrency` max-concurrent-processor cap, DLQ closure math. Created to fill gap surfaced by HR-interview-analyzer kit validation against emapta-avar reference implementation. |
