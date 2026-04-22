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
# Build context is agents/ (parent directory) — set in CDK AgentRuntimeArtifact.fromAsset()
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /var/task

# Install dependencies first (cached layer)
COPY observer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared utilities (used by all agents)
COPY shared/ ./shared/
COPY evaluations/ ./evaluations/

# Copy agent-specific code
COPY observer/agent.py .

EXPOSE 8080
ENTRYPOINT ["python", "agent.py"]
```

---

## MCP-Enabled Agent Dockerfile (agent that connects to Gateway)

```dockerfile
# agents/observer/Dockerfile — MCP-enabled (connects to Gateway via SigV4)
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /var/task

COPY observer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY evaluations/ ./evaluations/
COPY observer/agent.py .

EXPOSE 8080
ENTRYPOINT ["python", "agent.py"]
```

---

## MCP Server Dockerfile (tool server, not agent)

```dockerfile
# infra/containers/redshift-mcp/Dockerfile — MCP server on AgentCore Runtime
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app

RUN pip install --no-cache-dir \
    "mcp[cli]>=1.8.0" \
    "psycopg2-binary==2.9.9" \
    "boto3>=1.35.0"

COPY server.py .

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
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

## requirements.txt (per agent — MCP-enabled)

```
strands-agents==1.34.0
strands-agents-tools>=0.1.0
bedrock-agentcore==1.6.0
boto3>=1.34.0
mcp[cli]>=1.8.0
httpx>=0.27.0
```

## requirements.txt (MCP server — tool server only)

```
mcp[cli]>=1.8.0
boto3>=1.35.0
# Add data source driver: psycopg2-binary, opensearch-py, etc.
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
