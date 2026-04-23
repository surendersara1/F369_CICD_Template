# SOP — AWS Zero-ETL Integrations (Aurora/RDS/DynamoDB → Redshift; DynamoDB → OpenSearch; S3 → Redshift auto-copy)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `aws_cdk.aws_rds` + `aws_cdk.aws_redshift` + `aws_cdk.aws_dynamodb` L1/L2 · Zero-ETL resources: `AWS::RDS::Integration`, `AWS::Redshift::Integration` (newer), `AWS::DynamoDB::GlobalTable` for DDB streams, Amazon OpenSearch Service + DDB zero-ETL · Aurora MySQL 3.05+ / Aurora Postgres 16.4+ / RDS MySQL 8.0.32+ / Redshift Serverless (minimum 8 RPU) · `aws_cdk.aws_redshiftserverless` for target workgroup

---

## 1. Purpose

- Provide the deep-dive for **AWS Zero-ETL** — the family of managed replication integrations that eliminate custom pipelines between operational databases and the warehouse/search/lake. **Zero-ETL means AWS runs the CDC (change-data-capture) pipeline**; you only declare source + target + IAM; AWS handles the replication, schema evolution, and retries. 5–15 minute replication lag; read-only target; billing rolls into the TARGET warehouse cost.
- Codify the **four integration shapes** and when each applies:
  - **Aurora → Redshift Serverless** (most mature; GA 2023) — operational Postgres/MySQL replicating into Redshift for BI + aggregation. Target is read-only.
  - **RDS MySQL → Redshift Serverless** (GA 2024) — same shape for non-Aurora MySQL.
  - **DynamoDB → Redshift Serverless** (GA 2024) — NoSQL → columnar warehouse for analytics-on-DDB.
  - **DynamoDB → OpenSearch Service** (GA 2024) — live operational search over DDB items without a Lambda + Firehose pipeline.
  - **Aurora Postgres → S3 Iceberg** (via Glue Catalog + AWS Database Migration Service replacement — still preview-stage in some regions; we document the Glue-zero-ETL shape).
