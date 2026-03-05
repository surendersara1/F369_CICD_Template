# PARTIAL: AppSync GraphQL API + Real-Time Subscriptions

**Usage:** Include when SOW mentions GraphQL, real-time data, WebSocket, mobile clients, or complex data fetching.

---

## REST API Gateway vs AppSync — Decision Guide

| Criteria                      | API Gateway (REST/HTTP) | AppSync (GraphQL)                       |
| ----------------------------- | ----------------------- | --------------------------------------- |
| Mobile + web clients          | OK                      | ✅ Better (typed schema)                |
| Real-time subscriptions       | ❌ Needs WebSocket API  | ✅ Built-in subscriptions               |
| Complex data fetching (joins) | ❌ Multiple round trips | ✅ Single query                         |
| Simple CRUD REST              | ✅ Perfect              | Overkill                                |
| Fine-grained field auth       | ❌ Endpoint-level only  | ✅ Field-level auth                     |
| Offline sync (mobile)         | ❌ Not supported        | ✅ Built-in (Amplify DataStore)         |
| Multiple backend sources      | ❌ One Lambda per route | ✅ Multiple resolvers (Lambda/DDB/HTTP) |

---

## CDK Code Block — AppSync GraphQL API

```python
def _create_graphql_api(self, stage_name: str) -> None:
    """
    AWS AppSync GraphQL API with:
      - Cognito authentication
      - DynamoDB direct resolvers (no Lambda needed for simple CRUD)
      - Lambda resolvers for complex business logic
      - Real-time subscriptions (WebSocket)
      - Field-level authorization
    """

    import aws_cdk.aws_appsync as appsync

    # =========================================================================
    # GRAPHQL SCHEMA
    # [Claude: generate from Architecture Map Section 5 Data Entity Map]
    # =========================================================================

    # Define schema inline (or load from .graphql file)
    schema = appsync.SchemaFile.from_asset("infrastructure/schema.graphql")
    # File contents should be generated based on detected data entities

    # =========================================================================
    # APPSYNC API
    # =========================================================================

    # CloudWatch log group for AppSync
    appsync_log_group = logs.LogGroup(
        self, "AppSyncLogGroup",
        log_group_name=f"/aws/appsync/{{project_name}}-{stage_name}",
        retention=logs.RetentionDays.ONE_MONTH,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    self.graphql_api = appsync.GraphqlApi(
        self, "GraphqlApi",
        name=f"{{project_name}}-api-{stage_name}",

        # Schema
        definition=appsync.Definition.from_schema(schema),

        # Authentication: Cognito primary, API key for public queries
        authorization_config=appsync.AuthorizationConfig(
            default_authorization=appsync.AuthorizationMode(
                authorization_type=appsync.AuthorizationType.USER_POOL,
                user_pool_config=appsync.UserPoolConfig(
                    user_pool=self.user_pool,
                    default_action=appsync.UserPoolDefaultAction.ALLOW,
                ),
            ),
            additional_authorization_modes=[
                # IAM auth for server-to-server (Lambda to AppSync)
                appsync.AuthorizationMode(
                    authorization_type=appsync.AuthorizationType.IAM,
                ),
            ],
        ),

        # Logging
        log_config=appsync.LogConfig(
            field_log_level=appsync.FieldLogLevel.ERROR if stage_name == "prod" else appsync.FieldLogLevel.ALL,
            exclude_verbose_content=stage_name == "prod",  # Don't log variables (may contain PII)
            role=iam.Role(
                self, "AppSyncLogRole",
                assumed_by=iam.ServicePrincipal("appsync.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSAppSyncPushToCloudWatchLogs"),
                ],
            ),
        ),

        # X-Ray
        xray_enabled=True,

        # WAF (optional — attach regional WAF)
        # [Claude: create WAF WebACL for AppSync if SOW requires it]
    )

    # =========================================================================
    # DATA SOURCES
    # =========================================================================

    # DynamoDB data source (direct resolver — no Lambda needed for simple CRUD)
    ddb_data_source = self.graphql_api.add_dynamo_db_data_source(
        "DynamoDBSource",
        list(self.ddb_tables.values())[0],
        description="Primary DynamoDB table as AppSync data source",
    )

    # Lambda data source (for complex business logic resolvers)
    lambda_data_source = self.graphql_api.add_lambda_data_source(
        "LambdaSource",
        self.lambda_functions.get("GraphqlResolver", list(self.lambda_functions.values())[0]),
        description="Lambda resolver for complex queries",
    )

    # HTTP data source (for calling external REST APIs directly from AppSync)
    # http_data_source = self.graphql_api.add_http_data_source(
    #     "ExternalApiSource",
    #     "https://api.external-service.com",
    # )

    # None data source (for local resolvers: subscriptions, pass-through)
    none_data_source = self.graphql_api.add_none_data_source(
        "NoneSource",
        description="Local resolver for subscriptions and transformations",
    )

    # =========================================================================
    # RESOLVERS — Connect GraphQL operations to data sources
    # [Claude: generate one resolver per operation in Architecture Map]
    # =========================================================================

    # --- Query Resolvers ---

    # getItem: DynamoDB GetItem (direct resolver, no Lambda)
    ddb_data_source.create_resolver(
        "GetItemResolver",
        type_name="Query",
        field_name="getItem",    # [Claude: replace with actual entity name from Schema]
        request_mapping_template=appsync.MappingTemplate.dynamo_db_get_item("id", "id"),
        response_mapping_template=appsync.MappingTemplate.dynamo_db_result_item(),
    )

    # listItems: DynamoDB Scan (direct resolver, add filters via VTL)
    ddb_data_source.create_resolver(
        "ListItemsResolver",
        type_name="Query",
        field_name="listItems",
        request_mapping_template=appsync.MappingTemplate.dynamo_db_scan_table(),
        response_mapping_template=appsync.MappingTemplate.dynamo_db_result_list(),
    )

    # complexQuery: Lambda resolver (for queries that need business logic)
    lambda_data_source.create_resolver(
        "ComplexQueryResolver",
        type_name="Query",
        field_name="searchItems",     # [Claude: replace with actual query name]
        request_mapping_template=appsync.MappingTemplate.lambda_request(),
        response_mapping_template=appsync.MappingTemplate.lambda_result(),
    )

    # --- Mutation Resolvers ---

    # createItem: DynamoDB PutItem
    ddb_data_source.create_resolver(
        "CreateItemResolver",
        type_name="Mutation",
        field_name="createItem",      # [Claude: replace with actual entity name]
        request_mapping_template=appsync.MappingTemplate.dynamo_db_put_item(
            appsync.PrimaryKey.partition("id").auto(),  # Auto-generate UUID
            appsync.Values.projecting(),                 # Map all input fields
        ),
        response_mapping_template=appsync.MappingTemplate.dynamo_db_result_item(),
    )

    # updateItem: DynamoDB UpdateItem
    ddb_data_source.create_resolver(
        "UpdateItemResolver",
        type_name="Mutation",
        field_name="updateItem",
        request_mapping_template=appsync.MappingTemplate.dynamo_db_put_item(
            appsync.PrimaryKey.partition("id").is_("input.id"),
            appsync.Values.projecting("input"),
        ),
        response_mapping_template=appsync.MappingTemplate.dynamo_db_result_item(),
    )

    # --- Subscription Resolvers (real-time) ---
    # Subscriptions use None data source — AppSync manages WebSocket connections

    none_data_source.create_resolver(
        "OnCreateItemSubscriptionResolver",
        type_name="Subscription",
        field_name="onCreateItem",    # [Claude: match mutation name from Schema]
        request_mapping_template=appsync.MappingTemplate.from_string(
            '{"version": "2018-05-29", "payload": {}}'
        ),
        response_mapping_template=appsync.MappingTemplate.from_string(
            "$util.toJson($ctx.result)"
        ),
    )

    # =========================================================================
    # GRAPHQL SCHEMA FILE (generate this from Architecture Map entities)
    # Save to: infrastructure/schema.graphql
    # [Claude: generate actual schema from Architecture Map Section 5]
    # =========================================================================
    # Example schema:
    """
    type Item @aws_cognito_user_pools @aws_iam {
      id: ID!
      name: String!
      status: String!
      createdAt: AWSDateTime!
      updatedAt: AWSDateTime
      owner: String!
    }

    type Query {
      getItem(id: ID!): Item @aws_cognito_user_pools
      listItems(limit: Int, nextToken: String): ItemConnection @aws_cognito_user_pools
      searchItems(query: String!, filters: SearchFilters): ItemConnection @aws_cognito_user_pools
    }

    type Mutation {
      createItem(input: CreateItemInput!): Item @aws_cognito_user_pools
      updateItem(id: ID!, input: UpdateItemInput!): Item @aws_cognito_user_pools
      deleteItem(id: ID!): Item @aws_cognito_user_pools(cognito_groups: ["admin"])
    }

    type Subscription {
      onCreateItem: Item @aws_subscribe(mutations: ["createItem"])
      onUpdateItem(id: ID!): Item @aws_subscribe(mutations: ["updateItem"])
    }

    type ItemConnection {
      items: [Item!]!
      nextToken: String
    }

    input CreateItemInput { name: String!, status: String }
    input UpdateItemInput { name: String, status: String }
    input SearchFilters { status: String, dateRange: DateRangeInput }
    input DateRangeInput { from: AWSDateTime!, to: AWSDateTime! }
    """

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "GraphqlApiUrl",
        value=self.graphql_api.graphql_url,
        description="AppSync GraphQL API URL",
        export_name=f"{{project_name}}-graphql-url-{stage_name}",
    )
    CfnOutput(self, "GraphqlApiId",
        value=self.graphql_api.api_id,
        description="AppSync API ID",
        export_name=f"{{project_name}}-graphql-id-{stage_name}",
    )
```

---

## Real-Time Subscription Flow

```
Client subscribes to onCreateItem via WebSocket
      │
      ▼
AppSync (manages WS connections, no server needed)
      │
mutations trigger publishEvents to subscribers
      │
createItem mutation called by any client
      ▼
DynamoDB PutItem
      │
AppSync automatically fans out to all onCreateItem subscribers (WebSocket push)
      │
      ▼
All subscribed clients receive the new item in real-time
```
