# SOP — Amazon Q in QuickSight (NL-to-chart, topics, embedded Q, Athena-backed)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `aws_cdk.aws_quicksight` L1 · QuickSight Enterprise edition + **Q Generative Capabilities add-on** (required, separately licensed per-user/month) · QuickSight Topics (Q subject areas) · Amazon QuickSight Embedding SDK 2.x · Athena as default dataset engine · Spice (columnar cache) optional · QuickSight Embed for anonymous-user sharing

---

## 1. Purpose

- Provide the deep-dive for **Amazon Q in QuickSight** — the NL-to-chart + NL-to-insight layer on top of QuickSight. A business user asks "what drove revenue decline in EMEA last quarter?" and Q returns charts, a narrative, and drillable visuals — without writing SQL. Pairs with `PATTERN_TEXT_TO_SQL` (programmatic SQL) and `PATTERN_ENTERPRISE_CHAT_ROUTER` (conversational agent) to cover the full self-service BI spectrum.
- Codify the **Topic** as the unit of self-service — a **Topic** is a curated subject area (e.g. "Finance Analytics") that binds to one or more QuickSight datasets, annotates columns with synonyms + descriptions + format hints, and pre-seeds Q with domain-specific sample questions. **Topics are THE product-design surface** — a well-authored topic is the difference between Q answering "total revenue" correctly vs hallucinating a column name.
- Codify the **dataset → topic → analysis → dashboard pipeline** — datasets ingest from Athena (live) or Spice (cached); topics reference datasets with Q-specific metadata; analyses are author-built dashboards; dashboards are the published read-only artifacts for end users. Q operates on datasets *through* topics — it does NOT query Athena directly.
- Codify the **embedded Q** pattern — use QuickSight Embedding SDK to drop the Q bar + visuals into any web app. Three embed modes: (a) registered user (Cognito/SAML); (b) anonymous user (session-scoped, per-namespace); (c) console-level Q (for analyst UIs). For chat-router integration: expose Q-generated visuals as signed URLs consumers can iframe or download.
- Codify the **Athena integration with Q** — Q translates NL to DAX-like expressions over a dataset, then QuickSight compiles to SQL against Athena. This means **Q's quality is bounded by the dataset's column comments + topic synonyms + sample values**. Invest in topic authoring before blaming the LLM.
- Codify the **Q caveats** — Q does NOT do cross-dataset joins natively (build the joined view in the dataset); Q does NOT do time-series forecasting unless you explicitly add a forecast visual; Q does NOT write DML; Q's suggestions are deterministic per-dataset per-topic but not cross-deploy-stable (re-rank shifts on dataset refresh).
- Codify the **governance** — QuickSight row-level security (RLS) + column-level security (CLS) enforce on every Q answer automatically. Users only see data their RLS permits. LF does NOT directly gate Q — QuickSight reads via its own role; RLS is the gate.
- Codify the **cost gotcha** — Q Generative Capabilities is a SEPARATE SKU on top of QuickSight Enterprise. ~$500/user/month list for Author access, ~$40/user/month for Reader access (subject to AWS pricing). Budget before promising "AI BI" to finance users; this can dwarf the Bedrock cost.
- Include when the SOW signals: "QuickSight", "self-service BI", "NL-to-chart", "BI chatbot", "natural-language dashboards", "Q in QuickSight", "embed Q", "business-user analytics", "ask my data in English".
- This partial is the **BI layer** for the AI-native lakehouse kit. Pairs with `DATA_ATHENA` (data source), `DATA_ICEBERG_S3_TABLES` / `DATA_LAKEHOUSE_ICEBERG` (underlying tables), `DATA_LAKE_FORMATION` (upstream data enforcement), `PATTERN_ENTERPRISE_CHAT_ROUTER` (agent delegates viz questions to Q).

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the Athena data source + datasets + topics + analysis + dashboard + user/group permissions | **§3 Monolith Variant** |
| `QuickSightStack` owns data sources + datasets + topics; `BiAnalyticsStack` owns analyses + dashboards; `EmbedStack` owns anonymous namespaces + embedding config | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **QuickSight principals are account-scoped.** Users and groups live in the QuickSight namespace, not IAM. `CfnUser` and `CfnGroup` creation is a one-time concern; putting them in a single place keeps the user directory authoritative.
2. **`CfnDataSource` and `CfnDataSet` reference each other + the underlying Athena workgroup.** Cross-stack Cfn references into QuickSight constructs work via string IDs; no circular dependency risk here, but naming discipline matters.
3. **Topics are versioned resources.** Each Topic publish replaces the Q-understanding state. Keeping topics in one stack makes diffs predictable; spreading across stacks produces confusing audit trails.
4. **Embedding requires namespace pinning.** Anonymous-user embeds run in a dedicated namespace; re-using the default namespace across all embed-contexts is insecure. `EmbedStack` should own the namespace.
5. **Row-level security tables (RLS tables) are datasets too.** They're often small + dynamic (user → region mapping). If they're in a different stack from the data datasets they filter, cross-stack Arn references apply.

