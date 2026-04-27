# SOP — ECS Production Hardening (task IAM least-priv · auto-scaling · GuardDuty Runtime · ECR Inspector · network · secrets · Container Insights)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · ECS task IAM hardening · Service auto-scaling (target tracking + step + scheduled) · GuardDuty Runtime Monitoring for ECS · ECR enhanced scan + Inspector · Networking — VPC + private endpoints · Secrets Manager + Parameter Store · Container Insights v2 alarms · App Runner alternative for tiny services

---

## 1. Purpose

- Codify the **production-hardening layer** for ECS — security, observability, scaling correctness.
- Codify **Task IAM least-privilege** — separate roles per service, scoped policies, no `Resource: *`.
- Codify **service auto-scaling** patterns: target tracking (CPU, memory, ALB request count), step scaling (custom CW metrics), scheduled (predictable load).
- Codify **GuardDuty Runtime Monitoring for ECS** (GA Sept 2024) — agent-less detection of malicious behavior in tasks.
- Codify **ECR + Inspector** for image vulnerability scanning + CI gating.
- Codify **network hardening** — Fargate ENIs in private subnets + VPC endpoints + Security Groups + WAF on ALB.
- Codify **secrets management** — Secrets Manager + SSM Parameter Store integration via task definition.
- Codify **App Runner** as alternative to ECS for stateless web apps (zero-ops alternative for < 5 services).
- This is the **ECS production hardening specialisation**. Built on `ECS_CLUSTER_FOUNDATION` + `ECS_DEPLOYMENT_PATTERNS`.

When the SOW signals: "secure ECS", "ECS for PCI/HIPAA", "auto-scale ECS at scale", "container threat detection", "secrets in ECS", "App Runner".

---

## 2. Decision tree — App Runner vs ECS Fargate vs ECS EC2

| Need | App Runner | ECS Fargate | ECS EC2 |
|---|:---:|:---:|:---:|
| Single small web app, no ops | ✅ | ⚠️ overkill | ❌ |
| Multiple services + service mesh | ❌ | ✅ Service Connect | ✅ |
| Custom networking (private subnet, peering) | ⚠️ VPC connector only | ✅ | ✅ |
| Spot pricing (cost-sensitive) | ❌ | ✅ FARGATE_SPOT | ✅ EC2 spot |
| GPU / specialty hardware | ❌ | ❌ | ✅ |
| Auto-scaling | ✅ built-in | ✅ Application Auto Scaling | ✅ |
| Cost ceiling | $$$ at scale | medium | $ at scale |

```
Auto-scaling strategies:

  Target tracking (recommended default)
    - Pick metric (CPU 50%, Memory 70%, RequestCountPerTarget 1000)
    - ECS scales to maintain target
    - Scale-out cooldown 30-60s; scale-in cooldown 60s+
    
  Step scaling (custom CW metric)
    - "When SQS messages > 100, add 5 tasks"
    - Use for queue-driven workers
    - Define multiple steps (200 → +10, 500 → +20)
    
  Scheduled (predictable patterns)
    - "Every Monday 8am, scale to 20 tasks"
    - Stack on top of target tracking for known peaks
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — basic IAM + auto-scaling + ECR scanning | **§3 Monolith** |
| Production — full hardening + GuardDuty Runtime + Kyverno-equivalent + WAF | **§5 Production** |

---

## 3. Task IAM least-privilege

### 3.1 Per-service task role pattern

```python
# stacks/ecs_iam_stack.py
from aws_cdk import aws_iam as iam

# RULE: One task role per service. Never share.
# RULE: No "Resource: *" except for actions that don't support resource scoping.

# api-svc task role
api_task_role = iam.Role(self, "ApiTaskRole",
    assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    role_name=f"{env_name}-ecs-api-task-role",
)

# Specific resource ARNs only
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[
        f"arn:aws:secretsmanager:{region}:{account}:secret:{env_name}/api/db-creds-*",
    ],
))
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
    resources=[
        f"arn:aws:dynamodb:{region}:{account}:table/{env_name}-app",
        f"arn:aws:dynamodb:{region}:{account}:table/{env_name}-app/index/*",
    ],
))
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["s3:GetObject", "s3:PutObject"],
    resources=[
        f"arn:aws:s3:::{env_name}-app-uploads/*",
    ],
    conditions={
        "StringEquals": {
            "s3:x-amz-server-side-encryption": "aws:kms",
        },
    },
))
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["kms:Decrypt", "kms:GenerateDataKey"],
    resources=[kms_key.key_arn],
    conditions={
        "StringEquals": {"kms:ViaService": [
            f"s3.{region}.amazonaws.com",
            f"dynamodb.{region}.amazonaws.com",
        ]},
    },
))

