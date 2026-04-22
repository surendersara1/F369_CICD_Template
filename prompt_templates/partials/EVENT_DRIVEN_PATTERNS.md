# SOP — Event-Driven Patterns (SNS, SQS, EventBridge, Kinesis)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+)

---

## 1. Purpose

Wiring between producers and consumers: SNS fan-out, SQS (standard + FIFO), EventBridge rules and custom buses, Kinesis streams, DLQ + redrive, scheduled triggers.

This partial covers the *connectors*. The producers (Lambda, Fargate) come from `LAYER_BACKEND_LAMBDA` / `LAYER_BACKEND_ECS`. The target data stores come from `LAYER_DATA`.

---

## 2. When to include each service

| SOW signal | Service |
|---|---|
| "decouple", "async", "fan-out", "pub/sub" | SNS → SQS Fan-out |
| "ordered", "exactly-once", "FIFO" | SQS FIFO |
| "event bus", "domain events", "microservice events" | EventBridge custom bus |
| "streaming", "high-throughput events", ">1k events/sec" | Kinesis Data Streams |
| "S3 to pipeline", "file arrives → process" | S3 → EventBridge (never direct S3 → Lambda at scale) |
| "retry", "dead letter", "poison message" | DLQ + redrive |
| "delay", "scheduled retry", "backoff" | SQS delay queue |

---

## 3. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| All producers, queues, and consumers in ONE stack class | **§4 Monolith Variant** |
| Queues in `QueueStack`, buses in `EventStack`, consumers in `ComputeStack` (separate CDK stacks) | **§5 Micro-Stack Variant** |

**Why the split matters.** Two common L2 helpers blow up across stacks:

1. **`targets.SqsQueue(queue)`** on an EventBridge rule *auto-adds a resource policy* to the queue allowing the rule's ARN to send. If rule and queue are in different stacks → bidirectional export → cycle.
2. **`queue.grant_send_messages(role)`** where `queue` and `role` are in different stacks does the same thing from the other direction.

The Micro-Stack variant:
- Uses L1 `events.CfnRule` (raw dict target) when the target lives in a different stack
- Adds static-ARN resource policies to queues/topics manually
- Grants consumers via identity-side policies on the consumer role

---

## 4. Monolith Variant

### 4.1 SNS → SQS fan-out

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration, RemovalPolicy,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_sns_subscriptions as subs,
    aws_iam as iam,
)


def _create_fanout(self, stage: str) -> None:
    # One central topic
    self.order_topic = sns.Topic(
        self, "OrderCreatedTopic",
        topic_name=f"{{project_name}}-order-created-{stage}",
        master_key=self.kms_key,      # SSE-KMS
    )

    # DLQ shared by fan-out consumers
    self.fanout_dlq = sqs.Queue(
        self, "FanoutDLQ",
        queue_name=f"{{project_name}}-fanout-dlq-{stage}",
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )

    # Three subscriber queues, each triggering a different Lambda
    for subscriber in ["inventory", "email-notify", "analytics"]:
        q = sqs.Queue(
            self, f"{subscriber.title().replace('-', '')}Queue",
            queue_name=f"{{project_name}}-{subscriber}-{stage}",
            encryption_master_key=self.kms_key,
            visibility_timeout=Duration.seconds(180),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=self.fanout_dlq
            ),
        )
        self.order_topic.add_subscription(subs.SqsSubscription(
            q,
            raw_message_delivery=True,
            dead_letter_queue=self.fanout_dlq,
        ))
```

### 4.2 EventBridge custom bus + rules + archive

```python
from aws_cdk import aws_events as events, aws_events_targets as targets


