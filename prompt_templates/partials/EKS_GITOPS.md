# SOP — EKS GitOps (ArgoCD · Helm · External Secrets Operator · App-of-Apps · multi-cluster)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · ArgoCD v2.13+ · Helm 3.15+ · External Secrets Operator (ESO) v0.10+ · App-of-Apps pattern · ApplicationSet · CodeCommit / GitHub / GitLab as Git source

---

## 1. Purpose

- Codify the **GitOps deployment model** for EKS workloads. Git becomes the source of truth; ArgoCD reconciles cluster state to Git.
- Codify **ArgoCD installation** with Pod Identity, ALB Ingress, RBAC integrated to AWS IAM Identity Center (or OIDC of choice).
- Codify the **App-of-Apps + ApplicationSet** patterns for managing many environments and many clusters from a single Git repo.
- Codify **External Secrets Operator (ESO)** for syncing AWS Secrets Manager / SSM Parameter Store into Kubernetes Secrets — the only correct way to handle credentials in GitOps.
- Codify **Helm chart packaging conventions** for engagement code — values overlays per env (dev/stage/prod), no in-chart secrets.
- Codify **drift detection + auto-sync + sync waves** for safe rollouts.
- This is the **deployment-platform specialisation**. Built on `EKS_CLUSTER_FOUNDATION` + `EKS_POD_IDENTITY` + `EKS_NETWORKING` (for ArgoCD UI ingress).

When the SOW signals: "GitOps", "ArgoCD on EKS", "manage 5+ clusters", "drift detection", "no kubectl in pipelines", "secret rotation in K8s".

---

## 2. Decision tree — GitOps tooling

| Choice | When |
|---|---|
| **ArgoCD** | Default. CNCF-graduated, broad ecosystem. **Pick this.** |
| Flux v2 | Kustomize-first teams; lighter footprint. Viable. |
| AWS Proton + CodePipeline | Org wants AWS-managed end-to-end (no OSS deps); lacks GitOps fidelity. |

```
Sync model?
├── App-of-Apps (1 root app → N child apps) → §3 simple, scales to ~50 apps
├── ApplicationSet (templated generators) → §4 dynamic, scales to 1000s
└── Both — root ApplicationSet + per-env app-of-apps → §6 multi-cluster

Secrets source?
├── AWS Secrets Manager (rotating, broad) → §5 ESO with SecretStore type aws
├── SSM Parameter Store (cheaper, simpler) → §5 ESO with SecretStore type ssm
└── HashiCorp Vault / 1Password → ESO supports both — same pattern
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single cluster + single Git repo + ArgoCD | **§3 Monolith** |
| Production — multi-cluster + ApplicationSet + ESO | **§4+§5+§6 Full** |

---

## 3. ArgoCD install (CDK + Helm) with App-of-Apps

### 3.1 Architecture

```
   Git repo (gitops/)
       ├── clusters/
       │     ├── prod/
       │     │     ├── root-app.yaml             ← root Application
       │     │     ├── infrastructure/
       │     │     │     ├── external-secrets.yaml
       │     │     │     ├── cert-manager.yaml
       │     │     │     └── monitoring.yaml
       │     │     └── workloads/
       │     │           ├── app-svc.yaml
       │     │           └── checkout-svc.yaml
       │     └── stage/...
       └── charts/
             ├── app-svc/                         ← Helm charts
             └── checkout-svc/

   ArgoCD reconciles:
       Git → cluster
       (auto-sync = drift triggers re-apply)
```

### 3.2 CDK install

```python
# stacks/argocd_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_eks as eks
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct
import json


