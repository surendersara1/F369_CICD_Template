# SOP — Strands Multi-Agent (Parallel, Sub-Agent, Graph / Swarm, A2A)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · AgentCore Runtime (sub-agent hosting) · A2A protocol · Python 3.12+ · `concurrent.futures`

---

## 1. Purpose

- Codify the four multi-agent patterns used in production:
  1. **AgentCore Runtime sub-agents** — each agent is its own container; orchestrator calls via `invoke_agent_runtime`.
  2. **Sub-agents wrapped as `@tool`** — LLM decides when to call each sub-agent.
  3. **In-process agents-as-tools** — simpler, all agents share a Python process.
  4. **Hybrid** — parallel pre-fetch + LLM synthesis (the production default).
- Codify shared state across agents via `invocation_state`.
- Codify cross-platform A2A invocation (`A2AAgent`).
- Include when the SOW mentions multi-agent systems, supervisor/worker topology, parallel agent execution, or agent orchestration.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** Multi-agent topology is a framework choice, not a CDK-stack choice. §3 is the single canonical variant.
>
> Hosting topology (when sub-agents run as their own AgentCore Runtime vs. as in-process tools) is a **framework decision** captured in §5 Swap matrix. The CDK resources for each sub-agent's runtime are defined in `AGENTCORE_RUNTIME` and deployed per the rules in `STRANDS_DEPLOY_ECS` / `STRANDS_DEPLOY_LAMBDA`.

§4 Micro-Stack Variant is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Production topology

```
Production Pattern (AgentCore Runtime sub-agents):
  Supervisor Agent (its own AgentCore Runtime)
    ├── invoke_agent_runtime(OBSERVER_ARN)   ← parallel via ThreadPoolExecutor
    ├── invoke_agent_runtime(REASONER_ARN)   ← parallel via ThreadPoolExecutor
    └── invoke_agent_runtime(GOVERNANCE_ARN) ← sequential or parallel
  Supervisor synthesizes all results via Strands Agent.

  Key property: Sub-agents are SEPARATE AgentCore Runtimes. Communication is
  via invoke_agent_runtime() (SigV4, HTTP), NOT direct Python function calls.

In-process alternative (single container):
  Supervisor Agent
    ├── research_agent (in-process, passed as tool)
    ├── code_agent    (in-process, passed as tool)
    └── via Agent.as_tool() or direct tools=[...] list

Pattern selection:
  - AgentCore Runtime sub-agents : each scales independently, microVM isolation
  - In-process agents            : simpler, one deploy, no isolation
```

### 3.2 Pattern 1 — AgentCore Runtime sub-agent invocation

```python
"""Sub-agent invocation via AgentCore Runtime API. Parallel fan-out."""
import json, uuid
from concurrent.futures import ThreadPoolExecutor
import boto3

agentcore_client = boto3.client('bedrock-agentcore')


def _call_sub_agent(runtime_arn: str, payload: dict) -> str:
    """Invoke a sub-agent on its own AgentCore Runtime."""
    resp = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        contentType='application/json',
        payload=json.dumps(payload).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=str(uuid.uuid4()),
    )
    return resp.get('response').read().decode('utf-8')


# 2-way parallel (Observer + Reasoner)
def _parallel_observe_and_reason(query: str) -> tuple[str, str]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        obs = pool.submit(_call_sub_agent, OBSERVER_ARN, {'prompt': query})
        rea = pool.submit(_call_sub_agent, REASONER_ARN, {'prompt': query})
        return obs.result(timeout=120), rea.result(timeout=120)


# 3-way parallel (+ Governance). ~30 % faster p95 vs sequential.
def _parallel_all_agents(query: str) -> tuple[str, str, str]:
    with ThreadPoolExecutor(max_workers=3) as pool:
        obs = pool.submit(_call_sub_agent, OBSERVER_ARN,   {'prompt': query})
        rea = pool.submit(_call_sub_agent, REASONER_ARN,   {'prompt': query})
        gov = pool.submit(_call_sub_agent, GOVERNANCE_ARN,
                          {'prompt': f"Pre-check compliance for: {query}"})
        return obs.result(timeout=120), rea.result(timeout=120), gov.result(timeout=120)
```

### 3.3 Pattern 2 — sub-agents wrapped as `@tool`

Use when the supervisor's LLM should decide which sub-agent(s) to call (as opposed to the deterministic fan-out in §3.2).

