# SOP — CloudFront Foundation (distribution · OAC · cache behaviors · custom error pages · WAF · Shield · multi-origin)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · CloudFront distribution · Origin Access Control (OAC, replaces OAI) · Cache behaviors (path-pattern based) · Cache policies + Origin Request policies + Response Headers policies · Custom error pages · WAF v2 (CLOUDFRONT scope) · Shield Standard + Shield Advanced · Real-time logs · Origin groups (multi-origin failover) · ACM (us-east-1)

---

## 1. Purpose

- Codify **CloudFront** as the canonical AWS-native CDN. Replaces 3rd-party CDNs for AWS-first orgs; eliminates per-region origin setup; integrated with WAF + Shield.
- Codify **OAC (Origin Access Control)** — the modern (2022+) replacement for OAI. SigV4-signed requests to S3 with KMS support; OAI did not support KMS or non-S3 origins.
- Codify **cache behaviors** — multiple path-patterns within one distribution; each with its own policies.
- Codify the **3 policy types**: Cache Policy (TTLs + cache key) + Origin Request Policy (what's forwarded to origin) + Response Headers Policy (what's added to response).
- Codify **custom error pages** for SPA routing + branded 4xx/5xx.
- Codify **WAF v2 in CLOUDFRONT scope** (must be us-east-1) for L7 protection.
- Codify **Shield Standard** (free) + when to upgrade to Shield Advanced.
- Codify **Origin Groups** for multi-origin failover.
- Codify the **us-east-1 ACM cert requirement** for CloudFront.
- Pairs with `CDN_EDGE_COMPUTE` (Functions / Lambda@Edge) and `LAYER_FRONTEND` (S3 + React).

When the SOW signals: "CDN", "global edge", "CloudFront setup", "S3 + CloudFront", "branded error pages", "WAF on CDN", "Shield protection".

---

## 2. Decision tree — distribution shape

```
Origin type?
├── S3 static site (SPA, marketing site) → §3 S3 + OAC
├── Single ALB / API Gateway / ECS → §4 Custom origin
├── Multi-origin (failover or A/B) → §5 Origin Groups
├── Lambda Function URL → §4 Custom origin (no auth tier)
└── Hybrid (S3 + ALB by path) → §3 + §4 in same distribution

Cache strategy?
├── Mostly static (HTML 5min, assets 1y) → CachingOptimized + custom for HTML
├── Dynamic API + occasional static → CachingDisabled for /api/*
├── Authenticated dynamic → cache by Authorization header (with care)
└── Personalized → CachingDisabled OR Lambda@Edge for cache-key augmentation

WAF attachment?
├── Public-facing → MUST attach AWSManagedRulesCommonRuleSet
├── B2B / authenticated → API rate limit + bot control
└── Geo-restricted → block-country list

Edge compute?
├── Header rewrite, redirects, A/B → CloudFront Functions (cheap, fast)
├── Auth check, complex logic, KV lookups → Lambda@Edge OR Functions+KVStore
└── See CDN_EDGE_COMPUTE for full decision tree
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — S3 static site + CloudFront + OAC + WAF | **§3 Monolith** |
| Production — multi-origin + custom domain + Shield + edge compute | **§5 Production** |

---

## 3. S3 + OAC variant — static site with CloudFront

### 3.1 CDK

```python
# stacks/cdn_stack.py
from aws_cdk import Stack, Duration, RemovalPolicy
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as r53
from aws_cdk import aws_route53_targets as r53_targets
from aws_cdk import aws_wafv2 as waf
from constructs import Construct


