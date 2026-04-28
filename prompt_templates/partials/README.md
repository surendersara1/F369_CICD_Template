# F369 Partials — Library Index + Canonical Registry

**Location:** `E:\F369_CICD_Template\prompt_templates\partials\`
**Count:** 137 v2.0 partials (as of 2026-04-27 — Wave 17 added 3 CDN/edge compute partials; Wave 15 added 4 Bedrock Q Business + KB + Multi-Agent partials; Wave 16 added 3 ECS production partials; Wave 14 added 4 DR/resilience partials; Wave 13 added 4 migration partials; Wave 12 added 4 streaming analytics partials; Wave 11 added 6 enterprise governance partials; Wave 10 added 3 serverless backend partials; Wave 9 added 9 EKS production partials; Wave 7 added 7 P2/P3 SageMaker partials; Wave 6 added 8 SageMaker AI partials; Wave 5 added 8 data-platform partials)
**Authoring prompts:** [`_prompts/`](_prompts/README.md)

A partial is a self-contained SOP for one AWS concern — a CDK construct, an agent pattern, an IAM pattern, a compliance control, etc. Partials are consumed by LLM prompts (see the companion repo `F369_LLM_TEMPLATES`) that chain 3–15 partials into a 2-week client engagement (a "kit").

This README is the navigation surface for the library. It also enforces the **Canonical-Copy Rule** that prevents schema-hallucination drift (documented in the build prompt's §0 Hard Rule #8 and §9 Canonical Partials Registry).

---

## The Canonical-Copy Rule (READ BEFORE AUTHORING OR EDITING)

**Audit-driven discipline.** Three separate audit rounds (R1 = 2026-04-21, R2 = 2026-04-22, R3 = 2026-04-23) have caught the same failure mode: when a new partial uses a CDK primitive already covered by an existing audited partial, **re-deriving the pattern from memory re-introduces schema hallucinations**.

Round 3 is the clearest case: `PATTERN_CATALOG_EMBEDDINGS` and `PATTERN_MULTIMODAL_EMBEDDINGS` hallucinated a `filterable_metadata_keys` property on `AWS::S3Vectors::Index` that does not exist — even though the canonical `DATA_S3_VECTORS.md` (audited in R2) explicitly documents the correct schema. Fix: a 30-minute sweep that could have been avoided entirely by opening the canonical partial before authoring the new one.

### The rule

> **Before authoring any section that uses a CDK primitive, service API, or IAM action pattern covered by a canonical partial (see §Registry below), you MUST open that partial and COPY the audited pattern verbatim.** Adapt only variable names + logical IDs. Do not re-derive from memory.

### Enforcement

1. When editing an existing partial: check whether it's listed as canonical (§Registry). If yes, updates must go through a review pass — downstream consumers copy verbatim, so breaking-change edits cascade.
2. When authoring a new partial: for each primitive your §3 / §4 touches, find the canonical row in §Registry, OPEN the canonical partial, copy the `§3.X` code block, adapt naming only.
3. Your final `git diff` against the canonical should show primarily variable-name differences. Structural differences (different kwargs, different ARN patterns, different IAM action lists) mean you re-derived — **STOP and re-copy**.

---

## Canonical Partials Registry

This is the authoritative list of canonical partials — the ones whose §3/§4 patterns must be copied verbatim by any new partial touching the same primitive. A partial becomes canonical when it has passed at least one audit round AND no subsequent audit found a HIGH or MED issue in its covered primitive.

### Infrastructure + cross-cutting

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`LAYER_BACKEND_LAMBDA.md`](LAYER_BACKEND_LAMBDA.md) | Lambda base; **the 5 non-negotiables** (§4.1) echoed in every dual-variant partial | R1 | PASS |
| [`LAYER_NETWORKING.md`](LAYER_NETWORKING.md) | VPC + subnets + PrivateLink endpoints | R1 | PASS |
| [`LAYER_SECURITY.md`](LAYER_SECURITY.md) | KMS + IAM + permission boundary | R1 | PASS |
| [`LAYER_DATA.md`](LAYER_DATA.md) | DDB + S3 curated + patterns | R1 | PASS |
| [`LAYER_API.md`](LAYER_API.md) | API GW REST + WebSocket v2 | R1 | PASS |
| [`LAYER_FRONTEND.md`](LAYER_FRONTEND.md) | React + CloudFront + OAC (bucket + distro must share stack) | R1 | PASS |
| [`LAYER_OBSERVABILITY.md`](LAYER_OBSERVABILITY.md) | CloudWatch dashboards + alarms + X-Ray | R1 | PASS |
| [`LAYER_BACKEND_ECS.md`](LAYER_BACKEND_ECS.md) | ECS + Fargate base patterns | R1 | PASS |
| [`EVENT_DRIVEN_PATTERNS.md`](EVENT_DRIVEN_PATTERNS.md) | Cross-stack EventBridge (`CfnRule` + static-ARN target) | R1 | PASS |
| [`EVENT_DRIVEN_FAN_IN_AGGREGATOR.md`](EVENT_DRIVEN_FAN_IN_AGGREGATOR.md) | Fan-in aggregator for multi-source events | R2 | PASS |
| [`LLMOPS_BEDROCK.md`](LLMOPS_BEDROCK.md) | Bedrock `InvokeModel` + inference profile ARN shapes | R1 | PASS |
| [`COMPLIANCE_HIPAA_PCIDSS.md`](COMPLIANCE_HIPAA_PCIDSS.md) | Audit bucket + Backup Vault Lock + Config rules | R1 | PASS |
| [`SECURITY_WAF_SHIELD_MACIE.md`](SECURITY_WAF_SHIELD_MACIE.md) | WAF + Shield + Macie | R1 | PASS |

### Data platforms

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`DATA_S3_VECTORS.md`](DATA_S3_VECTORS.md) | `AWS::S3Vectors::VectorBucket` + `CfnIndex`; the `format_arn` idiom | R2 | PASS ⭐ **must-copy for any vector-store partial** |
| [`DATA_ICEBERG_S3_TABLES.md`](DATA_ICEBERG_S3_TABLES.md) | Managed Iceberg on S3 Tables; Athena `INSERT` ingest pattern | R3 | PASS (post-fix) |
| [`DATA_LAKEHOUSE_ICEBERG.md`](DATA_LAKEHOUSE_ICEBERG.md) | Self-managed Iceberg via Glue ETL + Athena v3 + Redshift Spectrum + LF | R1 | PASS |
| [`DATA_LAKE_FORMATION.md`](DATA_LAKE_FORMATION.md) | Gen-3 `CfnPrincipalPermissions` + LF-TBAC + RAM cross-account | R3 | PASS |
| [`DATA_GLUE_CATALOG.md`](DATA_GLUE_CATALOG.md) | Glue DB/Table/Crawler/DQ + federation via `CfnCatalog` | R3 | PASS |
| [`DATA_ATHENA.md`](DATA_ATHENA.md) | Workgroup + engine v3 + `EXPLAIN` preflight + `USING FUNCTION invoke_model` | R3 | PASS |
| [`DATA_AURORA_SERVERLESS_V2.md`](DATA_AURORA_SERVERLESS_V2.md) | Aurora Postgres v2; cluster parameter-group binding | R2 | PASS (post-fix) |
| [`DATA_MSK_KAFKA.md`](DATA_MSK_KAFKA.md) | MSK Serverless + connectors | R1 | PASS |
| [`DATA_ZERO_ETL.md`](DATA_ZERO_ETL.md) | Aurora/DDB → Redshift managed CDC via `CfnIntegration` | R3 | WARN (DDB source shape drift) |
| [`DATA_DATAZONE.md`](DATA_DATAZONE.md) | DataZone domain/project/data-product mesh | R3 | WARN (paginator name verify) |
| [`DATA_DMS_REPLICATION.md`](DATA_DMS_REPLICATION.md) | DMS Serverless homogeneous (2024 GA) + classic heterogeneous + S3 lakehouse landing | NEW (R5 pending) | UNAUDITED |
| [`DATA_RDS_MULTIAZ_CLUSTER.md`](DATA_RDS_MULTIAZ_CLUSTER.md) | RDS Multi-AZ DB cluster (3-node semi-sync) + Aurora Multi-AZ deployment + RDS Proxy | NEW (R5 pending) | UNAUDITED |
| [`DATA_EVENTBRIDGE_PIPES.md`](DATA_EVENTBRIDGE_PIPES.md) | DDB Streams / Kinesis / MSK source → enrich → S3/Firehose/SFN target | NEW (R5 pending) | UNAUDITED |
| [`DATA_APPFLOW_SAAS_INGEST.md`](DATA_APPFLOW_SAAS_INGEST.md) | Salesforce / Slack / ServiceNow / 60+ SaaS sources → S3 raw zone | NEW (R5 pending) | UNAUDITED |
| [`DATA_EMR_SERVERLESS_SPARK.md`](DATA_EMR_SERVERLESS_SPARK.md) | EMR Serverless 7.12 + Spark on Iceberg/Hudi/Delta + Glue Catalog + LF integration | NEW (R5 pending) | UNAUDITED |
| [`DATA_AURORA_GLOBAL_DR.md`](DATA_AURORA_GLOBAL_DR.md) | Aurora Global Database cross-region DR (RPO ≤ 1s, RTO ≤ 1 min) + AWS Backup cross-region | NEW (R5 pending) | UNAUDITED |
| [`DATA_ATHENA_FEDERATED_QUERY.md`](DATA_ATHENA_FEDERATED_QUERY.md) | Athena Federated Query (30+ Lambda connectors via SAR + Glue Catalog Federation) | NEW (R5 pending) | UNAUDITED |
| [`DATA_KINESIS_STREAMS_FIREHOSE.md`](DATA_KINESIS_STREAMS_FIREHOSE.md) | Kinesis Data Streams on-demand + provisioned + Enhanced Fan-Out + Firehose Lambda transform + dynamic partitioning + Parquet conversion + S3/OS sinks | NEW (R12 pending) | UNAUDITED |
| [`DATA_MANAGED_FLINK.md`](DATA_MANAGED_FLINK.md) | Amazon Managed Service for Apache Flink 1.20+ (Studio + Application) + Flink SQL + DataStream API + windowing + checkpointing + auto-scaling | NEW (R12 pending) | UNAUDITED |
| [`DATA_OPENSEARCH_SERVERLESS.md`](DATA_OPENSEARCH_SERVERLESS.md) | OS Serverless 3 collection types (TIMESERIES + VECTORSEARCH + SEARCH) + 3 policy types (encryption/network/data) + ISM + IAM SigV4 + VPC endpoints | NEW (R12 pending) | UNAUDITED |
| [`DATA_QUICKSIGHT_REALTIME.md`](DATA_QUICKSIGHT_REALTIME.md) | QuickSight Enterprise + Q (Bedrock-powered) + SPICE incremental refresh + RLS/CLS + embedded analytics + IDC SSO | NEW (R12 pending) | UNAUDITED |

### Security composite

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`SECURITY_DATALAKE_CHECKLIST.md`](SECURITY_DATALAKE_CHECKLIST.md) | 30-control composite security baseline for data lakes (LF + KMS + Macie + GuardDuty + CloudTrail Lake + Object Lock + Config + Access Analyzer) + daily audit Lambda | NEW (R5 pending) | UNAUDITED |

### AgentCore

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`AGENTCORE_RUNTIME.md`](AGENTCORE_RUNTIME.md) | AgentCore Runtime alpha L2 + `CfnRuntime` L1 fallback | R2 | WARN (alpha drift) |
| [`AGENTCORE_GATEWAY.md`](AGENTCORE_GATEWAY.md) | MCP Gateway + targets | R2 | PASS |
| [`AGENTCORE_IDENTITY.md`](AGENTCORE_IDENTITY.md) | Workload identity pools; OBO tokens | R2 | PASS |
| [`AGENTCORE_MEMORY.md`](AGENTCORE_MEMORY.md) | STM + LTM strategies | R2 | PASS |
| [`AGENTCORE_OBSERVABILITY.md`](AGENTCORE_OBSERVABILITY.md) | AgentCore dashboards + traces | R2 | PASS |
| [`AGENTCORE_BROWSER_TOOL.md`](AGENTCORE_BROWSER_TOOL.md) | Browser Tool (alpha L2 + L1 fallback) | R2 | WARN (alpha drift) |
| [`AGENTCORE_CODE_INTERPRETER.md`](AGENTCORE_CODE_INTERPRETER.md) | Code Interpreter; **scoped ARN for system CI** (not `"*"`) | R2 | PASS (post-fix) |
| [`AGENTCORE_AGENT_CONTROL.md`](AGENTCORE_AGENT_CONTROL.md) | Bedrock Guardrail + Cedar policy | R1 | PASS |
| [`AGENTCORE_A2A.md`](AGENTCORE_A2A.md) | Agent-to-agent protocol | R1 | PASS |

### Strands Agents SDK

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`STRANDS_AGENT_CORE.md`](STRANDS_AGENT_CORE.md) | Supervisor + tool library pattern | R1 | PASS |
| [`STRANDS_TOOLS.md`](STRANDS_TOOLS.md) | `@tool` wrapping; Code Interpreter shim | R1 | PASS |
| [`STRANDS_MULTI_AGENT.md`](STRANDS_MULTI_AGENT.md) | Fan-out + synthesis pattern | R1 | PASS |
| [`STRANDS_MCP_TOOLS.md`](STRANDS_MCP_TOOLS.md) | MCP client via SigV4 | R1 | PASS |
| [`STRANDS_MCP_SERVER.md`](STRANDS_MCP_SERVER.md) | MCP server hosting | R1 | PASS |
| [`STRANDS_HOOKS_PLUGINS.md`](STRANDS_HOOKS_PLUGINS.md) | RBAC middleware + token tracker | R1 | PASS |
| [`STRANDS_EVAL.md`](STRANDS_EVAL.md) | Grounding validator + eval | R1 | PASS |
| [`STRANDS_FRONTEND.md`](STRANDS_FRONTEND.md) | WebSocket streaming callback | R1 | PASS |
| [`STRANDS_DEPLOY_ECS.md`](STRANDS_DEPLOY_ECS.md) | Container → AgentCore Runtime | R1 | PASS |
| [`STRANDS_DEPLOY_LAMBDA.md`](STRANDS_DEPLOY_LAMBDA.md) | Strands in Lambda + layer | R1 | PASS |
| [`STRANDS_MODEL_PROVIDERS.md`](STRANDS_MODEL_PROVIDERS.md) | Bedrock + alt provider config | R1 | PASS |

### ML / SageMaker / MLOps

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`MLOPS_SAGEMAKER_TRAINING.md`](MLOPS_SAGEMAKER_TRAINING.md) | Training jobs + spot + warm pools | R1 | PASS |
| [`MLOPS_SAGEMAKER_SERVING.md`](MLOPS_SAGEMAKER_SERVING.md) | Real-time + serverless + async endpoints | R1 | PASS |
| [`MLOPS_BATCH_TRANSFORM.md`](MLOPS_BATCH_TRANSFORM.md) | Batch transform jobs | R1 | PASS |
| [`MLOPS_MULTI_MODEL_ENDPOINT.md`](MLOPS_MULTI_MODEL_ENDPOINT.md) | Multi-model endpoint | R1 | PASS |
| [`MLOPS_CLARIFY_EXPLAINABILITY.md`](MLOPS_CLARIFY_EXPLAINABILITY.md) | Clarify explainability + bias | R1 | PASS |
| [`MLOPS_GROUND_TRUTH.md`](MLOPS_GROUND_TRUTH.md) | Ground Truth labelling | R1 | PASS |
| [`MLOPS_AUDIO_PIPELINE.md`](MLOPS_AUDIO_PIPELINE.md) | Docker audio preprocessing + SageMaker MME | R2 | PASS (post-fix) |
| [`MLOPS_QUICKSIGHT_Q.md`](MLOPS_QUICKSIGHT_Q.md) | QuickSight Q topics + embedding | R3 | PASS (post-fix) |
| [`MLOPS_HYPERPOD_FM_TRAINING.md`](MLOPS_HYPERPOD_FM_TRAINING.md) | HyperPod Slurm + EKS for resilient FM training (Llama 3 70B/405B); FSx Lustre + EFA + auto-recovery | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_LLM_FINETUNING_PROD.md`](MLOPS_LLM_FINETUNING_PROD.md) | PEFT-LoRA pipeline + adapter inference components + JumpStart UI domain adaptation; multi-tenant LoRA serving | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_DISTRIBUTED_TRAINING.md`](MLOPS_DISTRIBUTED_TRAINING.md) | SMDDP data parallel + FSDP + DeepSpeed ZeRO-3; multi-node multi-GPU training jobs (non-HyperPod) | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_ASYNC_INFERENCE.md`](MLOPS_ASYNC_INFERENCE.md) | Async endpoints w/ S3 in/out + SNS notifications + auto-scale to 0; large-payload + bursty workloads | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_SAGEMAKER_UNIFIED_STUDIO.md`](MLOPS_SAGEMAKER_UNIFIED_STUDIO.md) | DataZone-integrated workspace + MLflow Apps + Bedrock + S3 Tables + TIP; modern Studio replacement | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_INFERENCE_PIPELINE_RECOMMENDER.md`](MLOPS_INFERENCE_PIPELINE_RECOMMENDER.md) | Multi-container inference pipelines (Serial/Direct) + Inference Recommender for right-sizing | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_CROSS_ACCOUNT_DEPLOY.md`](MLOPS_CROSS_ACCOUNT_DEPLOY.md) | 3-account ML governance (training → staging → prod) via RAM share + cross-account KMS/ECR/S3 | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_TRAINIUM_INFERENTIA_NEURON.md`](MLOPS_TRAINIUM_INFERENTIA_NEURON.md) | Trainium2 (training) + Inferentia2 (inference) on Neuron SDK 2.20+; 40-75% cost vs GPU | NEW (R6 pending) | UNAUDITED |
| [`MLOPS_LINEAGE_TRACKING.md`](MLOPS_LINEAGE_TRACKING.md) | ML Lineage API + auto-capture from Pipelines + Model Cards + compliance query Lambda | NEW (R7 pending) | UNAUDITED |
| [`MLOPS_MODEL_MONITOR_ADVANCED.md`](MLOPS_MODEL_MONITOR_ADVANCED.md) | Full 4-monitor pattern (Data + Model + Bias + Feature Attribution drift) + auto-rollback | NEW (R7 pending) | UNAUDITED |
| [`MLOPS_SMART_SIFTING.md`](MLOPS_SMART_SIFTING.md) | Drop-in DataLoader wrapper for 30-50% training cost savings on language models | NEW (R7 pending) | UNAUDITED |
| [`MLOPS_STUDIO_SPACES_LIFECYCLE.md`](MLOPS_STUDIO_SPACES_LIFECYCLE.md) | Per-user Studio Spaces (private + shared) + Custom Studio Images + Lifecycle Configurations | NEW (R7 pending) | UNAUDITED |
| [`MLOPS_CANVAS_NO_CODE.md`](MLOPS_CANVAS_NO_CODE.md) | No-code ML for citizen data scientists; AutoML + JumpStart UI + GenAI Q&A; handoff to MLOps | NEW (R7 pending) | UNAUDITED |
| [`MLOPS_GROUND_TRUTH_PLUS.md`](MLOPS_GROUND_TRUTH_PLUS.md) | Managed labeling service supporting infra (input + output buckets, IAM grants, batch trigger) | NEW (R7 pending) | UNAUDITED |
| [`MLOPS_GEOSPATIAL_ML.md`](MLOPS_GEOSPATIAL_ML.md) | Earth Observation Jobs (Sentinel-2 + Landsat) + pre-built models (LULC, NDVI, cloud removal) + custom training | NEW (R7 pending) | UNAUDITED |

