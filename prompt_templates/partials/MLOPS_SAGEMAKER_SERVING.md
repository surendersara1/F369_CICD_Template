# SOP — MLOps SageMaker Serving, Monitoring & Drift Detection

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws_sagemaker` L1 constructs · `aws_applicationautoscaling` · CloudWatch Alarms · SNS · SageMaker Model Monitor · Python 3.13 deployer Lambda

---

## 1. Purpose

- Provision SageMaker serving infrastructure across four modes (real-time, serverless, async, batch — batch covered separately in `MLOPS_BATCH_TRANSFORM`).
- Codify the **multi-variant endpoint config** (Champion + Challenger) enabling A/B testing and shadow mode — zero initial weight on Challenger; deployer Lambda shifts traffic on approval.
- Codify **application auto-scaling** with aggressive scale-out (1–2× capacity per step), conservative scale-in, and `InvocationsPerInstance` as the primary target metric.
- Provision **Model Monitor** (data quality daily job) + **endpoint error alarm** that feeds the deployer Lambda's **auto-rollback** config.
- Provide the **Model Deployer Lambda** triggered by EventBridge (`ModelApprovalStatus=Approved` from `MLOPS_SAGEMAKER_TRAINING`), with Blue/Green deployment using `LINEAR` traffic shift in prod and `ALL_AT_ONCE` elsewhere.
- Include when the SOW mentions inference endpoints, model deployment, A/B testing, champion/challenger, shadow mode, or production ML serving.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns endpoints + Model Monitor + deployer Lambda + data-lake buckets + KMS | **§3 Monolith Variant** |
| Data lake in `DataLakeStack`, `MLPlatformStack` owns the SageMaker role + Model Registry, `ServingStack` owns endpoints + Monitor + deployer | **§4 Micro-Stack Variant** |

**Why the split matters.** The deployer Lambda needs `iam:PassRole` on the SageMaker execution role (owned by `MLPlatformStack`), `sagemaker:*` on endpoints (local), and `s3:PutObject` on capture / monitor S3 prefixes (owned by data-lake). Monolith: `role.grant_pass_role(fn)` and `bucket.grant_write(fn)` work. Cross-stack: use identity-side `PolicyStatement` with role ARN and bucket ARN read from SSM. Model Monitor capture destination (`kms_key_id`, `s3_uri`) must use string ARNs, not cross-stack construct refs.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Serving mode picker

| Mode                     | Latency         | Cost                        | Use Case                                          |
| ------------------------ | --------------- | --------------------------- | ------------------------------------------------- |
| **Real-time Endpoint**   | < 100 ms        | Per instance-hour           | Low-latency API inference (fraud, recs)           |
| **Serverless Inference** | 100 ms – 2 s    | Per invocation              | Spiky / infrequent traffic, dev / staging         |
| **Async Inference**      | Seconds–minutes | Queue-based, large payloads | NLP on long documents, large model predictions    |
| **Batch Transform**      | Minutes–hours   | Cheapest                    | Offline scoring millions of records at once       |

### 3.2 CDK — real-time endpoint (multi-variant) + auto-scaling

```python
from aws_cdk import (
    Duration, CfnOutput,
    aws_sagemaker as sagemaker,
    aws_applicationautoscaling as autoscaling,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ec2 as ec2,
)


