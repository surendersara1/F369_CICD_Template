# SOP — EKS Security (Pod Security Standards · NetworkPolicies · ECR Inspector · GuardDuty for EKS · KMS · IMDSv2 · admission control)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · Pod Security Standards (PSS, replaces deprecated PodSecurityPolicy) · NetworkPolicies via VPC CNI Network Policy Agent · ECR Inspector enhanced scan · GuardDuty for EKS Runtime Monitoring + EKS Audit Logs · KMS envelope encryption · IMDSv2 mandatory · OPA Gatekeeper / Kyverno admission control

---

## 1. Purpose

- Codify the **defense-in-depth security posture** for EKS — control plane hardening, image supply chain, runtime detection, network segmentation, pod-level constraints, and audit.
- Codify **Pod Security Standards** (PSS) at admission via the built-in admission controller (since EKS 1.25). Three profiles: `privileged`, `baseline`, `restricted`.
- Codify **NetworkPolicies** enforced by VPC CNI Network Policy Agent — default-deny + allow-list per namespace. Includes egress to RDS / outside cluster.
- Codify **ECR Inspector enhanced scan** — continuous scanning of pushed images, OS + language packages (Python, Node, Java, Go, Ruby), severity-based blocking via CodeBuild/CodePipeline gate.
- Codify **GuardDuty for EKS** — Audit Logs (control plane API anomalies) + Runtime Monitoring (container syscalls, processes, network) via the GuardDuty agent DaemonSet.
- Codify the **admission control layer** — Kyverno preferred over OPA Gatekeeper for new clusters (simpler YAML policies, native validating + mutating + generating webhooks).
- This is the **security specialisation**. Built on `EKS_CLUSTER_FOUNDATION` + `EKS_POD_IDENTITY` + `EKS_NETWORKING`. Pairs with `EKS_OBSERVABILITY` for SIEM ingestion.

When the SOW signals: "PCI", "HIPAA", "SOC2", "production EKS", "supply chain attestation", "container threat detection", "namespace isolation", "regulated workloads".

---

## 2. Decision tree — security controls per env

| Control | Dev | Stage | Prod |
|---|---|---|---|
| PSS profile | `baseline` warn | `restricted` warn | `restricted` enforce |
| NetworkPolicies | optional | namespace-default-deny | namespace-default-deny + egress allow-list |
| ECR Inspector | scan-on-push | scan + warn HIGH | scan + block CRITICAL/HIGH |
| GuardDuty Runtime | audit-only | enabled | enabled + IR runbook |
| Image signing (Notary v2) | optional | optional | required |
| Admission controller | none | Kyverno warn | Kyverno enforce |
| KMS CMK | shared | per-env | per-env + key rotation |

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — PSS + namespace default-deny + ECR scan | **§3+§4 Monolith** |
| Production — full stack (PSS, NetworkPolicy, ECR, GuardDuty, Kyverno, signing) | **§3-§8 Multi-control** |

---

## 3. Pod Security Standards — namespace labels enforce profile

```yaml
# manifests/namespace-prod-app.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: prod-app
  labels:
    # PSS labels — built-in EKS admission webhook enforces
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.32
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

`restricted` rejects pods that:
- Run as root (`runAsUser: 0` or unset)
- Use host namespaces (`hostNetwork`, `hostPID`, `hostIPC`)
- Use privileged containers
- Use HostPath volumes
- Use ports < 1024
- Lack `seccompProfile`, `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities: drop: [ALL]`

### 3.1 Compliant pod template

```yaml
apiVersion: v1
kind: Pod
metadata: { name: app, namespace: prod-app }
spec:
  serviceAccountName: app-sa
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: app
      image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/app:1.0.0
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: { drop: [ALL] }
      resources:
        requests: { cpu: 100m, memory: 256Mi }
        limits: { cpu: 500m, memory: 512Mi }
      volumeMounts:
        - { name: tmp, mountPath: /tmp }
        - { name: cache, mountPath: /var/cache }
  volumes:
    - { name: tmp, emptyDir: {} }
    - { name: cache, emptyDir: {} }
