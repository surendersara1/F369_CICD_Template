# SOP — EKS Pod Identity (Pod Identity Associations · IRSA fallback · cross-account · least-privilege)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · EKS Pod Identity (GA Nov 2023, GA agent add-on `eks-pod-identity-agent`) · IRSA (legacy/fallback for cross-account) · IAM session tags · cross-account assume-role chains

---

## 1. Purpose

- Codify the **modern AWS-native IAM-for-pods pattern** — Pod Identity Associations. Replaces IRSA (IAM Roles for Service Accounts) for in-account workloads. AWS-managed credential rotation, no OIDC trust policy gymnastics, no per-cluster trust policy updates when rotating clusters.
- Codify the **fallback to IRSA** for cross-account scenarios where Pod Identity does not yet support trust delegation across accounts.
- Codify the **session tags** that propagate Kubernetes ServiceAccount/Namespace/Pod metadata into IAM session for ABAC.
- Codify the **migration path** from IRSA → Pod Identity for existing clusters.
- Codify **least-privilege patterns** — one role per workload, scoped to one namespace, scoped to one ServiceAccount.
- This is the **IAM-for-pods specialisation**. Built on `EKS_CLUSTER_FOUNDATION` (which installs `eks-pod-identity-agent` add-on). Required by `EKS_KARPENTER_AUTOSCALING`, `EKS_NETWORKING` (LBC), `EKS_OBSERVABILITY` (ADOT).

When the SOW signals: "pods need to call AWS APIs", "S3/Secrets Manager from pods", "remove static AWS credentials", "cross-account IAM for K8s", "rotate IRSA roles".

---

## 2. Decision tree — Pod Identity vs IRSA

| Need | Pod Identity (recommended) | IRSA (legacy) |
|---|---|---|
| In-account IAM for pods | ✅ best | ✅ works |
| Cross-account IAM for pods | ⚠️ via assume-role chain | ✅ direct (OIDC trust) |
| Trust policy maintenance | ✅ none — AWS-managed | ❌ trust policy per cluster, breaks on cluster rotation |
| Session tag propagation | ✅ native (namespace/SA/pod) | ⚠️ requires custom session policy |
| Multi-cluster role reuse | ✅ trivial (one role, N clusters) | ❌ trust policy lists every cluster's OIDC ARN |
| Credential refresh latency | < 1s | < 1s |
| GA support | ✅ Nov 2023, broad SDK | ✅ since 2019 |

```
Workload location?
├── Same account as IAM role → §3 Pod Identity Association
├── Different account → §4 IRSA + cross-account assume-role
└── Karpenter / LBC / ADOT controllers → §3 Pod Identity (canonical)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 1 workload + 1 IAM role + Pod Identity | **§3 Monolith Variant** |
| Production — many workloads, central IAM stack vs per-app stacks | **§7 Micro-Stack Variant** |

---

## 3. Monolith Variant — Pod Identity Association

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  EKS Cluster (foundation: eks-pod-identity-agent add-on running) │
   │     - Daemonset on every node, listens on 169.254.170.23:80      │
   │     - Implements AWS_CONTAINER_CREDENTIALS_FULL_URI flow         │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Pod (ServiceAccount: app-sa, Namespace: prod-app)               │
   │     1. SDK reads env vars set by Pod Identity webhook            │
   │     2. SDK calls 169.254.170.23:80/{credentials_path}            │
   │     3. Agent calls EKS Auth API → STS AssumeRoleForPodIdentity    │
   │     4. Returns short-lived creds (15min default, 1h max)         │
   └────────────────┬─────────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  IAM role: AppRole (trust policy = pods.eks.amazonaws.com)       │
   │     - One Pod Identity Association binds:                         │
   │         (cluster, namespace=prod-app, sa=app-sa) → AppRole       │
   │     - Session tags auto-applied:                                  │
   │         eks-cluster-arn, kubernetes-namespace, kubernetes-sa-name │
   └──────────────────────────────────────────────────────────────────┘
```

