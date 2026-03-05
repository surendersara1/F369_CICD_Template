# Example Architecture Map Output — MedFlow Patient Records Platform

**Generated from:** `examples/sample_sow.md`  
**Pass:** 1 — SOW Architecture Detector  
**Model:** Claude Opus 4.6

---

# ARCHITECTURE MAP

**Project:** MedFlow Patient Records Platform  
**Generated:** 2026-03-05  
**SOW Source:** sample_sow.md  
**Environments:** dev | staging | prod  
**Estimated Scale:** Large (2,000 concurrent, 25k users, 50TB data)

---

## 1. PROJECT OVERVIEW

MedFlow is a HIPAA-compliant, staff-facing patient records management platform for Regional Health Networks Inc. It provides a React SPA portal, a 10-endpoint REST API, background PDF report generation, scheduled EHR integrations, and nightly batch processing. All PHI must remain in us-east-1 with full encryption at rest and in transit.

---

## 2. DETECTED MICROSERVICES

| #   | Service Name         | Purpose                        | Trigger                | Method | Path                     | Data Stores                          | Long-Running?         |
| --- | -------------------- | ------------------------------ | ---------------------- | ------ | ------------------------ | ------------------------------------ | --------------------- |
| 1   | auth-service         | Login + JWT generation         | API GW                 | POST   | /auth/login              | DynamoDB:users, Cognito              | No                    |
| 2   | auth-refresh         | JWT refresh                    | API GW                 | POST   | /auth/refresh            | Cognito                              | No                    |
| 3   | patient-list         | List + filter patients         | API GW                 | GET    | /patients                | Aurora:patients                      | No                    |
| 4   | patient-create       | Create patient record          | API GW                 | POST   | /patients                | Aurora:patients, DynamoDB:audit      | No                    |
| 5   | patient-get          | Get patient by ID              | API GW                 | GET    | /patients/{id}           | Aurora:patients                      | No                    |
| 6   | patient-update       | Update patient record          | API GW                 | PUT    | /patients/{id}           | Aurora:patients, DynamoDB:audit      | No                    |
| 7   | document-upload      | Upload + trigger scan          | API GW                 | POST   | /patients/{id}/documents | S3:documents, DynamoDB:documents     | No                    |
| 8   | document-list        | List patient docs              | API GW                 | GET    | /patients/{id}/documents | DynamoDB:documents, S3:documents     | No                    |
| 9   | report-trigger       | Start async PDF gen            | API GW                 | POST   | /reports/generate        | SQS:report-queue, DynamoDB:reports   | No                    |
| 10  | report-status        | Check gen status               | API GW                 | GET    | /reports/{id}/status     | DynamoDB:reports                     | No                    |
| 11  | report-download      | Get pre-signed S3 URL          | API GW                 | GET    | /reports/{id}/download   | S3:reports, DynamoDB:reports         | No                    |
| 12  | virus-scanner        | Scan uploaded files            | SQS:scan-queue         | -      | -                        | S3:documents, DynamoDB:documents     | No                    |
| 13  | ehr-sync-epic        | Pull from Epic EHR API         | EventBridge (30min)    | -      | -                        | Aurora:patients, Secrets Manager     | No                    |
| 14  | ehr-sync-cerner      | Pull from Cerner EHR API       | EventBridge (30min)    | -      | -                        | Aurora:patients, Secrets Manager     | No                    |
| 15  | audit-aggregator     | Nightly audit log rollup       | EventBridge (cron 2am) | -      | -                        | DynamoDB:audit, Aurora:audit_summary | No                    |
| ECS | pdf-report-generator | Generate PDF compliance report | SQS:report-queue       | -      | -                        | Aurora, S3:reports                   | **YES → ECS Fargate** |

---

## 3. LAYER-BY-LAYER COMPONENT MAP

### L0 — NETWORKING

