# SOP — ECS Deployment Patterns (Rolling · Blue/Green via CodeDeploy · Canary · Circuit Breaker · ECS Anywhere)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · ECS deployment controllers — ECS (rolling) + CODE_DEPLOY (blue/green) + EXTERNAL · AWS CodeDeploy ECS deployment configurations · Canary 10% / Linear 10%/min / All-at-once · Circuit Breaker auto-rollback · Pre/Post-traffic Lambda hooks · ECS Anywhere (on-prem)

---

## 1. Purpose

- Codify the **3 ECS deployment controllers** + when to use each:
  - **ECS (rolling)** — built-in, simplest; `min_healthy_percent` / `max_healthy_percent` shape rolling deploy
  - **CODE_DEPLOY (blue/green)** — true blue/green via 2 ALB target groups; canary or linear traffic shift; pre/post hooks
  - **EXTERNAL** — for custom orchestrators (rare; ECS Anywhere with on-prem CD)
- Codify **Circuit Breaker** for rolling — auto-rollback on N% failed task starts (default disabled; ALWAYS enable in prod).
- Codify **Canary deployment patterns** — 10% traffic for 5 min, then 100% (or finer-grained 1% / 5% / 25% / 50% / 100% steps).
- Codify **Pre/Post traffic Lambda hooks** — validation gates between traffic shift steps.
- Codify **deployment_configuration parameters** — alarm-based rollback, deployment monitoring period.
- This is the **deployment specialisation**. Built on `ECS_CLUSTER_FOUNDATION`. Pairs with `ECS_PRODUCTION_HARDENING`.

When the SOW signals: "blue/green deploys", "canary releases", "zero-downtime ECS deploys", "auto-rollback on bad deploy".

---

## 2. Decision tree — which deployment controller

| Need | ECS (rolling) | CODE_DEPLOY (blue/green) |
|---|:---:|:---:|
| Simplest setup | ✅ | ❌ requires CodeDeploy app + 2 target groups |
| True blue/green (zero shared state during deploy) | ❌ | ✅ |
| Canary / linear traffic shift | ❌ | ✅ |
| Pre/post-traffic validation hooks | ❌ | ✅ |
| Stateful workloads (e.g., long-running connections) | ⚠️ | ✅ (drain old replicas) |
| Cost overhead | ✅ none | ⚠️ duplicate target groups during deploy |
| Auto-rollback on alarm | ✅ via circuit breaker | ✅ via CodeDeploy alarm config |

**Recommendation:**
- **Rolling + circuit breaker**: stateless web/API services, simple deploys
- **Blue/Green CodeDeploy**: anything customer-facing in prod where traffic shift granularity matters
- **External**: rare, for ECS Anywhere or custom CI

```
Blue/Green deployment lifecycle:

  T0   New task definition registered (version N+1)
       Blue env (current): N task replicas of vN, ALB target group A (100%)
       Green env (new): 0 replicas

  T1   CodeDeploy launches Green env tasks
       Green env: N replicas of vN+1, ALB target group B (0% traffic)

  T2   Pre-traffic Lambda hook runs against Green's TG B
       (smoke test, schema check, integration test)
       If fails → CodeDeploy aborts, Green tasks terminated

  T3   Traffic shift begins — Canary 10% to Green
       (Listener rule weighted 90% TG A, 10% TG B)

  T4   Wait period (e.g. 5 min); CW alarms watched
       If alarm → automatic rollback (TG A weight back to 100%)

  T5   Traffic shift to 100% Green
       (Listener weighted 0% TG A, 100% TG B)

  T6   Post-traffic Lambda hook runs against Green
       (final validation)

  T7   Termination wait period (default 0; can be 1h+ for safety)

  T8   Blue env (vN) tasks terminated
       Deployment complete
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — rolling + circuit breaker | **§3 Monolith** |
| Production — blue/green + canary + alarms + Lambda hooks | **§4 Blue/Green** |

---

## 3. Rolling Deployment + Circuit Breaker (default for most services)

### 3.1 CDK

```python
# stacks/ecs_rolling_deploy_stack.py
from aws_cdk import aws_ecs as ecs
from aws_cdk import Duration

