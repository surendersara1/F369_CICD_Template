# PARTIAL: AgentCore Gateway — MCP Endpoint, Lambda Targets, Runtime Proxy

**Usage:** Include when SOW mentions AgentCore Gateway, MCP tools, tool gateway, Lambda tool targets, or MCP Runtime proxy pattern.

---

## AgentCore Gateway Overview

```
AgentCore Gateway = Unified MCP endpoint for all agent tools:
  - CfnGateway (AWS::BedrockAgentCore::Gateway) with IAM or OAuth2 auth
  - CfnGatewayTarget per tool group (Lambda or MCP Runtime)
  - Tool schemas defined inline or loaded from JSON files
  - Two target patterns:
    1. Direct Lambda: Gateway → Lambda (tool logic in Lambda)
    2. Runtime Proxy: Gateway → Lambda proxy → AgentCore Runtime (MCP server)

Architecture (from real production):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  AgentCore Gateway (MCP protocol, IAM auth)                         │
  │                                                                     │
  │  Target: neptune-graph-fn ──→ Lambda (direct)                       │
  │  Target: monte-carlo-fn  ──→ Lambda (direct)                        │
  │  Target: variance-decomp ──→ Lambda (direct)                        │
  │  Target: forecast-metric ──→ Lambda (direct)                        │
  │                                                                     │
  │  Target: redshift-mcp-proxy ──→ Lambda proxy ──→ AgentCore Runtime  │
  │  Target: neptune-mcp-proxy  ──→ Lambda proxy ──→ AgentCore Runtime  │
  │  Target: opensearch-mcp-proxy→ Lambda proxy ──→ AgentCore Runtime   │
  │  Target: aurora-mcp-proxy   ──→ Lambda proxy ──→ AgentCore Runtime  │
  └─────────────────────────────────────────────────────────────────────┘

Agent connects via:
  Strands Agent → MCPClient(sigv4_transport(gateway_url)) → Gateway → Tools
```

---

## CDK Code Block — Gateway Stack

