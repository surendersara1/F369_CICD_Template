# SOP — Multi-Region DR (Backup-Restore · Pilot Light · Warm Standby · Active-Active · RPO/RTO matrix · failover orchestration)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Multi-region DR · Backup-Restore / Pilot Light / Warm Standby / Active-Active patterns · Aurora Global Database · DynamoDB Global Tables · S3 Cross-Region Replication (CRR) + Replication Time Control (RTC) · Route 53 health checks + failover routing · multi-region KMS keys · multi-region Secrets Manager replication

---

## 1. Purpose

- Codify the **4 canonical DR patterns** with explicit RPO / RTO targets + cost trade-offs:
  1. **Backup-Restore** — RPO hours, RTO hours, cheapest
  2. **Pilot Light** — RPO mins, RTO 10s of mins, low cost
  3. **Warm Standby** — RPO seconds, RTO < 10 min, medium cost
  4. **Active-Active** — RPO ~0, RTO ~0, highest cost
- Codify per-component multi-region replication patterns:
  - **Aurora Global Database** (cross-region DB; RPO < 1s, failover < 1 min)
  - **DynamoDB Global Tables v2** (active-active multi-region)
  - **S3 CRR + RTC** (15-min RPO bound)
  - **KMS multi-region keys** (cross-region decrypt without re-encrypt)
  - **Secrets Manager replication** (replicate secret to N regions)
  - **ECR cross-region replication**
  - **CloudFormation StackSets / CDK pipelines** (deploy stacks to both regions)
- Codify the **failover orchestration**: Route 53 health checks + failover routing OR Route 53 ARC routing controls (manual cut).
- Codify **DR runbook + game day** discipline.
- Pairs with `DR_ROUTE53_ARC` (failover control plane), `DR_RESILIENCE_HUB_FIS` (chaos engineering), `DR_BACKUP_VAULT_LOCK` (immutable backups).

When the SOW signals: "DR plan", "RPO/RTO", "regional failover", "regulatory DR requirement", "active-active across regions".

---

## 2. Decision tree — pick a DR pattern per workload

| Workload | RPO acceptable | RTO acceptable | Cost-sensitive | Recommended pattern |
|---|---|---|---|---|
| Internal admin tools | hours | hours | yes | Backup-Restore |
| Most prod web apps | minutes | < 30 min | yes | Pilot Light |
| Customer-facing API | < 5 min | < 10 min | medium | Warm Standby |
| Trading / payments / safety-critical | ~ 0 | ~ 0 | no | Active-Active |
| Analytics / reporting (downstream) | hours | hours | yes | Backup-Restore (S3 CRR) |
| Stateful microservices | seconds | < 5 min | medium | Warm Standby with Aurora Global |

```
RPO/RTO targets per pattern:

   Pattern         RPO        RTO         Standby cost (vs primary)
   ────────────────────────────────────────────────────────────────
   Backup-Restore  4-24 hours 4-24 hours  ~5-10%   (just storage)
   Pilot Light     5-15 min   10-60 min   ~15-25%  (DB always-on, app off)
   Warm Standby    seconds    < 10 min    ~50%     (DB + app scaled-down)
   Active-Active   ~0         ~0          ~100%    (full capacity DR region)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single workload Pilot Light DR | **§3 Monolith** |
| Production — full Active-Active or Warm Standby with multiple DBs | **§5 Production** |

---

## 3. Pilot Light Variant (recommended default)

### 3.1 Architecture

```
   Primary Region (us-east-1) — ACTIVE
   ┌─────────────────────────────────────────────────────────────────┐
   │ ALB → ECS/Lambda → Aurora PG (writer) → S3 (assets)              │
   │ Live traffic via Route 53 PRIMARY record                          │
   └────────────────────────────┬────────────────────────────────────┘
                                │ replication
                                ▼
   DR Region (us-west-2) — STANDBY (data only, compute off)
   ┌─────────────────────────────────────────────────────────────────┐
   │ Aurora Global secondary (read-only, RPO < 1s)                     │
   │ S3 CRR replica (RPO ~ 15 min with RTC)                            │
   │ DDB Global Table replica (active-active by default)               │
   │ KMS multi-region key replica                                       │
   │ Secrets Manager replica                                            │
   │ ECR cross-region replication                                       │
   │ ALB exists but with 0 target groups OR ECS/Lambda with 0 capacity │
   │ Route 53 SECONDARY record with health check                        │
   │                                                                    │
   │ On failover:                                                       │
   │   1. Promote Aurora Global secondary → primary writer (1 min)      │
   │   2. Scale up ECS service (5 min) OR enable Lambda concurrency     │
   │   3. Health check passes → Route 53 routes traffic                 │
   │   Total RTO: ~10-15 min                                             │
   └─────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — primary region

