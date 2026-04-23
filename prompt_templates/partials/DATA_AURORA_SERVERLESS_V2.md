# SOP — Aurora PostgreSQL Serverless v2 (ML scoring + complex reporting)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Aurora PostgreSQL 16.x · Serverless v2 (0.5 – 8 ACU) · RDS Data API (`rds-data`) · Secrets Manager with rotation · KMS at rest · Lambda via RDS Proxy

---

## 1. Purpose

- Provide a deep-dive for **Aurora PostgreSQL Serverless v2** when DynamoDB falls short — JOINs across candidate × interview × dimension, window functions, ACID across many scoring dimensions, ad-hoc BI queries, and per-role rubric weighting schemas.
- Codify the **ACU sizing trade-off** (min 0.5 for scale-to-~nothing vs min 2 for never-cold), secrets rotation, and VPC connectivity patterns (RDS Proxy for Lambda, Data API for serverless-friendly access with no connection pool).
- Provide a canonical **ML-scoring schema** — `candidates`, `interviews`, `scoring_dimensions`, `interview_scores`, `role_rubrics`, `rubric_weights` — with representative `WITH`-clause pivot queries and role-based rubric JOINs.
- Include when the SOW signals: "need SQL", "JOINs", "window functions", "reporting dashboard", "per-role weighting", "ML scoring with ACID", "analyst SQL console", "BI tool query", "pivot table across candidates".
- This is the **Serverless v2 + ML-scoring specialisation**. `LAYER_DATA` §3.2 / §4.2 covers plain RDS / Aurora basics; this partial deep-dives and does not repeat them.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC where the Aurora cluster, migrations Lambda, and consumer Lambdas all live in one `cdk.Stack` | **§3 Monolith Variant** |
| `DatabaseStack` owns Aurora cluster + secret + proxy; `ComputeStack` owns the Lambdas that query it | **§4 Micro-Stack Variant** |

**Why the split matters.** Same tax as every data layer:

- `cluster.grant_data_api_access(fn)` cross-stack auto-adds `rds-data:*` to the role *and* auto-adds `secretsmanager:GetSecretValue` on the secret owned by `DatabaseStack` → that secret's resource policy mutates → cyclic CloudFormation export.
- `db_secret.grant_read(fn)` across stacks does the same.
- `rds_proxy.grant_connect(fn)` adds `rds-db:connect` identity-side (safe) **but** also modifies the proxy's IAM-auth configuration when the fn's role ARN isn't known at synth time.
- The Serverless v2 cluster's KMS CMK — `storage_encryption_key=ext_key` — auto-adds the RDS service principal as a grantee → KMS policy mutation.

Micro-Stack variant fixes all of this by: (a) owning cluster + proxy + secret inside `DatabaseStack`; (b) publishing `ClusterArn`, `SecretArn`, `ProxyEndpoint`, `ProxyArn`, `ReaderEndpoint` via SSM parameters; (c) consumer Lambdas grant themselves `rds-data:*` (or `rds-db:connect`) and `secretsmanager:GetSecretValue` on the specific ARNs — identity-side only.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Lambda (scoring) ──► RDS Proxy ──► Aurora Serverless v2 writer
                                      │  ACU range: 0.5 - 8
                                      │  Auto-pause: disabled (cold-start penalty)
                                      │
                                      ▼
                                Aurora reader endpoint (optional, for BI)

  Lambda (report)  ──► RDS Data API (rds-data) ──► writer (no pool)

  Migrations Lambda (one-shot on deploy) ──► writer ──► flyway/alembic DDL
