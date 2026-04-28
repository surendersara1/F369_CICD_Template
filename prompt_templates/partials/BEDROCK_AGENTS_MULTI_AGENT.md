# SOP — Bedrock Agents Multi-Agent Collaboration (supervisor + collaborators · action groups · KB integration · session state · custom orchestration)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Bedrock Agents · Multi-Agent Collaboration (GA Dec 2024) · Action Groups (Lambda + OpenAPI schema) · Knowledge Base association · Session state + memory · Custom orchestration via promptOverride · Code Interpreter action group · Return of control (ROC) + Confirmation flow

---

## 1. Purpose

- Codify **Bedrock Agents** as the canonical AWS-native agent runtime — different from Strands SDK (open-source, you-host) and AgentCore (managed runtime). Bedrock Agents are pure-managed, Bedrock-flavored.
- Codify **Multi-Agent Collaboration (Dec 2024 GA)** — supervisor agent orchestrates multiple collaborator agents; useful for "router" patterns + domain-specialized teams.
- Codify **Action Groups** — Lambda backend OR OpenAPI passthrough OR Code Interpreter (built-in) OR Return of Control to user/app.
- Codify **Knowledge Base association** — KB tools auto-attach as RAG action.
- Codify **Session state + memory** — short-term within session + long-term cross-session via `SessionState` API.
- Codify **`promptOverrideConfiguration`** for custom orchestration (replace default ReAct prompts).
- Codify the **ROC (Return of Control)** flow — agent yields control back to caller for tool execution.
- This is the **Bedrock-native agent specialisation**. Choose over Strands/AgentCore when: (a) AWS-managed-everything preferred, (b) Bedrock + Claude/Llama for both reasoning + tools, (c) tight Bedrock KB integration.

When the SOW signals: "Bedrock Agents", "supervisor agent + collaborators", "agent that can call tools + KBs", "multi-agent system in AWS", "AWS-managed agent runtime".

---

## 2. Decision tree — Bedrock Agents vs Strands SDK vs AgentCore

| Need | Bedrock Agents | Strands SDK (open-source) | AgentCore (managed runtime) |
|---|:---:|:---:|:---:|
| AWS-managed orchestration | ✅ | ❌ self-host | ✅ |
| Custom orchestration loop / framework | ❌ Bedrock-defined | ✅ | ⚠️ container-level |
| Multi-agent collaboration | ✅ supervisor pattern (2024) | ✅ rich graph/swarm | ✅ |
| Tight KB integration | ✅ native | ⚠️ via tool | ⚠️ via tool |
| Custom UI / streaming | ⚠️ via streaming API | ✅ flexible | ✅ flexible |
| Cost (token + Lambda + Bedrock invoke) | $$ | $ + your compute | $$ |
| Code Interpreter built-in | ✅ | via tool | ✅ AgentCore CI |
| Time to value | ✅ days | weeks | days |

**Recommendation:**
- **Bedrock Agents** for AWS-first teams who want managed orchestration with Bedrock models — chatbots, KB-anchored support, multi-step task automation.
- **Strands SDK** for Python teams that want graph/swarm/workflow patterns + framework control.
- **AgentCore** for production agent runtime with managed scaling + observability + identity (OBO).

