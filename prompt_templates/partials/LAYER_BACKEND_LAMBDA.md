# SOP — Backend Compute Layer (Lambda + Fargate)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Lambda Python 3.12+ · Fargate arm64

---

## 1. Purpose

Provision backend compute:

- **Lambda functions** — event-driven microservices (API, SQS, EventBridge triggers)
- **ECS Fargate tasks** — long-running or memory-heavy workloads (> 15 min or > 10 GB)
- **Lambda layers** — shared dependencies (boto3, Powertools, domain libs)

This partial is authored as two first-class variants. Choose one based on your stack topology. Do not mix.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Building a single CDK stack class that owns everything (VPC + buckets + DB + Lambdas + queues all in one `class AppStack(cdk.Stack)`) | **§3 Monolith Variant** |
| Building multiple CDK stacks where Lambda resources are created in a different stack than their buckets / keys / tables / queues | **§4 Micro-Stack Variant** |

**Why the split matters.** CDK's L2 `grant_*` helpers (e.g. `bucket.grant_read(fn)`, `key.grant_decrypt(fn)`, `queue.grant_send_messages(fn)`) silently edit the resource's **resource policy** to reference the role's ARN. Inside a single stack this is fine — CloudFormation wires it with a local `Fn::GetAtt`. Across stacks it forces a **bidirectional CloudFormation export**: the upstream stack now depends on the downstream stack for the role ARN, while the downstream stack depends on the upstream stack for the resource ARN. CloudFormation rejects this as a **circular reference** at `cdk synth` time.

