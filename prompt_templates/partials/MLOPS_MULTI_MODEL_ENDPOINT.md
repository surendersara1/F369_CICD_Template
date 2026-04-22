# SOP — MLOps Multi-Model Endpoint (Host 50-100+ Models on One Instance)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Multi-Model Endpoint (MME) · `aws_sagemaker` L1 constructs · `aws_lambda` Python 3.13 · `sagemaker-runtime` (`TargetModel` header)

---

## 1. Purpose

- Provision a SageMaker **Multi-Model Endpoint (MME)** — one endpoint instance hosts N (50–500+) models, SageMaker loads them on-demand via the `TargetModel` header and evicts LRU models from memory.
- Codify the **model-routing Lambda** — accepts `tenant_id` + payload, resolves `TargetModel = {tenant_id}/{version}/model.tar.gz`, calls `InvokeEndpoint` and handles `ModelNotReadyException` with a `Retry-After` header.
- Codify the **model-upload Lambda** — copies an approved model artifact into the MME S3 prefix (`models/{tenant_id}/{version}/model.tar.gz`).
- Justify the cost win: 100 models on one endpoint instance ≈ $201/mo vs. 100 separate endpoints ≈ $20,100/mo.
- Include when the SOW mentions SaaS ML, one model per customer / tenant, many small models, or multi-tenant ML hosting.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns MME + router/upload Lambdas + models bucket | **§3 Monolith Variant** |
| Bucket in `DataLakeStack`, SageMaker role in `MLPlatformStack`, MME + Lambdas in `MMEStack` | **§4 Micro-Stack Variant** |

**Why the split matters.** The router Lambda needs `sagemaker:InvokeEndpoint` on the endpoint ARN (local), and the upload Lambda needs `s3:GetObject` / `s3:PutObject` on the models bucket (cross-stack in prod). Monolith uses L2 grants; micro-stack uses identity-side `PolicyStatement` with bucket name from SSM.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Why MME?

```
Normal approach: one endpoint per model
  Customer A → endpoint-A ($0.28/hr per ml.m5.large ≈ $201/mo)
  Customer B → endpoint-B
  ...
  100 customers ≈ $20,100 / month

Multi-Model Endpoint: 100 models on ONE endpoint
  100 customers → ONE endpoint ≈ $201 / month
  ~99% cost reduction
  Warm model:    < 5 ms
  Cold model:    1–3 s load
  LRU eviction:  automatic
```

### 3.2 CDK — MME config + endpoint + Lambdas

```python
from aws_cdk import (
    Aws, Duration, CfnOutput,
    aws_sagemaker as sagemaker,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ec2 as ec2,
)


def _create_multi_model_endpoint(self, stage_name: str) -> None:
    """Assumes self.{vpc, lambda_sg, kms_key, lake_buckets, sagemaker_role} set earlier."""

    models_s3_uri = f"s3://{self.lake_buckets['curated'].bucket_name}/models/"

    # MME config — N models, one endpoint config
    mme_endpoint_config = sagemaker.CfnEndpointConfig(
        self, "MMEEndpointConfig",
        endpoint_config_name=f"{{project_name}}-mme-{stage_name}",
        kms_key_id=self.kms_key.key_arn,
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="AllTraffic",
                instance_type="ml.m5.2xlarge",                 # enough RAM for N warm models
                initial_instance_count=1 if stage_name != "prod" else 2,
                model_name=f"{{project_name}}-mme-container-{stage_name}",
                routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                    routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                ),
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    status="ENABLED",
                    min_instance_count=1,
                    max_instance_count=10 if stage_name == "prod" else 2,
                ),
            ),
        ],
    )

    self.mme_endpoint = sagemaker.CfnEndpoint(
        self, "MMEEndpoint",
        endpoint_name=f"{{project_name}}-mme-{stage_name}",
        endpoint_config_name=mme_endpoint_config.endpoint_config_name,
    )

    # Router Lambda — resolves TargetModel from tenant_id
    mme_router = _lambda.Function(
        self, "MMERouter",
        function_name=f"{{project_name}}-mme-router-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/mme_router"),
        timeout=Duration.seconds(30),
        memory_size=256,
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
        environment={
            "ENDPOINT_NAME":  self.mme_endpoint.endpoint_name,
            "MODELS_S3_PATH": models_s3_uri,
        },
    )
    mme_router.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{{project_name}}-mme-{stage_name}"],
    ))

    # Model Upload Lambda — copies approved artifact into MME S3 prefix
    mme_upload = _lambda.Function(
        self, "MMEModelUpload",
        function_name=f"{{project_name}}-mme-model-upload-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_asset("lambda/mme_upload"),
        timeout=Duration.seconds(60),
        environment={
            "MODELS_BUCKET": self.lake_buckets["curated"].bucket_name,
            "MODELS_PREFIX": "models/",
        },
    )
    self.lake_buckets["curated"].grant_read_write(mme_upload)   # safe in monolith

    CfnOutput(self, "MMEEndpointName",   value=self.mme_endpoint.endpoint_name)
    CfnOutput(self, "MMERouterArn",      value=mme_router.function_arn)
    CfnOutput(self, "MMEModelUploadArn", value=mme_upload.function_arn)
```

