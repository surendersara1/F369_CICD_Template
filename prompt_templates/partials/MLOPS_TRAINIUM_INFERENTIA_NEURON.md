# SOP — Trainium2 + Inferentia2 + Neuron SDK (cost-efficient FM training/inference on AWS silicon)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Trainium2 (trn2.48xlarge, 16 chips) · AWS Inferentia2 (inf2.xlarge - inf2.48xlarge) · Neuron SDK 2.20+ · neuronx-distributed (NxD) · Optimum Neuron (HF integration) · NeMo on Neuron · LoRA/QLoRA on Trainium · Llama 3.x serving on Inferentia2

---

## 1. Purpose

- Codify the **AWS silicon pattern** for cost-efficient FM training and inference: Trainium2 trains 75% cheaper than P5e GPUs; Inferentia2 serves Llama 3 70B for ~40% of g5.48xlarge cost.
- Codify the **Neuron SDK toolchain**: compiler (`neuronx-cc`), runtime, neuronx-distributed for parallel training, Optimum Neuron for HF model export.
- Codify the **migration paths** from PyTorch+CUDA to PyTorch+Neuron — most code change is in model loading + parallelism config, not training loop.
- Cover the **inf2 inference deployment** pattern — convert HF model → Neuron compiled artifact → SageMaker endpoint.
- Cover the **Trainium2 cluster** for HyperPod (cross-references `MLOPS_HYPERPOD_FM_TRAINING` §5).
- This is the **AWS-silicon specialisation**. GPU patterns are in `MLOPS_DISTRIBUTED_TRAINING` and `MLOPS_HYPERPOD_FM_TRAINING`.

When the SOW signals: "we want Trainium not GPU", "compete on inference cost", "Neuron SDK", "Inferentia2 for Llama 3 70B", "cost-efficient FM training".

---

## 2. Decision tree — when AWS silicon wins

```
Workload?
├── Training 7B-70B PEFT-LoRA → §3 Trainium2 (40% cheaper than P5e)
├── Training 100B+ from scratch → P5en GPU (Trainium2 not yet supported at 100B+)
├── Inference Llama 3 7B-70B (high volume) → §4 Inferentia2 (40% cheaper than g5)
├── Inference Llama 3 405B → P5e GPU (inf2 doesn't fit 405B yet)
├── Stable Diffusion / vision models → GPU (Neuron SDK has limited CV support)
├── Custom architecture (non-transformer) → GPU (Neuron compiler may not support)
└── Existing CUDA codebase, can't refactor → GPU

Cost-vs-effort?
├── First-time on AWS silicon → 1-2 weeks Neuron migration effort
├── Annual savings > $200K → Neuron pays back in <2 months
└── Annual savings < $50K → stick with GPU (easier ops)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — train + serve in same stack | **§3 / §4 Monolith Variant** |
| Production — TrainingStack on Trainium2 + ServingStack on inf2 | **§5 Micro-Stack Variant** |

---

## 3. Trainium2 training variant — Llama 3 70B PEFT-LoRA

### 3.1 Architecture

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  HyperPod or Training Job: Trainium2 cluster                     │
   │     - Instance: ml.trn2.48xlarge (16× Trainium2 chips per node) │
   │     - Cluster: 4 nodes × 16 chips = 64 chips                    │
   │     - Cost: $7.78/hr per node × 4 = $31/hr total                 │
   │     (vs P5e equivalent: $30/hr × 4 = $123/hr — 75% savings)      │
   └────────────────┬────────────────────────────────────────────────┘
                    │
                    ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Container: AWS DL container w/ Neuron SDK 2.20+                 │
   │     - PyTorch 2.4 + neuronx-distributed (NxD) + Optimum Neuron   │
   │     - NEURON_COMPILE_CACHE_URL=s3://<bucket>/neuron-cache/        │
   │     - NEURON_RT_NUM_CORES=16 (chips per node)                    │
   │     - FI_PROVIDER=efa (cluster networking)                       │
   └─────────────────────────────────────────────────────────────────┘
                    │
                    ▼
   Training script: train_lora_neuron.py
     - Model: HF Llama 3 70B → optimum-neuron loads with NeuronModel
     - Compile model: neuronx-cc compiles graph (one-time, 30-60 min)
     - Train: PyTorch DDP across 64 Trainium chips
     - Output: adapter weights (LoRA, ~250 MB)
```

