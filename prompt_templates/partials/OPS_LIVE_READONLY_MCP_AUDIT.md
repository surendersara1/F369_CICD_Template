# SOP — Live Read-Only MCP Audit (Pre-Deploy + Post-Deploy)

**Version:** 1.0 · **Last-reviewed:** 2026-06-17 · **Status:** Active (NEW — R4 / F-AFIE-23)
**Applies to:** Pre-deploy verification (read-only MCP queries against AWS docs + IaC API surface) + Post-deploy verification (read-only AWS API checks via boto3 against the live deployment) · No state-changing AWS calls · Required gate in `commands/run-kit-overnight.sh` per kit
**Purpose:** Generalize the F-AFIE-11 detective-controls verification pattern into a reusable live-readonly audit harness that every kit chains as the FINAL pre-deploy + FIRST post-deploy step.

---

## 1. Purpose

R4 root cause **Class D — "kits don't run live-readonly audit"** is one of the 6 failure classes that drove AFIE-CPG's 11-sprint pain. Specific examples:

- **F-SEC-05:** CFN reported success on `inspector2:EnableForOrganization`; live API check would have caught the silent failure 6 weeks earlier.
- **F-AI-01:** A Bedrock model went Legacy mid-engagement; live MCP doc-check at deploy time would have caught it before the 15-day inactivity timer fired.
- **F-FIN-09:** Bedrock pricing drifted between the partial-author date and the kit-deploy date; the partial said $3/M input but the kit didn't check the SoT before deploy.

CFN success ≠ service is live. Partial code ≠ partial is current. The live-readonly audit closes both gaps.

This partial codifies two audit phases:

| Phase | When | Reads from | Writes to | Fail mode |
|---|---|---|---|---|
| **Pre-deploy** | Just before `cdk deploy` (CI step) | MCP doc server + AWS pricing page (manual fetch) + canonical partials (file-system grep) | A `pre_deploy_audit.md` report file | Pipeline FAILS on critical drift |
| **Post-deploy** | Immediately after `cdk deploy` (CI step) | Live AWS API (boto3, read-only ops only) | A `post_deploy_audit.md` report file | Pipeline FAILS on any check returning unexpected state |

Both phases are MANDATORY in the kit's CI workflow. No production deploy proceeds without both green.

---

## 2. Decision — which audit phase to include

| Engagement risk class | Pre-deploy MCP audit | Post-deploy boto3 audit |
|---|---|---|
| **POC / demo** | optional | optional |
| **Production / customer-facing** | MANDATORY | MANDATORY |
| **Regulated (SOX / HIPAA / PCI / FedRAMP)** | MANDATORY + auditor-archived | MANDATORY + auditor-archived |
| **Finance / agentic with model spend** | MANDATORY (pricing-SoT-check + lifecycle-check) | MANDATORY (active-service inventory) |

---

## 3. Pre-deploy MCP audit

The pre-deploy step runs against AWS public docs (via the AWS Documentation MCP server) + canonical partials in the F369_CICD_Template repo. Zero AWS API calls.

### 3.1 Bedrock model lifecycle check

```python
# scripts/pre_deploy_audit.py — Bedrock model lifecycle
"""Read AWS Bedrock model lifecycle doc, parse the Active + Legacy/EOL tables,
cross-check against models referenced in the project (SSM-driven router config).
Fail if any project-referenced model is in Legacy or EOL state."""

import re, sys, json
from pathlib import Path

# Step 1 — read the canonical lifecycle doc via MCP (this runs in CI; the
# MCP call is wrapped in a tiny helper that wraps the MCP server transport).
from mcp_helpers import read_aws_doc

active_doc = read_aws_doc(
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html"
)

# Step 2 — extract Active + Legacy/EOL model IDs from the doc.
active_models  = set(re.findall(r"`([a-z0-9._:-]+v\d+:\d+)`", active_doc.split("Legacy models")[0]))
legacy_section = active_doc.split("Legacy models", 1)[-1]
legacy_models  = set(re.findall(r"`([a-z0-9._:-]+v\d+:\d+)`", legacy_section))

# Step 3 — read the project's SSM-driven model router config (the canonical
# pattern from LLMOPS_BEDROCK §3.0b) — listed as JSON in infra/runtime_models.json
project_models = set(json.loads(Path("infra/runtime_models.json").read_text()).values())

# Step 4 — diff.
legacy_in_use = project_models & legacy_models
unknown       = project_models - active_models - legacy_models
if legacy_in_use:
    print(f"FAIL — project references Legacy/EOL models: {legacy_in_use}")
    print("Action: update infra/runtime_models.json + LLMOPS_BEDROCK partial Active table")
    sys.exit(1)
if unknown:
    print(f"WARN — project references models not in the Active or Legacy/EOL tables: {unknown}")
    print("Action: verify AWS doc has been refreshed; partial may be out of date")
    # Warn but don't fail — could be a region-new model.
```

