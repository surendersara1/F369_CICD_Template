# PARTIAL: Strands Agent Frontend — Chat UI Infrastructure

**Usage:** Include when SOW mentions agent chat UI, conversational interface, agent dashboard, AI assistant frontend, or any user-facing web interface for Strands agents.

---

## Agent Frontend Architecture Overview

```
Strands Agent Frontend = Chat UI for interacting with Strands agents:
  - React SPA with streaming chat interface
  - WebSocket (API GW v2) for real-time agent response streaming
  - REST fallback (API GW v1) for non-streaming invocations
  - Cognito authentication (same pool as API layer or dedicated)
  - CloudFront + S3 hosting (reuses LAYER_FRONTEND infra)
  - Session management UI (history, resume, new conversation)

Agent Frontend Stack:
  ┌─────────────────────────────────────────────────────────────────────┐
  │                   Strands Agent Chat Frontend                       │
  │  ┌──────────────────┐  ┌───────────────────┐  ┌────────────────┐  │
  │  │  React Chat UI   │  │  WebSocket API    │  │  REST API      │  │
  │  │  Streaming msgs  │  │  API GW v2 WSS    │  │  API GW v1     │  │
  │  │  Session list    │  │  $connect/$msg    │  │  /agent/invoke │  │
  │  │  Markdown render │  │  $disconnect      │  │  /agent/sessions│ │
  │  └──────────────────┘  └───────────────────┘  └────────────────┘  │
  │  ┌──────────────────┐  ┌───────────────────┐  ┌────────────────┐  │
  │  │  Cognito Auth    │  │  S3 + CloudFront  │  │  Connection    │  │
  │  │  JWT tokens      │  │  Static hosting   │  │  DynamoDB tbl  │  │
  │  │  Login/Signup    │  │  WAF protected    │  │  WS conn mgmt  │  │
  │  └──────────────────┘  └───────────────────┘  └────────────────┘  │
  └─────────────────────────────────────────────────────────────────────┘

Chat Flow (Streaming):
  User types message → React UI
       ↓
  WebSocket send (WSS) → API GW v2 → Lambda ($message route)
       ↓
  Lambda invokes Strands Agent (streaming callback)
       ↓
  Agent streams tokens → Lambda posts to WS Management API
       ↓
  WebSocket pushes chunks → React UI renders incrementally

Chat Flow (Non-Streaming Fallback):
  User types message → React UI
       ↓
  POST /agent/invoke → API GW v1 → Lambda
       ↓
  Lambda invokes Strands Agent → full response
       ↓
  JSON response → React UI renders complete message
```

---

## CDK Code Block — Agent Frontend Infrastructure

