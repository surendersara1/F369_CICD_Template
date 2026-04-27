# SOP — Centralized Logging (CloudTrail Lake · AWS Security Lake · OCSF · Log Archive account · cross-region replication)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · CloudTrail org trail (multi-account, multi-region) · CloudTrail Lake (managed event store + SQL queries) · AWS Security Lake (OCSF normalized to S3+Iceberg) · Log Archive account (Control Tower-managed) · Object Lock + Vault Lock for tamper-proof · Cross-region replication for DR

---

## 1. Purpose

- Codify the **central logging hub** — every account, every region, all CloudTrail events + VPC Flow Logs + Route 53 query logs + Security Hub findings → Log Archive account, immutable.
- Codify **CloudTrail Lake** — managed event store with SQL queries; 7-year retention; replaces hand-rolled Athena-on-CloudTrail-S3.
- Codify **AWS Security Lake (GA Apr 2024)** — normalizes findings from GuardDuty, Security Hub, AppFabric, plus customer logs to OCSF schema in S3+Iceberg, queryable from Athena/Snowflake/Splunk.
- Codify **Log Archive account hardening** — S3 Object Lock + bucket policy denying delete except by emergency-break-glass + cross-region replication.
- Codify the **canonical log source list**: CloudTrail mgmt + data events, VPC Flow Logs, Route 53 query logs, ELB access logs, S3 server access logs, WAF logs, Network Firewall alerts, Security Hub findings.
- This is the **observability of last resort** — when something bad happened, this is what you query.
- Built on `ENTERPRISE_CONTROL_TOWER` (Log Archive account exists). Pairs with `ENTERPRISE_SECURITY_HUB_GD_ORG`.

When the SOW signals: "centralized logs", "audit trail for SOC 2", "long-term log retention", "Security Lake", "tamper-proof logs", "PCI-DSS logging requirement".

---

## 2. Decision tree — log destination

| Log type | Volume | Query frequency | Best destination |
|---|---|---|---|
| CloudTrail mgmt | low | medium | CloudTrail Lake (managed, 7y free tier) |
| CloudTrail data (S3, Lambda) | high | low | S3 Log Archive (Athena on demand) |
| VPC Flow | huge | rare | S3 + Athena (CloudTrail Lake costs prohibitive) |
| Route 53 query | medium | rare | S3 + Athena |
| Security Hub findings | medium | high | Security Lake (normalized OCSF) + auto-export to SIEM |
| ELB access | low | medium | S3 + Athena |
| Application logs | varies | high | CloudWatch Logs → Firehose → S3 (per-app) |

```
Architecture:

 Workload Account 1                       Log Archive Account
 ┌────────────────────┐                   ┌──────────────────────────────────┐
 │ CloudTrail Org Trail│                   │ S3 bucket (Object Lock COMPLIANCE)│
 │ VPC Flow Logs       ├──────────────────►│   /CloudTrail/...                  │
 │ R53 Query Logs      │                   │   /VpcFlow/...                      │
 │ ELB Access Logs     │                   │   /R53/...                          │
 └────────────────────┘                   │   /ELB/...                          │
                                          │ + Cross-region replication (DR)     │
 Workload Account 2                       │ + Vault Lock prevents bucket policy │
 ┌────────────────────┐                   │   modification                       │
 │ ...same...         ├──────────────────►│                                      │
 └────────────────────┘                   └──────────────────┬───────────────────┘
                                                             │
                                                             ▼
                                          ┌──────────────────────────────────┐
                                          │ Audit Account                    │
                                          │  - CloudTrail Lake event ds       │
                                          │  - Security Lake (OCSF Iceberg)   │
                                          │  - Athena workgroup               │
                                          │  - QuickSight dashboards          │
                                          └──────────────────────────────────┘
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Org trail to Log Archive S3 + 1 Athena workgroup | **§3 Monolith** |
| Production — full stack: CT Lake + Security Lake + Object Lock + cross-region | **§5 Production** |

---

## 3. Monolith Variant — Org trail + S3 Log Archive

### 3.1 CDK in Log Archive account

```python
# stacks/log_archive_stack.py
from aws_cdk import Stack, Duration, RemovalPolicy
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from constructs import Construct


