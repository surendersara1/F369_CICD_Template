# PARTIAL: MLOps — SageMaker Model Serving, Monitoring & Drift Detection

**Usage:** Include when SOW mentions model deployment, inference endpoints, model monitoring, A/B testing, champion/challenger, or production ML serving.

---

## Serving Modes — Which to Choose

| Mode                     | Latency         | Cost                        | Use Case                                          |
| ------------------------ | --------------- | --------------------------- | ------------------------------------------------- |
| **Real-time Endpoint**   | < 100ms         | Pay per instance-hour       | Low-latency API inference (fraud, recommendation) |
| **Serverless Inference** | 100ms-2s        | Pay per invocation          | Spiky/infrequent traffic, dev/staging             |
| **Async Inference**      | Seconds-minutes | Queue-based, large payloads | NLP on long documents, large model predictions    |
| **Batch Transform**      | Minutes-hours   | Cheapest                    | Offline scoring millions of records at once       |

---

## CDK Code Block — SageMaker Serving + Model Monitor

```python
def _create_sagemaker_serving(self, stage_name: str) -> None:
    """
    SageMaker Model Serving Infrastructure.

    Components:
      A) SageMaker Real-time Endpoint (low-latency inference)
      B) Serverless Inference Endpoint (cost-effective for dev/staging)
      C) Async Inference Endpoint (large payload / long-running inference)
      D) SageMaker Model Monitor (data drift + model quality)
      E) SageMaker Clarify Monitor (bias + feature attribution drift)
      F) A/B Testing / Blue-Green Endpoint Deployment
      G) Lambda: Model Deployer (triggered by Model Registry approval)

    [Claude: include A or B based on latency requirements in Architecture Map.
     Always include D (Model Monitor) for production endpoints.
     Include F if SOW mentions A/B testing, champion/challenger, shadow mode.]
    """

    import aws_cdk.aws_sagemaker as sagemaker
    import aws_cdk.aws_applicationautoscaling as autoscaling

    # =========================================================================
    # A) REAL-TIME ENDPOINT (multi-variant for A/B testing)
    # =========================================================================
    # Note: The actual model artifact comes from the Model Registry.
    # The CDK creates the endpoint infrastructure; the model Lambda deploys to it.

    # Model configuration per environment
    endpoint_config = {
        "ds": {
            "instance_type": "ml.t3.medium",
            "instance_count": 1,
            "serverless": True,  # Use serverless in DS domain (unpredictable traffic)
        },
        "staging": {
            "instance_type": "ml.m5.large",
            "instance_count": 1,
            "serverless": False,
        },
        "prod": {
            "instance_type": "ml.m5.2xlarge",  # [Claude: use GPU if SOW mentions deep learning]
            "instance_count": 2,               # Min 2 for HA
            "serverless": False,
        },
    }

    endpoint_cfg = endpoint_config.get(stage_name, endpoint_config["ds"])

    # =========================================================================
    # B) SERVERLESS INFERENCE ENDPOINT (for dev/DS domain)
    # =========================================================================

    if endpoint_cfg["serverless"]:
        serverless_endpoint_config = sagemaker.CfnEndpointConfig(
            self, "ServerlessEndpointConfig",
            endpoint_config_name=f"{{project_name}}-serverless-{stage_name}",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=f"{{project_name}}-model-{stage_name}",  # Filled by Model Deployer Lambda
                    serverless_config=sagemaker.CfnEndpointConfig.ServerlessConfigProperty(
                        memory_size_in_mb=2048,
                        max_concurrency=10,          # Max parallel requests
                        provisioned_concurrency=2,   # Pre-warmed instances (reduce cold start)
                    ),
                )
            ],
        )

    # =========================================================================
    # A) REAL-TIME ENDPOINT (for staging + prod)
    # =========================================================================

    else:
        # Multi-variant endpoint config (enables A/B testing + shadow mode)
        # Start with single variant, Lambda adds more on approval
        self.endpoint_config = sagemaker.CfnEndpointConfig(
            self, "RealTimeEndpointConfig",
            endpoint_config_name=f"{{project_name}}-endpoint-config-{stage_name}",

            production_variants=[
                # Champion (current prod model — 100% traffic initially)
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="Champion",
                    model_name="PLACEHOLDER_TO_BE_SET_BY_DEPLOYER",
                    instance_type=endpoint_cfg["instance_type"],
                    initial_instance_count=endpoint_cfg["instance_count"],
                    initial_variant_weight=1.0,        # 100% of traffic

                    # Routing config: least outstanding requests (better than round-robin)
                    routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                        routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                    ),

                    # Managed Instance Scaling
                    managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                        status="ENABLED",
                        min_instance_count=endpoint_cfg["instance_count"],
                        max_instance_count=endpoint_cfg["instance_count"] * 4,
                    ),
                ),
                # Challenger (new model in shadow/A-B mode — 0% traffic until activated)
                # Traffic weight set to 0 by default; Lambda updates to split traffic for A/B test
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="Challenger",
                    model_name="PLACEHOLDER_CHALLENGER",
                    instance_type=endpoint_cfg["instance_type"],
                    initial_instance_count=1,          # Start minimal
                    initial_variant_weight=0.0,        # Shadow mode: receives copies, not live traffic
                ),
            ],

            kms_key_id=self.kms_key.key_arn,
        )

        # Endpoint (the actual serving URL)
        self.inference_endpoint = sagemaker.CfnEndpoint(
            self, "InferenceEndpoint",
            endpoint_name=f"{{project_name}}-inference-{stage_name}",
            endpoint_config_name=self.endpoint_config.endpoint_config_name,

            tags=[
                {"key": "Project", "value": "{{project_name}}"},
                {"key": "Environment", "value": stage_name},
                {"key": "ManagedBy", "value": "CDK"},
            ],
        )

        # =====================================================================
        # AUTO-SCALING on Endpoint (prod only)
        # =====================================================================

        if stage_name == "prod":
            endpoint_scalable_target = autoscaling.ScalableTarget(
                self, "EndpointScalingTarget",
                service_namespace=autoscaling.ServiceNamespace.SAGEMAKER,
                resource_id=f"endpoint/{self.inference_endpoint.endpoint_name}/variant/Champion",
                scalable_dimension="sagemaker:variant:DesiredInstanceCount",
                min_capacity=2,
                max_capacity=20,
            )

            # Scale out aggressively, scale in conservatively
            endpoint_scalable_target.scale_on_metric(
                "InvocationsPerInstance",
                metric=cw.Metric(
                    namespace="AWS/SageMaker",
                    metric_name="InvocationsPerInstance",
                    dimensions_map={
                        "EndpointName": self.inference_endpoint.endpoint_name,
                        "VariantName": "Champion",
                    },
                    period=Duration.minutes(1),
                    statistic="Average",
                ),
                scaling_steps=[
                    autoscaling.ScalingInterval(change=-1, lower=0, upper=30),    # Scale in if < 30/instance
                    autoscaling.ScalingInterval(change=1, lower=30, upper=70),    # Scale out if 30-70/instance
                    autoscaling.ScalingInterval(change=2, lower=70, upper=None),  # Scale out 2x if > 70
                ],
                cooldown=Duration.minutes(3),
                adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            )

    # =========================================================================
    # D) SAGEMAKER MODEL MONITOR — Data quality + model quality drift
    # =========================================================================

    # Capture config: log sample of requests + responses to S3 for monitoring
    capture_config = sagemaker.CfnEndpointConfig.DataCaptureConfigProperty(
        enable_capture=True,
        initial_sampling_percentage=20 if stage_name == "prod" else 100,  # Sample 20% in prod
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

    # Monitor schedule: run daily to check for drift
    # [Note: Baseline statistics computed separately during model registration]

    monitoring_schedule = sagemaker.CfnMonitoringSchedule(
        self, "DataQualityMonitor",
        monitoring_schedule_name=f"{{project_name}}-data-quality-{stage_name}",
        monitoring_schedule_config=sagemaker.CfnMonitoringSchedule.MonitoringScheduleConfigProperty(
            schedule_config=sagemaker.CfnMonitoringSchedule.ScheduleConfigProperty(
                schedule_expression="cron(0 8 * * ? *)",  # Daily at 8am UTC
            ),
            monitoring_job_definition=sagemaker.CfnMonitoringSchedule.MonitoringJobDefinitionProperty(
                monitoring_type="DataQuality",
                monitoring_inputs=[
                    sagemaker.CfnMonitoringSchedule.MonitoringInputProperty(
                        endpoint_input=sagemaker.CfnMonitoringSchedule.EndpointInputProperty(
                            endpoint_name=f"{{project_name}}-inference-{stage_name}",
                            local_path="/opt/ml/processing/input/endpoint",
                        )
                    )
                ],
                monitoring_output_config=sagemaker.CfnMonitoringSchedule.MonitoringOutputConfigProperty(
                    monitoring_outputs=[
                        sagemaker.CfnMonitoringSchedule.MonitoringOutputProperty(
                            s3_output=sagemaker.CfnMonitoringSchedule.S3OutputProperty(
                                local_path="/opt/ml/processing/output",
                                s3_uri=f"s3://{self.lake_buckets['curated'].bucket_name}/model-monitor/reports/",
                                s3_upload_mode="EndOfJob",
                            )
                        )
                    ],
                    kms_key_id=self.kms_key.key_arn,
                ),
                monitoring_resources=sagemaker.CfnMonitoringSchedule.MonitoringResourcesProperty(
                    cluster_config=sagemaker.CfnMonitoringSchedule.ClusterConfigProperty(
                        instance_count=1,
                        instance_type="ml.m5.xlarge",
                        volume_size_in_gb=20,
                    )
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

    # CloudWatch alarm: Alert when data drift violations detected
    cw.Alarm(
        self, "DataDriftAlarm",
        alarm_name=f"{{project_name}}-data-drift-{stage_name}",
        alarm_description="SageMaker Model Monitor detected data quality violations — potential data drift",
        metric=cw.Metric(
            namespace="aws/sagemaker/Endpoints/data-metrics",
            metric_name="feature_baseline_drift_distance",
            dimensions_map={"MonitoringSchedule": f"{{project_name}}-data-quality-{stage_name}"},
            period=Duration.hours(24),
            statistic="Maximum",
        ),
        threshold=0.5,   # Drift distance > 0.5 = significant drift
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    )

    # =========================================================================
    # G) MODEL DEPLOYER LAMBDA
    # Triggered by EventBridge when a model is approved in Model Registry
    # Deploys the approved model to the SageMaker endpoint
    # =========================================================================

    model_deployer_fn = _lambda.Function(
        self, "ModelDeployer",
        function_name=f"{{project_name}}-model-deployer-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

sm = boto3.client('sagemaker')

def handler(event, context):
    model_package_arn = event['detail']['ModelPackageArn']
    endpoint_name = os.environ['ENDPOINT_NAME']
    stage = os.environ['STAGE']

    logger.info(f"Deploying approved model: {model_package_arn} to {endpoint_name}")

    # Create model from approved package
    model_name = f"{endpoint_name}-{context.aws_request_id[:8]}"
    sm.create_model(
        ModelName=model_name,
        PrimaryContainer={"ModelPackageName": model_package_arn},
        ExecutionRoleArn=os.environ['SAGEMAKER_ROLE_ARN'],
    )

    # Update endpoint with new model (blue/green deployment)
    sm.update_endpoint(
        EndpointName=endpoint_name,
        EndpointConfigName=os.environ['ENDPOINT_CONFIG_NAME'],
        DeploymentConfig={
            "BlueGreenUpdatePolicy": {
                "TrafficRoutingConfiguration": {
                    "Type": "ALL_AT_ONCE" if stage != "prod" else "LINEAR",
                    # Linear traffic shift in prod: 10% → 50% → 100% over 10 min
                    "LinearStepSize": {"Type": "CAPACITY_PERCENT", "Value": 10} if stage == "prod" else None,
                },
                "WaitIntervalInSeconds": 300,  # Wait 5 min between traffic shifts
                "MaximumExecutionTimeoutInSeconds": 1800,  # 30 min max rollout
                "TerminationWaitInSeconds": 300,  # Keep old instances 5 min for rollback
            },
            "AutoRollbackConfiguration": {
                "Alarms": [
                    {"AlarmName": os.environ['ENDPOINT_ERROR_ALARM']},  # Rollback if errors spike
                ]
            },
        },
    )

    logger.info(f"Endpoint update initiated: {endpoint_name}")
    return {"statusCode": 200, "model_deployed": model_name, "endpoint": endpoint_name}
"""),
        environment={
            "ENDPOINT_NAME": f"{{project_name}}-inference-{stage_name}",
            "ENDPOINT_CONFIG_NAME": f"{{project_name}}-endpoint-config-{stage_name}",
            "SAGEMAKER_ROLE_ARN": self.sagemaker_role.role_arn,
            "STAGE": stage_name,
            "ENDPOINT_ERROR_ALARM": f"{{project_name}}-endpoint-errors-{stage_name}",
        },
        timeout=Duration.minutes(5),
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
    )

    # Grant deployer Lambda permission to manage SageMaker endpoints + models
    model_deployer_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "sagemaker:CreateModel",
                "sagemaker:UpdateEndpoint",
                "sagemaker:DescribeEndpoint",
                "sagemaker:DescribeModelPackage",
                "sagemaker:CreateEndpointConfig",
                "iam:PassRole",
            ],
            resources=["*"],  # [Claude: narrow to specific resources if account has strict SCPs]
        )
    )

    # =========================================================================
    # ENDPOINT ERROR ALARM (used for auto-rollback in deployer Lambda)
    # =========================================================================
    cw.Alarm(
        self, "EndpointErrorAlarm",
        alarm_name=f"{{project_name}}-endpoint-errors-{stage_name}",
        alarm_description="SageMaker endpoint error rate high — trigger deployment rollback",
        metric=cw.Metric(
            namespace="AWS/SageMaker",
            metric_name="ModelError",
            dimensions_map={"EndpointName": f"{{project_name}}-inference-{stage_name}"},
            period=Duration.minutes(5),
            statistic="Sum",
        ),
        threshold=10,  # More than 10 model errors in 5 min → rollback
        evaluation_periods=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    )

    cw.Alarm(
        self, "EndpointLatencyAlarm",
        alarm_name=f"{{project_name}}-endpoint-latency-{stage_name}",
        alarm_description="SageMaker endpoint p99 latency too high",
        metric=cw.Metric(
            namespace="AWS/SageMaker",
            metric_name="ModelLatency",
            dimensions_map={"EndpointName": f"{{project_name}}-inference-{stage_name}", "VariantName": "Champion"},
            period=Duration.minutes(5),
            statistic="p99",
        ),
        threshold=2000,  # 2 seconds p99 — [Claude: adjust from SOW latency requirements]
        evaluation_periods=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "InferenceEndpointName",
        value=f"{{project_name}}-inference-{stage_name}",
        description="SageMaker inference endpoint name",
        export_name=f"{{project_name}}-endpoint-name-{stage_name}",
    )
    CfnOutput(self, "ModelDeployerArn",
        value=model_deployer_fn.function_arn,
        description="Lambda ARN that deploys approved models to the endpoint",
        export_name=f"{{project_name}}-model-deployer-{stage_name}",
    )
```