Micro-stack fixes by: (a) `QuickSightStack` owns data sources + datasets + topics + RLS datasets + users/groups; (b) publishes dataset IDs + topic IDs via SSM; (c) `BiAnalyticsStack` reads those IDs + builds analyses + dashboards; (d) `EmbedStack` owns anonymous namespaces + embedding-config Lambdas that generate embedding URLs.

---

## 3. Monolith Variant

**Use when:** POC with a single dashboard for one user cohort.

### 3.1 Architecture

```
  ┌──────────────────────────────────────────────────────────────────┐
  │   QuickSight (us-east-1 or regional)                             │
  │                                                                  │
  │   CfnDataSource: Athena-LH-{stage}                               │
  │     type: ATHENA                                                 │
  │     workgroup: lakehouse-analyst-{stage}                         │
  │     role_arn: QuickSight service role (inherits S3 + Glue + LF)  │
  │                                                                  │
  │   CfnDataSet: ds_fact_revenue                                    │
  │     import_mode: DIRECT_QUERY (live) or SPICE (cached)           │
  │     physical_table_map:                                          │
  │       athena: SELECT * FROM lakehouse_{stage}.fact_revenue        │
  │     column_groups:                                               │
  │       time:    [ts, quarter, year]                               │
  │     column_tags:                                                 │
  │       amount:  {column_description: "Order total in currency",    │
  │                 column_geographic_role: null}                    │
  │       region:  {column_geographic_role: REGION}                  │
  │     field_folders:                                               │
  │       "time":     [ts, quarter, year]                            │
  │       "measures": [amount, currency]                             │
  │     row_level_permission_data_set: rls_ds (see below)            │
  │                                                                  │
  │   CfnDataSet: ds_dim_customer                                    │
  │     import_mode: SPICE                                           │
  │                                                                  │
  │   CfnDataSet: rls_ds                                             │
  │     Rows-table: user_name × region                               │
  │     Refreshed hourly                                             │
  │                                                                  │
  │   CfnTopic: finance_analytics                                    │
  │     datasets: [ds_fact_revenue, ds_dim_customer]                 │
  │     data_sets[0].dataset_metadata:                               │
  │       columns[amount].synonyms: ["revenue","total","sales"]      │
  │       columns[region].synonyms: ["market","territory"]           │
  │       columns[ts].semantic_type: DATE                            │
  │       columns[ts].time_granularity: "DAY"                        │
  │     default_answer_type: VISUAL                                  │
  │                                                                  │
  │   CfnAnalysis: finance_overview                                  │
  │     sheets[]: curated charts (dashboard scaffolding)             │
  │                                                                  │
  │   CfnDashboard: finance_published                                │
  │     source_entity: analysis_arn                                  │
  │     permissions: group "FinanceReaders" grants [DescribeDashboard]│
  │                                                                  │
  │   QuickSight Users/Groups:                                       │
  │     namespace: default                                           │
  │     user: alice@corp (IAM identity_type)                         │
  │     group: FinanceReaders, FinanceAnalysts                       │
  └──────────────────────────────────────────────────────────────────┘
                       ▲
                       │  Q bar + visuals (embedded)
  ┌──────────────────────────────────────────────────────────────────┐
  │  Web app → Embedding SDK → QuickSight Q                          │
  │    GetDashboardEmbedUrl (registered user)                        │
  │    GenerateEmbedUrlForAnonymousUser (anon, session-scoped)       │
  │    GetSessionEmbedUrl (full console with Q bar)                  │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_quicksight_q()` method body

