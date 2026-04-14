# PARTIAL: Data Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_data_layer()` method body.

---

## When to Include Each Data Construct

| SOW Signal                                  | Include              |
| ------------------------------------------- | -------------------- |
| "SQL", "relational", "transactions", "ACID" | Aurora Serverless V2 |
| "NoSQL", "key-value", "session", "metadata" | DynamoDB             |
| "cache", "low latency", "session store"     | ElastiCache Valkey Serverless (or Redis OSS) |
| "search", "full-text", "faceted search"     | OpenSearch Service   |
| "data lake", "analytics", "Athena", "Glue"  | S3 + Glue + Athena   |
| "message queue", "async", "decouple"        | SQS                  |
| "event streaming", "Kinesis"                | Kinesis Data Streams |

---

## CDK Code Block — Data Layer

```python
def _create_data_layer(self, stage_name: str) -> None:
    """
    Layer 2: Data Infrastructure

    Components (include based on Architecture Map):
      A) Aurora Serverless V2 (PostgreSQL)  — relational/SQL data
      B) DynamoDB Tables                    — NoSQL/fast lookup data
      C) ElastiCache Valkey/Redis          — caching/sessions
      D) S3 Data Bucket                     — file storage / data lake
      E) SQS Queues                         — async message passing

    All data stores are:
      - Encrypted at rest (KMS)
      - Encrypted in transit (TLS)
      - PITR / backup enabled
      - In isolated subnets (no public access)
    """

    # =========================================================================
    # A) AURORA SERVERLESS V2 (PostgreSQL)
    # Remove if no relational DB detected in Architecture Map
    # =========================================================================

    # Security group for Aurora — only allow Lambda/ECS to connect
    self.aurora_sg = ec2.SecurityGroup(
        self, "AuroraSG",
        vpc=self.vpc,
        description="Security group for Aurora cluster",
        allow_all_outbound=False,  # Least privilege
    )

    # Database credentials stored in Secrets Manager (never hardcoded)
    self.db_secret = sm.Secret(
        self, "AuroraSecret",
        secret_name=f"/{{project_name}}/{stage_name}/aurora/credentials",
        description="Aurora PostgreSQL master credentials",
        generate_secret_string=sm.SecretStringGenerator(
            secret_string_template='{"username": "postgres"}',
            generate_string_key="password",
            exclude_characters="\"@/\\ '",
            password_length=32,
        ),
    )

    # Aurora Serverless V2 cluster
    # ACU ranges scale per environment
    aurora_min_acu, aurora_max_acu = {
        "dev":     (0.5, 1.0),   # Scale to zero when idle
        "staging": (0.5, 4.0),
        "prod":    (1.0, 16.0),  # Always-on minimum
    }.get(stage_name, (0.5, 1.0))

    self.aurora_cluster = rds.DatabaseCluster(
        self, "AuroraCluster",
        cluster_identifier=f"{{project_name}}-{stage_name}",

        engine=rds.DatabaseClusterEngine.aurora_postgresql(
            version=rds.AuroraPostgresEngineVersion.VER_16_6,
        ),

        # Serverless V2 writer instance
        writer=rds.ClusterInstance.serverless_v2(
            "Writer",
            scale_with_writer=True,
        ),

        # Add reader in prod for read scaling
        readers=[
            rds.ClusterInstance.serverless_v2(
                "Reader",
                scale_with_writer=True,
            )
        ] if stage_name == "prod" else [],

        serverless_v2_min_capacity=aurora_min_acu,
        serverless_v2_max_capacity=aurora_max_acu,

        # Networking: isolated subnets (NO internet access)
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
        ),
        security_groups=[self.aurora_sg],

        # Authentication
        credentials=rds.Credentials.from_secret(self.db_secret),

        # Security
        storage_encrypted=True,
        storage_encryption_key=self.kms_key,

        # Availability (multi-AZ in prod)
        availability_zones=self.availability_zones[:2] if stage_name != "prod" else self.availability_zones[:3],

        # Backup & Recovery
        backup=rds.BackupProps(
            retention=Duration.days(7) if stage_name != "prod" else Duration.days(35),
            preferred_window="03:00-04:00",
        ),

        # Auto-pause (dev only — save costs)
        # Note: Serverless V2 doesn't have pause — use min 0 ACU for similar effect

        # Protection
        deletion_protection=stage_name == "prod",
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.SNAPSHOT,

        # Parameter group for optimization
        parameter_group=rds.ParameterGroup.from_parameter_group_name(
            self, "AuroraParams",
            parameter_group_name="default.aurora-postgresql16",
        ),

        # Enhanced monitoring
        monitoring_interval=Duration.seconds(60),
        monitoring_role=iam.Role(
            self, "AuroraMonitoringRole",
            assumed_by=iam.ServicePrincipal("monitoring.rds.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonRDSEnhancedMonitoringRole")
            ],
        ),

        # CloudWatch logs
        cloudwatch_logs_exports=["postgresql"],
        cloudwatch_logs_retention=logs.RetentionDays.ONE_MONTH,
    )

    # =========================================================================
    # B) DYNAMODB TABLES
    # Define one entry per detected data entity from Architecture Map Section 5
    # =========================================================================

    # Master configuration for all DynamoDB tables
    # [Claude: Replace with actual tables from Architecture Map Section 5]
    DYNAMODB_TABLES = [
        {
            "id": "MainTable",
            "table_name": f"{{project_name}}-main-{stage_name}",
            "pk": "pk",
            "sk": "sk",
            "gsi": [
                # GSI for query by email
                {"name": "GSI1", "pk": "gsi1pk", "sk": "gsi1sk"},
            ],
            "ttl_attribute": "ttl",  # Set to None if no TTL needed
            "stream": ddb.StreamViewType.NEW_AND_OLD_IMAGES,  # For DynamoDB Streams
        },
        # Add more tables from Architecture Map Section 5 here
    ]

    self.ddb_tables: Dict[str, ddb.Table] = {}

    for table_config in DYNAMODB_TABLES:
        table = ddb.Table(
            self, table_config["id"],
            table_name=table_config["table_name"],

            # Key schema
            partition_key=ddb.Attribute(name=table_config["pk"], type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name=table_config["sk"], type=ddb.AttributeType.STRING) if table_config.get("sk") else None,

            # Billing: on-demand (dev/staging), provisioned (prod)
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST if stage_name != "prod" else ddb.BillingMode.PROVISIONED,

            # Provisioned capacity (prod only)
            read_capacity=5 if stage_name == "prod" else None,
            write_capacity=5 if stage_name == "prod" else None,

            # Auto-scaling (prod only)
            # [Apply via table.auto_scale_read_capacity() after creation]

            # Encryption at rest
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,

            # Point-in-time recovery
            point_in_time_recovery=True,

            # Streams (for event-driven patterns)
            stream=table_config.get("stream"),

            # TTL (for auto-expiring records)
            time_to_live_attribute=table_config.get("ttl_attribute"),

            # Protection
            removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        )

        # Add Global Secondary Indexes
        for gsi in table_config.get("gsi", []):
            table.add_global_secondary_index(
                index_name=gsi["name"],
                partition_key=ddb.Attribute(name=gsi["pk"], type=ddb.AttributeType.STRING),
                sort_key=ddb.Attribute(name=gsi["sk"], type=ddb.AttributeType.STRING) if gsi.get("sk") else None,
            )

        # Auto-scaling for prod
        if stage_name == "prod":
            read_scaling = table.auto_scale_read_capacity(min_capacity=5, max_capacity=100)
            read_scaling.scale_on_utilization(target_utilization_percent=70)
            write_scaling = table.auto_scale_write_capacity(min_capacity=5, max_capacity=100)
            write_scaling.scale_on_utilization(target_utilization_percent=70)

        self.ddb_tables[table_config["id"]] = table

    # =========================================================================
    # C) ELASTICACHE (Valkey Serverless or Redis OSS)
    # AWS recommends Valkey (Redis-compatible fork) as the default engine.
    # ElastiCache Serverless auto-scales and requires no node management.
    # Include if caching/sessions detected in Architecture Map L2
    # =========================================================================

    self.redis_sg = ec2.SecurityGroup(
        self, "RedisSG",
        vpc=self.vpc,
        description="Security group for ElastiCache",
        allow_all_outbound=False,
    )

    # Option A: ElastiCache Serverless (recommended — auto-scales, no node management)
    # self.cache = elasticache.CfnServerlessCache(
    #     self, "CacheServerless",
    #     serverless_cache_name=f"{{project_name}}-cache-{stage_name}",
    #     engine="valkey",  # or "redis" for Redis OSS compatibility
    #     security_group_ids=[self.redis_sg.security_group_id],
    #     subnet_ids=[subnet.subnet_id for subnet in self.vpc.isolated_subnets],
    # )

    # Option B: ElastiCache Replication Group (node-based, more control)
    redis_subnet_group = elasticache.CfnSubnetGroup(
        self, "RedisSubnetGroup",
        description="Subnet group for ElastiCache",
        subnet_ids=[subnet.subnet_id for subnet in self.vpc.isolated_subnets],
    )

    self.redis_cluster = elasticache.CfnReplicationGroup(
        self, "Redis",
        replication_group_description=f"{{project_name}} cache {stage_name}",

        # Engine: Valkey (recommended) or Redis OSS
        engine="valkey",  # [Claude: use "redis" if SOW specifically requires Redis OSS]
        engine_version="8.0",
        cache_node_type="cache.t4g.micro" if stage_name == "dev" else "cache.r7g.large",

        # Cluster configuration
        num_cache_clusters=1 if stage_name == "dev" else 2,  # Multi-AZ in prod/staging

        # Networking
        cache_subnet_group_name=redis_subnet_group.ref,
        security_group_ids=[self.redis_sg.security_group_id],

        # Security
        at_rest_encryption_enabled=True,
        transit_encryption_enabled=True,

        # Auth token (in Secrets Manager)
        # auth_token=self.redis_auth_token.secret_value.to_string(),

        # Backups
        snapshot_retention_limit=1 if stage_name != "prod" else 7,
        snapshot_window="04:00-05:00",

        # Auto-failover (prod only)
        automatic_failover_enabled=stage_name == "prod",
        multi_az_enabled=stage_name == "prod",
    )

    # =========================================================================
    # D) S3 DATA BUCKET (for file uploads, exports, data lake)
    # =========================================================================

    self.data_bucket = s3.Bucket(
        self, "DataBucket",
        bucket_name=f"{{project_name}}-data-{stage_name}-{self.account}",

        # Security
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,

        # Versioning
        versioned=True,

        # Lifecycle rules for cost optimization
        lifecycle_rules=[
            s3.LifecycleRule(
                id="MoveToIA",
                enabled=True,
                transitions=[
                    s3.Transition(
                        storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                        transition_after=Duration.days(30),
                    ),
                    s3.Transition(
                        storage_class=s3.StorageClass.GLACIER,
                        transition_after=Duration.days(90),
                    ),
                ],
                expiration=Duration.days(365 * 7) if stage_name == "prod" else Duration.days(90),
            ),
        ],

        # Event notifications for processing triggers
        event_bridge_enabled=True,

        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        auto_delete_objects=stage_name != "prod",
    )

    # =========================================================================
    # E) SQS QUEUES (for async processing)
    # =========================================================================

    # Dead Letter Queue (for failed messages)
    self.dlq = sqs.Queue(
        self, "DLQ",
        queue_name=f"{{project_name}}-dlq-{stage_name}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
        removal_policy=RemovalPolicy.DESTROY,
    )

    # Main processing queue
    self.main_queue = sqs.Queue(
        self, "MainQueue",
        queue_name=f"{{project_name}}-queue-{stage_name}",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,

        # Visibility timeout: must be >= Lambda/ECS processing time
        visibility_timeout=Duration.seconds(300),

        # Retention
        retention_period=Duration.days(4),

        # Dead letter queue configuration
        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=3,  # Retry 3x before DLQ
            queue=self.dlq,
        ),

        removal_policy=RemovalPolicy.DESTROY,
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    CfnOutput(self, "AuroraEndpoint",
        value=self.aurora_cluster.cluster_endpoint.socket_address,
        description="Aurora cluster endpoint",
        export_name=f"{{project_name}}-aurora-endpoint-{stage_name}",
    )

    CfnOutput(self, "AuroraSecretArn",
        value=self.db_secret.secret_arn,
        description="Aurora credentials secret ARN",
        export_name=f"{{project_name}}-aurora-secret-{stage_name}",
    )

    CfnOutput(self, "DataBucketName",
        value=self.data_bucket.bucket_name,
        description="S3 data bucket name",
        export_name=f"{{project_name}}-data-bucket-{stage_name}",
    )

    CfnOutput(self, "MainQueueUrl",
        value=self.main_queue.queue_url,
        description="Main SQS queue URL",
        export_name=f"{{project_name}}-queue-url-{stage_name}",
    )
```
