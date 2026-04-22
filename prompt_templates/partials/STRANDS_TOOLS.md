# SOP — Strands Tools (@tool, Code Interpreter, Shell Executor, Community Tools)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · `strands-agents-tools` ≥ 0.1 · Python 3.12+ · `bedrock-agentcore` SDK (for Code Interpreter) · boto3 ≥ 1.34

---

## 1. Purpose

- Codify the `@tool` decorator pattern — docstring-as-schema for custom agent tools.
- Provide reference implementations for the five tool types used in production:
  1. Custom `@tool` functions (KB search, artifact save, domain business logic)
  2. Sub-agent tools (wrap `invoke_agent_runtime()` as `@tool`)
  3. Code Interpreter — AgentCore Python sandbox with S3 chart upload
  4. Shell / SQL executors with read-only sandboxing
  5. Community tools (`http_request`, `calculator`, `python_repl`, `retrieve` from `strands-agents-tools`)
- Enforce the six tool-design rules (docstring schema, return `str`, catch errors, idempotency, input validation, presigned URLs for binary outputs).
- Include when the SOW mentions custom agent tools, code execution, SQL tools, or any tool beyond MCP.

> **MCP tools** — when you want the tool to be reusable by other agents via Gateway — are covered separately in `STRANDS_MCP_TOOLS`. Use this SOP for in-process tools; use MCP when you need cross-agent reuse.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** `@tool` functions are Python callables inside the agent container; no CDK resources are declared here. §3 is the single canonical variant.
>
> Permissions the tool requires (e.g. `s3:PutObject` on `CHARTS_BUCKET`, `bedrock:Retrieve` on a KB) are granted on the agent's **execution role** — see `STRANDS_DEPLOY_ECS §4` / `STRANDS_DEPLOY_LAMBDA §4` / `AGENTCORE_RUNTIME §4`.

§4 Micro-Stack Variant is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Tool taxonomy

```
Tool Types in Production:
  1. @tool functions     — custom business logic (KB search, artifact save, etc.)
  2. Sub-agent tools     — wrap invoke_agent_runtime() as @tool
  3. Code Interpreter    — AgentCore invoke_code_interpreter() with S3 chart upload
  4. Shell / SQL Executor — SQL queries and Python scripts with sandboxing
  5. MCP tools           — via Gateway (see STRANDS_MCP_TOOLS)
  6. Community tools     — strands-agents-tools (http_request, calculator, etc.)
```

### 3.2 The `@tool` pattern

Docstrings are the tool schema. Strands generates the Bedrock tool spec from them.

```python
"""Custom tools using the @tool decorator. Docstrings = schema."""
import os, json, time
import boto3
from strands import tool


@tool
def search_knowledge_base(query: str, max_results: int = 5) -> str:
    """Search the knowledge base for relevant information.

    Args:
        query:       The search query to find relevant documents.
        max_results: Maximum number of results to return (default 5).
    Returns:
        Formatted search results with source citations.
    """
    client = boto3.client("bedrock-agent-runtime")
    response = client.retrieve(
        knowledgeBaseId=os.environ.get("KNOWLEDGE_BASE_ID", ""),
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": max_results}
        },
    )
    results = []
    for i, r in enumerate(response.get("retrievalResults", []), 1):
        text  = r["content"]["text"]
        score = r.get("score", 0)
        results.append(f"[Source {i}] (score: {score:.2f})\n{text}")
    return "\n---\n".join(results) if results else "No results found."


@tool
def save_artifact(filename: str, content: str) -> str:
    """Save an artifact to S3 with a presigned URL.

    Args:
        filename: Name of the file to save.
        content:  Content to write.
    Returns:
        JSON containing s3_uri and presigned_url.
    """
    s3 = boto3.client("s3")
    bucket = os.environ["AGENT_ARTIFACTS_BUCKET"]
    key = f"artifacts/{time.strftime('%Y/%m/%d')}/{filename}"
    s3.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"))
    url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=3600,
    )
    return json.dumps({"s3_uri": f"s3://{bucket}/{key}", "presigned_url": url})
```

### 3.3 Code Interpreter — Python sandbox + S3 chart upload

