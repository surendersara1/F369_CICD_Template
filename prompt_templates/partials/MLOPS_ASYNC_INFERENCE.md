# SOP — SageMaker Asynchronous Inference (large-payload S3 in/out · SNS notifications · auto-scale to 0)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker async inference endpoints (GA 2021, mature 2024-2026) · S3 input/output objects (up to 1 GB request, 1 GB response) · SNS success/failure topics · auto-scale 0-N instances (cold start ~5 min) · HuggingFace / PyTorch / custom containers

---

## 1. Purpose

- Codify the **async inference pattern** for ML predictions where:
  - Request payload exceeds the 6 MB real-time limit (large images, video, document PDFs)
  - Inference takes > 60 sec (real-time max) but < 1 hour (async max)
  - Throughput is bursty (auto-scale to 0 between batches)
  - Caller is OK with eventual response via SNS / S3 polling (not synchronous)
- Codify the **S3 input → endpoint → S3 output → SNS** wiring with KMS-per-zone.
- Codify the **auto-scale-to-zero** pattern (only async + serverless support this) with cold-start mitigation via `MinCapacity=0, MaxCapacity=10`.
- Provide the **invocation pattern**: caller `PUT`s payload to S3, calls `InvokeEndpointAsync`, gets immediate `InferenceId`, response delivered to S3 + SNS later.
- This is the **large-payload-async specialisation**. `MLOPS_SAGEMAKER_SERVING` covers real-time (sync, ≤6 MB, ≤60s); `MLOPS_BATCH_TRANSFORM` covers offline batch (millions of records). Async is the middle ground: per-request latency 1-30 min, single-record at a time.

When the SOW signals: "process a 100 MB PDF", "video frame inference", "long-running ML predictions", "burst workload, auto-scale to zero between batches", "fire-and-forget inference with notification".

---

## 2. Decision tree — sync vs async vs batch

```
Payload size + latency?
├── ≤ 6 MB request, ≤ 60 sec response → MLOPS_SAGEMAKER_SERVING (real-time)
├── ≤ 1 GB request, ≤ 1 hour response, sporadic → §3 ASYNC (this partial)
├── Millions of records, batch processing → MLOPS_BATCH_TRANSFORM
└── Streaming response (LLM tokens) → MLOPS_LLM_FINETUNING_PROD §adapter inference

Burst pattern?
├── Steady traffic 24/7 → real-time (auto-scale 1-N)
├── Sporadic bursts, idle hours → §3 ASYNC (auto-scale 0-N, save 90% cost)
└── Daily batch run → batch transform (cheapest)

Latency tolerance?
├── User waiting (sync) → real-time
├── User OK with email/notification (~5 min) → §3 ASYNC
├── User OK with overnight (~24 hr) → batch transform
└── User wants WebSocket stream → not async; use Streaming inference (LLM token streaming)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — endpoint + S3 buckets + SNS in one stack | **§3 Monolith Variant** |
| `ServingStack` owns endpoint + IAM; `ConsumerStack` owns invocation logic + SNS subscribers | **§5 Micro-Stack Variant** |

---

## 3. Monolith Variant — async endpoint with S3 in/out + SNS notifications

### 3.1 Architecture

```
   Caller (Lambda / EC2 / on-prem)
        │
        │  1. PUT payload to S3
        ▼
   S3 input bucket: s3://qra-async-input/<request-id>.json
        │
        │  2. InvokeEndpointAsync(input_location=<S3 URI>)
        ▼
   ┌────────────────────────────────────────────────────────────┐
   │  SageMaker Async Endpoint: my-async-endpoint               │
   │     - Instance: ml.g5.2xlarge (auto-scale 0-10)             │
   │     - Internal queue (max 100 in-flight)                    │
   │     - Container: HuggingFace / PyTorch                      │
   │     - Per-request timeout: 1 hour                            │
   └────────────────────────────────────────────────────────────┘
        │
        │  3. Container reads input from S3, runs inference
        │
        ▼
   S3 output bucket: s3://qra-async-output/<inference-id>.json
        │
        │  4. SNS notification: { "inferenceId", "outputLocation",
        │                          "responseStatus" }
        ▼
   ┌────────────────────────────────────────────────────────────┐
   │  SNS Topic: async-success-topic                              │
   │     subscribers: caller's Lambda, SQS queue, email           │
   └────────────────────────────────────────────────────────────┘
        │  on FAILURE → async-failure-topic
        ▼
   Caller's downstream Lambda picks up SNS message,
   reads result from S3, processes.