def _create_event_bus(self, stage: str) -> None:
    self.bus = events.EventBus(
        self, "DomainBus",
        event_bus_name=f"{{project_name}}-domain-bus-{stage}",
    )

    # Archive (required for replay). 30-day retention default.
    self.bus.archive(
        "DomainArchive",
        archive_name=f"{{project_name}}-domain-archive-{stage}",
        retention=Duration.days(30),
        event_pattern=events.EventPattern(source=events.Match.prefix("")),
    )

    # Rule: S3 ObjectCreated → processing Lambda (monolith: L2 target OK)
    events.Rule(
        self, "S3ObjectCreatedRule",
        event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
        event_pattern=events.EventPattern(
            source=["aws.s3"],
            detail_type=["Object Created"],
            detail={"bucket": {"name": [{"prefix": "{project_name}-"}]}},
        ),
        targets=[targets.LambdaFunction(self.lambda_functions["DocumentUpload"])],
    )

    # Rule: SFN failure → SNS ops topic (L2 target OK in monolith)
    events.Rule(
        self, "SFNFailedRule",
        event_pattern=events.EventPattern(
            source=["aws.states"],
            detail_type=["Step Functions Execution Status Change"],
            detail={"status": ["FAILED", "TIMED_OUT", "ABORTED"]},
        ),
        targets=[targets.SnsTopic(self.ops_topic)],
    )
```

### 4.3 SQS FIFO for ordered per-entity processing

```python
self.order_fifo = sqs.Queue(
    self, "OrderFifoQueue",
    queue_name=f"{{project_name}}-order.fifo",
    fifo=True,
    content_based_deduplication=True,
    encryption_master_key=self.kms_key,
    visibility_timeout=Duration.seconds(120),
)
# Producer uses MessageGroupId="order#<order_id>" to preserve per-order order.
```

### 4.4 Kinesis Data Stream (high-throughput producers)

```python
from aws_cdk import aws_kinesis as kinesis


self.telemetry_stream = kinesis.Stream(
    self, "TelemetryStream",
    stream_name=f"{{project_name}}-telemetry-{stage}",
    stream_mode=kinesis.StreamMode.ON_DEMAND,
    encryption=kinesis.StreamEncryption.KMS,
    encryption_key=self.kms_key,
    retention_period=Duration.hours(24),
)

from aws_cdk import aws_lambda_event_sources as les