# Permission boundary (optional but recommended for prod)
api_task_role.attach_inline_policy(iam.Policy(self, "TaskBoundary",
    document=iam.PolicyDocument(statements=[
        iam.PolicyStatement(
            effect=iam.Effect.DENY,
            actions=["iam:*", "organizations:*"],
            resources=["*"],
        ),
    ]),
))
```

### 3.2 Execution role (separate, ECS agent uses)

```python
exec_role = iam.Role(self, "ExecRole",
    assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
)
exec_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name(
    "service-role/AmazonECSTaskExecutionRolePolicy",
))
# For pulling secrets at task start
exec_role.add_to_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue", "ssm:GetParameters"],
    resources=[
        f"arn:aws:secretsmanager:{region}:{account}:secret:{env_name}/api/*",
        f"arn:aws:ssm:{region}:{account}:parameter/{env_name}/api/*",
    ],
))
exec_role.add_to_policy(iam.PolicyStatement(
    actions=["kms:Decrypt"],
    resources=[kms_key.key_arn],
))
```

---

## 4. Service auto-scaling — production patterns

### 4.1 Target tracking + custom metrics

```python
from aws_cdk import aws_applicationautoscaling as autoscaling
from aws_cdk import aws_cloudwatch as cw

scaling = api_service.auto_scale_task_count(
    min_capacity=3,                              # always-on baseline
    max_capacity=50,
)

# 1. CPU target tracking
scaling.scale_on_cpu_utilization("CpuTarget",
    target_utilization_percent=50,
    scale_in_cooldown=Duration.seconds(60),
    scale_out_cooldown=Duration.seconds(30),
)

# 2. Memory target tracking
scaling.scale_on_memory_utilization("MemTarget",
    target_utilization_percent=70,
)

# 3. ALB request count per target
scaling.scale_on_request_count("AlbReqTarget",
    target_group=target_group,
    requests_per_target=500,                     # per-task req/min target
)

# 4. Custom CW metric — SQS queue depth (for worker services)
scaling.scale_on_metric("SqsBacklog",
    metric=cw.Metric(
        namespace="AWS/SQS",
        metric_name="ApproximateNumberOfMessagesVisible",
        dimensions_map={"QueueName": "prod-jobs"},
        statistic="Maximum",
        period=Duration.minutes(1),
    ),
    scaling_steps=[
        autoscaling.ScalingInterval(upper=10,  change=-1),
        autoscaling.ScalingInterval(lower=50,  change=+2),
        autoscaling.ScalingInterval(lower=200, change=+5),
        autoscaling.ScalingInterval(lower=1000, change=+10),
    ],
    adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
    cooldown=Duration.seconds(60),
    metric_aggregation_type=autoscaling.MetricAggregationType.MAXIMUM,
)

# 5. Scheduled scaling (predictable peaks)
scaling.scale_on_schedule("MorningPeak",
    schedule=autoscaling.Schedule.cron(hour="7", minute="55"),
    min_capacity=10,                             # bump baseline before peak
    max_capacity=50,
)
scaling.scale_on_schedule("EveningWind",
    schedule=autoscaling.Schedule.cron(hour="22", minute="0"),
    min_capacity=3,
    max_capacity=30,
)
```

---

## 5. GuardDuty Runtime Monitoring for ECS (Sept 2024 GA)

```python
from aws_cdk import aws_guardduty as gd

