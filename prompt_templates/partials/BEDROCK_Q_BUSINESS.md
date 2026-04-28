# SOP — Amazon Q Business (enterprise GenAI assistant · native data connectors · plugins · custom apps · AppRoles · subscription tiers)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Q Business (GA April 2024) · Q Business Pro / Lite subscription tiers · 40+ native data connectors (S3, SharePoint, Confluence, Salesforce, ServiceNow, Slack, Teams, Box, Drive, Jira, GitHub, GitLab, Zendesk, Workday) · Q plugins (built-in + custom OpenAPI plugins) · Custom Q apps · AppRoles for governance · IAM Identity Center integration · Q Apps SDK

---

## 1. Purpose

- Codify **Amazon Q Business** as the canonical enterprise GenAI assistant. Replaces hand-rolled RAG chatbots with a managed service that handles: connectors, indexing, embeddings, retrieval, citation, plugin orchestration, and ChatOps UX.
- Codify the **40+ native data connectors** + which to use + auth patterns + sync strategies.
- Codify **Q plugins** — built-in (Salesforce, Jira, ServiceNow, etc.) + custom plugins via OpenAPI + Lambda action backends.
- Codify **Custom Q Apps** — declarative Q-powered apps published to your Q Business application (e.g., "Summarize this PR for me", "Create a JIRA ticket from this conversation").
- Codify **AppRoles** for fine-grained access control — group → app/plugin/data-source mappings.
- Codify **IAM Identity Center integration** — single sign-on via Azure AD / Okta / Google Workspace; document-level permissions inherited from source.
- Codify the **subscription tier strategy** — Q Business Lite ($3/user/mo) vs Pro ($20/user/mo).
- This is the **flagship enterprise GenAI specialisation**. Pairs with `BEDROCK_KNOWLEDGE_BASES` (deeper RAG control), `BEDROCK_AGENTS_MULTI_AGENT` (agentic patterns), `LLMOPS_BEDROCK` (foundation model invocation).

When the SOW signals: "ChatGPT for enterprise", "we want one search box across all our SaaS", "AI assistant for employees", "Q Business setup", "knowledge worker productivity".

---

## 2. Decision tree — Q Business vs Bedrock KB vs custom RAG

| Need | Q Business | Bedrock Knowledge Bases + Agents | Custom RAG (Strands/AgentCore) |
|---|:---:|:---:|:---:|
| Out-of-the-box ChatOps UI | ✅ web UI + Slack/Teams | ❌ build your own | ❌ build your own |
| Native SaaS connectors (no code) | ✅ 40+ | ❌ S3 + URL only | ❌ |
| Document-level permissions inheritance | ✅ from IDP | ⚠️ manual | ⚠️ manual |
| Custom prompt / model fine-tuning | ❌ AWS-controlled | ✅ choose model | ✅ |
| Domain-specific agents with tools | ⚠️ via plugins | ✅ Bedrock Agents | ✅ richest |
| Custom UI / embed | ⚠️ Q Apps SDK | ✅ flexible | ✅ flexible |
| Per-message billing | ❌ per-user MAU | ✅ token-based | ✅ token-based |
| Speed to value | ✅ days | weeks | weeks-months |

**Recommendation:**
- **Q Business** for "AI assistant for all employees" — knowledge workers, support agents, sales, HR.
- **Bedrock KB + Agents** for domain-specific apps (customer support bot, medical Q&A, code assistant).
- **Custom RAG** when Q Business's UX doesn't fit OR pricing doesn't (huge MAU + low query volume).

