# PARTIAL: Strands Session & Conversation Management

**Usage:** Include when SOW mentions session persistence, conversation history, sliding window, S3 sessions, DynamoDB sessions, or conversation management.

---

## Session & Conversation Overview

```
Session Managers (where conversation is stored):
  - S3SessionManager: Store sessions in S3 (scalable, serverless)
  - FileSessionManager: Local filesystem (dev/testing)
  - RepositorySessionManager: Custom backend via SessionRepository
  - AgentCoreMemorySessionManager: AgentCore Memory (see AGENTCORE_MEMORY.md)

Conversation Managers (how history is trimmed):
  - SlidingWindowConversationManager: Keep last N messages
  - SummarizingConversationManager: Summarize old messages
  - NullConversationManager: No trimming (unlimited context)
```

---

## CDK Code Block — Session Storage Infrastructure

```python
def _create_agent_session_storage(self, stage_name: str) -> None:
    """
    Agent session storage infrastructure.

    Components:
      A) DynamoDB table for agent sessions (conversation history)
      B) S3 bucket for session persistence (alternative to DynamoDB)

    [Claude: include A for DynamoDB-based sessions (default).
     Include B if SOW mentions S3 session storage.]
    """

    # =========================================================================
    # A) DYNAMODB — Agent Session Table
    # =========================================================================

    self.agent_session_table = ddb.Table(
        self, "AgentSessionTable",
        table_name=f"{{project_name}}-agent-sessions-{stage_name}",
        partition_key=ddb.Attribute(name="session_id", type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name="turn_id", type=ddb.AttributeType.NUMBER),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery=True,
        encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=self.kms_key,
        time_to_live_attribute="ttl",
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
    )
    self.agent_session_table.add_global_secondary_index(
        index_name="actor-sessions-idx",
        partition_key=ddb.Attribute(name="actor_id", type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
        projection_type=ddb.ProjectionType.ALL,
    )
    self.agent_session_table.grant_read_write_data(self.agentcore_runtime_role)
```

---

## Session Manager Patterns — Pass 3 Reference

### S3 Session Manager

```python
"""S3-based session persistence."""
from strands import Agent
from strands.session.s3_session_manager import S3SessionManager

session_mgr = S3SessionManager(
    bucket_name=os.environ["SESSION_BUCKET"],
    region_name="us-east-1",
)

agent = Agent(
    system_prompt="You are helpful.",
    session_manager=session_mgr,
    session_id="user-123-session-abc",
)
agent("Hello")  # Session auto-persisted to S3
```

### File Session Manager (dev/testing)

```python
"""Local file-based sessions for development."""
from strands.session.file_session_manager import FileSessionManager

session_mgr = FileSessionManager(base_dir="./sessions")
agent = Agent(session_manager=session_mgr, session_id="dev-session-1")
```

---

## Conversation Manager Patterns — Pass 3 Reference

### Sliding Window (keep last N messages)

```python
"""Trim conversation to last N messages."""
from strands import Agent
from strands.agent.conversation_manager.sliding_window_conversation_manager import (
    SlidingWindowConversationManager,
)

agent = Agent(
    system_prompt="You are helpful.",
    conversation_manager=SlidingWindowConversationManager(window_size=20),
)
```

### Summarizing (compress old messages)

```python
"""Summarize old messages to save context window."""
from strands.agent.conversation_manager.summarizing_conversation_manager import (
    SummarizingConversationManager,
)

agent = Agent(
    system_prompt="You are helpful.",
    conversation_manager=SummarizingConversationManager(
        summary_threshold=15,  # Summarize when > 15 messages
    ),
)
```
