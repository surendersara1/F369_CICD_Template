# SOP — Bedrock Flows + Prompt Management (visual orchestration · prompt versions · A/B testing · prompt routing)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Bedrock Flows (GA Aug 2024) · Bedrock Prompt Management API · Prompt versions + drafts + variants · A/B testing via prompt variants · Prompt routing (route by classifier) · Flow nodes (Prompt + Lambda + KB + Agent + Iterator + Storage + Condition) · Flow versions

---

## 1. Purpose

- Codify **Bedrock Flows** as the visual GenAI orchestration tool — drag-and-drop multi-step pipelines without code. For business analysts + low-code teams.
- Codify **Prompt Management** — version-controlled prompt registry, drafts, variants for A/B, prompt routing.
- Codify the **flow node types**: Prompt, Lambda, Knowledge Base, Agent, Iterator (loop), Collector (gather), Condition (branch), Storage (S3 read/write), Input/Output.
- Codify **A/B testing** with prompt variants — same prompt with different model / system / messages; route by random or feature flag.
- Codify **prompt routing** — classifier prompt picks downstream prompt/agent.
- This is the **low-code orchestration specialisation**. Pairs with `BEDROCK_AGENTS_MULTI_AGENT` (more code-driven), `LLMOPS_BEDROCK` (raw model invoke).

When the SOW signals: "no-code GenAI flows", "prompt versioning", "A/B test prompts", "Bedrock Flows", "visual workflow for content team".

---

## 2. Decision tree — Flows vs Agents vs Step Functions

| Need | Bedrock Flows | Bedrock Agents | Step Functions + Bedrock |
|---|:---:|:---:|:---:|
| Visual builder | ✅ | ❌ | ⚠️ Workflow Studio |
| Branching / conditions | ✅ | ⚠️ via prompt | ✅ |
| Loops / iteration | ✅ Iterator node | ⚠️ via prompt | ✅ Map state |
| Tool/action execution | ⚠️ via Lambda node | ✅ Action Groups | ✅ tasks |
| Reasoning loop (ReAct) | ❌ | ✅ | ⚠️ build it |
| Long-running (hours+) | ❌ flow timeout 30 min | ❌ session timeout | ✅ |
| Best for | Linear/branched content pipelines | Conversational + tools | Workflow automation |

**Recommendation:**
- **Bedrock Flows** for content pipelines (article gen, summarization, classification routes).
- **Bedrock Agents** for chat + tool use.
- **Step Functions** for long-running workflows with retries, parallel, timers.

```
Bedrock Flow architecture:

   Input node  ──► Prompt node (classifier)
                          │
                          ├── Condition: category=='technical'
                          │     │
                          │     ▼
                          │   Prompt node (technical-expert)
                          │     │
                          │     ▼
                          │   Output node
                          │
                          ├── Condition: category=='billing'
                          │     │
                          │     ▼
                          │   Lambda node (lookup-account)
                          │     │
                          │     ▼
                          │   Prompt node (billing-formatter)
                          │     │
                          │     ▼
                          │   Output node
                          │
                          └── Default
                                ▼
                              KB node (general-faq)
                                │
                                ▼
                              Output node
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single flow with 3 nodes | **§3 Monolith** |
| Production — Prompt Management + variants + A/B + flow versioning | **§5 Production** |

---

## 3. Monolith Variant — single Bedrock Flow

### 3.1 CDK

```python
# stacks/bedrock_flow_stack.py
from aws_cdk import Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from constructs import Construct
import json


class BedrockFlowStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Flow execution role ────────────────────────────────────
        flow_role = iam.Role(self, "FlowRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
        )
        flow_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-haiku-4-5-20251001",
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-sonnet-4-6",
            ],
        ))

        # ── 2. Bedrock Flow ──────────────────────────────────────────
        flow = bedrock.CfnFlow(self, "ContentFlow",
            name=f"{env_name}-content-flow",
            description="Article generation flow: classify → expert → format",
            execution_role_arn=flow_role.role_arn,
            customer_encryption_key_arn=kms_key_arn,
            definition=bedrock.CfnFlow.FlowDefinitionProperty(
                nodes=[
                    # Input node
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="FlowInput",
                        type="Input",
                        outputs=[bedrock.CfnFlow.FlowNodeOutputProperty(
                            name="document", type="String",
                        )],
                    ),
                    # Classifier — Haiku for speed
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="Classifier",
                        type="Prompt",
                        inputs=[bedrock.CfnFlow.FlowNodeInputProperty(
                            name="user_input", type="String",
                            expression="$.data",                # JSONPath from input
                        )],
                        outputs=[bedrock.CfnFlow.FlowNodeOutputProperty(
                            name="modelCompletion", type="String",
                        )],
                        configuration=bedrock.CfnFlow.FlowNodeConfigurationProperty(
                            prompt=bedrock.CfnFlow.PromptFlowNodeConfigurationProperty(
                                source_configuration=bedrock.CfnFlow.PromptFlowNodeSourceConfigurationProperty(
                                    inline=bedrock.CfnFlow.PromptFlowNodeInlineConfigurationProperty(
                                        model_id="anthropic.claude-haiku-4-5-20251001",
                                        inference_configuration=bedrock.CfnFlow.PromptInferenceConfigurationProperty(
                                            text=bedrock.CfnFlow.PromptModelInferenceConfigurationProperty(
                                                temperature=0.0,
                                                max_tokens=10,
                                            ),
                                        ),
                                        template_type="TEXT",
                                        template_configuration=bedrock.CfnFlow.PromptTemplateConfigurationProperty(
                                            text=bedrock.CfnFlow.TextPromptTemplateConfigurationProperty(
                                                text="Classify into ONE word: technical | billing | general\n\n{{user_input}}",
                                                input_variables=[
                                                    bedrock.CfnFlow.PromptInputVariableProperty(name="user_input"),
                                                ],
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                    # Condition node — branch on classifier output
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="CategoryRouter",
                        type="Condition",
                        inputs=[bedrock.CfnFlow.FlowNodeInputProperty(
                            name="category", type="String",
                            expression="$.data",
                        )],
                        configuration=bedrock.CfnFlow.FlowNodeConfigurationProperty(
                            condition=bedrock.CfnFlow.ConditionFlowNodeConfigurationProperty(
                                conditions=[
                                    bedrock.CfnFlow.FlowConditionProperty(
                                        name="IsTechnical",
                                        expression="category == \"technical\"",
                                    ),
                                    bedrock.CfnFlow.FlowConditionProperty(
                                        name="IsBilling",
                                        expression="category == \"billing\"",
                                    ),
                                    bedrock.CfnFlow.FlowConditionProperty(name="default"),
                                ],
                            ),
                        ),
                    ),
                    # Expert prompt nodes (technical / billing / general)
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="TechnicalExpert",
                        type="Prompt",
                        # ... using Sonnet for technical depth ...
                    ),
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="BillingFormatter",
                        type="Prompt",
                        # ...
                    ),
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="GeneralFaq",
                        type="KnowledgeBase",
                        # ... routes to KB for general questions ...
                    ),
                    # Output node
                    bedrock.CfnFlow.FlowNodeProperty(
                        name="FlowOutput",
                        type="Output",
                        inputs=[bedrock.CfnFlow.FlowNodeInputProperty(
                            name="response", type="String",
                            expression="$.data",
                        )],
                    ),
                ],
                connections=[
                    # Input → Classifier
                    bedrock.CfnFlow.FlowConnectionProperty(
                        name="InputToClassifier",
                        source="FlowInput",
                        target="Classifier",
                        type="Data",
                        configuration=bedrock.CfnFlow.FlowConnectionConfigurationProperty(
                            data=bedrock.CfnFlow.FlowDataConnectionConfigurationProperty(
                                source_output="document",
                                target_input="user_input",
                            ),
                        ),
                    ),
                    # Classifier → Router
                    bedrock.CfnFlow.FlowConnectionProperty(
                        name="ClassifierToRouter", source="Classifier", target="CategoryRouter",
                        type="Data",
                        configuration=bedrock.CfnFlow.FlowConnectionConfigurationProperty(
                            data=bedrock.CfnFlow.FlowDataConnectionConfigurationProperty(
                                source_output="modelCompletion", target_input="category",
                            ),
                        ),
                    ),
                    # Router → branches (Conditional connections)
                    bedrock.CfnFlow.FlowConnectionProperty(
                        name="RouterToTechnical", source="CategoryRouter", target="TechnicalExpert",
                        type="Conditional",
                        configuration=bedrock.CfnFlow.FlowConnectionConfigurationProperty(
                            conditional=bedrock.CfnFlow.FlowConditionalConnectionConfigurationProperty(
                                condition="IsTechnical",
                            ),
                        ),
                    ),
                    # ... other branches ...
                ],
            ),
        )

        # ── 3. Flow alias (production version) ───────────────────────
        flow_alias = bedrock.CfnFlowAlias(self, "FlowAlias",
            flow_arn=flow.attr_arn,
            name="prod",
            description="Production version 1",
            routing_configuration=[
                bedrock.CfnFlowAlias.FlowAliasRoutingConfigurationListItemProperty(
                    flow_version="1",
                ),
            ],
        )
```

### 3.2 Invoke flow

```python
import boto3
agent_runtime = boto3.client("bedrock-agent-runtime")

resp = agent_runtime.invoke_flow(
    flowIdentifier=flow_arn,
    flowAliasIdentifier=flow_alias_id,
    inputs=[{
        "content": {"document": "How do I configure SSO for our API?"},
        "nodeName": "FlowInput",
        "nodeOutputName": "document",
    }],
)
for event in resp["responseStream"]:
    if "flowOutputEvent" in event:
        print(event["flowOutputEvent"]["content"]["document"])
```

---

## 4. Prompt Management — versioned prompts + variants + A/B

### 4.1 CDK

```python
# Create a managed prompt
classifier_prompt = bedrock.CfnPrompt(self, "ClassifierPrompt",
    name="content-classifier",
    description="Classify user query into category",
    customer_encryption_key_arn=kms_key_arn,
    default_variant="v1-haiku",
    variants=[
        # Variant 1: Haiku (cheap, fast)
        bedrock.CfnPrompt.PromptVariantProperty(
            name="v1-haiku",
            template_type="TEXT",
            template_configuration=bedrock.CfnPrompt.PromptTemplateConfigurationProperty(
                text=bedrock.CfnPrompt.TextPromptTemplateConfigurationProperty(
                    text="Classify into ONE word: technical | billing | general\n\n{{query}}",
                    input_variables=[
                        bedrock.CfnPrompt.PromptInputVariableProperty(name="query"),
                    ],
                ),
            ),
            inference_configuration=bedrock.CfnPrompt.PromptInferenceConfigurationProperty(
                text=bedrock.CfnPrompt.PromptModelInferenceConfigurationProperty(
                    temperature=0.0,
                    max_tokens=10,
                ),
            ),
            model_id="anthropic.claude-haiku-4-5-20251001",
        ),
        # Variant 2: Sonnet w/ chain-of-thought (better, slower, expensive)
        bedrock.CfnPrompt.PromptVariantProperty(
            name="v2-sonnet-cot",
            template_type="CHAT",                              # chat-style
            template_configuration=bedrock.CfnPrompt.PromptTemplateConfigurationProperty(
                chat=bedrock.CfnPrompt.ChatPromptTemplateConfigurationProperty(
                    system=[bedrock.CfnPrompt.SystemContentBlockProperty(
                        text="You are a precise classifier. Think step-by-step.",
                    )],
                    messages=[
                        bedrock.CfnPrompt.MessageProperty(
                            role="user",
                            content=[bedrock.CfnPrompt.ContentBlockProperty(
                                text="Categorize: {{query}}\n\nThink step-by-step, then answer with ONE category.",
                            )],
                        ),
                    ],
                    input_variables=[
                        bedrock.CfnPrompt.PromptInputVariableProperty(name="query"),
                    ],
                ),
            ),
            model_id="anthropic.claude-sonnet-4-6",
            inference_configuration=...,
        ),
    ],
)

# Publish version 1
prompt_version = bedrock.CfnPromptVersion(self, "PromptV1",
    prompt_arn=classifier_prompt.attr_arn,
    description="Initial production version",
)
```

### 4.2 A/B testing via app code

```python
# Application picks variant based on feature flag / user cohort
import boto3, random
agent_runtime = boto3.client("bedrock-agent-runtime")

def classify(query, user_id):
    # 90% v1, 10% v2 (canary)
    variant = "v1-haiku" if random.random() < 0.9 else "v2-sonnet-cot"
    
    resp = agent_runtime.invoke_prompt(
        promptIdentifier=prompt_arn,
        promptVersion="1",
        promptVariantName=variant,
        templateInputVariables=[{"name": "query", "value": query}],
    )
    return resp["output"]["text"], variant
```

### 4.3 Prompt routing (classifier → downstream prompt)

In Bedrock Flows, build a routing classifier as the first prompt → use its output to route to specialized prompts (already shown in §3.1). This is the canonical "prompt as router" pattern.

---

## 5. Production Variant — full pipeline with monitoring

```python
# Add observability to flows
flow = bedrock.CfnFlow(self, "Flow",
    # ... base config ...
    # Logging — flow execution logs to CloudWatch
)

# Wrap invoke_flow in CloudWatch metrics
@metrics.log_metrics
@tracer.capture_lambda_handler
def invoke_with_metrics(event, context):
    user_id = event["user_id"]
    query = event["query"]
    
    start = time.time()
    resp = agent_runtime.invoke_flow(...)
    duration_ms = (time.time() - start) * 1000
    
    metrics.add_metric(name="FlowInvocationLatency", unit=MetricUnit.Milliseconds, value=duration_ms)
    metrics.add_metric(name="FlowInvocations", unit=MetricUnit.Count, value=1)
    
    # Track variant performance for A/B (if applicable)
    metrics.add_metadata(key="user_id", value=user_id)
    return resp
```

---

## 6. Common gotchas

- **Bedrock Flows max execution = 30 min** per invoke. For longer, split into smaller flows or use Step Functions + Bedrock.
- **Iterator node** — useful for "do X for each item" but can blow flow timeout if N is large. Cap iteration count.
- **Flow nodes don't support arbitrary state** — pass state via outputs/inputs. For complex state, use a Lambda node + DDB.
- **Conditional routing requires CONDITION node + Conditional connections** — not "if/else" inline.
- **Prompt Management variants must use same input variables** — different variables across variants requires different prompts.
- **Prompt versions are immutable** — once published, can't edit. New version = new publish.
- **`invoke_prompt` API directly or via flow node** — flow node automatically uses default variant; SDK call lets you specify.
- **Flow logs are sparse** — for deep tracing, add Lambda node that logs intermediate state to S3.
- **Iterator with KB lookups** — may exceed KB rate limits; throttle.
- **Cost** — each prompt node = InvokeModel call. 5-node flow with iterator over 100 items = 500+ InvokeModel calls. Monitor.
- **Flow visual designer** in Bedrock console exports JSON — check into git.
- **Cold start latency**: first invoke after long idle ~5-10s. Subsequent < 1s.
- **A/B testing needs analytics backend** — CW Metrics + custom dashboard tracks variant performance over time.
- **Feature flag + variant routing** — better than random; gives controlled rollout. Use AWS AppConfig.

---

## 7. Pytest worked example

```python
# tests/test_bedrock_flow.py
import boto3, pytest

agent = boto3.client("bedrock-agent")
agent_runtime = boto3.client("bedrock-agent-runtime")


def test_flow_prepared(flow_id):
    flow = agent.get_flow(flowIdentifier=flow_id)
    assert flow["status"] == "Prepared"


def test_flow_alias_points_to_version(flow_id, alias_id):
    alias = agent.get_flow_alias(aliasIdentifier=alias_id, flowIdentifier=flow_id)
    rc = alias["routingConfiguration"][0]
    assert rc["flowVersion"] == "1"


def test_invoke_flow_classifies_correctly(flow_arn, alias_id):
    resp = agent_runtime.invoke_flow(
        flowIdentifier=flow_arn,
        flowAliasIdentifier=alias_id,
        inputs=[{
            "content": {"document": "How do I reset my Postgres replication?"},
            "nodeName": "FlowInput",
            "nodeOutputName": "document",
        }],
    )
    output_chunks = []
    for event in resp["responseStream"]:
        if "flowOutputEvent" in event:
            output_chunks.append(event["flowOutputEvent"]["content"]["document"])
    full_output = "\n".join(output_chunks)
    # Should have routed to TechnicalExpert (covers DB topic)
    assert "Postgres" in full_output or "replication" in full_output


def test_prompt_variants_exist(prompt_arn):
    p = agent.get_prompt(promptIdentifier=prompt_arn)
    variants = p["variants"]
    assert len(variants) >= 2
    assert any(v["name"] == "v1-haiku" for v in variants)
    assert any(v["name"] == "v2-sonnet-cot" for v in variants)


def test_prompt_version_immutable(prompt_arn):
    """Can't edit published version — new edit = new version."""
    versions = agent.list_prompt_versions(promptIdentifier=prompt_arn)["promptVersionSummaries"]
    assert versions
    # Verify each version has a unique createdAt
    timestamps = [v["createdAt"] for v in versions]
    assert len(set(timestamps)) == len(timestamps)
```

---

## 8. Five non-negotiables

1. **Prompts versioned in Prompt Management** — never inline prompts in app code for prod.
2. **Flow + Prompt KMS encryption** — `customer_encryption_key_arn` on every resource.
3. **Flow alias for prod invokes** — never invoke `DRAFT` flow.
4. **Prompt variants for A/B testing** — track variant performance via CW Metrics dimension.
5. **Flow timeout consideration** — hard cap 30 min; split or use Step Functions for longer.

---

## 9. References

- [Bedrock Flows User Guide](https://docs.aws.amazon.com/bedrock/latest/userguide/flows.html)
- [Flow node types](https://docs.aws.amazon.com/bedrock/latest/userguide/flows-nodes.html)
- [Prompt Management](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management.html)
- [Prompt variants](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-variants.html)
- [InvokeFlow API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_InvokeFlow.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Bedrock Flows visual orchestration + Prompt Management API + variants + A/B + prompt routing + flow versioning. Wave 15. |
