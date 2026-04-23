# SOP — Enterprise Chat Router (Strands multi-source agent over lakehouse + docs + images)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · Strands Agents SDK 1.x · Claude Sonnet 4.7 supervisor · Claude Haiku 4.5 for cheap tool decisions · Bedrock Runtime Converse API · AgentCore Memory for session state · AgentCore Identity for OBO (on-behalf-of) auth · API Gateway WebSocket API for streaming · CloudFront-fronted web UI (optional) · `PATTERN_TEXT_TO_SQL` + `PATTERN_SEMANTIC_DATA_DISCOVERY` + `PATTERN_DOC_INGESTION_RAG` + `PATTERN_MULTIMODAL_EMBEDDINGS` as tools

---

## 1. Purpose

- Provide the deep-dive for the **hero agent** of the AI-native lakehouse kit — a Strands-powered supervisor that receives a single user question and **orchestrates across all available knowledge sources**: structured data (via Athena text-to-SQL), unstructured docs (via RAG), images/diagrams (via multimodal search), and schema discovery (via the find-my-data API). Returns a blended answer with inline citations + drill-down links.
- Codify the **Strands supervisor pattern** — one LLM-driven agent with a tool library, plus optional sub-agents (e.g. a SQL-specialist sub-agent for complex multi-step queries, a doc-specialist for multi-hop RAG). Uses the **Converse API** with tool use, not the lower-level `invoke_model` — Converse natively handles tool-invocation loops.
- Codify the **tool contract** — each tool is a Lambda (or Lambda-wrapped function) with a JSON schema. Four standard tools + optional extensions:
  1. `text_to_sql` — invokes `PATTERN_TEXT_TO_SQL` Lambda. Returns rows + SQL + lineage.
  2. `semantic_discovery` — invokes `PATTERN_SEMANTIC_DATA_DISCOVERY`. Returns tables + columns + images.
  3. `doc_rag` — invokes `PATTERN_DOC_INGESTION_RAG` query-side. Returns chunks + citations.
  4. `multimodal_search` — invokes `PATTERN_MULTIMODAL_EMBEDDINGS` query. Returns images + signed previews.
  5. (optional) `compute` — via `AGENTCORE_CODE_INTERPRETER`. For quantitative analysis over returned rows.
  6. (optional) `web_search` — via `AGENTCORE_BROWSER_TOOL`. For enrichment from external sources.
- Codify the **routing heuristics baked into the supervisor system prompt** — "If the question involves 'how many' / 'total' / 'average' → use text_to_sql. If 'what does X say about Y' → doc_rag. If 'show me a diagram' → multimodal_search. If unclear about data location → semantic_discovery first." But DO NOT hard-code routing; let the LLM decide and instrument the choice for telemetry.
- Codify the **blended-answer composition** — when multiple tools fire (common case), the supervisor synthesises: "Based on the data (SQL below) + the contract in section 4.2 of [doc], customers X, Y, Z …" with CITATIONS:
  - `[SQL-1]` linked to the exact SQL + rows
  - `[DOC-1]` linked to the source doc page
  - `[IMG-1]` linked to the thumbnail
- Codify the **streaming contract** — API Gateway WebSocket API for bidirectional streaming. Each tool invocation streams progress events (`tool_start`, `tool_chunk`, `tool_end`); each LLM token streams through. The UI shows "Running SQL…" → "Retrieving docs…" → partial text.
- Codify the **session memory via AgentCore Memory** — each user session has a memory resource that stores the conversation + tool invocations + relevant entities. The supervisor's system prompt includes a summary of prior turns (not verbatim — summarised to stay under token budgets).
- Codify the **OBO (on-behalf-of) auth via AgentCore Identity** — the agent inherits the caller's identity for LF-enforced queries. A user with `finance + internal` permissions calling the agent does NOT suddenly have `hr + confidential` access just because the agent's execution role would. **Identity propagation is the single most important safety property.**
- Codify the **budget + rate limiting** — per-session token budget (enforced by the supervisor before each tool call), per-caller daily budget (DDB same pattern as text-to-SQL), session timeout (30 min idle → summary + teardown).
- Codify the **test harness** — golden-set of ~30 questions with expected-tools-called + expected-citations. Run nightly; regressions in tool routing are the #1 bug class.
- Include when the SOW signals: "conversational BI", "enterprise chat", "ask anything over our data", "AI data assistant", "ChatGPT over our lakehouse", "agent over S3 lakehouse", "blended structured + unstructured Q&A".
- This partial is the **HERO DEMO** of the AI-Native Lakehouse kit. Pulls together everything built in Waves 1–2: lakehouse foundation (`DATA_ICEBERG_S3_TABLES`), catalog embeddings (`PATTERN_CATALOG_EMBEDDINGS`), multimodal index (`PATTERN_MULTIMODAL_EMBEDDINGS`), text-to-SQL (`PATTERN_TEXT_TO_SQL`), discovery (`PATTERN_SEMANTIC_DATA_DISCOVERY`), plus existing partials (`PATTERN_DOC_INGESTION_RAG`, `AGENTCORE_GATEWAY`, `AGENTCORE_MEMORY`, `AGENTCORE_BROWSER_TOOL`, `AGENTCORE_CODE_INTERPRETER`).

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the WebSocket API + supervisor Lambda + memory table + Cognito authoriser + all tool-function ARNs inline | **§3 Monolith Variant** |
| `ChatRouterStack` owns the API + supervisor + memory + session table; depends on SSM-published ARNs from `TextToSqlStack`, `DiscoveryStack`, `RagStack`, `MultimodalStack`, `AgentCoreStack` | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **The supervisor Lambda depends on 4-6 other Lambdas.** Any change to a downstream tool's signature breaks the router. Micro-stack gives each tool its own lifecycle; router reads ARNs via SSM and adapts to versioned interfaces.
2. **WebSocket APIs have two-way billing + connection state.** API GW WebSocket is a separate service from REST; keep it in the router stack where the connection-management logic lives.
3. **AgentCore Memory + Identity are account-level resources.** Memory stores live under `bedrock-agentcore:memory/...`; sharing across multiple agents (this chat router + the deep-research agent) requires careful namespace planning. Owner: `AgentCoreStack` (separate).
4. **Cognito + API GW + UI domain** live in `AuthStack` or `FrontendStack`. The chat router just consumes them.
5. **Session tokens can be large** (> 200 KB for multi-turn conversations). The session DDB table needs `S` attribute > 400 KB handling — use S3 offload for very long sessions.

