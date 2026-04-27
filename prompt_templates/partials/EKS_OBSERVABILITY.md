# SOP — EKS Observability (Container Insights · ADOT · Fluent Bit · Amazon Managed Prometheus · Managed Grafana)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · CloudWatch Container Insights with enhanced observability (2024) · AWS Distro for OpenTelemetry (ADOT) v0.96+ · Fluent Bit v3.1+ · Amazon Managed Prometheus (AMP) · Amazon Managed Grafana (AMG) · X-Ray distributed tracing

---

## 1. Purpose

- Codify the **three-pillar observability** (metrics + logs + traces) for EKS workloads using AWS-managed services + OSS-compatible APIs.
- Codify **CloudWatch Container Insights with enhanced observability** (2024) — pod/container metrics + control plane metrics + EKS performance logs out-of-the-box, no agent config needed.
- Codify **ADOT collector** for OTLP metrics → AMP, OTLP traces → X-Ray, OTLP logs → CloudWatch.
- Codify **Fluent Bit** as the log forwarder (CloudWatch Logs OR OpenSearch).
- Codify **AMP + AMG** for the Prometheus-native team — query language familiarity, Grafana dashboards, alerting via SNS.
- Codify the **dashboards & alarms baseline**: cluster health, node pressure, pod restart loops, ALB 5xx, ingress latency, AMP recording rules.
- This is the **observability specialisation**. Built on `EKS_CLUSTER_FOUNDATION` + `EKS_POD_IDENTITY`. Pairs with `EKS_NETWORKING` (ALB metrics) and any workload partial.

When the SOW signals: "we need dashboards", "production-ready monitoring", "SLO/SLI alerting", "trace requests across services", "OpenTelemetry on EKS", "Grafana for our team".

---

## 2. Decision tree — observability stack

```
Metrics destination?
├── CloudWatch only (small team, no Prometheus expertise) → §3 Container Insights
├── Prometheus-native (team uses PromQL) → §4 ADOT → AMP → AMG
└── Both (enterprise) → §3 + §4 in parallel

Logs destination?
├── CloudWatch (default) → §5 Fluent Bit → CW Logs
├── OpenSearch (search at scale) → §5 Fluent Bit → OpenSearch
└── S3 archive (compliance) → §5 Fluent Bit → Firehose → S3

Traces?
├── X-Ray (AWS-native) → §6 ADOT → X-Ray
├── Jaeger / Tempo / Zipkin → §6 ADOT → OTLP exporter
└── No tracing for POC → defer
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Container Insights + Fluent Bit + X-Ray | **§3+§5+§6 Monolith** |
| Production — full ADOT + AMP + AMG + dashboards + alarms | **§4+§7 Multi-stack** |

---

## 3. Container Insights with enhanced observability (the easy default)

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │  EKS Cluster                                         │
   │     amazon-cloudwatch-observability add-on           │
   │       ├── CloudWatch Agent (metrics + perf logs)     │
   │       └── Fluent Bit (container logs to CW Logs)     │
   └──────────────────┬───────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────────────────┐
   │  CloudWatch                                          │
   │     - /aws/containerinsights/<cluster>/performance   │
   │     - /aws/containerinsights/<cluster>/application   │
   │     - /aws/containerinsights/<cluster>/host          │
   │     - /aws/containerinsights/<cluster>/dataplane     │
   │     - Container Insights metrics (CPU/mem/net/disk)  │
   │     - Application Signals (RED metrics auto-derived) │
   └──────────────────────────────────────────────────────┘
```

### 3.2 CDK install