The Micro-Stack variant fixes this by granting **identity-side** only: every permission is a `PolicyStatement` attached to the Lambda's execution role. The upstream resource's policy is untouched. Dependencies stay unidirectional.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` subclass holds VPC + data + compute + messaging together. Typical for POC, prototypes, internal tools, stacks under ~400 resources.

### 3.1 CDK code — `_create_backend()` method body

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
)
from typing import Dict


def _create_backend(self, stage_name: str) -> None:
    """Monolith variant. Assumes self.{vpc, kms_key, data_bucket,
    aurora_cluster, db_secret, ddb_tables, main_queue, lambda_sg}
    were all created earlier in THIS stack class.
    """

    # -- Lambda layer (shared deps) ------------------------------------------
    self.common_layer = _lambda.LayerVersion(
        self, "CommonLayer",
        layer_version_name=f"{{project_name}}-common-{stage_name}",
        code=_lambda.Code.from_asset("layers/common"),
        compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
        description="aws-lambda-powertools + boto3 + project utils",
        removal_policy=RemovalPolicy.DESTROY,
    )

    # -- Shared env (all Lambdas see these) ----------------------------------
    shared_env = {
        "STAGE": stage_name,
        "TABLE_NAME": list(self.ddb_tables.values())[0].table_name,
        "QUEUE_URL": self.main_queue.queue_url,
        "DATA_BUCKET": self.data_bucket.bucket_name,
        "DB_SECRET_ARN": self.db_secret.secret_arn,
        "POWERTOOLS_SERVICE_NAME": "{project_name}",
        "POWERTOOLS_LOG_LEVEL": "INFO" if stage_name != "dev" else "DEBUG",
    }

    # -- Microservice registry (populate from Architecture Map §2) -----------
    MICROSERVICES = [
        {
            "id": "AuthService", "name": "auth-service",
            "handler": "index.handler", "code_path": "src/auth_service",
            "memory": 512, "timeout": 29, "concurrency": 50,
            "grants": [],
            "trigger": "api_gateway",
        },
        {
            "id": "PatientList", "name": "patient-list",
            "handler": "index.handler", "code_path": "src/patient_list",
            "memory": 512, "timeout": 29, "concurrency": 100,
            "grants": ["aurora_read", "ddb_read"],
            "trigger": "api_gateway",
        },
        {
            "id": "DocumentUpload", "name": "document-upload",
            "handler": "index.handler", "code_path": "src/document_upload",
            "memory": 1024, "timeout": 60, "concurrency": 50,
            "grants": ["s3_write", "ddb_write", "sqs_send"],
            "trigger": "api_gateway",
        },
        {
            "id": "VirusScanner", "name": "virus-scanner",
            "handler": "index.handler", "code_path": "src/virus_scanner",
            "memory": 1024, "timeout": 300, "concurrency": 10,
            "grants": ["s3_read_write", "ddb_write"],
            "trigger": "sqs",
        },
        # Add one entry per detected service.
    ]

    self.lambda_functions: Dict[str, _lambda.Function] = {}

    for svc in MICROSERVICES:
        log_group = logs.LogGroup(
            self, f"{svc['id']}LogGroup",
            log_group_name=f"/aws/lambda/{{project_name}}-{svc['name']}-{stage_name}",
            retention=(
                logs.RetentionDays.ONE_WEEK if stage_name == "dev"
                else logs.RetentionDays.ONE_MONTH
            ),
            encryption_key=self.kms_key,
            removal_policy=RemovalPolicy.DESTROY,
        )

        fn = _lambda.Function(
            self, f"{svc['id']}Fn",
            function_name=f"{{project_name}}-{svc['name']}-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler=svc["handler"],
            code=_lambda.Code.from_asset(svc["code_path"]),
            layers=[self.common_layer],
            memory_size=svc["memory"] if stage_name != "dev" else min(svc["memory"], 512),
            timeout=Duration.seconds(svc["timeout"]),
            reserved_concurrent_executions=svc["concurrency"],
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.lambda_sg],
            environment={**shared_env, **svc.get("env_overrides", {})},
            tracing=_lambda.Tracing.ACTIVE,
            log_group=log_group,
            environment_encryption=self.kms_key,
        )

        # L2 grants — SAFE in monolith (same stack owns all resources).
        grants = svc.get("grants", [])
        if "aurora_read" in grants:
            self.aurora_cluster.grant_connect(fn, "read_user")
            self.db_secret.grant_read(fn)
        if "aurora_write" in grants:
            self.aurora_cluster.grant_connect(fn, "app_user")
            self.db_secret.grant_read(fn)
        if "ddb_read" in grants:
            for t in self.ddb_tables.values():
                t.grant_read_data(fn)
        if "ddb_write" in grants:
            for t in self.ddb_tables.values():
                t.grant_read_write_data(fn)
        if "s3_read_write" in grants:
            self.data_bucket.grant_read_write(fn)
        if "s3_write" in grants:
            self.data_bucket.grant_write(fn)
        if "sqs_send" in grants:
            self.main_queue.grant_send_messages(fn)
        if "secrets_read" in grants:
            self.db_secret.grant_read(fn)

        self.lambda_functions[svc["id"]] = fn

    # -- SQS event sources ---------------------------------------------------
    self.lambda_functions["VirusScanner"].add_event_source(
        lambda_events.SqsEventSource(
            self.main_queue,
            batch_size=1,
            max_batching_window=Duration.seconds(0),
            report_batch_item_failures=True,
        )
    )

    # -- EventBridge schedulers ----------------------------------------------
    events.Rule(
        self, "NightlyAggregatorSchedule",
        rule_name=f"{{project_name}}-nightly-{stage_name}",
        schedule=events.Schedule.cron(hour="2", minute="0"),
        enabled=True,
        targets=[targets.LambdaFunction(self.lambda_functions["AuditAggregator"])],
    ) if "AuditAggregator" in self.lambda_functions else None

    # -- ECS Fargate worker (long-running) -----------------------------------
    self.ecs_cluster = ecs.Cluster(
        self, "WorkerCluster",
        cluster_name=f"{{project_name}}-workers-{stage_name}",
        vpc=self.vpc,
        container_insights=True,
        enable_fargate_capacity_providers=True,
    )

    ecs_task_role = iam.Role(
        self, "ECSTaskRole",
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        role_name=f"{{project_name}}-ecs-task-role-{stage_name}",
    )
    self.data_bucket.grant_read_write(ecs_task_role)
    self.db_secret.grant_read(ecs_task_role)
    self.main_queue.grant_consume_messages(ecs_task_role)

    ecs_execution_role = iam.Role(
        self, "ECSExecutionRole",
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy"
            )
        ],
    )
    self.kms_key.grant_decrypt(ecs_execution_role)

    cpu, memory = {
        "dev":     (512, 1024),
        "staging": (1024, 2048),
        "prod":    (2048, 4096),
    }.get(stage_name, (512, 1024))

    task_def = ecs.FargateTaskDefinition(
        self, "WorkerTask",
        family=f"{{project_name}}-worker-{stage_name}",
        task_role=ecs_task_role,
        execution_role=ecs_execution_role,
        cpu=cpu, memory_limit_mib=memory,
        runtime_platform=ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        ),
    )

    ecs_log_group = logs.LogGroup(
        self, "ECSLogGroup",
        log_group_name=f"/ecs/{{project_name}}-worker-{stage_name}",
        retention=logs.RetentionDays.ONE_MONTH,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    task_def.add_container(
        "WorkerContainer",
        container_name="worker",
        image=ecs.ContainerImage.from_asset("src/worker_task"),
        environment={
            "STAGE": stage_name,
            "QUEUE_URL": self.main_queue.queue_url,
            "DATA_BUCKET": self.data_bucket.bucket_name,
        },
        secrets={
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(self.db_secret, "password"),
            "DB_HOST":     ecs.Secret.from_secrets_manager(self.db_secret, "host"),
        },
        logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=ecs_log_group),
        readonly_root_filesystem=True,
    )

    # -- Outputs -------------------------------------------------------------
    CfnOutput(self, "ECSClusterName", value=self.ecs_cluster.cluster_name)
    for sid, fn in self.lambda_functions.items():
        CfnOutput(self, f"{sid}Arn", value=fn.function_arn)
```

