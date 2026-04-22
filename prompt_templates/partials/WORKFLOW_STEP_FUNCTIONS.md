# SOP — Step Functions Workflow Orchestration

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Step Functions Standard + Express + Distributed Map

---

## 1. Purpose

State-machine orchestration for:

- Multi-step business processes (saga, compensation)
- Long-running async pipelines (Transcribe + Bedrock + persist)
- Batch fan-out via Distributed Map (up to 10,000 parallel children)
- Retry + catch + DLQ at the workflow level (not per-Lambda)

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| State machine + task-target Lambdas in one stack | **§3 Monolith Variant** |
| State machine in `OrchestrationStack`, Lambdas in `ComputeStack`, buckets in `StorageStack` | **§4 Micro-Stack Variant** |

**Why the split matters.**
- `tasks.LambdaInvoke(lambda_function=fn)` across stacks auto-grants `lambda:InvokeFunction` on the Lambda's resource policy referencing the state machine's ARN → bidirectional export.
- `audio_bucket.grant_read(sfn_role)` across stacks auto-grants KMS Decrypt on the bucket's encryption key in `SecurityStack` → same cycle we have seen repeatedly.
- SFN's built-in SDK integrations for Transcribe / S3 / DDB need IAM actions on the SFN role — always identity-side.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_iam as iam,
)


def _create_workflow(self, stage: str) -> None:
    # -- State machine role (monolith: L2 grants OK) -------------------------
    sfn_role = iam.Role(
        self, "StateMachineRole",
        assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        role_name=f"{{project_name}}-sfn-role-{stage}",
    )
    # Monolith: direct L2 grants (same stack)
    self.lambda_functions["Processing"].grant_invoke(sfn_role)
    self.audio_bucket.grant_read(sfn_role)
    self.transcript_bucket.grant_read_write(sfn_role)

    # -- States --------------------------------------------------------------
    validate = sfn.Choice(self, "ValidateInput")
    invalid  = sfn.Fail(self, "InputInvalid", cause="Missing job_id", error="ValidationError")

    start_transcribe = sfn.CustomState(
        self, "StartTranscriptionJob",
        state_json={
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:transcribe:startTranscriptionJob",
            "Parameters": {
                "TranscriptionJobName.$": "States.Format('job-{}', $.job_id)",
                "LanguageCode": "en-US",
                "Media": {"MediaFileUri.$": "States.Format('s3://{}/{}', $.audio_bucket, $.audio_key)"},
                "OutputBucketName.$": "$.transcript_bucket",
                "Settings": {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 10},
            },
            "ResultPath": "$.transcribe_result",
            "Retry": [{"ErrorEquals": ["Transcribe.LimitExceededException"],
                        "IntervalSeconds": 5, "MaxAttempts": 5, "BackoffRate": 2.0}],
            # NOTE: do NOT set "Next" here — use CDK chaining below so the
            # state is attached to the graph exactly once.
        },
    )

    wait = sfn.Wait(self, "WaitForTranscription",
                     time=sfn.WaitTime.duration(Duration.seconds(30)))

    check = sfn.CustomState(
        self, "CheckTranscribeStatus",
        state_json={
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:transcribe:getTranscriptionJob",
            "Parameters": {
                "TranscriptionJobName.$": "$.transcribe_result.TranscriptionJob.TranscriptionJobName"
            },
            "ResultPath": "$.job_status",
        },
    )

    is_done = sfn.Choice(self, "IsTranscribeComplete")
    failed  = sfn.Fail(self, "TranscriptionFailed",
                        cause="Transcribe returned FAILED", error="TranscribeError")

    invoke_processing = tasks.LambdaInvoke(
        self, "InvokeProcessing",
        lambda_function=self.lambda_functions["Processing"],
        payload=sfn.TaskInput.from_json_path_at("$"),
        result_path="$.processing_result",
    )
    invoke_processing.add_retry(
        errors=["States.TaskFailed"],
        interval=Duration.seconds(2), max_attempts=3, backoff_rate=2.0,
    )

    complete = sfn.Pass(self, "MarkJobComplete",
                        parameters={"status": "COMPLETE", "job_id.$": "$.job_id"})

    # -- Wire once -----------------------------------------------------------
    wait.next(check).next(is_done)
    is_done.when(
        sfn.Condition.string_equals("$.job_status.TranscriptionJob.TranscriptionJobStatus", "COMPLETED"),
        invoke_processing.next(complete),
    ).when(
        sfn.Condition.string_equals("$.job_status.TranscriptionJob.TranscriptionJobStatus", "FAILED"),
        failed,
    ).otherwise(wait)

    validate.when(
        sfn.Condition.not_(sfn.Condition.is_present("$.job_id")),
        invalid,
    ).otherwise(start_transcribe)

    start_transcribe.next(wait)

    # -- Logs + state machine ------------------------------------------------
    log_group = logs.LogGroup(self, "SfnLogs",
        log_group_name=f"/aws/states/{{project_name}}-pipeline-{stage}",
        retention=logs.RetentionDays.ONE_MONTH,
    )
    self.state_machine = sfn.StateMachine(
        self, "Pipeline",
        state_machine_name=f"{{project_name}}-pipeline-{stage}",
        definition_body=sfn.DefinitionBody.from_chainable(validate),
        role=sfn_role,
        tracing_enabled=True,
        logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL,
                             include_execution_data=True),
        timeout=Duration.minutes(30),
    )
