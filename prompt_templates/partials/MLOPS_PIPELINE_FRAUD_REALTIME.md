# PARTIAL: Real-Time Fraud Detection Pipeline — Sub-100ms ML Scoring

**Usage:** Include when SOW mentions fraud detection, risk scoring, anomaly detection, real-time decision engine, transaction scoring, or credit risk.

---

## Architecture

```
OFFLINE (hourly Glue job):
  Transactions → Feature Engineering → Feature Store (offline + online)
  Aggregated features: tx_count_1h, spend_24h, new_merchant_flag, velocity signals

ONLINE (every transaction, <100ms):
  API → Fraud Scoring Lambda
         ├── Feature Store online lookup (<5ms)
         ├── SageMaker XGBoost endpoint (<20ms)
         └── Decision: APPROVE / REVIEW / DECLINE
               └── REVIEW/DECLINE → FIFO SQS → Case Management

RETRAINING (weekly):
  Labeled fraud cases → SageMaker Pipeline → XGBoost → Registry → Deploy
```

---

## CDK Code Block — Fraud Detection Infrastructure

```python
def _create_fraud_detection_pipeline(self, stage_name: str) -> None:
    """
    Real-Time Fraud Detection ML System.
    Sub-100ms end-to-end: Feature Store lookup + XGBoost + decision.

    Components:
      A) User Fraud Feature Group (online + offline Feature Store)
      B) Fraud Scoring Lambda (provisioned concurrency, no cold starts)
      C) Case Management FIFO Queue (flagged transactions)
      D) Feature Engineering Schedule (hourly Glue job)
      E) Latency and decision alarms
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # =========================================================================
    # A) USER FRAUD FEATURE GROUP
    # =========================================================================

    self.fraud_feature_group = sagemaker.CfnFeatureGroup(
        self, "UserFraudFeatures",
        feature_group_name=f"{{project_name}}-user-fraud-features-{stage_name}",
        record_identifier_feature_name="user_id",
        event_time_feature_name="event_time",
        feature_definitions=[
            {"featureName": "user_id",               "featureType": "String"},
            {"featureName": "event_time",            "featureType": "String"},
            {"featureName": "tx_count_1h",           "featureType": "Integral"},
            {"featureName": "tx_count_24h",          "featureType": "Integral"},
            {"featureName": "spend_1h_usd",          "featureType": "Fractional"},
            {"featureName": "spend_24h_usd",         "featureType": "Fractional"},
            {"featureName": "avg_spend_30d_usd",     "featureType": "Fractional"},
            {"featureName": "merchant_count_7d",     "featureType": "Integral"},
            {"featureName": "new_merchant_count_7d", "featureType": "Integral"},
            {"featureName": "intl_tx_count_30d",     "featureType": "Integral"},
            {"featureName": "declined_count_24h",    "featureType": "Integral"},
            {"featureName": "account_age_days",      "featureType": "Integral"},
            {"featureName": "fraud_score_90d_avg",   "featureType": "Fractional"},
        ],
        online_store_config={
            "EnableOnlineStore": True,
            "SecurityConfig": {"KmsKeyId": self.kms_key.key_arn},
            "TtlDuration": {"Unit": "Hours", "Value": "24"},
        },
        offline_store_config={
            "S3StorageConfig": {
                "S3Uri": f"s3://{self.lake_buckets['features'].bucket_name}/fraud-features/",
                "KmsKeyId": self.kms_key.key_arn,
            },
            "DisableGlueTableCreation": False,
        },
        role_arn=self.sagemaker_role.role_arn,
    )

    # =========================================================================
    # B) FRAUD SCORING LAMBDA — The real-time decision engine
    # =========================================================================

    fraud_scoring_fn = _lambda.Function(
        self, "FraudScoringFn",
        function_name=f"{{project_name}}-fraud-score-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/fraud_scoring"),
        # [Claude: generate src/fraud_scoring/index.py in Pass 3]
        environment={
            "FEATURE_GROUP_NAME": f"{{project_name}}-user-fraud-features-{stage_name}",
            "ENDPOINT_NAME":      f"{{project_name}}-fraud-inference-{stage_name}",
            "EVENT_BUS_NAME":     self.event_bus.event_bus_name if hasattr(self, 'event_bus') else "default",
            "DECLINE_THRESHOLD":  "0.85",
            "REVIEW_THRESHOLD":   "0.60",
        },
        memory_size=256,
        timeout=Duration.seconds(3),  # Must fit in API GW 29s limit — aim for <1s
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
    )

    # Provisioned concurrency alias — eliminates cold starts (critical for <100ms)
    fraud_alias = fraud_scoring_fn.add_alias(
        "live",
        provisioned_concurrent_executions=5 if stage_name == "prod" else 1,
    )

    # IAM permissions
    fraud_scoring_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:GetRecord", "sagemaker-featurestore-runtime:GetRecord"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:feature-group/*"],
    ))
    fraud_scoring_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-fraud-inference-{stage_name}"],
    ))

    # =========================================================================
    # C) CASE MANAGEMENT FIFO QUEUE
    # =========================================================================

    review_dlq = sqs.Queue(
        self, "FraudReviewDLQ",
        queue_name=f"{{project_name}}-fraud-review-dlq-{stage_name}.fifo",
        fifo=True,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=self.kms_key,
        retention_period=Duration.days(14),
        removal_policy=RemovalPolicy.DESTROY,
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
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.review_queue.grant_send_messages(fraud_scoring_fn)

    # =========================================================================
    # D) FEATURE ENGINEERING SCHEDULE (hourly)
    # =========================================================================

    feature_eng_fn = _lambda.Function(
        self, "FraudFeatureEngFn",
        function_name=f"{{project_name}}-fraud-feature-eng-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, logging
logger = logging.getLogger()
glue = boto3.client('glue')

def handler(event, context):
    resp = glue.start_job_run(
        JobName=os.environ['GLUE_JOB_NAME'],
        Arguments={'--feature_group': os.environ['FEATURE_GROUP'], '--lookback_hours': '25'},
    )
    logger.info(f"Glue job started: {resp['JobRunId']}")
    return {"job_run_id": resp["JobRunId"]}
"""),
        environment={
            "GLUE_JOB_NAME": f"{{project_name}}-fraud-feature-eng-{stage_name}",
            "FEATURE_GROUP":  f"{{project_name}}-user-fraud-features-{stage_name}",
        },
        timeout=Duration.seconds(30),
    )
    feature_eng_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["glue:StartJobRun"],
        resources=[f"arn:aws:glue:{self.region}:{self.account}:job/*"],
    ))
    events.Rule(self, "FraudFeatureEngSchedule",
        schedule=events.Schedule.rate(Duration.hours(1)),
        targets=[targets.LambdaFunction(feature_eng_fn)],
    )

    # =========================================================================
    # E) LATENCY ALARM (fraud scoring must be < 100ms p99)
    # =========================================================================

    cw.Alarm(
        self, "FraudLatencyAlarm",
        alarm_name=f"{{project_name}}-fraud-latency-{stage_name}",
        alarm_description="Fraud scoring p99 > 100ms — check Feature Store and endpoint performance",
        metric=fraud_scoring_fn.metric("Duration", statistic="p99", period=Duration.minutes(1)),
        threshold=100,
        evaluation_periods=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "FraudScoringFnArn",
        value=fraud_scoring_fn.function_arn,
        description="Fraud scoring Lambda — call this per transaction",
        export_name=f"{{project_name}}-fraud-scoring-{stage_name}",
    )
    CfnOutput(self, "FraudReviewQueueUrl",
        value=self.review_queue.queue_url,
        description="FIFO queue for flagged transactions awaiting human review",
        export_name=f"{{project_name}}-fraud-review-queue-{stage_name}",
    )
```

