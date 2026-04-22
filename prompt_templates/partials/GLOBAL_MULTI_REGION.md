# SOP — Global / Multi-Region Architecture (Active-Active, Failover, DR)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Route 53 · Global Accelerator · Aurora Global Database · DynamoDB Global Tables · S3 Cross-Region Replication · Lambda Python 3.13 arm64

---

## 1. Purpose

Provision a multi-region AWS footprint with a disciplined primary/secondary topology:

- **AWS Global Accelerator** — anycast IPs and TCP acceleration, with traffic dial flips on failover.
- **Route 53 health-check failover + latency routing** — per-region DNS records wired to regional health probes.
- **Aurora Global Database** — `CfnGlobalCluster` in the primary region, secondary cluster(s) in other regions with <1 s replication lag.
- **DynamoDB Global Tables** (`CfnGlobalTable`) — active-active multi-master replication across regions.
- **S3 Cross-Region Replication (CRR)** — versioned source + replication role; replica buckets in the secondary region.

Include when the SOW mentions multi-region, global users, active-active, RTO/RPO < 1 hour, data residency, or regional DR.

### Strategy selection (business-level)

| Strategy | RTO | RPO | Use when |
|---|---|---|---|
| **Backup & Restore** | hours | hours | Non-critical, pure batch |
| **Pilot Light** | tens of minutes | minutes | DR-only secondary, minimal stand-by infra |
| **Warm Standby** | minutes | seconds | Important services, fast-fail allowed |
| **Active-Active** | seconds | ~0 | Mission-critical, global users |

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| A single `cdk.Stack` class per region that owns VPC + data + compute + the global wiring (Accelerator, Route 53, Global DB joins) | **§3 Monolith Variant** |
| A dedicated `GlobalStack` (one per region) that consumes VPC, KMS, Aurora cluster names, and bucket names from other stacks via SSM, and publishes the Global Accelerator / Route 53 / cross-region replica wiring | **§4 Micro-Stack Variant** |

**Why the split matters.** A multi-region deploy touches resources in two stacks **in two regions**: the primary-region Aurora Global Cluster must exist before the secondary-region replica can join, and the secondary's KMS key must encrypt replication that references a primary-region bucket. Any cross-stack `grant_*` helper forces a bidirectional CloudFormation export across stack boundaries — and CFN rejects that as a circular reference during `cdk synth`.

The Micro-Stack variant:

1. Puts the **Global Accelerator** and **Route 53 hosted zone / failover records** in a dedicated `GlobalStack` that runs in the primary region only.
2. Runs a **per-region** `RegionalGlobalStack` that owns the Aurora secondary cluster, DynamoDB Global Table replica wiring, CRR role, and the regional health Lambda. Upstream VPC, KMS, DB subnet group, and bucket names are read via SSM (`ssm.StringParameter.value_for_string_parameter`).
3. Grants identity-side only on the health Lambda's execution role (no `table.grant_read(fn)` or `bucket.grant_read(fn)` on cross-stack resources).
4. Publishes `GLOBAL_ACCEL_ARN_SSM`, `AURORA_GLOBAL_CLUSTER_ID_SSM`, `DDB_GLOBAL_TABLE_NAME_SSM`, `REGION_HEALTH_FN_ARN_SSM` so the CI/CD partial and other stacks can consume them.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` subclass per region owns VPC + Aurora regional cluster + DynamoDB table + S3 lake bucket + Lambda and the global wiring. Typical for POC, prototype, internal-tool DR.

### 3.1 Architecture

```
Route 53 Health Check Failover / Latency Routing
       │                              │
  us-east-1 (primary)            eu-west-1 (secondary)
  ┌────────────────────┐         ┌────────────────────┐
  │ Global Accelerator │         │ Global Accelerator │
  │ API Gateway        │         │ API Gateway        │
  │ Lambda / ECS       │         │ Lambda / ECS       │
  │ Aurora Global DB ◄──<1s lag──► Aurora Read        │
  │ DynamoDB Global ◄──active-active──► DynamoDB      │
  │ S3 + CRR ──────────────────►   S3 Replica         │
  └────────────────────┘         └────────────────────┘
