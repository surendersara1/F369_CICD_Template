# PARTIAL: Backend Lambda + ECS Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_backend()` method body.

---

## CDK Code Block — Backend Layer (Lambda Microservices Loop + ECS Fargate)

```python
def _create_backend(self, stage_name: str) -> None:
    """
    Layer 3: Backend Compute

    A) Lambda Microservices — loop pattern (one function per detected service)
    B) ECS Fargate Workers — long-running/heavy processing tasks
    C) EventBridge Schedulers — cron/scheduled Lambda triggers

    All Lambda functions share:
      - VPC placement (private subnets)
      - KMS-encrypted CloudWatch log groups
      - X-Ray active tracing
      - Environment variables (from outputs of prior layers)
      - IAM grants via CDK grant_* methods (no manual policy statements)
    """

    # =========================================================================
    # SHARED LAMBDA CONFIGURATION
    # =========================================================================

    # Lambda execution role (shared base, each Lambda gets its own for isolation)
    # Using per-function roles is safer, but shared role shown here for brevity
    # [Claude: use per-function roles for HIPAA/SOC2 projects]

    # Environment variables shared across all Lambdas
    # Individual Lambdas may add more via lambda_env_overrides
    shared_lambda_env = {
        "STAGE": stage_name,
        "TABLE_NAME": list(self.ddb_tables.values())[0].table_name,  # Primary table
        "QUEUE_URL": self.main_queue.queue_url,
        "DATA_BUCKET": self.data_bucket.bucket_name,
        # NOTE: Aurora endpoint passed as secret, not plaintext env var
        # DB credentials fetched from Secrets Manager at runtime
        "DB_SECRET_ARN": self.db_secret.secret_arn,
    }

    # Lambda layer for common utilities (boto3, powertools, etc.)
    self.common_layer = _lambda.LayerVersion(
        self, "CommonLayer",
        layer_version_name=f"{{project_name}}-common-{stage_name}",
        code=_lambda.Code.from_asset("layers/common"),
        compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
        description="Common dependencies: aws-lambda-powertools, boto3",
        removal_policy=RemovalPolicy.DESTROY,
    )

    # =========================================================================
    # MICROSERVICES DEFINITION TABLE
    # [Claude: populate this from Architecture Map Section 2 DETECTED MICROSERVICES]
    # =========================================================================
    MICROSERVICES = [
        {
            "id": "AuthService",
            "name": "auth-service",
            "handler": "index.handler",
            "code_path": "src/auth_service",
            "memory": 512,
            "timeout_seconds": 29,       # API Gateway limit
            "reserved_concurrency": 50,  # Prevent Lambda storms
            "env_overrides": {},
            "data_grants": [],           # List of "table_grant_read", "table_grant_write", etc.
            "trigger": "api_gateway",    # api_gateway | sqs | eventbridge | none
        },
        {
            "id": "PatientList",
            "name": "patient-list",
            "handler": "index.handler",
            "code_path": "src/patient_list",
            "memory": 512,
            "timeout_seconds": 29,
            "reserved_concurrency": 100,
            "env_overrides": {},
            "data_grants": ["aurora_read", "ddb_read"],
            "trigger": "api_gateway",
        },
        {
            "id": "PatientCreate",
            "name": "patient-create",
            "handler": "index.handler",
            "code_path": "src/patient_create",
            "memory": 512,
            "timeout_seconds": 29,
            "reserved_concurrency": 50,
            "env_overrides": {},
            "data_grants": ["aurora_write", "ddb_write"],
            "trigger": "api_gateway",
        },
        {
            "id": "DocumentUpload",
            "name": "document-upload",
            "handler": "index.handler",
            "code_path": "src/document_upload",
            "memory": 1024,
            "timeout_seconds": 60,
            "reserved_concurrency": 50,
            "env_overrides": {"SCAN_QUEUE_URL": "placeholder_set_below"},
            "data_grants": ["s3_data_write", "ddb_write", "sqs_send"],
            "trigger": "api_gateway",
        },
        {
            "id": "VirusScanner",
            "name": "virus-scanner",
            "handler": "index.handler",
            "code_path": "src/virus_scanner",
            "memory": 1024,
            "timeout_seconds": 300,
            "reserved_concurrency": 10,  # Limit concurrent scans
            "env_overrides": {},
            "data_grants": ["s3_data_read_write", "ddb_write"],
            "trigger": "sqs",
        },
        {
            "id": "EhrSyncEpic",
            "name": "ehr-sync-epic",
            "handler": "index.handler",
            "code_path": "src/ehr_sync_epic",
            "memory": 512,
            "timeout_seconds": 900,  # 15 minutes — long sync
            "reserved_concurrency": 2,   # Prevent concurrent runs
            "env_overrides": {"EHR_SECRET_ARN": "epic-secret-arn"},
            "data_grants": ["aurora_write", "secrets_read"],
            "trigger": "eventbridge",
        },
        {
            "id": "AuditAggregator",
            "name": "audit-aggregator",
            "handler": "index.handler",
            "code_path": "src/audit_aggregator",
            "memory": 1024,
            "timeout_seconds": 900,
            "reserved_concurrency": 1,   # Only 1 concurrent run (nightly batch)
            "env_overrides": {},
            "data_grants": ["ddb_read", "aurora_write"],
            "trigger": "eventbridge",
        },
        # [Claude: Add all other microservices from Architecture Map Section 2]
    ]

    # =========================================================================
    # MICROSERVICES LOOP — Create all Lambda functions
    # =========================================================================

    self.lambda_functions: Dict[str, _lambda.Function] = {}

    for svc in MICROSERVICES:

        # Dedicated CloudWatch log group per function
        log_group = logs.LogGroup(
            self, f"{svc['id']}LogGroup",
            log_group_name=f"/aws/lambda/{{project_name}}-{svc['name']}-{stage_name}",
            retention=logs.RetentionDays.ONE_WEEK if stage_name == "dev" else logs.RetentionDays.ONE_MONTH,
            encryption_key=self.kms_key,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Lambda function
        fn = _lambda.Function(
            self, f"{svc['id']}Fn",
            function_name=f"{{project_name}}-{svc['name']}-{stage_name}",

            # Runtime
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler=svc["handler"],
            code=_lambda.Code.from_asset(svc["code_path"]),

            # Layers
            layers=[self.common_layer],

            # Compute
            memory_size=svc["memory"] if stage_name != "dev" else min(svc["memory"], 512),
            timeout=Duration.seconds(svc["timeout_seconds"]),

            # Concurrency control
            reserved_concurrent_executions=svc["reserved_concurrency"],

            # Networking (VPC placement)
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.lambda_sg],

            # Environment variables
            environment={
                **shared_lambda_env,
                **svc.get("env_overrides", {}),
            },

            # Observability
            tracing=_lambda.Tracing.ACTIVE,          # X-Ray distributed tracing
            log_group=log_group,

            # Security
            # role is auto-created per-function with least-privilege

            # Architecture (ARM64 = Graviton3, ~20% cheaper + faster)
            architecture=_lambda.Architecture.ARM_64,

            # Environment encryption
            environment_encryption=self.kms_key,
        )

        # =====================================================================
        # IAM GRANTS (per service configuration)
        # =====================================================================
        # Use grant_* instead of manual PolicyStatements

        if "aurora_read" in svc.get("data_grants", []):
            self.aurora_cluster.grant_connect(fn, "read_user")  # Requires IAM auth on Aurora
            self.db_secret.grant_read(fn)

        if "aurora_write" in svc.get("data_grants", []):
            self.aurora_cluster.grant_connect(fn, "app_user")
            self.db_secret.grant_read(fn)

        if "ddb_read" in svc.get("data_grants", []):
            for table in self.ddb_tables.values():
                table.grant_read_data(fn)

        if "ddb_write" in svc.get("data_grants", []):
            for table in self.ddb_tables.values():
                table.grant_read_write_data(fn)

        if "s3_data_read_write" in svc.get("data_grants", []):
            self.data_bucket.grant_read_write(fn)

        if "s3_data_write" in svc.get("data_grants", []):
            self.data_bucket.grant_write(fn)

        if "sqs_send" in svc.get("data_grants", []):
            self.main_queue.grant_send_messages(fn)

        if "secrets_read" in svc.get("data_grants", []):
            self.db_secret.grant_read(fn)
            # [Claude: add other secrets here based on Architecture Map]

        self.lambda_functions[svc["id"]] = fn

    # =========================================================================
    # SQS TRIGGERS (event sources for queue-triggered Lambdas)
    # =========================================================================

    # Attach SQS event source to virus-scanner Lambda
    self.lambda_functions["VirusScanner"].add_event_source(
        lambda_events.SqsEventSource(
            self.main_queue,  # Replace with scan_queue when defined
            batch_size=1,
            max_batching_window=Duration.seconds(0),
            report_batch_item_failures=True,  # Partial batch failure handling
        )
    )

    # =========================================================================
    # EVENTBRIDGE SCHEDULERS (for cron-triggered Lambdas)
    # =========================================================================

    # EHR Sync — every 30 minutes
    events.Rule(
        self, "EhrSyncSchedule",
        rule_name=f"{{project_name}}-ehr-sync-{stage_name}",
        schedule=events.Schedule.rate(Duration.minutes(30)),
        enabled=stage_name != "dev",  # Disable in dev to avoid API costs
        targets=[
            targets.LambdaFunction(self.lambda_functions["EhrSyncEpic"])
        ],
    )

    # Nightly Audit Aggregation — 2am UTC
    events.Rule(
        self, "AuditAggregatorSchedule",
        rule_name=f"{{project_name}}-audit-aggregator-{stage_name}",
        schedule=events.Schedule.cron(hour="2", minute="0"),
        enabled=True,
        targets=[
            targets.LambdaFunction(self.lambda_functions["AuditAggregator"])
        ],
    )

    # =========================================================================
    # ECS FARGATE — PDF Report Generator
    # (Long-running: >15 min, memory-intensive: 2GB+)
    # =========================================================================

    # ECS Cluster
    self.ecs_cluster = ecs.Cluster(
        self, "WorkerCluster",
        cluster_name=f"{{project_name}}-workers-{stage_name}",
        vpc=self.vpc,
        container_insights=True,
        enable_fargate_capacity_providers=True,
    )

    # ECS Task Role (what the container can DO)
    ecs_task_role = iam.Role(
        self, "ECSTaskRole",
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        role_name=f"{{project_name}}-ecs-task-role-{stage_name}",
    )
    self.data_bucket.grant_read_write(ecs_task_role)
    self.aurora_cluster.grant_connect(ecs_task_role, "report_user")
    self.db_secret.grant_read(ecs_task_role)
    self.main_queue.grant_consume_messages(ecs_task_role)

    # ECS Execution Role (what ECS control plane can do to RUN the task)
    ecs_execution_role = iam.Role(
        self, "ECSExecutionRole",
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy"),
        ],
    )
    self.kms_key.grant_decrypt(ecs_execution_role)

    # Task Definition
    task_cpu, task_memory = {
        "dev": (512, 1024),
        "staging": (1024, 2048),
        "prod": (2048, 4096),
    }.get(stage_name, (512, 1024))

    task_def = ecs.FargateTaskDefinition(
        self, "PDFGeneratorTask",
        family=f"{{project_name}}-pdf-generator-{stage_name}",
        task_role=ecs_task_role,
        execution_role=ecs_execution_role,
        cpu=task_cpu,
        memory_limit_mib=task_memory,
        runtime_platform=ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,  # Graviton3 (cheaper)
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        ),
    )

    # Log group for ECS container
    ecs_log_group = logs.LogGroup(
        self, "ECSLogGroup",
        log_group_name=f"/ecs/{{project_name}}-pdf-generator-{stage_name}",
        retention=logs.RetentionDays.ONE_WEEK if stage_name == "dev" else logs.RetentionDays.ONE_MONTH,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # Container definition
    task_def.add_container(
        "PDFGeneratorContainer",
        container_name="pdf-generator",
        image=ecs.ContainerImage.from_asset("src/worker_task"),

        environment={
            "STAGE": stage_name,
            "QUEUE_URL": self.main_queue.queue_url,
            "DATA_BUCKET": self.data_bucket.bucket_name,
        },

        secrets={
            # Pull from Secrets Manager — value injected as env var at runtime
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(self.db_secret, "password"),
            "DB_HOST": ecs.Secret.from_secrets_manager(self.db_secret, "host"),
        },

        logging=ecs.LogDrivers.aws_logs(
            stream_prefix="pdf-generator",
            log_group=ecs_log_group,
        ),

        health_check=ecs.HealthCheck(
            command=["CMD-SHELL", "python -c 'import sys; sys.exit(0)'"],
            interval=Duration.seconds(30),
            timeout=Duration.seconds(10),
            retries=3,
            start_period=Duration.seconds(60),
        ),

        readonly_root_filesystem=True,  # Security hardening
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    CfnOutput(self, "ECSClusterName",
        value=self.ecs_cluster.cluster_name,
        description="ECS cluster for long-running workers",
        export_name=f"{{project_name}}-ecs-cluster-{stage_name}",
    )

    # Export Lambda ARNs for cross-stack reference
    for service_id, fn in self.lambda_functions.items():
        CfnOutput(self, f"{service_id}Arn",
            value=fn.function_arn,
            description=f"ARN for {service_id} Lambda function",
        )
```