# Enable Runtime Monitoring at GuardDuty detector (Audit account or single-account)
gd.CfnDetector(self, "Detector",
    enable=True,
    finding_publishing_frequency="FIFTEEN_MINUTES",
    features=[
        gd.CfnDetector.CFNFeatureConfigurationProperty(
            name="RUNTIME_MONITORING",
            status="ENABLED",
            additional_configuration=[
                gd.CfnDetector.CFNFeatureAdditionalConfigurationProperty(
                    name="ECS_FARGATE_AGENT_MANAGEMENT",
                    status="ENABLED",                # auto-deploy agent into Fargate tasks
                ),
                gd.CfnDetector.CFNFeatureAdditionalConfigurationProperty(
                    name="EC2_AGENT_MANAGEMENT",
                    status="ENABLED",                # for ECS on EC2
                ),
            ],
        ),
    ],
)
# Findings appear in Security Hub aggregator (see ENTERPRISE_SECURITY_HUB_GD_ORG)
```

GuardDuty Runtime Monitoring detects:
- Cryptocurrency mining attempts in containers
- Reverse shells / unusual outbound connections
- File system tampering with sensitive paths
- Privilege escalation attempts
- Container drift from image baseline

---

## 6. ECR + Inspector for image scanning

```python
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_inspectorv2 as inspector

# ECR with KMS + immutability + scan on push (basic)
repo = ecr.Repository(self, "ApiRepo",
    repository_name=f"{env_name}/api",
    encryption=ecr.RepositoryEncryption.KMS,
    encryption_key=kms_key,
    image_tag_mutability=ecr.TagMutability.IMMUTABLE,
    image_scan_on_push=True,
)

# Inspector enhanced scan (account-wide config)
inspector.CfnFilter(self, "SuppressInfoFindings",
    name=f"{env_name}-suppress-info-cves",
    filter_action="SUPPRESS",
    filter_criteria={
        "severity": [{"comparison": "EQUALS", "value": "INFORMATIONAL"}],
        "ecrImageRepositoryName": [{"comparison": "EQUALS", "value": repo.repository_name}],
    },
)
```

```bash
# CI/CD gate — fail build if HIGH/CRITICAL CVEs in pushed image
DIGEST=$(aws ecr describe-images --repository-name api --image-ids imageTag=$TAG \
  --query 'imageDetails[0].imageDigest' --output text)

# Wait for Inspector scan to complete (60-300s)
sleep 90

CRITICAL=$(aws inspector2 list-findings \
  --filter-criteria "{\"ecrImageHash\":[{\"comparison\":\"EQUALS\",\"value\":\"$DIGEST\"}],
                       \"severity\":[{\"comparison\":\"EQUALS\",\"value\":\"CRITICAL\"}]}" \
  --query 'length(findings)' --output text)

if [ "$CRITICAL" -gt 0 ]; then
  echo "BLOCKING DEPLOY: $CRITICAL CRITICAL CVEs in $TAG"
  exit 1
fi
```

---

## 7. Network hardening

### 7.1 Fargate ENIs in private subnets only

```python
api_service = ecs.FargateService(self, "ApiService",
    cluster=cluster,
    task_definition=task_def,
    vpc_subnets=ec2.SubnetSelection(
        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,    # NO public IP
    ),
    assign_public_ip=False,
    security_groups=[task_sg],
    # ... rest of service config ...
)
```

### 7.2 Security Group — least-port

```python
task_sg = ec2.SecurityGroup(self, "TaskSg",
    vpc=vpc,
    description="ECS task SG — only ALB ingress allowed",
    allow_all_outbound=False,                        # explicit egress only
)
# Inbound from ALB SG only (port 8080)
task_sg.add_ingress_rule(
    peer=alb_sg,
    connection=ec2.Port.tcp(8080),
    description="ALB → task",
)
# Egress: only to RDS, Secrets Manager via VPC endpoint, etc.
task_sg.add_egress_rule(
    peer=rds_sg,
    connection=ec2.Port.tcp(5432),
    description="Task → RDS",
)
task_sg.add_egress_rule(
    peer=ec2.Peer.prefix_list("pl-secretsmanager"),  # VPC endpoint prefix list
    connection=ec2.Port.tcp(443),
    description="Task → Secrets Manager VPC endpoint",
)
```

### 7.3 VPC endpoints (avoid NAT cost + improve security)

```python
# Required for Fargate without NAT Gateway:
for svc in ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "ssm",
            "kms", "monitoring", "ssmmessages", "ec2messages"]:
    ec2.InterfaceVpcEndpoint(self, f"VpcE_{svc.replace('.', '_')}",
        vpc=vpc,
        service=ec2.InterfaceVpcEndpointAwsService(svc),
        subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        private_dns_enabled=True,
        security_groups=[endpoint_sg],
    )

