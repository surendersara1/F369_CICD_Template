# SOP — SageMaker Model Monitor Advanced (data drift · model quality · bias drift · feature attribution drift)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Model Monitor with all 4 monitor types: Data Quality (statistical drift) · Model Quality (accuracy/F1/regression-error) · Bias Drift (demographic parity drift) · Feature Attribution Drift (SHAP-shift) · Scheduled monitoring jobs · CloudWatch alarms · auto-rollback wiring

---

## 1. Purpose

- Codify the **4-type Model Monitor pattern** that the existing `MLOPS_SAGEMAKER_SERVING` only partially covers (data quality only).
- Cover **Model Quality** — requires labeled ground truth merge job; for classification + regression workloads.
- Cover **Bias Drift** — pre-calc baseline w/ Clarify, then ongoing comparison; for fairness governance.
- Cover **Feature Attribution Drift** — SHAP shift detection; flags when "what's driving predictions" changes.
- Provide the **baseline calibration** workflow + the **scheduling** + alarm wiring.
- This is the **drift-detection deep-dive specialisation**. `MLOPS_SAGEMAKER_SERVING` covers only data quality; this expands to the full 4 monitor types.

When the SOW signals: "drift detection", "model retraining triggers", "bias monitoring", "feature attribution monitoring", "GxP-grade ML monitoring".

---

## 2. Decision tree — which monitor type(s) to enable

```
Workload type?
├── Classification or regression with available ground truth → Data + Model + Bias + Attribution (all 4)
├── LLM / generative (no ground truth) → Data Quality only (no model quality possible)
├── Compliance-sensitive (HIPAA, fair lending) → Bias Drift mandatory
└── High-stakes (medical, finance) → All 4 + manual review monthly

Cadence?
├── Real-time fraud detection → hourly monitoring jobs
├── Standard prod model → daily
├── Slow-drift workload (e.g. brand sentiment) → weekly
└── Rapid retraining loop → run on every batch
```

---

## 3. The 4 monitor types — quick reference

| Monitor | Detects | Requires | Output |
|---|---|---|---|
| **Data Quality** | Input feature distribution shift (mean, std, KL divergence) | Baseline statistics from training data | `constraint_violations.json` + CloudWatch metric |
| **Model Quality** | Output accuracy / F1 / regression error degradation | Baseline + ground truth merge job | Same |
| **Bias Drift** | Demographic parity / disparate impact shift | Clarify baseline | Same + Clarify dashboard |
| **Feature Attribution Drift** | "Why" the model decides differs from baseline (SHAP-based) | SHAP baseline from Clarify | Same |

---

## 4. CDK — `_create_full_model_monitor()`

