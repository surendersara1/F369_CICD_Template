# PARTIAL: Strands AgentCore — Deployment, Gateway (MCP), and Memory

**Usage:** Include when SOW mentions AgentCore deployment, managed agent hosting, MCP server/tools, agent memory (STM/LTM), or production agent lifecycle management.

---

## AgentCore Architecture Overview

```
AgentCore = AWS-managed deployment + Gateway + Memory for Strands agents:
  - AgentCore Runtime: Managed hosting (no ECS/Lambda infra to manage)
  - AgentCore Gateway: MCP endpoint for external tools (OAuth2 secured)
  - AgentCore Memory: Short-term (STM) + Long-term (LTM) conversation memory

AgentCore Stack:
  ┌─────────────────────────────────────────────────────────────────────┐
  │                     Bedrock AgentCore                               │
  │  ┌──────────────────┐  ┌───────────────────┐  ┌────────────────┐  │
  │  │  AgentCore       │  │  AgentCore        │  │  AgentCore     │  │
  │  │  Runtime         │  │  Gateway          │  │  Memory        │  │
  │  │  BedrockAgent    │  │  MCP Endpoint     │  │  STM + LTM     │  │
  │  │  CoreApp wrapper │  │  OAuth2 + Cognito │  │  Session Mgr   │  │
  │  └──────────────────┘  └───────────────────┘  └────────────────┘  │
  │                                                                     │
  │  Deployment Lifecycle:                                              │
  │    agentcore configure → agentcore dev → agentcore launch           │
  │                                                                     │
  │  Gateway Target Types:                                              │
  │    Lambda | OpenAPI | Smithy | MCP Server                           │
  └─────────────────────────────────────────────────────────────────────┘

Gateway MCP Flow:
  Strands Agent → MCPClient (streamable HTTP) → AgentCore Gateway
       ↓                                              ↓
  OAuth2 Token (Cognito)                    Lambda Target (tool execution)
       ↓                                              ↓
  Tool results returned to agent              DynamoDB / S3 / external APIs
```

---

## CDK Code Block — AgentCore Deployment + Gateway + Memory

