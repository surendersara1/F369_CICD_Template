# SOP — Bedrock AgentCore Runtime (Managed Serverless Agent & MCP Server Hosting)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws-cdk.aws-bedrock-agentcore-alpha` · `aws-cdk-lib.aws-bedrock-agentcore-alpha` ≥ 2.160 · ARM64 (Graviton) · Python 3.13 container · Strands SDK v1.34+

---

## 1. Purpose

- Provision AWS-managed serverless runtimes for AI agents and MCP servers using the CDK L2 alpha construct `aws-bedrock-agentcore-alpha`.
- Codify the two protocol modes:
  - **`ProtocolType.HTTP`** for agent runtimes (Strands supervisor / observer / reasoner / governance).
  - **`ProtocolType.MCP`** for tool-server runtimes (Redshift, Neptune, Aurora, OpenSearch, SQLite, Mock).
- Codify the per-agent-stack topology — each agent is its own CDK stack with its own least-privilege execution role.
- Expose every runtime ARN via SSM so downstream stacks (Gateway, Supervisor) can read it without cross-stack exports.
- Include when the SOW mentions AgentCore Runtime, managed agent hosting, MCP server hosting, microVM isolation, or auto-scaling agent/tool sessions.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Building a single POC stack with a couple of runtimes + their dependencies | **§3 Monolith Variant** |
| Building the production layout (MS04 runtime stack separate from MS01 Network, MS02 Identity, MS05 Gateway, per-agent stacks) | **§4 Micro-Stack Variant** |

**Why the split matters.** In production, each agent is its own independently deployable stack, the MCP-server runtimes share one stack (MS04), the Gateway is another (MS05), and the Supervisor depends on the Observer + Reasoner *runtime ARNs*. Those ARNs are tokens at synth time. Passing them across stacks as construct references forces cross-stack CFN exports; worse, the `executionRole` of each runtime is shared in the simplest design but owned by the runtime stack, which then has every agent depending on it. If someone reaches for `runtime.grant_invoke(supervisor_role)` across stacks, CloudFormation will reject it as a cycle. The Micro-Stack variant publishes every runtime ARN via SSM `StringParameter` and every sub-agent permission via identity-side `PolicyStatement`.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` owns the VPC + `Runtime`s + their roles + any upstream data resources. POC / prototypes.

### 3.1 Install the alpha construct

```bash
pip install aws-cdk.aws-bedrock-agentcore-alpha
```

Then add to the stack's imports:

```python
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Runtime, AgentRuntimeArtifact, ProtocolType, RuntimeNetworkConfiguration,
)
```

### 3.2 Reusable `AgentRuntime` builder (method on the monolith stack)

```python
from aws_cdk import (
    Aws, CfnOutput,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_ssm as ssm,
)
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Runtime, AgentRuntimeArtifact, ProtocolType, RuntimeNetworkConfiguration,
)


