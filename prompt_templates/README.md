# 🏗️ AWS CDK CICD Prompt Template Library

**Model Target:** Claude Opus 4.6 | **Domain:** AWS CDK Infrastructure + CICD Pipelines

---

## Overview

This library contains a structured system of prompt templates that Claude Opus 4.6 uses to transform a **Statement of Work (SOW) Markdown file** into a **complete, world-class AWS CDK monorepo** with a multi-stage CICD pipeline.

The system is organized into **three generation passes** — mirroring the three-part structure of `Sample.txt`:

| Pass                                | File Produced         | What It Does                                                     |
| ----------------------------------- | --------------------- | ---------------------------------------------------------------- |
| **Pass 1 — Architecture Detection** | `ARCHITECTURE_MAP.md` | Reads SOW → extracts all AWS components needed across all layers |
| **Pass 2A — App Stack**             | `app_stack.py`        | Generates the CDK `FullSystemStack` with all infra resources     |
| **Pass 2B — Pipeline Stack**        | `pipeline_stack.py`   | Generates the self-mutating CDK pipeline (dev → staging → prod)  |
| **Pass 3 — Full Project Scaffold**  | `project_structure/`  | Generates the complete monorepo folder layout + supporting files |

---

## Directory Structure

```
prompt_templates/
├── README.md                          ← This file
│
├── MASTER_ORCHESTRATOR.md             ← Top-level prompt (run this first)
│
├── 01_SOW_ARCHITECTURE_DETECTOR.md    ← Pass 1: SOW → Architecture Map
├── 02A_APP_STACK_GENERATOR.md         ← Pass 2A: Architecture Map → app_stack.py
├── 02B_PIPELINE_STACK_GENERATOR.md    ← Pass 2B: Architecture Map → pipeline_stack.py
├── 03_PROJECT_SCAFFOLD_GENERATOR.md   ← Pass 3: Full monorepo skeleton
│
├── partials/
│   ├── LAYER_FRONTEND.md              ← Reusable: Frontend CDK constructs (S3+CF+WAF)
│   ├── LAYER_API.md                   ← Reusable: API layer constructs (APIGW+Auth)
│   ├── LAYER_BACKEND_LAMBDA.md        ← Reusable: Lambda microservice patterns
│   ├── LAYER_BACKEND_ECS.md           ← Reusable: ECS Fargate long-running task patterns
│   ├── LAYER_DATA.md                  ← Reusable: Data layer (Aurora/DynamoDB/S3/Redis)
│   ├── LAYER_NETWORKING.md            ← Reusable: VPC, subnets, security groups, NACLs
│   ├── LAYER_SECURITY.md              ← Reusable: IAM, Secrets Manager, KMS, WAF
│   ├── LAYER_OBSERVABILITY.md         ← Reusable: CloudWatch, X-Ray, alarms, dashboards
│   └── CICD_PIPELINE_STAGES.md        ← Reusable: dev/staging/prod approval gates
│
├── schemas/
│   ├── architecture_map_schema.json   ← JSON schema for the architecture map output
│   └── sow_input_schema.md            ← Expected SOW markdown format guide
│
└── examples/
    ├── sample_sow.md                  ← Example SOW input file
    └── sample_architecture_map.md     ← Example architecture map output
```

---

## How To Use

### Step 1 — Prepare Your SOW

Ensure your SOW markdown file follows the format described in `schemas/sow_input_schema.md`.

### Step 2 — Run the Master Orchestrator

Open `MASTER_ORCHESTRATOR.md` and use it as your system prompt in Claude Opus 4.6.
Paste your SOW content as the user message.

### Step 3 — Review the Architecture Map

Claude will output a structured `ARCHITECTURE_MAP.md`. Review and adjust before proceeding.

### Step 4 — Generate Infrastructure Code

Feed the Architecture Map into prompts `02A` and `02B` to generate the CDK stacks.

### Step 5 — Generate Project Scaffold

Use prompt `03` with the Architecture Map to generate the full monorepo structure.

---

## Design Principles

- **Layered Architecture**: Every project is decomposed into Frontend → API → Backend → Data layers
- **Three-Environment Default**: All pipelines generate dev → staging → prod stages
- **Approval Gates**: Manual approval between staging → prod for all production deployments
- **Security-First**: IAM least privilege, KMS encryption, Secrets Manager, WAF included by default
- **Observability**: CloudWatch dashboards, alarms, and X-Ray tracing included by default