def _create_sagemaker_serving(self, stage_name: str) -> None:
    """SageMaker serving. Assumes self.{vpc, lambda_sg, kms_key, lake_buckets,
    sagemaker_role, alert_topic} were created earlier."""

    endpoint_config_per_stage = {
        "ds":      {"instance_type": "ml.t3.medium", "instance_count": 1, "serverless": True},
        "staging": {"instance_type": "ml.m5.large",   "instance_count": 1, "serverless": False},
        "prod":    {"instance_type": "ml.m5.2xlarge", "instance_count": 2, "serverless": False},
    }
    ec = endpoint_config_per_stage.get(stage_name, endpoint_config_per_stage["ds"])

    # -- A) Serverless endpoint (DS / dev) -----------------------------------
    if ec["serverless"]:
        sagemaker.CfnEndpointConfig(
            self, "ServerlessEndpointConfig",
            endpoint_config_name=f"{{project_name}}-serverless-{stage_name}",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=f"{{project_name}}-model-{stage_name}",   # populated by deployer
                    serverless_config=sagemaker.CfnEndpointConfig.ServerlessConfigProperty(
                        memory_size_in_mb=2048,
                        max_concurrency=10,
                        provisioned_concurrency=2,
                    ),
                ),
            ],
        )
        return

    # -- B) Real-time endpoint (staging / prod, Champion + Challenger) -------
    capture_config = sagemaker.CfnEndpointConfig.DataCaptureConfigProperty(
        enable_capture=True,
        initial_sampling_percentage=20 if stage_name == "prod" else 100,
        destination_s3_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/model-monitor/captures/",
        kms_key_id=self.kms_key.key_arn,
        capture_options=[
            sagemaker.CfnEndpointConfig.CaptureOptionProperty(capture_mode="Input"),
            sagemaker.CfnEndpointConfig.CaptureOptionProperty(capture_mode="Output"),
        ],
        capture_content_type_header=sagemaker.CfnEndpointConfig.CaptureContentTypeHeaderProperty(
            json_content_types=["application/json"],
        ),
    )

    self.endpoint_config = sagemaker.CfnEndpointConfig(
        self, "RealTimeEndpointConfig",
        endpoint_config_name=f"{{project_name}}-endpoint-config-{stage_name}",
        kms_key_id=self.kms_key.key_arn,
        data_capture_config=capture_config,
        production_variants=[
            # Champion — 100% of traffic
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="Champion",
                model_name="PLACEHOLDER_TO_BE_SET_BY_DEPLOYER",
                instance_type=ec["instance_type"],
                initial_instance_count=ec["instance_count"],
                initial_variant_weight=1.0,
                routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                    routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                ),
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    status="ENABLED",
                    min_instance_count=ec["instance_count"],
                    max_instance_count=ec["instance_count"] * 4,
                ),
            ),
            # Challenger — 0% traffic, sits in shadow mode; deployer re-weights on A/B
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="Challenger",
                model_name="PLACEHOLDER_CHALLENGER",
                instance_type=ec["instance_type"],
                initial_instance_count=1,
                initial_variant_weight=0.0,
            ),
        ],
    )

    self.inference_endpoint = sagemaker.CfnEndpoint(
        self, "InferenceEndpoint",
        endpoint_name=f"{{project_name}}-inference-{stage_name}",
        endpoint_config_name=self.endpoint_config.endpoint_config_name,
    )

    # -- Auto-scaling on Champion variant (prod only) ------------------------
    if stage_name == "prod":
        scalable = autoscaling.ScalableTarget(
            self, "EndpointScalingTarget",
            service_namespace=autoscaling.ServiceNamespace.SAGEMAKER,
            resource_id=f"endpoint/{self.inference_endpoint.endpoint_name}/variant/Champion",
            scalable_dimension="sagemaker:variant:DesiredInstanceCount",
            min_capacity=2, max_capacity=20,
        )
        scalable.scale_on_metric(
            "InvocationsPerInstance",
            metric=cw.Metric(
                namespace="AWS/SageMaker",
                metric_name="InvocationsPerInstance",
                dimensions_map={
                    "EndpointName": self.inference_endpoint.endpoint_name,
                    "VariantName":  "Champion",
                },
                period=Duration.minutes(1),
                statistic="Average",
            ),
            scaling_steps=[
                autoscaling.ScalingInterval(change=-1, lower=0,  upper=30),
                autoscaling.ScalingInterval(change= 1, lower=30, upper=70),
                autoscaling.ScalingInterval(change= 2, lower=70, upper=None),
            ],
            cooldown=Duration.minutes(3),
            adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
        )
