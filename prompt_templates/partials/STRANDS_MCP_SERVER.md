# SOP — Strands MCP Server (FastMCP Containers on AgentCore Runtime)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `mcp[cli]` ≥ 1.8 · Python 3.13 · AgentCore Runtime with `ProtocolType.MCP` · ARM64 (Graviton) container · CDK v2 for deployment

---

## 1. Purpose

- Codify the FastMCP server container pattern — one MCP server per data source, streamable HTTP transport, ARM64 Graviton.
- Provide a canonical server template (`server.py`) with `_ok()`/`_err()` JSON envelope helpers and `Context.report_progress` support.
- Provide three data-source variants (Redshift + RDS Data API connection pool, Neptune Gremlin, Aurora RDS Data API).
- Show the MCP-enabled `Dockerfile` (ARM64, `pip install mcp[cli]`, HTTP port 8080).
- Show how `AGENTCORE_RUNTIME` hosts the server with `ProtocolType.MCP`.
- Include when the SOW mentions MCP servers, data-source tool servers, FastMCP, or MCP-enabled Dockerfiles.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split on the server code itself.** An MCP server is a standalone container — there is no CDK-stack dual-variant for the code. §3 is the single canonical variant.
>
> The deploy topology (one AgentCore Runtime per MCP server + supporting IAM role + VPC wiring) follows the dual-variant rules in `AGENTCORE_RUNTIME §3 / §4`. In a micro-stack layout, each MCP runtime goes in its own stack (e.g. `RedshiftMcpStack`, `NeptuneMcpStack`), and the agent's execution role is granted `bedrock-agentcore:InvokeAgentRuntime` identity-side.

§4 Micro-Stack Variant for this partial is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Architecture

```
MCP Server = containerised tool server running on AgentCore Runtime:
  - FastMCP framework (mcp[cli] package)
  - Streamable HTTP transport on port 8080 (recommended; 8000 legacy)
  - ARM64 Graviton containers for cost
  - One MCP server per data source (Redshift, Neptune, Aurora, OpenSearch, SQLite)
  - Deployed as AgentCore Runtime with ProtocolType.MCP
  - Accessed via Gateway → Lambda proxy → invoke_agent_runtime()

Production servers:
  ┌──────────────────┬──────────────────────────────────────────────────────────┐
  │ Server           │ Data Source + Tools                                      │
  ├──────────────────┼──────────────────────────────────────────────────────────┤
  │ redshift-mcp     │ Redshift Serverless — P&L, vendor spend, cash, AR, budget │
  │ neptune-mcp      │ Neptune graph — vendor relationships, impact chains      │
  │ aurora-mcp       │ Aurora Serverless — POs, invoices, approvals, GL entries │
  │ opensearch-mcp   │ OpenSearch — SOP search, anomaly baselines               │
  │ sqlite-mcp       │ SQLite — lightweight fixture / CSV data                  │
  └──────────────────┴──────────────────────────────────────────────────────────┘
```

### 3.2 Dockerfile (MCP-enabled, ARM64)

```dockerfile
# infra/containers/redshift-mcp/Dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app

RUN pip install --no-cache-dir \
    "mcp[cli]>=1.8.0" \
    "psycopg2-binary==2.9.9" \
    "boto3>=1.35.0"

COPY server.py .

# AgentCore Runtime injects these; never hard-code credentials
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080

EXPOSE 8080
CMD ["python", "server.py"]
```

### 3.3 Server template (FastMCP + JSON envelope)

```python
"""MCP Server template — FastMCP with streamable HTTP transport."""
import json, logging, os
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MCP_HOST  = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT  = int(os.environ.get("MCP_PORT", "8080"))
CLIENT_ID = os.environ.get("CLIENT_ID", "")

mcp = FastMCP("{project_name}-mcp-server", host=MCP_HOST, port=MCP_PORT)


def _metadata() -> dict:
    return {
        'data_source': '{data_source}',
        'client_id':   CLIENT_ID,
        'data_as_of':  datetime.now(timezone.utc).isoformat(),
    }


def _ok(tool: str, rows: list, **meta) -> str:
    return json.dumps({
        "tool":      tool,
        "source":    "{data_source}",
        "row_count": len(rows),
        "data":      rows,
        "_metadata": _metadata(),
        **meta,
    }, default=str)


def _err(tool: str, exc: Exception) -> str:
    logger.exception("Tool %s failed", tool)
    return json.dumps({"tool": tool, "error": str(exc), "data": []})


# ── Tools ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_data(query_param: str = "", limit: int = 100, ctx: Context | None = None) -> str:
    """Retrieve data from the data source.

    Args:
        query_param: Filter parameter.
        limit:       Max rows (default 100).
    """
    try:
        if ctx:
            await ctx.report_progress(progress=0.1, total=1.0)

        rows = _execute_query(query_param, limit)

        if ctx:
            await ctx.report_progress(progress=1.0, total=1.0)

        return _ok("get_data", rows, query_param=query_param)
    except Exception as exc:
        return _err("get_data", exc)

# [Claude: generate one @mcp.tool() per data-retrieval operation from SOW]


if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host=MCP_HOST, port=MCP_PORT)
```

