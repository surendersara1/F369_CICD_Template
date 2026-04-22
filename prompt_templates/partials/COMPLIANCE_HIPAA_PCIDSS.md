# SOP — Compliance Blueprints (HIPAA, PCI DSS, SOC 2)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · S3 Object Lock (WORM) · CloudTrail (multi-region + file validation) · AWS Config (managed rules) · AWS Backup (vault lock) · Inspector v2 · Security Hub · Python 3.13 evidence Lambda

---

## 1. Purpose

- Provision the **compliance control plane** common to HIPAA, PCI DSS, and SOC 2:
  - **Immutable audit trail** (CloudTrail → S3 Object Lock + file-integrity validation, CloudWatch Logs with 10-year retention for HIPAA).
  - **Continuous Config rules** (≥15 managed rules covering encryption, MFA, public-access, CloudTrail/VPC-flow-logs, backups).
  - **AWS Backup with Vault Lock** (immutable backups; 7-year max retention; daily + monthly compliance rules).
  - **Inspector v2** enabled account-wide via `AwsCustomResource`.
  - **Evidence collector Lambda** (weekly; writes Config summary + Security Hub findings to the audit bucket for auditor review).
- Codify the **compliance matrix** (what each standard requires, which AWS primitive satisfies it).
- Provide per-standard retention defaults (HIPAA 6y, PCI 1y, SOC 2 3y) and HIPAA BAA-eligible service gating.
- Include when the SOW mentions HIPAA, PCI DSS, SOC 2 Type II, GDPR, FedRAMP, BAA, PHI, cardholder data, or regulated workloads.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns the audit bucket + CloudTrail + Config rules + Backup vault + Inspector + evidence Lambda + KMS | **§3 Monolith Variant** |
| Dedicated `ComplianceStack` publishes audit bucket name + backup vault ARN + KMS ARN via SSM; other stacks consume and send their data to those resources identity-side | **§4 Micro-Stack Variant** |

**Why the split matters.** Every other stack (application Lambdas, databases, ML endpoints) needs `s3:PutObject` on the audit bucket's log prefix, `kms:Encrypt` on the compliance CMK, and `backup:PutBackupVaultAccessPolicy` notifications on the vault. In monolith those are local L2 grants. Across stacks they are bucket-policy / key-policy edits in `ComplianceStack` triggered by consumer stacks → circular. Micro-Stack publishes names/ARNs via SSM; consumers grant identity-side on their own roles.

> Per prompt §3.1 this partial is primarily policy + audit config, but the CDK footprint (Object-Lock bucket, Vault-Locked backup, custom-resource Inspector enabler, evidence Lambda) is substantial enough that the cross-stack pattern is a real concern. Dual-variant is retained.

---

## 3. Monolith Variant

**Use when:** one CDK stack owns the full compliance plane for a single account / environment.

### 3.1 Compliance matrix

| Requirement            | HIPAA       | PCI DSS     | SOC 2             | Implementation                |
| ---------------------- | ----------- | ----------- | ----------------- | ----------------------------- |
| Encryption at rest     | ✅ Required | ✅ Required | ✅ Required       | KMS CMK on ALL resources      |
| Encryption in transit  | ✅ Required | ✅ Required | ✅ Required       | TLS 1.2+ enforced everywhere  |
| Access logging         | ✅ Required | ✅ Required | ✅ Required       | CloudTrail + S3 access logs   |
| MFA                    | ✅ Required | ✅ Required | ✅ Best practice  | Cognito MFA enforced          |
| Network segmentation   | ✅ Required | ✅ Required | ✅ Required       | VPC + private subnets         |
| Vulnerability scanning | ✅ Required | ✅ Required | ✅ Required       | Inspector v2 auto-scan        |
| Backup & retention     | ✅ 6 years  | ✅ 1 year   | ✅ Required       | AWS Backup + Vault Lock       |
| Intrusion detection    | ✅ Required | ✅ Required | ✅ Required       | GuardDuty + Security Hub      |
| Incident response plan | ✅ Required | ✅ Required | ✅ Required       | CloudWatch alarms → PagerDuty |
| Audit trails immutable | ✅ Required | ✅ Required | ✅ Required       | CloudTrail + S3 Object Lock   |

