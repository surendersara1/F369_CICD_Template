# SOP — Bedrock Model Lifecycle (Active / Legacy / EOL) Awareness

**Version:** 1.0 · **Last-reviewed:** 2026-06-17 · **Status:** Active (NEW — R4 / F-AFIE-25)
**Applies to:** Every project that calls `bedrock-runtime:InvokeModel*` · SSM-driven model routing (LLMOPS_BEDROCK §3.0b) · Pre-deploy + monthly + post-deploy verification · Cross-region inference profile selection (LLMOPS_BEDROCK §3.1) · Active/Legacy/EOL transition incident playbook
**Purpose:** Carve out Bedrock-model-lifecycle awareness as its own dedicated partial that consumer kits + composites + agents `[[LLMOPS_BEDROCK_MODEL_LIFECYCLE]]` for the canonical patterns. Closes R4 root cause Class A's most volatile sub-case: Bedrock model state moves monthly, but kits weren't checking it before deploy.

---

## 1. Purpose

**The R4 F-AFIE-02 finding showed the structural gap:** the canonical Bedrock model lifecycle awareness lived as §3.0 inside `LLMOPS_BEDROCK.md`. That worked for IAM + pricing + InvokeModel patterns, but the **lifecycle** dimension has:

- A higher drift cadence (monthly, not quarterly) than the rest of the partial
- A different consumer surface — agents reading `/runtime/model/*` SSM params at *invocation* time, not just at deploy time
- A different incident playbook when a model goes Legacy mid-engagement (15-day inactivity → revoked access without warning if you've been offline)
- A different MCP-audit cadence (per F-AFIE-24, monthly for lifecycle vs. quarterly for everything else)

This partial isolates lifecycle so:
- Consumer kits can `[[LLMOPS_BEDROCK_MODEL_LIFECYCLE]]` from any deployment-pipeline doc without pulling in the full LLMOPS_BEDROCK partial
- The monthly maintenance check (F-AFIE-24 §4.1) has a clean target
- The AFIE F-AI-01 incident playbook has a documented home (rather than living as inline retro comments)

---

## 2. The lifecycle states

```
                                ┌─────────────┐
   Released  ────────────►      │   ACTIVE    │     ◄──── invoke freely
                                └──────┬──────┘
                                       │  ~6-12 months typical lifecycle
                                       ▼
                                ┌─────────────┐
   (Active → Legacy announced)  │   LEGACY    │     ◄──── existing customers
                                └──────┬──────┘            may lose access after
                                       │                    15 DAYS of inactivity
                                       │  typically 90-180 days
                                       ▼
                                ┌─────────────┐
                                │    EOL      │     ◄──── no new invocations
                                └─────────────┘            from any account
```

**Key transition rules (verified live via AWS canonical doc):**

| Transition | Behavior | Notice period |
|---|---|---|
| Active → Legacy (announcement) | Model still works for existing customers; no new account access | Typically 60-90 days before Legacy is enforced |
| Legacy → EOL (final cutover) | No invocations possible from any account | Stated EOL date is firm |
| **15-day inactivity rule (the gotcha)** | If a Legacy model goes unused by your account for 15 days, AWS may revoke access EARLY | Effective immediately, no warning |

The 15-day rule is what destroyed AFIE-CPG's Sprint 8 deployment: ms-09 went offline for 16 days (holiday + sprint transition); Claude 3 Sonnet had moved to Legacy in the interim; on redeploy `bedrock-runtime:InvokeModel` threw `AccessDeniedException` with no actionable error message.

**Authoritative source (MUST verify monthly via MCP):**
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html

---

## 3. Current Active models — verified 2026-06-17 snapshot

**This table is monthly-volatile.** Run the F-AFIE-24 §4.1 check on the 1st of every month and refresh:

| Provider | Model | Bedrock model ID (`foundation-model`) | Use-case canonical |
|---|---|---|---|
| Anthropic | Claude Sonnet 4.5 | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Reasoning, synthesis, multi-step planning |
| Anthropic | Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1:0` | Classification, extraction, routing, simple QA |
| Amazon | Titan-embed-text v2 | `amazon.titan-embed-text-v2:0` | Embeddings (1024-dim default) |
| Amazon | Nova Sonic v2 | `amazon.nova-sonic-v2:0` | Speech-to-text + speech-to-speech |
| Cohere | Cohere Rerank v3.5 | `cohere.rerank-v3-5:0` | KB rerank |

## 4. Legacy / EOL — DO NOT use in new code; check inactivity if old deployment

**This table is also monthly-volatile.** Same monthly refresh ritual.

| Model | Legacy date | EOL date | Public extended access from | Recommended replacement |
|---|---|---|---|---|
| `anthropic.claude-3-sonnet-20240229-v1:0` | 2026-01-30 | **2026-07-30** (past) | — | `anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `anthropic.claude-3-haiku-20240307-v1:0` | 2026-03-10 | 2026-09-10 | 2026-06-10 | `anthropic.claude-haiku-4-5-20251001-v1:0` |
| `anthropic.claude-sonnet-4-20250514-v1:0` | 2026-04-14 | **2026-10-14** | 2026-07-14 | `anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `amazon.nova-sonic-v1:0` | 2026-03-13 | 2026-09-14 | — | `amazon.nova-sonic-v2:0` |
| `amazon.nova-premier-v1:0` | 2026-03-13 | 2026-09-14 | — | (see model card) |
| `amazon.titan-image-generator-v2:0` | 2025-12-30 | **2026-06-30** (past) | — | (see model card) |

---

## 5. The 15-day inactivity rule — incident playbook

**When this fires:** your project hasn't called the model in 15 days AND the model has gone Legacy in the interim. On the next `InvokeModel` call:

```
botocore.errorfactory.AccessDeniedException:
  An error occurred (AccessDeniedException) when calling the InvokeModel operation:
  You don't have access to the model with the specified model ID.
```

**No CW log entry, no SNS notification, no IAM-policy denial.** The model has effectively been revoked from your account.

**Recovery playbook (AFIE Sprint 8 F-AI-01 retro):**

1. **Identify the model** — `grep` for the literal model ID in your CW logs (the deploy artifact OR the per-invoke metric dimension from LLMOPS_BEDROCK §3.0b).
2. **Confirm state** — `aws bedrock get-foundation-model --model-identifier <ID>` returns `modelLifecycle.status: LEGACY` or `EOL`.
3. **Pick replacement** — consult §3 above; map to the same use-case row.
4. **Update the SSM router** — change `/runtime/model/<task>` to the replacement model ID. **Do NOT redeploy code** — the router pattern from LLMOPS_BEDROCK §3.0b deliberately makes this a one-line SSM update.
5. **Verify** — `aws bedrock-runtime invoke-model --model-id <REPLACEMENT> --body ...` succeeds.
6. **Post-mortem** — open an `[Audit: R5/F-AI-XX]` audit-round entry; assess whether the project's monthly currency check (F-AFIE-24 §4.1) was running.

---

## 6. Cross-region inference profile resilience

Cross-region inference profiles (e.g., `us.anthropic.claude-sonnet-4-5-...`) automatically route to whichever region in the profile's pool has the active foundation model. **This is why the 3-ARN IAM canonical from F-AFIE-01 includes the profile ARN class** — it's not just an IAM nicety, it's a resilience mechanism.

When a region experiences a Bedrock-side issue (capacity, throttling, control-plane brownout), inference profiles silently fail over to other regions in the profile. Your IAM grant MUST include `arn:aws:bedrock:*:<account>:inference-profile/*` for that fallback to work.

**Region pool snapshot (verify against the AWS doc monthly):**

| Profile prefix | Pool members | Use when |
|---|---|---|
| `us.<model>` | us-east-1, us-east-2, us-west-2 | US-only workloads with regional autonomy |
| `eu.<model>` | eu-west-1, eu-west-3, eu-central-1 | EU-only workloads with regional autonomy |
| `apne.<model>` | ap-northeast-1, ap-northeast-3 | APAC-only workloads |
| `global.<model>` | All supported regions | Cross-geo workloads (latency-flexible) |

---

## 7. Pre-deploy + per-invocation patterns

### 7.1 Pre-deploy currency check (the F-AFIE-23 §3.1 + F-AFIE-24 §4.1 wire-up)

```python
# scripts/pre_deploy_audit.py (called from your CI workflow)
from mcp_helpers import read_aws_doc

doc = read_aws_doc(
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html",
    max_length=10_000,
)
# Parse Active + Legacy/EOL tables; cross-check against infra/runtime_models.json.
# Fail CI if any project-referenced model is in Legacy or EOL.
```

### 7.2 Per-invocation lifecycle awareness (the agent runtime pattern)

```python
# agents/shared/model_invoker.py
"""Wrap bedrock-runtime:InvokeModel with lifecycle-aware retry.
On AccessDeniedException, log the model ID + a remediation pointer, and
fail-over to the SSM-configured fallback before throwing.
"""

import boto3, os, json, logging
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
_brt = boto3.client("bedrock-runtime")
_ssm = boto3.client("ssm")


def invoke_with_lifecycle_retry(task: str, prompt: dict) -> dict:
    proj = os.environ["PROJECT_NAME"]
    primary  = _ssm.get_parameter(Name=f"/{proj}/runtime/model/{task}")["Parameter"]["Value"]
    fallback = _ssm.get_parameter(Name=f"/{proj}/runtime/model/{task}_fallback")["Parameter"]["Value"]

    for model_id in (primary, fallback):
        try:
            resp = _brt.invoke_model(modelId=model_id, body=json.dumps(prompt).encode())
            return json.loads(resp["body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] != "AccessDeniedException":
                raise
            logger.error(
                "Model %s returned AccessDenied — likely Legacy/EOL transition "
                "or 15-day inactivity rule fired. See "
                "LLMOPS_BEDROCK_MODEL_LIFECYCLE.md §5 for the incident playbook.",
                model_id,
            )
            # Emit a Powertools metric for paging
            from aws_lambda_powertools import Metrics
            from aws_lambda_powertools.metrics import MetricUnit
            m = Metrics(namespace=f"{proj}/Bedrock")
            m.add_metric(name="LifecycleAccessDenied", unit=MetricUnit.Count, value=1)
            m.add_dimensions(ModelId=model_id, Task=task)
            m.flush_metrics()
            # Loop continues to fallback model
    raise RuntimeError(f"Bedrock invocation failed for task={task}; both primary and fallback in Legacy/EOL")
```

### 7.3 Monthly lifecycle-refresh PR template

When the maintenance script (F-AFIE-24 §4.1) detects a transition, the auto-issue body should follow this template:

```markdown
## R5 lifecycle drift — month of {{YYYY-MM}}

**Affected partials:** LLMOPS_BEDROCK.md §3.0, LLMOPS_BEDROCK_MODEL_LIFECYCLE.md §3 + §4

**Drift detected:**
- `{{model-id}}` transitioned {{ACTIVE → LEGACY}} on {{date}}
- New EOL date: {{date}}
- Recommended replacement: `{{replacement-model-id}}`

**Project impact (consumer kits to update):**
- [ ] kits/<name>.md — Bedrock model references
- [ ] kits/<name2>.md — RAG embedding references

**Synth-guard impact (F-AFIE-22):**
- No new guard needed (the existing `assert_bedrock_invoke_three_arn_pattern` continues to enforce IAM correctness)

**MCP audit citation:** https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html (read {{date}})
```

---

## 8. Five non-negotiables

1. **Monthly MCP audit of the lifecycle doc is MANDATORY** — not quarterly. The 15-day inactivity rule means a quarterly miss is a 4-month risk window.
2. **Project-side: every model ID comes from SSM** (`/runtime/model/<task>`), not from a code literal. This makes the lifecycle response a one-line SSM update, not a redeploy.
3. **Every project SHIPS a fallback model** for every primary — set `/runtime/model/<task>_fallback` to the next-best model in the same use-case row.
4. **The IAM grant uses the 3-ARN canonical** (per F-AFIE-01) — foundation-model + inference-profile + application-inference-profile. Cross-region failover depends on the inference-profile arm.
5. **AccessDenied on InvokeModel triggers the §5 playbook** — never assume IAM misconfig until you've verified the model's lifecycle state.

---

## 9. References

- `LLMOPS_BEDROCK.md` §3.0 + §3.0b + §3.1 — the canonical partial this one carves out from
- `OPS_AWS_SERVICE_CURRENCY_CHECK.md` §4.1 — the monthly check that consumes this partial (F-AFIE-24)
- `OPS_LIVE_READONLY_MCP_AUDIT.md` §3.1 + §4.1 — pre-deploy + post-deploy verification (F-AFIE-23)
- `_assertions/cdk_synth_guards.md` — `assert_bedrock_invoke_three_arn_pattern` (F-AFIE-22)
- AWS canonical lifecycle doc: https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
- AWS Bedrock pricing (NOT MCP-fetchable; non-docs.aws.amazon.com): https://aws.amazon.com/bedrock/pricing/

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-06-17 | Initial. Carves Bedrock lifecycle awareness out of LLMOPS_BEDROCK §3.0 into a dedicated partial for consumers to `[[link]]`. NEW partial — F-AFIE-25. |