```python
# stacks/dr_primary_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_kms as kms
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_rds as rds
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_route53 as r53
from aws_cdk import aws_iam as iam
from constructs import Construct


class DrPrimaryStack(Stack):
    """Deploys to PRIMARY region (us-east-1)."""

    def __init__(self, scope: Construct, id: str, *,
                 dr_region: str = "us-west-2", **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Multi-region KMS CMK ──────────────────────────────────
        # Multi-region key with replica in DR region — cross-region decrypt
        primary_key = kms.CfnKey(self, "PrimaryKey",
            description="Multi-region CMK for DR",
            enable_key_rotation=True,
            multi_region=True,                    # KEY: enables replica
            key_policy={
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "EnableRoot",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{self.account}:root"},
                    "Action": "kms:*",
                    "Resource": "*",
                }],
            },
        )
        primary_key_alias = kms.CfnAlias(self, "PrimaryKeyAlias",
            alias_name="alias/dr-prod",
            target_key_id=primary_key.attr_key_id,
        )

        # ── 2. S3 with Cross-Region Replication + RTC ─────────────────
        primary_bucket = s3.Bucket(self, "PrimaryBucket",
            bucket_name=f"prod-app-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms.Key.from_key_arn(self, "K1", primary_key.attr_arn),
            versioned=True,                        # required for replication
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Replication role
        replication_role = iam.Role(self, "ReplicationRole",
            assumed_by=iam.ServicePrincipal("s3.amazonaws.com"),
        )

        # Cross-region replication with Replication Time Control (15-min SLA)
        primary_bucket.add_replication_policy([
            s3.ReplicationRule(
                destination=s3.ReplicationDestination(
                    bucket=s3.Bucket.from_bucket_name(
                        self, "ReplicaRef",
                        f"prod-app-replica-{self.account}-{dr_region}",
                    ),
                    replication_time=Duration.minutes(15),    # RTC ON
                    metrics=Duration.minutes(15),
                    storage_class=s3.StorageClass.STANDARD_IA,
                    encryption_configuration=s3.ReplicationEncryptionConfig(
                        replica_kms_key_id=f"arn:aws:kms:{dr_region}:{self.account}:alias/dr-prod",
                    ),
                ),
                priority=1,
                delete_marker_replication=False,
            ),
        ], role=replication_role)

        # ── 3. DynamoDB Global Table v2 (active-active) ──────────────
        global_table = ddb.Table(self, "GlobalTable",
            table_name="prod-app-global",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=kms.Key.from_key_arn(self, "K2", primary_key.attr_arn),
            point_in_time_recovery=True,
            replication_regions=[dr_region],          # KEY: makes it global
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
        )

        # ── 4. Secrets Manager replication ────────────────────────────
        db_secret = sm.Secret(self, "DbSecret",
            secret_name="prod-app/db-creds",
            replica_regions=[
                sm.ReplicaRegion(region=dr_region,
                                 encryption_key=kms.Key.from_key_arn(
                                     self, "K3", primary_key.attr_arn,
                                 )),
            ],
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template='{"username": "app"}',
                generate_string_key="password",
                exclude_characters='"@/\\',
                password_length=32,
            ),
        )

        # ── 5. Aurora Global Database (cross-region) ────────────────────
        # Primary cluster
        primary_aurora = rds.DatabaseCluster(self, "PrimaryAurora",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4,
            ),
            credentials=rds.Credentials.from_secret(db_secret),
            instance_props=rds.InstanceProps(
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.MEMORY6_GRAVITON, ec2.InstanceSize.LARGE,
                ),
                vpc=vpc,                            # parameterize
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                publicly_accessible=False,
            ),
            instances=2,                             # writer + 1 reader
            backup=rds.BackupProps(retention=Duration.days(35)),
            storage_encryption_key=kms.Key.from_key_arn(self, "K4", primary_key.attr_arn),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Aurora Global cluster wrapping the primary
        global_cluster = rds.CfnGlobalCluster(self, "GlobalCluster",
            global_cluster_identifier="prod-app-global",
            source_db_cluster_identifier=primary_aurora.cluster_identifier,
        )
        # Note: secondary cluster created in DR region stack (§3.3)

        # ── 6. ECR cross-region replication ───────────────────────────
        # Configure once at registry level (per account):
        # aws ecr put-replication-configuration --replication-configuration ...
        # Or via CDK custom resource.

        # ── 7. Route 53 — public hosted zone with health check ───────
        # (Hosted zone is global; create here but use in both regions)
        zone = r53.HostedZone(self, "Zone",
            zone_name="example.com",
        )

        # Primary ALB ARN (parameterize after ALB created)
        primary_health_check = r53.CfnHealthCheck(self, "PrimaryHealthCheck",
            health_check_config=r53.CfnHealthCheck.HealthCheckConfigProperty(
                type="HTTPS",
                fully_qualified_domain_name="primary.app.example.com",
                resource_path="/healthz",
                request_interval=10,
                failure_threshold=3,
            ),
        )

        # Primary failover record
        r53.CfnRecordSet(self, "PrimaryRecord",
            hosted_zone_id=zone.hosted_zone_id,
            name="app.example.com",
            type="A",
            set_identifier="primary",
            failover="PRIMARY",
            health_check_id=primary_health_check.attr_health_check_id,
            alias_target=r53.CfnRecordSet.AliasTargetProperty(
                dns_name=primary_alb_dns,
                hosted_zone_id=primary_alb_zone_id,
                evaluate_target_health=True,
            ),
        )
```

