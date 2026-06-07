# SOP — Amazon OpenSearch Serverless (Time Series · Vector Search · Search · Dashboards · index policies)

**Version:** 2.1 · **Last-reviewed:** 2026-06-17 · **Status:** Active
**R4 update (2026-06-17, F-AFIE-10):** §3 Monolith network policy now defaults `AllowFromPublic=False` + requires `source_vpce_ids` constructor parameter (asserted at synth time unless `compliance_class="dev"`). The §5 Production VPC-endpoint pattern is now the default behavior rather than a separate variant. §6 gotcha entry codifies the AFIE Sprint 10 F-DATA-03 retro: KMS-at-rest does NOT compensate for a public data plane because SigV4 is the only auth boundary. Forward-ref to F-AFIE-22 synth-guard `assert_oss_network_policy_no_public_in_prod`.
**Applies to:** AWS CDK v2 (Python 3.12+) · OpenSearch Serverless (collections) · 3 collection types: TIMESERIES, VECTORSEARCH, SEARCH · OCUs (OpenSearch Compute Units) — 2 indexing + 2 search minimum · Encryption + network + data access policies · IAM-based auth via SigV4 · OpenSearch Dashboards · ISM (Index State Management)

---

## 1. Purpose

- Codify **OpenSearch Serverless** as the canonical AWS-native real-time analytics + log search + vector store. Replaces self-managed OS clusters / Elasticsearch / managed-OS provisioned domains.
- Codify **3 collection types** + when to use each:
  - **TIMESERIES** — append-mostly logs, traces, metrics (clickstream, app logs)
  - **VECTORSEARCH** — k-NN/HNSW for embeddings (RAG, semantic search)
  - **SEARCH** — text search with re-indexing (catalog, knowledge base)
- Codify the **3 policy types** (Encryption, Network, Data Access) — NOT IAM policies; OS Serverless uses its own policy engine.
- Codify **IAM SigV4 auth** for IngestPipeline/Lambda → OS Serverless writes/reads.
- Codify **OS Dashboards** for visualizing collections.
- Codify **ISM (Index State Management)** for hot-warm-cold lifecycle on TIMESERIES collections.
- This is the **search/analytics specialisation**. Built on `LAYER_OBSERVABILITY` baseline. Pairs with `DATA_KINESIS_STREAMS_FIREHOSE` (ingest) + `DATA_MANAGED_FLINK` (compute) + `DATA_QUICKSIGHT_REALTIME` (BI on top).

When the SOW signals: "real-time log search", "ELK replacement", "vector search for RAG", "OpenSearch on AWS", "Kibana dashboards", "Splunk replacement at lower cost".

---

## 2. Decision tree — Serverless vs Managed Domain; collection type

| Need | Serverless | Managed Domain |
|---|:---:|:---:|
| Brand new workload, unpredictable scale | ✅ | ⚠️ overkill |
| Cost > $1k/mo predictable steady load | ⚠️ Serverless can be 30% pricier | ✅ provisioned cheaper |
| Multi-AZ HA out of box | ✅ default | ⚠️ requires configuration |
| Cross-cluster search | ❌ | ✅ |
| Custom plugins | ❌ | ✅ |
| OpenSearch ML (deprecated 2.x) | ❌ | ✅ |
| OpenSearch ML (3.x — model groups, agents) | ✅ (2024+) | ✅ |

**Recommendation: Serverless for greenfield. Managed Domain when you need cross-cluster, plugins, or have steady load > 10 OCUs/24×7.**

```
Collection types:

  TIMESERIES   → log/metric/trace ingestion; ISM for retention; rolling indices
                 example: app logs, K8s events, Kafka audit logs

  VECTORSEARCH → k-NN/HNSW; up to 16 KB vectors; metadata filtering;
                 example: RAG document embeddings, image similarity, recommendation embeddings

  SEARCH       → text search; full re-indexing OK; supports score_mode, function_score;
                 example: product catalog, knowledge base, doc search
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — TIMESERIES collection + Kinesis Firehose ingest + Dashboards | **§3 Monolith** |
| Production — multi-collection + ISM + IAM-bound + private VPC access | **§5 Production** |

---

## 3. Monolith Variant — TIMESERIES collection + Firehose ingest + Dashboards

### 3.1 CDK

```python
# stacks/oss_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_opensearchserverless as oss
from aws_cdk import aws_iam as iam
from constructs import Construct
import json


class OssStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 firehose_role_arn: str, dashboard_user_arns: list[str],
                 # F-AFIE-10: VPC-endpoint-only by default. source_vpce_ids must be
                 # supplied for any non-dev compliance_class; AllowFromPublic is the
                 # dev-only fallback.
                 source_vpce_ids: list[str] | None = None,
                 compliance_class: str = "prod-internal",
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        collection_name = f"{env_name}-events-ts"

        # ── 1. Encryption policy (KMS-backed) ─────────────────────────
        oss.CfnSecurityPolicy(self, "EncPolicy",
            name=f"{collection_name}-enc",
            type="encryption",
            policy=json.dumps({
                "Rules": [{
                    "ResourceType": "collection",
                    "Resource": [f"collection/{collection_name}"],
                }],
                "AWSOwnedKey": False,                            # use CMK
                "KmsARN": kms_key_arn,                            # parameterize
            }),
        )

        # ── 2. Network policy — VPC-endpoint-only by default (F-AFIE-10) ─
        # AFIE Sprint 10 F-DATA-03 HIGH: ms-09 deployed AllowFromPublic=True for
        # "ease of testing"; SecurityRiskAccount auditor flagged it because IAM SigV4
        # is the ONLY thing standing between any AWS account holder globally and the
        # data plane. KMS-at-rest doesn't help when the API is public + auth is a
        # SigV4 signature that a credential leak compromises end-to-end.
        # R4 default: AllowFromPublic=False; source_vpce_ids is REQUIRED parameter.
        # Set AllowFromPublic=True ONLY via dev-stage explicit flag.
        assert source_vpce_ids or compliance_class == "dev", (
            "F-AFIE-10: OpenSearch Serverless network policy must list source_vpce_ids "
            "for any non-dev compliance_class. Set compliance_class='dev' to use the "
            "AllowFromPublic=True fallback for local experimentation."
        )
        if compliance_class == "dev" and not source_vpce_ids:
            network_rules = [{
                "Rules": [
                    {"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]},
                    {"ResourceType": "dashboard",  "Resource": [f"collection/{collection_name}"]},
                ],
                "AllowFromPublic": True,
            }]
        else:
            network_rules = [{
                "Rules": [
                    {"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]},
                    {"ResourceType": "dashboard",  "Resource": [f"collection/{collection_name}"]},
                ],
                "AllowFromPublic": False,
                "SourceVPCEs": source_vpce_ids,
            }]
        oss.CfnSecurityPolicy(self, "NetworkPolicy",
            name=f"{collection_name}-net",
            type="network",
            policy=json.dumps(network_rules),
        )

        # ── 3. Collection ─────────────────────────────────────────────
        collection = oss.CfnCollection(self, "Collection",
            name=collection_name,
            type="TIMESERIES",                                    # SEARCH | VECTORSEARCH
            description="Event time-series for real-time analytics",
            standby_replicas="ENABLED",                           # multi-AZ HA
        )

        # ── 4. Data access policy (who can read/write to indices) ─────
        oss.CfnAccessPolicy(self, "DataAccess",
            name=f"{collection_name}-access",
            type="data",
            policy=json.dumps([{
                "Rules": [
                    {
                        "ResourceType": "index",
                        "Resource": [f"index/{collection_name}/*"],
                        "Permission": [
                            "aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                            "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument",
                        ],
                    },
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{collection_name}"],
                        "Permission": [
                            "aoss:CreateCollectionItems", "aoss:DeleteCollectionItems",
                            "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems",
                        ],
                    },
                ],
                "Principal": [
                    firehose_role_arn,
                    *dashboard_user_arns,
                ],
                "Description": "Firehose write + dashboard read",
            }]),
        )

        # ── 5. CloudWatch log group for dashboard audit logs ──────────
        # (Configured separately via OS console or aws aoss update-collection)

        self.collection_endpoint = collection.attr_collection_endpoint
        self.dashboard_endpoint = collection.attr_dashboard_endpoint
```

### 3.2 Index template + ISM policy (apply via API, not CDK)

```bash
# Create index template (via curl with SigV4 auth — see §4)
curl -X PUT "https://{collection_endpoint}/_index_template/events_template" \
  -H 'Content-Type: application/json' \
  --aws-sigv4 "aws:amz:us-east-1:aoss" -u "$AWS_ACCESS_KEY:$AWS_SECRET_KEY" \
  -d '{
    "index_patterns": ["events-*"],
    "template": {
      "settings": {
        "index.refresh_interval": "10s",
        "index.knn": false
      },
      "mappings": {
        "properties": {
          "event_time": {"type": "date"},
          "event_type": {"type": "keyword"},
          "user_id": {"type": "keyword"},
          "properties": {"type": "object", "enabled": true},
          "session_id": {"type": "keyword"}
        }
      }
    }
  }'

