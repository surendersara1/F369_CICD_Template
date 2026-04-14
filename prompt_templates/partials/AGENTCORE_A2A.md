# PARTIAL: AgentCore A2A — Agent-to-Agent Protocol

**Usage:** Include when SOW mentions A2A protocol, cross-platform agent communication, remote agents, or inter-agent messaging.

---

## A2A Protocol Overview

```
A2A = Open protocol for cross-platform agent communication:
  - Strands agents can call remote agents on any platform
  - Remote agents wrapped as tools via A2AAgent class
  - Supports LangChain, LangGraph, CrewAI remote agents
  - HTTP-based communication with structured messages
  - AgentCore Runtime supports A2A natively

A2A Flow:
  Local Strands Agent → A2AAgent(url) → HTTP POST → Remote Agent
       ↓                                                ↓
  Tool result returned                          Any framework/platform
```

---

## A2A Client — Pass 3 Reference

```python
"""Use remote A2A agents as tools in Strands."""
from strands import Agent
from strands.agent.a2a_agent import A2AAgent

# Wrap remote A2A-compatible agent as a tool
remote_specialist = A2AAgent(
    url="https://remote-agent.example.com/a2a",
    name="domain_specialist",
    description="Remote specialist for domain-specific analysis",
)

# Use alongside local agents
orchestrator = Agent(
    system_prompt="""You coordinate local and remote agents.
Use domain_specialist for specialized analysis.""",
    tools=[local_research_agent, remote_specialist],
)

response = orchestrator("Analyze the Q4 financial data")
```

---

## A2A Server (expose Strands agent as A2A endpoint)

```python
"""Expose a Strands agent as an A2A-compatible server."""
# AgentCore Runtime automatically exposes agents via A2A protocol
# when deployed with `agentcore launch`.
# No additional code needed — the runtime handles A2A routing.

# For custom A2A server (non-AgentCore):
from strands import Agent
from fastapi import FastAPI

app = FastAPI()
agent = Agent(system_prompt="You are a specialist.", tools=[...])

@app.post("/a2a")
async def a2a_endpoint(request: dict):
    """A2A-compatible endpoint."""
    message = request.get("message", "")
    response = agent(message)
    return {"response": str(response), "status": "completed"}
```
