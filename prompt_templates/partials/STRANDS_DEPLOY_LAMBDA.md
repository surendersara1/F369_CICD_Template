# SOP — Strands Deploy to Lambda (MCP Proxy, Direct Target, Standalone Agent)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Lambda Python 3.13 · ARM64 (Graviton) · Strands SDK Lambda Layer (v1.23.0+) · boto3 ≥ 1.34

---

## 1. Purpose

- Codify the three Lambda deployment patterns for Strands agents / MCP targets:
  1. **MCP runtime proxy Lambda** — Gateway target that bridges a Gateway tool call to an MCP server running on AgentCore Runtime.
  2. **Direct Lambda target** — the tool runs inside Lambda; Gateway calls it directly.
  3. **Standalone agent Lambda** — API Gateway → Lambda → `strands.Agent`, for short-lived agents that don't need AgentCore Runtime's 8-hour session window.
- Provide the canonical Strands Lambda Layer ARN template and Python-runtime/arch pairing.
- Wire IAM correctly: Lambda execution role for outbound calls (`bedrock-agentcore:InvokeAgentRuntime`, `bedrock:InvokeModel`) and a resource-based policy for inbound invocations from `bedrock-agentcore.amazonaws.com`.
- Include when the SOW mentions Lambda-hosted agents, MCP proxy Lambdas, standalone serverless agents, or container-image Lambdas for Strands.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Building a single CDK stack that owns the Gateway + the MCP runtime + these Lambdas together | **§3 Monolith Variant** |
| Building separate stacks (`GatewayStack`, `RuntimeStack`, `ComputeStack`, …) where the Lambda is in a different stack than the Gateway / Runtime / KMS / buckets it uses | **§4 Micro-Stack Variant** |

**Why the split matters.** The proxy Lambda needs `bedrock-agentcore:InvokeAgentRuntime` on a runtime whose ARN is owned by another stack. In a monolith, `runtime.grant_invoke(fn)` works; in micro-stack it forces a circular export (downstream→upstream for the runtime ARN, upstream→downstream for the role ARN). Granting identity-side on the function's execution role keeps dependencies unidirectional. Same pattern applies to direct Lambda targets that must allow `bedrock-agentcore.amazonaws.com` to invoke them.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` subclass owns the Lambda proxy, the MCP runtime, the Gateway, and any buckets/keys they touch. Typical POC / prototype.

### 3.1 Strands Lambda Layer ARN template

```
arn:aws:lambda:{region}:856699698935:layer:strands-agents-py{version}-{arch}:{layer_version}

Python runtimes : 3.10  3.11  3.12  3.13
Architectures   : x86_64  aarch64
Example v1      : strands-agents-py313-aarch64:1   (SDK v1.23.0)
```

Use this layer on the **standalone agent Lambda** (§3.4) so cold-start isn't dominated by installing `strands-agents` from pip. Proxy Lambdas do NOT need the layer — they only call boto3.

### 3.2 CDK — MCP runtime proxy Lambda (Pattern 1)

```python
from aws_cdk import (
    Duration, Aws,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_logs as logs,
)


def _create_mcp_proxy(self, runtime_arn: str) -> _lambda.Function:
    """Proxy Lambda: Gateway target that bridges to an AgentCore MCP runtime.

    Assumes the runtime is owned by THIS stack. For micro-stack, see §4.2.
    """
    log_group = logs.LogGroup(
        self, "McpProxyLogs",
        log_group_name="/aws/lambda/{project_name}-mcp-proxy",
        retention=logs.RetentionDays.ONE_MONTH,
    )

    proxy = _lambda.Function(
        self, "McpProxyFn",
        function_name="{project_name}-mcp-proxy",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="handler.handler",
        code=_lambda.Code.from_asset("lambda/mcp_runtime_proxy"),
        timeout=Duration.seconds(120),
        memory_size=512,
        environment={"RUNTIME_ARN": runtime_arn},
        log_group=log_group,
        tracing=_lambda.Tracing.ACTIVE,
    )

    # L2 grant — SAFE in monolith (same stack owns the runtime)
    proxy.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:InvokeAgentRuntime"],
        resources=[runtime_arn],
    ))
    return proxy
```

### 3.3 CDK — direct Lambda target (Pattern 2)