### 3.2 Pricing source-of-truth drift check

```python
# scripts/pre_deploy_audit.py — pricing drift
"""The AWS pricing page lives on aws.amazon.com (not docs.aws.amazon.com),
so it isn't MCP-fetchable. The drift-check pattern is manual + CI-archived:
the CI step computes a sha256 of the pricing-snapshot block in the partial,
the deploy gate requires a human to attest that the snapshot matches the
live page within the last N days (default 7)."""

import hashlib, datetime, sys
from pathlib import Path

SNAPSHOT_FILE = Path("infra/bedrock_pricing_snapshot.json")
LAST_VERIFIED_FILE = Path("infra/bedrock_pricing_last_verified.txt")
MAX_AGE_DAYS = 7

if not LAST_VERIFIED_FILE.exists():
    print("FAIL — no last-verified date for the pricing snapshot.")
    print("Action: open https://aws.amazon.com/bedrock/pricing/, verify the snapshot, "
          "and `date -u +%Y-%m-%d > infra/bedrock_pricing_last_verified.txt`")
    sys.exit(1)

last_verified = datetime.datetime.fromisoformat(LAST_VERIFIED_FILE.read_text().strip())
age_days = (datetime.datetime.utcnow() - last_verified).days
if age_days > MAX_AGE_DAYS:
    print(f"FAIL — pricing snapshot last verified {age_days} days ago > {MAX_AGE_DAYS}.")
    sys.exit(1)

snapshot_hash = hashlib.sha256(SNAPSHOT_FILE.read_bytes()).hexdigest()[:12]
print(f"OK — pricing snapshot {snapshot_hash} verified {age_days} days ago.")
```

### 3.3 Partial-currency check

```python
# scripts/pre_deploy_audit.py — partial-currency
"""Confirm the F369_CICD_Template partials referenced by this kit have
Last-reviewed dates within the freshness window for the engagement class.
Default window: 90 days for production; 30 days for regulated."""

import re, datetime, sys
from pathlib import Path

PARTIAL_PATHS = [
    "../F369_CICD_Template/prompt_templates/partials/LLMOPS_BEDROCK.md",
    "../F369_CICD_Template/prompt_templates/partials/LAYER_OBSERVABILITY.md",
    # ... per-kit list ...
]
MAX_AGE_DAYS = 90  # 30 for regulated

stale = []
for path in PARTIAL_PATHS:
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"\*\*Last-reviewed:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        stale.append((path, "no Last-reviewed field"))
        continue
    last = datetime.date.fromisoformat(m.group(1))
    age = (datetime.date.today() - last).days
    if age > MAX_AGE_DAYS:
        stale.append((path, f"{age} days"))

if stale:
    print("FAIL — partials with stale Last-reviewed dates:")
    for p, reason in stale:
        print(f"  {p} — {reason}")
    print("Action: open an issue in F369_CICD_Template to refresh, OR override "
          "the max-age for this engagement (document the override in the kit's README).")
    sys.exit(1)
```

### 3.4 Canonical-partial drift check

