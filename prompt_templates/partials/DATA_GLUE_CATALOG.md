# SOP — AWS Glue Data Catalog (databases, crawlers, federation, catalog-as-semantic-layer)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `aws_cdk.aws_glue` L1 + `aws_cdk.aws_glue_alpha` L2 (Python alpha — **churn risk**, we use L1 primarily) · Glue Data Catalog 3.0 · Crawlers (schedule + on-demand) · Glue Catalog federation (S3 Tables, Iceberg REST, Hive Metastore, Redshift, Snowflake, Hudi) · Glue Data Quality (Deequ-based) · Glue Catalog table-level tagging (for AI discovery prep)

---

## 1. Purpose

- Provide the deep-dive for **AWS Glue Data Catalog** as the **metadata plane** for the lake — databases, tables, partitions, columns, connections, and the federated catalogs that let one Athena/EMR/Redshift query reach S3 Tables, self-managed Iceberg, another Glue catalog in another account, a Hive Metastore on EMR, a Snowflake warehouse, or a Redshift cluster.
- Codify the **database/table/partition structure** (Glue's native model) and its mapping to Iceberg/Hive/open-format tables. Every table has a **`TableType`** (`EXTERNAL_TABLE`, `MANAGED_TABLE`, `ICEBERG`, `VIRTUAL_VIEW`) and a **`StorageDescriptor`** (columns, location, SerDe, input/output format). Every column has a name, type, comment.
- Codify **crawlers** — on-demand and scheduled. Crawlers infer schema from S3 prefixes, DynamoDB tables, JDBC sources, or Delta/Iceberg manifests, then write/update Glue tables. They are **stateless** (each run rescans); schedule lightly. Crawlers use classifiers (built-in or custom grok) to detect format.
- Codify **catalog federation** — `CfnCatalog` (introduced 2024) registers an external catalog as a federated source. S3 Tables auto-registers as `s3tablescatalog/<bucket>`; external Iceberg REST / Hive / Snowflake / Redshift require an explicit `CfnCatalog` + `CfnConnection` pair.
- Codify **table-level tags via `glue:TagResource`** — separate from Lake Formation LF-Tags. Glue resource tags are IAM/billing tags; LF-Tags are access-control semantics. Use Glue tags for "owner=finance-team", "cost-center=123"; use LF-Tags (see `DATA_LAKE_FORMATION.md`) for "domain=finance, sensitivity=pii".
- Codify **the catalog as a SEMANTIC LAYER for AI** — table `Description`, column `Comment`, table `Parameters` (arbitrary KV) are the substrate that Wave 2's `PATTERN_CATALOG_EMBEDDINGS` partial will vectorize. **Populate them seriously** — `Comment="customer_id — UUIDv4 identifying the billing entity; join to dim_customer.customer_id"` is the embedding text; empty comments produce garbage vectors.
- Codify the **Glue Data Quality** hookup — `CfnDataQualityRuleset` (DQDL: Declarative Data Quality Language) + ruleset evaluation job. Emits EventBridge events on pass/fail; pair with `EVENT_DRIVEN_FAN_IN_AGGREGATOR` for fan-in to a quality dashboard.
- Include when the SOW signals: "Glue catalog", "schema discovery", "crawlers", "data federation", "catalog federation", "cross-account catalog", "Hive Metastore migration", "Iceberg catalog", "data dictionary", "catalog-as-code", "AI-ready metadata", "Bedrock over catalog".
- This partial is the **metadata layer** — the WHAT-DATA-DO-WE-HAVE contract. It is consumed by `DATA_ATHENA` (query), `DATA_LAKE_FORMATION` (govern), `PATTERN_TEXT_TO_SQL` (Wave 3 — schema-aware prompt), `PATTERN_CATALOG_EMBEDDINGS` (Wave 2 — vectorize metadata for discovery).

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC / single-domain lake — one `cdk.Stack` owns the Glue database + tables + crawlers + connections + data-quality rulesets | **§3 Monolith Variant** |
| `CatalogStack` owns databases + crawlers + connections + DQ rulesets; `ComputeStack` writes/reads via table-name + catalog-name from SSM; `GovernanceStack` (see `DATA_LAKE_FORMATION`) layers LF-Tags on top | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Glue databases are account + region global.** Creating the same database name from two stacks fails with "AlreadyExistsException" on the second deploy. Owner: Catalog stack, one place.
2. **`CfnTable` is stateful in a non-obvious way.** Updates to `StorageDescriptor.columns` require a CFN replacement for some formats (Hive) and in-place update for others (Iceberg). Switching `TableType` from `EXTERNAL_TABLE` to `ICEBERG` mid-life is a replace — downstream Athena partitions and LF grants are dropped. Plan the TableType at first deploy.
3. **Crawlers need an IAM role, a target, and a schedule.** The role needs `s3:GetObject` on the data paths, `glue:*` on the target database, `kms:Decrypt` on any CMK. If the crawler is in `CatalogStack` but the S3 data is in `LakehouseStack`, the role is in `CatalogStack`; grants are identity-side + the BUCKET POLICY must also allow the crawler role. If the bucket is in another stack and you append the crawler role ARN to its policy, you create the classic bidirectional cycle. Break with `CfnBucketPolicy` in Lakehouse referencing the crawler role ARN as a **string** (from SSM).
4. **Federated catalogs (`CfnCatalog`) are first-class citizens.** They live in the Glue service but are referenced from Athena as `<catalog-id>:<database>:<table>`. Cross-stack: `CatalogStack` owns the federation, publishes catalog IDs via SSM, `ComputeStack` reads them.
5. **Glue connections (`CfnConnection`) encode secrets**. JDBC connections take a user+password, often from Secrets Manager. Cross-stack: secret ARN via SSM, connection in `CatalogStack`. Do NOT pass the raw Secret construct across stacks.

Micro-Stack fixes all of this by: (a) owning databases + tables + crawlers + connections + DQ rulesets + catalog federations in `CatalogStack`; (b) publishing `DatabaseName`, per-table `TableName`/`TableArn`, per-catalog `CatalogId`, per-crawler `CrawlerName`, per-connection `ConnectionName` via SSM; (c) consumers read via `value_for_string_parameter` and grant themselves identity-side.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Glue Data Catalog (account+region)
  │
  ├── Catalog: default (this account)                        ← AwsDataCatalog in Athena
  │   └── Database: lakehouse
  │       ├── Table: fact_revenue      (ICEBERG, location=s3://...)
  │       │     columns=[order_id:long, customer_id:string,
  │       │              ts:timestamp, amount:decimal(18,2)]
  │       │     description="Financial transactions — source of truth for revenue."
  │       │     parameters={"classification": "iceberg",
  │       │                 "metadata_location": "s3://...", ...}
  │       │
  │       ├── Table: dim_customer      (ICEBERG)
  │       ├── Table: stg_event         (EXTERNAL_TABLE, Parquet)
  │       └── View : v_active_revenue  (VIRTUAL_VIEW, stores SQL text)
  │
  ├── Catalog: s3tablescatalog/<bucket>   ← federated, auto-registered
  │   └── (mirrors S3 Tables namespaces)
  │
  └── Catalog: ext_snowflake_prod         ← federated via CfnCatalog + CfnConnection
      └── Database: SALES
          └── Table: customers

  Crawlers:
    ├── stg_event_crawler   (schedule cron, target s3://...stg-events/,
    │                        writes to lakehouse.stg_event)
    └── snowflake_crawler   (target CONNECTION=snowflake_conn, DB=SALES)

  Connections:
    └── snowflake_conn      (JDBC, secret=arn:aws:secretsmanager:...)

  Data Quality Rulesets:
    └── dq_fact_revenue     (DQDL: "Rules = [ ColumnCount = 5, ... ]")
```

### 3.2 CDK — `_create_glue_catalog()` method body

```python
from aws_cdk import (
    CfnOutput, RemovalPolicy, Stack,
    aws_events as events, aws_events_targets as targets,
    aws_glue as glue,                    # L1 — primary
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_secretsmanager as sm,
)


def _create_glue_catalog(self, stage: str) -> None:
    """Monolith variant. Assumes self.{lake_bucket, selfmanaged_cmk,
    snowflake_secret_arn} already exist (or create below)."""

    # A) Glue database.
    #    - Catalog ID is the AWS account (default catalog).
    #    - `name` is lowercase, 1-255 chars, [a-z0-9_]
    #    - Description is user-facing (shown in Athena, Glue console, and
    #      consumed by Wave 2's catalog-embeddings partial — write it well).
    self.glue_db = glue.CfnDatabase(
        self, "LakehouseDb",
        catalog_id=Stack.of(self).account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(
            name=f"lakehouse_{stage}",
            description=(
                "Finance + customer analytics lakehouse — source of truth for "
                "quarterly reporting, LTV, and renewal ops. Owner: finance-data-team."
            ),
            location_uri=f"s3://{self.lake_bucket.bucket_name}/lakehouse_{stage}/",
            # Tags visible to IAM / billing; LF-Tags are elsewhere (see
            # DATA_LAKE_FORMATION).
            parameters={
                "owner":       "finance-data-team",
                "cost_center": "CC-123",
                "domain":      "finance",             # for catalog-embeddings discovery
            },
        ),
    )

    # B) Iceberg table fact_revenue — declared as TableType=ICEBERG.
    #    Storage descriptor is minimal for Iceberg: input_format, output_format,
    #    and serde are set by Glue when TableType=ICEBERG, NOT by us. Only
    #    columns, description, parameters matter.
    self.tbl_fact_revenue = glue.CfnTable(
        self, "TblFactRevenue",
        catalog_id=Stack.of(self).account,
        database_name=self.glue_db.ref,       # Ref returns the db name
        table_input=glue.CfnTable.TableInputProperty(
            name="fact_revenue",
            description=(
                "Financial transactions — one row per settled order. "
                "Immutable after 48h. Joined via customer_id to dim_customer."
            ),
            table_type="EXTERNAL_TABLE",       # or ICEBERG; EXTERNAL for self-managed
            parameters={
                "classification":    "iceberg",
                "table_type":        "ICEBERG",
                "owner":             "finance-data-team",
                "metadata_location": f"s3://{self.lake_bucket.bucket_name}/iceberg/fact_revenue/metadata/v1.metadata.json",
            },
            storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                location=f"s3://{self.lake_bucket.bucket_name}/iceberg/fact_revenue/",
                # Column comments are the embedding substrate — write them!
                columns=[
                    glue.CfnTable.ColumnProperty(
                        name="order_id",     type="bigint",
                        comment="Monotonically increasing order identifier (PK).",
                    ),
                    glue.CfnTable.ColumnProperty(
                        name="customer_id",  type="string",
                        comment="FK to dim_customer.customer_id — UUIDv4 billing entity.",
                    ),
                    glue.CfnTable.ColumnProperty(
                        name="ts",           type="timestamp",
                        comment="Order settlement UTC timestamp — partition key.",
                    ),
                    glue.CfnTable.ColumnProperty(
                        name="amount",       type="decimal(18,2)",
                        comment="Order total in 'currency'. Use SUM(amount)/1 for dollars.",
                    ),
                    glue.CfnTable.ColumnProperty(
                        name="currency",     type="string",
                        comment="ISO 4217 3-letter currency code (USD, EUR, ...).",
                    ),
                ],
                # Iceberg drives these via metadata; Glue ignores but CFN
                # requires the keys. Use the Iceberg defaults.
                input_format=(
                    "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
                ),
                output_format=(
                    "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
                ),
                serde_info=glue.CfnTable.SerdeInfoProperty(
                    serialization_library=(
                        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                    ),
                ),
            ),
        ),
    )
    self.tbl_fact_revenue.add_dependency(self.glue_db)

    # C) Crawler for stg_event (Parquet, schema-inferred).
    crawler_role = iam.Role(
        self, "StgEventCrawlerRole",
        assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
        ],
    )
    # Identity-side grants on the data prefix.
    crawler_role.add_to_policy(iam.PolicyStatement(
        actions=["s3:GetObject", "s3:ListBucket"],
        resources=[
            self.lake_bucket.bucket_arn,
            f"{self.lake_bucket.bucket_arn}/stg-events/*",
        ],
    ))
    crawler_role.add_to_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.selfmanaged_cmk.key_arn],
    ))
    self.crawler_stg_event = glue.CfnCrawler(
        self, "StgEventCrawler",
        name=f"{{project_name}}-stg-event-{stage}",
        role=crawler_role.role_arn,
        database_name=self.glue_db.ref,
        targets=glue.CfnCrawler.TargetsProperty(
            s3_targets=[glue.CfnCrawler.S3TargetProperty(
                path=f"s3://{self.lake_bucket.bucket_name}/stg-events/",
                sample_size=20,           # sample 20 files for schema inference
            )],
        ),
        schedule=glue.CfnCrawler.ScheduleProperty(
            schedule_expression="cron(0 2 * * ? *)",   # 02:00 UTC daily
        ),
        recrawl_policy=glue.CfnCrawler.RecrawlPolicyProperty(
            recrawl_behavior="CRAWL_NEW_FOLDERS_ONLY",
        ),
        schema_change_policy=glue.CfnCrawler.SchemaChangePolicyProperty(
            update_behavior="UPDATE_IN_DATABASE",
            delete_behavior="LOG",
        ),
        configuration=(
            '{"Version":1.0,"CrawlerOutput":{"Partitions":{"AddOrUpdateBehavior":'
            '"InheritFromTable"}}}'
        ),
    )
    self.crawler_stg_event.add_dependency(self.glue_db)

    # D) JDBC connection to Snowflake (federated source).
    self.conn_snowflake = glue.CfnConnection(
        self, "SnowflakeConnection",
        catalog_id=Stack.of(self).account,
        connection_input=glue.CfnConnection.ConnectionInputProperty(
            name=f"snowflake_{stage}",
            connection_type="JDBC",
            connection_properties={
                "JDBC_CONNECTION_URL": "jdbc:snowflake://xy12345.snowflakecomputing.com/?db=SALES",
                "SECRET_ID":           self.snowflake_secret_arn,
                "JDBC_ENFORCE_SSL":    "true",
            },
            physical_connection_requirements=glue.CfnConnection.PhysicalConnectionRequirementsProperty(
                availability_zone=f"{Stack.of(self).region}a",
                subnet_id=self.private_subnets[0].subnet_id,
                security_group_id_list=[self.glue_sg.security_group_id],
            ),
        ),
    )

    # E) Federated catalog to a Snowflake warehouse (Athena-queryable).
    #    CfnCatalog registers Snowflake; queries reach it as
    #      <catalog-name>.SALES.CUSTOMERS
    #    in Athena.
    self.fed_snowflake = glue.CfnCatalog(
        self, "FedSnowflake",
        catalog_id=Stack.of(self).account,
        name=f"ext_snowflake_{stage}",
        catalog_input=glue.CfnCatalog.CatalogInputProperty(
            federated_catalog=glue.CfnCatalog.FederatedCatalogProperty(
                connection_name=self.conn_snowflake.ref,
                identifier=f"SALES@snowflake_{stage}",
            ),
        ),
    )
    self.fed_snowflake.add_dependency(self.conn_snowflake)

    # F) Glue Data Quality ruleset for fact_revenue.
    #    DQDL is Glue's rule language. Rules fire in an eval job (not shown
    #    here — see §3.4 and EVENT_DRIVEN_FAN_IN_AGGREGATOR for downstream).
    self.dq_fact_revenue = glue.CfnDataQualityRuleset(
        self, "DqFactRevenue",
        name=f"{{project_name}}-dq-fact-revenue-{stage}",
        description="Fact-revenue data quality — column count, non-null, amount sanity.",
        ruleset=(
            "Rules = ["
            "   ColumnCount = 5,"
            "   ColumnValues \"amount\" > 0,"
            "   Completeness \"customer_id\" > 0.99,"
            "   ColumnDataType \"ts\" = \"TIMESTAMP\""
            "]"
        ),
        target_table=glue.CfnDataQualityRuleset.DataQualityTargetTableProperty(
            database_name=self.glue_db.ref,
            table_name="fact_revenue",
        ),
    )

    # G) Outputs — cross-stack contract if catalog splits out.
    CfnOutput(self, "GlueDatabaseName",  value=self.glue_db.ref)
    CfnOutput(self, "FactRevenueTable",  value=self.tbl_fact_revenue.ref)
    CfnOutput(self, "SnowflakeConn",     value=self.conn_snowflake.ref)
    CfnOutput(self, "FedSnowflakeCat",   value=self.fed_snowflake.ref)
