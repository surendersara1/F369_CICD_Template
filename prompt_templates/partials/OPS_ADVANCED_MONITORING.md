# SOP — Advanced Ops (Synthetics, Config, Backup, Cost Anomaly)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+)

---

## 1. Purpose

Production-grade ops primitives beyond the basic `LAYER_OBSERVABILITY`:

- **CloudWatch Synthetics** — scheduled API probes (golden-path canaries)
- **AWS Config** — resource compliance rules
- **AWS Backup** — centralized backup plans for RDS + DDB
- **Cost Anomaly Detection** — ML-based cost spike alerts
- **CloudTrail Lake** — queryable audit trail (365-day default)
- **Log archive** — long retention via Firehose → S3 Glacier

Include when SOW contains: SLA, compliance, backup retention, cost governance, audit trail.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack project | **§3 Monolith Variant** |
| Dedicated `RecoveryStack` + `GovernanceStack` + `CostStack` | **§4 Micro-Stack Variant** |

No cycle risk — these services observe but don't mutate workloads.

---

## 3. Monolith Variant

### 3.1 Synthetic canary — end-to-end health probe

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_synthetics as synth,
    aws_s3 as s3,
)


canary_artifacts = s3.Bucket(
    self, "CanaryArtifacts",
    bucket_name=f"{{project_name}}-canary-artifacts-{stage}",
    removal_policy=cdk.RemovalPolicy.DESTROY,
    auto_delete_objects=True,
    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
)

synth.Canary(
    self, "ApiHealthCanary",
    canary_name=f"{{project_name}}-api-health-{stage}",
    schedule=synth.Schedule.rate(Duration.minutes(5)),
    test=synth.Test.custom(
        code=synth.Code.from_asset("canaries/api_health"),
        handler="index.handler",
    ),
    runtime=synth.Runtime.SYNTHETICS_PYTHON_SELENIUM_4_1,
    artifacts_bucket_location=synth.ArtifactsBucketLocation(bucket=canary_artifacts),
    start_after_creation=True,
    success_retention_period=Duration.days(7),
    failure_retention_period=Duration.days(30),
)
```

### 3.2 AWS Config rules

```python
from aws_cdk import aws_config as config


# Enable Config recorder (once per account per region)
# Often pre-existing; CDK will reference existing via from_lookup if needed.

config.ManagedRule(
    self, "S3EncryptionRule",
    identifier=config.ManagedRuleIdentifiers.S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED,
    config_rule_name=f"{{project_name}}-s3-encryption",
)
config.ManagedRule(
    self, "RdsEncryptionRule",
    identifier=config.ManagedRuleIdentifiers.RDS_STORAGE_ENCRYPTED,
)
config.ManagedRule(
    self, "LambdaPublicAccessRule",
    identifier=config.ManagedRuleIdentifiers.LAMBDA_FUNCTION_PUBLIC_ACCESS_PROHIBITED,
)
```

### 3.3 AWS Backup plan

```python
from aws_cdk import aws_backup as backup


plan = backup.BackupPlan(
    self, "BackupPlan",
    backup_plan_name=f"{{project_name}}-backup-{stage}",
    backup_plan_rules=[
        backup.BackupPlanRule(
            rule_name="DailyKeep30",
            schedule_expression=events.Schedule.cron(hour="3", minute="0"),
            delete_after=Duration.days(30),
            start_window=Duration.hours(1),
            completion_window=Duration.hours(4),
        ),
    ],
)
plan.add_selection(
    "Selection",
    resources=[
        backup.BackupResource.from_rds_database_instance(self.rds_instance),
        backup.BackupResource.from_dynamo_db_table(self.ddb_tables["jobs_ledger"]),
    ],
)
```

### 3.4 Cost Anomaly Detection

```python
from aws_cdk import aws_ce as ce


