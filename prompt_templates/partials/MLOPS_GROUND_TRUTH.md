# PARTIAL: SageMaker Ground Truth — Human Labeling Pipeline

**Usage:** Include when SOW mentions data labeling, annotation, creating training datasets from raw data, active learning, or human-in-the-loop data preparation.

---

## What Ground Truth Does

```
Raw Data (images, text, audio, video) → Ground Truth Labeling Job → Labeled Dataset → Training

Workforce options:
  1. Amazon Mechanical Turk  — large, fast, low cost ($0.012/label)
  2. AWS Marketplace vendors — specialized (medical, legal, language)
  3. Private workforce       — YOUR OWN employees (for confidential data)
  4. Automated labeling      — ML model labels easy examples (<$0.001/label)

Active Learning (semi-automated):
  Round 1: Humans label 10% of data
  Round 2: Train quick model, auto-label high-confidence items
  Round 3: Humans only label uncertain items
  Result: 70-80% automation rate, same quality
```

---

## CDK Code Block

```python
def _create_ground_truth_labeling(self, stage_name: str) -> None:
    """
    SageMaker Ground Truth Human Labeling Pipeline.

    Supports:
      - Text classification / sentiment
      - Named entity annotation
      - Image bounding box / classification
      - Semantic segmentation
      - Custom labeling tasks (via Lambda pre/post processor)

    Semi-automated active learning:
      Difficult examples → human labelers
      Easy examples → automated ML labeling
      Result: 70-80% cost reduction vs full human labeling
    """

    import aws_cdk.aws_sagemaker as sagemaker
    import aws_cdk.aws_cognito as cognito

    # =========================================================================
    # LABELING DATA BUCKETS
    # =========================================================================

    labeling_bucket = s3.Bucket(
        self, "LabelingBucket",
        bucket_name=f"{{project_name}}-labeling-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        versioned=True,
        removal_policy=RemovalPolicy.RETAIN,
    )
    labeling_bucket.grant_read_write(self.sagemaker_role)

    # =========================================================================
    # PRIVATE WORKFORCE (YOUR EMPLOYEES as labelers)
    # Use this for confidential/sensitive data (HIPAA, financial, legal)
    # =========================================================================

    # Cognito User Pool for labeler authentication
    labeler_user_pool = cognito.UserPool(
        self, "LabelerUserPool",
        user_pool_name=f"{{project_name}}-labelers-{stage_name}",
        self_sign_up_enabled=False,   # Admin creates accounts
        sign_in_aliases=cognito.SignInAliases(email=True, username=True),
        password_policy=cognito.PasswordPolicy(
            min_length=12,
            require_uppercase=True,
            require_digits=True,
            require_symbols=True,
        ),
        mfa=cognito.Mfa.REQUIRED,
        mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
        account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
        removal_policy=RemovalPolicy.RETAIN,
    )

    # Create private workforce configuration
    # [Claude: Ground Truth uses private workforce during labeling job creation]
    # NOTE: Private workforce is created via AWS Console or SDK (not supported directly in CDK L1 for all configs)
    # Use CfnWorkforce for private workforce:
    private_workforce = sagemaker.CfnWorkforce(
        self, "LabelingWorkforce",
        workforce_name=f"{{project_name}}-labelers-{stage_name}",
        cognito_config=sagemaker.CfnWorkforce.CognitoConfigProperty(
            client_id=labeler_user_pool.add_client(
                "LabelerClient",
                generate_secret=True,
            ).user_pool_client_id,
            user_pool=labeler_user_pool.user_pool_id,
        ),
    )

    # =========================================================================
    # LABELING JOB TRIGGER LAMBDA
    # =========================================================================

    labeling_trigger_fn = _lambda.Function(
        self, "LabelingJobTrigger",
        function_name=f"{{project_name}}-labeling-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime

logger = logging.getLogger()
sm = boto3.client('sagemaker')

def handler(event, context):
    task_type    = event.get('task_type', os.environ['DEFAULT_TASK_TYPE'])
    manifest_uri = event.get('manifest_uri', os.environ['DEFAULT_MANIFEST_URI'])
    job_name     = f"{{project_name}}-label-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    # Task-type to algorithm ARN mapping
    # See: https://docs.aws.amazon.com/sagemaker/latest/dg/sms-task-types.html
    ALGORITHM_ARNS = {
        "IMAGE_CLASSIFICATION":  f"arn:aws:sagemaker:{os.environ['AWS_REGION']}:027400017018:labeling-job-algorithm-specification/image-classification",
        "TEXT_CLASSIFICATION":   f"arn:aws:sagemaker:{os.environ['AWS_REGION']}:027400017018:labeling-job-algorithm-specification/text-classification",
        "BOUNDING_BOX":          f"arn:aws:sagemaker:{os.environ['AWS_REGION']}:027400017018:labeling-job-algorithm-specification/object-detection",
        "SEMANTIC_SEGMENTATION": f"arn:aws:sagemaker:{os.environ['AWS_REGION']}:027400017018:labeling-job-algorithm-specification/semantic-segmentation",
        "NAMED_ENTITY":          f"arn:aws:sagemaker:{os.environ['AWS_REGION']}:027400017018:labeling-job-algorithm-specification/named-entity-recognition",
    }

    resp = sm.create_labeling_job(
        LabelingJobName=job_name,

        LabelAttributeName="{{project_name}}-label",

        InputConfig={
            'DataSource': {'S3DataSource': {
                'ManifestS3Uri': manifest_uri,  # JSONL manifest pointing to items
            }},
            # Active learning: automatically label high-confidence items
            'DataAttributes': {
                'ContentClassifiers': ['FreeOfPersonallyIdentifiableInformation'],
            },
        },

        OutputConfig={
            'S3OutputPath': f"s3://{os.environ['LABELING_BUCKET']}/completed-labels/",
            'KmsKeyId': os.environ['KMS_KEY_ID'],
        },

        RoleArn=os.environ['SAGEMAKER_ROLE_ARN'],

        LabelCategoryConfigS3Uri=f"s3://{os.environ['LABELING_BUCKET']}/label-config/{task_type}/label_categories.json",

        HumanTaskConfig={
            # Private workforce
            'WorkteamArn': os.environ['WORKTEAM_ARN'],

            # UiConfig: custom template for labeling UI
            'UiConfig': {
                'UiTemplateS3Uri': f"s3://{os.environ['LABELING_BUCKET']}/ui-templates/{task_type}/template.html",
            },

            # Pre-annotation Lambda (transform data before showing to annotators)
            'PreHumanTaskLambdaArn': os.environ.get('PRE_ANNOTATION_LAMBDA_ARN', ''),

            # Task description shown to labelers
            'TaskTitle': f"{{project_name}} — {task_type.replace('_', ' ').title()} Task",
            'TaskDescription': f"Please review and annotate the following {task_type} item.",
            'TaskKeywords': ['{{project_name}}', task_type.lower()],

            # Time limits
            'TaskTimeLimitInSeconds': 300,        # 5 min per item
            'TaskAvailabilityLifetimeInSeconds': 60 * 60 * 24 * 7,  # 7 days

            # Number of labelers per item (for quality — majority vote)
            'NumberOfHumanWorkersPerDataObject': 3,  # 3 labelers → take majority

            # Annotation consolidation Lambda (resolve disagreements)
            'AnnotationConsolidationConfig': {
                'AnnotationConsolidationLambdaArn': os.environ.get('CONSOLIDATION_LAMBDA_ARN', ''),
            },
        },

        # Semi-automated active learning config
        LabelingJobAlgorithmsConfig={
            'LabelingJobAlgorithmSpecificationArn': ALGORITHM_ARNS.get(task_type, ALGORITHM_ARNS['TEXT_CLASSIFICATION']),
            'InitialActiveLearningModelArn': event.get('seed_model_arn', ''),  # Optional: start with existing model
            'LabelingJobResourceConfig': {
                'VolumeKmsKeyId': os.environ['KMS_KEY_ID'],
            },
        },

        Tags=[
            {'Key': 'Project',     'Value': '{{project_name}}'},
            {'Key': 'TaskType',    'Value': task_type},
            {'Key': 'Environment', 'Value': os.environ['STAGE']},
        ],
    )
    logger.info(f"Labeling job created: {resp['LabelingJobArn']}")
    return {'job_name': job_name, 'job_arn': resp['LabelingJobArn']}
"""),
        environment={
            "DEFAULT_TASK_TYPE":      "TEXT_CLASSIFICATION",
            "DEFAULT_MANIFEST_URI":   f"s3://{labeling_bucket.bucket_name}/unlabeled-manifest.jsonl",
            "LABELING_BUCKET":        labeling_bucket.bucket_name,
            "KMS_KEY_ID":             self.kms_key.key_arn,
            "SAGEMAKER_ROLE_ARN":     self.sagemaker_role.role_arn,
            "WORKTEAM_ARN":           f"arn:aws:sagemaker:{self.region}:{self.account}:workteam/private-crowd/{{project_name}}-labelers-{stage_name}",
            "STAGE":                  stage_name,
        },
        timeout=Duration.minutes(5),
    )
    labeling_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateLabelingJob", "sagemaker:DescribeLabelingJob"],
        resources=["*"],
    ))
    labeling_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.sagemaker_role.role_arn],
    ))
    labeling_bucket.grant_read(labeling_trigger_fn)

    # =========================================================================
    # LABELING COMPLETE TRIGGER
    # When SNS notification arrives that a labeling job is complete,
    # trigger the ML training pipeline with the new labeled data
    # =========================================================================

    labeling_complete_fn = _lambda.Function(
        self, "LabelingCompleteFn",
        function_name=f"{{project_name}}-labeling-complete-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
sm = boto3.client('sagemaker')

def handler(event, context):
    # Ground Truth publishes to SNS when job completes
    message = json.loads(event['Records'][0]['Sns']['Message'])
    output_manifest = message.get('LabelingJobOutput', {}).get('OutputDatasetS3Uri')
    logger.info(f"Labeling complete. Output manifest: {output_manifest}")

    if output_manifest:
        # Trigger training pipeline with newly labeled data
        resp = sm.start_pipeline_execution(
            PipelineName=os.environ['TRAINING_PIPELINE_NAME'],
            PipelineParameters=[
                {"Name": "DatasetS3Uri", "Value": output_manifest},
                {"Name": "DataSource",   "Value": "ground_truth"},
            ],
            PipelineExecutionDescription="Triggered by Ground Truth labeling completion",
            ClientRequestToken=context.aws_request_id,
        )
        logger.info(f"Training pipeline started: {resp['PipelineExecutionArn']}")
        return {"pipeline_execution_arn": resp['PipelineExecutionArn']}
    return {"status": "no_output_manifest"}
"""),
        environment={
            "TRAINING_PIPELINE_NAME": f"{{project_name}}-training-pipeline-{stage_name}",
        },
        timeout=Duration.seconds(30),
    )
    labeling_complete_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}-training-pipeline-{stage_name}"],
    ))

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "LabelingBucketName",
        value=labeling_bucket.bucket_name,
        description="S3 bucket for raw data manifests and completed labels",
        export_name=f"{{project_name}}-labeling-bucket-{stage_name}",
    )
    CfnOutput(self, "LabelingTriggerFnArn",
        value=labeling_trigger_fn.function_arn,
        description="Start a Ground Truth labeling job",
        export_name=f"{{project_name}}-labeling-trigger-{stage_name}",
    )
    CfnOutput(self, "LabelerUserPoolId",
        value=labeler_user_pool.user_pool_id,
        description="Cognito User Pool for private workforce labelers",
        export_name=f"{{project_name}}-labeler-user-pool-{stage_name}",
    )
```
