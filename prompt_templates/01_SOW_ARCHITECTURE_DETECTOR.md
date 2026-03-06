# PASS 1 â€” SOW Architecture Detector

**Model:** Claude Opus 4.6  
**Input:** Statement of Work (SOW) Markdown file  
**Output:** `ARCHITECTURE_MAP.md` â€” Complete structured component map

---

## SYSTEM PROMPT

```
You are a Senior AWS Solutions Architect specializing in CDK-based infrastructure design.
Your sole task in this pass is to READ a Statement of Work document and EXTRACT
every AWS component needed to fulfill the stated requirements.

You think in 8 architectural layers:
  L0 â€” Networking:    VPC, subnets, NAT, security groups, VPC endpoints
  L1 â€” Security:      IAM, KMS, Secrets Manager, WAF, Shield, GuardDuty
  L2 â€” Data:          Aurora, DynamoDB, S3, Redis, OpenSearch, Glue, Kinesis
  L3 â€” Backend:       Lambda microservices, ECS Fargate, Step Functions, SQS
  L4 â€” API:           API Gateway, AppSync, Cognito, Lambda authorizers
  L5 â€” Frontend:      S3, CloudFront, WAF, OAI, ACM (SSL), Route53
  L6 â€” Observability: CloudWatch, X-Ray, SNS alarms, Cost Explorer, Trusted Advisor
  L7 â€” CICD:          CodePipeline, CodeBuild, CodeCommit/GitHub, approvals, rollback

For EACH identified component:
  - Name the AWS service precisely (e.g., "Amazon Aurora Serverless V2 PostgreSQL")
  - Justify WHY it is needed (quote from SOW if possible)
  - Specify sizing/configuration (e.g., "min 0.5 ACU, max 4 ACU for dev")
  - Note environment differences (dev vs staging vs prod)

Be thorough. It is better to OVER-detect components (they can be removed later)
than to MISS components that cause architecture gaps.
```

---

## USER PROMPT

```
Please analyze the following Statement of Work and produce a complete ARCHITECTURE_MAP.md.

## STATEMENT OF WORK
---
{{SOW_CONTENT}}
---

## OUTPUT STRUCTURE

Produce a Markdown document with EXACTLY the following sections:

---
# ARCHITECTURE MAP
**Project:** {{extracted project name}}
**Generated:** {{date}}
**SOW Source:** {{filename}}
**Environments:** dev | staging | prod
**Estimated Scale:** {{small/medium/large}}

---

## 1. PROJECT OVERVIEW
{{2-3 sentence summary of what is being built, extracted from SOW}}

---

## 2. DETECTED MICROSERVICES

List every discrete service/function detected from the SOW.

| # | Service Name | Purpose | Trigger Type | HTTP Method | Path | Data Stores Touched | Long-Running? |
|---|-------------|---------|--------------|-------------|------|---------------------|---------------|
| 1 | user-auth-service | Handles user registration and login | API Gateway | POST | /auth/login | DynamoDB:users | No |
| 2 | report-generator | Generates PDF reports from data | SQS queue | - | - | Aurora, S3 | YES â†’ ECS |
...

---

## 3. LAYER-BY-LAYER COMPONENT MAP

### L0 â€” NETWORKING
| Component | AWS Service | Config (Dev) | Config (Prod) | SOW Justification |
|-----------|------------|--------------|---------------|-------------------|
| Main VPC | ec2.Vpc | 2 AZs, /24 CIDRs | 3 AZs, /22 CIDRs | "Isolated network for all services" |
| NAT Gateway | ec2.NatGateway | 1 per AZ | 1 per AZ | Required for Lambda in VPC to reach internet |
| VPC Endpoints | ec2.InterfaceVpcEndpoint | S3, DynamoDB, Secrets | All AWS services | Cost + latency optimization |
...

### L1 â€” SECURITY
| Component | AWS Service | Purpose | SOW Justification |
|-----------|------------|---------|-------------------|
...

### L2 â€” DATA
| Component | AWS Service | Config | SOW Justification |
|-----------|------------|--------|-------------------|
...

### L3 â€” BACKEND
| Component | AWS Service | Runtime | Memory | Timeout | SOW Justification |
|-----------|------------|---------|--------|---------|-------------------|
...

### L4 â€” API
| Component | AWS Service | Auth Method | Endpoint Type | SOW Justification |
|-----------|------------|------------|---------------|-------------------|
...

### L5 â€” FRONTEND
| Component | AWS Service | Config | SOW Justification |
|-----------|------------|--------|-------------------|
...

### L6 â€” OBSERVABILITY
| Component | AWS Service | Purpose |
|-----------|------------|---------|
...

### L7 â€” CICD
| Stage | AWS Service | Trigger | Actions | Approval Required? |
|-------|------------|---------|---------|-------------------|
| Source | CodeCommit | Git push to main | - | No |
| Build | CodeBuild | Auto | npm install, pip install, cdk synth | No |
| Dev Deploy | CodePipeline | Auto | cdk deploy FullSystemStack/Dev | No |
| Integration Test | CodeBuild | Auto | pytest tests/integration/ | No |
| Staging Deploy | CodePipeline | Auto after tests | cdk deploy FullSystemStack/Staging | No |
| Prod Approval | SNS + Manual | Before prod | Email notification | YES â€” Manual Click |
| Prod Deploy | CodePipeline | After approval | cdk deploy FullSystemStack/Prod | No |
| Rollback | CodeDeploy | On alarm | Auto-rollback if error rate > 1% | No |

---

## 4. SERVICE DEPENDENCY GRAPH
```