# S3 gateway endpoint (free)
ec2.GatewayVpcEndpoint(self, "S3Endpoint",
    vpc=vpc,
    service=ec2.GatewayVpcEndpointAwsService.S3,
    subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
)
```

---

## 8. Secrets management

### 8.1 Secrets Manager + automatic rotation

```python
db_secret = sm.Secret(self, "DbSecret",
    secret_name=f"{env_name}/api/db-creds",
    encryption_key=kms_key,
    generate_secret_string=sm.SecretStringGenerator(
        secret_string_template='{"username": "app"}',
        generate_string_key="password",
        password_length=32,
    ),
)

# Inject into task definition at runtime
task_def.add_container("api",
    secrets={
        "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
        "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
    },
    # ... rest of container ...
)

# Rotation Lambda for RDS credentials (AWS-managed)
db_secret.add_rotation_schedule("Rotation",
    rotation_lambda=secret_rotation_fn,
    automatically_after=Duration.days(30),
)
```

### 8.2 SSM Parameter Store for non-secret config

```python
ssm.StringParameter(self, "ApiTimeoutMs",
    parameter_name=f"/{env_name}/api/request-timeout-ms",
    string_value="30000",
    description="API request timeout in ms",
)

# In task def
task_def.add_container("api",
    environment={"DB_HOST": "..."},
    secrets={
        "TIMEOUT_MS": ecs.Secret.from_ssm_parameter(timeout_param),
    },
)
```

---

## 9. Common gotchas

- **Per-service task role is mandatory** — sharing `ecsTaskRole` across services means a compromise of one task leaks all secrets/permissions.
- **Resource: \*** in IAM policies = anti-pattern. Always specify ARN. The few actions that don't support resource scoping (some `*:Describe*`) need conditions: `aws:CalledViaService`.
- **Auto-scaling cooldowns**: scale-out should be FAST (30s); scale-in should be SLOW (60s+) to avoid flapping.
- **Step scaling on slow-changing metrics** (e.g., SQS depth measured every 1 min) lags reality by 1-2 min. For sub-minute scaling, use target tracking with high-frequency custom metrics.
- **ALB RequestCountPerTarget** is per-target-group, not per-target. If multiple services share TG, scaling math wrong.
- **Scheduled scaling overrides target tracking** during the window — verify that target tracking comes back online after scheduled period ends.
- **GuardDuty Runtime Monitoring agent in Fargate** = ~50 MB image extension + 256 MB RAM extra. Plan task memory.
- **GuardDuty Runtime can throttle on noisy syscalls** (e.g., webhook receivers with high request rate). Use suppression rules.
- **Inspector v2 image scan completes in 60-300 sec** — CI gate must poll, not assume immediate.
- **NAT Gateway hourly cost ($30/mo) + data transfer ($0.045/GB)** add up — VPC endpoints save 70% for AWS API traffic.
- **ECR image pull bandwidth** in private subnet without NAT requires `ecr.api` AND `ecr.dkr` AND `s3` (gateway) endpoints. Missing any = pull fails.
- **Secrets injected via task definition** = visible in `aws ecs describe-task` only as ARNs (good); but env vars are visible in CW Logs if app prints them. NEVER print secrets to stdout.
- **Secrets Manager rotation breaks if app caches creds for > rotation interval** — apps must re-fetch on connection failure.
- **App Runner has fewer config knobs** than ECS — choose for true zero-ops apps; switch to ECS when complexity grows.

---

## 10. Pytest worked example

```python
# tests/test_ecs_hardening.py
import boto3, pytest

ecs = boto3.client("ecs")
iam = boto3.client("iam")
ec2 = boto3.client("ec2")
gd = boto3.client("guardduty")


def test_per_service_task_roles_unique():
    """Each service should have its own task role (no sharing)."""
    services = ecs.list_services(cluster=cluster_name)["serviceArns"]
    roles = []
    for svc_arn in services:
        svc = ecs.describe_services(cluster=cluster_name, services=[svc_arn])["services"][0]
        td = ecs.describe_task_definition(taskDefinition=svc["taskDefinition"])["taskDefinition"]
        roles.append(td["taskRoleArn"])
    assert len(set(roles)) == len(roles), "Task roles shared across services"


