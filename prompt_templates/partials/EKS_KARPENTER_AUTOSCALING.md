# SOP — Karpenter v1.x autoscaling on EKS (NodePools · NodeClasses · consolidation · spot mix)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Karpenter v1.0+ (GA Aug 2024) · NodePools + EC2NodeClasses + NodeClaims · spot + on-demand mix · consolidation policies (`WhenUnderutilized`, `WhenEmpty`) · disruption budgets · Pod Identity for Karpenter controller

---

## 1. Purpose

- Codify the **Karpenter v1.x pattern** — the modern dynamic node provisioner that replaces Cluster Autoscaler. Karpenter watches unschedulable pods and provisions just-in-time nodes matching pod requirements (instance type, AZ, architecture, capacity type).
- Codify the **NodePool / EC2NodeClass / NodeClaim** v1 API (replaces v0.32's Provisioner / AWSNodeTemplate / Machine).
- Codify the **consolidation policies** that aggressively right-size: `WhenUnderutilized` (terminates underused nodes), `WhenEmpty` (terminates idle nodes), `whenEmptyOrUnderutilized` (the default, V1.0+).
- Codify the **spot+on-demand mix** with `karpenter.sh/capacity-type` label + price-capacity-optimized strategy.
- Codify the **disruption budgets** preventing over-aggressive consolidation in business hours.
- This is the **dynamic-autoscaling specialisation**. Built on `EKS_CLUSTER_FOUNDATION`. Replaces Cluster Autoscaler entirely (which is still supported but lacks Karpenter's instance flexibility + consolidation).

When the SOW signals: "fast pod provisioning", "spot at scale", "cost optimization", "burst workloads", "Karpenter migration from Cluster Autoscaler".

---

## 2. Decision tree — Karpenter vs Cluster Autoscaler

| Need | Karpenter | Cluster Autoscaler |
|---|---|---|
| Fast pod scheduling (< 60s) | ✅ best (~30-45s) | ⚠️ ~2-3 min (waits for ASG scale-out) |
| Many instance type options | ✅ explores ~600 types per pod | ❌ pre-defined ASGs only |
| Spot + on-demand mix per pod | ✅ native | ⚠️ requires multiple ASGs |
| Aggressive right-sizing | ✅ consolidation feature | ❌ only scale-down on idle |
| Stateful workloads (EBS) | ✅ topology-aware | ✅ topology-aware |
| Established cluster, low-churn | Either | ⚠️ simpler if no need to scale fast |

**Recommendation: Karpenter for new clusters. Migrate existing Cluster Autoscaler clusters opportunistically.**

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Karpenter + 1 NodePool + 1 EC2NodeClass | **§3 Monolith Variant** |
| Production — multiple NodePools (spot vs on-demand vs gpu) per workload | **§4 Multi-pool Variant** |

---

## 3. Monolith Variant — Karpenter + general-purpose NodePool

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  EKS Cluster (from EKS_CLUSTER_FOUNDATION)                       │
   │     - Baseline managed node group: 3× m6i.large (always-on)       │
   │     - Karpenter controller pods scheduled to baseline pool        │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Karpenter v1.x controller (Helm-installed)                       │
   │     - Pod Identity association → KarpenterControllerRole          │
   │     - Watches unschedulable pods                                   │
   │     - Provisions NodeClaims matching pod requirements              │
   │     - Consolidates underutilized nodes                              │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  EC2NodeClass: default                                            │
   │     - AMI family: AL2023                                          │
   │     - Subnet selector: tag karpenter.sh/discovery=qra-prod        │
   │     - SG selector: tag karpenter.sh/discovery=qra-prod            │
   │     - IAM role: KarpenterNodeRole (with SSM, ECR, CNI policies)   │
   │     - Block device mappings: 50GB gp3                              │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  NodePool: general-purpose                                        │
   │     - Requirements:                                                │
   │         karpenter.k8s.aws/instance-family: [m, c, r]               │
   │         karpenter.k8s.aws/instance-size: [large, xlarge, 2xlarge] │
   │         kubernetes.io/arch: [arm64]                                │
   │         karpenter.sh/capacity-type: [spot, on-demand]              │
   │     - Limits: cpu: 1000, memory: 1000Gi                           │
   │     - Disruption: consolidationPolicy=WhenEmptyOrUnderutilized    │
   │                   consolidateAfter=30s                              │
   │                   expireAfter=720h (30 days)                        │
   └──────────────────────────────────────────────────────────────────┘
                    │
                    ▼
   Provisioned NodeClaim → EC2 instance launched → kubelet joins cluster → pod scheduled
```

### 3.2 CDK — Karpenter controller infrastructure

```python
from aws_cdk import (
    aws_iam as iam,
    aws_eks as eks,
    aws_ec2 as ec2,
    aws_sqs as sqs,
)


def _create_karpenter_infra(self, stage: str) -> None:
    """Provisions Karpenter controller + node IAM + interruption SQS.
    Helm install + NodePool/EC2NodeClass YAML applied separately (kubectl/ArgoCD)."""

    # A) Karpenter node IAM role — what's attached to provisioned EC2 instances
    self.karpenter_node_role = iam.Role(self, "KarpenterNodeRole",
        role_name=f"KarpenterNodeRole-{{project_name}}-{stage}",
        assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
        ],
        permissions_boundary=self.permission_boundary,
    )
    iam.CfnInstanceProfile(self, "KarpenterNodeInstanceProfile",
        instance_profile_name=f"KarpenterNodeInstanceProfile-{{project_name}}-{stage}",
        roles=[self.karpenter_node_role.role_name],
    )

    # B) EKS access entry for Karpenter node role (replaces aws-auth entry)
    eks.CfnAccessEntry(self, "KarpenterNodeAccessEntry",
        cluster_name=self.eks_cluster.name,
        principal_arn=self.karpenter_node_role.role_arn,
        type="EC2_LINUX",                              # NEW type for Karpenter nodes
    )

    # C) Karpenter controller IAM role (used via Pod Identity Association)
    self.karpenter_controller_role = iam.Role(self, "KarpenterControllerRole",
        role_name=f"KarpenterControllerRole-{{project_name}}-{stage}",
        assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.karpenter_controller_role.add_to_policy(iam.PolicyStatement(
        actions=[
            # AllowScopedEC2InstanceAccessActions
            "ec2:RunInstances",
            "ec2:CreateFleet",
            "ec2:CreateLaunchTemplate",
            "ec2:CreateTags",
            "ec2:TerminateInstances",
            "ec2:DescribeInstances",
            "ec2:DescribeImages",
            "ec2:DescribeInstanceTypes",
            "ec2:DescribeInstanceTypeOfferings",
            "ec2:DescribeAvailabilityZones",
            "ec2:DescribeSpotPriceHistory",
            "ec2:DescribeSubnets",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeLaunchTemplates",
        ],
        resources=["*"],
    ))
    self.karpenter_controller_role.add_to_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.karpenter_node_role.role_arn],
        conditions={"StringEquals": {
            "iam:PassedToService": "ec2.amazonaws.com",
        }},
    ))
    self.karpenter_controller_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "eks:DescribeCluster",
            "ssm:GetParameter",                        # for AMI lookup
        ],
        resources=["*"],
    ))

    # D) Pod Identity Association — Karpenter controller pod → controller role
    eks.CfnPodIdentityAssociation(self, "KarpenterPIA",
        cluster_name=self.eks_cluster.name,
        namespace="kube-system",
        service_account="karpenter",
        role_arn=self.karpenter_controller_role.role_arn,
    )

    # E) SQS interruption queue (Karpenter listens for spot interruption events)
    self.karpenter_interruption_queue = sqs.Queue(self, "KarpenterInterruption",
        queue_name=f"karpenter-interruption-{{project_name}}-{stage}",
        retention_period=Duration.days(1),
        message_retention_period=Duration.minutes(5),
    )
    self.karpenter_interruption_queue.grant_consume_messages(self.karpenter_controller_role)

    # EventBridge rules → SQS for spot interruption + scheduled events
    for rule_name, event_pattern in [
        ("ScheduledChange", {
            "source": ["aws.health"],
            "detail-type": ["AWS Health Event"],
        }),
        ("SpotInterruption", {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Spot Instance Interruption Warning"],
        }),
        ("Rebalance", {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Instance Rebalance Recommendation"],
        }),
        ("InstanceStateChange", {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Instance State-change Notification"],
        }),
    ]:
        events.Rule(self, f"Karp{rule_name}",
            event_pattern=events.EventPattern(**event_pattern),
            targets=[targets.SqsQueue(self.karpenter_interruption_queue)],
        )

    # F) Tag VPC subnets and SGs for Karpenter discovery
    # Apply tags via CLI/script post-deploy:
    #   aws ec2 create-tags --resources subnet-xxx --tags Key=karpenter.sh/discovery,Value={cluster_name}
    #   aws ec2 create-tags --resources sg-xxx     --tags Key=karpenter.sh/discovery,Value={cluster_name}

    CfnOutput(self, "KarpenterNodeRole",       value=self.karpenter_node_role.role_arn)
    CfnOutput(self, "KarpenterControllerRole", value=self.karpenter_controller_role.role_arn)
    CfnOutput(self, "KarpenterInterruptionQueue", value=self.karpenter_interruption_queue.queue_url)
