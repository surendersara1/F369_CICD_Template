# SOP — ECS Cluster Foundation (Fargate + EC2 capacity providers · Service Connect · task definition · auto-scaling · IAM)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · ECS cluster · Capacity providers (FARGATE + FARGATE_SPOT + EC2 Auto Scaling Group) · Service Connect (replaces App Mesh, GA 2023) · ECS Task Definition (Linux + Windows + ARM64) · Service auto-scaling (target tracking + step scaling) · ECS Exec for debugging · CloudWatch Container Insights v2

---

## 1. Purpose

- Codify the **ECS production cluster foundation** — the non-EKS path for container workloads on AWS. Many clients with < 50 microservices choose ECS for simpler ops + lower TCO.
- Codify the **capacity provider strategy** — FARGATE (managed) + FARGATE_SPOT (70% discount) + EC2 (custom AMIs / GPU / heavy state) mixed.
- Codify **Service Connect** — the modern AWS-native service-mesh-lite (replaces App Mesh sunsetting Sept 2026). Auto service discovery + traffic management + TLS + observability.
- Codify **task definition** patterns — multi-container (sidecar), volumes, secrets, env, logging, ulimits, healthCheck.
- Codify **service auto-scaling** with target tracking on `CPUUtilization` + `MemoryUtilization` + custom metrics (RequestCountPerTarget).
- Codify **ECS Exec** for in-container debugging without SSH (uses SSM Session Manager).
- Codify **Container Insights v2** for ECS observability.
- This is the **ECS foundation specialisation**. Required by `ECS_DEPLOYMENT_PATTERNS` (blue/green) + `ECS_PRODUCTION_HARDENING` (security + autoscaling).

When the SOW signals: "containers but not Kubernetes", "Fargate", "ECS migration from EKS-too-complex", "AWS-managed orchestration", "Service Connect / replace App Mesh".

---

## 2. Decision tree — ECS vs EKS; Fargate vs EC2 capacity

| Need | ECS | EKS |
|---|:---:|:---:|
| Simpler ops / lower learning curve | ✅ | ❌ |
| Native AWS APIs (no kubectl) | ✅ | ❌ |
| Kubernetes-flavored manifests / Helm / ArgoCD | ❌ | ✅ |
| Hybrid Linux + Windows containers | ✅ native | ⚠️ Windows nodes possible |
| Cluster cost (small fleet) | ✅ no control plane fee | ❌ $73/mo per cluster |
| Marketplace + 3rd-party operators | ⚠️ limited | ✅ rich |

**Recommendation: ECS for < 50 services + simpler teams; EKS for > 50 services + K8s expertise OR multi-cloud aspiration.**

```
Capacity provider strategy:

  FARGATE        — managed; no servers; 100% serverless containers
                   Default for stateless web/API services
                   $0.04/vCPU/hr + $0.004/GB/hr
                   
  FARGATE_SPOT   — same as FARGATE but on spare capacity
                   70% discount; 2-min interruption notice
                   For fault-tolerant batch / dev / CI
                   
  EC2 (ASG)      — you manage instances
                   Use for: GPU workloads, > 8 vCPU per task,
                            heavy disk I/O, custom AMIs,
                            sustained load > 75% utilization
                   ECS-optimized AMI auto-rotates
                   
  EXTERNAL       — ECS Anywhere on-prem (rare)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Fargate cluster + Service Connect + 1 service | **§3 Monolith** |
| Production — Fargate + Spot + EC2 + Service Connect + multiple services | **§5 Production** |

---

## 3. Monolith Variant — Fargate + Service Connect + ALB

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │ ECS Cluster: prod-cluster                                        │
   │   - Capacity providers: FARGATE (60%) + FARGATE_SPOT (40%)        │
   │   - Container Insights v2 enabled                                  │
   │   - Service Connect namespace: prod.local (Cloud Map)              │
   └──────────────────────────────────────────────────────────────────┘

   Task Definitions:
     - api-task (Fargate, 1 vCPU, 2 GB, ARM64, Powertools layer)
     - worker-task (Fargate Spot, 0.5 vCPU, 1 GB, batch processor)
     - db-proxy-task (Fargate, 0.25 vCPU, 0.5 GB, RDS Proxy sidecar)

   Services:
     - api-svc (3 replicas, Fargate, Service Connect inbound port 8080)
     - worker-svc (auto 2-20 replicas, Fargate Spot, no inbound)
     - api-svc registered to ALB target group + Service Connect

   Service Connect mesh:
     api-svc.prod.local:8080 → routes to active api-svc tasks
     worker-svc reads SQS (no Service Connect inbound needed)

   Auto-scaling:
     api-svc target-tracks CPU 50%, scale 3 → 30 replicas
     worker-svc target-tracks SQS ApproximateNumberOfMessagesVisible / 100

   ALB (public):
     /api/* → api-svc target group (IP target type)
     /healthz → api-svc /healthz
```

