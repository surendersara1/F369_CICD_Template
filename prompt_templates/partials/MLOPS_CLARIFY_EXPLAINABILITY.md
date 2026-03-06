# PARTIAL: SageMaker Clarify — Bias Detection + Model Explainability (SHAP)

**Usage:** Include when SOW mentions model explainability, SHAP values, feature importance, bias detection, fairness, EU AI Act compliance, HIPAA/financial model auditability, or regulatory reporting.

---

## What Clarify Does

```
Two jobs, both run as SageMaker ProcessingJobs:

1. BIAS ANALYSIS (pre-training + post-training):
   Pre-training:  Is my TRAINING DATA biased? (before any model exists)
   Post-training: Does my TRAINED MODEL produce biased predictions?
   Metrics: DPL (demographic parity), DI (disparate impact), FTd, KL divergence

2. EXPLAINABILITY (SHAP):
   For every prediction: "Which features contributed most to this outcome?"
   Uses Kernel SHAP — model-agnostic, works with any model type
   Output: per-feature SHAP values + global feature importance
```

---

## CDK Code Block

```python
def _create_clarify_explainability(self, stage_name: str) -> None:
    """
    SageMaker Clarify — Bias + Explainability.

    Runs as part of the ML pipeline after model training.
    Results stored in S3 + published to Model Registry metadata.
    Required for: HIPAA, financial models, EU AI Act, SOC2 with ML.
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # =========================================================================
    # CLARIFY RESULTS BUCKET
    # =========================================================================

    clarify_bucket = s3.Bucket(
        self, "ClarifyResults",
        bucket_name=f"{{project_name}}-clarify-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="retain-audit-trail",
            enabled=True,
            expiration=Duration.days(365 * 7),  # 7 year retention for regulatory
        )],
        removal_policy=RemovalPolicy.RETAIN,  # Never destroy compliance artifacts
    )
    clarify_bucket.grant_read_write(self.sagemaker_role)

    # =========================================================================
    # CLARIFY BIAS ANALYSIS TRIGGER LAMBDA
    # Run this after each model training to check for bias
    # =========================================================================

    clarify_bias_fn = _lambda.Function(
        self, "ClarifyBiasFn",
        function_name=f"{{project_name}}-clarify-bias-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime

logger = logging.getLogger()
sm = boto3.client('sagemaker')

def handler(event, context):
    model_name      = event.get('model_name', os.environ['DEFAULT_MODEL_NAME'])
    dataset_s3_uri  = event.get('dataset_uri', os.environ['DATASET_URI'])
    job_name        = f"clarify-bias-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    # SageMaker Clarify bias analysis config
    clarify_config = {
        "version": "1.0",
        "dataset_type": "text/csv",
        "dataset_uri": dataset_s3_uri,
        "headers": json.loads(os.environ['FEATURE_HEADERS']),
        "label": os.environ['LABEL_COLUMN'],
        "label_values_or_threshold": json.loads(os.environ.get('POSITIVE_LABEL_VALUES', '[1]')),

        # Sensitive attributes to check for bias
        "facet": [
            {"name_or_index": os.environ['SENSITIVE_ATTRIBUTE'], "value_or_threshold": json.loads(os.environ.get('SENSITIVE_VALUES', '[]'))},
        ],

        # Pre-training bias metrics to compute
        "pre_training_bias_methods": "all",

        # Post-training bias metrics
        "post_training_bias_methods": "all",

        "predicted_label": "prediction",
        "predicted_label_dataset_uri": event.get('predictions_uri', os.environ.get('PREDICTIONS_URI', '')),
        "probability": "probability",
        "probability_threshold": 0.5,
    }

    resp = sm.create_processing_job(
        ProcessingJobName=job_name,
        ProcessingResources={
            "ClusterConfig": {
                "InstanceCount": 1,
                "InstanceType": "ml.m5.xlarge",
                "VolumeSizeInGB": 20,
            }
        },
        AppSpecification={
            "ImageUri": os.environ['CLARIFY_IMAGE_URI'],
            "ContainerArguments": ["--analysis-type", "bias"],
        },
        ProcessingInputs=[
            {
                "InputName": "analysis_config",
                "S3Input": {
                    "S3Uri": f"{os.environ['CLARIFY_BUCKET']}/configs/{job_name}/",
                    "LocalPath": "/opt/ml/processing/input/config",
                    "S3DataType": "S3Prefix",
                    "S3InputMode": "File",
                },
            }
        ],
        ProcessingOutputs=[
            {
                "OutputName": "analysis_result",
                "S3Output": {
                    "S3Uri": f"{os.environ['CLARIFY_BUCKET']}/results/{job_name}/",
                    "LocalPath": "/opt/ml/processing/output",
                    "S3UploadMode": "EndOfJob",
                },
            }
        ],
        RoleArn=os.environ['SAGEMAKER_ROLE_ARN'],
        Tags=[
            {"Key": "Project", "Value": os.environ.get('PROJECT_NAME', 'unknown')},
            {"Key": "ModelName", "Value": model_name},
            {"Key": "AnalysisType", "Value": "bias"},
        ],
    )
    logger.info(f"Clarify bias job started: {job_name}")
    return {"job_name": job_name, "job_arn": resp['ProcessingJobArn']}
"""),
        environment={
            "DEFAULT_MODEL_NAME":   f"{{project_name}}-model-{stage_name}",
            "DATASET_URI":          f"s3://{self.lake_buckets['processed'].bucket_name}/validation/",
            "CLARIFY_BUCKET":       f"s3://{clarify_bucket.bucket_name}",
            "SAGEMAKER_ROLE_ARN":   self.sagemaker_role.role_arn,
            "FEATURE_HEADERS":      '["age","income","gender","loan_amount","credit_score","label"]',
            "LABEL_COLUMN":         "label",
            "SENSITIVE_ATTRIBUTE":  "gender",      # [Claude: update from Architecture Map]
            "SENSITIVE_VALUES":     '["female","non-binary"]',
            "POSITIVE_LABEL_VALUES": '[1]',
            "CLARIFY_IMAGE_URI":    f"306415355426.dkr.ecr.{self.region}.amazonaws.com/sagemaker-clarify-processing:1",
            "PROJECT_NAME":         "{{project_name}}",
        },
        timeout=Duration.minutes(5),
    )
    clarify_bias_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob"],
        resources=["*"],
    ))
    clarify_bias_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.sagemaker_role.role_arn],
    ))
    clarify_bucket.grant_read_write(clarify_bias_fn)

    # =========================================================================
    # BIAS VIOLATION ALARM
    # Alert if a protected group is more likely to be declined/classified negatively
    # =========================================================================

    cw.Alarm(
        self, "BiasViolationAlarm",
        alarm_name=f"{{project_name}}-bias-violation-{stage_name}",
        alarm_description="SageMaker Clarify detected bias in model predictions — review before deployment",
        metric=cw.Metric(
            namespace=f"{{project_name}}/Clarify",
            metric_name="BiasViolationDetected",
            dimensions_map={"Stage": stage_name},
            period=Duration.hours(1),
            statistic="Maximum",
        ),
        threshold=0,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )

    # =========================================================================
    # EXPLAINABILITY REPORT LAMBDA (SHAP values per prediction)
    # =========================================================================

    explain_fn = _lambda.Function(
        self, "SHAPExplainFn",
        function_name=f"{{project_name}}-explain-prediction-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
sm_runtime = boto3.client('sagemaker-runtime')

ENDPOINT_NAME = os.environ['ENDPOINT_NAME']

def handler(event, context):
    # Invoke SageMaker endpoint with explain=True to get SHAP values
    payload = event.get('features', event.get('body', '{}'))
    if isinstance(payload, dict):
        payload = json.dumps(payload)

    response = sm_runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType='application/json',
        Body=payload,
        Accept='application/json',
        # Custom header to request explanation (endpoint must support it)
        CustomAttributes=json.dumps({"explain": True}),
    )
    result = json.loads(response['Body'].read())
    logger.info(f"Explanation generated: {json.dumps(result)[:200]}")
    return {'statusCode': 200, 'body': json.dumps(result)}
"""),
        environment={
            "ENDPOINT_NAME": f"{{project_name}}-inference-{stage_name}",
        },
        timeout=Duration.seconds(30),
    )
    explain_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-inference-{stage_name}"],
    ))

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "ClarifyResultsBucket",
        value=clarify_bucket.bucket_name,
        description="Bias and explainability reports bucket (7-year retention)",
        export_name=f"{{project_name}}-clarify-bucket-{stage_name}",
    )
    CfnOutput(self, "ClarifyBiasFnArn",
        value=clarify_bias_fn.function_arn,
        description="Trigger Clarify bias analysis after model training",
        export_name=f"{{project_name}}-clarify-bias-{stage_name}",
    )
    CfnOutput(self, "SHAPExplainFnArn",
        value=explain_fn.function_arn,
        description="Get SHAP feature attribution for any prediction",
        export_name=f"{{project_name}}-shap-explain-{stage_name}",
    )
```