- Codify the **contract model** — an `Integration` resource has: **source ARN** (DB cluster / DDB table), **target ARN** (Redshift namespace / OpenSearch domain), an **IntegrationName**, and a **DataFilter** (optional — per-table or per-column include/exclude). CDK L1 `CfnIntegration` creates the binding; AWS creates the managed replication service + monitoring dashboards.
- Codify the **compatibility gates** — NOT every source version works with every target. Aurora Postgres 15+ is required for Postgres zero-ETL (16.4+ is current minimum per AWS docs, April 2026); Aurora MySQL 3.05+ with `binlog_format=ROW`, `binlog_row_image=FULL`; RDS MySQL 8.0.32+; target Redshift Serverless must have `enable_case_sensitive_identifier=true` + `integration_enabled=true` parameter group settings.
- Codify the **target-side preparation** — before an Integration activates, the target MUST have a **database name** and an **IAM role trust policy** allowing the replication principal (`redshift.amazonaws.com` for RDS→Redshift; `dynamodb.amazonaws.com` for DDB→OS). In Redshift, a **source database name** is pre-created by the Integration activation; you then `CREATE EXTERNAL SCHEMA` to view it.
- Codify the **CDC semantics** — Zero-ETL is EVENTUALLY CONSISTENT with 5–15 minute typical lag. Bulk-backfill happens once at Integration creation (can take HOURS for large tables); after backfill, ongoing replication is near-real-time CDC. Replication state is visible in CloudWatch as `IntegrationDataStreamingLagInSeconds`.
- Codify the **schema evolution** — ADD COLUMN, ADD TABLE are auto-propagated; RENAME, DROP, type change are **NOT** auto-handled (some force a full re-sync; others break the integration until resolved). Plan schema ops on the source.
- Codify the **cost model** — Zero-ETL itself does NOT have a line item; cost is absorbed into the target (Redshift RPU-hours; OpenSearch instance hours). Source DB continues billing as normal. The hidden cost: source Aurora needs `aurora_activity_stream_mode=async` on (free) + storage overhead for CDC log retention (Aurora I/O cost).
- Include when the SOW signals: "zero-ETL", "Aurora to Redshift", "DDB analytics", "operational reporting", "no-ETL data pipeline", "near-real-time warehouse", "replace AWS DMS", "CDC without Lambdas".
- This partial is the **DATA-MOVEMENT layer** for the AI-native lakehouse kit. Pairs with `DATA_AURORA_SERVERLESS_V2` (Aurora source), `DATA_ICEBERG_S3_TABLES` (alternative lake target), `DATA_LAKEHOUSE_ICEBERG` (Redshift Spectrum side), and provides source data for `DATA_ATHENA` / `PATTERN_TEXT_TO_SQL` when Aurora OLTP data needs to appear in analytics.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the source Aurora cluster + target Redshift workgroup + Integration resource + IAM roles | **§3 Monolith Variant** |
| `AuroraStack` owns the source DB; `RedshiftStack` owns the target; `IntegrationStack` owns the `CfnIntegration` + monitoring | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **The Integration binds two long-lived resources.** Source Aurora cluster lifecycle (often prod, managed conservatively) differs from target Redshift (often data-team owned). Putting them in one stack creates a blast radius on deletions.
2. **Integration creation triggers a large backfill.** A 500 GB Aurora → Redshift initial sync can take 6+ hours. If it's part of a monolith stack deploy, the deploy times out (CFN default 15 min per resource). Break into a separate stack with its own CFN timeout (up to 12 hours via `timeout` on custom resources).
3. **Parameter group requirements on source + target are subtle.** Aurora source needs `binlog_format=ROW` + `binlog_row_image=FULL` (MySQL) or `rds.logical_replication=1` + `aurora.enhanced_binlog=1` (Postgres). Redshift target needs `enable_case_sensitive_identifier=true`. These are CLUSTER parameter groups — not instance parameter groups. Misconfiguration means the Integration ACTIVATES but no data flows.
4. **IAM trust between services is critical.** The Integration uses a service-linked role that AWS auto-creates; but target Redshift also needs a role with `redshift:*` + source-resource-policy acceptance. Cross-stack role references are string-ARN only; break the Cfn reference cycle.
5. **CloudWatch alarms on replication lag** live near the Integration. Owner: `IntegrationStack`.

Micro-Stack fixes by: (a) `IntegrationStack` owns the `CfnIntegration` + source-resource-policy (if cross-account) + CloudWatch alarms; (b) source + target ARN come in via SSM; (c) the Integration's long initial backfill runs outside the stack's CFN deploy via an EB-triggered Lambda that waits for `ACTIVE` state.

---

## 3. Monolith Variant

**Use when:** single-account POC — Aurora + Redshift + one Integration.

### 3.1 Architecture — Aurora → Redshift

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  Source — Aurora Postgres Serverless v2                          │
  │    cluster_identifier:    lh-source-{stage}                      │
  │    engine_version:        VER_16_4                               │
  │    cluster_parameter_group:                                      │
  │      rds.logical_replication=1                                   │
  │      aurora.enhanced_binlog=1                                    │
  │      max_logical_replication_workers=4                           │
  │      max_parallel_replication_workers=2                          │
  │                                                                  │
  │   Tables to replicate: public.orders, public.customers, ...      │
  └────────────────────┬─────────────────────────────────────────────┘
                       │
                       │  zero-ETL CDC (managed)
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  AWS::RDS::Integration                                           │
  │    source_arn:       aurora-cluster-arn                          │
  │    target_arn:       redshift-namespace-arn                      │
  │    integration_name: "aurora-to-redshift-{stage}"                │
  │    data_filter:      "include: public.*"                         │
  │    kms_key_id:       local CMK                                   │
  └────────────────────┬─────────────────────────────────────────────┘
                       │
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Target — Redshift Serverless                                    │
  │    namespace:       lh-warehouse-{stage}                         │
  │    workgroup:       lh-wg-{stage}                                │
  │    base_capacity:   8 RPU (minimum for integrations)             │
  │    parameter_group:                                              │
  │      enable_case_sensitive_identifier=true                       │
  │                                                                  │
  │   After activation:                                              │
  │     CREATE DATABASE aurora_replica FROM INTEGRATION 'aurora-...' │
  │     Tables appear under aurora_replica.public.*                  │
  │                                                                  │
  │   CloudWatch:                                                    │
  │     IntegrationDataStreamingLagInSeconds                         │
  │     IntegrationTablesInFailedState                               │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_zero_etl_aurora_to_redshift()` method body

```python
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_rds as rds,
    aws_redshiftserverless as rss,
    aws_sns as sns,
)


