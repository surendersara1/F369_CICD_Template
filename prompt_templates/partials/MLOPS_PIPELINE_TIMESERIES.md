# PARTIAL: Time Series Forecasting Pipeline — DeepAR, Chronos, Prophet

**Usage:** Include when SOW mentions demand forecasting, sales prediction, capacity planning, anomaly detection in metrics, predictive maintenance, or IoT sensor forecasting.

---

## Model Selection Guide

| Scenario                                  | Model                                      | Why                                               |
| ----------------------------------------- | ------------------------------------------ | ------------------------------------------------- |
| Hundreds of related series (SKUs, stores) | **DeepAR** (SageMaker built-in)            | Trains on all series jointly, handles cold-start  |
| Any time series, zero-shot                | **Chronos** (Amazon, HuggingFace)          | Pre-trained, no training needed, great out-of-box |
| Single series, interpretable, holidays    | **Prophet** (Meta)                         | Explainable trend/seasonality decomposition       |
| Complex multi-variate                     | **TFT** (Temporal Fusion Transformer)      | Best accuracy, GPU training needed                |
| Real-time streaming anomaly               | **Random Cut Forest** (SageMaker built-in) | Online learning, no batch training needed         |

---

## CDK Code Block

```python
def _create_timeseries_pipeline(self, stage_name: str) -> None:
    """
    Time Series Forecasting ML Pipeline.

    Supports:
      - Demand forecasting (retail, supply chain)
      - Capacity planning (infra, staffing)
      - Anomaly detection in metrics (ops, finance)
      - IoT predictive maintenance

    Pipeline Steps:
      1. Data Preparation — resample, fill gaps, add calendar features
      2. Training — DeepAR or Chronos on SageMaker
      3. Backtesting Evaluation — MAPE, RMSE, sMAPE on holdout period
      4. Forecast Generation — future N-period predictions → S3 + DynamoDB
      5. Alert Generation — trigger alerts when forecast anomaly detected
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # =========================================================================
    # FORECAST CONFIG
    # [Claude: extract from Architecture Map — forecast horizon, granularity]
    # =========================================================================

    FORECAST_CONFIG = {
        "horizon":     14,          # Forecast 14 periods ahead
        "granularity": "D",         # D=daily, H=hourly, W=weekly
        "context_len": 90,          # Use 90 periods of history as context
        "num_samples":  100,        # Monte Carlo samples for prediction intervals
        "quantiles":   [0.1, 0.5, 0.9],  # P10, P50 (median), P90 forecast bands
    }

    # =========================================================================
    # FORECASTING LAMBDA — Run forecast + write results to DynamoDB
    # =========================================================================

    forecast_fn = _lambda.Function(
        self, "ForecastFn",
        function_name=f"{{project_name}}-forecast-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/forecast_service"),
        environment={
            "ENDPOINT_NAME":  f"{{project_name}}-timeseries-inference-{stage_name}",
            "RESULTS_TABLE":  list(self.ddb_tables.values())[0].table_name,
            "HORIZON":        str(FORECAST_CONFIG["horizon"]),
            "GRANULARITY":    FORECAST_CONFIG["granularity"],
        },
        memory_size=512,
        timeout=Duration.minutes(5),
        tracing=_lambda.Tracing.ACTIVE,
    )
    list(self.ddb_tables.values())[0].grant_read_write_data(forecast_fn)
    forecast_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-timeseries-inference-{stage_name}"],
    ))

    # Scheduled forecast generation (daily at 3am)
    events.Rule(self, "DailyForecastSchedule",
        rule_name=f"{{project_name}}-daily-forecast-{stage_name}",
        schedule=events.Schedule.cron(hour="3", minute="0"),
        targets=[targets.LambdaFunction(forecast_fn)],
        enabled=stage_name != "ds",
    )

    # =========================================================================
    # PIPELINE TRIGGER LAMBDA
    # =========================================================================

    ts_pipeline_fn = _lambda.Function(
        self, "TSPipelineTrigger",
        function_name=f"{{project_name}}-ts-pipeline-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, logging
logger = logging.getLogger()
sm = boto3.client('sagemaker')

def handler(event, context):
    params = [
        {"Name": "DatasetS3Uri",  "Value": event.get('dataset_uri',  os.environ['DEFAULT_DATASET'])},
        {"Name": "ForecastHorizon","Value": str(event.get('horizon',  14))},
        {"Name": "ModelAlgorithm", "Value": event.get('algorithm',    'deepar')},
        {"Name": "ContextLength",  "Value": str(event.get('context',  90))},
        {"Name": "Epochs",         "Value": str(event.get('epochs',   300))},
    ]
    resp = sm.start_pipeline_execution(
        PipelineName=os.environ['PIPELINE_NAME'],
        PipelineParameters=params,
        ClientRequestToken=context.aws_request_id,
    )
    return {"execution_arn": resp['PipelineExecutionArn']}
"""),
        environment={
            "PIPELINE_NAME":   f"{{project_name}}-ts-pipeline-{stage_name}",
            "DEFAULT_DATASET": f"s3://{self.lake_buckets['processed'].bucket_name}/timeseries/",
        },
        timeout=Duration.seconds(30),
    )
    ts_pipeline_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}-ts-pipeline-{stage_name}"],
    ))

    # Retrain weekly
    events.Rule(self, "TSRetrainSchedule",
        schedule=events.Schedule.cron(hour="1", minute="0", week_day="SUN"),
        targets=[targets.LambdaFunction(ts_pipeline_fn)],
        enabled=stage_name == "prod",
    )

    CfnOutput(self, "ForecastFnArn",
        value=forecast_fn.function_arn,
        description="Invoke to get forecasts for a series",
        export_name=f"{{project_name}}-forecast-fn-{stage_name}",
    )
```

