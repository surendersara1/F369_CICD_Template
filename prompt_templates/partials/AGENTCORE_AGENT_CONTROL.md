# PARTIAL: AgentCore Agent Control — Cedar Policy Engine, Guardrails, RBAC, HITL

**Usage:** Include when SOW mentions governance, compliance, Cedar policies, guardrails, RBAC, HITL approval workflows, or agent access control.

---

## Agent Control Architecture (from real production)

```
3-Layer Access Control:
  Layer 1: Agent Access — which agents can this persona invoke?
  Layer 2: Tool Access — which MCP tools can this persona use?
  Layer 3: Data Filter — what data rows/fields can this persona see?

Infrastructure:
  CfnPolicyEngine (Cedar) → attached to Gateway via UpdateGateway
  CfnGuardrail (Bedrock) → attached to model via guardrailConfig
  DynamoDB RBAC table → per-persona policies loaded at runtime
  Step Functions HITL → approval workflows with waitForTaskToken

Flow:
  Agent request → RBAC check (DynamoDB) → Agent invocation
    → Tool call → Cedar policy check (Gateway) → Tool execution
    → Response → Guardrail check (Bedrock) → Data masking → User
    → If amount > threshold → HITL Step Function → Approval/Deny
```

---

## CDK Code Block — Governance Stack

```typescript
// infra/lib/stacks/ms-08-governance-stack.ts
import { aws_bedrockagentcore as agentcore } from 'aws-cdk-lib';

// ======================================================================
// Bedrock Guardrail (content filtering + PII + grounding)
// ======================================================================
const guardrail = new cdk.CfnResource(this, 'BedrockGuardrail', {
  type: 'AWS::Bedrock::Guardrail',
  properties: {
    Name: `{{project_name}}-guardrail`,
    Description: 'Content filtering, PII redaction, topic denial, grounding check',
    BlockedInputMessaging: 'Request blocked by content safety filter.',
    BlockedOutputsMessaging: 'Response filtered for safety.',
    ContentPolicyConfig: {
      FiltersConfig: [
        { Type: 'HATE', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
        { Type: 'INSULTS', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
        { Type: 'SEXUAL', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
        { Type: 'VIOLENCE', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
        { Type: 'MISCONDUCT', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
        { Type: 'PROMPT_ATTACK', InputStrength: 'HIGH', OutputStrength: 'NONE' },
      ],
    },
    SensitiveInformationPolicyConfig: {
      PiiEntitiesConfig: [
        { Type: 'EMAIL', Action: 'ANONYMIZE' },
        { Type: 'PHONE', Action: 'ANONYMIZE' },
        { Type: 'NAME', Action: 'ANONYMIZE' },
        { Type: 'CREDIT_DEBIT_CARD_NUMBER', Action: 'BLOCK' },
      ],
      // [Claude: add RegexesConfig for domain-specific PII patterns]
    },
    TopicPolicyConfig: {
      TopicsConfig: [
        // [Claude: add denied topics from SOW compliance requirements]
        { Name: 'competitor_confidential', Definition: 'Sharing competitor info', Type: 'DENY' },
      ],
    },
    ContextualGroundingPolicyConfig: {
      FiltersConfig: [
        { Type: 'GROUNDING', Threshold: 0.7 },
        { Type: 'RELEVANCE', Threshold: 0.7 },
      ],
    },
    KmsKeyArn: kmsKeyArn,
  },
});

// ======================================================================
// AgentCore Policy Engine (Cedar — CfnPolicyEngine + CfnPolicy)
// ======================================================================
const policyEngine = new agentcore.CfnPolicyEngine(this, 'CedarPolicyEngine', {
  name: `{{project_name}}_policy_engine`,
  encryptionKeyArn: kmsKeyArn,
  description: 'Cedar policy engine for governance rules',
});

// Load Cedar rules from file, create one CfnPolicy per statement
const cedarRaw = fs.readFileSync('infra/cedar/rules.cedar', 'utf-8');
const statements = cedarRaw.split(/\n\s*\n/)
  .map(s => s.trim())
  .filter(s => s.includes('permit(') || s.includes('forbid('));

for (let i = 0; i < statements.length; i++) {
  new agentcore.CfnPolicy(this, `CedarPolicy${i}`, {
    name: `{{project_name}}_rule_${i}`,
    policyEngineId: policyEngine.attrPolicyEngineId,
    definition: { cedar: { statement: statements[i] } },
    validationMode: 'IGNORE_ALL_FINDINGS',
  });
}

// Associate PolicyEngine with Gateway via AwsCustomResource
new cr.AwsCustomResource(this, 'AssociatePolicyEngine', {
  onCreate: {
    service: 'bedrock-agentcore-control',
    action: 'UpdateGateway',
    parameters: {
      gatewayIdentifier: ssmLookup(this, '/{{project_name}}/gateway/id'),
      policyEngineConfiguration: {
        arn: policyEngine.attrPolicyEngineArn,
        mode: 'ENFORCE',  // or 'LOG_ONLY' for testing
      },
    },
  },
});

// ======================================================================
// HITL Step Functions (approval workflows)
// ======================================================================
const hitlStateMachine = new stepfunctions.StateMachine(this, 'HitlApproval', {
  stateMachineName: `{{project_name}}-hitl-approval`,
  definitionBody: stepfunctions.DefinitionBody.fromChainable(
    new stepfunctions.Choice(this, 'AmountRouter')
      .when(stepfunctions.Condition.numberLessThanEquals('$.amount', 1000000),
        new stepfunctions.Pass(this, 'AutoApprove'))
      .when(stepfunctions.Condition.numberLessThanEquals('$.amount', 5000000),
        new stepfunctions.Pass(this, 'VpApproval'))
      .otherwise(new stepfunctions.Pass(this, 'CfoApproval'))
  ),
  timeout: cdk.Duration.hours(48),
});
```

