# SOP — Quarterly AWS-Service Currency Check (Partial-Library Maintenance)

**Version:** 1.0 · **Last-reviewed:** 2026-06-17 · **Status:** Active (NEW — R4 / F-AFIE-24)
**Applies to:** F369_CICD_Template partial-library upkeep · Quarterly cadence · One designated maintainer per partial-family · MCP-driven verification via `awslabs/aws-documentation-mcp-server` · Output: a refreshed partial header + new audit-round entry in `audit_report_partials_v2_*.md`
**Purpose:** Codify the maintenance ritual that prevents the R4 root cause **Class A — "2024 snapshot drift"** from re-accumulating. Every partial has a 90-day half-life (or 30-day for regulated-class partials); this runbook is how the library stays current.

---

## 1. Purpose

R4 found ~12 partials with 2024-era defaults that had become wrong by mid-2026:
- DynamoDB `point_in_time_recovery=bool` → deprecated; new spec object required (F-AFIE-17)
- Cognito `advanced_security_mode=ENFORCED` → deprecated; `feature_plan=PLUS` required (F-AFIE-21)
- Aurora Serverless v2 minimum 0.5 ACU → 0 ACU + auto_pause_duration GA'd (F-AFIE-13)
- OpenSearch Serverless `AllowFromPublic=True` default → security-policy-violating in modern engagements (F-AFIE-10)
- Bedrock model lifecycle table — moves monthly (F-AFIE-02)
- Bedrock pricing — moves quarterly (F-AFIE-20)

Without a maintenance ritual, every partial accumulates drift. AFIE-CPG consumed 4 such drifted partials in one engagement.

This SOP defines:
- **Who** owns each partial-family (Bedrock / Cognito / RDS / DynamoDB / OpenSearch / Networking / Cedar)
- **When** the quarterly check fires (calendar + on-trigger)
- **What** the checker queries (MCP doc reads, CDK API doc reads, CFN template-ref reads)
- **How** the result lands in the library (header bump, audit-round entry, F-AFIE-22 synth-guard update if needed)

---

## 2. Decision — which partials need quarterly currency

| Partial family | Drift cadence | Maintainer responsibility |
|---|---|---|
| Bedrock (LLMOPS_BEDROCK, BEDROCK_*, LLMOPS_BEDROCK_MODEL_LIFECYCLE) | **Monthly** (model lifecycle) + **Quarterly** (pricing + CDK API) | Bedrock-stack lead |
| Cognito (AGENTCORE_IDENTITY §3.3, SERVERLESS_HTTP_API_COGNITO) | Quarterly | Identity-stack lead |
| RDS / Aurora (DATA_AURORA_SERVERLESS_V2) | Quarterly | Data-stack lead |
| DynamoDB (LAYER_DATA, SERVERLESS_DYNAMODB_PATTERNS) | Quarterly | Data-stack lead |
| OpenSearch (DATA_OPENSEARCH_SERVERLESS, BEDROCK_KNOWLEDGE_BASES) | Quarterly | Data-stack lead |
| Networking (LAYER_NETWORKING, CDN_CLOUDFRONT_FOUNDATION) | Quarterly | Networking-stack lead |
| Cedar / Governance (AGENTCORE_AGENT_CONTROL) | Quarterly | Security-stack lead |
| Synth-guards (`_assertions/cdk_synth_guards.md`) | Quarterly (one rule per finding-class) | R4-audit-round lead |

The on-call rotation owns the schedule; the family-stack lead owns the substance of each check.

---

## 3. Quarterly check runbook — generic structure

Each family has its own checklist (§4 onwards). Every check follows this 5-step pattern:

### Step 1 — Open the partial(s) and read the `Last-reviewed` date

```bash
grep -n "^\*\*Last-reviewed:" prompt_templates/partials/<PARTIAL>.md
```

If `Last-reviewed` is < 90 days old AND `R4 update` note still applies, skip.

### Step 2 — Re-read the canonical AWS doc(s) via MCP

For each `# AWS doc:` URL inside the partial:

```python
from mcp__awslabs_aws-documentation-mcp-server import read_documentation
doc = read_documentation(url="https://docs.aws.amazon.com/<svc>/...", max_length=10_000)
```

Compare key sections (the ones the partial cites verbatim) against the partial's claim.

### Step 3 — Re-read the canonical CDK API doc(s)

```python
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_<svc>/<Construct>.html",
    max_length=2500, start_index=<best-guess>,
)
```

Check for:
- Deprecation banners on properties cited in the partial
- New properties relevant to the partial's R4 fix
- Changed default values

### Step 4 — Cross-check with CloudFormation template reference

```python
read_documentation(
    url="https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-<svc>-<type>.html",
    max_length=4000,
)
```

The CFN template ref is the actual deployment-time contract; CDK is a translator. Drift here is what breaks production.

### Step 5 — Update the partial OR open an audit-round entry

If no drift:
- Bump `Last-reviewed` to today; commit with `[Audit: R4-maint/<family>] no drift detected — Last-reviewed bumped`.