```

### 3.3 Model Monitor schedule + drift alarm

```python
sagemaker.CfnMonitoringSchedule(
    self, "DataQualityMonitor",
    monitoring_schedule_name=f"{{project_name}}-data-quality-{stage_name}",
    monitoring_schedule_config=sagemaker.CfnMonitoringSchedule.MonitoringScheduleConfigProperty(
        schedule_config=sagemaker.CfnMonitoringSchedule.ScheduleConfigProperty(
            schedule_expression="cron(0 8 * * ? *)",   # daily 08:00 UTC
        ),
        monitoring_job_definition=sagemaker.CfnMonitoringSchedule.MonitoringJobDefinitionProperty(
            monitoring_type="DataQuality",
            monitoring_inputs=[sagemaker.CfnMonitoringSchedule.MonitoringInputProperty(
                endpoint_input=sagemaker.CfnMonitoringSchedule.EndpointInputProperty(
                    endpoint_name=f"{{project_name}}-inference-{stage_name}",
                    local_path="/opt/ml/processing/input/endpoint",
                ),
            )],
            monitoring_output_config=sagemaker.CfnMonitoringSchedule.MonitoringOutputConfigProperty(
                monitoring_outputs=[sagemaker.CfnMonitoringSchedule.MonitoringOutputProperty(
                    s3_output=sagemaker.CfnMonitoringSchedule.S3OutputProperty(
                        local_path="/opt/ml/processing/output",
                        s3_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/model-monitor/reports/",
                        s3_upload_mode="EndOfJob",
                    ),
                )],
                kms_key_id=self.kms_key.key_arn,
            ),
            monitoring_resources=sagemaker.CfnMonitoringSchedule.MonitoringResourcesProperty(
                cluster_config=sagemaker.CfnMonitoringSchedule.ClusterConfigProperty(
                    instance_count=1, instance_type="ml.m5.xlarge", volume_size_in_gb=20,
                ),
            ),
            role_arn=self.sagemaker_role.role_arn,
            baseline_config=sagemaker.CfnMonitoringSchedule.BaselineConfigProperty(
                statistics_resource=sagemaker.CfnMonitoringSchedule.StatisticsResourceProperty(
                    s3_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/model-monitor/baseline/statistics.json",
                ),
                constraints_resource=sagemaker.CfnMonitoringSchedule.ConstraintsResourceProperty(
                    s3_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/model-monitor/baseline/constraints.json",
                ),
            ),
        ),
    ),
)

cw.Alarm(self, "DataDriftAlarm",
    alarm_name=f"{{project_name}}-data-drift-{stage_name}",
    metric=cw.Metric(
        namespace="aws/sagemaker/Endpoints/data-metrics",
        metric_name="feature_baseline_drift_distance",
        dimensions_map={"MonitoringSchedule": f"{{project_name}}-data-quality-{stage_name}"},
        period=Duration.hours(24),
        statistic="Maximum",
    ),
    threshold=0.5,
    evaluation_periods=1,
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
)
```

### 3.4 Model Deployer Lambda (triggered by Model Registry approval)

```python
model_deployer_fn = _lambda.Function(
    self, "ModelDeployer",
    function_name=f"{{project_name}}-model-deployer-{stage_name}",
    runtime=_lambda.Runtime.PYTHON_3_13,
    architecture=_lambda.Architecture.ARM_64,
    handler="index.handler",
    code=_lambda.Code.from_asset("lambda/model_deployer"),
    timeout=Duration.minutes(5),
    tracing=_lambda.Tracing.ACTIVE,
    vpc=self.vpc,
    vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
    security_groups=[self.lambda_sg],
    environment={
        "ENDPOINT_NAME":         f"{{project_name}}-inference-{stage_name}",
        "ENDPOINT_CONFIG_NAME":  f"{{project_name}}-endpoint-config-{stage_name}",
        "SAGEMAKER_ROLE_ARN":    self.sagemaker_role.role_arn,
        "STAGE":                 stage_name,
        "ENDPOINT_ERROR_ALARM":  f"{{project_name}}-endpoint-errors-{stage_name}",
    },
)
model_deployer_fn.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "sagemaker:CreateModel", "sagemaker:UpdateEndpoint",
        "sagemaker:DescribeEndpoint", "sagemaker:DescribeModelPackage",
        "sagemaker:CreateEndpointConfig",
    ],
    resources=[
        f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}*",
        f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint-config/{{project_name}}*",
        f"arn:aws:sagemaker:{self.region}:{self.account}:model/{{project_name}}*",
        f"arn:aws:sagemaker:{self.region}:{self.account}:model-package/{{project_name}}*",
    ],
))
# iam:PassRole scoped to the SageMaker role
model_deployer_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["iam:PassRole"],
    resources=[self.sagemaker_role.role_arn],
    conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
))
self.lambda_functions["ModelDeployer"] = model_deployer_fn
```

**Deployer handler (saved as `lambda/model_deployer/index.py`):**

```python
"""Deploy an approved model package to the SageMaker endpoint (Blue/Green)."""
import boto3, logging, os
logger = logging.getLogger(); logger.setLevel(logging.INFO)
sm = boto3.client('sagemaker')


