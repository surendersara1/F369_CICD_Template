# RAG Research Agent — Statement of Work (SOW)

**Project Name:** rag-research-agent
**Client:** Internal R&D / Consulting Delivery
**Model Target:** Claude Opus 4.6 via CDK CICD Template Library
**Output Directory:** `RAG_RESEARCH_AGENT/`

---

## Instructions for Claude

Paste this entire document into Claude Opus 4.6 using the `MASTER_ORCHESTRATOR.md` system prompt.
Claude will execute all THREE PASSES and produce a complete deployable CDK project.

---

## STATEMENT OF WORK

### 1. Executive Summary

Build a **RAG-powered Research Agent** using the **Strands SDK** connected to **Claude Sonnet 4.5** via Amazon Bedrock. The agent ingests documents (PDF, DOCX, TXT, HTML) into a Bedrock Knowledge Base backed by OpenSearch Serverless vector store, then answers user questions with cited, grounded responses. The system includes a **streaming chat dashboard** (React) with session history, a multi-agent architecture for complex research tasks, and a full CICD pipeline deploying to dev/staging/prod.

### 2. Functional Requirements

#### 2.1 Document Ingestion Pipeline
- Users upload documents (PDF, DOCX, TXT, HTML, Markdown) via the chat UI or a dedicated upload endpoint
- Documents are stored in S3 (encrypted, versioned)
- Bedrock Knowledge Base automatically chunks, embeds (Titan Embed V2), and indexes documents into OpenSearch Serverless vector store
- Support for multiple knowledge base collections (e.g., "engineering-docs", "legal-contracts", "product-specs")
- Metadata extraction: title, author, upload date, document type, source URL
- Maximum document size: 50MB per file

#### 2.2 Strands Research Agent
- Built with **Strands SDK** (`from strands import Agent, tool`)
- Connected to **Claude Sonnet 4.5** (`anthropic.claude-sonnet-4-20250514-v1:0`) via Bedrock as the primary LLM
- Uses **Claude Haiku** (`anthropic.claude-3-haiku-20240307-v1:0`) for fast routing and classification tasks
- Custom `@tool` functions:
  - `search_knowledge_base` — RAG retrieval from Bedrock Knowledge Base
  - `web_search` — Search the internet for current information (via Tavily or SerpAPI)
  - `save_research_report` — Save structured research output to S3 as PDF/Markdown
  - `summarize_document` — Summarize a specific uploaded document
  - `compare_documents` — Compare two or more documents side-by-side
  - `extract_entities` — Extract named entities (people, orgs, dates, amounts) from text
  - `cite_sources` — Format citations with page numbers and document references
- Conversation memory: DynamoDB session table with 24-hour TTL
- Maximum 30 tool-use turns per request
- System prompt enforces: always cite sources, never fabricate information, ask for clarification when ambiguous

#### 2.3 Multi-Agent Orchestration
- **Supervisor Agent** (Claude Sonnet 4.5): Routes complex research queries to specialist workers
- **Deep Research Agent**: Performs multi-hop retrieval across multiple knowledge base collections
- **Summarization Agent**: Condenses long documents and multi-source findings into executive summaries
- **Fact-Check Agent**: Cross-references claims against knowledge base and flags contradictions
- Supervisor collects results from workers and synthesizes a final grounded response

#### 2.4 Streaming Chat Dashboard (React)
- Real-time streaming of agent responses via WebSocket (API Gateway v2)
- REST fallback for non-streaming invocations
- Chat features:
  - Markdown rendering of agent responses (headers, lists, code blocks, tables)
  - Source citation cards (clickable links to original documents with page numbers)
  - Tool-use visualization (show which tools the agent called and their results)
  - "Thinking" indicator while agent processes
  - File upload drag-and-drop for document ingestion directly in chat
- Session management:
  - List previous conversations (sidebar)
  - Resume any previous session
  - Delete sessions
  - Export conversation as PDF/Markdown
- Authentication: Cognito User Pool with email sign-up, MFA optional in dev, required in prod
- Responsive design (desktop + tablet)

