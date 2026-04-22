# PARTIAL: AgentCore Identity — IAM SigV4, Cognito, Per-Agent Roles

**Usage:** Include when SOW mentions agent authentication, IAM roles, SigV4 signing, Cognito for portal users, or per-agent least-privilege.

---

## Identity Architecture (from real production)

```
Two auth patterns:
  1. IAM SigV4 (agent-to-gateway): Agents authenticate to Gateway via SigV4
     - No secrets to manage, no token rotation
     - Per-agent IAM roles with least-privilege policies
     - HTTPXSigV4Auth class for MCP streamable HTTP transport

  2. Cognito (portal users): Human users authenticate via Cognito
     - User Pool with MFA, password policy, groups (RBAC)
     - JWT tokens for API Gateway + WebSocket authorization
     - Per-persona RBAC policies loaded from DynamoDB

Production uses IAM SigV4 for all agent-to-service communication.
Cognito is only for the human-facing portal.
```

---

## CDK: Per-Agent IAM Roles — Pass 2A Reference

```typescript
// Each agent gets its own least-privilege IAM role
const executionRole = new iam.Role(this, 'ExecutionRole', {
  roleName: `{{project_name}}-${agentName}-role`,
  assumedBy: new iam.CompositePrincipal(
    new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
    new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
  ),
});

// Base: Bedrock model invocation
executionRole.addToPolicy(new iam.PolicyStatement({
  actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
  resources: [`arn:aws:bedrock:${cdk.Aws.REGION}::foundation-model/*`],
}));

// Base: SSM parameter read
executionRole.addToPolicy(new iam.PolicyStatement({
  actions: ['ssm:GetParameter', 'ssm:GetParameters'],
  resources: [`arn:aws:ssm:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:parameter/{{project_name}}/*`],
}));

// Agent-specific: Gateway invoke (only agents that need tools)
executionRole.addToPolicy(new iam.PolicyStatement({
  actions: ['bedrock-agentcore:InvokeGateway'],
  resources: ['*'],
}));

// Agent-specific: Sub-agent invocation (only supervisor)
executionRole.addToPolicy(new iam.PolicyStatement({
  actions: ['bedrock-agentcore:InvokeAgentRuntime'],
  resources: ['*'],
}));

// Agent-specific: Memory (only agents with memory)
executionRole.addToPolicy(new iam.PolicyStatement({
  actions: ['bedrock-agentcore:RetrieveMemoryRecords', 'bedrock-agentcore:CreateEvent'],
  resources: ['*'],
}));
```

---

## SigV4 Auth for Agent → Gateway — Pass 3 Reference

```python
"""SigV4 authentication for agent-to-gateway MCP calls."""
import os, boto3, httpx
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
    auth = HTTPXSigV4Auth(session, 'bedrock-agentcore',
                           os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
    return streamablehttp_client(url=gateway_url, auth=auth)
```

---

## CDK: Cognito for Portal Users — Pass 2A Reference

```typescript
// Cognito User Pool for human portal users (not agents)
const userPool = new cognito.UserPool(this, 'UserPool', {
  userPoolName: `{{project_name}}-users`,
  signInAliases: { email: true },
  selfSignUpEnabled: false,  // Admin-created users only
  mfa: cognito.Mfa.REQUIRED,
  mfaSecondFactor: { otp: true, sms: false },
  passwordPolicy: {
    minLength: 12, requireUppercase: true, requireLowercase: true,
    requireDigits: true, requireSymbols: true,
  },
  advancedSecurityMode: cognito.AdvancedSecurityMode.ENFORCED,
});

// User Pool Groups (RBAC personas)
for (const group of ['cfo', 'vp_finance', 'director', 'analyst', 'auditor']) {
  new cognito.CfnUserPoolGroup(this, `Group${group}`, {
    userPoolId: userPool.userPoolId,
    groupName: group,
  });
}
```
