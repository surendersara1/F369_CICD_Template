# SOP — Bedrock AgentCore Agent Control (Cedar, Guardrails, RBAC, HITL)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · L1 `aws_bedrockagentcore.CfnPolicyEngine` / `CfnPolicy` · `AWS::Bedrock::Guardrail` (via `CfnResource`) · Step Functions · DynamoDB · `custom_resources.AwsCustomResource`

---

## 1. Purpose

- Provision the 3-layer access-control surface:
  1. **Cedar policy engine** attached to the AgentCore Gateway (`CfnPolicyEngine` + per-statement `CfnPolicy`, associated via `UpdateGateway` custom resource).
  2. **Bedrock Guardrail** — content filter, PII redaction, topic denial, contextual grounding — referenced by `BedrockModel.additional_request_fields.guardrailConfig`.
  3. **DynamoDB RBAC table** — per-persona `agent_access` / `tool_access` / `data_filter` policies loaded at runtime by `AfieSteeringHooks` (see `STRANDS_HOOKS_PLUGINS`).
- Provision the HITL approval state machine (`waitForTaskToken`) with amount-tiered routing (AUTO → VP → CFO).
- Wire Cedar rules from a repo-anchored `rules.cedar` file into individual `CfnPolicy` resources (one per `permit`/`forbid` statement).
- Include when the SOW mentions governance, compliance, Cedar policies, guardrails, RBAC, HITL approval workflows, or agent access control.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single CDK stack owns guardrail + Cedar engine + RBAC table + HITL SFN | **§3 Monolith Variant** |
| MS08-Governance owns the control plane; MS05-Gateway is a different stack (the `UpdateGateway` association is the tricky cross-stack link) | **§4 Micro-Stack Variant** |

**Why the split matters.** The Cedar `PolicyEngine` must be attached to the Gateway via `UpdateGateway`. In a monolith the gateway identifier is a local construct ref. In micro-stack, it's an SSM-published string, and the `AwsCustomResource` runs in the governance stack (MS08) but calls the control-plane API against a gateway owned by MS05. IAM for the custom resource must allow `bedrock-agentcore-control:UpdateGateway` on the specific gateway ARN — identity-side in MS08.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Bedrock Guardrail

```python
import aws_cdk as cdk
from aws_cdk import Aws, aws_kms as kms


def _create_guardrail(self, cmk: kms.IKey) -> cdk.CfnResource:
    """AWS::Bedrock::Guardrail via generic CfnResource (no L2 at time of writing)."""
    return cdk.CfnResource(
        self, "BedrockGuardrail",
        type="AWS::Bedrock::Guardrail",
        properties={
            "Name":                    "{project_name}-guardrail",
            "Description":             "Content filter, PII redaction, topic denial, grounding",
            "BlockedInputMessaging":   "Request blocked by content safety filter.",
            "BlockedOutputsMessaging": "Response filtered for safety.",
            "ContentPolicyConfig": {
                "FiltersConfig": [
                    {"Type": "HATE",          "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "INSULTS",       "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "SEXUAL",        "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "VIOLENCE",      "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "MISCONDUCT",    "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"},
                ],
            },
            "SensitiveInformationPolicyConfig": {
                "PiiEntitiesConfig": [
                    {"Type": "EMAIL",                    "Action": "ANONYMIZE"},
                    {"Type": "PHONE",                    "Action": "ANONYMIZE"},
                    {"Type": "NAME",                     "Action": "ANONYMIZE"},
                    {"Type": "CREDIT_DEBIT_CARD_NUMBER", "Action": "BLOCK"},
                ],
                # [Claude: add RegexesConfig for domain-specific PII patterns]
            },
            "TopicPolicyConfig": {
                "TopicsConfig": [
                    {"Name": "competitor_confidential",
                     "Definition": "Sharing competitor-confidential information",
                     "Type": "DENY"},
                    # [Claude: add denied topics from SOW compliance requirements]
                ],
            },
            "ContextualGroundingPolicyConfig": {
                "FiltersConfig": [
                    {"Type": "GROUNDING", "Threshold": 0.7},
                    {"Type": "RELEVANCE", "Threshold": 0.7},
                ],
            },
            "KmsKeyArn": cmk.key_arn,
        },
    )
```