# ISM policy — hot 7d → warm 30d → delete 90d
curl -X PUT "https://{endpoint}/_plugins/_ism/policies/events_lifecycle" \
  --aws-sigv4 "aws:amz:us-east-1:aoss" \
  -d '{
    "policy": {
      "default_state": "hot",
      "states": [
        {
          "name": "hot",
          "actions": [{"rollover": {"min_index_age": "7d"}}],
          "transitions": [{"state_name": "warm", "conditions": {"min_index_age": "7d"}}]
        },
        {
          "name": "warm",
          "actions": [{"replica_count": {"number_of_replicas": 0}}],
          "transitions": [{"state_name": "delete", "conditions": {"min_index_age": "90d"}}]
        },
        {
          "name": "delete",
          "actions": [{"delete": {}}]
        }
      ]
    }
  }'
```

### 3.3 Firehose → OpenSearch sink

```python
# Add to Firehose ExtendedS3DestinationConfigurationProperty in DATA_KINESIS_STREAMS_FIREHOSE
# Or use AmazonopensearchserverlessDestinationConfiguration:

kdf.CfnDeliveryStream.AmazonopensearchserverlessDestinationConfigurationProperty(
    role_arn=firehose_role.role_arn,
    collection_endpoint=collection_endpoint,
    index_name="events-stream",
    s3_backup_mode="FailedDocumentsOnly",
    s3_configuration=...,    # backup bucket
    buffering_hints=kdf.CfnDeliveryStream.BufferingHintsProperty(
        interval_in_seconds=60,
        size_in_m_bs=5,
    ),
)
```

---

## 4. VECTORSEARCH variant — RAG / semantic search

```python
vector_collection = oss.CfnCollection(self, "VectorCol",
    name=f"{env_name}-rag-vectors",
    type="VECTORSEARCH",
    standby_replicas="ENABLED",
)
```

Index mapping (apply via API):

```json
{
  "mappings": {
    "properties": {
      "content_vector": {
        "type": "knn_vector",
        "dimension": 1024,
        "method": {
          "name": "hnsw",
          "space_type": "cosinesimil",
          "engine": "faiss",
          "parameters": {"ef_construction": 256, "m": 16}
        }
      },
      "content": {"type": "text"},
      "doc_id": {"type": "keyword"},
      "metadata": {"type": "object"}
    }
  },
  "settings": {"index.knn": true}
}
```

```python
# Python client query
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import boto3

credentials = boto3.Session().get_credentials()
auth = AWSV4SignerAuth(credentials, region="us-east-1", service="aoss")

client = OpenSearch(
    hosts=[{"host": collection_endpoint.replace("https://", ""), "port": 443}],
    http_auth=auth,
    use_ssl=True, verify_certs=True,
    connection_class=RequestsHttpConnection,
)

# k-NN query
results = client.search(
    index="rag-docs",
    body={
        "size": 10,
        "query": {
            "knn": {
                "content_vector": {
                    "vector": embedding_vector,    # 1024-dim from Bedrock Titan
                    "k": 10,
                },
            },
        },
        "_source": ["content", "doc_id", "metadata"],
    },
)
```

---

## 5. Production Variant — VPC private access + multi-collection

```python
# Network policy with VPC endpoints only (no public access)
vpc_endpoint = oss.CfnVpcEndpoint(self, "OssVpcE",
    name=f"{env_name}-oss-vpce",
    vpc_id=vpc.vpc_id,
    subnet_ids=[s.subnet_id for s in vpc.private_subnets],
    security_group_ids=[oss_sg.security_group_id],
)

