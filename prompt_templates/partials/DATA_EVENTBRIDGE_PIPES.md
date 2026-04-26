# SOP — EventBridge Pipes (DB-stream → enrich → S3/Firehose/SFN ingest)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · EventBridge Pipes (GA 2022, ongoing enhancements 2024-2026) · Sources: DynamoDB Streams, Kinesis Data Streams, MSK / self-managed Kafka, Confluent Cloud, SQS, Amazon MQ · Enrichment: Lambda / Step Functions Express / API destinations / API Gateway · Targets: 14+ AWS services including S3 (via Firehose), Lambda, SFN, EventBridge Bus, Glue Workflows, Redshift Data API, SageMaker Pipelines, Kinesis, SQS, SNS, Batch

---

## 1. Purpose

- Codify the **EventBridge Pipes pattern** as the modern replacement for "DynamoDB Stream → Lambda → SQS → Lambda → S3" glue pipelines. One Pipe = source + filter + (optional) enrichment + target. No Lambda in the middle for routing/filtering.
- Provide the **DB-stream → S3 lakehouse** pattern: DDB Stream → Pipe (filter PII → enrich with reference data → format Parquet) → Firehose → S3 raw zone.
- Provide the **Kinesis CDC → SFN orchestration** pattern: Kinesis Data Stream populated by DMS → Pipe (filter by table) → Step Functions Express (per-table workflow) → Iceberg MERGE.
- Provide the **MSK Kafka → Athena** pattern: MSK topic → Pipe (filter event-type) → Firehose with dynamic partitioning → S3 → Glue crawler → Athena.
- Codify the **filter expression** language (subset of EventBridge event pattern matching) — `$.dynamodb.NewImage.status.S` etc.
- Codify the **enrichment patterns** (synchronous Lambda invoke vs SFN Express vs API destination) and their 6 MB / 5-min / 30-sec limits.
- Codify the **batch + parallelism** controls: batch_size, maximum_batching_window_in_seconds, parallelization_factor.

When the SOW signals: "DDB Stream to lakehouse", "Kinesis to S3 with filtering", "MSK topic to Iceberg", "replace our Lambda glue with managed flow", "filter events without compute", "enrich events with reference data".

---

## 2. Decision — when to use a Pipe vs alternatives

| Need | Use | Why |
|---|---|---|
| DDB Stream → S3 (with PII filter) | **Pipe** | One resource; filter is JSON pattern; no Lambda code |
| DDB Stream → multiple targets | EventBridge Bus + rules | Pipes have ONE target; for fan-out use Bus |
| Kinesis → Firehose (no filter, no transform) | **Direct Kinesis-to-Firehose** | Skip Pipes; no value-add |
| Kinesis → S3 + dynamic partition by `event_type` | Firehose with dynamic partitioning OR Pipe + Firehose | Pipes when you need filter; Firehose alone if just routing |
| Kinesis → S3 + per-record transform > 6 MB output | **NOT Pipe** | Use Kinesis Data Analytics / Flink |
| MSK → S3 (raw landing) | **Pipe** with Firehose target | Pipes auto-scale Kafka consumers |
| SQS → SFN per-message | **Pipe** | Beats SFN's polling SQS source for ordering |
| RDS Multi-AZ writes → S3 | **NO direct source** | DBs are NOT pipe sources. Use DMS → Kinesis → Pipe, or DDB Stream replica via zero-ETL |
| Cross-account event routing | **Pipe** | Source in account A, target in account B (with resource policy) |
| Real-time fraud detection (sub-second) | **NOT Pipe** | Pipe latency 200-500 ms; for sub-second use Kinesis + Lambda directly |

### 2.1 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| Single-stack POC; source DDB table + Pipe + target Firehose all in one stack | **§3 Monolith Variant** |
| `DataStack` owns DDB + Stream + Firehose; `IngestionStack` owns the Pipe | **§4 Micro-Stack Variant** |

