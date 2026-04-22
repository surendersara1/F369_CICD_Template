# SOP — EKS Container Platform (Kubernetes with Karpenter, LBC, External Secrets)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws_cdk.aws_eks` L2 `Cluster` · Kubernetes v1.31 · Karpenter NodePool v1 · AWS Load Balancer Controller · External Secrets Operator · EBS CSI Driver

---

## 1. Purpose

Provision a production-ready Amazon EKS cluster with the non-negotiable platform components:

- **EKS Cluster** (L2 `eks.Cluster`) — private endpoint, KMS secrets encryption, CloudWatch control-plane logging, managed system node group.
- **Karpenter** node autoscaler — NodePool + EC2NodeClass CRDs for spot/on-demand bin-packing (< 30 s node provisioning vs 3-5 min for Cluster Autoscaler).
- **AWS Load Balancer Controller (LBC)** — manages ALB/NLB from K8s Ingress resources, supports Shield / WAFv2 annotations.
- **External Secrets Operator (ESO)** — syncs Secrets Manager / SSM Parameter Store into K8s `Secret` objects (never hard-code secrets in YAML).
- **EBS CSI Driver** — encrypted `gp3` default StorageClass with per-stage `Retain`/`Delete` reclaim policy.

Include when SOW mentions Kubernetes, EKS, K8s, container orchestration, microservices platform, GitOps, ArgoCD, service mesh (Istio / App Mesh), or multi-tenant container workloads.

### EKS vs ECS decision

| Factor | Use EKS | Use ECS Fargate |
|---|---|---|
| Team has K8s expertise | Yes | No |
| Multi-cloud portability needed | Yes | No |
| Helm chart ecosystem needed | Yes | No |
| Service mesh (Istio / Linkerd) needed | Yes | No |
| Simplicity is priority | No | Yes |
| Cost at small scale | No (EKS = +~$73/mo/cluster control plane) | Yes |

Defer to `LAYER_BACKEND_ECS` for Fargate; this SOP is for teams that have already decided EKS.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| A single `cdk.Stack` class that owns VPC + KMS + EKS cluster + all node groups + all helm chart add-ons | **§3 Monolith Variant** |
| A dedicated `PlatformStack` (EKS cluster + addons) with VPC in `NetworkingStack` and KMS in `SecurityStack`; workload teams consume the cluster via its name / OIDC issuer / kubeconfig | **§4 Micro-Stack Variant** |

**Why the split matters.** The L2 `eks.Cluster` construct quietly mutates multiple resources across the stack boundary when features are enabled:

- `secrets_encryption_key=ext_kms_key` (when `ext_kms_key` lives in `SecurityStack`) — CDK auto-edits the KMS key policy to allow `eks.amazonaws.com`. Cross-stack → circular export.
- `vpc=ext_vpc` (from `NetworkingStack`) is safe on its own, but `vpc_subnets=ec2.SubnetSelection(...)` plus cluster-created SGs cause the VPC stack to pick up downstream references.
- `add_helm_chart(...)` with `values` that reference cross-stack KMS ARNs or IAM role ARNs (e.g. `storageClasses[].parameters.kmsKeyId`) bakes cross-stack tokens into the Helm-values CloudFormation custom-resource input — if the custom resource is in one stack and the KMS key in another, you hit the same cycle.
- `cluster.open_id_connect_provider.open_id_connect_provider_arn` passed to consumer-stack IAM roles (IRSA trust policies) is a CFN export. If the consumer also exports *back* (e.g. the workload's role ARN is referenced in a cluster-scoped RoleBinding), the cycle reappears.

The Micro-Stack variant:

1. Keeps the KMS key and VPC owned by upstream stacks; `PlatformStack` creates a **local CMK for EKS secrets encryption** (5th non-negotiable — never set `secrets_encryption_key=` to a cross-stack key).
2. Grants identity-side on all IRSA / Pod Identity roles — never uses `ext_resource.grant_*(sa_role)` across stacks.
3. Publishes cluster name, cluster ARN, OIDC issuer, kubectl role ARN, EBS CSI role ARN, Karpenter node role ARN via SSM. Consumer workload stacks read these and build their own IRSA trust policies without touching `PlatformStack`.
4. Asset paths (Karpenter manifests, any custom Lambdas for bootstrap) anchored via `_LAMBDAS_ROOT = Path(__file__).resolve().parents[3] / "lambda"`.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` subclass holds VPC + KMS + EKS cluster + all addons. Typical for POC, single-team platform, internal tools.

### 3.1 Architecture

```
               ┌────────────── EKS Cluster (v1.31) ──────────────┐
               │   Private API endpoint (prod) / hybrid (dev)    │
               │   KMS secrets encryption (envelope)              │
  ALB/NLB ◄────┤   CloudWatch logs: API/Audit/Scheduler/CM/Auth   │
  (LBC)        │                                                  │
               │   ┌──────────── Managed Node Groups ────────┐    │
               │   │ system (m5.large x 2 on-demand, tainted) │    │
               │   └──────────────────────────────────────────┘    │
               │                                                  │
               │   ┌──────────── Karpenter NodePool ──────────┐    │
               │   │ spot+on-demand, c/m/r families, gen > 4  │    │
               │   │ IMDSv2 enforced, gp3 EBS encrypted       │    │
               │   └──────────────────────────────────────────┘    │
               │                                                  │
               │   Addons: LBC · ESO · EBS CSI · VPC CNI · Pod ID │
               └──────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_eks_platform()` method body

```python
def _create_eks_platform(self, stage_name: str) -> None:
    """
    Amazon EKS Cluster — Production-Ready Kubernetes Platform.

    Components:
      A) EKS Cluster (L2) with managed system node group
      B) Karpenter node autoscaler (NodePool + EC2NodeClass)
      C) AWS Load Balancer Controller (ALB Ingress)
      D) External Secrets Operator (Secrets Manager → K8s Secrets)
      E) EBS CSI Driver (encrypted gp3 default StorageClass)
    """

    from aws_cdk import (
        CfnOutput,
        aws_eks as eks,
        aws_ec2 as ec2,
        aws_iam as iam,
    )

    # =========================================================================
    # A) EKS CLUSTER
    # =========================================================================

    eks_cluster = eks.Cluster(
        self, "EKSCluster",
        cluster_name=f"{{project_name}}-{stage_name}",
        version=eks.KubernetesVersion.V1_31,
        cluster_logging=[
            eks.ClusterLoggingTypes.API,
            eks.ClusterLoggingTypes.AUTHENTICATOR,
            eks.ClusterLoggingTypes.SCHEDULER,
            eks.ClusterLoggingTypes.CONTROLLER_MANAGER,
            eks.ClusterLoggingTypes.AUDIT,
        ],

        # VPC configuration (private endpoint for prod)
        vpc=self.vpc,
        vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
        endpoint_access=(
            eks.EndpointAccess.PRIVATE if stage_name == "prod"
            else eks.EndpointAccess.PUBLIC_AND_PRIVATE
        ),

        # Secrets encryption with KMS (monolith: same-stack key is safe)
        secrets_encryption_key=self.kms_key,

        # We use Karpenter for workloads; system node group added below
        default_capacity=0,

        # kubectl Lambda / CodeBuild masters role
        masters_role=iam.Role(
            self, "EKSMastersRole",
            assumed_by=iam.CompositePrincipal(
                iam.AccountRootPrincipal(),
                iam.ServicePrincipal("codebuild.amazonaws.com"),
            ),
            role_name=f"{{project_name}}-eks-masters-{stage_name}",
        ),
    )

    # =========================================================================
    # SYSTEM MANAGED NODE GROUP
    # For Karpenter, CoreDNS, VPC CNI, LBC — tainted so workloads don't land here
    # =========================================================================

    system_ng = eks_cluster.add_nodegroup_capacity(
        "SystemNodeGroup",
        nodegroup_name=f"{{project_name}}-system-{stage_name}",
        instance_types=[
            ec2.InstanceType("m5.large"),   # 2 vCPU, 8 GB — sufficient for system pods
        ],
        min_size=2,
        max_size=4,
        desired_size=2,
        capacity_type=eks.CapacityType.ON_DEMAND,
        ami_type=eks.NodegroupAmiType.AL2_X86_64,
        disk_size=50,
        subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        labels={"role": "system", "compute-type": "managed"},
        taints=[{"key": "CriticalAddonsOnly", "value": "true", "effect": "NO_SCHEDULE"}],
    )

    # =========================================================================
    # B) KARPENTER — Node Autoscaler
    # Karpenter provisions nodes in <30 seconds vs 3-5 minutes for CA
    # =========================================================================

    karpenter_role = iam.Role(
        self, "KarpenterNodeRole",
        assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        role_name=f"KarpenterNodeRole-{{project_name}}-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
        ],
    )

    # Karpenter NodePool CRD (applied as manifest)
    karpenter_nodepool_manifest = {
        "apiVersion": "karpenter.sh/v1",
        "kind": "NodePool",
        "metadata": {"name": "default"},
        "spec": {
            "template": {
                "spec": {
                    "nodeClassRef": {"group": "karpenter.k8s.aws", "kind": "EC2NodeClass", "name": "default"},
                    "requirements": [
                        {"key": "karpenter.sh/capacity-type", "operator": "In",
                         "values": ["spot", "on-demand"] if stage_name != "prod" else ["on-demand"]},
                        {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64", "arm64"]},
                        {"key": "karpenter.k8s.aws/instance-category", "operator": "In",
                         "values": ["c", "m", "r"]},  # Compute, Memory, balanced
                        {"key": "karpenter.k8s.aws/instance-generation", "operator": "Gt", "values": ["4"]},
                    ],
                    "expireAfter": "720h",  # Rotate nodes every 30 days (security best practice)
                }
            },
            "limits": {"cpu": 1000, "memory": "1000Gi"},
            "disruption": {
                "consolidationPolicy": "WhenEmptyOrUnderutilized",
                "consolidateAfter": "30s",  # Bin-pack aggressively to save cost
            },
        },
    }

    # Karpenter EC2NodeClass
    karpenter_nodeclass_manifest = {
        "apiVersion": "karpenter.k8s.aws/v1",
        "kind": "EC2NodeClass",
        "metadata": {"name": "default"},
        "spec": {
            "amiFamily": "AL2",
            "role": karpenter_role.role_name,
            "subnetSelectorTerms": [{"tags": {"kubernetes.io/role/internal-elb": "1"}}],
            "securityGroupSelectorTerms": [{"tags": {"aws:eks:cluster-name": f"{{project_name}}-{stage_name}"}}],
            "blockDeviceMappings": [{
                "deviceName": "/dev/xvda",
                "ebs": {
                    "volumeSize": "50Gi",
                    "volumeType": "gp3",
                    "iops": 3000,
                    "encrypted": True,
                    "kmsKeyID": self.kms_key.key_arn,
                },
            }],
            "metadataOptions": {
                "httpEndpoint": "enabled",
                "httpProtocolIPv6": "disabled",
                "httpPutResponseHopLimit": 1,   # IMDSv2 enforced (security)
                "httpTokens": "required",       # IMDSv2 only
            },
            "userData": "#!/bin/bash\n/etc/eks/bootstrap.sh {{project_name}}-" + stage_name,
        },
    }

    eks_cluster.add_manifest("KarpenterNodePool", karpenter_nodepool_manifest)
    eks_cluster.add_manifest("KarpenterEC2NodeClass", karpenter_nodeclass_manifest)

    # =========================================================================
    # C) AWS LOAD BALANCER CONTROLLER (Helm install)
    # Manages ALB/NLB from K8s Ingress resources
    # =========================================================================

    lbc_role = iam.Role(
        self, "LBCRole",
        assumed_by=iam.WebIdentityPrincipal(
            eks_cluster.open_id_connect_provider.open_id_connect_provider_arn,
            conditions={
                "StringEquals": {
                    f"{eks_cluster.cluster_open_id_connect_issuer}:aud": "sts.amazonaws.com",
                    f"{eks_cluster.cluster_open_id_connect_issuer}:sub": "system:serviceaccount:kube-system:aws-load-balancer-controller",
                }
            }
        ),
        role_name=f"{{project_name}}-lbc-{stage_name}",
    )
    lbc_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "elasticloadbalancing:*",
            "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
            "ec2:DescribeInstances", "ec2:DescribeInternetGateways",
            "cognito-idp:DescribeUserPoolClient",
            "acm:ListCertificates", "acm:DescribeCertificate",
            "wafv2:GetWebACL", "wafv2:AssociateWebACL",
            "shield:DescribeProtection", "shield:CreateProtection",
        ],
        resources=["*"],
    ))

    eks_cluster.add_helm_chart(
        "AWSLoadBalancerController",
        chart="aws-load-balancer-controller",
        repository="https://aws.github.io/eks-charts",
        namespace="kube-system",
        release="aws-load-balancer-controller",
        values={
            "clusterName": f"{{project_name}}-{stage_name}",
            "serviceAccount": {
                "create": True,
                "name": "aws-load-balancer-controller",
                "annotations": {"eks.amazonaws.com/role-arn": lbc_role.role_arn},
            },
            "replicaCount": 2,     # HA LBC
            "enableShield": True,
            "enableWaf": True,
            "enableWafv2": True,
        },
    )

    # =========================================================================
    # D) EXTERNAL SECRETS OPERATOR
    # Syncs from Secrets Manager / SSM Parameter Store → K8s Secrets
    # Never hard-code secrets in K8s YAML
    # =========================================================================

    eso_role = iam.Role(
        self, "ESORole",
        assumed_by=iam.WebIdentityPrincipal(
            eks_cluster.open_id_connect_provider.open_id_connect_provider_arn,
            conditions={
                "StringEquals": {
                    f"{eks_cluster.cluster_open_id_connect_issuer}:sub":
                        "system:serviceaccount:external-secrets:external-secrets",
                }
            }
        ),
        role_name=f"{{project_name}}-eso-{stage_name}",
    )
    eso_role.add_to_policy(iam.PolicyStatement(
        actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
                 "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath",
                 "kms:Decrypt"],
        resources=["*"],
    ))

    eks_cluster.add_helm_chart(
        "ExternalSecretsOperator",
        chart="external-secrets",
        repository="https://charts.external-secrets.io",
        namespace="external-secrets",
        create_namespace=True,
        release="external-secrets",
        values={
            "serviceAccount": {
                "annotations": {"eks.amazonaws.com/role-arn": eso_role.role_arn},
            },
            "replicaCount": 2,
        },
    )

    # =========================================================================
    # E) EBS CSI DRIVER + STORAGE CLASSES
    # =========================================================================

    eks_cluster.add_helm_chart(
        "EBSCSIDriver",
        chart="aws-ebs-csi-driver",
        repository="https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
        namespace="kube-system",
        release="aws-ebs-csi-driver",
        values={
            "controller": {
                "serviceAccount": {
                    "annotations": {"eks.amazonaws.com/role-arn": self.ebs_csi_role.role_arn},
                }
            },
            "storageClasses": [
                {
                    "name": "gp3-encrypted",
                    "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                    "parameters": {
                        "type": "gp3",
                        "encrypted": "true",
                        "kmsKeyId": self.kms_key.key_arn,
                    },
                    "reclaimPolicy": "Retain" if stage_name == "prod" else "Delete",
                    "allowVolumeExpansion": True,
                }
            ],
        },
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    CfnOutput(self, "EKSClusterName",
        value=eks_cluster.cluster_name,
        description="EKS Cluster name — use in kubectl config",
        export_name=f"{{project_name}}-eks-cluster-{stage_name}",
    )
    CfnOutput(self, "EKSClusterArn",
        value=eks_cluster.cluster_arn,
        description="EKS Cluster ARN",
        export_name=f"{{project_name}}-eks-arn-{stage_name}",
    )
    CfnOutput(self, "EKSKubectlCmd",
        value=f"aws eks update-kubeconfig --region {self.region} --name {{project_name}}-{stage_name}",
        description="Command to configure kubectl for this cluster",
    )
