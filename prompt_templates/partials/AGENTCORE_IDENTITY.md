# SOP — Bedrock AgentCore Identity (IAM SigV4, Cognito, Per-Agent Roles)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws_iam` · `aws_cognito` · SigV4 / `botocore.auth` · `httpx` ≥ 0.27 · MCP streamable HTTP transport

---

## 1. Purpose

- Codify the two authentication surfaces:
  1. **IAM SigV4** — agent → Gateway, agent → Runtime, agent → any AWS service. No secrets, no rotation; least-privilege role per agent.
  2. **Cognito** — human portal users. User Pool with enforced MFA, password policy, groups for RBAC, advanced security mode.
- Provide the canonical per-agent execution role pattern (Bedrock model invoke + SSM read + optional InvokeGateway / InvokeAgentRuntime / Memory).
- Provide the `HTTPXSigV4Auth` client class used by every agent container talking to Gateway.
- Provide the Cognito User Pool + Pool Groups config for persona-based RBAC.
- Include when the SOW mentions agent authentication, IAM roles, SigV4 signing, Cognito for portal users, per-agent least-privilege, or MFA.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| One CDK stack owns agents + gateway + memory + portal together | **§3 Monolith Variant** |
| Per-agent stacks + MS02-Identity stack owns the Cognito User Pool; runtime ARNs, gateway ARN come from MS04 / MS05 via SSM | **§4 Micro-Stack Variant** |

**Why the split matters.** Every per-agent execution role needs `bedrock-agentcore:InvokeGateway` on the gateway ARN, `InvokeAgentRuntime` on sub-agent runtime ARNs, and `RetrieveMemoryRecords` on a memory ARN. In a monolith, those ARNs are local tokens. In micro-stack the ARNs come from other stacks; reading them via SSM + granting identity-side keeps the dependency graph unidirectional. The Cognito User Pool is typically a singleton in MS02 — other stacks get its ID via SSM or direct construct ref.

---

## 3. Monolith Variant

**Use when:** a POC / single-stack layout. Both IAM identities and Cognito are declared locally.

### 3.1 Per-agent execution role builder

```python
from aws_cdk import Aws, aws_iam as iam


def _create_agent_role(
    self,
    agent_name: str,
    needs_gateway: bool         = True,
    needs_sub_agents: bool      = False,
    needs_memory: bool          = False,
    extra_statements: list[iam.PolicyStatement] | None = None,
) -> iam.Role:
    """Per-agent least-privilege execution role."""
    role = iam.Role(
        self, f"{agent_name}-ExecRole",
        role_name=f"{{project_name}}-{agent_name}-role",
        assumed_by=iam.CompositePrincipal(
            iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            iam.ServicePrincipal("ecs-tasks.amazonaws.com"),   # in case of Fargate migration
        ),
    )

    # Base: Bedrock model invocation (cross-region inference profile)
    role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
    ))
    # Base: SSM read under project prefix
    role.add_to_policy(iam.PolicyStatement(
        actions=["ssm:GetParameter", "ssm:GetParameters"],
        resources=[f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter/{{project_name}}/*"],
    ))

    if needs_gateway:
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway"],
            # Scope to your gateway ARN when known; "*" is a stop-gap
            resources=[f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:gateway/*"],
        ))
    if needs_sub_agents:
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:runtime/*"],
        ))
    if needs_memory:
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:CreateEvent",
            ],
            resources=[f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:memory/*"],
        ))
    for s in (extra_statements or []):
        role.add_to_policy(s)
    return role
```

### 3.2 SigV4 auth for agent → Gateway

```python
"""SigV4 authentication for agent-to-gateway MCP calls."""
import os, boto3, httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.client.streamable_http import streamablehttp_client


class HTTPXSigV4Auth(httpx.Auth):
    """Sign httpx requests with SigV4 for the `bedrock-agentcore` service."""

    def __init__(self, session: boto3.Session, service: str, region: str):
        self.credentials = session.get_credentials().get_frozen_credentials()
        self.service     = service
        self.region      = region

    def auth_flow(self, request):
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content if hasattr(request, 'content') else b'',
        )
        aws_request.headers['Host']         = request.url.host
        aws_request.headers['Content-Type'] = 'application/json'
        SigV4Auth(self.credentials, self.service, self.region).add_auth(aws_request)
        for name, value in aws_request.headers.items():
            request.headers[name] = value
        yield request


def create_gateway_transport(gateway_url: str):
    session = boto3.Session()
    auth = HTTPXSigV4Auth(
        session, 'bedrock-agentcore',
        os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
    )
    return streamablehttp_client(url=gateway_url, auth=auth)
```

### 3.3 Cognito User Pool for portal users

