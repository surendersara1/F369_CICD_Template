# PARTIAL: Event-Driven Architecture — SNS, SQS Advanced, EventBridge, Kinesis

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` when SOW contains decoupling/async/event-driven patterns.

---

## When to Include Each Service

| SOW Signal                                              | Include                |
| ------------------------------------------------------- | ---------------------- |
| "decouple", "async", "fan-out", "pub/sub"               | SNS → SQS Fan-out      |
| "ordered processing", "exactly-once", "FIFO"            | SQS FIFO Queue         |
| "event bus", "domain events", "microservice events"     | EventBridge Custom Bus |
| "streaming", "high-throughput events", ">1k events/sec" | Kinesis Data Streams   |
| "S3 to pipeline", "file arrives → process"              | S3 Event Notifications |
| "retry", "dead letter", "poison message"                | DLQ + Redrive Policy   |
| "delay", "scheduled retry", "backoff"                   | SQS Delay Queue        |

---

## PATTERN A: SNS → SQS Fan-out (Pub/Sub)

The most important decoupling pattern. One publisher (SNS Topic) fans out to
multiple subscribers (SQS Queues), each triggering a different Lambda/ECS consumer.

```
Publisher Lambda
      │
      ▼
  SNS Topic ("order-created")
      │
   ┌──┴──────────────┬──────────────────┐
   ▼                 ▼                  ▼
SQS Queue         SQS Queue         SQS Queue
(inventory)       (email-notify)    (analytics)
   │                 │                  │
   ▼                 ▼                  ▼
Lambda            Lambda            Lambda/Firehose
```

```python
def _create_event_bus(self, stage_name: str) -> None:
    """
    Pub/Sub Fan-out: SNS Topics → multiple SQS Queue subscribers.

    Pattern: Publisher puts one SNS message → all SQS queues receive a copy.
    Each queue has its own consumer (Lambda or ECS), fully decoupled.
    """

    # =========================================================================
    # SNS TOPICS — One per domain event type
    # [Claude: generate from Architecture Map detected event types]
    # =========================================================================
    TOPIC_DEFINITIONS = [
        {
            "id": "OrderCreated",
            "name": "order-created",
            "description": "Published when a new order is placed",
            "subscribers": ["inventory-service", "email-notify", "analytics"],
        },
        {
            "id": "UserRegistered",
            "name": "user-registered",
            "description": "Published when a new user signs up",
            "subscribers": ["welcome-email", "crm-sync"],
        },
        # [Claude: add one entry per domain event from Architecture Map]
    ]

    self.sns_topics: Dict[str, sns.Topic] = {}
    self.subscriber_queues: Dict[str, sqs.Queue] = {}

    for topic_config in TOPIC_DEFINITIONS:

        # --- SNS Topic ---
        topic = sns.Topic(
            self, f"{topic_config['id']}Topic",
            topic_name=f"{{project_name}}-{topic_config['name']}-{stage_name}",
            display_name=topic_config["description"],
            # Encrypt with KMS
            master_key=self.kms_key,
        )
        self.sns_topics[topic_config["id"]] = topic

        # --- SQS Subscribers (one queue per subscriber service) ---
        for subscriber_name in topic_config["subscribers"]:

            # Dead Letter Queue for this subscriber
            dlq = sqs.Queue(
                self, f"{topic_config['id']}{subscriber_name.replace('-','').capitalize()}DLQ",
                queue_name=f"{{project_name}}-{subscriber_name}-{topic_config['name']}-dlq-{stage_name}",
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=self.kms_key,
                retention_period=Duration.days(14),
                removal_policy=RemovalPolicy.DESTROY,
            )

            # Subscriber Queue
            subscriber_queue = sqs.Queue(
                self, f"{topic_config['id']}{subscriber_name.replace('-','').capitalize()}Queue",
                queue_name=f"{{project_name}}-{subscriber_name}-{topic_config['name']}-{stage_name}",
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=self.kms_key,

                # Visibility timeout: must be >= Lambda timeout for this subscriber
                visibility_timeout=Duration.seconds(300),

                # Retention: messages not consumed within 4 days go to DLQ
                retention_period=Duration.days(4),

                # DLQ: after 3 failed processing attempts
                dead_letter_queue=sqs.DeadLetterQueue(
                    max_receive_count=3,
                    queue=dlq,
                ),

                # Receive wait time: long polling (reduces empty receive API calls = cost saving)
                receive_message_wait_time=Duration.seconds(20),

                removal_policy=RemovalPolicy.DESTROY,
            )

            # Subscribe this SQS queue to the SNS topic
            # RawMessageDelivery=True: SQS receives the raw payload, not SNS wrapper JSON
            topic.add_subscription(
                sns.subscriptions.SqsSubscription(
                    subscriber_queue,
                    raw_message_delivery=True,  # Cleaner payload for Lambda JSON parsing
                    filter_policy={
                        # Optional: filter messages by attribute
                        # "event_type": sns.SubscriptionFilter.string_filter(allowlist=["ORDER_PLACED"])
                    },
                )
            )

            queue_key = f"{topic_config['id']}_{subscriber_name}"
            self.subscriber_queues[queue_key] = subscriber_queue

            # Grant Lambda (of same name) to consume from this queue
            # [Claude: look up lambda_functions dict and grant if exists]
            service_lambda_id = subscriber_name.replace("-", "_").title().replace("_", "")
            if service_lambda_id in self.lambda_functions:
                subscriber_queue.grant_consume_messages(self.lambda_functions[service_lambda_id])
                self.lambda_functions[service_lambda_id].add_event_source(
                    lambda_events.SqsEventSource(
                        subscriber_queue,
                        batch_size=10,
                        max_batching_window=Duration.seconds(5),  # Batch for efficiency
                        report_batch_item_failures=True,          # Partial batch failure support
                    )
                )