```

### 3.3 Monolith gotchas

- **`eks.Cluster` L2 requires a `version=`** argument; picking a version older than 1.28 on a new deploy will be rejected by EKS. Track Kubernetes version support — https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html.
- **`eks-blueprints` module is deprecated.** If older docs recommend `eks.Cluster.add_blueprints(...)`, ignore — use `eks.Cluster` L2 + explicit `add_nodegroup_capacity` + `add_helm_chart` / `add_manifest` directly.
- **`secrets_encryption_key=self.kms_key`** requires the key policy allow `eks.amazonaws.com` via `kms:Encrypt/Decrypt/ReEncrypt*/GenerateDataKey*/DescribeKey`. CDK handles this implicitly only when key + cluster live in the same stack.
- **Karpenter NodePool CRD** must be installed before the NodePool manifest is applied. The `add_manifest` call creates a CFN custom resource; if the Karpenter Helm chart itself is not yet applied in the same stack, the NodePool creation silently fails. Order: Helm chart first, then `add_manifest`.
- **`masters_role`** — the supplied role becomes a cluster admin via AWS auth ConfigMap. Locking this down post-deploy requires editing the CM via `kubectl edit -n kube-system aws-auth`, not CDK.
- **Karpenter v1 manifests** differ from v1beta1 / v0.32: `apiVersion: karpenter.sh/v1` and `karpenter.k8s.aws/v1`. If your Helm chart pins an older Karpenter version, keep the manifests aligned to that API version.

---

## 4. Micro-Stack Variant

**Use when:** VPC in `NetworkingStack`, KMS in `SecurityStack`, EKS cluster + addons in `PlatformStack`, workload namespaces in separate `WorkloadStack`s.

### 4.1 The five non-negotiables

Memorize these (reference: `LAYER_BACKEND_LAMBDA` §4.1). Every cross-stack EKS failure reduces to one of them.

1. **Anchor asset paths to `__file__`, never relative-to-CWD.** Any Lambda used for cluster bootstrap (custom-resource installers, `kubectl` runners) uses `Path(__file__).resolve().parents[3] / "lambda" / "<name>"`.
2. **Never use `X.grant_*(role)` on a cross-stack resource X.** IRSA / Pod Identity roles get `PolicyStatement` with identity-side resources; do not call `ext_secret.grant_read(sa_role)`.
3. **Never target a cross-stack queue with `targets.SqsQueue(q)`.** Not directly relevant for EKS, but if an EventBridge alarm wraps to SQS, use L1 `CfnRule`.
4. **Never own a bucket in one stack and attach its CloudFront OAC in another.** Not relevant here.
5. **Never set `encryption_key=ext_key` where `ext_key` came from another stack.** The EKS `secrets_encryption_key` is a **local CMK owned by `PlatformStack`** (5th non-negotiable). Same rule applies to the EBS CSI default StorageClass KMS — resolve via SSM ARN string, not by passing an `IKey` across stacks.

Also: `permission_boundary` applied to every role in this stack.

### 4.2 `PlatformStack` (EKS + addons)

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, Duration, RemovalPolicy, CfnOutput,
    aws_ec2 as ec2,
    aws_eks as eks,
    aws_iam as iam,
    aws_kms as kms,
    aws_ssm as ssm,
)
from constructs import Construct

# stacks/platform_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class PlatformStack(cdk.Stack):
    """Owns the EKS cluster, platform CMK (local), system node group,
    Karpenter, LBC, ESO, and EBS CSI. VPC comes in by interface; all
    other upstream refs (alert topic, boundary) via SSM strings.

    Publishes cluster name, cluster ARN, OIDC issuer, and the Karpenter
    node role ARN via SSM so workload stacks can consume.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        vpc: ec2.IVpc,
        permission_boundary: iam.IManagedPolicy,
        **kwargs,
    ) -> None:
        super().__init__(scope, f"{{project_name}}-platform-{stage_name}", **kwargs)

        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk", "Layer": "Platform"}.items():
            cdk.Tags.of(self).add(k, v)

        IS_PROD = stage_name == "prod"

        # -----------------------------------------------------------------
        # Local CMK (5th non-negotiable — NEVER set secrets_encryption_key
        # to a cross-stack key. Create a local one owned here.)
        # -----------------------------------------------------------------
        cmk = kms.Key(self, "PlatformKey",
            alias=f"alias/{{project_name}}-eks-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -----------------------------------------------------------------
        # A) EKS Cluster — masters role is local
        # -----------------------------------------------------------------
        masters_role = iam.Role(
            self, "EKSMastersRole",
            assumed_by=iam.CompositePrincipal(
                iam.AccountRootPrincipal(),
                iam.ServicePrincipal("codebuild.amazonaws.com"),
            ),
            role_name=f"{{project_name}}-eks-masters-{stage_name}",
        )
        iam.PermissionsBoundary.of(masters_role).apply(permission_boundary)

        eks_cluster = eks.Cluster(
            self, "EKSCluster",
            cluster_name=f"{{project_name}}-{stage_name}",
            version=eks.KubernetesVersion.V1_31,  # TODO(verify): eks.KubernetesVersion.V1_31 constant exists in current CDK v2
            cluster_logging=[
                eks.ClusterLoggingTypes.API,
                eks.ClusterLoggingTypes.AUTHENTICATOR,
                eks.ClusterLoggingTypes.SCHEDULER,
                eks.ClusterLoggingTypes.CONTROLLER_MANAGER,
                eks.ClusterLoggingTypes.AUDIT,
            ],
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            endpoint_access=(
                eks.EndpointAccess.PRIVATE if IS_PROD
                else eks.EndpointAccess.PUBLIC_AND_PRIVATE
            ),
            secrets_encryption_key=cmk,            # LOCAL key — honors 5th non-negotiable
            default_capacity=0,
            masters_role=masters_role,
        )

        # -----------------------------------------------------------------
        # System managed node group (tainted — Karpenter scales workloads)
        # -----------------------------------------------------------------
        eks_cluster.add_nodegroup_capacity(
            "SystemNodeGroup",
            nodegroup_name=f"{{project_name}}-system-{stage_name}",
            instance_types=[ec2.InstanceType("m5.large")],
            min_size=2, max_size=4, desired_size=2,
            capacity_type=eks.CapacityType.ON_DEMAND,
            ami_type=eks.NodegroupAmiType.AL2_X86_64,
            disk_size=50,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            labels={"role": "system", "compute-type": "managed"},
            taints=[{"key": "CriticalAddonsOnly", "value": "true", "effect": "NO_SCHEDULE"}],
        )

        # -----------------------------------------------------------------
        # B) Karpenter node role (local) + NodePool/EC2NodeClass manifests
        # -----------------------------------------------------------------
        karpenter_role = iam.Role(
            self, "KarpenterNodeRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            role_name=f"KarpenterNodeRole-{{project_name}}-{stage_name}",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        iam.PermissionsBoundary.of(karpenter_role).apply(permission_boundary)

        nodepool_manifest = {
            "apiVersion": "karpenter.sh/v1",
            "kind": "NodePool",
            "metadata": {"name": "default"},
            "spec": {
                "template": {"spec": {
                    "nodeClassRef": {"group": "karpenter.k8s.aws", "kind": "EC2NodeClass", "name": "default"},
                    "requirements": [
                        {"key": "karpenter.sh/capacity-type", "operator": "In",
                         "values": ["spot", "on-demand"] if not IS_PROD else ["on-demand"]},
                        {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64", "arm64"]},
                        {"key": "karpenter.k8s.aws/instance-category", "operator": "In", "values": ["c", "m", "r"]},
                        {"key": "karpenter.k8s.aws/instance-generation", "operator": "Gt", "values": ["4"]},
                    ],
                    "expireAfter": "720h",
                }},
                "limits": {"cpu": 1000, "memory": "1000Gi"},
                "disruption": {
                    "consolidationPolicy": "WhenEmptyOrUnderutilized",
                    "consolidateAfter": "30s",
                },
            },
        }
        nodeclass_manifest = {
            "apiVersion": "karpenter.k8s.aws/v1",
            "kind": "EC2NodeClass",
            "metadata": {"name": "default"},
            "spec": {
                "amiFamily": "AL2",
                "role": karpenter_role.role_name,
                "subnetSelectorTerms": [{"tags": {"kubernetes.io/role/internal-elb": "1"}}],
                "securityGroupSelectorTerms": [{"tags": {"aws:eks:cluster-name": f"{{project_name}}-{stage_name}"}}],
                "blockDeviceMappings": [{
                    "deviceName": "/dev/xvda",
                    "ebs": {
                        "volumeSize": "50Gi",
                        "volumeType": "gp3",
                        "iops": 3000,
                        "encrypted": True,
                        "kmsKeyID": cmk.key_arn,       # LOCAL key ARN — safe
                    },
                }],
                "metadataOptions": {
                    "httpEndpoint": "enabled",
                    "httpProtocolIPv6": "disabled",
                    "httpPutResponseHopLimit": 1,
                    "httpTokens": "required",
                },
                "userData": f"#!/bin/bash\n/etc/eks/bootstrap.sh {{project_name}}-{stage_name}",
            },
        }
        eks_cluster.add_manifest("KarpenterNodePool", nodepool_manifest)
        eks_cluster.add_manifest("KarpenterEC2NodeClass", nodeclass_manifest)

        # -----------------------------------------------------------------
        # C) AWS Load Balancer Controller (IRSA identity-side)
        # -----------------------------------------------------------------
        lbc_role = iam.Role(
            self, "LBCRole",
            assumed_by=iam.WebIdentityPrincipal(
                eks_cluster.open_id_connect_provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{eks_cluster.cluster_open_id_connect_issuer}:aud": "sts.amazonaws.com",
                        f"{eks_cluster.cluster_open_id_connect_issuer}:sub":
                            "system:serviceaccount:kube-system:aws-load-balancer-controller",
                    }
                },
            ),
            role_name=f"{{project_name}}-lbc-{stage_name}",
        )
        iam.PermissionsBoundary.of(lbc_role).apply(permission_boundary)
        lbc_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:*",
                "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
                "ec2:DescribeInstances", "ec2:DescribeInternetGateways",
                "cognito-idp:DescribeUserPoolClient",
                "acm:ListCertificates", "acm:DescribeCertificate",
                "wafv2:GetWebACL", "wafv2:AssociateWebACL",
                "shield:DescribeProtection", "shield:CreateProtection",
            ],
            resources=["*"],
        ))
        eks_cluster.add_helm_chart(
            "AWSLoadBalancerController",
            chart="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace="kube-system",
            release="aws-load-balancer-controller",
            values={
                "clusterName": f"{{project_name}}-{stage_name}",
                "serviceAccount": {
                    "create": True,
                    "name": "aws-load-balancer-controller",
                    "annotations": {"eks.amazonaws.com/role-arn": lbc_role.role_arn},
                },
                "replicaCount": 2,
                "enableShield": True,
                "enableWaf": True,
                "enableWafv2": True,
            },
        )

        # -----------------------------------------------------------------
        # D) External Secrets Operator (identity-side)
        # -----------------------------------------------------------------
        eso_role = iam.Role(
            self, "ESORole",
            assumed_by=iam.WebIdentityPrincipal(
                eks_cluster.open_id_connect_provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{eks_cluster.cluster_open_id_connect_issuer}:sub":
                            "system:serviceaccount:external-secrets:external-secrets",
                    }
                },
            ),
            role_name=f"{{project_name}}-eso-{stage_name}",
        )
        iam.PermissionsBoundary.of(eso_role).apply(permission_boundary)
        # Identity-side: scoped wildcard for ESO dynamic secret sync
        eso_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
                "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath",
                "kms:Decrypt",
            ],
            resources=["*"],
        ))
        eks_cluster.add_helm_chart(
            "ExternalSecretsOperator",
            chart="external-secrets",
            repository="https://charts.external-secrets.io",
            namespace="external-secrets",
            create_namespace=True,
            release="external-secrets",
            values={
                "serviceAccount": {
                    "annotations": {"eks.amazonaws.com/role-arn": eso_role.role_arn},
                },
                "replicaCount": 2,
            },
        )

        # -----------------------------------------------------------------
        # E) EBS CSI Driver (local role + LOCAL CMK)
        # -----------------------------------------------------------------
        ebs_csi_role = iam.Role(
            self, "EBSCSIRole",
            assumed_by=iam.WebIdentityPrincipal(
                eks_cluster.open_id_connect_provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{eks_cluster.cluster_open_id_connect_issuer}:sub":
                            "system:serviceaccount:kube-system:ebs-csi-controller-sa",
                    }
                },
            ),
            role_name=f"{{project_name}}-ebs-csi-{stage_name}",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEBSCSIDriverPolicy"),
            ],
        )
        iam.PermissionsBoundary.of(ebs_csi_role).apply(permission_boundary)
        # EBS CSI needs KMS for encrypted gp3 provisioning
        ebs_csi_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:CreateGrant", "kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext"],
            resources=[cmk.key_arn],
        ))
        eks_cluster.add_helm_chart(
            "EBSCSIDriver",
            chart="aws-ebs-csi-driver",
            repository="https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
            namespace="kube-system",
            release="aws-ebs-csi-driver",
            values={
                "controller": {
                    "serviceAccount": {
                        "annotations": {"eks.amazonaws.com/role-arn": ebs_csi_role.role_arn},
                    },
                },
                "storageClasses": [
                    {
                        "name": "gp3-encrypted",
                        "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                        "parameters": {
                            "type": "gp3",
                            "encrypted": "true",
                            "kmsKeyId": cmk.key_arn,
                        },
                        "reclaimPolicy": "Retain" if IS_PROD else "Delete",
                        "allowVolumeExpansion": True,
                    }
                ],
            },
        )

        # -----------------------------------------------------------------
        # Publish via SSM — workload stacks read these
        # -----------------------------------------------------------------
        for pid, pname, pval in [
            ("ClusterNameParam", f"/{{project_name}}/{stage_name}/platform/cluster_name", eks_cluster.cluster_name),
            ("ClusterArnParam",  f"/{{project_name}}/{stage_name}/platform/cluster_arn",  eks_cluster.cluster_arn),
            ("OidcIssuerParam",  f"/{{project_name}}/{stage_name}/platform/oidc_issuer",  eks_cluster.cluster_open_id_connect_issuer),
            ("OidcArnParam",     f"/{{project_name}}/{stage_name}/platform/oidc_arn",
                eks_cluster.open_id_connect_provider.open_id_connect_provider_arn),
            ("KarpenterNodeRoleArn", f"/{{project_name}}/{stage_name}/platform/karpenter_node_role_arn",
                karpenter_role.role_arn),
            ("PlatformKmsArnParam", f"/{{project_name}}/{stage_name}/platform/kms_key_arn", cmk.key_arn),
        ]:
            ssm.StringParameter(self, pid, parameter_name=pname, string_value=pval)

        CfnOutput(self, "EKSClusterName", value=eks_cluster.cluster_name)
        CfnOutput(self, "EKSClusterArn",  value=eks_cluster.cluster_arn)
        CfnOutput(self, "EKSKubectlCmd",
            value=f"aws eks update-kubeconfig --region {self.region} --name {{project_name}}-{stage_name}")
```

