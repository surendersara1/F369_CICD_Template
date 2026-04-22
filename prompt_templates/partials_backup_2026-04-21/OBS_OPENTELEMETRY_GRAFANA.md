# PARTIAL: Advanced Observability — OpenTelemetry, Managed Grafana, Prometheus, RUM

**Usage:** Include when SOW mentions observability platform, Grafana, Prometheus, OpenTelemetry, distributed tracing, real user monitoring, or modern observability stack.

---

## Observability Stack

```
Application (Lambda, ECS, EKS)
       │  instrumented with
       ▼
OpenTelemetry SDK (auto-instrumentation)
       │  sends spans/metrics/logs to
       ▼
AWS Distro for OpenTelemetry (ADOT) Collector
  ├── Traces  → AWS X-Ray (distributed tracing)
  ├── Metrics → Amazon Managed Service for Prometheus (AMP)
  └── Logs    → CloudWatch Logs (structured JSON)
                           │
                           ▼
              Amazon Managed Grafana (AMG)
              ├── Pre-built dashboards (Lambda, ECS, RDS, SQS...)
              ├── Tempo (trace visualization)
              ├── Loki (log aggregation)
              └── Alertmanager → PagerDuty / Slack / OpsGenie

CloudWatch RUM (Real User Monitoring)
  → Browser session recording, Core Web Vitals, JS errors, user journey
```

---

## CDK Code Block — Advanced Observability

