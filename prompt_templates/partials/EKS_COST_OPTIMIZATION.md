# SOP — EKS Cost Optimization (Karpenter consolidation · Compute Optimizer · Kubecost · Spot strategy · request-right-sizing)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon EKS 1.32+ · Karpenter v1.x · AWS Compute Optimizer · Kubecost (or AWS Cost Allocation tags + CUR) · Spot Instances · Savings Plans · Graviton (ARM64) · Vertical Pod Autoscaler in recommendation mode

---

## 1. Purpose

- Codify the **5 cost levers** that account for ~80% of EKS cost reduction:
  1. **Right-size pod requests** — apps over-request CPU/memory → wasted reserved capacity. VPA in `recommend` mode + manual review.
  2. **Spot at scale** — 70% off on-demand for fault-tolerant workloads. Karpenter native spot.
  3. **Graviton (ARM64)** — 20-40% cheaper than x86, comparable perf for most workloads.
  4. **Karpenter consolidation** — aggressive bin-packing, scale-down idle nodes.
  5. **Savings Plans + Reserved Instances** for the always-on baseline (NOT for spot/burst).
- Codify **per-team cost allocation** via Kubecost (OSS) or AWS Cost Allocation Tags + CUR queries via Athena.
- Codify the **monitoring + alerting** for cost anomalies.
- This is the **FinOps specialisation**. Built on `EKS_KARPENTER_AUTOSCALING`. Pairs with `EKS_OBSERVABILITY` (cost dashboards in Grafana).

When the SOW signals: "EKS bill is too high", "FinOps", "showback", "cost per team", "Spot strategy", "Graviton migration", "Savings Plan analysis".

---

## 2. Decision tree — workload type → instance strategy

```
Workload behaviour?
├── Stateless web/API (replicas, no state) → 100% spot, multi-AZ, multi-instance type
├── Batch jobs / CI / data pipeline → 100% spot OK; checkpoint to S3
├── Stateful DB, message queue → on-demand baseline (RIs/SP) + spot for read replicas
├── ML training (long-running) → spot with checkpointing OR on-demand for short jobs
├── ML inference (real-time) → on-demand baseline + spot for surge (canary)
└── Critical/regulated → on-demand only (RIs)

Architecture?
├── Java/Go/Python apps that compile native → Graviton (ARM64) — 20-40% cheaper
├── Node.js / Python (interpreted) → Graviton works, lower savings (~15%)
├── x86-only legacy / proprietary binaries → x86 only
└── ML inference → Graviton instances (g3, g4dn, c7g, m7g) for CPU; Inferentia2 for GPU-equivalent

Reserved capacity?
├── Predictable always-on baseline > $1k/mo → Compute Savings Plan (1y, no upfront)
├── Always-on > $10k/mo → 3y Compute SP w/ partial upfront (60% discount)
└── Spot-heavy / spiky → no commitment
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Karpenter spot + consolidation, no cost dashboards | **§3 Monolith** |
| Production — full FinOps stack: Karpenter + Kubecost + alarms + RI/SP analysis | **§3-§7 Full** |

---

## 3. Karpenter consolidation — the #1 lever

(Detailed config in `EKS_KARPENTER_AUTOSCALING`. Recap key cost knobs.)

```yaml
# manifests/karpenter-cost-optimized.yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata: { name: cost-optimized }
spec:
  template:
    spec:
      requirements:
        - { key: kubernetes.io/arch, operator: In, values: [arm64, amd64] }   # both arches
        - { key: karpenter.sh/capacity-type, operator: In, values: [spot, on-demand] }
        - { key: karpenter.k8s.aws/instance-category, operator: In, values: [c, m, r] }
        - { key: karpenter.k8s.aws/instance-generation, operator: Gt, values: ["6"] }   # 6th gen+
        - { key: karpenter.k8s.aws/instance-cpu, operator: In, values: ["2","4","8","16","32"] }
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      expireAfter: 720h    # max node lifetime — replace nodes weekly for fresh AMI/security
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized  # most aggressive
    consolidateAfter: 30s                            # min wait before consolidating
    budgets:
      - { nodes: "10%" }   # only 10% of nodes can disrupt at once
  weight: 50
  limits:
    cpu: "10000"
    memory: "40000Gi"