```python
# stacks/observability_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct


class ContainerInsightsStack(Stack):
    """Installs amazon-cloudwatch-observability add-on with enhanced observability."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_name: str,
        cluster: eks.ICluster,
        alarm_topic: sns.ITopic,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. IAM role for CloudWatch Agent (Pod Identity) ───────────
        cw_role = iam.Role(self, "CwAgentRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSXrayWriteOnlyAccess"),
            ],
        )
        cw_role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))

        eks.CfnPodIdentityAssociation(self, "CwAgentAssoc",
            cluster_name=cluster_name,
            namespace="amazon-cloudwatch",
            service_account="cloudwatch-agent",
            role_arn=cw_role.role_arn,
        )

        # ── 2. amazon-cloudwatch-observability add-on ────────────────
        # Provides: CloudWatch Agent + Fluent Bit + Container Insights enhanced
        eks.CfnAddon(self, "CwObsAddon",
            cluster_name=cluster_name,
            addon_name="amazon-cloudwatch-observability",
            addon_version="v3.0.0-eksbuild.1",
            resolve_conflicts="OVERWRITE",
            # Enable enhanced observability (App Signals, control plane metrics)
            configuration_values=json.dumps({
                "containerLogs": {
                    "enabled": True,
                    "fluentBit": {
                        "config": {
                            "service": {"flush": "5", "grace": "30"},
                        },
                    },
                },
                "agent": {
                    "config": {
                        "logs": {
                            "metrics_collected": {
                                "kubernetes": {
                                    "enhanced_container_insights": True,
                                    "accelerated_compute_metrics": True,  # GPU metrics
                                },
                            },
                        },
                        "traces": {
                            "traces_collected": {
                                "application_signals": {},   # auto RED metrics
                            },
                        },
                    },
                },
            }),
        )

        # ── 3. Log group retention (default = never expires; cap to 30d) ──
        for log_type in ["performance", "application", "host", "dataplane"]:
            logs.CfnLogGroup(self, f"LogGroup{log_type.title()}",
                log_group_name=f"/aws/containerinsights/{cluster_name}/{log_type}",
                retention_in_days=30,
            )

        # ── 4. Baseline alarms ────────────────────────────────────────
        self._create_alarms(cluster_name, alarm_topic)

    def _create_alarms(self, cluster_name, topic):
        # Cluster-wide CPU > 80% for 10 min
        cw.Alarm(self, "ClusterCpuHigh",
            metric=cw.Metric(
                namespace="ContainerInsights",
                metric_name="node_cpu_utilization",
                dimensions_map={"ClusterName": cluster_name},
                statistic="Average",
                period=Duration.minutes(5),
            ),
            threshold=80, evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="Cluster CPU > 80% for 10 min",
        ).add_alarm_action(cw_actions.SnsAction(topic))

        # Failed pod count > 5
        cw.Alarm(self, "FailedPodsHigh",
            metric=cw.Metric(
                namespace="ContainerInsights",
                metric_name="cluster_failed_node_count",
                dimensions_map={"ClusterName": cluster_name},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=5, evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        ).add_alarm_action(cw_actions.SnsAction(topic))

        # Pod restart rate > 0.5/min (likely crash loop)
        cw.Alarm(self, "PodRestartLoop",
            metric=cw.Metric(
                namespace="ContainerInsights",
                metric_name="pod_number_of_container_restarts",
                dimensions_map={"ClusterName": cluster_name},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=10, evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        ).add_alarm_action(cw_actions.SnsAction(topic))
```

---

## 4. ADOT + AMP + AMG (Prometheus-native stack)

### 4.1 Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │  Pods (instrumented w/ OTel SDK)                     │
   │     OTLP gRPC → otlp-collector:4317                  │
   └──────────────────┬───────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────────────────┐
   │  ADOT collector (DaemonSet via opentelemetry-operator)│
   │     - Receivers: OTLP, prometheus (scrape)            │
   │     - Processors: batch, k8sattributes                │
   │     - Exporters:                                      │
   │         metrics → AMP (RemoteWrite)                   │
   │         traces → X-Ray                                │
   │         logs → CloudWatch                             │
   └──────────────────┬───────────────────────────────────┘
                      │
       ┌──────────────┼──────────────┐
       ▼              ▼              ▼
   ┌───────┐    ┌──────────┐    ┌──────────┐
   │  AMP  │    │ X-Ray    │    │ CW Logs  │
   │ (Prom)│    │ (traces) │    │  (logs)  │
   └───┬───┘    └──────────┘    └──────────┘
       │
       ▼
   ┌──────────────────────────────────────────────────────┐
   │  Amazon Managed Grafana (AMG)                        │
   │     - Datasource: AMP (PromQL)                        │
   │     - Datasource: X-Ray (trace-to-log)                │
   │     - Datasource: CloudWatch Logs                     │
   │     - SAML/SSO via AWS IAM Identity Center            │
   │     - Alerting → SNS                                  │
   └──────────────────────────────────────────────────────┘
