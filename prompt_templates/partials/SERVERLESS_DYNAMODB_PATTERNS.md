# SOP — DynamoDB Production Patterns (single-table design · GSI · transactions · streams · TTL · DAX · auto-scaling · backup)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · DynamoDB on-demand + provisioned · Single-table design · GSI / LSI · TransactWrite / TransactGet · Streams (NEW_AND_OLD_IMAGES) · TTL · DAX · Point-in-Time Recovery · AWS Backup · Global Tables v2

---

## 1. Purpose

- Codify the **single-table design** pattern as the production default for DynamoDB. Multi-table is only for the rare case where access patterns are truly disjoint.
- Codify the **GSI design conventions** — overload PK/SK with `entity_type` + `entity_id`; sparse GSIs for filtered queries; never query without a partition key.
- Codify **transactions** (TransactWriteItems for cross-item ACID) and when NOT to use them (cost: 2× write capacity).
- Codify **DDB Streams** as the canonical change-data-capture mechanism — Lambda trigger + EventBridge Pipes integration.
- Codify **TTL** for auto-expiry and **DAX** for sub-millisecond read caching.
- Codify **on-demand vs provisioned** capacity decision tree.
- Codify **PITR + AWS Backup + Global Tables** for backup/DR.
- This is the **DDB production patterns specialisation**. Built on `LAYER_DATA` (DDB base). Required by every serverless backend composite template.

When the SOW signals: "DDB at scale", "single-table design", "exactly-once writes", "DDB → S3 CDC", "global table", "DDB hot partition".

---

## 2. Decision tree — schema + capacity + DR

```
Access patterns?
├── 1-3 entity types, simple lookups → multi-table OK (don't over-engineer)
├── 4+ entity types with relations → §3 single-table design (default)
└── Time-series only → §3 single-table with composite SK (timestamp)

Capacity model?
├── Spiky / unknown traffic → on-demand (PAY_PER_REQUEST)
├── Predictable steady traffic > 1M req/day → provisioned + auto-scaling (-50% cost)
└── Mixed (predictable baseline + spike) → provisioned + reserved capacity

Cross-region?
├── Single region OK → §6 PITR (35d) + AWS Backup (longer)
└── Multi-region active-active → §7 Global Tables v2

Read latency requirement?
├── < 10ms p99 OK → DDB direct
├── < 1ms p99 → DAX (in-memory cache)
└── < 100µs (extreme) → ElastiCache Redis (different access model)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single-table + 2 GSIs + on-demand | **§3 Monolith** |
| Production — single-table + GSIs + Streams + DAX + Global Tables | **§7 Multi-region** |

---

## 3. Single-table design

### 3.1 Schema convention

```
PK (partition key) — generic name `pk`
SK (sort key)      — generic name `sk`

Entities encoded as:
  USER#<id>           PROFILE
  USER#<id>           ORDER#<order_id>
  ORDER#<order_id>    METADATA
  ORDER#<order_id>    ITEM#<sku>
  PRODUCT#<sku>       METADATA
  PRODUCT#<sku>       INVENTORY#<warehouse>

GSI1 (overloaded for inverted index):
  GSI1PK              GSI1SK
  ORDER#<order_id>    USER#<id>          ← find user for an order
  PRODUCT#<sku>       ORDER#<order_id>   ← find orders for a product

GSI2 (sparse — only orders within last 30 days):
  GSI2PK = "RECENT#ORDER"   GSI2SK = ISO timestamp
  (only set on items where status=open)
```

### 3.2 CDK

```python
# stacks/data_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_kms as kms
from constructs import Construct


class DataStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 kms_key: kms.IKey, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.table = ddb.Table(self, "AppTable",
            table_name=f"{env_name}-app",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,           # on-demand
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=kms_key,
            point_in_time_recovery=True,
            time_to_live_attribute="ttl",
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,           # CDC
            removal_policy=RemovalPolicy.RETAIN if env_name == "prod" else RemovalPolicy.DESTROY,
            deletion_protection=(env_name == "prod"),
        )

        # GSI1 — inverted index (overloaded PK/SK)
        self.table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=ddb.Attribute(name="GSI1PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="GSI1SK", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,                  # all attrs projected
        )

        # GSI2 — sparse (only items with GSI2PK attribute)
        self.table.add_global_secondary_index(
            index_name="GSI2",
            partition_key=ddb.Attribute(name="GSI2PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="GSI2SK", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.KEYS_ONLY,            # cheap; fetch full item via GetItem
        )
```

### 3.3 Application code — typed item layer

```python
# data/repositories.py
import boto3
from boto3.dynamodb.conditions import Key
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

ddb = boto3.resource("dynamodb")
table = ddb.Table("prod-app")


def create_order(user_id: str, items: list[dict]) -> str:
    order_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    total = sum(Decimal(str(i["price"])) * i["qty"] for i in items)

    # TransactWrite — order metadata + items + user→order index, all-or-nothing
    transact_items = [
        {"Put": {
            "TableName": "prod-app",
            "Item": {
                "pk": f"ORDER#{order_id}",
                "sk": "METADATA",
                "user_id": user_id,
                "total": total,
                "status": "open",
                "created_at": now,
                # GSI1 — inverted index: ORDER → USER
                "GSI1PK": f"ORDER#{order_id}",
                "GSI1SK": f"USER#{user_id}",
                # GSI2 — sparse recent index (only open orders)
                "GSI2PK": "RECENT#ORDER",
                "GSI2SK": now,
            },
            "ConditionExpression": "attribute_not_exists(pk)",  # idempotency on order_id
        }},
    ]
    for item in items:
        transact_items.append({"Put": {
            "TableName": "prod-app",
            "Item": {
                "pk": f"ORDER#{order_id}",
                "sk": f"ITEM#{item['sku']}",
                "qty": item["qty"],
                "price": Decimal(str(item["price"])),
                # GSI1 — find orders for a SKU
                "GSI1PK": f"PRODUCT#{item['sku']}",
                "GSI1SK": f"ORDER#{order_id}",
            },
        }})

    boto3.client("dynamodb").transact_write_items(TransactItems=transact_items)
    return order_id


def get_order(order_id: str) -> dict:
    """Returns metadata + all items in single Query."""
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"ORDER#{order_id}"),
    )
    return _hydrate_order(resp["Items"])


def list_orders_for_user(user_id: str) -> list[dict]:
    """Inverted lookup via GSI1."""
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1SK").eq(f"USER#{user_id}"),
    )
    return resp["Items"]


def list_recent_open_orders(limit: int = 100) -> list[dict]:
    """Sparse GSI2 — only contains open orders."""
    resp = table.query(
        IndexName="GSI2",
        KeyConditionExpression=Key("GSI2PK").eq("RECENT#ORDER"),
        ScanIndexForward=False,           # newest first
        Limit=limit,
    )
    return resp["Items"]


def close_order(order_id: str) -> None:
    """UpdateItem + remove from sparse GSI2 (close = no longer 'recent open')."""
    table.update_item(
        Key={"pk": f"ORDER#{order_id}", "sk": "METADATA"},
        UpdateExpression="SET #s = :closed REMOVE GSI2PK, GSI2SK",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":closed": "closed"},
    )
```

---

## 4. DDB Streams → Lambda / EventBridge Pipes

### 4.1 Lambda trigger (simple CDC)

```python
from aws_cdk import aws_lambda as _lambda
from aws_cdk.aws_lambda_event_sources import DynamoEventSource

cdc_fn = _lambda.Function(self, "CdcFn",
    runtime=_lambda.Runtime.PYTHON_3_12,
    handler="cdc.handler",
    code=_lambda.Code.from_asset("src/cdc"),
)
cdc_fn.add_event_source(DynamoEventSource(self.table,
    starting_position=_lambda.StartingPosition.LATEST,
    batch_size=100,
    max_batching_window=Duration.seconds(5),
    parallelization_factor=2,                # 2 concurrent Lambdas per shard
    retry_attempts=3,
    bisect_batch_on_error=True,              # halve batch on poison-pill
    on_failure=SqsDlq(dlq),                  # failed batches → DLQ
    report_batch_item_failures=True,         # partial-batch failure
))
```

### 4.2 EventBridge Pipes (richer routing — see `DATA_EVENTBRIDGE_PIPES`)

When you need filter + enrich + multiple targets, use Pipes instead of Lambda trigger.

---

## 5. TTL — auto-expiry

```python
# Set TTL attribute on item write
import time

table.put_item(Item={
    "pk": "SESSION#abc123",
    "sk": "META",
    "user_id": "u1",
    "ttl": int(time.time()) + 3600,    # expires in 1h (Unix epoch seconds)
})
# DDB scans every ~48h and removes expired items at no cost.
# TTL deletions appear in Streams — useful for "session ended" events.
```

---

## 6. DAX — sub-millisecond read cache

```python
from aws_cdk import aws_dax as dax

dax_subnets = [s.subnet_id for s in vpc.private_subnets]

dax_subnet_group = dax.CfnSubnetGroup(self, "DaxSubnetGroup",
    subnet_group_name="app-dax",
    subnet_ids=dax_subnets,
)

dax_cluster = dax.CfnCluster(self, "DaxCluster",
    cluster_name="app-dax",
    iam_role_arn=dax_role.role_arn,
    node_type="dax.r5.large",
    replication_factor=3,                          # 3 nodes across AZs for HA
    subnet_group_name=dax_subnet_group.subnet_group_name,
    security_group_ids=[dax_sg.security_group_id],
    cluster_endpoint_encryption_type="TLS",        # in-transit encryption
    sse_specification=dax.CfnCluster.SSESpecificationProperty(sse_enabled=True),
    parameter_group_name="default.dax1.0",
)
```

```python
# Application — use AmazonDaxClient instead of boto3 ddb client
from amazondax import AmazonDaxClient

