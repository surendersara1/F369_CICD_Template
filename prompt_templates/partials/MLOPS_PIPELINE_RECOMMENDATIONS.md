# SOP — MLOps Pipeline: Recommendations (Two-Tower / CF / Session, Hybrid Serving)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · DynamoDB pre-computed recs · SageMaker real-time re-rank endpoint · Batch Transform nightly precompute · Lambda with VPC + provisioned

---

## 1. Purpose

- Provision a hybrid recommender: DynamoDB pre-computed top-N (< 5 ms path) + optional SageMaker real-time re-ranking endpoint (~50 ms path) for contextual relevance.
- Codify the **Recommendations DynamoDB table**: `user_id` PK, CMK-encrypted, PITR, TTL-driven `expires_at` to force refresh.
- Codify the **nightly Batch Transform precompute Lambda** + EventBridge schedule (2 am) — `sagemaker:CreateTransformJob` over user features → output to curated lake.
- Codify the **serving Lambda**: DynamoDB lookup → cold-start fallback to `__popular__` → optional real-time re-rank on top 50 candidates.
- Include when the SOW mentions product recommendations, content personalisation, similar items, user preferences, collaborative filtering, or "customers also bought".

**Approach selection:**

| Scenario | Approach |
|---|---|
| Large catalog, cold-start handling | Two-Tower model (user encoder + item encoder → FAISS/OpenSearch kNN) |
| Small catalog | Matrix factorisation (classic CF) |
| Session-based (clickstream) | BERT4Rec / GRU4Rec behavioural sequence |
| Add contextual re-rank | Train a CTR model that consumes candidates + page context |

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single stack owns DDB table + serving Lambda + precompute Lambda + schedule + endpoint | **§3 Monolith Variant** |
| `DataStack` owns DDB, `MLPlatformStack` owns Feature Store + Model Group, `ServingStack` owns endpoint, `RecommendationsStack` owns the two Lambdas + schedule | **§4 Micro-Stack Variant** |

**Why the split matters.** The serving Lambda needs `dynamodb:GetItem` on the recs table (owned by `DataStack`) and `sagemaker:InvokeEndpoint` on the re-rank endpoint (owned by `ServingStack`). The precompute Lambda needs `sagemaker:CreateTransformJob` (resource-unscoped) and `iam:PassRole` on the SageMaker execution role. Monolith: `table.grant_read_data(fn)` is local. Micro-stack: cross-stack `grant_read_data` edits the **IAM role** (local to function stack), not the table, so L2 is actually safe here — but the KMS decrypt needs identity-side with the lake key ARN from SSM. The DDB table belongs in `RecommendationsStack` rather than a shared `DataStack` to avoid cross-stack KMS circular refs.

---

## 3. Monolith Variant

**Use when:** POC / single stack.

### 3.1 Architecture

```
OFFLINE (daily training pipeline):
  User events (clicks, purchases, ratings) → S3 → Processing → Two-Tower training →
  → Item embeddings → FAISS/OpenSearch kNN index → DynamoDB pre-computed recs

ONLINE (real-time, < 50 ms):
  User ID → Recommendations Lambda →
    Path A: DynamoDB pre-computed recs (fastest, < 5 ms, stale up to 24 h)
    Path B: Feature Store → SageMaker endpoint → real-time scoring (fresh, ~50 ms)
  → Ranked by CTR prediction model → Return top-N

NIGHTLY (2 am):
  EventBridge → RecsPrecompute Lambda → SageMaker Batch Transform →
  user features → user embeddings → kNN → DDB upsert with expires_at
```

### 3.2 CDK — `_create_recommendations_pipeline` method body

