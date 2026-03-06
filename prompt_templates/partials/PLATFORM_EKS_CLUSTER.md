# PARTIAL: EKS Container Platform — Kubernetes with Karpenter, GitOps, Service Mesh

**Usage:** Include when SOW mentions Kubernetes, EKS, K8s, container orchestration, microservices platform, GitOps, ArgoCD, or multi-tenant container workloads.

---

## EKS vs ECS Decision

| Factor                         | Use EKS                    | Use ECS Fargate |
| ------------------------------ | -------------------------- | --------------- |
| Team has K8s expertise         | ✅                         | ❌              |
| Multi-cloud portability needed | ✅                         | ❌              |
| Helm chart ecosystem needed    | ✅                         | ❌              |
| Service mesh (Istio) needed    | ✅                         | ❌              |
| Simplicity is priority         | ❌                         | ✅              |
| Cost at small scale            | ❌ (EKS = +$73/mo/cluster) | ✅              |

---

## CDK Code Block — EKS Cluster

```python
def _create_eks_platform(self, stage_name: str) -> None:
    """
    Amazon EKS Cluster — Production-Ready Kubernetes Platform.

    Components:
      A) EKS Cluster with managed node groups + Fargate profile
      B) Karpenter node autoscaler (replacement for Cluster Autoscaler)
      C) AWS Load Balancer Controller (ALB Ingress)
      D) External Secrets Operator (sync Secrets Manager → K8s Secrets)
      E) EBS CSI Driver (persistent storage)
      F) Amazon VPC CNI (pod networking)
      G) Pod Identity (new in 2024 — replaces IRSA)
    """

    import aws_cdk.aws_eks as eks
    import aws_cdk.aws_ec2 as ec2

    # =========================================================================
    # A) EKS CLUSTER
    # =========================================================================

    eks_cluster = eks.Cluster(
        self, "EKSCluster",
        cluster_name=f"{{project_name}}-{stage_name}",
        version=eks.KubernetesVersion.V1_31,  # Latest stable
        cluster_logging=[
            eks.ClusterLoggingTypes.API,
            eks.ClusterLoggingTypes.AUTHENTICATOR,
            eks.ClusterLoggingTypes.SCHEDULER,
            eks.ClusterLoggingTypes.CONTROLLER_MANAGER,
            eks.ClusterLoggingTypes.AUDIT,
        ],

        # VPC Configuration (private endpoint — no public K8s API)
        vpc=self.vpc,
        vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],

        # Endpoint access: private only for prod, public+private for dev
        endpoint_access=eks.EndpointAccess.PRIVATE if stage_name == "prod" else eks.EndpointAccess.PUBLIC_AND_PRIVATE,

        # Secrets encryption with KMS
        secrets_encryption_key=self.kms_key,

        # Default capacity: let Karpenter manage, start with minimal managed nodes
        default_capacity=0,  # We'll add node groups separately

        # kubectl Lambda role
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
    # MANAGED NODE GROUPS
    # =========================================================================

    # System node group — for Karpenter, CoreDNS, VPC CNI, LBC
    system_ng = eks_cluster.add_nodegroup_capacity(
        "SystemNodeGroup",
        nodegroup_name=f"{{project_name}}-system-{stage_name}",
        instance_types=[
            ec2.InstanceType("m5.large"),   # 2 vCPU, 8 GB — sufficient for system pods
        ],
        min_size=2,   # Must have 2 minimum for HA (spread across AZs)
        max_size=4,
        desired_size=2,
        capacity_type=eks.CapacityType.ON_DEMAND,  # On-demand for system — stability
        ami_type=eks.NodegroupAmiType.AL2_X86_64,  # Amazon Linux 2
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

    # Karpenter NodePool CRD (deployed via Helm)
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
                         "values": ["c", "m", "r"]},  # Compute, Memory, RAM optimized
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
    # Never hard-code secrets in K8s YAML!
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
