# Prompt — Build Remaining 37 F369 Partials to v2.0 SOP Standard

**Home:** `E:\F369_CICD_Template\prompt_templates\partials\_prompts\build_remaining_partials_v2.md`

> **How to invoke (overnight, local build only):**
>
> ```cmd
> cd /d E:\F369_CICD_Template
> type prompt_templates\partials\_prompts\build_remaining_partials_v2.md | claude --dangerously-skip-permissions --print
> ```
>
> **Run with Opus 4.6 (fast mode).** A subsequent Opus 4.7 audit pass is planned and uses `audit_partials_v2.md`.

---

## 0. Hard Rules

1. **LOCAL ONLY.** No AWS API calls, no deploys, no git push. Allowed: file I/O, `cdk synth --no-lookups -q`, `pip install` (to local venv).
2. **Use Opus 4.6.** If the model label says anything else, stop.
3. **Exemplar-driven.** You MUST follow the 8-section structure defined in `LAYER_BACKEND_LAMBDA.md` v2.0. Read that file first. Any deviation is a bug.
4. **Backup before overwrite.** Originals of the 37 target files already exist at `E:\F369_CICD_Template\prompt_templates\partials_backup_2026-04-21\`. Do not touch the backup. Do not create a second backup.
5. **No new partials.** Only rewrite the 37 listed in §2. Do not create new files beyond the scope.
6. **No AI-invented CDK APIs.** Verify every class / method / kwarg via `awslabs.cdk-mcp-server` MCP before writing. If uncertain, write `TODO(verify): <what to check>` in the code and move on — never hallucinate.
7. **Stop on ambiguity.** If an original partial is about a service you don't know well, write a stub with `## 1. Purpose` + `## 2. Decision` + a TODO note, and skip the code variants. Flag in the execution log.

---

## 1. Context — Read Before Building

Read these in order. They define the canonical pattern you must replicate:

1. `E:\F369_CICD_Template\prompt_templates\partials\LAYER_BACKEND_LAMBDA.md` — **THE exemplar**. 8 sections, dual-variant structure, the five non-negotiables.
2. `E:\F369_CICD_Template\prompt_templates\partials\EVENT_DRIVEN_PATTERNS.md` — how §4 Micro-Stack handles the `CfnRule` / static-policy pattern.
3. `E:\F369_CICD_Template\prompt_templates\partials\LAYER_FRONTEND.md` — how §4 explicitly names the architectural constraint (bucket + distro must share a stack).
4. `E:\NBS_Research_America\docs\Feature_Roadmap.md` — feature IDs used for cross-references.
5. `E:\NBS_Research_America\docs\template_params.md` — canonical parameter names.

The v1.0 originals of each target file are at `partials_backup_2026-04-21/<name>.md`. Use them as source of domain content — but rewrite structure, codify monolith-vs-micro-stack decision, fix CDK-version drift, remove cross-stack anti-patterns.

---

## 2. Scope — 37 Partials, Grouped by Domain

### Group A — Strands Agents (10 files)
`STRANDS_AGENT_CORE.md`, `STRANDS_DEPLOY_ECS.md`, `STRANDS_DEPLOY_LAMBDA.md`, `STRANDS_EVAL.md`, `STRANDS_FRONTEND.md`, `STRANDS_HOOKS_PLUGINS.md`, `STRANDS_MCP_SERVER.md`, `STRANDS_MCP_TOOLS.md`, `STRANDS_MODEL_PROVIDERS.md`, `STRANDS_MULTI_AGENT.md`, `STRANDS_TOOLS.md`

### Group B — Bedrock AgentCore (7 files)
`AGENTCORE_RUNTIME.md`, `AGENTCORE_GATEWAY.md`, `AGENTCORE_IDENTITY.md`, `AGENTCORE_MEMORY.md`, `AGENTCORE_OBSERVABILITY.md`, `AGENTCORE_A2A.md`, `AGENTCORE_AGENT_CONTROL.md`

### Group C — SageMaker + MLOps (10 files)
`MLOPS_SAGEMAKER_TRAINING.md`, `MLOPS_SAGEMAKER_SERVING.md`, `MLOPS_BATCH_TRANSFORM.md`, `MLOPS_MULTI_MODEL_ENDPOINT.md`, `MLOPS_CLARIFY_EXPLAINABILITY.md`, `MLOPS_GROUND_TRUTH.md`, `MLOPS_DATA_PLATFORM.md`, `MLOPS_PIPELINE_FRAUD_REALTIME.md`, `MLOPS_PIPELINE_LLM_FINETUNING.md`, `MLOPS_PIPELINE_NLP_HUGGINGFACE.md`, `MLOPS_PIPELINE_RECOMMENDATIONS.md`, `MLOPS_PIPELINE_TIMESERIES.md`, `MLOPS_PIPELINE_COMPUTER_VISION.md`