### EKS / Kubernetes platform

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`EKS_CLUSTER_FOUNDATION.md`](EKS_CLUSTER_FOUNDATION.md) | EKS 1.32 control plane, managed node groups, EKS access entries (replaces aws-auth), OIDC provider, KMS envelope encryption, all 5 control plane logs, 4 EKS add-ons | NEW (R9 pending) | UNAUDITED |
| [`EKS_KARPENTER_AUTOSCALING.md`](EKS_KARPENTER_AUTOSCALING.md) | Karpenter v1.0+ NodePool + EC2NodeClass + NodeClaim API; Pod Identity for controller; SQS interruption queue + 4 EventBridge rules; consolidation policies; spot+on-demand mix; disruption budgets | NEW (R9 pending) | UNAUDITED |
| [`EKS_POD_IDENTITY.md`](EKS_POD_IDENTITY.md) | Pod Identity Associations (preferred) + IRSA fallback for cross-account; sts:TagSession trust requirement; IRSA→Pod Identity migration path | NEW (R9 pending) | UNAUDITED |
| [`EKS_NETWORKING.md`](EKS_NETWORKING.md) | VPC CNI prefix delegation + Custom Networking for IP exhaustion; AWS Load Balancer Controller v2.8 (ALB IP-mode + group.name shared); NLB; Gateway API; ExternalDNS | NEW (R9 pending) | UNAUDITED |
| [`EKS_OBSERVABILITY.md`](EKS_OBSERVABILITY.md) | Container Insights enhanced observability + amazon-cloudwatch-observability add-on + ADOT + AMP + AMG + Application Signals + Fluent Bit | NEW (R9 pending) | UNAUDITED |
| [`EKS_STORAGE.md`](EKS_STORAGE.md) | EBS gp3-encrypted default StorageClass + EFS access points (RWX) + FSx Lustre PERSISTENT_2 (S3-hydrated) + FSx ONTAP + VolumeSnapshot CRDs | NEW (R9 pending) | UNAUDITED |
| [`EKS_GITOPS.md`](EKS_GITOPS.md) | ArgoCD HA v2.13 + App-of-Apps + ApplicationSet + External Secrets Operator (ESO) + IAM Identity Center OIDC SSO + multi-cluster | NEW (R9 pending) | UNAUDITED |
| [`EKS_SECURITY.md`](EKS_SECURITY.md) | Pod Security Standards (restricted) + VPC CNI Network Policy Agent + ECR + Inspector enhanced scan + GuardDuty EKS Audit Logs + Runtime Monitoring + Kyverno admission control + cosign image signing + IMDSv2 | NEW (R9 pending) | UNAUDITED |
| [`EKS_COST_OPTIMIZATION.md`](EKS_COST_OPTIMIZATION.md) | Karpenter consolidation + VPA recommend mode + Compute Optimizer + Kubecost (CUR via Athena) + Spot strategy + Graviton ARM64 + Compute Savings Plans | NEW (R9 pending) | UNAUDITED |

