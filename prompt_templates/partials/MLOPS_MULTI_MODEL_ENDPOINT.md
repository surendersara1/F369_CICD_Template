# PARTIAL: Multi-Model Endpoint — Host 50-100+ Models on One Instance

**Usage:** Include when SOW mentions SaaS ML, one model per customer/tenant, cost optimization for many small models, or model versioning across tenants.

---

## Why Multi-Model Endpoint (MME)?

```
Normal approach: One endpoint per model
  Customer A → endpoint-A ($0.28/hr per ml.m5.large = $201/mo per model)
  Customer B → endpoint-B
  Customer C → endpoint-C
  100 customers = $20,100/month just in endpoints

Multi-Model Endpoint: 100 models on ONE endpoint
  100 customers → ONE endpoint ($201/mo total)
  99% cost reduction
  SageMaker loads models on-demand, evicts LRU models from memory
  Warm models: <5ms, cold model load: 1-3 seconds
```

---

## CDK Code Block

```python
def _create_multi_model_endpoint(self, stage_name: str) -> None:
    """
    SageMaker Multi-Model Endpoint (MME).
    Hosts N models on one endpoint instance — massive cost saving for SaaS.

    Use cases:
      - One fraud model per bank customer (100s of banks)
      - One recommendation model per retailer
      - One NLP classifier per enterprise tenant (different label sets)
      - A/B test many model variants without separate endpoints

    Model routing: caller passes TargetModel header → SM routes to correct model
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # S3 prefix where all model artifacts live
    # Each model is a separate .tar.gz at: s3://bucket/models/{tenant_id}/model.tar.gz
    models_s3_uri = f"s3://{self.lake_buckets['curated'].bucket_name}/models/"

    # =========================================================================
    # MULTI-MODEL ENDPOINT CONFIG
    # =========================================================================

    mme_endpoint_config = sagemaker.CfnEndpointConfig(
        self, "MMEEndpointConfig",
        endpoint_config_name=f"{{project_name}}-mme-{stage_name}",
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="AllTraffic",
                instance_type="ml.m5.2xlarge",    # Enough RAM to hold N models in memory
                initial_instance_count=1 if stage_name != "prod" else 2,

                # Multi-model serving configuration
                model_name=f"{{project_name}}-mme-container-{stage_name}",

                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    status="ENABLED",
                    min_instance_count=1,
                    max_instance_count=10 if stage_name == "prod" else 2,
                ),
                routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                    routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                ),
            )
        ],
        kms_key_id=self.kms_key.key_arn,
    )

    self.mme_endpoint = sagemaker.CfnEndpoint(
        self, "MMEEndpoint",
        endpoint_name=f"{{project_name}}-mme-{stage_name}",
        endpoint_config_name=mme_endpoint_config.endpoint_config_name,
        tags=[{"key": "Project", "value": "{{project_name}}"}, {"key": "Type", "value": "MultiModel"}],
    )

    # =========================================================================
    # MME ROUTER LAMBDA
    # Abstracts the TargetModel routing from callers
    # =========================================================================

    mme_router_fn = _lambda.Function(
        self, "MMERouter",
        function_name=f"{{project_name}}-mme-router-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
sm_runtime = boto3.client('sagemaker-runtime')

ENDPOINT_NAME  = os.environ['ENDPOINT_NAME']
MODELS_S3_PATH = os.environ['MODELS_S3_PATH']

def handler(event, context):
    tenant_id    = event.get('tenant_id') or event.get('headers', {}).get('X-Tenant-ID')
    payload      = event.get('payload') or event.get('body', '{}')
    model_version = event.get('model_version', 'latest')

    if not tenant_id:
        return {'statusCode': 400, 'body': 'Missing tenant_id'}

    # TargetModel = relative S3 path within the models prefix
    target_model = f"{tenant_id}/{model_version}/model.tar.gz"

    try:
        response = sm_runtime.invoke_endpoint(
            EndpointName=ENDPOINT_NAME,
            TargetModel=target_model,   # SM loads this model if not already in memory
            ContentType='application/json',
            Body=payload if isinstance(payload, str) else json.dumps(payload),
            Accept='application/json',
        )
        result = json.loads(response['Body'].read())
        return {
            'statusCode': 200,
            'body': json.dumps(result),
            'headers': {'X-Model-Used': target_model},
        }
    except sm_runtime.exceptions.ModelNotReadyException:
        return {'statusCode': 503, 'body': json.dumps({'error': 'Model loading, retry in 3s'}),
                'headers': {'Retry-After': '3'}}
    except Exception as e:
        logger.error(f"MME error: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
"""),
        environment={
            "ENDPOINT_NAME":  f"{{project_name}}-mme-{stage_name}",
            "MODELS_S3_PATH": models_s3_uri,
        },
        memory_size=256,
        timeout=Duration.seconds(30),
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
    )
    mme_router_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-mme-{stage_name}"],
    ))

    # =========================================================================
    # MODEL UPLOAD LAMBDA — Register a new tenant model into the MME
    # =========================================================================

    model_upload_fn = _lambda.Function(
        self, "MMEModelUpload",
        function_name=f"{{project_name}}-mme-model-upload-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
logger = logging.getLogger()
s3  = boto3.client('s3')
sm  = boto3.client('sagemaker')

MODELS_BUCKET = os.environ['MODELS_BUCKET']
MODELS_PREFIX = os.environ['MODELS_PREFIX']

def handler(event, context):
    # Upload model artifact to the MME S3 prefix
    # Model artifact must be a valid SageMaker model.tar.gz
    tenant_id     = event['tenant_id']
    model_version = event.get('model_version', 'latest')
    source_uri    = event['source_s3_uri']  # Where the model artifact currently is

    src_bucket, src_key = source_uri.replace('s3://', '').split('/', 1)
    dst_key = f"{MODELS_PREFIX}{tenant_id}/{model_version}/model.tar.gz"

    s3.copy_object(
        Bucket=MODELS_BUCKET,
        CopySource={'Bucket': src_bucket, 'Key': src_key},
        Key=dst_key,
    )
    logger.info(f"Copied {source_uri} -> s3://{MODELS_BUCKET}/{dst_key}")
    return {
        'statusCode': 200,
        'target_model': f"{tenant_id}/{model_version}/model.tar.gz",
        'message': 'Model registered in MME. First call will load it (1-3s cold start).',
    }
"""),
        environment={
            "MODELS_BUCKET": self.lake_buckets["curated"].bucket_name,
            "MODELS_PREFIX": "models/",
        },
        timeout=Duration.seconds(60),
    )
    self.lake_buckets["curated"].grant_read_write(model_upload_fn)

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "MMEEndpointName",
        value=self.mme_endpoint.endpoint_name,
        description="Multi-Model Endpoint name",
        export_name=f"{{project_name}}-mme-{stage_name}",
    )
    CfnOutput(self, "MMERouterArn",
        value=mme_router_fn.function_arn,
        description="MME Router Lambda — pass tenant_id + payload",
        export_name=f"{{project_name}}-mme-router-{stage_name}",
    )
    CfnOutput(self, "MMEModelUploadArn",
        value=model_upload_fn.function_arn,
        description="Lambda to register a new tenant model into the MME",
        export_name=f"{{project_name}}-mme-upload-{stage_name}",
    )
```