### 3.2 CDK — `_create_trainium_training_resources()`

Same shape as `MLOPS_DISTRIBUTED_TRAINING §3.2` but with Trainium-specific instance + bootstrap:

```python
# Trigger Lambda environment differs:
trigger_fn = lambda_.Function(self, "TrainiumTrainingFn",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="index.handler",
    code=lambda_.Code.from_asset(str(LAMBDA_SRC / "trigger_trainium_job")),
    environment={
        "TRAINING_INPUT":     self.training_input.bucket_name,
        "TRAINING_OUTPUT":    self.training_output.bucket_name,
        "ROLE_ARN":           self.training_role.role_arn,
        "INSTANCE_TYPE":      "ml.trn2.48xlarge",        # NEW: Trainium2
        "INSTANCE_COUNT":     "4",
        "MAX_RUN_HOURS":      "24",
        "NEURON_CACHE_URL":   f"s3://{{project_name}}-neuron-cache-{stage}/",
    },
)

# Neuron compile cache bucket (CRITICAL for cost)
self.neuron_cache = s3.Bucket(self, "NeuronCache",
    bucket_name=f"{{project_name}}-neuron-cache-{stage}",
    encryption=s3.BucketEncryption.KMS,
    encryption_key=self.kms_key,
    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    enforce_ssl=True,
    lifecycle_rules=[s3.LifecycleRule(
        id="DeleteAfter180Days",
        expiration=Duration.days(180),
    )],
)
self.neuron_cache.grant_read_write(self.training_role)
```

### 3.3 Trigger Lambda — invoke Trainium training job

```python
"""trigger_trainium_job/index.py"""
import os, time, boto3

sm = boto3.client("sagemaker")


def handler(event, context):
    job_name = f"{event['job_name_prefix']}-trn2-{int(time.time())}"

    # Neuron-enabled DLC image (AWS Deep Learning Container)
    image_uri = (
        f"763104351884.dkr.ecr.{os.environ['AWS_REGION']}.amazonaws.com/"
        f"pytorch-training-neuronx:2.4.0-neuronx-py311-sdk2.21.0-ubuntu22.04"
    )

    sm.create_training_job(
        TrainingJobName=job_name,
        AlgorithmSpecification={
            "TrainingImage":      image_uri,
            "TrainingInputMode":  "FastFile",
        },
        RoleArn=os.environ["ROLE_ARN"],
        ResourceConfig={
            "InstanceType":  "ml.trn2.48xlarge",
            "InstanceCount": int(os.environ["INSTANCE_COUNT"]),
            "VolumeSizeInGB": 200,
            "KeepAlivePeriodInSeconds": 300,
        },
        InputDataConfig=[...],            # same as GPU
        OutputDataConfig=...,
        StoppingCondition={"MaxRuntimeInSeconds": 86400},
        # ────── NEURON-SPECIFIC ENV ──────
        Environment={
            "NEURON_RT_NUM_CORES":      "16",
            "NEURON_RT_VISIBLE_CORES":  "0-15",
            "NEURON_COMPILE_CACHE_URL": os.environ["NEURON_CACHE_URL"],
            "FI_PROVIDER":              "efa",
            "FI_EFA_USE_DEVICE_RDMA":   "1",
            "MALLOC_ARENA_MAX":         "64",
            "NEURON_CC_FLAGS":          "--model-type=transformer --auto-cast=none",
        },
        HyperParameters=event.get("hyperparameters", {}),
        # Trainium uses PyTorch DistributedDataParallel via neuronx-distributed
        # No SMDDP config needed — Neuron uses its own collective backend
        VpcConfig={
            "SecurityGroupIds": [os.environ["TRAINING_SG_ID"]],
            "Subnets":          os.environ["TRAINING_SUBNETS"].split(","),
        },
    )
```

### 3.4 Training script — `train_lora_neuron.py`

