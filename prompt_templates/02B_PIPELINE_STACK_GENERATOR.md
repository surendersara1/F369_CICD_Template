# PASS 2B — Pipeline Stack Generator (pipeline_stack.py)

**Model:** Claude Opus 4.6  
**Input:** `ARCHITECTURE_MAP.md` from Pass 1  
**Output:** `infrastructure/pipeline_stack.py` — Self-Mutating CDK Pipeline

---

## SYSTEM PROMPT

```
You are a Senior DevOps Engineer and AWS CDK Pipelines expert.
You write production-grade self-mutating CDK Pipeline stacks in Python.

RULES:
1. Use `pipelines.CodePipeline` (CDK Pipelines v2) — NOT the low-level CodePipeline constructs
2. The pipeline is "self-mutating": when you push new CDK code, the pipeline updates itself
3. Stages: Source → Build/Synth → Dev → Integration Tests → Staging → APPROVAL → Prod
4. ALWAYS include manual approval between Staging and Production
5. ALWAYS include SNS notification for approval requests and pipeline failures
6. ALWAYS include automated test steps between Dev and Staging
7. CodeBuild must run with VPC access for integration tests that hit real AWS resources
8. Use GitHub OR CodeCommit source based on Architecture Map specification
9. Each deployment stage wraps the FullSystemStack in an AppStage CDK Stage class
10. Include rollback configuration: CloudWatch alarms trigger auto-rollback in prod
```

---

## USER PROMPT

```
Using the following Architecture Map, generate the COMPLETE `pipeline_stack.py` file.

## ARCHITECTURE MAP
---
{{ARCHITECTURE_MAP_CONTENT}}
---

## PIPELINE DESIGN SPECIFICATION

### Stage Sequence
```

[SOURCE] → [BUILD/SYNTH] → [DEV DEPLOY] → [INTEGRATION TESTS]
→ [STAGING DEPLOY] → [MANUAL APPROVAL] → [PROD DEPLOY] → [SMOKE TESTS]

````

### Pipeline Configuration Matrix

| Stage | CDK Class | Trigger | Approval | Notifications | Rollback |
|-------|-----------|---------|----------|---------------|----------|
| Source | CodePipelineSource | Git push to `main` | None | None | N/A |
| Build | ShellStep (CodeBuild) | Automatic | None | On failure → SNS | N/A |
| Dev Deploy | AppStage("dev") | Automatic | None | On failure → SNS | Manual |
| Integration Test | ShellStep (CodeBuild) | Automatic | None | On failure → SNS | N/A |
| Staging Deploy | AppStage("staging") | Automatic | None | On failure → SNS | Automatic |
| Prod Approval | ManualApprovalStep | Manual email | REQUIRED | Email → approvers | N/A |
| Prod Deploy | AppStage("prod") | After approval | None | On success + fail | Automatic |
| Smoke Tests | ShellStep (CodeBuild) | Automatic | None | On failure → SNS | N/A |

---

## COMPLETE CODE TEMPLATE

Generate a single Python file: `infrastructure/pipeline_stack.py`

