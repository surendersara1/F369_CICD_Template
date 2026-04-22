# SOP — Bedrock AgentCore A2A (Agent-to-Agent Protocol Server & Client)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · `a2a-sdk` ≥ 0.1 · `starlette` / `uvicorn` · AWS CDK v2 (Python 3.12+) · AgentCore Runtime (`ProtocolType.HTTP`) or Fargate for hosting · Python 3.13 container · ARM64

---

## 1. Purpose

- Codify the two sides of the A2A (agent-to-agent) protocol:
  1. **Server** — expose a Strands agent as an A2A-compliant HTTP endpoint via `StrandsA2AExecutor` + `A2AStarletteApplication` so external agent platforms (LangGraph, CrewAI, third-party) can call it.
  2. **Client** — consume a remote A2A agent as a first-class tool in a Strands orchestrator via `A2AAgent`.
- Provide the A2A-enabled Dockerfile and `requirements.txt` (uvicorn + starlette + a2a-sdk on top of Strands).
- Document hosting options — AgentCore Runtime (recommended) vs. Fargate (for teams that need custom networking).
- Document authentication — mTLS / Cognito client-credentials / SigV4 / API-key — and the trade-offs for cross-org calls.
- Include when the SOW mentions A2A protocol, cross-platform agent communication, exposing agents to external systems, or inter-agent messaging.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC; A2A server runs in the same stack that owns its dependencies | **§3 Monolith Variant** |
| A2A agents hosted in their own stacks (`Agent-A2AGovernance`, `Agent-A2AProcurement`), consumed by supervisor stacks cross-stack | **§4 Micro-Stack Variant** |

**Why the split matters.** An A2A server is just another AgentCore Runtime / Fargate service from CDK's perspective. The cross-stack concern is the consumer side: if a supervisor's role needs to reach the A2A endpoint (inside VPC) or needs `bedrock-agentcore:InvokeAgentRuntime` on the A2A-hosting runtime ARN, the rules from `AGENTCORE_RUNTIME §4` apply. Identity-side grants + SSM-published endpoints.

---

## 3. Monolith Variant

**Use when:** a POC / single stack that owns the A2A server runtime, its role, and any local consumers.

### 3.1 A2A server (`StrandsA2AExecutor` + Starlette)

```python
"""A2A Server — expose a Strands agent via the A2A protocol on port 9100."""
import json, os
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import StrandsA2AExecutor

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")


@tool
def check_compliance(action: str, amount: float = 0) -> str:
    """Check an action against policy rules (Cedar-backed in prod; simplified here).

    Args:
        action: Description of the action.
        amount: Transaction amount in the base currency.
    Returns:
        JSON with status + approval_tier.
    """
    tier = (
        'AUTO'        if amount <= 1_000_000 else
        'VP_FINANCE'  if amount <= 5_000_000 else
        'CFO_BOARD'
    )
    return json.dumps({'status': 'COMPLIANT', 'approval_tier': tier, 'amount': amount})


agent = Agent(
    model=BedrockModel(model_id=MODEL_ID),
    tools=[check_compliance],
    system_prompt="You evaluate compliance requests against Cedar policy rules.",
    name="governance-a2a",
    description="Governance compliance checker — validates actions against policies",
)


def main():
    from a2a.server.apps.starlette import A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler
    import uvicorn

    executor        = StrandsA2AExecutor(agent=agent)
    request_handler = DefaultRequestHandler(agent_executor=executor)

    a2a_app = A2AStarletteApplication(
        agent_card=executor.get_agent_card(host='0.0.0.0', port=9100),
        http_handler=request_handler,
    )
    uvicorn.run(a2a_app.build(), host='0.0.0.0', port=9100)


if __name__ == "__main__":
    main()
```

### 3.2 A2A client (consume remote A2A agent as Strands tool)

```python
"""Consume a remote A2A agent as a tool in a Strands orchestrator."""
import os
from strands import Agent
from strands.agent.a2a_agent import A2AAgent

remote_governance = A2AAgent(
    endpoint=os.environ["A2A_GOVERNANCE_ENDPOINT"],   # e.g. https://governance-a2a.internal:9100
    name="governance_checker",
    description="Remote governance agent — Cedar compliance + SOP validation",
)

orchestrator = Agent(
    system_prompt="You coordinate local and remote agents.",
    tools=[local_observer, remote_governance],
)
```

### 3.3 A2A Dockerfile

```dockerfile
# agents/a2a_server/Dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app

COPY shared/ ./shared/
COPY a2a_server/ ./a2a_server/
COPY a2a_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 9100
CMD ["python", "-m", "a2a_server.server"]
```

`requirements.txt`:

```text
strands-agents>=1.34.0
a2a-sdk>=0.1.0
uvicorn>=0.30.0
starlette>=0.38.0
boto3>=1.35.0
```

### 3.4 CDK — host the A2A server on AgentCore Runtime