### 3.2 IAM role for the workload

```python
# stacks/pod_identity_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks
from constructs import Construct


class AppPodIdentityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,                # "prod"
        cluster_name: str,            # "f369-prod-cluster"
        namespace: str,               # "prod-app"
        service_account_name: str,    # "app-sa"
        s3_bucket_arns: list[str],    # buckets the workload reads/writes
        secrets_arns: list[str],      # secrets the workload reads
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. IAM role with Pod Identity trust policy ────────────────
        # Trust principal is pods.eks.amazonaws.com — NO OIDC URL anywhere.
        # Action = sts:AssumeRole + sts:TagSession (so EKS can inject session tags).
        self.role = iam.Role(
            self, "AppPodRole",
            role_name=f"{env_name}-{namespace}-{service_account_name}-role",
            assumed_by=iam.ServicePrincipal(
                "pods.eks.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            ),
            max_session_duration=Duration.hours(1),
        )

        # Pod Identity needs both AssumeRole and TagSession on the trust policy.
        # CDK's ServicePrincipal only grants AssumeRole; add TagSession explicitly.
        self.role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
                actions=["sts:TagSession"],
            )
        )

        # ── 2. Least-privilege workload permissions ────────────────────
        if s3_bucket_arns:
            self.role.add_to_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[f"{arn}/*" for arn in s3_bucket_arns],
            ))
            self.role.add_to_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:ListBucket"],
                resources=s3_bucket_arns,
            ))

        if secrets_arns:
            self.role.add_to_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=secrets_arns,
            ))

        # ── 3. Pod Identity Association ────────────────────────────────
        # Binds (cluster, namespace, ServiceAccount) → role.
        # ServiceAccount itself is created by the Helm/Kustomize app
        # (no annotation needed — Pod Identity discovers via association).
        self.association = eks.CfnPodIdentityAssociation(
            self, "Association",
            cluster_name=cluster_name,
            namespace=namespace,
            service_account=service_account_name,
            role_arn=self.role.role_arn,
        )
```

### 3.3 ServiceAccount manifest (deployed by app, not CDK)

```yaml
# manifests/serviceaccount.yaml — applied via Helm/ArgoCD
apiVersion: v1
kind: ServiceAccount
metadata:
  name: app-sa
  namespace: prod-app
  # NO eks.amazonaws.com/role-arn annotation needed for Pod Identity!
  # (annotation is only required for IRSA — see §4)
```

```yaml
# manifests/deployment.yaml — pod referencing the SA
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
  namespace: prod-app
spec:
  replicas: 3
  selector:
    matchLabels: { app: app }
  template:
    metadata:
      labels: { app: app }
    spec:
      serviceAccountName: app-sa  # binds to the Pod Identity Association
      containers:
        - name: app
          image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/app:1.0.0
          # Pod Identity webhook injects:
          #   AWS_CONTAINER_CREDENTIALS_FULL_URI=http://169.254.170.23/v1/credentials
          #   AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE=/var/run/secrets/...
          # SDK picks these up automatically.
```

### 3.4 Verify in pod

```bash
# Inside the pod:
$ env | grep AWS
AWS_CONTAINER_CREDENTIALS_FULL_URI=http://169.254.170.23/v1/credentials
AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE=/var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token
AWS_DEFAULT_REGION=us-east-1
AWS_REGION=us-east-1
AWS_STS_REGIONAL_ENDPOINTS=regional

$ aws sts get-caller-identity
{
    "UserId": "AROAEXAMPLE:botocore-session-1234567890",
    "Account": "123456789012",
    "Arn": "arn:aws:sts::123456789012:assumed-role/prod-prod-app-app-sa-role/botocore-session-1234567890"
}
```

---

## 4. IRSA fallback variant — cross-account or legacy clusters

When Pod Identity is not viable (cross-account trust delegation, pre-1.24 clusters, OIDC-aware tooling), use IRSA.

### 4.1 IRSA architecture