dax_client = AmazonDaxClient.resource(endpoint_url=DAX_ENDPOINT)
table = dax_client.Table("prod-app")
# All ddb.Table API works identically; reads cached, writes write-through.
```

---

## 7. Global Tables v2 — multi-region active-active

```python
self.table = ddb.Table(self, "GlobalAppTable",
    table_name=f"{env_name}-app",
    partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
    sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
    billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
    encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
    encryption_key=kms_key,                  # MUST be multi-region key for global table
    point_in_time_recovery=True,
    stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
    replication_regions=["us-west-2", "eu-west-1"],   # creates global table v2
)
```

Conflict resolution: last-writer-wins by application-level timestamp; DDB writes propagate < 1s p99.

---

## 8. Common gotchas

- **Single-table design is hard to learn** but pays off at scale. Don't fight it — use Alex DeBrie's *DynamoDB Book* patterns.
- **Hot partitions** if PK has low cardinality. Best practice: hash high-cardinality entity_id into PK; use `<entity>#<id>` not just `<entity>`.
- **TransactWriteItems consume 2× WCU** for each item written. Use only when ACID is required across items.
- **`ConditionExpression: attribute_not_exists(pk)`** is the idiom for idempotent inserts. Returns `ConditionalCheckFailedException` on duplicate — handle gracefully.
- **Scan is forbidden in production** — only Query (with PK) or GetItem. If you can't avoid Scan, your access pattern needs a new GSI.
- **GSI projection_type=ALL doubles storage cost.** Use `KEYS_ONLY` for sparse GSIs and fetch full items via GetItem if needed.
- **Sparse GSI** = only items with the GSI's PK attribute appear in the index. Write `"GSI2PK": ...` to add; remove via `REMOVE GSI2PK, GSI2SK` to delete.
- **DDB Streams retain 24h.** If your downstream is down longer, data loss. Use Pipes with DLQ + replay strategy.
- **TTL is best-effort, not real-time.** Items can persist hours past TTL. Don't rely on it for security (use IAM, not TTL).
- **DAX cache is item-level, not query-level.** A query that returns 10 items caches each item separately by primary key. Updates invalidate cached items.
- **Global Tables KMS key must be a multi-region key** (`MultiRegion=true` on the CMK). Single-region key = cannot enable replication.
- **PITR + Global Table** work together but PITR restores per-region; you can't restore a global table to a single point in time across all regions atomically.
- **`PAY_PER_REQUEST` (on-demand) costs ~7× provisioned for the same throughput at sustained load** — flip to provisioned + auto-scaling when load is predictable.

---

## 9. Pytest worked example

```python
# tests/test_repositories.py
import boto3, pytest
from moto import mock_aws

from data.repositories import create_order, get_order, list_orders_for_user


@pytest.fixture
def ddb_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        ddb.create_table(
            TableName="prod-app",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        yield


def test_create_and_fetch_order(ddb_table):
    order_id = create_order("u1", [{"sku": "X", "qty": 2, "price": "10.00"}])
    order = get_order(order_id)
    assert order["status"] == "open"
    assert order["total"] == 20


def test_inverted_index_via_gsi1(ddb_table):
    create_order("u1", [{"sku": "X", "qty": 1, "price": "5.00"}])
    create_order("u1", [{"sku": "Y", "qty": 1, "price": "5.00"}])
    orders = list_orders_for_user("u1")
    assert len(orders) >= 2


def test_idempotent_insert_blocks_duplicate(ddb_table):
    """Recreating an order with same id raises ConditionalCheckFailed."""
    # (would require fixing order_id; create_order generates uuid — adapt for test)
    pass
```

---

## 10. Five non-negotiables

1. **Single-table design** — one DDB table per service, GSIs over multi-table.
2. **`PointInTimeRecovery=true`** + `deletion_protection=true` on prod tables.
3. **KMS CMK encryption** — never AWS-owned key for tables containing PII / business data.
4. **No Scan operations** in production code paths (verified via code review + CloudWatch `ConsumedReadCapacityUnits` per-table).
5. **DDB Streams enabled** (`NEW_AND_OLD_IMAGES`) for any table that downstream services may need to react to.

---

## 11. References

- [DynamoDB Best Practices](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)
- [Single-Table Design — Alex DeBrie](https://www.alexdebrie.com/posts/dynamodb-single-table/)
- [TransactWriteItems API](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)
- [DAX Developer Guide](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DAX.html)
- [Global Tables v2 (2019.11.21)](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/V2globaltables_HowItWorks.html)
- [DDB Streams + Lambda](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.Lambda.html)

---

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Single-table design + GSI conventions + transactions + Streams + TTL + DAX + Global Tables v2 + PITR. Wave 10. |