{{ASCII or Mermaid graph showing service-to-service dependencies}}

Example:
CloudFront â†’ S3 (frontend assets)
CloudFront â†’ API Gateway â†’ Lambda (user-service) â†’ DynamoDB
CloudFront â†’ API Gateway â†’ Lambda (report-service) â†’ SQS â†’ ECS Worker â†’ Aurora â†’ S3
Cognito â†’ Lambda (authorizer) â†’ API Gateway
EventBridge â†’ Lambda (scheduler) â†’ Aurora

````

---

## 5. DATA ENTITY MAP
| Entity | Store | PK | SK | Key Attributes | Access Pattern |
|--------|-------|----|----|----------------|----------------|
| User | DynamoDB | userId | - | email, role, createdAt | GetUser, ListUsersByRole |
| Report | Aurora | id | - | status, s3_key, created_at | FilterByStatus, GetLatest |
...

---

## 6. ENVIRONMENT MATRIX
| Resource | Dev | Staging | Prod |
|----------|-----|---------|------|
| Aurora ACU | 0.5 min, 1 max | 0.5 min, 2 max | 1 min, 8 max |
| Lambda Memory | 256MB | 512MB | 1024MB |
| CloudFront PriceClass | PriceClass_100 | PriceClass_100 | PriceClass_All |
| WAF | Basic (count mode) | Standard (block) | Full (block + rate limit) |
| DynamoDB | On-demand | On-demand | Provisioned + auto-scale |
| Deletion Policy | DESTROY | SNAPSHOT | RETAIN |
| Multi-AZ | No | Yes | Yes |
...

---

## 7. RISK FLAGS
List any ambiguities or decisions that need human input:
1. âš ï¸  SOW mentions "reports" but it's unclear if PDF generation needs ECS or Lambda
2. âš ï¸  No explicit mention of authentication â€” assuming Cognito User Pool
3. âš ï¸  "High availability" mentioned â€” included multi-AZ for prod, confirm budget
...

---

## 8. CDK CONSTRUCT IMPORT LIST
Based on detected components, the following CDK imports will be needed:
```python
from aws_cdk import (
    Stack, Stage, CfnOutput, Duration, RemovalPolicy, Tags, Fn,
    CustomResource,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_secretsmanager as sm,
    aws_ssm as ssm,
    aws_rds as rds,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3_notifications as s3n,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_events,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_events as events,
    aws_events_targets as targets,
    aws_apigateway as apigw,
    aws_cognito as cognito,
    aws_cloudfront as cf,
    aws_cloudfront_origins as cf_origins,
    aws_wafv2 as wafv2,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_logs as logs,
    aws_xray as xray,
    aws_route53 as r53,
    aws_certificatemanager as acm,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
    aws_kinesis as kinesis,
    aws_glue as glue,
    aws_athena as athena,
    aws_backup as backup,
    aws_config as config,
    aws_cloudtrail as cloudtrail,
    aws_guardduty as guardduty,
    aws_inspector as inspector,
    aws_macie as macie,
    aws_securityhub as securityhub,
    aws_networkfirewall as networkfirewall,
    aws_globalaccelerator as ga,
    aws_shield as shield,
    # MLOps / SageMaker
    aws_sagemaker as sagemaker,
    # Streaming
    aws_msk as msk,
    aws_kinesis as kinesis,
    # Container Platform
    aws_eks as eks,
    # Observability
    aws_aps as aps,
    aws_grafana as grafana,
    aws_rum as rum,
    # LLMOps
    aws_bedrock as bedrock,
    # CICD
    pipelines,
    aws_codecommit as codecommit,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as pipeline_actions,
)
````

(Remove any not relevant to this project's detected components)

```

---

## DETECTION HEURISTICS

Claude uses these rules when analyzing the SOW:

