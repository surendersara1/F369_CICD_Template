# SOP — Amazon AppFlow (SaaS-to-S3 ingest · Salesforce / Slack / ServiceNow / 60+ sources)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AppFlow flows · Source connectors: Salesforce, Slack, ServiceNow, Marketo, Datadog, Singular, Snowflake, Trend Micro, Veeva, Zendesk, Google Analytics, Mailchimp, Amplitude, SAP OData, etc. · Destinations: S3, Redshift, EventBridge, Snowflake, Salesforce (bidirectional) · PrivateLink for Salesforce · CDC via EventBridge for Salesforce

---

## 1. Purpose

- Codify the **SaaS ingest pattern** for landing data from CRM / support / marketing tools into the S3 lakehouse: AppFlow flow → S3 raw zone (CSV / JSON / Parquet) → Glue crawler → Athena.
- Provide the **Salesforce-specific** patterns: Bulk API 2.0 for full-load (millions of records), EventBridge for change events (CDC), PrivateLink for VPC-isolated Salesforce orgs.
- Codify the **scheduling** model: on-demand · scheduled (cron) · event-driven (EventBridge).
- Codify the **incremental load** patterns: filter by `LastModifiedDate > $watermark` (Salesforce), `updated_at > $watermark` (ServiceNow), full-table snapshot (Slack messages).
- Codify the **field mapping + transformations** done at flow time (filter, mask, validate, truncate, compute).
- This is the **SaaS-to-lakehouse specialisation**. DMS covers DB→DB; AppFlow covers SaaS→S3 / SaaS→DB.

When the SOW signals: "Salesforce data into our lakehouse", "Slack messages searchable in Athena", "ServiceNow tickets joined with order data", "marketing tool funnel analysis".

---

## 2. Decision tree

```
Source type?
├── SaaS API with AppFlow connector (Salesforce, Slack, ServiceNow, Marketo, etc.) → §3 AppFlow
├── SaaS API without AppFlow connector → custom Lambda or AppFlow custom connector (1-2 wk effort)
├── Database → see DATA_DMS_REPLICATION
├── Web webhooks → API Gateway + Lambda + Firehose (NOT AppFlow)
└── Files dropped to SFTP/FTPS → AWS Transfer Family (NOT AppFlow)

Latency tolerance?
├── < 1 hour: scheduled flow every 15 min OR event-driven (Salesforce CDC via EventBridge)
├── < 24 hours: scheduled flow daily
└── Real-time required: NOT AppFlow — use Kinesis-fed direct integration

Bidirectional?
├── Read-only from SaaS → §3 (default)
└── Write to Salesforce too → §4 (bidirectional flow)
```

### 2.1 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC — Salesforce connection + flow + S3 destination all in one stack | **§3 Monolith Variant** |
| `IntegrationStack` owns connections + flows; `DataStack` owns destination buckets + Glue resources | **§5 Micro-Stack Variant** |

**Why the split.** AppFlow Connection profiles store OAuth tokens / API keys in encrypted metadata; refreshing them shouldn't require redeploying downstream consumers. `IntegrationStack` lifecycle is independent of `DataStack`.

---

## 3. Monolith Variant — Salesforce → S3 (scheduled + CDC)

### 3.1 Architecture

```
   Salesforce Org (SecureForce.my.salesforce.com)
      │
      ├── Bulk API 2.0 (full-load, daily 02:00)
      │
      ├── REST API (incremental, every 15 min, filter LastModifiedDate)
      │
      └── Platform Events / CDC (real-time mutations) ──► EventBridge ──► AppFlow
                                                            ↓
   ┌──────────────────────────────────────────────────────────────────┐
   │  AppFlow Flow: salesforce-accounts-to-s3                          │
   │     - Source: Salesforce (Account object)                         │
   │     - Filter: IsDeleted = false                                   │
   │     - Mask: PersonalEmail, BillingStreet                          │
   │     - Validate: AccountId NOT NULL                                │
   │     - Destination: S3 (Parquet, partition by ingestion_date)      │
   │     - Schedule: 15-min interval (incremental) OR EB CDC trigger    │
   └──────────────────────────────────────────────────────────────────┘
        │
        ▼
   S3 raw bucket: s3://qra-raw/saas/salesforce/account/year=YYYY/month=MM/day=DD/
        │
        ▼
   Glue crawler (hourly) ──► Glue Catalog ──► Athena workgroup
```