```

---

## PATTERN B: SQS FIFO Queue (Ordered, Exactly-Once)

Use when SOW requires **ordered processing** or **exactly-once delivery**.

```python
def _create_fifo_queues(self, stage_name: str) -> None:
    """
    SQS FIFO Queues for ordered, exactly-once message processing.

    Use cases:
      - Financial transactions (process in order, never duplicate)
      - Inventory updates (prevent oversell)
      - State machine transitions (ordered state changes)

    FIFO rules:
      - MessageGroupId: messages within a group are strictly ordered
      - MessageDeduplicationId: prevents duplicate processing (5-min window)
      - Throughput: 300 msg/sec (3000 with high-throughput mode)
    """

    # FIFO DLQ (must also be FIFO)
    fifo_dlq = sqs.Queue(
        self, "FifoDLQ",
        queue_name=f"{{project_name}}-fifo-dlq-{stage_name}.fifo",  # .fifo suffix required
        fifo=True,
        content_based_deduplication=True,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
        removal_policy=RemovalPolicy.DESTROY,
    )

    # FIFO queue with high-throughput mode
    self.fifo_queue = sqs.Queue(
        self, "FifoQueue",
        queue_name=f"{{project_name}}-ordered-{stage_name}.fifo",
        fifo=True,

        # Content-based deduplication: SQS hashes body as dedup ID
        # (alternative: set MessageDeduplicationId explicitly per message)
        content_based_deduplication=True,

        # High-throughput FIFO: 3000 msg/sec per MessageGroup (vs 300 standard)
        fifo_throughput_limit=sqs.FifoThroughputLimit.PER_MESSAGE_GROUP_ID,
        deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,

        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,

        visibility_timeout=Duration.seconds(300),
        retention_period=Duration.days(4),

        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=3,
            queue=fifo_dlq,
        ),

        removal_policy=RemovalPolicy.DESTROY,
    )
