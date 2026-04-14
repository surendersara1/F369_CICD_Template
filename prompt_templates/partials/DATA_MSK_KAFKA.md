# PARTIAL: MSK/Kafka Streaming — Amazon Managed Streaming for Kafka

**Usage:** Include when SOW mentions Apache Kafka, MSK, event streaming, Schema Registry, Kafka Connect, exactly-once delivery, or high-throughput event pipelines.

---

## Kafka vs Kinesis Decision

| Factor                           | Use MSK (Kafka) | Use Kinesis      |
| -------------------------------- | --------------- | ---------------- |
| Team knows Kafka                 | ✅              | ❌               |
| Kafka Connect ecosystem needed   | ✅              | ❌               |
| Schema Registry + Avro/Protobuf  | ✅              | ❌               |
| Exactly-once semantics           | ✅ Strong       | ⚠️ At-least-once |
| Long message retention (>7 days) | ✅ Unlimited    | ❌ 7 days max    |
| Simplicity + Lambda integration  | ❌              | ✅               |
| Cost at low scale                | ❌ Expensive    | ✅ Cheap         |

---

## CDK Code Block — MSK Kafka Cluster

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

    import aws_cdk.aws_msk as msk
    import aws_cdk.aws_glue as glue

    # =========================================================================
    # MSK CLUSTER SIZING GUIDE
    # [Claude: pick based on Architecture Map throughput requirements]
    # =========================================================================
    # Low throughput   (<100 MB/s):   2 brokers, kafka.m5.large ($0.19/hr/broker)
    # Medium throughput(<500 MB/s):  3 brokers, kafka.m5.2xlarge
    # High throughput  (<2 GB/s):    6 brokers, kafka.m5.4xlarge
    # Very high        (>2 GB/s):    MSK Serverless (pay per throughput, no provisioning)
    # =========================================================================

    # Security group for MSK brokers
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

        tags={"Project": "{{project_name}}", "Stage": stage_name},
    )

    # =========================================================================
    # B) GLUE SCHEMA REGISTRY (Schema Registry for Kafka — Avro, Protobuf, JSON Schema)
    # =========================================================================

    schema_registry = glue.CfnRegistry(
        self, "KafkaSchemaRegistry",
        name=f"{{project_name}}-kafka-schemas-{stage_name}",
        description=f"Schema Registry for {{project_name}} Kafka topics ({stage_name})",
        tags=[{"key": "Project", "value": "{{project_name}}"}],
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
            "namespace": "{{project_name}}",
            "fields": [
                {"name": "event_id",   "type": "string"},
                {"name": "user_id",    "type": "string"},
                {"name": "event_type", "type": "string"},
                {"name": "timestamp",  "type": "long", "logicalType": "timestamp-millis"},
                {"name": "properties", "type": {"type": "map", "values": "string"}, "default": {}},
            ]
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
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from kafka.admin import KafkaAdminClient, NewTopic
from kafka import KafkaProducer

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
                'retention.ms': str(event.get('retention_hours', 168) * 3600 * 1000),
                'compression.type': event.get('compression', 'lz4'),
                'min.insync.replicas': '2',
            }
        )]
        result = admin.create_topics(new_topics=topics, validate_only=False)
        logger.info(f"Created topic: {event['topic_name']}")
        return {"status": "created", "topic": event['topic_name']}

    elif action == 'list_topics':
        admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP_SERVERS,
                                  security_protocol='SASL_SSL', sasl_mechanism='OAUTHBEARER')
        return {"topics": list(admin.list_topics())}
"""),
        environment={
            "BOOTSTRAP_SERVERS": f"{{project_name}}-kafka-{stage_name}.PLACEHOLDER",  # [Claude: set from cluster bootstrap string]
        },
        timeout=Duration.seconds(30),
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[msk_sg],
    )

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
