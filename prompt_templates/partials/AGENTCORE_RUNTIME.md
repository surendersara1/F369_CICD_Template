# PARTIAL: AgentCore Runtime — Managed Serverless Agent Hosting

**Usage:** Include when SOW mentions AgentCore Runtime, managed agent hosting, serverless agent deployment, microVM isolation, or auto-scaling agent sessions.

---

## AgentCore Runtime Overview

```
AgentCore Runtime = AWS-managed serverless runtime for AI agents:
  - Dedicated microVM per user session (complete isolation)
  - 8-hour session persistence window
  - Auto-scales to thousands of sessions in seconds
  - Supports Strands, LangChain, LangGraph, CrewAI
  - Supports MCP and A2A protocols
  - Any model from any provider (Bedrock, OpenAI, Gemini, etc.)
  - Pay only for actual compute usage

Session Lifecycle:
  agentcore configure → agentcore dev (local) → agentcore launch (deploy)
       ↓
  User Request → AgentCore Runtime → microVM (isolated session)
       ↓                                    ↓
  Session persists up to 8 hours     Agent state preserved
       ↓                                    ↓
  Mcp-Session-Id header for continuity   Auto-scales horizontally
```

---

## CDK Code Block — AgentCore Runtime Supporting Infrastructure

```python
def _create_agentcore_runtime(self, stage_name: str) -> None:
    """
    AgentCore Runtime supporting infrastructure.

    Components:
      A) IAM Role for AgentCore Runtime (Bedrock model access, tool permissions)
      B) S3 bucket for agent artifacts (tool outputs, traces)
      C) SSM parameters for runtime configuration
      D) CloudWatch log group for agent runtime logs

    [Claude: AgentCore Runtime itself is managed by AWS — this CDK creates
     the supporting IAM, storage, and config that the runtime needs.
     The agent code is deployed via `agentcore launch` CLI, not CDK.]
    """

    # =========================================================================
    # A) IAM ROLE — AgentCore Runtime Execution
    # =========================================================================

    self.agentcore_runtime_role = iam.Role(
        self, "AgentCoreRuntimeRole",
        assumed_by=iam.CompositePrincipal(
            iam.ServicePrincipal("bedrock.amazonaws.com"),
            iam.ServicePrincipal("lambda.amazonaws.com"),
        ),
        role_name=f"{{project_name}}-agentcore-runtime-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
        ],
    )

    # Bedrock model invocation (all foundation models the agent may use)
    self.agentcore_runtime_role.add_to_policy(
        iam.PolicyStatement(
            sid="BedrockModelInvoke",
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.*",
                f"arn:aws:bedrock:{self.region}::foundation-model/amazon.*",
                f"arn:aws:bedrock:{self.region}::foundation-model/meta.*",
            ],
        )
    )

    # =========================================================================
    # B) S3 — Agent Artifacts Bucket
    # =========================================================================

    self.agent_artifacts_bucket = s3.Bucket(
        self, "AgentArtifactsBucket",
        bucket_name=f"{{project_name}}-agent-artifacts-{stage_name}-{self.account}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
        lifecycle_rules=[
            s3.LifecycleRule(id="expire-old-artifacts", expiration=Duration.days(90)),
        ],
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        auto_delete_objects=stage_name != "prod",
    )
    self.agent_artifacts_bucket.grant_read_write(self.agentcore_runtime_role)

    # =========================================================================
    # C) SSM — Runtime Configuration
    # =========================================================================

    ssm.StringParameter(
        self, "AgentCoreRuntimeConfig",
        parameter_name=f"/{{project_name}}/{stage_name}/agentcore/runtime-config",
        string_value=json.dumps({
            "default_model_id": "anthropic.claude-sonnet-4-20250514-v1:0",
            "fast_model_id": "anthropic.claude-3-haiku-20240307-v1:0",
            "session_ttl_hours": 8,
            "max_turns": 30,
            "artifacts_bucket": f"{{project_name}}-agent-artifacts-{stage_name}-{self.account}",
        }),
        description="AgentCore Runtime configuration",
    )

    # =========================================================================
    # D) CLOUDWATCH — Runtime Logs
    # =========================================================================

    logs.LogGroup(
        self, "AgentCoreRuntimeLogs",
        log_group_name=f"/{{project_name}}/{stage_name}/agentcore-runtime",
        retention=logs.RetentionDays.ONE_MONTH if stage_name != "prod" else logs.RetentionDays.ONE_YEAR,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "AgentCoreRuntimeRoleArn",
        value=self.agentcore_runtime_role.role_arn,
        description="IAM Role ARN for AgentCore Runtime",
    )
```

---

## `.bedrock_agentcore.yaml` — Agent Configuration

```yaml
# .bedrock_agentcore.yaml — AgentCore deployment configuration
agent:
  name: "{{project_name}}-agent"
  description: "Strands-based agent for {{project_name}}"
  entry_point: "src/strands_agent/agentcore_app.py"

runtime:
  python_version: "3.12"
  requirements: "src/strands_agent/requirements.txt"

environment:
  DEFAULT_MODEL_ID: "anthropic.claude-sonnet-4-20250514-v1:0"
  STAGE: "dev"
```

---

## AgentCore App Wrapper — Pass 3 Reference

```python
"""AgentCore entrypoint wrapper for Strands agent."""
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
import os

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore managed entrypoint."""
    model = BedrockModel(
        model_id=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
    )
    agent = Agent(
        model=model,
        system_prompt="You are a helpful AI assistant for {{project_name}}.",
        tools=[],  # [Claude: add tools based on SOW]
    )
    response = agent(payload.get("message", ""))
    return {"response": str(response)}

if __name__ == "__main__":
    app.run()
```

---

## CLI Deployment Commands

```bash
# First-time setup
agentcore configure

# Local development testing
agentcore dev

# Deploy to AgentCore Runtime
agentcore launch

# Test invocation
agentcore invoke --payload '{"message": "Hello"}'
```