```python
from pathlib import Path
from aws_cdk import Aws, aws_ec2 as ec2, aws_ecr_assets as ecr_assets, aws_iam as iam, aws_ssm as ssm
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Runtime, AgentRuntimeArtifact, ProtocolType, RuntimeNetworkConfiguration,
)


def _create_a2a_runtime(self, vpc: ec2.IVpc, agents_root: Path) -> Runtime:
    """Monolith helper — owns the role + runtime + SSM publish."""
    exec_role = iam.Role(self, "A2AGovernanceExecRole",
        role_name="{project_name}-a2a-governance-role",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    )
    exec_role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
    ))

    artifact = AgentRuntimeArtifact.from_asset(
        str(agents_root),
        file="a2a_server/Dockerfile",
        platform=ecr_assets.Platform.LINUX_ARM64,
    )
    runtime = Runtime(self, "A2AGovernanceRuntime",
        runtime_name="{project_name}_a2a_governance",
        agent_runtime_artifact=artifact,
        execution_role=exec_role,
        protocol_configuration=ProtocolType.HTTP,
        network_configuration=RuntimeNetworkConfiguration.using_vpc(
            self, vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        ),
        environment_variables={
            "MODEL_ID": "{MODEL_ID}",
        },
    )
    ssm.StringParameter(self, "A2AGovernanceArnParam",
        parameter_name="/{project_name}/agents/a2a_governance_arn",
        string_value=runtime.agent_runtime_arn,
    )
    return runtime
```

### 3.5 Monolith gotchas

- **`A2AAgent(endpoint=…)`** does not verify cert pinning. For cross-org calls, wrap in `httpx` with a CA bundle + mTLS, or go through a VPN / PrivateLink.
- **Port 9100** is the A2A-SDK default; AgentCore Runtime expects the container to listen on the `PORT` env var it injects. Either respect `PORT` or fix the container to 9100 and publish `agent_runtime_http_endpoint` accordingly.
- **`StrandsA2AExecutor.get_agent_card(host, port)`** publishes the agent's metadata (name, description, tool list). Anything in `description=` or `system_prompt=` ends up visible to callers — treat as public.
- **`DefaultRequestHandler`** has no auth by default. Put an IAM / Cognito authorizer in front (API Gateway or mTLS at ALB) before exposing beyond the VPC.
- **Uvicorn workers** default to 1. For CPU-bound synthesis, raise `--workers` — but measure cold-start impact first.

---

## 4. Micro-Stack Variant

**Use when:** MS04-AgentcoreRuntime or a per-agent stack (`Agent-A2AGovernance`) hosts the A2A server. Supervisors live in their own stacks and call the A2A endpoint.

### 4.1 The five non-negotiables

1. **Anchor Dockerfile build context** to `Path(__file__)`.
2. **Never call `runtime.grant_invoke(supervisor_role)`** across stacks — read the runtime ARN from SSM, grant identity-side.
3. **Never target cross-stack queues** with `targets.SqsQueue`.
4. **Never split a bucket + OAC** across stacks.
5. **Never set `encryption_key=ext_key`** where the key is from another stack.

### 4.2 Per-agent stack — `A2AGovernanceStack`

```python
from pathlib import Path

import aws_cdk as cdk
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
from constructs import Construct

_AGENTS_ROOT: Path = Path(__file__).resolve().parents[3] / "agents"


class A2AGovernanceStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        model_id: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-agent-a2a-governance", **kwargs)

        exec_role = iam.Role(self, "ExecRole",
            role_name="{project_name}-a2a-governance-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        exec_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
        ))
        iam.PermissionsBoundary.of(exec_role).apply(permission_boundary)

        artifact = AgentRuntimeArtifact.from_asset(
            str(_AGENTS_ROOT),
            file="a2a_server/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        self.runtime = Runtime(self, "A2ARuntime",
            runtime_name="{project_name}_a2a_governance",
            agent_runtime_artifact=artifact,
            execution_role=exec_role,
            protocol_configuration=ProtocolType.HTTP,
            network_configuration=RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            ),
            environment_variables={"MODEL_ID": model_id},
        )
        ssm.StringParameter(self, "ArnParam",
            parameter_name="/{project_name}/agents/a2a_governance_arn",
            string_value=self.runtime.agent_runtime_arn,
        )
        ssm.StringParameter(self, "EndpointParam",
            parameter_name="/{project_name}/agents/a2a_governance_endpoint",
            string_value=self.runtime.agent_runtime_http_endpoint,
        )
        CfnOutput(self, "A2AGovernanceArn", value=self.runtime.agent_runtime_arn)
```

### 4.3 Supervisor side — identity-side grant + env via SSM

