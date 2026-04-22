# SOP — Strands Deploy to AgentCore Runtime (and Fargate fallback)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws-cdk-lib.aws-bedrock-agentcore-alpha` · ECS Fargate arm64 · Python 3.13 · Strands SDK v1.34+ · AgentCore SDK v1.6+

---

## 1. Purpose

- Codify the production deployment pattern for Strands agents: **Docker container → AgentCore Runtime**. Each agent is its own container; AgentCore Runtime handles scaling, microVM isolation, and the 8-hour session window.
- Codify the MCP-enabled Dockerfile (same base; extra deps for MCP server role).
- Define when **ECS Fargate** is the right fallback (non-agent workloads: PDF generation, ETL, long-running batch with custom networking).
- Provide the standardized `agent.py` entrypoint pattern (`BedrockAgentCoreApp`) and the per-agent `requirements.txt`.
- Wire IAM identity-side so the agent's execution role can call `bedrock-agentcore:InvokeGateway`, `bedrock:InvokeModel`, and any sub-agent runtime ARNs — without cross-stack cycles.
- Include when the SOW mentions AgentCore Runtime deployment, containerized Strands agents, long-running agents, or per-agent Docker containers.

> **Note on name.** The file is named `STRANDS_DEPLOY_ECS` for historical reasons. Production targets **AgentCore Runtime**, not ECS Fargate, for agent workloads. Fargate is only the fallback for non-agent containers. Both patterns are in-scope here.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Building a single CDK stack that owns the VPC + all `AgentRuntime` resources + Fargate services + buckets/keys together | **§3 Monolith Variant** |
| Runtime resources live in `RuntimeStack`, but KMS keys live in `SecurityStack`, buckets in `StorageStack`, VPC in `NetworkStack` | **§4 Micro-Stack Variant** |

**Why the split matters.** A Strands agent container typically reads KMS-encrypted secrets, writes session state to an S3 bucket, and calls sub-agents (each its own AgentCore Runtime). In a monolith, `bucket.grant_write(runtime_role)` works. In micro-stack, that call edits the bucket's resource policy in another stack and creates a circular export. Identity-side `PolicyStatement` on the runtime's execution role avoids it.

---

## 3. Monolith Variant

**Use when:** one `cdk.Stack` owns everything — VPC + runtimes + Fargate + data resources.

### 3.1 Agent Dockerfile (MCP-enabled)

```dockerfile
# agents/observer/Dockerfile
# Build context is agents/ (parent directory) — set in AgentRuntimeArtifact.from_asset()
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /var/task

# Install dependencies first (cached layer)
COPY observer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared utilities — used by every agent container
COPY shared/ ./shared/
COPY evaluations/ ./evaluations/

# Agent-specific code
COPY observer/agent.py .

EXPOSE 8080
ENTRYPOINT ["python", "agent.py"]
```

### 3.2 Per-agent `requirements.txt`

```text
strands-agents==1.34.0
strands-agents-tools>=0.1.0
bedrock-agentcore==1.6.0
boto3>=1.34.0
mcp[cli]>=1.8.0
httpx>=0.27.0
```

### 3.3 Agent entrypoint (`BedrockAgentCoreApp`)

```python
"""Agent entry point for AgentCore Runtime deployment."""
import os
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = "You are the {project_name} Observer agent."


@app.entrypoint
def invoke(payload):
    query = payload.get('prompt', '')
    model = BedrockModel(model_id=os.environ['DEFAULT_MODEL_ID'])
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[])
    result = agent(query)
    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
```

### 3.4 CDK — AgentCore Runtime (primary) + optional Fargate (fallback)