```python
from aws_cdk import (
    CfnOutput, Stack,
    aws_iam as iam,
    aws_quicksight as qs,
)


def _create_quicksight_q(self, stage: str) -> None:
    """Monolith variant. Assumes self.{athena_workgroup_name,
    athena_results_bucket_arn, lake_bucket_arn, glue_db_name} exist and
    that QuickSight Enterprise + Q Generative Capabilities are already
    subscribed in the account."""

    aws_account_id = Stack.of(self).account
    region         = Stack.of(self).region
    qs_role_arn    = (
        f"arn:aws:iam::{aws_account_id}:role/service-role/"
        "aws-quicksight-service-role-v0"
    )

    # A) Data source — Athena.
    #    Note: type is "ATHENA"; `data_source_parameters` picks the workgroup.
    self.qs_data_source = qs.CfnDataSource(
        self, "QsAthenaSrc",
        aws_account_id=aws_account_id,
        data_source_id=f"athena-lh-{stage}",
        name=f"Athena Lakehouse {stage}",
        type="ATHENA",
        data_source_parameters=qs.CfnDataSource.DataSourceParametersProperty(
            athena_parameters=qs.CfnDataSource.AthenaParametersProperty(
                work_group=self.athena_workgroup_name,
                role_arn=qs_role_arn,
            ),
        ),
        ssl_properties=qs.CfnDataSource.SslPropertiesProperty(
            disable_ssl=False,
        ),
        # Permissions — grant QuickSight admins + authors describe/update.
        permissions=[qs.CfnDataSource.ResourcePermissionProperty(
            principal=f"arn:aws:quicksight:{region}:{aws_account_id}:"
                       f"group/default/Admins",
            actions=[
                "quicksight:UpdateDataSourcePermissions",
                "quicksight:DescribeDataSource",
                "quicksight:DescribeDataSourcePermissions",
                "quicksight:PassDataSource",
                "quicksight:UpdateDataSource",
                "quicksight:DeleteDataSource",
            ],
        )],
    )

    # B) RLS dataset — rows are user × region mappings.
    self.rls_ds = qs.CfnDataSet(
        self, "RlsDs",
        aws_account_id=aws_account_id,
        data_set_id=f"rls-ds-{stage}",
        name=f"Finance RLS {stage}",
        physical_table_map={
            "rls-source": qs.CfnDataSet.PhysicalTableProperty(
                relational_table=qs.CfnDataSet.RelationalTableProperty(
                    data_source_arn=self.qs_data_source.attr_arn,
                    catalog="AwsDataCatalog",
                    schema=f"lakehouse_{stage}",
                    name="rls_user_region",
                    input_columns=[
                        qs.CfnDataSet.InputColumnProperty(name="UserName", type="STRING"),
                        qs.CfnDataSet.InputColumnProperty(name="Region",   type="STRING"),
                    ],
                ),
            ),
        },
        import_mode="SPICE",              # SPICE: refreshed hourly
        permissions=[self._qs_admin_perm(aws_account_id, region, "DataSet")],
    )

    # C) Main dataset — fact_revenue joined with dim_customer, with RLS + Q
    #    metadata (column descriptions + synonyms).
    self.ds_fact_revenue = qs.CfnDataSet(
        self, "DsFactRevenue",
        aws_account_id=aws_account_id,
        data_set_id=f"ds-fact-revenue-{stage}",
        name=f"Fact Revenue {stage}",
        physical_table_map={
            "fact-revenue": qs.CfnDataSet.PhysicalTableProperty(
                relational_table=qs.CfnDataSet.RelationalTableProperty(
                    data_source_arn=self.qs_data_source.attr_arn,
                    catalog="AwsDataCatalog",
                    schema=f"lakehouse_{stage}",
                    name="fact_revenue",
                    input_columns=[
                        qs.CfnDataSet.InputColumnProperty(name="order_id",    type="INTEGER"),
                        qs.CfnDataSet.InputColumnProperty(name="customer_id", type="STRING"),
                        qs.CfnDataSet.InputColumnProperty(name="ts",          type="DATETIME"),
                        qs.CfnDataSet.InputColumnProperty(name="amount",      type="DECIMAL"),
                        qs.CfnDataSet.InputColumnProperty(name="currency",    type="STRING"),
                        qs.CfnDataSet.InputColumnProperty(name="region",      type="STRING"),
                    ],
                ),
            ),
        },
        # Column tags carry Q metadata.
        column_groups=[qs.CfnDataSet.ColumnGroupProperty(
            geo_spatial_column_group=qs.CfnDataSet.GeoSpatialColumnGroupProperty(
                name="GeoRegion",
                columns=["region"],
                country_code="US",      # placeholder — adjust per data
            ),
        )],
        field_folders={
            "Time":     qs.CfnDataSet.FieldFolderProperty(
                description="Temporal attributes",
                columns=["ts"],
            ),
            "Measures": qs.CfnDataSet.FieldFolderProperty(
                description="Numeric measures",
                columns=["amount"],
            ),
        },
        import_mode="SPICE",
        row_level_permission_data_set=qs.CfnDataSet.RowLevelPermissionDataSetProperty(
            arn=self.rls_ds.attr_arn,
            permission_policy="GRANT_ACCESS",     # or DENY_ACCESS
            status="ENABLED",
            format_version="VERSION_1",
        ),
        permissions=[self._qs_admin_perm(aws_account_id, region, "DataSet")],
    )
    self.ds_fact_revenue.add_dependency(self.rls_ds)

    # D) Topic — the Q subject area.
    self.topic = qs.CfnTopic(
        self, "FinanceTopic",
        aws_account_id=aws_account_id,
        topic_id=f"finance-analytics-{stage}",
        name="Finance Analytics",
        description=(
            "Revenue, customers, and renewals. Ask about totals, trends, "
            "top N by region, YoY/QoQ, and customer segment analysis."
        ),
        user_experience_version="NEW_READER_EXPERIENCE",
        data_sets=[qs.CfnTopic.DatasetMetadataProperty(
            dataset_arn=self.ds_fact_revenue.attr_arn,
            dataset_name="Fact Revenue",
            dataset_description="One row per settled order.",
            columns=[
                qs.CfnTopic.TopicColumnProperty(
                    column_name="amount",
                    column_friendly_name="Revenue",
                    column_description="Order total in the order's currency.",
                    column_synonyms=["revenue", "total", "sales", "order_value"],
                    aggregation="SUM",
                    # SemanticType.TypeName is an enum — ALL-CAPS values only:
                    #   BOOLEAN | CURRENCY | DATE | DIMENSION | DISTANCE |
                    #   DURATION | GEO_POINT | LOCATION | NUMBER | PERCENT |
                    #   PRODUCT | QUANTITY | TEMPERATURE | TIME | UUID
                    # SubTypeName holds qualifiers (e.g. currency code "USD").
                    semantic_type=qs.CfnTopic.SemanticTypeProperty(
                        type_name="CURRENCY",
                        sub_type_name="USD",
                    ),
                ),
                qs.CfnTopic.TopicColumnProperty(
                    column_name="region",
                    column_friendly_name="Region",
                    column_description="ISO market region code.",
                    column_synonyms=["market", "territory", "geography"],
                ),
                qs.CfnTopic.TopicColumnProperty(
                    column_name="ts",
                    column_friendly_name="Order Date",
                    column_synonyms=["date", "order time", "settlement date"],
                    time_granularity="DAY",
                    semantic_type=qs.CfnTopic.SemanticTypeProperty(
                        type_name="DATE",
                    ),
                ),
                qs.CfnTopic.TopicColumnProperty(
                    column_name="customer_id",
                    column_friendly_name="Customer",
                    column_synonyms=["customer", "account", "client"],
                    aggregation="COUNT_DISTINCT",
                ),
            ],
        )],
    )

    # E) Analysis — curated dashboard scaffolding. The UI in QuickSight
    #    Author is where non-CDK users build sheets; CDK can define the
    #    initial skeleton.
    self.analysis = qs.CfnAnalysis(
        self, "FinanceAnalysis",
        aws_account_id=aws_account_id,
        analysis_id=f"finance-overview-{stage}",
        name="Finance Overview",
        source_entity=qs.CfnAnalysis.AnalysisSourceEntityProperty(
            source_template=qs.CfnAnalysis.AnalysisSourceTemplateProperty(
                arn=(
                    # Use an existing public Template ARN, or a custom one
                    # published in this account.
                    f"arn:aws:quicksight:{region}:{aws_account_id}:"
                    f"template/finance-overview-template"
                ),
                data_set_references=[
                    qs.CfnAnalysis.DataSetReferenceProperty(
                        data_set_arn=self.ds_fact_revenue.attr_arn,
                        data_set_placeholder="revenue_placeholder",
                    ),
                ],
            ),
        ),
        permissions=[self._qs_admin_perm(aws_account_id, region, "Analysis")],
    )
    self.analysis.add_dependency(self.ds_fact_revenue)

    # F) Dashboard — published, read-only.
    self.dashboard = qs.CfnDashboard(
        self, "FinanceDashboard",
        aws_account_id=aws_account_id,
        dashboard_id=f"finance-dashboard-{stage}",
        name="Finance Dashboard",
        source_entity=qs.CfnDashboard.DashboardSourceEntityProperty(
            source_template=qs.CfnDashboard.DashboardSourceTemplateProperty(
                arn=(
                    f"arn:aws:quicksight:{region}:{aws_account_id}:"
                    f"template/finance-overview-template"
                ),
                data_set_references=[
                    qs.CfnDashboard.DataSetReferenceProperty(
                        data_set_arn=self.ds_fact_revenue.attr_arn,
                        data_set_placeholder="revenue_placeholder",
                    ),
                ],
            ),
        ),
        permissions=[
            # Readers (FinanceReaders group) get Describe only.
            qs.CfnDashboard.ResourcePermissionProperty(
                principal=f"arn:aws:quicksight:{region}:{aws_account_id}:"
                           f"group/default/FinanceReaders",
                actions=[
                    "quicksight:DescribeDashboard",
                    "quicksight:ListDashboardVersions",
                    "quicksight:QueryDashboard",
                ],
            ),
            # Authors (FinanceAnalysts) get full rights.
            qs.CfnDashboard.ResourcePermissionProperty(
                principal=f"arn:aws:quicksight:{region}:{aws_account_id}:"
                           f"group/default/FinanceAnalysts",
                actions=[
                    "quicksight:DescribeDashboard",
                    "quicksight:ListDashboardVersions",
                    "quicksight:UpdateDashboardPermissions",
                    "quicksight:QueryDashboard",
                    "quicksight:UpdateDashboard",
                    "quicksight:DeleteDashboard",
                    "quicksight:UpdateDashboardPublishedVersion",
                ],
            ),
        ],
        dashboard_publish_options=qs.CfnDashboard.DashboardPublishOptionsProperty(
            ad_hoc_filtering_option=qs.CfnDashboard.AdHocFilteringOptionProperty(
                availability_status="ENABLED",
            ),
            export_to_csv_option=qs.CfnDashboard.ExportToCSVOptionProperty(
                availability_status="ENABLED",
            ),
            sheet_controls_option=qs.CfnDashboard.SheetControlsOptionProperty(
                visibility_state="EXPANDED",
            ),
        ),
    )
    self.dashboard.add_dependency(self.analysis)

    # G) Outputs.
    CfnOutput(self, "QsDashboardId",   value=self.dashboard.dashboard_id)
    CfnOutput(self, "QsTopicId",       value=self.topic.topic_id)
    CfnOutput(self, "QsDataSourceArn", value=self.qs_data_source.attr_arn)


def _qs_admin_perm(self, account_id: str, region: str, resource_type: str) -> qs.CfnDataSet.ResourcePermissionProperty:
    """Admin group gets describe/update on the resource."""
    actions_by_type = {
        "DataSet": [
            "quicksight:DescribeDataSet",
            "quicksight:DescribeDataSetPermissions",
            "quicksight:PassDataSet",
            "quicksight:DescribeIngestion",
            "quicksight:ListIngestions",
            "quicksight:UpdateDataSet",
            "quicksight:DeleteDataSet",
            "quicksight:CreateIngestion",
            "quicksight:CancelIngestion",
            "quicksight:UpdateDataSetPermissions",
        ],
        "Analysis": [
            "quicksight:DescribeAnalysis",
            "quicksight:UpdateAnalysis",
            "quicksight:RestoreAnalysis",
            "quicksight:UpdateAnalysisPermissions",
            "quicksight:DeleteAnalysis",
            "quicksight:QueryAnalysis",
            "quicksight:DescribeAnalysisPermissions",
        ],
    }.get(resource_type, [])
    return qs.CfnDataSet.ResourcePermissionProperty(
        principal=f"arn:aws:quicksight:{region}:{account_id}:group/default/Admins",
        actions=actions_by_type,
    )
```