```python
"""LoRA fine-tune on Trainium2 with neuronx-distributed.
Notable differences from GPU version:
  - import neuronx_distributed instead of torch.distributed
  - NeuronModelForCausalLM instead of AutoModelForCausalLM
  - Compile step before training (one-time)
"""
import os, argparse, torch
from datasets import load_from_disk
from transformers import AutoTokenizer, TrainingArguments
from optimum.neuron import NeuronModelForCausalLM, NeuronTrainer
from peft import LoraConfig, get_peft_model, TaskType


def parse():
    p = argparse.ArgumentParser()
    p.add_argument("--base_model_id", default="meta-llama/Llama-3.1-70B-Instruct")
    p.add_argument("--lora_rank", type=int, default=16)
    p.add_argument("--per_device_train_batch_size", type=int, default=2)
    p.add_argument("--num_train_epochs", type=int, default=3)
    p.add_argument("--learning_rate", type=float, default=2e-5)
    return p.parse_args()


def main():
    args = parse()

    # Tokenizer — standard HF
    tokenizer = AutoTokenizer.from_pretrained(args.base_model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ────── KEY DIFFERENCE: Neuron-aware model loader ──────
    model = NeuronModelForCausalLM.from_pretrained(
        args.base_model_id,
        export=True,                                    # convert to Neuron on first load
        torch_dtype=torch.bfloat16,
        # Neuron compile flags
        compiler_kwargs={
            "auto_cast":     "matmult",
            "auto_cast_type":"bf16",
        },
        # Tensor parallelism across Trainium chips
        tensor_parallel_size=8,                         # 8-way TP
        pipeline_parallel_size=2,                        # 2-way PP
        # Total = 16 chips per node; PyTorch DDP shards data across nodes
    )

    # PEFT-LoRA
    lora_config = LoraConfig(
        r=args.lora_rank, lora_alpha=args.lora_rank * 2,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05, bias="none", task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)

    # Training args — same as GPU version
    training_args = TrainingArguments(
        output_dir="/opt/ml/model",
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=4,
        bf16=True,                                      # required on Trainium
        learning_rate=args.learning_rate,
        logging_steps=10,
        save_strategy="steps",
        save_steps=500,
        ddp_backend="xla",                              # Trainium uses XLA collective
        report_to="tensorboard",
    )

    train_ds = load_from_disk("/opt/ml/input/data/train")
    val_ds   = load_from_disk("/opt/ml/input/data/val")

    # ────── KEY DIFFERENCE: NeuronTrainer instead of HF Trainer ──────
    trainer = NeuronTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        tokenizer=tokenizer,
    )
    trainer.train()
    trainer.save_model("/opt/ml/model")


if __name__ == "__main__":
    main()
```

---

## 4. Inferentia2 inference variant — serving Llama 3 70B

### 4.1 Architecture

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  Endpoint: llama3-70b-inf2-prod                                  │
   │     - Instance: ml.inf2.48xlarge (12× Inferentia2 chips, 384 GB) │
   │     - Cost: $12.98/hr (vs g5.48xlarge $16.29/hr — 20% savings    │
   │       AT MUCH HIGHER THROUGHPUT — typically 40% better cost/req) │
   │     - Min instance: 1; Max auto-scale: 4                          │
   └────────────────┬────────────────────────────────────────────────┘
                    │
                    ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Container: HF DLC w/ Optimum Neuron                             │
   │     - Image: pytorch-inference-neuronx:2.4.0-neuronx-...          │
   │     - Model: precompiled Neuron artifact (1× compile, ∞ inferences)│
   │     - Tensor parallel across 12 chips                              │
   │     - HF text-generation pipeline                                  │
   └─────────────────────────────────────────────────────────────────┘
```

### 4.2 Step 1: Pre-compile model to Neuron format

This is a one-time process per model+config — typically 30-60 min on a c5.4xlarge:

```python
# scripts/compile_llama3_for_inf2.py
from optimum.neuron import NeuronModelForCausalLM

