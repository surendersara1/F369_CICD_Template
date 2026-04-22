# SOP — Event-Driven Fan-In Aggregator (N parallel producers → 1 aggregator)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · DynamoDB tracking table · SQS or DynamoDB Streams · Lambda aggregator · EventBridge completion event

---

## 1. Purpose

- Codify the **fan-in / scatter-gather** pattern where `N` parallel analyzer streams (text, audio, video, etc.) each emit a partial result, and a single aggregator Lambda waits for **all `N`** results keyed by a composite ID before computing a final weighted score.
- Provide the canonical **DynamoDB-backed aggregation ledger** with `ADD received_streams :one` conditional upsert, out-of-order arrival tolerance, and idempotent de-duplication.
- Provide a **partial-result TTL**: aggregate with whatever arrived after `max_wait_seconds` rather than blocking forever on a missing stream.
- Include when the SOW signals: "aggregate results from multiple analyzers", "combine scores", "multi-modal analysis", "reducer step", "gather N responses", "weighted composite score", "parallel analyzers → single verdict".
- This is the sibling of `EVENT_DRIVEN_PATTERNS` §4.1 (SNS fan-**out**). Fan-out = 1→N; fan-in = N→1.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| All producers, tracking table, aggregator Lambda, and downstream completion consumer in ONE stack | **§3 Monolith Variant** |
| Producers in `AnalyzerStack`(s), tracking table in `JobLedgerStack`, aggregator in `ComputeStack`, completion events on a shared custom bus | **§4 Micro-Stack Variant** |

**Why the split matters.** The aggregator Lambda needs:

1. `dynamodb:UpdateItem` on the **tracking table** (owned by `JobLedgerStack`).
2. `sqs:ReceiveMessage` / `DeleteMessage` on **one SQS queue per stream** (typically owned by `QueueStack`).
3. `events:PutEvents` on the **custom bus** (owned by `EventStack`) to emit the `AggregationComplete` event.
4. Optional KMS `Decrypt` on the tracking-table CMK (owned by `SecurityStack`).

Same anti-pattern family as `EVENT_DRIVEN_PATTERNS` §5.1 — every cross-stack `table.grant_*(fn)` / `queue.grant_*(fn)` silently mutates the upstream resource's policy and forces a bidirectional export → cycle. Micro-Stack variant grants identity-side only.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Stream A (text analyzer)   ──►  streamA.fifo ─┐
  Stream B (audio analyzer)  ──►  streamB.fifo ─┼──► aggregator_fn
  Stream C (video analyzer)  ──►  streamC.fifo ─┘          │
                                                           ▼
                     DynamoDB tracking table (aggregation-ledger)
                       PK = aggregation_key
                       attrs: received_streams (Number-Set),
                              scores (Map of stream -> score),
                              total_expected (Number),
                              created_at, ttl
                                                           │
                             ALL received_streams == total_expected
                                                           ▼
                               EventBridge custom bus → AggregationComplete
                                                           ▼
                        Downstream consumer (persist, notify, trigger next phase)
