# SOP — LLMOps (Amazon Bedrock, Prompts, Guardrails, Knowledge Base, Agents)

**Version:** 2.3 · **Last-reviewed:** 2026-06-17 · **Status:** Active (CANONICAL for Bedrock InvokeModel ARN shapes + lifecycle awareness + cost-aware routing)
**R4 update (2026-06-17, F-AFIE-20 — on top of F-AFIE-01+02 from 2026-06-16):** §3.0b NEW — pricing source-of-truth + cost-aware model routing. Documents the authoritative AWS pricing page URL + CUR 2.0 spend reconciliation, the 4 token-type categories (input/output/cache-read/cache-write — partial sums ≠ bill), 3 service tiers (standard/priority/flex), per-model $/M pricing snapshot, cheap-routing pattern (Sonnet for reasoning, Haiku for classification + extraction), SSM-driven model_router helper, per-invoke CW metrics emitter, and pre-deploy cost-check checklist. AFIE Sprint 10 F-FIN-09 retro: ms-09 paid $4,200 in week 6 (vs forecast $1,800) by Sonnet-routing every query including simple classification; cheap-routing would have saved ~60%.
**R4 update (2026-06-16):**
- §3.0 NEW — Current Active Models + Lifecycle Awareness subsection with authoritative table (Active vs Legacy/EOL with dates) + mandatory MCP currency-check pattern. Closes AFIE Sprint 8 F-AI-01 (Sonnet 4 EOL 2026-10-14 + offline->15d-may-lose-access risk). [F-AFIE-02]
- §3.1 + §4 — InvokeModel IAM grants restructured to the canonical 3-ARN pattern (`foundation-model/*` + `inference-profile/*` + `application-inference-profile/*`). Default model bumped from Claude 3 Sonnet/Haiku (Legacy/EOL) to Claude Sonnet 4.5 + Haiku 4.5 (Active). Closes AFIE Sprint 10 G-NEW-01 deploy-blocker. [F-AFIE-01]

AWS docs verified live via MCP: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html + https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Bedrock · SSM Parameter Store

---

## 1. Purpose

Bedrock integration for generative AI workloads:

- Model invocation policy scoped to exact model ARNs (no `bedrock:*` on `*`)
- Prompts stored in SSM Parameter Store (editable without redeploy)
- Guardrails for PII filtering + denied topics
- Batch inference via `CreateModelInvocationJob`
- Provisioned Throughput for sustained high TPS
- Knowledge Base (RAG) with OpenSearch Serverless vector store (Phase 3)
- Agents with Action Groups (Phase 3)

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Bedrock IAM + prompts + consuming Lambda in one stack | **§3 Monolith Variant** |
| Bedrock IAM/prompts in `AiStack`, consumer Lambdas in `ComputeStack` | **§4 Micro-Stack Variant** |

Bedrock resources themselves (model ARNs, Guardrails) aren't IAM-mutatable by consumers — cycles are less common. Main risk: if consumer grants itself access via `aiStack.bedrock_policy.attach_to_role(...)`, that's fine (adds to role identity). Don't try to call `role.grant_invoke(bedrock_model)` — no such method.

---

## 3. Monolith Variant

### 3.0 Current Active Models + Lifecycle Awareness (mandatory currency check)

**AWS doc (authoritative):** https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html

Bedrock models pass through three states: **Active → Legacy → EOL**. Once Legacy is announced, existing customers **may lose access after 15 days of inactivity** — i.e. a deployment that goes offline for two weeks may already be unable to invoke its synthesis model when redeployed. This is the AFIE-CPG Sprint 8 F-AI-01 incident.

**Verify against the AWS doc before every deploy.** The MCP currency-check pattern (per OPS_AWS_SERVICE_CURRENCY_CHECK partial) is:

```
mcp__awslabs_aws-documentation-mcp-server__read_documentation
url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
```

#### Active models (as of 2026-06-16, verify before deploy)

