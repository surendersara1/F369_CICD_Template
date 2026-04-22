# PARTIAL: Strands MCP Tools — Gateway Connection, Transports, Tool Discovery

**Usage:** Include when SOW mentions MCP tools, AgentCore Gateway, external tool protocols, or Model Context Protocol.

---

## MCP Integration Patterns (from real production)

```
Three connection patterns:
  1. SigV4 Auth → AgentCore Gateway (production — IAM-based, no secrets)
  2. OAuth2 Auth → AgentCore Gateway (alternative — Cognito client_credentials)
  3. stdio → Local MCP servers (dev/testing — uvx, npx)

Production Pattern:
  Agent (on AgentCore Runtime)
    → MCPClient(sigv4_transport(gateway_url))
    → AgentCore Gateway (IAM auth)
    → Gateway Target (Lambda or Lambda proxy → MCP Runtime)
    → Tool result returned to agent
```

---

## Pattern 1: SigV4 Auth → AgentCore Gateway (Production)

```python
"""Production pattern — SigV4 auth for agent-to-gateway MCP calls."""
import os
import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient

class HTTPXSigV4Auth(httpx.Auth):
    """SigV4 request signing for httpx (used by MCP streamable HTTP transport)."""
    def __init__(self, session, service, region):
        self.credentials = session.get_credentials().get_frozen_credentials()
        self.service = service
        self.region = region

    def auth_flow(self, request):
        aws_request = AWSRequest(method=request.method, url=str(request.url),
                                  data=request.content if hasattr(request, 'content') else b'')
        aws_request.headers['Host'] = request.url.host
        aws_request.headers['Content-Type'] = 'application/json'
        SigV4Auth(self.credentials, self.service, self.region).add_auth(aws_request)
        for name, value in aws_request.headers.items():
            request.headers[name] = value
        yield request

def create_gateway_transport(gateway_url: str):
    """Create SigV4-authenticated MCP transport for AgentCore Gateway."""
    session = boto3.Session()
    auth = HTTPXSigV4Auth(session, 'bedrock-agentcore',
                           os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
    return streamablehttp_client(url=gateway_url, auth=auth)

# Usage in agent:
GATEWAY_URL = os.environ['GATEWAY_URL']
gateway_client = MCPClient(lambda: create_gateway_transport(GATEWAY_URL))

with gateway_client:
    all_tools = gateway_client.list_tools_sync()
    agent = Agent(model=model, tools=all_tools, system_prompt=SYSTEM_PROMPT)
    result = agent("What is vendor spend this quarter?")
```

---

## Pattern 2: IAM Auth via mcp-proxy-for-aws (Alternative)

```python
"""Alternative — uses mcp-proxy-for-aws package for simpler setup."""
# pip install mcp-proxy-for-aws
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint=GATEWAY_URL,
    aws_region="us-east-1",
    aws_service="bedrock-agentcore",
))
```

---

## Pattern 3: stdio Transport (Dev/Testing)

```python
"""Local MCP servers for development."""
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"])
))

with mcp_client:
    agent = Agent(tools=mcp_client.list_tools_sync())
    result = agent("What is AWS Lambda?")
```

---

## Smart Tool Routing (Skip LLM Tool Selection)

```python
"""Deterministic keyword routing — 10x faster than LLM tool selection."""
import re

ROUTING_RULES = [
    (r'margin|revenue|p&l|profit|cogs|ebitda', ['get_pnl_history', 'get_pnl_by_cost_center']),
    (r'vendor|supplier|spend|procurement', ['get_vendor_spend']),
    (r'cash|treasury|covenant|liquidity', ['get_cash_balance']),
    (r'inventory|stock|warehouse|fill.rate', ['get_inventory_levels']),
    (r'ar|aging|dso|receivable', ['get_ar_aging_breakdown']),
    (r'budget|variance|actual', ['get_budget_vs_actual']),
    (r'kpi|dashboard|overview|summary', ['get_kpi_dashboard']),
    # [Claude: add routing rules based on SOW tool names]
]

def route_query(query: str, available_tools: list) -> tuple[list, bool]:
    """Match query keywords to specific tools. Returns (tools, is_deterministic)."""
    matched = set()
    for pattern, tool_names in ROUTING_RULES:
        if re.search(pattern, query.lower()):
            matched.update(tool_names)
    if not matched:
        return available_tools, False  # Fall back to full tool list (LLM selects)
    routed = [t for t in available_tools if getattr(t, 'name', str(t)) in matched]
    return (routed, True) if routed else (available_tools, False)

# Usage:
with gateway_client:
    all_tools = gateway_client.list_tools_sync()
    routed_tools, is_routed = route_query(query, all_tools)
    agent = Agent(model=model, tools=routed_tools, system_prompt=SYSTEM_PROMPT)
```

---

## Multiple MCP Servers

```python
"""Combine tools from multiple MCP servers."""
docs_client = MCPClient(lambda: stdio_client(StdioServerParameters(
    command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"])))
gateway_client = MCPClient(lambda: create_gateway_transport(GATEWAY_URL))

with docs_client, gateway_client:
    agent = Agent(tools=[docs_client, gateway_client])
```

---

## MCP on Lambda (Connection Lifecycle)

```python
"""MCP connections MUST be within context manager on Lambda."""
def handler(event, context):
    with MCPClient(lambda: create_gateway_transport(GATEWAY_URL)) as mcp:
        agent = Agent(tools=mcp.list_tools_sync())
        response = agent(event.get("message", ""))
    return {"statusCode": 200, "body": str(response)}
```