### 3.2 Cedar Policy Engine + Policies + Gateway association

```python
from pathlib import Path
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    custom_resources as cr,
)


def _create_policy_engine(self, cmk: kms.IKey, gateway_identifier: str) -> agentcore.CfnPolicyEngine:
    """Cedar engine + one CfnPolicy per statement + UpdateGateway association."""
    engine = agentcore.CfnPolicyEngine(
        self, "CedarPolicyEngine",
        name="{project_name}_policy_engine",
        encryption_key_arn=cmk.key_arn,
        description="Cedar policy engine for governance rules",
    )

    # Load Cedar rules from a repo-anchored file at synth time
    cedar_raw = Path("infra/cedar/rules.cedar").read_text(encoding="utf-8")
    statements = [
        s.strip()
        for s in cedar_raw.split("\n\n")
        if "permit(" in s or "forbid(" in s
    ]

    for i, stmt in enumerate(statements):
        agentcore.CfnPolicy(
            self, f"CedarPolicy{i}",
            name=f"{{project_name}}_rule_{i}",
            policy_engine_id=engine.attr_policy_engine_id,
            definition={"cedar": {"statement": stmt}},
            validation_mode="IGNORE_ALL_FINDINGS",
        )

    # Associate the engine with the Gateway via UpdateGateway control API
    cr.AwsCustomResource(
        self, "AssociatePolicyEngine",
        on_create=cr.AwsSdkCall(
            service="bedrock-agentcore-control",
            action="UpdateGateway",
            parameters={
                "gatewayIdentifier": gateway_identifier,
                "policyEngineConfiguration": {
                    "arn":  engine.attr_policy_engine_arn,
                    "mode": "ENFORCE",     # or LOG_ONLY in staging
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of("assoc-" + gateway_identifier),
        ),
        policy=cr.AwsCustomResourcePolicy.from_statements([
            iam.PolicyStatement(
                actions=["bedrock-agentcore-control:UpdateGateway"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:gateway/{gateway_identifier}",
                ],
            ),
        ]),
    )
    return engine
```

### 3.3 HITL Step Functions (amount-tiered approval)

```python
from aws_cdk import (
    Duration,
    aws_stepfunctions as sfn,
)


def _create_hitl_state_machine(self) -> sfn.StateMachine:
    """Amount → tier → approval path. Use `waitForTaskToken` integrations in prod."""
    auto_approve = sfn.Pass(self, "AutoApprove", comment="Amount ≤ 1M → auto")
    vp_approval  = sfn.Pass(self, "VpApproval",  comment="1M < amount ≤ 5M → VP Finance")
    cfo_approval = sfn.Pass(self, "CfoApproval", comment="> 5M → CFO + Board")

    router = (
        sfn.Choice(self, "AmountRouter")
        .when(sfn.Condition.number_less_than_equals("$.amount", 1_000_000), auto_approve)
        .when(sfn.Condition.number_less_than_equals("$.amount", 5_000_000), vp_approval)
        .otherwise(cfo_approval)
    )

    return sfn.StateMachine(
        self, "HitlApproval",
        state_machine_name="{project_name}-hitl-approval",
        definition_body=sfn.DefinitionBody.from_chainable(router),
        timeout=Duration.hours(48),
    )
```

### 3.4 RBAC table (DynamoDB)

```python
from aws_cdk import aws_dynamodb as ddb


def _create_rbac_table(self, cmk: kms.IKey) -> ddb.Table:
    return ddb.Table(
        self, "RbacPoliciesTable",
        table_name="{project_name}-rbac-policies",
        partition_key=ddb.Attribute(name="persona", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=cmk,
        point_in_time_recovery=True,
        removal_policy=cdk.RemovalPolicy.RETAIN,
    )
```

