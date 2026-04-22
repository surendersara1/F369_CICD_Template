# SOP — Strands Hooks & Plugins (Steering, RBAC, Circuit Breaker, Lifecycle)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 (hooks + plugins API) · Python 3.12+

---

## 1. Purpose

- Codify the 3-layer RBAC steering hook (agent-access → tool-access → data-masking) used for tenant isolation and persona enforcement.
- Codify a circuit breaker to stop cascading failures across sub-agents.
- Document the Strands lifecycle hook surface (`BeforeToolCallEvent`, `AfterToolCallEvent`, `BeforeInvocationEvent`) and when to use each.
- Codify the plugin pattern (multiple hooks + state bundled as a single `Plugin`).
- Include when the SOW mentions agent hooks, RBAC middleware, tenant isolation, tool interception, circuit breakers, or agent extensibility.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** Hooks and plugins are Python classes inside the agent process. §3 is the single canonical variant.
>
> The RBAC policy that the hook consumes (`rbac_policy` dict) typically comes from Cognito claims, a tenant-config table (DynamoDB), or an SSM parameter. Those resources are defined in `LAYER_SECURITY` / `LAYER_DATA`.

§4 Micro-Stack Variant is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Hook flow

```
Production uses 3 hook patterns:
  1. Steering Hooks       — 3-layer RBAC (agent / tool / data access control)
  2. Circuit Breaker      — prevent cascading failures across agents
  3. Strands lifecycle    — BeforeToolCallEvent, AfterToolCallEvent, BeforeInvocationEvent

Steering Hooks flow per request:
  Request → check_agent_access(persona)               # Layer 1 (agent allow-list)
         → Agent invocation
         → before_tool_call(tool_name, input)         # Layer 2 (tool allow/deny + SQL filter)
         → Tool execution
         → after_tool_call(tool_name, output)         # Layer 3 (field masking, PII redact)
         → Response to caller
```

### 3.2 3-layer RBAC steering hook

```python
"""3-layer RBAC steering hook — agent access, tool access, data filtering."""
import json, re


class AfieSteeringHooks:
    """Runtime middleware enforcing 3-layer RBAC and tenant isolation."""

    def __init__(self, client_id: str, persona: str = 'user', rbac_policy: dict | None = None):
        self.client_id = client_id
        self.persona   = persona
        self.rbac      = rbac_policy or {}

    # Layer 1 — agent allow-list
    def check_agent_access(self, agent_name: str) -> bool:
        access = self.rbac.get('agent_access', {})
        if access.get(agent_name) is False or access.get(agent_name) == 'denied':
            raise PermissionError(f"'{self.persona}' not authorized for {agent_name}")
        return True

    # Layer 2 — tool access + tenant isolation + SQL filter injection
    def before_tool_call(self, tool_name: str, tool_input: dict) -> dict:
        # Tenant isolation — always enforced, regardless of persona policy
        input_str = json.dumps(tool_input).lower()
        competitors = self.rbac.get('tenant_isolation', {}).get('forbidden_tenants', [])
        for comp in competitors:
            if comp in input_str and comp != self.client_id.lower():
                raise ValueError(f"Cannot access competitor data ({comp})")

        # Tool allow/deny list
        denied = self.rbac.get('tool_access', {}).get('denied', [])
        if tool_name in denied:
            # Return a sentinel dict; the tool wrapper must honour __rbac_denied
            return {'__rbac_denied': True, 'error': f'Tool {tool_name} denied for {self.persona}'}

        # Data scoping — inject a SQL WHERE clause for data-bound tools
        sql_filter = self.rbac.get('data_filter', {}).get('sql_filter', '')
        if sql_filter and 'sql' in tool_input:
            tool_input['sql'] += f' {sql_filter}'

        return tool_input

    # Layer 3 — data masking on output (PII + persona-restricted fields)
    def after_tool_call(self, tool_name: str, output: str) -> str:
        # Example PII redaction — national ID pattern
        output = re.sub(r'\b[12]\d{9}\b', '[REDACTED_NID]', output)

        # Mask restricted fields (both string and numeric values)
        for field in self.rbac.get('data_filter', {}).get('mask_fields', []):
            output = re.sub(rf'"{field}"\s*:\s*"[^"]*"', f'"{field}": "[RESTRICTED]"', output)
            output = re.sub(rf'"{field}"\s*:\s*[\d.]+',    f'"{field}": "[RESTRICTED]"', output)
        return output
```

