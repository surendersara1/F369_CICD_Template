# SOP — AWS Managed MCP Connectivity Protocol (Reference Text for Agent System Prompts)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** Strands / AgentCore agent system prompts · AgentCore Gateway Discovery · Managed MCP targets (Redshift, Bedrock Knowledge Bases, SAP/Oracle OpenAPI)

---

## 1. Purpose

- Provide the canonical **protocol text** that agent system prompts must include when the deployed agent talks to an AWS-managed MCP Gateway (AgentCore Gateway + registered targets).
- Codify the gateway discovery handshake: every session begins with `list_tools` against the Discovery URL.
- Codify target priority: Redshift/Cortex Analyst for historical analytical queries, Bedrock KBs for RAG with citations, SAP/Oracle as the "System of Reality" for ERP truth.
- Codify stateful-MCP usage for multi-step intermediate result sets.
- Serve as the single source-of-truth snippet referenced by `{{ managed_mcp_protocol }}` template placeholders in agent prompt templates.

Include when the SOW deploys a Strands / AgentCore agent that consumes AWS-managed MCP services (Gateway + Redshift MCP server + Bedrock KB MCP server + ERP OpenAPI targets).

---

## 2. Decision — Monolith vs Micro-Stack

This SOP has no architectural split — it is a reference / protocol doc consumed by agent system prompts, not a CDK construct set. §3 is the single canonical variant.

§4 Micro-Stack Variant is intentionally omitted. The five non-negotiables from `LAYER_BACKEND_LAMBDA §4.1` apply to the **infrastructure** SOPs that provision the managed MCP surface (`AGENTCORE_GATEWAY`, `STRANDS_MCP_SERVER`, `STRANDS_MCP_TOOLS`), not to this protocol text.

---

## 3. Canonical Variant

### 3.1 Connectivity Protocol

The following text is the literal snippet that must appear in agent system prompts or knowledge files. Do not rewrite, paraphrase, or re-order — agents parse it deterministically during session bootstrap. Render the `{{ gateway_discovery_url }}` placeholder at build time from `docs/template_params.md::GATEWAY_DISCOVERY_URL`.

```markdown
### AWS MANAGED MCP CONNECTIVITY PROTOCOL ###

## GATEWAY DISCOVERY
- You MUST interact with the 'AgentCore Gateway' via the Discovery URL: {{ gateway_discovery_url }}.
- At session start, perform a `list_tools` call to synchronize the available skills from the registered targets (Redshift, Knowledge Bases, ERPs).

## MANAGED SKILL EXECUTION
- [REDSHIFT]: Use the 'redshift-mcp-server' target for historical/analytical queries. Prefer the 'Cortex Analyst' tool within this server for natural-language-to-SQL tasks.
- [RAG]: Use 'bedrock-kb-mcp-server' for semantic retrieval. Always provide citations for data sourced from OpenSearch.
- [ERP]: Access SAP/Oracle via the 'AgentCore Gateway' OpenAPI targets. Treat these as the "System of Reality."

## STATEFUL INTERACTIONS
- If a tool requires multiple steps (e.g., complex multi-table joins), you MUST use 'Stateful MCP' features to maintain the intermediate result set within the AgentCore Runtime.
```

### 3.2 How agents consume this

This text is not executed — it is **included verbatim** in agent system prompts or agent knowledge files. The inclusion mechanism depends on the agent deployment:

| Agent deployment surface | Inclusion mechanism |
|---|---|
| Strands agent in `STRANDS_DEPLOY_LAMBDA` | Loaded at container build time into `system_prompt` kwarg on `Agent(...)` via a Jinja render of `prompts/system.md.j2` that includes `{% include "partials/aws_managed_mcp.md" %}` |
| Strands agent in `STRANDS_DEPLOY_ECS` / Fargate | Same Jinja pattern; the container image baked with the rendered prompt in `/app/prompts/system.md` |
| AgentCore Runtime agent (`AGENTCORE_RUNTIME`) | Published as an AgentCore Memory "long-term" knowledge entry keyed `managed_mcp_protocol_v1` and retrieved via `retrieve_memory_records()` at session start |
| Agent authored in an SSM Parameter Store prompt (`LLMOPS_BEDROCK` pattern) | Concatenated to the prompt value at `ssm put-parameter` time; agents read the full value via `ssm:GetParameter` |

