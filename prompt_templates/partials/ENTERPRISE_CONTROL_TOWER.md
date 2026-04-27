# SOP — AWS Control Tower (landing zone v3 · OUs · Account Factory · CfCT customizations · guardrails)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Control Tower landing zone v3 (2024) · Account Factory for Terraform (AFT) OR Customizations for Control Tower (CfCT) · OUs (Security, Workloads, Sandbox, Suspended) · Detective + Preventive guardrails · Region deny via SCP · Service Catalog provisioned products

---

## 1. Purpose

- Codify the **AWS Control Tower landing zone** as the canonical multi-account foundation. Replaces hand-rolled Organizations + AWS Config + CloudTrail per-account setup with a managed service.
- Codify the **OU shape**: Security (Log Archive + Audit accounts), Workloads (Prod, Non-Prod, Dev), Sandbox (developer experimentation), Suspended (decommissioning queue).
- Codify the **Account Factory** for declarative new-account creation (AFT preferred over manual provisioning).
- Codify **CfCT (Customizations for Control Tower)** for layering custom CloudFormation/SCP on top of the landing zone.
- Codify the **guardrail strategy**: 30+ Mandatory + Strongly Recommended + Elective controls; per-OU enforcement.
- Codify **landing zone v3 features (2024)**: opt-out of CloudTrail org trail, BYO KMS, opt-out of AWS Config aggregator, region selector.
- This is the **multi-account governance foundation**. Required by `ENTERPRISE_IDENTITY_CENTER`, `ENTERPRISE_ORG_SCPS_RCPS`, `ENTERPRISE_NETWORK_HUB_TGW`, `ENTERPRISE_CENTRALIZED_LOGGING`.

When the SOW signals: "set up AWS for our enterprise", "multi-account governance", "landing zone", "production-ready AWS foundation", "5+ teams need AWS access".

---

## 2. Decision tree — landing zone path

| Customer state | Recommendation |
|---|---|
| Brand new AWS account | Control Tower (1-day setup, managed) |
| Existing AWS Org with 5-20 accounts | Control Tower w/ existing Org adoption (2024+) |
| Existing AWS Org with 20+ accounts + custom controls | CfCT or AFT + Control Tower; enroll accounts in batches |
| Specialized regulated workload (FedRAMP, GovCloud) | Hand-rolled Org + Config + CloudTrail (Control Tower not GovCloud-supported until late 2025) |

**Recommendation: Control Tower for 95% of engagements.** Fight back if customer wants hand-rolled — they'll spend 6 months reinventing it.

```
OU shape (default):
  Root
    ├── Security (mandatory, created by CT)
    │     ├── Log Archive account     ← centralized CloudTrail + Config history
    │     └── Audit account            ← Security Hub admin + GuardDuty admin
    ├── Workloads
    │     ├── Production OU            ← prod accounts, strictest SCPs
    │     ├── Non-Production OU        ← stage / pre-prod
    │     └── Development OU           ← dev accounts
    ├── Sandbox                        ← developer experimentation, broad permissions
    ├── Infrastructure                 ← shared services (network hub, DNS, ECR, etc.)
    └── Suspended                      ← decommissioning queue (deny-all SCP)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 4-account landing zone (mgmt + Log Archive + Audit + 1 workload) | **§3 Monolith** |
| Production — 6+ OUs + AFT + custom guardrails | **§5 AFT Variant** |

---

## 3. Monolith Variant — Control Tower setup (manual / one-time)

**Note:** Control Tower setup is largely Console-driven for first deployment. CDK can manage post-setup additions (OUs, guardrail enrollment, customizations). For full IaC, see §5 AFT.

### 3.1 Setup checklist (1-day, before any CDK)

1. **Create AWS Organizations** (if not exists) — done automatically by Control Tower setup
2. **Set up AWS IAM Identity Center** (organization-level) — required by Control Tower
3. **Console: Set up landing zone** — Control Tower → Set up landing zone
   - Home region: `us-east-1` (or your primary)
   - Additional governed regions: comma-separated (CT enforces in these regions)
   - Log Archive account email + Audit account email (NEW emails, not aliases)
   - KMS encryption: BYO CMK (recommended)
   - CloudTrail org trail: enabled
   - AWS Config: enabled in all regions
4. **Wait 60 minutes** for landing zone to deploy
5. **Enable mandatory + strongly recommended guardrails** at OU level (30+ controls)

### 3.2 CDK after landing zone exists — create custom OU + enroll account

```python
# stacks/governance_stack.py
from aws_cdk import Stack
from aws_cdk import aws_organizations as orgs
from aws_cdk import aws_controltower as ct
from constructs import Construct