```

### 3.3 Karpenter Helm install (post-CDK)

```bash
# Install Karpenter via Helm (after CDK deploys infra)
helm registry logout public.ecr.aws

helm install karpenter \
  oci://public.ecr.aws/karpenter/karpenter \
  --version 1.0.0 \
  --namespace kube-system \
  --set settings.clusterName=qra-prod \
  --set settings.interruptionQueue=karpenter-interruption-qra-prod \
  --set controller.resources.requests.cpu=1 \
  --set controller.resources.requests.memory=1Gi \
  --set controller.resources.limits.cpu=1 \
  --set controller.resources.limits.memory=1Gi \
  --wait
```

### 3.4 NodePool + EC2NodeClass YAML (apply via kubectl OR ArgoCD)

`karpenter/ec2nodeclass-default.yaml`:

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023
  amiSelectorTerms:
    - alias: al2023@latest
  role: "KarpenterNodeRole-qra-prod"               # IAM role from CDK
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "qra-prod"          # tag set on private subnets
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "qra-prod"          # tag set on cluster SG
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 50Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        encrypted: true
        kmsKeyID: arn:aws:kms:us-east-1:111111111111:key/...
        deleteOnTermination: true
  metadataOptions:
    httpEndpoint: enabled
    httpProtocolIPv6: disabled
    httpPutResponseHopLimit: 2
    httpTokens: required                            # IMDSv2 mandatory
  tags:
    Environment: prod
    ManagedBy: karpenter
```

