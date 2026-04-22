# SOP — Bedrock AgentCore Observability (Token Tracking, Eval, Drift, Dashboards)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · CloudWatch Dashboards / Alarms / Logs · X-Ray Groups · SNS · DynamoDB (token records) · `bedrock-agentcore` Evaluations API · Python 3.13 Lambda canary

---

## 1. Purpose

- Provision the 3-layer agent observability plane:
  1. **Token tracking** — DynamoDB record per invocation + structured CloudWatch log, with model-specific cost math (`MODEL_PRICING`).
  2. **Online evaluation** — heuristic scorer (100 %) + sampled AgentCore Evaluations API; metrics published to `{project}/Evaluations`.
  3. **Behavioral drift** — CloudWatch alarms + EventBridge + a canary-evaluator Lambda that fingerprints agent behaviour and trips alerts.
- Provide the shared **circuit breaker** used inside agent entrypoints to stop cascading failures.
- Dashboards: agent metrics + FinOps (per-agent cost). X-Ray group for distributed tracing across agents / MCP servers.
- Include when the SOW mentions agent observability, token tracking, cost monitoring, online eval, behavioral drift, or agent quality metrics.

> **Note.** `STRANDS_EVAL` owns the *in-process* scorer and grounding validator. This SOP owns the *infrastructure* (DDB, CW, X-Ray, SNS, canary Lambda) that surrounds it.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns DDB + CW dashboards + SNS + canary Lambda + agent roles | **§3 Monolith Variant** |
| MS10-Observability owns the shared plane; agents in their own stacks publish metrics + log via identity-side grants | **§4 Micro-Stack Variant** |

**Why the split matters.** Every agent role needs `dynamodb:PutItem` on the token table, `cloudwatch:PutMetricData` on the eval namespace, and `sns:Publish` on the drift topic. In monolith these are local grants; across stacks, `table.grant_write_data(agent_role)` creates a bidirectional export. Micro-Stack publishes the resource ARNs (or names) via SSM and grants identity-side on the role.

---

## 3. Monolith Variant

**Use when:** a POC / single stack that contains the observability plane alongside the agents.

### 3.1 CDK — dashboards, X-Ray group, SNS topic, alarms, canary

```python
import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sns as sns,
    aws_xray as xray,
)


def _create_observability(self, cmk: kms.IKey) -> None:
    """Observability infra — dashboards, alarms, canary, topic, token table."""
    # Agent metrics dashboard
    cw.Dashboard(self, "AgentDashboard",
        dashboard_name="{project_name}-agent-metrics")
    cw.Dashboard(self, "FinOpsDashboard",
        dashboard_name="{project_name}-finops")

    # X-Ray group for distributed tracing
    xray.CfnGroup(self, "XRayGroup",
        group_name="{project_name}-traces",
        filter_expression='service("{project_name}-*")',
    )

    # Drift alert topic (KMS-encrypted)
    drift_topic = sns.Topic(self, "DriftAlertTopic",
        topic_name="{project_name}-behavioral-drift",
        master_key=cmk,
    )

    # Token-usage table (TTL 90 days)
    self.token_table = ddb.Table(self, "TokenTable",
        table_name="{project_name}-token-usage",
        partition_key=ddb.Attribute(name="invocation_id", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="ttl",
        encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=cmk,
        point_in_time_recovery=True,
        removal_policy=cdk.RemovalPolicy.RETAIN,
    )

    # Alarms — p95 latency above 45 s for 3 consecutive 5-min windows
    latency_alarm = cw.Alarm(self, "LatencyP95Alarm",
        alarm_name="{project_name}-agent-latency-p95",
        metric=cw.Metric(
            namespace="{project_name}/Agent",
            metric_name="AgentLatencyMs",
            statistic="p95",
            period=Duration.minutes(5),
        ),
        threshold=45_000,
        evaluation_periods=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    latency_alarm.add_alarm_action(cw_actions.SnsAction(drift_topic))

    # Composite eval-score drift alarm
    eval_alarm = cw.Alarm(self, "EvalCompositeAlarm",
        alarm_name="{project_name}-eval-composite-drift",
        metric=cw.Metric(
            namespace="{project_name}/Evaluations",
            metric_name="eval_composite",
            statistic="Average",
            period=Duration.minutes(15),
        ),
        threshold=0.5,
        evaluation_periods=2,
        comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    eval_alarm.add_alarm_action(cw_actions.SnsAction(drift_topic))

    # Canary evaluator Lambda (behavioral fingerprinting — nightly)
    canary_log = logs.LogGroup(self, "CanaryLogs",
        log_group_name="/aws/lambda/{project_name}-canary-evaluator",
        retention=logs.RetentionDays.ONE_MONTH,
    )
    _lambda.Function(self, "CanaryEvaluatorFn",
        function_name="{project_name}-canary-evaluator",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/canary_evaluator"),
        timeout=Duration.minutes(5),
        memory_size=512,
        log_group=canary_log,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "DRIFT_TOPIC_ARN": drift_topic.topic_arn,
            "TOKEN_TABLE":     self.token_table.table_name,
        },
    )
```