class LogArchiveStack(Stack):
    """Runs in Log Archive account."""

    def __init__(self, scope: Construct, id: str, *, org_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. KMS key for log encryption (multi-region) ──────────────
        log_key = kms.Key(self, "LogKey",
            description="Encryption for org log archive",
            enable_key_rotation=True,
            multi_region=True,                             # DR-friendly
            removal_policy=RemovalPolicy.RETAIN,
            policy=iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    sid="AllowCloudTrail",
                    principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                    actions=["kms:GenerateDataKey*", "kms:DescribeKey"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {"aws:SourceArn":
                            f"arn:aws:cloudtrail:*:{self.account}:trail/*"},
                    },
                ),
                iam.PolicyStatement(
                    sid="AllowDecryptByOrg",
                    principals=[iam.AnyPrincipal()],
                    actions=["kms:Decrypt", "kms:DescribeKey"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {"aws:PrincipalOrgID": org_id},
                    },
                ),
            ]),
        )

        # ── 2. S3 bucket for org logs — Object Lock COMPLIANCE ────────
        log_bucket = s3.Bucket(self, "LogArchiveBucket",
            bucket_name=f"org-log-archive-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=log_key,
            object_lock_enabled=True,                      # MUST set at create time
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(
                id="StandardIaThenGlacier",
                transitions=[
                    s3.Transition(storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                                  transition_after=Duration.days(90)),
                    s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                                  transition_after=Duration.days(180)),
                    s3.Transition(storage_class=s3.StorageClass.DEEP_ARCHIVE,
                                  transition_after=Duration.days(365)),
                ],
                noncurrent_version_expiration=Duration.days(2557),  # 7y
            )],
        )

        # Apply Object Lock default retention
        cfn_bucket = log_bucket.node.default_child
        cfn_bucket.add_property_override("ObjectLockConfiguration", {
            "ObjectLockEnabled": "Enabled",
            "Rule": {
                "DefaultRetention": {
                    "Mode": "COMPLIANCE",                  # immutable, no override even by root
                    "Days": 2557,                          # 7 years
                },
            },
        })

        # Bucket policy: deny delete + require TLS + restrict to org
        log_bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="DenyDelete",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:DeleteObject", "s3:DeleteObjectVersion",
                     "s3:DeleteBucket", "s3:PutBucketPolicy",
                     "s3:DeleteBucketPolicy"],
            resources=[log_bucket.bucket_arn, log_bucket.arn_for_objects("*")],
        ))
        log_bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="DenyInsecureTransport",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:*"],
            resources=[log_bucket.bucket_arn, log_bucket.arn_for_objects("*")],
            conditions={"Bool": {"aws:SecureTransport": "false"}},
        ))
        log_bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowOrgWrite",
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com"),
                        iam.ServicePrincipal("delivery.logs.amazonaws.com")],
            actions=["s3:PutObject", "s3:GetBucketAcl"],
            resources=[log_bucket.arn_for_objects("AWSLogs/*"),
                       log_bucket.bucket_arn],
            conditions={
                "StringEquals": {
                    "aws:SourceOrgID": org_id,
                    "s3:x-amz-acl": "bucket-owner-full-control",
                },
            },
        ))

        # ── 3. Cross-region replication (DR) ─────────────────────────
        replica_bucket = s3.Bucket(self, "LogArchiveReplica",
            bucket_name=f"org-log-archive-replica-{self.account}-us-west-2",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=log_key,                        # multi-region key handles
            object_lock_enabled=True,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )
        replication_role = iam.Role(self, "ReplicationRole",
            assumed_by=iam.ServicePrincipal("s3.amazonaws.com"),
        )
        log_bucket.add_replication_policy([
            s3.ReplicationRule(
                destination=s3.ReplicationDestination(
                    bucket=replica_bucket,
                    replication_time=Duration.minutes(15),
                    metrics=Duration.minutes(15),
                    storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                ),
                priority=1,
                delete_marker_replication=False,
            ),
        ], role=replication_role)

        self.log_bucket = log_bucket
        self.log_key = log_key
```

### 3.2 Org trail (CDK in Management account)

```python
# stacks/org_trail_stack.py — Management account
from aws_cdk import aws_cloudtrail as ct

org_trail = ct.Trail(self, "OrgTrail",
    trail_name="org-trail",
    bucket=log_bucket,                                   # cross-account ref
    encryption_key=log_key,
    is_multi_region_trail=True,
    is_organization_trail=True,                          # KEY
    include_global_service_events=True,
    enable_file_validation=True,                          # log integrity hash
    send_to_cloud_watch_logs=False,                       # only CloudTrail Lake/S3
    management_events=ct.ReadWriteType.ALL,
    insight_types=[
        ct.InsightType.API_CALL_RATE,
        ct.InsightType.API_ERROR_RATE,
    ],
)

