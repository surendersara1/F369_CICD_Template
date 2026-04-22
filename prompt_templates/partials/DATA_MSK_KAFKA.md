# SOP — MSK / Kafka Streaming (Amazon Managed Streaming for Kafka)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Kafka 3.6.0 · IAM SASL auth · Glue Schema Registry · MSK provisioned (L1 `aws_msk.CfnCluster`)

---

## 1. Purpose

Provision a managed Kafka streaming platform:

- **MSK provisioned cluster** (or Serverless for very-high-throughput) — IAM SASL auth, TLS in transit + at rest, 3-broker multi-AZ HA, custom broker config, Prometheus + CloudWatch monitoring.
- **Glue Schema Registry** for Avro / Protobuf / JSON Schema with BACKWARD compatibility — the free AWS-native alternative to Confluent Schema Registry.
- **Kafka admin Lambda** — topic lifecycle (create / list / ACLs) wired into the same VPC as the brokers.
- **MSK alarms** — broker disk, CPU, consumer lag — fed to the ops SNS topic.
- **VPC networking** (private subnets, SG per broker port) plus KMS-encrypted data volumes.

Include when SOW signals: "Apache Kafka", "MSK", "event streaming", "Schema Registry", "Kafka Connect", "exactly-once delivery", "high-throughput event pipelines".

### Kafka vs Kinesis decision

| Factor                           | Use MSK (Kafka) | Use Kinesis      |
|----------------------------------|-----------------|------------------|
| Team knows Kafka                 | Yes             | No               |
| Kafka Connect ecosystem needed   | Yes             | No               |
| Schema Registry + Avro/Protobuf  | Yes             | No               |
| Exactly-once semantics           | Strong          | At-least-once    |
| Long message retention (>7 days) | Unlimited       | 7 days max       |
| Simplicity + Lambda integration  | No              | Yes              |
| Cost at low scale                | Expensive       | Cheap            |

### MSK cluster sizing

```
# [Claude: pick based on Architecture Map throughput requirements]
# Low throughput   (<100 MB/s):  2 brokers, kafka.m5.large ($0.19/hr/broker)
# Medium throughput(<500 MB/s):  3 brokers, kafka.m5.2xlarge
# High throughput  (<2 GB/s):    6 brokers, kafka.m5.4xlarge
# Very high        (>2 GB/s):    MSK Serverless (pay per throughput, no provisioning)
```

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC / data-only stack where MSK cluster + producers + consumers + Lambda admin live in one `cdk.Stack` | **§3 Monolith** |
| Dedicated `StreamingStack` with consumer Lambdas / ECS tasks in other stacks (`ComputeStack`, ML ingestion stack, lakehouse raw-zone loader) | **§4 Micro-Stack** |

**Why the split matters.** Cross-stack grants that will **cycle** once you split:

- `msk_cluster.grant_*` — MSK L1 `CfnCluster` has no `grant_*` method, but CDK helpers built on top of the alpha `aws-msk-alpha.Cluster` L2 construct *do* mutate the cluster's IAM-auth policy surface when a consumer role is granted.
- Shared security group (`msk_sg`) mutation — if `ComputeStack` adds an ingress rule referencing `msk_sg` owned by `StreamingStack`, CDK rewrites the upstream SG's ingress rules with a reference back to the consumer SG, creating a cyclic export.
- `kms_key.grant_encrypt_decrypt(consumer_role)` where the key is in `StreamingStack` — standard KMS policy cycle.
- Bootstrap string distribution — using `cluster.attr_*` directly across stacks works in isolation, but if producers also need to feed back a consumer-group ACL via the admin Lambda, the round-trip turns cyclic.

The Micro-Stack variant fixes this by: (a) owning the MSK cluster, SG, KMS key, Schema Registry, and admin Lambda inside `StreamingStack`; (b) publishing bootstrap servers + cluster ARN + schema registry ARN + admin Lambda ARN via `ssm.StringParameter`; (c) consumer stacks grant identity-side `kafka-cluster:*` on specific topic/group ARNs, plus `kms:Decrypt` on the KMS ARN, and open their own SG's ingress to `msk_sg.security_group_id` via `peer=ec2.Peer.security_group_id(...)` read from SSM.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` class holds VPC + MSK + producers + consumers + admin Lambda together.

### 3.1 Architecture

```
Producers (EC2 / ECS / Lambda)
        │
        ▼                           ┌──────────────────────┐
  MSK provisioned cluster  ◄───────►│ Glue Schema Registry │
  (3 brokers, multi-AZ, TLS+IAM)    │  (Avro / Protobuf)    │
        │    │    │                 └──────────────────────┘
  9094 (TLS) 9098 (IAM SASL)
        │    │    │
        ▼    ▼    ▼
   Consumer Lambdas / ECS / Flink / MSK Connect
        │
        ▼
   Lakehouse raw zone  |  DynamoDB  |  Redshift
```

### 3.2 CDK — `_create_msk_kafka_cluster()` method body

