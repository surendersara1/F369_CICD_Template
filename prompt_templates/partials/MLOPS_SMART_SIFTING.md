# SOP — SageMaker Smart Sifting (efficient training data selection — drop 30-50% data with no quality loss)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Smart Sifting library (GA 2024) · PyTorch DataLoader integration · transformers/Hugging Face training scripts · supported tasks: image classification, language modeling, NLP classification

---

## 1. Purpose

- Codify the **Smart Sifting pattern** — a SageMaker library that **drops 30-50% of training data on the fly** by detecting "easy" examples the model already learned, focusing compute on hard examples.
- **Cost-efficiency lever:** training a 7B LoRA on 100K samples drops to ~60-70K effective samples → 30-40% compute saving with negligible quality loss.
- Codify the **Drop-In replacement for PyTorch DataLoader** that adds smart sifting transparently.
- Codify the **decision matrix** when smart sifting helps vs hurts (works on most LM workloads; can hurt on tiny datasets).
- This is the **training cost-efficiency specialisation**. Stacks on top of `MLOPS_LLM_FINETUNING_PROD` and `MLOPS_DISTRIBUTED_TRAINING`.

When the SOW signals: "training is too slow", "we can't afford the full dataset on every epoch", "30% cost reduction on training", "Smart Sifting".

---

## 2. Decision tree — when to use

```
Dataset size?
├── < 10K samples → DON'T use Smart Sifting (overhead > savings)
├── 10K - 1M samples → §3 Smart Sifting (sweet spot)
├── > 1M samples → §3 Smart Sifting (biggest savings)
└── Streaming / continuous training → §4 streaming variant

Workload type?
├── Language modeling (GPT-style) → ✅ supported (BetaSampling)
├── NLP classification → ✅ supported
├── Image classification → ✅ supported
├── Object detection / segmentation → ❌ not yet supported
├── Audio / speech → ❌ not yet supported
└── Custom architecture → ⚠️ test first; may not converge

Quality target?
├── Same accuracy as baseline → §3 use default 0.5 beta_value
├── 30%+ cost saving, ≤1% quality drop → 0.6-0.7 beta_value
└── Maximum cost saving (45%+) → 0.8 beta_value (test carefully)
```

---

## 3. Standard variant — Smart Sifting on PyTorch DataLoader

### 3.1 Architecture

```
   PyTorch DataLoader (standard)
        │
        │  Smart Sifting wraps it
        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  smart_sifting.dataloader.sift_dataloader.SiftingDataloader      │
   │     - Wraps standard DataLoader                                    │
   │     - Adds beta sampling + relative threshold                      │
   │     - Per-batch: drops "easy" samples, keeps hard ones             │
   │     - Per-epoch: ~30-50% effective batch reduction                 │
   └──────────────────────────────────────────────────────────────────┘
        │
        ▼
   Model.train() → only sees the kept samples
   Same loss function, same optimizer
```

### 3.2 Training script integration — drop-in replacement

```python
"""train_with_smart_sifting.py — only the changes from train.py."""
import torch
from torch.utils.data import DataLoader
from transformers import AutoModelForCausalLM, AutoTokenizer

# ────── NEW: Import Smart Sifting ──────
from smart_sifting.dataloader.sift_dataloader import SiftingDataloader
from smart_sifting.loss.abstract_sift_loss_module import Loss
from smart_sifting.sift_config.sift_configs import (
    RelativeProbabilisticSiftConfig, LossConfig, SiftingBaseConfig,
)


# ────── NEW: Subclass to extract loss for sifting decisions ──────
class SiftLLMLoss(Loss):
    """Loss adapter — Smart Sifting needs per-sample loss to decide kept/dropped."""

    def loss(self, model, transformed_batch, original_batch):
        # Forward pass — return per-sample CE loss
        outputs = model(**transformed_batch)
        return outputs.loss.detach()                              # per-sample loss tensor


def train_with_sifting():
    # 1. Build standard DataLoader
    train_dataset = load_my_dataset()
    standard_loader = DataLoader(
        train_dataset,
        batch_size=8,
        shuffle=True,
        num_workers=4,
    )

    # 2. ────── WRAP with SiftingDataloader ──────
    sift_config = RelativeProbabilisticSiftConfig(
        beta_value=0.5,                    # 0.5 = 50% target retention
        loss_history_length=500,            # samples for adaptive threshold
        loss_based_sift_config=LossConfig(
            sift_config=SiftingBaseConfig(
                sift_delay=0,               # skip warmup
            ),
        ),
    )

    sift_loss = SiftLLMLoss()

    sift_loader = SiftingDataloader(
        sift_config=sift_config,
        orig_dataloader=standard_loader,
        loss_impl=sift_loss,
        model=model,                        # the model being trained
    )

    # 3. Train using sift_loader instead of standard
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
    for epoch in range(3):
        for batch in sift_loader:           # ← only kept batches
            outputs = model(**batch)
            loss = outputs.loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
```

