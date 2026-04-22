# SOP — Data Layer (S3, RDS / Aurora, DynamoDB, Secrets Manager)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+)

---

## 1. Purpose

Persistent data stores:

- **S3 buckets** — object storage (raw data, artifacts, access logs, static sites)
- **RDS / Aurora** — relational DB (PostgreSQL 15 default, Aurora Serverless v2 swap-in)
- **DynamoDB** — key-value + job state (low-latency, streams-enabled)
- **Secrets Manager** — DB credentials, API keys (not flat env vars)
- **Lifecycle rules, PITR, backups** — retention + recovery

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| DB + buckets + consumer workloads all in one stack | **§3 Monolith Variant** |
| Separate `DatabaseStack`, `StorageStack`, `JobLedgerStack` consumed by `ComputeStack` | **§4 Micro-Stack Variant** |

**Why the split matters.** Every `bucket.grant_*(role)` and `table.grant_*(role)` across stacks auto-modifies the bucket policy / table encryption-key policy with the consumer role ARN. Micro-Stack variant uses identity-side grants throughout (see `LAYER_SECURITY` §4.2).

---

## 3. Monolith Variant

### 3.1 S3 buckets

```python
import aws_cdk as cdk
from aws_cdk import RemovalPolicy, Duration, aws_s3 as s3


def _create_s3(self, stage: str) -> None:
    common = dict(
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.audio_data_key,
        enforce_ssl=True,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        versioned=True,
        removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
        auto_delete_objects=stage != "prod",
    )
    self.audio_bucket = s3.Bucket(
        self, "AudioBucket",
        bucket_name=f"{{project_name}}-audio-{stage}",
        event_bridge_enabled=True,     # → EventBridge for upload events
        lifecycle_rules=[s3.LifecycleRule(
            transitions=[s3.Transition(
                storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                transition_after=Duration.days(90),
            )],
        )],
        **common,
    )
    self.transcript_bucket = s3.Bucket(
        self, "TranscriptBucket",
        bucket_name=f"{{project_name}}-transcripts-{stage}",
        lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
        **common,
    )
    self.reports_bucket = s3.Bucket(
        self, "ReportsBucket",
        bucket_name=f"{{project_name}}-reports-{stage}",
        lifecycle_rules=[s3.LifecycleRule(
            transitions=[s3.Transition(
                storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                transition_after=Duration.days(30),
            )],
        )],
        **common,
    )
    self.access_logs_bucket = s3.Bucket(
        self, "AccessLogsBucket",
        bucket_name=f"{{project_name}}-access-logs-{stage}",
        encryption=s3.BucketEncryption.S3_MANAGED,  # log buckets typically use SSE-S3
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        removal_policy=common["removal_policy"],
        auto_delete_objects=common["auto_delete_objects"],
    )
```

### 3.2 RDS PostgreSQL

```python
from aws_cdk import aws_rds as rds, aws_ec2 as ec2, aws_secretsmanager as sm


self.db_secret = rds.DatabaseSecret(
    self, "DbSecret",
    secret_name=f"{{project_name}}-db-{stage}",
    username="app_admin",
)

self.rds_instance = rds.DatabaseInstance(
    self, "Rds",
    instance_identifier=f"{{project_name}}-rds-{stage}",
    engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_15),
    instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM),
    vpc=self.vpc,
    vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
    security_groups=[self.rds_sg],
    credentials=rds.Credentials.from_secret(self.db_secret),
    allocated_storage=50,
    storage_encrypted=True,
    storage_encryption_key=self.job_metadata_key,
    database_name="app",
    backup_retention=Duration.days(7 if stage == "prod" else 0),
    multi_az=(stage == "prod"),
    removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
    deletion_protection=(stage == "prod"),
    iam_authentication=True,
)
```

### 3.3 DynamoDB