```

### 4.2 CDK — AMP workspace + ADOT

```python
# stacks/amp_amg_stack.py
from aws_cdk import Stack
from aws_cdk import aws_aps as aps        # Managed Prometheus
from aws_cdk import aws_grafana as grafana  # Managed Grafana
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks


class AmpAmgStack(Stack):
    def __init__(self, scope, id, *, cluster_name, cluster, identity_center_instance_arn,
                 alarm_topic_arn, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. AMP workspace ──────────────────────────────────────────
        amp = aps.CfnWorkspace(self, "AmpWorkspace",
            alias=f"{cluster_name}-amp",
            tags=[{"key": "cluster", "value": cluster_name}],
        )

        # AMP alert manager rules (recording + alerting)
        aps.CfnRuleGroupsNamespace(self, "AmpRules",
            name="baseline-rules",
            workspace=amp.attr_workspace_id,
            data="""
groups:
  - name: cluster
    rules:
      - record: cluster:node_cpu:ratio_rate5m
        expr: sum by(cluster) (rate(node_cpu_seconds_total{mode!="idle"}[5m]))
              / sum by(cluster) (machine_cpu_cores)
      - alert: HighPodRestartRate
        expr: rate(kube_pod_container_status_restarts_total[5m]) > 0.5
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "Pod {{ $labels.pod }} restarting frequently"
""",
        )

        # ── 2. ADOT collector via opentelemetry-operator ─────────────
        adot_role = iam.Role(self, "AdotRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
        )
        adot_role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))
        adot_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["aps:RemoteWrite", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"],
            resources=[amp.attr_arn],
        ))
        adot_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                     "logs:PutLogEvents", "logs:CreateLogStream", "logs:CreateLogGroup",
                     "cloudwatch:PutMetricData"],
            resources=["*"],
        ))

        eks.CfnPodIdentityAssociation(self, "AdotAssoc",
            cluster_name=cluster_name,
            namespace="opentelemetry-operator-system",
            service_account="adot-collector",
            role_arn=adot_role.role_arn,
        )

        # ADOT operator via add-on (ADOT operator manages collector CRDs)
        eks.CfnAddon(self, "AdotAddon",
            cluster_name=cluster_name,
            addon_name="adot",
            addon_version="v0.96.0-eksbuild.1",
            resolve_conflicts="OVERWRITE",
        )

        # ── 3. Managed Grafana workspace ──────────────────────────────
        amg_role = iam.Role(self, "AmgRole",
            assumed_by=iam.ServicePrincipal("grafana.amazonaws.com"),
        )
        amg_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels",
                     "aps:GetMetricMetadata", "aps:DescribeWorkspace",
                     "aps:ListWorkspaces"],
            resources=["*"],
        ))
        amg_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["xray:GetTraceSummaries", "xray:BatchGetTraces",
                     "xray:GetServiceGraph", "xray:GetTraceGraph",
                     "logs:DescribeLogGroups", "logs:GetLogGroupFields",
                     "logs:StartQuery", "logs:StopQuery", "logs:GetQueryResults",
                     "logs:GetLogEvents", "logs:DescribeLogStreams"],
            resources=["*"],
        ))

        amg = grafana.CfnWorkspace(self, "AmgWorkspace",
            account_access_type="CURRENT_ACCOUNT",
            authentication_providers=["AWS_SSO"],
            permission_type="SERVICE_MANAGED",
            data_sources=["PROMETHEUS", "XRAY", "CLOUDWATCH"],
            notification_destinations=["SNS"],
            role_arn=amg_role.role_arn,
            grafana_version="10.4",
        )
