# SOP — Bedrock Knowledge Bases (chunking strategies · vector store options · hybrid search · metadata filters · multi-tenant · citations)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Bedrock Knowledge Bases (GA Nov 2023) · Vector store options: OpenSearch Serverless / Aurora PostgreSQL pgvector / Pinecone / Redis Cloud / MongoDB Atlas / Neptune Analytics · Chunking strategies: default + fixed + hierarchical + semantic + custom Lambda · Hybrid search (BM25 + vector) · Metadata filters · Multi-tenant via filters · Generation models: Claude / Llama / Mistral · Citations + retrieval-only API

---

## 1. Purpose

- Codify **Bedrock Knowledge Bases** as the canonical AWS-native managed RAG service. Replaces hand-rolled `embed → upsert → retrieve → generate` pipelines with a managed flow.
- Codify **chunking strategies** — when to use default (300 tokens) vs fixed-size vs hierarchical vs semantic vs custom Lambda parser.
- Codify the **vector store decision tree** — OpenSearch Serverless vs Aurora pgvector vs Pinecone vs Redis vs Neptune Analytics.
- Codify **hybrid search** (BM25 keyword + vector semantic) — when on, default off.
- Codify **metadata filters** — file-level + chunk-level metadata for tenant isolation, recency, security clearance.
- Codify **citations + grounding** — Bedrock returns source spans with confidence; enforce with `RetrieveAndGenerate` configuration.
- Codify **multi-tenant patterns** — single KB with tenant-id filter vs separate KBs per tenant.
- Codify the **`Retrieve` (vector lookup only) vs `RetrieveAndGenerate` (full RAG)** APIs.
- This is the **deep RAG specialisation**. Use when Q Business UX doesn't fit OR you need fine-grained control. Pairs with `BEDROCK_Q_BUSINESS` (managed UX), `BEDROCK_AGENTS_MULTI_AGENT` (KB as agent tool).

When the SOW signals: "RAG pipeline", "knowledge base for chatbot", "Bedrock KB with custom UI", "vector search at scale", "compliance-grade citations".

---

## 2. Decision tree — vector store + chunking

### Vector store selection

| Vector store | Best for | RPS | Cost | Filter support |
|---|---|---|---|---|
| **OpenSearch Serverless (VECTORSEARCH)** | Default; AWS-native; tag-based filters | 1000+ | $$ ($350/mo min) | ✅ rich |
| **Aurora PostgreSQL + pgvector** | Already have Aurora; exact recall via IVFFlat/HNSW | 100s | $ at scale | ✅ SQL filters |
| **Pinecone** | Specialized; sparse/dense; namespaces for multi-tenant | 1000s | $$$ | ✅ namespaces |
| **MongoDB Atlas** | Already on MongoDB; integrated workflow | 100s | $$ | ✅ |
| **Redis Cloud (Enterprise)** | Sub-ms latency; in-memory | 10K+ | $$$ | ⚠️ |
| **Amazon Neptune Analytics** | GraphRAG (relationships matter) | 100s | $$ | ✅ |

**Default recommendation: OpenSearch Serverless VECTORSEARCH** — managed, tag filters, AWS-native auth, scales horizontally.

### Chunking strategy

| Strategy | Use when |
|---|---|
| **Default (300 tokens, 20% overlap)** | Most document Q&A; quick start |
| **Fixed-size** | Custom token count; structured docs (invoices, contracts) |
| **Hierarchical** | Long docs (books, manuals); preserve section context; uses parent+child chunks |
| **Semantic** | Topic-cohesive chunks; long-form articles (Bedrock auto-detects topic shifts) |
| **No chunking** | Pre-chunked sources (e.g., HelpScout articles) |
| **Custom Lambda** | Bespoke logic (e.g., per-paragraph + image OCR + metadata extraction) |

