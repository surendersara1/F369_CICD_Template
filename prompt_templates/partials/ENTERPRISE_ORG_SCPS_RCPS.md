# SOP — AWS Organizations SCPs + RCPs (Service Control Policies · Resource Control Policies · delegated admin · OU strategy)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Organizations · SCPs (Service Control Policies, govern principal actions) · RCPs (Resource Control Policies, GA Nov 2024 — govern resource access from outside) · Declarative Policies (GA 2024) · Delegated administration

---

## 1. Purpose

- Codify the **SCP strategy** — preventive guardrails applied at OU level. Different from IAM policies because they apply to ALL principals in target accounts (including root) and CANNOT be overridden by member account admin.
- Codify **RCPs (NEW Nov 2024)** — preventive guardrails on RESOURCES regardless of which principal (in your Org or external) tries to access them. Symmetrical to SCPs but applied to resources.
- Codify the **canonical SCP set** that every Org should run: deny region opt-out, deny CloudTrail/Config/GuardDuty disable, deny KMS key deletion, deny root user, restrict instance types, mandate encryption.
- Codify **RCP patterns** (perimeter): deny S3 access from outside Org, deny role assumption from untrusted accounts, restrict KMS data access by org boundary.
- Codify **Declarative Policies** for enforcing org-wide service config (e.g., EBS encryption by default, IMDSv2-only).
- Codify **delegated admin** — pushing security service admin (Security Hub, GuardDuty, Detective) to a dedicated Audit account so the Management account stays minimal.
- This is the **policy-as-guardrail specialisation**. Built on `ENTERPRISE_CONTROL_TOWER` (Org exists). Most production engagements need this.

When the SOW signals: "preventive controls", "block specific regions", "force MFA", "tenant isolation", "data perimeter", "PCI-DSS scope reduction".

---

## 2. Decision tree — SCP vs RCP vs IAM vs Config

| Concern | Tool |
|---|---|
| Block a principal's action regardless of its IAM perms | **SCP** |
| Block external principals from a resource regardless of who they are | **RCP** |
| Mandate a service config (EBS encrypt by default, IMDSv2-only) | **Declarative Policy** OR Config rule |
| Deny based on tag / time / IP | **SCP** with conditions |
| Detect non-compliance and remediate | **Config rule** + remediation Lambda |
| Per-user least privilege | **IAM** (managed in member account) |

```
Layer order (defense-in-depth):
  RCP (perimeter) — blocks "outside the org" or "wrong account"
       │
       ▼
  SCP (preventive) — blocks "even if IAM says yes"
       │
       ▼
  IAM (least-priv) — what THIS principal can do
       │
       ▼
  Config (detective) — flag non-compliance + remediate
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 5 canonical SCPs on Workloads OU | **§3 Monolith** |
| Production — 12+ SCPs + RCPs + Declarative Policies + per-OU customization | **§5 Production** |

---

## 3. Monolith Variant — canonical SCP set

### 3.1 CDK

```python
# stacks/scp_stack.py
from aws_cdk import Stack
from aws_cdk import aws_organizations as orgs
from constructs import Construct
import json


