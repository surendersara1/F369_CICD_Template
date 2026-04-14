# PARTIAL: Strands MCP Tools — Model Context Protocol Integration

**Usage:** Include when SOW mentions MCP tools, MCP servers, external tool protocols, AgentCore Gateway MCP, or Model Context Protocol.

---

## MCP Integration Overview

```
MCP = Open protocol for agent ↔ tool server communication:
  - MCPClient wraps any MCP server as Strands tools
  - Transport options: stdio, Streamable HTTP, SSE, AWS IAM
  - Context manager pattern for connection lifecycle
  - Tool filtering and name prefixing for multi-server setups
  - Elicitation support (server requests info from user)

Transport Options:
  stdio          → Local CLI tools (uvx, npx)
  Streamable HTTP → Remote HTTP servers (AgentCore Gateway)
  AWS IAM        → AWS services with SigV4 (mcp-proxy-for-aws)
  SSE            → Server-Sent Events transport (legacy)
```

---

## MCP Client Patterns — Pass 3 Reference

### stdio Transport (local MCP servers)

```python
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(
        command="uvx",
        args=["awslabs.aws-documentation-mcp-server@latest"],
    )
))

# Context manager ensures proper connection lifecycle
with mcp_client:
    tools = mcp_client.list_tools_sync()
    agent = Agent(tools=tools)
    response = agent("What is AWS Lambda?")
```

### Streamable HTTP Transport (remote servers / AgentCore Gateway)

```python
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient

# Basic HTTP connection
mcp_client = MCPClient(
    lambda: streamablehttp_client("http://localhost:8000/mcp")
)

# With OAuth2 authentication (AgentCore Gateway)
mcp_client = MCPClient(
    lambda: streamablehttp_client(
        url="https://gateway.example.com/mcp",
        headers={"Authorization": f"Bearer {token}"},
    )
)

with mcp_client:
    agent = Agent(tools=[mcp_client])
    response = agent("Query the database for recent orders")
```

### AWS IAM Transport (SigV4 authentication)

```python
# pip install mcp-proxy-for-aws
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint="https://your-service.us-east-1.amazonaws.com/mcp",
    aws_region="us-east-1",
    aws_service="bedrock-agentcore",
))
```

### Multiple MCP Servers

```python
from strands import Agent
from strands.tools.mcp import MCPClient

docs_client = MCPClient(lambda: stdio_client(StdioServerParameters(
    command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"])))

db_client = MCPClient(lambda: streamablehttp_client("http://localhost:8001/mcp"))

with docs_client, db_client:
    agent = Agent(tools=[docs_client, db_client])
    response = agent("Search docs and query the database")
```

---

## Lambda MCP Lifecycle

```python
"""MCP on Lambda — connection lifecycle management."""
from strands import Agent
from strands.tools.mcp import MCPClient

def handler(event, context):
    # MCP connections MUST be within context manager on Lambda
    with MCPClient(lambda: streamablehttp_client(url)) as mcp:
        agent = Agent(tools=[mcp])
        response = agent(event.get("message", ""))
    return {"statusCode": 200, "body": str(response)}
```

---

## Implementing an MCP Server

```python
"""Custom MCP server exposing tools to agents."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-tools-server")

@mcp.tool()
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"Weather in {city}: 72°F, sunny"

@mcp.tool()
def search_inventory(product: str, limit: int = 10) -> str:
    """Search product inventory."""
    return f"Found {limit} results for {product}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
```