```
Q Business architecture:

   IAM Identity Center (Azure AD / Okta / Google Workspace SSO)
        │  user.email → groups
        ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Q Business Application (per environment)                        │
   │   - Identity center instance                                     │
   │   - Subscription tier (Lite or Pro)                              │
   │   - AppRoles (groups → permissions)                              │
   │                                                                   │
   │ Indexes (one or more per app)                                    │
   │   - STARTER (free tier; 50K docs)                                │
   │   - ENTERPRISE (paid; unlimited; better recall)                  │
   │                                                                   │
   │ Retrievers (data planes for queries)                             │
   │   - NATIVE_INDEX (Q's own index from connectors)                 │
   │   - KENDRA_INDEX (existing Kendra index reused)                  │
   │                                                                   │
   │ Data Sources (40+ connectors)                                    │
   │   - S3 (your docs / wiki exports)                                │
   │   - SharePoint Online (Microsoft Graph API)                       │
   │   - Confluence (Cloud / Server)                                   │
   │   - Salesforce, ServiceNow, Jira                                  │
   │   - Slack, Teams, Workday, Zendesk, Box, Drive                    │
   │   - Custom data source via Bring-Your-Own connector               │
   │                                                                   │
   │ Plugins (action takers)                                          │
   │   - Built-in: Jira, Salesforce, ServiceNow, MS Teams, PagerDuty  │
   │   - Custom: OpenAPI spec → Lambda action backend                  │
   │                                                                   │
   │ Custom Q Apps (no-code app builder)                              │
   │   - Form-based prompt + sources + plugins                          │
   │                                                                   │
   │ Web Experience (auto-provisioned UI)                             │
   │   - https://<app-id>.{region}.qbusiness.aws/                       │
   │   - SSO via Identity Center                                        │
   │   - Custom domain optional                                          │
   └────────────────────────────────────────────────────────────────┘

   Channels (extend Q to where users work):
     - Q in Slack (slash command + DM)
     - Q in Teams (chat integration)
     - Q in browser extension (highlight → ask)
     - Q via SDK (embed in custom apps)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single app + 3 data sources + Lite tier | **§3 Monolith** |
| Production — multi-app + 10+ connectors + Pro tier + plugins + Q Apps | **§5 Production** |

---

## 3. Monolith Variant — Q Business app + 3 data sources

### 3.1 CDK

```python
# stacks/q_business_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_qbusiness as qb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct
import json


class QBusinessStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 idc_instance_arn: str,                      # IAM Identity Center
                 kms_key_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Q Business Application ─────────────────────────────────
        # Top-level container for indexes, retrievers, web UI, plugins, Q apps
        application = qb.CfnApplication(self, "QApp",
            display_name=f"{env_name}-q-business",
            description="Enterprise Q Business assistant",
            identity_center_instance_arn=idc_instance_arn,
            attachments_configuration=qb.CfnApplication.AttachmentsConfigurationProperty(
                attachments_control_mode="ENABLED",          # users can attach files in chat
            ),
            encryption_configuration=qb.CfnApplication.EncryptionConfigurationProperty(
                kms_key_id=kms_key_arn,
            ),
            personalization_configuration=qb.CfnApplication.PersonalizationConfigurationProperty(
                personalization_control_mode="ENABLED",       # user-specific responses
            ),
            q_apps_configuration=qb.CfnApplication.QAppsConfigurationProperty(
                q_apps_control_mode="ENABLED",                # users can create custom Q apps
            ),
            tags=[{"key": "env", "value": env_name}],
        )

        # ── 2. Index — STARTER (POC) or ENTERPRISE (prod) ─────────────
        index = qb.CfnIndex(self, "QIndex",
            application_id=application.attr_application_id,
            display_name=f"{env_name}-index",
            type="STARTER",                                   # or "ENTERPRISE"
            capacity_configuration=qb.CfnIndex.IndexCapacityConfigurationProperty(
                units=1,                                       # 1 unit = 100K docs / 8K queries/hr
            ),
            document_attribute_configurations=[
                # Custom searchable metadata fields
                qb.CfnIndex.DocumentAttributeConfigurationProperty(
                    name="department",
                    type="STRING",
                    search="ENABLED",
                ),
                qb.CfnIndex.DocumentAttributeConfigurationProperty(
                    name="confidentiality",
                    type="STRING",
                    search="DISABLED",                          # filter only, not searchable
                ),
            ],
        )

        # ── 3. Retriever — points the app at the index ────────────────
        retriever = qb.CfnRetriever(self, "QRetriever",
            application_id=application.attr_application_id,
            display_name=f"{env_name}-retriever",
            type="NATIVE_INDEX",
            configuration=qb.CfnRetriever.RetrieverConfigurationProperty(
                native_index_configuration=qb.CfnRetriever.NativeIndexConfigurationProperty(
                    index_id=index.attr_index_id,
                ),
            ),
        )

        # ── 4. Data source role ──────────────────────────────────────
        ds_role = iam.Role(self, "DsRole",
            assumed_by=iam.ServicePrincipal("qbusiness.amazonaws.com"),
            inline_policies={
                "DataSourceAccess": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["qbusiness:BatchPutDocument", "qbusiness:BatchDeleteDocument"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["s3:GetObject", "s3:ListBucket"],
                        resources=[
                            f"arn:aws:s3:::{env_name}-q-source",
                            f"arn:aws:s3:::{env_name}-q-source/*",
                        ],
                    ),
                ]),
            },
        )

        # ── 5. S3 data source ─────────────────────────────────────────
        s3_data_source = qb.CfnDataSource(self, "S3DataSource",
            application_id=application.attr_application_id,
            index_id=index.attr_index_id,
            display_name="Internal docs S3",
            description="Company wiki + policies + onboarding",
            role_arn=ds_role.role_arn,
            sync_schedule="cron(0 2 * * ? *)",                # daily 2am
            configuration={
                "type": "S3",
                "version": "1.0.0",
                "syncMode": "FULL_CRAWL",                       # or FORCED_FULL_CRAWL or CHANGE_LOG
                "connectionConfiguration": {
                    "repositoryEndpointMetadata": {
                        "BucketName": f"{env_name}-q-source",
                    },
                },
                "repositoryConfigurations": {
                    "document": {
                        "fieldMappings": [
                            {"indexFieldName": "_source_uri",
                             "indexFieldType": "STRING",
                             "dataSourceFieldName": "s3_document_id"},
                            {"indexFieldName": "department",
                             "indexFieldType": "STRING",
                             "dataSourceFieldName": "metadata.department"},
                        ],
                    },
                },
                "additionalProperties": {
                    "inclusionPatterns": ["*.pdf", "*.docx", "*.html", "*.md"],
                    "exclusionPatterns": ["*/drafts/*", "*/archive/*"],
                    "maxFileSizeInMegaBytes": "50",
                },
                "documentTitleFieldName": "title",
            },
        )

        # ── 6. SharePoint data source (example) ──────────────────────
        sharepoint_data_source = qb.CfnDataSource(self, "SharepointDS",
            application_id=application.attr_application_id,
            index_id=index.attr_index_id,
            display_name="SharePoint Online",
            role_arn=ds_role.role_arn,
            sync_schedule="cron(0 3 * * ? *)",                # daily 3am, 1h offset from S3
            configuration={
                "type": "SHAREPOINT",
                "version": "1.0.0",
                "syncMode": "CHANGE_LOG",                      # incremental
                "connectionConfiguration": {
                    "repositoryEndpointMetadata": {
                        "tenantId": "<tenant-id>",
                        "siteUrls": ["https://acme.sharepoint.com/sites/engineering"],
                    },
                },
                "repositoryConfigurations": {
                    "site": {"fieldMappings": [...]},
                    "document": {"fieldMappings": [...]},
                    "page": {"fieldMappings": [...]},
                    "list": {"fieldMappings": [...]},
                },
                "additionalProperties": {
                    "isCrawlAcl": "true",                       # KEY: inherit SharePoint ACLs
                    "fieldForUserId": "email",
                    "domain": "acme.sharepoint.com",
                },
                "secretArn": sharepoint_secret.secret_arn,      # creds in Secrets Manager
            },
        )

        # ── 7. Web Experience — auto-provisioned UI ──────────────────
        web_role = iam.Role(self, "WebRole",
            assumed_by=iam.ServicePrincipal("application.qbusiness.amazonaws.com"),
        )
        web_role.add_to_policy(iam.PolicyStatement(
            actions=["qbusiness:Chat", "qbusiness:ChatSync",
                     "qbusiness:ListMessages", "qbusiness:ListConversations",
                     "qbusiness:DeleteConversation", "qbusiness:PutFeedback",
                     "qbusiness:GetWebExperience", "qbusiness:GetApplication",
                     "qbusiness:ListPlugins", "qbusiness:GetChatControlsConfiguration"],
            resources=[application.attr_application_arn],
        ))

        web_experience = qb.CfnWebExperience(self, "QWebUI",
            application_id=application.attr_application_id,
            role_arn=web_role.role_arn,
            title="Acme Knowledge Assistant",
            subtitle="Ask anything about Acme.",
            welcome_message="Hi! I'm your Acme assistant. Ask me about our docs, policies, or use plugins for Jira / Salesforce.",
            sample_prompts_control_mode="ENABLED",
            origins=["https://acme.example.com"],              # for embedding
        )

        from aws_cdk import CfnOutput
        CfnOutput(self, "QAppId", value=application.attr_application_id)
        CfnOutput(self, "QWebUrl", value=web_experience.attr_default_endpoint)
