# Prompt — Audit of Rewritten F369 Partials (17 files, v2.0)

**Home:** `E:\F369_CICD_Template\prompt_templates\partials\_prompts\audit_partials_v2.md`

> **How to invoke:**
>
> ```cmd
> cd /d E:\F369_CICD_Template
> type prompt_templates\partials\_prompts\audit_partials_v2.md | claude --dangerously-skip-permissions --print
> ```
>
> **Run with Opus 4.7 (deep reasoning).** This prompt performs an adversarial audit of 17 partial rewrites by Opus 4.6 on 2026-04-21. No source code is modified — output is a findings report only.

---

## 0. Hard Rules

1. **READ-ONLY first pass.** Do not modify any `.md` file in `partials/` during the audit. If you find issues, write them to `docs/audit_report_partials_v2.md` at `E:\F369_CICD_Template\` — that's the only file you create.
2. **Verify claims, do not trust prose.** Every CDK API the author references (class names, method signatures, kwargs, service enums) must be cross-checked against live AWS CDK docs. You have the `awslabs.cdk-mcp-server` MCP available — use it.
3. **Synth-test the micro-stack code examples.** For each partial's §4 Micro-Stack Variant code block, extract into a temp directory, wire a minimal app harness, run `cdk synth --no-lookups -q`. Capture exit code + any errors.
4. **Offline only.** No AWS API calls. `CDK_DISABLE_VERSION_CHECK=1`, `--no-lookups`, no `cdk deploy`.
5. **No git push, no branch, no commit.** Report only. A human will decide which findings to act on.
6. **Stop if the partials folder is empty or missing** — that's a corrupted state, not an audit target.

---

## 1. Inputs

| Path | Purpose |
|---|---|
| `E:\F369_CICD_Template\prompt_templates\partials\` | The 17 rewritten partials (v2.0, 2026-04-21) to audit |
| `E:\F369_CICD_Template\prompt_templates\partials_backup_2026-04-21\` | Originals before the rewrite (v1.0 baselines for comparison) |
| `E:\NBS_Research_America\docs\Feature_Roadmap.md` | The feature IDs (A-00..A-32, OBS-01..27, etc.) cross-referenced in each SOP — verify they all exist |
| `E:\NBS_Research_America\docs\template_params.md` | Parameter names (`PROJECT_NAME`, `BEDROCK_MODEL_ID`, etc.) referenced in SOPs — verify they all exist |
| `E:\NBS_Research_America\infrastructure\cdk\stacks\*.py` | The actual stacks built from earlier drafts of these partials — use as ground truth for "does this pattern really work" |

## 2. Scope — the 17 partials under audit

```
LAYER_BACKEND_LAMBDA.md       LAYER_NETWORKING.md        LAYER_FRONTEND.md
LAYER_BACKEND_ECS.md          LAYER_SECURITY.md          LLMOPS_BEDROCK.md
EVENT_DRIVEN_PATTERNS.md      LAYER_DATA.md              LAYER_OBSERVABILITY.md
WORKFLOW_STEP_FUNCTIONS.md    LAYER_API.md               OPS_ADVANCED_MONITORING.md
OBS_OPENTELEMETRY_GRAFANA.md  LAYER_API_APPSYNC.md       SECURITY_WAF_SHIELD_MACIE.md
CICD_PIPELINE_STAGES.md       federated_data_layer.md
```

Every other `.md` file in `partials/` (STRANDS_*, AGENTCORE_*, MLOPS_*, COMPLIANCE_*, DATA_MSK_KAFKA, GLOBAL_MULTI_REGION, PLATFORM_EKS_CLUSTER, aws_managed_mcp) is **out of scope** for this audit. Do not open them.

## 3. Rubric — per-partial checklist (produce a row in the report for each of 17)

For each partial, grade each line as `PASS` / `WARN` / `FAIL` / `NOT-APPLICABLE`:

### 3.1 Structure
- [ ] Has `# SOP — <name>` H1
- [ ] Has `Version: 2.0 · Last-reviewed: 2026-04-21 · Status: Active` front-matter
- [ ] Has all 8 sections: Purpose, Decision, Monolith Variant, Micro-Stack Variant, Swap matrix, Worked example, References, Changelog
- [ ] Changelog §8 has entry for v2.0 (2026-04-21) and v1.0 (2026-03-05)

### 3.2 Code correctness (Monolith variant)
- [ ] Every CDK import resolves (verify against `aws_cdk` Python module layout)
- [ ] Every class constructor kwarg exists (cross-check via `cdk-mcp-server` or AWS CDK docs)
- [ ] Every method call matches documented signature
- [ ] No deprecated APIs (`log_retention=` on Lambda, `BucketEncryption.KMS_MANAGED`, etc.)

### 3.3 Code correctness (Micro-Stack variant)
- [ ] Same checks as §3.2
- [ ] The 5 non-negotiables from `LAYER_BACKEND_LAMBDA §4.1` are followed:
    1. Asset paths anchored to `Path(__file__)` (not CWD-relative strings)
    2. No `X.grant_*(role)` where `X` and `role` are cross-stack
    3. No `targets.SqsQueue(q)` where `q` is cross-stack (uses `CfnRule` instead)
    4. No bucket-in-one-stack + CloudFront-OAC-in-another
    5. No `encryption_key=ext_key` + `grant_*` chain across stacks

### 3.4 Executable verification
- [ ] Extract the Micro-Stack variant code + Worked Example from §4 + §6 into a temp `audit/<name>/` dir
- [ ] Create minimal `app.py` that instantiates the stack(s) with fixture dependencies
- [ ] Run `CDK_DISABLE_VERSION_CHECK=1 cdk synth --no-lookups -q` → exit 0 required
- [ ] Record actual CloudFormation resource count vs what `§6 Worked Example` claims

