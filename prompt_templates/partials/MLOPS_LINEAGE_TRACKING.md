# SOP — SageMaker ML Lineage Tracking (artifact graph · audit trail · cross-pipeline traceability)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker ML Lineage API (`sagemaker.lineage`) · Lineage entities (Artifact, Action, Context, Association, TrialComponent) · Model Card integration · automated lineage capture from Pipelines · query-by-artifact + query-by-context

---

## 1. Purpose

- Codify the **ML Lineage pattern** that answers regulator/auditor questions like:
  - "Which dataset version produced this model?"
  - "Which training run created this endpoint?"
  - "Which downstream models depend on this dataset?"
  - "Show me all artifacts that touched this Model Package."
- Codify the **automatic vs manual lineage capture** patterns — Pipelines auto-capture; standalone training jobs need manual `ArtifactSummary` calls.
- Codify the **Model Card** integration — the human-readable companion to the lineage graph.
- Provide a **query Lambda** that walks the lineage graph for compliance reports.
- This is the **lineage / traceability specialisation**. Mentioned in `MLOPS_SAGEMAKER_TRAINING` but not deep-dived; this partial is the deep-dive.

When the SOW signals: "regulatory traceability", "model audit trail", "GDPR data subject request — which model was trained on user X data", "lineage from raw S3 → model → endpoint", "Model Cards for governance".

---

## 2. Decision tree

```
What needs lineage?
├── Production model in regulated industry → §3 Full lineage (Artifacts + Actions + Contexts) + Model Card
├── Internal experiments only → §4 Auto-capture from Pipelines, no manual calls
├── Quick POC / prototype → skip lineage; revisit when productionizing
└── Vendor-supplied black-box model → Model Card only (no upstream artifacts)

Capture mode?
├── SageMaker Pipelines (recommended) → §3.2 auto-capture
├── Standalone training jobs → §3.3 manual capture in training script
├── Cross-account / cross-pipeline → §3.4 explicit ContextLink + AssociationCreate
└── Bedrock-fine-tuned models → §5 Bedrock + lineage bridge
```

---

## 3. Full lineage variant (Pipelines + Model Card)

### 3.1 Architecture

```
   S3 raw data ──► Glue ETL job ──► S3 curated ──► Pipeline data prep step ──►
                                                              │
                                                              ▼ (Artifact: input)
   Pipeline training step ──► Model artifact (Artifact) ──► Model Package (Action: register)
                                              │
                                              ▼ (Approval — Action: approve)
   Endpoint deployment (Action: deploy) ──► Endpoint (Context)

   ┌─────────────────────────────────────────────────────────────┐
   │  Lineage entity types:                                       │
   │     - Artifact: dataset, model.tar.gz, image, image-config    │
   │     - Action:   training-job, processing-job, register, deploy│
   │     - Context:  pipeline, endpoint, model-package-group       │
   │     - Association: connects entities (PRODUCED, CONSUMED, etc)│
   │     - TrialComponent: per-step run details                    │
   └─────────────────────────────────────────────────────────────┘
```

### 3.2 Auto-capture from Pipelines (zero code change)

SageMaker Pipelines auto-create lineage entities when `EnableSageMakerMetricsTimeSeries=True` (default). All pipeline steps become Actions; all step inputs/outputs become Artifacts.

```python
# Pipeline DSL — lineage is automatic; just enable in pipeline config
pipeline = Pipeline(
    name="qra-llm-finetune-prod",
    parameters=[...],
    steps=[data_prep, training, eval, register],
    sagemaker_session=session,
    pipeline_definition_config=PipelineDefinitionConfig(
        use_custom_job_prefix=True,
    ),
)
```

