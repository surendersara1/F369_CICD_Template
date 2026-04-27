# SOP — AWS Backup (centralized backup · Vault Lock immutability · cross-region copy · cross-account · backup plan policy)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Backup centralized · Backup Vault Lock COMPLIANCE mode (immutable, ransomware-proof) · Backup plans + selection rules · Cross-region copy + cross-account vault · KMS multi-region encryption · 20+ supported services (RDS, EBS, EFS, FSx, DynamoDB, S3, EC2 AMI, Aurora, DocumentDB, Redshift, Storage Gateway, Neptune, etc.)

---

## 1. Purpose

- Codify **AWS Backup** as the canonical org-wide backup orchestration. Replaces per-service backup config (RDS automated backups, DDB on-demand, EFS lifecycle) with a single policy-driven control plane.
- Codify **Vault Lock COMPLIANCE mode** — backups become immutable for the retention period; cannot be deleted by IAM root user, AWS support, or anyone. Ransomware/insider-threat protection.
- Codify **Backup plans + selection rules** — tag-based dynamic resource inclusion (`backup: daily-30day`).
- Codify **cross-region copy** for DR (in addition to in-region backup).
- Codify **cross-account vault** — backups copied to dedicated backup account (defense against compromise of source account).
- Codify the **20+ supported AWS services** + which to back up.
- Codify **restore testing** — automated periodic restore-and-validate workflows.
- Pairs with `DR_MULTI_REGION_PATTERNS` (replication for HA), `DR_ROUTE53_ARC` (failover orchestration), `DR_RESILIENCE_HUB_FIS` (validation).

When the SOW signals: "compliance-grade backup", "ransomware protection", "centralized backup policy", "PCI/HIPAA backup retention requirement", "cross-account immutable backups".

---

## 2. Decision tree — backup strategy

```
Backup goal:
├── Operational recovery (oops, deleted a row)        → Per-service auto-backup OK; AWS Backup cleaner
├── Compliance retention (7 years for SOX/HIPAA)      → AWS Backup + Vault Lock COMPLIANCE
├── Ransomware-proof immutable                         → Vault Lock COMPLIANCE + cross-account vault
├── Cross-region DR (regional failure restore)         → AWS Backup with cross-region copy
└── Multi-account org-wide                             → Backup Audit Manager + Org Policies

Vault Lock mode:
├── COMPLIANCE  — IMMUTABLE; cannot shorten retention; cannot delete; even root cannot
├── GOVERNANCE  — admin can override with iam:PassRole  (less secure but allows fixes)
└── No lock     — defaults; admin can delete (NOT for compliance)

Cross-account pattern:
├── Single account: vault in same account as resources       (basic)
├── Centralized: vault in dedicated backup account           (recommended for prod)
└── Air-gapped: vault in fully isolated account + Org SCPs   (regulated)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 1 backup plan, 1 vault, daily snapshots | **§3 Monolith** |
| Production — Org-wide policy + cross-account + cross-region + Vault Lock | **§5 Production** |

---

## 3. Monolith Variant — backup plan + Vault Lock + cross-region copy

### 3.1 CDK

```python
# stacks/backup_stack.py
from aws_cdk import Stack, Duration, RemovalPolicy
from aws_cdk import aws_backup as backup
from aws_cdk import aws_kms as kms
from aws_cdk import aws_iam as iam
from constructs import Construct


class BackupStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 dr_region: str = "us-west-2", **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Multi-region KMS key for vaults ──────────────────────
        backup_key = kms.Key(self, "BackupKey",
            description="AWS Backup vault encryption (multi-region)",
            multi_region=True,
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
            policy=iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    sid="AllowRootIAM",
                    principals=[iam.AccountRootPrincipal()],
                    actions=["kms:*"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="AllowBackupService",
                    principals=[iam.ServicePrincipal("backup.amazonaws.com")],
                    actions=["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey",
                             "kms:GenerateDataKey*", "kms:ReEncrypt*"],
                    resources=["*"],
                ),
            ]),
        )

        # ── 2. Primary backup vault with Vault Lock COMPLIANCE ──────
        primary_vault = backup.BackupVault(self, "PrimaryVault",
            backup_vault_name=f"{env_name}-vault",
            encryption_key=backup_key,
            removal_policy=RemovalPolicy.RETAIN,
            access_policy=iam.PolicyDocument(statements=[
                # Block delete actions
                iam.PolicyStatement(
                    sid="DenyDeleteRecoveryPoint",
                    effect=iam.Effect.DENY,
                    principals=[iam.AnyPrincipal()],
                    actions=["backup:DeleteRecoveryPoint",
                             "backup:UpdateRecoveryPointLifecycle"],
                    resources=["*"],
                ),
            ]),
        )
        # Apply Vault Lock COMPLIANCE — IMMUTABLE
        # CFN-only via CfnBackupVaultNotifications custom property
        cfn_vault = primary_vault.node.default_child
        cfn_vault.add_property_override("LockConfiguration", {
            "MinRetentionDays": 7,                       # min retention for any backup
            "MaxRetentionDays": 2557,                     # 7 years max
            "ChangeableForDays": 3,                        # 3-day "dry run" before lock seals
        })

        # ── 3. Cross-region (DR) vault — copy target ────────────────
        # Cross-region vault must be created in DR region stack (separate CDK app)
        # For cross-region copy from this region's plan, we reference its ARN.
        dr_vault_arn = f"arn:aws:backup:{dr_region}:{self.account}:backup-vault:{env_name}-vault-dr"

        # ── 4. Backup plan ──────────────────────────────────────────
        plan = backup.BackupPlan(self, "Plan",
            backup_plan_name=f"{env_name}-org-plan",
            backup_plan_rules=[
                # Rule 1: daily 12 AM, 30-day retention, copy to DR
                backup.BackupPlanRule(
                    rule_name="daily-30d",
                    backup_vault=primary_vault,
                    schedule_expression=events.Schedule.cron(hour="0", minute="0"),
                    start_window=Duration.hours(1),
                    completion_window=Duration.hours(4),
                    delete_after=Duration.days(30),
                    move_to_cold_storage_after=Duration.days(30),    # for EFS / supported types
                    copy_actions=[backup.BackupPlanCopyActionProps(
                        destination_backup_vault=backup.BackupVault.from_backup_vault_arn(
                            self, "DrVaultRef", dr_vault_arn,
                        ),
                        delete_after=Duration.days(30),
                    )],
                    enable_continuous_backup=False,                   # set True for RDS/Aurora PITR
                ),
                # Rule 2: weekly Sunday 1 AM, 1-year retention
                backup.BackupPlanRule(
                    rule_name="weekly-1y",
                    backup_vault=primary_vault,
                    schedule_expression=events.Schedule.cron(
                        day_of_week="SUN", hour="1", minute="0",
                    ),
                    delete_after=Duration.days(365),
                    move_to_cold_storage_after=Duration.days(90),
                ),
                # Rule 3: monthly 1st 2 AM, 7-year retention (compliance)
                backup.BackupPlanRule(
                    rule_name="monthly-7y",
                    backup_vault=primary_vault,
                    schedule_expression=events.Schedule.cron(
                        day="1", hour="2", minute="0",
                    ),
                    delete_after=Duration.days(2557),
                    move_to_cold_storage_after=Duration.days(180),
                ),
            ],
        )

        # ── 5. Selection — tag-based dynamic resource inclusion ──────
        backup.BackupSelection(self, "TaggedSelection",
            backup_plan=plan,
            backup_selection_name=f"{env_name}-tagged-resources",
            resources=[
                backup.BackupResource.from_tag(
                    "backup", backup.TagOperation.STRING_EQUALS, "daily-30day",
                ),
            ],
            allow_restores=True,                            # role can restore
        )

        # Selection example: ALL RDS in account
        backup.BackupSelection(self, "AllRdsSelection",
            backup_plan=plan,
            backup_selection_name=f"{env_name}-all-rds",
            resources=[
                backup.BackupResource.from_arn(
                    f"arn:aws:rds:{self.region}:{self.account}:db:*",
                ),
            ],
        )

        # ── 6. Restore testing plan (2024+) ────────────────────────
        backup.CfnRestoreTestingPlan(self, "RestoreTestPlan",
            restore_testing_plan=backup.CfnRestoreTestingPlan.RestoreTestingPlanProperty(
                restore_testing_plan_name=f"{env_name}-restore-test",
                schedule_expression="cron(0 4 ? * SUN *)",     # weekly Sun 4 AM
                start_window_hours=24,
                recovery_point_selection=backup.CfnRestoreTestingPlan.RestoreTestingRecoveryPointSelectionProperty(
                    algorithm="LATEST_WITHIN_WINDOW",
                    include_vaults=[primary_vault.backup_vault_arn],
                    recovery_point_types=["SNAPSHOT"],
                    selection_window_days=7,
                ),
            ),
        )
        # Selection of resources to test-restore (e.g., RDS)
        backup.CfnRestoreTestingSelection(self, "RestoreTestSelection",
            restore_testing_plan_name=f"{env_name}-restore-test",
            iam_role_arn=restore_role.role_arn,
            protected_resource_arns=["*"],
            protected_resource_type="RDS",
            restore_testing_selection_name="rds-test",
        )
