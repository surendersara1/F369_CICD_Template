# PARTIAL: Frontend Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_frontend()` method body.

---

## When to Include This Layer

Include frontend constructs when SOW contains ANY of:

- "web application", "React", "Next.js", "Angular", "Vue", "SPA"
- "website", "web UI", "web portal", "dashboard UI"
- "static assets", "HTML/CSS/JS hosting"

---

## CDK Code Block — Frontend Layer

```python
def _create_frontend(self, stage_name: str) -> None:
    """
    Layer 5: Frontend Infrastructure

    Architecture:
      S3 (private bucket) → CloudFront (OAC) → Users
      WAF attached to CloudFront distribution
      ACM certificate for custom domain (via Route53)

    Security:
      - S3 bucket is PRIVATE (no public access)
      - CloudFront uses Origin Access Control (OAC) to access S3 (replaces deprecated OAI)
      - WAF blocks common OWASP Top 10 attacks
      - HTTPS-only with TLS 1.2 minimum
      - Security headers via CloudFront Function
    """
    import aws_cdk.aws_cloudfront as cf
    import aws_cdk.aws_cloudfront_origins as cf_origins
    import aws_cdk.aws_wafv2 as wafv2
    import aws_cdk.aws_certificatemanager as acm
    import aws_cdk.aws_route53 as route53
    import aws_cdk.aws_route53_targets as route53_targets

    # =========================================================================
    # S3 BUCKET — Private, encrypted, versioned
    # =========================================================================
    self.frontend_bucket = s3.Bucket(
        self, "FrontendBucket",
        bucket_name=f"{{project_name}}-frontend-{stage_name}-{self.account}",

        # Security: block ALL public access
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,

        # Encryption at rest (S3-managed for CloudFront OAC compatibility)
        encryption=s3.BucketEncryption.S3_MANAGED,

        # Versioning for rollback capability
        versioned=True,

        # CORS for same-origin API calls
        cors=[
            s3.CorsRule(
                allowed_methods=[s3.HttpMethods.GET],
                allowed_origins=["*"] if stage_name == "dev" else [
                    f"https://{{project_name}}.example.com",
                    f"https://www.{{project_name}}.example.com",
                ],
                allowed_headers=["*"],
            )
        ],

        # Lifecycle rules for cost optimization
        lifecycle_rules=[
            s3.LifecycleRule(
                id="DeleteOldVersions",
                noncurrent_version_expiration=Duration.days(30 if stage_name == "prod" else 7),
                enabled=True,
            )
        ],

        # Environmental removal policies
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        auto_delete_objects=stage_name != "prod",

        # Access logging
        server_access_logs_prefix="frontend-access-logs/",
    )

    # =========================================================================
    # WAF — Web Application Firewall
    # =========================================================================
    # WAF v2 (scope: CLOUDFRONT must be in us-east-1)
    waf_rules = [
        # AWS Managed Rules: Core Rule Set (OWASP Top 10)
        wafv2.CfnWebACL.RuleProperty(
            name="AWSManagedRulesCommonRuleSet",
            priority=1,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name="AWSManagedRulesCommonRuleSet",
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="AWSManagedRulesCommonRuleSet",
                sampled_requests_enabled=True,
            ),
        ),
        # AWS Managed Rules: Known Bad Inputs
        wafv2.CfnWebACL.RuleProperty(
            name="AWSManagedRulesKnownBadInputsRuleSet",
            priority=2,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name="AWSManagedRulesKnownBadInputsRuleSet",
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="AWSManagedRulesKnownBadInputsRuleSet",
                sampled_requests_enabled=True,
            ),
        ),
        # Rate limiting rule (1000 req/5min per IP)
        wafv2.CfnWebACL.RuleProperty(
            name="RateLimitRule",
            priority=3,
            action=wafv2.CfnWebACL.RuleActionProperty(
                block={} if stage_name == "prod" else {}
            ),
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    limit=1000,
                    aggregate_key_type="IP",
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="RateLimitRule",
                sampled_requests_enabled=True,
            ),
        ),
    ]

    self.waf_acl = wafv2.CfnWebACL(
        self, "FrontendWAF",
        name=f"{{project_name}}-frontend-waf-{stage_name}",
        scope="CLOUDFRONT",  # Must be us-east-1 for CloudFront
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        rules=waf_rules,
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name="{{project_name}}FrontendWAF",
            sampled_requests_enabled=True,
        ),
    )

    # =========================================================================
    # CLOUDFRONT — Security Headers Function
    # =========================================================================
    security_headers_fn = cf.Function(
        self, "SecurityHeadersFn",
        code=cf.FunctionCode.from_inline("""
function handler(event) {
    var response = event.response;
    var headers = response.headers;

    // Security headers
    headers['strict-transport-security'] = { value: 'max-age=63072000; includeSubdomains; preload' };
    headers['x-content-type-options']    = { value: 'nosniff' };
    headers['x-frame-options']           = { value: 'DENY' };
    headers['x-xss-protection']          = { value: '1; mode=block' };
    headers['referrer-policy']           = { value: 'strict-origin-when-cross-origin' };
    headers['permissions-policy']        = { value: 'camera=(), microphone=(), geolocation=()' };
    headers['content-security-policy']   = {
        value: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https://execute-api.*.amazonaws.com"
    };

    return response;
}
"""),
        function_name=f"{{project_name}}-security-headers-{stage_name}",
    )

    # =========================================================================
    # CLOUDFRONT DISTRIBUTION
    # =========================================================================
    # Price class varies by environment (lower cost for dev/staging)
    price_class = {
        "dev":     cf.PriceClass.PRICE_CLASS_100,    # North America + Europe only
        "staging": cf.PriceClass.PRICE_CLASS_100,
        "prod":    cf.PriceClass.PRICE_CLASS_ALL,    # All CloudFront edge locations
    }.get(stage_name, cf.PriceClass.PRICE_CLASS_100)

    self.distribution = cf.Distribution(
        self, "Distribution",

        # OAC: CloudFront accesses private S3 bucket via Origin Access Control
        # (OAC replaces deprecated OAI — better security, supports SSE-KMS)
        default_behavior=cf.BehaviorOptions(
            origin=cf_origins.S3BucketOrigin.with_origin_access_control(
                self.frontend_bucket,
            ),
            viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
            compress=True,
            # Apply security headers function on every response
            function_associations=[
                cf.FunctionAssociation(
                    function=security_headers_fn,
                    event_type=cf.FunctionEventType.VIEWER_RESPONSE,
                )
            ],
        ),

        # API passthrough behavior (no caching)
        additional_behaviors={
            "/api/*": cf.BehaviorOptions(
                origin=cf_origins.HttpOrigin(
                    # Pass API calls to API Gateway
                    # Replace with self.api.url after API layer is created
                    f"{self.rest_api.rest_api_id}.execute-api.{self.region}.amazonaws.com",
                    origin_path=f"/{stage_name}",
                ),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
                cache_policy=cf.CachePolicy.CACHING_DISABLED,
                allowed_methods=cf.AllowedMethods.ALLOW_ALL,
            ),
        },

        # SPA routing: all 404s → index.html (React Router support)
        error_responses=[
            cf.ErrorResponse(
                http_status=404,
                response_http_status=200,
                response_page_path="/index.html",
            ),
            cf.ErrorResponse(
                http_status=403,
                response_http_status=200,
                response_page_path="/index.html",
            ),
        ],

        # WAF association
        web_acl_id=self.waf_acl.attr_arn,

        # SSL/TLS configuration
        minimum_protocol_version=cf.SecurityPolicyProtocol.TLS_V1_2_2021,
        ssl_support_method=cf.SSLMethod.SNI,

        price_class=price_class,

        # Access logging
        enable_logging=True,
        log_file_prefix="cloudfront-logs/",

        # HTTP version
        http_version=cf.HttpVersion.HTTP2_AND_3,
    )

    # Grant CloudFront OAC access to S3 (auto-configured by S3BucketOrigin.with_origin_access_control)

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    self.frontend_url_output = CfnOutput(
        self, "FrontendURL",
        value=f"https://{self.distribution.distribution_domain_name}",
        description="Frontend CloudFront URL",
        export_name=f"{{project_name}}-frontend-url-{stage_name}",
    )

    CfnOutput(
        self, "FrontendBucketName",
        value=self.frontend_bucket.bucket_name,
        description="S3 bucket for frontend assets",
        export_name=f"{{project_name}}-frontend-bucket-{stage_name}",
    )

    CfnOutput(
        self, "CloudFrontDistributionId",
        value=self.distribution.distribution_id,
        description="CloudFront Distribution ID (for cache invalidation)",
        export_name=f"{{project_name}}-cloudfront-id-{stage_name}",
    )
```

---

## Deployment Note

After `cdk deploy`, run the frontend deployment separately:

```bash
# Build React app
cd frontend && npm run build

# Deploy to S3
aws s3 sync ./frontend/build s3://$BUCKET_NAME --delete

# Invalidate CloudFront cache
aws cloudfront create-invalidation \
    --distribution-id $DISTRIBUTION_ID \
    --paths "/*"
```
