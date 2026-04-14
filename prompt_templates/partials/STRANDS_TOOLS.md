# PARTIAL: Strands Tools — @tool Decorator, Tool Executors, Community Tools

**Usage:** Include when SOW mentions custom agent tools, @tool decorator, tool execution, parallel tools, or community tools package.

---

## Strands Tools Overview

```
Tool Types:
  1. Custom @tool functions (Python decorator with docstring schema)
  2. Community tools (strands-agents-tools package)
  3. MCP tools (see STRANDS_MCP_TOOLS.md)
  4. Agents as tools (see STRANDS_MULTI_AGENT.md)
  5. File-loaded tools (dynamic loading from .py files)

Tool Execution:
  - Sequential (default): tools run one at a time
  - Parallel: concurrent execution when model returns multiple tool_use blocks
```

---

## Custom Tool Pattern — Pass 3 Reference

```python
"""Custom tools using @tool decorator."""
from strands import tool

@tool
def query_database(sql: str, database: str = "default") -> str:
    """Execute a read-only SQL query against the database.

    Args:
        sql: The SQL SELECT query to execute.
        database: Target database name (default: 'default').

    Returns:
        Query results as formatted text.
    """
    # [Claude: implement based on SOW data layer]
    import boto3
    client = boto3.client("rds-data")
    response = client.execute_statement(
        resourceArn=os.environ["DB_CLUSTER_ARN"],
        secretArn=os.environ["DB_SECRET_ARN"],
        database=database,
        sql=sql,
    )
    return str(response.get("records", []))


@tool
def send_notification(channel: str, message: str, urgency: str = "normal") -> str:
    """Send a notification to a specified channel.

    Args:
        channel: Target channel — 'email', 'slack', or 'sns'.
        message: The notification message content.
        urgency: Priority level — 'low', 'normal', or 'high'.

    Returns:
        Confirmation of notification delivery.
    """
    # [Claude: implement based on SOW notification requirements]
    import boto3
    sns = boto3.client("sns")
    sns.publish(
        TopicArn=os.environ["ALERT_TOPIC_ARN"],
        Subject=f"[{urgency.upper()}] Agent Notification",
        Message=message,
    )
    return f"Notification sent to {channel}"
```

---

## Tool Executors — Parallel Execution

```python
"""Parallel tool execution for concurrent tool calls."""
from strands import Agent
from strands.tools.executor import ThreadPoolExecutor

# Default: sequential execution
agent = Agent(tools=[tool_a, tool_b, tool_c])

# Parallel: concurrent execution when model returns multiple tool_use blocks
agent = Agent(
    tools=[tool_a, tool_b, tool_c],
    tool_executor=ThreadPoolExecutor(max_workers=5),
)
```

---

## Community Tools Package

```python
"""Built-in tools from strands-agents-tools."""
# pip install strands-agents-tools
from strands_tools import (
    http_request,    # HTTP GET/POST/PUT/DELETE
    retrieve,        # Document retrieval
    calculator,      # Math calculations
    python_repl,     # Python code execution
    file_read,       # Read files
    file_write,      # Write files
    shell,           # Shell command execution
)

from strands import Agent
agent = Agent(tools=[http_request, calculator, python_repl])
```

---

## Loading Tools from Files

```python
"""Dynamic tool loading from Python files."""
from strands import Agent

# Load tools from a file path
agent = Agent(tools=["path/to/my_tools.py"])

# Auto-reload tools when file changes (dev mode)
agent = Agent(tools=["path/to/my_tools.py"], auto_reload_tools=True)
```

---

## Tool Design Best Practices

```
1. DOCSTRINGS ARE CRITICAL — Strands generates tool schema from docstrings.
   Always include: description, Args (with types), Returns.

2. KEEP TOOLS FOCUSED — One tool = one action. Don't combine unrelated logic.

3. RETURN STRINGS — Tools should return str. The model reads the output.

4. ERROR HANDLING — Catch exceptions and return error messages as strings.
   Don't let tools raise unhandled exceptions.

5. IDEMPOTENT WHEN POSSIBLE — Tools may be retried by the agent loop.

6. SECURITY — Validate inputs. Don't execute arbitrary code from user input.
   Scope database queries to read-only when possible.
```
