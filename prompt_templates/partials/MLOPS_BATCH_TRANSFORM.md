# SOP — MLOps SageMaker Batch Transform (Offline Scoring at Scale)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Batch Transform · `aws_lambda` Python 3.13 · S3 → Lambda via EventBridge (decoupled) · DynamoDB + SNS

---

## 1. Purpose

- Provision infra for nightly / weekly offline scoring — millions of records in parallel at $0 idle cost.
- Codify the three-stage pipeline:
  1. **Trigger Lambda** starts a `CreateTransformJob` with `BatchStrategy=MultiRecord`.
  2. **SageMaker Batch Transform** scores all records on N parallel instances.
  3. **Post-processing Lambda** fires on S3 `OBJECT_CREATED` → EventBridge → formats results, writes to DDB, fan-outs SNS alerts on high-score entities.
- Codify `DataProcessing` with `JoinSource=Input` so output rows carry the original features for audit / reporting.
- Codify the S3 → EventBridge → Lambda pattern (no direct S3 → Lambda notification) so the scoring bucket stays cross-stack-portable.
- Include when the SOW mentions offline scoring, nightly predictions, scoring millions of records, reports generation, or any inference without realtime SLA.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns lake buckets + DDB results table + SNS topic + trigger Lambda + post-process Lambda | **§3 Monolith Variant** |
| Buckets in `DataLakeStack`, DDB in `AppStack`, SNS in `ObservabilityStack`, trigger+post-process in `ScoringStack` | **§4 Micro-Stack Variant** |

**Why the split matters.** Post-processing Lambda needs `s3:GetObject` on the curated bucket, `dynamodb:PutItem` on results table, `sns:Publish` on alert topic. In monolith: `bucket.grant_read(fn)`, `table.grant_write_data(fn)`, `topic.grant_publish(fn)` are all local L2 grants. Across stacks, each of those mutates a resource policy in another stack → cycle. Micro-stack uses identity-side grants with SSM-published names/ARNs, and S3 → Lambda notification is **replaced by S3 → EventBridge → Lambda** via `CfnRule` (third non-negotiable: no cross-stack `s3_notifications.LambdaDestination`).

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 When Batch vs Real-time?

| Use Batch Transform                            | Use Real-time Endpoint           |
| ---------------------------------------------- | -------------------------------- |
| Score 10M customers overnight                  | Fraud check on each transaction  |
| Monthly churn prediction report                | Recommendation on page load      |
| Weekly risk scoring for all accounts           | Chatbot response                 |
| Nightly demand forecast                        | Real-time pricing                |
| Cost: **$0.19/hr while running, $0 when idle** | Cost: **$0.28/hr 24/7**          |

### 3.2 CDK — trigger Lambda + schedule

```python
from aws_cdk import (
    Duration, CfnOutput,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
)


def _create_batch_transform(self, stage_name: str) -> None:
    """Assumes self.{lake_buckets, kms_key, sagemaker_role, ddb_tables,
    alert_topic, lambda_functions} created earlier."""

    batch_trigger_fn = _lambda.Function(
        self, "BatchTransformTrigger",
        function_name=f"{{project_name}}-batch-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/batch_trigger"),
        timeout=Duration.minutes(5),
        environment={
            "MODEL_NAME":     f"{{project_name}}-model-{stage_name}",
            "INPUT_S3_URI":   f"s3://{self.lake_buckets['processed'].bucket_name}/batch-input/",
            "OUTPUT_S3_URI":  f"s3://{self.lake_buckets['curated'].bucket_name}/batch-output/",
            "KMS_KEY_ID":     self.kms_key.key_arn,
            "INSTANCE_TYPE":  "ml.m5.xlarge"  if stage_name != "prod" else "ml.m5.4xlarge",
            "INSTANCE_COUNT": "1"             if stage_name != "prod" else "4",
            "STAGE":          stage_name,
        },
    )
    batch_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:transform-job/{{project_name}}*"],
    ))
    batch_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.sagemaker_role.role_arn],
        conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
    ))

    events.Rule(
        self, "NightlyBatchScore",
        rule_name=f"{{project_name}}-nightly-batch-{stage_name}",
        schedule=events.Schedule.cron(hour="1", minute="0"),
        targets=[targets.LambdaFunction(batch_trigger_fn)],
        enabled=stage_name != "ds",
    )
    self.lambda_functions["BatchTrigger"] = batch_trigger_fn
```

