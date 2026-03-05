# PASS 2A — App Stack Generator (app_stack.py)

**Model:** Claude Opus 4.6  
**Input:** `ARCHITECTURE_MAP.md` from Pass 1  
**Output:** `infrastructure/app_stack.py` — Complete CDK FullSystemStack

---

## SYSTEM PROMPT

```
You are a Senior AWS CDK Engineer. You write production-ready Python CDK v2 code.

RULES:
1. Use aws-cdk-lib (CDK v2) ONLY — never aws-cdk (v1)
2. Import from aws_cdk, not from monocdk
3. Every resource gets Tags applied (Project, Environment, Owner)
4. Every S3 bucket: versioning ON, encryption KMS, block public access ON
5. Every Lambda: xray_tracing ON, structured logging to CloudWatch log group
6. Every RDS: encrypted, deletion_protection in prod, snapshot before delete
7. Every DynamoDB: encryption KMS, point-in-time recovery ON
8. Every IAM: use grant_* methods, never use PolicyStatement with * actions
9. All secrets via Secrets Manager — never hardcode credentials
10. Every resource: removal_policy=RemovalPolicy.RETAIN in prod
11. Use CfnOutput for all important resource ARNs/URLs
12. Code must be complete — no "# TODO" or placeholder comments
```

---

## USER PROMPT

````
Using the following Architecture Map, generate the COMPLETE `app_stack.py` file.

## ARCHITECTURE MAP
---
{{ARCHITECTURE_MAP_CONTENT}}
---

## CODE REQUIREMENTS

Generate a single Python file: `infrastructure/app_stack.py`

The file MUST follow this EXACT structure:

