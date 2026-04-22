# PARTIAL: AgentCore Memory — STM, LTM, S3 Sessions, Memory Recall

**Usage:** Include when SOW mentions conversation memory, session persistence, user preferences, memory recall, or cross-session context.

---

## AgentCore Memory Overview

```
Two memory systems work together:
  1. AgentCore Memory (managed): STM + LTM with strategies (SUMMARY, USER_PREFERENCE, SEMANTIC)
  2. S3 SessionManager (Strands built-in): Multi-turn conversation persistence per session

Memory Flow (from real production):
  Agent invocation
    ↓
  Memory Recall: retrieve_memory_records(query) → inject past context
    ↓
  Agent processes messages (S3 SessionManager auto-persists turns)
    ↓
  Memory Store: create_event(payload) → LTM strategies extract & store
    ↓
  Next session → Memory Recall retrieves relevant past context
```

---

## CDK Code Block — Memory Stack

```typescript
// infra/lib/stacks/ms-07-agentcore-memory-stack.ts

// S3 bucket for Strands S3SessionManager (multi-turn conversation persistence)
const sessionBucket = new s3.Bucket(this, 'SessionBucket', {
  bucketName: `{{project_name}}-session-checkpoints-${cdk.Aws.ACCOUNT_ID}`,
  encryption: s3.BucketEncryption.S3_MANAGED,
  blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  enforceSSL: true,
  lifecycleRules: [{ id: 'session-cleanup', expiration: cdk.Duration.days(90) }],
  removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
  autoDeleteObjects: !isProd,
});

ssmPut(this, 'SsmSessionBucket', '/{{project_name}}/session/bucket', sessionBucket.bucketName);

// AgentCore Memory configuration (SSM for runtime)
ssmPut(this, 'SsmMemoryConfig', '/{{project_name}}/memory/config', JSON.stringify({
  strategies: ['SUMMARY', 'USER_PREFERENCE', 'SEMANTIC'],
  session_ttl_hours: 24,
  max_sessions_per_actor: 100,
}));
```

---

## Memory Recall Pattern — Pass 3 Reference

```python
"""Memory recall — retrieve past context before agent invocation."""
import boto3

agentcore_client = boto3.client('bedrock-agentcore')
MEMORY_ID = ssm_get('/{{project_name}}/memory/id', '')

def recall_memory(query: str, actor_id: str, top_k: int = 5) -> str:
    """Retrieve relevant past memories for context injection."""
    if not MEMORY_ID:
        return ''
    try:
        resp = agentcore_client.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=f'/summaries/{actor_id}/',
            searchCriteria={'searchQuery': query[:500], 'topK': top_k},
            maxResults=top_k,
        )
        memories = resp.get('memoryRecordSummaries', [])
        if memories:
            return '\n'.join([
                f"[MEMORY] {m.get('content', {}).get('text', '')[:300]}"
                for m in memories
            ])
    except Exception as e:
        logger.warning("Memory recall failed (non-fatal): %s", e)
    return ''
```

---

## Memory Store Pattern — Pass 3 Reference

```python
"""Memory store — persist conversation to LTM after agent response."""

def store_memory(query: str, response: str, actor_id: str, session_id: str):
    """Store conversation turn to AgentCore Memory for LTM extraction."""
    if not MEMORY_ID:
        return
    try:
        agentcore_client.create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {'conversational': {'content': {'text': query}, 'role': 'USER'}},
                {'conversational': {'content': {'text': str(response)[:2000]}, 'role': 'ASSISTANT'}},
            ],
        )
    except Exception as e:
        logger.warning("Memory store failed (non-fatal): %s", e)
```

---

## S3 SessionManager (Strands built-in) — Pass 3 Reference

```python
"""S3 SessionManager — multi-turn conversation persistence."""
from strands import Agent
from strands.session import S3SessionManager

SESSION_BUCKET = os.environ.get('SESSION_BUCKET', '')

def create_agent_with_session(session_id: str, actor_id: str):
    sess_mgr = None
    if SESSION_BUCKET:
        sess_mgr = S3SessionManager(
            session_id=session_id,
            bucket=SESSION_BUCKET,
            prefix=f'sessions/{{client_id}}/{actor_id}/',
            region_name=AWS_REGION,
        )

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[...],
        session_manager=sess_mgr,  # Auto-persists conversation turns
    )
    return agent
```

---

## Session Storage (Artifact Persistence) — Pass 3 Reference

```python
"""Persistent artifact storage — charts, reports, analysis survive across sessions."""
import boto3, os, uuid, time

class SessionStorage:
    """S3-backed artifact storage scoped to client/actor/session."""

    def __init__(self, client_id: str, actor_id: str, session_id: str):
        self.bucket = os.environ.get('SESSION_BUCKET', '')
        self._prefix = f'session-storage/{client_id}/{actor_id}/{session_id}'
        self._s3 = boto3.client('s3')

    def write(self, path: str, content: bytes | str, content_type: str = 'application/octet-stream') -> dict:
        key = f'{self._prefix}/{path.lstrip("/")}'
        body = content.encode() if isinstance(content, str) else content
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        return {'key': key, 'size_bytes': len(body)}

    def list_artifacts(self, prefix: str = 'artifacts/') -> list[dict]:
        resp = self._s3.list_objects_v2(Bucket=self.bucket, Prefix=f'{self._prefix}/{prefix}')
        return [{'path': o['Key'].replace(f'{self._prefix}/', ''), 'size': o['Size']}
                for o in resp.get('Contents', [])]

    def get_presigned_url(self, path: str, expires_in: int = 3600) -> str:
        key = f'{self._prefix}/{path.lstrip("/")}'
        return self._s3.generate_presigned_url('get_object',
            Params={'Bucket': self.bucket, 'Key': key}, ExpiresIn=expires_in)
```
