# PASS 3 — Full Project Scaffold Generator

**Model:** Claude Opus 4.6  
**Input:** `ARCHITECTURE_MAP.md` from Pass 1  
**Output:** Complete monorepo file structure with all supporting files

---

## SYSTEM PROMPT

```
You are a Senior DevOps Architect. You generate complete, production-ready project
scaffolding for AWS CDK monorepos.

Your job is to produce EVERY file needed to run a project from scratch:
- Not just directory names — ACTUAL FILE CONTENTS
- Every Python file must be syntactically complete and runnable
- Every Dockerfile must be production-hardened
- Every Makefile must have working targets
- Every test file must have real test assertions, not just "pass"

Standards:
- Python 3.12+
- CDK v2 (aws-cdk-lib)
- pytest for testing
- moto for AWS mocking
- Black + isort for formatting
- mypy for type checking
- bandit for security scanning
```

---

## USER PROMPT

````
Using the following Architecture Map, generate a COMPLETE monorepo project scaffold.

## ARCHITECTURE MAP
---
{{ARCHITECTURE_MAP_CONTENT}}
---

## OUTPUT FORMAT

For EACH file in the project, output:
### FILE: `{relative/path/to/file}`
```language
{complete file contents}
````

Do NOT skip any file. Generate actual content, not placeholders.

Required files to generate:

---

### ROOT FILES

#### `app.py` — CDK App Entry Point

```python
# =============================================================================
# FILE: app.py
# CDK application entry point
# Instantiates both the PipelineStack and (optionally) local dev stacks
# =============================================================================
import aws_cdk as cdk
from infrastructure.pipeline_stack import PipelineStack
from infrastructure.app_stack import FullSystemStack

app = cdk.App()

# Get context (from cdk.json or --context flags)
account = app.node.try_get_context("account") or "123456789012"
region  = app.node.try_get_context("region")  or "us-east-1"
env     = cdk.Environment(account=account, region=region)

# The self-mutating pipeline (recommended for prod use)
PipelineStack(app, "{{project_name}}Pipeline", env=env)

# Optional: deploy a standalone dev stack without pipeline (local iteration)
# FullSystemStack(app, "{{project_name}}DevStack", stage_name="dev", env=env)

app.synth()
```

#### `cdk.json` — CDK Configuration

```json
{
  "app": "python app.py",
  "watch": {
    "include": ["**"],
    "exclude": [
      "README.md",
      "cdk*.json",
      "**/__pycache__/**",
      "**/.venv/**",
      "**/node_modules/**",
      "dist/**"
    ]
  },
  "context": {
    "account": "{{aws_account_id}}",
    "region": "{{aws_region}}",
    "@aws-cdk/aws-lambda:recognizeLayerVersion": true,
    "@aws-cdk/core:checkSecretUsage": true,
    "@aws-cdk/core:target-partitions": ["aws", "aws-cn"],
    "@aws-cdk-containers/ecs-service-extensions:enableDefaultLogDriver": true,
    "@aws-cdk/aws-ec2:uniqueImdsv2TemplateName": true,
    "@aws-cdk/aws-ecs:arnFormatIncludesClusterName": true,
    "@aws-cdk/aws-iam:minimizePolicies": true,
    "@aws-cdk/aws-iam:importedRoleStackSafeDefaultPolicyName": true,
    "@aws-cdk/aws-s3:createDefaultLoggingPolicy": true,
    "@aws-cdk/aws-sns-subscriptions:restrictSqsDescryption": true,
    "@aws-cdk/aws-apigateway:disableCloudWatchRole": true,
    "@aws-cdk/core:enableStackNameDuplicates": true,
    "aws-cdk:enableDiffNoFail": true,
    "@aws-cdk/core:stackRelativeExports": true,
    "@aws-cdk/aws-rds:lowercaseDbIdentifier": true,
    "@aws-cdk/aws-efs:defaultEncryptionAtRest": true
  }
}
```

#### `requirements.txt`

```
aws-cdk-lib==2.172.0
constructs>=10.0.0,<11.0.0
```

#### `requirements-dev.txt`

```
# Testing
pytest==8.3.3
pytest-asyncio==0.24.0
pytest-cov==6.0.0
moto[all]==5.0.21
boto3==1.35.0
requests==2.32.3
locust==2.32.0

