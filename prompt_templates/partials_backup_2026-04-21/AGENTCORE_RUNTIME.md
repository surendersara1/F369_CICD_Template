# PARTIAL: AgentCore Runtime — Managed Serverless Agent & MCP Server Hosting

**Usage:** Include when SOW mentions AgentCore Runtime, managed agent hosting, MCP server hosting, microVM isolation, or auto-scaling agent/tool sessions.

---

## AgentCore Runtime Overview

```
AgentCore Runtime = AWS-managed serverless runtime for AI agents AND MCP servers:
  - Dedicated microVM per session (complete isolation)
  - 8-hour session persistence window
  - Auto-scales to thousands of sessions in seconds
  - Two protocol modes:
    ProtocolType.HTTP  → Agent runtimes (Strands agents, LangChain, etc.)
    ProtocolType.MCP   → MCP server runtimes (tool servers: Redshift, Neptune, etc.)
  - ARM64 Graviton containers (cost optimized)
  - VPC connectivity for private data sources
  - Pay only for actual compute usage

Architecture Pattern (from real production):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  CDK App (TypeScript)                                               │
  │                                                                     │
  │  MS00-Bootstrap → MS01-Network → MS02-Identity → MS03-DataFoundation│
  │       ↓                                                             │
  │  MS04-AgentcoreRuntime  ← MCP server runtimes (Redshift, Neptune,  │
  │       ↓                    OpenSearch, Aurora, SQLite, Mock)         │
  │  MS05-Gateway           ← Lambda proxy targets + direct Lambda tools│
  │       ↓                                                             │
  │  Agent-Observer  ┐                                                  │
  │  Agent-Reasoner  ├─ Per-agent stacks (independently deployable)     │
  │  Agent-Governance┘                                                  │
  │       ↓                                                             │
  │  Agent-Supervisor       ← Orchestrates all sub-agents               │
  │       ↓                                                             │
  │  MS07-Memory → MS08-Governance → MS09-Portal → MS10-Observability  │
  └─────────────────────────────────────────────────────────────────────┘

Key Insight: Each agent = its own CDK stack. Add 100 agents = add 100 stacks.
             Each MCP server = its own AgentCore Runtime with ProtocolType.MCP.
             Gateway routes tool calls to MCP Runtimes via Lambda proxy pattern.
```

---

## CDK L2 Alpha Construct — `@aws-cdk/aws-bedrock-agentcore-alpha`

```bash
npm install @aws-cdk/aws-bedrock-agentcore-alpha
```

---

## Reusable AgentRuntime Construct — Pass 2A Reference

This is the core reusable construct. Every agent stack uses it.