### 3.2 CDK — compliance blueprint (single stack method)

```python
from pathlib import Path
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CustomResource, CfnOutput,
    aws_backup as backup,
    aws_cloudtrail as cloudtrail,
    aws_config as config,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    custom_resources as cr,
)


def _create_compliance_controls(self, stage_name: str, compliance_standard: str = "HIPAA") -> None:
    """Assumes self.{kms_key, alert_topic} set earlier.

    compliance_standard ∈ {"HIPAA", "PCI_DSS", "SOC2", "ALL"}.
    """
    IS_HIPAA = compliance_standard in ("HIPAA",   "ALL")
    IS_PCI   = compliance_standard in ("PCI_DSS", "ALL")
    IS_SOC2  = compliance_standard in ("SOC2",    "ALL")

    # --- A) Immutable audit trail (WORM bucket + CloudTrail) -----------------
    audit_bucket = s3.Bucket(
        self, "ComplianceAuditBucket",
        bucket_name=f"{{project_name}}-compliance-audit-{stage_name}-{Aws.ACCOUNT_ID}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        object_lock_enabled=True,
        object_lock_default_retention=s3.ObjectLockRetention.governance(
            duration=Duration.days(365 * (6 if IS_HIPAA else 1 if IS_PCI else 3)),
        ),
        lifecycle_rules=[s3.LifecycleRule(
            id="archive-old-logs",
            enabled=True,
            transitions=[
                s3.Transition(storage_class=s3.StorageClass.GLACIER,
                              transition_after=Duration.days(90)),
                s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                              transition_after=Duration.days(365)),
            ],
        )],
        removal_policy=RemovalPolicy.RETAIN,
    )
    self.audit_bucket = audit_bucket

    cloudtrail_log_group = logs.LogGroup(
        self, "CloudTrailLogGroup",
        log_group_name=f"/aws/cloudtrail/{{project_name}}-{stage_name}",
        retention=logs.RetentionDays.TEN_YEARS if IS_HIPAA else logs.RetentionDays.ONE_YEAR,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.RETAIN,
    )
    cloudtrail.Trail(
        self, "ComplianceCloudTrail",
        trail_name=f"{{project_name}}-compliance-trail-{stage_name}",
        bucket=audit_bucket,
        s3_key_prefix="cloudtrail",
        encryption_key=self.kms_key,
        include_global_service_events=True,
        is_multi_region_trail=True,
        enable_file_validation=True,
        send_to_cloud_watch_logs=True,
        cloud_watch_log_group=cloudtrail_log_group,
        management_events=cloudtrail.ReadWriteType.ALL,
    )

    # --- B) AWS Config rules -------------------------------------------------
    COMPLIANCE_RULES = [
        ("encrypted-volumes",      config.ManagedRuleIdentifiers.EC2_EBS_ENCRYPTION_BY_DEFAULT),
        ("s3-bucket-encrypted",    config.ManagedRuleIdentifiers.S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED),
        ("rds-storage-encrypted",  config.ManagedRuleIdentifiers.RDS_STORAGE_ENCRYPTED),
        ("cloudtrail-encryption",  config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENCRYPTION_ENABLED),
        ("mfa-console",            config.ManagedRuleIdentifiers.MFA_ENABLED_FOR_IAM_CONSOLE_ACCESS),
        ("root-mfa",               config.ManagedRuleIdentifiers.ROOT_ACCOUNT_MFA_ENABLED),
        ("no-public-s3-buckets",   config.ManagedRuleIdentifiers.S3_BUCKET_PUBLIC_READ_PROHIBITED),
        ("no-public-rds",          config.ManagedRuleIdentifiers.RDS_INSTANCE_PUBLIC_ACCESS_CHECK),
        ("iam-no-inline-policies", config.ManagedRuleIdentifiers.IAM_NO_INLINE_POLICY_CHECK),
        ("cloudtrail-enabled",     config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENABLED),
        ("vpc-flow-logs-enabled",  config.ManagedRuleIdentifiers.VPC_FLOW_LOGS_ENABLED),
        ("s3-access-logs-enabled", config.ManagedRuleIdentifiers.S3_BUCKET_LOGGING_ENABLED),
        ("no-unrestricted-ssh",    config.ManagedRuleIdentifiers.INCOMING_SSH_DISABLED),
        ("dynamo-pitr",            config.ManagedRuleIdentifiers.DYNAMODB_PITR_ENABLED),
        ("rds-multi-az",           config.ManagedRuleIdentifiers.RDS_MULTI_AZ_SUPPORT),
    ]
    for name, ident in COMPLIANCE_RULES:
        config.ManagedRule(
            self, f"ConfigRule{name.replace('-', '').title()}",
            config_rule_name=f"{{project_name}}-{name}-{stage_name}",
            identifier=ident,
        )

    # --- C) AWS Backup with Vault Lock --------------------------------------
    backup_vault = backup.BackupVault(
        self, "ComplianceBackupVault",
        backup_vault_name=f"{{project_name}}-compliance-vault-{stage_name}",
        encryption_key=self.kms_key,
        lock_configuration=backup.LockConfiguration(
            min_retention=Duration.days(7),
            max_retention=Duration.days(365 * 7),
            changeable_for=Duration.days(3),       # 3-day grace period then LOCKED FOREVER
        ),
        removal_policy=RemovalPolicy.RETAIN,
        notification_events=[
            backup.BackupVaultEvents.BACKUP_JOB_FAILED,
            backup.BackupVaultEvents.RESTORE_JOB_FAILED,
            backup.BackupVaultEvents.COPY_JOB_FAILED,
        ],
        notification_topic=self.alert_topic,
    )
    self.backup_vault = backup_vault

    compliance_plan = backup.BackupPlan(
        self, "ComplianceBackupPlan",
        backup_plan_name=f"{{project_name}}-compliance-{stage_name}",
        backup_vault=backup_vault,
        backup_plan_rules=[
            backup.BackupPlanRule(
                rule_name="DailyBackup",
                schedule_expression=events.Schedule.cron(hour="2", minute="0"),
                delete_after=Duration.days(35),
                move_to_cold_storage_after=Duration.days(7),
                completion_window=Duration.hours(4),
                start_window=Duration.hours(1),
                recovery_point_tags={"Type": "Daily", "Project": "{project_name}"},
            ),
            backup.BackupPlanRule(
                rule_name="MonthlyComplianceBackup",
                schedule_expression=events.Schedule.cron(day="1", hour="3", minute="0"),
                delete_after=Duration.days(365 * 7),
                move_to_cold_storage_after=Duration.days(90),
                completion_window=Duration.hours(8),
                recovery_point_tags={"Type": "Compliance", "Retention": "7Years"},
            ),
        ],
    )
    compliance_plan.add_selection(
        "AllTaggedResources",
        resources=[backup.BackupResource.from_tag("Project", "{project_name}")],
        allow_restores=True,
    )

    # --- D) Inspector v2 account enable via AwsCustomResource ---------------
    cr.AwsCustomResource(
        self, "InspectorV2Enable",
        on_create=cr.AwsSdkCall(
            service="inspector2",
            action="enable",
            parameters={"resourceTypes": ["EC2", "ECR", "LAMBDA"]},
            physical_resource_id=cr.PhysicalResourceId.of(f"{{project_name}}-inspector-{stage_name}"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_statements([
            iam.PolicyStatement(
                actions=["inspector2:Enable", "inspector2:BatchGetAccountStatus"],
                resources=["*"],
            ),
        ]),
    )

    # --- E) Evidence collector Lambda ---------------------------------------
    evidence_fn = _lambda.Function(
        self, "ComplianceEvidenceFn",
        function_name=f"{{project_name}}-compliance-evidence-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/compliance_evidence"),
        environment={
            "AUDIT_BUCKET":        audit_bucket.bucket_name,
            "COMPLIANCE_STANDARD": compliance_standard,
        },
        timeout=Duration.minutes(5),
    )
    audit_bucket.grant_read_write(evidence_fn)
    evidence_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "config:GetComplianceSummaryByConfigRule",
            "securityhub:GetFindings",
            "securityhub:GetFindingAggregator",
            "sts:GetCallerIdentity",
        ],
        resources=["*"],           # these list-level APIs require "*"
    ))
    events.Rule(
        self, "WeeklyEvidenceCollection",
        rule_name=f"{{project_name}}-compliance-evidence-{stage_name}",
        schedule=events.Schedule.cron(hour="6", minute="0", week_day="MON"),
        targets=[targets.LambdaFunction(evidence_fn)],
    )

    CfnOutput(self, "ComplianceAuditBucket",   value=audit_bucket.bucket_name)
    CfnOutput(self, "ComplianceBackupVaultArn", value=backup_vault.backup_vault_arn)
    CfnOutput(self, "EvidenceCollectorArn",    value=evidence_fn.function_arn)
```

