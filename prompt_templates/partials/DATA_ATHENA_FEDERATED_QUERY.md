# SOP — Athena Federated Query (cross-DB SQL · 30+ connectors · Glue Federation · LF-governed)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Athena engine v3 · Athena Federated Query (Lambda connectors via SAR + Glue Catalog Federation) · 30+ pre-built connectors (RDBMS, NoSQL, SaaS, on-prem) · Lake Formation governance on federated catalogs · cross-account federation

---

## 1. Purpose

- Codify the **two paths** to query data outside Athena's native Glue Catalog from a single Athena SQL statement:
  1. **Lambda connectors via SAR (Serverless Application Repository)** — original Federated Query mechanism. Each source has a Lambda function deployed from SAR; queries route through the Lambda. 30+ connectors available.
  2. **Glue Catalog Federation** (newer, 2024+) — register an external metastore (Hive, Iceberg REST, BigQuery, Snowflake) AS A CATALOG inside Glue Data Catalog. Athena queries it natively without Lambda.
- Provide the **decision tree** between Lambda-connector vs Glue-Federation vs DMS-to-S3 vs zero-ETL.
- Provide the **cross-DB JOIN pattern** — `SELECT a.id, b.name FROM aurora.public.orders a JOIN dynamodb.customers b ON a.customer_id = b.id` — and where it falls down (predicate pushdown, JOIN cost).
- Codify the **Lake Formation governance** layer over federated queries (LF-Tags applied to federated catalog → row/column filters enforced).
- Codify the **cost model** of federated queries (Lambda invocation cost + scan cost + cross-region egress).
- This is the **federated/cross-source query specialisation**. `DATA_ATHENA` (~750 lines) covers the Athena workgroup foundation; this partial deep-dives into the federation layer specifically.

When the SOW signals: "query across multiple databases", "joining Postgres + DynamoDB", "Snowflake from Athena", "BigQuery in our SQL queries", "single SQL across all sources", "federation layer".

---

## 2. Decision tree — federation mechanism

```
Source type?
├── RDBMS (Postgres, MySQL, Oracle, SQL Server, Redshift, Aurora, RDS)
│   ├── Frequent queries (> 100/day)?
│   │   └── YES → DMS or zero-ETL → S3 (NOT federation; persistent copy is cheaper at scale)
│   └── Occasional ad-hoc → §3 Lambda connector (athena-postgres / athena-mysql / athena-oracle / etc.)
├── NoSQL (DynamoDB, DocumentDB, Cassandra/Keyspaces, MongoDB)
│   └── §3 Lambda connector (athena-dynamodb / athena-docdb / athena-cassandra)
├── Analytics warehouse (Snowflake, BigQuery, Vertica, Teradata)
│   ├── Glue Federation supports it? (Snowflake yes via Glue connection)
│   │   ├── YES → §4 Glue Catalog Federation (no Lambda)
│   │   └── NO  → §3 Lambda connector via SAR
├── External APIs (REST endpoints, custom)
│   └── §5 Custom connector (write your own Athena connector framework)
├── On-prem RDBMS via PrivateLink/VPN
│   └── §3 Lambda connector in VPC (athena-jdbc with VPC config)
├── S3 in another account (cross-account)
│   └── NOT federation — use Glue Catalog cross-account share via Lake Formation RAM
└── Iceberg in another data catalog (Tabular / Polaris)
    └── §4 Glue Catalog Federation w/ Iceberg REST
```

### 2.1 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC — connector Lambda deployed from SAR + workgroup + sample query all in one stack | **§3 Monolith Variant** |
| `DataPlaneStack` owns workgroup + Glue resources; `FederationStack` owns connector Lambdas | **§6 Micro-Stack Variant** |

**Why the split.** Federation Lambda functions are deployed via SAR (`sam.CfnApplication`) which CDK doesn't manage in standard L2. Cross-stack, the Athena DataCatalog resource references the Lambda ARN — if the Lambda's stack is destroyed, the DataCatalog is broken. Splitting allows the catalog to outlive Lambda redeploys.

---

## 3. Lambda connector variant (SAR-deployed, 30+ sources)

### 3.1 The full connector matrix (as of 2026-04)