### 3.3 Router handler (`lambda/mme_router/index.py`)

```python
"""MME router — resolves TargetModel from tenant_id and calls InvokeEndpoint."""
import boto3, json, logging, os

logger     = logging.getLogger()
sm_runtime = boto3.client('sagemaker-runtime')
ENDPOINT_NAME = os.environ['ENDPOINT_NAME']


def handler(event, context):
    tenant_id     = event.get('tenant_id') or event.get('headers', {}).get('X-Tenant-ID')
    payload       = event.get('payload') or event.get('body', '{}')
    model_version = event.get('model_version', 'latest')
    if not tenant_id:
        return {'statusCode': 400, 'body': 'Missing tenant_id'}

    target_model = f"{tenant_id}/{model_version}/model.tar.gz"
    try:
        response = sm_runtime.invoke_endpoint(
            EndpointName=ENDPOINT_NAME,
            TargetModel=target_model,                           # SM loads if not warm
            ContentType='application/json',
            Body=payload if isinstance(payload, str) else json.dumps(payload),
            Accept='application/json',
        )
        return {
            'statusCode': 200,
            'body':       response['Body'].read().decode('utf-8'),
            'headers':    {'X-Model-Used': target_model},
        }
    except sm_runtime.exceptions.ModelNotReadyException:
        return {
            'statusCode': 503,
            'body':       json.dumps({'error': 'Model loading, retry in 3s'}),
            'headers':    {'Retry-After': '3'},
        }
    except Exception as e:
        logger.error("MME error: %s", e)
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
```

### 3.4 Upload handler (`lambda/mme_upload/index.py`)

```python
"""Copy an approved model.tar.gz into the MME S3 prefix (models/{tenant}/{version}/)."""
import boto3, logging, os

logger        = logging.getLogger()
s3            = boto3.client('s3')
MODELS_BUCKET = os.environ['MODELS_BUCKET']
MODELS_PREFIX = os.environ['MODELS_PREFIX']


def handler(event, context):
    tenant_id     = event['tenant_id']
    model_version = event.get('model_version', 'latest')
    source_uri    = event['source_s3_uri']                  # e.g. s3://registry-bucket/.../model.tar.gz

    src_bucket, src_key = source_uri.replace('s3://', '').split('/', 1)
    dst_key = f"{MODELS_PREFIX}{tenant_id}/{model_version}/model.tar.gz"
    s3.copy_object(
        Bucket=MODELS_BUCKET,
        CopySource={'Bucket': src_bucket, 'Key': src_key},
        Key=dst_key,
    )
    logger.info("Copied %s -> s3://%s/%s", source_uri, MODELS_BUCKET, dst_key)
    return {
        'statusCode':   200,
        'target_model': f"{tenant_id}/{model_version}/model.tar.gz",
        'message':      'Model registered. First call will load it (1–3s cold start).',
    }
```

### 3.5 Monolith gotchas

