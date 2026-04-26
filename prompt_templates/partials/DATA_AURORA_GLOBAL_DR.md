# SOP — Aurora Global Database (cross-region DR · RPO ≤ 1s · RTO ≤ 1 min · write forwarding)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Aurora MySQL 8.0.x / PostgreSQL 16.x · Aurora Global Database (1 primary region + up to 5 secondary regions) · Managed switchover (planned) and unplanned failover · Write forwarding from secondary regions · Route 53 health checks for DNS failover · AWS Backup cross-region copy as secondary DR layer

---

## 1. Purpose

- Codify the **Aurora Global Database pattern** for cross-region disaster recovery: typical RPO ≤ 1 second, managed RTO ≤ 1 minute via switchover, ~30 sec via DNS failover.
- Distinguish **switchover** (planned, no data loss, app cutover) vs **failover** (unplanned region outage, possible recent-write loss).
- Codify the **write forwarding** pattern (write to secondary, replicated forward to primary) — useful for active-passive multi-region apps.
- Codify the **Route 53 + health check** pattern for app-side DNS failover when the writer region goes dark.
- Codify the **AWS Backup cross-region copy** as a second-tier DR (RPO 24h, immune to Aurora-specific outages — true bit-for-bit independent restore path).
- This is the **cross-region DR specialisation**. `DATA_AURORA_SERVERLESS_V2` covers in-region; `DATA_RDS_MULTIAZ_CLUSTER` covers in-region multi-AZ. This is the only path for cross-region.

When the SOW signals: "RPO < 1 minute", "RTO < 5 minutes", "regional failure scenario", "active-active multi-region", "regulated workload requiring DR", "ransomware-immune restore path".

---

## 2. Decision tree

```
DR requirement?
├── In-region only (1 AZ outage) → §DATA_RDS_MULTIAZ_CLUSTER (3-AZ standbys)
├── Cross-region passive (warm standby, read-only secondary) → §3 Aurora Global, no write forwarding
├── Cross-region active (writes from any region, eventual consistency) → §4 Aurora Global w/ write forwarding
├── Air-gapped DR (immune to Aurora-specific issues) → §5 AWS Backup cross-region copy + restore drill
└── Multi-region active-active strong consistency → NOT possible w/ Aurora; consider DynamoDB Global Tables

Failover SLA?
├── < 1 min, planned → §6 Switchover
├── < 5 min, unplanned → §7 Managed failover
├── < 30 sec, app-handled → §8 Route 53 health check + DNS failover
└── Manual, hours → AWS Backup restore (last resort)
```

### 2.1 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC — global cluster + secondary cluster + Route 53 + Backup all in one stack (per region) | **§3 Monolith Variant** |
| Cross-region: `PrimaryStack` in us-east-1, `SecondaryStack` in us-west-2 | **§9 Micro-Stack Variant** |

**Why the split.** Aurora Global Database creates the "global" object in the primary region; secondary clusters are added in other regions referencing the global ID. Stack-per-region keeps CFN deploys regional. Cross-region SSM published.

---

## 3. Monolith Variant — Aurora Global Database (passive secondary)

### 3.1 Architecture