# Log S3 data events (high-volume — only enable if you need it)
org_trail.add_s3_event_selector(
    s3_selector=[
        ct.S3EventSelector(bucket=sensitive_bucket, object_prefix="confidential/"),
    ],
    read_write_type=ct.ReadWriteType.ALL,
    include_management_events=False,
)
```

---

## 4. CloudTrail Lake (managed event data store)

```python
from aws_cdk import aws_cloudtrail as ct

# CloudTrail Lake event data store — runs in Audit account typically
event_data_store = ct.CfnEventDataStore(self, "OrgEventStore",
    name="org-management-events",
    multi_region_enabled=True,
    organization_enabled=True,                            # all org accounts
    retention_period=2557,                                # 7y max
    advanced_event_selectors=[
        {
            "Name": "Mgmt events",
            "FieldSelectors": [
                {"Field": "eventCategory", "Equals": ["Management"]},
            ],
        },
    ],
    termination_protection_enabled=True,
    kms_key_id=log_key.key_arn,
)

# Then query via SQL:
# SELECT eventTime, userIdentity.arn, eventName, awsRegion
# FROM <event-data-store-id>
# WHERE eventName = 'AssumeRole'
#   AND userIdentity.arn LIKE '%root%'
#   AND eventTime > TIMESTAMP '2026-04-01 00:00:00'
# ORDER BY eventTime DESC LIMIT 100
```

---

## 5. AWS Security Lake (GA April 2024 — OCSF normalized)

```python
from aws_cdk import aws_securitylake as sl

# Run in delegated admin account (Audit)
sl_data_lake = sl.CfnDataLake(self, "SecurityLake",
    configurations=[sl.CfnDataLake.DataLakeConfigurationProperty(
        region=self.region,
        encryption_configuration=sl.CfnDataLake.EncryptionConfigurationProperty(
            kms_key_id=log_key.key_arn,
        ),
        lifecycle_configuration=sl.CfnDataLake.LifecycleConfigurationProperty(
            transitions=[
                sl.CfnDataLake.TransitionProperty(days=90, storage_class="STANDARD_IA"),
                sl.CfnDataLake.TransitionProperty(days=365, storage_class="GLACIER_IR"),
            ],
            expiration=sl.CfnDataLake.ExpirationProperty(days=2557),
        ),
    )],
    meta_store_manager_role_arn=meta_role.role_arn,
)

# Add AWS-native log sources (auto-normalized to OCSF schema)
for source in [
    {"name": "ROUTE53", "version": "2.0"},
    {"name": "VPC_FLOW", "version": "2.0"},
    {"name": "CLOUD_TRAIL_MGMT", "version": "2.0"},
    {"name": "SH_FINDINGS", "version": "2.0"},
]:
    sl.CfnAwsLogSource(self, f"Src{source['name']}",
        data_lake_arn=sl_data_lake.attr_arns[0],
        source_name=source["name"],
        source_version=source["version"],
        accounts=[acct_id for acct_id in all_workload_accounts],
        regions=[self.region],
    )

# Subscriber for SIEM (Splunk / Datadog / Sumo Logic)
sl.CfnSubscriber(self, "SplunkSubscriber",
    data_lake_arn=sl_data_lake.attr_arns[0],
    subscriber_name="splunk-prod",
    sources=[
        sl.CfnSubscriber.AwsLogSourceProperty(source_name="VPC_FLOW", source_version="2.0"),
        sl.CfnSubscriber.AwsLogSourceProperty(source_name="CLOUD_TRAIL_MGMT", source_version="2.0"),
    ],
    subscriber_identity=sl.CfnSubscriber.SubscriberIdentityProperty(
        external_id="splunk-prod-external-id",
        principal=splunk_aws_account_id,
    ),
    access_types=["S3", "LAKEFORMATION"],   # S3 access OR Lake Formation grants
)
```

Now Athena, QuickSight, Snowflake, Databricks can all query the Security Lake via standard SQL — schema is OCSF.

```sql
-- Find all GuardDuty findings of severity > 5 in last 7 days
SELECT * FROM amazon_security_lake.amazon_guardduty
WHERE severity > 5
  AND time > current_timestamp - INTERVAL '7' DAY;
