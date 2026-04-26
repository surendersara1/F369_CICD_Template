# SOP — Production LLM fine-tuning (PEFT-LoRA · QLoRA · adapter inference · JumpStart domain adaptation)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker JumpStart Foundation Models · PEFT-LoRA / QLoRA / DoRA / IA³ · LoRA adapter inference components on real-time endpoints · SageMaker Pipelines for fine-tuning workflow · Hugging Face PEFT library + Optimum Neuron · Trainium2 cost-optimized fine-tuning · Hugging Face/JumpStart deep learning containers

---

## 1. Purpose

- Codify the **production LLM fine-tuning pattern** for orgs that have:
  - An open-weight base model (Llama 3 8B/70B/405B, Mistral 7B/24B, Qwen 2.5 7B/32B/72B, Gemma 3 9B/27B)
  - 1K - 1M labeled samples (instruction tuning, domain adaptation, RLHF preference data)
  - Production-grade requirements: lineage, model registry, A/B testing, rollback
- Provide the **PEFT-LoRA decision tree** vs full fine-tune vs RAG vs JumpStart UI.
- Codify the **adapter inference component pattern** (NEW 2024) — multiple LoRA adapters on a single base-model endpoint, dynamically loaded per-request. Critical for multi-tenant fine-tuned LLM serving.
- Codify the **domain adaptation pipeline** using JumpStart's UI-driven flow.
- Codify the **SageMaker Pipelines wrapper** for fine-tuning: data prep → train → eval → register → deploy.
- This is the **production LLM fine-tuning specialisation**. `MLOPS_HYPERPOD_FM_TRAINING` covers training-from-scratch / 100B+ scale. This partial covers production fine-tuning workflows on existing base models.

When the SOW signals: "fine-tune Llama 3 70B on our domain", "QLoRA on our customer-support tickets", "multi-tenant LoRA adapters", "register fine-tuned model with Model Registry", "production endpoint serving multiple fine-tuned variants".

---

## 2. Decision tree — fine-tuning vs alternatives