```

### 3.2 CDK — `_create_aggregator()` method body

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as les,
    aws_logs as logs,
    aws_sqs as sqs,
)


def _create_aggregator(self, stage: str) -> None:
    """Monolith variant. Assumes self.{kms_key, event_bus, lambda_sg, vpc} exist
    and self.stream_names = ["text", "audio", "video"]."""

    # A) Aggregation-ledger table. One row per aggregation_key.
    self.agg_ledger = ddb.Table(
        self, "AggregationLedger",
        table_name=f"{{project_name}}-aggregation-ledger-{stage}",
        partition_key=ddb.Attribute(
            name="aggregation_key", type=ddb.AttributeType.STRING
        ),
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

    # B) One DLQ shared by all stream queues
    self.agg_dlq = sqs.Queue(
        self, "AggregatorDLQ",
        queue_name=f"{{project_name}}-aggregator-dlq-{stage}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )

    # C) One FIFO queue per stream. MessageGroupId = aggregation_key keeps
    #    per-aggregation ordering but lets different aggregations parallelize.
    self.stream_queues: dict[str, sqs.Queue] = {}
    for name in self.stream_names:
        self.stream_queues[name] = sqs.Queue(
            self, f"Stream{name.title()}Queue",
            queue_name=f"{{project_name}}-stream-{name}-{stage}.fifo",
            fifo=True,
            content_based_deduplication=True,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.kms_key,
            visibility_timeout=Duration.seconds(90),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=self.agg_dlq
            ),
        )

    # D) Aggregator Lambda
    agg_log = logs.LogGroup(
        self, "AggregatorLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-aggregator-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.aggregator_fn = _lambda.Function(
        self, "AggregatorFn",
        function_name=f"{{project_name}}-aggregator-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset("lambda/aggregator"),
        memory_size=512,
        timeout=Duration.seconds(30),
        log_group=agg_log,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "AGG_LEDGER_TABLE": self.agg_ledger.table_name,
            "EVENT_BUS_NAME":   self.event_bus.event_bus_name,
            "EXPECTED_STREAMS": ",".join(sorted(self.stream_names)),
            "TTL_SECONDS":      str(60 * 60 * 24),       # 24h
            "WEIGHTS_JSON":     '{"text":0.5,"audio":0.3,"video":0.2}',
            "POWERTOOLS_SERVICE_NAME": "{project_name}-aggregator",
            "POWERTOOLS_LOG_LEVEL":    "INFO",
        },
    )
    # Same-stack L2 grants are safe in monolith
    self.agg_ledger.grant_read_write_data(self.aggregator_fn)
    self.event_bus.grant_put_events_to(self.aggregator_fn)

    # E) Wire each stream queue as an event source
    for name, q in self.stream_queues.items():
        q.grant_consume_messages(self.aggregator_fn)
        self.aggregator_fn.add_event_source(les.SqsEventSource(
            q,
            batch_size=10,
            max_batching_window=Duration.seconds(2),
            report_batch_item_failures=True,
        ))

    # F) Partial-result sweeper — fires every 5 minutes, flushes any
    #    aggregation_key whose `created_at + max_wait` has elapsed.
    sweeper_log = logs.LogGroup(
        self, "AggSweeperLogs",
        log_group_name=f"/aws/lambda/{{project_name}}-agg-sweeper-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.agg_sweeper_fn = _lambda.Function(
        self, "AggSweeperFn",
        function_name=f"{{project_name}}-agg-sweeper-{stage}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.lambda_handler",
        code=_lambda.Code.from_asset("lambda/agg_sweeper"),
        memory_size=256,
        timeout=Duration.seconds(60),
        log_group=sweeper_log,
        environment={
            "AGG_LEDGER_TABLE": self.agg_ledger.table_name,
            "EVENT_BUS_NAME":   self.event_bus.event_bus_name,
            "MAX_WAIT_SECONDS": "900",                    # 15 min
            "WEIGHTS_JSON":     '{"text":0.5,"audio":0.3,"video":0.2}',
        },
    )
    self.agg_ledger.grant_read_write_data(self.agg_sweeper_fn)
    self.event_bus.grant_put_events_to(self.agg_sweeper_fn)
    events.Rule(
        self, "AggSweeperSchedule",
        rule_name=f"{{project_name}}-agg-sweeper-{stage}",
        schedule=events.Schedule.rate(Duration.minutes(5)),
        targets=[targets.LambdaFunction(self.agg_sweeper_fn)],
    )

    CfnOutput(self, "AggregatorFnArn", value=self.aggregator_fn.function_arn)
    CfnOutput(self, "AggLedgerName",   value=self.agg_ledger.table_name)
```

### 3.3 Aggregator handler — saved to `lambda/aggregator/index.py`

