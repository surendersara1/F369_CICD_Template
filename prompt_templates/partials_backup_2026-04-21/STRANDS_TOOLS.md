# PARTIAL: Strands Tools — @tool, Code Interpreter, Shell Executor, Community Tools

**Usage:** Include when SOW mentions custom agent tools, code execution, shell commands, or community tools.

---

## Tool Patterns (from real production)

```
Tool Types in Production:
  1. @tool functions — custom business logic (KB search, artifact save, etc.)
  2. Sub-agent tools — wrap invoke_agent_runtime() as @tool
  3. Code Interpreter — AgentCore invoke_code_interpreter() with S3 chart upload
  4. Shell Executor — SQL queries and Python scripts with sandboxing
  5. MCP tools — via Gateway (see STRANDS_MCP_TOOLS.md)
  6. Community tools — strands-agents-tools package (http_request, calculator, etc.)
```

---

## @tool Pattern — Pass 3 Reference

```python
"""Custom tools using @tool decorator. Docstrings = schema."""
from strands import tool
import boto3, os, json, time

@tool
def search_knowledge_base(query: str, max_results: int = 5) -> str:
    """Search the knowledge base for relevant information.
    Args:
        query: The search query to find relevant documents.
        max_results: Maximum number of results to return (default 5).
    Returns:
        Formatted search results with source citations.
    """
    client = boto3.client("bedrock-agent-runtime")
    response = client.retrieve(
        knowledgeBaseId=os.environ.get("KNOWLEDGE_BASE_ID", ""),
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": max_results}},
    )
    results = []
    for i, r in enumerate(response.get("retrievalResults", []), 1):
        text = r["content"]["text"]
        score = r.get("score", 0)
        results.append(f"[Source {i}] (score: {score:.2f})\n{text}")
    return "\n---\n".join(results) if results else "No results found."

@tool
def save_artifact(filename: str, content: str) -> str:
    """Save an artifact to S3 with presigned URL.
    Args:
        filename: Name of the file to save.
        content: Content to write.
    Returns:
        S3 URI and presigned URL of the saved artifact.
    """
    s3 = boto3.client("s3")
    bucket = os.environ["AGENT_ARTIFACTS_BUCKET"]
    key = f"artifacts/{time.strftime('%Y/%m/%d')}/{filename}"
    s3.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"))
    url = s3.generate_presigned_url('get_object',
        Params={'Bucket': bucket, 'Key': key}, ExpiresIn=3600)
    return json.dumps({"s3_uri": f"s3://{bucket}/{key}", "presigned_url": url})
```

---

## Code Interpreter — Pass 3 Reference

```python
"""Code Interpreter — execute Python in AgentCore sandbox with S3 chart upload."""
import base64, io, json, logging, os, time, uuid
import boto3

_agentcore = boto3.client('bedrock-agentcore')
_s3 = boto3.client('s3')
CHARTS_BUCKET = os.environ.get('CHARTS_BUCKET', '')

@tool
def run_financial_analysis(code: str, description: str = "") -> str:
    """Execute Python code for custom analysis, charts, and simulations.
    Available: numpy, pandas, matplotlib, scipy, seaborn.
    Generated charts are uploaded to S3 with presigned URLs.
    Args:
        code: Complete Python script to execute.
        description: What this script does (for audit trail).
    Returns:
        Execution result with stdout, stderr, and chart URLs.
    """
    try:
        resp = _agentcore.invoke_code_interpreter(
            code=code, language='python', timeoutInSeconds=120)
        files = {}
        media_urls = []
        for f in resp.get('outputFiles', []):
            file_name = f['name']
            file_bytes = f['content']
            files[file_name] = base64.b64encode(file_bytes).decode()
            # Upload to S3 with presigned URL
            if CHARTS_BUCKET:
                key = f"charts/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex[:8]}_{file_name}"
                _s3.put_object(Bucket=CHARTS_BUCKET, Key=key, Body=file_bytes)
                url = _s3.generate_presigned_url('get_object',
                    Params={'Bucket': CHARTS_BUCKET, 'Key': key}, ExpiresIn=3600)
                media_urls.append({'name': file_name, 'url': url})
        return json.dumps({
            'stdout': resp.get('stdout', ''), 'stderr': resp.get('stderr', ''),
            'files': files, 'media_urls': media_urls,
            'success': resp.get('exitCode', 1) == 0,
        })
    except Exception as e:
        return json.dumps({'error': str(e), 'success': False})
```

---

## Shell Executor — Pass 3 Reference

```python
"""Shell executor — SQL queries and Python scripts with sandboxing."""

@tool
def run_sql_query(sql: str, database: str = "default") -> str:
    """Execute a read-only SQL query against the database.
    Only SELECT/WITH/EXPLAIN statements are allowed.
    Args:
        sql: SQL SELECT query to execute.
        database: Target database name.
    Returns:
        Query results as JSON with columns, rows, and row count.
    """
    # Validate: only allow read-only statements
    sql_upper = sql.strip().upper()
    if not any(sql_upper.startswith(kw) for kw in ['SELECT', 'WITH', 'EXPLAIN']):
        return json.dumps({'error': 'Only SELECT/WITH/EXPLAIN statements allowed'})
    # [Claude: implement using Redshift Data API or Aurora Data API]
    return json.dumps({'columns': [], 'rows': [], 'row_count': 0})
```

---

## Community Tools

```python
"""Built-in tools from strands-agents-tools."""
# pip install strands-agents-tools
from strands_tools import http_request, calculator, python_repl, retrieve
from strands import Agent

agent = Agent(tools=[http_request, calculator, python_repl])
```

---

## Tool Design Rules

```
1. DOCSTRINGS ARE SCHEMA — Strands generates tool schema from docstrings.
   Always include: description, Args (with types), Returns.
2. RETURN STRINGS — Tools must return str. The model reads the output.
3. ERROR HANDLING — Catch exceptions, return error as string. Never raise.
4. IDEMPOTENT — Tools may be retried by the agent loop.
5. SECURITY — Validate inputs. Scope DB queries to read-only.
6. PRESIGNED URLS — For file outputs, upload to S3 and return presigned URL.
```