### 3.3 CDK — DR region (Pilot Light)

```python
# stacks/dr_secondary_stack.py — deployed to DR region
class DrSecondaryStack(Stack):
    def __init__(self, scope, id, *,
                 primary_global_cluster_id: str,
                 primary_kms_key_arn: str,
                 primary_replica_bucket_name: str,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. KMS multi-region replica (no new key, just replica) ────
        replica_key = kms.CfnReplicaKey(self, "ReplicaKey",
            primary_key_arn=primary_kms_key_arn,
            description="DR region replica of primary CMK",
        )

        # ── 2. S3 replica bucket (target of CRR from primary) ────────
        replica_bucket = s3.Bucket(self, "ReplicaBucket",
            bucket_name=primary_replica_bucket_name,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms.Key.from_key_arn(self, "RK", replica_key.attr_arn),
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── 3. Aurora Global secondary cluster ────────────────────────
        secondary_aurora = rds.CfnDBCluster(self, "SecondaryAurora",
            global_cluster_identifier=primary_global_cluster_id,
            engine="aurora-postgresql",
            engine_version="16.4",
            db_cluster_identifier="prod-app-secondary",
            kms_key_id=replica_key.attr_arn,
            db_subnet_group_name="dr-subnet-group",
            vpc_security_group_ids=[dr_sg.security_group_id],
            storage_encrypted=True,
            deletion_protection=True,
        )
        # Add 1-2 reader instances
        rds.CfnDBInstance(self, "SecondaryAuroraReader1",
            db_cluster_identifier=secondary_aurora.ref,
            db_instance_class="db.r6g.large",
            engine="aurora-postgresql",
            publicly_accessible=False,
        )

        # ── 4. ECS service / Lambda — defined but desiredCount=0 ─────
        # On failover, scale up via Auto Scaling target tracking
        # OR via Lambda invocation that calls update-service desired-count.

        # ── 5. ALB — created but with 0 target group registrations ────
        # Same for Lambda — has alias but reserved-concurrency=0

        # ── 6. Route 53 SECONDARY record with health check ────────────
        # (Defined in primary stack since hosted zone is global; references DR ALB)
```

---

## 4. Active-Active Variant — multi-region writes (advanced)

For RPO/RTO ~ 0:
- Aurora Global with **write forwarding** (limited multi-writer) OR application-layer sharded routing
- DynamoDB Global Tables v2 (last-writer-wins)
- Application traffic routed by **Route 53 latency-based routing** OR Global Accelerator (anycast)
- Conflict resolution at app layer (vector clocks, CRDTs, last-writer-wins with timestamps)

**Big trade-off**: doubles cost (full capacity in both regions) + adds complexity (eventually-consistent reads after writes that propagated to remote region).

---

## 5. Production Variant — multi-workload + game day automation

(Combines patterns. See `enterprise/11_multi_region_dr` composite template.)

---

## 6. Common gotchas

- **Multi-region KMS key vs cross-region key copies**: multi-region keys (`MultiRegion=true`) share key material across regions — same Key ID, same plaintext data key. Decrypt-anywhere. Cross-region key copies are different keys — must re-encrypt data on copy.
- **Aurora Global RPO is < 1s but failover takes ~1 min** — promotion is not instant. Apps must retry connection.
- **Aurora Global write forwarding adds 100-300ms latency** to writes from secondary region. Use sparingly.
- **DynamoDB Global Tables conflict resolution = last-writer-wins by item-level timestamp.** Apps that update same item from both regions can lose data unless they include conflict resolution at app layer.
- **S3 CRR doesn't replicate existing objects** — only new puts after replication is configured. Use S3 Batch Replication for backfill.
- **S3 CRR + Object Lock** doesn't replicate the lock state to replica by default. Configure replicaModification on bucket.
- **Route 53 health checks cost $0.50/check/month** + per-evaluation charges. Health-checking 100 endpoints = $50/mo + checks.
- **Failover routing requires public DNS records** — internal services need Route 53 ARC OR App-level discovery.
- **Cross-region data transfer costs** — replicating 10 TB across regions = $200/mo (data transfer alone). Plan for it.
- **EBS snapshots aren't multi-region by default** — use `aws ec2 copy-snapshot` or AWS Backup with cross-region copy.
- **Lambda layers and ECR images need cross-region replication** — use ECR replication rules + CDK `assetReplicas` for Lambda layers.
- **CloudFormation StackSets vs CDK pipelines** — both can deploy to multiple regions. CDK pipelines are easier for app-level deployment; StackSets for org-wide governance.
- **Pilot Light scale-up time** — ECS desired_count from 0 → 10 takes 5-10 min (image pull + ALB target registration + health check). Pre-warm if RTO < 5 min needed.
- **Active-Active without conflict mgmt = data loss** — never deploy active-active without explicit conflict strategy at app layer.