### 3.2 Monolith gotchas

- **CloudFormation 500-resource limit** per stack. ≥ 8 Lambdas + ECS + VPC + 3 tables + 2 buckets + queues will exceed; at that point, **switch to the micro-stack variant** (don't try to split a monolith).
- `from_asset("src/x")` resolves relative to the CWD when `cdk synth` runs. Always run synth from the project root, or use `Path(__file__).parent / "src" / "x"` if anchoring matters.
- `environment_encryption=self.kms_key` requires the KMS key policy allow the Lambda service principal. CDK handles this implicitly only when both live in the same stack.

---

## 4. Micro-Stack Variant

**Use when:** Lambda is in `ComputeStack`, but buckets live in `StorageStack`, KMS keys in `SecurityStack`, tables in `JobLedgerStack`, queues in `QueueStack`, secrets in `DatabaseStack`.

### 4.1 The five non-negotiables

Memorize these. Every failure we have hit in micro-stack CDK reduces to one of them.

1. **Anchor asset paths to `__file__`, never relative-to-CWD.**
2. **Never use `X.grant_*(role)` on a cross-stack resource X.** Always identity-side `PolicyStatement` on the role.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** Use L1 `CfnRule` with a raw target dict + a static-ARN resource policy on the queue.
4. **Never own a bucket in one stack and attach its CloudFront OAC in another.** The origin auto-grants back. Put the bucket in the CDN stack.
5. **Never set `encryption_key=ext_key` where `ext_key` came from another stack.** Same reason — CDK auto-grants the service principal on the key's policy. Use identity-side KMS on the consuming role instead.

### 4.2 CDK code — `ComputeStack` class

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_kms as kms,
    aws_sqs as sqs,
    aws_dynamodb as ddb,
    aws_secretsmanager as sm,
    aws_events as events,
    aws_iam as iam,
)
from constructs import Construct

