# PARTIAL: AgentCore Observability — Token Tracking, Online Eval, Circuit Breaker, Dashboards

**Usage:** Include when SOW mentions agent observability, token tracking, cost monitoring, online evaluation, behavioral drift, or agent quality metrics.

---

## Observability Architecture (from real production)

```
3-Layer Observability:
  1. Token Tracking: DynamoDB + structured CloudWatch logs per invocation
  2. Online Evaluation: Heuristic scoring (100%) + AgentCore Evaluations API (sampled)
  3. Behavioral Drift: CloudWatch alarms + EventBridge + canary evaluator

Infrastructure:
  DynamoDB (token records) → CloudWatch Metrics → Dashboards + Alarms
  AgentCore Evaluations API → CloudWatch custom namespace
  X-Ray groups → distributed tracing across agents
  EventBridge → SNS drift alerts
  Circuit Breaker → prevent cascading failures
```

---

## CDK Code Block — Observability Stack

```typescript
// infra/lib/stacks/ms-10-observability-stack.ts

// Agent metrics dashboard
const agentDashboard = new cloudwatch.Dashboard(this, 'AgentDashboard', {
  dashboardName: `{{project_name}}-agent-metrics`,
});

// FinOps dashboard (per-agent cost tracking)
const finOpsDashboard = new cloudwatch.Dashboard(this, 'FinOpsDashboard', {
  dashboardName: `{{project_name}}-finops`,
});

// X-Ray group for distributed tracing
new xray.CfnGroup(this, 'XRayGroup', {
  groupName: `{{project_name}}-traces`,
  filterExpression: 'service("{{project_name}}-*")',
});

// Drift alert topic
const driftTopic = new sns.Topic(this, 'DriftAlertTopic', {
  topicName: `{{project_name}}-behavioral-drift`,
  masterKey: cmk,
});

// Latency P95 alarm
new cloudwatch.Alarm(this, 'LatencyP95Alarm', {
  metric: new cloudwatch.Metric({
    namespace: `{{project_name}}/Agent`,
    metricName: 'AgentLatencyMs',
    statistic: 'p95',
    period: cdk.Duration.minutes(5),
  }),
  threshold: 45000,  // 45 seconds
  evaluationPeriods: 3,
});

// Canary evaluator Lambda (behavioral fingerprinting)
new lambda.Function(this, 'CanaryEvaluatorFn', {
  functionName: `{{project_name}}-canary-evaluator`,
  runtime: lambda.Runtime.PYTHON_3_13,
  handler: 'index.handler',
  code: lambda.Code.fromAsset('lambda/canary_evaluator'),
});
```

---

## Token Tracker — Pass 3 Reference

```python
"""Token usage tracking — DynamoDB + structured CloudWatch log per invocation."""
import json, logging, os, time, uuid
import boto3

logger = logging.getLogger(__name__)
_ddb = boto3.resource('dynamodb')

MODEL_PRICING = {
    'sonnet': {'input': 0.003, 'output': 0.015},  # per 1K tokens
    'haiku': {'input': 0.0003, 'output': 0.0015},
}

def track_tokens(agent_name: str, model_id: str, agent_obj, query: str,
                 start_time: float, table_name: str = '') -> dict:
    elapsed_ms = int((time.time() - start_time) * 1000)
    input_tokens = output_tokens = 0

    try:
        metrics = getattr(agent_obj, 'event_loop_metrics', None)
        if metrics:
            latest = getattr(metrics, 'latest_agent_invocation', None)
            if latest and hasattr(latest, 'usage') and latest.usage:
                input_tokens = latest.usage.get('inputTokens', 0) or 0
                output_tokens = latest.usage.get('outputTokens', 0) or 0
    except Exception as e:
        logger.warning('Token extraction failed: %s', e)

    pricing = MODEL_PRICING['haiku'] if 'haiku' in model_id.lower() else MODEL_PRICING['sonnet']
    cost_usd = (input_tokens * pricing['input'] + output_tokens * pricing['output']) / 1000

    record = {
        'invocation_id': str(uuid.uuid4()),
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'agent': agent_name, 'model_id': model_id,
        'input_tokens': input_tokens, 'output_tokens': output_tokens,
        'total_tokens': input_tokens + output_tokens,
        'cost_usd': str(round(cost_usd, 6)), 'latency_ms': elapsed_ms,
        'ttl': int(time.time()) + (90 * 86400),
    }

    if table_name:
        try: _ddb.Table(table_name).put_item(Item=record)
        except Exception as e: logger.warning('Token DDB write failed: %s', e)

    logger.info('TOKEN_USAGE: %s', json.dumps({
        'agent': agent_name, 'input_tokens': input_tokens,
        'output_tokens': output_tokens, 'cost_usd': round(cost_usd, 6),
        'latency_ms': elapsed_ms, 'model_id': model_id,
    }))
    return record
```