### 3.5 RBAC loader (runs in agent container)

```python
"""RBAC policy loader — per-persona from DynamoDB with in-process cache."""
import boto3, logging, os

logger = logging.getLogger(__name__)
_ddb   = boto3.resource('dynamodb')
_cache: dict[str, dict] = {}

DEFAULT_POLICY = {
    'agent_access': {'supervisor': True, 'observer': True, 'reasoner': True},
    'tool_access':  {'allowed': ['*'], 'denied': [], 'mode': 'allow_all'},
    'data_filter':  {'mask_fields': [], 'sql_filter': ''},
}


def load_rbac_policy(persona: str) -> dict:
    if persona in _cache:
        return _cache[persona]
    try:
        table = _ddb.Table(os.environ.get('RBAC_TABLE', '{project_name}-rbac-policies'))
        resp  = table.get_item(Key={'persona': persona})
        policy = resp.get('Item', DEFAULT_POLICY)
        _cache[persona] = policy
        return policy
    except Exception as e:
        logger.warning("RBAC load failed for %s: %s — using default", persona, e)
        _cache[persona] = DEFAULT_POLICY
        return DEFAULT_POLICY
```

### 3.6 Guardrail → Strands model wiring

```python
"""Attach Bedrock Guardrail to Strands BedrockModel (see also STRANDS_MODEL_PROVIDERS §3.3)."""
from strands.models import BedrockModel


def build_bedrock_model(model_id: str, guardrail_id: str = '', guardrail_version: str = 'DRAFT'):
    if guardrail_id:
        return BedrockModel(
            model_id=model_id,
            additional_request_fields={
                'guardrailConfig': {
                    'guardrailIdentifier': guardrail_id,
                    'guardrailVersion':    guardrail_version,
                    'trace':               'enabled',
                },
            },
        )
    return BedrockModel(model_id=model_id)
```

### 3.7 Monolith gotchas

- **Cedar file parsing.** Splitting on `\n\n` is brittle — a rule with an internal blank line will be split. Use a real Cedar parser (e.g. `cedarpy`) or author rules as one-per-file and glob them.
- **`validation_mode="IGNORE_ALL_FINDINGS"`** is a dev shortcut. In prod set `VALIDATE` and fix findings rather than ignore them.
- **UpdateGateway mode `ENFORCE` vs `LOG_ONLY`** — start in `LOG_ONLY` in staging, review `bedrock-agentcore:AuthorizeAction` CloudTrail events for a week, then switch to `ENFORCE`. Going straight to `ENFORCE` risks blocking legitimate traffic.
- **`AWS::Bedrock::Guardrail` is L1 via `CfnResource`** because the typed L1 isn't exposed for all properties yet. Watch for L2 / typed L1 emergence in CDK releases.
- **HITL `sfn.Pass`** states are placeholders — in production replace with `tasks.LambdaInvoke` (or `SqsSendMessage` + `waitForTaskToken`) to actually page approvers and persist the token.
- **Guardrail + Cedar are orthogonal.** Guardrail filters content in LLM calls; Cedar authorises tool calls at the Gateway. Use both.

---

## 4. Micro-Stack Variant

**Use when:** MS08-Governance owns the control plane; Gateway is in MS05.

### 4.1 The five non-negotiables

1. **Anchor `rules.cedar`** to `Path(__file__)`-anchored repo root.
2. **Never call `engine.grant_use(gateway)`** cross-stack — use `AwsCustomResource` with identity-side IAM scoped to the gateway ARN.
3. **Never target cross-stack queues** with `targets.SqsQueue` for HITL notification; use identity-side `sqs:SendMessage`.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** — MS08 owns its own CMK.

### 4.2 MS08 — `GovernanceStack`