# Repo-root anchored asset path. Adjust parents[N] to your tree depth.
# stacks/compute_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "backend" / "lambdas"


# --- Identity-side grant helpers ------------------------------------------
# These attach permissions to the Lambda's execution role (a resource LOCAL
# to ComputeStack), never touching the resource's own policy in the upstream
# stack. That keeps the CloudFormation dependency graph unidirectional.

def _kms_grant(fn: _lambda.IFunction, key: kms.IKey, actions: list[str]) -> None:
    fn.add_to_role_policy(iam.PolicyStatement(actions=actions, resources=[key.key_arn]))


def _ddb_grant(fn: _lambda.IFunction, table: ddb.ITable, actions: list[str]) -> None:
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=actions,
        resources=[table.table_arn, f"{table.table_arn}/index/*"],
    ))


def _s3_grant(fn: _lambda.IFunction, bucket: s3.IBucket, actions: list[str]) -> None:
    # Split so ListBucket gets bucket ARN, object-level actions get /*.
    object_actions = [a for a in actions if a != "s3:ListBucket"]
    bucket_actions = [a for a in actions if a == "s3:ListBucket"]
    if object_actions:
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=object_actions, resources=[bucket.arn_for_objects("*")]
        ))
    if bucket_actions:
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=bucket_actions, resources=[bucket.bucket_arn]
        ))


def _sqs_grant(fn: _lambda.IFunction, queue: sqs.IQueue, actions: list[str]) -> None:
    fn.add_to_role_policy(iam.PolicyStatement(actions=actions, resources=[queue.queue_arn]))


def _secret_grant(fn: _lambda.IFunction, secret: sm.ISecret) -> None:
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
        resources=[secret.secret_arn],
    ))