### 3.2 Token tracker (runs in agent container)

```python
"""Token usage tracker — DDB write + structured CloudWatch log per invocation."""
import json, logging, os, time, uuid
import boto3

logger = logging.getLogger(__name__)
_ddb   = boto3.resource('dynamodb')

MODEL_PRICING = {
    'sonnet': {'input': 0.003,  'output': 0.015},    # per 1K tokens
    'haiku':  {'input': 0.0003, 'output': 0.0015},
    # [Claude: extend with project-specific models]
}


def track_tokens(
    agent_name: str,
    model_id: str,
    agent_obj,
    query: str,
    start_time: float,
    table_name: str = '',
) -> dict:
    """Extract Strands usage metrics, compute cost, persist to DDB + emit log line."""
    elapsed_ms = int((time.time() - start_time) * 1000)
    input_tokens = output_tokens = 0

    try:
        metrics = getattr(agent_obj, 'event_loop_metrics', None)
        if metrics:
            latest = getattr(metrics, 'latest_agent_invocation', None)
            if latest and getattr(latest, 'usage', None):
                input_tokens  = latest.usage.get('inputTokens',  0) or 0
                output_tokens = latest.usage.get('outputTokens', 0) or 0
    except Exception as e:
        logger.warning('Token extraction failed: %s', e)

    pricing = MODEL_PRICING['haiku'] if 'haiku' in model_id.lower() else MODEL_PRICING['sonnet']
    cost_usd = (input_tokens * pricing['input'] + output_tokens * pricing['output']) / 1000

    record = {
        'invocation_id': str(uuid.uuid4()),
        'timestamp':     time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'agent':         agent_name,
        'model_id':      model_id,
        'input_tokens':  input_tokens,
        'output_tokens': output_tokens,
        'total_tokens':  input_tokens + output_tokens,
        'cost_usd':      str(round(cost_usd, 6)),
        'latency_ms':    elapsed_ms,
        'ttl':           int(time.time()) + (90 * 86400),
    }
    if table_name:
        try:
            _ddb.Table(table_name).put_item(Item=record)
        except Exception as e:
            logger.warning('Token DDB write failed: %s', e)

    logger.info('TOKEN_USAGE: %s', json.dumps({
        'agent':         agent_name,
        'input_tokens':  input_tokens,
        'output_tokens': output_tokens,
        'cost_usd':      round(cost_usd, 6),
        'latency_ms':    elapsed_ms,
        'model_id':      model_id,
    }))
    return record
```

### 3.3 Online evaluator (runs in agent container)

See `STRANDS_EVAL §3.2` for the full scorer. The infrastructure-side concern is that the evaluator publishes to `"{project_name}/Evaluations"`; alarms in §3.1 consume this namespace.

### 3.4 Circuit breaker (runs in agent container)

See `STRANDS_HOOKS_PLUGINS §3.5`. Reiterated here because the observability plane relies on the breaker emitting a structured log when it trips, which feeds a separate alarm:

```python
"""Emit a structured event when the breaker trips so the alarm plane can react."""
import logging, time
logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_secs: int = 60):
        self._failures     = 0
        self._last_failure = 0.0
        self._open         = False
        self._threshold    = threshold
        self._reset_secs   = reset_secs

    def check(self) -> None:
        if self._open:
            if time.time() - self._last_failure > self._reset_secs:
                self._open = False
                self._failures = 0
            else:
                logger.warning("CIRCUIT_OPEN: failures=%d", self._failures)
                raise RuntimeError(f'Circuit breaker OPEN — {self._failures} failures')

    def record_success(self) -> None:
        self._failures = 0
        self._open     = False

    def record_failure(self) -> None:
        self._failures    += 1
        self._last_failure = time.time()
        if self._failures >= self._threshold:
            self._open = True
            logger.warning("CIRCUIT_TRIPPED: threshold=%d", self._threshold)
```

### 3.5 Monolith gotchas