```python
def _create_strands_agentcore(self, stage_name: str) -> None:
    """
    AgentCore deployment infrastructure: Gateway (MCP) + Memory + Auth.

    Components:
      A) Cognito User Pool + OAuth2 Client (Gateway authentication)
      B) Lambda tool targets (tools exposed via Gateway MCP endpoint)
      C) AgentCore Gateway configuration (CDK custom resource or CLI)
      D) AgentCore Memory configuration (STM + LTM)
      E) Agent wrapper code (BedrockAgentCoreApp + MCP client)

    [Claude: include A+B+C for any SOW mentioning AgentCore or MCP tools.
     Include D if SOW mentions conversation memory, session continuity, or user preferences.
     Always include E as the agent deployment wrapper.]
    """

    # =========================================================================
    # A) COGNITO — OAuth2 Authentication for AgentCore Gateway
    # =========================================================================

    # User Pool for Gateway OAuth2 (machine-to-machine auth)
    self.agentcore_user_pool = cognito.UserPool(
        self, "AgentCoreUserPool",
        user_pool_name=f"{{project_name}}-agentcore-{stage_name}",
        removal_policy=RemovalPolicy.DESTROY if stage_name != "prod" else RemovalPolicy.RETAIN,
        sign_in_aliases=cognito.SignInAliases(email=True),
        self_sign_up_enabled=False,  # Machine-to-machine only
    )

    # Resource server (defines OAuth2 scopes for tool access)
    resource_server = self.agentcore_user_pool.add_resource_server(
        "AgentCoreResourceServer",
        identifier=f"{{project_name}}-gateway",
        scopes=[
            cognito.ResourceServerScope(
                scope_name="tools.invoke",
                scope_description="Invoke tools via AgentCore Gateway",
            ),
        ],
    )

    # OAuth2 client credentials (for agent → gateway auth)
    self.agentcore_app_client = self.agentcore_user_pool.add_client(
        "AgentCoreAppClient",
        user_pool_client_name=f"{{project_name}}-agent-client-{stage_name}",
        generate_secret=True,
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[
                cognito.OAuthScope.resource_server(
                    resource_server,
                    cognito.ResourceServerScope(
                        scope_name="tools.invoke",
                        scope_description="Invoke tools via AgentCore Gateway",
                    ),
                ),
            ],
        ),
    )

    # Cognito domain for token endpoint
    self.agentcore_user_pool.add_domain(
        "AgentCoreDomain",
        cognito_domain=cognito.CognitoDomainOptions(
            domain_prefix=f"{{project_name}}-agentcore-{stage_name}",
        ),
    )

    # Store client credentials in Secrets Manager
    self.agentcore_client_secret = sm.Secret(
        self, "AgentCoreClientSecret",
        secret_name=f"{{project_name}}/{stage_name}/agentcore-gateway-credentials",
        description="OAuth2 client credentials for AgentCore Gateway",
        encryption_key=self.kms_key,
        generate_secret_string=sm.SecretStringGenerator(
            secret_string_template=json.dumps({
                "client_id": self.agentcore_app_client.user_pool_client_id,
                "token_endpoint": f"https://{{project_name}}-agentcore-{stage_name}.auth.{self.region}.amazoncognito.com/oauth2/token",
                "scope": f"{{project_name}}-gateway/tools.invoke",
            }),
            generate_string_key="client_secret",  # Placeholder — real secret from Cognito
        ),
    )
    self.agentcore_client_secret.grant_read(self.strands_agent_role)

    # =========================================================================
    # B) LAMBDA TOOL TARGETS — Tools Exposed via Gateway MCP Endpoint
    # =========================================================================

    # [Claude: generate one Lambda per tool group detected in SOW.
    #  Each Lambda implements tools that the agent calls via MCP Gateway.]

    self.gateway_tool_lambdas = {}

    # Example: Database query tool
    db_tool_fn = _lambda.Function(
        self, "GatewayToolDB",
        function_name=f"{{project_name}}-gateway-tool-db-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/gateway_tools/db_tool"),
        # [Claude: generate tool Lambda code in Pass 3]
        environment={
            "STAGE": stage_name,
            # [Claude: add DB connection env vars from LAYER_DATA]
        },
        timeout=Duration.seconds(30),
        memory_size=256,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
    )
    self.gateway_tool_lambdas["db_tool"] = db_tool_fn

    # Example: External API tool
    api_tool_fn = _lambda.Function(
        self, "GatewayToolAPI",
        function_name=f"{{project_name}}-gateway-tool-api-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/gateway_tools/api_tool"),
        environment={"STAGE": stage_name},
        timeout=Duration.seconds(30),
        memory_size=256,
    )
    self.gateway_tool_lambdas["api_tool"] = api_tool_fn

    # [Claude: add more tool Lambdas based on SOW-detected capabilities]

    # Allow Bedrock/AgentCore to invoke tool Lambdas
    for tool_name, tool_fn in self.gateway_tool_lambdas.items():
        tool_fn.add_permission(
            f"AgentCoreInvoke-{tool_name}",
            principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
            action="lambda:InvokeFunction",
        )

    # =========================================================================
    # C) AGENTCORE GATEWAY CONFIGURATION
    # [Claude: This is configured via agentcore CLI or custom resource.
    #  The CDK creates the supporting infra; the gateway itself is managed.]
    # =========================================================================

    # SSM parameters for AgentCore CLI configuration
    ssm.StringParameter(
        self, "AgentCoreGatewayConfig",
        parameter_name=f"/{{project_name}}/{stage_name}/agentcore/gateway-config",
        string_value=json.dumps({
            "gateway_name": f"{{project_name}}-gateway-{stage_name}",
            "auth": {
                "type": "OAUTH2",
                "cognito_user_pool_id": self.agentcore_user_pool.user_pool_id,
                "cognito_app_client_id": self.agentcore_app_client.user_pool_client_id,
            },
            "targets": [
                {
                    "name": tool_name,
                    "type": "LAMBDA",
                    "lambda_arn": tool_fn.function_arn,
                }
                for tool_name, tool_fn in self.gateway_tool_lambdas.items()
            ],
        }),
        description="AgentCore Gateway configuration for CLI setup",
    )

    # =========================================================================
    # D) AGENTCORE MEMORY CONFIGURATION (STM + LTM)
    # [Claude: include if SOW mentions conversation memory, user preferences,
    #  session continuity, or long-term learning]
    # =========================================================================

    # Memory configuration stored in SSM for agent runtime
    ssm.StringParameter(
        self, "AgentCoreMemoryConfig",
        parameter_name=f"/{{project_name}}/{stage_name}/agentcore/memory-config",
        string_value=json.dumps({
            "memory_id": f"{{project_name}}-memory-{stage_name}",
            "strategies": [
                {
                    "type": "SUMMARY",
                    "description": "Summarize conversation for context continuity",
                },
                {
                    "type": "USER_PREFERENCE",
                    "description": "Remember user preferences across sessions",
                },
                {
                    "type": "SEMANTIC",
                    "description": "Semantic memory for knowledge retention",
                },
            ],
            "session_config": {
                "session_ttl_hours": 24,
                "max_sessions_per_actor": 100,
            },
        }),
        description="AgentCore Memory configuration (STM + LTM strategies)",
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "AgentCoreUserPoolId",
        value=self.agentcore_user_pool.user_pool_id,
        description="Cognito User Pool ID for AgentCore Gateway OAuth2",
        export_name=f"{{project_name}}-agentcore-pool-{stage_name}",
    )
    CfnOutput(self, "AgentCoreClientId",
        value=self.agentcore_app_client.user_pool_client_id,
        description="OAuth2 Client ID for agent → gateway authentication",
    )
    CfnOutput(self, "AgentCoreGatewaySecretArn",
        value=self.agentcore_client_secret.secret_arn,
        description="Secrets Manager ARN for Gateway OAuth2 credentials",
    )
```