### 3.4 Redshift MCP server (pooled connections)

```python
"""Redshift MCP — financial analytics with thread-safe connection pooling."""
import os, threading
from contextlib import contextmanager
import psycopg2, psycopg2.pool
from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("redshift-mcp", host="0.0.0.0", port=8080)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _init_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=10,
                host=os.environ["REDSHIFT_HOST"],
                port=int(os.environ.get("REDSHIFT_PORT", "5439")),
                dbname=os.environ["REDSHIFT_DB"],
                user=os.environ["REDSHIFT_USER"],
                sslmode="require",
            )
    return _pool


@contextmanager
def _conn():
    pool = _init_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


@mcp.tool()
async def get_pnl_history(bu_id: str = "", period: str = "", limit: int = 120,
                           ctx: Context | None = None) -> str:
    """Retrieve P&L history by business unit and period.

    Args:
        bu_id:  Business unit code (blank = all).
        period: Fiscal period YYYY-MM (blank = all).
        limit:  Max rows (default 120).
    """
    conditions, params = [], []
    if bu_id:  conditions.append("p.bu_id = %s");         params.append(bu_id)
    if period: conditions.append("p.fiscal_period = %s"); params.append(period)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    if ctx: await ctx.report_progress(progress=0.1, total=1.0)

    rows = _query(f"""
        SELECT p.fiscal_period, p.bu_id, p.revenue_sar, p.cogs_sar,
               p.gross_profit_sar, p.ebitda_sar, p.gross_margin_pct
        FROM fact_pnl p {where}
        ORDER BY p.fiscal_period DESC LIMIT %s
    """, tuple(params + [limit]))

    if ctx: await ctx.report_progress(progress=1.0, total=1.0)
    import json
    return json.dumps({"tool": "get_pnl_history", "row_count": len(rows), "data": rows}, default=str)

# [Claude: generate more @mcp.tool() functions based on SOW data schema]


if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host="0.0.0.0", port=8080)
```

### 3.5 Neptune MCP server (graph traversal)

```python
"""Neptune MCP — Gremlin traversal tools (vendor graph, impact chains)."""
import json, os, urllib.request
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("neptune-mcp", host="0.0.0.0", port=8080)
NEPTUNE_ENDPOINT = os.environ['NEPTUNE_ENDPOINT']


def _gremlin(query: str) -> list:
    url = f"https://{NEPTUNE_ENDPOINT}:8182/gremlin"
    payload = json.dumps({'gremlin': query}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get('result', {}).get('data', {}).get('@value', [])


@mcp.tool()
def get_vendor_relationships(vendor_id: str) -> str:
    """Get all relationships for a vendor — cost centers, SKUs, BUs.

    Args:
        vendor_id: Canonical vendor identifier.
    Returns:
        JSON with the relationships list and count.
    """
    # NOTE: Do NOT interpolate vendor_id into Gremlin directly.
    # Use bindings in production; inline here only for readability.
    data = _gremlin(
        f"g.V().has('vendor_id', '{vendor_id}')"
        f".bothE().otherV().path().by(elementMap()).limit(50)"
    )
    return json.dumps({'vendor_id': vendor_id, 'relationships': data, 'count': len(data)})


@mcp.tool()
def get_impact_chain(source_id: str, hops: int = 3) -> str:
    """Trace downstream impact through IMPACTS / CAUSES edges.

    Args:
        source_id: Entity vertex id.
        hops:      Max traversal depth (capped at 5).
    Returns:
        JSON with the impact chain path.
    """
    data = _gremlin(
        f"g.V().has('~id', '{source_id}')"
        f".repeat(outE('IMPACTS','CAUSES').inV().simplePath()).times({min(hops, 5)})"
        f".path().by(elementMap()).limit(100)"
    )
    return json.dumps({'source': source_id, 'impact_chain': data})


if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host="0.0.0.0", port=8080)
```