| Component                      | AWS Service                        | Config (Dev)                     | Config (Prod) | SOW Justification                                        |
| ------------------------------ | ---------------------------------- | -------------------------------- | ------------- | -------------------------------------------------------- |
| Main VPC                       | ec2.Vpc                            | 2 AZs, /24                       | 3 AZs, /22    | "12 hospital locations, 2k concurrent" — multi-AZ needed |
| Public Subnets                 | ec2.SubnetType.PUBLIC              | 1 per AZ                         | 1 per AZ      | ALB, NAT Gateways                                        |
| Private Subnets                | ec2.SubnetType.PRIVATE_WITH_EGRESS | 1 per AZ                         | 1 per AZ      | Lambda, ECS Fargate                                      |
| Isolated Subnets               | ec2.SubnetType.PRIVATE_ISOLATED    | 1 per AZ                         | 1 per AZ      | Aurora, ElastiCache                                      |
| NAT Gateway                    | ec2.NatGateway                     | 1 total                          | 1 per AZ      | Lambda/ECS internet egress (EHR API calls)               |
| VPC Endpoint — S3              | ec2.GatewayVpcEndpoint             | ✓                                | ✓             | HIPAA: keep S3 traffic in AWS network (no internet)      |
| VPC Endpoint — DynamoDB        | ec2.GatewayVpcEndpoint             | ✓                                | ✓             | HIPAA: no internet path to DynamoDB                      |
| VPC Endpoint — Secrets Manager | ec2.InterfaceVpcEndpoint           | ✓                                | ✓             | Secure secret retrieval without internet egress          |
| VPC Endpoint — SSM             | ec2.InterfaceVpcEndpoint           | ✓                                | ✓             | ECS container management                                 |
| Security Group — Lambda        | ec2.SecurityGroup                  | Egress 443 to DB SGs             | Same          | Lambda → Aurora, Redis, DynamoDB                         |
| Security Group — ECS           | ec2.SecurityGroup                  | Egress 443, 5432                 | Same          | ECS Worker → Aurora, S3                                  |
| Security Group — Aurora        | ec2.SecurityGroup                  | Inbound 5432 from Lambda/ECS SGs | Same          | HIPAA: no public DB access                               |
| Security Group — Redis         | ec2.SecurityGroup                  | Inbound 6379 from Lambda SG      | Same          | Cache access limited to Lambda                           |

### L1 — SECURITY

| Component               | AWS Service                                    | Purpose                           | SOW Justification                      |
| ----------------------- | ---------------------------------------------- | --------------------------------- | -------------------------------------- |
| PHI Encryption Key      | aws_kms.Key                                    | Encrypt all PHI at rest           | "HIPAA: all PHI encrypted AES-256"     |
| Pipeline Encryption Key | aws_kms.Key                                    | Encrypt pipeline artifacts        | Security best practice                 |
| Secrets: Epic API       | aws_secretsmanager.Secret                      | Epic EHR API credentials          | "credentials in Secrets Manager"       |
| Secrets: Cerner API     | aws_secretsmanager.Secret                      | Cerner EHR API credentials        | "separate credentials"                 |
| Secrets: Aurora DB      | aws_secretsmanager.Secret                      | Auto-rotated DB password          | HIPAA: no hardcoded credentials        |
| Cognito User Pool       | aws_cognito.UserPool                           | User authentication               | "AWS Cognito with MFA enforcement"     |
| Cognito MFA             | MFA enforced (TOTP)                            | MFA for all users                 | "MFA required for all users"           |
| Cognito Groups          | Roles: Admin, Clinician, Receptionist, Auditor | RBAC                              | "Role-based access control"            |
| Lambda Authorizer       | aws_apigateway.RequestAuthorizer               | JWT validation per-request        | Token-based auth for all API endpoints |
| WAF (CloudFront)        | aws_wafv2.CfnWebACL                            | Block OWASP Top 10, rate limit    | "WAF required on all public endpoints" |
| WAF (API Gateway)       | aws_wafv2.CfnWebACL                            | Regional WAF on API GW            | "WAF required on all public endpoints" |
| IAM Role — Lambda       | aws_iam.Role                                   | Per-service least-privilege roles | HIPAA: minimum necessary access        |
| IAM Role — ECS Task     | aws_iam.Role                                   | PDF generator task role           | Access S3 + Aurora only                |
| CloudTrail              | aws_cloudtrail.Trail                           | API activity audit log            | "SOC 2: full audit trail"              |
| GuardDuty               | aws_guardduty.CfnDetector                      | Threat detection                  | "penetration testing + HIPAA"          |
| Security Hub            | aws_securityhub.CfnHub                         | Unified security findings         | SOC 2 Type II compliance               |
| Config Rules            | aws_config.ManagedRule                         | Compliance posture                | "SOC 2 Type II"                        |

### L2 — DATA