```
   Pod (SA app-sa, annotated with role-arn)
        │
        ▼
   AWS SDK reads projected token at /var/run/secrets/eks.amazonaws.com/serviceaccount/token
        │
        ▼
   STS AssumeRoleWithWebIdentity (OIDC token from cluster) → credentials
        │
        ▼
   IAM role with trust policy: federated(OIDC) + condition(sub=system:serviceaccount:ns:sa)
```

### 4.2 IRSA CDK

```python
# stacks/irsa_cross_account_stack.py
from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from constructs import Construct


class CrossAccountIrsaStack(Stack):
    """Workload in ACCOUNT_A's EKS cluster needs to assume a role in ACCOUNT_B."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_oidc_provider_arn: str,    # arn:aws:iam::A:oidc-provider/oidc.eks...
        cluster_oidc_issuer_url: str,      # oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D...
        namespace: str,
        service_account_name: str,
        target_account_id: str,            # Account B
        target_role_name: str,             # Role in account B to assume
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. Local IRSA role (in account A) ──────────────────────────
        # Trust policy: federated to cluster OIDC, condition on sub claim.
        sub_claim = f"system:serviceaccount:{namespace}:{service_account_name}"

        local_role = iam.Role(
            self, "LocalIrsaRole",
            assumed_by=iam.FederatedPrincipal(
                federated=cluster_oidc_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{cluster_oidc_issuer_url}:sub": sub_claim,
                        f"{cluster_oidc_issuer_url}:aud": "sts.amazonaws.com",
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

        # ── 2. Local role can assume the target role in account B ──────
        target_role_arn = f"arn:aws:iam::{target_account_id}:role/{target_role_name}"
        local_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["sts:AssumeRole"],
            resources=[target_role_arn],
        ))

        self.role_arn = local_role.role_arn
        # ServiceAccount manifest (app-side) annotates with this role_arn:
        #   eks.amazonaws.com/role-arn: <local_role.role_arn>
```

### 4.3 IRSA ServiceAccount (annotation REQUIRED)

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: app-sa
  namespace: prod-app
  annotations:
    # IRSA requires the annotation — Pod Identity does not.
    eks.amazonaws.com/role-arn: arn:aws:iam::111111111111:role/cross-acct-irsa-role
```

### 4.4 Application code (cross-account chain)

```python
# Inside pod (account A) — assume role in account B
import boto3

# Step 1: SDK uses IRSA web identity token to assume LocalIrsaRole (account A)
sts_a = boto3.client("sts")  # auto-uses IRSA

# Step 2: Use those creds to assume target role in account B
resp = sts_a.assume_role(
    RoleArn="arn:aws:iam::222222222222:role/cross-acct-target-role",
    RoleSessionName="from-eks-pod",
)
creds = resp["Credentials"]

# Step 3: Build a session for account B
s3_b = boto3.client(
    "s3",
    aws_access_key_id=creds["AccessKeyId"],
    aws_secret_access_key=creds["SecretAccessKey"],
    aws_session_token=creds["SessionToken"],
)
```

---

## 5. Migration: IRSA → Pod Identity (in-place)

For existing clusters with IRSA-annotated ServiceAccounts:

```python
# stacks/migration_stack.py — runs alongside existing IRSA setup
from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks


class IrsaToPodIdentityMigrationStack(Stack):
    def __init__(self, scope, id, *, cluster_name, existing_role_arn,
                 namespace, sa_name, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Step 1: Add Pod Identity trust to existing role.
        # The role keeps its IRSA OIDC trust AND gains Pod Identity trust.
        # Both work simultaneously during migration.
        existing_role = iam.Role.from_role_arn(self, "Existing", existing_role_arn,
                                                mutable=True)
        existing_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
                actions=["sts:AssumeRole", "sts:TagSession"],
            )
        )

        # Step 2: Create Pod Identity Association
        eks.CfnPodIdentityAssociation(
            self, "MigAssoc",
            cluster_name=cluster_name,
            namespace=namespace,
            service_account=sa_name,
            role_arn=existing_role_arn,
        )

        # Step 3 (post-deploy, manual): remove eks.amazonaws.com/role-arn annotation
        #   from the SA. Pod Identity wins because the agent runs at 169.254.170.23
        #   and the SDK prefers AWS_CONTAINER_CREDENTIALS_FULL_URI.
        # Step 4 (after verification): remove OIDC trust statement from role,
        #   leaving only pods.eks.amazonaws.com.
