# SOP — AWS Application Migration Service (MGN · server lift-and-shift · post-launch actions · cutover · staging · vCenter agentless)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Application Migration Service (MGN, formerly CloudEndure Migration) · Replication agents on source servers · vCenter agentless replication · Source Server Connector · Launch templates · Post-launch actions (SSM documents) · Cutover + finalize

---

## 1. Purpose

- Codify **AWS MGN** as the canonical lift-and-shift path for migrating physical, virtual, and cloud servers to AWS EC2 with minimal downtime + minimal source app changes.
- Codify the **agent-based path** (Replication Agent on source) for any OS supported.
- Codify the **agentless path via vCenter Source Server Connector** for VMware vSphere fleets (no agents on each VM).
- Codify the **Launch Template configuration** — instance type, subnet, SG, IAM role, target right-sizing.
- Codify **Post-launch actions** — install CloudWatch Agent, SSM Agent, domain-join, run pre-set SSM documents.
- Codify the **wave-based cutover strategy** — staging → test → cutover → finalize.
- Codify **Replication settings** — bandwidth throttling, EBS encryption, staging area subnet, replication server type.
- This is the **server-migration specialisation**. Pairs with `MIGRATION_SCHEMA_CONVERSION` (DBs), `MIGRATION_DATASYNC` (storage), `MIGRATION_HUB_STRATEGY` (org-level orchestration).

When the SOW signals: "lift-and-shift Windows/Linux servers", "migrate from on-prem", "data center exit", "VMware to AWS", "Azure/GCP to AWS migration", "decommission DC by Q4".

---

## 2. Decision tree — agent vs agentless; rehost vs replatform

```
Source environment?
├── Physical servers (no hypervisor) → agent-based (only option)
├── VMware vSphere 6.7+ → agentless (vCenter Source Server Connector) preferred
├── Hyper-V / KVM / Xen → agent-based
├── EC2 in another AWS region → CloudEndure-style intra-AWS or AWS Backup copy-jobs
└── Azure / GCP VMs → agent-based

Migration approach (6R framework)?
├── Rehost (lift-and-shift) → §3 MGN (this partial)
├── Replatform (lift-tinker-shift, e.g., RDS for self-managed DB) → MGN + post-launch SSM
├── Refactor → AWS Migration Hub Refactor Spaces (separate engagement)
├── Repurchase (SaaS swap) → no migration tool needed
├── Retain → no migration
└── Retire → decommission, no migration

Cutover strategy?
├── < 50 servers → single wave
├── 50-500 → 3-5 waves of related apps (DB+app+web together)
└── > 500 → 6-12 waves with discovery + dependency mapping (use Migration Hub Strategy)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — agent-based, 5 servers, single test cutover | **§3 Monolith** |
| Production — agentless vCenter + 100+ servers + waves + post-launch automation | **§5 Production** |

---

## 3. Monolith Variant — agent-based, single wave

### 3.1 Architecture

```
   On-prem / source environment
   ┌──────────────────────────────────────┐
   │ Source servers (Windows, Linux)       │
   │   - MGN Replication Agent installed   │
   │   - Block-level replication (TLS)     │
   └─────────────────┬────────────────────┘
                     │ HTTPS to MGN endpoint
                     ▼
   ┌──────────────────────────────────────────────────────────┐
   │ AWS — MGN service (per region, per account)              │
   │                                                            │
   │ Staging Area Subnet (private subnet in target VPC)        │
   │   - MGN auto-launches t3.small "replication servers"      │
   │   - Receives replicated blocks → writes to staging EBS    │
   │   - One replication server per ~15 source servers         │
   │                                                            │
   │ Source Server inventory:                                   │
   │   - state: NOT_READY → READY_FOR_TESTING → READY_FOR_CUTOVER │
   │   - Launch template per server (instance type, IAM, ...)  │
   │   - Post-launch actions list                               │
   └─────────────────┬────────────────────────────────────────┘
                     │ "Test" or "Cutover" action
                     ▼
   ┌──────────────────────────────────────────────────────────┐
   │ Target VPC                                               │
   │   - Test instances (transient, terminate after test)      │
   │   - Cutover instances (final EC2)                         │
   │   - Post-launch actions execute via SSM:                  │
   │     · Install CW Agent + SSM Agent                        │
   │     · Domain-join AD                                       │
   │     · Restore IIS bindings / Apache configs               │
   │     · Run smoke tests                                      │
   └──────────────────────────────────────────────────────────┘
                     │ Source server → "Finalize" → marks complete
                     ▼
                Replication stops; staging volumes deleted