```

### 3.2 Sync workflow

```bash
# Trigger initial data source sync (or wait for scheduled)
aws qbusiness start-data-source-sync-job \
  --application-id $APP_ID \
  --index-id $INDEX_ID \
  --data-source-id $DS_ID

# Monitor
aws qbusiness list-data-source-sync-jobs \
  --application-id $APP_ID \
  --index-id $INDEX_ID \
  --data-source-id $DS_ID

# After sync completes, query via web UI OR API
aws qbusiness chat-sync \
  --application-id $APP_ID \
  --user-id "user@acme.com" \
  --user-message "What's our remote work policy?" \
  --client-token $(uuidgen)
# Returns: response + source attributions + conversation ID
```

---

## 4. Plugins — built-in + custom

### 4.1 Built-in plugin (Jira example)

```python
jira_plugin = qb.CfnPlugin(self, "JiraPlugin",
    application_id=application.attr_application_id,
    type="JIRA_CLOUD",                                       # built-in type
    display_name="Jira",
    server_url="https://acme.atlassian.net",
    auth_configuration=qb.CfnPlugin.PluginAuthConfigurationProperty(
        oauth2_client_credential_configuration=qb.CfnPlugin.OAuth2ClientCredentialConfigurationProperty(
            secret_arn=jira_oauth_secret.secret_arn,
            role_arn=plugin_role.role_arn,
        ),
    ),
    state="ENABLED",
)
# User in chat: "Create a Jira ticket for the bug we just discussed"
# Q routes to Jira plugin → creates ticket using OAuth2 user identity
```

### 4.2 Custom OpenAPI plugin

```python
# 1. Define your API in OpenAPI 3.0 spec
# (Stored in S3, Q reads at plugin creation)

# Sample openapi.json for "expense-tracker" plugin:
# {
#   "openapi": "3.0.0",
#   "info": {"title": "Expense Tracker", "version": "1.0"},
#   "paths": {
#     "/expenses": {
#       "post": {
#         "operationId": "createExpense",
#         "summary": "Submit an expense report",
#         "parameters": [...],
#         "requestBody": {"content": {"application/json": {"schema": {...}}}},
#         "responses": {"200": {...}}
#       }
#     }
#   }
# }

