# PARTIAL: Advanced Monitoring — CloudWatch Synthetics, Config, Backup, Cost

**Usage:** Referenced when SOW contains compliance, SLA monitoring, cost governance, or backup requirements.

---

## CDK Code Block — Advanced Operations & Monitoring

```python
def _create_advanced_monitoring(self, stage_name: str) -> None:
    """
    Advanced operational monitoring beyond basic CloudWatch alarms.

    Components:
      A) CloudWatch Synthetics Canaries  — synthetic monitoring of endpoints
      B) AWS Config Rules                — compliance posture + drift detection
      C) AWS Backup                      — centralized backup policies (HIPAA/SOC2)
      D) Cost Anomaly Detection          — automated cost spike alerting
      E) SSM Parameter Store             — config values (cheaper than Secrets Manager)
      F) CloudWatch Contributor Insights — high-cardinality traffic analysis
    """

    # =========================================================================
    # A) CLOUDWATCH SYNTHETICS — Endpoint canary monitoring
    # Runs headless browser tests against your URLs every N minutes
    # =========================================================================

    # Canary execution role
    canary_role = iam.Role(
        self, "CanaryRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchSyntheticsFullAccess"),
        ],
    )
    self.data_bucket.grant_read_write(canary_role)  # Canary stores screenshots/artifacts

    # API Health Canary — runs every 5 minutes, alerts if API returns non-200
    api_canary = synthetics.CfnCanary(
        self, "ApiHealthCanary",
        name=f"{{project_name}}-api-health-{stage_name}",
        artifact_s3_location=f"s3://{self.data_bucket.bucket_name}/canary-artifacts/api/",
        execution_role_arn=canary_role.role_arn,
        runtime_version="syn-nodejs-puppeteer-6.2",

        schedule=synthetics.CfnCanary.ScheduleProperty(
            expression="rate(5 minutes)",
        ),

        # Inline canary script
        code=synthetics.CfnCanary.CodeProperty(
            handler="index.handler",
            script="""
const synthetics = require('Synthetics');
const log = require('SyntheticsLogger');

const apiCanaryBlueprint = async function () {
    const url = process.env.API_ENDPOINT + '/health';

    const requestOptions = {
        hostname: new URL(url).hostname,
        path: new URL(url).pathname,
        method: 'GET',
        headers: { 'User-Agent': 'CloudWatch-Synthetics' },
        protocol: 'https:',
        port: 443,
    };

    await synthetics.executeHttpStep('Verify API Health', requestOptions, async function(res) {
        if (res.statusCode !== 200) {
            throw new Error(`Expected 200 but got ${res.statusCode}`);
        }
        const body = await new Promise((resolve) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => resolve(data));
        });
        const json = JSON.parse(body);
        if (json.status !== 'healthy') {
            throw new Error(`Health check returned status: ${json.status}`);
        }
    });
};

exports.handler = async () => { return await apiCanaryBlueprint(); };
            """,
        ),

        run_config=synthetics.CfnCanary.RunConfigProperty(
            timeout_in_seconds=60,
            environment_variables={"API_ENDPOINT": "https://api.{{project_name}}.example.com"},
        ),

        # Only start canary in non-dev environments (save cost)
        start_canary_after_creation=stage_name != "dev",

        success_retention_period=30,
        failure_retention_period=90,
    )

    # Alarm on canary failure
    cw.Alarm(
        self, "ApiCanaryAlarm",
        alarm_name=f"{{project_name}}-api-canary-{stage_name}",
        alarm_description="Synthetic API health check failing",
        metric=cw.Metric(
            namespace="CloudWatchSynthetics",
            metric_name="SuccessPercent",
            dimensions_map={"CanaryName": api_canary.name},
            period=Duration.minutes(5),
            statistic="Average",
        ),
        threshold=90,  # Alert if success rate drops below 90%
        comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluation_periods=3,
        treat_missing_data=cw.TreatMissingData.BREACHING,
    ).add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # =========================================================================
    # B) AWS CONFIG — Compliance posture + drift detection
    # Continuously evaluates resources against rules
    # =========================================================================

    if stage_name in ("staging", "prod"):

        MANAGED_CONFIG_RULES = [
            # Encryption rules
            ("S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED", {}),
            ("S3_BUCKET_SSL_REQUESTS_ONLY", {}),
            ("RDS_STORAGE_ENCRYPTED", {}),
            ("DYNAMODB_TABLE_ENCRYPTED_AT_REST", {}),
            ("ENCRYPTED_VOLUMES", {}),

            # Access control rules
            ("S3_BUCKET_PUBLIC_READ_PROHIBITED", {}),
            ("S3_BUCKET_PUBLIC_WRITE_PROHIBITED", {}),
            ("IAM_PASSWORD_POLICY", {
                "RequireUppercaseCharacters": "true",
                "RequireLowercaseCharacters": "true",
                "RequireSymbols": "true",
                "RequireNumbers": "true",
                "MinimumPasswordLength": "12",
            }),
            ("MFA_ENABLED_FOR_IAM_CONSOLE_ACCESS", {}),

            # Logging rules
            ("CLOUD_TRAIL_ENABLED", {}),
            ("CLOUDWATCH_LOG_GROUP_ENCRYPTED", {}),

            # Network rules
            ("RESTRICTED_INCOMING_TRAFFIC", {"blockedPort1": "22", "blockedPort2": "3389"}),
            ("VPC_DEFAULT_SECURITY_GROUP_CLOSED", {}),
            ("VPC_FLOW_LOGS_ENABLED", {}),
        ]

        for rule_name, parameters in MANAGED_CONFIG_RULES:
            config.ManagedRule(
                self, f"ConfigRule{rule_name.replace('_','').title()[:20]}",
                identifier=config.ManagedRuleIdentifiers.by_managed_rule_identifier(rule_name),
                config_rule_name=f"{{project_name}}-{rule_name.lower().replace('_','-')}-{stage_name}",
                input_parameters=parameters if parameters else None,
            )

    # =========================================================================
    # C) AWS BACKUP — Centralized backup policy (HIPAA/SOC2 required)
    # =========================================================================

    # Backup vault (encrypted, with access policy)
    backup_vault = backup.BackupVault(
        self, "BackupVault",
        backup_vault_name=f"{{project_name}}-vault-{stage_name}",
        encryption_key=self.kms_key,

        # Lock settings: prevent backup deletion for compliance
        lock_configuration=backup.LockConfiguration(
            min_retention=Duration.days(7),
            max_retention=Duration.days(365 * 7),  # 7 years for HIPAA
            change_timeout=Duration.days(3),        # 3-day cooling period before lock
        ) if stage_name == "prod" else None,

        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
    )

    # Backup plan (schedule + retention rules)
    backup_plan = backup.BackupPlan(
        self, "BackupPlan",
        backup_plan_name=f"{{project_name}}-backup-plan-{stage_name}",
        backup_vault=backup_vault,
        backup_plan_rules=[
            # Daily backups — retained for 35 days
            backup.BackupPlanRule(
                rule_name="DailyBackup",
                schedule_expression=events.Schedule.cron(hour="4", minute="0"),  # 4am UTC daily
                delete_after=Duration.days(35),
                move_to_cold_storage_after=Duration.days(30) if stage_name == "prod" else None,
                recovery_point_tags={"Type": "Daily", "Project": "{{project_name}}"},
            ),
            # Weekly backups — retained for 1 year
            backup.BackupPlanRule(
                rule_name="WeeklyBackup",
                schedule_expression=events.Schedule.cron(hour="5", minute="0", week_day="SUN"),
                delete_after=Duration.days(365),
                move_to_cold_storage_after=Duration.days(90),
                recovery_point_tags={"Type": "Weekly"},
            ),
            # Monthly backups — retained for 7 years (HIPAA minimum 6 years)
            backup.BackupPlanRule(
                rule_name="MonthlyBackup",
                schedule_expression=events.Schedule.cron(hour="6", minute="0", day="1"),
                delete_after=Duration.days(365 * 7),
                move_to_cold_storage_after=Duration.days(30),
                recovery_point_tags={"Type": "Monthly"},
            ),
        ],
    )

    # Resources to back up — selection by tags
    backup_plan.add_selection(
        "TaggedResources",
        resources=[
            backup.BackupResource.from_tag("Project", "{{project_name}}"),
            backup.BackupResource.from_tag("Environment", stage_name),
        ],
        backup_selection_name=f"{{project_name}}-all-resources-{stage_name}",
        role=iam.Role.from_role_arn(
            self, "BackupRole",
            f"arn:aws:iam::{self.account}:role/AWSBackupDefaultServiceRole",
            mutable=False,
        ),
    )

    # =========================================================================
    # D) COST ANOMALY DETECTION — Alert on unexpected spend spikes
    # =========================================================================

    # Cost anomaly monitor + subscription (email alert when spend spikes)
    anomaly_monitor = ce.CfnAnomalyMonitor(
        self, "CostAnomalyMonitor",
        monitor_name=f"{{project_name}}-cost-monitor-{stage_name}",
        monitor_type="DIMENSIONAL",
        monitor_dimension="SERVICE",  # Monitor by AWS service
    )

    ce.CfnAnomalySubscription(
        self, "CostAnomalyAlert",
        subscription_name=f"{{project_name}}-cost-alerts-{stage_name}",
        monitor_arn_list=[anomaly_monitor.attr_monitor_arn],

        # Alert when anomaly exceeds $50 or 50% above expected
        threshold_expression= '{ "And": [{ "Dimensions": { "Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE", "MatchOptions": ["GREATER_THAN_OR_EQUAL"], "Values": ["50"] } }] }',

        subscribers=[
            ce.CfnAnomalySubscription.SubscriberProperty(
                address="devops@example.com",  # [Claude: replace with Architecture Map owner email]
                type="EMAIL",
                status="CONFIRMED",
            ),
            ce.CfnAnomalySubscription.SubscriberProperty(
                address=self.alert_topic.topic_arn,
                type="SNS",
                status="CONFIRMED",
            ),
        ],
        frequency="DAILY",
    )

    # =========================================================================
    # E) SSM PARAMETER STORE — Non-secret config values
    # Much cheaper than Secrets Manager for non-sensitive config
    # (~$0.05/10k API calls vs $0.40/secret/month)
    # =========================================================================

    # Store non-sensitive config: feature flags, limits, URLs
    ssm.StringParameter(
        self, "ApiEndpointParam",
        parameter_name=f"/{{project_name}}/{stage_name}/config/api_endpoint",
        string_value=self.rest_api.url if hasattr(self, 'rest_api') else "PLACEHOLDER",
        description="API Gateway endpoint URL",
        tier=ssm.ParameterTier.STANDARD,  # Free tier (up to 4KB)
    )

    ssm.StringParameter(
        self, "FeatureFlagsParam",
        parameter_name=f"/{{project_name}}/{stage_name}/config/feature_flags",
        string_value='{"new_dashboard": false, "bulk_export": true}',
        description="Feature flag configuration (JSON)",
        tier=ssm.ParameterTier.STANDARD,
    )

    # Grant Lambda to read SSM parameters (much cheaper: GetParameters by path)
    ssm_read_policy = iam.PolicyStatement(
        actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
        resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{{project_name}}/{stage_name}/*"],
    )
    for fn in self.lambda_functions.values():
        fn.add_to_role_policy(ssm_read_policy)

    # =========================================================================
    # F) CLOUDWATCH CONTRIBUTOR INSIGHTS — Top-N traffic analysis
    # See which API paths, users, or IPs generate the most traffic
    # =========================================================================

    # Contributor Insights on DynamoDB (identifies hot partition keys)
    cw.CfnInsightRule(
        self, "DynamoHotKeysRule",
        rule_name=f"{{project_name}}-dynamo-hot-keys-{stage_name}",
        rule_body=json.dumps({
            "Schema": {"Name": "CloudWatchLogRule", "Version": 1},
            "AggregateOn": "Count",
            "Contribution": {
                "Filters": [{"Match": "$.eventSource", "In": ["dynamodb.amazonaws.com"]}],
                "Keys": ["$.requestParameters.tableName", "$.requestParameters.key.pk.S"],
            },
            "LogGroupNames": [f"/{{project_name}}/{stage_name}/*"],
        }),
        rule_state="ENABLED",
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "BackupVaultName",
        value=backup_vault.backup_vault_name,
        description="AWS Backup vault name",
        export_name=f"{{project_name}}-backup-vault-{stage_name}",
    )
```

