# SOP — RDS Multi-AZ DB Cluster (3-node semi-sync) + Aurora Multi-AZ deployment + RDS Proxy multiplexing

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · RDS for MySQL 8.0.x / PostgreSQL 16.x · RDS Multi-AZ DB cluster (1 writer + 2 readable standbys, semi-synchronous replication) · Aurora MySQL/Postgres Multi-AZ deployment (writer + 1-15 read replicas across AZs) · RDS Proxy with cluster targets

---

## 1. Purpose

- Codify the **three flavors of "Multi-AZ"** in RDS that engineers conflate:
  - **RDS Multi-AZ instance** (legacy) — 1 writer + 1 hot standby (NOT readable). Failover ~60-120s. Synchronous storage replication.
  - **RDS Multi-AZ DB cluster** (modern, 2022 GA, MySQL/PostgreSQL only) — 1 writer + 2 **readable** standby instances. Semi-synchronous replication. Failover ~35s. **This is the right HA pattern for RDS in 2026.**
  - **Aurora Multi-AZ deployment** — 1 writer + up to 15 readers across AZs, shared distributed storage (6-way replicated across 3 AZs). Failover ~30s. Different architecture entirely.
- Provide a **decision matrix** for picking among them based on engine, Multi-AZ requirement, read scale, and failover SLA.
- Cover the **RDS Proxy multiplexing pattern** for both cluster shapes — including the per-shape gotchas (RDS Multi-AZ DB cluster needs `target_role=READ_WRITE` for writer pinning).
- Cover the **RDS Multi-AZ DB cluster snapshot semantics** (snapshots created from the writer, restorable to standalone Multi-AZ DB instance, but NOT directly to Aurora — requires DMS).
- This is the **non-Aurora-Serverless HA specialisation**. `DATA_AURORA_SERVERLESS_V2` covers Serverless v2; `DATA_AURORA_GLOBAL_DR` covers cross-region. This partial covers the in-region 3-AZ HA story for both RDS and Aurora provisioned instances.

When the SOW signals: "we have a self-managed Postgres / MySQL we want to lift", "we need Multi-AZ", "Aurora is too expensive — can we use RDS?", "readable standby for analytics", "RDS Proxy for connection pooling", "minimize failover blast radius".

---

## 2. Decision tree — which Multi-AZ shape?

```
Engine?
├── PostgreSQL or MySQL (RDS, not Aurora)
│   ├── Read replicas needed? Yes/maybe?
│   │   ├── YES (need 2 readable standbys, semi-sync) → §3 RDS Multi-AZ DB CLUSTER
│   │   └── NO  (just HA, single writer) → use legacy Multi-AZ DB instance (NOT this SOP)
│   └── Failover SLA < 60s required?
│       └── YES → §3 RDS Multi-AZ DB CLUSTER (35s typical)
├── Aurora MySQL or Aurora PostgreSQL
│   ├── Need provisioned (predictable) compute? → §4 Aurora Multi-AZ DEPLOYMENT
│   ├── Need Serverless v2 (scale to ~zero)? → see DATA_AURORA_SERVERLESS_V2
│   └── Need cross-region DR? → see DATA_AURORA_GLOBAL_DR
├── Oracle / SQL Server / MariaDB / DB2
│   └── Use legacy Multi-AZ DB instance (RDS-managed); RDS Multi-AZ DB cluster does NOT support these engines as of 2026-04
└── Why are you on RDS at all? Consider Aurora or Serverless v2.
```

### 2.1 Quick comparison

| Property | RDS Multi-AZ instance (legacy) | **RDS Multi-AZ DB cluster** | **Aurora Multi-AZ deployment** |
|---|---|---|---|
| Engines | All RDS engines | MySQL 8.0.x, PostgreSQL 13.x+ | Aurora MySQL, Aurora Postgres |
| Standby readable? | ❌ NO | ✅ YES (2 standbys, both readable) | ✅ YES (up to 15 readers) |
| Replication | Sync storage | Semi-sync logical | Storage-level distributed (6-way) |
| Failover | ~60-120s | ~35s | ~30s |
| Storage | EBS gp2/gp3, 64 TiB max | EBS gp3, 64 TiB max | Shared distributed, 128 TiB |
| Read scale | None | 2 readable standbys (no autoscale) | Up to 15 readers, autoscaling |
| Cost (2 vCPU writer + 2 readers) | $X | ~1.5× X | ~2× X |
| Use when | Legacy / non-supported engine | RDS HA + read scaling, predictable $ | Aurora-native features, max read scale |
| RDS Proxy support | ✅ | ✅ (with caveats §3.5) | ✅ |
| Backup retention | Up to 35 days | Up to 35 days | Up to 35 days |
| Cross-region read replica | ✅ (logical) | ⚠️ One target only | ✅ Aurora Global Database |

