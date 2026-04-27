# SOP — AWS Resilience Hub + Fault Injection Service (FIS) (resiliency policies · assessments · chaos engineering · game day automation)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Resilience Hub · Resiliency Policies (RPO/RTO targets) · Application import (CFN/CDK/AppRegistry) · AWS Fault Injection Service (FIS) · FIS Action Library (EC2/ECS/EKS/RDS/Network/SSM stress) · Experiment Templates · Stop Conditions · CloudWatch alarms

---

## 1. Purpose

- Codify **AWS Resilience Hub** as the canonical **resilience assessment + tracking tool** — measures app posture vs target RPO/RTO, scores it, recommends improvements.
- Codify **Fault Injection Service (FIS)** as the canonical **chaos engineering** tool — purpose-built fault injection (kill EC2, throttle network, fail RDS over) with **stop conditions** (CloudWatch alarms) for safety.
- Codify the **game day discipline**: hypothesis → experiment → measure → improve → repeat.
- Codify **resilience policies** with explicit RPO/RTO/RTOR per disruption type (region/AZ/cluster/instance/network).
- Codify **assessment runs** in CI/CD — track resilience score over time, prevent regressions.
- This is the **resilience-validation specialisation**. Pairs with `DR_MULTI_REGION_PATTERNS` (the patterns being validated), `DR_ROUTE53_ARC` (failover orchestration tested via FIS), `DR_BACKUP_VAULT_LOCK` (backup-restore validated).

When the SOW signals: "chaos engineering", "game days", "resilience score", "validate DR plan", "we don't know if our DR works", "RPO/RTO contractual obligation".

---

## 2. Decision tree — what to inject; assessment cadence

```
Resilience disruption type → FIS action library:
  Region failure                 → fis:network/disrupt-connectivity (region-scope)
  AZ failure                     → fis:network/disrupt-connectivity (subnet-scope)
                                    + fis:ec2/StopInstances (AZ-filtered)
  EKS pod failure                → fis:eks/pod-cpu-stress, pod-memory-stress
                                    + fis:eks/pod-delete
  ECS task failure               → fis:ecs/StopTask
  RDS failure                    → fis:rds/FailoverDBCluster (test multi-AZ failover)
  Database connection exhaustion → fis:rds/StopReplication + Lambda block
  Network latency injection      → fis:network/latency
  Network packet loss            → fis:network/packet-loss
  Service throttling             → fis:apigateway/Throttle (custom Lambda action)

Assessment cadence:
  Per-deploy (CI gate)            → resilience-hub start-app-assessment (smoke)
  Weekly                          → full FIS chaos game (random fault injection)
  Monthly                         → full DR drill (regional failover end-to-end)
  Quarterly                       → executive game day (multi-team observation)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single app + 1 resiliency policy + 3 FIS experiments | **§3 Monolith** |
| Production — multi-app + assessment in CI + monthly drills + autoshift | **§5 Production** |

---

## 3. Resilience Hub — assessments + scoring

### 3.1 CDK

```python
# stacks/resilience_hub_stack.py
from aws_cdk import Stack
from aws_cdk import aws_resiliencehub as rh
from constructs import Construct
import json


class ResilienceHubStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 app_cfn_stack_arn: str,         # the app's CDK/CFN stack to assess
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Resiliency Policy — explicit RPO/RTO targets ───────────
        policy = rh.CfnResiliencyPolicy(self, "ProdPolicy",
            policy_name="prod-99-99",
            tier="MissionCritical",                # also: Important, CoreServices, Standard
            data_location_constraint="AnyLocation",
            policy={
                "AZ": rh.CfnResiliencyPolicy.FailurePolicyProperty(
                    rpo_in_secs=60,                # max 1 min data loss on AZ failure
                    rto_in_secs=300,               # max 5 min recovery
                ),
                "Hardware": rh.CfnResiliencyPolicy.FailurePolicyProperty(
                    rpo_in_secs=60, rto_in_secs=300,
                ),
                "Software": rh.CfnResiliencyPolicy.FailurePolicyProperty(
                    rpo_in_secs=60, rto_in_secs=300,
                ),
                "Region": rh.CfnResiliencyPolicy.FailurePolicyProperty(
                    rpo_in_secs=300,               # 5 min RPO on region failure
                    rto_in_secs=900,               # 15 min RTO
                ),
            },
        )

        # ── 2. Application — points at CFN stack OR resource group ────
        app = rh.CfnApp(self, "App",
            name="prod-app",
            description="Customer-facing API + DB",
            app_template_body=json.dumps({
                "resources": [
                    {
                        "logicalResourceId": {"identifier": "OrdersTable"},
                        "type": "AWS::DynamoDB::Table",
                    },
                    {
                        "logicalResourceId": {"identifier": "AppCluster"},
                        "type": "AWS::ECS::Service",
                    },
                    {
                        "logicalResourceId": {"identifier": "OrderApi"},
                        "type": "AWS::ApiGateway::RestApi",
                    },
                ],
                "appComponents": [{
                    "name": "OrdersComponent",
                    "type": "AWS::ResilienceHub::ApplicationComponent",
                    "resourceNames": ["OrdersTable", "AppCluster", "OrderApi"],
                }],
                "version": 2,
            }),
            resilience_policy_arn=policy.attr_arn,
            permission_model=rh.CfnApp.PermissionModelProperty(
                type="LegacyIAMUser",                    # or RoleBased
                cross_account_role_arns=[],
            ),
        )

        # ── 3. Assessment trigger Lambda (run in CI) ──────────────────
        # Each assessment scores the app against the policy and recommends
        # changes. Run in CI/CD; track over time.
        # boto3.client("resiliencehub").start_app_assessment(...)