**Why the split matters.** Pipes need an IAM service role with `dynamodb:DescribeStream` + `dynamodb:GetRecords` on a specific stream ARN, and `firehose:PutRecord*` on a specific delivery stream ARN. Cross-stack, `stream.grant_read(pipe_role)` mutates the stream's resource policy. Identity-side via SSM-published ARNs avoids this.

---

## 3. Monolith Variant — DDB Stream → Pipe (filter+enrich) → Firehose → S3

### 3.1 Architecture

```
   DDB Table (MyOrders)
        │
        │  Stream view: NEW_AND_OLD_IMAGES
        │
        ▼
   ┌──────────────────────────────────────────────────────────┐
   │  EventBridge Pipe: orders-to-lakehouse                   │
   │                                                            │
   │  Source:    DDB Stream                                    │
   │  Filter:    {dynamodb: {NewImage: {                       │
   │                status: {S: ["completed", "shipped"]}      │
   │             }}}                                            │
   │  Enrich:    Lambda (lookup customer tier from RDS)        │
   │             - timeout 30s, max payload 6 MB out           │
   │  Target:    Firehose delivery stream                      │
   │             - input_template= JSON to Parquet schema      │
   └──────────────────────────────────────────────────────────┘
        │
        ▼
   Firehose delivery stream → S3 raw/orders/year=YYYY/month=MM/day=DD/
        - Buffer: 60 s OR 5 MB
        - Format conversion: JSON → Parquet (Glue schema)
        - Compression: SNAPPY
        - KMS encryption: account default
        ↓
   Glue crawler (hourly) → Athena (workgroup: analyst-only)
```

### 3.2 CDK — `_create_ddb_pipe_to_firehose()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_kinesisfirehose as firehose,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_pipes as pipes,                  # L1 only — Pipes not yet L2
    aws_s3 as s3,
)


