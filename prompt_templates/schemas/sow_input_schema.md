# SOW Input Format Guide

**Purpose:** Defines the expected Markdown format for SOW files that are fed into the prompt template system.

---

## Supported SOW Formats

The architecture detector (`01_SOW_ARCHITECTURE_DETECTOR.md`) can handle:

1. ✅ **Formal SOW documents** — long-form, structured with sections
2. ✅ **Brief project descriptions** — 1-2 page summaries with requirements
3. ✅ **User story collections** — Agile-format with "As a user, I want..."
4. ✅ **Technical specifications** — API specs, ERDs, system diagrams described in text
5. ✅ **Hybrid documents** — Mix of all of the above

---

## Ideal SOW Structure for Best Results

```markdown
# Project Statement of Work

**Project Name:** [Unique project identifier, e.g., "ZetaPay Payment Platform"]
**Date:** [YYYY-MM-DD]
**Version:** [e.g., v1.2]

---

## 1. Executive Summary

[2-4 sentences describing what is being built and for whom]

## 2. Business Objectives

[List of business goals this system must achieve]

## 3. System Overview

[High-level description of the system and its components]

## 4. Functional Requirements

### 4.1 User-Facing Features

- [Feature 1: description]
- [Feature 2: description]
  ...

### 4.2 API Requirements

- [API 1: method, path, purpose]
- [API 2: ...]
  ...

### 4.3 Background Processing Requirements

- [Job 1: description, frequency, expected duration]
  ...

### 4.4 Data Requirements

- [Entity 1: name, key fields, relationships]
  ...

## 5. Non-Functional Requirements

### 5.1 Performance

- [e.g., "API response time < 200ms at p95 under 1000 concurrent users"]
- [e.g., "Report generation completed within 5 minutes"]

### 5.2 Scalability

- [e.g., "System must support up to 50,000 registered users"]
- [e.g., "Handle up to 500 transactions per second"]

### 5.3 Availability

- [e.g., "99.9% uptime SLA for production"]
- [e.g., "RTO: 4 hours, RPO: 1 hour"]

### 5.4 Security & Compliance

- [e.g., "SOC 2 Type II compliant"]
- [e.g., "PII data must be encrypted at rest and in transit"]
- [e.g., "Multi-factor authentication required for admin users"]

### 5.5 Cost

- [e.g., "Monthly infrastructure budget: $2,000"]
- [e.g., "Prefer serverless to minimize fixed costs"]

## 6. Integration Requirements

- [External API 1: name, type (REST/GraphQL/SOAP), purpose]
- [Third-party service: name, integration method]

## 7. Environments Required

- [ ] Development
- [ ] Staging / UAT
- [ ] Production
- [ ] Other: ****\_\_\_****

## 8. Geographic Requirements

- [e.g., "Primary AWS region: us-east-1"]
- [e.g., "EU data residency: data must not leave eu-west-1"]
- [e.g., "Multi-region for disaster recovery"]

## 9. Technology Preferences (if any)

- Frontend: [React / Next.js / Angular / Vue / Other]
- Database: [PostgreSQL / MySQL / DynamoDB-first / Other]
- Source Control: [CodeCommit / GitHub / GitLab / Bitbucket]
- Authentication: [Cognito / custom / Auth0 / Other]

## 10. Out of Scope

[Explicitly list what is NOT being built]
```

---

## Minimum Viable SOW

If your SOW is brief, here's the minimum information needed:

```markdown
# Project: [Name]

## What we're building:

[Description]

## Key features:

- [Feature 1]
- [Feature 2]
- [Feature 3]

## Scale:

[e.g., "10 internal users" OR "100k public users"]

## Environments needed:

[e.g., "Dev + Prod" OR "Dev + Staging + Prod"]
```

---

## Keywords That Trigger Specific AWS Services

Include these keywords in your SOW to ensure the correct services are detected:

| If you need...     | Use this keyword in SOW                                         |
| ------------------ | --------------------------------------------------------------- |
| User login         | "authentication", "login", "JWT", "SSO", "OAuth"                |
| File uploads       | "upload", "file storage", "documents", "media", "attachments"   |
| Email sending      | "email", "notifications", "SES", "transactional email"          |
| Background jobs    | "background processing", "async", "scheduled", "cron", "batch"  |
| Real-time features | "real-time", "websocket", "live updates", "push notifications"  |
| Search             | "search", "full-text search", "faceted search", "elasticsearch" |
| Caching            | "cache", "performance", "low latency", "session storage"        |
| Analytics          | "analytics", "reporting", "BI", "dashboards", "metrics"         |
| ML/AI              | "machine learning", "AI", "inference", "predictions", "models"  |
| Multi-region       | "DR", "disaster recovery", "global", "multi-region", "failover" |
| Compliance         | "HIPAA", "SOC 2", "PCI", "GDPR", "audit logs", "compliance"     |
