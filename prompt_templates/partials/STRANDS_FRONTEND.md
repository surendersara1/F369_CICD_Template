# PARTIAL: Strands Agent Frontend — WebSocket Streaming, Portal, Callback Handler

**Usage:** Include when SOW mentions agent chat UI, real-time streaming, WebSocket portal, or conversational interface.

---

## Frontend Architecture (from real production)

```
Portal Stack:
  React SPA → WebSocket (API GW v2) → Lambda → invoke_agent_runtime()
       ↓              ↓                    ↓
  Real-time progress  $connect/$msg/$disconnect  Agent streams back via
  steps from agent    Connection DynamoDB table   WebSocketCallbackHandler
  Confidence scores   Cognito JWT auth            post_to_connection()
  Chart rendering     REST fallback (API GW v1)   Presigned URL charts

Key: Agent pushes REAL reasoning steps to portal via callback_handler,
     not simulated progress messages.
```

---

## CDK: WebSocket API — Pass 2A Reference

```typescript
// WebSocket API (API Gateway v2)
const wsApi = new apigwv2.CfnApi(this, 'AgentWSApi', {
  name: `{{project_name}}-agent-ws`,
  protocolType: 'WEBSOCKET',
  routeSelectionExpression: '$request.body.action',
});

// WebSocket handlers
const wsMessageFn = new lambda.Function(this, 'WSMessageFn', {
  functionName: `{{project_name}}-ws-message`,
  runtime: lambda.Runtime.PYTHON_3_13,
  handler: 'handler.handler',
  code: lambda.Code.fromAsset('lambda/websocket_handler'),
  timeout: cdk.Duration.minutes(15),
  memorySize: 512,
});

// Grant WebSocket management API access
wsMessageFn.addToRolePolicy(new iam.PolicyStatement({
  actions: ['execute-api:ManageConnections'],
  resources: [`arn:aws:execute-api:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:${wsApi.ref}/*`],
}));
```

---

## WebSocket Callback Handler (Strands callback_handler) — Pass 3 Reference

This is the real streaming pattern. The agent pushes actual reasoning steps to the portal.

```python
"""WebSocket streaming callback — pushes REAL agent events to portal."""
import json, logging, time
import boto3

class WebSocketCallbackHandler:
    """Strands callback_handler — receives streaming events, pushes to WebSocket."""

    TOOL_AGENT_MAP = {
        'observer_agent': 'Observer',
        'reasoner_agent': 'Reasoner',
        'governance_agent': 'Governance',
        'run_financial_analysis': 'Code Interpreter',
        # [Claude: map tool names to display labels from SOW]
    }

    def __init__(self, connection_id: str, ws_endpoint: str):
        self.connection_id = connection_id
        self.ws_endpoint = ws_endpoint
        self._apigw = boto3.client('apigatewaymanagementapi', endpoint_url=ws_endpoint)
        self._step_count = 0
        self._start_time = time.time()

    def __call__(self, **kwargs):
        """Strands callback protocol — receives streaming events from Agent."""
        event = kwargs.get('event', {})

        # Detect tool use start
        tool_use = event.get('contentBlockStart', {}).get('start', {}).get('toolUse')
        if tool_use:
            self._step_count += 1
            tool_name = tool_use.get('name', 'unknown')
            self._post({
                'type': 'progress',
                'agent': self.TOOL_AGENT_MAP.get(tool_name, 'Agent'),
                'detail': f'Calling {tool_name}...',
                'step': self._step_count,
                'elapsed_ms': int((time.time() - self._start_time) * 1000),
            })

        # Stream reasoning text (CoT)
        reasoning = kwargs.get('reasoningText', '')
        if reasoning:
            self._post({'type': 'progress', 'agent': 'Supervisor',
                        'phase': 'Reasoning', 'detail': reasoning[:200]})

        # Stream LLM text tokens (batched)
        data = kwargs.get('data', '')
        if data and len(data) > 50:
            self._post({'type': 'token', 'text': data})

    def _post(self, data: dict):
        try:
            self._apigw.post_to_connection(
                ConnectionId=self.connection_id, Data=json.dumps(data).encode())
        except Exception:
            pass

    def send_custom_step(self, agent: str, phase: str, detail: str):
        """Send custom progress step (for pre/post agent phases)."""
        self._step_count += 1
        self._post({'type': 'progress', 'agent': agent, 'phase': phase,
                     'detail': detail, 'step': self._step_count})
```

---

## WebSocket Message Handler — Pass 3 Reference

```python
"""$message handler — invoke agent and stream response back."""
import boto3, os, json, time, uuid

agentcore_client = boto3.client('bedrock-agentcore')

def handler(event, context):
    connection_id = event['requestContext']['connectionId']
    body = json.loads(event.get('body', '{}'))
    query = body.get('message', '')
    session_id = body.get('session_id', str(uuid.uuid4()))
    role = body.get('role', 'cfo')

    # Invoke supervisor agent on AgentCore Runtime
    supervisor_arn = os.environ['SUPERVISOR_ARN']
    resp = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=supervisor_arn,
        contentType='application/json',
        payload=json.dumps({
            'prompt': query,
            'role': role,
            'runtimeSessionId': session_id,
            'connectionId': connection_id,
            'wsEndpoint': os.environ['WS_ENDPOINT'],
        }).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=session_id,
    )

    result = json.loads(resp.get('response').read().decode('utf-8'))

    # Send final response
    mgmt = boto3.client('apigatewaymanagementapi', endpoint_url=os.environ['WS_ENDPOINT'])
    mgmt.post_to_connection(
        ConnectionId=connection_id,
        Data=json.dumps({'type': 'response', 'data': result}).encode())

    return {'statusCode': 200}
```

---

## Usage in Supervisor Agent

```python
# Wire callback to Strands Agent for real streaming
callbacks = []
if connection_id and ws_endpoint:
    ws_cb = WebSocketCallbackHandler(connection_id, ws_endpoint)
    callbacks.append(ws_cb)
    # Send pre-synthesis progress steps
    ws_cb.send_custom_step('Supervisor', 'RBAC', f'Loaded {actor_id} policy')
    ws_cb.send_custom_step('Supervisor', 'Model', f'Using {model_id}')

synthesizer = Agent(
    model=model,
    tools=[...],
    system_prompt=SYSTEM_PROMPT,
    callback_handler=callbacks[0] if callbacks else None,
    session_manager=sess_mgr,
)
```
