# SOP — MLOps Pipeline: NLP / Hugging Face (BERT-class Text Models on SageMaker)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Pipelines · Hugging Face DLC (transformers 4.36) · BERT / RoBERTa / DistilBERT · CPU inference (`ml.m5.*`) · Managed Instance Scaling

---

## 1. Purpose

- Provision an NLP training + serving pipeline: preprocess → Hugging Face fine-tune (Spot CPU) → eval → F1 quality gate → Model Registry → real-time endpoint with managed autoscaling.
- Codify the task-model table (text classification, NER, sentiment, clinical NLP, summarization, QA, zero-shot, multilingual, embeddings) so Claude picks the right base model from SOW signals.
- Codify the real-time endpoint config with `LEAST_OUTSTANDING_REQUESTS` routing + `ManagedInstanceScaling` (1 → N instances) — CPU only (BERT-class don't need GPU for inference).
- Codify the trigger Lambda — starts the pipeline with task type, base model ID, dataset URI, num_labels, epochs, min F1.
- Include when the SOW mentions text classification, NER, sentiment analysis, document parsing, summarization, QA, entity extraction, or semantic search embeddings.

**NLP task → model selection:**

| NLP task | Recommended model | Container |
|---|---|---|
| Text Classification (multi-class) | `distilbert-base-uncased` / `roberta-base` | HuggingFace DLC |
| Named Entity Recognition (NER) | `dslim/bert-base-NER` / `Jean-Baptiste/roberta-large-ner-english` | HuggingFace DLC |
| Sentiment Analysis | `cardiffnlp/twitter-roberta-base-sentiment` | HuggingFace DLC |
| Clinical NLP (HIPAA) | `emilyalsentzer/Bio_ClinicalBERT` | HuggingFace DLC |
| Document Summarization | `facebook/bart-large-cnn` | HuggingFace DLC |
| Question Answering (extractive) | `deepset/roberta-base-squad2` | HuggingFace DLC |
| Zero-shot Classification | `facebook/bart-large-mnli` | HuggingFace DLC |
| Multilingual | `xlm-roberta-base` | HuggingFace DLC |
| Embeddings (for search) | `sentence-transformers/all-mpnet-base-v2` | HuggingFace DLC |

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns endpoint config + trigger Lambda + Model Group + dataset read | **§3 Monolith Variant** |
| `MLPlatformStack` owns Model Group + SageMaker role, `ServingStack` owns endpoint, `NLPPipelineStack` owns trigger Lambda + endpoint config | **§4 Micro-Stack Variant** |

**Why the split matters.** The trigger Lambda needs `sagemaker:StartPipelineExecution` on the NLP pipeline (defined in `MLPlatformStack`). The endpoint config references a model name (published by the deployer Lambda at approval time). Monolith: L2 helpers work. Micro-stack: cross-stack `bucket.grant_*` on the dataset bucket would cycle — use identity-side on the SageMaker role and SSM-published ARNs. Endpoint KMS follows the fifth non-negotiable (ARN string only).

---

## 3. Monolith Variant

**Use when:** POC / single-stack.

### 3.1 Architecture

```
TRIGGER:
  EventBridge / API / Lambda → NLPPipelineTrigger
    └── sagemaker:StartPipelineExecution(task_type, base_model, dataset_uri)

PIPELINE (ml/pipelines/nlp_pipeline.py):
  1) PreprocessNLPData   (SKLearnProcessor → tokenize + label encode)
  2) FinetuneNLPModel    (HuggingFace DLC, Spot, CPU m5.4xlarge)
  3) EvaluateNLPModel    (F1, precision, recall, accuracy)
  4) F1QualityGate       (ConditionGreaterThanOrEqualTo on MinF1Score)
  5) RegisterNLPModel    (ModelPackageGroup, PendingManualApproval)

SERVING:
  CfnEndpointConfig (LEAST_OUTSTANDING_REQUESTS + ManagedInstanceScaling)
    → CfnEndpoint (created by MLOPS_SAGEMAKER_SERVING deployer on approval)
```

### 3.2 CDK — `_create_nlp_pipeline` method body

```python
from aws_cdk import (
    CfnOutput, Duration,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_sagemaker as sagemaker,
)


def _create_nlp_pipeline(self, stage_name: str) -> None:
    """
    NLP Pipeline using Hugging Face Transformers on SageMaker.

    Assumes self.{lake_buckets, kms_key} set.

    Tasks supported (set in Architecture Map):
      A) Text Classification  — categorize documents/tickets/emails
      B) Named Entity Recognition — extract entities (names, dates, orgs, drugs)
      C) Sentiment Analysis   — positive/negative/neutral scoring
      D) Summarization        — condense long documents
      E) Embeddings           — vector representations for semantic search

    Pipeline Steps:
      1. Data Validation + Preprocessing (tokenization, label encoding)
      2. Fine-tuning or zero-shot inference
      3. Evaluation (F1, precision, recall, confusion matrix)
      4. Model Registration
      5. Real-time endpoint deployment
    """

    # =========================================================================
    # NLP ENDPOINT — Real-time inference
    # =========================================================================

    # Multi-model endpoint config — host all NLP models on one instance
    nlp_endpoint_config = sagemaker.CfnEndpointConfig(
        self, "NLPEndpointConfig",
        endpoint_config_name=f"{{project_name}}-nlp-{stage_name}",
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="AllTraffic",
                model_name=f"{{project_name}}-nlp-model-{stage_name}",
                # CPU is fine for BERT-class models — no GPU needed for inference
                instance_type="ml.m5.xlarge" if stage_name != "prod" else "ml.m5.2xlarge",
                initial_instance_count=1,
                routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                    routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                ),
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    status="ENABLED",
                    min_instance_count=1,
                    max_instance_count=5 if stage_name == "prod" else 2,
                ),
            )
        ],
        kms_key_id=self.kms_key.key_arn,
    )

    # =========================================================================
    # NLP PIPELINE TRIGGER LAMBDA
    # =========================================================================

    nlp_pipeline_fn = _lambda.Function(
        self, "NLPPipelineTrigger",
        function_name=f"{{project_name}}-nlp-pipeline-trigger-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/nlp_pipeline_trigger"),
        environment={
            "PIPELINE_NAME":      f"{{project_name}}-nlp-pipeline-{stage_name}",
            "DEFAULT_TASK":       "text-classification",
            "DEFAULT_MODEL_ID":   "distilbert-base-uncased",
            "DEFAULT_DATASET":    f"s3://{self.lake_buckets['processed'].bucket_name}/nlp-data/",
        },
        timeout=Duration.seconds(30),
    )
    nlp_pipeline_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}-nlp-pipeline-{stage_name}"],
    ))

    CfnOutput(self, "NLPEndpointConfigName",
        value=nlp_endpoint_config.endpoint_config_name,
        export_name=f"{{project_name}}-nlp-endpoint-config-{stage_name}",
    )
    CfnOutput(self, "NLPPipelineTriggerArn",
        value=nlp_pipeline_fn.function_arn,
        export_name=f"{{project_name}}-nlp-pipeline-trigger-{stage_name}",
    )
```

### 3.3 Trigger handler (`lambda/nlp_pipeline_trigger/index.py`)

```python
"""Start NLP fine-tuning pipeline."""
import boto3, logging, os

logger = logging.getLogger()
logger.setLevel(logging.INFO)
sm = boto3.client('sagemaker')


def handler(event, context):
    params = [
        {"Name": "TaskType",    "Value": event.get("task_type",  os.environ["DEFAULT_TASK"])},
        {"Name": "BaseModelId", "Value": event.get("model_id",   os.environ["DEFAULT_MODEL_ID"])},
        {"Name": "DatasetS3Uri","Value": event.get("dataset_uri", os.environ["DEFAULT_DATASET"])},
        {"Name": "NumLabels",   "Value": str(event.get("num_labels", 2))},
        {"Name": "MaxEpochs",   "Value": str(event.get("epochs",    5))},
        {"Name": "MinF1Score",  "Value": str(event.get("min_f1",    0.80))},
    ]
    resp = sm.start_pipeline_execution(
        PipelineName=os.environ["PIPELINE_NAME"],
        PipelineParameters=params,
        ClientRequestToken=context.aws_request_id,
    )
    logger.info(f"Started NLP pipeline: {resp['PipelineExecutionArn']}")
    return {"execution_arn": resp["PipelineExecutionArn"]}
```

### 3.4 Pipeline definition (`ml/pipelines/nlp_pipeline.py`)

```python
# ml/pipelines/nlp_pipeline.py
from sagemaker.huggingface import HuggingFace, HuggingFaceModel
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep, ProcessingInput, ProcessingOutput
from sagemaker.workflow.parameters import ParameterString, ParameterInteger, ParameterFloat
from sagemaker.workflow.model_step import ModelStep
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.sklearn.processing import SKLearnProcessor
from sagemaker.inputs import TrainingInput


def create_nlp_pipeline(sm_session, role_arn, pipeline_name, s3_bucket, model_package_group):

    # === Parameters ===
    task_type     = ParameterString("TaskType", default_value="text-classification")
    base_model_id = ParameterString("BaseModelId", default_value="distilbert-base-uncased")
    dataset_uri   = ParameterString("DatasetS3Uri")
    num_labels    = ParameterInteger("NumLabels", default_value=2)
    max_epochs    = ParameterInteger("MaxEpochs", default_value=5)
    min_f1        = ParameterFloat("MinF1Score", default_value=0.80)
    approval      = ParameterString("ModelApprovalStatus", default_value="PendingManualApproval")

    # === Step 1: Preprocess ===
    preprocessor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type="ml.m5.xlarge", instance_count=1,
        sagemaker_session=sm_session,
    )
    preprocess_step = ProcessingStep(
        name="PreprocessNLPData",
        processor=preprocessor,
        code="ml/scripts/nlp_preprocess.py",
        inputs=[ProcessingInput(source=dataset_uri, destination="/opt/ml/processing/input")],
        outputs=[
            ProcessingOutput(output_name="train",     source="/opt/ml/processing/output/train"),
            ProcessingOutput(output_name="test",      source="/opt/ml/processing/output/test"),
            ProcessingOutput(output_name="label_map", source="/opt/ml/processing/output/labels"),
        ],
        job_arguments=["--task", task_type, "--num-labels", num_labels],
    )

    # === Step 2: Fine-tune ===
    # HuggingFace DLC (Deep Learning Container) handles all HF dependencies
    hf_estimator = HuggingFace(
        entry_point="ml/scripts/nlp_train.py",
        role=role_arn,
        # CPU instance for BERT-class — much cheaper than GPU, only 5% slower
        instance_type="ml.m5.4xlarge",
        instance_count=1,
        transformers_version="4.36.0",
        pytorch_version="2.1.0",
        py_version="py310",
        sagemaker_session=sm_session,
        use_spot_instances=True,
        max_wait=14400,
        max_run=7200,
        hyperparameters={
            "model_name_or_path": base_model_id,
            "task_name":          task_type,
            "num_labels":         num_labels,
            "num_train_epochs":   max_epochs,
            "per_device_train_batch_size": 32,
            "per_device_eval_batch_size":  64,
            "learning_rate":      2e-5,
            "weight_decay":       0.01,
            "warmup_steps":       500,
            "max_seq_length":     512,
            "output_dir":         "/opt/ml/model",
            "evaluation_strategy": "epoch",
            "save_best_model":    True,
            "fp16":               True,  # Mixed precision on CPU with recent PyTorch
        },
        metric_definitions=[
            {"Name": "eval_f1",        "Regex": "eval_f1=([0-9\\.]+)"},
            {"Name": "eval_accuracy",  "Regex": "eval_accuracy=([0-9\\.]+)"},
            {"Name": "eval_precision", "Regex": "eval_precision=([0-9\\.]+)"},
            {"Name": "eval_recall",    "Regex": "eval_recall=([0-9\\.]+)"},
            {"Name": "train_loss",     "Regex": "train_loss=([0-9\\.]+)"},
        ],
    )
    training_step = TrainingStep(
        name="FinetuneNLPModel",
        estimator=hf_estimator,
        inputs={
            "train": TrainingInput(preprocess_step.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri, content_type="application/x-parquet"),
            "test":  TrainingInput(preprocess_step.properties.ProcessingOutputConfig.Outputs["test"].S3Output.S3Uri,  content_type="application/x-parquet"),
        },
    )

    # === Step 3: Evaluation ===
    eval_processor = SKLearnProcessor(
        framework_version="1.2-1", role=role_arn,
        instance_type="ml.m5.xlarge", instance_count=1,
    )
    eval_step = ProcessingStep(
        name="EvaluateNLPModel",
        processor=eval_processor,
        code="ml/scripts/nlp_evaluate.py",
        inputs=[
            ProcessingInput(source=training_step.properties.ModelArtifacts.S3ModelArtifacts, destination="/opt/ml/processing/model"),
            ProcessingInput(source=preprocess_step.properties.ProcessingOutputConfig.Outputs["test"].S3Output.S3Uri, destination="/opt/ml/processing/test"),
        ],
        outputs=[ProcessingOutput(output_name="metrics", source="/opt/ml/processing/output")],
    )

    # === Step 4: Quality Gate → Register ===
    model_step = ModelStep(
        name="RegisterNLPModel",
        model_approval_status=approval,
        model_package_group_name=model_package_group,
    )
    quality_gate = ConditionStep(
        name="F1QualityGate",
        conditions=[ConditionGreaterThanOrEqualTo(
            left=eval_step.properties.ProcessingOutputConfig.Outputs["metrics"],
            right=min_f1,
        )],
        if_steps=[model_step],
        else_steps=[],
    )

    return Pipeline(
        name=pipeline_name,
        parameters=[task_type, base_model_id, dataset_uri, num_labels, max_epochs, min_f1, approval],
        steps=[preprocess_step, training_step, eval_step, quality_gate],
        sagemaker_session=sm_session,
    )
```

### 3.5 Training script (`ml/scripts/nlp_train.py`)

```python
# ml/scripts/nlp_train.py
import argparse, os
from datasets import load_from_disk
from transformers import (
    AutoTokenizer, AutoModelForSequenceClassification,
    TrainingArguments, Trainer, DataCollatorWithPadding
)
from sklearn.metrics import f1_score, accuracy_score, precision_score, recall_score
import numpy as np


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    return {
        "accuracy":  accuracy_score(labels, predictions),
        "f1":        f1_score(labels, predictions, average="weighted"),
        "precision": precision_score(labels, predictions, average="weighted"),
        "recall":    recall_score(labels, predictions, average="weighted"),
    }


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model_name_or_path", type=str)
    p.add_argument("--num_labels", type=int, default=2)
    p.add_argument("--num_train_epochs", type=int, default=5)
    p.add_argument("--per_device_train_batch_size", type=int, default=32)
    p.add_argument("--per_device_eval_batch_size",  type=int, default=64)
    p.add_argument("--learning_rate", type=float, default=2e-5)
    p.add_argument("--weight_decay", type=float, default=0.01)
    p.add_argument("--warmup_steps", type=int, default=500)
    p.add_argument("--max_seq_length", type=int, default=512)
    p.add_argument("--fp16", type=bool, default=True)
    p.add_argument("--output_dir", type=str, default="/opt/ml/model")
    return p.parse_args()


def main():
    args = parse_args()
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    model     = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path, num_labels=args.num_labels
    )

    train_dataset = load_from_disk("/opt/ml/input/data/train")
    eval_dataset  = load_from_disk("/opt/ml/input/data/test")

    def tokenize(examples):
        return tokenizer(examples["text"], truncation=True, max_length=args.max_seq_length)

    train_dataset = train_dataset.map(tokenize, batched=True)
    eval_dataset  = eval_dataset.map(tokenize, batched=True)

    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        warmup_steps=args.warmup_steps,
        fp16=args.fp16,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        report_to=["tensorboard"],
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        tokenizer=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=compute_metrics,
    )
    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)


if __name__ == "__main__":
    main()
```

### 3.6 Monolith gotchas

- **CPU inference beats GPU for BERT-class** — `ml.m5.2xlarge` serves ~50 QPS per instance and costs 1/5 of `ml.g4dn.xlarge`. Only switch to GPU if p99 latency > 200 ms under load.
- **`LEAST_OUTSTANDING_REQUESTS` routing** needs ≥ 2 instances to matter — at `initial_instance_count=1` the routing strategy is inert until scale-up.
- **`ManagedInstanceScaling` lag** — scale-up takes 3–5 minutes; pre-warm with `min_instance_count=2` in prod for burstable traffic.
- **`fp16=True` on CPU** is only meaningful with PyTorch 2.1+ (bf16 AVX-512 path). Older runtimes silently ignore it.
- **Tokenizer `max_length=512`** is BERT's hard ceiling. For longer documents: chunk + aggregate, or switch to `longformer-base-4096`.
- **`metric_for_best_model="f1"`** with `load_best_model_at_end=True` is the pair that saves the best checkpoint — missing either and you ship the last epoch, not the best.
- **Zero-shot paths** (`facebook/bart-large-mnli`) skip training entirely; the pipeline becomes preprocess → (skip) → register. Gate on a labeled test set accuracy instead of F1 against training data.

---

## 4. Micro-Stack Variant

**Use when:** `NLPPipelineStack` is separate from `MLPlatformStack` (owns Model Group + SageMaker role) and `DataLakeStack` (owns dataset bucket + KMS).

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)` via `_LAMBDAS_ROOT`.
2. **Never call `bucket.grant_*`** on the dataset bucket across stacks — identity-side `s3:GetObject` on the SageMaker role (managed in `MLPlatformStack`).
3. **Never target cross-stack queues** — trigger is invoked via API/event; no SQS.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_master_key=ext_key`** — endpoint config takes KMS **ARN string** (`kms_key_id=`), not a construct.

