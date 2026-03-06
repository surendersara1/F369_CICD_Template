# PARTIAL: MLOps — SageMaker Training Pipeline

**Usage:** Include when SOW mentions ML model training, data science platform, feature engineering, model registry, MLflow, or experiment tracking.

---

## SageMaker MLOps Architecture

```
Data Scientists          ML Pipeline                    Model Registry
────────────────        ─────────────────              ─────────────────
SageMaker Studio ──►   Processing Job                 Model Package
  (Jupyter)              (feature eng)         ──►    Version 1.0 (pending)
  (Data Wrangler)   ──► Training Job                  Version 1.0 (approved) ──► Endpoint
  (Experiments)          (Spot instances)      ──►    Version 2.0 (staging)
  (MLflow)          ──► HPO Tuning Job                Version 2.0 (rejected)
                    ──► Evaluation Step
                    ──► Register Step ──────────────►  Model Registry

Environment Strategy (DIFFERENT from software dev/stage/prod):
  DS Domain    = Data science exploration   (any experiment, no governance)
  Staging Domain = Validated models         (reviewed by ML engineer)
  Production Domain = Approved models only  (approved by model committee)
```

---

## Three-Domain ML Environment Strategy

This is **different from software dev/staging/prod**. In MLOps:

| Domain                   | Who Uses It     | What Happens                                                   | Approval Gate                                        |
| ------------------------ | --------------- | -------------------------------------------------------------- | ---------------------------------------------------- |
| **Data Science (DS)**    | Data scientists | Experimentation, EDA, prototype models, any notebook           | None — free exploration                              |
| **Staging (ML Staging)** | ML Engineers    | Validated training pipelines, model evaluation, A/B test setup | ML Engineer reviews                                  |
| **Production (ML Prod)** | Model Ops       | Only approved models serve traffic, monitored 24/7             | Model Committee approval in SageMaker Model Registry |

---

## CDK Code Block — SageMaker Training Infrastructure