# Built on ECS_CLUSTER_FOUNDATION cluster + task_def

api_service = ecs.FargateService(self, "ApiService",
    cluster=cluster,
    task_definition=task_def,
    desired_count=3,
    deployment_controller=ecs.DeploymentController(
        type=ecs.DeploymentControllerType.ECS,                # rolling
    ),
    # Circuit breaker — auto-rollback if deployment fails
    circuit_breaker=ecs.DeploymentCircuitBreaker(
        enable=True,
        rollback=True,
    ),
    # Deployment shape: never below 100% running; up to 200% during deploy
    min_healthy_percent=100,
    max_healthy_percent=200,
    # Health check grace period — give app time to start
    health_check_grace_period=Duration.minutes(2),
    # ECS-managed deployment alarms (2024+) — auto-rollback if alarm fires
    deployment_alarms=ecs.DeploymentAlarmConfig(
        alarm_names=[error_rate_alarm.alarm_name, latency_alarm.alarm_name],
        enable=True,
        rollback=True,
    ),
)
```

**Rolling lifecycle:**
- Deploy v2 → ECS launches new task with v2 (running parallel to v1)
- Wait for v2 task to pass health checks (target group registration)
- ECS stops one v1 task
- Repeat until all replaced
- If any task fails to start (or alarm fires) → circuit breaker rolls back

---

## 4. Blue/Green CodeDeploy (production canonical for customer-facing)

### 4.1 Architecture

```
   ALB Listener (port 443)
     │
     ├── Default action: Forward to TG-A (Blue, 100%)
     │
     └── (during deploy)
         Default action: Weighted Forward
            TG-A (Blue): 90%
            TG-B (Green): 10%   ← canary
         (then shifts to 0/100)
```

### 4.2 CDK

```python
from aws_cdk import aws_codedeploy as codedeploy
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_lambda as _lambda

# ── Two ALB target groups ────────────────────────────────────────────
tg_blue = elbv2.ApplicationTargetGroup(self, "TgBlue",
    target_group_name="api-blue",
    vpc=vpc, port=8080, protocol=elbv2.ApplicationProtocol.HTTP,
    target_type=elbv2.TargetType.IP,
    health_check=elbv2.HealthCheck(
        path="/healthz",
        protocol=elbv2.Protocol.HTTP,
        port="8080",
        healthy_threshold_count=2,
        unhealthy_threshold_count=3,
        interval=Duration.seconds(15),
        timeout=Duration.seconds(5),
    ),
    deregistration_delay=Duration.seconds(60),
)

tg_green = elbv2.ApplicationTargetGroup(self, "TgGreen",
    target_group_name="api-green",
    vpc=vpc, port=8080, protocol=elbv2.ApplicationProtocol.HTTP,
    target_type=elbv2.TargetType.IP,
    health_check=elbv2.HealthCheck(
        path="/healthz",
        protocol=elbv2.Protocol.HTTP, port="8080",
        healthy_threshold_count=2,
        interval=Duration.seconds(15),
    ),
    deregistration_delay=Duration.seconds(60),
)

# ── ALB Listener (production) ────────────────────────────────────────
prod_listener = alb.add_listener("ProdListener",
    port=443, protocol=elbv2.ApplicationProtocol.HTTPS,
    certificates=[acm_cert],
    ssl_policy=elbv2.SslPolicy.TLS13_RES,
    default_target_groups=[tg_blue],          # initially route to Blue
)

# ── Test listener (port 9090; CodeDeploy uses for pre-traffic hooks) ─
test_listener = alb.add_listener("TestListener",
    port=9090, protocol=elbv2.ApplicationProtocol.HTTP,
    default_target_groups=[tg_green],          # always routes to Green
)