```

### 3.2 CDK — `_create_global_multi_region()` method body

```python
def _create_global_multi_region(self, stage_name: str, primary_region: str = "us-east-1") -> None:
    """
    Multi-Region Global Architecture.
    Deploy this stack in EACH region. IS_PRIMARY flag controls what gets created where.

    Components:
      A) AWS Global Accelerator (anycast IPs, TCP acceleration) — primary region only
      B) Route 53 health check failover routing — per region
      C) Aurora Global Database (<1s cross-region replication)
      D) DynamoDB Global Tables (active-active multi-master)
      E) S3 Cross-Region Replication (CRR) — primary publishes, secondary receives
      F) Regional health-check Lambda (probe Aurora + DynamoDB)
      G) Replication-lag alarm
    """

    from aws_cdk import (
        Duration, CfnOutput, Fn,
        aws_globalaccelerator as ga,
        aws_route53 as route53,
        aws_rds as rds,
        aws_dynamodb as ddb,
        aws_lambda as _lambda,
        aws_iam as iam,
        aws_cloudwatch as cw,
        aws_cloudwatch_actions as cw_actions,
    )

    IS_PRIMARY = self.region == primary_region

    # =========================================================================
    # A) AWS GLOBAL ACCELERATOR (create once in primary region)
    # =========================================================================

    if IS_PRIMARY:
        accelerator = ga.Accelerator(
            self, "GlobalAccelerator",
            accelerator_name=f"{{project_name}}-{stage_name}",
            enabled=True,
            ip_address_type=ga.IpAddressType.DUAL_STACK,
        )
        listener = accelerator.add_listener(
            "HTTPSListener",
            port_ranges=[ga.PortRange(from_port=443, to_port=443)],
            protocol=ga.ConnectionProtocol.TCP,
            client_affinity=ga.ClientAffinity.SOURCE_IP,
        )
        listener.add_endpoint_group(
            "PrimaryRegion",
            region=primary_region,
            traffic_dial_percentage=100,
            health_check_path="/health",
            health_check_protocol=ga.HealthCheckProtocol.HTTPS,
            health_check_interval_seconds=10,
            threshold_count=2,
        )
        listener.add_endpoint_group(
            "SecondaryRegion",
            region="eu-west-1",  # [Claude: replace with SOW secondary region]
            traffic_dial_percentage=0,   # 0% normally — switches on failover
            health_check_path="/health",
            health_check_protocol=ga.HealthCheckProtocol.HTTPS,
            health_check_interval_seconds=10,
            threshold_count=2,
        )
        CfnOutput(self, "GlobalAcceleratorArn",
            value=accelerator.accelerator_arn,
            description="Global Accelerator ARN",
            export_name=f"{{project_name}}-global-accel-{stage_name}",
        )

    # =========================================================================
    # B) ROUTE 53 HEALTH CHECK (per region)
    # =========================================================================

    health_check = route53.CfnHealthCheck(
        self, "RegionHealthCheck",
        health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
            type="HTTPS",
            fully_qualified_domain_name=f"{{project_name}}-health-{self.region}.{{project_name}}.com",
            port=443,
            resource_path="/health",
            request_interval=10,
            failure_threshold=2,
            enable_sni=True,
            insufficient_data_health_status="Unhealthy",
        ),
    )

    # =========================================================================
    # C) AURORA GLOBAL DATABASE
    # =========================================================================

    if IS_PRIMARY:
        aurora_global = rds.CfnGlobalCluster(
            self, "AuroraGlobalCluster",
            global_cluster_identifier=f"{{project_name}}-global-{stage_name}",
            engine="aurora-postgresql",
            engine_version="15.4",
            storage_encrypted=True,
            deletion_protection=stage_name == "prod",
        )
        CfnOutput(self, "AuroraGlobalClusterID",
            value=f"{{project_name}}-global-{stage_name}",
            description="Join secondary region Aurora clusters to this Global Cluster ID",
            export_name=f"{{project_name}}-aurora-global-{stage_name}",
        )
    else:
        # Secondary read replica — auto-promoted to primary on failover
        rds.CfnDBCluster(
            self, "AuroraSecondaryCluster",
            db_cluster_identifier=f"{{project_name}}-secondary-{stage_name}",
            engine="aurora-postgresql",
            engine_version="15.4",
            storage_encrypted=True,
            kms_key_id=self.kms_key.key_arn,
            global_cluster_identifier=Fn.import_value(f"{{project_name}}-aurora-global-{stage_name}"),
            vpc_security_group_ids=[self.aurora_sg.security_group_id],
            db_subnet_group_name=self.db_subnet_group.subnet_group_name,
            deletion_protection=stage_name == "prod",
        )

    # =========================================================================
    # D) DYNAMODB GLOBAL TABLES (active-active, multi-master)
    # =========================================================================

    ddb.CfnGlobalTable(
        self, "MainGlobalTable",
        table_name=f"{{project_name}}-main-{stage_name}",
        billing_mode="PAY_PER_REQUEST",
        stream_specification=ddb.CfnGlobalTable.StreamSpecificationProperty(
            stream_view_type="NEW_AND_OLD_IMAGES",  # Required for Global Tables
        ),
        attribute_definitions=[
            ddb.CfnGlobalTable.AttributeDefinitionProperty(attribute_name="pk", attribute_type="S"),
            ddb.CfnGlobalTable.AttributeDefinitionProperty(attribute_name="sk", attribute_type="S"),
        ],
        key_schema=[
            ddb.CfnGlobalTable.KeySchemaProperty(attribute_name="pk", key_type="HASH"),
            ddb.CfnGlobalTable.KeySchemaProperty(attribute_name="sk", key_type="RANGE"),
        ],
        replicas=[
            ddb.CfnGlobalTable.ReplicaSpecificationProperty(
                region="us-east-1",
                point_in_time_recovery_specification=ddb.CfnGlobalTable.PointInTimeRecoverySpecificationProperty(
                    point_in_time_recovery_enabled=True),
                sse_specification=ddb.CfnGlobalTable.ReplicaSSESpecificationProperty(
                    kms_master_key_id=self.kms_key.key_arn),
            ),
            ddb.CfnGlobalTable.ReplicaSpecificationProperty(
                region="eu-west-1",  # [Claude: use regions from Architecture Map]
                point_in_time_recovery_specification=ddb.CfnGlobalTable.PointInTimeRecoverySpecificationProperty(
                    point_in_time_recovery_enabled=True),
            ),
        ],
        sse_specification=ddb.CfnGlobalTable.SSESpecificationProperty(
            sse_enabled=True, sse_type="KMS"),
        time_to_live_specification=ddb.CfnGlobalTable.TimeToLiveSpecificationProperty(
            attribute_name="expires_at", enabled=True),
    )

    # =========================================================================
    # E) S3 CROSS-REGION REPLICATION
    # =========================================================================

    if IS_PRIMARY:
        replication_role = iam.Role(
            self, "S3ReplicationRole",
            assumed_by=iam.ServicePrincipal("s3.amazonaws.com"),
            role_name=f"{{project_name}}-s3-crr-{stage_name}",
        )
        # [Claude: add replication config to lake buckets from MLOPS_DATA_PLATFORM.md]
        # Source bucket must have versioning enabled (already done in lake bucket definitions)

    # =========================================================================
    # F) REGIONAL HEALTH CHECK LAMBDA
    # =========================================================================

    health_fn = _lambda.Function(
        self, "RegionHealthFn",
        function_name=f"{{project_name}}-health-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, logging
from datetime import datetime
logger = logging.getLogger()
rds = boto3.client('rds')
ddb = boto3.client('dynamodb')

def handler(event, context):
    checks = {}
    try:
        rds.describe_db_clusters(DBClusterIdentifier=os.environ['AURORA_CLUSTER_ID'])
        checks['aurora'] = 'healthy'
    except Exception as e:
        checks['aurora'] = 'unhealthy'
    try:
        ddb.describe_table(TableName=os.environ['DDB_TABLE_NAME'])
        checks['dynamodb'] = 'healthy'
    except Exception as e:
        checks['dynamodb'] = 'unhealthy'

    healthy = all(v == 'healthy' for v in checks.values())
    return {
        'statusCode': 200 if healthy else 503,
        'body': json.dumps({'status': 'healthy' if healthy else 'degraded',
                            'region': os.environ['AWS_DEFAULT_REGION'],
                            'timestamp': datetime.utcnow().isoformat(),
                            'checks': checks}),
        'headers': {'Content-Type': 'application/json'},
    }
"""),
        environment={
            "AURORA_CLUSTER_ID": f"{{project_name}}-{stage_name}",
            "DDB_TABLE_NAME":    f"{{project_name}}-main-{stage_name}",
        },
        timeout=Duration.seconds(10),
        tracing=_lambda.Tracing.ACTIVE,
    )

    # =========================================================================
    # G) REPLICATION LAG ALARM
    # =========================================================================

    cw.Alarm(
        self, "AuroraReplicationLagAlarm",
        alarm_name=f"{{project_name}}-replication-lag-{stage_name}",
        alarm_description="Aurora Global DB replication lag > 1s — failover readiness at risk",
        metric=cw.Metric(
            namespace="AWS/RDS",
            metric_name="AuroraGlobalDBReplicationLag",
            dimensions_map={"DBClusterIdentifier": f"{{project_name}}-{stage_name}"},
            period=Duration.minutes(1),
            statistic="Maximum",
        ),
        threshold=1000,  # 1000ms = 1 second
        evaluation_periods=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarm_actions=[cw_actions.SnsAction(self.alert_topic)],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "IsPrimaryRegion",
        value=str(IS_PRIMARY),
        description="Whether this is the primary write region",
    )
    CfnOutput(self, "HealthFnArn",
        value=health_fn.function_arn,
        description="Regional health Lambda — wire Route53 health check to this",
        export_name=f"{{project_name}}-health-fn-{self.region}-{stage_name}",
    )
```

### 3.3 Monolith gotchas

- **Global Accelerator `add_endpoint_group(region=...)`** accepts any region string, but the actual endpoint resources (ALB, NLB, EIP) must exist in that region before the listener can bind them. If you create the Accelerator *before* the secondary region's stack deploys, the endpoint group dial sits at 0% with no healthy targets — which is actually the intended failover-ready state. Don't mistake this for a broken deploy.
- **`CfnGlobalCluster` + secondary `CfnDBCluster`** must be deployed in order: primary region's `CfnGlobalCluster` first, secondary region's `CfnDBCluster` second. `Fn.import_value(...)` does not cross regions — the secondary stack must receive the global cluster identifier as a parameter, an SSM lookup, or a hard-coded string. The v1.0 code uses `Fn.import_value` which only works inside the same account+region; for a real multi-region deploy swap to `ssm.StringParameter.value_for_string_parameter(...)` reading a *primary-region* SSM key replicated via a manual `aws ssm put-parameter` in the secondary region.
- **`CfnGlobalTable` with KMS** — only the *first* replica can set `sse_specification`; subsequent replicas inherit region-local AWS-managed keys unless you explicitly pass a per-region CMK ARN. Cross-region CMK access is not automatic.
- **Route 53 health check FQDN** must resolve to a publicly-addressable endpoint. An internal ALB in a private subnet won't work — use Global Accelerator anycast IPs or a public ALB.
- **S3 CRR role** must have both `s3:GetReplicationConfiguration` on the source bucket and `s3:ReplicateObject/Delete/Tags` on the destination. V1.0 leaves the policy as a `[Claude: add ...]` marker — fill it in before deploy.

---

## 4. Micro-Stack Variant

**Use when:** the Aurora cluster lives in `DatabaseStack`, S3 buckets in `StorageStack`, DynamoDB tables in `JobLedgerStack`, KMS keys in `SecurityStack`, VPC + DB subnet group in `NetworkingStack` — and this stack only wires the **global topology** across them.

### 4.1 The five non-negotiables

Memorize these (reference: `LAYER_BACKEND_LAMBDA` §4.1). Every cross-stack multi-region failure reduces to one of them.

1. **Anchor asset paths to `__file__`, never relative-to-CWD.** The regional health Lambda's code asset uses `Path(__file__).resolve().parents[3] / "lambda" / "region_health"`.
2. **Never use `X.grant_*(role)` on a cross-stack resource X.** Use identity-side `PolicyStatement` on the health Lambda's execution role, scoping to the Aurora cluster ARN and DynamoDB table ARN strings read via SSM.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** Not relevant here, but if alarms route through a cross-stack SNS topic, use `sns.Topic.from_topic_arn(self, "T", ssm_lookup)`.
4. **Never own a bucket in one stack and attach its CloudFront OAC in another.** Keep global CDN + origin buckets together.
5. **Never set `encryption_key=ext_key` where `ext_key` came from another stack.** The secondary-region Aurora `CfnDBCluster.kms_key_id` uses an ARN **string** read from SSM (5th non-negotiable). Don't pass a `kms.IKey` object resolved from another stack.

Also: `permission_boundary` applied to every role in this stack.

### 4.2 `GlobalStack` (primary region only) + `RegionalGlobalStack` (per region)

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput, Fn,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_route53 as route53,
    aws_globalaccelerator as ga,
    aws_rds as rds,
    aws_dynamodb as ddb,
    aws_ssm as ssm,
    aws_sns as sns,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
)
from constructs import Construct

# stacks/global_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class GlobalStack(cdk.Stack):
    """Deployed in the PRIMARY region only. Owns the Global Accelerator,
    the Aurora Global Cluster envelope, and the DynamoDB Global Table.
    Publishes ARNs via SSM for regional stacks to consume.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        primary_region: str,
        secondary_regions: list[str],
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(
            scope, f"{{project_name}}-global-{stage_name}",
            env=cdk.Environment(region=primary_region),
            **kwargs,
        )

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk", "Layer": "Global"}.items():
            cdk.Tags.of(self).add(k, v)

        # -----------------------------------------------------------------
        # A) Global Accelerator (primary only)
        # -----------------------------------------------------------------
        accelerator = ga.Accelerator(
            self, "GlobalAccelerator",
            accelerator_name=f"{{project_name}}-{stage_name}",
            enabled=True,
            ip_address_type=ga.IpAddressType.DUAL_STACK,
        )
        listener = accelerator.add_listener(
            "HTTPSListener",
            port_ranges=[ga.PortRange(from_port=443, to_port=443)],
            protocol=ga.ConnectionProtocol.TCP,
            client_affinity=ga.ClientAffinity.SOURCE_IP,
        )
        listener.add_endpoint_group(
            "PrimaryRegion",
            region=primary_region,
            traffic_dial_percentage=100,
            health_check_path="/health",
            health_check_protocol=ga.HealthCheckProtocol.HTTPS,
            health_check_interval_seconds=10,
            threshold_count=2,
        )
        for idx, sec in enumerate(secondary_regions):
            listener.add_endpoint_group(
                f"SecondaryRegion{idx}",
                region=sec,
                traffic_dial_percentage=0,
                health_check_path="/health",
                health_check_protocol=ga.HealthCheckProtocol.HTTPS,
                health_check_interval_seconds=10,
                threshold_count=2,
            )

        # -----------------------------------------------------------------
        # B) Aurora Global Cluster envelope (primary defines, secondary joins)
        # -----------------------------------------------------------------
        aurora_global = rds.CfnGlobalCluster(
            self, "AuroraGlobalCluster",
            global_cluster_identifier=f"{{project_name}}-global-{stage_name}",
            engine="aurora-postgresql",
            engine_version="15.4",
            storage_encrypted=True,
            deletion_protection=stage_name == "prod",
        )

        # -----------------------------------------------------------------
        # C) DynamoDB Global Table — defines ALL replicas at once
        # -----------------------------------------------------------------
        # Each replica's SSE KMS ARN must be read via SSM from that region's
        # SecurityStack BEFORE this stack deploys. For the primary we can
        # read from SSM in-region; for secondaries we pass ARNs in as a
        # constructor arg (populated by the caller from deploy-time lookups).
        primary_kms_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage_name}/security/cmk_arn",
        )

        replicas = [
            ddb.CfnGlobalTable.ReplicaSpecificationProperty(
                region=primary_region,
                point_in_time_recovery_specification=ddb.CfnGlobalTable.PointInTimeRecoverySpecificationProperty(
                    point_in_time_recovery_enabled=True),
                sse_specification=ddb.CfnGlobalTable.ReplicaSSESpecificationProperty(
                    kms_master_key_id=primary_kms_arn),
            ),
        ]
        for sec in secondary_regions:
            replicas.append(ddb.CfnGlobalTable.ReplicaSpecificationProperty(
                region=sec,
                point_in_time_recovery_specification=ddb.CfnGlobalTable.PointInTimeRecoverySpecificationProperty(
                    point_in_time_recovery_enabled=True),
                # Secondary KMS is region-local AWS-managed unless populated
                # out-of-band via SSM in the secondary region.
            ))

        global_table = ddb.CfnGlobalTable(
            self, "MainGlobalTable",
            table_name=f"{{project_name}}-main-{stage_name}",
            billing_mode="PAY_PER_REQUEST",
            stream_specification=ddb.CfnGlobalTable.StreamSpecificationProperty(
                stream_view_type="NEW_AND_OLD_IMAGES"),
            attribute_definitions=[
                ddb.CfnGlobalTable.AttributeDefinitionProperty(attribute_name="pk", attribute_type="S"),
                ddb.CfnGlobalTable.AttributeDefinitionProperty(attribute_name="sk", attribute_type="S"),
            ],
            key_schema=[
                ddb.CfnGlobalTable.KeySchemaProperty(attribute_name="pk", key_type="HASH"),
                ddb.CfnGlobalTable.KeySchemaProperty(attribute_name="sk", key_type="RANGE"),
            ],
            replicas=replicas,
            sse_specification=ddb.CfnGlobalTable.SSESpecificationProperty(
                sse_enabled=True, sse_type="KMS"),
            time_to_live_specification=ddb.CfnGlobalTable.TimeToLiveSpecificationProperty(
                attribute_name="expires_at", enabled=True),
        )

        # -----------------------------------------------------------------
        # Publish via SSM — consumer stacks read these
        # -----------------------------------------------------------------
        ssm.StringParameter(self, "GlobalAccelArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/global/accel_arn",
            string_value=accelerator.accelerator_arn,
        )
        ssm.StringParameter(self, "AuroraGlobalIdParam",
            parameter_name=f"/{{project_name}}/{stage_name}/global/aurora_global_id",
            string_value=f"{{project_name}}-global-{stage_name}",
        )
        ssm.StringParameter(self, "DdbGlobalTableNameParam",
            parameter_name=f"/{{project_name}}/{stage_name}/global/ddb_table_name",
            string_value=f"{{project_name}}-main-{stage_name}",
        )

        CfnOutput(self, "GlobalAcceleratorArn", value=accelerator.accelerator_arn)
        CfnOutput(self, "AuroraGlobalClusterID",
            value=f"{{project_name}}-global-{stage_name}")