(13 in this group — adjust the table in your execution log.)

### Group D — Compliance & Data platforms (3 files)
`COMPLIANCE_HIPAA_PCIDSS.md`, `DATA_LAKEHOUSE_ICEBERG.md`, `DATA_MSK_KAFKA.md`

### Group E — Infra variants (3 files)
`GLOBAL_MULTI_REGION.md`, `PLATFORM_EKS_CLUSTER.md`, `aws_managed_mcp.md`

**Total: 36.** (Group A = 11, B = 7, C = 13, D = 3, E = 3 → 37.) Reconcile count against the `ls partials/*.md | wc -l` minus the 17 v2.0 files.

---

## 3. The canonical 8-section SOP structure (REQUIRED)

```markdown
# SOP — <Short Name>

**Version:** 2.0 · **Last-reviewed:** <today> · **Status:** Active
**Applies to:** <language + SDK + services + version constraints>

---

## 1. Purpose
- 3-5 bullets
- What this partial builds, when to include, SOW signals that trigger it

## 2. Decision — Monolith vs Micro-Stack
- A table with 2 rows
- An explanation of WHY the split matters (the failure mode that makes micro-stack code different)
- If truly no split is meaningful (e.g. Strands eval is eval-only, no CDK stack) — write:
  > "This SOP has no architectural split. §3 is the single canonical variant."
  > Then skip to §3 with just one code block.

## 3. Monolith Variant
- Full Python/CDK code, runnable
- §3.X Gotchas subsection

## 4. Micro-Stack Variant
- Full Python/CDK code
- The 5 non-negotiables from LAYER_BACKEND_LAMBDA §4.1 applied:
  1. Asset paths anchored to Path(__file__)
  2. No X.grant_*(role) cross-stack
  3. No targets.SqsQueue(q) cross-stack
  4. No bucket + OAC split across stacks
  5. No encryption_key=ext_key + grant chain cross-stack
- §4.X Gotchas subsection

## 5. Swap matrix
- When to switch variants (3-5 rows)

## 6. Worked example
- Pytest-style executable harness that `cdk synth --no-lookups` verifies

## 7. References
- docs/template_params.md keys cited
- docs/Feature_Roadmap.md feature IDs cited
- Related SOPs by name

## 8. Changelog
| Version | Date | Change |
|---|---|---|
| 2.0 | <today> | Dual-variant SOP rewrite. |
| 1.0 | 2026-03-05 | Initial. |
```

### 3.1 Domain-specific notes

**Strands Agents**: many STRANDS_* partials describe framework usage, not CDK stacks. For these, §2 should read "no architectural split" and §3 is the canonical variant showing Strands agent code + deployment (Lambda or ECS).

**AGENTCORE_**: Bedrock AgentCore Runtime is an AWS-hosted service. §3 shows the CDK `bedrock.CfnAgent` or AgentCore Runtime config. §4 covers split when the agent + its action-group Lambdas are in separate stacks (same cross-stack grant considerations as LLMOPS_BEDROCK).

**MLOPS_SAGEMAKER_***: SageMaker pipelines, endpoints, training jobs. Cross-stack risk is lower (SageMaker resources are mostly self-contained), but S3 model-artifact buckets accessed from multiple stacks = same `bucket.grant_*` identity-side pattern.

**COMPLIANCE_HIPAA_PCIDSS**: this is policy + audit config, not new CDK primitives. §2 = "no architectural split". §3 lists the controls (Config rules, CloudTrail settings, required tags, BAA-ready services) and how each plugs into existing stacks.

**DATA_LAKEHOUSE_ICEBERG, DATA_MSK_KAFKA, GLOBAL_MULTI_REGION, PLATFORM_EKS_CLUSTER**: treat each as a fresh CDK stack. Use `LAYER_DATA`, `LAYER_NETWORKING`, `LAYER_BACKEND_ECS` as references.

**aws_managed_mcp**: AWS Managed MCP server. Small partial — likely just a reference doc + a Lambda recipe. Consider whether it even needs §4 (probably not).

---

## 4. Process (ordered)

### Step 0 — Baseline

