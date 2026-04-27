# SOP — AWS Lambda Powertools v3 (logger · tracer · metrics · idempotency · parameters · batch · validation · feature flags)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Lambda Powertools for Python v3.x · Lambda Powertools for TypeScript v2.x · Lambda Layers · Idempotency via DDB · Parameters via SSM/Secrets Manager · structured JSON logging · X-Ray tracing · EMF metrics · `@event_parser` Pydantic validation · Feature Flags via AppConfig

---

## 1. Purpose

- Codify the **must-include observability + reliability layer** for every Lambda function. Powertools is not optional in production — it removes ~200 lines of boilerplate from each function while giving structured logs, traces, metrics, idempotency, and runtime config.
- Codify the **8 Powertools utilities** that account for ~95% of usage:
  1. **Logger** — structured JSON logs with correlation IDs
  2. **Tracer** — X-Ray decorator with auto-segment naming
  3. **Metrics** — CloudWatch EMF (no API call cost)
  4. **Idempotency** — DDB-backed exactly-once handler execution
  5. **Parameters** — SSM/Secrets Manager with TTL caching
  6. **Batch Processing** — partial-failure handling for SQS/Kinesis/DDB Streams
  7. **Event Parser** — Pydantic validation of event payloads
  8. **Feature Flags** — AppConfig dynamic config evaluation
- Codify the **Lambda Layer pattern** to avoid bundling Powertools into every function (saves ~10MB cold-start).
- This is the **Lambda quality bar partial**. Built on `LAYER_BACKEND_LAMBDA` (the 5 non-negotiables). Required by every serverless composite template.

When the SOW signals: "production Lambda", "structured logs", "exactly-once", "API rate limit avoidance", "feature flags in serverless".

---

## 2. Decision tree — which utilities for which workload

| Workload | Logger | Tracer | Metrics | Idempotency | Batch | Event Parser | Feature Flags |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Sync API handler (REST/HTTP) | ✅ | ✅ | ✅ | ⚠️ if mutating | — | ✅ | optional |
| Async event consumer (SQS/SNS) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | optional |
| Stream processor (Kinesis/DDB Streams) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | optional |
| Step Functions task | ✅ | ✅ | ✅ | depends | — | ✅ | optional |
| Cron / scheduled | ✅ | ✅ | ✅ | — | — | — | ✅ |

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC / single function | **§3 Monolith** — bundle Powertools as dependency |
| Production / 10+ functions | **§4 Layer Variant** — shared Lambda Layer per runtime |

---

## 3. Monolith Variant — Powertools as direct dependency

### 3.1 `requirements.txt` / `package.json`

```text
# Python
aws-lambda-powertools[tracer,parser,validation]==3.4.0
pydantic==2.9.0
```

```json
// TypeScript
"dependencies": {
  "@aws-lambda-powertools/logger": "^2.10.0",
  "@aws-lambda-powertools/tracer": "^2.10.0",
  "@aws-lambda-powertools/metrics": "^2.10.0",
  "@aws-lambda-powertools/idempotency": "^2.10.0",
  "@aws-lambda-powertools/parameters": "^2.10.0",
  "@aws-lambda-powertools/parser": "^2.10.0"
}
```

### 3.2 Canonical Python handler — all 8 utilities

```python
# handler.py
from typing import Optional
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer, IdempotencyConfig, idempotent,
)
from aws_lambda_powertools.utilities.parameters import get_parameter, get_secret
from aws_lambda_powertools.utilities.parser import event_parser, BaseModel
from aws_lambda_powertools.utilities.feature_flags import FeatureFlags, AppConfigStore
from aws_lambda_powertools.utilities.typing import LambdaContext

# ── Module-level instances (re-used across invocations on warm starts) ──
logger = Logger(service="checkout-svc")           # service name auto-tagged on every log
tracer = Tracer(service="checkout-svc")           # X-Ray segments named after handler
metrics = Metrics(namespace="Checkout", service="checkout-svc")

# Idempotency — DDB table with key = JMESPath of payload
idem_store = DynamoDBPersistenceLayer(table_name="idempotency-store")
idem_config = IdempotencyConfig(
    event_key_jmespath='body."order_id"',          # idempotency key from event body
    expires_after_seconds=3600,                     # 1h dedup window
    use_local_cache=True,                            # warm-start cache
)

# Feature flags — AppConfig
ff_store = AppConfigStore(environment="prod", application="checkout", name="features")
feature_flags = FeatureFlags(store=ff_store)


# ── Pydantic event model — fail fast on bad input ──
class OrderRequest(BaseModel):
    order_id: str
    customer_id: str
    items: list[dict]
    total_cents: int


# ── Handler with all Powertools layered on ──
@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
@idempotent(persistence_store=idem_store, config=idem_config)
@event_parser(model=OrderRequest)
def lambda_handler(event: OrderRequest, context: LambdaContext) -> dict:
    logger.info("processing order", extra={"order_id": event.order_id})

    # Parameters — auto-cached (TTL 5 min default)
    api_key = get_secret("/prod/checkout/payment-api-key")
    fee_pct = float(get_parameter("/prod/checkout/fee-pct"))

    # Feature flag evaluation
    use_new_pricing = feature_flags.evaluate(
        name="new_pricing_engine",
        context={"customer_id": event.customer_id, "tier": "gold"},
        default=False,
    )

    # Custom metric
    metrics.add_metric(name="OrderProcessed", unit=MetricUnit.Count, value=1)
    metrics.add_metadata(key="order_id", value=event.order_id)

    # Custom trace subsegment
    with tracer.provider.in_subsegment("calculate_total"):
        total = _calculate_total(event.items, fee_pct, use_new_pricing)

    logger.info("order priced", extra={"total_cents": total})
    return {"statusCode": 200, "body": {"order_id": event.order_id, "total_cents": total}}


@tracer.capture_method  # any helper method auto-traced
def _calculate_total(items, fee_pct, use_new_pricing) -> int:
    subtotal = sum(i["price_cents"] * i["qty"] for i in items)
    return int(subtotal * (1 + (fee_pct / 100)))
```