### Enterprise / Multi-account governance

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`ENTERPRISE_CONTROL_TOWER.md`](ENTERPRISE_CONTROL_TOWER.md) | Control Tower landing zone v3 + canonical OU shape (Security/Workloads/Sandbox/Infrastructure/Suspended) + Account Factory + AFT + CfCT customizations + 30+ guardrails | NEW (R11 pending) | UNAUDITED |
| [`ENTERPRISE_IDENTITY_CENTER.md`](ENTERPRISE_IDENTITY_CENTER.md) | IAM Identity Center + Permission Sets (4 canonical) + ABAC via session tags + Azure AD/Okta SAML federation + SCIM auto-provisioning + Trusted Token Issuer | NEW (R11 pending) | UNAUDITED |
| [`ENTERPRISE_ORG_SCPS_RCPS.md`](ENTERPRISE_ORG_SCPS_RCPS.md) | 5 canonical SCPs (region opt-out, region restrict, security disable, root user, encrypt) + 2 RCPs (Nov 2024 GA — perimeter S3 + STS) + Declarative Policies + delegated admin | NEW (R11 pending) | UNAUDITED |
| [`ENTERPRISE_NETWORK_HUB_TGW.md`](ENTERPRISE_NETWORK_HUB_TGW.md) | Transit Gateway hub + Egress VPC + Inspection VPC w/ Network Firewall + RAM share to org + R53 Resolver inbound/outbound + centralized PrivateLink endpoints | NEW (R11 pending) | UNAUDITED |
| [`ENTERPRISE_CENTRALIZED_LOGGING.md`](ENTERPRISE_CENTRALIZED_LOGGING.md) | CloudTrail org trail + Log Archive S3 (Object Lock COMPLIANCE 7y + cross-region replication) + CloudTrail Lake event store + AWS Security Lake (OCSF Iceberg, Apr 2024 GA) | NEW (R11 pending) | UNAUDITED |
| [`ENTERPRISE_SECURITY_HUB_GD_ORG.md`](ENTERPRISE_SECURITY_HUB_GD_ORG.md) | Security Hub Central Configuration (Sept 2024) + GuardDuty 6 features org-wide + Inspector v2 + Macie + Detective + Access Analyzer + 4 standards subscriptions + finding routing | NEW (R11 pending) | UNAUDITED |

