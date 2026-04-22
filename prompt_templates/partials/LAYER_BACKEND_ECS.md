# SOP — Backend ECS Fargate (Long-Running Tasks)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Fargate arm64 · container runtime Python/Node/etc.

---

## 1. Purpose

Containerized workloads that don't fit Lambda's 15-minute / 10 GB / 250 MB deploy package limits:

- Video/audio transcoding (FFmpeg)
- Large PDF/report generation
- Long-running ML inference (non-Bedrock, non-SageMaker)
- Scheduled batch jobs > 15 min
- Persistent workers consuming SQS at high throughput

Lambda is default. Fargate is for when Lambda isn't enough. See §5 decision matrix.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| ECS cluster, task, and upstream resources (buckets, queues, keys) in one stack | **§3 Monolith Variant** |
| ECS cluster + task in `FargateStack`, KMS/S3/SQS in separate stacks | **§4 Micro-Stack Variant** |

Same cross-stack grant rule as Lambda: identity-side policies on the task role, never `bucket.grant_*(task_role)` cross-stack.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration, RemovalPolicy,
    aws_ecs as ecs,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_ecr as ecr,
    aws_applicationautoscaling as appscaling,
)


def _create_ecs(self, stage: str) -> None:
    # ECR repository (manage lifecycle, scan on push)
    self.worker_repo = ecr.Repository(
        self, "WorkerRepo",
        repository_name=f"{{project_name}}-worker-{stage}",
        image_scan_on_push=True,
        image_tag_mutability=ecr.TagMutability.IMMUTABLE,
        removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
        lifecycle_rules=[ecr.LifecycleRule(max_image_count=10, rule_priority=1)],
    )

    self.ecs_cluster = ecs.Cluster(
        self, "Cluster",
        cluster_name=f"{{project_name}}-workers-{stage}",
        vpc=self.vpc,
        container_insights=True,
        enable_fargate_capacity_providers=True,
    )

    # Task role — what the container code can DO (identity policy)
    self.ecs_task_role = iam.Role(
        self, "EcsTaskRole",
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        role_name=f"{{project_name}}-ecs-task-role-{stage}",
    )
    # Monolith: L2 grants are fine — all resources in this stack
    self.data_bucket.grant_read_write(self.ecs_task_role)
    self.main_queue.grant_consume_messages(self.ecs_task_role)
    self.db_secret.grant_read(self.ecs_task_role)

    # Execution role — what ECS control plane does to LAUNCH the task (pull image, write logs)
    self.ecs_execution_role = iam.Role(
        self, "EcsExecutionRole",
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy"
            )
        ],
    )
    # KMS decrypt for pulling encrypted secrets into env
    self.kms_key.grant_decrypt(self.ecs_execution_role)

    cpu, memory = {
        "dev":     (512, 1024),
        "staging": (1024, 2048),
        "prod":    (2048, 4096),
    }.get(stage, (512, 1024))

    task_def = ecs.FargateTaskDefinition(
        self, "WorkerTask",
        family=f"{{project_name}}-worker-{stage}",
        task_role=self.ecs_task_role,
        execution_role=self.ecs_execution_role,
        cpu=cpu, memory_limit_mib=memory,
        runtime_platform=ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        ),
    )

    log_group = logs.LogGroup(
        self, "WorkerLogs",
        log_group_name=f"/ecs/{{project_name}}-worker-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    task_def.add_container(
        "WorkerContainer",
        container_name="worker",
        image=ecs.ContainerImage.from_asset("src/worker_task"),  # relative to CWD
        environment={
            "STAGE":       stage,
            "QUEUE_URL":   self.main_queue.queue_url,
            "DATA_BUCKET": self.data_bucket.bucket_name,
        },
        secrets={
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(self.db_secret, "password"),
            "DB_HOST":     ecs.Secret.from_secrets_manager(self.db_secret, "host"),
        },
        logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=log_group),
        health_check=ecs.HealthCheck(
            command=["CMD-SHELL", "python -c 'import sys; sys.exit(0)'"],
            interval=Duration.seconds(30),
            timeout=Duration.seconds(10),
            retries=3, start_period=Duration.seconds(60),
        ),
        readonly_root_filesystem=True,  # hardening — container FS is immutable
    )

    # Service (persistent, autoscaled) OR scheduled task (one-off)
    service = ecs.FargateService(
        self, "WorkerService",
        service_name=f"{{project_name}}-worker-{stage}",
        cluster=self.ecs_cluster,
        task_definition=task_def,
        desired_count=1,
        capacity_provider_strategies=[
            ecs.CapacityProviderStrategy(
                capacity_provider="FARGATE_SPOT", weight=3
            ),
            ecs.CapacityProviderStrategy(
                capacity_provider="FARGATE", weight=1, base=1,
            ),
        ],
        circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        enable_execute_command=(stage != "prod"),  # ECS Exec for dev/staging debugging
    )

    # Autoscale on SQS depth
    scaling = service.auto_scale_task_count(min_capacity=1, max_capacity=50)
    scaling.scale_on_metric(
        "ScaleOnQueueDepth",
        metric=self.main_queue.metric_approximate_number_of_messages_visible(),
        scaling_steps=[
            appscaling.ScalingInterval(upper=10,  change=0),
            appscaling.ScalingInterval(lower=10,  change=+1),
            appscaling.ScalingInterval(lower=100, change=+5),
        ],
        adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
        cooldown=Duration.seconds(60),
    )