### 3.2 CDK — `_create_appflow_salesforce_to_s3()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_appflow as appflow,             # L1 only — AppFlow not yet L2
    aws_secretsmanager as sm,
    aws_events as events,
    aws_events_targets as targets,
)


def _create_appflow_salesforce_to_s3(self, stage: str) -> None:
    """Monolith. Salesforce → S3 raw zone, scheduled every 15 min,
    Salesforce CDC events trigger event-driven backup."""

    # A) Salesforce Connection profile — uses OAuth credentials stored in
    # Secrets Manager. Salesforce admin pre-creates a Connected App + grants
    # the AWS user OAuth scopes; the cred is the resulting refresh_token.
    sf_secret = sm.Secret.from_secret_name_v2(
        self, "SfSecret",
        secret_name=f"{{project_name}}-salesforce-{stage}",
    )
    self.kms_key.grant_decrypt(sf_secret)

    sf_connector_profile = appflow.CfnConnectorProfile(self, "SfProfile",
        connector_profile_name=f"{{project_name}}-sf-{stage}",
        connector_type="Salesforce",
        connection_mode="Public",                                # or "Private" for PrivateLink
        connector_profile_config=appflow.CfnConnectorProfile.ConnectorProfileConfigProperty(
            connector_profile_credentials=appflow.CfnConnectorProfile.ConnectorProfileCredentialsProperty(
                salesforce=appflow.CfnConnectorProfile.SalesforceConnectorProfileCredentialsProperty(
                    access_token="{{resolve:secretsmanager:" + sf_secret.secret_name + ":SecretString:access_token}}",
                    refresh_token="{{resolve:secretsmanager:" + sf_secret.secret_name + ":SecretString:refresh_token}}",
                    client_credentials_arn=sf_secret.secret_arn,
                ),
            ),
            connector_profile_properties=appflow.CfnConnectorProfile.ConnectorProfilePropertiesProperty(
                salesforce=appflow.CfnConnectorProfile.SalesforceConnectorProfilePropertiesProperty(
                    instance_url="https://acme.my.salesforce.com",
                    is_sandbox_environment=(stage != "prod"),
                    use_private_link_for_metadata_and_authorization=False,
                ),
            ),
        ),
    )

    # B) The flow — schedules every 15 min, full-load Account object daily 02:00
    self.sf_account_flow = appflow.CfnFlow(self, "SfAccountFlow",
        flow_name=f"{{project_name}}-sf-account-{stage}",
        description="Salesforce Account → S3 raw zone",
        kms_arn=self.kms_key.key_arn,
        trigger_config=appflow.CfnFlow.TriggerConfigProperty(
            trigger_type="Scheduled",
            trigger_properties=appflow.CfnFlow.ScheduledTriggerPropertiesProperty(
                schedule_expression="rate(15 minutes)",            # 15-min incremental
                data_pull_mode="Incremental",
                schedule_start_time=int(datetime.utcnow().timestamp()),
                first_execution_from=int((datetime.utcnow() - timedelta(days=30)).timestamp()),
                schedule_offset=0,
                timezone="UTC",
            ),
        ),
        source_flow_config=appflow.CfnFlow.SourceFlowConfigProperty(
            connector_type="Salesforce",
            connector_profile_name=sf_connector_profile.connector_profile_name,
            source_connector_properties=appflow.CfnFlow.SourceConnectorPropertiesProperty(
                salesforce=appflow.CfnFlow.SalesforceSourcePropertiesProperty(
                    object="Account",
                    enable_dynamic_field_update=True,            # auto-pick up new Salesforce fields
                    include_deleted_records=False,                # don't pull soft-deleted
                ),
            ),
            incremental_pull_config=appflow.CfnFlow.IncrementalPullConfigProperty(
                datetime_type_field_name="LastModifiedDate",      # incremental key
            ),
        ),
        destination_flow_config_list=[
            appflow.CfnFlow.DestinationFlowConfigProperty(
                connector_type="S3",
                destination_connector_properties=appflow.CfnFlow.DestinationConnectorPropertiesProperty(
                    s3=appflow.CfnFlow.S3DestinationPropertiesProperty(
                        bucket_name=self.raw_bucket.bucket_name,
                        bucket_prefix="saas/salesforce/account",
                        s3_output_format_config=appflow.CfnFlow.S3OutputFormatConfigProperty(
                            file_type="PARQUET",
                            aggregation_config=appflow.CfnFlow.AggregationConfigProperty(
                                aggregation_type="SingleFile",     # or "None" per record
                            ),
                            prefix_config=appflow.CfnFlow.PrefixConfigProperty(
                                prefix_type="PATH_AND_FILENAME",
                                prefix_format="DAY",                # year/month/day partition
                                prefix_hierarchy=["EXECUTION_ID"],
                            ),
                            preserve_source_data_typing=True,
                        ),
                    ),
                ),
            ),
        ],
        tasks=[
            # Filter: exclude deleted records (already done at source, but extra guard)
            appflow.CfnFlow.TaskProperty(
                task_type="Filter",
                source_fields=["IsDeleted"],
                connector_operator=appflow.CfnFlow.ConnectorOperatorProperty(
                    salesforce="EQUAL_TO",
                ),
                task_properties=[
                    appflow.CfnFlow.TaskPropertiesObjectProperty(
                        key="VALUE", value="false",
                    ),
                ],
            ),
            # Mask: redact email + street
            appflow.CfnFlow.TaskProperty(
                task_type="Mask",
                source_fields=["PersonEmail", "BillingStreet"],
                connector_operator=appflow.CfnFlow.ConnectorOperatorProperty(
                    salesforce="MASK_ALL",
                ),
                task_properties=[
                    appflow.CfnFlow.TaskPropertiesObjectProperty(
                        key="MASK_LENGTH", value="5",
                    ),
                ],
            ),
            # Validate: AccountId not null
            appflow.CfnFlow.TaskProperty(
                task_type="Validate",
                source_fields=["Id"],
                connector_operator=appflow.CfnFlow.ConnectorOperatorProperty(
                    salesforce="VALIDATE_NON_NULL",
                ),
                task_properties=[
                    appflow.CfnFlow.TaskPropertiesObjectProperty(
                        key="VALIDATION_ACTION", value="DropRecord",
                    ),
                ],
            ),
            # Map all fields straight through
            appflow.CfnFlow.TaskProperty(
                task_type="Map_all",
                source_fields=[],
                connector_operator=appflow.CfnFlow.ConnectorOperatorProperty(
                    salesforce="NO_OP",
                ),
                task_properties=[],
            ),
        ],
    )

    CfnOutput(self, "SfFlowArn", value=self.sf_account_flow.attr_flow_arn)