```python
"""Wrap sub-agent invocations as @tool — LLM-driven routing."""
from strands import Agent, tool


@tool
def observer_agent(query: str) -> str:
    """Retrieve data from the data warehouse.

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
        query:            The analysis question.
        observation_data: Raw data from observer to analyze.
    Returns:
        Causal analysis with root causes.
    """
    prompt = query
    if observation_data:
        prompt += f"\n\nObservation data: {observation_data[:3000]}"
    return _call_sub_agent(REASONER_ARN, {'prompt': prompt})


# Supervisor — LLM picks which tool(s) to invoke
supervisor = Agent(
    model=model,
    tools=[observer_agent, reasoner_agent, governance_agent],
    system_prompt=SYSTEM_PROMPT,
)
```

### 3.4 Pattern 3 — in-process agents-as-tools

All sub-agents share the supervisor's Python process. Simpler, one deploy, no microVM isolation.

```python
"""In-process agents — all run in the same container/Lambda."""
from strands import Agent

research_agent = Agent(
    system_prompt="You are a research specialist.",
    tools=[search_knowledge_base],
)

code_agent = Agent(
    system_prompt="You are a code specialist.",
    tools=[save_artifact],
)

# Option A: direct tools=[agent1, agent2] — agents auto-convert to callable tools
orchestrator = Agent(
    system_prompt="You route queries to specialists.",
    tools=[research_agent, code_agent],
)

# Option B: .as_tool(name, description) for a custom name / preserved context
orchestrator = Agent(
    tools=[
        research_agent.as_tool(name="researcher", description="Research questions"),
        code_agent.as_tool(name="coder", description="Coding tasks", preserve_context=True),
    ],
)
```

### 3.5 Pattern 4 — hybrid (parallel pre-fetch + LLM synthesis)

**This is the production default.** Pre-fetch in parallel (deterministic, no LLM cost), then let the synthesizer LLM write the brief with all data already in context.

```python
"""Hybrid: deterministic parallel fetch, then LLM synthesis."""

@app.entrypoint
def invoke(payload):
    query = payload.get('prompt', '')

    # Phase 1 — parallel data fetch (no LLM needed). Fixed concurrency.
    observation, reasoning = _parallel_observe_and_reason(query)

    # Phase 2 — LLM synthesis. Only the tools needed for synthesis.
    synthesizer = Agent(
        model=model,
        tools=[run_financial_analysis, governance_agent],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=ws_callback,
        session_manager=sess_mgr,
    )

    synthesis_prompt = (
        f"Query: {query}\n\n"
        f"=== Observer Data ===\n{observation[:4000]}\n\n"
        f"=== Reasoner Analysis ===\n{reasoning[:4000]}\n\n"
        f"Synthesize a response. Use run_financial_analysis for charts if needed."
    )
    result = synthesizer(synthesis_prompt)
    return {"result": str(result)}
```

### 3.6 Shared state across agents

```python
"""Pass shared state to every agent + tool in one call."""
response = orchestrator(
    "Research and implement a caching solution",
    invocation_state={
        "tenant_id":        "tenant-123",
        "user_preferences": {"language": "python"},
    },
)

# Access inside any @tool — kwargs carries invocation_state
@tool
def my_tool(query: str, **kwargs) -> str:
    """Tool that reads shared invocation state.

    Args:
        query: The tool query.
    Returns:
        Result string.
    """
    state = kwargs.get("invocation_state", {})
    tenant_id = state.get("tenant_id")
    # ... use tenant_id
    return "ok"
```

### 3.7 A2A protocol (remote agents)

```python
"""Agent-to-Agent protocol for cross-platform agent communication."""
from strands.agent.a2a_agent import A2AAgent

remote_agent = A2AAgent(
    url="https://remote-agent.example.com/a2a",
    name="remote_specialist",
    description="Remote specialist for domain-specific tasks",
)

orchestrator = Agent(tools=[local_agent, remote_agent])
```

See `AGENTCORE_A2A` for the server-side A2A skill definition, authentication (mTLS / SigV4 / API-key), and discovery endpoint requirements.

### 3.8 Gotchas