```

### 3.2 Assessment output

```bash
aws resiliencehub start-app-assessment \
  --app-arn $APP_ARN \
  --assessment-name "weekly-$(date +%Y-%m-%d)" \
  --client-token $(uuidgen)

# After ~5 min:
aws resiliencehub describe-app-assessment --assessment-arn $ASSESSMENT_ARN
# Output:
#   - resilienceScore: 78 (0-100)
#   - actualRPO/RTO per disruption vs target (PASS/FAIL per cell)
#   - Recommendations:
#     "Add cross-region replica for OrdersTable (current RPO 4h vs target 5min)"
#     "Add ECS service auto-scaling (current RTO 25 min vs target 5 min)"
```

---

## 4. FIS — chaos engineering

### 4.1 CDK — experiment template

```python
# stacks/fis_stack.py
from aws_cdk import aws_fis as fis
from aws_cdk import aws_iam as iam
from aws_cdk import aws_cloudwatch as cw

# IAM role FIS assumes
fis_role = iam.Role(self, "FisRole",
    assumed_by=iam.ServicePrincipal("fis.amazonaws.com"),
    inline_policies={
        "FisActions": iam.PolicyDocument(statements=[
            iam.PolicyStatement(
                actions=[
                    "ec2:StopInstances", "ec2:StartInstances", "ec2:RebootInstances",
                    "ecs:UpdateService", "ecs:StopTask",
                    "rds:RebootDBInstance", "rds:FailoverDBCluster",
                    "eks:DescribeCluster",
                    "ssm:SendCommand",
                ],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:ResourceTag/FisExperiment": "allowed"},
                },
            ),
        ]),
    },
)

# Stop condition — CloudWatch alarm
slo_alarm = cw.Alarm(self, "SloErrorAlarm",
    metric=cw.Metric(
        namespace="AWS/ApplicationELB",
        metric_name="HTTPCode_Target_5XX_Count",
        dimensions_map={"LoadBalancer": "app/prod-alb/abc"},
        statistic="Sum", period=Duration.minutes(1),
    ),
    threshold=100,
    evaluation_periods=2,
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    alarm_description="STOP FIS if SLO breached",
)

# Experiment template — kill 1 ECS task
exp_template = fis.CfnExperimentTemplate(self, "KillEcsTaskExperiment",
    description="Stop 1 random ECS task in prod-app service",
    role_arn=fis_role.role_arn,
    targets={
        "EcsTasks": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
            resource_type="aws:ecs:task",
            resource_tags={"FisExperiment": "allowed"},
            selection_mode="COUNT(1)",
            filters=[fis.CfnExperimentTemplate.ExperimentTemplateTargetFilterProperty(
                path="ServiceName", values=["prod-app"],
            )],
        ),
    },
    actions={
        "StopTask": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
            action_id="aws:ecs:stop-task",
            description="Stop one ECS task",
            targets={"Tasks": "EcsTasks"},
        ),
    },
    stop_conditions=[
        fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
            source="aws:cloudwatch:alarm",
            value=slo_alarm.alarm_arn,
        ),
    ],
    tags={"GameDay": "weekly"},
)