```python
from aws_cdk import aws_dynamodb as ddb


self.ddb_tables = {}
self.ddb_tables["jobs_ledger"] = ddb.Table(
    self, "JobsLedger",
    table_name=f"{{project_name}}-jobs-ledger-{stage}",
    partition_key=ddb.Attribute(name="job_id",   type=ddb.AttributeType.STRING),
    sort_key=ddb.Attribute(     name="stage_ts", type=ddb.AttributeType.STRING),
    billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
    encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
    encryption_key=self.job_metadata_key,
    time_to_live_attribute="ttl",
    stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
    point_in_time_recovery=(stage == "prod"),
    removal_policy=RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY,
)
self.ddb_tables["jobs_ledger"].add_global_secondary_index(
    index_name="by-user",
    partition_key=ddb.Attribute(name="user_id",    type=ddb.AttributeType.STRING),
    sort_key=ddb.Attribute(     name="created_at", type=ddb.AttributeType.STRING),
    projection_type=ddb.ProjectionType.ALL,
)
self.ddb_tables["jobs_ledger"].add_global_secondary_index(
    index_name="by-status",
    partition_key=ddb.Attribute(name="status",     type=ddb.AttributeType.STRING),
    sort_key=ddb.Attribute(     name="updated_at", type=ddb.AttributeType.STRING),
    projection_type=ddb.ProjectionType.KEYS_ONLY,
)

self.ddb_tables["audit_log"] = ddb.Table(
    self, "AuditLog",
    table_name=f"{{project_name}}-audit-log-{stage}",
    partition_key=ddb.Attribute(name="event_id", type=ddb.AttributeType.STRING),
    billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
    encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
    encryption_key=self.job_metadata_key,
    point_in_time_recovery=True,  # immutable-ish audit trail
)
```

### 3.4 Monolith gotchas

- `event_bridge_enabled=True` is implemented by CDK as a **custom resource** (`Custom::S3BucketNotifications`) — not by a property on the `AWS::S3::Bucket` itself. Test assertions that look at the bucket's `NotificationConfiguration` property will miss it.
- RDS in `PRIVATE_ISOLATED` requires the VPC to declare isolated subnets.
- DDB `stream=NEW_AND_OLD_IMAGES` enables stream but the consumer Lambda event source is wired separately (in `LAYER_BACKEND_LAMBDA`).

---

## 4. Micro-Stack Variant

### 4.1 `StorageStack`

```python
import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy, Duration,
    aws_s3 as s3,
    aws_kms as kms,
)
from constructs import Construct


class StorageStack(cdk.Stack):
    def __init__(self, scope: Construct, audio_data_key: kms.IKey, **kwargs) -> None:
        super().__init__(scope, "{project_name}-storage", **kwargs)

        common = dict(
            encryption=s3.BucketEncryption.KMS,
            encryption_key=audio_data_key,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,  # POC; RETAIN in prod
            auto_delete_objects=True,
        )
        self.audio_bucket = s3.Bucket(
            self, "AudioBucket",
            bucket_name="{project_name}-audio",
            event_bridge_enabled=True,
            lifecycle_rules=[s3.LifecycleRule(transitions=[s3.Transition(
                storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                transition_after=Duration.days(90))])],
            **common,
        )
        self.transcript_bucket = s3.Bucket(self, "TranscriptBucket",
            bucket_name="{project_name}-transcripts",
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
            **common,
        )
        self.reports_bucket = s3.Bucket(self, "ReportsBucket",
            bucket_name="{project_name}-reports",
            lifecycle_rules=[s3.LifecycleRule(transitions=[s3.Transition(
                storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                transition_after=Duration.days(30))])],
            **common,
        )
        self.access_logs_bucket = s3.Bucket(self, "AccessLogsBucket",
            bucket_name="{project_name}-access-logs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        for out in [
            ("AudioBucketName",       self.audio_bucket.bucket_name),
            ("TranscriptBucketName",  self.transcript_bucket.bucket_name),
            ("ReportsBucketName",     self.reports_bucket.bucket_name),
        ]:
            cdk.CfnOutput(self, out[0], value=out[1])
```

### 4.2 `DatabaseStack`

```python
from aws_cdk import (
    RemovalPolicy, Duration,
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_kms as kms,
)


class DatabaseStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        vpc: ec2.IVpc,
        rds_sg: ec2.ISecurityGroup,
        job_metadata_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, "{project_name}-database", **kwargs)

        self.db_secret = rds.DatabaseSecret(
            self, "DbSecret",
            secret_name="{project_name}-db",
            username="app_admin",
        )
        self.rds = rds.DatabaseInstance(
            self, "Rds",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_15),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[rds_sg],
            credentials=rds.Credentials.from_secret(self.db_secret),
            allocated_storage=50,
            storage_encrypted=True,
            storage_encryption_key=job_metadata_key,
            database_name="app",
            backup_retention=Duration.days(0),        # POC; 7d+ in prod
            multi_az=False,                            # POC
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
            iam_authentication=True,
        )

        self.db_endpoint = self.rds.db_instance_endpoint_address

        cdk.CfnOutput(self, "DbEndpoint",  value=self.db_endpoint)
        cdk.CfnOutput(self, "DbSecretArn", value=self.db_secret.secret_arn)
```

### 4.3 `JobLedgerStack`

