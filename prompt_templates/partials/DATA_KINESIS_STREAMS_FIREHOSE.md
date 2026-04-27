# SOP — Kinesis Data Streams + Firehose (on-demand vs provisioned · enhanced fan-out · dynamic partitioning · Lambda transform · S3/OS/Redshift sinks)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Kinesis Data Streams (KDS) on-demand + provisioned · Enhanced Fan-Out (EFO) · Kinesis Data Firehose (KDF) · Lambda transform · Dynamic partitioning · S3 / OpenSearch Serverless / Redshift / Splunk / HTTP endpoint sinks · KMS encryption · KCL/KPL clients

---

## 1. Purpose

- Codify **Kinesis Data Streams** as the canonical AWS-native ingestion layer for high-throughput event streams (clickstream, IoT telemetry, app events, log shipping).
- Codify **Kinesis Data Firehose** as the canonical "stream-to-storage" pipeline — buffers + batches + optionally transforms + delivers to S3/OpenSearch/Redshift/Splunk.
- Codify the **on-demand vs provisioned mode** decision for KDS — most engagements should default to on-demand.
- Codify **Enhanced Fan-Out** for low-latency consumers (< 70ms vs 200ms standard).
- Codify **dynamic partitioning** in Firehose for cheap S3 layout (`year=/month=/day=/hour=/customer_id=`).
- Codify **Lambda transform** in Firehose for parse/enrich/filter before write.
- This is the **streaming ingestion specialisation**. Pairs with `DATA_MANAGED_FLINK` (compute) + `DATA_OPENSEARCH_SERVERLESS` (search/dashboards) + `DATA_EVENTBRIDGE_PIPES` (alternative for low-volume).

When the SOW signals: "real-time analytics", "log shipping at scale", "clickstream", "IoT data ingestion", "kappa architecture", "stream → S3 + Athena".

---

## 2. Decision tree — KDS vs KDF vs MSK vs EventBridge

| Need | KDS | KDF | MSK | EventBridge Pipes |
|---|:---:|:---:|:---:|:---:|
| Custom consumer apps (Flink, KCL) | ✅ | ❌ stream not consumable | ✅ Kafka clients | ⚠️ as source only |
| Buffered batch delivery to S3 | ⚠️ via Firehose downstream | ✅ direct | ⚠️ via S3 sink connector | ⚠️ |
| Sub-second latency | ✅ EFO < 70ms | ❌ 60-300s buffer | ✅ | ✅ |
| > 1 GB/s ingestion | ✅ on-demand auto-scales | ✅ | ✅ Kafka | ❌ |
| Replay 7-365 days | ✅ retention up to 365d | ❌ no retention | ✅ topic retention | ❌ |
| Existing Kafka client | ❌ | ❌ | ✅ | ❌ |
| Simple SaaS → S3 | ❌ overkill | ✅ direct | ❌ | ⚠️ small volume only |

```
Architecture pattern:
  Producers (mobile, web, IoT) ──► Kinesis Data Streams (raw)
                                          │
                       ┌──────────────────┼──────────────────┐
                       ▼                  ▼                  ▼
                 ┌──────────┐      ┌──────────┐       ┌──────────────┐
                 │ Firehose │      │ Flink    │       │ KCL consumer  │
                 │ → S3     │      │ → enriched│       │ (Lambda or    │
                 │  partit. │      │   stream  │       │  ECS task)    │
                 └──────────┘      └──────────┘       └──────────────┘
                       │                  │
                       ▼                  ▼
                 S3 Iceberg         Aggregated stream → Firehose → OpenSearch
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — KDS on-demand + KDF → S3 | **§3 Monolith** |
| Production — KDS provisioned + KDF + Lambda transform + dynamic partitioning + EFO | **§5 Production** |

---

## 3. Monolith Variant — KDS + Firehose → S3

### 3.1 Architecture

```
   Producers ──put_records──► Kinesis Data Stream (on-demand, retention 24h)
                                    │
                                    ▼
                         Kinesis Data Firehose
                            - Buffer: 64 MB OR 60s
                            - Lambda transform (parse + add metadata)
                            - Dynamic partitioning (year/month/day/event_type)
                            - Compression: Snappy (ZSTD if Parquet)
                            - Format: Parquet via Glue table schema
                                    │
                                    ▼
                              S3 raw bucket
                              (partitioned, lifecycle to Glacier 90d)
                                    │
                                    ▼
                              Glue Catalog table → Athena
