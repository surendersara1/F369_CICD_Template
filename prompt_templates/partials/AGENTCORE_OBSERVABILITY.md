# PARTIAL: AgentCore Observability — Traces, Metrics, CloudWatch

**Usage:** Include when SOW mentions agent observability, OpenTelemetry, agent traces, agent metrics, CloudWatch dashboards, or agent monitoring.

---

## Observability Overview

```
Strands + AgentCore Observability:
  - OpenTelemetry traces (agent loop, model calls, tool calls)
  - Built-in metrics (token usage, latency, tool accuracy)
  - CloudWatch integration (dashboards, alarms)
  - X-Ray tracing for Lambda/ECS agents
  - AgentCore Runtime built-in observability

Trace Hierarchy:
  Agent Invocation (span)
    ├── Model Call (span) — model_id, tokens, latency
    ├── Tool Call (span) — tool_name, duration, success
    ├── Model Call (span) — with tool results
    └── Final Response (span)
```

---

## CDK Code Block — Agent Observability Infrastructure

```python
def _create_agent_observability(self, stage_name: str) -> None:
    """
    Agent observability infrastructure.

    Components:
      A) CloudWatch custom metric namespace
      B) CloudWatch dashboard for agent metrics
      C) Alarms for latency, errors, cost spikes
      D) SNS topic for alerts

    [Claude: include for any production agent deployment.]
    """

    AGENT_NAMESPACE = f"{{project_name}}/Agent"

    # =========================================================================
    # A) SNS ALERT TOPIC
    # =========================================================================

    self.alert_topic = sns.Topic(
        self, "AgentAlertTopic",
        topic_name=f"{{project_name}}-agent-alerts-{stage_name}",
        master_key=self.kms_key,
    )

    # =========================================================================
    # B) CLOUDWATCH ALARMS
    # =========================================================================

    # Agent latency alarm
    cw.Alarm(
        self, "AgentLatencyAlarm",
        alarm_name=f"{{project_name}}-agent-latency-{stage_name}",
        metric=cw.Metric(
            namespace=AGENT_NAMESPACE,
            metric_name="InvocationLatencyMs",
            dimensions_map={"Stage": stage_name},
            period=Duration.minutes(5),
            statistic="p99",
        ),
        threshold=30000,  # 30 seconds p99
        evaluation_periods=3,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    ).add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # Token cost alarm
    cw.Alarm(
        self, "AgentCostAlarm",
        alarm_name=f"{{project_name}}-agent-cost-{stage_name}",
        metric=cw.Metric(
            namespace=AGENT_NAMESPACE,
            metric_name="EstimatedCostUSD",
            dimensions_map={"Stage": stage_name},
            period=Duration.hours(1),
            statistic="Sum",
        ),
        threshold=50,  # $50/hour
        evaluation_periods=1,
    ).add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    # =========================================================================
    # C) CLOUDWATCH DASHBOARD
    # =========================================================================

    cw.Dashboard(
        self, "AgentDashboard",
        dashboard_name=f"{{project_name}}-agent-{stage_name}",
    ).add_widgets(
        cw.Row(
            cw.GraphWidget(title="Invocation Latency (ms)", width=12,
                left=[cw.Metric(namespace=AGENT_NAMESPACE, metric_name="InvocationLatencyMs",
                    dimensions_map={"Stage": stage_name}, statistic="Average", period=Duration.minutes(5))]),
            cw.GraphWidget(title="Token Usage", width=12,
                left=[
                    cw.Metric(namespace=AGENT_NAMESPACE, metric_name="InputTokens",
                        dimensions_map={"Stage": stage_name}, statistic="Sum", period=Duration.hours(1)),
                    cw.Metric(namespace=AGENT_NAMESPACE, metric_name="OutputTokens",
                        dimensions_map={"Stage": stage_name}, statistic="Sum", period=Duration.hours(1)),
                ]),
        ),
        cw.Row(
            cw.GraphWidget(title="Tool Call Count", width=12,
                left=[cw.Metric(namespace=AGENT_NAMESPACE, metric_name="ToolCallCount",
                    dimensions_map={"Stage": stage_name}, statistic="Sum", period=Duration.minutes(5))]),
            cw.GraphWidget(title="Estimated Cost (USD)", width=12,
                left=[cw.Metric(namespace=AGENT_NAMESPACE, metric_name="EstimatedCostUSD",
                    dimensions_map={"Stage": stage_name}, statistic="Sum", period=Duration.hours(1))]),
        ),
    )
```

---

## OpenTelemetry Integration — Pass 3 Reference

```python
"""Enable OpenTelemetry tracing for Strands agents."""
# Strands SDK auto-instruments with OpenTelemetry when OTEL is configured
import os
os.environ["OTEL_SERVICE_NAME"] = "{{project_name}}-agent"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:4317"

# For AWS X-Ray integration (Lambda/ECS):
# Enable tracing=_lambda.Tracing.ACTIVE in CDK Lambda definition
# X-Ray SDK auto-captures Bedrock API calls
```
