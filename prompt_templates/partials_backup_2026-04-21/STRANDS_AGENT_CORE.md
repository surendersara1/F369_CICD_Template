# PARTIAL: Strands Agent Core — Agent Class, Tools, Model Fallback, Streaming

**Usage:** Include when SOW mentions Strands SDK, custom AI agents, agentic AI, or any Strands-based agent implementation.

---

## Strands Agent Core Patterns (from real production)

```
Agent Architecture:
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

---

## Packages

```bash
pip install strands-agents strands-agents-tools
pip install bedrock-agentcore
pip install boto3
```

---

## Supervisor Agent Pattern — Pass 3 Reference

The supervisor is the orchestrator. It calls sub-agents in parallel, then synthesizes.

```python
"""Supervisor Agent — orchestrates sub-agents, synthesizes Intelligence Brief."""
import json, logging, os, uuid, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.session import S3SessionManager

from shared.model_builder import build_model_with_fallback
from shared.steering_hooks import AfieSteeringHooks
from shared.ws_callback import WebSocketCallbackHandler
from shared.token_tracker import track_tokens
from shared.ssm_helper import ssm_get

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()
agentcore_client = boto3.client('bedrock-agentcore')

# Config loaded at container startup from SSM
MODEL_ID = ssm_get('/{{project_name}}/runtime/default_model')
FALLBACK_MODEL_ID = ssm_get('/{{project_name}}/runtime/fallback_model')
OBSERVER_ARN = ssm_get('/{{project_name}}/agents/observer_agent_arn')
REASONER_ARN = ssm_get('/{{project_name}}/agents/reasoner_agent_arn')
MEMORY_ID = ssm_get('/{{project_name}}/memory/id', '')
SESSION_BUCKET = os.environ.get('SESSION_BUCKET', '')


# ── Sub-agent invocation ─────────────────────────────────────────────────

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
    """Run Observer and Reasoner in parallel."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        obs = pool.submit(_call_sub_agent, OBSERVER_ARN, {'prompt': query})
        rea = pool.submit(_call_sub_agent, REASONER_ARN, {'prompt': query})
        return obs.result(timeout=120), rea.result(timeout=120)


# ── Agent tools ──────────────────────────────────────────────────────────

@tool
def observer_agent(query: str) -> str:
    """Retrieve financial data from data warehouse.
    Args:
        query: The data retrieval question.
    Returns:
        Structured financial data with metrics and anomalies.
    """
    return _call_sub_agent(OBSERVER_ARN, {'prompt': query})

@tool
def reasoner_agent(query: str, observation_data: str = "") -> str:
    """Root cause analysis using graph traversal.
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


SYSTEM_PROMPT = """You are the {{project_name}} Supervisor Agent.
You orchestrate specialist agents and synthesize results.
# [Claude: customize based on SOW agent persona and rules]
"""


@app.entrypoint
def invoke(payload):
    query = payload.get('prompt', '')
    actor_id = payload.get('role', 'user')
    session_id = payload.get('runtimeSessionId', str(uuid.uuid4()))
    connection_id = payload.get('connectionId', '')
    ws_endpoint = payload.get('wsEndpoint', '')

    start = time.time()

    # Model selection: simple→Haiku, complex→Sonnet
    model, selected_model_id, complexity = build_model_with_fallback(
        query, MODEL_ID, FALLBACK_MODEL_ID)

    # Streaming callback (if WebSocket connection available)
    callbacks = []
    if connection_id and ws_endpoint:
        callbacks.append(WebSocketCallbackHandler(connection_id, ws_endpoint))

    # Memory recall
    memory_context = recall_memory(query, actor_id) if MEMORY_ID else ''

    # Parallel sub-agent execution
    observation, reasoning = _parallel_observe_and_reason(query)

    # Synthesis via Strands Agent
    sess_mgr = S3SessionManager(session_id=session_id, bucket=SESSION_BUCKET,
        prefix=f'sessions/{actor_id}/') if SESSION_BUCKET else None

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
        "result": str(result),
        "model_used": selected_model_id,
        "query_complexity": complexity,
        "token_usage": token_data,
    }

if __name__ == "__main__":
    app.run()
```

---

## Observer Agent Pattern — Pass 3 Reference

Connects to Gateway via MCP, retrieves data with smart tool routing.

```python
"""Observer Agent — retrieves data via AgentCore Gateway MCP tools."""
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.tools.mcp.mcp_client import MCPClient

from shared.sigv4_auth import create_gateway_transport
from shared.tool_router import route_query
from shared.model_builder import build_bedrock_model

app = BedrockAgentCoreApp()
gateway_client = MCPClient(lambda: create_gateway_transport(os.environ['GATEWAY_URL']))

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

---

## Model Fallback with Complexity Classification — Pass 3 Reference