```python
from aws_cdk import (
    aws_iam as iam,
    aws_sagemaker as sagemaker,
    aws_s3 as s3,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    Duration,
)


def _create_full_model_monitor(self, stage: str) -> None:
    """Creates 4-type Model Monitor: Data Quality + Model Quality + Bias + Feature Attribution."""

    # A) S3 bucket for monitor artifacts (baselines, reports, ground truth)
    self.monitor_bucket = s3.Bucket(self, "MonitorBucket",
        bucket_name=f"{{project_name}}-monitor-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
    )

    # B) Monitor execution role
    self.monitor_role = iam.Role(self, "MonitorRole",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.monitor_bucket.grant_read_write(self.monitor_role)
    self.kms_key.grant_encrypt_decrypt(self.monitor_role)
    self.endpoint_data_capture_bucket.grant_read(self.monitor_role)

    # C) Data Capture on the endpoint (must be enabled at endpoint config)
    # (assumed configured in MLOPS_SAGEMAKER_SERVING; we just reference)
    endpoint_name = ssm.StringParameter.value_for_string_parameter(
        self, f"/{{project_name}}/{stage}/serving/endpoint-name")

    # D) Data Quality Monitor — baseline → schedule
    dq_baseline_job = sagemaker.CfnDataQualityJobDefinition(self, "DqBaseline",
        job_definition_name=f"{{project_name}}-dq-baseline-{stage}",
        data_quality_app_specification=sagemaker.CfnDataQualityJobDefinition.DataQualityAppSpecificationProperty(
            image_uri=f"159807026194.dkr.ecr.{self.region}.amazonaws.com/sagemaker-model-monitor-analyzer",
        ),
        data_quality_baseline_config=sagemaker.CfnDataQualityJobDefinition.DataQualityBaselineConfigProperty(
            constraints_resource=sagemaker.CfnDataQualityJobDefinition.ConstraintsResourceProperty(
                s3_uri=f"s3://{self.monitor_bucket.bucket_name}/baselines/data-quality/constraints.json",
            ),
            statistics_resource=sagemaker.CfnDataQualityJobDefinition.StatisticsResourceProperty(
                s3_uri=f"s3://{self.monitor_bucket.bucket_name}/baselines/data-quality/statistics.json",
            ),
        ),
        data_quality_job_input=sagemaker.CfnDataQualityJobDefinition.DataQualityJobInputProperty(
            endpoint_input=sagemaker.CfnDataQualityJobDefinition.EndpointInputProperty(
                endpoint_name=endpoint_name,
                local_path="/opt/ml/processing/input",
                s3_input_mode="File",
                s3_data_distribution_type="FullyReplicated",
            ),
        ),
        data_quality_job_output_config=sagemaker.CfnDataQualityJobDefinition.MonitoringOutputConfigProperty(
            monitoring_outputs=[sagemaker.CfnDataQualityJobDefinition.MonitoringOutputProperty(
                s3_output=sagemaker.CfnDataQualityJobDefinition.S3OutputProperty(
                    s3_uri=f"s3://{self.monitor_bucket.bucket_name}/reports/data-quality/",
                    local_path="/opt/ml/processing/output",
                    s3_upload_mode="EndOfJob",
                ),
            )],
            kms_key_id=self.kms_key.key_arn,
        ),
        job_resources=sagemaker.CfnDataQualityJobDefinition.MonitoringResourcesProperty(
            cluster_config=sagemaker.CfnDataQualityJobDefinition.ClusterConfigProperty(
                instance_type="ml.m5.xlarge",
                instance_count=1,
                volume_size_in_gb=30,
            ),
        ),
        role_arn=self.monitor_role.role_arn,
    )

    # E) Schedule — daily Data Quality job
    sagemaker.CfnMonitoringSchedule(self, "DqSchedule",
        monitoring_schedule_name=f"{{project_name}}-dq-schedule-{stage}",
        monitoring_schedule_config=sagemaker.CfnMonitoringSchedule.MonitoringScheduleConfigProperty(
            schedule_config=sagemaker.CfnMonitoringSchedule.ScheduleConfigProperty(
                schedule_expression="cron(0 8 ? * * *)",     # 8 AM UTC daily
            ),
            monitoring_job_definition_name=dq_baseline_job.job_definition_name,
            monitoring_type="DataQuality",
        ),
    )

    # F) Model Quality Monitor — needs ground truth merger
    mq_baseline_job = sagemaker.CfnModelQualityJobDefinition(self, "MqBaseline",
        job_definition_name=f"{{project_name}}-mq-baseline-{stage}",
        model_quality_app_specification=sagemaker.CfnModelQualityJobDefinition.ModelQualityAppSpecificationProperty(
            image_uri=f"159807026194.dkr.ecr.{self.region}.amazonaws.com/sagemaker-model-monitor-analyzer",
            problem_type="BinaryClassification",  # or MulticlassClassification, Regression
        ),
        model_quality_baseline_config=sagemaker.CfnModelQualityJobDefinition.ModelQualityBaselineConfigProperty(
            constraints_resource=sagemaker.CfnModelQualityJobDefinition.ConstraintsResourceProperty(
                s3_uri=f"s3://{self.monitor_bucket.bucket_name}/baselines/model-quality/constraints.json",
            ),
        ),
        model_quality_job_input=sagemaker.CfnModelQualityJobDefinition.ModelQualityJobInputProperty(
            endpoint_input=sagemaker.CfnModelQualityJobDefinition.EndpointInputProperty(
                endpoint_name=endpoint_name,
                local_path="/opt/ml/processing/input/endpoint",
                inference_attribute="probability",        # field in the response with score
                probability_attribute="probability",
                probability_threshold_attribute=0.5,
                s3_input_mode="File",
                s3_data_distribution_type="FullyReplicated",
            ),
            ground_truth_s3_input=sagemaker.CfnModelQualityJobDefinition.MonitoringGroundTruthS3InputProperty(
                s3_uri=f"s3://{self.monitor_bucket.bucket_name}/ground-truth/",
            ),
        ),
        model_quality_job_output_config=...,            # similar to DQ
        job_resources=...,
        role_arn=self.monitor_role.role_arn,
    )

    # G) Bias Drift Monitor (uses Clarify)
    sagemaker.CfnModelBiasJobDefinition(self, "BiasMonitor",
        job_definition_name=f"{{project_name}}-bias-{stage}",
        model_bias_app_specification=sagemaker.CfnModelBiasJobDefinition.ModelBiasAppSpecificationProperty(
            image_uri=f"205585389593.dkr.ecr.{self.region}.amazonaws.com/sagemaker-clarify-processing",
            config_uri=f"s3://{self.monitor_bucket.bucket_name}/baselines/bias/analysis_config.json",
        ),
        model_bias_baseline_config=sagemaker.CfnModelBiasJobDefinition.ModelBiasBaselineConfigProperty(
            baselining_job_name="bias-baselining-2026-04-01",
            constraints_resource=sagemaker.CfnModelBiasJobDefinition.ConstraintsResourceProperty(
                s3_uri=f"s3://{self.monitor_bucket.bucket_name}/baselines/bias/constraints.json",
            ),
        ),
        model_bias_job_input=...,
        model_bias_job_output_config=...,
        job_resources=...,
        role_arn=self.monitor_role.role_arn,
    )

    # H) Feature Attribution Drift Monitor
    sagemaker.CfnModelExplainabilityJobDefinition(self, "AttributionMonitor",
        job_definition_name=f"{{project_name}}-attribution-{stage}",
        model_explainability_app_specification=sagemaker.CfnModelExplainabilityJobDefinition.ModelExplainabilityAppSpecificationProperty(
            image_uri=f"205585389593.dkr.ecr.{self.region}.amazonaws.com/sagemaker-clarify-processing",
            config_uri=f"s3://{self.monitor_bucket.bucket_name}/baselines/attribution/analysis_config.json",
        ),
        model_explainability_baseline_config=sagemaker.CfnModelExplainabilityJobDefinition.ModelExplainabilityBaselineConfigProperty(
            baselining_job_name="attribution-baselining-2026-04-01",
            constraints_resource=...,
        ),
        model_explainability_job_input=...,
        model_explainability_job_output_config=...,
        job_resources=...,
        role_arn=self.monitor_role.role_arn,
    )

    # I) CloudWatch alarms on each monitor's violation metric
    for monitor in ["DataQuality", "ModelQuality", "ModelBias", "FeatureAttribution"]:
        cloudwatch.Alarm(self, f"{monitor}DriftAlarm",
            metric=cloudwatch.Metric(
                namespace="aws/sagemaker/Endpoints/data-metrics",
                metric_name="feature_baseline_drift_count",
                dimensions_map={
                    "Endpoint":         endpoint_name,
                    "MonitoringSchedule": f"{{project_name}}-{monitor.lower()}-schedule-{stage}",
                },
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
            alarm_description=f"{monitor} drift detected",
        )

    # J) Auto-rollback Lambda — fires on alarm, reverts endpoint to previous variant
    rollback_fn = lambda_.Function(self, "AutoRollbackFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "auto_rollback")),
        timeout=Duration.minutes(5),
        environment={"ENDPOINT_NAME": endpoint_name},
    )
    rollback_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:UpdateEndpoint", "sagemaker:DescribeEndpoint",
                 "sagemaker:DescribeEndpointConfig"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{endpoint_name}",
                   f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint-config/*"],
    ))
    self.alert_topic.add_subscription(subs.LambdaSubscription(rollback_fn))
```

