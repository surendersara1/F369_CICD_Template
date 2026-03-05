# PARTIAL: ECS Fargate Long-Running Task Patterns

**Usage:** Referenced when Architecture Map Section 2 contains services marked `long_running: true`.

---

## When to Use ECS Fargate vs Lambda

| Criteria           | Lambda                                     | ECS Fargate                                  |
| ------------------ | ------------------------------------------ | -------------------------------------------- |
| Max execution time | 15 minutes                                 | Unlimited                                    |
| Memory             | Up to 10GB                                 | Up to 120GB                                  |
| Startup time       | Milliseconds (warm)                        | 30-90 seconds                                |
| Trigger types      | Event-driven                               | Poll-based (SQS), scheduled, manual          |
| Cost model         | Per-invocation                             | Per-second of runtime                        |
| Best for           | API handlers, event processors, short jobs | PDF gen, ML inference, ETL, video processing |

**Rule of thumb from Architecture Map:**

- Duration > 15 min → **ECS Fargate**
- Memory > 10GB → **ECS Fargate**
- SOW says "heavy processing", "batch", "report generation" → **ECS Fargate**

---

## CDK Code Block — ECS Fargate Worker Pattern

```python
def _create_ecs_workers(self, stage_name: str) -> None:
    """
    ECS Fargate workers for long-running tasks.

    Pattern: SQS Queue → ECS Task (triggered by SQS message or schedule)

    Each worker:
      - Polls SQS queue in a loop
      - Processes one job per container lifecycle
      - Exits 0 on success, non-zero on failure
      - Auto-restart handled by ECS task definition

    Workers defined in WORKER_DEFINITIONS list below.
    [Claude: populate from Architecture Map Section 2, long_running=true rows]
    """

    # =========================================================================
    # WORKER DEFINITIONS
    # [Claude: one entry per detected long-running task from Architecture Map]
    # =========================================================================
    WORKER_DEFINITIONS = [
        {
            "id": "PdfGenerator",
            "name": "pdf-generator",
            "docker_asset": "src/workers/pdf_generator",
            "cpu": {"dev": 512, "staging": 1024, "prod": 2048},
            "memory_mb": {"dev": 1024, "staging": 2048, "prod": 4096},
            "queue_source": "report_queue",   # key in self.sqs_queues dict
            "data_grants": ["aurora_read", "s3_write", "secrets_read"],
            "env_overrides": {"OUTPUT_BUCKET": "reports"},
        },
        # Add more workers here for each detected long-running service
        # Example: ETL worker, video transcoder, ML inference batch job, etc.
    ]

    self.ecs_task_definitions: Dict[str, ecs.FargateTaskDefinition] = {}

    for worker in WORKER_DEFINITIONS:

        # -----------------------------------------------------------------
        # IAM ROLES
        # -----------------------------------------------------------------

        # Task Role: what the CONTAINER can do (business logic permissions)
        task_role = iam.Role(
            self, f"{worker['id']}TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            role_name=f"{{project_name}}-{worker['name']}-task-{stage_name}",
        )

        # Task Execution Role: what ECS CONTROL PLANE can do (pull image, write logs)
        execution_role = iam.Role(
            self, f"{worker['id']}ExecRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            role_name=f"{{project_name}}-{worker['name']}-exec-{stage_name}",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        # Decrypt secrets at container start
        self.kms_key.grant_decrypt(execution_role)

        # Apply data grants from worker config
        if "aurora_read" in worker.get("data_grants", []):
            self.aurora_cluster.grant_connect(task_role)
            self.db_secret.grant_read(task_role)
        if "s3_write" in worker.get("data_grants", []):
            self.data_bucket.grant_write(task_role)
        if "s3_read_write" in worker.get("data_grants", []):
            self.data_bucket.grant_read_write(task_role)
        if "secrets_read" in worker.get("data_grants", []):
            self.db_secret.grant_read(task_role)
        if "sqs_consume" in worker.get("data_grants", []):
            self.sqs_queues.get(worker["queue_source"], self.main_queue).grant_consume_messages(task_role)

        # -----------------------------------------------------------------
        # LOG GROUP
        # -----------------------------------------------------------------
        log_group = logs.LogGroup(
            self, f"{worker['id']}LogGroup",
            log_group_name=f"/ecs/{{project_name}}-{worker['name']}-{stage_name}",
            retention=logs.RetentionDays.ONE_WEEK if stage_name == "dev" else logs.RetentionDays.ONE_MONTH,
            encryption_key=self.kms_key,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -----------------------------------------------------------------
        # TASK DEFINITION
        # -----------------------------------------------------------------
        cpu = worker["cpu"].get(stage_name, worker["cpu"]["dev"])
        memory_mb = worker["memory_mb"].get(stage_name, worker["memory_mb"]["dev"])

        task_def = ecs.FargateTaskDefinition(
            self, f"{worker['id']}TaskDef",
            family=f"{{project_name}}-{worker['name']}-{stage_name}",
            task_role=task_role,
            execution_role=execution_role,
            cpu=cpu,
            memory_limit_mib=memory_mb,

            # ARM64 (Graviton3): ~20% cheaper for same performance
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # -----------------------------------------------------------------
        # CONTAINER DEFINITION
        # -----------------------------------------------------------------
        container = task_def.add_container(
            f"{worker['id']}Container",
            container_name=worker["name"],

            # Build Docker image from local source
            image=ecs.ContainerImage.from_asset(
                worker["docker_asset"],
                # Platform must match runtime_platform above
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),

            # Environment variables (non-sensitive)
            environment={
                "STAGE": stage_name,
                "QUEUE_URL": self.main_queue.queue_url,  # Override per-worker below
                "AWS_DEFAULT_REGION": self.region,
                **{k: v for k, v in worker.get("env_overrides", {}).items()},
            },

            # Secrets (pulled from Secrets Manager at container start — NEVER in env plaintext)
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(self.db_secret, "password"),
                "DB_HOST": ecs.Secret.from_secrets_manager(self.db_secret, "host"),
                "DB_PORT": ecs.Secret.from_secrets_manager(self.db_secret, "port"),
                "DB_NAME": ecs.Secret.from_secrets_manager(self.db_secret, "dbname"),
            },

            # Logging: CloudWatch Logs
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=worker["name"],
                log_group=log_group,
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,  # Non-blocking prevents log backpressure
                max_buffer_size=Size.mebibytes(25),
            ),

            # Health check: simple Python process check
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "python -c 'import sys; sys.exit(0)' || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(10),
                retries=3,
                start_period=Duration.seconds(60),  # Grace period for startup
            ),

            # Read-only root filesystem (security hardening)
            readonly_root_filesystem=True,

            # Resource limits (prevent container from consuming all task resources)
            # Useful if task has multiple containers (sidecar pattern)
        )

        self.ecs_task_definitions[worker["id"]] = task_def

    # =========================================================================
    # FARGATE SCHEDULED TASKS (for time-triggered workers without a queue)
    # =========================================================================
    # Example: Nightly batch job using ECS Fargate (not Lambda — needs >15min)

    # events.Rule(
    #     self, "NightlyBatchRule",
    #     rule_name=f"{{project_name}}-nightly-batch-{stage_name}",
    #     schedule=events.Schedule.cron(hour="1", minute="0"),
    #     targets=[
    #         targets.EcsTask(
    #             cluster=self.ecs_cluster,
    #             task_definition=self.ecs_task_definitions["NightlyBatch"],
    #             subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
    #             security_groups=[self.ecs_sg],
    #             platform_version=ecs.FargatePlatformVersion.LATEST,
    #         )
    #     ],
    # )

    # =========================================================================
    # QUEUE-BASED WORKER TRIGGER (Lambda → SQS → ECS)
    # =========================================================================
    # When a Lambda puts a message in the queue, a separate process needs to
    # START an ECS task for each message. Options:
    #
    # Option A: Lambda trigger (reads SQS, starts ECS task via boto3)
    #           Good for: low volume, simple use cases
    #
    # Option B: EventBridge Pipe (SQS → ECS task, fully managed)
    #           Good for: no Lambda needed, lower latency
    #
    # Option C: Worker polls SQS itself (long-running container loop)
    #           Good for: high-throughput, container manages its own lifecycle
    #           This is the DEFAULT pattern in src/workers/*/main.py

    # Implementation of Option A (Lambda trigger to start ECS task):
    ecs_trigger_fn = _lambda.Function(
        self, "EcsTaskTrigger",
        function_name=f"{{project_name}}-ecs-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json

ecs = boto3.client('ecs')

def handler(event, context):
    for record in event.get('Records', []):
        body = json.loads(record['body'])
        ecs.run_task(
            cluster=os.environ['ECS_CLUSTER'],
            taskDefinition=os.environ['TASK_DEFINITION'],
            launchType='FARGATE',
            networkConfiguration={
                'awsvpcConfiguration': {
                    'subnets': os.environ['PRIVATE_SUBNETS'].split(','),
                    'securityGroups': [os.environ['ECS_SG_ID']],
                    'assignPublicIp': 'DISABLED',
                }
            },
            overrides={
                'containerOverrides': [{
                    'name': os.environ['CONTAINER_NAME'],
                    'environment': [
                        {'name': 'JOB_PAYLOAD', 'value': json.dumps(body)},
                        {'name': 'MESSAGE_ID', 'value': record['messageId']},
                    ],
                }]
            },
        )
"""),
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
        environment={
            "ECS_CLUSTER": self.ecs_cluster.cluster_arn,
            "TASK_DEFINITION": list(self.ecs_task_definitions.values())[0].task_definition_arn,
            "CONTAINER_NAME": list(WORKER_DEFINITIONS)[0]["name"],
            "PRIVATE_SUBNETS": ",".join([s.subnet_id for s in self.vpc.private_subnets]),
            "ECS_SG_ID": self.ecs_sg.security_group_id,
        },
        timeout=Duration.seconds(30),
        tracing=_lambda.Tracing.ACTIVE,
        architecture=_lambda.Architecture.ARM_64,
    )

    # Grant permission to run ECS tasks
    ecs_trigger_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ecs:RunTask", "iam:PassRole"],
            resources=[
                list(self.ecs_task_definitions.values())[0].task_definition_arn,
                f"arn:aws:iam::{self.account}:role/{list(WORKER_DEFINITIONS)[0]['name']}*",
            ],
        )
    )

    # Connect SQS queue to trigger Lambda
    ecs_trigger_fn.add_event_source(
        lambda_events.SqsEventSource(
            self.main_queue,
            batch_size=1,    # One ECS task per message
            report_batch_item_failures=True,
        )
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    CfnOutput(self, "ECSClusterArn",
        value=self.ecs_cluster.cluster_arn,
        description="ECS Cluster ARN for workers",
        export_name=f"{{project_name}}-ecs-cluster-arn-{stage_name}",
    )

    for worker_id, task_def in self.ecs_task_definitions.items():
        CfnOutput(self, f"{worker_id}TaskDefArn",
            value=task_def.task_definition_arn,
            description=f"Task definition ARN for {worker_id}",
        )
```