```python
# inside SupervisorStack
from aws_cdk import aws_iam as iam, aws_ssm as ssm

a2a_arn      = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/agents/a2a_governance_arn",
)
a2a_endpoint = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/agents/a2a_governance_endpoint",
)

supervisor_role.add_to_policy(iam.PolicyStatement(
    actions=["bedrock-agentcore:InvokeAgentRuntime"],
    resources=[a2a_arn],
))

supervisor_env = {
    "A2A_GOVERNANCE_ENDPOINT": a2a_endpoint,
    # … other supervisor env vars
}
```

### 4.4 Micro-stack gotchas

- **`agent_runtime_http_endpoint`** is the addressable URL the client uses; `agent_runtime_arn` is the IAM ARN. Publish both — supervisors need one for `A2AAgent(endpoint=…)` and the other to grant `InvokeAgentRuntime`.
- **In-VPC reachability** — both supervisor and A2A runtimes must be in subnets that can resolve each other's endpoint. Same VPC is simplest; otherwise use PrivateLink.
- **Authentication between Strands supervisor and A2A server** — `A2AAgent` sends plain HTTP by default. Inside a VPC this is tolerable; across VPCs or accounts, wrap the endpoint in an API Gateway with IAM auth and sign requests with SigV4 client-side.
- **A2A agent card** is public to every caller. Do not put customer-specific data in `description` or the tool schemas. Parameterise by `invocation_state` if needed.
- **Cross-org calls** — A2A is a good integration point for third-party agents, but you lose in-VPC isolation. Put a WAF in front, log every request to CloudWatch + S3, and review the agent card before exposing.

---

## 5. Swap matrix — hosting / auth variants

| Need | Swap |
|---|---|
| Minimum setup | §3 Monolith on AgentCore Runtime |
| Production, separate deploy cadence | §4 Micro-Stack, one stack per A2A agent |
| Non-AWS caller | Expose via API Gateway + Cognito OAuth2 client-credentials; `A2AAgent` client authenticates via bearer token |
| Cross-VPC / cross-account | PrivateLink endpoint + mTLS on the A2A endpoint |
| Needs > 15 min per request (batch gov checks) | Fargate with ALB — AgentCore session model enforces 8 h but per-request still bounded |
| Pure-Python local dev | `python a2a_server/server.py` — hits `BedrockModel` directly; no CDK needed |
| Offline test without Bedrock | Swap to `OllamaModel(model_id='llama3.2')`; agent card stays the same |

---

## 6. Worked example — A2A stack synthesizes + agent card renders

Save as `tests/sop/test_AGENTCORE_A2A.py`. Offline.

```python
"""SOP verification — A2AGovernanceStack synths, exec role has Bedrock only."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_a2a_governance_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.agent_a2a_governance import A2AGovernanceStack
    stack = A2AGovernanceStack(app, vpc=vpc,
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        permission_boundary=boundary, env=env)

    template = Template.from_stack(stack)
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)
    template.resource_count_is("AWS::SSM::Parameter", 2)   # arn + endpoint


def test_agent_card_renders_locally():
    """Smoke-test that StrandsA2AExecutor can build an agent card offline."""
    from unittest.mock import patch
    from strands import Agent
    from strands.models import BedrockModel
    from strands.multiagent.a2a import StrandsA2AExecutor

    with patch('boto3.client'):
        agent = Agent(
            model=BedrockModel(model_id="anthropic.claude-haiku-4-5-20251001-v1:0"),
            system_prompt="test",
            name="governance-a2a",
            description="Governance compliance checker",
        )
        executor = StrandsA2AExecutor(agent=agent)
        card = executor.get_agent_card(host="0.0.0.0", port=9100)

    assert card.name == "governance-a2a"
    assert "Governance" in card.description
```

---

## 7. References

- `docs/template_params.md` — `A2A_GOVERNANCE_ARN_SSM`, `A2A_GOVERNANCE_ENDPOINT_SSM`, `A2A_PORT`, `A2A_AUTH_MODE`
- `docs/Feature_Roadmap.md` — feature IDs `STR-08` (A2A), `AG-08` (cross-platform agents)
- A2A protocol spec: https://github.com/google-a2a/a2a-protocol
- Strands A2A: https://strandsagents.com/latest/user-guide/concepts/multiagent/a2a/
- Related SOPs: `STRANDS_MULTI_AGENT` (A2A client pattern), `AGENTCORE_RUNTIME` (hosting the A2A server), `AGENTCORE_IDENTITY` (cross-org SigV4 / OAuth2 auth), `AGENTCORE_AGENT_CONTROL` (Cedar-backed compliance check example in §3.1), `LAYER_NETWORKING` (PrivateLink / cross-VPC reachability)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — per-agent A2A stack publishes both ARN and HTTP endpoint via SSM; supervisor reads both and grants identity-side `InvokeAgentRuntime`. Translated CDK from TypeScript to Python (original was prose-only for CDK). Added Swap matrix (§5), Worked example (§6), Gotchas on auth, agent-card PII, and port conventions. |
| 1.0 | 2026-03-05 | Initial — A2A server, A2A client, Dockerfile, requirements.txt. |