```python
"""Model selection: simple→Haiku (10x cheaper), complex→Sonnet."""
import re
from strands.models import BedrockModel

SIMPLE = re.compile(r'\b(what is|show me|current|latest|how much|list|get|total)\b', re.I)
COMPLEX = re.compile(r'\b(why|root cause|analyze|compare|recommend|forecast|simulate|variance)\b', re.I)

def classify_query_complexity(query: str) -> str:
    complex_matches = len(COMPLEX.findall(query))
    simple_matches = len(SIMPLE.findall(query))
    if complex_matches >= 2: return 'complex'
    if simple_matches >= 1 and complex_matches == 0: return 'simple'
    return 'complex' if len(query) > 100 else 'simple'

def build_model_with_fallback(query, primary_id, fallback_id, guardrail_id='', guardrail_version='DRAFT'):
    complexity = classify_query_complexity(query)
    model_id = fallback_id if complexity == 'simple' else primary_id
    model = build_bedrock_model(model_id, guardrail_id, guardrail_version)
    return model, model_id, complexity

def build_bedrock_model(model_id, guardrail_id='', guardrail_version='DRAFT'):
    if guardrail_id:
        return BedrockModel(model_id=model_id, additional_request_fields={
            'guardrailConfig': {
                'guardrailIdentifier': guardrail_id,
                'guardrailVersion': guardrail_version,
                'trace': 'enabled',
            }
        })
    return BedrockModel(model_id=model_id)
```

---

## Smart Tool Routing — Pass 3 Reference

```python
"""Deterministic keyword routing before LLM fallback."""
import re

ROUTING_RULES = [
    (r'margin|revenue|p&l|profit|cogs|ebitda', ['get_pnl_history']),
    (r'vendor|supplier|spend|procurement', ['get_vendor_spend']),
    (r'cash|treasury|covenant|liquidity', ['get_cash_balance']),
    (r'inventory|stock|warehouse', ['get_inventory_levels']),
    (r'budget|variance|actual', ['get_budget_vs_actual']),
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
    routed = [t for t in available_tools
              if getattr(t, 'name', str(t)) in matched]
    return (routed, True) if routed else (available_tools, False)
```

---

## WebSocket Streaming Callback — Pass 3 Reference

```python
"""Real-time streaming of agent reasoning steps to portal."""
import json, time, boto3

class WebSocketCallbackHandler:
    """Strands callback_handler — pushes real agent events to WebSocket."""

    TOOL_AGENT_MAP = {
        'observer_agent': 'Observer',
        'reasoner_agent': 'Reasoner',
        # [Claude: map tool names to display labels]
    }

    def __init__(self, connection_id: str, ws_endpoint: str):
        self.connection_id = connection_id
        self.ws_endpoint = ws_endpoint
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
                'type': 'progress',
                'agent': self.TOOL_AGENT_MAP.get(tool_name, 'Agent'),
                'detail': f'Calling {tool_name}...',
                'step': self._step_count,
            })

    def _post(self, data: dict):
        try:
            self._apigw.post_to_connection(
                ConnectionId=self.connection_id,
                Data=json.dumps(data).encode(),
            )
        except Exception:
            pass

    def send_custom_step(self, agent: str, phase: str, detail: str):
        self._step_count += 1
        self._post({'type': 'progress', 'agent': agent, 'phase': phase, 'detail': detail})
```

---

## 3-Layer RBAC Steering Hooks — Pass 3 Reference

```python
"""3-layer RBAC: agent access → tool access → data filtering."""

class AfieSteeringHooks:
    def __init__(self, client_id: str, persona: str = 'user', rbac_policy: dict = None):
        self.client_id = client_id
        self.persona = persona
        self.rbac = rbac_policy or {}

    def check_agent_access(self, agent_name: str) -> bool:
        access = self.rbac.get('agent_access', {})
        if access.get(agent_name) is False:
            raise PermissionError(f"'{self.persona}' not authorized for {agent_name}")
        return True

    def before_tool_call(self, tool_name: str, tool_input: dict) -> dict:
        # Layer 2: Tool whitelist/blacklist
        tool_access = self.rbac.get('tool_access', {})
        denied = tool_access.get('denied', [])
        if tool_name in denied:
            return {'__rbac_denied': True, 'error': f'Tool {tool_name} denied for {self.persona}'}
        # Layer 3: Inject SQL filters
        sql_filter = self.rbac.get('data_filter', {}).get('sql_filter', '')
        if sql_filter and 'sql' in tool_input:
            tool_input['sql'] += f' {sql_filter}'
        return tool_input

    def after_tool_call(self, tool_name: str, output: str) -> str:
        # Layer 3: Mask restricted fields
        import re
        for field in self.rbac.get('data_filter', {}).get('mask_fields', []):
            output = re.sub(rf'"{field}"\s*:\s*"[^"]*"', f'"{field}": "[RESTRICTED]"', output)
        return output
```