```
cd E:\F369_CICD_Template\prompt_templates\partials
# Verify the 17 v2.0 exemplars exist and have v2.0 header
grep -l "Version.*2.0.*Last-reviewed.*2026-04-21" *.md | wc -l   # Expect >= 17
# Verify backup exists
ls ..\partials_backup_2026-04-21\ | wc -l                          # Expect 54
```

If either check fails, stop. Don't build on a broken baseline.

### Step 1 — For each partial in §2, in order:

```
1. READ the original at partials_backup_2026-04-21/<name>.md
2. IDENTIFY: does this partial describe CDK infra (→ dual-variant) or framework usage (→ single-variant, skip §4)?
3. VERIFY every CDK/SDK class + method referenced in the original via cdk-mcp-server
4. REWRITE to the 8-section structure, preserving domain content but fixing:
   - Deprecated APIs
   - Cross-stack anti-patterns (if dual-variant)
   - Missing cross-references
   - Vague code
5. WRITE the new version to partials/<name>.md (overwrites v1.0)
6. SYNTH-TEST the code example in §6 if present (extract to temp dir, run cdk synth --no-lookups)
7. APPEND one row to docs/build_log_partials_v2.md at E:\F369_CICD_Template\
```

### Step 2 — Execution log format

Append one row per partial:

```markdown
| UTC Timestamp | # | Group | Partial | Lines before | Lines after | Dual variant? | Synth test | Status |
|---------------|---|-------|---------|--------------|-------------|---------------|------------|--------|
| 2026-04-21T23:00Z | 1/37 | A Strands | STRANDS_AGENT_CORE | 210 | 285 | NO (framework) | N/A | PASS |
| ... |
```

On failure:
- `FAIL_RETRY` → attempt 1 fix → re-log
- Still red → `BLOCKED` → stop the run, report

### Step 3 — Completion

When all 37 are processed, print:

```
===================================================================
PARTIALS v2.0 BUILD (Opus 4.6) — COMPLETE
  Partials rewritten:      <N> / 37
  Dual-variant:            <D>
  Single-variant (framework): <S>
  Skipped (BLOCKED):       <B>
  Synth tests run:         <M>
  Synth tests passed:      <P> / <M>
  Execution log:           E:\F369_CICD_Template\docs\build_log_partials_v2.md
  AWS API calls made:      0
  Next step:               run audit_partials_v2.md with Opus 4.7
===================================================================
```

---

## 5. Quality gates (per partial, before overwriting)

- [ ] 8 sections present (§1–§8) or explicit justification (framework-only single-variant)
- [ ] Version/date front-matter exact format
- [ ] All CDK APIs cross-checked
- [ ] Worked example (§6) either compiles via `cdk synth` OR is clearly labelled as a policy/config doc with no code
- [ ] Cross-refs (Related SOPs in §7) all exist in the partials folder
- [ ] File ends with Changelog row for v2.0

---

## 6. Failure modes & recovery

| Symptom | Action |
|---|---|
| Original partial is < 50 lines, mostly placeholder | Mark as "stub" in the new version; §1 + §2 + a single-variant §3 with `# TODO(Phase 3): expand`. Log as `STUB`, not `BLOCKED`. |
| CDK docs return no match for a class name in the original | Flag in execution log; write `TODO(verify): <class>` comment at the call site. Do not invent a working signature. |
| Synth test fails | Attempt 1 fix. Still fails → mark section as `# TODO(fix): <error>` and continue. Do not spend >10 min per partial on synth-fixing. |
| Run out of context / token budget | Stop, log `CONTEXT_EXHAUSTED` with the list of un-processed partials, exit cleanly. |

---

## 7. What you must NOT do

- Do NOT modify any of the 17 v2.0 exemplars.
- Do NOT touch `partials_backup_2026-04-21/`.
- Do NOT invent new services or features not in the original partial.
- Do NOT call AWS APIs.
- Do NOT `git commit` or `git push` — this is a local build.
- Do NOT skip the cross-reference check in §5.
- Do NOT produce "compressed" SOPs that drop §4 without justifying it. If you write "framework-only, single variant", state that explicitly in §2.

---

## 8. After you finish

The next step (human-operated) is to run the audit prompt:

```
E:\NBS_Research_America\docs\prompts\audit_partials_v2.md
```

with Opus 4.7 — but this time expanded to audit all 54 partials instead of the original 17. The audit prompt's §2 scope will need to be updated by the human before running.

Good luck. Be rigorous. Flag uncertainty instead of guessing.