# Experiment template — region failure simulation (network partition)
region_partition = fis.CfnExperimentTemplate(self, "RegionPartitionExperiment",
    description="Simulate network partition between primary and DR region",
    role_arn=fis_role.role_arn,
    targets={
        "PrimaryVpc": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
            resource_type="aws:ec2:vpc",
            resource_arns=[primary_vpc_arn],
            selection_mode="ALL",
        ),
    },
    actions={
        "BlockEgress": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
            action_id="aws:network:disrupt-connectivity",
            description="Block all egress from primary VPC for 10 min",
            parameters={
                "duration": "PT10M",
                "scope": "all",
            },
            targets={"VPCs": "PrimaryVpc"},
        ),
    },
    stop_conditions=[
        fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
            source="aws:cloudwatch:alarm",
            value=availability_alarm.alarm_arn,
        ),
    ],
    experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
        account_targeting="single-account",
        empty_target_resolution_mode="fail",
    ),
)

# Experiment template — RDS failover (test Aurora Global promote)
rds_failover = fis.CfnExperimentTemplate(self, "RdsFailoverExperiment",
    description="Trigger Aurora cluster failover to test app reconnection",
    role_arn=fis_role.role_arn,
    targets={
        "AuroraCluster": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
            resource_type="aws:rds:cluster",
            resource_arns=[aurora_cluster_arn],
            selection_mode="ALL",
        ),
    },
    actions={
        "Failover": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
            action_id="aws:rds:failover-db-cluster",
            targets={"Clusters": "AuroraCluster"},
        ),
    },
    stop_conditions=[
        fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
            source="aws:cloudwatch:alarm",
            value=db_connection_alarm.alarm_arn,
        ),
    ],
)
```

### 4.2 Run experiments

```bash
# Manual run
aws fis start-experiment \
  --experiment-template-id $TEMPLATE_ID \
  --tags Purpose=GameDay,Date=$(date +%Y-%m-%d)

# Monitor
aws fis get-experiment --id $EXPERIMENT_ID
# State: pending → initiating → running → completed | stopped | failed

# Scheduled — EventBridge rule that triggers FIS experiment weekly
aws events put-rule \
  --name fis-weekly-chaos \
  --schedule-expression "cron(0 14 ? * THU *)"  # Thursdays 2pm
aws events put-targets ...   # FIS:StartExperiment via Lambda
```

### 4.3 Game day playbook

```
Hypothesis: "If we kill 30% of ECS tasks in prod-app, error rate stays < 1%
            and recovery completes within 60 sec via auto-scaling."

Experiment: 
  fis:ecs:stop-task with COUNT(30%) of prod-app tasks

Observation:
  - Error rate during experiment
  - Recovery time (when ECS service desiredCount reaches steady)
  - Customer impact (synthetic test results)

Result:
  PASS / FAIL based on hypothesis

Action items if FAIL:
  - Increase auto-scaling responsiveness
  - Add task circuit breaker
  - Tune ALB health check intervals
```

---

## 5. Production Variant — assessment in CI + auto-stop + multi-team game days

### 5.1 CI gate via Resilience Hub

```yaml
# .github/workflows/deploy.yml
- name: Resilience Hub Assessment
  run: |
    aws resiliencehub start-app-assessment \
      --app-arn ${{ secrets.RH_APP_ARN }} \
      --assessment-name ci-${{ github.sha }}
    # Wait for assessment...
    aws resiliencehub describe-app-assessment \
      --assessment-arn $ASSESSMENT_ARN \
      --query 'assessment.resilienceScore.score'
- name: Block merge if score drops
  run: |
    # Compare against baseline; fail if drops > 5 points
```

### 5.2 Quarterly executive game day

- Full regional failover via Route 53 ARC + FIS region partition
- Run in stage (or prod with reduced traffic via WAF)
- Multi-team observers: SRE, app team, security, customer support, business
- Document timing, decisions, blockers
- Improve runbook + tooling based on findings

---

## 6. Common gotchas

- **FIS experiment can damage production** — always require CW alarm stop conditions; tag-based target filtering (`FisExperiment: allowed`) in IAM policy.
- **`aws:network:disrupt-connectivity` blocks ALL VPC egress** — including AWS API calls. Lambdas can't reach STS / DDB / S3. Plan for this.
- **`aws:rds:failover-db-cluster` causes 30-60 sec downtime** in non-Multi-AZ setups. Validate target is multi-AZ before running.
- **Stop conditions are evaluated every 30 sec** — fast-failing experiments may breach SLO before stop fires. Add explicit `duration` in action params.
- **FIS pricing**: $0.10 per action-minute. 10-min experiment with 100 EC2 instances = $100. Plan budget.
- **Resilience Hub app import** uses CFN stack OR Resource Group. CDK apps have implicit CFN stacks that work.
- **Resilience Hub recommendations are best-effort** — they suggest patterns, not specific code. Engineering judgment required.
- **Assessment cost**: $0.000033/resource-hour assessed. 100 resources × 730h = $24/mo per app.
- **CI assessments add 5-10 min to deploy time** — only run on main branch / production deploys, not every PR.
- **EKS FIS actions require eksctl integration** for cluster auth. Configure once.
- **Game day rituals** — schedule, communicate, run, observe, retro. Skipping any step = no learning.
- **Don't FIS in production** without 30 days of practice in stage. Build muscle memory in safer environments.
- **Synthetic monitoring during experiments** = catches user-impacting issues even if internal alarms don't fire.

---

## 7. Pytest worked example

```python
# tests/test_resilience.py
import boto3, pytest, time