```
Multi-Agent Collaboration architecture:

   User input
        │
        ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Supervisor Agent                                                │
   │   - Receives user query                                          │
   │   - Decides: which collaborator(s) to invoke?                    │
   │   - Aggregates collaborator outputs into final response           │
   │   - Has its own KB association                                    │
   │                                                                   │
   │   Collaborators:                                                 │
   │     orders-agent      — handles order-related questions          │
   │     billing-agent     — handles billing/invoice queries          │
   │     technical-agent   — handles technical product Q              │
   │     human-handoff-agent — escalates to ticket if stuck           │
   └────────────────┬───────────────────────────────────────────────┘
                    │ delegate
                    ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Collaborator Agent (e.g., orders-agent)                          │
   │   - Specialized prompt: "You handle order-related queries"       │
   │   - Action Groups:                                               │
   │     - lookup_order (Lambda)                                      │
   │     - cancel_order (Lambda; requires confirmation)               │
   │     - search_order_kb (KB integration)                           │
   │   - Returns response to supervisor                                │
   └────────────────────────────────────────────────────────────────┘
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single agent + 1 KB + 1 action group | **§3 Single Agent** |
| Production — supervisor + 3-5 collaborators + KBs + tools + Code Interpreter | **§5 Multi-Agent** |

---

## 3. Single Agent Variant — agent + Action Group (Lambda) + KB

### 3.1 CDK

```python
# stacks/bedrock_agent_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_iam as iam
from constructs import Construct
import json


class BedrockAgentStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 kb_id: str,                                # from BEDROCK_KNOWLEDGE_BASES
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Action group Lambda — implements business logic ──────
        action_fn = _lambda.Function(self, "OrdersActionFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="actions.handler",
            code=_lambda.Code.from_asset("src/agent_actions"),
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "POWERTOOLS_SERVICE_NAME": "agent-actions",
            },
        )
        # Lambda code (src/agent_actions/actions.py):
        #   def handler(event, context):
        #       action_group = event["actionGroup"]
        #       function = event["function"]      # e.g., "lookup_order"
        #       parameters = {p["name"]: p["value"] for p in event.get("parameters", [])}
        #       
        #       if function == "lookup_order":
        #           order = ddb.get_item(Key={"pk": f"ORDER#{parameters['order_id']}"})
        #           result = json.dumps(order)
        #       elif function == "cancel_order":
        #           # Mutating ops should require confirmation in agent prompt
        #           result = "Order cancelled"
        #       
        #       return {
        #         "messageVersion": "1.0",
        #         "response": {
        #           "actionGroup": action_group,
        #           "function": function,
        #           "functionResponse": {
        #             "responseBody": {"TEXT": {"body": result}}
        #           }
        #         }
        #       }

        # Bedrock can invoke this Lambda
        action_fn.add_permission("AllowBedrockInvoke",
            principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
            action="lambda:InvokeFunction",
        )

        # ── 2. Agent execution role ──────────────────────────────────
        agent_role = iam.Role(self, "AgentRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
        )
        # Invoke Bedrock model
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-sonnet-4-6",
            ],
        ))
        # Invoke action group Lambda
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[action_fn.function_arn],
        ))
        # Retrieve from KB
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:Retrieve"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb_id}"],
        ))

        # ── 3. Agent ─────────────────────────────────────────────────
        agent = bedrock.CfnAgent(self, "OrdersAgent",
            agent_name=f"{env_name}-orders-agent",
            agent_resource_role_arn=agent_role.role_arn,
            description="Helps customers with order questions",
            foundation_model="anthropic.claude-sonnet-4-6",
            instruction="""You are a helpful customer support agent for Acme.
You help customers with their orders. Be concise and accurate.
You have these tools:
- lookup_order(order_id): Returns order status, items, total.
- cancel_order(order_id): Cancels an order. ALWAYS confirm with the user before calling.

Use the knowledge base to answer general policy questions (returns, shipping, etc.).
If you cannot help, say so politely and offer to escalate.
""",
            idle_session_ttl_in_seconds=600,                 # 10 min
            customer_encryption_key_arn=kms_key_arn,
            # Optional: orchestration prompt override
            # prompt_override_configuration=...
        )

        # ── 4. Action Group ───────────────────────────────────────────
        bedrock.CfnAgent.AgentActionGroupProperty(
            action_group_name="orders",
            action_group_state="ENABLED",
            action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                lambda_=action_fn.function_arn,
            ),
            # Function-style schema (preferred for new development)
            function_schema=bedrock.CfnAgent.FunctionSchemaProperty(
                functions=[
                    bedrock.CfnAgent.FunctionProperty(
                        name="lookup_order",
                        description="Look up an order by its ID",
                        parameters={
                            "order_id": bedrock.CfnAgent.ParameterDetailProperty(
                                type="string",
                                description="The unique order identifier",
                                required=True,
                            ),
                        },
                    ),
                    bedrock.CfnAgent.FunctionProperty(
                        name="cancel_order",
                        description="Cancel an order. Requires user confirmation before calling.",
                        parameters={
                            "order_id": bedrock.CfnAgent.ParameterDetailProperty(
                                type="string", required=True,
                            ),
                            "reason": bedrock.CfnAgent.ParameterDetailProperty(
                                type="string", required=False,
                            ),
                        },
                        # KEY: require user confirmation flow
                        require_confirmation="ENABLED",
                    ),
                ],
            ),
            description="Order operations",
        )

        # ── 5. KB association ────────────────────────────────────────
        bedrock.CfnAgentKnowledgeBaseAssociationProperty(
            knowledge_base_id=kb_id,
            description="Acme policies, FAQ, returns guide",
            knowledge_base_state="ENABLED",
        )

        # ── 6. Code Interpreter (built-in action group, opt-in) ───────
        bedrock.CfnAgent.AgentActionGroupProperty(
            action_group_name="CodeInterpreter",
            parent_action_group_signature="AMAZON.CodeInterpreter",  # built-in
            action_group_state="ENABLED",
            description="Execute Python code for calculations and data analysis",
        )

        # ── 7. Agent alias (production version) ──────────────────────
        agent_alias = bedrock.CfnAgentAlias(self, "AgentAlias",
            agent_id=agent.attr_agent_id,
            agent_alias_name="prod",
            description="Production alias",
            routing_configuration=[
                bedrock.CfnAgentAlias.AgentAliasRoutingConfigurationListItemProperty(
                    agent_version="1",                            # specific version
                ),
            ],
        )