```

### 3.2 CDK — `_create_aurora_serverless_v2()` method body

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_rds as rds,
    aws_secretsmanager as sm,
)


def _create_aurora_serverless_v2(self, stage: str) -> None:
    """Monolith variant. Assumes self.{vpc, rds_sg, kms_key, lambda_sg} exist.
    Publishes Aurora cluster + RDS Proxy + rotating secret + Data API."""

    # A) Rotating secret (username + password). RDS generates + rotates.
    self.db_secret = rds.DatabaseSecret(
        self, "AuroraSecret",
        secret_name=f"{{project_name}}-aurora-{stage}",
        username="ml_admin",
    )

    # B) Aurora PostgreSQL 16, Serverless v2 (min 0.5, max 8 ACU).
    #    Data API enabled -> serverless-friendly, no connection pool needed.
    # TODO(verify): rds.AuroraPostgresEngineVersion.VER_16_X symbol — CDK
    #   publishes specific minor versions (VER_16_4 etc). If the exact symbol
    #   isn't available in your aws-cdk-lib version, pin to VER_16_4 explicitly.
    self.aurora_cluster = rds.DatabaseCluster(
        self, "AuroraCluster",
        cluster_identifier=f"{{project_name}}-aurora-{stage}",
        engine=rds.DatabaseClusterEngine.aurora_postgres(
            version=rds.AuroraPostgresEngineVersion.VER_16_4
        ),
        credentials=rds.Credentials.from_secret(self.db_secret),
        writer=rds.ClusterInstance.serverless_v2("Writer"),
        readers=[
            rds.ClusterInstance.serverless_v2("Reader1", scale_with_writer=True),
        ] if stage == "prod" else [],
        serverless_v2_min_capacity=0.5,   # scales to almost-zero between queries
        serverless_v2_max_capacity=8,     # caps cost at ~$1.44/hr at peak
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        ),
        security_groups=[self.rds_sg],
        storage_encrypted=True,
        storage_encryption_key=self.kms_key,
        default_database_name="ml_scoring",
        backup=rds.BackupProps(
            retention=Duration.days(7 if stage == "prod" else 1),
            preferred_window="03:00-04:00",
        ),
        preferred_maintenance_window="sun:04:30-sun:05:30",
        deletion_protection=(stage == "prod"),
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
        iam_authentication=True,
        # Enables the rds-data client (serverless-friendly, no pool)
        enable_data_api=True,
        # Performance Insights for slow-query analysis
        # TODO(verify): performance_insights_retention on Serverless v2 clusters
        #   is sometimes only settable on the ClusterInstance, not the cluster.
        # NOTE: `rds.ParameterGroup` passed via `parameter_group=` on a
        # DatabaseCluster synthesizes to AWS::RDS::DBClusterParameterGroup
        # (CDK auto-calls bindToCluster()). `shared_preload_libraries` is a
        # cluster-level param and is applied correctly. If you ever observe
        # the param not taking effect, drop to the L1 escape hatch
        # `rds.CfnDBClusterParameterGroup` and reference it via `cfn_cluster`.
        parameter_group=rds.ParameterGroup(
            self, "AuroraParamGroup",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4
            ),
            parameters={
                "log_min_duration_statement": "1000",     # log slow queries > 1 s
                "log_statement":              "ddl",
                "shared_preload_libraries":   "pg_stat_statements",
            },
        ),
    )

    # C) Secret rotation — 30-day Lambda-based rotation.
    self.db_secret.add_rotation_schedule(
        "DbSecretRotation",
        hosted_rotation=sm.HostedRotation.postgre_sql_single_user(),
        automatically_after=Duration.days(30),
    )

    # D) RDS Proxy — persistent-connection pool for Lambdas that use psycopg
    self.rds_proxy = self.aurora_cluster.add_proxy(
        "AuroraProxy",
        secrets=[self.db_secret],
        vpc=self.vpc,
        security_groups=[self.rds_sg],
        iam_auth=True,
        require_tls=True,
        borrow_timeout=Duration.seconds(30),
        idle_client_timeout=Duration.minutes(30),
        max_connections_percent=80,
        max_idle_connections_percent=50,
    )

    CfnOutput(self, "AuroraClusterArn",    value=self.aurora_cluster.cluster_arn)
    CfnOutput(self, "AuroraWriterEndpoint", value=self.aurora_cluster.cluster_endpoint.hostname)
    CfnOutput(self, "AuroraReaderEndpoint", value=self.aurora_cluster.cluster_read_endpoint.hostname)
    CfnOutput(self, "AuroraProxyEndpoint", value=self.rds_proxy.endpoint)
    CfnOutput(self, "AuroraSecretArn",     value=self.db_secret.secret_arn)
```

