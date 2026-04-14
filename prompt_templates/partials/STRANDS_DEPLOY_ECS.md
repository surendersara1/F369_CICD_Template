# PARTIAL: Strands Deploy to ECS Fargate — Long-Running & Streaming Agents

**Usage:** Include when SOW mentions long-running agents, streaming responses, multi-turn sessions >15min, ECS Fargate agent hosting, or containerized agents.

---

## ECS Fargate Deployment Overview

```
ECS Fargate Agent = Containerized agent for long-running/streaming workloads:
  - No timeout limit (unlike Lambda's 15 min)
  - Response streaming via HTTP/WebSocket
  - Multi-turn sessions with persistent connections
  - Auto-scaling based on CPU/memory or request count
  - Health check endpoint for ALB integration
```

---

## CDK Code Block — ECS Fargate Agent Host

```python
def _create_strands_agent_ecs(self, stage_name: str) -> None:
    """
    Strands agent ECS Fargate service for long-running/streaming agents.

    Components:
      A) ECS Cluster
      B) Fargate Task Definition with agent container
      C) Fargate Service with auto-scaling

    [Claude: include when SOW mentions long-running agents, streaming,
     or multi-turn sessions exceeding 15 minutes.]
    """

    # =========================================================================
    # A) ECS CLUSTER
    # =========================================================================

    self.ecs_cluster = ecs.Cluster(
        self, "AgentCluster",
        cluster_name=f"{{project_name}}-agents-{stage_name}",
        vpc=self.vpc,
        container_insights=True,
    )

    # =========================================================================
    # B) FARGATE TASK DEFINITION
    # =========================================================================

    self.strands_agent_task_def = ecs.FargateTaskDefinition(
        self, "StrandsAgentTaskDef",
        family=f"{{project_name}}-strands-agent-{stage_name}",
        cpu=1024,
        memory_limit_mib=2048,
        task_role=self.strands_agent_role,
    )

    self.strands_agent_task_def.add_container(
        "AgentContainer",
        container_name="strands-agent",
        image=ecs.ContainerImage.from_asset("src/strands_agent_ecs"),
        environment={
            "STAGE": stage_name,
            "DEFAULT_MODEL_ID": "anthropic.claude-sonnet-4-20250514-v1:0",
            "SESSION_TABLE": self.agent_session_table.table_name,
            "AGENT_ARTIFACTS_BUCKET": self.agent_artifacts_bucket.bucket_name,
        },
        logging=ecs.LogDrivers.aws_logs(
            stream_prefix="strands-agent",
            log_retention=logs.RetentionDays.ONE_MONTH,
        ),
        port_mappings=[ecs.PortMapping(container_port=8080)],
        health_check=ecs.HealthCheck(
            command=["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"],
            interval=Duration.seconds(30),
            timeout=Duration.seconds(5),
            retries=3,
        ),
    )

    # =========================================================================
    # C) FARGATE SERVICE
    # =========================================================================

    self.strands_agent_service = ecs.FargateService(
        self, "StrandsAgentService",
        service_name=f"{{project_name}}-strands-agent-{stage_name}",
        cluster=self.ecs_cluster,
        task_definition=self.strands_agent_task_def,
        desired_count=1 if stage_name == "dev" else 2,
        assign_public_ip=False,
        security_groups=[self.ecs_sg],
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
    )

    # Auto-scaling
    scaling = self.strands_agent_service.auto_scale_task_count(
        min_capacity=1 if stage_name == "dev" else 2,
        max_capacity=5 if stage_name == "dev" else 20,
    )
    scaling.scale_on_cpu_utilization("CpuScaling", target_utilization_percent=70)
```

---

## Dockerfile — Pass 3 Reference

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "server.py"]
```

---

## FastAPI Server — Pass 3 Reference

```python
"""ECS agent server with streaming support."""
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from strands import Agent
from strands.models import BedrockModel
import os, json, asyncio

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/agent/invoke")
async def invoke(request: dict):
    agent = Agent(
        model=BedrockModel(model_id=os.environ["DEFAULT_MODEL_ID"]),
        system_prompt="You are helpful.",
        tools=[],
    )
    response = agent(request.get("message", ""))
    return {"response": str(response)}

@app.post("/agent/stream")
async def stream(request: dict):
    """Streaming agent response via SSE."""
    async def generate():
        agent = Agent(
            model=BedrockModel(model_id=os.environ["DEFAULT_MODEL_ID"]),
            system_prompt="You are helpful.",
        )
        # [Claude: implement streaming callback for token-level streaming]
        response = agent(request.get("message", ""))
        yield f"data: {json.dumps({'content': str(response)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```