---

## 7. Pytest worked example

```python
# tests/test_dr_pattern.py
import boto3, pytest, time

# Run from primary region; assertions check both regions

primary = boto3.Session(region_name="us-east-1")
secondary = boto3.Session(region_name="us-west-2")


def test_kms_multi_region_replica_active():
    primary_kms = primary.client("kms")
    sec_kms = secondary.client("kms")
    primary_key = primary_kms.describe_key(KeyId="alias/dr-prod")
    assert primary_key["KeyMetadata"]["MultiRegion"] is True
    sec_key = sec_kms.describe_key(KeyId="alias/dr-prod")
    assert sec_key["KeyMetadata"]["MultiRegionConfiguration"]["MultiRegionKeyType"] == "REPLICA"


def test_s3_cross_region_replication_active(primary_bucket):
    s3 = primary.client("s3")
    cfg = s3.get_bucket_replication(Bucket=primary_bucket)
    rules = cfg["ReplicationConfiguration"]["Rules"]
    assert rules[0]["Status"] == "Enabled"
    rt = rules[0]["Destination"].get("ReplicationTime", {})
    assert rt.get("Status") == "Enabled"     # RTC active


def test_aurora_global_cluster_secondary_synced():
    rds = primary.client("rds")
    gc = rds.describe_global_clusters(GlobalClusterIdentifier="prod-app-global")["GlobalClusters"][0]
    members = gc["GlobalClusterMembers"]
    assert len(members) == 2     # 1 primary + 1 secondary
    secondary = next(m for m in members if not m["IsWriter"])
    # Lag < 1s p99 typical
    # Real check: query replication lag from CloudWatch metric AuroraGlobalDBReplicationLag


def test_ddb_global_table_replicas():
    ddb_p = primary.client("dynamodb")
    desc = ddb_p.describe_table(TableName="prod-app-global")["Table"]
    replicas = desc.get("Replicas", [])
    assert len(replicas) >= 2
    assert any(r["RegionName"] == "us-west-2" for r in replicas)


def test_secrets_manager_replicated():
    sm_p = primary.client("secretsmanager")
    secret = sm_p.describe_secret(SecretId="prod-app/db-creds")
    replicated = secret.get("ReplicationStatus", [])
    assert any(r["Region"] == "us-west-2" and r["Status"] == "InSync" for r in replicated)


def test_route53_failover_records_exist():
    r53 = primary.client("route53")
    records = r53.list_resource_record_sets(HostedZoneId=hosted_zone_id)["ResourceRecordSets"]
    failover_records = [r for r in records if r.get("Failover")]
    assert len(failover_records) >= 2     # PRIMARY + SECONDARY
    primary_rec = next(r for r in failover_records if r["Failover"] == "PRIMARY")
    assert primary_rec.get("HealthCheckId")
```

---

## 8. Five non-negotiables

1. **Multi-region KMS keys** for any data replicated cross-region — never re-encrypt on copy.
2. **Aurora Global Database** for stateful workloads (NOT manual snapshot copies for prod DR).
3. **DynamoDB Global Tables v2** for any DDB → multi-region (NOT custom replication scripts).
4. **S3 CRR with Replication Time Control (RTC)** — 15-min RPO; without RTC, replication is best-effort.
5. **Route 53 health checks + failover records** OR Route 53 ARC routing controls — DNS-based failover orchestration must be tested.

---

## 9. References

- [AWS DR Patterns whitepaper](https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html)
- [Aurora Global Database](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database.html)
- [DynamoDB Global Tables v2](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/V2globaltables_HowItWorks.html)
- [S3 Cross-Region Replication + RTC](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-time-control.html)
- [KMS multi-region keys](https://docs.aws.amazon.com/kms/latest/developerguide/multi-region-keys-overview.html)
- [Secrets Manager replication](https://docs.aws.amazon.com/secretsmanager/latest/userguide/create-manage-multi-region-secrets.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. 4 DR patterns + multi-region KMS/Aurora Global/DDB Global/S3 CRR/Secrets/ECR/Route 53 failover. Wave 14. |