### 3.3 Canonical ML-scoring schema — saved to `lambda/db_migrations/sql/001_init.sql`

```sql
-- Candidate-centric ML scoring schema. Optimized for per-role rubric JOINs
-- and candidate × interview × dimension pivots.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE candidates (
    candidate_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email             CITEXT NOT NULL UNIQUE,
    full_name         TEXT NOT NULL,
    resume_s3_key     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE interviews (
    interview_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id      UUID NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    role_code         TEXT NOT NULL,                       -- e.g. "SWE_L5", "PM_SR"
    scheduled_at      TIMESTAMPTZ NOT NULL,
    completed_at      TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled|completed|cancelled
    video_s3_key      TEXT,
    transcript_s3_key TEXT,
    overall_score     NUMERIC(5,2),                        -- denormalized weighted
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_interviews_candidate ON interviews(candidate_id);
CREATE INDEX ix_interviews_role      ON interviews(role_code);
CREATE INDEX ix_interviews_status    ON interviews(status) WHERE status <> 'completed';

-- Dimensions each interview is scored along. Drives rubric JOINs.
CREATE TABLE scoring_dimensions (
    dimension_code    TEXT PRIMARY KEY,                    -- "COMM", "TECH", "CULTURE", ...
    dimension_name    TEXT NOT NULL,
    description       TEXT
);

-- Per-role weights. A SWE_L5 weights TECH at 0.5, COMM at 0.2, etc.
CREATE TABLE role_rubrics (
    role_code         TEXT NOT NULL,
    dimension_code    TEXT NOT NULL REFERENCES scoring_dimensions(dimension_code),
    weight            NUMERIC(4,3) NOT NULL CHECK (weight BETWEEN 0 AND 1),
    PRIMARY KEY (role_code, dimension_code)
);

-- Per-interview raw dimension scores (0..10) emitted by the analyzers.
CREATE TABLE interview_scores (
    interview_id      UUID NOT NULL REFERENCES interviews(interview_id) ON DELETE CASCADE,
    dimension_code    TEXT NOT NULL REFERENCES scoring_dimensions(dimension_code),
    raw_score         NUMERIC(4,2) NOT NULL CHECK (raw_score BETWEEN 0 AND 10),
    confidence        NUMERIC(4,3) NOT NULL DEFAULT 1.000,
    analyzer_stream   TEXT NOT NULL,                       -- "text" | "audio" | "video"
    evidence_json     JSONB,
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (interview_id, dimension_code, analyzer_stream)
);
CREATE INDEX ix_scores_dimension ON interview_scores(dimension_code);

-- Denormalized final weighted score per (candidate, interview)
CREATE MATERIALIZED VIEW mv_interview_weighted AS
SELECT
    s.interview_id,
    i.candidate_id,
    i.role_code,
    SUM(s.raw_score * rr.weight * s.confidence) AS weighted_score,
    COUNT(DISTINCT s.dimension_code)            AS dims_covered,
    MAX(s.scored_at)                            AS last_scored_at
FROM interview_scores s
JOIN interviews i      ON i.interview_id   = s.interview_id
JOIN role_rubrics rr   ON rr.role_code     = i.role_code
                       AND rr.dimension_code = s.dimension_code
GROUP BY s.interview_id, i.candidate_id, i.role_code;
CREATE UNIQUE INDEX ix_mv_weighted_interview ON mv_interview_weighted(interview_id);

-- Refresh trigger (concurrent so no read lock):
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_interview_weighted;
```

### 3.4 Common query patterns — saved to `lambda/scoring_api/queries.py`