### CDN / Edge compute

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`CDN_CLOUDFRONT_FOUNDATION.md`](CDN_CLOUDFRONT_FOUNDATION.md) | CloudFront distribution + Origin Access Control (OAC, replaces OAI) + Cache/Origin/Response Headers Policies + custom error pages + WAF v2 (CLOUDFRONT scope, us-east-1) + Shield + multi-origin + ACM us-east-1 | NEW (R17 pending) | UNAUDITED |
| [`CDN_EDGE_COMPUTE.md`](CDN_EDGE_COMPUTE.md) | CloudFront Functions vs Lambda@Edge decision tree + KeyValueStore (Mar 2024) + canonical patterns (URL rewrite, redirects, JWT auth, geo routing, A/B canary, image resize) + cost economics | NEW (R17 pending) | UNAUDITED |
| [`CDN_MULTI_ORIGIN_FAILOVER.md`](CDN_MULTI_ORIGIN_FAILOVER.md) | Origin Groups (passive failover on 5xx) + Lambda@Edge active per-country origin routing + Route 53 DNS-based alternative + canary/A/B at edge + failover testing runbook | NEW (R17 pending) | UNAUDITED |

### Bedrock GenAI (Q Business + KB + Agents)

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`BEDROCK_Q_BUSINESS.md`](BEDROCK_Q_BUSINESS.md) | Amazon Q Business application + 40+ data source connectors (S3, SharePoint, Confluence, Salesforce, ServiceNow, Slack, Teams, Box, Drive, Jira, etc.) + plugins (built-in + custom OpenAPI) + Custom Q Apps + AppRoles + IDC SSO + Pro/Lite tier | NEW (R15 pending) | UNAUDITED |
| [`BEDROCK_KNOWLEDGE_BASES.md`](BEDROCK_KNOWLEDGE_BASES.md) | Bedrock KB deep dive: chunking strategies (default/fixed/hierarchical/semantic/custom Lambda) + vector store options (OS Serverless/Aurora pgvector/Pinecone/Redis/Neptune Analytics) + hybrid search + reranking + multi-tenant filters + Bedrock Guardrails | NEW (R15 pending) | UNAUDITED |
| [`BEDROCK_AGENTS_MULTI_AGENT.md`](BEDROCK_AGENTS_MULTI_AGENT.md) | Bedrock Agents + Multi-Agent Collaboration (Dec 2024 GA) supervisor + collaborators + Action Groups (Lambda + OpenAPI + Code Interpreter + ROC) + KB association + memory + custom orchestration | NEW (R15 pending) | UNAUDITED |
| [`BEDROCK_FLOWS_PROMPT_MGMT.md`](BEDROCK_FLOWS_PROMPT_MGMT.md) | Bedrock Flows visual orchestration + Prompt Management API + prompt versions + variants for A/B + prompt routing + flow versioning | NEW (R15 pending) | UNAUDITED |