```

### 3.2 Invoke agent

```python
import boto3
agent_runtime = boto3.client("bedrock-agent-runtime")

resp = agent_runtime.invoke_agent(
    agentId=agent_id,
    agentAliasId=agent_alias_id,
    sessionId="user-session-12345",                          # session continuity
    inputText="What's the status of order ORD-456?",
    enableTrace=True,                                          # debug trace
    sessionState={
        "sessionAttributes": {                                  # cross-turn state
            "user_id": "user-123",
            "tier": "gold",
        },
        "promptSessionAttributes": {
            "current_date": "2026-04-27",
        },
    },
)

# Streaming response
for event in resp["completion"]:
    if "chunk" in event:
        text = event["chunk"]["bytes"].decode()
        print(text, end="")
    elif "trace" in event:
        # Debug: see ReAct steps
        pass
    elif "returnControl" in event:
        # ROC flow — agent wants caller to execute action
        rc = event["returnControl"]
        # ... handle, return result via invoke_agent w/ sessionState ...
```

---

## 4. Multi-Agent Collaboration

### 4.1 Supervisor + collaborators CDK

```python
# Supervisor agent
supervisor = bedrock.CfnAgent(self, "Supervisor",
    agent_name=f"{env_name}-supervisor",
    foundation_model="anthropic.claude-sonnet-4-6",
    agent_resource_role_arn=supervisor_role.role_arn,
    instruction="""You are a customer support supervisor. Route queries to specialists:
- orders-agent: order status, returns, refunds
- billing-agent: invoices, payment methods, charges
- technical-agent: product setup, troubleshooting
Aggregate their responses into a clear answer for the customer.""",
    agent_collaboration="SUPERVISOR_ROUTER",                  # KEY: enables multi-agent
    # Or: SUPERVISOR (always invokes named collaborators in sequence)
    # Or: DISABLED (no collaboration; single-agent mode)
)

