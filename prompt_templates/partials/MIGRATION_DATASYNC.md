# SOP — AWS DataSync (NFS · SMB · HDFS · object storage migration · scheduled · agent + agentless · S3/EFS/FSx targets)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS DataSync · DataSync agent (on-prem VM or EC2) · DataSync Discovery (block storage assessment) · sources: NFS / SMB / HDFS / object storage (S3-compatible, Azure Blob, GCS) · targets: S3, EFS, FSx for Windows/Lustre/ONTAP · scheduled tasks · bandwidth throttling

---

## 1. Purpose

- Codify **AWS DataSync** as the canonical AWS-native large-scale storage migration + replication tool. 10× faster than rsync/robocopy at scale; built-in encryption + integrity verification.
- Codify **agent deployment** — VMware OVA, Hyper-V VHDX, KVM, EC2 AMI; OR agentless for source EFS/FSx/S3.
- Codify **DataSync Discovery** — pre-migration assessment of on-prem block storage (analyzes IOPS, throughput, capacity → AWS recommendations).
- Codify **task patterns**: one-time migration, scheduled sync (incremental), continuous replication (deprecated for ongoing — use FSx for ONTAP backup or S3 cross-region replication for that).
- Codify **filter patterns**: include/exclude by glob; modified-since timestamp; preserve metadata (ACLs, ownership, timestamps).
- Codify **sources + targets matrix** + when to use which.
- This is the **storage migration specialisation**. Pairs with `MIGRATION_MGN` (servers), `MIGRATION_SCHEMA_CONVERSION` (DBs), `MIGRATION_HUB_STRATEGY` (org).

When the SOW signals: "migrate file shares to AWS", "lift NAS data to S3", "Hadoop HDFS to S3", "Isilon/NetApp to FSx", "decommission file servers".

---

## 2. Decision tree — source/target pairing

| Source | Target | Why |
|---|---|---|
| NFS (NetApp, Isilon, generic Linux) | S3 | analytics / archive |
| NFS | FSx for Lustre | HPC + ML training |
| NFS | FSx for ONTAP | NetApp-managed in AWS, lift-and-shift |
| NFS | EFS | shared file in AWS-native apps (EKS PV) |
| SMB (Windows file servers) | FSx for Windows File Server | AD-integrated; Windows ACLs preserved |
| SMB | S3 | archive / object access |
| HDFS | S3 (open table format S3 Tables / Iceberg) | Hadoop EOL → AWS lakehouse |
| Object (S3-compatible, Azure Blob, GCS) | S3 | cloud-to-cloud migration |
| EFS | EFS (other region/account) | DR replication |
| FSx for Windows | FSx for Windows | DR replication |
| S3 | S3 (cross-region) | replication (use S3 CRR instead — cheaper) |

```
Migration scale guidance:
  < 1 TB           → DataSync over public internet, single agent, < 1 day
  1-100 TB         → DataSync over Direct Connect or VPN, multi-agent, 1-7 days
  100 TB-1 PB      → DataSync over DX Hosted Connection, 5+ agents, weeks
  > 1 PB           → AWS Snowball Edge for bulk + DataSync for delta sync after
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single NFS share → S3, one-time | **§3 Monolith** |
| Production — multiple shares, scheduled sync, multi-agent, DX | **§5 Production** |

---

## 3. Monolith Variant — NFS → S3 one-time migration

### 3.1 Architecture

```
   On-prem data center
   ┌──────────────────────────────────────┐
   │ NFS share (NetApp/Isilon/Linux)       │
   │   /export/data, 10 TB                  │
   └────────────────┬─────────────────────┘
                    │ NFSv3 / v4
                    ▼
   ┌──────────────────────────────────────┐
   │ DataSync Agent (VMware OVA)            │
   │   - 4 vCPU + 32 GB minimum             │
   │   - Reads NFS source, encrypts in-flight│
   │   - Compresses, batches                  │
   └────────────────┬─────────────────────┘
                    │ TLS 1.2 over public internet OR DX
                    ▼
   ┌──────────────────────────────────────┐
   │ DataSync service (managed in AWS)     │
   │   - Validates checksums (MD5)          │
   │   - Writes to S3 with KMS encryption    │
   │   - Preserves metadata (timestamps,     │
   │     POSIX uid/gid, ACLs as object tags) │
   └────────────────┬─────────────────────┘
                    │
                    ▼
              S3 bucket (target)
              s3://datalake-raw/legacy-fileshare/