```
Goal?
├── Make the model speak your domain language → §3 PEFT-LoRA fine-tune
├── Teach a new task (classification, NER, etc.) → §3 PEFT-LoRA on labeled data
├── Just inject knowledge from documents → consider RAG instead (cheaper, simpler)
├── Improve base model behavior on user preferences → DPO/RLHF (advanced, see §HyperPod)
└── Quick prototype on JumpStart UI → §4 JumpStart UI domain adaptation

Fine-tune scope?
├── Full parameter (all weights updated) → expensive, requires HyperPod (see MLOPS_HYPERPOD_FM_TRAINING)
├── PEFT-LoRA (1-5% params) → §3 (most common production choice)
├── PEFT-QLoRA (4-bit quantized base + LoRA) → §3 (lowest cost; ~25% accuracy hit)
├── DoRA / IA³ (newer PEFT variants) → §3 (experimental; PEFT lib supports)
└── Prompt tuning (no weight updates) → consider Bedrock Prompt Caching instead

Multi-tenant serving?
├── Many tenants, each with own adapter (5-50 adapters, shared base) → §5 adapter inference components
├── Few high-volume tenants, dedicated endpoint each → MLOPS_SAGEMAKER_SERVING
└── Single fine-tuned model, no multi-tenancy → MLOPS_SAGEMAKER_SERVING with single variant
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — fine-tune script + endpoint + IAM all in one stack | **§3 / §5 Monolith Variant** |
| `FineTuneStack` owns training + registry; `ServingStack` owns endpoint + adapters | **§7 Micro-Stack Variant** |

---

## 3. PEFT-LoRA Pipeline variant (production fine-tune workflow)

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  SageMaker Pipeline: llm-finetune-pipeline                       │
   │                                                                    │
   │  1. ProcessingStep: data_prep                                     │
   │     - sklearn container; cleans + tokenizes                       │
   │     - input: s3://raw/instruction-tuning/                         │
   │     - output: s3://prep/instruction-tokenized/                    │
   │                                                                    │
   │  2. TrainingStep: lora_train (HuggingFace container)              │
   │     - base_model: meta-llama/Llama-3.1-70B (from JumpStart cache) │
   │     - PEFT lib: LoRA adapter dim=16, alpha=32                     │
   │     - output: s3://artifacts/<run-id>/adapter_model.safetensors   │
   │                                                                    │
   │  3. ProcessingStep: eval                                           │
   │     - run lm-eval-harness on holdout                              │
   │     - output: metrics.json (perplexity, accuracy, BLEU)           │
   │                                                                    │
   │  4. ConditionStep: if metrics > threshold                          │
   │     - True  → register model in Model Registry (Approved=False)   │
   │     - False → fail pipeline, alert team                            │
   │                                                                    │
   │  5. (manual gate) human approves Model Package                     │
   │                                                                    │
   │  6. EventBridge fires on Model Package state change                │
   │     → DeployerLambda updates serving endpoint with new adapter    │
   └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_llm_finetune_pipeline()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_sagemaker as sagemaker,         # L1
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
)


def _create_llm_finetune_pipeline(self, stage: str) -> None:
    """Monolith. SageMaker Pipeline: data_prep → train → eval → register.
    Triggered manually or via EventBridge schedule."""

    # A) S3 buckets — raw data, processed data, model artifacts
    self.training_bucket = s3.Bucket(self, "LlmTrainingBucket",
        bucket_name=f"{{project_name}}-llm-training-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
        removal_policy=RemovalPolicy.RETAIN,
    )

    # B) Pipeline execution role
    self.pipeline_role = iam.Role(self, "FinetunePipelineRole",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
        ],
        permissions_boundary=self.permission_boundary,
    )
    self.training_bucket.grant_read_write(self.pipeline_role)
    self.kms_key.grant_encrypt_decrypt(self.pipeline_role)

    # C) Model Package Group — stable name for registry
    self.model_package_group = sagemaker.CfnModelPackageGroup(self, "LlmMpg",
        model_package_group_name=f"{{project_name}}-llm-mpg-{stage}",
        model_package_group_description=f"Fine-tuned Llama models for {{project_name}}",
    )

    # D) Pipeline definition (JSON authored by helper script)
    # Pipeline JSON is too large to inline here; it's authored in
    # scripts/pipelines/llm_finetune_pipeline.py and uploaded to S3.
    # Pipeline DSL captured in §3.3 below.
    pipeline_definition_s3_key = f"pipelines/llm-finetune-{stage}.json"
    s3deploy.BucketDeployment(self, "PipelineDef",
        sources=[s3deploy.Source.asset("./pipelines/")],
        destination_bucket=self.training_bucket,
        destination_key_prefix="pipelines/",
    )

    # E) The Pipeline resource — references the JSON via PipelineDefinitionS3Location
    self.finetune_pipeline = sagemaker.CfnPipeline(self, "LlmFinetunePipeline",
        pipeline_name=f"{{project_name}}-llm-finetune-{stage}",
        pipeline_definition=sagemaker.CfnPipeline.PipelineDefinitionProperty(
            pipeline_definition_s3_location=sagemaker.CfnPipeline.S3LocationProperty(
                bucket=self.training_bucket.bucket_name,
                key=pipeline_definition_s3_key,
            ),
        ),
        role_arn=self.pipeline_role.role_arn,
        pipeline_display_name=f"LLM Fine-tune {stage}",
    )

    # F) EventBridge: model package approved → Lambda → deploy adapter
    deployer_fn = lambda_.Function(self, "AdapterDeployerFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "deploy_adapter")),
        timeout=Duration.minutes(15),
        environment={
            "MODEL_PACKAGE_GROUP": self.model_package_group.model_package_group_name,
            "ENDPOINT_NAME":       f"{{project_name}}-llm-adapter-endpoint-{stage}",
            "BASE_MODEL_NAME":     "meta-llama/Llama-3.1-70B-Instruct",
        },
    )
    deployer_fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "sagemaker:DescribeModelPackage",
            "sagemaker:CreateInferenceComponent",
            "sagemaker:UpdateInferenceComponent",
            "sagemaker:DeleteInferenceComponent",
        ],
        resources=["*"],
    ))
    self.training_bucket.grant_read(deployer_fn)

    events.Rule(self, "ModelApprovedRule",
        event_pattern=events.EventPattern(
            source=["aws.sagemaker"],
            detail_type=["SageMaker Model Package State Change"],
            detail={
                "ModelPackageGroupName": [self.model_package_group.model_package_group_name],
                "ModelApprovalStatus":   ["Approved"],
            },
        ),
        targets=[targets.LambdaFunction(deployer_fn)],
    )

    CfnOutput(self, "PipelineName", value=self.finetune_pipeline.pipeline_name)
    CfnOutput(self, "ModelPackageGroup",
              value=self.model_package_group.model_package_group_name)
```