```python
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy,
    aws_dynamodb as ddb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
)


def _create_recommendations_pipeline(self, stage_name: str) -> None:
    """
    Recommender System Pipeline.

    Assumes self.{vpc, lambda_sg, kms_key, lake_buckets} set.

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
        code=_lambda.Code.from_asset("lambda/recommendations_serve"),
        environment={
            "RECS_TABLE":           recs_table.table_name,
            "RERANK_ENDPOINT_NAME": f"{{project_name}}-rerank-inference-{stage_name}",
            "TOP_N":                "20",
            "USE_REALTIME_RERANK":  "true" if stage_name == "prod" else "false",
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
        code=_lambda.Code.from_asset("lambda/recommendations_precompute"),
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

### 3.3 Serving handler (`lambda/recommendations_serve/index.py`)

```python
"""Hybrid recommendations serving: DDB pre-computed + optional real-time re-rank."""
import boto3, json, logging, os, time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb   = boto3.resource('dynamodb')
sm_runtime = boto3.client('sagemaker-runtime')

RECS_TABLE    = dynamodb.Table(os.environ['RECS_TABLE'])
ENDPOINT_NAME = os.environ['RERANK_ENDPOINT_NAME']
TOP_N         = int(os.environ.get('TOP_N', '20'))
USE_REALTIME  = os.environ.get('USE_REALTIME_RERANK', 'true').lower() == 'true'


def handler(event, context):
    user_id  = event.get('user_id')
    context_ = event.get('context', {})   # Page context (category, search query, etc.)
    n        = event.get('n', TOP_N)

    # Step 1: Get pre-computed candidates from DynamoDB (< 5 ms)
    response = RECS_TABLE.get_item(Key={'user_id': user_id})
    candidates = response.get('Item', {}).get('recommendations', [])
    cold_start = not bool(response.get('Item'))

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
        'cold_start': cold_start,
    }
```

### 3.4 Precompute handler (`lambda/recommendations_precompute/index.py`)

```python
"""Nightly batch transform to precompute recs for all users."""
import boto3, logging, os
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)
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
            'S3OutputPath':  os.environ['OUTPUT_URI'],
            'Accept':        'application/json',
            'AssembleWith':  'Line',
        },
        TransformResources={
            'InstanceType':  'ml.m5.2xlarge',
            'InstanceCount': 2,    # Parallelize across users
        },
    )
    logger.info(f"Batch transform started: {resp['TransformJobArn']}")
    return {"transform_job_arn": resp["TransformJobArn"]}