class GovernanceStack(Stack):
    """Runs in Management account. Adds custom OU under Workloads + enrolls account in CT."""

    def __init__(self, scope: Construct, id: str, *, root_id: str,
                 workloads_ou_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Custom OU under Workloads ──────────────────────────────
        ml_ou = orgs.CfnOrganizationalUnit(self, "MlWorkloadsOu",
            name="MlWorkloads",
            parent_id=workloads_ou_id,
            tags=[{"key": "purpose", "value": "ml-platform"}],
        )

        # ── 2. Enable a Control Tower control on the OU ───────────────
        # (Control Tower controls = guardrails. Some are PREVENTIVE (SCP),
        #  some DETECTIVE (Config rule), some PROACTIVE (CFN hook).)
        ct.CfnEnabledControl(self, "DenyEbsUnencrypted",
            control_identifier=f"arn:aws:controltower:{self.region}::control/AWS-GR_ENCRYPTED_VOLUMES",
            target_identifier=ml_ou.attr_id,
        )

        ct.CfnEnabledControl(self, "DenyRootUserAccess",
            control_identifier=f"arn:aws:controltower:{self.region}::control/AWS-GR_RESTRICT_ROOT_USER",
            target_identifier=ml_ou.attr_id,
        )

        ct.CfnEnabledControl(self, "RequireMfaForRoot",
            control_identifier=f"arn:aws:controltower:{self.region}::control/AWS-GR_ROOT_ACCOUNT_MFA_ENABLED",
            target_identifier=ml_ou.attr_id,
        )

        # ── 3. Account Factory — provision a new account into the OU ──
        # Service Catalog product 'AWS Control Tower Account Factory'
        # CDK-friendly approach: use AWS Service Catalog ProvisionedProduct
        # See §5 for full AFT (preferred for IaC)
```

### 3.3 The 30+ mandatory + recommended guardrails (apply to all OUs)

Categories:
- **Identity:** root user MFA, no root access keys
- **Logging:** CloudTrail enabled, Config enabled, log integrity validation
- **Encryption:** EBS encryption, S3 SSE, RDS encryption, no-public-snapshot
- **Network:** no internet-routed RDS, no internet-routed Redshift, restrict SSH from 0.0.0.0/0
- **Governance:** disallow region opt-out, disallow CloudTrail deletion, disallow Config deletion

Apply by OU using `ct.CfnEnabledControl` per control identifier.

---

## 4. CfCT (Customizations for Control Tower)

Use CfCT when you need to deploy custom CloudFormation templates or SCPs as part of the landing zone lifecycle (every account creation runs your customizations).

```yaml
# manifest.yaml (in CodeCommit repo monitored by CfCT)
region: us-east-1
version: 2021-03-15
resources:
  - name: deny-eu-west-3
    description: Block all activity in eu-west-3
    resource_file: policies/deny-eu-west-3.json
    deploy_method: scp
    deployment_targets:
      organizational_units:
        - Workloads
  - name: org-cloudtrail-cmk
    description: Replace CT-managed CloudTrail with CMK-encrypted variant
    resource_file: templates/cloudtrail-cmk.yaml
    deploy_method: stack_set
    deployment_targets:
      accounts: [<log-archive-account-id>]
    regions: [us-east-1]
```

CDK orchestration of CfCT:

```python
from aws_cdk import aws_codecommit as cc
from aws_cdk import aws_codepipeline as pipeline

# CfCT pipeline auto-deploys from CodeCommit on push
cfct_repo = cc.Repository(self, "CfctRepo",
    repository_name="cfct-customizations",
    description="Custom controls layered on Control Tower",
)
# Configure pipeline + lambda invokers per CfCT solution guide
```

---

## 5. Account Factory for Terraform (AFT) — preferred IaC for new accounts

AFT is a standalone solution AWS publishes that deploys IaC for every new account created via Account Factory. Each account triggers 4 Terraform stages: account customizations, global customizations, account, and account requests.

```hcl
# aft/account-requests/prod-data-platform.tf
module "prod_data_platform" {
  source = "../modules/aft-account-request"

  control_tower_parameters = {
    AccountEmail              = "aws-prod-data@example.com"
    AccountName               = "prod-data-platform"
    ManagedOrganizationalUnit = "Workloads:Production"
    SSOUserEmail              = "platform-team@example.com"
    SSOUserFirstName          = "Platform"
    SSOUserLastName           = "Team"
  }

  account_tags = {
    Environment = "prod"
    Owner       = "platform-team"
    DataClass   = "confidential"
  }

  account_customizations_name = "prod-data-platform"   # references aft/account-customizations/prod-data-platform/
}
```

```hcl
# aft/account-customizations/prod-data-platform/terraform/main.tf
# Runs in the new account after creation
resource "aws_kms_key" "default" {
  description             = "Default account CMK"
  enable_key_rotation     = true
  multi_region            = true
  deletion_window_in_days = 30
}

# Apply the standard data-platform CDK app, etc.
```

---

## 6. Common gotchas

- **Control Tower setup takes ~60 minutes** — don't time-box client work without buffer.
- **Account emails must be unique across ALL of AWS, not just your Org.** Use email aliases like `aws-prod-data@yourcompany.com`.
- **Landing zone v3 requires re-baseline** (manual button in CT console) every time you add governed regions or change KMS settings.
- **Decommissioning accounts requires moving to Suspended OU first**, applying deny-all SCP, then closing via root user — accounts stay in Org for 90 days post-close.
- **Custom OUs must be < 5 levels deep** under Root. CT itself uses 1 level.
- **`AWS-GR_*` control IDs are versioned** — `AWS-GR_RESTRICT_ROOT_USER_V2` exists; check current names before hardcoding.
- **CfCT manifest version 2021-03-15 vs 2020-01-01** — older customizations won't deploy on newer pipelines without manifest update.
- **AFT Terraform state is stored in the AFT management account**, not Org Management. Permission boundary differences matter.
- **AFT account customizations run AFTER the account is created** — they can't prevent creation. SCPs (preventive) are the only way to block bad behavior at account-create time.
- **Control Tower org trail captures CloudTrail for ALL governed accounts in ALL governed regions.** Cost can balloon. Use S3 lifecycle policies on the Log Archive bucket.
- **Repairing a drifted landing zone** = "Repair" button in CT console. Does NOT auto-remediate custom drift (e.g., manually disabling Config).
- **Don't put production resources in the Management (Payer) account.** It's for Organizations + billing only.

---

## 7. Pytest worked example (boto3-based assertion)

```python
# tests/test_landing_zone.py
import boto3, pytest

ct = boto3.client("controltower", region_name="us-east-1")
orgs = boto3.client("organizations")


def test_landing_zone_active():
    landing_zones = ct.list_landing_zones()["landingZones"]
    assert landing_zones, "No landing zone deployed"
    arn = landing_zones[0]["arn"]
    detail = ct.get_landing_zone(landingZoneIdentifier=arn)
    assert detail["landingZone"]["status"] == "ACTIVE"


def test_required_ous_exist():
    """Security, Workloads, Sandbox, Infrastructure, Suspended."""
    roots = orgs.list_roots()["Roots"]
    ous = []
    paginator = orgs.get_paginator("list_organizational_units_for_parent")
    for page in paginator.paginate(ParentId=roots[0]["Id"]):
        ous.extend(page["OrganizationalUnits"])
    names = {o["Name"] for o in ous}
    required = {"Security", "Workloads", "Sandbox", "Infrastructure", "Suspended"}
    assert required.issubset(names), f"Missing OUs: {required - names}"


def test_log_archive_account_exists():
    accounts = orgs.list_accounts()["Accounts"]
    log_archive = [a for a in accounts if "log-archive" in a["Email"].lower()]
    assert log_archive, "No Log Archive account"
    assert log_archive[0]["Status"] == "ACTIVE"


def test_mandatory_guardrails_enabled_on_workloads():
    """CT controls AWS-GR_RESTRICT_ROOT_USER, AWS-GR_ENCRYPTED_VOLUMES on Workloads OU."""
    workloads_ou_id = "ou-xxxx-xxxxxxxx"   # parameterize
    enabled = ct.list_enabled_controls(targetIdentifier=workloads_ou_id)["enabledControls"]
    ids = [e["controlIdentifier"] for e in enabled]
    required = ["AWS-GR_RESTRICT_ROOT_USER", "AWS-GR_ENCRYPTED_VOLUMES",
                "AWS-GR_ROOT_ACCOUNT_MFA_ENABLED"]
    for ctrl in required:
        assert any(ctrl in i for i in ids), f"Missing guardrail {ctrl}"
```

---

## 8. Five non-negotiables

1. **Control Tower landing zone v3 (current)** — never set up Org + Config + CloudTrail by hand.
2. **OU shape per §2** — Security / Workloads / Sandbox / Infrastructure / Suspended; never flat.
3. **30+ mandatory + strongly recommended guardrails enabled at Workloads OU**.
4. **AFT or CfCT for new account customization** — never bake account-specific things into manual scripts.
5. **No production resources in the Management account.** Period.

---

## 9. References

- [Control Tower User Guide — Landing Zone v3](https://docs.aws.amazon.com/controltower/latest/userguide/landing-zone-version-3.html)
- [Mandatory + Strongly Recommended controls](https://docs.aws.amazon.com/controltower/latest/userguide/mandatory-controls.html)
- [Account Factory for Terraform (AFT)](https://docs.aws.amazon.com/controltower/latest/userguide/aft-overview.html)
- [Customizations for Control Tower (CfCT)](https://aws.amazon.com/solutions/implementations/customizations-for-aws-control-tower/)
- [`controltower:EnabledControl` API](https://docs.aws.amazon.com/controltower/latest/APIReference/API_EnabledControl.html)
- [OU best practices](https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/recommended-ous.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Control Tower landing zone v3 + canonical OU shape + AFT + CfCT + 30+ guardrails. Wave 11. |