```

### 3.2 CDK setup (target VPC + IAM + post-launch SSM docs)

```python
# stacks/mgn_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_kms as kms
from constructs import Construct
import json


class MgnStack(Stack):
    """Pre-MGN setup. MGN itself is enabled via console/CLI per account+region.
    This stack creates: target VPC, staging subnet, IAM roles, post-launch SSM docs."""

    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 kms_key: kms.IKey, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Target VPC + staging subnet ─────────────────────────────
        # MGN can use existing VPC; create staging subnet inside it.
        vpc = ec2.Vpc(self, "MigrationVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.50.0.0/16"),
            max_azs=3,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=20,
                ),
                ec2.SubnetConfiguration(
                    name="staging", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,                           # /24 supports 250+ replication servers
                ),
            ],
        )

        # ── 2. SG for replication servers (staging area) ───────────────
        repl_sg = ec2.SecurityGroup(self, "ReplicationSg",
            vpc=vpc,
            description="MGN replication servers — receive 1500/tcp from sources",
            allow_all_outbound=True,
        )
        # Inbound: source IPs (parameterize per engagement)
        repl_sg.add_ingress_rule(
            ec2.Peer.ipv4("203.0.113.0/24"),                 # source data center CIDR
            ec2.Port.tcp(1500),
            "MGN replication from on-prem",
        )
        repl_sg.add_ingress_rule(
            ec2.Peer.ipv4("198.51.100.0/24"),                # Azure / second source
            ec2.Port.tcp(1500),
            "MGN replication from Azure",
        )

        # ── 3. IAM roles ──────────────────────────────────────────────
        # MGN needs 2 service roles auto-created, but you can pre-create
        # to control config: AWSServiceRoleForApplicationMigrationService
        # (no CDK; it's auto-created on first MGN init).
        # Custom Launch Template role (passed to migrated EC2):
        ec2_role = iam.Role(self, "MigratedEc2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
            ],
        )
        ec2_profile = iam.CfnInstanceProfile(self, "MigratedEc2Profile",
            instance_profile_name=f"{env_name}-migrated-ec2-profile",
            roles=[ec2_role.role_name],
        )

        # ── 4. Post-launch action: install CW Agent + SSM Agent (if missing) ──
        ssm.CfnDocument(self, "PostLaunchInstallAgents",
            document_type="Command",
            document_format="YAML",
            name=f"{env_name}-mgn-postlaunch-install-agents",
            content=ssm.CfnDocument.PROPS_CONTENT_TYPE,
            tags=[{"key": "MgnPostLaunch", "value": "true"}],
            content="""
schemaVersion: '2.2'
description: Install CloudWatch + SSM agents on migrated server
mainSteps:
  - name: InstallSsmAgent
    action: aws:runShellScript
    inputs:
      runCommand:
        - |
          set -ex
          if ! systemctl is-active --quiet amazon-ssm-agent; then
            cd /tmp
            wget https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/linux_amd64/amazon-ssm-agent.rpm
            rpm -i amazon-ssm-agent.rpm
            systemctl enable --now amazon-ssm-agent
          fi
  - name: InstallCloudWatchAgent
    action: aws:runShellScript
    inputs:
      runCommand:
        - |
          rpm -i https://s3.amazonaws.com/amazoncloudwatch-agent/redhat/amd64/latest/amazon-cloudwatch-agent.rpm || true
""",
        )

        # ── 5. Post-launch action: domain-join (Windows) ──────────────
        ssm.CfnDocument(self, "PostLaunchDomainJoin",
            document_type="Command",
            document_format="JSON",
            name=f"{env_name}-mgn-postlaunch-domain-join",
            tags=[{"key": "MgnPostLaunch", "value": "true"}],
            content=json.dumps({
                "schemaVersion": "2.2",
                "description": "Domain-join Windows server to AD",
                "parameters": {
                    "DomainName": {"type": "String"},
                    "DomainOu": {"type": "String"},
                },
                "mainSteps": [{
                    "name": "DomainJoin",
                    "action": "aws:domainJoin",
                    "inputs": {
                        "directoryId": "{{DomainName}}",
                        "directoryOU": "{{DomainOu}}",
                    },
                }],
            }),
        )

        # ── 6. Post-launch action: smoke test (curl health endpoint) ──
        ssm.CfnDocument(self, "PostLaunchSmokeTest",
            document_type="Command",
            document_format="YAML",
            name=f"{env_name}-mgn-postlaunch-smoke",
            tags=[{"key": "MgnPostLaunch", "value": "true"}],
            content="""
schemaVersion: '2.2'
description: Verify migrated app responds on local port
parameters:
  HealthCheckUrl:
    type: String
    default: http://localhost/healthz
mainSteps:
  - name: SmokeTest
    action: aws:runShellScript
    inputs:
      runCommand:
        - |
          for i in 1 2 3 4 5; do
            if curl -sf {{HealthCheckUrl}}; then
              echo "Smoke test PASS"
              exit 0
            fi
            sleep 10
          done
          echo "Smoke test FAILED" >&2
          exit 1
""",
        )

        # Output staging subnet ID (configure in MGN replication settings)
        from aws_cdk import CfnOutput
        CfnOutput(self, "StagingSubnetId",
            value=vpc.select_subnets(subnet_group_name="staging").subnet_ids[0],
        )
        CfnOutput(self, "ReplicationSgId", value=repl_sg.security_group_id)
        CfnOutput(self, "Ec2InstanceProfileArn", value=ec2_profile.attr_arn)
