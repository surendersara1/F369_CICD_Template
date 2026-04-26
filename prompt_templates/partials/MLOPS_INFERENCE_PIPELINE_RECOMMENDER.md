# SOP — Multi-container inference pipelines + Inference Recommender (right-sizing)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker multi-container endpoints (sequential/inference-pipeline + direct-invoke modes) · Inference Recommender (advisory + load test) · ProductionVariant + ProductionVariantContainer · per-container EnvironmentVariables and ContainerHostname · CloudWatch metrics for cost-per-1000-inferences

---

## 1. Purpose

- Codify the **inference pipeline pattern** — a single endpoint with 2-15 containers in sequence (e.g. preprocess → model → postprocess) and direct-invoke mode (caller targets a specific container).
- Codify the **Inference Recommender pattern** — automate the process of finding the cheapest instance type that meets latency SLA. Replaces the "guess and load-test" approach.
- Provide the **decision tree** between inference pipeline (1 endpoint) vs separate endpoints (1 per stage) vs Step Functions chaining (orchestration).
- Provide the **cost-per-1000-inferences** metric that lets you make pricing decisions across instance types.
- This is the **inference-shape-optimization specialisation**. `MLOPS_SAGEMAKER_SERVING` covers single-container endpoints; this partial covers multi-container + automated right-sizing.

When the SOW signals: "preprocess and postprocess in same endpoint", "tokenize → model → detokenize", "we need to chain containers", "we don't know the right instance type", "find cheapest endpoint that meets P99 latency SLA".

---

## 2. Decision tree

```
Pipeline shape?
├── 1 model, no pre/post → MLOPS_SAGEMAKER_SERVING (single-container)
├── Preprocess → model → postprocess (each ≤ 60 sec) → §3 INFERENCE PIPELINE (sequential mode)
├── 5-15 stages, complex orchestration → §4 STEP FUNCTIONS (multiple endpoints)
└── Multi-tenant routing (caller picks container) → §5 DIRECT-INVOKE multi-container

Right-sizing?
├── Know the right instance type → skip Inference Recommender
├── Multiple options to compare → §6 Inference Recommender (Default Job)
├── Custom load test profile → §6 Inference Recommender (Advanced Job)
└── Need recommendations integrated with CI/CD → §6 Inference Recommender + Pipeline step

Latency SLA?
├── < 100 ms p99 → instance must hold model in GPU memory; no cold starts
├── < 1 sec p99 → real-time endpoint w/ keepwarm
├── < 60 sec p99 → real-time endpoint sufficient
└── > 60 sec → use async (MLOPS_ASYNC_INFERENCE)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — pipeline endpoint + Inference Recommender job in one stack | **§3 / §6 Monolith Variant** |
| `ServingStack` owns endpoint; `RightSizingStack` owns Recommender job + result aggregation | **§7 Micro-Stack Variant** |

---

## 3. Inference Pipeline variant — sequential containers (preprocess → model → postprocess)

### 3.1 Architecture

```
   Caller request → SageMaker endpoint
                  │
                  ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Endpoint: ml-inference-pipeline                                  │
   │  Instance: ml.g5.2xlarge (single instance hosts ALL 3 containers) │
   │                                                                    │
   │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐        │
   │  │ Container 1  │ →  │ Container 2  │ →  │ Container 3  │        │
   │  │ "preprocess" │    │ "model"      │    │ "postprocess"│        │
   │  └──────────────┘    └──────────────┘    └──────────────┘        │
   │         │                    │                    │              │
   │  Inputs/outputs flow through /tmp filesystem within container     │
   │  Caller sees only: invoke → final response                        │
   └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_inference_pipeline_endpoint()`

```python
from aws_cdk import (
    aws_iam as iam,
    aws_sagemaker as sagemaker,
)