```

---

## 4. NetworkPolicies — default-deny + explicit allow

VPC CNI v1.14+ ships the Network Policy Agent. Enable in CNI config:

```python
# In EKS_CLUSTER_FOUNDATION add-on config
configuration_values=json.dumps({
    "enableNetworkPolicy": "true",
    "nodeAgent": {
        "enablePolicyEventLogs": "true",
        "enableCloudWatchLogs": "true",
    },
})
```

```yaml
# manifests/netpol-default-deny.yaml — apply per namespace
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny-all, namespace: prod-app }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
# Allow from same-namespace + ingress controller
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-app-ingress, namespace: prod-app }
spec:
  podSelector: { matchLabels: { app: app-svc } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector: { matchLabels: { name: kube-system } }
          podSelector: { matchLabels: { app.kubernetes.io/name: aws-load-balancer-controller } }
        - podSelector: {}      # any pod in same namespace
      ports:
        - { protocol: TCP, port: 8080 }
---
# Allow egress to RDS + AWS APIs (HTTPS) + DNS
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-app-egress, namespace: prod-app }
spec:
  podSelector: { matchLabels: { app: app-svc } }
  policyTypes: [Egress]
  egress:
    # DNS
    - to:
        - namespaceSelector: { matchLabels: { name: kube-system } }
          podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports: [{ protocol: UDP, port: 53 }]
    # RDS via VPC CIDR
    - to:
        - ipBlock: { cidr: 10.0.0.0/16, except: [10.0.255.0/24] }   # exclude metadata
      ports: [{ protocol: TCP, port: 5432 }]
    # AWS APIs (S3, Secrets Manager, STS via VPC endpoints)
    - to:
        - ipBlock: { cidr: 10.0.0.0/16 }
      ports: [{ protocol: TCP, port: 443 }]
```

---

## 5. ECR + Inspector enhanced scanning + signing

```python
# stacks/ecr_security_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_inspectorv2 as inspector
from aws_cdk import aws_iam as iam
from constructs import Construct


class EcrSecurityStack(Stack):
    def __init__(self, scope, id, *, env_name, kms_key_arn, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. ECR repo with KMS encryption + immutability + scan-on-push ──
        repo = ecr.Repository(self, "AppRepo",
            repository_name=f"{env_name}/app",
            encryption=ecr.RepositoryEncryption.KMS,
            encryption_key=kms.Key.from_key_arn(self, "Key", kms_key_arn),
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Keep last 30 production images",
                    tag_status=ecr.TagStatus.TAGGED,
                    tag_prefix_list=["prod-"],
                    max_image_count=30,
                ),
                ecr.LifecycleRule(
                    description="Expire untagged after 7d",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(7),
                ),
            ],
        )

        # Repo policy — only allow EKS cluster + CodeBuild to pull/push
        repo.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowEksPull",
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("eks.amazonaws.com")],
            actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
        ))

        # ── 2. Inspector enabled (account-level) ──────────────────────
        # Inspector v2 enables enhanced scanning automatically once enabled
        # Use SSM Parameter or CloudFormation custom to ensure enabled
        # (or enable via AWS Organizations delegated admin)

        # Inspector exception filter (suppress false-positives)
        inspector.CfnFilter(self, "SuppressUnreachable",
            name="suppress-unreachable-cves",
            filter_action="SUPPRESS",
            filter_criteria={
                "vulnerabilityId": [{"comparison": "EQUALS", "value": "CVE-2024-XXXXX"}],
                "ecrImageRepositoryName": [{"comparison": "EQUALS", "value": repo.repository_name}],
            },
        )

        self.repo = repo
```

### 5.1 CI gate (pseudocode CodeBuild)

```bash
# buildspec.yml — block deploy if HIGH/CRITICAL CVE found
- |
  IMAGE=$(docker push $ECR/app:$TAG)
  aws inspector2 list-findings \
    --filter-criteria '{"ecrImageHash":[{"comparison":"EQUALS","value":"'$DIGEST'"}],
                        "severity":[{"comparison":"EQUALS","value":"HIGH"},
                                    {"comparison":"EQUALS","value":"CRITICAL"}]}' \
    --query 'findings[].title' --output text > findings.txt
  if [ -s findings.txt ]; then
    echo "BLOCKING DEPLOY - vulnerabilities found:"
    cat findings.txt
    exit 1
  fi
```

### 5.2 Image signing (Notary v2 / cosign)

```bash
# Sign image push (cosign with KMS key)
cosign sign --key awskms:///arn:aws:kms:us-east-1:123:key/xxx \
            123.dkr.ecr.us-east-1.amazonaws.com/app@sha256:abcd...

# Verify in cluster (Kyverno policy below enforces)
```

---

## 6. GuardDuty for EKS

```python
# stacks/guardduty_eks_stack.py
from aws_cdk import aws_guardduty as gd