- **Model naming convention is load-bearing.** The router builds `TargetModel=f"{tenant}/{version}/model.tar.gz"` — the upload path MUST match. Bake this into both Lambdas via the `MODELS_PREFIX` env var.
- **Instance RAM sizing.** With `ml.m5.2xlarge` (32 GB) + typical 50–200 MB models, you can keep ~100 models warm. For larger models use memory-optimized (`ml.r5.*`).
- **`ModelNotReadyException`** returns 200 to the client on some SDK versions unless you explicitly catch it. Always handle explicitly + return 503 + `Retry-After` (as above).
- **Model artifacts cannot be mutated in place.** Changing `s3://.../tenant-X/v1/model.tar.gz` after SM has loaded it causes stale behaviour. Use new version paths (`/v2/`) and update the caller's `model_version`.
- **`InvokeEndpoint` TargetModel is a PATH, not an ARN.** Relative to the S3 prefix configured on the model container. Getting this wrong returns `ValidationException: Cannot find TargetModel`.

---

## 4. Micro-Stack Variant

**Use when:** buckets / KMS / roles live in other stacks.

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)`.
2. **Never call `bucket.grant_read_write(fn)`** across stacks — identity-side `PolicyStatement`.
3. **Never target cross-stack queues** — not relevant here.
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on endpoint config — use KMS ARN string from SSM.

### 4.2 `MMEStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sagemaker as sagemaker,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class MMEStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        lake_bucket_curated_ssm: str,
        lake_key_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-mme-{stage_name}", **kwargs)

        curated_bucket = ssm.StringParameter.value_for_string_parameter(self, lake_bucket_curated_ssm)
        lake_key_arn   = ssm.StringParameter.value_for_string_parameter(self, lake_key_arn_ssm)
        models_s3_uri  = f"s3://{curated_bucket}/models/"

        endpoint_config = sagemaker.CfnEndpointConfig(
            self, "MMEEndpointConfig",
            endpoint_config_name=f"{{project_name}}-mme-{stage_name}",
            kms_key_id=lake_key_arn,            # STRING (fifth non-negotiable)
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    instance_type="ml.m5.2xlarge",
                    initial_instance_count=1 if stage_name != "prod" else 2,
                    model_name=f"{{project_name}}-mme-container-{stage_name}",
                    routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                        routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                    ),
                    managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                        status="ENABLED",
                        min_instance_count=1,
                        max_instance_count=10 if stage_name == "prod" else 2,
                    ),
                ),
            ],
        )

        endpoint = sagemaker.CfnEndpoint(
            self, "MMEEndpoint",
            endpoint_name=f"{{project_name}}-mme-{stage_name}",
            endpoint_config_name=endpoint_config.endpoint_config_name,
        )

        # Router Lambda
        router_log = logs.LogGroup(self, "RouterLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-mme-router-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        router = _lambda.Function(self, "MMERouter",
            function_name=f"{{project_name}}-mme-router-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "mme_router")),
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=router_log,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            environment={
                "ENDPOINT_NAME":  endpoint.endpoint_name,
                "MODELS_S3_PATH": models_s3_uri,
            },
        )
        router.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint.endpoint_name}"],
        ))
        iam.PermissionsBoundary.of(router.role).apply(permission_boundary)

        # Upload Lambda
        upload_log = logs.LogGroup(self, "UploadLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-mme-model-upload-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        upload = _lambda.Function(self, "MMEUpload",
            function_name=f"{{project_name}}-mme-model-upload-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "mme_upload")),
            timeout=Duration.seconds(60),
            log_group=upload_log,
            environment={
                "MODELS_BUCKET": curated_bucket,
                "MODELS_PREFIX": "models/",
            },
        )
        # Identity-side S3 grant (cross-stack safe)
        upload.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=["arn:aws:s3:::*"],          # source bucket is arbitrary at runtime
        ))
        upload.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:PutObjectAcl", "s3:AbortMultipartUpload"],
            resources=[f"arn:aws:s3:::{curated_bucket}/models/*"],
        ))
        upload.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Encrypt", "kms:GenerateDataKey", "kms:Decrypt"],
            resources=[lake_key_arn],
        ))
        iam.PermissionsBoundary.of(upload.role).apply(permission_boundary)

        CfnOutput(self, "MMEEndpointName",   value=endpoint.endpoint_name)
        CfnOutput(self, "MMERouterArn",      value=router.function_arn)
        CfnOutput(self, "MMEUploadArn",      value=upload.function_arn)

        # Publish endpoint name for downstream (API Gateway, etc.)
        ssm.StringParameter(self, "MMEEndpointNameParam",
            parameter_name=f"/{{project_name}}/ml/mme_endpoint_name",
            string_value=endpoint.endpoint_name,
        )
