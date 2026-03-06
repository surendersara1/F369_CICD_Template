# 🏗️ AWS CDK CICD Prompt Template Library

**Model Target:** Claude Opus 4.6 | **Domain:** AWS CDK Infrastructure + CICD Pipelines  
**Purpose:** Transform any Statement of Work (SOW) into a complete, deployable AWS CDK monorepo

---

## What Is This?

This folder is a **prompt engineering library** — a structured collection of instructions
for Claude Opus 4.6 that tells it how to generate production-ready AWS infrastructure code.

**You bring:** A Statement of Work (SOW) markdown file describing what you want to build.  
**Claude produces:** A complete AWS CDK project with infrastructure code, CICD pipeline, and all supporting files — ready to `cdk deploy`.

None of the files in this folder run directly. They are **prompts you copy into Claude Opus 4.6**, which then generates your actual project.

---

## The Three File Types

### 1️⃣ Numbered Prompt Files (`01_`, `02A_`, `02B_`, `03_`) — The Actual Prompts

These are the prompts you paste into Claude Opus 4.6. Each one has a specific job.
Run them in order — the output of each step feeds into the next.

| File                               | What You Paste In | What Claude Outputs                   |
| ---------------------------------- | ----------------- | ------------------------------------- |
| `MASTER_ORCHESTRATOR.md`           | Your SOW          | Everything (all 3 passes in one shot) |
| `01_SOW_ARCHITECTURE_DETECTOR.md`  | Your SOW          | `ARCHITECTURE_MAP.md`                 |
| `02A_APP_STACK_GENERATOR.md`       | Architecture Map  | `infrastructure/app_stack.py`         |
| `02B_PIPELINE_STACK_GENERATOR.md`  | Architecture Map  | `infrastructure/pipeline_stack.py`    |
| `03_PROJECT_SCAFFOLD_GENERATOR.md` | Architecture Map  | Full project folder + all files       |

---

### 2️⃣ Partials (`partials/` folder) — Claude's Code Cookbook

These are **pre-written CDK Python code blocks** for every AWS layer.
Claude references these internally while generating your project.
You never use these directly — they are Claude's reference material.

Think of them like a chef's mise en place: prepped ingredients Claude
assembles into your specific dish (project).