```

### 3.2 CDK

```python
# stacks/streaming_stack.py
from aws_cdk import Stack, Duration, RemovalPolicy
from aws_cdk import aws_kinesis as kinesis
from aws_cdk import aws_kinesisfirehose as kdf
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_kms as kms
from constructs import Construct
import json


class StreamingStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 kms_key: kms.IKey, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Kinesis Data Stream (on-demand) ────────────────────────
        self.stream = kinesis.Stream(self, "EventStream",
            stream_name=f"{env_name}-events",
            stream_mode=kinesis.StreamMode.ON_DEMAND,           # auto-scales
            encryption=kinesis.StreamEncryption.KMS,
            encryption_key=kms_key,
            retention_period=Duration.hours(24),                # extend to 7d ($) or 365d ($$)
            removal_policy=RemovalPolicy.RETAIN if env_name == "prod" else RemovalPolicy.DESTROY,
        )

        # ── 2. S3 raw bucket (with partitioned lifecycle) ────────────
        raw_bucket = s3.Bucket(self, "RawBucket",
            bucket_name=f"{env_name}-stream-raw-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(
                transitions=[
                    s3.Transition(storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                                  transition_after=Duration.days(30)),
                    s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                                  transition_after=Duration.days(90)),
                ],
                expiration=Duration.days(2557),                  # 7y
            )],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── 3. Glue catalog table (Parquet schema) ───────────────────
        glue_db = glue.CfnDatabase(self, "GlueDb",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=f"{env_name}_stream",
            ),
        )

        glue_table = glue.CfnTable(self, "GlueTable",
            catalog_id=self.account,
            database_name=glue_db.ref,
            table_input=glue.CfnTable.TableInputProperty(
                name="events",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "parquet",
                    "has_encrypted_data": "true",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{raw_bucket.bucket_name}/events/",
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="event_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="event_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="user_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="timestamp", type="timestamp"),
                        glue.CfnTable.ColumnProperty(name="properties",
                                                      type="map<string,string>"),
                    ],
                    compressed=True,
                ),
                # Partition keys (dynamic via Firehose)
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="year", type="int"),
                    glue.CfnTable.ColumnProperty(name="month", type="int"),
                    glue.CfnTable.ColumnProperty(name="day", type="int"),
                    glue.CfnTable.ColumnProperty(name="event_type_p", type="string"),
                ],
            ),
        )

        # ── 4. Lambda transform — parse JSON + add timestamp + filter ─
        transform_fn = _lambda.Function(self, "TransformFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="transform.handler",
            code=_lambda.Code.from_asset("src/firehose_transform"),
            timeout=Duration.minutes(5),                          # max for KDF transform
            memory_size=256,
        )
        # Lambda code (src/firehose_transform/transform.py):
        #   import base64, json, datetime
        #   def handler(event, context):
        #       output = []
        #       for record in event["records"]:
        #           payload = json.loads(base64.b64decode(record["data"]))
        #           # validate, enrich, filter
        #           if not payload.get("event_id"):
        #               output.append({"recordId": record["recordId"],
        #                              "result": "Dropped"})
        #               continue
        #           payload["ingested_at"] = datetime.datetime.utcnow().isoformat()
        #           output.append({
        #               "recordId": record["recordId"],
        #               "result": "Ok",
        #               "data": base64.b64encode(json.dumps(payload).encode()).decode(),
        #               "metadata": {
        #                   "partitionKeys": {
        #                       "event_type_p": payload.get("event_type", "unknown"),
        #                   },
        #               },
        #           })
        #       return {"records": output}

        # ── 5. Firehose role ──────────────────────────────────────────
        kdf_role = iam.Role(self, "KdfRole",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
        )
        self.stream.grant_read(kdf_role)
        raw_bucket.grant_write(kdf_role)
        kms_key.grant_encrypt_decrypt(kdf_role)
        transform_fn.grant_invoke(kdf_role)

        # Glue table read for schema discovery
        kdf_role.add_to_policy(iam.PolicyStatement(
            actions=["glue:GetTable", "glue:GetTableVersion", "glue:GetTableVersions"],
            resources=[
                f"arn:aws:glue:{self.region}:{self.account}:catalog",
                f"arn:aws:glue:{self.region}:{self.account}:database/{glue_db.ref}",
                f"arn:aws:glue:{self.region}:{self.account}:table/{glue_db.ref}/events",
            ],
        ))

        # ── 6. Firehose delivery stream ──────────────────────────────
        firehose = kdf.CfnDeliveryStream(self, "Firehose",
            delivery_stream_name=f"{env_name}-events-firehose",
            delivery_stream_type="KinesisStreamAsSource",
            kinesis_stream_source_configuration=kdf.CfnDeliveryStream.KinesisStreamSourceConfigurationProperty(
                kinesis_stream_arn=self.stream.stream_arn,
                role_arn=kdf_role.role_arn,
            ),
            extended_s3_destination_configuration=kdf.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=raw_bucket.bucket_arn,
                role_arn=kdf_role.role_arn,
                # Buffer
                buffering_hints=kdf.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60,                       # write every 60s OR 64MB
                    size_in_m_bs=64,
                ),
                # Compression — Snappy for Parquet
                compression_format="UNCOMPRESSED",                # Parquet handles compression internally
                # Convert to Parquet
                data_format_conversion_configuration=kdf.CfnDeliveryStream.DataFormatConversionConfigurationProperty(
                    enabled=True,
                    schema_configuration=kdf.CfnDeliveryStream.SchemaConfigurationProperty(
                        catalog_id=self.account,
                        database_name=glue_db.ref,
                        table_name="events",
                        region=self.region,
                        role_arn=kdf_role.role_arn,
                    ),
                    input_format_configuration=kdf.CfnDeliveryStream.InputFormatConfigurationProperty(
                        deserializer=kdf.CfnDeliveryStream.DeserializerProperty(
                            open_x_json_ser_de=kdf.CfnDeliveryStream.OpenXJsonSerDeProperty(
                                case_insensitive=False,
                            ),
                        ),
                    ),
                    output_format_configuration=kdf.CfnDeliveryStream.OutputFormatConfigurationProperty(
                        serializer=kdf.CfnDeliveryStream.SerializerProperty(
                            parquet_ser_de=kdf.CfnDeliveryStream.ParquetSerDeProperty(
                                compression="SNAPPY",
                                writer_version="V2",
                            ),
                        ),
                    ),
                ),
                # Dynamic partitioning
                dynamic_partitioning_configuration=kdf.CfnDeliveryStream.DynamicPartitioningConfigurationProperty(
                    enabled=True,
                    retry_options=kdf.CfnDeliveryStream.RetryOptionsProperty(
                        duration_in_seconds=300,
                    ),
                ),
                # Prefix uses Lambda metadata.partitionKeys + intrinsics
                prefix=("events/"
                        "year=!{timestamp:yyyy}/month=!{timestamp:MM}/"
                        "day=!{timestamp:dd}/event_type_p=!{partitionKeyFromLambda:event_type_p}/"),
                error_output_prefix="error/!{firehose:error-output-type}/",
                # Lambda transform
                processing_configuration=kdf.CfnDeliveryStream.ProcessingConfigurationProperty(
                    enabled=True,
                    processors=[
                        kdf.CfnDeliveryStream.ProcessorProperty(
                            type="Lambda",
                            parameters=[
                                kdf.CfnDeliveryStream.ProcessorParameterProperty(
                                    parameter_name="LambdaArn",
                                    parameter_value=transform_fn.function_arn,
                                ),
                                kdf.CfnDeliveryStream.ProcessorParameterProperty(
                                    parameter_name="BufferSizeInMBs", parameter_value="3",
                                ),
                                kdf.CfnDeliveryStream.ProcessorParameterProperty(
                                    parameter_name="BufferIntervalInSeconds", parameter_value="60",
                                ),
                            ],
                        ),
                        # MetadataExtraction processor (alternative to Lambda for simple field extraction)
                        # kdf.CfnDeliveryStream.ProcessorProperty(
                        #     type="MetadataExtraction",
                        #     parameters=[
                        #         kdf.CfnDeliveryStream.ProcessorParameterProperty(
                        #             parameter_name="MetadataExtractionQuery",
                        #             parameter_value="{event_type_p:.event_type}",
                        #         ),
                        #     ],
                        # ),
                    ],
                ),
                # Server-side encryption
                encryption_configuration=kdf.CfnDeliveryStream.EncryptionConfigurationProperty(
                    kms_encryption_config=kdf.CfnDeliveryStream.KMSEncryptionConfigProperty(
                        awskms_key_arn=kms_key.key_arn,
                    ),
                ),
                # CloudWatch logging
                cloud_watch_logging_options=kdf.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                    enabled=True,
                    log_group_name=f"/aws/kinesisfirehose/{env_name}-events-firehose",
                    log_stream_name="S3Delivery",
                ),
            ),
        )