#### 2.5 AgentCore Deployment
- Deploy the Strands agent via **Bedrock AgentCore** for managed hosting
- AgentCore Gateway with OAuth2 authentication (Cognito)
- AgentCore Memory enabled:
  - Short-term memory (STM): conversation context within session
  - Long-term memory (LTM): user preferences, frequently asked topics, research patterns
  - Memory strategies: SUMMARY, USER_PREFERENCE, SEMANTIC

#### 2.6 Agent Evaluation & Quality
- Golden dataset with 50+ test cases covering:
  - Basic Q&A (knowledge base retrieval)
  - Multi-hop reasoning (cross-document queries)
  - Tool-use correctness (right tool called for right task)
  - Safety (prompt injection resistance, no hallucination)
  - Multi-turn context retention
- LLM-as-judge scoring (Claude Sonnet 4.5 grades responses 1-5)
- Prompt regression testing: compare eval scores before/after prompt changes
- CICD quality gate: block production deploy if eval score drops below 0.85
- CloudWatch dashboard tracking: overall score, tool accuracy, latency, cost per eval run

### 3. Non-Functional Requirements

#### 3.1 Performance
- Agent response latency: < 10 seconds for simple queries, < 30 seconds for multi-agent research
- WebSocket streaming: first token within 2 seconds
- Knowledge base retrieval: < 3 seconds for vector search
- Chat dashboard load time: < 2 seconds (Core Web Vitals compliant)

#### 3.2 Security
- All data encrypted at rest (KMS) and in transit (TLS 1.2+)
- Bedrock Guardrails enabled:
  - PII redaction (detect and mask SSN, credit card, phone numbers in responses)
  - Topic blocking (block requests about competitors, internal financials)
  - Grounding check (verify response is grounded in retrieved documents)
- Cognito authentication required for all API and WebSocket endpoints
- WAF on CloudFront (OWASP Top 10, rate limiting 1000 req/5min per IP)
- VPC isolation for all Lambda functions and OpenSearch
- IAM least-privilege for all roles

#### 3.3 Scalability
- Serverless architecture: Lambda for agent execution, OpenSearch Serverless for vector store
- DynamoDB on-demand billing for session and eval tables
- CloudFront for global frontend distribution
- ECS Fargate for long-running research tasks (>15 min multi-agent sessions)

#### 3.4 Observability
- CloudWatch dashboards: API latency, Lambda errors, agent invocation count, token usage
- X-Ray distributed tracing across all Lambda functions
- Agent-specific metrics: tool call frequency, average turns per session, knowledge base hit rate
- SNS alerts on: Lambda errors > 5/5min, API 5xx > 10/5min, eval score regression, cost spike
- Structured JSON logging for all Lambda functions

#### 3.5 Cost Optimization
- Claude Haiku for routing/classification (10x cheaper than Sonnet)
- OpenSearch Serverless (pay per OCU, scales to zero)
- Lambda ARM64 architecture (20% cheaper than x86)
- S3 Intelligent-Tiering for document storage
- 90-day lifecycle on agent artifacts bucket
- Token usage tracking with hourly cost alarm

### 4. Environments

| Resource | Dev | Staging | Prod |
|----------|-----|---------|------|
| Lambda Memory | 512MB | 1024MB | 1024MB |
| Lambda Timeout | 15min | 15min | 15min |
| ECS Fargate | 1 task (1 vCPU/2GB) | 1 task | 2 tasks |
| OpenSearch OCU | 2 min | 2 min | 4 min |
| CloudFront | PriceClass_100 | PriceClass_100 | PriceClass_All |
| WAF | Count mode | Block mode | Block + rate limit |
| Cognito MFA | Optional | Optional | Required |
| Guardrails | Enabled (log only) | Enabled (block) | Enabled (block) |
| Deletion Policy | DESTROY | SNAPSHOT | RETAIN |
| Agent Eval Gate | Skip | Run (warn only) | Run (block deploy) |

### 5. CICD Pipeline