```python
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, CfnOutput,
    aws_bedrockagentcore as agentcore,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_kms as kms,
    aws_ssm as ssm,
    aws_stepfunctions as sfn,
    custom_resources as cr,
)
from constructs import Construct

_CEDAR_RULES: Path = Path(__file__).resolve().parents[3] / "infra" / "cedar" / "rules.cedar"


class GovernanceStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        gateway_identifier_ssm_name: str,
        gateway_arn_ssm_name: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ms08-governance", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # Local CMK for governance resources
        cmk = kms.Key(self, "GovernanceKey",
            alias="alias/{project_name}-governance",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        # Guardrail — Bedrock (L1 via CfnResource)
        guardrail = cdk.CfnResource(self, "BedrockGuardrail",
            type="AWS::Bedrock::Guardrail",
            properties={
                "Name":                    "{project_name}-guardrail",
                "Description":             "Content filter, PII redaction, topic denial, grounding",
                "BlockedInputMessaging":   "Request blocked by content safety filter.",
                "BlockedOutputsMessaging": "Response filtered for safety.",
                "ContentPolicyConfig":             {"FiltersConfig": [
                    {"Type": "HATE",          "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "INSULTS",       "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "SEXUAL",        "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "VIOLENCE",      "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "MISCONDUCT",    "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                    {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"},
                ]},
                "SensitiveInformationPolicyConfig": {"PiiEntitiesConfig": [
                    {"Type": "EMAIL",                    "Action": "ANONYMIZE"},
                    {"Type": "PHONE",                    "Action": "ANONYMIZE"},
                    {"Type": "NAME",                     "Action": "ANONYMIZE"},
                    {"Type": "CREDIT_DEBIT_CARD_NUMBER", "Action": "BLOCK"},
                ]},
                "TopicPolicyConfig":                {"TopicsConfig": [
                    {"Name": "competitor_confidential",
                     "Definition": "Sharing competitor-confidential information",
                     "Type": "DENY"},
                ]},
                "ContextualGroundingPolicyConfig":  {"FiltersConfig": [
                    {"Type": "GROUNDING", "Threshold": 0.7},
                    {"Type": "RELEVANCE", "Threshold": 0.7},
                ]},
                "KmsKeyArn": cmk.key_arn,
            },
        )

        # Cedar engine
        engine = agentcore.CfnPolicyEngine(self, "CedarPolicyEngine",
            name="{project_name}_policy_engine",
            encryption_key_arn=cmk.key_arn,
            description="Cedar policy engine for governance rules",
        )

        cedar_raw = _CEDAR_RULES.read_text(encoding="utf-8")
        statements = [s.strip() for s in cedar_raw.split("\n\n") if "permit(" in s or "forbid(" in s]
        for i, stmt in enumerate(statements):
            agentcore.CfnPolicy(self, f"CedarPolicy{i}",
                name=f"{{project_name}}_rule_{i}",
                policy_engine_id=engine.attr_policy_engine_id,
                definition={"cedar": {"statement": stmt}},
                validation_mode="VALIDATE",
            )

        # Associate engine with gateway (gateway owned by MS05)
        gateway_identifier = ssm.StringParameter.value_for_string_parameter(self, gateway_identifier_ssm_name)
        gateway_arn        = ssm.StringParameter.value_for_string_parameter(self, gateway_arn_ssm_name)

        cr.AwsCustomResource(self, "AssociatePolicyEngine",
            on_create=cr.AwsSdkCall(
                service="bedrock-agentcore-control",
                action="UpdateGateway",
                parameters={
                    "gatewayIdentifier": gateway_identifier,
                    "policyEngineConfiguration": {
                        "arn":  engine.attr_policy_engine_arn,
                        "mode": "ENFORCE",
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"assoc-{Aws.STACK_NAME}"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["bedrock-agentcore-control:UpdateGateway"],
                    resources=[gateway_arn],
                ),
            ]),
        )

        # RBAC table
        self.rbac_table = ddb.Table(self, "RbacPoliciesTable",
            table_name="{project_name}-rbac-policies",
            partition_key=ddb.Attribute(name="persona", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # HITL SFN (placeholder states — swap to waitForTaskToken in prod)
        router = (
            sfn.Choice(self, "AmountRouter")
            .when(sfn.Condition.number_less_than_equals("$.amount", 1_000_000),
                  sfn.Pass(self, "AutoApprove"))
            .when(sfn.Condition.number_less_than_equals("$.amount", 5_000_000),
                  sfn.Pass(self, "VpApproval"))
            .otherwise(sfn.Pass(self, "CfoApproval"))
        )
        self.hitl_sm = sfn.StateMachine(self, "HitlApproval",
            state_machine_name="{project_name}-hitl-approval",
            definition_body=sfn.DefinitionBody.from_chainable(router),
            timeout=Duration.hours(48),
        )

        # Publish for agent stacks
        ssm.StringParameter(self, "RbacTableParam",
            parameter_name="/{project_name}/governance/rbac_table",
            string_value=self.rbac_table.table_name,
        )
        ssm.StringParameter(self, "GuardrailIdParam",
            parameter_name="/{project_name}/governance/guardrail_id",
            string_value=guardrail.get_att("GuardrailId").to_string(),
        )
        ssm.StringParameter(self, "HitlSmArnParam",
            parameter_name="/{project_name}/governance/hitl_sm_arn",
            string_value=self.hitl_sm.state_machine_arn,
        )

        # Apply boundary on all roles we create here
        iam.PermissionsBoundary.of(self.hitl_sm.role).apply(permission_boundary)
```

