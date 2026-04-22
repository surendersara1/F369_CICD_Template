# SOP — Strands Model Providers (Fallback, Guardrails, Multi-Provider)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · Amazon Bedrock (default) · Ollama / OpenAI / Anthropic / Gemini / SageMaker / Nova (alternative providers) · Python 3.12+

---

## 1. Purpose

- Codify complexity-based model routing — simple queries → Haiku (~10× cheaper), complex → Sonnet, high-risk → full Sonnet + approval chain.
- Attach a Bedrock Guardrail to a `BedrockModel` cleanly, with trace enabled for audit.
- Document the supported `strands.models.*` provider matrix and the two-line swap pattern for local dev (Ollama), OpenAI, Anthropic direct, Gemini, and SageMaker endpoints.
- Keep all model selection in one place: a single `build_model_with_fallback()` callable consumed by every agent.
- Include when the SOW mentions cost optimization, model fallback, Haiku↔Sonnet routing, guardrail integration, or multi-provider strategies.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** Model providers are client-side Python classes; nothing is provisioned. §3 is the single canonical variant.
>
> The Bedrock Guardrail resource itself (`bedrock.CfnGuardrail`) is defined in `LLMOPS_BEDROCK`. This SOP only wires its ID into `BedrockModel`.

§4 Micro-Stack Variant is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Routing policy

```
Production uses intelligent model routing:
  Simple queries    (KPI lookups, data retrieval)          → Haiku  (≈10× cheaper)
  Complex queries   (RCA, forecasting, simulations)        → Sonnet (higher quality)
  Approval queries  (POs, compliance, authorization)       → Sonnet (full chain + HITL)

Classification is keyword-based (no LLM call, no cost):
  SIMPLE   : "what is", "show me", "current", "latest", "how much", "list", "get", "total", "display"
  COMPLEX  : "why", "root cause", "analyze", "compare", "recommend", "forecast", "simulate",
             "variance", "decompose", "trend", "impact", "risk", "scenario"
  APPROVAL : "approve", "authorize", "purchase order", "PO", "payment",
             "sign off", "budget approval", "compliance check", "vendor onboard"
```

### 3.2 Complexity classifier + fallback builder

```python
"""Model selection: simple → Haiku, complex → Sonnet, approval → Sonnet+HITL."""
import re
from strands.models import BedrockModel

SIMPLE   = re.compile(r'\b(what is|show me|current|latest|how much|list|get|total|display)\b', re.I)
COMPLEX  = re.compile(r'\b(why|root cause|analyze|compare|recommend|forecast|simulate|variance|decompose|trend|impact|risk|scenario)\b', re.I)
APPROVAL = re.compile(r'\b(approve|authorize|purchase order|PO\b|payment|sign off|budget approval|compliance check|vendor onboard)\b', re.I)


def classify_query_complexity(query: str) -> str:
    if len(APPROVAL.findall(query)) >= 1:
        return 'approval'
    complex_n = len(COMPLEX.findall(query))
    simple_n  = len(SIMPLE.findall(query))
    if complex_n >= 2:
        return 'complex'
    if simple_n >= 1 and complex_n == 0:
        return 'simple'
    return 'complex' if len(query) > 100 else 'simple'


def build_model_with_fallback(
    query: str,
    primary_id: str,
    fallback_id: str,
    guardrail_id: str = '',
    guardrail_version: str = 'DRAFT',
):
    """Return (model, model_id, complexity). Callers pass complexity to telemetry."""
    complexity = classify_query_complexity(query)
    # simple → fallback (Haiku); complex & approval → primary (Sonnet)
    model_id = fallback_id if complexity == 'simple' else primary_id
    model = build_bedrock_model(model_id, guardrail_id, guardrail_version)
    return model, model_id, complexity
```

### 3.3 Guardrail integration

```python
"""Attach a Bedrock Guardrail to a Strands BedrockModel."""
from strands.models import BedrockModel


def build_bedrock_model(
    model_id: str,
    guardrail_id: str = '',
    guardrail_version: str = 'DRAFT',
) -> BedrockModel:
    if guardrail_id:
        return BedrockModel(
            model_id=model_id,
            additional_request_fields={
                'guardrailConfig': {
                    'guardrailIdentifier': guardrail_id,
                    'guardrailVersion':    guardrail_version,
                    'trace':               'enabled',  # required for audit trail
                }
            },
        )
    return BedrockModel(model_id=model_id)
```

### 3.4 Supported providers

| Provider | Python | TS | Import |
|---|---|---|---|
| Amazon Bedrock       | ✅ | ✅ | `from strands.models.bedrock import BedrockModel` |
| Amazon Nova          | ✅ | ❌ | `from strands.models.nova import NovaModel` |
| Anthropic (direct)   | ✅ | ❌ | `from strands.models.anthropic import AnthropicModel` |
| OpenAI               | ✅ | ✅ | `from strands.models.openai import OpenAIModel` |
| Google Gemini        | ✅ | ✅ | `from strands.models.google import GoogleModel` |
| Ollama (local)       | ✅ | ❌ | `from strands.models.ollama import OllamaModel` |
| SageMaker endpoint   | ✅ | ❌ | `from strands.models.sagemaker import SageMakerModel` |
| Custom               | ✅ | ✅ | Implement the `Model` interface |