### 3.3 Pipeline definition (Python DSL → JSON)

`pipelines/llm_finetune_pipeline.py`:

```python
"""Build pipeline JSON. Run once at deploy: python build_pipeline.py."""
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.pipeline_context import PipelineSession
from sagemaker.workflow.parameters import (ParameterString, ParameterFloat, ParameterInteger)
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.workflow.conditions import ConditionGreaterThan
from sagemaker.workflow.functions import JsonGet
from sagemaker.workflow.model_step import ModelStep
from sagemaker.huggingface import HuggingFace, HuggingFaceProcessor
from sagemaker.sklearn import SKLearnProcessor
from sagemaker import Model
from sagemaker.inputs import TrainingInput, ProcessingInput, ProcessingOutput

session = PipelineSession()

# Pipeline parameters
input_data_uri = ParameterString(name="InputDataUri",
    default_value="s3://qra-llm-training-prod/raw/instruction-tuning/")
base_model_id  = ParameterString(name="BaseModelId",
    default_value="meta-llama/Llama-3.1-70B-Instruct")
lora_rank      = ParameterInteger(name="LoraRank",   default_value=16)
lora_alpha     = ParameterInteger(name="LoraAlpha",  default_value=32)
learning_rate  = ParameterFloat  (name="LearningRate", default_value=2e-5)
eval_threshold = ParameterFloat  (name="EvalThreshold", default_value=0.65)


# 1) Data prep
data_prep_processor = SKLearnProcessor(
    framework_version="1.4-1",
    instance_type="ml.m5.4xlarge",
    instance_count=1,
    role="ROLE_PLACEHOLDER",
    sagemaker_session=session,
)
data_prep_step = ProcessingStep(
    name="DataPrep",
    processor=data_prep_processor,
    code="scripts/prep_instruction_tuning.py",
    inputs=[ProcessingInput(
        source=input_data_uri,
        destination="/opt/ml/processing/input",
    )],
    outputs=[
        ProcessingOutput(output_name="train", source="/opt/ml/processing/output/train"),
        ProcessingOutput(output_name="val",   source="/opt/ml/processing/output/val"),
    ],
)


# 2) PEFT-LoRA training
hf_estimator = HuggingFace(
    entry_point="scripts/train_lora.py",
    role="ROLE_PLACEHOLDER",
    instance_type="ml.p4d.24xlarge",            # 8× A100; for 70B use HyperPod
    instance_count=1,
    transformers_version="4.45.0",
    pytorch_version="2.4.0",
    py_version="py311",
    hyperparameters={
        "base_model_id":  base_model_id,
        "lora_rank":      lora_rank,
        "lora_alpha":     lora_alpha,
        "learning_rate":  learning_rate,
        "num_train_epochs": 3,
        "per_device_train_batch_size": 4,
        "gradient_accumulation_steps": 4,
        "fp16": True,
        "bf16": False,
    },
    sagemaker_session=session,
    distribution={"smdistributed": {"dataparallel": {"enabled": True}}},  # SMDDP
    keep_alive_period_in_seconds=300,            # warm pool for retries
    max_run=86400,                                # 24 hr cap
)
training_step = TrainingStep(
    name="LoraFineTune",
    estimator=hf_estimator,
    inputs={
        "train": TrainingInput(
            s3_data=data_prep_step.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri,
            content_type="application/json",
        ),
        "val": TrainingInput(
            s3_data=data_prep_step.properties.ProcessingOutputConfig.Outputs["val"].S3Output.S3Uri,
            content_type="application/json",
        ),
    },
    cache_config={"Enabled": True, "ExpireAfter": "P30D"},   # cache for 30 days
)


# 3) Eval
eval_processor = HuggingFaceProcessor(
    instance_type="ml.g5.2xlarge",
    instance_count=1,
    role="ROLE_PLACEHOLDER",
    transformers_version="4.45.0",
    pytorch_version="2.4.0",
    sagemaker_session=session,
)
eval_step = ProcessingStep(
    name="Eval",
    processor=eval_processor,
    code="scripts/eval_lora.py",
    inputs=[
        ProcessingInput(
            source=training_step.properties.ModelArtifacts.S3ModelArtifacts,
            destination="/opt/ml/processing/model",
        ),
        ProcessingInput(
            source="s3://qra-llm-training-prod/holdout/",
            destination="/opt/ml/processing/holdout",
        ),
    ],
    outputs=[ProcessingOutput(output_name="metrics", source="/opt/ml/processing/output/metrics")],
)


# 4) Conditional registration
def get_metric(eval_step, metric_name):
    return JsonGet(
        step_name=eval_step.name,
        property_file=eval_step.properties.ProcessingOutputConfig.Outputs["metrics"].S3Output.S3Uri,
        json_path=f"$.{metric_name}",
    )

register_step = ModelStep(
    name="RegisterModel",
    step_args=Model(
        image_uri=hf_estimator.training_image_uri(),
        model_data=training_step.properties.ModelArtifacts.S3ModelArtifacts,
        role="ROLE_PLACEHOLDER",
        sagemaker_session=session,
    ).register(
        content_types=["application/json"],
        response_types=["application/json"],
        inference_instances=["ml.g5.12xlarge", "ml.g5.24xlarge", "ml.p4d.24xlarge"],
        transform_instances=["ml.g5.12xlarge"],
        model_package_group_name="qra-llm-mpg-prod",
        approval_status="PendingManualApproval",         # human gate
        customer_metadata_properties={
            "lora_rank":   str(lora_rank.default_value),
            "base_model":  base_model_id.default_value,
        },
    ),
)

condition_step = ConditionStep(
    name="EvalThresholdCheck",
    conditions=[
        ConditionGreaterThan(
            left=get_metric(eval_step, "accuracy"),
            right=eval_threshold,
        ),
    ],
    if_steps=[register_step],
    else_steps=[],
)


# Wire pipeline
pipeline = Pipeline(
    name="qra-llm-finetune-prod",
    parameters=[input_data_uri, base_model_id, lora_rank, lora_alpha, learning_rate, eval_threshold],
    steps=[data_prep_step, training_step, eval_step, condition_step],
    sagemaker_session=session,
)

# Output JSON for CDK to upload
import json
with open("pipelines/llm-finetune-prod.json", "w") as f:
    f.write(pipeline.definition())
```