```python
def _create_msk_kafka_cluster(self, stage_name: str) -> None:
    """
    Amazon MSK (Managed Streaming for Kafka) Cluster + Schema Registry.

    Components:
      A) MSK Serverless OR Provisioned cluster (based on throughput from Architecture Map)
      B) Schema Registry (via AWS Glue Schema Registry — free, integrated)
      C) MSK Connect — managed Kafka connectors (S3 sink, DynamoDB sink, JDBC source)
      D) Producer/Consumer IAM roles with fine-grained topic ACLs
      E) MSK cluster alarms (lag, disk, CPU)
      F) Kafka admin Lambda (create topics, manage ACLs)
    """

    import json
    from aws_cdk import (
        Duration, CfnOutput,
        aws_ec2 as ec2,
        aws_iam as iam,
        aws_lambda as _lambda,
        aws_cloudwatch as cw,
        aws_cloudwatch_actions as cw_actions,
    )
    import aws_cdk.aws_msk as msk
    import aws_cdk.aws_glue as glue

    # =========================================================================
    # Security group for MSK brokers
    # =========================================================================
    msk_sg = ec2.SecurityGroup(
        self, "MSKSecurityGroup",
        vpc=self.vpc,
        security_group_name=f"{{project_name}}-msk-{stage_name}",
        description="MSK broker security group — allow Kafka clients from private subnets only",
        allow_all_outbound=False,
    )
    # Kafka plaintext (9092), TLS (9094), IAM Auth (9098), ZooKeeper (2181 — only for older MSK)
    msk_sg.add_ingress_rule(
        peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
        connection=ec2.Port.tcp(9094),   # TLS
        description="Kafka TLS from VPC",
    )
    msk_sg.add_ingress_rule(
        peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
        connection=ec2.Port.tcp(9098),   # IAM auth
        description="Kafka IAM auth from VPC",
    )

    # =========================================================================
    # A) MSK PROVISIONED CLUSTER
    # =========================================================================

    msk_config = msk.CfnConfiguration(
        self, "MSKConfig",
        name=f"{{project_name}}-kafka-config-{stage_name}",
        kafka_versions_list=["3.6.0"],
        server_properties="\n".join([
            "auto.create.topics.enable=false",       # Topics must be created explicitly
            "default.replication.factor=3",          # 3x replication for durability
            "min.insync.replicas=2",                 # Write must be acked by >=2 brokers
            "num.partitions=6",                      # Default partitions per topic
            "log.retention.hours=168",               # 7 day default retention
            "log.segment.bytes=1073741824",          # 1 GB segments
            "log.retention.bytes=-1",                # Unlimited size retention (use hours)
            "compression.type=lz4",                  # Compress for cost/throughput
            "message.max.bytes=10485760",            # 10MB max message size
            "replica.lag.time.max.ms=30000",
            "zookeeper.session.timeout.ms=18000",
            "unclean.leader.election.enable=false",  # Never elect a lagging replica (no data loss)
            "delete.topic.enable=true",
        ]),
    )

    self.msk_cluster = msk.CfnCluster(
        self, "MSKCluster",
        cluster_name=f"{{project_name}}-kafka-{stage_name}",
        kafka_version="3.6.0",

        number_of_broker_nodes=3 if stage_name == "prod" else 2,  # 3 for HA (one per AZ)

        broker_node_group_info=msk.CfnCluster.BrokerNodeGroupInfoProperty(
            instance_type="kafka.m5.large" if stage_name != "prod" else "kafka.m5.2xlarge",
            client_subnets=[
                s.subnet_id for s in self.vpc.private_subnets[:3]  # One per AZ
            ],
            security_groups=[msk_sg.security_group_id],
            storage_info=msk.CfnCluster.StorageInfoProperty(
                ebs_storage_info=msk.CfnCluster.EBSStorageInfoProperty(
                    volume_size=1000 if stage_name == "prod" else 100,  # GB per broker
                    provisioned_throughput=msk.CfnCluster.ProvisionedThroughputProperty(
                        enabled=True,
                        volume_throughput=250,   # MB/s — needed for high throughput
                    ) if stage_name == "prod" else None,
                )
            ),
        ),

        # IAM-based authentication (most secure — no passwords)
        client_authentication=msk.CfnCluster.ClientAuthenticationProperty(
            sasl=msk.CfnCluster.SaslProperty(
                iam=msk.CfnCluster.IamProperty(enabled=True),
            ),
            tls=msk.CfnCluster.TlsProperty(
                certificate_authority_arn_list=[],
                enabled=True,
            ),
        ),

        encryption_info=msk.CfnCluster.EncryptionInfoProperty(
            encryption_at_rest=msk.CfnCluster.EncryptionAtRestProperty(
                data_volume_kms_key_id=self.kms_key.key_arn,
            ),
            encryption_in_transit=msk.CfnCluster.EncryptionInTransitProperty(
                client_broker="TLS",        # Client ↔ Broker: TLS only
                in_cluster=True,            # Broker ↔ Broker: TLS
            ),
        ),

        # Enhanced monitoring — detailed per-broker and per-topic metrics in CloudWatch
        enhanced_monitoring="PER_TOPIC_PER_BROKER",

        # Open monitoring (Prometheus compatible)
        open_monitoring=msk.CfnCluster.OpenMonitoringProperty(
            prometheus=msk.CfnCluster.PrometheusProperty(
                jmx_exporter=msk.CfnCluster.JmxExporterProperty(enabled_in_broker=True),
                node_exporter=msk.CfnCluster.NodeExporterProperty(enabled_in_broker=True),
            )
        ),

        # Broker logs → CloudWatch
        logging_info=msk.CfnCluster.LoggingInfoProperty(
            broker_logs=msk.CfnCluster.BrokerLogsProperty(
                cloud_watch_logs=msk.CfnCluster.CloudWatchLogsProperty(
                    enabled=True,
                    log_group=f"/aws/msk/{{project_name}}-{stage_name}",
                ),
                s3=msk.CfnCluster.S3Property(
                    enabled=True,
                    bucket=self.lake_buckets["raw"].bucket_name,
                    prefix=f"kafka-logs/",
                ) if stage_name == "prod" else None,
            )
        ),

        configuration_info=msk.CfnCluster.ConfigurationInfoProperty(
            arn=msk_config.attr_arn,
            revision=1,
        ),

        tags={"Project": "{project_name}", "Stage": stage_name},
    )

    # =========================================================================
    # B) GLUE SCHEMA REGISTRY (Schema Registry for Kafka — Avro, Protobuf, JSON Schema)
    # =========================================================================

    schema_registry = glue.CfnRegistry(
        self, "KafkaSchemaRegistry",
        name=f"{{project_name}}-kafka-schemas-{stage_name}",
        description=f"Schema Registry for {{project_name}} Kafka topics ({stage_name})",
        tags=[{"key": "Project", "value": "{project_name}"}],
    )

    # [Claude: add one CfnSchema per Kafka topic from Architecture Map]
    # Example schema for a user-events topic:
    user_events_schema = glue.CfnSchema(
        self, "UserEventsSchema",
        name="user-events",
        registry=glue.CfnSchema.RegistryProperty(arn=schema_registry.attr_arn),
        data_format="AVRO",
        compatibility="BACKWARD",  # New schema must be compatible with previous consumers
        schema_definition=json.dumps({
            "type": "record",
            "name": "UserEvent",
            "namespace": "{project_name}",
            "fields": [
                {"name": "event_id",   "type": "string"},
                {"name": "user_id",    "type": "string"},
                {"name": "event_type", "type": "string"},
                {"name": "timestamp",  "type": "long", "logicalType": "timestamp-millis"},
                {"name": "properties", "type": {"type": "map", "values": "string"}, "default": {}},
            ],
        }),
        tags=[{"key": "Topic", "value": "user-events"}],
    )

    # =========================================================================
    # C) KAFKA ADMIN LAMBDA — Topic management
    # =========================================================================

    kafka_admin_fn = _lambda.Function(
        self, "KafkaAdminFn",
        function_name=f"{{project_name}}-kafka-admin-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/kafka_admin"),
        environment={
            # [Claude: set from cluster bootstrap string after cluster create]
            "BOOTSTRAP_SERVERS": f"{{project_name}}-kafka-{stage_name}.PLACEHOLDER",
            "CLUSTER_ARN":       self.msk_cluster.attr_arn,
        },
        timeout=Duration.seconds(30),
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[msk_sg],
    )
    kafka_admin_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "kafka-cluster:Connect",
            "kafka-cluster:AlterCluster",
            "kafka-cluster:DescribeCluster",
            "kafka-cluster:*Topic*",
            "kafka-cluster:WriteData",
            "kafka-cluster:ReadData",
        ],
        resources=[
            self.msk_cluster.attr_arn,
            f"{self.msk_cluster.attr_arn.replace(':cluster/', ':topic/')}/*",
        ],
    ))

    # =========================================================================
    # D) MSK ALARMS
    # =========================================================================

    for metric_name, threshold, description in [
        ("KafkaDataLogsDiskUsed", 80, "MSK broker disk usage > 80% — add storage"),
        ("CpuUser", 60, "MSK broker CPU > 60% — consider scale up"),
        ("EstimatedMaxTimeLag", 300000, "Consumer lag > 5 minutes — consumers falling behind"),
    ]:
        cw.Alarm(
            self, f"MSKAlarm{metric_name}",
            alarm_name=f"{{project_name}}-msk-{metric_name.lower()}-{stage_name}",
            alarm_description=description,
            metric=cw.Metric(
                namespace="AWS/Kafka",
                metric_name=metric_name,
                dimensions_map={"Cluster Name": f"{{project_name}}-kafka-{stage_name}"},
                period=Duration.minutes(5),
                statistic="Maximum",
            ),
            threshold=threshold,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "MSKClusterArn",
        value=self.msk_cluster.attr_arn,
        description="MSK Kafka Cluster ARN",
        export_name=f"{{project_name}}-msk-arn-{stage_name}",
    )
    CfnOutput(self, "SchemaRegistryArn",
        value=schema_registry.attr_arn,
        description="Glue Schema Registry ARN — register Kafka topic schemas here",
        export_name=f"{{project_name}}-schema-registry-{stage_name}",
    )
    CfnOutput(self, "KafkaAdminFnArn",
        value=kafka_admin_fn.function_arn,
        description="Lambda to create/manage Kafka topics",
        export_name=f"{{project_name}}-kafka-admin-{stage_name}",
    )
```