### 3.3 Evidence-collector handler (`lambda/compliance_evidence/index.py`)

```python
"""Weekly evidence collection — Config + Security Hub summaries → audit bucket."""
import boto3, json, logging, os
from datetime import datetime, timezone

logger = logging.getLogger(); logger.setLevel(logging.INFO)

config_client = boto3.client('config')
securityhub   = boto3.client('securityhub')
s3            = boto3.client('s3')

AUDIT_BUCKET   = os.environ['AUDIT_BUCKET']
COMPLIANCE_STD = os.environ['COMPLIANCE_STANDARD']


def handler(event, context):
    evidence = {
        'timestamp':            datetime.now(timezone.utc).isoformat(),
        'compliance_standard':  COMPLIANCE_STD,
        'account':              boto3.client('sts').get_caller_identity()['Account'],
        'region':               os.environ['AWS_DEFAULT_REGION'],
    }
    cfg = config_client.get_compliance_summary_by_config_rule()
    evidence['config_compliance'] = {
        'compliant_resources':     cfg['ComplianceSummary']['CompliantResourceCount']['CappedCount'],
        'non_compliant_resources': cfg['ComplianceSummary']['NonCompliantResourceCount']['CappedCount'],
    }
    sh = securityhub.get_findings(
        Filters={'RecordState': [{'Value': 'ACTIVE', 'Comparison': 'EQUALS'}]},
        MaxResults=1,
    )
    evidence['security_hub_active_findings'] = sh.get('Total', 'N/A')

    key = f"compliance-evidence/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/evidence.json"
    s3.put_object(
        Bucket=AUDIT_BUCKET, Key=key,
        Body=json.dumps(evidence, indent=2),
        ContentType='application/json',
    )
    logger.info("Evidence collected: s3://%s/%s", AUDIT_BUCKET, key)
    return {"evidence_location": f"s3://{AUDIT_BUCKET}/{key}"}
```