```

**Migration order matters:**
1. Add Pod Identity trust to role (both work)
2. Create Pod Identity Association
3. Restart pods → SDK switches to Pod Identity (env var precedence)
4. Verify CloudTrail shows `AssumeRoleForPodIdentity` (not `AssumeRoleWithWebIdentity`)
5. Remove SA annotation
6. Remove OIDC trust from role

---

## 6. Common gotchas

- **`sts:TagSession` is mandatory.** The default `iam.ServicePrincipal` only adds `sts:AssumeRole`. Pod Identity calls `AssumeRoleForPodIdentity` which requires both. Missing `TagSession` → pods get `AccessDenied` with no obvious cause.
- **`eks-pod-identity-agent` add-on must be installed on the cluster.** EKS_CLUSTER_FOUNDATION installs it by default; if missing, Pod Identity Associations exist but pods can't reach 169.254.170.23.
- **Pod Identity does NOT support cross-account directly.** The trust principal `pods.eks.amazonaws.com` is account-scoped. For cross-account, either (a) use IRSA, or (b) use Pod Identity to assume an in-account role that then assumes the cross-account role (chain).
- **`max_session_duration` on the role caps at 12h, but Pod Identity tokens default to 15min and refresh.** Don't set unrealistic expectations.
- **Pod Identity Associations are NOT bulk-deletable via cluster delete.** Delete them explicitly before tearing down the cluster, or CFN will hang.
- **Session tag keys are AWS-managed.** You get `eks-cluster-arn`, `kubernetes-namespace`, `kubernetes-service-account`, `kubernetes-pod-name`, `kubernetes-pod-uid` for free in the IAM session — use these in IAM policy `Condition` blocks for ABAC. You CANNOT add custom session tags via Pod Identity (use IRSA + custom session policy if you need that).
- **One association per (cluster, namespace, SA).** Cannot have two roles for the same SA. If you need that, split into two SAs.
- **Webhook injects env vars only at pod create time.** Existing pods don't pick up new associations — must restart.
- **IRSA + Pod Identity coexist on same SA only during migration.** Pod Identity wins (env var precedence). Remove SA annotation when done.

---

## 7. Micro-Stack Variant — central PodIdentity stack vs per-app stacks

Shape:
- `EksClusterStack` — owns cluster (from EKS_CLUSTER_FOUNDATION)
- `PlatformIamStack` — owns roles + Pod Identity Associations for platform components (Karpenter, LBC, ADOT, ExternalDNS) — created once per cluster
- `WorkloadIamStack` — per-app stack, owns one role + one association per workload

```python
# stacks/platform_iam_stack.py — runs once per cluster
class PlatformIamStack(Stack):
    def __init__(self, scope, id, *, cluster_name, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Karpenter controller
        karpenter_role = self._make_role("KarpenterController", [
            "ec2:RunInstances", "ec2:CreateTags", "ec2:CreateFleet",
            "ec2:DescribeInstances", "ec2:TerminateInstances",
            "iam:PassRole", "pricing:GetProducts",
            "ssm:GetParameter", "eks:DescribeCluster",
        ])
        eks.CfnPodIdentityAssociation(self, "KarpAssoc",
            cluster_name=cluster_name,
            namespace="kube-system",
            service_account="karpenter",
            role_arn=karpenter_role.role_arn,
        )

        # AWS Load Balancer Controller
        lbc_role = self._make_role("LbcController", [
            "elasticloadbalancing:*", "ec2:Describe*",
            "iam:CreateServiceLinkedRole",
        ])
        eks.CfnPodIdentityAssociation(self, "LbcAssoc",
            cluster_name=cluster_name,
            namespace="kube-system",
            service_account="aws-load-balancer-controller",
            role_arn=lbc_role.role_arn,
        )

        # ADOT collector
        adot_role = self._make_role("AdotCollector", [
            "aps:RemoteWrite", "logs:PutLogEvents",
            "xray:PutTraceSegments", "cloudwatch:PutMetricData",
        ])
        eks.CfnPodIdentityAssociation(self, "AdotAssoc",
            cluster_name=cluster_name,
            namespace="opentelemetry",
            service_account="adot-collector",
            role_arn=adot_role.role_arn,
        )

    def _make_role(self, name, actions):
        role = iam.Role(self, f"{name}Role",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
        )
        role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))
        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW, actions=actions, resources=["*"],
        ))
        return role
