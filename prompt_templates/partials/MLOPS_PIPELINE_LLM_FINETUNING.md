# PARTIAL: LLM Fine-Tuning Pipeline — LoRA/QLoRA on SageMaker

**Usage:** Include when SOW mentions custom LLM, fine-tuning, domain-specific chatbot, RLHF, LoRA, QLoRA, instruction tuning, or adapting foundation models.

---

## When to Fine-Tune vs RAG vs Prompt Engineering

| Approach               | When to Use                                                | Cost                | Latency              |
| ---------------------- | ---------------------------------------------------------- | ------------------- | -------------------- |
| **Prompt Engineering** | Model already knows the domain, just needs guidance        | $ (free)            | Same                 |
| **RAG**                | Private docs/knowledge the model doesn't know              | $$                  | +100-500ms retrieval |
| **Fine-Tuning (LoRA)** | Custom tone/style, domain vocabulary, task-specific format | $$$ (training cost) | Same as base         |
| **Full Fine-Tuning**   | Maximum performance, large budget, proprietary data        | $$$$ (GPU hours)    | Same as base         |

**Rule of thumb:** Try RAG first. Fine-tune if RAG accuracy < 80% or you need custom behavior the model can't learn from context.

---

## What LoRA/QLoRA Is (Simply)

```
Full Fine-Tuning: Update ALL 7B parameters → expensive
LoRA:            Freeze base model, train tiny adapter matrices (0.1% of params) → cheap
QLoRA:           LoRA + 4-bit quantization → train 70B models on a single A100

Result: Same quality as full fine-tuning, 10-100x cheaper
```

---

## CDK Code Block — LLM Fine-Tuning Infrastructure