---

## Fraud Scoring Lambda (`src/fraud_scoring/index.py`)

```python
import boto3, os, json, time, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

smfs       = boto3.client('sagemaker-featurestore-runtime')
sm_runtime = boto3.client('sagemaker-runtime')
sqs        = boto3.client('sqs')

FEATURE_GROUP     = os.environ['FEATURE_GROUP_NAME']
ENDPOINT_NAME     = os.environ['ENDPOINT_NAME']
DECLINE_THRESHOLD = float(os.environ.get('DECLINE_THRESHOLD', '0.85'))
REVIEW_THRESHOLD  = float(os.environ.get('REVIEW_THRESHOLD', '0.60'))

def handler(event, context):
    t0 = time.time()
    tx         = json.loads(event.get('body', json.dumps(event)))
    user_id    = tx['user_id']
    amount_usd = float(tx['amount_usd'])

    # Step 1: Feature Store online lookup (<5ms)
    try:
        record = smfs.get_record(FeatureGroupName=FEATURE_GROUP, RecordIdentifierValue=user_id)
        feats  = {f['FeatureName']: f['ValueAsString'] for f in record.get('Record', [])}
    except Exception:
        feats = {}  # New user — use zeros (conservative)

    # Step 2: Build feature vector
    fv = [
        float(feats.get('tx_count_1h', 0)),
        float(feats.get('tx_count_24h', 0)),
        float(feats.get('spend_1h_usd', 0)),
        float(feats.get('spend_24h_usd', 0)),
        float(feats.get('avg_spend_30d_usd', 100)),
        float(feats.get('merchant_count_7d', 0)),
        float(feats.get('new_merchant_count_7d', 0)),
        float(feats.get('declined_count_24h', 0)),
        float(feats.get('account_age_days', 30)),
        amount_usd,
        1.0 if tx.get('is_international') else 0.0,
        amount_usd / max(float(feats.get('avg_spend_30d_usd', 100)), 1),  # Spend ratio
    ]

    # Step 3: XGBoost inference (<20ms)
    resp  = sm_runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType='text/csv',
        Body=','.join(map(str, fv)),
        Accept='application/json',
    )
    score = float(json.loads(resp['Body'].read())['predictions'][0])

    # Step 4: Decision
    if score >= DECLINE_THRESHOLD:   decision = 'DECLINE'
    elif score >= REVIEW_THRESHOLD:  decision = 'REVIEW'
    else:                            decision = 'APPROVE'

    result = {'transaction_id': tx['transaction_id'], 'decision': decision,
              'fraud_score': round(score, 4), 'latency_ms': round((time.time()-t0)*1000, 1)}
    logger.info(json.dumps(result))
    return {'statusCode': 200, 'body': json.dumps(result)}
```