```

### 3.5 Monolith gotchas

- **TTL `expires_at` is seconds-since-epoch** — not ISO-8601. DDB deletes on a best-effort schedule (up to 48 h late); design for stale recs.
- **Cold-start `__popular__` row** must be maintained by the precompute job — add an explicit "aggregate popular items" step or recs for new users will be empty.
- **`sagemaker:InvokeEndpoint` fails when endpoint doesn't exist** — gate the call with `USE_REALTIME_RERANK=false` until the endpoint is deployed by `MLOPS_SAGEMAKER_SERVING`.
- **`CreateTransformJob` requires `sagemaker:PassRole`** on the SageMaker execution role ARN — not in the snippet above; add it when the role is in the same stack.
- **Batch Transform `BatchStrategy='MultiRecord'`** packs multiple records per payload — correct for short JSONL; switch to `SingleRecord` for records > 5 MB.
- **VPC Lambda cold start** — add `reservedConcurrency` + provisioned concurrency for < 5 ms recs path; the DDB lookup is fast but the cold start isn't.

---

## 4. Micro-Stack Variant

**Use when:** `RecommendationsStack` is separate from `ServingStack` (owns re-rank endpoint) and `MLPlatformStack` (owns Two-Tower model + SageMaker role).

### 4.1 The five non-negotiables

1. **Anchor Lambda assets** to `Path(__file__)` via `_LAMBDAS_ROOT`.
2. **Never call `endpoint.grant_invoke(fn)`** across stacks — identity-side `sagemaker:InvokeEndpoint` scoped to the endpoint ARN (read from SSM).
3. **Never target cross-stack queues** — not relevant (the precompute schedule → local Lambda).
4. **Never split a bucket + OAC** — not relevant.
5. **Never set `encryption_key=ext_key`** on the DDB table when the key is cross-stack — keep the recs table + a local CMK inside this stack.

### 4.2 `RecommendationsStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy,
    aws_dynamodb as ddb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class RecommendationsStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        rerank_endpoint_name_ssm: str,
        twotower_model_name_ssm: str,
        features_bucket_name_ssm: str,
        curated_bucket_name_ssm: str,
        sagemaker_role_arn_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-recommendations-{stage_name}", **kwargs)

        endpoint_name    = ssm.StringParameter.value_for_string_parameter(self, rerank_endpoint_name_ssm)
        model_name       = ssm.StringParameter.value_for_string_parameter(self, twotower_model_name_ssm)
        features_bucket  = ssm.StringParameter.value_for_string_parameter(self, features_bucket_name_ssm)
        curated_bucket   = ssm.StringParameter.value_for_string_parameter(self, curated_bucket_name_ssm)
        sagemaker_role   = ssm.StringParameter.value_for_string_parameter(self, sagemaker_role_arn_ssm)

        # Local CMK for DDB (fifth non-negotiable — no cross-stack key)
        cmk = kms.Key(self, "RecsKey",
            alias=f"alias/{{project_name}}-recs-{stage_name}",
            enable_key_rotation=True, rotation_period=Duration.days(365),
        )

        recs_table = ddb.Table(self, "RecommendationsTable",
            table_name=f"{{project_name}}-recommendations-{stage_name}",
            partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        )

        # Serving Lambda
        serve_log = logs.LogGroup(self, "ServeLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-recommendations-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        recs_fn = _lambda.Function(self, "RecommendationsFn",
            function_name=f"{{project_name}}-recommendations-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "recommendations_serve")),
            timeout=Duration.seconds(3),
            memory_size=256,
            log_group=serve_log,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            environment={
                "RECS_TABLE":           recs_table.table_name,
                "RERANK_ENDPOINT_NAME": endpoint_name,
                "TOP_N":                "20",
                "USE_REALTIME_RERANK":  "true" if stage_name == "prod" else "false",
            },
        )
        recs_table.grant_read_data(recs_fn)       # same-stack L2 safe
        recs_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{endpoint_name}"],
        ))
        iam.PermissionsBoundary.of(recs_fn.role).apply(permission_boundary)

        # Precompute Lambda
        pre_log = logs.LogGroup(self, "PrecomputeLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-recs-precompute-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        precompute_fn = _lambda.Function(self, "RecsPrecomputeFn",
            function_name=f"{{project_name}}-recs-precompute-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "recommendations_precompute")),
            timeout=Duration.seconds(30),
            log_group=pre_log,
            environment={
                "MODEL_NAME":        model_name,
                "USER_FEATURES_URI": f"s3://{features_bucket}/user-features/",
                "OUTPUT_URI":        f"s3://{curated_bucket}/recs-output/",
            },
        )
        precompute_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob"],
            resources=[f"arn:aws:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:transform-job/recs-precompute-*"],
        ))
        # PassRole with PassedToService condition
        precompute_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[sagemaker_role],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))
        iam.PermissionsBoundary.of(precompute_fn.role).apply(permission_boundary)

        events.Rule(self, "NightlyRecsPrecompute",
            rule_name=f"{{project_name}}-nightly-recs-precompute-{stage_name}",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            targets=[targets.LambdaFunction(precompute_fn)],
            enabled=stage_name != "ds",
        )

        cdk.CfnOutput(self, "RecommendationsFnArn",
            value=recs_fn.function_arn,
            export_name=f"{{project_name}}-recommendations-fn-{stage_name}",
        )
        cdk.CfnOutput(self, "RecsTableName",
            value=recs_table.table_name,
            export_name=f"{{project_name}}-recs-table-{stage_name}",
        )
