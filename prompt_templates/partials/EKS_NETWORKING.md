# SOP — EKS Networking (VPC CNI · prefix delegation · AWS Load Balancer Controller · ALB/NLB Ingress · ExternalDNS)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · Amazon VPC CNI v1.18+ · prefix delegation (16 IPs/ENI vs 1) · AWS Load Balancer Controller v2.8+ · ALB Ingress (HTTP/HTTPS) · NLB Service (TCP/UDP/TLS) · ExternalDNS v0.14+ · Custom Networking for IP exhaustion

---

## 1. Purpose

- Codify the **VPC CNI plugin** that gives every pod a routable VPC IP. Cover prefix delegation (the #1 lever against IP exhaustion), Custom Networking (separate pod CIDR), and Security Groups for Pods.
- Codify the **AWS Load Balancer Controller (LBC)** — the CNCF-grade Kubernetes-aware ALB/NLB provisioner. Replaces in-tree `Service type=LoadBalancer` with `aws-load-balancer-controller`.
- Codify **Ingress patterns** — IP-mode ALB (preferred), instance-mode (legacy), shared ALBs across namespaces (`alb.ingress.kubernetes.io/group.name`).
- Codify the **NLB pattern** for TCP/UDP/gRPC traffic and the new (2024) Gateway API support.
- Codify **ExternalDNS** for automatic Route 53 record creation.
- Codify **IP exhaustion mitigation** — secondary CIDR + Custom Networking for the rare cluster that exceeds /16 capacity.
- This is the **networking specialisation**. Built on `EKS_CLUSTER_FOUNDATION` + `EKS_POD_IDENTITY`. Required by `EKS_OBSERVABILITY` (ALB ingress for Grafana), `EKS_GITOPS` (ArgoCD ingress).

When the SOW signals: "external traffic to pods", "HTTPS termination", "WAF in front of ingress", "gRPC/TCP services", "DNS for services", "running out of pod IPs".

---

## 2. Decision tree — Ingress vs Service vs Gateway API

```
External traffic type?
├── HTTP/HTTPS (web, REST, GraphQL) → §3 ALB Ingress (IP mode)
├── TCP / UDP (databases, gaming, MQTT) → §4 NLB Service (IP mode)
├── gRPC → §3 ALB Ingress (HTTP/2 supported) OR §4 NLB
├── TLS passthrough (no AWS-side termination) → §4 NLB
└── Multi-protocol per route → §5 Gateway API + LBC (2024)

ALB target type?
├── EKS-only cluster (no other workloads) → IP mode (default — pod IPs as targets)
├── Mixed EC2 + EKS → instance mode (NodePort + ASG target)
└── ALB shared across namespaces (cost saving) → IP mode + group.name annotation

Pod IP supply?
├── < 5,000 pods → default VPC CNI (IPs from VPC subnet)
├── 5,000-50,000 pods → §6 prefix delegation (16x density per ENI)
└── > 50,000 pods OR overlapping CIDRs → §7 Custom Networking (secondary CIDR for pods)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 1 ALB Ingress + 1 ExternalDNS in single stack | **§3 Monolith Variant** |
| Production — LBC + ExternalDNS in `PlatformStack`; per-app Ingress in app stacks | **§9 Micro-Stack Variant** |

---

## 3. ALB Ingress (IP mode) — preferred for HTTP/HTTPS

### 3.1 Architecture

```
   Internet
       │
       ▼
   ┌─────────────────────────────────┐
   │  Route 53: app.example.com → ALB│  ← created by ExternalDNS
   └────────────────┬────────────────┘
                    │
                    ▼
   ┌─────────────────────────────────┐
   │  ACM cert (us-east-1 if CF)     │
   │  WAF v2 (optional, attached)    │
   │  ALB (provisioned by LBC)       │
   │     - Listener :443 HTTPS       │
   │     - Listener :80 redirect→443 │
   └────────────────┬────────────────┘
                    │  IP target group (pod IPs directly)
                    ▼
   ┌─────────────────────────────────┐
   │  Pods (any node, any AZ)        │
   │  pod-IP:8080 reachable from ALB │
   └─────────────────────────────────┘
```

### 3.2 Install AWS Load Balancer Controller (CDK + Helm)

```python
# stacks/lbc_stack.py
from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks
from constructs import Construct


class LbcStack(Stack):
    """Installs AWS Load Balancer Controller via Helm; uses Pod Identity."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_name: str,
        cluster: eks.ICluster,    # imported via Cluster.from_cluster_attributes
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. IAM role for the controller ─────────────────────────────
        # Policy is the official LBC IAM policy (~150 actions across EC2/ELBv2/ACM/WAF/Shield)
        # Reference: https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.8.0/docs/install/iam_policy.json
        lbc_role = iam.Role(
            self, "LbcRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
        )
        lbc_role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))
        # Attach inline policy (load JSON from file, or define key statements)
        lbc_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "iam:CreateServiceLinkedRole",
                "ec2:DescribeAccountAttributes", "ec2:DescribeAddresses",
                "ec2:DescribeAvailabilityZones", "ec2:DescribeInternetGateways",
                "ec2:DescribeVpcs", "ec2:DescribeVpcPeeringConnections",
                "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
                "ec2:DescribeInstances", "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeTags", "ec2:GetCoipPoolUsage",
                "ec2:DescribeCoipPools", "ec2:GetSecurityGroupsForVpc",
                "elasticloadbalancing:DescribeLoadBalancers",
                "elasticloadbalancing:DescribeLoadBalancerAttributes",
                "elasticloadbalancing:DescribeListeners",
                "elasticloadbalancing:DescribeListenerCertificates",
                "elasticloadbalancing:DescribeSSLPolicies",
                "elasticloadbalancing:DescribeRules",
                "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:DescribeTargetGroupAttributes",
                "elasticloadbalancing:DescribeTargetHealth",
                "elasticloadbalancing:DescribeTags",
                "elasticloadbalancing:DescribeTrustStores",
            ],
            resources=["*"],
        ))
        lbc_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:CreateLoadBalancer",
                "elasticloadbalancing:CreateTargetGroup",
                "elasticloadbalancing:CreateListener",
                "elasticloadbalancing:DeleteLoadBalancer",
                "elasticloadbalancing:DeleteTargetGroup",
                "elasticloadbalancing:DeleteListener",
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:DeleteRule",
                "elasticloadbalancing:RegisterTargets",
                "elasticloadbalancing:DeregisterTargets",
                "elasticloadbalancing:ModifyListener",
                "elasticloadbalancing:ModifyLoadBalancerAttributes",
                "elasticloadbalancing:ModifyTargetGroup",
                "elasticloadbalancing:ModifyTargetGroupAttributes",
                "elasticloadbalancing:ModifyRule",
                "elasticloadbalancing:AddTags",
                "elasticloadbalancing:RemoveTags",
                "elasticloadbalancing:SetIpAddressType",
                "elasticloadbalancing:SetSecurityGroups",
                "elasticloadbalancing:SetSubnets",
                "elasticloadbalancing:SetWebAcl",
                "elasticloadbalancing:AddListenerCertificates",
                "elasticloadbalancing:RemoveListenerCertificates",
            ],
            resources=["*"],
        ))
        lbc_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["acm:ListCertificates", "acm:DescribeCertificate",
                     "wafv2:GetWebACL", "wafv2:AssociateWebACL",
                     "wafv2:DisassociateWebACL", "wafv2:GetWebACLForResource",
                     "shield:GetSubscriptionState",
                     "shield:DescribeProtection",
                     "shield:CreateProtection", "shield:DeleteProtection"],
            resources=["*"],
        ))

        # ── 2. Pod Identity Association ────────────────────────────────
        eks.CfnPodIdentityAssociation(
            self, "LbcAssoc",
            cluster_name=cluster_name,
            namespace="kube-system",
            service_account="aws-load-balancer-controller",
            role_arn=lbc_role.role_arn,
        )

        # ── 3. Helm chart install ──────────────────────────────────────
        cluster.add_helm_chart(
            "AwsLoadBalancerController",
            chart="aws-load-balancer-controller",
            release="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace="kube-system",
            version="1.8.1",  # → app version 2.8.1
            values={
                "clusterName": cluster_name,
                "serviceAccount": {
                    "create": True,
                    "name": "aws-load-balancer-controller",
                    # NO IRSA annotation — Pod Identity handles it.
                },
                "region": self.region,
                "vpcId": cluster.vpc.vpc_id,
                # Recommended HA settings
                "replicaCount": 2,
                "podDisruptionBudget": {"maxUnavailable": 1},
                "topologySpreadConstraints": [{
                    "maxSkew": 1,
                    "topologyKey": "topology.kubernetes.io/zone",
                    "whenUnsatisfiable": "ScheduleAnyway",
                    "labelSelector": {"matchLabels": {
                        "app.kubernetes.io/name": "aws-load-balancer-controller",
                    }},
                }],
            },
        )
```

### 3.3 ALB Ingress YAML

```yaml
# manifests/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app-ingress
  namespace: prod-app
  annotations:
    # Provisioner
    kubernetes.io/ingress.class: alb
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip                  # IP mode (preferred)
    alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=60,routing.http2.enabled=true
    # TLS
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
    alb.ingress.kubernetes.io/ssl-redirect: '443'
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:us-east-1:123456789012:certificate/xxxx
    alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-2021-06
    # WAF (optional)
    alb.ingress.kubernetes.io/wafv2-acl-arn: arn:aws:wafv2:us-east-1:123456789012:regional/webacl/prod-acl/xxx
    # ExternalDNS hint
    external-dns.alpha.kubernetes.io/hostname: app.example.com
    # Cost saving: share one ALB across all ingresses with same group.name
    alb.ingress.kubernetes.io/group.name: prod-shared
    alb.ingress.kubernetes.io/group.order: '10'
    # Health check
    alb.ingress.kubernetes.io/healthcheck-path: /healthz
    alb.ingress.kubernetes.io/healthcheck-interval-seconds: '15'
    alb.ingress.kubernetes.io/healthy-threshold-count: '2'
    alb.ingress.kubernetes.io/unhealthy-threshold-count: '3'
    # Subnets — required if VPC has untagged subnets
    alb.ingress.kubernetes.io/subnets: subnet-aaa,subnet-bbb,subnet-ccc
    # Security groups (optional — LBC creates managed SG by default)
    alb.ingress.kubernetes.io/security-groups: sg-managed-by-platform
    alb.ingress.kubernetes.io/manage-backend-security-group-rules: 'true'
spec:
  ingressClassName: alb
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: app-svc
                port:
                  number: 80
---
apiVersion: v1
kind: Service
metadata:
  name: app-svc
  namespace: prod-app
spec:
  type: ClusterIP   # IP mode targets pods directly; ClusterIP is sufficient
  selector: { app: app }
  ports:
    - port: 80
      targetPort: 8080
      protocol: TCP
```

### 3.4 Subnet tagging (required for LBC discovery)

```python
# In NetworkStack: tag public subnets for ALB, private for internal LB
from aws_cdk import aws_ec2 as ec2, Tags

vpc = ec2.Vpc(self, "Vpc",
    subnet_configuration=[
        ec2.SubnetConfiguration(
            name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24,
        ),
        ec2.SubnetConfiguration(
            name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22,
        ),
    ],
)

# Tag for LBC discovery
for subnet in vpc.public_subnets:
    Tags.of(subnet).add("kubernetes.io/role/elb", "1")
    Tags.of(subnet).add(f"kubernetes.io/cluster/{cluster_name}", "shared")

for subnet in vpc.private_subnets:
    Tags.of(subnet).add("kubernetes.io/role/internal-elb", "1")
    Tags.of(subnet).add(f"kubernetes.io/cluster/{cluster_name}", "shared")
```

---

## 4. NLB Service (IP mode) — TCP/UDP/TLS

```yaml
# manifests/nlb-service.yaml — gRPC service exposed via NLB
apiVersion: v1
kind: Service
metadata:
  name: grpc-svc
  namespace: prod-app
  annotations:
    # Provisioner — use new annotation (k8s 1.22+) not deprecated cloud provider
    service.beta.kubernetes.io/aws-load-balancer-type: external
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
    service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing
    service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled: 'true'
    # TLS termination at NLB (alternative: passthrough)
    service.beta.kubernetes.io/aws-load-balancer-ssl-cert: arn:aws:acm:us-east-1:...:certificate/xxx
    service.beta.kubernetes.io/aws-load-balancer-ssl-ports: '443'
    service.beta.kubernetes.io/aws-load-balancer-ssl-negotiation-policy: ELBSecurityPolicy-TLS13-1-2-2021-06
    # Health check
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol: HTTP
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-path: /healthz
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-port: '8080'
    # ExternalDNS
    external-dns.alpha.kubernetes.io/hostname: grpc.example.com
spec:
  type: LoadBalancer
  loadBalancerClass: service.k8s.aws/nlb   # explicit — disables in-tree controller
  selector: { app: grpc-svc }
  ports:
    - name: grpc
      port: 443
      targetPort: 50051
      protocol: TCP
```

---

## 5. Gateway API (newer, 2024+)

LBC v2.8+ supports Kubernetes Gateway API as alternative to Ingress. Recommended for new clusters with multi-protocol/multi-tenant routing:

```yaml
# Gateway → 1:1 with ALB
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: prod-gateway
  namespace: kube-system
spec:
  gatewayClassName: alb     # LBC registers an alb GatewayClass
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: tls-cert
---
# HTTPRoute → ALB rules
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: app-route
  namespace: prod-app
spec:
  parentRefs:
    - name: prod-gateway
      namespace: kube-system
  hostnames: [app.example.com]
  rules:
    - matches:
        - path: { type: PathPrefix, value: / }
      backendRefs:
        - name: app-svc
          port: 80
```

---

## 6. VPC CNI prefix delegation (IP density)

Default: each ENI gets N IPs (e.g., m5.large = 10). Pods limited to N-1 per node.
**Prefix delegation**: each ENI gets N /28 prefixes = 16 IPs each. m5.large can host ~110 pods.

```python
# stacks/cluster_foundation.py — extend EKS_CLUSTER_FOUNDATION
cluster.add_addon(
    "vpc-cni",
    addon_version="v1.18.5-eksbuild.1",
    configuration_values=json.dumps({
        "env": {
            "ENABLE_PREFIX_DELEGATION": "true",
            "WARM_PREFIX_TARGET": "1",
            # Enable Pod ENIs (security groups for pods) if needed
            # "ENABLE_POD_ENI": "true",
        },
        "nodeAgent": {"enablePolicyEventLogs": "true"},  # for Network Policies
    }),
)
```

---

## 7. IP exhaustion mitigation — Custom Networking (secondary CIDR)

If primary VPC CIDR is exhausted, Custom Networking lets pods use IPs from a different (e.g., 100.64.0.0/10) CIDR while nodes stay on primary.

```python
# stacks/custom_networking.py
from aws_cdk import aws_ec2 as ec2

# 1. Add secondary CIDR to existing VPC
ec2.CfnVPCCidrBlock(self, "PodCidr",
    vpc_id=vpc.vpc_id,
    cidr_block="100.64.0.0/16",
)

# 2. Create pod subnets in new CIDR (one per AZ)
for i, az in enumerate(vpc.availability_zones):
    ec2.CfnSubnet(self, f"PodSubnet{i}",
        vpc_id=vpc.vpc_id,
        cidr_block=f"100.64.{i*32}.0/19",
        availability_zone=az,
        tags=[{"key": "kubernetes.io/role/cni", "value": "1"}],
    )

# 3. Configure VPC CNI Custom Networking
# (Apply via kubectl after cluster creation)
# kubectl set env ds/aws-node -n kube-system AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true
# kubectl set env ds/aws-node -n kube-system ENI_CONFIG_LABEL_DEF=topology.kubernetes.io/zone

# 4. Apply ENIConfig per AZ
# apiVersion: crd.k8s.amazonaws.com/v1alpha1
# kind: ENIConfig
# metadata: { name: us-east-1a }
# spec:
#   subnet: subnet-podsubnet-1a
#   securityGroups: [sg-pods]
```

---

## 8. ExternalDNS

```python
# stacks/external_dns_stack.py
class ExternalDnsStack(Stack):
    def __init__(self, scope, id, *, cluster_name, cluster, hosted_zone_id, **kwargs):
        super().__init__(scope, id, **kwargs)

        ed_role = iam.Role(self, "ExternalDnsRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
        )
        ed_role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))
        ed_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["route53:ChangeResourceRecordSets"],
            resources=[f"arn:aws:route53:::hostedzone/{hosted_zone_id}"],
        ))
        ed_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["route53:ListHostedZones", "route53:ListResourceRecordSets",
                     "route53:ListTagsForResource"],
            resources=["*"],
        ))

        eks.CfnPodIdentityAssociation(self, "EdAssoc",
            cluster_name=cluster_name,
            namespace="external-dns",
            service_account="external-dns",
            role_arn=ed_role.role_arn,
        )

        cluster.add_helm_chart("ExternalDns",
            chart="external-dns",
            release="external-dns",
            repository="https://kubernetes-sigs.github.io/external-dns",
            namespace="external-dns",
            version="1.15.0",
            values={
                "provider": "aws",
                "policy": "sync",   # delete records when service is removed
                "txtOwnerId": cluster_name,
                "domainFilters": ["example.com"],
                "zoneIdFilters": [hosted_zone_id],
                "serviceAccount": {"create": True, "name": "external-dns"},
            },
        )
```

---

## 9. Common gotchas

- **ALB target-type=ip is required for Fargate** — instance mode hits NodePorts which Fargate doesn't expose.
- **Subnet tagging is mandatory.** LBC won't find subnets without `kubernetes.io/role/elb=1` (public) or `kubernetes.io/role/internal-elb=1` (private). Cluster tag also required: `kubernetes.io/cluster/<name>=shared|owned`.
- **`alb.ingress.kubernetes.io/group.name`** — share one ALB across many Ingresses to save $/mo. Without it, every Ingress gets its own ALB ($16/mo each).
- **Old in-tree LB controller still active by default.** Add `loadBalancerClass: service.k8s.aws/nlb` on Service to force LBC.
- **VPC CNI ENABLE_PREFIX_DELEGATION cannot be unset.** Once enabled on a node, it owns prefixes. To revert, replace the node.
- **Network Policies require Network Policy Controller** — installed by default in VPC CNI v1.14+; verify with `kubectl get pods -n kube-system | grep aws-node`.
- **Security Groups for Pods (Pod ENIs) costs extra ENIs per pod.** Only use for compliance-critical workloads (PCI, HIPAA pod-level isolation). Most clusters should use Network Policies instead.
- **WAF v2 ARN must be regional** — `arn:aws:wafv2:REGION:ACCOUNT:regional/webacl/...` not `global/`. Regional ALB ≠ CloudFront.
- **IP-mode ALB → pod IPs as targets, but pods must be reachable from ALB SG.** LBC manages SG rules automatically only if `manage-backend-security-group-rules: 'true'`.
- **Cross-zone LB on NLB costs $$** — disabled by default. Enable only if you need true round-robin across AZs.
- **ExternalDNS `policy: sync` deletes records aggressively** — start with `upsert-only` until verified.

---

## 10. Pytest worked example

```python
# tests/test_networking.py
import boto3
import requests

elbv2 = boto3.client("elbv2")
ec2 = boto3.client("ec2")


def test_alb_provisioned_for_ingress(cluster_name):
    """The Ingress in prod-app namespace produced an ALB."""
    albs = elbv2.describe_load_balancers()["LoadBalancers"]
    matched = [
        a for a in albs
        if a["Type"] == "application"
        and any(t["Key"] == "ingress.k8s.aws/cluster" and t["Value"] == cluster_name
                for t in elbv2.describe_tags(ResourceArns=[a["LoadBalancerArn"]])
                            ["TagDescriptions"][0]["Tags"])
    ]
    assert len(matched) >= 1, "No ALB found for cluster"


def test_alb_listener_redirects_http_to_https(alb_arn):
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    http = next(l for l in listeners if l["Port"] == 80)
    actions = http["DefaultActions"]
    assert any(a["Type"] == "redirect"
               and a["RedirectConfig"]["Protocol"] == "HTTPS"
               for a in actions)


def test_alb_uses_tls_1_3(alb_arn):
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    https = next(l for l in listeners if l["Port"] == 443)
    assert https["SslPolicy"].startswith("ELBSecurityPolicy-TLS13"), \
        f"ALB SSL policy {https['SslPolicy']} is not TLS 1.3"


def test_health_check_endpoint_returns_200():
    """End-to-end: hit the ALB → pod → /healthz."""
    r = requests.get("https://app.example.com/healthz", timeout=10)
    assert r.status_code == 200


def test_prefix_delegation_active(cluster_name):
    """VPC CNI add-on has ENABLE_PREFIX_DELEGATION=true."""
    eks = boto3.client("eks")
    addon = eks.describe_addon(clusterName=cluster_name, addonName="vpc-cni")["addon"]
    config = json.loads(addon.get("configurationValues", "{}"))
    assert config.get("env", {}).get("ENABLE_PREFIX_DELEGATION") == "true"
```

---

## 11. Five non-negotiables

1. **ALB Ingress IP-mode + group.name shared across namespaces** — never one ALB per Ingress.
2. **TLS 1.3 SSL policy on all internet-facing listeners** (`ELBSecurityPolicy-TLS13-1-2-2021-06`).
3. **Subnets tagged correctly** — `kubernetes.io/role/elb` + cluster tag (verified in test).
4. **VPC CNI prefix delegation enabled** if cluster expects > 50 nodes.
5. **LBC HA**: replicaCount ≥ 2, PDB maxUnavailable=1, topology spread across AZs.

---

## 12. References

- [AWS Load Balancer Controller — Install Guide](https://kubernetes-sigs.github.io/aws-load-balancer-controller/v2.8/deploy/installation/)
- [LBC IAM policy (v2.8.0)](https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.8.0/docs/install/iam_policy.json)
- [VPC CNI prefix delegation](https://docs.aws.amazon.com/eks/latest/userguide/cni-increase-ip-addresses.html)
- [VPC CNI Custom Networking](https://docs.aws.amazon.com/eks/latest/userguide/cni-custom-network.html)
- [ExternalDNS for AWS](https://kubernetes-sigs.github.io/external-dns/v0.14.0/tutorials/aws/)
- [Gateway API + LBC (2024)](https://kubernetes-sigs.github.io/aws-load-balancer-controller/v2.8/guide/gateway/gateway/)

---

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. VPC CNI + prefix delegation + Custom Networking + LBC v2.8 + ALB IP-mode + NLB + Gateway API + ExternalDNS. Wave 9. |
