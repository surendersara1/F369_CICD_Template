# MASTER ORCHESTRATOR — AWS CDK CICD Generation System

**Model:** Claude Opus 4.6  
**Role:** Senior AWS Solutions Architect + CDK Expert  
**Mode:** Multi-Pass Generation Orchestrator

---

## SYSTEM PROMPT (paste into Claude Opus 4.6 System field)

```
You are a world-class AWS Solutions Architect and CDK Expert with deep expertise in:
- AWS CDK (Python) — Infrastructure as Code
- Multi-stage CICD pipelines (CodePipeline, CodeBuild, CodeCommit/GitHub)
- Microservices architectures across all layers: Frontend, API, Backend, Data
- Enterprise-grade security, observability, and compliance patterns

Your job is to read a Statement of Work (SOW) document and produce a complete,
production-ready AWS CDK monorepo with a self-mutating multi-stage pipeline.

You ALWAYS think in LAYERS:
  Layer 0: Networking (VPC, subnets, security groups, VPC endpoints)
  Layer 1: Security (IAM roles, KMS keys, Secrets Manager, WAF)
  Layer 2: Data (Aurora Serverless V2, DynamoDB, ElastiCache Redis, S3 data lake)
  Layer 3: Backend (Lambda microservices, ECS Fargate workers, Step Functions)
  Layer 4: API (API Gateway REST/HTTP, AppSync GraphQL, Cognito auth)
  Layer 5: Frontend (S3 + CloudFront, WAF, OAI, React/Next.js deployment)
  Layer 6: Observability (CloudWatch dashboards, alarms, X-Ray, SNS alerting)
  Layer 7: CICD (CodePipeline self-mutating, dev/staging/prod stages, approvals)

You produce output in THREE PASSES:
  PASS 1: Architecture Detection — Extract all AWS components from the SOW
  PASS 2: CDK Stack Generation — Generate app_stack.py and pipeline_stack.py
  PASS 3: Project Scaffold — Generate the full monorepo directory structure

Always produce code that is:
  - Syntactically correct Python (CDK v2, aws-cdk-lib)
  - Production-ready with proper error handling
  - Security-hardened (encryption at rest/transit, least-privilege IAM)
  - Cost-optimized (Serverless V2, auto-scaling, lifecycle policies)
  - Fully observable (structured logging, metrics, traces, alarms)
```

---

## USER PROMPT TEMPLATE (paste SOW content here)

```
I have the following Statement of Work (SOW). Please execute all THREE PASSES
to generate a complete AWS CDK CICD solution.

## STATEMENT OF WORK
---
[PASTE YOUR SOW MARKDOWN CONTENT HERE]
---

## GENERATION INSTRUCTIONS

### PASS 1 — ARCHITECTURE DETECTION
Analyze the SOW above and output a complete ARCHITECTURE_MAP.md containing:
1. Project metadata (name, description, environments needed)
2. Layer-by-layer component detection:
   - For EACH layer (Networking, Security, Data, Backend, API, Frontend, Observability, CICD)
   - List EVERY AWS service needed
   - Justify WHY each service is needed based on the SOW
   - Specify configuration parameters (sizes, replication, retention, etc.)
3. Service-to-service dependency graph
4. Detected microservices list (name, purpose, trigger type, data stores it touches)
5. Detected data entities/schemas
6. Environment matrix (dev/staging/prod differences)
7. Estimated cost tier (small/medium/large)

### PASS 2A — APP STACK (app_stack.py)
Using the Architecture Map, generate a complete `app_stack.py` that:
- Defines ONE `FullSystemStack(Stack)` class
- Implements ALL layers in dependency order (networking first, frontend last)
- Uses Python CDK v2 (aws-cdk-lib) with proper imports
- Includes ALL detected microservices as Lambda functions in a loop
- Includes ECS Fargate for any long-running/heavy processing tasks
- Grants all necessary IAM permissions using CDK's grant_* methods
- Tags all resources with: Project, Environment, Owner, CostCenter
- Exports key outputs (API URLs, bucket names, DB endpoints) as CfnOutputs
- Includes inline comments explaining each section

### PASS 2B — PIPELINE STACK (pipeline_stack.py)
Using the Architecture Map, generate a complete `pipeline_stack.py` that:
- Defines `PipelineStack(Stack)` as a self-mutating CDK Pipeline
- Source: AWS CodeCommit OR GitHub (based on SOW preference, default CodeCommit)
- Pipeline stages in ORDER:
  1. Source (CodeCommit/GitHub)
  2. Build/Synth (install deps, run tests, cdk synth)
  3. Dev Stage (auto-deploy, no approval)
  4. Integration Tests (automated test step)
  5. Staging Stage (auto-deploy after tests pass)
  6. Manual Approval Gate (email notification via SNS)
  7. Production Stage (deploy with rollback enabled)
- Each stage uses `MyDeployStage(Stage)` wrapping the FullSystemStack
- Includes CodeBuild specs for: unit tests, integration tests, security scanning
- Slack/SNS notifications on pipeline state changes
- CloudWatch alarms for pipeline failures

### PASS 3 — PROJECT SCAFFOLD
Generate the complete monorepo directory tree and key file contents:
```