def handler(event, context):
    model_package_arn = event['detail']['ModelPackageArn']
    endpoint_name     = os.environ['ENDPOINT_NAME']
    stage             = os.environ['STAGE']

    model_name = f"{endpoint_name}-{context.aws_request_id[:8]}"
    sm.create_model(
        ModelName=model_name,
        PrimaryContainer={"ModelPackageName": model_package_arn},
        ExecutionRoleArn=os.environ['SAGEMAKER_ROLE_ARN'],
    )

    sm.update_endpoint(
        EndpointName=endpoint_name,
        EndpointConfigName=os.environ['ENDPOINT_CONFIG_NAME'],
        DeploymentConfig={
            "BlueGreenUpdatePolicy": {
                "TrafficRoutingConfiguration": {
                    "Type":           "ALL_AT_ONCE" if stage != "prod" else "LINEAR",
                    "LinearStepSize": {"Type": "CAPACITY_PERCENT", "Value": 10} if stage == "prod" else None,
                    "WaitIntervalInSeconds": 300,
                },
                "MaximumExecutionTimeoutInSeconds": 1800,
                "TerminationWaitInSeconds":         300,
            },
            "AutoRollbackConfiguration": {
                "Alarms": [{"AlarmName": os.environ['ENDPOINT_ERROR_ALARM']}],
            },
        },
    )
    return {"statusCode": 200, "model_deployed": model_name, "endpoint": endpoint_name}
```

### 3.5 Endpoint error + latency alarms (drive rollback + paging)

```python
cw.Alarm(self, "EndpointErrorAlarm",
    alarm_name=f"{{project_name}}-endpoint-errors-{stage_name}",
    metric=cw.Metric(
        namespace="AWS/SageMaker",
        metric_name="ModelError",
        dimensions_map={"EndpointName": f"{{project_name}}-inference-{stage_name}"},
        period=Duration.minutes(5), statistic="Sum",
    ),
    threshold=10, evaluation_periods=2,
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
)

