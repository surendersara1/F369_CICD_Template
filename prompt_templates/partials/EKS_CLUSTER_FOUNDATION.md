# SOP — EKS Cluster Foundation (managed control plane · node groups · OIDC · EKS access entries)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ (latest LTS) · managed node groups + Fargate profiles + (separate partial: Karpenter for dynamic provisioning) · OIDC provider for IRSA · EKS access entries (replaces aws-auth ConfigMap, GA 2024) · KMS envelope encryption for secrets · control plane logging · VPC + private endpoints

---

## 1. Purpose

- Codify the **EKS cluster foundation** that every other EKS partial builds on. Cluster shape that engagement teams can reuse without re-deriving.
- Cover the **modern access management**: EKS access entries (Console-managed, IAM-aware, replaces the legacy `aws-auth` ConfigMap pattern that has been the #1 footgun for 7 years).
- Codify the **OIDC provider** required for IRSA (IAM Roles for Service Accounts) and Pod Identity (newer, preferred — see `EKS_POD_IDENTITY`).
- Cover **node group strategies**: managed node groups (default), Fargate profiles (serverless pods), self-managed nodes (specialized).
- Codify the **secrets envelope encryption** with customer-managed KMS CMK.
- Codify **control plane logging** (5 log types: api, audit, authenticator, controllerManager, scheduler) → CloudWatch Logs.
- This is the **foundation specialisation**. `EKS_KARPENTER_AUTOSCALING` adds dynamic node provisioning. `EKS_POD_IDENTITY` adds IAM-for-pods. `EKS_NETWORKING` adds Load Balancer Controller. `EKS_OBSERVABILITY` adds metrics/logs/traces. All build on this foundation.

When the SOW signals: "we need EKS", "container orchestration at scale", "Kubernetes on AWS", "migrate from ECS to EKS", "deploy 50+ microservices", "GitOps platform".

---

## 2. Decision tree — node strategy

```
Workload type?
├── Stateless web/API services → Managed node groups (m6i / c6i) OR Fargate
├── Batch/CI workloads (variable) → §EKS_KARPENTER_AUTOSCALING (dynamic spot)
├── Compute-intensive (ML inference, video transcode) → Managed node groups (g5/inf2)
├── Stateful (databases, message queues) → Managed node groups (m6i + EBS gp3 + topology constraints)
├── Many tiny pods, no scaling needed → Fargate profiles (serverless, no node mgmt)
└── Mixed (most engagements) → Managed for baseline + Karpenter for elastic surge

Cluster auth model?
├── New cluster (post-2024) → §3 EKS access entries (modern, IAM-aware)
├── Migrating existing cluster → §4 transitional (access entries + aws-auth coexist)
└── Tightly-controlled (government, regulated) → §3 + restrict console-create
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — cluster + 1 node group + 1 namespace in single stack | **§3 Monolith Variant** |
| `NetworkStack` owns VPC; `EksClusterStack` owns cluster + node groups; `WorkloadStack` owns app deployments | **§7 Micro-Stack Variant** |

---

## 3. Monolith Variant — managed control plane + 1 managed node group

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  AWS-managed EKS control plane (multi-AZ, HA)                    │
   │     - Version: 1.32 (latest LTS as of 2026-04)                    │
   │     - Endpoint: private (or public+private for hybrid access)     │
   │     - Logging: api, audit, authenticator, controllerManager, scheduler→CloudWatch│
   │     - Secrets envelope encryption: customer-managed KMS CMK        │
   │     - OIDC provider auto-created (for IRSA + Pod Identity)         │
   │     - Add-ons: VPC CNI 1.18+, CoreDNS 1.11+, kube-proxy 1.32+,    │
   │       EKS Pod Identity Agent 1.x                                   │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Managed node group: baseline-mng                                │
   │     - Instance type: m6i.large × 3 (across 3 AZs)                 │
   │     - AMI: AL2023 ARM64 (or x86 if needed)                        │
   │     - Subnets: 3 private (PRIVATE_WITH_EGRESS for image pull)     │
   │     - SSH: disabled (use SSM Session Manager)                      │
   │     - Auto-scaling: 3-10 nodes (cluster-autoscaler OR karpenter)   │
   │     - Taints: none (general workloads)                              │
   └──────────────────────────────────────────────────────────────────┘

   Access:
   - Console / kubectl access via EKS access entries (NOT aws-auth ConfigMap)
   - AdminAccessEntry → cluster-admin role for break-glass
   - PolicyAccessEntry per team → namespace-scoped roles
```

### 3.2 CDK — `_create_eks_cluster_foundation()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_eks as eks,                       # L2 (high-level) — CfnCluster (L1) for full control
    aws_ec2 as ec2,
    aws_logs as logs,
)


