# SOP — MLOps Pipeline: Real-Time Fraud Detection (Sub-100 ms ML Scoring)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Feature Store (online + offline) · XGBoost endpoint · Lambda with provisioned concurrency · SQS FIFO · Glue for feature eng

---

## 1. Purpose

- Provision a sub-100 ms fraud-scoring path: Feature Store online lookup (<5 ms) → XGBoost endpoint (<20 ms) → APPROVE / REVIEW / DECLINE → flagged → FIFO SQS → case management.
- Codify the **User Fraud Feature Group** (online + offline, 24-hour TTL, KMS-encrypted) with a canonical 12-feature vector (velocity, new-merchant, intl, declined_24h, account age, spend ratio).
- Codify **provisioned concurrency** (≥ 5 prod, 1 non-prod) so the scoring Lambda never pays a cold start.
- Codify the **hourly Glue feature-engineering** kick-off Lambda + schedule; the Glue job itself is generated in `MLOPS_DATA_PLATFORM` / Pass 3.
- Include when the SOW mentions fraud detection, risk scoring, anomaly detection, real-time decision engine, transaction scoring, or credit-risk rules.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns Feature Group + endpoint + scoring Lambda + SQS + Glue schedule | **§3 Monolith Variant** |
| `MLPlatformStack` owns the Feature Group, `ServingStack` owns the endpoint, `FraudScoringStack` owns the Lambda + SQS + schedule | **§4 Micro-Stack Variant** |

**Why the split matters.** The scoring Lambda needs `featurestore-runtime:GetRecord` on the Feature Group (owned by `MLPlatformStack`) and `sagemaker:InvokeEndpoint` on the fraud endpoint (owned by `ServingStack`). Monolith: scoped resource ARNs are local. Cross-stack: identity-side grants with ARNs read from SSM. The SQS FIFO queue ideally lives in the `FraudScoringStack` (local) so the Lambda's `grant_send_messages` stays same-stack.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Architecture

```
OFFLINE (hourly Glue):
  Transactions → Feature Engineering → Feature Store (offline + online)
  Features: tx_count_1h/24h, spend_1h/24h, avg_spend_30d, merchant_count_7d,
            new_merchant_count_7d, intl_tx_count_30d, declined_count_24h, account_age_days

ONLINE (per transaction, < 100 ms):
  API → Fraud Scoring Lambda (provisioned concurrency)
         ├── Feature Store online lookup      (< 5 ms)
         ├── XGBoost SageMaker endpoint        (< 20 ms)
         └── Decision: APPROVE / REVIEW / DECLINE
               └── REVIEW/DECLINE → FIFO SQS → case management

RETRAINING (weekly):
  Labeled fraud cases → SageMaker Pipeline → XGBoost → Registry → MLOPS_SAGEMAKER_SERVING deployer
```

### 3.2 CDK — feature group, scoring Lambda + alias, FIFO queue, schedule, alarm

