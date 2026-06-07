# R4 — AFIE-Lessons Audit & Library Update Plan

**Audit lead:** Claude Opus 4.7 (1M context)
**Plan date:** 2026-06-16
**Wall-clock budget:** 48 hours
**Scope:** retroactively fold the AFIE-CPG 11-sprint audit-fix-verify cycle's lessons back into the canonical partials + composite templates so the same 100+ gaps don't reappear on the next kit build.
**Source repo:** `E:\F369_CICD_Template\` (this) — partials + audit reports
**Companion repo:** `E:\F369_LLM_TEMPLATES\` — composite templates + kits
**Branch (both repos):** `audit-r4-afie-lessons`
**Source of findings:** `E:\NBS_PAF_Finance_V2\afie-deploy\` — specifically `SPRINT8_FINDINGS_QUEUE.md`, `SPRINT10_FINDINGS_QUEUE.md`, `BAKEOFF_CERTIFICATION.md`, `SPRINT11_HANDOFF.md`
**Format:** R4 follows R1/R2/R3 audit format (`docs/audit_report_partials_v2.md`, `docs/audit_report_partials_v2_new9.md`, `docs/audit_report_partials_v2_new12.md`). Findings ID prefix = `F-AFIE-NN`.

---

## Why this audit exists

AFIE-CPG was the most ambitious kit test the library has run — a 20-stack agentic-finance app built from F369 partials. It failed the "one-shot clean build" criterion: 11 sprints, ~72 findings across two independent audit lenses, ~$1000s in iterative Claude compute to reach `BAKEOFF_CERTIFICATION = clean GO`. The pattern of failure has 6 documented root causes (see `docs/LESSONS_FROM_AFIE_2026-06.md`), and most trace to gaps in canonical partials that copy verbatim into every future project.

The CTO direction (durable in memory): **fix the library, not the project**. R4 lands the AFIE-surfaced gotchas into the partials so the *next* project starts at the bar AFIE reached after 11 sprints.

---

## Hard discipline — every change MCP-audited

Per the user's mandate, every code change in R4 follows this sequence:

```
1. mcp__awslabs_aws-documentation-mcp-server__search_documentation
     query: "<service> <pattern> best practices 2026"
2. mcp__awslabs_aws-documentation-mcp-server__read_documentation
     url: <canonical URL from search>
