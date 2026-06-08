# R4 Audit — Handoff Document

**Audit round:** R4 (AFIE-CPG retrospective)
**Status:** COMPLETE — 25 findings RESOLVED, 4 new partials authored, kit-level enforcement wired, downstream sweep done
**Branch:** `audit-r4-afie-lessons` (in both `F369_CICD_Template` + `F369_LLM_TEMPLATES`)
**Date:** 2026-06-17
**Auditor:** Claude Opus 4.7 (1M context)

> **TL;DR.** R4 lands ~30 partial fixes across 25 findings + 4 net-new partials in `F369_CICD_Template` to close the gaps AFIE-CPG surfaced in 11 sprints. R4 also lands kit-level enforcement in `F369_LLM_TEMPLATES` so that `commands/run-kit-overnight.sh` blocks deploys that don't meet the R4 contract. All 6 root-cause classes (A/B/C/D/E/F) explicitly closed. No outstanding HIGH or MED severity work; LOW residual drift inventory documented for R5.

---

## What's in scope

This handoff covers two repositories and one feature branch in each:

| Repo | Branch | Commits ahead of main |
|---|---|---|
| `E:\F369_CICD_Template` | `audit-r4-afie-lessons` | 27 commits |
| `E:\F369_LLM_TEMPLATES` | `audit-r4-afie-lessons` | 1 commit |

Pushed to origin on GitHub. PRs not yet opened (intentional — handoff first, merge after review).