```

---

## 4. Producer side — KPL or boto3 PutRecords

### 4.1 boto3 (low volume — most apps)

```python
import boto3, json
kds = boto3.client("kinesis")

resp = kds.put_records(
    StreamName="prod-events",
    Records=[
        {"Data": json.dumps(event), "PartitionKey": event["user_id"]}
        for event in batch[:500]                    # max 500 records or 5 MB per request
    ],
)
# Check resp["FailedRecordCount"] and retry failed records with backoff
```

### 4.2 KPL (high volume — Java) or Lambda Powertools (Python)

For > 1000 events/sec per producer, use KPL (Kinesis Producer Library, Java) which aggregates many small records into larger PutRecord calls (~10× cost reduction).

---

## 5. Production Variant — Enhanced Fan-Out + provisioned

```python
# Provisioned mode for predictable >50 MB/s sustained throughput
self.stream = kinesis.Stream(self, "EventStream",
    stream_mode=kinesis.StreamMode.PROVISIONED,
    shard_count=10,                                   # each shard = 1 MB/s in, 2 MB/s out
    encryption=kinesis.StreamEncryption.KMS,
    encryption_key=kms_key,
    retention_period=Duration.days(7),
)

# Enhanced Fan-Out consumer (dedicated 2 MB/s per consumer per shard)
efo_consumer = kinesis.CfnStreamConsumer(self, "FlinkEfo",
    consumer_name="flink-app-efo",
    stream_arn=self.stream.stream_arn,
)
# Flink app references EFO consumer ARN in its source config
```

---

## 6. Common gotchas

- **On-demand mode auto-scales but has 5-minute scale-up latency.** Bursty workloads may throttle initially. For predictable spikes, switch to provisioned with extra headroom.
- **Provisioned shard limits**: 1 MB/s OR 1000 records/s in; 2 MB/s OR 5 reads/s out. Hit either → throttle.
- **Hot shards** when partition_key cardinality is low. Use UUID or hash for even distribution.
- **Firehose dynamic partitioning costs $0.018/GB processed** in addition to base ingestion. Cheap, but accounted for in pricing.
- **Firehose max buffer 128 MB / 900s** for S3 destination. For OpenSearch destination, 100 MB / 900s. Latency = buffer interval.
- **Lambda transform return MUST match Firehose schema**: list of `{recordId, result, data, metadata?}`. Wrong format = stuck pipeline + CloudWatch errors.
- **Parquet conversion in Firehose requires Glue table schema** to match exactly. Adding a field to producer = schema mismatch errors until Glue updated.
- **EFO consumer cost is $0.015/hour per consumer per shard** + data transfer. 10 shards × 3 EFO consumers × 730h = $328/mo.
- **KDS retention extension cost**: 7d retention = $0.02/shard-hour extra; 365d = $0.10/shard-hour. Plan before extending.
- **Cross-region streaming requires KDS Global Stream** (2024+) OR Firehose-to-cross-region-S3 (slower path).
- **Lambda transform timeout 5 min max** — can't do heavy enrichment. Defer to Flink for that.
- **KDS encryption KMS key MUST allow `kms:GenerateDataKey` for `kinesis.amazonaws.com`** in key policy. CDK helper does this.

---

## 7. Pytest worked example

```python
# tests/test_streaming.py
import boto3, json, time, pytest