```python
def _create_llm_finetuning_pipeline(self, stage_name: str) -> None:
    """
    LLM Fine-Tuning Pipeline using LoRA/QLoRA on SageMaker.

    Supported base models (set in Architecture Map):
      - meta-llama/Llama-3.1-8B-Instruct     (best quality/cost balance)
      - meta-llama/Llama-3.1-70B-Instruct    (highest quality, needs QLoRA)
      - mistralai/Mistral-7B-Instruct-v0.3   (fast, efficient)
      - tiiuae/falcon-7b-instruct            (permissive license)
      - HuggingFace Hub any model            (set in pipeline params)

    Pipeline Steps:
      1. Data Preparation   — Format raw data into instruction-tuning JSONL
      2. LoRA/QLoRA Training — Fine-tune on SageMaker with Hugging Face DLC
      3. Model Merge        — Merge LoRA adapters into base model weights
      4. Evaluation         — BLEU, ROUGE, perplexity, custom task accuracy
      5. Safety Check       — Run Perspective API / Guardrails eval
      6. Register           — Push to Model Registry + HuggingFace Hub (optional)
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # =========================================================================
    # FINE-TUNING JOB CONFIGURATION
    # [Claude: select GPU instance based on model size from Architecture Map]
    # =========================================================================

    GPU_CONFIGS = {
        "7b":  {"instance": "ml.g5.2xlarge",  "count": 1, "method": "LoRA",  "quantization": "bf16"},
        "13b": {"instance": "ml.g5.12xlarge", "count": 1, "method": "LoRA",  "quantization": "bf16"},
        "34b": {"instance": "ml.g5.48xlarge", "count": 1, "method": "QLoRA", "quantization": "4bit"},
        "70b": {"instance": "ml.p4d.24xlarge","count": 1, "method": "QLoRA", "quantization": "4bit"},
    }

    # [Claude: detect model size from SOW and select GPU config]
    model_size = "7b"  # Default — update from Architecture Map
    gpu_config = GPU_CONFIGS[model_size]

    # =========================================================================
    # PIPELINE TRIGGER LAMBDA
    # =========================================================================

    finetuning_trigger_fn = _lambda.Function(
        self, "LLMFineTuningTrigger",
        function_name=f"{{project_name}}-llm-finetune-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)
sm = boto3.client('sagemaker')

def handler(event, context):
    pipeline_name = os.environ['PIPELINE_NAME']
    base_model_id = event.get('base_model_id', os.environ['DEFAULT_BASE_MODEL'])
    dataset_s3_uri = event.get('dataset_s3_uri', os.environ['DEFAULT_DATASET_URI'])

    params = [
        {"Name": "BaseModelId", "Value": base_model_id},
        {"Name": "DatasetS3Uri", "Value": dataset_s3_uri},
        {"Name": "LoRARank", "Value": str(event.get('lora_rank', 16))},
        {"Name": "LoRaAlpha", "Value": str(event.get('lora_alpha', 32))},
        {"Name": "Epochs", "Value": str(event.get('epochs', 3))},
        {"Name": "LearningRate", "Value": str(event.get('learning_rate', '2e-4'))},
        {"Name": "BatchSize", "Value": str(event.get('batch_size', 4))},
        {"Name": "ModelApprovalStatus", "Value": "PendingManualApproval"},
        {"Name": "RunId", "Value": datetime.utcnow().strftime('%Y%m%d-%H%M%S')},
    ]

    resp = sm.start_pipeline_execution(
        PipelineName=pipeline_name,
        PipelineParameters=params,
        PipelineExecutionDescription=f"Fine-tune {base_model_id} - {datetime.utcnow().isoformat()}",
        ClientRequestToken=context.aws_request_id,
    )
    logger.info(f"Started fine-tuning pipeline: {resp['PipelineExecutionArn']}")
    return {"pipeline_execution_arn": resp['PipelineExecutionArn']}
"""),
        environment={
            "PIPELINE_NAME": f"{{project_name}}-llm-finetune-{stage_name}",
            "DEFAULT_BASE_MODEL": "meta-llama/Llama-3.1-8B-Instruct",
            "DEFAULT_DATASET_URI": f"s3://{self.lake_buckets['processed'].bucket_name}/training-data/llm/",
        },
        timeout=Duration.seconds(30),
    )
    finetuning_trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}-llm-finetune-{stage_name}"],
    ))

    # =========================================================================
    # SAGEMAKER PIPELINE DEFINITION (saved to ml/pipelines/llm_finetuning_pipeline.py)
    # Claude generates this in Pass 3
    # =========================================================================

    # Pipeline Python code template:
    PIPELINE_CODE = '''
# ml/pipelines/llm_finetuning_pipeline.py
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.workflow.model_step import ModelStep
from sagemaker.workflow.parameters import ParameterString, ParameterInteger, ParameterFloat
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.huggingface import HuggingFace
from sagemaker.sklearn.processing import SKLearnProcessor

def create_llm_finetuning_pipeline(sm_session, role_arn, pipeline_name, s3_bucket, model_package_group):
    # === Pipeline Parameters ===
    base_model_id = ParameterString("BaseModelId", default_value="meta-llama/Llama-3.1-8B-Instruct")
    dataset_uri   = ParameterString("DatasetS3Uri")
    lora_rank     = ParameterInteger("LoRARank", default_value=16)
    lora_alpha    = ParameterInteger("LoRaAlpha", default_value=32)
    epochs        = ParameterInteger("Epochs", default_value=3)
    learning_rate = ParameterString("LearningRate", default_value="2e-4")
    batch_size    = ParameterInteger("BatchSize", default_value=4)
    approval_status = ParameterString("ModelApprovalStatus", default_value="PendingManualApproval")
    min_eval_score  = ParameterFloat("MinEvalScore", default_value=0.75)  # Minimum ROUGE-L threshold

    # === Step 1: Data Preparation ===
    # Convert raw data (CSV/JSON/PDF text) → instruction-tuning JSONL format:
    # {"instruction": "...", "input": "...", "output": "..."}
    data_processor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type="ml.m5.2xlarge", instance_count=1,
        sagemaker_session=sm_session,
    )
    data_prep_step = ProcessingStep(
        name="PrepareFineTuningData",
        processor=data_processor,
        code="ml/scripts/llm_data_prep.py",
        inputs=[ProcessingInput(source=dataset_uri, destination="/opt/ml/processing/input")],
        outputs=[
            ProcessingOutput(output_name="train", source="/opt/ml/processing/output/train"),
            ProcessingOutput(output_name="eval",  source="/opt/ml/processing/output/eval"),
        ],
        job_arguments=[
            "--format", "alpaca",        # Alpaca instruction format
            "--max-seq-length", "2048",
            "--train-split", "0.9",
        ],
    )

    # === Step 2: LoRA/QLoRA Training ===
    # Uses HuggingFace Deep Learning Container with TRL + PEFT libraries
    huggingface_estimator = HuggingFace(
        entry_point="ml/scripts/llm_train_lora.py",
        role=role_arn,
        instance_type="ml.g5.2xlarge",    # A10G GPU — good for 7B with LoRA
        instance_count=1,
        transformers_version="4.36.0",
        pytorch_version="2.1.0",
        py_version="py310",
        sagemaker_session=sm_session,

        # Spot instances (up to 70% cheaper for GPU training)
        use_spot_instances=True,
        max_wait=86400,                    # 24hr max wait for spot
        max_run=43200,                     # 12hr max run
        checkpoint_s3_uri=f"s3://{s3_bucket}/checkpoints/{{project_name}}/",
        checkpoint_local_path="/opt/ml/checkpoints",

        hyperparameters={
            "model_id": base_model_id,
            "lora_r": lora_rank,
            "lora_alpha": lora_alpha,
            "lora_dropout": 0.05,
            "num_train_epochs": epochs,
            "learning_rate": learning_rate,
            "per_device_train_batch_size": batch_size,
            "gradient_accumulation_steps": 4,
            "gradient_checkpointing": True,  # Reduce GPU memory
            "bf16": True,                    # bfloat16 mixed precision
            "load_in_4bit": True,            # QLoRA — 4-bit base model
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_compute_dtype": "bfloat16",
            "max_seq_length": 2048,
            "output_dir": "/opt/ml/model",
            "merge_weights": True,           # Merge LoRA into base at end
            "logging_steps": 10,
            "save_strategy": "epoch",
            "warmup_ratio": 0.03,
            "lr_scheduler_type": "cosine",
        },

        # Environment
        environment={
            "HUGGING_FACE_HUB_TOKEN": "{{hf_token_from_secrets_manager}}",
            "NCCL_DEBUG": "INFO",
        },

        metric_definitions=[
            {"Name": "training_loss", "Regex": "training_loss=([0-9\\.]+)"},
            {"Name": "eval_loss",     "Regex": "eval_loss=([0-9\\.]+)"},
            {"Name": "eval_rouge_l",  "Regex": "eval_rouge_l=([0-9\\.]+)"},
        ],
    )

    training_step = TrainingStep(
        name="LoRAFineTuning",
        estimator=huggingface_estimator,
        inputs={
            "train": TrainingInput(data_prep_step.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri),
            "eval":  TrainingInput(data_prep_step.properties.ProcessingOutputConfig.Outputs["eval"].S3Output.S3Uri),
        },
    )

    # === Step 3: Evaluation ===
    eval_processor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type="ml.m5.2xlarge", instance_count=1,
    )
    eval_step = ProcessingStep(
        name="EvaluateFineTunedModel",
        processor=eval_processor,
        code="ml/scripts/llm_evaluate.py",
        inputs=[
            ProcessingInput(source=training_step.properties.ModelArtifacts.S3ModelArtifacts, destination="/opt/ml/processing/model"),
            ProcessingInput(source=data_prep_step.properties.ProcessingOutputConfig.Outputs["eval"].S3Output.S3Uri, destination="/opt/ml/processing/eval"),
        ],
        outputs=[ProcessingOutput(output_name="eval_report", source="/opt/ml/processing/output")],
        job_arguments=["--min-rouge-l", "0.75"],
    )

    # === Step 4: Quality Gate ===
    accuracy_condition = ConditionGreaterThanOrEqualTo(
        left=eval_step.properties.ProcessingOutputConfig.Outputs["eval_report"]...,
        right=min_eval_score,
    )

    register_step = ModelStep(
        name="RegisterFineTunedModel",
        model_approval_status=approval_status,
        model_package_group_name=model_package_group,
        ...
    )

    condition_step = ConditionStep(
        name="QualityGate",
        conditions=[accuracy_condition],
        if_steps=[register_step],
        else_steps=[],
    )

    return Pipeline(
        name=pipeline_name,
        parameters=[base_model_id, dataset_uri, lora_rank, lora_alpha,
                    epochs, learning_rate, batch_size, approval_status, min_eval_score],
        steps=[data_prep_step, training_step, eval_step, condition_step],
        sagemaker_session=sm_session,
    )
'''

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "LLMFineTuningTriggerArn",
        value=finetuning_trigger_fn.function_arn,
        description="Lambda ARN to start LLM fine-tuning pipeline",
        export_name=f"{{project_name}}-llm-finetune-trigger-{stage_name}",
    )
```