### Functional Requirement â†’ Service Mapping
```

"user login / authentication / JWT" â†’ Cognito User Pool + Identity Pool
"file storage / upload / download" â†’ S3 + Lambda trigger + CloudFront signed URLs
"real-time updates / live data" â†’ API Gateway WebSocket OR AppSync subscriptions
"background processing / async jobs" â†’ SQS + Lambda OR SQS + ECS Fargate (if >15min)
"scheduled tasks / cron" â†’ EventBridge Scheduler + Lambda
"relational data / SQL / ACID" â†’ Aurora Serverless V2 (PostgreSQL)
"fast lookups / session / cache" â†’ DynamoDB + ElastiCache Redis
"search / full-text / faceted" â†’ OpenSearch Service
"email notifications" â†’ Amazon SES + SNS
"SMS / push notifications" â†’ Amazon SNS
"workflow / saga / orchestration" â†’ Step Functions
"reporting / analytics / BI" â†’ Athena + Glue + S3 + QuickSight
"audit logs / compliance" â†’ CloudTrail + Config + GuardDuty

--- MLOps / Data Science ---
"data lake / raw data / feature engineering" â†’ MLOPS_DATA_PLATFORM.md (S3 4-zone + Glue + Athena)
"data warehouse / Redshift / BI dashboards" â†’ MLOPS_DATA_PLATFORM.md (Redshift Serverless)
"large-scale ETL / Spark / PySpark" â†’ MLOPS_DATA_PLATFORM.md (EMR Serverless)
"lakehouse / Iceberg tables / ACID on S3 / MERGE INTO" → DATA_LAKEHOUSE_ICEBERG.md  (S3 Iceberg + Athena v3 DML)
"Redshift Spectrum / federated query / data mesh" → DATA_LAKEHOUSE_ICEBERG.md  (cross-engine federated queries)
"time travel / schema evolution / snapshot queries" → DATA_LAKEHOUSE_ICEBERG.md  (Iceberg time travel)
"Lake Formation / column masking / row-level security" → DATA_LAKEHOUSE_ICEBERG.md  (fine-grained access control)
"train ML model / SageMaker Pipelines" â†’ MLOPS_SAGEMAKER_TRAINING.md (Studio + Feature Store)
"ML inference / model endpoint" â†’ MLOPS_SAGEMAKER_SERVING.md (real-time + auto-scaling)
"LLM fine-tuning / LoRA / QLoRA / Llama / Mistral" â†’ MLOPS_PIPELINE_LLM_FINETUNING.md
"NLP / BERT / text classification / NER / sentiment" â†’ MLOPS_PIPELINE_NLP_HUGGINGFACE.md
"real-time fraud / <100ms scoring / Feature Store" â†’ MLOPS_PIPELINE_FRAUD_REALTIME.md
"time series / demand forecast / DeepAR / Prophet" â†’ MLOPS_PIPELINE_TIMESERIES.md
"computer vision / image detection / YOLOv8 / OCR" â†’ MLOPS_PIPELINE_COMPUTER_VISION.md
"recommendations / collaborative filtering / personalization" â†’ MLOPS_PIPELINE_RECOMMENDATIONS.md
"100 models / 1 model per tenant / SaaS ML" â†’ MLOPS_MULTI_MODEL_ENDPOINT.md
"batch scoring / nightly predictions / offline ML" â†’ MLOPS_BATCH_TRANSFORM.md
"SHAP / explainability / bias / EU AI Act" â†’ MLOPS_CLARIFY_EXPLAINABILITY.md
"data labeling / annotation / active learning" â†’ MLOPS_GROUND_TRUTH.md

--- LLMOps ---
"LLM / Bedrock / generative AI / chatbot" â†’ LLMOPS_BEDROCK.md (Bedrock API + Guardrails + Gateway)
"RAG / document Q&A / knowledge base" â†’ LLMOPS_BEDROCK.md (Knowledge Bases + OpenSearch)
"AI agent / agentic / multi-step AI" â†’ LLMOPS_BEDROCK.md (Bedrock Agents)
"multi-region / disaster recovery" â†’ Route53 + Global Accelerator + S3 CRR

--- Enterprise Security ---
"WAF / bot protection / DDoS / OWASP" â†’ SECURITY_WAF_SHIELD_MACIE.md (WAF v2 + Shield)
"network firewall / IDS / IPS / egress filtering" â†’ SECURITY_WAF_SHIELD_MACIE.md (Network Firewall)
"PII scanning / PHI in S3 / data classification" â†’ SECURITY_WAF_SHIELD_MACIE.md (Macie)
"HIPAA / PCI DSS / SOC2 / FedRAMP" â†’ COMPLIANCE_HIPAA_PCIDSS.md
"multi-region / active-active / <1 min RTO" â†’ GLOBAL_MULTI_REGION.md

--- Startup / SaaS Platform ---
"Kubernetes / K8s / EKS / Helm / GitOps" â†’ PLATFORM_EKS_CLUSTER.md
"Apache Kafka / MSK / Schema Registry" â†’ DATA_MSK_KAFKA.md
"Prometheus / Grafana / OpenTelemetry / SLO" â†’ OBS_OPENTELEMETRY_GRAFANA.md

```

### Scale Tier Detection
```

SOW mentions < 100 users/day â†’ small (serverless only, single AZ dev)
SOW mentions 100-10k users/day â†’ medium (mix serverless + containers, 2 AZ prod)
SOW mentions > 10k users/day â†’ large (containers primary, 3 AZ, Global Accelerator)

```

```
