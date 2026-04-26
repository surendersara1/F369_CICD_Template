# SOP — Distributed training on SageMaker (SMDDP data parallel · SMP model parallel · EFA · FSDP · DeepSpeed)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Training Jobs (non-HyperPod) · SageMaker Distributed Data Parallel (SMDDP) library · SageMaker Model Parallel (SMP) library · PyTorch FSDP · DeepSpeed ZeRO-3 · EFA networking · multi-node multi-GPU clusters · Hugging Face Accelerate

---

## 1. Purpose

- Codify the **SageMaker Training Job** distributed-training patterns when HyperPod is overkill but single-node training is too slow.
- Provide the **decision tree** between data-parallel (SMDDP / FSDP / DDP) and model-parallel (SMP / tensor-parallel / pipeline-parallel) approaches.
- Codify the **EFA + placement group** networking patterns required for efficient multi-node all-reduce.
- Cover the **Hugging Face Trainer + Accelerate + DeepSpeed** integration that most teams actually use.
- Cover the **automatic mixed precision (AMP) + gradient checkpointing + sharded optimizer** memory-saving combos.
- This is the **multi-node training without cluster lifecycle specialisation**. `MLOPS_HYPERPOD_FM_TRAINING` covers persistent clusters; this partial covers ephemeral training jobs (SageMaker spins up + tears down per training run).

When the SOW signals: "train across 8 GPUs / multiple nodes", "FSDP for memory savings", "we hit OOM at batch size 1", "DeepSpeed ZeRO-3 across 4 nodes", "we need efficient 70B fine-tuning without HyperPod overhead".

---

## 2. Decision tree — parallelism strategy

```
Model size?
├── Fits on 1 GPU (< 7B params with gradient checkpointing) → §3 SMDDP data-parallel
├── Fits on 1 node (8 GPUs combined memory, ~30B params) → §3 SMDDP + FSDP
├── Spans nodes (> 30B params) → §4 SMP model-parallel OR DeepSpeed ZeRO-3
└── 100B+ params, > 24h training → STOP — use MLOPS_HYPERPOD_FM_TRAINING

Library preference?
├── HF ecosystem (Llama, Mistral, BERT) → Accelerate + FSDP / DeepSpeed (§3.4)
├── PyTorch native → SMDDP + DistributedDataParallel (§3.2)
├── Custom architecture → SMP for tensor + pipeline parallel
└── Stable Diffusion / multi-modal → SMDDP + FSDP

Hardware?
├── A100 (p4d.24xlarge, 8× 40GB) → cost-effective for ≤ 30B
├── H100 (p5.48xlarge, 8× 80GB) → 3× faster than A100; for 30B-70B
├── H200 (p5e.48xlarge, 8× 141GB) → for 70B+ without sharding
└── Trainium2 (trn2.48xlarge, 16 chips) → use MLOPS_TRAINIUM_INFERENTIA_NEURON

Infrastructure model?
├── One-shot training, < 24 hr → §3 SageMaker Training Job (this partial)
├── Persistent cluster, multiple jobs → MLOPS_HYPERPOD_FM_TRAINING
└── Hyperparameter tuning, many parallel runs → SageMaker HPO + this partial
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — script + estimator + S3 buckets in one stack | **§3 / §4 Monolith Variant** |
| `TrainingStack` owns IAM + estimator config; `JobsStack` triggers via SFN/EB | **§6 Micro-Stack Variant** |

---

## 3. SMDDP Data Parallel + FSDP variant (most common)

### 3.1 Architecture

```
   ┌────────────────────────────────────────────────────────────────┐
   │  SageMaker Training Job: lora-llama-3-13b                     │
   │   - Estimator: HuggingFace                                     │
   │   - Instance: ml.p4d.24xlarge × 4 (32 GPUs total)              │
   │   - distribution={"smdistributed": {"dataparallel": {enabled}}}│
   │   - EFA enabled automatically (p4d/p5/p5e instances)            │
   └────────────────┬───────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Inside each container:                                          │
   │     - PyTorch DDP (gradient sync) via SMDDP backend              │
   │     - FSDP shards model weights across 8 GPUs/node               │
   │     - HF Accelerate / Trainer wraps the loop                     │
   │     - All-reduce: SMDDP optimized for AWS topology (vs NCCL)     │
   └──────────────────────────────────────────────────────────────────┘
                    │
                    ▼
   S3 model output → /opt/ml/model → tarball uploaded after training
   Checkpoints: /opt/ml/checkpoints (SageMaker auto-uploads to S3)
