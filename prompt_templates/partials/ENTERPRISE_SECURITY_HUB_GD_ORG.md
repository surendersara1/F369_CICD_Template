# SOP — Org-wide Security Hub + GuardDuty + Inspector + Macie + Detective (delegated admin · finding aggregation · SIEM export)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Security Hub Central Configuration (2024) · GuardDuty (Foundational + EKS Audit + EKS Runtime + S3 + Lambda + RDS + EBS Malware) · Inspector v2 (EC2 + ECR + Lambda + Lambda Code) · Macie · Detective · Access Analyzer · Audit Manager · CSPM via PCI/CIS standards

---

## 1. Purpose

- Codify the **org-wide enablement** of all 7 AWS-native security services with delegated admin in the Audit account.
- Codify **Security Hub Central Configuration (Sept 2024)** — declarative org-wide enable + standard subscriptions + finding aggregation.
- Codify the **GuardDuty feature stack** — Foundational + EKS Audit + EKS Runtime + S3 Protection + Lambda Protection + RDS Protection + EBS Malware Protection.
- Codify **Inspector v2** for continuous CVE scanning (EC2 + ECR + Lambda + Lambda Code, the latter GA 2024).
- Codify **Macie** for S3 PII/PHI discovery + classification.
- Codify **Detective** for graph-based investigation across CloudTrail/VPC Flow/GuardDuty.
- Codify **Access Analyzer** for cross-account access surface + IAM unused permissions.
- Codify the **finding routing**: Security Hub → EventBridge → SNS/Lambda/Slack/PagerDuty + Security Lake (canonical SIEM destination).
- Codify the **PCI / CIS / NIST CSF / AWS FSBP standard** subscriptions.
- This is the **detective-controls layer**. Built on `ENTERPRISE_CONTROL_TOWER`, `ENTERPRISE_ORG_SCPS_RCPS` (delegated admin), `ENTERPRISE_CENTRALIZED_LOGGING`. Pairs with `EKS_SECURITY` for K8s coverage.

When the SOW signals: "centralized security findings", "SOC 2 / PCI / HIPAA evidence", "CSPM", "container threat detection", "PII discovery in S3", "investigate AWS incident".

---

## 2. Decision tree — what to enable

| Service | When | Cost concern |
|---|---|---|
| Security Hub | Always | $0.0010/finding ingested + $0.0030/check |
| GuardDuty Foundational | Always | $0.85/M CloudTrail events + $0.10/GB VPC Flow |
| GuardDuty EKS Audit + Runtime | EKS clusters present | extra ~$1/node/mo |
| GuardDuty S3 Protection | Sensitive S3 buckets | $1.20/M S3 events |
| GuardDuty Lambda Protection | Lambda-heavy stack | extra small |
| GuardDuty EBS Malware | Compliance requires | $0.05/GB scanned |
| Inspector v2 EC2 | EC2 fleet present | $1.258/instance/mo |
| Inspector v2 ECR | Container images | $0.09/image scan |
| Inspector v2 Lambda + Code | Lambda-heavy stack | $0.30/function/mo |
| Macie | S3 with sensitive data | $1/GB scanned (one-time) + ongoing |
| Detective | Investigation needs | $2/GB ingested |
| Access Analyzer | Always | free for external; $2/IAM finding for unused |

```
Routing:

 Workload accts ──► GuardDuty agent ──┐
 Workload accts ──► Inspector scanner ──┤
 Workload accts ──► Macie classifier ──┤
 Workload accts ──► Access Analyzer ──┤
                                       │
                                       ▼
                       ┌───────────────────────────────────┐
                       │ Audit Account (delegated admin)   │
                       │   - Security Hub (aggregator)      │
                       │   - GuardDuty admin                │
                       │   - Inspector admin                │
                       │   - Macie admin                    │
                       │   - Detective admin                │
                       │   - Access Analyzer admin          │
                       └────────────────┬──────────────────┘
                                        │
                       ┌────────────────┼────────────────────────┐
                       ▼                ▼                        ▼
                ┌──────────┐   ┌──────────────┐         ┌────────────────┐
                │ EventBridge│   │ Security Lake │         │ SOC dashboard  │
                │ → SNS      │   │ (OCSF Iceberg)│         │ (QuickSight /  │
                │ → PagerDuty│   │               │         │  Splunk via SL)│
                │ → Slack    │   │               │         │                │
                └──────────┘   └──────────────┘         └────────────────┘
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Security Hub + GuardDuty + Access Analyzer in 4 accounts | **§3 Monolith** |
| Production — full 7 services + Central Config + Standards + SIEM export | **§5 Production** |

---

## 3. Monolith Variant — delegated admin + org-wide enable

### 3.1 Run in Management account — delegate to Audit account

```python
# stacks/security_org_admin_stack.py — Management account
from aws_cdk import Stack
from aws_cdk import aws_organizations as orgs
from constructs import Construct


