# SOP — Amazon QuickSight (datasets · SPICE refresh · embedded · ML insights · Q topics · row-level security · subscription pricing)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · QuickSight Enterprise + Q · datasets (direct query OR SPICE) · SPICE incremental refresh · scheduled refresh · embedded analytics · row-level security (RLS) · column-level security (CLS) · ML insights (anomaly detection, forecast) · Q topics (NL→SQL) · IAM Identity Center integration

---

## 1. Purpose

- Codify **QuickSight Enterprise + Q** as the canonical AWS-native BI layer. Replaces external BI (Tableau / Power BI) for AWS-first orgs that want native integration + 50% lower cost.
- Codify the **dataset patterns**: direct query (real-time, source-load) vs SPICE (in-memory, faster, cached).
- Codify **SPICE refresh strategies**: full vs incremental; scheduled; on-demand from EventBridge after data load.
- Codify **embedded analytics** for SaaS/internal apps — auth via Cognito JWT or IAM identity-aware embedding.
- Codify **row-level security (RLS)** — per-user data filter via dataset.
- Codify **column-level security (CLS)** — sensitive column hiding by group.
- Codify **ML insights** — anomaly detection on time-series, forecast.
- Codify **Q topics** — natural-language → SQL queries (now powered by Bedrock).
- Codify **subscription pricing** strategy — Author $24/mo, Reader $5/mo (or Reader Capacity for high-volume embedded).
- This is the **BI dashboards specialisation**. Pairs with `DATA_KINESIS_STREAMS_FIREHOSE` (real-time data → S3) + `DATA_OPENSEARCH_SERVERLESS` (real-time index) + `DATA_ATHENA` (query layer).

When the SOW signals: "BI dashboards", "embed analytics in our app", "QuickSight", "executive dashboards", "ML-driven insights", "natural language queries".

---

## 2. Decision tree — direct query vs SPICE; user vs reader

| Use case | Mode | Pricing |
|---|---|---|
| Real-time operational dashboard (< 1 min lag) | Direct query | Author $24 |
| Hourly executive dashboard | SPICE + scheduled refresh | Author $24, Reader $5 |
| Embedded in SaaS for 1000+ end-users | SPICE + Reader Capacity | $0.30/session-hour |
| Analyst exploration | SPICE + Q topics | Author + Reader Pro $10 |
| Real-time IoT (< 5 sec) | NOT QuickSight — use Grafana / OS Dashboards | — |

```
Layer:
  Sources (S3+Iceberg, Athena, Redshift, Aurora, Snowflake, Salesforce, ...)
       │
       ▼
  Datasets (curated SQL)
       │  (direct query OR SPICE refresh)
       ▼
  Analyses (designer view) ──► Dashboards (published, shared)
       │                              │
       │                              ├── Embedded in app via JWT
       │                              ├── Email subscription (PDF/CSV)
       │                              └── Slack share
       ▼
  Q topics (NL queries on dataset metadata)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 3 dashboards + SPICE + IDC SSO | **§3 Monolith** |
| Production — embedded SaaS + RLS + Q + ML insights | **§5 Embedded SaaS** |

---

## 3. Monolith Variant — Athena source + SPICE + IDC SSO

### 3.1 CDK

```python
# stacks/quicksight_stack.py
from aws_cdk import Stack
from aws_cdk import aws_quicksight as qs
from aws_cdk import aws_iam as iam
from constructs import Construct
import json


class QuicksightStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 athena_workgroup: str, athena_db: str,
                 idc_instance_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Pre-req: QuickSight account already enabled (CDK can't enable account itself)
        # Console: Subscribe to QuickSight Enterprise + Q in this AWS account

        # ── 1. Data source (Athena) ──────────────────────────────────
        athena_ds = qs.CfnDataSource(self, "AthenaSource",
            aws_account_id=self.account,
            data_source_id=f"{env_name}-athena",
            name=f"{env_name} Athena",
            type="ATHENA",
            data_source_parameters=qs.CfnDataSource.DataSourceParametersProperty(
                athena_parameters=qs.CfnDataSource.AthenaParametersProperty(
                    work_group=athena_workgroup,
                    role_arn=qs_athena_role.role_arn,    # role QS assumes to query Athena
                ),
            ),
            ssl_properties=qs.CfnDataSource.SslPropertiesProperty(disable_ssl=False),
            permissions=[qs.CfnDataSource.ResourcePermissionProperty(
                principal=f"arn:aws:quicksight:{self.region}:{self.account}:group/default/admins",
                actions=["quicksight:DescribeDataSource", "quicksight:DescribeDataSourcePermissions",
                         "quicksight:PassDataSource", "quicksight:UpdateDataSource",
                         "quicksight:DeleteDataSource", "quicksight:UpdateDataSourcePermissions"],
            )],
        )

        # ── 2. Dataset — SPICE-imported with custom SQL ──────────────
        events_ds = qs.CfnDataSet(self, "EventsDataset",
            aws_account_id=self.account,
            data_set_id=f"{env_name}-events-ds",
            name=f"{env_name} Events (last 30d)",
            import_mode="SPICE",                          # in-memory cache
            physical_table_map={
                "events_table": qs.CfnDataSet.PhysicalTableProperty(
                    custom_sql=qs.CfnDataSet.CustomSqlProperty(
                        data_source_arn=athena_ds.attr_arn,
                        name="events_30d",
                        sql_query=f"""
                            SELECT
                                event_id, event_type, user_id,
                                timestamp AS event_time,
                                CAST(properties['country'] AS VARCHAR) AS country,
                                CAST(properties['amount'] AS DOUBLE) AS amount
                            FROM {athena_db}.events
                            WHERE timestamp > current_timestamp - INTERVAL '30' DAY
                        """,
                        columns=[
                            {"name": "event_id", "type": "STRING"},
                            {"name": "event_type", "type": "STRING"},
                            {"name": "user_id", "type": "STRING"},
                            {"name": "event_time", "type": "DATETIME"},
                            {"name": "country", "type": "STRING"},
                            {"name": "amount", "type": "DECIMAL"},
                        ],
                    ),
                ),
            },
            permissions=[
                qs.CfnDataSet.ResourcePermissionProperty(
                    principal=f"arn:aws:quicksight:{self.region}:{self.account}:group/default/analysts",
                    actions=["quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions",
                             "quicksight:PassDataSet", "quicksight:DescribeIngestion",
                             "quicksight:ListIngestions"],
                ),
            ],
            # Row-level security
            row_level_permission_data_set=qs.CfnDataSet.RowLevelPermissionDataSetProperty(
                arn=rls_dataset_arn,                       # separate dataset with user→country mapping
                permission_policy="GRANT_ACCESS",
            ),
            # Column-level security
            column_level_permission_rules=[
                qs.CfnDataSet.ColumnLevelPermissionRuleProperty(
                    column_names=["amount"],
                    principals=[f"arn:aws:quicksight:{self.region}:{self.account}:group/default/finance"],
                ),
            ],
            # Refresh — incremental SPICE refresh
            data_set_refresh_properties=qs.CfnDataSet.DataSetRefreshPropertiesProperty(
                refresh_configuration=qs.CfnDataSet.RefreshConfigurationProperty(
                    incremental_refresh=qs.CfnDataSet.IncrementalRefreshProperty(
                        lookback_window=qs.CfnDataSet.LookbackWindowProperty(
                            column_name="event_time",
                            size=24,
                            size_unit="HOUR",
                        ),
                    ),
                ),
            ),
        )

        # ── 3. Refresh schedule — every hour ─────────────────────────
        qs.CfnRefreshSchedule(self, "EventsRefresh",
            aws_account_id=self.account,
            data_set_id=events_ds.data_set_id,
            schedule=qs.CfnRefreshSchedule.RefreshScheduleMapProperty(
                schedule_id=f"{env_name}-events-hourly",
                schedule_frequency=qs.CfnRefreshSchedule.RefreshScheduleMapProperty.ScheduleFrequencyProperty(
                    interval="HOURLY",
                ),
                refresh_type="INCREMENTAL_REFRESH",        # full SPICE refresh hourly
                start_after_date_time="2026-04-26T00:00:00",
            ),
        )

        # ── 4. Analysis (created from template OR via QS console) ─────
        # Best done via console + export to CFN. Skipping detailed analysis here.

        # ── 5. Identity Center integration (org-wide SSO) ────────────
        # Console step: QuickSight admin → Manage QuickSight → Single Sign-On
        # Map IDC groups to QuickSight roles (Admin, Author, Reader)
```

### 3.2 Q topic (NL queries via Bedrock)

```python
# Configure a Q topic from a dataset — analysts ask "show me daily revenue last week"
qs.CfnTopic(self, "EventsQTopic",
    aws_account_id=self.account,
    topic_id=f"{env_name}-events-q",
    name="Events analytics",
    description="Ask questions about events, users, revenue",
    user_experience_version="NEW_READER_EXPERIENCE",      # Q powered by Bedrock
    data_sets=[
        qs.CfnTopic.DatasetMetadataProperty(
            data_set_arn=events_ds.attr_arn,
            data_set_name="Events",
            data_set_description="Last 30 days of app events",
            calculated_fields=[],
            columns=[
                qs.CfnTopic.TopicColumnProperty(
                    column_name="event_type",
                    column_friendly_name="Event Type",
                    column_synonyms=["activity", "action", "event"],
                ),
                qs.CfnTopic.TopicColumnProperty(
                    column_name="amount",
                    column_friendly_name="Revenue",
                    column_synonyms=["money", "$", "USD", "revenue", "sales"],
                    aggregation="SUM",
                    semantic_type=qs.CfnTopic.SemanticTypeProperty(
                        type_name="Currency",
                        sub_type_name="USD",
                    ),
                ),
                qs.CfnTopic.TopicColumnProperty(
                    column_name="event_time",
                    column_friendly_name="When",
                    column_synonyms=["date", "time", "day", "hour"],
                    semantic_type=qs.CfnTopic.SemanticTypeProperty(type_name="Date"),
                ),
            ],
            named_entities=[
                qs.CfnTopic.TopicNamedEntityProperty(
                    entity_name="Geo",
                    entity_synonyms=["country", "region"],
                    semantic_entity_type=qs.CfnTopic.SemanticEntityTypeProperty(
                        type_name="Geography",
                    ),
                ),
            ],
        ),
    ],
)
```

---

## 4. Embedded analytics for SaaS

### 4.1 Architecture

```
   End-user signs into your SaaS app (Cognito / Auth0)
        │
        │ JWT
        ▼
   Your backend Lambda
        │ 1. Verify JWT
        │ 2. Determine which RLS rows user can see
        │ 3. AssumeRole to QS role with namespace + RLS tags
        │ 4. quicksight:GenerateEmbedUrlForRegisteredUser
        │    OR quicksight:GenerateEmbedUrlForAnonymousUser
        ▼
   Returns 1-time embed URL (≤ 5 min validity)
        │
        ▼
   Frontend embeds in <iframe>
```

### 4.2 Backend embed-URL Lambda

```python
# src/embed_url/handler.py
import boto3, json
qs = boto3.client("quicksight")

def handler(event, context):
    user_jwt = event["headers"]["authorization"].replace("Bearer ", "")
    # ... validate JWT, extract user_id, country ...
    user_id = "user-abc"
    country = "US"

    # For registered-user embed (preferred — RLS works)
    resp = qs.generate_embed_url_for_registered_user(
        AwsAccountId="123456789012",
        UserArn=f"arn:aws:quicksight:us-east-1:123456789012:user/default/{user_id}",
        SessionLifetimeInMinutes=600,
        ExperienceConfiguration={
            "Dashboard": {
                "InitialDashboardId": "dashboard-id",
                "FeatureConfigurations": {
                    "StatePersistence": {"Enabled": True},
                    "Bookmarks": {"Enabled": True},
                },
            },
        },
        AllowedDomains=["https://app.example.com"],
    )

    # For anonymous embed (no QS user; cheaper for 1000+ end-users)
    # resp = qs.generate_embed_url_for_anonymous_user(
    #     AwsAccountId=..., Namespace="default",
    #     AuthorizedResourceArns=[f"arn:aws:quicksight:...:dashboard/dashboard-id"],
    #     ExperienceConfiguration={...},
    #     AllowedDomains=["https://app.example.com"],
    #     SessionTags=[
    #         {"Key": "country", "Value": country},     # for RLS via session tags
    #         {"Key": "user_id", "Value": user_id},
    #     ],
    # )

    return {
        "statusCode": 200,
        "body": json.dumps({"embedUrl": resp["EmbedUrl"]}),
    }
```

---

## 5. Common gotchas

- **QuickSight enablement is per-account, console-only.** CDK can't subscribe the account. Bake into runbook.
- **Pricing:** Author $24/mo (annual) or $36/mo (monthly). Reader $5/mo. Q add-on +$50/mo per Author. Reader Pro $10/mo (Q access). Reader Capacity $0.30/session-hour for embedded.
- **SPICE quota: 1 GB/Author by default** — increase via support ticket. Each author = 10 GB SPICE in many accounts.
- **SPICE incremental refresh requires a date column with monotonic values**. Random updates to old rows = silent data loss in SPICE.
- **Direct query latency depends on source.** Athena = 5-30 sec; Redshift = 1-3 sec; Aurora = sub-sec.
- **RLS dataset must NOT include the principal column in user-visible columns** unless you want users to see who can see what.
- **CLS removes columns entirely from visualization** — they don't even appear as restricted. Users may be surprised.
- **Q topics powered by Bedrock (2024+)** — accuracy depends on column synonyms + named entities. Curate carefully.
- **Q topic refresh is manual** — re-run after dataset schema changes.
- **Embedded URLs are 1-time use, ≤ 5 min validity for first browser load**, then session lasts `SessionLifetimeInMinutes`.
- **`generate_embed_url_for_anonymous_user` requires `Reader Capacity Pricing` (RCP) enabled** — cheaper than per-user but invoiced separately.
- **`AllowedDomains` is enforced** — wrong domain = blank iframe. Add `https://app.example.com` AND `https://staging.example.com`.
- **IDC integration replaces native QuickSight users.** Migration requires careful planning if you have existing users.
- **ML insights (anomaly detection, forecast)** require Enterprise + sufficient historical data (3-12 months).
- **No CDK for analyses/dashboards** — author in console, export as `aws quicksight describe-dashboard-definition` JSON, manage via API.

---

## 6. Pytest worked example

```python
# tests/test_quicksight.py
import boto3, pytest

qs = boto3.client("quicksight")
ACCOUNT_ID = "123456789012"


def test_dataset_exists(dataset_id):
    ds = qs.describe_data_set(AwsAccountId=ACCOUNT_ID, DataSetId=dataset_id)["DataSet"]
    assert ds["ImportMode"] == "SPICE"


def test_dataset_has_rls(dataset_id):
    ds = qs.describe_data_set(AwsAccountId=ACCOUNT_ID, DataSetId=dataset_id)["DataSet"]
    assert ds.get("RowLevelPermissionDataSet"), "RLS not configured"


def test_refresh_schedule_exists(dataset_id):
    schedules = qs.list_refresh_schedules(
        AwsAccountId=ACCOUNT_ID, DataSetId=dataset_id,
    )["RefreshSchedules"]
    assert schedules
    assert schedules[0]["ScheduleFrequency"]["Interval"] in ["HOURLY", "DAILY"]


def test_dashboard_published(dashboard_id):
    dash = qs.describe_dashboard(AwsAccountId=ACCOUNT_ID, DashboardId=dashboard_id)["Dashboard"]
    assert dash["Version"]["Status"] == "CREATION_SUCCESSFUL"


def test_embed_url_generates(user_arn):
    resp = qs.generate_embed_url_for_registered_user(
        AwsAccountId=ACCOUNT_ID,
        UserArn=user_arn,
        SessionLifetimeInMinutes=15,
        ExperienceConfiguration={"Dashboard": {"InitialDashboardId": "test-dashboard"}},
    )
    assert resp["EmbedUrl"].startswith("https://")
```

---

## 7. Five non-negotiables

1. **SPICE for any dashboard with > 100 users** — direct query overwhelms source DBs.
2. **Incremental refresh** for SPICE datasets > 100 MB — full refresh wastes capacity.
3. **RLS configured** for any dataset shown to multiple tenants/teams.
4. **IDC SSO integration** — no native QuickSight users in production.
5. **`AllowedDomains` set on every embed-URL Generate** — without it, any domain can iframe.

---

## 8. References

- [QuickSight User Guide](https://docs.aws.amazon.com/quicksight/latest/user/welcome.html)
- [SPICE](https://docs.aws.amazon.com/quicksight/latest/user/spice.html)
- [Embedded analytics](https://docs.aws.amazon.com/quicksight/latest/user/embedded-analytics.html)
- [Row-Level Security](https://docs.aws.amazon.com/quicksight/latest/user/restrict-access-to-a-data-set-using-row-level-security.html)
- [Q topics (Bedrock-powered)](https://docs.aws.amazon.com/quicksight/latest/user/quicksight-q.html)
- [Pricing](https://aws.amazon.com/quicksight/pricing/)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. QS Enterprise + Q + SPICE + RLS + CLS + Q topics + embedded analytics + IDC SSO + Bedrock-powered Q. Wave 12. |