| Source | Connector | SAR ID | Notes |
|---|---|---|---|
| **PostgreSQL / Aurora Postgres** | `athena-postgresql` | AthenaPostgreSQLConnector | JDBC; supports predicate pushdown for WHERE, LIMIT, ORDER BY |
| **MySQL / Aurora MySQL** | `athena-mysql` | AthenaMySQLConnector | JDBC |
| **Oracle** | `athena-oracle` | AthenaOracleConnector | JDBC; requires Oracle JDBC driver layer (license-bound) |
| **SQL Server** | `athena-sqlserver` | AthenaSQLServerConnector | JDBC |
| **DynamoDB** | `athena-dynamodb` | AthenaDynamoDBConnector | Native AWS SDK; pushdown on partition key |
| **DocumentDB / MongoDB** | `athena-docdb` | AthenaDocumentDBConnector | Mongo wire protocol |
| **Redshift** | `athena-redshift` | AthenaRedshiftConnector | JDBC; consider Spectrum direct instead |
| **OpenSearch / Elasticsearch** | `athena-elasticsearch` | AthenaElasticsearchConnector | REST; Lucene query pushdown |
| **Cloudera / Hive (HDFS)** | `athena-hortonworks-hive` | AthenaHortonworksHiveConnector | Thrift |
| **Snowflake** | `athena-snowflake` | AthenaSnowflakeConnector | JDBC (Lambda) OR Glue Federation (newer) |
| **Google BigQuery** | `athena-google-bigquery` | AthenaGoogleBigQueryConnector | gRPC; consider Glue Federation |
| **SAP HANA** | `athena-saphana` | AthenaSAPHANAConnector | JDBC |
| **Vertica** | `athena-vertica` | AthenaVerticaConnector | JDBC |
| **Teradata** | `athena-teradata` | AthenaTeradataConnector | JDBC |
| **Cassandra / Keyspaces** | `athena-cassandra` | AthenaCassandraConnector | CQL |
| **Apache Pinot** | `athena-pinot` | AthenaPinotConnector | REST |
| **Apache Druid** | `athena-druid` | AthenaDruidConnector | Native protocol |
| **HBase** | `athena-hbase` | AthenaHBaseConnector | Thrift |
| **Neo4j** | `athena-neo4j` | AthenaNeo4jConnector | Bolt protocol |
| **Neptune** | `athena-neptune` | AthenaNeptuneConnector | Gremlin / SPARQL |
| **CloudWatch Logs** | `athena-cloudwatch` | AthenaCloudwatchConnector | Native AWS SDK |
| **CloudWatch Metrics** | `athena-cloudwatch-metrics` | AthenaCloudwatchMetricsConnector | Native |
| **MSK / Kafka** | `athena-msk` | AthenaMSKConnector | Confluent SerDes |
| **TPC-DS (synthetic)** | `athena-tpcds` | AthenaTPCDSConnector | For benchmarking |
| **Generic JDBC** | `athena-jdbc` | AthenaJDBCConnector | Catch-all for any JDBC source |
| **Generic REST** | `athena-rest` | AthenaRESTConnector | Custom JSON output |
| **Db2** | `athena-db2` | (SAR community) | JDBC |
| **MariaDB** | `athena-mariadb` | (SAR community) | Like MySQL |
| **InfluxDB** | `athena-timestream` | AthenaTimestreamConnector | for Timestream |

### 3.2 Architecture

```
   Athena query: SELECT ... FROM postgres_catalog.public.orders
                      JOIN dynamodb_catalog.customers ...
        │
        ▼
   ┌────────────────────────────────────────────────┐
   │  Athena engine v3                              │
   │  - parses query                                 │
   │  - identifies external catalogs                  │
   │  - dispatches to connector Lambda(s)             │
   └────────────────────────────────────────────────┘
        │                                  │
        ▼                                  ▼
   ┌─────────────────────┐        ┌──────────────────────┐
   │  Lambda Connector:  │        │  Lambda Connector:    │
   │  athena-postgresql  │        │  athena-dynamodb      │
   │  - JDBC connect     │        │  - AWS SDK            │
   │  - pushdown WHERE   │        │  - PK pushdown        │
   │  - return Arrow flight │     │  - return Arrow       │
   └─────────────────────┘        └──────────────────────┘
            │                              │
            ▼                              ▼
   ┌─────────────────────┐        ┌──────────────────────┐
   │  RDS Postgres 16.4  │        │  DynamoDB             │
   │  (writer endpoint)  │        │  customers table       │
   └─────────────────────┘        └──────────────────────┘

   Spill: large results > 4 MB land in S3 spill bucket (per-connector)
```