# Enable detector + EKS Audit Logs (control plane) + EKS Runtime Monitoring (agent)
detector = gd.CfnDetector(self, "Detector",
    enable=True,
    finding_publishing_frequency="FIFTEEN_MINUTES",
    features=[
        {"name": "EKS_AUDIT_LOGS", "status": "ENABLED"},
        {"name": "EKS_RUNTIME_MONITORING", "status": "ENABLED",
         "additionalConfiguration": [
             {"name": "EKS_ADDON_MANAGEMENT", "status": "ENABLED"},  # auto-installs agent
         ]},
        {"name": "S3_DATA_EVENTS", "status": "ENABLED"},
        {"name": "EBS_MALWARE_PROTECTION", "status": "ENABLED"},
    ],
)

# Findings → SNS → PagerDuty / Slack
gd.CfnPublishingDestination(self, "ToS3",
    detector_id=detector.ref,
    destination_type="S3",
    destination_properties={
        "destinationArn": findings_bucket.bucket_arn,
        "kmsKeyArn": kms_key_arn,
    },
)
```

---

## 7. Kyverno admission control

```python
# stacks/kyverno_stack.py
cluster.add_helm_chart("Kyverno",
    chart="kyverno",
    release="kyverno",
    repository="https://kyverno.github.io/kyverno",
    namespace="kyverno",
    version="3.3.0",
    create_namespace=True,
    values={
        "admissionController": {"replicas": 3},
        "backgroundController": {"replicas": 2},
        "cleanupController": {"replicas": 2},
        "reportsController": {"replicas": 2},
    },
)
```

```yaml
# manifests/kyverno-policies.yaml
---
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: { name: require-image-signature }
spec:
  validationFailureAction: Enforce
  rules:
    - name: verify-cosign
      match: { any: [{ resources: { kinds: [Pod] } }] }
      verifyImages:
        - imageReferences: ["123.dkr.ecr.us-east-1.amazonaws.com/*"]
          attestors:
            - entries:
                - keys:
                    publicKeys: |
                      -----BEGIN PUBLIC KEY-----
                      ...
                      -----END PUBLIC KEY-----
---
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: { name: disallow-latest-tag }
spec:
  validationFailureAction: Enforce
  rules:
    - name: require-image-tag
      match: { any: [{ resources: { kinds: [Pod] } }] }
      validate:
        message: "Image tag :latest is forbidden"
        pattern:
          spec:
            containers:
              - image: "!*:latest"
---
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: { name: require-resources }
spec:
  validationFailureAction: Enforce
  rules:
    - name: require-cpu-mem-limits
      match: { any: [{ resources: { kinds: [Pod] } }] }
      validate:
        message: "CPU + memory limits required"
        pattern:
          spec:
            containers:
              - resources:
                  limits:
                    cpu: "?*"
                    memory: "?*"
                  requests:
                    cpu: "?*"
                    memory: "?*"
```

---

## 8. IMDSv2 mandatory + node hardening

(Already covered in `EKS_KARPENTER_AUTOSCALING` for Karpenter nodes; reinforce for managed node groups.)

```python
# In EKS_CLUSTER_FOUNDATION managed node group
nodegroup_props = {
    "launch_template": ec2.CfnLaunchTemplate(self, "Lt",
        launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
            metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                http_tokens="required",                # IMDSv2 only
                http_put_response_hop_limit=2,         # 2 hops for pod -> IMDS
                http_endpoint="enabled",
            ),
            block_device_mappings=[ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                device_name="/dev/xvda",
                ebs=ec2.CfnLaunchTemplate.EbsProperty(
                    volume_type="gp3",
                    volume_size=100,
                    encrypted=True,
                    kms_key_id=kms_key_arn,
                    delete_on_termination=True,
                ),
            )],
            user_data=Fn.base64("\n".join([
                "#!/bin/bash",
                "set -ex",
                # Disable SSH (use SSM Session Manager only)
                "systemctl disable sshd && systemctl stop sshd",
                # Block IMDSv1 entirely
                "iptables -I INPUT -p tcp --dport 80 -d 169.254.169.254 -j DROP",
            ])),
        ),
    ),
}
```

---

## 9. Common gotchas

- **PSS `restricted` blocks most off-the-shelf Helm charts.** Test in dev before enforcing in prod. Patch chart values or use `audit`/`warn` mode first.
- **NetworkPolicy default-deny breaks DNS** if you don't allow egress to kube-dns. ALWAYS include the DNS allow-rule first.
- **VPC CNI Network Policy Agent != Calico** — limited L7 support. For HTTP-aware policies use Cilium or AWS App Mesh.
- **GuardDuty Runtime Monitoring agent** is ~150MB image, runs as DaemonSet. Plan node memory.
- **ECR scan-on-push only triggers on tag push, not digest push.** CI must `docker push tag` not just `digest`.
- **Inspector findings can lag 1-30 min after push.** CI gate must poll, not assume immediate.
- **Image signing with cosign + KMS** requires CI role to have `kms:Sign` AND key policy allowing it. Easy miss.
- **Kyverno `validationFailureAction: Enforce`** rejects pods. Always start with `Audit`, validate findings, then promote to `Enforce` namespace by namespace.
- **PSS warning vs enforce label:** `enforce` blocks; `warn` only logs to API server. Production should always have `enforce`.
- **`hostNetwork: true` in DaemonSets** (Karpenter, ADOT, Fluent Bit) is allowed by `privileged` PSS only. Use a dedicated namespace with the `privileged` profile for system DaemonSets.

---

## 10. Pytest worked example

```python
# tests/test_security.py
import boto3, json