```python
def _create_strands_agent_frontend(self, stage_name: str) -> None:
    """
    Strands Agent chat frontend infrastructure.

    Components:
      A) WebSocket API (API Gateway v2) for streaming agent responses
      B) WebSocket Lambda handlers ($connect, $message, $disconnect)
      C) DynamoDB connection table (WebSocket connection management)
      D) REST endpoints for session management (list, resume, delete)
      E) Frontend config injection (Cognito, API URLs → runtime config)

    [Claude: include A+B+C for any SOW mentioning agent chat UI or streaming.
     Include D for session history/resume features.
     Always include E to wire frontend config to backend endpoints.
     Reuse LAYER_FRONTEND S3+CloudFront for static hosting — do NOT duplicate.]
    """

    # =========================================================================
    # A) WEBSOCKET API — Real-Time Agent Response Streaming
    # =========================================================================

    self.agent_ws_api = apigwv2.CfnApi(
        self, "AgentWebSocketApi",
        name=f"{{project_name}}-agent-ws-{stage_name}",
        protocol_type="WEBSOCKET",
        route_selection_expression="$request.body.action",
    )

    # WebSocket stage with logging
    ws_log_group = logs.LogGroup(
        self, "AgentWSAccessLogs",
        log_group_name=f"/{{project_name}}/{stage_name}/agent-ws-access-logs",
        retention=logs.RetentionDays.ONE_MONTH if stage_name != "prod" else logs.RetentionDays.ONE_YEAR,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    self.agent_ws_stage = apigwv2.CfnStage(
        self, "AgentWSStage",
        api_id=self.agent_ws_api.ref,
        stage_name=stage_name,
        auto_deploy=True,
        default_route_settings=apigwv2.CfnStage.RouteSettingsProperty(
            logging_level="INFO",
            data_trace_enabled=stage_name != "prod",
            throttling_rate_limit=100,
            throttling_burst_limit=200,
        ),
        access_log_settings=apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=ws_log_group.log_group_arn,
        ),
    )

    # =========================================================================
    # B) WEBSOCKET LAMBDA HANDLERS
    # =========================================================================

    # IAM role for WebSocket Lambdas (agent invoke + WS management API)
    ws_lambda_role = iam.Role(
        self, "AgentWSLambdaRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        role_name=f"{{project_name}}-agent-ws-lambda-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole"),
        ],
    )

    # Permission to post messages back to WebSocket clients
    ws_lambda_role.add_to_policy(
        iam.PolicyStatement(
            sid="WebSocketManagementAPI",
            actions=["execute-api:ManageConnections"],
            resources=[
                f"arn:aws:execute-api:{self.region}:{self.account}:{self.agent_ws_api.ref}/{stage_name}/POST/@connections/*",
            ],
        )
    )

    # Permission to invoke the Strands agent Lambda
    self.strands_agent_lambda.grant_invoke(ws_lambda_role)

    # Shared environment for all WS handlers
    ws_env = {
        "STAGE": stage_name,
        "CONNECTION_TABLE": f"{{project_name}}-agent-ws-connections-{stage_name}",
        "SESSION_TABLE": f"{{project_name}}-agent-sessions-{stage_name}",
        "AGENT_FUNCTION_NAME": self.strands_agent_lambda.function_name,
        "WS_ENDPOINT": f"https://{self.agent_ws_api.ref}.execute-api.{self.region}.amazonaws.com/{stage_name}",
    }

    # $connect handler — authenticate and register connection
    self.ws_connect_fn = _lambda.Function(
        self, "AgentWSConnectFn",
        function_name=f"{{project_name}}-agent-ws-connect-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/agent_frontend/ws_connect"),
        environment=ws_env,
        timeout=Duration.seconds(10),
        memory_size=256,
        role=ws_lambda_role,
    )

    # $message handler — invoke agent and stream response back
    self.ws_message_fn = _lambda.Function(
        self, "AgentWSMessageFn",
        function_name=f"{{project_name}}-agent-ws-message-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/agent_frontend/ws_message"),
        environment=ws_env,
        timeout=Duration.minutes(15),  # Match agent Lambda timeout
        memory_size=512,
        role=ws_lambda_role,
    )

    # $disconnect handler — clean up connection record
    self.ws_disconnect_fn = _lambda.Function(
        self, "AgentWSDisconnectFn",
        function_name=f"{{project_name}}-agent-ws-disconnect-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/agent_frontend/ws_disconnect"),
        environment=ws_env,
        timeout=Duration.seconds(10),
        memory_size=256,
        role=ws_lambda_role,
    )

    # Wire Lambda integrations to WebSocket routes
    for route_key, fn, fn_id in [
        ("$connect", self.ws_connect_fn, "Connect"),
        ("$default", self.ws_message_fn, "Message"),
        ("$disconnect", self.ws_disconnect_fn, "Disconnect"),
    ]:
        integration = apigwv2.CfnIntegration(
            self, f"AgentWSIntegration{fn_id}",
            api_id=self.agent_ws_api.ref,
            integration_type="AWS_PROXY",
            integration_uri=f"arn:aws:apigateway:{self.region}:lambda:path/2015-03-31/functions/{fn.function_arn}/invocations",
        )
        apigwv2.CfnRoute(
            self, f"AgentWSRoute{fn_id}",
            api_id=self.agent_ws_api.ref,
            route_key=route_key,
            target=f"integrations/{integration.ref}",
        )
        fn.add_permission(
            f"APIGW-{fn_id}",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=f"arn:aws:execute-api:{self.region}:{self.account}:{self.agent_ws_api.ref}/*",
        )

    # =========================================================================
    # C) DYNAMODB — WebSocket Connection Management
    # =========================================================================

    self.ws_connection_table = ddb.Table(
        self, "AgentWSConnectionTable",
        table_name=f"{{project_name}}-agent-ws-connections-{stage_name}",
        partition_key=ddb.Attribute(name="connection_id", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="ttl",
        removal_policy=RemovalPolicy.DESTROY,
    )
    self.ws_connection_table.add_global_secondary_index(
        index_name="actor-connections-idx",
        partition_key=ddb.Attribute(name="actor_id", type=ddb.AttributeType.STRING),
        projection_type=ddb.ProjectionType.ALL,
    )
    self.ws_connection_table.grant_read_write_data(ws_lambda_role)

    # Grant WS handlers access to the agent session table (for session resume)
    self.agent_session_table.grant_read_write_data(ws_lambda_role)

    # =========================================================================
    # D) REST ENDPOINTS — Session Management
    # [Claude: attach these to the existing REST API from LAYER_API if present,
    #  otherwise create a minimal HTTP API for session management.]
    # =========================================================================

    agent_resource = self.rest_api.root.add_resource("agent")

    # POST /agent/invoke — non-streaming agent invocation (fallback)
    invoke_resource = agent_resource.add_resource("invoke")
    invoke_resource.add_method(
        "POST",
        apigw.LambdaIntegration(self.strands_agent_lambda, proxy=True),
        authorization_type=apigw.AuthorizationType.COGNITO,
        authorizer=self.cognito_authorizer,
    )

    # Session management Lambda
    self.session_mgmt_fn = _lambda.Function(
        self, "AgentSessionMgmtFn",
        function_name=f"{{project_name}}-agent-sessions-api-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/agent_frontend/session_mgmt"),
        environment={
            "STAGE": stage_name,
            "SESSION_TABLE": f"{{project_name}}-agent-sessions-{stage_name}",
        },
        timeout=Duration.seconds(10),
        memory_size=256,
    )
    self.agent_session_table.grant_read_write_data(self.session_mgmt_fn)

    # GET /agent/sessions — list user's conversation sessions
    sessions_resource = agent_resource.add_resource("sessions")
    sessions_resource.add_method(
        "GET",
        apigw.LambdaIntegration(self.session_mgmt_fn, proxy=True),
        authorization_type=apigw.AuthorizationType.COGNITO,
        authorizer=self.cognito_authorizer,
    )

    # GET /agent/sessions/{session_id} — get session history
    session_detail = sessions_resource.add_resource("{session_id}")
    session_detail.add_method(
        "GET",
        apigw.LambdaIntegration(self.session_mgmt_fn, proxy=True),
        authorization_type=apigw.AuthorizationType.COGNITO,
        authorizer=self.cognito_authorizer,
    )

    # DELETE /agent/sessions/{session_id} — delete a session
    session_detail.add_method(
        "DELETE",
        apigw.LambdaIntegration(self.session_mgmt_fn, proxy=True),
        authorization_type=apigw.AuthorizationType.COGNITO,
        authorizer=self.cognito_authorizer,
    )

    # =========================================================================
    # E) FRONTEND CONFIG — Runtime Configuration for React App
    # [Claude: this SSM parameter is read at build time or injected as
    #  window.__RUNTIME_CONFIG__ in index.html by the deployment script.]
    # =========================================================================

    ssm.StringParameter(
        self, "AgentFrontendConfig",
        parameter_name=f"/{{project_name}}/{stage_name}/agent-frontend-config",
        string_value=json.dumps({
            "cognito": {
                "user_pool_id": self.user_pool.user_pool_id,
                "app_client_id": self.user_pool_client.user_pool_client_id,
                "region": self.region,
            },
            "api": {
                "rest_endpoint": self.rest_api.url,
                "ws_endpoint": f"wss://{self.agent_ws_api.ref}.execute-api.{self.region}.amazonaws.com/{stage_name}",
            },
            "features": {
                "streaming_enabled": True,
                "session_history_enabled": True,
                "file_upload_enabled": False,  # [Claude: set True if SOW mentions file/doc upload in chat]
            },
        }),
        description="Runtime configuration for agent chat frontend",
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "AgentWebSocketURL",
        value=f"wss://{self.agent_ws_api.ref}.execute-api.{self.region}.amazonaws.com/{stage_name}",
        description="WebSocket URL for agent chat streaming",
        export_name=f"{{project_name}}-agent-ws-url-{stage_name}",
    )
    CfnOutput(self, "AgentRESTInvokeURL",
        value=f"{self.rest_api.url}agent/invoke",
        description="REST endpoint for non-streaming agent invocation",
    )
    CfnOutput(self, "AgentSessionsURL",
        value=f"{self.rest_api.url}agent/sessions",
        description="REST endpoint for session management",
    )
```

