# PARTIAL: CICD Pipeline Stages Configuration

**Usage:** Referenced by `02B_PIPELINE_STACK_GENERATOR.md` for approval gates and stage configs.

---

## The Three-Environment Pattern

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐     ┌────────────────┐     ┌──────────────┐
│   SOURCE     │────▶│    BUILD     │────▶│  DEV STAGE   │────▶│ STAGING STAGE  │────▶│  PROD STAGE  │
│  (Git Push)  │     │ (CDK Synth)  │     │ (Auto Deploy) │     │ (Auto Deploy)  │     │(Manual Appvl)│
└─────────────┘     └──────────────┘     └───────────────┘     └────────────────┘     └──────────────┘
                           │                      │                      │                      │
                      Unit Tests             Smoke Tests          Integration          Smoke Tests
                      Sec Scan               (Post-Deploy)           Tests             Rollback
                                                                  (Pre-Deploy)         Monitor
```

---

## Stage Definitions

### Stage 1: Source

```python
# CodeCommit source
source = pipelines.CodePipelineSource.code_commit(
    codecommit.Repository.from_repository_name(self, "Repo", "{{project_name}}-repo"),
    "main",
    action_name="Source",
    code_build_clone_output=True,
)

# OR GitHub source
source = pipelines.CodePipelineSource.git_hub(
    "{{org}}/{{project_name}}",
    "main",
    authentication=SecretValue.secrets_manager("github-token"),
)
```

### Stage 2: Build / Synth

```python
synth = pipelines.ShellStep(
    "Synth",
    input=source,
    install_commands=[
        "npm install -g aws-cdk@2",
        "pip install -r requirements.txt",
        "pip install -r requirements-dev.txt",
    ],
    commands=[
        "# Security scan",
        "bandit -r infrastructure/ src/ -ll -q",

        "# Unit tests",
        "pytest tests/unit/ -q --tb=short",

        "# CDK synthesis",
        "cdk synth --quiet",
    ],
    env={
        "CDK_DEFAULT_ACCOUNT": self.account,
        "CDK_DEFAULT_REGION": self.region,
    },
)
```

### Stage 3: Dev Deployment

```python
# === DEV STAGE (auto-deploy, no approval) ===
dev_stage = pipeline.add_stage(
    AppStage(self, "Dev", stage_name="dev",
             env={"account": DEV_ACCOUNT, "region": AWS_REGION}),
)

# Post-deploy: smoke tests
dev_stage.add_post(
    pipelines.ShellStep(
        "DevSmokeTests",
        env_from_cfn_outputs={
            "API_ENDPOINT": dev_stack.api_endpoint_output,
            "FRONTEND_URL": dev_stack.frontend_url_output,
        },
        commands=[
            "pip install requests pytest",
            "pytest tests/smoke/ -v -m dev --tb=short",
        ],
    )
)
```

### Stage 4: Integration Tests

```python
# === INTEGRATION TESTS (run against Dev environment before Staging) ===
integration_tests = pipelines.ShellStep(
    "IntegrationTests",
    env_from_cfn_outputs={
        "API_ENDPOINT": dev_stack.api_endpoint_output,
        "TABLE_NAME": dev_stack.main_table_output,
    },
    commands=[
        "pip install boto3 requests pytest pytest-asyncio",

        "# Test API endpoints end-to-end",
        "pytest tests/integration/ -v --tb=short -x --timeout=120",

        "# Generate test report",
        "pytest tests/integration/ --junitxml=test-results/integration.xml || true",
    ],
    primary_output_directory="test-results",
)
```

### Stage 5: Staging Deployment

```python
# === STAGING STAGE (requires integration tests to pass) ===
staging_stage = pipeline.add_stage(
    AppStage(self, "Staging", stage_name="staging",
             env={"account": STAGING_ACCOUNT, "region": AWS_REGION}),
    pre=[integration_tests],  # Runs integration tests BEFORE staging deploy
)