| Partial File                             | AWS Services It Covers                                                                                                                                                                                                                                                                                                             |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LAYER_NETWORKING.md`                    | VPC, subnets, NAT Gateways, VPC Endpoints, Security Groups                                                                                                                                                                                                                                                                         |
| `LAYER_SECURITY.md`                      | KMS keys, Secrets Manager, IAM roles, CloudTrail, GuardDuty                                                                                                                                                                                                                                                                        |
| `LAYER_DATA.md`                          | Aurora Serverless V2, DynamoDB, ElastiCache Redis, S3, SQS                                                                                                                                                                                                                                                                         |
| `LAYER_BACKEND_LAMBDA.md`                | Lambda microservices loop, EventBridge schedulers                                                                                                                                                                                                                                                                                  |
| `LAYER_BACKEND_ECS.md`                   | ECS Fargate long-running workers, SQS task triggers                                                                                                                                                                                                                                                                                |
| `EVENT_DRIVEN_PATTERNS.md`               | SNS fan-out, SQS FIFO, EventBridge Bus, Kinesis, DynamoDB Streams, S3 triggers, DLQ redrive                                                                                                                                                                                                                                        |
| `WORKFLOW_STEP_FUNCTIONS.md`             | Step Functions: sequential, parallel, saga, human approval                                                                                                                                                                                                                                                                         |
| `LAYER_API.md`                           | API Gateway REST, Cognito User Pool (MFA), JWT authorizers                                                                                                                                                                                                                                                                         |
| `LAYER_API_APPSYNC.md`                   | AppSync GraphQL, real-time subscriptions, VTL resolvers                                                                                                                                                                                                                                                                            |
| `LAYER_FRONTEND.md`                      | S3 + CloudFront + WAF + security headers + OAI                                                                                                                                                                                                                                                                                     |
| `LAYER_OBSERVABILITY.md`                 | CloudWatch alarms, dashboards, X-Ray tracing, SNS alerts                                                                                                                                                                                                                                                                           |
| `OPS_ADVANCED_MONITORING.md`             | Synthetics canaries, AWS Config rules, AWS Backup, Cost Anomaly Detection, SSM Parameter Store                                                                                                                                                                                                                                     |
| `CICD_PIPELINE_STAGES.md`                | Dev → Staging → Prod pipeline stages, approval gates, rollback                                                                                                                                                                                                                                                                     |
| `MLOPS_DATA_PLATFORM.md`                 | S3 4-zone data lake (raw/processed/curated/features), Glue ETL (Iceberg), Athena, Lake Formation governance, Redshift Serverless, EMR Serverless Spark                                                                                                                                                                             |
| `MLOPS_SAGEMAKER_TRAINING.md`            | SageMaker Studio Domain (VPC-only), Feature Store (online+offline), Model Registry, MLflow tracking server, Pipeline trigger Lambda, spot instance training, 3-domain ML env                                                                                                                                                       |
| `MLOPS_SAGEMAKER_SERVING.md`             | Real-time endpoints with A/B multi-variant, serverless inference, auto-scaling, blue-green deploy with auto-rollback, Model Monitor data drift detection                                                                                                                                                                           |
| `LLMOPS_BEDROCK.md`                      | Bedrock Knowledge Bases RAG (OpenSearch vector store), Bedrock Agents, Guardrails (PII redaction, topic blocking, grounding), LLM Gateway, prompt registry, token cost observability                                                                                                                                               |
| **— Enterprise Data Lakehouse —**        |                                                                                                                                                                                                                                                                                                                                    |
| `DATA_LAKEHOUSE_ICEBERG.md`              | S3 5-zone lake (raw/processed/curated/served/audit), Apache Iceberg ACID tables, Athena v3 DML (MERGE INTO + time travel), Redshift Serverless + Spectrum federated queries, Lake Formation column/row security, Glue 4.0 Spark ETL with Iceberg connector, Glue Data Quality rules, hourly incremental pipeline, S3 event trigger |
| **— Specialized SageMaker Pipelines —**  |                                                                                                                                                                                                                                                                                                                                    |
| `MLOPS_PIPELINE_LLM_FINETUNING.md`       | LLM fine-tuning with LoRA/QLoRA (Llama, Mistral, Falcon), HuggingFace DLC, SageMaker GPU training (g5.2xlarge), evaluation Lambda, model registration to Model Registry                                                                                                                                                            |
| `MLOPS_PIPELINE_NLP_HUGGINGFACE.md`      | NLP pipeline (text classification, NER, sentiment, summarization, embeddings) using HuggingFace Transformers on SageMaker, BERT/RoBERTa/DistilBERT, async and real-time endpoints                                                                                                                                                  |
| `MLOPS_PIPELINE_FRAUD_REALTIME.md`       | Real-time fraud detection (<100ms), Feature Store online store, XGBoost/LightGBM scoring Lambda with provisioned concurrency, FIFO review queue, hourly feature engineering                                                                                                                                                        |
| `MLOPS_PIPELINE_TIMESERIES.md`           | Time series forecasting (DeepAR, Chronos, Prophet, TFT), daily forecast Lambda, weekly retraining, multi-product support, S3 forecast archive                                                                                                                                                                                      |
| `MLOPS_PIPELINE_COMPUTER_VISION.md`      | Computer vision pipeline (object detection/segmentation/classification, YOLOv8), async inference endpoint for large images, GPU training on g5 instances                                                                                                                                                                           |
| `MLOPS_PIPELINE_RECOMMENDATIONS.md`      | Recommender system (Two-Tower model, collaborative filtering), hybrid pre-computed + real-time re-ranking Lambda, nightly batch transform, DynamoDB result store                                                                                                                                                                   |
| `MLOPS_MULTI_MODEL_ENDPOINT.md`          | Multi-Model Endpoint hosting 100s of models on one endpoint, MME router Lambda for per-tenant model routing, model upload Lambda — reduces SaaS ML hosting cost by 90%                                                                                                                                                             |
| `MLOPS_BATCH_TRANSFORM.md`               | Large-scale offline scoring with SageMaker Batch Transform, S3 trigger Lambda, post-processing Lambda, failed-job CloudWatch alarm                                                                                                                                                                                                 |
| `MLOPS_CLARIFY_EXPLAINABILITY.md`        | SageMaker Clarify bias detection (pre/post training) and SHAP explainability, 7-year audit bucket for compliance, bias violation CloudWatch alarm                                                                                                                                                                                  |
| `MLOPS_GROUND_TRUTH.md`                  | SageMaker Ground Truth data labeling with private workforce (Cognito), active learning (auto-labeling), integration hook to kick off training pipeline on job completion                                                                                                                                                           |
| **— Enterprise / Regulated Workloads —** |                                                                                                                                                                                                                                                                                                                                    |
| `SECURITY_WAF_SHIELD_MACIE.md`           | WAF v2 managed rules (OWASP Top 10, Bot Control, ATP account takeover, geo-block, IP rate limit), Shield Advanced DDoS, Network Firewall (AWS threat intel + domain allowlist), Macie PII/PHI scanning, Security Hub                                                                                                               |
| `COMPLIANCE_HIPAA_PCIDSS.md`             | WORM audit trail (S3 Object Lock), 19 AWS Config managed rules, Backup vault lock (7-year retention), Inspector v2 auto-enable, weekly evidence collector Lambda — supports HIPAA / PCI DSS / SOC2                                                                                                                                 |
| `GLOBAL_MULTI_REGION.md`                 | AWS Global Accelerator anycast IPs, Route53 health check failover, Aurora Global Database (<1s RPO), DynamoDB Global Tables (active-active multi-master), S3 CRR, regional health Lambda, replication lag alarm                                                                                                                    |
| **— Startup / SaaS Platform —**          |                                                                                                                                                                                                                                                                                                                                    |
| `PLATFORM_EKS_CLUSTER.md`                | EKS 1.31 private cluster, Karpenter autoscaler (spot + on-demand, 30s provision), AWS Load Balancer Controller ALB Ingress, External Secrets Operator, EBS CSI gp3 encrypted storage                                                                                                                                               |
| `DATA_MSK_KAFKA.md`                      | Amazon MSK Kafka 3.6 (IAM auth + TLS + KMS encryption), Glue Schema Registry (Avro + backward compatibility), Kafka admin Lambda, disk/CPU/consumer-lag alarms, broker sizing guide                                                                                                                                                |
| `OBS_OPENTELEMETRY_GRAFANA.md`           | Amazon Managed Prometheus (SLO recording rules + error budgets), Managed Grafana with SSO, ADOT Lambda layer (zero-code tracing), CloudWatch RUM (Core Web Vitals), SLO error budget dashboard, Alertmanager → PagerDuty                                                                                                           |

---

### 3️⃣ Supporting Files

| File                                   | Purpose                                                                  |
| -------------------------------------- | ------------------------------------------------------------------------ |
| `schemas/sow_input_schema.md`          | Format guide for writing your SOW (what keywords trigger which services) |
| `schemas/architecture_map_schema.json` | JSON schema to validate the architecture map output                      |
| `examples/sample_sow.md`               | A complete example SOW (MedFlow healthcare platform)                     |
| `examples/sample_architecture_map.md`  | The architecture map that Pass 1 produces for the sample SOW             |

---

## How It All Connects — The Full Flow

```
YOUR SOW.md
    │
    │  Paste into Claude with prompt 01
    ▼
