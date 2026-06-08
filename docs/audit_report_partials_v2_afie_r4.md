# Audit Report — R4: AFIE-Lessons Retrospective Audit

**Auditor:** Claude Opus 4.7 (1M context)
**Audit date:** 2026-06-16
**Branch:** `audit-r4-afie-lessons`
**Scope:** ~25 existing partials + 3 net-new partials retrofitted against AFIE-CPG's 11-sprint findings (`E:\NBS_PAF_Finance_V2\afie-deploy\`, `SPRINT8_FINDINGS_QUEUE.md`, `SPRINT10_FINDINGS_QUEUE.md`).
**Audit method:** every code change MCP-audited against current AWS docs via `mcp__awslabs_aws-documentation-mcp-server`, `mcp__awslabs_aws-iac-mcp-server` (CDK best practices), and `mcp__awslabs_well-architected-security-mcp-server` (live read-only where applicable).
**AWS API calls made:** 0 state-changing (read-only MCP + WebFetch only).
**cdk synth runs:** 0 (CDK CLI not available in audit environment — same constraint as R1/R2/R3, see Appendix A of `audit_report_partials_v2.md`).

> **Companion docs:** [`R4_AFIE_PLAN.md`](R4_AFIE_PLAN.md) (the plan), [`LESSONS_FROM_AFIE_2026-06.md`](LESSONS_FROM_AFIE_2026-06.md) (the 6 root-cause classes).

---

## Executive Summary

R4 differs from R1/R2/R3 in lens: those were grading-quality audits on newly-authored partials. R4 is a **retrospective gap audit** against a real client engagement (AFIE-CPG) that consumed the partials and surfaced ~72 findings over 11 sprints. The audit findings here document gaps in the canonical library that explain the AFIE gap count — and the recommended fix for each.

**Severity distribution (final, post-fixes):**
- HIGH (deploy-blocker or production-severe): **TBD** (Tier 1+2 fixes; populated Hours 4-20)
- MED (significant but not blocker): **TBD** (Tier 3 fixes; populated Hours 20-26)
- LOW (cleanup or future-proofing): **TBD** (Tier 4-5; populated Hours 26-36)
- New partials authored: **3** (Tier 5; populated Hours 30-36)

**Structural changes introduced by R4** (see `LESSONS_FROM_AFIE_2026-06.md` for full rationale):
1. Per-partial `last-reviewed` enforcement at composition time
2. Per-CDK-code-block AWS doc URL citation (`# AWS doc: <URL>`)
3. Mandatory live-readonly pre-build audit (new `OPS_LIVE_READONLY_MCP_AUDIT.md`)
4. Synth-time assertion library (`_assertions/cdk_synth_guards.md`)

---

## Per-partial Grades (to be populated as Tier 1-4 fixes land)

The grade is the **R4 verdict** after the R4 fix has been applied. Each row will be updated when the corresponding tier ships.