```python
from aws_cdk import (
    Duration, Aws,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_logs as logs,
    aws_ssm as ssm,
)
from aws_cdk.aws_bedrock_agentcore_alpha import (
    AgentRuntimeArtifact, Runtime, ProtocolType, RuntimeNetworkConfiguration,
)


def _create_observer_runtime(self, vpc: ec2.IVpc) -> Runtime:
    """AgentCore Runtime — observer agent container.

    Assumes self.{agent_role, model_id, gateway_url} were set earlier.
    """
    artifact = AgentRuntimeArtifact.from_asset(
        "agents",                          # build context
        file="observer/Dockerfile",
        platform=ecr_assets.Platform.LINUX_ARM64,
    )

    runtime = Runtime(
        self, "ObserverRuntime",
        runtime_name="{project_name}_observer",
        agent_runtime_artifact=artifact,
        execution_role=self.agent_role,
        protocol_configuration=ProtocolType.HTTP,    # agent runtime (MCP servers use ProtocolType.MCP)
        network_configuration=RuntimeNetworkConfiguration.using_vpc(
            self, vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        ),
        environment_variables={
            "DEFAULT_MODEL_ID": self.model_id,
            "GATEWAY_URL":      self.gateway_url,
        },
    )

    # Publish the runtime ARN so supervisors can read it from SSM
    ssm.StringParameter(self, "ObserverArnParam",
        parameter_name="/{project_name}/agents/observer_agent_arn",
        string_value=runtime.runtime_arn,
    )

    # L2 grants — SAFE in monolith (same stack owns agent_role)
    self.agent_role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
    ))
    self.agent_role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:InvokeGateway"],
        resources=["*"],   # Gateway ARN is one per account; scope tighter if known
    ))
    return runtime


def _create_pdf_fargate(self, vpc: ec2.IVpc) -> ecs.FargateService:
    """Fargate is the fallback for NON-agent containers (PDF gen, ETL)."""
    cluster = ecs.Cluster(self, "BatchCluster", vpc=vpc, container_insights=True)

    task_def = ecs.FargateTaskDefinition(self, "PdfGenTask",
        family="{project_name}-pdf-gen",
        cpu=1024, memory_limit_mib=2048,
        runtime_platform=ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        ),
    )

    log_group = logs.LogGroup(self, "PdfGenLogs",
        log_group_name="/ecs/{project_name}-pdf-gen",
        retention=logs.RetentionDays.ONE_MONTH,
    )

    task_def.add_container(
        "PdfGen",
        image=ecs.ContainerImage.from_asset("containers/pdf_gen"),
        logging=ecs.LogDrivers.aws_logs(stream_prefix="pdf", log_group=log_group),
        readonly_root_filesystem=True,
    )

    return ecs.FargateService(
        self, "PdfGenService",
        cluster=cluster,
        task_definition=task_def,
        desired_count=1,
        capacity_provider_strategies=[
            ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=1),
        ],
    )
```

### 3.5 When to use AgentCore Runtime vs ECS Fargate

| Criterion | AgentCore Runtime | ECS Fargate |
|---|---|---|
| Agent workloads (Strands supervisor, observer, reasoner) | ✅ purpose-built | Overkill |
| Per-session isolation (microVM) | ✅ | Shared container |
| Auto-scaling on session volume | ✅ managed | Manual ASG / target-tracking |
| MCP server hosting (`ProtocolType.MCP`) | ✅ | Not supported |
| Non-agent workloads (PDF gen, ETL, video transcode) | ❌ | ✅ |
| Custom networking beyond the managed VPC integration | Limited | Full control |
| Max request duration | Session-bound, streaming | Task lifetime (effectively unlimited) |

### 3.6 Monolith gotchas

- **`AgentRuntimeArtifact.from_asset("agents", file="observer/Dockerfile")`** uses CWD-relative paths. Run `cdk synth` from the project root or switch to `Path(__file__)` anchoring (§4.2).
- **`ProtocolType.HTTP`** is for agent runtimes; MCP *servers* use `ProtocolType.MCP` — see `STRANDS_MCP_SERVER`. Mismatched protocol = silent 404 at invoke time.
- **`InvokeGateway` resource `"*"`** is a concession because the Gateway ARN usually isn't known at synth time in a monolith. Tighten to the known ARN as soon as it stabilizes.
- **Runtime ARN in SSM** is the production hand-off to other stacks (supervisors read it at runtime). In monolith you can skip SSM and pass the construct reference directly — but keep the SSM param so the supervisor code doesn't change across variants.
- **Fargate + NAT** — a single AZ's NAT is a SPOF for the task's outbound. Use 2+ AZs with NAT in each.

---

## 4. Micro-Stack Variant

**Use when:** the AgentCore runtime lives in `RuntimeStack`, but the VPC is in `NetworkStack`, execution role / KMS in `SecurityStack`, buckets in `StorageStack`.

### 4.1 The five non-negotiables

1. **Anchor Docker build contexts to `Path(__file__)`** — not CWD-relative strings.
2. **Never call `bucket.grant_*(runtime_role)`** where bucket is in another stack. Use identity-side `PolicyStatement` on the role.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`** for event-driven agent invocation; use L1 `CfnRule` with a static-ARN target.
4. **Never split a bucket and its CloudFront OAC** — not relevant here, but the rule is part of the exemplar set.
5. **Never set `encryption_key=ext_key`** on resources inside `RuntimeStack` where `ext_key` came from another stack — apply KMS identity-side.

### 4.2 CDK — `RuntimeStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration, Aws,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_kms as kms,
    aws_ssm as ssm,
)
from aws_cdk.aws_bedrock_agentcore_alpha import (
    AgentRuntimeArtifact, Runtime, ProtocolType, RuntimeNetworkConfiguration,
)
from constructs import Construct