```python
def _create_advanced_observability(self, stage_name: str) -> None:
    """
    Advanced Observability Stack.

    Components:
      A) Amazon Managed Service for Prometheus (AMP) — metrics storage
      B) Amazon Managed Grafana (AMG) — dashboards + alerting
      C) ADOT Collector Layer for Lambda (OpenTelemetry auto-instrumentation)
      D) CloudWatch RUM (Real User Monitoring for frontend)
      E) CloudWatch Embedded Metrics Format (EMF) helper
      F) Custom SLO/SLI dashboard + error budget tracking
    """

    import aws_cdk.aws_aps as aps  # Amazon Managed Prometheus
    import aws_cdk.aws_grafana as grafana
    import aws_cdk.aws_rum as rum

    # =========================================================================
    # A) AMAZON MANAGED PROMETHEUS (AMP)
    # Fully managed Prometheus — no servers to manage
    # =========================================================================

    amp_workspace = aps.CfnWorkspace(
        self, "AMPWorkspace",
        alias=f"{{project_name}}-{stage_name}",

        # Alert manager configuration (routes alerts to Grafana AlertManager)
        alert_manager_definition="""
alertmanager_config: |
  route:
    receiver: 'default'
    group_by: ['alertname', 'cluster', 'service']
    group_wait:      30s
    group_interval:  5m
    repeat_interval: 1h
    routes:
      - match: { severity: critical }
        receiver: 'critical'
        repeat_interval: 15m
  receivers:
    - name: 'default'
      sns_configs:
        - topic_arn: PLACEHOLDER_SNS_ARN  # [Claude: replace with alert_topic.topic_arn]
          api_url: 'https://sns.REGION.amazonaws.com/'
          sigv4:
            region: REGION
            role_arn: PLACEHOLDER_ROLE_ARN
    - name: 'critical'
      pagerduty_configs:
        - service_key: 'PLACEHOLDER_PAGERDUTY_KEY'  # [Claude: from Secrets Manager]
""",

        # Recording rules — pre-compute expensive queries for fast dashboards
        rule_groups_namespace=aps.CfnWorkspace.RuleGroupsNamespaceProperty(
            name=f"{{project_name}}-rules",
            data="""
groups:
  - name: LatencyPercentiles
    interval: 60s
    rules:
      - record: job:request_latency_seconds:p99
        expr: histogram_quantile(0.99, rate(request_duration_seconds_bucket[5m]))
      - record: job:request_latency_seconds:p95
        expr: histogram_quantile(0.95, rate(request_duration_seconds_bucket[5m]))
      - record: job:request_error_rate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])

  - name: SLOErrorBudget
    interval: 60s
    rules:
      # SLO: 99.9% availability (allows 43.8 minutes downtime/month)
      - record: slo:error_budget_remaining:ratio
        expr: 1 - (1 - 0.999) * 30 * 24 * 60 / job:request_error_rate
      - alert: ErrorBudgetBurning
        expr: slo:error_budget_remaining:ratio < 0.5
        for: 10m
        labels: { severity: critical }
        annotations:
          summary: "Error budget 50% consumed — SLO at risk"
""",
        ),

        logging_configuration=aps.CfnWorkspace.LoggingConfigurationProperty(
            log_group_arn=logs.LogGroup(
                self, "AMPLogGroup",
                log_group_name=f"/aws/prometheus/{{project_name}}-{stage_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                encryption_key=self.kms_key,
                removal_policy=RemovalPolicy.RETAIN,
            ).log_group_arn,
        ),

        tags=[{"key": "Project", "value": "{{project_name}}"}, {"key": "Stage", "value": stage_name}],
    )

    # =========================================================================
    # B) AMAZON MANAGED GRAFANA (AMG)
    # =========================================================================

    grafana_role = iam.Role(
        self, "GrafanaRole",
        assumed_by=iam.ServicePrincipal("grafana.amazonaws.com"),
        role_name=f"{{project_name}}-grafana-{stage_name}",
    )
    grafana_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata",
            "cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms",
            "xray:GetTraceSummaries", "xray:GetTrace", "xray:GetGroups",
            "logs:StartQuery", "logs:GetQueryResults", "logs:DescribeLogGroups",
            "ec2:DescribeRegions", "tag:GetResources",
        ],
        resources=["*"],
    ))

    grafana_workspace = grafana.CfnWorkspace(
        self, "GrafanaWorkspace",
        name=f"{{project_name}}-{stage_name}",
        description=f"{{project_name}} Observability Dashboard ({stage_name})",
        account_access_type="CURRENT_ACCOUNT",
        authentication_providers=["AWS_SSO"],  # SSO via IAM Identity Center
        permission_type="SERVICE_MANAGED",
        role_arn=grafana_role.role_arn,
        grafana_version="10.4",

        data_sources=[
            "CLOUDWATCH",           # CloudWatch Metrics + Logs
            "PROMETHEUS",           # Amazon Managed Prometheus
            "XRAY",                 # AWS X-Ray traces
            "ATHENA",               # Athena for log analytics
        ],

        notification_destinations=["SNS"],
        vpc_configuration=grafana.CfnWorkspace.VpcConfigurationProperty(
            security_group_ids=[self.lambda_sg.security_group_id],
            subnet_ids=[s.subnet_id for s in self.vpc.private_subnets[:2]],
        ) if stage_name == "prod" else None,

        organization_role_name="ADMIN",
        organizational_units=[],

        tags=[{"key": "Project", "value": "{{project_name}}"}],
    )

    # =========================================================================
    # C) ADOT LAMBDA LAYER (OpenTelemetry auto-instrumentation for Lambda)
    # Zero-code changes to add OpenTelemetry to Lambda functions
    # =========================================================================

    # ADOT Lambda Layer ARN — language-specific
    ADOT_LAYER_ARNS = {
        "python": f"arn:aws:lambda:{self.region}:901920570463:layer:aws-otel-python-amd64-ver-1-21-0:1",
        "nodejs": f"arn:aws:lambda:{self.region}:901920570463:layer:aws-otel-nodejs-amd64-ver-1-18-1:1",
        "java":   f"arn:aws:lambda:{self.region}:901920570463:layer:aws-otel-java-wrapper-amd64-ver-1-32-0:1",
    }

    # [Claude: add ADOT layer to all Lambda functions in the project]
    # Example: Add to any Lambda function in your stack:
    # fn.add_layers(_lambda.LayerVersion.from_layer_version_arn(self, "ADOT", ADOT_LAYER_ARNS["python"]))
    # fn.add_environment("AWS_LAMBDA_EXEC_WRAPPER", "/opt/otel-instrument")
    # fn.add_environment("OPENTELEMETRY_COLLECTOR_CONFIG_FILE", "/var/task/otel-config.yaml")
    # fn.add_environment("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4317")

    # SSM parameter to store ADOT config (shared across all Lambdas)
    ssm.StringParameter(
        self, "ADOTConfigParam",
        parameter_name=f"/{{project_name}}/{stage_name}/adot-collector-config",
        string_value=json.dumps({
            "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}}}},
            "processors": {
                "batch": {"timeout": "1s", "send_batch_size": 50},
                "resource": {"attributes": [
                    {"key": "service.name", "value": "{{project_name}}", "action": "insert"},
                    {"key": "deployment.environment", "value": stage_name, "action": "insert"},
                ]},
            },
            "exporters": {
                "awsxray": {},
                "awsprometheusremotewrite": {"endpoint": f"https://aps-workspaces.{self.region}.amazonaws.com/workspaces/{amp_workspace.attr_arn}/api/v1/remote_write"},
                "awscloudwatchlogs": {"log_group_name": f"/aws/otel/{{project_name}}-{stage_name}"},
            },
            "service": {
                "pipelines": {
                    "traces": {"receivers": ["otlp"], "processors": ["batch", "resource"], "exporters": ["awsxray"]},
                    "metrics": {"receivers": ["otlp"], "processors": ["batch", "resource"], "exporters": ["awsprometheusremotewrite"]},
                    "logs": {"receivers": ["otlp"], "processors": ["batch"], "exporters": ["awscloudwatchlogs"]},
                }
            },
        }),
        description="ADOT Collector configuration for all Lambda functions",
    )

    # =========================================================================
    # D) CLOUDWATCH RUM (Real User Monitoring)
    # Track frontend performance: Core Web Vitals, JS errors, user sessions
    # =========================================================================

    rum_monitor = rum.CfnAppMonitor(
        self, "RUMAppMonitor",
        name=f"{{project_name}}-{stage_name}",
        domain=f"{{project_name}}.com",  # [Claude: replace with actual domain]
        app_monitor_configuration=rum.CfnAppMonitor.AppMonitorConfigurationProperty(
            allow_cookies=True,
            enable_x_ray=True,       # Connect RUM sessions to X-Ray traces
            session_sample_rate=stage_name != "prod" and 1.0 or 0.1,  # Sample 10% in prod
            telemetries=["performance", "errors", "http"],
            included_pages=[".*"],   # Monitor all pages
            excluded_pages=["/healthcheck", "/internal/.*"],
        ),
        cw_log_enabled=True,         # Send RUM data to CloudWatch Logs
        custom_events=rum.CfnAppMonitor.CustomEventsProperty(status="ENABLED"),
    )

    # =========================================================================
    # E) SLO DASHBOARD — Error Budget Tracking
    # =========================================================================

    cw.Dashboard(
        self, "SLODashboard",
        dashboard_name=f"{{project_name}}-slo-{stage_name}",
        widgets=[
            [
                cw.GraphWidget(
                    title="Availability SLO (Target: 99.9%)",
                    left=[
                        cw.MathExpression(
                            expression="(1 - errors/requests) * 100",
                            using_metrics={
                                "errors":   cw.Metric(namespace="{{project_name}}", metric_name="5xxErrors",
                                                      dimensions_map={"Stage": stage_name}, statistic="Sum"),
                                "requests": cw.Metric(namespace="{{project_name}}", metric_name="TotalRequests",
                                                      dimensions_map={"Stage": stage_name}, statistic="Sum"),
                            },
                        )
                    ],
                    right=[
                        cw.Metric(namespace="{{project_name}}", metric_name="ErrorBudgetRemaining",
                                  dimensions_map={"Stage": stage_name}, statistic="Average"),
                    ],
                    period=Duration.hours(1),
                    width=12,
                ),
                cw.GraphWidget(
                    title="Latency p95/p99 vs SLO Threshold",
                    left=[
                        cw.Metric(namespace="AWS/ApiGateway", metric_name="Latency",
                                  dimensions_map={"ApiName": f"{{project_name}}-api-{stage_name}"},
                                  statistic="p95"),
                        cw.Metric(namespace="AWS/ApiGateway", metric_name="Latency",
                                  dimensions_map={"ApiName": f"{{project_name}}-api-{stage_name}"},
                                  statistic="p99"),
                    ],
                    width=12,
                ),
            ],
        ],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "AMPWorkspaceArn",
        value=amp_workspace.attr_arn,
        description="Amazon Managed Prometheus workspace ARN",
        export_name=f"{{project_name}}-amp-{stage_name}",
    )
    CfnOutput(self, "GrafanaWorkspaceUrl",
        value=grafana_workspace.attr_endpoint,
        description="Grafana dashboard URL",
        export_name=f"{{project_name}}-grafana-{stage_name}",
    )
    CfnOutput(self, "RUMAppMonitorId",
        value=rum_monitor.attr_id,
        description="CloudWatch RUM App Monitor ID — embed snippet in frontend",
        export_name=f"{{project_name}}-rum-{stage_name}",
    )
```