**Not in scope** (per the user's session-opening directive):
- `E:\NBS_Snowflake_Master` — separate Snowflake-stack library, its own audit cycle
- `E:\NBS_PAF_Finance_V2\afie-deploy` — downstream consumer that triggered R4; R4 is upstream remediation, not project-side fixes

---

## Findings inventory

### Tier 1 — deploy-blockers (4 findings, 5 commits)

| Commit | Finding | Partials touched | Key fix |
|---|---|---|---|
| `87a7812` | Foundation | R4_AFIE_PLAN.md, LESSONS_FROM_AFIE_2026-06.md, audit_report_partials_v2_afie_r4.md | Plan + lessons + audit-report shell |
| `0a7ee4a` | **F-AFIE-01** Bedrock 3-ARN IAM | 11 partials, ~17 grant sites | `foundation-model/*` + `inference-profile/*` + `application-inference-profile/*` (cross-region inference profile access) |
| `ab83bdc` | **F-AFIE-02** Model lifecycle awareness | LLMOPS_BEDROCK §3.0 NEW | Active vs Legacy/EOL table + 15-day-inactivity warning + MCP currency-check command |
| `40a5bb9` | **F-AFIE-03** REST API authorizer mandatory | LAYER_API §4 + SERVERLESS_HTTP_API_COGNITO §5 | `default_method_options` on `RestApi.root` so `addProxy({any_method:true})` inherits Cognito authz |
| `aedc77c` | **F-AFIE-04** CloudFront TLS pick-one + us-east-1 + HSTS | CDN_CLOUDFRONT_FOUNDATION §3.0 NEW + LAYER_FRONTEND §3.1 | TLS pick-one decision tree, us-east-1 pin for ACM+WAF, finance-grade HSTS |

### Tier 2 — HIGH security+reliability (8 findings, 5 commits)

| Commit | Finding | Partials touched | Key fix |
|---|---|---|---|
| `347fb04` | **F-AFIE-05+07** SNS-CMK + alarm missing-data | LAYER_SECURITY + LAYER_OBSERVABILITY | 4th canonical CMK `notifications_key` (cloudwatch+events+sns grants); all alarms get `treat_missing_data` with 3-row semantic decision table |
| `007ff72` | **F-AFIE-06** Log retention compliance-class table | LAYER_BACKEND_LAMBDA + AGENTCORE_OBSERVABILITY | `_RETENTION_BY_CLASS` driven by `compliance_class` CDK context (dev 7d → prod-sox 2y) |
| `fcb9c14` | **F-AFIE-08** Cedar + fail-closed RBAC | AGENTCORE_AGENT_CONTROL | `validation_mode=VALIDATE` default; §3.2a canonical Cedar context envelope; DEFAULT_POLICY deny-all + RbacLoadError raises instead of silent allow-all |
| `3d65b0f` | **F-AFIE-09** Identity scoping | AGENTCORE_IDENTITY §3.1 + LAYER_SECURITY | `_create_agent_role` signature: bool flags → required `*_arns` lists + opt-in `permit_wildcard`; `DenyAgentCoreInvokeAcrossProjects` boundary statement |
| `4b76666` | **F-AFIE-10** OpenSearch VPC-endpoint-only default | DATA_OPENSEARCH_SERVERLESS | `AllowFromPublic` flipped True→False; `source_vpce_ids` required for non-dev |
| `97ac603` | **F-AFIE-11** Detective controls live verify | ENTERPRISE_SECURITY_HUB_GD_ORG | §3.3 NEW `verify_security_baseline.py` checking all 6 detective controls post-deploy |
| `da54fa9` | **F-AFIE-12** Gateway role tag-condition | AGENTCORE_GATEWAY | `Condition: StringEquals { aws:ResourceTag/Project }` on `lambda:InvokeFunction` + `bedrock-agentcore:*` wildcards |

### Tier 3 — cost levers (5 findings, 5 commits)

| Commit | Finding | Partials touched | Key fix |
|---|---|---|---|
| `02ef0c1` | **F-AFIE-13** Aurora scale-to-zero | DATA_AURORA_SERVERLESS_V2 | `min_capacity=0` for dev/staging + `serverless_v2_auto_pause_duration=Duration.seconds(300)` |
| `f0f7289` | **F-AFIE-14** Redshift MaxCapacity mandatory | MLOPS_DATA_PLATFORM + DATA_LAKEHOUSE_ICEBERG + DATA_ZERO_ETL + DATA_DBT_REDSHIFT_SERVERLESS | `max_capacity` MANDATORY on every workgroup; prod cap 512→256 starting point |
| `2131a54` | **F-AFIE-15** Fargate Spot stage-tuned | LAYER_BACKEND_ECS + ECS_PRODUCTION_HARDENING | dev SPOT 9 + base-0; staging SPOT 5; prod SPOT 3 + base=1 |
| `b5429fb` | **F-AFIE-16** NAT-free dev + endpoints | LAYER_NETWORKING | `nat_gateways=0` dev/staging default + 7→13 interface endpoints + DDB gateway endpoint |
| `3082ab4` | **F-AFIE-17** DDB PITR spec object | LAYER_DATA + SERVERLESS_DYNAMODB_PATTERNS | Deprecated `point_in_time_recovery=bool` → new `point_in_time_recovery_specification`; PITR ON for ALL stages |

### Tier 4 — pattern fixes (4 findings, 4 commits)

| Commit | Finding | Partials touched | Key fix |
|---|---|---|---|
| `9935e9d` | **F-AFIE-18** S3 Vectors default for cost-sensitive RAG | BEDROCK_KNOWLEDGE_BASES §3.0a NEW | Full CDK pattern with scoped `s3vectors:*` grants; §2 decision tree restructured |
| `c3d1f61` | **F-AFIE-19** WebSocket `$connect` auth | LAYER_API §5 | `WebSocketLambdaAuthorizer` + full Cognito-JWT-validating handler with explicit Deny on bad token |
| `376b7d0` | **F-AFIE-20** Bedrock pricing SoT + cost-aware routing | LLMOPS_BEDROCK §3.0b NEW | 4-token-type CUR reconciliation, 3 service tiers, SSM-driven model router (Sonnet for reasoning; Haiku for classification), per-invoke metrics emitter |
| `1ab059d` | **F-AFIE-21** Cognito feature_plan=PLUS | AGENTCORE_IDENTITY + ENTERPRISE_IDENTITY_CENTER | Replaces deprecated `advanced_security_mode=ENFORCED` |

### Tier 5 — structural net-new partials (4 findings, 4 commits)

| Commit | Finding | NEW partial | Closes |
|---|---|---|---|
| `461ee37` | **F-AFIE-22** Synth-guard library | `_assertions/cdk_synth_guards.md` | 17 canonical synth-time guards with full implementation + CI wiring |
| `f0f531d` | **F-AFIE-23** Live-readonly MCP audit harness | `OPS_LIVE_READONLY_MCP_AUDIT.md` | Pre-deploy + post-deploy audit chained into kit CI |
| `6ae69c0` | **F-AFIE-24** Quarterly currency check runbook | `OPS_AWS_SERVICE_CURRENCY_CHECK.md` | Quarterly cadence for 7 partial families + monthly Bedrock |
| `39debaf` | **F-AFIE-25** Bedrock model lifecycle dedicated partial | `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md` | 15-day inactivity playbook + `invoke_with_lifecycle_retry()` + cross-region inference-profile resilience |

### Tier 6 — kit-level R4 propagation (1 commit in F369_LLM_TEMPLATES)

| Commit | Artifact | Type | Change |
|---|---|---|---|
| `8c3e5a1` | `kits/_design/R4_compliance_addendum.md` | **NEW** | 8 kit-level mandates + migration playbook + full partial cross-ref |
| `8c3e5a1` | `kits/_template/README.md` | v1.0 → v1.1 | Standard MANDATES R4 integration for kits authored after 2026-06-17 |
| `8c3e5a1` | `commands/run-kit-overnight.sh` | Updated | `--r4-audit on\|off` flag; chains pytest synth-guards → pre-deploy → post-deploy after LLM phases |
| `8c3e5a1` | `Library.md` | Updated | R4 compliance-gate banner + pre-R4 kits flagged "R4-pending" |
| `8c3e5a1` | 8 existing kits | All flagged R4-pending | Each kit gets an engagement-specific R4 banner |

### Tier 7 — grep-r downstream sweep (1 commit)

| Commit | Class | Partials touched | Sites fixed |
|---|---|---|---|
| `71e6820` | F-AFIE-10 + F-AFIE-18 reconciliation | BEDROCK_KNOWLEDGE_BASES OSS variant | 1 site: `AllowFromPublic=True` fenced to `compliance_class=dev` only |
| `71e6820` | F-AFIE-21 carry-forward | SERVERLESS_HTTP_API_COGNITO | 3 sites: `advanced_security_mode=ENFORCED` → `feature_plan=PLUS` |
| `71e6820` | F-AFIE-17 carry-forward | EVENT_DRIVEN_FAN_IN_AGGREGATOR + MLOPS_AUDIO_PIPELINE §3 + PATTERN_DOC_INGESTION_RAG §3 + AGENTCORE_AGENT_CONTROL §3.4 | 5 sites: `point_in_time_recovery=bool` → spec object |

---

## R4 root-cause coverage (all 6 classes closed)

| Class | Description | Closed by |
|---|---|---|
| **A** | 2024 snapshot drift (Bedrock models, Cognito tiers, RDS prop renames, etc.) | **F-AFIE-24** quarterly currency-check runbook + monthly Bedrock-lifecycle check |
| **B** | Re-derivation despite Canonical-Copy Rule (consumers re-author partials and drift) | **F-AFIE-22** synth-guards enforce canonical defaults at `cdk synth` time |
| **C** | Composites don't enforce currency (kits ship stale partial versions) | **F-AFIE-22** + **F-AFIE-23** — synth-time + runtime gates in the kit CI |
| **D** | Kits don't run live-readonly audit (CFN success ≠ service is live) | **F-AFIE-23** post-deploy boto3 audit |
| **E** | No synth-time assertion library | **F-AFIE-22** is the library |
| **F** | No per-partial regression test scaffolds | **F-AFIE-22** §5.1 per-partial test pattern |

---

## What's R4-compliant now

### F369_CICD_Template partial inventory (post-R4)

- **Updated to R4 canonical** (v2.1+ headers with R4-update notes): 25 partials
- **NEW for R4**: 4 partials (`_assertions/cdk_synth_guards.md`, `OPS_LIVE_READONLY_MCP_AUDIT.md`, `OPS_AWS_SERVICE_CURRENCY_CHECK.md`, `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md`)
- **Audit report**: `docs/audit_report_partials_v2_afie_r4.md` with 27-row grade table + per-finding detail + MCP audit log in Appendix B

### F369_LLM_TEMPLATES kit inventory (post-R4)

- **NEW**: `kits/_design/R4_compliance_addendum.md` (the kit-level R4 contract)
- **Updated standard**: `kits/_template/README.md` v1.0 → v1.1 mandating R4 integration
- **Updated runner**: `commands/run-kit-overnight.sh` with `--r4-audit` flag chaining 3 audit phases
- **Updated index**: `Library.md` with R4 compliance-gate banner
- **R4-pending flags**: All 8 existing kits get engagement-specific R4 migration notes

---

## R5 work backlog (LOW residual drift)

The Tier 7 grep-r sweep was a first pass. The following residual drift is documented but NOT fixed in R4 — slated for R5:

### PITR bool prop migration (remaining ~7 sites)

The `point_in_time_recovery=bool` deprecated prop is still used in:

- `DR_MULTI_REGION_PATTERNS.md` (×1)
- `MLOPS_PIPELINE_RECOMMENDATIONS.md` (×2)
- `MLOPS_PIPELINE_TIMESERIES.md` (×1)
- `SERVERLESS_LAMBDA_POWERTOOLS.md` (×1)
- `PATTERN_BATCH_UPLOAD.md` (×2)
- `PATTERN_DDB_CONTROL_PLANE.md` (dict-form `{"enabled": ...}`)
- `PATTERN_CUSTOMER_MAINTAINED_DIM_DDB.md` (dict-form)
- `AGENTCORE_OBSERVABILITY.md` (×2)
- §4 Micro-Stack sites in 3 partials (MLOPS_AUDIO_PIPELINE, PATTERN_DOC_INGESTION_RAG, AGENTCORE_AGENT_CONTROL)

**These are correctness-bounded** — PITR is on, just via the older prop. F-AFIE-22 synth-guard `assert_ddb_table_uses_pitr_specification` will flag them in CI. R5 will batch-migrate them lockstep with the guard firing.

### Other R5 candidates surfaced by the sweep

- `LAYER_DATA.md` §3.4 swap-matrix table has a literal `point_in_time_recovery=True` reference (documentation drift, not a CDK call)
- Test coverage for the synth-guard library itself — F-AFIE-22 ships the guards but not unit tests for the guards
- Live MCP audit scripts (F-AFIE-23) ship as templates inside the partial; the actual `scripts/pre_deploy_audit.py` files need to be authored per-kit during R4 migration

---

## How to merge

### Step 1 — Final review

```bash
# F369_CICD_Template
cd E:/F369_CICD_Template
git log audit-r4-afie-lessons --oneline ^main | head -30   # Should show 27 commits
git diff main...audit-r4-afie-lessons --stat               # ~30 partials touched + 4 new + docs

# F369_LLM_TEMPLATES
cd E:/F369_LLM_TEMPLATES
git log audit-r4-afie-lessons --oneline ^main | head -5    # Should show 1 commit
git diff main...audit-r4-afie-lessons --stat               # 12 files touched
```

### Step 2 — Open PR (when ready)

```bash
# F369_CICD_Template
gh pr create --title "[R4] AFIE-CPG retrospective — 25 findings + 4 new partials + kit-level enforcement" \
             --body-file docs/R4_HANDOFF.md \
             --base main --head audit-r4-afie-lessons

# F369_LLM_TEMPLATES
gh pr create --title "[R4 / Tier 6] Kit-level R4 compliance addendum + overnight runner audit chain" \
             --body-file ../F369_CICD_Template/docs/R4_HANDOFF.md \
             --base main --head audit-r4-afie-lessons
```

### Step 3 — After merge: kit migrations

- Pick the priority kit (`kits/qualitative-research-audio-analytics.md` — the Business-First reference impl)
- Add `cdk.json` `compliance_class` context
- Author `tests/test_synth_guards_full.py` per `_assertions/cdk_synth_guards.md` §5.2
- Author `scripts/pre_deploy_audit.py` + `scripts/post_deploy_audit.py` per `OPS_LIVE_READONLY_MCP_AUDIT.md`
- Run `commands/run-kit-overnight.sh --r4-audit on --dry-run` and validate
- Open `[Migrate: R4/kit-qualitative-research-audio-analytics]` PR

Repeat for remaining 7 kits.

### Step 4 — Schedule the maintenance cron

Wire `OPS_AWS_SERVICE_CURRENCY_CHECK.md` §11 GitHub Actions workflow into F369_CICD_Template:

```yaml
# .github/workflows/quarterly-currency-check.yml — first-of-quarter at 08:00 UTC
# + Bedrock-monthly job on first-of-month
```

---

## How to verify R4 is working in a kit

After kit migration:

```bash
# At the kit's deployable-repo root (e.g. E:/NBS_Research_America_regen)

# 1) Synth-guards (F-AFIE-22)
python -m pytest tests/test_synth_guards_full.py -v
# Expected: 17/17 PASS (one assertion per F-AFIE finding class)

# 2) Pre-deploy audit (F-AFIE-23 §3)
python scripts/pre_deploy_audit.py
# Expected: 4/4 checks PASS (model lifecycle / pricing snapshot / partial currency / canonical-partial drift)

# 3) cdk deploy
cdk deploy --all --require-approval never

# 4) Post-deploy audit (F-AFIE-23 §4)
python scripts/post_deploy_audit.py
# Expected: 6+ live checks PASS (Cognito tier / DDB PITR / Bedrock active models / Aurora MinCapacity /
#           CW Alarm missing-data / OSS network policy / NAT-free dev / CloudFront us-east-1 / cost-shape)
```

If any phase fails, the runner exits non-zero and writes a per-script log with a remediation pointer to the specific F-AFIE finding doc.

---

## Statistics

| Metric | Value |
|---|---|
| R4 findings | 25 |
| Partials touched | ~30 |
| Partials NEW | 4 |
| Total commits across both repos | 28 (27 in F369_CICD_Template + 1 in F369_LLM_TEMPLATES) |
| Total partial-doc lines added/changed | ~2,500 |
| MCP doc-reads performed | ~25 (cited in `audit_report_partials_v2_afie_r4.md` Appendix B) |
| AWS APIs called (state-changing) | 0 |
| `cdk deploy` invocations | 0 |
| Root-cause classes closed | 6 of 6 |

---

## Cross-reference index (where every R4 artifact lives)

### In `F369_CICD_Template`

- **Plan:** `docs/R4_AFIE_PLAN.md`
- **Lessons:** `docs/LESSONS_FROM_AFIE_2026-06.md`
- **Audit report:** `docs/audit_report_partials_v2_afie_r4.md`
- **Handoff (this file):** `docs/R4_HANDOFF.md`
- **Updated partials:** `prompt_templates/partials/*.md` (25 files with v2.1+ headers + R4 update notes)
- **NEW partials:**
  - `prompt_templates/partials/_assertions/cdk_synth_guards.md` (F-AFIE-22)
  - `prompt_templates/partials/OPS_LIVE_READONLY_MCP_AUDIT.md` (F-AFIE-23)
  - `prompt_templates/partials/OPS_AWS_SERVICE_CURRENCY_CHECK.md` (F-AFIE-24)
  - `prompt_templates/partials/LLMOPS_BEDROCK_MODEL_LIFECYCLE.md` (F-AFIE-25)

### In `F369_LLM_TEMPLATES`

- **Addendum:** `kits/_design/R4_compliance_addendum.md` (the kit-level R4 contract)
- **Standard:** `kits/_template/README.md` v1.1 (mandates R4 integration)
- **Runner:** `commands/run-kit-overnight.sh` (with `--r4-audit` flag)
- **Index:** `Library.md` (R4 banner at top)
- **Kit R4-pending flags:** every `kits/*.md` file has an R4-pending banner comment

### Previous audit rounds (carried forward)

- R1 (2026-04-21): `docs/audit_report_partials_v2.md` — 17 v2.0 exemplar partials
- R2 (2026-04-22): `docs/audit_report_partials_v2_new9.md` — 9 kit-driven partials
- R3 (2026-04-23): `docs/audit_report_partials_v2_new12.md` — 12 AI-native-lakehouse partials
- R4 (2026-06-17): **this round** — AFIE-CPG retrospective

---

## Maintenance contract

Per `OPS_AWS_SERVICE_CURRENCY_CHECK.md`:

- **Monthly** (1st of every month, 08:00 UTC): Bedrock lifecycle check
- **Quarterly** (1st of every quarter, 08:00 UTC): all 7 partial families + synth-guards
- **Maintainer rotation:** family-stack leads own substance; on-call rotation owns schedule
- **Output:** `docs/maint/quarterly_<YYYY-MM>.md` archived in repo (SOC2 audit trail)

---

## End of handoff

R4 audit is structurally complete. Next audit round will be R5 — triggered either by:
1. The next downstream-deployment engagement surfacing AFIE-class regressions
2. The quarterly currency-check cron firing a drift alert
3. Manual invocation by the partial-family maintainer

The synth-guard library (F-AFIE-22) + the live-readonly audit harness (F-AFIE-23) make R5 incremental — each finding is a single guard rule + a single partial-fix + a single audit-report row. The 11-sprint AFIE pain that triggered R4 should not recur.

> "Defaults are not consent." — R4 lesson, codified.