### 3.3 Strands lifecycle hooks

```python
"""Lifecycle hooks — log, time, and intercept tool calls."""
import time
from strands import Agent
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent

agent = Agent(system_prompt="...", tools=[...])

# Log every tool call the agent makes
def log_tool(event: BeforeToolCallEvent) -> None:
    print(f"Calling tool: {event.tool_use['name']}")

agent.add_hook(log_tool)

# Measure tool latency
_timers: dict[str, float] = {}

def start_timer(event: BeforeToolCallEvent) -> None:
    _timers[event.tool_use['name']] = time.time()

def end_timer(event: AfterToolCallEvent) -> None:
    started = _timers.pop(event.tool_use['name'], time.time())
    print(f"Tool {event.tool_use['name']} took {time.time() - started:.2f}s")

agent.add_hook(start_timer)
agent.add_hook(end_timer)
```

### 3.4 Plugin pattern (bundled hooks + state)

```python
"""Plugins bundle multiple hooks with their own state."""
import time
from strands import Agent
from strands.plugins import Plugin, hook
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent


class PerformancePlugin(Plugin):
    name = "performance-plugin"

    @hook
    def start_timer(self, event: BeforeToolCallEvent) -> None:
        self._start = time.time()

    @hook
    def end_timer(self, event: AfterToolCallEvent) -> None:
        elapsed = time.time() - self._start
        print(f"Tool {event.tool_use['name']} took {elapsed:.2f}s")


agent = Agent(plugins=[PerformancePlugin()])
```

### 3.5 Circuit breaker

Prevents one failing sub-agent from pulling the whole supervisor turn down when it retries.

```python
"""Circuit breaker — open after N failures, auto-reset after reset_secs."""
import time


class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_secs: int = 60):
        self._failures     = 0
        self._last_failure = 0.0
        self._open         = False
        self._threshold    = threshold
        self._reset_secs   = reset_secs

    def check(self) -> None:
        if self._open:
            if time.time() - self._last_failure > self._reset_secs:
                # Reset window elapsed; attempt one call
                self._open = False
                self._failures = 0
            else:
                raise RuntimeError(f'Circuit breaker OPEN — {self._failures} failures')

    def record_success(self) -> None:
        self._failures = 0
        self._open     = False

    def record_failure(self) -> None:
        self._failures     += 1
        self._last_failure  = time.time()
        if self._failures >= self._threshold:
            self._open = True


# Usage inside an agent entrypoint
# circuit = CircuitBreaker()
# circuit.check()
# try:
#     result = agent(query)
#     circuit.record_success()
# except Exception:
#     circuit.record_failure()
#     raise
```

### 3.6 Gotchas

- **`check_agent_access` must be called BEFORE `Agent(...)` construction.** If called after invoke, the first tool call has already happened and the audit log records an allowed invocation.
- **The `__rbac_denied` sentinel is a convention, not a Strands protocol.** Your tool wrapper must check for it; otherwise the agent will treat the dict as a real tool result and reason over an `error` field.
- **SQL filter injection is crude.** Only safe when the tool treats `tool_input['sql']` as an already-parameterised query template. If the LLM can generate arbitrary SQL, use a DB-side row filter (RLS, Lake Formation) instead.
- **`after_tool_call` regex-mask** applies to JSON-serialized output only. Tools returning plain text bypass the mask — always `json.dumps(...)` structured results.
- **`CircuitBreaker` is process-local.** Across a Fargate service with 10 tasks you get 10 breakers, each with its own count. If you need global trip behaviour, store state in DynamoDB with a conditional `UpdateItem`.
- **Hook exceptions are not the same as tool errors.** An unhandled exception from `before_tool_call` crashes the agent turn. Catch and convert to `{'__rbac_denied': True, 'error': ...}` unless you intend the crash.
- **`Plugin.name`** must be unique across the plugin list. Duplicates silently overwrite the earlier plugin's hooks.