### 3.3 SQS batch processing with partial failure

```python
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor, EventType, process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord

processor = BatchProcessor(event_type=EventType.SQS)


def record_handler(record: SQSRecord):
    payload = record.json_body                        # auto-parsed
    logger.append_keys(message_id=record.message_id)  # add to all subsequent logs
    # ... process record; raise to mark as failure ...
    process_one(payload)


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def lambda_handler(event, context):
    return process_partial_response(
        event=event, record_handler=record_handler,
        processor=processor, context=context,
    )
    # Returns batchItemFailures so SQS only retries failed records.
```

### 3.4 Idempotency table CDK

```python
from aws_cdk import aws_dynamodb as ddb

idem_table = ddb.Table(self, "IdempotencyStore",
    table_name="idempotency-store",
    partition_key=ddb.Attribute(name="id", type=ddb.AttributeType.STRING),
    billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
    time_to_live_attribute="expiration",          # auto-cleanup expired records
    encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
    encryption_key=kms_key,
    removal_policy=RemovalPolicy.RETAIN,           # protect against accidental delete
    point_in_time_recovery=True,
)
idem_table.grant_read_write_data(lambda_fn)
```

---

## 4. Layer Variant — shared Powertools layer

Lambda Powertools is published as a managed AWS Layer per region per runtime, OR you build your own.

### 4.1 Option A — AWS-managed layer (recommended)

```python
from aws_cdk import aws_lambda as _lambda

POWERTOOLS_LAYER_ARNS = {
    "us-east-1": "arn:aws:lambda:us-east-1:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:7",
    "us-west-2": "arn:aws:lambda:us-west-2:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:7",
    "eu-west-1": "arn:aws:lambda:eu-west-1:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:7",
}

powertools_layer = _lambda.LayerVersion.from_layer_version_arn(
    self, "PowertoolsLayer",
    POWERTOOLS_LAYER_ARNS[self.region],
)

fn = _lambda.Function(self, "Fn",
    runtime=_lambda.Runtime.PYTHON_3_12,
    handler="handler.lambda_handler",
    code=_lambda.Code.from_asset("src/"),         # only your code, not Powertools
    layers=[powertools_layer],
    environment={
        "POWERTOOLS_SERVICE_NAME": "checkout-svc",
        "POWERTOOLS_METRICS_NAMESPACE": "Checkout",
        "POWERTOOLS_LOG_LEVEL": "INFO",
        "POWERTOOLS_LOGGER_LOG_EVENT": "false",       # set true in non-prod for debug
        "POWERTOOLS_LOGGER_SAMPLE_RATE": "0.01",      # 1% of INFO logs at DEBUG
        "POWERTOOLS_TRACE_DISABLED": "false",
        "POWERTOOLS_TRACER_CAPTURE_RESPONSE": "false", # don't trace large responses
        "POWERTOOLS_PARAMETERS_MAX_AGE": "300",       # 5 min cache for params
        "POWERTOOLS_IDEMPOTENCY_DISABLED": "false",   # safety toggle
    },
    tracing=_lambda.Tracing.ACTIVE,
)
```

### 4.2 Option B — custom layer (when you need extra deps)

```python
from aws_cdk import aws_lambda as _lambda

custom_layer = _lambda.LayerVersion(self, "CustomLayer",
    code=_lambda.Code.from_asset("layers/python",
        bundling=BundlingOptions(
            image=_lambda.Runtime.PYTHON_3_12.bundling_image,
            command=["bash", "-c",
                "pip install -r requirements.txt -t /asset-output/python && "
                "find /asset-output/python -name '*.pyc' -delete"],
        ),
    ),
    compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
    description="Powertools v3 + Pydantic v2 + boto3",
)
```

`layers/python/requirements.txt`:
```
aws-lambda-powertools[tracer,parser,validation]==3.4.0
pydantic==2.9.0
boto3==1.35.40
```

---

## 5. Common gotchas