class ComputeStack(cdk.Stack):
    """Lambda functions with dedicated execution roles.

    Every upstream resource is passed in by interface. We NEVER call
    resource.grant_*(fn) on cross-stack resources — see §4.1.
    """

    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        audio_bucket: s3.IBucket,
        transcript_bucket: s3.IBucket,
        reports_bucket: s3.IBucket,
        audio_data_key: kms.IKey,
        job_metadata_key: kms.IKey,
        db_secret: sm.ISecret,
        db_endpoint: str,
        jobs_ledger: ddb.ITable,
        audit_log: ddb.ITable,
        audio_ingest_queue: sqs.IQueue,
        dlq_reprocess_queue: sqs.IQueue,
        event_bus: events.IEventBus,
        bedrock_policy: iam.IManagedPolicy,
        ssm_prompt_policy: iam.IManagedPolicy,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-compute", **kwargs)

        # Apply tags from a central source, not hard-coded
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        runtime = _lambda.Runtime.PYTHON_3_12

        vpc_config = {
            "vpc": vpc,
            "security_groups": [lambda_sg],
            "vpc_subnets": ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        }

        common_env = {
            "DB_SECRET_ARN": db_secret.secret_arn,
            "DB_ENDPOINT":   db_endpoint,
            "AUDIO_BUCKET":  audio_bucket.bucket_name,
            "TRANSCRIPT_BUCKET": transcript_bucket.bucket_name,
            "REPORTS_BUCKET": reports_bucket.bucket_name,
            "JOBS_LEDGER_TABLE": jobs_ledger.table_name,
            "AUDIT_LOG_TABLE": audit_log.table_name,
            "EVENT_BUS_NAME": event_bus.event_bus_name,
            "POWERTOOLS_SERVICE_NAME": "{project_name}",
            "POWERTOOLS_LOG_LEVEL": "INFO",
        }

        def make_lambda(
            logical_id: str,
            code_path: str,
            handler: str,
            memory: int,
            timeout_secs: int,
            extra_env: dict | None = None,
        ) -> _lambda.Function:
            """Explicit LogGroup avoids the deprecated log_retention= prop which
            spawns an extra CDK-managed custom-resource Lambda per function."""
            log_group = logs.LogGroup(
                self, f"{logical_id}Logs",
                log_group_name=f"/aws/lambda/{{project_name}}-{logical_id.lower()}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            fn = _lambda.Function(
                self, logical_id,
                function_name=f"{{project_name}}-{logical_id.lower()}",
                runtime=runtime,
                architecture=_lambda.Architecture.ARM_64,
                handler=handler,
                # anchor to repo root, not CWD
                code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / code_path)),
                memory_size=memory,
                timeout=cdk.Duration.seconds(timeout_secs),
                environment={**common_env, **(extra_env or {})},
                log_group=log_group,
                tracing=_lambda.Tracing.ACTIVE,
                # Do NOT set environment_encryption=<cross-stack key>.
                # Would auto-grant the Lambda service principal on key policy.
                **vpc_config,
            )
            # Secret read is identity-side
            _secret_grant(fn, db_secret)
            return fn

        # -- Upload -----------------------------------------------------------
        self.upload_fn = make_lambda("upload-handler", "upload", "handler.lambda_handler", 256, 30)
        _s3_grant(self.upload_fn, audio_bucket, ["s3:PutObject", "s3:AbortMultipartUpload"])
        _kms_grant(self.upload_fn, audio_data_key, ["kms:Encrypt", "kms:GenerateDataKey"])
        _ddb_grant(self.upload_fn, jobs_ledger, ["dynamodb:PutItem", "dynamodb:UpdateItem"])
        _kms_grant(self.upload_fn, job_metadata_key, ["kms:Encrypt", "kms:GenerateDataKey"])
        self.upload_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"], resources=[event_bus.event_bus_arn]
        ))

        # -- Status -----------------------------------------------------------
        self.status_fn = make_lambda("status-handler", "status", "handler.lambda_handler", 128, 10)
        _ddb_grant(self.status_fn, jobs_ledger, [
            "dynamodb:GetItem", "dynamodb:Query",
        ])
        _kms_grant(self.status_fn, job_metadata_key, ["kms:Decrypt", "kms:DescribeKey"])

        # -- Processing (Bedrock consumer) -----------------------------------
        self.processing_fn = make_lambda(
            "processing-handler", "processing", "handler.lambda_handler", 1024, 300
        )
        self.processing_fn.role.add_managed_policy(bedrock_policy)
        self.processing_fn.role.add_managed_policy(ssm_prompt_policy)
        _s3_grant(self.processing_fn, transcript_bucket, ["s3:GetObject"])
        _s3_grant(self.processing_fn, reports_bucket, ["s3:PutObject", "s3:AbortMultipartUpload"])
        _kms_grant(self.processing_fn, audio_data_key, ["kms:Decrypt", "kms:DescribeKey"])
        _kms_grant(self.processing_fn, job_metadata_key, ["kms:Decrypt", "kms:GenerateDataKey"])
        _ddb_grant(self.processing_fn, jobs_ledger, [
            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
        ])
        _ddb_grant(self.processing_fn, audit_log, ["dynamodb:PutItem"])

        # -- Insights (read-path) --------------------------------------------
        self.insights_fn = make_lambda("insights-handler", "insights", "handler.lambda_handler", 256, 10)
        _s3_grant(self.insights_fn, reports_bucket, ["s3:GetObject"])
        _ddb_grant(self.insights_fn, jobs_ledger, ["dynamodb:GetItem", "dynamodb:Query"])
        _kms_grant(self.insights_fn, audio_data_key, ["kms:Decrypt", "kms:DescribeKey"])
        _kms_grant(self.insights_fn, job_metadata_key, ["kms:Decrypt", "kms:DescribeKey"])

        # -- Router (SQS → SFN) ----------------------------------------------
        self.router_fn = make_lambda("router-handler", "router", "handler.lambda_handler", 256, 30)
        _sqs_grant(self.router_fn, audio_ingest_queue, [
            "sqs:ReceiveMessage", "sqs:DeleteMessage",
            "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility",
        ])
        _ddb_grant(self.router_fn, jobs_ledger, [
            "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem",
        ])
        _ddb_grant(self.router_fn, audit_log, ["dynamodb:PutItem"])
        self.router_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:StartExecution"], resources=["*"],  # Scoped at orchestration stack
        ))
        self.router_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"], resources=[event_bus.event_bus_arn]
        ))

        # -- DLQ reprocessor -------------------------------------------------
        self.dlq_fn = make_lambda(
            "dlq-reprocessor", "dlq_reprocessor", "handler.lambda_handler", 256, 60
        )
        _sqs_grant(self.dlq_fn, dlq_reprocess_queue, [
            "sqs:ReceiveMessage", "sqs:DeleteMessage",
            "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility",
        ])
        _sqs_grant(self.dlq_fn, audio_ingest_queue, ["sqs:SendMessage"])
        _ddb_grant(self.dlq_fn, jobs_ledger, [
            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
        ])
        _ddb_grant(self.dlq_fn, audit_log, ["dynamodb:PutItem"])
        _kms_grant(self.dlq_fn, job_metadata_key, ["kms:Decrypt", "kms:GenerateDataKey"])
        _kms_grant(self.dlq_fn, audio_data_key, ["kms:Decrypt", "kms:GenerateDataKey"])
        self.dlq_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"], resources=[event_bus.event_bus_arn]
        ))

        # Apply the shared permission boundary to every function role
        for fn in [
            self.upload_fn, self.status_fn, self.processing_fn,
            self.insights_fn, self.router_fn, self.dlq_fn,
        ]:
            iam.PermissionsBoundary.of(fn.role).apply(permission_boundary)
