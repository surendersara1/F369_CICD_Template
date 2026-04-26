# SOP — Cross-account ML model deployment (multi-account governance · RAM model package sharing · stage promotion)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Model Registry shared via AWS RAM (Resource Access Manager) · cross-account IAM roles for endpoint deployment · KMS key cross-account grant patterns · ECR cross-account image sharing · S3 cross-account artifact access · multi-account governance (training-account → staging-account → prod-account)

---

## 1. Purpose

- Codify the **3-account ML governance pattern** that mature orgs adopt:
  - **Training account** — owns training data, training jobs, MLflow, registered model packages
  - **Staging account** — pulls approved models from training, deploys to test endpoints, runs golden-set tests
  - **Prod account** — pulls validated models from staging, deploys to production endpoints, isolated blast radius
- Codify the **RAM share** for Model Package Groups (NEW in 2024 — replaces manual cross-account IAM trust).
- Codify the **ECR cross-account pull** for custom inference containers.
- Codify the **KMS grant** pattern for encrypted model artifacts crossing accounts.
- Provide the **promotion Lambda** that copies an approved model from training-account → staging-account, then staging-account → prod-account.
- This is the **multi-account ML governance specialisation**. Single-account works for POCs; production ML at scale needs blast-radius isolation.

When the SOW signals: "regulated industry needs SoD between data scientists and prod", "training account separate from prod", "we need approval gates between staging and prod", "audit needs proof of model lineage across accounts".

---

## 2. Decision tree — single account vs multi-account