```
Architecture:
   Raw docs (S3)
        │
        ▼
   ┌────────────────────────────────────────┐
   │ Bedrock Knowledge Base                  │
   │   - Data source: S3 / Confluence / Web  │
   │   - Chunking: hierarchical (default)    │
   │   - Custom transformation Lambda (opt)  │
   │   - Embedding model: Titan v2 / Cohere   │
   └─────────────┬──────────────────────────┘
                 │  ingestion job
                 ▼
   ┌────────────────────────────────────────┐
   │ Vector Store                            │
   │   - OS Serverless VECTORSEARCH (default)│
   │   - Aurora pgvector (alt)                │
   │   - Schema: vector + text + metadata     │
   └─────────────┬──────────────────────────┘
                 │  query
                 ▼
   ┌────────────────────────────────────────┐
   │ Retrieve API                            │
   │   - Hybrid: BM25 + vector              │
   │   - Filter: metadata (tenant, security) │
   │   - Reranking: Cohere or AWS rerank      │
   │   - Returns: chunks + scores             │
   └─────────────┬──────────────────────────┘
                 │  augment + generate
                 ▼
   ┌────────────────────────────────────────┐
   │ RetrieveAndGenerate API                 │
   │   - LLM: Claude Opus / Sonnet / Haiku  │
   │   - Includes citations                   │
   │   - Configurable prompt template         │
   │   - Guardrails (PII, content filters)   │
   └────────────────────────────────────────┘
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — KB with S3 source + OS Serverless + default chunking | **§3 Monolith** |
| Production — multi-tenant + hybrid search + custom chunking + reranking + guardrails | **§5 Production** |

---

## 3. Monolith Variant — KB + S3 + OpenSearch Serverless

### 3.1 CDK

```python
# stacks/bedrock_kb_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_opensearchserverless as oss
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_kms as kms
from constructs import Construct
import json


class BedrockKbStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 kms_key: kms.IKey, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. S3 source bucket ───────────────────────────────────────
        source_bucket = s3.Bucket(self, "KbSourceBucket",
            bucket_name=f"{env_name}-kb-source-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── 2. OpenSearch Serverless VECTORSEARCH collection ──────────
        # (See DATA_OPENSEARCH_SERVERLESS for details)
        oss.CfnSecurityPolicy(self, "OssEncPolicy",
            name=f"{env_name}-kb-enc",
            type="encryption",
            policy=json.dumps({
                "Rules": [{"ResourceType": "collection",
                           "Resource": [f"collection/{env_name}-kb"]}],
                "AWSOwnedKey": False,
                "KmsARN": kms_key.key_arn,
            }),
        )
        oss.CfnSecurityPolicy(self, "OssNetPolicy",
            name=f"{env_name}-kb-net",
            type="network",
            policy=json.dumps([{
                "Rules": [
                    {"ResourceType": "collection",
                     "Resource": [f"collection/{env_name}-kb"]},
                ],
                "AllowFromPublic": True,                       # use VPC for prod
            }]),
        )
        kb_collection = oss.CfnCollection(self, "KbCollection",
            name=f"{env_name}-kb",
            type="VECTORSEARCH",
            standby_replicas="ENABLED",
        )

        # ── 3. KB execution role ─────────────────────────────────────
        kb_role = iam.Role(self, "KbRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
        )
        # Read source S3
        source_bucket.grant_read(kb_role)
        # Decrypt KMS
        kms_key.grant_encrypt_decrypt(kb_role)
        # Bedrock embedding model invoke
        kb_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
            ],
        ))
        # OpenSearch Serverless data plane
        kb_role.add_to_policy(iam.PolicyStatement(
            actions=["aoss:APIAccessAll"],
            resources=[kb_collection.attr_arn],
        ))

        # OS Serverless data access policy granting kb_role
        oss.CfnAccessPolicy(self, "OssDataAccess",
            name=f"{env_name}-kb-access",
            type="data",
            policy=json.dumps([{
                "Rules": [
                    {"ResourceType": "index",
                     "Resource": [f"index/{env_name}-kb/*"],
                     "Permission": ["aoss:CreateIndex", "aoss:DescribeIndex",
                                     "aoss:UpdateIndex",
                                     "aoss:ReadDocument", "aoss:WriteDocument"]},
                    {"ResourceType": "collection",
                     "Resource": [f"collection/{env_name}-kb"],
                     "Permission": ["aoss:CreateCollectionItems",
                                     "aoss:DescribeCollectionItems",
                                     "aoss:UpdateCollectionItems"]},
                ],
                "Principal": [kb_role.role_arn],
            }]),
        )

        # ── 4. Knowledge Base ─────────────────────────────────────────
        kb = bedrock.CfnKnowledgeBase(self, "KnowledgeBase",
            name=f"{env_name}-kb",
            description="Enterprise knowledge base",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=1024,                       # Titan v2 supports 256/512/1024
                            embedding_data_type="FLOAT32",
                        ),
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=kb_collection.attr_arn,
                    vector_index_name=f"{env_name}-kb-index",
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field="vector",
                        text_field="text",
                        metadata_field="metadata",
                    ),
                ),
            ),
        )

        # ── 5. Data source — S3 with hierarchical chunking ────────────
        data_source = bedrock.CfnDataSource(self, "S3DataSource",
            name=f"{env_name}-kb-s3",
            knowledge_base_id=kb.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=source_bucket.bucket_arn,
                    inclusion_prefixes=["docs/", "policies/"],
                ),
            ),
            # Hierarchical chunking — preserves doc structure
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="HIERARCHICAL",
                    hierarchical_chunking_configuration=bedrock.CfnDataSource.HierarchicalChunkingConfigurationProperty(
                        levels=[
                            bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                                max_tokens=1500,                  # parent (large)
                            ),
                            bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                                max_tokens=300,                   # child (small, returned in retrieval)
                            ),
                        ],
                        overlap_tokens=60,
                    ),
                ),
                # Custom Lambda transformation (optional — extract metadata, OCR images, etc.)
                # custom_transformation_configuration=...
                # Parsing strategy for PDFs/HTML
                parsing_configuration=bedrock.CfnDataSource.ParsingConfigurationProperty(
                    parsing_strategy="BEDROCK_FOUNDATION_MODEL",   # use FM to parse complex PDFs (tables, images)
                    bedrock_foundation_model_configuration=bedrock.CfnDataSource.BedrockFoundationModelConfigurationProperty(
                        model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-haiku-4-5-20251001",
                    ),
                ),
            ),
            data_deletion_policy="RETAIN",                        # keep vector embeddings if data source deleted
        )
```

### 3.2 Ingestion + query workflow

```bash
# Trigger ingestion
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id $KB_ID \
  --data-source-id $DS_ID \
  --description "Initial ingestion"

# Monitor
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id $KB_ID \
  --data-source-id $DS_ID \
  --ingestion-job-id $JOB_ID
# Status: STARTING → IN_PROGRESS → COMPLETE / FAILED
```

```python
# Query — Retrieve API (vector + metadata filter only)
import boto3
agent_runtime = boto3.client("bedrock-agent-runtime")

resp = agent_runtime.retrieve(
    knowledgeBaseId=kb_id,
    retrievalQuery={"text": "What is our PTO policy for new hires?"},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 5,
            "overrideSearchType": "HYBRID",                       # BM25 + vector
            "filter": {
                "andAll": [
                    {"equals": {"key": "department", "value": "HR"}},
                    {"greaterThanOrEquals": {"key": "year", "value": 2024}},
                ],
            },
        },
    },
)
# Returns chunks with content, location (S3 URI), score, metadata