class ScpStack(Stack):
    """Runs in Management account. Creates SCPs + attaches to OUs."""

    def __init__(self, scope: Construct, id: str, *,
                 workloads_ou_id: str, sandbox_ou_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Deny region opt-out ────────────────────────────────────
        deny_region_optout = orgs.CfnPolicy(self, "DenyRegionOptOut",
            name="DenyRegionOptOut",
            description="Block disabling of opted-in regions",
            type="SERVICE_CONTROL_POLICY",
            content=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "DenyRegionDisable",
                    "Effect": "Deny",
                    "Action": [
                        "account:DisableRegion",
                        "account:EnableRegion",
                    ],
                    "Resource": "*",
                }],
            }),
            target_ids=[workloads_ou_id],
        )

        # ── 2. Restrict allowed regions ───────────────────────────────
        deny_unapproved_regions = orgs.CfnPolicy(self, "DenyUnapprovedRegions",
            name="DenyUnapprovedRegions",
            description="Only allow operations in us-east-1, us-west-2, eu-west-1",
            type="SERVICE_CONTROL_POLICY",
            content=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "DenyOutsideAllowedRegions",
                    "Effect": "Deny",
                    "NotAction": [
                        # global services that must be allowed
                        "iam:*", "organizations:*", "route53:*",
                        "cloudfront:*", "waf:*", "wafv2:*",
                        "support:*", "trustedadvisor:*",
                        "globalaccelerator:*", "shield:*",
                        "cur:*", "ce:*", "budgets:*",
                        "kms:*", "sts:*",
                        # SSO + Identity Center
                        "sso:*", "sso-directory:*", "identitystore:*",
                        # Health + signin
                        "health:*", "signin:*",
                    ],
                    "Resource": "*",
                    "Condition": {
                        "StringNotEquals": {
                            "aws:RequestedRegion": ["us-east-1", "us-west-2", "eu-west-1"],
                        },
                    },
                }],
            }),
            target_ids=[workloads_ou_id, sandbox_ou_id],
        )

        # ── 3. Deny disabling security services ───────────────────────
        deny_security_disable = orgs.CfnPolicy(self, "DenySecurityDisable",
            name="DenySecurityDisable",
            description="Prevent disabling CloudTrail, Config, GuardDuty, Security Hub",
            type="SERVICE_CONTROL_POLICY",
            content=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyCloudTrailDisable",
                        "Effect": "Deny",
                        "Action": [
                            "cloudtrail:StopLogging", "cloudtrail:DeleteTrail",
                            "cloudtrail:UpdateTrail", "cloudtrail:PutEventSelectors",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Sid": "DenyConfigDisable",
                        "Effect": "Deny",
                        "Action": [
                            "config:DeleteConfigRule",
                            "config:DeleteConfigurationRecorder",
                            "config:DeleteDeliveryChannel",
                            "config:StopConfigurationRecorder",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Sid": "DenyGuardDutyDisable",
                        "Effect": "Deny",
                        "Action": [
                            "guardduty:DisableOrganizationAdminAccount",
                            "guardduty:DeleteDetector",
                            "guardduty:UpdateDetector",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Sid": "DenySecurityHubDisable",
                        "Effect": "Deny",
                        "Action": [
                            "securityhub:DisableSecurityHub",
                            "securityhub:DisassociateFromMasterAccount",
                        ],
                        "Resource": "*",
                    },
                ],
            }),
            target_ids=[workloads_ou_id],
        )

        # ── 4. Deny root user actions ─────────────────────────────────
        deny_root_user = orgs.CfnPolicy(self, "DenyRootUser",
            name="DenyRootUser",
            description="Block all actions by the root user",
            type="SERVICE_CONTROL_POLICY",
            content=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "DenyRoot",
                    "Effect": "Deny",
                    "Action": "*",
                    "Resource": "*",
                    "Condition": {
                        "StringLike": {"aws:PrincipalArn": "arn:aws:iam::*:root"},
                    },
                }],
            }),
            target_ids=[workloads_ou_id],
        )

        # ── 5. Mandate encryption (EBS, S3, RDS) ─────────────────────
        deny_unencrypted = orgs.CfnPolicy(self, "DenyUnencrypted",
            name="DenyUnencrypted",
            description="Block creation of unencrypted EBS / S3 / RDS",
            type="SERVICE_CONTROL_POLICY",
            content=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyUnencryptedEbs",
                        "Effect": "Deny",
                        "Action": ["ec2:CreateVolume", "ec2:RunInstances"],
                        "Resource": "*",
                        "Condition": {
                            "Bool": {"ec2:Encrypted": "false"},
                        },
                    },
                    {
                        "Sid": "DenyUnencryptedRds",
                        "Effect": "Deny",
                        "Action": ["rds:CreateDBInstance", "rds:CreateDBCluster"],
                        "Resource": "*",
                        "Condition": {
                            "Bool": {"rds:StorageEncrypted": "false"},
                        },
                    },
                    {
                        "Sid": "DenyS3UploadWithoutSse",
                        "Effect": "Deny",
                        "Action": "s3:PutObject",
                        "Resource": "*",
                        "Condition": {
                            "StringNotEquals": {
                                "s3:x-amz-server-side-encryption": ["AES256", "aws:kms"],
                            },
                            "Null": {
                                "s3:x-amz-server-side-encryption": "false",
                            },
                        },
                    },
                ],
            }),
            target_ids=[workloads_ou_id],
        )

        # ── 6. Restrict expensive instance types in Sandbox ───────────
        sandbox_instance_limit = orgs.CfnPolicy(self, "SandboxInstanceLimit",
            name="SandboxInstanceLimit",
            description="Sandbox: max m6i.large; no GPU; no Outposts",
            type="SERVICE_CONTROL_POLICY",
            content=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "DenyExpensiveInstances",
                    "Effect": "Deny",
                    "Action": "ec2:RunInstances",
                    "Resource": "arn:aws:ec2:*:*:instance/*",
                    "Condition": {
                        "ForAnyValue:StringNotLike": {
                            "ec2:InstanceType": [
                                "t3.*", "t3a.*", "t4g.*",
                                "m6i.large", "m6i.xlarge",
                                "c6i.large",
                            ],
                        },
                    },
                }],
            }),
            target_ids=[sandbox_ou_id],
        )