---


## WebSocket Handler Code — Pass 3 Reference

### `src/agent_frontend/ws_connect/index.py`

```python
"""
WebSocket $connect handler — authenticate and register connection.
"""
import boto3, os, json, time

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["CONNECTION_TABLE"])


def handler(event, context):
    """Handle WebSocket $connect — validate token and store connection."""
    connection_id = event["requestContext"]["connectionId"]

    # Extract actor_id from Cognito authorizer claims (if using Cognito)
    # or from query string token for custom auth
    query_params = event.get("queryStringParameters") or {}
    actor_id = query_params.get("actor_id", "anonymous")
    session_id = query_params.get("session_id", connection_id)

    # [Claude: add Cognito JWT validation here if not using API GW authorizer.
    #  If LAYER_API Cognito authorizer is attached to WS $connect, claims are
    #  available at event["requestContext"]["authorizer"].]

    table.put_item(Item={
        "connection_id": connection_id,
        "actor_id": actor_id,
        "session_id": session_id,
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": int(time.time()) + (2 * 3600),  # 2hr connection TTL
    })

    return {"statusCode": 200, "body": "Connected"}
```

### `src/agent_frontend/ws_message/index.py`

```python
"""
WebSocket $message handler — invoke Strands agent and stream response.
"""
import boto3, os, json, time

lambda_client = boto3.client("lambda")
apigw_mgmt = None  # Initialized lazily with endpoint URL


def _get_apigw_management(endpoint_url: str):
    """Get API Gateway Management API client for posting to WebSocket."""
    global apigw_mgmt
    if apigw_mgmt is None:
        apigw_mgmt = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=endpoint_url,
        )
    return apigw_mgmt


def _post_to_connection(connection_id: str, data: dict) -> bool:
    """Send a message to a WebSocket connection. Returns False if stale."""
    try:
        mgmt = _get_apigw_management(os.environ["WS_ENDPOINT"])
        mgmt.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(data).encode("utf-8"),
        )
        return True
    except mgmt.exceptions.GoneException:
        return False


def handler(event, context):
    """Handle WebSocket $message — invoke agent and stream back."""
    connection_id = event["requestContext"]["connectionId"]
    body = json.loads(event.get("body", "{}"))
    user_message = body.get("message", "")
    session_id = body.get("session_id", connection_id)

    if not user_message:
        _post_to_connection(connection_id, {
            "type": "error",
            "message": "Empty message",
        })
        return {"statusCode": 400}

    # Send "thinking" indicator
    _post_to_connection(connection_id, {
        "type": "status",
        "status": "thinking",
        "session_id": session_id,
    })

    # Invoke the Strands agent Lambda synchronously
    # [Claude: for true token-level streaming, use Lambda response streaming
    #  with InvokeWithResponseStream. For simplicity, this uses full invocation
    #  and posts the complete response. Upgrade to streaming if SOW requires it.]
    response = lambda_client.invoke(
        FunctionName=os.environ["AGENT_FUNCTION_NAME"],
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "message": user_message,
            "session_id": session_id,
            "actor_id": body.get("actor_id", "anonymous"),
        }),
    )

    payload = json.loads(response["Payload"].read())
    agent_body = json.loads(payload.get("body", "{}"))

    # Send agent response to client
    _post_to_connection(connection_id, {
        "type": "message",
        "session_id": agent_body.get("session_id", session_id),
        "content": agent_body.get("response", "No response from agent."),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    # Send "done" indicator
    _post_to_connection(connection_id, {
        "type": "status",
        "status": "done",
    })

    return {"statusCode": 200}
```

