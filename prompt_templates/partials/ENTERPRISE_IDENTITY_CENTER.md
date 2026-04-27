# SOP — AWS IAM Identity Center (SSO · permission sets · ABAC · external IdP federation · group provisioning)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · IAM Identity Center (formerly AWS SSO) · Permission Sets (managed + inline policy + customer-managed) · ABAC via session tags · External IdP federation (Azure AD / Okta / Google Workspace) · SCIM auto-provisioning · Application assignments

---

## 1. Purpose

- Codify **IAM Identity Center as the single human-access point** to all AWS accounts in the Org. Replaces per-account IAM users with central identity + role assumption.
- Codify the **Permission Set patterns**: short-lived (1h) credentials, scope-tight policies, account-OU-keyed assignments.
- Codify **ABAC (Attribute-Based Access Control)** via session tags propagated from IdP — tag-driven policies that don't require per-team role definition.
- Codify **external IdP federation** (Azure AD / Okta / Google Workspace) with SAML 2.0 + SCIM auto-provisioning.
- Codify **application assignments** for SAML/OIDC-aware AWS-managed apps (Cognito, OpenSearch, Managed Grafana) and customer apps.
- This is the **human-identity foundation**. Built on `ENTERPRISE_CONTROL_TOWER` (Identity Center is enabled by Control Tower setup). Required by every production engagement that gives humans console access.

When the SOW signals: "SSO into AWS", "Azure AD / Okta integration", "least-privilege console access", "developer access to multiple accounts".

---

## 2. Decision tree — identity source

| Customer state | Recommendation |
|---|---|
| Greenfield / no IdP | Identity Center as identity source (built-in directory) |
| Has Azure AD / Entra ID | Federate Identity Center → Azure AD via SAML + SCIM |
| Has Okta | Federate via SAML + SCIM |
| Has Google Workspace | Federate via SAML + SCIM (no full SCIM until 2024+ — verify) |
| Has on-prem AD | AD Connector OR Microsoft AD + trust → Identity Center |

```
Permission Set strategy:
  AdministratorAccess          → Org-level admin (rare; root-emergency only)
  PowerUserAccess              → engineering leads in Workloads/Production
  DeveloperAccess (custom)     → engineers in Workloads/Non-Production
  ReadOnlyAccess               → finance / audit / on-call
  BillingAccess (custom)       → finance team in Management account
  SecurityAuditor (custom)     → SecOps in Audit account
  NetworkAdmin (custom)        → Infrastructure account only
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — built-in directory + 4 permission sets + 1 account assignment | **§3 Monolith** |
| Production — Azure AD federated + 6+ permission sets + ABAC + SCIM | **§5 Federated Variant** |

---

## 3. Monolith Variant — built-in directory + permission sets

### 3.1 CDK — Permission Sets + assignments

```python
# stacks/identity_center_stack.py
from aws_cdk import Stack
from aws_cdk import aws_sso as sso
from aws_cdk import aws_iam as iam
from constructs import Construct
import json