# ── ECS Service with deployment controller = CODE_DEPLOY ────────────
api_service = ecs.FargateService(self, "ApiService",
    cluster=cluster,
    task_definition=task_def,
    desired_count=3,
    deployment_controller=ecs.DeploymentController(
        type=ecs.DeploymentControllerType.CODE_DEPLOY,
    ),
    health_check_grace_period=Duration.minutes(2),
)
api_service.attach_to_application_target_group(tg_blue)        # Initial registration

# ── Pre-traffic Lambda hook ──────────────────────────────────────────
pre_traffic_fn = _lambda.Function(self, "PreTrafficHook",
    function_name="CodeDeployHook_pre_traffic_api",             # MUST start with CodeDeployHook_
    runtime=_lambda.Runtime.PYTHON_3_12,
    handler="hook.pre_traffic",
    code=_lambda.Code.from_asset("src/codedeploy_hooks"),
    timeout=Duration.minutes(15),                                # max for Lambda hook
    environment={
        "TEST_LISTENER_URL": "http://api.example.com:9090",
    },
)
pre_traffic_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["codedeploy:PutLifecycleEventHookExecutionStatus"],
    resources=["*"],
))

# Pre-traffic hook code (src/codedeploy_hooks/hook.py):
#   import boto3, requests
#   cd = boto3.client("codedeploy")
#
#   def pre_traffic(event, context):
#       deployment_id = event["DeploymentId"]
#       hook_id = event["LifecycleEventHookExecutionId"]
#       try:
#           r = requests.get(f"{TEST_LISTENER_URL}/healthz", timeout=10)
#           assert r.status_code == 200
#           r = requests.post(f"{TEST_LISTENER_URL}/api/test/checkout", json={...})
#           assert r.status_code == 200
#           status = "Succeeded"
#       except Exception as e:
#           status = "Failed"
#       cd.put_lifecycle_event_hook_execution_status(
#           deploymentId=deployment_id,
#           lifecycleEventHookExecutionId=hook_id,
#           status=status,
#       )
#       return {"statusCode": 200}

# ── Post-traffic Lambda hook (same pattern) ─────────────────────────
post_traffic_fn = _lambda.Function(self, "PostTrafficHook",
    function_name="CodeDeployHook_post_traffic_api",
    # ... same shape ...
)

# ── CW alarms — auto-rollback triggers ──────────────────────────────
error_rate_alarm = cw.Alarm(self, "ApiErrorRateAlarm",
    metric=cw.Metric(
        namespace="AWS/ApplicationELB",
        metric_name="HTTPCode_Target_5XX_Count",
        dimensions_map={
            "TargetGroup": tg_green.target_group_full_name,
            "LoadBalancer": alb.load_balancer_full_name,
        },
        statistic="Sum", period=Duration.minutes(1),
    ),
    threshold=10,
    evaluation_periods=2,
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
)

latency_alarm = cw.Alarm(self, "ApiLatencyAlarm",
    metric=cw.Metric(
        namespace="AWS/ApplicationELB",
        metric_name="TargetResponseTime",
        dimensions_map={"TargetGroup": tg_green.target_group_full_name},
        statistic="p99", period=Duration.minutes(1),
    ),
    threshold=1.0,                                           # 1s p99
    evaluation_periods=3,
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
)

# ── CodeDeploy application + deployment group ───────────────────────
cd_app = codedeploy.EcsApplication(self, "CdApp",
    application_name=f"{env_name}-api-cd-app",
)

