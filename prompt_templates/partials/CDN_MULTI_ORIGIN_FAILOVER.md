# SOP — CloudFront Multi-Origin Failover (origin groups · health checks · cross-region · per-region routing · weighted A/B)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · CloudFront Origin Groups (passive failover on 5xx) · Multi-region origins via Lambda@Edge (active routing) · Route 53 health-check-based DNS failover (alternative pattern) · Latency-based + geo-based routing · Weighted origins for canary

---

## 1. Purpose

- Codify **CloudFront origin failover** strategies — when to use Origin Groups (CF-native, passive) vs Lambda@Edge routing (programmable, active) vs Route 53 health-checks (DNS-based, slow but flexible).
- Codify **Origin Groups** — primary + fallback origins; CF auto-fails over on configurable 5xx status codes.
- Codify **Lambda@Edge per-region origin selection** — based on viewer country, latency, or readiness check.
- Codify **canary / A/B via weighted origins** — route 10% to v2, 90% to v1.
- Codify the **cross-region origin pattern** — primary in us-east-1, failover in us-west-2 (each with own ALB + workload).
- Codify the **interaction with `DR_MULTI_REGION_PATTERNS`** — CloudFront can be the front door for multi-region failover.
- Pairs with `CDN_CLOUDFRONT_FOUNDATION` (distribution + behaviors), `CDN_EDGE_COMPUTE` (Lambda@Edge), `DR_MULTI_REGION_PATTERNS` (DR architecture).

When the SOW signals: "global app with regional failover", "active-active CDN routing", "canary at edge", "multi-origin", "weighted routing".

---

## 2. Decision tree — failover/routing strategy

| Need | Origin Group (passive) | Lambda@Edge (active) | Route 53 (DNS) |
|---|:---:|:---:|:---:|
| Failover on 5xx | ✅ best | ⚠️ via response check | ⚠️ TTL bound |
| Active routing by country/region | ❌ | ✅ best | ✅ geo records |
| Latency-based routing | ❌ | ⚠️ approximate | ✅ best |
| Weighted A/B (10% / 90%) | ❌ | ✅ via random | ✅ weighted records |
| Health-check based failover (active probing) | ❌ | ⚠️ custom | ✅ |
| Sub-second failover | ✅ ~100ms | ✅ | ❌ DNS TTL |
| Customer-impact during failover | ✅ minimal | ✅ minimal | ⚠️ DNS-cache impact |
| Cost | ✅ free | $$ Lambda@Edge | $$ R53 health checks |

**Recommendation:**
- **Origin Group** for "primary fails → fall back" (most common DR pattern).
- **Lambda@Edge** for active geo routing or canary (10% to new region).
- **Route 53** for cross-distribution failover (e.g., entire CloudFront distro is down).

```
Common architecture — multi-region active-passive:

   Global users
        │
        ▼
   CloudFront Distribution (single, global)
        │
        │ Origin Group:
        │   Primary: ALB-us-east-1 (active)
        │   Fallback: ALB-us-west-2 (warm standby)
        │   Failover criteria: 503, 504, etc.
        ▼
   ALB-us-east-1                     ALB-us-west-2
        │                                  │
        ▼                                  ▼
   ECS-us-east-1                     ECS-us-west-2
   (10 tasks)                        (2 tasks; warm)
        │                                  │
        ▼                                  ▼
   Aurora Global writer            Aurora Global reader (promotable)
        │                                  ▲
        │ Aurora Global replication (RPO < 1s)
        └──────────────────────────────────┘
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Origin Group with 2 origins | **§3 Origin Group** |
| Production — Lambda@Edge geo routing + canary + Origin Group | **§5 Active Routing** |

---

## 3. Origin Group — passive failover

### 3.1 CDK

```python
# stacks/cdn_failover_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct


class CdnFailoverStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 primary_alb: elbv2.IApplicationLoadBalancer,
                 fallback_alb: elbv2.IApplicationLoadBalancer,
                 hosted_zone, domain_name, cert, kms_key, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Primary origin (ALB in us-east-1) ─────────────────────
        primary = origins.LoadBalancerV2Origin(primary_alb,
            protocol_policy=cf.OriginProtocolPolicy.HTTPS_ONLY,
            origin_ssl_protocols=[cf.OriginSslPolicy.TLS_V1_2],
            connection_attempts=3,
            connection_timeout=Duration.seconds(10),
            read_timeout=Duration.seconds(30),
            keepalive_timeout=Duration.seconds(5),
            custom_headers={"X-CloudFront-Verify": "primary-only"},
        )

        # ── 2. Fallback origin (ALB in us-west-2) ────────────────────
        fallback = origins.LoadBalancerV2Origin(fallback_alb,
            protocol_policy=cf.OriginProtocolPolicy.HTTPS_ONLY,
            origin_ssl_protocols=[cf.OriginSslPolicy.TLS_V1_2],
            connection_attempts=3,
            connection_timeout=Duration.seconds(10),
            read_timeout=Duration.seconds(30),
            keepalive_timeout=Duration.seconds(5),
            custom_headers={"X-CloudFront-Verify": "fallback-only"},
        )

        # ── 3. Origin Group ──────────────────────────────────────────
        origin_group = origins.OriginGroup(
            primary_origin=primary,
            fallback_origin=fallback,
            fallback_status_codes=[500, 502, 503, 504],          # which codes trigger failover
        )

        # ── 4. Distribution ──────────────────────────────────────────
        distribution = cf.Distribution(self, "Distribution",
            comment="Multi-region active-passive distribution",
            domain_names=[domain_name],
            certificate=cert,
            default_behavior=cf.BehaviorOptions(
                origin=origin_group,                                 # KEY
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                cache_policy=cf.CachePolicy.CACHING_DISABLED,        # API; no cache
                origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER,
                compress=True,
            ),
            web_acl_id=waf_acl_arn,
            minimum_protocol_version=cf.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
```

### 3.2 ALB-side handshake check

```python
# In primary ALB listener — only allow CloudFront with custom header
primary_alb_listener.add_action("VerifyCloudFront",
    priority=10,
    conditions=[
        elbv2.ListenerCondition.http_header("X-CloudFront-Verify", ["primary-only"]),
    ],
    action=elbv2.ListenerAction.forward([primary_target_group]),
)
primary_alb_listener.add_action("BlockNonCloudFront",
    priority=100,
    conditions=[
        elbv2.ListenerCondition.path_patterns(["*"]),
    ],
    action=elbv2.ListenerAction.fixed_response(403, content_type="text/plain",
                                                 message_body="Direct ALB access blocked"),
)
# Now ALB only serves traffic from CloudFront (auth via shared header).
```

---

## 4. Lambda@Edge active routing

### 4.1 Per-country origin selection

```javascript
// src/edge/origin-router/index.js
'use strict';

const ORIGIN_MAP = {
    'US': {domain: 'api-us-east-1.example.com', path: ''},
    'CA': {domain: 'api-us-east-1.example.com', path: ''},
    'GB': {domain: 'api-eu-west-1.example.com', path: ''},
    'DE': {domain: 'api-eu-west-1.example.com', path: ''},
    'JP': {domain: 'api-ap-northeast-1.example.com', path: ''},
    'AU': {domain: 'api-ap-southeast-2.example.com', path: ''},
};
const DEFAULT_ORIGIN = {domain: 'api-us-east-1.example.com', path: ''};

exports.handler = async (event) => {
    const request = event.Records[0].cf.request;
    
    // CF auto-injects viewer-country header at viewer-request stage
    const country = request.headers['cloudfront-viewer-country'] &&
                    request.headers['cloudfront-viewer-country'][0].value;
    
    const target = ORIGIN_MAP[country] || DEFAULT_ORIGIN;
    
    request.origin = {
        custom: {
            domainName: target.domain,
            path: target.path,
            port: 443,
            protocol: 'https',
            sslProtocols: ['TLSv1.2'],
            readTimeout: 30,
            keepaliveTimeout: 5,
            customHeaders: {
                'x-cloudfront-verify': [{
                    key: 'X-CloudFront-Verify',
                    value: 'edge-routed',
                }],
            },
        },
    };
    request.headers['host'] = [{key: 'Host', value: target.domain}];
    
    return request;
};
```

```python
# Attach as origin-request Lambda@Edge
distribution.add_behavior("/api/*",
    origin=primary_origin,  # placeholder; Lambda@Edge will override
    edge_lambdas=[
        cf.EdgeLambda(
            function_version=origin_router_fn.current_version,
            event_type=cf.LambdaEdgeEventType.ORIGIN_REQUEST,
        ),
    ],
    cache_policy=cf.CachePolicy.CACHING_DISABLED,
    viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
)
```

### 4.2 Weighted A/B (canary at edge)

```javascript
// Route 10% of traffic to canary origin
exports.handler = async (event) => {
    const request = event.Records[0].cf.request;
    
    // Sticky cookie wins (preserve user's variant)
    const cookies = parseCookies(request.headers.cookie);
    let variant = cookies.variant;
    
    if (!variant) {
        variant = Math.random() < 0.1 ? 'canary' : 'stable';
        // Note: setting cookie at origin-request requires response-side Function
    }
    
    if (variant === 'canary') {
        request.origin.custom.domainName = 'canary-api.example.com';
        request.headers['host'] = [{key: 'Host', value: 'canary-api.example.com'}];
    }
    
    return request;
};
```

---

## 5. Route 53 — DNS-based failover (alternative)

```python
from aws_cdk import aws_route53 as r53

# CloudFront primary distribution
cf_primary = cf.Distribution(self, "CfPrimary", ...)

# Backup CloudFront distribution OR alternate-region ALB
backup_alb = elbv2.ApplicationLoadBalancer.from_lookup(self, "Backup",
    region="us-west-2", load_balancer_arn=backup_alb_arn)

# Route 53 health check (on primary)
primary_health = r53.CfnHealthCheck(self, "PrimaryHealthCheck",
    health_check_config=r53.CfnHealthCheck.HealthCheckConfigProperty(
        type="HTTPS",
        fully_qualified_domain_name=cf_primary.distribution_domain_name,
        resource_path="/healthz",
        request_interval=10,                                     # check every 10s
        failure_threshold=3,                                      # 3 consecutive fails = unhealthy
    ),
)

# Failover record set
r53.CfnRecordSet(self, "PrimaryRecord",
    hosted_zone_id=hosted_zone_id,
    name="app.example.com",
    type="A",
    set_identifier="primary",
    failover="PRIMARY",
    health_check_id=primary_health.attr_health_check_id,
    alias_target=r53.CfnRecordSet.AliasTargetProperty(
        dns_name=cf_primary.distribution_domain_name,
        hosted_zone_id="Z2FDTNDATAQYW2",                          # CloudFront global
        evaluate_target_health=False,                              # CF doesn't expose
    ),
)

r53.CfnRecordSet(self, "SecondaryRecord",
    hosted_zone_id=hosted_zone_id,
    name="app.example.com",
    type="A",
    set_identifier="secondary",
    failover="SECONDARY",
    alias_target=r53.CfnRecordSet.AliasTargetProperty(
        dns_name=backup_alb.load_balancer_dns_name,
        hosted_zone_id=backup_alb.load_balancer_canonical_hosted_zone_id,
        evaluate_target_health=True,
    ),
)
# Failover happens at DNS resolution — clients get backup IP after primary fails health check.
# TTL = 60s typically; clients with cached DNS hold old IP for up to TTL.
```

---

## 6. Common gotchas

- **Origin Group failover triggers on response, not connection** — CF must get a response (5xx) from primary; if primary is unreachable, CF retries N times before declaring failure (slow).
- **Origin Group `fallback_status_codes`** — typical [500, 502, 503, 504]. Don't include 4xx (would fail over on every bad request).
- **Origin Group + caching** — failed primary response is cached (negative cache); during outage, all subsequent requests skip primary check + go to fallback. After primary recovers, cached negative responses still fail-over until TTL expires.
- **Origin Group + sticky sessions** don't work — every request can route to either origin. Apps must be stateless.
- **Lambda@Edge per-country origin routing** — `cloudfront-viewer-country` header only at viewer-request stage. At origin-request, header is preserved; at viewer-response, only top-level country.
- **Lambda@Edge cost** for active routing = $0.60/1M invocations × all viewer-requests. At 100M/mo = $60/mo. Cache aggressively to reduce.
- **Route 53 health checks** check from 8+ global checker locations; need ≥ 3 consecutive failures from majority. Failover decision = ~30s. + DNS propagation = ~60s. Total = 90s+.
- **Cross-region failover for stateful apps** requires data layer support — Aurora Global secondary needs promotion (~1 min); during failover, writes fail until promotion.
- **CloudFront cache pollution during failover** — cached responses from failed primary still serve until TTL. For DR testing, consider invalidation post-failover.
- **Custom header verification** — set `X-CloudFront-Verify: <secret>` at CloudFront → ALB checks → blocks direct access. Rotate secret quarterly.
- **Multi-region cost** — fallback ALB + warm ECS = ~50-100% extra infra. Plan budget.
- **Failover testing** — easy to break: kill primary ALB; validate response served by fallback; restore + verify recovery. Do quarterly.

---

## 7. Pytest worked example

```python
# tests/test_cdn_failover.py
import boto3, requests, pytest

cf = boto3.client("cloudfront")
elbv2 = boto3.client("elbv2")


def test_origin_group_configured(distribution_id):
    d = cf.get_distribution(Id=distribution_id)["Distribution"]
    config = d["DistributionConfig"]
    origin_groups = config.get("OriginGroups", {}).get("Items", [])
    assert origin_groups
    og = origin_groups[0]
    failover_codes = og["FailoverCriteria"]["StatusCodes"]["Items"]
    assert 502 in failover_codes
    assert 503 in failover_codes
    assert 504 in failover_codes


def test_failover_happens_on_primary_5xx(domain_name, primary_alb_arn):
    """Disable primary ALB (drain targets) → request returns 200 from fallback."""
    # Drain primary ALB targets
    targets = elbv2.describe_target_health(TargetGroupArn=primary_tg_arn)["TargetHealthDescriptions"]
    elbv2.deregister_targets(
        TargetGroupArn=primary_tg_arn,
        Targets=[{"Id": t["Target"]["Id"], "Port": t["Target"]["Port"]} for t in targets],
    )
    
    # Wait for ALB to start failing (5xx from primary)
    import time
    time.sleep(30)
    
    # Hit CloudFront — should fall over
    r = requests.get(f"https://{domain_name}/api/healthz")
    assert r.status_code == 200
    # Verify response came from fallback (custom header)
    assert r.headers.get("x-served-by-region") == "us-west-2"
    
    # Cleanup: re-register primary
    # ...


def test_lambda_edge_routes_uk_to_eu_origin():
    """UK user (CF country header) routes to eu-west-1."""
    # Use proxy / VPN with UK exit; or test the Lambda directly
    pass


def test_canary_routes_10pct(domain_name):
    """100 sequential requests should split ~10/90 between canary/stable."""
    canary_count = 0
    for _ in range(100):
        r = requests.get(f"https://{domain_name}/api/version")
        if r.headers.get("X-Variant") == "canary":
            canary_count += 1
    # Allow noise; expect 5-20 canary
    assert 5 <= canary_count <= 20
```

---

## 8. Five non-negotiables

1. **Origin Group for any multi-region active-passive** — never rely on Route 53 alone for sub-30s failover.
2. **Custom header (`X-CloudFront-Verify: <rotated-secret>`)** between CF and ALB — block direct ALB access.
3. **Stateless app design** — Origin Group + Lambda@Edge route to either origin per request; no sticky state.
4. **Quarterly failover test** — drain primary; verify fallback serves; restore; verify recovery.
5. **Negative cache TTL ≤ 60s** — don't cache 5xx responses for hours; failover storms.

---

## 9. References

- [Origin Groups + failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html)
- [Lambda@Edge for origin routing](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/lambda-examples.html#lambda-examples-content-based-routing-examples)
- [Route 53 failover routing](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/dns-failover.html)
- [CloudFront viewer headers](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-cloudfront-headers.html#cloudfront-headers-viewer-location)
- [DR_MULTI_REGION_PATTERNS partial](DR_MULTI_REGION_PATTERNS.md) — broader DR architecture

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Origin Groups (passive failover) + Lambda@Edge (active per-country routing) + Route 53 alternative + canary/A/B at edge + failover testing. Wave 17. |