```

### 3.3 On-demand crawler invocation

```python
# lambda/kick_crawler/handler.py
"""Trigger a crawler on-demand (from an S3 event, an EventBridge schedule,
or an API call)."""
import os, boto3

CRAWLER_NAME = os.environ["CRAWLER_NAME"]
glue_client = boto3.client("glue")

def lambda_handler(event, _ctx):
    # Idempotent: if the crawler is already RUNNING, StartCrawler raises
    # CrawlerRunningException. Catch + ignore — the existing run will cover it.
    try:
        glue_client.start_crawler(Name=CRAWLER_NAME)
        return {"started": True, "crawler": CRAWLER_NAME}
    except glue_client.exceptions.CrawlerRunningException:
        return {"started": False, "reason": "already-running"}
```

### 3.4 Data quality evaluation job

```python
# Glue job bootstrapped via aws_glue.CfnJob (not shown full) runs this
# PySpark snippet against the target table. Emits CloudWatch metrics +
# EventBridge events on pass/fail.
"""
from awsglue.context import GlueContext
from pyspark.context import SparkContext
from awsgluedq.transforms import EvaluateDataQuality

sc  = SparkContext.getOrCreate()
gc  = GlueContext(sc)
df  = gc.create_dynamic_frame.from_catalog(
    database="lakehouse_prod", table_name="fact_revenue",
)
result = EvaluateDataQuality.apply(
    frame=df,
    ruleset="""Rules = [ ColumnCount = 5, ColumnValues "amount" > 0 ]""",
    publishing_options={
        "dataQualityEvaluationContext": "fact_revenue_dq",
        "enableDataQualityCloudWatchMetrics": True,
        "enableDataQualityResultsPublishing": True,
        "resultsS3Prefix": "s3://.../dq-results/",
    },
)
result.toDF().show()
"""
```

### 3.5 Monolith gotchas

1. **`glue.CfnDatabase.ref` returns the database NAME, not an ARN.** Most other Cfn resources return ARNs or IDs. If you need the ARN (for IAM `resources=[...]`), build it with `f"arn:aws:glue:{region}:{account}:database/{db_name}"`.
2. **Table `StorageDescriptor` is required even for Iceberg TableType.** CFN refuses to create a `CfnTable` without input_format/output_format/serde, even though Iceberg ignores them. Use the Parquet Hive defaults shown in §3.2.
3. **Column comments are silently length-capped at 255 chars.** Glue Data Catalog stores `comment` as a shortString. Longer comments truncate — a problem if you use them as the AI embedding substrate. For longer descriptions, put the long form in `Parameters` (KV store, 4 KB per value).
4. **Crawler `RecrawlPolicy` defaults to `CRAWL_EVERYTHING`.** For append-only S3 paths (time-partitioned logs, event streams), switch to `CRAWL_NEW_FOLDERS_ONLY` — scans are orders of magnitude faster.
5. **`CfnConnection.physical_connection_requirements` must point at a subnet with a Glue ENI.** Crawlers and jobs launch ENIs in the specified subnet. Subnet must have NAT or VPC endpoints for Glue, STS, KMS, S3, and the JDBC target's endpoint.
6. **`CfnCatalog` federated is v2024+ only.** CDK versions earlier than v2.150 do not have the construct; synth silently ignores without emitting it. Pin v2.238+.
7. **Glue Catalog resource tags are NOT LF-Tags.** They are IAM + billing tags set via `glue:TagResource` or the CDK `Tags.of(...).add(...)` pattern on Cfn resources. Grants against tags use `aws:ResourceTag/owner=finance-team` IAM conditions — orthogonal to LF.
8. **`CfnDataQualityRuleset.ruleset` is a multi-line DQDL string.** Escape double quotes (`"amount"` → `\"amount\"`) if you inline it in Python. Better: read from a `.dqdl` file via `Path(__file__).parent / "dqdl" / "fact_revenue.dqdl"`.