def _create_zero_etl_aurora_to_redshift(self, stage: str) -> None:
    """Monolith variant. Assumes self.{vpc, db_secret} exist."""

    # A) Local CMK.
    self.zetl_cmk = kms.Key(
        self, "ZetlCmk",
        alias=f"alias/{{project_name}}-zetl-{stage}",
        enable_key_rotation=True,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Source Aurora Postgres cluster parameter group — zero-ETL requires
    #    these CLUSTER-level params (not instance-level):
    #      rds.logical_replication=1
    #      aurora.enhanced_binlog=1
    source_pg = rds.ParameterGroup(
        self, "ZetlSourceParamGroup",
        engine=rds.DatabaseClusterEngine.aurora_postgres(
            version=rds.AuroraPostgresEngineVersion.VER_16_4,
        ),
        parameters={
            "rds.logical_replication":           "1",
            "aurora.enhanced_binlog":            "1",
            "max_logical_replication_workers":   "4",
            "max_parallel_replication_workers":  "2",
            "max_replication_slots":             "20",
            "max_wal_senders":                   "20",
            "log_min_duration_statement":        "1000",
        },
    )

    # C) Source Aurora cluster (abbreviated — see DATA_AURORA_SERVERLESS_V2
    #    for full config).
    self.aurora_cluster = rds.DatabaseCluster(
        self, "ZetlSourceAurora",
        engine=rds.DatabaseClusterEngine.aurora_postgres(
            version=rds.AuroraPostgresEngineVersion.VER_16_4,
        ),
        credentials=rds.Credentials.from_secret(self.db_secret),
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        cluster_identifier=f"{{project_name}}-lh-source-{stage}",
        default_database_name="appdb",
        serverless_v2_min_capacity=0.5,
        serverless_v2_max_capacity=4,
        writer=rds.ClusterInstance.serverless_v2("writer"),
        readers=[rds.ClusterInstance.serverless_v2("reader1", scale_with_writer=True)],
        parameter_group=source_pg,
        storage_encrypted=True,
        storage_encryption_key=self.zetl_cmk,
        backup=rds.BackupProps(retention=Duration.days(14)),
    )

    # D) Target Redshift Serverless workgroup + namespace.
    #    Namespace holds the database; workgroup is the compute.
    self.rs_namespace = rss.CfnNamespace(
        self, "ZetlTargetRsNamespace",
        namespace_name=f"{{project_name}}-lh-wh-{stage}",
        admin_username="admin",
        admin_user_password="_cdk-injected-secret_",  # replace via Secret
        db_name=f"wh_{stage}",
        kms_key_id=self.zetl_cmk.key_id,
        iam_roles=[],              # filled below after role created
    )

    # Role the Redshift namespace assumes for Integration + Spectrum + S3.
    rs_role = iam.Role(
        self, "RsIntegrationRole",
        assumed_by=iam.CompositePrincipal(
            iam.ServicePrincipal("redshift.amazonaws.com"),
            iam.ServicePrincipal("redshift-serverless.amazonaws.com"),
        ),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonRedshiftAllCommandsFullAccess"
            ),
        ],
    )
    # Integration trust — source RDS service principal writes into target.
    rs_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "rds:DescribeDBClusters", "rds:DescribeDBInstances",
            "rds:DescribeIntegrations",
        ],
        resources=["*"],
    ))
    # Allow Redshift to read from Integration S3 shard bucket (AWS-managed).
    rs_role.add_to_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.zetl_cmk.key_arn],
    ))

    # Attach the role to the namespace (via L1 override — L2 property is
    # a list; CDK mutates after create).
    self.rs_namespace.iam_roles = [rs_role.role_arn]

    # Redshift parameter group — enable_case_sensitive_identifier=true.
    rs_pg = rss.CfnWorkgroup.ConfigParameterProperty(
        parameter_key="enable_case_sensitive_identifier",
        parameter_value="true",
    )
    self.rs_workgroup = rss.CfnWorkgroup(
        self, "ZetlTargetRsWorkgroup",
        workgroup_name=f"{{project_name}}-lh-wg-{stage}",
        namespace_name=self.rs_namespace.namespace_name,
        base_capacity=8,                     # 8 RPU is minimum for zero-ETL
        max_capacity=64,
        enhanced_vpc_routing=True,
        publicly_accessible=False,
        subnet_ids=[s.subnet_id for s in self.vpc.private_subnets],
        security_group_ids=[self.redshift_sg.security_group_id],
        config_parameters=[rs_pg],
    )
    self.rs_workgroup.add_dependency(self.rs_namespace)

    # E) Source-resource policy — allow the Redshift account/region to
    #    accept this Integration. (In single-account, same-region, this is
    #    implicit; we still add it for completeness + future cross-account.)
    source_resource_policy_doc = iam.PolicyDocument(statements=[
        iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("redshift.amazonaws.com")],
            actions=["rds:AuthorizeDBLogDeliveryToIntegration"],
            resources=[self.aurora_cluster.cluster_arn],
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": Stack.of(self).account,
                },
            },
        ),
    ])
    # Note: AWS::RDS::Integration references source_arn + target_arn. The
    # source policy is optional in same-account same-region; required for
    # cross-account. We emit it anyway as a future-proof.
    iam.CfnPolicy(
        self, "ZetlSourcePolicy",
        policy_name=f"{{project_name}}-zetl-source-{stage}",
        policy_document=source_resource_policy_doc,
        roles=[rs_role.role_name],
    )

    # F) The Integration itself — the star of the partial.
    #    Note: CfnIntegration uses the RDS namespace
    #    (AWS::RDS::Integration), even for Aurora→Redshift.
    self.integration = rds.CfnIntegration(
        self, "AuroraToRedshiftIntegration",
        source_arn=self.aurora_cluster.cluster_arn,
        target_arn=(
            f"arn:aws:redshift-serverless:{Stack.of(self).region}:"
            f"{Stack.of(self).account}:namespace/{self.rs_namespace.attr_namespace_namespace_id}"
        ),
        integration_name=f"{{project_name}}-aurora-to-redshift-{stage}",
        kms_key_id=self.zetl_cmk.key_arn,
        # Data filter — include all schemas under "public", all tables.
        # Syntax: "include: public.*" or per-table "include: public.orders"
        # Separate multiple includes/excludes with comma.
        data_filter="include: public.*",
        additional_encryption_context={
            "aws:rds:integration": f"{{project_name}}-zetl-{stage}",
        },
    )
    self.integration.add_dependency(self.aurora_cluster)
    self.integration.add_dependency(self.rs_workgroup)

    # G) CloudWatch alarms on replication lag.
    alarm_topic = sns.Topic(
        self, "ZetlLagTopic",
        topic_name=f"{{project_name}}-zetl-lag-{stage}",
    )
    cw.Alarm(
        self, "HighReplicationLagAlarm",
        alarm_description="Zero-ETL replication lag > 15 min for 3 consecutive periods.",
        metric=cw.Metric(
            namespace="AWS/RDS",
            metric_name="IntegrationDataStreamingLagInSeconds",
            dimensions_map={"IntegrationName": self.integration.integration_name},
            statistic="Maximum",
            period=Duration.minutes(5),
        ),
        threshold=900,                  # 15 minutes
        evaluation_periods=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

    cw.Alarm(
        self, "FailedTablesAlarm",
        alarm_description="Zero-ETL tables in failed state > 0.",
        metric=cw.Metric(
            namespace="AWS/RDS",
            metric_name="IntegrationTablesInFailedState",
            dimensions_map={"IntegrationName": self.integration.integration_name},
            statistic="Maximum",
            period=Duration.minutes(5),
        ),
        threshold=0,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

    # H) Outputs.
    CfnOutput(self, "IntegrationArn", value=self.integration.attr_integration_arn)
    CfnOutput(self, "IntegrationName", value=self.integration.integration_name)
    CfnOutput(self, "RsWorkgroupName", value=self.rs_workgroup.workgroup_name)
    CfnOutput(self, "RsNamespaceName", value=self.rs_namespace.namespace_name)
```

### 3.3 Post-activation — declare the integration database in Redshift

Zero-ETL activates the Integration; at that point the data is *available* but not yet queryable by name. You must issue one-time DDL in Redshift:

```sql
-- Run once as the admin, after the Integration moves to ACTIVE.
-- <integration-id> comes from the IntegrationArn (the last path segment).
CREATE DATABASE aurora_replica
    FROM INTEGRATION '<integration-id>'
    DATABASE 'appdb';

-- Now query tables as aurora_replica.public.<table>.
SELECT count(*) FROM aurora_replica.public.orders;
```

For automation, wrap this in a custom-resource-backed Lambda that polls the Integration for `ACTIVE` status + issues the DDL:

```python
# lambda/zetl_activate_db/handler.py
import json, time, os, boto3, psycopg

rds = boto3.client("rds")
INTEGRATION_NAME = os.environ["INTEGRATION_NAME"]
RS_SECRET_ARN    = os.environ["RS_SECRET_ARN"]
RS_DB            = os.environ["RS_DB"]
RS_HOST          = os.environ["RS_HOST"]
INTEGRATION_DB   = os.environ["INTEGRATION_DB"]
SOURCE_DB        = os.environ["SOURCE_DB"]

def lambda_handler(event, _ctx):
    # Custom resource event — create / delete / update.
    if event["RequestType"] == "Delete":
        # Optional: DROP DATABASE integration_db (requires no open connections)
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "zetl-db")}

    # Wait for integration ACTIVE.
    for _ in range(60):
        resp = rds.describe_integrations(IntegrationIdentifier=INTEGRATION_NAME)
        status = resp["Integrations"][0]["Status"]
        if status == "active":
            break
        if status in ("failed", "deleting"):
            raise RuntimeError(f"Integration in {status}")
        time.sleep(30)

    integration_id = resp["Integrations"][0]["IntegrationArn"].split("/")[-1]

    # Connect to Redshift as admin, issue DDL.
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=RS_SECRET_ARN)
    creds = json.loads(secret["SecretString"])
    with psycopg.connect(
        host=RS_HOST, dbname=RS_DB,
        user=creds["username"], password=creds["password"],
        port=5439, sslmode="require",
    ) as conn:
        with conn.cursor() as cur:
            # Idempotent — CREATE DATABASE ... IF NOT EXISTS is not supported;
            # catch + ignore "database already exists".
            try:
                cur.execute(
                    f"CREATE DATABASE {INTEGRATION_DB} "
                    f"FROM INTEGRATION '{integration_id}' "
                    f"DATABASE '{SOURCE_DB}'"
                )
            except psycopg.errors.DuplicateDatabase:
                pass
        conn.commit()
    return {"PhysicalResourceId": f"zetl-db-{integration_id}"}