### 3.5 Cross-references
- [ ] Every `docs/Feature_Roadmap.md` feature ID referenced (e.g. `A-15`, `OBS-06`) exists in the actual roadmap
- [ ] Every `docs/template_params.md` key referenced (e.g. `BEDROCK_MODEL_ID`) exists in params
- [ ] Every "Related SOP" referenced in §7 exists in the partials folder

### 3.6 Consistency across partials
- [ ] Identity-side grant helpers (`_kms_grant`, `_ddb_grant`, `_s3_grant`, `_sqs_grant`, `_secret_grant`) are defined identically in partials that use them (or explicitly imported from `LAYER_BACKEND_LAMBDA`)
- [ ] Tag dict / naming conventions are consistent
- [ ] Region / account placeholder syntax is consistent

### 3.7 Completeness vs v1.0 baseline
- [ ] Diff against `partials_backup_2026-04-21/<name>.md`
- [ ] Nothing in v1.0 that belonged in v2.0 was silently dropped (if something was dropped, is there a `TODO` or note explaining why?)

## 4. Audit process (ordered)

### Step 1 — Fast scan (expected: 10 minutes)
Read each of 17 partials once, grade §3.1 structure. Output a preliminary table.

### Step 2 — API verification (expected: 30 minutes)
For each Monolith + Micro-Stack code block:
- Extract every `ClassName(...)` and `fn.method(...)` call
- Look up via `awslabs.cdk-mcp-server` or fetch AWS CDK docs
- Flag any hallucinated / deprecated / wrong-signature call

### Step 3 — Synth test (expected: 1 hour)
For each Micro-Stack variant:
- Create `audit/<partial-name>/app.py` with mock upstream fixtures
- Run `cdk synth`
- Log exit code + error messages
- If synth fails, attempt one obvious fix (e.g. missing import) and re-run once — if still fails, flag as `FAIL` with full error

### Step 4 — Cross-reference check
- Grep `docs/Feature_Roadmap.md` for every feature ID cited
- Grep `docs/template_params.md` for every `[PLACEHOLDER]` cited
- List the partials folder, verify every `Related SOP` name exists

### Step 5 — Consistency audit
- Concatenate helper-function definitions across partials; flag any that diverge
- Tabulate tags/naming; flag inconsistency

### Step 6 — Completeness diff
- `diff partials_backup_2026-04-21/X.md partials/X.md` (or the bash equivalent)
- Flag content from v1.0 that didn't make it to v2.0 without explicit reason

### Step 7 — Write `docs/audit_report_partials_v2.md` at `E:\F369_CICD_Template\`

Report format:

```markdown
# Audit Report — F369 Partials v2.0 Rewrite

**Auditor:** Claude Opus 4.7
**Audit date:** <today>
**Scope:** 17 partials rewritten 2026-04-21
**AWS API calls made:** 0
**cdk synth runs:** <count>
**cdk synth exit-0 count:** <count> / <count>

## Executive Summary

- Partials graded PASS end-to-end: X / 17
- Partials with WARN-only findings: Y / 17
- Partials with any FAIL: Z / 17
- Total non-negotiables violations: N
- Hallucinated CDK APIs found: M

## Per-partial Grades

| # | Partial | Struct | Mono code | Micro code | 5 Non-Neg | Synth | Xref | Consistency | Completeness | Overall |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | LAYER_BACKEND_LAMBDA | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |

## Detailed Findings

### Finding F001 — <Severity: CRITICAL|HIGH|MED|LOW>
**Partial:** `LAYER_<name>.md`
**Section:** §4.X
**Issue:** <concrete description>
**Evidence:** <grep output, synth error, doc link>
**Recommended fix:** <specific edit>

### Finding F002 — ...

## Appendix A — Synth Transcripts

<For each of the 17 Micro-Stack synth attempts: command, exit code, full stderr if any>

## Appendix B — CDK API Verification Log

<For each class/method checked: name, CDK docs URL, verdict>

## Appendix C — Cross-Reference Check

<Feature IDs cited vs actually present in roadmap; missing ones listed>
```

## 5. Severity rubric

- **CRITICAL** — cycle-forming code that will break `cdk synth` when used in a real project. Example: a `key.grant_*(cross_stack_role)` call.
- **HIGH** — deprecated API, wrong signature, will raise at synth. Example: `log_retention=` on Lambda, `api_key=` on `add_usage_plan`.
- **MED** — doc inconsistency, missing cross-reference, unclear example. Won't break deploys but confuses future users.
- **LOW** — style, typos, minor phrasing.

## 6. What NOT to do

- Do NOT rewrite the partials yourself. Report only.
- Do NOT call any AWS API.
- Do NOT add new sections that the author didn't write. If something is missing, note it as a finding; don't "helpfully" patch it.
- Do NOT skip the synth step to save time. That's the whole point of this audit.
- Do NOT audit partials outside the 17-file scope.

## 7. Completion

When §4 steps 1–7 are done, print:

```
===================================================================
AUDIT COMPLETE
  Partials audited:        17 / 17
  Synth tests run:         <N>
  Synth tests passed:      <M> / <N>
  CRITICAL findings:       <c>
  HIGH findings:           <h>
  MED findings:            <m>
  LOW findings:            <l>
  Report:                  E:\F369_CICD_Template\docs\audit_report_partials_v2.md
  AWS API calls made:      0
===================================================================
```

Then exit.
