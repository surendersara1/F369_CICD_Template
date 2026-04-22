# SOP — Bedrock AgentCore Gateway (MCP Endpoint, Lambda Targets, Runtime Proxy)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · L1 `aws_bedrockagentcore.CfnGateway` / `CfnGatewayTarget` · Lambda Python 3.13 · ARM64 (Graviton) · `mcp` Python SDK ≥ 1.8 · `httpx` ≥ 0.27

---

## 1. Purpose

- Provision the unified MCP endpoint (`CfnGateway`) with IAM (or OAuth2) auth and the tool-target configurations (`CfnGatewayTarget`) that route tool calls to either:
  1. **Direct Lambda** — the tool runs in Lambda (Monte Carlo sim, variance decomposition, forecast, etc.).
  2. **Lambda proxy → AgentCore Runtime MCP server** — Lambda is a JSON-RPC bridge to a long-lived MCP server runtime.
- Codify the three IAM surfaces:
  - Gateway role (`lambda:InvokeFunction` on targets + PolicyEngine permissions for Cedar RBAC).
  - Lambda-proxy execution role (`bedrock-agentcore:InvokeAgentRuntime` on MCP runtime ARNs).
  - Direct-target Lambda resource policy that allows `bedrock-agentcore.amazonaws.com` to invoke.
- Codify inline tool-schema vs. JSON-file tool-schema loading for `CfnGatewayTarget.targetConfiguration.mcp.lambda.toolSchema`.
- Codify SigV4 client-side auth for agents hitting the gateway (reused from `STRANDS_MCP_TOOLS §3.2`).
- Include when the SOW mentions AgentCore Gateway, MCP tools, tool gateway, Lambda tool targets, or MCP runtime proxy pattern.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns Gateway + Lambda targets + MCP runtime ARNs + SSM params | **§3 Monolith Variant** |
| Gateway in `MS05-Gateway` stack reads MCP runtime ARNs from SSM (runtimes live in `MS04-AgentcoreRuntime`), Lambda targets are in the same stack as Gateway | **§4 Micro-Stack Variant** |

**Why the split matters.** The gateway role needs `lambda:InvokeFunction` on every Lambda target. In a monolith that's a local ARN; across stacks, CDK's `lambda_fn.grant_invoke(gateway_role)` modifies the Lambda's resource policy — safe. The Lambda-proxy role needs `bedrock-agentcore:InvokeAgentRuntime` on a runtime owned by MS04. Calling `runtime.grant_invoke(proxy_role)` across stacks edits MS04's runtime policy referencing MS05's role ARN — CloudFormation rejects as cycle. The Micro-Stack variant reads runtime ARNs from SSM and grants identity-side on the proxy role.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` owns the Gateway, all Lambda targets, the MCP-runtime ARNs, and the SSM params.

### 3.1 Gateway + role

```python
from aws_cdk import (
    Aws, CfnOutput,
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    aws_ssm as ssm,
)


def _create_gateway(self) -> agentcore.CfnGateway:
    """Gateway + role. Gateway reads policy engine for Cedar RBAC (see AGENTCORE_AGENT_CONTROL)."""
    gateway_role = iam.Role(
        self, "GatewayRole",
        role_name="{project_name}-gateway-role",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    )
    # Invoke any Lambda with the project prefix (Gateway targets)
    gateway_role.add_to_policy(iam.PolicyStatement(
        actions=["lambda:InvokeFunction"],
        resources=[f"arn:aws:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:function:{{project_name}}-*"],
    ))
    # Cedar policy-engine access (only if using AGENTCORE_AGENT_CONTROL)
    gateway_role.add_to_policy(iam.PolicyStatement(
        sid="PolicyEngineAccess",
        actions=[
            "bedrock-agentcore:GetPolicyEngine",
            "bedrock-agentcore:AuthorizeAction",
            "bedrock-agentcore:PartiallyAuthorizeActions",
        ],
        resources=[
            f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:policy-engine/*",
            f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:gateway/*",
        ],
    ))

    gateway = agentcore.CfnGateway(
        self, "McpGateway",
        name="{project_name}-gateway",
        authorizer_type="AWS_IAM",
        protocol_type="MCP",
        role_arn=gateway_role.role_arn,
        description="Unified MCP tool endpoint for all agents",
    )
    self.gateway_role = gateway_role
    self.gateway      = gateway

    # Publish the gateway identifier for downstream consumers
    ssm.StringParameter(
        self, "GatewayEndpointParam",
        parameter_name="/{project_name}/mcp/gateway_endpoint",
        string_value=gateway.attr_gateway_url,
    )
    CfnOutput(self, "GatewayArn", value=gateway.attr_gateway_arn)
    return gateway
```

