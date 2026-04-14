# PARTIAL: Strands Deploy to Lambda — Proxy Lambda, Direct Lambda, Lambda Layer

**Usage:** Include when SOW mentions Lambda-hosted agents, MCP proxy Lambda, or serverless agent deployment.

---

## Lambda Deployment Patterns (from real production)

```
Three Lambda patterns:
  1. MCP Runtime Proxy: Gateway → Lambda → invoke_agent_runtime() → MCP Server
  2. Direct Lambda Target: Gateway → Lambda (tool logic runs in Lambda)
  3. Agent Lambda: API GW → Lambda → Strands Agent (for simple agents)

Production uses Pattern 1+2 via Gateway. Pattern 3 for standalone agents.

Lambda Layer ARN (official Strands):
  arn:aws:lambda:{region}:856699698935:layer:strands-agents-py{version}-{arch}:{layer_version}
  Python: 3.10-3.13  |  Arch: x86_64, aarch64  |  Layer v1 = SDK v1.23.0
```

---

## Pattern 1: MCP Runtime Proxy Lambda — Pass 2A Reference

```typescript
// CDK: Lambda proxy that bridges Gateway → AgentCore Runtime (MCP server)
const proxyRole = new iam.Role(this, 'McpProxyRole', {
  assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  managedPolicies: [
    iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
  ],
});
proxyRole.addToPolicy(new iam.PolicyStatement({
  actions: ['bedrock-agentcore:InvokeAgentRuntime'],
  resources: [/* MCP Runtime ARNs from SSM */],
}));

const proxyFn = new lambda.Function(this, 'McpProxyFn', {
  functionName: `{{project_name}}-mcp-proxy`,
  runtime: lambda.Runtime.PYTHON_3_13,
  architecture: lambda.Architecture.ARM_64,
  handler: 'handler.handler',
  code: lambda.Code.fromAsset('lambda/mcp_runtime_proxy'),
  role: proxyRole,
  timeout: cdk.Duration.seconds(120),
  memorySize: 512,
  environment: { RUNTIME_ARN: runtimeArn },
});
```

---

## MCP Proxy Handler — Pass 3 Reference

```python
"""MCP Runtime Proxy — Gateway → Lambda → AgentCore Runtime (MCP server)."""
import json, logging, os, uuid
import boto3

RUNTIME_ARN = os.environ['RUNTIME_ARN']
agentcore_client = boto3.client('bedrock-agentcore')

def handler(event, context):
    # Extract tool name from Gateway context metadata
    tool_name = 'unknown'
    try:
        if hasattr(context, 'client_context') and context.client_context:
            raw = getattr(context.client_context, 'custom', {}).get('bedrockAgentCoreToolName', '')
            tool_name = raw.split('___')[-1] if '___' in raw else raw
    except Exception: pass

    # Build MCP JSON-RPC request
    mcp_request = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": event},
    }

    response = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        contentType='application/json',
        payload=json.dumps(mcp_request).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=str(uuid.uuid4()),
    )

    raw = response.get('response').read().decode('utf-8')
    result = json.loads(raw)
    if 'result' in result and 'content' in result.get('result', {}):
        texts = [c.get('text', '') for c in result['result']['content'] if c.get('type') == 'text']
        if texts:
            try: return json.loads(texts[0])
            except: return {'text': texts[0]}
    return result
```

---

## Pattern 2: Direct Lambda Target — Pass 2A Reference

```typescript
// CDK: Lambda with tool logic, exposed directly via Gateway
const toolFn = new lambda.Function(this, 'ToolFn', {
  functionName: `{{project_name}}-tool-name`,
  runtime: lambda.Runtime.PYTHON_3_13,
  architecture: lambda.Architecture.ARM_64,
  handler: 'index.handler',
  code: lambda.Code.fromAsset('lambda/tool_name'),
  timeout: cdk.Duration.seconds(60),
  memorySize: 512,
  vpc, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});

// Both identity-based AND resource-based permissions required
toolFn.grantInvoke(gatewayRole);
toolFn.addPermission('AllowAgentCoreInvoke', {
  principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  sourceAccount: cdk.Aws.ACCOUNT_ID,
});
```

---

## Pattern 3: Standalone Agent Lambda — Pass 2A Reference

```python
"""Simple agent Lambda — for standalone agents not on AgentCore Runtime."""
from strands import Agent
from strands.models import BedrockModel
import json

def handler(event, context):
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    agent = Agent(
        model=BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0"),
        system_prompt="You are helpful.",
        tools=[],  # [Claude: add tools from SOW]
    )
    response = agent(body.get("message", ""))
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response": str(response)}),
    }
```
