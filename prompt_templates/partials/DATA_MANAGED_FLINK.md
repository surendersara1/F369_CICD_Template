# SOP — Amazon Managed Service for Apache Flink (Studio · Application · Flink SQL · windowing · state · checkpointing · scaling)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Managed Service for Apache Flink (formerly Kinesis Data Analytics) v1.20+ · Flink SQL · DataStream API · Studio (Zeppelin notebook) · Stateful processing + checkpointing to S3 · Auto-scaling · Kinesis/MSK source · S3/OS/Kinesis sink

---

## 1. Purpose

- Codify **Managed Apache Flink** as the canonical AWS streaming-compute layer. Replaces self-hosted Flink + Kafka Streams with a managed service that handles state, scaling, checkpointing.
- Codify **Studio (Zeppelin notebooks)** for interactive development / one-off queries / SQL prototyping.
- Codify **Application (production)** for long-running streaming jobs.
- Codify **Flink SQL patterns**: tumbling/sliding/session windows, joins (interval, temporal), enrichment via lookup, deduplication.
- Codify **state backend (RocksDB on EBS)** + **checkpointing to S3** for exactly-once.
- Codify **auto-scaling** based on `containerCPUUtilization` / per-shard load.
- Codify **Kinesis Data Streams source** + **MSK source** + **S3 sink** + **OpenSearch sink**.
- This is the **streaming compute specialisation**. Pairs with `DATA_KINESIS_STREAMS_FIREHOSE` (ingest) + `DATA_OPENSEARCH_SERVERLESS` (search) + `DATA_MSK_KAFKA` (alternative source).

When the SOW signals: "real-time aggregation", "windowed analytics", "fraud detection", "stream enrichment", "Flink on AWS", "Kafka Streams replacement".

---

## 2. Decision tree — Studio vs Application; SQL vs DataStream

```
Use case?
├── Interactive SQL exploration → Studio (Zeppelin notebook)
├── Long-running production job → Application
└── Both — prototype in Studio, deploy to Application

Job type?
├── Pure SQL aggregation / join → §3 Application + Flink SQL
├── Custom stateful logic, complex events → §4 Application + DataStream API (Java/Scala)
└── Lookup enrichment from external system → §5 Async I/O pattern

Sink?
├── Real-time dashboard → OpenSearch / Timestream
├── Cheap analytics → S3 (Parquet) via Firehose
├── Downstream microservice → Kinesis / Lambda / SNS
└── Materialized view → DynamoDB / RDS
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Studio notebook for SQL exploration + one Application | **§3 Monolith** |
| Production — multiple Applications + auto-scaling + alarms | **§6 Production** |

---

## 3. Monolith Variant — Application + Flink SQL

### 3.1 Architecture

```
   Kinesis Data Stream (source)
        │
        ▼
   ┌────────────────────────────────────────┐
   │  Managed Flink Application             │
   │  - Flink 1.20+                          │
   │  - 4 KPUs (1 vCPU + 4GB each)           │
   │  - RocksDB state on EBS                  │
   │  - Checkpoint every 60s → S3             │
   │  - Snapshot every 24h (point-in-time)   │
   │                                          │
   │  Flink SQL job:                          │
   │   - 1-min tumbling window count          │
   │   - 5-min sliding window p95 latency     │
   │   - Lookup enrichment from DDB           │
   │   - Sink to OpenSearch                    │
   └────────────┬───────────────────────────┘
                │
                ▼
        OpenSearch (real-time dashboard)
```

### 3.2 CDK

```python
# stacks/flink_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_kinesisanalyticsv2 as kda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct
import json


class FlinkStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 source_stream_arn: str, sink_oss_collection_arn: str,
                 jar_bucket: s3.IBucket, jar_key: str,           # uploaded JAR
                 kms_key_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Checkpoint + savepoint S3 bucket ───────────────────────
        ckpt_bucket = s3.Bucket(self, "CkptBucket",
            bucket_name=f"{env_name}-flink-checkpoints-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[s3.LifecycleRule(
                noncurrent_version_expiration=Duration.days(7),
                expiration=Duration.days(30),                     # auto-clean old checkpoints
            )],
        )

        # ── 2. IAM role ───────────────────────────────────────────────
        flink_role = iam.Role(self, "FlinkRole",
            assumed_by=iam.ServicePrincipal("kinesisanalytics.amazonaws.com"),
        )
        flink_role.add_to_policy(iam.PolicyStatement(
            actions=["kinesis:DescribeStream", "kinesis:GetShardIterator",
                     "kinesis:GetRecords", "kinesis:ListShards",
                     "kinesis:DescribeStreamSummary"],
            resources=[source_stream_arn],
        ))
        flink_role.add_to_policy(iam.PolicyStatement(
            actions=["aoss:APIAccessAll"],
            resources=[sink_oss_collection_arn],
        ))
        ckpt_bucket.grant_read_write(flink_role)
        jar_bucket.grant_read(flink_role)
        flink_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey"],
            resources=[kms_key_arn],
        ))
        # CloudWatch
        flink_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:DescribeLogGroups", "logs:DescribeLogStreams",
                     "logs:CreateLogStream", "logs:PutLogEvents", "cloudwatch:PutMetricData"],
            resources=["*"],
        ))

        # ── 3. CloudWatch log group + stream ──────────────────────────
        log_group = logs.LogGroup(self, "FlinkLogGroup",
            log_group_name=f"/aws/kinesis-analytics/{env_name}-flink-app",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        log_stream = logs.LogStream(self, "FlinkLogStream",
            log_group=log_group,
            log_stream_name="flink-cwlog",
        )

        # ── 4. Managed Flink Application ──────────────────────────────
        app = kda.CfnApplicationV2(self, "FlinkApp",
            application_name=f"{env_name}-stream-aggregator",
            runtime_environment="FLINK-1_20",
            service_execution_role=flink_role.role_arn,
            application_configuration=kda.CfnApplicationV2.ApplicationConfigurationProperty(
                application_code_configuration=kda.CfnApplicationV2.ApplicationCodeConfigurationProperty(
                    code_content_type="ZIPFILE",
                    code_content=kda.CfnApplicationV2.CodeContentProperty(
                        s3_content_location=kda.CfnApplicationV2.S3ContentLocationProperty(
                            bucket_arn=jar_bucket.bucket_arn,
                            file_key=jar_key,
                        ),
                    ),
                ),
                # Flink runtime config
                flink_application_configuration=kda.CfnApplicationV2.FlinkApplicationConfigurationProperty(
                    checkpoint_configuration=kda.CfnApplicationV2.CheckpointConfigurationProperty(
                        configuration_type="CUSTOM",
                        checkpointing_enabled=True,
                        checkpoint_interval=60_000,                  # ms — every 60s
                        min_pause_between_checkpoints=5_000,
                    ),
                    monitoring_configuration=kda.CfnApplicationV2.MonitoringConfigurationProperty(
                        configuration_type="CUSTOM",
                        log_level="INFO",
                        metrics_level="APPLICATION",                  # OPERATOR for deep debug
                    ),
                    parallelism_configuration=kda.CfnApplicationV2.ParallelismConfigurationProperty(
                        configuration_type="CUSTOM",
                        parallelism=4,                                # 4 KPUs
                        parallelism_per_kpu=1,
                        auto_scaling_enabled=True,
                    ),
                ),
                # Per-job runtime properties
                environment_properties=kda.CfnApplicationV2.EnvironmentPropertiesProperty(
                    property_groups=[
                        kda.CfnApplicationV2.PropertyGroupProperty(
                            property_group_id="FlinkApplicationProperties",
                            property_map={
                                "source_stream": source_stream_arn.split("/")[-1],
                                "source_region": self.region,
                                "sink_oss_endpoint": sink_oss_collection_arn,
                                "checkpoint_path": f"s3a://{ckpt_bucket.bucket_name}/checkpoints/",
                            },
                        ),
                    ],
                ),
            ),
        )

        # ── 5. CloudWatch logging on the app ─────────────────────────
        kda.CfnApplicationCloudWatchLoggingOptionV2(self, "FlinkCwLogOpt",
            application_name=app.ref,
            cloud_watch_logging_option=kda.CfnApplicationCloudWatchLoggingOptionV2.CloudWatchLoggingOptionProperty(
                log_stream_arn=f"arn:aws:logs:{self.region}:{self.account}:log-group:{log_group.log_group_name}:log-stream:{log_stream.log_stream_name}",
            ),
        )
```

### 3.3 Flink SQL job (uploaded as JAR or Studio notebook)

```sql
-- src/flink/aggregator.sql

-- Source: Kinesis stream (raw events JSON)
CREATE TABLE source_events (
    event_id STRING,
    event_type STRING,
    user_id STRING,
    properties MAP<STRING, STRING>,
    event_time TIMESTAMP_LTZ(3),
    WATERMARK FOR event_time AS event_time - INTERVAL '5' SECOND
) WITH (
    'connector' = 'kinesis',
    'stream' = 'prod-events',
    'aws.region' = 'us-east-1',
    'scan.stream.initpos' = 'LATEST',
    'format' = 'json'
);

-- Sink: OpenSearch (per-minute aggregates)
CREATE TABLE sink_minute_aggregates (
    window_start TIMESTAMP_LTZ(3),
    window_end TIMESTAMP_LTZ(3),
    event_type STRING,
    event_count BIGINT,
    unique_users BIGINT,
    PRIMARY KEY (window_start, event_type) NOT ENFORCED
) WITH (
    'connector' = 'opensearch-2',
    'hosts' = 'https://my-collection.us-east-1.aoss.amazonaws.com',
    'index' = 'minute-aggregates',
    'username' = 'iam',                      -- IAM auth via execution role
    'aws.region' = 'us-east-1',
    'sink.bulk-flush.max-actions' = '1000',
    'sink.bulk-flush.interval' = '10s'
);

-- 1-minute tumbling windows
INSERT INTO sink_minute_aggregates
SELECT
    window_start,
    window_end,
    event_type,
    COUNT(*) AS event_count,
    COUNT(DISTINCT user_id) AS unique_users
FROM TABLE(
    TUMBLE(TABLE source_events, DESCRIPTOR(event_time), INTERVAL '1' MINUTE)
)
GROUP BY window_start, window_end, event_type;

-- 5-minute sliding window for moving average (slides every 1 min)
CREATE TABLE sink_sliding_avg (
    window_end TIMESTAMP_LTZ(3),
    event_type STRING,
    avg_per_minute DOUBLE
) WITH ( ... );

INSERT INTO sink_sliding_avg
SELECT
    window_end,
    event_type,
    AVG(CAST(event_count AS DOUBLE)) OVER (
        PARTITION BY event_type
        ORDER BY window_end
        RANGE BETWEEN INTERVAL '5' MINUTE PRECEDING AND CURRENT ROW
    ) AS avg_per_minute
FROM sink_minute_aggregates;

-- Lookup join (enrich with user profile from DDB)
CREATE TABLE user_profile_lookup (
    user_id STRING,
    tier STRING,
    cohort STRING
) WITH (
    'connector' = 'dynamodb',
    'table-name' = 'user-profiles',
    'aws.region' = 'us-east-1'
);

INSERT INTO sink_enriched_events
SELECT
    e.event_id, e.event_type, e.user_id,
    u.tier, u.cohort,
    e.event_time
FROM source_events AS e
LEFT JOIN user_profile_lookup FOR SYSTEM_TIME AS OF e.event_time AS u
    ON e.user_id = u.user_id;
```

---

## 4. DataStream API job (Java) — when SQL isn't enough

```java
// src/main/java/com/acme/FraudDetector.java
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
env.enableCheckpointing(60_000);
env.getCheckpointConfig().setExternalizedCheckpointCleanup(
    ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION);

DataStream<Transaction> txStream = env.fromSource(
    KinesisStreamsSource.<Transaction>builder()
        .setStreamArn("arn:aws:kinesis:us-east-1:123:stream/transactions")
        .setSourceConfig(...)
        .setDeserializationSchema(new TransactionDeserializer())
        .build(),
    WatermarkStrategy.<Transaction>forBoundedOutOfOrderness(Duration.ofSeconds(10))
        .withTimestampAssigner((tx, ts) -> tx.getEventTime()),
    "kinesis-source"
);

// Stateful per-user fraud detection
txStream
    .keyBy(Transaction::getUserId)
    .process(new FraudDetectorFunction())   // ProcessFunction with state
    .addSink(KinesisStreamsSink.<Alert>builder()
        .setStreamArn("arn:aws:kinesis:us-east-1:123:stream/alerts")
        .setSerializationSchema(new AlertSerializer())
        .build());

env.execute("Fraud detector");
```

---

## 5. Async I/O pattern — external lookups without blocking

```java
// Async lookup against DDB / external API without blocking the operator
AsyncDataStream.unorderedWait(
    txStream,
    new AsyncDdbLookup(),                      // returns CompletableFuture
    1, TimeUnit.SECONDS,                       // timeout
    100                                         // capacity
)
.process(...);
```

---

## 6. Common gotchas

- **KPU pricing: $0.11/hour per KPU** + $0.10/GB stored state on EBS. 4 KPUs × 730h = $321/mo for the simplest job.
- **`parallelism_per_kpu`** rule of thumb: 1 for stateful jobs, 4 for stateless (transformations only).
- **Auto-scaling lags 15-30 min.** For known traffic spikes (Black Friday), pre-scale via `update-application`.
- **Checkpoints to S3 cost ~$0.05/GB stored.** Large state = unbounded growth without cleanup. Set lifecycle on checkpoint bucket.
- **Watermarks must align with event-time semantics.** Without `WATERMARK FOR event_time`, windows fire on processing-time and miss late-arriving events.
- **Studio notebooks cost $0.93/hour per Zeppelin compute** when running. STOP when idle — easy to forget.
- **Flink SQL JSON deserializer FAILS HARD on schema mismatch.** Use `'json.fail-on-missing-field' = 'false'` for tolerance.
- **OpenSearch sink bulk-flush settings** matter: too small = too many requests, too large = memory pressure. Defaults rarely fit.
- **DynamoDB lookup sink hits read capacity** — use DAX or in-memory cache via Async I/O if traffic is high.
- **MSK source requires VPC config** in the Flink app. Studio notebooks can't reach private MSK without a VPC notebook.
- **Recovery from S3 checkpoint** can take 5+ min for large state. Don't kill long-paused jobs without snapshot.
- **`min_pause_between_checkpoints`** must be > 5_000 ms or successive checkpoints stomp each other.

---

## 7. Pytest worked example

```python
# tests/test_flink.py
import boto3, time

kda = boto3.client("kinesisanalyticsv2")


def test_app_running(app_name):
    desc = kda.describe_application(ApplicationName=app_name)["ApplicationDetail"]
    assert desc["ApplicationStatus"] == "RUNNING"


def test_app_has_checkpointing_enabled(app_name):
    desc = kda.describe_application(ApplicationName=app_name)["ApplicationDetail"]
    flink_cfg = desc["ApplicationConfigurationDescription"]["FlinkApplicationConfigurationDescription"]
    ckpt = flink_cfg["CheckpointConfigurationDescription"]
    assert ckpt["CheckpointingEnabled"] is True
    assert ckpt["CheckpointInterval"] >= 30_000   # at least 30s


def test_app_recent_snapshot_exists(app_name):
    snaps = kda.list_application_snapshots(ApplicationName=app_name)["ApplicationSnapshotSummaries"]
    assert snaps, "No snapshots — app may not have run long enough"
    latest = max(snaps, key=lambda s: s["SnapshotCreationTimestamp"])
    age = time.time() - latest["SnapshotCreationTimestamp"].timestamp()
    assert age < 86400 * 2, f"Latest snapshot too old: {age/3600:.1f}h"


def test_app_no_recent_failures(app_name):
    """CW Alarm 'KPUs failed' should be OK."""
    cw = boto3.client("cloudwatch")
    alarms = cw.describe_alarms(AlarmNames=[f"{app_name}-failed"])["MetricAlarms"]
    if alarms:
        assert alarms[0]["StateValue"] == "OK"
```

---

## 8. Five non-negotiables

1. **Checkpointing every 60s + snapshots every 24h** to S3 with KMS encryption.
2. **Watermarks defined** on every source for event-time correctness.
3. **CloudWatch logging at INFO level + metrics at APPLICATION level** — silent failures otherwise.
4. **Auto-scaling enabled** for production; KPU upper bound set to control cost.
5. **Stop Studio notebooks when idle** — billed per hour while RUNNING.

---

## 9. References

- [Managed Service for Apache Flink — Developer Guide](https://docs.aws.amazon.com/managed-flink/latest/java/what-is.html)
- [Flink SQL on Kinesis](https://docs.aws.amazon.com/managed-flink/latest/java/how-creating-apps.html)
- [Studio notebooks](https://docs.aws.amazon.com/managed-flink/latest/java/how-zeppelin.html)
- [Auto-scaling](https://docs.aws.amazon.com/managed-flink/latest/java/how-scaling.html)
- [State backends + checkpointing](https://nightlies.apache.org/flink/flink-docs-release-1.20/docs/ops/state/checkpoints/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Managed Flink 1.20+ Application + Studio + Flink SQL + DataStream API + checkpointing + auto-scaling + sources/sinks. Wave 12. |
