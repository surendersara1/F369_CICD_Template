# SOP — MLOps Pipeline: Time-Series Forecasting (DeepAR / Chronos / RCF)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Pipelines · DeepAR (built-in) · Chronos (HuggingFace) · Random Cut Forest · CPU training (`ml.c5.*`) · DynamoDB forecasts table

---

## 1. Purpose

- Provision a forecasting pipeline: preprocess (resample + calendar features) → DeepAR train (Spot CPU) → backtest (MAPE / sMAPE / RMSE) → register → daily forecast generation → DDB results table.
- Codify the **forecast Lambda** — invoked daily at 3 am, calls the endpoint with user series, writes predictions to DDB with quantile bands (P10/P50/P90).
- Codify the **trigger Lambda** — weekly retrain (Sunday 1 am) by starting the SageMaker pipeline with dataset URI + horizon + algorithm parameters.
- Codify **Random Cut Forest** as the real-time anomaly-detection alternative (no batch training cycle, sub-10 ms scoring, Kinesis-compatible).
- Include when the SOW mentions demand forecasting, sales prediction, capacity planning, anomaly detection in metrics, predictive maintenance, or IoT sensor forecasting.

**Model selection:**

| Scenario | Model | Why |
|---|---|---|
| Hundreds of related series (SKUs, stores) | **DeepAR** (SageMaker built-in) | Trains on all series jointly; cold-start handling |
| Any series, zero-shot | **Chronos** (Amazon, HuggingFace) | Pre-trained, no training needed |
| Single series, interpretable | **Prophet** (Meta) | Explainable trend / seasonality decomposition |
| Multi-variate | **TFT** (Temporal Fusion Transformer) | Best accuracy, GPU training |
| Real-time streaming anomaly | **Random Cut Forest** (SageMaker built-in) | Online, no batch cycle |

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns forecast Lambda + DDB + trigger Lambda + pipeline + schedules + endpoint | **§3 Monolith Variant** |
| `DataStack` owns DDB, `MLPlatformStack` owns Model Group, `ServingStack` owns endpoint, `TimeSeriesStack` owns the two Lambdas + schedules | **§4 Micro-Stack Variant** |

**Why the split matters.** The forecast Lambda needs `sagemaker:InvokeEndpoint` on the time-series endpoint (owned by `ServingStack`) and `dynamodb:Put/Get` on the results table. Monolith: local grants are safe. Micro-stack: the DDB table lives in `TimeSeriesStack` (keep L2 grants local) and cross-stack grants are identity-side only with ARNs read from SSM. KMS on DDB follows the fifth non-negotiable (local CMK or SQS_MANAGED).

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Architecture

```
DAILY (3 am):
  EventBridge → ForecastFn → InvokeEndpoint(user_series)
    → quantile predictions (P10/P50/P90) → DDB results table

WEEKLY (Sunday 1 am, prod only):
  EventBridge → TSPipelineTrigger → StartPipelineExecution
    └── preprocess → DeepAR train (Spot c5.2xlarge) → backtest → register

REAL-TIME (optional — RCF path):
  Kinesis → Lambda → InvokeEndpoint(rcf) → anomaly_score → alert if > 3σ
```

### 3.2 CDK — `_create_timeseries_pipeline` method body

```python
from aws_cdk import (
    CfnOutput, Duration,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_sagemaker as sagemaker,
)


def _create_timeseries_pipeline(self, stage_name: str) -> None:
    """
    Time Series Forecasting ML Pipeline.

    Assumes self.{lake_buckets, ddb_tables} set. `ddb_tables` contains the
    forecast-results table created earlier in this stack.

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
        runtime=_lambda.Runtime.PYTHON_3_13,
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
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/ts_pipeline_trigger"),
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
        rule_name=f"{{project_name}}-ts-retrain-{stage_name}",
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

### 3.3 Pipeline trigger handler (`lambda/ts_pipeline_trigger/index.py`)

```python
"""Start time-series training pipeline."""
import boto3, logging, os