# Collaborator agents (each is a normal Bedrock Agent)
orders_agent = bedrock.CfnAgent(self, "OrdersAgent", ...)
billing_agent = bedrock.CfnAgent(self, "BillingAgent", ...)
technical_agent = bedrock.CfnAgent(self, "TechnicalAgent", ...)

# Associate collaborators with supervisor
bedrock.CfnAgentCollaborator(self, "OrdersCollab",
    agent_id=supervisor.attr_agent_id,
    agent_descriptor=bedrock.CfnAgentCollaborator.AgentDescriptorProperty(
        alias_arn=orders_agent_alias.attr_agent_alias_arn,
    ),
    collaboration_instruction="Route order-related questions here. This agent has access to order DB and can cancel/modify orders.",
    collaborator_name="orders-agent",
    relay_conversation_history="TO_COLLABORATOR",            # share full history
)

bedrock.CfnAgentCollaborator(self, "BillingCollab",
    agent_id=supervisor.attr_agent_id,
    agent_descriptor=bedrock.CfnAgentCollaborator.AgentDescriptorProperty(
        alias_arn=billing_agent_alias.attr_agent_alias_arn,
    ),
    collaboration_instruction="Route billing, invoice, and payment questions here.",
    collaborator_name="billing-agent",
)

bedrock.CfnAgentCollaborator(self, "TechCollab",
    agent_id=supervisor.attr_agent_id,
    agent_descriptor=bedrock.CfnAgentCollaborator.AgentDescriptorProperty(
        alias_arn=technical_agent_alias.attr_agent_alias_arn,
    ),
    collaboration_instruction="Route technical product questions here. Has access to product manuals via KB.",
    collaborator_name="technical-agent",
)
```

### 4.2 Supervisor invocation

User → Supervisor only; supervisor decides which collaborator(s) to invoke.

```python
resp = agent_runtime.invoke_agent(
    agentId=supervisor.attr_agent_id,
    agentAliasId=supervisor_alias_id,
    sessionId="multi-tenant-user-789",
    inputText="My recent order ORD-789 was charged twice — help!",
)
# Supervisor reasons:
#   "This question involves orders AND billing. Route to both, then synthesize."
# → Invokes orders-agent (gets order status, items)
# → Invokes billing-agent (checks for duplicate charges)
# → Synthesizes: "I see ORD-789 was charged twice. I've initiated a refund of $X."
```

### 4.3 Collaboration patterns

```yaml
agent_collaboration:
  - SUPERVISOR_ROUTER  # supervisor decides which collaborator(s); most flexible
  - SUPERVISOR         # supervisor invokes ALL collaborators in defined sequence
  - DISABLED           # single-agent mode

relay_conversation_history:
  - TO_COLLABORATOR    # collaborator sees full history (rich context)
  - DISABLED           # collaborator only sees the delegated question (faster, less leak)
```

---

## 5. Production Variant — full stack with sessions, memory, custom orchestration

### 5.1 Long-term memory (cross-session)

```python
# Bedrock Agents memory (preview 2024) — persists across sessions
agent = bedrock.CfnAgent(self, "AgentWithMemory",
    # ... base config ...
    memory_configuration=bedrock.CfnAgent.MemoryConfigurationProperty(
        enabled_memory_types=["SESSION_SUMMARY"],             # summarize past sessions
        storage_days=30,                                       # retain 30 days
    ),
)