### 2.2 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC where the cluster + Proxy + consumer Lambdas all live in one `cdk.Stack` | **§3 / §4 Monolith Variant** |
| `DatabaseStack` owns cluster + Proxy + secret; `ComputeStack` owns Lambdas | **§5 Micro-Stack Variant** |

**Why the split matters.** Same DB-cross-stack tax as `DATA_AURORA_SERVERLESS_V2`: `db_secret.grant_read(fn)`, `proxy.grant_connect(fn)`, `cluster.grant_data_api_access(fn)` (RDS Multi-AZ DB cluster does NOT support Data API as of 2026-04 — only Aurora Serverless v1/v2 do) all create cyclic CFN exports.

---

## 3. RDS Multi-AZ DB Cluster variant (MySQL/PostgreSQL, 1 writer + 2 readable standbys)

### 3.1 Architecture

```
                     Application
                          │
                          │  via RDS Proxy (recommended)
                          ▼
                    ┌─────────────┐
                    │  RDS Proxy  │
                    │  endpoints: │
                    │   • writer  │  (read+write)
                    │   • reader  │  (load-balanced over standbys)
                    └─────┬───────┘
                          │
        ┌─────────────────┼─────────────────┐
        │ AZ-a            │ AZ-b            │ AZ-c
        ▼                 ▼                 ▼
   ┌──────────┐     ┌──────────┐      ┌──────────┐
   │  WRITER  │ ─── │ STANDBY1 │ ──── │ STANDBY2 │
   │  (RW)    │ <── │  (RO)    │ ←──  │  (RO)    │
   └──────────┘     └──────────┘      └──────────┘
        │                 │                 │
        └────── Semi-synchronous replication (1-of-2 quorum) ─┘
        EBS gp3 per instance · backup window 03:00-04:00 · maintenance window sun:04:30
```

### 3.2 CDK — `_create_rds_multiaz_cluster()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_rds as rds,
    aws_secretsmanager as sm,
)