### 3.4 Training script — `scripts/train_lora.py`

```python
import argparse
import os
import torch
from datasets import load_dataset
from transformers import (AutoTokenizer, AutoModelForCausalLM, TrainingArguments)
from peft import LoraConfig, get_peft_model, TaskType
from trl import SFTTrainer


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--base_model_id", required=True)
    p.add_argument("--lora_rank", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument("--learning_rate", type=float, default=2e-5)
    p.add_argument("--num_train_epochs", type=int, default=3)
    p.add_argument("--per_device_train_batch_size", type=int, default=4)
    p.add_argument("--gradient_accumulation_steps", type=int, default=4)
    p.add_argument("--fp16", action="store_true")
    p.add_argument("--bf16", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    # Load base model + tokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.base_model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model_id,
        torch_dtype=torch.bfloat16 if args.bf16 else torch.float16,
        device_map="auto",
    )

    # LoRA config — only attention + output projection layers
    lora_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    # Output: trainable params: 86M (0.12%) of 70B total

    # Load dataset
    train = load_dataset("json", data_files="/opt/ml/input/data/train/*.json", split="train")
    val = load_dataset("json", data_files="/opt/ml/input/data/val/*.json", split="train")

    # Training arguments
    training_args = TrainingArguments(
        output_dir="/opt/ml/model",
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        gradient_checkpointing=True,                       # save memory
        learning_rate=args.learning_rate,
        bf16=args.bf16,
        fp16=args.fp16 and not args.bf16,
        logging_steps=10,
        save_strategy="steps",
        save_steps=500,
        evaluation_strategy="steps",
        eval_steps=500,
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        report_to="tensorboard",
    )

    # SFTTrainer (HF TRL — easier than raw Trainer for instruction tuning)
    trainer = SFTTrainer(
        model=model,
        train_dataset=train,
        eval_dataset=val,
        args=training_args,
        tokenizer=tokenizer,
        max_seq_length=2048,
        dataset_text_field="text",                          # field in input JSON
    )

    trainer.train()
    # Saves adapter only — not full base model. Output ~250 MB instead of 140 GB.
    trainer.model.save_pretrained("/opt/ml/model")


if __name__ == "__main__":
    main()
```