```

### 3.2 CDK — `_create_async_inference_endpoint()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sagemaker as sagemaker,         # L1
    aws_applicationautoscaling as appscaling,
)


def _create_async_inference_endpoint(self, stage: str) -> None:
    """Monolith. SageMaker async inference endpoint with S3 in/out + SNS topics
    + auto-scale to zero between batches."""

    # A) S3 buckets — input + output (separate, KMS-encrypted)
    self.async_input = s3.Bucket(self, "AsyncInput",
        bucket_name=f"{{project_name}}-async-input-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="DeleteAfter7Days",
            expiration=Duration.days(7),                # input is ephemeral
        )],
        removal_policy=RemovalPolicy.DESTROY if stage != "prod" else RemovalPolicy.RETAIN,
    )
    self.async_output = s3.Bucket(self, "AsyncOutput",
        bucket_name=f"{{project_name}}-async-output-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="GlacierAfter30Days",
            transitions=[s3.Transition(
                storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                transition_after=Duration.days(30),
            )],
            noncurrent_version_expiration=Duration.days(180),
        )],
        removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
    )

    # B) SNS topics — success + failure
    self.async_success_topic = sns.Topic(self, "AsyncSuccessTopic",
        topic_name=f"{{project_name}}-async-success-{stage}",
        display_name="Async Inference Success",
        master_key=self.kms_key,
    )
    self.async_failure_topic = sns.Topic(self, "AsyncFailureTopic",
        topic_name=f"{{project_name}}-async-failure-{stage}",
        display_name="Async Inference Failure",
        master_key=self.kms_key,
    )

    # C) Endpoint execution role
    self.endpoint_role = iam.Role(self, "AsyncEndpointRole",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.async_input.grant_read(self.endpoint_role)
    self.async_output.grant_read_write(self.endpoint_role)
    self.kms_key.grant_encrypt_decrypt(self.endpoint_role)
    self.async_success_topic.grant_publish(self.endpoint_role)
    self.async_failure_topic.grant_publish(self.endpoint_role)

    # D) Model — assume model package from registry (cross-stack via SSM)
    model_pkg_arn = ssm.StringParameter.value_for_string_parameter(
        self, f"/{{project_name}}/{stage}/llm/active-model-package")

    self.async_model = sagemaker.CfnModel(self, "AsyncModel",
        model_name=f"{{project_name}}-async-model-{stage}",
        execution_role_arn=self.endpoint_role.role_arn,
        primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
            model_package_name=model_pkg_arn,
        ),
        vpc_config=sagemaker.CfnModel.VpcConfigProperty(
            security_group_ids=[self.endpoint_sg.security_group_id],
            subnets=[s.subnet_id for s in self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
        ),
        enable_network_isolation=False,                 # need S3 access
    )

    # E) Endpoint config — ASYNC INFERENCE CONFIG IS THE KEY
    self.async_endpoint_config = sagemaker.CfnEndpointConfig(self, "AsyncEpConfig",
        endpoint_config_name=f"{{project_name}}-async-ep-config-{stage}",
        kms_key_id=self.kms_key.key_arn,
        production_variants=[sagemaker.CfnEndpointConfig.ProductionVariantProperty(
            variant_name="AllTraffic",
            model_name=self.async_model.model_name,
            initial_instance_count=1,                   # min for prod readiness
            instance_type="ml.g5.2xlarge",              # GPU for ML inference
            initial_variant_weight=1.0,
        )],
        # ────── ASYNC CONFIG ────────────────────────────────────────────
        async_inference_config=sagemaker.CfnEndpointConfig.AsyncInferenceConfigProperty(
            output_config=sagemaker.CfnEndpointConfig.AsyncInferenceOutputConfigProperty(
                s3_output_path=f"s3://{self.async_output.bucket_name}/output/",
                s3_failure_path=f"s3://{self.async_output.bucket_name}/errors/",
                kms_key_id=self.kms_key.key_arn,
                notification_config=sagemaker.CfnEndpointConfig.AsyncInferenceNotificationConfigProperty(
                    success_topic=self.async_success_topic.topic_arn,
                    error_topic=self.async_failure_topic.topic_arn,
                    include_inference_response_in=["SUCCESS_NOTIFICATION_TOPIC", "ERROR_NOTIFICATION_TOPIC"],
                ),
            ),
            client_config=sagemaker.CfnEndpointConfig.AsyncInferenceClientConfigProperty(
                max_concurrent_invocations_per_instance=4,    # internal queue
            ),
        ),
    )

    # F) Endpoint
    self.async_endpoint = sagemaker.CfnEndpoint(self, "AsyncEp",
        endpoint_name=f"{{project_name}}-async-ep-{stage}",
        endpoint_config_name=self.async_endpoint_config.endpoint_config_name,
    )
    self.async_endpoint.add_dependency(self.async_endpoint_config)

    # G) Auto-scale 0 to 10 — UNIQUE TO ASYNC: scale to zero
    target = appscaling.ScalableTarget(self, "AsyncScaleTarget",
        service_namespace=appscaling.ServiceNamespace.SAGEMAKER,
        scalable_dimension="sagemaker:variant:DesiredInstanceCount",
        resource_id=f"endpoint/{self.async_endpoint.endpoint_name}/variant/AllTraffic",
        min_capacity=0,                                         # ZERO — unique to async/serverless
        max_capacity=10,
    )
    # Scale on backlog metric — # of pending requests in queue
    target.scale_to_track_metric("BacklogPerInstance",
        target_value=4.0,
        custom_metric=cloudwatch.Metric(
            namespace="AWS/SageMaker",
            metric_name="ApproximateBacklogSizePerInstance",
            dimensions_map={"EndpointName": self.async_endpoint.endpoint_name},
            statistic="Average",
        ),
        scale_in_cooldown=Duration.minutes(5),
        scale_out_cooldown=Duration.minutes(2),
    )

    CfnOutput(self, "AsyncEndpointName", value=self.async_endpoint.endpoint_name)
    CfnOutput(self, "InputBucketName",   value=self.async_input.bucket_name)
    CfnOutput(self, "SuccessTopicArn",   value=self.async_success_topic.topic_arn)
```

### 3.3 Caller-side invocation pattern

```python
"""Lambda that submits a large PDF for async ML inference."""
import os
import json
import uuid
import boto3

s3 = boto3.client("s3")
sm = boto3.client("sagemaker-runtime")


def handler(event, context):
    pdf_bytes = event["pdf_bytes"]                          # base64 in this example
    request_id = str(uuid.uuid4())

    # 1. PUT payload to S3 (encrypted with KMS-per-zone)
    s3.put_object(
        Bucket=os.environ["INPUT_BUCKET"],
        Key=f"requests/{request_id}.pdf",
        Body=pdf_bytes,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ["KMS_KEY_ARN"],
    )

    # 2. Submit async inference
    response = sm.invoke_endpoint_async(
        EndpointName=os.environ["ENDPOINT_NAME"],
        InputLocation=f"s3://{os.environ['INPUT_BUCKET']}/requests/{request_id}.pdf",
        ContentType="application/pdf",
        # Optional: caller-defined output prefix
        InferenceId=request_id,
        InvocationTimeoutSeconds=3600,                       # 1 hr max
        # Optional: separate KMS key for response (if multi-tenant)
        # CustomAttributes can pass per-request config
        CustomAttributes=json.dumps({"tenant": event["tenant_id"]}),
    )
    return {
        "inferenceId":     response["InferenceId"],
        "outputLocation":  response["OutputLocation"],          # S3 URI where result lands
        "failureLocation": response["FailureLocation"],          # S3 URI on failure
    }
```

### 3.4 SNS-subscriber Lambda — receives result notifications

```python
"""Lambda subscribed to async-success-topic. Reads result from S3, processes."""
import os
import json
import boto3

s3 = boto3.client("s3")


def handler(event, context):
    """SNS message envelope contains the SageMaker notification."""
    for record in event["Records"]:
        msg = json.loads(record["Sns"]["Message"])

        inference_id     = msg["inferenceId"]
        output_location  = msg["responseParameters"]["outputLocation"]   # s3://...
        # If include_inference_response_in includes the topic, response is inline:
        if "responseBody" in msg.get("responseParameters", {}):
            inline_response = json.loads(msg["responseParameters"]["responseBody"])
            # process inline (small responses)
            print(f"Inline result for {inference_id}: {inline_response}")
            continue

        # Else, fetch from S3
        bucket, key = output_location.replace("s3://", "").split("/", 1)
        obj = s3.get_object(Bucket=bucket, Key=key)
        result = json.loads(obj["Body"].read())
        # ... downstream processing ...
```

### 3.5 Cold-start mitigation

Async endpoints scale to 0 → first request after idle period takes ~3-5 min (instance warm-up + container start + model load).

Mitigation strategies:

| Strategy | Trade-off |
|---|---|
| `min_capacity=1` (always-warm 1 instance) | Loses scale-to-zero cost benefit |
| `min_capacity=0` + caller polls / waits | Simplest but adds 5-min latency per cold-start |
| Keep-warm Lambda hits endpoint every 10 min during business hours | $0.50/mo extra; reduces cold starts to ~5/day |
| Provisioned Concurrency (newer) | Predictable latency at higher cost |
| Pre-loaded model at instance start | Container image bakes model; faster start |

```python
# Keep-warm Lambda example
events.Rule(self, "KeepWarmRule",
    schedule=events.Schedule.rate(Duration.minutes(10)),
    targets=[targets.LambdaFunction(keep_warm_fn)],
    description="Pre-warm async endpoint every 10 min during business hours",
)
```

---

## 4. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| First request after 1hr idle takes 5+ min | Cold start (auto-scale to 0) | Use min_capacity=1 OR keep-warm Lambda OR accept the latency |
| Backlog grows but no scale-out | `BacklogPerInstance` not configured | Verify scale-on-metric uses `ApproximateBacklogSizePerInstance` not invocations |
| SNS messages missing | KMS encryption mismatch | If SNS topic uses CMK A and IAM role can't decrypt → silent drop. Grant role kms:Decrypt on topic CMK |
| Output truncated | Response > 1 GB limit | Async max response is 1 GB; for larger use Batch Transform |
| `InvocationTimeoutSeconds` exceeded | Inference > 1 hr | Async max is 1 hr; for longer use Batch Transform OR Step Functions chunking |
| Caller doesn't see immediate response | Misunderstanding async semantics | `invoke_endpoint_async` returns `InferenceId` immediately; result via SNS / S3 polling |
| Multi-tenant: tenant A sees tenant B's results | Output prefix not tenant-scoped | Use `CustomAttributes` to pass tenant_id; container writes to `s3://output/{tenant}/...` |

### 4.1 Cost ballpark

| Workload | Compute | Idle | Cost / 1M requests |
|---|---|---|---|
| 100 KB image inference, 5 sec | ml.g5.2xlarge × 1 (avg) | scale to 0 between bursts | ~$120 |
| 100 MB PDF inference, 60 sec | ml.g5.2xlarge × 2 (avg) | scale to 0 nightly | ~$1,800 |
| Always-warm 1 instance | ml.g5.2xlarge × 1 (24/7) | n/a | ~$1,000/mo + per-request |
| Keep-warm Lambda | $0.50/mo | n/a | $0.50/mo |

Compare to real-time always-warm: ~$1,000/mo even at 0 traffic. Async-with-scale-to-0: $50/mo at low traffic.

---

## 5. Micro-Stack variant (cross-stack via SSM)

```python
# In ServingStack
ssm.StringParameter(self, "AsyncEpName",
    parameter_name=f"/{{project_name}}/{stage}/async/endpoint-name",
    string_value=self.async_endpoint.endpoint_name)
ssm.StringParameter(self, "AsyncInputBucket",
    parameter_name=f"/{{project_name}}/{stage}/async/input-bucket",
    string_value=self.async_input.bucket_name)
ssm.StringParameter(self, "AsyncSuccessTopicArn",
    parameter_name=f"/{{project_name}}/{stage}/async/success-topic-arn",
    string_value=self.async_success_topic.topic_arn)

# In ConsumerStack — caller Lambda + SNS subscriber
endpoint_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/async/endpoint-name")
input_bucket = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/async/input-bucket")

# Caller Lambda — identity-side grants
caller_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["sagemaker:InvokeEndpointAsync"],
    resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{endpoint_name}"],
))
caller_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["s3:PutObject"],
    resources=[f"arn:aws:s3:::{input_bucket}/requests/*"],
))
```

---

## 6. Worked example — pytest

```python
def test_async_endpoint_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")

    from infrastructure.cdk.stacks.async_endpoint_stack import AsyncEndpointStack
    stack = AsyncEndpointStack(app, stage_name="prod", env=env, ...)
    t = Template.from_stack(stack)

    # Endpoint config with async config block
    t.has_resource_properties("AWS::SageMaker::EndpointConfig", Match.object_like({
        "AsyncInferenceConfig": Match.object_like({
            "OutputConfig": Match.object_like({
                "NotificationConfig": Match.object_like({
                    "SuccessTopic": Match.any_value(),
                    "ErrorTopic":   Match.any_value(),
                }),
            }),
            "ClientConfig": Match.object_like({
                "MaxConcurrentInvocationsPerInstance": Match.greater_than(0),
            }),
        }),
    }))
    # 2 SNS topics (success + failure)
    t.resource_count_is("AWS::SNS::Topic", Match.greater_than_or_equal(2))
    # 2 S3 buckets (input + output)
    t.resource_count_is("AWS::S3::Bucket", Match.greater_than_or_equal(2))
    # Auto-scale target with min_capacity=0
    t.has_resource_properties("AWS::ApplicationAutoScaling::ScalableTarget",
        Match.object_like({
            "MinCapacity": 0,
            "ServiceNamespace": "sagemaker",
        }))
```

---

## 7. Five non-negotiables

1. **Always set `s3_failure_path`.** Without it, failed inferences silently disappear. Set to a separate prefix on the output bucket (`errors/`).

2. **`include_inference_response_in` for small responses.** Saves caller a round-trip to S3 for ≤ 256 KB responses. Set `["SUCCESS_NOTIFICATION_TOPIC"]` in NotificationConfig.

3. **Min capacity 0 ONLY when latency tolerance allows 5-min cold starts.** For user-facing async (e.g. "process this in 1 min"), min capacity 1 + keep-warm.

4. **Lifecycle on input bucket: 7-day expiration.** Input is ephemeral (already in output). Keeping forever leaks PII, costs money.

5. **Output bucket KMS-encrypted with reports-zone CMK.** Same key as final delivered models — separate from raw-zone CMK to maintain trust boundary.

---

## 8. References

- `docs/template_params.md` — `ASYNC_ENDPOINT_INSTANCE_TYPE`, `ASYNC_MIN_CAPACITY`, `ASYNC_MAX_CAPACITY`, `ASYNC_BACKLOG_TARGET`, `ASYNC_OUTPUT_RETENTION_DAYS`
- AWS docs:
  - [Async inference overview](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference.html)
  - [Inference options comparison](https://docs.aws.amazon.com/sagemaker/latest/dg/deploy-model-options.html)
  - [Auto-scaling async endpoints](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference-autoscale.html)
  - [SNS notification config](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference-monitor.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_SERVING` — real-time / serverless
  - `MLOPS_BATCH_TRANSFORM` — millions-of-records offline
  - `MLOPS_INFERENCE_PIPELINE_RECOMMENDER` — multi-container endpoints
  - `LAYER_OBSERVABILITY` — backlog + duration alarms

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — async inference for large-payload + bursty workloads. CDK monolith with S3 in/out + SNS topics + auto-scale 0-10 + KMS-per-zone. Caller invocation pattern + SNS subscriber pattern. Cold-start mitigation strategies (always-warm, keep-warm Lambda, Provisioned Concurrency). 5 non-negotiables. Created to fill F369 audit gap (2026-04-26): async endpoints were 0% covered. |