```

### 3.4 Alternate: DynamoDB → Redshift

```python
def _create_zero_etl_ddb_to_redshift(self, stage: str) -> None:
    """DynamoDB → Redshift. Different CFN type:
    AWS::DynamoDB::GlobalTable is NOT the integration — it's
    AWS::RDS::Integration again, with target=Redshift, source=DDB arn.
    (As of April 2026 the preferred CFN resource for DDB-to-Redshift is
    AWS::RDS::Integration with a DynamoDB source; an L1 ergonomics upgrade
    is expected.)"""
    ddb_table = self.orders_table   # existing DDB table

    ddb_to_rs = rds.CfnIntegration(
        self, "DdbToRedshiftIntegration",
        source_arn=ddb_table.table_arn,
        target_arn=self.rs_namespace_arn,
        integration_name=f"{{project_name}}-ddb-to-redshift-{stage}",
        kms_key_id=self.zetl_cmk.key_arn,
        # DDB data filter is a single table; use "*" for all attributes
        # or a JSON path expression for projection pushdown.
        data_filter="include: *",
    )
```

### 3.5 Alternate: DynamoDB → OpenSearch Service

```python
from aws_cdk import aws_opensearchservice as os_svc


def _create_zero_etl_ddb_to_opensearch(self, stage: str) -> None:
    domain = os_svc.Domain(
        self, "ZetlOsDomain",
        version=os_svc.EngineVersion.OPENSEARCH_2_13,
        capacity=os_svc.CapacityConfig(
            data_nodes=3,
            data_node_instance_type="r7g.large.search",
        ),
        encryption_at_rest=os_svc.EncryptionAtRestOptions(
            enabled=True, kms_key=self.zetl_cmk,
        ),
        node_to_node_encryption=True,
        enforce_https=True,
    )

    # The integration is actually AWS::DynamoDB::GlobalTable streaming +
    # an OpenSearch ingestion pipeline; as of April 2026 the single-resource
    # CFN shape is AWS::DynamoDB::*Integration (preview). Using pipeline:
    from aws_cdk import aws_osis as osis      # OpenSearch Ingestion Service

    self.ddb_to_os_pipeline = osis.CfnPipeline(
        self, "DdbToOsPipeline",
        pipeline_name=f"{{project_name}}-ddb-to-os-{stage}",
        min_units=1, max_units=4,
        pipeline_configuration_body=(
            # The pipeline YAML — source: DDB, sink: OpenSearch.
            # AWS ships a canonical "dynamodb-to-opensearch" template.
            f"version: \"2\"\n"
            f"dynamodb-pipeline:\n"
            f"  source:\n"
            f"    dynamodb:\n"
            f"      acknowledgments: true\n"
            f"      tables:\n"
            f"        - table_arn: \"{self.orders_table.table_arn}\"\n"
            f"          export:\n"
            f"            s3_bucket: \"arn:aws:s3:::...\"\n"
            f"          stream:\n"
            f"            start_position: \"LATEST\"\n"
            f"  sink:\n"
            f"    - opensearch:\n"
            f"        hosts: [\"{domain.domain_endpoint}\"]\n"
            f"        index: \"orders\"\n"
            f"        aws:\n"
            f"          region: \"{Stack.of(self).region}\"\n"
        ),
    )