# Post-deploy: performance baseline
staging_stage.add_post(
    pipelines.ShellStep(
        "StagingPerformanceTest",
        commands=[
            "pip install locust requests",
            "python tests/performance/baseline_check.py",
        ],
    )
)
```

### Stage 6: Production Approval Gate

```python
# === MANUAL APPROVAL (between Staging and Production) ===
approval = pipelines.ManualApprovalStep(
    "ApproveProductionDeployment",
    comment="""
PRODUCTION DEPLOYMENT — APPROVAL REQUIRED

Pre-flight checklist:
✓ Dev smoke tests passed
✓ Integration tests passed
✓ Staging smoke tests passed
✓ Performance baseline checked

Action required:
1. Review the Staging environment: https://staging.{{project_name}}.example.com
2. Review CloudWatch dashboard: [Staging Dashboard Link]
3. Confirm change request: CR-xxxx
4. Click "Approve" below if satisfied

ROLLBACK: If prod deploy fails, run:
  aws codepipeline stop-pipeline-execution --pipeline-name {{project_name}}-Pipeline --abandon
    """,
)
```

### Stage 7: Production Deployment

```python
# === PRODUCTION STAGE (with rollback alarm) ===
prod_stage = pipeline.add_stage(
    AppStage(self, "Prod", stage_name="prod",
             env={"account": PROD_ACCOUNT, "region": AWS_REGION}),
    pre=[approval],
)

# Post-deploy: production smoke tests + notification
prod_stage.add_post(
    pipelines.ShellStep(
        "ProdSmokeTests",
        commands=[
            "pip install requests pytest boto3",
            "pytest tests/smoke/ -v -m prod --tb=short",

            "# Notify Slack on success",
            'python -c "import boto3; sns=boto3.client(\'sns\');'
            'sns.publish(TopicArn=\'$NOTIFICATION_TOPIC\','
            'Message=\'✅ {{project_name}} successfully deployed to Production\','
            'Subject=\'Prod Deploy Success\')"',
        ],
        env={
            "NOTIFICATION_TOPIC": self.failure_topic.topic_arn,
        }
    )
)
```

---

## Multi-Account Configuration

```python
# Account IDs per environment
# In practice, store these in SSM Parameter Store or CDK context
DEV_ACCOUNT     = app.node.try_get_context("dev_account")     or "111111111111"
STAGING_ACCOUNT = app.node.try_get_context("staging_account") or "222222222222"
PROD_ACCOUNT    = app.node.try_get_context("prod_account")    or "333333333333"
AWS_REGION      = app.node.try_get_context("region")          or "us-east-1"

# Each account must be bootstrapped with trust to the pipeline account:
# cdk bootstrap aws://DEV_ACCOUNT/us-east-1 --trust PIPELINE_ACCOUNT --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess
# cdk bootstrap aws://STAGING_ACCOUNT/us-east-1 --trust PIPELINE_ACCOUNT ...
# cdk bootstrap aws://PROD_ACCOUNT/us-east-1 --trust PIPELINE_ACCOUNT ...
```

---

## Emergency Rollback Procedures

### Automatic Rollback (CloudWatch Alarm)

```python
# Create alarm that triggers if error rate > 1% in prod
prod_error_alarm = cw.Alarm(
    self, "ProdErrorAlarm",
    metric=cw.Metric(
        namespace="AWS/Lambda",
        metric_name="Errors",
        statistic="Sum",
        period=Duration.minutes(1),
    ),
    threshold=10,
    evaluation_periods=2,
    alarm_description="Lambda error rate exceeded threshold in production",
    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
)

# Alarm action: notify team
prod_error_alarm.add_alarm_action(
    cw_actions.SnsAction(self.failure_topic)
)
```

### Manual Emergency Rollback Commands

```bash
# Stop the running pipeline execution
aws codepipeline stop-pipeline-execution \
    --pipeline-name {{project_name}}-Pipeline \
    --pipeline-execution-id <execution-id> \
    --abandon

# Redeploy previous version via CloudFormation rollback
aws cloudformation cancel-update-stack \
    --stack-name {{project_name}}Stack-Prod

# Or deploy a specific previous revision
git revert HEAD~1
git push origin main  # Triggers pipeline again
```

---

## Approval Notification Email Template

```
Subject: [ACTION REQUIRED] {{project_name}} Production Deployment Approval

A production deployment is waiting for your approval.

Deployment Summary:
  - Pipeline: {{project_name}}-Pipeline
  - Time: {timestamp}
  - Triggered by: {git_commit_author}
  - Commit: {git_commit_sha} — {git_commit_message}

Environments Status:
  ✅ Dev — smoke tests passed
  ✅ Integration tests — all {N} tests passed
  ✅ Staging — performance baseline within threshold

Pre-Approval Checklist:
  □ Reviewed staging environment
  □ Change request approved: CR-____
  □ On-call engineer notified: @{oncall_handle}
  □ Rollback plan reviewed

Approve here: {approval_url}

If you have concerns, REJECT the deployment — the pipeline
will notify the team and halt all production changes.

— {{project_name}} CI/CD System
```