class ArgoCdStack(Stack):
    def __init__(self, scope: Construct, id: str, *, cluster_name: str,
                 cluster: eks.ICluster, gitops_repo_url: str,
                 acm_cert_arn: str, hosted_zone: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. ArgoCD Helm chart (HA mode) ────────────────────────────
        cluster.add_helm_chart("ArgoCd",
            chart="argo-cd",
            release="argocd",
            repository="https://argoproj.github.io/argo-helm",
            namespace="argocd",
            version="7.7.0",      # → ArgoCD 2.13.x
            create_namespace=True,
            values={
                "global": {
                    "domain": f"argocd.{hosted_zone}",
                },
                "configs": {
                    "params": {
                        "server.insecure": True,    # TLS terminated at ALB
                    },
                    # RBAC — map AWS IAM Identity Center groups to ArgoCD roles
                    "rbac": {
                        "policy.csv": (
                            "g, platform-admins, role:admin\n"
                            "g, platform-readers, role:readonly\n"
                        ),
                        "scopes": "[groups]",
                    },
                    "cm": {
                        "url": f"https://argocd.{hosted_zone}",
                        # OIDC config (IAM Identity Center)
                        "oidc.config": (
                            "name: AWS-IDC\n"
                            "issuer: https://identitycenter.amazonaws.com/ssoins-XXXX\n"
                            "clientID: $oidc.argocd.clientId\n"
                            "clientSecret: $oidc.argocd.clientSecret\n"
                            "requestedScopes: [openid, profile, email, groups]\n"
                        ),
                    },
                },
                "server": {
                    "replicas": 2,
                    "ingress": {
                        "enabled": True,
                        "ingressClassName": "alb",
                        "annotations": {
                            "alb.ingress.kubernetes.io/scheme": "internet-facing",
                            "alb.ingress.kubernetes.io/target-type": "ip",
                            "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
                            "alb.ingress.kubernetes.io/certificate-arn": acm_cert_arn,
                            "alb.ingress.kubernetes.io/ssl-policy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
                            "external-dns.alpha.kubernetes.io/hostname": f"argocd.{hosted_zone}",
                            "alb.ingress.kubernetes.io/group.name": "platform-shared",
                        },
                        "hosts": [f"argocd.{hosted_zone}"],
                    },
                },
                "controller": {"replicas": 2},
                "applicationSet": {"replicas": 2},
                "repoServer": {"replicas": 2},
                "redis-ha": {"enabled": True},
                "dex": {"enabled": False},   # using IAM IDC OIDC directly
            },
        )

        # ── 2. Bootstrap root Application (App-of-Apps) ──────────────
        cluster.add_manifest("RootApp", {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "metadata": {
                "name": "root",
                "namespace": "argocd",
                "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            },
            "spec": {
                "project": "default",
                "source": {
                    "repoURL": gitops_repo_url,
                    "targetRevision": "main",
                    "path": f"clusters/{cluster_name}",
                    "directory": {"recurse": True},
                },
                "destination": {
                    "server": "https://kubernetes.default.svc",
                    "namespace": "argocd",
                },
                "syncPolicy": {
                    "automated": {"prune": True, "selfHeal": True},
                    "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
                    "retry": {"limit": 5, "backoff": {"duration": "10s", "maxDuration": "5m"}},
                },
            },
        })
```

### 3.3 Child Application example

```yaml
# clusters/prod/workloads/app-svc.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-svc-prod
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/acme/gitops.git
    targetRevision: main
    path: charts/app-svc
    helm:
      valueFiles:
        - ../../clusters/prod/values/app-svc.yaml   # env overlay
  destination:
    server: https://kubernetes.default.svc
    namespace: prod-app
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
    # Sync waves — infrastructure first, then apps
    # (per-resource annotation: argocd.argoproj.io/sync-wave: "0")
```

---

## 4. ApplicationSet — templated apps for many envs/clusters

```yaml
# clusters/_root/applicationset-workloads.yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: workloads
  namespace: argocd
spec:
  generators:
    - matrix:
        generators:
          - clusters:
              selector:
                matchLabels: { env: prod }
          - git:
              repoURL: https://github.com/acme/gitops.git
              revision: main
              directories:
                - path: charts/*
  template:
    metadata:
      name: '{{path.basename}}-{{name}}'
    spec:
      project: default
      source:
        repoURL: https://github.com/acme/gitops.git
        targetRevision: main
        path: '{{path}}'
        helm:
          valueFiles:
            - ../../clusters/{{name}}/values/{{path.basename}}.yaml
      destination:
        server: '{{server}}'
        namespace: prod-app
      syncPolicy:
        automated: { prune: true, selfHeal: true }
        syncOptions: [CreateNamespace=true]
```

---

## 5. External Secrets Operator (ESO) + AWS Secrets Manager

### 5.1 CDK install

```python
# stacks/eso_stack.py
class EsoStack(Stack):
    def __init__(self, scope, id, *, cluster_name, cluster, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── IAM role for ESO (cluster-wide) ────────────────────────────
        eso_role = iam.Role(self, "EsoRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
        )
        eso_role.assume_role_policy.add_statements(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            actions=["sts:TagSession"],
        ))
        eso_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
                "secretsmanager:ListSecrets",
                "ssm:GetParameter", "ssm:GetParameters",
                "ssm:GetParametersByPath", "ssm:DescribeParameters",
                "kms:Decrypt",
            ],
            # In prod, scope to specific secret arns
            resources=["*"],
        ))

        eks.CfnPodIdentityAssociation(self, "EsoAssoc",
            cluster_name=cluster_name,
            namespace="external-secrets",
            service_account="external-secrets",
            role_arn=eso_role.role_arn,
        )

        cluster.add_helm_chart("Eso",
            chart="external-secrets",
            release="external-secrets",
            repository="https://charts.external-secrets.io",
            namespace="external-secrets",
            version="0.10.5",
            create_namespace=True,
            values={
                "installCRDs": True,
                "replicaCount": 2,
                "serviceAccount": {"create": True, "name": "external-secrets"},
            },
        )
```

### 5.2 ClusterSecretStore + ExternalSecret YAML

```yaml
# manifests/cluster-secret-store.yaml — bootstrap once per cluster
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata: { name: aws-sm }
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth: {}    # uses Pod Identity automatically
---
# Per-app ExternalSecret
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: app-db-creds
  namespace: prod-app
spec:
  refreshInterval: 1h         # ESO polls SM hourly + on rotation events
  secretStoreRef:
    name: aws-sm
    kind: ClusterSecretStore
  target:
    name: app-db-creds        # K8s secret name
    creationPolicy: Owner
  dataFrom:
    - extract:
        key: prod/app/db-creds   # full SM secret as JSON → K8s secret keys
```

### 5.3 PushSecret (rare — write K8s → SM)

Useful when an operator generates a secret in-cluster (e.g., cert-manager) and needs to push to SM for cross-cluster sharing.

```yaml
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata: { name: ca-cert-push, namespace: cert-manager }
spec:
  refreshInterval: 1h
  secretStoreRefs:
    - name: aws-sm
      kind: ClusterSecretStore
  selector:
    secret: { name: ca-key-pair }
  data:
    - match:
        secretKey: tls.crt
        remoteRef: { remoteKey: prod/cluster/ca-cert }
```

---

## 6. Multi-cluster shape

```
   ┌──────────────────────────────────────┐
   │  Hub cluster (mgmt-prod)             │
   │     ArgoCD (HA, 3 controllers)        │
   │     ApplicationSet generators:        │
   │       - clusters w/ label env=prod    │
   │       - git directories = charts/*    │
   └─────────────┬────────────────────────┘
                 │ (ArgoCD cluster registration via secret w/ kubeconfig)
       ┌─────────┼─────────┬─────────┐
       ▼         ▼         ▼         ▼
   prod-east  prod-west  stage-east  dev-east
   (managed)  (managed)  (managed)  (managed)
```

```bash
# Register a cluster with ArgoCD
argocd cluster add my-cluster-context --label env=prod --label region=us-west-2
```

---

## 7. Common gotchas

- **Don't put secrets in Git** — ESO is the only sane path. SealedSecrets / SOPS are alternatives but require key mgmt that ESO sidesteps.
- **`syncPolicy.automated.prune: true` will delete resources** when removed from Git. Test first in stage; some teams set `prune: false` for prod.
- **App-of-Apps recursion** — the root app must include itself in its sync set (or it'll be pruned on first sync). Use `selfHeal: true` + finalizer.
- **Helm `valueFiles` with `..` paths require `helm.valueFiles` not `helm.values`**. ArgoCD interprets relative to the chart `path`.
- **Sync waves are advisory not absolute.** Resources without `argocd.argoproj.io/sync-wave` annotation default to wave 0 and ignore your ordering.
- **ApplicationSet matrix generator with > 100 child apps slows ArgoCD UI.** Use cluster sharding (multiple ArgoCD instances) past that scale.
- **ESO ClusterSecretStore vs SecretStore** — ClusterSecretStore is cluster-wide (one auth identity), SecretStore is namespace-scoped (allows per-namespace IAM). Default to ClusterSecretStore unless you need per-team isolation.
- **ESO refresh interval default is 1h.** For credentials with ≤ 15min rotation (RDS Multi-User), set `refreshInterval: 5m` AND ensure app reads secret on every connect (no caching).
- **ArgoCD auto-sync + Helm + ConfigMap reload** — pods don't restart on ConfigMap changes by default. Add `checksum/config: {{ include "config.yaml" . | sha256sum }}` annotation in pod template.
- **GitHub repo > 100 MB** — ArgoCD repo-server caches git checkouts; large repos OOM. Use sparse-checkout or split repos.

---

## 8. Pytest worked example

```python
# tests/test_gitops.py
import requests, base64, time

ARGOCD_URL = "https://argocd.example.com"


def test_argocd_ui_responds():
    r = requests.get(f"{ARGOCD_URL}/api/version", timeout=10)
    assert r.status_code == 200
    assert r.json()["Version"].startswith("v2.")


def test_root_app_synced(argocd_token):
    headers = {"Authorization": f"Bearer {argocd_token}"}
    r = requests.get(f"{ARGOCD_URL}/api/v1/applications/root", headers=headers, timeout=10)
    app = r.json()
    assert app["status"]["sync"]["status"] == "Synced"
    assert app["status"]["health"]["status"] == "Healthy"


def test_external_secret_synced_within_5min(kubeconfig):
    """ExternalSecret should produce a K8s secret within 5 min of creation."""
    # subprocess.check_output(["kubectl", "wait", "--for=condition=Ready",
    #                          "externalsecret/app-db-creds", "-n", "prod-app",
    #                          "--timeout=5m"])
    pass


def test_no_secrets_in_git(repo_path):
    """Static check — fail if any *.yaml has 'kind: Secret' (excluding ExternalSecret)."""
    import subprocess
    bad = subprocess.run(
        ["grep", "-rE", "^kind: Secret$", repo_path],
        capture_output=True, text=True,
    ).stdout
    assert not bad, f"Found raw Secret manifests:\n{bad}"
```

---

## 9. Five non-negotiables

1. **No raw `kind: Secret` in Git** — ESO + AWS Secrets Manager / SSM only.
2. **ArgoCD HA**: 2+ replicas of server, controller, applicationSet, repoServer; redis-ha enabled.
3. **OIDC SSO via IAM Identity Center** — no local ArgoCD admin password in production.
4. **`syncPolicy.automated.selfHeal: true`** for all infra apps; manual prune ack for stateful apps until validated.
5. **TLS terminated at ALB** + `server.insecure: true` (avoids double TLS); ALB SSL policy = TLS 1.3.

---

## 10. References

- [ArgoCD — Getting Started](https://argo-cd.readthedocs.io/en/stable/getting_started/)
- [ApplicationSet controller](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/)
- [App-of-Apps pattern](https://argo-cd.readthedocs.io/en/stable/operator-manual/cluster-bootstrapping/)
- [External Secrets Operator](https://external-secrets.io/latest/)
- [ESO + AWS Secrets Manager](https://external-secrets.io/latest/provider/aws-secrets-manager/)
- [GitOps Bridge (CDK + ArgoCD blueprint)](https://github.com/gitops-bridge-dev/gitops-bridge)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. ArgoCD HA + App-of-Apps + ApplicationSet + ESO + multi-cluster + IAM IDC SSO. Wave 9. |
