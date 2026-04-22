# SOP — Bedrock AgentCore Memory (STM, LTM, S3 Sessions, Memory Recall)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `bedrock-agentcore` Memory service (STM + LTM strategies) · Strands `S3SessionManager` · `aws_s3` · `aws_ssm`

---

## 1. Purpose

- Provision the two memory systems used together in production:
  1. **AgentCore Memory (managed)** — STM + LTM with strategies (`SUMMARY`, `USER_PREFERENCE`, `SEMANTIC`). Memory ID + config published via SSM.
  2. **`S3SessionManager` (Strands built-in)** — multi-turn conversation persistence per session, with KMS-SSE, lifecycle cleanup, and per-actor prefix scoping.
- Codify the memory-recall / memory-store round trip (`retrieve_memory_records` pre-invoke, `create_event` post-invoke) as a best-effort, non-fatal call path.
- Codify a `SessionStorage` helper for ad-hoc artifact persistence (charts, reports) within a session namespace.
- Include when the SOW mentions conversation memory, session persistence, user preferences, memory recall, or cross-session context.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| One CDK stack owns the session bucket + SSM memory config + agent roles together | **§3 Monolith Variant** |
| MS07-Memory owns the session bucket + SSM config; agent stacks read `SESSION_BUCKET` / `MEMORY_ID` via SSM at deploy time | **§4 Micro-Stack Variant** |

**Why the split matters.** Every agent role needs `s3:PutObject` / `s3:GetObject` on the session bucket and `bedrock-agentcore:RetrieveMemoryRecords` / `CreateEvent` on the memory ARN. In a monolith, `session_bucket.grant_read_write(agent_role)` works. Across stacks (bucket in MS07, agent role in an agent stack), that L2 grant edits the bucket policy in MS07 referencing the role ARN from the agent stack — circular export. The Micro-Stack variant publishes the bucket name + memory ARN via SSM and grants identity-side on the agent role.

---

## 3. Monolith Variant

**Use when:** a single stack owns session storage + memory config + agent roles.

### 3.1 Session bucket + memory config

```python
import json
import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ssm as ssm,
)


def _create_memory(self, is_prod: bool, session_kms_key: kms.IKey) -> s3.Bucket:
    """Session bucket for Strands S3SessionManager + AgentCore Memory config in SSM."""
    session_bucket = s3.Bucket(
        self, "SessionBucket",
        bucket_name=f"{{project_name}}-session-checkpoints-{Aws.ACCOUNT_ID}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=session_kms_key,                 # local to this stack
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=False,
        lifecycle_rules=[
            s3.LifecycleRule(
                id="session-cleanup",
                expiration=Duration.days(90),
                abort_incomplete_multipart_upload_after=Duration.days(1),
            ),
        ],
        removal_policy=cdk.RemovalPolicy.RETAIN if is_prod else cdk.RemovalPolicy.DESTROY,
        auto_delete_objects=not is_prod,
    )

    # Publish bucket name for agent stacks to consume via env var
    ssm.StringParameter(
        self, "SsmSessionBucket",
        parameter_name="/{project_name}/session/bucket",
        string_value=session_bucket.bucket_name,
    )
    # AgentCore Memory configuration — read at container startup
    ssm.StringParameter(
        self, "SsmMemoryConfig",
        parameter_name="/{project_name}/memory/config",
        string_value=json.dumps({
            "strategies":             ["SUMMARY", "USER_PREFERENCE", "SEMANTIC"],
            "session_ttl_hours":      24,
            "max_sessions_per_actor": 100,
        }),
    )
    self.session_bucket = session_bucket
    return session_bucket
```

### 3.2 Memory recall (pre-invoke)