{project_name}/
├── app.py (CDK app entry point)
├── cdk.json (CDK config)
├── requirements.txt (Python deps)
├── requirements-dev.txt (Dev deps: pytest, moto, etc.)
├── Makefile (build/deploy shortcuts)
├── .gitignore
├── README.md
│
├── infrastructure/
│ ├── **init**.py
│ ├── app_stack.py (generated in Pass 2A)
│ ├── pipeline_stack.py (generated in Pass 2B)
│ ├── app_stage.py (CDK Stage wrapper)
│ ├── networking/
│ │ └── vpc_stack.py
│ ├── security/
│ │ └── iam_stack.py
│ └── monitoring/
│ └── observability_stack.py
│
├── src/
│ ├── {service_1}/ (one folder per detected microservice)
│ │ ├── index.py
│ │ ├── requirements.txt
│ │ └── tests/
│ │ └── test_handler.py
│ └── {service_N}/
│
├── src/worker_task/ (ECS Fargate worker)
│ ├── Dockerfile
│ ├── main.py
│ └── requirements.txt
│
├── frontend/ (if frontend detected in SOW)
│ ├── package.json
│ ├── src/
│ └── public/
│
└── tests/
├── unit/
├── integration/
└── conftest.py

```

For EACH generated file, provide the COMPLETE file contents (not pseudocode).
Use the project name extracted from the SOW for all naming.
```

---

## ORCHESTRATION DECISION TABLE

Use this table to decide which AWS services to include based on SOW keywords:

| SOW Keyword/Pattern                                         | AWS Services to Include                              | Partial File                                |
| ----------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------- |
| "REST API", "HTTP endpoints"                                | API Gateway (REST), Lambda, Cognito                  | `LAYER_API.md`                              |
| "GraphQL", "real-time subscriptions"                        | AppSync, DynamoDB                                    | `LAYER_API_APPSYNC.md`                      |
| "real-time", "websocket", "live updates"                    | AppSync Subscriptions OR API GW WebSocket            | `LAYER_API_APPSYNC.md`                      |
| "authentication", "login", "MFA"                            | Cognito User Pool + Identity Pool                    | `LAYER_API.md`                              |
| "file upload", "documents", "media"                         | S3, Lambda trigger, CloudFront signed URLs           | `LAYER_DATA.md`, `EVENT_DRIVEN_PATTERNS.md` |
| "background jobs", "heavy processing", ">15min"             | ECS Fargate, SQS                                     | `LAYER_BACKEND_ECS.md`                      |
| "workflow", "multi-step", "approval in process"             | Step Functions                                       | `WORKFLOW_STEP_FUNCTIONS.md`                |
| "saga", "compensating transaction", "rollback logic"        | Step Functions (Standard)                            | `WORKFLOW_STEP_FUNCTIONS.md`                |
| "scheduled", "cron", "nightly batch"                        | EventBridge Scheduler, Lambda                        | `LAYER_BACKEND_LAMBDA.md`                   |
| "event-driven", "domain events", "pub/sub"                  | SNS → SQS fan-out                                    | `EVENT_DRIVEN_PATTERNS.md`                  |
| "ordered processing", "exactly-once", "FIFO"                | SQS FIFO                                             | `EVENT_DRIVEN_PATTERNS.md`                  |
| "streaming", "high-throughput events", ">1k/sec"            | Kinesis Data Streams + Firehose                      | `EVENT_DRIVEN_PATTERNS.md`                  |
| "event bus", "microservice events", "routing rules"         | EventBridge Custom Bus                               | `EVENT_DRIVEN_PATTERNS.md`                  |
| "file arrives → trigger", "S3 processing pipeline"          | S3 Event Notifications → EventBridge/SQS             | `EVENT_DRIVEN_PATTERNS.md`                  |
| "DynamoDB change", "change data capture", "CDC"             | DynamoDB Streams → Lambda                            | `EVENT_DRIVEN_PATTERNS.md`                  |
| "relational", "SQL", "transactions", "ACID"                 | Aurora Serverless V2 (PostgreSQL)                    | `LAYER_DATA.md`                             |
| "NoSQL", "key-value", "session", "metadata"                 | DynamoDB                                             | `LAYER_DATA.md`                             |
| "search", "full-text", "faceted"                            | OpenSearch Service                                   | `LAYER_DATA.md`                             |
| "cache", "low latency", "session store"                     | ElastiCache Redis                                    | `LAYER_DATA.md`                             |
| "email", "transactional email"                              | SES + SNS                                            | `LAYER_OBSERVABILITY.md`                    |
| "decouple", "async", "queue"                                | SQS (Standard + DLQ)                                 | `LAYER_DATA.md`                             |
| "frontend", "web app", "React", "Next.js"                   | S3 + CloudFront + WAF + OAI                          | `LAYER_FRONTEND.md`                         |
| "mobile", "iOS", "Android"                                  | API Gateway, Cognito, AppSync                        | `LAYER_API_APPSYNC.md`                      |
| "reporting", "analytics", "BI", "Athena"                    | Athena + Glue + S3 data lake + Firehose              | `EVENT_DRIVEN_PATTERNS.md`                  |
| "synthetic monitoring", "canary", "SLA check"               | CloudWatch Synthetics                                | `OPS_ADVANCED_MONITORING.md`                |
| "backup", "recovery", "RTO/RPO"                             | AWS Backup centralized policy                        | `OPS_ADVANCED_MONITORING.md`                |
| "cost governance", "budget alert", "spend spike"            | Cost Anomaly Detection                               | `OPS_ADVANCED_MONITORING.md`                |
| "third-party API", "webhook", "integration"                 | EventBridge + Lambda + Secrets Manager               | `EVENT_DRIVEN_PATTERNS.md`                  |
| **— MLOps / Data Science —**                                |                                                      |                                             |
| "data lake", "feature engineering", "Glue", "Iceberg"       | S3 4-zone lake + Glue ETL + Athena + Lake Formation  | `MLOPS_DATA_PLATFORM.md`                    |
| "data warehouse", "Redshift", "BI", "analysts"              | Redshift Serverless + Glue catalog                   | `MLOPS_DATA_PLATFORM.md`                    |
| "train model", "SageMaker Pipelines", "ML pipeline"         | SageMaker Studio + Feature Store + Model Registry    | `MLOPS_SAGEMAKER_TRAINING.md`               |
| "model deployment", "inference endpoint", "real-time score" | SageMaker endpoint + auto-scaling + blue-green       | `MLOPS_SAGEMAKER_SERVING.md`                |
| "model drift", "data drift", "model monitor"                | SageMaker Model Monitor + retrain trigger            | `MLOPS_SAGEMAKER_SERVING.md`                |
| "LLM fine-tuning", "LoRA", "QLoRA", "PEFT", "Llama"         | SageMaker GPU training + HuggingFace DLC             | `MLOPS_PIPELINE_LLM_FINETUNING.md`          |
| "NLP", "text classification", "NER", "sentiment", "BERT"    | HuggingFace Transformers on SageMaker                | `MLOPS_PIPELINE_NLP_HUGGINGFACE.md`         |
| "fraud detection", "real-time scoring", "<100ms"            | Feature Store online + XGBoost + FIFO queue          | `MLOPS_PIPELINE_FRAUD_REALTIME.md`          |
| "time series", "demand forecast", "DeepAR", "Prophet"       | SageMaker DeepAR/Chronos + daily forecast Lambda     | `MLOPS_PIPELINE_TIMESERIES.md`              |
| "computer vision", "image detection", "YOLOv8", "OCR"       | GPU endpoint + async inference + SageMaker           | `MLOPS_PIPELINE_COMPUTER_VISION.md`         |
| "recommendations", "collaborative filtering", "Two-Tower"   | Two-Tower model + DynamoDB pre-computed + re-ranking | `MLOPS_PIPELINE_RECOMMENDATIONS.md`         |
| "100 models", "one model per tenant", "SaaS ML"             | SageMaker Multi-Model Endpoint                       | `MLOPS_MULTI_MODEL_ENDPOINT.md`             |
| "batch scoring", "offline predictions", "nightly ML"        | SageMaker Batch Transform + S3 trigger               | `MLOPS_BATCH_TRANSFORM.md`                  |
| "data labeling", "annotation", "Ground Truth"               | SageMaker Ground Truth + private workforce           | `MLOPS_GROUND_TRUTH.md`                     |
| "SHAP", "explainability", "bias detection", "fairness"      | SageMaker Clarify + Macie + 7yr audit bucket         | `MLOPS_CLARIFY_EXPLAINABILITY.md`           |
| **— LLMOps —**                                              |                                                      |                                             |
| "LLM", "generative AI", "chatbot", "Claude", "Bedrock"      | Bedrock API + Guardrails + LLM Gateway Lambda        | `LLMOPS_BEDROCK.md`                         |
| "RAG", "document Q&A", "knowledge base", "retrieval"        | Bedrock Knowledge Bases + OpenSearch vector store    | `LLMOPS_BEDROCK.md`                         |
| "AI agent", "agentic", "multi-step AI", "tool use"          | Bedrock Agents + action group Lambda                 | `LLMOPS_BEDROCK.md`                         |
| "PII in LLM", "content filtering", "safe AI"                | Bedrock Guardrails (PII redaction + topic blocking)  | `LLMOPS_BEDROCK.md`                         |
| **— Enterprise Security —**                                 |                                                      |                                             |
| "WAF", "bot protection", "DDoS", "OWASP", "rate limiting"   | WAF v2 managed rules + Shield Advanced               | `SECURITY_WAF_SHIELD_MACIE.md`              |
| "network firewall", "intrusion detection", "IDS/IPS"        | AWS Network Firewall + domain allowlist              | `SECURITY_WAF_SHIELD_MACIE.md`              |
| "PII scanning", "PHI in S3", "data classification"          | Amazon Macie + Security Hub                          | `SECURITY_WAF_SHIELD_MACIE.md`              |
| "HIPAA", "PCI DSS", "SOC2", "FedRAMP", "compliance audit"   | WORM trail + Config rules + Backup vault lock        | `COMPLIANCE_HIPAA_PCIDSS.md`                |
| "multi-region", "active-active", "global DR", "<1min RTO"   | Global Accelerator + Aurora Global + DynamoDB Global | `GLOBAL_MULTI_REGION.md`                    |
| **— Startup / SaaS Platform —**                             |                                                      |                                             |
| "Kubernetes", "K8s", "EKS", "Helm", "GitOps", "ArgoCD"      | EKS + Karpenter + LBC + External Secrets Operator    | `PLATFORM_EKS_CLUSTER.md`                   |
| "Apache Kafka", "MSK", "Schema Registry", "exactly-once"    | Amazon MSK + Glue Schema Registry (Avro)             | `DATA_MSK_KAFKA.md`                         |
| "Prometheus", "Grafana", "OpenTelemetry", "OTEL", "traces"  | AMP + Managed Grafana + ADOT Lambda layer            | `OBS_OPENTELEMETRY_GRAFANA.md`              |
| "SLO", "error budget", "real user monitoring", "RUM"        | CloudWatch RUM + SLO dashboard + Alertmanager        | `OBS_OPENTELEMETRY_GRAFANA.md`              |

