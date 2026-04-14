# PARTIAL: Strands Agent Frontend — WebSocket Chat UI

**Usage:** Include when SOW mentions agent chat UI, conversational interface, WebSocket streaming, or user-facing agent frontend.

---

## Agent Frontend Overview

```
Chat UI Stack:
  React SPA → WebSocket (API GW v2) → Lambda → Strands Agent
       ↓              ↓                    ↓
  Streaming msgs   $connect/$msg/$disconnect   Agent response
  Session list     Connection DynamoDB table    Session DynamoDB
  Cognito auth     REST fallback (API GW v1)   Artifacts S3
```

---

## CDK Code Block — WebSocket + Session REST

```python
def _create_agent_frontend(self, stage_name: str) -> None:
    """
    Agent chat frontend infrastructure.

    Components:
      A) WebSocket API (API GW v2) for streaming
      B) WebSocket Lambda handlers ($connect, $message, $disconnect)
      C) DynamoDB connection table
      D) REST endpoints for session management
      E) Frontend config SSM parameter

    [Claude: include A+B+C for streaming chat.
     Include D for session history/resume.
     Always include E to wire frontend config.]
    """

    # A) WebSocket API
    self.agent_ws_api = apigwv2.CfnApi(self, "AgentWSApi",
        name=f"{{project_name}}-agent-ws-{stage_name}",
        protocol_type="WEBSOCKET",
        route_selection_expression="$request.body.action")

    self.agent_ws_stage = apigwv2.CfnStage(self, "AgentWSStage",
        api_id=self.agent_ws_api.ref, stage_name=stage_name, auto_deploy=True,
        default_route_settings=apigwv2.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=100, throttling_burst_limit=200))

    # B) WebSocket Lambda Role
    ws_role = iam.Role(self, "WSLambdaRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")])
    ws_role.add_to_policy(iam.PolicyStatement(
        actions=["execute-api:ManageConnections"],
        resources=[f"arn:aws:execute-api:{self.region}:{self.account}:{self.agent_ws_api.ref}/{stage_name}/POST/@connections/*"]))
    self.strands_agent_lambda.grant_invoke(ws_role)

    ws_env = {
        "STAGE": stage_name,
        "CONNECTION_TABLE": f"{{project_name}}-ws-connections-{stage_name}",
        "SESSION_TABLE": self.agent_session_table.table_name,
        "AGENT_FUNCTION_NAME": self.strands_agent_lambda.function_name,
        "WS_ENDPOINT": f"https://{self.agent_ws_api.ref}.execute-api.{self.region}.amazonaws.com/{stage_name}",
    }

    ws_fns = {}
    for name, handler_path, timeout in [
        ("Connect", "src/agent_frontend/ws_connect", 10),
        ("Message", "src/agent_frontend/ws_message", 900),
        ("Disconnect", "src/agent_frontend/ws_disconnect", 10),
    ]:
        ws_fns[name] = _lambda.Function(self, f"WS{name}Fn",
            function_name=f"{{project_name}}-ws-{name.lower()}-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13, architecture=_lambda.Architecture.ARM_64,
            handler="index.handler", code=_lambda.Code.from_asset(handler_path),
            environment=ws_env, timeout=Duration.seconds(timeout),
            memory_size=256 if name != "Message" else 512, role=ws_role)

    # Wire routes
    for route_key, fn_id in [("$connect", "Connect"), ("$default", "Message"), ("$disconnect", "Disconnect")]:
        integ = apigwv2.CfnIntegration(self, f"WSInt{fn_id}",
            api_id=self.agent_ws_api.ref, integration_type="AWS_PROXY",
            integration_uri=f"arn:aws:apigateway:{self.region}:lambda:path/2015-03-31/functions/{ws_fns[fn_id].function_arn}/invocations")
        apigwv2.CfnRoute(self, f"WSRoute{fn_id}",
            api_id=self.agent_ws_api.ref, route_key=route_key, target=f"integrations/{integ.ref}")
        ws_fns[fn_id].add_permission(f"APIGW-{fn_id}",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=f"arn:aws:execute-api:{self.region}:{self.account}:{self.agent_ws_api.ref}/*")

    # C) Connection Table
    ws_conn_table = ddb.Table(self, "WSConnectionTable",
        table_name=f"{{project_name}}-ws-connections-{stage_name}",
        partition_key=ddb.Attribute(name="connection_id", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST, time_to_live_attribute="ttl",
        removal_policy=RemovalPolicy.DESTROY)
    ws_conn_table.grant_read_write_data(ws_role)
    self.agent_session_table.grant_read_write_data(ws_role)

    # E) Frontend Config
    ssm.StringParameter(self, "FrontendConfig",
        parameter_name=f"/{{project_name}}/{stage_name}/agent-frontend-config",
        string_value=json.dumps({
            "cognito": {"user_pool_id": self.user_pool.user_pool_id,
                "app_client_id": self.user_pool_client.user_pool_client_id},
            "api": {"rest_endpoint": self.rest_api.url,
                "ws_endpoint": f"wss://{self.agent_ws_api.ref}.execute-api.{self.region}.amazonaws.com/{stage_name}"},
            "features": {"streaming_enabled": True, "session_history_enabled": True},
        }))

    CfnOutput(self, "AgentWSURL",
        value=f"wss://{self.agent_ws_api.ref}.execute-api.{self.region}.amazonaws.com/{stage_name}")
```

