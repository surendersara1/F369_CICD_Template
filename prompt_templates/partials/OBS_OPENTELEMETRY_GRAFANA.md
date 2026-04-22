# SOP — OpenTelemetry, Managed Grafana, Managed Prometheus, RUM

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · ADOT collector · Amazon Managed Grafana · Amazon Managed Prometheus · CloudWatch RUM

---

## 1. Purpose

Third-party / open-standards observability stack on top of AWS-native telemetry:

- **ADOT (AWS Distro for OpenTelemetry)** Lambda layer for trace/metric/log forwarding
- **Amazon Managed Grafana** workspaces for exec dashboards
- **Amazon Managed Prometheus** for Prometheus-compatible metric scraping (Fargate/EKS)
- **CloudWatch RUM** for client-side real user monitoring (JS SDK)
- **X-Ray → Grafana** data source

Include when SOW mentions: Grafana, Prometheus, OpenTelemetry, distributed tracing, real user monitoring.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| All observability infra in one stack | **§3 Monolith Variant** |
| Dedicated `ObsStack` consumed by workload stacks | **§4 Micro-Stack Variant** |

No cycle risk. RUM and Grafana are read-only observers.

---

## 3. Monolith Variant

### 3.1 ADOT Lambda layer

```python
import aws_cdk as cdk
from aws_cdk import aws_lambda as _lambda


# ADOT layer (check latest ARN in docs)
adot_layer = _lambda.LayerVersion.from_layer_version_arn(
    self, "AdotLayer",
    layer_version_arn=f"arn:aws:lambda:{self.region}:901920570463:layer:aws-otel-python-amd64-ver-1-25-0:1",
)
# Attach to each Lambda:
for fn in self.lambda_functions.values():
    fn.add_layers(adot_layer)
    fn.add_environment("AWS_LAMBDA_EXEC_WRAPPER", "/opt/otel-instrument")
    fn.add_environment("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
```

### 3.2 Amazon Managed Grafana

```python
from aws_cdk import aws_grafana as grafana


self.grafana = grafana.CfnWorkspace(
    self, "GrafanaWorkspace",
    account_access_type="CURRENT_ACCOUNT",
    authentication_providers=["AWS_SSO"],
    permission_type="SERVICE_MANAGED",
    name=f"{{project_name}}-grafana-{stage}",
    data_sources=["CLOUDWATCH", "PROMETHEUS", "XRAY"],
)
```

### 3.3 Amazon Managed Prometheus

```python
from aws_cdk import aws_aps as aps


self.prom = aps.CfnWorkspace(
    self, "PromWorkspace",
    alias=f"{{project_name}}-prom-{stage}",
)
```

### 3.4 CloudWatch RUM (client-side)

```python
from aws_cdk import aws_rum as rum


self.rum_app = rum.CfnAppMonitor(
    self, "RumApp",
    name=f"{{project_name}}-rum-{stage}",
    domain="{custom_domain_name}",
    app_monitor_configuration=rum.CfnAppMonitor.AppMonitorConfigurationProperty(
        allow_cookies=False,
        enable_x_ray=True,
        session_sample_rate=0.1,   # 10% of sessions sampled
        telemetries=["performance", "errors", "http"],
    ),
    custom_events=rum.CfnAppMonitor.CustomEventsProperty(status="ENABLED"),
)
```

### 3.5 Monolith gotchas

- **ADOT layer ARN** is region-specific. Look up the latest per-region ARN from [AWS docs](https://aws-otel.github.io/docs/getting-started/lambda).
- **Grafana workspace** requires AWS SSO to be enabled in the account.
- **Prometheus scrape targets** must push to the workspace's `remote_write` endpoint (no pull model).
- **RUM domain** must be the frontend's public domain; CORS from CloudFront + RUM script are required.

---

## 4. Micro-Stack Variant

### 4.1 `ObsStack` — all advanced obs together

```python
import aws_cdk as cdk
from aws_cdk import (
    aws_grafana as grafana,
    aws_aps as aps,
    aws_rum as rum,
    aws_lambda as _lambda,
)
from constructs import Construct


class ObsStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        lambda_fns: dict[str, _lambda.IFunction] | None = None,
        frontend_domain: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-obs", **kwargs)

        # Attach ADOT to cross-stack Lambdas — layer attachment is safe (no policy mutation).
        if lambda_fns:
            adot_layer = _lambda.LayerVersion.from_layer_version_arn(
                self, "AdotLayer",
                layer_version_arn=f"arn:aws:lambda:{self.region}:901920570463:layer:aws-otel-python-amd64-ver-1-25-0:1",
            )
            for fn in lambda_fns.values():
                fn.add_layers(adot_layer)

        self.grafana = grafana.CfnWorkspace(
            self, "Grafana",
            account_access_type="CURRENT_ACCOUNT",
            authentication_providers=["AWS_SSO"],
            permission_type="SERVICE_MANAGED",
            name="{project_name}-grafana",
            data_sources=["CLOUDWATCH", "PROMETHEUS", "XRAY"],
        )
        self.prom = aps.CfnWorkspace(self, "Prom", alias="{project_name}-prom")

        if frontend_domain:
            self.rum_app = rum.CfnAppMonitor(
                self, "Rum",
                name="{project_name}-rum",
                domain=frontend_domain,
                app_monitor_configuration=rum.CfnAppMonitor.AppMonitorConfigurationProperty(
                    allow_cookies=False,
                    enable_x_ray=True,
                    session_sample_rate=0.1,
                    telemetries=["performance", "errors", "http"],
                ),
            )

        cdk.CfnOutput(self, "GrafanaUrl", value=self.grafana.attr_endpoint)
        cdk.CfnOutput(self, "PromUrl",    value=self.prom.attr_prometheus_endpoint)
```

### 4.2 Micro-stack gotchas

- **`fn.add_layers(layer)`** cross-stack adds the layer ARN to the function's LayersConfiguration (one-way). No cycle.
- **`fn.add_environment(key, value)`** cross-stack mutates the function's Environment block — this IS a mutation; CDK tracks it but requires a redeploy of the consumer stack. Use a `CfnOutput` of env-var values and let the consumer stack read them instead if independent deploy cadence matters.

---

## 5. Worked example

```python
def test_obs_stack_creates_grafana_and_prom():
    # ... instantiate ObsStack ...
    t = Template.from_stack(obs)
    t.resource_count_is("AWS::Grafana::Workspace", 1)
    t.resource_count_is("AWS::APS::Workspace", 1)
```

---

## 6. References

- `docs/Feature_Roadmap.md` — OBS-22, OBS-23, OBS-24, OBS-27, TRC-12, FE-13
- Related SOPs: `LAYER_OBSERVABILITY` (CloudWatch baseline), `LAYER_FRONTEND` (RUM domain)

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Cross-stack layer attachment is safe; env-var mutation noted. |
| 1.0 | 2026-03-05 | Initial. |