### `src/agent_frontend/ws_disconnect/index.py`

```python
"""
WebSocket $disconnect handler — clean up connection record.
"""
import boto3, os

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["CONNECTION_TABLE"])


def handler(event, context):
    """Handle WebSocket $disconnect — remove connection from table."""
    connection_id = event["requestContext"]["connectionId"]
    table.delete_item(Key={"connection_id": connection_id})
    return {"statusCode": 200, "body": "Disconnected"}
```

### `src/agent_frontend/session_mgmt/index.py`

```python
"""
Session management REST API — list, get, delete agent conversation sessions.
"""
import boto3, os, json
from boto3.dynamodb.conditions import Key

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["SESSION_TABLE"])


def handler(event, context):
    """Route session management requests."""
    method = event["httpMethod"]
    path_params = event.get("pathParameters") or {}
    session_id = path_params.get("session_id")

    # Extract actor_id from Cognito claims
    claims = event["requestContext"]["authorizer"]["claims"]
    actor_id = claims.get("sub", claims.get("email", "anonymous"))

    if method == "GET" and not session_id:
        return _list_sessions(actor_id)
    elif method == "GET" and session_id:
        return _get_session(session_id, actor_id)
    elif method == "DELETE" and session_id:
        return _delete_session(session_id, actor_id)
    else:
        return _response(405, {"error": "Method not allowed"})


def _list_sessions(actor_id: str) -> dict:
    """List all sessions for an actor, most recent first."""
    result = table.query(
        IndexName="actor-sessions-idx",
        KeyConditionExpression=Key("actor_id").eq(actor_id),
        ScanIndexForward=False,
        Limit=50,
    )
    # Deduplicate by session_id (table has multiple turns per session)
    seen = {}
    for item in result.get("Items", []):
        sid = item["session_id"]
        if sid not in seen:
            seen[sid] = {
                "session_id": sid,
                "created_at": item.get("created_at", ""),
                "preview": item.get("user_message", "")[:100],
            }
    return _response(200, {"sessions": list(seen.values())})


def _get_session(session_id: str, actor_id: str) -> dict:
    """Get all turns for a session."""
    result = table.query(
        KeyConditionExpression=Key("session_id").eq(session_id),
        ScanIndexForward=True,
    )
    items = result.get("Items", [])
    # Verify ownership
    if items and items[0].get("actor_id") != actor_id:
        return _response(403, {"error": "Access denied"})
    turns = [
        {
            "turn_id": item["turn_id"],
            "user_message": item.get("user_message", ""),
            "agent_response": item.get("agent_response", ""),
            "created_at": item.get("created_at", ""),
        }
        for item in items
    ]
    return _response(200, {"session_id": session_id, "turns": turns})


def _delete_session(session_id: str, actor_id: str) -> dict:
    """Delete all turns for a session (soft-delete via TTL or hard delete)."""
    result = table.query(
        KeyConditionExpression=Key("session_id").eq(session_id),
        ProjectionExpression="session_id, turn_id, actor_id",
    )
    items = result.get("Items", [])
    if items and items[0].get("actor_id") != actor_id:
        return _response(403, {"error": "Access denied"})
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={
                "session_id": item["session_id"],
                "turn_id": item["turn_id"],
            })
    return _response(200, {"deleted": session_id})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }
```