### 4.2 `NLPPipelineStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sagemaker as sagemaker,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class NLPPipelineStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        pipeline_name_ssm: str,                 # /proj/ml/nlp_pipeline_name
        dataset_bucket_name_ssm: str,           # /proj/lake/processed_bucket
        lake_key_arn_ssm: str,                  # /proj/lake/kms_key_arn
        model_name_ssm: str,                    # /proj/ml/nlp_model_name (published by deployer)
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-nlp-pipeline-{stage_name}", **kwargs)

        pipeline_name  = ssm.StringParameter.value_for_string_parameter(self, pipeline_name_ssm)
        dataset_bucket = ssm.StringParameter.value_for_string_parameter(self, dataset_bucket_name_ssm)
        lake_key_arn   = ssm.StringParameter.value_for_string_parameter(self, lake_key_arn_ssm)
        model_name     = ssm.StringParameter.value_for_string_parameter(self, model_name_ssm)

        # Endpoint config — KMS as STRING (fifth non-negotiable)
        endpoint_config = sagemaker.CfnEndpointConfig(
            self, "NLPEndpointConfig",
            endpoint_config_name=f"{{project_name}}-nlp-{stage_name}",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=model_name,
                    instance_type="ml.m5.xlarge" if stage_name != "prod" else "ml.m5.2xlarge",
                    initial_instance_count=1,
                    routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                        routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                    ),
                    managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                        status="ENABLED",
                        min_instance_count=1,
                        max_instance_count=5 if stage_name == "prod" else 2,
                    ),
                )
            ],
            kms_key_id=lake_key_arn,     # STRING — not construct
        )

        # Trigger Lambda
        log_group = logs.LogGroup(self, "TriggerLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-nlp-pipeline-trigger-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        trigger_fn = _lambda.Function(
            self, "NLPPipelineTriggerFn",
            function_name=f"{{project_name}}-nlp-pipeline-trigger-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "nlp_pipeline_trigger")),
            timeout=Duration.seconds(30),
            log_group=log_group,
            environment={
                "PIPELINE_NAME":    pipeline_name,
                "DEFAULT_TASK":     "text-classification",
                "DEFAULT_MODEL_ID": "distilbert-base-uncased",
                "DEFAULT_DATASET":  f"s3://{dataset_bucket}/nlp-data/",
            },
        )
        trigger_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:StartPipelineExecution", "sagemaker:DescribePipelineExecution"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:pipeline/{pipeline_name}"],
        ))
        iam.PermissionsBoundary.of(trigger_fn.role).apply(permission_boundary)

        cdk.CfnOutput(self, "NLPEndpointConfigName",
            value=endpoint_config.endpoint_config_name,
            export_name=f"{{project_name}}-nlp-endpoint-config-{stage_name}",
        )
        cdk.CfnOutput(self, "NLPPipelineTriggerArn",
            value=trigger_fn.function_arn,
            export_name=f"{{project_name}}-nlp-pipeline-trigger-{stage_name}",
        )