class RegionalGlobalStack(cdk.Stack):
    """Deployed in EACH region (primary + secondaries). Owns the regional
    health Lambda, Route 53 health check, Aurora secondary cluster (for
    secondary regions), S3 CRR role (primary only), and replication alarms.

    All upstream resources come in via SSM ARN strings — never as L2
    objects from another stack.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        is_primary: bool,
        alert_topic_arn_ssm: str,
        vpc_id_ssm: str,
        aurora_sg_id_ssm: str,
        db_subnet_group_name_ssm: str,
        regional_kms_arn_ssm: str,
        aurora_global_id_ssm: str,
        ddb_table_name_ssm: str,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-regional-global-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk", "Layer": "RegionalGlobal"}.items():
            cdk.Tags.of(self).add(k, v)

        # All cross-stack reads are SSM strings
        vpc_id = ssm.StringParameter.value_for_string_parameter(self, vpc_id_ssm)
        aurora_sg_id = ssm.StringParameter.value_for_string_parameter(self, aurora_sg_id_ssm)
        db_subnet_group_name = ssm.StringParameter.value_for_string_parameter(self, db_subnet_group_name_ssm)
        regional_kms_arn = ssm.StringParameter.value_for_string_parameter(self, regional_kms_arn_ssm)
        aurora_global_id = ssm.StringParameter.value_for_string_parameter(self, aurora_global_id_ssm)
        ddb_table_name = ssm.StringParameter.value_for_string_parameter(self, ddb_table_name_ssm)
        alert_topic = sns.Topic.from_topic_arn(self, "AlertTopic",
            ssm.StringParameter.value_for_string_parameter(self, alert_topic_arn_ssm),
        )

        # Re-hydrate VPC by token (lookup-free; offline synth safe when token passed)
        vpc = ec2.Vpc.from_vpc_attributes(self, "ImportedVpc",
            vpc_id=vpc_id,
            availability_zones=cdk.Fn.get_azs(),
        )

        # -----------------------------------------------------------------
        # A) Route 53 health check (per region)
        # -----------------------------------------------------------------
        health_check = route53.CfnHealthCheck(
            self, "RegionHealthCheck",
            health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                type="HTTPS",
                fully_qualified_domain_name=f"{{project_name}}-health-{self.region}.{{project_name}}.com",
                port=443,
                resource_path="/health",
                request_interval=10,
                failure_threshold=2,
                enable_sni=True,
                insufficient_data_health_status="Unhealthy",
            ),
        )

        # -----------------------------------------------------------------
        # B) Aurora secondary cluster (secondary regions only)
        # -----------------------------------------------------------------
        if not is_primary:
            rds.CfnDBCluster(
                self, "AuroraSecondaryCluster",
                db_cluster_identifier=f"{{project_name}}-secondary-{stage_name}",
                engine="aurora-postgresql",
                engine_version="15.4",
                storage_encrypted=True,
                kms_key_id=regional_kms_arn,            # 5th non-negotiable: ARN string, not IKey
                global_cluster_identifier=aurora_global_id,
                vpc_security_group_ids=[aurora_sg_id],
                db_subnet_group_name=db_subnet_group_name,
                deletion_protection=stage_name == "prod",
            )

        # -----------------------------------------------------------------
        # C) S3 CRR role (primary only)
        # -----------------------------------------------------------------
        if is_primary:
            replication_role = iam.Role(
                self, "S3ReplicationRole",
                assumed_by=iam.ServicePrincipal("s3.amazonaws.com"),
                role_name=f"{{project_name}}-s3-crr-{stage_name}",
            )
            iam.PermissionsBoundary.of(replication_role).apply(permission_boundary)
            replication_role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "s3:GetReplicationConfiguration",
                    "s3:ListBucket",
                    "s3:GetObjectVersionForReplication",
                    "s3:GetObjectVersionAcl",
                    "s3:GetObjectVersionTagging",
                ],
                resources=["*"],  # Scope via source-bucket inventory at consume site
            ))
            replication_role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "s3:ReplicateObject",
                    "s3:ReplicateDelete",
                    "s3:ReplicateTags",
                ],
                resources=["*"],  # Replica bucket ARNs scope via consume-site rule
            ))

            ssm.StringParameter(self, "CrrRoleArnParam",
                parameter_name=f"/{{project_name}}/{stage_name}/global/crr_role_arn",
                string_value=replication_role.role_arn,
            )

        # -----------------------------------------------------------------
        # D) Regional health Lambda (anchored asset, identity-side grants)
        # -----------------------------------------------------------------
        log_group = logs.LogGroup(
            self, "RegionHealthLogs",
            log_group_name=f"/aws/lambda/{{project_name}}-health-{stage_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        health_fn = _lambda.Function(
            self, "RegionHealthFn",
            function_name=f"{{project_name}}-health-{stage_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(str(_LAMBDAS_ROOT / "region_health")),
            environment={
                "AURORA_CLUSTER_ID": f"{{project_name}}-{stage_name}",
                "DDB_TABLE_NAME": ddb_table_name,
            },
            timeout=Duration.seconds(10),
            tracing=_lambda.Tracing.ACTIVE,
            log_group=log_group,
        )
        iam.PermissionsBoundary.of(health_fn.role).apply(permission_boundary)

        # Identity-side grants (no cross-stack grant_*; all ARN strings)
        aurora_cluster_arn = (
            f"arn:aws:rds:{self.region}:{Aws.ACCOUNT_ID}:cluster:"
            f"{{project_name}}-{stage_name}"
        )
        ddb_table_arn = (
            f"arn:aws:dynamodb:{self.region}:{Aws.ACCOUNT_ID}:table/" + ddb_table_name
        )
        health_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["rds:DescribeDBClusters"],
            resources=[aurora_cluster_arn],
        ))
        health_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:DescribeTable"],
            resources=[ddb_table_arn],
        ))

        # -----------------------------------------------------------------
        # E) Replication-lag alarm
        # -----------------------------------------------------------------
        cw.Alarm(
            self, "AuroraReplicationLagAlarm",
            alarm_name=f"{{project_name}}-replication-lag-{stage_name}",
            alarm_description="Aurora Global DB replication lag > 1s — failover readiness at risk",
            metric=cw.Metric(
                namespace="AWS/RDS",
                metric_name="AuroraGlobalDBReplicationLag",
                dimensions_map={"DBClusterIdentifier": f"{{project_name}}-{stage_name}"},
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            threshold=1000,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_actions=[cw_actions.SnsAction(alert_topic)],
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        # -----------------------------------------------------------------
        # Publish regional outputs
        # -----------------------------------------------------------------
        ssm.StringParameter(self, "RegionHealthFnArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/global/health_fn_arn_{self.region}",
            string_value=health_fn.function_arn,
        )
        ssm.StringParameter(self, "RegionHealthCheckIdParam",
            parameter_name=f"/{{project_name}}/{stage_name}/global/health_check_id_{self.region}",
            string_value=health_check.attr_health_check_id,
        )

        CfnOutput(self, "IsPrimaryRegion", value=str(is_primary))
        CfnOutput(self, "HealthFnArn", value=health_fn.function_arn)
```

### 4.3 Regional health Lambda handler

```python
# lambda/region_health/index.py
import json, os, logging
from datetime import datetime

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rds = boto3.client("rds")
ddb = boto3.client("dynamodb")


def handler(event, context):
    checks = {}
    try:
        rds.describe_db_clusters(DBClusterIdentifier=os.environ["AURORA_CLUSTER_ID"])
        checks["aurora"] = "healthy"
    except Exception as e:
        logger.warning("aurora check failed: %s", e)
        checks["aurora"] = "unhealthy"

    try:
        ddb.describe_table(TableName=os.environ["DDB_TABLE_NAME"])
        checks["dynamodb"] = "healthy"
    except Exception as e:
        logger.warning("dynamodb check failed: %s", e)
        checks["dynamodb"] = "unhealthy"

    healthy = all(v == "healthy" for v in checks.values())
    return {
        "statusCode": 200 if healthy else 503,
        "body": json.dumps({
            "status": "healthy" if healthy else "degraded",
            "region": os.environ.get("AWS_REGION", "unknown"),
            "timestamp": datetime.utcnow().isoformat(),
            "checks": checks,
        }),
        "headers": {"Content-Type": "application/json"},
    }
```

### 4.4 Micro-stack gotchas

- **Cross-region SSM lookups.** `ssm.StringParameter.value_for_string_parameter` reads from the **stack's own region**. For primary-region-only values consumed by a secondary-region stack, you must replicate the SSM parameter out-of-band (CLI / pipeline) or use an `AwsCustomResource` to call `ssm:GetParameter` against the primary region at deploy time — `# TODO(verify): AwsCustomResource cross-region IAM policy + ordering guarantees`.
- **`CfnGlobalTable` is a single resource that creates all replicas**, not one resource per region. It must be deployed in the primary region only. Do not put it inside `RegionalGlobalStack` — it goes in `GlobalStack`.
- **`rds.CfnDBCluster` with `global_cluster_identifier`** needs the global cluster to exist before secondary deploy. The v1.0 `Fn.import_value` pattern does not cross regions; use SSM string or constructor arg.
- **`ec2.Vpc.from_vpc_attributes(..., availability_zones=cdk.Fn.get_azs())`** produces tokens that CFN resolves at deploy time — fine for subnet-group reads but will fail if any construct requires concrete AZ strings at synth time. `# TODO(verify): Vpc.from_vpc_attributes token resolution for subnet_group downstream use`.
- **Global Accelerator endpoint group region** can be any region, but the actual ALB/NLB ARN targets must be added via a separate `add_endpoint(...)` call per region — the listener is created primary-side but endpoint *references* live in the global stack. The v2.0 pattern leaves endpoint targeting to a follow-up deploy step.
- **Route 53 health check `attr_health_check_id`** is stable across redeploys only if the FQDN and port remain unchanged. Mutating the FQDN forces resource replacement, invalidating the ID in downstream DNS failover records.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| Single-region with read replica only (no failover) | Drop §4 entirely; keep just the Aurora reader in `DatabaseStack` — see `LAYER_DATA` §3 |
| Pilot Light DR (secondary off by default) | Keep Aurora Global Cluster + DynamoDB Global Table, set Global Accelerator secondary `traffic_dial_percentage=0`, scale secondary compute to `desired_count=0` |
| Active-active writes (multi-master) | DynamoDB Global Tables already support this; Aurora does **not** — use DynamoDB-only or switch to Aurora DSQL (preview) — `# TODO(verify): aurora DSQL CfnCluster surface` |
| Data residency (EU data stays in EU) | Drop cross-region DynamoDB replicas; use `CfnTable` (regional) in each region, sync via AppSync or EventBridge cross-region bus |
| Primary-region outage > 30 min | Operator-driven: flip Global Accelerator traffic dial to 100% on secondary, promote Aurora secondary via `rds:FailoverGlobalCluster`, update Route 53 weighted records |
| Add third region | Append to `secondary_regions` list; a new `RegionalGlobalStack` instance in that region; update DynamoDB Global Table `replicas[]` and redeploy `GlobalStack` |
| Replace Global Accelerator with CloudFront | Drop §3.2-A / §4.2-A; put CloudFront multi-origin with origin-group failover in `LAYER_FRONTEND` |

---

## 6. Worked example

Save as `tests/sop/test_GLOBAL_MULTI_REGION.py`. Offline — no AWS credentials needed.

```python
"""SOP verification — GlobalStack + RegionalGlobalStack synthesize without cycles."""
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_sns as sns,
    aws_ssm as ssm,
)
from aws_cdk.assertions import Template, Match


def _env(region="us-east-1"):
    return cdk.Environment(account="000000000000", region=region)


def test_global_stack_synthesizes():
    app = cdk.App()

    # Minimal deps in primary region
    deps = cdk.Stack(app, "Deps", env=_env())
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])
    ssm.StringParameter(deps, "CmkArn",
        parameter_name="/{project_name}/prod/security/cmk_arn",
        string_value="arn:aws:kms:us-east-1:000000000000:key/abcd-1234",
    )

    from infrastructure.cdk.stacks.global_stack import GlobalStack
    g = GlobalStack(
        app,
        stage_name="prod",
        primary_region="us-east-1",
        secondary_regions=["eu-west-1"],
        permission_boundary=boundary,
    )
    t = Template.from_stack(g)
    t.resource_count_is("AWS::GlobalAccelerator::Accelerator", 1)
    t.resource_count_is("AWS::RDS::GlobalCluster", 1)
    t.resource_count_is("AWS::DynamoDB::GlobalTable", 1)
    t.resource_count_is("AWS::SSM::Parameter", 3)
    t.has_resource_properties("AWS::DynamoDB::GlobalTable", {
        "Replicas": Match.array_with([
            Match.object_like({"Region": "us-east-1"}),
            Match.object_like({"Region": "eu-west-1"}),
        ]),
    })


def test_regional_stack_primary_synthesizes():
    app = cdk.App()

    deps = cdk.Stack(app, "Deps", env=_env())
    topic = sns.Topic(deps, "AlertTopic")
    for pname, pval in [
        ("/{project_name}/prod/obs/alert_topic_arn", topic.topic_arn),
        ("/{project_name}/prod/networking/vpc_id", "vpc-012"),
        ("/{project_name}/prod/data/aurora_sg_id", "sg-012"),
        ("/{project_name}/prod/data/db_subnet_group_name", "sng-012"),
        ("/{project_name}/prod/security/cmk_arn", "arn:aws:kms:us-east-1:000000000000:key/a"),
        ("/{project_name}/prod/global/aurora_global_id", "{project_name}-global-prod"),
        ("/{project_name}/prod/global/ddb_table_name", "{project_name}-main-prod"),
    ]:
        ssm.StringParameter(deps, pname.replace("/", "_"),
            parameter_name=pname, string_value=pval)

    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.regional_global_stack import RegionalGlobalStack
    r = RegionalGlobalStack(
        app, stage_name="prod", is_primary=True,
        alert_topic_arn_ssm="/{project_name}/prod/obs/alert_topic_arn",
        vpc_id_ssm="/{project_name}/prod/networking/vpc_id",
        aurora_sg_id_ssm="/{project_name}/prod/data/aurora_sg_id",
        db_subnet_group_name_ssm="/{project_name}/prod/data/db_subnet_group_name",
        regional_kms_arn_ssm="/{project_name}/prod/security/cmk_arn",
        aurora_global_id_ssm="/{project_name}/prod/global/aurora_global_id",
        ddb_table_name_ssm="/{project_name}/prod/global/ddb_table_name",
        permission_boundary=boundary,
        env=_env(),
    )
    t = Template.from_stack(r)
    t.resource_count_is("AWS::Route53::HealthCheck", 1)
    t.resource_count_is("AWS::IAM::Role", 2)  # CRR role + health fn role
    t.resource_count_is("AWS::Lambda::Function", 1)
    t.resource_count_is("AWS::CloudWatch::Alarm", 1)
    # Primary: no secondary Aurora cluster
    t.resource_count_is("AWS::RDS::DBCluster", 0)
```

---

## 7. References

- `docs/template_params.md` — `GLOBAL_ACCEL_ARN_SSM`, `AURORA_GLOBAL_CLUSTER_ID_SSM`, `DDB_GLOBAL_TABLE_NAME_SSM`, `REGION_HEALTH_FN_ARN_SSM`, `CRR_ROLE_ARN_SSM`, `PRIMARY_REGION`, `SECONDARY_REGIONS`, `STAGE_NAME`
- `docs/Feature_Roadmap.md` — feature IDs `DR-01..DR-12` (multi-region), `DR-13..DR-18` (failover orchestration)
- Global Accelerator CFN: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/AWS_GlobalAccelerator.html
- Aurora Global Database: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database.html
- DynamoDB Global Tables v2: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/V2globaltables_reqs_bestpractices.html
- S3 Cross-Region Replication: https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html
- Route 53 health-check failover: https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/dns-failover-configuring.html
- Related SOPs: `LAYER_NETWORKING` (VPC per region), `LAYER_DATA` (regional Aurora + DynamoDB), `LAYER_SECURITY` (KMS per region), `LAYER_FRONTEND` (CloudFront alternative), `OPS_ADVANCED_MONITORING` (SNS topics + alarms)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) split into `GlobalStack` (primary-only, owns Accelerator + Aurora Global Cluster + DynamoDB Global Table) and `RegionalGlobalStack` (per-region, owns Route 53 health check + Aurora secondary + S3 CRR role + health Lambda + replication alarm). Codified five non-negotiables: anchored asset `_LAMBDAS_ROOT`, identity-side IAM on health Lambda, SSM-resolved cross-stack refs, KMS key ARN as string (5th non-negotiable — secondary Aurora `kms_key_id` is ARN string not IKey), permission boundary on every role. Added `TODO(verify)` markers on cross-region SSM lookups, `Vpc.from_vpc_attributes` token resolution, Aurora DSQL CfnCluster surface. Added Swap matrix (§5), Worked example (§6), Monolith gotchas (§3.3), Micro-stack gotchas (§4.4). Preserved all v1.0 content: strategy-selection table, Global Accelerator config with dial-based failover, Route 53 HTTPS health check, Aurora Global Cluster + secondary, DynamoDB Global Table with KMS + PITR + TTL + NEW_AND_OLD_IMAGES stream, S3 CRR role placeholder, inline health Lambda, Aurora replication-lag alarm. |
| 1.0 | 2026-03-05 | Initial — single-stack multi-region pattern: Global Accelerator, Route 53 health check, Aurora Global Database, DynamoDB Global Tables, S3 CRR, regional health Lambda, replication-lag alarm. |