```

---

## PATTERN C: EventBridge Custom Event Bus

Use for **domain events between microservices**. Unlike SNS, EventBridge supports
complex routing rules, schema registry, archive & replay, and cross-account events.

```python
def _create_event_bus(self, stage_name: str) -> None:
    """
    Custom EventBridge Event Bus for domain events.

    Architecture:
      Any service → PutEvents → Custom Bus → Rules → Targets

    Advantages over SNS:
      - Content-based routing (route by event field values, not just type)
      - Schema registry (auto-discover event schema)
      - Archive & Replay (replay past events for debugging/recovery)
      - Cross-account event routing
      - EventBridge Pipes (connect SQS→Lambda without glue code)
    """

    # Custom Event Bus (events go here, NOT to the default bus)
    self.event_bus = events.EventBus(
        self, "DomainEventBus",
        event_bus_name=f"{{project_name}}-events-{stage_name}",
    )

    # Encrypt event bus (attach resource policy)
    # [EventBridge encryption applied via KMS key policy]

    # Archive: retain all events for 30 days (1 year in prod)
    # Allows replay of any past events for debugging or re-processing
    self.event_bus.archive(
        "EventArchive",
        archive_name=f"{{project_name}}-event-archive-{stage_name}",
        description="Archive of all domain events for replay",
        event_pattern=events.EventPattern(source=events.Match.prefix("{{project_name}}")),
        retention=Duration.days(30) if stage_name != "prod" else Duration.days(365),
    )

    # =========================================================================
    # EVENT RULES — Route events to the right targets
    # [Claude: generate from Architecture Map detected event flows]
    # =========================================================================
    EVENT_RULES = [
        {
            "id": "OrderCreatedRule",
            "description": "Route order.created events to inventory + analytics",
            "source": ["{{project_name}}.orders"],
            "detail_type": ["order.created"],
            "targets": ["inventory_lambda", "analytics_firehose"],
        },
        {
            "id": "UserRegisteredRule",
            "description": "Route user.registered to CRM sync and welcome email",
            "source": ["{{project_name}}.users"],
            "detail_type": ["user.registered"],
            "targets": ["crm_sync_lambda"],
        },
    ]

    for rule_config in EVENT_RULES:
        rule = events.Rule(
            self, rule_config["id"],
            rule_name=f"{{project_name}}-{rule_config['id'].lower()}-{stage_name}",
            description=rule_config["description"],
            event_bus=self.event_bus,
            event_pattern=events.EventPattern(
                source=rule_config["source"],
                detail_type=rule_config["detail_type"],
            ),
        )

        # Add Lambda targets
        for target_id in rule_config["targets"]:
            if target_id in self.lambda_functions:
                rule.add_target(targets.LambdaFunction(
                    self.lambda_functions[target_id],
                    retry_attempts=2,
                    dead_letter_queue=self.dlq,  # Failed event deliveries → DLQ
                ))

    # =========================================================================
    # EVENTBRIDGE PIPES — SQS → Lambda/ECS without glue Lambda
    # [Native connector: polls SQS, filters, enriches, routes to target]
    # =========================================================================
    # Note: CDK L2 Pipes construct is in alpha — use CfnPipe (L1) for now
    # pipes.CfnPipe(self, "SqsToEcsPipe", ...)
    # [Claude: add if SOW requires SQS→ECS with filtering/enrichment]

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "EventBusName",
        value=self.event_bus.event_bus_name,
        description="Custom EventBridge event bus name",
        export_name=f"{{project_name}}-event-bus-{stage_name}",
    )
    CfnOutput(self, "EventBusArn",
        value=self.event_bus.event_bus_arn,
        description="EventBridge event bus ARN (for cross-service PutEvents)",
        export_name=f"{{project_name}}-event-bus-arn-{stage_name}",
    )
```

---

## PATTERN D: Kinesis Data Streams + Firehose

Use for **high-throughput event streaming** (>1,000 events/sec) or real-time analytics pipelines.

```python
def _create_kinesis_streams(self, stage_name: str) -> None:
    """
    Kinesis Data Streams for high-throughput event ingestion.

    Use cases:
      - Clickstream / user activity tracking
      - IoT sensor data
      - Application metrics at scale
      - Real-time fraud signals

    Flow:
      Producers → Kinesis Data Stream → Lambda (real-time processing)
                                      → Firehose → S3 (data lake)
                                      → Firehose → OpenSearch (search/analytics)
    """

    # Kinesis Data Stream
    # on-demand mode: auto-scales shards (simpler, slightly more expensive)
    # provisioned mode: fixed shards (cheaper at predictable throughput)
    self.event_stream = kinesis.Stream(
        self, "EventStream",
        stream_name=f"{{project_name}}-events-{stage_name}",

        # On-demand (recommended unless you know your exact shard count)
        stream_mode=kinesis.StreamMode.ON_DEMAND,

        # For provisioned mode (uncomment and set shard count):
        # stream_mode=kinesis.StreamMode.PROVISIONED,
        # shard_count=2 if stage_name != "prod" else 10,

        # Data retention: 24hr default, up to 365 days
        retention_period=Duration.hours(24) if stage_name == "dev" else Duration.days(7),

        # Encryption
        encryption=kinesis.StreamEncryption.KMS,
        encryption_key=self.kms_key,

        removal_policy=RemovalPolicy.DESTROY,
    )

    # Lambda consumer of Kinesis stream
    # [Claude: look up the correct Lambda from lambda_functions dict]
    if "StreamProcessor" in self.lambda_functions:
        self.lambda_functions["StreamProcessor"].add_event_source(
            lambda_events.KinesisEventSource(
                self.event_stream,
                starting_position=_lambda.StartingPosition.TRIM_HORIZON,
                batch_size=100,          # Up to 100 records per invocation
                max_batching_window=Duration.seconds(5),
                parallelization_factor=2,  # 2 concurrent Lambdas per shard
                bisect_batch_on_error=True,  # On failure, split batch and retry half
                report_batch_item_failures=True,
                retry_attempts=3,
                destination_config=_lambda.DestinationConfig(
                    on_failure=destinations.SqsDestination(self.dlq),
                ),
            )
        )

    # Kinesis Firehose → S3 data lake (raw event storage + Athena queryable)
    firehose_role = iam.Role(
        self, "FirehoseRole",
        assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
    )
    self.data_bucket.grant_read_write(firehose_role)
    self.event_stream.grant_read(firehose_role)
    self.kms_key.grant_encrypt_decrypt(firehose_role)

    self.firehose = firehose.CfnDeliveryStream(
        self, "EventFirehose",
        delivery_stream_name=f"{{project_name}}-event-firehose-{stage_name}",
        delivery_stream_type="KinesisStreamAsSource",

        kinesis_stream_source_configuration=firehose.CfnDeliveryStream.KinesisStreamSourceConfigurationProperty(
            kinesis_stream_arn=self.event_stream.stream_arn,
            role_arn=firehose_role.role_arn,
        ),

        extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
            bucket_arn=self.data_bucket.bucket_arn,
            role_arn=firehose_role.role_arn,

            # Partition by date for efficient Athena queries
            prefix="events/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/",
            error_output_prefix="events-errors/!{firehose:error-output-type}/",

            # Buffer before writing to S3 (reduce S3 API calls)
            buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                interval_in_seconds=60,   # Write every 60 seconds
                size_in_m_bs=5,          # OR when buffer hits 5MB
            ),

            # Compression (reduces S3 storage and Athena scan cost)
            compression_format="GZIP",

            # Encryption
            encryption_configuration=firehose.CfnDeliveryStream.EncryptionConfigurationProperty(
                kms_encryption_config=firehose.CfnDeliveryStream.KMSEncryptionConfigProperty(
                    awskms_key_arn=self.kms_key.key_arn,
                )
            ),
        ),
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "KinesisStreamName",
        value=self.event_stream.stream_name,
        description="Kinesis Data Stream name",
        export_name=f"{{project_name}}-kinesis-stream-{stage_name}",
    )
    CfnOutput(self, "KinesisStreamArn",
        value=self.event_stream.stream_arn,
        description="Kinesis Data Stream ARN (for producers)",
        export_name=f"{{project_name}}-kinesis-arn-{stage_name}",
    )