| Provider | Model | Bedrock model ID (foundation-model) | Notes |
|---|---|---|---|
| Anthropic | Claude Sonnet 4.5 | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Canonical synthesis model |
| Anthropic | Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1:0` | Canonical fallback (cheaper routing) |
| Amazon | Titan-embed-text v2 | `amazon.titan-embed-text-v2:0` | Canonical embeddings (1024-dim default) |
| Amazon | Nova Sonic v2 | `amazon.nova-sonic-v2:0` | Speech (v1 is Legacy — see below) |
| Cohere | Cohere Rerank v3.5 | `cohere.rerank-v3-5:0` | Reranking for KB |

#### Legacy / EOL — DO NOT use in new partials or new project code

| Model | Legacy date | EOL date | Public extended access from | Replacement |
|---|---|---|---|---|
| `anthropic.claude-3-sonnet-20240229-v1:0` | 2026-01-30 | **2026-07-30** (past) | — | claude-sonnet-4-5 |
| `anthropic.claude-3-haiku-20240307-v1:0` | 2026-03-10 | 2026-09-10 | 2026-06-10 | claude-haiku-4-5 |
| `anthropic.claude-sonnet-4-20250514-v1:0` | 2026-04-14 | **2026-10-14** | 2026-07-14 | claude-sonnet-4-5 |
| `amazon.nova-sonic-v1:0` | 2026-03-13 | 2026-09-14 | — | nova-sonic-v2 |
| `amazon.nova-premier-v1:0` | 2026-03-13 | 2026-09-14 | — | (see model card) |
| `amazon.titan-image-generator-v2:0` | 2025-12-30 | **2026-06-30** (past) | — | (see model card) |

**Pre-deploy checklist:**
- [ ] Run the MCP currency-check above; reconcile against the tables here
- [ ] Grep your project for any literal model ID in the Legacy/EOL table
- [ ] If any project deployment has been offline >15 days, expect Bedrock access to be revoked on the first Legacy model call — replace the model literal AND re-deploy before testing

**Project-side discipline:** read the synthesis model from SSM (`/{project}/runtime/default_model`), not from a code literal. This makes the model swap a one-line SSM update, not a redeploy. The AFIE-CPG `agents/shared/ssm_helper.py` pattern is canonical.

### 3.0b Pricing source-of-truth + cost-aware model routing (F-AFIE-20)

**Authoritative pricing page (the SoT):** https://aws.amazon.com/bedrock/pricing/
**Authoritative spend reconciliation (CUR 2.0):** https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-understanding-cur-data.html
**MCP currency check (pre-deploy, mandatory):** the pricing page is NOT served by the AWS Documentation MCP server (it's on `aws.amazon.com`, not `docs.aws.amazon.com`). Use a manual fetch as part of the pre-deploy checklist; reconcile against the table below.

**AFIE Sprint 10 F-FIN-09 retro:** ms-09 sent every query — including simple `is_pii(text)?` classification — to Claude Sonnet 4.5 at $3/M input + $15/M output. Week 6 bill was $4,200 vs forecast $1,800. Re-routing classification + extraction to Haiku ($0.80/M input + $4/M output) would have saved ~60% of the bill. Canonical partial had model IDs but offered no pricing context to inform routing decisions.

**Four token-type categories you MUST track (per the CUR doc cited above):**

| Token type | CUR usage type pattern | Notes |
|---|---|---|
| Input | `*-input-tokens` | Tokens sent in the request prompt |
| Output | `*-output-tokens` | Tokens generated in the response (typically 5x input cost) |
| Cache read | `*-cache-read-input-token-count` | Significantly **cheaper** than input — use prompt caching for repeated context |
| Cache write | `*-cache-write-input-token-count` | More expensive than input — pays off only on ≥ 2-3 reads of the same prefix |

> ⚠️ AWS canonical guidance: "If you only sum input and output tokens, your totals will not match your bill." Reconcile against all FOUR token types or expect 10-30% drift.

**Three service tiers (impact on price & availability):**
- **Standard** — default; on-demand pricing as listed.
- **Priority** — premium ~25% for guaranteed capacity during throttling events; use for prod customer-facing workloads.
- **Flex** — discounted; best-effort latency; for batch and overnight workloads.

**Per-model on-demand pricing (snapshot as of 2026-06-17 — VERIFY against the SoT URL before every deploy):**

| Model | $/M input tokens | $/M output tokens | When to use |
|---|---|---|---|
| `anthropic.claude-sonnet-4-5-20250929-v1:0` | $3.00 | $15.00 | Reasoning, synthesis, multi-step planning |
| `anthropic.claude-haiku-4-5-20251001-v1:0` | $0.80 | $4.00 | Classification, extraction, routing, simple QA |
| `amazon.titan-embed-text-v2:0` | $0.02 | n/a (embedding) | Embeddings (1024-dim default) |
| `cohere.rerank-v3-5:0` | $1.00 per 1K queries | n/a | KB rerank |

**Cost-aware routing pattern — read which model to use from SSM (not from a code literal):**

```python
# agents/shared/model_router.py — pick model by task class
from enum import Enum
import os, boto3