---

## DLQ Redrive Automation (Auto-recover failed messages)

```python
def _create_dlq_redrive(self, stage_name: str) -> None:
    """
    Automatically redrive messages from DLQ back to source queue
    after investigation window. Avoids manual operational toil.

    Pattern:
      CloudWatch alarm (DLQ > 0 for 1hr) → SNS → On-call engineer
      Engineer investigates → Manual redrive via console OR automated Lambda
    """

    # Lambda to redrive DLQ → source queue (call this after investigation)
    redrive_fn = _lambda.Function(
        self, "DLQRedriveFn",
        function_name=f"{{project_name}}-dlq-redrive-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="index.handler",
        architecture=_lambda.Architecture.ARM_64,
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

sqs = boto3.client('sqs')

def handler(event, context):
    dlq_url = os.environ['DLQ_URL']
    source_url = os.environ['SOURCE_QUEUE_URL']
    max_messages = int(event.get('max_messages', 10))
    redriven = 0

    while redriven < max_messages:
        resp = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
        msgs = resp.get('Messages', [])
        if not msgs:
            break
        for msg in msgs:
            sqs.send_message(QueueUrl=source_url, MessageBody=msg['Body'],
                           MessageAttributes=msg.get('MessageAttributes', {}))
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=msg['ReceiptHandle'])
            redriven += 1
            logger.info(f"Redriven message {msg['MessageId']}")

    return {"redriven": redriven}
"""),
        environment={
            "DLQ_URL": self.dlq.queue_url,
            "SOURCE_QUEUE_URL": self.main_queue.queue_url,
        },
        timeout=Duration.minutes(5),
        tracing=_lambda.Tracing.ACTIVE,
    )

    # Permissions
    self.dlq.grant_consume_messages(redrive_fn)
    self.main_queue.grant_send_messages(redrive_fn)

    CfnOutput(self, "DLQRedriveFunctionArn",
        value=redrive_fn.function_arn,
        description="Invoke this Lambda to redrive DLQ messages (after investigation)",
    )
```