```

**Key knobs:**
- `consolidationPolicy: WhenEmptyOrUnderutilized` — terminates nodes that can be replaced cheaper, OR are empty.
- `consolidateAfter: 30s` — minimum time before consolidating a candidate node (gives pods time to settle).
- `expireAfter: 720h` — forces node refresh, picks up new spot price/AZ data.
- Multiple `instance-category` + `instance-generation` → Karpenter picks cheapest matching capacity.
- `karpenter.sh/capacity-type: spot,on-demand` → spot first, on-demand fallback.

---

## 4. Right-size pod requests — VPA recommend mode

```python
# stacks/vpa_stack.py
class VpaStack(Stack):
    def __init__(self, scope, id, *, cluster, **kwargs):
        super().__init__(scope, id, **kwargs)
        cluster.add_helm_chart("Vpa",
            chart="vertical-pod-autoscaler",
            release="vpa",
            repository="https://cowboysysop.github.io/charts/",
            namespace="vpa",
            version="9.8.2",
            create_namespace=True,
            values={
                "updater": {"enabled": False},      # recommendation mode — DO NOT auto-update
                "admissionController": {"enabled": False},
                "recommender": {"enabled": True, "extraArgs": {"recommendation-margin-fraction": "0.10"}},
            },
        )
```

```yaml
# manifests/vpa-checkout-svc.yaml — recommend resources for the deployment
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata: { name: checkout-svc-vpa, namespace: prod-app }
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: checkout-svc
  updatePolicy:
    updateMode: "Off"      # recommendation only — do not restart pods
  resourcePolicy:
    containerPolicies:
      - containerName: '*'
        minAllowed: { cpu: 50m, memory: 64Mi }
        maxAllowed: { cpu: 4, memory: 8Gi }
```

```bash
# Get recommendations
kubectl describe vpa checkout-svc-vpa -n prod-app
# Recommendation:
#   Container Recommendations:
#     Container Name:  checkout
#     Lower Bound:  cpu: 200m   memory: 512Mi
#     Target:       cpu: 350m   memory: 768Mi
#     Upper Bound:  cpu: 1      memory: 1.5Gi
# → set Deployment requests: cpu=350m, memory=768Mi (target)
# → set limits: cpu=1, memory=1.5Gi (upper bound)
```

---

## 5. Compute Optimizer — instance type recommendations

```bash
# Enable account-wide
aws compute-optimizer update-enrollment-status --status Active --include-member-accounts

# Get EKS-relevant recommendations (Auto Scaling groups behind managed node groups)
aws compute-optimizer get-auto-scaling-group-recommendations \
  --auto-scaling-group-arns arn:aws:autoscaling:us-east-1:123:autoScalingGroup:xxx
```

Compute Optimizer surfaces:
- "Over-provisioned" ASGs → suggest smaller instance type
- "Optimized" → no change
- "Under-provisioned" → suggest larger
- Graviton recommendations where x86 currently used

---

## 6. Kubecost — per-team / per-namespace cost

```python
# stacks/kubecost_stack.py
cluster.add_helm_chart("Kubecost",
    chart="cost-analyzer",
    release="kubecost",
    repository="https://kubecost.github.io/cost-analyzer/",
    namespace="kubecost",
    version="2.4.1",
    create_namespace=True,
    values={
        "kubecostToken": "<free-tier-token-or-license>",
        "global": {
            "prometheus": {"enabled": True},   # bundled Prom (or point to AMP)
            "grafana": {"enabled": False},     # use AMG
        },
        "kubecostProductConfigs": {
            "clusterName": cluster_name,
            "currencyCode": "USD",
            # AWS spot data feed for accurate spot pricing
            "athenaProjectID": account_id,
            "athenaBucketName": athena_results_bucket.bucket_name,
            "athenaDatabase": "athenacurcfn_kubecost",
            "athenaTable": "kubecost_cur",
            "athenaWorkgroup": "primary",
            "awsServiceKeyName": "kubecost-cur-reader",   # IAM role via Pod Identity
        },
        "ingress": {
            "enabled": True,
            "ingressClassName": "alb",
            "annotations": {
                "alb.ingress.kubernetes.io/scheme": "internal",
                "alb.ingress.kubernetes.io/target-type": "ip",
                "alb.ingress.kubernetes.io/group.name": "platform-internal",
            },
            "hosts": [{"host": "kubecost.internal.example.com", "paths": ["/"]}],
        },
    },
)
```

Kubecost dashboards show:
- Cost per namespace / deployment / pod / label
- Allocation: requests vs usage vs idle (the "waste" metric)
- Savings recommendations (right-sizing, abandoned PVCs, unused services)

---

## 7. Cost allocation tags + CUR + Athena (DIY alternative to Kubecost)

```python
# stacks/cur_stack.py
from aws_cdk import aws_cur as cur