### 3.4 Retention-period cheat sheet

| Standard | Audit logs | Backups | Evidence |
|---|---|---|---|
| HIPAA    | 10 years (HITECH §164.530(j)) | 6 years of immutable backups | Quarterly attestation, annual SOC 2 / external audit |
| PCI DSS  | 1 year online, 3 months immediate | 1 year retention | Annual ROC, quarterly scans |
| SOC 2    | 3 years (Trust Services Criteria CC6.1) | Defined by RPO, minimum 1 year | Annual Type II attestation |

### 3.5 Gotchas

- **`object_lock_enabled=True` is immutable** — cannot be flipped off after creation. Testing in dev requires a separate bucket; never reuse a compliance bucket name.
- **Governance vs Compliance mode** — `ObjectLockRetention.governance()` lets a root-role with `s3:BypassGovernanceRetention` override; `compliance()` blocks even root. Start with governance; switch to compliance only after operational confidence.
- **Vault Lock `changeable_for=3 days`** — after the grace period, the lock config is PERMANENT. Even AWS Support cannot lift it. Triple-check `max_retention` before the grace period expires.
- **Inspector v2** charges per asset scanned. Enabling for all three resource types (`EC2`, `ECR`, `LAMBDA`) at account level may surprise-bill in a large estate. Enable granularly if cost is a concern.
- **Config rules default to evaluating all supported resources.** If your account has many unrelated resources, violations pile up. Scope with `rule_scope=` per rule.
- **CloudTrail `include_global_service_events=True`** duplicates global events across multi-region trails. Keep ONE multi-region trail with this flag; any other trails should set it to False.
- **`backup.BackupResource.from_tag("Project", "{project_name}")`** requires every resource to be tagged. Enforce via `aws_resourcegroupstaggingapi` + a Lambda + EventBridge rule that re-tags drift.