def _create_rds_multiaz_cluster(self, stage: str) -> None:
    """Monolith variant. Assumes self.{vpc, rds_sg, kms_key, lambda_sg} exist.
    Creates RDS for PostgreSQL Multi-AZ DB cluster (1 writer + 2 readable
    standbys) + RDS Proxy + rotating secret + IAM auth."""

    # A) Rotating secret
    self.db_secret = rds.DatabaseSecret(
        self, "RdsSecret",
        secret_name=f"{{project_name}}-rds-mc-{stage}",
        username="app_admin",
    )

    # B) Multi-AZ DB Cluster (NOT Aurora; this is RDS for Postgres in cluster mode)
    # NOTE: As of CDK v2.150+, the L2 `rds.DatabaseCluster` supports MultiAZ cluster
    # via `cluster_type=CLUSTER_MULTI_AZ`. If your CDK version doesn't have it, use
    # the L1 `rds.CfnDBCluster` + `engine="postgres"` (NOT "aurora-postgresql").
    self.rds_cluster = rds.DatabaseCluster(
        self, "RdsMultiAzCluster",
        cluster_identifier=f"{{project_name}}-rds-mc-{stage}",
        engine=rds.DatabaseClusterEngine.postgres(
            version=rds.PostgresEngineVersion.VER_16_4,
        ),
        cluster_type=rds.DBClusterType.CLUSTER_MULTI_AZ,   # NEW: multi-az db cluster
        credentials=rds.Credentials.from_secret(self.db_secret),
        # 1 writer + 2 standby instances, all m6gd
        writer=rds.ClusterInstance.provisioned("Writer",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.M6GD, ec2.InstanceSize.LARGE),
            publicly_accessible=False,
        ),
        readers=[
            rds.ClusterInstance.provisioned("Standby1",
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.M6GD, ec2.InstanceSize.LARGE),
                publicly_accessible=False,
                # NOTE: MultiAZ DB cluster requires identical writer+reader sizing
            ),
            rds.ClusterInstance.provisioned("Standby2",
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.M6GD, ec2.InstanceSize.LARGE),
                publicly_accessible=False,
            ),
        ],
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        security_groups=[self.rds_sg],
        storage_encrypted=True,
        storage_encryption_key=self.kms_key,
        # MultiAZ DB cluster supports 100GB-64TB gp3
        # NOTE: storage_type at cluster level only; not per-instance
        # storage=100, allocated_storage_type=rds.StorageType.GP3,  # CDK L2 may differ
        default_database_name="appdb",
        backup=rds.BackupProps(
            retention=Duration.days(35 if stage == "prod" else 7),
            preferred_window="03:00-04:00",
        ),
        preferred_maintenance_window="sun:04:30-sun:05:30",
        deletion_protection=(stage == "prod"),
        removal_policy=(RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY),
        iam_authentication=True,
        # Cluster parameter group — multiaz-specific tuning
        parameter_group=rds.ParameterGroup(
            self, "RdsMcParams",
            engine=rds.DatabaseClusterEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16_4),
            parameters={
                # Read-from-standby — clients on reader endpoint see committed data
                # within ~10ms of write commit on writer.
                "synchronous_commit": "remote_apply",
                # Encourage parallel queries on standbys for analytics workloads
                "max_parallel_workers": "8",
                "max_parallel_workers_per_gather": "4",
                # Logical replication if downstream zero-ETL is desired
                "rds.logical_replication": "1",
                "max_replication_slots": "20",
                "max_wal_senders": "20",
            },
        ),
    )

    # C) RDS Proxy — multiplexes connections, hides failover from app
    self.rds_proxy = rds.DatabaseProxy(
        self, "RdsProxy",
        proxy_target=rds.ProxyTarget.from_cluster(self.rds_cluster),
        secrets=[self.db_secret],
        vpc=self.vpc,
        security_groups=[self.rds_sg],
        # Required for MultiAZ DB cluster: enable IAM auth at proxy level too
        iam_auth=True,
        require_tls=True,
        # MultiAZ DB cluster supports session pinning by transaction
        # idle_client_timeout=Duration.minutes(30),
        # max_connections_percent=100,
        # max_idle_connections_percent=50,
        debug_logging=(stage != "prod"),
    )

    # D) Outputs / SSM publishing for consumer stacks (see §5)
    CfnOutput(self, "ClusterEndpoint",
              value=self.rds_cluster.cluster_endpoint.hostname)
    CfnOutput(self, "ReaderEndpoint",
              value=self.rds_cluster.cluster_read_endpoint.hostname)
    CfnOutput(self, "ProxyWriterEndpoint",
              value=self.rds_proxy.endpoint)
```

### 3.3 Lambda consumer — IAM auth via RDS Proxy

```python
# In ComputeStack — Lambda that reads from cluster via Proxy reader endpoint

read_lambda = lambda_.Function(
    self, "AnalyticsReadFn",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="index.handler",
    code=lambda_.Code.from_asset(str(LAMBDA_SRC / "analytics_read")),
    vpc=self.vpc,
    vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
    security_groups=[self.lambda_sg],
    timeout=Duration.seconds(30),
    environment={
        "PROXY_HOST":   self.rds_proxy.endpoint,                  # writer endpoint
        "PROXY_RO":     "rds-proxy-readonly",                     # reader endpoint suffix
        "DB_NAME":      "appdb",
        "DB_USER":      "app_admin",
        "READ_ONLY":    "1",                                      # routes to standby
    },
)

# Identity-side grant — never the proxy.grant_connect(read_lambda) cross-stack
read_lambda.add_to_role_policy(iam.PolicyStatement(
    actions=["rds-db:connect"],
    resources=[
        f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:"
        f"{self.rds_proxy.db_proxy_arn.split(':')[-1]}/app_admin",
    ],
))

