# SOP — CloudFront Edge Compute (Functions vs Lambda@Edge · KeyValueStore · viewer/origin events · A/B testing · auth at edge)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · CloudFront Functions (JavaScript, viewer-events only, sub-millisecond) · Lambda@Edge (Node.js / Python, all 4 events, 5-30s timeout) · CloudFront KeyValueStore (KVS, GA Mar 2024) · Function event types: viewer-request, viewer-response, origin-request, origin-response

---

## 1. Purpose

- Codify the **CloudFront Functions vs Lambda@Edge** decision tree — most common confusion point.
- Codify **CloudFront KeyValueStore (KVS)** — small, fast key-value reads from CF Functions for A/B routing, redirect tables, feature flags.
- Codify the **4 event types** + which functions/Lambda can run on each.
- Codify the **canonical edge patterns**: header rewrite, URL rewrite (SPA routing), geo personalization, A/B routing, JWT validation, signed cookies/URL, image resize on demand.
- Codify the **cost economics** — Functions $0.10/1M invocations vs Lambda@Edge $0.60/1M (6× difference).
- This is the **edge-compute specialisation**. Built on `CDN_CLOUDFRONT_FOUNDATION` (distribution + behaviors).

When the SOW signals: "edge logic", "URL rewrite", "header manipulation at edge", "auth at edge", "personalization", "A/B testing at edge", "feature flags via CDN".

---

## 2. Decision tree — Functions vs Lambda@Edge

| Need | CloudFront Functions | Lambda@Edge |
|---|:---:|:---:|
| Header rewrite (X-Forwarded-*, security) | ✅ best | ✅ |
| URL rewrite / SPA path fallback | ✅ best | ✅ |
| Add/remove cookies | ✅ | ✅ |
| Redirect (301/302) | ✅ best | ✅ |
| Validate JWT (signature only, no DB lookup) | ✅ via crypto subtle (limited) | ✅ best |
| Validate JWT + DB/cache lookup | ❌ | ✅ |
| Make HTTP calls to other services | ❌ | ✅ |
| Read large config | ❌ (1KB function size) | ✅ |
| KV lookups (small, e.g., redirects) | ✅ via KeyValueStore (NEW 2024) | ✅ via DDB |
| Image resize / dynamic content | ❌ | ✅ |
| Multi-step logic | ❌ (10ms exec time) | ✅ |
| Origin events (response/request) | ❌ viewer-only | ✅ |
| Cost (1M invocations) | $0.10 | $0.60 |
| Cold start | None (always warm) | 50-300ms first |
| Runtime | JavaScript ES2020 | Node.js / Python |

**Rule of thumb:**
- **CloudFront Functions** for the 80% common case (rewrites, redirects, headers, simple auth).
- **Lambda@Edge** for the 20% (DB lookups, HTTP calls, image processing, complex auth).

```
Event types:

   Viewer Request   ──► Origin Request   ──► [Origin]   ──► Origin Response   ──► Viewer Response
        ▲                       ▲                                ▲                        ▲
        │                       │                                │                        │
   CF Functions           Lambda@Edge                     Lambda@Edge             CF Functions
   Lambda@Edge                                                                     Lambda@Edge

   Viewer events  = client-facing (run on every request, even cached)
   Origin events  = origin-facing (run only on cache miss)

   Cost discipline:
     - Auth check + URL rewrite: viewer-request (run on cached too)
     - Image resize: origin-response (only on miss)
     - Header injection: viewer-response (after CF processes)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single Function for SPA routing + headers | **§3 Monolith** |
| Production — Functions + KVS + Lambda@Edge for image resize + auth | **§5 Production** |

---

## 3. CloudFront Functions — viewer events

### 3.1 CDK + JavaScript

```python
# stacks/cdn_functions_stack.py
from aws_cdk import Stack
from aws_cdk import aws_cloudfront as cf
from constructs import Construct