```python
from aws_cdk import aws_ec2 as ec2


def _create_direct_tool(self, vpc: ec2.IVpc) -> _lambda.Function:
    """Tool logic inside Lambda, exposed via Gateway directly (no MCP runtime)."""
    tool_fn = _lambda.Function(
        self, "ToolFn",
        function_name="{project_name}-tool-vendor-spend",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/tool_vendor_spend"),
        timeout=Duration.seconds(60),
        memory_size=512,
        vpc=vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        tracing=_lambda.Tracing.ACTIVE,
    )

    # Resource-based policy — allow AgentCore Gateway to invoke this Lambda
    tool_fn.add_permission(
        "AllowAgentCoreInvoke",
        principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        source_account=Aws.ACCOUNT_ID,
        action="lambda:InvokeFunction",
    )
    return tool_fn
```

### 3.4 CDK — standalone agent Lambda (Pattern 3)

```python
def _create_standalone_agent(self) -> _lambda.Function:
    """API GW → Lambda → strands.Agent. Short-lived agents only (< 15 min)."""
    strands_layer_arn = (
        f"arn:aws:lambda:{Aws.REGION}:856699698935:"
        f"layer:strands-agents-py313-aarch64:1"
    )
    layer = _lambda.LayerVersion.from_layer_version_arn(
        self, "StrandsLayer", strands_layer_arn,
    )

    agent_fn = _lambda.Function(
        self, "AgentFn",
        function_name="{project_name}-agent",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/agent_standalone"),
        layers=[layer],
        timeout=Duration.seconds(300),   # 5 min max per turn
        memory_size=1024,
        environment={
            "MODEL_ID":          "{MODEL_ID}",
            "FALLBACK_MODEL_ID": "{FALLBACK_MODEL_ID}",
        },
        tracing=_lambda.Tracing.ACTIVE,
    )

    # Identity-side Bedrock grant
    agent_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
    ))
    return agent_fn
```

### 3.5 Proxy handler (Python, shared with §4)

```python
"""MCP Runtime Proxy — Gateway → Lambda → AgentCore Runtime (MCP server)."""
import json, os, uuid
import boto3

RUNTIME_ARN      = os.environ['RUNTIME_ARN']
agentcore_client = boto3.client('bedrock-agentcore')


def handler(event, context):
    # Extract tool name from the Gateway context metadata
    tool_name = 'unknown'
    try:
        if hasattr(context, 'client_context') and context.client_context:
            raw = getattr(context.client_context, 'custom', {}).get(
                'bedrockAgentCoreToolName', ''
            )
            tool_name = raw.split('___')[-1] if '___' in raw else raw
    except Exception:
        pass

    # JSON-RPC tools/call envelope for MCP
    mcp_request = {
        "jsonrpc": "2.0", "id": 1,
        "method":  "tools/call",
        "params":  {"name": tool_name, "arguments": event},
    }

    response = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        contentType='application/json',
        payload=json.dumps(mcp_request).encode('utf-8'),
        qualifier='DEFAULT',
        runtimeSessionId=str(uuid.uuid4()),
    )
    raw    = response.get('response').read().decode('utf-8')
    result = json.loads(raw)

    # Unwrap MCP content block → flat result (Gateway expects a single object)
    if 'result' in result and 'content' in result.get('result', {}):
        texts = [c.get('text', '') for c in result['result']['content']
                 if c.get('type') == 'text']
        if texts:
            try:
                return json.loads(texts[0])
            except Exception:
                return {'text': texts[0]}
    return result
```

### 3.6 Standalone agent handler (Python, §3.4)

```python
"""Standalone agent Lambda — short-lived Strands Agent per request."""
import json, os
from strands import Agent
from strands.models import BedrockModel


def handler(event, context):
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    agent = Agent(
        model=BedrockModel(model_id=os.environ["MODEL_ID"]),
        system_prompt="You are helpful.",
        tools=[],  # [Claude: add tools from SOW]
    )
    response = agent(body.get("message", ""))
    return {
        "statusCode": 200,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps({"response": str(response)}),
    }
```

### 3.7 Monolith gotchas