Micro-Stack fixes by: (a) owning WebSocket API + supervisor Lambda + session DDB + router memory handle in `ChatRouterStack`; (b) reading `TextToSqlFnArn`, `DiscoveryFnArn`, `DocRagFnArn`, `MultimodalFnArn`, `MemoryArn`, `IdentityPoolArn` via SSM; (c) granting itself `lambda:InvokeFunction` + `bedrock-agentcore:*` on the specific ARNs.

---

## 3. Monolith Variant

**Use when:** POC with a well-scoped demo — one user cohort, one deployment.

### 3.1 Architecture

```
                  Browser UI  (streaming chat, citations, drill-downs)
                       │
                       │  wss://... (Cognito JWT on Connect)
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  API Gateway WebSocket API                                       │
  │    $connect   → AuthoriserFn (Cognito JWT validate)              │
  │    $default   → RouterSupervisorFn                               │
  │    $disconnect→ SessionCleanupFn                                 │
  │    tool_hook  → (internal — tools call back for streaming)       │
  └──────────────────────────────────────────────────────────────────┘
                       │
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  RouterSupervisorFn (Lambda — Strands Agents runtime)            │
  │                                                                  │
  │  Session lifecycle:                                              │
  │   1) Pull prior messages from AgentCore Memory for session_id    │
  │   2) Load session budget from DDB                                │
  │   3) Create Strands Agent with tool library + identity context   │
  │   4) Run agent.stream(user_message) → yields events              │
  │                                                                  │
  │  Each event from Strands:                                        │
  │   - 'thinking'     → not sent to UI (internal reasoning)         │
  │   - 'text_delta'   → postToConnection(token)                     │
  │   - 'tool_use'     → postToConnection({tool: 'text_to_sql'})     │
  │                    → invoke Lambda (via boto3 or tool wrapper)   │
  │   - 'tool_result'  → postToConnection({tool_result_summary})     │
  │   - 'complete'     → postToConnection({done: true})              │
  │                                                                  │
  │  OBO auth:                                                       │
  │   - AgentCore Identity mints a per-tool access token for the     │
  │     CALLER's identity (not the router's role).                   │
  │   - Text-to-SQL sees LF-enforced filters for the user.           │
  │                                                                  │
  │  Budget:                                                         │
  │   - Pre-tool-call budget check (DDB read).                       │
  │   - Post-response budget update (tokens + tool count).           │
  │                                                                  │
  │  Memory:                                                         │
  │   - On completion: summarise conversation + persist to           │
  │     AgentCore Memory for recall next turn.                       │
  └──────────────────────────────────────────────────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────────┐
  │Text-to-  │ │Discovery │ │Doc RAG     │ │Multimodal    │
  │SQL       │ │          │ │            │ │Search        │
  │Lambda    │ │Lambda    │ │Lambda      │ │Lambda        │
  └──────────┘ └──────────┘ └────────────┘ └──────────────┘
        │                                               │
        ▼                                               ▼
  (Athena, Catalog indexes)                 (Titan Multimodal, S3 Vectors)

  Optional extensions:
        │              │
        ▼              ▼
  ┌──────────┐ ┌──────────┐
  │Code      │ │Browser   │
  │Interp    │ │Tool      │
  │(AgentCore│ │(AgentCore│
  │)         │ │)         │
  └──────────┘ └──────────┘
```

### 3.2 CDK — `_create_chat_router()` method body