---

## 4. Micro-Stack Variant

**Use when:** `ComplianceStack` is dedicated; consumer stacks send logs / backups there.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `audit_bucket.grant_write(consumer_role)`** or `backup_vault.grant(...)` across stacks. Consumer roles grant identity-side `s3:PutObject` scoped to the audit-bucket prefix and `backup:StartBackupJob` scoped to the vault ARN.
3. **Never target cross-stack queues** — not relevant here.
4. **Never split a bucket and CloudFront OAC** — the audit bucket is private; no CDN.
5. **Never set `encryption_key=ext_key`** — the compliance CMK is **owned by `ComplianceStack`** (local reference), and consumers grant identity-side `kms:Encrypt`/`GenerateDataKey` on its ARN string.

### 4.2 `ComplianceStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_backup as backup,
    aws_cloudtrail as cloudtrail,
    aws_config as config,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class ComplianceStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        compliance_standard: str,           # "HIPAA" | "PCI_DSS" | "SOC2" | "ALL"
        alert_topic_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-compliance-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk", "Compliance": compliance_standard}.items():
            cdk.Tags.of(self).add(k, v)

        IS_HIPAA = compliance_standard in ("HIPAA",   "ALL")
        IS_PCI   = compliance_standard in ("PCI_DSS", "ALL")

        alert_topic = sns.Topic.from_topic_arn(self, "AlertTopic",
            ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm),
        )

        # Local CMK owned by this stack (honors 5th non-negotiable)
        cmk = kms.Key(self, "ComplianceKey",
            alias=f"alias/{{project_name}}-compliance-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- WORM audit bucket ----------------------------------------------
        audit_bucket = s3.Bucket(self, "AuditBucket",
            bucket_name=f"{{project_name}}-compliance-audit-{stage_name}-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            versioned=True,
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.governance(
                duration=Duration.days(365 * (6 if IS_HIPAA else 1 if IS_PCI else 3)),
            ),
            lifecycle_rules=[s3.LifecycleRule(
                id="archive-old-logs",
                enabled=True,
                transitions=[
                    s3.Transition(storage_class=s3.StorageClass.GLACIER,
                                  transition_after=Duration.days(90)),
                    s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                                  transition_after=Duration.days(365)),
                ],
            )],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # CloudTrail
        ct_log_group = logs.LogGroup(self, "CloudTrailLogGroup",
            log_group_name=f"/aws/cloudtrail/{{project_name}}-{stage_name}",
            retention=logs.RetentionDays.TEN_YEARS if IS_HIPAA else logs.RetentionDays.ONE_YEAR,
            encryption_key=cmk,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cloudtrail.Trail(self, "ComplianceCloudTrail",
            trail_name=f"{{project_name}}-compliance-trail-{stage_name}",
            bucket=audit_bucket,
            s3_key_prefix="cloudtrail",
            encryption_key=cmk,
            include_global_service_events=True,
            is_multi_region_trail=True,
            enable_file_validation=True,
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=ct_log_group,
            management_events=cloudtrail.ReadWriteType.ALL,
        )

        # Config managed rules (same set as §3.2)
        for name, ident in [
            ("encrypted-volumes",      config.ManagedRuleIdentifiers.EC2_EBS_ENCRYPTION_BY_DEFAULT),
            ("s3-bucket-encrypted",    config.ManagedRuleIdentifiers.S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED),
            ("rds-storage-encrypted",  config.ManagedRuleIdentifiers.RDS_STORAGE_ENCRYPTED),
            ("cloudtrail-encryption",  config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENCRYPTION_ENABLED),
            ("mfa-console",            config.ManagedRuleIdentifiers.MFA_ENABLED_FOR_IAM_CONSOLE_ACCESS),
            ("root-mfa",               config.ManagedRuleIdentifiers.ROOT_ACCOUNT_MFA_ENABLED),
            ("no-public-s3-buckets",   config.ManagedRuleIdentifiers.S3_BUCKET_PUBLIC_READ_PROHIBITED),
            ("no-public-rds",          config.ManagedRuleIdentifiers.RDS_INSTANCE_PUBLIC_ACCESS_CHECK),
            ("iam-no-inline-policies", config.ManagedRuleIdentifiers.IAM_NO_INLINE_POLICY_CHECK),
            ("cloudtrail-enabled",     config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENABLED),
            ("vpc-flow-logs-enabled",  config.ManagedRuleIdentifiers.VPC_FLOW_LOGS_ENABLED),
            ("s3-access-logs-enabled", config.ManagedRuleIdentifiers.S3_BUCKET_LOGGING_ENABLED),
            ("no-unrestricted-ssh",    config.ManagedRuleIdentifiers.INCOMING_SSH_DISABLED),
            ("dynamo-pitr",            config.ManagedRuleIdentifiers.DYNAMODB_PITR_ENABLED),
            ("rds-multi-az",           config.ManagedRuleIdentifiers.RDS_MULTI_AZ_SUPPORT),
        ]:
            config.ManagedRule(self, f"ConfigRule{name.replace('-', '').title()}",
                config_rule_name=f"{{project_name}}-{name}-{stage_name}",
                identifier=ident,
            )

        # Backup vault + plan (local CMK; SNS from ITopic)
        backup_vault = backup.BackupVault(self, "BackupVault",
            backup_vault_name=f"{{project_name}}-compliance-vault-{stage_name}",
            encryption_key=cmk,
            lock_configuration=backup.LockConfiguration(
                min_retention=Duration.days(7),
                max_retention=Duration.days(365 * 7),
                changeable_for=Duration.days(3),
            ),
            removal_policy=RemovalPolicy.RETAIN,
            notification_events=[
                backup.BackupVaultEvents.BACKUP_JOB_FAILED,
                backup.BackupVaultEvents.RESTORE_JOB_FAILED,
                backup.BackupVaultEvents.COPY_JOB_FAILED,
            ],
            notification_topic=alert_topic,
        )
        plan = backup.BackupPlan(self, "BackupPlan",
            backup_plan_name=f"{{project_name}}-compliance-{stage_name}",
            backup_vault=backup_vault,
            backup_plan_rules=[
                backup.BackupPlanRule(
                    rule_name="DailyBackup",
                    schedule_expression=events.Schedule.cron(hour="2", minute="0"),
                    delete_after=Duration.days(35),
                    move_to_cold_storage_after=Duration.days(7),
                    completion_window=Duration.hours(4),
                    start_window=Duration.hours(1),
                    recovery_point_tags={"Type": "Daily", "Project": "{project_name}"},
                ),
                backup.BackupPlanRule(
                    rule_name="MonthlyComplianceBackup",
                    schedule_expression=events.Schedule.cron(day="1", hour="3", minute="0"),
                    delete_after=Duration.days(365 * 7),
                    move_to_cold_storage_after=Duration.days(90),
                    completion_window=Duration.hours(8),
                    recovery_point_tags={"Type": "Compliance", "Retention": "7Years"},
                ),
            ],
        )
        plan.add_selection("AllTaggedResources",
            resources=[backup.BackupResource.from_tag("Project", "{project_name}")],
            allow_restores=True,
        )

        # Inspector v2 enable
        cr.AwsCustomResource(self, "InspectorV2Enable",
            on_create=cr.AwsSdkCall(
                service="inspector2",
                action="enable",
                parameters={"resourceTypes": ["EC2", "ECR", "LAMBDA"]},
                physical_resource_id=cr.PhysicalResourceId.of(f"{{project_name}}-inspector-{stage_name}"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["inspector2:Enable", "inspector2:BatchGetAccountStatus"],
                    resources=["*"],
                ),
            ]),
        )

        # Evidence collector
        evidence_log = logs.LogGroup(self, "EvidenceLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-compliance-evidence-{stage_name}",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        evidence_fn = _lambda.Function(self, "EvidenceFn",
            function_name=f"{{project_name}}-compliance-evidence-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "compliance_evidence")),
            timeout=Duration.minutes(5),
            log_group=evidence_log,
            environment={
                "AUDIT_BUCKET":        audit_bucket.bucket_name,
                "COMPLIANCE_STANDARD": compliance_standard,
            },
        )
        audit_bucket.grant_read_write(evidence_fn)      # same-stack L2 safe
        evidence_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "config:GetComplianceSummaryByConfigRule",
                "securityhub:GetFindings",
                "securityhub:GetFindingAggregator",
                "sts:GetCallerIdentity",
            ],
            resources=["*"],
        ))
        iam.PermissionsBoundary.of(evidence_fn.role).apply(permission_boundary)

        events.Rule(self, "WeeklyEvidenceCollection",
            rule_name=f"{{project_name}}-compliance-evidence-{stage_name}",
            schedule=events.Schedule.cron(hour="6", minute="0", week_day="MON"),
            targets=[targets.LambdaFunction(evidence_fn)],
        )

        # Publish consumer-facing names/ARNs
        ssm.StringParameter(self, "AuditBucketParam",
            parameter_name=f"/{{project_name}}/compliance/audit_bucket",
            string_value=audit_bucket.bucket_name,
        )
        ssm.StringParameter(self, "ComplianceKmsArnParam",
            parameter_name=f"/{{project_name}}/compliance/kms_key_arn",
            string_value=cmk.key_arn,
        )
        ssm.StringParameter(self, "BackupVaultArnParam",
            parameter_name=f"/{{project_name}}/compliance/backup_vault_arn",
            string_value=backup_vault.backup_vault_arn,
        )
        CfnOutput(self, "AuditBucket",        value=audit_bucket.bucket_name)
        CfnOutput(self, "BackupVaultArn",     value=backup_vault.backup_vault_arn)