### 3.6 Aurora MCP server (RDS Data API)

```python
"""Aurora MCP — transactional data via RDS Data API (no long-lived connections)."""
import json, os
import boto3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("aurora-mcp", host="0.0.0.0", port=8080)

_rds        = boto3.client('rds-data')
CLUSTER_ARN = os.environ['AURORA_CLUSTER_ARN']
SECRET_ARN  = os.environ['AURORA_SECRET_ARN']
DATABASE    = os.environ.get('AURORA_DATABASE', 'transactional')


def _query(sql: str, params: list | None = None) -> list[dict]:
    resp = _rds.execute_statement(
        resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN,
        database=DATABASE, sql=sql,
        includeResultMetadata=True,
        parameters=params or [],
    )
    columns = [c['name'] for c in resp.get('columnMetadata', [])]
    rows: list[dict] = []
    for record in resp.get('records', []):
        row = {}
        for i, col in enumerate(columns):
            field = record[i]
            if   'stringValue' in field: row[col] = field['stringValue']
            elif 'longValue'   in field: row[col] = field['longValue']
            elif 'doubleValue' in field: row[col] = field['doubleValue']
            elif 'isNull'      in field: row[col] = None
            else:                        row[col] = str(field)
        rows.append(row)
    return rows


@mcp.tool()
def get_pending_purchase_orders(status: str = "PENDING", min_amount: float = 0) -> str:
    """Get purchase orders by status and minimum amount.

    Args:
        status:     PO status (PENDING / APPROVED / REJECTED).
        min_amount: Minimum PO amount filter.
    Returns:
        JSON with purchase_orders list and count.
    """
    rows = _query(
        "SELECT * FROM purchase_orders WHERE status = :s AND amount_sar >= :a LIMIT 50",
        [
            {'name': 's', 'value': {'stringValue': status}},
            {'name': 'a', 'value': {'doubleValue': min_amount}},
        ],
    )
    return json.dumps({'purchase_orders': rows, 'count': len(rows)})


if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host="0.0.0.0", port=8080)
```

### 3.7 CDK deployment (AgentCore Runtime, `ProtocolType.MCP`)

The runtime construct and its IAM / VPC wiring live in `AGENTCORE_RUNTIME`. Reference shape:

```python
from aws_cdk import aws_ec2 as ec2
from aws_cdk.aws_bedrock_agentcore_alpha import (
    AgentRuntimeArtifact, Runtime, ProtocolType, RuntimeNetworkConfiguration,
)

artifact = AgentRuntimeArtifact.from_asset(
    str(Path(__file__).resolve().parents[3] / "infra" / "containers" / "redshift-mcp"),
    platform=ecr_assets.Platform.LINUX_ARM64,
)

Runtime(self, "RedshiftMcpRuntime",
    runtime_name="{project_name}_redshift_mcp",
    agent_runtime_artifact=artifact,
    execution_role=mcp_runtime_role,
    protocol_configuration=ProtocolType.MCP,   # MCP protocol, not HTTP
    network_configuration=RuntimeNetworkConfiguration.using_vpc(
        self, vpc=vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
    ),
    environment_variables={
        "REDSHIFT_WORKGROUP": "{project_name}-wg",
        "REDSHIFT_DB":        "{project_name}_warehouse",
        "REDSHIFT_IAM_AUTH":  "true",
    },
)
```

### 3.8 Tool schema (for Gateway target import)

When the Gateway target is a Lambda proxy, the target declares the tool schema explicitly:

```json
[
  {
    "name": "get_pnl_history",
    "description": "Retrieve P&L history by business unit and period",
    "inputSchema": {
      "type": "object",
      "properties": {
        "bu_id":  { "type": "string", "description": "Business unit code (blank = all)" },
        "period": { "type": "string", "description": "Fiscal period YYYY-MM" },
        "limit":  { "type": "number", "description": "Max rows (default 120)" }
      }
    }
  }
]
```

### 3.9 Gotchas