```

### 4.3 Micro-stack gotchas

- **Local CMK for DDB** is what keeps the fifth non-negotiable honoured — don't import a cross-stack lake CMK as a construct and pass it to `encryption_key=`.
- **`iam:PassRole` with `iam:PassedToService=sagemaker.amazonaws.com`** — required when the precompute Lambda passes the SageMaker execution role to `CreateTransformJob`. Without the condition, IAM Access Analyzer flags this as over-privileged.
- **`transform-job/recs-precompute-*`** scoping on `CreateTransformJob` — the resource pattern is the expected job-name prefix; anything else fails IAM evaluation.
- **`features_bucket` / `curated_bucket` are just names** used in the S3 URI env vars — there's no cross-stack `s3:Get*` grant needed here because the Lambda never reads from S3 directly; SageMaker Batch Transform does that using the SageMaker role.
- **Endpoint may not exist** at stack-deploy time — `USE_REALTIME_RERANK=false` defaults for non-prod prevent runtime failures.

---

## 5. Swap matrix — when to switch variants

| Trigger | Action |
|---|---|
| POC, one stack | §3 Monolith |
| Production MSxx | §4 Micro-Stack |
| Swap Two-Tower → BERT4Rec | Change `MODEL_NAME` env var; retrain pipeline lives in `MLOPS_SAGEMAKER_TRAINING` |
| Add context features (device, time-of-day) | Extend rerank endpoint payload + CTR model; no infra change |
| Move off DDB to ElastiCache | Add an ElastiCache cluster + swap read path; keep DDB as source-of-truth |
| Drop real-time rerank | Set `USE_REALTIME_RERANK=false`; remove endpoint dependency |
| Run precompute more often | Change EventBridge schedule (hourly / 15-min) |

---

## 6. Worked example — RecommendationsStack synthesizes

Save as `tests/sop/test_MLOPS_PIPELINE_RECOMMENDATIONS.py`. Offline.

```python
"""SOP verification — RecommendationsStack synthesizes DDB + serve + precompute + schedule."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_recommendations_stack():
    app = cdk.App()
    env = _env()
    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2)
    sg   = ec2.SecurityGroup(deps, "Sg", vpc=vpc)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.recommendations_stack import RecommendationsStack
    stack = RecommendationsStack(
        app, stage_name="prod",
        vpc=vpc, lambda_sg=sg,
        rerank_endpoint_name_ssm="/test/ml/rerank_endpoint_name",
        twotower_model_name_ssm="/test/ml/twotower_model_name",
        features_bucket_name_ssm="/test/lake/features_bucket",
        curated_bucket_name_ssm="/test/lake/curated_bucket",
        sagemaker_role_arn_ssm="/test/ml/sagemaker_role_arn",
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 2)
    t.resource_count_is("AWS::DynamoDB::Table",  1)
    t.resource_count_is("AWS::Events::Rule",     1)
    t.resource_count_is("AWS::KMS::Key",         1)
```

---

## 7. References

- `docs/template_params.md` — `RECS_TABLE_NAME`, `RECS_TOP_N`, `RECS_USE_REALTIME_RERANK`, `RERANK_ENDPOINT_NAME_SSM`, `TWOTOWER_MODEL_NAME_SSM`
- `docs/Feature_Roadmap.md` — feature IDs `ML-50` (recommendations serving), `ML-51` (nightly batch precompute), `ML-52` (real-time rerank)
- DynamoDB TTL: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html
- SageMaker Batch Transform: https://docs.aws.amazon.com/sagemaker/latest/dg/batch-transform.html
- Related SOPs: `MLOPS_SAGEMAKER_TRAINING` (Two-Tower training), `MLOPS_SAGEMAKER_SERVING` (rerank endpoint deployer), `LAYER_DATA` (DDB patterns), `LAYER_BACKEND_LAMBDA` (five non-negotiables), `EVENT_DRIVEN_PATTERNS` (EventBridge schedule)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `RecommendationsStack` reads rerank endpoint name + Two-Tower model name + lake buckets + SageMaker role ARN via SSM; identity-side `sagemaker:InvokeEndpoint` scoped to endpoint ARN; identity-side `sagemaker:CreateTransformJob` scoped to transform-job name pattern; `iam:PassRole` with `iam:PassedToService` Condition; local CMK for DDB (5th non-negotiable). Extracted inline Lambda handlers to `lambda/recommendations_serve/` and `lambda/recommendations_precompute/` assets. Added Swap matrix (§5), Worked example (§6), Gotchas. |
| 1.0 | 2026-03-05 | Initial — hybrid recommendations (DDB pre-computed + real-time rerank), nightly batch transform, DDB table with TTL. |