class CdnStack(Stack):
    """MUST be deployed to us-east-1 for CloudFront ACM cert + WAF.
    For multi-region apps, deploy this stack to us-east-1 + main app to other regions."""

    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 hosted_zone: r53.IHostedZone,
                 domain_name: str,                            # app.example.com
                 kms_key: kms.IKey, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. S3 bucket for static site ─────────────────────────────
        site_bucket = s3.Bucket(self, "SiteBucket",
            bucket_name=f"{env_name}-site-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,    # CRITICAL: block public
            versioned=True,                                          # rollback-friendly
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── 2. ACM cert (MUST be us-east-1 for CloudFront) ──────────
        cert = acm.Certificate(self, "Cert",
            domain_name=domain_name,
            subject_alternative_names=[f"*.{domain_name}"],         # for sub-paths
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # ── 3. WAF v2 ACL (CLOUDFRONT scope; must be in us-east-1) ────
        waf_acl = waf.CfnWebACL(self, "Waf",
            name=f"{env_name}-cf-acl",
            scope="CLOUDFRONT",                                      # KEY
            default_action={"allow": {}},
            visibility_config={
                "sampledRequestsEnabled": True,
                "cloudWatchMetricsEnabled": True,
                "metricName": f"{env_name}-cf-acl",
            },
            rules=[
                # AWS Managed Common Rule Set
                {
                    "name": "AWSManagedRulesCommonRuleSet",
                    "priority": 1,
                    "statement": {"managedRuleGroupStatement": {
                        "vendorName": "AWS",
                        "name": "AWSManagedRulesCommonRuleSet",
                    }},
                    "overrideAction": {"none": {}},
                    "visibilityConfig": {"sampledRequestsEnabled": True,
                                          "cloudWatchMetricsEnabled": True,
                                          "metricName": "common-rules"},
                },
                # Bot control (paid managed rule)
                {
                    "name": "AWSManagedRulesBotControlRuleSet",
                    "priority": 2,
                    "statement": {"managedRuleGroupStatement": {
                        "vendorName": "AWS",
                        "name": "AWSManagedRulesBotControlRuleSet",
                        "managedRuleGroupConfigs": [
                            {"awsManagedRulesBotControlRuleSet": {
                                "inspectionLevel": "COMMON",
                            }},
                        ],
                    }},
                    "overrideAction": {"none": {}},
                    "visibilityConfig": {"sampledRequestsEnabled": True,
                                          "cloudWatchMetricsEnabled": True,
                                          "metricName": "bot-control"},
                },
                # Rate limit per IP
                {
                    "name": "RateLimit",
                    "priority": 3,
                    "statement": {"rateBasedStatement": {
                        "limit": 2000,                               # per 5-min window per IP
                        "aggregateKeyType": "IP",
                    }},
                    "action": {"block": {}},
                    "visibilityConfig": {"sampledRequestsEnabled": True,
                                          "cloudWatchMetricsEnabled": True,
                                          "metricName": "rate-limit"},
                },
                # Geo-block (example: block specific countries)
                {
                    "name": "GeoBlock",
                    "priority": 4,
                    "statement": {"geoMatchStatement": {
                        "countryCodes": ["RU", "KP", "IR"],          # example
                    }},
                    "action": {"block": {}},
                    "visibilityConfig": {"sampledRequestsEnabled": True,
                                          "cloudWatchMetricsEnabled": True,
                                          "metricName": "geo-block"},
                },
            ],
        )

        # ── 4. Origin Access Control (OAC) — modern replacement for OAI ──
        oac = cf.S3OriginAccessControl(self, "OAC",
            origin_access_control_name=f"{env_name}-site-oac",
            description="OAC for static site",
            signing=cf.Signing.SIGV4_ALWAYS,                         # SigV4-sign every request
        )

        # ── 5. Cache policies ─────────────────────────────────────────
        # Custom cache policy for HTML (short TTL)
        html_cache_policy = cf.CachePolicy(self, "HtmlCachePolicy",
            cache_policy_name=f"{env_name}-html",
            comment="Cache HTML for 5 min",
            default_ttl=Duration.minutes(5),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.minutes(5),
            cookie_behavior=cf.CacheCookieBehavior.none(),
            header_behavior=cf.CacheHeaderBehavior.none(),
            query_string_behavior=cf.CacheQueryStringBehavior.none(),
            enable_accept_encoding_brotli=True,
            enable_accept_encoding_gzip=True,
        )

        # Built-in CachingOptimized — for static assets (1y TTL)
        # Built-in CachingDisabled — for API endpoints

        # ── 6. Response Headers Policy — security headers ────────────
        response_headers_policy = cf.ResponseHeadersPolicy(self, "RespHeaders",
            response_headers_policy_name=f"{env_name}-secure-headers",
            comment="Security + CORS headers",
            security_headers_behavior=cf.ResponseSecurityHeadersBehavior(
                strict_transport_security=cf.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                content_type_options=cf.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cf.ResponseHeadersFrameOptions(
                    frame_option=cf.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cf.ResponseHeadersReferrerPolicy(
                    referrer_policy=cf.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                xss_protection=cf.ResponseHeadersXSSProtection(
                    protection=True, mode_block=True, override=True,
                ),
                content_security_policy=cf.ResponseHeadersContentSecurityPolicy(
                    content_security_policy="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:;",
                    override=True,
                ),
            ),
            custom_headers_behavior=cf.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cf.ResponseCustomHeader(
                        header="Permissions-Policy",
                        value="geolocation=(), camera=(), microphone=()",
                        override=True,
                    ),
                ],
            ),
            cors_behavior=cf.ResponseHeadersCorsBehavior(
                access_control_allow_credentials=False,
                access_control_allow_headers=["Authorization", "Content-Type"],
                access_control_allow_methods=["GET", "POST"],
                access_control_allow_origins=["https://app.example.com"],
                access_control_max_age=Duration.hours(1),
                origin_override=True,
            ),
        )

        # ── 7. CloudFront Distribution ───────────────────────────────
        distribution = cf.Distribution(self, "Distribution",
            comment=f"{env_name} static site",
            domain_names=[domain_name],
            certificate=cert,
            default_root_object="index.html",
            minimum_protocol_version=cf.SecurityPolicyProtocol.TLS_V1_2_2021,
            web_acl_id=waf_acl.attr_arn,                           # attach WAF
            geo_restriction=cf.GeoRestriction.allowlist("US", "CA", "GB", "DE"),  # optional
            log_bucket=log_bucket,                                   # access logs
            log_file_prefix="cloudfront-logs/",
            log_includes_cookies=False,
            enable_ipv6=True,
            http_version=cf.HttpVersion.HTTP2_AND_3,                # H2 + H3 (QUIC)
            price_class=cf.PriceClass.PRICE_CLASS_100,              # US/EU/IL (cheapest)
            # Default behavior — for HTML
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    site_bucket,
                    origin_access_control=oac,
                ),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cf.AllowedMethods.ALLOW_GET_HEAD,
                cached_methods=cf.CachedMethods.CACHE_GET_HEAD,
                compress=True,
                cache_policy=html_cache_policy,
                origin_request_policy=cf.OriginRequestPolicy.CORS_S3_ORIGIN,
                response_headers_policy=response_headers_policy,
            ),
            # Path-specific behaviors
            additional_behaviors={
                # Static assets (long cache)
                "/static/*": cf.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(
                        site_bucket, origin_access_control=oac,
                    ),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,    # 1y TTL
                    response_headers_policy=response_headers_policy,
                    compress=True,
                ),
                # API path (no cache)
                "/api/*": cf.BehaviorOptions(
                    origin=origins.HttpOrigin(api_dns_name,
                                                origin_path="/prod"),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                    cache_policy=cf.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    compress=True,
                ),
            },
            # Custom error pages — SPA fallback to /index.html
            error_responses=[
                cf.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",                 # SPA routing
                    ttl=Duration.minutes(0),
                ),
                cf.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(0),
                ),
                cf.ErrorResponse(
                    http_status=500,
                    response_http_status=500,
                    response_page_path="/error/500.html",             # branded error
                    ttl=Duration.minutes(1),
                ),
            ],
        )

        # ── 8. Route 53 alias ────────────────────────────────────────
        r53.ARecord(self, "AliasRecord",
            zone=hosted_zone,
            record_name=domain_name,
            target=r53.RecordTarget.from_alias(
                r53_targets.CloudFrontTarget(distribution),
            ),
        )

        # ── 9. Real-time logs (optional, for debugging) ──────────────
        # cf.RealtimeLogConfig(...)
        # Streams to Kinesis Data Streams; high-volume; opt-in only

        from aws_cdk import CfnOutput
        CfnOutput(self, "DistributionDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
```

---

## 4. Custom origin (ALB / API Gateway / Lambda Function URL)

```python
# ALB origin
distribution = cf.Distribution(self, "Distribution",
    default_behavior=cf.BehaviorOptions(
        origin=origins.LoadBalancerV2Origin(alb,
            protocol_policy=cf.OriginProtocolPolicy.HTTPS_ONLY,
            origin_ssl_protocols=[cf.OriginSslPolicy.TLS_V1_2],
            connection_attempts=3,
            connection_timeout=Duration.seconds(10),
            read_timeout=Duration.seconds(30),
            keepalive_timeout=Duration.seconds(5),
            custom_headers={
                "X-Custom-Header": "from-cloudfront-only",     # ALB checks this
            },
        ),
        viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cache_policy=cf.CachePolicy.CACHING_DISABLED,
        origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER,
    ),
    # ... rest ...
)

# API Gateway origin
distribution = cf.Distribution(self, "Distribution",
    default_behavior=cf.BehaviorOptions(
        origin=origins.RestApiOrigin(rest_api,
            origin_path="/prod",
        ),
        # ...
    ),
)

# Lambda Function URL origin (no API GW needed)
distribution = cf.Distribution(self, "Distribution",
    default_behavior=cf.BehaviorOptions(
        origin=origins.FunctionUrlOrigin(my_lambda.function_url),
        viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        # ...
    ),
)
```

---

## 5. Origin Groups — multi-origin failover

```python
# Primary origin = ALB in us-east-1
# Failover origin = ALB in us-west-2
primary_origin = origins.LoadBalancerV2Origin(alb_us_east_1, ...)
failover_origin = origins.LoadBalancerV2Origin(alb_us_west_2, ...)

origin_group = origins.OriginGroup(
    primary_origin=primary_origin,
    fallback_origin=failover_origin,
    fallback_status_codes=[500, 502, 503, 504],
)

distribution = cf.Distribution(self, "Distribution",
    default_behavior=cf.BehaviorOptions(
        origin=origin_group,
        # ...
    ),
)
# CloudFront automatically tries primary; on 5xx fails over to secondary.
```

---

## 6. Production Variant — Shield Advanced + real-time logs + custom domain alternates

### 6.1 Shield Advanced (DDoS protection, $3000/mo)

```python
from aws_cdk import aws_shield as shield

# Subscribe (one-time per account)
shield_subscription = shield.CfnSubscription(self, "ShieldSub",
    auto_renew="ENABLED",
)

# Protect the distribution
shield.CfnProtection(self, "ShieldProtection",
    name=f"{env_name}-cf-protection",
    resource_arn=f"arn:aws:cloudfront::{self.account}:distribution/{distribution.distribution_id}",
    application_layer_automatic_response_configuration={
        "Action": {"Block": {}},
        "Status": "ENABLED",
    },
)
# Adds: 24/7 DDoS Response Team support, cost protection (no DDoS bills),
#       advanced WAF metrics, ALAR (Auto Application Layer Response).
```

### 6.2 Real-time logs (debugging + analytics)

```python
from aws_cdk import aws_kinesis as kinesis

rt_log_stream = kinesis.Stream(self, "CfRtLogStream",
    stream_mode=kinesis.StreamMode.ON_DEMAND,
    encryption=kinesis.StreamEncryption.KMS,
    encryption_key=kms_key,
)

cf.RealtimeLogConfig(self, "CfRtLog",
    fields=["timestamp", "c-ip", "cs-host", "cs-uri-stem", "sc-status",
             "cs-method", "cs-bytes", "x-edge-result-type",
             "x-edge-detailed-result-type", "x-edge-response-result-type"],
    sampling_rate=100,                                       # %; 100 = all
    endpoints=[
        cf.Endpoint.from_kinesis_stream(rt_log_stream, real_time_log_role),
    ],
    realtime_log_config_name=f"{env_name}-rt-logs",
)

# Attach to a behavior
distribution = cf.Distribution(self, "Distribution",
    default_behavior=cf.BehaviorOptions(
        # ...
        realtime_log_config=rt_log_config,
    ),
)
```

### 6.3 Custom CNAME alternates + IPv6

```python
distribution = cf.Distribution(self, "Distribution",
    domain_names=[
        "app.example.com",
        "www.app.example.com",                                # alternate
        "static.example.com",                                  # CDN-only
    ],
    enable_ipv6=True,                                          # default but verify
    # ...
)
```

---

## 7. Common gotchas

- **ACM cert MUST be in us-east-1** for CloudFront — even if app is in eu-west-1. Common surprise.
- **WAF for CloudFront MUST be CLOUDFRONT scope** + in us-east-1. Other-region WAF won't attach.
- **OAC vs OAI**:
  - **OAI** (legacy): SigV4 unsupported, S3-only, no KMS support.
  - **OAC** (2022+): SigV4 always, supports KMS-encrypted buckets, supports Lambda Function URLs + MediaStore + S3.
  - Always use OAC for new builds.
- **OAC + bucket policy** — CloudFront's OAC uses SigV4; bucket policy must allow `cloudfront.amazonaws.com` with condition `aws:SourceArn = distribution ARN`.
- **HTTP/3 (QUIC)** is opt-in via `http_version: HTTP2_AND_3`. Adds ~5% performance for mobile clients.
- **Price classes**:
  - `PriceClass_All` — all 200+ edge locations (most expensive).
  - `PriceClass_200` — US, EU, MEA, India, JP, Korea, SG, HK, TW, AU, NZ.
  - `PriceClass_100` — US, EU, IL only (cheapest; ~50% savings vs All).
  - Pick based on user geography.
- **Cache invalidation cost** — first 1000/month free; then $0.005/path. For frequent invalidations, prefer cache-busting URLs (`/static/v1.2.3/...`).
- **Default root object** — only applies to root URL (`/`). Sub-paths (`/foo`) don't get default-root behavior. SPA routing via custom error pages instead.
- **SPA routing via 404 → 200 + index.html** — works but hides real 404s. Better: Lambda@Edge or CloudFront Function for path-rewrite.
- **CORS on S3 origin** — CloudFront doesn't proxy `OPTIONS` requests to S3 by default. Use `OriginRequestPolicy.CORS_S3_ORIGIN` + Response Headers Policy with CORS.
- **Real-time logs cost** — Kinesis Data Streams ingestion + storage. 100% sampling at high traffic = $1000s/mo. Sample 5-10% for cost.
- **Distribution propagation = 5-15 minutes**. Cache state takes longer to flush. Plan deploys.
- **Geo-restriction** at distribution level vs WAF — distribution-level is simpler; WAF allows per-rule geo + override.
- **Origin failover** doesn't health-check; only triggers on 5xx response from primary. For active health checks, use Route 53 + ALB.
- **HTTP→HTTPS redirect** is at viewer protocol policy level. `REDIRECT_TO_HTTPS` is the right default.
- **Per-distribution quota: 25 alternate domain names**. For more, use multiple distributions.

---

## 8. Pytest worked example

```python
# tests/test_cloudfront.py
import boto3, pytest, requests, ssl

cf = boto3.client("cloudfront")
waf = boto3.client("wafv2", region_name="us-east-1")


def test_distribution_deployed(distribution_id):
    d = cf.get_distribution(Id=distribution_id)["Distribution"]
    assert d["Status"] == "Deployed"


def test_https_only(domain_name):
    """HTTP should redirect to HTTPS."""
    r = requests.get(f"http://{domain_name}/", allow_redirects=False)
    assert r.status_code in [301, 302, 308]
    assert r.headers["Location"].startswith("https://")


def test_tls_1_3(domain_name):
    """TLS 1.3 should be supported."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    with ssl.create_connection((domain_name, 443)) as sock:
        with ctx.wrap_socket(sock, server_hostname=domain_name) as ssock:
            assert ssock.version() == "TLSv1.3"


def test_security_headers_present(domain_name):
    r = requests.get(f"https://{domain_name}/")
    assert r.headers.get("Strict-Transport-Security")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Content-Security-Policy")


def test_waf_blocks_known_attack(domain_name):
    """SQL injection probe should be blocked."""
    r = requests.get(f"https://{domain_name}/?id=1' OR '1'='1")
    # AWSManagedRulesCommonRuleSet should block
    assert r.status_code == 403


def test_waf_rate_limit(domain_name):
    """Rapid requests from one IP should hit rate limit."""
    blocked = 0
    for _ in range(2500):
        r = requests.get(f"https://{domain_name}/")
        if r.status_code == 403:
            blocked += 1
    assert blocked > 0


def test_origin_access_control_oac(distribution_id):
    """Distribution should use OAC, not OAI (legacy)."""
    d = cf.get_distribution(Id=distribution_id)["Distribution"]
    origins = d["DistributionConfig"]["Origins"]["Items"]
    for o in origins:
        if "S3" in o.get("OriginPath", "") or "s3" in o["DomainName"]:
            assert o.get("OriginAccessControlId"), \
                f"Origin {o['Id']} not using OAC"
            assert not o["S3OriginConfig"]["OriginAccessIdentity"], \
                f"Origin {o['Id']} still using OAI"


def test_404_fallback_to_index_html(domain_name):
    """SPA routing: /nonexistent should return 200 with index.html."""
    r = requests.get(f"https://{domain_name}/nonexistent/path")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
```

---

## 9. Five non-negotiables

1. **OAC (Origin Access Control)** — never use legacy OAI for new builds.
2. **WAF v2 attached** with AWSManagedRulesCommonRuleSet + rate limit + (optional) geo block.
3. **TLS 1.2+ minimum** (`SecurityPolicyProtocol.TLS_V1_2_2021`); HTTP→HTTPS redirect.
4. **Security headers via Response Headers Policy** — HSTS + CSP + X-Frame-Options + X-Content-Type-Options.
5. **CMK encryption** on S3 + KMS key policy allowing CloudFront via `aws:SourceArn` condition.

---

## 10. References

- [CloudFront User Guide](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Introduction.html)
- [Origin Access Control (OAC)](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)
- [Cache + Origin Request + Response Headers Policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/working-with-policies.html)
- [WAF for CloudFront](https://docs.aws.amazon.com/waf/latest/developerguide/cloudfront-features.html)
- [Shield Advanced](https://docs.aws.amazon.com/waf/latest/developerguide/ddos-overview.html)
- [Origin Groups + failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html)
- [Real-time logs](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/real-time-logs.html)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. CloudFront + OAC + cache behaviors + 3 policy types + custom error pages + WAF v2 (CLOUDFRONT scope) + Shield Standard/Advanced + multi-origin + ACM us-east-1. Wave 17. |