```python
from aws_cdk import aws_cognito as cognito


def _create_user_pool(self) -> cognito.UserPool:
    """Cognito for human portal users — admin-created, MFA required, groups for RBAC."""
    user_pool = cognito.UserPool(
        self, "UserPool",
        user_pool_name="{project_name}-users",
        sign_in_aliases=cognito.SignInAliases(email=True),
        self_sign_up_enabled=False,                  # admin-created only
        mfa=cognito.Mfa.REQUIRED,
        mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
        password_policy=cognito.PasswordPolicy(
            min_length=12,
            require_uppercase=True,
            require_lowercase=True,
            require_digits=True,
            require_symbols=True,
        ),
        advanced_security_mode=cognito.AdvancedSecurityMode.ENFORCED,
        removal_policy=cdk.RemovalPolicy.RETAIN,
    )

    # RBAC personas → Cognito groups; persona policy lives in DDB / SSM
    for group_name in ["cfo", "vp_finance", "director", "analyst", "auditor"]:
        cognito.CfnUserPoolGroup(
            self, f"Group_{group_name}",
            user_pool_id=user_pool.user_pool_id,
            group_name=group_name,
        )
    return user_pool
```

### 3.4 Monolith gotchas

- **`resources=["*"]` is a smell.** Always scope to a known prefix (`runtime/*`, `gateway/*`, `memory/*`). `"*"` in prod IAM fails Security Hub controls SH.IAM.* and CIS controls.
- **`CompositePrincipal`** adds one `sts:AssumeRole` statement per principal. If you never plan to run the container on Fargate, drop `ecs-tasks.amazonaws.com` — keeps the role auditable.
- **`sms=False` on MFA** is intentional — SMS-OTP is SIM-swap-vulnerable; TOTP only.
- **`advanced_security_mode=ENFORCED`** enables adaptive auth + compromised-credentials detection. It is priced per MAU — check cost vs. required SOC2 / HIPAA controls.
- **User Pool removal policy = RETAIN** — never DESTROY a pool that's been in prod; users and group memberships are lost permanently.

---

## 4. Micro-Stack Variant

**Use when:** MS02-Identity owns the User Pool; each agent has its own stack; gateway/runtime/memory ARNs come from SSM.

### 4.1 The five non-negotiables

1. **Anchor any Lambda asset** (e.g. Cognito triggers) to `Path(__file__)`.
2. **Never call `user_pool.grant(...)` or `runtime.grant_invoke(role)`** where resource and role are in different stacks. Read ARNs from SSM; grant identity-side.
3. **Never target cross-stack queues** with `targets.SqsQueue`.
4. **Never split a bucket + OAC** across stacks — not relevant here.
5. **Never set `encryption_key=ext_key`** — Cognito pool encryption uses AWS-owned KMS unless you explicitly swap.

### 4.2 MS02 — `IdentityStack` (User Pool + Pool Groups)

```python
import aws_cdk as cdk
from aws_cdk import (
    Aws, CfnOutput,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_ssm as ssm,
)
from constructs import Construct


class IdentityStack(cdk.Stack):
    """MS02 — Cognito User Pool for portal, plus the shared permission boundary."""

    def __init__(
        self,
        scope: Construct,
        rbac_group_names: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ms02-identity", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Shared permission boundary — applied to every role in every downstream stack
        self.permission_boundary = iam.ManagedPolicy(
            self, "PermissionBoundary",
            managed_policy_name="{project_name}-permission-boundary",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    actions=["iam:*"],
                    resources=["*"],
                    conditions={"StringNotEquals": {"aws:ResourceTag/Project": "{project_name}"}},
                ),
                iam.PolicyStatement(
                    actions=["*"], resources=["*"],   # Allow-all outside IAM (bounded by role policy)
                ),
            ],
        )

        # User Pool
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name="{project_name}-users",
            sign_in_aliases=cognito.SignInAliases(email=True),
            self_sign_up_enabled=False,
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_uppercase=True, require_lowercase=True,
                require_digits=True,    require_symbols=True,
            ),
            advanced_security_mode=cognito.AdvancedSecurityMode.ENFORCED,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        for group_name in (rbac_group_names or ["cfo", "vp_finance", "director", "analyst", "auditor"]):
            cognito.CfnUserPoolGroup(
                self, f"Group_{group_name}",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=group_name,
            )

        # Publish IDs + permission-boundary ARN for downstream stacks
        ssm.StringParameter(
            self, "UserPoolIdParam",
            parameter_name="/{project_name}/identity/user_pool_id",
            string_value=self.user_pool.user_pool_id,
        )
        ssm.StringParameter(
            self, "PermissionBoundaryArnParam",
            parameter_name="/{project_name}/identity/permission_boundary_arn",
            string_value=self.permission_boundary.managed_policy_arn,
        )
        CfnOutput(self, "UserPoolArn", value=self.user_pool.user_pool_arn)
```

### 4.3 Per-agent role — ARNs read from SSM