---

## React Chat UI — Pass 3 Reference

Claude generates this in `frontend/src/` during Pass 3 when agent chat UI is detected:

### `frontend/src/hooks/useAgentChat.ts`

```typescript
/**
 * React hook for Strands agent chat — WebSocket streaming with REST fallback.
 *
 * Usage:
 *   const { messages, sendMessage, isStreaming, sessions, loadSession } = useAgentChat();
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

interface AgentChatConfig {
  wsEndpoint: string;
  restEndpoint: string;
  streamingEnabled?: boolean;
}

// [Claude: read config from window.__RUNTIME_CONFIG__ or environment variables]
const CONFIG: AgentChatConfig = {
  wsEndpoint: (window as any).__RUNTIME_CONFIG__?.api?.ws_endpoint || '',
  restEndpoint: (window as any).__RUNTIME_CONFIG__?.api?.rest_endpoint || '',
  streamingEnabled: (window as any).__RUNTIME_CONFIG__?.features?.streaming_enabled ?? true,
};

export function useAgentChat(sessionId?: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState(sessionId || crypto.randomUUID());
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  // --- WebSocket connection ---
  const connectWS = useCallback(async () => {
    if (!CONFIG.streamingEnabled || !CONFIG.wsEndpoint) return;

    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString() || '';
      const url = `${CONFIG.wsEndpoint}?token=${token}&session_id=${currentSessionId}`;

      const ws = new WebSocket(url);

      ws.onopen = () => console.log('[AgentChat] WebSocket connected');

      ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'message') {
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: data.content,
            timestamp: data.timestamp,
          }]);
          setIsStreaming(false);
        } else if (data.type === 'status' && data.status === 'thinking') {
          setIsStreaming(true);
        } else if (data.type === 'status' && data.status === 'done') {
          setIsStreaming(false);
        }
      };

      ws.onclose = () => {
        console.log('[AgentChat] WebSocket closed, reconnecting in 3s...');
        reconnectTimer.current = setTimeout(connectWS, 3000);
      };

      ws.onerror = (err) => console.error('[AgentChat] WebSocket error:', err);

      wsRef.current = ws;
    } catch (err) {
      console.error('[AgentChat] Failed to connect WebSocket:', err);
    }
  }, [currentSessionId]);

  useEffect(() => {
    connectWS();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connectWS]);

  // --- Send message ---
  const sendMessage = useCallback(async (content: string) => {
    const userMsg: ChatMessage = {
      role: 'user',
      content,
      timestamp: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);

    // Try WebSocket first, fall back to REST
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'message',
        message: content,
        session_id: currentSessionId,
      }));
    } else {
      // REST fallback
      setIsStreaming(true);
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString() || '';
        const resp = await fetch(`${CONFIG.restEndpoint}agent/invoke`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`,
          },
          body: JSON.stringify({
            message: content,
            session_id: currentSessionId,
          }),
        });
        const data = await resp.json();
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.response,
          timestamp: new Date().toISOString(),
        }]);
      } catch (err) {
        console.error('[AgentChat] REST invoke failed:', err);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: 'Sorry, something went wrong. Please try again.',
          timestamp: new Date().toISOString(),
        }]);
      } finally {
        setIsStreaming(false);
      }
    }
  }, [currentSessionId]);

  // --- Session management ---
  const startNewSession = useCallback(() => {
    setMessages([]);
    setCurrentSessionId(crypto.randomUUID());
  }, []);

  const loadSession = useCallback(async (sid: string) => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString() || '';
      const resp = await fetch(`${CONFIG.restEndpoint}agent/sessions/${sid}`, {
        headers: { 'Authorization': `Bearer ${token}` },
      });
      const data = await resp.json();
      const loaded: ChatMessage[] = data.turns.flatMap((t: any) => [
        { role: 'user' as const, content: t.user_message, timestamp: t.created_at },
        { role: 'assistant' as const, content: t.agent_response, timestamp: t.created_at },
      ]);
      setMessages(loaded);
      setCurrentSessionId(sid);
    } catch (err) {
      console.error('[AgentChat] Failed to load session:', err);
    }
  }, []);

  return {
    messages,
    sendMessage,
    isStreaming,
    currentSessionId,
    startNewSession,
    loadSession,
  };
}
```

### `frontend/src/components/AgentChat.tsx`

```tsx
/**
 * Agent Chat component — renders the conversational UI.
 *
 * [Claude: customize styling based on SOW design requirements.
 *  This is a minimal functional component — add Tailwind/MUI as needed.]
 */
