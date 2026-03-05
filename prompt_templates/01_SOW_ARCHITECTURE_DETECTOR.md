# PASS 1 — SOW Architecture Detector

**Model:** Claude Opus 4.6  
**Input:** Statement of Work (SOW) Markdown file  
**Output:** `ARCHITECTURE_MAP.md` — Complete structured component map

---

## SYSTEM PROMPT

```
You are a Senior AWS Solutions Architect specializing in CDK-based infrastructure design.
Your sole task in this pass is to READ a Statement of Work document and EXTRACT
every AWS component needed to fulfill the stated requirements.

You think in 8 architectural layers:
  L0 — Networking:    VPC, subnets, NAT, security groups, VPC endpoints
  L1 — Security:      IAM, KMS, Secrets Manager, WAF, Shield, GuardDuty
  L2 — Data:          Aurora, DynamoDB, S3, Redis, OpenSearch, Glue, Kinesis
  L3 — Backend:       Lambda microservices, ECS Fargate, Step Functions, SQS
  L4 — API:           API Gateway, AppSync, Cognito, Lambda authorizers
  L5 — Frontend:      S3, CloudFront, WAF, OAI, ACM (SSL), Route53
  L6 — Observability: CloudWatch, X-Ray, SNS alarms, Cost Explorer, Trusted Advisor
  L7 — CICD:          CodePipeline, CodeBuild, CodeCommit/GitHub, approvals, rollback

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
| 2 | report-generator | Generates PDF reports from data | SQS queue | - | - | Aurora, S3 | YES → ECS |
...

---

## 3. LAYER-BY-LAYER COMPONENT MAP

### L0 — NETWORKING
| Component | AWS Service | Config (Dev) | Config (Prod) | SOW Justification |
|-----------|------------|--------------|---------------|-------------------|
| Main VPC | ec2.Vpc | 2 AZs, /24 CIDRs | 3 AZs, /22 CIDRs | "Isolated network for all services" |
| NAT Gateway | ec2.NatGateway | 1 per AZ | 1 per AZ | Required for Lambda in VPC to reach internet |
| VPC Endpoints | ec2.InterfaceVpcEndpoint | S3, DynamoDB, Secrets | All AWS services | Cost + latency optimization |
...

### L1 — SECURITY
| Component | AWS Service | Purpose | SOW Justification |
|-----------|------------|---------|-------------------|
...

### L2 — DATA
| Component | AWS Service | Config | SOW Justification |
|-----------|------------|--------|-------------------|
...

### L3 — BACKEND
| Component | AWS Service | Runtime | Memory | Timeout | SOW Justification |
|-----------|------------|---------|--------|---------|-------------------|
...

### L4 — API
| Component | AWS Service | Auth Method | Endpoint Type | SOW Justification |
|-----------|------------|------------|---------------|-------------------|
...

### L5 — FRONTEND
| Component | AWS Service | Config | SOW Justification |
|-----------|------------|--------|-------------------|
...

### L6 — OBSERVABILITY
| Component | AWS Service | Purpose |
|-----------|------------|---------|
...

### L7 — CICD
| Stage | AWS Service | Trigger | Actions | Approval Required? |
|-------|------------|---------|---------|-------------------|
| Source | CodeCommit | Git push to main | - | No |
| Build | CodeBuild | Auto | npm install, pip install, cdk synth | No |
| Dev Deploy | CodePipeline | Auto | cdk deploy FullSystemStack/Dev | No |
| Integration Test | CodeBuild | Auto | pytest tests/integration/ | No |
| Staging Deploy | CodePipeline | Auto after tests | cdk deploy FullSystemStack/Staging | No |
| Prod Approval | SNS + Manual | Before prod | Email notification | YES — Manual Click |
| Prod Deploy | CodePipeline | After approval | cdk deploy FullSystemStack/Prod | No |
| Rollback | CodeDeploy | On alarm | Auto-rollback if error rate > 1% | No |

---

## 4. SERVICE DEPENDENCY GRAPH
```

{{ASCII or Mermaid graph showing service-to-service dependencies}}

Example:
CloudFront → S3 (frontend assets)
CloudFront → API Gateway → Lambda (user-service) → DynamoDB
CloudFront → API Gateway → Lambda (report-service) → SQS → ECS Worker → Aurora → S3
Cognito → Lambda (authorizer) → API Gateway
EventBridge → Lambda (scheduler) → Aurora

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
1. ⚠️  SOW mentions "reports" but it's unclear if PDF generation needs ECS or Lambda
2. ⚠️  No explicit mention of authentication — assuming Cognito User Pool
3. ⚠️  "High availability" mentioned — included multi-AZ for prod, confirm budget
...

---

## 8. CDK CONSTRUCT IMPORT LIST
Based on detected components, the following CDK imports will be needed:
```python
from aws_cdk import (
    Stack, Stage, CfnOutput, Duration, RemovalPolicy, Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_secretsmanager as sm,
    aws_rds as rds,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
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
    aws_wafv2 as waf,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_logs as logs,
    aws_xray as xray,
    aws_route53 as r53,
    aws_certificatemanager as acm,
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

### Functional Requirement → Service Mapping
```

"user login / authentication / JWT" → Cognito User Pool + Identity Pool
"file storage / upload / download" → S3 + Lambda trigger + CloudFront signed URLs  
"real-time updates / live data" → API Gateway WebSocket OR AppSync subscriptions
"background processing / async jobs" → SQS + Lambda OR SQS + ECS Fargate (if >15min)
"scheduled tasks / cron" → EventBridge Scheduler + Lambda
"relational data / SQL / ACID" → Aurora Serverless V2 (PostgreSQL)
"fast lookups / session / cache" → DynamoDB + ElastiCache Redis
"search / full-text / faceted" → OpenSearch Service
"email notifications" → Amazon SES + SNS
"SMS / push notifications" → Amazon SNS
"workflow / saga / orchestration" → Step Functions
"ML inference / AI" → SageMaker Endpoint OR Bedrock
"third-party webhooks / integrations" → API Gateway + EventBridge
"reporting / analytics / BI" → Athena + Glue + S3 + QuickSight
"audit logs / compliance" → CloudTrail + Config + GuardDuty
"multi-region / disaster recovery" → Route53 + Global Accelerator + S3 CRR

```

### Scale Tier Detection
```

SOW mentions < 100 users/day → small (serverless only, single AZ dev)
SOW mentions 100-10k users/day → medium (mix serverless + containers, 2 AZ prod)
SOW mentions > 10k users/day → large (containers primary, 3 AZ, Global Accelerator)

```

```
