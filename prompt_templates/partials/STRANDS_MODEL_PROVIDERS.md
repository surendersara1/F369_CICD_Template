# PARTIAL: Strands Model Providers — Model Fallback, Guardrail Integration, Multi-Provider

**Usage:** Include when SOW mentions model selection, cost optimization, Haiku/Sonnet routing, guardrails, or multiple LLM providers.

---

## Model Selection Pattern (from real production)

```
Production uses intelligent model routing:
  Simple queries (KPI lookups, data retrieval) → Haiku (10x cheaper)
  Complex queries (RCA, forecasting, simulations) → Sonnet (higher quality)
  Approval queries (POs, compliance, authorization) → Sonnet (full chain)

Classification is keyword-based (no LLM call needed):
  SIMPLE: "what is", "show me", "current", "latest", "how much", "list", "get"
  COMPLEX: "why", "root cause", "analyze", "compare", "recommend", "forecast"
  APPROVAL: "approve", "authorize", "purchase order", "payment", "compliance"
```

---

## Model Fallback with Complexity Classification — Pass 3 Reference

```python
"""Model selection: simple→Haiku (10x cheaper), complex→Sonnet."""
import re
from strands.models import BedrockModel

SIMPLE = re.compile(r'\b(what is|show me|current|latest|how much|list|get|total|display)\b', re.I)
COMPLEX = re.compile(r'\b(why|root cause|analyze|compare|recommend|forecast|simulate|variance|decompose|trend|impact|risk|scenario)\b', re.I)
APPROVAL = re.compile(r'\b(approve|authorize|purchase order|PO\b|payment|sign off|budget approval|compliance check|vendor onboard)\b', re.I)

def classify_query_complexity(query: str) -> str:
    if len(APPROVAL.findall(query)) >= 1: return 'approval'
    complex_n = len(COMPLEX.findall(query))
    simple_n = len(SIMPLE.findall(query))
    if complex_n >= 2: return 'complex'
    if simple_n >= 1 and complex_n == 0: return 'simple'
    return 'complex' if len(query) > 100 else 'simple'

def build_model_with_fallback(query, primary_id, fallback_id, guardrail_id='', guardrail_version='DRAFT'):
    complexity = classify_query_complexity(query)
    model_id = fallback_id if complexity == 'simple' else primary_id
    model = build_bedrock_model(model_id, guardrail_id, guardrail_version)
    return model, model_id, complexity
```

---

## Guardrail Integration — Pass 3 Reference

```python
"""Attach Bedrock Guardrail to Strands BedrockModel."""
def build_bedrock_model(model_id: str, guardrail_id: str = '', guardrail_version: str = 'DRAFT'):
    if guardrail_id:
        return BedrockModel(model_id=model_id, additional_request_fields={
            'guardrailConfig': {
                'guardrailIdentifier': guardrail_id,
                'guardrailVersion': guardrail_version,
                'trace': 'enabled',  # Enable guardrail trace for audit
            }
        })
    return BedrockModel(model_id=model_id)
```

---

## Supported Providers

| Provider | Python | TypeScript | Import |
|----------|--------|------------|--------|
| Amazon Bedrock | ✅ | ✅ | `from strands.models.bedrock import BedrockModel` |
| Amazon Nova | ✅ | ❌ | `from strands.models.nova import NovaModel` |
| Anthropic (direct) | ✅ | ❌ | `from strands.models.anthropic import AnthropicModel` |
| OpenAI | ✅ | ✅ | `from strands.models.openai import OpenAIModel` |
| Google Gemini | ✅ | ✅ | `from strands.models.google import GoogleModel` |
| Ollama (local) | ✅ | ❌ | `from strands.models.ollama import OllamaModel` |
| SageMaker | ✅ | ❌ | `from strands.models.sagemaker import SageMakerModel` |
| Custom | ✅ | ✅ | Implement `Model` interface |

---

## Multi-Provider Example

```python
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.models.openai import OpenAIModel

# Bedrock (default — uses IAM role, no API key)
bedrock = BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0")

# OpenAI (needs API key in env or Secrets Manager)
openai = OpenAIModel(client_args={"api_key": os.environ["OPENAI_API_KEY"]}, model_id="gpt-4o")

# Models are interchangeable — just swap
agent = Agent(model=bedrock, system_prompt="...", tools=[...])
```