```

### 3.2 CDK — `_create_smddp_training_estimator()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_sagemaker as sagemaker,         # L1 — Estimator is Python SDK side; CDK provides infra
)

# Estimator config + execution lives in Python script run via CDK Lambda OR
# is captured in a Pipeline (see MLOPS_LLM_FINETUNING_PROD §3.2)


def _create_smddp_training_resources(self, stage: str) -> None:
    """Monolith. CDK creates: S3 input/output buckets + IAM execution role +
    Lambda triggers training estimator with SMDDP enabled."""

    # A) Buckets
    self.training_input  = s3.Bucket(self, "TrainingInput",
        bucket_name=f"{{project_name}}-train-input-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
    )
    self.training_output = s3.Bucket(self, "TrainingOutput",
        bucket_name=f"{{project_name}}-train-output-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
        removal_policy=RemovalPolicy.RETAIN,
    )

    # B) Training execution role
    self.training_role = iam.Role(self, "TrainingRole",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
        ],
        permissions_boundary=self.permission_boundary,
    )
    self.training_input.grant_read(self.training_role)
    self.training_output.grant_read_write(self.training_role)
    self.kms_key.grant_encrypt_decrypt(self.training_role)

    # C) Trigger Lambda — invoked by SFN, EB, or manual API call
    trigger_fn = lambda_.Function(self, "TrainingTriggerFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "trigger_training")),
        timeout=Duration.minutes(5),
        environment={
            "TRAINING_INPUT":  self.training_input.bucket_name,
            "TRAINING_OUTPUT": self.training_output.bucket_name,
            "ROLE_ARN":        self.training_role.role_arn,
            "INSTANCE_TYPE":   "ml.p4d.24xlarge",
            "INSTANCE_COUNT":  "4",
            "MAX_RUN_HOURS":   "24",
        },
    )
    trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateTrainingJob",
                 "sagemaker:DescribeTrainingJob",
                 "sagemaker:StopTrainingJob"],
        resources=[
            f"arn:aws:sagemaker:{self.region}:{self.account}:training-job/*"
        ],
    ))
    trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.training_role.role_arn],
        conditions={"StringEquals": {
            "iam:PassedToService": "sagemaker.amazonaws.com",
        }},
    ))
```

### 3.3 Trigger Lambda — `trigger_training/index.py`

```python
"""Triggers SageMaker Training Job with SMDDP data parallel."""
import os
import json
import time
import boto3

sm = boto3.client("sagemaker")


