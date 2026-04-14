# PARTIAL: AgentCore Identity — Authentication & Authorization

**Usage:** Include when SOW mentions AgentCore Identity, OAuth2 for agents, Cognito integration, Entra ID, Okta, agent authentication, or SigV4 signing.

---

## AgentCore Identity Overview

```
AgentCore Identity = Unified auth for agent-to-service communication:
  - Amazon Cognito (user pools, machine-to-machine)
  - Microsoft Entra ID (Azure AD)
  - Okta
  - Google, GitHub OAuth providers
  - IAM roles (SigV4 signing)
  - API keys
  - Token vault for outbound MCP calls

Auth Flow:
  Agent → AgentCore Identity → Token Vault → OAuth2/SigV4 → External Service
```

---

## CDK Code Block — AgentCore Identity (Cognito OAuth2)

```python
def _create_agentcore_identity(self, stage_name: str) -> None:
    """
    AgentCore Identity — OAuth2 authentication for agent-to-service calls.

    Components:
      A) Cognito User Pool (machine-to-machine OAuth2)
      B) Resource Server with scopes (tool access control)
      C) App Client with client_credentials grant
      D) Secrets Manager for client credentials

    [Claude: include when SOW mentions agent auth, Gateway, or MCP tools.
     Use Cognito for AWS-native. Add Entra ID/Okta config in SSM if SOW specifies.]
    """

    # =========================================================================
    # A) COGNITO USER POOL — Machine-to-Machine Auth
    # =========================================================================

    self.agentcore_user_pool = cognito.UserPool(
        self, "AgentCoreUserPool",
        user_pool_name=f"{{project_name}}-agentcore-{stage_name}",
        removal_policy=RemovalPolicy.DESTROY if stage_name != "prod" else RemovalPolicy.RETAIN,
        sign_in_aliases=cognito.SignInAliases(email=True),
        self_sign_up_enabled=False,
    )

    # =========================================================================
    # B) RESOURCE SERVER — OAuth2 Scopes
    # =========================================================================

    resource_server = self.agentcore_user_pool.add_resource_server(
        "AgentCoreResourceServer",
        identifier=f"{{project_name}}-gateway",
        scopes=[
            cognito.ResourceServerScope(
                scope_name="tools.invoke",
                scope_description="Invoke tools via AgentCore Gateway",
            ),
            cognito.ResourceServerScope(
                scope_name="memory.read",
                scope_description="Read from AgentCore Memory",
            ),
            cognito.ResourceServerScope(
                scope_name="memory.write",
                scope_description="Write to AgentCore Memory",
            ),
        ],
    )

    # =========================================================================
    # C) APP CLIENT — Client Credentials Grant
    # =========================================================================

    self.agentcore_app_client = self.agentcore_user_pool.add_client(
        "AgentCoreAppClient",
        user_pool_client_name=f"{{project_name}}-agent-client-{stage_name}",
        generate_secret=True,
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[
                cognito.OAuthScope.resource_server(
                    resource_server,
                    cognito.ResourceServerScope(scope_name="tools.invoke", scope_description="Invoke tools"),
                ),
            ],
        ),
    )

    self.agentcore_user_pool.add_domain(
        "AgentCoreDomain",
        cognito_domain=cognito.CognitoDomainOptions(
            domain_prefix=f"{{project_name}}-ac-{stage_name}",
        ),
    )

    # =========================================================================
    # D) SECRETS MANAGER — Client Credentials
    # =========================================================================

    self.agentcore_client_secret = sm.Secret(
        self, "AgentCoreClientSecret",
        secret_name=f"{{project_name}}/{stage_name}/agentcore-gateway-credentials",
        description="OAuth2 client credentials for AgentCore Gateway",
        encryption_key=self.kms_key,
    )
    self.agentcore_client_secret.grant_read(self.agentcore_runtime_role)

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "AgentCoreUserPoolId",
        value=self.agentcore_user_pool.user_pool_id,
        description="Cognito User Pool ID for AgentCore Identity",
    )
    CfnOutput(self, "AgentCoreClientId",
        value=self.agentcore_app_client.user_pool_client_id,
        description="OAuth2 Client ID for agent authentication",
    )
```

---

## OAuth2 Token Helper — Pass 3 Reference

```python
"""OAuth2 token acquisition with caching for AgentCore Identity."""
import boto3, json, time, requests

_token_cache = {"token": None, "expires_at": 0}

def get_oauth2_token() -> str:
    """Get OAuth2 token with caching for Gateway authentication."""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    sm_client = boto3.client("secretsmanager")
    secret = json.loads(
        sm_client.get_secret_value(SecretId=os.environ["GATEWAY_SECRET_ARN"])["SecretString"]
    )
    resp = requests.post(secret["token_endpoint"], data={
        "grant_type": "client_credentials",
        "client_id": secret["client_id"],
        "client_secret": secret["client_secret"],
        "scope": secret["scope"],
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp.raise_for_status()
    token_data = resp.json()
    _token_cache["token"] = token_data["access_token"]
    _token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _token_cache["token"]
```