### 3.3 CDK — `_register_postgres_federated_catalog()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_athena as athena,
    aws_serverlessrepo as sam,            # for SAR deployment
    aws_secretsmanager as sm,
    aws_ec2 as ec2,
)


def _register_postgres_federated_catalog(self, stage: str) -> None:
    """Monolith. Deploys athena-postgresql connector from SAR, configures the
    Aurora source, and registers a DataCatalog in Athena."""

    # A) Spill bucket — connector writes large intermediate results here
    self.spill_bucket = s3.Bucket(self, "SpillBucket",
        bucket_name=f"{{project_name}}-athena-spill-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="DeleteSpillAfter7Days",
            expiration=Duration.days(7),                # spill is ephemeral
        )],
        removal_policy=RemovalPolicy.DESTROY,
    )

    # B) Source secret (Aurora cred, already in Secrets Manager)
    # Assume self.aurora_secret exists from DataStack

    # C) Deploy connector via SAR — `sam.CfnApplication` references the
    # SAR app ID and we customize parameters per source.
    pg_connector = sam.CfnApplication(self, "PostgresConnector",
        location=sam.CfnApplication.ApplicationLocationProperty(
            application_id=f"arn:aws:serverlessrepo:us-east-1:292517598671:applications/AthenaPostgreSQLConnector",
            semantic_version="2025.30.1",                # update annually
        ),
        parameters={
            "LambdaFunctionName":     f"{{project_name}}-athena-pg-{stage}",
            "DefaultConnectionString": f"postgres://{self.aurora_endpoint}:5432/appdb",
            "SecretNamePrefix":       f"{{project_name}}-aurora-{stage}",
            "SpillBucket":            self.spill_bucket.bucket_name,
            "SpillPrefix":            "athena-pg-spill",
            "LambdaTimeout":          "900",             # seconds, max 15 min
            "LambdaMemory":           "3008",            # MB
            "DisableSpillEncryption": "false",
            "SecurityGroupIds":       self.lambda_sg.security_group_id,
            "SubnetIds":              ",".join([s.subnet_id for s in self.vpc.select_subnets(
                                          subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets]),
        },
    )

    # D) Register the connector Lambda as an Athena DataCatalog
    self.pg_data_catalog = athena.CfnDataCatalog(self, "PgCatalog",
        name=f"{{project_name}}_postgres_{stage}",       # this becomes catalog.<db>.<table>
        type="LAMBDA",
        description="Aurora Postgres federated catalog",
        parameters={
            "function": cdk.Fn.get_att(pg_connector.logical_id, "Outputs.LambdaFunctionArn").to_string(),
        },
    )

    CfnOutput(self, "PgCatalogName", value=self.pg_data_catalog.name)
```

### 3.4 Cross-DB JOIN — sample query

```sql
-- Workgroup: analyst-only (with scan cutoff)
-- Catalogs: aurora_pg + dynamodb (both registered as DataCatalog)
-- Glue Catalog default: 'lakehouse_curated' for Iceberg curated tables

SELECT
  o.order_id,
  o.amount_usd,
  o.created_at,
  c.tier,
  c.lifetime_value,
  i.season
FROM aurora_pg.public.orders AS o
INNER JOIN dynamodb.customers AS c
  ON o.customer_id = c.customer_id
INNER JOIN lakehouse_curated.product_inventory AS i           -- Iceberg native
  ON o.product_id = i.product_id
WHERE o.created_at >= DATE '2026-04-01'                       -- pushed to Aurora
  AND c.tier IN ('platinum', 'gold')                          -- pushed to DDB pk filter
  AND i.season = 'spring'                                     -- pushed to Iceberg partition
LIMIT 1000;
```

**Pushdown semantics matter.** The optimizer pushes `WHERE` predicates back to each source. For Aurora the WHERE becomes a real SQL WHERE; for DynamoDB the connector translates it to a Query (if PK match) or Scan + filter; for Iceberg the predicate prunes partitions. **JOIN happens in Athena's engine** — large unfiltered JOINs cause Lambda timeouts and S3 spill blowup. Always filter source-side first.

### 3.5 Connector pushdown matrix

| Connector | WHERE = | WHERE LIKE | WHERE IN | LIMIT | ORDER BY | aggregate |
|---|---|---|---|---|---|---|
| postgresql | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| mysql | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| oracle | ✅ | ✅ | ✅ | ⚠️ via subquery | ✅ | ✅ |
| dynamodb | ✅ on PK | ❌ | ✅ on PK | ✅ | ❌ | ❌ |
| documentdb | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| redshift | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| elasticsearch | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| snowflake | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| cloudwatch | ✅ on log group | ✅ | ❌ | ✅ | ❌ | ❌ |
| msk | ❌ all-scan | ❌ | ❌ | ✅ | ❌ | ❌ |

A `❌` cell means Athena fetches everything and filters in-engine. For Kafka / CloudWatch this is expensive — only use for occasional ad-hoc.

---

## 4. Glue Catalog Federation variant (newer, no Lambda)

### 4.1 When to use over Lambda connector

- Source is **Snowflake** (best), **BigQuery**, **Iceberg REST** (Tabular / Polaris / SageMaker Lakehouse).
- Source's data appears AS A CATALOG in Glue, queryable from any AWS analytics tool (Athena, Redshift Spectrum, EMR).
- You want **Lake Formation governance** to apply natively (Lake Formation can enforce row/column filters on a federated catalog).

### 4.2 CDK — `_register_glue_catalog_federation_snowflake()`

```python
def _register_glue_catalog_federation_snowflake(self, stage: str) -> None:
    """Glue Catalog Federation to Snowflake. Athena treats it as a native
    Glue catalog — no Lambda connector required."""

    # A) Glue Connection — pointer to Snowflake account
    glue_connection = glue.CfnConnection(self, "SnowflakeConn",
        catalog_id=self.account,
        connection_input=glue.CfnConnection.ConnectionInputProperty(
            name=f"{{project_name}}-snowflake-conn-{stage}",
            description="Federation to Snowflake account",
            connection_type="JDBC",
            physical_connection_requirements=glue.CfnConnection.PhysicalConnectionRequirementsProperty(
                availability_zone="us-east-1a",
                security_group_id_list=[self.glue_sg.security_group_id],
                subnet_id=self.vpc.select_subnets(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets[0].subnet_id,
            ),
            connection_properties={
                "JDBC_CONNECTION_URL": "jdbc:snowflake://acme.snowflakecomputing.com:443/?warehouse=ANALYTICS&db=PROD",
                "USERNAME": "{{resolve:secretsmanager:snowflake-cred:SecretString:user}}",
                "PASSWORD": "{{resolve:secretsmanager:snowflake-cred:SecretString:password}}",
                "JDBC_DRIVER_CLASS_NAME": "net.snowflake.client.jdbc.SnowflakeDriver",
                "JDBC_DRIVER_JAR_URI": f"s3://{self.driver_bucket.bucket_name}/snowflake-jdbc-3.16.0.jar",
            },
        ),
    )

    # B) Glue Catalog Federation entry — the actual federation primitive
    # NOTE: CfnCatalog (federation type) is on aws-cdk-lib v2.155+
    glue_federated_catalog = glue.CfnCatalog(self, "SnowflakeCatalog",
        catalog_id=self.account,
        catalog_input=glue.CfnCatalog.CatalogInputProperty(
            name=f"snowflake_{stage}",
            description="Snowflake federated catalog",
            federated_catalog=glue.CfnCatalog.FederatedCatalogProperty(
                connection_name=glue_connection.ref,
                identifier="ACME_PROD",                  # Snowflake DB
            ),
            create_table_default_permissions=[],         # disables IAMAllowedPrincipals
        ),
    )

    # C) Lake Formation: apply LF-Tags to the federated catalog
    lakeformation.CfnTagAssociation(self, "SnowflakeLFTags",
        resource=lakeformation.CfnTagAssociation.ResourceProperty(
            catalog=lakeformation.CfnTagAssociation.CatalogResourceProperty(),
        ),
        lf_tags=[
            lakeformation.CfnTagAssociation.LFTagPairProperty(
                key="domain", values=["finance"],
                catalog_id=self.account,
            ),
            lakeformation.CfnTagAssociation.LFTagPairProperty(
                key="sensitivity", values=["confidential"],
                catalog_id=self.account,
            ),
        ],
    )
```

After registration, queries against `snowflake_<stage>.<schema>.<table>` work in Athena natively, with LF-TBAC enforcement.

---

## 5. Custom connector (when no SAR exists)

Write your own connector using the Athena Connector Framework (Java SDK). Key contract:

1. **`MetadataHandler`** — Athena calls this to list databases, tables, schemas, partition keys.
2. **`RecordHandler`** — Athena calls this with a partition + WHERE predicate to fetch records, returns Apache Arrow.

Skeleton at [GitHub awslabs/aws-athena-query-federation](https://github.com/awslabs/aws-athena-query-federation). Custom connector adds 1-2 weeks of dev time — only do it if no SAR connector and engagement is large enough.

---

## 6. Micro-Stack variant (cross-stack via SSM)

```python
# In FederationStack
ssm.StringParameter(self, "PgCatalogName",
    parameter_name=f"/{{project_name}}/{stage}/athena/pg-catalog-name",
    string_value=self.pg_data_catalog.name)
ssm.StringParameter(self, "PgConnectorLambdaArn",
    parameter_name=f"/{{project_name}}/{stage}/athena/pg-connector-arn",
    string_value=cdk.Fn.get_att(pg_connector.logical_id, "Outputs.LambdaFunctionArn").to_string())

# In QueryConsumerStack — Athena query Lambda grants itself
catalog_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/athena/pg-catalog-name")
# IAM policy lets the consumer Lambda invoke the connector via Athena
query_lambda.add_to_role_policy(iam.PolicyStatement(
    actions=["athena:StartQueryExecution", "athena:GetQueryExecution",
             "athena:GetQueryResults"],
    resources=[f"arn:aws:athena:{self.region}:{self.account}:workgroup/{workgroup_name}"],
))
```

---

## 7. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| "Connector Lambda invocation failed" | VPC config wrong — Lambda can't reach source | Check Lambda's SG can egress to source IP+port; check source SG ingress allows Lambda's SG |
| Query times out at 30 min | Athena query timeout | Athena workgroup `query_execution_timeout=1800` (30 min max). For longer-running, use Glue ETL job + write to S3 |
| Spill bucket fills 1 TB+ | Large unfiltered JOIN | Add WHERE filter source-side; JOIN in Athena memory only after sources are pre-filtered. Or rewrite as a Glue ETL job |
| "DataCatalog not found" | Connector deployed but DataCatalog not registered | Verify `CfnDataCatalog` resource and `function` parameter points to deployed Lambda ARN |
| Pushdown not happening (full scan) | Connector version old | Update SAR semantic_version; pushdown improvements ship continuously |
| Cross-region query fails with timeout | Source in different region | Federation is single-region; for cross-region, replicate to local region first |
| "AccessDenied" on Glue Catalog Federation | LF-TBAC rejected | Grant tags via `CfnPrincipalPermissions` on the federated catalog resource; `ALL` permissions on `Catalog` |
| Snowflake federation slow | JDBC driver not in S3 | Driver JAR must be in S3, referenced from Glue Connection. For Snowflake, use 3.16.0+ |
| Cost spike on federated query | Lambda invocations + spill scans | Enforce workgroup scan cutoff; review connector default `LambdaMemory` |

### 7.1 Cost model

| Component | Cost |
|---|---|
| Athena scan (Glue Catalog tables) | $5 / TB scanned |
| Federated Lambda invocation | ~$0.20 / million × runtime in seconds |
| Federated Lambda runtime | $0.00001667 / GB-second @ 3008 MB |
| Spill bucket S3 storage | $0.023 / GB / month (delete after 7 days = ~$0) |
| Spill bucket data transfer | $0.01 / GB cross-AZ |
| Glue Connection | Free |
| Glue Federation overhead | None (free) |

Rule of thumb: **federated query is 2-3× the cost of a native Glue Catalog query** at the same scan size, due to Lambda invocations + spill. Use sparingly.

---

## 8. Worked example — pytest synth + boto3 query test

```python
"""SOP verification — FederationStack contains spill bucket, connector
deployment, DataCatalog registration."""
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match


def test_federation_pg_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    from infrastructure.cdk.stacks.federation_stack import FederationStack
    stack = FederationStack(app, stage_name="dev", env=env, ...)
    t = Template.from_stack(stack)

    # Spill bucket w/ 7d lifecycle
    t.has_resource_properties("AWS::S3::Bucket", Match.object_like({
        "BucketEncryption": Match.object_like({
            "ServerSideEncryptionConfiguration": Match.array_with([
                Match.object_like({
                    "ServerSideEncryptionByDefault": Match.object_like({
                        "SSEAlgorithm": "aws:kms",
                    }),
                }),
            ]),
        }),
        "LifecycleConfiguration": Match.object_like({
            "Rules": Match.array_with([
                Match.object_like({"ExpirationInDays": 7}),
            ]),
        }),
    }))
    # SAR application for connector
    t.resource_count_is("AWS::Serverless::Application", 1)
    # DataCatalog registered
    t.has_resource_properties("AWS::Athena::DataCatalog", Match.object_like({
        "Type": "LAMBDA",
    }))


def test_federation_query_runs():
    """Integration test — spin up federation, run a sample query."""
    import boto3
    athena = boto3.client("athena")
    response = athena.start_query_execution(
        QueryString="""
            SELECT COUNT(*) FROM aurora_pg_dev.public.orders
            WHERE created_at >= DATE '2026-04-01'
        """,
        QueryExecutionContext={"Database": "default", "Catalog": "aurora_pg_dev"},
        WorkGroup="analyst-only",
    )
    qid = response["QueryExecutionId"]
    # Poll until SUCCEEDED
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    assert status["State"] == "SUCCEEDED", status.get("StateChangeReason")
```

---

## 9. Five non-negotiables

1. **Always set a workgroup scan cutoff.** Federated queries can blow up costs catastrophically — default workgroup `BytesScannedCutoffPerQuery: 1_000_000_000` (1 GB) for ad-hoc; raise per-workgroup for power users.

2. **Spill bucket lifecycle: 7-day expiration MAX.** Spill is ephemeral; nothing else uses it. Forgetting the lifecycle rule = TB+ of orphaned spill files at $0.023/GB/month.

3. **Connector Lambda IN VPC for any production source.** Source DBs should not be public. Lambda needs to be in the same VPC (or peered) as source. Public-internet connector is a security smell.

4. **Use Glue Catalog Federation for Snowflake / BigQuery if available.** Lambda connectors charge per-invocation + spill; Glue Federation is free overhead. Migrate Lambda-connector → Glue-Federation when a stable Federation provider exists.

5. **Apply LF-Tags at federated-catalog level.** Without LF-Tags, federated tables bypass Lake Formation governance. `CfnTagAssociation` against the catalog applies tags down to all federated tables — required for compliance posture.

---

## 10. References

- `docs/template_params.md` — `FEDERATION_CONNECTOR_VERSION`, `FEDERATION_LAMBDA_MEMORY_MB`, `FEDERATION_LAMBDA_TIMEOUT_SEC`, `FEDERATION_SPILL_RETENTION_DAYS`
- `docs/Feature_Roadmap.md` — `FED-01` (Postgres connector), `FED-02` (DynamoDB connector), `FED-03` (Snowflake Glue Federation), `FED-04` (LF-TBAC on federated)
- AWS docs:
  - [Use Athena Federated Query](https://docs.aws.amazon.com/athena/latest/ug/federated-queries.html)
  - [Available connector types](https://docs.aws.amazon.com/athena/latest/ug/connectors-prebuilt.html)
  - [Glue Catalog Federation](https://docs.aws.amazon.com/lake-formation/latest/dg/federated-catalog-data-connection.html)
  - [Athena + Lake Formation governance](https://docs.aws.amazon.com/athena/latest/ug/security-athena-lake-formation.html)
  - [Custom connector framework](https://github.com/awslabs/aws-athena-query-federation)
- Related SOPs:
  - `DATA_ATHENA` — workgroup foundation, engine v3, Iceberg DML
  - `DATA_LAKE_FORMATION` — LF-TBAC + RAM cross-account
  - `DATA_GLUE_CATALOG` — native catalog (federation extends it)
  - `DATA_DMS_REPLICATION` — alternative to federation for high-frequency / large datasets
  - `LAYER_NETWORKING` — VPC config for connector Lambda to reach source

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — covers BOTH Athena Federation paths: Lambda connectors via SAR (30+ pre-built sources) AND Glue Catalog Federation (newer, no-Lambda). Decision tree for picking. CDK for both variants with Postgres + Snowflake worked examples. Connector pushdown matrix (which sources push WHERE / IN / LIMIT). Cross-DB JOIN sample. Custom connector framework pointer. Cost model (federation = 2-3x native scan cost). LF-TBAC integration. 5 non-negotiables incl. workgroup scan cutoff + spill lifecycle. Pytest + boto3 integration harness. Created to fill F369 audit gap (2026-04-26): "data federation between databases" was scattered between DATA_ATHENA + DATA_LAKE_FORMATION; this consolidates into a focused federation specialisation. |