### 4.3 Workload consumer pattern

```python
# inside workload stack (e.g. WorkloadStack)
cluster_name = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/platform/cluster_name",
)
oidc_issuer = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/platform/oidc_issuer",
)
oidc_arn = ssm.StringParameter.value_for_string_parameter(
    self, "/{project_name}/prod/platform/oidc_arn",
)

# Build IRSA role in workload stack — trust policy uses OIDC issuer as string
workload_sa_role = iam.Role(
    self, "WorkloadSARole",
    assumed_by=iam.WebIdentityPrincipal(
        oidc_arn,
        conditions={
            "StringEquals": {
                f"{oidc_issuer}:sub": "system:serviceaccount:apps:my-workload",
            }
        },
    ),
)
# Identity-side grants on workload resources (buckets, queues) — never
# ext_bucket.grant_*(workload_sa_role).
```

### 4.4 Micro-stack gotchas

- **`eks.Cluster.from_cluster_attributes(..., cluster_name=<token>)`** in consumer stacks does not let you call `add_helm_chart` or `add_manifest` — the imported cluster is *read-only*. Keep all chart/manifest installs inside `PlatformStack`.
- **OIDC issuer string vs ARN** — `open_id_connect_provider.open_id_connect_provider_arn` is the ARN (for trust policy `Principal.Federated`). `cluster_open_id_connect_issuer` is the bare URL minus `https://` (for `StringEquals` conditions). Mixing them yields `Invalid identity token` at pod-runtime, which is silent at synth.
- **`eks.Cluster` creates a CFN custom resource + Lambda** (the kubectl provider) inside the platform stack. Cross-account `kubectl` calls require passing `kubectl_role` with `AssumeRole` on the target account — `# TODO(verify): eks.Cluster.kubectl_lambda_role cross-account assume-role pattern`.
- **Karpenter NodePool CRD install ordering** — `add_manifest` resources are installed in parallel unless explicitly chained. Use `nodepool_resource.node.add_dependency(nodeclass_resource)` if CRD install order matters, otherwise the first pod to schedule may fail until both CRDs are accepted.
- **`add_helm_chart(values={"storageClasses": [...]})`** serializes values via a Lambda custom resource. If you pass a cross-stack token inside `values`, the token is resolved at custom-resource-execute time — but the custom resource's role lives in `PlatformStack`, so grants to cross-stack resources (e.g. a cross-stack KMS decrypt) will fail silently. Always use LOCAL KMS for EBS CSI.
- **`permission_boundary` on IRSA roles** must include `sts:AssumeRoleWithWebIdentity`; a boundary that forgets this action silently breaks every IRSA trust relationship — pods get `AccessDenied` from every AWS API call without a clear log line.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| Pure Fargate on EKS (no EC2) | Replace `add_nodegroup_capacity` + Karpenter with `eks_cluster.add_fargate_profile(...)` — drop Karpenter node role + NodePool manifests |
| AWS Solutions blueprint pattern | Do NOT use `eks-blueprints` module (deprecated); compose from L2 + Helm as in §3 / §4 |
| Service mesh (App Mesh retired; Istio) | Add `eks_cluster.add_helm_chart("Istio", chart="istiod", repository="https://istio-release.storage.googleapis.com/charts", ...)` after LBC |
| GitOps (ArgoCD) | Add `eks_cluster.add_helm_chart("ArgoCD", ..., namespace="argocd", create_namespace=True)` + seed Application CR via `add_manifest` |
| Single-team POC, cost-sensitive | Switch to ECS Fargate — see `LAYER_BACKEND_ECS` |
| Multi-tenant platform with workload isolation | Keep Micro-Stack; workload stacks own their own IRSA roles + namespaces + NetworkPolicies |
| Karpenter v1beta1 required | Revert manifests to `karpenter.sh/v1beta1` + `karpenter.k8s.aws/v1beta1`; pin Helm chart ≤ v0.32 |

