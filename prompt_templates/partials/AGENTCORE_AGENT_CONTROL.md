# PARTIAL: AgentCore Agent Control — Runtime Guardrails

**Usage:** Include when SOW mentions runtime guardrails, agent control, tool access control, execution boundaries, or agent safety policies.

---

## Agent Control Overview

```
Agent Control = Runtime guardrails without changing agent code:
  - Define what agents can and can't do at runtime
  - Tool access control (allow/deny specific tools)
  - Execution boundaries (time limits, resource limits)
  - Content filtering via Bedrock Guardrails integration
  - No code changes required — policy-based configuration

Agent Control Flow:
  Agent attempts tool call → Agent Control policy check
       ↓ (allowed)                    ↓ (denied)
  Tool executes normally        Tool call blocked with reason
```

---

## Guardrails Integration — Pass 3 Reference

```python
"""Bedrock Guardrails integration with Strands agents."""
from strands import Agent
from strands.models import BedrockModel

# Guardrails applied at model level via Bedrock
model = BedrockModel(
    model_id="anthropic.claude-sonnet-4-20250514-v1:0",
    guardrail_id=os.environ.get("GUARDRAIL_ID"),
    guardrail_version="DRAFT",
)

agent = Agent(
    model=model,
    system_prompt="You are helpful.",
    tools=[...],
)
```

---

## CDK Code Block — Bedrock Guardrails

```python
def _create_agent_guardrails(self, stage_name: str) -> None:
    """
    Bedrock Guardrails for agent content safety.

    [Claude: include for any production agent deployment.
     Customize filters based on SOW compliance requirements.]
    """
    import aws_cdk.aws_bedrock as bedrock

    self.guardrail = bedrock.CfnGuardrail(
        self, "AgentGuardrail",
        name=f"{{project_name}}-guardrail-{stage_name}",
        description="Safety guardrails for {{project_name}} agents",
        content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
            filters_config=[
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="SEXUAL", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="VIOLENCE", input_strength="MEDIUM", output_strength="MEDIUM"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="HATE", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"),
            ],
        ),
        sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
            pii_entities_config=[
                bedrock.CfnGuardrail.GuardrailPiiEntityConfigProperty(type="EMAIL", action="ANONYMIZE"),
                bedrock.CfnGuardrail.GuardrailPiiEntityConfigProperty(type="PHONE", action="ANONYMIZE"),
                bedrock.CfnGuardrail.GuardrailPiiEntityConfigProperty(type="SSN", action="BLOCK"),
                bedrock.CfnGuardrail.GuardrailPiiEntityConfigProperty(type="CREDIT_DEBIT_CARD_NUMBER", action="BLOCK"),
            ],
        ),
        topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
            topics_config=[
                bedrock.CfnGuardrail.GuardrailTopicConfigProperty(
                    name="Competitor Discussion", type="DENY",
                    definition="Comparing to competitor products",
                    examples=["How does this compare to ChatGPT?"],
                ),
            ],
        ),
        blocked_input_messaging="I can't respond to that request.",
        blocked_output_messaging="I can't provide that information.",
    )

    CfnOutput(self, "GuardrailId", value=self.guardrail.attr_guardrail_id)
```