```python
from pathlib import Path
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_int,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
)
# WebSocket authorizer is in the `_alpha` package (churny — pin the
# version in requirements). If you want to avoid the alpha dep entirely,
# use `apigwv2.CfnAuthorizer` (L1, stable) as shown in the fallback at
# the end of this section.
from aws_cdk.aws_apigatewayv2_authorizers_alpha import WebSocketLambdaAuthorizer
from aws_cdk.aws_lambda_python_alpha import PythonFunction


def _create_chat_router(self, stage: str) -> None:
    """Monolith. Assumes self.{t2s_fn_arn, discovery_fn_arn, doc_rag_fn_arn,
    multimodal_fn_arn, memory_arn, identity_pool_arn, user_pool} exist."""

    # A) Session table — active WebSocket connections + conversation ids.
    self.session_table = ddb.Table(
        self, "ChatSessions",
        table_name=f"{{project_name}}-chat-sessions-{stage}",
        partition_key=ddb.Attribute(name="connection_id", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="ttl_epoch",
        encryption=ddb.TableEncryption.AWS_MANAGED,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Per-session budget table.
    self.budget_table = ddb.Table(
        self, "ChatBudget",
        table_name=f"{{project_name}}-chat-budget-{stage}",
        partition_key=ddb.Attribute(name="caller_id", type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name="date", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="ttl_epoch",
        encryption=ddb.TableEncryption.AWS_MANAGED,
    )

    # C) Authoriser Lambda — validates Cognito JWT on $connect.
    self.authoriser_fn = PythonFunction(
        self, "WsAuthoriser",
        entry=str(Path(__file__).parent.parent / "lambda" / "chat_ws_authoriser"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.seconds(10),
        memory_size=512,
        environment={
            "COGNITO_USER_POOL_ID": self.user_pool.user_pool_id,
            "COGNITO_CLIENT_ID":    self.user_pool_client.user_pool_client_id,
        },
    )

    # D) Connect / disconnect Lambdas (lightweight, just session management).
    self.connect_fn = PythonFunction(
        self, "WsConnect",
        entry=str(Path(__file__).parent.parent / "lambda" / "chat_ws_connect"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.seconds(10),
        memory_size=512,
        environment={"SESSION_TABLE": self.session_table.table_name},
    )
    self.disconnect_fn = PythonFunction(
        self, "WsDisconnect",
        entry=str(Path(__file__).parent.parent / "lambda" / "chat_ws_disconnect"),
        runtime=_lambda.Runtime.PYTHON_3_12,
        timeout=Duration.seconds(10),
        memory_size=512,
        environment={
            "SESSION_TABLE": self.session_table.table_name,
            "MEMORY_ARN":    self.memory_arn,
        },
    )
    self.session_table.grant_read_write_data(self.connect_fn)
    self.session_table.grant_read_write_data(self.disconnect_fn)

    # E) Supervisor Lambda — the heavy one. Docker image because Strands SDK
    #    + bedrock-runtime + bedrock-agentcore need a recent SDK stack.
    self.supervisor_fn = _lambda.DockerImageFunction(
        self, "RouterSupervisor",
        function_name=f"{{project_name}}-chat-supervisor-{stage}",
        code=_lambda.DockerImageCode.from_image_asset(
            directory=str(Path(__file__).parent.parent / "lambda_docker" / "chat_supervisor"),
            platform=_lambda.Platform.LINUX_AMD64,
        ),
        architecture=_lambda.Architecture.X86_64,
        memory_size=3072,
        timeout=Duration.minutes(5),
        reserved_concurrent_executions=50,
        environment={
            "SESSION_TABLE":     self.session_table.table_name,
            "BUDGET_TABLE":      self.budget_table.table_name,
            "MEMORY_ARN":        self.memory_arn,
            "IDENTITY_POOL_ARN": self.identity_pool_arn,
            "SUPERVISOR_MODEL":  "us.anthropic.claude-sonnet-4-7-20260109-v1:0",
            "DECISION_MODEL":    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "T2S_FN_ARN":        self.t2s_fn_arn,
            "DISCOVERY_FN_ARN":  self.discovery_fn_arn,
            "DOC_RAG_FN_ARN":    self.doc_rag_fn_arn,
            "MULTIMODAL_FN_ARN": self.multimodal_fn_arn,
            "SESSION_TOKEN_BUDGET": "200000",   # per-session cap
            "DAILY_TOKEN_BUDGET":   "5000000",  # per-caller-per-day cap
            "MAX_TOOL_CALLS_PER_TURN": "8",
        },
    )

    # F) Grants — supervisor needs to invoke each tool Lambda, talk to
    #    Bedrock, read/write session + budget tables, and use AgentCore
    #    Memory + Identity.
    for arn in (self.t2s_fn_arn, self.discovery_fn_arn,
                self.doc_rag_fn_arn, self.multimodal_fn_arn):
        self.supervisor_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[arn],
        ))
    self.supervisor_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                 "bedrock:Converse", "bedrock:ConverseStream"],
        resources=[
            f"arn:aws:bedrock:{Stack.of(self).region}:*:"
            f"inference-profile/us.anthropic.claude-sonnet-4-7-20260109-v1:0",
            f"arn:aws:bedrock:{Stack.of(self).region}:*:"
            f"inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
    ))
    self.supervisor_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:StartMemorySession",
                 "bedrock-agentcore:RetrieveMemory",
                 "bedrock-agentcore:SaveMemory",
                 "bedrock-agentcore:DeleteMemorySession"],
        resources=[self.memory_arn],
    ))
    self.supervisor_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock-agentcore:GetWorkloadAccessToken"],
        resources=[self.identity_pool_arn],
    ))
    self.supervisor_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["execute-api:ManageConnections"],
        resources=[f"arn:aws:execute-api:{Stack.of(self).region}:"
                   f"{Stack.of(self).account}:*/*"],
    ))
    self.session_table.grant_read_write_data(self.supervisor_fn)
    self.budget_table.grant_read_write_data(self.supervisor_fn)

    # G) WebSocket API.
    self.ws_api = apigwv2.WebSocketApi(
        self, "ChatWsApi",
        api_name=f"{{project_name}}-chat-ws-{stage}",
        connect_route_options=apigwv2.WebSocketRouteOptions(
            integration=apigwv2_int.WebSocketLambdaIntegration(
                "ConnectInt", self.connect_fn,
            ),
            authorizer=WebSocketLambdaAuthorizer(
                "WsAuth", self.authoriser_fn,
                identity_source=["route.request.querystring.token"],
            ),
        ),
        disconnect_route_options=apigwv2.WebSocketRouteOptions(
            integration=apigwv2_int.WebSocketLambdaIntegration(
                "DisconnectInt", self.disconnect_fn,
            ),
        ),
        default_route_options=apigwv2.WebSocketRouteOptions(
            integration=apigwv2_int.WebSocketLambdaIntegration(
                "DefaultInt", self.supervisor_fn,
            ),
        ),
    )
    self.ws_stage = apigwv2.WebSocketStage(
        self, "ChatWsStage",
        web_socket_api=self.ws_api,
        stage_name=stage,
        auto_deploy=True,
    )

    # Supervisor needs the WebSocket callback URL to post messages back.
    self.supervisor_fn.add_environment(
        "WS_CALLBACK_URL",
        f"https://{self.ws_api.api_id}.execute-api.{Stack.of(self).region}.amazonaws.com/{stage}",
    )

    # H) Outputs.
    CfnOutput(self, "ChatWsUrl",        value=self.ws_stage.url)
    CfnOutput(self, "SupervisorFnArn",  value=self.supervisor_fn.function_arn)
    CfnOutput(self, "SessionTable",     value=self.session_table.table_name)
```