cw.Alarm(self, "EndpointLatencyAlarm",
    alarm_name=f"{{project_name}}-endpoint-latency-{stage_name}",
    metric=cw.Metric(
        namespace="AWS/SageMaker",
        metric_name="ModelLatency",
        dimensions_map={
            "EndpointName": f"{{project_name}}-inference-{stage_name}",
            "VariantName":  "Champion",
        },
        period=Duration.minutes(5), statistic="p99",
    ),
    threshold=2000, evaluation_periods=3,           # [Claude: adjust per SOW SLA]
    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
)
```

### 3.6 Monolith gotchas

- **Challenger variant with `weight=0`** receives ZERO traffic unless the deployer Lambda re-weights. Shadow mode (mirrored copies) is a separate `ShadowProductionVariant` — not a weight-0 prod variant.
- **`resources=["*"]` on the deployer** is a code-smell. Scope by name prefix (as §3.4 does) unless your org has strict SCPs and the wildcard is mandated.
- **Capture sampling percentage** is 100 % in staging; drop to 20 % in prod to keep capture-bucket costs bounded.
- **Baseline statistics / constraints** — the Monitor schedule references S3 files that must exist before the first run. They're produced by `CreateDataQualityJobDefinition` offline (see `MLOPS_CLARIFY_EXPLAINABILITY`).
- **`AutoRollbackConfiguration.Alarms`** must exist when `update_endpoint` is called. Alarm and deployer must be deployed together (monolith) or the deployer must depend on the alarm (micro-stack).

---

## 4. Micro-Stack Variant

**Use when:** production layout — data lake, ML platform, serving are separate stacks.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `bucket.grant_write(deployer_fn)`** across stacks — identity-side `PolicyStatement` on the Lambda role.
3. **Never target cross-stack queues** with `targets.SqsQueue`.
4. **Never split bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on endpoint config / monitor outputs when the key is from another stack — use the KMS ARN string.

### 4.2 `ServingStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration,
    aws_applicationautoscaling as autoscaling,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sagemaker as sagemaker,
    aws_sns as sns,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class ServingStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        alert_topic: sns.ITopic,
        # Cross-stack names / ARNs (from SSM)
        sagemaker_role_arn_ssm: str,
        lake_bucket_curated_ssm: str,
        lake_key_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-serving-{stage_name}", **kwargs)

        sagemaker_role_arn = ssm.StringParameter.value_for_string_parameter(self, sagemaker_role_arn_ssm)
        curated_bucket     = ssm.StringParameter.value_for_string_parameter(self, lake_bucket_curated_ssm)
        lake_key_arn       = ssm.StringParameter.value_for_string_parameter(self, lake_key_arn_ssm)

        capture = sagemaker.CfnEndpointConfig.DataCaptureConfigProperty(
            enable_capture=True,
            initial_sampling_percentage=20 if stage_name == "prod" else 100,
            destination_s3_uri=f"s3://{curated_bucket}/model-monitor/captures/",
            kms_key_id=lake_key_arn,                       # STRING (5th non-negotiable)
            capture_options=[
                sagemaker.CfnEndpointConfig.CaptureOptionProperty(capture_mode="Input"),
                sagemaker.CfnEndpointConfig.CaptureOptionProperty(capture_mode="Output"),
            ],
            capture_content_type_header=sagemaker.CfnEndpointConfig.CaptureContentTypeHeaderProperty(
                json_content_types=["application/json"],
            ),
        )

        endpoint_config = sagemaker.CfnEndpointConfig(
            self, "RealTimeEndpointConfig",
            endpoint_config_name=f"{{project_name}}-endpoint-config-{stage_name}",
            kms_key_id=lake_key_arn,
            data_capture_config=capture,
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="Champion",
                    model_name="PLACEHOLDER_TO_BE_SET_BY_DEPLOYER",
                    instance_type="ml.m5.2xlarge",
                    initial_instance_count=2,
                    initial_variant_weight=1.0,
                    routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                        routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                    ),
                    managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                        status="ENABLED", min_instance_count=2, max_instance_count=8,
                    ),
                ),
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="Challenger",
                    model_name="PLACEHOLDER_CHALLENGER",
                    instance_type="ml.m5.2xlarge",
                    initial_instance_count=1,
                    initial_variant_weight=0.0,
                ),
            ],
        )

        endpoint = sagemaker.CfnEndpoint(
            self, "InferenceEndpoint",
            endpoint_name=f"{{project_name}}-inference-{stage_name}",
            endpoint_config_name=endpoint_config.endpoint_config_name,
        )

        # Auto-scaling on Champion variant (prod only)
        if stage_name == "prod":
            scalable = autoscaling.ScalableTarget(
                self, "EndpointScalingTarget",
                service_namespace=autoscaling.ServiceNamespace.SAGEMAKER,
                resource_id=f"endpoint/{endpoint.endpoint_name}/variant/Champion",
                scalable_dimension="sagemaker:variant:DesiredInstanceCount",
                min_capacity=2, max_capacity=20,
            )
            scalable.scale_on_metric(
                "InvocationsPerInstance",
                metric=cw.Metric(
                    namespace="AWS/SageMaker",
                    metric_name="InvocationsPerInstance",
                    dimensions_map={"EndpointName": endpoint.endpoint_name, "VariantName": "Champion"},
                    period=Duration.minutes(1), statistic="Average",
                ),
                scaling_steps=[
                    autoscaling.ScalingInterval(change=-1, lower=0,  upper=30),
                    autoscaling.ScalingInterval(change= 1, lower=30, upper=70),
                    autoscaling.ScalingInterval(change= 2, lower=70, upper=None),
                ],
                cooldown=Duration.minutes(3),
                adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            )

        # Alarms FIRST — the deployer's AutoRollback references them by name.
        error_alarm = cw.Alarm(self, "EndpointErrorAlarm",
            alarm_name=f"{{project_name}}-endpoint-errors-{stage_name}",
            metric=cw.Metric(
                namespace="AWS/SageMaker", metric_name="ModelError",
                dimensions_map={"EndpointName": endpoint.endpoint_name},
                period=Duration.minutes(5), statistic="Sum",
            ),
            threshold=10, evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(alert_topic)],
        )

        # Model Deployer Lambda
        log_group = logs.LogGroup(self, "DeployerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-model-deployer-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        deployer = _lambda.Function(self, "ModelDeployer",
            function_name=f"{{project_name}}-model-deployer-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "model_deployer")),
            timeout=Duration.minutes(5),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            log_group=log_group,
            environment={
                "ENDPOINT_NAME":         endpoint.endpoint_name,
                "ENDPOINT_CONFIG_NAME":  endpoint_config.endpoint_config_name,
                "SAGEMAKER_ROLE_ARN":    sagemaker_role_arn,
                "STAGE":                 stage_name,
                "ENDPOINT_ERROR_ALARM":  error_alarm.alarm_name,
            },
        )
        deployer.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "sagemaker:CreateModel", "sagemaker:UpdateEndpoint",
                "sagemaker:DescribeEndpoint", "sagemaker:DescribeModelPackage",
                "sagemaker:CreateEndpointConfig",
            ],
            resources=[
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{{project_name}}*",
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint-config/{{project_name}}*",
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:model/{{project_name}}*",
                f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:model-package/{{project_name}}*",
            ],
        ))
        deployer.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[sagemaker_role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))
        iam.PermissionsBoundary.of(deployer.role).apply(permission_boundary)

        # Publish for OrchestrationStack's CfnRule target
        ssm.StringParameter(self, "ModelDeployerFnName",
            parameter_name=f"/{{project_name}}/ml/model_deployer_fn_name",
            string_value=deployer.function_name,
        )
        cdk.CfnOutput(self, "InferenceEndpointName", value=endpoint.endpoint_name)