```python
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_sagemaker as sagemaker,
    aws_sqs as sqs,
)


def _create_fraud_detection_pipeline(self, stage_name: str) -> None:
    """Assumes self.{vpc, lambda_sg, kms_key, lake_buckets, sagemaker_role, alert_topic} set."""

    # A) Fraud feature group
    self.fraud_feature_group = sagemaker.CfnFeatureGroup(
        self, "UserFraudFeatures",
        feature_group_name=f"{{project_name}}-user-fraud-features-{stage_name}",
        record_identifier_feature_name="user_id",
        event_time_feature_name="event_time",
        feature_definitions=[
            sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(feature_name=n, feature_type=t)
            for n, t in [
                ("user_id",              "String"),
                ("event_time",           "String"),
                ("tx_count_1h",          "Integral"),
                ("tx_count_24h",         "Integral"),
                ("spend_1h_usd",         "Fractional"),
                ("spend_24h_usd",        "Fractional"),
                ("avg_spend_30d_usd",    "Fractional"),
                ("merchant_count_7d",    "Integral"),
                ("new_merchant_count_7d", "Integral"),
                ("intl_tx_count_30d",    "Integral"),
                ("declined_count_24h",   "Integral"),
                ("account_age_days",     "Integral"),
                ("fraud_score_90d_avg",  "Fractional"),
            ]
        ],
        online_store_config=sagemaker.CfnFeatureGroup.OnlineStoreConfigProperty(
            enable_online_store=True,
            security_config=sagemaker.CfnFeatureGroup.OnlineStoreSecurityConfigProperty(
                kms_key_id=self.kms_key.key_arn,
            ),
            ttl_duration=sagemaker.CfnFeatureGroup.TtlDurationProperty(unit="Hours", value=24),
        ),
        offline_store_config=sagemaker.CfnFeatureGroup.OfflineStoreConfigProperty(
            s3_storage_config=sagemaker.CfnFeatureGroup.S3StorageConfigProperty(
                s3_uri=f"s3://{self.lake_buckets['features'].bucket_name}/fraud-features/",
                kms_key_id=self.kms_key.key_arn,
            ),
            disable_glue_table_creation=False,
        ),
        role_arn=self.sagemaker_role.role_arn,
    )

    # B) Scoring Lambda with provisioned concurrency
    fraud_scoring_fn = _lambda.Function(
        self, "FraudScoringFn",
        function_name=f"{{project_name}}-fraud-score-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/fraud_scoring"),
        memory_size=256,
        timeout=Duration.seconds(3),
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
        environment={
            "FEATURE_GROUP_NAME": self.fraud_feature_group.feature_group_name,
            "ENDPOINT_NAME":      f"{{project_name}}-fraud-inference-{stage_name}",
            "DECLINE_THRESHOLD":  "0.85",
            "REVIEW_THRESHOLD":   "0.60",
        },
    )
    fraud_scoring_fn.add_alias(
        "live",
        provisioned_concurrent_executions=5 if stage_name == "prod" else 1,
    )
    fraud_scoring_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker-featurestore-runtime:GetRecord"],
        resources=[self.fraud_feature_group.attr_feature_group_arn],
    ))
    fraud_scoring_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{{project_name}}-fraud-inference-{stage_name}"],
    ))

    # C) FIFO case-review queue (with DLQ)
    review_dlq = sqs.Queue(
        self, "FraudReviewDLQ",
        queue_name=f"{{project_name}}-fraud-review-dlq-{stage_name}.fifo",
        fifo=True,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
    )
    self.review_queue = sqs.Queue(
        self, "FraudReviewQueue",
        queue_name=f"{{project_name}}-fraud-review-{stage_name}.fifo",
        fifo=True,
        content_based_deduplication=True,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        visibility_timeout=Duration.minutes(30),
        retention_period=Duration.days(7),
        dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=review_dlq),
    )
    self.review_queue.grant_send_messages(fraud_scoring_fn)    # same-stack L2

    # D) Hourly feature-engineering Glue kick-off
    feature_eng_fn = _lambda.Function(
        self, "FraudFeatureEngFn",
        function_name=f"{{project_name}}-fraud-feature-eng-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/fraud_feature_eng"),
        timeout=Duration.seconds(30),
        environment={
            "GLUE_JOB_NAME": f"{{project_name}}-fraud-feature-eng-{stage_name}",
            "FEATURE_GROUP": self.fraud_feature_group.feature_group_name,
        },
    )
    feature_eng_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["glue:StartJobRun"],
        resources=[f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:job/{{project_name}}-fraud-feature-eng-{stage_name}"],
    ))
    events.Rule(
        self, "FraudFeatureEngSchedule",
        rule_name=f"{{project_name}}-fraud-feature-eng-{stage_name}",
        schedule=events.Schedule.rate(Duration.hours(1)),
        targets=[targets.LambdaFunction(feature_eng_fn)],
    )

    # E) Latency alarm (p99 < 100 ms)
    cw.Alarm(
        self, "FraudLatencyAlarm",
        alarm_name=f"{{project_name}}-fraud-latency-{stage_name}",
        metric=fraud_scoring_fn.metric("Duration", statistic="p99", period=Duration.minutes(1)),
        threshold=100,
        evaluation_periods=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    )

    CfnOutput(self, "FraudScoringFnArn", value=fraud_scoring_fn.function_arn)
    CfnOutput(self, "FraudReviewQueueUrl", value=self.review_queue.queue_url)
```

