# SOP — Frontend Layer (S3 + CloudFront + OAC)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · React / Vite SPA build artifacts

---

## 1. Purpose

Host a React (or any static SPA) behind CloudFront with:

- Origin Access Control (OAC) — the modern replacement for OAI
- HTTPS only, TLS 1.2+, custom domain (optional)
- SPA error-page rewrites (403/404 → `/index.html`)
- Cache policies (long TTL for static, no-cache for HTML shell)
- WAF + bot control (optional; see `SECURITY_WAF_SHIELD_MACIE`)

---

## 2. Decision — Monolith vs Micro-Stack

**THIS IS THE CANONICAL OAC CROSS-STACK CYCLE CASE.** Read §4 before splitting.

| You are… | Use variant |
|---|---|
| S3 bucket + CloudFront distribution + BucketDeployment all in one stack | **§3 Monolith Variant** |
| Separate `FrontendStack` (bucket) and `CdnStack` (distribution) in different CDK stacks | **§4 Micro-Stack Variant (ONE CORRECT WAY)** |

**Why the split is a landmine.** `origins.S3BucketOrigin.with_origin_access_control(bucket, ...)` auto-grants `s3:GetObject` on the bucket's resource policy referencing the distribution's ARN. If bucket and distribution are in different stacks, this creates an immediate cross-stack circular export:

- CdnStack needs `bucket.bucket_domain_name` (for origin)
- BucketStack's policy needs `distribution.distribution_arn` (for OAC grant)

**The fix is NOT** to try to break the cycle with manual policies. **The fix IS** to own the bucket in CdnStack. The bucket and the distribution belong in the same CDK stack because they're inseparable at the IAM level.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy, Duration,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
)


def _create_frontend(self, stage: str) -> None:
    self.frontend_bucket = s3.Bucket(
        self, "FrontendBucket",
        bucket_name=f"{{project_name}}-frontend-{stage}",
        encryption=s3.BucketEncryption.S3_MANAGED,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=RemovalPolicy.DESTROY if stage != "prod" else RemovalPolicy.RETAIN,
        auto_delete_objects=stage != "prod",
    )

    oac = cf.S3OriginAccessControl(self, "OAC")

    domain_names = [f"{{custom_domain_name}}"] if "{use_custom_domain}" == "true" else None
    certificate = (
        acm.Certificate.from_certificate_arn(self, "Cert", "{acm_certificate_arn}")
        if domain_names else None
    )

    self.distribution = cf.Distribution(
        self, "Cdn",
        comment=f"{{project_name}}-{stage}",
        default_behavior=cf.BehaviorOptions(
            origin=origins.S3BucketOrigin.with_origin_access_control(
                self.frontend_bucket, origin_access_control=oac,
            ),
            viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
            response_headers_policy=cf.ResponseHeadersPolicy.SECURITY_HEADERS,
            compress=True,
        ),
        default_root_object="index.html",
        minimum_protocol_version=cf.SecurityPolicyProtocol.TLS_V1_2_2021,
        error_responses=[
            cf.ErrorResponse(
                http_status=403, response_http_status=200,
                response_page_path="/index.html", ttl=Duration.seconds(0),
            ),
            cf.ErrorResponse(
                http_status=404, response_http_status=200,
                response_page_path="/index.html", ttl=Duration.seconds(0),
            ),
        ],
        domain_names=domain_names, certificate=certificate,
    )

    # Deploy React build artifacts
    s3deploy.BucketDeployment(
        self, "DeployReact",
        sources=[s3deploy.Source.asset("frontend/dist")],
        destination_bucket=self.frontend_bucket,
        distribution=self.distribution,
        distribution_paths=["/*"],
        prune=True,
        retain_on_delete=stage == "prod",
    )

    cdk.CfnOutput(self, "CdnUrl",          value=f"https://{self.distribution.distribution_domain_name}")
    cdk.CfnOutput(self, "DistributionId",  value=self.distribution.distribution_id)
```

### 3.1 Monolith gotchas

- **`S3BucketOrigin.with_origin_access_control`** auto-writes a bucket policy statement. Works fine in monolith (same stack).
- **`BucketDeployment`** needs local Docker or `use_efs=False`. Size ≤ 512 MB.
- **`response_headers_policy=SECURITY_HEADERS`** applies a managed policy; customize via `cf.ResponseHeadersPolicy` to add CSP.
- **Custom domain certificate** MUST be in `us-east-1` for CloudFront, regardless of your app region.

---

## 4. Micro-Stack Variant — THE CORRECT PATTERN

**Principle:** the frontend bucket lives in `CdnStack`, not `FrontendStack`. `FrontendStack` (if it exists) becomes a deploy-only stack or is merged into `CdnStack` entirely.

### 4.1 `CdnStack` — owns bucket + distribution

```python
import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy, Duration,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
)
from constructs import Construct