kds = boto3.client("kinesis")
kdf = boto3.client("firehose")
s3 = boto3.client("s3")


def test_stream_active(stream_name):
    desc = kds.describe_stream(StreamName=stream_name)["StreamDescription"]
    assert desc["StreamStatus"] == "ACTIVE"
    assert desc["EncryptionType"] == "KMS"


def test_firehose_active(firehose_name):
    desc = kdf.describe_delivery_stream(DeliveryStreamName=firehose_name)["DeliveryStreamDescription"]
    assert desc["DeliveryStreamStatus"] == "ACTIVE"


def test_end_to_end_delivery(stream_name, raw_bucket):
    """Put 100 records → wait 90s → verify Parquet file in S3 with dynamic partition."""
    records = [{
        "Data": json.dumps({
            "event_id": f"evt-{i}", "event_type": "page_view",
            "user_id": f"user-{i % 10}", "timestamp": time.time(),
        }),
        "PartitionKey": f"user-{i % 10}",
    } for i in range(100)]
    kds.put_records(StreamName=stream_name, Records=records)

    time.sleep(90)   # wait for Firehose buffer flush

    today = time.strftime("year=%Y/month=%m/day=%d")
    objs = s3.list_objects_v2(Bucket=raw_bucket,
                              Prefix=f"events/{today}/event_type_p=page_view/")
    assert objs["KeyCount"] >= 1, f"No Parquet files found in events/{today}/event_type_p=page_view/"