```

---

## PATTERN E: DynamoDB Streams → Lambda (Change Data Capture)

Trigger Lambda on every DynamoDB item INSERT/MODIFY/REMOVE.

```python
# After creating a DynamoDB table with stream enabled:
# stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES

# Attach Lambda trigger to DynamoDB stream
if "DdbStreamProcessor" in self.lambda_functions:
    self.lambda_functions["DdbStreamProcessor"].add_event_source(
        lambda_events.DynamoEventSource(
            self.ddb_tables["MainTable"],
            starting_position=_lambda.StartingPosition.LATEST,
            batch_size=100,
            max_batching_window=Duration.seconds(5),
            bisect_batch_on_error=True,
            report_batch_item_failures=True,
            retry_attempts=3,
            # Filter: only process INSERT and MODIFY, skip REMOVE
            filters=[
                _lambda.FilterCriteria.filter({
                    "eventName": _lambda.FilterRule.or_filter(
                        _lambda.FilterRule.is_equal("INSERT"),
                        _lambda.FilterRule.is_equal("MODIFY"),
                    )
                })
            ],
        )
    )
```

---

## PATTERN F: S3 Event Notifications → Processing Pipeline

Trigger Lambda or push to EventBridge/SQS when files arrive in S3.

```python
# Option A: S3 → Lambda directly (simple, tight coupling)
self.data_bucket.add_event_notification(
    s3.EventType.OBJECT_CREATED,
    s3_notifications.LambdaDestination(self.lambda_functions["FileProcessor"]),
    s3.NotificationKeyFilter(prefix="uploads/", suffix=".pdf"),
)

# Option B: S3 → EventBridge → Multiple targets (recommended for decoupling)
# (requires event_bridge_enabled=True on the bucket — set in LAYER_DATA.md)
# EventBridge rule:
events.Rule(
    self, "S3UploadRule",
    event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", "default"),
    event_pattern=events.EventPattern(
        source=["aws.s3"],
        detail_type=["Object Created"],
        detail={
            "bucket": {"name": [self.data_bucket.bucket_name]},
            "object": {"key": [{"prefix": "uploads/"}]},
        },
    ),
    targets=[
        targets.LambdaFunction(self.lambda_functions["VirusScanner"]),
        targets.SqsQueue(self.main_queue),  # Also enqueue for async processing
    ],
)

# Option C: S3 → SQS (for reliable at-least-once processing with DLQ)
self.data_bucket.add_event_notification(
    s3.EventType.OBJECT_CREATED,
    s3_notifications.SqsDestination(self.main_queue),
    s3.NotificationKeyFilter(prefix="uploads/"),
)
```