### 3.3 Supervisor Lambda — Strands + Converse streaming

```python
# lambda_docker/chat_supervisor/handler.py
"""
The router supervisor. Wakes on a WebSocket message, loads conversation
memory, creates a Strands Agent with the four tools, runs the agent with
streaming, and posts back incremental events over the WS.

Event shape from API GW WebSocket:
{
  "requestContext": {
    "connectionId": "...",
    "routeKey":     "$default",
    "authorizer":   {"sub": "...", "custom:domain": "...", ...}
  },
  "body": '{"message": "revenue by region last quarter"}'
}
"""
import json
import os
import time
from typing import Any

import boto3

# Strands Agents SDK — Python — see kits/deep-research-agent for deep usage
from strands_agents import Agent
from strands_agents.tools import lambda_tool

SESSION_TABLE       = os.environ["SESSION_TABLE"]
BUDGET_TABLE        = os.environ["BUDGET_TABLE"]
MEMORY_ARN          = os.environ["MEMORY_ARN"]
IDENTITY_POOL_ARN   = os.environ["IDENTITY_POOL_ARN"]
SUPERVISOR_MODEL    = os.environ["SUPERVISOR_MODEL"]
T2S_FN_ARN          = os.environ["T2S_FN_ARN"]
DISCOVERY_FN_ARN    = os.environ["DISCOVERY_FN_ARN"]
DOC_RAG_FN_ARN      = os.environ["DOC_RAG_FN_ARN"]
MULTIMODAL_FN_ARN   = os.environ["MULTIMODAL_FN_ARN"]
WS_CALLBACK_URL     = os.environ["WS_CALLBACK_URL"]
SESSION_TOKEN_BUDGET = int(os.environ["SESSION_TOKEN_BUDGET"])

ddb      = boto3.client("dynamodb")
ac_id    = boto3.client("bedrock-agentcore")
ac_mem   = boto3.client("bedrock-agentcore")
lam      = boto3.client("lambda")
apigw_mgmt = boto3.client("apigatewaymanagementapi", endpoint_url=WS_CALLBACK_URL)


# ---- WS helpers -----------------------------------------------------------

def _post(conn_id: str, payload: dict) -> None:
    try:
        apigw_mgmt.post_to_connection(ConnectionId=conn_id, Data=json.dumps(payload).encode())
    except apigw_mgmt.exceptions.GoneException:
        # Client disconnected mid-stream; swallow.
        pass


# ---- Session + identity ---------------------------------------------------

def _load_session(connection_id: str) -> dict:
    resp = ddb.get_item(TableName=SESSION_TABLE,
                        Key={"connection_id": {"S": connection_id}})
    item = resp.get("Item", {})
    return {
        "connection_id": connection_id,
        "caller_id":     item.get("caller_id", {}).get("S", "anon"),
        "caller_domain": item.get("caller_domain", {}).get("S", "default"),
        "max_sensitivity": item.get("max_sensitivity", {}).get("S", "internal"),
        "session_id":    item.get("session_id", {}).get("S", connection_id),
        "access_groups": json.loads(item.get("access_groups", {}).get("S", "[]")),
    }


def _obo_token(session: dict) -> str:
    """Get an access token for the CALLER identity — this is what makes
    LF enforcement work end-to-end. The supervisor's own role does NOT
    get propagated to the tools."""
    resp = ac_id.get_workload_access_token(
        workloadIdentityArn=IDENTITY_POOL_ARN,
        subject=session["caller_id"],
        attributes={
            "domain":          session["caller_domain"],
            "max_sensitivity": session["max_sensitivity"],
            "access_groups":   ",".join(session["access_groups"]),
        },
    )
    return resp["accessToken"]


# ---- Tool wrappers --------------------------------------------------------

def _make_text_to_sql_tool(session: dict):
    @lambda_tool(
        name="text_to_sql",
        description=(
            "Generate and run a SELECT query over the lakehouse. Use when "
            "the question asks for aggregates, counts, totals, averages, "
            "specific rows, or any structured-data answer. Input: a "
            "natural-language question. Output: rows + SQL + lineage."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question":    {"type": "string"},
                "max_sensitivity": {
                    "type": "string",
                    "enum": ["public","internal","confidential","pii"],
                    "default": session["max_sensitivity"],
                },
            },
            "required": ["question"],
        },
    )
    def run(question: str, max_sensitivity: str | None = None) -> dict:
        resp = lam.invoke(
            FunctionName=T2S_FN_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "question":        question,
                "caller_id":       session["caller_id"],
                "caller_domain":   session["caller_domain"],
                "max_sensitivity": max_sensitivity or session["max_sensitivity"],
            }).encode(),
        )
        return json.loads(resp["Payload"].read())
    return run


def _make_discovery_tool(session: dict):
    @lambda_tool(
        name="semantic_discovery",
        description=(
            "Find what data exists about a topic. Use when the user asks "
            "'where is X data?' or when you are unsure which table to query. "
            "Input: topic. Output: databases, tables, columns, images."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question":          {"type": "string"},
                "include_multimodal": {"type": "boolean", "default": False},
                "top_k_tables":      {"type": "integer", "default": 8},
            },
            "required": ["question"],
        },
    )
    def run(question: str, include_multimodal: bool = False, top_k_tables: int = 8) -> dict:
        # Mimic the API GW event shape so the same Lambda can be called
        # both from WS router and from a direct HTTP client.
        resp = lam.invoke(
            FunctionName=DISCOVERY_FN_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "requestContext": {"authorizer": {"claims": {
                    "sub":          session["caller_id"].split(":")[-1],
                    "custom:domain": session["caller_domain"],
                    "custom:max_sensitivity": session["max_sensitivity"],
                }}},
                "body": json.dumps({
                    "question":           question,
                    "top_k_tables":       top_k_tables,
                    "sample_values":      False,
                    "include_multimodal": include_multimodal,
                    "include_summary":    False,
                }),
            }).encode(),
        )
        body = json.loads(resp["Payload"].read()).get("body", "{}")
        return json.loads(body)
    return run


def _make_doc_rag_tool(session: dict):
    @lambda_tool(
        name="doc_rag",
        description=(
            "Retrieve relevant passages from internal documents (contracts, "
            "policies, SOPs, customer notes). Use when the question refers "
            "to written content rather than structured data. Input: question. "
            "Output: chunks with citations (doc_id, page, source)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "top_k":    {"type": "integer", "default": 5},
            },
            "required": ["question"],
        },
    )
    def run(question: str, top_k: int = 5) -> dict:
        resp = lam.invoke(
            FunctionName=DOC_RAG_FN_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "question":      question,
                "caller_id":     session["caller_id"],
                "access_groups": session["access_groups"],
                "top_k":         top_k,
            }).encode(),
        )
        return json.loads(resp["Payload"].read())
    return run


def _make_multimodal_tool(session: dict):
    @lambda_tool(
        name="multimodal_search",
        description=(
            "Search for images, diagrams, or PDF pages by visual or textual "
            "similarity. Use when the question asks to 'show' or 'find a "
            "diagram/figure/chart'. Input: question or text descriptor. "
            "Output: images with thumbnail URLs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text":       {"type": "string"},
                "modalities": {"type": "array", "items": {"type": "string"},
                               "default": ["image"]},
                "top_k":      {"type": "integer", "default": 5},
            },
            "required": ["text"],
        },
    )
    def run(text: str, modalities: list | None = None, top_k: int = 5) -> dict:
        resp = lam.invoke(
            FunctionName=MULTIMODAL_FN_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "text":       text,
                "modalities": modalities or ["image"],
                "top_k":      top_k,
                "filter":     {
                    "access_group": {"$in": session["access_groups"] + ["default"]},
                },
            }).encode(),
        )
        return json.loads(resp["Payload"].read())
    return run


# ---- Supervisor prompt ----------------------------------------------------

_SUPERVISOR_SYSTEM = """\
You are an enterprise data assistant with access to a lakehouse, internal
documents, and multimodal content. You have four tools:

- text_to_sql       : structured-data questions (aggregates, totals, rows).
- semantic_discovery: "where is X data?" / unsure which table to query.
- doc_rag           : written content in documents (contracts, policies).
- multimodal_search : images, diagrams, charts, PDF figures.

ROUTING RULES:
1. If the question mixes both structured and unstructured ("revenue for
   customers with open complaints"), call text_to_sql AND doc_rag IN PARALLEL.
2. Use semantic_discovery FIRST only if you don't know which tables to hit.
3. Use multimodal_search when the question asks to "show", "find a
   diagram", or refers to a figure/chart.
4. At most 5 tool calls per turn. Prefer fewer.

CITATION RULES:
- Every factual claim MUST carry an inline citation: [SQL-N], [DOC-N], [IMG-N].
- After your answer, produce a "Sources" section with the full link:
    [SQL-1] <SQL snippet>  scanned=<X bytes>
    [DOC-1] <doc_id>#page=<n>
    [IMG-1] <thumbnail URL>
- If a claim has no source, do not make the claim.

STYLE:
- Be concise. Two paragraphs max for the narrative; tables welcome.
- Surface tradeoffs briefly, don't lecture.
- If a tool returns no usable results, say so explicitly.

CALLER CONTEXT:
- caller_id: {caller_id}
- domain:    {caller_domain}
- max_sensitivity: {max_sensitivity}
- access_groups: {access_groups}

CONVERSATION SO FAR (summary):
{memory_summary}
"""


# ---- Main handler ---------------------------------------------------------

def lambda_handler(event, _ctx):
    conn_id = event["requestContext"]["connectionId"]
    body = json.loads(event.get("body") or "{}")
    user_message = body.get("message", "").strip()
    if not user_message:
        _post(conn_id, {"type": "error", "message": "empty message"})
        return {"statusCode": 400}

    session = _load_session(conn_id)

    # 1) Budget check (elided — same pattern as text-to-sql).

    # 2) Memory recall — load prior-turn summary from AgentCore Memory.
    try:
        mem = ac_mem.retrieve_memory(
            memoryArn=MEMORY_ARN,
            sessionId=session["session_id"],
            maxResults=20,
        )
        mem_summary = "\n".join(m.get("content", "") for m in mem.get("memories", []))
    except Exception:
        mem_summary = ""

    # 3) Build agent + tools.
    tools = [
        _make_text_to_sql_tool(session),
        _make_discovery_tool(session),
        _make_doc_rag_tool(session),
        _make_multimodal_tool(session),
    ]
    sys_prompt = _SUPERVISOR_SYSTEM.format(
        caller_id=session["caller_id"],
        caller_domain=session["caller_domain"],
        max_sensitivity=session["max_sensitivity"],
        access_groups=session["access_groups"],
        memory_summary=mem_summary[:4000],
    )
    agent = Agent(
        model=SUPERVISOR_MODEL,
        system_prompt=sys_prompt,
        tools=tools,
        max_tool_calls=int(os.environ["MAX_TOOL_CALLS_PER_TURN"]),
    )

    # 4) Stream the agent's response over the WebSocket.
    _post(conn_id, {"type": "start", "session_id": session["session_id"]})
    full_text_parts: list[str] = []
    tool_events: list[dict] = []
    try:
        for event_delta in agent.stream(user_message):
            et = event_delta.get("type")
            if et == "text":
                _post(conn_id, {"type": "text", "delta": event_delta["text"]})
                full_text_parts.append(event_delta["text"])
            elif et == "tool_use":
                _post(conn_id, {
                    "type": "tool_use",
                    "tool": event_delta["name"],
                    "input": event_delta["input"],
                })
                tool_events.append(event_delta)
            elif et == "tool_result":
                _post(conn_id, {
                    "type": "tool_result",
                    "tool": event_delta["name"],
                    "summary": event_delta.get("summary", ""),
                })
            elif et == "error":
                _post(conn_id, {"type": "error", "message": event_delta.get("message", "")})
    except Exception as e:
        _post(conn_id, {"type": "error", "message": str(e)})
        return {"statusCode": 500}

    _post(conn_id, {"type": "end"})

    # 5) Persist turn to AgentCore Memory.
    final_text = "".join(full_text_parts)
    try:
        ac_mem.save_memory(
            memoryArn=MEMORY_ARN,
            sessionId=session["session_id"],
            memories=[
                {"content": f"User: {user_message}", "role": "user"},
                {"content": f"Assistant: {final_text}", "role": "assistant"},
            ],
        )
    except Exception:
        pass    # memory is best-effort

    # 6) Update budget (elided).

    return {"statusCode": 200}
```

