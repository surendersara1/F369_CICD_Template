# F369 Partials — Builder/Auditor Prompts

Meta-prompts for maintaining the F369 partial library. Not partials themselves — run these against the partials to build, audit, or evolve them.

**Location:** `E:\F369_CICD_Template\prompt_templates\partials\_prompts\`
**Underscore prefix** keeps this folder sorted first and distinct from the actual SOP partials that sit alongside.

---

## Contents

| File | Model | When to run |
|---|---|---|
| [`audit_partials_v2.md`](audit_partials_v2.md) | Opus 4.7 (deep review) | Audit the 17 v2.0 partials rewritten on 2026-04-21. Produces a findings report at `E:\F369_CICD_Template\docs\audit_report_partials_v2.md`. Read-only; no partials modified. |
| [`build_remaining_partials_v2.md`](build_remaining_partials_v2.md) | Opus 4.6 (fast build) | Rewrite the remaining 37 partials (Strands, AgentCore, MLOps, Compliance, Data platforms, Infra variants) to the same 8-section SOP structure established in the 17 exemplars. |

---

## Recommended sequence

1. Run `audit_partials_v2.md` first with Opus 4.7.
2. Triage the CRITICAL/HIGH findings. Fix the exemplar bugs in the 17 before they cascade.
3. Run `build_remaining_partials_v2.md` with Opus 4.6 to produce the other 37.
4. Run `audit_partials_v2.md` again with its `§2 Scope` expanded to cover all 54 partials.

---

## Invocation (Windows cmd)

```cmd
cd /d E:\F369_CICD_Template

:: Audit pass (Opus 4.7)
type prompt_templates\partials\_prompts\audit_partials_v2.md | claude --dangerously-skip-permissions --print > docs\audit_stdout.log 2>&1

:: Build pass (Opus 4.6, after audit passes)
type prompt_templates\partials\_prompts\build_remaining_partials_v2.md | claude --dangerously-skip-permissions --print > docs\build_stdout.log 2>&1
```

---

## Related

- **[`../README.md`](../README.md)** — Partials library index + **Canonical Partials Registry** (the MUST-READ for any session authoring a new partial). Enforces the Canonical-Copy Rule that audits R1-R3 identified as the #1 source of schema-hallucination FAILs.
- The 75 v2.0 partials themselves live one level up in `../partials/*.md`.
- Originals (pre-rewrite) are at `../../partials_backup_2026-04-21/`.
- `../LAYER_BACKEND_LAMBDA.md` is the canonical structural exemplar + the 5 non-negotiables — both prompts reference it as ground truth.
- Audit reports: `../../../docs/audit_report_partials_v2*.md` (3 rounds to date).

---

## The Canonical-Copy Rule (MUST READ before authoring a new partial)

Audit rounds R1-R3 all caught the same failure mode: **new partials re-deriving a primitive's pattern from memory instead of copying from an already-audited canonical partial**. Most recent example (R3, 2026-04-23): two Wave-2 partials fabricated a `filterable_metadata_keys` property on `AWS::S3Vectors::Index` that does not exist — even though `DATA_S3_VECTORS.md` (audited in R2) documents the correct schema.

The rule is now enforced in `build_remaining_partials_v2.md §0 Hard Rule #8` and §9 Canonical Partials Registry. New sessions authoring partials MUST load that prompt + read §9 before touching code.

The same registry lives in the canonical location at `../README.md` for anyone browsing the library directly.