```

### 3.3 Salesforce CDC via EventBridge (real-time, not scheduled)

```python
# Salesforce → AppFlow events on EventBridge partner event bus
# AppFlow auto-creates a partner event source named after the SF instance.

# 1) Associate the partner event source with EventBridge
events.CfnEventBus(self, "SfEventBus",
    name=f"aws.partner/appflow/salesforce-acme.my.salesforce.com",
    event_source_name=f"aws.partner/appflow/salesforce-acme.my.salesforce.com",
)

# 2) Rule for Salesforce CDC events of Account/Opportunity changes
events.Rule(self, "SfCdcRule",
    event_bus=events.EventBus.from_event_bus_name(
        self, "ImportedSfBus",
        event_bus_name="aws.partner/appflow/salesforce-acme.my.salesforce.com"),
    event_pattern=events.EventPattern(
        source=["Salesforce.com"],
        detail_type=["AccountChangeEvent", "OpportunityChangeEvent"],
    ),
    targets=[
        # Send to a Lambda that writes to S3 raw with the change payload
        targets.LambdaFunction(self.cdc_writer_fn),
        # AND/OR send to Firehose for buffered S3 writes
    ],
)
```

### 3.4 Field mapping cookbook — common task types

| Task | When to use | Example |
|---|---|---|
| `Map` | Rename single field | source `BillingPostalCode` → destination `zip_code` |
| `Map_all` | Default — pass-through all fields | (no params) |
| `Filter` | Drop records meeting condition | `IsDeleted == false` |
| `Mask` | Redact PII characters | `MASK_LENGTH=5` keeps last 5 chars |
| `Validate` | Drop records on validation failure | `VALIDATION_ACTION=DropRecord` for null |
| `Truncate` | Limit string length | `TRUNCATE_LENGTH=100` |
| `Arithmetic` | Compute derived field | `revenue * exchange_rate` |
| `Merge` | Concatenate fields | `firstName + ' ' + lastName` |

### 3.5 Salesforce-specific gotchas

| Issue | Fix |
|---|---|
| OAuth token expires every 24h | Set up secret rotation Lambda OR rely on AppFlow's auto-refresh via refresh_token |
| Bulk API 2.0 limited to 100K records per job | Schedule daily full-load that uses Bulk; 15-min flows use REST |
| Custom Salesforce objects (`__c`) not auto-discovered | Set `enable_dynamic_field_update=true` and rerun flow once |
| Salesforce field-level security blocks queries | Salesforce admin must grant the Connected App's Profile read on each field |
| Sandbox vs Prod org confusion | `is_sandbox_environment` differs; using prod cred against sandbox URL silently fails auth |
| Records deleted from source not deleted in lakehouse | Use Salesforce's "deleted records" API + downstream MERGE to keep target in sync |

---

## 4. Bidirectional flow (write back to Salesforce)

```python
# Flow that writes to Salesforce — destination_connector_properties.salesforce
self.write_to_sf_flow = appflow.CfnFlow(self, "WriteToSfFlow",
    ...
    destination_flow_config_list=[
        appflow.CfnFlow.DestinationFlowConfigProperty(
            connector_type="Salesforce",
            connector_profile_name=sf_connector_profile.connector_profile_name,
            destination_connector_properties=appflow.CfnFlow.DestinationConnectorPropertiesProperty(
                salesforce=appflow.CfnFlow.SalesforceDestinationPropertiesProperty(
                    object="Lead",
                    write_operation_type="UPSERT",            # INSERT / UPDATE / UPSERT / DELETE
                    id_field_names=["Email"],                  # natural key for upsert
                    error_handling_config=appflow.CfnFlow.ErrorHandlingConfigProperty(
                        bucket_name=self.error_bucket.bucket_name,
                        bucket_prefix="appflow-errors/sf-leads",
                        fail_on_first_error=False,
                    ),
                ),
            ),
        ),
    ],
)
```

---

## 5. Micro-Stack variant (cross-stack via SSM)

```python
# In IntegrationStack
ssm.StringParameter(self, "SfProfileName",
    parameter_name=f"/{{project_name}}/{stage}/integration/sf-profile",
    string_value=sf_connector_profile.connector_profile_name)