oss.CfnSecurityPolicy(self, "PrivateNetwork",
    name=f"{collection_name}-net-private",
    type="network",
    policy=json.dumps([{
        "Rules": [
            {"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]},
            {"ResourceType": "dashboard", "Resource": [f"collection/{collection_name}"]},
        ],
        "AllowFromPublic": False,
        "SourceVPCEs": [vpc_endpoint.attr_id],
    }]),
)
```

---

## 6. Common gotchas

- **R4 / F-AFIE-10: VPC-endpoint-only is the prod default.** §3 + §4 + §5 now require `source_vpce_ids` for any non-dev compliance_class. `AllowFromPublic=True` is the dev-only fallback; the `assert` at the top of the network-policy section catches accidental prod-public deployments at synth time. AFIE Sprint 10 F-DATA-03 retro: KMS-at-rest does NOT compensate for a public data plane — IAM SigV4 is the only auth, a credential leak compromises the collection globally. Forward-ref: F-AFIE-22 `assert_oss_network_policy_no_public_in_prod` synth-guard.
- **OS Serverless has a 2 OCU minimum (1 indexing + 1 search) per collection** — $0.24/OCU-hour = ~$350/mo per collection minimum. For dev, share collections across apps if budget tight.
- **Standby replicas (multi-AZ) DOUBLE the OCU cost.** Prod-only in many cases.
- **OS Serverless does NOT support all OpenSearch APIs** — no `_cluster/health`, no `_cat/*` (some), no snapshots (managed automatically).
- **No cross-collection search** — must query collections individually.
- **VECTORSEARCH max dimension = 16,000** — larger embeddings need chunking.
- **VECTORSEARCH HNSW build is memory-intensive** during initial bulk ingest. Throttle to 100 docs/s for first load.
- **TIMESERIES has automatic shard management** — you don't size shards. But primary shard count via index template is ignored.
- **Data access policies are NOT IAM** — separate engine. IAM principal must be referenced in OS Serverless data access policy by ARN. Easy to miss.
- **SigV4 service name is `aoss`** (not `es`). Get this wrong → 403.
- **OS Dashboards URL is collection-specific.** Bookmark.
- **ISM rollover requires write alias + `is_write_index: true`** on the latest index — Firehose-managed indices need extra setup.
- **No reserved capacity discounts on OS Serverless** as of 2026. Cost-conscious workloads steady > 10 OCUs should consider Managed Domain.
- **VectorSearch k-NN scoring is cosine similarity by default** but only if you specify `cosinesimil`; default L2 (Euclidean) gives different results.
- **Dashboards anonymous auth not supported** — every viewer needs IAM principal in data access policy or SAML federation.

---

## 7. Pytest worked example

```python
# tests/test_oss.py
import boto3, pytest
from opensearchpy import OpenSearch, AWSV4SignerAuth, RequestsHttpConnection

aoss = boto3.client("opensearchserverless")


def test_collection_active(collection_name):
    cols = aoss.batch_get_collection(names=[collection_name])["collectionDetails"]
    assert cols
    assert cols[0]["status"] == "ACTIVE"


def test_index_exists(collection_endpoint, region):
    creds = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(creds, region, "aoss")
    client = OpenSearch(
        hosts=[{"host": collection_endpoint.replace("https://", ""), "port": 443}],
        http_auth=auth, use_ssl=True, verify_certs=True,
        connection_class=RequestsHttpConnection,
    )
    assert client.indices.exists(index="events-*")


def test_data_access_policy_includes_firehose_role(collection_name, firehose_role_arn):
    pol_name = f"{collection_name}-access"
    detail = aoss.get_access_policy(name=pol_name, type="data")["accessPolicyDetail"]
    policy = detail["policy"]
    statements = policy if isinstance(policy, list) else [policy]
    principals = []
    for s in statements:
        principals.extend(s.get("Principal", []))
    assert firehose_role_arn in principals


def test_encryption_policy_uses_cmk(collection_name):
    pol_name = f"{collection_name}-enc"
    detail = aoss.get_security_policy(name=pol_name, type="encryption")["securityPolicyDetail"]
    pol = detail["policy"]
    assert pol.get("AWSOwnedKey") is False
    assert pol.get("KmsARN")
```

---

## 8. Five non-negotiables

1. **CMK encryption** (`AWSOwnedKey: false` + explicit `KmsARN`) — never AWS-owned key for production.
2. **Network policy `AllowFromPublic: false`** + `SourceVPCEs: [...]` for production.
3. **Standby replicas ENABLED** for production (multi-AZ HA).
4. **ISM policy** on every TIMESERIES collection — auto-cleanup or unbounded growth.
5. **Data access policy minimal** — separate read-only vs write principals; no `*` permissions.

---

## 9. References

- [OpenSearch Serverless — Developer Guide](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless.html)
- [Collection types](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-overview.html#serverless-collection)
- [Vector search with OS Serverless](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html)
- [SigV4 auth for ingest](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-clients.html)
- [Index State Management (ISM)](https://opensearch.org/docs/latest/im-plugin/ism/index/)
- [Firehose → OpenSearch Serverless](https://docs.aws.amazon.com/firehose/latest/dev/destination-opensearch-serverless.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. OS Serverless 3 collection types + 3 policy types + ISM + VPC private + Firehose sink + k-NN. Wave 12. |
