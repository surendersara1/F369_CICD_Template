# PARTIAL: Strands MCP Server — FastMCP Containers on AgentCore Runtime

**Usage:** Include when SOW mentions MCP servers, data source tools, FastMCP, containerized tool servers, or MCP-enabled Dockerfiles.

---

## MCP Server Architecture (from real production)

```
MCP Server = Containerized tool server running on AgentCore Runtime:
  - FastMCP framework (from mcp[cli] package)
  - Streamable HTTP transport on port 8000 or 8080
  - ARM64 Graviton containers for cost optimization
  - One MCP server per data source (Redshift, Neptune, Aurora, OpenSearch, etc.)
  - Deployed as AgentCore Runtime with ProtocolType.MCP
  - Accessed via Gateway → Lambda proxy → invoke_agent_runtime()

MCP Server Types in Production:
  ┌──────────────────┬──────────────────────────────────────────────┐
  │ Server           │ Data Source + Tools                          │
  ├──────────────────┼──────────────────────────────────────────────┤
  │ redshift-mcp     │ Redshift Serverless — P&L, vendor spend,    │
  │                  │ cash balance, AR aging, budget vs actual     │
  │ neptune-mcp      │ Neptune graph — vendor relationships,       │
  │                  │ cost center impact chains, supplier network  │
  │ aurora-mcp       │ Aurora Serverless — POs, invoices, contracts,│
  │                  │ approvals, GL entries, payment schedule      │
  │ opensearch-mcp   │ OpenSearch Serverless — SOP search,         │
  │                  │ anomaly baselines, semantic search           │
  │ sqlite-mcp       │ SQLite — lightweight fixture/CSV data       │
  └──────────────────┴──────────────────────────────────────────────┘
```

---

## MCP Server Dockerfile — Pass 3 Reference

```dockerfile
# infra/containers/redshift-mcp/Dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app

# Install MCP framework + data source driver
RUN pip install --no-cache-dir \
    "mcp[cli]>=1.8.0" \
    "psycopg2-binary==2.9.9" \
    "boto3>=1.35.0"

COPY server.py .

# Environment variables injected by AgentCore Runtime (no hardcoded credentials)
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

EXPOSE 8000
CMD ["python", "server.py"]
```

---

## MCP Server Pattern (FastMCP) — Pass 3 Reference

```python
"""MCP Server template — FastMCP with streamable HTTP transport."""
import json, logging, os
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
CLIENT_ID = os.environ.get("CLIENT_ID", "")

mcp = FastMCP("{{project_name}}-mcp-server", host=MCP_HOST, port=MCP_PORT)


def _metadata():
    return {
        'data_source': '{{data_source}}',
        'client_id': CLIENT_ID,
        'data_as_of': datetime.now(timezone.utc).isoformat(),
    }


def _ok(tool: str, rows: list, **meta) -> str:
    return json.dumps({
        "tool": tool, "source": "{{data_source}}", "row_count": len(rows),
        "data": rows, "_metadata": _metadata(), **meta,
    }, default=str)


def _err(tool: str, exc: Exception) -> str:
    logger.exception("Tool %s failed", tool)
    return json.dumps({"tool": tool, "error": str(exc), "data": []})


# ── Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_data(query_param: str = "", limit: int = 100, ctx: Context = None) -> str:
    """Retrieve data from the data source.

    Args:
        query_param: Filter parameter.
        limit: Max rows (default 100).
    """
    try:
        # MCP progress notifications (Feature 8: Stateful MCP)
        if ctx:
            await ctx.report_progress(progress=0.1, total=1.0)

        rows = _execute_query(query_param, limit)

        if ctx:
            await ctx.report_progress(progress=1.0, total=1.0)

        return _ok("get_data", rows, query_param=query_param)
    except Exception as exc:
        return _err("get_data", exc)

# [Claude: generate one @mcp.tool() per data retrieval operation from SOW]


if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host=MCP_HOST, port=MCP_PORT)
```

---

## Redshift MCP Server — Pass 3 Reference

```python
"""Redshift MCP Server — financial analytics tools with connection pooling."""
import json, logging, os, threading
from contextlib import contextmanager
import psycopg2, psycopg2.pool
from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("redshift-mcp", host="0.0.0.0", port=8000)

# Connection pool (thread-safe, lazy-initialized)
_pool = None
_pool_lock = threading.Lock()

def _init_pool():
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
async def get_pnl_history(bu_id: str = "", period: str = "", limit: int = 120, ctx: Context = None) -> str:
    """Retrieve P&L history by business unit and period.
    Args:
        bu_id: Business unit code (blank = all).
        period: Fiscal period YYYY-MM (blank = all).
        limit: Max rows (default 120).
    """
    conditions, params = [], []
    if bu_id: conditions.append("p.bu_id = %s"); params.append(bu_id)
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
    return _ok("get_pnl_history", rows)

# [Claude: generate more @mcp.tool() functions based on SOW data schema]

if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host="0.0.0.0", port=8000)
```

