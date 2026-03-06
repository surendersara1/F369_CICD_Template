# PARTIAL: Advanced Security — WAF v2, Shield Advanced, Macie, Network Firewall, Security Hub

**Usage:** Include when SOW mentions enterprise security, WAF, DDoS protection, PII scanning, security posture, financial services, healthcare, or government workloads.

---

## Defense-in-Depth Stack

```
Internet Traffic
      │
      ▼
┌─────────────────────────────────────┐
│  AWS Shield Advanced (L3/L4 DDoS)  │  Always-on volumetric attack protection
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  AWS WAF v2 (L7 HTTP filtering)    │  Rule groups: OWASP, Bot Control, ATO
│    Managed Rules:                  │
│    • AWSManagedRulesCommonRuleSet   │  OWASP Top 10 (SQLi, XSS, RFI etc.)
│    • AWSManagedRulesBotControlRuleSet│ Bot detection (scrapers, scanners)
│    • AWSManagedRulesATPRuleSet     │  Account Takeover Prevention
│    • AWSManagedRulesACFPRuleSet    │  Account Creation Fraud Prevention
│    • Geo/IP rate limiting rules    │  Block countries, rate-limit IPs
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  AWS Network Firewall (VPC L4/L7)  │  Stateful inspection, IDS/IPS signatures
│    • Suricata-compatible rules      │  Emerging threat rules
│    • Domain allow/deny lists        │  Block malicious domains
│    • TLS inspection                │  Decrypt + inspect encrypted traffic
└─────────────────┬───────────────────┘
                  │
                  ▼
         Your Application (CloudFront → API GW → Lambda/ECS)

Data Protection:
  Amazon Macie   → Scan S3 buckets for PII/PHI/secrets
  Security Hub   → Aggregate findings from all services
  GuardDuty      → Threat intelligence (anomaly detection)
  Inspector v2   → Container + Lambda vulnerability scanning
```

---

## CDK Code Block — Advanced Security Stack