# Query — RetrieveAndGenerate API (full RAG with LLM)
resp = agent_runtime.retrieve_and_generate(
    input={"text": "Summarize our PTO policy in 3 bullets"},
    retrieveAndGenerateConfiguration={
        "type": "KNOWLEDGE_BASE",
        "knowledgeBaseConfiguration": {
            "knowledgeBaseId": kb_id,
            "modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6",
            "retrievalConfiguration": {...},
            "generationConfiguration": {
                "promptTemplate": {
                    "textPromptTemplate": "You are HR assistant. Use ONLY the search results to answer. Cite sources by chunk number.\n\n$search_results$\n\nQuestion: $query$\n\nAnswer:"
                },
                "guardrailConfiguration": {
                    "guardrailId": guardrail_id,                  # Bedrock Guardrails
                    "guardrailVersion": "1",
                },
                "inferenceConfig": {
                    "textInferenceConfig": {
                        "temperature": 0.1,
                        "topP": 0.9,
                        "maxTokens": 1024,
                    },
                },
            },
        },
    },
)
# Returns: output.text + citations[] (each with retrievedReferences)
```

---

## 4. Multi-tenant patterns

### 4.1 Single KB with tenant filter (preferred for < 1M chunks total)

```python
# Each ingested document tagged with tenant_id metadata
# At query time, filter on metadata.tenant_id = current user's tenant
resp = agent_runtime.retrieve_and_generate(
    input={"text": user_query},
    retrieveAndGenerateConfiguration={
        "type": "KNOWLEDGE_BASE",
        "knowledgeBaseConfiguration": {
            "knowledgeBaseId": kb_id,
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {
                    "filter": {
                        "equals": {"key": "tenant_id", "value": user.tenant_id},
                    },
                },
            },
            "modelArn": "...",
        },
    },
)
```

Metadata is supplied via JSON sidecar files in S3 (Bedrock convention):
```
docs/
  tenant_a/
    policy.pdf
    policy.pdf.metadata.json     # {"metadataAttributes": {"tenant_id": "tenant_a", "year": 2024}}
  tenant_b/
    policy.pdf
    policy.pdf.metadata.json     # {"metadataAttributes": {"tenant_id": "tenant_b", "year": 2024}}
```

### 4.2 Per-tenant KBs (defense-in-depth for regulated multi-tenant)

```python
# CDK loop creating one KB per tenant
for tenant_id in tenants:
    bedrock.CfnKnowledgeBase(self, f"Kb_{tenant_id}",
        name=f"prod-kb-{tenant_id}",
        # ... per-tenant config ...
    )
# Pros: hard isolation, smaller indexes, per-tenant capacity
# Cons: 10× management overhead at 10+ tenants
```

---

## 5. Production Variant — hybrid + reranking + guardrails + custom Lambda chunker

### 5.1 Custom Lambda chunker for complex source

```python
# Useful for: scientific papers (per-figure chunks), legal docs (per-clause), code (per-function)

custom_chunker_fn = _lambda.Function(self, "ChunkerFn",
    runtime=_lambda.Runtime.PYTHON_3_12,
    handler="chunker.handler",
    code=_lambda.Code.from_asset("src/kb_chunker"),
    timeout=Duration.minutes(15),
    memory_size=2048,
)
# Bedrock invokes Lambda with input + S3 location;
# Lambda returns chunks JSON with per-chunk metadata.
# Output schema:
# {
#   "fileContents": [
#     {
#       "contentBody": "<chunk text>",
#       "contentMetadata": {"chunk_idx": 0, "section": "Methods"},
#       "contentType": "STRING"
#     }
#   ]
# }