```

### 3.1 Monolith gotchas

- **`ContainerImage.from_asset("path")`** resolves relative to CWD. Same issue as Lambda — use `Path(__file__).parents[N] / "src" / "worker_task"` in CI environments where CWD varies.
- **Fargate Spot** is 70% cheaper but tasks receive a 2-minute interruption notice. OK for idempotent batch work, not OK for user-facing requests.
- **Readonly root filesystem** breaks libraries that write to `/tmp` at import time. Either set `--tmpfs /tmp` equivalent via `linux_parameters`, or mount a volume.
- **`ecs.Secret.from_secrets_manager`** injects the secret value into the container env at task start. The task role must have `secretsmanager:GetSecretValue` on the secret's ARN (and the execution role needs KMS Decrypt on the secret's encryption key). L2 `grant_read` in monolith handles this.

---

## 4. Micro-Stack Variant

### 4.1 `FargateStack` — cluster + task, consumes upstream by interface

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration, RemovalPolicy,
    aws_ecs as ecs,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_secretsmanager as sm,
    aws_logs as logs,
    aws_ecr as ecr,
)
from constructs import Construct

_IMAGE_SRC = Path(__file__).resolve().parents[3] / "backend" / "containers" / "worker"


class FargateStack(cdk.Stack):
    """Long-running container workload. Upstream resources injected by interface."""

    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        ecs_sg: ec2.ISecurityGroup,
        data_bucket: s3.IBucket,
        work_queue: sqs.IQueue,
        dlq: sqs.IQueue,
        db_secret: sm.ISecret,
        audio_data_key: kms.IKey,
        job_metadata_key: kms.IKey,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-fargate", **kwargs)

        repo = ecr.Repository(
            self, "WorkerRepo",
            repository_name="{project_name}-worker",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            removal_policy=RemovalPolicy.DESTROY,  # POC
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10, rule_priority=1)],
        )

        cluster = ecs.Cluster(
            self, "Cluster",
            cluster_name="{project_name}-workers",
            vpc=vpc,
            container_insights=True,
            enable_fargate_capacity_providers=True,
        )

        # Task role — identity-side only, never use upstream.grant_*(task_role)
        task_role = iam.Role(
            self, "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            role_name="{project_name}-ecs-task-role",
        )
        iam.PermissionsBoundary.of(task_role).apply(permission_boundary)

        # S3 data access
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucket"],
            resources=[data_bucket.bucket_arn, data_bucket.arn_for_objects("*")],
        ))
        # SQS consume
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["sqs:ReceiveMessage", "sqs:DeleteMessage",
                     "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"],
            resources=[work_queue.queue_arn],
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["sqs:SendMessage"], resources=[dlq.queue_arn],
        ))
        # KMS (identity-side)
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[audio_data_key.key_arn, job_metadata_key.key_arn],
        ))
        # Secret read (identity-side)
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[db_secret.secret_arn],
        ))

        # Execution role — ECS control plane creds
        exec_role = iam.Role(
            self, "ExecRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        # Execution role needs KMS decrypt too (for secret pull + log group key)
        exec_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=[job_metadata_key.key_arn, audio_data_key.key_arn],
        ))

        task_def = ecs.FargateTaskDefinition(
            self, "Task",
            family="{project_name}-worker",
            task_role=task_role,
            execution_role=exec_role,
            cpu=2048, memory_limit_mib=4096,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        log_group = logs.LogGroup(
            self, "TaskLogs",
            log_group_name="/ecs/{project_name}-worker",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_def.add_container(
            "Container",
            container_name="worker",
            image=ecs.ContainerImage.from_asset(str(_IMAGE_SRC)),  # __file__ anchored
            environment={
                "QUEUE_URL":   work_queue.queue_url,
                "DATA_BUCKET": data_bucket.bucket_name,
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                "DB_HOST":     ecs.Secret.from_secrets_manager(db_secret, "host"),
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=log_group),
            readonly_root_filesystem=True,
        )

        self.service = ecs.FargateService(
            self, "Service",
            service_name="{project_name}-worker",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            security_groups=[ecs_sg],
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=3),
                ecs.CapacityProviderStrategy(capacity_provider="FARGATE", weight=1, base=1),
            ],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)
        cdk.CfnOutput(self, "RepoUri",     value=repo.repository_uri)
```