- **Standalone agent Lambda with full synthesis** easily hits the 15-minute timeout when parallel sub-agents stall. Use AgentCore Runtime (`AGENTCORE_RUNTIME`) for synthesizers; keep Lambda for tools and short agents.
- **`RUNTIME_ARN` in env** is plain text — acceptable for identity-only data, but don't put credentials here. Use Secrets Manager + inject the secret ARN.
- **`add_permission` is a resource-based policy** — it lives on the Lambda itself, NOT on the invoker. Safe for cross-stack in both directions (no circular dep).
- **`from_asset("lambda/tool_x")`** resolves relative to CWD. Always run `cdk synth` from the project root or move to `Path(__file__)` anchoring as in §4.

---

## 4. Micro-Stack Variant

**Use when:** the Lambda lives in `ComputeStack`, but the Gateway is in `GatewayStack`, the MCP runtime is in `RuntimeStack`, and buckets/keys are in `StorageStack`/`SecurityStack`.

### 4.1 The five non-negotiables (from `LAYER_BACKEND_LAMBDA §4.1`)

1. **Anchor asset paths to `__file__`**, never CWD-relative.
2. **Never use `runtime.grant_invoke(fn)`** across stacks — attach identity-side `PolicyStatement` on the role.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`** — use L1 `CfnRule` + static-ARN resource policy on the queue.
4. **Never own a bucket in one stack and attach its CloudFront OAC in another.**
5. **Never set `encryption_key=ext_key` where `ext_key` came from another stack** — use identity-side KMS grants.

### 4.2 CDK — `ComputeStack` hosting the proxy + direct target + standalone agent

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_ec2 as ec2,
    aws_iam as iam,
    Aws,
)
from constructs import Construct

# stacks/compute_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


def _grant_runtime_invoke(fn: _lambda.IFunction, runtime_arn: str) -> None:
    """Identity-side grant — safe when the runtime is in another stack."""
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:InvokeAgentRuntime"],
        resources=[runtime_arn],
    ))


class ComputeStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        mcp_runtime_arn: str,       # plain string — no import needed
        standalone_model_id: str,
        standalone_fallback_model_id: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-compute", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # ── Pattern 1: MCP proxy ──────────────────────────────────────────
        proxy_log = logs.LogGroup(
            self, "McpProxyLogs",
            log_group_name="/aws/lambda/{project_name}-mcp-proxy",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.mcp_proxy_fn = _lambda.Function(
            self, "McpProxyFn",
            function_name="{project_name}-mcp-proxy",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "mcp_runtime_proxy")),
            timeout=cdk.Duration.seconds(120),
            memory_size=512,
            environment={"RUNTIME_ARN": mcp_runtime_arn},
            log_group=proxy_log,
            tracing=_lambda.Tracing.ACTIVE,
        )
        _grant_runtime_invoke(self.mcp_proxy_fn, mcp_runtime_arn)

        # ── Pattern 2: direct Lambda target (tool logic) ─────────────────
        tool_log = logs.LogGroup(
            self, "VendorSpendLogs",
            log_group_name="/aws/lambda/{project_name}-tool-vendor-spend",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.direct_tool_fn = _lambda.Function(
            self, "VendorSpendFn",
            function_name="{project_name}-tool-vendor-spend",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "tool_vendor_spend")),
            timeout=cdk.Duration.seconds(60),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            log_group=tool_log,
            tracing=_lambda.Tracing.ACTIVE,
        )
        # Resource policy on the Lambda itself — cross-stack safe (no circular dep)
        self.direct_tool_fn.add_permission(
            "AllowAgentCoreInvoke",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            source_account=Aws.ACCOUNT_ID,
            action="lambda:InvokeFunction",
        )

        # ── Pattern 3: standalone agent Lambda ───────────────────────────
        strands_layer_arn = (
            f"arn:aws:lambda:{Aws.REGION}:856699698935:"
            f"layer:strands-agents-py313-aarch64:1"
        )
        strands_layer = _lambda.LayerVersion.from_layer_version_arn(
            self, "StrandsLayer", strands_layer_arn,
        )
        agent_log = logs.LogGroup(
            self, "AgentFnLogs",
            log_group_name="/aws/lambda/{project_name}-agent",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.standalone_agent_fn = _lambda.Function(
            self, "AgentFn",
            function_name="{project_name}-agent",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "agent_standalone")),
            layers=[strands_layer],
            timeout=cdk.Duration.seconds(300),
            memory_size=1024,
            environment={
                "MODEL_ID":          standalone_model_id,
                "FALLBACK_MODEL_ID": standalone_fallback_model_id,
            },
            log_group=agent_log,
            tracing=_lambda.Tracing.ACTIVE,
        )
        # Identity-side Bedrock grant
        self.standalone_agent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
        ))

        # Shared permission boundary
        for fn in [self.mcp_proxy_fn, self.direct_tool_fn, self.standalone_agent_fn]:
            iam.PermissionsBoundary.of(fn.role).apply(permission_boundary)
```