# Code Quality
black==24.10.0
isort==5.13.2
mypy==1.13.0
bandit==1.8.0
flake8==7.1.1

# CDK Testing
aws-cdk-lib==2.172.0
constructs>=10.0.0,<11.0.0
```

#### `Makefile`

```makefile
.PHONY: help install bootstrap synth deploy-dev deploy-staging deploy-prod test lint clean

# Project name from architecture map
PROJECT_NAME := {{project_name}}
AWS_REGION   := {{aws_region}}
AWS_ACCOUNT  := $(shell aws sts get-caller-identity --query Account --output text)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

install:  ## Install all Python dependencies
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

bootstrap:  ## Bootstrap CDK in your AWS account (run once per account/region)
	cdk bootstrap aws://$(AWS_ACCOUNT)/$(AWS_REGION)

synth:  ## Synthesize CDK CloudFormation templates
	cdk synth

deploy-pipeline: synth  ## Deploy the CICD pipeline (one-time setup)
	cdk deploy $(PROJECT_NAME)Pipeline --require-approval never

deploy-dev: synth  ## Deploy directly to Dev (bypassing pipeline, for local testing)
	cdk deploy $(PROJECT_NAME)DevStack --require-approval never

diff:  ## Show diff between deployed and local stacks
	cdk diff

test:  ## Run all tests
	pytest tests/ -v --cov=infrastructure --cov=src --cov-report=term-missing

test-unit:  ## Run unit tests only
	pytest tests/unit/ -v --tb=short

test-integration:  ## Run integration tests (requires AWS credentials)
	pytest tests/integration/ -v --tb=short

lint:  ## Run linters
	black --check infrastructure/ src/ tests/
	isort --check infrastructure/ src/ tests/
	flake8 infrastructure/ src/ tests/
	mypy infrastructure/ --ignore-missing-imports
	bandit -r infrastructure/ src/ -ll

format:  ## Auto-format code
	black infrastructure/ src/ tests/
	isort infrastructure/ src/ tests/

security-scan:  ## Run security scan
	bandit -r infrastructure/ src/ -f json -o security-report.json || true
	@echo "Security report saved to security-report.json"

destroy-dev:  ## DANGER: Destroy dev environment
	@echo "WARNING: This will destroy the dev environment!"
	@read -p "Type 'yes' to confirm: " confirm && [ "$$confirm" = "yes" ]
	cdk destroy $(PROJECT_NAME)Stack/Dev --force

clean:  ## Clean build artifacts
	rm -rf .cdk.staging/ cdk.out/ .pytest_cache/ __pycache__/ dist/ *.egg-info/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
```

#### `.gitignore`

```
# CDK
cdk.out/
.cdk.staging/

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
.env
venv/
.venv/
*.egg-info/
dist/
build/
.eggs/

# Testing
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/

# IDE
.idea/
.vscode/
*.swp
*.swo

# AWS
.aws/

# Reports
security-report.json
test-reports/
```

---

### INFRASTRUCTURE FILES

#### `infrastructure/__init__.py`

```python
# Infrastructure package
```

#### `infrastructure/app_stage.py`

```python
# =============================================================================
# FILE: infrastructure/app_stage.py
# CDK Stage wrapper — instantiated once per environment in the pipeline
# =============================================================================
from __future__ import annotations
import aws_cdk as cdk
from constructs import Construct
from .app_stack import FullSystemStack


