# PARTIAL: Step Functions — Workflow Orchestration

**Usage:** Include when SOW contains multi-step workflows, saga patterns, human approvals in business logic, or complex retry/compensation logic.

---

## When to Use Step Functions vs Lambda Chaining

| Pattern                            | Lambda Chaining   | Step Functions                |
| ---------------------------------- | ----------------- | ----------------------------- |
| Steps < 3, simple sequence         | ✅ Fine           | Overkill                      |
| Steps > 3 OR complex branching     | ❌ Gets messy     | ✅ Use this                   |
| Needs retry with backoff           | ❌ Manual code    | ✅ Built-in                   |
| Needs human approval in workflow   | ❌ Impossible     | ✅ .waitForTaskToken          |
| Long-running (hours/days)          | ❌ Lambda timeout | ✅ Use this                   |
| Need full audit trail of each step | ❌ DIY logging    | ✅ Built-in execution history |
| Parallel branches                  | ❌ Complex        | ✅ Parallel state             |

---

## CDK Code Block — Step Functions

```python
def _create_workflows(self, stage_name: str) -> None:
    """
    AWS Step Functions state machines for complex multi-step workflows.

    Common patterns implemented:
      A) Sequential workflow     (A → B → C → Done)
      B) Parallel workflow       (A → [B, C] in parallel → D)
      C) Saga / compensation     (A → B → C, on fail: undo B → undo A)
      D) Human approval in loop  (Submit → Wait → Human approves → Continue)

    [Claude: detect which pattern from Architecture Map by looking for:
      - "approval workflow", "multi-step process" → pattern D
      - "parallel processing", "concurrent" → pattern B
      - "rollback on failure", "compensating transaction" → pattern C
      - "pipeline", "sequential steps" → pattern A]
    """

    import aws_cdk.aws_stepfunctions as sfn
    import aws_cdk.aws_stepfunctions_tasks as sfn_tasks

    # =========================================================================
    # PATTERN A: Sequential Workflow
    # Example: Document Processing Pipeline
    # Receive Upload → Scan Virus → Extract Text → Classify → Store → Notify
    # =========================================================================

    # Step 1: Trigger virus scan
    scan_step = sfn_tasks.LambdaInvoke(
        self, "VirusScanStep",
        lambda_function=self.lambda_functions.get("VirusScanner", self.lambda_functions[list(self.lambda_functions.keys())[0]]),
        output_path="$.Payload",    # Pass Lambda's return value to next state
        retry_on_service_exceptions=True,
        task_timeout=sfn.Timeout.duration(Duration.minutes(5)),
    )

    # Step 2: Check scan result → branch on clean/infected
    check_scan = sfn.Choice(self, "ScanClean?")

    # Step 3a: If clean → extract content
    extract_step = sfn_tasks.LambdaInvoke(
        self, "ExtractTextStep",
        lambda_function=self.lambda_functions.get("TextExtractor", self.lambda_functions[list(self.lambda_functions.keys())[0]]),
        output_path="$.Payload",
    )

    # Step 3b: If infected → quarantine and notify
    quarantine_step = sfn_tasks.LambdaInvoke(
        self, "QuarantineStep",
        lambda_function=self.lambda_functions.get("Quarantine", self.lambda_functions[list(self.lambda_functions.keys())[0]]),
    )
    quarantine_notify = sfn_tasks.SnsPublish(
        self, "NotifyQuarantine",
        topic=self.alert_topic,
        message=sfn.TaskInput.from_json_path_at("States.Format('INFECTED FILE DETECTED: {}', $.s3_key)"),
    )

    # Step 4: Store result
    store_step = sfn_tasks.DynamoPutItem(
        self, "StoreResultStep",
        table=list(self.ddb_tables.values())[0],
        item={
            "pk": sfn_tasks.DynamoAttributeValue.from_string(
                sfn.JsonPath.string_at("$.document_id")
            ),
            "sk": sfn_tasks.DynamoAttributeValue.from_string("DOCUMENT#PROCESSED"),
            "status": sfn_tasks.DynamoAttributeValue.from_string("PROCESSED"),
        },
    )

    # Step 5: Notify success via SNS
    notify_success = sfn_tasks.SnsPublish(
        self, "NotifySuccess",
        topic=self.alert_topic,    # Replace with user notification topic
        message=sfn.TaskInput.from_json_path_at(
            "States.Format('Document {} processed successfully', $.document_id)"
        ),
    )

    # Failure handler: catch any unhandled errors
    workflow_failed = sfn.Fail(
        self, "WorkflowFailed",
        error="ProcessingFailed",
        cause="Document processing pipeline failed",
    )

    # Build state machine chain
    definition = (
        scan_step
        .next(
            check_scan
            .when(
                sfn.Condition.string_equals("$.scan_result", "CLEAN"),
                extract_step.next(store_step).next(notify_success)
            )
            .when(
                sfn.Condition.string_equals("$.scan_result", "INFECTED"),
                quarantine_step.next(quarantine_notify)
            )
            .otherwise(workflow_failed)
        )
    )

    # Add retry + error handling to each step
    scan_step.add_retry(
        errors=["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
        interval=Duration.seconds(2),
        max_attempts=3,
        backoff_rate=2.0,         # Exponential backoff: 2s, 4s, 8s
    )
    scan_step.add_catch(workflow_failed, errors=["States.ALL"])

    # Log group for Step Functions execution history
    sfn_log_group = logs.LogGroup(
        self, "WorkflowLogGroup",
        log_group_name=f"/{{project_name}}/{stage_name}/workflows",
        retention=logs.RetentionDays.ONE_MONTH,
        encryption_key=self.kms_key,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # State Machine
    self.doc_processing_sm = sfn.StateMachine(
        self, "DocProcessingStateMachine",
        state_machine_name=f"{{project_name}}-doc-processing-{stage_name}",
        definition_body=sfn.DefinitionBody.from_chainable(definition),

        # Express for high-throughput (100k exec/sec), sync execution
        # Standard for long-running (up to 1 year), async, full audit history
        state_machine_type=sfn.StateMachineType.EXPRESS if stage_name == "dev" else sfn.StateMachineType.STANDARD,

        # Timeout for entire execution
        timeout=Duration.minutes(30),

        # Logging
        logs=sfn.LogOptions(
            destination=sfn_log_group,
            level=sfn.LogLevel.ERROR if stage_name == "prod" else sfn.LogLevel.ALL,
            include_execution_data=stage_name != "prod",  # Don't log PHI to CloudWatch in prod
        ),

        # X-Ray tracing
        tracing_enabled=True,
    )

    # Grant Lambda functions permission to be invoked by Step Functions
    for fn in self.lambda_functions.values():
        fn.grant_invoke(self.doc_processing_sm)

    # Grant Lambda (trigger) permission to start the state machine
    if "DocumentUpload" in self.lambda_functions:
        self.doc_processing_sm.grant_start_execution(self.lambda_functions["DocumentUpload"])

    # =========================================================================
    # PATTERN D: Human Approval in Workflow (waitForTaskToken)
    # Used for business approval gates WITHIN a workflow (not CICD approvals)
    # Example: "Manager must approve expense report before payment"
    # =========================================================================

    # The task callback pattern:
    # 1. Workflow sends email with a unique task token
    # 2. Workflow PAUSES, waiting for the token to be returned
    # 3. Human clicks approve link → Lambda calls sfn.send_task_success(token)
    # 4. Workflow resumes from where it paused

    approval_task = sfn_tasks.SnsPublish(
        self, "SendApprovalEmail",
        topic=self.approval_topic if hasattr(self, 'approval_topic') else self.alert_topic,

        # Include the task token in the message payload
        # Approver's Lambda/API will call SendTaskSuccess with this token
        message=sfn.TaskInput.from_object({
            "approval_url": sfn.JsonPath.string_at("$.approval_url"),
            "request_id": sfn.JsonPath.string_at("$.request_id"),
            "task_token": sfn.JsonPath.task_token,  # The magic token
            "details": sfn.JsonPath.string_at("$.details"),
        }),

        # .wait_for_task_token PAUSES the workflow here until token is returned
        integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,

        # Heartbeat: if no heartbeat received, fail after 7 days (business SLA)
        heartbeat=Duration.days(7),

        task_timeout=sfn.Timeout.duration(Duration.days(7)),
    )

    # =========================================================================
    # PATTERN B: Parallel State
    # Example: After document upload, simultaneously: scan virus + extract metadata
    # =========================================================================

    parallel_processing = sfn.Parallel(self, "ParallelProcessing")
    parallel_processing.branch(
        sfn_tasks.LambdaInvoke(
            self, "ParallelVirusScan",
            lambda_function=self.lambda_functions.get("VirusScanner", list(self.lambda_functions.values())[0]),
        )
    )
    parallel_processing.branch(
        sfn_tasks.LambdaInvoke(
            self, "ParallelMetadataExtract",
            lambda_function=self.lambda_functions.get("MetadataExtractor", list(self.lambda_functions.values())[0]),
        )
    )
    # parallel_processing runs BOTH branches simultaneously, waits for both to finish

    # =========================================================================
    # MAP STATE: Process a list of items in parallel (dynamic parallelism)
    # Example: Generate 10 report sections simultaneously
    # =========================================================================

    map_state = sfn.Map(
        self, "ProcessSections",
        items_path="$.sections",      # Array in input
        result_path="$.section_results",
        max_concurrency=5,            # Max 5 parallel iterations
    )
    map_state.item_processor(
        sfn_tasks.LambdaInvoke(
            self, "ProcessSection",
            lambda_function=self.lambda_functions.get("SectionProcessor", list(self.lambda_functions.values())[0]),
            output_path="$.Payload",
        )
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "DocProcessingStateMachineArn",
        value=self.doc_processing_sm.state_machine_arn,
        description="Document Processing State Machine ARN",
        export_name=f"{{project_name}}-doc-processing-sm-{stage_name}",
    )
```
