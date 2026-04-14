# PARTIAL: Strands Multi-Agent — Agents-as-Tools, Graph, Swarm, Workflow, A2A

**Usage:** Include when SOW mentions multi-agent systems, supervisor/worker agents, agent orchestration, Graph/Swarm/Workflow patterns, or Agent-to-Agent (A2A) protocol.

---

## Multi-Agent Patterns Overview

```
Strands Multi-Agent Patterns:
  1. Agents-as-Tools: Supervisor delegates to worker agents via tool calls
  2. Graph: DAG-based execution with defined node dependencies
  3. Swarm: Dynamic agent handoffs based on model decisions
  4. Workflow: Sequential/parallel pipeline with explicit control flow
  5. A2A Protocol: Cross-platform agent communication (remote agents)

Pattern Selection:
  - Graph:    Execution path defined by developer (DAG edges)
  - Swarm:    Execution path decided by model at runtime (handoffs)
  - Workflow: Execution path is sequential/parallel pipeline (explicit)
```

---

## Agents-as-Tools (Simplest Pattern) — Pass 3 Reference

### Direct Agent Passing (auto-converted to tools)

```python
"""Agents-as-Tools — pass agents directly in tools array."""
from strands import Agent
from strands.models import BedrockModel

# Specialized worker agents
research_agent = Agent(
    model=BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0"),
    system_prompt="You are a research specialist. Search and synthesize information.",
    tools=[search_knowledge_base],
)

code_agent = Agent(
    model=BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0"),
    system_prompt="You are a code specialist. Write, review, and debug code.",
    tools=[save_artifact],
)

# Orchestrator — agents auto-converted to callable tools
orchestrator = Agent(
    model=BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0"),
    system_prompt="""You route queries to specialists:
- research_agent: for research questions
- code_agent: for coding tasks
Answer simple questions directly.""",
    tools=[research_agent, code_agent],
)

response = orchestrator("Research the latest AWS Lambda features")
```

### Customized Agent Tools (.as_tool())

```python
"""Customize agent tool name, description, and context behavior."""
orchestrator = Agent(
    system_prompt="You route queries to specialists.",
    tools=[
        research_agent.as_tool(
            name="research_assistant",
            description="Process research queries requiring factual information.",
        ),
        code_agent.as_tool(
            name="code_assistant",
            description="Handle coding tasks and code review.",
            preserve_context=True,  # Remember prior interactions
        ),
    ],
)
```

### Custom @tool Wrapper (full control)

```python
"""Full control over agent invocation with @tool decorator."""
from strands import Agent, tool

@tool
def ask_research_agent(question: str) -> str:
    """Delegate a research question to the research specialist.

    Args:
        question: The research question to investigate.

    Returns:
        Research findings with citations.
    """
    agent = Agent(
        system_prompt="You are a research specialist.",
        tools=[search_knowledge_base],
    )
    return str(agent(question))

supervisor = Agent(
    system_prompt="You coordinate specialist agents.",
    tools=[ask_research_agent],
)
```

---

## Shared State Across Agents

```python
"""Pass shared state to all agents via invocation_state."""
orchestrator = Agent(
    system_prompt="You coordinate agents.",
    tools=[research_agent, code_agent],
)

# Shared state accessible by all agents and tools
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
    """Tool with access to shared state."""
    state = kwargs.get("invocation_state", {})
    tenant_id = state.get("tenant_id")
    # ... scoped to tenant
```

---

## A2A Protocol (Remote Agents)

```python
"""Agent-to-Agent protocol for cross-platform communication."""
from strands.agent.a2a_agent import A2AAgent

# Wrap a remote A2A-compatible agent as a tool
remote_agent = A2AAgent(
    url="https://remote-agent.example.com/a2a",
    name="remote_specialist",
    description="Remote specialist agent for domain-specific tasks",
)

orchestrator = Agent(
    system_prompt="You coordinate local and remote agents.",
    tools=[research_agent, remote_agent],
)
```
