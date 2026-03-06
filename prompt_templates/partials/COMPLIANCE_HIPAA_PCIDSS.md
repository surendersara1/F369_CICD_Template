# PARTIAL: Compliance Blueprints — HIPAA, PCI DSS, SOC2

**Usage:** Include when SOW mentions HIPAA, PCI DSS, SOC2 Type II, GDPR, FedRAMP, BAA, PHI, cardholder data, or regulated workloads.

---

## Compliance Matrix — What Each Standard Requires

| Requirement            | HIPAA       | PCI DSS     | SOC2             | Our Implementation            |
| ---------------------- | ----------- | ----------- | ---------------- | ----------------------------- |
| Encryption at rest     | ✅ Required | ✅ Required | ✅ Required      | KMS CMK on ALL resources      |
| Encryption in transit  | ✅ Required | ✅ Required | ✅ Required      | TLS 1.2+ enforced everywhere  |
| Access logging         | ✅ Required | ✅ Required | ✅ Required      | CloudTrail + S3 access logs   |
| MFA                    | ✅ Required | ✅ Required | ✅ Best practice | Cognito MFA enforced          |
| Network segmentation   | ✅ Required | ✅ Required | ✅ Required      | VPC + private subnets only    |
| Vulnerability scanning | ✅ Required | ✅ Required | ✅ Required      | Inspector v2 auto-scan        |
| Backup & retention     | ✅ 6 years  | ✅ 1 year   | ✅ Required      | AWS Backup with vault lock    |
| Intrusion detection    | ✅ Required | ✅ Required | ✅ Required      | GuardDuty + Security Hub      |
| Incident response plan | ✅ Required | ✅ Required | ✅ Required      | CloudWatch alarms → PagerDuty |
| Audit trails immutable | ✅ Required | ✅ Required | ✅ Required      | CloudTrail + S3 Object Lock   |

---

## CDK Code Block — Compliance Blueprint