### 3.4 Dockerfile for supervisor

```dockerfile
# lambda_docker/chat_supervisor/Dockerfile
FROM public.ecr.aws/lambda/python:3.12

RUN pip install --no-cache-dir \
    "boto3>=1.34" \
    "strands-agents>=1.0" \
    "pydantic>=2.7"

COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
```

### 3.5 UI WebSocket client (reference)

```javascript
// ui/src/chat.ts — minimal reference.
const token = await getCognitoIdToken();
const ws = new WebSocket(`${WS_URL}?token=${token}`);

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  switch (msg.type) {
    case 'start':       setStatus('thinking...'); break;
    case 'text':        appendToBubble(msg.delta); break;
    case 'tool_use':    setStatus(`running ${msg.tool}...`); break;
    case 'tool_result': showToolCard(msg); break;
    case 'end':         setStatus('ready'); break;
    case 'error':       showError(msg.message); break;
  }
};

function send(msg: string) {
  ws.send(JSON.stringify({ message: msg }));
}
```

### 3.6 Monolith gotchas

1. **AgentCore SDK is alpha.** Exact method names + ARN shapes are in flux as of v2.238. The `bedrock-agentcore:GetWorkloadAccessToken` call above is representative; consult the AgentCore devguide before shipping. If unavailable at deploy time, fall back to passing `caller_id` as a plain string — LF enforcement at the tool Lambda degrades to whatever the tool's IAM role allows (dangerous; only acceptable if the tool Lambdas themselves re-authenticate).
2. **API Gateway WebSocket `postToConnection` requires same-region endpoint.** Do not hard-code region; derive from the event.
3. **WebSocket billing is PER MESSAGE + minute.** Streaming 1000 tokens as 1000 `postToConnection` calls = 1000 messages. Batch tokens into ~20-token chunks on the client side if cost matters.
4. **Lambda timeout is 5 min** for the supervisor, which caps the entire turn. For complex multi-tool turns (> 4 tools), this is tight — break up into smaller turns via a planner agent.
5. **Strands SDK `stream()` yields synchronously.** If a tool call inside the stream takes 30 s, the generator blocks. Use Strands' async stream variant if available, or set per-tool timeouts.
6. **Memory recall can exceed token budget.** `retrieve_memory` with 20 results can be 10k+ tokens. Cap summary at 4000 chars; let AgentCore's own memory summarisation handle the rest.
7. **OBO tokens expire at ~15 min.** For long turns, refresh before each tool call.
8. **Session tables grow unbounded** if TTL isn't aggressive. Default: 1 hour from last-message TTL, refreshed on each turn.
9. **Tool results in the supervisor's context window.** A tool that returns 10 MB of SQL results WILL blow the context. Each tool wrapper must TRUNCATE / SUMMARISE its output before returning to Strands. Text-to-SQL: return top 100 rows + total count. Doc RAG: return top 5 chunks, 500 chars each.
10. **Parallel tool calls are the norm**, not the exception. Strands supports parallel tool execution by default; ensure each tool Lambda is reentrant and the supervisor is aware that 3-4 tools may fire simultaneously (budget + rate-limit accordingly).