```

### 3.3 MGN console / CLI workflow

```bash
# ── 1. Initialize MGN (per account, per region — once) ─────────────
aws mgn initialize-service --region us-east-1
# Creates AWSServiceRoleForApplicationMigrationService

# ── 2. Configure replication settings template ──────────────────────
aws mgn create-replication-configuration-template \
  --staging-area-subnet-id subnet-xxx \
  --staging-area-tags Project=migration-q2,Wave=1 \
  --replication-server-instance-type t3.small \
  --use-dedicated-replication-server false \
  --default-large-staging-disk-type GP3 \
  --replication-servers-security-groups-ids sg-xxx \
  --bandwidth-throttling 0 \
  --create-public-ip false \
  --ebs-encryption CUSTOM \
  --ebs-encryption-key-arn arn:aws:kms:us-east-1:123:key/xxx \
  --associate-default-security-group false \
  --data-plane-routing PRIVATE_IP

# ── 3. Configure launch template (target instance shape) ───────────
aws mgn create-launch-configuration-template \
  --launch-disposition STARTED \
  --target-instance-type-right-sizing-method BASIC \
  --copy-private-ip false \
  --copy-tags true \
  --boot-mode LEGACY_BIOS \
  --post-launch-actions \
    "deployment=TEST_AND_CUTOVER,
     ssmDocuments=[
        {actionName=InstallAgents,ssmDocumentName=prod-mgn-postlaunch-install-agents,order=1},
        {actionName=DomainJoin,ssmDocumentName=prod-mgn-postlaunch-domain-join,order=2,parameters={DomainName={parameterType=STRING,parameterValue=corp.example.com}}},
        {actionName=SmokeTest,ssmDocumentName=prod-mgn-postlaunch-smoke,order=3}
     ]"

# ── 4. On each source server: install MGN Replication Agent ────────
# Linux:
wget -O ./aws-replication-installer-init.py \
  https://aws-application-migration-service-{region}.s3.{region}.amazonaws.com/latest/linux/aws-replication-installer-init.py
sudo python3 aws-replication-installer-init.py \
  --region us-east-1 \
  --aws-access-key-id AKIA... \
  --aws-secret-access-key ... \
  --no-prompt