After execution:
```python
# Query lineage graph
import boto3
sm = boto3.client("sagemaker")

# Get all Actions for a pipeline execution
actions = sm.list_actions(
    SourceUri=f"arn:aws:sagemaker:us-east-1:111111111111:pipeline/qra-llm-finetune-prod/execution/abc123",
)
# Get artifacts produced
artifacts = sm.list_artifacts(
    SourceUri="s3://qra-models/llama3-70b-tuned/v1/model.tar.gz",
)
# Walk associations
associations = sm.list_associations(
    SourceArn=actions["ActionSummaries"][0]["ActionArn"],
)
```

### 3.3 Manual capture (for standalone training scripts)

When training runs outside Pipelines (e.g. EC2, HyperPod), manually create lineage:

```python
# scripts/log_lineage.py — call after training completes
import boto3, os
sm = boto3.client("sagemaker")

def log_training_lineage(run_id, dataset_uri, model_uri, hyperparameters):
    """Creates Artifact entities + Action linking them."""

    # 1. Dataset artifact (or look up if exists)
    dataset_artifact = sm.create_artifact(
        ArtifactName=f"dataset-{run_id}",
        Source={
            "SourceUri":   dataset_uri,
            "SourceTypes": [{"Value": "s3", "SourceIdType": "S3URI"}],
        },
        ArtifactType="DataSet",
        Properties={
            "row_count": "1000000",
            "version":   "v3",
            "schema_hash": "sha256:abc...",
        },
    )

    # 2. Training Action
    training_action = sm.create_action(
        ActionName=f"train-{run_id}",
        Source={"SourceUri": f"file://{run_id}", "SourceType": "Custom"},
        ActionType="ModelTraining",
        Status="Completed",
        Properties={
            "instance_type": "ml.p4d.24xlarge",
            **{k: str(v) for k, v in hyperparameters.items()},
        },
    )

    # 3. Model artifact
    model_artifact = sm.create_artifact(
        ArtifactName=f"model-{run_id}",
        Source={"SourceUri": model_uri, "SourceTypes": [{"Value": "s3"}]},
        ArtifactType="Model",
    )

    # 4. Associations: dataset → training, training → model
    sm.add_association(
        SourceArn=dataset_artifact["ArtifactArn"],
        DestinationArn=training_action["ActionArn"],
        AssociationType="ContributedTo",
    )
    sm.add_association(
        SourceArn=training_action["ActionArn"],
        DestinationArn=model_artifact["ArtifactArn"],
        AssociationType="Produced",
    )
    return {
        "dataset_arn":  dataset_artifact["ArtifactArn"],
        "training_arn": training_action["ActionArn"],
        "model_arn":    model_artifact["ArtifactArn"],
    }
```

### 3.4 Model Card — human-readable governance doc

```python
sm.create_model_card(
    ModelCardName=f"llama3-70b-tuned-v1",
    Content=json.dumps({
        "model_overview": {
            "model_name":         "Llama 3 70B Tuned for Customer Support",
            "model_version":      "1.0",
            "model_description":  "PEFT-LoRA fine-tune of Llama 3 70B on internal support tickets",
            "intended_uses":      "Drafting first-response replies to customer support tickets",
            "out_of_scope_uses":  "Final response without human review; legal/medical advice",
            "owner":              "ml-platform-team@example.com",
        },
        "intended_uses": {
            "primary_uses":       ["Draft customer support replies"],
            "primary_users":      ["Customer support agents"],
            "out_of_scope_uses":  ["Direct customer-facing without review"],
        },
        "training_details": {
            "training_data_uri":  "s3://qra-curated/support-tickets/v3/",
            "training_runs": [
                {"run_id": "lora-llama3-70b-2026-04-01", "epochs": 3, "lora_rank": 16},
            ],
            "objective_function": "instruction-tuning loss",
        },
        "evaluation_details": {
            "evaluation_metrics": [
                {"name": "BLEU",      "value": 0.65},
                {"name": "ROUGE-L",   "value": 0.72},
                {"name": "Toxicity",  "value": 0.02},
            ],
            "evaluation_data_uri": "s3://qra-curated/support-tickets/holdout/",
            "datasets_used":       ["held-out 10K support tickets"],
        },
        "ethical_considerations": {
            "sensitive_data":     "PII in tickets is masked via Macie pre-redaction",
            "potential_bias":     "Training data skewed to enterprise customers; small-business tickets under-represented",
            "mitigation":         "Augment dataset; add model monitor for distribution drift",
        },
        "additional_information": {
            "approval_status":  "Approved",
            "approver":         "vp-engineering@example.com",
            "model_package_arn": "arn:aws:sagemaker:us-east-1:111111111111:model-package/qra-llm-mpg/2",
        },
    }),
    ModelCardStatus="Approved",
    SecurityConfig={"KmsKeyId": os.environ["KMS_KEY_ARN"]},
)
```