```python
"""Memory recall — retrieve past context before agent invocation. Non-fatal."""
import logging
import boto3

logger           = logging.getLogger(__name__)
agentcore_client = boto3.client('bedrock-agentcore')


def recall_memory(query: str, actor_id: str, memory_id: str, top_k: int = 5) -> str:
    """Return a newline-joined string of [MEMORY] excerpts, or '' on any failure."""
    if not memory_id:
        return ''
    try:
        resp = agentcore_client.retrieve_memory_records(
            memoryId=memory_id,
            namespace=f'/summaries/{actor_id}/',
            searchCriteria={'searchQuery': query[:500], 'topK': top_k},
            maxResults=top_k,
        )
        memories = resp.get('memoryRecordSummaries', [])
        if memories:
            return '\n'.join(
                f"[MEMORY] {m.get('content', {}).get('text', '')[:300]}"
                for m in memories
            )
    except Exception as e:
        logger.warning("Memory recall failed (non-fatal): %s", e)
    return ''
```

### 3.3 Memory store (post-invoke)

```python
"""Memory store — persist conversation turn to LTM after agent response."""
from datetime import datetime, timezone


def store_memory(query: str, response: str, actor_id: str,
                 session_id: str, memory_id: str) -> None:
    """Best-effort LTM write; never raises to the caller."""
    if not memory_id:
        return
    try:
        agentcore_client.create_event(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {'conversational': {'content': {'text': query},                'role': 'USER'}},
                {'conversational': {'content': {'text': str(response)[:2000]}, 'role': 'ASSISTANT'}},
            ],
        )
    except Exception as e:
        logger.warning("Memory store failed (non-fatal): %s", e)
```

### 3.4 `S3SessionManager` wiring

```python
"""Strands S3SessionManager — multi-turn conversation persistence."""
import os
from strands import Agent
from strands.session import S3SessionManager

SESSION_BUCKET = os.environ.get('SESSION_BUCKET', '')
AWS_REGION     = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
CLIENT_ID      = os.environ.get('CLIENT_ID', '')


def create_agent_with_session(session_id: str, actor_id: str, model, system_prompt: str):
    sess_mgr = None
    if SESSION_BUCKET:
        sess_mgr = S3SessionManager(
            session_id=session_id,
            bucket=SESSION_BUCKET,
            prefix=f'sessions/{CLIENT_ID}/{actor_id}/',
            region_name=AWS_REGION,
        )
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        session_manager=sess_mgr,   # auto-persists turns to S3
    )
```

### 3.5 Artifact `SessionStorage` helper

```python
"""Persistent artifact storage — charts, reports, analysis survive across sessions."""
import os
import boto3


class SessionStorage:
    """S3-backed artifact storage scoped to client/actor/session."""

    def __init__(self, client_id: str, actor_id: str, session_id: str):
        self.bucket   = os.environ['SESSION_BUCKET']
        self._prefix  = f'session-storage/{client_id}/{actor_id}/{session_id}'
        self._s3      = boto3.client('s3')

    def write(self, path: str, content: bytes | str,
              content_type: str = 'application/octet-stream') -> dict:
        key  = f'{self._prefix}/{path.lstrip("/")}'
        body = content.encode() if isinstance(content, str) else content
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        return {'key': key, 'size_bytes': len(body)}

    def list_artifacts(self, prefix: str = 'artifacts/') -> list[dict]:
        resp = self._s3.list_objects_v2(
            Bucket=self.bucket,
            Prefix=f'{self._prefix}/{prefix}',
        )
        return [
            {'path': o['Key'].replace(f'{self._prefix}/', ''), 'size': o['Size']}
            for o in resp.get('Contents', [])
        ]

    def get_presigned_url(self, path: str, expires_in: int = 3600) -> str:
        key = f'{self._prefix}/{path.lstrip("/")}'
        return self._s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': key},
            ExpiresIn=expires_in,
        )
```

### 3.6 Monolith gotchas

- **`SESSION_BUCKET=""` (empty)** disables `S3SessionManager` — agents run statelessly. Useful in dev; explicit in prod.
- **`S3SessionManager` writes on every turn** — ~200 ms cold; pre-warm by writing one `session_id` once at container start.
- **`search_criteria={'searchQuery': ...}`** is case-sensitive on the service side for some strategies. Lowercase the query if you see empty recalls for obvious matches.
- **`max_sessions_per_actor=100`** is a cap only on the memory service's LTM retention; S3 session objects accumulate until the lifecycle rule expires them.
- **KMS on the session bucket** — if you also set `encryption_key=ext_key` in an agent stack, you hit the fifth non-negotiable (§4.1). Keep KMS grants identity-side in the agent role.