```
   ┌────────────────────────────────────────────────────────────────┐
   │  Region: us-east-1 (PRIMARY)                                    │
   │                                                                  │
   │  Aurora Global Cluster: app-global-prod                         │
   │      ├── Primary cluster: app-primary-use1                       │
   │      │     ├── Writer (db.r6g.xlarge)                           │
   │      │     ├── Reader 1 (db.r6g.xlarge)                         │
   │      │     └── Reader 2 (db.r6g.xlarge)                         │
   │      │                                                            │
   │      │  Reads/writes from app in us-east-1                       │
   │      │  Storage: 6-way replicated across 3 AZs                   │
   │      └── Cross-region replication (< 1s lag) ──┐                 │
   └─────────────────────────────────────────────────┼────────────────┘
                                                      │
                                                      ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  Region: us-west-2 (SECONDARY, read-only)                       │
   │                                                                  │
   │      └── Secondary cluster: app-secondary-usw2                   │
   │            ├── Reader 1 (db.r6g.xlarge)                          │
   │            └── Reader 2 (db.r6g.xlarge)                          │
   │                                                                  │
   │      Read traffic from app in us-west-2 (LOCAL READS)            │
   │      Promotion candidate during DR                               │
   │      Storage: 6-way replicated across 3 AZs                      │
   └────────────────────────────────────────────────────────────────┘
                                                      ▲
                                                      │
   ┌──────────────────────────────────────────────────┴─────────────┐
   │  Route 53 hosted zone: app.example.com                          │
   │      Primary record  → app-primary-use1 endpoint, weight 100    │
   │      Secondary record → app-secondary-usw2 endpoint, weight 0   │
   │      Failover policy: PRIMARY → SECONDARY on health check fail  │
   └────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — primary region (`app-primary-use1`)

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_rds as rds,
    aws_secretsmanager as sm,
    aws_ec2 as ec2,
)


def _create_aurora_global_primary(self, stage: str) -> None:
    """Monolith. Aurora Global Database — primary cluster in us-east-1.
    Secondary cluster created via separate cross-region stack."""

    self.db_secret = rds.DatabaseSecret(self, "AuroraSecret",
        secret_name=f"{{project_name}}-aurora-{stage}",
        username="app_admin",
    )

    # A) The Aurora cluster (primary). It will be ASSOCIATED with a global
    # cluster after creation via custom resource (no native CDK L2 for global).
    # ─── NOTE: Aurora Global Database support in CDK L2 is partial as of 2026-04.
    # We use L1 + manual association.
    self.primary_cluster = rds.DatabaseCluster(self, "PrimaryCluster",
        cluster_identifier=f"{{project_name}}-primary-{stage}",
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
            preferred_window="03:00-04:00",
        ),
        deletion_protection=(stage == "prod"),
        iam_authentication=True,
        # Required for Global Database
        # NOTE: Aurora Global requires the cluster engine to be Aurora; not RDS-Postgres
    )

    # B) Global cluster — CDK L1 (CfnGlobalCluster) wraps it
    self.global_cluster = rds.CfnGlobalCluster(self, "GlobalCluster",
        global_cluster_identifier=f"{{project_name}}-global-{stage}",
        source_db_cluster_identifier=self.primary_cluster.cluster_arn,
        engine="aurora-postgresql",
        engine_version="16.4",
        storage_encrypted=True,
        deletion_protection=(stage == "prod"),
    )
    # Ensure global cluster is created AFTER primary cluster
    self.global_cluster.add_dependency(
        self.primary_cluster.node.default_child)

    # C) Cross-region replication settings
    # Aurora Global automatically maintains a 1-second-lag replica in
    # secondary regions. Nothing else to configure on primary side.

    # D) Outputs for secondary stack to consume
    CfnOutput(self, "GlobalClusterId",
              value=self.global_cluster.global_cluster_identifier)
    CfnOutput(self, "PrimaryClusterArn",
              value=self.primary_cluster.cluster_arn)
    # Publish to SSM for cross-region pickup
    ssm.StringParameter(self, "GlobalIdSsm",
        parameter_name=f"/{{project_name}}/{stage}/aurora-global/id",
        string_value=self.global_cluster.global_cluster_identifier)
```

### 3.3 CDK — secondary region (`app-secondary-usw2`)

This is a separate CDK stack deployed to `us-west-2`:

