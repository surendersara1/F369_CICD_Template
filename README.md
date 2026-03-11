# F369 CDK CICD Template Library

A prompt engineering library for **Claude Opus 4.6** that transforms any Statement of Work (SOW) into a complete, deployable **AWS CDK** project with infrastructure, CICD pipeline, and application code.

**You bring a SOW. Claude generates a production-ready AWS project.**

```
Your SOW.md  →  Claude Opus 4.6 + This Library  →  Complete CDK Project  →  cdk deploy
```

---

## How It Works

This library uses a 3-pass generation system:

| Pass | Prompt | Input | Output |
|------|--------|-------|--------|
| 1 | `01_SOW_ARCHITECTURE_DETECTOR.md` | Your SOW | `ARCHITECTURE_MAP.md` — detected AWS services and layers |
| 2A | `02A_APP_STACK_GENERATOR.md` | Architecture Map | `infrastructure/app_stack.py` — all AWS resources |
| 2B | `02B_PIPELINE_STACK_GENERATOR.md` | Architecture Map | `infrastructure/pipeline_stack.py` — CICD pipeline |
| 3 | `03_PROJECT_SCAFFOLD_GENERATOR.md` | Architecture Map + Stacks | Full project: Lambda handlers, tests, Makefile, README |

Or use `MASTER_ORCHESTRATOR.md` to run all passes in one shot.

---

## Repository Structure

```
F369_CICD_Template/
│
├── prompt_templates/                    ← The Library
│   ├── MASTER_ORCHESTRATOR.md           ← One-shot: runs all 3 passes
│   ├── 01_SOW_ARCHITECTURE_DETECTOR.md  ← Pass 1: detect architecture
│   ├── 02A_APP_STACK_GENERATOR.md       ← Pass 2A: generate app stack
│   ├── 02B_PIPELINE_STACK_GENERATOR.md  ← Pass 2B: generate pipeline
│   ├── 03_PROJECT_SCAFFOLD_GENERATOR.md ← Pass 3: generate all files
│   ├── partials/                        ← 35+ CDK code cookbooks
│   ├── schemas/                         ← SOW format guide + JSON schema
│   └── examples/                        ← Sample SOW + architecture map
│
├── Example_Application_Generated/       ← Generated Example Projects
│   ├── RAG_RESEARCH_AGENT/              ← Full RAG agent (Strands + Bedrock)
│   └── Generated_Code_Prompts/          ← SOW prompts used for each example
│       └── RAG_RESEARCH_AGENT.md
│
└── README.md                            ← You are here
```

---

## What the Partials Cover

The library includes 35+ pre-built CDK code blocks ("partials") that Claude assembles based on your SOW:

| Category | Partials |
|----------|----------|
| Core Infrastructure | VPC/Networking, Security (KMS/IAM/CloudTrail), Data (DynamoDB/Aurora/S3/OpenSearch) |
| Compute | Lambda microservices, ECS Fargate workers |
| API | API Gateway REST + Cognito, AppSync GraphQL |
| Frontend | S3 + CloudFront + WAF |
| Patterns | Event-driven (SNS/SQS/EventBridge/Kinesis), Step Functions workflows |
| Observability | CloudWatch, X-Ray, OpenTelemetry + Grafana, Synthetics canaries |
| CICD | Dev → Staging → Prod pipeline with approval gates and rollback |
| AI/ML — LLMOps | Bedrock Knowledge Bases, Guardrails, LLM Gateway |
| AI/ML — Agentic | Strands SDK runtime, AgentCore deploy, streaming chat frontend, eval harness |
| AI/ML — SageMaker | Training, serving, pipelines (NLP, CV, fraud, timeseries, recommendations) |
| AI/ML — Data | Iceberg lakehouse, MSK Kafka, Glue ETL |
| Enterprise | HIPAA/PCI compliance, WAF/Shield/Macie, multi-region DR, EKS |

---

## Quick Start

### Option A: One-Shot Generation

1. Write your SOW (see `prompt_templates/examples/sample_sow.md` for format)
2. Open Claude Opus 4.6
3. Paste `MASTER_ORCHESTRATOR.md` as the system prompt
4. Paste your SOW as the user message
5. Claude generates the complete project

### Option B: Step-by-Step (recommended for complex projects)

1. Paste `01_SOW_ARCHITECTURE_DETECTOR.md` + your SOW → get `ARCHITECTURE_MAP.md`
2. Review and edit the architecture map
3. Paste `02A_APP_STACK_GENERATOR.md` + architecture map → get `app_stack.py`
4. Paste `02B_PIPELINE_STACK_GENERATOR.md` + architecture map → get `pipeline_stack.py`
5. Paste `03_PROJECT_SCAFFOLD_GENERATOR.md` + architecture map + stacks → get full project
6. Run `cdk deploy`

---

## Example: RAG Research Agent

The `Example_Application_Generated/RAG_RESEARCH_AGENT/` folder contains a complete generated project:

- Strands SDK agent with 7 custom tools + multi-agent orchestration
- Bedrock Knowledge Base (OpenSearch Serverless vector store)
- Bedrock Guardrails (PII redaction, topic blocking)
- AgentCore deployment with Gateway and Memory
- Streaming React chat dashboard via WebSocket
- Golden dataset eval pipeline with LLM-as-judge scoring
- 10-stage CICD pipeline with eval quality gate
- 47 files, zero TODOs, production-ready

The SOW prompt that generated it: `Example_Application_Generated/Generated_Code_Prompts/RAG_RESEARCH_AGENT.md`

---

## SOW Keyword Detection

Claude automatically maps keywords in your SOW to AWS services:

| Your SOW mentions... | Claude includes... |
|---|---|
| "authentication", "login", "MFA" | Cognito User Pool |
| "REST API", "HTTP endpoints" | API Gateway + Lambda |
| "GraphQL", "real-time" | AppSync |
| "file upload", "documents" | S3 + CloudFront |
| "background jobs", ">15 minutes" | ECS Fargate + SQS |
| "workflow", "approval" | Step Functions |
| "relational", "SQL" | Aurora Serverless V2 |
| "NoSQL", "key-value" | DynamoDB |
| "LLM", "generative AI", "Bedrock" | Bedrock + Guardrails |
| "RAG", "knowledge base" | Bedrock KB + OpenSearch |
| "Strands SDK", "AI agent" | Strands runtime + AgentCore |
| "multi-agent", "supervisor" | Strands multi-agent |
| "HIPAA", "compliance" | CloudTrail + Config + Backup |
| "multi-region", "DR" | Route53 + Global Accelerator |
| "data lake", "Iceberg" | S3 lake + Glue + Athena |
| "ML pipeline", "SageMaker" | SageMaker Studio + Pipelines |

See `prompt_templates/schemas/sow_input_schema.md` for the full keyword reference.

---

## Tech Stack

- IaC: AWS CDK v2 (Python)
- CICD: CodePipeline (self-mutating)
- Testing: pytest + moto + CDK assertions
- AI Model: Claude Opus 4.6 (prompt execution)
- Target Cloud: AWS

---

## License

Proprietary — F369 Consulting