# stacks/runtime_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_AGENTS_ROOT: Path = Path(__file__).resolve().parents[3] / "agents"


# Identity-side grant helpers
def _bedrock_model_grant(role: iam.IRole) -> None:
    role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
    ))


def _gateway_invoke_grant(role: iam.IRole, gateway_arn: str) -> None:
    role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:InvokeGateway"],
        resources=[gateway_arn],
    ))


def _kms_grant(role: iam.IRole, key: kms.IKey, actions: list[str]) -> None:
    role.add_to_policy(iam.PolicyStatement(actions=actions, resources=[key.key_arn]))


class RuntimeStack(cdk.Stack):
    """AgentCore runtimes — container-per-agent, identity-side grants only.

    Every upstream resource is an interface. We NEVER call resource.grant_*(role)
    on cross-stack resources — see §4.1 non-negotiables.
    """

    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        agent_role: iam.IRole,
        session_kms_key: kms.IKey,
        gateway_arn: str,
        gateway_url: str,
        model_id: str,
        fallback_model_id: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-runtime", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # ── Observer agent ────────────────────────────────────────────
        observer_artifact = AgentRuntimeArtifact.from_asset(
            str(_AGENTS_ROOT),                          # Path(__file__)-anchored
            file="observer/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        self.observer = Runtime(
            self, "ObserverRuntime",
            runtime_name="{project_name}_observer",
            agent_runtime_artifact=observer_artifact,
            execution_role=agent_role,
            protocol_configuration=ProtocolType.HTTP,
            network_configuration=RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            ),
            environment_variables={
                "DEFAULT_MODEL_ID":  model_id,
                "FALLBACK_MODEL_ID": fallback_model_id,
                "GATEWAY_URL":       gateway_url,
            },
        )
        ssm.StringParameter(self, "ObserverArnParam",
            parameter_name="/{project_name}/agents/observer_agent_arn",
            string_value=self.observer.runtime_arn,
        )

        # ── Reasoner agent ────────────────────────────────────────────
        reasoner_artifact = AgentRuntimeArtifact.from_asset(
            str(_AGENTS_ROOT),
            file="reasoner/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        self.reasoner = Runtime(
            self, "ReasonerRuntime",
            runtime_name="{project_name}_reasoner",
            agent_runtime_artifact=reasoner_artifact,
            execution_role=agent_role,
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
        ssm.StringParameter(self, "ReasonerArnParam",
            parameter_name="/{project_name}/agents/reasoner_agent_arn",
            string_value=self.reasoner.runtime_arn,
        )

        # ── Supervisor agent (invokes observer + reasoner) ───────────
        supervisor_artifact = AgentRuntimeArtifact.from_asset(
            str(_AGENTS_ROOT),
            file="supervisor/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        self.supervisor = Runtime(
            self, "SupervisorRuntime",
            runtime_name="{project_name}_supervisor",
            agent_runtime_artifact=supervisor_artifact,
            execution_role=agent_role,
            protocol_configuration=ProtocolType.HTTP,
            network_configuration=RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            ),
            environment_variables={
                "DEFAULT_MODEL_ID":  model_id,
                "FALLBACK_MODEL_ID": fallback_model_id,
                "GATEWAY_URL":       gateway_url,
            },
        )
        ssm.StringParameter(self, "SupervisorArnParam",
            parameter_name="/{project_name}/agents/supervisor_agent_arn",
            string_value=self.supervisor.runtime_arn,
        )

        # ── Identity-side grants on the shared agent_role ────────────
        _bedrock_model_grant(agent_role)
        _gateway_invoke_grant(agent_role, gateway_arn)
        _kms_grant(agent_role, session_kms_key, ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"])

        # Supervisor also needs InvokeAgentRuntime on observer + reasoner
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[self.observer.runtime_arn, self.reasoner.runtime_arn],
        ))

        # Apply permission boundary
        iam.PermissionsBoundary.of(agent_role).apply(permission_boundary)