```python
"""Code Interpreter — execute Python in AgentCore sandbox, upload charts to S3."""
import base64, json, os, time, uuid
import boto3
from strands import tool

_agentcore    = boto3.client('bedrock-agentcore')
_s3           = boto3.client('s3')
CHARTS_BUCKET = os.environ.get('CHARTS_BUCKET', '')


@tool
def run_financial_analysis(code: str, description: str = "") -> str:
    """Execute Python for custom analysis, charts, and simulations.

    Available libraries: numpy, pandas, matplotlib, scipy, seaborn.
    Generated charts are uploaded to S3 with a 1-hour presigned URL.

    Args:
        code:        Complete Python script to execute.
        description: What this script does (for audit trail).
    Returns:
        JSON with stdout, stderr, files (base64), media_urls (presigned).
    """
    try:
        resp = _agentcore.invoke_code_interpreter(
            code=code, language='python', timeoutInSeconds=120,
        )
        files, media_urls = {}, []
        for f in resp.get('outputFiles', []):
            file_name  = f['name']
            file_bytes = f['content']
            files[file_name] = base64.b64encode(file_bytes).decode()
            if CHARTS_BUCKET:
                key = f"charts/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex[:8]}_{file_name}"
                _s3.put_object(Bucket=CHARTS_BUCKET, Key=key, Body=file_bytes)
                url = _s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': CHARTS_BUCKET, 'Key': key},
                    ExpiresIn=3600,
                )
                media_urls.append({'name': file_name, 'url': url})
        return json.dumps({
            'stdout':     resp.get('stdout', ''),
            'stderr':     resp.get('stderr', ''),
            'files':      files,
            'media_urls': media_urls,
            'success':    resp.get('exitCode', 1) == 0,
        })
    except Exception as e:
        return json.dumps({'error': str(e), 'success': False})
```

### 3.4 Shell / SQL executor (read-only sandbox)

```python
"""SQL executor — SELECT/WITH/EXPLAIN only. Other statements rejected."""
import json
from strands import tool


@tool
def run_sql_query(sql: str, database: str = "default") -> str:
    """Execute a read-only SQL query against the database.

    Only SELECT/WITH/EXPLAIN statements are allowed — writes are rejected.

    Args:
        sql:      SQL SELECT query to execute.
        database: Target database name.
    Returns:
        JSON with columns, rows, and row_count; or error string.
    """
    sql_upper = sql.strip().upper()
    if not any(sql_upper.startswith(kw) for kw in ('SELECT', 'WITH', 'EXPLAIN')):
        return json.dumps({'error': 'Only SELECT/WITH/EXPLAIN statements allowed'})
    # [Claude: implement with Redshift Data API (redshift-data) or Aurora Data API (rds-data).
    #  Never build the SQL by f-string interpolation of user input — the LLM-provided `sql`
    #  is already the query, but user-derived literals inside it must be parameterized.]
    return json.dumps({'columns': [], 'rows': [], 'row_count': 0})
```

### 3.5 Community tools (`strands-agents-tools`)

```python
"""Built-in tools from the strands-agents-tools package."""
# pip install strands-agents-tools
from strands_tools import http_request, calculator, python_repl, retrieve
from strands import Agent

agent = Agent(tools=[http_request, calculator, python_repl])
```

Pick community tools deliberately:

| Tool | When to use | Watch for |
|---|---|---|
| `http_request`  | External API calls, webhook POSTs | Tool can reach any URL — tighten via the execution role's egress SG / proxy |
| `calculator`    | Numeric sanity checks inside the agent | Input is eval-like; rely on the library's parser, don't pre-substitute |
| `python_repl`   | Quick local compute during dev | Do NOT ship to prod — use Code Interpreter (sandboxed) instead |
| `retrieve`      | Bedrock Knowledge Base retrieval | Requires `bedrock:Retrieve` on role |

### 3.6 Design rules (all six apply to every tool you write)

1. **Docstrings are schema.** Always include a one-line description, `Args:` block with types, and `Returns:`. Strands reads this to build the Bedrock tool spec.
2. **Return `str` (or JSON-encoded `str`).** The model reads the return value as text. Return `json.dumps({...})` for structured output.
3. **Catch exceptions.** Never raise out of a tool — the agent loop will crash. Return an error string / `{"error": ...}` JSON.
4. **Be idempotent.** The agent loop can retry a tool call. A second call with the same args must not double-write, double-charge, or duplicate side effects.
5. **Validate inputs.** Reject writes in read-only tools. Reject oversized payloads. Treat every tool arg as LLM-generated and potentially adversarial.
6. **Presigned URLs for binary outputs.** Never return raw bytes; upload to S3, return a time-bounded presigned URL (1 h default).