def test_no_wildcard_resource_in_task_role(task_role_arn):
    role_name = task_role_arn.split("/")[-1]
    inline = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    for p in inline:
        doc = iam.get_role_policy(RoleName=role_name, PolicyName=p)["PolicyDocument"]
        for stmt in doc["Statement"]:
            if stmt["Effect"] == "Allow":
                resources = stmt.get("Resource", [])
                if isinstance(resources, str):
                    resources = [resources]
                assert "*" not in resources or stmt.get("Action", "") in ["logs:CreateLogStream"], \
                    f"Wildcard resource in policy {p}: {stmt}"


def test_task_in_private_subnet(service_name, cluster_name):
    svc = ecs.describe_services(cluster=cluster_name, services=[service_name])["services"][0]
    nc = svc["networkConfiguration"]["awsvpcConfiguration"]
    assert nc["assignPublicIp"] == "DISABLED"


def test_security_group_no_open_ingress(task_sg_id):
    sg = ec2.describe_security_groups(GroupIds=[task_sg_id])["SecurityGroups"][0]
    for rule in sg["IpPermissions"]:
        for ip_range in rule.get("IpRanges", []):
            assert ip_range["CidrIp"] != "0.0.0.0/0", "Task SG has 0.0.0.0/0 ingress"


def test_guardduty_runtime_monitoring_enabled():
    detectors = gd.list_detectors()["DetectorIds"]
    detector = gd.get_detector(DetectorId=detectors[0])
    rt = next((f for f in detector["Features"] if f["Name"] == "RUNTIME_MONITORING"), None)
    assert rt and rt["Status"] == "ENABLED"
    fargate_addon = next((a for a in rt.get("AdditionalConfiguration", [])
                           if a["Name"] == "ECS_FARGATE_AGENT_MANAGEMENT"), None)
    assert fargate_addon and fargate_addon["Status"] == "ENABLED"


def test_no_critical_cves_in_running_image(repo_name, current_image_tag):
    inspector = boto3.client("inspector2")
    ecr = boto3.client("ecr")
    image = ecr.describe_images(
        repositoryName=repo_name,
        imageIds=[{"imageTag": current_image_tag}],
    )["imageDetails"][0]
    findings = inspector.list_findings(
        filterCriteria={
            "ecrImageHash": [{"comparison": "EQUALS", "value": image["imageDigest"]}],
            "severity": [{"comparison": "EQUALS", "value": "CRITICAL"}],
        },
    )["findings"]
    assert not findings, f"Critical CVEs: {[f['title'] for f in findings]}"


def test_autoscaling_attached_with_min_capacity_above_1(service_arn):
    aas = boto3.client("application-autoscaling")
    targets = aas.describe_scalable_targets(
        ServiceNamespace="ecs", ResourceIds=[service_arn],
    )["ScalableTargets"]
    assert targets
    assert targets[0]["MinCapacity"] >= 2, "MinCapacity < 2 = no HA"
```

---

## 11. Five non-negotiables

1. **Per-service task role + execution role separation** — never share roles.
2. **No `Resource: *`** in task IAM policies (except CW logs / X-Ray, which lack ARN scoping).
3. **Fargate tasks in private subnets only** + assign_public_ip=False + Security Group with explicit ingress.
4. **GuardDuty Runtime Monitoring** for ECS Fargate with `ECS_FARGATE_AGENT_MANAGEMENT: ENABLED`.
5. **Inspector + CI gate** blocking CRITICAL CVEs in production image deploys.

---

## 12. References

- [ECS task IAM best practices](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)
- [Service auto-scaling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html)
- [GuardDuty Runtime Monitoring for ECS](https://docs.aws.amazon.com/guardduty/latest/ug/runtime-monitoring.html)
- [Inspector + ECR](https://docs.aws.amazon.com/inspector/latest/user/scanning-ecr.html)
- [VPC endpoints for ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/vpc-endpoints.html)
- [Secrets Manager + ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/specifying-sensitive-data-secrets.html)
- [App Runner](https://docs.aws.amazon.com/apprunner/latest/dg/what-is-apprunner.html)

---

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Task IAM least-priv + auto-scaling (3 modes) + GuardDuty Runtime + ECR/Inspector + network + secrets + App Runner alternative. Wave 16. |