---

## 4. Compliance query Lambda (for audit responses)

```python
"""lineage_query/index.py — answers 'which models came from dataset X?' queries."""
import boto3
sm = boto3.client("sagemaker")


def downstream_models(dataset_s3_uri):
    """Returns all model artifacts derived from this dataset."""

    # 1. Find the dataset artifact
    artifacts = sm.list_artifacts(SourceUri=dataset_s3_uri)
    if not artifacts["ArtifactSummaries"]:
        return []
    dataset_arn = artifacts["ArtifactSummaries"][0]["ArtifactArn"]

    # 2. Walk forward: dataset → training actions
    associations = sm.list_associations(
        SourceArn=dataset_arn,
        AssociationType="ContributedTo",
    )

    models = []
    for assoc in associations["AssociationSummaries"]:
        training_arn = assoc["DestinationArn"]
        # 3. Walk training → model
        produces = sm.list_associations(
            SourceArn=training_arn,
            AssociationType="Produced",
        )
        for p in produces["AssociationSummaries"]:
            if "model" in p["DestinationArn"].lower():
                models.append(p["DestinationArn"])

    return models


def upstream_data(model_arn):
    """GDPR-style: which data trained this model?"""
    # Walk backwards from model → training → dataset
    backward = sm.list_associations(
        DestinationArn=model_arn,
        AssociationType="Produced",
    )
    datasets = []
    for assoc in backward["AssociationSummaries"]:
        training_arn = assoc["SourceArn"]
        datasets_for_training = sm.list_associations(
            DestinationArn=training_arn,
            AssociationType="ContributedTo",
        )
        for d in datasets_for_training["AssociationSummaries"]:
            datasets.append(d["SourceArn"])
    return datasets
```

---

## 5. Five non-negotiables

1. **Pipelines + auto-capture is the default.** Manual capture is only for legacy/standalone jobs. Move to Pipelines for all new training.
2. **Model Card per registered model.** Auditors want human-readable docs. The lineage graph is the proof; the Card is the explainer.
3. **Tag artifacts with `data_subject_id` for GDPR/CCPA.** Without it, "delete all my data" requests can't be honored programmatically.
4. **Lineage queries hit `list_associations`, not `list_artifacts`.** Walking the graph requires the association API; listing artifacts gives you nodes without edges.
5. **Retain lineage indefinitely.** Lineage entities are tiny (~KB each) and free; deleting them breaks audit trails. Default: never delete.

---

## 6. References

- AWS docs:
  - [ML Lineage Tracking](https://docs.aws.amazon.com/sagemaker/latest/dg/lineage-tracking.html)
  - [Model Cards](https://docs.aws.amazon.com/sagemaker/latest/dg/model-cards.html)
  - [Lineage entities + associations](https://docs.aws.amazon.com/sagemaker/latest/dg/lineage-tracking-entities.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — pipelines that auto-create lineage
  - `MLOPS_LLM_FINETUNING_PROD` — registers model packages w/ lineage
  - `MLOPS_CROSS_ACCOUNT_DEPLOY` — lineage crossing account boundaries

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — auto-capture from Pipelines + manual capture pattern + Model Card integration + compliance query Lambda. Created Wave 7 (2026-04-26). |