### 3.5 Multi-provider swap

```python
import os
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.models.openai import OpenAIModel

# Bedrock (default — uses IAM role, no API key)
bedrock = BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0")

# OpenAI (needs API key in env or Secrets Manager)
openai = OpenAIModel(
    client_args={"api_key": os.environ["OPENAI_API_KEY"]},
    model_id="gpt-4o",
)

# Models are interchangeable — just swap the `model=` arg
agent = Agent(model=bedrock, system_prompt="...", tools=[...])
```

### 3.6 Gotchas

- **`guardrailConfig` is silently ignored** if nested under the wrong key. It MUST sit at the top level of `additional_request_fields`, not under `model_kwargs` or `body`. Verify with a CloudTrail `bedrock:InvokeModel` event — the request payload should show `guardrailIdentifier`.
- **`APPROVAL` queries use the primary (Sonnet) model but also require HITL.** Returning only Sonnet is not enough — gate the call behind an SFN approval task; see `WORKFLOW_STEP_FUNCTIONS §4`.
- **Cross-region inference profiles** (e.g. `us.anthropic.claude-sonnet-…`) require the correct `model_id` prefix. A regional model ID in a cross-region profile context silently routes to only one region and caps throughput.
- **OpenAI / Anthropic direct / Gemini keys** belong in Secrets Manager, not env vars in the task definition. Load via boto3 `secretsmanager.get_secret_value` at startup and set `os.environ["OPENAI_API_KEY"]` in-process only.
- **`OllamaModel`** in dev: point at `http://host.docker.internal:11434` from an ECS task on macOS; on Linux use the gateway IP. No credentials.
- **Guardrail `trace='enabled'`** adds ~20 ms per invocation. Acceptable everywhere in prod; leave it on.
- **Complexity classifier is deliberately naive** — it's a cost heuristic, not a semantic gate. Don't use it to decide security policy (e.g. "approvals need Sonnet" is cost-shaping, not authorization). Authorization lives in `AfieSteeringHooks` (see `STRANDS_HOOKS_PLUGINS`).

---

## 5. Swap matrix — provider / routing variants

| Need | Swap |
|---|---|
| Run locally without Bedrock | `BedrockModel(...)` → `OllamaModel(host='http://localhost:11434', model_id='llama3.2')` |
| Use OpenAI in prod          | `OpenAIModel(client_args={'api_key': ...}, model_id='gpt-4o')` + put the key in Secrets Manager |
| Force all traffic to primary (disable fallback) | In `build_model_with_fallback`, return `primary_id` unconditionally |
| Disable guardrails for debugging | Pass `guardrail_id=''` — `build_bedrock_model` skips the `additional_request_fields` block |
| Pin to a regional model (no cross-region inference) | Use the regional ID directly, e.g. `anthropic.claude-haiku-4-5-20251001-v1:0` (no `us.` prefix) |
| Swap entire provider at runtime | `model` is just a constructor arg; `Agent(model=...)` — no other code changes |

---

## 6. Worked example — classifier + guardrail wiring

Save as `tests/sop/test_STRANDS_MODEL_PROVIDERS.py`. Offline.

```python
"""SOP verification — classifier cases + guardrail request shape."""
from shared.model_builder import (
    classify_query_complexity,
    build_bedrock_model,
)


def test_classifier_buckets():
    assert classify_query_complexity("what is current cash?")           == 'simple'
    assert classify_query_complexity("why did margin drop, compare Q2") == 'complex'
    assert classify_query_complexity("approve the vendor onboard PO")   == 'approval'


def test_guardrail_block_shape():
    model = build_bedrock_model(
        model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
        guardrail_id="gr-abc123",
        guardrail_version="2",
    )
    # Strands BedrockModel stores request mutations under a known attr;
    # the shape must include a top-level `guardrailConfig`.
    fields = model.additional_request_fields  # type: ignore[attr-defined]
    assert 'guardrailConfig' in fields
    assert fields['guardrailConfig']['guardrailIdentifier'] == 'gr-abc123'
    assert fields['guardrailConfig']['trace'] == 'enabled'


def test_no_guardrail_means_no_additional_fields():
    model = build_bedrock_model(model_id="anthropic.claude-haiku-4-5-20251001-v1:0")
    fields = getattr(model, 'additional_request_fields', None)
    assert not fields  # empty dict or None is fine
```

---

## 7. References

- `docs/template_params.md` — `MODEL_ID` (primary/Sonnet), `FALLBACK_MODEL_ID` (Haiku), `GUARDRAIL_ID`, `GUARDRAIL_VERSION`
- `docs/Feature_Roadmap.md` — feature IDs `A-15..A-22` (Bedrock model selection, guardrails), `STR-03` (multi-provider)
- Bedrock Guardrails: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html
- Strands model providers: https://strandsagents.com/latest/user-guide/concepts/model-providers/
- Related SOPs: `STRANDS_AGENT_CORE` (how `build_model_with_fallback` plugs into the agent), `LLMOPS_BEDROCK` (the `CfnGuardrail` resource + permissions), `STRANDS_HOOKS_PLUGINS` (authorization — not cost — gating)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added Gotchas (§3.6) covering guardrail misplacement, cross-region inference, and classifier misuse. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — classifier, fallback builder, guardrail wiring, supported-provider matrix, multi-provider example. |