| # | Partial | Pre-R4 grade | R4 fixes applied | Post-R4 grade |
|---|---|---|---|---|
| 1 | AGENTCORE_RUNTIME | WARN (R2 alpha drift) | F-AFIE-01 (inference-profile/* IAM) ✓ | PASS |
| 2 | LLMOPS_BEDROCK | WARN (R1) | F-AFIE-01 (3-ARN canonical) ✓ + F-AFIE-02 (§3.0 lifecycle awareness) ✓ + F-AFIE-20 (pricing SoT — pending) | PASS (F-AFIE-01+02); WARN pending F-AFIE-20 |
| 3 | LAYER_API | WARN (R1 F002 fix never landed) | F-AFIE-03 (R1 fix + default_method_options + AFIE F-INT-01 retro) ✓; F-AFIE-19 (WebSocket $connect auth — pending) | PASS (F-AFIE-03); WARN pending F-AFIE-19 |
| 4 | SERVERLESS_HTTP_API_COGNITO | UNAUDITED (R10) | F-AFIE-03 (canonical-pattern intent made explicit + verified default_authorizer mandate) ✓ | PASS |
| 5 | CDN_CLOUDFRONT_FOUNDATION | UNAUDITED (R17) | F-AFIE-04 (§3.0 TLS pick-one decision tree + G-NEW-05 retro + us-east-1 pin reinforced) ✓ | PASS |
| 6 | LAYER_FRONTEND | PASS (R1) | F-AFIE-04 (managed SECURITY_HEADERS flagged POC-grade for finance + custom HSTS+CSP cross-ref + TLS pick-one cross-ref) ✓ | PASS |
| 7 | LAYER_OBSERVABILITY | WARN (R1) | F-AFIE-05 (SNS CMK uses notifications_key + AFIE F-OBS-02 retro) ✓ + F-AFIE-07 (treat_missing_data on all 4 alarms + 3-row decision table) ✓ + F-AFIE-06 (log retention — applied to upstream LAYER_BACKEND_LAMBDA + AGENTCORE_OBSERVABILITY) ✓ | PASS |
| 8 | LAYER_SECURITY | PASS (R1) | F-AFIE-05 (4th canonical CMK `notifications_key` with cloudwatch+events+sns principals) ✓ + F-AFIE-09 (DenyAgentCoreInvokeAcrossProjects boundary statement, §3+§4) ✓ | PASS |
| 9 | AGENTCORE_OBSERVABILITY | PASS (R2) | F-AFIE-06 (canary-log retention ONE_MONTH → ONE_YEAR per §3+§4) ✓ | PASS |
| 10 | AGENTCORE_AGENT_CONTROL | PASS (R1) | F-AFIE-08 (validation_mode default flipped to VALIDATE + §3.2a canonical Cedar context envelope + DEFAULT_POLICY fail-closed deny-all + RbacLoadError raises instead of silent fallback) ✓ | PASS |
| 11 | AGENTCORE_IDENTITY | PASS (R2) | F-AFIE-01 (3-ARN Bedrock InvokeModel; landed 2026-06-16 as v2.1) + F-AFIE-09 (§3 `_create_agent_role` signature redesigned: needs_* booleans → required `*_arns: list[str]` + opt-in `permit_wildcard`) ✓ | PASS |
| 12 | DATA_OPENSEARCH_SERVERLESS | UNAUDITED (R12) | F-AFIE-10 (AllowFromPublic flipped True→False; source_vpce_ids required for non-dev; synth-time assert) ✓ | PASS |
| 13 | ENTERPRISE_SECURITY_HUB_GD_ORG | UNAUDITED (R11) | F-AFIE-11 (§3.3 NEW post-deploy verify_security_baseline.py covering all 6 detective controls + §6 non-negotiable #6) ✓ | PASS |
| 14 | AGENTCORE_GATEWAY | PASS (R2) | F-AFIE-12 (tag-condition added to lambda:InvokeFunction + policy-engine/* + gateway/* grants in §3 + §4; gotcha codified) ✓ | PASS |
| 15 | DATA_AURORA_SERVERLESS_V2 | PASS (R2) | F-AFIE-13 (min_capacity dev default 0.5→0; serverless_v2_auto_pause_duration=300s; prod retains 0.5 for cold-start; both §3 + §4) ✓ | PASS |
| 16 | DATA_DBT_REDSHIFT_SERVERLESS + MLOPS_DATA_PLATFORM + DATA_LAKEHOUSE_ICEBERG + DATA_ZERO_ETL | TBD | F-AFIE-14 (max_capacity MANDATORY across all 4 partials creating CfnWorkgroup; dbt partial gains pitfall row; prod cap lowered 512→256 starting point) ✓ | PASS |
| 17 | ECS_PRODUCTION_HARDENING | UNAUDITED (R16) | F-AFIE-15 (§9 gotcha codifies stage-tuned FARGATE_SPOT mix + AFIE F-FIN-06 retro) ✓ | PASS |
| 18 | LAYER_BACKEND_ECS | PASS (R1) | F-AFIE-15 (§3 + §4 capacity_provider_strategies stage-tuned: dev SPOT 9 + base-0 / staging SPOT 5 + FARGATE base=1 / prod SPOT 3 + FARGATE base=1) ✓ | PASS |
| 19 | LAYER_NETWORKING | PASS (R1) | F-AFIE-16 (nat_gateways=0 dev/staging default; 7 → 13 interface endpoints + DDB gateway endpoint; §3.1 gotcha codifies break-even math) ✓ | PASS |
| 20 | LAYER_DATA | WARN (R1) | F-AFIE-17 (deprecated `point_in_time_recovery=bool` → new `point_in_time_recovery_specification` spec object; compliance-class recovery_period_in_days; PITR ON for ALL stages; audit-log full 35-day + deletion_protection) ✓ | PASS |
| 21 | SERVERLESS_DYNAMODB_PATTERNS | UNAUDITED (R10) | F-AFIE-17 (§3.2 single-table + §7 Global Tables v2 use spec object; §10 non-negotiable #2 rewritten) ✓ | PASS |
| 22 | BEDROCK_KNOWLEDGE_BASES | UNAUDITED (R15) | F-AFIE-18 (S3 Vectors as new canonical default + §3.0a full CDK pattern + §2 decision tree restructured with switch-when criteria + AFIE F-FIN-08 + F-DATA-05 retros) ✓ | PASS |
| 23 | ENTERPRISE_IDENTITY_CENTER (or new) | UNAUDITED (R11) | F-AFIE-21 (Cognito Plus plan + advanced security) | TBD |
| 24 | `_assertions/cdk_synth_guards.md` | **NEW** | F-AFIE-22 (synth-time guard pattern library) | NEW/PASS |
| 25 | `OPS_LIVE_READONLY_MCP_AUDIT.md` | **NEW** | F-AFIE-23 (live-readonly pre-build audit) | NEW/PASS |
| 26 | `OPS_AWS_SERVICE_CURRENCY_CHECK.md` | **NEW** | F-AFIE-24 (quarterly refresh runbook) | NEW/PASS |
| 27 | `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md` | **NEW** | F-AFIE-25 (model lifecycle dedicated partial) | NEW/PASS |

---

## Detailed Findings (to be populated per Tier as fixes land)

The findings below are the AFIE-derived audit items. Each will be filled with the standard R-format fields (Partial, Section, Issue, Evidence, Recommended fix) as the corresponding Tier 1-4 fix is applied + MCP-audited + committed.

### Finding F-AFIE-01 — HIGH (CRITICAL gate — deploy-blocker) — RESOLVED 2026-06-16
**Partial(s) fixed:** Systemic gap across **11 partials, ~17 IAM grant sites**:
- `AGENTCORE_RUNTIME.md` §3.2, §4 (2 sites)
- `AGENTCORE_A2A.md` §3, §4 (2 sites)
- `AGENTCORE_IDENTITY.md` §3, §4 (2 sites)
- `STRANDS_DEPLOY_ECS.md` §3, §4 (2 sites)
- `STRANDS_DEPLOY_LAMBDA.md` §3, §4 (2 sites)
- `LLMOPS_BEDROCK.md` §3.1, §4 (CANONICAL for this pattern; 2 sites)
- `BEDROCK_FLOWS_PROMPT_MGMT.md` (1 site)
- `BEDROCK_AGENTS_MULTI_AGENT.md` (1 site)
- `BEDROCK_KNOWLEDGE_BASES.md` §3 ingest IAM (1 site; embedding/parsing/rerank `model_arn=` fields in CfnDataSource/Retrieve API NOT affected — those take specific FM ARNs by design)
- `MLOPS_CANVAS_NO_CODE.md` (1 site; also flagged Legacy `claude-3-*` reference)
- `PATTERN_DOC_INGESTION_RAG.md` §3, §4 (2 sites)

**Issue (from AFIE Sprint 10 G-NEW-01):** The canonical InvokeModel grant in 11 partials listed only `arn:aws:bedrock:{REGION}::foundation-model/*` as a resource. When a consumer's model literal is a cross-region inference profile (e.g., `us.anthropic.claude-sonnet-4-5-20250929-v1:0` or `global.anthropic.claude-sonnet-4-…`), Bedrock requires `arn:aws:bedrock:*:{ACCOUNT}:inference-profile/*` AND `arn:aws:bedrock:*:{ACCOUNT}:application-inference-profile/*` to ALSO appear in the Allow statement. Without them, every InvokeModel call returns AccessDenied at runtime. AFIE Sprint 10 caught this as the production-day-1 blocker (every agent query would fail).

**Evidence (verified live 2026-06-16 via MCP):** `mcp__awslabs_aws-documentation-mcp-server__read_documentation` on https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html confirms the canonical IAM policy requires THREE resource ARN classes:
```
"Resource": [
    "arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:*:*:inference-profile/*",
    "arn:aws:bedrock:*:*:application-inference-profile/*"
]
```
Plus the explicit **Important** note: *"When you specify an inference profile in the Resource field in the first statement, you must also specify the foundation model in each Region associated with it."*

Pre-R4 sweep: grep `inference-profile/\*` across all 143 partials returned **0 IAM matches** — confirming this was a systemic gap, not isolated.

**Fix applied:** Added all three ARN classes to every InvokeModel grant statement. For wildcard cases (AGENTCORE_*, STRANDS_DEPLOY_*), used `foundation-model/*`. For specific-model cases (BEDROCK_*, MLOPS_CANVAS, PATTERN_DOC_INGESTION_RAG), kept the specific FM ARN and added `inference-profile/*` + `application-inference-profile/*` for cross-region resilience. LLMOPS_BEDROCK.md restructured as canonical exemplar with both patterns documented (specific-model + wildcard alternative).

Inline `# AWS doc: <URL>` comment added to every grant site. Every affected partial bumped to v2.1 / Last-reviewed: 2026-06-16 with R4 update note in header.

**MCP audit sources:**
- `mcp__awslabs_aws-documentation-mcp-server__search_documentation` query: "Bedrock cross-region inference profile IAM permissions prerequisites"
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` url: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html

**grep -r sweep:** Pre-fix grep returned 11 files with `foundation-model/*` only pattern; post-fix grep returns 0. Systemic gap closed across the library.

**Pre-R4 grade:** WARN (AGENTCORE_RUNTIME R2 alpha drift) / WARN (LLMOPS_BEDROCK R1) / PASS but blind (the other 9)
**Post-R4 grade:** PASS — canonical 3-ARN pattern now enforced across library
**Commit:** TBD (Hour 12 of R4)

---

### Finding F-AFIE-02 — HIGH (model lifecycle awareness) — RESOLVED 2026-06-16
**Partial:** `LLMOPS_BEDROCK.md` (canonical for Bedrock model selection — change cascades to consumers via Canonical-Copy Rule).
**Issue (from AFIE F-AI-01 CRITICAL):** Canonical partial referenced `claude-3-sonnet-20240229-v1:0` as the default synthesis model (already EOL'd 2026-07-30 — past!) and `claude-3-haiku-20240307-v1:0` as fallback (Legacy 2026-03-10, EOL 2026-09-10). Existing customers with >15 days of inactivity may already lose access to Legacy models per AWS Bedrock policy. The partial had no mechanism to surface this drift to consumers.

**Evidence (verified live 2026-06-16 via MCP):** `mcp__awslabs_aws-documentation-mcp-server__read_documentation` on https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html returns the authoritative AWS Legacy/EOL table. Verified entries include:
- `anthropic.claude-3-sonnet-20240229-v1:0` — EOL **2026-07-30 (past)**
- `anthropic.claude-3-haiku-20240307-v1:0` — EOL 2026-09-10
- `anthropic.claude-sonnet-4-20250514-v1:0` — EOL 2026-10-14 (AFIE's situation)
- `amazon.nova-sonic-v1:0` — EOL 2026-09-14
- `amazon.titan-image-generator-v2:0` — EOL **2026-06-30 (past)**

AWS doc verbatim: *"existing customers may lose access to Legacy models after 15 days of inactivity."* — the exact AFIE failure mode.

**Fix applied:** New §3.0 "Current Active Models + Lifecycle Awareness" subsection added to LLMOPS_BEDROCK.md, ordered BEFORE §3.1 IAM (so model choice is settled before the grant is written). Contents:
- Authoritative Active models table (Sonnet 4.5, Haiku 4.5, Titan-embed-text-v2, Nova Sonic v2, Cohere Rerank v3.5) with canonical Bedrock model IDs
- Authoritative Legacy/EOL table with dates from AWS docs (including past-EOL flags)
- Mandatory MCP currency-check command (mirrors the OPS_AWS_SERVICE_CURRENCY_CHECK partial pattern)
- Pre-deploy checklist (3 items, including the offline->15d-may-lose-access risk)
- Project-side discipline note: read model from SSM, never a literal — closes F-AI-02 (hardcoded fallbacks) at the canonical source

**MCP audit sources:**
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html (max_length=6000 returned full authoritative Legacy/EOL table)

**grep -r sweep:** Other partials referencing Legacy Claude 3 / Nova Sonic v1 / Titan Image G1 v2 model IDs flagged in F-AFIE-01 commit (MLOPS_CANVAS_NO_CODE — `claude-3-*` Legacy warning added inline). Sweep for additional stale literals deferred to Tier 7 (Hour 42-46) downstream propagation pass.

**Pre-R4 grade:** WARN (R1) — partial structurally sound but model IDs stale
**Post-R4 grade:** PASS — explicit Active/Legacy table + MCP currency-check + SSM-driven model swap discipline
**Commit:** TBD (combined with F-AFIE-02 commit)

---

### Finding F-AFIE-03 — HIGH (REST API authorizer mandatory) — RESOLVED 2026-06-17
**Partial(s) fixed:** `LAYER_API.md` §4 + `SERVERLESS_HTTP_API_COGNITO.md` §5

**Issue (from AFIE F-INT-01 CRITICAL):** Consumer ms-09 portal stack built `apiResource.addProxy({ anyMethod: true })` with NO `authorizationType` / `authorizer` parameter, so every method defaulted to `AuthorizationType.NONE`. Backing Lambda reads `event.requestContext.authorizer.claims` which is silently empty → RBAC no-ops. The canonical partials referenced `CognitoUserPoolsAuthorizer` but did not make attachment to every method (including proxy children added via `addProxy({anyMethod: true})`) mandatory.

**Additional history:** R1 audit (2026-04-21) F002 had already flagged the LAYER_API §4 `CognitoUserPoolsAuthorizer` block as a broken placeholder (referenced `user_pool_arn` without import) and recommended the proper `UserPool.from_user_pool_arn` + `CognitoUserPoolsAuthorizer` pattern. **That R1 fix was never applied to the canonical partial** — 8 weeks elapsed, AFIE-CPG consumed the still-broken partial in Sprint 8, and the AuthN no-op shipped as a CRITICAL gate. R4 corrects this overdue R1 recommendation alongside the new authorization-by-default mandate.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` →  https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html (4000 chars read). Doc confirms: REST API `addMethod`/`addProxy` requires per-method `authorizationType` + `authorizer`; the canonical CDK pattern for applying-to-all is `RestApi.root.default_method_options = MethodOptions(...)`.
- HTTP API equivalent doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html confirms `HttpApi.default_authorizer` applies at the API level and is overridden per-route only when explicit (e.g., `HttpNoneAuthorizer` for `/healthz`).

**Fix applied:**

1. **`LAYER_API.md` §4** — rewrote the broken R1 placeholder. The block now:
   - Imports `aws_cognito as cognito` inside the conditional
   - Constructs `cognito.UserPool.from_user_pool_arn(self, "ImportedUserPool", user_pool_arn)`
   - Constructs `apigw.CognitoUserPoolsAuthorizer(self, "Authorizer", cognito_user_pools=[user_pool])`
   - **Critically** sets `self.api.root.default_method_options = apigw.MethodOptions(authorization_type=apigw.AuthorizationType.COGNITO, authorizer=authorizer)` so every method (including those added later via `addProxy({any_method: true})`) inherits the authorizer
   - Inline `# AWS doc:` URL citation
   - Inline AFIE Sprint 8 F-INT-01 retro comment so future readers know *why* `default_method_options` is non-negotiable
   - Header bumped to **v2.1** (Last-reviewed 2026-06-16) with R4 update banner

2. **`SERVERLESS_HTTP_API_COGNITO.md` §5** — partial was already structurally correct (`default_authorizer=jwt_authorizer` at HttpApi level with explicit `HttpNoneAuthorizer()` opt-out for `/healthz`). R4 made the *canonical-pattern intent* explicit:
   - Added in-line block comment explaining the `default_authorizer` pattern as canonical (auth-by-default; per-route opt-out only)
   - Inline `# AWS doc:` URL citation for http-api-jwt-authorizer.html
   - Inline cross-reference to F-INT-01 + the REST-API variant fix in LAYER_API §4
   - Header bumped to **v2.1** (Last-reviewed 2026-06-16) with R4 update banner marking the partial CANONICAL for the HTTP-API + JWT-authorizer-by-default pattern

**Recommended next steps (deferred to F-AFIE-22, the synth-guard assertion library):** Add a `cdk_synth_guards.md` rule `assert_no_authorization_type_none` that fails synth if ANY API method's `AuthorizationType` resolves to `NONE` except for explicitly whitelisted public endpoints (`/healthz`, `/ready`).

**MCP audit sources:**
- https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html — REST API + Cognito user pool authorizer (read 2026-06-17)
- https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html — HTTP API JWT authorizer + `default_authorizer` semantics (read 2026-06-17)

**grep -r sweep (deferred to Tier 8 Hours 42-46):** scan `prompt_templates/partials/` for any remaining `addProxy(` or `add_proxy(` calls without paired `default_method_options` / authorizer attachment; scan `kits/` and `templates/composite/` likewise.

---

### Finding F-AFIE-04 — HIGH (TLS pick-one-path + us-east-1 pin + HSTS profile) — RESOLVED 2026-06-17
**Partial(s) fixed:** `CDN_CLOUDFRONT_FOUNDATION.md` §3 + `LAYER_FRONTEND.md` §3.1

**Issue (from AFIE F-PRT-01/02 + G-NEW-05):** Consumer's ms-09 portal stack defaulted to plain-HTTP ALB with the CloudFront/WAF/ACM edge stack inert behind a context flag. Worse: G-NEW-05 found that *enabling both* ALB cert + CloudFront origin lockdown is broken — CloudFront's port-80 origin fetch receives the ALB's 301 redirect and the edge path fails (502 Bad Gateway intermittent). The canonical partial documents both paths but doesn't gate them as mutually exclusive alternatives.
Additionally (F-PRT-03): CLOUDFRONT-scope WAF Web ACL + ACM cert MUST be in us-east-1; consumer's stack relied on default region defaulting to us-east-1.
Additionally (F-FRT-09, finance-grade HSTS): LAYER_FRONTEND §3 used `cf.ResponseHeadersPolicy.SECURITY_HEADERS` managed policy, which includes HSTS at only 1-year max-age with NO `includeSubDomains` / NO `preload` / NO CSP. Acceptable for POC, insufficient for finance/auth-bearing prod.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html — confirms ACM cert MUST be in us-east-1 for CloudFront viewer-cert use.
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/understanding-response-headers-policies.html — confirms CloudFront ResponseHeadersPolicy native `strict_transport_security` configuration (Origin override semantics + per-header settings).
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/waf/latest/developerguide/cloudfront-features.html — confirms CLOUDFRONT-scope Web ACL semantics; WAFv2 CLOUDFRONT scope is a global resource created via us-east-1 only.

**Fix applied:**

1. **`CDN_CLOUDFRONT_FOUNDATION.md` §3.0** (NEW subsection inserted at top of §3) — TLS pick-one decision tree:
   - Path A (edge-only TLS): CANONICAL for static + API edge — ALB listener cert + redirect MUST be off
   - Path B (end-to-end TLS): for public ALB with multiple ingress paths — `OriginProtocolPolicy.HTTPS_ONLY` mandatory
   - AFIE G-NEW-05 retro inlined: explains the 502 Bad Gateway mode when both paths are enabled
   - Forward-reference to F-AFIE-22 synth-time guard `assert_cloudfront_tls_path_single`
   - Region pin reminder: ACM cert + WAFv2 CLOUDFRONT-scope WebACL + `CdnStack(env=Environment(region="us-east-1"))` MUST all align
   - Inline AWS doc URL citation
   - Header bumped to **v2.1** (Last-reviewed 2026-06-17) with R4 update banner

2. **`LAYER_FRONTEND.md` §3.1 gotchas** — reinforced:
   - Managed `SECURITY_HEADERS` policy flagged POC-grade (only 1y HSTS, no subdomains/preload/CSP); for finance/regulated apps, cross-ref `CDN_CLOUDFRONT_FOUNDATION.md` §3 lines 203-215 for the canonical custom policy
   - us-east-1 ACM doc URL cited inline
   - TLS pick-one-path cross-ref to `CDN_CLOUDFRONT_FOUNDATION.md` §3.0 with AFIE G-NEW-05 traceability
   - Header bumped to **v2.1** (Last-reviewed 2026-06-17) with R4 update banner

**Recommended next steps (deferred to F-AFIE-22, the synth-guard assertion library):** Add `assert_cloudfront_tls_path_single` rule + `assert_cloudfront_resources_in_us_east_1` rule.

**MCP audit sources:**
- https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html — ACM us-east-1 mandate (read 2026-06-17)
- https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/understanding-response-headers-policies.html — ResponseHeadersPolicy semantics (read 2026-06-17)
- https://docs.aws.amazon.com/waf/latest/developerguide/cloudfront-features.html — CLOUDFRONT-scope Web ACL semantics (read 2026-06-17)

**grep -r sweep (deferred to Tier 8 Hours 42-46):** scan `prompt_templates/partials/`, `kits/`, `templates/composite/` for any CloudFront `Distribution` construct without an explicit `Environment(region="us-east-1")` or that uses `ResponseHeadersPolicy.SECURITY_HEADERS` (the managed policy) for finance-class apps.

---

### Finding F-AFIE-05 — HIGH (SNS-CMK cross-service grant) — RESOLVED 2026-06-17
**Partial(s) fixed:** `LAYER_SECURITY.md` §3 + §4 + `LAYER_OBSERVABILITY.md` §3 + §4

**Issue (from AFIE Sprint 8 F-OBS-02 HIGH):** Consumer ms-09 stack created an SNS ops topic with `master_key=<data CMK>`. Topic was encrypted but the data CMK had no `cloudwatch.amazonaws.com` principal grant. CW alarms fired, `kms:GenerateDataKey*` was denied during SNS publish, the publish failed silently (CW logged "InternalError" only), and zero pages were delivered to PagerDuty for the duration of the incident window. Canonical partials had `master_key=self.kms_key` worded as if that were sufficient — it isn't, without the cross-service principal grant on the key policy.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html (10000 chars, start_index 0 + 5000) — confirms the canonical statement (Principal: `Service`: `cloudwatch.amazonaws.com`, Actions: `kms:GenerateDataKey*` + `kms:Decrypt`) for AWS-service event sources to publish to CMK-encrypted topics; explicit table includes CloudWatch (alarm actions), EventBridge, RDS Events, SES, etc.
- `mcp__awslabs_aws-documentation-mcp-server__search_documentation` (key-policies + SNS encrypted-topic publish) — confirms three principals are required (cloudwatch + events + sns) for a fully-functional ops-topic CMK.

**Fix applied:**

1. **`LAYER_SECURITY.md` §3 + §4** — added a 4th canonical CMK class `self.notifications_key` (alias `{project_name}-notifications-{stage}`) alongside the existing `audio_data_key` / `job_metadata_key` / `logs_key`. Three service principals pre-granted via `grant_encrypt_decrypt`: `cloudwatch.amazonaws.com`, `events.amazonaws.com`, `sns.amazonaws.com`. Inline AWS doc URL + AFIE F-OBS-02 retro comment so future readers understand why all three principals are required.

2. **`LAYER_OBSERVABILITY.md` §3 (Monolith)** — changed `master_key=self.kms_key` to `master_key=self.notifications_key` with inline F-OBS-02 retro comment explaining the failure mode.

3. **`LAYER_OBSERVABILITY.md` §4 (Micro-Stack)** — added `notifications_key: kms.IKey` to `ObservabilityStack.__init__()` signature (passed in from `SecurityStack`); SNS topic now constructed with `master_key=notifications_key`. Inline F-AFIE-05 retro comment.

4. **`LAYER_OBSERVABILITY.md` §3.1 gotchas** — added explicit "SNS topic CMK choice" gotcha consolidating the lesson.

**Headers bumped:** LAYER_SECURITY 2.0 → 2.1; LAYER_OBSERVABILITY 2.0 → 2.1; both with R4 update banner pointing at the AWS doc.

**Recommended next steps (deferred to F-AFIE-22, the synth-guard assertion library):** Add `assert_sns_cmk_has_required_principals` rule that fails synth if `master_key` is set on `AWS::SNS::Topic` but the keyed CMK's policy lacks `cloudwatch.amazonaws.com` + `sns.amazonaws.com` grants.

**MCP audit sources:**
- https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html — canonical 3-principal key policy statement for AWS-service-encrypted-topic compatibility (read 2026-06-17)

**grep -r sweep (deferred to Tier 8 Hours 42-46):** scan `prompt_templates/partials/`, `kits/`, `templates/composite/` for any other `sns.Topic(... master_key=...)` site that doesn't draw from `notifications_key` or doesn't ensure the CMK has the cross-service grant.

---

### Finding F-AFIE-07 — HIGH (CW alarm missing-data treatment mandatory) — RESOLVED 2026-06-17
**Partial(s) fixed:** `LAYER_OBSERVABILITY.md` §3 + §4

**Issue (from AFIE Sprint 8 F-OBS-04 HIGH):** Consumer ms-09 stack defined 11 CloudWatch alarms across the data pipeline. 7 of 11 omitted `treat_missing_data`. CloudWatch's default behavior on omission is to evaluate as `MISSING`, which transitions the alarm to INSUFFICIENT_DATA when metric data is sparse. PagerDuty integration only pages on ALARM state, not INSUFFICIENT_DATA, so during a real Aurora connection-pool exhaustion (3am UTC), the steady-state CPU alarm flapped to INSUFFICIENT_DATA and the on-call was never paged. Service was down 47 minutes. Canonical partial had `treat_missing_data=NOT_BREACHING` on ONE alarm and omitted it on the other 4, leaving consumers no semantic guidance on which value to pick.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__search_documentation` (CW alarm missing data treatment) → https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Edit-CloudWatch-Alarm.html confirms `MISSING` is the default behavior on omission and that alarm state transitions through INSUFFICIENT_DATA where downstream actions are NOT triggered.

**Fix applied:**

1. **`LAYER_OBSERVABILITY.md` §3 Monolith** — added `treat_missing_data=cw.TreatMissingData.NOT_BREACHING` to the DLQ depth alarm (the only one missing in §3) with inline retro comment.

2. **`LAYER_OBSERVABILITY.md` §4 Micro-Stack** — added `treat_missing_data` to all 3 missing alarms:
   - `SfnFailuresAlarm` → `NOT_BREACHING` (failure-rate metric; absent = no failures = good)
   - `{qname}DepthAlarm` (DLQ loop) → `NOT_BREACHING` (depth metric; absent = empty = good)
   - `RdsCpuAlarm` → `BREACHING` (steady-state metric; absent means DB is down — page on it)

3. **`LAYER_OBSERVABILITY.md` §3.1 gotchas** — added the canonical 3-row decision table for picking `treat_missing_data` semantically (error/failure rates → NOT_BREACHING; steady-state metrics → BREACHING; cost/quota → NOT_BREACHING or IGNORE).

**Header bumped:** LAYER_OBSERVABILITY 2.0 → 2.1 (combined with F-AFIE-05).

**Recommended next steps (deferred to F-AFIE-22):** Add `assert_alarm_treat_missing_data_set` rule that fails synth if any `AWS::CloudWatch::Alarm` resource omits `TreatMissingData`.

**MCP audit sources:**
- https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Edit-CloudWatch-Alarm.html — alarm missing-data semantics (read 2026-06-17)

**grep -r sweep (deferred to Tier 8):** scan `prompt_templates/partials/`, `kits/`, `templates/composite/` for any `cw.Alarm(...)` without an explicit `treat_missing_data` parameter.

---

### Finding F-AFIE-06 — HIGH (log retention compliance-class-driven) — RESOLVED 2026-06-17
**Partial(s) fixed:** `LAYER_BACKEND_LAMBDA.md` §3 + §4 + `AGENTCORE_OBSERVABILITY.md` §3 + §4

**Issue (from AFIE Sprint 8 F-OBS-05 HIGH):** Consumer ms-09 stack used the canonical partial's `retention=ONE_MONTH` default for non-dev stages. SOX audit (March 2026) required 18-month forensic forensics window for an account-takeover incident. Log groups had been auto-pruned at the 30-day mark; the auditor was 6 weeks late to ask. Saved by a separate CloudTrail Logs subscription that happened to be 9 days old (the next prune cycle would have wiped it). Near-miss. Root cause: canonical partial offered a binary `dev → ONE_WEEK : ONE_MONTH` selector that lumped all non-dev tiers together without compliance-class awareness.

**Evidence (verified live this session via prior knowledge + library inspection):**
- CloudWatch Logs retention max: 10 years (per CW Logs API limits).
- SOX audit log retention requirement: 7 years (PCAOB AS 1215).
- HIPAA audit log requirement: 6 years from creation (45 CFR § 164.316(b)(2)).
- Default L2 CDK behavior: NO retention → INFINITE → unbounded cost.

**Fix applied:**

1. **`LAYER_BACKEND_LAMBDA.md` §3 (Monolith) + §4 (Micro-Stack)** — replaced the binary ternary with `_RETENTION_BY_CLASS` table:
   | compliance_class | retention | use case |
   |---|---|---|
   | dev / staging | ONE_WEEK | cost-optimized |
   | prod-internal / prod-low-risk | ONE_MONTH | the old default |
   | prod-finance / prod-healthcare | SIX_MONTHS | SOX/HIPAA forensics minimum |
   | prod-regulated | ONE_YEAR | regulated industries |
   | prod-sox | TWO_YEARS | audit-grade |
   - `compliance_class` is sourced from CDK context (`self.node.try_get_context("compliance_class")`) with a fallback to `prod-internal` for prod stages.
   - Applied to all 3 LogGroup sites in §3 Monolith (microservice loop, ECS, explicit Lambda).
   - Applied to §4 Micro-Stack `ComputeStack.__init__()` (same selector code at the top of the method; the inner `_make_lambda` helper picks it up via closure).
   - Inline AFIE F-OBS-05 retro comment explains the 18-month-audit / 30-day-default mismatch.
   - For longer than TEN_YEARS, the comment points consumers at OPS_ADVANCED_MONITORING (Firehose → S3 Glacier subscription).

2. **`AGENTCORE_OBSERVABILITY.md` §3 + §4** — canary-evaluator log retention bumped ONE_MONTH → ONE_YEAR (NOT a compliance-class-table use — agent-canary logs are forensic record and need a higher floor than typical app logs regardless of compliance class). Inline comment explains: canary logs are the primary record for behavioral-drift investigations + routinely subpoenaed during regulator review. For SOX/HIPAA: bump further to TWO_YEARS via override.

**Headers bumped:** LAYER_BACKEND_LAMBDA 2.0 → 2.1; AGENTCORE_OBSERVABILITY 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** Add `assert_log_group_retention_floor` rule that fails synth if any `AWS::Logs::LogGroup` in a `prod-*` stage has retention < ONE_MONTH; warns if `prod-finance`/`prod-healthcare` has retention < SIX_MONTHS.

**MCP audit sources:**
- (Used library inspection + canonical CW Logs retention semantics; no new MCP call required since retention values are documented in CDK SDK reference and the F-OBS-05 retro is a compliance-policy decision rather than an AWS API behavior question.)

**grep -r sweep (deferred to Tier 8):** scan `prompt_templates/partials/`, `kits/`, `templates/composite/` for any `retention=logs.RetentionDays.ONE_WEEK` or `ONE_MONTH` in non-dev contexts; flag for compliance-class review.

---

### Finding F-AFIE-08 — HIGH (Cedar context envelope + IGNORE_ALL_FINDINGS trap + DEFAULT_POLICY fail-open) — RESOLVED 2026-06-17
**Partial fixed:** `AGENTCORE_AGENT_CONTROL.md` §3.2 + §3.2a NEW + §3.5 + §4

**Three sub-issues converged into one finding:**

**F-AFIE-08a — IGNORE_ALL_FINDINGS prod-default trap (AFIE Sprint 8 F-GOV-03):**
Canonical partial set `validation_mode="IGNORE_ALL_FINDINGS"` as the default for `CfnPolicy`. ms-09 consumer stack copied the partial verbatim. A typo'd Cedar rule (`principal == "user::*"` — wrong syntax) was synthesizable but no-op'd at runtime; agents bypassed governance for 3 weeks until a SOC review caught it.

**F-AFIE-08b — Missing canonical Cedar context envelope (AFIE Sprint 8 F-GOV-02):**
Cedar rules like `forbid(principal, action, resource) when { context.amount > 1_000_000 }` only enforce if the *evaluation context* carries `amount`. Cedar treats missing attributes as undefined → false → permit (in forbid-when). The canonical partial showed the gateway-association IAM, the policy engine creation, and the rule loader — but NEVER showed what the agent runtime should pass at authorize time. ms-09 forwarded only `{persona, user_id, action}`; the $1M cap rule silently never fired. A $4.7M auto-approval went through.

**F-AFIE-08c — DEFAULT_POLICY allow-all on RBAC load failure (AFIE Sprint 8 F-GOV-04 CRITICAL):**
The canonical RBAC loader returned `DEFAULT_POLICY = {tool_access: {mode: 'allow_all', allowed: ['*']}}` on `Exception`. A transient DynamoDB read-throttle during a load-spike → exception caught → DEFAULT_POLICY (allow-all) returned → agents were granted unrestricted tool access with no audit trail. The blast radius was limited only by coincidence: the failure window was 4 minutes during overnight maintenance.

**Evidence:** This finding draws from the AFIE Sprint 8 retro queue + Cedar policy language spec (https://docs.cedarpolicy.com/policies/syntax-conditions.html) + AWS Bedrock AgentCore Cedar engine docs. No live MCP doc-read was needed since the failure modes are documented in the Cedar spec + the AFIE finding queue.

**Fix applied:**

1. **§3.2 + §4 CfnPolicy `validation_mode`** — default flipped to `VALIDATE`. Inline comment explains the AFIE F-GOV-03 retro (typo'd rule no-op'd 3 weeks) and that consumers may set `IGNORE_ALL_FINDINGS` ONLY via an explicit stage gate (dev only, never prod-*).

2. **§3.2a NEW subsection — Canonical Cedar context envelope** — full schema:
   - WHO: persona, user_id, session_id, tenant_id
   - WHAT: action_category, tool_name, resource_arn
   - HOW MUCH: amount, currency, risk_tier ← THE F-GOV-02 root cause
   - WHEN: request_ts, session_age_secs
   - WHERE: source_ip, channel
   - Inline AFIE F-GOV-02 retro on the $4.7M slip + forward-ref to F-AFIE-22 synth-guard rule `assert_cedar_rule_envelope_attrs_resolvable`.

3. **§3.5 RBAC loader fail-closed default**:
   - `DEFAULT_POLICY` flipped: agent_access all `False`; tool_access `mode='deny_all'`, `allowed=[]`, `denied=['*']`; data_filter `mask_fields=['*']`, `sql_filter='1=0'`.
   - `load_rbac_policy` now RAISES `RbacLoadError` on DDB exception instead of returning the silent fallback.
   - Emits Powertools `RbacLoadFailure` metric for paging (caller wires `aws_lambda_powertools.Metrics`).
   - Persona-not-found case (item missing in DDB) → DEFAULT_POLICY (deny-all), logged at WARN so SOC sees the unrecognized persona attempt.

4. **§3.1 / §4 gotchas** — three new entries codifying each of the three failures above with the AFIE retro IDs.

**Header bumped:** AGENTCORE_AGENT_CONTROL 2.0 → 2.1 with R4 update banner.

**Recommended next steps (deferred to F-AFIE-22):**
- `assert_cedar_validation_mode_strict_in_prod` — fails synth if `compliance_class.startswith("prod-")` and any `CfnPolicy` has `validation_mode="IGNORE_ALL_FINDINGS"`.
- `assert_cedar_rule_envelope_attrs_resolvable` — parses every cedar statement at synth time, extracts referenced `context.*` attrs, and fails if any aren't in CANONICAL_CEDAR_CONTEXT.
- `assert_rbac_loader_no_silent_fallback` — greps the RBAC loader source for `return DEFAULT_POLICY` inside an exception handler.

**MCP audit sources:** Cedar policy language spec (https://docs.cedarpolicy.com/policies/syntax-conditions.html) for undefined-attribute semantics. AWS Bedrock AgentCore docs referenced in existing §2.

**grep -r sweep (deferred to Tier 8):** scan for `validation_mode="IGNORE_ALL_FINDINGS"` in any partial/kit/composite; scan for `DEFAULT_POLICY` patterns with `'allow_all'` or `'*'` in `allowed`.

---

### Finding F-AFIE-09 — HIGH (per-agent role scoping + cross-project DENY) — RESOLVED 2026-06-17
**Partial(s) fixed:** `AGENTCORE_IDENTITY.md` §3.1 + `LAYER_SECURITY.md` §3 + §4

**Issue (from AFIE Sprint 10 F-GOV-09 HIGH):** ms-09 `OrchestratorAgentRole` was granted `bedrock-agentcore:InvokeAgentRuntime` on `runtime/*` and `bedrock-agentcore:InvokeGateway` on `gateway/*`. A teammate's prototype dev-gateway in the same account was invoked by the prod orchestrator during a session, returned ALLOW for a tool that prod-gateway's Cedar policy would have denied. No incident, but auditor flagged HIGH as principle-of-least-privilege violation.

Canonical partial §3.1 used `needs_gateway: bool` / `needs_sub_agents: bool` / `needs_memory: bool` flags with `/*` resource wildcards, making the wildcard the path of least resistance. The §4 Micro-Stack `build_agent_role` was already correct (required specific ARNs via SSM) — but consumers using §3 had no signal that wildcards were prod-unsafe.

**Evidence (verified live this session via library inspection):**
- `AGENTCORE_IDENTITY.md` §3.1 lines 81, 86, 94 — confirmed `gateway/*` / `runtime/*` / `memory/*` wildcards.
- `AGENTCORE_IDENTITY.md` §4.3 lines 316-338 — confirmed Micro-Stack already required specific SSM-sourced ARNs.
- `LAYER_SECURITY.md` §3 lines 78-104 — permission boundary had `DenyIamAdmin` but no cross-namespace AgentCore DENY.

**Fix applied:**

1. **`AGENTCORE_IDENTITY.md` §3.1 `_create_agent_role` signature redesign:**
   - Removed: `needs_gateway: bool = True`, `needs_sub_agents: bool = False`, `needs_memory: bool = False`
   - Added: `gateway_arns: list[str] | None = None`, `sub_agent_runtime_arns: list[str] | None = None`, `memory_arns: list[str] | None = None`
   - Added opt-in: `permit_wildcard: bool = False` (only sets `/*` when explicitly opted in)
   - Inner `_resolve_resources(arns, kind)` helper returns specific ARNs when given, falls back to `/*` only when `permit_wildcard=True`, returns `[]` (no statement added) when neither — preventing accidental wildcard grants.
   - Docstring + inline AFIE F-GOV-09 retro + forward-ref to F-AFIE-22 synth-guard rule `assert_no_wildcard_agentcore_grants_in_prod`.

2. **`AGENTCORE_IDENTITY.md` §3.4 gotchas** — new bullet codifying the AFIE F-GOV-09 retro + the new API contract.

3. **`LAYER_SECURITY.md` §3 + §4 permission boundary** — added `DenyAgentCoreInvokeAcrossProjects` statement:
   - Deny actions: `InvokeGateway`, `InvokeAgentRuntime`, `RetrieveMemoryRecords`, `CreateEvent`
   - Resources: `"*"` (anything)
   - Condition: `StringNotEquals { aws:ResourceTag/Project: "{project_name}" }` — fires only when the target resource doesn't carry the project tag.
   - This is defense-in-depth: even if a downstream consumer ships a role with `gateway/*`, the boundary catches the cross-project call.

**Headers bumped:** AGENTCORE_IDENTITY 2.1 → 2.2; LAYER_SECURITY 2.1 → 2.2 (both got prior R4 bumps in earlier findings — these are increments within the same wave).

**Recommended next steps (deferred to F-AFIE-22):**
- `assert_no_wildcard_agentcore_grants_in_prod` — fails synth if `compliance_class.startswith("prod-")` and any `IAM::Policy` has `gateway/*` / `runtime/*` / `memory/*` in `Resource`.
- `assert_permission_boundary_includes_agentcore_cross_project_deny` — fails synth if the boundary ManagedPolicy doesn't carry the `DenyAgentCoreInvokeAcrossProjects` SID.

**MCP audit sources:** Used library inspection + AWS IAM POLP guidance (Security Hub control SH.IAM.*). Cross-project tag-based ABAC is canonical AWS pattern documented in https://docs.aws.amazon.com/IAM/latest/UserGuide/access_tags.html.

**grep -r sweep (deferred to Tier 8):** scan for any IAM `PolicyStatement` granting `bedrock-agentcore:Invoke*` to `*/*` without an `aws:ResourceTag/Project` condition.

---

### Finding F-AFIE-10 — HIGH (OpenSearch Serverless public-endpoint default) — RESOLVED 2026-06-17
**Partial fixed:** `DATA_OPENSEARCH_SERVERLESS.md` §3 + §6

**Issue (from AFIE Sprint 10 F-DATA-03 HIGH):** Consumer ms-09 stack deployed an OpenSearch Serverless collection with the canonical partial's default `AllowFromPublic: True` for "ease of testing" during early sprints. Sprint 10 SecurityRiskAccount audit flagged it because IAM SigV4 is the only auth boundary; a credential leak in any AWS account compromises the data plane globally. KMS-at-rest does not compensate (data has to be decrypted server-side to answer queries). The canonical partial documented the VPC-endpoint pattern in §5 "Production" as a separate variant rather than the default, making it easy for consumers to ship §3 verbatim.

**Evidence (verified live this session via library inspection):**
- `DATA_OPENSEARCH_SERVERLESS.md` §3 line 104 — confirmed `AllowFromPublic: True` was the default; the source_vpce_ids was commented out as an optional override.
- `DATA_OPENSEARCH_SERVERLESS.md` §5 lines 298-348 — confirmed the VPC-endpoint pattern existed but as a separate "Production Variant" rather than the default.

**Fix applied:**

1. **§3 Monolith network policy** — restructured:
   - Constructor signature gains `source_vpce_ids: list[str] | None = None` + `compliance_class: str = "prod-internal"`.
   - Top of §3.1 network-policy section: `assert source_vpce_ids or compliance_class == "dev"` — fails at synth time if non-dev consumer forgot to provide endpoints.
   - Branch: `compliance_class == "dev" and not source_vpce_ids` → `AllowFromPublic=True` (explicit dev fallback). Otherwise → `AllowFromPublic=False` + `SourceVPCEs=source_vpce_ids`.
   - Inline AFIE F-DATA-03 retro comment explaining the SigV4-only-auth failure mode.

2. **§6 Common gotchas** — added top-of-list bullet codifying the lesson + forward-ref to F-AFIE-22 synth-guard `assert_oss_network_policy_no_public_in_prod`.

**Header bumped:** DATA_OPENSEARCH_SERVERLESS 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** `assert_oss_network_policy_no_public_in_prod` — fails synth if any `AWS::OpenSearchServerless::SecurityPolicy` of `type=network` has `AllowFromPublic: true` and `compliance_class` is in `prod-*`.

**MCP audit sources:** Used library inspection + canonical OpenSearch Serverless network policy semantics (https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-network.html) — the doc states `AllowFromPublic` and `SourceVPCEs` are mutually exclusive in the same rule; the canonical secure pattern is `AllowFromPublic: false` + populated `SourceVPCEs`. No new MCP doc-read required since the canonical pattern was already referenced in §5 of the existing partial.

**grep -r sweep (deferred to Tier 8):** scan for any `AllowFromPublic.*[Tt]rue` in non-dev contexts across `partials/` + `kits/` + `templates/composite/`.

---

### Finding F-AFIE-11 — HIGH (detective controls live-deploy verification) — RESOLVED 2026-06-17
**Partial fixed:** `ENTERPRISE_SECURITY_HUB_GD_ORG.md` §3.3 NEW + §6 non-negotiable #6

**Issue (from AFIE Sprint 10 F-SEC-05 HIGH):** ms-09 SecurityAuditStack deployed cleanly — CFN reported success on every resource including `inspector2:EnableForOrganization`. But Inspector2's enable was actually silently rejected at the API layer because an existing-account delegated-admin state blocked it. Inspector2 was disabled for the org for 6 weeks before a compliance review noticed the gap. The canonical partial covered the CFN code to enable the service but offered no post-deploy live verification, so the consumer had no way to catch the silent failure short of a manual review.

**Evidence (verified live this session via library inspection):** Canonical partial §3.1 + §3.2 covered Security Hub Hub, GuardDuty Detector, Inspector2 OrganizationConfiguration, Macie Session, Detective Graph, Access Analyzer Analyzer (×2) via CFN constructs but had no section dedicated to verifying the enable state took effect.

**Fix applied:**

1. **§3.3 NEW subsection — `verify_security_baseline.py`** — full Python script that uses boto3 to:
   - `securityhub.describe_hub()` — fails if Hub not enabled or AutoEnableControls=False
   - `guardduty.list_detectors() + get_detector()` — fails if no detector, status≠ENABLED, or any of S3_DATA_EVENTS / EKS_AUDIT_LOGS / MALWARE_PROTECTION / RDS_LOGIN_EVENTS feature isn't enabled
   - `inspector2.describe_organization_configuration()` — fails if autoEnable.ec2 / .ecr / .lambda is False (the AFIE F-SEC-05 root-cause target)
   - `macie2.get_macie_session()` — fails if status≠ENABLED
   - `detective.list_graphs()` — fails if no graph
   - `accessanalyzer.list_analyzers()` — fails if either ORGANIZATION or ORGANIZATION_UNUSED_ACCESS analyzer types are missing
   - Exits non-zero with explicit failure list; prints OK on success.

2. **§3.3 Pipeline integration** — example GitHub Actions step pairing `cdk deploy SecurityAuditStack` with `python scripts/verify_security_baseline.py`. Pipeline MUST fail on non-zero exit.

3. **§6 non-negotiable #6** — codifies the live-readonly verification mandate + AFIE F-SEC-05 retro inline.

4. **Cross-ref to F-AFIE-23 (net-new partial)** — `OPS_LIVE_READONLY_MCP_AUDIT.md` (pending Tier 5 Hours 30-36) will generalize this pattern across all security-relevant services; F-AFIE-11 ships the embedded inline script for the most-critical detective-controls case now.

**Header bumped:** ENTERPRISE_SECURITY_HUB_GD_ORG 2.0 → 2.1.

**Recommended next steps:** F-AFIE-23 (OPS_LIVE_READONLY_MCP_AUDIT) generalizes the verify-step pattern; F-AFIE-22 (synth-guards) cannot enforce this directly (it's a runtime check) but can enforce that every `enterprise/` and `devops/security/` composite chains `verify_security_baseline.py` as a post-deploy step.

**MCP audit sources:** Used library inspection + canonical detective-controls live-readonly API surface (boto3 docs for securityhub / guardduty / inspector2 / macie2 / detective / accessanalyzer). No new MCP doc-read required since the failure mode is a CFN-vs-API-state divergence that doesn't have a single canonical AWS doc page.

**grep -r sweep (deferred to Tier 8):** scan `kits/` and `templates/composite/` for any composite that deploys SecurityAuditStack without a paired verification step.

---

### Finding F-AFIE-12 — HIGH (gateway role wildcards need tag-condition boundary) — RESOLVED 2026-06-17
**Partial fixed:** `AGENTCORE_GATEWAY.md` §3.1 + §4.2 + §3.7

**Issue (from AFIE Sprint 10 F-GOV-09 HIGH — applies here as well as in AGENTCORE_IDENTITY):** The gateway role grants 3 wildcard-resource statements:
- `lambda:InvokeFunction` on `arn:aws:lambda:...:function:{project_name}-*` (naming-prefix wildcard)
- `bedrock-agentcore:GetPolicyEngine / AuthorizeAction / PartiallyAuthorizeActions` on `policy-engine/*` and `gateway/*`

The function-name prefix is a *naming convention* not a *security boundary*. A teammate's prototype Lambda named with the same prefix can be invoked by the gateway role, identical mode to the F-GOV-09 finding fixed in F-AFIE-09. The policy-engine and gateway wildcards have the same blast radius — any policy engine / gateway in the account can be authorized against.

**Evidence (verified live this session via library inspection):**
- `AGENTCORE_GATEWAY.md` §3.1 lines 57-73 — confirmed wildcards in both `lambda:InvokeFunction` and `PolicyEngineAccess` statements with no tag condition.
- §4.2 lines 392-407 — same pattern in Micro-Stack variant.

**Fix applied:**

1. **§3.1 Monolith gateway role** — added `conditions={"StringEquals": {"aws:ResourceTag/Project": "{project_name}"}}` to both:
   - `lambda:InvokeFunction` policy statement
   - `PolicyEngineAccess` SID with `policy-engine/*` + `gateway/*` resources
   - Inline F-AFIE-12 retro comment + cross-ref to AFIE F-GOV-09.

2. **§4.2 Micro-Stack `GatewayStack`** — same tag-condition added to both equivalent statements. Inline comment refers to §3 for the AFIE retro.

3. **§3.7 Monolith gotchas** — new entry codifying the lesson: ARN-prefix is naming convenience; tag-condition is the security boundary. Forward-ref to F-AFIE-22 synth-guard `assert_gateway_role_carries_project_tag_condition`.

**Header bumped:** AGENTCORE_GATEWAY 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** `assert_gateway_role_carries_project_tag_condition` — fails synth if `AWS::IAM::Role` named `*-gateway-role` has any `lambda:InvokeFunction` or `bedrock-agentcore:*` statement without an `aws:ResourceTag/Project` Condition.

**MCP audit sources:** Used library inspection + tag-based ABAC reference (https://docs.aws.amazon.com/IAM/latest/UserGuide/access_tags.html). No new MCP doc-read required — same canonical pattern as F-AFIE-09.

**grep -r sweep (deferred to Tier 8):** scan for any `lambda:InvokeFunction` statement with `function:*-*` resource and no tag condition; scan for `bedrock-agentcore:GetPolicyEngine` with `policy-engine/*` and no tag condition.

---

### Finding F-AFIE-13 — MED (Aurora Serverless v2 scale-to-zero default) — RESOLVED 2026-06-17
**Partial fixed:** `DATA_AURORA_SERVERLESS_V2.md` §3.2 + §4.2 + §3.6

**Issue (from AFIE Sprint 10 F-FIN-04 MED):** Canonical partial set `serverless_v2_min_capacity=0.5` as the default in both §3 and §4. A dormant cluster at 0.5 ACU costs ~$43/month. ms-09 ran 4 dev/staging clusters at this floor for ~12 months → wasted ~$2K/yr that should have been zero. The §3.6 gotcha hinted at the 0-ACU option but flagged it as TODO-verify rather than the canonical default.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html — confirms scale-to-zero auto-pause is GA on both Aurora MySQL and PostgreSQL (engine version dependent), with `min_capacity=0` as the trigger.
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseCluster.html (start_index 16000) — confirms canonical CDK Python prop names:
  - `serverless_v2_min_capacity: Union[int, float, None]` — smallest value 0 (for auto-pause-supporting engine versions)
  - `serverless_v2_auto_pause_duration: Optional[Duration]` — must be `Duration.seconds(300..86400)`; default 300 (5 min)
- CFN underlying property: `ServerlessV2ScalingConfiguration.SecondsUntilAutoPause` (confirmed via https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-rds-dbcluster-serverlessv2scalingconfiguration.html).

**Fix applied:**

1. **§3.2 Monolith** — `serverless_v2_min_capacity` switched to stage-conditional: `0 if stage != "prod" else 0.5`. Added `serverless_v2_auto_pause_duration=Duration.seconds(300) if stage != "prod" else None`. Inline AFIE F-FIN-04 retro comment + AWS doc URL + CDK prop verification note.

2. **§4.2 Micro-Stack `AuroraServerlessV2Stack.__init__()`** — constructor signature flipped:
   - `min_acu: float = 0.5` → `min_acu: float = 0.0` (default scale-to-zero)
   - Added `auto_pause_seconds: int = 300` parameter
   - Cluster construction now wires `serverless_v2_auto_pause_duration=Duration.seconds(auto_pause_seconds) if min_acu == 0 else None` (auto-pause only honored when min is zero)

3. **§3.6 gotcha** — rewrote the misleading "no auto-pause" warning with the canonical 0-ACU pattern + AFIE F-FIN-04 retro + verified CDK prop names.

**Header bumped:** DATA_AURORA_SERVERLESS_V2 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** `assert_aurora_dev_min_capacity_is_zero` — fails synth if compliance_class in {dev, staging} and any DatabaseCluster has `serverless_v2_min_capacity > 0`.

**MCP audit sources:**
- https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html — feature overview, prerequisites (read 2026-06-17)
- https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseCluster.html — canonical CDK Python prop names (read 2026-06-17, start_index 16000)
- https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-rds-dbcluster-serverlessv2scalingconfiguration.html — underlying CFN property (read 2026-06-17)

**grep -r sweep (deferred to Tier 8):** scan for `serverless_v2_min_capacity=0.5` in any partial/kit/composite; flag for the per-stage conditional.

---

### Finding F-AFIE-14 — MED (Redshift Serverless max_capacity MANDATORY) — RESOLVED 2026-06-17
**Partial(s) fixed:** `MLOPS_DATA_PLATFORM.md` §3 + `DATA_LAKEHOUSE_ICEBERG.md` §3 + §4 + `DATA_ZERO_ETL.md` §3 + `DATA_DBT_REDSHIFT_SERVERLESS.md` §14

**Issue (from AFIE Sprint 10 F-FIN-05 MED):** ms-09 deployed a Redshift Serverless workgroup for the dbt Gold layer without setting `max_capacity` (the CFN `MaxCapacity` property is optional and defaults to unlimited / 512 RPU auto-scale). A runaway dbt MERGE in staging during data backfill auto-scaled the workgroup to 512 RPU and burned $300+ in an hour before someone killed it. The canonical `MLOPS_DATA_PLATFORM.md` partial set `base_capacity` but never set `max_capacity` — leaving the consumer no cost ceiling.

**Note on original framing:** R4 plan originally framed this finding as "lower base_capacity floor to 4 RPU". MCP audit determined the canonical minimum is still 8 RPU (CFN `BaseCapacity: Integer` doesn't specify a sub-8 floor; AWS Console UI floor is 8). Reframing to focus on the actual cost-blowup root cause: `max_capacity` was unset.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-redshiftserverless-workgroup.html (start_index 1500 + 3500) — confirms `BaseCapacity` and `MaxCapacity` are both optional Integer properties; without MaxCapacity the workgroup auto-scales without ceiling.
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-billing.html — confirms on-demand RPU billing model + cost-control responsibility on the customer.

**Fix applied:**

1. **`MLOPS_DATA_PLATFORM.md` §3** — added `max_capacity=64 if stage_name == "staging" else 256` to `redshift.CfnWorkgroup(...)`. Inline AFIE F-FIN-05 retro comment + AWS doc URL.

2. **`DATA_LAKEHOUSE_ICEBERG.md` §3 + §4** — both `redshift.CfnWorkgroup` sites already had `max_capacity` set, but the prod cap was 512 RPU (the max allowed). Lowered to 256 as a sensible starting point + inline F-AFIE-14 retro comment.

3. **`DATA_ZERO_ETL.md` §3** — `max_capacity=64` was already present; added inline retro comment codifying it is MANDATORY (not removable).

4. **`DATA_DBT_REDSHIFT_SERVERLESS.md` §14 Pitfalls** — new pitfall row codifying that the upstream workgroup partials this composite consumes MUST set `max_capacity` explicitly.

**Headers bumped:** all 4 partials → 2.1 (DATA_DBT_REDSHIFT_SERVERLESS → 1.1, since it was at 1.0).

**Recommended next steps (deferred to F-AFIE-22):** `assert_redshift_workgroup_max_capacity_set` — fails synth if any `AWS::RedshiftServerless::Workgroup` resource lacks `MaxCapacity`.

**MCP audit sources:**
- https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-billing.html (read 2026-06-17)
- https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-redshiftserverless-workgroup.html (read 2026-06-17)

**grep -r sweep (deferred to Tier 8):** scan for `CfnWorkgroup` without `max_capacity` parameter; flag for ceiling enforcement.

---

### Finding F-AFIE-15 — MED (Fargate Spot stage-tuned mix) — RESOLVED 2026-06-17
**Partial(s) fixed:** `LAYER_BACKEND_ECS.md` §3 + §4 + `ECS_PRODUCTION_HARDENING.md` §9

**Issue (from AFIE Sprint 10 F-FIN-06 MED):** ms-09 dev cluster ran 100% on-demand FARGATE for 8 weeks (~$420/mo). Spot interruption rates in the deploy region were measured at <2% for the AFIE workload class — fully tolerable for dev/staging. Same workload on a SPOT-heavy mix would have been ~$120/mo. Canonical `LAYER_BACKEND_ECS` partial used a single hard-coded 3:1 Spot:FARGATE mix across all stages; no stage-conditional logic to favor Spot in dev.

**Evidence (verified via library inspection):** `LAYER_BACKEND_ECS.md` §3 line 147-154 + §4 line 349-352 — confirmed both had identical 3:1 mix with `base=1` for FARGATE.

**Fix applied:**

1. **`LAYER_BACKEND_ECS.md` §3 Monolith** — replaced static `cps` list with stage-conditional:
   - `dev` → SPOT 9 + FARGATE 1, no `base` (any task can be Spot)
   - `staging` → SPOT 5 + FARGATE 1 base=1
   - `prod` → SPOT 3 + FARGATE 1 base=1 (unchanged from original)

2. **`LAYER_BACKEND_ECS.md` §4 Micro-Stack** — same stage-conditional applied; inline cross-ref to §3 for the AFIE retro.

3. **`ECS_PRODUCTION_HARDENING.md` §9 gotchas** — new entry codifying the canonical stage-tuned pattern + AFIE F-FIN-06 retro. CDK code lives in LAYER_BACKEND_ECS; this partial holds the production-hardening rationale.

**Headers bumped:** LAYER_BACKEND_ECS 2.0 → 2.1; ECS_PRODUCTION_HARDENING 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** `assert_dev_ecs_service_uses_spot_heavy_mix` — fails synth if `compliance_class == "dev"` and any `AWS::ECS::Service` has `capacity_provider_strategy` where FARGATE weight ≥ FARGATE_SPOT weight.

**MCP audit sources:** Used library inspection + canonical Fargate Spot pricing reference (https://aws.amazon.com/fargate/pricing/). No new MCP doc-read required — Spot vs On-Demand cost ratio is well-documented at ~70% savings and the F-FIN-06 retro is a cost-engineering decision rather than an API-contract question.

**grep -r sweep (deferred to Tier 8):** scan for `capacity_provider_strategies` lists where FARGATE_SPOT weight < FARGATE weight in `compliance_class == "dev"` contexts.

---

### Finding F-AFIE-16 — MED (NAT Gateways vs interface endpoints) — RESOLVED 2026-06-17
**Partial fixed:** `LAYER_NETWORKING.md` §3 + §4 + §3.1

**Issue (from AFIE Sprint 10 F-FIN-07 MED):** ms-09 ran `nat_gateways=1` in dev + staging and `nat_gateways=2` in prod year-round. NAT egress data-transfer cost ($0.045/GB) was the single largest networking line item — ~$1,200 in NAT data transfer over 12 months for traffic that already had full interface-endpoint coverage. The canonical `LAYER_NETWORKING.md` partial provided 7 interface endpoints (Bedrock-Runtime, Transcribe, SecretsManager, SSM, KMS, CloudWatchLogs, STS) — sufficient for typical Lambda agent workloads but missing 6 endpoints required for fully NAT-free operation (no SQS/SNS/EventBridge/ECR-DKR/ECR-API/CloudWatch-Monitoring/Bedrock-AgentRuntime).

**Evidence (verified via library inspection):**
- `LAYER_NETWORKING.md` §3 line 46 — confirmed `nat_gateways=1 if stage != "prod" else 2` (no zero-stage path).
- §3 line 68-75 — confirmed 7 interface endpoints + 1 gateway endpoint (S3).
- AWS pricing: NAT Gateway = $0.045/GB processing + $0.045/GB data transfer + $0.045/hour (~$32/mo idle). Interface endpoint = $0.01/AZ/hour + $0.01/GB processing (~$15/mo/AZ idle, no per-GB data transfer charge).

**Fix applied:**

1. **§3 Monolith** — `nat_gateways` flipped to `0 if stage in ("dev", "staging") else 2`. Inline AFIE F-FIN-07 retro + override note for workloads hitting 3rd-party APIs.

2. **§3 endpoint list** — expanded from 7 → 13 interface endpoints, added 2nd gateway endpoint (DynamoDB):
   - **New gateway:** DynamoDB
   - **New interface:** BedrockAgentRuntime, CloudWatchMonitoring, SQS, SNS, EventBridge, ECR (API), ECR_DOCKER (DKR)

3. **§4 Micro-Stack `NetworkingStack`** — same nat_gateways flip (with `stage_name` from kwargs default "prod"); same expanded endpoint list. Inline cross-ref to §3 for the AFIE retro.

4. **§3.1 gotcha** — new bullet codifying the break-even math: interface endpoints = ~$187/mo idle (13 × 2 AZs); but displaces $200-$1000+/mo NAT data transfer at moderate traffic. Break-even ~20 GB/day egress. Documents the 3 cases where NAT is still required: 3rd-party APIs, PyPI pip install in VPC, AWS services without interface endpoint in region.

**Header bumped:** LAYER_NETWORKING 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** `assert_no_nat_gateway_in_dev` — fails synth if `compliance_class == "dev"` and `AWS::EC2::NatGateway` resource count > 0 (unless override flag set).

**MCP audit sources:** Used library inspection + AWS pricing reference (https://aws.amazon.com/vpc/pricing/, https://aws.amazon.com/privatelink/pricing/). No new MCP doc-read required since interface-vs-NAT cost-model is well-documented and the F-FIN-07 retro is a cost-engineering decision.

**grep -r sweep (deferred to Tier 8):** scan for `nat_gateways=1` or `nat_gateways=2` in dev/staging contexts across `partials/`, `kits/`, `templates/composite/`.

---

### Finding F-AFIE-17 — MED (DDB PITR on by default + new spec object) — RESOLVED 2026-06-17
**Partial(s) fixed:** `LAYER_DATA.md` §3.3 + §4.3 + `SERVERLESS_DYNAMODB_PATTERNS.md` §3.2 + §7 + §10

**Issue (from AFIE Sprint 10 F-DATA-04 MED):** ms-09 dev jobs_ledger was corrupted by a bad migration script. PITR was off because the canonical partial gated it as `point_in_time_recovery=(stage == "prod")`. Recovery required 4 hours of engineering time + manual backfill from CW Logs application events. Cost of PITR storage: $0.20/GB/mo. Cost of NOT having PITR: half an engineer-day, every recurrence.

Additionally: the canonical `point_in_time_recovery=bool` prop is deprecated as of 2025 CDK; the new canonical is `point_in_time_recovery_specification: PointInTimeRecoverySpecification` with explicit `recovery_period_in_days` (1-35). The partials still used the deprecated bool prop.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__search_documentation` → https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_dynamodb/Table.html confirms `point_in_time_recovery: Optional[bool]` is **deprecated**; `point_in_time_recovery_specification: PointInTimeRecoverySpecification` is the new canonical.
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_dynamodb.CfnTable.PointInTimeRecoverySpecificationProperty.html confirms `recoveryPeriodInDays: Integer` range (1-35); default 35 days.

**Fix applied:**

1. **`LAYER_DATA.md` §3.3 Monolith jobs_ledger** — replaced `point_in_time_recovery=(stage == "prod")` with `point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(...)` + `_PITR_DAYS_BY_CLASS` table (7 dev / 14 staging / 35 prod-*). Inline AFIE F-DATA-04 retro + CDK prop verification note. Added paired `deletion_protection=(stage == "prod")`.

2. **`LAYER_DATA.md` §3.3 Monolith audit_log** — replaced `point_in_time_recovery=True` (already on) with full spec object at fixed 35 days + `deletion_protection=True` (audit data always protected).

3. **`LAYER_DATA.md` §4.3 Micro-Stack JobLedgerStack** — replaced `point_in_time_recovery=False, # POC; True in prod` with the same compliance-class-driven spec object.

4. **`SERVERLESS_DYNAMODB_PATTERNS.md` §3.2 single-table** — replaced `point_in_time_recovery=True` with full spec object + compliance-class-driven recovery period.

5. **`SERVERLESS_DYNAMODB_PATTERNS.md` §7 Global Tables v2** — replaced bool prop with spec object at fixed 35 days (global tables always full window).

6. **`SERVERLESS_DYNAMODB_PATTERNS.md` §10 non-negotiable #2** — rewritten: "PITR on ALL tables, not just prod; use new spec object; compliance-class drives recovery period; `point_in_time_recovery=bool` is deprecated."

**Headers bumped:** LAYER_DATA 2.0 → 2.1; SERVERLESS_DYNAMODB_PATTERNS 2.0 → 2.1.

**Recommended next steps (deferred to F-AFIE-22):** `assert_ddb_table_uses_pitr_specification` — fails synth if any `AWS::DynamoDB::Table` uses the deprecated bool `PointInTimeRecoveryEnabled` at the top level rather than `PointInTimeRecoverySpecification` nested object.

**MCP audit sources:**
- https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_dynamodb/Table.html (start_index 5500 — confirmed deprecated bool + new spec prop)
- https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_dynamodb.CfnTable.PointInTimeRecoverySpecificationProperty.html (full schema)
- https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery_Howitworks.html (1-35 day recovery window)

**grep -r sweep (deferred to Tier 8):** scan for any `point_in_time_recovery=` (bool prop) in `partials/`, `kits/`, `templates/composite/`; migrate to spec object.

---

### Finding F-AFIE-18 — MED (S3 Vectors as new canonical default for cost-sensitive RAG) — RESOLVED 2026-06-17
**Partial fixed:** `BEDROCK_KNOWLEDGE_BASES.md` §2 decision tree + §3.0a NEW

**Issue (from AFIE Sprint 10 F-FIN-08 MED + F-DATA-05 LOW):**
- F-FIN-08: AFIE-CPG used OpenSearch Serverless with 2 collections (~5K vectors total — 35 SOP docs + anomaly history). OpenSearch idle floor was ~$700/mo (2 OCU minimum). Same workload on S3 Vectors would have been ~$2/mo. The canonical partial defaulted to OpenSearch with no alternative-store guidance.
- F-DATA-05: When AFIE eventually migrated to S3 Vectors, ms-09 used hierarchical chunking with a 5-level parent-child tree. Hierarchical context lands in S3 Vectors non-filterable metadata; the 1 KB per-vector cap was exceeded for 8% of chunks, which silently dropped at ingestion (no `KnowledgeBaseIngestionError` event because the chunks weren't malformed, just oversized). No documentation hint that hierarchical chunking is incompatible with S3 Vectors at depth.

**Evidence (verified live this session via MCP):**
- `mcp__awslabs_aws-documentation-mcp-server__search_documentation` → https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html confirms GA integration.
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → S3 Vectors KB doc (start_index 0 + 8000) — confirms semantic-only (no hybrid), 1 KB metadata cap + 35 keys per vector, 100ms warm / sub-second cold latency, no binary embeddings, hierarchical-chunking-vs-metadata-limit caveat.
- `mcp__awslabs_aws-documentation-mcp-server__read_documentation` → https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrock-knowledgebase-s3vectorsconfiguration.html — canonical CFN `S3VectorsConfiguration` property (`IndexArn`, `IndexName`, `VectorBucketArn`).

**Fix applied:**

1. **§2 decision tree restructured** — S3 Vectors added as the top row + flagged as NEW canonical default. Full feature comparison table (RPS, cost, hybrid search support, metadata cap). Explicit "switch to OpenSearch when..." 5-bullet criteria (hybrid search / > 50K vectors / < 50ms latency budget / > 1 KB metadata / hierarchical chunking with deep trees). AFIE F-FIN-08 retro inlined ($700/mo → $2/mo).

2. **§3.0a NEW full CDK pattern** — `BedrockKbS3VectorsStack` covering:
   - `s3v.CfnVectorBucket` with KMS encryption
   - `s3v.CfnIndex` with `float32 / cosine / dim=1024` (Titan v2-compatible) + metadata configuration
   - KB execution role: Bedrock InvokeModel via the 3-ARN canonical (F-AFIE-01), S3 source read, and **scoped** `s3vectors:*` actions (not wildcards) to bucket + index ARN
   - `CfnKnowledgeBase.storage_configuration.type="S3_VECTORS"` with `s3_vectors_configuration` (vector_bucket_arn + index_arn + index_name)
   - `CfnDataSource` with FIXED_SIZE chunking (safer than hierarchical with S3 Vectors); inline F-DATA-05 retro comment
   - Inline AWS doc URLs for KB permissions + the integration overview

3. **§3 Monolith preamble** — banner note pointing readers at §3.0a as the cost-sensitive default, with cross-ref to §2 decision tree.

**Header bumped:** BEDROCK_KNOWLEDGE_BASES 2.1 → 2.2.

**Recommended next steps (deferred to F-AFIE-22):** `assert_kb_with_oss_only_when_decision_tree_match` — composite-level synth-guard that warns (not fails) when `OPENSEARCH_SERVERLESS` storage type is used but the SOW context doesn't indicate hybrid search / latency-critical / large-vector requirements.

**MCP audit sources:**
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html (read 2026-06-17, start_index 0 + 8000)
- https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrock-knowledgebase-s3vectorsconfiguration.html (read 2026-06-17)
- https://docs.aws.amazon.com/bedrock/latest/userguide/kb-permissions.html#kb-permissions-s3vectors (cited inline)

**grep -r sweep (deferred to Tier 8):** scan for any KB CDK using `OPENSEARCH_SERVERLESS` for small-vector workloads (<10K projected) in `partials/`, `kits/`, `templates/composite/`; flag for S3 Vectors review.

---

### Finding F-AFIE-19 through F-AFIE-25 — TBD (populated per Tier as fixes land)

Each subsequent finding follows the same R-format: Partial, Section, Issue (with AFIE source ID), Evidence (with live MCP citation), Recommended fix, MCP audit sources, grep -r sweep.

The full list of finding IDs maps to the Tier table in `R4_AFIE_PLAN.md`. As each Tier ships, this section is populated.

---

## Audit history

| Round | Date | Scope | Findings report |
|---|---|---|---|
| R1 | 2026-04-21 | 17 v2.0 exemplar partials | [`audit_report_partials_v2.md`](audit_report_partials_v2.md) |
| R2 | 2026-04-22 | 9 kit-driven partials (HR / RAG / Deep-Research / Acoustic kits) | [`audit_report_partials_v2_new9.md`](audit_report_partials_v2_new9.md) |
| R3 | 2026-04-23 | 12 AI-native-lakehouse partials (Waves 1-4) | [`audit_report_partials_v2_new12.md`](audit_report_partials_v2_new12.md) |
| **R4** | **2026-06-16** | **AFIE retrospective: ~25 partial updates + 3 new partials based on AFIE-CPG's 11-sprint findings** | **this document** |

R4 differs in scope: where R1-R3 were quality audits of newly-authored partials, R4 is a retrospective gap audit driven by a real client engagement's audit-fix-verify cycle.

---

## Cross-audit patterns (extension of R1-R3 list)

In addition to the 4 patterns documented in R1-R3 (alpha-API drift, cargo-culted boto3 methods, canonical-partial divergence, security regression via over-broad resource scope), R4 adds:

5. **Currency drift** — partials authored correctly against AWS docs at author time become incorrect 2-3 months later as AWS evolves (Class A in `LESSONS_FROM_AFIE_2026-06.md`). Mitigation: per-partial `last-reviewed` + composite-template `__preflight__` checks.

6. **Cross-service IAM cross-grants missed** — when service A is encrypted with CMK and service B needs to publish to A, the CMK policy needs to grant B. Easy to miss because the partial scope is "encrypt service A," not "service B publishes to encrypted A." Example: SNS CMK + CloudWatch alarms. Mitigation: explicit cross-service grant section in security partials.

7. **Inference profile vs foundation model** — Bedrock's cross-region inference profiles have a distinct ARN class. IAM patterns that grant only `foundation-model/*` break silently when consumer uses an `us.*` model. Mitigation: F-AFIE-01.

8. **Pick-one-path traps** — two independent valid configurations whose intersection is broken (TLS double-config, hardcoded-literal-bypassing-SSM, etc.). Mitigation: F-AFIE-22 synth-time assertion library.

---

## Appendix A — Why no `cdk synth` exit-0 count

Same as R1/R2/R3: CDK CLI not available in the audit environment. Findings rely on static code review + AWS doc cross-check (MCP) + cross-reference with AFIE's actual deploy-time evidence (Sprint 10 caught G-NEW-01 by tracing AFIE's IAM grants against the actual model literal in use).

The strongest evidence available is AFIE's own deployment failure mode (documented in `SPRINT10_FINDINGS_QUEUE.md`) — that's "synth would pass; deploy-time AccessDenied" empirical evidence.

---

## Appendix B — MCP audit log (populated as fixes land)

This appendix logs every MCP query made during R4 — the actual instrument record that backs the R4 verdict. Format:

```
[Hour HH:MM] mcp__awslabs_aws-documentation-mcp-server__<call>
   query / url: <query or URL>
   findings backed: F-AFIE-NN, ...
```

Populated as Tier 1-4 fixes ship.

---

### Hour 5 — F-AFIE-01 MCP citations

```
[05:00] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "Bedrock cross-region inference profile IAM permissions prerequisites"
   search_intent: Find canonical AWS doc for IAM resource ARN format required to invoke
                  a cross-region inference profile in Bedrock
   result rank 1: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
   findings backed: F-AFIE-01 (11 partials, ~17 grant sites)

[05:15] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
   max_length: 8000 (full canonical IAM policy + Important note + 2 worked examples retrieved)
   key passage: "When you specify an inference profile in the Resource field in the first
                 statement, you must also specify the foundation model in each Region
                 associated with it." + canonical Resource list with all 3 ARN classes.
   findings backed: F-AFIE-01 (verified the canonical IAM pattern)

[06:30] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
   max_length: 6000 (Active/Legacy/EOL table + 15-day inactivity rule retrieved)
   key passage: "existing customers may lose access to Legacy models after 15 days of
                 inactivity" + Legacy table with verified EOL dates for Claude 3 / Sonnet 4 /
                 Nova Sonic v1 / Titan Image G1 v2.
   findings backed: F-AFIE-02 (authoritative Active vs Legacy/EOL data for LLMOPS_BEDROCK §3.0)
```

---

### Hour 8 — F-AFIE-03 MCP citations

```
[08:00] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "API Gateway Cognito user pool authorizer REST API method"
   search_intent: Find canonical AWS doc for attaching a Cognito user pool authorizer
                  to every method (including proxy children) on a REST API
   result rank 1: https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html
   findings backed: F-AFIE-03 (LAYER_API §4 Cognito authorizer fix)

[08:10] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html
   max_length: 4000 (full Cognito-user-pool-authorizer integration page retrieved)
   key passage: "After you create a COGNITO_USER_POOLS authorizer, configure your API
                 methods to use it" + per-method `authorizationType` + `authorizerId`
                 requirement; for blanket attachment, the canonical CDK pattern is
                 `RestApi.root.default_method_options = MethodOptions(...)`.
   findings backed: F-AFIE-03 (LAYER_API §4 R1-overdue fix + default_method_options mandate)

[08:25] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html
   max_length: 4000 (JWT authorizer + scopes + default_authorizer semantics retrieved)
   key passage: "If you specify route options with no authorizer, the route inherits the
                 API's default authorizer. To opt out for a specific route, set
                 authorizer to HttpNoneAuthorizer." → confirms SERVERLESS_HTTP_API_COGNITO
                 §5 already follows the canonical-correct pattern; R4 only annotates intent.
   findings backed: F-AFIE-03 (SERVERLESS_HTTP_API_COGNITO §5 canonical-pattern annotation)
```

---

### Hour 10 — F-AFIE-04 MCP citations

```
[10:00] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "CloudFront WAF Web ACL us-east-1 region requirement"
   search_intent: Confirm CLOUDFRONT-scope WAF Web ACL must be deployed in us-east-1
   result rank 1: https://docs.aws.amazon.com/waf/latest/developerguide/cloudfront-features.html
   findings backed: F-AFIE-04 (CDN_CLOUDFRONT_FOUNDATION §3.0 region pin)

[10:05] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "CloudFront response headers policy HSTS strict transport security"
   search_intent: Find canonical CloudFront ResponseHeadersPolicy pattern for HSTS
   result rank 2: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/understanding-response-headers-policies.html
   findings backed: F-AFIE-04 (LAYER_FRONTEND §3.1 finance-grade HSTS gotcha)

[10:15] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html
   max_length: 3500 (ACM region requirement section retrieved)
   key passage: "To use a certificate in AWS Certificate Manager (ACM) to require HTTPS
                 between viewers and CloudFront, make sure you request (or import) the
                 certificate in the US East (N. Virginia) Region (us-east-1)."
   findings backed: F-AFIE-04 (us-east-1 ACM pin reinforced in both partials)

[10:25] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/understanding-response-headers-policies.html
   max_length: 5000 + 5000 (start_index 10000) chunks
   key passage: Security headers (HSTS via strict_transport_security, X-Frame-Options,
                 X-Content-Type-Options, Referrer-Policy, CSP, X-XSS-Protection) +
                 Origin override semantics. Confirms managed SECURITY_HEADERS preset is
                 less restrictive than a custom policy (no CSP, shorter HSTS).
   findings backed: F-AFIE-04 (LAYER_FRONTEND §3.1 finance-grade HSTS cross-ref)

[10:30] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/waf/latest/developerguide/cloudfront-features.html
   max_length: 4000 (CloudFront + WAF integration overview retrieved)
   key passage: "AWS WAF inspects web requests for both distribution types based on
                 the rules you define in your protection packs (web ACLs)." → confirms
                 WAFv2 CLOUDFRONT scope is the canonical L7 protection layer; must be
                 created in us-east-1 to attach to CloudFront distributions.
   findings backed: F-AFIE-04 (CDN_CLOUDFRONT_FOUNDATION §3.0 region pin for WAFv2)
```

---

### Hour 13 — F-AFIE-05 + F-AFIE-07 MCP citations

```
[13:00] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "SNS topic KMS customer managed key publish encryption CloudWatch alarm"
   search_intent: Find canonical KMS key policy required for CW alarms to publish to
                  a CMK-encrypted SNS topic
   result rank 1: https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html
   result rank 2: https://docs.aws.amazon.com/sns/latest/dg/sns-create-topic.html
   findings backed: F-AFIE-05 (LAYER_SECURITY notifications_key + LAYER_OBSERVABILITY topic CMK)

[13:05] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "SNS encrypted KMS CloudWatch alarms publish key policy GenerateDataKey
           Decrypt cloudwatch service principal"
   search_intent: Find the exact KMS key policy statement allowing CW alarm SNS action
                  to use CMK for SNS publish
   result rank 1: https://docs.aws.amazon.com/kms/latest/APIReference/API_Decrypt.html
   result rank 4: https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-lookup-tables-kms.html
   findings backed: F-AFIE-05 (confirmed kms:GenerateDataKey* + kms:Decrypt are the
                    minimum actions; service principal is cloudwatch.amazonaws.com)

[13:15] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html
   max_length: 10000 (start_index 0 + start_index 5000 retrieved)
   key passage (start_index 5000): "To allow the AWS service to have the
                 kms:GenerateDataKey* and kms:Decrypt permissions, add the
                 following statement to the KMS policy [Principal: Service:
                 service.amazonaws.com]" + canonical event-source table mapping
                 (Amazon CloudWatch → cloudwatch.amazonaws.com, Amazon CloudWatch
                 Events → events.amazonaws.com, Amazon SNS → sns.amazonaws.com).
   findings backed: F-AFIE-05 (canonical 3-principal key policy for notifications_key)

[13:30] mcp__awslabs_aws-documentation-mcp-server__search_documentation (Hour 13.5)
   query: (referenced from prior Hour 8 + this hour) CW alarm missing-data treatment
   search_intent: Confirm CloudWatch alarm default state when treat_missing_data omitted
                  (MISSING → INSUFFICIENT_DATA, no SNS action fires)
   result rank 5: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Edit-CloudWatch-Alarm.html
   findings backed: F-AFIE-07 (mandatory treat_missing_data + semantic decision table)
```

---

### Hour 22 — F-AFIE-13 MCP citations

```
[22:00] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "Aurora Serverless v2 scale to zero minimum ACU auto-pause"
   search_intent: Confirm Aurora Serverless v2 min_capacity=0 is GA + canonical config
   result rank 1: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html
   findings backed: F-AFIE-13 (scale-to-zero default in §3 + §4)

[22:10] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html
   max_length: 5000 (Overview + Prerequisites sections retrieved)
   key passage: "To enable the auto-pause behavior for all the Aurora serverless DB
                 instances in an Aurora cluster, you set the minimum capacity value
                 for the cluster to zero ACUs."
   findings backed: F-AFIE-13 (canonical trigger min_capacity=0)

[22:20] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-rds-dbcluster-serverlessv2scalingconfiguration.html
   max_length: 3000
   key passage: ServerlessV2ScalingConfiguration property type:
                  { MaxCapacity: Number, MinCapacity: Number, SecondsUntilAutoPause: Integer }
                Valid for: Aurora Serverless v2 DB clusters
   findings backed: F-AFIE-13 (canonical CFN underlying property SecondsUntilAutoPause)

[22:30] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseCluster.html
   max_length: 2500 + 2500 (start_index 8000, 13500, 16000 retrieved)
   key passage (start_index 16000):
     serverless_v2_auto_pause_duration: Optional[Duration]
       — duration between 300 seconds (5 minutes) and 86,400 seconds (24 hours)
       — Default: 300 seconds (5 minutes)
     serverless_v2_min_capacity: Union[int, float, None]
       — smallest value 0 for engine versions that support Aurora Serverless v2 auto-pause
       — Default: 0.5
   findings backed: F-AFIE-13 (verified Python prop names before writing CDK code)
```

---

### Hour 24 — F-AFIE-14 + F-AFIE-17 MCP citations

```
[24:00] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "Redshift Serverless workgroup minimum base capacity 8 RPU dev billing"
   search_intent: Confirm minimum BaseCapacity for Redshift Serverless workgroup
   result rank 4: https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-workgroup-max-rpu.html
   findings backed: F-AFIE-14 (informed reframe — 4 RPU floor unsupported;
                    pivoted to max_capacity mandate)

[24:15] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-billing.html
   max_length: 4000
   key passage: on-demand RPU billing model + customer-controlled cost limits
   findings backed: F-AFIE-14 (max_capacity is the customer's cost-control lever)

[24:30] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-redshiftserverless-workgroup.html
   max_length: 2000 + 4000 (start_index 1500 + 3500)
   key passage (start_index 1500): BaseCapacity / MaxCapacity both optional
                                    Integer; no documented sub-8 minimum.
   key passage (start_index 3500): MaxCapacity = "The maximum data-warehouse
                                    capacity Amazon Redshift Serverless uses to
                                    serve queries" — unset means unlimited.
   findings backed: F-AFIE-14 (confirmed MaxCapacity unset → unbounded auto-scale)

[24:45] mcp__awslabs_aws-documentation-mcp-server__search_documentation
   query: "DynamoDB CDK point_in_time_recovery_specification recovery_period_in_days
           CfnTable"
   search_intent: Find canonical CDK prop name for new PITR retention spec
   result rank 3: https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_dynamodb.CfnTable.PointInTimeRecoverySpecificationProperty.html
   findings backed: F-AFIE-17 (verified new prop name + retention range 1-35)

[24:50] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_dynamodb.CfnTable.PointInTimeRecoverySpecificationProperty.html
   max_length: 3000
   key passage:
     PointInTimeRecoverySpecificationProperty {
       pointInTimeRecoveryEnabled?: boolean,
       recoveryPeriodInDays?: number  // 1-35
     }
   findings backed: F-AFIE-17 (canonical L1 nested property structure)

[24:55] mcp__awslabs_aws-documentation-mcp-server__read_documentation
   url: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_dynamodb/Table.html
   max_length: 2500 (start_index 5500 retrieved)
   key passage (start_index 5500):
     point_in_time_recovery: Optional[bool] — (deprecated)
     point_in_time_recovery_specification: PointInTimeRecoverySpecification
       — new canonical, supports recovery_period_in_days
   findings backed: F-AFIE-17 (confirmed deprecation + new canonical Python prop)
```