┌──────────────────────────────────────┐
│  01_SOW_ARCHITECTURE_DETECTOR.md    │
│                                      │
│  Claude reads your SOW and detects:  │
│    → What AWS services you need      │
│    → How many microservices          │
│    → Which data stores               │
│    → Dev vs prod configuration       │
└─────────────────┬────────────────────┘
                  │ Claude outputs
                  ▼
         ARCHITECTURE_MAP.md           ← ⚠️ REVIEW THIS before moving on
         (you can edit it here)
                  │
         ┌────────┴──────────────────┐
         │                           │
   Paste into 02A             Paste into 02B
         │                           │
         ▼                           ▼
┌──────────────────┐       ┌───────────────────────┐
│ 02A_APP_STACK_   │       │ 02B_PIPELINE_STACK_   │
│ GENERATOR        │       │ GENERATOR              │
│                  │       │                        │
│ Claude uses the  │       │ Claude generates the   │
│ partials cookbook│       │ self-mutating CodePipe │
│ to write your    │       │ line with:             │
│ app_stack.py     │       │  - Dev auto-deploy     │
│ with all layers: │       │  - Integration tests   │
│  L0 Networking   │       │  - Staging auto-deploy │
│  L1 Security     │       │  - Manual approval     │
│  L2 Data         │       │  - Prod deploy         │
│  L3 Backend      │       │  - Rollback alarms     │
│  L4 API          │       └────────────┬───────────┘
│  L5 Frontend     │                    │
│  L6 Observability│             pipeline_stack.py
└────────┬─────────┘
         │
   app_stack.py
         │
         └──────────────┬──────────────────┘
                        │
                  Paste both + Architecture Map
                  into prompt 03
                        │
                        ▼
         ┌──────────────────────────────┐
         │  03_PROJECT_SCAFFOLD_        │
         │  GENERATOR                  │
         │                              │
         │  Claude generates every      │
         │  other file in the project:  │
         │    app.py                    │
         │    cdk.json                  │
         │    Makefile                  │
         │    requirements.txt          │
         │    src/service_1/index.py    │
         │    src/service_N/index.py    │
         │    src/workers/Dockerfile    │
         │    tests/unit/...            │
         │    tests/smoke/...           │
         │    README.md                 │
         └──────────────┬───────────────┘
                        │
                        ▼
              YOUR COMPLETE PROJECT
              └── Run: cdk deploy
              └── AWS infrastructure live