---

## 4. JumpStart UI domain adaptation variant

For non-engineering teams: SageMaker JumpStart provides a no-code domain-adaptation flow:

1. **Open Studio → JumpStart → Pick model** (e.g. Llama 3 70B).
2. **Click Fine-tune** → Upload S3 prefix with `train.jsonl` (instruction format).
3. **Configure hyperparameters** (epochs, batch size, learning rate; defaults are sensible).
4. **Launch training job** — SageMaker manages container + instance + checkpointing.
5. **Output: Model Package** registered in default group.

Use this when:
- Time-to-first-result < 1 day required
- No PyTorch/HuggingFace expertise on team
- Standard PEFT-LoRA settings are sufficient

CDK wraps this as a **JumpStart-launched fine-tune trigger Lambda** for orchestration:

```python
from aws_cdk import aws_lambda as lambda_

jumpstart_fn = lambda_.Function(self, "JumpStartFineTuneFn",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="index.handler",
    code=lambda_.Code.from_asset(str(LAMBDA_SRC / "jumpstart_finetune")),
    timeout=Duration.minutes(15),
    environment={
        "JUMPSTART_MODEL_ID":     "meta-textgeneration-llama-3-70b-instruct",
        "JUMPSTART_MODEL_VERSION":"4.0.0",
        "TRAINING_INSTANCE_TYPE": "ml.p4d.24xlarge",
        "MAX_RUN_HOURS":          "24",
    },
)
# Lambda code calls SageMaker JumpStart SDK:
# sagemaker.jumpstart.estimator.JumpStartEstimator(
#     model_id=..., role=..., instance_type=...
# ).fit({"training": "s3://...input.jsonl"})
```

---

## 5. Adapter Inference Components variant (multi-tenant LoRA serving)

NEW 2024+ feature: deploy multiple LoRA adapters on a single base-model endpoint. Adapter is loaded on-demand per request via `LoRAName` parameter. Cost-efficient for multi-tenant fine-tuned LLM serving.

### 5.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  Endpoint: qra-llm-multi-tenant-prod                             │
   │     - Instance: ml.g5.48xlarge (8× A10G GPUs, 192 GB)             │
   │     - Base model: Llama 3 70B (loaded once, hot)                  │
   │                                                                    │
   │  Inference Components:                                            │
   │     - llama-base                  (the base model itself)         │
   │     - tenant-acme-adapter         (LoRA, 250 MB)                  │
   │     - tenant-globex-adapter       (LoRA, 250 MB)                  │
   │     - tenant-initech-adapter      (LoRA, 250 MB)                  │
   │     - ... up to ~30 adapters can fit alongside base               │
   │                                                                    │
   │  Request: { "model_id": "tenant-acme-adapter",                    │
   │             "prompt": "...",                                      │
   │             "parameters": {...} }                                 │
   │  Response: <generated text using acme-tuned adapter>              │
   └──────────────────────────────────────────────────────────────────┘