def handler(event, context):
    """event: { 'job_name_prefix', 'hyperparameters', 's3_train', 's3_val' }"""

    job_name = f"{event['job_name_prefix']}-{int(time.time())}"

    # Hugging Face DLC image URI for Pytorch 2.4 + transformers 4.45 + EFA
    image_uri = (
        f"763104351884.dkr.ecr.{os.environ['AWS_REGION']}.amazonaws.com/"
        f"huggingface-pytorch-training:2.4.0-transformers4.45.0-gpu-py311-cu124-ubuntu22.04"
    )

    response = sm.create_training_job(
        TrainingJobName=job_name,
        AlgorithmSpecification={
            "TrainingImage":      image_uri,
            "TrainingInputMode":  "FastFile",        # stream from S3, no full copy
        },
        RoleArn=os.environ["ROLE_ARN"],
        ResourceConfig={
            "InstanceType":  os.environ["INSTANCE_TYPE"],   # ml.p4d.24xlarge
            "InstanceCount": int(os.environ["INSTANCE_COUNT"]),
            "VolumeSizeInGB": 200,
            "KeepAlivePeriodInSeconds": 300,                # warm pool for retries
        },
        InputDataConfig=[
            {
                "ChannelName": "train",
                "DataSource": {"S3DataSource": {
                    "S3DataType": "S3Prefix",
                    "S3Uri":      event["s3_train"],
                    "S3DataDistributionType": "FullyReplicated",
                }},
                "InputMode": "FastFile",
            },
            {
                "ChannelName": "val",
                "DataSource": {"S3DataSource": {
                    "S3DataType": "S3Prefix",
                    "S3Uri":      event["s3_val"],
                    "S3DataDistributionType": "FullyReplicated",
                }},
                "InputMode": "FastFile",
            },
        ],
        OutputDataConfig={
            "S3OutputPath": f"s3://{os.environ['TRAINING_OUTPUT']}/jobs/{job_name}/",
            "KmsKeyId":     os.environ.get("KMS_KEY_ARN", ""),
        },
        StoppingCondition={
            "MaxRuntimeInSeconds":     int(os.environ["MAX_RUN_HOURS"]) * 3600,
            "MaxWaitTimeInSeconds":    (int(os.environ["MAX_RUN_HOURS"]) + 1) * 3600,
        },
        EnableManagedSpotTraining=False,                    # spot for non-prod only
        CheckpointConfig={
            "S3Uri":     f"s3://{os.environ['TRAINING_OUTPUT']}/checkpoints/{job_name}/",
            "LocalPath": "/opt/ml/checkpoints",
        },
        VpcConfig={
            "SecurityGroupIds": [os.environ["TRAINING_SG_ID"]],
            "Subnets":          os.environ["TRAINING_SUBNETS"].split(","),
        },
        EnableNetworkIsolation=False,                       # SMDDP needs network access for S3
        EnableInterContainerTrafficEncryption=True,
        # ────── DISTRIBUTION CONFIG: SMDDP DATA PARALLEL ───────────────────
        # This is the magic: SageMaker recognizes this and configures EFA +
        # SMDDP backend automatically across all instances.
        HyperParameters=event.get("hyperparameters", {}),
        # The 'sagemaker_distributed_dataparallel_enabled' is set via the
        # Estimator SDK; for raw boto3 use TrainingJobDistributionConfig:
        # NOTE: most teams use the HF Estimator SDK (cleaner) — see §3.4
    )
    return {"job_name": job_name, "arn": response["TrainingJobArn"]}
```

### 3.4 Hugging Face Estimator equivalent (cleaner)

For most teams, the HF Estimator SDK in the trigger Lambda is simpler:

```python
import sagemaker
from sagemaker.huggingface import HuggingFace


def trigger_with_hf_estimator(event):
    estimator = HuggingFace(
        entry_point="train_lora.py",                            # in source_dir below
        source_dir="scripts/",                                    # uploaded to S3
        role=os.environ["ROLE_ARN"],
        instance_type="ml.p4d.24xlarge",
        instance_count=4,                                         # 4 nodes × 8 GPUs = 32 GPUs
        transformers_version="4.45.0",
        pytorch_version="2.4.0",
        py_version="py311",
        # ──── SMDDP DATA PARALLEL ────
        distribution={
            "smdistributed": {
                "dataparallel": {
                    "enabled": True,
                    "custom_mpi_options": "-x NCCL_DEBUG=WARN",
                },
            },
        },
        # Environment variables on each node
        environment={
            "TRANSFORMERS_OFFLINE":   "0",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "FI_PROVIDER":             "efa",
            "FI_EFA_USE_DEVICE_RDMA":  "1",                       # EFA RDMA mode
            "RDMAV_FORK_SAFE":         "1",
        },
        hyperparameters=event.get("hyperparameters", {}),
        max_run=86400,                                            # 24 hr cap
        keep_alive_period_in_seconds=300,
        checkpoint_s3_uri=f"s3://{os.environ['TRAINING_OUTPUT']}/checkpoints/",
        checkpoint_local_path="/opt/ml/checkpoints",
        sagemaker_session=sagemaker.Session(),
    )
    estimator.fit(
        inputs={
            "train": event["s3_train"],
            "val":   event["s3_val"],
        },
        job_name=f"{event['job_name_prefix']}-{int(time.time())}",
        wait=False,                                               # async
    )
