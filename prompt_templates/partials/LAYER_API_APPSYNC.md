# SOP — AppSync GraphQL API + Real-Time Subscriptions

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS AppSync GraphQL

---

## 1. Purpose

GraphQL API with real-time subscriptions (WebSocket transport), Lambda/DynamoDB/RDS resolvers, Cognito/API Key authorization. Use as alternative to `LAYER_API` (REST) when frontend needs field-selective queries or real-time data.

Include when SOW mentions: GraphQL, real-time, live feed, mobile clients, complex data fetching, WebSocket push.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| AppSync + resolvers + data sources in one stack | **§3 Monolith Variant** |
| AppSync in `ApiStack`, Lambda resolvers in `ComputeStack`, DDB in separate stack | **§4 Micro-Stack Variant** |

**Cross-stack risk.** `appsync.LambdaDataSource(fn)` auto-grants `lambda:InvokeFunction` on the function's resource policy. Same fix pattern as REST: use `allow_test_invoke=False` style + explicit `fn.add_permission()` consumer-side.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_appsync as appsync,
    aws_logs as logs,
)
from pathlib import Path


def _create_graphql(self, stage: str) -> None:
    schema_path = Path(__file__).parent / "schema" / "schema.graphql"

    self.graphql_api = appsync.GraphqlApi(
        self, "GraphqlApi",
        name=f"{{project_name}}-graphql-{stage}",
        schema=appsync.SchemaFile.from_asset(str(schema_path)),
        authorization_config=appsync.AuthorizationConfig(
            default_authorization=appsync.AuthorizationMode(
                authorization_type=appsync.AuthorizationType.API_KEY,
                api_key_config=appsync.ApiKeyConfig(
                    expires=cdk.Expiration.after(Duration.days(365)),
                ),
            ),
            additional_authorization_modes=[
                appsync.AuthorizationMode(
                    authorization_type=appsync.AuthorizationType.USER_POOL,
                    user_pool_config=appsync.UserPoolConfig(user_pool=self.user_pool),
                ),
            ],
        ),
        log_config=appsync.LogConfig(
            field_log_level=appsync.FieldLogLevel.ERROR,
            retention=logs.RetentionDays.ONE_MONTH,
        ),
        xray_enabled=True,
    )

    # Data sources (monolith — L2 bindings OK)
    jobs_ds = self.graphql_api.add_dynamo_db_data_source("JobsDs",
        table=self.ddb_tables["jobs_ledger"])
    status_ds = self.graphql_api.add_lambda_data_source("StatusDs",
        lambda_function=self.lambda_functions["Status"])

    # Resolvers — direct DDB for simple lookups, Lambda for complex
    jobs_ds.create_resolver("GetJobById",
        type_name="Query", field_name="getJob",
        request_mapping_template=appsync.MappingTemplate.dynamo_db_get_item("job_id", "id"),
        response_mapping_template=appsync.MappingTemplate.dynamo_db_result_item(),
    )
    status_ds.create_resolver("ListJobs",
        type_name="Query", field_name="listJobs",
    )

    cdk.CfnOutput(self, "GraphqlUrl", value=self.graphql_api.graphql_url)
    cdk.CfnOutput(self, "GraphqlKey", value=self.graphql_api.api_key or "")
```

### 3.1 Monolith gotchas

- **Schema file** must exist at synth time. Use `Path(__file__).parent` anchor to avoid CWD issues.
- **API key expiration** — CDK rejects > 365 days; rotate via re-deploy.
- **`FieldLogLevel.ALL`** is expensive in prod; use `ERROR` and emit custom metrics from resolvers.

---

## 4. Micro-Stack Variant

### 4.1 `GraphqlStack` — owns API, accepts data sources by interface

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_appsync as appsync,
    aws_lambda as _lambda,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[3] / "schemas" / "schema.graphql"


class GraphqlStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        status_fn: _lambda.IFunction,
        jobs_table: ddb.ITable,
        user_pool_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-graphql", **kwargs)

        self.api = appsync.GraphqlApi(
            self, "Api",
            name="{project_name}-graphql",
            schema=appsync.SchemaFile.from_asset(str(_SCHEMA)),
            # ... auth + log config ...
            xray_enabled=True,
        )

        # Lambda data source — cross-stack
        status_ds = self.api.add_lambda_data_source("StatusDs", lambda_function=status_fn)
        # AppSync auto-grants invoke on status_fn (this mutates ComputeStack).
        # To avoid the bidirectional export, either:
        #   (a) accept the one-way dep: GraphqlStack depends on ComputeStack (fine)
        #   (b) use HTTP data source + API Gateway → Lambda (extra hop)
        # Option (a) is correct here; it's a one-direction dep.

        # DynamoDB data source — identity-side IAM on AppSync's service role
        jobs_ds = self.api.add_dynamo_db_data_source("JobsDs", table=jobs_table)
        # The DDB data source auto-creates a service role; CDK scopes it correctly
        # to the table ARN without touching the table in another stack.

        jobs_ds.create_resolver("GetJob",
            type_name="Query", field_name="getJob",
            request_mapping_template=appsync.MappingTemplate.dynamo_db_get_item("job_id", "id"),
            response_mapping_template=appsync.MappingTemplate.dynamo_db_result_item(),
        )

        cdk.CfnOutput(self, "GraphqlUrl", value=self.api.graphql_url)
```

### 4.2 Micro-stack gotchas

- **`LambdaDataSource` creates a service-role → role → function Invoke chain.** The function's resource policy IS updated. Accept the one-way dep: `GraphqlStack.add_dependency(ComputeStack)`. No cycle because no reverse edge.
- **DDB data source**: CDK creates a service role INSIDE GraphqlStack with identity-side policy referencing `jobs_table.table_arn`. Safe.
- **RDS via Lambda resolver** is the norm for AppSync + RDS (direct RDS data source is Aurora Serverless only).

---

## 5. Worked example

```python
def test_graphql_api_has_xray():
    # ... instantiate GraphqlStack ...
    t = Template.from_stack(gq)
    t.has_resource_properties("AWS::AppSync::GraphQLApi", {
        "XrayEnabled": True,
    })
```

---

## 6. References

- `docs/Feature_Roadmap.md` — AP-20
- Related SOPs: `LAYER_API` (REST alternative), `LAYER_BACKEND_LAMBDA` (resolvers)

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Explicit one-way dependency for Lambda data sources. |
| 1.0 | 2026-03-05 | Initial. |
