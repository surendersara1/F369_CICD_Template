# PARTIAL: Strands Deploy to AgentCore Runtime — Containerized Agents

**Usage:** Include when SOW mentions AgentCore Runtime deployment, containerized agents, long-running agents, or per-agent Docker containers.

---

## Deployment Pattern (from real production)

```
Production deploys agents as Docker containers on AgentCore Runtime:
  - Each agent = Dockerfile + agent.py + shared/ utilities
  - AgentCore Runtime manages scaling, isolation, session persistence
  - ARM64 Graviton containers for cost optimization
  - VPC connectivity for private data sources
  - 8-hour session persistence window

NOT ECS Fargate — AgentCore Runtime replaces ECS for agent workloads.
ECS is only used for non-agent workloads (PDF generation, ETL, etc.)
```

---

## Agent Dockerfile — Pass 3 Reference

```dockerfile
# agents/observer/Dockerfile
FROM python:3.13-slim
WORKDIR /app

# Copy shared utilities first (cached layer)
COPY shared/ ./shared/
COPY evaluations/ ./evaluations/

# Copy agent-specific code
COPY observer/ ./observer/
COPY observer/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "observer.agent"]
```

---

## Agent Entry Point (BedrockAgentCoreApp) — Pass 3 Reference

```python
"""Agent entry point for AgentCore Runtime deployment."""
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload):
    query = payload.get('prompt', '')
    model = BedrockModel(model_id=os.environ.get('DEFAULT_MODEL_ID'))
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[...])
    result = agent(query)
    return {"result": str(result)}

if __name__ == "__main__":
    app.run()
```

---

## CDK: AgentRuntime Construct — Pass 2A Reference

```typescript
// Uses the reusable AgentRuntime construct (see AGENTCORE_RUNTIME.md)
import { AgentRuntime } from '../../constructs/agent-runtime';

new AgentRuntime(this, 'Observer', {
  agentName: 'observer',
  runtimeName: 'observer_agent_v3',
  ssmOutputPath: '/{{project_name}}/agents/observer_agent_arn',
  environmentVariables: {
    GATEWAY_URL: ssmLookup(this, '/{{project_name}}/mcp/gateway_endpoint'),
  },
  additionalPolicies: [
    new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeGateway'],
      resources: ['*'],
    }),
  ],
});
```

---

## requirements.txt (per agent)

```
strands-agents>=0.1.0
strands-agents-tools>=0.1.0
bedrock-agentcore>=0.1.0
boto3>=1.35.0
```

---

## When to Use AgentCore Runtime vs ECS Fargate

| Criteria | AgentCore Runtime | ECS Fargate |
|----------|-------------------|-------------|
| Agent workloads | ✅ Purpose-built | Overkill |
| Session isolation | ✅ microVM per session | Shared container |
| Auto-scaling | ✅ Managed | Manual ASG config |
| MCP server hosting | ✅ ProtocolType.MCP | Not supported |
| Non-agent workloads | ❌ | ✅ PDF gen, ETL, batch |
| Custom networking | Limited | Full control |