### 3.2 CDK

```python
# stacks/ecs_foundation_stack.py
from aws_cdk import Stack, Duration, RemovalPolicy
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_servicediscovery as cloudmap
from aws_cdk import aws_kms as kms
from aws_cdk import aws_ecr as ecr
from constructs import Construct


class EcsFoundationStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 vpc: ec2.IVpc, kms_key: kms.IKey, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Cloud Map namespace for Service Connect ────────────────
        # Service Connect uses Cloud Map for service discovery
        sc_namespace = cloudmap.PrivateDnsNamespace(self, "ScNamespace",
            name=f"{env_name}.local",
            vpc=vpc,
        )

        # ── 2. ECS Cluster with Container Insights v2 ─────────────────
        self.cluster = ecs.Cluster(self, "Cluster",
            cluster_name=f"{env_name}-cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENHANCED,    # v2 (2024+)
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(
                name=f"{env_name}.local",
                use_for_service_connect=True,
            ),
            execute_command_configuration=ecs.ExecuteCommandConfiguration(
                kms_key=kms_key,
                logging=ecs.ExecuteCommandLogging.OVERRIDE,
                log_configuration=ecs.ExecuteCommandLogConfiguration(
                    cloud_watch_log_group=logs.LogGroup(self, "ExecLogGroup",
                        log_group_name=f"/aws/ecs/{env_name}/exec",
                        encryption_key=kms_key,
                        retention=logs.RetentionDays.ONE_MONTH,
                    ),
                    cloud_watch_encryption_enabled=True,
                ),
            ),
        )

        # ── 3. Capacity providers — Fargate + Fargate Spot ─────────────
        # Default FARGATE/FARGATE_SPOT capacity providers are auto-attached
        # to new clusters. Set strategy:
        ecs.CfnClusterCapacityProviderAssociations(self, "CapacityAssoc",
            cluster=self.cluster.cluster_name,
            capacity_providers=["FARGATE", "FARGATE_SPOT"],
            default_capacity_provider_strategy=[
                ecs.CfnClusterCapacityProviderAssociations.CapacityProviderStrategyProperty(
                    capacity_provider="FARGATE",
                    weight=60,
                    base=1,                                # always 1 task on FARGATE
                ),
                ecs.CfnClusterCapacityProviderAssociations.CapacityProviderStrategyProperty(
                    capacity_provider="FARGATE_SPOT",
                    weight=40,
                ),
            ],
        )

        # ── 4. ECR repo for app image ────────────────────────────────
        self.ecr_repo = ecr.Repository(self, "AppRepo",
            repository_name=f"{env_name}/api",
            encryption=ecr.RepositoryEncryption.KMS,
            encryption_key=kms_key,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Keep last 30 prod-tagged",
                    tag_status=ecr.TagStatus.TAGGED,
                    tag_prefix_list=["prod-"],
                    max_image_count=30,
                ),
                ecr.LifecycleRule(
                    description="Expire untagged after 7d",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(7),
                ),
            ],
        )

        # ── 5. Task execution role (pulls image, writes logs) ──────
        self.execution_role = iam.Role(self, "ExecRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy",
                ),
            ],
        )
        self.execution_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=[kms_key.key_arn],
        ))

        # ── 6. Task role (permissions for the running app) ────────────
        # One task role per service is best practice — see ECS_PRODUCTION_HARDENING

        # ── 7. CloudWatch log group ──────────────────────────────────
        self.log_group = logs.LogGroup(self, "AppLogGroup",
            log_group_name=f"/aws/ecs/{env_name}/api",
            encryption_key=kms_key,
            retention=logs.RetentionDays.ONE_MONTH,
        )
```

### 3.3 Task Definition + Service with Service Connect

