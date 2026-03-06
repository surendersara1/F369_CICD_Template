# PARTIAL: NLP Pipeline — Hugging Face Transformers on SageMaker

**Usage:** Include when SOW mentions text classification, NER, sentiment analysis, document parsing, summarization, question answering, or entity extraction.

---

## NLP Task → Model Selection Guide

| NLP Task                          | Recommended Model                                                 | SageMaker Container |
| --------------------------------- | ----------------------------------------------------------------- | ------------------- |
| Text Classification (multi-class) | `distilbert-base-uncased` / `roberta-base`                        | HuggingFace DLC     |
| Named Entity Recognition (NER)    | `dslim/bert-base-NER` / `Jean-Baptiste/roberta-large-ner-english` | HuggingFace DLC     |
| Sentiment Analysis                | `cardiffnlp/twitter-roberta-base-sentiment`                       | HuggingFace DLC     |
| Clinical NLP (HIPAA)              | `emilyalsentzer/Bio_ClinicalBERT`                                 | HuggingFace DLC     |
| Document Summarization            | `facebook/bart-large-cnn`                                         | HuggingFace DLC     |
| Question Answering (extractive)   | `deepset/roberta-base-squad2`                                     | HuggingFace DLC     |
| Zero-shot Classification          | `facebook/bart-large-mnli`                                        | HuggingFace DLC     |
| Multilingual                      | `xlm-roberta-base`                                                | HuggingFace DLC     |
| Embeddings (for search)           | `sentence-transformers/all-mpnet-base-v2`                         | HuggingFace DLC     |

---

## CDK Code Block — NLP Pipeline Infrastructure

```python
def _create_nlp_pipeline(self, stage_name: str) -> None:
    """
    NLP Pipeline using Hugging Face Transformers on SageMaker.

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
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
sm = boto3.client('sagemaker')

def handler(event, context):
    params = [
        {"Name": "TaskType", "Value": event.get("task_type", os.environ["DEFAULT_TASK"])},
        {"Name": "BaseModelId", "Value": event.get("model_id", os.environ["DEFAULT_MODEL_ID"])},
        {"Name": "DatasetS3Uri", "Value": event.get("dataset_uri", os.environ["DEFAULT_DATASET"])},
        {"Name": "NumLabels", "Value": str(event.get("num_labels", 2))},
        {"Name": "MaxEpochs", "Value": str(event.get("epochs", 5))},
        {"Name": "MinF1Score", "Value": str(event.get("min_f1", 0.80))},
    ]
    resp = sm.start_pipeline_execution(
        PipelineName=os.environ["PIPELINE_NAME"],
        PipelineParameters=params,
        ClientRequestToken=context.aws_request_id,
    )
    return {"execution_arn": resp["PipelineExecutionArn"]}
"""),
        environment={
            "PIPELINE_NAME": f"{{project_name}}-nlp-pipeline-{stage_name}",
            "DEFAULT_TASK": "text-classification",
            "DEFAULT_MODEL_ID": "distilbert-base-uncased",
            "DEFAULT_DATASET": f"s3://{self.lake_buckets['processed'].bucket_name}/nlp-data/",
        },
        timeout=Duration.seconds(30),
    )
    nlp_pipeline_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:StartPipelineExecution"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:pipeline/{{project_name}}-nlp-pipeline-{stage_name}"],
    ))
```

---

## SageMaker Pipeline Code (`ml/pipelines/nlp_pipeline.py`)

```python
# ml/pipelines/nlp_pipeline.py
from sagemaker.huggingface import HuggingFace, HuggingFaceModel
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.workflow.parameters import ParameterString, ParameterInteger, ParameterFloat
from sagemaker.workflow.model_step import ModelStep
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.sklearn.processing import SKLearnProcessor

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
            ProcessingOutput(output_name="train", source="/opt/ml/processing/output/train"),
            ProcessingOutput(output_name="test",  source="/opt/ml/processing/output/test"),
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
            "train": TrainingInput(preprocess_step.properties...train_uri, content_type="application/x-parquet"),
            "test":  TrainingInput(preprocess_step.properties...test_uri,  content_type="application/x-parquet"),
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
            ProcessingInput(source=preprocess_step.properties...test_uri, destination="/opt/ml/processing/test"),
        ],
        outputs=[ProcessingOutput(output_name="metrics", source="/opt/ml/processing/output")],
    )

    # === Step 4: Quality Gate → Register ===
    model_step   = ModelStep(name="RegisterNLPModel", model_approval_status=approval,
                             model_package_group_name=model_package_group, ...)
    quality_gate = ConditionStep(
        name="F1QualityGate",
        conditions=[ConditionGreaterThanOrEqualTo(left=..., right=min_f1)],
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

---

## Training Script Skeleton (`ml/scripts/nlp_train.py`)

```python
# ml/scripts/nlp_train.py
import argparse, os
from datasets import load_from_disk
from transformers import (
    AutoTokenizer, AutoModelForSequenceClassification,
    TrainingArguments, Trainer, DataCollatorWithPadding
)
from sklearn.metrics import f1_score, accuracy_score
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
```
