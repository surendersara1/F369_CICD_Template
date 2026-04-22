### FEDERATED DATA LAYER (THE REALITY) ###

## DATA TIERING STRATEGY
- [SYSTEM OF REALITY] (ERP): Treat live data from SAP/Oracle/Infor as the immutable truth for current state. Access these via OpenAPI-to-MCP wrappers in the Gateway.
- [SYSTEM OF RECORD] (Redshift): Use the 'Amazon Redshift MCP Server' for historical analysis. Always prefer executing 'Cortex Analyst' tools over writing raw SQL for complex schemas.
- [KNOWLEDGE CONTEXT] (RAG): Access unstructured documentation via the 'Bedrock Knowledge Base MCP Server'.

## DATA FEDERATION RULES
- SEMANTIC JOINING: When joining ERP (Live) and Redshift (Historical) data, you MUST perform the join within the [AgentCore Code Interpreter] using the `agentcore_memory_client` to store intermediate frames.
- DATA PROTECTION: Adhere to 'Bedrock Guardrails'. Sensitive PII from ERP systems MUST be masked before being passed to the LLM sampling layer.

## ERROR HANDLING
- If the [System of Reality] returns a 401/403, do not attempt to guess data. Use the AgentCore Identity provider to check if the user's OAuth2 session has expired.