```python
"""Canonical query patterns used by the scoring API and BI layer."""

# 1) Candidate × interview × dimension pivot (one row per candidate,
#    columns = dimensions). Uses crosstab-free CASE pivot for portability.
CANDIDATE_DIMENSION_PIVOT = """
SELECT
    c.candidate_id,
    c.full_name,
    i.role_code,
    MAX(CASE WHEN s.dimension_code = 'TECH'    THEN s.raw_score END) AS tech_score,
    MAX(CASE WHEN s.dimension_code = 'COMM'    THEN s.raw_score END) AS comm_score,
    MAX(CASE WHEN s.dimension_code = 'CULTURE' THEN s.raw_score END) AS culture_score,
    AVG(s.confidence)                                                AS avg_confidence
FROM candidates c
JOIN interviews i         ON i.candidate_id    = c.candidate_id
JOIN interview_scores s   ON s.interview_id    = i.interview_id
WHERE i.status = 'completed'
  AND (:candidate_id IS NULL OR c.candidate_id = :candidate_id)
GROUP BY c.candidate_id, c.full_name, i.role_code
ORDER BY c.full_name;
"""

# 2) Role-based rubric JOIN: apply weights at query time (no denorm needed).
SCORE_WITH_ROLE_WEIGHTS = """
WITH weighted AS (
    SELECT
        s.interview_id,
        SUM(s.raw_score * rr.weight * s.confidence) AS weighted_score,
        COUNT(DISTINCT s.dimension_code)            AS dims_covered
    FROM interview_scores s
    JOIN interviews i      ON i.interview_id      = s.interview_id
    JOIN role_rubrics rr   ON rr.role_code        = i.role_code
                           AND rr.dimension_code  = s.dimension_code
    WHERE i.interview_id = :interview_id
    GROUP BY s.interview_id
)
UPDATE interviews
SET overall_score = w.weighted_score
FROM weighted w
WHERE interviews.interview_id = w.interview_id
RETURNING interviews.interview_id, interviews.overall_score, w.dims_covered;
"""

# 3) Rolling 30-day candidate percentile (window function).
PERCENTILE_BY_ROLE = """
SELECT
    i.interview_id,
    i.candidate_id,
    i.role_code,
    i.overall_score,
    PERCENT_RANK() OVER (
        PARTITION BY i.role_code
        ORDER BY i.overall_score NULLS LAST
    ) AS role_percentile
FROM interviews i
WHERE i.status = 'completed'
  AND i.completed_at >= now() - INTERVAL '30 days';
"""
```

### 3.5 Scoring-API handler — saved to `lambda/scoring_api/index.py`

```python
"""Scoring API backed by Aurora Serverless v2 via the RDS Data API.

Data API is the right choice for serverless workloads:
- No connection pool to manage (unlike psycopg).
- IAM-only auth (no password in env).
- Scales from 0 connections without cold-start pool warmup.
- Downside: higher per-query latency than Proxy (~15-30 ms overhead).

Use RDS Proxy instead for:
- Hot paths (< 50 ms required).
- Heavy JSONB / array parameter passing.
- Long-lived transactions.
"""
import json
import logging
import os
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rds_data = boto3.client("rds-data")

CLUSTER_ARN = os.environ["AURORA_CLUSTER_ARN"]
SECRET_ARN  = os.environ["AURORA_SECRET_ARN"]
DATABASE    = os.environ.get("DATABASE_NAME", "ml_scoring")


def lambda_handler(event, _ctx):
    body = json.loads(event.get("body", "{}"))
    op   = event.get("pathParameters", {}).get("op", "pivot")

    try:
        if op == "pivot":
            rows = _exec(
                sql="""SELECT c.candidate_id::text, c.full_name, i.role_code,
                              MAX(CASE WHEN s.dimension_code='TECH' THEN s.raw_score END) AS tech,
                              MAX(CASE WHEN s.dimension_code='COMM' THEN s.raw_score END) AS comm
                       FROM candidates c
                       JOIN interviews i ON i.candidate_id = c.candidate_id
                       JOIN interview_scores s ON s.interview_id = i.interview_id
                       WHERE i.status = 'completed'
                         AND (:candidate_id::uuid IS NULL OR c.candidate_id = :candidate_id::uuid)
                       GROUP BY c.candidate_id, c.full_name, i.role_code""",
                params=[_param("candidate_id", body.get("candidate_id"))],
            )
            return _ok(rows)

        if op == "rescore":
            # Transactional multi-statement: compute weighted + update interviews.
            tx_id = rds_data.begin_transaction(
                resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, database=DATABASE,
            )["transactionId"]
            try:
                _exec(
                    sql="""WITH w AS (
                               SELECT s.interview_id,
                                      SUM(s.raw_score * rr.weight * s.confidence) AS ws
                               FROM interview_scores s
                               JOIN interviews i    ON i.interview_id = s.interview_id
                               JOIN role_rubrics rr ON rr.role_code    = i.role_code
                                                  AND rr.dimension_code = s.dimension_code
                               WHERE s.interview_id = :iid::uuid
                               GROUP BY s.interview_id
                           )
                           UPDATE interviews SET overall_score = w.ws
                           FROM w WHERE interviews.interview_id = w.interview_id""",
                    params=[_param("iid", body["interview_id"])],
                    tx_id=tx_id,
                )
                rds_data.commit_transaction(
                    resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, transactionId=tx_id,
                )
            except Exception:
                rds_data.rollback_transaction(
                    resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, transactionId=tx_id,
                )
                raise
            return _ok({"rescored": body["interview_id"]})

        return _err(404, f"unknown op={op}")
    except Exception as e:
        logger.exception("scoring-api failed op=%s", op)
        return _err(500, str(e))


def _exec(sql: str, params: list[dict], tx_id: str | None = None) -> list[dict]:
    kwargs = dict(
        resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, database=DATABASE,
        sql=sql, parameters=params, formatRecordsAs="JSON",
    )
    if tx_id:
        kwargs["transactionId"] = tx_id
    resp = rds_data.execute_statement(**kwargs)
    # formatRecordsAs=JSON returns a JSON string in resp["formattedRecords"].
    return json.loads(resp.get("formattedRecords", "[]"))


def _param(name: str, value):
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, (float, Decimal)):
        return {"name": name, "value": {"doubleValue": float(value)}}
    return {"name": name, "value": {"stringValue": str(value)}}


def _ok(body):
    return {"statusCode": 200, "body": json.dumps(body, default=str)}


def _err(code: int, msg: str):
    return {"statusCode": code, "body": json.dumps({"error": msg})}
```