### 3.3 Embedding Q — Lambda that mints a URL

```python
# lambda/qs_embed/handler.py
"""
Mint a QuickSight embed URL for a registered user. Called from the web
app backend after Cognito auth.

Event: {
  "dashboard_id": "finance-dashboard-prod",
  "username":     "alice@corp",          # Cognito user
  "session_lifetime_minutes": 60,
  "allow_topic":  true                  # expose Q bar
}
"""
import json
import os
from typing import Any

import boto3

QS_ACCOUNT_ID = os.environ["QS_ACCOUNT_ID"]
QS_NAMESPACE  = os.environ["QS_NAMESPACE"]          # usually "default"

qs = boto3.client("quicksight")


def lambda_handler(event, _ctx) -> dict[str, Any]:
    dashboard_id = event["dashboard_id"]
    username     = event["username"]
    lifetime     = event.get("session_lifetime_minutes", 60)
    allow_topic  = event.get("allow_topic", True)

    # The embed URL is signed + session-scoped.
    resp = qs.generate_embed_url_for_registered_user(
        AwsAccountId=QS_ACCOUNT_ID,
        SessionLifetimeInMinutes=lifetime,
        UserArn=(
            f"arn:aws:quicksight:{os.environ['AWS_REGION']}:"
            f"{QS_ACCOUNT_ID}:user/{QS_NAMESPACE}/{username}"
        ),
        ExperienceConfiguration={
            "Dashboard": {
                "InitialDashboardId": dashboard_id,
                "FeatureConfigurations": {
                    # Show the Q bar in the embedded dashboard.
                    "StatePersistence": {"Enabled": True},
                },
            },
        } if not allow_topic else {
            # Full Q console experience with Q bar.
            "QuickSightConsole": {
                "InitialPath": f"/dashboards/{dashboard_id}",
            },
        },
        AllowedDomains=["https://app.example.com"],     # strict allow-list
    )

    return {
        "embed_url":           resp["EmbedUrl"],
        "session_expiry_iso":  "2026-04-22T12:00:00Z",   # derived from lifetime
        "request_id":          resp["RequestId"],
    }
```