```

---

## 6. Common gotchas

- **Object Lock COMPLIANCE mode is permanent** — root user CANNOT shorten retention. Pick GOVERNANCE if you need a backdoor; COMPLIANCE for audit-grade.
- **CloudTrail org trail must be created in Management account.** Cannot delegate trail creation.
- **CloudTrail S3 data events cost $0.10/100K events.** A noisy bucket logs millions per day. Filter via `S3EventSelector` prefix.
- **CloudTrail Lake retention max = 7 years (2557 days).** For longer, copy to S3 Glacier Deep Archive.
- **Security Lake costs $$ per GB ingested + per GB queried.** Estimate before enabling all sources org-wide.
- **Security Lake region GA varies** — verify availability for your home region before relying on it.
- **Cross-region replication adds 15-min RPO** with Replication Time Control. Without RTC, replication is best-effort (can lag hours).
- **Object Lock + lifecycle Transitions to Glacier** work together — but objects in DEEP_ARCHIVE take 12h to retrieve. Plan for IR.
- **Bucket policy `Deny s3:DeleteBucket` requires Vault Lock** to be tamper-proof against future bucket policy changes by admin. Without Vault Lock, an admin can update the policy.
- **Security Lake Lake Formation grants are namespaced as `amazon_security_lake_<region>_<source>`.** Authoring queries from QuickSight requires LF-tag scope.
- **VPC Flow Logs format v3+ has a different schema** than v2. Security Lake expects v3 — verify VPC config.
- **Cross-account log delivery requires explicit S3 bucket policy** allowing `delivery.logs.amazonaws.com` write — easy to miss.
- **Don't enable CloudTrail Lake for every account by default** if you already have S3 trail — pay 2× ingestion cost.

---

## 7. Pytest worked example

```python
# tests/test_centralized_logging.py
import boto3, pytest

s3 = boto3.client("s3", region_name="us-east-1")
ct = boto3.client("cloudtrail", region_name="us-east-1")
sl = boto3.client("securitylake", region_name="us-east-1")


def test_log_archive_bucket_object_lock():
    bucket = "org-log-archive-..."
    cfg = s3.get_object_lock_configuration(Bucket=bucket)
    assert cfg["ObjectLockConfiguration"]["ObjectLockEnabled"] == "Enabled"
    rule = cfg["ObjectLockConfiguration"]["Rule"]["DefaultRetention"]
    assert rule["Mode"] == "COMPLIANCE"
    assert rule["Days"] >= 2555   # 7y


def test_log_archive_bucket_denies_delete():
    bucket = "org-log-archive-..."
    policy = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    statements = policy["Statement"]
    deny_delete = [s for s in statements
                   if s.get("Sid") == "DenyDelete" and s["Effect"] == "Deny"]
    assert deny_delete


def test_log_archive_cross_region_replication():
    bucket = "org-log-archive-..."
    cfg = s3.get_bucket_replication(Bucket=bucket)
    rules = cfg["ReplicationConfiguration"]["Rules"]
    assert any(r["Status"] == "Enabled" for r in rules)


def test_org_trail_active_and_multi_region():
    trails = ct.describe_trails(trailNameList=["org-trail"])["trailList"]
    assert trails
    t = trails[0]
    assert t["IsMultiRegionTrail"] is True
    assert t["IsOrganizationTrail"] is True
    assert t["LogFileValidationEnabled"] is True


def test_security_lake_data_lake_active():
    lakes = sl.list_data_lakes(regions=["us-east-1"])["dataLakes"]
    assert lakes
    assert lakes[0]["createStatus"] == "COMPLETED"
```

---

## 8. Five non-negotiables

1. **Org trail (multi-region, multi-account, log file validation enabled)** in Management account.
2. **Log Archive S3 bucket Object Lock COMPLIANCE mode** + 7y default retention + `DenyDelete` bucket policy.
3. **Cross-region replication** of Log Archive bucket (RTC enabled for 15-min RPO).
4. **CloudTrail Lake event data store** with `termination_protection_enabled` (cannot be deleted).
5. **Security Lake enabled with at minimum 4 sources**: CloudTrail mgmt, VPC Flow, Route 53, Security Hub findings.

---

## 9. References

- [CloudTrail org trail](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/creating-trail-organization.html)
- [CloudTrail Lake](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-lake.html)
- [AWS Security Lake (GA Apr 2024)](https://docs.aws.amazon.com/security-lake/latest/userguide/what-is-security-lake.html)
- [OCSF schema](https://schema.ocsf.io/)
- [S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-overview.html)
- [S3 Cross-Region Replication + RTC](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-time-control.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Org trail + Log Archive (Object Lock COMPLIANCE + CRR) + CloudTrail Lake + Security Lake (OCSF). Wave 11. |