---

## 4. Micro-Stack Variant

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** on supervisor Docker asset + connect/disconnect Lambda entries.
2. **Identity-side grants** — the supervisor role grants itself `lambda:InvokeFunction` on SSM-read tool ARNs; no cross-stack role mutations.
3. **`CfnRule` cross-stack EventBridge** — N/A for chat router (WS-driven).
4. **Same-stack bucket + OAC** — the UI bucket + CloudFront distribution live in `FrontendStack`, not this router stack. Chat router emits the WebSocket URL via SSM for the UI to consume.
5. **KMS ARNs as strings** — tool Lambdas' CMK ARNs (if any) flow through as env vars or SSM lookups; the supervisor does not re-encrypt.

### 4.2 ChatRouterStack — the consumer of Waves 1–3

```python
# stacks/chat_router_stack.py  (abbreviated — same shape as §3.2 with SSM
# inputs; omitted here to save space. Key difference:)
from aws_cdk import Stack, aws_ssm as ssm
from constructs import Construct


class ChatRouterStack(Stack):
    def __init__(self, scope, construct_id, *, stage: str, **kw) -> None:
        super().__init__(scope, construct_id, **kw)

        # Resolve EVERY tool ARN + AgentCore + auth via SSM.
        t2s_arn       = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/text_to_sql/fn_arn")
        disc_arn      = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/discovery/fn_arn")
        doc_rag_arn   = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/doc_rag/fn_arn")
        multimodal_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/multimodal/query_fn_arn")
        memory_arn    = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/agentcore/memory_arn")
        identity_pool = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/agentcore/identity_pool_arn")
        user_pool_id  = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/auth/user_pool_id")

        # ... session table, budget table, Docker supervisor, WS API
        # (same shape as §3.2, all grants reference the SSM-resolved ARNs)

        # Publish the WS URL back out so the UI stack can grab it.
        # (Omitted — ssm.StringParameter publishing pattern identical.)
```