- **Module-level instances must be defined OUTSIDE the handler.** Defining `Logger()` inside `lambda_handler` re-creates it per invocation → no benefit from warm-start cache.
- **`@idempotent` decorator order matters** — must come AFTER `@event_parser` so the parsed/validated event is what's keyed; idempotency expects a dict-like, not raw event.
- **Idempotency `event_key_jmespath` must be deterministic.** Don't use `timestamp` or any field that varies on legitimate retries.
- **EMF metrics are extracted from logs by CloudWatch** — there's no `PutMetricData` API call, so no rate limit. But the log lines must be ≤ 256 KB and the metric namespace + dimensions ≤ 30.
- **`POWERTOOLS_LOGGER_LOG_EVENT=true` leaks PII into logs.** Disable in prod. Use `logger.append_keys(...)` for safe context.
- **Tracer `@capture_response: true` + large response = X-Ray segment too large to ingest** — silently dropped traces. Default to `false` for endpoints returning blobs.
- **Parameters cache is per-Lambda-invocation-context** — shared across invocations on the same warm container, NOT across containers. Concurrency = N containers = N cache misses on first invocation each.
- **Feature Flags SDK polls AppConfig** every `POWERTOOLS_PARAMETERS_MAX_AGE` seconds. New flag values not visible immediately. Account for this in tests.
- **Batch processor `process_partial_response` requires the function's reportBatchItemFailures = true** in the event source mapping. Without it, a single failure triggers full-batch redrive.
- **Lambda Layer ARN is region-locked.** When deploying to a new region, look up the matching ARN — they differ across regions.
- **Powertools v3 Python dropped `parser`'s legacy `BaseEnvelope` API** — migrate to model-first per [migration guide](https://docs.powertools.aws.dev/lambda/python/latest/upgrade/).

---

## 6. Pytest worked example

```python
# tests/test_handler.py
import pytest
from aws_lambda_powertools.utilities.idempotency import IdempotencyConfig
from aws_lambda_powertools.utilities.parser import ValidationError
from handler import lambda_handler


@pytest.fixture
def valid_event():
    return {
        "body": {
            "order_id": "ord_123",
            "customer_id": "cust_abc",
            "items": [{"sku": "X", "qty": 2, "price_cents": 1000}],
            "total_cents": 2000,
        },
    }


def test_happy_path(valid_event, lambda_context, mock_ddb_idempotency):
    resp = lambda_handler(valid_event, lambda_context)
    assert resp["statusCode"] == 200
    assert resp["body"]["order_id"] == "ord_123"


def test_invalid_event_rejected(lambda_context):
    bad = {"body": {"order_id": "x"}}  # missing required fields
    with pytest.raises(ValidationError):
        lambda_handler(bad, lambda_context)


def test_idempotent_replay_returns_cached(valid_event, lambda_context, mock_ddb_idempotency):
    """Calling twice with same order_id returns cached response without re-execution."""
    r1 = lambda_handler(valid_event, lambda_context)
    r2 = lambda_handler(valid_event, lambda_context)
    assert r1 == r2
    # Verify only ONE DDB write to idempotency store


def test_metrics_emitted(valid_event, lambda_context, mock_ddb_idempotency, capsys):
    lambda_handler(valid_event, lambda_context)
    out = capsys.readouterr().out
    assert '"OrderProcessed"' in out          # EMF metric in log output
    assert '"ColdStart"' in out               # cold-start metric


def test_correlation_id_in_logs(valid_event, lambda_context, mock_ddb_idempotency, capsys):
    valid_event["headers"] = {"x-correlation-id": "test-trace-123"}
    lambda_handler(valid_event, lambda_context)
    out = capsys.readouterr().out
    assert "test-trace-123" in out
```

---

## 7. Five non-negotiables

1. **`@logger.inject_lambda_context` + `@tracer.capture_lambda_handler` + `@metrics.log_metrics` on every handler** — the trio is non-negotiable in prod.
2. **`@idempotent` on every handler that mutates state** (writes to DDB / S3 / external API) — DDB-backed store with TTL cleanup.
3. **`@event_parser` with Pydantic model on every API/event-driven handler** — fail fast on bad input.
4. **Powertools env vars set explicitly** (service name, namespace, log level, tracer capture toggles) — no defaults in prod.
5. **Lambda Layer (managed or custom) for ≥ 5 functions** — never bundle Powertools per-function.

---

## 8. References

- [AWS Lambda Powertools for Python — v3 docs](https://docs.powertools.aws.dev/lambda/python/latest/)
- [Lambda Powertools for TypeScript — v2 docs](https://docs.powertools.aws.dev/lambda/typescript/latest/)
- [Idempotency utility](https://docs.powertools.aws.dev/lambda/python/latest/utilities/idempotency/)
- [Batch processing utility](https://docs.powertools.aws.dev/lambda/python/latest/utilities/batch/)
- [Feature Flags utility + AppConfig](https://docs.powertools.aws.dev/lambda/python/latest/utilities/feature_flags/)
- [Lambda Layer ARNs (latest)](https://docs.powertools.aws.dev/lambda/python/latest/#lambda-layer)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Powertools v3 (Python) + v2 (TypeScript). Logger + Tracer + Metrics + Idempotency + Parameters + Batch + Event Parser + Feature Flags + Layer pattern. Wave 10. |