- **`mcp[cli]` vs `fastmcp`** — the canonical server import is `from mcp.server.fastmcp import FastMCP` (inside the `mcp` package). A separate `fastmcp` PyPI package exists and is API-compatible but not the AWS-recommended form. Pick one and pin it.
- **Port 8000 vs 8080** — AgentCore Runtime `ProtocolType.MCP` expects port **8080** by default. Legacy examples use 8000; match the runtime config.
- **Gremlin literal interpolation** — `_gremlin(f"...{vendor_id}...")` is injection-prone if the vertex id ever comes from untrusted input. Use Gremlin bindings (`{'gremlin': query, 'bindings': {...}}`) in any path that crosses the trust boundary.
- **RDS Data API rate limits** — Aurora Serverless v2 throttles the Data API at ~100 req/s per cluster. Back off with exponential retry or batch on the server side.
- **Connection-pool cold-start** — `psycopg2.pool.ThreadedConnectionPool` opens its first connection on the first tool call. Cold agent + cold pool adds ~1 s; pre-warm by hitting a no-op tool at container startup.
- **`ctx.report_progress` requires FastMCP ≥ 1.8**. Earlier versions silently drop the call.
- **Return type** — every `@mcp.tool()` must return `str` (raw JSON is fine). Returning a `dict` works locally with FastMCP but fails at the Gateway boundary.
- **Credentials** — never bake DB users / passwords into the image. Use execution-role IAM auth (Redshift, Aurora) or Secrets Manager ARNs injected via `environmentVariables`.

---

## 5. Swap matrix — server / transport variants

| Need | Swap |
|---|---|
| Tool reachable only from a single agent | Skip MCP; use `@tool` directly (`STRANDS_TOOLS`) |
| Connection-heavy query workload | Keep pooled `psycopg2` (§3.4) |
| Short-lived queries, stateless | RDS Data API (§3.6) — no long-lived connection |
| Graph traversal | Neptune Gremlin (§3.5) |
| Dev / fixture data | `sqlite-mcp` — one file, no IAM |
| Legacy port 8000 | Set `MCP_PORT=8000`, match the runtime's `containerConfiguration.port` |
| Use `fastmcp` PyPI package instead of `mcp[cli]` | Swap import; API is compatible |

---

## 6. Worked example — FastMCP lists tools offline

Save as `tests/sop/test_STRANDS_MCP_SERVER.py`. Offline; uses stdio transport against the server in a child process would require `uvx` — the offline variant below verifies the decorator registration path.

```python
"""SOP verification — FastMCP accepts tool decorators and exposes tool metadata."""
from mcp.server.fastmcp import FastMCP


def test_server_registers_tool():
    mcp = FastMCP("test-mcp", host="127.0.0.1", port=8080)

    @mcp.tool()
    async def echo(text: str) -> str:
        """Echo input back.

        Args:
            text: the text.
        Returns:
            Same text.
        """
        return text

    # FastMCP exposes registered tools on `._tool_manager` in current versions.
    # Use an attribute probe that's compatible across minor releases.
    tool_names = [t.name for t in mcp._tool_manager.list_tools()]
    assert "echo" in tool_names
```

---

## 7. References

- `docs/template_params.md` — `REDSHIFT_HOST`, `REDSHIFT_DB`, `REDSHIFT_WORKGROUP`, `NEPTUNE_ENDPOINT`, `AURORA_CLUSTER_ARN`, `AURORA_SECRET_ARN`, `MCP_PORT`, `MCP_RUNTIME_NAME`
- `docs/Feature_Roadmap.md` — feature IDs `AG-04` (MCP server runtime), `A-24` (data-source tools), `DL-06` (lakehouse tools)
- MCP Python SDK (FastMCP): https://github.com/modelcontextprotocol/python-sdk
- AgentCore Runtime `ProtocolType.MCP`: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-runtime-mcp.html
- Related SOPs: `AGENTCORE_RUNTIME` (hosting the server), `AGENTCORE_GATEWAY` (target that forwards to this server), `STRANDS_MCP_TOOLS` (client-side wiring), `LAYER_DATA` (Redshift / Aurora / Neptune resource definitions), `LAYER_NETWORKING` (VPC endpoints for RDS Data API / Neptune)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (server code) with explicit cross-reference to `AGENTCORE_RUNTIME` for the dual-variant deploy topology. Added Gotchas (§3.9) on port, Gremlin injection, RDS Data API limits. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite; consolidated three per-data-source examples into one canonical template plus three data-source variants. |
| 1.0 | 2026-03-05 | Initial — FastMCP template, Redshift / Neptune / Aurora servers, Dockerfile, CDK deploy reference, tool-schema JSON. |
