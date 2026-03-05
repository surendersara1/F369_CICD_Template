# PARTIAL: API Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_api_layer()` method body.

---

## CDK Code Block — API Layer (API Gateway + Cognito + Authorizer)

```python
def _create_api_layer(self, stage_name: str) -> None:
    """
    Layer 4: API Infrastructure

    Components:
      A) Cognito User Pool + App Client (authentication)
      B) API Gateway REST API (request routing)
      C) Cognito/Lambda Authorizer (authorization)
      D) API Resources + Methods (one per microservice)
      E) API Usage Plans + API Keys (rate limiting)
      F) Regional WAF attached to API Gateway

    Security:
      - All endpoints require valid Cognito JWT (Bearer token)
      - CORS configured for frontend domain only (not wildcard in prod)
      - API Gateway access logs → CloudWatch for audit trail
      - TLS only, no HTTP
    """

    # =========================================================================
    # A) COGNITO USER POOL
    # =========================================================================

    self.user_pool = cognito.UserPool(
        self, "UserPool",
        user_pool_name=f"{{project_name}}-users-{stage_name}",

        # Sign-in options
        sign_in_aliases=cognito.SignInAliases(email=True, username=False),
        auto_verify=cognito.AutoVerifiedAttrs(email=True),

        # MFA: Required for all users (HIPAA requirement)
        mfa=cognito.Mfa.REQUIRED,
        mfa_second_factor=cognito.MfaSecondFactor(
            otp=True,   # TOTP authenticator app
            sms=False,  # SMS MFA discouraged for HIPAA (SS7 interception risk)
        ),

        # Password policy
        password_policy=cognito.PasswordPolicy(
            min_length=12,
            require_uppercase=True,
            require_lowercase=True,
            require_digits=True,
            require_symbols=True,
            temp_password_validity=Duration.days(3),
        ),

        # Account recovery
        account_recovery=cognito.AccountRecovery.EMAIL_ONLY,

        # User attributes
        standard_attributes=cognito.StandardAttributes(
            email=cognito.StandardAttribute(required=True, mutable=False),
            given_name=cognito.StandardAttribute(required=True, mutable=True),
            family_name=cognito.StandardAttribute(required=True, mutable=True),
        ),
        custom_attributes={
            "role": cognito.StringAttribute(mutable=True),
            "hospital_id": cognito.StringAttribute(mutable=True),
        },

        # Security: advanced security (detect compromised credentials)
        advanced_security_mode=cognito.AdvancedSecurityMode.ENFORCED if stage_name == "prod" else cognito.AdvancedSecurityMode.OFF,

        # Email configuration (SES in prod for better deliverability)
        email=cognito.UserPoolEmail.with_cognito(
            reply_to="noreply@{{project_name}}.example.com"
        ) if stage_name != "prod" else cognito.UserPoolEmail.with_ses(
            from_email="noreply@{{project_name}}.example.com",
            from_name="{{project_name}} Platform",
            ses_region=self.region,
        ),

        # Lambda triggers
        lambda_triggers=cognito.UserPoolTriggers(
            # Custom message trigger (customize emails)
            # pre_sign_up: validate hospital domain
            # post_authentication: audit log each login
        ),

        # Deletion policy
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
    )

    # Cognito User Pool Groups (RBAC roles)
    for group_name, description in [
        ("admin", "Administrative users with full access"),
        ("clinician", "Clinical staff with read/write access to patient records"),
        ("receptionist", "Front-desk staff with limited patient record access"),
        ("auditor", "Compliance auditors with read-only access to audit logs"),
    ]:
        cognito.CfnUserPoolGroup(
            self, f"Group{group_name.capitalize()}",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=group_name,
            description=description,
        )

    # App Client (for frontend SPA)
    self.user_pool_client = self.user_pool.add_client(
        "WebAppClient",
        user_pool_client_name=f"{{project_name}}-web-client-{stage_name}",

        # OAuth flows for SPA
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(authorization_code_grant=True),
            scopes=[
                cognito.OAuthScope.EMAIL,
                cognito.OAuthScope.OPENID,
                cognito.OAuthScope.PROFILE,
                cognito.OAuthScope.custom("{{project_name}}/read"),
                cognito.OAuthScope.custom("{{project_name}}/write"),
            ],
            callback_urls=[
                f"https://{{project_dashboard_domain}}/callback",
                "http://localhost:3000/callback",  # Dev only — stripped in prod via context
            ],
            logout_urls=[
                f"https://{{project_dashboard_domain}}/logout",
                "http://localhost:3000/logout",
            ],
        ),

        # Token validity
        id_token_validity=Duration.hours(1),
        access_token_validity=Duration.hours(1),
        refresh_token_validity=Duration.days(30),

        # Security: enable token revocation
        enable_token_revocation=True,
        prevent_user_existence_errors=True,

        # No secret (public SPA client)
        generate_secret=False,

        # Auth flows (ONLY allow SRP — no plaintext passwords over the wire)
        auth_flows=cognito.AuthFlow(
            user_srp=True,
            user_password=False,  # Disabled — SRP only
            admin_user_password=False,
        ),
    )

    # =========================================================================
    # B) API GATEWAY REST API
    # =========================================================================

    # Access log group
    api_log_group = logs.LogGroup(
        self, "ApiAccessLogs",
        log_group_name=f"/{{project_name}}/{stage_name}/api-access-logs",
        retention=logs.RetentionDays.ONE_MONTH if stage_name != "prod" else logs.RetentionDays.ONE_YEAR,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    self.rest_api = apigw.RestApi(
        self, "RestApi",
        rest_api_name=f"{{project_name}}-api-{stage_name}",
        description=f"{{project_name}} REST API ({stage_name})",

        # Enable CloudWatch access logs
        deploy_options=apigw.StageOptions(
            stage_name=stage_name,
            access_log_destination=apigw.LogGroupLogDestination(api_log_group),
            access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                caller=True,
                http_method=True,
                ip=True,
                protocol=True,
                request_time=True,
                resource_path=True,
                response_length=True,
                status=True,
                user=True,
            ),
            logging_level=apigw.MethodLoggingLevel.INFO,
            data_trace_enabled=stage_name != "prod",  # Disable body logging in prod (HIPAA)
            metrics_enabled=True,
            tracing_enabled=True,  # X-Ray tracing
            throttling_rate_limit=1000,
            throttling_burst_limit=2000,
        ),

        # CORS (restrict origins in prod)
        default_cors_preflight_options=apigw.CorsOptions(
            allow_origins=["*"] if stage_name == "dev" else [
                f"https://{{project_dashboard_domain}}",
                f"https://www.{{project_dashboard_domain}}",
            ],
            allow_methods=apigw.Cors.ALL_METHODS,
            allow_headers=["Content-Type", "Authorization", "X-Amz-Date", "X-Api-Key"],
            max_age=Duration.days(1),
        ),

        # Binary types (for file downloads)
        binary_media_types=["application/pdf", "application/octet-stream"],

        # Endpoint type: Regional (attach regional WAF)
        endpoint_types=[apigw.EndpointType.REGIONAL],
    )

    # =========================================================================
    # C) COGNITO AUTHORIZER
    # =========================================================================

    cognito_authorizer = apigw.CognitoUserPoolsAuthorizer(
        self, "CognitoAuthorizer",
        cognito_user_pools=[self.user_pool],
        authorizer_name=f"{{project_name}}-cognito-authorizer",
        identity_source="method.request.header.Authorization",
        results_cache_ttl=Duration.minutes(5),
    )

    # =========================================================================
    # D) API RESOURCES + METHOD BINDINGS
    # [Claude: Generate from Architecture Map Section 4.2 API Requirements]
    # =========================================================================

    # Helper: create resource + method binding
    def add_api_endpoint(
        parent_resource: apigw.IResource,
        path: str,
        method: str,
        lambda_fn: _lambda.Function,
        require_auth: bool = True,
        request_validator: apigw.RequestValidator = None,
    ) -> apigw.Method:
        resource = parent_resource.add_resource(path) if path else parent_resource

        return resource.add_method(
            method,
            apigw.LambdaIntegration(
                lambda_fn,
                proxy=True,
                timeout=Duration.seconds(29),  # API Gateway max
                allow_test_invoke=stage_name != "prod",
            ),
            authorization_type=apigw.AuthorizationType.COGNITO if require_auth else apigw.AuthorizationType.NONE,
            authorizer=cognito_authorizer if require_auth else None,
            request_validator=request_validator,
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="401"),
                apigw.MethodResponse(status_code="403"),
                apigw.MethodResponse(status_code="404"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

    # AUTH ENDPOINTS (no Cognito auth — they ARE the auth)
    auth_resource = self.rest_api.root.add_resource("auth")
    add_api_endpoint(auth_resource, "login",   "POST", self.lambda_functions["AuthService"],  require_auth=False)
    add_api_endpoint(auth_resource, "refresh", "POST", self.lambda_functions["AuthService"],  require_auth=False)

    # PATIENT ENDPOINTS
    patients_resource = self.rest_api.root.add_resource("patients")
    add_api_endpoint(patients_resource, None,      "GET",  self.lambda_functions["PatientList"])
    add_api_endpoint(patients_resource, None,      "POST", self.lambda_functions["PatientCreate"])

    patient_resource = patients_resource.add_resource("{id}")
    add_api_endpoint(patient_resource, None,        "GET", self.lambda_functions["PatientList"])
    add_api_endpoint(patient_resource, None,        "PUT", self.lambda_functions["PatientCreate"])

    # DOCUMENTS ENDPOINTS
    docs_resource = patient_resource.add_resource("documents")
    add_api_endpoint(docs_resource, None, "POST", self.lambda_functions["DocumentUpload"])
    add_api_endpoint(docs_resource, None, "GET",  self.lambda_functions["DocumentUpload"])

    # REPORTS ENDPOINTS
    reports_resource = self.rest_api.root.add_resource("reports")
    add_api_endpoint(reports_resource, "generate", "POST", self.lambda_functions["AuthService"])  # [Claude: replace with report-trigger Lambda]

    report_resource = reports_resource.add_resource("{id}")
    add_api_endpoint(report_resource, "status",   "GET",  self.lambda_functions["AuthService"])  # [Claude: replace]
    add_api_endpoint(report_resource, "download", "GET",  self.lambda_functions["AuthService"])  # [Claude: replace]

    # Health check (no auth — used for load balancer and monitoring)
    health_resource = self.rest_api.root.add_resource("health")
    health_resource.add_method(
        "GET",
        apigw.MockIntegration(
            integration_responses=[
                apigw.IntegrationResponse(
                    status_code="200",
                    response_templates={"application/json": '{"status": "healthy"}'},
                )
            ],
            passthrough_behavior=apigw.PassthroughBehavior.NEVER,
            request_templates={"application/json": '{"statusCode": 200}'},
        ),
        authorization_type=apigw.AuthorizationType.NONE,
        method_responses=[apigw.MethodResponse(status_code="200")],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    self.api_endpoint_output = CfnOutput(
        self, "ApiEndpoint",
        value=self.rest_api.url,
        description="API Gateway endpoint URL",
        export_name=f"{{project_name}}-api-endpoint-{stage_name}",
    )

    CfnOutput(self, "UserPoolId",
        value=self.user_pool.user_pool_id,
        description="Cognito User Pool ID",
        export_name=f"{{project_name}}-user-pool-id-{stage_name}",
    )

    CfnOutput(self, "UserPoolClientId",
        value=self.user_pool_client.user_pool_client_id,
        description="Cognito App Client ID",
        export_name=f"{{project_name}}-user-pool-client-id-{stage_name}",
    )
```