def _create_ddb_pipe_to_firehose(self, stage: str) -> None:
    """Monolith variant. DDB Stream → Pipe (filter + Lambda enrich) →
    Firehose → S3 raw zone with Parquet conversion via Glue schema."""

    # A) DDB table with Stream enabled
    self.orders_table = ddb.Table(
        self, "OrdersTable",
        table_name=f"{{project_name}}-orders-{stage}",
        partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
        stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=self.kms_key,
        removal_policy=(RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY),
    )

    # B) Enrichment Lambda — adds customer.tier from a lookup table
    enrich_fn = lambda_.Function(
        self, "EnrichFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "enrich_orders")),
        timeout=Duration.seconds(30),               # Pipes ENRICHMENT max 30s
        memory_size=512,
    )
    self.kms_key.grant_decrypt(enrich_fn)
    # enrich_fn reads customer_tier from a separate lookup table or RDS
    # (cross-stack here would need IAM; assume same stack)

    # C) Firehose target — Parquet output to S3
    fh_role = iam.Role(self, "FhRole",
        assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
        permissions_boundary=self.permission_boundary)
    self.raw_bucket.grant_read_write(fh_role)
    self.kms_key.grant_encrypt_decrypt(fh_role)
    # Glue grant — needed for schema lookup at conversion time
    fh_role.add_to_policy(iam.PolicyStatement(
        actions=["glue:GetTableVersions", "glue:GetTable", "glue:GetDatabase"],
        resources=[f"arn:aws:glue:{self.region}:{self.account}:catalog",
                   f"arn:aws:glue:{self.region}:{self.account}:database/lakehouse_raw",
                   f"arn:aws:glue:{self.region}:{self.account}:table/lakehouse_raw/orders_raw"],
    ))

    self.fh_stream = firehose.CfnDeliveryStream(
        self, "FhStream",
        delivery_stream_name=f"{{project_name}}-orders-fh-{stage}",
        delivery_stream_type="DirectPut",                     # Pipe puts directly
        extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
            bucket_arn=self.raw_bucket.bucket_arn,
            role_arn=fh_role.role_arn,
            buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                interval_in_seconds=60,
                size_in_m_bs=5,
            ),
            compression_format="UNCOMPRESSED",   # Parquet handles its own compression
            prefix="orders/year=!{partitionKeyFromQuery:year}/"
                   "month=!{partitionKeyFromQuery:month}/"
                   "day=!{partitionKeyFromQuery:day}/",
            error_output_prefix="errors/orders/!{firehose:error-output-type}/"
                                "year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/",
            encryption_configuration=firehose.CfnDeliveryStream.EncryptionConfigurationProperty(
                kms_encryption_config=firehose.CfnDeliveryStream.KMSEncryptionConfigProperty(
                    awskms_key_arn=self.kms_key.key_arn,
                ),
            ),
            data_format_conversion_configuration=firehose.CfnDeliveryStream.DataFormatConversionConfigurationProperty(
                enabled=True,
                input_format_configuration=firehose.CfnDeliveryStream.InputFormatConfigurationProperty(
                    deserializer=firehose.CfnDeliveryStream.DeserializerProperty(
                        open_x_json_ser_de=firehose.CfnDeliveryStream.OpenXJsonSerDeProperty(),
                    ),
                ),
                output_format_configuration=firehose.CfnDeliveryStream.OutputFormatConfigurationProperty(
                    serializer=firehose.CfnDeliveryStream.SerializerProperty(
                        parquet_ser_de=firehose.CfnDeliveryStream.ParquetSerDeProperty(
                            compression="SNAPPY",
                        ),
                    ),
                ),
                schema_configuration=firehose.CfnDeliveryStream.SchemaConfigurationProperty(
                    catalog_id=self.account,
                    database_name="lakehouse_raw",
                    table_name="orders_raw",
                    region=self.region,
                    role_arn=fh_role.role_arn,
                    version_id="LATEST",
                ),
            ),
            dynamic_partitioning_configuration=firehose.CfnDeliveryStream.DynamicPartitioningConfigurationProperty(
                enabled=True,
                retry_options=firehose.CfnDeliveryStream.RetryOptionsProperty(
                    duration_in_seconds=300,
                ),
            ),
            processing_configuration=firehose.CfnDeliveryStream.ProcessingConfigurationProperty(
                enabled=True,
                processors=[firehose.CfnDeliveryStream.ProcessorProperty(
                    type="MetadataExtraction",
                    parameters=[
                        firehose.CfnDeliveryStream.ProcessorParameterProperty(
                            parameter_name="MetadataExtractionQuery",
                            parameter_value="{year:.timestamp[0:4],month:.timestamp[5:7],day:.timestamp[8:10]}",
                        ),
                        firehose.CfnDeliveryStream.ProcessorParameterProperty(
                            parameter_name="JsonParsingEngine",
                            parameter_value="JQ-1.6",
                        ),
                    ],
                )],
            ),
        ),
    )

    # D) Pipe service role
    pipe_role = iam.Role(self, "PipeRole",
        assumed_by=iam.ServicePrincipal("pipes.amazonaws.com"),
        permissions_boundary=self.permission_boundary)
    # Source: DDB Stream
    pipe_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "dynamodb:DescribeStream",
            "dynamodb:GetRecords",
            "dynamodb:GetShardIterator",
            "dynamodb:ListStreams",
        ],
        resources=[self.orders_table.table_stream_arn],
    ))
    self.kms_key.grant_decrypt(pipe_role)
    # Enrichment: invoke Lambda
    enrich_fn.grant_invoke(pipe_role)
    # Target: write to Firehose
    pipe_role.add_to_policy(iam.PolicyStatement(
        actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
        resources=[self.fh_stream.attr_arn],
    ))

    # E) The Pipe itself
    self.pipe = pipes.CfnPipe(
        self, "OrdersPipe",
        name=f"{{project_name}}-orders-pipe-{stage}",
        role_arn=pipe_role.role_arn,
        source=self.orders_table.table_stream_arn,
        source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
            dynamo_db_stream_parameters=pipes.CfnPipe.PipeSourceDynamoDBStreamParametersProperty(
                starting_position="LATEST",                  # or TRIM_HORIZON for replay
                batch_size=100,                              # max 1000 for DDB Stream
                maximum_batching_window_in_seconds=10,       # micro-batch window
                parallelization_factor=4,                    # workers per shard
                maximum_record_age_in_seconds=86400,         # 24h
                maximum_retry_attempts=3,
                on_partial_batch_item_failure="AUTOMATIC_BISECT",
                dead_letter_config=pipes.CfnPipe.DeadLetterConfigProperty(
                    arn=self.dlq.queue_arn,
                ),
            ),
            filter_criteria=pipes.CfnPipe.FilterCriteriaProperty(
                filters=[pipes.CfnPipe.FilterProperty(
                    pattern=json.dumps({
                        "dynamodb": {
                            "NewImage": {
                                "status": {"S": ["completed", "shipped"]}
                            }
                        }
                    }),
                )],
            ),
        ),
        enrichment=enrich_fn.function_arn,
        enrichment_parameters=pipes.CfnPipe.PipeEnrichmentParametersProperty(
            input_template=None,                              # raw event in
            # http_parameters not used — Lambda direct invoke
        ),
        target=self.fh_stream.attr_arn,
        target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
            kinesis_stream_parameters=None,                   # Not Kinesis here
            # For Firehose target, no specific parameters — body is the event
        ),
        log_configuration=pipes.CfnPipe.PipeLogConfigurationProperty(
            level="INFO",
            include_execution_data=["ALL"],                   # for debugging
            cloudwatch_logs_log_destination=pipes.CfnPipe.CloudwatchLogsLogDestinationProperty(
                log_group_arn=self.pipe_log_group.log_group_arn,
            ),
        ),
    )
    # Required: dlq queue grants pipe role write
    self.dlq.grant_send_messages(pipe_role)
