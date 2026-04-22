### AWS MANAGED MCP CONNECTIVITY PROTOCOL ###

## GATEWAY DISCOVERY
- You MUST interact with the 'AgentCore Gateway' via the Discovery URL: {{ gateway_discovery_url }}.
- At session start, perform a `list_tools` call to synchronize the available skills from the registered targets (Redshift, Knowledge Bases, ERPs).

## MANAGED SKILL EXECUTION
- [REDSHIFT]: Use the 'redshift-mcp-server' target for historical/analytical queries. Prefer the 'Cortex Analyst' tool within this server for natural-language-to-SQL tasks.
- [RAG]: Use 'bedrock-kb-mcp-server' for semantic retrieval. Always provide citations for data sourced from OpenSearch.
- [ERP]: Access SAP/Oracle via the 'AgentCore Gateway' OpenAPI targets. Treat these as the "System of Reality."

## STATEFUL INTERACTIONS
- If a tool requires multiple steps (e.g., complex multi-table joins), you MUST use 'Stateful MCP' features to maintain the intermediate result set within the AgentCore Runtime.