---

## Cedar Policy Rules — Pass 3 Reference

```cedar
// infra/cedar/rules.cedar

// DEFAULT PERMIT — allow all tool calls unless denied below
permit(
  principal, action,
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
);

// RULE-001: Spend Cap — deny if amount exceeds cap without approver
forbid(
  principal, action,
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  context has "amount" && context has "spend_cap" &&
  context.amount > context.spend_cap &&
  !(context has "hitl_override" && context.hitl_override == true)
};

// [Claude: add more forbid rules based on SOW compliance requirements]
```

---

## RBAC Policy Loader — Pass 3 Reference

```python
"""RBAC policy loader — per-persona access control from DynamoDB."""
import boto3, os, logging

logger = logging.getLogger(__name__)
_ddb = boto3.resource('dynamodb')
_cache = {}

DEFAULT_POLICY = {
    'agent_access': {'supervisor': True, 'observer': True, 'reasoner': True},
    'tool_access': {'allowed': ['*'], 'denied': [], 'mode': 'allow_all'},
    'data_filter': {'mask_fields': [], 'sql_filter': ''},
}

def load_rbac_policy(persona: str) -> dict:
    if persona in _cache:
        return _cache[persona]
    try:
        table = _ddb.Table(os.environ.get('RBAC_TABLE', '{{project_name}}-rbac-policies'))
        resp = table.get_item(Key={'persona': persona})
        policy = resp.get('Item', DEFAULT_POLICY)
        _cache[persona] = policy
        return policy
    except Exception as e:
        logger.warning("RBAC load failed for %s: %s — using default", persona, e)
        _cache[persona] = DEFAULT_POLICY
        return DEFAULT_POLICY
```

---

## Guardrail Integration with Strands Model — Pass 3 Reference

```python
"""Attach Bedrock Guardrail to Strands BedrockModel."""
from strands.models import BedrockModel

def build_bedrock_model(model_id: str, guardrail_id: str = '', guardrail_version: str = 'DRAFT'):
    if guardrail_id:
        return BedrockModel(model_id=model_id, additional_request_fields={
            'guardrailConfig': {
                'guardrailIdentifier': guardrail_id,
                'guardrailVersion': guardrail_version,
                'trace': 'enabled',
            }
        })
    return BedrockModel(model_id=model_id)
```