```

---

## When to Use MASTER vs Individual Prompts

| Situation                                           | Use                                                       |
| --------------------------------------------------- | --------------------------------------------------------- |
| First time, any project                             | `MASTER_ORCHESTRATOR.md` — runs all passes in one shot    |
| Large or complex SOW (> 5 pages)                    | Run `01` → review map → run `02A`, `02B`, `03` separately |
| You only need to regenerate infra (not pipeline)    | Just `02A`                                                |
| You only need to regenerate the pipeline            | Just `02B`                                                |
| Adding a new microservice to an existing project    | Just `02A` + `03` (for the new service folder)            |
| You want to review the architecture before any code | Just `01` first                                           |

---

## What the Generated Project Looks Like

```
your-project-name/
├── app.py                        CDK app entry point
├── cdk.json                      CDK config + context variables
├── requirements.txt              aws-cdk-lib, constructs (pinned versions)
├── requirements-dev.txt          pytest, moto, black, bandit, mypy
├── Makefile                      make install / deploy / test / lint
├── .gitignore
├── README.md                     Project-specific documentation
│
├── infrastructure/
│   ├── app_stack.py              ← Generated by 02A (all your AWS infra)
│   ├── pipeline_stack.py         ← Generated by 02B (CICD pipeline)
│   └── app_stage.py              CDK Stage wrapper
│
├── src/
│   ├── auth_service/             One folder per detected microservice
│   │   ├── index.py              Lambda handler
│   │   ├── requirements.txt
│   │   └── tests/
│   │       └── test_handler.py
│   ├── patient_list/
│   ├── ... (N services)
│   └── workers/
│       └── pdf_generator/
│           ├── main.py           Background ECS worker polling SQS
│           ├── Dockerfile        Multi-stage, non-root, ARM64
│           └── requirements.txt
│
└── tests/
    ├── unit/test_app_stack.py    CDK assertions (checks your stack is defined correctly)
    ├── integration/              Boto3 tests against live dev environment
    ├── smoke/test_smoke.py       Post-deploy health checks (dev/staging/prod)
    └── conftest.py               pytest fixtures with moto mocking