```

### 4.3 Micro-stack gotchas

- **`model_name` via SSM** — the deployer Lambda publishes the model name after a package approval (see `MLOPS_SAGEMAKER_SERVING`). The endpoint config reads it at deploy time; if the SSM param doesn't exist yet, `cdk synth` fails.
- **KMS ARN string in `kms_key_id=`** is the fifth non-negotiable; resist the temptation to import with `kms.Key.from_key_arn(...)` and pass the construct.
- **Endpoint resource is NOT in this stack** — `CfnEndpointConfig` lives here; `CfnEndpoint` is created by the deployer Lambda on approval. This keeps the config versioned without a redeploy loop.
- **Scoped pipeline ARN** — `sagemaker:StartPipelineExecution` resources must be the exact pipeline ARN; wildcards fail AWS IAM policy validation on SageMaker pipelines.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx | §4 Micro-Stack |
| Switch task (classification → NER) | Change `TaskType` parameter + preprocess script; base model typically changes too |
| Long-document NLP (> 512 tokens) | Swap base model to `longformer-base-4096`; raise `max_seq_length` |
| GPU inference for real-time latency (< 50 ms) | Change `instance_type` to `ml.g4dn.xlarge`; rebuild endpoint config |
| Multi-language | Swap base model to `xlm-roberta-base` |
| Zero-shot (no training data) | Skip training step; use `facebook/bart-large-mnli` directly at inference |

---

## 6. Worked example — NLPPipelineStack synthesizes

Save as `tests/sop/test_MLOPS_PIPELINE_NLP_HUGGINGFACE.py`. Offline.

```python
"""SOP verification — NLPPipelineStack synthesizes endpoint config + trigger Lambda."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_nlp_pipeline_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.nlp_pipeline_stack import NLPPipelineStack
    stack = NLPPipelineStack(
        app, stage_name="prod",
        pipeline_name_ssm="/test/ml/nlp_pipeline_name",
        dataset_bucket_name_ssm="/test/lake/processed_bucket",
        lake_key_arn_ssm="/test/lake/kms_key_arn",
        model_name_ssm="/test/ml/nlp_model_name",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function",           1)
    t.resource_count_is("AWS::SageMaker::EndpointConfig",  1)
    t.resource_count_is("AWS::Logs::LogGroup",             1)
```

---

## 7. References

- `docs/template_params.md` — `NLP_PIPELINE_NAME_SSM`, `NLP_MODEL_NAME_SSM`, `NLP_DEFAULT_TASK`, `NLP_DEFAULT_MODEL_ID`, `NLP_MIN_F1`
- `docs/Feature_Roadmap.md` — feature IDs `ML-45` (NLP pipeline), `ML-46` (HuggingFace DLC), `ML-47` (managed endpoint autoscaling)
- Hugging Face on SageMaker: https://huggingface.co/docs/sagemaker/index
- SageMaker endpoint `LEAST_OUTSTANDING_REQUESTS`: https://docs.aws.amazon.com/sagemaker/latest/dg/endpoint-production-variants.html
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (Model Group + Studio), `MLOPS_SAGEMAKER_SERVING` (deployer + endpoint creation), `MLOPS_PIPELINE_LLM_FINETUNING` (LLM alternative), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `NLPPipelineStack` reads pipeline name + dataset bucket + KMS ARN + model name via SSM; identity-side `sagemaker:StartPipelineExecution` scoped to the pipeline ARN; `CfnEndpointConfig.kms_key_id` takes the ARN string (fifth non-negotiable). Extracted inline trigger Lambda to `lambda/nlp_pipeline_trigger/` asset. Kept the HF training pipeline + training script + task→model table. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — NLP task table, endpoint config with autoscaling, HF pipeline + training script. |