logger = logging.getLogger()
logger.setLevel(logging.INFO)
sm = boto3.client('sagemaker')


def handler(event, context):
    params = [
        {"Name": "DatasetS3Uri",    "Value": event.get('dataset_uri', os.environ['DEFAULT_DATASET'])},
        {"Name": "ForecastHorizon", "Value": str(event.get('horizon',  14))},
        {"Name": "ModelAlgorithm",  "Value": event.get('algorithm',    'deepar')},
        {"Name": "ContextLength",   "Value": str(event.get('context',  90))},
        {"Name": "Epochs",          "Value": str(event.get('epochs',  300))},
    ]
    resp = sm.start_pipeline_execution(
        PipelineName=os.environ['PIPELINE_NAME'],
        PipelineParameters=params,
        ClientRequestToken=context.aws_request_id,
    )
    logger.info(f"Started TS pipeline: {resp['PipelineExecutionArn']}")
    return {"execution_arn": resp['PipelineExecutionArn']}
```

### 3.4 Pipeline definition (`ml/pipelines/timeseries_pipeline.py`)

```python
# ml/pipelines/timeseries_pipeline.py
from sagemaker.estimator import Estimator
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep, ProcessingInput, ProcessingOutput
from sagemaker.workflow.parameters import ParameterString, ParameterInteger
from sagemaker.workflow.model_step import ModelStep
from sagemaker.sklearn.processing import SKLearnProcessor
from sagemaker.inputs import TrainingInput
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
            "train": TrainingInput(preprocess_step.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri, content_type="application/json"),
            "test":  TrainingInput(preprocess_step.properties.ProcessingOutputConfig.Outputs["test"].S3Output.S3Uri,  content_type="application/json"),
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
            ProcessingInput(source=preprocess_step.properties.ProcessingOutputConfig.Outputs["test"].S3Output.S3Uri, destination="/opt/ml/processing/test"),
        ],
        outputs=[ProcessingOutput(output_name="metrics", source="/opt/ml/processing/output")],
        job_arguments=["--target-metric", "MAPE", "--max-mape", "0.20"],  # Alert if MAPE > 20%
    )

    # === Step 4: Register model ===
    model_step = ModelStep(
        name="RegisterForecastModel",
        model_approval_status="PendingManualApproval",
        model_package_group_name=model_package_group,
    )

    return Pipeline(
        name=pipeline_name,
        parameters=[dataset_uri, horizon, algorithm, context_len, epochs],
        steps=[preprocess_step, training_step, eval_step, model_step],
        sagemaker_session=sm_session,
    )
```

### 3.5 Random Cut Forest — real-time anomaly alternative

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

### 3.6 Monolith gotchas

- **DeepAR JSONL input format** — each line is `{"start": "2024-01-01", "target": [v1, v2, ...]}` per series. `ts_preprocess.py` must emit this exact schema; CSV fails.
- **`num_dynamic_feat="auto"`** — DeepAR detects feature count from input; mismatched train/test feature counts fail silently and produce garbage forecasts.
- **MAPE divides by zero** on series with zero values — `ts_evaluate.py` should fall back to sMAPE when any actual = 0.
- **Spot `max_wait ≥ max_run`** is a correctness constraint; exceed it and DeepAR training fails immediately.
- **DynamoDB write amplification** — if the forecast Lambda writes N horizons × M series on every run, PAY_PER_REQUEST costs rise fast. Consider batch writes (`BatchWriteItem` 25 items/call) for M > 100 series.
- **Daily schedule drift** — `cron(hour="3", minute="0")` is UTC; confirm business timezone offsets or anchor to local-time quartz cron in EventBridge Scheduler instead.
- **RCF `feature_dim`** must match the number of dimensions per record; univariate streams = 1, multivariate ≥ 2.

---

## 4. Micro-Stack Variant

**Use when:** `TimeSeriesStack` is separate from `ServingStack` (owns endpoint) and `MLPlatformStack` (owns Model Group + SageMaker role).

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)` via `_LAMBDAS_ROOT`.
2. **Never call `endpoint.grant_invoke(fn)`** across stacks — identity-side `sagemaker:InvokeEndpoint` on the endpoint ARN (read from SSM).
3. **Never target cross-stack queues** — schedules target local Lambdas.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on the DDB table — use a local CMK.