`karpenter/nodepool-general.yaml`:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general-purpose
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["m", "c", "r"]                   # general-purpose families
        - key: karpenter.k8s.aws/instance-size
          operator: In
          values: ["large", "xlarge", "2xlarge", "4xlarge"]
        - key: kubernetes.io/arch
          operator: In
          values: ["arm64"]                         # ARM64 ~20% cheaper
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
      taints: []                                    # general workloads
      expireAfter: 720h                             # rotate nodes after 30 days

  limits:
    cpu: "1000"                                     # cluster-wide cap
    memory: 1000Gi

  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized   # v1 default
    consolidateAfter: 30s                            # wait 30s before consolidating

    # Disruption budgets — protect business hours
    budgets:
      - nodes: "10%"                                # only allow 10% of nodes to be disrupted simultaneously
      - nodes: "0"
        schedule: "0 9 * * mon-fri"                # no disruption 9am-5pm weekdays
        duration: 8h
        reasons: ["Underutilized", "Empty"]
```

### 3.5 GPU NodePool (separate NodeClass + NodePool)

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu
spec:
  amiFamily: Bottlerocket
  amiSelectorTerms:
    - alias: bottlerocket-nvidia@latest
  role: "KarpenterNodeRole-qra-prod"
  subnetSelectorTerms:
    - tags: {karpenter.sh/discovery: "qra-prod"}
  securityGroupSelectorTerms:
    - tags: {karpenter.sh/discovery: "qra-prod"}

---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: ["g5.xlarge", "g5.2xlarge", "g5.4xlarge", "g6.xlarge"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]                      # GPU spot is unstable
      taints:
        - key: nvidia.com/gpu
          value: "true"
          effect: NoSchedule                         # only GPU workloads
  limits:
    cpu: "200"
    memory: 800Gi
  disruption:
    consolidationPolicy: WhenEmpty                   # don't consolidate active GPU work
```

---

## 4. Multi-pool variant — production prod cluster

For real-world prod, typically 3-5 NodePools:

| Pool | When | Disruption |
|---|---|---|
| `baseline-mng` | always-on managed node group (Karpenter controller, daemonsets) | manual only |
| `general-spot` | bulk stateless workloads | aggressive consolidation |
| `general-od` | latency-critical / DB-backed services | conservative consolidation, business-hour blackout |
| `gpu-od` | ML inference / training | WhenEmpty only |
| `system-arm-od` | system services (CoreDNS, kube-proxy mirrors) | manual only |

---

## 5. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Karpenter controller can't launch nodes | Subnets/SGs not tagged | Apply `karpenter.sh/discovery: <cluster-name>` to subnets + SG |
| Pods stuck Pending despite Karpenter | NodePool requirements don't match pod | Check pod's nodeAffinity / tolerations against NodePool requirements |
| Spot interruptions kill pods | No PodDisruptionBudget | Add PDB with `minAvailable` / `maxUnavailable` per workload |
| Consolidation triggers too aggressively | Default `consolidateAfter=0s` in old configs | Set `30s` minimum; use disruption budgets for business hours |
| GPU nodes provisioned but pods don't schedule | NVIDIA device plugin DaemonSet not installed | Helm install nvidia-device-plugin in kube-system |
| Karpenter ignores spot interruption events | SQS queue ARN wrong in Helm install | Verify `settings.interruptionQueue` matches CDK output |
| New nodes don't pull images | KarpenterNodeRole missing ECR perms | Add `AmazonEC2ContainerRegistryReadOnly` |
| Cost higher than expected | Consolidation disabled or NodePool limits too high | Lower limits + verify `consolidationPolicy=WhenEmptyOrUnderutilized` |

---

## 6. Cost ballpark vs Cluster Autoscaler

| Cluster shape | Cluster Autoscaler | Karpenter | Savings |
|---|---|---|---|
| 100-pod prod, all on-demand | $5K/mo | $4.5K/mo | 10% (consolidation) |
| 100-pod prod, 70% spot | $5K/mo | $2.5K/mo | 50% (better spot management + consolidation) |
| 1000-pod variable workload | $40K/mo | $25K/mo | 38% |
| GPU inference cluster | $30K/mo | $22K/mo | 27% (no spot) |

Karpenter savings come from: (a) instance type flexibility (cheapest fitting type), (b) aggressive consolidation, (c) better spot diversification.

---

## 7. Five non-negotiables

1. **Pod Identity Association for Karpenter controller (NOT IRSA).** Pod Identity is newer (Nov 2023), simpler IAM, no annotation cruft. Reserve IRSA for pre-Pod-Identity legacy.

2. **`consolidateAfter ≥ 30s`.** Setting `0s` causes flapping (provision → consolidate → provision) — burns money on EC2 launch overhead.

3. **Disruption budgets for business hours.** Without them, Karpenter can rotate 50% of your fleet during peak traffic. Set `nodes: 0` schedule for 9am-5pm Mon-Fri at minimum.

4. **IMDSv2 mandatory.** `metadataOptions.httpTokens: required` in EC2NodeClass. Prevents SSRF + token abuse.

5. **`expireAfter: 720h` (30 days).** Forces node rotation monthly — picks up AMI patches automatically. Without it, nodes can run for years missing CVEs.

---

## 8. References

- AWS docs:
  - [Karpenter on EKS](https://karpenter.sh/)
  - [Karpenter v1.0 release](https://karpenter.sh/v1.0/)
  - [Karpenter NodePool API](https://karpenter.sh/docs/concepts/nodepools/)
  - [EC2NodeClass API](https://karpenter.sh/docs/concepts/nodeclasses/)
  - [Migration from v0.32 to v1](https://karpenter.sh/docs/upgrading/v1-migration/)
- Related SOPs:
  - `EKS_CLUSTER_FOUNDATION` — required base cluster
  - `EKS_POD_IDENTITY` — Pod Identity for Karpenter controller
  - `EKS_NETWORKING` — subnet tags for Karpenter discovery
  - `EKS_COST_OPTIMIZATION` — consolidation tuning + Compute Optimizer
  - `EKS_SECURITY` — IMDSv2 + Network Policies

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Karpenter v1.0+ NodePool/EC2NodeClass. CDK for IAM + SQS interruption + Pod Identity Association. Helm install commands. Multi-pool prod patterns (general spot/od + gpu + system). 5 non-negotiables. Created Wave 9 (2026-04-26). |