---

## Training Script Skeleton (`ml/scripts/llm_train_lora.py`)

```python
# ml/scripts/llm_train_lora.py
import os, argparse, json
from datasets import load_from_disk
from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training, TaskType
from trl import SFTTrainer
import torch

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model_id", type=str)
    p.add_argument("--lora_r", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument("--lora_dropout", type=float, default=0.05)
    p.add_argument("--num_train_epochs", type=int, default=3)
    p.add_argument("--learning_rate", type=float, default=2e-4)
    p.add_argument("--per_device_train_batch_size", type=int, default=4)
    p.add_argument("--gradient_accumulation_steps", type=int, default=4)
    p.add_argument("--max_seq_length", type=int, default=2048)
    p.add_argument("--load_in_4bit", type=bool, default=True)
    p.add_argument("--merge_weights", type=bool, default=True)
    p.add_argument("--output_dir", type=str, default="/opt/ml/model")
    return p.parse_args()

def main():
    args = parse_args()

    # 4-bit quantization config (QLoRA)
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=args.load_in_4bit,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,  # Nested quantization — saves more memory
    )

    # Load base model + tokenizer from HuggingFace Hub
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        quantization_config=bnb_config if args.load_in_4bit else None,
        device_map="auto",
        trust_remote_code=True,
        token=os.environ.get("HUGGING_FACE_HUB_TOKEN"),
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model_id, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # Prepare model for QLoRA training
    model = prepare_model_for_kbit_training(model)

    # LoRA config — only train attention matrices (q, v projections)
    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        target_modules=["q_proj", "v_proj", "k_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()  # e.g., "trainable params: 6,815,744 || all params: 6,747,217,920 || trainable%: 0.10%"

    # Load datasets
    train_dataset = load_from_disk("/opt/ml/input/data/train")
    eval_dataset  = load_from_disk("/opt/ml/input/data/eval")

    # Training arguments
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        gradient_checkpointing=True,
        learning_rate=args.learning_rate,
        bf16=True,
        logging_steps=10,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        warmup_ratio=0.03,
        lr_scheduler_type="cosine",
        report_to=["tensorboard"],
    )

    # SFT Trainer (Supervised Fine-Tuning)
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        tokenizer=tokenizer,
        max_seq_length=args.max_seq_length,
        dataset_text_field="text",  # Combined instruction+input+output field
        packing=True,               # Pack multiple short sequences per GPU step
    )
    trainer.train(resume_from_checkpoint=os.path.exists("/opt/ml/checkpoints"))

    # Merge LoRA adapters into base model for efficient inference
    if args.merge_weights:
        model = model.merge_and_unload()

    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print("Training complete. Model saved.")

if __name__ == "__main__":
    main()
```
