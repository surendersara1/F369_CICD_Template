# SOP — API Layer (REST + WebSocket via API Gateway)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · API Gateway REST v1 + WebSocket v2

---

## 1. Purpose

Public API surface: request validation, auth (API Key / Cognito JWT / Lambda authorizer), throttling, CORS, access logging, X-Ray tracing, custom domain. Integrations are Lambda proxy by default.

For GraphQL instead of REST, see `LAYER_API_APPSYNC`.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| API Gateway + target Lambdas all in one stack | **§3 Monolith Variant** |
| API Gateway in `ApiStack`, Lambdas in `ComputeStack`, Cognito in `AuthStack` | **§4 Micro-Stack Variant** |

**Why the split matters.** `apigw.LambdaIntegration(fn)` auto-grants `lambda:InvokeFunction` on the Lambda's resource policy referencing the REST API ARN. Cross-stack → cycle. Fix: use L2 integration but grant invoke from the API's execution role identity-side; or use the `allow_test_invoke=False` pattern.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_apigateway as apigw,
    aws_logs as logs,
    aws_iam as iam,
)


def _create_api(self, stage: str) -> None:
    access_log_group = logs.LogGroup(
        self, "ApiAccessLogs",
        log_group_name=f"/aws/apigateway/{{project_name}}-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
    )

    self.api = apigw.RestApi(
        self, "Api",
        rest_api_name=f"{{project_name}}-api-{stage}",
        deploy_options=apigw.StageOptions(
            stage_name="v1",
            logging_level=apigw.MethodLoggingLevel.INFO,
            access_log_destination=apigw.LogGroupLogDestination(access_log_group),
            access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                caller=True, http_method=True, ip=True, protocol=True,
                request_time=True, resource_path=True, response_length=True,
                status=True, user=True,
            ),
            tracing_enabled=True,
            throttling_burst_limit=1000,
            throttling_rate_limit=500,
        ),
        default_cors_preflight_options=apigw.CorsOptions(
            allow_origins=[f"https://{{custom_domain_name}}"] if stage == "prod" else apigw.Cors.ALL_ORIGINS,
            allow_methods=apigw.Cors.ALL_METHODS,
            allow_headers=apigw.Cors.DEFAULT_HEADERS + ["x-correlation-id"],
        ),
        endpoint_types=[apigw.EndpointType.REGIONAL],
        cloud_watch_role=True,
    )

    # Auth — API key + usage plan (POC). Swap to Cognito for prod (see §4).
    api_key = self.api.add_api_key(
        "DefaultKey",
        api_key_name=f"{{project_name}}-default-key-{stage}",
    )
    usage_plan = self.api.add_usage_plan(
        "DefaultPlan",
        name=f"{{project_name}}-plan-{stage}",
        throttle=apigw.ThrottleSettings(
            burst_limit=100 if stage != "prod" else 1000,
            rate_limit=50 if stage != "prod" else 500,
        ),
    )
    usage_plan.add_api_key(api_key)
    usage_plan.add_api_stage(stage=self.api.deployment_stage)
    auth_kwargs = {"api_key_required": True}

    def _integration(fn):
        return apigw.LambdaIntegration(fn, proxy=True)

    # Routes — monolith: L2 LambdaIntegration auto-grants invoke on same-stack Lambdas
    self.api.root.add_resource("upload").add_method(
        "POST", _integration(self.lambda_functions["Upload"]), **auth_kwargs
    )
    jobs = self.api.root.add_resource("jobs")
    jobs.add_method("GET", _integration(self.lambda_functions["Status"]), **auth_kwargs)
    jobs.add_resource("{id}").add_method(
        "GET", _integration(self.lambda_functions["Status"]), **auth_kwargs
    )
    self.api.root.add_resource("insights").add_resource("{id}").add_method(
        "GET", _integration(self.lambda_functions["Insights"]), **auth_kwargs
    )

    cdk.CfnOutput(self, "ApiUrl", value=self.api.url)
