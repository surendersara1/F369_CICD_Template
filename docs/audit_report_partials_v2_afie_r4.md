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
| 7 | LAYER_OBSERVABILITY | WARN (R1) | F-AFIE-05 (SNS CMK grant), F-AFIE-06 (log retention), F-AFIE-07 (missing-data treatment) | TBD |
| 8 | LAYER_SECURITY | PASS (R1) | F-AFIE-05 (KMS cross-service grant pattern), F-AFIE-09 (identity scoping) | TBD |
| 9 | AGENTCORE_OBSERVABILITY | PASS (R2) | F-AFIE-06 (log retention) | TBD |
| 10 | AGENTCORE_AGENT_CONTROL | PASS (R1) | F-AFIE-08 (Cedar context envelope + IGNORE_ALL_FINDINGS trap) | TBD |
| 11 | AGENTCORE_IDENTITY | PASS (R2) | F-AFIE-09 (identity role scoping) | TBD |
| 12 | DATA_OPENSEARCH_SERVERLESS | UNAUDITED (R12) | F-AFIE-10 (VPC-endpoint-only canonical default) | TBD |
| 13 | ENTERPRISE_SECURITY_HUB_GD_ORG | UNAUDITED (R11) | F-AFIE-11 (detective controls live-deploy step) | TBD |
| 14 | AGENTCORE_GATEWAY | PASS (R2) | F-AFIE-12 (partial scoping for InvokeAgentRuntime/Gateway) | TBD |
| 15 | DATA_AURORA_SERVERLESS_V2 | PASS (R2) | F-AFIE-13 (scale-to-zero default) | TBD |
| 16 | DATA_DBT_REDSHIFT_SERVERLESS (or new) | TBD | F-AFIE-14 (4 RPU base) | TBD |
| 17 | ECS_PRODUCTION_HARDENING | UNAUDITED (R16) | F-AFIE-15 (Fargate Spot for dev) | TBD |
| 18 | LAYER_BACKEND_ECS | PASS (R1) | F-AFIE-15 (Spot pattern cross-ref) | TBD |
| 19 | LAYER_NETWORKING | PASS (R1) | F-AFIE-16 (interface endpoints over NAT) | TBD |
| 20 | LAYER_DATA | WARN (R1) | F-AFIE-17 (PITR on by default + spec object) | TBD |
| 21 | SERVERLESS_DYNAMODB_PATTERNS | UNAUDITED (R10) | F-AFIE-17 (PITR + spec object) | TBD |
| 22 | BEDROCK_KNOWLEDGE_BASES | UNAUDITED (R15) | F-AFIE-18 (S3 Vectors backend + decision tree) | TBD |
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

### Finding F-AFIE-05 through F-AFIE-25 — TBD (populated per Tier as fixes land)

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
