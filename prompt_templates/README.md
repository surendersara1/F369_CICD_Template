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

| Partial File                 | AWS Services It Covers                                                                         |
| ---------------------------- | ---------------------------------------------------------------------------------------------- |
| `LAYER_NETWORKING.md`        | VPC, subnets, NAT Gateways, VPC Endpoints, Security Groups                                     |
| `LAYER_SECURITY.md`          | KMS keys, Secrets Manager, IAM roles, CloudTrail, GuardDuty                                    |
| `LAYER_DATA.md`              | Aurora Serverless V2, DynamoDB, ElastiCache Redis, S3, SQS                                     |
| `LAYER_BACKEND_LAMBDA.md`    | Lambda microservices loop, EventBridge schedulers                                              |
| `LAYER_BACKEND_ECS.md`       | ECS Fargate long-running workers, SQS task triggers                                            |
| `EVENT_DRIVEN_PATTERNS.md`   | SNS fan-out, SQS FIFO, EventBridge Bus, Kinesis, DynamoDB Streams, S3 triggers, DLQ redrive    |
| `WORKFLOW_STEP_FUNCTIONS.md` | Step Functions: sequential, parallel, saga, human approval                                     |
| `LAYER_API.md`               | API Gateway REST, Cognito User Pool (MFA), JWT authorizers                                     |
| `LAYER_API_APPSYNC.md`       | AppSync GraphQL, real-time subscriptions, VTL resolvers                                        |
| `LAYER_FRONTEND.md`          | S3 + CloudFront + WAF + security headers + OAI                                                 |
| `LAYER_OBSERVABILITY.md`     | CloudWatch alarms, dashboards, X-Ray tracing, SNS alerts                                       |
| `OPS_ADVANCED_MONITORING.md` | Synthetics canaries, AWS Config rules, AWS Backup, Cost Anomaly Detection, SSM Parameter Store |
| `CICD_PIPELINE_STAGES.md`    | Dev → Staging → Prod pipeline stages, approval gates, rollback                                 |

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

| If your SOW mentions...                   | Claude includes...                               |
| ----------------------------------------- | ------------------------------------------------ |
| "authentication", "login", "MFA"          | Cognito User Pool with enforced MFA              |
| "REST API", "HTTP endpoints"              | API Gateway + Lambda + Cognito authorizer        |
| "GraphQL", "real-time", "subscriptions"   | AppSync + DynamoDB resolvers                     |
| "file upload", "documents", "media"       | S3 + CloudFront signed URLs + virus scan queue   |
| "background jobs", ">15 minutes"          | ECS Fargate + SQS trigger Lambda                 |
| "workflow", "multi-step approval"         | Step Functions with `.waitForTaskToken`          |
| "decouple", "pub/sub", "fan-out"          | SNS → SQS fan-out pattern                        |
| "ordered processing", "FIFO"              | SQS FIFO with deduplication                      |
| "streaming", ">1000 events/sec"           | Kinesis Data Streams + Firehose                  |
| "scheduled", "cron", "nightly"            | EventBridge Scheduler + Lambda                   |
| "relational", "SQL", "transactions"       | Aurora Serverless V2 (PostgreSQL)                |
| "NoSQL", "key-value", "metadata"          | DynamoDB                                         |
| "cache", "low latency"                    | ElastiCache Redis                                |
| "frontend", "React", "Next.js", "web app" | S3 + CloudFront + WAF + security headers         |
| "HIPAA", "SOC2", "compliance", "audit"    | CloudTrail + GuardDuty + AWS Config + AWS Backup |
| "SLA monitoring", "canary", "uptime"      | CloudWatch Synthetics                            |
| "backup", "RTO", "RPO"                    | AWS Backup with vault lock + 7yr retention       |
| "cost governance", "budget"               | Cost Anomaly Detection                           |
| "multi-region", "DR", "global"            | Route53 + Global Accelerator + S3 CRR            |

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

## Design Principles

- **Layered Architecture** — Every project decomposes into 8 ordered layers (Networking → Security → Data → Backend → API → Frontend → Observability → CICD)
- **Single `stage_name` Parameter** — One CDK class, three environments. No copy-paste stacks.
- **Microservices as a Loop** — All Lambda functions defined in a list, created in one `for` loop
- **`grant_*` Only** — All IAM permissions use CDK's built-in grant methods — never raw `*` policies
- **ECS for Long-Running** — SOW keywords like "heavy processing", ">15 min", "report generation" auto-route to ECS Fargate
- **Security by Default** — KMS encryption, VPC isolation, WAF, MFA, Secrets Manager on every project
- **Compliance-Ready** — CloudTrail, GuardDuty, AWS Config, AWS Backup included when SOW mentions HIPAA/SOC2
- **Self-Mutating Pipeline** — Push new CDK code and the pipeline updates itself automatically