### 3.7 Gotchas

- **Missing `Returns:` in docstring** → Strands generates an empty output schema and the model cannot reason about the tool's output. Always include it.
- **`@tool` with no type annotations on params** → schema defaults to `string` for all args, and the model may pass the wrong shape. Always annotate.
- **`invoke_code_interpreter` timeouts are per-invocation, not cumulative.** A long analysis that trips 120 s on one call can be split into multiple `@tool` invocations that the agent chains.
- **`s3.generate_presigned_url` uses the caller's credential chain.** In AgentCore Runtime the caller is the runtime execution role — if the role lacks `s3:GetObject` on the object, the URL is valid but returns 403.
- **`strands_tools.python_repl`** shares the agent's Python process. Memory leaks, global-state mutations, and import side-effects persist across tool calls in the same agent turn.

---

## 5. Swap matrix — tool-style variants

| Need | Swap |
|---|---|
| Tool must be reusable by multiple agents | Replace `@tool` with MCP tool — see `STRANDS_MCP_TOOLS` |
| Tool needs VPC-only egress (private RDS, internal API) | Wrap the call in a Lambda; tool just calls `lambda.invoke` |
| Tool output is huge (> 256 KB) | Upload to S3, return presigned URL; do not return raw bytes in the tool result |
| Tool needs streaming progress | Emit progress via `WebSocketCallbackHandler.send_custom_step()` (see `STRANDS_AGENT_CORE §3.7`) |
| Tool requires structured tabular output | Return JSON-stringified `{"columns": [...], "rows": [...]}` — the agent can cite rows |
| Community tool is almost-right | Copy the source into `tools/` and modify; don't monkey-patch the upstream |

---

## 6. Worked example — verify tool registration + schema

Save as `tests/sop/test_STRANDS_TOOLS.py`. Offline; no AWS calls.

```python
"""SOP verification — tools register, schemas generated, error path returns string."""
from unittest.mock import patch
from strands import Agent, tool
from strands.models import BedrockModel


@tool
def echo(text: str) -> str:
    """Echo input back.

    Args:
        text: The text to echo.
    Returns:
        The same text.
    """
    return text


@tool
def failing(text: str) -> str:
    """Always fails — demonstrates error handling contract.

    Args:
        text: Ignored.
    Returns:
        JSON with error.
    """
    try:
        raise RuntimeError("synthetic")
    except Exception as e:
        import json
        return json.dumps({"error": str(e)})


def test_tool_registers_with_agent():
    model = BedrockModel(model_id="anthropic.claude-haiku-4-5-20251001-v1:0")
    with patch('boto3.client'):
        agent = Agent(model=model, tools=[echo, failing], system_prompt="test")
    assert agent is not None


def test_failing_tool_returns_string_not_raise():
    result = failing("x")
    assert isinstance(result, str)
    assert '"error"' in result
```

---

## 7. References

- `docs/template_params.md` — `KNOWLEDGE_BASE_ID`, `AGENT_ARTIFACTS_BUCKET`, `CHARTS_BUCKET`, `CODE_INTERPRETER_TIMEOUT_SECONDS`
- `docs/Feature_Roadmap.md` — feature IDs `STR-04` (custom tools), `STR-05` (code interpreter), `A-22` (KB retrieval)
- Strands `@tool` reference: https://strandsagents.com/latest/user-guide/concepts/tools/
- `strands-agents-tools` package: https://pypi.org/project/strands-agents-tools/
- Related SOPs: `STRANDS_AGENT_CORE` (how tools plug into `Agent(...)`), `STRANDS_MCP_TOOLS` (cross-agent tools), `STRANDS_HOOKS_PLUGINS` (before/after tool hooks for RBAC), `STRANDS_DEPLOY_LAMBDA` / `STRANDS_DEPLOY_ECS` / `AGENTCORE_RUNTIME` (grants for tool permissions on the execution role)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added design-rules table, Gotchas (§3.7), Swap matrix (§5), Worked example (§6). Community-tools quick picker added (§3.5). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — `@tool`, Code Interpreter, SQL sandbox, community-tools, design rules. |