```python
from aws_cdk import aws_iam as iam, aws_ssm as ssm


def build_agent_role(
    stack: cdk.Stack,
    agent_name: str,
    gateway_arn_ssm_name: str | None = None,
    sub_agent_runtime_arn_ssm_names: list[str] | None = None,
    memory_arn_ssm_name: str | None = None,
) -> iam.Role:
    """Used inside each per-agent stack. Reads ARNs from SSM at deploy time."""
    role = iam.Role(
        stack, f"{agent_name}-ExecRole",
        role_name=f"{{project_name}}-{agent_name}-role",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    )
    role.add_to_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[f"arn:aws:bedrock:{cdk.Aws.REGION}::foundation-model/*"],
    ))
    role.add_to_policy(iam.PolicyStatement(
        actions=["ssm:GetParameter", "ssm:GetParameters"],
        resources=[f"arn:aws:ssm:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:parameter/{{project_name}}/*"],
    ))

    if gateway_arn_ssm_name:
        gateway_arn = ssm.StringParameter.value_for_string_parameter(stack, gateway_arn_ssm_name)
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway"],
            resources=[gateway_arn],
        ))

    for ssm_name in (sub_agent_runtime_arn_ssm_names or []):
        arn = ssm.StringParameter.value_for_string_parameter(stack, ssm_name)
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[arn],
        ))

    if memory_arn_ssm_name:
        memory_arn = ssm.StringParameter.value_for_string_parameter(stack, memory_arn_ssm_name)
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:CreateEvent",
            ],
            resources=[memory_arn],
        ))

    # Apply boundary from MS02
    boundary_arn = ssm.StringParameter.value_for_string_parameter(
        stack, "/{project_name}/identity/permission_boundary_arn",
    )
    iam.PermissionsBoundary.of(role).apply(
        iam.ManagedPolicy.from_managed_policy_arn(stack, "Boundary", boundary_arn),
    )
    return role
```

### 4.4 Micro-stack gotchas

- **`value_for_string_parameter`** returns a token; you cannot do `len(...)` / iterate it at synth time. If you need to loop over N runtime ARNs, pass the list of SSM names as a Python argument and resolve them one by one.
- **Boundary from SSM** — `ManagedPolicy.from_managed_policy_arn` works with tokenised ARNs. Pass the SSM-resolved ARN to it.
- **User Pool ID vs ARN** — most consumers need the ID (for authorizers). Publish both or choose one and stick with it.
- **`ssm.StringParameter.value_from_lookup`** requires synth-time credentials (an AWS profile). Use `value_for_string_parameter` for offline synth.
- **Cognito User Pool is a singleton** — if an agent needs the pool ID for a custom authorizer, read via SSM rather than importing the construct.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack, MS02 owns User Pool + boundary |
| Federated auth (Okta, Entra ID) | Add Cognito identity provider (SAML / OIDC); link `cognito.CfnIdentityProvider` in MS02 |
| Service-to-service auth for non-AWS sub-agents | Keep SigV4 for AWS; issue OAuth2 client-credentials for non-AWS |
| Per-tenant auth | One User Pool per tenant OR one pool + custom attribute `tenant_id` + custom authorizer that scopes claims |
| Agent runs outside AWS | Replace SigV4 with mTLS / Cognito client_credentials; document the trade-off |

---

## 6. Worked example — MS02 + agent role synthesize

Save as `tests/sop/test_AGENTCORE_IDENTITY.py`. Offline.

```python
"""SOP verification — MS02 publishes User Pool + boundary; agent role reads them."""
import aws_cdk as cdk
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ms02_identity_stack():
    app = cdk.App()
    env = _env()

    from infrastructure.cdk.stacks.ms02_identity import IdentityStack
    ms02 = IdentityStack(app, env=env)

    template = Template.from_stack(ms02)
    template.resource_count_is("AWS::Cognito::UserPool",      1)
    template.resource_count_is("AWS::Cognito::UserPoolGroup", 5)
    template.resource_count_is("AWS::IAM::ManagedPolicy",     1)
    template.resource_count_is("AWS::SSM::Parameter",         2)  # user_pool_id + boundary_arn
```

---

## 7. References

- `docs/template_params.md` — `COGNITO_POOL_ID_SSM`, `PERMISSION_BOUNDARY_ARN_SSM`, `RBAC_GROUPS`, `REGION`
- `docs/Feature_Roadmap.md` — feature IDs `SEC-03` (SigV4), `SEC-04` (Cognito), `SEC-05` (permission boundary), `AG-05` (per-agent roles)
- AWS SigV4 request signing: https://docs.aws.amazon.com/general/latest/gr/signing-aws-api-requests.html
- Cognito User Pool Advanced Security: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pool-settings-advanced-security.html
- Related SOPs: `AGENTCORE_RUNTIME` (execution-role trust policy), `AGENTCORE_GATEWAY` (gateway role + InvokeGateway), `STRANDS_MCP_TOOLS` (client SigV4 transport), `AGENTCORE_AGENT_CONTROL` (Cedar policy engine on top of IAM), `LAYER_SECURITY` (KMS + permission boundary pattern at the workload level), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MS02 IdentityStack publishes User Pool ID + boundary ARN via SSM; per-agent stacks read both via `value_for_string_parameter` and grant identity-side on `bedrock-agentcore:InvokeGateway` / `InvokeAgentRuntime` / `RetrieveMemoryRecords`. Tightened IAM `resources=` scopes from `"*"` to `arn:…:runtime/*` / `gateway/*` / `memory/*`. Translated CDK from TypeScript to Python. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — per-agent role builder (TS), SigV4 client auth, Cognito pool + groups. |