```

---

## SOW Keyword → AWS Service Detection

Claude automatically detects which AWS services you need based on keywords in your SOW:

| If your SOW mentions...                                                  | Claude includes...                                                   |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| "authentication", "login", "MFA"                                         | Cognito User Pool with enforced MFA                                  |
| "REST API", "HTTP endpoints"                                             | API Gateway + Lambda + Cognito authorizer                            |
| "GraphQL", "real-time", "subscriptions"                                  | AppSync + DynamoDB resolvers                                         |
| "file upload", "documents", "media"                                      | S3 + CloudFront signed URLs + virus scan queue                       |
| "background jobs", ">15 minutes"                                         | ECS Fargate + SQS trigger Lambda                                     |
| "workflow", "multi-step approval"                                        | Step Functions with `.waitForTaskToken`                              |
| "decouple", "pub/sub", "fan-out"                                         | SNS → SQS fan-out pattern                                            |
| "ordered processing", "FIFO"                                             | SQS FIFO with deduplication                                          |
| "streaming", ">1000 events/sec"                                          | Kinesis Data Streams + Firehose                                      |
| "scheduled", "cron", "nightly"                                           | EventBridge Scheduler + Lambda                                       |
| "relational", "SQL", "transactions"                                      | Aurora Serverless V2 (PostgreSQL)                                    |
| "NoSQL", "key-value", "metadata"                                         | DynamoDB                                                             |
| "cache", "low latency"                                                   | ElastiCache Redis                                                    |
| "frontend", "React", "Next.js", "web app"                                | S3 + CloudFront + WAF + security headers                             |
| "HIPAA", "SOC2", "compliance", "audit"                                   | CloudTrail + GuardDuty + AWS Config + AWS Backup                     |
| "SLA monitoring", "canary", "uptime"                                     | CloudWatch Synthetics                                                |
| "backup", "RTO", "RPO"                                                   | AWS Backup with vault lock + 7yr retention                           |
| "cost governance", "budget"                                              | Cost Anomaly Detection                                               |
| "multi-region", "DR", "global"                                           | Route53 + Global Accelerator + S3 CRR                                |
| "data science", "feature engineering", "data lake"                       | S3 4-zone lake + Glue ETL (Iceberg) + Athena + Lake Formation        |
| "data warehouse", "BI", "QuickSight", "analysts"                         | Redshift Serverless + Athena + Glue catalog                          |
| "large-scale ETL", "Spark", "PySpark", "petabyte"                        | EMR Serverless                                                       |
| "train model", "ML pipeline", "SageMaker Pipelines"                      | SageMaker Studio + Feature Store + Model Registry + Pipeline trigger |
| "experiment tracking", "MLflow", "hyperparameter tuning"                 | MLflow on SageMaker + Experiments + HPO Jobs                         |
| "model deployment", "inference endpoint", "real-time scoring"            | SageMaker real-time endpoint + auto-scaling + blue-green deploy      |
| "model monitoring", "data drift", "model decay", "training-serving skew" | SageMaker Model Monitor + drift alarms + retraining trigger          |
| "A/B test models", "champion/challenger", "shadow mode"                  | SageMaker multi-variant endpoint with traffic weights                |
| "LLM", "generative AI", "chatbot", "Claude", "Bedrock"                   | Bedrock API + Guardrails + LLM Gateway Lambda                        |
| "RAG", "document Q&A", "knowledge base", "retrieval"                     | Bedrock Knowledge Bases + OpenSearch Serverless vector store         |
| "AI agent", "agentic", "multi-step AI", "tool use"                       | Bedrock Agents + action group Lambda                                 |
| "prompt management", "prompt versioning", "A/B test prompts"             | Prompt Registry (DynamoDB) + SSM parameter store                     |
| "PII in LLM", "content filtering", "safe AI"                             | Bedrock Guardrails (PII redaction + topic blocking + grounding)      |
| "LLM cost", "token tracking", "AI spend"                                 | LLM Gateway Lambda with token metering + cost alarm                  |
| "WAF", "bot protection", "DDoS", "rate limiting", "OWASP"                | WAF v2 (OWASP + Bot Control + ATP) + Shield Advanced                 |
| "intrusion detection", "network firewall", "egress filtering"            | AWS Network Firewall + GuardDuty                                     |
| "PII scanning", "PHI in S3", "data classification"                       | Amazon Macie with custom data identifiers                            |
| "security findings", "Security Hub", "posture management"                | AWS Security Hub + Inspector v2                                      |
| "multi-region", "active-active", "global DR", "<1 minute RTO"            | Global Accelerator + Aurora Global DB + DynamoDB Global Tables       |
| "Kubernetes", "K8s", "EKS", "Helm", "GitOps", "ArgoCD"                   | EKS cluster + Karpenter + External Secrets + AWS LBC                 |
| "Apache Kafka", "MSK", "Schema Registry", "exactly-once"                 | Amazon MSK + Glue Schema Registry (Avro/Protobuf)                    |
| "Prometheus", "Grafana", "OpenTelemetry", "OTEL", "distributed tracing"  | AMP + Managed Grafana + ADOT Lambda layer                            |
| "real user monitoring", "Core Web Vitals", "frontend errors", "RUM"      | CloudWatch RUM with session recording                                |
| "SLO", "error budget", "SLI", "reliability engineering"                  | AMP recording rules + Grafana SLO dashboard + Alertmanager           |

---

## Pipeline Design (What 02B Generates)

```
Git push to 'main' branch
    │
    ▼