### ECS / Containers (non-Kubernetes)

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`ECS_CLUSTER_FOUNDATION.md`](ECS_CLUSTER_FOUNDATION.md) | ECS cluster + Capacity providers (Fargate + Fargate Spot + EC2) + Service Connect (App Mesh replacement) + task definition + auto-scaling + Container Insights v2 + ECS Exec | NEW (R16 pending) | UNAUDITED |
| [`ECS_DEPLOYMENT_PATTERNS.md`](ECS_DEPLOYMENT_PATTERNS.md) | Rolling + CodeDeploy blue/green + canary configs + circuit breaker + deployment alarms + pre/post-traffic Lambda hooks + ECS Anywhere | NEW (R16 pending) | UNAUDITED |
| [`ECS_PRODUCTION_HARDENING.md`](ECS_PRODUCTION_HARDENING.md) | Per-service task IAM least-priv + auto-scaling (target tracking + step + scheduled) + GuardDuty Runtime Monitoring for ECS + ECR/Inspector + network + secrets + App Runner alternative | NEW (R16 pending) | UNAUDITED |

### Disaster Recovery / Resilience

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`DR_MULTI_REGION_PATTERNS.md`](DR_MULTI_REGION_PATTERNS.md) | 4 DR patterns (Backup-Restore / Pilot Light / Warm Standby / Active-Active) + RPO/RTO matrix + multi-region KMS / Aurora Global / DDB Global / S3 CRR with RTC + Route 53 failover | NEW (R14 pending) | UNAUDITED |
| [`DR_ROUTE53_ARC.md`](DR_ROUTE53_ARC.md) | Route 53 Application Recovery Controller — 5-region cluster + routing controls + readiness checks + safety rules + zonal shift/autoshift + failover script | NEW (R14 pending) | UNAUDITED |
| [`DR_RESILIENCE_HUB_FIS.md`](DR_RESILIENCE_HUB_FIS.md) | Resilience Hub assessments + resiliency policies + AWS Fault Injection Service (FIS) experiments + stop conditions + tag-scoped IAM + game day playbook | NEW (R14 pending) | UNAUDITED |
| [`DR_BACKUP_VAULT_LOCK.md`](DR_BACKUP_VAULT_LOCK.md) | AWS Backup centralized + Vault Lock COMPLIANCE (immutable) + cross-region copy + cross-account vault + Org backup policy + Backup Audit Manager + restore testing | NEW (R14 pending) | UNAUDITED |