```

### 3.3 Filter expression cookbook

EventBridge Pipes use a **subset** of EventBridge event pattern matching. Common patterns:

```jsonc
// Match exact value
{"dynamodb": {"NewImage": {"status": {"S": ["completed"]}}}}

// Match any of multiple values
{"dynamodb": {"NewImage": {"status": {"S": ["completed", "shipped"]}}}}

// Match prefix
{"dynamodb": {"NewImage": {"customer_id": {"S": [{"prefix": "ENT-"}]}}}}

// Numeric comparison (only on top-level "data" field for non-DDB sources;
// for DDB use string comparison after type coercion in enrichment)
{"detail": {"amount": [{"numeric": [">", 100]}]}}

// Match exists (field is present)
{"dynamodb": {"NewImage": {"refund_id": [{"exists": true}]}}}

// Match anything but
{"dynamodb": {"NewImage": {"status": {"S": [{"anything-but": ["draft"]}]}}}}

// IP address match (for VPC flow logs / API access logs sources)
{"sourceIPAddress": [{"cidr": "10.0.0.0/8"}]}
```

**Limit:** filter expression total size ≤ 4 KB. Long-list filters (`IN (1000 values)`) won't fit — push to enrichment Lambda or upstream.

### 3.4 Enrichment Lambda — payload format

```python
# enrich_orders/index.py

def handler(events, context):
    """Pipes invokes synchronously. Input: list of source events
    (already filter-passed). Output: list of same length, possibly
    augmented. Empty list = drop all. Per-element None = drop that one."""
    out = []
    for ev in events:
        new_image = ev["dynamodb"]["NewImage"]
        customer_id = new_image["customer_id"]["S"]

        # Lookup tier from cached source (Lambda layer / DynamoDB / RDS)
        tier = lookup_customer_tier(customer_id)

        # Mutate / augment
        new_image["customer_tier"] = {"S": tier}
        new_image["enriched_at"]   = {"S": datetime.utcnow().isoformat()}

        # OPTIONAL: drop if customer is internal-test
        if tier == "internal":
            continue

        out.append(ev)
    return out