---

## Neptune MCP Server — Pass 3 Reference

```python
"""Neptune MCP Server — graph traversal tools."""
import json, logging, os, urllib.request
from fastmcp import FastMCP

mcp = FastMCP("neptune-mcp")
NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', '')

def _gremlin(query: str) -> list:
    url = f"https://{NEPTUNE_ENDPOINT}:8182/gremlin"
    payload = json.dumps({'gremlin': query}).encode()
    req = urllib.request.Request(url, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get('result', {}).get('data', {}).get('@value', [])

@mcp.tool()
def get_vendor_relationships(vendor_id: str) -> str:
    """Get all relationships for a vendor — cost centers, SKUs, BUs."""
    data = _gremlin(f"g.V().has('vendor_id', '{vendor_id}').bothE().otherV().path().by(elementMap()).limit(50)")
    return json.dumps({'vendor_id': vendor_id, 'relationships': data, 'count': len(data)})

@mcp.tool()
def get_impact_chain(source_id: str, hops: int = 3) -> str:
    """Trace downstream impact chain from a source entity through IMPACTS/CAUSES edges."""
    data = _gremlin(
        f"g.V().has('~id', '{source_id}')"
        f".repeat(outE('IMPACTS','CAUSES').inV().simplePath()).times({min(hops, 5)})"
        f".path().by(elementMap()).limit(100)")
    return json.dumps({'source': source_id, 'impact_chain': data})

if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host="0.0.0.0", port=8080)
```

---

## Aurora MCP Server — Pass 3 Reference

```python
"""Aurora MCP Server — transactional data via RDS Data API."""
import json, logging, os
import boto3
from fastmcp import FastMCP

mcp = FastMCP("aurora-mcp")
_rds = boto3.client('rds-data')
CLUSTER_ARN = os.environ['AURORA_CLUSTER_ARN']
SECRET_ARN = os.environ['AURORA_SECRET_ARN']
DATABASE = os.environ.get('AURORA_DATABASE', 'transactional')

def _query(sql: str, params: list = None) -> list[dict]:
    resp = _rds.execute_statement(
        resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN,
        database=DATABASE, sql=sql, includeResultMetadata=True,
        parameters=params or [])
    columns = [c['name'] for c in resp.get('columnMetadata', [])]
    rows = []
    for record in resp.get('records', []):
        row = {}
        for i, col in enumerate(columns):
            field = record[i]
            if 'stringValue' in field: row[col] = field['stringValue']
            elif 'longValue' in field: row[col] = field['longValue']
            elif 'doubleValue' in field: row[col] = field['doubleValue']
            elif 'isNull' in field: row[col] = None
            else: row[col] = str(field)
        rows.append(row)
    return rows

@mcp.tool()
def get_pending_purchase_orders(status: str = "PENDING", min_amount: float = 0) -> str:
    """Get purchase orders by status and minimum amount."""
    rows = _query("SELECT * FROM purchase_orders WHERE status = :s AND amount_sar >= :a LIMIT 50",
        [{'name': 's', 'value': {'stringValue': status}},
         {'name': 'a', 'value': {'doubleValue': min_amount}}])
    return json.dumps({'purchase_orders': rows, 'count': len(rows)})

if __name__ == "__main__":
    mcp.run(transport="streamablehttp", host="0.0.0.0", port=8080)
```

---

## CDK: Deploy MCP Server as AgentCore Runtime

```typescript
// See AGENTCORE_RUNTIME.md for full pattern
const artifact = agentcore.AgentRuntimeArtifact.fromAsset('infra/containers/redshift-mcp', {
  platform: assets.Platform.LINUX_ARM64,
});

new agentcore.Runtime(this, 'RedshiftMcpRuntime', {
  runtimeName: '{{project_name}}_redshift_mcp',
  agentRuntimeArtifact: artifact,
  executionRole: mcpRuntimeRole,
  protocolConfiguration: agentcore.ProtocolType.MCP,  // ← MCP protocol
  networkConfiguration: RuntimeNetworkConfiguration.usingVpc(this, {
    vpc, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  }),
  environmentVariables: {
    REDSHIFT_WORKGROUP: '{{project_name}}-wg',
    REDSHIFT_DB: '{{project_name}}_warehouse',
    REDSHIFT_IAM_AUTH: 'true',
  },
});
```

---

## Tool Schema JSON (for Gateway Target)

```json
[
  {
    "name": "get_pnl_history",
    "description": "Retrieve P&L history by business unit and period",
    "inputSchema": {
      "type": "object",
      "properties": {
        "bu_id": { "type": "string", "description": "Business unit code (blank = all)" },
        "period": { "type": "string", "description": "Fiscal period YYYY-MM" },
        "limit": { "type": "number", "description": "Max rows (default 120)" }
      }
    }
  }
]
```