### 3.3 Trigger handler (`lambda/batch_trigger/index.py`)

```python
"""Start a SageMaker Batch Transform job. Pack multiple records per request for throughput."""
import boto3, logging, os
from datetime import datetime

logger = logging.getLogger()
sm     = boto3.client('sagemaker')


def handler(event, context):
    run_id     = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    model_name = event.get('model_name', os.environ['MODEL_NAME'])
    input_uri  = event.get('input_uri',  os.environ['INPUT_S3_URI'])
    output_uri = f"{os.environ['OUTPUT_S3_URI']}{run_id}/"

    resp = sm.create_transform_job(
        TransformJobName=f"{{project_name}}-batch-{run_id}",
        ModelName=model_name,
        BatchStrategy='MultiRecord',            # pack records → throughput
        MaxPayloadInMB=6,
        MaxConcurrentTransforms=0,              # 0 = auto
        TransformInput={
            'DataSource':  {'S3DataSource': {'S3DataType': 'S3Prefix', 'S3Uri': input_uri}},
            'ContentType': 'text/csv',
            'SplitType':   'Line',
        },
        TransformOutput={
            'S3OutputPath': output_uri,
            'Accept':       'application/json',
            'AssembleWith': 'Line',
            'KmsKeyId':     os.environ['KMS_KEY_ID'],
        },
        TransformResources={
            'InstanceType':  os.environ.get('INSTANCE_TYPE',  'ml.m5.2xlarge'),
            'InstanceCount': int(os.environ.get('INSTANCE_COUNT', '2')),
        },
        DataProcessing={
            'InputFilter':  '$',                    # pass-through
            'OutputFilter': '$.SageMakerOutput',    # extract prediction
            'JoinSource':   'Input',                # append input cols for context
        },
        Tags=[
            {'Key': 'Project',     'Value': '{project_name}'},
            {'Key': 'Environment', 'Value': os.environ['STAGE']},
            {'Key': 'Trigger',     'Value': 'scheduled' if not event.get('manual') else 'manual'},
        ],
    )
    logger.info("Batch job started: %s, output: %s", resp['TransformJobArn'], output_uri)
    return {
        "job_name":   f"{{project_name}}-batch-{run_id}",
        "job_arn":    resp['TransformJobArn'],
        "output_uri": output_uri,
    }
```

### 3.4 Post-processing Lambda (S3 → EventBridge → Lambda)

```python
from aws_cdk import aws_events as events, aws_events_targets as targets, Duration


def _create_post_process(self, stage_name: str, batch_trigger_fn) -> None:
    post_process_fn = _lambda.Function(
        self, "BatchPostProcessFn",
        function_name=f"{{project_name}}-batch-postprocess-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/batch_postprocess"),
        timeout=Duration.minutes(15),
        memory_size=512,
        environment={
            "RESULTS_TABLE":   list(self.ddb_tables.values())[0].table_name,
            "ALERT_TOPIC_ARN": self.alert_topic.topic_arn,
            "ALERT_THRESHOLD": "0.90",
        },
    )
    self.lake_buckets["curated"].grant_read(post_process_fn)
    list(self.ddb_tables.values())[0].grant_write_data(post_process_fn)
    self.alert_topic.grant_publish(post_process_fn)

    # S3 → EventBridge → Lambda (decoupled). Bucket must have EventBridge notifications enabled.
    events.Rule(
        self, "BatchOutputArrivedRule",
        rule_name=f"{{project_name}}-batch-output-arrived-{stage_name}",
        event_pattern=events.EventPattern(
            source=["aws.s3"],
            detail_type=["Object Created"],
            detail={
                "bucket": {"name": [self.lake_buckets["curated"].bucket_name]},
                "object": {"key":  [{"prefix": "batch-output/"}, {"suffix": ".out"}]},
            },
        ),
        targets=[targets.LambdaFunction(post_process_fn)],
    )
```