def _create_eks_cluster_foundation(self, stage: str) -> None:
    """Monolith. Assumes self.{vpc, kms_key, permission_boundary} exist."""

    # A) Cluster IAM role (control plane assumes this)
    self.cluster_role = iam.Role(self, "EksClusterRole",
        role_name=f"{{project_name}}-eks-cluster-{stage}",
        assumed_by=iam.ServicePrincipal("eks.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSClusterPolicy"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSVPCResourceController"),
        ],
        permissions_boundary=self.permission_boundary,
    )

    # B) Node group IAM role (worker nodes assume this)
    self.node_role = iam.Role(self, "EksNodeRole",
        role_name=f"{{project_name}}-eks-node-{stage}",
        assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
            # SSM Session Manager — replaces SSH access for ops
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
        ],
        permissions_boundary=self.permission_boundary,
    )

    # C) Cluster — using L1 CfnCluster for full control over access config + logging
    self.eks_cluster = eks.CfnCluster(self, "EksCluster",
        name=f"{{project_name}}-{stage}",
        version="1.32",                               # latest LTS as of 2026-04
        role_arn=self.cluster_role.role_arn,
        resources_vpc_config=eks.CfnCluster.ResourcesVpcConfigProperty(
            subnet_ids=[s.subnet_id for s in self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
            security_group_ids=[self.cluster_sg.security_group_id],
            endpoint_public_access=False,             # private API endpoint
            endpoint_private_access=True,
            # public_access_cidrs=["10.0.0.0/8"],     # if hybrid mode needed
        ),
        kubernetes_network_config=eks.CfnCluster.KubernetesNetworkConfigProperty(
            ip_family="ipv4",                         # or ipv6 for IP-exhaustion mitigation
            service_ipv4_cidr="172.20.0.0/16",
        ),
        logging=eks.CfnCluster.LoggingProperty(
            cluster_logging=eks.CfnCluster.ClusterLoggingProperty(
                enabled_types=[
                    eks.CfnCluster.LoggingTypeConfigProperty(type="api"),
                    eks.CfnCluster.LoggingTypeConfigProperty(type="audit"),
                    eks.CfnCluster.LoggingTypeConfigProperty(type="authenticator"),
                    eks.CfnCluster.LoggingTypeConfigProperty(type="controllerManager"),
                    eks.CfnCluster.LoggingTypeConfigProperty(type="scheduler"),
                ],
            ),
        ),
        encryption_config=[
            eks.CfnCluster.EncryptionConfigProperty(
                provider=eks.CfnCluster.ProviderProperty(
                    key_arn=self.kms_key.key_arn,    # KMS envelope encryption for secrets
                ),
                resources=["secrets"],
            ),
        ],
        # ────── ACCESS ENTRIES (modern, replaces aws-auth ConfigMap) ──────
        access_config=eks.CfnCluster.AccessConfigProperty(
            authentication_mode="API_AND_CONFIG_MAP",  # API = access entries; CONFIG_MAP = legacy fallback
            # During migration, use API_AND_CONFIG_MAP. After all entries
            # migrated to API, set to "API" only and delete aws-auth.
            bootstrap_cluster_creator_admin_permissions=False,
        ),
    )

    # D) OIDC provider — required for IRSA and Pod Identity Associations
    self.oidc_provider = iam.CfnOIDCProvider(self, "EksOidcProvider",
        url=cdk.Fn.get_att(self.eks_cluster.logical_id,
                            "Identity.Oidc.Issuer").to_string(),
        client_id_list=["sts.amazonaws.com"],
        thumbprint_list=[],                           # auto-fetched in newer CDK
    )

    # E) Managed node group — baseline general-purpose
    self.baseline_mng = eks.CfnNodegroup(self, "BaselineMNG",
        cluster_name=self.eks_cluster.name,
        nodegroup_name="baseline",
        node_role=self.node_role.role_arn,
        subnets=[s.subnet_id for s in self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
        scaling_config=eks.CfnNodegroup.ScalingConfigProperty(
            min_size=3,
            max_size=10,
            desired_size=3,
        ),
        instance_types=["m6i.large"],
        ami_type="AL2023_ARM_64_STANDARD" if stage != "dev" else "AL2023_x86_64_STANDARD",
        capacity_type="ON_DEMAND",
        disk_size=50,
        update_config=eks.CfnNodegroup.UpdateConfigProperty(
            max_unavailable_percentage=33,            # rolling update 33% at a time
        ),
        # No remote_access — use SSM Session Manager
        labels={
            "workload-tier": "baseline",
            "node-pool":     "baseline",
        },
        # Taints: none (general-purpose pool)
        # Specialty pools (gpu, spot) defined separately or via Karpenter
    )
    self.baseline_mng.add_dependency(self.eks_cluster)

    # F) EKS-managed add-ons (replaces helm install for these core components)
    for addon in [
        ("vpc-cni",                "v1.18.5-eksbuild.1"),
        ("coredns",                "v1.11.3-eksbuild.2"),
        ("kube-proxy",             "v1.32.0-eksbuild.2"),
        ("eks-pod-identity-agent", "v1.3.4-eksbuild.1"),
    ]:
        eks.CfnAddon(self, f"Addon{addon[0].replace('-', '').title()}",
            cluster_name=self.eks_cluster.name,
            addon_name=addon[0],
            addon_version=addon[1],
            resolve_conflicts="OVERWRITE",
        ).add_dependency(self.eks_cluster)

    # G) Access entries (modern access management — replaces aws-auth)
    # Admin access entry (break-glass)
    eks.CfnAccessEntry(self, "AdminAccessEntry",
        cluster_name=self.eks_cluster.name,
        principal_arn=f"arn:aws:iam::{self.account}:role/EksBreakGlassRole",
        type="STANDARD",
        access_policies=[eks.CfnAccessEntry.AccessPolicyProperty(
            access_scope=eks.CfnAccessEntry.AccessScopeProperty(type="cluster"),
            policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
        )],
    )
    # Per-team access (namespace-scoped)
    for team in ["team-platform", "team-app"]:
        eks.CfnAccessEntry(self, f"TeamAccess{team}",
            cluster_name=self.eks_cluster.name,
            principal_arn=f"arn:aws:iam::{self.account}:role/{team}",
            type="STANDARD",
            access_policies=[eks.CfnAccessEntry.AccessPolicyProperty(
                access_scope=eks.CfnAccessEntry.AccessScopeProperty(
                    type="namespace",
                    namespaces=[team.replace("team-", "")],
                ),
                policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy",
            )],
        )

    CfnOutput(self, "ClusterName", value=self.eks_cluster.name)
    CfnOutput(self, "ClusterEndpoint",
              value=cdk.Fn.get_att(self.eks_cluster.logical_id, "Endpoint").to_string())
    CfnOutput(self, "ClusterCAData",
              value=cdk.Fn.get_att(self.eks_cluster.logical_id,
                                    "CertificateAuthorityData").to_string())
```

### 3.3 EKS access entries — the access policy catalog

Pre-built AWS-managed access policies (use these vs custom):

| Policy ARN suffix | Scope | Use case |
|---|---|---|
| `AmazonEKSClusterAdminPolicy` | cluster | Break-glass admin (root-equivalent) |
| `AmazonEKSAdminPolicy` | cluster or namespace | Day-to-day admin (no cluster-role-binding) |
| `AmazonEKSEditPolicy` | namespace | Developer can deploy + edit resources |
| `AmazonEKSViewPolicy` | namespace | Read-only |
| `AmazonEKSAdminViewPolicy` | namespace | View + view secrets |

```bash
# Add an access entry via CLI (post-deploy)
aws eks create-access-entry \
  --cluster-name qra-prod \
  --principal-arn arn:aws:iam::111111111111:role/MyDevRole \
  --type STANDARD

aws eks associate-access-policy \
  --cluster-name qra-prod \
  --principal-arn arn:aws:iam::111111111111:role/MyDevRole \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy \
  --access-scope type=namespace,namespaces=app-prod
```

### 3.4 kubectl access from local machine

```bash
# Update kubeconfig (uses STS via assumed role)
aws eks update-kubeconfig --region us-east-1 --name qra-prod \
  --role-arn arn:aws:iam::111111111111:role/MyDevRole

kubectl get nodes
kubectl get pods -n app-prod
```

---

## 4. Migration variant — coexist EKS access entries with legacy aws-auth

For existing clusters running `aws-auth` ConfigMap:

```python
access_config=eks.CfnCluster.AccessConfigProperty(
    authentication_mode="API_AND_CONFIG_MAP",       # both work
    bootstrap_cluster_creator_admin_permissions=False,
),
```

Migration plan:
1. Set `authentication_mode="API_AND_CONFIG_MAP"`.
2. Create access entries mirroring every aws-auth mapping (script via `kubectl get cm aws-auth -o yaml`).
3. Test access entries work for each principal (try `aws eks describe-access-entry`).
4. Once confirmed, set `authentication_mode="API"` and delete the aws-auth ConfigMap.
5. Document the cutover in runbook.

---

## 5. Fargate variant — serverless pods

For workloads where node management is overhead:

```python
self.fargate_profile = eks.CfnFargateProfile(self, "FargateApp",
    cluster_name=self.eks_cluster.name,
    fargate_profile_name="app-fargate",
    pod_execution_role_arn=self.fargate_pod_role.role_arn,
    selectors=[eks.CfnFargateProfile.SelectorProperty(
        namespace="app-prod",
        labels={"compute-type": "fargate"},
    )],
    subnets=[s.subnet_id for s in self.vpc.select_subnets(
        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
)
```

Pods scheduled to Fargate when matched. Trade-off: each pod = its own micro-VM, slower cold start (60s+), no DaemonSets, no persistent storage (use EFS).

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| `kubectl` returns `error: You must be logged in to the server` | `aws eks update-kubeconfig` not run, or wrong --role-arn | Re-run with --role-arn matching access entry principal |
| Pods can't pull from ECR | Node role missing `AmazonEC2ContainerRegistryReadOnly` | Verify on `EksNodeRole` managed policies |
| Control plane `audit` log not in CloudWatch | Logging not enabled | `cluster_logging.enabled_types` must include "audit" |
| Cluster creation timeout | Invalid VPC config / SG / subnet AZ mismatch | Cluster needs subnets in 2+ AZs, SG must allow control plane → nodes |
| Add-on stuck in `CREATE_FAILED` | Add-on version not compatible with cluster version | Check compatibility matrix; downgrade or upgrade cluster |
| OIDC provider thumbprint mismatch | Old thumbprint cached | Recreate `CfnOIDCProvider`; use auto-fetch in CDK v2.155+ |
| aws-auth changes don't propagate | Race with access entries | If both modes active, access entries WIN. Migrate fully to API mode |

---

## 7. Five non-negotiables

1. **Private API endpoint by default.** Public endpoint = control plane visible from internet. Set `endpoint_public_access=False` unless hybrid access required (then restrict to corp CIDR).

2. **Secrets envelope encryption with KMS CMK mandatory.** Without it, etcd stores secrets at rest with AWS-managed key only. CMK gives you key rotation control + audit trail.

3. **All 5 control plane log types enabled.** `audit` is the most important for compliance — without it, no record of who did what in cluster. Cost is negligible (~$0.50/GB).

4. **Use EKS access entries, NOT aws-auth ConfigMap, for new clusters.** aws-auth is the #1 historical source of EKS lockouts. Access entries are IAM-aware, auditable, and can't be accidentally `kubectl delete cm`'d.

5. **No SSH on nodes.** Node role gets `AmazonSSMManagedInstanceCore`; ops access via `aws ssm start-session --target i-...`. Disable EC2 key pair on node group.

---

## 8. References

- AWS docs:
  - [EKS overview](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html)
  - [EKS access entries](https://docs.aws.amazon.com/eks/latest/userguide/access-entries.html)
  - [Control plane logging](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html)
  - [Secrets envelope encryption](https://docs.aws.amazon.com/eks/latest/userguide/enable-kms.html)
  - [Add-ons](https://docs.aws.amazon.com/eks/latest/userguide/eks-add-ons.html)
- Related SOPs:
  - `EKS_KARPENTER_AUTOSCALING` — dynamic node provisioning
  - `EKS_POD_IDENTITY` — IAM for pods (Pod Identity Associations + IRSA)
  - `EKS_NETWORKING` — Load Balancer Controller + ALB/NLB
  - `EKS_OBSERVABILITY` — Container Insights + ADOT + Fluent Bit
  - `EKS_SECURITY` — Network Policies + Pod Security + GuardDuty for EKS
  - `LAYER_NETWORKING` — VPC + private subnets

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — EKS 1.32 cluster foundation. CDK monolith with private endpoint + KMS envelope + 5 log types + access entries (modern) + 4 EKS add-ons + baseline managed node group + OIDC provider. Migration path for legacy aws-auth. Fargate profile variant. 5 non-negotiables. Created Wave 9 (2026-04-26). |