### Migration

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`MIGRATION_MGN.md`](MIGRATION_MGN.md) | AWS Application Migration Service (MGN) — agent-based + agentless vCenter + post-launch SSM docs + wave cutover + right-sizing | NEW (R13 pending) | UNAUDITED |
| [`MIGRATION_SCHEMA_CONVERSION.md`](MIGRATION_SCHEMA_CONVERSION.md) | DMS Schema Conversion (in-DMS, replaces standalone SCT) + Fleet Advisor + Babelfish for Aurora PG (T-SQL passthrough) + manual Oracle/SQL Server → PG patterns | NEW (R13 pending) | UNAUDITED |
| [`MIGRATION_DATASYNC.md`](MIGRATION_DATASYNC.md) | DataSync agent + agentless + Discovery + NFS/SMB/HDFS/S3 sources + S3/EFS/FSx targets + multi-agent + scheduled sync | NEW (R13 pending) | UNAUDITED |
| [`MIGRATION_HUB_STRATEGY.md`](MIGRATION_HUB_STRATEGY.md) | Migration Hub + Strategy Recommendations + Refactor Spaces (Strangler Fig) + 6R framework + ADS + wave planning + TCO | NEW (R13 pending) | UNAUDITED |

### Serverless Backend

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`SERVERLESS_LAMBDA_POWERTOOLS.md`](SERVERLESS_LAMBDA_POWERTOOLS.md) | Lambda Powertools v3 (Python) / v2 (TypeScript) — logger + tracer + metrics + idempotency + parameters + batch + event parser + feature flags + Lambda Layer pattern | NEW (R10 pending) | UNAUDITED |
| [`SERVERLESS_DYNAMODB_PATTERNS.md`](SERVERLESS_DYNAMODB_PATTERNS.md) | Single-table design + GSI overload + transactions + DDB Streams + TTL + DAX + Global Tables v2 + PITR | NEW (R10 pending) | UNAUDITED |
| [`SERVERLESS_HTTP_API_COGNITO.md`](SERVERLESS_HTTP_API_COGNITO.md) | API Gateway HTTP API v2 + Cognito JWT authorizer + custom domain + WAF + CORS + throttling + access logs | NEW (R10 pending) | UNAUDITED |

### Agent / query patterns

| Canonical partial | Covers | First audited | Status |
|---|---|---|---|
| [`PATTERN_CATALOG_EMBEDDINGS.md`](PATTERN_CATALOG_EMBEDDINGS.md) | 3-level catalog embedding index + fingerprint-diff refresh | R3 | PASS (post-fix) |
| [`PATTERN_MULTIMODAL_EMBEDDINGS.md`](PATTERN_MULTIMODAL_EMBEDDINGS.md) | Titan Multimodal G1 for images + PDF pages | R3 | PASS (post-fix) |
| [`PATTERN_TEXT_TO_SQL.md`](PATTERN_TEXT_TO_SQL.md) | 4-phase discover-generate-preflight-execute pipeline | R3 | PASS |
| [`PATTERN_SEMANTIC_DATA_DISCOVERY.md`](PATTERN_SEMANTIC_DATA_DISCOVERY.md) | Find-my-data API; identity-from-JWT | R3 | PASS |
| [`PATTERN_ENTERPRISE_CHAT_ROUTER.md`](PATTERN_ENTERPRISE_CHAT_ROUTER.md) | Strands supervisor + 4 tools + OBO | R3 | PASS (post-fix) |
| [`PATTERN_DOC_INGESTION_RAG.md`](PATTERN_DOC_INGESTION_RAG.md) | Document chunk → embed → store → retrieve | R2 | PASS |
| [`PATTERN_AUDIO_SIMILARITY_SEARCH.md`](PATTERN_AUDIO_SIMILARITY_SEARCH.md) | Wav2Vec2 + audio-similarity store | R2 | PASS |
| [`PATTERN_BATCH_UPLOAD.md`](PATTERN_BATCH_UPLOAD.md) | Multi-format batch upload → validate → store | R2 | PASS |

---

## Common "what to copy" answers

Quick lookup — "I'm authoring a partial that uses X. Where do I copy from?"