```python
def _create_advanced_security(self, stage_name: str) -> None:
    """
    Advanced Security Layer — Enterprise/Healthcare/Financial grade.

    Components:
      A) AWS WAF v2 with all managed rule groups (CloudFront + API GW)
      B) AWS Shield Advanced (DDoS protection + 24/7 DRT access)
      C) AWS Network Firewall (VPC-level stateful inspection)
      D) Amazon Macie (S3 PII/PHI scanning + classification)
      E) AWS Security Hub (centralized findings aggregation)
      F) Amazon Inspector v2 (ECR + Lambda vulnerability scanning)
      G) WAF Log Analysis Lambda (parse, alert, auto-block)
    """

    import aws_cdk.aws_wafv2 as wafv2
    import aws_cdk.aws_networkfirewall as networkfirewall
    import aws_cdk.aws_macie as macie
    import aws_cdk.aws_securityhub as securityhub
    import aws_cdk.aws_inspector as inspector

    # =========================================================================
    # A) AWS WAF v2 — Web Application Firewall
    # =========================================================================

    # WAF Log bucket (required by AWS WAF for logging)
    waf_log_bucket = s3.Bucket(
        self, "WAFLogBucket",
        # IMPORTANT: WAF log buckets MUST be named "aws-waf-logs-*"
        bucket_name=f"aws-waf-logs-{{project_name}}-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,  # WAF doesn't support KMS CMK for log delivery
        lifecycle_rules=[s3.LifecycleRule(
            id="waf-log-retention",
            expiration=Duration.days(90),
            transitions=[s3.Transition(
                storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                transition_after=Duration.days(30),
            )],
            enabled=True,
        )],
        removal_policy=RemovalPolicy.RETAIN,
    )

    # WAF WebACL — CloudFront scope (global, us-east-1)
    # [Claude: create a REGIONAL WebACL for API Gateway, separate from CloudFront global]
    self.waf_acl = wafv2.CfnWebACL(
        self, "WAFWebACL",
        name=f"{{project_name}}-waf-{stage_name}",
        scope="CLOUDFRONT",         # CLOUDFRONT or REGIONAL
        description=f"{{project_name}} WAF Web ACL — Enterprise security rules ({stage_name})",

        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),

        rules=[
            # === Rule 1: AWS Managed — Core Rule Set (OWASP Top 10) ===
            # Blocks: SQLi, XSS, LFI/RFI, SSRF, command injection
            wafv2.CfnWebACL.RuleProperty(
                name="AWSManagedRulesCommonRuleSet",
                priority=10,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS",
                        name="AWSManagedRulesCommonRuleSet",
                        # Exclude rules that may cause false positives in your app
                        excluded_rules=[
                            wafv2.CfnWebACL.ExcludedRuleProperty(name="SizeRestrictions_BODY"),
                            # Add more exclusions based on app testing
                        ],
                        managed_rule_group_configs=[
                            wafv2.CfnWebACL.ManagedRuleGroupConfigProperty(
                                payload_type="JSON",  # If API returns JSON
                                login_path="/auth/login",
                            )
                        ] if stage_name == "prod" else [],
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name="AWSManagedRulesCommonRuleSet",
                    sampled_requests_enabled=True,
                ),
            ),

            # === Rule 2: Known Bad Inputs (Log4j, SSRF, Spring4Shell) ===
            wafv2.CfnWebACL.RuleProperty(
                name="AWSManagedRulesKnownBadInputsRuleSet",
                priority=20,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS", name="AWSManagedRulesKnownBadInputsRuleSet",
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="BadInputs", sampled_requests_enabled=True,
                ),
            ),

            # === Rule 3: Bot Control ===
            # [Include when SOW mentions "bot protection", "scraping prevention", "credential stuffing"]
            wafv2.CfnWebACL.RuleProperty(
                name="AWSManagedRulesBotControlRuleSet",
                priority=30,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(
                    none={} if stage_name != "prod" else {}  # Count in staging, block in prod
                ),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS",
                        name="AWSManagedRulesBotControlRuleSet",
                        managed_rule_group_configs=[
                            wafv2.CfnWebACL.ManagedRuleGroupConfigProperty(
                                aws_managed_rules_bot_control_rule_set=wafv2.CfnWebACL.AWSManagedRulesBotControlRuleSetProperty(
                                    inspection_level="TARGETED",  # COMMON (cheaper) or TARGETED (detects sophisticated bots)
                                    enable_machine_learning=True if stage_name == "prod" else False,
                                )
                            )
                        ],
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="BotControl", sampled_requests_enabled=True,
                ),
            ),

            # === Rule 4: Account Takeover Prevention (ATP) ===
            # [Include when SOW has user authentication, login endpoints]
            wafv2.CfnWebACL.RuleProperty(
                name="AWSManagedRulesATPRuleSet",
                priority=40,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS",
                        name="AWSManagedRulesATPRuleSet",
                        managed_rule_group_configs=[
                            wafv2.CfnWebACL.ManagedRuleGroupConfigProperty(
                                aws_managed_rules_atp_rule_set=wafv2.CfnWebACL.AWSManagedRulesATPRuleSetProperty(
                                    login_path="/auth/login",
                                    request_inspection=wafv2.CfnWebACL.RequestInspectionProperty(
                                        payload_type="JSON",
                                        username_field=wafv2.CfnWebACL.UsernameFieldProperty(identifier="/username"),
                                        password_field=wafv2.CfnWebACL.PasswordFieldProperty(identifier="/password"),
                                    ),
                                    response_inspection=wafv2.CfnWebACL.ResponseInspectionProperty(
                                        status_code=wafv2.CfnWebACL.ResponseInspectionStatusCodeProperty(
                                            success_codes=[200],
                                            failure_codes=[401, 403],
                                        )
                                    ),
                                )
                            )
                        ],
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="ATPRuleSet", sampled_requests_enabled=True,
                ),
            ),

            # === Rule 5: IP Rate Limiting (per IP, 2000 req/5min) ===
            wafv2.CfnWebACL.RuleProperty(
                name="IPRateLimit",
                priority=50,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=2000,   # Requests per 5-minute window per IP
                        aggregate_key_type="IP",
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="IPRateLimit", sampled_requests_enabled=True,
                ),
            ),

            # === Rule 6: Geo Block — restrict to approved countries ===
            # [Claude: include if SOW mentions geo-restriction, compliance, export controls]
            wafv2.CfnWebACL.RuleProperty(
                name="GeoRestriction",
                priority=5,  # Check first (cheapest rule)
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    not_statement=wafv2.CfnWebACL.NotStatementProperty(
                        statement=wafv2.CfnWebACL.StatementProperty(
                            geo_match_statement=wafv2.CfnWebACL.GeoMatchStatementProperty(
                                # [Claude: replace with allowed countries from Architecture Map]
                                country_codes=["US", "CA", "GB", "AU", "DE", "FR", "JP"],
                            )
                        )
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="GeoBlock", sampled_requests_enabled=True,
                ),
            ),
        ],

        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=f"{{project_name}}-waf-{stage_name}",
            sampled_requests_enabled=True,
        ),

        custom_response_bodies={
            "BlockedResponse": wafv2.CfnWebACL.CustomResponseBodyProperty(
                content='{"error": "Request blocked by security policy", "code": "WAF_BLOCKED"}',
                content_type="APPLICATION_JSON",
            )
        },
    )

    # WAF Logging Configuration
    wafv2.CfnLoggingConfiguration(
        self, "WAFLogging",
        resource_arn=self.waf_acl.attr_arn,
        log_destination_configs=[waf_log_bucket.bucket_arn],
        redacted_fields=[
            # Redact sensitive headers from WAF logs
            wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                single_header={"Name": "authorization"}
            ),
            wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                single_header={"Name": "cookie"}
            ),
        ],
        logging_filter=wafv2.CfnLoggingConfiguration.LoggingFilterProperty(
            default_behavior="DROP",  # Only log blocked + counted requests (not ALL traffic — saves cost)
            filters=[
                wafv2.CfnLoggingConfiguration.FilterProperty(
                    behavior="KEEP",
                    requirement="MEETS_ANY",
                    conditions=[
                        wafv2.CfnLoggingConfiguration.ConditionProperty(
                            action_condition=wafv2.CfnLoggingConfiguration.ActionConditionProperty(action="BLOCK")
                        ),
                        wafv2.CfnLoggingConfiguration.ConditionProperty(
                            action_condition=wafv2.CfnLoggingConfiguration.ActionConditionProperty(action="COUNT")
                        ),
                    ],
                )
            ],
        ),
    )

    # =========================================================================
    # B) AWS SHIELD ADVANCED
    # [Include when SOW mentions DDoS protection, financial services, critical infra]
    # Note: Shield Advanced costs $3,000/month — confirm with SOW budget
    # =========================================================================

    # Shield Advanced is enabled account-wide via AWS console/CLI
    # CDK registers specific resources for protection:
    # [Claude: protect the CloudFront distribution, Route53 hosted zones, ELBs, Elastic IPs]

    shield.CfnProtection(
        self, "CloudFrontShieldProtection",
        name=f"{{project_name}}-cloudfront-{stage_name}",
        resource_arn=self.cloudfront_distribution.distribution_arn if hasattr(self, 'cloudfront_distribution') else "PLACEHOLDER",
        health_check_arns=[],  # Add Route53 health checks for proactive engagement
    )

    # =========================================================================
    # C) AWS NETWORK FIREWALL (VPC-level deep packet inspection)
    # [Include when SOW mentions "network firewall", "IDS/IPS", "egress filtering"]
    # =========================================================================

    # Firewall policy — stateful rules
    fw_policy = networkfirewall.CfnFirewallPolicy(
        self, "NetworkFirewallPolicy",
        firewall_policy_name=f"{{project_name}}-fw-policy-{stage_name}",
        firewall_policy=networkfirewall.CfnFirewallPolicy.FirewallPolicyProperty(
            stateless_default_actions=["aws:forward_to_sfe"],  # Forward to stateful engine
            stateless_fragment_default_actions=["aws:forward_to_sfe"],
            stateful_engine_options=networkfirewall.CfnFirewallPolicy.StatefulEngineOptionsProperty(
                rule_order="STRICT_ORDER",
                stream_exception_policy="DROP",  # Drop packets on inspection engine failure (safe default)
            ),
            stateful_rule_group_references=[
                # AWS Managed Threat Intelligence rules
                networkfirewall.CfnFirewallPolicy.StatefulRuleGroupReferenceProperty(
                    resource_arn=f"arn:aws:network-firewall:{self.region}:aws-managed:stateful-rulegroup/ThreatSignaturesMalwareWeb",
                    priority=100,
                ),
                networkfirewall.CfnFirewallPolicy.StatefulRuleGroupReferenceProperty(
                    resource_arn=f"arn:aws:network-firewall:{self.region}:aws-managed:stateful-rulegroup/ThreatSignaturesDoS",
                    priority=200,
                ),
                networkfirewall.CfnFirewallPolicy.StatefulRuleGroupReferenceProperty(
                    resource_arn=f"arn:aws:network-firewall:{self.region}:aws-managed:stateful-rulegroup/AbusedLegitMalwareC2",
                    priority=300,
                ),
                # Custom domain allow-list rule group
                networkfirewall.CfnFirewallPolicy.StatefulRuleGroupReferenceProperty(
                    resource_arn=self.domain_allowlist_rule_group.attr_rule_group_arn if hasattr(self, 'domain_allowlist_rule_group') else "PLACEHOLDER",
                    priority=400,
                ),
            ],
        ),
    )

    # Domain allowlist — egress control (block unexpected outbound connections)
    domain_allowlist = networkfirewall.CfnRuleGroup(
        self, "DomainAllowlistRuleGroup",
        rule_group_name=f"{{project_name}}-domain-allowlist-{stage_name}",
        type="STATEFUL",
        capacity=100,
        rule_group=networkfirewall.CfnRuleGroup.RuleGroupProperty(
            rules_source=networkfirewall.CfnRuleGroup.RulesSourceProperty(
                rules_source_list=networkfirewall.CfnRuleGroup.RulesSourceListProperty(
                    generated_rules_type="ALLOWLIST",
                    target_types=["HTTP_HOST", "TLS_SNI"],
                    # [Claude: add allowed external domains from Architecture Map]
                    targets=[
                        ".amazonaws.com",
                        ".cloudfront.net",
                        ".cognito.amazonaws.com",
                        "api.stripe.com",      # Payment processor example
                        "api.sendgrid.com",    # Email provider example
                        # Add more based on SOW external dependencies
                    ],
                )
            )
        ),
    )

    # =========================================================================
    # D) AMAZON MACIE — S3 PII/PHI scanning
    # [Include when SOW mentions HIPAA, GDPR, PII, PHI, data privacy, financial data]
    # =========================================================================

    # Enable Macie
    macie.CfnSession(
        self, "MacieSession",
        finding_publishing_frequency="SIX_HOURS",  # Check for new findings every 6 hours
        status="ENABLED",
    )

    # Custom data identifier — detect your domain-specific sensitive data
    macie.CfnCustomDataIdentifier(
        self, "PatientIDDataIdentifier",
        name=f"{{project_name}}-patient-id-{stage_name}",
        description="Detect {{project_name}} patient/member IDs in S3",
        # [Claude: update regex for your specific ID format]
        regex=r"{{project_name}}-PAT-\d{8}",
        keywords=["patient", "member", "patient_id", "member_id"],
        maximum_match_distance=50,
    )

    # Macie Classification Job — scan all buckets weekly
    macie.CfnFindingsFilter(
        self, "MacieCriticalFilter",
        name=f"{{project_name}}-critical-findings-{stage_name}",
        action="ARCHIVE",  # Auto-archive low-severity (reduce noise)
        description="Archive informational findings, surface only high severity",
        finding_criteria=macie.CfnFindingsFilter.FindingCriteriaProperty(
            criterion={
                "severity.description": macie.CfnFindingsFilter.CriterionAdditionalPropertiesProperty(
                    not_eq=["Low", "Informational"],
                )
            }
        ),
    )

    # =========================================================================
    # E) AWS SECURITY HUB — Centralized findings aggregation
    # =========================================================================

    securityhub.CfnHub(
        self, "SecurityHub",
        enable_default_standards=True,   # AWS Foundational Security Best Practices v1
        auto_enable_controls=True,
        control_finding_generator="SECURITY_CONTROL",
    )

    # =========================================================================
    # F) WAF ALARM — Alert on spike in blocked requests
    # =========================================================================

    cw.Alarm(
        self, "WAFBlockedRequestsAlarm",
        alarm_name=f"{{project_name}}-waf-blocked-spike-{stage_name}",
        alarm_description="WAF blocked request rate spike — possible attack in progress",
        metric=cw.Metric(
            namespace="AWS/WAFV2",
            metric_name="BlockedRequests",
            dimensions_map={
                "WebACL": f"{{project_name}}-waf-{stage_name}",
                "Region": self.region,
                "Rule": "ALL",
            },
            period=Duration.minutes(5),
            statistic="Sum",
        ),
        threshold=500,       # Alert if > 500 blocks in 5 min — [Claude: adjust based on expected traffic]
        evaluation_periods=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "WAFWebACLArn",
        value=self.waf_acl.attr_arn,
        description="WAF WebACL ARN — associate with CloudFront, API GW",
        export_name=f"{{project_name}}-waf-arn-{stage_name}",
    )
    CfnOutput(self, "WAFLogBucketName",
        value=waf_log_bucket.bucket_name,
        description="WAF access log bucket",
        export_name=f"{{project_name}}-waf-logs-{stage_name}",
    )
```
