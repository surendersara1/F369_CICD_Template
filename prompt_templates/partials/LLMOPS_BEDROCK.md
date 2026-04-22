# SOP — LLMOps (Amazon Bedrock, Prompts, Guardrails, Knowledge Base, Agents)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
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

### 3.1 IAM policies (scoped to exact model ARN)

```python
import aws_cdk as cdk
from aws_cdk import aws_iam as iam, aws_ssm as ssm, aws_bedrock as bedrock


def _create_ai(self, stage: str) -> None:
    region = self.region
    account = self.account

    # Models this project uses (environment-specific for safety)
    default_model  = "anthropic.claude-3-sonnet-20240229-v1:0"
    fallback_model = "anthropic.claude-3-haiku-20240307-v1:0"
    model_arns = [
        f"arn:aws:bedrock:{region}::foundation-model/{default_model}",
        f"arn:aws:bedrock:{region}::foundation-model/{fallback_model}",
    ]

    # Model invocation — scoped to these specific models
    self.bedrock_policy = iam.ManagedPolicy(
        self, "BedrockInvokePolicy",
        managed_policy_name=f"{{project_name}}-bedrock-invoke-{stage}",
        statements=[
            iam.PolicyStatement(
                sid="InvokeSpecificModels",
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
        default_model  = "anthropic.claude-3-sonnet-20240229-v1:0"
        fallback_model = "anthropic.claude-3-haiku-20240307-v1:0"
        model_arns = [
            f"arn:aws:bedrock:{region}::foundation-model/{default_model}",
            f"arn:aws:bedrock:{region}::foundation-model/{fallback_model}",
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
