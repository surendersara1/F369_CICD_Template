# SOP — Advanced Security (WAF, Shield, Macie, GuardDuty, Security Hub)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+)

---

## 1. Purpose

Enterprise security controls beyond the `LAYER_SECURITY` baseline:

- **AWS WAF v2** — OWASP Core Rule Set, rate-based rules, bot control
- **AWS Shield Advanced** — DDoS protection (costs $3000/month base, only enable for high-value apps)
- **Amazon Macie** — automated S3 PII/PHI scanning
- **Amazon GuardDuty** — threat detection (DNS, VPC flow, CloudTrail anomalies)
- **AWS Security Hub** — aggregated findings dashboard (CIS + Foundational benchmarks)
- **AWS Network Firewall** — east-west traffic inspection (Phase 3)

Include when SOW mentions: WAF, DDoS, PII scanning, compliance posture, HIPAA, PCI, SOC 2.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack | **§3 Monolith Variant** |
| Dedicated `GovernanceStack` / `SecurityDetectionStack` across account | **§4 Micro-Stack Variant** |

These services largely observe; no cycle risk. WAF is attached to CloudFront or API Gateway via Web ACL association — safe cross-stack if the WebACL is referenced by ARN.

---

## 3. Monolith Variant

### 3.1 WAF WebACL on CloudFront

**CRITICAL:** CloudFront-scoped WebACLs must be created in `us-east-1`. If your app deploys to another region, create a dedicated us-east-1 stack for the WebACL and share its ARN via `cdk.Fn.import_value` or stack cross-region references.

```python
import aws_cdk as cdk
from aws_cdk import aws_wafv2 as wafv2


# In a us-east-1 stack:
waf_cloudfront = wafv2.CfnWebACL(
    self, "CloudfrontWaf",
    name=f"{{project_name}}-cf-waf-{stage}",
    scope="CLOUDFRONT",   # must be CLOUDFRONT, not REGIONAL
    default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        metric_name=f"{{project_name}}-cf-waf",
        sampled_requests_enabled=True,
    ),
    rules=[
        wafv2.CfnWebACL.RuleProperty(
            name="AWSManagedRulesCommonRuleSet",
            priority=0,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name="AWSManagedRulesCommonRuleSet",
                ),
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="CommonRuleSet", sampled_requests_enabled=True,
            ),
        ),
        wafv2.CfnWebACL.RuleProperty(
            name="RateLimit",
            priority=1,
            action=wafv2.CfnWebACL.RuleActionProperty(block={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    limit=1000,
                    aggregate_key_type="IP",
                ),
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="RateLimit", sampled_requests_enabled=True,
            ),
        ),
    ],
)

# Attach: cf.Distribution(web_acl_id=waf_cloudfront.attr_arn, ...)
```

### 3.2 WAF WebACL on API Gateway

```python
# In the same region as the API
waf_api = wafv2.CfnWebACL(
    self, "ApiWaf",
    name=f"{{project_name}}-api-waf-{stage}",
    scope="REGIONAL",
    default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        metric_name=f"{{project_name}}-api-waf",
        sampled_requests_enabled=True,
    ),
    rules=[
        # OWASP Core Rule Set
        wafv2.CfnWebACL.RuleProperty(
            name="CommonRuleSet", priority=0,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name="AWSManagedRulesCommonRuleSet",
                ),
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="CommonRuleSet", sampled_requests_enabled=True,
            ),
        ),
    ],
)
wafv2.CfnWebACLAssociation(
    self, "ApiWafAssociation",
    resource_arn=self.api.deployment_stage.stage_arn,
    web_acl_arn=waf_api.attr_arn,
)
```

### 3.3 Macie, GuardDuty, Security Hub

```python
from aws_cdk import aws_macie as macie, aws_guardduty as guardduty, aws_securityhub as sechub


macie.CfnSession(self, "MacieSession", status="ENABLED",
                  finding_publishing_frequency="FIFTEEN_MINUTES")

guardduty.CfnDetector(self, "GuardDutyDetector", enable=True,
                       finding_publishing_frequency="FIFTEEN_MINUTES",
                       data_sources=guardduty.CfnDetector.CFNDataSourceConfigurationsProperty(
                           s3_logs=guardduty.CfnDetector.CFNS3LogsConfigurationProperty(enable=True),
                       ))

sechub.CfnHub(self, "SecurityHub",
               enable_default_standards=True,
               auto_enable_controls=True)
```

### 3.4 Monolith gotchas

- **WAF scope** — CLOUDFRONT vs REGIONAL; can't be changed after creation.
- **WAF + CloudFront** must be in us-east-1, always.
- **Macie / GuardDuty / Security Hub** are account-global services; deploying "a second copy" fails. Deploy in exactly one stack (often an account-bootstrap stack, not per-app).
- **Shield Advanced** subscription costs $3000/month and commits for 1 year. Enable only after business approval.

---

## 4. Micro-Stack Variant

### 4.1 Split by concern

- `SecurityDetectionStack` — GuardDuty, Security Hub, Macie (deploys once per account)
- `WafStack` (us-east-1) — CloudFront-scoped WebACL
- `WafRegionalStack` — Regional WebACLs for API Gateway
- `NetworkFirewallStack` (Phase 3) — east-west inspection

### 4.2 `WafStack` (us-east-1)

```python
import aws_cdk as cdk
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct


class WafStack(cdk.Stack):
    """Deploy in us-east-1 regardless of the app's primary region."""

    def __init__(self, scope: Construct, **kwargs) -> None:
        super().__init__(scope, "{project_name}-waf-cf",
                          env=cdk.Environment(region="us-east-1", **kwargs.pop("env", {}).__dict__),
                          **kwargs)

        self.web_acl = wafv2.CfnWebACL(
            self, "Waf",
            name="{project_name}-cf-waf",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="{project_name}-cf-waf", sampled_requests_enabled=True,
            ),
            rules=[...],  # same as §3.1
        )
        cdk.CfnOutput(self, "WebAclArn", value=self.web_acl.attr_arn,
                       export_name="{project_name}-cf-waf-arn")
```

### 4.3 `CdnStack` consumes the WebACL ARN

```python
# In app.py, use cross-region reference
app = cdk.App()
waf = WafStack(app, env=cdk.Environment(region="us-east-1"))

cdn = CdnStack(app,
    web_acl_arn=waf.web_acl.attr_arn,   # CDK handles cross-region via exports
    env=cdk.Environment(region="us-east-1"),  # CF is global, bucket can be here too
    cross_region_references=True,
)
```

### 4.4 Micro-stack gotchas

- **`cross_region_references=True`** on the App is required for CDK to wire exports across regions.
- **WAF association** across stacks uses `CfnWebACLAssociation` with `web_acl_arn=<imported>`. Safe (read-only reference).
- **Security Hub, GuardDuty, Macie** are account-singletons — deploy from a *bootstrap* stack, not per-app.

---

## 5. Worked example

```python
def test_regional_waf_has_common_rule_set():
    # ... instantiate a stack with waf_api ...
    t = Template.from_stack(sec)
    t.has_resource_properties("AWS::WAFv2::WebACL", {
        "Rules": Match.array_with([
            Match.object_like({"Name": "CommonRuleSet"}),
        ]),
    })
```

---

## 6. References

- `docs/Feature_Roadmap.md` — SECX-04..SECX-16
- Related SOPs: `LAYER_SECURITY` (baseline), `LAYER_FRONTEND` (CloudFront attach), `LAYER_API` (API Gateway attach)

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. us-east-1 WAF stack for CloudFront. Cross-region references pattern. |
| 1.0 | 2026-03-05 | Initial. |