class CdnStack(cdk.Stack):
    """Owns frontend bucket + CloudFront distribution together.

    These two resources are inseparable at the IAM level — the bucket's policy
    MUST reference the distribution's ARN (via OAC). Splitting them across
    stacks creates an unavoidable circular CloudFormation export.
    """

    def __init__(
        self,
        scope: Construct,
        api_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-cdn", **kwargs)

        self.frontend_bucket = s3.Bucket(
            self, "FrontendBucket",
            bucket_name="{project_name}-frontend",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # POC
            auto_delete_objects=True,
        )

        oac = cf.S3OriginAccessControl(self, "OAC")

        self.distribution = cf.Distribution(
            self, "Cdn",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self.frontend_bucket, origin_access_control=oac,
                ),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cf.ResponseHeadersPolicy.SECURITY_HEADERS,
                compress=True,
            ),
            default_root_object="index.html",
            minimum_protocol_version=cf.SecurityPolicyProtocol.TLS_V1_2_2021,
            error_responses=[
                cf.ErrorResponse(http_status=403, response_http_status=200,
                                  response_page_path="/index.html", ttl=Duration.seconds(0)),
                cf.ErrorResponse(http_status=404, response_http_status=200,
                                  response_page_path="/index.html", ttl=Duration.seconds(0)),
            ],
        )

        # OPTIONAL: deploy React build if dist/ exists at synth time
        # Comment out if the deployment happens via a separate CI step.
        # s3deploy.BucketDeployment(
        #     self, "DeployReact",
        #     sources=[s3deploy.Source.asset("frontend/dist")],
        #     destination_bucket=self.frontend_bucket,
        #     distribution=self.distribution,
        #     distribution_paths=["/*"],
        # )

        cdk.CfnOutput(self, "CdnUrl",         value=f"https://{self.distribution.distribution_domain_name}")
        cdk.CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(self, "FrontendBucket", value=self.frontend_bucket.bucket_name)
```

### 4.2 Optional `FrontendStack` — deploy-only

If you want a separate stack for deployment cadence (e.g. frontend team deploys 10x/day, infra team rarely), create it AFTER CdnStack and consume `cdn.frontend_bucket` one-way.

```python
class FrontendDeployStack(cdk.Stack):
    def __init__(self, scope, frontend_bucket: s3.IBucket, distribution: cf.IDistribution, **kwargs):
        super().__init__(scope, "{project_name}-frontend-deploy", **kwargs)
        s3deploy.BucketDeployment(
            self, "DeployReact",
            sources=[s3deploy.Source.asset("frontend/dist")],
            destination_bucket=frontend_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
        )
```

`BucketDeployment` is a custom resource; it grants its own Lambda temporary write access to the bucket. CDK scopes this correctly across stacks. Safe.

### 4.3 Micro-stack gotchas

- **Never create the bucket in `FrontendStack` and the distribution in `CdnStack`.** This is the cycle that bit us.
- **`BucketDeployment.retain_on_delete`** defaults to `False` in non-prod; flipping this may orphan a Lambda role.
- **`distribution.distribution_id`** is a token; don't interpolate it into a string that ends up in another stack's resource policy (same OAC cycle trap in a different disguise).
- **Custom domain certificate** must be in `us-east-1`. If your main stack is in a different region, use `cross_region_references=True` on the app OR create a separate `us-east-1` certificate stack.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / single team | Monolith — bucket + distribution together |
| Frontend deployed independently from infra | CdnStack owns both resources; optional FrontendDeployStack does only BucketDeployment |
| Multiple distributions (public + internal) share one bucket | Rare; nearly always an anti-pattern. Create two buckets |
| Need WAF | Add `web_acl_id=` to `cf.Distribution` — see `SECURITY_WAF_SHIELD_MACIE` |

---

## 6. Worked example — verify no cycle

```python
def test_cdn_stack_synthesizes_without_cross_stack_cycle():
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from infrastructure.cdk.stacks.cdn_stack import CdnStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    # No separate FrontendStack — CdnStack owns its bucket
    cdn = CdnStack(app, api_url="https://api.example.com/v1", env=env)

    t = Template.from_stack(cdn)
    t.resource_count_is("AWS::CloudFront::Distribution", 1)
    t.resource_count_is("AWS::S3::Bucket", 1)
    # If a cycle existed, the from_stack() call would raise during synth.
```

---

## 7. References

- `docs/template_params.md` — `CUSTOM_DOMAIN_NAME`, `ACM_CERTIFICATE_ARN`
- `docs/Feature_Roadmap.md` — CDN-01..CDN-11, FE-01..FE-20
- Related SOPs: `LAYER_API` (CORS origin), `SECURITY_WAF_SHIELD_MACIE` (WAF attach)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Micro-Stack variant explicitly mandates: bucket + distribution in the same stack. Documented the OAC cross-stack cycle as the canonical landmine. |
| 1.0 | 2026-03-05 | Initial (bucket in FrontendStack, distribution in CdnStack — CYCLE-PRONE). |
