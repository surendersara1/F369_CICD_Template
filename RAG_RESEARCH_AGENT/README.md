# RAG Research Agent

A production-grade RAG-powered Research Agent built with **Strands SDK**, **Amazon Bedrock** (Claude Sonnet 4.5), and **AWS CDK**. The system ingests documents into a Bedrock Knowledge Base backed by OpenSearch Serverless, then answers user questions with cited, grounded responses through a streaming React chat dashboard.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CloudFront + WAF                         │
│                     (React SPA Frontend)                        │
├─────────────────────────────────────────────────────────────────┤
│  API Gateway REST          │  API Gateway v2 WebSocket          │
│  (Cognito Auth)            │  (Streaming Agent Responses)       │
├────────────────────────────┼────────────────────────────────────┤
│  Lambda: Strands Agent     │  Lambda: WS Connect/Message/Disc   │
│  Lambda: Doc Ingestion     │  Lambda: Session Management        │
│  Lambda: Eval Runner       │  Lambda: Gateway Tools (MCP)       │
│  ECS Fargate: Long-running │                                    │
├─────────────────────────────────────────────────────────────────┤
│  Bedrock KB + Guardrails   │  AgentCore (Gateway + Memory)      │
│  OpenSearch Serverless     │  Step Functions (Eval Pipeline)     │
│  DynamoDB (Sessions/Eval)  │  S3 (Documents/Artifacts/Eval)     │
├─────────────────────────────────────────────────────────────────┤
│  VPC + Subnets + Endpoints │  KMS + CloudTrail + IAM            │
│  CloudWatch + X-Ray + SNS  │  Cognito User Pools                │
└─────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Strands SDK Agent** with 7 custom `@tool` functions (KB search, web search, summarize, compare, entities, citations, save report)
- **Multi-Agent Orchestration**: Supervisor routes to Deep Research, Summarization, and Fact-Check workers
- **Streaming Chat UI**: React + WebSocket with markdown rendering, source citation cards, file upload
- **Bedrock Knowledge Base**: Hierarchical chunking, Titan Embed V2, OpenSearch Serverless vector store
- **Bedrock Guardrails**: PII redaction, topic blocking, content filtering
- **AgentCore Deployment**: Managed hosting with Gateway (MCP + OAuth2) and Memory (STM + LTM)
- **Eval-Gated CICD**: Golden dataset evaluation blocks production deploys if score < 0.85
- **10-Stage Pipeline**: Source → Build → Dev → Eval → Integration → Staging → Eval Gate → Approval → Prod → Smoke

## Prerequisites

- Python 3.12+
- Node.js 18+ (for CDK CLI and React frontend)
- AWS CDK v2 (`npm install -g aws-cdk`)
- AWS CLI configured with appropriate credentials
- Docker (for ECS Fargate builds and `cdk synth` with bundling)

## Quick Start

```bash
# Clone and enter project
cd RAG_RESEARCH_AGENT

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Bootstrap CDK (first time only)
cdk bootstrap aws://ACCOUNT_ID/REGION

# Synthesize CloudFormation templates
cdk synth

# Deploy dev environment
cdk deploy Dev/AppStack
```

## Project Structure

```
RAG_RESEARCH_AGENT/
├── app.py                          # CDK app entry point
├── cdk.json                        # CDK configuration
├── requirements.txt                # CDK + runtime dependencies
├── requirements-dev.txt            # Test + dev dependencies
├── Makefile                        # Build/test/deploy shortcuts
├── .bedrock_agentcore.yaml         # AgentCore deployment config
│
├── infrastructure/
│   ├── app_stack.py                # Full system stack (12 layers)
│   ├── pipeline_stack.py           # 10-stage CICD pipeline
│   └── app_stage.py                # CDK Stage wrapper
│
├── src/
│   ├── strands_agent/
│   │   ├── index.py                # Strands agent + 7 custom @tools
│   │   ├── agentcore_app.py        # AgentCore deployment wrapper
│   │   ├── multi_agent.py          # Supervisor + 3 worker agents
│   │   └── requirements.txt
│   ├── agent_frontend/
│   │   ├── ws_connect/index.py     # WebSocket $connect handler
│   │   ├── ws_message/index.py     # WebSocket $default handler (streaming)
│   │   ├── ws_disconnect/index.py  # WebSocket $disconnect handler
│   │   └── session_mgmt/index.py   # REST session CRUD
│   ├── agent_eval/
│   │   ├── runner/index.py         # Eval runner (load, run, judge, aggregate)
│   │   └── prompt_regression.py    # Prompt regression comparison
│   ├── document_ingestion/
│   │   ├── index.py                # S3 upload + KB sync trigger
│   │   └── requirements.txt
│   └── gateway_tools/
│       ├── db_tool/index.py        # AgentCore Gateway DB tool
│       └── api_tool/index.py       # AgentCore Gateway API tool
│
├── frontend/
│   ├── package.json
│   ├── public/index.html
│   └── src/
│       ├── App.tsx                  # Main app with auth + routing
│       ├── hooks/useAgentChat.ts    # WebSocket chat hook
│       ├── components/
│       │   ├── AgentChat.tsx        # Chat interface
│       │   ├── SessionSidebar.tsx   # Session history sidebar
│       │   ├── SourceCitationCard.tsx # Citation display
│       │   └── FileUpload.tsx       # Drag-and-drop upload
│       └── config/runtime.ts        # Runtime config loader
│
├── eval/
│   └── golden-datasets/             # 50+ test cases across 5 categories
│       ├── core-qa.json
│       ├── multi-hop.json
│       ├── tool-use.json
│       ├── safety.json
│       └── multi-turn.json
│
└── tests/
    ├── conftest.py                  # Shared CDK fixtures
    ├── unit/test_app_stack.py       # CDK stack unit tests (60+ assertions)
    ├── integration/test_agent_api.py # Live API integration tests
    └── smoke/test_smoke.py          # Post-deploy health checks
```