- Source: GitHub (`{{org}}/rag-research-agent`, `main` branch)
- Pipeline stages:
  1. Source (GitHub webhook)
  2. Build (pip install, pytest unit tests, bandit security scan, cdk synth)
  3. Dev Deploy (auto-deploy, smoke tests post-deploy)
  4. Agent Eval (run golden dataset against dev agent, warn on regression)
  5. Integration Tests (API endpoint tests, WebSocket connection test)
  6. Staging Deploy (auto-deploy after tests pass)
  7. Agent Eval Gate (run golden dataset against staging agent, BLOCK if score < 0.85)
  8. Manual Approval (email notification to tech lead)
  9. Production Deploy (with CloudWatch alarm rollback)
  10. Prod Smoke Tests (health check, basic agent invocation)

### 6. Technology Stack Summary

| Component | Technology |
|-----------|-----------|
| Agent Framework | Strands SDK (Python) |
| Primary LLM | Claude Sonnet 4.5 via Bedrock |
| Fast LLM | Claude Haiku via Bedrock |
| Embedding Model | Amazon Titan Embed V2 |
| Vector Store | OpenSearch Serverless |
| Knowledge Base | Bedrock Knowledge Bases |
| Agent Hosting | Bedrock AgentCore |
| Agent Memory | AgentCore Memory (STM + LTM) |
| Agent Gateway | AgentCore Gateway (MCP + OAuth2) |
| Compute | Lambda (agents) + ECS Fargate (long-running) |
| API | API Gateway REST + API Gateway v2 WebSocket |
| Auth | Cognito User Pool |
| Frontend | React SPA + CloudFront + S3 |
| Data | DynamoDB (sessions, eval) + S3 (documents, artifacts) |
| Safety | Bedrock Guardrails (PII, topics, grounding) |
| Observability | CloudWatch + X-Ray + SNS |
| IaC | AWS CDK v2 (Python) |
| CICD | CodePipeline (self-mutating) |
| Testing | pytest + moto + golden dataset eval + LLM-as-judge |

---

## GENERATION INSTRUCTIONS

Execute all THREE PASSES using the CDK CICD Template Library:

### PASS 1 — Architecture Detection
Use `01_SOW_ARCHITECTURE_DETECTOR.md` to analyze this SOW and produce `ARCHITECTURE_MAP.md`.

Expected partial triggers based on this SOW:
- `LAYER_NETWORKING.md` — VPC for Lambda, OpenSearch
- `LAYER_SECURITY.md` — KMS, IAM, Secrets Manager
- `LAYER_DATA.md` — DynamoDB, S3, OpenSearch Serverless
- `LAYER_BACKEND_LAMBDA.md` — Agent Lambda functions
- `LAYER_BACKEND_ECS.md` — Long-running multi-agent research tasks
- `LAYER_API.md` — API Gateway REST, Cognito auth
- `LAYER_FRONTEND.md` — S3 + CloudFront + WAF
- `LAYER_OBSERVABILITY.md` — CloudWatch, X-Ray, SNS
- `CICD_PIPELINE_STAGES.md` — Dev → Staging → Prod pipeline
- `LLMOPS_BEDROCK.md` — Knowledge Bases, Guardrails, LLM Gateway
- `STRANDS_AGENT_RUNTIME.md` — Strands agent, custom tools, multi-agent orchestration
- `STRANDS_AGENTCORE_DEPLOY.md` — AgentCore deployment, Gateway MCP, Memory
- `STRANDS_AGENT_FRONTEND.md` — WebSocket streaming chat, React UI, session management
- `STRANDS_AGENT_EVAL.md` — Golden dataset eval, LLM judge, prompt regression, CICD gate