```python
"""Fan-in aggregator.

Each SQS message = {aggregation_key, stream, score, payload_hash, produced_at}.
We atomically merge the score into the ledger row and, iff all expected streams
have arrived, emit AggregationComplete on the custom bus.
"""
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb   = boto3.resource("dynamodb")
table = ddb.Table(os.environ["AGG_LEDGER_TABLE"])
ebr   = boto3.client("events")

BUS              = os.environ["EVENT_BUS_NAME"]
EXPECTED_STREAMS = set(os.environ["EXPECTED_STREAMS"].split(","))
TTL_SECONDS      = int(os.environ.get("TTL_SECONDS", "86400"))
WEIGHTS          = {k: Decimal(str(v)) for k, v in json.loads(
    os.environ.get("WEIGHTS_JSON", "{}")).items()}


def lambda_handler(event, _ctx):
    """SQS batch. Returns batchItemFailures for poison messages only."""
    failures: list[dict] = []
    for record in event["Records"]:
        try:
            _process(record)
        except Exception:
            logger.exception("aggregator failed for record %s", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _process(record: dict) -> None:
    body          = json.loads(record["body"])
    agg_key       = body["aggregation_key"]
    stream        = body["stream"]
    score         = Decimal(str(body["score"]))
    payload_hash  = body.get("payload_hash", "")      # idempotency key
    now           = int(time.time())

    if stream not in EXPECTED_STREAMS:
        logger.warning("unknown stream=%s key=%s — dropping", stream, agg_key)
        return

    # Atomic upsert. ADD on a String-Set is idempotent; re-delivery is safe.
    # Using payload_hash in a dedup map rejects replay of the same partial.
    try:
        resp = table.update_item(
            Key={"aggregation_key": agg_key},
            UpdateExpression=(
                "ADD received_streams :stream "
                "SET scores.#s = :score, "
                "    payload_hashes.#s = :h, "
                "    total_expected = if_not_exists(total_expected, :n), "
                "    created_at     = if_not_exists(created_at, :now), "
                "    #ttl           = if_not_exists(#ttl, :ttl)"
            ),
            ConditionExpression=(
                # Reject duplicate payload for same stream. If attribute exists
                # with a DIFFERENT hash we let it through (re-scoring allowed).
                "attribute_not_exists(payload_hashes.#s) OR "
                "payload_hashes.#s = :h"
            ),
            ExpressionAttributeNames={"#s": stream, "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":stream": {stream},
                ":score":  score,
                ":h":      payload_hash,
                ":n":      Decimal(len(EXPECTED_STREAMS)),
                ":now":    Decimal(now),
                ":ttl":    Decimal(now + TTL_SECONDS),
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Duplicate replay — skip silently. Ledger already has the value.
            logger.info("duplicate stream=%s key=%s hash=%s — skipping",
                        stream, agg_key, payload_hash)
            return
        raise

    attrs    = resp["Attributes"]
    received = set(attrs.get("received_streams", set()))

    if received == EXPECTED_STREAMS:
        _emit_complete(agg_key, attrs)


def _emit_complete(agg_key: str, attrs: dict) -> None:
    scores = {k: float(v) for k, v in attrs.get("scores", {}).items()}
    weighted = sum(scores[k] * float(WEIGHTS.get(k, 0)) for k in scores)

    detail = {
        "aggregation_key": agg_key,
        "status":          "COMPLETE",
        "scores":          scores,
        "weighted_score":  round(weighted, 4),
        "streams_received": sorted(scores.keys()),
    }
    ebr.put_events(Entries=[{
        "Source":       "{project_name}.aggregator",
        "DetailType":   "AggregationComplete",
        "Detail":       json.dumps(detail),
        "EventBusName": BUS,
    }])
    logger.info("aggregation COMPLETE key=%s weighted=%s", agg_key, detail["weighted_score"])
```

### 3.4 Partial-result sweeper — saved to `lambda/agg_sweeper/index.py`