### 3.6 Monolith gotchas

- **`serverless_v2_min_capacity=0.5`** is the scale-to-near-zero floor. It does NOT auto-pause; Serverless v2 has no auto-pause (v1 did). A dormant cluster at 0.5 ACU still costs ~$43/month. For dev, consider stopping the cluster via scheduled Lambda or flipping to `serverless_v2_min_capacity=0` (Aurora Serverless v2 does allow 0 ACU scale-to-zero as of late 2024 — `# TODO(verify): CDK aws-cdk-lib support for 0 in your pinned version; fall back to 0.5 if synth rejects`).
- **Data API has a 100 KB payload cap** per `execute_statement` request and a 45-second statement timeout. Long reports → use Proxy instead.
- **`enable_data_api=True`** must be set on the cluster. Forgetting it manifests as `BadRequestException: ... Data API is not enabled for this cluster` at query time, not at synth.
- **`rds-data:*` is not the same as `rds-db:connect`.** Data API permissions are on the CLUSTER ARN; `rds-db:connect` is on a DB resource ARN (`arn:aws:rds-db:region:account:dbuser:cluster-XXXX/username`).
- **Secret rotation on Serverless v2** with `hosted_rotation=postgre_sql_single_user` creates a Lambda in the cluster's VPC. If the VPC has no NAT + no Secrets Manager VPC endpoint, rotation silently fails. Add the endpoint or use multi-user rotation.
- **Materialized view refresh** lock: `REFRESH MATERIALIZED VIEW` (without `CONCURRENTLY`) takes an AccessExclusiveLock — all readers block. Always `CONCURRENTLY`, which requires a unique index.
- **`preferred_maintenance_window` spans Saturday 23:00 to Sunday 00:30 UTC in engine default** — if your users are in Tokyo that's Sunday mid-morning. Always set it explicitly.

---

## 4. Micro-Stack Variant