def _create_inference_pipeline_endpoint(self, stage: str) -> None:
    """Monolith. 3-container pipeline endpoint."""

    # A) IAM execution role
    self.endpoint_role = iam.Role(self, "PipelineRole",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.input_bucket.grant_read_write(self.endpoint_role)
    self.kms_key.grant_encrypt_decrypt(self.endpoint_role)

    # B) Model with multiple containers — SAGEMAKER PIPELINE MODE
    self.pipeline_model = sagemaker.CfnModel(self, "PipelineModel",
        model_name=f"{{project_name}}-pipeline-model-{stage}",
        execution_role_arn=self.endpoint_role.role_arn,
        # ────── CONTAINERS list (sequential!) ──────
        containers=[
            # 1) Preprocess (sklearn container with custom code)
            sagemaker.CfnModel.ContainerDefinitionProperty(
                container_hostname="preprocess",
                image=f"683313688378.dkr.ecr.{self.region}.amazonaws.com/sagemaker-scikit-learn:1.4-1",
                model_data_url=f"s3://{self.artifacts_bucket.bucket_name}/models/preprocess/model.tar.gz",
                environment={
                    "SAGEMAKER_PROGRAM":         "preprocess.py",
                    "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/code",
                    "SAGEMAKER_REGION":          self.region,
                },
                inference_specification_name="preprocess",
            ),
            # 2) Main model (HF / PyTorch container)
            sagemaker.CfnModel.ContainerDefinitionProperty(
                container_hostname="model",
                image=f"763104351884.dkr.ecr.{self.region}.amazonaws.com/huggingface-pytorch-inference:2.4.0-transformers4.45.0-gpu-py311-cu124",
                model_data_url=f"s3://{self.artifacts_bucket.bucket_name}/models/llama-3-8b-tuned/model.tar.gz",
                environment={
                    "HF_TASK":                   "text-generation",
                    "SAGEMAKER_MODEL_SERVER_TIMEOUT": "60",
                },
            ),
            # 3) Postprocess (sklearn container)
            sagemaker.CfnModel.ContainerDefinitionProperty(
                container_hostname="postprocess",
                image=f"683313688378.dkr.ecr.{self.region}.amazonaws.com/sagemaker-scikit-learn:1.4-1",
                model_data_url=f"s3://{self.artifacts_bucket.bucket_name}/models/postprocess/model.tar.gz",
                environment={
                    "SAGEMAKER_PROGRAM":         "postprocess.py",
                    "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/code",
                },
            ),
        ],
        # ────── INFERENCE EXECUTION CONFIG ──────
        # Mode: Serial (default for multi-container) — runs in sequence
        inference_execution_config=sagemaker.CfnModel.InferenceExecutionConfigProperty(
            mode="Serial",                                          # Direct = caller picks; Serial = sequence
        ),
    )

    # C) Endpoint config + endpoint
    self.pipeline_ep_config = sagemaker.CfnEndpointConfig(self, "PipelineEpConfig",
        endpoint_config_name=f"{{project_name}}-pipeline-ep-config-{stage}",
        production_variants=[sagemaker.CfnEndpointConfig.ProductionVariantProperty(
            variant_name="AllTraffic",
            model_name=self.pipeline_model.model_name,
            initial_instance_count=2,
            instance_type="ml.g5.2xlarge",                          # GPU for model container
            initial_variant_weight=1.0,
        )],
        kms_key_id=self.kms_key.key_arn,
    )
    self.pipeline_endpoint = sagemaker.CfnEndpoint(self, "PipelineEp",
        endpoint_name=f"{{project_name}}-pipeline-ep-{stage}",
        endpoint_config_name=self.pipeline_ep_config.endpoint_config_name,
    )
```

### 3.3 Direct-invoke mode — caller picks container

When pipeline mode is "Direct" (not "Serial"), caller specifies which container to invoke:

```python
inference_execution_config=sagemaker.CfnModel.InferenceExecutionConfigProperty(
    mode="Direct",                                              # caller picks
),
```

Invocation:

```python
sm_runtime.invoke_endpoint(
    EndpointName="my-multi-container-ep",
    TargetContainerHostname="preprocess",                       # picks the container
    Body=json.dumps({"input": data}),
    ContentType="application/json",
)
```

Use cases:
- Multi-stage debug ("invoke just preprocess to see intermediate output")
- Multi-tenant: each tenant has its own container with custom logic

---

## 4. Step Functions alternative (orchestration across endpoints)

For 5+ stages or conditional flows, prefer Step Functions over inference pipelines:

```python
sfn_workflow = sfn.StateMachine(self, "InferenceFlow",
    definition=(
        sfn_tasks.SageMakerInvokeEndpoint(self, "Preprocess",
            endpoint_name=preprocess_endpoint_name)
        .next(sfn_tasks.SageMakerInvokeEndpoint(self, "MainModel",
            endpoint_name=model_endpoint_name))
        .next(sfn_tasks.SageMakerInvokeEndpoint(self, "Postprocess",
            endpoint_name=postprocess_endpoint_name))
    ),
)
```

Trade-off: more flexible orchestration, but each invoke is a separate network hop (latency × 3).

---

## 5. Inference Recommender variant — automated right-sizing

### 5.1 What it does

Inference Recommender runs your model against:
- Pre-defined load profiles (5 RPS, 50 RPS, 500 RPS)
- Multiple instance types (ml.c5.xlarge, ml.g5.2xlarge, ml.p4d.24xlarge, etc.)

And reports: cost-per-1000-inferences, p99 latency, throughput per instance.

### 5.2 CDK + boto3 trigger

```python
# Trigger Lambda — runs `create_inference_recommendations_job` after model registry update
import boto3

sm = boto3.client("sagemaker")


def trigger_default_recommender_job(model_package_arn):
    """Default job: tests the standard catalog of instances at 5/10/50/100 RPS profiles."""
    response = sm.create_inference_recommendations_job(
        JobName=f"recommend-{int(time.time())}",
        JobType="Default",
        RoleArn=os.environ["RECOMMENDER_ROLE_ARN"],
        InputConfig={
            "ContainerConfig": {
                "Domain":          "MACHINE_LEARNING",
                "Task":            "TEXT_GENERATION",
                "Framework":       "PYTORCH",
                "PayloadConfig": {
                    "SamplePayloadUrl": "s3://qra-recommender-payloads/llm-sample-payloads/",
                    "SupportedContentTypes": ["application/json"],
                },
                "SupportedInstanceTypes": [
                    "ml.g5.xlarge", "ml.g5.2xlarge", "ml.g5.4xlarge",
                    "ml.g5.12xlarge", "ml.p4d.24xlarge",
                ],
            },
            "ModelPackageVersionArn": model_package_arn,
            "TrafficPattern": {
                "TrafficType":  "PHASES",
                "Phases": [
                    {"InitialNumberOfUsers": 1, "SpawnRate": 1, "DurationInSeconds": 120},
                    {"InitialNumberOfUsers": 5, "SpawnRate": 1, "DurationInSeconds": 120},
                    {"InitialNumberOfUsers": 20, "SpawnRate": 2, "DurationInSeconds": 120},
                    {"InitialNumberOfUsers": 50, "SpawnRate": 5, "DurationInSeconds": 120},
                ],
            },
        },
        StoppingConditions={
            "MaxInvocations":        10000,
            "MaxRuntimeInSeconds":   3600,
            "ModelLatencyThresholds": [{
                "Percentile": "P99",
                "ValueInMilliseconds": 500,                         # SLA: P99 < 500ms
            }],
        },
        OutputConfig={
            "KmsKeyId": os.environ["KMS_KEY_ARN"],
            "CompiledOutputConfig": {
                "S3OutputUri": f"s3://{os.environ['RESULTS_BUCKET']}/recommendations/",
            },
        },
    )
    return response["JobArn"]
```

### 5.3 Result interpretation

After job completes (~30-60 min):

```python
result = sm.describe_inference_recommendations_job(JobName="recommend-1719252000")

# Top 3 recommendations by cost-per-1000-inferences
for rec in result["InferenceRecommendations"][:3]:
    print(f"Instance: {rec['EndpointConfiguration']['InstanceType']}")
    print(f"  Cost/inference: ${rec['Metrics']['CostPerInference']}")
    print(f"  Cost/hour:      ${rec['Metrics']['CostPerHour']}")
    print(f"  P99 latency:    {rec['Metrics']['ModelLatencyP99']} ms")
    print(f"  Max RPS:        {rec['Metrics']['MaxInvocations']}")
    print(f"  CPU utilization:{rec['Metrics']['CpuUtilization']}%")
    print(f"  GPU utilization:{rec['Metrics']['GpuUtilization']}%")
```

### 5.4 Integration with CI/CD pipeline

Add a step to the model-registration pipeline that auto-runs Inference Recommender on every model approval:

```python
# In SageMaker Pipeline
recommender_step = ProcessingStep(
    name="InferenceRecommender",
    processor=PythonProcessor(...),
    code="scripts/run_recommender.py",
    inputs=[ProcessingInput(
        source=register_step.properties.ModelPackageArn,
        destination="/opt/ml/processing/input",
    )],
    outputs=[ProcessingOutput(
        output_name="recommendations",
        source="/opt/ml/processing/output",
    )],
)
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Pipeline hangs at container 2 | Container 1's output not in expected location | Default: `/opt/ml/output/data/`. Verify container 1's `output_data_dir` env |
| Direct-invoke can't find target container | `TargetContainerHostname` mismatch | Match `ContainerHostname` exactly (case-sensitive) |
| Inference Recommender job times out | Load test phase too long | Reduce phase durations to 60s; cap `MaxInvocations` to 1000 |
| Recommender shows g5.xlarge wins but P99 spikes | Sample payload isn't representative | Use real-world payloads; longer test duration |
| Recommender fails with payload error | Payload format mismatch | `SupportedContentTypes` must match container's `Accept` header |
| All instances fail latency SLA | SLA too tight or model too big | Check P99 across multiple instances; might need GPU + larger model |

### 6.1 Cost ballpark for Inference Recommender jobs

| Job type | Duration | Instance hours | $ |
|---|---|---|---|
| Default (5 instances × 4 phases × 2 min) | ~30 min | ~5 instance-hours | $5-$30 |
| Advanced (20 instances × custom load) | ~3 hr | ~60 instance-hours | $50-$300 |
| With p4d/p5e (FM-scale) | ~4 hr | ~32 p5e-hours | $1,000+ |

Budget: $30 per default job. Run on every model-registry approval (~weekly cadence) = ~$120/mo per pipeline.

---

## 7. Five non-negotiables

1. **Inference Recommender on every model approval.** Without it, you guess instance type → either over-provision (waste $) or under-provision (P99 misses SLA). Make it a Pipeline step.

2. **Sample payloads MUST match production traffic shape.** Default sample payloads are tiny. Capture 20+ real production payloads, save to S3, point Recommender at them.

3. **Pipeline mode `Serial` for sequential, `Direct` for routing.** Mismatch breaks invocation. Default is `Direct` (gotcha — most teams want `Serial`).

4. **Container `ContainerHostname` is the contract.** Lowercase, no spaces, ≤ 63 chars. Used in `TargetContainerHostname`. Document it in container docs.

5. **`StoppingConditions.ModelLatencyThresholds` — set P99 SLA explicitly.** Without it, Recommender returns instances that "work" but miss your SLA. Set P99 ≤ 500ms minimum.

---

## 8. References

- AWS docs:
  - [Inference Recommender](https://docs.aws.amazon.com/sagemaker/latest/dg/inference-recommender.html)
  - [Multi-container endpoints](https://docs.aws.amazon.com/sagemaker/latest/dg/multi-container-endpoints.html)
  - [Inference pipelines](https://docs.aws.amazon.com/sagemaker/latest/dg/inference-pipelines.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_SERVING` — single-container endpoints
  - `MLOPS_LLM_FINETUNING_PROD` — adapter inference components
  - `MLOPS_ASYNC_INFERENCE` — async alternative
  - `MLOPS_BATCH_TRANSFORM` — batch alternative

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — multi-container inference pipelines (Serial + Direct mode) and Inference Recommender for automated right-sizing. CDK monolith. Recommender trigger pattern + result interpretation + CI/CD integration. 5 non-negotiables. Created to fill F369 audit gap (2026-04-26): inference pipelines + cost optimization were 0% covered. |