cd_dg = codedeploy.EcsDeploymentGroup(self, "CdDg",
    application=cd_app,
    deployment_group_name=f"{env_name}-api-dg",
    service=api_service,
    blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
        blue_target_group=tg_blue,
        green_target_group=tg_green,
        listener=prod_listener,
        test_listener=test_listener,
        deployment_approval_wait_time=Duration.minutes(15),     # manual approval window
        termination_wait_time=Duration.hours(1),                # keep Blue 1h post-cut for fast rollback
    ),
    deployment_config=codedeploy.EcsDeploymentConfig.CANARY_10_PERCENT_5_MINUTES,
    # Available configs:
    #   ALL_AT_ONCE              — 100% traffic shift instantly
    #   LINEAR_10_PERCENT_1_MINUTE  (10 min total shift)
    #   LINEAR_10_PERCENT_3_MINUTES (30 min total)
    #   CANARY_10_PERCENT_5_MINUTES (10% for 5 min then 90%)
    #   CANARY_10_PERCENT_15_MINUTES
    #   CANARY_10_PERCENT_30_MINUTES
    #   CANARY_10_PERCENT_45_MINUTES
    alarms=[error_rate_alarm, latency_alarm],
    auto_rollback=codedeploy.AutoRollbackConfig(
        deployment_in_alarm=True,                               # CW alarm
        failed_deployment=True,                                  # CodeDeploy lifecycle failure
        stopped_deployment=True,                                 # manual cancel
    ),
)
```

### 4.3 Custom deployment config (e.g., 5-step linear)

```python
# Custom — useful for fine-grained canary
custom_config = codedeploy.EcsDeploymentConfig(self, "Custom5Step",
    deployment_config_name=f"{env_name}-5step-canary",
    traffic_routing=codedeploy.TimeBasedCanaryTrafficRouting(
        interval=Duration.minutes(2),
        percentage=20,
    ),
    # OR: TimeBasedLinearTrafficRouting (gradual ramp)
    # OR: AllAtOnceTrafficRouting
)
```

---

## 5. ECS Anywhere (on-prem deployment) — brief

For workloads on customer hardware (factory, edge):
```bash
# Register on-prem instance with ECS
aws ssm create-activation \
  --iam-role ecsAnywhereRole \
  --registration-limit 100

# On the on-prem host:
curl --proto "=https" -o "/tmp/ecs-anywhere-install.sh" \
  "https://amazon-ecs-agent.s3.amazonaws.com/ecs-anywhere-install-latest.sh"
sudo bash /tmp/ecs-anywhere-install.sh \
  --region us-east-1 \
  --cluster prod-cluster \
  --activation-id <id> \
  --activation-code <code>

# Deploy with launch_type=EXTERNAL or capacity-provider for on-prem hosts
```

---

## 6. Common gotchas

- **Circuit breaker only triggers on TASK launch failures**, not application-level errors. For app errors, use deployment alarms (CW).
- **Deployment alarms (`deployment_alarms`)** require ECS deployment controller v3 — older accounts may need updated agent.
- **Blue/Green termination_wait_time** retains old (Blue) tasks after cut — extra cost during this window. Keep at 1h for fast rollback; reduce to 0 for cost-sensitive.
- **Pre-traffic hook Lambda must be named `CodeDeployHook_*`** — case-sensitive.
- **Pre/post hooks have 60-min max execution** — but typically should complete in < 5 min.
- **Test listener (port 9090) is internal-only** — don't expose externally; security group should only allow VPC traffic.
- **ALB target group `deregistration_delay`** matters during cuts — too short = in-flight requests dropped; too long = slower rollback.
- **CodeDeploy application + deployment group** must be created FIRST, then ECS service `attach_to_target_group` updates registration.
- **CodeDeploy doesn't update task definition itself** — it shifts traffic to existing TaskDefinition revision. Update task def via separate API/CDK first.
- **CANARY_10_PERCENT_5_MINUTES** = 10% for 5 min, then 100%. Not gradual ramp. For ramp, use `LINEAR_*`.
- **Auto-rollback on `failed_deployment: true`** = CodeDeploy lifecycle event failure (Lambda hook returns "Failed").
- **Multiple deployment groups per service NOT allowed** — only one active CodeDeploy at a time.
- **Force a new deployment** to apply unchanged task def (e.g., to refresh secrets): `aws ecs update-service --force-new-deployment`.
- **Blue/Green doubles target group cost** during deploy window — minor but accounted for.
- **CodeDeploy console UI sometimes lags** — use CLI/API for status: `aws deploy get-deployment --deployment-id`.

---

## 7. Pytest worked example

```python
# tests/test_deployments.py
import boto3, pytest