```

### 3.6 Monolith gotchas

1. **Initial backfill can exceed CFN timeout.** A 100 GB Aurora → Redshift backfill takes 2–6 hours. The `CfnIntegration` resource returns "active" at CREATION (replication STARTED), not "active" at BACKFILL COMPLETE. Do NOT put application code depending on data freshness in the same `cdk deploy`. Build a separate post-deploy workflow.
2. **Source parameter-group changes require cluster REBOOT.** `rds.logical_replication=1` + `aurora.enhanced_binlog=1` are static parameters — applied only on reboot. Set them BEFORE creating the integration, or accept a maintenance-window reboot.
3. **Target Redshift minimum is 8 RPU** — you cannot use a < 8 RPU workgroup with Zero-ETL. Budget accordingly (~$0.36/RPU-hour).
4. **`integration_name` is immutable.** Renaming requires delete + recreate, triggering a full backfill.
5. **DDL changes on source** (new table) auto-propagate. DROP TABLE, RENAME TABLE, column type changes often break the integration — `IntegrationTablesInFailedState` alarm catches it, but recovery is manual: drop the target table + refresh via `REFRESH TABLE <integration_db>.<schema>.<table>`.
6. **Data filter syntax is tricky.** `include: public.*` includes all tables; `include: public.orders, public.customers` includes two; `exclude: *` then `include: public.orders` is WRONG — use one direction. Test filter strings on a small integration first.
7. **Cross-account / cross-region** requires source-resource-policy + acceptance in target. Extra IAM choreography; document in separate partial if needed.
8. **Monitoring metrics for DDB-OS pipelines are DIFFERENT** from RDS-Redshift. DDB→OS uses OSIS metrics (`pipeline_active_units`, `pipeline_invalid_events`). Set up separately.
9. **Source credentials MUST be rotated WITHOUT breaking the integration.** AWS-managed rotation is fine; manual rotation that changes the replication user's password breaks replication. Use a dedicated replication user with Secrets Manager.
10. **Data types don't always round-trip.** Aurora Postgres `JSONB` becomes Redshift `SUPER`; Aurora `TIMESTAMPTZ` becomes Redshift `TIMESTAMPTZ`; Aurora `UUID` becomes Redshift `VARCHAR`. Document type mappings for your schema.

---

## 4. Micro-Stack Variant

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** — on the custom-resource backfill-waiter Lambda entry.
2. **Identity-side grants** — consumers (agent, BI) query Redshift via `GetClusterCredentials` + IAM role on SSM-read workgroup ARN.
3. **`CfnRule` cross-stack EventBridge** — for DDL-drift alerts, EB rule lives in `IntegrationStack`; target is the alert Lambda.
4. **Same-stack bucket + OAC** — N/A.
5. **KMS ARNs as strings** — source cluster CMK + target workgroup CMK often differ; SSM-publish both.

### 4.2 IntegrationStack — owns the CfnIntegration

```python
# stacks/integration_stack.py
from aws_cdk import (
    CfnOutput, Duration, Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_rds as rds,
    aws_sns as sns,
    aws_ssm as ssm,
)
from constructs import Construct


class IntegrationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        aurora_cluster_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/aurora/cluster_arn"
        )
        rs_namespace_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/redshift/namespace_arn"
        )
        zetl_cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/zetl/cmk_arn"
        )

        integration = rds.CfnIntegration(
            self, "AuroraToRedshift",
            source_arn=aurora_cluster_arn,
            target_arn=rs_namespace_arn,
            integration_name=f"{{project_name}}-a2r-{stage}",
            kms_key_id=zetl_cmk_arn,
            data_filter="include: public.*",
        )

        alarm_topic = sns.Topic(
            self, "ZetlLagTopic",
            topic_name=f"{{project_name}}-zetl-lag-{stage}",
        )
        cw.Alarm(
            self, "HighReplicationLagAlarm",
            metric=cw.Metric(
                namespace="AWS/RDS",
                metric_name="IntegrationDataStreamingLagInSeconds",
                dimensions_map={"IntegrationName": integration.integration_name},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=900, evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

        ssm.StringParameter(
            self, "IntegrationArnParam",
            parameter_name=f"/{{project_name}}/{stage}/zetl/integration_arn",
            string_value=integration.attr_integration_arn,
        )
        ssm.StringParameter(
            self, "IntegrationNameParam",
            parameter_name=f"/{{project_name}}/{stage}/zetl/integration_name",
            string_value=integration.integration_name,
        )

        CfnOutput(self, "IntegrationArn", value=integration.attr_integration_arn)
```

### 4.3 Consumer pattern — analytics Lambda reads Redshift

```python
# In AnalyticsStack — consumer reads from the integration DB in Redshift.
workgroup = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/redshift/workgroup_name"
)
# Build the analytics Lambda with Redshift Data API access.
lam.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "redshift-data:ExecuteStatement",
        "redshift-data:GetStatementResult",
        "redshift-data:DescribeStatement",
    ],
    resources=[
        f"arn:aws:redshift-serverless:{self.region}:{self.account}:workgroup/{workgroup}",
    ],
))
# ... and GetClusterCredentials for auth into the integration DB ...
```

### 4.4 Micro-stack gotchas

- **Deletion order**: IntegrationStack → consumer (deploy); consumer → IntegrationStack → RedshiftStack → AuroraStack (delete). Deleting source before Integration leaves the Integration in a failed state and retention of CDC logs in the source can grow unbounded.
- **Source + target are in different accounts**: add source-resource-policy allowing the target account to receive CDC; acceptance is one-time manual in the target account.
- **Custom-resource backfill-waiter** is a good place to emit a CloudWatch event when backfill completes — downstream dashboards + alerting can key off it.

---

## 5. Swap matrix

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Source→target pair | Aurora Postgres → Redshift | Aurora MySQL → Redshift | MySQL workload; same CFN shape. |
| Source→target pair | Aurora → Redshift | DynamoDB → Redshift | NoSQL workload; same CFN shape with DDB source ARN. |
| Source→target pair | Aurora → Redshift | DynamoDB → OpenSearch | Search use case; OSIS pipeline instead of RDS Integration. |
| Source→target pair | Aurora → Redshift | Aurora → S3 Iceberg via Glue zero-ETL (preview in some regions) | Lake-first storage; schema-evolve friendly. |
| Target engine | Redshift Serverless 8 RPU | Provisioned Redshift | Cost predictability for steady workloads; min 8 RPU equivalent. |
| Replication mechanism | Zero-ETL (managed CDC) | AWS DMS | Cross-cloud / cross-engine (e.g. Postgres → Snowflake). Zero-ETL is AWS-only. |
| Replication mechanism | Zero-ETL | Custom Lambda + DynamoDB Streams / logical decoding | Full control, ops overhead. Use only when filter complexity exceeds DataFilter DSL. |
| Replication lag | 5-15 min | Real-time via Kafka MSK + Debezium | Sub-second; high ops cost. Use for trading / fraud only. |
| Destination | Redshift (warehouse) | OpenSearch (search) | Full-text search + operational analytics. |
| Encryption | KMS default | Integration-specific CMK | Compliance (PCI, HIPAA) — dedicated key for data-in-transit between source/target. |
| Data filter | `include: public.*` | Per-table `include: public.orders, public.customers` | Prod-scope creep; start narrow, broaden after QA. |

---

## 6. Worked example

```python
# tests/test_zero_etl_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.integration_stack import IntegrationStack