## Development

### Run Unit Tests

```bash
pytest tests/unit/ -v --tb=short
```

### Run Integration Tests (requires deployed stack)

```bash
export API_URL=https://xxx.execute-api.us-east-1.amazonaws.com/dev
export WS_URL=wss://xxx.execute-api.us-east-1.amazonaws.com/dev
export USER_POOL_ID=us-east-1_xxxxx
export USER_POOL_CLIENT_ID=xxxxx
export TEST_USERNAME=test@example.com
export TEST_PASSWORD=YourPassword123!
pytest tests/integration/ -v
```

### Run Smoke Tests (post-deploy)

```bash
export API_URL=https://xxx.execute-api.us-east-1.amazonaws.com/dev
export CLOUDFRONT_URL=https://dxxxxx.cloudfront.net
pytest tests/smoke/ -v -k test_health
```

### Build Frontend

```bash
cd frontend
npm install
npm run build
```

### Makefile Shortcuts

```bash
make install      # Install all dependencies
make test         # Run unit tests
make lint         # Run black + isort + bandit
make synth        # CDK synth
make deploy-dev   # Deploy to dev
make deploy-prod  # Deploy to prod (requires approval)
```

## Environments

| Setting | Dev | Staging | Prod |
|---------|-----|---------|------|
| Lambda Memory | 512 MB | 1024 MB | 1024 MB |
| Lambda Timeout | 15 min | 15 min | 15 min |
| OpenSearch Standby | Disabled | Disabled | Enabled |
| CloudFront Price Class | 100 | 100 | All |
| WAF Mode | Count | Block | Block + Rate Limit |
| Cognito MFA | Optional | Optional | Required |
| Eval Gate | Skip | Warn | Block (< 0.85) |
| Deletion Policy | Destroy | Snapshot | Retain |

## Agent Evaluation

The eval pipeline uses a golden dataset of 50+ test cases across 5 categories:

1. **Core Q&A** — Knowledge base retrieval accuracy
2. **Multi-Hop** — Cross-document reasoning
3. **Tool Use** — Correct tool selection and execution
4. **Safety** — Prompt injection resistance, no hallucination
5. **Multi-Turn** — Context retention across conversation turns

Evaluation runs as a Step Functions workflow:
1. Load dataset from S3
2. Run each test case against the agent (5 concurrent)
3. LLM-as-judge scores responses 1-5
4. Aggregate scores and publish to CloudWatch
5. Quality gate: block deploy if overall score < 0.85

## Security

- All data encrypted at rest (KMS CMK) and in transit (TLS 1.2+)
- Bedrock Guardrails: PII anonymization (email, phone), PII blocking (SSN, credit cards), topic denial
- Cognito authentication on all API and WebSocket endpoints
- WAF with AWS Managed Rules (OWASP Top 10) and IP rate limiting
- VPC isolation for Lambda functions and OpenSearch Serverless
- CloudTrail audit logging with S3 data events and Lambda data events
- IAM least-privilege roles per service

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Agent Framework | Strands SDK (Python) |
| Primary LLM | Claude Sonnet 4.5 via Bedrock |
| Fast LLM | Claude Haiku via Bedrock |
| Embedding | Amazon Titan Embed V2 (1024d) |
| Vector Store | OpenSearch Serverless |
| Knowledge Base | Bedrock Knowledge Bases |
| Agent Hosting | Bedrock AgentCore |
| Compute | Lambda (ARM64) + ECS Fargate |
| API | API Gateway REST + WebSocket |
| Auth | Cognito User Pool |
| Frontend | React SPA + CloudFront + WAF |
| Data | DynamoDB + S3 |
| Safety | Bedrock Guardrails |
| Observability | CloudWatch + X-Ray + SNS |
| IaC | AWS CDK v2 (Python) |
| CICD | CodePipeline (self-mutating) |
| Testing | pytest + moto + golden dataset eval |

## License

Internal use only. Proprietary to consulting delivery team.
