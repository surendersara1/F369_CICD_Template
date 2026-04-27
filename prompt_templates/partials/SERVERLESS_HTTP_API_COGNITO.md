# SOP — API Gateway HTTP API + Cognito (JWT authorizer · Lambda integration · CORS · throttling · custom domain)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · API Gateway v2 (HTTP API) · Cognito User Pool + Identity Pool · JWT authorizer · Lambda proxy integration · CORS · Custom domain via Route 53 + ACM · WAF v2 · throttling

---

## 1. Purpose

- Codify **HTTP API as the modern serverless REST default** — 70% cheaper than REST API ($1/M vs $3.50/M), faster cold start, native JWT authorizer, Lambda proxy integration as one-liner.
- Codify **Cognito User Pool + JWT authorizer** as the canonical auth pattern for new APIs (replaces custom Lambda authorizers for 90% of cases).
- Codify **CORS configuration** at the API level (no more Lambda CORS shimming).
- Codify **throttling** per route + per stage + per usage plan (HTTP API doesn't support API keys natively — use Cognito client + IAM patterns instead).
- Codify **custom domain** with Route 53 + ACM regional cert + base path mapping.
- Codify **WAF v2** integration (HTTP API supports WAF since 2024).
- This is the **modern serverless API specialisation**. Built on `LAYER_API` (REST + WebSocket base). Use HTTP API for new builds; REST API only for legacy + features HTTP API lacks.

When the SOW signals: "build a serverless API", "JWT auth", "Cognito SSO", "modern REST API", "API cost reduction".

---

## 2. Decision tree — HTTP API vs REST API vs AppSync

| Need | HTTP API | REST API | AppSync (GraphQL) |
|---|:---:|:---:|:---:|
| JWT (Cognito/OIDC) auth | ✅ native | ⚠️ via Lambda authorizer | ✅ native |
| Cost ($/M req) | ✅ $1.00 | $3.50 | $4.00 (queries) + $2/M subscription-min |
| Cold start | ✅ ~10ms | ~50ms | ~100ms |
| Native API keys | ❌ | ✅ | ❌ |
| Request validation | ⚠️ schema only | ✅ rich | ✅ via JS resolvers |
| WAF | ✅ (2024+) | ✅ | ✅ |
| Custom authorizers | Lambda only | Lambda + IAM + Cognito | Lambda + IAM + Cognito + OIDC + API key |
| WebSocket | ✅ separate WebSocket API | ❌ | ✅ subscriptions |
| Caching | ❌ | ✅ ($/hr edge cache) | ✅ |

**Recommendation:** HTTP API for new REST builds; AppSync if GraphQL fits domain; REST API only when you need API keys or request validation.

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single API + Cognito + 3 routes | **§3 Monolith** |
| Production — API + Lambda authorizer fallback + custom domain + WAF | **§4 Production** |

---

## 3. Monolith Variant — HTTP API + Cognito JWT + Lambda

### 3.1 Architecture

```
   Browser ──────► Route 53 (api.example.com)
                        │
                        ▼
                  CloudFront (optional — for global edge)
                        │
                        ▼
                  Custom domain ──► HTTP API
                                    │
                                    ├── /healthz       (no auth)
                                    ├── /orders        (Cognito JWT)
                                    ├── /orders/{id}   (Cognito JWT)
                                    └── /admin/*       (Cognito JWT, admin group)
                                    │
                                    ▼
                              Lambda functions
                                    │
                                    ▼
                              DynamoDB (single-table)
```

### 3.2 CDK — Cognito + HTTP API + Lambda

```python
# stacks/api_stack.py
from aws_cdk import Stack, Duration, CfnOutput
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_auth
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_int
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_route53 as r53
from aws_cdk import aws_route53_targets as r53_targets
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_wafv2 as waf
from constructs import Construct


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        hosted_zone: r53.IHostedZone,
        domain_name: str,                     # api.example.com
        powertools_layer: _lambda.ILayerVersion,
        ddb_table_arn: str,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. Cognito User Pool ──────────────────────────────────────
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"{env_name}-app-pool",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False),
                given_name=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True, require_uppercase=True,
                require_digits=True, require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OPTIONAL,             # OPTIONAL or REQUIRED for prod
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            advanced_security_mode=cognito.AdvancedSecurityMode.ENFORCED,
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Welcome",
                email_body="Your temp password is {####}. Sign in at https://app.example.com",
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # User pool groups (for RBAC)
        cognito.CfnUserPoolGroup(self, "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="admin", description="Admin users",
            precedence=1,
        )

        # App client (used by web frontend)
        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.EMAIL, cognito.OAuthScope.OPENID,
                        cognito.OAuthScope.PROFILE],
                callback_urls=[f"https://app.example.com/callback"],
                logout_urls=[f"https://app.example.com/"],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
            generate_secret=False,                    # SPA can't store secret
            auth_flows=cognito.AuthFlow(
                user_srp=True,                        # SRP — secure remote password
                user_password=False,                  # never use plaintext flow
            ),
        )

        # User pool domain (for hosted UI)
        self.user_pool.add_domain("HostedDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{env_name}-app"),
        )

        # ── 2. Lambda function (with Powertools) ─────────────────────
        self.api_fn = _lambda.Function(self, "ApiFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("src/api"),
            layers=[powertools_layer],
            timeout=Duration.seconds(10),
            memory_size=512,
            environment={
                "POWERTOOLS_SERVICE_NAME": "api",
                "POWERTOOLS_METRICS_NAMESPACE": "App",
                "POWERTOOLS_LOG_LEVEL": "INFO",
                "DDB_TABLE": ddb_table_arn.split("/")[-1],
            },
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=_lambda.RetentionDays.ONE_MONTH,
        )

        # ── 3. JWT authorizer (Cognito) ───────────────────────────────
        jwt_authorizer = apigwv2_auth.HttpUserPoolAuthorizer(
            "CognitoAuth",
            self.user_pool,
            user_pool_clients=[self.user_pool_client],
            authorizer_name="cognito-jwt",
            identity_source=["$request.header.Authorization"],
        )

        # ── 4. Custom domain ──────────────────────────────────────────
        cert = acm.Certificate(self, "ApiCert",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        custom_domain = apigwv2.DomainName(self, "ApiDomain",
            domain_name=domain_name,
            certificate=cert,
            endpoint_type=apigwv2.EndpointType.REGIONAL,
            security_policy=apigwv2.SecurityPolicy.TLS_1_2,
        )

        # ── 5. HTTP API ──────────────────────────────────────────────
        self.api = apigwv2.HttpApi(self, "HttpApi",
            api_name=f"{env_name}-app-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["https://app.example.com"],
                allow_methods=[apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST,
                               apigwv2.CorsHttpMethod.PUT, apigwv2.CorsHttpMethod.DELETE,
                               apigwv2.CorsHttpMethod.OPTIONS],
                allow_headers=["Authorization", "Content-Type", "X-Correlation-Id"],
                allow_credentials=True,
                max_age=Duration.hours(1),
            ),
            default_authorizer=jwt_authorizer,
            disable_execute_api_endpoint=True,        # force traffic via custom domain
            default_domain_mapping=apigwv2.DomainMappingOptions(
                domain_name=custom_domain,
            ),
        )

        # ── 6. Routes ────────────────────────────────────────────────
        lambda_int = apigwv2_int.HttpLambdaIntegration("LambdaInt", self.api_fn)

        # Public — no auth
        self.api.add_routes(
            path="/healthz",
            methods=[apigwv2.HttpMethod.GET],
            integration=lambda_int,
            authorizer=apigwv2_auth.HttpNoneAuthorizer(),
        )

        # Auth required (default authorizer)
        self.api.add_routes(
            path="/orders",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=lambda_int,
        )
        self.api.add_routes(
            path="/orders/{id}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=lambda_int,
        )
        # Admin-only — JWT scope check inside handler (HTTP API doesn't support scope-per-route)
        self.api.add_routes(
            path="/admin/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=lambda_int,
        )

        # ── 7. Throttling (default stage) ─────────────────────────────
        default_stage = self.api.default_stage.node.default_child
        default_stage.add_property_override("DefaultRouteSettings.ThrottlingBurstLimit", 1000)
        default_stage.add_property_override("DefaultRouteSettings.ThrottlingRateLimit", 500)
        # Stage-level access logs
        default_stage.add_property_override("AccessLogSettings.DestinationArn",
            access_log_group.log_group_arn)
        default_stage.add_property_override("AccessLogSettings.Format",
            json.dumps({
                "requestId": "$context.requestId",
                "ip": "$context.identity.sourceIp",
                "user": "$context.authorizer.claims.sub",
                "email": "$context.authorizer.claims.email",
                "method": "$context.httpMethod",
                "path": "$context.path",
                "status": "$context.status",
                "latency_ms": "$context.responseLatency",
                "integration_latency_ms": "$context.integrationLatency",
            }),
        )

        # ── 8. Route 53 alias ────────────────────────────────────────
        r53.ARecord(self, "ApiAlias",
            zone=hosted_zone, record_name=domain_name,
            target=r53.RecordTarget.from_alias(
                r53_targets.ApiGatewayv2DomainProperties(
                    custom_domain.regional_domain_name,
                    custom_domain.regional_hosted_zone_id,
                ),
            ),
        )

        CfnOutput(self, "ApiUrl", value=f"https://{domain_name}")
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
```

### 3.3 Lambda handler — JWT claims access

```python
# src/api/handler.py
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response
from aws_lambda_powertools import Logger, Tracer, Metrics

logger = Logger()
tracer = Tracer()
metrics = Metrics()
app = APIGatewayHttpResolver()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/orders")
def list_orders():
    user_id = app.current_event.request_context.authorizer.jwt_claim["sub"]
    email = app.current_event.request_context.authorizer.jwt_claim["email"]
    logger.info("list_orders", extra={"user_id": user_id, "email": email})
    # ... fetch orders for user ...
    return {"orders": [...]}


@app.post("/orders")
def create_order():
    user_id = app.current_event.request_context.authorizer.jwt_claim["sub"]
    body = app.current_event.json_body
    # ... create order ...
    return Response(status_code=201, content_type="application/json", body={"id": "..."})


@app.get("/admin/<proxy>")
def admin_handler(proxy: str):
    """Admin routes — check group membership in JWT claims."""
    groups = app.current_event.request_context.authorizer.jwt_claim.get("cognito:groups", [])
    if "admin" not in groups:
        return Response(status_code=403, content_type="application/json",
                        body={"error": "admin required"})
    # ... handle admin action ...
    return {"action": proxy}


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event, context):
    return app.resolve(event, context)
```

---

## 4. Production Variant — adds WAF + multi-region + Lambda authorizer fallback

```python
# Stage 1: WAF v2 ACL
waf_acl = waf.CfnWebACL(self, "ApiWaf",
    name=f"{env_name}-api-acl",
    scope="REGIONAL",
    default_action={"allow": {}},
    visibility_config={
        "sampledRequestsEnabled": True, "cloudWatchMetricsEnabled": True,
        "metricName": f"{env_name}-api-acl",
    },
    rules=[
        {
            "name": "AWSManagedRulesCommonRuleSet",
            "priority": 1,
            "statement": {"managedRuleGroupStatement": {
                "vendorName": "AWS", "name": "AWSManagedRulesCommonRuleSet",
            }},
            "overrideAction": {"none": {}},
            "visibilityConfig": {
                "sampledRequestsEnabled": True, "cloudWatchMetricsEnabled": True,
                "metricName": "common-rules",
            },
        },
        {
            "name": "RateLimitPerIp",
            "priority": 2,
            "statement": {"rateBasedStatement": {
                "limit": 2000, "aggregateKeyType": "IP",
            }},
            "action": {"block": {}},
            "visibilityConfig": {
                "sampledRequestsEnabled": True, "cloudWatchMetricsEnabled": True,
                "metricName": "rate-limit",
            },
        },
    ],
)

# Stage 2: associate WAF with HTTP API stage
waf.CfnWebACLAssociation(self, "WafAssoc",
    resource_arn=self.api.arn_for_execute_api(),
    web_acl_arn=waf_acl.attr_arn,
)
```

Lambda authorizer (when JWT is insufficient — e.g., per-resource ACL):

```python
lambda_auth = apigwv2_auth.HttpLambdaAuthorizer(
    "CustomAuth",
    handler=auth_fn,
    response_types=[apigwv2_auth.HttpLambdaResponseType.SIMPLE],
    results_cache_ttl=Duration.minutes(5),
    identity_source=["$request.header.Authorization"],
)
```

---

## 5. Common gotchas

- **HTTP API does not support API keys** — for B2B SaaS with key-based metering, use REST API or wrap with Cognito client_credentials grant + DDB-tracked usage.
- **HTTP API JWT authorizer caches identity for 1 hour** — token revocation latency is up to 1h. For instant revoke, use Lambda authorizer with DDB session check.
- **`HttpUserPoolAuthorizer` requires `audience` to match user pool client ID.** Mismatch → 401 with no useful error.
- **CORS `allow_credentials: true` requires explicit `allow_origins` (not `*`).** Browser silently rejects credential-mode XHR otherwise.
- **`disable_execute_api_endpoint: false` (default)** keeps the auto-generated `*.execute-api.region.amazonaws.com` URL active — bypasses WAF + custom domain. Set to `true` in production.
- **Stage-level access logs require explicit log group + format**. Without it, you get nothing in CW Logs.
- **HTTP API throttling is BURST + RATE per route, not per usage plan.** No quota concept (vs REST API). For per-customer quotas, enforce in Lambda + DDB.
- **Cognito user pool deletion is permanent** — `RemovalPolicy.RETAIN` is mandatory in prod. Lost user pool = lost users.
- **Cognito `advanced_security_mode: ENFORCED` adds $0.05/MAU** but blocks compromised credential attacks. Worth it for prod.
- **`generate_secret: true` on app client + SPA = leaked secret in browser.** Always `false` for browser/mobile clients.
- **JWT in `Authorization: Bearer <token>` header** — HTTP API expects exactly this format. Custom header names = config drift.
- **Cognito hosted UI domain prefix is account-globally-unique.** Use env-prefix to avoid conflicts.

---

## 6. Pytest worked example

```python
# tests/test_api.py
import pytest, requests, jwt

API_URL = "https://api.example.com"


def test_healthz_no_auth():
    r = requests.get(f"{API_URL}/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_protected_route_requires_jwt():
    r = requests.get(f"{API_URL}/orders")
    assert r.status_code == 401


def test_protected_route_with_valid_jwt(cognito_jwt):
    r = requests.get(f"{API_URL}/orders",
                     headers={"Authorization": f"Bearer {cognito_jwt}"})
    assert r.status_code == 200


def test_admin_route_requires_admin_group(cognito_jwt_non_admin):
    r = requests.get(f"{API_URL}/admin/users",
                     headers={"Authorization": f"Bearer {cognito_jwt_non_admin}"})
    assert r.status_code == 403


def test_admin_route_admin_user(cognito_jwt_admin):
    r = requests.get(f"{API_URL}/admin/users",
                     headers={"Authorization": f"Bearer {cognito_jwt_admin}"})
    assert r.status_code == 200


def test_cors_preflight_returns_correct_headers():
    r = requests.options(f"{API_URL}/orders",
                         headers={"Origin": "https://app.example.com",
                                  "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    assert "POST" in r.headers["Access-Control-Allow-Methods"]


def test_rate_limit_kicks_in():
    """Burst > 1000 req/s gets 429."""
    fail = 0
    for _ in range(2000):
        r = requests.get(f"{API_URL}/healthz")
        if r.status_code == 429:
            fail += 1
    assert fail > 0
```

---

## 7. Five non-negotiables

1. **Cognito `advanced_security_mode: ENFORCED`** + MFA optional or required.
2. **`disable_execute_api_endpoint: true`** + custom domain only.
3. **WAF v2 with rate limit + AWSManagedRulesCommonRuleSet** on stage.
4. **Stage-level access logs** to KMS-encrypted CW Log Group; structured JSON; correlation ID + Cognito sub claim.
5. **App client `generate_secret: false`** for SPAs/mobile + SRP auth flow only (no plaintext password flow).

---

## 8. References

- [HTTP API vs REST API comparison](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html)
- [JWT Authorizer for HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html)
- [Cognito User Pools — best practices](https://docs.aws.amazon.com/cognito/latest/developerguide/best-practices-for-user-pools.html)
- [Cognito Advanced Security](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pool-settings-advanced-security.html)
- [WAF v2 + HTTP API (2024 GA)](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-control-access-aws-waf.html)
- [Lambda Powertools APIGatewayHttpResolver](https://docs.powertools.aws.dev/lambda/python/latest/core/event_handler/api_gateway/)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. HTTP API + Cognito JWT + Lambda + custom domain + CORS + WAF v2 + throttling + access logs. Wave 10. |