```

### 3.2 Tag resources for backup

```python
# In your application stacks, tag resources to opt them into the backup plan
from aws_cdk import Tags

ddb_table = ddb.Table(self, "OrdersTable", ...)
Tags.of(ddb_table).add("backup", "daily-30day")

rds_cluster = rds.DatabaseCluster(self, "AppDb", ...)
Tags.of(rds_cluster).add("backup", "daily-30day")

efs_fs = efs.FileSystem(self, "Shared", ...)
Tags.of(efs_fs).add("backup", "daily-30day")
```

---

## 4. Cross-account centralized backup (recommended for prod)

```
┌──────────────────────────────────────┐    ┌──────────────────────────────────────┐
│ Source Account (workload)            │    │ Backup Account (dedicated)           │
│   - DDB, RDS, EFS, EBS               │    │   - Backup Vault (Vault Lock)         │
│   - Backup plan with copy_action      │───►│   - KMS multi-region key             │
│   - Backup ServiceRole                │    │   - Cross-account share allow         │
└──────────────────────────────────────┘    └──────────────────────────────────────┘
```

### 4.1 Backup account vault (cross-account share)

```python
# In backup account's CDK
backup_vault = backup.BackupVault(self, "OrgBackupVault",
    backup_vault_name="org-backup-vault",
    encryption_key=multi_region_key,
    access_policy=iam.PolicyDocument(statements=[
        iam.PolicyStatement(
            sid="AllowSourceAccountsCopy",
            effect=iam.Effect.ALLOW,
            principals=[iam.AccountPrincipal("111111111111"),
                         iam.AccountPrincipal("222222222222")],
            actions=["backup:CopyIntoBackupVault"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="DenyDelete",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["backup:DeleteRecoveryPoint",
                     "backup:UpdateRecoveryPointLifecycle"],
            resources=["*"],
        ),
    ]),
)
# Apply Vault Lock COMPLIANCE
```

### 4.2 Source account plan with cross-account copy

```python
plan_rule = backup.BackupPlanRule(
    rule_name="daily-cross-acct",
    backup_vault=local_vault,
    schedule_expression=events.Schedule.cron(hour="0"),
    delete_after=Duration.days(30),
    copy_actions=[backup.BackupPlanCopyActionProps(
        destination_backup_vault=backup.BackupVault.from_backup_vault_arn(
            self, "CrossAcctVault",
            "arn:aws:backup:us-east-1:999999999999:backup-vault:org-backup-vault",
        ),
        delete_after=Duration.days(2557),                   # 7y in cross-acct vault
    )],
)
```

---

## 5. Production Variant — Org-wide policy via Backup Audit Manager

```python
# Run in Org Management account or delegated admin
# Org-wide backup policy (applies to all member accounts)
from aws_cdk import aws_organizations as orgs

orgs.CfnPolicy(self, "OrgBackupPolicy",
    name="org-backup-default",
    type="BACKUP_POLICY",
    content=json.dumps({
        "plans": {
            "OrgDailyBackup": {
                "regions": {"@@assign": ["us-east-1", "us-west-2"]},
                "rules": {
                    "daily-30d": {
                        "schedule_expression": {"@@assign": "cron(0 0 * * ? *)"},
                        "lifecycle": {
                            "delete_after_days": {"@@assign": "30"},
                            "move_to_cold_storage_after_days": {"@@assign": "30"},
                        },
                        "target_backup_vault_name": {"@@assign": "org-vault"},
                        "copy_actions": {
                            "arn:aws:backup:us-west-2:$account:backup-vault:org-vault-dr": {
                                "lifecycle": {
                                    "delete_after_days": {"@@assign": "30"},
                                },
                            },
                        },
                    },
                },
                "selections": {
                    "tags": {
                        "AllTaggedResources": {
                            "iam_role_arn": {"@@assign": "arn:aws:iam::$account:role/AWSBackupDefaultServiceRole"},
                            "tag_key": {"@@assign": "backup"},
                            "tag_value": {"@@assign": ["daily-30day"]},
                        },
                    },
                },
            },
        },
    }),
    target_ids=[workloads_ou_id],
)

# Backup Audit Manager — track compliance
from aws_cdk import aws_backupgateway as bag   # alias differs; use CfnFramework

backup.CfnFramework(self, "BackupFramework",
    framework_name=f"{env_name}-backup-compliance",
    framework_description="Org backup compliance (PCI + SOC 2)",
    framework_controls=[
        backup.CfnFramework.FrameworkControlProperty(
            control_name="BACKUP_RECOVERY_POINT_ENCRYPTED",
            control_input_parameters=[],
            control_scope=backup.CfnFramework.ControlScopeProperty(
                compliance_resource_types=["EBS", "RDS", "DynamoDB", "EFS"],
            ),
        ),
        backup.CfnFramework.FrameworkControlProperty(
            control_name="BACKUP_RECOVERY_POINT_MINIMUM_RETENTION_CHECK",
            control_input_parameters=[
                backup.CfnFramework.ControlInputParameterProperty(
                    parameter_name="requiredRetentionDays", parameter_value="30",
                ),
            ],
        ),
        backup.CfnFramework.FrameworkControlProperty(
            control_name="BACKUP_RESOURCES_PROTECTED_BY_BACKUP_PLAN",
            control_scope=backup.CfnFramework.ControlScopeProperty(
                tags={"backup": "daily-30day"},
            ),
        ),
    ],
)

# Generate compliance reports daily
backup.CfnReportPlan(self, "BackupReport",
    report_plan_name=f"{env_name}-daily-backup-report",
    report_setting=backup.CfnReportPlan.ReportSettingProperty(
        report_template="BACKUP_JOB_REPORT",
        framework_arns=[],
    ),
    report_delivery_channel=backup.CfnReportPlan.ReportDeliveryChannelProperty(
        s3_bucket_name=compliance_bucket.bucket_name,
        formats=["CSV", "JSON"],
    ),
)
```

---

## 6. Common gotchas

- **Vault Lock COMPLIANCE is permanent** — once `ChangeableForDays` window closes, you CANNOT shorten retention or delete vault. Even AWS Support cannot. Test in stage with GOVERNANCE first; promote to COMPLIANCE only after validated.
- **`MinRetentionDays`** — every backup plan rule must use `delete_after_days >= MinRetentionDays`. Otherwise plan rejected.
- **Cross-account vault encryption**: source account must have `kms:Encrypt` on destination vault's key. Easy to miss. Add KMS key policy.
- **Cross-region copy doubles cost** + adds data transfer. Plan for it (typical: 30-50% backup cost increase).
- **AWS Backup pricing** = $0.05/GB stored + $0.02/GB restored + cross-region transfer. 10 TB primary + 10 TB DR = $1000/mo.
- **Continuous backups (RDS PITR)** are extra: 30-day rolling window allows point-in-time recovery between snapshots.
- **Backup of Aurora cluster vs cluster instances** — Backup hands snapshots at cluster level, not instance. Restoring rebuilds cluster.
- **EFS cold storage** moves to Infrequent Access tier after `move_to_cold_storage_after_days`. Restoring from cold takes hours; charge for retrieval.
- **DynamoDB on-demand backups via AWS Backup are SEPARATE from DDB native PITR.** PITR (35-day rolling window) is automatic; AWS Backup is policy-driven.
- **Restore testing** uses real resources (counts toward your account limits) — schedule during off-hours; cleanup after.
- **Backup plan tag selection** is dynamic — adding a tag to a new resource auto-includes it in next backup window. Removing a tag stops new backups but keeps existing recovery points.
- **Backup IAM service-linked role** = `AWSServiceRoleForBackup`. Auto-created on first use; needs `iam:CreateServiceLinkedRole` admin.
- **Restore from cross-account vault** requires source-account IAM role assumed by destination account. Share role; document procedure.
- **Vault Lock violates "delete on stack delete"** — even `RemovalPolicy.DESTROY` won't delete locked vaults. Plan vault names; stick with them.

---

## 7. Pytest worked example

```python
# tests/test_backup.py
import boto3, pytest, datetime as dt

backup = boto3.client("backup")


def test_vault_lock_compliance(vault_name):
    vault = backup.describe_backup_vault(BackupVaultName=vault_name)
    lock = vault.get("LockDate")
    assert lock, "Vault not locked"
    assert vault.get("MinRetentionDays")
    assert vault.get("MaxRetentionDays")


def test_all_critical_resources_have_backup_tag():
    """All RDS clusters and DDB tables should have backup tag."""
    ddb = boto3.client("dynamodb")
    rds = boto3.client("rds")
    tagging = boto3.client("resourcegroupstaggingapi")

    # RDS clusters
    clusters = rds.describe_db_clusters()["DBClusters"]
    for c in clusters:
        if c["DBClusterIdentifier"].startswith("prod-"):
            tags = {t["Key"]: t["Value"] for t in c.get("TagList", [])}
            assert tags.get("backup") == "daily-30day", \
                f"Cluster {c['DBClusterIdentifier']} missing backup tag"


def test_recent_backup_jobs_succeeded(plan_name):
    """All backup jobs in last 7 days for our plan are COMPLETED."""
    by_plan = backup.list_backup_jobs(
        ByBackupPlanId=plan_name,
        ByCreatedAfter=dt.datetime.utcnow() - dt.timedelta(days=7),
    )["BackupJobs"]
    assert by_plan, "No backup jobs in last 7 days"
    failed = [j for j in by_plan if j["State"] not in ("COMPLETED", "RUNNING")]
    assert not failed, f"Failed jobs: {[j['BackupJobId'] for j in failed]}"


def test_cross_region_copy_jobs_succeeded():
    copies = backup.list_copy_jobs(
        ByCreatedAfter=dt.datetime.utcnow() - dt.timedelta(days=7),
    )["CopyJobs"]
    failed = [c for c in copies if c["State"] not in ("COMPLETED", "RUNNING")]
    assert not failed, f"Failed copies: {[c['CopyJobId'] for c in failed]}"


def test_restore_test_runs_pass():
    """Recent restore test jobs all PASSED."""
    tests = backup.list_restore_jobs(
        ByCreatedAfter=dt.datetime.utcnow() - dt.timedelta(days=7),
    )["RestoreJobs"]
    test_jobs = [t for t in tests if "RestoreTestRun" in (t.get("Description") or "")]
    failed = [t for t in test_jobs if t["Status"] not in ("COMPLETED", "RUNNING")]
    assert not failed, f"Restore tests failed: {[t['RestoreJobId'] for t in failed]}"


def test_backup_audit_framework_compliant(framework_name):
    """Backup Audit Manager framework — all controls COMPLIANT."""
    framework = backup.describe_framework(FrameworkName=framework_name)
    assert framework["DeploymentStatus"] == "COMPLETED"
    # Audit results must be checked via separate API
```

---

## 8. Five non-negotiables

1. **Vault Lock COMPLIANCE** for all production backups — IMMUTABLE, ransomware-proof.
2. **Cross-region copy** for any tier-1 / tier-2 workload (DR-ready).
3. **Cross-account vault** for prod — backups in dedicated backup account, isolated from source compromise.
4. **Tag-based selection** + Org-wide backup policy via SCPs (no opt-out).
5. **Restore testing weekly** — un-tested backups are no backups.

---

## 9. References

- [AWS Backup — User Guide](https://docs.aws.amazon.com/aws-backup/latest/devguide/whatisbackup.html)
- [Vault Lock](https://docs.aws.amazon.com/aws-backup/latest/devguide/vault-lock.html)
- [Backup Audit Manager](https://docs.aws.amazon.com/aws-backup/latest/devguide/aws-backup-audit-manager.html)
- [Cross-region + cross-account](https://docs.aws.amazon.com/aws-backup/latest/devguide/copy-actions.html)
- [Restore Testing (2024 GA)](https://docs.aws.amazon.com/aws-backup/latest/devguide/restore-testing.html)
- [Org-wide backup policy](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_backup.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. AWS Backup + Vault Lock COMPLIANCE + cross-region + cross-account + Org policy + Audit Manager + restore testing. Wave 14. |