```

### 5.2 CDK delta — register adapter as inference component

```python
def _create_adapter_inference_component(self,
                                         endpoint_name: str,
                                         tenant_slug: str,
                                         adapter_s3_uri: str) -> None:
    """Adds a LoRA adapter as a new inference component on the multi-tenant
    base-model endpoint. Called from DeployerLambda when Model Package is approved."""

    component = sagemaker.CfnInferenceComponent(self, f"AdapterIc{tenant_slug}",
        endpoint_name=endpoint_name,
        inference_component_name=f"adapter-{tenant_slug}",
        variant_name="AllTraffic",
        specification=sagemaker.CfnInferenceComponent.InferenceComponentSpecificationProperty(
            base_inference_component_name="llama-base",        # ATTACHES to base
            container=sagemaker.CfnInferenceComponent.InferenceComponentContainerSpecificationProperty(
                artifact_url=adapter_s3_uri,                    # adapter weights
            ),
            compute_resource_requirements=sagemaker.CfnInferenceComponent.InferenceComponentComputeResourceRequirementsProperty(
                min_memory_required_in_mb=512,                  # adapter is small
                number_of_accelerator_devices_required=0,       # uses base's GPUs
            ),
        ),
        runtime_config=sagemaker.CfnInferenceComponent.InferenceComponentRuntimeConfigProperty(
            copy_count=1,                                       # singleton; rolling upgrade
        ),
    )
    return component
```

### 5.3 Invoking the adapter

```python
import boto3

runtime = boto3.client("sagemaker-runtime")

response = runtime.invoke_endpoint(
    EndpointName="qra-llm-multi-tenant-prod",
    InferenceComponentName="adapter-acme",                # picks the adapter
    ContentType="application/json",
    Body=json.dumps({
        "inputs":     "Customer asked: can I get a refund? My response:",
        "parameters": {
            "max_new_tokens": 200,
            "temperature":    0.3,
            "top_p":          0.9,
        },
    }),
)
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| OOM during 70B LoRA training on 1× p4d | Activation memory not checkpointed | Set `gradient_checkpointing=True`; reduce `per_device_train_batch_size=1` |
| Eval accuracy worse than base | Catastrophic forgetting | Reduce learning rate to 5e-6; train fewer epochs (1-2); use higher LoRA rank (32-64) |
| Adapter inference component fails to load | Memory budget too tight | `min_memory_required_in_mb` should match adapter size + 1 GB overhead |
| Multi-tenant endpoint queue stalls | All adapters competing for GPU | Use `inference_component_concurrent_invocations_per_instance` to limit |
| Pipeline cache miss every run | Hyperparameter changed → cache key changed | Cache only steps with deterministic outputs; vary hyperparams in main pipeline |
| HuggingFace container can't access JumpStart base model | IAM missing JumpStart S3 perms | `arn:aws:s3:::jumpstart-cache-prod-{region}/*` read access |
| QLoRA NaN loss | bnb 4-bit quantization incompatible with FP16 | Use BF16 (`bf16=True, fp16=False`); requires Ampere+ GPUs (A10G/A100/H100) |

### 6.1 Cost ballpark — fine-tune workflows

| Workflow | Compute | Time | Cost |
|---|---|---|---|
| Llama 3 8B PEFT-LoRA on 100K samples | 1× ml.g5.12xlarge | 2 hr | ~$10 |
| Llama 3 8B QLoRA on 1M samples | 1× ml.g5.12xlarge | 8 hr | ~$40 |
| Llama 3 70B PEFT-LoRA on 1M samples | 1× ml.p4d.24xlarge | 24 hr | ~$800 |
| Llama 3 70B PEFT-LoRA on 1M samples (HyperPod) | 8× ml.p5e.48xlarge | 6 hr | ~$1,500 |
| Llama 3 70B JumpStart UI (default settings) | 1× ml.p4d.24xlarge | 12 hr | ~$400 |
| Multi-tenant inference (100 tenants × 1K rps) | 1× ml.g5.48xlarge | 24/7 | ~$2,400/mo |