self.lambda_functions["TelemetryProcessor"].add_event_source(
    les.KinesisEventSource(
        self.telemetry_stream,
        starting_position=_lambda.StartingPosition.TRIM_HORIZON,
        batch_size=100,
        max_batching_window=Duration.seconds(5),
        parallelization_factor=4,
        bisect_batch_on_error=True,
        retry_attempts=3,
        on_failure=les.SqsDlq(self.fanout_dlq),
    )
)
```

### 4.5 Monolith gotchas

- **SNS raw message delivery** (`raw_message_delivery=True`) strips the SNS envelope — consumer Lambda sees the original payload as `event["Records"][0]["body"]`. Without it, every consumer has to parse a wrapped JSON envelope.
- **DLQ on rule targets** is a separate config from **DLQ on the Lambda event-source mapping**. You probably want both.
- **SFN → EventBridge** event format differs between Standard and Express workflows. Express emits CloudWatch metrics, not EB events — if you need per-execution events on Express, invoke EB yourself from the state machine.

---

## 5. Micro-Stack Variant

### 5.1 The four non-negotiables for cross-stack event wiring

1. **Rule + target in different stacks → use L1 `events.CfnRule` with a raw `TargetProperty` dict.** L2 `events.Rule(targets=[targets.SqsQueue(q)])` auto-adds queue resource policy referencing the rule ARN — cycle.
2. **Queues and topics add resource policies via static-ARN conditions, not by CDK auto-grant.** Use `aws:SourceArn: arn:aws:events:{region}:{account}:rule/default/*` (wildcard on rule) or `arn:aws:sns:{region}:{account}:topic-name` (explicit topic ARN).
3. **Consumer Lambdas grant themselves SQS permissions via identity-side policy.** Never call `queue.grant_consume_messages(fn)` cross-stack.
4. **S3 → EventBridge → SQS, never S3 → SQS direct** in a micro-stack architecture. S3 event notification to a queue in another stack works but has awkward bucket-policy auto-mutation. EventBridge gives archive + replay + filter for free and is the idiomatic path.

### 5.2 `QueueStack` — queues that will receive events

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration, RemovalPolicy,
    aws_sqs as sqs,
    aws_kms as kms,
    aws_iam as iam,
)
from constructs import Construct


class QueueStack(cdk.Stack):
    """SQS queues. Each has a DLQ. Resource policies are STATIC (no cross-stack Ref)."""

    def __init__(
        self,
        scope: Construct,
        audio_data_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-queue", **kwargs)

        def _make(name: str, fifo: bool = False, timeout_s: int = 180) -> tuple[sqs.Queue, sqs.Queue]:
            dlq = sqs.Queue(
                self, f"{name}Dlq",
                queue_name=f"{{project_name}}-{name}-dlq{'.fifo' if fifo else ''}",
                fifo=fifo or None,
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=audio_data_key,
                retention_period=Duration.days(14),
            )
            q = sqs.Queue(
                self, name.title().replace("-", "") + "Queue",
                queue_name=f"{{project_name}}-{name}{'.fifo' if fifo else ''}",
                fifo=fifo or None,
                content_based_deduplication=fifo or None,
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=audio_data_key,
                visibility_timeout=Duration.seconds(timeout_s),
                dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
            )
            return q, dlq

        self.audio_ingest_queue,   self.audio_ingest_dlq   = _make("audio-ingest")
        self.transcribe_request_q, self.transcribe_req_dlq = _make("transcribe-request")
        self.bedrock_analysis_q,   self.bedrock_dlq        = _make("bedrock-analysis", fifo=True)
        self.insight_store_q,      self.insight_store_dlq  = _make("insight-store")
        self.notify_q,             self.notify_dlq         = _make("notify")
        self.dlq_reprocess_q,      self.reprocess_parent_dlq = _make("dlq-reprocess")

        # Allow EventBridge to SendMessage to ingest queue, scoped to THIS account's
        # default bus rules (static-ARN condition, no cross-stack Ref).
        self.audio_ingest_queue.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowEventBridgeRulesToSend",
            actions=["sqs:SendMessage"],
            principals=[iam.ServicePrincipal("events.amazonaws.com")],
            resources=[self.audio_ingest_queue.queue_arn],
            conditions={
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:events:{self.region}:{self.account}:rule/default/*"
                }
            },
        ))

        cdk.CfnOutput(self, "AudioIngestQueueUrl",  value=self.audio_ingest_queue.queue_url)
        cdk.CfnOutput(self, "AudioIngestQueueArn",  value=self.audio_ingest_queue.queue_arn)
```

### 5.3 `EventStack` — custom bus + rules using L1 CfnRule for cross-stack targets

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_events as events,
    aws_sqs as sqs,
)
from constructs import Construct


class EventStack(cdk.Stack):
    """EventBridge custom bus + archive + rules.

    For cross-stack targets (queues in QueueStack), we use L1 CfnRule with a
    raw target dict to avoid L2 targets.SqsQueue auto-grant which injects a
    queue resource policy referencing the rule ARN -- creating a circular
    cross-stack export. Permissions are granted via static-ARN policies
    attached in QueueStack.
    """

    def __init__(
        self,
        scope: Construct,
        audio_ingest_queue: sqs.IQueue,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-event", **kwargs)

        # Custom bus
        self.bus = events.EventBus(
            self, "DomainBus",
            event_bus_name=f"{{project_name}}-domain-bus",
        )

        # Archive on the custom bus (enables event replay)
        self.bus.archive(
            "DomainArchive",
            archive_name=f"{{project_name}}-domain-archive",
            retention=Duration.days(30),
            event_pattern=events.EventPattern(source=events.Match.prefix("")),
        )

        # Rule: S3 ObjectCreated on default bus -> audio_ingest_queue (cross-stack)
        # L1 CfnRule skips the L2 auto-grant that would create a cycle.
        events.CfnRule(
            self, "S3ObjectCreatedRule",
            event_bus_name="default",
            state="ENABLED",
            event_pattern={
                "source": ["aws.s3"],
                "detail-type": ["Object Created"],
                "detail": {"bucket": {"name": [{"prefix": "{project_name}-"}]}},
            },
            targets=[
                events.CfnRule.TargetProperty(
                    arn=audio_ingest_queue.queue_arn,
                    id="IngestQueueTarget",
                )
            ],
        )

        # Rule: Transcribe Job State Change -> CloudWatch Logs (same-stack L2 OK)
        log_group_rule = events.Rule(
            self, "TranscribeJobStateRule",
            event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
            event_pattern=events.EventPattern(
                source=["aws.transcribe"],
                detail_type=["Transcribe Job State Change"],
                detail={"TranscriptionJobStatus": ["COMPLETED", "FAILED"]},
            ),
        )
        # Target in same stack (the bus itself) -> L2 OK
        from aws_cdk import aws_events_targets as targets_
        log_group_rule.add_target(targets_.EventBus(self.bus))

        cdk.CfnOutput(self, "DomainBusName", value=self.bus.event_bus_name)
        cdk.CfnOutput(self, "DomainBusArn",  value=self.bus.event_bus_arn)
```

### 5.4 Consumer `ComputeStack` — identity-side SQS grants

See `LAYER_BACKEND_LAMBDA.md` §4.2 for the full ComputeStack pattern. Key excerpts:

```python
# Attach SQS event source WITHOUT L2 grant_consume_messages. Use identity policy.
from aws_cdk import aws_lambda_event_sources as les


def _sqs_grant(fn, queue, actions):
    fn.add_to_role_policy(iam.PolicyStatement(actions=actions, resources=[queue.queue_arn]))


_sqs_grant(self.router_fn, audio_ingest_queue, [
    "sqs:ReceiveMessage", "sqs:DeleteMessage",
    "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility",
])

# Event source mapping itself is OK cross-stack (no policy mutation).
self.router_fn.add_event_source(les.SqsEventSource(
    audio_ingest_queue,
    batch_size=10,
    max_batching_window=Duration.seconds(5),
    report_batch_item_failures=True,
))
```

### 5.5 Micro-stack gotchas

- **`CfnRule.TargetProperty`** field names use CFN camelCase underneath but CDK exposes snake_case Python — always check with `cdk synth` → inspect `cdk.out/<stack>.template.json` when in doubt.
- **Kinesis** is less common in micro-stack splits because consumers usually want the stream's ARN for event-source mapping *and* for its KMS key decrypt. If you split, keep the consumer Lambda in the same stack as the stream — or do KMS decrypt identity-side per `LAYER_BACKEND_LAMBDA.md`.
- **SNS cross-region** fan-out: use FIFO topics only when ordering matters; FIFO is single-region.

---

## 6. DLQ + redrive pattern (both variants)

```python
# On consumer Lambda: add an OnFailure destination so partial batch failures
# that drain past maxReceiveCount land in the DLQ with context.
from aws_cdk import aws_lambda_destinations as dest


self.router_fn.configure_async_invoke(
    on_failure=dest.SqsDestination(self.dlq_reprocess_q),
    retry_attempts=2,
    max_event_age=Duration.hours(6),
)

# Reprocessor Lambda pulls from DLQ, inspects, requeues valid items.
# See backend/lambdas/dlq_reprocessor/handler.py for reference.
```

---

## 7. Worked example — synth both variants

```python
"""Verify monolith and micro-stack event wiring both compile."""
import aws_cdk as cdk
from aws_cdk import aws_kms as kms
from aws_cdk.assertions import Template


def test_micro_stack_event_wiring_has_no_cycles():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    from infrastructure.cdk.stacks.queue_stack import QueueStack
    from infrastructure.cdk.stacks.event_stack import EventStack

    sec = cdk.Stack(app, "Sec", env=env)
    key = kms.Key(sec, "AudioDataKey")

    queues = QueueStack(app, audio_data_key=key, env=env)
    events_stack = EventStack(app, audio_ingest_queue=queues.audio_ingest_queue, env=env)

    # If a cycle existed, this would raise during synth.
    Template.from_stack(queues)
    Template.from_stack(events_stack)
```

---

## 8. References

- `docs/template_params.md` — `EB_CUSTOM_BUS_NAME`, `SQS_DLQ_MAX_RECEIVE_COUNT`, `SQS_VISIBILITY_TIMEOUT_MULTIPLIER`
- `docs/Feature_Roadmap.md` — features M-01..M-14, E-01..E-11
- AWS docs: [EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/), [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/), [Kinesis](https://docs.aws.amazon.com/streams/latest/dev/introduction.html)
- Related SOPs: `LAYER_BACKEND_LAMBDA` (consumers), `LAYER_DATA` (downstream persistence), `LAYER_SECURITY` (KMS key policies)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP rewrite. Micro-Stack variant uses L1 `CfnRule` + static-ARN queue policies to prevent cross-stack circular exports. Codified the four non-negotiables (§5.1). Added QueueStack / EventStack worked examples. |
| 1.0 | 2026-03-05 | Initial partial with L2 `targets.SqsQueue`, implicit auto-grants. |