---

## 4. Micro-Stack Variant

**Use when:** MS07-Memory owns the session bucket + SSM config; agent stacks consume them read-side.

### 4.1 The five non-negotiables

1. **Anchor any Lambda asset** (e.g. a memory-cleanup Lambda) to `Path(__file__)`.
2. **Never call `session_bucket.grant_read_write(agent_role)`** across stacks. Use identity-side `PolicyStatement` on the agent role.
3. **Never target a cross-stack queue** with `targets.SqsQueue`.
4. **Never split the bucket + OAC** — the session bucket is private; no CDN.
5. **Never set `encryption_key=ext_key`** on the memory cleanup resources with a key from another stack.

### 4.2 MS07 — `MemoryStack`

```python
import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct


class MemoryStack(cdk.Stack):
    """MS07 — session bucket + AgentCore Memory SSM config + memory ARN publishing."""

    def __init__(
        self,
        scope: Construct,
        session_kms_key: kms.IKey,           # owned by MS02/MS03 (SecurityStack)
        memory_id: str,                       # written to memory once via console / out-of-band CFN
        is_prod: bool,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ms07-memory", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Session bucket — KMS key is cross-stack INTERFACE (kms.IKey).
        # We do NOT set encryption_key=key_from_another_stack — that is the
        # fifth non-negotiable from LAYER_BACKEND_LAMBDA §4.1. Instead, use
        # S3-managed encryption here and grant KMS identity-side on consumer roles.
        self.session_bucket = s3.Bucket(
            self, "SessionBucket",
            bucket_name=f"{{project_name}}-session-checkpoints-{Aws.ACCOUNT_ID}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="session-cleanup",
                    expiration=Duration.days(90),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
            ],
            removal_policy=cdk.RemovalPolicy.RETAIN if is_prod else cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=not is_prod,
        )

        # SSM publishes for agent stacks
        ssm.StringParameter(
            self, "SsmSessionBucket",
            parameter_name="/{project_name}/session/bucket",
            string_value=self.session_bucket.bucket_name,
        )
        ssm.StringParameter(
            self, "SsmMemoryId",
            parameter_name="/{project_name}/memory/id",
            string_value=memory_id,
        )
        ssm.StringParameter(
            self, "SsmMemoryConfig",
            parameter_name="/{project_name}/memory/config",
            string_value=json.dumps({
                "strategies":             ["SUMMARY", "USER_PREFERENCE", "SEMANTIC"],
                "session_ttl_hours":      24,
                "max_sessions_per_actor": 100,
            }),
        )
        CfnOutput(self, "SessionBucketName", value=self.session_bucket.bucket_name)
```

### 4.3 Identity-side grants in the per-agent stack

```python
# inside an agent stack's __init__, after creating agent_role
from aws_cdk import Aws, aws_iam as iam, aws_ssm as ssm

session_bucket_name = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/session/bucket",
)
memory_id = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/memory/id",
)

# S3 — identity-side
agent_role.add_to_policy(iam.PolicyStatement(
    actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
    resources=[f"arn:aws:s3:::{session_bucket_name}/*"],
))
agent_role.add_to_policy(iam.PolicyStatement(
    actions=["s3:ListBucket"],
    resources=[f"arn:aws:s3:::{session_bucket_name}"],
))

# AgentCore Memory — identity-side
agent_role.add_to_policy(iam.PolicyStatement(
    actions=["bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:CreateEvent"],
    resources=[f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:memory/{memory_id}"],
))

# Inject via environment variables
env = {
    "SESSION_BUCKET": session_bucket_name,
    "MEMORY_ID":      memory_id,
}
```

