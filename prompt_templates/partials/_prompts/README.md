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

- The 17 v2.0 partials themselves live one level up in `partials/*.md`
- Originals (pre-rewrite) are at `../../partials_backup_2026-04-21/`
- `LAYER_BACKEND_LAMBDA.md` is the canonical structural exemplar — both prompts reference it as ground truth
