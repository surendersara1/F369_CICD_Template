# SOP — Strands Agent Frontend (WebSocket Streaming, Portal, Callback Handler)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · API Gateway v2 (WebSocket) · Lambda Python 3.13 · `apigatewaymanagementapi` · React SPA with JWT-auth handshake · AWS CDK v2

---

## 1. Purpose

- Codify the real-time streaming portal architecture: React SPA ↔ API Gateway WebSocket ↔ Lambda ↔ AgentCore Runtime.
- Codify the **`WebSocketCallbackHandler`** — the real production streaming pattern, which pushes *actual* agent reasoning events (tool-use start, reasoning text, token batches) rather than simulated progress.
- Provide the WebSocket `$message` handler Lambda that invokes the supervisor agent and relays the final response.
- Explicitly link to the CDK for the underlying WebSocket API, routes, connection-table, and auth — which belongs in `LAYER_API` / `LAYER_FRONTEND`, not duplicated here.
- Include when the SOW mentions agent chat UI, real-time streaming, WebSocket portal, or conversational interface.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split on the callback-handler code** (it is Python inside the agent container). §3 is the canonical variant for the handler.
>
> For the CDK resources (WebSocket API, `$connect`/`$message`/`$disconnect` integrations, connection DynamoDB table, Cognito authorizer, `execute-api:ManageConnections` grant), the **dual-variant** rules in `LAYER_API §3 / §4` (monolith vs micro-stack) apply. When the WebSocket API is in one stack and the message-handler Lambda is in another, the `ManageConnections` grant must be **identity-side** on the Lambda's execution role — no `apigwv2.grant_*(fn)` across stacks.

§4 Micro-Stack Variant is intentionally omitted for the callback-handler code; see §7 for the deploy references.

---

## 3. Canonical Variant

### 3.1 Frontend architecture

```
Portal Stack:

  React SPA ──WebSocket──▶ API GW v2 ──▶ Lambda ($message) ──▶ invoke_agent_runtime()
      │                         │                │
      │                         │                └─▶ Supervisor on AgentCore Runtime
      │                         │                        │
      │  $connect / $msg /      │                        └─▶ Strands Agent
      │  $disconnect routes     │                              └─▶ WebSocketCallbackHandler
      │                         │                                    │
      │◀────────────────────────┴──────────── post_to_connection() ◀─┘
      │
      ├─ real-time progress steps (tool-use, reasoning)
      ├─ confidence scores  (from STRANDS_EVAL)
      ├─ presigned URLs for charts  (from STRANDS_TOOLS code interpreter)
      └─ final response  (type=response)

Auth: Cognito JWT validated at $connect. ConnectionId persisted in DDB table.
REST fallback: API GW v1 for clients that cannot hold a WebSocket open.
```

### 3.2 Callback handler (streaming to WebSocket)

```python
"""WebSocket streaming callback — pushes REAL agent events to the portal."""
import json, time
import boto3


class WebSocketCallbackHandler:
    """Strands callback_handler — receives streaming events, posts to WebSocket."""

    TOOL_AGENT_MAP = {
        'observer_agent':         'Observer',
        'reasoner_agent':         'Reasoner',
        'governance_agent':       'Governance',
        'run_financial_analysis': 'Code Interpreter',
        # [Claude: map tool names to display labels from SOW]
    }

    def __init__(self, connection_id: str, ws_endpoint: str):
        self.connection_id = connection_id
        self.ws_endpoint   = ws_endpoint
        self._apigw        = boto3.client(
            'apigatewaymanagementapi', endpoint_url=ws_endpoint,
        )
        self._step_count = 0
        self._start_time = time.time()

    # Strands calls this for every streaming event
    def __call__(self, **kwargs):
        event = kwargs.get('event', {})

        # Tool-use start — high-value event for the UI
        tool_use = event.get('contentBlockStart', {}).get('start', {}).get('toolUse')
        if tool_use:
            self._step_count += 1
            tool_name = tool_use.get('name', 'unknown')
            self._post({
                'type':       'progress',
                'agent':      self.TOOL_AGENT_MAP.get(tool_name, 'Agent'),
                'detail':     f'Calling {tool_name}...',
                'step':       self._step_count,
                'elapsed_ms': int((time.time() - self._start_time) * 1000),
            })

        # Reasoning (CoT) — trim to 200 chars for UI
        reasoning = kwargs.get('reasoningText', '')
        if reasoning:
            self._post({
                'type':   'progress',
                'agent':  'Supervisor',
                'phase':  'Reasoning',
                'detail': reasoning[:200],
            })

        # LLM text tokens — batch to reduce WS chatter
        data = kwargs.get('data', '')
        if data and len(data) > 50:
            self._post({'type': 'token', 'text': data})

    def _post(self, data: dict) -> None:
        """Never raise — a closed WebSocket must not kill the agent turn."""
        try:
            self._apigw.post_to_connection(
                ConnectionId=self.connection_id,
                Data=json.dumps(data).encode(),
            )
        except Exception:
            # GoneException is expected when the client disconnects mid-turn
            pass

    def send_custom_step(self, agent: str, phase: str, detail: str) -> None:
        """Emit a custom progress step (pre/post agent phases, gate events)."""
        self._step_count += 1
        self._post({
            'type':   'progress',
            'agent':  agent,
            'phase':  phase,
            'detail': detail,
            'step':   self._step_count,
        })
```