# In data source config:
custom_chunking_data_source = bedrock.CfnDataSource(self, "CustomChunkDS",
    knowledge_base_id=kb.attr_knowledge_base_id,
    data_source_configuration=...,
    vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
        chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
            chunking_strategy="NONE",                          # disable default chunking
        ),
        custom_transformation_configuration=bedrock.CfnDataSource.CustomTransformationConfigurationProperty(
            transformations=[bedrock.CfnDataSource.TransformationProperty(
                step_to_apply="POST_CHUNKING",
                transformation_function=bedrock.CfnDataSource.TransformationFunctionProperty(
                    transformation_lambda_configuration=bedrock.CfnDataSource.TransformationLambdaConfigurationProperty(
                        lambda_arn=custom_chunker_fn.function_arn,
                    ),
                ),
            )],
            intermediate_storage=bedrock.CfnDataSource.IntermediateStorageProperty(
                s3_location=bedrock.CfnDataSource.S3LocationProperty(
                    uri=f"s3://{intermediate_bucket.bucket_name}/chunks/",
                ),
            ),
        ),
    ),
)
```

### 5.2 Reranking with Cohere or AWS Rerank

```python
# Reranking improves precision by 10-30% on retrieval
resp = agent_runtime.retrieve(
    knowledgeBaseId=kb_id,
    retrievalQuery={"text": query},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 20,                              # over-retrieve
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {
                        "modelArn": f"arn:aws:bedrock:{region}::foundation-model/cohere.rerank-v3-5:0",
                    },
                    "numberOfRerankedResults": 5,               # final top-5
                },
            },
        },
    },
)
```

### 5.3 Bedrock Guardrails integration

```python
guardrail = bedrock.CfnGuardrail(self, "KbGuardrail",
    name=f"{env_name}-kb-guardrail",
    description="Block PII leakage + competitor mentions",
    blocked_input_messaging="I can't process that request.",
    blocked_outputs_messaging="I can't share that information.",
    content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
        filters_config=[
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type="HATE", input_strength="HIGH", output_strength="HIGH",
            ),
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type="SEXUAL", input_strength="HIGH", output_strength="HIGH",
            ),
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE",
            ),
        ],
    ),
    sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
        pii_entities_config=[
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="EMAIL", action="ANONYMIZE"),
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="PHONE", action="ANONYMIZE"),
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="CREDIT_DEBIT_CARD_NUMBER", action="BLOCK"),
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="US_SOCIAL_SECURITY_NUMBER", action="BLOCK"),
        ],
    ),
    word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
        words_config=[
            bedrock.CfnGuardrail.WordConfigProperty(text="competitor-name-1"),
        ],
        managed_word_lists_config=[
            bedrock.CfnGuardrail.ManagedWordsConfigProperty(type="PROFANITY"),
        ],
    ),
)
# Use guardrail_id + version in retrieve_and_generate guardrailConfiguration
```

---

## 6. Common gotchas

- **Hierarchical chunking** is the best default for most documents — preserves doc structure better than fixed chunks.
- **Embedding model dimensions**: Titan v2 supports 256/512/1024. Larger = better recall but more storage/compute. Default 1024 unless cost-sensitive.
- **OS Serverless 2 OCU minimum** = ~$350/mo per collection. Share collection across KBs if multiple small ones.
- **OS Serverless VECTORSEARCH dimension cap = 16,000** — Titan v2 1024 fits comfortably.
- **Aurora pgvector vs OS Serverless** — Aurora cheaper at scale (no min OCUs); use HNSW index. OS easier for ops.
- **Hybrid search is OPT-IN per query** via `overrideSearchType: HYBRID`. Default is `SEMANTIC` (vector only). Hybrid = better for keyword-heavy queries (product names, IDs).
- **Metadata file MUST be exact same name** as source + `.metadata.json` suffix — `policy.pdf.metadata.json`. Wrong name → metadata silently dropped.
- **Metadata filterable types**: STRING, NUMBER, STRING_LIST, NUMBER_LIST. No boolean directly — encode as 0/1.
- **`numberOfResults` cap = 100** for Retrieve API. Beyond that → custom retrieval pipeline.
- **`promptTemplate.textPromptTemplate` vars**: `$search_results$`, `$query$`, `$output_format_instructions$`. Customize for citation format.
- **Citations format** — `retrievedReferences[].location.s3Location.uri` + `content.text` (the matched chunk). Render with link to source.
- **Re-ingestion behavior** — `start-ingestion-job` re-processes ALL files (full crawl). For incremental, ensure source uses CHANGE_LOG mode if available, or filter by lastModified in pre-process Lambda.
- **Custom Lambda chunker timeout 15 min max** — for very large files, split upstream.
- **`parsingStrategy: BEDROCK_FOUNDATION_MODEL`** for PDFs uses Bedrock vision; ~10× cost vs default. Use only when PDFs have tables/images that matter.
- **Bedrock Guardrails increase latency 200-500ms** per request. Acceptable for chat; check for high-throughput batch.
- **Per-tenant KB cost** scales linearly — at 100 tenants = 100 × min OCUs ≈ $35K/mo. Use shared KB + filter unless regulated.
- **Cross-region** — KB is region-local. Replicate source data + create KB per region for global apps.
- **Cost knobs**: chunking model (parsing) + embedding model + retrieval LLM (RetrieveAndGenerate) + reranking model. Each is a Bedrock InvokeModel charge.

---

## 7. Pytest worked example

```python
# tests/test_bedrock_kb.py
import boto3, pytest

