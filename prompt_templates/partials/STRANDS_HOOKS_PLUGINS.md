# PARTIAL: Strands Hooks & Plugins — Steering Hooks, Circuit Breaker, RBAC Middleware

**Usage:** Include when SOW mentions agent hooks, RBAC middleware, tenant isolation, circuit breaker, tool interception, or agent extensibility.

---

## Hooks Architecture (from real production)

```
Production uses 3 hook patterns:
  1. Steering Hooks: 3-layer RBAC (agent/tool/data access control)
  2. Circuit Breaker: Prevent cascading failures across agents
  3. Strands Lifecycle Hooks: BeforeToolCall, AfterToolCall, etc.

Steering Hooks Flow:
  Request → check_agent_access(persona) → Agent invocation
    → before_tool_call(tool_name, input) → Tool execution
    → after_tool_call(tool_name, output) → Data masking → Response
```

---

## 3-Layer RBAC Steering Hooks — Pass 3 Reference

```python
"""3-layer RBAC: agent access → tool access → data filtering."""
import json, logging, re

class AfieSteeringHooks:
    """Runtime middleware enforcing 3-layer RBAC + tenant isolation."""

    def __init__(self, client_id: str, persona: str = 'user', rbac_policy: dict = None):
        self.client_id = client_id
        self.persona = persona
        self.rbac = rbac_policy or {}

    # Layer 1: Agent Access
    def check_agent_access(self, agent_name: str) -> bool:
        access = self.rbac.get('agent_access', {})
        if access.get(agent_name) is False or access.get(agent_name) == 'denied':
            raise PermissionError(f"'{self.persona}' not authorized for {agent_name}")
        return True

    # Layer 2: Tool Access + Tenant Isolation
    def before_tool_call(self, tool_name: str, tool_input: dict) -> dict:
        # Tenant isolation (always enforced)
        input_str = json.dumps(tool_input).lower()
        competitors = ['competitor_a', 'competitor_b']  # [Claude: from SOW]
        for comp in competitors:
            if comp in input_str and comp != self.client_id.lower():
                raise ValueError(f"Cannot access competitor data ({comp})")

        # Tool whitelist/blacklist
        tool_access = self.rbac.get('tool_access', {})
        denied = tool_access.get('denied', [])
        if tool_name in denied:
            return {'__rbac_denied': True, 'error': f'Tool {tool_name} denied for {self.persona}'}

        # Inject SQL filters for data-scoped tools
        sql_filter = self.rbac.get('data_filter', {}).get('sql_filter', '')
        if sql_filter and 'sql' in tool_input:
            tool_input['sql'] += f' {sql_filter}'

        return tool_input

    # Layer 3: Data Filtering (field masking on output)
    def after_tool_call(self, tool_name: str, output: str) -> str:
        # Redact PII patterns
        output = re.sub(r'\b[12]\d{9}\b', '[REDACTED_NID]', output)

        # Mask persona-restricted fields
        for field in self.rbac.get('data_filter', {}).get('mask_fields', []):
            output = re.sub(rf'"{field}"\s*:\s*"[^"]*"', f'"{field}": "[RESTRICTED]"', output)
            output = re.sub(rf'"{field}"\s*:\s*[\d.]+', f'"{field}": "[RESTRICTED]"', output)
        return output
```

---

## Strands Lifecycle Hooks — Pass 3 Reference

```python
"""Strands hook registration for logging, metrics, tool interception."""
from strands import Agent
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent, BeforeInvocationEvent

agent = Agent(system_prompt="...", tools=[...])

# Log every tool call
def log_tool(event: BeforeToolCallEvent) -> None:
    print(f"Calling tool: {event.tool_use['name']}")
agent.add_hook(log_tool)

# Track tool latency
import time
_timers = {}
def start_timer(event: BeforeToolCallEvent):
    _timers[event.tool_use['name']] = time.time()
def end_timer(event: AfterToolCallEvent):
    elapsed = time.time() - _timers.pop(event.tool_use['name'], time.time())
    print(f"Tool {event.tool_use['name']} took {elapsed:.2f}s")
agent.add_hook(start_timer)
agent.add_hook(end_timer)
```

---

## Plugin Pattern — Pass 3 Reference

```python
"""Plugins bundle multiple hooks with configuration."""
from strands.plugins import Plugin, hook
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent

class PerformancePlugin(Plugin):
    name = "performance-plugin"

    @hook
    def start_timer(self, event: BeforeToolCallEvent):
        self._start = time.time()

    @hook
    def end_timer(self, event: AfterToolCallEvent):
        elapsed = time.time() - self._start
        print(f"Tool {event.tool_use['name']} took {elapsed:.2f}s")

agent = Agent(plugins=[PerformancePlugin()])
```

---

## Circuit Breaker — Pass 3 Reference

```python
"""Circuit breaker — prevents cascading failures across agents."""
import time

class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_secs: int = 60):
        self._failures = 0
        self._last_failure = 0.0
        self._open = False
        self._threshold = threshold
        self._reset_secs = reset_secs

    def check(self):
        if self._open:
            if time.time() - self._last_failure > self._reset_secs:
                self._open = False; self._failures = 0
            else:
                raise RuntimeError(f'Circuit breaker OPEN — {self._failures} failures')

    def record_success(self):
        self._failures = 0; self._open = False

    def record_failure(self):
        self._failures += 1
        self._last_failure = time.time()
        if self._failures >= self._threshold:
            self._open = True

# Usage in agent entrypoint:
# circuit = CircuitBreaker()
# circuit.check()  # Raises if open
# try:
#     result = agent(query)
#     circuit.record_success()
# except Exception:
#     circuit.record_failure()
#     raise
```