```python
from aws_cdk import (
    RemovalPolicy,
    aws_dynamodb as ddb,
    aws_kms as kms,
)


class JobLedgerStack(cdk.Stack):
    def __init__(self, scope: Construct, job_metadata_key: kms.IKey, **kwargs) -> None:
        super().__init__(scope, "{project_name}-job-ledger", **kwargs)

        self.jobs_ledger = ddb.Table(
            self, "JobsLedger",
            table_name="{project_name}-jobs-ledger",
            partition_key=ddb.Attribute(name="job_id",   type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="stage_ts", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=job_metadata_key,
            time_to_live_attribute="ttl",
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery=False,  # POC; True in prod
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.jobs_ledger.add_global_secondary_index(
            index_name="by-user",
            partition_key=ddb.Attribute(name="user_id",    type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="created_at", type=ddb.AttributeType.STRING),
        )
        self.jobs_ledger.add_global_secondary_index(
            index_name="by-status",
            partition_key=ddb.Attribute(name="status",     type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(     name="updated_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.KEYS_ONLY,
        )

        self.audit_log = ddb.Table(
            self, "AuditLog",
            table_name="{project_name}-audit-log",
            partition_key=ddb.Attribute(name="event_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=job_metadata_key,
        )

        cdk.CfnOutput(self, "JobsLedgerName", value=self.jobs_ledger.table_name)
        cdk.CfnOutput(self, "AuditLogName",   value=self.audit_log.table_name)
```

### 4.4 Downstream consumer pattern (for reference, full code in `LAYER_BACKEND_LAMBDA`)

```python
# NEVER do this cross-stack:
# self.jobs_ledger.grant_read_data(upload_fn)   # mutates JobLedgerStack → cycle
# self.audio_bucket.grant_put(upload_fn)        # mutates StorageStack + SecurityStack KMS → cycle

# ALWAYS do this instead — identity-side on consumer role:
from aws_cdk import aws_iam as iam


def _ddb_grant(fn, table, actions):
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=actions,
        resources=[table.table_arn, f"{table.table_arn}/index/*"],
    ))


def _s3_grant(fn, bucket, actions):
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=actions, resources=[bucket.arn_for_objects("*")]
    ))
```

### 4.5 Micro-stack gotchas

- **`ddb.Table` + cross-stack encryption key** — `encryption_key=external_key` does NOT auto-mutate the key's policy in micro-stack mode (the key is referenced by ARN; DDB uses the *owner account's* IAM). This is safe.
- **`rds.DatabaseInstance` + cross-stack KMS key** — same as DDB, safe. The KMS grant happens at *account-root* level implicitly.
- **BUT** `bucket.encryption_key=external_key` + later `bucket.grant_read(role_in_another_stack)` still creates the cycle because `grant_read` propagates Decrypt to the external key.
- **PITR on DDB**: enables a pointer-in-time recovery — free to turn on for small tables but costs per GB continuously after enablement.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC, no separate lifecycle for storage vs compute | Monolith |
| Storage outlives compute (data retention policy) | Micro-Stack — StorageStack has `RemovalPolicy.RETAIN`; ComputeStack is disposable |
| Need Aurora Serverless for variable load | Swap `rds.DatabaseInstance` → `rds.DatabaseCluster` with Serverless v2 config |
| Need > 5-minute point-in-time rollback on DDB | `point_in_time_recovery=True` + adjust |

---

## 6. Worked example

```python
def test_job_ledger_stack_has_gsis():
    import aws_cdk as cdk
    from aws_cdk import aws_kms as kms
    from aws_cdk.assertions import Template, Match
    from infrastructure.cdk.stacks.job_ledger_stack import JobLedgerStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    sec = cdk.Stack(app, "Sec", env=env)
    key = kms.Key(sec, "MetaKey")

    jls = JobLedgerStack(app, job_metadata_key=key, env=env)
    t = Template.from_stack(jls)
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "GlobalSecondaryIndexes": Match.array_with([
            Match.object_like({"IndexName": "by-user"}),
            Match.object_like({"IndexName": "by-status"}),
        ])
    })
```

---

## 7. References

- `docs/template_params.md` — `RDS_*`, `DDB_*`, `S3_*` lifecycle vars
- `docs/Feature_Roadmap.md` — S-01..S-22, D-00..D-25, DY-01..DY-13
- Related SOPs: `LAYER_SECURITY` (KMS), `LAYER_BACKEND_LAMBDA` (consumers + identity-side grant helpers)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Explicit custom-resource note on `event_bridge_enabled`. Emphasis on cross-stack grant pattern for consumers. |
| 1.0 | 2026-03-05 | Initial. |
