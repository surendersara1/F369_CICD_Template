# PARTIAL: Strands Hooks & Plugins — Lifecycle Events, Extensibility

**Usage:** Include when SOW mentions agent hooks, lifecycle callbacks, tool interception, agent plugins, custom logging, or agent extensibility.

---

## Hooks Overview

```
Hooks = Composable event callbacks for agent lifecycle:
  - BeforeInvocationEvent / AfterInvocationEvent
  - BeforeModelCallEvent / AfterModelCallEvent
  - BeforeToolCallEvent / AfterToolCallEvent
  - MessageAddedEvent
  - Multi-agent: BeforeNodeCallEvent / AfterNodeCallEvent

Plugins = Bundled hooks + tools + config (reusable packages)

Hook Lifecycle:
  BeforeInvocation → [BeforeModelCall → AfterModelCall →
    BeforeToolCall → AfterToolCall]* → AfterInvocation
```

---

## Hook Registration — Pass 3 Reference

```python
"""Register hooks on agent lifecycle events."""
from strands import Agent
from strands.hooks import (
    BeforeInvocationEvent,
    AfterInvocationEvent,
    BeforeToolCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    AfterModelCallEvent,
    MessageAddedEvent,
)

agent = Agent(system_prompt="You are helpful.", tools=[...])

# Type-inferred registration (recommended)
def log_tool_call(event: BeforeToolCallEvent) -> None:
    print(f"Calling tool: {event.tool_use['name']}")

def log_tool_result(event: AfterToolCallEvent) -> None:
    print(f"Tool completed: {event.tool_use['name']}")

agent.add_hook(log_tool_call)   # Event type inferred from type hint
agent.add_hook(log_tool_result)

# Explicit event type registration
def on_start(event: BeforeInvocationEvent) -> None:
    print("Agent invocation started")

agent.add_hook(on_start, BeforeInvocationEvent)
```

---

## Plugin Pattern — Pass 3 Reference

```python
"""Plugins bundle multiple hooks with configuration."""
from strands import Agent
from strands.plugins import Plugin, hook
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent
import time

class PerformancePlugin(Plugin):
    """Track tool execution latency and log all tool calls."""
    name = "performance-plugin"

    def __init__(self):
        self._timers = {}

    @hook
    def start_timer(self, event: BeforeToolCallEvent) -> None:
        self._timers[event.tool_use['name']] = time.time()

    @hook
    def end_timer(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use['name']
        elapsed = time.time() - self._timers.pop(name, time.time())
        print(f"Tool {name} took {elapsed:.2f}s")

class GuardrailPlugin(Plugin):
    """Block specific tool calls based on rules."""
    name = "guardrail-plugin"

    @hook
    def check_tool(self, event: BeforeToolCallEvent) -> None:
        blocked = ["shell", "file_write"]
        if event.tool_use['name'] in blocked:
            event.tool_use['input'] = {}  # Clear input to block
            print(f"BLOCKED tool: {event.tool_use['name']}")

# Use plugins with agent
agent = Agent(
    system_prompt="You are helpful.",
    tools=[...],
    plugins=[PerformancePlugin(), GuardrailPlugin()],
)
```

---

## Multi-Agent Hooks

```python
"""Hooks for multi-agent orchestrators (Graph, Swarm)."""
from strands.hooks import BeforeNodeCallEvent, AfterNodeCallEvent

# Register on orchestrator
orchestrator.hooks.add_callback(BeforeNodeCallEvent, lambda e: print(f"Node starting: {e}"))
orchestrator.hooks.add_callback(AfterNodeCallEvent, lambda e: print(f"Node completed: {e}"))
```

---

## Tool Interception (Advanced)

```python
"""Intercept and modify tool calls before execution."""
from strands.hooks import BeforeToolCallEvent

def inject_tenant_scope(event: BeforeToolCallEvent) -> None:
    """Add tenant_id to all database tool calls."""
    if event.tool_use['name'] == 'query_database':
        event.tool_use['input']['tenant_filter'] = "tenant-123"

agent.add_hook(inject_tenant_scope)
```