```

### 3.5 Training script with FSDP — `scripts/train_lora.py`

```python
"""Hugging Face training with FSDP + SMDDP backend.
Run by SageMaker via the Estimator's distribution config."""
import argparse
import os
import torch
from datasets import load_from_disk
from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments, Trainer
from peft import LoraConfig, get_peft_model, TaskType

# SMDDP integration (auto-detected when distribution={"smdistributed": ...})
import smdistributed.dataparallel.torch.torch_smddp                    # noqa: F401
import torch.distributed as dist


def parse():
    p = argparse.ArgumentParser()
    p.add_argument("--base_model_id", default="meta-llama/Llama-3.1-8B-Instruct")
    p.add_argument("--lora_rank", type=int, default=16)
    p.add_argument("--per_device_train_batch_size", type=int, default=4)
    p.add_argument("--num_train_epochs", type=int, default=3)
    p.add_argument("--learning_rate", type=float, default=2e-5)
    return p.parse_args()


def main():
    args = parse()

    # SMDDP init — replaces standard DDP init
    dist.init_process_group(backend="smddp")                         # NOT "nccl"

    rank       = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    torch.cuda.set_device(local_rank)

    # Load model + tokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.base_model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model_id,
        torch_dtype=torch.bfloat16,
        device_map="cpu",                                            # FSDP wraps; not auto
    )

    # PEFT-LoRA
    lora_config = LoraConfig(
        r=args.lora_rank, lora_alpha=args.lora_rank * 2,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05, bias="none", task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)

    # FSDP — set via TrainingArguments.fsdp_config (HF Trainer wraps for us)
    training_args = TrainingArguments(
        output_dir="/opt/ml/model",
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=4,
        bf16=True,                                                   # required on A100/H100
        fsdp="full_shard auto_wrap",                                 # FSDP enable
        fsdp_config={
            "min_num_params":     1_000_000,                         # only shard layers > 1M params
            "transformer_layer_cls_to_wrap": "LlamaDecoderLayer",    # auto-wrap units
            "fsdp_offload_params": False,                            # CPU-offload off (slow)
            "fsdp_state_dict_type": "FULL_STATE_DICT",
        },
        gradient_checkpointing=True,
        learning_rate=args.learning_rate,
        warmup_steps=100,
        logging_steps=10,
        save_strategy="steps",
        save_steps=500,
        evaluation_strategy="steps",
        eval_steps=500,
        ddp_backend="smddp",                                         # use SMDDP all-reduce
        report_to="tensorboard",
    )

    train_ds = load_from_disk("/opt/ml/input/data/train")
    val_ds   = load_from_disk("/opt/ml/input/data/val")

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        tokenizer=tokenizer,
    )
    trainer.train()
    if rank == 0:
        trainer.save_model("/opt/ml/model")


if __name__ == "__main__":
    main()
```

### 3.6 SMDDP all-reduce vs NCCL — when each wins

| Topology | NCCL | SMDDP | Winner |
|---|---|---|---|
| 1 node, 8 GPUs (NVLink) | bandwidth-saturating | same | tie (NCCL simpler) |
| 2 nodes, 16 GPUs, 100 Gbps | 90% efficiency | 95% efficiency | SMDDP |
| 4 nodes, 32 GPUs, 400 Gbps EFA | 75% efficiency | 92% efficiency | SMDDP (clear) |
| 8+ nodes, EFA | < 60% efficiency | 88% efficiency | SMDDP (5× cheaper at scale) |

Set `ddp_backend="smddp"` in HF Trainer to use SMDDP.

---

## 4. SMP (Model Parallel) variant — for models too big for FSDP

When model + activations + optimizer state exceed all GPUs combined memory:

```python
# Estimator distribution for SMP
distribution = {
    "torch_distributed": {
        "enabled": True,
    },
    "smdistributed": {
        "modelparallel": {
            "enabled": True,
            "parameters": {
                "tensor_parallel_degree":   8,           # split across 8 GPUs/node
                "pipeline_parallel_degree": 4,           # 4 pipeline stages
                "ddp": True,                              # data-parallel across replicas
                "hybrid_shard_degree": 8,                 # FSDP within tensor-parallel group
                "auto_partition": True,                   # SMP picks layer split points
                "default_partition": 0,
                "fp16_params": False,
                "bf16_params": True,
                "delayed_parameter_initialization": True,
                "activation_checkpointing": True,
                "skip_tracing": True,
            },
        },
    },
}
```

Use SMP when:
- Model > 70B params
- Hierarchical 3D parallelism needed (tensor + pipeline + data)
- FSDP alone insufficient

Most teams now prefer FSDP + DeepSpeed ZeRO-3 (open-source); SMP is AWS-specific.

---

## 5. DeepSpeed ZeRO-3 alternative (open-source)

Equivalent to FSDP + SMDDP for memory savings; broader community support:

```python
# In training script
from transformers import TrainingArguments