model = NeuronModelForCausalLM.from_pretrained(
    "meta-llama/Llama-3.1-70B-Instruct",
    export=True,
    batch_size=4,
    sequence_length=2048,
    num_cores=12,                                     # all 12 chips on inf2.48xlarge
    auto_cast_type="bf16",
    optlevel=2,
)
# Save compiled artifact — this is what gets uploaded to S3 for endpoint
model.save_pretrained("./llama3-70b-neuron-compiled")
# Outputs:
#   model.safetensors (LoRA-merged or base)
#   neuron_model_compiled.pt
#   compiler_args.json
#   ~30-50 GB total (full base model)
```

Upload to S3:

```bash
tar -czf model.tar.gz llama3-70b-neuron-compiled/
aws s3 cp model.tar.gz s3://qra-models/inf2/llama3-70b/v1/model.tar.gz
```

### 4.3 CDK — `_create_inf2_endpoint()`

```python
def _create_inf2_endpoint(self, stage: str) -> None:
    """Inferentia2 endpoint for Llama 3 70B serving."""

    self.inf2_role = iam.Role(self, "Inf2Role",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.models_bucket.grant_read(self.inf2_role)
    self.kms_key.grant_decrypt(self.inf2_role)

    # Model
    self.inf2_model = sagemaker.CfnModel(self, "Inf2Model",
        model_name=f"{{project_name}}-llama3-70b-inf2-{stage}",
        execution_role_arn=self.inf2_role.role_arn,
        primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
            # Inferentia-enabled HF DLC
            image=f"763104351884.dkr.ecr.{self.region}.amazonaws.com/"
                  f"huggingface-pytorch-tgi-inference:2.4.0-neuronx-py311-sdk2.21.0-ubuntu22.04-v1.0",
            model_data_url=f"s3://{self.models_bucket.bucket_name}/inf2/llama3-70b/v1/model.tar.gz",
            environment={
                "HF_TASK":                "text-generation",
                "HF_MODEL_ID":            "/opt/ml/model",
                "HF_MAX_BATCH_SIZE":      "4",
                "HF_MAX_INPUT_LENGTH":    "1024",
                "HF_MAX_TOTAL_TOKENS":    "2048",
                "HF_NUM_CORES":           "12",
                "NEURON_RT_NUM_CORES":    "12",
                # Critical: pre-compiled artifacts already in /opt/ml/model
                "NEURON_COMPILE_CACHE_URL": "/opt/ml/model/neuron-cache/",
            },
        ),
    )

    # Endpoint config
    self.inf2_ep_config = sagemaker.CfnEndpointConfig(self, "Inf2EpConfig",
        endpoint_config_name=f"{{project_name}}-llama3-inf2-config-{stage}",
        production_variants=[sagemaker.CfnEndpointConfig.ProductionVariantProperty(
            variant_name="AllTraffic",
            model_name=self.inf2_model.model_name,
            initial_instance_count=1,
            instance_type="ml.inf2.48xlarge",
            initial_variant_weight=1.0,
            container_startup_health_check_timeout_in_seconds=600,    # Neuron warm-up
        )],
        kms_key_id=self.kms_key.key_arn,
    )

    self.inf2_endpoint = sagemaker.CfnEndpoint(self, "Inf2Ep",
        endpoint_name=f"{{project_name}}-llama3-70b-inf2-{stage}",
        endpoint_config_name=self.inf2_ep_config.endpoint_config_name,
    )
```

### 4.4 Cost comparison — Llama 3 70B inference

| Endpoint | Instance | Cost/hr | Tokens/sec | Cost/1M tokens |
|---|---|---|---|---|
| g5.48xlarge | 8× A10G (192 GB) | $16.29 | 60 | $75 |
| g6.48xlarge | 8× L40S (376 GB) | $19.94 | 90 | $61 |
| p4d.24xlarge | 8× A100 (320 GB) | $32.77 | 110 | $82 |
| **inf2.48xlarge** | **12× Inf2 (384 GB)** | **$12.98** | **80** | **$45** |
| p5.48xlarge | 8× H100 (640 GB) | $98.32 | 200 | $136 |

Inferentia2 is the **cheapest cost/1M-tokens for Llama 3 70B** at moderate throughput. For ultra-high throughput (>200 tok/s), GPUs win on per-instance throughput; Inferentia2 wins on cost-per-token.

---

## 5. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| First run takes 60 min before training starts | Neuron compilation, no cache | Set `NEURON_COMPILE_CACHE_URL=s3://<bucket>/`; subsequent runs ~30s |
| `torch.distributed.init_process_group` fails | NCCL not on Trainium | Use `xla` backend not `nccl` for Trainium |
| Loss = NaN early in training | Neuron auto-cast incompatibility | Set `--auto-cast=none` in NEURON_CC_FLAGS for sensitive ops |
| Compiled model doesn't load on inf2 | Compile params (batch, seq) mismatch endpoint | Recompile with exact endpoint params |
| Accuracy drop vs GPU baseline | BF16 vs FP32 difference | Acceptable; if regression > 2pp, investigate `auto_cast_type` |
| Inf2 endpoint deployment fails | Container startup timeout | Set `container_startup_health_check_timeout_in_seconds=600` |
| Cost higher than expected | Neuron compile bucket not configured | `NEURON_COMPILE_CACHE_URL` mandatory; without it, recompile every run |
| Custom architecture fails to compile | Neuron compiler doesn't support op | Check Neuron Compatibility Matrix; if op unsupported → use GPU |