### 4.2 `TimeSeriesStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class TimeSeriesStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        ts_endpoint_name_ssm: str,
        pipeline_name_ssm: str,
        dataset_bucket_name_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-timeseries-{stage_name}", **kwargs)

        endpoint_name   = ssm.StringParameter.value_for_string_parameter(self, ts_endpoint_name_ssm)
        pipeline_name   = ssm.StringParameter.value_for_string_parameter(self, pipeline_name_ssm)
        dataset_bucket  = ssm.StringParameter.value_for_string_parameter(self, dataset_bucket_name_ssm)

        # Local CMK for DDB results table
        cmk = kms.Key(self, "TSKey",
            alias=f"alias/{{project_name}}-ts-{stage_name}",
            enable_key_rotation=True, rotation_period=Duration.days(365),
        )

        results_table = ddb.Table(self, "ForecastResults",
            table_name=f"{{project_name}}-forecast-results-{stage_name}",
            partition_key=ddb.Attribute(name="series_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="forecast_ts",   type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        )

        # Forecast Lambda
        fc_log = logs.LogGroup(self, "ForecastLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-forecast-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        forecast_fn = _lambda.Function(self, "ForecastFn",
            function_name=f"{{project_name}}-forecast-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "forecast_service")),
            timeout=Duration.minutes(5),
            memory_size=512,
            log_group=fc_log,
            environment={
                "ENDPOINT_NAME": endpoint_name,
                "RESULTS_TABLE": results_table.table_name,
                "HORIZON":       "14",
                "GRANULARITY":   "D",
            },
        )
        results_table.grant_read_write_data(forecast_fn)       # same-stack L2 safe
        forecast_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"],
        ))
        iam.PermissionsBoundary.of(forecast_fn.role).apply(permission_boundary)

        events.Rule(self, "DailyForecastSchedule",
            rule_name=f"{{project_name}}-daily-forecast-{stage_name}",
            schedule=events.Schedule.cron(hour="3", minute="0"),
            targets=[targets.LambdaFunction(forecast_fn)],
            enabled=stage_name != "ds",
        )

        # Pipeline trigger Lambda
        pl_log = logs.LogGroup(self, "TriggerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-ts-pipeline-trigger-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        ts_trigger_fn = _lambda.Function(self, "TSPipelineTriggerFn",
            function_name=f"{{project_name}}-ts-pipeline-trigger-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "ts_pipeline_trigger")),
            timeout=Duration.seconds(30),
            log_group=pl_log,
            environment={
                "PIPELINE_NAME":   pipeline_name,
                "DEFAULT_DATASET": f"s3://{dataset_bucket}/timeseries/",
            },
        )
        ts_trigger_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:StartPipelineExecution", "sagemaker:DescribePipelineExecution"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:pipeline/{pipeline_name}"],
        ))
        iam.PermissionsBoundary.of(ts_trigger_fn.role).apply(permission_boundary)

        events.Rule(self, "TSRetrainSchedule",
            rule_name=f"{{project_name}}-ts-retrain-{stage_name}",
            schedule=events.Schedule.cron(hour="1", minute="0", week_day="SUN"),
            targets=[targets.LambdaFunction(ts_trigger_fn)],
            enabled=stage_name == "prod",
        )

        cdk.CfnOutput(self, "ForecastFnArn",
            value=forecast_fn.function_arn,
            export_name=f"{{project_name}}-forecast-fn-{stage_name}",
        )
        cdk.CfnOutput(self, "ForecastResultsTableName",
            value=results_table.table_name,
            export_name=f"{{project_name}}-forecast-results-table-{stage_name}",
        )