### PASS 2A — App Stack
Use `02A_APP_STACK_GENERATOR.md` to generate `infrastructure/app_stack.py` with ALL layers including:
- `_create_networking()`
- `_create_security()`
- `_create_data_layer()` (DynamoDB + S3 + OpenSearch Serverless)
- `_create_backend()` (Lambda microservices)
- `_create_api_layer()` (API Gateway + Cognito)
- `_create_frontend()` (S3 + CloudFront + WAF)
- `_create_observability()` (CloudWatch + X-Ray + SNS)
- `_create_strands_agent_runtime()` (Strands agent Lambda/ECS, sessions, artifacts)
- `_create_strands_agentcore()` (AgentCore Gateway, Memory, OAuth2)
- `_create_strands_agent_frontend()` (WebSocket API, connection table, session REST)
- `_create_strands_agent_eval()` (Eval pipeline, golden datasets, score dashboard)

### PASS 2B — Pipeline Stack
Use `02B_PIPELINE_STACK_GENERATOR.md` to generate `infrastructure/pipeline_stack.py` with:
- Standard 7-stage pipeline
- Agent eval gate between staging and prod (STRANDS_AGENT_EVAL.md CICD integration)

### PASS 3 — Project Scaffold
Use `03_PROJECT_SCAFFOLD_GENERATOR.md` to generate the complete monorepo under `RAG_RESEARCH_AGENT/`:

```
RAG_RESEARCH_AGENT/
├── app.py
├── cdk.json
├── requirements.txt
├── requirements-dev.txt
├── Makefile
├── .gitignore
├── .bedrock_agentcore.yaml
├── README.md
│
├── infrastructure/
│   ├── __init__.py
│   ├── app_stack.py
│   ├── pipeline_stack.py
│   └── app_stage.py
│
├── src/
│   ├── strands_agent/
│   │   ├── index.py              (Strands agent + custom @tools)
│   │   ├── agentcore_app.py      (AgentCore deployment wrapper)
│   │   ├── multi_agent.py        (Supervisor + worker agents)
│   │   └── requirements.txt
│   │
│   ├── agent_frontend/
│   │   ├── ws_connect/index.py
│   │   ├── ws_message/index.py
│   │   ├── ws_disconnect/index.py
│   │   └── session_mgmt/index.py
│   │
│   ├── agent_eval/
│   │   ├── runner/index.py
│   │   └── prompt_regression.py
│   │
│   ├── document_ingestion/
│   │   ├── index.py              (S3 upload trigger → KB sync)
│   │   └── requirements.txt
│   │
│   └── gateway_tools/
│       ├── db_tool/index.py
│       └── api_tool/index.py
│
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── App.tsx
│   │   ├── hooks/useAgentChat.ts
│   │   ├── components/
│   │   │   ├── AgentChat.tsx
│   │   │   ├── SessionSidebar.tsx
│   │   │   ├── SourceCitationCard.tsx
│   │   │   └── FileUpload.tsx
│   │   └── config/runtime.ts
│   └── public/index.html
│
├── eval/
│   └── golden-datasets/
│       ├── core-qa.json
│       ├── multi-hop.json
│       ├── tool-use.json
│       ├── safety.json
│       └── multi-turn.json
│
└── tests/
    ├── unit/test_app_stack.py
    ├── integration/test_agent_api.py
    ├── smoke/test_smoke.py
    └── conftest.py
```

---

## KEY DESIGN DECISIONS

1. **Strands over Bedrock Agents**: We use Strands SDK for full control over agent behavior, custom tools, and multi-agent orchestration. Bedrock Agents (managed) is too opaque for research use cases.

2. **AgentCore for hosting**: Instead of managing Lambda/ECS ourselves, AgentCore provides managed hosting with built-in Gateway and Memory. The CDK creates supporting infra (Cognito, tool Lambdas), AgentCore manages the runtime.

3. **WebSocket for streaming**: Agent responses stream token-by-token via WebSocket. REST is fallback only. This gives the chat UI a responsive feel even for complex multi-agent queries.

4. **Eval-gated deploys**: No agent reaches production without passing the golden dataset evaluation. This prevents prompt regressions and quality degradation from shipping to users.

5. **Multi-agent for complex research**: Simple questions go to the single agent. Complex queries (multi-hop, cross-document, fact-checking) are routed by the supervisor to specialist workers.