### 3.3 WebSocket `$message` handler Lambda

```python
"""$message handler — invoke supervisor agent, relay final response to the WS."""
import os, json, uuid
import boto3

agentcore_client = boto3.client('bedrock-agentcore')


def handler(event, context):
    connection_id = event['requestContext']['connectionId']
    body          = json.loads(event.get('body', '{}'))
    query         = body.get('message',    '')
    session_id    = body.get('session_id', str(uuid.uuid4()))
    role          = body.get('role',       'user')

    supervisor_arn = os.environ['SUPERVISOR_ARN']
    ws_endpoint    = os.environ['WS_ENDPOINT']   # wss://… not https://…

    # Invoke the supervisor on its AgentCore Runtime.
    # The supervisor opens its OWN WebSocket back to the client via
    # WebSocketCallbackHandler — we only relay the final response here.
    resp = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=supervisor_arn,
        contentType='application/json',
        payload=json.dumps({
            'prompt':           query,
            'role':             role,
            'runtimeSessionId': session_id,
            'connectionId':     connection_id,
            'wsEndpoint':       ws_endpoint,
        }).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=session_id,
    )
    result = json.loads(resp.get('response').read().decode('utf-8'))

    # Final response — separate frame from the streaming progress events
    mgmt = boto3.client('apigatewaymanagementapi', endpoint_url=ws_endpoint)
    mgmt.post_to_connection(
        ConnectionId=connection_id,
        Data=json.dumps({'type': 'response', 'data': result}).encode(),
    )

    return {'statusCode': 200}
```

### 3.4 Wiring in the supervisor

```python
"""Use the callback only when the user is on a WebSocket."""
callbacks = []
if connection_id and ws_endpoint:
    ws_cb = WebSocketCallbackHandler(connection_id, ws_endpoint)
    callbacks.append(ws_cb)
    # Pre-synthesis banner events (not agent-driven)
    ws_cb.send_custom_step('Supervisor', 'RBAC',  f'Loaded {actor_id} policy')
    ws_cb.send_custom_step('Supervisor', 'Model', f'Using {model_id}')

synthesizer = Agent(
    model=model,
    tools=[...],
    system_prompt=SYSTEM_PROMPT,
    callback_handler=callbacks[0] if callbacks else None,
    session_manager=sess_mgr,
)
```

### 3.5 CDK (reference — defined in `LAYER_API` / `LAYER_FRONTEND`)

The WebSocket API, routes, connection table, and authorizer belong in the API layer. Keep the following shape in mind when reading those SOPs:

```python
from aws_cdk import aws_apigatewayv2 as apigwv2, aws_iam as iam, Aws, Duration
from aws_cdk import aws_lambda as _lambda

ws_api = apigwv2.CfnApi(self, "AgentWSApi",
    name="{project_name}-agent-ws",
    protocol_type="WEBSOCKET",
    route_selection_expression="$request.body.action",
)

ws_message_fn = _lambda.Function(self, "WSMessageFn",
    function_name="{project_name}-ws-message",
    runtime=_lambda.Runtime.PYTHON_3_13,
    handler="handler.handler",
    code=_lambda.Code.from_asset("lambda/websocket_handler"),
    timeout=Duration.minutes(15),
    memory_size=512,
)

# Identity-side grant — safe even if the WebSocket API is in another stack
ws_message_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["execute-api:ManageConnections"],
    resources=[f"arn:aws:execute-api:{Aws.REGION}:{Aws.ACCOUNT_ID}:{ws_api.ref}/*"],
))
```

### 3.6 Gotchas

