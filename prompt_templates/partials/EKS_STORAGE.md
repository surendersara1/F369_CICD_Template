# SOP — EKS Storage (EBS CSI · EFS CSI · FSx CSI · StorageClasses · snapshots · volume expansion)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · EBS CSI driver v1.34+ (gp3 default) · EFS CSI driver v2.0+ · FSx CSI driver v1.3+ (Lustre + ONTAP) · VolumeSnapshotClass · VolumeSnapshot CRDs · resizable PVCs

---

## 1. Purpose

- Codify the **three storage drivers** (EBS, EFS, FSx) and their canonical use cases:
  - **EBS** — RWO block storage. Pod-attached, AZ-scoped. Default for stateful apps (Postgres, Kafka, Elasticsearch).
  - **EFS** — RWX shared file. Multi-AZ, multi-pod. For shared config, model artifacts, web assets.
  - **FSx for Lustre** — high-perf parallel FS for ML training/HPC. Hydrated from S3.
  - **FSx for ONTAP** — NFS/SMB enterprise file. For Windows + Linux mixed; backup, snapshot, replication baked in.
- Codify the **StorageClass shape** that engagements should standardize on (encrypted, gp3 throughput-tunable, expansion enabled).
- Codify **VolumeSnapshotClass + VolumeSnapshot** for app-managed backups.
- Codify the **PVC patterns** for StatefulSets (one PVC per replica, topology spread).
- Codify **volume expansion** (online resize, no remount).
- This is the **persistent-storage specialisation**. Built on `EKS_CLUSTER_FOUNDATION` + `EKS_POD_IDENTITY`.

When the SOW signals: "stateful workloads on EKS", "Postgres in K8s", "shared model artifacts", "ML training data on Lustre", "Windows + Linux file share".

---

## 2. Decision tree — which CSI driver

```
Storage need?
├── Single-pod block (DB, message queue) → §3 EBS gp3 (RWO)
├── Multi-pod shared file (config, model artifacts) → §4 EFS (RWX)
├── ML training, parallel reads from S3 → §5 FSx Lustre PERSISTENT_2
├── Windows + Linux NFS/SMB enterprise → §6 FSx ONTAP
└── Object storage in pod (S3-as-bucket) → use `aws-mountpoint-s3-csi-driver` (Mountpoint-for-S3)

Performance tier?
├── Standard DB IOPS (< 16K) → gp3 (3000 baseline IOPS, $0.08/GB-mo)
├── High-IOPS DB (> 16K, ≤ 256K) → io2 Block Express
├── Throughput-bound (Kafka, log shipping) → gp3 with `throughput=1000` (1 GB/s)
└── Burstable cheap → gp2 (legacy — avoid; gp3 always cheaper)

Backup strategy?
├── Per-PVC snapshots (point-in-time) → §7 VolumeSnapshot + AWS Backup
├── Cluster-wide DR → §7 + replicate snapshots cross-region
└── App-managed (Postgres pg_dump to S3) → orthogonal — no CSI needed
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — EBS-only StatefulSet (Postgres) | **§3 Monolith** |
| Production — EBS + EFS + FSx + snapshot policy | **§8 Multi-driver Variant** |

---

## 3. EBS CSI driver + gp3 StorageClass

### 3.1 CDK install

```python
# stacks/storage_stack.py
from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks
from constructs import Construct
import json


class EbsCsiStack(Stack):
    def __init__(self, scope: Construct, id: str, *, cluster_name: str,
                 cluster: eks.ICluster, kms_key_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. IAM role (Pod Identity) ────────────────────────────────
        ebs_role = iam.Role(self, "EbsCsiRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEBSCSIDriverPolicy"),
            ],
        )
        ebs_role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))
        # CMK encryption — driver needs KMS permissions
        ebs_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["kms:CreateGrant", "kms:Encrypt", "kms:Decrypt",
                     "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"],
            resources=[kms_key_arn],
        ))

        eks.CfnPodIdentityAssociation(self, "EbsCsiAssoc",
            cluster_name=cluster_name,
            namespace="kube-system",
            service_account="ebs-csi-controller-sa",
            role_arn=ebs_role.role_arn,
        )

        # ── 2. EBS CSI add-on ─────────────────────────────────────────
        eks.CfnAddon(self, "EbsCsiAddon",
            cluster_name=cluster_name,
            addon_name="aws-ebs-csi-driver",
            addon_version="v1.34.0-eksbuild.1",
            resolve_conflicts="OVERWRITE",
            configuration_values=json.dumps({
                "controller": {
                    "replicaCount": 2,
                    "topologySpreadConstraints": [{
                        "maxSkew": 1,
                        "topologyKey": "topology.kubernetes.io/zone",
                        "whenUnsatisfiable": "ScheduleAnyway",
                        "labelSelector": {"matchLabels": {"app": "ebs-csi-controller"}},
                    }],
                },
            }),
        )