training_args = TrainingArguments(
    output_dir="/opt/ml/model",
    deepspeed="ds_config_zero3.json",                    # JSON config
    bf16=True,
    gradient_accumulation_steps=4,
    ...
)
```

`ds_config_zero3.json`:

```json
{
  "bf16": {"enabled": true},
  "zero_optimization": {
    "stage": 3,
    "offload_optimizer": {
      "device": "none",
      "pin_memory": true
    },
    "offload_param": {
      "device": "none",
      "pin_memory": true
    },
    "overlap_comm": true,
    "contiguous_gradients": true,
    "sub_group_size": 1e9,
    "reduce_bucket_size": "auto",
    "stage3_prefetch_bucket_size": "auto",
    "stage3_param_persistence_threshold": "auto",
    "stage3_max_live_parameters": 1e9,
    "stage3_max_reuse_distance": 1e9,
    "stage3_gather_16bit_weights_on_model_save": true
  },
  "gradient_accumulation_steps": "auto",
  "gradient_clipping": "auto",
  "steps_per_print": 10,
  "train_batch_size": "auto",
  "train_micro_batch_size_per_gpu": "auto",
  "wall_clock_breakdown": false
}
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| OOM at batch size 1 | Model + optimizer state too big for GPU | Switch to ZeRO-3 / FSDP; enable gradient_checkpointing; reduce sequence length |
| All-reduce 10× slower than expected | NCCL fallback (EFA not active) | Verify env: `echo $FI_PROVIDER` should be `efa`; update Estimator env block |
| Training stalls after 1 epoch | DataLoader bottleneck | Increase `dataloader_num_workers=4`; preprocess + cache datasets |
| Loss spikes / NaN | Mixed-precision overflow | Use BF16 not FP16 (Ampere+); reduce learning rate 5×; enable gradient clipping |
| First step takes 20 min | Compilation + dataset load | Use `keep_alive_period_in_seconds=300` for warm pool; `FastFile` input mode |
| Logs flood with NCCL warnings | NCCL_DEBUG=INFO | Set `NCCL_DEBUG=WARN` in `custom_mpi_options` |
| Different ranks compute different losses | Random seed not synced | Set `seed=42` in TrainingArguments + `torch.manual_seed(42)` per-rank |
| Spot interruption mid-training | Spot capacity reclaimed | Use checkpoint_s3_uri; on restart, resume from latest |

### 6.1 Cost ballpark — multi-node training jobs

| Workload | Compute | Time | Cost |
|---|---|---|---|
| 7B fine-tune (8 GPUs) | 1× p4d.24xlarge | 4 hr | ~$130 |
| 13B fine-tune (8 GPUs) | 1× p4d.24xlarge | 8 hr | ~$260 |
| 70B fine-tune (32 GPUs FSDP) | 4× p4d.24xlarge | 16 hr | ~$2,100 |
| 70B fine-tune (32 H100 FSDP) | 4× p5.48xlarge | 6 hr | ~$2,400 |
| 70B fine-tune (HyperPod 64 GPUs) | 8× p5e.48xlarge | 4 hr | ~$1,000 |
| 405B fine-tune | 8× p5e.48xlarge SMP | 36 hr | ~$8,800 |

**HyperPod is cheaper for repeat training.** Standalone Training Jobs are cheaper for one-shot.

