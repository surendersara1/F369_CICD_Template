# PARTIAL: Strands Orchestration — Runtime SOPs & Execution Safety

**Usage:** Include when SOW mentions AgentCore Runtime execution context, MCP session management, long-running tasks, progress notifications, or tenant-scoped execution.

---

## AgentCore Runtime Execution Context

```
When running inside AgentCore Runtime microVM:
  - Mcp-Session-Id header maintains context across 8-hour window
  - Progress Notifications required for tasks > 2 minutes
  - Code Interpreter available for Python execution
  - All tool calls scoped to tenant_id from identity context
  - MCP Sampling for LLM-generated content within tools
```

---

## Execution Safety Rules

```python
"""Execution safety patterns for AgentCore Runtime."""

# 1. TENANT SCOPING — All tool calls must be scoped
@tool
def query_data(query: str, **kwargs) -> str:
    """Query data scoped to current tenant."""
    state = kwargs.get("invocation_state", {})
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        return "Error: No tenant context available"
    # Scope all queries to tenant
    return execute_query(query, tenant_filter=tenant_id)

# 2. PROGRESS NOTIFICATIONS — For long-running tasks
@tool
def generate_report(params: str) -> str:
    """Generate a report with progress updates."""
    # Emit progress every 30 seconds for long tasks
    for step in range(total_steps):
        process_step(step)
        if step % 5 == 0:
            emit_progress(f"Step {step}/{total_steps} complete")
    return "Report generated"

# 3. SESSION STATE — Use Mcp-Session-Id for continuity
# AgentCore automatically manages session state within the 8-hour window.
# No manual session management needed when using AgentCore Runtime.

# 4. VALIDATION — Check outputs before returning
# Verify no PII leaked during data retrieval
# Apply guardrails to all agent outputs
```

---

## Related Partials

- Agent core class and tools: `STRANDS_AGENT_CORE.md`
- Multi-agent patterns: `STRANDS_MULTI_AGENT.md`
- AgentCore Runtime: `AGENTCORE_RUNTIME.md`
- AgentCore Memory: `AGENTCORE_MEMORY.md`
- AgentCore Gateway: `AGENTCORE_GATEWAY.md`