---

## 5. Swap matrix — hook / plugin variants

| Need | Swap |
|---|---|
| Enforce at data layer, not agent layer | Keep Layer 1 & 2 hooks; remove Layer 3 masking; use Lake Formation / Postgres RLS |
| Cross-task circuit breaker | Replace in-process `CircuitBreaker` with DynamoDB-backed counter + conditional update |
| Metrics only (no control) | Use lifecycle hooks (§3.3) without the RBAC hook class |
| Multiple policy sources | Merge policies in `__init__` (`rbac_policy = tenant_policy | persona_policy | user_policy`) |
| Per-user audit trail | Emit a structured log line in `after_tool_call` with `client_id`, `persona`, `tool_name`, input/output hashes |
| Dev bypass | Env-flag `RBAC_ENFORCE=false` → `AfieSteeringHooks` returns pass-through in every layer |

---

## 6. Worked example — RBAC + lifecycle hook roundtrip

Save as `tests/sop/test_STRANDS_HOOKS_PLUGINS.py`. Offline.

```python
"""SOP verification — steering hook denies, masks, and records circuit state."""
from shared.hooks import AfieSteeringHooks, CircuitBreaker


def test_denied_tool_returns_sentinel():
    hook = AfieSteeringHooks(
        client_id="acme",
        persona="analyst",
        rbac_policy={"tool_access": {"denied": ["run_sql_query"]}},
    )
    out = hook.before_tool_call("run_sql_query", {"sql": "SELECT 1"})
    assert out.get("__rbac_denied") is True


def test_field_mask_applied():
    hook = AfieSteeringHooks(
        client_id="acme",
        persona="analyst",
        rbac_policy={"data_filter": {"mask_fields": ["salary"]}},
    )
    masked = hook.after_tool_call("any", '{"salary": 123456, "name": "alice"}')
    assert '"salary": "[RESTRICTED]"' in masked
    assert '"name": "alice"' in masked


def test_circuit_breaker_opens_and_resets(monkeypatch):
    import time as _t
    cb = CircuitBreaker(threshold=2, reset_secs=0)  # immediate reset window
    cb.record_failure(); cb.record_failure()
    assert cb._open is True
    # Force reset window to elapse
    cb._last_failure -= 1
    cb.check()  # flips back to closed
    assert cb._open is False
```

---

## 7. References

- `docs/template_params.md` — `RBAC_POLICY_SSM_KEY`, `RBAC_ENFORCE`, `CIRCUIT_BREAKER_THRESHOLD`, `CIRCUIT_BREAKER_RESET_SECONDS`
- `docs/Feature_Roadmap.md` — feature IDs `STR-09` (hooks), `STR-10` (plugins), `SEC-12` (tenant isolation)
- Strands Hooks API: https://strandsagents.com/latest/user-guide/concepts/hooks/
- Related SOPs: `STRANDS_AGENT_CORE` (where `AfieSteeringHooks` is instantiated), `STRANDS_TOOLS` (the tool wrapper that must honour `__rbac_denied`), `LAYER_SECURITY` (policy source: Cognito / SSM / DDB), `LAYER_OBSERVABILITY` (emit hook events to CloudWatch / X-Ray)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added Gotchas (§3.6) on hook-ordering and `__rbac_denied` sentinel semantics. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — 3-layer RBAC, lifecycle hooks, plugin pattern, circuit breaker. |