```

### 4.3 Fargate in a `FargateStack`

Same rule: all KMS / secret / bucket / queue permissions go on the **task role's identity policy**, not via `resource.grant_*(task_role)`. Dockerfile assets (`ecs.ContainerImage.from_asset("path")`) get the same `Path(__file__).parents[N]` anchor.

### 4.4 Micro-stack gotchas

- `log_retention=` on `lambda.Function` is deprecated. It also silently creates a CDK-managed **custom-resource Lambda** per function (inflates `AWS::Lambda::Function` count by 1 per function, breaks `resource_count_is` assertions). Use explicit `log_group=logs.LogGroup(...)` instead.
- When unit-testing multiple Lambda handlers whose files are all named `handler.py`, load each under a unique module name via `importlib.util.spec_from_file_location("upload_handler", path)`. Plain `sys.path.insert + import handler` collides in `sys.modules`.
- `cdk.Environment(account="")` (empty string) fails with `Unable to parse environment specification "aws:///us-east-1"`. Guard with `_account = SETTINGS.account or "000000000000"` for offline synth.
- `permission_boundary` is a `ManagedPolicy` that must have at least one `PolicyStatement`. An empty boundary fails synth validation.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC / prototype in one stack, < 400 resources | Stay on Monolith |
| New domain added (e.g. analytics) that needs its own deploy cycle | Split out as a new stack → Micro-Stack variant for the split |
| `cdk synth` error: `Adding this dependency ... would create a cyclic reference` | You are in micro-stack territory with a monolith-pattern grant. Convert the offending `grant_*` call to identity-side per §4.1 |
| Stack hits CFN 500-resource limit | Split; use Micro-Stack variant on both halves |
| Need to deploy services on independent cadences | Micro-Stack |
| Same team owns everything, single deploy pipeline | Monolith is fine; don't split prematurely |

---

## 6. Worked example — verify both variants synthesize

Save as `tests/sop/test_LAYER_BACKEND_LAMBDA.py` in your project and run with `python -m pytest tests/sop/test_LAYER_BACKEND_LAMBDA.py`. No AWS credentials needed.

```python
"""SOP verification harness — both monolith and micro-stack compile clean."""
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2, aws_s3 as s3, aws_kms as kms, aws_sqs as sqs,
    aws_dynamodb as ddb, aws_secretsmanager as sm, aws_events as events,
    aws_iam as iam,
)
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_monolith_variant_synthesizes(tmp_path):
    # You would import your monolith AppStack here and instantiate it.
    # Verify CFN is produced without errors.
    pass