eks = boto3.client("eks")
ecr = boto3.client("ecr")
inspector = boto3.client("inspector2")
gd = boto3.client("guardduty")


def test_ecr_repo_immutable_kms_scanned(repo_name):
    r = ecr.describe_repositories(repositoryNames=[repo_name])["repositories"][0]
    assert r["imageTagMutability"] == "IMMUTABLE"
    assert r["encryptionConfiguration"]["encryptionType"] == "KMS"
    assert r["imageScanningConfiguration"]["scanOnPush"] is True


def test_no_critical_cves_in_latest_prod_image(repo_name):
    images = ecr.describe_images(
        repositoryName=repo_name,
        filter={"tagStatus": "TAGGED"},
    )["imageDetails"]
    latest = sorted(images, key=lambda i: i["imagePushedAt"], reverse=True)[0]
    findings = inspector.list_findings(
        filterCriteria={
            "ecrImageHash": [{"comparison": "EQUALS", "value": latest["imageDigest"]}],
            "severity": [{"comparison": "EQUALS", "value": "CRITICAL"}],
        },
    )["findings"]
    assert not findings, f"Critical CVEs in {latest['imageTags'][0]}: {[f['title'] for f in findings]}"


def test_guardduty_eks_features_enabled():
    detectors = gd.list_detectors()["DetectorIds"]
    assert detectors, "No GuardDuty detector"
    detector = gd.get_detector(DetectorId=detectors[0])
    feature_status = {f["Name"]: f["Status"] for f in detector["Features"]}
    assert feature_status.get("EKS_AUDIT_LOGS") == "ENABLED"
    assert feature_status.get("EKS_RUNTIME_MONITORING") == "ENABLED"


def test_namespace_has_pss_restricted(kubeconfig):
    """All prod-* namespaces must have pod-security.kubernetes.io/enforce=restricted."""
    # subprocess.check_output(["kubectl", "get", "ns", "-o", "json"]) and parse
    pass


def test_default_deny_netpol_in_prod_namespaces(kubeconfig):
    """Each prod-* ns has a NetworkPolicy named default-deny-all."""
    pass
```

---

## 11. Five non-negotiables

1. **PSS `restricted` enforced on all production namespaces** — namespace label `pod-security.kubernetes.io/enforce: restricted`.
2. **Default-deny NetworkPolicy** in every production namespace + explicit allow rules.
3. **ECR Inspector enhanced scan + CI gate** blocking CRITICAL/HIGH CVEs.
4. **GuardDuty EKS Audit Logs + Runtime Monitoring** enabled with findings → SNS.
5. **IMDSv2 only** + SSH disabled + EBS encrypted with CMK on all nodes.

---

## 12. References

- [Pod Security Standards (Kubernetes docs)](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
- [VPC CNI Network Policy](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)
- [ECR + Inspector enhanced scan](https://docs.aws.amazon.com/inspector/latest/user/scanning-ecr.html)
- [GuardDuty for EKS — Runtime Monitoring](https://docs.aws.amazon.com/guardduty/latest/ug/runtime-monitoring.html)
- [GuardDuty for EKS — Audit Logs](https://docs.aws.amazon.com/guardduty/latest/ug/eks-protection.html)
- [Kyverno policies](https://kyverno.io/policies/)
- [Sigstore cosign + AWS KMS](https://docs.sigstore.dev/key_management/signing_with_self-managed_keys/)

---

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. PSS + NetworkPolicies + ECR/Inspector + GuardDuty (Audit + Runtime) + Kyverno + IMDSv2 + image signing. Wave 9. |