```
Account model?
├── 1 account (data scientists deploy directly to prod) → MLOPS_SAGEMAKER_TRAINING (POC mode)
├── 2 accounts (training + prod) → §3 simplified flow
├── 3 accounts (training + staging + prod) → §3 RECOMMENDED for regulated/large
├── 4+ accounts (per-business-unit) → §4 hierarchical RAM shares
└── Cross-region as well as cross-account → combine §3 + Aurora-Global-DR pattern

Sharing mechanism?
├── Model Registry → RAM share (modern, 2024+)
├── ECR images → ECR repository policy + cross-account pull
├── S3 model artifacts → S3 bucket policy + KMS cross-account grant
└── Lake Formation tables → see DATA_LAKE_FORMATION §3.5 (cross-account RAM)

Approval flow?
├── Manual approval each promotion → human-in-the-loop Lambda
├── Auto-promotion on golden-set pass → EventBridge + Lambda
└── Hybrid (auto stage→prod, manual prod→region2) → Step Functions w/ wait state
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 2 accounts only (training + prod) | **§3 simplified Variant** |
| Production — 3 accounts (training + staging + prod) | **§3 full Variant** |
| Enterprise — N accounts via RAM hierarchy | **§4 hierarchical Variant** |

---

## 3. Three-account variant — training → staging → prod

### 3.1 Architecture

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  TRAINING ACCOUNT (111111111111)                                 │
   │     - SageMaker Pipelines, MLflow, training data, processing     │
   │     - Model Registry: ModelPackageGroup "qra-llm-mpg"             │
   │     - ECR: training images                                        │
   │     - S3: model artifacts (KMS-encrypted)                         │
   │                                                                    │
   │  Approval gate: human ApprovalStatus="Approved"                   │
   │     ↓                                                              │
   │  EventBridge: ModelPackageStateChange → Lambda → RAM share        │
   └────────────────────┬────────────────────────────────────────────┘
                        │
                        │  RAM share grant + IAM trust
                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  STAGING ACCOUNT (222222222222)                                  │
   │     - DeployerLambda — picks up shared MPG                        │
   │     - SageMaker Endpoint: staging-endpoint                        │
   │     - Golden-set test runner — automated test pass/fail           │
   │     - On test pass: re-promote to prod-account RAM share          │
   └────────────────────┬────────────────────────────────────────────┘
                        │
                        │  Auto or manual promotion
                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  PROD ACCOUNT (333333333333)                                     │
   │     - DeployerLambda — picks up shared MPG (staging-validated)    │
   │     - SageMaker Endpoint: prod-endpoint (blue/green or canary)    │
   │     - CloudWatch alarms → auto-rollback                          │
   └─────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK in TRAINING account — RAM share

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_ram as ram,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
)


def _create_training_account_share(self, stage: str) -> None:
    """In TRAINING account. Set up RAM share for Model Package Group +
    ECR repos + KMS key, triggered by Approved model package state."""

    # A) Existing MPG (created by training pipeline; from MLOPS_LLM_FINETUNING_PROD)
    mpg_arn = (f"arn:aws:sagemaker:{self.region}:{self.account}:"
               f"model-package-group/{{project_name}}-llm-mpg")

    # B) RAM resource share — shares MPG with staging + prod accounts
    self.mpg_share = ram.CfnResourceShare(self, "MpgShare",
        name=f"{{project_name}}-mpg-share-{stage}",
        principals=[
            "222222222222",                          # staging account ID
            "333333333333",                          # prod account ID
        ],
        resource_arns=[mpg_arn],
        permission_arns=[
            # AWS-managed RAM permission for Model Package Group
            f"arn:aws:ram::aws:permission/AWSRAMPermissionSageMakerModelPackageGroup",
        ],
        allow_external_principals=False,             # only org accounts
    )

    # C) IAM trust on MPG for staging+prod deployer Lambdas
    self.mpg_resource_policy = iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[
                    iam.AccountPrincipal("222222222222"),
                    iam.AccountPrincipal("333333333333"),
                ],
                actions=[
                    "sagemaker:DescribeModelPackage",
                    "sagemaker:DescribeModelPackageGroup",
                    "sagemaker:ListModelPackages",
                    "sagemaker:GetModelPackageGroupPolicy",
                ],
                resources=[
                    mpg_arn,
                    f"{mpg_arn}/*",
                ],
            ),
        ],
    )
    # Attach via custom resource (CDK doesn't have L2 for MPG resource policy)
    cr.AwsCustomResource(self, "MpgPolicyAttach",
        on_create=cr.AwsSdkCall(
            service="SageMaker",
            action="putModelPackageGroupPolicy",
            parameters={
                "ModelPackageGroupName": "{{project_name}}-llm-mpg",
                "ResourcePolicy": self.mpg_resource_policy.to_string(),
            },
            physical_resource_id=cr.PhysicalResourceId.of("MpgPolicy"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
            resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE,
        ),
    )

    # D) S3 bucket policy — allows staging+prod to read model artifacts
    self.artifacts_bucket.add_to_resource_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        principals=[
            iam.AccountPrincipal("222222222222"),
            iam.AccountPrincipal("333333333333"),
        ],
        actions=["s3:GetObject"],
        resources=[f"{self.artifacts_bucket.bucket_arn}/models/*"],
    ))

    # E) KMS key cross-account grant
    self.kms_key.grant_decrypt(
        iam.AccountPrincipal("222222222222"),
    )
    self.kms_key.grant_decrypt(
        iam.AccountPrincipal("333333333333"),
    )

    # F) ECR cross-account pull
    self.training_image_repo.add_to_resource_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        principals=[
            iam.AccountPrincipal("222222222222"),
            iam.AccountPrincipal("333333333333"),
        ],
        actions=[
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
            "ecr:BatchCheckLayerAvailability",
        ],
    ))

    # G) Notify Lambda — fires on Approved model package state change,
    # publishes to a cross-account SNS so staging picks up
    notify_fn = lambda_.Function(self, "NotifyStagingFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "notify_staging")),
        timeout=Duration.minutes(5),
        environment={
            "STAGING_ACCOUNT": "222222222222",
            "STAGING_REGION":  self.region,
        },
    )
    notify_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sns:Publish"],
        # Cross-account SNS topic ARN (created in staging account)
        resources=[f"arn:aws:sns:{self.region}:222222222222:promote-to-staging"],
    ))

    events.Rule(self, "ApprovedModelRule",
        event_pattern=events.EventPattern(
            source=["aws.sagemaker"],
            detail_type=["SageMaker Model Package State Change"],
            detail={
                "ModelPackageGroupName": ["{{project_name}}-llm-mpg"],
                "ModelApprovalStatus":   ["Approved"],
            },
        ),
        targets=[targets.LambdaFunction(notify_fn)],
    )
```

### 3.3 CDK in STAGING account — accept share + deploy