---

## 4. Micro-Stack Variant

**Use when:** multiple stacks (producer domains + consumers + governance) all reference the same Glue Catalog.

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** — on any crawler-kick / dq-eval Lambda `entry`.
2. **Identity-side grants** — consumers grant themselves `glue:GetDatabase`/`GetTable`/`GetPartitions` on SSM-read resource ARNs; NEVER mutate a Glue resource policy from a consumer stack.
3. **`CfnRule` cross-stack EventBridge** — DQ pass/fail events emit to EB; the rule lives in CatalogStack, target Lambda ARN is a string from SSM.
4. **Same-stack bucket + OAC** — N/A unless serving catalog metadata via CloudFront.
5. **KMS ARNs as strings** — the lake bucket's CMK ARN is SSM-published by its owner; CatalogStack reads it via `value_for_string_parameter` to grant the crawler role `kms:Decrypt`.

### 4.2 CatalogStack — owns databases, tables, crawlers, DQ

```python
# stacks/catalog_stack.py
from pathlib import Path
from aws_cdk import (
    CfnOutput, Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_glue as glue,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct


class CatalogStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) Resolve lake bucket contract via SSM.
        lake_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lake/bucket_name"
        )
        lake_cmk_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lake/cmk_arn"
        )

        # B) Database.
        db = glue.CfnDatabase(
            self, "LakehouseDb",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=f"lakehouse_{stage}",
                description="Finance analytics lakehouse — quarterly reporting + LTV.",
                location_uri=f"s3://{lake_bucket_name}/lakehouse_{stage}/",
                parameters={
                    "owner":       "finance-data-team",
                    "cost_center": "CC-123",
                    "domain":      "finance",
                },
            ),
        )

        # C) Table — well-commented columns for AI embedding prep.
        tbl = glue.CfnTable(
            self, "TblFactRevenue",
            catalog_id=self.account,
            database_name=db.ref,
            table_input=glue.CfnTable.TableInputProperty(
                name="fact_revenue",
                description=(
                    "Financial transactions — one row per settled order. "
                    "Immutable after 48h."
                ),
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification":    "iceberg",
                    "table_type":        "ICEBERG",
                    "metadata_location": f"s3://{lake_bucket_name}/iceberg/fact_revenue/metadata/v1.metadata.json",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{lake_bucket_name}/iceberg/fact_revenue/",
                    columns=[
                        glue.CfnTable.ColumnProperty(
                            name="order_id", type="bigint",
                            comment="Order identifier (PK).",
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="customer_id", type="string",
                            comment="FK to dim_customer.customer_id — billing entity UUID.",
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="ts", type="timestamp",
                            comment="Settlement UTC timestamp.",
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="amount", type="decimal(18,2)",
                            comment="Order total in 'currency'.",
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="currency", type="string",
                            comment="ISO 4217 3-letter code.",
                        ),
                    ],
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                    ),
                ),
            ),
        )
        tbl.add_dependency(db)

        # D) Crawler role — identity-side grants against the lake bucket.
        #    Bucket ARN constructed from SSM-read name (a string/token).
        crawler_role = iam.Role(
            self, "StgEventCrawlerRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
            ],
        )
        crawler_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[
                f"arn:aws:s3:::{lake_bucket_name}",
                f"arn:aws:s3:::{lake_bucket_name}/stg-events/*",
            ],
        ))
        crawler_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[lake_cmk_arn],
        ))
        crawler = glue.CfnCrawler(
            self, "StgEventCrawler",
            name=f"{{project_name}}-stg-event-{stage}",
            role=crawler_role.role_arn,
            database_name=db.ref,
            targets=glue.CfnCrawler.TargetsProperty(
                s3_targets=[glue.CfnCrawler.S3TargetProperty(
                    path=f"s3://{lake_bucket_name}/stg-events/",
                    sample_size=20,
                )],
            ),
            schedule=glue.CfnCrawler.ScheduleProperty(
                schedule_expression="cron(0 2 * * ? *)",
            ),
            recrawl_policy=glue.CfnCrawler.RecrawlPolicyProperty(
                recrawl_behavior="CRAWL_NEW_FOLDERS_ONLY",
            ),
            schema_change_policy=glue.CfnCrawler.SchemaChangePolicyProperty(
                update_behavior="UPDATE_IN_DATABASE",
                delete_behavior="LOG",
            ),
        )
        crawler.add_dependency(db)

        # E) DQ ruleset + EB rule on pass/fail → target Lambda in
        #    AlertingStack (ARN via SSM, not L2 construct).
        dq = glue.CfnDataQualityRuleset(
            self, "DqFactRevenue",
            name=f"{{project_name}}-dq-fact-revenue-{stage}",
            description="Column count + completeness + value sanity.",
            ruleset=(
                'Rules = ['
                '   ColumnCount = 5,'
                '   ColumnValues "amount" > 0,'
                '   Completeness "customer_id" > 0.99'
                ']'
            ),
            target_table=glue.CfnDataQualityRuleset.DataQualityTargetTableProperty(
                database_name=db.ref,
                table_name="fact_revenue",
            ),
        )

        alert_lambda_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/alerting/dq_handler_arn"
        )
        events.CfnRule(
            self, "DqResultRule",
            event_bus_name="default",
            name=f"{{project_name}}-dq-result-{stage}",
            description="Fire on Glue DQ evaluation results.",
            event_pattern={
                "source": ["aws.glue-dataquality"],
                "detail-type": ["Data Quality Evaluation Results Available"],
                "detail": {
                    "rulesetNames": [dq.ref],
                },
            },
            targets=[events.CfnRule.TargetProperty(
                id="DqHandler",
                arn=alert_lambda_arn,
            )],
        )

        # F) Publish cross-stack contract.
        ssm.StringParameter(
            self, "DatabaseNameParam",
            parameter_name=f"/{{project_name}}/{stage}/catalog/database_name",
            string_value=db.ref,
        )
        ssm.StringParameter(
            self, "FactRevenueTableParam",
            parameter_name=f"/{{project_name}}/{stage}/catalog/fact_revenue_table",
            string_value=tbl.ref,
        )
        ssm.StringParameter(
            self, "StgEventCrawlerName",
            parameter_name=f"/{{project_name}}/{stage}/catalog/stg_event_crawler",
            string_value=crawler.ref,
        )

        CfnOutput(self, "GlueDatabaseName", value=db.ref)
        CfnOutput(self, "FactRevenueTable", value=tbl.ref)
        CfnOutput(self, "StgEventCrawler",  value=crawler.ref)
```