### 3.3 Kafka admin handler (`lambda/kafka_admin/index.py`)

Requires the `kafka-python` library packaged with the function (layer or requirements).

```python
"""Kafka admin — create topics / list topics. Uses IAM SASL OAUTHBEARER."""
import os, logging
from kafka.admin import KafkaAdminClient, NewTopic

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOOTSTRAP_SERVERS = os.environ['BOOTSTRAP_SERVERS'].split(',')


def handler(event, context):
    action = event.get('action', 'create_topic')

    if action == 'create_topic':
        admin = KafkaAdminClient(
            bootstrap_servers=BOOTSTRAP_SERVERS,
            security_protocol='SASL_SSL',
            sasl_mechanism='OAUTHBEARER',
            client_id=f"admin-{context.function_name}",
        )
        topics = [NewTopic(
            name=event['topic_name'],
            num_partitions=event.get('num_partitions', 6),
            replication_factor=event.get('replication_factor', 3),
            topic_configs={
                'retention.ms':        str(event.get('retention_hours', 168) * 3600 * 1000),
                'compression.type':    event.get('compression', 'lz4'),
                'min.insync.replicas': '2',
            },
        )]
        admin.create_topics(new_topics=topics, validate_only=False)
        logger.info(f"Created topic: {event['topic_name']}")
        return {"status": "created", "topic": event['topic_name']}

    elif action == 'list_topics':
        admin = KafkaAdminClient(
            bootstrap_servers=BOOTSTRAP_SERVERS,
            security_protocol='SASL_SSL',
            sasl_mechanism='OAUTHBEARER',
        )
        return {"topics": list(admin.list_topics())}

    return {"status": "unknown_action", "action": action}
```

