# PARTIAL: AgentCore A2A — Agent-to-Agent Protocol Server & Client

**Usage:** Include when SOW mentions A2A protocol, cross-platform agent communication, exposing agents to external systems, or inter-agent messaging.

---

## A2A Architecture (from real production)

```
A2A = Open protocol for cross-platform agent communication:
  - Expose Strands agents as A2A servers (any external agent can call them)
  - Consume remote A2A agents as tools in Strands orchestrators
  - Uses StrandsA2AExecutor + A2AStarletteApplication
  - Deployed as AgentCore Runtime or standalone container

Use Case: External procurement agent (LangGraph) calls AFIE Governance
          via A2A to check Cedar compliance for a SAR 3M PO.
```

---

## A2A Server — Pass 3 Reference

```python
"""A2A Server — expose a Strands agent via A2A protocol."""
import os
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import StrandsA2AExecutor

@tool
def check_compliance(action: str, amount: float = 0) -> str:
    """Check action against policy rules.
    Args:
        action: Description of the action to check.
        amount: Transaction amount.
    Returns:
        Compliance verdict with approval tier.
    """
    import json
    tier = 'AUTO' if amount <= 1_000_000 else 'VP_FINANCE' if amount <= 5_000_000 else 'CFO_BOARD'
    return json.dumps({'status': 'COMPLIANT', 'approval_tier': tier, 'amount': amount})

# Build the agent
model = BedrockModel(model_id='us.anthropic.claude-haiku-4-5-20251001-v1:0')
agent = Agent(
    model=model,
    tools=[check_compliance],
    system_prompt="You evaluate compliance requests against Cedar policy rules.",
    name="governance-a2a",
    description="Governance compliance checker — validates actions against policies",
)

def main():
    from a2a.server.apps.starlette import A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler

    executor = StrandsA2AExecutor(agent=agent)
    request_handler = DefaultRequestHandler(agent_executor=executor)
    a2a_app = A2AStarletteApplication(
        agent_card=executor.get_agent_card(host='0.0.0.0', port=9100),
        http_handler=request_handler,
    )

    import uvicorn
    uvicorn.run(a2a_app.build(), host='0.0.0.0', port=9100)

if __name__ == "__main__":
    main()
```

---

## A2A Client — Pass 3 Reference

```python
"""Consume a remote A2A agent as a tool in Strands."""
from strands import Agent
from strands.agent.a2a_agent import A2AAgent

# Wrap remote A2A server as a tool
remote_governance = A2AAgent(
    endpoint="http://governance-a2a:9100",
    name="governance_checker",
    description="Remote governance agent — Cedar compliance + SOP validation",
)

# Use alongside local agents
orchestrator = Agent(
    system_prompt="You coordinate local and remote agents.",
    tools=[local_observer, remote_governance],
)
```

---

## A2A Dockerfile — Pass 3 Reference

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY shared/ ./shared/
COPY a2a_server/ ./a2a_server/
COPY a2a_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 9100
CMD ["python", "-m", "a2a_server.server"]
```

---

## requirements.txt

```
strands-agents>=0.1.0
a2a-sdk>=0.1.0
uvicorn>=0.30.0
starlette>=0.38.0
boto3>=1.35.0
```