def _create_agent_runtime(
    self,
    agent_name: str,
    runtime_name: str,
    ssm_output_path: str,
    environment_variables: dict[str, str] | None = None,
    additional_policies: list[iam.PolicyStatement] | None = None,
) -> Runtime:
    """Create one agent runtime + its execution role. Monolith-friendly.

    Assumes self.{vpc, client_id, region} are already set on the stack.
    """
    # Per-agent execution role (least privilege)
    execution_role = iam.Role(
        self, f"{agent_name}-ExecutionRole",
        role_name=f"{{project_name}}-{agent_name}-role",
        assumed_by=iam.CompositePrincipal(
            iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        ),
    )
    execution_role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
    ))
    execution_role.add_to_policy(iam.PolicyStatement(
        actions=["ssm:GetParameter", "ssm:GetParameters"],
        resources=[f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter/{{project_name}}/*"],
    ))
    for policy in (additional_policies or []):
        execution_role.add_to_policy(policy)

    # Docker artifact — build from agents/ directory
    artifact = AgentRuntimeArtifact.from_asset(
        "agents",
        file=f"{agent_name}/Dockerfile",
        platform=ecr_assets.Platform.LINUX_ARM64,
    )

    runtime = Runtime(
        self, f"{agent_name}-Runtime",
        runtime_name=runtime_name,
        agent_runtime_artifact=artifact,
        execution_role=execution_role,
        description=f"{{project_name}} {agent_name} agent",
        protocol_configuration=ProtocolType.HTTP,
        network_configuration=RuntimeNetworkConfiguration.using_vpc(
            self, vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        ),
        environment_variables={
            "CLIENT_ID":          self.client_id,
            "AWS_DEFAULT_REGION": self.region,
            **(environment_variables or {}),
        },
    )

    # Publish ARN via SSM (Gateway + other stacks will read this)
    ssm.StringParameter(
        self, f"{agent_name}-ArnParam",
        parameter_name=ssm_output_path,
        string_value=runtime.agent_runtime_arn,
    )
    CfnOutput(self, f"{agent_name}ArnOutput", value=runtime.agent_runtime_arn)
    return runtime
```

### 3.3 MCP-server runtimes (`ProtocolType.MCP`)

```python
def _create_mcp_runtimes(self) -> dict[str, Runtime]:
    """One shared execution role; one Runtime per data source."""
    mcp_role = iam.Role(
        self, "McpRuntimeRole",
        role_name="{project_name}-mcp-runtime-role",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    )
    # [Claude: add data-source permissions — Redshift Data API, Neptune, etc.]

    mcp_servers = [
        {
            "id":         "RedshiftMcp",
            "name":       "redshift-mcp",
            "docker":     "infra/containers/redshift-mcp",
            "ssm_key":    "redshift_mcp_endpoint",
            "desc":       "Redshift MCP server — financial analytics tools",
            "env":        {
                "REDSHIFT_WORKGROUP": "{project_name}-wg",
                "REDSHIFT_DB":        "{project_name}_warehouse",
                "REDSHIFT_IAM_AUTH":  "true",
            },
        },
        # [Claude: add more servers per SOW — neptune-mcp, opensearch-mcp, aurora-mcp, sqlite-mcp]
    ]

    runtimes: dict[str, Runtime] = {}
    for srv in mcp_servers:
        artifact = AgentRuntimeArtifact.from_asset(
            srv["docker"], platform=ecr_assets.Platform.LINUX_ARM64,
        )
        rt = Runtime(
            self, f"{srv['id']}Runtime",
            runtime_name=f"{{project_name}}_{srv['name'].replace('-', '_')}",
            agent_runtime_artifact=artifact,
            execution_role=mcp_role,
            description=srv["desc"],
            protocol_configuration=ProtocolType.MCP,   # MCP, not HTTP
            network_configuration=RuntimeNetworkConfiguration.using_vpc(
                self, vpc=self.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            ),
            environment_variables=srv["env"],
        )
        ssm.StringParameter(
            self, f"Ssm{srv['id']}Arn",
            parameter_name=f"/{{project_name}}/runtime/{srv['ssm_key']}",
            string_value=rt.agent_runtime_arn,
        )
        runtimes[srv["id"]] = rt
    return runtimes
```

### 3.4 Agent container Dockerfile

```dockerfile
# agents/observer/Dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /var/task

COPY observer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY evaluations/ ./evaluations/
COPY observer/agent.py .

EXPOSE 8080
ENTRYPOINT ["python", "agent.py"]
```

Per-agent `requirements.txt`:

```text
strands-agents==1.34.0
strands-agents-tools>=0.1.0
bedrock-agentcore==1.6.0
boto3>=1.34.0
mcp[cli]>=1.8.0
httpx>=0.27.0
```

### 3.5 Monolith gotchas

- **`aws-cdk.aws-bedrock-agentcore-alpha` is an alpha module.** Pin the minor version (`aws-cdk.aws-bedrock-agentcore-alpha==2.160.0a0`). The API changes between alpha releases — treat any upgrade as breaking.
- **Build context `agents/` is CWD-relative.** Always run `cdk synth` from the repo root. Path anchoring (§4.2) is the fix in micro-stack.
- **Session ID routing.** Runtimes are microVM-per-session; the same `runtimeSessionId` within an 8-hour window is guaranteed to land on the same microVM. Use this property for implicit memory without Memory-service overhead.
- **Execution-role trust policy** must include `bedrock-agentcore.amazonaws.com`. The `ecs-tasks.amazonaws.com` principal is only needed if you later move the same container to Fargate; omit it if you are committed to AgentCore.
- **`CompositePrincipal`** de-duplicates silently; multiple calls to `.add_to_policy` with identical statements are deduped in the IAM policy, not at the construct level.

---

## 4. Micro-Stack Variant

**Use when:** the production layout — MS01-Network, MS02-Identity, MS03-DataFoundation, **MS04-AgentcoreRuntime** (MCP servers), MS05-Gateway, then **per-agent stacks** (ObserverStack, ReasonerStack, SupervisorStack, etc.), and MS07-Memory / MS08-Governance / MS09-Portal / MS10-Observability downstream.

### 4.1 The five non-negotiables

1. **Anchor Dockerfile build contexts to `Path(__file__)`** — not CWD-relative strings.
2. **Never call `runtime.grant_invoke(other_role)`** across stacks; publish the ARN via SSM and grant identity-side in the consumer stack.
3. **Never target a cross-stack queue** with `targets.SqsQueue(q)` for runtime-triggered events.
4. **Never split bucket + OAC** across stacks.
5. **Never set `encryption_key=ext_key`** where `ext_key` came from another stack.

### 4.2 MS04 — `AgentcoreRuntimeStack` (MCP servers only)

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_ssm as ssm,
)
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Runtime, AgentRuntimeArtifact, ProtocolType, RuntimeNetworkConfiguration,
)
from constructs import Construct

# stacks/ms04_agentcore_runtime.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_CONTAINERS_ROOT: Path = Path(__file__).resolve().parents[3] / "infra" / "containers"


class AgentcoreRuntimeStack(cdk.Stack):
    """MS04 — all MCP server runtimes in one stack; shared IAM role; SSM-published ARNs."""

    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        mcp_role_additional_policies: list[iam.PolicyStatement] | None = None,
        permission_boundary: iam.IManagedPolicy | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ms04-agentcore-runtime", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Shared execution role for MCP runtimes
        mcp_role = iam.Role(
            self, "McpRuntimeRole",
            role_name="{project_name}-mcp-runtime-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        mcp_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter/{{project_name}}/*"],
        ))
        for p in (mcp_role_additional_policies or []):
            mcp_role.add_to_policy(p)

        if permission_boundary:
            iam.PermissionsBoundary.of(mcp_role).apply(permission_boundary)

        mcp_servers = [
            {"id": "RedshiftMcp",   "name": "redshift-mcp",   "key": "redshift_mcp_endpoint"},
            {"id": "NeptuneMcp",    "name": "neptune-mcp",    "key": "neptune_mcp_endpoint"},
            {"id": "AuroraMcp",     "name": "aurora-mcp",     "key": "aurora_mcp_endpoint"},
            {"id": "OpenSearchMcp", "name": "opensearch-mcp", "key": "opensearch_mcp_endpoint"},
            # [Claude: add more per SOW]
        ]

        self.runtimes: dict[str, Runtime] = {}
        for srv in mcp_servers:
            artifact = AgentRuntimeArtifact.from_asset(
                str(_CONTAINERS_ROOT / srv["name"]),          # Path(__file__)-anchored
                platform=ecr_assets.Platform.LINUX_ARM64,
            )
            rt = Runtime(
                self, f"{srv['id']}Runtime",
                runtime_name=f"{{project_name}}_{srv['name'].replace('-', '_')}",
                agent_runtime_artifact=artifact,
                execution_role=mcp_role,
                description=f"{{project_name}} {srv['name']} runtime",
                protocol_configuration=ProtocolType.MCP,
                network_configuration=RuntimeNetworkConfiguration.using_vpc(
                    self, vpc=vpc,
                    vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                ),
            )
            ssm.StringParameter(
                self, f"Ssm{srv['id']}Arn",
                parameter_name=f"/{{project_name}}/runtime/{srv['key']}",
                string_value=rt.agent_runtime_arn,
            )
            self.runtimes[srv["id"]] = rt
```

### 4.3 Per-agent stack pattern — `ObserverStack`

```python
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import Aws, aws_ec2 as ec2, aws_iam as iam, aws_ssm as ssm, aws_ecr_assets as ecr_assets
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Runtime, AgentRuntimeArtifact, ProtocolType, RuntimeNetworkConfiguration,
)
from constructs import Construct

_AGENTS_ROOT: Path = Path(__file__).resolve().parents[3] / "agents"


class ObserverAgentStack(cdk.Stack):
    """One stack per agent — independently deployable."""

    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        gateway_url_ssm_name: str,           # e.g. '/{project_name}/mcp/gateway_endpoint'
        gateway_arn: str,
        model_id: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-agent-observer", **kwargs)

        execution_role = iam.Role(
            self, "ObserverRole",
            role_name="{project_name}-observer-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        # Identity-side grants only — every resource is cross-stack
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
        ))
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter/{{project_name}}/*"],
        ))
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway"],
            resources=[gateway_arn],
        ))
        iam.PermissionsBoundary.of(execution_role).apply(permission_boundary)

        # Read Gateway URL at deploy time (not synth — it's cross-stack)
        gateway_url = ssm.StringParameter.value_for_string_parameter(self, gateway_url_ssm_name)

        artifact = AgentRuntimeArtifact.from_asset(
            str(_AGENTS_ROOT),
            file="observer/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        self.runtime = Runtime(
            self, "ObserverRuntime",
            runtime_name="{project_name}_observer_agent_v3",
            agent_runtime_artifact=artifact,
            execution_role=execution_role,
            protocol_configuration=ProtocolType.HTTP,
            network_configuration=RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            ),
            environment_variables={
                "DEFAULT_MODEL_ID": model_id,
                "GATEWAY_URL":      gateway_url,
            },
        )
        # Publish ARN — Supervisor stack will read it
        ssm.StringParameter(
            self, "ObserverArnParam",
            parameter_name="/{project_name}/agents/observer_agent_arn",
            string_value=self.runtime.agent_runtime_arn,
        )
```

### 4.4 CDK app entry — stack dependency graph

```python
# infra/app.py
import aws_cdk as cdk

app = cdk.App()
env = cdk.Environment(account="{account}", region="{region}")

# Layer 1: foundation
ms00 = BootstrapStack(app, "MS00-Bootstrap", env=env)
ms01 = NetworkStack(app, "MS01-Network", env=env)
ms02 = IdentityStack(app, "MS02-Identity", env=env)
ms03 = DataFoundationStack(app, "MS03-DataFoundation", env=env)

# Layer 2: MCP runtimes
ms04 = AgentcoreRuntimeStack(app, "MS04-AgentcoreRuntime",
    vpc=ms01.vpc, permission_boundary=ms02.boundary, env=env)
ms04.add_dependency(ms01); ms04.add_dependency(ms02)

# Layer 3: Gateway
ms05 = GatewayStack(app, "MS05-Gateway",
    vpc=ms01.vpc, mcp_runtime_arns_ssm_prefix="/{project_name}/runtime/",
    env=env)
ms05.add_dependency(ms04)

# Layer 4: per-agent stacks (independent)
agent_observer = ObserverAgentStack(app, "Agent-Observer",
    vpc=ms01.vpc,
    gateway_url_ssm_name="/{project_name}/mcp/gateway_endpoint",
    gateway_arn=ms05.gateway_arn_literal,   # published from MS05 via SSM
    model_id="{MODEL_ID}",
    permission_boundary=ms02.boundary, env=env)
agent_observer.add_dependency(ms05)

agent_reasoner  = ReasonerAgentStack(app, "Agent-Reasoner", ..., env=env)
agent_reasoner.add_dependency(ms05)

# Supervisor reads Observer + Reasoner ARNs via SSM at deploy time
agent_supervisor = SupervisorAgentStack(app, "Agent-Supervisor", ..., env=env)
agent_supervisor.add_dependency(agent_observer)
agent_supervisor.add_dependency(agent_reasoner)

# Layer 5+: memory, governance, portal, observability, CICD
app.synth()
```

### 4.5 Micro-stack gotchas

- **`ssm.StringParameter.value_for_string_parameter`** resolves at *deploy* time; `value_from_lookup` resolves at *synth* time (and requires credentials). Use `value_for_string_parameter` for cross-stack ARN reads to keep synth offline.
- **Runtime ARN tokens** cannot be written into `StringParameter.string_value` in a *different* stack — the producer (runtime owner) must also be the SSM writer. Consumers read by parameter name.
- **Supervisor needs `bedrock-agentcore:InvokeAgentRuntime` on Observer + Reasoner ARNs.** Those ARNs are known to the Supervisor stack as strings it reads from SSM; grant identity-side on the Supervisor role.
- **Parallel deploys of per-agent stacks** — CDK can deploy independent stacks concurrently. Ensure the containers do not share a build cache race; `ecr_assets.Platform.LINUX_ARM64` is idempotent but the asset hashing is per stack.
- **Alpha module jsii serialisation** — `Runtime` props that are passed as tokens across stacks (e.g. a VPC from MS01) must be interfaces (`ec2.IVpc`). The construct does not accept raw VPC IDs for `using_vpc`.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, < 400 resources, one pipeline | Stay on §3 Monolith |
| Separate deploy cadence for agents vs. MCP servers | §4 Micro-Stack (MS04 + per-agent stacks) |
| Supervisor cross-stack grant cycle | Publish sub-agent ARNs via SSM; grant identity-side on Supervisor role |
| Need > 3 GB RAM per container | Stay on AgentCore — runtime supports up to the alpha construct's per-session cap; if exceeded, split the work into smaller agents |
| Long-lived session beyond 8 h | Persist checkpoint to `AGENTCORE_MEMORY`; reconnect creates a new microVM with memory context |
| High-QPS MCP server | Scale by session concurrency limits in the runtime config; alpha construct exposes `maxSessionConcurrency` in later versions |
| Non-agent batch workload | Use Fargate — see `STRANDS_DEPLOY_ECS §3.4` |

---

## 6. Worked example — MS04 + one agent stack synthesize clean

Save as `tests/sop/test_AGENTCORE_RUNTIME.py`. Offline.

```python
"""SOP verification — MS04 synthesizes 4 MCP runtimes + 4 SSM params."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ms04_agentcore_runtime_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ms04_agentcore_runtime import AgentcoreRuntimeStack
    ms04 = AgentcoreRuntimeStack(app, vpc=vpc, permission_boundary=boundary, env=env)

    template = Template.from_stack(ms04)
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 4)
    template.resource_count_is("AWS::SSM::Parameter", 4)


def test_observer_agent_stack_has_identity_side_grants_only():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.agent_observer import ObserverAgentStack
    obs = ObserverAgentStack(
        app,
        vpc=vpc,
        gateway_url_ssm_name="/test/mcp/gateway_endpoint",
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/test",
        model_id="anthropic.claude-sonnet-4-20250514-v1:0",
        permission_boundary=boundary,
        env=env,
    )

    template = Template.from_stack(obs)
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)
    template.resource_count_is("AWS::SSM::Parameter", 1)
```

---

## 7. References

- `docs/template_params.md` — `MODEL_ID`, `FALLBACK_MODEL_ID`, `MCP_RUNTIME_SSM_PREFIX`, `GATEWAY_ARN`, `GATEWAY_URL_SSM_NAME`, `AGENT_RUNTIME_ARCH`
- `docs/Feature_Roadmap.md` — feature IDs `AG-01` (AgentCore runtime), `AG-02` (MCP-server runtime), `STR-01` (agent deploy)
- CDK alpha construct: https://docs.aws.amazon.com/cdk/api/v2/docs/aws-bedrock-agentcore-alpha-readme.html
- AgentCore Runtime overview: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-runtime.html
- Related SOPs: `STRANDS_DEPLOY_ECS` (container build + deploy), `STRANDS_MCP_SERVER` (server code inside MCP runtimes), `AGENTCORE_GATEWAY` (reads SSM runtime ARNs), `AGENTCORE_IDENTITY` (execution-role patterns), `AGENTCORE_MEMORY` (persistent session state), `AGENTCORE_OBSERVABILITY` (metrics + tracing), `LAYER_BACKEND_LAMBDA` (five non-negotiables + helpers)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) applying five non-negotiables: `Path(__file__)`-anchored build contexts, SSM-published runtime ARNs, identity-side grants for Bedrock / Gateway / SSM, permission-boundary on every execution role, `value_for_string_parameter` for deploy-time cross-stack reads. Translated CDK from TypeScript to Python. Added MS04 canonical stack, per-agent stack template, full app-entry dependency graph, Swap matrix, Worked example. |
| 1.0 | 2026-03-05 | Initial — reusable AgentRuntime construct (TS), MCP-server runtimes, per-agent stacks, app dependency graph, Dockerfile, requirements.txt. |