```typescript
// infra/lib/constructs/agent-runtime.ts
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as assets from 'aws-cdk-lib/aws-ecr-assets';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import { RuntimeNetworkConfiguration } from '@aws-cdk/aws-bedrock-agentcore-alpha';
import { Construct } from 'constructs';

export interface AgentRuntimeProps {
  agentName: string;       // e.g. 'supervisor', 'observer'
  runtimeName: string;     // e.g. 'supervisor_agent_v4'
  ssmOutputPath: string;   // e.g. '/{{project_name}}/agents/supervisor_agent_arn'
  environmentVariables?: Record<string, string>;
  additionalPolicies?: iam.PolicyStatement[];
}

export class AgentRuntime extends Construct {
  public readonly runtime: agentcore.Runtime;
  public readonly executionRole: iam.Role;

  constructor(scope: Construct, id: string, props: AgentRuntimeProps) {
    super(scope, id);

    const { agentName, runtimeName, ssmOutputPath } = props;

    // Import VPC from foundation stacks via SSM
    const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
      vpcId: ssmLookup(this, '/{{project_name}}/network/vpc_id'),
      availabilityZones: [/* from context */],
      privateSubnetIds: [/* from SSM */],
    });

    // Per-agent execution role (least privilege)
    this.executionRole = new iam.Role(this, 'ExecutionRole', {
      roleName: `{{project_name}}-${agentName}-role`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      ),
    });

    // Base permissions all agents need
    this.executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [`arn:aws:bedrock:${cdk.Aws.REGION}::foundation-model/*`],
    }));

    this.executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters'],
      resources: [`arn:aws:ssm:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:parameter/{{project_name}}/*`],
    }));

    // Agent-specific policies
    if (props.additionalPolicies) {
      for (const policy of props.additionalPolicies) {
        this.executionRole.addToPolicy(policy);
      }
    }

    // Docker container artifact — build from agents/ directory
    const artifact = agentcore.AgentRuntimeArtifact.fromAsset('agents/', {
      platform: assets.Platform.LINUX_ARM64,
      file: `${agentName}/Dockerfile`,
    });

    // AgentCore Runtime (HTTP protocol for agents)
    this.runtime = new agentcore.Runtime(this, 'Runtime', {
      runtimeName,
      agentRuntimeArtifact: artifact,
      executionRole: this.executionRole,
      description: `{{project_name}} ${agentName} agent`,
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      networkConfiguration: RuntimeNetworkConfiguration.usingVpc(this, {
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      }),
      environmentVariables: {
        CLIENT_ID: '{{client_id}}',
        AWS_DEFAULT_REGION: '{{aws_region}}',
        ...props.environmentVariables,
      },
    });

    // Export runtime ARN to SSM for cross-stack reference
    new ssm.StringParameter(this, `Ssm${agentName}Arn`, {
      parameterName: ssmOutputPath,
      stringValue: this.runtime.agentRuntimeArn,
    });
  }
}
```

---

## MCP Server Runtimes — Pass 2A Reference

MCP servers deployed as AgentCore Runtimes with `ProtocolType.MCP`:

```typescript
// infra/lib/stacks/runtimes/ms-04-agentcore-runtime-stack.ts

// Shared execution role for all MCP server runtimes
const mcpRuntimeRole = new iam.Role(this, 'McpRuntimeRole', {
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
});

// [Claude: add permissions based on SOW data sources — Redshift, Neptune, etc.]

// MCP Server definitions — one Runtime per data source
const mcpServers = [
  {
    id: 'RedshiftMcp',
    name: 'redshift-mcp',
    dockerDir: 'infra/containers/redshift-mcp',
    ssmEndpointKey: 'redshift_mcp_endpoint',
    description: 'Redshift MCP Server — financial analytics tools',
    envVars: {
      REDSHIFT_WORKGROUP: '{{project_name}}-wg',
      REDSHIFT_DB: '{{project_name}}_warehouse',
      REDSHIFT_IAM_AUTH: 'true',
    },
  },
  // [Claude: add more MCP servers per SOW data sources:
  //  neptune-mcp, opensearch-mcp, aurora-mcp, sqlite-mcp, etc.]
];

for (const server of mcpServers) {
  const artifact = agentcore.AgentRuntimeArtifact.fromAsset(server.dockerDir, {
    platform: assets.Platform.LINUX_ARM64,
  });

  const runtime = new agentcore.Runtime(this, `${server.id}Runtime`, {
    runtimeName: `{{project_name}}_${server.name.replace(/-/g, '_')}`,
    agentRuntimeArtifact: artifact,
    executionRole: mcpRuntimeRole,
    description: server.description,
    protocolConfiguration: agentcore.ProtocolType.MCP,  // ← MCP protocol
    networkConfiguration: RuntimeNetworkConfiguration.usingVpc(this, {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    }),
    environmentVariables: server.envVars,
  });

  // Export Runtime ARN to SSM — Gateway reads these
  ssmPut(this, `Ssm${server.id}Arn`,
    `/{{project_name}}/runtime/${server.ssmEndpointKey}`,
    runtime.agentRuntimeArn);
}
```

---

## Per-Agent Stack Pattern — Pass 2A Reference

Each agent is its own independently deployable CDK stack:

```typescript
// infra/lib/stacks/agents/observer-stack.ts
import { AgentRuntime } from '../../constructs/agent-runtime';

export class ObserverAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AgentStackProps) {
    super(scope, id, props);

    new AgentRuntime(this, 'Observer', {
      agentName: 'observer',
      runtimeName: 'observer_agent_v3',
      ssmOutputPath: '/{{project_name}}/agents/observer_agent_arn',
      environmentVariables: {
        GATEWAY_URL: ssmLookup(this, '/{{project_name}}/mcp/gateway_endpoint'),
      },
      additionalPolicies: [
        new iam.PolicyStatement({
          actions: ['bedrock-agentcore:InvokeGateway'],
          resources: ['*'],
        }),
      ],
    });
  }
}
```

---

## CDK App Entry — Stack Dependency Graph

```typescript
// infra/bin/app.ts
// LAYER 1: Foundation (deploy first, rarely changes)
const ms00 = new BootstrapStack(app, 'MS00-Bootstrap', { env });
const ms01 = new NetworkStack(app, 'MS01-Network', { env });
const ms02 = new IdentityStack(app, 'MS02-Identity', { env });
const ms03 = new DataFoundationStack(app, 'MS03-DataFoundation', { env });

// LAYER 2: MCP Runtimes
const ms04 = new AgentcoreRuntimeStack(app, 'MS04-AgentcoreRuntime', { env });
ms04.addDependency(ms01); ms04.addDependency(ms02);

// LAYER 3: Gateway
const ms05 = new GatewayStack(app, 'MS05-Gateway', { env });
ms05.addDependency(ms04);

// LAYER 4: Agents (independently deployable)
const agentObserver = new ObserverAgentStack(app, 'Agent-Observer', { env });
agentObserver.addDependency(ms05);

const agentReasoner = new ReasonerAgentStack(app, 'Agent-Reasoner', { env });
agentReasoner.addDependency(ms05);

const agentSupervisor = new SupervisorAgentStack(app, 'Agent-Supervisor', { env });
agentSupervisor.addDependency(agentObserver);
agentSupervisor.addDependency(agentReasoner);

// LAYER 5+: Memory, Governance, Portal, Observability, CICD
```

---

## Agent Dockerfile — Pass 3 Reference

```dockerfile
# agents/observer/Dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY shared/ ./shared/
COPY observer/ ./observer/
COPY observer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "observer.agent"]
```

---

## requirements.txt (per agent)

```
strands-agents>=0.1.0
strands-agents-tools>=0.1.0
bedrock-agentcore>=0.1.0
boto3>=1.35.0
```