### 4.3 Consumer pattern — Lambda that reads Glue catalog

```python
# stacks/compute_stack.py — a reader lambda.
from pathlib import Path
from aws_cdk import (
    Duration, Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction


class ComputeStack(Stack):
    def __init__(self, scope, construct_id, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) Resolve cross-stack catalog contract via SSM.
        db_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog/database_name"
        )
        fact_revenue_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/catalog/fact_revenue_table"
        )

        # B) Reader Lambda.
        reader = PythonFunction(
            self, "CatalogReaderFn",
            entry=str(Path(__file__).parent.parent / "lambda" / "catalog_reader"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(2),
            environment={
                "DATABASE_NAME": db_name,
                "TABLE_NAME":    fact_revenue_name,
            },
        )

        # C) Identity-side grants — Glue catalog reads. ARNs are built from
        #    the SSM-read tokens via f-strings; CDK resolves at deploy time.
        reader.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "glue:GetDatabase", "glue:GetDatabases",
                "glue:GetTable",    "glue:GetTables",
                "glue:GetPartitions",
            ],
            resources=[
                f"arn:aws:glue:{self.region}:{self.account}:catalog",
                f"arn:aws:glue:{self.region}:{self.account}:database/{db_name}",
                f"arn:aws:glue:{self.region}:{self.account}:table/{db_name}/*",
            ],
        ))
```

