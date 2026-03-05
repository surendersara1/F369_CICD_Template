# PARTIAL: Observability Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_observability()` method body.

---

## CDK Code Block — Observability Layer

```python
def _create_observability(self, stage_name: str) -> None:
    """
    Layer 6: Observability Infrastructure

    Components:
      A) CloudWatch Log Groups (per Lambda + ECS)
      B) CloudWatch Alarms (Lambda errors, RDS CPU, SQS DLQ)
      C) CloudWatch Dashboard (unified view)
      D) X-Ray Tracing (end-to-end distributed tracing)
      E) SNS Alert Topic (alarm notifications to email/Slack)
    """

    # =========================================================================
    # A) SNS ALERT TOPIC
    # =========================================================================
    self.alert_topic = sns.Topic(
        self, "AlertTopic",
        topic_name=f"{{project_name}}-alerts-{stage_name}",
        display_name=f"{{project_name}} ({stage_name}) — Infrastructure Alerts",
        master_key=self.kms_key,
    )

    # Email subscriptions (replace with actual alert emails from SOW/Architecture Map)
    ALERT_EMAILS = [
        "devops@example.com",
        "platform-team@example.com",
    ]
    for email in ALERT_EMAILS:
        self.alert_topic.add_subscription(
            sns.subscriptions.EmailSubscription(email)
        )

    # =========================================================================
    # B) CLOUDWATCH ALARMS
    # =========================================================================

    alarm_defaults = dict(
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        evaluation_periods=2,
        datapoints_to_alarm=2,
    )

    # --- Lambda Error Rate Alarm ---
    # [Claude: generate one alarm per detected Lambda microservice]
    for service_name, lambda_fn in self.lambda_functions.items():

        # Error count alarm
        error_alarm = cw.Alarm(
            self, f"{service_name}ErrorAlarm",
            alarm_name=f"{{project_name}}-{service_name}-errors-{stage_name}",
            alarm_description=f"Lambda {service_name} error rate is too high",
            metric=lambda_fn.metric_errors(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=5 if stage_name == "prod" else 10,
            **alarm_defaults,
        )
        error_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))
        error_alarm.add_ok_action(cw_actions.SnsAction(self.alert_topic))

        # Duration alarm (approaching timeout)
        duration_alarm = cw.Alarm(
            self, f"{service_name}DurationAlarm",
            alarm_name=f"{{project_name}}-{service_name}-duration-{stage_name}",
            alarm_description=f"Lambda {service_name} approaching timeout",
            metric=lambda_fn.metric_duration(
                period=Duration.minutes(5),
                statistic="p99",
            ),
            # Alert if p99 duration > 80% of configured timeout
            threshold=lambda_fn.timeout.to_milliseconds() * 0.8,
            **alarm_defaults,
        )
        duration_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        # Throttle alarm
        throttle_alarm = cw.Alarm(
            self, f"{service_name}ThrottleAlarm",
            alarm_name=f"{{project_name}}-{service_name}-throttles-{stage_name}",
            alarm_description=f"Lambda {service_name} is being throttled",
            metric=lambda_fn.metric_throttles(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=1,
            **alarm_defaults,
        )
        throttle_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # --- Aurora CPU Alarm ---
    aurora_cpu_alarm = cw.Alarm(
        self, "AuroraCPUAlarm",
        alarm_name=f"{{project_name}}-aurora-cpu-{stage_name}",
        alarm_description="Aurora Serverless CPU utilization high",
        metric=cw.Metric(
            namespace="AWS/RDS",
            metric_name="CPUUtilization",
            dimensions_map={
                "DBClusterIdentifier": self.aurora_cluster.cluster_identifier,
            },
            period=Duration.minutes(5),
            statistic="Average",
        ),
        threshold=80,
        **alarm_defaults,
    )
    aurora_cpu_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # --- DLQ Depth Alarm (messages in dead letter queue = processing failures) ---
    dlq_alarm = cw.Alarm(
        self, "DLQDepthAlarm",
        alarm_name=f"{{project_name}}-dlq-messages-{stage_name}",
        alarm_description="Messages in DLQ — investigate Lambda/ECS failures",
        metric=self.dlq.metric_approximate_number_of_messages_visible(
            period=Duration.minutes(1),
            statistic="Maximum",
        ),
        threshold=1,
        evaluation_periods=1,
        datapoints_to_alarm=1,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # --- API Gateway 5xx Error Rate ---
    api_5xx_alarm = cw.Alarm(
        self, "Api5xxAlarm",
        alarm_name=f"{{project_name}}-api-5xx-{stage_name}",
        alarm_description="API Gateway 5xx error rate elevated",
        metric=self.rest_api.metric_server_error(
            period=Duration.minutes(5),
            statistic="Sum",
        ),
        threshold=10 if stage_name == "prod" else 25,
        **alarm_defaults,
    )
    api_5xx_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # --- API Gateway Latency P99 ---
    api_latency_alarm = cw.Alarm(
        self, "ApiLatencyAlarm",
        alarm_name=f"{{project_name}}-api-latency-{stage_name}",
        alarm_description="API Gateway P99 latency exceeded SLA",
        metric=self.rest_api.metric_latency(
            period=Duration.minutes(5),
            statistic="p99",
        ),
        threshold=3000,  # 3 seconds
        **alarm_defaults,
    )
    api_latency_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # =========================================================================
    # C) CLOUDWATCH DASHBOARD
    # =========================================================================

    dashboard = cw.Dashboard(
        self, "MainDashboard",
        dashboard_name=f"{{project_name}}-{stage_name}",
        period_override=cw.PeriodOverride.AUTO,
    )

    # Row 1: API Gateway metrics
    dashboard.add_widgets(
        cw.Row(
            cw.GraphWidget(
                title="API Gateway — Requests",
                left=[self.rest_api.metric_count(period=Duration.minutes(1))],
                right=[self.rest_api.metric_server_error(period=Duration.minutes(1))],
                width=12,
            ),
            cw.GraphWidget(
                title="API Gateway — Latency",
                left=[
                    self.rest_api.metric_latency(period=Duration.minutes(1), statistic="p50"),
                    self.rest_api.metric_latency(period=Duration.minutes(1), statistic="p99"),
                ],
                width=12,
            ),
        )
    )

    # Row 2: Lambda metrics per microservice
    lambda_widgets = []
    for service_name, lambda_fn in self.lambda_functions.items():
        lambda_widgets.append(
            cw.GraphWidget(
                title=f"Lambda: {service_name}",
                left=[
                    lambda_fn.metric_invocations(period=Duration.minutes(1)),
                    lambda_fn.metric_errors(period=Duration.minutes(1)),
                ],
                right=[
                    lambda_fn.metric_duration(period=Duration.minutes(1), statistic="p99"),
                ],
                width=8,
            )
        )
    dashboard.add_widgets(cw.Row(*lambda_widgets))

    # Row 3: Data layer metrics
    dashboard.add_widgets(
        cw.Row(
            cw.GraphWidget(
                title="Aurora — CPU & Connections",
                left=[
                    cw.Metric(
                        namespace="AWS/RDS",
                        metric_name="CPUUtilization",
                        dimensions_map={"DBClusterIdentifier": self.aurora_cluster.cluster_identifier},
                        period=Duration.minutes(1),
                        statistic="Average",
                    ),
                ],
                right=[
                    cw.Metric(
                        namespace="AWS/RDS",
                        metric_name="DatabaseConnections",
                        dimensions_map={"DBClusterIdentifier": self.aurora_cluster.cluster_identifier},
                        period=Duration.minutes(1),
                        statistic="Average",
                    ),
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="SQS — Queue Depth",
                left=[
                    self.main_queue.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(1),
                        statistic="Maximum",
                    ),
                    self.dlq.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(1),
                        statistic="Maximum",
                    ),
                ],
                width=12,
            ),
        )
    )

    # Row 4: Alarm status summary
    dashboard.add_widgets(
        cw.Row(
            cw.AlarmStatusWidget(
                title="System Health",
                alarms=[
                    aurora_cpu_alarm,
                    dlq_alarm,
                    api_5xx_alarm,
                    api_latency_alarm,
                ],
                width=24,
            )
        )
    )

    # =========================================================================
    # D) LOG INSIGHTS QUERIES (saved for quick access)
    # =========================================================================

    logs.QueryDefinition(
        self, "ErrorPattern",
        query_definition_name=f"{{project_name}}/{stage_name}/ErrorPatterns",
        query_string=logs.QueryString(
            fields=["@timestamp", "@message", "@logStream"],
            filter_statements=['@message like /ERROR/ or @message like /Exception/'],
            sort="@timestamp desc",
            limit=100,
        ),
        log_groups=[
            # [Claude: Add all Lambda log groups here]
        ],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    CfnOutput(self, "DashboardURL",
        value=f"https://console.aws.amazon.com/cloudwatch/home?region={self.region}#dashboards:name={{project_name}}-{stage_name}",
        description="CloudWatch Dashboard URL",
    )

    CfnOutput(self, "AlertTopicArn",
        value=self.alert_topic.topic_arn,
        description="SNS Alert Topic ARN",
        export_name=f"{{project_name}}-alert-topic-{stage_name}",
    )
```