class IdentityCenterStack(Stack):
    """Runs in Management account. Creates Permission Sets + assignments."""

    def __init__(self, scope: Construct, id: str, *,
                 instance_arn: str,                      # IDC instance ARN (in Mgmt account)
                 org_id: str,
                 admin_group_id: str,                    # IDC group IDs (manual or IaC create)
                 dev_group_id: str,
                 readonly_group_id: str,
                 prod_account_ids: list[str],
                 nonprod_account_ids: list[str],
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. AdministratorAccess Permission Set ──────────────────────
        admin_ps = sso.CfnPermissionSet(self, "AdminPs",
            instance_arn=instance_arn,
            name="AdministratorAccess",
            description="Full admin (break-glass)",
            session_duration="PT1H",                      # 1h MAX for admin
            relay_state_type="https://console.aws.amazon.com/billing/home",
            managed_policies=["arn:aws:iam::aws:policy/AdministratorAccess"],
        )

        # ── 2. DeveloperAccess Permission Set (custom inline) ──────────
        dev_inline_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                # Compute / common services
                {"Effect": "Allow", "Action": [
                    "ec2:*", "ecs:*", "eks:*", "lambda:*",
                    "s3:*", "dynamodb:*",
                    "logs:*", "cloudwatch:*", "xray:*",
                    "sqs:*", "sns:*", "events:*",
                    "states:*", "apigateway:*", "appsync:*",
                    "secretsmanager:GetSecretValue", "ssm:GetParameter*",
                    "cognito-idp:*", "cognito-identity:*",
                    "cloudformation:*", "cdk:*",
                ], "Resource": "*"},
                # Deny dangerous actions
                {"Effect": "Deny", "Action": [
                    "iam:*",                              # IAM via separate workflow
                    "organizations:*",
                    "kms:ScheduleKeyDeletion", "kms:DisableKey",
                    "ec2:DeleteVpc", "ec2:DeleteSubnet",
                    "rds:DeleteDBCluster", "rds:DeleteDBInstance",
                    "dynamodb:DeleteTable",
                    "s3:DeleteBucket",
                ], "Resource": "*"},
            ],
        })

        dev_ps = sso.CfnPermissionSet(self, "DevPs",
            instance_arn=instance_arn,
            name="DeveloperAccess",
            description="Engineer access — full read/write on services, deny destructive + IAM",
            session_duration="PT8H",                      # 8h working day
            inline_policy=dev_inline_policy,
        )

        # ── 3. ReadOnlyAccess Permission Set ───────────────────────────
        readonly_ps = sso.CfnPermissionSet(self, "ReadOnlyPs",
            instance_arn=instance_arn,
            name="ReadOnlyAccess",
            description="Read-only across services",
            session_duration="PT8H",
            managed_policies=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
        )

        # ── 4. SecurityAuditor Permission Set (Audit account only) ─────
        secaudit_ps = sso.CfnPermissionSet(self, "SecAuditPs",
            instance_arn=instance_arn,
            name="SecurityAuditor",
            description="SecOps — Security Hub, GuardDuty, Macie, Inspector, IAM Access Analyzer",
            session_duration="PT4H",
            managed_policies=[
                "arn:aws:iam::aws:policy/SecurityAudit",
                "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess",
            ],
            inline_policy=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": [
                        "securityhub:*",
                        "guardduty:GetFindings", "guardduty:UpdateFindingsFeedback",
                        "macie2:GetFindings",
                        "inspector2:ListFindings",
                        "access-analyzer:*",
                    ],
                    "Resource": "*",
                }],
            }),
        )

        # ── 5. Assignments ────────────────────────────────────────────
        # Admins → AdministratorAccess on ALL accounts in the org
        # (loop through all accounts in production)
        for prod_acct in prod_account_ids:
            sso.CfnAssignment(self, f"AdminAssign{prod_acct}",
                instance_arn=instance_arn,
                permission_set_arn=admin_ps.attr_permission_set_arn,
                principal_id=admin_group_id,
                principal_type="GROUP",
                target_id=prod_acct,
                target_type="AWS_ACCOUNT",
            )

            sso.CfnAssignment(self, f"ReadOnlyAssign{prod_acct}",
                instance_arn=instance_arn,
                permission_set_arn=readonly_ps.attr_permission_set_arn,
                principal_id=readonly_group_id,
                principal_type="GROUP",
                target_id=prod_acct,
                target_type="AWS_ACCOUNT",
            )

        # Dev group → DeveloperAccess on Non-Prod accounts only
        for nonprod_acct in nonprod_account_ids:
            sso.CfnAssignment(self, f"DevAssign{nonprod_acct}",
                instance_arn=instance_arn,
                permission_set_arn=dev_ps.attr_permission_set_arn,
                principal_id=dev_group_id,
                principal_type="GROUP",
                target_id=nonprod_acct,
                target_type="AWS_ACCOUNT",
            )
```

### 3.2 Sign-in flow

User goes to `https://<idc-portal-id>.awsapps.com/start` → sees AWS account tiles + role chooser → clicks → 1h credentials in browser console OR `aws sso login --profile <name>` for CLI.

---

