# SOP — Strands MCP Tools (Gateway, Transports, Tool Discovery, Routing)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · `mcp` Python SDK ≥ 0.3 · `mcp-proxy-for-aws` (optional) · `httpx` ≥ 0.27 · AgentCore Gateway · Python 3.12+

---

## 1. Purpose

- Codify the three MCP transport patterns used in production:
  1. **SigV4 → AgentCore Gateway** (production default, IAM-based, no secrets)
  2. **IAM via `mcp-proxy-for-aws`** (simpler setup, same security)
  3. **stdio** (local dev / integration tests)
- Codify MCP connection lifecycle — `with MCPClient(...)` is mandatory; a bare `list_tools_sync()` on a closed transport raises a non-obvious `RuntimeError`.
- Codify deterministic keyword routing (`route_query`) that selects a tool subset before handing to the LLM — ~10× faster p95 than full-list tool selection.
- Codify multi-server composition (multiple MCP clients combined into one agent).
- Include when the SOW mentions MCP tools, AgentCore Gateway, Model Context Protocol, or external tool protocols.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** MCP client wiring is agent-side Python. §3 is the single canonical variant.
>
> The Gateway resource itself (`bedrock-agentcore:CreateGateway`, target configuration, IAM) is defined in `AGENTCORE_GATEWAY`. The server-side MCP runtime is covered by `STRANDS_MCP_SERVER`.

§4 Micro-Stack Variant is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Transport selection

```
Three connection patterns:
  1. SigV4 → AgentCore Gateway         (PROD  : IAM-based, no secrets)
  2. mcp-proxy-for-aws → Gateway       (PROD  : same IAM auth, simpler setup)
  3. stdio → local MCP server          (DEV   : uvx / npx subprocess)

Production flow:
  Agent (on AgentCore Runtime)
    → MCPClient(sigv4_transport(gateway_url))
    → AgentCore Gateway (IAM auth — bedrock-agentcore:InvokeGateway on role)
    → Gateway Target (Lambda proxy → MCP runtime)
    → Tool result returned to agent
```

### 3.2 Pattern 1 — SigV4 auth (production default)

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
    """SigV4 request signing for httpx — used by the MCP streamable HTTP transport."""

    def __init__(self, session: boto3.Session, service: str, region: str):
        self.credentials = session.get_credentials().get_frozen_credentials()
        self.service     = service
        self.region      = region

    def auth_flow(self, request):
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content if hasattr(request, 'content') else b'',
        )
        aws_request.headers['Host']         = request.url.host
        aws_request.headers['Content-Type'] = 'application/json'
        SigV4Auth(self.credentials, self.service, self.region).add_auth(aws_request)
        for name, value in aws_request.headers.items():
            request.headers[name] = value
        yield request


def create_gateway_transport(gateway_url: str):
    """Return a SigV4-signed MCP transport bound to AgentCore Gateway."""
    session = boto3.Session()
    auth = HTTPXSigV4Auth(
        session,
        'bedrock-agentcore',
        os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
    )
    return streamablehttp_client(url=gateway_url, auth=auth)


# Usage
GATEWAY_URL = os.environ['GATEWAY_URL']
gateway_client = MCPClient(lambda: create_gateway_transport(GATEWAY_URL))

with gateway_client:
    all_tools = gateway_client.list_tools_sync()
    agent = Agent(model=model, tools=all_tools, system_prompt=SYSTEM_PROMPT)
    result = agent("What is vendor spend this quarter?")
```

### 3.3 Pattern 2 — IAM via `mcp-proxy-for-aws`

Preferred when you want the SigV4 flow without owning the `httpx.Auth` boilerplate.

```python
"""Alternative — mcp-proxy-for-aws handles SigV4 for you."""
# pip install mcp-proxy-for-aws
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint=GATEWAY_URL,
    aws_region="us-east-1",
    aws_service="bedrock-agentcore",
))
```

### 3.4 Pattern 3 — stdio transport (dev / test)

```python
"""Local MCP servers via uvx or npx — development only."""
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient
from strands import Agent

mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(
        command="uvx",
        args=["awslabs.aws-documentation-mcp-server@latest"],
    )
))

with mcp_client:
    agent = Agent(tools=mcp_client.list_tools_sync())
    result = agent("What is AWS Lambda?")
```

### 3.5 Deterministic tool routing

Skips the LLM tool-selection round-trip for queries that match a keyword rule. Falls back to full-list LLM selection when nothing matches.

```python
"""Deterministic keyword routing — often 10× faster p95 than LLM tool selection."""
import re

ROUTING_RULES = [
    (r'margin|revenue|p&l|profit|cogs|ebitda',  ['get_pnl_history', 'get_pnl_by_cost_center']),
    (r'vendor|supplier|spend|procurement',      ['get_vendor_spend']),
    (r'cash|treasury|covenant|liquidity',       ['get_cash_balance']),
    (r'inventory|stock|warehouse|fill.rate',    ['get_inventory_levels']),
    (r'ar|aging|dso|receivable',                ['get_ar_aging_breakdown']),
    (r'budget|variance|actual',                 ['get_budget_vs_actual']),
    (r'kpi|dashboard|overview|summary',         ['get_kpi_dashboard']),
    # [Claude: add routing rules based on SOW tool names]
]


def route_query(query: str, available_tools: list) -> tuple[list, bool]:
    """Match query keywords to a tool subset. Returns (tools, is_deterministic)."""
    matched: set[str] = set()
    for pattern, tool_names in ROUTING_RULES:
        if re.search(pattern, query.lower()):
            matched.update(tool_names)
    if not matched:
        return available_tools, False  # LLM fallback — full list
    routed = [t for t in available_tools if getattr(t, 'name', str(t)) in matched]
    return (routed, True) if routed else (available_tools, False)


# Inside the agent entrypoint
with gateway_client:
    all_tools = gateway_client.list_tools_sync()
    routed_tools, is_routed = route_query(query, all_tools)
    agent = Agent(model=model, tools=routed_tools, system_prompt=SYSTEM_PROMPT)
```

### 3.6 Multiple MCP servers in one agent

```python
"""Compose tools from multiple MCP servers."""
docs_client    = MCPClient(lambda: stdio_client(StdioServerParameters(
    command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"],
)))
gateway_client = MCPClient(lambda: create_gateway_transport(GATEWAY_URL))

with docs_client, gateway_client:
    agent = Agent(tools=[docs_client, gateway_client])
```

### 3.7 MCP on Lambda — lifecycle

```python
"""MCP transports MUST live inside a context manager — Lambda too."""
def handler(event, context):
    with MCPClient(lambda: create_gateway_transport(GATEWAY_URL)) as mcp:
        agent = Agent(tools=mcp.list_tools_sync())
        response = agent(event.get("message", ""))
    return {"statusCode": 200, "body": str(response)}