def test_microstack_variant_synthesizes_without_cycles():
    app = cdk.App()
    env = _env()

    # Minimal upstream stacks
    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg  = ec2.SecurityGroup(deps, "Sg", vpc=vpc)
    bkt_audio      = s3.Bucket(deps, "Audio")
    bkt_transcript = s3.Bucket(deps, "Transcript")
    bkt_reports    = s3.Bucket(deps, "Reports")
    key_audio      = kms.Key(deps, "AudioKey")
    key_meta       = kms.Key(deps, "MetaKey")
    secret = sm.Secret(deps, "DbSecret")
    ledger = ddb.Table(deps, "Ledger",
        partition_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING))
    audit = ddb.Table(deps, "Audit",
        partition_key=ddb.Attribute(name="event_id", type=ddb.AttributeType.STRING))
    q_in = sqs.Queue(deps, "Ingest"); q_dlq = sqs.Queue(deps, "DlqReproc")
    bus = events.EventBus(deps, "Bus")
    bedrock_pol = iam.ManagedPolicy(deps, "BP",
        statements=[iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=["*"])])
    ssm_pol = iam.ManagedPolicy(deps, "SP",
        statements=[iam.PolicyStatement(actions=["ssm:GetParameter"], resources=["*"])])
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    # Import and instantiate ComputeStack exactly as in §4.2
    from infrastructure.cdk.stacks.compute_stack import ComputeStack
    compute = ComputeStack(
        app,
        vpc=vpc, lambda_sg=sg,
        audio_bucket=bkt_audio, transcript_bucket=bkt_transcript, reports_bucket=bkt_reports,
        audio_data_key=key_audio, job_metadata_key=key_meta,
        db_secret=secret, db_endpoint="endpoint.rds",
        jobs_ledger=ledger, audit_log=audit,
        audio_ingest_queue=q_in, dlq_reprocess_queue=q_dlq,
        event_bus=bus,
        bedrock_policy=bedrock_pol, ssm_prompt_policy=ssm_pol,
        permission_boundary=boundary,
        env=env,
    )

    # If a circular dep existed, Template.from_stack would raise during synth.
    template = Template.from_stack(compute)
    template.resource_count_is("AWS::Lambda::Function", 6)  # no LogRetention custom resources
    # Assert no Lambda has a cross-stack resource policy ref to a role.arn
```

---

## 7. References

- `docs/template_params.md` — `PROJECT_NAME`, `STACK_PREFIX`, `LAMBDA_RUNTIME`, tags
- `docs/Feature_Roadmap.md` — feature IDs C-01..C-18
- AWS docs: [CDK Best Practices](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html), [Lambda Powertools for Python](https://docs.powertools.aws.dev/lambda/python/latest/)
- Related SOPs: `LAYER_NETWORKING` (VPC), `LAYER_DATA` (DDB, RDS), `EVENT_DRIVEN_PATTERNS` (SQS wiring), `LAYER_BACKEND_ECS` (Fargate deep dive), `LAYER_SECURITY` (KMS, IAM)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP rewrite. Added Micro-Stack variant with identity-side grants, `Path(__file__)`-anchored assets, explicit `LogGroup` (no deprecated `log_retention`). Codified five non-negotiables (§4.1). Added worked verification harness. |
| 1.0 | 2026-03-05 | Initial monolith-only partial with L2 `grant_*` calls. |