### 3.3 Scoring handler (`src/fraud_scoring/index.py`)

```python
"""< 100 ms fraud scoring: Feature Store lookup → XGBoost → decision."""
import boto3, json, logging, os, time

logger     = logging.getLogger(); logger.setLevel(logging.INFO)
smfs       = boto3.client('sagemaker-featurestore-runtime')
sm_runtime = boto3.client('sagemaker-runtime')

FEATURE_GROUP     = os.environ['FEATURE_GROUP_NAME']
ENDPOINT_NAME     = os.environ['ENDPOINT_NAME']
DECLINE_THRESHOLD = float(os.environ.get('DECLINE_THRESHOLD', '0.85'))
REVIEW_THRESHOLD  = float(os.environ.get('REVIEW_THRESHOLD',  '0.60'))


def handler(event, context):
    t0         = time.time()
    tx         = json.loads(event.get('body', json.dumps(event)))
    user_id    = tx['user_id']
    amount_usd = float(tx['amount_usd'])

    # 1) Feature Store online lookup
    try:
        record = smfs.get_record(FeatureGroupName=FEATURE_GROUP, RecordIdentifierValue=user_id)
        feats  = {f['FeatureName']: f['ValueAsString'] for f in record.get('Record', [])}
    except Exception:
        feats = {}                                # new user → conservative zeros

    # 2) Feature vector (12 values)
    fv = [
        float(feats.get('tx_count_1h',          0)),
        float(feats.get('tx_count_24h',         0)),
        float(feats.get('spend_1h_usd',         0)),
        float(feats.get('spend_24h_usd',        0)),
        float(feats.get('avg_spend_30d_usd',    100)),
        float(feats.get('merchant_count_7d',    0)),
        float(feats.get('new_merchant_count_7d', 0)),
        float(feats.get('declined_count_24h',   0)),
        float(feats.get('account_age_days',     30)),
        amount_usd,
        1.0 if tx.get('is_international') else 0.0,
        amount_usd / max(float(feats.get('avg_spend_30d_usd', 100)), 1),    # spend ratio
    ]

    # 3) XGBoost inference
    resp  = sm_runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType='text/csv',
        Body=','.join(map(str, fv)),
        Accept='application/json',
    )
    score = float(json.loads(resp['Body'].read())['predictions'][0])

    # 4) Decision
    decision = (
        'DECLINE' if score >= DECLINE_THRESHOLD else
        'REVIEW'  if score >= REVIEW_THRESHOLD  else
        'APPROVE'
    )

    result = {
        'transaction_id': tx['transaction_id'],
        'decision':       decision,
        'fraud_score':    round(score, 4),
        'latency_ms':     round((time.time() - t0) * 1000, 1),
    }
    logger.info(json.dumps(result))
    return {'statusCode': 200, 'body': json.dumps(result)}
```

### 3.4 Monolith gotchas

- **Provisioned concurrency costs money 24/7.** Budget `5 executions × $0.000004167 × seconds` ≈ $54/month per region in prod. Worth it for SLA-bound scoring.
- **`sagemaker-featurestore-runtime:GetRecord`** is the correct action — not `sagemaker:GetRecord`. The latter exists but is for batch/control plane.
- **Spend ratio `amount_usd / avg_spend_30d_usd`** protects against divide-by-zero via `max(..., 1)`. Small default (100) keeps the ratio meaningful for new users.
- **XGBoost `ContentType='text/csv'`** is the built-in container's expected format; for custom containers adapt to their contract.
- **TTL 24 hours** on online store means stale users fall back to offline. Acceptable; rare user scores are typically lower risk.
- **Lambda timeout 3 s** is hard ceiling; the 100-ms alarm fires long before timeout but protect against tail blowups.

---

## 4. Micro-Stack Variant