### 3.4 Monolith gotchas

- **`aws_msk.CfnCluster` is L1** — no `grant_*` helpers. An alpha L2 exists as `aws-cdk-lib/aws-msk-alpha.Cluster` but this SOP uses the stable L1. If you switch to the alpha L2, you gain `grant_cluster_write()` etc. but also cross-stack grant cycles.
- **MSK provisioned cluster create takes 20-30 minutes.** CDK `cdk deploy` will appear hung; watch CloudFormation events, not the CLI.
- **`client_authentication.tls.certificate_authority_arn_list=[]`** enables mutual TLS but only validates server identity without client CAs. If you want mTLS with client certs, populate the CA ARNs (ACM Private CA).
- **`enhanced_monitoring="PER_TOPIC_PER_BROKER"`** is the most expensive monitoring tier (~$0.30/broker/hour on top of cluster cost). `PER_BROKER` is usually enough for ops; drop to `DEFAULT` in dev.
- **Bootstrap servers string is not an attribute on `CfnCluster`** — you must fetch it via `aws kafka get-bootstrap-brokers --cluster-arn ...` post-deploy, or use an `AwsCustomResource` to look it up at deploy time. The `PLACEHOLDER` env var in the admin Lambda is deliberate.
- **`number_of_broker_nodes` must be a multiple of the AZ count.** With 3 AZs + `number_of_broker_nodes=2`, MSK errors out.
- **Glue Schema Registry `compatibility="BACKWARD"`** means new schemas must be readable by old consumers (field additions with defaults OK; field removal blocked). Change to `FULL` if you have strict bi-directional requirements; `NONE` disables checks entirely.
- **`lake_buckets["raw"]` reference** — the monolith assumes a lakehouse bucket exists in the same stack. In the micro-stack variant this must be read via SSM, and broker S3 logging becomes optional.

---

## 4. Micro-Stack Variant

**Use when:** a dedicated `StreamingStack` owns the MSK cluster, security group, KMS key, Schema Registry and admin Lambda. Consumer producers and Lambda / ECS consumers live in other stacks and connect using the published bootstrap string.

### 4.1 The five non-negotiables

Memorize these (reference: `LAYER_BACKEND_LAMBDA` §4.1). Every cross-stack MSK failure reduces to one of them.

1. **Anchor asset paths to `__file__`, never relative-to-CWD.** The admin Lambda code asset uses `Path(__file__).resolve().parents[3] / "lambda" / "kafka_admin"`.
2. **Never use `X.grant_*(role)` on a cross-stack resource X.** Use identity-side `PolicyStatement` on the consumer role. The MSK L1 `CfnCluster` has no `grant_*` anyway — any helpful alpha shortcut will cycle.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** Not relevant here; but if you route alarms back through SQS from consumer stacks, use L1 `CfnRule`.
4. **Never own a bucket in one stack and attach its CloudFront OAC in another.** Not relevant — broker logs go to a S3 log bucket inside `StreamingStack`.
5. **Never set `encryption_key=ext_key` where `ext_key` came from another stack.** The MSK KMS key is **owned by `StreamingStack`** (local reference). Consumers grant identity-side `kms:Decrypt` on its ARN string read via SSM.

Also: `iam:PassRole` with `PassedToService` Condition wherever a role ARN is handed to Kafka Connect; shared `permission_boundary` on every role in the stack.

### 4.2 `StreamingStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_ssm as ssm,
    aws_sns as sns,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
)
import aws_cdk.aws_msk as msk
import aws_cdk.aws_glue as glue
from constructs import Construct
import json