```

### 4.3 Micro-stack gotchas

- **Upload Lambda `s3:GetObject` on `*`** — the source bucket for approved models might come from any registry location (e.g. the training job's output S3 prefix). Scope via a `Condition` on `aws:ResourceTag/Project` if the registry tags artifacts.
- **`kms:Encrypt` on the lake KMS key** — the upload Lambda's `copy_object` re-encrypts with the destination bucket's KMS. If the source bucket uses a different KMS, also add `Decrypt` on that key.
- **Router `VpcSubnetSelection`** — MME endpoints have a managed internet connection via SageMaker's own ENIs; the router Lambda only needs VPC if its callers are VPC-bound. If all callers are API Gateway, drop VPC config for faster cold start.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx | §4 Micro-Stack |
| Model memory exceeds instance RAM | Bigger instance (`ml.r5.*`) or shard tenants across multiple MMEs |
| Long cold starts annoy users | Keep a "warmer" Lambda that periodically hits rare tenant models |
| Tenant isolation / compliance | Separate MME per compliance zone; keep the router's routing table scoped accordingly |
| Different framework per tenant | Multiple models per `TargetModel` group via a multi-framework container (e.g. SageMaker Triton) |
| Need per-tenant auto-scaling | MME doesn't scale per model; either accept coarse-grained scaling or split to individual endpoints for heavy tenants |

---

## 6. Worked example — MMEStack synthesizes

Save as `tests/sop/test_MLOPS_MULTI_MODEL_ENDPOINT.py`. Offline.

```python
"""SOP verification — MMEStack synthesizes endpoint, router, upload Lambdas."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_mme_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg   = ec2.SecurityGroup(deps, "Sg", vpc=vpc)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.mme_stack import MMEStack
    stack = MMEStack(
        app, stage_name="prod",
        vpc=vpc, lambda_sg=sg,
        lake_bucket_curated_ssm="/test/lake/curated_bucket",
        lake_key_arn_ssm="/test/lake/kms_key_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::SageMaker::EndpointConfig", 1)
    t.resource_count_is("AWS::SageMaker::Endpoint",       1)
    t.resource_count_is("AWS::Lambda::Function",          2)   # router + upload
    t.resource_count_is("AWS::SSM::Parameter",            1)
```

---

## 7. References

- `docs/template_params.md` — `MME_INSTANCE_TYPE`, `MME_MIN_INSTANCES`, `MME_MAX_INSTANCES`, `MODELS_PREFIX`, `MME_ENDPOINT_NAME_SSM`
- `docs/Feature_Roadmap.md` — feature IDs `ML-18` (multi-model endpoint), `ML-19` (per-tenant model hosting)
- SageMaker MME: https://docs.aws.amazon.com/sagemaker/latest/dg/multi-model-endpoints.html
- `InvokeEndpoint` with `TargetModel`: https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_runtime_InvokeEndpoint.html
- Related SOPs: `MLOPS_SAGEMAKER_SERVING` (single-model endpoints), `MLOPS_SAGEMAKER_TRAINING` (model artifact source), `LAYER_BACKEND_LAMBDA` (five non-negotiables)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — MMEStack reads curated bucket + KMS ARN via SSM; `kms_key_id=` takes the string (fifth non-negotiable); identity-side grants for `sagemaker:InvokeEndpoint`, scoped `s3:PutObject` on the models prefix, `kms:Encrypt/Decrypt` on the lake key. Extracted router + upload handlers to asset files. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — MME config, router Lambda inline, upload Lambda inline, outputs. |