---

## Online Evaluator — Pass 3 Reference

```python
"""Online evaluation — heuristic scoring + AgentCore Evaluations API."""
import json, logging, os, random, re
import boto3

logger = logging.getLogger(__name__)
_cw = boto3.client('cloudwatch')
_bedrock = boto3.client('bedrock-agentcore')

class OnlineEvaluator:
    def evaluate_response(self, query: str, result: str, tool_calls: list[str]) -> dict:
        scores = {
            'faithfulness': self._score_faithfulness(result, tool_calls),
            'tool_selection': self._score_tool_selection(query, tool_calls),
            'completeness': self._score_completeness(result),
        }
        scores['composite'] = (
            scores['faithfulness'] * 0.4 +
            scores['tool_selection'] * 0.3 +
            scores['completeness'] * 0.3
        )

        # AgentCore Evaluations API (sampled — more expensive)
        if random.random() < 0.5:
            for evaluator_id in ['builtin:correctness', 'builtin:faithfulness', 'builtin:helpfulness']:
                try:
                    resp = _bedrock.evaluate(
                        evaluatorId=evaluator_id,
                        evaluationInput={'query': query[:2000], 'response': result[:4000]},
                    )
                    scores[f'ac_{evaluator_id.split(":")[1]}'] = resp.get('evaluationResult', {}).get('score', 0.5)
                except Exception:
                    pass

        self._publish_metrics(scores)
        return scores

    def _score_faithfulness(self, result, tool_calls):
        result_numbers = set(re.findall(r'\d+\.?\d*', result))
        if not result_numbers: return 1.0
        tool_numbers = set(re.findall(r'\d+\.?\d*', ' '.join(tool_calls)))
        if not tool_numbers: return 0.5
        return len(result_numbers & tool_numbers) / len(result_numbers)

    def _score_tool_selection(self, query, tool_calls):
        # [Claude: customize keyword→tool mapping based on SOW]
        return 1.0 if tool_calls else 0.5

    def _score_completeness(self, result):
        checks = [len(result) > 200, bool(re.search(r'\d+\.?\d*%', result)),
                   bool(re.search(r'recommend|action|suggest', result, re.I))]
        return sum(checks) / len(checks)

    def _publish_metrics(self, scores):
        try:
            _cw.put_metric_data(Namespace='{{project_name}}/Evaluations',
                MetricData=[{'MetricName': f'eval_{k}', 'Value': v, 'Unit': 'None'}
                            for k, v in scores.items() if isinstance(v, (int, float))])
        except Exception: pass
```

---

## Circuit Breaker — Pass 3 Reference

```python
"""Circuit breaker — prevents cascading failures across agents."""
import time, logging

class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_secs: int = 60):
        self._failures = 0
        self._last_failure = 0.0
        self._open = False
        self._threshold = threshold
        self._reset_secs = reset_secs

    def check(self):
        if self._open:
            if time.time() - self._last_failure > self._reset_secs:
                self._open = False
                self._failures = 0
            else:
                raise RuntimeError(f'Circuit breaker OPEN — {self._failures} failures')

    def record_success(self):
        self._failures = 0
        self._open = False

    def record_failure(self):
        self._failures += 1
        self._last_failure = time.time()
        if self._failures >= self._threshold:
            self._open = True
```
