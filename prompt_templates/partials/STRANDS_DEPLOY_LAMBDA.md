# PARTIAL: Strands Deploy to Lambda — Serverless Agent Hosting

**Usage:** Include when SOW mentions Lambda-hosted agents, serverless agent deployment, or lightweight agent tasks (<15 min).

---

## Lambda Deployment Overview

```
Lambda Agent = Serverless agent execution for request/response workloads:
  - Max 15 min timeout (suitable for most agent tasks)
  - Official Strands Lambda Layer available (quick setup)
  - Custom layer for additional dependencies (strands-agents-tools)
  - ARM64 recommended for cost optimization
  - MCP tools require context manager lifecycle on Lambda
  - No streaming (use Fargate for streaming responses)

Lambda Layer ARN:
  arn:aws:lambda:{region}:856699698935:layer:strands-agents-py{version}-{arch}:{layer_version}
  Example: arn:aws:lambda:us-east-1:856699698935:layer:strands-agents-py3_12-x86_64:1

  Python: 3.10, 3.11, 3.12, 3.13
  Arch: x86_64, aarch64
  Layer v1 = SDK v1.23.0
```

---

## CDK Code Block — Lambda Agent Host

```python
def _create_strands_agent_lambda(self, stage_name: str) -> None:
    """
    Strands agent Lambda function.

    Components:
      A) IAM Role for Bedrock model access
      B) Lambda function with agent code
      C) Environment variables for agent configuration

    [Claude: include for any SOW with Strands agents.
     Use Lambda for <15min tasks. Use ECS Fargate for long-running/streaming.]
    """

    # =========================================================================
    # A) IAM ROLE
    # =========================================================================

    self.strands_agent_role = iam.Role(
        self, "StrandsAgentRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        role_name=f"{{project_name}}-strands-agent-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole"),
        ],
    )
    self.strands_agent_role.add_to_policy(iam.PolicyStatement(
        sid="BedrockModelInvoke",
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources=[
            f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.*",
            f"arn:aws:bedrock:{self.region}::foundation-model/amazon.*",
        ],
    ))

    # [Claude: add KB Retrieve, Guardrails, etc. based on SOW]

    # =========================================================================
    # B) LAMBDA FUNCTION
    # =========================================================================

    self.strands_agent_lambda = _lambda.Function(
        self, "StrandsAgentFn",
        function_name=f"{{project_name}}-strands-agent-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("src/strands_agent"),
        environment={
            "STAGE": stage_name,
            "DEFAULT_MODEL_ID": "anthropic.claude-sonnet-4-20250514-v1:0",
            "SESSION_TABLE": self.agent_session_table.table_name,
            "AGENT_ARTIFACTS_BUCKET": self.agent_artifacts_bucket.bucket_name,
            "MAX_TURNS": "30",
            # [Claude: add KNOWLEDGE_BASE_ID, GUARDRAIL_ID if enabled]
        },
        timeout=Duration.minutes(15),
        memory_size=512 if stage_name == "dev" else 1024,
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
        role=self.strands_agent_role,
    )

    self.agent_session_table.grant_read_write_data(self.strands_agent_role)
    self.agent_artifacts_bucket.grant_read_write(self.strands_agent_role)

    CfnOutput(self, "StrandsAgentLambdaArn",
        value=self.strands_agent_lambda.function_arn,
        description="Strands Agent Lambda ARN",
    )
```

---

## Packaging for Lambda

```bash
# Install dependencies for ARM64 Lambda
pip install -r requirements.txt \
    --python-version 3.12 \
    --platform manylinux2014_aarch64 \
    --target ./packaging/_dependencies \
    --only-binary=:all:
```

---

## requirements.txt

```
strands-agents>=0.1.0
strands-agents-tools>=0.1.0
boto3>=1.35.0
```