| Component        | AWS Service                          | Config (Dev)    | Config (Prod)             | SOW Justification                                    |
| ---------------- | ------------------------------------ | --------------- | ------------------------- | ---------------------------------------------------- |
| Patient DB       | Aurora Serverless V2 (PostgreSQL 16) | 0.5-2 ACU       | 2-16 ACU, Multi-AZ reader | "PostgreSQL (relational)" + ACID for patient records |
| Metadata + Audit | DynamoDB                             | On-demand       | Provisioned + auto-scale  | Fast lookups for audit logs, documents metadata      |
| Documents Store  | S3 Bucket (PHI-encrypted)            | ✓               | ✓ + object lock           | "file upload, medical documents, imaging"            |
| Reports Store    | S3 Bucket                            | ✓               | ✓ + 7-year retention      | "PDF compliance reports" + SOC 2 retention           |
| Session Cache    | ElastiCache Redis 7.1                | cache.t4g.micro | cache.r7g.large, Multi-AZ | "< 200ms p95" — cache patient lookups                |
| Report Queue     | SQS FIFO                             | ✓               | ✓ + KMS                   | Async report generation trigger                      |
| Scan Queue       | SQS Standard                         | ✓               | ✓ + KMS                   | Async virus scanning trigger                         |
| Report DLQ       | SQS Standard                         | ✓               | ✓                         | Failed report handling                               |
| Scan DLQ         | SQS Standard                         | ✓               | ✓                         | Failed scan handling                                 |

### L3 — BACKEND

| Component                    | AWS Service                    | Runtime              | Memory              | Timeout                | SOW Justification                            |
| ---------------------------- | ------------------------------ | -------------------- | ------------------- | ---------------------- | -------------------------------------------- |
| 14 Lambda Functions (see §2) | aws_lambda.Function            | Python 3.12          | 512MB dev, 1GB prod | 29s (API), 300s (scan) | REST API endpoints + scheduled jobs          |
| PDF Generator                | ECS Fargate Task               | Python 3.12 (Docker) | 2GB                 | 10 min                 | "must use ECS Fargate (> 15 minute jobs)"    |
| EventBridge Rule — Epic      | EventBridge Scheduler          | -                    | -                   | Every 30 min           | "EHR sync every 30 minutes"                  |
| EventBridge Rule — Cerner    | EventBridge Scheduler          | -                    | -                   | Every 30 min           | Same                                         |
| EventBridge Rule — Audit     | EventBridge Scheduler          | -                    | -                   | Cron: 0 2 \* \* ?      | "nightly batch jobs at 2am UTC"              |
| Step Functions (optional)    | aws_stepfunctions.StateMachine | -                    | -                   | -                      | Orchestrate virus-scan → make-available flow |

### L4 — API

| Component          | AWS Service                | Auth Method              | Endpoint Type     | SOW Justification                       |
| ------------------ | -------------------------- | ------------------------ | ----------------- | --------------------------------------- |
| Main REST API      | API Gateway (REST)         | Cognito Authorizer (JWT) | Regional          | 10 API endpoints listed in SOW §4.2     |
| Lambda Authorizer  | RequestAuthorizer          | JWT validation           | -                 | Role claims extraction for RBAC         |
| Cognito Authorizer | CognitoUserPoolsAuthorizer | Backup                   | -                 | Simple auth for non-sensitive endpoints |
| API Usage Plan     | api.add_usage_plan         | 1000 req/min dev         | 5000 req/min prod | Rate limiting                           |
| API Access Logs    | logs.LogGroup              | -                        | -                 | "full audit trail" — SOC 2              |

### L5 — FRONTEND

| Component        | AWS Service              | Config                     | SOW Justification                  |
| ---------------- | ------------------------ | -------------------------- | ---------------------------------- |
| Frontend Bucket  | S3 (private, KMS)        | KMS encrypted, versioned   | React SPA hosting                  |
| CloudFront CDN   | CloudFront Distribution  | US-only edge               | "CloudFront restricted to US edge" |
| OAI              | CloudFront OAI           | ✓                          | Secure S3 access                   |
| WAF (US-only)    | WAFv2 (CLOUDFRONT scope) | US geo-restriction + OWASP | "data residency: US only"          |
| Security Headers | CloudFront Function      | HSTS, CSP, X-Frame         | Security hardening                 |
| SSL Certificate  | ACM (us-east-1)          | \*.medflow.example.com     | HTTPS enforcement                  |
| Custom Domain    | Route53                  | medflow.example.com → CF   | User-facing domain                 |

### L6 — OBSERVABILITY