---

## SageMaker Pipeline Code (`ml/pipelines/timeseries_pipeline.py`)

```python
# ml/pipelines/timeseries_pipeline.py
from sagemaker.estimator import Estimator
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.workflow.parameters import ParameterString, ParameterInteger
from sagemaker.sklearn.processing import SKLearnProcessor
import sagemaker

def create_timeseries_pipeline(sm_session, role_arn, pipeline_name, s3_bucket, model_package_group):

    # === Parameters ===
    dataset_uri = ParameterString("DatasetS3Uri")
    horizon     = ParameterInteger("ForecastHorizon", default_value=14)
    algorithm   = ParameterString("ModelAlgorithm", default_value="deepar")
    context_len = ParameterInteger("ContextLength", default_value=90)
    epochs      = ParameterInteger("Epochs", default_value=300)

    # === Step 1: Preprocess — resample, fill gaps, train/test split ===
    preprocessor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type="ml.m5.xlarge", instance_count=1,
    )
    preprocess_step = ProcessingStep(
        name="PreprocessTimeSeries",
        processor=preprocessor,
        code="ml/scripts/ts_preprocess.py",
        inputs=[ProcessingInput(source=dataset_uri, destination="/opt/ml/processing/input")],
        outputs=[
            ProcessingOutput(output_name="train", source="/opt/ml/processing/output/train"),
            ProcessingOutput(output_name="test",  source="/opt/ml/processing/output/test"),
        ],
        job_arguments=[
            "--granularity", "D",
            "--context-length", str(context_len),
            "--forecast-horizon", str(horizon),
            "--fill-method", "forward_fill",
            "--add-calendar-features", "true",  # Day of week, month, holidays, fourier terms
        ],
    )

    # === Step 2: Training (DeepAR — SageMaker built-in) ===
    region = sm_session.boto_region_name
    deepar_image = sagemaker.image_uris.retrieve("forecasting-deepar", region)

    estimator = Estimator(
        image_uri=deepar_image,
        role=role_arn,
        instance_type="ml.c5.2xlarge",  # CPU is fine for DeepAR
        instance_count=1,
        sagemaker_session=sm_session,
        use_spot_instances=True,
        max_wait=14400,
        max_run=7200,
        hyperparameters={
            "time_freq":                 "D",       # Daily frequency
            "context_length":            str(context_len),
            "prediction_length":         str(horizon),
            "num_cells":                 40,
            "num_layers":                3,
            "likelihood":                "gaussian",
            "epochs":                    str(epochs),
            "mini_batch_size":           32,
            "learning_rate":             "1e-3",
            "dropout_rate":              "0.05",
            "embedding_dimension":       10,
            "num_dynamic_feat":          "auto",
            # Evaluation quantiles (gives P10, P50, P90 bands)
            "test_quantiles":            "[0.1, 0.5, 0.9]",
        },
    )
    training_step = TrainingStep(
        name="TrainDeepAR",
        estimator=estimator,
        inputs={
            "train": TrainingInput(preprocess_step.properties...train_uri, content_type="application/json"),
            "test":  TrainingInput(preprocess_step.properties...test_uri,  content_type="application/json"),
        },
    )

    # === Step 3: Backtesting evaluation ===
    eval_processor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type="ml.m5.xlarge", instance_count=1,
    )
    eval_step = ProcessingStep(
        name="BacktestEvaluation",
        processor=eval_processor,
        code="ml/scripts/ts_evaluate.py",
        inputs=[
            ProcessingInput(source=training_step.properties.ModelArtifacts.S3ModelArtifacts, destination="/opt/ml/processing/model"),
            ProcessingInput(source=preprocess_step.properties...test_uri, destination="/opt/ml/processing/test"),
        ],
        outputs=[ProcessingOutput(output_name="metrics", source="/opt/ml/processing/output")],
        job_arguments=["--target-metric", "MAPE", "--max-mape", "0.20"],  # Alert if MAPE > 20%
    )

    # === Step 4: Register model ===
    model_step = ModelStep(
        name="RegisterForecastModel",
        model_approval_status="PendingManualApproval",
        model_package_group_name=model_package_group,
        ...
    )

    return Pipeline(
        name=pipeline_name,
        parameters=[dataset_uri, horizon, algorithm, context_len, epochs],
        steps=[preprocess_step, training_step, eval_step, model_step],
        sagemaker_session=sm_session,
    )
```

---

## Anomaly Detection (RCF — Real-Time Alternative to DeepAR)

Use Random Cut Forest for real-time anomaly detection on streaming metrics (no batch training cycle needed):

```python
# SageMaker RCF — trains in minutes, serves sub-10ms
rcf_image = sagemaker.image_uris.retrieve("randomcutforest", region)

rcf_estimator = Estimator(
    image_uri=rcf_image, role=role_arn,
    instance_type="ml.m5.large", instance_count=1,
    hyperparameters={
        "num_samples_per_tree": 512,
        "num_trees": 100,
        "feature_dim": 1,
        "eval_metrics": '["accuracy", "precision_recall_fscore"]',
    },
)
# RCF output: anomaly_score per point
# Score > 3 standard deviations above mean = anomaly
# Integrate with Kinesis → Lambda → RCF endpoint → alert if score > threshold
```