class CdnFunctionsStack(Stack):
    def __init__(self, scope: Construct, id: str, *, distribution: cf.IDistribution,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. URL rewrite Function (viewer-request) ──────────────────
        # Use case: SPA path fallback ("/about" → "/about/index.html")
        url_rewrite_fn = cf.Function(self, "UrlRewriteFn",
            function_name="url-rewrite-spa",
            runtime=cf.FunctionRuntime.JS_2_0,
            comment="SPA path fallback to index.html",
            code=cf.FunctionCode.from_inline("""
function handler(event) {
    var request = event.request;
    var uri = request.uri;
    
    // If URI ends with /, append index.html
    if (uri.endsWith('/')) {
        request.uri += 'index.html';
    }
    // If URI has no extension (e.g., /about), append /index.html
    else if (!uri.includes('.')) {
        request.uri += '/index.html';
    }
    
    return request;
}
"""),
        )

        # ── 2. Header Manipulation Function (viewer-response) ─────────
        # Use case: add custom security headers to every response
        # NOTE: Response Headers Policy is preferred over a Function for static headers.
        # Use Functions for dynamic header logic (e.g., add header based on cookie).
        header_fn = cf.Function(self, "HeaderFn",
            function_name="dynamic-headers",
            runtime=cf.FunctionRuntime.JS_2_0,
            code=cf.FunctionCode.from_inline("""
function handler(event) {
    var response = event.response;
    var headers = response.headers;
    
    // Add request ID for tracing
    headers['x-request-id'] = {value: event.context.requestId};
    headers['x-edge-location'] = {value: event.context.eventType};
    
    // Add header based on cookie
    var cookies = event.request.cookies;
    if (cookies.user_segment && cookies.user_segment.value === 'beta') {
        headers['x-experiment'] = {value: 'beta-features-on'};
    }
    
    return response;
}
"""),
        )

        # ── 3. Redirect Function (viewer-request) ─────────────────────
        # Use case: marketing campaign redirects without backend
        redirect_fn = cf.Function(self, "RedirectFn",
            function_name="campaign-redirects",
            runtime=cf.FunctionRuntime.JS_2_0,
            code=cf.FunctionCode.from_inline("""
function handler(event) {
    var request = event.request;
    var uri = request.uri;
    
    // Hardcoded redirects (small set)
    var redirects = {
        '/old-blog/post-1': '/blog/2024/post-1',
        '/promo': '/landing/spring-2026',
        '/legacy-pricing': '/pricing',
    };
    
    if (redirects[uri]) {
        return {
            statusCode: 301,
            statusDescription: 'Moved Permanently',
            headers: {
                'location': {value: redirects[uri]},
                'cache-control': {value: 'public, max-age=86400'},
            },
        };
    }
    
    return request;
}
"""),
        )

        # ── 4. JWT validation Function (viewer-request) ──────────────
        # Limited: can verify HMAC-signed JWT or RSA via crypto subtle.
        # For DB-backed session check, use Lambda@Edge instead.
        # ... (sample omitted; verify signature + claims; reject 401 if invalid)
```

### 3.2 Attach Function to distribution behavior

```python
# Attach to default behavior (viewer-request event)
distribution = cf.Distribution(self, "Distribution",
    default_behavior=cf.BehaviorOptions(
        origin=...,
        function_associations=[
            cf.FunctionAssociation(
                function=url_rewrite_fn,
                event_type=cf.FunctionEventType.VIEWER_REQUEST,
            ),
            cf.FunctionAssociation(
                function=header_fn,
                event_type=cf.FunctionEventType.VIEWER_RESPONSE,
            ),
        ],
        # ...
    ),
)
```

---

## 4. CloudFront KeyValueStore (KVS) — fast KV reads from Functions (Mar 2024)

KVS gives Functions read-only access to a small key-value store (5 MB max, sub-ms reads). Perfect for: redirect tables, feature flags, A/B routing rules, country code → region mapping.

### 4.1 CDK + Function with KVS

```python
from aws_cdk import aws_cloudfront as cf

# Create KeyValueStore
kvs = cf.KeyValueStore(self, "RedirectsKvs",
    key_value_store_name=f"{env_name}-redirects",
    comment="Redirect table for marketing URLs",
    source=cf.ImportSource.from_inline(json.dumps({
        "data": [
            {"key": "/old-blog/post-1", "value": "/blog/2024/post-1"},
            {"key": "/promo", "value": "/landing/spring-2026"},
            {"key": "/legacy-pricing", "value": "/pricing"},
            # ... can be 1000s of entries (5 MB total)
        ],
    })),
)

# Function with KVS access
redirect_kvs_fn = cf.Function(self, "RedirectKvsFn",
    function_name="redirects-kvs",
    runtime=cf.FunctionRuntime.JS_2_0,
    key_value_store=kvs,                                       # KEY
    code=cf.FunctionCode.from_inline("""
import cf from 'cloudfront';
const kvsHandle = cf.kvs();

async function handler(event) {
    const request = event.request;
    const uri = request.uri;
    
    try {
        const target = await kvsHandle.get(uri);
        if (target) {
            return {
                statusCode: 301,
                statusDescription: 'Moved Permanently',
                headers: {
                    'location': {value: target},
                    'cache-control': {value: 'public, max-age=86400'},
                },
            };
        }
    } catch (err) {
        // Key not in store; pass through
    }
    
    return request;
}
"""),
)
```

### 4.2 Update KVS contents (without redeploying Function)

```bash
# Update via SDK / CLI — no Function redeploy needed
aws cloudfront-keyvaluestore put-key \
  --kvs-arn $KVS_ARN \
  --if-match $ETAG \
  --key "/new-promo" \
  --value "/landing/summer-2026"

# List
aws cloudfront-keyvaluestore list-keys --kvs-arn $KVS_ARN

# Bulk update
aws cloudfront-keyvaluestore update-keys --kvs-arn $KVS_ARN \
  --if-match $ETAG \
  --puts file://updates.json \
  --deletes file://deletes.json
```

---

## 5. Lambda@Edge — for richer logic (DB lookup, HTTP calls, image processing)

### 5.1 CDK — Lambda@Edge

```python
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_iam as iam

# Lambda@Edge MUST be in us-east-1
# (CDK app target region must be us-east-1 OR use CrossRegionLambda construct)

# Image resize Lambda — runs on origin-response (only on cache miss)
image_resize_fn = _lambda.Function(self, "ImageResizeFn",
    function_name="image-resize-edge",
    runtime=_lambda.Runtime.NODEJS_20_X,
    handler="index.handler",
    code=_lambda.Code.from_asset("src/edge/image-resize"),
    role=image_resize_role,
    timeout=Duration.seconds(30),                              # Lambda@Edge max 30s
    memory_size=256,                                            # Lambda@Edge max 10240 MB
)

# Make Lambda eligible for Lambda@Edge
image_resize_version = image_resize_fn.current_version

# Attach to distribution behavior
distribution = cf.Distribution(self, "Distribution",
    additional_behaviors={
        "/images/*": cf.BehaviorOptions(
            origin=...,
            edge_lambdas=[
                cf.EdgeLambda(
                    function_version=image_resize_version,
                    event_type=cf.LambdaEdgeEventType.ORIGIN_RESPONSE,
                    include_body=False,                          # bodies > 1MB: false
                ),
            ],
            cache_policy=...,
        ),
    },
)
```

### 5.2 Sample Lambda@Edge — JWT auth with cache

```javascript
// src/edge/jwt-auth/index.js
'use strict';
const jwt = require('jsonwebtoken');

// Lambda@Edge has 30s timeout + 128 MB-10240 MB memory
exports.handler = async (event) => {
    const request = event.Records[0].cf.request;
    const headers = request.headers;
    
    // Skip auth for static paths
    if (request.uri.startsWith('/static/') || request.uri === '/healthz') {
        return request;
    }
    
    // Extract Authorization header
    const auth = headers.authorization && headers.authorization[0];
    if (!auth || !auth.value.startsWith('Bearer ')) {
        return {
            status: '401',
            statusDescription: 'Unauthorized',
            body: JSON.stringify({error: 'Missing token'}),
            headers: {'content-type': [{key: 'Content-Type', value: 'application/json'}]},
        };
    }
    
    const token = auth.value.substring(7);
    
    try {
        // Verify JWT signature + expiry
        const decoded = jwt.verify(token, process.env.JWT_SECRET);
        
        // Add user info as request header (for origin)
        request.headers['x-user-id'] = [{key: 'X-User-Id', value: decoded.sub}];
        request.headers['x-user-tier'] = [{key: 'X-User-Tier', value: decoded.tier}];
        
        return request;
    } catch (err) {
        return {
            status: '401',
            statusDescription: 'Unauthorized',
            body: JSON.stringify({error: 'Invalid token'}),
        };
    }
};
```

### 5.3 Sample Lambda@Edge — origin-request rewrite (per-region origin)

```javascript
// src/edge/region-router/index.js
exports.handler = async (event) => {
    const request = event.Records[0].cf.request;
    
    // Pick origin based on viewer country (CF-provided)
    const country = request.headers['cloudfront-viewer-country'] &&
                    request.headers['cloudfront-viewer-country'][0].value;
    
    const originMap = {
        'US': 'api-us-east.example.com',
        'CA': 'api-us-east.example.com',
        'GB': 'api-eu-west.example.com',
        'DE': 'api-eu-west.example.com',
        'JP': 'api-ap-northeast.example.com',
    };
    
    const origin = originMap[country] || 'api-us-east.example.com';
    
    request.origin = {
        custom: {
            domainName: origin,
            port: 443,
            protocol: 'https',
            path: '',
            sslProtocols: ['TLSv1.2'],
            readTimeout: 30,
            keepaliveTimeout: 5,
            customHeaders: {},
        },
    };
    request.headers['host'] = [{key: 'Host', value: origin}];
    
    return request;
};
```

---

## 6. Common edge patterns

### 6.1 A/B testing via cookie + KVS

```javascript
// Function: route 10% of users to variant B
async function handler(event) {
    const request = event.request;
    const cookies = request.cookies;
    
    // Check sticky cookie first
    if (cookies.variant) {
        return request;       // already assigned
    }
    
    // Random assignment + sticky cookie
    const variant = Math.random() < 0.1 ? 'B' : 'A';
    
    // For variant B, route to different origin path
    if (variant === 'B') {
        request.uri = '/v2' + request.uri;
    }
    
    // Set sticky cookie via response (next request honored)
    // (Function on viewer-response would set the cookie)
    
    return request;
}
```

### 6.2 Geo personalization

```javascript
// Function: redirect based on country
function handler(event) {
    const request = event.request;
    const country = request.headers['cloudfront-viewer-country'] &&
                    request.headers['cloudfront-viewer-country'][0].value;
    
    if (country === 'GB' && !request.uri.startsWith('/uk/')) {
        return {
            statusCode: 302,
            headers: {'location': {value: '/uk' + request.uri}},
        };
    }
    
    return request;
}
```

### 6.3 Signed URL / signed cookie (Lambda@Edge)

For protected content, use Lambda@Edge to sign URLs at origin-request time. Standard CloudFront signed URLs/cookies pattern.

### 6.4 Image resize on demand (Lambda@Edge origin-response)

```javascript
// Lambda@Edge: resize images per query string ?w=300&h=200
const sharp = require('sharp');

exports.handler = async (event) => {
    const request = event.Records[0].cf.request;
    const response = event.Records[0].cf.response;
    
    // Only resize on miss + image content
    if (!response.headers['content-type'] ||
        !response.headers['content-type'][0].value.startsWith('image/')) {
        return response;
    }
    
    const params = new URLSearchParams(request.querystring);
    const width = parseInt(params.get('w')) || 0;
    const height = parseInt(params.get('h')) || 0;
    
    if (width || height) {
        const buffer = Buffer.from(response.body, 'base64');
        const resized = await sharp(buffer).resize(width, height).toBuffer();
        response.body = resized.toString('base64');
        response.bodyEncoding = 'base64';
        response.headers['content-length'] = [{key: 'Content-Length', value: resized.length.toString()}];
    }
    
    return response;
};
```

---

## 7. Common gotchas

- **CloudFront Functions hard limits**:
  - Code size: 10 KB
  - Memory: 2 MB
  - Execution time: 10 ms (1 ms typical)
  - Single file (no imports of arbitrary npm packages)
  - JavaScript ES2020 only (no async/await pre-runtime 2.0; runtime 2.0 supports it)
- **Lambda@Edge limits**:
  - Memory: 128-10240 MB
  - Execution time: 30s (origin events) / 5s (viewer events)
  - Body inclusion: ≤ 1 MB; > 1 MB needs `include_body: false`
  - Region: us-east-1 only for Lambda; deployed to all edge locations.
- **Lambda@Edge deployment delay** = 5-15 min (replicates to all edges). Be patient.
- **Lambda@Edge versioning** — must use specific version, not `$LATEST`. CDK `current_version` does this.
- **Lambda@Edge vs Functions cost**:
  - Functions: $0.10/1M invocations.
  - Lambda@Edge: $0.60/1M + GB-second compute ($0.00005001 per GB-second).
  - Functions = 6× cheaper for same simple work. Default to Functions; escalate to Lambda@Edge only when needed.
- **KeyValueStore limits**: 5 MB total per store; 1024 bytes per value; 16 stores per account; eventual consistency 60s.
- **KVS update is eventually consistent** — propagation across edges takes up to 60s. Don't use for time-sensitive auth tokens.
- **Function on cached responses** — viewer-request runs on EVERY request (cache hit + miss). For expensive work, prefer origin events (only run on miss).
- **Function execution per request** — even simple Functions add ~1 ms p99. For latency-critical paths, consider whether the logic is needed.
- **Headers manipulation** — header names lowercase in event object; header values are array (`headers['x-foo'] = [{value: 'bar'}]`).
- **Request body access in Lambda@Edge** requires `include_body: true` — body in base64. Origin events for body manipulation.
- **CSP headers via Function** — preferred over Response Headers Policy when CSP includes nonces or per-request values.
- **Function size limits (10 KB)** — minify aggressively; use KVS for any data > 1 KB.
- **Invalidation cost** — if you invalidate to deploy new Function, $0.005/path.
- **Function logs** to CW Logs (us-east-1) — sample 5-10% in prod for cost.
- **Lambda@Edge + KMS + Secrets** — Lambda@Edge can't access env vars at runtime; use Secrets Manager via SDK or hardcode (BAD) or Pass via headers (also bad). Best: use KVS for static config; Secrets Manager via Lambda@Edge with `aws-sdk` if must.

---

## 8. Pytest worked example

```python
# tests/test_edge_compute.py
import boto3, pytest, requests

cf = boto3.client("cloudfront")


def test_function_attached_to_distribution(distribution_id):
    d = cf.get_distribution(Id=distribution_id)["Distribution"]
    config = d["DistributionConfig"]
    default_behavior = config["DefaultCacheBehavior"]
    fns = default_behavior.get("FunctionAssociations", {}).get("Items", [])
    assert fns, "No Functions attached"
    fn_arns = [f["FunctionARN"] for f in fns]
    # Verify expected Function ARN present


def test_url_rewrite_works(domain_name):
    """SPA path /about → returns index.html (200, not 404)."""
    r = requests.get(f"https://{domain_name}/about")
    assert r.status_code == 200
    assert "<!doctype html" in r.text.lower()


def test_redirect_function_works(domain_name):
    r = requests.get(f"https://{domain_name}/old-blog/post-1", allow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"] == "/blog/2024/post-1"


def test_kvs_lookup_works(domain_name):
    """Newly added KVS entry should resolve within 60s."""
    # Add via SDK
    kvs_client = boto3.client("cloudfront-keyvaluestore")
    kvs_client.put_key(KvsARN=kvs_arn, IfMatch=etag,
                        Key="/test-redirect", Value="/test-target")
    
    # Wait for eventual consistency
    import time
    time.sleep(75)
    
    r = requests.get(f"https://{domain_name}/test-redirect", allow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"] == "/test-target"


def test_geo_routing_lambda_edge(domain_name):
    """UK viewer redirected to /uk/ prefix."""
    # Test with Cloudflare-style header (CF-Edge sets it)
    # In real test, use VPN or origin-source IP for GB country
    pass


def test_function_logs_present(function_name):
    """Function should be writing logs."""
    logs = boto3.client("logs", region_name="us-east-1")
    log_groups = logs.describe_log_groups(
        logGroupNamePrefix=f"/aws/cloudfront/function/{function_name}",
    )["logGroups"]
    assert log_groups, f"No log group for Function {function_name}"
```

---

## 9. Five non-negotiables

1. **Default to CloudFront Functions** for the 80% case (rewrites, redirects, headers, simple auth) — 6× cheaper.
2. **Lambda@Edge only when necessary** — DB lookup, HTTP call, image processing, body manipulation.
3. **KeyValueStore over hardcoded tables** — for any redirect/feature-flag set > 10 entries.
4. **Function + Lambda@Edge logs** sampled at 5-10% in prod — full sampling for debugging only.
5. **Function size < 5 KB** — leaves headroom; minify + use KVS for data.

---

## 10. References

- [CloudFront Functions](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-functions.html)
- [Lambda@Edge](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/lambda-at-the-edge.html)
- [KeyValueStore (Mar 2024 GA)](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/kvs-with-functions.html)
- [Functions vs Lambda@Edge comparison](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/edge-functions.html)
- [Function event types](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/lambda-event-structure.html)
- [Image resize at edge sample](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/lambda-examples.html)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. CloudFront Functions vs Lambda@Edge decision tree + KeyValueStore (2024) + canonical patterns (rewrite, redirect, A/B, geo, image resize, JWT auth) + cost economics. Wave 17. |