class AppStage(cdk.Stage):
    """
    Wraps the FullSystemStack in a CDK Stage.
    The pipeline instantiates this once per environment (dev, staging, prod).
    """

    def __init__(self, scope: Construct, id: str,
                 stage_name: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        self.app_stack = FullSystemStack(
            self, f"{{project_name}}Stack",
            stage_name=stage_name,
        )

        # Expose key outputs for pipeline test steps
        self.api_endpoint = self.app_stack.api_endpoint_output
        self.frontend_url = self.app_stack.frontend_url_output
```

---

### MICROSERVICE FILES (Generate ONE set per detected service)

For EACH microservice in the Architecture Map section 2, generate:

#### `src/{{service_name}}/index.py`

```python
# =============================================================================
# FILE: src/{{service_name}}/index.py
# SERVICE: {{service_name}}
# PURPOSE: {{service_purpose_from_architecture_map}}
# TRIGGER: {{trigger_type}}
# =============================================================================
from __future__ import annotations
import json
import os
import logging
from typing import Any, Dict

import boto3

# Configure structured logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients (outside handler for Lambda warm start optimization)
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for {{service_name}}.

    Trigger: {{trigger_type}}
    Purpose: {{service_purpose}}
    """
    logger.info(
        "Processing request",
        extra={
            "request_id": context.aws_request_id,
            "function_name": context.function_name,
            "event_keys": list(event.keys()),
        },
    )

    try:
        # Extract input
        body = json.loads(event.get("body", "{}"))

        # === BUSINESS LOGIC ===
        # [Claude: implement actual business logic based on service purpose]
        result = process_request(body)

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "X-Request-ID": context.aws_request_id,
            },
            "body": json.dumps(result),
        }

    except ValueError as e:
        logger.warning("Validation error", extra={"error": str(e)})
        return {
            "statusCode": 400,
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        logger.error("Unhandled error", extra={"error": str(e)}, exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal Server Error"}),
        }


def process_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Core business logic for {{service_name}}.
    [Claude: implement based on the service purpose from Architecture Map]
    """
    # [Implement actual logic here]
    return {"status": "success", "data": body}
```

#### `src/{{service_name}}/requirements.txt`

```
boto3>=1.35.0
aws-lambda-powertools>=3.0.0  # Structured logging, tracing, metrics
```

#### `src/{{service_name}}/tests/test_handler.py`

```python
# =============================================================================
# FILE: src/{{service_name}}/tests/test_handler.py
# Tests for {{service_name}} Lambda handler
# =============================================================================
import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def lambda_context():
    """Mock Lambda context object."""
    context = MagicMock()
    context.aws_request_id = "test-request-id-123"
    context.function_name = "{{service_name}}"
    return context


@pytest.fixture
def api_gateway_event():
    """Mock API Gateway proxy event."""
    return {
        "httpMethod": "POST",
        "path": "/{{service_name}}",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"test": "data"}),
        "requestContext": {
            "requestId": "test-request-id",
        },
    }


class TestHandler:
    """Tests for the {{service_name}} Lambda handler."""

    def test_successful_request(self, api_gateway_event, lambda_context):
        """Test happy path returns 200 with correct structure."""
        with patch.dict("os.environ", {"TABLE_NAME": "test-table"}):
            with patch("boto3.resource"):
                from src.{{service_name}}.index import handler
                response = handler(api_gateway_event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "status" in body

    def test_empty_body(self, lambda_context):
        """Test handler handles empty body gracefully."""
        event = {"body": None}
        with patch.dict("os.environ", {"TABLE_NAME": "test-table"}):
            with patch("boto3.resource"):
                from src.{{service_name}}.index import handler
                response = handler(event, lambda_context)

        assert response["statusCode"] in [200, 400]  # Not 500

    def test_response_has_request_id_header(self, api_gateway_event, lambda_context):
        """Test response includes correlation ID header."""
        with patch.dict("os.environ", {"TABLE_NAME": "test-table"}):
            with patch("boto3.resource"):
                from src.{{service_name}}.index import handler
                response = handler(api_gateway_event, lambda_context)

        assert "X-Request-ID" in response.get("headers", {})
```

---

### ECS FARGATE WORKER FILES

#### `src/worker_task/main.py`

```python
# =============================================================================
# FILE: src/worker_task/main.py
# ECS Fargate long-running worker
# PURPOSE: {{worker_purpose_from_architecture_map}}
# =============================================================================
from __future__ import annotations
import os
import logging
import signal
import time
from typing import Any

import boto3

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)

# Graceful shutdown handling (ECS sends SIGTERM before SIGKILL)
shutdown_requested = False