custom_plugin = qb.CfnPlugin(self, "ExpensePlugin",
    application_id=application.attr_application_id,
    type="CUSTOM",
    display_name="Expense Tracker",
    auth_configuration=qb.CfnPlugin.PluginAuthConfigurationProperty(
        no_auth_configuration=qb.CfnPlugin.NoAuthConfigurationProperty(),  # or oauth2 / basic
    ),
    custom_plugin_configuration=qb.CfnPlugin.CustomPluginConfigurationProperty(
        description="Submit + view expense reports",
        api_schema_type="OPEN_API_V3",
        api_schema=qb.CfnPlugin.APISchemaProperty(
            payload=open_api_spec_json_string,                # or s3 reference
        ),
    ),
    state="ENABLED",
)
# User in chat: "Submit an expense for $42 lunch with client"
# Q parses → calls expense API via plugin → confirms back
```

---

## 5. Custom Q Apps (no-code app builder)

Q Apps are user-created mini-apps published in the Q Business app. Examples:

```yaml
# Sample Q App definition (created via Q Business UI; CDK can pre-provision)
name: "Onboarding Helper"
description: "Help new hires understand the codebase + setup"
prompt_template: |
  You are an onboarding assistant. The new hire is starting on the {{team}}
  team as a {{role}}. Their start date is {{start_date}}.
  Provide a personalized 2-week onboarding checklist with links to docs.

inputs:
  - name: team
    type: text
  - name: role
    type: text
  - name: start_date
    type: date

sources:
  - data_source: S3DataSource         # restrict retrieval to this DS
  - data_source: SharepointDS

plugins:
  - JIRA_CLOUD                         # allow Q app to create Jira tickets
```

CDK provisioning (preview API):

```python
qb.CfnQApp(self, "OnboardingApp",
    application_id=application.attr_application_id,
    title="Onboarding Helper",
    description="...",
    # ... full spec ...
)
```

---

## 6. AppRoles — group-based access control

```python
# Restrict who can use which plugin / data source / Q app
qb.CfnAppRoleAssociation(self, "DevsCanUseJiraPlugin",
    application_id=application.attr_application_id,
    principal=f"arn:aws:identitystore:::Group/{ids_dev_group_id}",
    role="USER",                                            # USER | ADMIN
    permissions={
        "plugins": [{"id": jira_plugin.attr_plugin_id, "permission": "CHAT_WITH_PLUGIN"}],
        "dataSources": [
            {"id": s3_data_source.attr_data_source_id, "permission": "RETRIEVE"},
            {"id": sharepoint_data_source.attr_data_source_id, "permission": "RETRIEVE"},
        ],
        "qApps": ["*"],
    },
)

# Finance team — only Salesforce + S3 docs + admin Q app
qb.CfnAppRoleAssociation(self, "FinanceCanUseSalesforce",
    application_id=application.attr_application_id,
    principal=f"arn:aws:identitystore:::Group/{ids_finance_group_id}",
    role="USER",
    permissions={
        "plugins": [{"id": salesforce_plugin.attr_plugin_id, "permission": "CHAT_WITH_PLUGIN"}],
        "dataSources": [{"id": s3_data_source.attr_data_source_id, "permission": "RETRIEVE"}],
        "qApps": [admin_q_app.attr_q_app_id],
    },
)
```

---

## 7. Common gotchas

- **Subscription tiers cost meaningfully**:
  - Q Business **Lite** $3/user/mo — no Q Apps creation, no plugins, no Q Developer.
  - Q Business **Pro** $20/user/mo — full features.
  - Bill is per active user per month — minimum 50 users for some discounts.
- **Index types**:
  - **STARTER** — free; 50K docs cap; experimental retrieval. Not for production.
  - **ENTERPRISE** — paid (~$1.40/hr per index unit); 100K-1M docs/unit; better recall.
- **Document-level permissions inheritance** requires `isCrawlAcl=true` in connector config + IDP user mapping. Without it, all users see all indexed content.
- **S3 connector incremental sync** uses S3 inventory or LastModified — make sure source bucket has versioning OR maintains modify timestamps.
- **SharePoint Online connector requires Microsoft Graph API permissions** — set up via Azure AD app registration. App-only or delegated.
- **Connector sync schedule cron** — runs in UTC. Stagger schedules across data sources to avoid throttling source systems.
- **Custom OpenAPI plugin OAuth2** — Q Business handles token exchange but spec must include `securitySchemes`. Plain bearer tokens won't work.
- **Q Apps SDK is preview (2024)** — APIs may change. Check release notes before relying.
- **Web UI custom domain** requires CloudFront distribution + ACM cert + ALB OR direct mapping. Not a simple toggle.
- **Citations** — Q always cites sources by default. For sensitive content, configure response control to suppress citations to specific source paths.
- **Conversation history retention default 30 days**. For longer (compliance), use `RetentionPolicy` API.
- **Native index doesn't support hybrid (keyword + vector) search OOTB** — Q manages that internally. For full hybrid control, use `BEDROCK_KNOWLEDGE_BASES` instead.
- **Data source connector errors are silent** — check `list-data-source-sync-jobs` for `ERROR_CODE` field; alarm on failed syncs.
- **Cross-region** — Q Business is region-local. Multi-region apps need separate Q Business apps per region.

---

## 8. Pytest worked example

```python
# tests/test_q_business.py
import boto3, time, pytest