# Reader endpoint requires the same dbuser ARN — IAM auth covers both
# Allow secret read (Proxy itself uses it for auth, but if we want fallback)
self.db_secret.grant_read(read_lambda)
self.kms_key.grant_decrypt(read_lambda)
```

### 3.4 Reader-routing pattern — Postgres `application_name` trick

For Multi-AZ DB cluster, RDS Proxy exposes ONE writer endpoint. To route reads to standbys, use the cluster's `cluster_read_endpoint` directly (bypasses proxy) OR use the proxy with `application_name=read-only` parameter — proxy's reader pool will route accordingly.

```python
# Lambda handler — picks reader vs writer endpoint based on intent
import os
import psycopg2

def handler(event, context):
    is_read_only = event.get("read_only", os.environ.get("READ_ONLY") == "1")
    host = (
        os.environ["PROXY_RO"] if is_read_only
        else os.environ["PROXY_HOST"]
    )

    # IAM auth — token replaces password for 15 min
    import boto3
    rds = boto3.client("rds")
    token = rds.generate_db_auth_token(
        DBHostname=host, Port=5432,
        DBUsername=os.environ["DB_USER"],
    )

    conn = psycopg2.connect(
        host=host, port=5432,
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=token,
        sslmode="require",
        sslrootcert="/opt/rds-ca-rsa2048-g1.pem",
    )
    # ... query ...
```

### 3.5 RDS Proxy gotchas with Multi-AZ DB cluster

| Issue | Fix |
|---|---|
| Proxy session pinning kills perf | MultiAZ DB cluster supports session-level pinning (default). For transactional workloads, set `target.target_role=READ_WRITE`. For analytic reads, use cluster's reader endpoint directly (bypass proxy) |
| IAM auth fails with "PAM authentication failed" | Cluster parameter group must have `rds.iam_authentication=1`. RDS doesn't auto-set this for MultiAZ DB cluster. |
| Failover causes stale connections | `idle_client_timeout` should be < `connection_borrow_timeout` of app. Set proxy `idle_client_timeout=180s` and app pool max-idle to 60s |
| Cross-AZ data transfer costs | Reader endpoint load-balances across all 3 AZs. App in AZ-a will pay $0.01/GB on reads from AZ-b/c. Use `availability_zone_affinity=true` on proxy if you can pin to one AZ |
| Logical replication slot bloat | If using `rds.logical_replication=1` for downstream zero-ETL, slots tied to standbys not writer. After failover, slots may need recreation |

---

## 4. Aurora Multi-AZ deployment variant (provisioned, not Serverless)

### 4.1 Architecture

```
                    ┌─────────────┐
                    │  RDS Proxy  │
                    └─────┬───────┘
                          │
              ┌───────────┼───────────────┐
              │ AZ-a      │ AZ-b          │ AZ-c
              ▼           ▼               ▼
         ┌────────┐  ┌────────┐      ┌────────┐
         │ WRITER │  │READER1 │      │READER2 │
         │  (RW)  │  │  (RO)  │      │  (RO)  │
         └────┬───┘  └────────┘      └────────┘
              │
              ▼
     ┌──────────────────────────────────────┐
     │  Aurora Storage (6-way replicated)   │
     │  Distributed across 3 AZs            │
     └──────────────────────────────────────┘