ssm.StringParameter(self, "SfFlowArn",
    parameter_name=f"/{{project_name}}/{stage}/integration/sf-flow-arn",
    string_value=self.sf_account_flow.attr_flow_arn)

# In DataStack — destination bucket grants read to flow service principal via
# bucket policy (since flow's role is in another stack)
self.raw_bucket.add_to_resource_policy(iam.PolicyStatement(
    effect=iam.Effect.ALLOW,
    principals=[iam.ServicePrincipal("appflow.amazonaws.com")],
    actions=["s3:PutObject", "s3:PutObjectAcl"],
    resources=[f"{self.raw_bucket.bucket_arn}/saas/salesforce/*"],
    conditions={"StringEquals": {
        "aws:SourceAccount": self.account,
    }},
))
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Flow executes but writes 0 records | Filter rejects all OR incremental key has no recent updates | Check CloudWatch `RecordsProcessed` metric; verify filter |
| Flow execution time > 1 hour | Salesforce Bulk API single-job limit | Split into multiple flows by date range or object subset |
| OAuth refresh fails after rotation | Secrets Manager rotation broke refresh_token | OAuth refresh_token only updates if SF-side rotation happens; rely on long-lived refresh_token from Connected App |
| AppFlow shows "InvalidUserCredentialsException" | OAuth token expired or scope insufficient | Re-auth via console once; verify Connected App scopes include `api` + `refresh_token` |
| S3 destination shows duplicate records | Incremental pull boundary issue | `LastModifiedDate >= $watermark` (inclusive) causes overlap; use `>` and accept ~1 record loss/run, or use Glue dedup |
| Slack flow "channel not found" | Bot not in channel | Slack admin invite the bot to the channel; OAuth scope `channels:history` required |
| ServiceNow rate limit | API key has tight throttle | ServiceNow admin: increase rate limit on the integration user; OR add backoff in flow (not native — use `error_handling_config`) |

### 6.1 Cost model

| Component | Cost |
|---|---|
| AppFlow flow execution | $0.001 per flow run + $0.0001 per record |
| AppFlow data processing (transformations) | $0.001 / 1000 records |
| S3 destination storage | $0.023 / GB |
| EventBridge partner events | $1.00 / million events |
| Salesforce API call (your SF org) | counted toward SF API quota — NOT AWS bill |