```python
# =============================================================================
# FILE: infrastructure/pipeline_stack.py
# PROJECT: {{project_name}}
# DESCRIPTION: Self-mutating CDK Pipeline — Dev → Staging → Prod
# GENERATED: {{date}}
# CDK VERSION: aws-cdk-lib 2.x (CDK Pipelines v2)
# =============================================================================

from __future__ import annotations
from typing import List
from aws_cdk import (
    Stack, Stage, Duration, RemovalPolicy,
    aws_codecommit as codecommit,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_iam as iam,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_logs as logs,
    pipelines,
)
from constructs import Construct
from .app_stack import FullSystemStack

# =============================================================================
# APP STAGE WRAPPER
# =============================================================================
class AppStage(Stage):
    """
    CDK Stage wrapper for FullSystemStack.
    Each pipeline stage (dev/staging/prod) instantiates this class,
    which creates an isolated CloudFormation stack per environment.
    """
    def __init__(self, scope: Construct, id: str,
                 stage_name: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Instantiate the full application stack for this environment
        FullSystemStack(
            self, f"{{project_name}}Stack",
            stage_name=stage_name,
            # Stack-level env is inherited from Stage's env
        )


# =============================================================================
# PIPELINE STACK
# =============================================================================
class PipelineStack(Stack):
    """
    Self-mutating CDK Pipeline for {{project_name}}.

    Pipeline definition lives IN the repo — when you push changes to this file,
    the pipeline reconfigures itself on the next run (self-mutation).

    Flow:
      CodeCommit/GitHub → CodeBuild (synth) → Dev → Tests → Staging → Approval → Prod
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # === NOTIFICATIONS ===
        self._create_notification_topics()

        # === SOURCE ===
        source = self._create_source()

        # === CODEBUILD ENVIRONMENT ===
        build_env = self._create_build_environment()

        # === PIPELINE DEFINITION ===
        pipeline = pipelines.CodePipeline(
            self, "Pipeline",
            pipeline_name="{{project_name}}-Pipeline",

            # self-mutation: pipeline updates itself when pipeline code changes
            self_mutation=True,

            # Cross-account keys for multi-account deployment
            cross_account_keys=True,

            # Enable Docker for Lambda and ECS asset building
            docker_enabled_for_synth=True,

            # The synth step: build the CDK Cloud Assembly
            synth=pipelines.ShellStep(
                "Synth",
                input=source,
                install_commands=[
                    "npm install -g aws-cdk@latest",
                    "pip install -r requirements.txt",
                ],
                commands=[
                    "# Run unit tests before synth",
                    "pip install -r requirements-dev.txt",
                    "pytest tests/unit/ -v --tb=short",

                    "# Run security scan",
                    "pip install bandit",
                    "bandit -r infrastructure/ src/ -ll",

                    "# Synthesize CDK app",
                    "cdk synth",
                ],
                # Pass CDK context variables
                env={
                    "CDK_DEFAULT_REGION": self.region,
                    "CDK_DEFAULT_ACCOUNT": self.account,
                },
            ),

            # CodeBuild environment for the synth step
            code_build_defaults=pipelines.CodeBuildOptions(
                build_environment=build_env,
                role_policy=[
                    # Allow CodeBuild to look up existing resources
                    iam.PolicyStatement(
                        actions=[
                            "ec2:DescribeAvailabilityZones",
                            "ssm:GetParameter",
                            "sts:AssumeRole",
                        ],
                        resources=["*"],
                    ),
                ],
            ),
        )

        # =====================================================================
        # STAGE 1: DEV ENVIRONMENT (Auto-deploy)
        # =====================================================================
        dev_stage = pipeline.add_stage(
            AppStage(self, "Dev",
                     stage_name="dev",
                     env={"account": self.account, "region": self.region}),
        )

        # Smoke test step after dev deploy
        dev_stage.add_post(
            pipelines.ShellStep(
                "DevSmokeTests",
                commands=[
                    "pip install boto3 requests pytest",
                    "pytest tests/smoke/ -v -m 'dev' --tb=short",
                ],
                env_from_cfn_outputs={
                    # Pull API URL from dev stack outputs
                    "API_ENDPOINT": dev_stage.stages[0].stacks[0].format_arn(
                        service="execute-api", resource="*"
                    ) if False else "placeholder",
                    # Note: Claude should replace placeholder with actual CfnOutput
                },
            )
        )

        # =====================================================================
        # STAGE 2: INTEGRATION TESTS (Against Dev environment)
        # =====================================================================
        # NOTE: These run after dev deploy but before staging deploy
        integration_test_step = pipelines.ShellStep(
            "IntegrationTests",
            commands=[
                "pip install boto3 requests pytest pytest-asyncio",
                "# Run integration tests against Dev environment",
                "pytest tests/integration/ -v --tb=short -x",
                "echo 'Integration tests passed - proceeding to Staging'",
            ],
            env={
                "STAGE": "dev",
                "AWS_DEFAULT_REGION": self.region,
            },
        )

        # =====================================================================
        # STAGE 3: STAGING ENVIRONMENT (Auto-deploy after tests)
        # =====================================================================
        staging_stage = pipeline.add_stage(
            AppStage(self, "Staging",
                     stage_name="staging",
                     env={"account": self.account, "region": self.region}),
            pre=[integration_test_step],
        )

        # Performance test after staging deploy
        staging_stage.add_post(
            pipelines.ShellStep(
                "StagingPerformanceTests",
                commands=[
                    "pip install locust",
                    "# Run performance baseline tests",
                    "echo 'Running performance tests against staging...'",
                    "python tests/performance/run_load_test.py --duration 60 --users 10",
                ],
            )
        )

        # =====================================================================
        # STAGE 4: MANUAL APPROVAL (Required before Production)
        # =====================================================================
        # SNS notification is sent to approvers; they must click Approve in Console
        approval_step = pipelines.ManualApprovalStep(
            "ApproveProductionDeploy",
            comment=(
                "PRODUCTION DEPLOYMENT APPROVAL REQUIRED\n\n"
                "Please review the following before approving:\n"
                "1. Staging smoke tests have passed ✓\n"
                "2. Performance tests show no regression ✓\n"
                "3. Security scan completed ✓\n"
                "4. Change request ticket number: [REQUIRED]\n\n"
                "Staging environment URL: https://staging.{{project_name}}.example.com\n"
                "Approve in the AWS CodePipeline console."
            ),
        )

        # =====================================================================
        # STAGE 4.5: AGENT EVAL GATE (Conditional — if Strands agents detected)
        # [Claude: include this step if Architecture Map detects Strands agents.
        #  Uses STRANDS_AGENT_EVAL.md partial. Runs golden dataset eval against
        #  staging agent and blocks prod deploy if score < threshold.]
        # =====================================================================
        # agent_eval_step = pipelines.ShellStep(
        #     "AgentEvalGate",
        #     commands=[
        #         "pip install boto3",
        #         "EXEC_ARN=$(aws stepfunctions start-execution"
        #         "  --state-machine-arn $EVAL_STATE_MACHINE_ARN"
        #         "  --input '{\"eval_run_id\": \"cicd-'$CODEBUILD_BUILD_NUMBER'\"}'
        #         "  --query 'executionArn' --output text)",
        #         "# Poll until complete (see STRANDS_AGENT_EVAL.md for full script)",
        #     ],
        # )

        # =====================================================================
        # STAGE 5: PRODUCTION ENVIRONMENT (With rollback)
        # =====================================================================
        prod_stage = pipeline.add_stage(
            AppStage(self, "Prod",
                     stage_name="prod",
                     env={"account": self.account, "region": self.region}),
            pre=[approval_step],
        )

        # Post-production smoke tests
        prod_stage.add_post(
            pipelines.ShellStep(
                "ProdSmokeTests",
                commands=[
                    "pip install boto3 requests pytest",
                    "pytest tests/smoke/ -v -m 'prod' --tb=short",
                    "echo 'Production deployment successful!'",
                ],
            )
        )

        # Force pipeline build (required to generate pipeline assets)
        pipeline.build_pipeline()

        # === PIPELINE NOTIFICATIONS & ALARMS ===
        self._create_pipeline_alarms(pipeline)

        # === OUTPUTS ===
        self._create_outputs(pipeline)

    # -------------------------------------------------------------------------
    # HELPER METHODS
    # -------------------------------------------------------------------------

    def _create_notification_topics(self) -> None:
        """Create SNS topics for pipeline notifications."""

        # Approval notification topic
        self.approval_topic = sns.Topic(
            self, "ApprovalTopic",
            topic_name="{{project_name}}-prod-approval",
            display_name="{{project_name}} Production Deployment Approval",
        )

        # Add email subscriptions for approvers
        # [REPLACE WITH ACTUAL APPROVER EMAILS FROM ARCHITECTURE MAP OR SOW]
        APPROVER_EMAILS = [
            "devops-lead@example.com",
            "architect@example.com",
        ]
        for email in APPROVER_EMAILS:
            self.approval_topic.add_subscription(
                subs.EmailSubscription(email)
            )

        # Pipeline failure notification topic
        self.failure_topic = sns.Topic(
            self, "FailureTopic",
            topic_name="{{project_name}}-pipeline-failures",
            display_name="{{project_name}} Pipeline Failure Alerts",
        )
        self.failure_topic.add_subscription(
            subs.EmailSubscription("devops@example.com")
        )

    def _create_source(self) -> pipelines.IFileSetProducer:
        """
        Configure the pipeline source.
        {{IF_CODECOMMIT}}
        """
        # OPTION A: AWS CodeCommit (default)
        repo = codecommit.Repository.from_repository_name(
            self, "Repo",
            repository_name="{{project_name}}-repo",
        )
        return pipelines.CodePipelineSource.code_commit(
            repo, "main",
            action_name="Source",
            code_build_clone_output=True,  # Enable full git history for semantic versioning
        )

        # OPTION B: GitHub (uncomment if using GitHub)
        # return pipelines.CodePipelineSource.git_hub(
        #     "{{github_owner}}/{{github_repo}}",
        #     "main",
        #     authentication=SecretValue.secrets_manager("github-token"),
        #     trigger=codepipeline_actions.GitHubTrigger.WEBHOOK,
        # )

    def _create_build_environment(self) -> codebuild.BuildEnvironment:
        """High-performance CodeBuild environment for synthesis step."""
        return codebuild.BuildEnvironment(
            build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
            compute_type=codebuild.ComputeType.MEDIUM,
            privileged=True,  # Required for Docker builds (Lambda layers, ECS)
            environment_variables={
                "ENVIRONMENT": codebuild.BuildEnvironmentVariable(value="build"),
            },
        )

    def _create_pipeline_alarms(self, pipeline: pipelines.CodePipeline) -> None:
        """
        CloudWatch alarms for pipeline health monitoring.
        Alarms trigger SNS notifications on failures.
        """
        # NOTE: After pipeline.build_pipeline(), you can access
        # pipeline.pipeline (the underlying CodePipeline L2 construct)

        # [Claude: Add CloudWatch metric alarm for pipeline execution failures]
        # Example:
        # pipeline_metric = pipeline.pipeline.metric_failed_stage_executions(
        #     period=Duration.minutes(5)
        # )
        pass  # Replace with actual alarm code from Architecture Map

    def _create_outputs(self, pipeline: pipelines.CodePipeline) -> None:
        """Export pipeline metadata."""
        from aws_cdk import CfnOutput
        CfnOutput(
            self, "PipelineName",
            value=pipeline.pipeline.pipeline_name,
            description="Name of the CDK Pipeline",
        )
````

## PIPELINE CONFIGURATION SPECIFICS

Claude MUST populate these based on the Architecture Map:

### Source Branch Strategy

```
main branch  → Full pipeline (dev → staging → prod)
develop branch → Dev only pipeline (optional, based on SOW)
feature/* → Unit tests only (no deployment)
```

### CodeBuild Specifications

Generate a `buildspec_integration.yml` that runs:

```yaml
version: 0.2
phases:
  install:
    runtime-versions:
      python: 3.12
    commands:
      - pip install -r requirements-dev.txt
      - pip install boto3 pytest pytest-asyncio moto
  pre_build:
    commands:
      - echo "Running integration tests against $STAGE environment"
      - export API_ENDPOINT=$(aws cloudformation describe-stacks
        --stack-name {{project_name}}Stack-$STAGE
        --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue"
        --output text)
  build:
    commands:
      - pytest tests/integration/ -v --tb=short -x
  post_build:
    commands:
      - echo "Tests complete. Exit code $CODEBUILD_BUILD_SUCCEEDING"
reports:
  pytest-reports:
    files:
      - "**/*"
    base-directory: test-reports
    file-format: JUNITXML
```

### Approval Configuration

Approval email MUST include:

- Link to staging environment
- Link to CloudWatch dashboard
- Summary of changes deployed
- Estimated impact on production
- Rollback instructions

### Rollback Strategy

```
Dev:     No automatic rollback (fast iteration)
Staging: Automatic rollback if smoke tests fail
Prod:    CloudWatch alarm-triggered rollback + manual emergency rollback command
```

```

---

## PIPELINE VALIDATION CHECKLIST

- [ ] `self_mutation=True` is set on CodePipeline
- [ ] `cross_account_keys=True` for multi-account deployments
- [ ] Manual approval step exists between Staging and Prod
- [ ] SNS topics created for approval and failure notifications
- [ ] Email subscriptions added to approval topic
- [ ] Integration test step runs between Dev and Staging
- [ ] Smoke tests run after each environment deployment
- [ ] `AppStage` class wraps `FullSystemStack` with `stage_name` parameter
- [ ] `pipeline.build_pipeline()` called before accessing `pipeline.pipeline`
- [ ] CfnOutput created for Pipeline ARN and URL
```
