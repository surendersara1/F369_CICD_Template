# Example SOW — "MedFlow Patient Records Platform"

**Purpose:** Demonstrates an ideal SOW format for the CDK CICD prompt template system.

---

# Project Statement of Work

**Project Name:** MedFlow Patient Records Platform  
**Date:** 2026-03-05  
**Version:** v1.0  
**Client:** Regional Health Networks Inc.

---

## 1. Executive Summary

MedFlow is a HIPAA-compliant patient records management platform for regional healthcare providers. The system provides a web portal for staff to create, access, and manage patient records; a REST API for third-party EHR integrations; and a background processing engine for generating compliance reports and PDF summaries.

---

## 2. Business Objectives

- Replace legacy on-premise system with cloud-native solution
- Reduce report generation time from 4 hours to under 10 minutes
- Enable real-time access to patient records for clinical staff across 12 hospital locations
- Achieve SOC 2 Type II and HIPAA compliance
- Support 25,000 registered clinical users with up to 2,000 concurrent

---

## 3. System Overview

The system is a multi-tier web application:

- A React web portal (SPA) for clinical staff
- A REST API backend (10+ endpoints) for record management
- A background PDF report generation engine for compliance reports
- An integration gateway for connecting with third-party EHR vendor systems
- Nightly batch jobs for data aggregation and compliance auditing

---

## 4. Functional Requirements

### 4.1 User-Facing Features

- User authentication with MFA (multi-factor authentication) required for all users
- Role-based access control (RBAC): Admin, Clinician, Receptionist, Auditor
- Patient record creation, search, and update
- File upload: attach medical documents and imaging scans (DICOM) to patient records
- Real-time notifications: alert staff when records are updated by others
- Compliance report generation: generate PDF compliance reports on demand
- Audit log viewer: searchable audit trail of all record accesses

### 4.2 API Requirements

- POST /auth/login — authenticate user, return JWT
- POST /auth/refresh — refresh expired tokens
- GET /patients — list patients (paginated, filterable)
- POST /patients — create new patient record
- GET /patients/{id} — get patient record by ID
- PUT /patients/{id} — update patient record
- POST /patients/{id}/documents — upload document to patient record
- GET /patients/{id}/documents — list documents for patient
- POST /reports/generate — trigger compliance report generation (async)
- GET /reports/{id}/status — check report generation status
- GET /reports/{id}/download — download generated PDF report

### 4.3 Background Processing

- PDF report generation: triggered by API, may take up to 8 minutes, must not timeout
- Nightly audit aggregation: cron job at 2am UTC, aggregates access logs to daily summary
- Document virus scanning: every uploaded file must be scanned before access is granted
- Third-party EHR sync: pull patient updates from two external EHR APIs every 30 minutes

### 4.4 Data Requirements

- Patient entity: id, name (encrypted), dob (encrypted), ssn (encrypted), medical_record_id, created_by, updated_at
- Document entity: id, patient_id, type, s3_key, virus_scan_status, uploaded_by, uploaded_at
- Report entity: id, type, status, s3_key, requested_by, requested_at, completed_at
- AuditLog entity: id, user_id, action, resource_type, resource_id, timestamp, ip_address
- User entity: id, email, role, hospital_id, last_login, mfa_enabled

---

## 5. Non-Functional Requirements

### 5.1 Performance

- API response time: < 200ms at p95 for read operations under 500 concurrent users
- PDF generation: < 10 minutes for any report
- File upload: support files up to 500MB

### 5.2 Scalability

- 25,000 registered users
- Up to 2,000 concurrent active users during peak shift hours
- Storage: 50TB expected in year 1, growing 20% annually

### 5.3 Availability

- 99.9% uptime SLA for production
- RTO: 2 hours, RPO: 15 minutes
- Zero-downtime deployments required

### 5.4 Security & Compliance

- HIPAA compliant: all PHI encrypted at rest (AES-256) and in transit (TLS 1.2+)
- SOC 2 Type II: full audit trail, access controls, encryption
- All data stays within us-east-1 (data residency requirement)
- No PHI in logs — all logs must be redacted/anonymized
- WAF required on all public endpoints
- Penetration testing must pass before production go-live

### 5.5 Cost

- Monthly infrastructure budget: $8,000 for production
- Prefer serverless where possible to minimize idle costs

---

## 6. Integration Requirements

- EHR Vendor 1 (Epic): REST API, polling every 30 minutes, credentials in Secrets Manager
- EHR Vendor 2 (Cerner): REST API, polling every 30 minutes, separate credentials
- SMTP relay: for sending audit reports via email (use Amazon SES)
- SMS alerts: for MFA codes and critical patient alerts (Amazon SNS)

---

## 7. Environments Required

- [x] Development
- [x] Staging / UAT
- [x] Production

---

## 8. Geographic Requirements

- Primary AWS region: us-east-1 (Virginia)
- No multi-region required (HIPAA data residency: US only)
- CloudFront must be restricted to US edge locations only

---

## 9. Technology Preferences

- Frontend: React with TypeScript
- Database: PostgreSQL (relational) + DynamoDB for metadata/audit logs
- Source Control: AWS CodeCommit
- Authentication: AWS Cognito with MFA enforcement
- Report generation: Must use ECS Fargate (> 15 minute jobs, memory-intensive)

---

## 10. Out of Scope

- Mobile apps (future phase)
- Patient-facing portal (staff-only in v1)
- Real-time video/telemedicine
- Billing and insurance claims processing