class TaskClass(Enum):
    REASONING       = "reasoning"        # Sonnet 4.5 — multi-step, synthesis
    CLASSIFICATION  = "classification"   # Haiku 4.5 — yes/no, label, route
    EXTRACTION      = "extraction"       # Haiku 4.5 — fields from text
    EMBEDDING       = "embedding"        # Titan-embed v2

_ssm = boto3.client("ssm")
_CACHE: dict[TaskClass, str] = {}


def model_for(task: TaskClass) -> str:
    """Read the configured model ID from SSM. Override at runtime via SSM update."""
    if task in _CACHE:
        return _CACHE[task]
    proj = os.environ["PROJECT_NAME"]
    param = f"/{proj}/runtime/model/{task.value}"
    model_id = _ssm.get_parameter(Name=param)["Parameter"]["Value"]
    _CACHE[task] = model_id
    return model_id
```

```python
# infra/stacks/ai_stack.py — publish the routing config as SSM
ssm.StringParameter(self, "ModelReasoning",
    parameter_name=f"/{project_name}/runtime/model/reasoning",
    string_value="anthropic.claude-sonnet-4-5-20250929-v1:0",
)
ssm.StringParameter(self, "ModelClassification",
    parameter_name=f"/{project_name}/runtime/model/classification",
    string_value="anthropic.claude-haiku-4-5-20251001-v1:0",
)
ssm.StringParameter(self, "ModelExtraction",
    parameter_name=f"/{project_name}/runtime/model/extraction",
    string_value="anthropic.claude-haiku-4-5-20251001-v1:0",
)
```

**Per-invocation cost emission to CloudWatch metrics (so consumers can compute $/query at scale):**

```python
# agents/shared/bedrock_metrics.py — emit per-invoke token counts
from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

_metrics = Metrics(namespace="{project_name}/Bedrock")