rh = boto3.client("resiliencehub")
fis = boto3.client("fis")


def test_resilience_score_above_threshold(app_arn):
    assessments = rh.list_app_assessments(appArn=app_arn)["assessmentSummaries"]
    assert assessments
    latest = assessments[0]
    detail = rh.describe_app_assessment(assessmentArn=latest["assessmentArn"])
    score = detail["assessment"]["resilienceScore"]["score"]
    assert score >= 80, f"Resilience score {score} below threshold 80"


def test_no_critical_recommendations(app_arn):
    assessments = rh.list_app_assessments(appArn=app_arn)["assessmentSummaries"]
    detail = rh.describe_app_assessment(assessmentArn=assessments[0]["assessmentArn"])
    recs = rh.list_app_assessment_recommendations(
        assessmentArn=assessments[0]["assessmentArn"],
    )["alarmRecommendations"]
    critical = [r for r in recs if r.get("severity") == "CRITICAL"]
    assert not critical, f"Critical recommendations: {[r['name'] for r in critical]}"


def test_fis_experiments_have_stop_conditions():
    templates = fis.list_experiment_templates()["experimentTemplates"]
    for t in templates:
        detail = fis.get_experiment_template(id=t["id"])
        stops = detail["experimentTemplate"].get("stopConditions", [])
        assert any(s.get("source") == "aws:cloudwatch:alarm" for s in stops), \
            f"Experiment {t['id']} has no CW alarm stop condition"


def test_fis_role_has_resource_tag_condition(fis_role_arn):
    """IAM safety: FIS role can only act on tagged resources."""
    iam = boto3.client("iam")
    role_name = fis_role_arn.split("/")[-1]
    policies = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    for p in policies:
        doc = iam.get_role_policy(RoleName=role_name, PolicyName=p)["PolicyDocument"]
        for stmt in doc["Statement"]:
            if "ec2:StopInstances" in (stmt.get("Action") or []):
                cond = stmt.get("Condition", {})
                assert cond.get("StringEquals", {}).get("aws:ResourceTag/FisExperiment") == "allowed", \
                    "FIS role lacks tag-based safety condition"


def test_recent_game_day_executed():
    """At least 1 FIS experiment completed in last 7 days."""
    experiments = fis.list_experiments(maxResults=20)["experiments"]
    from datetime import datetime, timezone, timedelta
    threshold = datetime.now(timezone.utc) - timedelta(days=7)
    recent = [e for e in experiments
              if e["state"]["status"] == "completed"
              and datetime.fromisoformat(e["startTime"]) > threshold]
    assert recent, "No FIS experiments run in last 7 days"
```

---

## 8. Five non-negotiables

1. **Every FIS experiment template has CloudWatch alarm stop conditions** — never run without safety net.
2. **FIS IAM role tag-scoped** — `aws:ResourceTag/FisExperiment: allowed` condition on EC2/ECS/RDS actions.
3. **Resilience Hub assessment in CI** — track score over time; block deploys that regress > 5 points.
4. **Quarterly game day** with multi-team observation + retrospective.
5. **Practice in stage 30+ days before running in prod** — chaos engineering muscle, not gambling.

---

## 9. References

- [AWS Resilience Hub User Guide](https://docs.aws.amazon.com/resilience-hub/latest/userguide/what-is.html)
- [AWS FIS — User Guide](https://docs.aws.amazon.com/fis/latest/userguide/what-is.html)
- [FIS Action library](https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html)
- [Chaos engineering principles](https://principlesofchaos.org/)
- [Game day playbook (AWS)](https://aws.amazon.com/builders-library/operational-excellence/)
- [Resilience Hub pricing](https://aws.amazon.com/resilience-hub/pricing/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Resilience Hub assessments + resiliency policies + FIS experiments + stop conditions + tag-scoped IAM + game day. Wave 14. |