If drift detected:
- Open an audit-round entry in `docs/audit_report_partials_v2_<family>_R5.md` (or the current round)
- Apply the partial fix
- Update the relevant synth-guard rule in `_assertions/cdk_synth_guards.md` (the R5 self-reinforcement pattern: guard before fix)
- Commit with `[Audit: R5/F-MAINT-NN] <one-line summary>`

---

## 4. Bedrock family — monthly check

**Files in scope:** `LLMOPS_BEDROCK.md`, `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md`, `BEDROCK_KNOWLEDGE_BASES.md`, `BEDROCK_AGENTS_MULTI_AGENT.md`, `BEDROCK_FLOWS_PROMPT_MGMT.md`

### 4.1 Model lifecycle (the highest-frequency drift)

```python
# scripts/maint/check_bedrock_lifecycle.py
doc = read_documentation(
    url="https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html",
    max_length=10_000,
)
```

- Compare Active model list against `LLMOPS_BEDROCK.md` §3.0 table.
- Compare Legacy/EOL dates — flag any model where the EOL date has slipped or the model has moved Legacy.
- If `claude-sonnet-4-N` or `claude-haiku-4-N` jumps a version (e.g., 4.5 → 4.6), open a fix.

### 4.2 Pricing — quarterly

```bash
# Open https://aws.amazon.com/bedrock/pricing/ manually (not MCP-fetchable)
# Compare the per-model pricing table in LLMOPS_BEDROCK.md §3.0b against the live page
# If drift > ±5% on any model, update the partial + bump
# infra/bedrock_pricing_last_verified.txt in every consumer kit
```

### 4.3 CDK API — quarterly

```python
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrock/CfnKnowledgeBase.html",
    max_length=2500, start_index=10_000,
)
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrock/CfnFoundationModelInvocationLoggingConfiguration.html",
    max_length=2500,
)
```

- Check `S3VectorsConfigurationProperty` for new required fields.
- Check `CfnDataSource.VectorIngestionConfigurationProperty` for new chunking strategies.
- Check `inference-profile` ARN format for change (the 3-ARN canonical from F-AFIE-01).

---

## 5. Cognito family — quarterly check

**Files in scope:** `AGENTCORE_IDENTITY.md` §3.3 + §4, `SERVERLESS_HTTP_API_COGNITO.md`

### 5.1 Feature-plan structure

```python
read_documentation(
    url="https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-sign-in-feature-plans.html",
    max_length=4000,
)
```

- Confirm Lite / Essentials / Plus tier structure is unchanged.
- Check the "Features by plan" table for movement (e.g., a Plus-only feature added to Essentials).

### 5.2 Threat-protection features

```python
read_documentation(
    url="https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pool-settings-threat-protection.html",
    max_length=4000,
)
```

- Confirm adaptive auth + compromised-credentials detection are still Plus-only.

### 5.3 CDK API

```python
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cognito/UserPool.html",
    max_length=2500, start_index=4500,
)
```

- Confirm `feature_plan: Optional[FeaturePlan]` is still the canonical prop.
- Check for new MFA modes (passkey is post-2024; passkey defaults may change).

---

## 6. RDS / Aurora family — quarterly check

**Files in scope:** `DATA_AURORA_SERVERLESS_V2.md`

### 6.1 Auto-pause feature

```python
read_documentation(
    url="https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html",
    max_length=5000,
)
```

- Confirm `min_capacity=0` is still the trigger for auto-pause.
- Check the "Prerequisites and Limitations" section for new engine-version requirements.

### 6.2 CDK API

```python
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseCluster.html",
    max_length=2500, start_index=16_000,
)
```

- Confirm `serverless_v2_auto_pause_duration: Optional[Duration]` is canonical (NOT `_seconds`).
- Check the engine-version compatibility for `min_capacity=0` (Aurora PostgreSQL minor versions move quarterly).

---

## 7. DynamoDB family — quarterly check

**Files in scope:** `LAYER_DATA.md` §3.3 + §4.3, `SERVERLESS_DYNAMODB_PATTERNS.md` §3.2 + §7

### 7.1 PITR specification

```python
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_dynamodb/Table.html",
    max_length=2500, start_index=5500,
)
```

- Confirm `point_in_time_recovery: Optional[bool]` is still marked DEPRECATED.
- Confirm `point_in_time_recovery_specification: PointInTimeRecoverySpecification` is the canonical replacement.
- Check `recovery_period_in_days` range (currently 1-35).

### 7.2 Global Tables v2

```python
read_documentation(
    url="https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_dynamodb/TableV2.html",
    max_length=2500,
)
```

- If `TableV2` becomes the canonical replacement for `Table`, schedule a partial-rewrite finding.

---

## 8. OpenSearch family — quarterly check

**Files in scope:** `DATA_OPENSEARCH_SERVERLESS.md`, `BEDROCK_KNOWLEDGE_BASES.md` §3 + §3.0a

### 8.1 S3 Vectors integration

```python
read_documentation(
    url="https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html",
    max_length=5000,
)
```

- Confirm limitations section: semantic-search-only, 1 KB metadata cap, 35 keys, no binary embeddings.
- Check supported embedding models list (Titan v2 / Cohere etc.).