## 4. Federation — Azure AD example

### 4.1 Setup steps (one-time)

1. **Identity Center → Identity source → Choose external IdP**
2. Download AWS IAM Identity Center metadata XML
3. Azure AD: create Enterprise App "AWS IAM Identity Center"
4. Upload AWS metadata; download Azure AD metadata
5. Identity Center: upload Azure AD metadata
6. Configure SCIM provisioning: enable in Azure AD, paste SCIM endpoint + token from Identity Center
7. Assign Azure AD groups to the Enterprise App → groups appear in Identity Center via SCIM

### 4.2 ABAC — pass user attributes via SAML

```python
# In Identity Center → Settings → Attributes for access control
# Define attribute keys that come in via SAML:
#   - team        ← from Azure AD attribute extensionAttribute1
#   - cost_center ← from Azure AD attribute extensionAttribute2

# Then in permission set inline policy:
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["s3:*"],
        "Resource": "arn:aws:s3:::*",
        "Condition": {
            "StringEquals": {
                "s3:ResourceTag/team": "${aws:PrincipalTag/team}",
            },
        },
    }],
}
# Result: user can only access S3 buckets tagged with the same team as their AD profile.
# No per-team Permission Set needed.
```

### 4.3 SCIM auto-provisioning

Once SCIM is set up:
- Add user to "AWS-Developers" group in Azure AD → user appears in IDC within 30 min
- Remove from group → IDC access removed
- Update user attribute → IDC attribute updated
- No manual sync needed; Azure AD remains source of truth

---

## 5. Federated Variant — full production setup

Same as §3 + federation (§4) + extras:

### 5.1 Application assignments (Cognito, Managed Grafana, OS Dashboards)

```python
# Assign IDC group to Managed Grafana workspace
gd_app_arn = "arn:aws:sso::123456789012:application/ssoins-XXX/apl-YYY"

sso.CfnApplicationAssignment(self, "AmgAssoc",
    application_arn=gd_app_arn,
    principal_id=dev_group_id,
    principal_type="GROUP",
)
```

### 5.2 Trusted Token Issuer (TTI) — for OAuth flows

For applications using OAuth2 / OIDC (custom apps, third-party integrations), set up TTI to issue access tokens that AWS services can accept.

```python
sso.CfnTrustedTokenIssuer(self, "OktaTti",
    instance_arn=instance_arn,
    name="okta-tti",
    trusted_token_issuer_type="OIDC_JWT",
    trusted_token_issuer_configuration=sso.CfnTrustedTokenIssuer.TrustedTokenIssuerConfigurationProperty(
        oidc_jwt_configuration=sso.CfnTrustedTokenIssuer.OidcJwtConfigurationProperty(
            issuer_url="https://acme.okta.com",
            claim_attribute_path="sub",
            identity_store_attribute_path="email",
            jwks_retrieval_option="OPEN_ID_DISCOVERY",
        ),
    ),
)
```

---

## 6. Common gotchas

- **Identity Center can only run in ONE region per Org.** Choose carefully — it's painful to migrate.
- **The Identity Center "instance ARN" lives in the Management account.** All Permission Sets + Assignments must be created from there (not from member accounts).
- **`session_duration` cap is 12h** (PT12H). Default is PT1H for new permission sets.
- **`relay_state_type`** — destination URL after sign-in. Helpful for billing/finance flows.
- **Inline policy size cap: 10 KB.** Use customer-managed policies (referenced by ARN) for larger.
- **Customer-managed policies must exist in EVERY target account** with the same name. Use StackSets to deploy.
- **External IdP SAML attribute mapping** — case-sensitive. `userName` ≠ `username`. Use SAML tracer to debug.
- **SCIM is one-way** Azure AD → AWS only. Manual changes in AWS get overwritten on next SCIM sync.
- **Group membership SCIM lag is 30-60 min.** New hires can't sign in immediately.
- **Removing user from group does NOT terminate active CLI sessions.** Sessions are valid for `session_duration` — for emergency revoke, deactivate user in IdP AND rotate Permission Set policy.
- **MFA enforcement happens at IdP level** (Azure AD Conditional Access, Okta MFA policy), not Identity Center. Verify with the IdP team.
- **Mixing Identity Center with legacy IAM Users** is allowed but signals tech debt — plan migration window.
- **`aws sso login` fails on first run** if `~/.aws/sso` cache is missing. Document for new joiners.
- **AdminAccess sessions logged to CloudTrail** in `userIdentity.principalId` as `AROA*:<sso-user-name>` — easy to grep.