### 4.3 Micro-stack gotchas

- **Deletion order is critical**: all upstream stacks (TextToSql, Discovery, DocRag, Multimodal, AgentCore) must be alive while ChatRouterStack exists. A surprise `cdk destroy` on any upstream stack leaves the router with dangling SSM refs and the next user turn fails.
- **Cross-stack version drift**: upstream tools change their Lambda input schema; the supervisor's tool wrappers may silently pass outdated shapes. Solution: integration tests in CI that round-trip each tool through the supervisor.
- **OBO token caching**: cache per-session OBO tokens in the session DDB row for 10 min to avoid re-minting on every turn.
- **UI CORS origins** must include the CloudFront distribution URL of the Frontend stack; hard-code in the router stack's WS API CORS config.

---

## 5. Swap matrix

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Agent runtime | Strands Agents SDK | LangChain | Python ecosystem momentum; but Strands is AWS-first and has better AgentCore integration. Prefer Strands on AWS. |
| Agent runtime | Strands Agents SDK | Bedrock Agents (native) | No runtime code; but less flexible (fixed action groups, no custom loops). Use for simple agents with < 3 tools. |
| Supervisor model | Claude Sonnet 4.7 | Claude Opus 4.7 | Hardest multi-tool planning; 5× cost. Gate behind a complexity classifier. |
| Supervisor model | Claude Sonnet 4.7 | Claude Haiku 4.5 | Simple, single-tool turns; 90% cost savings. Router degrades on multi-tool. |
| Streaming | WebSocket API | Server-Sent Events (SSE) over HTTPS | One-way streaming only; simpler infra. Loses interactive typing indicators from server. |
| Streaming | WebSocket API | Polling via REST `/status/{job_id}` | Poll interval adds latency; acceptable for batch UIs. |
| Memory | AgentCore Memory | DDB conversation table + in-prompt summary | Simpler; loses semantic recall. Use for single-session-only. |
| Memory | AgentCore Memory | Zep / Mem0 via Lambda connector | Open-source long-term memory; adds ops overhead. |
| Identity propagation | AgentCore Identity OBO | Shared execution role + `caller_id` parameter | Acceptable ONLY if all tool Lambdas re-authenticate. Without AgentCore Identity, you lose LF enforcement for tool calls. |
| Parallel tool calls | Strands default | Forced-serial via supervisor prompt | Debuggability; 2-3× slower responses. |
| Citation format | `[SQL-1]` inline + Sources block | Footnote style `¹` with `<sup>` HTML | UI preference; functionally identical. |
| Session store | DDB | ElastiCache Redis | Sub-ms latency; stateful-connection-affinity concerns in Lambda. |
| Tool chain | 4 tools (SQL+discovery+RAG+MM) | Plus `compute` (Code Interpreter) | Quant analysis ("compute YoY growth") over returned rows. See `AGENTCORE_CODE_INTERPRETER`. |
| Tool chain | 4 tools | Plus `web_search` (Browser Tool) | External enrichment ("is this company public?"). See `AGENTCORE_BROWSER_TOOL`. |
| Testing | Golden-set 30 questions | LLM-as-judge automated eval | Faster iteration; subjective drift over time. |