```

### 4.2 CDK — `_create_aurora_provisioned_multiaz()`

```python
def _create_aurora_provisioned_multiaz(self, stage: str) -> None:
    """Aurora Postgres provisioned (non-Serverless) with Multi-AZ readers."""

    self.db_secret = rds.DatabaseSecret(
        self, "AuroraSecret",
        secret_name=f"{{project_name}}-aurora-{stage}",
        username="app_admin",
    )

    self.aurora_cluster = rds.DatabaseCluster(
        self, "AuroraCluster",
        cluster_identifier=f"{{project_name}}-aurora-{stage}",
        engine=rds.DatabaseClusterEngine.aurora_postgres(
            version=rds.AuroraPostgresEngineVersion.VER_16_4),
        credentials=rds.Credentials.from_secret(self.db_secret),
        writer=rds.ClusterInstance.provisioned("Writer",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.R6G, ec2.InstanceSize.XLARGE),
        ),
        readers=[
            rds.ClusterInstance.provisioned(f"Reader{i}",
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.R6G, ec2.InstanceSize.XLARGE),
                # scale_with_writer=True applies for Serverless v2 ONLY
            ) for i in range(1, 3)
        ],
        vpc=self.vpc,
        vpc_subnets=ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        security_groups=[self.rds_sg],
        storage_encrypted=True,
        storage_encryption_key=self.kms_key,
        backup=rds.BackupProps(
            retention=Duration.days(35 if stage == "prod" else 7),
        ),
        deletion_protection=(stage == "prod"),
        iam_authentication=True,
        enable_data_api=True,                     # Available on provisioned Aurora
        # Aurora deploys instances across AZs automatically; no explicit AZ config
    )

    # Aurora reader auto-scaling (5 min cooldown, target CPU 60%)
    self.aurora_cluster.add_aurora_reader_replica_auto_scaling(
        min_capacity=2, max_capacity=8,
        target_cpu_utilization=60,
        scale_in_cooldown=Duration.minutes(5),
        scale_out_cooldown=Duration.minutes(5),
    )

    # RDS Proxy — Aurora supports it natively
    self.aurora_proxy = rds.DatabaseProxy(
        self, "AuroraProxy",
        proxy_target=rds.ProxyTarget.from_cluster(self.aurora_cluster),
        secrets=[self.db_secret],
        vpc=self.vpc,
        security_groups=[self.rds_sg],
        require_tls=True,
        iam_auth=True,
        # Aurora supports session-level + transaction-level pinning
    )
```

### 4.3 Aurora-specific considerations vs RDS Multi-AZ DB cluster

| Concern | Aurora Multi-AZ deployment | RDS Multi-AZ DB cluster |
|---|---|---|
| Read replica auto-scaling | ✅ Native (1-15) | ❌ Manual (fixed 2 standbys) |
| Storage cost model | Pay per GB stored, no pre-allocation | Pay per GB allocated (gp3) |
| Failover speed | ~30s | ~35s |
| Cross-region replica | Aurora Global Database | Read replica only (logical) |
| Backtrack (point-in-time within last N hours) | ✅ Aurora MySQL only | ❌ |
| Clone (cheap copy via copy-on-write) | ✅ | ❌ Snapshot-restore only |
| Data API (HTTP query, no connection pool) | ✅ | ❌ |
| Performance Insights | ✅ Free tier 7 days | ✅ Free tier 7 days |
| Pgvector / extensions | ✅ All Aurora extensions | ✅ All RDS extensions |

---

## 5. Micro-Stack variant (cross-stack via SSM)

```python
# In DatabaseStack
ssm.StringParameter(self, "ClusterArn",
    parameter_name=f"/{{project_name}}/{stage}/db/cluster-arn",
    string_value=self.rds_cluster.cluster_arn)
ssm.StringParameter(self, "ProxyArn",
    parameter_name=f"/{{project_name}}/{stage}/db/proxy-arn",
    string_value=self.rds_proxy.db_proxy_arn)
ssm.StringParameter(self, "ProxyEndpoint",
    parameter_name=f"/{{project_name}}/{stage}/db/proxy-endpoint",
    string_value=self.rds_proxy.endpoint)
ssm.StringParameter(self, "ReaderEndpoint",
    parameter_name=f"/{{project_name}}/{stage}/db/reader-endpoint",
    string_value=self.rds_cluster.cluster_read_endpoint.hostname)
ssm.StringParameter(self, "SecretArn",
    parameter_name=f"/{{project_name}}/{stage}/db/secret-arn",
    string_value=self.db_secret.secret_arn)

# In ComputeStack — consumer Lambda grants ITSELF identity-side
proxy_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/db/proxy-arn")
secret_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/db/secret-arn")