```python
"""Emitted every 5 min. Finds ledger rows older than MAX_WAIT_SECONDS
that have NOT yet emitted AggregationComplete, then emits a
PARTIAL-COMPLETE event with whatever scores arrived."""
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb     = boto3.resource("dynamodb")
table   = ddb.Table(os.environ["AGG_LEDGER_TABLE"])
ebr     = boto3.client("events")

BUS          = os.environ["EVENT_BUS_NAME"]
MAX_WAIT     = int(os.environ.get("MAX_WAIT_SECONDS", "900"))
WEIGHTS_JSON = json.loads(os.environ.get("WEIGHTS_JSON", "{}"))


def lambda_handler(_event, _ctx):
    cutoff = int(time.time()) - MAX_WAIT
    # Scan is acceptable because TTL keeps the table small (< 1 GB typical).
    # Switch to a GSI on `status` if the table grows large.
    scanned = table.scan(
        FilterExpression="created_at < :c AND attribute_not_exists(emitted)",
        ExpressionAttributeValues={":c": cutoff},
    )
    flushed = 0
    for row in scanned.get("Items", []):
        _flush_partial(row)
        flushed += 1
    logger.info("swept rows=%d flushed=%d cutoff=%d", len(scanned.get("Items", [])),
                flushed, cutoff)


def _flush_partial(row: dict) -> None:
    agg_key = row["aggregation_key"]
    scores  = {k: float(v) for k, v in row.get("scores", {}).items()}
    weighted = sum(scores[k] * float(WEIGHTS_JSON.get(k, 0)) for k in scores)
    ebr.put_events(Entries=[{
        "Source":       "{project_name}.aggregator",
        "DetailType":   "AggregationPartial",
        "Detail":       json.dumps({
            "aggregation_key": agg_key,
            "status":          "PARTIAL",
            "scores":          scores,
            "weighted_score":  round(weighted, 4),
            "streams_received": sorted(scores.keys()),
            "missing_streams": sorted(set(os.environ["EXPECTED_STREAMS"].split(","))
                                      - set(scores.keys()))
                                if os.environ.get("EXPECTED_STREAMS") else [],
        }),
        "EventBusName": BUS,
    }])
    # Mark as emitted so the sweeper does not double-fire
    table.update_item(
        Key={"aggregation_key": agg_key},
        UpdateExpression="SET emitted = :one",
        ExpressionAttributeValues={":one": 1},
    )
```

### 3.5 Monolith gotchas

- **`ADD received_streams :stream`** requires `:stream` to be a set type (`{stream}` in Python boto3 creates a `set` literal, which the resource client auto-maps to DynamoDB String-Set). Passing `[stream]` creates a List and `ADD` fails with `ValidationException`.
- **`ConditionalCheckFailedException` is NOT an error** for duplicate replays — it is the idempotency signal. Swallow it explicitly or CloudWatch fills with false-positive errors.
- **String-Set vs Number-Set** — prefer string set for stream names (future-proofs against non-integer identifiers). Use number set only when aggregating numeric IDs.
- **FIFO queue `MessageGroupId = aggregation_key`** means per-aggregation ordering; different `aggregation_key`s still parallelize. Without a MessageGroupId, FIFO fails to publish.
- **Sweeper Scan**: once the ledger exceeds ~5 GB, replace the scan with a GSI on `status` (partition key) + `created_at` (sort). The FilterExpression pattern does not scale.
- **`grant_put_events_to`** auto-grants in monolith — it adds `events:PutEvents` resource to the Lambda's identity policy with the bus ARN. No cross-stack drama in monolith.
- **24-hour TTL** — long enough to ride out Transcribe retries (15-min typical) and human-in-the-loop delays, short enough to keep table small. Raise for batch-style fan-in.

---

## 4. Micro-Stack Variant