---

## AgentCore Deployment Wrapper — Pass 3 Reference

Claude generates this in `src/strands_agent/agentcore_app.py` during Pass 3:

```python
"""
AgentCore Deployment Wrapper — {{project_name}}
Wraps the Strands agent for managed AgentCore hosting.

Deploy with:
  agentcore configure  (first time only)
  agentcore dev        (local testing)
  agentcore launch     (deploy to AgentCore)
  agentcore invoke     (test invocation)
"""
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
import boto3, os, json, time

app = BedrockAgentCoreApp()

# =========================================================================
# MCP CLIENT — Connect to AgentCore Gateway for external tools
# =========================================================================

_token_cache = {"token": None, "expires_at": 0}

def _get_oauth2_token() -> str:
    """Get OAuth2 token with caching for Gateway authentication."""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    sm_client = boto3.client("secretsmanager")
    secret = json.loads(
        sm_client.get_secret_value(
            SecretId=os.environ["GATEWAY_SECRET_ARN"]
        )["SecretString"]
    )

    import requests
    resp = requests.post(
        secret["token_endpoint"],
        data={
            "grant_type": "client_credentials",
            "client_id": secret["client_id"],
            "client_secret": secret["client_secret"],
            "scope": secret["scope"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    token_data = resp.json()

    _token_cache["token"] = token_data["access_token"]
    _token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _token_cache["token"]


def get_gateway_mcp_client(gateway_url: str) -> MCPClient:
    """Create MCP client connected to AgentCore Gateway."""
    token = _get_oauth2_token()
    return MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url,
            headers={"Authorization": f"Bearer {token}"},
        )
    )


# =========================================================================
# AGENT WITH MEMORY — AgentCore Memory Integration
# =========================================================================

def create_agent_with_memory(session_id: str, actor_id: str):
    """Create agent with AgentCore Memory (STM + LTM)."""
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    memory_config = AgentCoreMemoryConfig(
        memory_id=os.environ.get("MEMORY_ID", ""),
        memory_strategies=["SUMMARY", "USER_PREFERENCE", "SEMANTIC"],
    )

    session_manager = AgentCoreMemorySessionManager(
        memory_config=memory_config,
        session_id=session_id,
        actor_id=actor_id,
    )

    model = BedrockModel(
        model_id=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
    )

    # Connect to Gateway MCP tools if configured
    tools = []
    gateway_url = os.environ.get("GATEWAY_MCP_URL")
    if gateway_url:
        mcp_client = get_gateway_mcp_client(gateway_url)
        tools.append(mcp_client)

    agent = Agent(
        model=model,
        system_prompt="You are a helpful AI assistant for {{project_name}}.",
        tools=tools,
        # [Claude: add local @tool functions as needed]
    )

    return agent, session_manager


# =========================================================================
# AGENTCORE ENTRYPOINT
# =========================================================================

@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore managed entrypoint."""
    user_message = payload.get("message", "")
    session_id = payload.get("session_id", "default")
    actor_id = payload.get("actor_id", "anonymous")

    agent, session_manager = create_agent_with_memory(session_id, actor_id)

    # Start memory session
    session_manager.start_session(agent)

    # Run agent
    response = agent(user_message)

    # End memory session (persists STM/LTM)
    session_manager.end_session(agent)

    return {
        "session_id": session_id,
        "response": str(response),
    }


if __name__ == "__main__":
    app.run()
```

---

## `.bedrock_agentcore.yaml` — Pass 3 Reference

Claude generates this config file in the project root during Pass 3:

```yaml
# .bedrock_agentcore.yaml — AgentCore deployment configuration
# Deploy: agentcore configure && agentcore launch

agent:
  name: "{{project_name}}-agent"
  description: "Strands-based agentic AI for {{project_name}}"
  entry_point: "src/strands_agent/agentcore_app.py"

runtime:
  python_version: "3.12"
  requirements: "src/strands_agent/requirements.txt"

memory:
  enabled: true
  mode: "agentcore"  # Use AgentCore managed memory (STM + LTM)
  # mode: "local"    # Use local in-memory (for dev/testing only)

gateway:
  enabled: true
  # Gateway URL is set after `agentcore launch` creates the endpoint

environment:
  DEFAULT_MODEL_ID: "anthropic.claude-sonnet-4-20250514-v1:0"
  STAGE: "dev"  # Override per environment
```
