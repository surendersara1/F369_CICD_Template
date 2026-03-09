"""
RAG Research Agent — Strands SDK Agent with Custom Tools
Powered by Claude Sonnet 4.5 via Amazon Bedrock
"""
from strands import Agent, tool
from strands.models import BedrockModel
import boto3
import os
import json
import time


# =============================================================================
# CUSTOM TOOLS
# =============================================================================

@tool
def search_knowledge_base(query: str, max_results: int = 5) -> str:
    """Search the knowledge base for relevant documents and information.

    Args:
        query: The search query to find relevant documents.
        max_results: Maximum number of results to return (default 5).

    Returns:
        Formatted search results with source citations.
    """
    client = boto3.client("bedrock-agent-runtime")
    response = client.retrieve(
        knowledgeBaseId=os.environ.get("KNOWLEDGE_BASE_ID", ""),
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": max_results}
        },
    )
    results = []
    for i, r in enumerate(response.get("retrievalResults", []), 1):
        text = r["content"]["text"]
        source = r.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        score = r.get("score", 0)
        results.append(f"[Source {i}] (score: {score:.2f}) {source}\n{text}")
    return "\n---\n".join(results) if results else "No results found in knowledge base."


@tool
def web_search(query: str) -> str:
    """Search the internet for current information not in the knowledge base.

    Args:
        query: The search query for web search.

    Returns:
        Web search results summary.
    """
    # Uses Tavily API if configured, otherwise returns guidance
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return f"Web search not configured. Query was: {query}. Please configure TAVILY_API_KEY."
    import requests
    resp = requests.post("https://api.tavily.com/search", json={
        "api_key": api_key, "query": query, "max_results": 5,
    }, timeout=15)
    if resp.status_code != 200:
        return f"Web search failed with status {resp.status_code}"
    data = resp.json()
    results = []
    for r in data.get("results", []):
        results.append(f"[{r['title']}]({r['url']})\n{r['content'][:300]}")
    return "\n---\n".join(results) if results else "No web results found."


@tool
def save_research_report(filename: str, content: str, format: str = "markdown") -> str:
    """Save a structured research report to S3 as a persistent artifact.

    Args:
        filename: Name of the report file (without extension).
        content: The full report content.
        format: Output format — 'markdown' or 'text' (default: markdown).

    Returns:
        S3 URI of the saved report.
    """
    s3 = boto3.client("s3")
    bucket = os.environ["AGENT_ARTIFACTS_BUCKET"]
    ext = "md" if format == "markdown" else "txt"
    key = f"reports/{time.strftime('%Y/%m/%d')}/{filename}.{ext}"
    s3.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"),
                  ContentType="text/markdown" if format == "markdown" else "text/plain")
    return f"s3://{bucket}/{key}"


@tool
def summarize_document(document_text: str, max_length: int = 500) -> str:
    """Summarize a document or long text into a concise executive summary.

    Args:
        document_text: The full text to summarize.
        max_length: Target maximum length in words for the summary.

    Returns:
        A concise summary of the document.
    """
    bedrock = boto3.client("bedrock-runtime")
    response = bedrock.invoke_model(
        modelId=os.environ.get("FAST_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_length * 2,
            "messages": [{"role": "user",
                "content": f"Summarize the following in under {max_length} words:\n\n{document_text[:10000]}"}],
        }),
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


@tool
def compare_documents(doc_a: str, doc_b: str) -> str:
    """Compare two documents side-by-side, highlighting similarities and differences.

    Args:
        doc_a: Text content of the first document.
        doc_b: Text content of the second document.

    Returns:
        A structured comparison of the two documents.
    """
    bedrock = boto3.client("bedrock-runtime")
    response = bedrock.invoke_model(
        modelId=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2000,
            "messages": [{"role": "user",
                "content": f"Compare these two documents. List key similarities and differences:\n\n"
                           f"DOCUMENT A:\n{doc_a[:5000]}\n\nDOCUMENT B:\n{doc_b[:5000]}"}],
        }),
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


@tool
def extract_entities(text: str) -> str:
    """Extract named entities from text: people, organizations, dates, amounts, locations.

    Args:
        text: The text to extract entities from.

    Returns:
        JSON-formatted list of extracted entities with types.
    """
    bedrock = boto3.client("bedrock-runtime")
    response = bedrock.invoke_model(
        modelId=os.environ.get("FAST_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [{"role": "user",
                "content": f"Extract all named entities from this text. Return as JSON array with "
                           f"'entity', 'type' (PERSON/ORG/DATE/AMOUNT/LOCATION), and 'context' fields:\n\n{text[:5000]}"}],
        }),
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


@tool
def cite_sources(claim: str, sources: str) -> str:
    """Format proper citations for a claim based on retrieved source documents.

    Args:
        claim: The claim or statement that needs citation.
        sources: The source documents text to cite from.

    Returns:
        The claim with properly formatted citations and references.
    """
    bedrock = boto3.client("bedrock-runtime")
    response = bedrock.invoke_model(
        modelId=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [{"role": "user",
                "content": f"Format this claim with proper citations from the sources provided.\n\n"
                           f"CLAIM: {claim}\n\nSOURCES:\n{sources[:5000]}\n\n"
                           f"Return the claim with inline citations [1], [2] etc. and a References section."}],
        }),
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


# =============================================================================
# AGENT DEFINITION
# =============================================================================

SYSTEM_PROMPT = """You are a RAG Research Agent — an expert AI research assistant.

You help users research topics by searching a knowledge base of uploaded documents,
searching the web for current information, and synthesizing findings into clear,
well-cited reports.

Rules:
- ALWAYS cite sources when retrieving information from the knowledge base
- NEVER fabricate information — if you don't know, say so
- Ask for clarification when the request is ambiguous
- Use the knowledge base first, then web search for current information
- When generating reports, save them as artifacts for future reference
- Extract entities and compare documents when asked for analysis
- Format citations with document names and page numbers when available
"""


def create_agent(session_id: str = None):
    """Create a configured Strands research agent."""
    model = BedrockModel(
        model_id=os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            search_knowledge_base,
            web_search,
            save_research_report,
            summarize_document,
            compare_documents,
            extract_entities,
            cite_sources,
        ],
    )
    return agent


# =============================================================================
# LAMBDA HANDLER
# =============================================================================

def handler(event, context):
    """Lambda handler for Strands agent invocation."""
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    user_message = body.get("message", "")
    session_id = body.get("session_id", context.aws_request_id)
    actor_id = body.get("actor_id", "anonymous")

    if not user_message:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "Empty message"}),
        }

    agent = create_agent(session_id=session_id)
    response = agent(user_message)

    # Save session turn
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(os.environ["SESSION_TABLE"])
    table.put_item(Item={
        "session_id": session_id,
        "turn_id": int(time.time() * 1000),
        "actor_id": actor_id,
        "user_message": user_message,
        "agent_response": str(response),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": int(time.time()) + (24 * 3600),
    })

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"session_id": session_id, "response": str(response)}),
    }