- **Token flooding** — Bedrock emits hundreds of small `data` events per second. Posting each to the WebSocket throttles API Gateway (≈ 500 msg/s per connection) and overwhelms the client. The handler batches on `len(data) > 50`; tune per-SOW.
- **`GoneException` on `post_to_connection`** is routine — the client closed the socket. Catch broadly; do not let it abort the agent turn.
- **Two endpoint URLs** — `ws_endpoint` in `WebSocketCallbackHandler` is the API GW **management** URL (`https://{api-id}.execute-api.{region}.amazonaws.com/{stage}`), NOT the connect URL (`wss://…`). Getting this wrong silently fails with 403.
- **15-minute Lambda timeout** on `$message` is the ceiling for an agent turn. Long-running synthesis (code interpreter + big data) must stream and be prepared to finish via a separate push if the user reconnects.
- **Cognito JWT at `$connect`** is not revalidated on every `$message`. If a user's role changes mid-session, the agent still sees the old persona until disconnect. Refresh RBAC from DDB per turn if latency allows.
- **ConnectionId reuse** across supervisor + sub-agent WebSocket posters can race. Pass the connection ID through `invocation_state` to keep ordering consistent; do not spawn handlers in sub-agents' threads.
- **Presigned URLs for charts expire in 1 h** (`STRANDS_TOOLS §3.3`) — the UI must tolerate 403 and ask for a refresh or render a stale-asset banner.

---

## 5. Swap matrix — UI / streaming variants

| Need | Swap |
|---|---|
| No streaming (REST only) | Omit `callback_handler`; return the final response synchronously via HTTP |
| Long synthesis, client may disconnect | Persist partial results to DDB keyed by `session_id`; allow reconnect to fetch |
| Server-side chat history | `S3SessionManager` (see `STRANDS_AGENT_CORE §3.3`) + DDB connection-to-session index |
| Multiple concurrent agents → same UI | Tag every WS frame with `agent_id`; UI filters by current agent |
| Push from sub-agents too | Pass `(connection_id, ws_endpoint)` via `invocation_state`; sub-agent constructs its own handler |
| IAM-auth WebSocket (no Cognito) | API GW v2 `AWS_IAM` authorizer on `$connect`; frontend signs requests with SigV4 |
| Reduce message volume | Raise batch threshold (`len(data) > 100`); drop reasoning events in prod |

---

## 6. Worked example — handler handles GoneException

Save as `tests/sop/test_STRANDS_FRONTEND.py`. Offline.

```python
"""SOP verification — callback does not raise when WebSocket is closed."""
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError


def test_callback_suppresses_gone_exception():
    with patch('boto3.client') as mock_client:
        apigw = MagicMock()
        apigw.post_to_connection.side_effect = ClientError(
            {'Error': {'Code': 'GoneException', 'Message': 'connection gone'}},
            'PostToConnection',
        )
        mock_client.return_value = apigw

        from shared.ws_callback import WebSocketCallbackHandler
        cb = WebSocketCallbackHandler("conn-123", "https://api.execute-api.us-east-1.amazonaws.com/prod")

        # Must NOT raise even when the WebSocket is closed
        cb.send_custom_step("Supervisor", "RBAC", "ok")


def test_callback_batches_small_token_events():
    with patch('boto3.client') as mock_client:
        apigw = MagicMock()
        mock_client.return_value = apigw

        from shared.ws_callback import WebSocketCallbackHandler
        cb = WebSocketCallbackHandler("conn-123", "https://x")

        # A very small 'data' token should NOT be posted
        cb(data="hi")
        assert apigw.post_to_connection.call_count == 0

        # A token over the 50-char threshold should be posted
        cb(data="x" * 60)
        assert apigw.post_to_connection.call_count == 1
```

---

## 7. References

- `docs/template_params.md` — `SUPERVISOR_ARN`, `WS_ENDPOINT`, `WS_CONNECTION_TABLE`, `FRONTEND_DOMAIN`
- `docs/Feature_Roadmap.md` — feature IDs `STR-12` (streaming UI), `FE-14` (portal), `AP-12` (WebSocket routes)
- API Gateway v2 WebSocket APIs: https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-websocket-api.html
- `apigatewaymanagementapi.post_to_connection`: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/apigatewaymanagementapi.html
- Related SOPs: `STRANDS_AGENT_CORE §3.7` (callback handler origin), `LAYER_API` (WebSocket API + routes), `LAYER_FRONTEND` (React SPA + CloudFront), `LAYER_SECURITY` (Cognito authorizer, JWT), `AGENTCORE_RUNTIME` (supervisor hosting), `STRANDS_EVAL` (confidence object projected to UI)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant for the handler code; deferred CDK dual-variant to `LAYER_API`. Added Gotchas (§3.6) on token flooding, `GoneException`, dual endpoint URLs, and JWT refresh. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — WebSocket API CDK, `WebSocketCallbackHandler`, `$message` handler, supervisor wiring. |