# Windows (PowerShell):
Invoke-WebRequest -Uri "https://aws-application-migration-service-{region}.s3.{region}.amazonaws.com/latest/windows/AwsReplicationWindowsInstaller.exe" `
  -OutFile "$env:TEMP\AwsReplicationWindowsInstaller.exe"
& "$env:TEMP\AwsReplicationWindowsInstaller.exe" --region us-east-1 ...

# ── 5. Wait for replication: state goes NOT_READY → READY_FOR_TESTING ─
aws mgn describe-source-servers --query 'items[*].[sourceServerID,dataReplicationInfo.dataReplicationState]'

# ── 6. Test launch (creates transient EC2 in target VPC) ───────────
aws mgn start-test --source-server-i-ds s-XXXX

# ── 7. Validate test instance, then mark "Ready for Cutover" ───────
aws mgn finalize-cutover ...   # for test (terminates test instance)
# Or just mark ready manually after smoke test passes:
aws mgn change-server-life-cycle-state \
  --source-server-id s-XXXX --state READY_FOR_CUTOVER

# ── 8. Cutover (final launch) ──────────────────────────────────────
aws mgn start-cutover --source-server-i-ds s-XXXX

# ── 9. Once verified, finalize (stops replication) ─────────────────
aws mgn finalize-cutover --source-server-i-ds s-XXXX
# Source server marked DISCONNECTED; staging volumes deleted within 7d.
```

---

## 4. Agentless via vCenter Source Server Connector

For VMware fleets, install the Source Server Connector OVA in vCenter; it enumerates VMs and replicates without per-VM agent installation.

### 4.1 Steps

1. Console: MGN → Source servers → Add servers → Through vCenter
2. Download Source Server Connector OVA → import into vSphere
3. Configure with AWS access keys + region
4. Connector discovers all VMs; mark which to migrate
5. Replication starts using VMware Snapshot APIs (no agent on each VM)
6. Same launch / cutover flow as agent-based

**Limits:**
- vSphere 6.7+ required
- Up to 300 VMs per connector → use multiple connectors for larger fleets
- Some Linux distros not supported agentless (use agent-based for those)

---

## 5. Production Variant — wave-based migration with dependency awareness

```python
# Add wave tagging + Migration Hub integration
# Each source server tagged: Wave=1|2|3..., AppGroup=app1|app2

# Wave 1 (low-risk, 10 servers): dev environment
# Wave 2 (medium-risk, 50 servers): stage + internal tools
# Wave 3 (high-risk, 200 servers): production app + DB

# Per-wave runbook:
#   T-7 days: install agents on wave servers
#   T-3 days: validate replication state for all
#   T-1 day: run test launch on 2 sample servers per app group, smoke
#   T-0 (cutover window — typically a weekend):
#     1. Set source apps to read-only (DB freeze)
#     2. Wait for replication lag → 0
#     3. Stop sources (or quiesce)
#     4. start-cutover for all wave servers in dependency order
#     5. Run app-level cutover (DNS swap, LB target re-point)
#     6. Smoke test
#     7. If GO: finalize-cutover
#        If NO-GO: revert (start sources back up)
#   T+1: monitor for 48h before final source decommission
```

---

## 6. Common gotchas

- **Replication agent requires kernel module rebuild** on RHEL 7/8/9 — fails silently on older minor releases. Run `aws-replication-installer-init.py` with `--force-driver-install` if needed.
- **Source disk quirks**: dynamic disks (Windows), LVM with thin-provisioned volumes (Linux), and BitLocker-encrypted volumes can fail. Inventory FIRST.
- **Bandwidth saturation** — default unthrottled. Set `--bandwidth-throttling` in Mbps to protect production WAN.
- **Staging area subnet size** — t3.small replication servers + EBS volumes per source. /24 = ~250 source server limit. Plan /22 for large migrations.
- **Test launches don't trigger post-launch actions by default** — must enable `deployment=TEST_AND_CUTOVER`.
- **Boot mode**: x86 sources usually `LEGACY_BIOS`; UEFI sources need `UEFI`. Wrong choice = unbootable target.
- **Right-sizing modes**:
  - `NONE` = match source CPU/RAM exactly
  - `BASIC` = round up to closest matching EC2 type
  - Custom right-sizing = manually set per server