Post-process handler (`lambda/batch_postprocess/index.py`):

```python
"""Parse Batch Transform JSONL output; write to DDB; alert on high scores."""
import boto3, json, logging, os
logger = logging.getLogger(); logger.setLevel(logging.INFO)

s3  = boto3.client('s3')
ddb = boto3.resource('dynamodb').Table(os.environ['RESULTS_TABLE'])
sns = boto3.client('sns')
HIGH_SCORE_THRESHOLD = float(os.environ.get('ALERT_THRESHOLD', '0.90'))


def handler(event, context):
    # EventBridge "Object Created" shape — detail.bucket.name / detail.object.key
    bucket = event['detail']['bucket']['name']
    key    = event['detail']['object']['key']

    body = s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')

    items, alerts = [], []
    for line in body.strip().split('\n'):
        if not line.strip():
            continue
        result    = json.loads(line)
        entity_id = result.get('id', result.get('user_id', 'unknown'))
        score     = float(result.get('score', result.get('prediction', 0)))
        items.append({'entity_id': entity_id, 'score': score, 'details': result})
        if score >= HIGH_SCORE_THRESHOLD:
            alerts.append({'id': entity_id, 'score': score})

    with ddb.batch_writer() as bw:
        for it in items:
            bw.put_item(Item={
                'entity_id': it['entity_id'],
                'score':     str(it['score']),
                'details':   json.dumps(it['details']),
                'batch_key': key,
            })

    if alerts:
        sns.publish(
            TopicArn=os.environ['ALERT_TOPIC_ARN'],
            Subject=f"Batch scoring: {len(alerts)} high-score entities",
            Message=json.dumps({'high_risk': alerts[:100], 'total': len(alerts)}),
        )

    logger.info("Processed %d records, %d alerts", len(items), len(alerts))
    return {"processed": len(items)}
```

### 3.5 Failure alarm

```python
from aws_cdk import aws_cloudwatch as cw, aws_cloudwatch_actions as cw_actions

cw.Alarm(
    self, "BatchJobFailedAlarm",
    alarm_name=f"{{project_name}}-batch-failed-{stage_name}",
    metric=cw.Metric(
        namespace="AWS/SageMaker",
        metric_name="transform:failed",
        statistic="Sum",
        period=Duration.hours(4),
    ),
    threshold=1,
    evaluation_periods=1,
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
)
```

### 3.6 Monolith gotchas

- **`MaxConcurrentTransforms=0`** means "auto" — SageMaker sizes based on model container. Don't set to 1 unless your model is single-threaded.
- **`AssembleWith='Line'`** + `Accept='application/json'` produces newline-delimited JSON (JSONL) — the post-processor parses line-by-line. If the model returns a JSON array, switch to `AssembleWith='None'` and parse once.
- **`DataProcessing.InputFilter`** must be a valid JMESPath. `$` is "pass-through"; complex filters silently drop rows that don't match.
- **S3 → Lambda direct notification** (`bucket.add_event_notification`) couples the bucket to the Lambda. Use `S3 → EventBridge → Lambda` (as above) to decouple — required for micro-stack.
- **`iam:PassRole` must use the `iam:PassedToService` Condition** — otherwise the trigger role could pass the SageMaker role to *anything*.
- **`transform:failed` metric** is nearly always missing data — `NOT_BREACHING` is mandatory.

---

## 4. Micro-Stack Variant

**Use when:** buckets / DDB / SNS / scoring Lambdas are in separate stacks.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `bucket.grant_read(fn)`** across stacks — identity-side `PolicyStatement` on Lambda role.
3. **Never use `s3_notifications.LambdaDestination`** across stacks; use **S3 → EventBridge → Lambda** with `CfnRule` (or a same-stack `events.Rule` if the consumer Lambda is local).
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on Transform output — pass `KmsKeyId` as an ARN string via env.