For 1M records / day with 4 transformations: ~$30 / month AppFlow + $5 EB + $20 S3 = ~$55 / month per source.

---

## 7. Worked example — pytest synth

```python
def test_appflow_sf_to_s3_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    deps = cdk.Stack(app, "Deps", env=env)
    key = kms.Key(deps, "Key")
    raw = s3.Bucket(deps, "Raw", encryption_key=key)

    from infrastructure.cdk.stacks.appflow_stack import AppflowSfStack
    stack = AppflowSfStack(app, stage_name="dev",
        kms_key=key, raw_bucket=raw, env=env)
    t = Template.from_stack(stack)

    t.has_resource_properties("AWS::AppFlow::ConnectorProfile", Match.object_like({
        "ConnectorType": "Salesforce",
        "ConnectionMode": "Public",
    }))
    t.has_resource_properties("AWS::AppFlow::Flow", Match.object_like({
        "TriggerConfig": Match.object_like({
            "TriggerType": "Scheduled",
            "TriggerProperties": Match.object_like({
                "ScheduleExpression": "rate(15 minutes)",
                "DataPullMode":       "Incremental",
            }),
        }),
        "SourceFlowConfig": Match.object_like({
            "ConnectorType": "Salesforce",
            "IncrementalPullConfig": Match.object_like({
                "DatetimeTypeFieldName": "LastModifiedDate",
            }),
        }),
        "DestinationFlowConfigList": Match.array_with([Match.object_like({
            "ConnectorType": "S3",
        })]),
    }))
```

---

## 8. Five non-negotiables

1. **PII masking happens IN AppFlow, not downstream.** Setting `Mask` task in the flow keeps the raw SaaS data from ever landing in S3 with PII. Once it's in S3, redacting requires a separate pipeline. Do it at flow-time.

2. **Always specify `error_handling_config` for write flows.** Without it, a single bad record fails the entire flow run. Set `fail_on_first_error=false` and route errors to a separate S3 prefix for inspection.

3. **Schedule alignment matters.** Don't schedule 4 different SaaS flows at `cron(0 * * * ? *)` (top of every hour) — they'll all hit AppFlow concurrency limits. Stagger by 5-10 min: `cron(0 *)`, `cron(5 *)`, `cron(10 *)`, etc.

4. **Use PrivateLink for Salesforce in production.** `connection_mode="Private"` requires Salesforce Shield + customer's VPC endpoint setup. Public-mode flows traverse the internet.

5. **Per-flow IAM role with least privilege.** AppFlow flows run as a service role you specify (default uses `AWSServiceRoleForAmazonAppFlow`). Override with a per-flow role granting only `s3:PutObject` on the specific destination prefix.

---

## 9. References

- `docs/template_params.md` — `APPFLOW_SCHEDULE_EXPRESSION`, `APPFLOW_INCREMENTAL_FIELD`, `APPFLOW_TRANSFORM_TASKS`, `APPFLOW_FILE_TYPE`, `APPFLOW_USE_PRIVATELINK`
- AWS docs:
  - [Salesforce connector](https://docs.aws.amazon.com/appflow/latest/userguide/salesforce.html)
  - [EventBridge integration with Salesforce](https://docs.aws.amazon.com/appflow/latest/userguide/EventBridge.html)
  - [Slack connector](https://docs.aws.amazon.com/appflow/latest/userguide/slack.html)
- Related SOPs:
  - `DATA_DMS_REPLICATION` — for DB sources (NOT SaaS)
  - `DATA_GLUE_CATALOG` — auto-crawl AppFlow output into Glue Catalog
  - `DATA_LAKEHOUSE_ICEBERG` — MERGE pattern for downstream Iceberg from incremental SaaS pulls
  - `LAYER_SECURITY` — KMS + Secrets Manager rotation

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Salesforce + Slack + ServiceNow + 60+ SaaS sources to S3 raw zone via AppFlow. Scheduled vs CDC vs event-driven trigger types. Field mapping cookbook (Filter, Mask, Validate, Map, Truncate, Arithmetic, Merge). Bidirectional flow (write to Salesforce) with upsert + error handling. Salesforce-specific gotchas (Bulk API limits, custom objects, sandbox vs prod). Cost model (~$55/mo for 1M records/day). Created to fill F369 audit gap (2026-04-26): SaaS ingest was 0% covered. |