```python
def _create_sagemaker_training(self, stage_name: str) -> None:
    """
    SageMaker MLOps Training Platform.

    Components:
      A) SageMaker Studio Domain (data scientist workspace)
      B) SageMaker Feature Store (centralized feature management)
      C) SageMaker Experiments (training run tracking)
      D) SageMaker Pipelines via Lambda trigger (orchestrated ML workflow)
      E) Model Registry (model versioning and approval workflow)
      F) MLflow Tracking Server on SageMaker (optional: experiment tracking)

    [Claude: always include A + E for any ML SOW.
     Include B if SOW mentions "feature store", "feature engineering", "training/serving skew".
     Include F if SOW explicitly mentions MLflow.]
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # =========================================================================
    # SAGEMAKER EXECUTION ROLE
    # =========================================================================

    self.sagemaker_role = iam.Role(
        self, "SageMakerExecutionRole",
        assumed_by=iam.CompositePrincipal(
            iam.ServicePrincipal("sagemaker.amazonaws.com"),
        ),
        role_name=f"{{project_name}}-sagemaker-execution-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
        ],
    )

    # Grant SageMaker access to data lake + KMS
    for bucket in self.lake_buckets.values():
        bucket.grant_read_write(self.sagemaker_role)
    self.kms_key.grant_encrypt_decrypt(self.sagemaker_role)
    self.db_secret.grant_read(self.sagemaker_role)

    # =========================================================================
    # A) SAGEMAKER STUDIO DOMAIN
    # =========================================================================

    # VPC-only Studio (data stays inside VPC — no internet access from notebooks)
    self.studio_domain = sagemaker.CfnDomain(
        self, "StudioDomain",
        domain_name=f"{{project_name}}-studio-{stage_name}",
        auth_mode="IAM",  # Or "SSO" if using AWS IAM Identity Center

        vpc_id=self.vpc.vpc_id,
        subnet_ids=[s.subnet_id for s in self.vpc.private_subnets],

        app_network_access_type="VpcOnly",  # CRITICAL for security: no direct internet

        default_user_settings=sagemaker.CfnDomain.UserSettingsProperty(
            execution_role=self.sagemaker_role.role_arn,
            security_groups=[self.lambda_sg.security_group_id],

            # Jupyter Server (Studio UI)
            jupyter_server_app_settings=sagemaker.CfnDomain.JupyterServerAppSettingsProperty(
                default_resource_spec=sagemaker.CfnDomain.ResourceSpecProperty(
                    instance_type="system",   # Managed by SageMaker
                    sage_maker_image_arn=None,
                ),
            ),

            # Kernel Gateway (compute for notebooks)
            kernel_gateway_app_settings=sagemaker.CfnDomain.KernelGatewayAppSettingsProperty(
                default_resource_spec=sagemaker.CfnDomain.ResourceSpecProperty(
                    instance_type="ml.t3.medium" if stage_name == "ds" else "ml.m5.xlarge",
                ),
                # Prevent data scientists from using expensive GPU instances
                lifecycle_config_arns=[],
            ),

            # SageMaker Canvas (no-code ML) — enable only in DS domain
            canvas_app_settings=sagemaker.CfnDomain.CanvasAppSettingsProperty(
                time_series_forecasting_settings=sagemaker.CfnDomain.TimeSeriesForecastingSettingsProperty(
                    status="ENABLED" if stage_name == "ds" else "DISABLED",
                ),
            ),

            # Sharing settings — prevent copy/paste of data out of notebooks
            sharing_settings=sagemaker.CfnDomain.SharingSettingsProperty(
                notebook_output_option="Disabled" if stage_name == "prod" else "Allowed",
                s3_output_path=f"s3://{self.lake_buckets['curated'].bucket_name}/notebook-outputs/",
                s3_kms_key_id=self.kms_key.key_arn,
            ),
        ),

        domain_settings=sagemaker.CfnDomain.DomainSettingsProperty(
            # Security group for Studio ENI (VPC traffic only)
            security_group_ids=[self.lambda_sg.security_group_id],
        ),

        kms_key_id=self.kms_key.key_arn,

        tags=[
            {"key": "Project", "value": "{{project_name}}"},
            {"key": "Environment", "value": stage_name},
            {"key": "Domain", "value": "DataScience"},
        ],
    )

    # User Profiles per team/role
    # [Claude: generate one profile per role detected in Architecture Map]
    for profile_name, role_desc in [
        ("data-scientist", "Data scientists — full experiment access"),
        ("ml-engineer", "ML engineers — pipeline authoring, model registration"),
        ("ml-ops", "MLOps team — monitoring, endpoint management"),
    ]:
        sagemaker.CfnUserProfile(
            self, f"StudioUser{profile_name.replace('-','').title()}",
            domain_id=self.studio_domain.attr_domain_id,
            user_profile_name=profile_name,
            user_settings=sagemaker.CfnDomain.UserSettingsProperty(
                execution_role=self.sagemaker_role.role_arn,
            ),
        )

    # =========================================================================
    # B) SAGEMAKER FEATURE STORE
    # =========================================================================

    # Feature Group: store computed features for training AND serving
    # Online store: low-latency real-time feature serving (ElastiCache-backed)
    # Offline store: S3-backed, used for training dataset generation

    # [Claude: generate one feature group per entity from Architecture Map Section 5]
    FEATURE_GROUPS = [
        {
            "name": "user-features",
            "record_id": "user_id",
            "event_time": "event_time",
            "features": [
                {"name": "user_id", "type": "String"},
                {"name": "event_time", "type": "String"},
                {"name": "total_spend_30d", "type": "Fractional"},
                {"name": "session_count_7d", "type": "Integral"},
                {"name": "preferred_category", "type": "String"},
                {"name": "churn_risk_score", "type": "Fractional"},
            ],
            "online_enabled": True,    # Needed for real-time inference
            "offline_enabled": True,   # Needed for training
        },
        # [Claude: add more feature groups from Architecture Map]
    ]

    self.feature_groups: Dict[str, sagemaker.CfnFeatureGroup] = {}

    for fg_config in FEATURE_GROUPS:
        fg = sagemaker.CfnFeatureGroup(
            self, f"FeatureGroup{fg_config['name'].replace('-','').title()}",
            feature_group_name=f"{{project_name}}-{fg_config['name']}-{stage_name}",
            record_identifier_feature_name=fg_config["record_id"],
            event_time_feature_name=fg_config["event_time"],

            feature_definitions=[
                sagemaker.CfnFeatureGroup.FeatureDefinitionProperty(
                    feature_name=f["name"],
                    feature_type=f["type"],
                )
                for f in fg_config["features"]
            ],

            online_store_config=sagemaker.CfnFeatureGroup.OnlineStoreConfigProperty(
                enable_online_store=fg_config["online_enabled"],
                security_config=sagemaker.CfnFeatureGroup.OnlineStoreSecurityConfigProperty(
                    kms_key_id=self.kms_key.key_arn,
                ),
            ) if fg_config["online_enabled"] else None,

            offline_store_config=sagemaker.CfnFeatureGroup.OfflineStoreConfigProperty(
                s3_storage_config=sagemaker.CfnFeatureGroup.S3StorageConfigProperty(
                    s3_uri=f"s3://{self.lake_buckets['features'].bucket_name}/feature-store/{fg_config['name']}/",
                    kms_key_id=self.kms_key.key_arn,
                ),
                disable_glue_table_creation=False,  # Auto-register in Glue catalog
                data_catalog_config=sagemaker.CfnFeatureGroup.DataCatalogConfigProperty(
                    catalog=self.account,
                    database=f"{{project_name}}_{stage_name}_catalog",
                    table_name=fg_config["name"].replace("-", "_"),
                ),
            ) if fg_config["offline_enabled"] else None,

            role_arn=self.sagemaker_role.role_arn,

            tags=[
                {"key": "Project", "value": "{{project_name}}"},
                {"key": "Environment", "value": stage_name},
            ],
        )
        self.feature_groups[fg_config["name"]] = fg

    # =========================================================================
    # C) SAGEMAKER MODEL REGISTRY — Model versioning + approval workflow
    # =========================================================================

    # Model Package Group = one per ML use case / model family
    # [Claude: generate one group per detected ML use case from Architecture Map]
    ML_MODEL_GROUPS = [
        {"name": "churn-prediction", "description": "Customer churn risk scoring model"},
        {"name": "recommendation", "description": "Product recommendation model"},
        # [Claude: add from Architecture Map ML services]
    ]

    self.model_package_groups: Dict[str, sagemaker.CfnModelPackageGroup] = {}

    for group_config in ML_MODEL_GROUPS:
        mpg = sagemaker.CfnModelPackageGroup(
            self, f"ModelGroup{group_config['name'].replace('-','').title()}",
            model_package_group_name=f"{{project_name}}-{group_config['name']}-{stage_name}",
            model_package_group_description=group_config["description"],

            # Model approval policy:
            # MANUAL = requires human approval in Model Registry before deployment
            # AUTOMATIC = auto-approve (use only in dev)
            # [Claude: always use MANUAL in staging and prod]

            tags=[
                {"key": "Project", "value": "{{project_name}}"},
                {"key": "ModelFamily", "value": group_config["name"]},
            ],
        )
        self.model_package_groups[group_config["name"]] = mpg

    # EventBridge rule: trigger deployment Lambda when model is APPROVED in registry
    events.Rule(
        self, "ModelApprovedRule",
        rule_name=f"{{project_name}}-model-approved-{stage_name}",
        description="Trigger endpoint deployment when ML engineer approves model in registry",
        event_pattern=events.EventPattern(
            source=["aws.sagemaker"],
            detail_type=["SageMaker Model Package State Change"],
            detail={
                "ModelApprovalStatus": ["Approved"],
                "ModelPackageGroupName": [
                    f"{{project_name}}-{g['name']}-{stage_name}" for g in ML_MODEL_GROUPS
                ],
            },
        ),
        targets=[
            targets.LambdaFunction(
                self.lambda_functions.get(
                    "ModelDeployer",
                    list(self.lambda_functions.values())[0]
                ),
                retry_attempts=2,
            )
        ],
    )

    # =========================================================================
    # D) MLFLOW TRACKING SERVER (optional — for teams already using MLflow)
    # =========================================================================
    # [Include when SOW explicitly says "MLflow" or "open-source experiment tracking"]

    # SageMaker managed MLflow server (2024 GA feature)
    mlflow_server = sagemaker.CfnMlflowTrackingServer(
        self, "MLflowServer",
        tracking_server_name=f"{{project_name}}-mlflow-{stage_name}",
        artifact_store_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/mlflow-artifacts/",
        role_arn=self.sagemaker_role.role_arn,
        tracking_server_size="Small" if stage_name != "prod" else "Medium",
        mlflow_version="2.13.0",
        automatic_model_registration=True,  # Auto-register logged models to Model Registry
    )

    # =========================================================================
    # E) SAGEMAKER PIPELINE TRIGGER LAMBDA
    # Starts a SageMaker Pipeline run (the ML training workflow)
    # SageMaker Pipelines themselves are defined in pipeline code (not CDK)
    # =========================================================================

    pipeline_trigger_fn = _lambda.Function(
        self, "MLPipelineTrigger",
        function_name=f"{{project_name}}-ml-pipeline-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sm = boto3.client('sagemaker')

def handler(event, context):
    pipeline_name = os.environ['PIPELINE_NAME']

    # Build pipeline parameters from trigger event
    params = [
        {"Name": "ProcessingInstanceType", "Value": os.environ.get('PROCESSING_INSTANCE', 'ml.m5.xlarge')},
        {"Name": "TrainingInstanceType", "Value": os.environ.get('TRAINING_INSTANCE', 'ml.m5.2xlarge')},
        {"Name": "ModelApprovalStatus", "Value": "PendingManualApproval"},
        {"Name": "ExecutionDate", "Value": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')},
    ]

    # Inject dynamic params from trigger event
    if 'pipeline_parameters' in event:
        for k, v in event['pipeline_parameters'].items():
            params.append({"Name": k, "Value": str(v)})

    response = sm.start_pipeline_execution(
        PipelineName=pipeline_name,
        PipelineParameters=params,
        PipelineExecutionDescription=f"Triggered by {context.function_name}",
        ClientRequestToken=context.aws_request_id,
    )

    execution_arn = response['PipelineExecutionArn']
    logger.info(f"Started pipeline execution: {execution_arn}")

    return {
        "statusCode": 200,
        "pipeline_execution_arn": execution_arn,
    }
"""),
        environment={
            "PIPELINE_NAME": f"{{project_name}}-training-pipeline-{stage_name}",
            "PROCESSING_INSTANCE": "ml.m5.xlarge" if stage_name != "prod" else "ml.m5.4xlarge",
            "TRAINING_INSTANCE": "ml.m5.2xlarge" if stage_name != "prod" else "ml.p3.2xlarge",
        },
        timeout=Duration.seconds(30),
        tracing=_lambda.Tracing.ACTIVE,
    )

    # Grant Lambda permission to start SageMaker pipelines
    pipeline_trigger_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["sagemaker:StartPipelineExecution", "sagemaker:DescribePipelineExecution"],
            resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}*"],
        )
    )

    # Schedule: retrain model daily/weekly
    events.Rule(
        self, "MLRetrainingSchedule",
        rule_name=f"{{project_name}}-ml-retrain-schedule-{stage_name}",
        schedule=events.Schedule.cron(
            hour="2", minute="0",
            week_day="MON" if stage_name == "prod" else "*",  # Weekly in prod, daily in dev
        ),
        enabled=stage_name != "ds",  # Don't auto-retrain in data science exploration domain
        targets=[targets.LambdaFunction(pipeline_trigger_fn)],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "StudioDomainId",
        value=self.studio_domain.attr_domain_id,
        description="SageMaker Studio Domain ID",
        export_name=f"{{project_name}}-studio-domain-id-{stage_name}",
    )

    CfnOutput(self, "MLPipelineTriggerArn",
        value=pipeline_trigger_fn.function_arn,
        description="Lambda ARN to trigger SageMaker training pipeline",
        export_name=f"{{project_name}}-ml-pipeline-trigger-{stage_name}",
    )

    for group_name in self.model_package_groups:
        CfnOutput(self, f"ModelGroup{group_name.replace('-','').title()}",
            value=f"{{project_name}}-{group_name}-{stage_name}",
            description=f"SageMaker Model Package Group: {group_name}",
        )
```