```

---

## 4. RCPs — Resource Control Policies (NEW Nov 2024)

RCPs apply to RESOURCES regardless of which principal (your-Org or external) tries to access them. Currently support: S3, KMS, SQS, Secrets Manager, STS.

### 4.1 RCP — block S3 access from outside Org

```python
deny_external_s3 = orgs.CfnPolicy(self, "DenyExternalS3",
    name="DenyExternalS3Access",
    description="Block S3 access from any principal outside our Org",
    type="RESOURCE_CONTROL_POLICY",                       # NEW
    content=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyOutsideOrg",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": "*",
            "Condition": {
                "StringNotEqualsIfExists": {
                    "aws:PrincipalOrgID": "o-XXXXXXXXXX",
                },
                "BoolIfExists": {
                    "aws:PrincipalIsAWSService": "false",   # allow AWS service principals
                },
            },
        }],
    }),
    target_ids=[workloads_ou_id],
)
```

### 4.2 RCP — restrict STS AssumeRole to known accounts

```python
restrict_assumerole = orgs.CfnPolicy(self, "RestrictAssumeRole",
    name="RestrictAssumeRoleSourceAccount",
    description="Block role assumption from untrusted accounts",
    type="RESOURCE_CONTROL_POLICY",
    content=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyAssumeFromOutsideOrgOrPartners",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "sts:AssumeRole",
            "Resource": "*",
            "Condition": {
                "StringNotEqualsIfExists": {
                    "aws:PrincipalOrgID": ["o-XXXXXXXXXX"],
                    # Or include partner Org ID:
                    # "aws:PrincipalOrgID": ["o-XXXXXXXXXX", "o-PARTNERORG"],
                },
            },
        }],
    }),
    target_ids=[workloads_ou_id],
)
```

---

## 5. Declarative Policies (GA 2024)

Declarative Policies enforce service-level configuration at the Org level. Examples: "all new EBS volumes must be encrypted by default", "all EC2 must use IMDSv2".

```python
ebs_encrypt_default = orgs.CfnPolicy(self, "EbsEncryptDefault",
    name="EbsEncryptByDefault",
    description="All EBS volumes encrypted by default in all accounts",
    type="DECLARATIVE_POLICY_EC2",
    content=json.dumps({
        "ec2_attributes": {
            "ebs_encryption_by_default": "enabled",
        },
    }),
    target_ids=[root_id],   # apply org-wide
)

imdsv2_required = orgs.CfnPolicy(self, "Imdsv2Required",
    name="Imdsv2Required",
    description="IMDSv2 required for all new EC2 instances",
    type="DECLARATIVE_POLICY_EC2",
    content=json.dumps({
        "ec2_attributes": {
            "default_instance_metadata_settings": {
                "http_tokens": "required",
                "http_put_response_hop_limit": 2,
            },
        },
    }),
    target_ids=[root_id],
)
```

---

## 6. Delegated administration

Move security service admin out of Management account into the Audit account.

```python
from aws_cdk import aws_securityhub as sh
from aws_cdk import aws_guardduty as gd

# Run in Management account
orgs.CfnDelegatedAdministrator(self, "SecurityHubAdmin",
    account_id=audit_account_id,
    service_principal="securityhub.amazonaws.com",
)

orgs.CfnDelegatedAdministrator(self, "GuardDutyAdmin",
    account_id=audit_account_id,
    service_principal="guardduty.amazonaws.com",
)

orgs.CfnDelegatedAdministrator(self, "InspectorAdmin",
    account_id=audit_account_id,
    service_principal="inspector2.amazonaws.com",
)

orgs.CfnDelegatedAdministrator(self, "MacieAdmin",
    account_id=audit_account_id,
    service_principal="macie.amazonaws.com",
)

orgs.CfnDelegatedAdministrator(self, "DetectiveAdmin",
    account_id=audit_account_id,
    service_principal="detective.amazonaws.com",
)