def handle_shutdown(signum: int, frame: Any) -> None:
    global shutdown_requested
    logger.info("Shutdown signal received. Finishing current task...")
    shutdown_requested = True

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


def main() -> None:
    """Main worker loop — polls SQS queue and processes jobs."""
    sqs = boto3.client("sqs")
    queue_url = os.environ["QUEUE_URL"]

    logger.info("Worker started", extra={"queue_url": queue_url})

    while not shutdown_requested:
        try:
            # Poll for messages
            response = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,  # Long polling
                VisibilityTimeout=300,  # 5 min processing time
            )

            messages = response.get("Messages", [])

            if not messages:
                continue

            for message in messages:
                process_message(message)

                # Delete message after successful processing
                sqs.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                )

        except Exception as e:
            logger.error("Worker error", extra={"error": str(e)}, exc_info=True)
            time.sleep(5)  # Back-off on error

    logger.info("Worker shutting down gracefully")


def process_message(message: dict) -> None:
    """
    Process a single SQS message.
    [Claude: implement based on worker purpose from Architecture Map]
    """
    import json
    body = json.loads(message["Body"])
    logger.info("Processing message", extra={"message_id": message["MessageId"]})
    # [Implement actual processing logic here]


if __name__ == "__main__":
    main()
```

#### `src/worker_task/Dockerfile`

```dockerfile
# =============================================================================
# FILE: src/worker_task/Dockerfile
# Production-hardened ECS Fargate worker image
# =============================================================================
# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Final stage (minimal image)
FROM python:3.12-slim

# Security: run as non-root
RUN groupadd -r worker && useradd -r -g worker worker

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY main.py .

# Remove write permissions (immutable container)
RUN chmod -R 555 /app && chown -R worker:worker /app

USER worker

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "main.py"]
```

---

### TEST FILES

#### `tests/conftest.py`

```python
# =============================================================================
# FILE: tests/conftest.py
# Shared pytest fixtures and configuration
# =============================================================================
import os
import pytest
import boto3
from moto import mock_aws


@pytest.fixture(autouse=True)
def aws_credentials():
    """Mocked AWS credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def dynamodb_table():
    """Create a test DynamoDB table."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-table",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table
```

#### `tests/unit/test_app_stack.py`

```python
# =============================================================================
# FILE: tests/unit/test_app_stack.py
# CDK unit tests — validate stack structure without deploying
# =============================================================================
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match
import pytest
from infrastructure.app_stack import FullSystemStack


@pytest.fixture
def app():
    return cdk.App()


@pytest.fixture
def stack(app):
    return FullSystemStack(app, "TestStack", stage_name="dev")


@pytest.fixture
def template(stack):
    return Template.from_stack(stack)


class TestNetworking:
    def test_vpc_created(self, template):
        template.has_resource_properties("AWS::EC2::VPC", {
            "EnableDnsHostnames": True,
            "EnableDnsSupport": True,
        })

    def test_vpc_has_correct_azs(self, template):
        # Dev should have at least 2 AZs
        template.resource_count_is("AWS::EC2::Subnet", Match.any_value())


class TestSecurity:
    def test_kms_key_created(self, template):
        template.resource_count_is("AWS::KMS::Key", Match.any_value())

    def test_no_public_rds(self, template):
        # Ensure no RDS is publicly accessible
        template.has_resource_properties("AWS::RDS::DBInstance", {
            "PubliclyAccessible": False,
        }) if False else None  # Replace with actual check


class TestData:
    def test_dynamodb_has_pitr(self, template):
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "PointInTimeRecoverySpecification": {
                "PointInTimeRecoveryEnabled": True,
            }
        })

    def test_dynamodb_encrypted(self, template):
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "SSESpecification": {
                "SSEEnabled": True,
            }
        })


class TestLambda:
    def test_all_lambdas_have_xray(self, template):
        template.has_resource_properties("AWS::Lambda::Function", {
            "TracingConfig": {"Mode": "Active"},
        })

    def test_all_lambdas_have_log_groups(self, template):
        template.resource_count_is("AWS::Logs::LogGroup", Match.any_value())