```

### 4.3 Fargate for non-agent workloads (separate `BatchStack`)

Same discipline — grants on the `task_role` go identity-side; see `LAYER_BACKEND_ECS §4` for the micro-stack Fargate pattern.

### 4.4 Micro-stack gotchas

- **`AgentRuntimeArtifact.from_asset(str(_AGENTS_ROOT), ...)`** — pass `str()` of the Path; the CDK API wants a string. Skipping `str()` compiles but fails at build time on Windows paths.
- **`agent_role` is shared across all three runtimes in the example.** That is intentional — one role is easier to audit and avoids N cross-stack imports. If a sub-agent needs less privilege, create a scoped sub-role in `SecurityStack` and pass both roles in.
- **`runtime_arn` from a Runtime object is a CloudFormation token**, not a string at synth time. Passing it *within* the same stack (as above, for `InvokeAgentRuntime`) is fine. Passing it to *another* stack requires a plain string (via SSM or CfnExport) or it forces a cross-stack export.
- **`ssm.StringParameter.string_value` cannot be a token from another stack.** Put the SSM writer in the same stack as the runtime, and have consumers read via `ssm.StringParameter.value_from_lookup` (synth-time) or `ssm.StringParameter.from_string_parameter_name` (deploy-time).
- **Permission boundary on a shared role** — applied once on `agent_role` is enough; applying again inside each sub-stack is a no-op but harmless.

---

## 5. Swap matrix — when to switch

| Trigger | Action |
|---|---|
| POC / single region / single pipeline | Stay on §3 Monolith |
| Separate deploy cadence for agents vs. shared services | Micro-stack (§4) |
| Runtime-ARN cross-stack cycle on supervisor | Publish ARN via SSM in the runtime stack; consumer reads by SSM name |
| Custom networking (mesh, service discovery) | Move that specific workload to Fargate (§3.4 / `LAYER_BACKEND_ECS`) |
| Agent workload > 8 h session window | Split into stateful-sync Lambda + AgentCore Runtime, or move to Fargate with custom session store |
| High egress cost on Gateway VPC endpoint | Co-locate runtime + Gateway + MCP servers in the same VPC |

---

## 6. Worked example — both variants synthesize cleanly

Save as `tests/sop/test_STRANDS_DEPLOY_ECS.py`. Offline.

```python
"""SOP verification — RuntimeStack synths without cross-stack cycles."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam, aws_kms as kms
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_microstack_runtime_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    session_key = kms.Key(deps, "SessionKey")
    agent_role  = iam.Role(deps, "AgentRole",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"))
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.runtime_stack import RuntimeStack
    runtime_stack = RuntimeStack(
        app,
        vpc=vpc,
        agent_role=agent_role,
        session_kms_key=session_key,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/test-gw",
        gateway_url="https://test.gateway.example",
        model_id="anthropic.claude-sonnet-4-20250514-v1:0",
        fallback_model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
        permission_boundary=boundary,
        env=env,
    )

    template = Template.from_stack(runtime_stack)
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 3)   # observer, reasoner, supervisor
    template.resource_count_is("AWS::SSM::Parameter", 3)              # three ARN params
```

---

## 7. References

- `docs/template_params.md` — `MODEL_ID`, `FALLBACK_MODEL_ID`, `GATEWAY_URL`, `GATEWAY_ARN`, `AGENT_RUNTIME_ARCH` (`ARM_64`)
- `docs/Feature_Roadmap.md` — feature IDs `STR-01` (AgentCore deploy), `AG-02` (runtime containers), `C-19..C-24` (Fargate)
- Bedrock AgentCore Runtime construct: https://docs.aws.amazon.com/cdk/api/v2/docs/aws-bedrock-agentcore-alpha-readme.html
- Related SOPs: `LAYER_BACKEND_LAMBDA` (the five non-negotiables + grant helpers), `LAYER_BACKEND_ECS` (Fargate deep dive for non-agent workloads), `AGENTCORE_RUNTIME` (the Runtime construct details + session model), `AGENTCORE_GATEWAY` (`InvokeGateway` target), `STRANDS_DEPLOY_LAMBDA` (when Lambda fits better than AgentCore), `STRANDS_MCP_SERVER` (`ProtocolType.MCP` servers)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) applying the five non-negotiables: `Path(__file__)`-anchored build contexts, identity-side grants for Bedrock / Gateway / KMS, SSM-published runtime ARNs to break cross-stack cycles. Translated CDK from TypeScript to Python. Clarified naming — file is DEPLOY_ECS for historical reasons; production target is AgentCore Runtime, Fargate is fallback. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — AgentCore Runtime pattern, agent Dockerfile, MCP-enabled variant, MCP server Dockerfile, TS CDK examples, requirements.txt, AgentCore vs Fargate decision table. |
