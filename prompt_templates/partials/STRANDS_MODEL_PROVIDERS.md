# PARTIAL: Strands Model Providers — Multi-LLM Support

**Usage:** Include when SOW mentions multiple LLM providers, model switching, Bedrock, Anthropic, OpenAI, Google Gemini, or any non-default model provider.

---

## Supported Model Providers

| Provider | Python | TypeScript | Import |
|----------|--------|------------|--------|
| Amazon Bedrock | ✅ | ✅ | `from strands.models.bedrock import BedrockModel` |
| Amazon Nova | ✅ | ❌ | `from strands.models.nova import NovaModel` |
| Anthropic (direct) | ✅ | ❌ | `from strands.models.anthropic import AnthropicModel` |
| OpenAI | ✅ | ✅ | `from strands.models.openai import OpenAIModel` |
| Google Gemini | ✅ | ✅ | `from strands.models.google import GoogleModel` |
| LiteLLM | ✅ | ❌ | `from strands.models.litellm import LiteLLMModel` |
| llama.cpp | ✅ | ❌ | `from strands.models.llamacpp import LlamaCppModel` |
| LlamaAPI | ✅ | ❌ | `from strands.models.llamaapi import LlamaAPIModel` |
| MistralAI | ✅ | ❌ | `from strands.models.mistral import MistralModel` |
| Ollama | ✅ | ❌ | `from strands.models.ollama import OllamaModel` |
| SageMaker | ✅ | ❌ | `from strands.models.sagemaker import SageMakerModel` |
| Writer | ✅ | ❌ | `from strands.models.writer import WriterModel` |
| Custom | ✅ | ✅ | Implement `Model` interface |

---

## CDK Code Block — Secrets for Third-Party API Keys

```python
def _create_llm_provider_secrets(self, stage_name: str) -> None:
    """
    Secrets Manager for third-party LLM API keys.

    [Claude: only create secrets for providers mentioned in SOW.
     Bedrock does not need a secret — it uses IAM roles.]
    """

    self.llm_api_keys = {}
    # [Claude: include only providers from SOW]
    for provider in ["anthropic", "openai"]:
        self.llm_api_keys[provider] = sm.Secret(
            self, f"LLMApiKey-{provider.title()}",
            secret_name=f"{{project_name}}/{stage_name}/llm-api-key/{provider}",
            description=f"{provider.title()} API key for Strands agent",
            encryption_key=self.kms_key,
        )
        self.llm_api_keys[provider].grant_read(self.agentcore_runtime_role)
```

---

## Model Initialization Patterns — Pass 3 Reference

```python
"""Model provider initialization patterns."""
from strands import Agent

# --- Amazon Bedrock (default, uses IAM role) ---
from strands.models.bedrock import BedrockModel
bedrock_model = BedrockModel(
    model_id="anthropic.claude-sonnet-4-20250514-v1:0",
    region_name="us-east-1",
)

# --- Anthropic Direct API ---
from strands.models.anthropic import AnthropicModel
anthropic_model = AnthropicModel(
    client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]},
    model_id="claude-sonnet-4-20250514",
)

# --- OpenAI ---
from strands.models.openai import OpenAIModel
openai_model = OpenAIModel(
    client_args={"api_key": os.environ["OPENAI_API_KEY"]},
    model_id="gpt-4o",
)

# --- Google Gemini ---
from strands.models.google import GoogleModel
google_model = GoogleModel(
    model_id="gemini-2.0-flash",
    client_args={"api_key": os.environ["GOOGLE_API_KEY"]},
)

# --- Ollama (local) ---
from strands.models.ollama import OllamaModel
ollama_model = OllamaModel(model_id="llama3.1:8b")

# --- SageMaker Endpoint ---
from strands.models.sagemaker import SageMakerModel
sagemaker_model = SageMakerModel(
    endpoint_name="my-llm-endpoint",
    region_name="us-east-1",
)

# Models are interchangeable — just swap the model instance:
agent = Agent(model=bedrock_model, system_prompt="...", tools=[...])
# Switch to OpenAI: agent = Agent(model=openai_model, ...)
```