**Use when:** `DatabaseStack` owns Aurora + Proxy + secret; `ComputeStack` (or an `AnalyticsStack`) owns the Lambdas that query it.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA` §4.1)

1. **Anchor asset paths to `__file__`, never relative-to-CWD** — `_LAMBDAS_ROOT` pattern.
2. **Never call `cluster.grant_data_api_access(fn)` cross-stack.** Identity-side `PolicyStatement` with `rds-data:*` actions on the cluster ARN + `secretsmanager:GetSecretValue` on the secret ARN.
3. **Never call `secret.grant_read(fn)` cross-stack.** Same pattern — identity-side on the fn role.
4. **Never set `storage_encryption_key=ext_kms_key`** on an Aurora cluster when the CMK comes from another stack. Own the CMK inside `DatabaseStack` (or use the AWS-managed `aws/rds` key).
5. **Never pass the `rds.DatabaseCluster` object itself into `ComputeStack`.** Pass ARN + endpoint strings via SSM and reconstitute nothing — consumer Lambdas only need the ARN + secret + endpoint to call `rds-data` or `rds-db:connect`.

### 4.2 Dedicated `DatabaseStack` — Aurora Serverless v2 + Proxy + secret

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_rds as rds,
    aws_secretsmanager as sm,
    aws_ssm as ssm,
)
from constructs import Construct

_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class AuroraServerlessV2Stack(cdk.Stack):
    """Aurora PostgreSQL Serverless v2 cluster + RDS Proxy + rotating secret.

    Publishes every consumer-relevant handle via SSM so downstream stacks
    can import by parameter name without creating CloudFormation exports.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        rds_sg: ec2.ISecurityGroup,
        permission_boundary: iam.IManagedPolicy,
        min_acu: float = 0.5,
        max_acu: float = 8.0,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-database-{stage_name}", **kwargs)

        # Local CMK — never import from SecurityStack here (non-negotiable #4)
        cmk = kms.Key(
            self, "AuroraKey",
            alias=f"alias/{{project_name}}-aurora-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )

        secret = rds.DatabaseSecret(
            self, "AuroraSecret",
            secret_name=f"{{project_name}}-aurora-{stage_name}",
            username="ml_admin",
            encryption_key=cmk,
        )

        # TODO(verify): rds.AuroraPostgresEngineVersion.VER_16_4 vs VER_16_X
        #   across aws-cdk-lib releases. If unavailable, pin to VER_16_2 or the
        #   latest Serverless-v2-supported minor in your CDK version.
        cluster = rds.DatabaseCluster(
            self, "AuroraCluster",
            cluster_identifier=f"{{project_name}}-aurora-{stage_name}",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4
            ),
            credentials=rds.Credentials.from_secret(secret),
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            readers=(
                [rds.ClusterInstance.serverless_v2("Reader1", scale_with_writer=True)]
                if stage_name == "prod" else []
            ),
            serverless_v2_min_capacity=min_acu,
            serverless_v2_max_capacity=max_acu,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            security_groups=[rds_sg],
            storage_encrypted=True,
            storage_encryption_key=cmk,
            default_database_name="ml_scoring",
            backup=rds.BackupProps(
                retention=Duration.days(7 if stage_name == "prod" else 1),
                preferred_window="03:00-04:00",
            ),
            preferred_maintenance_window="sun:04:30-sun:05:30",
            deletion_protection=(stage_name == "prod"),
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
            iam_authentication=True,
            enable_data_api=True,
        )

        secret.add_rotation_schedule(
            "Rotation",
            hosted_rotation=sm.HostedRotation.postgre_sql_single_user(
                vpc=vpc,
                security_groups=[rds_sg],
            ),
            automatically_after=Duration.days(30),
        )

        proxy = cluster.add_proxy(
            "AuroraProxy",
            secrets=[secret],
            vpc=vpc,
            security_groups=[rds_sg],
            iam_auth=True,
            require_tls=True,
            borrow_timeout=Duration.seconds(30),
            idle_client_timeout=Duration.minutes(30),
        )

        # The migrations Lambda runs with iam:PassRole only to a local role.
        # Cross-account / cross-role PassRole is NOT permitted here.
        migrator_role = iam.Role(
            self, "MigratorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=permission_boundary,
        )
        migrator_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "rds-data:BatchExecuteStatement",
                "rds-data:BeginTransaction",
                "rds-data:CommitTransaction",
                "rds-data:RollbackTransaction",
            ],
            resources=[cluster.cluster_arn],
        ))
        migrator_role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[secret.secret_arn],
        ))
        migrator_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=[cmk.key_arn],
        ))
        # iam:PassRole scoped to this exact role + the Lambda service.
        migrator_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[migrator_role.role_arn],
            conditions={"StringEquals": {
                "iam:PassedToService": "lambda.amazonaws.com"
            }},
        ))

        # Publish everything downstream needs via SSM.
        ssm.StringParameter(
            self, "ClusterArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/aurora/cluster_arn",
            string_value=cluster.cluster_arn,
        )
        ssm.StringParameter(
            self, "SecretArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/aurora/secret_arn",
            string_value=secret.secret_arn,
        )
        ssm.StringParameter(
            self, "WriterEndpointParam",
            parameter_name=f"/{{project_name}}/{stage_name}/aurora/writer_endpoint",
            string_value=cluster.cluster_endpoint.hostname,
        )
        ssm.StringParameter(
            self, "ReaderEndpointParam",
            parameter_name=f"/{{project_name}}/{stage_name}/aurora/reader_endpoint",
            string_value=cluster.cluster_read_endpoint.hostname,
        )
        ssm.StringParameter(
            self, "ProxyEndpointParam",
            parameter_name=f"/{{project_name}}/{stage_name}/aurora/proxy_endpoint",
            string_value=proxy.endpoint,
        )
        ssm.StringParameter(
            self, "KmsArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/aurora/kms_arn",
            string_value=cmk.key_arn,
        )

        self.cluster = cluster
        self.secret  = secret
        self.proxy   = proxy
        self.cmk     = cmk

        CfnOutput(self, "ClusterArn",   value=cluster.cluster_arn)
        CfnOutput(self, "ProxyEndpoint", value=proxy.endpoint)
```

