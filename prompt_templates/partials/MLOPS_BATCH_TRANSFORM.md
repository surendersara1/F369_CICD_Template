# PARTIAL: SageMaker Batch Transform — Offline Scoring at Scale

**Usage:** Include when SOW mentions offline scoring, nightly batch predictions, scoring millions of records, generating reports, or any ML inference that doesn't need to be real-time.

---

## When Batch Transform vs Real-Time Endpoint

| Use Batch Transform                            | Use Real-Time Endpoint          |
| ---------------------------------------------- | ------------------------------- |
| Score 10M customers overnight                  | Fraud check on each transaction |
| Monthly churn prediction report                | Recommendation on page load     |
| Weekly risk scoring for all accounts           | Chatbot response                |
| Nightly demand forecast                        | Real-time pricing               |
| Cost: **$0.19/hr while running, $0 when idle** | Cost: **$0.28/hr 24/7**         |

---

## CDK Code Block

```python
def _create_batch_transform_pipeline(self, stage_name: str) -> None:
    """
    SageMaker Batch Transform — large-scale offline scoring.

    Use for: nightly churn scoring, monthly risk reports, demand forecasting.
    Cost: pay only while job runs (often 2-4hr), zero idle cost.

    Pipeline:
      1. Trigger Lambda starts a BatchTransform job
      2. SageMaker scores all records in parallel (multi-instance)
      3. Results written to S3 → trigger downstream Lambda via S3 event
      4. Post-process Lambda: format results, write to DynamoDB / Redshift / SNS alerts
    """

    # =========================================================================
    # BATCH TRANSFORM TRIGGER LAMBDA
    # =========================================================================

    batch_trigger_fn = _lambda.Function(
        self, "BatchTransformTrigger",
        function_name=f"{{project_name}}-batch-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime

logger = logging.getLogger()
sm = boto3.client('sagemaker')

def handler(event, context):
    run_id     = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    model_name = event.get('model_name', os.environ['MODEL_NAME'])
    input_uri  = event.get('input_uri',  os.environ['INPUT_S3_URI'])
    output_uri = f"{os.environ['OUTPUT_S3_URI']}{run_id}/"

    resp = sm.create_transform_job(
        TransformJobName=f"{{project_name}}-batch-{run_id}",
        ModelName=model_name,

        # Batch strategy: MultiRecord = pack multiple records into one request
        # Maximizes throughput, reduces cost
        BatchStrategy='MultiRecord',
        MaxPayloadInMB=6,              # Max request size sent to model
        MaxConcurrentTransforms=0,     # 0 = use all available (auto)

        TransformInput={
            'DataSource': {'S3DataSource': {
                'S3DataType': 'S3Prefix',
                'S3Uri': input_uri,
            }},
            'ContentType': 'text/csv',
            'SplitType': 'Line',        # Split input by newline
        },

        TransformOutput={
            'S3OutputPath': output_uri,
            'Accept': 'application/json',
            'AssembleWith': 'Line',     # One result per line in output file
            'KmsKeyId': os.environ['KMS_KEY_ID'],
        },

        TransformResources={
            # [Claude: scale up for prod, smaller for dev]
            'InstanceType': os.environ.get('INSTANCE_TYPE', 'ml.m5.2xlarge'),
            'InstanceCount': int(os.environ.get('INSTANCE_COUNT', '2')),  # Parallel scoring
        },

        Tags=[
            {'Key': 'Project',     'Value': '{{project_name}}'},
            {'Key': 'Environment', 'Value': os.environ['STAGE']},
            {'Key': 'Trigger',     'Value': 'scheduled' if not event.get('manual') else 'manual'},
        ],

        DataProcessing={
            # JMESPath expressions to extract features and join with output
            'InputFilter':  '$',           # Pass through all input columns
            'OutputFilter': '$.SageMakerOutput',  # Extract just the prediction
            'JoinSource':   'Input',       # Append input cols to output for context
        },
    )
    logger.info(f"Batch job started: {resp['TransformJobArn']}, output: {output_uri}")
    return {
        'job_name':   f"{{project_name}}-batch-{run_id}",
        'job_arn':    resp['TransformJobArn'],
        'output_uri': output_uri,
    }
"""),
        environment={
            "MODEL_NAME":       f"{{project_name}}-model-{stage_name}",
            "INPUT_S3_URI":     f"s3://{self.lake_buckets['processed'].bucket_name}/batch-input/",
            "OUTPUT_S3_URI":    f"s3://{self.lake_buckets['curated'].bucket_name}/batch-output/",
            "KMS_KEY_ID":       self.kms_key.key_arn,
            "INSTANCE_TYPE":    "ml.m5.xlarge" if stage_name != "prod" else "ml.m5.4xlarge",
            "INSTANCE_COUNT":   "1" if stage_name != "prod" else "4",
            "STAGE":            stage_name,
        },
        timeout=Duration.minutes(5),
    )
    batch_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob"],
        resources=["*"],
    ))
    batch_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.sagemaker_role.role_arn],
    ))

    # Schedule: run nightly batch scoring at 1am
    events.Rule(self, "NightlyBatchScore",
        rule_name=f"{{project_name}}-nightly-batch-{stage_name}",
        schedule=events.Schedule.cron(hour="1", minute="0"),
        targets=[targets.LambdaFunction(batch_trigger_fn)],
        enabled=stage_name != "ds",
    )

    # =========================================================================
    # POST-PROCESSING LAMBDA — Triggered by S3 when batch results arrive
    # =========================================================================

    post_process_fn = _lambda.Function(
        self, "BatchPostProcessFn",
        function_name=f"{{project_name}}-batch-postprocess-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, csv, logging, io
logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3  = boto3.client('s3')
ddb = boto3.resource('dynamodb').Table(os.environ['RESULTS_TABLE'])
sns = boto3.client('sns')

HIGH_SCORE_THRESHOLD = float(os.environ.get('ALERT_THRESHOLD', '0.90'))

def handler(event, context):
    # Triggered by S3 event when batch output file arrives
    for record in event['Records']:
        bucket = record['s3']['bucket']['name']
        key    = record['s3']['object']['key']

        obj  = s3.get_object(Bucket=bucket, Key=key)
        body = obj['Body'].read().decode('utf-8')

        # Parse output — each line is a JSON prediction result
        batch_items = []
        high_score_alerts = []

        for line in body.strip().split('\\n'):
            if not line.strip():
                continue
            result = json.loads(line)
            entity_id = result.get('id', result.get('user_id', 'unknown'))
            score     = float(result.get('score', result.get('prediction', 0)))

            batch_items.append({'entity_id': entity_id, 'score': score, 'details': result})

            if score >= HIGH_SCORE_THRESHOLD:
                high_score_alerts.append({'id': entity_id, 'score': score})

        # Batch write to DynamoDB
        with ddb.batch_writer() as bw:
            for item in batch_items:
                bw.put_item(Item={
                    'entity_id': item['entity_id'],
                    'score':     str(item['score']),
                    'details':   json.dumps(item['details']),
                    'batch_key': key,
                })

        # Alert on high-score entities (e.g., high churn risk, high fraud risk)
        if high_score_alerts:
            sns.publish(
                TopicArn=os.environ['ALERT_TOPIC_ARN'],
                Subject=f"🔔 Batch scoring: {len(high_score_alerts)} high-risk entities",
                Message=json.dumps({'high_risk': high_score_alerts[:100], 'total': len(high_score_alerts)}),
            )

        logger.info(f"Processed {len(batch_items)} records, {len(high_score_alerts)} high-score alerts")
    return {"processed": len(batch_items)}
"""),
        environment={
            "RESULTS_TABLE":    list(self.ddb_tables.values())[0].table_name,
            "ALERT_TOPIC_ARN":  self.alert_topic.topic_arn,
            "ALERT_THRESHOLD":  "0.90",
        },
        memory_size=512,
        timeout=Duration.minutes(15),
    )
    self.lake_buckets["curated"].grant_read(post_process_fn)
    list(self.ddb_tables.values())[0].grant_write_data(post_process_fn)
    self.alert_topic.grant_publish(post_process_fn)

    # S3 trigger: fire post-processing Lambda when batch output arrives
    self.lake_buckets["curated"].add_event_notification(
        s3.EventType.OBJECT_CREATED,
        s3n.LambdaDestination(post_process_fn),
        s3.NotificationKeyFilter(prefix="batch-output/", suffix=".out"),
    )

    # =========================================================================
    # BATCH JOB MONITORING ALARM
    # =========================================================================
    cw.Alarm(
        self, "BatchJobFailedAlarm",
        alarm_name=f"{{project_name}}-batch-failed-{stage_name}",
        alarm_description="SageMaker batch transform job failed — check logs",
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

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "BatchTriggerFnArn",
        value=batch_trigger_fn.function_arn,
        description="Trigger nightly batch scoring job",
        export_name=f"{{project_name}}-batch-trigger-{stage_name}",
    )
```