agent = boto3.client("bedrock-agent")
agent_runtime = boto3.client("bedrock-agent-runtime")


def test_kb_active(kb_id):
    kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    assert kb["status"] == "ACTIVE"


def test_data_source_synced(kb_id, data_source_id):
    jobs = agent.list_ingestion_jobs(
        knowledgeBaseId=kb_id, dataSourceId=data_source_id,
    )["ingestionJobSummaries"]
    assert jobs
    latest = jobs[0]
    assert latest["status"] == "COMPLETE"
    stats = latest["statistics"]
    assert stats["numberOfDocumentsScanned"] > 0
    assert stats["numberOfDocumentsFailed"] == 0


def test_retrieve_returns_citations(kb_id):
    resp = agent_runtime.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": "company values"},
    )
    assert resp.get("retrievalResults")
    for r in resp["retrievalResults"]:
        assert r.get("content", {}).get("text")
        assert r.get("location", {}).get("s3Location", {}).get("uri")
        assert r.get("score")


def test_metadata_filter_isolates_tenant(kb_id):
    """Tenant A's query should only return tenant A's docs."""
    resp = agent_runtime.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": "policy"},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "filter": {"equals": {"key": "tenant_id", "value": "tenant_a"}},
            },
        },
    )
    for r in resp["retrievalResults"]:
        meta = r.get("metadata", {})
        assert meta.get("tenant_id") == "tenant_a"


def test_retrieve_and_generate_returns_grounded_answer(kb_id):
    resp = agent_runtime.retrieve_and_generate(
        input={"text": "What's our PTO policy?"},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001",
            },
        },
    )
    assert resp.get("output", {}).get("text")
    assert resp.get("citations"), "No citations — possible hallucination"


def test_guardrail_blocks_pii_leak(kb_id, guardrail_id):
    """Guardrail should redact emails in output."""
    resp = agent_runtime.retrieve_and_generate(
        input={"text": "Show me the customer email list"},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": "...",
                "generationConfiguration": {
                    "guardrailConfiguration": {
                        "guardrailId": guardrail_id,
                        "guardrailVersion": "1",
                    },
                },
            },
        },
    )
    output = resp.get("output", {}).get("text", "")
    import re
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", output), "Email leaked despite guardrail"
```

---

## 8. Five non-negotiables

1. **Hierarchical chunking** as default unless docs are < 500 tokens (then default fixed).
2. **Hybrid search (`HYBRID`) ON for keyword-heavy queries** — IDs, product names, abbreviations.
3. **Metadata filters for multi-tenant** — single KB OK; never query without tenant filter.
4. **Bedrock Guardrails** on every production `RetrieveAndGenerate` — PII redact + content filter.
5. **CMK encryption** on KB + vector store + source bucket — never AWS-owned keys.

---

## 9. References

- [Bedrock Knowledge Bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html)
- [Chunking strategies](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking-parsing.html)
- [Hierarchical chunking (2024+)](https://aws.amazon.com/blogs/machine-learning/announcing-hierarchical-chunking-amazon-bedrock-knowledge-bases/)
- [Custom Lambda transformation](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-custom-transformation.html)
- [Reranking with Cohere/AWS](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-reranking.html)
- [Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html)
- [Vector store options](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. KB + chunking strategies + vector store options + hybrid search + metadata filters + multi-tenant + reranking + Guardrails. Wave 15. |