```

### 4.3 Micro-stack gotchas

- **Local CMK on DDB** keeps the fifth non-negotiable honoured; the lake CMK stays in its owning stack.
- **Dual EventBridge rules + enabled flags** — `DailyForecastSchedule` runs in staging+prod; `TSRetrainSchedule` only in prod. The flags avoid accidental staging retraining runs.
- **Pipeline ARN from SSM token** — formatted as `arn:aws:sagemaker:{region}:{account}:pipeline/{pipeline_name}`. Don't attempt `split` on unresolved tokens.
- **`results_table` sort key** (`forecast_ts`) allows per-series time-range queries without scanning the whole series; make sure the Lambda writes both keys.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx | §4 Micro-Stack |
| Swap DeepAR → Chronos (zero-shot) | Change `ModelAlgorithm` parameter; skip training step |
| Add exogenous regressors | Extend preprocess to include `dynamic_feat`; re-train |
| Move to hourly granularity | Change `"D"` → `"H"`, shorten context, retrain |
| Switch to real-time anomaly (RCF) | Add Kinesis → Lambda → RCF endpoint path (§3.5) |
| Raise forecast frequency | Change EventBridge schedule to 4× daily |

---

## 6. Worked example — TimeSeriesStack synthesizes

Save as `tests/sop/test_MLOPS_PIPELINE_TIMESERIES.py`. Offline.

```python
"""SOP verification — TimeSeriesStack synthesizes forecast + trigger + DDB + schedules."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_timeseries_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.timeseries_stack import TimeSeriesStack
    stack = TimeSeriesStack(
        app, stage_name="prod",
        ts_endpoint_name_ssm="/test/ml/ts_endpoint_name",
        pipeline_name_ssm="/test/ml/ts_pipeline_name",
        dataset_bucket_name_ssm="/test/lake/processed_bucket",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 2)
    t.resource_count_is("AWS::DynamoDB::Table",  1)
    t.resource_count_is("AWS::Events::Rule",     2)
    t.resource_count_is("AWS::KMS::Key",         1)
```

---

## 7. References

- `docs/template_params.md` — `TS_ENDPOINT_NAME_SSM`, `TS_PIPELINE_NAME_SSM`, `TS_FORECAST_HORIZON`, `TS_GRANULARITY`, `TS_MAX_MAPE`
- `docs/Feature_Roadmap.md` — feature IDs `ML-55` (time-series forecasting), `ML-56` (daily forecast schedule), `ML-57` (RCF anomaly detection)
- DeepAR: https://docs.aws.amazon.com/sagemaker/latest/dg/deepar.html
- Random Cut Forest: https://docs.aws.amazon.com/sagemaker/latest/dg/randomcutforest.html
- Chronos on HuggingFace: https://huggingface.co/amazon/chronos-t5-large
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (Model Group), `MLOPS_SAGEMAKER_SERVING` (endpoint deployer), `LAYER_DATA` (DDB), `LAYER_BACKEND_LAMBDA` (five non-negotiables), `EVENT_DRIVEN_PATTERNS` (schedules)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `TimeSeriesStack` reads endpoint name + pipeline name + dataset bucket via SSM; identity-side `sagemaker:InvokeEndpoint` + `sagemaker:StartPipelineExecution` scoped to respective ARNs; local CMK for DDB results table (5th non-negotiable). Extracted inline Lambda trigger to `lambda/ts_pipeline_trigger/` asset. Kept full DeepAR pipeline + RCF alternative. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — DeepAR forecasting pipeline, daily forecast Lambda, weekly retrain trigger, RCF anomaly detection. |