### 8.2 Network policy

```python
read_documentation(
    url="https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-network.html",
    max_length=4000,
)
```

- Confirm AllowFromPublic and SourceVPCEs are still mutually exclusive in the same rule.

---

## 9. Networking + CloudFront family — quarterly check

**Files in scope:** `LAYER_NETWORKING.md`, `CDN_CLOUDFRONT_FOUNDATION.md`

### 9.1 Interface-endpoint catalog

```python
read_documentation(
    url="https://docs.aws.amazon.com/vpc/latest/privatelink/aws-services-privatelink-support.html",
    max_length=5000,
)
```

- Verify the 13 interface endpoints in `LAYER_NETWORKING.md` §3 are all still supported.
- Check for new endpoints relevant to Bedrock / AgentCore / Strands workloads (BedrockAgentCore, StrandsRuntime, etc.).

### 9.2 CloudFront TLS + WAF

```python
read_documentation(
    url="https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html",
    max_length=3500,
)
```

- Confirm ACM cert MUST be in us-east-1 for CloudFront viewer-cert.

```python
read_documentation(
    url="https://docs.aws.amazon.com/waf/latest/developerguide/cloudfront-features.html",
    max_length=4000,
)
```

- Confirm WAFv2 CLOUDFRONT scope is still us-east-1-only.

---

## 10. Cedar / governance family — quarterly check

**Files in scope:** `AGENTCORE_AGENT_CONTROL.md` §3 + §3.2 + §3.2a

### 10.1 Cedar policy language

```python
# Cedar spec is hosted at cedarpolicy.com (not docs.aws.amazon.com)
# Check the spec via plain HTTPS GET in the maint script
import urllib.request
spec = urllib.request.urlopen("https://docs.cedarpolicy.com/policies/syntax-conditions.html").read()
```

- Confirm undefined-attribute → false semantic in `forbid ... when {...}` (the F-GOV-02 root cause) is unchanged.

### 10.2 AgentCore CfnPolicy

```python
read_documentation(
    url="https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-policies.html",
    max_length=4000,
)
```

- Confirm `validation_mode` enum values (VALIDATE / IGNORE_ALL_FINDINGS) are unchanged.

---

## 11. CI integration — scheduled cron

```yaml
# .github/workflows/quarterly-currency-check.yml
name: F369 quarterly partial-library currency check
on:
  schedule:
    - cron: "0 8 1 */3 *"   # First of every quarter at 08:00 UTC
  workflow_dispatch: {}     # Manual trigger
jobs:
  bedrock-monthly:
    runs-on: ubuntu-latest
    if: github.event.schedule == '0 8 1 * *' || github.event_name == 'workflow_dispatch'
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install -r scripts/maint/requirements.txt
      - run: python scripts/maint/check_bedrock_lifecycle.py
  quarterly-full:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          for family in bedrock cognito rds dynamodb opensearch networking cedar; do
            python scripts/maint/check_${family}.py || true
          done
      - name: Open issue if drift detected
        if: failure()
        uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.create({
              owner: context.repo.owner, repo: context.repo.repo,
              title: `[Maint] Quarterly currency check found drift — ${new Date().toISOString().slice(0,10)}`,
              body: "Drift detected; see workflow run for details.",
              labels: ["maintenance", "audit-r5-or-later"],
            });
```

---

## 12. Five non-negotiables

1. **Bedrock model lifecycle: MONTHLY check, not quarterly.** AWS announces Legacy with ~6-month EOL windows; a quarterly miss is a 4-month exposure.
2. **Drift detection ≠ drift fix.** The runbook surfaces drift; the partial fix follows the R4 audit-round format (Plan → MCP audit → Edit → Synth-guard update → Audit report row → Commit).
3. **No silent "Last-reviewed" bumps.** Bumping the date without a fresh MCP audit is the worst-case fail mode (gives false confidence). The maintainer attests the MCP queries actually ran.
4. **Synth-guards (F-AFIE-22) update LOCKSTEP with partial fixes.** A partial that says "use new spec object" without a matching synth-guard is incomplete.
5. **Quarterly check output is ARCHIVED.** Each run writes `docs/maint/quarterly_<YYYY-MM>.md` capturing what was checked, what was found, and what was bumped. Required for SOC2 audit trail.

---

## 13. References

- `_assertions/cdk_synth_guards.md` — the 17 synth-guards updated lockstep with partial fixes (F-AFIE-22)
- `OPS_LIVE_READONLY_MCP_AUDIT.md` — runtime audit at deploy time (F-AFIE-23)
- `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md` — dedicated Bedrock lifecycle partial (F-AFIE-25)
- `docs/R4_AFIE_PLAN.md` — what triggered this maintenance ritual
- `LESSONS_FROM_AFIE_2026-06.md` — root cause Class A (2024 snapshot drift) rationale
- AWS Documentation MCP server: https://github.com/awslabs/aws-documentation-mcp-server

---

## 14. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-06-17 | Initial. Quarterly cadence for 7 partial families + monthly cadence for Bedrock lifecycle. NEW partial — F-AFIE-24. |
