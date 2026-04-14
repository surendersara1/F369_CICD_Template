# PARTIAL: Strands Multi-Agent — Parallel Execution, Sub-Agent Invocation, Graph/Swarm

**Usage:** Include when SOW mentions multi-agent systems, supervisor/worker agents, parallel agent execution, or agent orchestration.

---

## Multi-Agent Patterns (from real production)

```
Production Pattern (AgentCore Runtime):
  Supervisor Agent (on AgentCore Runtime)
    ├── invoke_agent_runtime(OBSERVER_ARN)  ← parallel via ThreadPoolExecutor
    ├── invoke_agent_runtime(REASONER_ARN)  ← parallel via ThreadPoolExecutor
    └── invoke_agent_runtime(GOVERNANCE_ARN) ← sequential or parallel
  Supervisor synthesizes all results via Strands Agent

  Key: Sub-agents are separate AgentCore Runtimes, NOT in-process agents.
       Communication is via invoke_agent_runtime() API, not direct function calls.

Local Pattern (single process):
  Supervisor Agent
    ├── research_agent (in-process, passed as tool)
    ├── code_agent (in-process, passed as tool)
    └── Agent.as_tool() or @tool wrapper

Pattern Selection:
  - AgentCore Runtime: Each agent = separate container, scales independently
  - In-process: All agents in one process, simpler but no isolation
```

---

## Pattern 1: AgentCore Runtime Sub-Agent Invocation (Production)

```python
"""Sub-agent invocation via AgentCore Runtime API."""
import json, uuid
from concurrent.futures import ThreadPoolExecutor
import boto3

agentcore_client = boto3.client('bedrock-agentcore')

def _call_sub_agent(runtime_arn: str, payload: dict) -> str:
    """Invoke sub-agent on its own AgentCore Runtime."""
    resp = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        contentType='application/json',
        payload=json.dumps(payload).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=str(uuid.uuid4()),
    )
    return resp.get('response').read().decode('utf-8')


# 2-way parallel: Observer + Reasoner
def _parallel_observe_and_reason(query: str) -> tuple[str, str]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        obs = pool.submit(_call_sub_agent, OBSERVER_ARN, {'prompt': query})
        rea = pool.submit(_call_sub_agent, REASONER_ARN, {'prompt': query})
        return obs.result(timeout=120), rea.result(timeout=120)


# 3-way parallel: Observer + Reasoner + Governance (~30% faster)
def _parallel_all_agents(query: str) -> tuple[str, str, str]:
    with ThreadPoolExecutor(max_workers=3) as pool:
        obs = pool.submit(_call_sub_agent, OBSERVER_ARN, {'prompt': query})
        rea = pool.submit(_call_sub_agent, REASONER_ARN, {'prompt': query})
        gov = pool.submit(_call_sub_agent, GOVERNANCE_ARN,
                          {'prompt': f"Pre-check compliance for: {query}"})
        return obs.result(timeout=120), rea.result(timeout=120), gov.result(timeout=120)
```

---

## Pattern 2: Sub-Agents as @tool (for LLM-driven routing)

```python
"""Wrap sub-agent calls as @tool for LLM-driven orchestration."""
from strands import Agent, tool

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
        Causal analysis with root causes.
    """
    prompt = query
    if observation_data:
        prompt += f"\n\nObservation data: {observation_data[:3000]}"
    return _call_sub_agent(REASONER_ARN, {'prompt': prompt})

# Supervisor uses these as tools — LLM decides when to call each
supervisor = Agent(
    model=model,
    tools=[observer_agent, reasoner_agent, governance_agent],
    system_prompt=SYSTEM_PROMPT,
)
```

---

## Pattern 3: In-Process Agents-as-Tools (Simpler, No AgentCore)

```python
"""In-process agents — all run in same container/Lambda."""
from strands import Agent

research_agent = Agent(
    system_prompt="You are a research specialist.",
    tools=[search_knowledge_base],
)

code_agent = Agent(
    system_prompt="You are a code specialist.",
    tools=[save_artifact],
)

# Direct passing — agents auto-converted to callable tools
orchestrator = Agent(
    system_prompt="You route queries to specialists.",
    tools=[research_agent, code_agent],
)

# Or with .as_tool() for custom name/description
orchestrator = Agent(
    tools=[
        research_agent.as_tool(name="researcher", description="Research questions"),
        code_agent.as_tool(name="coder", description="Coding tasks", preserve_context=True),
    ],
)
```

---

## Pattern 4: Hybrid — Parallel Pre-fetch + LLM Synthesis

The real production pattern: pre-fetch data in parallel, then let LLM synthesize.

```python
"""Hybrid: parallel data fetch + LLM synthesis."""

@app.entrypoint
def invoke(payload):
    query = payload.get('prompt', '')

    # Phase 1: Parallel data fetch (no LLM needed)
    observation, reasoning = _parallel_observe_and_reason(query)

    # Phase 2: LLM synthesis with all data available
    synthesizer = Agent(
        model=model,
        tools=[run_financial_analysis, governance_agent],  # Only tools needed for synthesis
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

---

## Shared State Across Agents

```python
"""Pass shared state to all agents via invocation_state."""
response = orchestrator(
    "Research and implement a caching solution",
    invocation_state={
        "tenant_id": "tenant-123",
        "user_preferences": {"language": "python"},
    },
)

# Access in tools:
@tool
def my_tool(query: str, **kwargs) -> str:
    state = kwargs.get("invocation_state", {})
    tenant_id = state.get("tenant_id")
```

---

## A2A Protocol (Remote Agents)

```python
"""Agent-to-Agent protocol for cross-platform communication."""
from strands.agent.a2a_agent import A2AAgent

remote_agent = A2AAgent(
    url="https://remote-agent.example.com/a2a",
    name="remote_specialist",
    description="Remote specialist for domain-specific tasks",
)

orchestrator = Agent(tools=[local_agent, remote_agent])
```