---

## 7. Worked example — pytest

```python
def test_smddp_training_resources_synthesize():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    from infrastructure.cdk.stacks.smddp_training_stack import SmdpTrainingStack
    stack = SmdpTrainingStack(app, stage_name="dev", env=env, ...)
    t = Template.from_stack(stack)

    # 2 buckets (input + output)
    t.resource_count_is("AWS::S3::Bucket", Match.greater_than_or_equal(2))
    # SageMaker execution role
    t.has_resource_properties("AWS::IAM::Role", Match.object_like({
        "AssumeRolePolicyDocument": Match.object_like({
            "Statement": Match.array_with([Match.object_like({
                "Principal": Match.object_like({
                    "Service": "sagemaker.amazonaws.com",
                }),
            })]),
        }),
    }))
    # Trigger Lambda with sagemaker:CreateTrainingJob perm
    t.has_resource_properties("AWS::Lambda::Function", Match.object_like({
        "Environment": Match.object_like({
            "Variables": Match.object_like({
                "INSTANCE_TYPE": Match.string_like_regexp(r"ml\.(p4d|p5e?|trn2)\..*"),
            }),
        }),
    }))
```

---

## 8. Five non-negotiables

1. **Always use BF16 (not FP16) on Ampere+ hardware.** FP16 has 5-bit exponent → underflow on FM gradients. BF16 has 8-bit exponent (same as FP32) → safe. Set `bf16=True, fp16=False` in TrainingArguments.

2. **Gradient checkpointing on for any model > 7B.** Trades 30% compute for 50% memory — usually net win since memory is the bottleneck.

3. **EFA env vars MUST be set in Estimator.** `FI_PROVIDER=efa`, `FI_EFA_USE_DEVICE_RDMA=1`, `RDMAV_FORK_SAFE=1`. Without these, NCCL silently falls back to TCP (10× slower).

4. **`KeepAlivePeriodInSeconds=300` for warm pools.** Subsequent retries (after Spot interrupt or transient OOM) skip the 5-10 min container start.

5. **`MaxRuntimeInSeconds + MaxWaitTimeInSeconds` to cap cost.** Without these, a runaway training job can spin for days. Standard: `MaxRuntime=24h`, `MaxWait=25h`.

---

## 9. References

- `docs/template_params.md` — `TRAINING_INSTANCE_TYPE`, `TRAINING_INSTANCE_COUNT`, `TRAINING_USE_SMDDP`, `TRAINING_USE_FSDP`, `TRAINING_USE_DEEPSPEED`, `TRAINING_BF16`
- AWS docs:
  - [SageMaker Distributed Training](https://docs.aws.amazon.com/sagemaker/latest/dg/distributed-training.html)
  - [SMDDP overview](https://docs.aws.amazon.com/sagemaker/latest/dg/data-parallel.html)
  - [SMP overview](https://docs.aws.amazon.com/sagemaker/latest/dg/model-parallel.html)
  - [Hugging Face Estimator](https://sagemaker.readthedocs.io/en/stable/frameworks/huggingface/index.html)
  - [PyTorch FSDP](https://pytorch.org/tutorials/intermediate/FSDP_tutorial.html)
- Related SOPs:
  - `MLOPS_HYPERPOD_FM_TRAINING` — persistent cluster alternative for repeat training
  - `MLOPS_LLM_FINETUNING_PROD` — full pipeline wrapper
  - `MLOPS_SAGEMAKER_TRAINING` — single-node baseline
  - `MLOPS_TRAINIUM_INFERENTIA_NEURON` — Trainium2 alternative
  - `LAYER_NETWORKING` — VPC + EFA + cluster placement group

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — ephemeral SageMaker Training Jobs with SMDDP data parallel + FSDP + DeepSpeed ZeRO-3 alternatives. CDK for IAM + buckets + trigger Lambda. HF Estimator + raw boto3 patterns. SMP model-parallel for 70B+ models. SMDDP vs NCCL all-reduce comparison table. Cost ballpark per workload. 5 non-negotiables. Created to fill F369 audit gap (2026-04-26): distributed training across 8+ GPUs was 0% covered. |