Whichever mechanism ships the text, the agent reads it as part of its system-prompt context. The protocol then *directs* how the agent's planner chooses managed MCP targets at runtime.

**Contract.** Do not mutate the three headed sections (`GATEWAY DISCOVERY`, `MANAGED SKILL EXECUTION`, `STATEFUL INTERACTIONS`) without bumping the SOP version — downstream evaluators (`STRANDS_EVAL`) assert these headings verbatim when scoring protocol adherence. Adding new `[TAG]:` bullets under `MANAGED SKILL EXECUTION` is allowed; removing them is a breaking change.

### 3.3 Gotchas

- **Gateway discovery ordering.** `list_tools` at session start is non-negotiable. If an agent attempts to invoke a managed target before `list_tools` returns, the Gateway rejects the call with `DiscoveryIncomplete` — which surfaces to the LLM as an opaque tool error. Bake the `list_tools` call into session-bootstrap code (not the LLM's responsibility), and only expose tools to the LLM *after* discovery has resolved.
- **`list_tools` timing.** Gateway discovery is eventually-consistent: newly-registered targets may not appear in the first `list_tools` response for up to 30 s after target creation. Cache the tool list per session (not per agent lifetime) and re-invoke `list_tools` if a tool returns `ToolNotFound` mid-session — `# TODO(verify): AgentCore Gateway list_tools staleness window in the managed control plane`.
- **Stateful-MCP session lifetime.** The Gateway holds intermediate result sets in the AgentCore Runtime session, bounded by the runtime's session TTL (8 h default — see `AGENTCORE_RUNTIME`). Multi-hour analyst workflows that rely on stateful joins must either refresh the session before TTL or snapshot the intermediate set to S3/DynamoDB.
- **Target priority.** The protocol's `[REDSHIFT] / [RAG] / [ERP]` ordering is a strong hint, not a hard rule — the LLM will still sometimes attempt to answer an ERP question from a Bedrock KB. Supervisor agents (`STRANDS_MULTI_AGENT`) should gate ERP queries behind an explicit routing step that calls the SAP/Oracle OpenAPI target before any RAG call.
- **RAG citation requirement.** The `Always provide citations for data sourced from OpenSearch` line is enforced at evaluation time (`STRANDS_EVAL`), not at runtime. A Bedrock KB MCP response without `citations[]` populated still returns 200 from the Gateway, but will fail online evaluation. Wire a post-response validator that rejects uncited RAG content before the agent's synthesis step.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| Agent also talks to a self-hosted MCP server (not AWS-managed) | Include `STRANDS_MCP_SERVER` protocol text as a **second** appended block; do not merge with this one |
| Redshift swapped for Athena/Glue | Replace `[REDSHIFT]:` bullet with `[ATHENA]:` + appropriate tool name; bump to v2.1 |
| Bedrock KBs replaced by vector-DB backed RAG (OpenSearch direct) | Replace `[RAG]:` bullet; citation requirement remains verbatim |
| ERP target migrates from SAP to Workday | Update `[ERP]:` wording to name the new "System of Reality"; priority rule stays |
| Multi-agent (A2A) flow | Include this SOP in the supervisor's prompt only; sub-agents receive a trimmed variant without the ERP bullet |
| Agent runs without AgentCore Gateway | Delete the `## GATEWAY DISCOVERY` block and remove stateful-MCP section; switch to self-hosted MCP per `STRANDS_MCP_TOOLS` |

---

## 6. Worked example

Offline verification that the protocol text renders with the required headings and bullets. Save as `tests/sop/test_aws_managed_mcp.py`.

```python
"""SOP verification — protocol text is well-formed for agent-prompt inclusion."""
import re
from pathlib import Path

PARTIAL = Path(__file__).resolve().parents[2] / "prompt_templates" / "partials" / "aws_managed_mcp.md"

REQUIRED_SECTIONS = [
    "## GATEWAY DISCOVERY",
    "## MANAGED SKILL EXECUTION",
    "## STATEFUL INTERACTIONS",
]

REQUIRED_BULLETS = [
    "You MUST interact with the 'AgentCore Gateway' via the Discovery URL: {{ gateway_discovery_url }}.",
    "At session start, perform a `list_tools` call",
    "[REDSHIFT]:",
    "[RAG]:",
    "[ERP]:",
    "Always provide citations for data sourced from OpenSearch.",
    "Treat these as the \"System of Reality.\"",
    "you MUST use 'Stateful MCP' features",
]


def _protocol_block() -> str:
    body = PARTIAL.read_text(encoding="utf-8")
    # Extract the fenced-code block under §3.1 Connectivity Protocol
    m = re.search(
        r"### 3\.1 Connectivity Protocol.*?```markdown\s*(.*?)```",
        body,
        re.DOTALL,
    )
    assert m, "§3.1 Connectivity Protocol markdown block not found"
    return m.group(1)


def test_three_required_sections_present():
    text = _protocol_block()
    for section in REQUIRED_SECTIONS:
        assert section in text, f"Missing required section: {section}"


def test_required_bullets_present():
    text = _protocol_block()
    for bullet in REQUIRED_BULLETS:
        assert bullet in text, f"Missing required bullet fragment: {bullet!r}"


def test_gateway_placeholder_preserved():
    text = _protocol_block()
    assert "{{ gateway_discovery_url }}" in text, \
        "Placeholder must remain un-rendered in the source partial"


def test_three_sections_in_order():
    text = _protocol_block()
    positions = [text.index(s) for s in REQUIRED_SECTIONS]
    assert positions == sorted(positions), \
        "Section order drift — consumers depend on GATEWAY → SKILLS → STATEFUL"
```

---

## 7. References

- `docs/template_params.md` — `GATEWAY_DISCOVERY_URL`, `MANAGED_MCP_PROTOCOL_SSM` (if published via SSM for agents to fetch dynamically), `REDSHIFT_MCP_TARGET_NAME`, `BEDROCK_KB_MCP_TARGET_NAME`, `ERP_OPENAPI_TARGET_NAME`
- `docs/Feature_Roadmap.md` — feature IDs `MCP-01..MCP-06` (managed MCP protocol inclusion), `MCP-07..MCP-10` (ERP OpenAPI targets)
- AgentCore Gateway: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-gateway.html
- Bedrock Knowledge Bases: https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html
- Redshift MCP / Cortex Analyst target patterns (internal): see `AGENTCORE_GATEWAY` §3.3
- Related SOPs: `AGENTCORE_GATEWAY` (Gateway provisioning), `STRANDS_MCP_SERVER` (self-hosted MCP server counterpart), `STRANDS_MCP_TOOLS` (client-side tool consumption), `AGENTCORE_RUNTIME` (session / TTL behaviour), `STRANDS_EVAL` (citation + heading adherence evaluators), `LLMOPS_BEDROCK` (SSM prompt distribution)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section single-variant SOP (§4 Micro-Stack intentionally omitted — this is protocol text, not a CDK stack). Preserved the exact 12-line v1.0 protocol text verbatim in §3.1 as the canonical snippet (downstream evaluators assert the three section headings verbatim). Added §3.2 "How agents consume this" documenting the inclusion mechanisms for Strands Lambda / ECS, AgentCore Runtime, and SSM-distributed prompts. Added §3.3 Gotchas covering Gateway discovery ordering, `list_tools` staleness, stateful-MCP session lifetime, target priority, RAG citation enforcement. Added Swap matrix (§5) for Redshift / RAG / ERP target substitutions. Added Worked example (§6) — pytest harness that asserts the three required section headings, required bullets, section ordering, and that the `{{ gateway_discovery_url }}` placeholder remains un-rendered. |
| 1.0 | 2026-03-05 | Initial — 12-line AWS Managed MCP Connectivity Protocol text for agent system prompts (GATEWAY DISCOVERY + MANAGED SKILL EXECUTION + STATEFUL INTERACTIONS). |