[SOURCE] Pull code from CodeCommit/GitHub
    │
    ▼
[BUILD] CodeBuild
    • pip install dependencies
    • bandit security scan (blocks if critical finding)
    • pytest tests/unit/ (blocks if tests fail)
    • cdk synth (validates CDK compiles)
    │
    ▼ (auto)
[DEV DEPLOY] cdk deploy → Dev environment
    + Smoke tests run immediately after
    │
    ▼ (auto)
[INTEGRATION TESTS] pytest tests/integration/ against live Dev
    │
    ▼ (auto, if tests pass)
[STAGING DEPLOY] cdk deploy → Staging environment
    + Performance baseline tests
    │
    ▼
[MANUAL APPROVAL] ⚠️  Email sent to approvers via SNS
    │   Human reviews in AWS Console and clicks Approve
    ▼ (after human approves)
[PROD DEPLOY] cdk deploy → Production
    + Smoke tests
    + CloudWatch rollback alarm monitoring
```

---

## MLOps / LLMOps / AIOps Coverage

This library has **full MLOps, LLMOps, and AIOps coverage** via 4 dedicated partials.
Claude automatically includes them when it detects ML/AI keywords in your SOW.

### The Four MLOps/LLMOps Partials

| Partial                       | What It Covers                                                                                                                                                                                            | SOW Keywords That Trigger It                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `MLOPS_DATA_PLATFORM.md`      | **Data Foundation** — S3 4-zone lake, Glue ETL (Iceberg format), Athena serverless SQL, Lake Formation column-level governance, Redshift Serverless DW, EMR Serverless Spark                              | "data science", "feature engineering", "data lake", "analytics"                     |
| `MLOPS_SAGEMAKER_TRAINING.md` | **Training Platform** — Studio Domain (VPC-only), Feature Store (online + offline), Model Registry, MLflow tracking, Pipeline trigger Lambda, spot instance training (90% cheaper), 3-domain env strategy | "train model", "ML pipeline", "experiments", "model registry", "MLflow"             |
| `MLOPS_SAGEMAKER_SERVING.md`  | **Production Serving** — Real-time endpoints, A/B multi-variant, serverless inference, auto-scaling, blue-green deploy with auto-rollback, Model Monitor drift detection                                  | "inference endpoint", "model deployment", "drift monitoring", "champion/challenger" |
| `LLMOPS_BEDROCK.md`           | **LLMOps** — Bedrock RAG (OpenSearch vector store + Knowledge Bases), Agents + action groups, Guardrails (PII redaction, topic blocking, grounding), LLM Gateway, prompt registry, token cost dashboard   | "LLM", "RAG", "chatbot", "Bedrock", "generative AI", "agents"                       |

---

### The 3-Domain ML Environment Strategy

> ⚠️ **This is DIFFERENT from software dev/staging/prod.** ML projects need a three-domain structure designed around the ML lifecycle:

| Domain                | Who Uses It     | What Happens Here                                                                      | Approval Required?                                      |
| --------------------- | --------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| **Data Science (DS)** | Data scientists | Free exploration, EDA, prototyping, any experiment, no governance                      | ❌ None — open sandbox                                  |
| **ML Staging**        | ML Engineers    | Validated training pipelines, systematic evaluation, A/B test setup, integration tests | ✅ ML Engineer review                                   |
| **ML Production**     | Model Ops team  | Only approved models (via Model Registry) serve live traffic, monitored 24/7           | ✅ Model Committee approval in SageMaker Model Registry |

**How it connects to software pipelines:**

```
Software CICD:    Dev ──────────────► Staging ──────► Prod
                   ↑                                    ↑
ML Serving:    DS Domain ──► ML Staging ──► Model Registry Approval ──► Serving Endpoint
ML Training:   Studio (any) → Pipeline Run → Evaluation → Pending → Approved → Deployed
```

---

### ML Pipeline Design (SageMaker Pipelines)

```
Scheduled Trigger (EventBridge) OR Manual trigger
    │
    ▼