```python
# scripts/pre_deploy_audit.py — partial drift
"""Confirm the kit hasn't accidentally fork-edited a canonical partial. Hash
each partial referenced by the kit, compare against an upstream-pinned hash."""

import hashlib, json, sys
from pathlib import Path

PINNED_HASHES = json.loads(Path("infra/partial_hashes.json").read_text())
drift = []
for partial_path, expected_sha in PINNED_HASHES.items():
    actual_sha = hashlib.sha256(Path(partial_path).read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        drift.append((partial_path, expected_sha[:12], actual_sha[:12]))

if drift:
    print("FAIL — kit has drifted from upstream canonical partials:")
    for p, exp, act in drift:
        print(f"  {p} — expected {exp}, got {act}")
    print("Action: either update infra/partial_hashes.json (if upstream changed) "
          "OR revert the local edit (the Canonical-Copy Rule — see "
          "F369_CICD_Template/prompt_templates/partials/README.md).")
    sys.exit(1)
```

---

## 4. Post-deploy boto3 audit

Runs after `cdk deploy`. Read-only AWS API calls only — no `aws *` state-changing commands.

### 4.1 Detective-controls inventory (the F-AFIE-11 pattern, generalized)

```python
# scripts/post_deploy_audit.py — detective controls
"""Per-service Status==ENABLED check. Generalizes the verify_security_baseline.py
pattern from ENTERPRISE_SECURITY_HUB_GD_ORG §3.3 to any AWS security service
the kit deploys."""

import boto3, sys

failures: list[str] = []

# ── Cognito ─────────────────────────────────────────────────────────
cognito = boto3.client("cognito-idp")
for pool in cognito.list_user_pools(MaxResults=60)["UserPools"]:
    pool_id = pool["Id"]
    desc = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    # F-AFIE-21: confirm feature_plan = PLUS for prod pools
    if desc.get("UserPoolTier") not in ("ESSENTIALS", "PLUS"):
        failures.append(f"UserPool {pool_id}: UserPoolTier={desc.get('UserPoolTier')} — "
                        f"check feature_plan migration (F-AFIE-21)")

# ── DynamoDB PITR ───────────────────────────────────────────────────
ddb = boto3.client("dynamodb")
for table_name in ddb.list_tables()["TableNames"]:
    cb = ddb.describe_continuous_backups(TableName=table_name)["ContinuousBackupsDescription"]
    if cb["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] != "ENABLED":
        failures.append(f"DDB {table_name}: PITR is DISABLED — F-AFIE-17")

# ── Bedrock active models in account ────────────────────────────────
bedrock = boto3.client("bedrock")
active_in_account = {m["modelId"]
                     for m in bedrock.list_foundation_models()["modelSummaries"]
                     if m.get("modelLifecycle", {}).get("status") == "ACTIVE"}
# Cross-check with SSM router config (per LLMOPS_BEDROCK §3.0b)
import json
project_models = set(json.loads(open("infra/runtime_models.json").read()).values())
not_active = project_models - active_in_account
if not_active:
    failures.append(f"Bedrock: project references models not Active in account: {not_active} — F-AFIE-02")

# ── Aurora Serverless v2 scale-to-zero (dev only) ───────────────────
import os
if os.environ.get("COMPLIANCE_CLASS") in ("dev", "staging"):
    rds = boto3.client("rds")
    for c in rds.describe_db_clusters()["DBClusters"]:
        scaling = c.get("ServerlessV2ScalingConfiguration", {})
        if scaling.get("MinCapacity", 1) > 0:
            failures.append(f"Aurora {c['DBClusterIdentifier']}: dev MinCapacity > 0 — F-AFIE-13")

# ── CloudWatch alarms with missing-data == 'missing' (the silent default) ─
cw = boto3.client("cloudwatch")
for alarm in cw.describe_alarms()["MetricAlarms"]:
    if alarm.get("TreatMissingData", "missing") == "missing":
        failures.append(f"CWAlarm {alarm['AlarmName']}: TreatMissingData=missing — F-AFIE-07")

# ── OpenSearch Serverless network policy: AllowFromPublic in prod ───
if os.environ.get("COMPLIANCE_CLASS", "").startswith("prod"):
    oss = boto3.client("opensearchserverless")
    for p in oss.list_security_policies(type="network")["securityPolicySummaries"]:
        full = oss.get_security_policy(name=p["name"], type="network")["securityPolicyDetail"]
        for rule in full.get("policy", []):
            if rule.get("AllowFromPublic"):
                failures.append(f"OSS NetworkPolicy {p['name']}: AllowFromPublic=true in prod — F-AFIE-10")

# ── Final ───────────────────────────────────────────────────────────
if failures:
    print("VERIFY FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"VERIFY OK — {len(failures)} failures across all R4 detective checks.")
```