Web-side integration:

```javascript
// ui/src/qs-embed.ts
import {
    createEmbeddingContext,
} from "amazon-quicksight-embedding-sdk";

const { embedUrl } = await fetch("/api/qs-embed", {
  method: "POST",
  body: JSON.stringify({
    dashboard_id: "finance-dashboard-prod",
    username:     currentUser.email,
    allow_topic:  true,
  }),
}).then(r => r.json());

const ctx = await createEmbeddingContext({});
const dashboard = await ctx.embedDashboard({
  url:       embedUrl,
  container: document.getElementById("qs-panel")!,
  resizeHeightOnSizeChangedEvent: true,
  toolbarOptions: { export: true, undoRedo: true, reset: true },
});
```

### 3.4 Monolith gotchas

1. **Q Generative Capabilities is a SEPARATE subscription.** `CfnTopic` deploys fine without Q enabled, but `generate_embed_url_for_registered_user` with `allow_topic=True` and the Q bar WILL NOT RENDER. Enable Q in the QuickSight console (per-namespace, per-user licence assignment) before expecting Q answers.
2. **QuickSight is region-bound**. Dashboards + datasets live in one region; cross-region replication is NOT automatic. Pick the region with QuickSight Q availability (most major regions).
3. **Dataset refresh via CDK is a separate API call.** `CfnDataSet` creates + updates the definition; actual SPICE refresh is `CreateIngestion` which you wire via an EB-scheduled Lambda or manual invocation. SPICE data staleness without refresh is ~1 hour default; for live, use `DIRECT_QUERY`.
4. **RLS datasets must match user names exactly.** QuickSight compares `UserName` in the RLS dataset to the QuickSight `UserArn` username suffix. If RLS has `alice@corp` but the user is registered as `alice`, RLS silently denies everything.
5. **Column friendly names show in the UI; column names show in SQL.** If the underlying table renames a column, the Topic breaks — Q queries go through the original column. Version the Topic + retrain Q on dataset schema changes.
6. **Semantic types ("Currency", "Date") affect Q's answer generation.** Mis-tagging a boolean as a measure will make Q compute `SUM(boolean)` for "total X". Review semantic type assignments manually.
7. **Template ARNs (for `CfnAnalysis` / `CfnDashboard`) MUST exist before deploy.** Publish your template ARN once (via console or API) and pin it. CDK cannot create the template in the same synth that references it (circular).
8. **Embedding URLs are single-use + short-lived.** Cache the embed URL server-side for at most 5 min; regenerate on each page load. Users who leave a tab open past `SessionLifetimeInMinutes` will see the embed break.
9. **AllowedDomains must include the exact origin.** Missing `https://` or trailing slash discrepancies cause CORS failures that are hard to debug.
10. **Q answers may hallucinate if the Topic is under-specified.** Always include synonyms, semantic types, sample questions. Spend real author time on the Topic; it's the product surface.