[Step 1: Feature Engineering]
  ProcessingJob (SKLearn/Pandas)
  Source: S3 raw zone → Output: Feature Store + processed zone
    │
    ▼
[Step 2: Model Training]
  TrainingJob (Spot instances — up to 90% cheaper)
  Algorithm: XGBoost / PyTorch / Hugging Face / custom container
  Checkpoints saved to S3 (resume if Spot interrupted)
    │
    ▼
[Step 3: Model Evaluation]
  ProcessingJob calculates: Accuracy, AUC, F1, RMSE, SHAP values
    │
    ▼
[Step 4: Accuracy Gate (Condition Step)]
  IF accuracy >= threshold → Register model
  IF below threshold       → Pipeline stops (no bad model registered)
    │
    ▼ (if passes)
[Step 5: Register in Model Registry]
  Status: PendingManualApproval
  ML Engineer reviews metrics in Studio → clicks Approve/Reject
    │
    ▼ (on Approve — triggered by EventBridge)
[Model Deployer Lambda]
  Blue-green traffic shift: 10% → 50% → 100%
  Auto-rollback if endpoint error alarm fires
    │
    ▼
[Model Monitor — runs daily]
  Checks data distribution vs training baseline
  If drift detected → CloudWatch alarm → triggers retraining
```

---

### LLMOps Architecture (Bedrock)

```
Documents (S3) ──► Bedrock Knowledge Base ──► OpenSearch Vector Store
                         │  (chunked, embedded)
                         │
User Query ──► LLM Gateway Lambda ──► Guardrails (PII check, topic block)
                         │                    │ (if safe)
                         ▼                    ▼
               Bedrock RAG (Retrieve + Generate)
                  1. Embed query (Titan Embed V2)
                  2. Vector similarity search in OpenSearch
                  3. Retrieved chunks → Claude Sonnet 3.5
                  4. Grounded response (verified by Guardrail)
                         │
                         ▼
               Response to user
               + Token usage logged → Cost alarm if spike
               + Guardrail violations → Security alarm
```

---

## Enterprise / Regulated Workload Coverage

Use **three dedicated partials** when SOW mentions compliance, DDoS, PII, or global reach:

| Partial                        | What Gets Deployed                                                                                                                                    | Compliance Addressed                   |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `SECURITY_WAF_SHIELD_MACIE.md` | WAF v2 (OWASP + Bot Control + ATP + geo-block) → Shield Advanced → Network Firewall (threat intel + domain allowlist) → Macie PII scan → Security Hub | PCI DSS 6.6, HIPAA § 164.312, SOC2 CC6 |
| `COMPLIANCE_HIPAA_PCIDSS.md`   | WORM S3 Object Lock (6yr HIPAA / 1yr PCI) → 19 Config rules → Backup vault lock → Inspector v2 → weekly evidence collector                            | HIPAA, PCI DSS 10.5, SOC2 CC7          |
| `GLOBAL_MULTI_REGION.md`       | Global Accelerator → Route53 failover → Aurora Global DB → DynamoDB Global → S3 CRR                                                                   | Business continuity, data residency    |

### Defense-in-Depth Stack

```
Internet Traffic
      │
      ▼  Layer 1: Shield Advanced (L3/L4 DDoS volumetric)
      ▼  Layer 2: WAF v2 (L7 HTTP — OWASP, bots, credential stuffing)
      ▼  Layer 3: CloudFront (edge caching, TLS termination)
      ▼  Layer 4: Network Firewall (VPC-level stateful, IDS/IPS, domain block)
      ▼  Layer 5: Security Groups (port-level, least privilege)
      ▼  Layer 6: VPC Private Subnets (no direct internet exposure)
      ▼  App: Lambda / ECS / EKS

Data Protection:
  Macie   → Scan S3 for PII/PHI automatically
  Config  → 19 rules alerting on any drift from compliance baseline
  Backup  → Immutable vault-locked backups (cannot be deleted, even by root)