---

## 6. Worked example — offline synth + scripted session

```python
# tests/test_chat_router_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.chat_router_stack import ChatRouterStack


def test_synth_ws_api_supervisor_tools_wired():
    app = cdk.App()
    stack = ChatRouterStack(app, "ChatRouter-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # WS API with $connect / $disconnect / $default routes.
    tpl.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    tpl.has_resource_properties("AWS::ApiGatewayV2::Route", {
        "RouteKey": "$default",
    })
    tpl.has_resource_properties("AWS::ApiGatewayV2::Route", {
        "RouteKey": "$connect",
        "AuthorizationType": "CUSTOM",
    })

    # Supervisor is a Docker image Lambda, 3 GB + 5 min.
    tpl.has_resource_properties("AWS::Lambda::Function", {
        "PackageType": "Image",
        "MemorySize":  3072,
        "Timeout":     300,
    })

    # 2 DDB tables (sessions + budget).
    tpl.resource_count_is("AWS::DynamoDB::Table", 2)

    # Supervisor IAM allows invoking all 4 tools + AgentCore + Bedrock.
    tpl.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": ["lambda:InvokeFunction"],
                    "Effect": "Allow",
                }),
                Match.object_like({
                    "Action": Match.array_with([
                        "bedrock:InvokeModel",
                        "bedrock:ConverseStream",
                    ]),
                }),
            ]),
        }),
    })


# tests/test_golden_questions.py
"""Golden-set — 30 questions, expected tool routing + citations.
Runs as part of CI nightly. Failure = routing regression."""
import asyncio, json, os, pytest, websockets


GOLDEN = [
    {
        "q":        "total revenue by region last quarter",
        "tools":    ["text_to_sql"],
        "must_cite": ["[SQL-1]"],
    },
    {
        "q":        "what does our contract with Acme say about SLA breach penalties",
        "tools":    ["doc_rag"],
        "must_cite": ["[DOC-1]"],
    },
    {
        "q":        "show me a wiring diagram for the XJ-550 pump",
        "tools":    ["multimodal_search"],
        "must_cite": ["[IMG-1]"],
    },
    {
        "q":        "revenue for customers whose contracts mention renewal bonuses",
        "tools":    ["text_to_sql", "doc_rag"],
        "must_cite": ["[SQL-1]", "[DOC-1]"],
    },
    # ... 26 more ...
]


@pytest.mark.integration
@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["q"][:40])
async def test_golden_question(case):
    url = os.environ["CHAT_WS_URL"]
    token = os.environ["TEST_ID_TOKEN"]
    async with websockets.connect(f"{url}?token={token}") as ws:
        await ws.send(json.dumps({"message": case["q"]}))
        tools_fired: list[str] = []
        full_text = ""
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "tool_use":
                tools_fired.append(msg["tool"])
            elif msg["type"] == "text":
                full_text += msg["delta"]
            elif msg["type"] == "end":
                break
        # Exact tool set — avoids false positives from "extra helpful" calls.
        assert set(tools_fired) == set(case["tools"]), tools_fired
        for c in case["must_cite"]:
            assert c in full_text, f"{c} missing from: {full_text[:200]}"
```

---

## 7. References

- AWS docs — *Strands Agents SDK* (Python, tool decorators, stream API).
- AWS docs — *Bedrock Converse / ConverseStream API* (multi-tool).
- AWS docs — *AgentCore Memory* (session-scoped memory resources).
- AWS docs — *AgentCore Identity* (workload identity pools, OBO tokens).
- AWS docs — *API Gateway WebSocket API* (routes, authoriser, postToConnection).
- `PATTERN_TEXT_TO_SQL.md` — the SQL tool's backing service.
- `PATTERN_SEMANTIC_DATA_DISCOVERY.md` — the discovery tool.
- `PATTERN_DOC_INGESTION_RAG.md` — the RAG tool (from Wave 2 of rag-chatbot kit).
- `PATTERN_MULTIMODAL_EMBEDDINGS.md` — the multimodal tool.
- `AGENTCORE_MEMORY.md` — memory resource construct.
- `AGENTCORE_GATEWAY.md` — alternative tool-hosting framework (MCP-like).
- `AGENTCORE_CODE_INTERPRETER.md` — optional `compute` tool.
- `AGENTCORE_BROWSER_TOOL.md` — optional `web_search` tool.
- `mlops/20_strands_agent_lambda_deployment.md` — Strands deployment pattern.
- `mlops/21_strands_multi_agent_patterns.md` — supervisor + sub-agent patterns.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Strands supervisor with 4 standard tools (text_to_sql, semantic_discovery, doc_rag, multimodal_search) + 2 optional extensions (code_interpreter, browser_tool). API GW WebSocket streaming. AgentCore Memory for sessions, Identity for OBO. Golden-set test harness with 30 questions. Inline citations + Sources block. 10 monolith gotchas, 4 micro-stack gotchas, 14-row swap matrix, pytest synth + WS roundtrip + golden-set integration harness.