### 4.3 Identity-side grants in per-agent stack

```python
# inside an agent stack
rbac_table   = ssm.StringParameter.value_for_string_parameter(self, "/{project_name}/governance/rbac_table")
guardrail_id = ssm.StringParameter.value_for_string_parameter(self, "/{project_name}/governance/guardrail_id")
hitl_sm_arn  = ssm.StringParameter.value_for_string_parameter(self, "/{project_name}/governance/hitl_sm_arn")

agent_role.add_to_policy(iam.PolicyStatement(
    actions=["dynamodb:GetItem"],
    resources=[f"arn:aws:dynamodb:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/{rbac_table}"],
))
agent_role.add_to_policy(iam.PolicyStatement(
    actions=["states:StartExecution"],
    resources=[hitl_sm_arn],
))
# Guardrail ID is just a string the model reads; no IAM action on Bedrock Guardrail
env = {
    "RBAC_TABLE":   rbac_table,
    "GUARDRAIL_ID": guardrail_id,
    "HITL_SM_ARN":  hitl_sm_arn,
}
```

### 4.4 Micro-stack gotchas

- **`AwsCustomResource` in MS08 with `UpdateGateway` on MS05's gateway** — the action runs at deploy time of MS08. If MS05 hasn't been deployed yet, the SSM lookup returns a broken token → custom resource fails. Add `stack.add_dependency(ms05)`.
- **`validation_mode="VALIDATE"`** in prod — any syntax error in `rules.cedar` fails the stack deploy. Use a CI step that runs `cedar validate` before `cdk deploy`.
- **SSM param for guardrail ID** — `CfnResource.get_att("GuardrailId")` returns a token; `.to_string()` is needed for SSM's `string_value=`.
- **HITL `sm.role`** — the SFN role needs permission to call downstream Lambdas / SQS. Grant those identity-side in MS08; do not rely on cross-stack `fn.grant_invoke(sm.role)`.
- **Cedar rule ID drift** — if you rename statements or reorder, CDK regenerates `CfnPolicy<N>` resources and may delete+create policies, causing a brief enforcement gap. Number by logical ID not index.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx layout | §4 Micro-Stack, MS08 owns the plane |
| Start-up / staging — want Cedar feedback without blocking | `mode=LOG_ONLY` in `UpdateGateway` |
| Rule authoring is bottleneck | Move Cedar file to a shared repo, add CI validation, split per-statement files + glob loader |
| Guardrail tuning | Add `RegexesConfig` for domain PII; change `ContentPolicyConfig.FiltersConfig` thresholds |
| HITL escalation to humans | Replace `sfn.Pass` with `tasks.LambdaInvoke.waitForTaskToken` + SQS → paging tool |
| Multi-tenant | Partition RBAC table by `tenant_id#persona`; update loader to fetch per-tenant |