---

## 6. Worked example

Save as `tests/sop/test_PLATFORM_EKS_CLUSTER.py`. Offline — no AWS credentials needed.

```python
"""SOP verification — PlatformStack synthesizes without cross-stack KMS cycle."""
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
)
from aws_cdk.assertions import Template, Match


def _env():
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_platform_stack_synthesizes():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=2)
    boundary = iam.ManagedPolicy(deps, "Boundary",
        statements=[iam.PolicyStatement(
            actions=["sts:AssumeRoleWithWebIdentity", "*"], resources=["*"],
        )])

    from infrastructure.cdk.stacks.platform_stack import PlatformStack
    p = PlatformStack(
        app, stage_name="prod", vpc=vpc,
        permission_boundary=boundary, env=env,
    )

    t = Template.from_stack(p)
    # Cluster + 1 local CMK + 1 managed node group
    t.resource_count_is("AWS::EKS::Cluster", 1)
    t.resource_count_is("AWS::KMS::Key", 1)
    t.resource_count_is("AWS::EKS::Nodegroup", 1)
    # Cluster version pinned
    t.has_resource_properties("AWS::EKS::Cluster", Match.object_like({
        "Version": "1.31",
    }))
    # SSM publications
    t.resource_count_is("AWS::SSM::Parameter", 6)
```