```

---

## 8. Pytest worked example — Pod Identity Association exists for SA

```python
# tests/test_pod_identity.py
import boto3
import pytest

eks = boto3.client("eks")
sts = boto3.client("sts")


def test_pod_identity_agent_addon_running(cluster_name):
    """Verify eks-pod-identity-agent add-on is installed."""
    addons = eks.list_addons(clusterName=cluster_name)["addons"]
    assert "eks-pod-identity-agent" in addons

    addon = eks.describe_addon(
        clusterName=cluster_name, addonName="eks-pod-identity-agent",
    )["addon"]
    assert addon["status"] == "ACTIVE"


def test_workload_has_pod_identity_association(cluster_name):
    """The prod-app/app-sa SA has exactly one Pod Identity Association."""
    associations = eks.list_pod_identity_associations(
        clusterName=cluster_name,
        namespace="prod-app",
        serviceAccount="app-sa",
    )["associations"]
    assert len(associations) == 1
    assoc = eks.describe_pod_identity_association(
        clusterName=cluster_name, associationId=associations[0]["associationId"],
    )["association"]
    role_arn = assoc["roleArn"]

    # Role trust policy permits Pod Identity service principal
    iam = boto3.client("iam")
    role_name = role_arn.split("/")[-1]
    trust = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]
    principals = [
        s["Principal"].get("Service") for s in trust["Statement"]
    ]
    assert "pods.eks.amazonaws.com" in principals


def test_session_tags_propagate(cluster_name):
    """Spawn a debug pod, exec sts:GetCallerIdentity, verify session tags."""
    # (integration test — requires kubectl + cluster access)
    # Pseudocode:
    #   kubectl run -n prod-app debug --rm -i --image=amazon/aws-cli \
    #     --serviceaccount=app-sa -- sts get-caller-identity --query Arn
    # Expected ARN: ...:assumed-role/prod-prod-app-app-sa-role/eks-prod-app-app-sa-<podname>
    # The session name encodes ns/sa/pod for audit trail.
    pass
```

---

## 9. Five non-negotiables

1. **`sts:TagSession` always present in trust policy** — without it, Pod Identity returns AccessDenied.
2. **`eks-pod-identity-agent` add-on installed** — verified in test (§8).
3. **One role per workload, scoped to one (namespace, sa)** — never share roles across workloads.
4. **`max_session_duration` ≤ 1h** — limits blast radius if creds leak.
5. **Pod Identity preferred over IRSA for in-account workloads** — only fall back to IRSA for cross-account.

---

## 10. References

- [Amazon EKS Pod Identity — User Guide](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
- [eks-pod-identity-agent add-on](https://docs.aws.amazon.com/eks/latest/userguide/add-ons-pod-id.html)
- [IRSA vs Pod Identity comparison](https://docs.aws.amazon.com/eks/latest/userguide/service-accounts.html)
- [STS AssumeRoleForPodIdentity API](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleForPodIdentity.html)
- [IAM session tags for ABAC](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_session-tags.html)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Pod Identity Associations + IRSA fallback + cross-account chain + IRSA→Pod Identity migration. Wave 9. |