### 4.4 Micro-stack gotchas

- **Tokens in ARN f-strings are OK; tokens as dict KEYS are NOT.** `{db_name: "something"}` fails — tokens are not hashable. Always use tokens as values.
- **The auto-federated S3 Tables catalog (`s3tablescatalog/<bucket>`) does NOT need a `CfnCatalog`.** It appears automatically after the table bucket is created. For grants against it, construct the Glue ARN as `arn:aws:glue:<region>:<account>:database/s3tablescatalog/<bucket>/<namespace>`.
- **DQ event fan-in:** multiple rulesets evaluating the same table emit multiple events. Use `EVENT_DRIVEN_FAN_IN_AGGREGATOR` to collate into one per-table DQ summary before alerting.
- **Cross-account Glue Catalog** (a consumer account queries the producer account's catalog) requires EITHER LF cross-account sharing (preferred, see `DATA_LAKE_FORMATION`) OR the older `CfnDatabase.resource_policy`. Do NOT mix — LF will override the resource policy in ways that are hard to reason about.
- **Deletion order:** CatalogStack → ComputeStack (deploy); ComputeStack → CatalogStack (delete). If you delete CatalogStack first, the SSM params disappear and ComputeStack's next action fails.

---

## 5. Swap matrix — when to replace or supplement

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Catalog | Glue Data Catalog (this) | Iceberg REST catalog (Tabular / Polaris) | Multi-cloud / Databricks portability. Loses Lake Formation integration and auto-federation with S3 Tables. |
| Schema inference | Glue crawler | Manual `CfnTable` in CDK | Deterministic schema, no drift from crawler guess. Required for Iceberg; crawlers are weak on Iceberg manifest traversal. |
| Crawler source | S3 | JDBC connection + crawler | On-prem / external DB source. Slower; crawler runs in Glue VPC endpoint. |
| Federation target | `CfnCatalog` + Snowflake | Athena Query Federation (Lambda connector) | Older pattern — Athena-specific; loses the "visible from EMR / Redshift / Bedrock KB" benefit of `CfnCatalog`. Avoid for new work. |
| Table-level semantics | Glue `description` + column `comment` + `parameters` | External data dictionary (Collibra, Alation) | Enterprise governance with workflow + stewardship. Pair with Glue, don't replace — Glue remains the technical truth. |
| Data quality | Glue Data Quality (Deequ) | Great Expectations in Glue job | GE is more flexible (Python rules), DQ is faster to wire (DQDL + no external dep). Prefer DQ unless existing GE assets. |
| Cross-account catalog | LF Gen-3 cross-account | Catalog-resource-policy via `CfnDatabase.resource_policy` | Legacy pattern; use LF for new work, resource-policy only for pre-LF codebases. |
| Partition management | Crawler `CRAWL_NEW_FOLDERS_ONLY` | Partition-indexing + `glue:BatchCreatePartition` via Lambda | Latency — crawlers are minutes; direct API is seconds. Use when SLA < crawler schedule. |

---

## 6. Worked example — offline synth + crawler kick

```python
# tests/test_catalog_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.catalog_stack import CatalogStack


def test_synth_catalog_has_database_table_crawler_dq():
    app = cdk.App()
    stack = CatalogStack(app, "Cat-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # Database named + described.
    tpl.has_resource_properties("AWS::Glue::Database", {
        "DatabaseInput": Match.object_like({
            "Name":        "lakehouse_dev",
            "Description": Match.string_like_regexp(".*Finance.*"),
            "Parameters":  Match.object_like({"domain": "finance"}),
        }),
    })

    # Table has Iceberg parameters + commented columns.
    tpl.has_resource_properties("AWS::Glue::Table", {
        "TableInput": Match.object_like({
            "Name":      "fact_revenue",
            "Parameters": Match.object_like({"table_type": "ICEBERG"}),
            "StorageDescriptor": Match.object_like({
                "Columns": Match.array_with([
                    Match.object_like({
                        "Name": "customer_id", "Type": "string",
                        "Comment": Match.string_like_regexp(".*dim_customer.*"),
                    }),
                ]),
            }),
        }),
    })

    # Crawler with CRAWL_NEW_FOLDERS_ONLY.
    tpl.has_resource_properties("AWS::Glue::Crawler", {
        "Name":           "{project_name}-stg-event-dev",
        "RecrawlPolicy":  {"RecrawlBehavior": "CRAWL_NEW_FOLDERS_ONLY"},
        "Schedule":       {"ScheduleExpression": "cron(0 2 * * ? *)"},
    })

    # DQ ruleset.
    tpl.has_resource_properties("AWS::Glue::DataQualityRuleset", {
        "Name":    "{project_name}-dq-fact-revenue-dev",
        "Ruleset": Match.string_like_regexp(".*ColumnCount = 5.*"),
    })

    # EB rule for DQ events.
    tpl.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": Match.object_like({
            "source":      ["aws.glue-dataquality"],
            "detail-type": ["Data Quality Evaluation Results Available"],
        }),
    })


# lambda/catalog_reader/handler.py — runtime sanity check.
"""Get table description + columns; useful in §4.3 consumer pattern."""
import os, boto3
glue_client = boto3.client("glue")

def lambda_handler(event, _ctx):
    db  = os.environ["DATABASE_NAME"]
    tbl = os.environ["TABLE_NAME"]
    resp = glue_client.get_table(DatabaseName=db, Name=tbl)
    tbl_info = resp["Table"]
    return {
        "description": tbl_info.get("Description"),
        "columns": [
            {"name": c["Name"], "type": c["Type"], "comment": c.get("Comment")}
            for c in tbl_info["StorageDescriptor"]["Columns"]
        ],
        "parameters": tbl_info.get("Parameters", {}),
    }
```

---

## 7. References

- AWS docs — *AWS Glue Data Catalog developer guide* (databases, tables, crawlers, federation).
- AWS docs — *Glue Catalog Federation via `CfnCatalog`* (Snowflake, Redshift, Hive Metastore, Iceberg REST).
- AWS docs — *Glue Data Quality (DQDL reference)*.
- `DATA_ICEBERG_S3_TABLES.md` — auto-federated `s3tablescatalog/<bucket>` sibling catalog.
- `DATA_LAKEHOUSE_ICEBERG.md` — self-managed Iceberg on plain S3; same Glue catalog fronts it.
- `DATA_LAKE_FORMATION.md` — governance layer (LF-Tags on Glue resources).
- `DATA_ATHENA.md` — query engine against the catalog; `AwsDataCatalog` + federated catalogs.
- `PATTERN_CATALOG_EMBEDDINGS.md` (Wave 2) — vectorizes `description` + `columns[*].comment` + `parameters` for semantic discovery.
- `PATTERN_TEXT_TO_SQL.md` (Wave 3) — consumes this catalog as the schema prompt for Bedrock.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables (SSM string-ARN pattern echoed).

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Catalog-as-semantic-layer emphasis (column comments, description, parameters) explicitly pre-positioned for Wave 2 catalog embeddings and Wave 3 text-to-SQL. Federation via `CfnCatalog`. DQ ruleset + EB fan-in. 8 monolith gotchas, 5 micro-stack gotchas, 8-row swap matrix.