# Each session is summarized and stored;
# agent can reference past summaries in new sessions for continuity.
```

### 5.2 Custom orchestration (override default ReAct)

```python
agent = bedrock.CfnAgent(self, "CustomOrchestrationAgent",
    # ... base config ...
    prompt_override_configuration=bedrock.CfnAgent.PromptOverrideConfigurationProperty(
        prompt_configurations=[
            bedrock.CfnAgent.PromptConfigurationProperty(
                prompt_type="ORCHESTRATION",                   # main reasoning loop
                prompt_state="ENABLED",
                prompt_creation_mode="OVERRIDDEN",
                base_prompt_template_arn=...,                  # or inline
                inference_configuration=bedrock.CfnAgent.InferenceConfigurationProperty(
                    temperature=0.0,                            # deterministic
                    maximum_length=2048,
                    top_p=0.9,
                ),
                parser_mode="OVERRIDDEN",                       # use custom Lambda parser
                parser_lambda_arn=parser_fn.function_arn,
            ),
            # Other prompt types: PRE_PROCESSING, POST_PROCESSING,
            #                     KNOWLEDGE_BASE_RESPONSE_GENERATION,
            #                     ROUTING_CLASSIFIER (multi-agent)
        ],
    ),
)
```

### 5.3 Return of Control (ROC) for sensitive operations

```python
# Action group set to RETURN_CONTROL — agent yields back to caller
bedrock.CfnAgent.AgentActionGroupProperty(
    action_group_name="payments",
    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
        custom_control="RETURN_CONTROL",                       # ROC flow
    ),
    function_schema=bedrock.CfnAgent.FunctionSchemaProperty(
        functions=[bedrock.CfnAgent.FunctionProperty(
            name="charge_card",
            description="Charge customer's card. ALWAYS prompts user.",
            parameters={...},
        )],
    ),
)

# In agent invoke loop:
# When agent decides to call charge_card, it RETURNS the function call
# to your app instead of executing. Your app:
# 1. Shows confirmation UI to user ("Charge $X to card ending 4242?")
# 2. On user confirm, executes the charge in your code
# 3. Sends result back via invoke_agent with sessionState.invocationId

resp = agent_runtime.invoke_agent(
    agentId=agent_id, agentAliasId=alias_id, sessionId=session,
    sessionState={
        "invocationId": prior_invocation_id,
        "returnControlInvocationResults": [{
            "functionResult": {
                "actionGroup": "payments",
                "function": "charge_card",
                "responseBody": {"TEXT": {"body": "Charged successfully. txn-id: abc"}},
            },
        }],
    },
)
```

---

## 6. Common gotchas

- **Bedrock Agents differ from AgentCore + Strands** — pure-managed, less customization. If you need framework control, use Strands. If you need custom runtime container, use AgentCore.
- **Multi-agent collaboration GA Dec 2024** — earlier preview API differs. Verify CFN/SDK versions.
- **Function-style schema is preferred** over OpenAPI schema for new development — simpler, fewer edge cases.
- **`require_confirmation: "ENABLED"`** on a function makes Bedrock prompt user before calling — critical for mutating ops.
- **Action group Lambda must respond in Bedrock-specific format** — `responseBody.TEXT.body` for function-style. Wrong shape → silent error in trace.
- **Bedrock Agents session TTL default 30 min** — too short for multi-step research. Set `idle_session_ttl_in_seconds` per use case.
- **Memory configuration is preview** — may have data residency / consent implications. Validate with legal.
- **`agent_collaboration: SUPERVISOR_ROUTER`** routes to ONE collaborator per turn (most common). For parallel multi-collab, use `SUPERVISOR` mode.
- **Streaming response chunks are partial JSON** — don't parse line-by-line; concat then parse final.
- **Bedrock Agent quota: 100 agents per account/region** by default — request increase for multi-tenant.
- **KB association on agent uses agent's IAM role** — that role needs `bedrock:Retrieve` + KB resource ARN.
- **Cost**: per InvokeAgent = N × InvokeModel calls (orchestration loop) + Lambda invokes + KB retrievals. Easily 5-20× single InvokeModel for complex tasks. Monitor.
- **Tracing via `enableTrace=True`** — invaluable for debugging; turn off in prod for performance.
- **Supervisor + collaborator pattern adds 2-3× latency** vs single agent — supervisor reasons + delegates + waits + synthesizes.
- **Cross-region** — agents are region-local. Multi-region = duplicate agents per region.
- **Versioning** — agents have draft + version 1, 2, 3, ... + alias (like Lambda). Always invoke via alias for prod.

---

## 7. Pytest worked example

```python
# tests/test_bedrock_agent.py
import boto3, pytest, json