---

## SageMaker Pipeline Code (saved as `ml/pipelines/training_pipeline.py`)

This goes in your project's `ml/` folder. It's NOT CDK — it's the SageMaker Pipeline definition, run by the trigger Lambda above. Claude generates this in Pass 3.

```python
# ml/pipelines/training_pipeline.py
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.workflow.model_step import ModelStep
from sagemaker.workflow.parameters import ParameterString, ParameterInteger
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.sklearn.processing import SKLearnProcessor
from sagemaker.estimator import Estimator

def create_pipeline(sagemaker_session, role_arn, pipeline_name, model_package_group_name):

    # Pipeline Parameters (can be overridden at runtime)
    processing_instance = ParameterString("ProcessingInstanceType", default_value="ml.m5.xlarge")
    training_instance = ParameterString("TrainingInstanceType", default_value="ml.m5.2xlarge")
    approval_status = ParameterString("ModelApprovalStatus", default_value="PendingManualApproval")
    accuracy_threshold = ParameterInteger("AccuracyThreshold", default_value=80)  # Min 80% accuracy

    # Step 1: Data processing + feature engineering
    processor = SKLearnProcessor(framework_version="1.2-1", role=role_arn,
                                  instance_type=processing_instance, instance_count=1)
    process_step = ProcessingStep(
        name="FeatureEngineering",
        processor=processor,
        code="ml/scripts/feature_engineering.py",
        inputs=[...],
        outputs=[...],
    )

    # Step 2: Model training (with Spot instances for cost savings)
    estimator = Estimator(
        image_uri="...",        # Your training container
        role=role_arn,
        instance_type=training_instance,
        instance_count=1,
        use_spot_instances=True,       # Up to 90% cheaper than on-demand
        max_wait=7200,                 # Max wait for spot (2 hours)
        max_run=3600,                  # Job timeout (1 hour)
        checkpoint_s3_uri="s3://...", # Resume from checkpoints if spot interrupted
        hyperparameters={"n-estimators": 200, "max-depth": 6},
        sagemaker_session=sagemaker_session,
    )
    train_step = TrainingStep(name="ModelTraining", estimator=estimator,
                              inputs={"train": process_step.properties.ProcessingOutputConfig...})

    # Step 3: Evaluate model (calculate accuracy, AUC, F1)
    eval_step = ProcessingStep(name="ModelEvaluation", ...)

    # Step 4: Conditional registration (only register if accuracy >= threshold)
    accuracy_condition = ConditionGreaterThanOrEqualTo(
        left=eval_step.properties.ProcessingOutputConfig...,
        right=accuracy_threshold,
    )

    model_step = ModelStep(name="RegisterModel",
                           model_approval_status=approval_status,
                           model_package_group_name=model_package_group_name, ...)

    condition_step = ConditionStep(
        name="AccuracyCheck",
        conditions=[accuracy_condition],
        if_steps=[model_step],      # Register if accurate enough
        else_steps=[],              # Do nothing if below threshold (pipeline stops)
    )

    return Pipeline(
        name=pipeline_name,
        parameters=[processing_instance, training_instance, approval_status, accuracy_threshold],
        steps=[process_step, train_step, eval_step, condition_step],
        sagemaker_session=sagemaker_session,
    )
```