```

### 4.3 Micro-stack gotchas

- **Alarm name reference in deployer env** — the deployer reads `ENDPOINT_ERROR_ALARM` and passes it straight to `UpdateEndpoint(..., AutoRollbackConfiguration.Alarms=[{AlarmName: ...}])`. If the alarm lives in another stack, keep the name deterministic and hardcoded on both sides (not a construct ref).
- **`iam:PassRole` Condition** — `iam:PassedToService: sagemaker.amazonaws.com` keeps the deployer from passing the role to unrelated services.
- **`resource=['arn:…:endpoint/{project_name}*']`** — wildcard inside a scoped prefix. If stack names conflict, tighten further with `-{stage_name}` suffix.
- **Deployer Lambda in the serving stack** is intentional — it needs the endpoint's full ARN at synth time. Moving it to `OrchestrationStack` would require cross-stack ARN lookup, which works but adds SSM indirection.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack |
| Switch from real-time → serverless | Only change `CfnEndpointConfig` (serverless vs production variants); deployer Lambda stays the same |
| Add shadow variant for risk-free testing | Add a third variant with `shadow_production_variants=` (SageMaker SDK calls this `ShadowProductionVariant`) |
| Large-payload inference | Switch to Async Inference; requires SNS notification topic + S3 input/output config |
| Rollback on latency (not only errors) | Add a latency CW alarm + include in `AutoRollbackConfiguration.Alarms` |
| Multi-model endpoint | See `MLOPS_MULTI_MODEL_ENDPOINT` — different `EndpointConfig` semantics |

---

## 6. Worked example — ServingStack synthesizes

Save as `tests/sop/test_MLOPS_SAGEMAKER_SERVING.py`. Offline.

```python
"""SOP verification — ServingStack synthesizes endpoint + deployer + alarm."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam, aws_sns as sns
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_serving_stack_prod():
    app = cdk.App()
    env = _env()

    deps  = cdk.Stack(app, "Deps", env=env)
    vpc   = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg    = ec2.SecurityGroup(deps, "Sg", vpc=vpc)
    topic = sns.Topic(deps, "Alerts")
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.serving_stack import ServingStack
    stack = ServingStack(
        app, stage_name="prod",
        vpc=vpc, lambda_sg=sg, alert_topic=topic,
        sagemaker_role_arn_ssm="/test/ml/sagemaker_role_arn",
        lake_bucket_curated_ssm="/test/lake/curated_bucket",
        lake_key_arn_ssm="/test/lake/kms_key_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::SageMaker::EndpointConfig",             1)
    t.resource_count_is("AWS::SageMaker::Endpoint",                   1)
    t.resource_count_is("AWS::ApplicationAutoScaling::ScalableTarget", 1)
    t.resource_count_is("AWS::CloudWatch::Alarm",                     1)   # error
    t.resource_count_is("AWS::Lambda::Function",                      1)   # deployer
```

---

## 7. References

- `docs/template_params.md` — `ENDPOINT_NAME_SSM`, `ENDPOINT_CONFIG_NAME_SSM`, `MODEL_DEPLOYER_FN_NAME_SSM`, `CAPTURE_SAMPLING_PERCENT`, `CHAMPION_INSTANCE_TYPE`, `CHAMPION_MIN`, `CHAMPION_MAX`
- `docs/Feature_Roadmap.md` — feature IDs `ML-10` (real-time endpoint), `ML-11` (serverless), `ML-12` (Model Monitor), `ML-13` (A/B), `ML-14` (auto-scaling), `ML-15` (deployer Lambda)
- SageMaker endpoint data capture: https://docs.aws.amazon.com/sagemaker/latest/dg/model-monitor-data-capture.html
- Blue/Green deployment for endpoints: https://docs.aws.amazon.com/sagemaker/latest/dg/deployment-guardrails-blue-green.html
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (approval event source), `MLOPS_CLARIFY_EXPLAINABILITY` (baseline stats / bias monitor), `MLOPS_BATCH_TRANSFORM` (offline scoring), `MLOPS_MULTI_MODEL_ENDPOINT` (multi-model pattern), `LAYER_OBSERVABILITY` (alarm plumbing), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — ServingStack resolves SageMaker role ARN, curated bucket name, KMS key ARN via SSM; `kms_key_id=` uses string ARN (5th non-negotiable); deployer grants identity-side with scoped `sagemaker:*` and `iam:PassRole` Condition. Extracted deployer handler from inline code to `lambda/model_deployer`. Added Swap matrix (§5), Worked example (§6), Gotchas on shadow vs weight=0, alarm cross-stack names. |
| 1.0 | 2026-03-05 | Initial — serving modes, real-time multi-variant endpoint, auto-scaling, Model Monitor, deployer Lambda inline, error + latency alarms. |