### 3.2 Pattern 1 — direct Lambda target

```python
from aws_cdk import aws_lambda as _lambda, aws_ec2 as ec2, Duration


def _create_direct_target(self, vpc: ec2.IVpc) -> None:
    """Direct Lambda target — tool logic in Lambda (e.g., Monte Carlo sim)."""
    monte_carlo_fn = _lambda.Function(
        self, "MonteCarloFn",
        function_name="{project_name}-monte-carlo",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/monte_carlo_sim"),
        timeout=Duration.seconds(300),
        memory_size=2048,
        vpc=vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
    )

    # L2 grant — SAFE in monolith (gateway_role is local)
    monte_carlo_fn.grant_invoke(self.gateway_role)
    # Resource policy — allow AgentCore service to invoke this Lambda
    monte_carlo_fn.add_permission(
        "AllowAgentCoreInvoke",
        principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        action="lambda:InvokeFunction",
        source_account=Aws.ACCOUNT_ID,
    )

    agentcore.CfnGatewayTarget(
        self, "MonteCarloTarget",
        name="monte-carlo-fn",
        gateway_identifier=self.gateway.attr_gateway_identifier,
        credential_provider_configurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"},
        ],
        target_configuration={
            "mcp": {
                "lambda": {
                    "lambdaArn": monte_carlo_fn.function_arn,
                    "toolSchema": {
                        "inlinePayload": [
                            {
                                "name":        "run_monte_carlo_simulation",
                                "description": "Run Monte Carlo simulation for P&L projections",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "iterations": {"type": "number",
                                                        "description": "Simulation iterations (default 500)"},
                                        "scenario":   {"type": "string",
                                                        "description": "Scenario name"},
                                    },
                                },
                            },
                        ],
                    },
                },
            },
        },
    )
```

### 3.3 Pattern 2 — Lambda proxy → AgentCore MCP runtime

```python
import json
from pathlib import Path


def _create_mcp_proxies(self, vpc: ec2.IVpc) -> None:
    """Lambda proxy targets — one per MCP runtime on AgentCore."""
    # Shared proxy role
    proxy_role = iam.Role(
        self, "McpProxyRole",
        role_name="{project_name}-mcp-proxy-role",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole",
            ),
        ],
    )
    # Add InvokeAgentRuntime on the specific MCP runtime ARNs
    proxy_role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:InvokeAgentRuntime"],
        # These ARNs are known at synth time because runtimes are in the same stack (monolith)
        resources=[
            self.mcp_runtimes["RedshiftMcp"].agent_runtime_arn,
            self.mcp_runtimes["NeptuneMcp"].agent_runtime_arn,
            # [Claude: add more per MCP runtime]
        ],
    ))

    proxies = [
        {"id": "Redshift",   "target": "redshift-mcp-proxy",   "runtime_key": "RedshiftMcp",   "schema": "schemas/redshift_tools.json"},
        {"id": "Neptune",    "target": "neptune-mcp-proxy",    "runtime_key": "NeptuneMcp",    "schema": "schemas/neptune_tools.json"},
        # [Claude: add more proxies per SOW data sources]
    ]

    for p in proxies:
        runtime_arn = self.mcp_runtimes[p["runtime_key"]].agent_runtime_arn
        tool_schema = json.loads(Path(p["schema"]).read_text(encoding="utf-8"))

        proxy_fn = _lambda.Function(
            self, f"{p['id']}ProxyFn",
            function_name=f"{{project_name}}-{p['target']}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambda/mcp_runtime_proxy"),
            role=proxy_role,
            timeout=Duration.seconds(120),
            memory_size=512,
            environment={"RUNTIME_ARN": runtime_arn},
        )

        proxy_fn.grant_invoke(self.gateway_role)
        proxy_fn.add_permission(
            f"AllowGatewayInvoke{p['id']}",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            source_account=Aws.ACCOUNT_ID,
            action="lambda:InvokeFunction",
        )

        agentcore.CfnGatewayTarget(
            self, f"{p['id']}Target",
            name=p["target"],
            gateway_identifier=self.gateway.attr_gateway_identifier,
            credential_provider_configurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"},
            ],
            target_configuration={
                "mcp": {
                    "lambda": {
                        "lambdaArn": proxy_fn.function_arn,
                        "toolSchema": {"inlinePayload": tool_schema},
                    },
                },
            },
        )
```