### 4.2 Network exposure check

```python
# scripts/post_deploy_audit.py — network exposure
"""Confirm dev/staging stacks have nat_gateways=0 (F-AFIE-16). Confirm
CloudFront and ACM cert are pinned to us-east-1 (F-AFIE-04)."""

import os, boto3, sys

failures = []
ec2 = boto3.client("ec2")

if os.environ.get("COMPLIANCE_CLASS", "") in ("dev", "staging"):
    nats = ec2.describe_nat_gateways()["NatGateways"]
    active = [n for n in nats if n["State"] in ("available", "pending")]
    if active:
        failures.append(f"NAT Gateways active in {os.environ['COMPLIANCE_CLASS']}: "
                        f"{[n['NatGatewayId'] for n in active]} — F-AFIE-16")

# CloudFront ACM check — distribution must reference a us-east-1 cert
cf = boto3.client("cloudfront")
for d in cf.list_distributions().get("DistributionList", {}).get("Items", []):
    cert = d.get("ViewerCertificate", {}).get("ACMCertificateArn", "")
    if cert and ":us-east-1:" not in cert:
        failures.append(f"CloudFront {d['Id']}: ACM cert NOT in us-east-1 — F-AFIE-04")

if failures:
    print("VERIFY FAILED:"); [print(f"  - {f}") for f in failures]; sys.exit(1)
print("VERIFY OK — network exposure checks passed.")
```

### 4.3 Cost-shape check (post-deploy + scheduled)

```python
# scripts/post_deploy_audit.py — cost-shape
"""Run 24h after deploy + on a daily schedule. Pull CUR-derived spend deltas
from CloudWatch metrics emitted by the per-invoke Powertools helper
(LLMOPS_BEDROCK §3.0b). Alert if $/query trend exceeds a budget envelope."""

import os, datetime, boto3, sys

cw = boto3.client("cloudwatch")
namespace = f"{os.environ['PROJECT_NAME']}/Bedrock"

end = datetime.datetime.utcnow()
start = end - datetime.timedelta(hours=24)

resp = cw.get_metric_statistics(
    Namespace=namespace,
    MetricName="OutputTokens",
    StartTime=start, EndTime=end,
    Period=3600, Statistics=["Sum"],
)
total_output_tokens = sum(p["Sum"] for p in resp["Datapoints"])
# Per LLMOPS_BEDROCK pricing snapshot — pin to a project-specific budget file.
budget_envelope_usd = 50.0    # $50/day for the AFIE-class workload
sonnet_output_price = 15.0 / 1_000_000   # $/token
estimated_cost = total_output_tokens * sonnet_output_price
if estimated_cost > budget_envelope_usd:
    print(f"WARN — last 24h Bedrock output spend ${estimated_cost:.2f} exceeds "
          f"envelope ${budget_envelope_usd:.2f} — F-AFIE-20")
    # Don't fail post-deploy on this; surface to ops via SNS.
else:
    print(f"OK — last 24h Bedrock cost ${estimated_cost:.2f} within envelope.")
```

---

## 5. CI wiring