| I'm authoring something that uses… | Copy from… | Section |
|---|---|---|
| S3 Vectors | `DATA_S3_VECTORS.md` | §3.2 (CfnIndex), §3.3 (grants), §3.4 (PutVectors/QueryVectors) |
| S3 Tables (managed Iceberg) | `DATA_ICEBERG_S3_TABLES.md` | §3.2 (CfnTable), §3.3 (grants), §3.4 (Athena INSERT) |
| Self-managed Iceberg | `DATA_LAKEHOUSE_ICEBERG.md` | §3 |
| Lake Formation | `DATA_LAKE_FORMATION.md` | §3.2 (Gen-3 grants), §3.3 (cross-account) |
| Glue Catalog / crawlers | `DATA_GLUE_CATALOG.md` | §3.2 (database+table), §3.3 (crawler) |
| Athena workgroups | `DATA_ATHENA.md` | §3.2 (workgroup + result bucket) |
| Aurora Postgres v2 | `DATA_AURORA_SERVERLESS_V2.md` | §3.2 (cluster + param group) |
| Zero-ETL | `DATA_ZERO_ETL.md` | §3.2 (RDS Integration) |
| DMS migration / CDC | `DATA_DMS_REPLICATION.md` | §3 (homogeneous) + §4 (heterogeneous + S3 target) |
| RDS Multi-AZ DB cluster | `DATA_RDS_MULTIAZ_CLUSTER.md` | §3.2 (cluster_type=CLUSTER_MULTI_AZ) |
| Aurora Multi-AZ deployment | `DATA_RDS_MULTIAZ_CLUSTER.md` | §4 (provisioned readers + auto-scaling) |
| Aurora Global Database (cross-region) | `DATA_AURORA_GLOBAL_DR.md` | §3 (primary) + §3.3 (secondary cross-region) |
| EventBridge Pipes | `DATA_EVENTBRIDGE_PIPES.md` | §3.2 (pipe + filter + enrich) |
| AppFlow SaaS ingest | `DATA_APPFLOW_SAAS_INGEST.md` | §3.2 (Salesforce flow + tasks) |
| EMR Serverless + Spark | `DATA_EMR_SERVERLESS_SPARK.md` | §3.2 (CfnApplication + Iceberg conf) |
| Athena Federated Query | `DATA_ATHENA_FEDERATED_QUERY.md` | §3.3 (SAR connector) + §4 (Glue Federation) |
| Kinesis Data Streams + Firehose (real-time ingest, Lambda transform, dynamic partitioning) | `DATA_KINESIS_STREAMS_FIREHOSE.md` | §3 (Monolith) + §5 (Production EFO) |
| Managed Flink (windowing, stateful streaming, Flink SQL) | `DATA_MANAGED_FLINK.md` | §3 (Monolith) + §4 (DataStream API) |
| OpenSearch Serverless (TIMESERIES / VECTORSEARCH / SEARCH collections) | `DATA_OPENSEARCH_SERVERLESS.md` | §3 (TIMESERIES) + §4 (VECTORSEARCH) + §5 (Private VPC) |
| QuickSight Enterprise + Q + embedded analytics + RLS | `DATA_QUICKSIGHT_REALTIME.md` | §3 (Monolith SPICE) + §4 (Embedded SaaS) |
| Data lake security baseline | `SECURITY_DATALAKE_CHECKLIST.md` | §4 (30-control composite) |
| HyperPod cluster (FM training) | `MLOPS_HYPERPOD_FM_TRAINING.md` | §3 (Slurm) + §4 (EKS) |
| LLM PEFT-LoRA fine-tune (production) | `MLOPS_LLM_FINETUNING_PROD.md` | §3 (Pipeline) + §5 (adapter components) |
| Multi-node distributed training (non-HyperPod) | `MLOPS_DISTRIBUTED_TRAINING.md` | §3.2 (SMDDP+FSDP) + §4 (SMP) |
| Async inference (large payloads, bursty) | `MLOPS_ASYNC_INFERENCE.md` | §3.2 (endpoint + SNS topics) |
| SageMaker Unified Studio | `MLOPS_SAGEMAKER_UNIFIED_STUDIO.md` | §3.2 (DataZone + Studio) |
| Multi-container inference pipeline + Inference Recommender | `MLOPS_INFERENCE_PIPELINE_RECOMMENDER.md` | §3.2 (CfnModel containers) + §5 (Recommender job) |
| Cross-account model deployment | `MLOPS_CROSS_ACCOUNT_DEPLOY.md` | §3 (3-account flow with RAM) |
| Trainium2 / Inferentia2 / Neuron SDK | `MLOPS_TRAINIUM_INFERENTIA_NEURON.md` | §3 (training) + §4 (inference) |
| ML Lineage / Model Cards | `MLOPS_LINEAGE_TRACKING.md` | §3 (manual + auto capture) + §4 (compliance query) |
| Model Monitor (drift detection, all 4 types) | `MLOPS_MODEL_MONITOR_ADVANCED.md` | §4 (CDK), §5 (baseline calibration) |
| Smart Sifting (training cost savings) | `MLOPS_SMART_SIFTING.md` | §3.2 (PyTorch wrapper) |
| Studio Spaces (per-user) | `MLOPS_STUDIO_SPACES_LIFECYCLE.md` | §3.2 + §5 (custom image) + §6 (lifecycle script) |
| Canvas (no-code ML) | `MLOPS_CANVAS_NO_CODE.md` | §3.2 (enable in domain + per-user) |
| Ground Truth Plus (managed labeling) | `MLOPS_GROUND_TRUTH_PLUS.md` | §3.3 (CDK supporting infra) |
| Geospatial ML (Earth Observation) | `MLOPS_GEOSPATIAL_ML.md` | §3.3 (CDK + EOJ trigger) |
| EKS cluster (control plane + node groups + access entries) | `EKS_CLUSTER_FOUNDATION.md` | §3 (Monolith) + §7 (Micro-Stack) |
| Karpenter v1.x autoscaling | `EKS_KARPENTER_AUTOSCALING.md` | §3 (Monolith NodePool) + §4 (multi-pool prod) |
| Pod Identity / IRSA | `EKS_POD_IDENTITY.md` | §3 (Pod Identity) + §4 (IRSA cross-account) |
| EKS networking (VPC CNI + LBC + ALB/NLB Ingress) | `EKS_NETWORKING.md` | §3 (ALB IP-mode) + §4 (NLB) + §6 (prefix delegation) |
| EKS observability (Container Insights + ADOT + AMP/AMG) | `EKS_NETWORKING.md` → `EKS_OBSERVABILITY.md` | §3 (Container Insights) + §4 (ADOT/AMP/AMG) |
| EKS storage (EBS/EFS/FSx) | `EKS_STORAGE.md` | §3 (EBS gp3) + §4 (EFS) + §5 (FSx Lustre) + §7 (snapshots) |
| EKS GitOps (ArgoCD + ESO) | `EKS_GITOPS.md` | §3 (ArgoCD) + §4 (ApplicationSet) + §5 (ESO) |
| EKS security (PSS + NetworkPolicy + ECR/Inspector + GuardDuty + Kyverno) | `EKS_SECURITY.md` | §3 (PSS) + §4 (NetworkPolicy) + §5 (ECR/Inspector) + §6 (GuardDuty) + §7 (Kyverno) |
| EKS cost optimization (Karpenter + VPA + Spot + Graviton + SP) | `EKS_COST_OPTIMIZATION.md` | §3 (Karpenter consolidation) + §4 (VPA) + §6 (Kubecost) + §8 (SP) |
| Lambda Powertools (logger/tracer/metrics/idempotency) | `SERVERLESS_LAMBDA_POWERTOOLS.md` | §3 (Monolith) + §4 (Layer) |
| DynamoDB single-table design + GSI / transactions / Streams / DAX | `SERVERLESS_DYNAMODB_PATTERNS.md` | §3 (single-table + GSI) + §4 (Streams) + §6 (DAX) + §7 (Global Tables) |
| HTTP API + Cognito JWT authorizer + custom domain + WAF | `SERVERLESS_HTTP_API_COGNITO.md` | §3 (Monolith) + §4 (Production WAF) |
| Control Tower landing zone + OUs + AFT/CfCT | `ENTERPRISE_CONTROL_TOWER.md` | §3 (Monolith) + §5 (AFT) |
| IAM Identity Center + Permission Sets + Azure AD federation | `ENTERPRISE_IDENTITY_CENTER.md` | §3 (Monolith) + §4 (Federated) |
| SCPs / RCPs / Declarative Policies | `ENTERPRISE_ORG_SCPS_RCPS.md` | §3 (5 canonical SCPs) + §4 (RCPs) + §5 (Declarative) |
| Transit Gateway hub-and-spoke + Network Firewall + RAM | `ENTERPRISE_NETWORK_HUB_TGW.md` | §3 (Monolith) + §4 (Network Firewall) |
| Centralized logging (CloudTrail Lake + Security Lake + Log Archive) | `ENTERPRISE_CENTRALIZED_LOGGING.md` | §3 (Log Archive) + §4 (CloudTrail Lake) + §5 (Security Lake) |
| Security Hub Central Config + GuardDuty 6 features + Inspector + Macie | `ENTERPRISE_SECURITY_HUB_GD_ORG.md` | §3 (Monolith) |
| AgentCore Runtime | `AGENTCORE_RUNTIME.md` | §3.2 (alpha L2) + §3.2b (L1) |
| AgentCore Memory | `AGENTCORE_MEMORY.md` | §3 |
| AgentCore Identity (OBO) | `AGENTCORE_IDENTITY.md` | §3 |
| AgentCore Code Interpreter | `AGENTCORE_CODE_INTERPRETER.md` | §3.2 + §4.2 (scoped ARN) |
| Strands Agent | `STRANDS_AGENT_CORE.md` + `STRANDS_TOOLS.md` | §3 |
| Bedrock InvokeModel | `LLMOPS_BEDROCK.md` | §3 (inference profile ARN shape) |
| QuickSight Q | `MLOPS_QUICKSIGHT_Q.md` | §3.2 (Topic) + §3.3 (embed SDK) |
| Cross-stack EventBridge | `EVENT_DRIVEN_PATTERNS.md` | §4 |
| Bucket + CloudFront OAC | `LAYER_FRONTEND.md` | §4 |
| Catalog embeddings | `PATTERN_CATALOG_EMBEDDINGS.md` | §3.2 + §3.3 |
| Multimodal embeddings | `PATTERN_MULTIMODAL_EMBEDDINGS.md` | §3.2 + §3.3 |
| Text-to-SQL | `PATTERN_TEXT_TO_SQL.md` | §3.2 + §3.3 (4-phase pipeline) |
| Chat router | `PATTERN_ENTERPRISE_CHAT_ROUTER.md` | §3.2 + §3.3 |