```

### 3.2 StorageClass YAML

```yaml
# manifests/storageclass-gp3.yaml — DEFAULT for all stateful workloads
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-encrypted
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"           # MB/s — bump to 1000 for log/throughput-bound
  encrypted: "true"
  kmsKeyId: arn:aws:kms:us-east-1:123456789012:key/xxxx
  fsType: ext4
  tagSpecification_1: "Name={{.PVCNamespace}}-{{.PVCName}}"
  tagSpecification_2: "Cluster=f369-prod-cluster"
allowVolumeExpansion: true     # online expand without restart
volumeBindingMode: WaitForFirstConsumer  # ensures pod's AZ matches volume AZ
reclaimPolicy: Delete
---
# High-IOPS class for OLTP workloads
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: io2-blockexpress
provisioner: ebs.csi.aws.com
parameters:
  type: io2
  iops: "32000"
  encrypted: "true"
  kmsKeyId: arn:aws:kms:us-east-1:123456789012:key/xxxx
allowVolumeExpansion: true
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain          # for compliance — operator must explicitly delete
```

### 3.3 StatefulSet using PVC (Postgres example)

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: prod-data
spec:
  serviceName: postgres-headless
  replicas: 3
  selector: { matchLabels: { app: postgres } }
  template:
    metadata: { labels: { app: postgres } }
    spec:
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector: { matchLabels: { app: postgres } }
      containers:
        - name: postgres
          image: postgres:16
          env:
            - { name: PGDATA, value: /var/lib/postgresql/data/pgdata }
          volumeMounts:
            - { name: data, mountPath: /var/lib/postgresql/data }
          resources:
            requests: { cpu: 1, memory: 4Gi }
            limits: { cpu: 4, memory: 16Gi }
  volumeClaimTemplates:
    - metadata: { name: data }
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: gp3-encrypted
        resources:
          requests: { storage: 100Gi }
```

---

## 4. EFS CSI driver + RWX file system

### 4.1 EFS file system + CDK

```python
# stacks/efs_stack.py
from aws_cdk import aws_efs as efs

efs_fs = efs.FileSystem(self, "SharedFs",
    vpc=vpc,
    encrypted=True,
    kms_key=kms_key,
    performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
    throughput_mode=efs.ThroughputMode.ELASTIC,    # auto-scales
    lifecycle_policy=efs.LifecyclePolicy.AFTER_30_DAYS,  # IA tier
    out_of_infrequent_access_policy=efs.OutOfInfrequentAccessPolicy.AFTER_1_ACCESS,
    enable_automatic_backups=True,
    file_system_policy=iam.PolicyDocument(statements=[
        iam.PolicyStatement(
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["*"],
            resources=["*"],
            conditions={"Bool": {"aws:SecureTransport": "false"}},  # TLS in transit
        ),
    ]),
)

# Allow ingress from EKS node SG
efs_fs.connections.allow_default_port_from(eks_nodes_sg)

# Access point per workload (POSIX uid/gid + rootDir scoping)
ap = efs_fs.add_access_point("ModelArtifacts",
    path="/models",
    create_acl=efs.Acl(owner_uid="1000", owner_gid="1000", permissions="0755"),
    posix_user=efs.PosixUser(uid="1000", gid="1000"),
)

# EFS CSI add-on (Pod Identity association similar to EBS — abbreviated)
eks.CfnAddon(self, "EfsCsiAddon",
    cluster_name=cluster_name,
    addon_name="aws-efs-csi-driver",
    addon_version="v2.0.7-eksbuild.1",
    resolve_conflicts="OVERWRITE",
)
```