| Component              | AWS Service                 | Purpose                                            |
| ---------------------- | --------------------------- | -------------------------------------------------- |
| CloudWatch Dashboard   | cw.Dashboard                | Unified ops view: API metrics, Lambda, Aurora, SQS |
| Lambda Error Alarms    | cw.Alarm (×14)              | Alert on Lambda errors per service                 |
| API 5xx Alarm          | cw.Alarm                    | Alert DevOps on elevated API errors                |
| API Latency Alarm      | cw.Alarm                    | Alert if p99 > 500ms (SOW: < 200ms p95)            |
| Aurora CPU Alarm       | cw.Alarm                    | Alert if Aurora CPU > 80%                          |
| DLQ Depth Alarm        | cw.Alarm                    | Alert if report/scan jobs failing                  |
| ECS Task Failure Alarm | cw.Alarm                    | Alert on PDF generator failures                    |
| X-Ray Tracing          | All Lambda + API GW         | End-to-end trace for patient record requests       |
| CloudWatch Logs        | logs.LogGroup (per service) | Centralized log aggregation                        |
| Log Insights Queries   | logs.QueryDefinition        | Error patterns, PHI-access patterns                |
| SNS Alert Topic        | sns.Topic                   | Email + PagerDuty for critical alarms              |
| CloudTrail             | cloudtrail.Trail            | API-level audit trail (SOC 2)                      |

### L7 — CICD

| Stage              | AWS Service              | Trigger                 | Actions                                          | Approval?  |
| ------------------ | ------------------------ | ----------------------- | ------------------------------------------------ | ---------- |
| Source             | CodeCommit               | Push to `main`          | Clone repo                                       | No         |
| Build              | CodeBuild (STANDARD_7_0) | Automatic               | pip install, bandit scan, pytest unit, cdk synth | No         |
| Dev Deploy         | CodePipeline             | Automatic               | cdk deploy MedFlowStack-Dev                      | No         |
| Dev Smoke Tests    | CodeBuild                | Automatic               | pytest tests/smoke/ -m dev                       | No         |
| Integration Tests  | CodeBuild (VPC)          | Automatic               | pytest tests/integration/ against Dev            | No         |
| Staging Deploy     | CodePipeline             | After integration tests | cdk deploy MedFlowStack-Staging                  | No         |
| Staging Perf Tests | CodeBuild                | Automatic               | locust baseline test                             | No         |
| Prod Approval      | SNS + Manual             | Email approvers         | Human review + ticket                            | **YES**    |
| Prod Deploy        | CodePipeline             | After approval          | cdk deploy MedFlowStack-Prod (blue/green)        | No         |
| Prod Smoke Tests   | CodeBuild                | Automatic               | pytest tests/smoke/ -m prod                      | No         |
| Rollback Alarm     | CloudWatch               | Error rate > 1%         | Notify DevOps + manual rollback                  | Alert only |

---

## 4. SERVICE DEPENDENCY GRAPH

```
Users (Browser)
  └─▶ Route53 ──▶ CloudFront (WAF) ──▶ S3 (React SPA frontend)
                                    └─▶ API Gateway (WAF) ──▶ Cognito Authorizer
                                                           ├─▶ Lambda: auth-service ──▶ Cognito
                                                           ├─▶ Lambda: patient-* ──▶ Aurora PostgreSQL
                                                           │                      └─▶ DynamoDB (audit)
                                                           ├─▶ Lambda: document-upload ──▶ S3 (documents)
                                                           │                           └─▶ SQS (scan-queue)
                                                           ├─▶ Lambda: report-trigger ──▶ SQS (report-queue)
                                                           └─▶ Lambda: report-download ──▶ S3 (pre-signed URL)

SQS (scan-queue) ──▶ Lambda: virus-scanner ──▶ S3 (update scan status)
                                           └─▶ DynamoDB (update document status)

SQS (report-queue) ──▶ ECS Fargate: pdf-generator ──▶ Aurora (read patient data)
                                                   └─▶ S3 (write PDF report)

EventBridge (30min) ──▶ Lambda: ehr-sync-epic ──▶ Secrets Manager (Epic creds)
                                               └─▶ Aurora (write patient updates)

EventBridge (2am) ──▶ Lambda: audit-aggregator ──▶ DynamoDB (read audit logs)
                                               └─▶ Aurora (write audit_summary)

Cognito ──▶ Lambda authorizer ──▶ API Gateway
```

---

## 5. DATA ENTITY MAP

| Entity       | Store                   | PK               | SK/Index                        | Key Attributes                                     | Access Patterns                           |
| ------------ | ----------------------- | ---------------- | ------------------------------- | -------------------------------------------------- | ----------------------------------------- |
| User         | Cognito + DynamoDB      | userId           | email (GSI)                     | email, role, hospitalId, mfaEnabled                | GetByEmail, GetById, ListByRole           |
| Patient      | Aurora (patients table) | id (UUID)        | medical_record_id               | name_enc, dob_enc, ssn_enc, created_by, updated_at | GetById, ListPaginated, FilterByHospital  |
| Document     | DynamoDB                | pk=DOC#{id}      | sk=PATIENT#{patientId}          | type, s3_key, scan_status, uploaded_by             | GetByPatient, GetById, FilterByScanStatus |
| Report       | DynamoDB                | pk=REPORT#{id}   | sk=STATUS#{status}              | type, s3_key, requested_by, requested_at           | GetById, GetByRequester, ListByStatus     |
| AuditLog     | DynamoDB                | pk=AUDIT#{date}  | sk=TS#{timestamp}#USER#{userId} | action, resource_type, resource_id, ip             | QueryByDate, QueryByUser, QueryByResource |
| AuditSummary | Aurora (audit_summary)  | id               | date, hospital_id               | access_count, unique_users, anomalies              | GetByDateRange, GetByHospital             |
| EHRSync      | DynamoDB                | pk=SYNC#{vendor} | sk=PATIENT#{externalId}         | internal_patient_id, last_synced, status           | GetSyncStatus, ListPendingSync            |

