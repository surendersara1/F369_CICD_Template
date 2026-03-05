# PARTIAL: Security Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_security()` method body.

---

## CDK Code Block — Security Layer

```python
def _create_security(self, stage_name: str) -> None:
    """
    Layer 1: Security Infrastructure

    Components:
      A) KMS Customer Managed Keys (CMK) for encryption
      B) Secrets Manager secrets (DB credentials, API keys)
      C) Baseline IAM roles and policies
      D) GuardDuty + Security Hub (prod/staging)
      E) CloudTrail (all environments for SOC2/HIPAA)

    Principles:
      - All encryption uses Customer Managed Keys (CMK), not AWS-managed
      - All secrets in Secrets Manager with automatic rotation
      - GuardDuty threat detection in prod and staging
      - CloudTrail captures all API-level activity
    """

    # =========================================================================
    # A) KMS — Customer Managed Keys
    # =========================================================================

    # Master encryption key for all data (PHI, databases, queues, S3)
    self.kms_key = kms.Key(
        self, "MasterKey",
        alias=f"alias/{{project_name}}-master-{stage_name}",
        description=f"{{project_name}} master encryption key ({stage_name}) — encrypts all data at rest",

        # Key rotation (HIPAA: recommended annually, adjust for compliance requirements)
        enable_key_rotation=True,
        rotation_period=Duration.days(90) if stage_name == "prod" else Duration.days(365),

        # Key policy: allow account root + specific service principals
        # [Claude: CDK automatically creates key policy — customize if needed]

        # Multi-region key (for DR — only if multi-region is in Architecture Map)
        # multi_region=True,

        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
    )

    # Pipeline-specific key (for CodePipeline artifacts)
    self.pipeline_key = kms.Key(
        self, "PipelineKey",
        alias=f"alias/{{project_name}}-pipeline-{stage_name}",
        description=f"{{project_name}} pipeline artifact encryption key ({stage_name})",
        enable_key_rotation=True,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # =========================================================================
    # B) SECRETS MANAGER
    # =========================================================================

    # Note: Aurora credentials secret is created in _create_data_layer()
    # Additional secrets defined here:

    # [Claude: Add one Secret per external API credential from Architecture Map L1]
    # Example: Epic EHR API credentials
    self.epic_api_secret = sm.Secret(
        self, "EpicApiSecret",
        secret_name=f"/{{project_name}}/{stage_name}/integrations/epic",
        description="Epic EHR API credentials (client_id, client_secret, base_url)",
        secret_string_value=SecretValue.unsafe_plain_text(
            # Placeholder — replace with actual secret value via Console or CLI
            '{"client_id": "REPLACE_ME", "client_secret": "REPLACE_ME", "base_url": "REPLACE_ME"}'
        ),
        encryption_key=self.kms_key,
        # Note: rotation for API keys is custom — create rotation Lambda if needed
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
    )

    # Slack webhook for pipeline notifications (if applicable)
    self.slack_webhook_secret = sm.Secret(
        self, "SlackWebhookSecret",
        secret_name=f"/{{project_name}}/{stage_name}/notifications/slack-webhook",
        description="Slack webhook URL for DevOps notifications",
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # =========================================================================
    # C) BASELINE IAM ROLES
    # =========================================================================

    # Lambda Execution Role (base role — each Lambda also gets service-specific grants)
    # Note: CDK creates per-Lambda roles automatically. This is a custom SHARED role
    # [Claude: For HIPAA/SOC2, prefer per-Lambda roles — remove shared role if compliance needed]
    self.lambda_base_role = iam.Role(
        self, "LambdaBaseRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        role_name=f"{{project_name}}-lambda-base-{stage_name}",
        managed_policies=[
            # VPC access for Lambda (create/delete ENIs)
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaVPCAccessExecutionRole"
            ),
        ],
        inline_policies={
            "CloudWatchLogs": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                        resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/{{project_name}}/*"],
                    ),
                ]
            ),
            "XRay": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                        resources=["*"],
                    ),
                ]
            ),
        },
    )
    # Allow Lambda to use the master KMS key
    self.kms_key.grant_encrypt_decrypt(self.lambda_base_role)

    # =========================================================================
    # D) GUARDDUTY (threat detection — prod + staging)
    # =========================================================================

    if stage_name in ("staging", "prod"):
        # Enable GuardDuty detector
        guardduty_detector = guardduty.CfnDetector(
            self, "GuardDutyDetector",
            enable=True,
            finding_publishing_frequency="FIFTEEN_MINUTES" if stage_name == "prod" else "ONE_HOUR",
            features=[
                guardduty.CfnDetector.CFNFeatureConfigurationProperty(
                    name="S3_DATA_EVENTS",
                    status="ENABLED",
                ),
                guardduty.CfnDetector.CFNFeatureConfigurationProperty(
                    name="EKS_AUDIT_LOGS",
                    status="DISABLED",  # Not using EKS
                ),
                guardduty.CfnDetector.CFNFeatureConfigurationProperty(
                    name="LAMBDA_NETWORK_LOGS",
                    status="ENABLED",
                ),
            ],
        )

    # =========================================================================
    # E) CLOUDTRAIL (API activity audit log)
    # =========================================================================

    # S3 bucket for CloudTrail logs (separate from data bucket)
    cloudtrail_bucket = s3.Bucket(
        self, "CloudTrailBucket",
        bucket_name=f"{{project_name}}-cloudtrail-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        lifecycle_rules=[
            s3.LifecycleRule(
                id="RetainAuditLogs",
                enabled=True,
                expiration=Duration.days(365 * 7),  # 7 years retention (HIPAA)
                transitions=[
                    s3.Transition(
                        storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                        transition_after=Duration.days(90),
                    ),
                ],
            )
        ],
        removal_policy=RemovalPolicy.RETAIN,  # Always retain audit logs
        object_lock_enabled=stage_name == "prod",
    )

    # CloudTrail trail — log ALL API calls (management + data events)
    trail = cloudtrail.Trail(
        self, "AuditTrail",
        trail_name=f"{{project_name}}-audit-trail-{stage_name}",
        bucket=cloudtrail_bucket,

        # Include data events (S3 object reads/writes, Lambda invocations)
        # WARNING: This can generate large volumes of logs and increase cost
        send_to_cloud_watch_logs=True,
        cloud_watch_logs_retention=logs.RetentionDays.ONE_YEAR,

        # Encrypt logs
        encryption_key=self.kms_key,

        # Validate log integrity (detect tampering)
        enable_file_validation=True,

        # Include ALL regions (for CloudTrail completeness)
        is_multi_region_trail=True if stage_name == "prod" else False,
        include_global_service_events=True,
    )

    # Add data event selectors — log S3 object-level operations
    trail.log_all_s3_data_events()
    trail.log_all_lambda_data_events()

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    CfnOutput(self, "KmsKeyArn",
        value=self.kms_key.key_arn,
        description="Master KMS key ARN",
        export_name=f"{{project_name}}-kms-key-{stage_name}",
    )

    CfnOutput(self, "EpicApiSecretArn",
        value=self.epic_api_secret.secret_arn,
        description="Epic API credentials secret ARN",
        export_name=f"{{project_name}}-epic-secret-{stage_name}",
    )

    CfnOutput(self, "CloudTrailBucketName",
        value=cloudtrail_bucket.bucket_name,
        description="S3 bucket for CloudTrail audit logs",
        export_name=f"{{project_name}}-cloudtrail-bucket-{stage_name}",
    )
```

---

## Security Compliance Notes

| Control               | HIPAA            | SOC 2    | Implementation                       |
| --------------------- | ---------------- | -------- | ------------------------------------ |
| Encryption at rest    | Required         | Required | KMS CMK on all stores                |
| Encryption in transit | Required         | Required | TLS 1.2+ enforced everywhere         |
| Access logging        | Required         | Required | CloudTrail + API GW access logs      |
| Audit trail           | Required (6yr)   | Required | DynamoDB audit table + CloudTrail    |
| MFA                   | Required (admin) | Required | Cognito REQUIRED MFA                 |
| Least privilege       | Required         | Required | CDK `grant_*` methods                |
| Incident response     | Required         | Required | GuardDuty + SNS alarms               |
| Backup and recovery   | Required         | Required | Aurora backup + PITR + S3 versioning |