```

### 4.3 ADOT collector CRD (apply via kubectl/Helm)

```yaml
# manifests/adot-collector.yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: adot-collector
  namespace: opentelemetry-operator-system
spec:
  mode: daemonset
  serviceAccount: adot-collector
  image: public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0
  config:
    receivers:
      otlp:
        protocols:
          grpc: { endpoint: 0.0.0.0:4317 }
          http: { endpoint: 0.0.0.0:4318 }
      prometheus:
        config:
          scrape_configs:
            - job_name: kubernetes-pods
              kubernetes_sd_configs:
                - role: pod
              relabel_configs:
                - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
                  regex: 'true'
                  action: keep
    processors:
      batch: { timeout: 10s, send_batch_size: 1024 }
      k8sattributes:
        auth_type: serviceAccount
        passthrough: false
        extract:
          metadata: [k8s.namespace.name, k8s.pod.name, k8s.deployment.name, k8s.node.name]
      memory_limiter: { check_interval: 1s, limit_percentage: 80, spike_limit_percentage: 25 }
    exporters:
      prometheusremotewrite:
        endpoint: https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-XXXX/api/v1/remote_write
        auth: { authenticator: sigv4auth }
      awsxray:
        region: us-east-1
      awscloudwatchlogs:
        region: us-east-1
        log_group_name: /aws/eks/adot
        log_stream_name: collector
    extensions:
      sigv4auth:
        region: us-east-1
        service: aps
    service:
      extensions: [sigv4auth]
      pipelines:
        metrics:
          receivers: [otlp, prometheus]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [prometheusremotewrite]
        traces:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [awsxray]
        logs:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [awscloudwatchlogs]
```

### 4.4 Application instrumentation (Python example)

```python
# app/main.py
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource

resource = Resource.create({
    "service.name": "checkout-svc",
    "service.version": "1.4.2",
    "deployment.environment": "prod",
})

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://adot-collector:4317", insecure=True))
)
trace.set_tracer_provider(tracer_provider)

# Metrics
meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint="http://adot-collector:4317", insecure=True),
        export_interval_millis=15000,
    )],
)
metrics.set_meter_provider(meter_provider)
```

---

## 5. Fluent Bit log forwarding (alternative to Container Insights bundle)

If using ADOT for metrics+traces but want dedicated log pipeline:

```yaml
# manifests/fluent-bit.yaml — Helm values
fluentBit:
  image: public.ecr.aws/aws-observability/aws-for-fluent-bit:stable
  config:
    outputs: |
      [OUTPUT]
          Name              cloudwatch_logs
          Match             kube.*
          region            us-east-1
          log_group_name    /aws/eks/<cluster>/application
          log_stream_prefix from-fluent-bit-
          auto_create_group On
          log_retention_days 30

      [OUTPUT]
          Name              opensearch
          Match             kube.app.high-volume.*
          Host              vpc-app-XXXX.us-east-1.es.amazonaws.com
          Port              443
          tls               On
          AWS_Auth          On
          AWS_Region        us-east-1
          Index             eks-app-logs
```

---

## 6. X-Ray distributed tracing — `traces` pipeline above already routes to X-Ray. Application uses OTel SDK + `OTLPSpanExporter`. X-Ray Console shows service map.

---

## 7. Common gotchas

- **`amazon-cloudwatch-observability` add-on supersedes the older `amazon-cloudwatch/cloudwatch-agent` Helm chart.** Don't install both.
- **Enhanced Container Insights = double the metric volume = 2-3× cost.** Disable per-container metrics if cluster has > 1000 pods.
- **AMP RemoteWrite requires SigV4 auth.** Native Prometheus `remote_write` won't work — use ADOT or [aws-sigv4-proxy](https://github.com/awslabs/aws-sigv4-proxy).
- **AMG `AWS_SSO` requires Identity Center configured in same region.** Falls back to SAML if no IDC.
- **CW Log retention defaults to "Never expire"** — leaks $$$. Always set `retention_in_days`.
- **Fluent Bit OOM-kills under burst** — increase `Mem_Buf_Limit` in `[INPUT]` (default 5MB is too low). Set 50MB minimum.
- **ADOT receiver for Prometheus only scrapes pods with annotation `prometheus.io/scrape: 'true'`.** Apps without annotation are invisible.
- **Application Signals (auto RED metrics) requires Java/Python SDK auto-instrumentation.** No instrumentation = no metrics.
- **AMG seat licensing** — Editor and Admin seats cost $9/mo each, Viewer $5/mo. Don't make every developer an Admin.
- **CW agent KMS encryption optional but free** — use CMK for compliance environments.

---

## 8. Pytest worked example

```python
# tests/test_observability.py
import boto3, time, requests

