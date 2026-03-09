"""
Multi-Agent Supervisor Pattern — RAG Research Agent
Supervisor routes complex queries to specialist worker agents.
"""
from strands import Agent, tool
from strands.models import BedrockModel
import os

# =============================================================================
# WORKER AGENTS
# =============================================================================

# Import tools from main agent
from index import search_knowledge_base, web_search, summarize_document, cite_sources

_model_id = os.environ.get("DEFAULT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")
_fast_model_id = os.environ.get("FAST_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

deep_research_agent = Agent(
    model=BedrockModel(model_id=_model_id),
    system_prompt="""You are a Deep Research specialist. Perform multi-hop retrieval
across the knowledge base to answer complex questions that require synthesizing
information from multiple documents. Always cite every source.""",
    tools=[search_knowledge_base, web_search, cite_sources],
)

summarization_agent = Agent(
    model=BedrockModel(model_id=_fast_model_id),
    system_prompt="""You are a Summarization specialist. Condense long documents
and multi-source findings into clear, concise executive summaries. Preserve key
facts and figures. Keep summaries under 500 words unless asked otherwise.""",
    tools=[summarize_document],
)

fact_check_agent = Agent(
    model=BedrockModel(model_id=_model_id),
    system_prompt="""You are a Fact-Check specialist. Cross-reference claims against
the knowledge base. Flag any contradictions, unsupported claims, or inconsistencies.
Rate confidence as HIGH, MEDIUM, or LOW for each claim.""",
    tools=[search_knowledge_base, cite_sources],
)


# =============================================================================
# SUPERVISOR TOOLS (wrap workers as tools)
# =============================================================================

@tool
def ask_deep_research(question: str) -> str:
    """Delegate a complex research question to the Deep Research specialist.

    Args:
        question: The research question requiring multi-hop retrieval.

    Returns:
        Detailed research findings with citations.
    """
    return str(deep_research_agent(question))


@tool
def ask_summarizer(text: str) -> str:
    """Delegate text to the Summarization specialist for condensing.

    Args:
        text: The text or findings to summarize.

    Returns:
        Concise executive summary.
    """
    return str(summarization_agent(text))


@tool
def ask_fact_checker(claims: str) -> str:
    """Delegate claims to the Fact-Check specialist for verification.

    Args:
        claims: The claims or statements to verify against the knowledge base.

    Returns:
        Fact-check report with confidence ratings.
    """
    return str(fact_check_agent(claims))


# =============================================================================
# SUPERVISOR AGENT
# =============================================================================

supervisor = Agent(
    model=BedrockModel(model_id=_model_id),
    system_prompt="""You are the Supervisor Agent for the RAG Research system.

For complex research queries, break them into sub-tasks and delegate to specialists:
- ask_deep_research: For questions requiring multi-document synthesis
- ask_summarizer: For condensing long findings into executive summaries
- ask_fact_checker: For verifying claims against the knowledge base

For simple questions, answer directly using your own knowledge.
Always synthesize results from multiple agents into a coherent final response.""",
    tools=[ask_deep_research, ask_summarizer, ask_fact_checker,
           search_knowledge_base, web_search, cite_sources],
)