```

### 4.3 Consumer stacks — identity-side grants

```python
# inside any downstream stack (application, ML, data)
audit_bucket_name = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/compliance/audit_bucket",
)
compliance_kms_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/compliance/kms_key_arn",
)
backup_vault_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/compliance/backup_vault_arn",
)

# Example: grant a consumer role write to its own log prefix in the audit bucket
consumer_role.add_to_policy(iam.PolicyStatement(
    actions=["s3:PutObject"],
    resources=[f"arn:aws:s3:::{audit_bucket_name}/app-logs/*"],
))
consumer_role.add_to_policy(iam.PolicyStatement(
    actions=["kms:Encrypt", "kms:GenerateDataKey", "kms:Decrypt"],
    resources=[compliance_kms_arn],
))
# Start a backup job in the compliance vault
consumer_role.add_to_policy(iam.PolicyStatement(
    actions=["backup:StartBackupJob"],
    resources=[backup_vault_arn],
))
```

### 4.4 Micro-stack gotchas

- **`object_lock_enabled=True` requires `versioned=True`.** Leaving versioning off silently strips the lock. CDK warns but doesn't fail synth.
- **`AwsCustomResource` for Inspector enable** runs at deploy time. Subsequent deploys see the resource as already-enabled and skip; `BatchGetAccountStatus` is allowed so you can detect already-enabled state if you add an `on_update` handler.
- **SNS topic via `Topic.from_topic_arn`** — the notification is configured at vault creation; if you later rotate the topic ARN, you must recreate the vault (which is locked). Pick a stable topic.
- **Backup Vault Lock min/max** — AWS enforces `min_retention ≤ max_retention`. Setting `min_retention > max_retention` silently fails with an unclear error at deploy time.
- **Config rules in consumer accounts** — if this compliance plane is for a single account, Config rules here cover it. For multi-account (Organizations), move Config rules to an Organizational Conformance Pack in the management account.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, single account, one stack | §3 Monolith |
| Production MSxx layout, consumer stacks in other accounts or pipelines | §4 Micro-Stack |
| Multi-account compliance | Organizational Conformance Packs + AWS Control Tower; ComplianceStack still deploys per-account audit bucket |
| Cost-constrained | Skip Inspector v2 on `LAMBDA` resource type; keep EC2 + ECR |
| FedRAMP | Add AWS Audit Manager with the FedRAMP Moderate framework; consume ComplianceStack outputs as evidence sources |
| Immutable compliance mode (no root override) | Change `ObjectLockRetention.governance()` → `ObjectLockRetention.compliance()` (one-way) |
| Add SOC 2 Type II | Extend evidence-collector to aggregate quarterly reports; keep weekly cadence |

---

## 6. Worked example — ComplianceStack synthesizes

Save as `tests/sop/test_COMPLIANCE_HIPAA_PCIDSS.py`. Offline.

```python
"""SOP verification — ComplianceStack produces CMK + audit bucket + trail + rules + vault + evidence Lambda."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam, aws_sns as sns, aws_ssm as ssm
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_compliance_stack_hipaa():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    topic = sns.Topic(deps, "Alerts")
    # Parent stack publishes alert topic ARN
    ssm.StringParameter(deps, "AlertTopicArn",
        parameter_name="/test/obs/alert_topic_arn",
        string_value=topic.topic_arn,
    )
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.compliance_stack import ComplianceStack
    stack = ComplianceStack(
        app, stage_name="prod",
        compliance_standard="HIPAA",
        alert_topic_arn_ssm="/test/obs/alert_topic_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::KMS::Key",                   1)
    t.resource_count_is("AWS::S3::Bucket",                 1)    # audit bucket only
    t.resource_count_is("AWS::CloudTrail::Trail",          1)
    t.resource_count_is("AWS::Config::ConfigRule",         15)
    t.resource_count_is("AWS::Backup::BackupVault",        1)
    t.resource_count_is("AWS::Backup::BackupPlan",         1)
    t.resource_count_is("AWS::Lambda::Function",           2)    # evidence + inspector custom resource
    t.resource_count_is("AWS::SSM::Parameter",             3)    # audit bucket + kms arn + vault arn
```

---

## 7. References

- `docs/template_params.md` — `COMPLIANCE_STANDARD`, `AUDIT_BUCKET_SSM`, `COMPLIANCE_KMS_KEY_ARN_SSM`, `BACKUP_VAULT_ARN_SSM`, `COMPLIANCE_RETENTION_YEARS`
- `docs/Feature_Roadmap.md` — feature IDs `GOV-01..GOV-11` (compliance), `SEC-10` (audit trail), `SEC-11` (backup vault)
- AWS HIPAA eligible services: https://aws.amazon.com/compliance/hipaa-eligible-services-reference/
- AWS PCI DSS services: https://aws.amazon.com/compliance/pci-dss-level-1-faqs/
- AWS SOC compliance: https://aws.amazon.com/compliance/soc-faqs/
- Config managed rules: https://docs.aws.amazon.com/config/latest/developerguide/managed-rules-by-aws-config.html
- Related SOPs: `LAYER_SECURITY` (KMS, permission boundary), `LAYER_OBSERVABILITY` (CloudWatch plumbing), `OPS_ADVANCED_MONITORING` (GuardDuty, Security Hub — mentioned in the matrix), `AGENTCORE_AGENT_CONTROL` (Cedar policy + guardrails for regulated agents), `MLOPS_CLARIFY_EXPLAINABILITY` (7-year retention pattern), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `ComplianceStack` owns local CMK (honors 5th non-negotiable), publishes audit-bucket name + KMS ARN + backup-vault ARN via SSM; consumers grant identity-side `s3:PutObject` / `kms:Encrypt` / `backup:StartBackupJob`. Extracted evidence-collector handler and Inspector v2 enabler from inline `from_inline` to `AwsCustomResource` + external asset. Added Swap matrix (§5), Worked example (§6), Gotchas on Object Lock mode, Vault Lock irreversibility, and Config rule scope. Content preserved from v1.0 (compliance matrix, Config rule list, retention policies). |
| 1.0 | 2026-03-05 | Initial — compliance matrix, WORM audit bucket + CloudTrail, 15 Config rules, Vault-locked AWS Backup, Inspector v2 custom resource, evidence collector Lambda. |