---

## 7. Micro-Stack variant (cross-stack via SSM)

```python
# In FineTuneStack
ssm.StringParameter(self, "MpgName",
    parameter_name=f"/{{project_name}}/{stage}/llm/mpg-name",
    string_value=self.model_package_group.model_package_group_name)
ssm.StringParameter(self, "TrainingBucket",
    parameter_name=f"/{{project_name}}/{stage}/llm/training-bucket",
    string_value=self.training_bucket.bucket_name)

# In ServingStack — endpoint + adapter components
mpg_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/llm/mpg-name")

# DeployerLambda subscribes to model approval events for THIS mpg
events.Rule(self, "MpgApprovedRuleRemote",
    event_pattern=events.EventPattern(
        source=["aws.sagemaker"],
        detail={"ModelPackageGroupName": [mpg_name],
                "ModelApprovalStatus": ["Approved"]},
    ),
    targets=[targets.LambdaFunction(adapter_deployer_fn)],
)
```

---

## 8. Five non-negotiables

1. **Always PEFT-LoRA before considering full fine-tune.** 80-95% of the gain at 1-5% the cost. Full fine-tune only when LoRA plateaus and business case justifies $50K+ run.

2. **Adapter inference components for multi-tenant.** Single-endpoint-per-tenant is 50× more expensive at scale. Multi-tenant adapters share base model GPUs.

3. **Eval threshold gate before model registration.** Without `ConditionStep` checking accuracy/perplexity, every training run pollutes the registry. Auto-approve only if metrics > human-set threshold.

4. **Manual approval gate before production deployment.** `ModelApprovalStatus="PendingManualApproval"` forces human eyeball on the metrics + sample outputs before EventBridge → DeployerLambda fires.

5. **Save only the adapter (not full base model).** PEFT correctly outputs ~250 MB adapter; if you save the full model, you ship 140 GB per registered version. Cost + bandwidth nightmare.

---

## 9. References

- `docs/template_params.md` — `LLM_BASE_MODEL_ID`, `LORA_RANK`, `LORA_ALPHA`, `LLM_FINETUNE_INSTANCE`, `LLM_EVAL_THRESHOLD`, `LLM_ADAPTER_MAX_PER_ENDPOINT`
- AWS docs:
  - [JumpStart foundation models](https://docs.aws.amazon.com/sagemaker/latest/dg/jumpstart-foundation-models.html)
  - [Fine-tune LLM domain adaptation](https://docs.aws.amazon.com/sagemaker/latest/dg/jumpstart-foundation-models-fine-tuning-domain-adaptation.html)
  - [Adapter inference components](https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-adapt.html)
  - [HyperPod PEFT-LoRA Llama 3 70B tutorial](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-eks-checkpointless-recipes-peft-llama.html)
  - [Hugging Face PEFT library](https://huggingface.co/docs/peft/index)
- Related SOPs:
  - `MLOPS_HYPERPOD_FM_TRAINING` — for 100B+ training or full fine-tune
  - `MLOPS_SAGEMAKER_TRAINING` — pipeline + Model Registry foundation
  - `MLOPS_SAGEMAKER_SERVING` — single-tenant inference variant
  - `MLOPS_TRAINIUM_INFERENTIA_NEURON` — Trainium for cost-optimized PEFT
  - `LLMOPS_BEDROCK` — alternative path: Bedrock Custom Models / Bedrock Imported Models

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — production LLM fine-tuning workflow with PEFT-LoRA, adapter inference components, JumpStart UI flow, SageMaker Pipeline DSL, train_lora.py reference script. CDK monolith + micro-stack with SSM. Cost ballpark per workflow. 5 non-negotiables. Created to fill F369 audit gap (2026-04-26): existing MLOPS_PIPELINE_LLM_FINETUNING was a stub; this is the full production-grade replacement. |