orgs.CfnDelegatedAdministrator(self, "AccessAnalyzerAdmin",
    account_id=audit_account_id,
    service_principal="access-analyzer.amazonaws.com",
)
```

---

## 7. Common gotchas

- **SCPs do NOT grant permissions.** They only filter what the IAM principal could otherwise do. Without an IAM allow, SCPs are no-op.
- **`FullAWSAccess` SCP attached at root** is REQUIRED — without it, member accounts have no permissions at all. Don't detach.
- **SCP `NotAction` patterns are tricky.** Common bug: forgetting to include a global service in NotAction → it gets blocked.
- **RCPs are NEW (Nov 2024).** Older AWS docs may not mention them. Use `aws organizations describe-policy --policy-id <id>` to verify type.
- **Declarative Policies are EC2-only as of GA.** EBS + IMDS + serial console + AMI block public access. More services coming.
- **`aws:PrincipalOrgID` requires `aws:PrincipalIsAWSService` exception** for AWS service-linked roles. Without it, you'll block Lambda's own STS calls.
- **Region restriction SCPs break global services** if NotAction is incomplete. Test thoroughly in stage; many global service IDs aren't intuitive (`a4b`, `chime`, `wellarchitected`).
- **SCP changes propagate within seconds** but cached IAM evaluations may take ~5 min. Test from a fresh session.
- **Delegated admin can be revoked** but service config (e.g., Security Hub aggregation) may need manual cleanup.
- **SCP size limit: 5 KB** (smaller than IAM 6.144 KB). Split into multiple SCPs if needed; Org allows up to 5 SCPs per OU.
- **SCP on Management account** is allowed but you can't deny `organizations:*` there or you'll lock yourself out.
- **`Condition: aws:RequestedRegion`** doesn't work for IAM, KMS (some operations), and a few others — they're treated as "global" even when called regionally.

---

## 8. Pytest worked example

```python
# tests/test_scps.py
import boto3, pytest

orgs = boto3.client("organizations")


def test_required_scps_attached_to_workloads_ou(workloads_ou_id):
    policies = orgs.list_policies_for_target(
        TargetId=workloads_ou_id,
        Filter="SERVICE_CONTROL_POLICY",
    )["Policies"]
    names = {p["Name"] for p in policies}
    required = {
        "DenyRegionOptOut", "DenyUnapprovedRegions",
        "DenySecurityDisable", "DenyRoot", "DenyUnencrypted",
    }
    assert required.issubset(names), f"Missing SCPs: {required - names}"


def test_rcp_external_s3_blocked(workloads_ou_id):
    """RCP must deny S3 access from outside Org."""
    policies = orgs.list_policies_for_target(
        TargetId=workloads_ou_id,
        Filter="RESOURCE_CONTROL_POLICY",
    )["Policies"]
    found = False
    for p in policies:
        detail = orgs.describe_policy(PolicyId=p["Id"])
        if "DenyOutsideOrg" in detail["Policy"]["Content"]:
            found = True
    assert found, "DenyExternalS3Access RCP not attached"


def test_security_services_have_delegated_admin():
    """Verify Security Hub, GuardDuty, Inspector delegated to Audit account."""
    delegated = orgs.list_delegated_administrators()["DelegatedAdministrators"]
    by_service = {}
    for d in delegated:
        services = orgs.list_delegated_services_for_account(AccountId=d["Id"])["DelegatedServices"]
        for s in services:
            by_service.setdefault(s["ServicePrincipal"], []).append(d["Id"])
    required = ["securityhub.amazonaws.com", "guardduty.amazonaws.com",
                "inspector2.amazonaws.com", "macie.amazonaws.com"]
    for svc in required:
        assert by_service.get(svc), f"No delegated admin for {svc}"
```

---

## 9. Five non-negotiables

1. **5 canonical SCPs on Workloads OU**: deny region opt-out, deny unapproved regions, deny security service disable, deny root user, deny unencrypted.
2. **`DenyExternalS3Access` RCP** + `RestrictAssumeRoleSourceAccount` RCP on Workloads OU.
3. **Declarative Policies enforcing EBS encrypt-by-default + IMDSv2** at root.
4. **Security services delegated to Audit account** — Security Hub, GuardDuty, Inspector, Macie, Detective, Access Analyzer.
5. **All SCPs unit-tested** with `aws iam simulate-principal-policy` before attaching to OU.

---

## 10. References

- [SCPs — User Guide](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html)
- [RCPs (Nov 2024 GA)](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_rcps.html)
- [Declarative Policies](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_declarative.html)
- [Delegated administration](https://docs.aws.amazon.com/organizations/latest/userguide/services-that-can-integrate.html)
- [SCP examples (canonical set)](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps_examples.html)
- [Data perimeter blueprint (RCP-driven)](https://aws.amazon.com/identity/data-perimeters-on-aws/)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. SCPs (5 canonical) + RCPs (Nov 2024) + Declarative Policies + delegated admin. Wave 11. |