- **`ThreadPoolExecutor.result(timeout=…)` surfaces only the first exception** and cancels pending futures only on interpreter exit. If Reasoner hangs while Observer succeeds, you get Observer + a `TimeoutError` — handle both.
- **`runtimeSessionId=uuid.uuid4()` per call** throws away the 8-hour AgentCore session. Reuse the *same* session ID across sub-agent calls in one user turn to share the microVM's warm context and memory.
- **Sub-agent in-process + `ThreadPoolExecutor`** — `Agent(...)` objects hold state (session, model client). Don't share one `Agent` across threads; build inside the thread or use a lock.
- **`Agent.as_tool(preserve_context=True)`** makes the sub-agent keep its own conversation history across supervisor turns. Memory cost compounds — reset between user turns.
- **3-way parallel dips diminishing returns** past ≈3 sub-agents in production; the Bedrock model call dominates and becomes the long pole. Measure with X-Ray segments before fanning out wider.
- **`invoke_agent_runtime` is SigV4-signed** — the supervisor's IAM role must hold `bedrock-agentcore:InvokeAgentRuntime` on each sub-agent's `agentRuntimeArn` (grant in `STRANDS_DEPLOY_ECS §4` / `AGENTCORE_RUNTIME §4`).
- **`A2AAgent(url=…)` does not verify cert pinning by default.** For production, use mTLS or wrap in `requests` with a CA bundle.

---

## 5. Swap matrix — topology variants

| Need | Swap |
|---|---|
| Simpler deploy, one container | Pattern 3 (in-process agents-as-tools) |
| Sub-agents scale / fail independently | Pattern 1 (AgentCore Runtime sub-agents) |
| LLM should decide sub-agent routing | Pattern 2 (sub-agents as `@tool`) |
| Minimize LLM cost on orchestration | Pattern 4 (hybrid — deterministic fan-out + single synthesis LLM) |
| Cross-org sub-agent (different AWS account / platform) | Pattern 1 + A2A via §3.7 |
| Streaming synthesis to the UI | Keep `callback_handler` on synthesizer only (not sub-agents) — avoid event storm |
| Fair-share compute across sub-agents | Pattern 1 + AgentCore Runtime auto-scaling; tune concurrent session limit per agent |

---

## 6. Worked example — parallel fan-out + synthesis

Save as `tests/sop/test_STRANDS_MULTI_AGENT.py`. Offline; mocks `boto3.client`.

```python
"""SOP verification — parallel fan-out returns both results within budget."""
import time
from unittest.mock import patch, MagicMock


def _fake_response(text: str):
    body = MagicMock()
    body.read.return_value = text.encode('utf-8')
    return {"response": body}


def test_parallel_fanout_completes_in_parallel():
    with patch('boto3.client') as mock_cli:
        client = MagicMock()
        mock_cli.return_value = client

        def slow(**_):
            time.sleep(0.1)
            return _fake_response("ok")

        client.invoke_agent_runtime.side_effect = slow

        from shared.multi_agent import _parallel_observe_and_reason
        t0 = time.time()
        obs, rea = _parallel_observe_and_reason("test query")
        elapsed = time.time() - t0

    assert obs == "ok" and rea == "ok"
    # Two 0.1 s calls must finish in well under 0.2 s if truly parallel
    assert elapsed < 0.15


def test_in_process_agents_as_tools_composes():
    from strands import Agent
    from strands.models import BedrockModel
    model = BedrockModel(model_id="anthropic.claude-haiku-4-5-20251001-v1:0")

    with patch('boto3.client'):
        researcher = Agent(model=model, system_prompt="research")
        orchestrator = Agent(
            model=model,
            tools=[researcher.as_tool(name="researcher", description="research questions")],
            system_prompt="route",
        )
    assert orchestrator is not None
```

---

## 7. References

- `docs/template_params.md` — `OBSERVER_ARN`, `REASONER_ARN`, `GOVERNANCE_ARN`, `MULTI_AGENT_MAX_WORKERS`, `SUB_AGENT_TIMEOUT_SECONDS`
- `docs/Feature_Roadmap.md` — feature IDs `STR-07` (multi-agent), `STR-08` (A2A), `A-19` (supervisor synthesis)
- AgentCore Runtime API: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-core_InvokeAgentRuntime.html
- A2A protocol: https://github.com/google-a2a/a2a-protocol (upstream spec)
- Related SOPs: `STRANDS_AGENT_CORE` (supervisor that uses these patterns), `STRANDS_TOOLS` (`@tool` semantics for wrapped sub-agents), `AGENTCORE_RUNTIME` (where each sub-agent is hosted), `AGENTCORE_A2A` (server-side A2A), `STRANDS_DEPLOY_ECS` / `STRANDS_DEPLOY_LAMBDA` (grants for `InvokeAgentRuntime`)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added Gotchas (§3.8) covering session-ID reuse, thread-safety of `Agent`, and IAM grants on `InvokeAgentRuntime`. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — four multi-agent patterns (runtime sub-agents, `@tool` wrap, in-process, hybrid), shared state, A2A. |
