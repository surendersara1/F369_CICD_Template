# PARTIAL: AgentCore Gateway — MCP Endpoint for External Tools

**Usage:** Include when SOW mentions AgentCore Gateway, MCP server/tools, tool gateway, Lambda tool targets, or external tool access via MCP protocol.

---

## AgentCore Gateway Overview

```
AgentCore Gateway = MCP endpoint exposing tools to agents:
  - Lambda targets (each Lambda = one or more MCP tools)
  - OAuth2 secured via AgentCore Identity (Cognito)
  - Streamable HTTP transport for MCP clients
  - AWS IAM auth via mcp-proxy-for-aws package
  - Supports OpenAPI, Smithy, and MCP Server target types

Gateway Flow:
  Strands Agent → MCPClient (streamable HTTP) → AgentCore Gateway
       ↓                                              ↓
  OAuth2 Token (Cognito) or IAM SigV4        Lambda Target (tool execution)
       ↓                                              ↓
  Tool results returned to agent              DynamoDB / S3 / external APIs
```

---

## CDK Code Block — AgentCore Gateway Infrastructure

```python
def _create_agentcore_gateway(self, stage_name: str) -> None:
    """
    AgentCore Gateway — MCP tool endpoint infrastructure.

    Components:
      A) Lambda tool targets (tools exposed via Gateway)
      B) Gateway configuration (SSM for CLI setup)
      C) IAM permissions for Bedrock to invoke tool Lambdas

    [Claude: include when SOW mentions MCP tools, external tool access,
     or AgentCore Gateway. Generate one Lambda per tool group from SOW.]
    """

    # =========================================================================
    # A) LAMBDA TOOL TARGETS
    # =========================================================================

    self.gateway_tool_lambdas = {}

    # [Claude: generate one Lambda per tool group detected in SOW]
    db_tool_fn = _lambda.Function(
        self, "GatewayToolDB",
        function_name=f"{{project_name}}-gateway-tool-db-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/gateway_tools/db_tool"),
        environment={"STAGE": stage_name},
        timeout=Duration.seconds(30),
        memory_size=256,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
    )
    self.gateway_tool_lambdas["db_tool"] = db_tool_fn

    api_tool_fn = _lambda.Function(
        self, "GatewayToolAPI",
        function_name=f"{{project_name}}-gateway-tool-api-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/gateway_tools/api_tool"),
        environment={"STAGE": stage_name},
        timeout=Duration.seconds(30),
        memory_size=256,
    )
    self.gateway_tool_lambdas["api_tool"] = api_tool_fn

    # =========================================================================
    # B) IAM — Allow Bedrock/AgentCore to invoke tool Lambdas
    # =========================================================================

    for tool_name, tool_fn in self.gateway_tool_lambdas.items():
        tool_fn.add_permission(
            f"AgentCoreInvoke-{tool_name}",
            principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
            action="lambda:InvokeFunction",
        )

    # =========================================================================
    # C) GATEWAY CONFIG — SSM for CLI setup
    # =========================================================================

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
                {"name": name, "type": "LAMBDA", "lambda_arn": fn.function_arn}
                for name, fn in self.gateway_tool_lambdas.items()
            ],
        }),
        description="AgentCore Gateway configuration for CLI setup",
    )
```

---

## MCP Client Connection — Pass 3 Reference

```python
"""Connect Strands agent to AgentCore Gateway via MCP."""
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

def get_gateway_mcp_client(gateway_url: str, token: str) -> MCPClient:
    """Create MCP client connected to AgentCore Gateway (OAuth2)."""
    return MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url,
            headers={"Authorization": f"Bearer {token}"},
        )
    )

# Usage with agent:
# with get_gateway_mcp_client(url, token) as mcp:
#     agent = Agent(model=model, tools=[mcp])
```

### AWS IAM Auth (alternative to OAuth2)

```python
"""Connect via IAM SigV4 using mcp-proxy-for-aws."""
# pip install mcp-proxy-for-aws
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint="https://your-service.us-east-1.amazonaws.com/mcp",
    aws_region="us-east-1",
    aws_service="bedrock-agentcore",
))
```