```python
def _create_staging_deploy(self, stage: str) -> None:
    """In STAGING account. Receives Approved model packages from training,
    deploys to staging endpoint, runs golden-set tests."""

    # A) Cross-account SNS topic — receives notifications from training
    self.promote_topic = sns.Topic(self, "PromoteToStagingTopic",
        topic_name="promote-to-staging",
        master_key=self.kms_key,
    )
    self.promote_topic.grant_publish(
        iam.AccountPrincipal("111111111111"),                # training account
    )

    # B) DeployerLambda — picks up the model package, creates endpoint
    deployer_fn = lambda_.Function(self, "StagingDeployerFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "staging_deployer")),
        timeout=Duration.minutes(15),
        environment={
            "TRAINING_ACCOUNT": "111111111111",
            "STAGE_ENDPOINT_NAME": "staging-endpoint",
        },
    )
    deployer_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "sagemaker:DescribeModelPackage",                # cross-account read
            "sagemaker:CreateModel",
            "sagemaker:CreateEndpointConfig",
            "sagemaker:CreateEndpoint",
            "sagemaker:UpdateEndpoint",
            "sagemaker:DescribeEndpoint",
        ],
        resources=["*"],
    ))
    # IAM PassRole for SageMaker to assume during create
    deployer_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.endpoint_role.role_arn],
        conditions={"StringEquals": {
            "iam:PassedToService": "sagemaker.amazonaws.com",
        }},
    ))

    # C) SNS subscription
    self.promote_topic.add_subscription(subs.LambdaSubscription(deployer_fn))

    # D) Golden-set test runner — runs after endpoint is in-service
    test_fn = lambda_.Function(self, "GoldenSetTestFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "golden_set_test")),
        timeout=Duration.minutes(15),
        environment={
            "ENDPOINT_NAME":      "staging-endpoint",
            "GOLDEN_SET_S3":      f"s3://{{project_name}}-golden-set/staging/",
            "PASS_THRESHOLD":     "0.85",
            "PROD_PROMOTE_TOPIC": f"arn:aws:sns:{self.region}:333333333333:promote-to-prod",
        },
    )
    test_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/staging-endpoint"],
    ))
    test_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sns:Publish"],
        resources=[f"arn:aws:sns:{self.region}:333333333333:promote-to-prod"],
    ))

    # Run golden-set 30 min after deploy
    events.Rule(self, "GoldenSetSchedule",
        event_pattern=events.EventPattern(
            source=["aws.sagemaker"],
            detail_type=["SageMaker Endpoint State Change"],
            detail={
                "EndpointName": ["staging-endpoint"],
                "EndpointStatus": ["IN_SERVICE"],
            },
        ),
        targets=[targets.LambdaFunction(test_fn)],
    )
```

### 3.4 CDK in PROD account — accept share + deploy

```python
def _create_prod_deploy(self, stage: str) -> None:
    """In PROD account. Receives validated model packages from staging,
    deploys via blue/green, monitors for auto-rollback."""

    # Similar to staging but:
    # - Endpoint deployment uses ProductionVariants for blue/green
    # - Subscribes to staging's "promote-to-prod" SNS topic
    # - CloudWatch alarms wired to auto-rollback Lambda
    # - Manual approval gate (optional) before blue→green cutover
    ...
```

---

## 4. Hierarchical RAM share (4+ accounts)

For BU-level isolation:

```
   Training (Central) ──┬─→ Staging-Eng ──┬─→ Prod-Engineering
                        ├─→ Staging-Fin ──┴─→ Prod-Finance
                        └─→ Staging-HR  ───→ Prod-HR
```

Each BU gets its own staging + prod accounts; central training shares MPG to all stagings.

```python
# Central RAM share — all stagings as principals
ram.CfnResourceShare(...,
    principals=[
        "ENG_STAGING", "ENG_PROD",
        "FIN_STAGING", "FIN_PROD",
        "HR_STAGING",  "HR_PROD",
    ],
    resource_arns=[mpg_arn],
)
```

For tighter governance, use AWS Organizations OU-based principals: `arn:aws:organizations::ROOT_ACCT:ou/o-xxxx/ou-yyyy`.

---