---

## 6. Worked example — MS08 governance stack synthesizes

Save as `tests/sop/test_AGENTCORE_AGENT_CONTROL.py`. Offline.

```python
"""SOP verification — MS08 produces guardrail, Cedar engine, RBAC table, HITL SM."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_ms08_governance_stack(tmp_path):
    app = cdk.App()
    env = _env()

    # Stub cedar rules so _CEDAR_RULES.read_text works
    (tmp_path / "infra" / "cedar").mkdir(parents=True)
    (tmp_path / "infra" / "cedar" / "rules.cedar").write_text(
        'permit(principal, action, resource);\n\nforbid(principal, action, resource) when { context.amount > 1 };'
    )

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.ms08_governance import GovernanceStack
    ms08 = GovernanceStack(
        app,
        gateway_identifier_ssm_name="/test/mcp/gateway_identifier",
        gateway_arn_ssm_name="/test/mcp/gateway_arn",
        permission_boundary=boundary,
        env=env,
    )

    template = Template.from_stack(ms08)
    template.resource_count_is("AWS::Bedrock::Guardrail",                  1)
    template.resource_count_is("AWS::BedrockAgentCore::PolicyEngine",      1)
    template.resource_count_is("AWS::BedrockAgentCore::Policy",            2)   # permit + forbid
    template.resource_count_is("AWS::DynamoDB::Table",                     1)
    template.resource_count_is("AWS::StepFunctions::StateMachine",         1)
    template.resource_count_is("AWS::SSM::Parameter",                      3)   # rbac, guardrail, hitl
```

---

## 7. References

- `docs/template_params.md` — `RBAC_TABLE_SSM_NAME`, `GUARDRAIL_ID_SSM_NAME`, `HITL_SM_ARN_SSM_NAME`, `CEDAR_RULES_PATH`, `CEDAR_VALIDATION_MODE`
- `docs/Feature_Roadmap.md` — feature IDs `GOV-01..GOV-11` (governance), `SEC-12` (RBAC), `A-30` (guardrail)
- Cedar language spec: https://docs.cedarpolicy.com/
- AgentCore PolicyEngine: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-gateway-policy-engine.html
- Bedrock Guardrails: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html
- Related SOPs: `STRANDS_MODEL_PROVIDERS` (`guardrailConfig` wiring), `STRANDS_HOOKS_PLUGINS` (in-process RBAC + circuit breaker), `AGENTCORE_GATEWAY` (Gateway IAM + target config), `AGENTCORE_IDENTITY` (permission boundary, personas), `WORKFLOW_STEP_FUNCTIONS` (HITL patterns), `LAYER_SECURITY` (customer-managed KMS), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MS08 GovernanceStack reads gateway identifier/ARN via SSM, associates Cedar engine via `AwsCustomResource` scoped identity-side, publishes RBAC table / guardrail ID / HITL ARN via SSM. Agent stacks grant identity-side `dynamodb:GetItem` and `states:StartExecution`. Translated CDK from TypeScript to Python. Added Swap matrix (§5), Worked example (§6), Gotchas on Cedar file parsing, validation_mode, LOG_ONLY → ENFORCE migration, and Cedar rule ID drift. |
| 1.0 | 2026-03-05 | Initial — guardrail, Cedar engine, UpdateGateway, HITL SFN, RBAC table, RBAC loader, guardrail model wiring. |
