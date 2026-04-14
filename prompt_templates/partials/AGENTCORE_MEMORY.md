# PARTIAL: AgentCore Memory — Short-Term & Long-Term Memory

**Usage:** Include when SOW mentions AgentCore Memory, conversation persistence, STM/LTM, user preferences, session continuity, or semantic memory.

---

## AgentCore Memory Overview

```
AgentCore Memory = Managed memory service for Strands agents:
  - Short-Term Memory (STM): Conversation persistence within a session
  - Long-Term Memory (LTM): Cross-session learning with strategies
    - SUMMARY: Summarize conversations for context continuity
    - USER_PREFERENCE: Remember user preferences across sessions
    - SEMANTIC: Semantic memory for knowledge retention
  - Namespace-based retrieval for scoped memory access
  - Message batching for performance optimization
  - Context manager pattern for automatic cleanup

Memory Flow:
  Agent invocation → SessionManager.start_session()
       ↓
  Agent processes messages (auto-persisted to STM)
       ↓
  SessionManager.end_session() → LTM strategies extract & store
       ↓
  Next session → STM restored + LTM context injected
```

---

## CDK Code Block — AgentCore Memory Configuration

```python
def _create_agentcore_memory(self, stage_name: str) -> None:
    """
    AgentCore Memory configuration.

    Components:
      A) Memory configuration (SSM parameter for runtime)
      B) IAM permissions for memory API access

    [Claude: include when SOW mentions conversation memory, user preferences,
     session continuity, or long-term learning. Memory resource is created
     via SDK/CLI, not CDK — CDK provides supporting config.]
    """

    # =========================================================================
    # A) MEMORY CONFIG — SSM Parameter
    # =========================================================================

    ssm.StringParameter(
        self, "AgentCoreMemoryConfig",
        parameter_name=f"/{{project_name}}/{stage_name}/agentcore/memory-config",
        string_value=json.dumps({
            "memory_id": f"{{project_name}}-memory-{stage_name}",
            "strategies": [
                {"type": "SUMMARY", "description": "Summarize conversation for context"},
                {"type": "USER_PREFERENCE", "description": "Remember user preferences"},
                {"type": "SEMANTIC", "description": "Semantic knowledge retention"},
            ],
            "session_config": {
                "session_ttl_hours": 24,
                "max_sessions_per_actor": 100,
                "batch_size": 5,
            },
        }),
        description="AgentCore Memory configuration (STM + LTM strategies)",
    )

    # =========================================================================
    # B) IAM — Memory API Access
    # =========================================================================

    self.agentcore_runtime_role.add_to_policy(
        iam.PolicyStatement(
            sid="AgentCoreMemoryAccess",
            actions=[
                "bedrock-agentcore:CreateMemory",
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:UpdateMemory",
                "bedrock-agentcore:DeleteMemory",
                "bedrock-agentcore:CreateSession",
                "bedrock-agentcore:GetSession",
            ],
            resources=["*"],
        )
    )
```

---

## Memory Setup Script — Pass 3 Reference

```python
"""One-time memory resource creation (run separately from agent app)."""
from bedrock_agentcore.memory import MemoryClient

client = MemoryClient(region_name="us-east-1")

# Create memory with LTM strategies
memory = client.create_memory(
    name="{{project_name}}-memory",
    description="Agent memory for {{project_name}}",
    memory_strategies=[
        {"type": "SUMMARY"},
        {"type": "USER_PREFERENCE"},
        {"type": "SEMANTIC"},
    ],
)
print(f"Memory ID: {memory['id']}")
# Export: os.environ['AGENTCORE_MEMORY_ID'] = memory['id']
```

---

## Agent with Memory — Pass 3 Reference

```python
"""Strands agent with AgentCore Memory (STM + LTM)."""
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
import os

MEM_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")

def create_agent_with_memory(session_id: str, actor_id: str):
    """Create agent with AgentCore Memory using context manager pattern."""
    config = AgentCoreMemoryConfig(
        memory_id=MEM_ID,
        session_id=session_id,
        actor_id=actor_id,
    )

    # Context manager ensures messages are flushed on exit
    session_manager = AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )

    model = BedrockModel(
        model_id=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
    )

    agent = Agent(
        model=model,
        system_prompt="You are a helpful assistant. Use all you know about the user.",
        session_manager=session_manager,
        tools=[],  # [Claude: add tools from SOW]
    )
    return agent, session_manager


# Usage with context manager (recommended):
# with AgentCoreMemorySessionManager(...) as sm:
#     agent = Agent(session_manager=sm, ...)
#     agent("Hello")

# Usage with explicit lifecycle:
# agent, sm = create_agent_with_memory(session_id, actor_id)
# sm.start_session(agent)
# response = agent("Hello")
# sm.end_session(agent)
```

---

## LTM Retrieval with Namespaces — Pass 3 Reference

```python
"""Advanced LTM retrieval with namespace filtering."""
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig, RetrievalConfig
)

# Single namespace retrieval
config = AgentCoreMemoryConfig(
    memory_id=MEM_ID,
    session_id=session_id,
    actor_id=actor_id,
    retrieval_config=RetrievalConfig(
        namespace_pattern="user_preference",
        max_results=10,
    ),
)

# Multiple namespace retrieval
config = AgentCoreMemoryConfig(
    memory_id=MEM_ID,
    session_id=session_id,
    actor_id=actor_id,
    retrieval_config=RetrievalConfig(
        namespace_pattern="user_preference|semantic",
        max_results=20,
    ),
)
```