cur.CfnReportDefinition(self, "Cur",
    report_name="kubecost-cur",
    time_unit="HOURLY",
    format="Parquet",
    compression="Parquet",
    s3_bucket=cur_bucket.bucket_name,
    s3_prefix="cur/",
    s3_region=self.region,
    additional_schema_elements=["RESOURCES", "SPLIT_COST_ALLOCATION_DATA"],
    additional_artifacts=["ATHENA"],
    refresh_closed_reports=True,
    report_versioning="OVERWRITE_REPORT",
)

# Activate user-defined cost allocation tags (must be done in Billing console)
# Tags to apply on every K8s resource via Kyverno mutation:
#   eks:cluster-name, eks:namespace, app, team, cost-center
```

```sql
-- Athena query: cost per namespace per day
SELECT
  line_item_usage_start_date AS day,
  resource_tags['user_team']  AS team,
  resource_tags['eks_namespace'] AS namespace,
  SUM(line_item_unblended_cost) AS cost_usd
FROM kubecost_cur
WHERE line_item_product_code = 'AmazonEC2'
  AND year = '2026' AND month = '04'
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 4 DESC;
```

---

## 8. Savings Plans + RI strategy

**Rule of thumb:**
- Always-on baseline (the count of nodes that's been ≥ X for 30+ days) → 1y Compute SP, no upfront.
- Spot eligible workloads → no commitment.
- Don't commit to specific instance types via RI — Compute SP is more flexible.

```bash
# AWS Cost Explorer → Savings Plans → Recommendations (auto-generated)
aws ce get-savings-plans-purchase-recommendation \
  --savings-plans-type COMPUTE_SP \
  --term-in-years ONE_YEAR \
  --payment-option NO_UPFRONT \
  --lookback-period-in-days SIXTY_DAYS
```

---

## 9. Cost anomaly alerts

```python
# stacks/cost_alerts_stack.py
from aws_cdk import aws_ce as ce

ce.CfnAnomalyMonitor(self, "EksAnomaly",
    monitor_name="eks-cost-anomaly",
    monitor_type="DIMENSIONAL",
    monitor_dimension="SERVICE",
)

ce.CfnAnomalySubscription(self, "EksAnomalySub",
    subscription_name="eks-cost-spike",
    monitor_arn_list=[monitor.attr_monitor_arn],
    subscribers=[{"address": "finops@example.com", "type": "EMAIL"}],
    threshold_expression={
        "Dimensions": {
            "Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
            "MatchOptions": ["GREATER_THAN_OR_EQUAL"],
            "Values": ["100"],   # alert on any anomaly ≥ $100
        },
    },
    frequency="DAILY",
)
```

---

## 10. Common gotchas

- **VPA `updateMode: Auto` restarts pods to apply new requests.** For production-critical apps, stick to `Off` mode and manually update Deployment specs.
- **Karpenter consolidation can disrupt pods** mid-request even with disruption budgets if `consolidateAfter` is short. Set PDBs on every Deployment.
- **Spot interruption is 2-min warning** — apps must `SIGTERM` gracefully. Karpenter handles drain via SQS interruption queue (see `EKS_KARPENTER_AUTOSCALING`).
- **Graviton works for most JVM/Python/Node workloads** but Java with native libs (e.g., `com.sun.jna`, ROCKSDBJNI) needs ARM64 builds. Test before migration.
- **Compute Optimizer for ASG only sees managed node groups, not Karpenter NodeClaims.** For Karpenter, rely on Kubecost or CUR.
- **CUR + SPLIT_COST_ALLOCATION_DATA must be enabled** for per-pod cost split. Without it, CUR shows EC2 instance cost, not pod-share.
- **Kubecost free tier limits:** 15-day retention, no multi-cluster aggregation. License starts ~$0.012/CPU-hr managed.
- **Savings Plans cover EC2 only, not Fargate.** Fargate has separate Compute SP type.
- **Don't commit to RIs/SPs > 60% of historical usage.** You can't "return" excess commitment.
- **Spot pricing can spike** during Re:Invent week, big AWS events. Multi-AZ + multi-instance-type Karpenter mitigates.

---

## 11. Pytest worked example

```python
# tests/test_cost_optimization.py
import boto3, json