monitor = ce.CfnAnomalyMonitor(
    self, "CostAnomalyMonitor",
    monitor_name=f"{{project_name}}-cost-monitor-{stage}",
    monitor_type="DIMENSIONAL",
    monitor_dimension="SERVICE",
)
ce.CfnAnomalySubscription(
    self, "CostAnomalySubscription",
    subscription_name=f"{{project_name}}-cost-alerts-{stage}",
    monitor_arn_list=[monitor.attr_monitor_arn],
    subscribers=[
        ce.CfnAnomalySubscription.SubscriberProperty(
            type="EMAIL", address="{owner_email}",
        ),
    ],
    threshold=100,   # USD
    frequency="DAILY",
)
```

### 3.5 Monolith gotchas

- **`synth.Code.from_asset(path)`** is CWD-relative; anchor to `__file__` if you run synth from CI.
- **AWS Config recorder** is one-per-account-per-region; deploying a second in the same region fails. In micro-stack, put Config rules in a dedicated stack.
- **Backup vault** encryption defaults to AWS-managed KMS; override with `encryption_key=` for CMK if compliance requires.

---

## 4. Micro-Stack Variant

Split the services by stack concern:

- `GovernanceStack` — Config rules, Security Hub, GuardDuty
- `RecoveryStack` — Backup plans, PITR enablement, cross-region replication
- `CostStack` — Budgets, Cost Anomaly, Savings Plans tracking
- `SyntheticsStack` (optional) — CW Synthetics canaries

All four read upstream ARNs (no mutation). Same code as §3, just factored.

### 4.1 `GovernanceStack` skeleton

```python
import aws_cdk as cdk
from aws_cdk import aws_config as config
from constructs import Construct


class GovernanceStack(cdk.Stack):
    def __init__(self, scope: Construct, **kwargs) -> None:
        super().__init__(scope, "{project_name}-governance", **kwargs)

        for rule_id, mrid in [
            ("S3Encryption",       config.ManagedRuleIdentifiers.S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED),
            ("RdsEncryption",      config.ManagedRuleIdentifiers.RDS_STORAGE_ENCRYPTED),
            ("LambdaPublicAccess", config.ManagedRuleIdentifiers.LAMBDA_FUNCTION_PUBLIC_ACCESS_PROHIBITED),
            ("CloudTrailEnabled",  config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENABLED),
        ]:
            config.ManagedRule(self, rule_id, identifier=mrid)
```

### 4.2 `RecoveryStack` skeleton

```python
import aws_cdk as cdk
from aws_cdk import aws_backup as backup, aws_rds as rds, aws_dynamodb as ddb
from aws_cdk.aws_events import Schedule
from aws_cdk import Duration
from constructs import Construct


class RecoveryStack(cdk.Stack):
    def __init__(
        self, scope: Construct,
        rds_instance: rds.IDatabaseInstance,
        jobs_ledger: ddb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-recovery", **kwargs)

        plan = backup.BackupPlan(self, "Plan",
            backup_plan_rules=[backup.BackupPlanRule(
                rule_name="DailyKeep30",
                schedule_expression=Schedule.cron(hour="3", minute="0"),
                delete_after=Duration.days(30),
            )],
        )
        plan.add_selection("Selection",
            resources=[
                backup.BackupResource.from_rds_database_instance(rds_instance),
                backup.BackupResource.from_dynamo_db_table(jobs_ledger),
            ],
        )
```

### 4.3 Micro-stack gotchas

- **Config recorder is global-per-region** — if another team's stack already deploys it, reference via `from_lookup` or accept a circular failure on `cdk deploy` and manually resolve.
- **Backup IAM role** — CDK creates one automatically; add the `AWSBackupServiceRolePolicyForBackup` managed policy ref if customizing.
- **Cost Anomaly** L1 CfnAnomalyMonitor is the only option; no L2.

---

## 5. Worked example

```python
def test_recovery_stack_backs_up_rds_and_ddb():
    # ... instantiate RecoveryStack ...
    t = Template.from_stack(rec)
    t.resource_count_is("AWS::Backup::BackupPlan", 1)
    t.resource_count_is("AWS::Backup::BackupSelection", 1)
```

---

## 6. References

- `docs/Feature_Roadmap.md` — OBS-20 (canary), GOV-01..GOV-11, REC-01..REC-16, COST-01..COST-12
- Related SOPs: `LAYER_OBSERVABILITY` (baseline), `SECURITY_WAF_SHIELD_MACIE` (threat detection)

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Split into Governance/Recovery/Cost/Synthetics stacks for micro-stack topologies. |
| 1.0 | 2026-03-05 | Initial. |