- **Post-launch actions FAIL if SSM Agent isn't running** on target. The Install Agents action must finish FIRST in `order: 1`.
- **MGN service role**: `AWSServiceRoleForApplicationMigrationService` needs `iam:CreateServiceLinkedRole` on first init — admin user required.
- **License (Windows BYOL vs PAYG)**: default = License Included (PAYG). For BYOL, set `license-configurations` in launch template (Server, SQL, etc.).
- **Network Load Balancer behind ALB** doesn't work — direct ALB → migrated EC2 works.
- **Replicating into a Transit Gateway-attached VPC** requires VPC endpoint for `mgn` service (or NAT Gateway with internet egress for control plane).
- **Cross-account migration** — staging area is in target account; source accounts use Source Server Connector with cross-account access keys. Or use AWS Account Factory.
- **Cutover replication LAG** — final block sync can take 5-60 min depending on disk activity. Quiesce app FIRST.
- **Cost** — replication servers + EBS staging + data transfer. ~$50/source server/mo during replication. Cutover ASAP after replication completes.

---

## 7. Pytest worked example

```python
# tests/test_mgn.py
import boto3, pytest

mgn = boto3.client("mgn")


def test_mgn_initialized():
    repl_template = mgn.describe_replication_configuration_templates()["items"]
    assert repl_template, "MGN not initialized"


def test_replication_uses_cmk(staging_subnet_id, kms_key_arn):
    template = mgn.describe_replication_configuration_templates()["items"][0]
    assert template["ebsEncryption"] == "CUSTOM"
    assert template["ebsEncryptionKeyArn"] == kms_key_arn
    assert template["stagingAreaSubnetId"] == staging_subnet_id


def test_post_launch_actions_configured(launch_template_id):
    template = mgn.get_launch_configuration_template(
        launchConfigurationTemplateID=launch_template_id,
    )
    actions = template.get("postLaunchActions", {}).get("ssmDocuments", [])
    names = [a["ssmDocumentName"] for a in actions]
    # Install Agents must be first
    assert names[0].endswith("install-agents")


def test_no_servers_in_lagging_state():
    """No source server should be lagging > 60 min."""
    servers = mgn.describe_source_servers()["items"]
    for s in servers:
        info = s.get("dataReplicationInfo", {})
        if info.get("dataReplicationState") == "REPLICATING":
            lag = info.get("lagDuration", "PT0S")
            # Parse ISO 8601 duration; assert < 60 min
            # (in real test, use isodate library)
            assert "PT5" in lag or "PT0" in lag, f"Server {s['sourceServerID']} lagging {lag}"


def test_all_wave_1_servers_ready_for_cutover():
    servers = mgn.describe_source_servers(
        filters={"sourceServerIDs": [], "isArchived": False},
    )["items"]
    wave_1 = [s for s in servers if any(t["key"] == "Wave" and t["value"] == "1"
                                          for t in s.get("tags", {}).items())]
    not_ready = [s["sourceServerID"] for s in wave_1
                 if s["lifeCycle"]["state"] != "READY_FOR_CUTOVER"]
    assert not not_ready, f"Wave 1 not ready: {not_ready}"
```

---

## 8. Five non-negotiables

1. **EBS encryption with CMK** on replication settings — never AWS-owned key.
2. **Post-launch action `install-agents` runs FIRST** with `order: 1`; subsequent actions assume SSM Agent is up.
3. **Staging subnet sized for fleet** — /24 for ≤ 250 servers, /22 for larger.
4. **Wave-based cutover with weekend windows** + GO/NO-GO checklist; never big-bang for > 50 servers.
5. **Decommission within 7 days post-cutover** — replication servers + staging EBS keep accruing $$$ otherwise.

---

## 9. References

- [AWS Application Migration Service — User Guide](https://docs.aws.amazon.com/mgn/latest/ug/what-is-application-migration-service.html)
- [vCenter Source Server Connector](https://docs.aws.amazon.com/mgn/latest/ug/vcenter-client.html)
- [Post-launch actions](https://docs.aws.amazon.com/mgn/latest/ug/post-launch-settings.html)
- [Replication networking](https://docs.aws.amazon.com/mgn/latest/ug/Network-Requirements.html)
- [MGN pricing](https://aws.amazon.com/application-migration-service/pricing/)
- [6R framework](https://aws.amazon.com/cloud-migration/strategies/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. MGN agent-based + agentless vCenter + post-launch SSM docs + wave cutover + right-sizing. Wave 13. |