### 4.3 Micro-stack gotchas

- **`mcp_runtime_arn` is passed as a plain string**, not a construct reference. Source it from an SSM parameter or a CloudFormation export that the runtime stack publishes — that way both stacks can be synthed independently.
- **Do NOT call `runtime.grant_invoke(fn)`** on a construct imported from another stack. It will silently add a `Principal` to the runtime's resource policy and force a circular export.
- **`add_permission`** on the direct-target Lambda is inherently resource-local — the principal is a service principal, not another stack's role, so there is no cross-stack cycle.
- **Strands Layer ARN** is region-scoped. In a multi-region deploy, compute per stack's region via `Aws.REGION` (as above).
- **Permissions boundary** must have at least one statement; an empty boundary fails synth.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| One stack owns everything, < 400 resources | Stay on §3 Monolith |
| `cdk synth` fails with "Adding this dependency … would create a cyclic reference" | Likely a `grant_invoke` on a cross-stack runtime — switch the call site to §4.2 identity-side grant |
| You add a second region or deploy pipeline | Micro-stack (each region a fresh compute stack, shared runtime ARN via SSM) |
| Long-running agent (> 15 min) | Move from Pattern 3 (standalone) to AgentCore Runtime — see `AGENTCORE_RUNTIME` |
| Tool needs > 3 GB memory or heavy native deps | Move the tool to ECS Fargate — see `STRANDS_DEPLOY_ECS` |
| Lambda cold-start > 5 s | Provision `reserved_concurrent_executions` or prefer the Strands Lambda Layer over pip install |

---

## 6. Worked example — both variants synthesize

Save as `tests/sop/test_STRANDS_DEPLOY_LAMBDA.py`. Offline; no AWS calls.

```python
"""SOP verification — ComputeStack synths with no cross-stack cycles."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_microstack_compute_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg   = ec2.SecurityGroup(deps, "Sg", vpc=vpc)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.compute_stack import ComputeStack
    compute = ComputeStack(
        app,
        vpc=vpc, lambda_sg=sg,
        mcp_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/my-runtime",
        standalone_model_id="anthropic.claude-sonnet-4-20250514-v1:0",
        standalone_fallback_model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
        permission_boundary=boundary,
        env=env,
    )

    template = Template.from_stack(compute)
    template.resource_count_is("AWS::Lambda::Function", 3)       # proxy, direct, standalone
    template.resource_count_is("AWS::Lambda::Permission", 1)     # bedrock-agentcore invoke
```

---

## 7. References

- `docs/template_params.md` — `MODEL_ID`, `FALLBACK_MODEL_ID`, `MCP_RUNTIME_ARN`, `STRANDS_LAYER_VERSION`
- `docs/Feature_Roadmap.md` — feature IDs `STR-02` (Lambda deploy), `AG-03` (Gateway targets), `C-12` (ARM64 Lambda), `A-17` (Bedrock grants)
- Strands Lambda Layer: https://strandsagents.com/latest/user-guide/deploy/lambda-layer/
- AgentCore Gateway Lambda targets: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-gateway-targets.html
- Related SOPs: `LAYER_BACKEND_LAMBDA` (the five non-negotiables + helper functions), `STRANDS_DEPLOY_ECS` (when Lambda is too constrained), `AGENTCORE_RUNTIME` (for > 15 min agents), `AGENTCORE_GATEWAY` (target config), `STRANDS_MCP_SERVER` (what the proxy is fronting)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) applying the five non-negotiables: identity-side `bedrock-agentcore:InvokeAgentRuntime` grant, `Path(__file__)`-anchored assets, explicit `LogGroup`, resource policy for `bedrock-agentcore.amazonaws.com` invoker, plain-string `mcp_runtime_arn`. Translated CDK from TypeScript to Python. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — three Lambda patterns (MCP proxy, direct target, standalone agent), layer ARN template, TS CDK examples, proxy handler. |