---

## PARTIAL FILES REFERENCE

When generating code, include these partials based on SOW detection:

| Partial                                  | Layer      | Include When                                              |
| ---------------------------------------- | ---------- | --------------------------------------------------------- |
| **— Core (Always) —**                    |            |                                                           |
| `LAYER_NETWORKING.md`                    | L0         | Always                                                    |
| `LAYER_SECURITY.md`                      | L1         | Always                                                    |
| `LAYER_DATA.md`                          | L2         | Always (at minimum: S3 + DynamoDB)                        |
| `LAYER_BACKEND_LAMBDA.md`                | L3A        | Always (at minimum: 1 Lambda)                             |
| `LAYER_OBSERVABILITY.md`                 | L6         | Always                                                    |
| `CICD_PIPELINE_STAGES.md`                | L7         | Always                                                    |
| **— Conditional App Layers —**           |            |                                                           |
| `LAYER_BACKEND_ECS.md`                   | L3B        | Long-running tasks (>15min) detected                      |
| `EVENT_DRIVEN_PATTERNS.md`               | L2/L3      | async, decoupling, events, streaming detected             |
| `WORKFLOW_STEP_FUNCTIONS.md`             | L3         | multi-step workflows, approvals, saga detected            |
| `LAYER_API.md`                           | L4         | REST API detected                                         |
| `LAYER_API_APPSYNC.md`                   | L4         | GraphQL OR real-time subscriptions detected               |
| `LAYER_FRONTEND.md`                      | L5         | Frontend / web app detected                               |
| `OPS_ADVANCED_MONITORING.md`             | L6+        | compliance, backup, canary, cost monitoring detected      |
| **— MLOps / Data Platform —**            |            |                                                           |
| `MLOPS_DATA_PLATFORM.md`                 | Data       | Data lake, Glue ETL, Athena, Redshift detected            |
| `MLOPS_SAGEMAKER_TRAINING.md`            | ML         | Model training, Feature Store, Model Registry detected    |
| `MLOPS_SAGEMAKER_SERVING.md`             | ML         | Model deployment, endpoints, drift monitoring detected    |
| `LLMOPS_BEDROCK.md`                      | LLM        | Bedrock, RAG, LLM Gateway, AI agents detected             |
| **— Specialized SageMaker Pipelines —**  |            |                                                           |
| `MLOPS_PIPELINE_LLM_FINETUNING.md`       | ML         | LLM fine-tuning, LoRA, QLoRA, Llama, Mistral detected     |
| `MLOPS_PIPELINE_NLP_HUGGINGFACE.md`      | ML         | NLP, BERT, text classification, NER, sentiment detected   |
| `MLOPS_PIPELINE_FRAUD_REALTIME.md`       | ML         | Real-time fraud, <100ms latency, Feature Store detected   |
| `MLOPS_PIPELINE_TIMESERIES.md`           | ML         | Time series, demand forecast, DeepAR, Prophet detected    |
| `MLOPS_PIPELINE_COMPUTER_VISION.md`      | ML         | CV, image detection, YOLOv8, segmentation detected        |
| `MLOPS_PIPELINE_RECOMMENDATIONS.md`      | ML         | Recommendations, collaborative filtering detected         |
| `MLOPS_MULTI_MODEL_ENDPOINT.md`          | ML         | SaaS ML, 1 model per tenant, multi-tenant detected        |
| `MLOPS_BATCH_TRANSFORM.md`               | ML         | Batch scoring, nightly predictions, offline ML detected   |
| `MLOPS_CLARIFY_EXPLAINABILITY.md`        | ML         | SHAP, bias, explainability, EU AI Act, HIPAA ML detected  |
| `MLOPS_GROUND_TRUTH.md`                  | ML         | Data labeling, annotation, active learning detected       |
| **— Enterprise Security & Compliance —** |            |                                                           |
| `SECURITY_WAF_SHIELD_MACIE.md`           | Security   | WAF, DDoS, bot protection, PII scanning detected          |
| `COMPLIANCE_HIPAA_PCIDSS.md`             | Compliance | HIPAA, PCI DSS, SOC2, FedRAMP, regulated workload         |
| `GLOBAL_MULTI_REGION.md`                 | Global     | Multi-region, active-active, <1min RTO, global users      |
| **— Startup / SaaS Platform —**          |            |                                                           |
| `PLATFORM_EKS_CLUSTER.md`                | Platform   | Kubernetes, EKS, Helm, GitOps, ArgoCD detected            |
| `DATA_MSK_KAFKA.md`                      | Streaming  | Apache Kafka, MSK, Schema Registry, exactly-once detected |
| `OBS_OPENTELEMETRY_GRAFANA.md`           | Observ.    | Prometheus, Grafana, OpenTelemetry, OTEL, SLO detected    |

---

## OUTPUT FORMAT REQUIREMENTS

All generated code MUST include:

1. **File header comment** with: filename, description, generated date, SOW reference
2. **Section dividers** (# === SECTION NAME ===) for each layer
3. **Inline comments** explaining non-obvious CDK patterns
4. **Type hints** on all Python functions
5. **No hardcoded values** — all configs via environment variables or CDK context