```python
# Per-service task role (least privilege)
api_task_role = iam.Role(self, "ApiTaskRole",
    assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
)
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[db_secret_arn],
))
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
    resources=[ddb_table_arn, f"{ddb_table_arn}/*"],
))

# Task definition — Fargate, 1 vCPU, 2 GB, ARM64
task_def = ecs.FargateTaskDefinition(self, "ApiTaskDef",
    family=f"{env_name}-api",
    cpu=1024,                                                    # 1 vCPU
    memory_limit_mib=2048,                                       # 2 GB
    runtime_platform=ecs.RuntimePlatform(
        cpu_architecture=ecs.CpuArchitecture.ARM64,             # Graviton — 20% cheaper
        operating_system_family=ecs.OperatingSystemFamily.LINUX,
    ),
    task_role=api_task_role,
    execution_role=execution_role,
    ephemeral_storage_gib=21,                                    # default 20; 21+ if needed
)

# Container — main app
api_container = task_def.add_container("api",
    image=ecs.ContainerImage.from_ecr_repository(ecr_repo, tag="prod-1.0.0"),
    cpu=1024,                                                    # all cpu to main container
    memory_limit_mib=1792,                                       # leave 256 for sidecar
    logging=ecs.LogDriver.aws_logs(
        stream_prefix="api",
        log_group=log_group,
        mode=ecs.AwsLogDriverMode.NON_BLOCKING,                  # 2024 GA — drop logs if buffer full
    ),
    environment={
        "POWERTOOLS_SERVICE_NAME": "api",
        "POWERTOOLS_METRICS_NAMESPACE": "App",
        "AWS_DEFAULT_REGION": self.region,
    },
    secrets={
        "DB_CREDS": ecs.Secret.from_secrets_manager(db_secret),
    },
    health_check=ecs.HealthCheck(
        command=["CMD-SHELL", "curl -f http://localhost:8080/healthz || exit 1"],
        interval=Duration.seconds(15),
        timeout=Duration.seconds(5),
        retries=3,
        start_period=Duration.seconds(60),
    ),
    port_mappings=[ecs.PortMapping(
        name="api-port",                                         # Service Connect port name
        container_port=8080,
        protocol=ecs.Protocol.TCP,
        app_protocol=ecs.AppProtocol.http,                       # Service Connect L7 awareness
    )],
    ulimits=[ecs.Ulimit(
        name=ecs.UlimitName.NOFILE,
        soft_limit=65536,
        hard_limit=65536,
    )],
)

# (Optional) Sidecar — e.g., aws-otel-collector for traces
otel_sidecar = task_def.add_container("otel",
    image=ecs.ContainerImage.from_registry(
        "public.ecr.aws/aws-observability/aws-otel-collector:latest",
    ),
    cpu=0,
    memory_limit_mib=256,
    essential=False,
    logging=ecs.LogDriver.aws_logs(stream_prefix="otel", log_group=log_group),
    environment={"AWS_REGION": self.region},
)

# Service with Service Connect
api_service = ecs.FargateService(self, "ApiService",
    cluster=cluster,
    task_definition=task_def,
    service_name=f"{env_name}-api",
    desired_count=3,
    capacity_provider_strategies=[
        ecs.CapacityProviderStrategy(capacity_provider="FARGATE", weight=60, base=1),
        ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=40),
    ],
    enable_execute_command=True,                                 # ECS Exec for debugging
    enable_ecs_managed_tags=True,
    propagate_tags=ecs.PropagatedTagSource.SERVICE,
    deployment_controller=ecs.DeploymentController(
        type=ecs.DeploymentControllerType.ECS,                   # rolling deploy default;
                                                                  # use CODE_DEPLOY for blue/green
    ),
    circuit_breaker=ecs.DeploymentCircuitBreaker(
        enable=True, rollback=True,                              # auto-rollback on failed deploy
    ),
    # Service Connect — both client + server registration
    service_connect_configuration=ecs.ServiceConnectProps(
        services=[ecs.ServiceConnectService(
            port_mapping_name="api-port",                         # matches container port name
            dns_name="api",                                        # api.prod.local
            port=8080,
            ingress_port_override=8080,
            timeout=ecs.ServiceConnectTimeout(
                idle_timeout=Duration.minutes(5),
                per_request_timeout=Duration.seconds(30),
            ),
        )],
        log_driver=ecs.LogDriver.aws_logs(
            stream_prefix="sc-envoy",
            log_group=log_group,
        ),
    ),
    # Public ALB target registration (handled separately if ALB is shared)
    cloud_map_options=ecs.CloudMapOptions(
        name="api",
        cloud_map_namespace=cluster.default_cloud_map_namespace,
    ),
    health_check_grace_period=Duration.minutes(2),               # 2-min grace before TG checks
    min_healthy_percent=100,                                     # never below desired during deploy
    max_healthy_percent=200,                                     # blue/green-style temporary 2x
)

# ── Auto-scaling ──────────────────────────────────────────────
scaling = api_service.auto_scale_task_count(
    min_capacity=3,
    max_capacity=30,
)

# Target tracking on CPU
scaling.scale_on_cpu_utilization("CpuScaling",
    target_utilization_percent=50,
    scale_in_cooldown=Duration.seconds(60),
    scale_out_cooldown=Duration.seconds(30),
)

# Target tracking on memory
scaling.scale_on_memory_utilization("MemScaling",
    target_utilization_percent=70,
)

# Custom: ALB RequestCountPerTarget
scaling.scale_on_metric("RequestRate",
    metric=cw.Metric(
        namespace="AWS/ApplicationELB",
        metric_name="RequestCountPerTarget",
        dimensions_map={"TargetGroup": target_group.target_group_full_name},
        statistic="Sum", period=Duration.minutes(1),
    ),
    scaling_steps=[
        autoscaling.ScalingInterval(upper=100, change=-1),
        autoscaling.ScalingInterval(lower=200, change=+1),
        autoscaling.ScalingInterval(lower=500, change=+3),
    ],
    adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
    cooldown=Duration.seconds(60),
)
```

