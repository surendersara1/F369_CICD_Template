# PARTIAL: Recommender System Pipeline — Collaborative Filtering, Two-Tower Model

**Usage:** Include when SOW mentions product recommendations, content personalization, similar items, user preferences, collaborative filtering, or "customers also bought."

---

## Architecture

```
OFFLINE (daily training pipeline):
  User events (clicks, purchases, ratings) → S3 → Processing → Two-Tower training →
  → Item embeddings → FAISS/OpenSearch kNN index → DynamoDB pre-computed recs

ONLINE (real-time, <50ms):
  User ID → Recommendations Lambda →
    Path A: DynamoDB pre-computed recs (fastest, <5ms, stale up to 24h)
    Path B: Feature Store → SageMaker endpoint → real-time scoring (fresh, ~50ms)
  → Ranked by CTR prediction model → Return top-N
```

---

## CDK Code Block

```python
def _create_recommendations_pipeline(self, stage_name: str) -> None:
    """
    Recommender System Pipeline.

    Approaches (detected from Architecture Map):
      A) Two-Tower Model (best for large catalogs, cold-start handling)
      B) Matrix Factorization (classic CF — good for small catalogs)
      C) Behavioral Sequence (BERT4Rec — session-based recommendations)

    Pipeline:
      1. Feature prep (user/item features, interaction matrix)
      2. Train Two-Tower (user encoder + item encoder)
      3. Build kNN index (item embeddings → OpenSearch)
      4. Pre-compute recommendations for all users (batch) → DynamoDB
      5. Deploy real-time re-ranking endpoint
    """

    # =========================================================================
    # PRE-COMPUTED RECOMMENDATIONS TABLE (DynamoDB)
    # Key: user_id → recs: [{item_id, score}, ...] top 100
    # =========================================================================

    recs_table = ddb.Table(
        self, "RecommendationsTable",
        table_name=f"{{project_name}}-recommendations-{stage_name}",
        partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery=True,
        encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=self.kms_key,
        time_to_live_attribute="expires_at",  # Recs expire, forcing refresh
        removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
    )

    # =========================================================================
    # RECOMMENDATIONS LAMBDA — Hybrid: pre-computed + real-time re-ranking
    # =========================================================================

    recs_fn = _lambda.Function(
        self, "RecommendationsFn",
        function_name=f"{{project_name}}-recommendations-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging, time
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb   = boto3.resource('dynamodb')
sm_runtime = boto3.client('sagemaker-runtime')

RECS_TABLE     = dynamodb.Table(os.environ['RECS_TABLE'])
ENDPOINT_NAME  = os.environ['RERANK_ENDPOINT_NAME']
TOP_N          = int(os.environ.get('TOP_N', '20'))
USE_REALTIME   = os.environ.get('USE_REALTIME_RERANK', 'true').lower() == 'true'

def handler(event, context):
    user_id  = event.get('user_id')
    context_ = event.get('context', {})   # Page context (category, search query, etc.)
    n        = event.get('n', TOP_N)

    # Step 1: Get pre-computed candidates from DynamoDB (<5ms)
    response = RECS_TABLE.get_item(Key={'user_id': user_id})
    candidates = response.get('Item', {}).get('recommendations', [])

    if not candidates:
        # Cold-start user: fall back to popular items
        response = RECS_TABLE.get_item(Key={'user_id': '__popular__'})
        candidates = response.get('Item', {}).get('recommendations', [])
        logger.info(f"Cold-start user {user_id}, using popular items")

    # Step 2: Real-time re-ranking (optional — sort by contextual relevance)
    if USE_REALTIME and candidates and ENDPOINT_NAME:
        payload = json.dumps({
            'user_id': user_id,
            'candidates': candidates[:50],  # Re-rank top 50 candidates
            'context': context_,
        })
        resp = sm_runtime.invoke_endpoint(
            EndpointName=ENDPOINT_NAME,
            ContentType='application/json',
            Body=payload,
            Accept='application/json',
        )
        ranked = json.loads(resp['Body'].read())
        candidates = ranked.get('ranked_candidates', candidates)

    return {
        'statusCode': 200,
        'user_id': user_id,
        'recommendations': candidates[:n],
        'total_candidates': len(candidates),
        'cold_start': not bool(response.get('Item')),
    }
"""),
        environment={
            "RECS_TABLE":             recs_table.table_name,
            "RERANK_ENDPOINT_NAME":   f"{{project_name}}-rerank-inference-{stage_name}",
            "TOP_N":                  "20",
            "USE_REALTIME_RERANK":    "true" if stage_name == "prod" else "false",
        },
        memory_size=256,
        timeout=Duration.seconds(3),
        tracing=_lambda.Tracing.ACTIVE,
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        security_groups=[self.lambda_sg],
    )
    recs_table.grant_read_data(recs_fn)
    recs_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpoint"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-rerank-inference-{stage_name}"],
    ))

    # =========================================================================
    # BATCH PRE-COMPUTE LAMBDA
    # Run nightly: fetch user embeddings from SageMaker, ANN search, write DDB
    # =========================================================================

    precompute_fn = _lambda.Function(
        self, "RecsPrecomputeFn",
        function_name=f"{{project_name}}-recs-precompute-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime, timedelta

logger = logging.getLogger()
sm = boto3.client('sagemaker')

def handler(event, context):
    # Trigger the batch transform job to score all users
    resp = sm.create_transform_job(
        TransformJobName=f"recs-precompute-{datetime.utcnow().strftime('%Y%m%d%H%M')}",
        ModelName=os.environ['MODEL_NAME'],
        BatchStrategy='MultiRecord',
        MaxPayloadInMB=6,
        TransformInput={
            'DataSource': {'S3DataSource': {
                'S3DataType': 'S3Prefix',
                'S3Uri': os.environ['USER_FEATURES_URI'],
            }},
            'ContentType': 'application/json',
            'SplitType': 'Line',
        },
        TransformOutput={
            'S3OutputPath': os.environ['OUTPUT_URI'],
            'Accept': 'application/json',
            'AssembleWith': 'Line',
        },
        TransformResources={
            'InstanceType': 'ml.m5.2xlarge',
            'InstanceCount': 2,    # Parallelize across users
        },
    )
    logger.info(f"Batch transform started: {resp['TransformJobArn']}")
    return {"transform_job_arn": resp["TransformJobArn"]}
"""),
        environment={
            "MODEL_NAME":        f"{{project_name}}-twotower-{stage_name}",
            "USER_FEATURES_URI": f"s3://{self.lake_buckets['features'].bucket_name}/user-features/",
            "OUTPUT_URI":        f"s3://{self.lake_buckets['curated'].bucket_name}/recs-output/",
        },
        timeout=Duration.seconds(30),
    )
    precompute_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:CreateTransformJob"],
        resources=["*"],
    ))

    # Schedule nightly pre-computation at 2am
    events.Rule(self, "NightlyRecsPrecompute",
        schedule=events.Schedule.cron(hour="2", minute="0"),
        targets=[targets.LambdaFunction(precompute_fn)],
        enabled=stage_name != "ds",
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "RecommendationsFnArn",
        value=recs_fn.function_arn,
        description="Call with user_id to get personalized recommendations",
        export_name=f"{{project_name}}-recommendations-fn-{stage_name}",
    )
    CfnOutput(self, "RecsTableName",
        value=recs_table.table_name,
        description="DynamoDB table holding pre-computed recommendations per user",
        export_name=f"{{project_name}}-recs-table-{stage_name}",
    )
```