```

### 3.5 Step Functions Express enrichment (when 30s Lambda is too short)

Use SFN Express when enrichment needs:
- Multiple downstream API calls (Bedrock + RDS lookup + S3 read)
- Conditional branches (different enrichment per event type)
- Retry-with-backoff per step

```python
# Pipe with SFN Express target
self.pipe_with_sfn = pipes.CfnPipe(
    self, "EnrichWithSfnPipe",
    role_arn=pipe_role.role_arn,
    source=stream_arn,
    enrichment=sfn_express_state_machine.state_machine_arn,
    target=fh_stream.attr_arn,
    enrichment_parameters=pipes.CfnPipe.PipeEnrichmentParametersProperty(
        input_template=None,                 # Source event passed as-is
    ),
    # SFN Express max duration: 5 min (vs Lambda 30s in pipes context)
)
pipe_role.add_to_policy(iam.PolicyStatement(
    actions=["states:StartSyncExecution"],   # SYNC for pipe enrichment
    resources=[sfn_express_state_machine.state_machine_arn],
))
```

---

## 4. Kinesis CDC → SFN orchestration variant

When DMS lands CDC events in a Kinesis Data Stream, use Pipes to per-table-route them:

```python
# Source: Kinesis stream populated by DMS
# Filter: by table-name (e.g. "orders" only)
# Target: per-table SFN workflow

orders_pipe = pipes.CfnPipe(
    self, "OrdersCdcPipe",
    role_arn=pipe_role.role_arn,
    source=kinesis_stream.stream_arn,
    source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
        kinesis_stream_parameters=pipes.CfnPipe.PipeSourceKinesisStreamParametersProperty(
            starting_position="LATEST",
            batch_size=500,
            maximum_batching_window_in_seconds=30,
            parallelization_factor=8,
            on_partial_batch_item_failure="AUTOMATIC_BISECT",
            dead_letter_config=pipes.CfnPipe.DeadLetterConfigProperty(arn=dlq.queue_arn),
        ),
        filter_criteria=pipes.CfnPipe.FilterCriteriaProperty(
            filters=[pipes.CfnPipe.FilterProperty(
                pattern=json.dumps({
                    "data": {
                        "metadata": {
                            "table-name": ["orders"],
                            "schema-name": ["public"],
                        }
                    }
                }),
            )],
        ),
    ),
    target=orders_iceberg_merge_sfn.state_machine_arn,
    target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
        step_function_state_machine_parameters=pipes.CfnPipe.PipeTargetStateMachineParametersProperty(
            invocation_type="FIRE_AND_FORGET",                # async; SFN handles retry
        ),
    ),
)
```

The SFN Express workflow then runs an Athena `MERGE INTO curated.orders ... USING raw_cdc.orders ...` per batch. See `DATA_LAKEHOUSE_ICEBERG` §4 for the MERGE pattern.

---

## 5. Micro-Stack variant (cross-stack via SSM)

```python
# In DataStack — owner of source + target
ssm.StringParameter(self, "DdbStreamArn",
    parameter_name=f"/{{project_name}}/{stage}/data/orders-stream-arn",
    string_value=self.orders_table.table_stream_arn)
ssm.StringParameter(self, "FhStreamArn",
    parameter_name=f"/{{project_name}}/{stage}/data/orders-fh-arn",
    string_value=self.fh_stream.attr_arn)
ssm.StringParameter(self, "DlqArn",
    parameter_name=f"/{{project_name}}/{stage}/data/orders-pipe-dlq",
    string_value=self.dlq.queue_arn)

# In IngestionStack — owns the Pipe + enrichment
stream_arn  = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/data/orders-stream-arn")
fh_arn      = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/data/orders-fh-arn")