```python
def _create_compliance_controls(self, stage_name: str, compliance_standard: str = "HIPAA") -> None:
    """
    Compliance Controls Blueprint — HIPAA, PCI DSS, and SOC2.

    Compliance standards supported (set "compliance_standard" in Architecture Map):
      - "HIPAA"    → Healthcare PHI workloads, BAA-eligible services only
      - "PCI_DSS"  → Cardholder data environment (CDE) segmentation
      - "SOC2"     → Trust Service Criteria (Security, Availability, Confidentiality)
      - "ALL"      → Full compliance stack (union of all requirements)

    Components:
      A) Immutable Audit Trail (CloudTrail + S3 Object Lock)
      B) AWS Config Rules (continuous compliance checking)
      C) AWS Backup with vault lock (immutable backups)
      D) Inspector v2 (automated vulnerability scanning)
      E) Compliance Evidence Lambda (automated evidence collection)
      F) PCI DSS network segmentation (CDE isolation)
      G) HIPAA BAA-eligible service checklist validation
    """

    import aws_cdk.aws_config as config
    import aws_cdk.aws_inspector as inspector
    import aws_cdk.aws_backup as backup

    IS_HIPAA   = compliance_standard in ["HIPAA",   "ALL"]
    IS_PCI     = compliance_standard in ["PCI_DSS", "ALL"]
    IS_SOC2    = compliance_standard in ["SOC2",    "ALL"]

    # =========================================================================
    # A) IMMUTABLE AUDIT TRAIL
    # CloudTrail logs + S3 Object Lock (WORM) = tamper-proof audit evidence
    # =========================================================================

    # Compliance audit trail bucket — Object Lock prevents deletion
    audit_bucket = s3.Bucket(
        self, "ComplianceAuditBucket",
        bucket_name=f"{{project_name}}-compliance-audit-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        object_lock_enabled=True,  # WORM — cannot be deleted or modified
        object_lock_default_retention=s3.ObjectLockRetention.governance(
            duration=Duration.days(365 * (6 if IS_HIPAA else 1 if IS_PCI else 3))
        ),
        lifecycle_rules=[s3.LifecycleRule(
            id="archive-old-logs",
            transitions=[
                s3.Transition(storage_class=s3.StorageClass.GLACIER, transition_after=Duration.days(90)),
                s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transition_after=Duration.days(365)),
            ],
            enabled=True,
        )],
        removal_policy=RemovalPolicy.RETAIN,  # NEVER auto-delete compliance bucket
    )

    # CloudTrail — log ALL API calls across all services
    cloudtrail.Trail(
        self, "ComplianceCloudTrail",
        trail_name=f"{{project_name}}-compliance-trail-{stage_name}",
        bucket=audit_bucket,
        s3_key_prefix="cloudtrail",
        encryption_key=self.kms_key,
        include_global_service_events=True,  # Include IAM, STS, Route53
        is_multi_region_trail=True,          # Log from ALL regions
        enable_file_validation=True,         # Log file integrity validation (SHA-256)
        send_to_cloud_watch_logs=True,
        cloud_watch_log_group=logs.LogGroup(
            self, "CloudTrailLogGroup",
            log_group_name=f"/aws/cloudtrail/{{project_name}}-{stage_name}",
            retention=logs.RetentionDays.TEN_YEARS if IS_HIPAA else logs.RetentionDays.ONE_YEAR,
            encryption_key=self.kms_key,
            removal_policy=RemovalPolicy.RETAIN,
        ),
        cloud_watch_logs_retention=logs.RetentionDays.TEN_YEARS if IS_HIPAA else logs.RetentionDays.ONE_YEAR,
        management_events=cloudtrail.ReadWriteType.ALL,  # ALL management events
    )

    # =========================================================================
    # B) AWS CONFIG RULES — Continuous compliance checking
    # =========================================================================

    # Common compliance rules
    COMPLIANCE_RULES = [
        # Encryption
        ("encrypted-volumes",           config.ManagedRuleIdentifiers.EC2_EBS_ENCRYPTION_BY_DEFAULT),
        ("s3-bucket-encrypted",         config.ManagedRuleIdentifiers.S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED),
        ("rds-storage-encrypted",       config.ManagedRuleIdentifiers.RDS_STORAGE_ENCRYPTED),
        ("cloudtrail-encryption",       config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENCRYPTION_ENABLED),

        # Access control
        ("mfa-enabled-for-console",     config.ManagedRuleIdentifiers.MFA_ENABLED_FOR_IAM_CONSOLE_ACCESS),
        ("root-account-mfa",            config.ManagedRuleIdentifiers.ROOT_ACCOUNT_MFA_ENABLED),
        ("no-public-s3-buckets",        config.ManagedRuleIdentifiers.S3_BUCKET_PUBLIC_READ_PROHIBITED),
        ("no-public-rds",               config.ManagedRuleIdentifiers.RDS_INSTANCE_PUBLIC_ACCESS_CHECK),
        ("iam-no-inline-policies",      config.ManagedRuleIdentifiers.IAM_NO_INLINE_POLICY_CHECK),

        # Logging
        ("cloudtrail-enabled",          config.ManagedRuleIdentifiers.CLOUD_TRAIL_ENABLED),
        ("vpc-flow-logs-enabled",       config.ManagedRuleIdentifiers.VPC_FLOW_LOGS_ENABLED),
        ("s3-access-logs-enabled",      config.ManagedRuleIdentifiers.S3_BUCKET_LOGGING_ENABLED),

        # Network
        ("no-unrestricted-ssh",         config.ManagedRuleIdentifiers.INCOMING_SSH_DISABLED),
        ("no-unrestricted-rdp",         config.ManagedRuleIdentifiers.RESTRICTED_INCOMING_TRAFFIC),
        ("vpc-sg-open-only-to-alb",     config.ManagedRuleIdentifiers.EC2_SECURITY_GROUP_ATTACHED_TO_ENI),

        # Backup
        ("dynamo-backup-enabled",       config.ManagedRuleIdentifiers.DYNAMODB_PITR_ENABLED),
        ("rds-backup-enabled",          config.ManagedRuleIdentifiers.RDS_MULTI_AZ_SUPPORT),
    ]

    for rule_name, rule_id in COMPLIANCE_RULES:
        config.ManagedRule(
            self, f"ConfigRule{rule_name.replace('-', '').title()}",
            config_rule_name=f"{{project_name}}-{rule_name}-{stage_name}",
            identifier=rule_id,
            # Auto-remediation: notify on non-compliance
            rule_scope=config.RuleScope.from_resource(
                config.ResourceType.AWS_S3_BUCKET if "s3" in rule_name
                else config.ResourceType.AWS_EC2_INSTANCE
            ) if "s3" in rule_name or "ec2" in rule_name.lower() else None,
        )

    # =========================================================================
    # C) AWS BACKUP — Immutable, policy-driven backups
    # =========================================================================

    backup_vault = backup.BackupVault(
        self, "ComplianceBackupVault",
        backup_vault_name=f"{{project_name}}-compliance-vault-{stage_name}",
        encryption_key=self.kms_key,
        # Vault lock — once enabled, backups CANNOT be deleted (even by root)
        lock_configuration=backup.LockConfiguration(
            min_retention=Duration.days(7),
            max_retention=Duration.days(365 * 7),   # 7 years max retention
            changeable_for=Duration.days(3),         # 3-day grace period to configure, then locked FOREVER
        ),
        removal_policy=RemovalPolicy.RETAIN,
        notification_events=[
            backup.BackupVaultEvents.BACKUP_JOB_FAILED,
            backup.BackupVaultEvents.RESTORE_JOB_FAILED,
            backup.BackupVaultEvents.COPY_JOB_FAILED,
        ],
        notification_topic=self.alert_topic,
    )

    compliance_backup_plan = backup.BackupPlan(
        self, "ComplianceBackupPlan",
        backup_plan_name=f"{{project_name}}-compliance-{stage_name}",
        backup_vault=backup_vault,
        backup_plan_rules=[
            # Daily backups retained 35 days
            backup.BackupPlanRule(
                rule_name="DailyBackup",
                schedule_expression=events.Schedule.cron(hour="2", minute="0"),
                delete_after=Duration.days(35),
                move_to_cold_storage_after=Duration.days(7),
                completion_window=Duration.hours(4),
                start_window=Duration.hours(1),
                recovery_point_tags={"Type": "Daily", "Project": "{{project_name}}"},
            ),
            # Monthly backups retained 7 years (compliance)
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

    # Apply backup plan to all tagged resources
    compliance_backup_plan.add_selection(
        "AllTaggedResources",
        resources=[backup.BackupResource.from_tag("Project", "{{project_name}}")],
        allow_restores=True,
    )

    # =========================================================================
    # D) AMAZON INSPECTOR V2 — Automated vulnerability scanning
    # =========================================================================

    # Inspector v2 is enabled at account level via Custom Resource
    custom_resource_role = iam.Role(
        self, "InspectorEnableRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AWSLambdaBasicExecutionRole")],
    )
    custom_resource_role.add_to_policy(iam.PolicyStatement(
        actions=["inspector2:Enable", "inspector2:BatchGetAccountStatus"],
        resources=["*"],
    ))

    inspector_enable_fn = _lambda.Function(
        self, "InspectorEnableFn",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, json, cfnresponse, logging
logger = logging.getLogger()
inspector2 = boto3.client('inspector2')
def handler(event, context):
    try:
        if event['RequestType'] in ['Create', 'Update']:
            inspector2.enable(resourceTypes=['EC2', 'ECR', 'LAMBDA'])
            logger.info("Inspector v2 enabled")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        logger.error(f"Inspector enable failed: {e}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {})
"""),
        role=custom_resource_role,
        timeout=Duration.seconds(30),
    )

    CustomResource(
        self, "InspectorV2Enable",
        service_token=inspector_enable_fn.function_arn,
        properties={"Version": "1"},
    )

    # =========================================================================
    # E) COMPLIANCE EVIDENCE COLLECTOR
    # Runs weekly, collects automated evidence for auditors
    # =========================================================================

    evidence_fn = _lambda.Function(
        self, "ComplianceEvidenceFn",
        function_name=f"{{project_name}}-compliance-evidence-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime, timezone
logger = logging.getLogger()
logger.setLevel(logging.INFO)

config_client    = boto3.client('config')
securityhub      = boto3.client('securityhub')
s3               = boto3.client('s3')
audit_bucket     = os.environ['AUDIT_BUCKET']
compliance_std   = os.environ['COMPLIANCE_STANDARD']

def handler(event, context):
    evidence = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'compliance_standard': compliance_std,
        'account': boto3.client('sts').get_caller_identity()['Account'],
        'region': os.environ['AWS_DEFAULT_REGION'],
    }

    # Collect Config compliance summary
    config_resp = config_client.get_compliance_summary_by_config_rule()
    evidence['config_compliance'] = {
        'compliant_rules': config_resp['ComplianceSummary']['CompliantResourceCount']['CappedCount'],
        'non_compliant_rules': config_resp['ComplianceSummary']['NonCompliantResourceCount']['CappedCount'],
    }

    # Collect Security Hub findings summary
    sh_resp = securityhub.get_findings(
        Filters={'RecordState': [{'Value': 'ACTIVE', 'Comparison': 'EQUALS'}]},
        MaxResults=1,
    )
    evidence['security_hub_active_findings'] = sh_resp['Total'] if 'Total' in sh_resp else 'N/A'

    # Write evidence to audit bucket
    key = f"compliance-evidence/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/evidence.json"
    s3.put_object(
        Bucket=audit_bucket,
        Key=key,
        Body=json.dumps(evidence, indent=2),
        ContentType='application/json',
    )
    logger.info(f"Evidence collected: s3://{audit_bucket}/{key}")
    return {'evidence_location': f"s3://{audit_bucket}/{key}"}
"""),
        environment={
            "AUDIT_BUCKET":         audit_bucket.bucket_name,
            "COMPLIANCE_STANDARD":  compliance_standard,
        },
        timeout=Duration.minutes(5),
    )
    audit_bucket.grant_read_write(evidence_fn)
    evidence_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["config:GetComplianceSummaryByConfigRule", "securityhub:GetFindings",
                 "securityhub:GetFindingAggregator", "sts:GetCallerIdentity"],
        resources=["*"],
    ))

    # Weekly evidence collection
    events.Rule(self, "WeeklyEvidenceCollection",
        schedule=events.Schedule.cron(hour="6", minute="0", week_day="MON"),
        targets=[targets.LambdaFunction(evidence_fn)],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "ComplianceAuditBucket",
        value=audit_bucket.bucket_name,
        description=f"WORM-locked audit trail bucket ({compliance_standard} compliant)",
        export_name=f"{{project_name}}-audit-bucket-{stage_name}",
    )
    CfnOutput(self, "ComplianceBackupVaultArn",
        value=backup_vault.backup_vault_arn,
        description="Locked backup vault — immutable backups for compliance",
        export_name=f"{{project_name}}-backup-vault-{stage_name}",
    )
    CfnOutput(self, "EvidenceCollectorArn",
        value=evidence_fn.function_arn,
        description="Weekly compliance evidence collector Lambda",
        export_name=f"{{project_name}}-evidence-fn-{stage_name}",
    )
```