---

## Audit history

| Round | Date | Scope | Findings report |
|---|---|---|---|
| R1 | 2026-04-21 | 17 v2.0 exemplar partials | [`docs/audit_report_partials_v2.md`](../../docs/audit_report_partials_v2.md) |
| R2 | 2026-04-22 | 9 kit-driven partials (HR / RAG / Deep-Research / Acoustic kits) | [`docs/audit_report_partials_v2_new9.md`](../../docs/audit_report_partials_v2_new9.md) |
| R3 | 2026-04-23 | 12 AI-native-lakehouse partials (Waves 1-4) | [`docs/audit_report_partials_v2_new12.md`](../../docs/audit_report_partials_v2_new12.md) |

All three audits share the same rubric (see `_prompts/audit_partials_v2.md`). Findings are graded HIGH / MED / LOW. Every audit has produced at least one HIGH finding traceable to memory-re-derivation — motivating the Canonical-Copy Rule.

### Key cross-audit patterns

1. **Alpha-API drift** (R2/F002-F005, R3/F2-04, R3/F2-10) — AgentCore + Strands SDK + apigatewayv2-authorizers are all in alpha packages that rename across minor versions. Mitigation: pin versions in `requirements.txt`, flag `TODO(verify)` at call sites, document L1 fallbacks in canonical partials' §3.2b.

2. **Cargo-culted boto3 methods** (R2/F001 `ephemeral_storage_size=Duration.seconds(0) and None`, R3/F2-03 `s3t.put_table_data`) — method names copy-pasted from memory of similar services. Mitigation: the Registry's "Why" column calls out known-cargo-cult patterns.

3. **Canonical-partial divergence** (R3/F2-01, F2-02, F2-11 — all centered on `DATA_S3_VECTORS`) — new partials re-derived instead of copying. **This is the motivating case for the Canonical-Copy Rule.**

4. **Security regression via over-broad resource scope** (R2/F004 `ci_arn = "*"`) — caught by the "scope IAM resources as tightly as possible" audit lens.

---

## When an audit finds a new gotcha

Run this loop:

1. Fix the canonical partial (edit `<CANONICAL>.md` §3 / §4).
2. Update this README's audit-status column.
3. Update the Registry row in this README + in `_prompts/build_remaining_partials_v2.md §9` with the finding + audit reference `[Audit: R<N>/F<NNN>]`.
4. `grep -r` for the old pattern across all partials; fix downstream partials in the same commit.
5. Commit with a descriptive message including `[Audit: R<N>/F<NNN>]`.
6. Update the audit report's fix log.

---

## Adding a new partial

1. Read [`_prompts/build_remaining_partials_v2.md`](_prompts/build_remaining_partials_v2.md) — especially §0 Hard Rules, §3 structure, **§9 Canonical Registry**.
2. Read [`LAYER_BACKEND_LAMBDA.md`](LAYER_BACKEND_LAMBDA.md) — the structural exemplar + 5 non-negotiables.
3. For each primitive your partial touches, look it up in §Registry above + OPEN the canonical partial.
4. Author §1–§8 following the 8-section structure.
5. Run `cdk synth --no-lookups -q` on the §6 worked example (if applicable).
6. If your partial uses a primitive that SHOULD have a canonical partial but none exists, flag it in your commit message so the Registry can be updated.

---

## Related

- [`_prompts/`](_prompts/README.md) — builder + auditor meta-prompts (Opus 4.6 / Opus 4.7)
- [`../../docs/`](../../docs/) — audit reports + build logs + architectural notes
- **Companion repo:** `F369_LLM_TEMPLATES` — kits (2-week engagement playbooks) + LLM-prompt templates that consume these partials