---

## 5. Baseline calibration workflow

Before scheduling, calibrate baselines:

```python
"""scripts/calibrate_baselines.py — run once on training data."""
from sagemaker import Session
from sagemaker.model_monitor import (
    DefaultModelMonitor, ModelQualityMonitor,
    ModelBiasMonitor, ModelExplainabilityMonitor,
    DatasetFormat,
)
from sagemaker.clarify import (
    BiasConfig, DataConfig, ModelConfig, SHAPConfig,
)


def calibrate_all():
    session = Session()
    role = "ROLE_ARN"

    # 1. Data Quality baseline
    dq_monitor = DefaultModelMonitor(role=role, instance_count=1, instance_type="ml.m5.xlarge")
    dq_monitor.suggest_baseline(
        baseline_dataset="s3://qra-curated/training-data/v3/",
        dataset_format=DatasetFormat.csv(header=True),
        output_s3_uri=f"s3://qra-monitor-prod/baselines/data-quality/",
    )

    # 2. Model Quality baseline (requires ground truth labels)
    mq_monitor = ModelQualityMonitor(role=role, instance_count=1, instance_type="ml.m5.xlarge")
    mq_monitor.suggest_baseline(
        baseline_dataset="s3://qra-curated/training-data/v3/with-labels.csv",
        dataset_format=DatasetFormat.csv(header=True),
        problem_type="BinaryClassification",
        inference_attribute="probability",
        probability_attribute="probability",
        ground_truth_attribute="label",
        output_s3_uri=f"s3://qra-monitor-prod/baselines/model-quality/",
    )

    # 3. Bias baseline (Clarify)
    bias_monitor = ModelBiasMonitor(role=role, instance_count=1, instance_type="ml.m5.xlarge")
    bias_monitor.suggest_baseline(
        data_config=DataConfig(
            s3_data_input_path="s3://qra-curated/training-data/v3/",
            s3_output_path=f"s3://qra-monitor-prod/baselines/bias/",
            label="label",
            headers=["age", "income", "gender", "race", "label"],
            dataset_type="text/csv",
        ),
        bias_config=BiasConfig(
            label_values_or_threshold=[1],
            facet_name="gender",
            facet_values_or_threshold=[0],
            group_name="age",
        ),
        model_config=ModelConfig(
            model_name="qra-prod-model",
            instance_count=1,
            instance_type="ml.m5.xlarge",
        ),
        model_predicted_label_config=ModelPredictedLabelConfig(
            probability_threshold=0.5,
        ),
    )

    # 4. Feature Attribution baseline (SHAP)
    expl_monitor = ModelExplainabilityMonitor(role=role, instance_count=1, instance_type="ml.m5.xlarge")
    expl_monitor.suggest_baseline(
        data_config=DataConfig(...),  # same shape as bias
        explainability_config=SHAPConfig(
            baseline=[[0]*4],          # all-zero baseline for SHAP
            num_samples=100,
            agg_method="mean_abs",
        ),
        model_config=ModelConfig(...),
    )


if __name__ == "__main__":
    calibrate_all()
```