- **`table.grant_write_data(agent_role)`** is safe here (same stack) but becomes a cycle in micro-stack — prefer identity-side from the start so you don't have to refactor.
- **`put_metric_data`** is 150 TPS per account. Batch scorer output in a single call (§3.3 in `STRANDS_EVAL`).
- **Structured logs vs. metrics** — the `TOKEN_USAGE: { … }` line is consumed by a CloudWatch Logs metric filter elsewhere; do not change the prefix.
- **`MODEL_PRICING`** is app-side — keep in sync with AWS pricing updates. Consider loading from an SSM parameter so pricing shifts don't require a code redeploy.
- **Canary Lambda** — runs on a schedule (EventBridge rule); that rule is defined in `CICD_PIPELINE_STAGES` or `OPS_ADVANCED_MONITORING`, not here.

---

## 4. Micro-Stack Variant

**Use when:** MS10-Observability owns the shared plane; agents in their own stacks.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `table.grant_write_data(cross_stack_role)`** — identity-side `PolicyStatement` only.
3. **Never target cross-stack queues** with `targets.SqsQueue`.
4. **Never split a bucket + OAC** — not relevant here.
5. **Never set `encryption_key=ext_key`** — the token table KMS key is MS10-local (see below).

### 4.2 MS10 — `ObservabilityStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, CfnOutput,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sns as sns,
    aws_ssm as ssm,
    aws_xray as xray,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class ObservabilityStack(cdk.Stack):
    """MS10 — token table, dashboards, alarms, canary, SNS, X-Ray. ARNs via SSM."""

    def __init__(
        self,
        scope: Construct,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ms10-observability", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Local CMK for the token table + SNS topic (owned in this stack)
        cmk = kms.Key(self, "ObsKey",
            alias="alias/{project_name}-observability",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        # Token usage table
        self.token_table = ddb.Table(self, "TokenTable",
            table_name="{project_name}-token-usage",
            partition_key=ddb.Attribute(name="invocation_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # Dashboards
        cw.Dashboard(self, "AgentDashboard",
            dashboard_name="{project_name}-agent-metrics")
        cw.Dashboard(self, "FinOpsDashboard",
            dashboard_name="{project_name}-finops")

        # X-Ray group
        xray.CfnGroup(self, "XRayGroup",
            group_name="{project_name}-traces",
            filter_expression='service("{project_name}-*")',
        )

        # Drift topic
        self.drift_topic = sns.Topic(self, "DriftAlertTopic",
            topic_name="{project_name}-behavioral-drift",
            master_key=cmk,
        )

        # Alarms (same as §3.1) — SNS action on the drift topic
        # ... (condensed; see §3.1 for identical pattern)

        # Canary evaluator Lambda
        canary_log = logs.LogGroup(self, "CanaryLogs",
            log_group_name="/aws/lambda/{project_name}-canary-evaluator",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.canary_fn = _lambda.Function(self, "CanaryEvaluatorFn",
            function_name="{project_name}-canary-evaluator",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "canary_evaluator")),
            timeout=Duration.minutes(5),
            memory_size=512,
            log_group=canary_log,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DRIFT_TOPIC_ARN": self.drift_topic.topic_arn,
                "TOKEN_TABLE":     self.token_table.table_name,
            },
        )
        # Same-stack L2 grants — safe
        self.drift_topic.grant_publish(self.canary_fn)
        self.token_table.grant_read_data(self.canary_fn)
        iam.PermissionsBoundary.of(self.canary_fn.role).apply(permission_boundary)

        # Publish names/ARNs for agent stacks
        ssm.StringParameter(self, "SsmTokenTable",
            parameter_name="/{project_name}/observability/token_table",
            string_value=self.token_table.table_name,
        )
        ssm.StringParameter(self, "SsmDriftTopicArn",
            parameter_name="/{project_name}/observability/drift_topic_arn",
            string_value=self.drift_topic.topic_arn,
        )
        CfnOutput(self, "TokenTableName", value=self.token_table.table_name)
```

### 4.3 Identity-side grants in per-agent stacks

```python
# inside an agent stack, after creating agent_role
token_table_name = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/observability/token_table",
)
drift_topic_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/observability/drift_topic_arn",
)