```

---

## Startup / SaaS Platform Coverage

Three partials replace entire managed platform engineering stacks:

| Partial                        | What Gets Deployed                                           | Replaces                                   |
| ------------------------------ | ------------------------------------------------------------ | ------------------------------------------ |
| `PLATFORM_EKS_CLUSTER.md`      | EKS 1.31 + Karpenter + AWS LBC + External Secrets + EBS CSI  | Self-managed K8s, kops, Rancher            |
| `DATA_MSK_KAFKA.md`            | MSK Kafka 3.6 + Glue Schema Registry + admin Lambda + alarms | Self-managed Confluent, self-managed Kafka |
| `OBS_OPENTELEMETRY_GRAFANA.md` | AMP + Managed Grafana + ADOT Lambda + RUM + SLO dashboard    | Self-managed Grafana/Prometheus stacks     |

### Karpenter vs Cluster Autoscaler

```
Cluster Autoscaler (old):          Karpenter (new — 2024 default):
  Provision time: 3-5 minutes        Provision time: < 30 seconds
  Bin-packing:    Manual             Bin-packing:    Automatic (WhenUnderutilized)
  Node rotation:  Manual             Node rotation:  Automatic (expireAfter: 720h)
  Spot support:   Basic              Spot support:   First-class (fallback chain)
  Cost savings:   10-20%             Cost savings:   Up to 70% with mixed instances
```

### MSK vs Kinesis Decision

```
Use MSK (Kafka) when:              Use Kinesis when:
  Team knows Kafka                   Team prefers Lambda + managed
  Schema Registry needed             < 7 day retention OK
  Kafka Connect ecosystem            Cost at low scale matters
  Exactly-once semantics critical    Simple Lambda fan-out needed
  Long retention (weeks/months)
```

---

## Design Principles

- **Layered Architecture** — Every project decomposes into 8 ordered layers (Networking → Security → Data → Backend → API → Frontend → Observability → CICD)
- **Single `stage_name` Parameter** — One CDK class, three environments. No copy-paste stacks.
- **Microservices as a Loop** — All Lambda functions defined in a list, created in one `for` loop
- **`grant_*` Only** — All IAM permissions use CDK's built-in grant methods — never raw `*` policies
- **ECS for Long-Running** — SOW keywords like "heavy processing", ">15 min", "report generation" auto-route to ECS Fargate
- **Security by Default** — KMS encryption, VPC isolation, WAF, MFA, Secrets Manager on every project
- **Compliance-Ready** — CloudTrail, GuardDuty, AWS Config, AWS Backup included when SOW mentions HIPAA/SOC2
- **Self-Mutating Pipeline** — Push new CDK code and the pipeline updates itself automatically
- **MLOps 3-Domain Environments** — ML uses DS / Staging / Prod domains; not the same as software dev/staging/prod
- **Model Approval Gate** — No model reaches production without explicit approval in SageMaker Model Registry
- **Spot Training by Default** — All SageMaker training jobs use Spot instances (up to 90% cheaper) with checkpoint resume
- **Drift → Retrain Loop** — Model Monitor triggers retraining Lambda automatically when data drift is detected
- **LLM Gateway Pattern** — All Bedrock calls go through a single Lambda for rate limiting, cost tracking, caching, and guardrails
- **LLM Cost Observability** — Token usage and estimated cost tracked per request, with CloudWatch alarm on hourly spend spikes
- **Defense-in-Depth Security** — 6 layers: Shield → WAF → CloudFront → Network Firewall → Security Groups → Private Subnets
- **WORM Audit Trails** — Compliance logs use S3 Object Lock (tamper-proof, immutable, even root can't delete)
- **Zero Trust Secrets** — Kubernetes uses External Secrets Operator — secrets never in YAML, always synced from Secrets Manager
- **Karpenter over CA** — EKS uses Karpenter (30s provision, auto bin-packing, 70% cost saving) not Cluster Autoscaler
- **Schema Registry First** — Kafka topics always paired with Avro/Protobuf schema in Glue Schema Registry (backward compatibility enforced)
- **SLO Error Budgets** — Alerts fire on error budget burn rate, not just raw error counts — reduces alert fatigue
- **Active-Active by Design** — Multi-region uses DynamoDB Global Tables + Aurora Global DB, not primary/replica hot-standby
- **OpenTelemetry as Standard** — All services emit traces/metrics/logs via ADOT — no vendor lock-in on observability