**Use when:** `FraudScoringStack` is separate from `MLPlatformStack` (owns feature group) and `ServingStack` (owns endpoint).

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `feature_group.grant(fn)`** or `endpoint.grant_invoke(fn)` across stacks — identity-side with ARNs read from SSM.
3. **Never target cross-stack queues** — SQS FIFO here is owned locally.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_master_key=ext_key`** on the SQS queue — use a local CMK or `SQS_MANAGED`.

### 4.2 `FraudScoringStack`

```python
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class FraudScoringStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        feature_group_arn_ssm: str,
        endpoint_name_ssm: str,
        glue_feature_eng_job_name_ssm: str,
        alert_topic_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-fraud-scoring-{stage_name}", **kwargs)

        feature_group_arn   = ssm.StringParameter.value_for_string_parameter(self, feature_group_arn_ssm)
        endpoint_name       = ssm.StringParameter.value_for_string_parameter(self, endpoint_name_ssm)
        glue_job_name       = ssm.StringParameter.value_for_string_parameter(self, glue_feature_eng_job_name_ssm)
        alert_topic_arn     = ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm)

        # Local CMK for SQS
        cmk = kms.Key(self, "FraudKey",
            alias=f"alias/{{project_name}}-fraud-{stage_name}",
            enable_key_rotation=True, rotation_period=Duration.days(365),
        )

        # Scoring Lambda
        log = logs.LogGroup(self, "ScoringLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-fraud-score-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        scoring_fn = _lambda.Function(self, "FraudScoringFn",
            function_name=f"{{project_name}}-fraud-score-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "fraud_scoring")),
            memory_size=256,
            timeout=Duration.seconds(3),
            log_group=log,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            environment={
                "FEATURE_GROUP_NAME": feature_group_arn.split('/')[-1],  # name from ARN
                "ENDPOINT_NAME":      endpoint_name,
                "DECLINE_THRESHOLD":  "0.85",
                "REVIEW_THRESHOLD":   "0.60",
            },
        )
        scoring_fn.add_alias("live",
            provisioned_concurrent_executions=5 if stage_name == "prod" else 1,
        )
        scoring_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker-featurestore-runtime:GetRecord"],
            resources=[feature_group_arn],
        ))
        scoring_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"],
        ))
        iam.PermissionsBoundary.of(scoring_fn.role).apply(permission_boundary)

        # FIFO case queue with local CMK
        review_dlq = sqs.Queue(self, "FraudReviewDLQ",
            queue_name=f"{{project_name}}-fraud-review-dlq-{stage_name}.fifo",
            fifo=True,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            retention_period=Duration.days(14),
        )
        self.review_queue = sqs.Queue(self, "FraudReviewQueue",
            queue_name=f"{{project_name}}-fraud-review-{stage_name}.fifo",
            fifo=True,
            content_based_deduplication=True,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=cmk,
            visibility_timeout=Duration.minutes(30),
            retention_period=Duration.days(7),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=review_dlq),
        )
        self.review_queue.grant_send_messages(scoring_fn)       # same-stack safe

        # Feature-eng kickoff
        fe_log = logs.LogGroup(self, "FeatureEngLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-fraud-feature-eng-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        feature_eng_fn = _lambda.Function(self, "FraudFeatureEngFn",
            function_name=f"{{project_name}}-fraud-feature-eng-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "fraud_feature_eng")),
            timeout=Duration.seconds(30),
            log_group=fe_log,
            environment={
                "GLUE_JOB_NAME": glue_job_name,
                "FEATURE_GROUP": feature_group_arn.split('/')[-1],
            },
        )
        feature_eng_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:StartJobRun"],
            resources=[f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:job/{glue_job_name}"],
        ))
        iam.PermissionsBoundary.of(feature_eng_fn.role).apply(permission_boundary)

        events.Rule(self, "FraudFeatureEngSchedule",
            rule_name=f"{{project_name}}-fraud-feature-eng-{stage_name}",
            schedule=events.Schedule.rate(Duration.hours(1)),
            targets=[targets.LambdaFunction(feature_eng_fn)],
        )

        # Latency alarm
        cw.Alarm(self, "FraudLatencyAlarm",
            alarm_name=f"{{project_name}}-fraud-latency-{stage_name}",
            metric=scoring_fn.metric("Duration", statistic="p99", period=Duration.minutes(1)),
            threshold=100, evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(
                sns.Topic.from_topic_arn(self, "AlertTopic", alert_topic_arn),
            )],
        )

        cdk.CfnOutput(self, "FraudScoringFnArn", value=scoring_fn.function_arn)
