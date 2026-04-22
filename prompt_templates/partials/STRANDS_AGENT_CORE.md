# SOP — Strands Agent Core (Agent, Tools, Model Fallback, Streaming)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1.0 · `strands-agents-tools` · `bedrock-agentcore` runtime · Python 3.12+ · boto3 ≥ 1.34

---

## 1. Purpose

- Build a Strands-SDK agent entrypoint (`@app.entrypoint`) that runs inside an AgentCore Runtime container, Fargate task, or container Lambda.
- Codify the supervisor / sub-agent orchestration pattern (ThreadPoolExecutor for parallel sub-agent calls, Strands `Agent()` for synthesis).
- Codify model-fallback (Sonnet for complex, Haiku for simple) and deterministic keyword tool routing before LLM fallback.
- Codify WebSocket streaming via `callback_handler` and 3-layer RBAC via hooks.
- Include when the SOW mentions Strands SDK, custom AI agents, agentic AI, or any Strands-based agent implementation.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** Strands is a framework library, not a CDK construct family — nothing is provisioned here. §3 is the single canonical variant (the agent's Python code).
>
> For deployment topology of this agent (where the container runs, how the IAM execution role is granted), see:
> - `AGENTCORE_RUNTIME` — recommended default (managed microVM + 8-hour session window)
> - `STRANDS_DEPLOY_ECS` — self-hosted Fargate service
> - `STRANDS_DEPLOY_LAMBDA` — container-image Lambda for short-lived agents

§4 Micro-Stack Variant is intentionally omitted. The five non-negotiables from `LAYER_BACKEND_LAMBDA §4.1` apply to the **deploy** partials referenced above, not to agent code.

---

## 3. Canonical Variant

### 3.1 Agent architecture

```
Agent Architecture (supervisor flow):
  BedrockAgentCoreApp (entrypoint wrapper)
    ↓
  Model Selection: classify_query_complexity() → Sonnet or Haiku
    ↓
  Memory Recall: retrieve_memory_records() → inject past context
    ↓
  RBAC: AfieSteeringHooks → check agent/tool/data access
    ↓
  Sub-Agent Execution: parallel ThreadPoolExecutor
    ↓
  Synthesis: Agent(model, tools, system_prompt, callback_handler, session_manager)
    ↓
  Grounding Validation: verify numbers against source data
    ↓
  Memory Store: create_event() → persist to LTM
    ↓
  Online Evaluation: 10% quality sampling
```

### 3.2 Packages

```bash
pip install strands-agents strands-agents-tools
pip install bedrock-agentcore
pip install boto3
```

Pin versions in `requirements.txt` so the CDK container asset is reproducible.

### 3.3 Supervisor agent (orchestrator)

Calls sub-agents in parallel, synthesizes an answer.

```python
"""Supervisor Agent — orchestrates sub-agents, synthesizes Intelligence Brief."""
import json, logging, os, uuid, time
from concurrent.futures import ThreadPoolExecutor

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.session import S3SessionManager

from shared.model_builder import build_model_with_fallback
from shared.ws_callback import WebSocketCallbackHandler
from shared.token_tracker import track_tokens
from shared.ssm_helper import ssm_get

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()
agentcore_client = boto3.client('bedrock-agentcore')

# Config loaded at container startup from SSM
MODEL_ID          = ssm_get('/{project_name}/runtime/default_model')
FALLBACK_MODEL_ID = ssm_get('/{project_name}/runtime/fallback_model')
OBSERVER_ARN      = ssm_get('/{project_name}/agents/observer_agent_arn')
REASONER_ARN      = ssm_get('/{project_name}/agents/reasoner_agent_arn')
MEMORY_ID         = ssm_get('/{project_name}/memory/id', '')
SESSION_BUCKET    = os.environ.get('SESSION_BUCKET', '')


# ── Sub-agent invocation ────────────────────────────────────────────────

def _call_sub_agent(runtime_arn: str, payload: dict) -> str:
    """Invoke sub-agent on AgentCore Runtime."""
    resp = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        contentType='application/json',
        payload=json.dumps(payload).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=str(uuid.uuid4()),
    )
    return resp.get('response').read().decode('utf-8')


def _parallel_observe_and_reason(query: str) -> tuple[str, str]:
    """Run Observer and Reasoner in parallel (≤ 120 s each)."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        obs = pool.submit(_call_sub_agent, OBSERVER_ARN, {'prompt': query})
        rea = pool.submit(_call_sub_agent, REASONER_ARN, {'prompt': query})
        return obs.result(timeout=120), rea.result(timeout=120)


# ── Agent tools ─────────────────────────────────────────────────────────

@tool
def observer_agent(query: str) -> str:
    """Retrieve data from the enterprise data layer.

    Args:
        query: The data-retrieval question.
    Returns:
        Structured data with metrics and anomalies.
    """
    return _call_sub_agent(OBSERVER_ARN, {'prompt': query})


@tool
def reasoner_agent(query: str, observation_data: str = "") -> str:
    """Root-cause analysis using graph traversal.

    Args:
        query: The analysis question.
        observation_data: Raw data from observer to analyze.
    Returns:
        Causal analysis with root causes and impact chains.
    """
    prompt = query
    if observation_data:
        prompt += f"\n\nObservation data: {observation_data[:3000]}"
    return _call_sub_agent(REASONER_ARN, {'prompt': prompt})

# [Claude: add more @tool functions based on SOW capabilities]


SYSTEM_PROMPT = """You are the {project_name} Supervisor Agent.
You orchestrate specialist agents and synthesize results.
# [Claude: customize based on SOW agent persona and rules]
"""


@app.entrypoint
def invoke(payload):
    query         = payload.get('prompt', '')
    actor_id      = payload.get('role', 'user')
    session_id    = payload.get('runtimeSessionId', str(uuid.uuid4()))
    connection_id = payload.get('connectionId', '')
    ws_endpoint   = payload.get('wsEndpoint', '')

    start = time.time()

    # Model selection: simple → Haiku, complex → Sonnet
    model, selected_model_id, complexity = build_model_with_fallback(
        query, MODEL_ID, FALLBACK_MODEL_ID)

    # Streaming callback (if WebSocket connection available)
    callbacks = []
    if connection_id and ws_endpoint:
        callbacks.append(WebSocketCallbackHandler(connection_id, ws_endpoint))

    # Memory recall (function defined in STRANDS_HOOKS_PLUGINS / AGENTCORE_MEMORY)
    memory_context = recall_memory(query, actor_id) if MEMORY_ID else ''

    # Parallel sub-agent execution
    observation, reasoning = _parallel_observe_and_reason(query)

    # Synthesis via Strands Agent
    sess_mgr = S3SessionManager(
        session_id=session_id, bucket=SESSION_BUCKET,
        prefix=f'sessions/{actor_id}/',
    ) if SESSION_BUCKET else None

    synthesizer = Agent(
        model=model,
        tools=[observer_agent, reasoner_agent],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=callbacks[0] if callbacks else None,
        session_manager=sess_mgr,
    )

    synthesis_prompt = (
        f"Query: {query}\n\n"
        f"{'=== Past Context ===\n' + memory_context + '\n\n' if memory_context else ''}"
        f"=== Observer Data ===\n{observation[:4000]}\n\n"
        f"=== Reasoner Analysis ===\n{reasoning[:4000]}\n\n"
        f"Synthesize a response from the above data."
    )
    result = synthesizer(synthesis_prompt)

    # Memory store
    if MEMORY_ID:
        store_memory(query, str(result), actor_id, session_id)

    # Token tracking
    token_data = track_tokens('Supervisor', selected_model_id, synthesizer, query, start)

    return {
        "result":           str(result),
        "model_used":       selected_model_id,
        "query_complexity": complexity,
        "token_usage":      token_data,
    }

if __name__ == "__main__":
    app.run()
```

### 3.4 Observer (data-retrieval sub-agent)

Connects to AgentCore Gateway via MCP and routes queries deterministically before LLM fallback.

```python
"""Observer Agent — retrieves data via AgentCore Gateway MCP tools."""
import os
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.tools.mcp.mcp_client import MCPClient

from shared.sigv4_auth import create_gateway_transport
from shared.tool_router import route_query
from shared.model_builder import build_bedrock_model

app = BedrockAgentCoreApp()
gateway_client = MCPClient(lambda: create_gateway_transport(os.environ['GATEWAY_URL']))

MODEL_ID = os.environ['MODEL_ID']
SYSTEM_PROMPT = "You are an Observer agent. Retrieve data, surface anomalies."


@app.entrypoint
def invoke(payload):
    query = payload.get('prompt', '')

    with gateway_client:
        all_tools = gateway_client.list_tools_sync()

        # Smart routing: keyword match → specific tools (skip LLM tool selection)
        routed_tools, is_routed = route_query(query, all_tools)

        observer = Agent(
            model=build_bedrock_model(MODEL_ID),
            tools=routed_tools,
            system_prompt=SYSTEM_PROMPT,
        )
        result = observer(query)

    return {"result": str(result), "routing": "deterministic" if is_routed else "llm_fallback"}

if __name__ == "__main__":
    app.run()
```

### 3.5 Model fallback with complexity classification

```python
"""Model selection: simple → Haiku (~10× cheaper), complex → Sonnet."""
import re
from strands.models import BedrockModel

SIMPLE  = re.compile(r'\b(what is|show me|current|latest|how much|list|get|total)\b', re.I)
COMPLEX = re.compile(r'\b(why|root cause|analyze|compare|recommend|forecast|simulate|variance)\b', re.I)


def classify_query_complexity(query: str) -> str:
    complex_matches = len(COMPLEX.findall(query))
    simple_matches  = len(SIMPLE.findall(query))
    if complex_matches >= 2:
        return 'complex'
    if simple_matches >= 1 and complex_matches == 0:
        return 'simple'
    return 'complex' if len(query) > 100 else 'simple'


def build_model_with_fallback(query, primary_id, fallback_id,
                              guardrail_id='', guardrail_version='DRAFT'):
    complexity = classify_query_complexity(query)
    model_id = fallback_id if complexity == 'simple' else primary_id
    model = build_bedrock_model(model_id, guardrail_id, guardrail_version)
    return model, model_id, complexity


def build_bedrock_model(model_id, guardrail_id='', guardrail_version='DRAFT'):
    if guardrail_id:
        return BedrockModel(model_id=model_id, additional_request_fields={
            'guardrailConfig': {
                'guardrailIdentifier': guardrail_id,
                'guardrailVersion':    guardrail_version,
                'trace':               'enabled',
            }
        })
    return BedrockModel(model_id=model_id)
```

### 3.6 Smart tool routing (deterministic keyword match)

```python
"""Deterministic keyword routing before LLM tool-selection fallback."""
import re

ROUTING_RULES = [
    (r'margin|revenue|p&l|profit|cogs|ebitda',   ['get_pnl_history']),
    (r'vendor|supplier|spend|procurement',       ['get_vendor_spend']),
    (r'cash|treasury|covenant|liquidity',        ['get_cash_balance']),
    (r'inventory|stock|warehouse',               ['get_inventory_levels']),
    (r'budget|variance|actual',                  ['get_budget_vs_actual']),
    # [Claude: add routing rules based on SOW tool names]
]


def route_query(query: str, available_tools: list) -> tuple[list, bool]:
    query_lower = query.lower()
    matched = set()
    for pattern, tools in ROUTING_RULES:
        if re.search(pattern, query_lower):
            matched.update(tools)
    if not matched:
        return available_tools, False  # LLM fallback
    routed = [t for t in available_tools if getattr(t, 'name', str(t)) in matched]
    return (routed, True) if routed else (available_tools, False)
```

### 3.7 WebSocket streaming callback

```python
"""Real-time streaming of agent reasoning steps to the portal."""
import json, boto3


class WebSocketCallbackHandler:
    """Strands callback_handler — pushes real agent events to WebSocket."""

    TOOL_AGENT_MAP = {
        'observer_agent': 'Observer',
        'reasoner_agent': 'Reasoner',
        # [Claude: map tool names to display labels]
    }

    def __init__(self, connection_id: str, ws_endpoint: str):
        self.connection_id = connection_id
        self.ws_endpoint   = ws_endpoint
        self._apigw = boto3.client('apigatewaymanagementapi', endpoint_url=ws_endpoint)
        self._step_count = 0

    def __call__(self, **kwargs):
        """Strands callback protocol."""
        event = kwargs.get('event', {})
        tool_use = event.get('contentBlockStart', {}).get('start', {}).get('toolUse')
        if tool_use:
            self._step_count += 1
            tool_name = tool_use.get('name', 'unknown')
            self._post({
                'type':   'progress',
                'agent':  self.TOOL_AGENT_MAP.get(tool_name, 'Agent'),
                'detail': f'Calling {tool_name}...',
                'step':   self._step_count,
            })

    def _post(self, data: dict):
        try:
            self._apigw.post_to_connection(
                ConnectionId=self.connection_id,
                Data=json.dumps(data).encode(),
            )
        except Exception:
            # WebSocket can be gone (client disconnected). Don't crash the agent.
            pass

    def send_custom_step(self, agent: str, phase: str, detail: str):
        self._step_count += 1
        self._post({'type': 'progress', 'agent': agent, 'phase': phase, 'detail': detail})
```

### 3.8 3-layer RBAC hooks

Full hook implementation, including Strands `HookProvider` wiring, is in `STRANDS_HOOKS_PLUGINS`. Minimal shape below.

```python
"""3-layer RBAC: agent access → tool access → data filtering."""
import re


class AfieSteeringHooks:
    def __init__(self, client_id: str, persona: str = 'user', rbac_policy: dict = None):
        self.client_id = client_id
        self.persona   = persona
        self.rbac      = rbac_policy or {}

    def check_agent_access(self, agent_name: str) -> bool:
        access = self.rbac.get('agent_access', {})
        if access.get(agent_name) is False:
            raise PermissionError(f"'{self.persona}' not authorized for {agent_name}")
        return True

    def before_tool_call(self, tool_name: str, tool_input: dict) -> dict:
        # Layer 2: Tool whitelist/blacklist
        denied = self.rbac.get('tool_access', {}).get('denied', [])
        if tool_name in denied:
            return {'__rbac_denied': True, 'error': f'Tool {tool_name} denied for {self.persona}'}
        # Layer 3: Inject SQL filters
        sql_filter = self.rbac.get('data_filter', {}).get('sql_filter', '')
        if sql_filter and 'sql' in tool_input:
            tool_input['sql'] += f' {sql_filter}'
        return tool_input

    def after_tool_call(self, tool_name: str, output: str) -> str:
        # Layer 3: Mask restricted fields
        for field in self.rbac.get('data_filter', {}).get('mask_fields', []):
            output = re.sub(rf'"{field}"\s*:\s*"[^"]*"', f'"{field}": "[RESTRICTED]"', output)
        return output
```

### 3.9 Gotchas

- **`@app.entrypoint` must return a JSON-serializable dict.** Returning a `strands.AgentResult` directly fails the AgentCore Runtime response contract. Always wrap with `str(result)`.
- **`ThreadPoolExecutor.result(timeout=…)` swallows sub-agent exceptions inside the future**. Check `future.exception()` before `result()` if you need to distinguish timeout from downstream failure.
- **`BedrockModel(additional_request_fields=…)` is silently dropped** if you pass the guardrail block under the wrong key. The correct shape is `{'guardrailConfig': {...}}` under `additional_request_fields`, not `model_kwargs`.
- **`S3SessionManager` writes on every invoke** — on cold container + cold bucket this adds ~200 ms. Set `SESSION_BUCKET=""` in dev to skip.
- **`gateway_client.list_tools_sync()` inside `with gateway_client:`** — the `with` block is required; the client's transport isn't open outside of it. Calling `list_tools_sync()` on a closed transport raises a non-obvious `RuntimeError`.
- **`post_to_connection` on a closed WebSocket raises `GoneException`.** Catch broadly — do not let it bubble and kill the agent turn.

---

## 5. Swap matrix — configuration variants

| Need | Swap |
|---|---|
| Local dev, no Bedrock calls | Replace `BedrockModel(...)` with `OllamaModel(...)` (`from strands.models import OllamaModel`). Same agent code. |
| Multi-turn chat with server-side history | Set `SESSION_BUCKET` env → `S3SessionManager(...)` auto-persists. Leave empty for stateless. |
| Cross-region latency | Use cross-region inference profiles (`us.anthropic.claude-sonnet-…`) in `MODEL_ID`; no code change. |
| No streaming required | Omit `callback_handler=`. The agent still returns the final result synchronously. |
| Single-agent (no sub-agents) | Remove `_parallel_observe_and_reason`; `Agent(tools=[...direct MCP tools...])`. |
| Memory (LTM) needed | Set `MEMORY_ID` from SSM; `recall_memory()` / `store_memory()` defined in `AGENTCORE_MEMORY`. |

---

## 6. Worked example — verify supervisor constructs cleanly

Save as `tests/sop/test_STRANDS_AGENT_CORE.py`. Runs offline; no Bedrock calls.

```python
"""SOP verification — supervisor agent constructs without IO."""
from unittest.mock import patch, MagicMock
from strands import Agent
from strands.models import BedrockModel


def test_complexity_classifier():
    from shared.model_builder import classify_query_complexity
    assert classify_query_complexity("what is revenue?") == 'simple'
    assert classify_query_complexity(
        "Why did margin drop and analyze the root cause compared to budget variance?"
    ) == 'complex'


def test_agent_constructs_with_tools():
    model = BedrockModel(model_id="anthropic.claude-haiku-4-5-20251001-v1:0")

    @patch('boto3.client')
    def _build(mock_client):
        from strands import tool

        @tool
        def dummy(query: str) -> str:
            """dummy tool"""
            return "ok"

        return Agent(model=model, tools=[dummy], system_prompt="test")

    agent = _build()
    assert agent is not None


def test_router_matches_known_keywords():
    from shared.tool_router import route_query
    tools = [MagicMock(name='get_pnl_history', **{'name': 'get_pnl_history'})]
    routed, is_routed = route_query("show me the revenue", tools)
    assert is_routed is True
    assert len(routed) == 1
```

---

## 7. References

- `docs/template_params.md` — `MODEL_ID`, `FALLBACK_MODEL_ID`, `GATEWAY_URL`, `MEMORY_ID`, `SESSION_BUCKET`
- `docs/Feature_Roadmap.md` — feature IDs `STR-01..STR-11` (Strands core), `A-15..A-22` (Bedrock model selection)
- Strands Agents SDK: https://strandsagents.com
- Bedrock AgentCore Runtime: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core.html
- Related SOPs: `STRANDS_TOOLS` (custom `@tool`), `STRANDS_MODEL_PROVIDERS` (Bedrock / Ollama swap), `STRANDS_MULTI_AGENT` (A2A orchestration), `STRANDS_HOOKS_PLUGINS` (hook wiring), `STRANDS_MCP_TOOLS` (MCP client), `STRANDS_DEPLOY_LAMBDA`, `STRANDS_DEPLOY_ECS`, `AGENTCORE_RUNTIME`, `AGENTCORE_MEMORY`

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added Gotchas (§3.9), Swap matrix (§5), Worked example (§6). Cross-linked to deploy and MCP SOPs. Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — supervisor / observer / model-fallback / tool-routing / WS callback / RBAC hook patterns from production codebase. |
