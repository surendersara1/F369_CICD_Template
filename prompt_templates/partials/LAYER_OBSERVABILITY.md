# SOP — Observability Layer (CloudWatch, X-Ray, Logs Insights)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · CloudWatch Dashboards + Alarms + Log Insights + X-Ray

---

## 1. Purpose

Baseline observability consumed by every workload:

- CloudWatch log groups with retention + KMS encryption
- Structured JSON logging via Lambda Powertools (field set: `job_id`, `correlation_id`, `user_id`, `stage`)
- X-Ray tracing on Lambda + SFN + API Gateway
- Custom metric filters from log lines
- CloudWatch dashboards (pipeline + costs + quotas)
- Alarms → SNS → PagerDuty / Slack
- Synthetic canaries (Phase 2)

See also `OPS_ADVANCED_MONITORING` for deeper ops (log archiving, anomaly detection) and `OBS_OPENTELEMETRY_GRAFANA` for third-party export.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Observability defined alongside the workloads it monitors | **§3 Monolith Variant** |
| Dedicated `ObservabilityStack` that references workloads across stacks | **§4 Micro-Stack Variant** |

No cycle risk — ObservabilityStack reads metrics/ARNs from every other stack but never mutates them.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_logs as logs,
)


def _create_observability(self, stage: str) -> None:
    self.ops_topic = sns.Topic(
        self, "OpsAlerts",
        topic_name=f"{{project_name}}-ops-{stage}",
        master_key=self.kms_key,
    )

    # -- Metric filters — turn structured log events into custom metrics -----
    for fn_id, fn in self.lambda_functions.items():
        log_group = fn.log_group
        logs.MetricFilter(
            self, f"{fn_id}ErrorFilter",
            log_group=log_group,
            metric_namespace=f"{{project_name}}/errors",
            metric_name=f"{fn_id}ErrorCount",
            filter_pattern=logs.FilterPattern.literal('{ $.level = "ERROR" }'),
            metric_value="1",
        )

    # -- Alarms --------------------------------------------------------------
    for fn_id, fn in self.lambda_functions.items():
        cw.Alarm(
            self, f"{fn_id}ErrorRateAlarm",
            alarm_name=f"{{project_name}}-{fn_id}-errors-{stage}",
            metric=fn.metric_errors(period=Duration.minutes(5)),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(self.ops_topic))

    # DLQ depth alarm (messages visible)
    cw.Alarm(
        self, "DlqDepthAlarm",
        alarm_name=f"{{project_name}}-dlq-depth-{stage}",
        metric=self.fanout_dlq.metric_approximate_number_of_messages_visible(),
        threshold=1,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    ).add_alarm_action(cw_actions.SnsAction(self.ops_topic))

    # -- Dashboard -----------------------------------------------------------
    dashboard = cw.Dashboard(
        self, "PipelineDashboard",
        dashboard_name=f"{{project_name}}-pipeline-{stage}",
    )
    dashboard.add_widgets(
        cw.GraphWidget(
            title="Lambda Invocations + Errors",
            left=[fn.metric_invocations() for fn in self.lambda_functions.values()],
            right=[fn.metric_errors()      for fn in self.lambda_functions.values()],
            width=24, height=6,
        ),
        cw.GraphWidget(
            title="SQS — Visible Messages",
            left=[self.main_queue.metric_approximate_number_of_messages_visible()],
            width=12, height=6,
        ),
        cw.SingleValueWidget(
            title="Errors — last 1h",
            metrics=[fn.metric_errors(period=Duration.hours(1)) for fn in self.lambda_functions.values()],
            width=12, height=6,
        ),
    )