---

## 6. Five non-negotiables

1. **Data Capture must be enabled BEFORE deploying.** Without it, monitors have nothing to compare. Set `DataCaptureConfig` on endpoint config — see `MLOPS_SAGEMAKER_SERVING`.
2. **Baselines from training data, not synthetic.** Synthetic baselines miss real-world distribution. Always re-baseline when retraining.
3. **Ground truth merger latency is the model-quality cap.** If labels arrive 7 days later, model quality detection lag is 7 days minimum.
4. **Bias monitor needs `facet_name` columns in production traffic.** Strip PII from training but keep facet columns; otherwise bias monitor can't compute disparity.
5. **Auto-rollback should only trigger on multi-violation patterns, not single alarms.** Add an `evaluation_periods=3` (3 consecutive violations) to avoid flapping.

---

## 7. References

- AWS docs:
  - [Model Monitor overview](https://docs.aws.amazon.com/sagemaker/latest/dg/model-monitor.html)
  - [Data Quality](https://docs.aws.amazon.com/sagemaker/latest/dg/model-monitor-data-quality.html)
  - [Model Quality](https://docs.aws.amazon.com/sagemaker/latest/dg/model-monitor-model-quality.html)
  - [Bias Drift](https://docs.aws.amazon.com/sagemaker/latest/dg/clarify-model-monitor-bias-drift.html)
  - [Feature Attribution Drift](https://docs.aws.amazon.com/sagemaker/latest/dg/clarify-model-monitor-feature-attribution-drift.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_SERVING` — data capture + Data Quality monitor (basic)
  - `MLOPS_CLARIFY_EXPLAINABILITY` — Clarify bias + SHAP setup (one-shot, not monitor)
  - `MLOPS_LINEAGE_TRACKING` — links baselines to production deployments

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — full 4-monitor pattern (Data + Model + Bias + Attribution drift). CDK for all 4 job definitions + schedules + alarms + auto-rollback. Baseline calibration script. Created Wave 7 (2026-04-26). |