```python
def _create_aurora_global_secondary(self, stage: str, primary_region: str) -> None:
    """Cross-region stack. Reads global cluster ID from SSM (us-east-1)
    and creates a secondary cluster joined to it."""

    # SSM cross-region read — requires custom resource (CDK doesn't natively
    # cross-region SSM lookup). Use boto3 in a one-shot CR Lambda.
    global_id_lookup = cr.AwsCustomResource(self, "GlobalIdLookup",
        on_create=cr.AwsSdkCall(
            service="SSM",
            action="getParameter",
            region=primary_region,
            parameters={
                "Name": f"/{{project_name}}/{stage}/aurora-global/id",
            },
            physical_resource_id=cr.PhysicalResourceId.of("GlobalIdLookup"),
        ),
        on_update=cr.AwsSdkCall(
            service="SSM", action="getParameter", region=primary_region,
            parameters={"Name": f"/{{project_name}}/{stage}/aurora-global/id"},
            physical_resource_id=cr.PhysicalResourceId.of("GlobalIdLookup"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
            resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE,
        ),
    )
    global_id = global_id_lookup.get_response_field("Parameter.Value")

    # A) Secondary cluster — joins the global cluster
    self.secondary_cluster = rds.CfnDBCluster(self, "SecondaryCluster",
        db_cluster_identifier=f"{{project_name}}-secondary-{stage}",
        engine="aurora-postgresql",
        engine_version="16.4",
        global_cluster_identifier=global_id,                  # JOIN the global
        # secondary clusters CANNOT have a master_username / master_user_password
        # they inherit from the primary
        # secondary_cluster auto-replicates from primary
        db_subnet_group_name=self.subnet_group.db_subnet_group_name,
        vpc_security_group_ids=[self.rds_sg.security_group_id],
        kms_key_id=self.kms_key.key_arn,
        storage_encrypted=True,
        backup_retention_period=35 if stage == "prod" else 7,
        deletion_protection=(stage == "prod"),
        # NO writer instance config — secondary is read-only
    )

    # B) Reader instances
    for i in range(1, 3):
        rds.CfnDBInstance(self, f"SecondaryReader{i}",
            db_cluster_identifier=self.secondary_cluster.db_cluster_identifier,
            db_instance_class="db.r6g.xlarge",
            engine="aurora-postgresql",
            publicly_accessible=False,
        )

    # C) Promote-to-primary capability is exposed via the
    # ModifyGlobalCluster API call — NOT in CFN. See §6 switchover.

    CfnOutput(self, "SecondaryClusterId",
              value=self.secondary_cluster.db_cluster_identifier)
```

---

## 4. Write forwarding variant (active-active read, write-forwarded)

```python
# In secondary cluster CDK — enable write forwarding
secondary_cluster.enable_global_write_forwarding = True

# App in us-west-2 connects to secondary writer endpoint:
# - Reads served locally (low latency)
# - Writes auto-forwarded to primary in us-east-1
# - Eventually consistent — secondary sees its own writes after primary replicates back
```

Use case: customer-facing app deployed in 5 regions; users write rarely but read often. Each region has its own secondary cluster; writes go cross-region but reads are local.

**Limit:** write forwarding adds 50-200ms latency on writes (cross-region RTT). Not for high-write workloads.

---

## 5. AWS Backup cross-region copy (independent DR layer)

Aurora Global Database is a **synchronous replication** path. If a logical bug corrupts data, the corruption replicates to secondaries instantly. AWS Backup with cross-region copy is the **last line of defense**:

```python
import aws_cdk.aws_backup as backup

# Backup vault in us-east-1 (with KMS CMK + Vault Lock)
self.primary_vault = backup.BackupVault(self, "PrimaryVault",
    backup_vault_name=f"{{project_name}}-primary-vault-{stage}",
    encryption_key=self.kms_key,
    removal_policy=RemovalPolicy.RETAIN,
)
# Cross-region vault in us-west-2 — receives copies
# (Created via separate stack in us-west-2; ARN published via SSM)

# Backup plan: daily snapshot, 35-day retention, copy to secondary region
self.backup_plan = backup.BackupPlan(self, "DailyAuroraBackup",
    backup_plan_name=f"{{project_name}}-aurora-daily-{stage}",
    backup_plan_rules=[backup.BackupPlanRule(
        backup_vault=self.primary_vault,
        rule_name="DailyAuroraSnapshot",
        schedule_expression=events.Schedule.cron(hour="2"),  # 02:00 UTC
        delete_after=Duration.days(35),
        copy_actions=[backup.BackupPlanCopyActionProps(
            destination_backup_vault=backup.BackupVault.from_backup_vault_arn(
                self, "SecondaryVault",
                # ARN from us-west-2 vault, via SSM
                backup_vault_arn=ssm.StringParameter.value_for_string_parameter(
                    self, f"/{{project_name}}/{stage}/aurora-secondary-vault-arn"),
            ),
            delete_after=Duration.days(35),
            move_to_cold_storage_after=Duration.days(7),
        )],
        # Vault Lock — make backups immutable (ransomware protection)
        # Configure separately on the vault, not the rule
    )],
)
self.backup_plan.add_selection("AuroraSelection",
    resources=[backup.BackupResource.from_arn(self.primary_cluster.cluster_arn)],
)
```

---

## 6. Switchover (planned, no data loss)

Switchover is a managed operation that promotes a secondary to primary, demotes the old primary to secondary. Use for:
- Major maintenance on primary region
- Compliance-mandated regional rotation
- Pre-staged DR drill

```bash
# CLI command — no CDK equivalent (use Step Functions to orchestrate)
aws rds switchover-global-cluster \
  --global-cluster-identifier app-global-prod \
  --target-db-cluster-identifier arn:aws:rds:us-west-2:000000000000:cluster:app-secondary-prod \
  --region us-east-1
```

Behavior:
1. Stops writes to current primary.
2. Waits for replication lag = 0.
3. Promotes target secondary to primary.
4. Demotes old primary to secondary.
5. Returns ~1 minute later.

---

## 7. Managed failover (unplanned, possible data loss)

For region outage, switchover CAN'T complete (primary unreachable). Use `failover-global-cluster`:

```bash
# Forcibly promote a secondary, accept potential data loss
aws rds failover-global-cluster \
  --global-cluster-identifier app-global-prod \
  --target-db-cluster-identifier arn:aws:rds:us-west-2:000000000000:cluster:app-secondary-prod \
  --allow-data-loss \
  --region us-west-2
```

After failover, the old primary (when it recovers) MUST be removed from the global cluster and re-added as a fresh secondary. Stale primary data is not auto-reconciled.

---

## 8. Route 53 + health check for app-side DNS failover

```python
# In each region's stack — Route 53 record + health check
zone = route53.HostedZone.from_lookup(self, "Zone", domain_name="example.com")

# Primary region's record
route53.ARecord(self, "AppDnsPrimary",
    zone=zone,
    record_name="app",
    target=route53.RecordTarget.from_alias(
        targets.LoadBalancerTarget(self.alb_primary)),
    set_identifier="primary-use1",
    failover=route53.Failover.PRIMARY,
    health_check=route53.CfnHealthCheck.HealthCheckConfigProperty(
        type="HTTPS",
        fully_qualified_domain_name=self.alb_primary.load_balancer_dns_name,
        port=443,
        resource_path="/healthz",
        request_interval=30,
        failure_threshold=3,
    ),
    ttl=Duration.seconds(60),                  # 60s TTL = 60s max DNS propagation delay
)
```

---

