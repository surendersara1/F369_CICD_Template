"""
AgentCore Deployment Wrapper — RAG Research Agent
Wraps the Strands agent for managed AgentCore hosting.

Deploy with:
  agentcore configure
  agentcore dev        (local testing)
  agentcore launch     (deploy to AgentCore)
"""
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
import boto3
import os
import json
import time

app = BedrockAgentCoreApp()

_token_cache = {"token": None, "expires_at": 0}


def _get_oauth2_token() -> str:
    """Get OAuth2 token with caching for Gateway authentication."""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    sm_client = boto3.client("secretsmanager")
    secret = json.loads(
        sm_client.get_secret_value(SecretId=os.environ["GATEWAY_SECRET_ARN"])["SecretString"]
    )

    import requests
    resp = requests.post(secret["token_endpoint"], data={
        "grant_type": "client_credentials",
        "client_id": secret["client_id"],
        "client_secret": secret["client_secret"],
        "scope": secret["scope"],
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp.raise_for_status()
    token_data = resp.json()

    _token_cache["token"] = token_data["access_token"]
    _token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _token_cache["token"]


def get_gateway_mcp_client(gateway_url: str) -> MCPClient:
    """Create MCP client connected to AgentCore Gateway."""
    token = _get_oauth2_token()
    return MCPClient(lambda: streamablehttp_client(
        url=gateway_url, headers={"Authorization": f"Bearer {token}"}))


def create_agent_with_memory(session_id: str, actor_id: str):
    """Create agent with AgentCore Memory (STM + LTM)."""
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    memory_config = AgentCoreMemoryConfig(
        memory_id=os.environ.get("MEMORY_ID", ""),
        memory_strategies=["SUMMARY", "USER_PREFERENCE", "SEMANTIC"],
    )
    session_manager = AgentCoreMemorySessionManager(
        memory_config=memory_config, session_id=session_id, actor_id=actor_id)

    model = BedrockModel(
        model_id=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"))

    tools = []
    gateway_url = os.environ.get("GATEWAY_MCP_URL")
    if gateway_url:
        tools.append(get_gateway_mcp_client(gateway_url))

    # Import local tools
    from index import (search_knowledge_base, web_search, save_research_report,
                       summarize_document, compare_documents, extract_entities, cite_sources)
    tools.extend([search_knowledge_base, web_search, save_research_report,
                  summarize_document, compare_documents, extract_entities, cite_sources])

    agent = Agent(
        model=model,
        system_prompt="You are a RAG Research Agent with memory capabilities.",
        tools=tools,
    )
    return agent, session_manager


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore managed entrypoint."""
    user_message = payload.get("message", "")
    session_id = payload.get("session_id", "default")
    actor_id = payload.get("actor_id", "anonymous")

    agent, session_manager = create_agent_with_memory(session_id, actor_id)
    session_manager.start_session(agent)
    response = agent(user_message)
    session_manager.end_session(agent)

    return {"session_id": session_id, "response": str(response)}


if __name__ == "__main__":
    app.run()