---

## 4. Micro-Stack Variant

**Use when:** large BI deployment with many topics, multiple user groups, embedded + console modes.

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** — on the embedding Lambda entry.
2. **Identity-side grants** — the embedding Lambda grants itself `quicksight:GenerateEmbedUrlForRegisteredUser` / `GenerateEmbedUrlForAnonymousUser`; consumers grant themselves `lambda:InvokeFunction` on the embed Lambda's ARN.
3. **`CfnRule` cross-stack EventBridge** — for SPICE refresh orchestration, the EB scheduled rule lives in `QuickSightStack`; target Lambda (refresh runner) is same-stack.
4. **Same-stack bucket + OAC** — N/A.
5. **KMS ARNs as strings** — if the Athena result bucket uses KMS, QuickSight's service role needs `kms:Decrypt`; this is set once at subscription time, not per-stack.

### 4.2 QuickSightStack — owns data sources + datasets + topics

```python
# stacks/quicksight_stack.py  (abbreviated — same pattern as §3.2 with SSM
# inputs for athena_workgroup_name, athena_results_bucket, glue_db_name)
from aws_cdk import Stack, aws_quicksight as qs, aws_ssm as ssm
from constructs import Construct


class QuickSightStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        athena_wg = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/athena/workgroup_name"
        )

        # Data source, RLS dataset, main dataset, topic — same as §3.2.
        # ... omitted to save space ...

        # Publish IDs for BiAnalyticsStack to consume.
        ssm.StringParameter(self, "TopicIdParam",
            parameter_name=f"/{{project_name}}/{stage}/quicksight/topic_id",
            string_value=f"finance-analytics-{stage}",
        )
        ssm.StringParameter(self, "FactRevenueDatasetIdParam",
            parameter_name=f"/{{project_name}}/{stage}/quicksight/ds_fact_revenue_id",
            string_value=f"ds-fact-revenue-{stage}",
        )
```