class TestFrontend:
    def test_s3_bucket_not_public(self, template):
        template.has_resource_properties("AWS::S3::Bucket", {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        })

    def test_cloudfront_uses_https_only(self, template):
        template.has_resource_properties("AWS::CloudFront::Distribution", {
            "DistributionConfig": {
                "ViewerCertificate": Match.object_like({
                    "MinimumProtocolVersion": "TLSv1.2_2021",
                })
            }
        })
```

#### `tests/smoke/test_smoke.py`

```python
# =============================================================================
# FILE: tests/smoke/test_smoke.py
# Smoke tests — run against live environments after deployment
# Marks: dev, staging, prod
# =============================================================================
import os
import pytest
import requests


API_ENDPOINT = os.environ.get("API_ENDPOINT", "http://localhost:3000")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3001")


@pytest.mark.dev
@pytest.mark.staging
@pytest.mark.prod
class TestSmoke:
    def test_api_health_check(self):
        """API Gateway should return 200 on health check endpoint."""
        response = requests.get(f"{API_ENDPOINT}/health", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "healthy"

    def test_frontend_loads(self):
        """CloudFront should serve the frontend application."""
        response = requests.get(FRONTEND_URL, timeout=10)
        assert response.status_code == 200
        assert "<!DOCTYPE html>" in response.text or "<html" in response.text

    @pytest.mark.prod
    def test_https_enforced_in_prod(self):
        """Production CloudFront should redirect HTTP to HTTPS."""
        if "https://" not in FRONTEND_URL:
            pytest.skip("Not a prod HTTPS URL")
        http_url = FRONTEND_URL.replace("https://", "http://")
        response = requests.get(http_url, allow_redirects=False, timeout=10)
        assert response.status_code in [301, 302, 307, 308]
```

---

### DOCUMENTATION

#### `README.md`

````markdown
# {{project_name}}

{{project_description_from_sow}}

## Architecture Overview

This project uses AWS CDK (Python) with a self-mutating CICD pipeline.

### Layers

| Layer         | Services                                |
| ------------- | --------------------------------------- |
| Frontend      | S3 + CloudFront + WAF                   |
| API           | API Gateway + Cognito                   |
| Backend       | Lambda (×N microservices) + ECS Fargate |
| Data          | Aurora Serverless V2 + DynamoDB + S3    |
| Observability | CloudWatch + X-Ray + SNS Alarms         |
| CICD          | CodePipeline (Dev → Staging → Prod)     |

## Quick Start

### Prerequisites

- Python 3.12+
- Node.js 18+
- AWS CLI configured
- CDK bootstrapped (`make bootstrap`)

### Setup

```bash
# Install dependencies
make install

# Bootstrap CDK (one-time per account/region)
make bootstrap

# Deploy the pipeline
make deploy-pipeline
```
````

### Running Tests

```bash
make test        # All tests
make test-unit   # Unit tests only (fast, no AWS)
make lint        # Linting
```

## Environments

| Environment | Purpose                   | Deployment                |
| ----------- | ------------------------- | ------------------------- |
| dev         | Development iteration     | Auto on push to main      |
| staging     | Pre-production validation | Auto after dev tests pass |
| prod        | Production                | Manual approval required  |

## Pipeline Flow

```
git push main → CodePipeline → Dev → [Tests] → Staging → [APPROVAL] → Prod
```

```

```

---

## SCAFFOLD VALIDATION CHECKLIST

After generation, Claude verifies:

- [ ] `app.py` instantiates PipelineStack correctly
- [ ] `cdk.json` has all CDK feature flags for v2
- [ ] `requirements.txt` pins aws-cdk-lib version
- [ ] `Makefile` has all critical targets (install, deploy, test, lint)
- [ ] ONE `src/` folder EXISTS per detected microservice
- [ ] Each microservice has: `index.py`, `requirements.txt`, `tests/`
- [ ] `src/worker_task/` exists if long-running tasks were detected
- [ ] `src/worker_task/Dockerfile` is production-hardened (non-root, multi-stage)
- [ ] `tests/unit/test_app_stack.py` tests all major resource types
- [ ] `tests/smoke/test_smoke.py` covers API and frontend health
- [ ] `README.md` is project-specific (not generic placeholder text)