---

## 7. References

- `docs/template_params.md` — `EKS_CLUSTER_NAME_SSM`, `EKS_OIDC_ISSUER_SSM`, `EKS_OIDC_ARN_SSM`, `EKS_KARPENTER_NODE_ROLE_ARN_SSM`, `EKS_PLATFORM_KMS_ARN_SSM`, `K8S_VERSION`, `STAGE_NAME`
- `docs/Feature_Roadmap.md` — feature IDs `EKS-01..EKS-12` (cluster), `EKS-13..EKS-18` (addons), `EKS-19..EKS-24` (Karpenter)
- AWS EKS CloudFormation: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/AWS_EKS.html
- AWS Load Balancer Controller: https://kubernetes-sigs.github.io/aws-load-balancer-controller/
- Karpenter: https://karpenter.sh/
- External Secrets Operator: https://external-secrets.io/
- AWS EBS CSI Driver: https://github.com/kubernetes-sigs/aws-ebs-csi-driver
- Related SOPs: `LAYER_NETWORKING` (VPC + private subnets), `LAYER_SECURITY` (permission boundary, KMS), `LAYER_BACKEND_ECS` (when to pick Fargate instead), `OPS_ADVANCED_MONITORING` (control-plane log alarms), `SECURITY_WAF_SHIELD_MACIE` (LBC → WAFv2 wiring)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section dual-variant SOP. Added Micro-Stack variant (§4) — `PlatformStack` owns LOCAL CMK (honors 5th non-negotiable — EKS `secrets_encryption_key` never cross-stack), EKS L2 `Cluster` v1.31, system managed node group, Karpenter node role + NodePool/EC2NodeClass manifests, LBC + ESO + EBS CSI via `add_helm_chart` with IRSA trust policies built from `cluster_open_id_connect_issuer` string + `open_id_connect_provider_arn`, permission boundary on every IAM role. Publishes cluster name, cluster ARN, OIDC issuer, OIDC ARN, Karpenter node role ARN, platform KMS ARN via SSM. Added workload consumer pattern (§4.3) showing IRSA role construction from SSM-resolved OIDC issuer. Added `TODO(verify)` on `KubernetesVersion.V1_31` and `eks.Cluster.kubectl_lambda_role` cross-account assume-role. Added Swap matrix (§5), Worked example (§6), Monolith gotchas (§3.3), Micro-stack gotchas (§4.4). Preserved all v1.0 content: EKS-vs-ECS decision, L2 `eks.Cluster` with private endpoint + KMS encryption + full CloudWatch logging, system managed node group with taints, Karpenter NodePool v1 + EC2NodeClass with IMDSv2, LBC Helm with Shield+WAFv2+ACM, ESO Helm, EBS CSI gp3-encrypted default StorageClass. |
| 1.0 | 2026-03-05 | Initial — EKS L2 `Cluster` v1.31 + system node group + Karpenter v1 + LBC + ESO + EBS CSI. |