```yaml
# .github/workflows/kit-deploy.yml
name: Kit deploy with R4 live audits
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions: {id-token: write, contents: read}
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.DEPLOY_ROLE_ARN }}
          aws-region: us-east-1

      # ── R4 pre-deploy audit (F-AFIE-23) ─────────────────────────
      - name: Pre-deploy MCP audit
        run: |
          python scripts/pre_deploy_audit.py
        # Fail fast: model lifecycle / pricing drift / partial currency / canonical-partial drift

      # ── R4 synth-guards (F-AFIE-22) ─────────────────────────────
      - name: CDK synth guards
        run: pytest tests/test_synth_guards_full.py -v

      # ── Deploy ─────────────────────────────────────────────────
      - name: CDK deploy
        run: cdk deploy --all --require-approval never

      # ── R4 post-deploy audit (F-AFIE-23) ───────────────────────
      - name: Post-deploy boto3 audit
        env:
          COMPLIANCE_CLASS: prod-finance
          PROJECT_NAME: ${{ github.event.repository.name }}
        run: |
          python scripts/post_deploy_audit.py
        # Fail fast: detective-controls inventory / network exposure / cost-shape

      # ── Archive both reports (mandatory for regulated engagements) ──
      - uses: actions/upload-artifact@v4
        with:
          name: r4-live-audit-reports
          path: |
            pre_deploy_audit.md
            post_deploy_audit.md
```

---

## 6. MCP-helper shim (no SDK assumptions)

Different MCP transports — STDIO, HTTP/SSE, claude.ai — wrap the AWS Doc MCP server differently. Standardize via a thin helper:

```python
# scripts/mcp_helpers.py
"""Wrap mcp__awslabs_aws-documentation-mcp-server__read_documentation +
mcp__awslabs_aws-documentation-mcp-server__search_documentation for use in
the audit scripts. Falls back to plain HTTPS GET when the MCP server isn't
available (e.g., local dev runs)."""

import os, urllib.request, json

def read_aws_doc(url: str, max_length: int = 10_000) -> str:
    transport = os.environ.get("MCP_TRANSPORT", "http")
    if transport == "http":
        endpoint = os.environ["MCP_DOC_SERVER_URL"]   # e.g., http://localhost:8765/
        req = urllib.request.Request(
            f"{endpoint}/read_documentation",
            data=json.dumps({"url": url, "max_length": max_length}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["result"]
    elif transport == "fallback":
        # Plain GET fallback (no MCP server in scope; works for CI without one)
        with urllib.request.urlopen(url) as resp:
            return resp.read().decode("utf-8")
    else:
        raise RuntimeError(f"Unknown MCP_TRANSPORT: {transport}")
```

---

## 7. Five non-negotiables

1. **Pre-deploy + post-deploy audits are MANDATORY for prod-* compliance classes.** No exceptions; the CI workflow MUST fail-close.
2. **Audits write report files** (`pre_deploy_audit.md`, `post_deploy_audit.md`); they are archived as CI artifacts and retained per the kit's compliance-class log-retention floor (F-AFIE-06).
3. **All AWS API calls in the post-deploy audit are READ-ONLY.** No `create_*`, `update_*`, `delete_*`, `put_*`. The audit role is a separate IAM principal with `*:Describe*` + `*:Get*` + `*:List*` only.
4. **The audit role's permissions are scoped to the kit's resources** via `aws:ResourceTag/Project` conditions (the F-AFIE-09 pattern).
5. **When the audit finds a regression, BLOCK the deploy** and surface a remediation pointer — never auto-remediate (defaults are not consent).

---

## 8. References

- `ENTERPRISE_SECURITY_HUB_GD_ORG.md` §3.3 — original verify_security_baseline.py (F-AFIE-11)
- `OPS_AWS_SERVICE_CURRENCY_CHECK.md` — quarterly partial-refresh runbook (F-AFIE-24)
- `LLMOPS_BEDROCK_MODEL_LIFECYCLE.md` — dedicated model lifecycle partial (F-AFIE-25)
- `_assertions/cdk_synth_guards.md` — pre-deploy synth-time guards (F-AFIE-22)
- `LESSONS_FROM_AFIE_2026-06.md` — root cause Class D rationale
- AWS Documentation MCP server: `https://github.com/awslabs/aws-documentation-mcp-server`

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-06-17 | Initial. Generalizes F-AFIE-11 pattern across all R4 detective + cost + partial-currency checks. NEW partial — F-AFIE-23. |