### 4.2 Micro-stack gotchas

- **`ContainerImage.from_asset`** — use `Path(__file__).parents[N]` anchor, not relative strings.
- **Execution role needs KMS decrypt** on the secret's encryption key. Without it, task launch fails at Secrets Manager decrypt, not in your container code.
- **Autoscaling on cross-stack queue depth** — `service.auto_scale_task_count(...).scale_on_metric(metric=work_queue.metric_approximate_number_of_messages_visible())` is safe (reads queue ARN only; no policy mutation).
- **`enable_execute_command=True`** requires the task role to have `ssmmessages:*` — CDK adds this automatically only if you set the flag at service-create time (not after).

---

## 5. Decision — Lambda vs Fargate

| Signal | Use |
|---|---|
| Execution < 15 min | Lambda |
| Execution > 15 min | Fargate |
| Memory > 10 GB | Fargate |
| Dep package > 250 MB | Fargate (or Lambda container image up to 10 GB) |
| Burst concurrency | Lambda (scales to 1000+ in seconds) |
| Persistent worker loop | Fargate |
| Cost-sensitive long-running | Fargate Spot (+ fallback) |
| Simple HTTP handler | Lambda |

---

## 6. Worked example

```python
def test_fargate_task_uses_identity_side_kms():
    import aws_cdk as cdk
    from aws_cdk import aws_kms as kms
    from aws_cdk.assertions import Template, Match
    # ... instantiate upstream stacks + FargateStack ...
    t = Template.from_stack(fg)
    # Task role's policy includes KMS actions on the key ARN
    t.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {"Statement": Match.array_with([
            Match.object_like({"Action": Match.array_with(["kms:Decrypt"])}),
        ])}
    })
```

---

## 7. References

- `docs/template_params.md` — `LAMBDA_ARCH`, `AWS_REGION`
- `docs/Feature_Roadmap.md` — C-19..C-24
- Related SOPs: `LAYER_BACKEND_LAMBDA` (when to stay Lambda), `LAYER_SECURITY` (KMS identity-side), `EVENT_DRIVEN_PATTERNS` (SQS wiring)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Micro-Stack: identity-side task role policies, `Path(__file__)`-anchored image assets, explicit execution-role KMS decrypt for secrets/logs. |
| 1.0 | 2026-03-05 | Initial. |
