# PARTIAL: Multi-Region / Global Architecture — Active-Active, Failover, DR

**Usage:** Include when SOW mentions multi-region, disaster recovery, RTO/RPO < 1 hour, global users, active-active, or data residency.

---

## Strategy Selection

| Strategy             | RTO     | RPO     | Use When                       |
| -------------------- | ------- | ------- | ------------------------------ |
| **Backup & Restore** | Hours   | Hours   | Non-critical                   |
| **Warm Standby**     | Minutes | Seconds | Important services             |
| **Active-Active**    | Seconds | ~0      | Mission-critical, global users |

---

## Architecture

```
Route53 Health Check Failover / Latency Routing
       │                              │
  us-east-1 (primary)           eu-west-1 (secondary)
  ┌────────────────────┐        ┌────────────────────┐
  │ Global Accelerator │        │ Global Accelerator │
  │ API Gateway        │        │ API Gateway        │
  │ Lambda / ECS       │        │ Lambda / ECS       │
  │ Aurora Global DB ◄──<1s lag──► Aurora Read       │
  │ DynamoDB Global ◄──active-active──► DynamoDB     │
  │ S3 + CRR ──────────────────►  S3 Replica         │
  └────────────────────┘        └────────────────────┘
```

---

## CDK Code Block — Multi-Region Stack

```python
def _create_global_multi_region(self, stage_name: str, primary_region: str = "us-east-1") -> None:
    """
    Multi-Region Global Architecture.
    Deploy this stack in EACH region. IS_PRIMARY flag controls what gets created where.

    Components:
      A) AWS Global Accelerator (anycast IPs, TCP acceleration)
      B) Route53 health check failover routing
      C) Aurora Global Database (<1s cross-region replication)
      D) DynamoDB Global Tables (active-active multi-master)
      E) S3 Cross-Region Replication (CRR)
      F) Regional health check Lambda
    """

    import aws_cdk.aws_globalaccelerator as ga
    import aws_cdk.aws_route53 as route53

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
    # B) ROUTE53 HEALTH CHECK
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
        runtime=_lambda.Runtime.PYTHON_3_12,
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
        checks['aurora'] = f'unhealthy'
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
    # REPLICATION LAG ALARM
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