ec2 = boto3.client("ec2")
co = boto3.client("compute-optimizer")
ce = boto3.client("ce")


def test_spot_percentage_above_threshold(cluster_name, threshold=0.5):
    """≥ 50% of cluster nodes should be spot."""
    insts = ec2.describe_instances(Filters=[
        {"Name": "tag:karpenter.sh/cluster", "Values": [cluster_name]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    flat = [i for r in insts["Reservations"] for i in r["Instances"]]
    spot = [i for i in flat if i.get("InstanceLifecycle") == "spot"]
    pct = len(spot) / len(flat) if flat else 0
    assert pct >= threshold, f"Spot pct {pct:.1%} below {threshold:.0%}"


def test_no_overprovisioned_node_groups(asg_arns):
    """Compute Optimizer should not flag any ASG as Over-provisioned."""
    recs = co.get_auto_scaling_group_recommendations(
        autoScalingGroupArns=asg_arns,
    )["autoScalingGroupRecommendations"]
    bad = [r for r in recs if r["finding"] == "OVER_PROVISIONED"]
    assert not bad, f"Over-provisioned ASGs: {[r['autoScalingGroupArn'] for r in bad]}"


def test_cost_anomaly_subscription_exists():
    subs = ce.get_anomaly_subscriptions()["AnomalySubscriptions"]
    assert any("eks" in s["SubscriptionName"].lower() for s in subs)


def test_cost_allocation_tags_active():
    """Required tags must be Active in billing."""
    tags = ce.list_cost_allocation_tags(
        Status="Active",
        TagKeys=["eks:cluster-name", "eks:namespace", "team"],
    )["CostAllocationTags"]
    assert len(tags) >= 3, "Missing required cost allocation tags"
```

---

## 12. Five non-negotiables

1. **Karpenter `consolidationPolicy: WhenEmptyOrUnderutilized` + `consolidateAfter ≤ 60s`** on at least one NodePool.
2. **VPA in recommend mode** for every production Deployment; quarterly request right-size review.
3. **Spot ≥ 50% of nodes** for fault-tolerant workloads (verified in test §11).
4. **CUR enabled with `SPLIT_COST_ALLOCATION_DATA`** + Cost Allocation Tags activated.
5. **Cost Anomaly Detection** subscribed for EKS spend ≥ $100 anomaly.

---

## 13. References

- [Karpenter consolidation](https://karpenter.sh/docs/concepts/disruption/#consolidation)
- [Vertical Pod Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler)
- [AWS Compute Optimizer for EC2](https://docs.aws.amazon.com/compute-optimizer/latest/ug/view-ec2-recommendations.html)
- [Kubecost on EKS](https://docs.kubecost.com/install-and-configure/install)
- [AWS Cost Explorer + CUR + Split Cost Allocation Data](https://docs.aws.amazon.com/cur/latest/userguide/split-cost-allocation-data.html)
- [Savings Plans for Compute](https://docs.aws.amazon.com/savingsplans/latest/userguide/what-is-savings-plans.html)

---

## 14. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. Karpenter consolidation + VPA recommend + Compute Optimizer + Kubecost + CUR + Spot strategy + Graviton + SP/RI. Wave 9. |