### 3.3 SageMaker Estimator integration

```python
from sagemaker.huggingface import HuggingFace

estimator = HuggingFace(
    entry_point="train_with_smart_sifting.py",
    source_dir="scripts/",
    role="ROLE_ARN",
    instance_type="ml.p4d.24xlarge",
    instance_count=1,
    transformers_version="4.45.0",
    pytorch_version="2.4.0",
    py_version="py311",
    # ────── NEW: requirements.txt must include smart-sifting ──────
    # scripts/requirements.txt:
    #   smart-sifting>=0.4.0
    hyperparameters={
        "model_name":      "meta-llama/Llama-3.1-8B-Instruct",
        "beta_value":      0.5,            # target retention rate
        "smart_sifting":   "true",
    },
    distribution={"smdistributed": {"dataparallel": {"enabled": True}}},
    keep_alive_period_in_seconds=300,
    max_run=86400,
)

estimator.fit(inputs={"train": "s3://qra-training/v3/"})
```

### 3.4 Beta value tuning matrix

| beta_value | Retention | Cost saving | Quality risk |
|---|---|---|---|
| 0.3 | ~30% | ~70% | High — many useful samples dropped |
| 0.5 | ~50% | ~50% | Low — sweet spot for most |
| 0.6 | ~60% | ~40% | Very low — recommended for production |
| 0.7 | ~70% | ~30% | Negligible |
| 1.0 | ~100% | 0% | None (no sifting) |

Default: 0.5 for cost-sensitive POCs; 0.6 for production training.

---

## 4. Streaming / continual training variant

For streams (e.g. CDC into S3 → train continuously):

```python
# Combine Smart Sifting with FastFile mode
sift_config = RelativeProbabilisticSiftConfig(
    beta_value=0.6,
    loss_history_length=1000,           # larger window for streaming stability
    loss_based_sift_config=LossConfig(
        sift_config=SiftingBaseConfig(
            sift_delay=100,             # skip first 100 batches (warmup window)
        ),
    ),
)
```

---

## 5. Five non-negotiables

1. **`sift_delay > 0` for production training.** Skipping warmup batches lets the loss-history ringbuffer stabilize. Without warmup, early decisions are noisy.

2. **Target retention 0.5-0.7 in production.** Below 0.5 risks accuracy regression; above 0.7 the savings barely justify the dependency.

3. **Validate on a held-out set BEFORE production rollout.** Train baseline (no sifting) + train w/ sifting on the same data; compare eval metrics. Production rollout if delta < 1pp.

4. **Smart Sifting + DDP/FSDP work together.** Use as drop-in inside the DDP-wrapped training loop. Each rank does its own sifting on its data shard.

5. **Loss adapter must return per-sample loss, not batched mean.** Smart Sifting decisions are per-sample. If your loss returns a scalar, use `loss = F.cross_entropy(logits, labels, reduction="none")`.

---

## 6. References

- AWS docs:
  - [SageMaker Smart Sifting](https://docs.aws.amazon.com/sagemaker/latest/dg/smart-sifting.html)
  - [Smart Sifting GitHub](https://github.com/aws/smart-sifting)
  - [Beta sampling theory](https://docs.aws.amazon.com/sagemaker/latest/dg/smart-sifting-relative-prob.html)
- Related SOPs:
  - `MLOPS_LLM_FINETUNING_PROD` — primary place to apply Smart Sifting
  - `MLOPS_DISTRIBUTED_TRAINING` — works inside DDP/FSDP
  - `MLOPS_HYPERPOD_FM_TRAINING` — works on HyperPod too

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — drop-in PyTorch DataLoader wrapper for 30-50% training cost savings. Beta value tuning matrix. Streaming variant. SageMaker Estimator integration. Created Wave 7 (2026-04-26). |