---

## 6. ENVIRONMENT MATRIX

| Resource                     | Dev              | Staging          | Prod                           |
| ---------------------------- | ---------------- | ---------------- | ------------------------------ |
| Aurora Min/Max ACU           | 0.5 / 2          | 0.5 / 4          | 2 / 16                         |
| Aurora Readers               | 0                | 0                | 1 read replica                 |
| Aurora Deletion Policy       | DESTROY          | SNAPSHOT         | RETAIN                         |
| DynamoDB Billing             | On-demand        | On-demand        | Provisioned + auto-scale       |
| Lambda Memory                | 512MB            | 512MB            | 1024MB                         |
| Lambda Reserved Concurrency  | 10               | 50               | 200                            |
| ECS Fargate CPU/Mem          | 256/512          | 512/1024         | 1024/2048                      |
| ElastiCache Node             | cache.t4g.micro  | cache.t4g.medium | cache.r7g.large                |
| ElastiCache Nodes            | 1                | 1                | 2 (multi-AZ)                   |
| CloudFront Price Class       | PriceClass_100   | PriceClass_100   | PriceClass_100 (US-only)       |
| WAF Mode                     | Count (log only) | Block            | Block + rate limit             |
| Multi-AZ NAT                 | No (1 NAT total) | No               | Yes (1 per AZ)                 |
| KMS Key Rotation             | Annual           | Annual           | 90 days                        |
| CloudWatch Log Retention     | 7 days           | 30 days          | 7 years (HIPAA)                |
| S3 Object Lock               | No               | No               | Yes (compliance mode, 7 years) |
| Deletion Protection (Aurora) | No               | No               | Yes                            |
| GuardDuty                    | No               | Yes              | Yes                            |
| Security Hub                 | No               | Yes              | Yes                            |
| CloudTrail                   | No               | Yes              | Yes (with log validation)      |
| Backup Retention             | 1 day            | 7 days           | 35 days                        |
| Budget Alert                 | $100/mo          | $500/mo          | $8,000/mo                      |

---

## 7. RISK FLAGS

1. ⚠️ **SOW says "< 15 minute jobs → ECS" but also "< 10 minutes for reports"**: PDF generation could potentially fit in Lambda Extended (< 15 min). Recommended: keep ECS for headroom and memory (PDF needs 2GB+).
2. ⚠️ **Virus scanning service**: SOW doesn't specify a vendor. Assumed: ClamAV running in Lambda container image (free) OR AWS Marketplace security vendor. Decision needed.
3. ⚠️ **DICOM file support**: Medical imaging (DICOM) files can be 100-500MB. S3 multipart upload + presigned URLs required. Added to document-upload service design.
4. ⚠️ **MFA type not specified**: Assumed TOTP (authenticator app) via Cognito. SMS MFA has HIPAA concerns (telecom interception). Confirm with client.
5. ⚠️ **Blue/green deployment**: SOW requires "zero-downtime deployments." Added CodeDeploy blue/green for Lambda. ECS uses rolling deployment.
6. ⚠️ **HIPAA audit log retention**: HIPAA requires 6 years minimum. Architecture map specifies 7 years with S3 Object Lock compliance mode.

---

## 8. CDK CONSTRUCT IMPORT LIST

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
    aws_sns_subscriptions as subs,
    aws_events as events,
    aws_events_targets as targets,
    aws_apigateway as apigw,
    aws_cognito as cognito,
    aws_cloudfront as cf,
    aws_cloudfront_origins as cf_origins,
    aws_route53 as r53,
    aws_certificatemanager as acm,
    aws_wafv2 as waf,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_logs as logs,
    aws_cloudtrail as cloudtrail,
    aws_elasticache as elasticache,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
    aws_ses as ses,
    pipelines,
    aws_codecommit as codecommit,
    aws_codebuild as codebuild,
)
from constructs import Construct
```