**Use when:** producers, tracking table, aggregator Lambda, and custom bus live in different CDK stacks (`AnalyzerStack`, `JobLedgerStack`, `ComputeStack`, `EventStack`).

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** (`_LAMBDAS_ROOT` pattern).
2. **Never use `table.grant_read_write_data(fn)` cross-stack.** Identity-side `PolicyStatement` on the aggregator role.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** SQS event source mapping itself is fine (no policy mutation) — only L2 `grant_consume_messages` is dangerous. Use identity-side grants.
4. **Never set `encryption_key=ext_key`** on a table created in another stack — but in fan-in the ledger IS in `JobLedgerStack`, so this just means: decrypt permission lives as identity-side `kms:Decrypt` on the aggregator role, not on the KMS key policy.
5. **Never call `event_bus.grant_put_events_to(fn)` cross-stack.** Identity-side `events:PutEvents` with the bus ARN read from SSM.

### 4.2 Dedicated `AggregatorStack`

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
    aws_lambda_event_sources as les,
    aws_logs as logs,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class AggregatorStack(cdk.Stack):
    """Fan-in aggregator. Consumes N stream queues, writes one tracking row per
    aggregation_key, emits AggregationComplete on the custom bus when all
    streams arrive or the sweeper fires after MAX_WAIT_SECONDS.

    All cross-stack resources are imported by SSM parameter (or IInterface
    handles passed in by the app wiring). No cross-stack grant_* calls.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        agg_ledger_arn_ssm: str,
        agg_ledger_kms_arn_ssm: str,
        event_bus_arn_ssm: str,
        event_bus_name_ssm: str,
        stream_queue_arn_ssms: dict[str, str],      # e.g. {"text": "/.../arn", ...}
        agg_dlq_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        expected_streams: list[str],
        weights: dict[str, float],
        max_wait_seconds: int = 900,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-aggregator-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Read cross-stack references from SSM — strings, not L2 constructs.
        # String-parameter tokens resolve at deploy time to the actual ARN.
        agg_ledger_arn  = ssm.StringParameter.value_for_string_parameter(
            self, agg_ledger_arn_ssm
        )
        agg_ledger_name = cdk.Fn.select(
            1, cdk.Fn.split("/", agg_ledger_arn)          # arn:aws:dynamodb:...:table/<name>
        )
        agg_kms_arn     = ssm.StringParameter.value_for_string_parameter(
            self, agg_ledger_kms_arn_ssm
        )
        bus_arn         = ssm.StringParameter.value_for_string_parameter(
            self, event_bus_arn_ssm
        )
        bus_name        = ssm.StringParameter.value_for_string_parameter(
            self, event_bus_name_ssm
        )

        # Local log groups — always in-stack
        agg_log = logs.LogGroup(
            self, "AggregatorLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-aggregator-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        aggregator_fn = _lambda.Function(
            self, "AggregatorFn",
            function_name=f"{{project_name}}-aggregator-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "aggregator")),
            memory_size=512,
            timeout=Duration.seconds(30),
            log_group=agg_log,
            tracing=_lambda.Tracing.ACTIVE,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[lambda_sg],
            environment={
                "AGG_LEDGER_TABLE": agg_ledger_name.to_string(),
                "EVENT_BUS_NAME":   bus_name,
                "EXPECTED_STREAMS": ",".join(sorted(expected_streams)),
                "TTL_SECONDS":      str(60 * 60 * 24),
                "WEIGHTS_JSON":     cdk.Fn.sub(
                    '{ "text":0.5, "audio":0.3, "video":0.2 }'  # TODO(verify): pass dict via json.dumps
                ),
                "POWERTOOLS_SERVICE_NAME": "{project_name}-aggregator",
                "POWERTOOLS_LOG_LEVEL":    "INFO",
            },
        )

        # -- Identity-side grants ONLY ---------------------------------------
        # DynamoDB
        aggregator_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "dynamodb:GetItem", "dynamodb:UpdateItem",
                "dynamodb:PutItem", "dynamodb:Query",
            ],
            resources=[agg_ledger_arn, f"{agg_ledger_arn}/index/*"],
        ))
        # KMS for ledger
        aggregator_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[agg_kms_arn],
        ))
        # EventBridge PutEvents to the custom bus
        aggregator_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"],
            resources=[bus_arn],
        ))

        # -- One event-source mapping per stream queue -----------------------
        for stream_name, arn_ssm in stream_queue_arn_ssms.items():
            q_arn = ssm.StringParameter.value_for_string_parameter(self, arn_ssm)
            # Identity-side SQS consume
            aggregator_fn.add_to_role_policy(iam.PolicyStatement(
                actions=[
                    "sqs:ReceiveMessage", "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility",
                ],
                resources=[q_arn],
            ))
            # Event-source mapping takes an IQueue — reconstitute from ARN.
            queue = sqs.Queue.from_queue_attributes(
                self, f"{stream_name.title()}QueueImport",
                queue_arn=q_arn,
                # QueueUrl is derived implicitly when only ARN is given.
                fifo=True,
            )
            aggregator_fn.add_event_source(les.SqsEventSource(
                queue,
                batch_size=10,
                max_batching_window=Duration.seconds(2),
                report_batch_item_failures=True,
            ))

        iam.PermissionsBoundary.of(aggregator_fn.role).apply(permission_boundary)

        # -- Sweeper Lambda for partial-result flush -------------------------
        sweeper_log = logs.LogGroup(
            self, "AggSweeperLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-agg-sweeper-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        sweeper_fn = _lambda.Function(
            self, "AggSweeperFn",
            function_name=f"{{project_name}}-agg-sweeper-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "agg_sweeper")),
            memory_size=256,
            timeout=Duration.seconds(60),
            log_group=sweeper_log,
            environment={
                "AGG_LEDGER_TABLE": agg_ledger_name.to_string(),
                "EVENT_BUS_NAME":   bus_name,
                "EXPECTED_STREAMS": ",".join(sorted(expected_streams)),
                "MAX_WAIT_SECONDS": str(max_wait_seconds),
                "WEIGHTS_JSON":     '{"text":0.5,"audio":0.3,"video":0.2}',
            },
        )
        sweeper_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Scan", "dynamodb:UpdateItem"],
            resources=[agg_ledger_arn],
        ))
        sweeper_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[agg_kms_arn],
        ))
        sweeper_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"],
            resources=[bus_arn],
        ))
        iam.PermissionsBoundary.of(sweeper_fn.role).apply(permission_boundary)

        events.Rule(
            self, "AggSweeperSchedule",
            rule_name=f"{{project_name}}-agg-sweeper-{stage_name}",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(sweeper_fn)],
        )

        CfnOutput(self, "AggregatorFnArn", value=aggregator_fn.function_arn)
        CfnOutput(self, "AggSweeperFnArn", value=sweeper_fn.function_arn)
```

### 4.3 Micro-stack gotchas

- **`Queue.from_queue_attributes(queue_arn=...)`** — CDK requires at minimum the ARN; the URL is reconstructed. Keep `fifo=True` when importing FIFO queues or event-source mapping silently treats them as standard.
- **`ssm.StringParameter.value_for_string_parameter`** returns a **token** (not a string). Use `cdk.Fn.sub` / `cdk.Fn.split` for any string manipulation at synth time; do not `.split("/")` in Python — you get a token-substring error.
- **`agg_ledger_name`** derivation via `cdk.Fn.select(1, cdk.Fn.split("/", arn))` works because DDB ARNs are `arn:aws:dynamodb:region:account:table/<name>`. Confirm in `cdk.out/<stack>.template.json`. `# TODO(verify): assert this survives yarn-packaged stack exports.`
- **Sweeper `dynamodb:Scan`** — scoped to the ledger ARN only. A table-wide scan without a resource constraint is a security finding.
- **KMS grants under the same account** — `kms:Decrypt` identity-side works because the table's encryption uses KMS via the service principal, which already has AllowDecrypt on the key policy for root. Cross-account would need an explicit key-policy statement (out of scope).

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| 2 streams only (binary fan-in) | Use a single DDB row with two boolean fields; no String-Set, no sweeper — simpler |
| Hundreds of streams | Replace String-Set with a separate "stream_arrivals" table keyed on `(aggregation_key, stream_id)` + COUNT query on completion check |
| Strict SLA on completion (sub-second) | Switch from SQS to Kinesis or DDB Streams; use `parallelization_factor` > 1 on the aggregator event source |
| Exactly-once weighted scoring under concurrent replays | FIFO SQS + `MessageDeduplicationId = sha256(aggregation_key + stream + payload_hash)` |
| Need streaming updates (emit partial after each arrival) | Emit `AggregationProgress` on every update, not just completion |
| Aggregation key spans many days | Remove TTL or set it to 30 days+; keep sweeper interval as-is |
| Multi-region | Replicate ledger with DynamoDB Global Tables; aggregator still regional; completion event on a global bus via cross-region rule |

---

## 6. Worked example — pytest offline CDK synth

Save as `tests/sop/test_EVENT_DRIVEN_FAN_IN_AGGREGATOR.py`. Offline; no AWS calls.

```python
"""SOP verification — AggregatorStack synthesizes three event sources + sweeper."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_aggregator_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg   = ec2.SecurityGroup(deps, "LambdaSg", vpc=vpc)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.aggregator_stack import AggregatorStack
    stack = AggregatorStack(
        app,
        stage_name="dev",
        vpc=vpc,
        lambda_sg=sg,
        agg_ledger_arn_ssm="/test/ddb/agg_ledger_arn",
        agg_ledger_kms_arn_ssm="/test/kms/agg_ledger_arn",
        event_bus_arn_ssm="/test/events/bus_arn",
        event_bus_name_ssm="/test/events/bus_name",
        stream_queue_arn_ssms={
            "text":  "/test/sqs/text_arn",
            "audio": "/test/sqs/audio_arn",
            "video": "/test/sqs/video_arn",
        },
        agg_dlq_arn_ssm="/test/sqs/agg_dlq_arn",
        permission_boundary=boundary,
        expected_streams=["text", "audio", "video"],
        weights={"text": 0.5, "audio": 0.3, "video": 0.2},
        env=env,
    )
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function",            2)  # aggregator + sweeper
    t.resource_count_is("AWS::Lambda::EventSourceMapping",  3)  # one per stream
    t.resource_count_is("AWS::Events::Rule",                1)  # sweeper schedule