class SecurityOrgAdminStack(Stack):
    def __init__(self, scope: Construct, id: str, *, audit_account_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        for service in [
            "securityhub.amazonaws.com",
            "guardduty.amazonaws.com",
            "inspector2.amazonaws.com",
            "macie.amazonaws.com",
            "detective.amazonaws.com",
            "access-analyzer.amazonaws.com",
            "auditmanager.amazonaws.com",
        ]:
            orgs.CfnDelegatedAdministrator(self, f"DelAdmin{service.split('.')[0]}",
                account_id=audit_account_id,
                service_principal=service,
            )
```

### 3.2 Run in Audit account — enable services org-wide

```python
# stacks/security_audit_stack.py — Audit account (delegated admin)
from aws_cdk import Stack
from aws_cdk import aws_securityhub as sh
from aws_cdk import aws_guardduty as gd
from aws_cdk import aws_inspectorv2 as inspector
from aws_cdk import aws_macie as macie
from constructs import Construct


class SecurityAuditStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 all_workload_account_ids: list[str], all_regions: list[str],
                 sns_topic_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Security Hub Central Configuration (Sept 2024) ────────
        # Enable Security Hub at delegated admin
        sh_hub = sh.CfnHub(self, "Hub",
            tags={"managed-by": "central-config"},
            auto_enable_controls=True,
        )

        # Configuration Policy — declarative org-wide enable
        sh_config_policy = sh.CfnConfigurationPolicy(self, "OrgPolicy",
            name="org-default",
            description="Default Security Hub config for all workload accounts",
            configuration_policy=sh.CfnConfigurationPolicy.PolicyProperty(
                security_hub=sh.CfnConfigurationPolicy.SecurityHubPolicyProperty(
                    enabled_standard_identifiers=[
                        "arn:aws:securityhub:::standards/aws-foundational-security-best-practices/v/1.0.0",
                        "arn:aws:securityhub:::standards/cis-aws-foundations-benchmark/v/3.0.0",
                        "arn:aws:securityhub:::standards/pci-dss/v/4.0.1",
                        f"arn:aws:securityhub:{self.region}::standards/nist-800-53/v/5.0.0",
                    ],
                    service_enabled=True,
                    security_controls_configuration=sh.CfnConfigurationPolicy.SecurityControlsConfigurationProperty(
                        disabled_security_control_identifiers=[
                            # Disable specific controls that don't apply (e.g. workspaces if no WS)
                            "WorkSpaces.1",
                        ],
                    ),
                ),
            ),
        )

        # Associate config policy to root OU (all accounts)
        sh.CfnPolicyAssociation(self, "AssocAll",
            configuration_policy_id=sh_config_policy.attr_id,
            target_id=root_ou_id,
            target_type="ROOT",
        )

        # Finding aggregator (cross-region)
        sh.CfnFindingAggregator(self, "Aggregator",
            region_linking_mode="ALL_REGIONS",
        )

        # ── 2. GuardDuty — full feature stack ─────────────────────────
        gd_detector = gd.CfnDetector(self, "Detector",
            enable=True,
            finding_publishing_frequency="FIFTEEN_MINUTES",
            features=[
                gd.CfnDetector.CFNFeatureConfigurationProperty(
                    name="S3_DATA_EVENTS", status="ENABLED",
                ),
                gd.CfnDetector.CFNFeatureConfigurationProperty(
                    name="EKS_AUDIT_LOGS", status="ENABLED",
                ),
                gd.CfnDetector.CFNFeatureConfigurationProperty(
                    name="EBS_MALWARE_PROTECTION", status="ENABLED",
                ),
                gd.CfnDetector.CFNFeatureConfigurationProperty(
                    name="RDS_LOGIN_EVENTS", status="ENABLED",
                ),
                gd.CfnDetector.CFNFeatureConfigurationProperty(
                    name="EKS_RUNTIME_MONITORING", status="ENABLED",
                    additional_configuration=[
                        gd.CfnDetector.CFNFeatureAdditionalConfigurationProperty(
                            name="EKS_ADDON_MANAGEMENT", status="ENABLED",
                        ),
                    ],
                ),
                gd.CfnDetector.CFNFeatureConfigurationProperty(
                    name="LAMBDA_NETWORK_LOGS", status="ENABLED",
                ),
            ],
        )

        # Org-wide auto-enable for new accounts
        gd.CfnOrganizationConfiguration(self, "GdOrgConfig",
            detector_id=gd_detector.ref,
            auto_enable_organization_members="ALL",
            features=[
                gd.CfnOrganizationConfiguration.OrganizationFeatureConfigurationProperty(
                    name=f, auto_enable="ALL",
                ) for f in ["S3_DATA_EVENTS", "EKS_AUDIT_LOGS",
                            "EBS_MALWARE_PROTECTION", "RDS_LOGIN_EVENTS",
                            "EKS_RUNTIME_MONITORING", "LAMBDA_NETWORK_LOGS"]
            ],
        )

        # ── 3. Inspector v2 — EC2 + ECR + Lambda + Lambda Code ────────
        inspector.CfnCisScanConfiguration(self, "CisCfg",
            scan_name="weekly-cis",
            schedule={"weekly": {"day": "SUNDAY", "startTime": {"timeOfDay": "01:00", "timeZone": "UTC"}}},
            security_level="LEVEL_2",
            targets=inspector.CfnCisScanConfiguration.CisTargetsProperty(
                account_ids=all_workload_account_ids,
                target_resource_tags={"InspectorScan": ["true"]},
            ),
        )

        # Org-wide auto-enable
        # (CDK doesn't have CfnOrgConfig for inspector; use AwsCustomResource to call
        #  inspector2:UpdateOrganizationConfiguration)

        # ── 4. Macie — auto-enable + sensitive data discovery ────────
        macie.CfnSession(self, "MacieSession",
            finding_publishing_frequency="FIFTEEN_MINUTES",
            status="ENABLED",
        )

        # Auto-enable for new accounts
        macie.CfnOrganizationConfiguration(self, "MacieOrgConfig",
            auto_enable=True,
        )

        # Recurring sensitive data discovery job (S3 buckets tagged 'macie-scan: true')
        # macie.CfnClassificationJob(...) per scan job; abbreviated

        # ── 5. Detective ─────────────────────────────────────────────
        # Detective auto-enables when GuardDuty findings stream in;
        # delegated admin enables Detective via console or CFN custom resource

        # ── 6. Finding routing — EventBridge → SNS ─────────────────
        from aws_cdk import aws_events as events
        from aws_cdk import aws_events_targets as targets

        events.CfnRule(self, "ShCriticalFindings",
            event_bus_name="default",
            name="security-hub-critical-findings",
            event_pattern={
                "source": ["aws.securityhub"],
                "detail-type": ["Security Hub Findings - Imported"],
                "detail": {
                    "findings": {
                        "Severity": {"Label": ["CRITICAL", "HIGH"]},
                        "Workflow": {"Status": ["NEW"]},
                    },
                },
            },
            targets=[{
                "arn": sns_topic_arn,
                "id": "sns",
                "inputTransformer": {
                    "inputPathsMap": {
                        "title": "$.detail.findings[0].Title",
                        "severity": "$.detail.findings[0].Severity.Label",
                        "account": "$.detail.findings[0].AwsAccountId",
                        "region": "$.detail.findings[0].Region",
                        "type": "$.detail.findings[0].Types[0]",
                        "resource": "$.detail.findings[0].Resources[0].Id",
                    },
                    "inputTemplate": '"[<severity>] <title> in account <account>/<region> on <resource>"',
                },
            }],
        )

        # ── 7. Macie + GuardDuty findings → Security Lake ────────────
        # Already covered in ENTERPRISE_CENTRALIZED_LOGGING via CfnAwsLogSource
```

---

## 4. Common gotchas

- **Security Hub Central Configuration replaces the old "auto-enable" mechanism** (Sept 2024). Don't mix the two — uninstall old config first.
- **GuardDuty auto-enable for new accounts is opt-in per feature.** Without `OrganizationFeatureConfigurationProperty(auto_enable: ALL)`, new accounts skip the feature.
- **Inspector v2 enrollment for new accounts requires `inspector2:UpdateOrganizationConfiguration`** (no CFN); use AwsCustomResource or run once via CLI.
- **Macie continuous discovery costs $$$** at scale (per-GB-scanned). Use one-shot classification jobs for cost control or scope to tagged buckets.
- **Detective consumes GuardDuty + CloudTrail + VPC Flow logs continuously.** Cost = $2/GB ingested. Scope to investigation regions.
- **Security Hub findings "Workflow Status" defaults to NEW** until manually changed. Use EventBridge filter on Status: NEW to avoid alert spam.
- **Standards subscription 'CIS v3.0.0'** has different control IDs than v1.4.0. Don't disable controls by IDs from old version.
- **Access Analyzer external access analyzer** vs **unused access analyzer** — two separate analyzers, two costs. Both worth enabling.
- **Security Hub finding aggregator REGION_LINKING_MODE: ALL_REGIONS** aggregates findings to the home region. Costs nothing extra; saves console-switching.
- **GuardDuty findings have severity 0.1-8.9** — Security Hub maps these to CRITICAL/HIGH/MEDIUM/LOW. Filter on Security Hub severity, not GD severity.
- **Disabling a Security Hub control mid-stream** still leaves historical findings. Suppress them via Workflow Status: SUPPRESSED.
- **PCI DSS standard requires manual evidence** for ~30% of controls. Audit Manager helps; GA in 2023 — pair with Security Hub.

---

## 5. Pytest worked example

```python
# tests/test_security_org.py
import boto3, pytest

orgs = boto3.client("organizations")
sh = boto3.client("securityhub")
gd = boto3.client("guardduty")
inspector = boto3.client("inspector2")
macie = boto3.client("macie2")


def test_security_services_delegated():
    expected = ["securityhub.amazonaws.com", "guardduty.amazonaws.com",
                "inspector2.amazonaws.com", "macie.amazonaws.com",
                "detective.amazonaws.com", "access-analyzer.amazonaws.com"]
    delegated = orgs.list_delegated_administrators()["DelegatedAdministrators"]
    services_seen = set()
    for d in delegated:
        services = orgs.list_delegated_services_for_account(AccountId=d["Id"])["DelegatedServices"]
        services_seen.update(s["ServicePrincipal"] for s in services)
    for svc in expected:
        assert svc in services_seen, f"Missing delegated admin: {svc}"


def test_security_hub_standards_enabled():
    """At minimum AWS FSBP + CIS + PCI + NIST 800-53."""
    enabled = sh.get_enabled_standards()["StandardsSubscriptions"]
    arns = [s["StandardsArn"] for s in enabled]
    expected = [
        "aws-foundational-security-best-practices",
        "cis-aws-foundations-benchmark",
        "pci-dss",
        "nist-800-53",
    ]
    for sub in expected:
        assert any(sub in a for a in arns), f"Missing standard: {sub}"


def test_guardduty_org_auto_enable():
    detectors = gd.list_detectors()["DetectorIds"]
    assert detectors
    org_cfg = gd.describe_organization_configuration(DetectorId=detectors[0])
    assert org_cfg["AutoEnableOrganizationMembers"] == "ALL"


def test_finding_aggregator_all_regions():
    aggs = sh.list_finding_aggregators()["FindingAggregators"]
    assert aggs
    detail = sh.get_finding_aggregator(FindingAggregatorArn=aggs[0]["FindingAggregatorArn"])
    assert detail["RegionLinkingMode"] == "ALL_REGIONS"


def test_critical_findings_route_to_sns():
    """EventBridge rule security-hub-critical-findings exists with SNS target."""
    events_client = boto3.client("events")
    rules = events_client.list_rules(NamePrefix="security-hub-critical")["Rules"]
    assert rules
    targets = events_client.list_targets_by_rule(Rule=rules[0]["Name"])["Targets"]
    assert any("sns" in t["Arn"].lower() for t in targets)
```

---

## 6. Five non-negotiables

1. **All 6 security services delegated to Audit account** via `orgs.CfnDelegatedAdministrator`.
2. **Security Hub Central Configuration** with config policy associated to root OU (auto-enables every new account).
3. **At minimum 4 standards subscribed**: AWS FSBP + CIS Benchmark v3 + PCI-DSS v4 + NIST 800-53 r5.
4. **GuardDuty all 6 features auto-enabled org-wide** (S3 data events + EKS Audit + EKS Runtime + EBS Malware + RDS + Lambda).
5. **Critical/High findings → SNS → on-call** (PagerDuty/Slack) within 15 min via EventBridge rule with input transformer.

---

## 7. References

- [Security Hub Central Configuration (Sept 2024)](https://docs.aws.amazon.com/securityhub/latest/userguide/central-configuration-intro.html)
- [GuardDuty features](https://docs.aws.amazon.com/guardduty/latest/ug/guardduty-features-activation-model.html)
- [Inspector v2 — User Guide](https://docs.aws.amazon.com/inspector/latest/user/what-is-inspector.html)
- [Macie — sensitive data discovery](https://docs.aws.amazon.com/macie/latest/user/macie-classify-objects.html)
- [Detective — investigations](https://docs.aws.amazon.com/detective/latest/userguide/what-is-detective.html)
- [IAM Access Analyzer — unused access](https://docs.aws.amazon.com/IAM/latest/UserGuide/what-is-access-analyzer.html)
- [Security Hub standards reference](https://docs.aws.amazon.com/securityhub/latest/userguide/securityhub-standards.html)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Security Hub Central Config + GuardDuty 6 features + Inspector v2 + Macie + Detective + Access Analyzer + 4 standards + finding routing. Wave 11. |