3. Read the actual file:line in the partial
4. Edit: replace pattern + add inline AWS doc URL comment + add to §References
5. Update partial header: last-reviewed: 2026-06-16
6. For IAM blocks: also mcp__awslabs_aws-iac-mcp-server__cdk_best_practices
7. For security blocks: also mcp__awslabs_well-architected-security-mcp-server (read-only)
8. Commit with [Audit: R4/F-AFIE-NN] tag + AWS doc URL in commit body
9. grep -r for the old pattern across all 143 partials; fix downstream propagation
10. README + Registry update
```

Findings without a fresh MCP citation are flagged as "unsupported"; not merged.

---

## Tier-ordered scope (48 hours)

### Tier 0 — Foundation docs (Hours 0-4)

| Artifact | Path |
|---|---|
| Plan (this doc) | `docs/R4_AFIE_PLAN.md` |
| Lessons + Class A-F root causes | `docs/LESSONS_FROM_AFIE_2026-06.md` |
| R4 audit report shell (to be filled as Tier 1-4 fixes land) | `docs/audit_report_partials_v2_afie_r4.md` |

**Commit cadence:** single commit `add: R4 audit foundation — plan + lessons + report shell`. Pause for user review at Hour 4 checkpoint.

### Tier 1 — Deploy-blocker partials (Hours 4-12)

The 4 AFIE CRITICAL findings (would break production day 1) + the inference-profile blocker Sprint 10 caught:

| # | Partial | AFIE finding | Specific fix |
|---|---|---|---|
| F-AFIE-01 | `AGENTCORE_RUNTIME.md` §3.2, §4 | G-NEW-01 | Add `inference-profile/*` ARN alongside `foundation-model/*` in InvokeModel resources; mirror the correct pattern at `AGENTCORE_MEMORY` §3 if it exists |
| F-AFIE-02 | `LLMOPS_BEDROCK.md` §3 | F-AI-01 | Add **model-lifecycle awareness** subsection: current Active model defaults (Sonnet 4.5, Haiku 4.5, Titan v2, Nova 2 Sonic); link to https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html as mandatory currency-check |
| F-AFIE-03 | `LAYER_API.md` §4 + `SERVERLESS_HTTP_API_COGNITO.md` §3 | F-INT-01 | Make Cognito authorizer attachment **mandatory** on every method — explicit example showing proxy methods + 401 default; CORS = specific origin, not `*` |
| F-AFIE-04 | `CDN_CLOUDFRONT_FOUNDATION.md` §3 + `LAYER_FRONTEND.md` §4 | F-PRT-01/02 + G-NEW-05 | TLS pick-one-path enforcement: synth-time assertion preventing ALB-cert + edge-lockdown both ON; us-east-1 pin for CLOUDFRONT-scope WAF + ACM cert |

**Per-partial commit pattern:**
```
F-AFIE-NN: <one-line fix description> [HIGH|CRITICAL/SECURITY|BUG] (R4)

<2-3 sentence rationale tied to AFIE finding ID>

AWS docs verified via MCP:
- <canonical URL 1>
- <canonical URL 2>

grep -r sweep: <list any other partials updated for same pattern>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

**Checkpoint:** Hour 12 — pause for user review of Tier 1.

### Tier 2 — Security + reliability HIGHs (Hours 12-20)

8 partials addressing AFIE HIGH findings:

| # | Partial | AFIE finding | Specific fix |
|---|---|---|---|
| F-AFIE-05 | `LAYER_OBSERVABILITY.md` + `LAYER_SECURITY.md` | F-OBS-03 | When SNS topic is CMK-encrypted AND used as alarm action: KMS key policy MUST grant `cloudwatch.amazonaws.com` |
| F-AFIE-06 | `LAYER_OBSERVABILITY.md` + `AGENTCORE_OBSERVABILITY.md` | F-OBS-01 | Explicit log-group retention for AgentCore auto-created `/aws/bedrock-agentcore/*` (default 90 days) |
| F-AFIE-07 | `LAYER_OBSERVABILITY.md` | F-OBS-02 | Alarm `missingDataTreatment` = `breaching` for outage detection (was implicit "missing" → INSUFFICIENT_DATA) |
| F-AFIE-08 | `AGENTCORE_AGENT_CONTROL.md` | F-AI-04 + G-NEW-02 | Cedar context envelope (`context.input.*` vs flat); flag IGNORE_ALL_FINDINGS as a trap; add live-verify section |
| F-AFIE-09 | `AGENTCORE_IDENTITY.md` + `LAYER_SECURITY.md` | F-FND-02 | Identity role wildcard `resources:['*']` scoping pattern (account+region ARN templating) |
| F-AFIE-10 | `DATA_OPENSEARCH_SERVERLESS.md` §5 | F-DATA-01 | VPC-endpoint-only canonical default (replace `AllowFromPublic` example) |
| F-AFIE-11 | `ENTERPRISE_SECURITY_HUB_GD_ORG.md` | F-SEC-02 | Verify partial enables Inspector + Access Analyzer + Security Hub + Macie; add as live-deploy step |
| F-AFIE-12 | `AGENTCORE_GATEWAY.md` + `AGENTCORE_RUNTIME.md` | N-DEF-01 | `bedrock-agentcore:InvokeAgentRuntime`/`InvokeGateway` partial scoping (account+region `runtime/*`+`gateway/*`) |

**Checkpoint:** Hour 20 — pause for user review of Tier 2.

### Tier 3 — Cost levers (Hours 20-26)

The 2nd-wave savings (S3 Vectors discovery from Sprint 6 saved $700/mo; Tier 3 stacks more on):

| # | Partial | AFIE finding | Specific fix |
|---|---|---|---|
| F-AFIE-13 | `DATA_AURORA_SERVERLESS_V2.md` §3.2 | F-DATA-02 | Default min ACU 0 (scale-to-zero GA); auto-pause window; wake-latency note |
| F-AFIE-14 | `DATA_DBT_REDSHIFT_SERVERLESS.md` or new `DATA_REDSHIFT_SERVERLESS.md` | F-DATA-06 | Default base 4 RPU for <32TB dev (was implicit 8 RPU) |
| F-AFIE-15 | `ECS_PRODUCTION_HARDENING.md` + `LAYER_BACKEND_ECS.md` | F-CMP-02 | Fargate Spot for dev/non-prod default; document prod tradeoff |
| F-AFIE-16 | `LAYER_NETWORKING.md` | F-FND-04 | Interface endpoints (Bedrock, AgentCore, S3 Vectors) over single zonal NAT; NAT $/GB note |
| F-AFIE-17 | `LAYER_DATA.md` + `SERVERLESS_DYNAMODB_PATTERNS.md` | F-DATA-03 + G-NEW-04 | PITR on by default for audit/approval tables; use `pointInTimeRecoverySpecification` spec object (boolean is deprecated) |

**Checkpoint:** Hour 26 — pause for user review of Tier 3.

### Tier 4 — Pattern fixes (Hours 26-30)

| # | Partial | AFIE finding | Specific fix |
|---|---|---|---|
| F-AFIE-18 | `BEDROCK_KNOWLEDGE_BASES.md` §3 | (Sprint 6 lesson) | Add S3 Vectors as first-class backend; decision tree: low-QPS + no-hybrid-needed → S3 Vectors |
| F-AFIE-19 | `LAYER_API.md` §4 (WebSocket section) | F-INT-03 | $connect JWT authorizer mandatory; reject client-supplied role; derive from JWT |
| F-AFIE-20 | `LLMOPS_BEDROCK.md` (new MODEL_PRICING block) | G-NEW-03 | Single source of truth for token pricing (refs maintained quarterly per `OPS_AWS_SERVICE_CURRENCY_CHECK`) |
| F-AFIE-21 | `ENTERPRISE_IDENTITY_CENTER.md` or new Cognito plan section | F-FND-03 | Cognito Plus plan + advanced security on by default; small cost note |
| F-AFIE-22 | `_assertions/cdk_synth_guards.md` (new pattern library) | (Sprint 11 G-NEW-05 pattern) | TLS pick-one-path synth assertion, no-legacy-model-literal assertion, scoped-resource assertion |

### Tier 5 — Net-new partials (Hours 30-36)

| # | Partial | Why new | Scope |
|---|---|---|---|
| F-AFIE-23 | `OPS_LIVE_READONLY_MCP_AUDIT.md` | Sprint 10 discovery — Well-Architected Security MCP works against live account read-only | Mandatory pre-deploy step: enumerate detective controls, find OS endpoint exposure, IAM Access Analyzer findings |
| F-AFIE-24 | `OPS_AWS_SERVICE_CURRENCY_CHECK.md` | No current partial codifies the "verify AWS service hasn't moved" discipline | Quarterly runbook + per-service MCP queries; tracks Bedrock model lifecycle, GA changes, deprecation notices |
| F-AFIE-25 | `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md` | Split from `LLMOPS_BEDROCK.md` — model deprecation deserves dedicated section | Current Active models, lifecycle dates, EOL roadmap, inference-profile cross-region pattern; weekly checkable |

### Tier 6 — Composite templates + kits (Hours 36-42)

Affected composite templates in `F369_LLM_TEMPLATES/` (informed by AFIE-touched primitives):

| Folder | Files (likely) |
|---|---|
| `mlops/` | `03_llm_inference_deployment.md`, `04_rag_pipeline.md`, `12_bedrock_guardrails_agents.md`, `14_bedrock_agents_action_groups.md`, `22_strands_agentcore_deployment.md`, `22b_agentcore_runtime_custom_resource.md` |
| `devops/` | `04_iam_roles_policies_mlops.md`, `12_bedrock_invocation_logging.md`, `15_strands_agent_observability.md`, `16_agent_guardrails_control.md` |
| `backend/` | `01_serverless_api_starter.md`, `06_cloudfront_global_edge_app.md` |
| `enterprise/` | `12_cognito_group_claim_authz_jwt.md`, `10_centralized_security_ops.md` |
| `finops/` | `04_inference_cost_optimization.md`, `01_cost_allocation_ml.md` |
| `data/` | `06_operational_db_to_lakehouse.md`, `08_resilient_db_dr.md` |
| `iac/` | `02_cdk_ml_llm_infrastructure.md`, `04_cdk_ecs_llm_inference.md` |

**Change per composite:** add a `__preflight__` section requiring each consumed partial's `last-reviewed` ≤ 90 days; add references to the new OPS_LIVE_READONLY_MCP_AUDIT + OPS_AWS_SERVICE_CURRENCY_CHECK partials.

**Kits updated:** all 8 existing kits in `F369_LLM_TEMPLATES/kits/` + `kits/_template/` Business-First Kit Standard — add pre-build MCP-audit step to delivery plan.

### Tier 7 — grep -r downstream propagation (Hours 42-46)

For each pattern fix in Tier 1-4, sweep all 143 partials in F369_CICD_Template for the old pattern. Fix every downstream occurrence in the same wave commit. README Registry updated.

### Tier 8 — Verification + push (Hours 46-48)

- Self-test: re-scaffold a small kit (likely `kits/deep-research-agent.md`) against updated partials, dry-synth, compare to AFIE's known-broken pre-Sprint-1 state
- Write `R4_HANDOFF.md` in both repos summarizing what changed
- Update `prompt_templates/partials/README.md` audit-status column + Registry tags
- Update `F369_LLM_TEMPLATES/Library.md` count + composite changelog
- Atomic push BOTH repos at the same time

---

## Checkpoints (when user reviews)

| Hour | What's reviewable | Time needed |
|---|---|---|
| **4** | Plan (this doc) + LESSONS_FROM_AFIE + R4 audit report shell | ~30 min |
| **12** | Tier 1 deploy-blocker partials (5 files updated) | ~30 min |
| **20** | Tier 2 security+reliability HIGHs (8 files) | ~30 min |
| **30** | Tier 1-5 complete (F369_CICD_Template work substantially done) | ~45 min |
| **46** | Both repos done — final state review before push | ~30 min |

Total user time across 48 hours: ~3 hours at 5 checkpoints.

---

## Out of scope for R4

- Touching `NBS_Snowflake_Master` (separate library, per user direction)
- Touching `NBS_PAF_Finance_V2/afie-deploy` (AFIE is the test; library is the fix target)
- Authoring new kits from scratch (only updating the 8 existing)
- Net-new partials beyond the 3 in Tier 5 (TLS-synth-assertion-library deferred to R5)
- Running any AWS state-changing commands (read-only MCP + WebFetch only)
- Snowflake anything (per direction)

---

## Commit conventions

Per F369_CICD_Template `.claude/rules/wave-commits.md` + CLAUDE.md:
- HEREDOC multi-line commit messages
- Each commit ends with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
- Findings tagged `[Audit: R4/F-AFIE-NN]`
- Tier-grouped where natural (e.g., F-AFIE-05 and F-AFIE-06 both touch `LAYER_OBSERVABILITY.md` so one commit covers both)

---

## Expected output by Hour 48

- **F369_CICD_Template:** ~25 partial files edited + 3 net-new partials + 3 root docs (this plan, lessons, R4 audit report) + README registry/audit-status updates. Estimated ~3,000-4,000 insertions / ~500-800 deletions across ~35-45 commits.
- **F369_LLM_TEMPLATES:** ~20 composites + 8 kits + `_template/` updated + `Library.md`. Estimated ~1,500-2,500 insertions / ~200-400 deletions across ~25-30 commits.
- **Both branches pushed** to origin's `audit-r4-afie-lessons` for PR creation.
- **R4_HANDOFF.md** in both repos summarizing scope + verification + next-engagement guidance.