def emit_invoke(model_id: str, task: str, input_tokens: int, output_tokens: int,
                cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
    """Emit per-invoke token counts; pair with a CW metric math expression
    that multiplies by $/token to produce a near-real-time cost graph."""
    dims = {"ModelId": model_id, "Task": task}
    for name, n in [
        ("InputTokens", input_tokens),
        ("OutputTokens", output_tokens),
        ("CacheReadTokens", cache_read_tokens),
        ("CacheWriteTokens", cache_write_tokens),
    ]:
        if n > 0:
            _metrics.add_metric(name=name, unit=MetricUnit.Count, value=n)
    _metrics.add_dimensions(**dims)
    _metrics.flush_metrics()
```

**Pre-deploy cost-check checklist:**
- [ ] Open https://aws.amazon.com/bedrock/pricing/ and verify the rates in the table above are still current; update the partial if they've drifted.
- [ ] Confirm CUR 2.0 export is enabled for the AFIE-class workload (per the CUR doc cited above) — otherwise reconciliation is impossible.
- [ ] Verify the SSM model-routing config (`/{project}/runtime/model/*`) maps every TaskClass to the cheapest model that meets the SLO.
- [ ] Verify per-invoke metrics are emitted (CW namespace `{project_name}/Bedrock`) so finance can audit $/query trends.

### 3.1 IAM policies (scoped to exact model ARN)

```python
import aws_cdk as cdk
from aws_cdk import aws_iam as iam, aws_ssm as ssm, aws_bedrock as bedrock


def _create_ai(self, stage: str) -> None:
    region = self.region
    account = self.account

    # Models this project uses (environment-specific for safety)
    # CURRENT ACTIVE MODELS as of 2026-06-16 (verify against
    # https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html):
    #   - Claude Sonnet 4.5 (Active)        | claude-3 Sonnet/Haiku are LEGACY / EOL'd
    #   - Claude Haiku 4.5 (Active)         |
    #   - Titan-embed-text v2 (Active)      | v1 is Legacy
    #   - Nova Sonic v2 (Active)            | v1 is Legacy
    default_model  = "anthropic.claude-sonnet-4-5-20250929-v1:0"   # Active
    fallback_model = "anthropic.claude-haiku-4-5-20251001-v1:0"    # Active

    # AWS doc: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
    # The CANONICAL 3-ARN pattern — foundation-model + inference-profile + application-inference-profile.
    # Cross-region inference profiles (e.g. `us.anthropic.claude-sonnet-4-5-…`,
    # `global.anthropic.claude-sonnet-4-…`) AccessDenied if only foundation-model/* granted.
    # Application inference profiles (Bedrock console-created) need their own ARN class too.
    # This was AFIE-CPG Sprint 10's G-NEW-01 production blocker.
    model_arns = [
        # Foundation-model ARNs (account-empty `::` is correct — global resource)
        f"arn:aws:bedrock:{region}::foundation-model/{default_model}",
        f"arn:aws:bedrock:{region}::foundation-model/{fallback_model}",
        # Cross-region inference profile ARNs (account-scoped)
        f"arn:aws:bedrock:*:{account}:inference-profile/*",
        # Application inference profiles (Bedrock console-created)
        f"arn:aws:bedrock:*:{account}:application-inference-profile/*",
    ]
    # ALTERNATIVE — broader "any active Bedrock model" pattern (use when SSM-driven
    # model swap is the canonical consumer path; keeps grant stable across model rotations):
    #   model_arns = [
    #       f"arn:aws:bedrock:*::foundation-model/*",
    #       f"arn:aws:bedrock:*:{account}:inference-profile/*",
    #       f"arn:aws:bedrock:*:{account}:application-inference-profile/*",
    #   ]

    # Model invocation — scoped per the model_arns list above
    self.bedrock_policy = iam.ManagedPolicy(
        self, "BedrockInvokePolicy",
        managed_policy_name=f"{{project_name}}-bedrock-invoke-{stage}",
        statements=[
            iam.PolicyStatement(
                sid="InvokeBedrockModels",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=model_arns,
            ),
            iam.PolicyStatement(
                sid="ListFoundationModels",
                actions=["bedrock:ListFoundationModels", "bedrock:GetFoundationModel"],
                resources=["*"],
            ),
        ],
    )

    # Attach to consuming Lambda roles (monolith — direct)
    self.lambda_functions["Processing"].role.add_managed_policy(self.bedrock_policy)

    # -- SSM Parameter Store — prompt templates ------------------------------
    prompts = {
        "summary":      "You are a research analyst. Summarize the transcript into 3-5 executive bullets.",
        "sentiment":    "Analyze overall sentiment. Return a score [-1, +1], a label, and 3-5 contributing themes.",
        "key-topics":   "Extract the top 10 key topics with mention counts and 1 representative quote each.",
        "action-items": "Identify action items with suggested owner and priority.",
    }
    self.prompt_params = {}
    for key, body in prompts.items():
        p = ssm.StringParameter(
            self, f"Prompt{key.title().replace('-', '')}",
            parameter_name=f"/{{project_name}}/prompts/{key}",
            string_value=body,
            description=f"Bedrock prompt: {key}",
            tier=ssm.ParameterTier.STANDARD,
        )
        self.prompt_params[key] = p

    # SSM read policy for Lambdas that load prompts at cold start
    self.ssm_prompt_policy = iam.ManagedPolicy(
        self, "SsmPromptReadPolicy",
        managed_policy_name=f"{{project_name}}-ssm-prompt-read-{stage}",
        statements=[
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
                resources=[
                    f"arn:aws:ssm:{region}:{account}:parameter/{{project_name}}/prompts/*"
                ],
            )
        ],
    )
    self.lambda_functions["Processing"].role.add_managed_policy(self.ssm_prompt_policy)
```

### 3.2 Transcribe role (SFN SDK integration target)

```python
self.transcribe_role = iam.Role(
    self, "TranscribeRole",
    assumed_by=iam.ServicePrincipal("transcribe.amazonaws.com"),
    role_name=f"{{project_name}}-transcribe-role-{stage}",
    description="Used by SFN SDK integration to start transcription jobs",
)
# Monolith: L2 grants OK
self.audio_bucket.grant_read(self.transcribe_role)
self.transcript_bucket.grant_write(self.transcribe_role)
self.audio_data_key.grant_decrypt(self.transcribe_role)
```

### 3.3 Monolith gotchas

- **Bedrock model access must be enabled in the AWS console** before first invocation — CDK does not grant this. Manual step per account/region.
- **Prompt updates via SSM** take effect on next Lambda cold start, not instantly. Force rotation by updating an env var on the Lambda (CDK deploy) OR publish an EventBridge "config-updated" event consumed by a warm Lambda that re-reads.
- **Adaptive retry** at the boto3 client is essential for Bedrock; use `Config(retries={"mode": "adaptive", "max_attempts": 5})`.

---

## 4. Micro-Stack Variant

### 4.1 `AiStack` — owns policies + prompts + roles

```python
import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_ssm as ssm,
    aws_s3 as s3,
    aws_kms as kms,
)
from constructs import Construct


class AiStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        audio_bucket: s3.IBucket,
        transcript_bucket: s3.IBucket,
        audio_data_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-ai", **kwargs)

        region, account = self.region, self.account
        # See §3.1 for the canonical 3-ARN pattern + current Active model list.
        # AWS doc: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
        default_model  = "anthropic.claude-sonnet-4-5-20250929-v1:0"   # Active (was claude-3-sonnet — Legacy/EOL)
        fallback_model = "anthropic.claude-haiku-4-5-20251001-v1:0"    # Active (was claude-3-haiku — Legacy/EOL)
        model_arns = [
            # Foundation-model ARNs
            f"arn:aws:bedrock:{region}::foundation-model/{default_model}",
            f"arn:aws:bedrock:{region}::foundation-model/{fallback_model}",
            # Cross-region inference profile ARNs (REQUIRED for us./global. prefixed model IDs)
            f"arn:aws:bedrock:*:{account}:inference-profile/*",
            # Application inference profiles (Bedrock console-created)
            f"arn:aws:bedrock:*:{account}:application-inference-profile/*",
        ]

        self.bedrock_policy = iam.ManagedPolicy(
            self, "BedrockInvokePolicy",
            managed_policy_name="{project_name}-bedrock-invoke",
            statements=[
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=model_arns,
                ),
                iam.PolicyStatement(
                    actions=["bedrock:ListFoundationModels", "bedrock:GetFoundationModel"],
                    resources=["*"],
                ),
            ],
        )

        # Prompts in SSM
        prompts = {
            "summary":      "...Summary prompt...",
            "sentiment":    "...Sentiment prompt...",
            "key-topics":   "...Key topics prompt...",
            "action-items": "...Action items prompt...",
        }
        for key, body in prompts.items():
            ssm.StringParameter(
                self, f"Prompt{key.title().replace('-', '')}",
                parameter_name=f"/{{project_name}}/prompts/{key}",
                string_value=body,
            )

        self.ssm_prompt_policy = iam.ManagedPolicy(
            self, "SsmPromptReadPolicy",
            managed_policy_name="{project_name}-ssm-prompt-read",
            statements=[
                iam.PolicyStatement(
                    actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
                    resources=[f"arn:aws:ssm:{region}:{account}:parameter/{{project_name}}/prompts/*"],
                )
            ],
        )

        # Transcribe role — identity-side grants (critical for micro-stack)
        self.transcribe_role = iam.Role(
            self, "TranscribeRole",
            assumed_by=iam.ServicePrincipal("transcribe.amazonaws.com"),
            role_name="{project_name}-transcribe-role",
        )
        # DO NOT use bucket.grant_read / bucket.grant_write — those auto-grant
        # KMS decrypt on the CMK (in SecurityStack), creating a cross-stack
        # cycle with this stack's TranscribeRole.
        self.transcribe_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[audio_bucket.bucket_arn, audio_bucket.arn_for_objects("*")],
        ))
        self.transcribe_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:AbortMultipartUpload"],
            resources=[transcript_bucket.arn_for_objects("*")],
        ))
        self.transcribe_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"],
            resources=[audio_data_key.key_arn],
        ))

        cdk.CfnOutput(self, "BedrockPolicyArn",   value=self.bedrock_policy.managed_policy_arn)
        cdk.CfnOutput(self, "SsmPromptPolicyArn", value=self.ssm_prompt_policy.managed_policy_arn)
        cdk.CfnOutput(self, "TranscribeRoleArn",  value=self.transcribe_role.role_arn)
```

### 4.2 Consumer ComputeStack — attach managed policies

```python
# In ComputeStack, processing Lambda gets both policies attached
self.processing_fn.role.add_managed_policy(bedrock_policy)
self.processing_fn.role.add_managed_policy(ssm_prompt_policy)
```

`add_managed_policy` is safe cross-stack: it adds the policy ARN as a role attachment. No auto-grant, no cycle.

### 4.3 Guardrails (Phase 2)

```python
from aws_cdk import aws_bedrock as bedrock


self.pii_guardrail = bedrock.CfnGuardrail(
    self, "PiiGuardrail",
    name="{project_name}-pii-guardrail",
    description="Redact PII from Bedrock inputs and outputs",
    blocked_input_messaging="Input contains blocked content.",
    blocked_outputs_messaging="Output contains blocked content.",
    sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
        pii_entities_config=[
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="NAME",  action="ANONYMIZE"),
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="EMAIL", action="ANONYMIZE"),
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type="PHONE", action="ANONYMIZE"),
        ],
    ),
)
```

### 4.4 Knowledge Base (Phase 3, RAG)

```python
# OpenSearch Serverless vector collection + Bedrock KnowledgeBase
# See mlops/04_rag_pipeline.md in F369_LLM_TEMPLATES for the full template.
# High-level CDK shape:
from aws_cdk import aws_opensearchserverless as aoss


collection = aoss.CfnCollection(
    self, "VectorCollection",
    name="{project_name}-vector",
    type="VECTORSEARCH",
)
# + CfnSecurityPolicy (encryption, network, data access)
# + bedrock.CfnKnowledgeBase pointing at the collection + an S3 source bucket
```

### 4.5 Micro-stack gotchas

- **`add_managed_policy` cross-stack** is safe (role attachment, no resource policy mutation).
- **TranscribeRole**: resist the temptation to use `audio_bucket.grant_read(transcribe_role)` — it auto-grants KMS decrypt, triggering the cross-stack cycle through SecurityStack.
- **Bedrock Provisioned Throughput** commits to a reservation; don't enable in non-prod.
- **Guardrail versioning** — `bedrock.CfnGuardrailVersion` is immutable once created; manage via a deployment-time tag.

---

## 5. Batch inference (high-throughput)

```python
# Provisioned Throughput Unit (PTU) for sustained >50 TPS
# Creates a commitment — enable only in prod after capacity planning.
ptu = bedrock.CfnProvisionedModelThroughput(
    self, "Ptu",
    model_units=1,
    provisioned_model_name="{project_name}-ptu",
    model_id=f"arn:aws:bedrock:{region}::foundation-model/{default_model}",
    commitment_duration="OneMonth",  # or SixMonths
)

# Batch inference — process many files from S3 asynchronously
# No native L2; use a Lambda that calls CreateModelInvocationJob:
#   bedrock_client.create_model_invocation_job(
#       roleArn=batch_role.role_arn,
#       modelId=default_model,
#       inputDataConfig={"s3InputDataConfig": {"s3Uri": "s3://.../batch-in/"}},
#       outputDataConfig={"s3OutputDataConfig": {"s3Uri": "s3://.../batch-out/"}},
#   )
```

---

## 6. Worked example

```python
def test_ai_stack_scopes_bedrock_to_exact_model_arns():
    import aws_cdk as cdk
    from aws_cdk import aws_s3 as s3, aws_kms as kms
    from aws_cdk.assertions import Template, Match
    from infrastructure.cdk.stacks.ai_stack import AiStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    dep = cdk.Stack(app, "Dep", env=env)

    ai = AiStack(
        app,
        audio_bucket=s3.Bucket(dep, "A"),
        transcript_bucket=s3.Bucket(dep, "T"),
        audio_data_key=kms.Key(dep, "K"),
        env=env,
    )
    t = Template.from_stack(ai)
    t.has_resource_properties("AWS::IAM::ManagedPolicy", {
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": Match.array_with(["bedrock:InvokeModel"]),
                    "Resource": Match.array_with([Match.string_like_regexp(".*foundation-model.*claude-3-sonnet.*")]),
                }),
            ])
        })
    })
```

---

## 7. References

- `docs/template_params.md` — `BEDROCK_MODEL_ID`, `SSM_PROMPT_PREFIX`, `TRANSCRIBE_*`
- `docs/Feature_Roadmap.md` — A-00..A-32
- Related SOPs: `LAYER_SECURITY` (KMS), `LAYER_BACKEND_LAMBDA` (Lambda consumer pattern), `WORKFLOW_STEP_FUNCTIONS` (SFN → Transcribe)
- External: F369_LLM_TEMPLATES `mlops/04_rag_pipeline.md`, `mlops/12_bedrock_guardrails_agents.md`, `mlops/24_bedrock_prompt_management.md`

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Identity-side grants on TranscribeRole documented as the cycle fix. Managed-policy cross-stack attachment pattern. |
| 1.0 | 2026-03-05 | Initial. |