### 4.3 BiAnalyticsStack — consumes dataset + builds dashboards

```python
# stacks/bi_analytics_stack.py
# Reads dataset IDs from SSM; builds analyses + dashboards independently.
```

### 4.4 Micro-stack gotchas

- **Topic update after dataset schema change** is ALWAYS a new version. CDK `cdk deploy` publishes a new topic version; QuickSight auto-promotes. For manual review, flip `auto_promote=False` — but that's a console setting, not CDK.
- **Deletion order**: upstream QuickSightStack → BiAnalyticsStack (deploy); BiAnalytics → QuickSight (delete). Dashboards deleted first, then datasets, then data source.
- **Anonymous embedding namespaces** can't be deleted once created (per AWS API). Use a versioned naming convention (`embed-ns-v1`, `embed-ns-v2`) if you expect frequent cleanup.

---

## 5. Swap matrix

| Concern | Default | Swap with | Why |
|---|---|---|---|
| BI engine | QuickSight + Q | Tableau / Power BI | Existing corporate standard. Accept Q ecosystem loss; connect via Athena JDBC. |
| Q alternative | Q in QuickSight | Bedrock-powered chart-gen via `PATTERN_ENTERPRISE_CHAT_ROUTER` + matplotlib tool | Fully custom UX; no per-user Q licence cost; build everything. Higher engineering investment. |
| Data source | Athena live query | Spice cache | Sub-second interactive charts; stale data by refresh interval. Use Spice for repeated dashboard use, live for ad-hoc Q. |
| Data source | Athena | Redshift | Existing warehouse. Use when data is already in Redshift; Athena federation for lake-side tables. |
| Row-level security | RLS dataset | Tag-based row filter in LF | LF is AWS-native + cross-engine. RLS is simpler but QuickSight-only. |
| Embedding | `GenerateEmbedUrlForRegisteredUser` | `GenerateEmbedUrlForAnonymousUser` | Customer-facing app; not Cognito-backed. Pairs with tenant-scoped namespace. |
| Template | Pre-existing published template | In-line `Definition` (newer CDK) | Template is reusable across analyses + dashboards; `Definition` is self-contained but verbose. |
| Topic authoring | CDK `CfnTopic` | QuickSight console + `DescribeTopic` export | Console is faster for iteration; export + commit JSON to version-control. |
| Dataset refresh | Manual `CreateIngestion` | EB schedule + Lambda | Automated; pair with data-quality checks before refresh. |
| Q integration with chat | Direct embed | Chat router delegates viz to Q via signed URL | Best of both — router handles blended Q&A, Q handles visualisation. |