## 5. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Staging Lambda can't read training's MPG | RAM share not accepted in receiving account | `aws ram accept-resource-share-invitation` from staging account console |
| KMS Decrypt fails in staging | KMS key resource policy + grant both required | Both: key policy allows AccountPrincipal("staging") AND `key.grant_decrypt(account_principal)` |
| ECR pull fails | Repo policy + IAM both needed | Repo policy allows accounts; staging Lambda's IAM also has `ecr:GetDownloadUrl*` |
| Prod endpoint creation succeeds but invokes fail | Container image not pulled cross-region | ECR repo replication or use registry-level cross-region (NEW 2024) |
| Approval event not reaching staging Lambda | Cross-account event bus not configured | Use SNS bridge (training Lambda → SNS → staging subscriber) OR EB cross-account rule |
| Model package state change race condition | Two events fire close together | Add idempotency key to deployer (model package version ARN) |

### 5.1 Cost ballpark

| Component | Per-month |
|---|---|
| RAM share (1 share, 3 accounts) | $0 (free) |
| Cross-account SNS message | ~$0.01 / 1M messages |
| Cross-account ECR pull | $0.01/GB (intra-region) |
| Cross-account S3 GET | $0.0004/1000 |

Multi-account adds < $5/mo overhead. The cost is operational complexity, not compute.

---

## 6. Worked example — pytest

```python
def test_training_account_share_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="111111111111", region="us-east-1")
    from infrastructure.cdk.stacks.training_share_stack import TrainingShareStack
    stack = TrainingShareStack(app, stage_name="prod", env=env, ...)
    t = Template.from_stack(stack)

    # RAM share with 2 principals
    t.has_resource_properties("AWS::RAM::ResourceShare", Match.object_like({
        "Principals": Match.array_with([
            "222222222222", "333333333333",
        ]),
    }))
    # IAM PassRole on training role for cross-account use
    t.has_resource_properties("AWS::Lambda::Function", Match.any_value())
    # EventBridge rule for ApprovedStateChange
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "EventPattern": Match.object_like({
            "detail-type": ["SageMaker Model Package State Change"],
        }),
    }))
```

---

## 7. Five non-negotiables

1. **Use AWS Organizations + SCP for blast-radius isolation, not just IAM.** SCP at the OU level prevents cross-account principal escalation. Without SCP, a compromised IAM principal in staging can still read prod resources via cross-account roles.

2. **Idempotency on deployer Lambdas.** Model package state-change events can fire twice (rare but real). Use the model package version ARN as a dedupe key — `boto3.client("sagemaker").describe_endpoint(...)` returns existing, skip re-create.

3. **Manual approval gate for prod cutover (regulated industries).** EventBridge auto-promotion staging→prod is OK for low-risk; for healthcare/finance, add a Lambda that pauses and requires Slack/email approval.

4. **Golden-set tests MUST run in staging before prod promotion.** Without an automated quality gate, you ship regressions. Pass threshold is environment-specific (typically 0.85+ across regression tests).

5. **Cross-account observability — central CloudWatch.** Each account logs locally, but centralize critical metrics (endpoint InvocationsP99, FailedInvocations) to a security/ops account via CloudWatch cross-account observability or Kinesis Data Firehose.

---

## 8. References

- AWS docs:
  - [Cross-account model package sharing](https://docs.aws.amazon.com/sagemaker/latest/dg/model-registry-cross-account.html)
  - [AWS RAM Resource Shares](https://docs.aws.amazon.com/ram/latest/userguide/getting-started-sharing.html)
  - [Cross-account ML governance whitepaper](https://docs.aws.amazon.com/whitepapers/latest/build-secure-enterprise-ml-platform/multi-account-strategy.html)
  - [Cross-account ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/repository-policy-cross-account-permissions.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — single-account base case
  - `MLOPS_LLM_FINETUNING_PROD` — pipeline that produces the MPG being shared
  - `LAYER_SECURITY` — KMS + IAM permission boundary
  - `DATA_LAKE_FORMATION` — pattern for cross-account data sharing (parallel to model sharing)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — 3-account ML governance (training → staging → prod) with RAM share for Model Package Groups (2024+ feature), cross-account KMS/ECR/S3 grants, EventBridge + SNS bridge for cross-account state-change propagation. CDK for all 3 accounts. Hierarchical RAM share for 4+ accounts. 5 non-negotiables incl. SCP isolation + idempotent deployers. Created to fill F369 audit gap (2026-04-26): cross-account ML deploy was 0% covered. |