```

### 3.2 CDK

```python
# stacks/datasync_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_datasync as ds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct


class DataSyncStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 kms_key: kms.IKey, agent_arn: str,           # set after agent activated
                 source_nfs_server_ip: str, source_export_path: str,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Target S3 bucket ──────────────────────────────────────
        target_bucket = s3.Bucket(self, "TargetBucket",
            bucket_name=f"{env_name}-migrated-data-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── 2. IAM role for DataSync to write to S3 ──────────────────
        ds_role = iam.Role(self, "DsRole",
            assumed_by=iam.ServicePrincipal("datasync.amazonaws.com"),
        )
        target_bucket.grant_read_write(ds_role)
        kms_key.grant_encrypt_decrypt(ds_role)

        # ── 3. CloudWatch log group ──────────────────────────────────
        log_group = logs.LogGroup(self, "DsLogGroup",
            log_group_name=f"/aws/datasync/{env_name}-migration",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=kms_key,
        )

        # ── 4. Source location — NFS ─────────────────────────────────
        source_nfs = ds.CfnLocationNFS(self, "SourceNfs",
            server_hostname=source_nfs_server_ip,
            subdirectory=source_export_path,
            on_prem_config=ds.CfnLocationNFS.OnPremConfigProperty(
                agent_arns=[agent_arn],
            ),
            mount_options=ds.CfnLocationNFS.MountOptionsProperty(version="NFS4_0"),
        )

        # ── 5. Target location — S3 ──────────────────────────────────
        target_s3 = ds.CfnLocationS3(self, "TargetS3",
            s3_bucket_arn=target_bucket.bucket_arn,
            s3_config=ds.CfnLocationS3.S3ConfigProperty(
                bucket_access_role_arn=ds_role.role_arn,
            ),
            s3_storage_class="STANDARD",                    # or INTELLIGENT_TIERING / DEEP_ARCHIVE
            subdirectory="/legacy-fileshare/",
        )

        # ── 6. Task ──────────────────────────────────────────────────
        task = ds.CfnTask(self, "MigrationTask",
            name=f"{env_name}-fileshare-to-s3",
            source_location_arn=source_nfs.attr_location_arn,
            destination_location_arn=target_s3.attr_location_arn,
            cloud_watch_log_group_arn=log_group.log_group_arn,
            options=ds.CfnTask.OptionsProperty(
                # Verify mode
                verify_mode="ONLY_FILES_TRANSFERRED",         # or POINT_IN_TIME_CONSISTENT (slower)
                # Preserve metadata
                posix_permissions="PRESERVE",
                preserve_deleted_files="REMOVE",              # or PRESERVE; REMOVE = sync source-of-truth
                preserve_devices="NONE",
                uid="INT_VALUE",
                gid="INT_VALUE",
                # Performance
                bytes_per_second=-1,                          # -1 = unlimited; or 100_000_000 for 100MB/s
                task_queueing="ENABLED",                       # serialize concurrent tasks
                transfer_mode="CHANGED",                       # only changed files (incremental)
                # Logging
                log_level="TRANSFER",                          # OFF / BASIC / TRANSFER
                # Atime preserve
                atime="BEST_EFFORT",
                mtime="PRESERVE",
                # Object tags as ACLs (S3 target only)
                object_tags="PRESERVE",
            ),
            # Filters (optional) — exclude tmp/cache dirs
            excludes=[ds.CfnTask.FilterRuleProperty(
                filter_type="SIMPLE_PATTERN",
                value="*/tmp/*|*/cache/*|*.swp",
            )],
            # Schedule (optional — for incremental sync)
            schedule=ds.CfnTask.TaskScheduleProperty(
                schedule_expression="cron(0 2 * * ? *)",      # daily 2am
            ),
            tags=[{"key": "Wave", "value": "2"}],
        )
```

### 3.3 Agent deployment workflow

```bash
# ── 1. Download agent OVA / VHDX / EC2 AMI ──────────────────────────
# Console: DataSync → Agents → Create agent → choose hypervisor
# OVA URL: https://d3pkk32qbdosil.cloudfront.net/AWS-DataSync-x.x.x.ova

# ── 2. Deploy in vSphere / Hyper-V / KVM ───────────────────────────
# - 4 vCPU, 32 GB RAM minimum
# - 80 GB disk
# - Network: must reach source storage AND aws.amazon.com (HTTPS)

# ── 3. Get activation key ──────────────────────────────────────────
# After power-on, agent shows local IP; visit http://<agent-ip>/
# Click "Get activation key" → token returned
# OR via CLI:
curl "http://<agent-ip>/?gatewayType=SYNC&activationRegion=us-east-1"

# ── 4. Activate in DataSync console ────────────────────────────────
aws datasync create-agent \
  --activation-key <key> \
  --agent-name onprem-dc1-agent \
  --tags Key=Wave,Value=2

# ── 5. Run task ─────────────────────────────────────────────────────
aws datasync start-task-execution \
  --task-arn $TASK_ARN \
  --override-options TransferMode=CHANGED,VerifyMode=ONLY_FILES_TRANSFERRED

# Monitor progress:
aws datasync describe-task-execution --task-execution-arn $EXEC_ARN
# Output: BytesTransferred, FilesTransferred, EstimatedBytesToTransfer
```

---

## 4. SMB → FSx for Windows File Server (preserve ACLs)

```python
# Source SMB location
source_smb = ds.CfnLocationSMB(self, "SourceSmb",
    server_hostname="fileserver.corp.example.com",
    subdirectory="\\Share\\Data",
    user="datasync-svc",
    domain="CORP",
    password=secrets.SecretValue.secrets_manager(secret_arn),
    agent_arns=[agent_arn],
    mount_options=ds.CfnLocationSMB.MountOptionsProperty(version="SMB3"),
)

# Target FSx for Windows location
target_fsx = ds.CfnLocationFSxWindows(self, "TargetFsx",
    fsx_filesystem_arn=fsx_fs_arn,
    user="Admin",
    domain="CORP.AWS.LOCAL",
    password=secrets.SecretValue.secrets_manager(fsx_secret_arn),
    security_group_arns=[sg_arn],
    subdirectory="\\share-prod\\",
)

# Task — preserves Windows NTFS ACLs (DataSync uses SMB3 ACL APIs)
task = ds.CfnTask(self, "SmbTask",
    source_location_arn=source_smb.attr_location_arn,
    destination_location_arn=target_fsx.attr_location_arn,
    options=ds.CfnTask.OptionsProperty(
        smb_security_descriptor_copy_flags="OWNER_DACL_SACL",   # full SD preservation
        # ... rest of options ...
    ),
)
```

---

## 5. Production — DataSync Discovery + multi-agent + DX

### 5.1 Discovery (pre-migration assessment)

DataSync Discovery runs an agent that probes block storage performance characteristics; outputs CSV recommendations for AWS storage choice.

```bash
aws datasync add-storage-system \
  --server-configuration ServerHostname=netapp.corp.local,ServerPort=443 \
  --system-type NetAppONTAP \
  --agent-arns $DISCOVERY_AGENT_ARN \
  --credentials Username=netapp_admin,Password=... \
  --name prod-netapp

aws datasync start-discovery-job \
  --storage-system-arn $SS_ARN \
  --collection-duration-minutes 1440   # 24h scan

# After 24h, get recommendations
aws datasync get-discovery-job --discovery-job-arn $DJ_ARN
aws datasync describe-storage-system-resources \
  --discovery-job-arn $DJ_ARN \
  --resource-type SVM
# Output: per-volume recommended target (FSx ONTAP / FSx Windows / EFS / S3) + sizing
```

### 5.2 Multi-agent for scale

For > 1 TB/hr throughput:
- Deploy 4-8 agents in parallel
- Multiple tasks (one per agent or shared tasks)
- DataSync auto-balances task across agent pool
- Each agent: dedicated 10 Gbps NIC; SR-IOV if VMware ESXi
- DX hosted connection (1 Gbps or 10 Gbps) instead of public internet

### 5.3 Cost optimization

- DataSync charges $0.0125/GB transferred (one-time fee per byte). 1 TB = $12.50.
- Run DataSync at night to avoid prod source impact.
- Use `--bytes-per-second` to throttle during business hours.
- Avoid re-running on already-migrated data (use `TransferMode=CHANGED`).

---

## 6. Common gotchas

- **Agent pre-flight checks**: needs HTTPS egress to `*.amazonaws.com` + reachability to source. Run from agent: `curl -v https://datasync.us-east-1.amazonaws.com/`.
- **NFS export must allow agent IP** with no_root_squash for ACL preservation. Otherwise DataSync runs as nfsnobody → can't read root-owned files.
- **SMB requires kerberos OR NTLM** depending on share. Domain-join agent OR use DOMAIN\user format with password.
- **Agent OVA lock-in**: each agent activated to ONE region. Re-activation required to move regions.
- **`PRESERVE` permissions** on POSIX → S3 maps uid/gid to S3 object tags `s3:x-amz-meta-uid` etc. Most readers ignore them; preserve if you'll restore back to POSIX.
- **`PRESERVE_DELETED_FILES=REMOVE`** is destructive — files deleted from source ARE removed from target. Set REMOVE only if syncing source-of-truth replication.
- **HDFS source requires Kerberos config** + krb5.conf + keytab on agent. Hadoop classpath quirky.
- **S3 source for cloud-to-cloud needs DataSync IAM role with KMS access** to source bucket's KMS key (cross-account too).
- **Verify mode `POINT_IN_TIME_CONSISTENT` is slow** (full reverify after transfer). Use `ONLY_FILES_TRANSFERRED` for routine sync.
- **CloudWatch logging at TRANSFER level** logs every file path — can hit log retention $$$. Use BASIC for production unless debugging.
- **DataSync to S3 INTELLIGENT_TIERING** has 30-day minimum residency. For frequently-accessed data, use STANDARD.
- **Bandwidth not metering correctly** — DataSync reports compressed bytes. Real WAN consumption can be 30-70% of source size.
- **Multi-agent task balance** — DataSync picks any available agent per task. To control which agent does what, use multiple tasks pinned to specific agents.

---

## 7. Pytest worked example

```python
# tests/test_datasync.py
import boto3, pytest

ds = boto3.client("datasync")


def test_agent_online(agent_arn):
    agent = ds.describe_agent(AgentArn=agent_arn)
    assert agent["Status"] == "ONLINE"


def test_task_succeeded(task_arn):
    """Latest task execution should be SUCCESS or RUNNING (if scheduled)."""
    execs = ds.list_task_executions(TaskArn=task_arn)["TaskExecutions"]
    assert execs
    latest = execs[0]
    detail = ds.describe_task_execution(TaskExecutionArn=latest["TaskExecutionArn"])
    assert detail["Status"] in ["SUCCESS", "RUNNING"]


def test_no_files_failed(task_execution_arn):
    detail = ds.describe_task_execution(TaskExecutionArn=task_execution_arn)
    assert detail.get("Result", {}).get("ErrorCode") is None
    files_failed = detail.get("FilesTransferred", 0) - detail.get("FilesVerified", 0)
    assert files_failed == 0, f"{files_failed} files failed verification"


def test_preserve_metadata_enabled(task_arn):
    task = ds.describe_task(TaskArn=task_arn)
    opts = task["Options"]
    assert opts["PosixPermissions"] == "PRESERVE"
    assert opts["Mtime"] == "PRESERVE"


def test_kms_encryption_target_bucket(target_bucket):
    s3 = boto3.client("s3")
    enc = s3.get_bucket_encryption(Bucket=target_bucket)
    rules = enc["ServerSideEncryptionConfiguration"]["Rules"]
    assert rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
```

---

## 8. Five non-negotiables

1. **CMK encryption on target bucket / FSx / EFS** — never AWS-owned key.
2. **CloudWatch log group with KMS encryption + 30-day retention** — capture failures.
3. **`PRESERVE_DELETED_FILES=PRESERVE` for one-time migration**; `REMOVE` only for ongoing sync source-of-truth.
4. **`bytes_per_second` throttle during business hours** to protect production WAN.
5. **DataSync Discovery before migration** for any fleet > 50 TB — sizing + cost surprises avoided.

---

## 9. References

- [AWS DataSync — User Guide](https://docs.aws.amazon.com/datasync/latest/userguide/what-is-datasync.html)
- [DataSync Discovery](https://docs.aws.amazon.com/datasync/latest/userguide/datasync-discovery.html)
- [Agent deployment](https://docs.aws.amazon.com/datasync/latest/userguide/working-with-agents.html)
- [Task options](https://docs.aws.amazon.com/datasync/latest/userguide/configure-data-transfer.html)
- [SMB ACL preservation](https://docs.aws.amazon.com/datasync/latest/userguide/special-files.html)
- [DataSync pricing](https://aws.amazon.com/datasync/pricing/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Agent + agentless + Discovery + NFS/SMB/HDFS/S3 sources + S3/EFS/FSx targets + multi-agent + bandwidth throttling. Wave 13. |