```

### 4.3 Micro-stack gotchas

- **`feature_group_arn.split('/')[-1]`** recovers the feature-group name from the ARN for the `FeatureGroupName` kwarg. Tokens split correctly at deploy time.
- **Scoped `sagemaker-featurestore-runtime:GetRecord` resource** is the full feature-group ARN — don't use `*`.
- **Local CMK for SQS** avoids importing a cross-stack key. If HIPAA demands a specific CMK, publish its ARN via SSM and declare an `IKey` via `kms.Key.from_key_arn` — but the queue `encryption_master_key=` accepts only an `IKey`, not an ARN string, so the cross-stack pattern still requires identity-side `kms:Decrypt` grants on consumers.
- **`grant_send_messages` from Lambda to the local queue** is fine — both in `FraudScoringStack`.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx | §4 Micro-Stack |
| Add feature (e.g. device fingerprint) | Append to feature-group definitions + update Glue job + scoring Lambda vector |
| Shift to lighter-weight model (logistic regression) | Swap endpoint name via SSM; scoring Lambda unchanged |
| Streaming source (Kafka / Kinesis) | Add Kinesis → Lambda trigger instead of API GW → Lambda |
| Very high QPS (>10k rps) | Move scoring into a Fargate service with local feature cache; endpoint stays the same |
| Custom scoring container (multi-model) | Swap endpoint to MME; scoring Lambda forwards `TargetModel` header |

---

## 6. Worked example — FraudScoringStack synthesizes

Save as `tests/sop/test_MLOPS_PIPELINE_FRAUD_REALTIME.py`. Offline.

```python
"""SOP verification — FraudScoringStack synthesizes scoring + SQS + schedule + alarm."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_fraud_scoring_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg   = ec2.SecurityGroup(deps, "Sg", vpc=vpc)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.fraud_scoring_stack import FraudScoringStack
    stack = FraudScoringStack(
        app, stage_name="prod",
        vpc=vpc, lambda_sg=sg,
        feature_group_arn_ssm="/test/ml/fraud_feature_group_arn",
        endpoint_name_ssm="/test/ml/fraud_endpoint_name",
        glue_feature_eng_job_name_ssm="/test/ml/fraud_glue_job_name",
        alert_topic_arn_ssm="/test/obs/alert_topic_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 2)
    t.resource_count_is("AWS::SQS::Queue",       2)  # queue + dlq
    t.resource_count_is("AWS::Events::Rule",     1)
    t.resource_count_is("AWS::CloudWatch::Alarm", 1)
```

---

## 7. References

- `docs/template_params.md` — `FRAUD_FEATURE_GROUP_ARN_SSM`, `FRAUD_ENDPOINT_NAME_SSM`, `FRAUD_DECLINE_THRESHOLD`, `FRAUD_REVIEW_THRESHOLD`, `FRAUD_PROVISIONED_CONCURRENCY`
- `docs/Feature_Roadmap.md` — feature IDs `ML-35` (fraud pipeline), `ML-36` (feature store online), `ML-37` (provisioned concurrency)
- SageMaker Feature Store runtime: https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_featurestore-runtime_GetRecord.html
- Related SOPs: `MLOPS_DATA_PLATFORM` (Glue feature-eng job), `MLOPS_SAGEMAKER_SERVING` (endpoint deployer), `MLOPS_SAGEMAKER_TRAINING` (XGBoost training), `EVENT_DRIVEN_PATTERNS` (SQS FIFO + DLQ), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `FraudScoringStack` reads feature-group ARN + endpoint name + Glue job name + alert topic via SSM; identity-side grants on `sagemaker-featurestore-runtime:GetRecord`, scoped `sagemaker:InvokeEndpoint`, scoped `glue:StartJobRun`; local CMK avoids cross-stack KMS (5th non-negotiable). Extracted handler assets. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — fraud feature group, scoring Lambda, FIFO queue, feature-eng schedule, latency alarm. |