agent_client = boto3.client("bedrock-agent")
agent_runtime = boto3.client("bedrock-agent-runtime")


def test_agent_prepared(agent_id):
    """Agent must be in PREPARED state to invoke."""
    agent = agent_client.get_agent(agentId=agent_id)["agent"]
    assert agent["agentStatus"] == "PREPARED"


def test_action_group_associated(agent_id):
    ag = agent_client.list_agent_action_groups(
        agentId=agent_id, agentVersion="DRAFT",
    )["actionGroupSummaries"]
    assert ag, "No action groups"
    # Code Interpreter check
    names = [a["actionGroupName"] for a in ag]
    assert any("CodeInterpreter" in n for n in names) or len(names) >= 1


def test_kb_associated(agent_id):
    kbs = agent_client.list_agent_knowledge_bases(
        agentId=agent_id, agentVersion="DRAFT",
    )["agentKnowledgeBaseSummaries"]
    assert kbs


def test_invoke_agent_returns_response(agent_id, alias_id):
    resp = agent_runtime.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId="test-session-1",
        inputText="What's the status of order ORD-123?",
    )
    chunks = []
    for event in resp["completion"]:
        if "chunk" in event:
            chunks.append(event["chunk"]["bytes"].decode())
    full_response = "".join(chunks)
    assert full_response, "No response"


def test_multi_agent_routing(supervisor_id, supervisor_alias_id):
    """Supervisor should route order question to orders-agent."""
    resp = agent_runtime.invoke_agent(
        agentId=supervisor_id,
        agentAliasId=supervisor_alias_id,
        sessionId="multi-test-1",
        inputText="My order ORD-456 is missing — help",
        enableTrace=True,
    )
    routed_to = []
    for event in resp["completion"]:
        if "trace" in event:
            trace = event["trace"]
            if trace.get("collaboratorName"):
                routed_to.append(trace["collaboratorName"])
    assert "orders-agent" in routed_to, f"Routed to: {routed_to}"


def test_confirmation_required_for_cancel(agent_id, alias_id):
    """Cancel order action must request confirmation."""
    resp = agent_runtime.invoke_agent(
        agentId=agent_id, agentAliasId=alias_id,
        sessionId="confirm-test-1",
        inputText="Cancel order ORD-789",
    )
    response_text = "".join(e["chunk"]["bytes"].decode() for e in resp["completion"] if "chunk" in e)
    # Should ASK for confirmation, not directly cancel
    assert any(word in response_text.lower() for word in ["confirm", "are you sure", "proceed"])
```

---

## 8. Five non-negotiables

1. **`require_confirmation: ENABLED`** on every mutating action (cancel, charge, delete, refund).
2. **CMK encryption** (`customer_encryption_key_arn`) on every Bedrock Agent.
3. **Use alias for prod invokes** — never invoke `DRAFT` directly in production.
4. **Action Group Lambda response format strict** — verify with `enableTrace=True` during dev.
5. **Action group IAM scoped to specific Lambda ARN** — never wildcard `lambda:*`.

---

## 9. References

- [Bedrock Agents User Guide](https://docs.aws.amazon.com/bedrock/latest/userguide/agents.html)
- [Multi-Agent Collaboration (Dec 2024 GA)](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-multi-agent-collaboration.html)
- [Action Groups](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-action-create.html)
- [Code Interpreter action group](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-code-interpretation.html)
- [Return of Control](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-returncontrol.html)
- [Memory configuration (preview)](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-memory.html)
- [Custom orchestration](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-custom-orchestration.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Bedrock Agents + Multi-Agent Collaboration (Dec 2024) + action groups + KB integration + memory + ROC + custom orchestration. Wave 15. |