### 4.2 `ScoringStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_ssm as ssm,
    aws_sns as sns,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class ScoringStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        lake_bucket_processed_ssm: str,
        lake_bucket_curated_ssm: str,
        lake_key_arn_ssm: str,
        sagemaker_role_arn_ssm: str,
        results_table_name_ssm: str,
        alert_topic_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-scoring-{stage_name}", **kwargs)

        processed_bucket    = ssm.StringParameter.value_for_string_parameter(self, lake_bucket_processed_ssm)
        curated_bucket      = ssm.StringParameter.value_for_string_parameter(self, lake_bucket_curated_ssm)
        lake_key_arn        = ssm.StringParameter.value_for_string_parameter(self, lake_key_arn_ssm)
        sagemaker_role_arn  = ssm.StringParameter.value_for_string_parameter(self, sagemaker_role_arn_ssm)
        results_table_name  = ssm.StringParameter.value_for_string_parameter(self, results_table_name_ssm)
        alert_topic_arn     = ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm)

        # ── Trigger Lambda ────────────────────────────────────────────
        trigger_log = logs.LogGroup(self, "TriggerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-batch-trigger-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        batch_trigger = _lambda.Function(self, "BatchTriggerFn",
            function_name=f"{{project_name}}-batch-trigger-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "batch_trigger")),
            timeout=Duration.minutes(5),
            log_group=trigger_log,
            environment={
                "MODEL_NAME":     f"{{project_name}}-model-{stage_name}",
                "INPUT_S3_URI":   f"s3://{processed_bucket}/batch-input/",
                "OUTPUT_S3_URI":  f"s3://{curated_bucket}/batch-output/",
                "KMS_KEY_ID":     lake_key_arn,             # STRING (5th non-negotiable)
                "INSTANCE_TYPE":  "ml.m5.xlarge"  if stage_name != "prod" else "ml.m5.4xlarge",
                "INSTANCE_COUNT": "1"             if stage_name != "prod" else "4",
                "STAGE":          stage_name,
            },
        )
        batch_trigger.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:transform-job/{{project_name}}*"],
        ))
        batch_trigger.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[sagemaker_role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))
        iam.PermissionsBoundary.of(batch_trigger.role).apply(permission_boundary)

        events.Rule(self, "NightlyBatchScore",
            rule_name=f"{{project_name}}-nightly-batch-{stage_name}",
            schedule=events.Schedule.cron(hour="1", minute="0"),
            targets=[targets.LambdaFunction(batch_trigger)],
            enabled=stage_name != "ds",
        )

        # ── Post-process Lambda ──────────────────────────────────────
        pp_log = logs.LogGroup(self, "PostProcessLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-batch-postprocess-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        post_process = _lambda.Function(self, "BatchPostProcessFn",
            function_name=f"{{project_name}}-batch-postprocess-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "batch_postprocess")),
            timeout=Duration.minutes(15),
            memory_size=512,
            log_group=pp_log,
            environment={
                "RESULTS_TABLE":   results_table_name,
                "ALERT_TOPIC_ARN": alert_topic_arn,
                "ALERT_THRESHOLD": "0.90",
            },
        )
        # Identity-side grants
        post_process.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{curated_bucket}/batch-output/*"],
        ))
        post_process.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:BatchWriteItem", "dynamodb:PutItem"],
            resources=[f"arn:aws:dynamodb:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/{results_table_name}"],
        ))
        post_process.add_to_role_policy(iam.PolicyStatement(
            actions=["sns:Publish"],
            resources=[alert_topic_arn],
        ))
        post_process.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[lake_key_arn],
        ))
        iam.PermissionsBoundary.of(post_process.role).apply(permission_boundary)

        # S3 → EventBridge → Lambda (cross-stack safe)
        events.Rule(self, "BatchOutputArrivedRule",
            rule_name=f"{{project_name}}-batch-output-arrived-{stage_name}",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [curated_bucket]},
                    "object": {"key":  [{"prefix": "batch-output/"}, {"suffix": ".out"}]},
                },
            ),
            targets=[targets.LambdaFunction(post_process)],
        )

        # Failure alarm
        cw.Alarm(self, "BatchJobFailedAlarm",
            alarm_name=f"{{project_name}}-batch-failed-{stage_name}",
            metric=cw.Metric(
                namespace="AWS/SageMaker",
                metric_name="transform:failed",
                statistic="Sum",
                period=Duration.hours(4),
            ),
            threshold=1, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(
                sns.Topic.from_topic_arn(self, "AlertTopic", alert_topic_arn),
            )],
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
```

### 4.3 Micro-stack gotchas

- **Curated bucket must have EventBridge notifications enabled.** Set `event_bridge_enabled=True` on the bucket in the data-lake stack, OR call `aws s3api put-bucket-notification-configuration` once post-deploy.
- **`sns.Topic.from_topic_arn`** resolves the cross-stack topic into an `ITopic` for `SnsAction`. It's a one-liner — use it instead of trying to import the whole stack.
- **`KmsKeyId` as string** — CreateTransformJob accepts an ARN string in the `TransformOutput.KmsKeyId`; this is the correct cross-stack shape.
- **Results DDB identity-side grant** — must include `BatchWriteItem` because the post-processor uses `batch_writer()`.
- **Post-process Lambda in `ScoringStack`** is the right owner — it's the closest to the Batch job. Moving it elsewhere adds SSM indirection for the DDB/SNS names.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack |
| Input data in Redshift / Aurora, not S3 | Unload to S3 in a prior Step Function step; Batch Transform only reads from S3 |
| Results fanout to Kafka / MSK | Add second post-process consumer or replace DDB target with Kinesis Firehose |
| Very large model (> 6 MB request) | Increase `MaxPayloadInMB`; or switch to Async Inference endpoint |
| Scheduled, NOT event-driven input | Keep the nightly rule; disable `BatchOutputArrivedRule` and consume from Athena |
| Multi-region batch | One `ScoringStack` per region; each triggers local model in its region |

---

## 6. Worked example — ScoringStack synthesizes

Save as `tests/sop/test_MLOPS_BATCH_TRANSFORM.py`. Offline.

```python
"""SOP verification — ScoringStack synthesizes trigger + post-process + rules + alarm."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_scoring_stack():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.scoring_stack import ScoringStack
    stack = ScoringStack(
        app, stage_name="staging",
        lake_bucket_processed_ssm="/test/lake/processed_bucket",
        lake_bucket_curated_ssm="/test/lake/curated_bucket",
        lake_key_arn_ssm="/test/lake/kms_key_arn",
        sagemaker_role_arn_ssm="/test/ml/sagemaker_role_arn",
        results_table_name_ssm="/test/app/results_table_name",
        alert_topic_arn_ssm="/test/obs/alert_topic_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 2)     # trigger + post-process
    t.resource_count_is("AWS::Events::Rule",     2)     # nightly + output-arrived
    t.resource_count_is("AWS::CloudWatch::Alarm", 1)
```

---

## 7. References

- `docs/template_params.md` — `BATCH_INPUT_S3_URI`, `BATCH_OUTPUT_S3_URI`, `BATCH_INSTANCE_TYPE`, `BATCH_INSTANCE_COUNT`, `ALERT_THRESHOLD`, `MODEL_NAME_SSM`, `RESULTS_TABLE_NAME_SSM`
- `docs/Feature_Roadmap.md` — feature IDs `ML-16` (batch transform), `ML-17` (post-process), `E-04` (S3 → EventBridge)
- SageMaker Batch Transform: https://docs.aws.amazon.com/sagemaker/latest/dg/batch-transform.html
- S3 → EventBridge: https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventBridge.html
- Related SOPs: `MLOPS_SAGEMAKER_SERVING` (real-time alternative), `MLOPS_SAGEMAKER_TRAINING` (model source), `EVENT_DRIVEN_PATTERNS` (S3 → EB → Lambda), `LAYER_DATA` (lake buckets, KMS), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — ScoringStack resolves all cross-stack refs via SSM; identity-side grants only; S3 → EventBridge → Lambda (not `s3_notifications.LambdaDestination`); `KmsKeyId` passed as string ARN (5th non-negotiable); `iam:PassRole` scoped with `iam:PassedToService`. Extracted inline Lambda code to `lambda/batch_trigger/` and `lambda/batch_postprocess/` assets. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — batch trigger + post-process Lambdas inline, S3 `LambdaDestination` notification, nightly schedule, failure alarm. |
