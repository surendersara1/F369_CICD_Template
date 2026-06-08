# Lessons from AFIE — Why the One-Shot Kit Build Failed

**Author:** Claude Opus 4.7 (1M context) — written 2026-06-16 as Hour 0-4 foundation for R4 audit.
**Source project:** `E:\NBS_PAF_Finance_V2\afie-deploy\` (AFIE-CPG — agentic finance Q&A platform for MENA CPG executives).
**Source evidence:** `AUDIT_2026-06-06.md` (original code-review audit), `SPRINT8_FINDINGS_QUEUE.md` (AWS-docs-truth audit, 65 findings), `SPRINT10_FINDINGS_QUEUE.md` (verification, 7 new gaps), `BAKEOFF_CERTIFICATION.md` (final state).

---

## What AFIE was supposed to prove

AFIE was the most ambitious kit test the F369 library has run — a 20-stack agentic-finance app built from F369 canonical partials. The success criterion was straightforward: **one-shot clean build to deploy-ready state.** That criterion was not met.

What actually happened: **11 autonomous Claude Code sprints over a week**, ~72 distinct findings across two independent audit lenses, ~7-8 hours of cumulative Claude compute, $1000s in iteration cost. The endpoint (`BAKEOFF_CERTIFICATION.md` = clean GO as of Sprint 11) is solid — but the path to it was not "one shot."

This document is the honest analysis of *why*. It is the input to R4 — the audit round that retrofits the library so the *next* project starts at the bar AFIE reached after 11 sprints.

---

## The 6 root-cause classes (with AFIE finding traceability)

### Class A — Partials encoded 2024 snapshots, not 2026 truth

The partials were authored in waves 1-19 across 2026-01 through 2026-04. AWS evolves faster than the wave cadence. By the time AFIE consumed them, the partials encoded patterns that were correct *when authored* but had drifted to wrong *when consumed*:

| Partial assumption | Current AWS truth | AFIE finding ID |
|---|---|---|
| `claude-sonnet-4-20250514-v1:0` is the canonical synthesis model | Sonnet 4 went **Bedrock Legacy 2026-04-14**, **EOL 2026-10-14**; current Active = `claude-sonnet-4-5-20250929-v1:0` | F-AI-01 (CRITICAL) |
| Aurora Serverless v2 min ACU = 0.5 (never $0 idle) | Aurora v2 **scale-to-zero is GA** | F-DATA-02 (HIGH) |
| RAG vector store = OpenSearch Serverless (~$700/mo idle floor) | **Amazon S3 Vectors GA Dec 2, 2025**, first-class Bedrock KB backend, ~$5-$10/mo for AFIE's workload | (Sprint 6 deep-research; saved $173-$346/mo) |
| API Gateway REST is canonical | **HTTP API ~70% cheaper** for JWT-only authz workloads | F-INT-02 (MEDIUM) |
| `amazon.titan-embed-text-v1` 1536-dim embeddings | Titan v2 default 1024-dim; v1 deprecated | F-AI-08 + Sprint 7 MS-08 latent bug |
| Redshift Serverless base = 8 RPU | 4 RPU base now available for <32TB | F-DATA-06 (MEDIUM) |

**Generic mechanism:** the canonical partials have a "First audited" date in `prompt_templates/partials/README.md` but no per-content "last-AWS-verified" mechanism. Once a partial passes R1/R2/R3 audit, it's locked as canonical — but the AWS reality underneath it keeps moving.

**Fix in R4:**
- Add `last-reviewed: YYYY-MM-DD` to every partial header (already convention in `.claude/rules/partial-authoring.md`)
- Make `last-reviewed` enforceable — a kit's `__preflight__` step verifies every consumed partial's `last-reviewed` is within 90 days
- New partial `OPS_AWS_SERVICE_CURRENCY_CHECK.md` codifies the quarterly refresh runbook + per-service MCP queries

---

### Class B — Patterns re-derived from memory at author time (even with the Canonical-Copy Rule in place)

The Canonical-Copy Rule was designed to prevent this. R3 caught the same pattern. **It happens anyway.** AFIE proved the rule is necessary but not sufficient.

The smoking gun: `AGENTCORE_RUNTIME.md` §3.2 line 87-88:

```python
execution_role.add_to_policy(iam.PolicyStatement(
    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
))
```

Only `foundation-model/*`. **No `inference-profile/*`.** When AFIE Sprint 9 (correctly) swapped the model literal to `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (a cross-region inference profile), the IAM grant was suddenly insufficient. Sprint 10's verification caught it as **G-NEW-01** — the deploy-time AccessDenied blocker.

The pattern was even already correct in the same codebase at `ms-07-agentcore-memory-stack.ts:94-95`:

```typescript
resources: [
  `arn:aws:bedrock:*::foundation-model/*`,
  `arn:aws:bedrock:*:${cdk.Aws.ACCOUNT_ID}:inference-profile/*`,
]
```

This is a R3-class hallucination: the canonical pattern exists in one place in the codebase but the partial author didn't open `AGENTCORE_MEMORY` or check the AWS doc — they re-derived from memory and produced an incomplete grant.

Other AFIE evidence:
- `LAYER_OBSERVABILITY.md` + KMS interaction — SNS CMK topic doesn't grant `cloudwatch.amazonaws.com`; alarms silently fail. AWS KMS docs explicitly document the cross-service grant requirement. Partial encoded "encrypt the SNS topic" without checking. **F-OBS-03 (HIGH) — the #1 reliability bug AFIE found.**
- `BEDROCK_KNOWLEDGE_BASES.md` — didn't list S3 Vectors as a backend option even though S3 Vectors went GA December 2, 2025 and is first-class Bedrock KB integrated. **Sprint 6 deep-research finding.**
- Cedar `context.amount_sar` vs `context.input.*` shape — the AWS AgentCore example uses nested envelope; partial encoded flat top-level. **G-NEW-02 (MED) — silently permissive governance.**

**Fix in R4:**
- Per-code-block AWS doc URL requirement — every CDK block now must end with `# AWS doc: <URL>` comment
- All Tier 1-3 partials get retrospective MCP audit + URL annotation
- Update `.claude/rules/partial-authoring.md` to make per-block citation mandatory

---

### Class C — Composite templates didn't enforce partial currency at composition time

`F369_LLM_TEMPLATES` composite templates (e.g., `mlops/22_strands_agentcore_deployment.md`) reference partials by name. They don't:
- Verify the partial's `last-reviewed` is recent
- Run the partial's MCP currency-check before composing
- Audit the resulting CDK at synth-time against AWS docs

So stale partials silently propagate. AFIE used the partials as they were on Wave 19 (2026-04-28). 6-8 weeks of AWS drift later, when AFIE deployed, the stale assumptions broke.

**Fix in R4:**
- Every composite template gets a `__preflight__` section listing the partials it consumes
- The preflight runs each partial's MCP currency-check before composing
- Composites cite the new `OPS_AWS_SERVICE_CURRENCY_CHECK.md` partial

---

### Class D — Kits don't run a live-readonly AWS account check at engagement kickoff

AFIE Sprint 10 made a discovery worth landing: **the Well-Architected Security MCP server has live read-only AWS access** (via `sarapython` AWS profile). This means we can query the actual deployment-target account *before* writing code:
- Is GuardDuty on? (F-SEC-02)
- Is Inspector on? (F-SEC-02)
- Is Access Analyzer on? (F-SEC-02)
- Are there existing IAM Access Analyzer findings? (F-SEC-02)
- What OpenSearch collections are reachable from public? (F-DATA-01)

AFIE found all of these *at audit time* (Sprint 8, two months into the build). They could have been caught *at kickoff* if any kit's pre-build step had run live-readonly MCP queries against the target account.

**Fix in R4:**
- New partial `OPS_LIVE_READONLY_MCP_AUDIT.md` codifies the mandatory pre-build live-readonly audit
- All 8 kits' delivery plans get a "Day 1" step: run `OPS_LIVE_READONLY_MCP_AUDIT`
- `kits/_template/` Business-First Kit Standard gets this as a new mandatory section

---

### Class E — No CDK-synth-time assertions for "common traps"

AFIE surfaced several patterns where both behaviors are individually valid but the *combination* is broken:

| Trap | What's individually valid | What's broken when combined | AFIE finding |
|---|---|---|---|
| TLS double-config | ALB cert (HTTPS-on-ALB) is fine; CloudFront edge (HTTPS-at-edge, HTTP-to-origin) is fine | Both enabled → CloudFront origin fetch receives ALB's 301 redirect, edge path breaks | G-NEW-05 (LOW but operationally severe) |
| Hardcoded model literal bypassing SSM | SSM-driven model selection is the canonical path; literal fallback is intentional | Hardcoded literal silently overrides SSM, breaking the swap mechanism | F-AI-02 (HIGH) |
| IAM grant missing inference-profile/* | `foundation-model/*` is valid; `inference-profile/*` is valid | When model literal is a cross-region inference profile, only the latter grant works | G-NEW-01 (HIGH BLOCKER) |
| Cedar rules `context.amount_sar` flat | Flat top-level context access is valid in Cedar | AgentCore delivers nested `context.input.*` → no forbid rule fires | G-NEW-02 (MED) |

These can't be caught at PR review reliably — too many surface combinations. They *can* be caught at `cdk synth` time with a small assertion library. AFIE Sprint 11 added the TLS-pick-one-path synth assertion in `infra/bin/afie-app.ts` — it works.

**Fix in R4:**
- New canonical pattern in `F-AFIE-22`: `_assertions/cdk_synth_guards.md` documenting the synth-assertion approach
- TLS-pick-one-path assertion as the first instance
- Future: no-legacy-model-literal assertion, scoped-resource assertion, KMS-cross-service-grant assertion

---

### Class F — No regression test scaffolds per partial

AFIE Sprints 7-11 added ~120 new regression tests (started at 197, ended at 794). Many test exactly the AFIE-finding-level invariants that the partials should have shipped with — for example:

- `tests/unit/test_inference_profile_iam_grant.py` — asserts every InvokeModel grant has BOTH foundation-model and inference-profile ARN classes
- `tests/unit/test_model_id_no_legacy.py` — asserts no `claude-sonnet-4` literal (without `-5`) exists anywhere in `agents/`, `lambda/`, `infra/`, `portal/`
- `tests/unit/test_tls_single_path_assertion.py` — asserts `afie-app.ts` throws if both TLS flags set
- `tests/unit/test_cedar_rules_context_shape.py` — asserts Cedar rule context keys match documented agent-side shape
- `tests/unit/test_ms09_pitr_spec.py` — asserts MS-09's tables use `pointInTimeRecoverySpecification` not deprecated boolean
- `tests/unit/test_supervisor_iam_scoping.py` — asserts InvokeAgentRuntime/InvokeGateway scoped to account+region
- `tests/unit/test_s3vectors_kb_client_coverage.py` — full coverage of the S3 Vectors KB client wrapper

These are AFIE-specific but the *invariants* they assert apply to every project consuming the same partials.

**Fix in R4:**
- Each updated partial's §6 worked example evolves into a pytest fixture stub that consumer projects can adapt
- The R4 audit report explicitly lists test fixtures that should exist downstream of each fix
- Deferred to R5: a `_test_fixtures/` library of reusable pytest patterns

---

## Cumulative impact (the actual cost of AFIE's audit-fix-verify cycle)

| Sprint | Brew time | Findings closed | Notable |
|---|---|---|---|
| 1 | ~55 min | 20+ (CRITICAL+HIGH) | Original code-review audit lens |
| 2 | ~50 min | 16 | KB-SQL + MCP collapse foundations |
| 3 | ~54 min | 25 | Mechanical migration sweep |
| 4 | ~50 min | 18 | Finalization + queued deletions |
| 5 | ~91 min | 14 | Persona-data-driven + 100% coverage + COST_ESTIMATE doc |
| 6 | ~50 min | 7 | S3 Vectors migration (TIERED) — saved $173-346/mo |
| 7 | ~41 min | 18 | Fitness review + REDEPLOY_RUNBOOK |
| 8 | ~21 min | 65 surfaced (audit only) | AWS-docs-MCP audit — second independent lens |
| 9 | ~90 min | 65 closed | Fix all Sprint 8 findings |
| 10 | ~26 min | 7 surfaced + 65 verified | Verification caught G-NEW-01 (deploy-time blocker) |
| 11 | ~26 min | 9 closed | Surgical close + BAKEOFF_CERTIFICATION re-issued GO |

**~7-8 hours total compute. ~$1000s in iteration cost. ~72 distinct findings across two independent audit lenses.**

The library cost to retrofit (R4): 48 hours, single pass, MCP-audited per change.

---

## What R4 changes structurally (not just per-partial fixes)

R4 isn't just "patch the partials." It changes 4 structural conventions in F369:

1. **Per-partial currency mechanism.** Every partial header gets `last-reviewed: YYYY-MM-DD` and a MCP currency-check query. Composites verify before composing.

2. **Per-code-block AWS doc URL.** Every CDK code block in a partial must end with `# AWS doc: <canonical URL>`. R4 retrofits Tier 1-3 partials; future authoring requires it.

3. **Live-readonly pre-build audit.** Every kit's Day 1 step runs `OPS_LIVE_READONLY_MCP_AUDIT.md` against the deployment-target account. Catches Class D issues at kickoff.

4. **Synth-time assertion library.** Class E traps get caught at `cdk synth` instead of at audit time. New canonical pattern documented in `_assertions/cdk_synth_guards.md`.

---

## What this doesn't fix (deferred to R5+)

- Sub-monthly Bedrock model lifecycle tracking (R4 makes it discoverable; R5 might automate detection)
- Per-partial test fixture library (R4 evolves §6 worked examples into stubs; full library is R5)
- IAM Access Analyzer "policies" (R4 enforces least-privilege patterns; AI-driven scope analysis is R5)
- Multi-region partials drift (R4 fixes us-east-1 examples; multi-region patterns + Active-Active is its own audit round)

---

## Conclusion

AFIE wasn't a failure of execution — Sprint 9 closed 65 findings in 90 minutes of compute and Sprint 11 re-certified the system as clean GO. The 11-sprint cost was a **failure of library currency + composition discipline**.

R4 lands the lessons. After R4, the next kit build should hit a far higher bar at Day 1 — the bar AFIE reached only after 11 sprints. The structural mechanism (per-partial currency, per-block doc citation, live-readonly pre-build audit, synth-time assertion library) is the actual deliverable; the 25 individual partial edits are evidence the mechanism works.