```

### 3.1 Monolith gotchas

- **`metric_errors()` etc. are L2 helpers** that assume the Lambda's default CW namespace. Custom Lambda metrics emitted via Powertools live in a different namespace — reference them explicitly via `cw.Metric(namespace=..., metric_name=...)`.
- **Composite alarms** (any of N breaches → page) are built with `cw.CompositeAlarm`.
- **Log retention forever**: not possible; max 10 years. For compliance archiving, subscribe a log group to Kinesis Firehose → S3 Glacier (see `OPS_ADVANCED_MONITORING`).

---

## 4. Micro-Stack Variant

### 4.1 `ObservabilityStack` — no mutation of upstream

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_lambda as _lambda,
    aws_rds as rds,
    aws_stepfunctions as sfn,
    aws_logs as logs,
)
from constructs import Construct


class ObservabilityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        lambda_fns: dict[str, _lambda.IFunction],
        state_machine: sfn.IStateMachine,
        queues: dict[str, sqs.IQueue],
        rds_instance: rds.IDatabaseInstance,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-observability", **kwargs)

        self.ops_topic = sns.Topic(
            self, "OpsAlerts",
            topic_name="{project_name}-ops-alerts",
        )

        # Alarms — built from upstream metric ARNs (no mutation)
        for fn_id, fn in lambda_fns.items():
            cw.Alarm(
                self, f"{fn_id}ErrorsAlarm",
                alarm_name=f"{{project_name}}-{fn_id}-errors",
                metric=fn.metric_errors(period=Duration.minutes(5)),
                threshold=5, evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # SFN execution failure alarm
        cw.Alarm(
            self, "SfnFailuresAlarm",
            alarm_name="{project_name}-sfn-failures",
            metric=state_machine.metric_failed(period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        ).add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # DLQ depth alarms (one per queue whose name ends with 'dlq')
        for qname, q in queues.items():
            if "dlq" in qname.lower():
                cw.Alarm(
                    self, f"{qname}DepthAlarm",
                    alarm_name=f"{{project_name}}-{qname}-depth",
                    metric=q.metric_approximate_number_of_messages_visible(),
                    threshold=1, evaluation_periods=1,
                    comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                ).add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # RDS CPU alarm
        cw.Alarm(
            self, "RdsCpuAlarm",
            alarm_name="{project_name}-rds-cpu",
            metric=rds_instance.metric_cpu_utilization(period=Duration.minutes(5)),
            threshold=80, evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        ).add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Dashboard
        dashboard = cw.Dashboard(
            self, "PipelineDashboard",
            dashboard_name="{project_name}-pipeline",
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda Errors",
                left=[fn.metric_errors() for fn in lambda_fns.values()],
                width=24, height=6,
            ),
            cw.GraphWidget(
                title="Step Functions — Started / Succeeded / Failed",
                left=[state_machine.metric_started(), state_machine.metric_succeeded()],
                right=[state_machine.metric_failed()],
                width=24, height=6,
            ),
        )

        cdk.CfnOutput(self, "OpsTopicArn", value=self.ops_topic.topic_arn)
```

### 4.2 Micro-stack gotchas

- **`fn.metric_errors()`** produces a reference to the Lambda's CW metric via `AWS/Lambda` namespace; no mutation of the Lambda itself.
- **`log_group` on another stack's Lambda** is accessible via `fn.log_group` — reading, not writing, so no cycle.
- **Metric filters** on cross-stack log groups — create them in the OWNING stack where the log group is. Emit the *metric* into a shared namespace that ObservabilityStack alarms on. This keeps the filter co-located with the source.

---

## 5. Structured log contract (Powertools)

Every log line from every Lambda must carry:

```json
{
  "level": "INFO",
  "message": "transcription complete",
  "service": "audio-analytics",
  "job_id": "0190a1b2-...",
  "correlation_id": "0190a1b2-...",
  "user_id": "user-123",
  "stage": "transcribe",
  "timestamp": "2026-04-21T10:11:12.345Z"
}
```

This enables Logs Insights queries like:

```
fields @timestamp, job_id, stage, message
| filter correlation_id = "0190a1b2-..."
| sort @timestamp asc
```

Single-query reconstruction of any job's full lifecycle.

---

## 6. Worked example

```python
def test_observability_creates_dashboard_and_ops_topic():
    # ... instantiate ObservabilityStack with mock upstream resources ...
    t = Template.from_stack(obs)
    t.resource_count_is("AWS::CloudWatch::Dashboard", 1)
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.has_resource("AWS::CloudWatch::Alarm", Match.any_value())
```

---

## 7. References

- `docs/Feature_Roadmap.md` — OBS-01..OBS-27, TRC-01..TRC-12
- Related SOPs: `OPS_ADVANCED_MONITORING` (log archiving, anomaly detection), `OBS_OPENTELEMETRY_GRAFANA` (ADOT export), `LAYER_BACKEND_LAMBDA` (Powertools setup)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Cross-stack safety: read upstream metrics/ARNs, never mutate. Structured log contract. |
| 1.0 | 2026-03-05 | Initial. |