```typescript
// infra/lib/stacks/gateway/ms-05-gateway-stack.ts
import { aws_bedrockagentcore as agentcore } from 'aws-cdk-lib';

// ======================================================================
// Gateway IAM Role
// ======================================================================
const gatewayRole = new iam.Role(this, 'GatewayRole', {
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
});

gatewayRole.addToPolicy(new iam.PolicyStatement({
  actions: ['lambda:InvokeFunction'],
  resources: [`arn:aws:lambda:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:function:{{project_name}}-*`],
}));

// PolicyEngine permissions (for Cedar RBAC — see AGENTCORE_AGENT_CONTROL.md)
gatewayRole.addToPolicy(new iam.PolicyStatement({
  sid: 'PolicyEngineAccess',
  actions: [
    'bedrock-agentcore:GetPolicyEngine',
    'bedrock-agentcore:AuthorizeAction',
    'bedrock-agentcore:PartiallyAuthorizeActions',
  ],
  resources: [
    `arn:aws:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:policy-engine/*`,
    `arn:aws:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:gateway/*`,
  ],
}));

// ======================================================================
// AgentCore Gateway (CfnGateway L1)
// ======================================================================
const gateway = new agentcore.CfnGateway(this, 'McpGateway', {
  name: `{{project_name}}-gateway`,
  authorizerType: 'AWS_IAM',
  protocolType: 'MCP',
  roleArn: gatewayRole.roleArn,
  description: 'Unified MCP tool endpoint for all agents',
});
```

---

## Pattern 1: Direct Lambda Target

For tools where logic runs directly in Lambda (no MCP Runtime needed):

```typescript
// Direct Lambda function (e.g., Monte Carlo simulation)
const monteCarloFn = new lambda.Function(this, 'MonteCarloFn', {
  functionName: `{{project_name}}-monte-carlo`,
  runtime: lambda.Runtime.PYTHON_3_13,
  architecture: lambda.Architecture.ARM_64,
  handler: 'index.handler',
  code: lambda.Code.fromAsset('lambda/monte_carlo_sim'),
  timeout: cdk.Duration.seconds(300),
  memorySize: 2048,
  vpc, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});

// Grant Gateway + resource-based permission (both required)
monteCarloFn.grantInvoke(gatewayRole);
monteCarloFn.addPermission('AllowAgentCoreInvoke', {
  principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  action: 'lambda:InvokeFunction',
  sourceAccount: cdk.Aws.ACCOUNT_ID,
});

// Gateway Target with inline tool schema
const target = new agentcore.CfnGatewayTarget(this, 'MonteCarloTarget', {
  name: 'monte-carlo-fn',
  gatewayIdentifier: gateway.attrGatewayIdentifier,
  credentialProviderConfigurations: [
    { credentialProviderType: 'GATEWAY_IAM_ROLE' },
  ],
  targetConfiguration: {
    mcp: {
      lambda: {
        lambdaArn: monteCarloFn.functionArn,
        toolSchema: {
          inlinePayload: [
            {
              name: 'run_monte_carlo_simulation',
              description: 'Run Monte Carlo simulation for P&L projections',
              inputSchema: {
                type: 'object',
                properties: {
                  iterations: { type: 'number', description: 'Simulation iterations (default 500)' },
                  scenario: { type: 'string', description: 'Scenario name' },
                },
              },
            },
          ],
        },
      },
    },
  },
});
target.addDependency(gateway);
```

---

## Pattern 2: Lambda Proxy → AgentCore Runtime (MCP Server)

For tools backed by MCP servers running on AgentCore Runtime:

```typescript
// Shared proxy role for all MCP Runtime proxy Lambdas
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

// MCP proxy definitions — one per MCP Runtime
const mcpProxies = [
  {
    id: 'Redshift',
    targetName: 'redshift-mcp-proxy',
    runtimeSsmKey: 'redshift_mcp_endpoint',
    schemaFile: 'schemas/redshift_tools.json',  // Tool schemas loaded from JSON
    description: 'Redshift MCP proxy — financial analytics',
  },
  // [Claude: add more proxies per SOW data sources]
];

for (const proxy of mcpProxies) {
  const runtimeArn = ssmLookup(this, `/{{project_name}}/runtime/${proxy.runtimeSsmKey}`);
  const toolSchemaData = JSON.parse(fs.readFileSync(proxy.schemaFile, 'utf-8'));

  const proxyFn = new lambda.Function(this, `${proxy.id}ProxyFn`, {
    functionName: `{{project_name}}-${proxy.targetName}`,
    runtime: lambda.Runtime.PYTHON_3_13,
    architecture: lambda.Architecture.ARM_64,
    handler: 'handler.handler',
    code: lambda.Code.fromAsset('lambda/mcp_runtime_proxy'),
    role: proxyRole,
    timeout: cdk.Duration.seconds(120),
    memorySize: 512,
    environment: { RUNTIME_ARN: runtimeArn },
  });

  proxyFn.grantInvoke(gatewayRole);
  proxyFn.addPermission(`AllowGatewayInvoke${proxy.id}`, {
    principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
    sourceAccount: cdk.Aws.ACCOUNT_ID,
  });

  new agentcore.CfnGatewayTarget(this, `${proxy.id}Target`, {
    name: proxy.targetName,
    gatewayIdentifier: gateway.attrGatewayIdentifier,
    credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
    targetConfiguration: {
      mcp: { lambda: { lambdaArn: proxyFn.functionArn, toolSchema: { inlinePayload: toolSchemaData } } },
    },
  });
}
```

---

## Lambda Proxy Handler — Pass 3 Reference

```python
"""MCP Runtime Proxy — Lambda bridge: Gateway → AgentCore Runtime (MCP server).

Architecture: Gateway --[IAM]--> Lambda --[IAM]--> AgentCore Runtime (MCP)
"""
import json, logging, os, uuid
import boto3

logger = logging.getLogger()
RUNTIME_ARN = os.environ['RUNTIME_ARN']
agentcore_client = boto3.client('bedrock-agentcore')

def handler(event, context):
    # Extract tool name from Gateway context metadata
    DELIMITER = '___'
    tool_name = 'unknown'
    try:
        if hasattr(context, 'client_context') and context.client_context:
            custom = getattr(context.client_context, 'custom', {}) or {}
            raw = custom.get('bedrockAgentCoreToolName', 'unknown')
            tool_name = raw[raw.index(DELIMITER) + len(DELIMITER):] if DELIMITER in raw else raw
    except Exception:
        pass

    # Build MCP JSON-RPC tools/call request
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

    # Parse MCP JSON-RPC response
    if 'result' in result and 'content' in result.get('result', {}):
        texts = [c.get('text', '') for c in result['result']['content'] if c.get('type') == 'text']
        if texts:
            try: return json.loads(texts[0])
            except: return {'text': texts[0]}
    return result
```

---

## Tool Schema JSON Format

```json
// schemas/redshift_tools.json
[
  {
    "name": "get_pnl_history",
    "description": "Get P&L history by business unit and period",
    "inputSchema": {
      "type": "object",
      "properties": {
        "business_unit": { "type": "string", "description": "Business unit code" },
        "period": { "type": "string", "description": "Period: MTD, QTD, YTD, T12M" }
      },
      "required": ["period"]
    }
  }
]
```

---

## SigV4 Auth for Agent → Gateway — Pass 3 Reference

```python
"""SigV4 authentication for agent-to-gateway MCP calls."""
import boto3, httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.client.streamable_http import streamablehttp_client

class HTTPXSigV4Auth(httpx.Auth):
    def __init__(self, session, service, region):
        self.credentials = session.get_credentials().get_frozen_credentials()
        self.service = service
        self.region = region

    def auth_flow(self, request):
        aws_request = AWSRequest(method=request.method, url=str(request.url),
                                  data=request.content if hasattr(request, 'content') else b'')
        aws_request.headers['Host'] = request.url.host
        aws_request.headers['Content-Type'] = 'application/json'
        SigV4Auth(self.credentials, self.service, self.region).add_auth(aws_request)
        for name, value in aws_request.headers.items():
            request.headers[name] = value
        yield request

def create_gateway_transport(gateway_url: str):
    session = boto3.Session()
    auth = HTTPXSigV4Auth(session, 'bedrock-agentcore', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
    return streamablehttp_client(url=gateway_url, auth=auth)
```