## 9. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Replication lag > 1 sec | Network congestion or large transactions | Inspect `AuroraGlobalDBProgressLag` CloudWatch metric; scale primary writer up |
| Switchover stalls at "promoting" | Replication lag still pending | Pre-check lag with `aws rds describe-global-clusters`; only switchover when lag = 0 |
| Failover succeeded but old primary won't rejoin | Stale state | Manually remove old primary via `remove-from-global-cluster`, then add fresh secondary |
| App writes succeed in secondary but disappear | Write forwarding lag | Eventually-consistent reads may not see your own writes for 1-3s; use `select_endpoint=primary` for strong reads |
| Cross-region SSM lookup fails on first deploy | Race condition | Custom resource with explicit `on_create` + `on_update` + retries 3x |
| Backup copy to secondary region fails | KMS key mismatch | Each region needs its own CMK; backup copy specifies `destination_backup_vault` ARN |
| Vault Lock prevents backup deletion | Compliance mode | Vault Lock COMPLIANCE is permanent. Use GOVERNANCE in dev |

### 9.1 Cost ballpark

| Component | Monthly $ |
|---|---|
| Primary cluster (3 × r6g.xlarge) | $930 |
| Secondary cluster (2 × r6g.xlarge, no writer) | $620 |
| Cross-region replication (storage I/O × 1 MB/s) | $86 |
| Backup vault (35-day retention, 200 GB) | $9 |
| Cross-region backup copy data transfer (200 GB × 4/mo) | $18 |
| Route 53 health checks (2 endpoints × 1 min interval) | $1 |
| **Total per global cluster** | **~$1,664 / mo** |

---

## 10. Five non-negotiables

1. **Switchover BEFORE failover.** Always try `switchover-global-cluster` first; only use `failover-global-cluster` if primary is genuinely unreachable. Failover with `--allow-data-loss` literally drops the most-recent transactions.

2. **Backup vault MUST have Vault Lock enabled in production.** Without Vault Lock, a compromised IAM principal can delete backups. Vault Lock COMPLIANCE prevents that — even root account can't override.

3. **Test DR drill quarterly, document the runbook.** Aurora Global Database is one switchover from prod-down. Without quarterly drills, you find out at 3am during a real outage that your app couldn't reconnect.

4. **Write forwarding is for low-write apps only.** > 100 writes/sec from secondary → consider true active-active with DynamoDB Global Tables or per-region Aurora clusters w/ application-level sync.

5. **Storage encryption MUST use a per-region CMK.** Cross-region encryption requires the destination region to have its own CMK; sharing a single key across regions creates dependency on the key's home region — defeats the DR purpose.

---

## 11. References

- `docs/template_params.md` — `AURORA_GLOBAL_PRIMARY_REGION`, `AURORA_GLOBAL_SECONDARY_REGIONS`, `AURORA_GLOBAL_WRITE_FORWARDING_ENABLED`, `BACKUP_CROSS_REGION_COPY_ENABLED`
- AWS docs:
  - [Using Aurora Global Database](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database.html)
  - [Switchover and failover](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database-disaster-recovery.html)
  - [Aurora replication](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Replication.html)
  - [DR options whitepaper](https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html)
- Related SOPs:
  - `DATA_AURORA_SERVERLESS_V2` — in-region Serverless v2 (different DR model)
  - `DATA_RDS_MULTIAZ_CLUSTER` — in-region 3-AZ HA (distinct from cross-region DR)
  - `GLOBAL_MULTI_REGION` — broader multi-region patterns (CloudFront + S3 replication etc.)
  - `LAYER_NETWORKING` — cross-region VPC peering / Transit Gateway (NOT required for Aurora Global; Aurora uses its own backbone)

---

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Aurora Global Database for cross-region DR with RPO ≤ 1s / RTO ≤ 1 min. CDK for primary + secondary stacks (cross-region split). Switchover (planned) + failover (unplanned w/ data loss) procedures. Write forwarding for active-passive multi-region apps. AWS Backup cross-region copy as independent DR layer w/ Vault Lock. Route 53 health-check + DNS failover. 5 non-negotiables incl. switchover-before-failover + quarterly DR drills. Cost ballpark $1,664/mo per global cluster. Created to fill F369 audit gap (2026-04-26): cross-region DR was 0% covered. |