### 4.2 EFS StorageClass + PVC

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: { name: efs-shared }
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap                       # access point dynamic provisioning
  fileSystemId: fs-xxxxxxxx
  directoryPerms: "0755"
  uid: "1000"
  gid: "1000"
  basePath: /dynamic_provisioning
mountOptions: [tls, iam]                          # TLS in transit + IAM auth
reclaimPolicy: Retain
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: model-artifacts, namespace: prod-ml }
spec:
  accessModes: [ReadWriteMany]
  storageClassName: efs-shared
  resources:
    requests: { storage: 100Gi }   # symbolic — EFS is elastic
```

---

## 5. FSx for Lustre — ML training, S3-hydrated

```python
# stacks/fsx_lustre_stack.py
from aws_cdk import aws_fsx as fsx

lustre = fsx.LustreFileSystem(self, "TrainingFs",
    vpc=vpc,
    vpc_subnet=vpc.private_subnets[0],
    storage_capacity_gib=2400,                 # 2.4 TiB minimum for SCRATCH_2
    lustre_configuration=fsx.LustreConfiguration(
        deployment_type=fsx.LustreDeploymentType.PERSISTENT_2,
        per_unit_storage_throughput=500,       # MB/s/TiB → 1.2 GB/s aggregate
        data_compression_type=fsx.LustreDataCompressionType.LZ4,
        # Hydrate from S3 — DRA (data repository association)
        data_repository_associations=[fsx.LustreDataRepositoryAssociation(
            data_repository_path="s3://training-data-prod/datasets/imagenet/",
            file_system_path="/imagenet",
            s3=fsx.LustreS3=...,  # auto-import + auto-export
        )],
    ),
    kms_key=kms_key,
    security_group=fsx_sg,
)
```

```yaml
# StorageClass for static FSx Lustre PV
apiVersion: v1
kind: PersistentVolume
metadata: { name: fsx-lustre-imagenet }
spec:
  capacity: { storage: 2400Gi }
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-xxxxxx::fsx-mountname
    volumeAttributes:
      dnsname: fs-xxxxxx.fsx.us-east-1.amazonaws.com
      mountname: abc123
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: imagenet, namespace: ml-training }
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  resources: { requests: { storage: 2400Gi } }
  volumeName: fsx-lustre-imagenet
```

---

## 6. FSx for ONTAP — Windows + Linux NFS/SMB

(Brief — skip detail for MVP partial.) Use `fsx.OntapFileSystem`, install `aws-fsx-openzfs-csi-driver` or `trident` (NetApp's CSI). Supports NFSv4 + SMB3 + iSCSI from same FS.

---

## 7. VolumeSnapshot for backups

```python
# stacks/snapshot_stack.py
# CSI snapshot CRDs are installed by the EBS CSI add-on; just create
# VolumeSnapshotClass + a CronJob to take periodic snapshots.

cluster.add_manifest("SnapshotClass", {
    "apiVersion": "snapshot.storage.k8s.io/v1",
    "kind": "VolumeSnapshotClass",
    "metadata": {"name": "ebs-snap"},
    "driver": "ebs.csi.aws.com",
    "deletionPolicy": "Retain",            # snapshots persist after PVC delete
    "parameters": {
        "encrypted": "true",
        "kmsKeyId": kms_key_arn,
        "tagSpecification_1": "BackupPolicy=daily-30day",
    },
})
```

```yaml
# CronJob — daily snapshot of postgres-0 PVC
apiVersion: batch/v1
kind: CronJob
metadata: { name: pg-snapshot, namespace: prod-data }
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: snapshot-creator
          restartPolicy: OnFailure
          containers:
            - name: snap
              image: bitnami/kubectl
              command:
                - sh
                - -c
                - |
                  cat <<EOF | kubectl apply -f -
                  apiVersion: snapshot.storage.k8s.io/v1
                  kind: VolumeSnapshot
                  metadata:
                    name: pg-$(date +%Y%m%d)
                    namespace: prod-data
                  spec:
                    volumeSnapshotClassName: ebs-snap
                    source:
                      persistentVolumeClaimName: data-postgres-0
                  EOF