# stacks/streaming_stack.py  ->  stacks/  ->  cdk/  ->  infrastructure/  ->  <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class StreamingStack(cdk.Stack):
    """Owns the MSK cluster, KMS key, security group, broker-log bucket,
    Glue Schema Registry, and Kafka admin Lambda.

    Consumer stacks read bootstrap servers + cluster ARN + schema registry
    ARN + security-group ID + KMS ARN via SSM and grant identity-side.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        alert_topic_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-streaming-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk", "Layer": "Streaming"}.items():
            cdk.Tags.of(self).add(k, v)

        IS_PROD = stage_name == "prod"

        alert_topic = sns.Topic.from_topic_arn(self, "AlertTopic",
            ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm),
        )

        # --- Local CMK (honors 5th non-negotiable) --------------------------
        cmk = kms.Key(self, "StreamingKey",
            alias=f"alias/{{project_name}}-streaming-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Local log bucket for broker logs (optional; prod only) --------
        broker_logs_bucket = s3.Bucket(self, "MSKBrokerLogsBucket",
            bucket_name=f"{{project_name}}-msk-broker-logs-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            enforce_ssl=True,
            versioned=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90), enabled=True)],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Security group (owned here) -----------------------------------
        msk_sg = ec2.SecurityGroup(self, "MSKSecurityGroup",
            vpc=vpc,
            security_group_name=f"{{project_name}}-msk-{stage_name}",
            description="MSK broker SG — consumers add their own SG as peer via SSM lookup",
            allow_all_outbound=False,
        )
        msk_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(9094),
            description="Kafka TLS from VPC",
        )
        msk_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(9098),
            description="Kafka IAM auth from VPC",
        )

        # =================================================================
        # A) MSK provisioned cluster (owned here)
        # =================================================================
        msk_config = msk.CfnConfiguration(self, "MSKConfig",
            name=f"{{project_name}}-kafka-config-{stage_name}",
            kafka_versions_list=["3.6.0"],
            server_properties="\n".join([
                "auto.create.topics.enable=false",
                "default.replication.factor=3",
                "min.insync.replicas=2",
                "num.partitions=6",
                "log.retention.hours=168",
                "log.segment.bytes=1073741824",
                "log.retention.bytes=-1",
                "compression.type=lz4",
                "message.max.bytes=10485760",
                "replica.lag.time.max.ms=30000",
                "zookeeper.session.timeout.ms=18000",
                "unclean.leader.election.enable=false",
                "delete.topic.enable=true",
            ]),
        )

        msk_cluster = msk.CfnCluster(self, "MSKCluster",
            cluster_name=f"{{project_name}}-kafka-{stage_name}",
            kafka_version="3.6.0",
            number_of_broker_nodes=3 if IS_PROD else 2,
            broker_node_group_info=msk.CfnCluster.BrokerNodeGroupInfoProperty(
                instance_type="kafka.m5.large" if not IS_PROD else "kafka.m5.2xlarge",
                client_subnets=[s.subnet_id for s in vpc.private_subnets[:3]],
                security_groups=[msk_sg.security_group_id],
                storage_info=msk.CfnCluster.StorageInfoProperty(
                    ebs_storage_info=msk.CfnCluster.EBSStorageInfoProperty(
                        volume_size=1000 if IS_PROD else 100,
                        provisioned_throughput=msk.CfnCluster.ProvisionedThroughputProperty(
                            enabled=True,
                            volume_throughput=250,
                        ) if IS_PROD else None,
                    ),
                ),
            ),
            client_authentication=msk.CfnCluster.ClientAuthenticationProperty(
                sasl=msk.CfnCluster.SaslProperty(
                    iam=msk.CfnCluster.IamProperty(enabled=True),
                ),
                tls=msk.CfnCluster.TlsProperty(
                    certificate_authority_arn_list=[],
                    enabled=True,
                ),
            ),
            encryption_info=msk.CfnCluster.EncryptionInfoProperty(
                encryption_at_rest=msk.CfnCluster.EncryptionAtRestProperty(
                    data_volume_kms_key_id=cmk.key_arn,          # LOCAL key
                ),
                encryption_in_transit=msk.CfnCluster.EncryptionInTransitProperty(
                    client_broker="TLS",
                    in_cluster=True,
                ),
            ),
            enhanced_monitoring="PER_TOPIC_PER_BROKER",
            open_monitoring=msk.CfnCluster.OpenMonitoringProperty(
                prometheus=msk.CfnCluster.PrometheusProperty(
                    jmx_exporter=msk.CfnCluster.JmxExporterProperty(enabled_in_broker=True),
                    node_exporter=msk.CfnCluster.NodeExporterProperty(enabled_in_broker=True),
                ),
            ),
            logging_info=msk.CfnCluster.LoggingInfoProperty(
                broker_logs=msk.CfnCluster.BrokerLogsProperty(
                    cloud_watch_logs=msk.CfnCluster.CloudWatchLogsProperty(
                        enabled=True,
                        log_group=f"/aws/msk/{{project_name}}-{stage_name}",
                    ),
                    s3=msk.CfnCluster.S3Property(
                        enabled=True,
                        bucket=broker_logs_bucket.bucket_name,    # LOCAL bucket
                        prefix="kafka-logs/",
                    ) if IS_PROD else None,
                ),
            ),
            configuration_info=msk.CfnCluster.ConfigurationInfoProperty(
                arn=msk_config.attr_arn,
                revision=1,
            ),
        )

        # =================================================================
        # B) Glue Schema Registry (owned here)
        # =================================================================
        schema_registry = glue.CfnRegistry(self, "KafkaSchemaRegistry",
            name=f"{{project_name}}-kafka-schemas-{stage_name}",
            description=f"Schema Registry for {{project_name}} Kafka topics ({stage_name})",
        )
        glue.CfnSchema(self, "UserEventsSchema",
            name="user-events",
            registry=glue.CfnSchema.RegistryProperty(arn=schema_registry.attr_arn),
            data_format="AVRO",
            compatibility="BACKWARD",
            schema_definition=json.dumps({
                "type": "record",
                "name": "UserEvent",
                "namespace": "{project_name}",
                "fields": [
                    {"name": "event_id",   "type": "string"},
                    {"name": "user_id",    "type": "string"},
                    {"name": "event_type", "type": "string"},
                    {"name": "timestamp",  "type": "long", "logicalType": "timestamp-millis"},
                    {"name": "properties", "type": {"type": "map", "values": "string"}, "default": {}},
                ],
            }),
        )

        # =================================================================
        # C) Kafka admin Lambda (anchored asset, identity-side grants only)
        # =================================================================
        admin_log = logs.LogGroup(self, "KafkaAdminLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-kafka-admin-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Bootstrap servers are not directly attributes of CfnCluster; use a
        # custom resource at deploy time to resolve them, or allow the value to
        # come in via SSM from an operator-run lookup. Here we mark the env
        # var as a deploy-time SSM lookup so operators can populate it.
        bootstrap_ssm_name = f"/{{project_name}}/{stage_name}/streaming/bootstrap_servers"

        kafka_admin_fn = _lambda.Function(self, "KafkaAdminFn",
            function_name=f"{{project_name}}-kafka-admin-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "kafka_admin")),
            environment={
                "BOOTSTRAP_SERVERS_SSM": bootstrap_ssm_name,
                "CLUSTER_ARN":           msk_cluster.attr_arn,
            },
            timeout=Duration.seconds(30),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[msk_sg],
            log_group=admin_log,
        )
        # Identity-side MSK IAM auth grants (specific to this cluster's ARN)
        topic_arn_prefix = msk_cluster.attr_arn.replace(":cluster/", ":topic/")
        group_arn_prefix = msk_cluster.attr_arn.replace(":cluster/", ":group/")
        kafka_admin_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "kafka-cluster:Connect",
                "kafka-cluster:AlterCluster",
                "kafka-cluster:DescribeCluster",
            ],
            resources=[msk_cluster.attr_arn],
        ))
        kafka_admin_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "kafka-cluster:*Topic*",
                "kafka-cluster:WriteData",
                "kafka-cluster:ReadData",
                "kafka-cluster:AlterGroup",
                "kafka-cluster:DescribeGroup",
            ],
            resources=[f"{topic_arn_prefix}/*", f"{group_arn_prefix}/*"],
        ))
        # SSM read for bootstrap servers
        kafka_admin_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{Aws.ACCOUNT_ID}:parameter{bootstrap_ssm_name}"],
        ))
        iam.PermissionsBoundary.of(kafka_admin_fn.role).apply(permission_boundary)

        # =================================================================
        # D) Alarms
        # =================================================================
        for metric_name, threshold, description in [
            ("KafkaDataLogsDiskUsed", 80,     "MSK broker disk usage > 80% — add storage"),
            ("CpuUser",               60,     "MSK broker CPU > 60% — consider scale up"),
            ("EstimatedMaxTimeLag",   300000, "Consumer lag > 5 minutes — consumers falling behind"),
        ]:
            cw.Alarm(self, f"MSKAlarm{metric_name}",
                alarm_name=f"{{project_name}}-msk-{metric_name.lower()}-{stage_name}",
                alarm_description=description,
                metric=cw.Metric(
                    namespace="AWS/Kafka",
                    metric_name=metric_name,
                    dimensions_map={"Cluster Name": f"{{project_name}}-kafka-{stage_name}"},
                    period=Duration.minutes(5),
                    statistic="Maximum",
                ),
                threshold=threshold,
                evaluation_periods=3,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                alarm_actions=[cw_actions.SnsAction(alert_topic)],
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )

        # =================================================================
        # Publish consumer-facing values via SSM (no CFN exports → no cycles)
        # =================================================================
        for pid, pname, pval in [
            ("ClusterArnParam",      f"/{{project_name}}/{stage_name}/streaming/cluster_arn",      msk_cluster.attr_arn),
            ("SchemaRegistryArn",    f"/{{project_name}}/{stage_name}/streaming/schema_registry_arn", schema_registry.attr_arn),
            ("StreamingKmsArn",      f"/{{project_name}}/{stage_name}/streaming/kms_key_arn",       cmk.key_arn),
            ("MskSgId",              f"/{{project_name}}/{stage_name}/streaming/msk_sg_id",         msk_sg.security_group_id),
            ("KafkaAdminFnArn",      f"/{{project_name}}/{stage_name}/streaming/admin_fn_arn",      kafka_admin_fn.function_arn),
            ("ClusterName",          f"/{{project_name}}/{stage_name}/streaming/cluster_name",      msk_cluster.cluster_name),
        ]:
            ssm.StringParameter(self, pid, parameter_name=pname, string_value=pval)

        CfnOutput(self, "MSKClusterArn",      value=msk_cluster.attr_arn)
        CfnOutput(self, "SchemaRegistryArn",  value=schema_registry.attr_arn)
        CfnOutput(self, "KafkaAdminFnArn",    value=kafka_admin_fn.function_arn)
        CfnOutput(self, "BootstrapServersHint",
            value=f"Populate SSM {bootstrap_ssm_name} via: "
                  f"aws kafka get-bootstrap-brokers --cluster-arn {msk_cluster.attr_arn}")
```

Consumer stacks (producer Lambda / ECS consumer) read SSM and grant identity-side:

```python
# inside consumer stack (e.g. ComputeStack)
cluster_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/streaming/cluster_arn",
)
streaming_kms_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/streaming/kms_key_arn",
)
msk_sg_id = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/streaming/msk_sg_id",
)

# Open consumer SG's egress to MSK port 9098 (IAM auth) using msk_sg_id as peer
consumer_sg.add_egress_rule(
    peer=ec2.Peer.security_group_id(msk_sg_id),
    connection=ec2.Port.tcp(9098),
    description="Consumer → MSK IAM auth (cross-stack via SSM SG id)",
)

# Identity-side MSK IAM auth grants — derive topic/group ARN prefixes from cluster ARN
topic_arn = cdk.Fn.join("", [cdk.Fn.select(0, cdk.Fn.split(":cluster/", cluster_arn)),
                              ":topic/",
                              cdk.Fn.select(1, cdk.Fn.split(":cluster/", cluster_arn)),
                              "/user-events/*"])

consumer_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"],
    resources=[cluster_arn],
))
consumer_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kafka-cluster:ReadData", "kafka-cluster:DescribeTopic"],
    resources=[topic_arn],
))
consumer_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kms:Decrypt", "kms:DescribeKey"],
    resources=[streaming_kms_arn],
))
```

### 4.3 Micro-stack gotchas

- **Bootstrap servers are not a CFN attribute.** `CfnCluster` exposes `.attr_arn`, `.cluster_name`, but NOT bootstrap brokers. The operator must run `aws kafka get-bootstrap-brokers --cluster-arn <arn>` after cluster creation and populate the `/{project_name}/{stage}/streaming/bootstrap_servers` SSM parameter. Alternatively, wrap with an `AwsCustomResource` calling `kafka:GetBootstrapBrokers` — `# TODO(verify): AwsCustomResource IAM scope for kafka:GetBootstrapBrokers post-cluster-create ordering`.
- **Cross-stack SG peer via SSM** — `ec2.Peer.security_group_id(ssm_token)` accepts a CFN token, but the consumer stack cannot add an *ingress* rule to the upstream `msk_sg` (that would cycle). Instead, the consumer opens its own SG's egress toward the MSK SG ID, and the MSK SG's ingress is already open to the VPC CIDR (configured in `StreamingStack`). Fine-grained SG→SG ingress can be set with a separate `CfnSecurityGroupIngress` resource in the consumer stack using the upstream SG id as `group_id`.
- **MSK IAM resource ARNs** use the `:topic/<cluster-name>/<uuid>/<topic>` pattern — wildcards at the end scope down to a specific topic. Never use `"*"` on `kafka-cluster:*Topic*`; it covers every cluster in the account.
- **Schema Registry `CfnSchema` is immutable on `data_format`.** Changing AVRO → PROTOBUF forces resource replacement, which detaches all existing consumer bindings.
- **`permission_boundary` on the admin Lambda role** must allow `kafka-cluster:*` actions; a too-tight boundary blocks the Lambda silently (SASL auth returns generic UNKNOWN_SERVER_ERROR, not AccessDeniedException).

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| Throughput > 2 GB/s, bursty | Swap `CfnCluster` → `msk-serverless.CfnServerlessCluster` — no broker sizing, pay per throughput. Drop `enhanced_monitoring` (N/A on serverless) |
| Low throughput + Lambda-first | Swap to Kinesis Data Streams + `SqsEventSource` — see `LAYER_BACKEND_LAMBDA` §3.1 |
| Cost-constrained dev | Use `instance_type=kafka.t3.small` (available on MSK), `number_of_broker_nodes=2`, disable `enhanced_monitoring`, skip broker S3 logs |
| Confluent Schema Registry required | Replace `glue.CfnRegistry` with a Confluent Cloud connector registered via MSK Connect; keep rest of stack |
| Need Kafka Connect (S3 sink / DDB sink / JDBC source) | Add `aws_msk.CfnConnector` and a custom plugin bucket; IAM role must `iam:PassRole` to `kafkaconnect.amazonaws.com` |
| mTLS (client certificates required) | Populate `certificate_authority_arn_list=[acm_pca_arn]` and issue client certs from ACM Private CA |
| Cross-region replication | Use MSK Replicator (L1 only, `CfnReplicator`) — not MirrorMaker2 |

---

## 6. Worked example

Save as `tests/sop/test_DATA_MSK_KAFKA.py`. Offline — no AWS credentials needed.

```python
"""SOP verification — StreamingStack synthesizes with stub SSM params + boundary."""
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_sns as sns,
    aws_ssm as ssm,
)
from aws_cdk.assertions import Template, Match


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_streaming_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=3)
    topic = sns.Topic(deps, "AlertTopic")
    ssm.StringParameter(deps, "AlertTopicArn",
        parameter_name="/test/obs/alert_topic_arn",
        string_value=topic.topic_arn,
    )
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(
            actions=["*"], resources=["*"],
        )])

    from infrastructure.cdk.stacks.streaming_stack import StreamingStack
    stack = StreamingStack(
        app, stage_name="prod",
        vpc=vpc,
        alert_topic_arn_ssm="/test/obs/alert_topic_arn",
        permission_boundary=boundary,
        env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::KMS::Key",           1)
    t.resource_count_is("AWS::S3::Bucket",         1)    # broker logs
    t.resource_count_is("AWS::MSK::Cluster",       1)
    t.resource_count_is("AWS::MSK::Configuration", 1)
    t.resource_count_is("AWS::Glue::Registry",     1)
    t.resource_count_is("AWS::Glue::Schema",       1)
    t.resource_count_is("AWS::Lambda::Function",   1)    # kafka admin
    t.resource_count_is("AWS::CloudWatch::Alarm",  3)
    t.resource_count_is("AWS::SSM::Parameter",     6)    # published values
    # IAM auth is enabled and TLS encryption in transit
    t.has_resource_properties("AWS::MSK::Cluster", {
        "ClientAuthentication": Match.object_like({
            "Sasl": {"Iam": {"Enabled": True}},
        }),
        "EncryptionInfo": Match.object_like({
            "EncryptionInTransit": {"ClientBroker": "TLS", "InCluster": True},
        }),
        "KafkaVersion": "3.6.0",
    })
```

---

## 7. References

- `docs/template_params.md` — `MSK_CLUSTER_ARN_SSM`, `MSK_BOOTSTRAP_SERVERS_SSM`, `MSK_SCHEMA_REGISTRY_ARN_SSM`, `MSK_KMS_KEY_ARN_SSM`, `MSK_SG_ID_SSM`, `KAFKA_ADMIN_FN_ARN_SSM`, `KAFKA_VERSION`, `STAGE_NAME`
- `docs/Feature_Roadmap.md` — feature IDs `MSK-01..MSK-14` (cluster), `MSK-15..MSK-20` (schema registry), `MSK-21..MSK-25` (Kafka Connect)
- MSK CloudFormation: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/AWS_MSK.html
- MSK IAM auth: https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html
- Glue Schema Registry: https://docs.aws.amazon.com/glue/latest/dg/schema-registry.html
- MSK Serverless: https://docs.aws.amazon.com/msk/latest/developerguide/serverless.html
- Related SOPs: `LAYER_NETWORKING` (VPC + private subnets — MSK requires 2-3 AZs), `LAYER_SECURITY` (KMS, permission boundary), `LAYER_BACKEND_LAMBDA` (five non-negotiables, identity-side grant helpers), `DATA_LAKEHOUSE_ICEBERG` (raw zone as Kafka Connect S3 sink target), `LAYER_DATA` (DDB as consumer sink), `OPS_ADVANCED_MONITORING` (MSK CloudWatch + Prometheus)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `StreamingStack` owns local CMK (honors 5th non-negotiable), MSK `CfnCluster` + `CfnConfiguration`, broker-log S3 bucket, MSK security group, Glue Schema Registry, Kafka admin Lambda; publishes cluster ARN + schema registry ARN + KMS ARN + SG id + admin fn ARN + cluster name via SSM; consumer stacks grant identity-side `kafka-cluster:Connect/ReadData/DescribeTopic` scoped to topic/group ARN prefixes, plus `kms:Decrypt` on KMS ARN. Extracted inline admin Lambda from `Code.from_inline` to `Code.from_asset(_LAMBDAS_ROOT / "kafka_admin")` with explicit `LogGroup`. Admin Lambda reads bootstrap servers from SSM (`BOOTSTRAP_SERVERS_SSM`) rather than hardcoded env var. Added permissions boundary + `TODO(verify)` marker on `AwsCustomResource` for `kafka:GetBootstrapBrokers` ordering. Added Swap matrix (§5), Worked example (§6), Monolith gotchas (§3.4), Micro-stack gotchas (§4.3). Preserved all v1.0 content: Kafka-vs-Kinesis decision, cluster sizing guide, `CfnConfiguration` server properties, full `CfnCluster` with IAM SASL + TLS + KMS at rest + Prometheus + CloudWatch/S3 logging, Glue Schema Registry + user-events Avro schema, admin Lambda handler, three alarms. |
| 1.0 | 2026-03-05 | Initial monolith — MSK provisioned cluster 3.6.0 with IAM SASL + TLS, `CfnConfiguration` with production-safe broker settings, Glue Schema Registry + example Avro `user-events` schema, kafka-python admin Lambda with create/list topic actions, three CloudWatch alarms (disk / CPU / consumer lag), broker logs to CloudWatch + S3 (prod). |