qb = boto3.client("qbusiness")


def test_application_active(app_id):
    app = qb.get_application(applicationId=app_id)
    assert app["status"] == "ACTIVE"


def test_index_active(app_id, index_id):
    idx = qb.get_index(applicationId=app_id, indexId=index_id)
    assert idx["status"] == "ACTIVE"


def test_data_source_synced(app_id, index_id, data_source_id):
    """Latest sync should be SUCCEEDED, not failed."""
    syncs = qb.list_data_source_sync_jobs(
        applicationId=app_id, indexId=index_id, dataSourceId=data_source_id,
    )["history"]
    assert syncs
    latest = syncs[0]
    assert latest["status"] == "SUCCEEDED"


def test_chat_returns_with_citations(app_id, test_user_id):
    """Sample query should return response with source attributions."""
    resp = qb.chat_sync(
        applicationId=app_id,
        userId=test_user_id,
        userMessage="What is our company's PTO policy?",
    )
    assert resp.get("systemMessage")
    assert resp.get("sourceAttributions"), "No citations returned"
    # First citation should have title + URL
    citations = resp["sourceAttributions"]
    assert citations[0].get("title")
    assert citations[0].get("url") or citations[0].get("citationNumber")


def test_app_role_blocks_unauthorized_plugin(app_id, finance_user_id):
    """Finance user cannot use Jira plugin (not in their AppRole)."""
    resp = qb.chat_sync(
        applicationId=app_id,
        userId=finance_user_id,
        userMessage="Create a Jira ticket called 'Test'",
    )
    # Q should respond it can't use Jira; no plugin call
    assert "I can't use Jira" in resp["systemMessage"] or \
           not any(p["pluginId"].startswith("jira") for p in resp.get("usedPlugins", []))
```

---

## 9. Five non-negotiables

1. **IAM Identity Center as identity source** — never local Q users; SSO via your IDP only.
2. **Document-level ACL inheritance** (`isCrawlAcl=true`) on every connector that supports it.
3. **AppRoles per group** — never grant `*` permissions; scope plugins + data sources by group.
4. **CMK encryption** on application + index + connector secrets — never AWS-owned key.
5. **Citations enabled** by default — turn off only for sensitive prompt-style use (rare).

---

## 10. References

- [Amazon Q Business User Guide](https://docs.aws.amazon.com/amazonq/latest/qbusiness-ug/what-is.html)
- [Q Business connectors](https://docs.aws.amazon.com/amazonq/latest/qbusiness-ug/connectors-list.html)
- [Q Business plugins (built-in + custom)](https://docs.aws.amazon.com/amazonq/latest/qbusiness-ug/plugins.html)
- [Q Apps (preview)](https://docs.aws.amazon.com/amazonq/latest/qbusiness-ug/qapps.html)
- [Q Business pricing](https://aws.amazon.com/q/business/pricing/)
- [Identity Center setup](https://docs.aws.amazon.com/amazonq/latest/qbusiness-ug/idc-instance.html)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Q Business application + indexes + retrievers + 40+ connectors + plugins + Q Apps + AppRoles + Identity Center. Wave 15. |