# Pipe role grants ITSELF identity-side on the SSM-resolved ARNs
pipe_role.add_to_policy(iam.PolicyStatement(
    actions=["dynamodb:DescribeStream", "dynamodb:GetRecords",
             "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
    resources=[stream_arn],
))
pipe_role.add_to_policy(iam.PolicyStatement(
    actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
    resources=[fh_arn],
))
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Pipe stuck in CREATE_FAILED | IAM service role missing source perms | Pipe service role needs both `kms:Decrypt` (if source is encrypted) and source-specific perms (`dynamodb:GetRecords` / `kinesis:GetRecords` / etc.) |
| Filter not matching expected events | Filter pattern syntax wrong | Pipes filter syntax = EventBridge subset. Test in Pipes console "Test pattern" before deploying |
| Enrichment Lambda timing out | Default 5s timeout | Pipe enrichment max is 30s for Lambda, 5 min for SFN Express. Increase Lambda timeout in CDK to 30s explicitly |
| Target Firehose getting 0-record batches | Filter rejects all events | Check CloudWatch metrics `pipe.target.invocations` vs `pipe.source.records-received` |
| DLQ filling fast | Enrichment errors or target throttling | Inspect `messageId.original` in DLQ message; reprocess via redrive policy |
| "Pipe couldn't subscribe to source" | Source-resource policy missing | DDB Stream needs no resource policy; Kinesis stream / MSK topic / SQS queue may need cross-account allow |
| Throughput plateaus despite parallelization_factor | Source shard count is the limit | DDB Stream auto-shards; Kinesis is capped at provisioned shard count. Increase shards or switch to On-Demand |
| Filter expression > 4 KB | Too many literal values in `["a","b",...]` | Push filtering into enrichment Lambda or upstream source |

### 6.1 Pipes vs alternatives

| Need | Pipes | EventBridge Bus | Kinesis Firehose direct | Lambda glue |
|---|---|---|---|---|
| 1 source → 1 target with filter+enrich | ✅ **best** | ❌ no enrich | ❌ no filter | ⚠️ glue |
| 1 source → many targets | ❌ Pipes is 1:1 | ✅ **best** | ❌ | ⚠️ glue |
| Sub-second latency | ⚠️ ~200ms | ⚠️ ~100ms | ❌ buffered | ✅ |
| > 6 MB enrichment output | ❌ | ❌ | ⚠️ batch only | ✅ |
| State across events | ❌ stateless | ❌ stateless | ❌ | ✅ |
| Cross-account | ✅ | ✅ | ❌ | ⚠️ |
| Cost (per million events) | ~$0.40 | ~$1.00 | ~$0.20 | ~$0.20-$2 (Lambda time) |

---

## 7. Worked example — pytest synth harness

```python
"""SOP verification — DdbPipeToFirehoseStack contains DDB stream, Pipe,
Firehose with Parquet conversion, IAM roles, DLQ."""
import json
import aws_cdk as cdk
from aws_cdk import aws_dynamodb as ddb, aws_iam as iam, aws_kms as kms, aws_s3 as s3
from aws_cdk.assertions import Template, Match


def test_ddb_pipe_to_firehose_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    deps = cdk.Stack(app, "Deps", env=env)
    key = kms.Key(deps, "Key")
    raw = s3.Bucket(deps, "Raw", encryption_key=key)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ddb_pipe_stack import DdbPipeStack
    stack = DdbPipeStack(
        app, stage_name="dev",
        kms_key=key, raw_bucket=raw, permission_boundary=boundary,
        env=env,
    )
    t = Template.from_stack(stack)

    # DDB Table with stream
    t.has_resource_properties("AWS::DynamoDB::Table", Match.object_like({
        "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
    }))
    # Pipe
    t.has_resource_properties("AWS::Pipes::Pipe", Match.object_like({
        "SourceParameters": Match.object_like({
            "DynamoDBStreamParameters": Match.object_like({
                "StartingPosition": "LATEST",
                "BatchSize":        100,
            }),
            "FilterCriteria": Match.object_like({
                "Filters": Match.array_with([Match.object_like({
                    "Pattern": Match.string_like_regexp(r'.*completed.*shipped.*'),
                })]),
            }),
        }),
    }))
    # Firehose with Parquet conversion
    t.has_resource_properties("AWS::KinesisFirehose::DeliveryStream", Match.object_like({
        "ExtendedS3DestinationConfiguration": Match.object_like({
            "DataFormatConversionConfiguration": Match.object_like({
                "Enabled": True,
                "OutputFormatConfiguration": Match.object_like({
                    "Serializer": Match.object_like({
                        "ParquetSerDe": Match.object_like({"Compression": "SNAPPY"}),
                    }),
                }),
            }),
            "DynamicPartitioningConfiguration": Match.object_like({"Enabled": True}),
        }),
    }))
    # DLQ
    t.resource_count_is("AWS::SQS::Queue", Match.greater_than_or_equal(1))
```

---

## 8. Five non-negotiables

1. **Always set a DLQ.** Pipes silently drop records on persistent errors otherwise. `dead_letter_config.arn` should point to an SQS queue with 14-day retention. Wire CloudWatch alarm on queue depth > 0.

2. **Filter early, enrich sparingly.** The filter is free; the enrichment Lambda costs ~$0.20 per million invocations + Lambda runtime. Filter out 80% of events with the JSON pattern before they reach enrichment.

3. **`maximum_batching_window_in_seconds` matters more than `batch_size`.** Default is 0 (no waiting); this can spike Lambda concurrency. For DDB streams set 10s; for high-throughput Kinesis set 30s; for SQS standard set 5s.

4. **Pipe service role grants are identity-side, scoped to specific ARNs.** Never `stream.grant_read(pipe_role)` cross-stack — produces cyclic exports the same way Aurora secrets do. Always SSM-publish ARNs.

5. **Log to CloudWatch with `level=INFO` in dev, `ERROR` in prod.** Pipes logs include source record + filter-pass + enrichment-output for debugging. INFO in prod inflates cost and may log PII.

---

## 9. References

- `docs/template_params.md` — `PIPE_BATCH_SIZE`, `PIPE_BATCHING_WINDOW_SEC`, `PIPE_PARALLELIZATION_FACTOR`, `PIPE_RETRY_ATTEMPTS`, `FIREHOSE_BUFFER_INTERVAL_SEC`, `FIREHOSE_BUFFER_SIZE_MB`
- `docs/Feature_Roadmap.md` — `EBP-01` (DDB pipe), `EBP-02` (Kinesis CDC pipe), `EBP-03` (MSK pipe), `EBP-04` (SFN Express enrichment), `EBP-05` (Iceberg MERGE downstream)
- AWS docs:
  - [EventBridge Pipes concepts](https://docs.aws.amazon.com/eventbridge/latest/userguide/pipes-concepts.html)
  - [Pipe enrichment](https://docs.aws.amazon.com/eventbridge/latest/userguide/pipes-enrichment.html)
  - [Kinesis Firehose dynamic partitioning](https://docs.aws.amazon.com/firehose/latest/dev/dynamic-partitioning.html)
  - [Apache Kafka pipe source](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-pipes-kafka.html)
  - [SQS pipe source](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-pipes-sqs.html)
- Related SOPs:
  - `DATA_DMS_REPLICATION` — populates Kinesis stream that feeds Pipes for CDC fan-out
  - `DATA_LAKEHOUSE_ICEBERG` — MERGE pattern for downstream Iceberg from CDC
  - `DATA_GLUE_CATALOG` — Glue table that Firehose uses for Parquet schema
  - `EVENT_DRIVEN_PATTERNS` — comparison of Pipes vs Bus vs direct integrations
  - `LAYER_OBSERVABILITY` — CloudWatch alarms on pipe-DLQ depth, source-records-received

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — covers DDB Streams / Kinesis / MSK / SQS source patterns with filter expressions, Lambda + SFN Express enrichment, Firehose / SFN / EventBridge Bus / Glue Workflow targets. CDK monolith + micro-stack with SSM cross-stack contract. Filter expression cookbook with 6 common patterns. Enrichment Lambda payload format + drop-by-returning-empty semantics. Decision matrix vs EventBridge Bus / Firehose direct / Lambda glue. 5 non-negotiables incl. DLQ + filter-early. Pytest synth harness. Created to fill F369 data-ecosystem audit gap (2026-04-26): EventBridge Pipes was 0% covered despite being the modern replacement for Lambda glue in DB-stream → S3 pipelines. |