---

## React Chat Hook — Pass 3 Reference

```typescript
/**useAgentChat — WebSocket streaming with REST fallback.*/
import { useState, useEffect, useRef, useCallback } from 'react';

interface ChatMessage { role: 'user' | 'assistant'; content: string; timestamp: string; }

export function useAgentChat(sessionId?: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentSessionId] = useState(sessionId || crypto.randomUUID());
  const wsRef = useRef<WebSocket | null>(null);

  const connectWS = useCallback(async () => {
    const ws = new WebSocket(`${CONFIG.wsEndpoint}?session_id=${currentSessionId}`);
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'message') {
        setMessages(prev => [...prev, { role: 'assistant', content: data.content, timestamp: data.timestamp }]);
        setIsStreaming(false);
      } else if (data.type === 'status' && data.status === 'thinking') {
        setIsStreaming(true);
      }
    };
    wsRef.current = ws;
  }, [currentSessionId]);

  const sendMessage = useCallback(async (content: string) => {
    setMessages(prev => [...prev, { role: 'user', content, timestamp: new Date().toISOString() }]);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'message', message: content, session_id: currentSessionId }));
    }
  }, [currentSessionId]);

  useEffect(() => { connectWS(); return () => wsRef.current?.close(); }, [connectWS]);
  return { messages, sendMessage, isStreaming, currentSessionId };
}
```

---

## WebSocket Handlers — Pass 3 Reference

### ws_connect/index.py
```python
"""$connect — register WebSocket connection."""
import boto3, os, time
ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["CONNECTION_TABLE"])

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    query = event.get("queryStringParameters") or {}
    table.put_item(Item={
        "connection_id": connection_id,
        "session_id": query.get("session_id", connection_id),
        "ttl": int(time.time()) + 7200,
    })
    return {"statusCode": 200}
```

### ws_message/index.py
```python
"""$message — invoke agent and post response back."""
import boto3, os, json, time
lambda_client = boto3.client("lambda")

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    body = json.loads(event.get("body", "{}"))
    mgmt = boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["WS_ENDPOINT"])

    mgmt.post_to_connection(ConnectionId=connection_id,
        Data=json.dumps({"type": "status", "status": "thinking"}).encode())

    response = lambda_client.invoke(
        FunctionName=os.environ["AGENT_FUNCTION_NAME"],
        Payload=json.dumps({"message": body.get("message", ""), "session_id": body.get("session_id")}))
    payload = json.loads(response["Payload"].read())
    agent_body = json.loads(payload.get("body", "{}"))

    mgmt.post_to_connection(ConnectionId=connection_id,
        Data=json.dumps({"type": "message", "content": agent_body.get("response", ""),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ")}).encode())
    return {"statusCode": 200}
```

### ws_disconnect/index.py
```python
"""$disconnect — clean up connection."""
import boto3, os
ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["CONNECTION_TABLE"])

def handler(event, context):
    table.delete_item(Key={"connection_id": event["requestContext"]["connectionId"]})
    return {"statusCode": 200}
```