```

For cross-region DR, **AWS Backup** (separate service) is the canonical path — schedules + lifecycle + cross-region copy + restore points.

---

## 8. Common gotchas

- **`volumeBindingMode: WaitForFirstConsumer` is mandatory for EBS.** Default `Immediate` provisions in random AZ → pod scheduled in different AZ → unschedulable forever.
- **gp2 is always more expensive than gp3** for equivalent perf. Migrate via `kubectl patch sc` or new SC + PVC swap.
- **EBS volume can NOT be detached/reattached across AZs.** If pod moves AZ (e.g., AZ outage), PVC stuck. Mitigate with replication at app layer (Postgres streaming, Kafka MirrorMaker).
- **EFS access points are required for security.** Without an AP, pods see entire EFS root with uid 0 → cross-tenant leak.
- **EFS throughput-mode "Bursting" is rate-limited** based on filesystem size. Use Elastic for unpredictable workloads.
- **FSx Lustre SCRATCH_2 has no replication** — instance failure = data loss. Use PERSISTENT_2 for anything reusable.
- **VolumeExpansion is online but only for ext4/xfs.** Filesystem expand happens automatically via CSI; resize PVC `resources.requests.storage` then wait for `pvc.status.capacity` update.
- **CSI snapshots are NOT cross-region.** Use AWS Backup or `ebs-snapshot-replication` Lambda for cross-region.
- **Mountpoint-for-S3 CSI is read-mostly + append-mode**. Not POSIX-compliant. Don't use for DBs.
- **EBS GP3 throughput maxes at 1000 MB/s.** For higher, switch to io2 Block Express (4000 MB/s).

---

## 9. Pytest worked example

```python
# tests/test_storage.py
import boto3, time

ec2 = boto3.client("ec2")
eks = boto3.client("eks")


def test_ebs_csi_addon_active(cluster_name):
    addon = eks.describe_addon(clusterName=cluster_name,
                                addonName="aws-ebs-csi-driver")["addon"]
    assert addon["status"] == "ACTIVE"


def test_all_volumes_encrypted(cluster_name):
    """Every EBS volume tagged with cluster has encryption=true."""
    vols = ec2.describe_volumes(Filters=[
        {"Name": "tag:Cluster", "Values": [cluster_name]},
    ])["Volumes"]
    unencrypted = [v["VolumeId"] for v in vols if not v["Encrypted"]]
    assert not unencrypted, f"Unencrypted volumes: {unencrypted}"


def test_storage_class_default_is_gp3(kubeconfig):
    """Default SC must be gp3-encrypted."""
    # subprocess.check_output(["kubectl", "get", "sc"]) and parse
    # Assert: only one SC has annotation `is-default-class: "true"`,
    # and it uses provisioner ebs.csi.aws.com with type gp3.
    pass


def test_snapshot_class_has_retain_policy(kubeconfig):
    """VolumeSnapshotClass deletionPolicy = Retain."""
    pass
```

---

## 10. Five non-negotiables

1. **`volumeBindingMode: WaitForFirstConsumer`** on every EBS StorageClass.
2. **`encrypted: true` + `kmsKeyId: <CMK>`** on every StorageClass — no AWS-owned-key encryption.
3. **`allowVolumeExpansion: true`** so PVCs can grow without rebuild.
4. **EFS access points only** — never raw EFS root mount.
5. **Snapshot policy in place** before any production stateful workload deploys (CronJob or AWS Backup).

---

## 11. References

- [EBS CSI driver — install](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html)
- [EFS CSI driver — install](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html)
- [FSx for Lustre CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html)
- [Mountpoint-for-S3 CSI](https://docs.aws.amazon.com/eks/latest/userguide/s3-csi.html)
- [VolumeSnapshot CRDs](https://kubernetes.io/docs/concepts/storage/volume-snapshots/)
- [AWS Backup for EBS / EFS / FSx](https://docs.aws.amazon.com/aws-backup/latest/devguide/whatisbackup.html)

---

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. EBS gp3 + EFS + FSx Lustre + ONTAP + CSI snapshots + StatefulSet patterns. Wave 9. |