import React, { useState, useRef, useEffect } from 'react';
import { useAgentChat } from '../hooks/useAgentChat';

export function AgentChat() {
  const { messages, sendMessage, isStreaming, startNewSession } = useAgentChat();
  const [input, setInput] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;
    sendMessage(input.trim());
    setInput('');
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', maxWidth: 800, margin: '0 auto' }}>
      {/* Header */}
      <header style={{ padding: 16, borderBottom: '1px solid #e0e0e0', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: 0 }}>AI Assistant</h2>
        <button onClick={startNewSession} aria-label="Start new conversation">
          New Chat
        </button>
      </header>

      {/* Messages */}
      <main style={{ flex: 1, overflowY: 'auto', padding: 16 }} role="log" aria-live="polite" aria-label="Chat messages">
        {messages.map((msg, i) => (
          <div
            key={i}
            style={{
              marginBottom: 12,
              padding: 12,
              borderRadius: 8,
              backgroundColor: msg.role === 'user' ? '#e3f2fd' : '#f5f5f5',
              alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '80%',
              marginLeft: msg.role === 'user' ? 'auto' : 0,
            }}
            role="article"
            aria-label={`${msg.role === 'user' ? 'You' : 'Assistant'}: ${msg.content.substring(0, 50)}`}
          >
            <strong>{msg.role === 'user' ? 'You' : 'Assistant'}</strong>
            <p style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}>{msg.content}</p>
          </div>
        ))}
        {isStreaming && (
          <div style={{ padding: 12, color: '#666' }} aria-live="assertive">
            <em>Thinking...</em>
          </div>
        )}
        <div ref={messagesEndRef} />
      </main>

      {/* Input */}
      <form onSubmit={handleSubmit} style={{ padding: 16, borderTop: '1px solid #e0e0e0', display: 'flex', gap: 8 }}>
        <label htmlFor="chat-input" className="sr-only">Type your message</label>
        <input
          id="chat-input"
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Type your message..."
          disabled={isStreaming}
          style={{ flex: 1, padding: 12, borderRadius: 8, border: '1px solid #ccc' }}
          aria-label="Chat message input"
        />
        <button
          type="submit"
          disabled={isStreaming || !input.trim()}
          style={{ padding: '12px 24px', borderRadius: 8 }}
          aria-label="Send message"
        >
          Send
        </button>
      </form>
    </div>
  );
}
```