eks = boto3.client("eks")
cw = boto3.client("cloudwatch")
logs = boto3.client("logs")
amp = boto3.client("amp")


def test_cw_observability_addon_active(cluster_name):
    addon = eks.describe_addon(
        clusterName=cluster_name,
        addonName="amazon-cloudwatch-observability",
    )["addon"]
    assert addon["status"] == "ACTIVE"


def test_container_insights_metrics_present(cluster_name):
    """Within 5 min of cluster activity, metrics should appear."""
    end = int(time.time())
    start = end - 600
    resp = cw.get_metric_data(
        MetricDataQueries=[{
            "Id": "cpu",
            "MetricStat": {
                "Metric": {
                    "Namespace": "ContainerInsights",
                    "MetricName": "node_cpu_utilization",
                    "Dimensions": [{"Name": "ClusterName", "Value": cluster_name}],
                },
                "Period": 60, "Stat": "Average",
            },
        }],
        StartTime=start, EndTime=end,
    )
    assert len(resp["MetricDataResults"][0]["Values"]) > 0, "No CPU metrics"


def test_log_groups_have_retention(cluster_name):
    for log_type in ["performance", "application", "host", "dataplane"]:
        lg = logs.describe_log_groups(
            logGroupNamePrefix=f"/aws/containerinsights/{cluster_name}/{log_type}",
        )["logGroups"]
        assert lg, f"Missing log group for {log_type}"
        assert lg[0].get("retentionInDays") is not None, f"No retention on {log_type}"
        assert lg[0]["retentionInDays"] <= 90, "Retention too long"


def test_amp_workspace_active(workspace_id):
    ws = amp.describe_workspace(workspaceId=workspace_id)["workspace"]
    assert ws["status"]["statusCode"] == "ACTIVE"


def test_adot_collector_running(kubeconfig):
    """kubectl-based check (integration test)."""
    # subprocess.check_call(["kubectl", "get", "pods", "-n", "opentelemetry-operator-system",
    #                        "-l", "app.kubernetes.io/name=adot-collector"])
    pass
```

---

## 9. Five non-negotiables

1. **CW Log retention ≤ 30 days for application logs** (longer for compliance — set per env).
2. **All log groups encrypted with KMS CMK** (set in `CfnLogGroup(kms_key_id=...)`).
3. **At least 3 baseline alarms wired to SNS**: cluster CPU, failed pods, restart loop.
4. **ADOT collector mode = `daemonset`** for OTLP + node-local Prometheus scrape; never `deployment` (loses node attribution).
5. **Application Signals OR custom RED metrics for every public service** — without these, MTTD on incidents > 30 min.

---

## 10. References

- [Container Insights with enhanced observability](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-setup-EKS-quickstart-EKS.html)
- [`amazon-cloudwatch-observability` add-on](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/install-CloudWatch-Observability-EKS-addon.html)
- [AWS Distro for OpenTelemetry — EKS](https://aws-otel.github.io/docs/getting-started/operator-on-eks)
- [Amazon Managed Prometheus — RemoteWrite](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-onboard-ingest-metrics-OpenTelemetry.html)
- [Amazon Managed Grafana](https://docs.aws.amazon.com/grafana/latest/userguide/what-is-Amazon-Managed-Service-Grafana.html)
- [Application Signals on EKS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Application-Signals-Enable-EKS.html)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Container Insights + ADOT + AMP/AMG + Fluent Bit + X-Ray + Application Signals + baseline alarms. Wave 9. |