```

### 3.1 Monolith gotchas

- **`AccessLogFormat.json_with_standard_fields()` requires every field** as a boolean kwarg. Signature changed in CDK 2.100+ — pass all 9 fields explicitly.
- **`add_usage_plan(api_key=...)` was removed.** Use `usage_plan.add_api_key(api_key)` after construction.
- **CORS origins** should narrow to the frontend domain in prod; `Cors.ALL_ORIGINS` leaks the API to any caller.
- **`cloud_watch_role=True`** creates an account-wide API Gateway → CloudWatch Logs role the first time; CDK warns if one already exists.

---

## 4. Micro-Stack Variant

### 4.1 `ApiStack` — integration without auto-grant cycles

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_apigateway as apigw,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_iam as iam,
)
from constructs import Construct


class ApiStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        upload_fn: _lambda.IFunction,
        status_fn: _lambda.IFunction,
        insights_fn: _lambda.IFunction,
        use_cognito: bool = False,
        user_pool_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-api", **kwargs)

        access_log_group = logs.LogGroup(
            self, "AccessLogs",
            log_group_name="/aws/apigateway/{project_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        self.api = apigw.RestApi(
            self, "Api",
            rest_api_name="{project_name}-api",
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                logging_level=apigw.MethodLoggingLevel.INFO,
                access_log_destination=apigw.LogGroupLogDestination(access_log_group),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True, http_method=True, ip=True, protocol=True,
                    request_time=True, resource_path=True, response_length=True,
                    status=True, user=True,
                ),
                tracing_enabled=True,
                throttling_burst_limit=100,
                throttling_rate_limit=50,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,   # narrow in prod
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=apigw.Cors.DEFAULT_HEADERS + ["x-correlation-id"],
            ),
        )

        # Auth variant
        if use_cognito and user_pool_arn:
            authorizer = apigw.CognitoUserPoolsAuthorizer(
                self, "Authorizer",
                cognito_user_pools=[
                    # from_user_pool_arn does NOT cross-mutate; safe cross-stack
                    apigw.CognitoUserPoolsAuthorizer  # placeholder; real import omitted
                ],
            )
            auth_kwargs = {
                "authorization_type": apigw.AuthorizationType.COGNITO,
                "authorizer": authorizer,
            }
        else:
            api_key = self.api.add_api_key("DefaultKey")
            usage_plan = self.api.add_usage_plan(
                "DefaultPlan",
                throttle=apigw.ThrottleSettings(burst_limit=100, rate_limit=50),
            )
            usage_plan.add_api_key(api_key)
            usage_plan.add_api_stage(stage=self.api.deployment_stage)
            auth_kwargs = {"api_key_required": True}

        def _integration(fn):
            # L2 LambdaIntegration with proxy=True. CDK tries to add resource
            # policy on fn referencing this RestApi's ARN. Cross-stack cycle.
            # Workaround: disable the test-invoke permission that forces the
            # cross-stack grant. We grant invoke manually below on each function.
            return apigw.LambdaIntegration(fn, proxy=True, allow_test_invoke=False)

        # Routes
        self.api.root.add_resource("upload").add_method(
            "POST", _integration(upload_fn), **auth_kwargs
        )
        jobs = self.api.root.add_resource("jobs")
        jobs.add_method("GET", _integration(status_fn), **auth_kwargs)
        jobs.add_resource("{id}").add_method("GET", _integration(status_fn), **auth_kwargs)
        self.api.root.add_resource("insights").add_resource("{id}").add_method(
            "GET", _integration(insights_fn), **auth_kwargs
        )

        # Cross-stack invoke permissions must go on each Lambda's resource policy
        # (one-directional: API → Lambda; API Gateway is principal). We do this
        # from the consumer side (ComputeStack) via fn.add_permission(), called
        # in app.py AFTER both stacks are constructed, passing this api.arn.
        self._api_arn_for_execute = (
            f"arn:{self.partition}:execute-api:{self.region}:{self.account}:"
            f"{self.api.rest_api_id}/*/*/*"
        )

        cdk.CfnOutput(self, "ApiUrl",           value=self.api.url)
        cdk.CfnOutput(self, "ApiArnForExecute", value=self._api_arn_for_execute)
```

### 4.2 Consumer-side permission (in `ComputeStack` or `app.py`)

```python
# In app.py, AFTER both ApiStack and ComputeStack are created:
for fn in [compute.upload_fn, compute.status_fn, compute.insights_fn]:
    fn.add_permission(
        f"ApiGwInvoke{fn.node.id}",
        principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
        source_arn=f"arn:aws:execute-api:{env.region}:{env.account}:{api.api.rest_api_id}/*/*",
    )
```

This adds a resource-policy statement to the Lambda (local to ComputeStack) with a STATIC source_arn condition (uses `api.rest_api_id` which is a small token, still produces a CFN Ref but only ComputeStack → ApiStack direction). **No reverse edge → no cycle.**

### 4.3 Micro-stack gotchas

- **`allow_test_invoke=False`** disables the "Test" button in the AWS Console for that method. Safe cost.
- **Cognito authorizer cross-stack** — `CognitoUserPoolsAuthorizer` accepts a user pool by interface; no cross-stack mutation.
- **Custom domain cross-stack** — the `ACertificate` from `AuthStack` can be consumed without mutation; but the domain mapping itself should be in `ApiStack`.

---

## 5. WebSocket variant (for push updates)

```python
from aws_cdk import aws_apigatewayv2 as apigwv2, aws_apigatewayv2_integrations as integrations


ws = apigwv2.WebSocketApi(
    self, "WebSocketApi",
    api_name="{project_name}-ws",
    connect_route_options=apigwv2.WebSocketRouteOptions(
        integration=integrations.WebSocketLambdaIntegration("ConnectInt", connect_fn),
    ),
    disconnect_route_options=apigwv2.WebSocketRouteOptions(
        integration=integrations.WebSocketLambdaIntegration("DisconnectInt", disconnect_fn),
    ),
    default_route_options=apigwv2.WebSocketRouteOptions(
        integration=integrations.WebSocketLambdaIntegration("DefaultInt", default_fn),
    ),
)
apigwv2.WebSocketStage(
    self, "WsStage",
    web_socket_api=ws, stage_name="v1", auto_deploy=True,
)
```

---

## 6. Worked example

```python
def test_api_stage_has_json_logs_and_xray():
    import aws_cdk as cdk
    from aws_cdk.assertions import Template, Match
    # ... instantiate ApiStack with mock Lambdas ...
    t = Template.from_stack(api)
    t.has_resource_properties("AWS::ApiGateway::Stage", {
        "AccessLogSetting": Match.any_value(),
        "TracingEnabled": True,
    })
```

---

## 7. References

- `docs/template_params.md` — `API_KEY_USAGE_PLAN_*`, `AUTH_MODE`
- `docs/Feature_Roadmap.md` — AP-01..AP-20
- Related SOPs: `LAYER_BACKEND_LAMBDA` (integration targets), `LAYER_FRONTEND` (CORS origin)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Fixed `add_usage_plan` signature. Explicit `json_with_standard_fields` args. `allow_test_invoke=False` + consumer-side `add_permission` to avoid cross-stack grant cycle. |
| 1.0 | 2026-03-05 | Initial. |