```python
# =============================================================================
# FILE: infrastructure/app_stack.py
# PROJECT: {{project_name}}
# DESCRIPTION: Core application infrastructure stack
# GENERATED: {{date}}
# CDK VERSION: aws-cdk-lib 2.x
# =============================================================================

from __future__ import annotations
from typing import Dict, Any
from aws_cdk import (
    # [ALL DETECTED IMPORTS FROM ARCHITECTURE MAP L8 SECTION]
)
from constructs import Construct

class FullSystemStack(Stack):
    """
    Complete application infrastructure for {{project_name}}.

    Layers implemented (bottom-up):
      L0 - Networking (VPC, subnets, security groups)
      L1 - Security (KMS keys, IAM roles, Secrets Manager)
      L2 - Data (Aurora, DynamoDB, S3, Redis)
      L3 - Backend (Lambda microservices, ECS Fargate)
      L4 - API (API Gateway, Cognito, authorizers)
      L5 - Frontend (S3 + CloudFront)
      L6 - Observability (CloudWatch, alarms, X-Ray)
    """

    def __init__(self, scope: Construct, id: str,
                 stage_name: str = "dev", **kwargs: Any) -> None:
        super().__init__(scope, id, **kwargs)

        # === TAGS (applied to all resources in this stack) ===
        Tags.of(self).add("Project", "{{project_name}}")
        Tags.of(self).add("Environment", stage_name)
        Tags.of(self).add("ManagedBy", "CDK")
        Tags.of(self).add("Owner", "{{owner_team}}")

        # === L0: NETWORKING ===
        self._create_networking(stage_name)

        # === L1: SECURITY & ENCRYPTION ===
        self._create_security(stage_name)

        # === L2: DATA LAYER ===
        self._create_data_layer(stage_name)

        # === L3: BACKEND SERVICES ===
        self._create_backend(stage_name)

        # === L4: API LAYER ===
        self._create_api_layer(stage_name)

        # === L5: FRONTEND ===
        self._create_frontend(stage_name)

        # === L6: OBSERVABILITY ===
        self._create_observability(stage_name)

        # === OUTPUTS ===
        self._create_outputs()

    # -------------------------------------------------------------------------
    # L0: NETWORKING
    # -------------------------------------------------------------------------
    def _create_networking(self, stage_name: str) -> None:
        """
        VPC with public, private, and isolated subnets.
        - Public: Load balancers, NAT gateways
        - Private: Lambda functions, ECS tasks (with internet via NAT)
        - Isolated: RDS, ElastiCache (no internet access)
        """
        # [GENERATE ALL NETWORKING CODE FROM ARCHITECTURE MAP L0 SECTION]
        # ...

    # -------------------------------------------------------------------------
    # L1: SECURITY
    # -------------------------------------------------------------------------
    def _create_security(self, stage_name: str) -> None:
        """
        KMS keys for encryption, Secrets Manager for credentials,
        IAM roles for service-to-service authorization.
        """
        # [GENERATE ALL SECURITY CODE FROM ARCHITECTURE MAP L1 SECTION]
        # ...

    # -------------------------------------------------------------------------
    # L2: DATA LAYER
    # -------------------------------------------------------------------------
    def _create_data_layer(self, stage_name: str) -> None:
        """
        Persistent storage: Aurora Serverless V2, DynamoDB, S3, ElastiCache.
        All encrypted at rest using KMS. PITR enabled for all stores.
        """
        # [GENERATE ALL DATA CODE FROM ARCHITECTURE MAP L2 SECTION]
        # ...

    # -------------------------------------------------------------------------
    # L3: BACKEND SERVICES
    # -------------------------------------------------------------------------
    def _create_backend(self, stage_name: str) -> None:
        """
        Lambda microservices loop + ECS Fargate for long-running tasks.
        Each Lambda gets: VPC placement, env vars, IAM grants, CloudWatch logs.
        """
        # [GENERATE ALL BACKEND CODE — LOOP THROUGH DETECTED MICROSERVICES]
        # Include the services loop pattern:
        # MICROSERVICES = [detected service list from Architecture Map]
        # for service_config in MICROSERVICES:
        #     fn = _lambda.Function(...)
        #     table.grant_read_write_data(fn)
        #     cluster.grant_connect(fn)
        # ...

    # -------------------------------------------------------------------------
    # L4: API LAYER
    # -------------------------------------------------------------------------
    def _create_api_layer(self, stage_name: str) -> None:
        """
        API Gateway (REST or HTTP) with Cognito authorizer.
        Each microservice gets an API resource + method binding.
        """
        # [GENERATE ALL API CODE FROM ARCHITECTURE MAP L4 SECTION]
        # ...

    # -------------------------------------------------------------------------
    # L5: FRONTEND
    # -------------------------------------------------------------------------
    def _create_frontend(self, stage_name: str) -> None:
        """
        S3 bucket (private) + CloudFront distribution with OAI.
        WAF attached to CloudFront. ACM certificate for custom domain.
        """
        # [GENERATE ALL FRONTEND CODE FROM ARCHITECTURE MAP L5 SECTION]
        # ...

    # -------------------------------------------------------------------------
    # L6: OBSERVABILITY
    # -------------------------------------------------------------------------
    def _create_observability(self, stage_name: str) -> None:
        """
        CloudWatch dashboard, Lambda error alarms, RDS CPU alarms,
        SQS dead-letter alarms. All alarms notify SNS topic.
        """
        # [GENERATE ALL OBSERVABILITY CODE FROM ARCHITECTURE MAP L6 SECTION]
        # ...

    # -------------------------------------------------------------------------
    # OUTPUTS (CfnOutput for all key resources)
    # -------------------------------------------------------------------------
    def _create_outputs(self) -> None:
        """Export key resource identifiers for cross-stack reference."""
        # [GENERATE ALL CfnOutputs FOR DETECTED RESOURCES]
        # ...
````

## ADDITIONAL CONSTRAINTS:

1. The `stage_name` parameter MUST control all environment-specific configs
2. Use Python conditional expressions: `if stage_name == "prod" else ...`
3. ALL Lambda functions must be defined in a LOOP over a MICROSERVICES list
4. ECS tasks must have CPU and memory appropriate to stage (512/1024 dev, 1024/2048 prod)
5. All database passwords must come from Secrets Manager, not hardcoded
6. Security groups must have MINIMUM ports open (principle of least privilege)
7. CloudFront distribution must use HTTPS-only with TLSv1.2_2021

Generate the COMPLETE Python code with NO placeholders. Every method body must
contain working CDK code, not comments saying "implement here".

```

---

## CODE QUALITY CHECKLIST
Claude verifies these before outputting the code:

- [ ] All imports present and correct (aws-cdk-lib v2 syntax)
- [ ] `stage_name` parameter used for all environment-specific branches
- [ ] Microservices loop pattern used (no copy-paste of Lambda definitions)
- [ ] KMS encryption applied to: S3, DynamoDB, Aurora, CloudWatch Logs
- [ ] `grant_*` methods used for all IAM permissions
- [ ] `removal_policy` set appropriately per environment
- [ ] `CfnOutput` created for all integration points
- [ ] All `Duration`, `Size`, `RemovalPolicy` from aws_cdk (not deprecated imports)
- [ ] ECS Fargate uses `Platform.LINUX_ARM64` for cost savings (if no x86 constraint)
- [ ] Lambda reserved concurrency set (avoid Lambda throttle storms)
```