```

### 3.1 Monolith gotchas

- **Never call `.next()` twice on the same state.** CDK enforces "state X already has a next" validation. If a state is the target of a loop (e.g. `wait.next(check)` plus `is_done.otherwise(wait)`), chain it ONCE via CDK; the `.otherwise(wait)` jumps to the already-chained state without re-chaining.
- **Don't mix `"Next"` in `state_json` AND `.next()` in CDK** — the first sets it in raw ASL, the second asserts it in CDK's graph. CDK will either silently overwrite or fail validation.
- **Distributed Map** needs `item_reader` + `item_batcher` properly configured. See §4.3.

---

## 4. Micro-Stack Variant

### 4.1 `OrchestrationStack` — identity-side grants on SFN role

```python
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_logs as logs,
    aws_iam as iam,
)
from constructs import Construct


class OrchestrationStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        processing_fn: _lambda.IFunction,
        audio_bucket: s3.IBucket,
        transcript_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-orchestration", **kwargs)

        # Role lives in THIS stack. Grants are identity-side on it.
        sfn_role = iam.Role(
            self, "StateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            role_name="{project_name}-sfn-role",
        )
        # Lambda invoke permission (identity-side — does NOT mutate processing_fn)
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[processing_fn.function_arn, f"{processing_fn.function_arn}:*"],
        ))
        # S3 reads on audio bucket; writes on transcript bucket — identity-side
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[audio_bucket.bucket_arn, audio_bucket.arn_for_objects("*")],
        ))
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
            resources=[transcript_bucket.bucket_arn, transcript_bucket.arn_for_objects("*")],
        ))
        # Transcribe via SDK integration
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"],
            resources=["*"],
        ))
        # X-Ray
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                     "xray:GetSamplingRules", "xray:GetSamplingTargets"],
            resources=["*"],
        ))

        # ... states identical to §3, wired via CDK chaining once each ...

        log_group = logs.LogGroup(
            self, "SfnLogs",
            log_group_name="/aws/states/{project_name}-pipeline",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        self.state_machine = sfn.StateMachine(
            self, "Pipeline",
            state_machine_name="{project_name}-pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(validate),  # same graph
            role=sfn_role,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=log_group, level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
            timeout=Duration.minutes(30),
        )

        cdk.CfnOutput(self, "StateMachineArn", value=self.state_machine.state_machine_arn)
```

### 4.2 `tasks.LambdaInvoke` is SAFE in micro-stack

`tasks.LambdaInvoke(lambda_function=fn)` alone does NOT auto-grant cross-stack — it uses the SFN role's identity policy when `role` is provided. The CYCLE only forms if you also call `fn.grant_invoke(sfn_role)` from orchestration stack (which would add `sfn_role.arn` to the function's resource policy in ComputeStack).

**Rule:** use `tasks.LambdaInvoke(...)` with an explicit `role=sfn_role` passed to the state machine, AND add `lambda:InvokeFunction` to sfn_role identity policy manually. Do not call `fn.grant_invoke(role)`.

### 4.3 Distributed Map for batch

```python
distributed_map = sfn.DistributedMap(
    self, "BatchFanout",
    max_concurrency=100,
    item_reader=sfn.S3JsonItemReader(bucket=audio_bucket, key="batches/{job_batch_id}.json"),
    item_batcher=sfn.ItemBatcher(max_items_per_batch=10),
    result_writer=sfn.ResultWriter(bucket=transcript_bucket, prefix="aggregate-results/"),
    tolerated_failure_percentage=5,
)
# Each child runs an Express state machine (low-latency, cheap) with the same
# core logic used in the Standard primary workflow.
distributed_map.item_processor(
    sfn.DefinitionBody.from_chainable(start_transcribe),
    mode=sfn.ProcessorMode.DISTRIBUTED,
    execution_type=sfn.ProcessorType.EXPRESS,
)
```

### 4.4 Micro-stack gotchas

- **Express workflows** don't support long-duration waits (>5 min) — use Standard for polling-heavy flows.
- **Callback pattern** (task token + `SendTaskSuccess`) works cross-stack: consumer Lambda receives the task token, calls SFN API to resume. No cross-stack policy mutation.
- **Distributed Map** requires the S3 input bucket to have a specific IAM set-up; add it identity-side on the SFN role.

---

## 5. Workflow patterns — when to use what

| Pattern | Use |
|---|---|
| Single state machine, mixed parallel + sequential | Standard |
| High-volume short tasks (< 5 min) | Express (5× cheaper, higher throughput) |
| Fan-out > 40 concurrent | Distributed Map + Express children |
| Long-running with manual approval | Standard + task token callback |
| Near-realtime (< 100 ms orchestration overhead) | Step Functions is wrong tool; use EventBridge Pipes or direct Lambda chain |

---

## 6. Worked example

```python
def test_state_machine_has_x_ray_and_log_destination():
    import aws_cdk as cdk
    from aws_cdk.assertions import Template, Match
    # ... instantiate OrchestrationStack ...
    t = Template.from_stack(orch)
    t.has_resource_properties("AWS::StepFunctions::StateMachine", {
        "TracingConfiguration": {"Enabled": True},
        "LoggingConfiguration": Match.object_like({"Level": "ALL"}),
    })
```

---

## 7. References

- `docs/template_params.md` — `MAX_TRANSCRIBE_POLL_ATTEMPTS`, `TRANSCRIBE_POLL_INTERVAL_SECONDS`
- `docs/Feature_Roadmap.md` — O-01..O-25
- Related SOPs: `LAYER_BACKEND_LAMBDA` (task targets), `LLMOPS_BEDROCK` (Bedrock task integration)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Rule: chain each state via CDK exactly once. Identity-side sfn_role grants in micro-stack. Distributed Map recipe. |
| 1.0 | 2026-03-05 | Initial. |