agent_role.add_to_policy(iam.PolicyStatement(
    actions=["dynamodb:PutItem"],
    resources=[f"arn:aws:dynamodb:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/{token_table_name}"],
))
agent_role.add_to_policy(iam.PolicyStatement(
    actions=["cloudwatch:PutMetricData"],
    resources=["*"],
    conditions={"StringEquals": {"cloudwatch:namespace": [
        "{project_name}/Agent",
        "{project_name}/Evaluations",
    ]}},
))
agent_role.add_to_policy(iam.PolicyStatement(
    actions=["sns:Publish"],
    resources=[drift_topic_arn],
))

# Inject env vars
env = {
    "TOKEN_TABLE":     token_table_name,
    "DRIFT_TOPIC_ARN": drift_topic_arn,
    "EVAL_NAMESPACE":  "{project_name}/Evaluations",
}
```

### 4.4 Micro-stack gotchas

- **`cloudwatch:PutMetricData`** does not scope by namespace ARN — use the `Condition` block above with `cloudwatch:namespace`.
- **SSM-resolved ARNs in IAM `resources=[...]`** — tokenised at synth, resolved at deploy; CFN emits `${Token[…]}` in the policy JSON, which is expected.
- **Canary Lambda reads DDB in MS10**; its role grant is local so `table.grant_read_data(canary_fn)` is safe. Don't grant cross-stack to agent roles.
- **Alarm → topic → email/Slack subscription** should be in MS10 too. Cross-account delivery requires a cross-account SNS policy — never managed from the agent stack.
- **`EVAL_NAMESPACE`** env var mirrors the namespace literal baked into alarms. Keep them in sync or alarms stop firing silently.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack (MS10 owns plane) |
| Multi-account fan-out of drift alerts | Cross-account SNS subscription + resource policy in MS10 |
| Token cost tracking in Athena | Add S3 export of the DDB stream via Firehose — add to MS10 |
| PagerDuty / Slack integration | SNS → EventBridge → EB-Chatbot; defined in MS10 not agent stacks |
| FinOps attribution per tenant | Add `tenant_id` as a partition key attribute on the DDB token table; adjust dashboards |
| Disable online eval API for cost | Set `EVAL_SAMPLE_RATE=0` via SSM — no IaC change |

---

## 6. Worked example — MS10 synthesizes

Save as `tests/sop/test_AGENTCORE_OBSERVABILITY.py`. Offline.

```python
"""SOP verification — MS10 provides token table, dashboards, X-Ray, SNS, canary."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ms10_observability_stack():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ms10_observability import ObservabilityStack
    ms10 = ObservabilityStack(app, permission_boundary=boundary, env=env)

    template = Template.from_stack(ms10)
    template.resource_count_is("AWS::DynamoDB::Table",       1)
    template.resource_count_is("AWS::CloudWatch::Dashboard", 2)
    template.resource_count_is("AWS::SNS::Topic",            1)
    template.resource_count_is("AWS::XRay::Group",           1)
    template.resource_count_is("AWS::Lambda::Function",      1)   # canary
    template.resource_count_is("AWS::KMS::Key",              1)
    template.resource_count_is("AWS::SSM::Parameter",        2)
```

---

## 7. References

- `docs/template_params.md` — `TOKEN_TABLE_SSM_NAME`, `DRIFT_TOPIC_SSM_NAME`, `EVAL_NAMESPACE`, `MODEL_PRICING_SSM_NAME`, `CANARY_SCHEDULE`
- `docs/Feature_Roadmap.md` — feature IDs `OBS-14` (eval metrics), `OBS-18` (canary), `OBS-22` (drift), `FIN-02` (FinOps dashboard), `TRC-03` (X-Ray groups)
- CloudWatch Dashboards: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Dashboards.html
- AgentCore Evaluations: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-evaluations.html
- Related SOPs: `STRANDS_EVAL` (scorer + grounding), `STRANDS_HOOKS_PLUGINS` (circuit breaker in-process), `AGENTCORE_RUNTIME` (agents that emit metrics), `LAYER_OBSERVABILITY` (generic CW / metric filters / log groups), `OPS_ADVANCED_MONITORING` (EventBridge schedules + composite alarms), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MS10 owns token table + dashboards + drift topic + canary with local CMK; agent stacks read names/ARNs via SSM and grant identity-side (`dynamodb:PutItem`, `cloudwatch:PutMetricData` with namespace `Condition`, `sns:Publish`). Translated CDK from TypeScript to Python. Added Swap matrix (§5), Worked example (§6), Gotchas. Referenced `STRANDS_EVAL` and `STRANDS_HOOKS_PLUGINS` for in-process scorer + circuit-breaker rather than duplicating. |
| 1.0 | 2026-03-05 | Initial — observability stack (TS), token tracker, online evaluator, circuit breaker. |