read_lambda.add_to_role_policy(iam.PolicyStatement(
    actions=["rds-db:connect"],
    resources=[
        f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:"
        f"{cdk.Fn.select(6, cdk.Fn.split(':', proxy_arn))}/app_admin",
    ],
))
read_lambda.add_to_role_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[secret_arn],
))
```

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| "Can't add 3rd reader to Multi-AZ DB cluster" | Hard limit | Multi-AZ DB cluster is fixed-size: 1 writer + 2 standbys. For more readers use Aurora |
| "Failover took 5 minutes, expected 35s" | DNS TTL too long | App-side connection pool must close on `connection refused` and reconnect; many clients cache DNS for 60s+ |
| Replica lag spikes during bulk loads | Apply lag on standbys | Set `synchronous_commit=remote_write` (faster commit, less safe) OR throttle bulk loader |
| Connection storm after failover | RDS Proxy not used | Use Proxy. If using direct, set `tcp_keepalives_idle=60` to detect dead conns faster |
| Cross-AZ data transfer charges spike | Reads bouncing AZ-a/b/c | Use Proxy `availability_zone_affinity=true` to pin to writer AZ if read latency budget allows |
| RDS Proxy session pinning kills throughput | App holds session-level state | Move session-level state out of the DB (e.g., temp tables → real tables); investigate `pg_stat_activity` for `state_change` patterns |
| IAM auth fails after token expiry | Tokens valid 15 min | Lambda must regenerate token per invocation; long-running ECS tasks need a refresh thread |
| `synchronous_commit=remote_apply` slows writes | Strong-consistency tax | Keep at `remote_write` unless reads-from-standby require zero-lag; document trade-off |

### 6.1 Sizing rule of thumb

| Workload | Writer size | Standby/reader size | Storage |
|---|---|---|---|
| OLTP, < 100 TPS | db.m6gd.large | db.m6gd.large × 2 | 200 GB gp3 |
| OLTP, 100-1000 TPS | db.r6gd.xlarge | db.r6gd.xlarge × 2 | 500 GB gp3 |
| OLTP + analytics on standby | db.r6gd.2xlarge | db.r6gd.2xlarge × 2 | 1 TB gp3 |
| OLTP + heavy DW on standby | Aurora cluster instead | (auto-scale 1-8 readers) | (auto) |

---

## 7. Worked example — pytest synth harness

```python
"""SOP verification — RdsMultiAzClusterStack synth contains 3 instances
(1 writer + 2 standbys), RDS Proxy, KMS-encrypted, IAM auth on."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam, aws_kms as kms
from aws_cdk.assertions import Template, Match


def test_rds_multiaz_cluster_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=3,
        subnet_configuration=[ec2.SubnetConfiguration(
            name="iso", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)])
    rds_sg = ec2.SecurityGroup(deps, "RdsSg", vpc=vpc)
    key = kms.Key(deps, "Key")
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.rds_multiaz_cluster_stack import (
        RdsMultiAzClusterStack,
    )
    stack = RdsMultiAzClusterStack(
        app, stage_name="prod",
        vpc=vpc, rds_sg=rds_sg, kms_key=key,
        permission_boundary=boundary, env=env,
    )
    t = Template.from_stack(stack)

    # Cluster + 3 instances (1 writer + 2 standbys)
    t.has_resource_properties("AWS::RDS::DBCluster", Match.object_like({
        "Engine":      "postgres",
        "EngineMode":  "provisioned",
        "DBClusterInstanceClass": Match.any_value(),
        "StorageEncrypted": True,
        "EnableIAMDatabaseAuthentication": True,
        "DeletionProtection": True,
        "BackupRetentionPeriod": 35,
    }))
    t.resource_count_is("AWS::RDS::DBInstance", 3)
    # RDS Proxy
    t.has_resource_properties("AWS::RDS::DBProxy", Match.object_like({
        "RequireTLS": True,
        "Auth": Match.array_with([Match.object_like({
            "IAMAuth": "REQUIRED",
        })]),
    }))
    # SSM publication
    t.resource_count_is("AWS::SSM::Parameter", Match.greater_than_or_equal(5))
    # KMS-encrypted secret
    t.resource_count_is("AWS::SecretsManager::Secret", 1)
    t.resource_count_is("AWS::SecretsManager::RotationSchedule", 1)


def test_aurora_provisioned_multiaz_synthesizes():
    """Aurora Multi-AZ deployment with reader auto-scaling."""
    # ... similar setup ...
    from infrastructure.cdk.stacks.aurora_provisioned_stack import (
        AuroraProvisionedStack,
    )
    stack = AuroraProvisionedStack(app, stage_name="prod", ..., env=env)
    t = Template.from_stack(stack)

    t.has_resource_properties("AWS::RDS::DBCluster", Match.object_like({
        "Engine": "aurora-postgresql",
        "EnableHttpEndpoint": True,                # Data API on
    }))
    # Auto-scaling target + policy for readers
    t.resource_count_is("AWS::ApplicationAutoScaling::ScalableTarget",
                        Match.greater_than_or_equal(1))
    t.resource_count_is("AWS::ApplicationAutoScaling::ScalingPolicy",
                        Match.greater_than_or_equal(1))
```

---

## 8. Five non-negotiables

1. **Always use RDS Proxy in front of cluster for any production Lambda/ECS workload.** Direct connections from horizontally-scaled compute to a primary DB cause connection storms during failover. Proxy: $0.015/hr per ACU, worth it.

2. **IAM authentication ONLY for production app workloads.** Password-based auth on app users requires secrets rotation everywhere; IAM tokens are 15 min and can't be leaked. Reserve password auth for one-shot migrations and admin connections.

3. **`synchronous_commit=remote_write` is the default; `remote_apply` is opt-in.** `remote_apply` waits for standby to finish replaying — slow writes. `remote_write` waits for WAL receipt — sufficient for HA. Document the trade-off in the cluster parameter group.

4. **Cluster parameter group must enable IAM auth + logical replication.** `rds.iam_authentication=1`, `rds.logical_replication=1` (Postgres) or `binlog_format=ROW` (MySQL). These are NOT defaults; an RDS Multi-AZ DB cluster with default params blocks IAM auth and zero-ETL downstream consumers.

5. **Reader endpoint reads cost $0.01/GB cross-AZ.** For analytic-read-heavy workloads, place compute in the writer's AZ via Proxy `availability_zone_affinity=true` OR accept the data-transfer cost. Track via Cost Explorer tag `data-transfer:cross-az`.

---

## 9. References

- `docs/template_params.md` — `RDS_MULTIAZ_CLUSTER_TYPE`, `RDS_INSTANCE_CLASS`, `RDS_BACKUP_RETENTION_DAYS`, `RDS_PROXY_IDLE_TIMEOUT_MINUTES`, `AURORA_READER_AUTOSCALE_MIN`, `AURORA_READER_AUTOSCALE_MAX`
- `docs/Feature_Roadmap.md` — `DB-40` (Multi-AZ DB cluster), `DB-41` (Aurora provisioned readers), `DB-42` (Proxy IAM auth), `DB-43` (cluster parameter group tuning)
- AWS docs:
  - [Multi-AZ DB cluster overview](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/multi-az-db-clusters-concepts.html)
  - [Aurora DB cluster overview](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Overview.html)
  - [RDS Proxy with Multi-AZ DB clusters](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy.html)
  - [Aurora replicas](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-replicas-adding.html)
  - [Configuring and managing Multi-AZ deployment](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html)
- Related SOPs:
  - `DATA_AURORA_SERVERLESS_V2` — Aurora Serverless v2 (autoscale 0.5-256 ACU)
  - `DATA_AURORA_GLOBAL_DR` — cross-region Aurora Global Database
  - `DATA_DMS_REPLICATION` — migrate INTO Multi-AZ DB cluster from on-prem source
  - `DATA_ZERO_ETL` — replicate Multi-AZ DB cluster → Redshift via zero-ETL
  - `LAYER_NETWORKING` — VPC endpoint for `secretsmanager` + `rds-data`
  - `LAYER_SECURITY` — KMS CMK + permission boundary patterns

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — covers all 3 Multi-AZ flavors (legacy DB instance / modern DB cluster / Aurora deployment). Decision tree for picking between them. CDK for both RDS Multi-AZ DB cluster (1 writer + 2 readable standbys) and Aurora provisioned. RDS Proxy multiplexing patterns + per-shape gotchas. IAM auth + reader-routing patterns. Cluster parameter group tuning (synchronous_commit, logical_replication). 5 non-negotiables. Pytest synth harness. Created to fill gap surfaced by F369 data-ecosystem audit (2026-04-26): non-Aurora-Serverless HA was 0% covered; this brings it to full. |