### 3.4 Inter-service communication via Service Connect

```python
# A different service (worker-svc) calls api.prod.local:8080
# Service Connect handles:
#   - DNS resolution (Cloud Map)
#   - Connection load balancing across api-svc replicas
#   - mTLS optionally (set Service Connect tls config)
#   - Request retries
#   - Per-request timeout
#   - Outlier detection (eject failing replicas)
#   - Per-call metrics emitted to CloudWatch

# In worker-svc Python code:
import requests
resp = requests.get("http://api.prod.local:8080/users/123")
# Service Connect intercepts via Envoy sidecar in worker task,
# routes to a healthy api-svc task, returns response.
```

---

## 4. EC2 capacity provider variant

For workloads that need EC2 (GPU, > 8 vCPU per task, custom AMI):

```python
# Auto Scaling Group with ECS-optimized AMI
asg = autoscaling.AutoScalingGroup(self, "EcsAsg",
    vpc=vpc,
    instance_type=ec2.InstanceType.of(
        ec2.InstanceClass.MEMORY6_GRAVITON, ec2.InstanceSize.LARGE,
    ),
    machine_image=ecs.EcsOptimizedImage.amazon_linux2(
        hardware_type=ecs.AmiHardwareType.ARM,
    ),
    min_capacity=2,
    max_capacity=10,
    desired_capacity=3,
)
asg.scale_on_cpu_utilization("CpuScale", target_utilization_percent=70)

# Capacity provider wraps the ASG
ec2_capacity = ecs.AsgCapacityProvider(self, "Ec2CapacityProvider",
    auto_scaling_group=asg,
    enable_managed_scaling=True,                  # ECS auto-adjusts ASG desired
    enable_managed_termination_protection=False,  # set true for stateful
    capacity_provider_name="ec2-graviton",
)

cluster.add_asg_capacity_provider(ec2_capacity)

# Then in service / task:
ec2_service = ecs.Ec2Service(self, "Ec2Service",
    cluster=cluster,
    task_definition=ec2_task_def,
    capacity_provider_strategies=[
        ecs.CapacityProviderStrategy(capacity_provider="ec2-graviton", weight=100),
    ],
    # ... rest of service config ...
)
```

---

## 5. Common gotchas

- **Container Insights v2 (Enhanced) costs more** than v1 — full per-container metrics. Disable per-container if cluster has > 500 containers.
- **Capacity provider strategy `base: 1`** means 1 task always on Fargate (not Spot) for availability. Without `base`, all could go to Spot and lose capacity in one event.
- **Service Connect requires `port_mappings.name`** in container definition — without it, no Service Connect exposure.
- **Service Connect adds Envoy sidecar** to every task (~50 MB image, ~256 MB RAM). Plan task memory.
- **Service Connect TLS is opt-in** (`tls.kms_key` config) — without TLS, traffic is plain HTTP between tasks within VPC.
- **NON_BLOCKING log mode (2024 GA)** is preferred — old default `BLOCKING` could cause container hang on logging backpressure.
- **Capacity provider mismatch with launch type**: a Fargate task definition can't run on EC2 capacity provider. CDK enforces type at compile time.
- **ECS Exec requires task role policy** + execution role policy for SSM. CDK `enable_execute_command=True` adds them automatically.
- **Task role vs execution role confusion**:
  - Execution role: ECS agent uses to pull image, write logs, fetch secrets at task START
  - Task role: container uses for runtime AWS API calls
  - Different IAM principals; never share roles.