def test_synth_integration_and_alarms():
    app = cdk.App()
    stack = IntegrationStack(app, "Zetl-dev", stage="dev")
    tpl = Template.from_stack(stack)

    tpl.has_resource_properties("AWS::RDS::Integration", {
        "IntegrationName": "{project_name}-a2r-dev",
        "DataFilter":      Match.string_like_regexp(".*include: public.*"),
    })

    tpl.has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "IntegrationDataStreamingLagInSeconds",
        "Threshold":  900,
    })


# tests/test_integration_lag.py
"""Integration — after deploy, confirm lag < 15 min for a test INSERT."""
import os, time, boto3, psycopg, pytest


@pytest.mark.integration
def test_replication_lag_under_15_min():
    # 1) Insert a marker row into Aurora.
    aurora = boto3.client("rds-data")
    aurora.execute_statement(
        resourceArn=os.environ["AURORA_CLUSTER_ARN"],
        secretArn=os.environ["AURORA_SECRET_ARN"],
        database="appdb",
        sql=(
            "INSERT INTO public.test_marker (id, ts) "
            "VALUES (gen_random_uuid(), now())"
        ),
    )
    t_insert = time.time()

    # 2) Poll Redshift for the row.
    rs_data = boto3.client("redshift-data")
    wg = os.environ["RS_WORKGROUP"]
    end = time.time() + 900
    seen = False
    while time.time() < end:
        q = rs_data.execute_statement(
            WorkgroupName=wg,
            Database=f"aurora_replica",
            Sql="SELECT count(*) FROM aurora_replica.public.test_marker",
        )
        # poll for result...
        time.sleep(30)
        # ... if count > 0: seen=True, break
    assert seen, "zero-ETL lag > 15 min"
    assert time.time() - t_insert < 900
```

---

## 7. References

- AWS docs — *Zero-ETL integrations overview* (Aurora→Redshift, DDB→Redshift, DDB→OpenSearch).
- AWS docs — *`AWS::RDS::Integration` CFN reference*.
- AWS docs — *Aurora Postgres prerequisites for zero-ETL*.
- AWS docs — *Redshift Serverless `enable_case_sensitive_identifier`*.
- `DATA_AURORA_SERVERLESS_V2.md` — source Aurora configuration.
- `DATA_LAKEHOUSE_ICEBERG.md` — Redshift Spectrum side; can join zero-ETL'd data with lake data.
- `DATA_ATHENA.md` — Athena can query Redshift via federation for combined views.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Aurora→Redshift primary, DDB→Redshift and DDB→OpenSearch secondary. Cluster-parameter-group requirements called out. 8 RPU minimum. Post-activation `CREATE DATABASE FROM INTEGRATION` via custom resource. CloudWatch replication-lag + failed-tables alarms. 10 monolith gotchas, 3 micro-stack gotchas, 11-row swap matrix, pytest synth + integration lag test.