def test_athena_query_returns_records(athena_db, athena_workgroup):
    """Query the events table via Athena — should return ≥ 100."""
    athena = boto3.client("athena")
    qid = athena.start_query_execution(
        QueryString=f"SELECT COUNT(*) FROM {athena_db}.events WHERE event_type = 'page_view'",
        WorkGroup=athena_workgroup,
    )["QueryExecutionId"]
    # poll + assert
```

---

## 8. Five non-negotiables

1. **KMS CMK encryption** on stream + Firehose + S3 destination — never AWS-owned key.
2. **Dynamic partitioning enabled** in Firehose — `year=/month=/day=/<biz key>=` minimum.
3. **Parquet output (NOT raw JSON) to S3** — 5-10× cheaper Athena scans.
4. **Firehose CloudWatch logging enabled** — invisible failures otherwise.
5. **On-demand mode by default**; provisioned only when sustained > 50 MB/s.

---

## 9. References

- [Kinesis Data Streams User Guide](https://docs.aws.amazon.com/streams/latest/dev/introduction.html)
- [Kinesis Data Firehose](https://docs.aws.amazon.com/firehose/latest/dev/what-is-this-service.html)
- [Dynamic partitioning in Firehose](https://docs.aws.amazon.com/firehose/latest/dev/dynamic-partitioning.html)
- [Lambda transform in Firehose](https://docs.aws.amazon.com/firehose/latest/dev/data-transformation.html)
- [Enhanced Fan-Out (EFO)](https://docs.aws.amazon.com/streams/latest/dev/enhanced-consumers.html)
- [On-demand vs Provisioned](https://docs.aws.amazon.com/streams/latest/dev/how-do-i-size-a-stream.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. KDS on-demand + provisioned + EFO + KDF + Lambda transform + Parquet + dynamic partitioning + S3 sink. Wave 12. |