- **Image pull errors hidden** unless task fails to start. Watch CW Logs for `CannotPullContainerError`.
- **ARM64 (Graviton) Fargate** = 20% cheaper but image must be multi-arch (`docker buildx build --platform linux/arm64,linux/amd64`).
- **Fargate ephemeral storage default 20 GB** — bump via `ephemeral_storage_gib` for large image extraction or temp data.
- **Fargate task max 16 vCPU + 120 GB RAM** — for bigger workloads, switch to EC2 capacity.
- **Service auto-scaling target tracking** has 3-min minimum scale-out latency. For sub-minute scaling, use Step Scaling on CW metric.
- **Service Connect outlier detection** kicks tasks out of routing on connection failures — but only with explicit `outlier_detection` config in service config.

---

## 6. Pytest worked example

```python
# tests/test_ecs_foundation.py
import boto3, pytest

ecs = boto3.client("ecs")
cw = boto3.client("cloudwatch")


def test_cluster_active(cluster_name):
    cluster = ecs.describe_clusters(clusters=[cluster_name])["clusters"][0]
    assert cluster["status"] == "ACTIVE"
    assert cluster["settings"][0]["value"] == "enhanced"   # Container Insights v2


def test_capacity_provider_strategy(cluster_name):
    cluster = ecs.describe_clusters(clusters=[cluster_name],
                                      include=["ATTACHMENTS"])["clusters"][0]
    cps = cluster.get("defaultCapacityProviderStrategy", [])
    cp_names = {cp["capacityProvider"] for cp in cps}
    assert "FARGATE" in cp_names
    assert "FARGATE_SPOT" in cp_names
    fargate_base = next((cp["base"] for cp in cps if cp["capacityProvider"] == "FARGATE"), 0)
    assert fargate_base >= 1, "FARGATE base should be ≥ 1 for availability"


def test_service_running(service_name, cluster_name):
    svc = ecs.describe_services(cluster=cluster_name, services=[service_name])["services"][0]
    assert svc["status"] == "ACTIVE"
    assert svc["runningCount"] == svc["desiredCount"]
    assert svc["deploymentConfiguration"]["deploymentCircuitBreaker"]["enable"] is True


def test_service_connect_active(service_name, cluster_name):
    svc = ecs.describe_services(cluster=cluster_name, services=[service_name])["services"][0]
    sc_config = svc.get("serviceConnectConfiguration", {})
    assert sc_config.get("enabled") is True


def test_task_role_least_priv(task_definition_arn):
    iam = boto3.client("iam")
    td = ecs.describe_task_definition(taskDefinition=task_definition_arn)["taskDefinition"]
    task_role_arn = td["taskRoleArn"]
    # Verify role has only specific resource ARNs, not "*"
    role_name = task_role_arn.split("/")[-1]
    policies = iam.list_attached_role_policies(RoleName=role_name)
    inline = iam.list_role_policies(RoleName=role_name)
    # Real check: parse each policy and assert no `Resource: *` for sensitive actions


def test_logs_kms_encrypted(log_group_name):
    logs = boto3.client("logs")
    lg = logs.describe_log_groups(logGroupNamePrefix=log_group_name)["logGroups"][0]
    assert lg.get("kmsKeyId"), "Log group not KMS-encrypted"


def test_autoscaling_attached(service_arn):
    aas = boto3.client("application-autoscaling")
    targets = aas.describe_scalable_targets(
        ServiceNamespace="ecs", ResourceIds=[service_arn],
    )["ScalableTargets"]
    assert targets, "No autoscaling target"
    assert targets[0]["MinCapacity"] >= 2     # HA
```

---

## 7. Five non-negotiables

1. **Container Insights v2 enabled** + KMS-encrypted log groups + 30-day retention.
2. **Capacity provider strategy** with FARGATE base ≥ 1 + FARGATE_SPOT for cost.
3. **Service Connect for all internal service-to-service** — no hardcoded IPs/DNS.
4. **Per-service task role + execution role separation** — never share roles.
5. **ECS Exec enabled with KMS-encrypted SSM session logs** — debug without SSH.

---

## 8. References

- [ECS Developer Guide](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html)
- [Service Connect](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect.html)
- [Capacity providers](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-capacity-providers.html)
- [Container Insights v2](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-Enhanced-Observability-ECS.html)
- [ECS Exec](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html)
- [Fargate pricing](https://aws.amazon.com/fargate/pricing/)
- [App Mesh deprecation (Sept 2026)](https://aws.amazon.com/blogs/containers/migrating-from-aws-app-mesh-to-amazon-ecs-service-connect/)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. ECS cluster + Fargate + Fargate Spot + Service Connect + task definition + auto-scaling + Container Insights v2 + ECS Exec. Wave 16. |