### 3.4 Proxy handler (shared with §4)

```python
"""MCP Runtime Proxy — Lambda bridge: Gateway → AgentCore Runtime (MCP server).

Flow: Gateway --[IAM]--> Lambda proxy --[IAM]--> AgentCore Runtime (MCP)
"""
import json, logging, os, uuid
import boto3

logger           = logging.getLogger()
RUNTIME_ARN      = os.environ['RUNTIME_ARN']
agentcore_client = boto3.client('bedrock-agentcore')

DELIMITER = '___'


def handler(event, context):
    # Extract tool name from Gateway context metadata
    tool_name = 'unknown'
    try:
        if hasattr(context, 'client_context') and context.client_context:
            custom = getattr(context.client_context, 'custom', {}) or {}
            raw    = custom.get('bedrockAgentCoreToolName', 'unknown')
            tool_name = raw[raw.index(DELIMITER) + len(DELIMITER):] if DELIMITER in raw else raw
    except Exception:
        pass

    # JSON-RPC tools/call envelope — MCP protocol
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

    # Unwrap MCP content block → flat object (Gateway expects single result)
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

### 3.5 Tool-schema JSON format

```json
[
  {
    "name": "get_pnl_history",
    "description": "Get P&L history by business unit and period",
    "inputSchema": {
      "type": "object",
      "properties": {
        "business_unit": {"type": "string", "description": "Business unit code"},
        "period":        {"type": "string", "description": "Period: MTD, QTD, YTD, T12M"}
      },
      "required": ["period"]
    }
  }
]
```

### 3.6 SigV4 client-side auth (reference — full implementation in `STRANDS_MCP_TOOLS §3.2`)

```python
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from shared.sigv4_auth import create_gateway_transport

gateway_client = MCPClient(lambda: create_gateway_transport(os.environ['GATEWAY_URL']))
```

### 3.7 Monolith gotchas

- **`CfnGateway.attr_gateway_url` vs `attr_gateway_arn`** — agents connect via the URL; IAM policies scope by ARN. Don't confuse the two in SSM params.
- **Inline schema vs file** — for > ~10 tools, keep schemas in JSON files in `schemas/`. Inline mushrooms the CloudFormation template and delays stack-update drift detection.
- **`credentialProviderType: "GATEWAY_IAM_ROLE"`** is the default for IAM-authed gateways. If you switch to OAuth2 for federated access, the value changes — don't leave both configured.
- **Resource-based policy on every Lambda target** — the `AllowAgentCoreInvoke` permission is required **in addition to** `grant_invoke(gateway_role)`. Missing it gives `AccessDenied` at invoke time with no synth warning.
- **`attr_gateway_identifier` ≠ `attr_gateway_arn`.** Targets reference the *identifier* (a short string), not the ARN.

---

## 4. Micro-Stack Variant

**Use when:** the production layout — MS05-Gateway contains the `CfnGateway`, gateway role, Lambda targets, and proxy Lambdas. MCP runtimes live in MS04. Runtime ARNs are read via SSM.

### 4.1 The five non-negotiables

1. **Anchor Lambda `code=from_asset(...)`** to `Path(__file__)`.
2. **Never call `runtime.grant_invoke(proxy_role)`** across stacks — runtime is in MS04, proxy role is in MS05. Read the runtime ARN from SSM and grant identity-side.
3. **Never target cross-stack queues** with `targets.SqsQueue`.
4. **Never split bucket + OAC** — not relevant, but the rule applies.
5. **Never set `encryption_key=ext_key`** on a queue/secret/log-group from another stack.

### 4.2 `GatewayStack` (MS05)

```python
from pathlib import Path
import json

import aws_cdk as cdk
from aws_cdk import (
    Aws, CfnOutput, Duration,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_ssm as ssm,
)
from constructs import Construct

# stacks/ms05_gateway.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"
_SCHEMAS_ROOT: Path = Path(__file__).resolve().parents[3] / "schemas"


class GatewayStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        mcp_runtime_ssm_names: dict[str, str],   # {"Redshift": "/proj/runtime/redshift_mcp_endpoint", ...}
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ms05-gateway", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # ── Gateway role + gateway ──────────────────────────────────
        gateway_role = iam.Role(
            self, "GatewayRole",
            role_name="{project_name}-gateway-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        gateway_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:function:{{project_name}}-*"],
        ))
        gateway_role.add_to_policy(iam.PolicyStatement(
            sid="PolicyEngineAccess",
            actions=[
                "bedrock-agentcore:GetPolicyEngine",
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
            ],
            resources=[
                f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:policy-engine/*",
                f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:gateway/*",
            ],
        ))
        iam.PermissionsBoundary.of(gateway_role).apply(permission_boundary)

        self.gateway = agentcore.CfnGateway(
            self, "McpGateway",
            name="{project_name}-gateway",
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            role_arn=gateway_role.role_arn,
            description="Unified MCP tool endpoint for all agents",
        )

        ssm.StringParameter(
            self, "GatewayEndpointParam",
            parameter_name="/{project_name}/mcp/gateway_endpoint",
            string_value=self.gateway.attr_gateway_url,
        )
        ssm.StringParameter(
            self, "GatewayArnParam",
            parameter_name="/{project_name}/mcp/gateway_arn",
            string_value=self.gateway.attr_gateway_arn,
        )
        CfnOutput(self, "GatewayUrl", value=self.gateway.attr_gateway_url)

        # ── Shared proxy role (identity-side grant for InvokeAgentRuntime) ──
        proxy_role = iam.Role(
            self, "McpProxyRole",
            role_name="{project_name}-mcp-proxy-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        # Read runtime ARNs from SSM at deploy time — no cross-stack construct import
        proxy_role_runtime_arns: list[str] = []
        for proxy_id, ssm_name in mcp_runtime_ssm_names.items():
            arn = ssm.StringParameter.value_for_string_parameter(self, ssm_name)
            proxy_role_runtime_arns.append(arn)
        proxy_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=proxy_role_runtime_arns,
        ))
        iam.PermissionsBoundary.of(proxy_role).apply(permission_boundary)

        # ── Proxy Lambdas + gateway targets ─────────────────────────
        for proxy_id, ssm_name in mcp_runtime_ssm_names.items():
            target_name  = f"{proxy_id.lower()}-mcp-proxy"
            runtime_arn  = ssm.StringParameter.value_for_string_parameter(self, ssm_name)
            schema_file  = _SCHEMAS_ROOT / f"{proxy_id.lower()}_tools.json"
            tool_schema  = json.loads(schema_file.read_text(encoding="utf-8"))

            log_group = logs.LogGroup(
                self, f"{proxy_id}ProxyLogs",
                log_group_name=f"/aws/lambda/{{project_name}}-{target_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )

            proxy_fn = _lambda.Function(
                self, f"{proxy_id}ProxyFn",
                function_name=f"{{project_name}}-{target_name}",
                runtime=_lambda.Runtime.PYTHON_3_13,
                architecture=_lambda.Architecture.ARM_64,
                handler="handler.handler",
                code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "mcp_runtime_proxy")),
                role=proxy_role,
                timeout=Duration.seconds(120),
                memory_size=512,
                environment={"RUNTIME_ARN": runtime_arn},
                log_group=log_group,
                tracing=_lambda.Tracing.ACTIVE,
            )
            proxy_fn.grant_invoke(gateway_role)            # same stack — safe L2 grant
            proxy_fn.add_permission(
                f"AllowGatewayInvoke{proxy_id}",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                source_account=Aws.ACCOUNT_ID,
                action="lambda:InvokeFunction",
            )

            agentcore.CfnGatewayTarget(
                self, f"{proxy_id}Target",
                name=target_name,
                gateway_identifier=self.gateway.attr_gateway_identifier,
                credential_provider_configurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                ],
                target_configuration={
                    "mcp": {
                        "lambda": {
                            "lambdaArn":  proxy_fn.function_arn,
                            "toolSchema": {"inlinePayload": tool_schema},
                        },
                    },
                },
            )
```

### 4.3 Micro-stack gotchas

- **`ssm.StringParameter.value_for_string_parameter`** is deploy-time. The resulting token is fine in IAM `resources=[...]` and Lambda `environment={...}`, but **it cannot be iterated** at synth time (e.g. `for arn in runtime_arns: check(arn)` fails).
- **`mcp_runtime_ssm_names`** is a plain dict of SSM names, not construct refs. Keep it in the app-entry and pass down — no cross-stack imports.
- **Direct Lambda targets** (Monte Carlo etc., analogous to monolith §3.2) can go in the same MS05 stack or their own per-tool stack. In production, co-located for simplicity.
- **`schema_file.read_text()` at synth time** means the JSON file must exist when `cdk synth` runs. Generate-on-demand patterns (build-time codegen) need to run before `synth`.
- **Gateway ARN published via SSM** — consumer stacks (Agents) read it to grant `bedrock-agentcore:InvokeGateway`. Don't try to export the ARN as a CfnOutput and import by stack — SSM is cleaner.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one CDK stack | §3 Monolith |
| Production MSxx layout, multiple MCP runtimes in MS04 | §4 Micro-Stack, SSM-published runtime ARNs |
| Add a tool group with distinct schema | New `CfnGatewayTarget` + JSON in `schemas/`; no new IAM unless a new runtime |
| Switch to OAuth2 federated auth | Replace `authorizer_type="AWS_IAM"` with `OAUTH2` + add identity source config; reissue `CfnGateway` |
| Tool logic moves from Lambda → MCP runtime | Remove direct-target Lambda; add proxy target + runtime (MS04) |
| Per-tenant gateways | One `CfnGateway` per tenant name; use resource tagging for cost split |

---

## 6. Worked example — MS05 gateway stack synthesizes

Save as `tests/sop/test_AGENTCORE_GATEWAY.py`. Offline.

```python
"""SOP verification — MS05 synthesizes gateway + N targets without cross-stack cycles."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_gateway_stack_synthesizes_with_two_proxy_targets(tmp_path):
    app = cdk.App()
    env = _env()

    # Stub schemas expected by the stack
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "redshift_tools.json").write_text('[{"name": "x", "description": "x", "inputSchema": {}}]')
    (tmp_path / "schemas" / "neptune_tools.json").write_text('[{"name": "y", "description": "y", "inputSchema": {}}]')

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    # In real code, _SCHEMAS_ROOT anchors to the repo. For the test harness,
    # import the stack with schemas present on disk.
    from infrastructure.cdk.stacks.ms05_gateway import GatewayStack
    ms05 = GatewayStack(
        app, vpc=vpc,
        mcp_runtime_ssm_names={
            "Redshift": "/test/runtime/redshift_mcp_endpoint",
            "Neptune":  "/test/runtime/neptune_mcp_endpoint",
        },
        permission_boundary=boundary, env=env,
    )

    template = Template.from_stack(ms05)
    template.resource_count_is("AWS::BedrockAgentCore::Gateway",      1)
    template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 2)
    template.resource_count_is("AWS::Lambda::Function",               2)
    template.resource_count_is("AWS::Lambda::Permission",             2)   # AllowGatewayInvoke* (resource policy)
```

---

## 7. References

- `docs/template_params.md` — `GATEWAY_URL_SSM_NAME`, `GATEWAY_ARN_SSM_NAME`, `MCP_RUNTIME_SSM_PREFIX`, `GATEWAY_AUTH_MODE`
- `docs/Feature_Roadmap.md` — feature IDs `AG-03` (Gateway), `AG-04` (MCP targets), `STR-06` (client MCP)
- CDK L1 `AWS::BedrockAgentCore::Gateway`: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-bedrockagentcore-gateway.html
- Gateway + targets overview: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-gateway.html
- Related SOPs: `AGENTCORE_RUNTIME` (MS04 runtimes, SSM ARN publishing), `STRANDS_MCP_TOOLS` (client-side SigV4 + transport), `STRANDS_DEPLOY_LAMBDA` (direct-target Lambdas), `AGENTCORE_IDENTITY` (identity / OAuth2 authorizers), `AGENTCORE_AGENT_CONTROL` (Cedar policy engine for fine-grained tool authz), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MS05 GatewayStack reads MCP runtime ARNs via `value_for_string_parameter` (deploy-time SSM), grants proxy role identity-side `bedrock-agentcore:InvokeAgentRuntime`, keeps `grant_invoke(gateway_role)` only for same-stack Lambda targets. Translated CDK from TypeScript to Python. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — Gateway stack, direct Lambda + proxy patterns, proxy handler, tool-schema JSON, SigV4 client-side auth. |