---

## 6. Worked example

```python
# tests/test_quicksight_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.quicksight_stack import QuickSightStack


def test_synth_topic_dataset_with_column_synonyms():
    app = cdk.App()
    stack = QuickSightStack(app, "QS-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # Topic has synonyms on amount column.
    tpl.has_resource_properties("AWS::QuickSight::Topic", {
        "Name": "Finance Analytics",
        "DataSets": Match.array_with([
            Match.object_like({
                "Columns": Match.array_with([
                    Match.object_like({
                        "ColumnName": "amount",
                        "ColumnFriendlyName": "Revenue",
                        "ColumnSynonyms": Match.array_with([
                            "revenue", "total", "sales",
                        ]),
                    }),
                ]),
            }),
        ]),
    })

    # RLS dataset present + fact_revenue dataset references it.
    tpl.resource_count_is("AWS::QuickSight::DataSet", 2)
    tpl.has_resource_properties("AWS::QuickSight::DataSet", {
        "Name": "Fact Revenue dev",
        "RowLevelPermissionDataSet": Match.object_like({
            "PermissionPolicy": "GRANT_ACCESS",
            "Status":           "ENABLED",
        }),
    })


# tests/test_integration_embed.py
"""Integration — require QuickSight Q + a test user."""
import pytest, os, boto3


@pytest.mark.integration
def test_embed_url_for_alice():
    qs = boto3.client("quicksight")
    account = os.environ["QS_ACCOUNT_ID"]
    username = "alice@corp"
    resp = qs.generate_embed_url_for_registered_user(
        AwsAccountId=account,
        SessionLifetimeInMinutes=15,
        UserArn=f"arn:aws:quicksight:us-east-1:{account}:user/default/{username}",
        ExperienceConfiguration={
            "QuickSightConsole": {
                "InitialPath": "/dashboards/finance-dashboard-dev",
            },
        },
        AllowedDomains=["http://localhost:3000"],
    )
    assert resp["EmbedUrl"].startswith("https://")
```

---

## 7. References

- AWS docs — *Amazon QuickSight Q* (topics, NL engine, embedded Q).
- AWS docs — *QuickSight Embedding SDK* (registered + anonymous flows).
- AWS docs — *QuickSight RLS + CLS*.
- `DATA_ATHENA.md` — the query engine under Q.
- `DATA_ICEBERG_S3_TABLES.md` / `DATA_LAKEHOUSE_ICEBERG.md` — underlying tables.
- `DATA_LAKE_FORMATION.md` — upstream enforcement; complements QuickSight RLS.
- `PATTERN_ENTERPRISE_CHAT_ROUTER.md` — can delegate viz to Q via embed URL.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Topic-as-product-surface emphasis. Column synonyms + semantic types + friendly names. RLS dataset pattern. Embedding SDK with registered + anonymous flows. Athena-backed data source. Q licence cost callout. 10 monolith gotchas, 3 micro-stack gotchas, 10-row swap matrix, pytest synth + embed integration harness.