```

---

## 7. References

- `docs/template_params.md` — `AGG_EXPECTED_STREAMS`, `AGG_WEIGHTS_JSON`, `AGG_MAX_WAIT_SECONDS`, `AGG_TTL_SECONDS`
- `docs/Feature_Roadmap.md` — feature IDs `FI-01` (aggregator ledger), `FI-02` (partial sweeper), `FI-03` (streaming progress events)
- AWS docs:
  - [DynamoDB UpdateItem — ADD action](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.UpdateExpressions.html)
  - [DynamoDB Conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
  - [SQS FIFO MessageGroupId](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/using-messagegroupid-property.html)
  - [EventBridge PutEvents](https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutEvents.html)
- Related SOPs:
  - `EVENT_DRIVEN_PATTERNS` — sibling fan-**out** pattern + DLQ primitives
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables
  - `LAYER_DATA` — JobLedger table shape + DDB Streams
  - `WORKFLOW_STEP_FUNCTIONS` — alternative: Step Functions Parallel state for bounded fan-in

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — fan-in aggregator (N parallel producers → 1 reducer) with DynamoDB atomic-merge ledger, String-Set `ADD received_streams`, idempotent `payload_hashes.#stream` dedup, partial-result sweeper, weighted composite scoring, FIFO per-stream queues. Created to fill gap surfaced by HR-interview-analyzer kit validation against emapta-avar reference implementation. |