### 5.1 Cost ballpark — Trainium2 LoRA fine-tunes

| Workload | Compute | Time | Cost (Trainium2) | Cost (GPU equiv.) |
|---|---|---|---|---|
| Llama 3 8B PEFT-LoRA, 100K samples | 1× trn2.48xlarge | 3 hr | ~$24 | ~$30 (g5.12xlarge) |
| Llama 3 70B PEFT-LoRA, 1M samples | 4× trn2.48xlarge | 12 hr | ~$370 | ~$1,500 (4× p4d) |
| Llama 3 70B full fine-tune (HyperPod) | 16× trn2.48xlarge | 5 days | ~$15,000 | ~$60,000 (16× p5e) |

**Trainium2 saves 60-75% on FM training** for supported workloads.

---

## 6. Five non-negotiables

1. **`NEURON_COMPILE_CACHE_URL` mandatory.** Without it, every training run recompiles the model graph (30-60 min wasted compute). With S3 cache, second run skips compilation entirely.

2. **BF16 is the only reliable precision on Trainium2.** FP16 has stability issues; FP32 wastes the silicon. Set `bf16=True, fp16=False`.

3. **Pre-compile inference models BEFORE deploying.** Inf2 endpoints expect compiled artifacts. Don't ship raw HF models — they'll silently auto-compile on first request, taking 30-60 min and timing out the health check.

4. **Use `xla` DDP backend on Trainium, NOT `nccl`.** NCCL doesn't run on Trainium. The HF Trainer handles this automatically when using NeuronTrainer; raw PyTorch must explicitly set `init_process_group(backend="xla")`.

5. **Verify Neuron compatibility for your exact model.** Most popular models (Llama 3.x, Mistral, Qwen, Gemma 3) work. Less common architectures may have unsupported ops. Check the [Neuron Compatibility Matrix](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/general/arch/model-architecture-fit.html) before committing.

---

## 7. References

- AWS Neuron docs:
  - [Neuron SDK overview](https://awsdocs-neuron.readthedocs-hosted.com/)
  - [SageMaker + Neuron flows](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/devflows/sagemaker-flows.html)
  - [HuggingFace Llama3 SFT-LoRA tutorial](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/libraries/nxd-training/tutorials/hf_llama3_8B_SFT_LORA.html)
- AWS docs:
  - [Trainium and Inferentia overview](https://aws.amazon.com/machine-learning/neuron/)
  - [Optimum Neuron](https://huggingface.co/docs/optimum-neuron/index)
- Related SOPs:
  - `MLOPS_HYPERPOD_FM_TRAINING` §5 — Trainium2 cluster on HyperPod
  - `MLOPS_LLM_FINETUNING_PROD` — GPU equivalent
  - `MLOPS_DISTRIBUTED_TRAINING` — GPU multi-node
  - `MLOPS_SAGEMAKER_SERVING` — GPU inference equivalent

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Trainium2 (training) + Inferentia2 (inference) on Neuron SDK 2.20+. CDK for both training (HF Estimator-style) and inference (CfnEndpoint w/ pre-compiled artifact). Cost comparison vs GPU equivalents. NeuronTrainer + NeuronModelForCausalLM patterns. 5 non-negotiables incl. compile cache mandatory + BF16 + pre-compile inference. Created to fill F369 audit gap (2026-04-26): AWS silicon was 0% covered. |