### 4.4 Micro-stack gotchas

- **`memory_id` creation is out-of-band.** At time of writing there is no L1 `AWS::BedrockAgentCore::Memory` CloudFormation resource. Create the memory via SDK / console once per environment, store the ID in a param passed to `MemoryStack`, and don't rotate it.
- **SSM tokens in `resources=[...]`** — IAM accepts tokenised ARN segments (e.g. `memory/{memory_id}`). At deploy time it resolves; at synth time the policy doc shows `${Token[…]}`.
- **`S3_MANAGED` encryption** in MS07 avoids the fifth non-negotiable. If you *must* use a customer-managed key for the session bucket, either:
  - Declare that key in MS07 itself (same stack), or
  - Use `kms.Key.from_key_arn` to re-materialise the key as an interface — but this still risks a cross-stack resource policy edit unless the key is AWS-owned.
- **Memory service throttling** — `retrieve_memory_records` is ~50 TPS account-wide. High-QPS synthesizers need client-side rate limiting or batching.
- **`$connect` Cognito claims** are the canonical actor ID — never accept `actor_id` from the client payload without re-validating against the session JWT.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack (MS07 owns session bucket + memory SSM) |
| Memory not required for the SOW | Leave `MEMORY_ID=''` in SSM — `recall_memory` / `store_memory` become no-ops |
| Stateless agents (no multi-turn) | `SESSION_BUCKET=''` disables `S3SessionManager`; strategies still fire via memory API |
| Cross-account session replay | Add a cross-account S3 replication rule; KMS key must be multi-account |
| > 100 MB of session state per actor | Shift chart payloads to `SessionStorage.write(..., artifacts/…)` rather than the S3SessionManager blob |
| HIPAA/PCI | Use customer-managed KMS on the session bucket (declare CMK in MS07 to stay on-stack with the bucket) |

---

## 6. Worked example — MS07 memory stack synthesizes

Save as `tests/sop/test_AGENTCORE_MEMORY.py`. Offline.

```python
"""SOP verification — MS07 creates the session bucket + 3 SSM params."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam, aws_kms as kms
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ms07_memory_stack():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    key  = kms.Key(deps, "SessionKey")
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ms07_memory import MemoryStack
    ms07 = MemoryStack(
        app,
        session_kms_key=key,
        memory_id="memory-abc123",
        is_prod=False,
        permission_boundary=boundary,
        env=env,
    )

    template = Template.from_stack(ms07)
    template.resource_count_is("AWS::S3::Bucket",    1)
    template.resource_count_is("AWS::SSM::Parameter", 3)
    template.has_resource_properties("AWS::S3::Bucket", {
        "BucketEncryption": {
            "ServerSideEncryptionConfiguration": [
                {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
            ],
        },
    })
```

---

## 7. References

- `docs/template_params.md` — `SESSION_BUCKET_SSM_NAME`, `MEMORY_ID_SSM_NAME`, `MEMORY_STRATEGIES`, `SESSION_TTL_HOURS`
- `docs/Feature_Roadmap.md` — feature IDs `AG-06` (memory), `AG-07` (sessions), `S-03` (S3 encryption)
- AgentCore Memory API: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-memory.html
- Strands `S3SessionManager`: https://strandsagents.com/latest/user-guide/concepts/sessions/
- Related SOPs: `AGENTCORE_RUNTIME` (agents that consume memory), `AGENTCORE_IDENTITY` (agent-role grants), `AGENTCORE_AGENT_CONTROL` (actor/persona scoping), `LAYER_SECURITY` (customer-managed KMS for regulated workloads), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MS07 MemoryStack publishes session bucket name + memory ID via SSM; agent stacks read via `value_for_string_parameter` and grant identity-side. Moved away from KMS-CMK cross-stack (fifth non-negotiable) by using `S3_MANAGED` in MS07; CMK path documented as explicit trade-off. Translated CDK from TypeScript to Python. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — memory stack (TS), recall / store helpers, S3SessionManager, SessionStorage. |