### 4.3 Consumer pattern — identity-side grants in `ComputeStack`

```python
# Inside ComputeStack. No cluster reference — ARNs read from SSM.
from aws_cdk import aws_ssm as ssm, aws_iam as iam


cluster_arn_param  = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/aurora/cluster_arn"
)
secret_arn_param   = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/aurora/secret_arn"
)
kms_arn_param      = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/aurora/kms_arn"
)

scoring_fn.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "rds-data:ExecuteStatement",
        "rds-data:BatchExecuteStatement",
        "rds-data:BeginTransaction",
        "rds-data:CommitTransaction",
        "rds-data:RollbackTransaction",
    ],
    resources=[cluster_arn_param],
))
scoring_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[secret_arn_param],
))
scoring_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kms:Decrypt"],
    resources=[kms_arn_param],
))
```

### 4.4 Micro-stack gotchas

- **`ssm.StringParameter.value_for_string_parameter`** returns a token — you can use it as a `resources=[...]` entry in an IAM statement, but DO NOT try to `.split(":")` it in Python. It resolves at deploy time.
- **VPC endpoints are mandatory** for `secretsmanager` and `rds-data` when the Lambda is in `PRIVATE_ISOLATED`. Without them calls hang until the Lambda times out.
- **Reader endpoint is not addressable** until at least one reader instance exists. In non-prod (readers=[]) the `ReaderEndpointParam` still resolves — but any Lambda that tries to connect to it gets `could not translate host name`. Fail fast in your handler if `stage != prod`.
- **Data API vs Proxy coexistence** — both can be enabled simultaneously. Cost is identical; choose per-call based on payload size.
- **`iam:PassRole`** with `iam:PassedToService` condition is required if the migrations Lambda is invoked by another service (Step Functions, EventBridge) that passes a role. Omitting the condition trips IAM policy linters.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / low-traffic dashboard only | §3 Monolith + `max_acu=2`, `readers=[]` |
| Production ML scoring with BI workload | §4 Micro-Stack + 1 reader at parity, `max_acu=8` (scale on reader) |
| Scale-to-zero for cost | Set `serverless_v2_min_capacity=0` (AWS docs: supported since Nov 2024). `# TODO(verify): that your CDK version accepts 0.0`. Adds ~30s cold-start on first query |
| Very high JSONB writes | Bump `max_acu` to 16 or 32; memory-bound workloads benefit |
| Need Aurora Global Database (multi-region) | Replace `DatabaseCluster` with `DatabaseCluster.from_cluster_arn` + `rds.CfnGlobalCluster` secondary; Data API stays per-region |
| Need > 100 KB query payloads | Switch caller from Data API to `psycopg[binary]` over Proxy |
| Need analyst SQL console | Add RDS Query Editor (Data API required); IAM policy grants `rds-data:*` to analysts, not to apps |
| Need materialized views to refresh fast | Add dedicated reader + run `REFRESH MATERIALIZED VIEW CONCURRENTLY` there via scheduled Lambda |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_DATA_AURORA_SERVERLESS_V2.py`. Offline; `cdk.Stack` as deps stub.

```python
"""SOP verification — AuroraServerlessV2Stack synth contains the expected
resources and the cluster's min/max ACU are wired correctly."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_aurora_serverless_v2_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc  = ec2.Vpc(deps, "Vpc", max_azs=2,
                   subnet_configuration=[
                       ec2.SubnetConfiguration(
                           name="iso", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                       ),
                       ec2.SubnetConfiguration(
                           name="egress", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                       ),
                   ])
    rds_sg = ec2.SecurityGroup(deps, "RdsSg", vpc=vpc)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.aurora_serverless_v2_stack import (
        AuroraServerlessV2Stack,
    )
    stack = AuroraServerlessV2Stack(
        app, stage_name="dev",
        vpc=vpc, rds_sg=rds_sg,
        permission_boundary=boundary,
        min_acu=0.5, max_acu=8.0,
        env=env,
    )
    t = Template.from_stack(stack)

    # Serverless v2 ACU range landed on the cluster
    t.has_resource_properties("AWS::RDS::DBCluster", Match.object_like({
        "Engine": "aurora-postgresql",
        "EnableHttpEndpoint": True,                        # Data API enabled
        "ServerlessV2ScalingConfiguration": {
            "MinCapacity": 0.5,
            "MaxCapacity": 8.0,
        },
    }))
    # RDS Proxy present
    t.resource_count_is("AWS::RDS::DBProxy", 1)
    # 1 rotating secret
    t.resource_count_is("AWS::SecretsManager::Secret", 1)
    t.resource_count_is("AWS::SecretsManager::RotationSchedule", 1)
    # SSM params published
    t.resource_count_is("AWS::SSM::Parameter", 6)
```

---

## 7. References

- `docs/template_params.md` — `AURORA_MIN_ACU`, `AURORA_MAX_ACU`, `AURORA_ENABLE_DATA_API`, `AURORA_SECRET_ROTATION_DAYS`, `AURORA_DATABASE_NAME`, `AURORA_PROXY_IDLE_TIMEOUT_MINUTES`
- `docs/Feature_Roadmap.md` — feature IDs `DB-30` (Aurora Serverless v2), `DB-31` (Data API), `DB-32` (Proxy), `DB-33` (ML scoring schema), `DB-34` (materialized pivot view)
- AWS docs:
  - [Aurora Serverless v2](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.html)
  - [RDS Data API](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html)
  - [RDS Proxy](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/rds-proxy.html)
  - [Secrets Manager rotation for RDS](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotate-secrets_turn-on-rds.html)
- Related SOPs:
  - `LAYER_DATA` — RDS basics (non-Serverless-v2), S3 + DDB
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers
  - `LAYER_NETWORKING` — VPC endpoints for `secretsmanager` and `rds-data`
  - `LAYER_SECURITY` — KMS CMK policy patterns
  - `OPS_ADVANCED_MONITORING` — Performance Insights dashboards, slow-query log sinks

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — Aurora PostgreSQL Serverless v2 deep-dive for ML scoring. ACU min/max config, Data API vs RDS Proxy trade-off matrix, secret rotation with hosted VPC Lambda, canonical ML-scoring schema (candidates × interviews × dimensions × role rubrics × materialized weighted view), candidate × dimension pivot + percentile window-function queries, Data API scoring-API handler with `execute_statement` + transactional `begin/commit/rollback`. Created to fill gap surfaced by HR-interview-analyzer kit validation against emapta-avar reference implementation. |