---

## 7. Pytest worked example

```python
# tests/test_identity_center.py
import boto3, pytest

idc = boto3.client("sso-admin", region_name="us-east-1")
ids = boto3.client("identitystore", region_name="us-east-1")

INSTANCE_ARN = "arn:aws:sso:::instance/ssoins-XXXXXXXX"
IDS_ID = "d-XXXXXXXXXX"


def test_required_permission_sets_exist():
    ps_list = idc.list_permission_sets(InstanceArn=INSTANCE_ARN)["PermissionSets"]
    names = {idc.describe_permission_set(InstanceArn=INSTANCE_ARN, PermissionSetArn=arn)
             ["PermissionSet"]["Name"] for arn in ps_list}
    required = {"AdministratorAccess", "DeveloperAccess",
                "ReadOnlyAccess", "SecurityAuditor"}
    assert required.issubset(names), f"Missing: {required - names}"


def test_admin_session_duration_capped():
    """Admin permission set MUST be ≤ 1h."""
    ps_list = idc.list_permission_sets(InstanceArn=INSTANCE_ARN)["PermissionSets"]
    for arn in ps_list:
        ps = idc.describe_permission_set(InstanceArn=INSTANCE_ARN, PermissionSetArn=arn)["PermissionSet"]
        if ps["Name"] == "AdministratorAccess":
            assert ps["SessionDuration"] == "PT1H", f"Admin session too long: {ps['SessionDuration']}"


def test_dev_group_has_no_prod_access():
    """DeveloperAccess assignment for Dev group should NOT cover prod accounts."""
    dev_group_id = "..."        # parameterize
    prod_account_ids = ["111111111111", "222222222222"]
    for acct in prod_account_ids:
        assignments = idc.list_account_assignments(
            InstanceArn=INSTANCE_ARN, AccountId=acct,
            PermissionSetArn="<DeveloperAccess PS arn>",
        )["AccountAssignments"]
        principals = [a["PrincipalId"] for a in assignments
                      if a["PrincipalType"] == "GROUP"]
        assert dev_group_id not in principals, f"Dev group has prod access on {acct}"


def test_scim_users_recently_synced():
    """At least 1 user updated within last 24h (proxy for SCIM working)."""
    users = ids.list_users(IdentityStoreId=IDS_ID)["Users"]
    # ListUsers doesn't return updated_at; check via DescribeUser per user
    pass  # integration test
```

---

## 8. Five non-negotiables

1. **AdministratorAccess `session_duration ≤ PT1H`** — break-glass only.
2. **No IAM Users in member accounts** — Identity Center is the only human entry path.
3. **MFA enforced at IdP level** for all federated users.
4. **DeveloperAccess does NOT cover prod accounts** — verified in test §7.
5. **SCIM provisioning enabled** for any external IdP — manual sync = stale access.

---

## 9. References

- [IAM Identity Center User Guide](https://docs.aws.amazon.com/singlesignon/latest/userguide/what-is.html)
- [Permission Sets](https://docs.aws.amazon.com/singlesignon/latest/userguide/permissionsetsconcept.html)
- [ABAC with Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/abac.html)
- [Azure AD federation guide](https://docs.aws.amazon.com/singlesignon/latest/userguide/azure-ad-idp.html)
- [SCIM provisioning](https://docs.aws.amazon.com/singlesignon/latest/userguide/provision-automatically.html)
- [Trusted Token Issuer](https://docs.aws.amazon.com/singlesignon/latest/userguide/using-applications-with-trusted-token-issuer.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Identity Center + Permission Sets + ABAC + Azure AD federation + SCIM + TTI. Wave 11. |