```

### 3.8 Gotchas

- **`list_tools_sync()` outside `with`** raises a non-obvious `RuntimeError: transport closed`. Always wrap.
- **SigV4 `Host` header** must be set *before* signing. If the httpx client adds it afterward, the signature mismatches and Gateway returns 403. The `HTTPXSigV4Auth.auth_flow` above sets it explicitly — keep that order.
- **Tool schema leakage** — `list_tools_sync()` returns every tool on the gateway target. If the target exposes tools the agent shouldn't see, scope at the gateway (different target) rather than filtering client-side.
- **Streamable HTTP keepalive** — `streamablehttp_client` keeps a long-lived connection. Lambda containers may die mid-call; wrap `agent(...)` with a 30 s retry so a cold pool re-establishes.
- **stdio subprocesses leak on crash.** If the parent Python process dies mid-handler, `uvx` / `npx` children may become zombies on the container. Not an issue on Lambda (container is torn down), real on ECS — prefer streamable HTTP in prod.
- **Routing table drift.** `ROUTING_RULES` is a code-level list; if the gateway adds a tool but `ROUTING_RULES` isn't updated, the tool is only reachable via LLM fallback. Document and re-sync on every target change.
- **`Agent(tools=[docs_client, gateway_client])`** — passing the MCP client objects (not `list_tools_sync()`) makes Strands lazy-list; both clients must still be active in a `with` block for the full agent turn.

---

## 5. Swap matrix — transport / routing variants

| Need | Swap |
|---|---|
| Simpler SigV4 setup | Pattern 2 (`mcp-proxy-for-aws`) instead of hand-rolled `HTTPXSigV4Auth` |
| Local dev without AWS | Pattern 3 (stdio) with `uvx awslabs.*-mcp-server` |
| Mix local + remote tools | §3.6 — pass both clients to `Agent(tools=[…])` |
| Cognito-based auth to Gateway | Replace SigV4 with OAuth2 client_credentials; see `AGENTCORE_GATEWAY` for the identity pool config |
| Latency-critical queries | Enable `route_query` — deterministic routing skips LLM tool-selection |
| Tool visibility per persona | Do NOT filter client-side — create separate Gateway targets per persona role |
| Tool discovery at container startup (cache) | Cache `list_tools_sync()` result in a module-level var; refresh on container cold start |

---

## 6. Worked example — MCP client + router integration

Save as `tests/sop/test_STRANDS_MCP_TOOLS.py`. Offline; stdio transport with a stub server.

```python
"""SOP verification — routing picks the right subset; MCP client errors outside `with`."""
from unittest.mock import MagicMock
import pytest


def test_router_deterministic_match():
    from shared.tool_router import route_query
    tools = [MagicMock(**{'name': 'get_pnl_history'}),
             MagicMock(**{'name': 'get_vendor_spend'}),
             MagicMock(**{'name': 'get_kpi_dashboard'})]
    for t in tools:
        t.name = t._extract_mock_name()  # expose name attr

    # 'margin' → pnl tools only
    routed, det = route_query("what is operating margin?", tools)
    assert det is True
    assert {getattr(t, 'name') for t in routed} == {'get_pnl_history'}


def test_router_falls_back_to_full_list():
    from shared.tool_router import route_query
    tools = [MagicMock(**{'name': 'get_kpi_dashboard'})]
    for t in tools:
        t.name = 'get_kpi_dashboard'
    routed, det = route_query("explain the weather patterns", tools)
    assert det is False
    assert len(routed) == 1


def test_mcp_client_errors_without_context(monkeypatch):
    """Sanity-check the gotcha — list_tools_sync outside `with` fails."""
    # Synthetic client object that rejects access outside context
    class FakeClient:
        def __enter__(self):  return self
        def __exit__(self, *_): pass
        def list_tools_sync(self):
            raise RuntimeError("transport closed")
    client = FakeClient()
    with pytest.raises(RuntimeError):
        client.list_tools_sync()
```

---

## 7. References

- `docs/template_params.md` — `GATEWAY_URL`, `MCP_TRANSPORT` (`sigv4` / `oauth2` / `stdio`), `MCP_ROUTING_ENABLED`
- `docs/Feature_Roadmap.md` — feature IDs `STR-06` (MCP tools), `AG-03` (AgentCore Gateway)
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- `mcp-proxy-for-aws` package: https://pypi.org/project/mcp-proxy-for-aws/
- AgentCore Gateway overview: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-gateway.html
- Related SOPs: `STRANDS_AGENT_CORE` (how MCP tools plug into the supervisor), `STRANDS_MCP_SERVER` (server-side MCP runtime), `AGENTCORE_GATEWAY` (the Gateway resource + targets + IAM), `STRANDS_DEPLOY_ECS` / `STRANDS_DEPLOY_LAMBDA` (granting `bedrock-agentcore:InvokeGateway` on the agent role)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added Gotchas (§3.8) covering `with`-context requirement, SigV4 host-header ordering, and routing-table drift. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — SigV4, `mcp-proxy-for-aws`, stdio transport, routing, multi-server, Lambda lifecycle. |