ecs = boto3.client("ecs")
cd = boto3.client("codedeploy")
cw = boto3.client("cloudwatch")


def test_service_uses_codedeploy(service_name, cluster_name):
    svc = ecs.describe_services(cluster=cluster_name, services=[service_name])["services"][0]
    assert svc["deploymentController"]["type"] == "CODE_DEPLOY"


def test_circuit_breaker_enabled_on_rolling_service(service_name, cluster_name):
    """For ECS-controlled services (rolling)."""
    svc = ecs.describe_services(cluster=cluster_name, services=[service_name])["services"][0]
    if svc["deploymentController"]["type"] == "ECS":
        cb = svc["deploymentConfiguration"]["deploymentCircuitBreaker"]
        assert cb["enable"] is True
        assert cb["rollback"] is True


def test_codedeploy_app_exists(app_name):
    apps = cd.list_applications()["applications"]
    assert app_name in apps


def test_deployment_group_has_alarms(app_name, dg_name):
    dg = cd.get_deployment_group(applicationName=app_name, deploymentGroupName=dg_name)["deploymentGroupInfo"]
    alarms = dg.get("alarmConfiguration", {})
    assert alarms.get("enabled") is True
    assert alarms.get("alarms"), "No alarms wired"


def test_auto_rollback_configured(app_name, dg_name):
    dg = cd.get_deployment_group(applicationName=app_name, deploymentGroupName=dg_name)["deploymentGroupInfo"]
    rb = dg.get("autoRollbackConfiguration", {})
    assert rb.get("enabled") is True
    events = rb.get("events", [])
    assert "DEPLOYMENT_FAILURE" in events
    assert "DEPLOYMENT_STOP_ON_ALARM" in events


def test_recent_deployment_succeeded(app_name, dg_name):
    deps = cd.list_deployments(
        applicationName=app_name,
        deploymentGroupName=dg_name,
        includeOnlyStatuses=["Succeeded", "Failed", "Stopped"],
        maxResults=5,
    )["deployments"]
    if deps:
        latest = cd.get_deployment(deploymentId=deps[0])["deploymentInfo"]
        # Allow last to be either Succeeded or Stopped (not Failed)
        assert latest["status"] != "Failed"


def test_pre_traffic_lambda_has_correct_prefix(env_name):
    lambda_client = boto3.client("lambda")
    fns = lambda_client.list_functions()["Functions"]
    pre_traffic = [f for f in fns if f["FunctionName"].startswith("CodeDeployHook_pre_traffic")]
    assert pre_traffic, "Pre-traffic Lambda missing or misnamed"
```

---

## 8. Five non-negotiables

1. **Circuit breaker enabled** on all ECS-controlled rolling deploys — auto-rollback on task failure.
2. **CodeDeploy blue/green for customer-facing prod services** with `CANARY_10_PERCENT_5_MINUTES` minimum.
3. **CW alarms wired to auto-rollback** — error rate + latency p99 minimum.
4. **Pre-traffic Lambda hook validates Green before any traffic** — smoke test + integration check.
5. **`termination_wait_time: 1h`** for Blue/Green prod — fast rollback window if issue surfaces post-cut.

---

## 9. References

- [ECS deployment types](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-types.html)
- [CodeDeploy ECS blue/green](https://docs.aws.amazon.com/codedeploy/latest/userguide/deployments-create-ecs.html)
- [Deployment circuit breaker](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html)
- [Deployment alarms (2024+)](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-alarm-failure.html)
- [Pre/Post-traffic Lambda hooks](https://docs.aws.amazon.com/codedeploy/latest/userguide/reference-appspec-file-structure-hooks.html)
- [ECS Anywhere](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-anywhere.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. ECS rolling + CodeDeploy blue/green + canary configs + pre/post hooks + circuit breaker + deployment alarms. Wave 16. |
