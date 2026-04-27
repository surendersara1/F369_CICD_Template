# SOP — Route 53 Application Recovery Controller (ARC) (routing controls · readiness checks · zonal shift · cluster · safety rules)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Route 53 ARC — Routing Controls + Cluster + Control Panel + Safety Rules · Routing Control Health Checks (vs metric-based health checks) · Readiness Checks (resource-set-aware) · Zonal Shift + Zonal Autoshift (per-AZ failover) · DNS failover via ARC

---

## 1. Purpose

- Codify **Route 53 ARC** as the canonical **manual DR cutover** mechanism. Different from Route 53 health checks (automatic, metric-based).
- Codify **Routing Controls** — On/Off switches for traffic; manipulated via API to flip primary/secondary atomically.
- Codify **Readiness Checks** — verify the standby region is actually ready (resource sets matched, capacity available, DB replicating, etc.) before failover.
- Codify **Zonal Shift / Zonal Autoshift** — per-AZ traffic shift (built on Route 53 ARC, simpler than full regional failover; for AZ-level issues).
- Codify the **5-region high-availability cluster** — ARC service itself runs across 5 regions for control-plane resilience.
- Codify **safety rules** — gating rules ("can't shift to secondary unless secondary is READY") and assertion rules ("at least one region must be ON").
- This is the **DR control-plane specialisation**. Pairs with `DR_MULTI_REGION_PATTERNS` (data replication), `DR_RESILIENCE_HUB_FIS` (chaos engineering), `LAYER_NETWORKING` (Route 53 base).

When the SOW signals: "manual failover control", "active-passive failover", "we don't trust automatic DNS failover", "AZ failure isolation", "regulator requires manual cutover".

---

## 2. Decision tree — ARC vs basic Route 53 failover

| Need | Route 53 ARC | R53 health check + failover |
|---|---|---|
| Manual cutover (human decision) | ✅ | ❌ automatic |
| < 30s failover decision-to-traffic-shift | ✅ | ⚠️ DNS TTL bound |
| Verify standby actually ready before shift | ✅ Readiness Checks | ❌ |
| Cross-region AND zonal | ✅ both | regional only |
| Resilient to single-region AWS outage | ✅ runs across 5 regions | ⚠️ single AWS Region for the API |
| Cost concern | ❌ ~$2.50/control/mo + $250/cluster/mo | ✅ much cheaper |

```
ARC architecture:

   ┌────────────────────────────────────────────────────────────────┐
   │ ARC Cluster (data plane, 5 regions)                            │
   │   - 5 Cluster Endpoints (one per region)                         │
   │   - Quorum-based; 3 of 5 endpoints required to update state     │
   │ ARC Control Panel                                                │
   │   - Logical grouping of routing controls                          │
   │   - Has safety rules (gating, assertion)                          │
   │ ARC Routing Controls                                              │
   │   - On/Off switches; health check linked                          │
   │   - 1 control per (workload × region)                              │
   │ ARC Readiness Checks                                              │
   │   - Resource sets per workload                                     │
   │   - Reports READY / NOT_READY based on resource readiness         │
   └────────────────────────────────────────────────────────────────┘

   Operator workflow on regional failover:
     1. Validate readiness check on DR region = READY
     2. Update routing control: us-east-1 OFF, us-west-2 ON
        (atomic via cluster endpoint)
     3. R53 health check (linked to routing control) flips → DNS routes
        to us-west-2 within ~30s
     4. Verify traffic flowing
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single workload, 2 routing controls, 1 cluster | **§3 Monolith** |
| Production — multi-workload + safety rules + zonal autoshift | **§5 Production** |

---

## 3. Monolith Variant — single workload + cluster + 2 routing controls

### 3.1 CDK

```python
# stacks/arc_stack.py
from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_route53recoverycontrol as arc
from aws_cdk import aws_route53recoveryreadiness as arc_readiness
from aws_cdk import aws_route53 as r53
from aws_cdk import aws_iam as iam
from constructs import Construct


class ArcStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 primary_region: str = "us-east-1",
                 secondary_region: str = "us-west-2",
                 hosted_zone_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. ARC Cluster (data plane, 5-region) ─────────────────────
        cluster = arc.CfnCluster(self, "Cluster",
            name="prod-cluster",
            tags=[{"key": "env", "value": "prod"}],
        )
        # 5 cluster endpoints auto-created in 5 regions:
        # us-east-1, us-west-2, ap-northeast-1, eu-west-1, ap-southeast-2

        # ── 2. Control Panel ──────────────────────────────────────────
        control_panel = arc.CfnControlPanel(self, "ControlPanel",
            cluster_arn=cluster.attr_cluster_arn,
            name="prod-failover",
        )

        # ── 3. Routing Controls — one per region ──────────────────────
        primary_rc = arc.CfnRoutingControl(self, "PrimaryRc",
            cluster_arn=cluster.attr_cluster_arn,
            control_panel_arn=control_panel.attr_control_panel_arn,
            name="prod-primary-us-east-1",
        )
        secondary_rc = arc.CfnRoutingControl(self, "SecondaryRc",
            cluster_arn=cluster.attr_cluster_arn,
            control_panel_arn=control_panel.attr_control_panel_arn,
            name="prod-secondary-us-west-2",
        )

        # ── 4. Health Checks linked to routing controls ──────────────
        # ARC routing controls integrate with Route 53 via "calculated"
        # health checks of type RECOVERY_CONTROL.
        primary_health = r53.CfnHealthCheck(self, "PrimaryHealthCheck",
            health_check_config=r53.CfnHealthCheck.HealthCheckConfigProperty(
                type="RECOVERY_CONTROL",
                routing_control_arn=primary_rc.attr_routing_control_arn,
            ),
        )
        secondary_health = r53.CfnHealthCheck(self, "SecondaryHealthCheck",
            health_check_config=r53.CfnHealthCheck.HealthCheckConfigProperty(
                type="RECOVERY_CONTROL",
                routing_control_arn=secondary_rc.attr_routing_control_arn,
            ),
        )

        # ── 5. Route 53 records — primary + secondary (failover routing) ─
        r53.CfnRecordSet(self, "PrimaryRecord",
            hosted_zone_id=hosted_zone_id,
            name="app.example.com",
            type="A",
            set_identifier="primary",
            failover="PRIMARY",
            health_check_id=primary_health.attr_health_check_id,
            alias_target=r53.CfnRecordSet.AliasTargetProperty(
                dns_name=primary_alb_dns,                  # parameterize
                hosted_zone_id=primary_alb_zone_id,
                evaluate_target_health=True,
            ),
        )
        r53.CfnRecordSet(self, "SecondaryRecord",
            hosted_zone_id=hosted_zone_id,
            name="app.example.com",
            type="A",
            set_identifier="secondary",
            failover="SECONDARY",
            health_check_id=secondary_health.attr_health_check_id,
            alias_target=r53.CfnRecordSet.AliasTargetProperty(
                dns_name=secondary_alb_dns,
                hosted_zone_id=secondary_alb_zone_id,
                evaluate_target_health=True,
            ),
        )

        # ── 6. Safety Rules ───────────────────────────────────────────
        # Assertion rule: at least one routing control must be ON
        arc.CfnSafetyRule(self, "AtLeastOneOn",
            control_panel_arn=control_panel.attr_control_panel_arn,
            name="at-least-one-on",
            rule_config=arc.CfnSafetyRule.RuleConfigProperty(
                inverted=False,
                threshold=1,
                type="ATLEAST",
            ),
            assertion_rule=arc.CfnSafetyRule.AssertionRuleProperty(
                asserted_controls=[
                    primary_rc.attr_routing_control_arn,
                    secondary_rc.attr_routing_control_arn,
                ],
                wait_period_ms=5000,
            ),
        )

        # Gating rule: cannot turn OFF primary unless secondary is ON
        # (Prevents accidental dual-OFF outage)
        arc.CfnSafetyRule(self, "GatePrimaryOff",
            control_panel_arn=control_panel.attr_control_panel_arn,
            name="gate-primary-off",
            rule_config=arc.CfnSafetyRule.RuleConfigProperty(
                inverted=False,
                threshold=1,
                type="ATLEAST",
            ),
            gating_rule=arc.CfnSafetyRule.GatingRuleProperty(
                gating_controls=[secondary_rc.attr_routing_control_arn],
                target_controls=[primary_rc.attr_routing_control_arn],
                wait_period_ms=5000,
            ),
        )
```

### 3.2 Readiness Checks (verify standby ready before failover)

```python
# Resource set — DDB tables, Aurora secondaries, ALBs, etc.
# (ARC has 17 supported resource types)
ddb_resource_set = arc_readiness.CfnResourceSet(self, "DdbReadinessSet",
    resource_set_name="prod-ddb-tables",
    resource_set_type="AWS::DynamoDB::Table",
    resources=[
        arc_readiness.CfnResourceSet.ResourceProperty(
            resource_arn=f"arn:aws:dynamodb:us-east-1:{account}:table/prod-app-global",
            readiness_scopes=[
                f"arn:aws:route53-recovery-readiness::{account}:cell/us-east-1",
            ],
        ),
        arc_readiness.CfnResourceSet.ResourceProperty(
            resource_arn=f"arn:aws:dynamodb:us-west-2:{account}:table/prod-app-global",
            readiness_scopes=[
                f"arn:aws:route53-recovery-readiness::{account}:cell/us-west-2",
            ],
        ),
    ],
)

# Cells — represent regions (or AZs)
us_east_cell = arc_readiness.CfnCell(self, "UsEastCell",
    cell_name="us-east-1",
)
us_west_cell = arc_readiness.CfnCell(self, "UsWestCell",
    cell_name="us-west-2",
)

# Recovery Group — collection of cells
recovery_group = arc_readiness.CfnRecoveryGroup(self, "RecoveryGroup",
    recovery_group_name="prod-rg",
    cells=[us_east_cell.attr_cell_arn, us_west_cell.attr_cell_arn],
)

# Readiness check — periodically verifies cells match
readiness_check = arc_readiness.CfnReadinessCheck(self, "DdbReadinessCheck",
    readiness_check_name="prod-ddb-readiness",
    resource_set_name=ddb_resource_set.resource_set_name,
)
```

### 3.3 Failover orchestration script

```python
# scripts/arc_failover.py
import boto3, sys

CLUSTER_ENDPOINTS = [
    "https://example-cluster-1.us-east-1.routing-control.amazonaws.com/v1",
    "https://example-cluster-1.us-west-2.routing-control.amazonaws.com/v1",
    "https://example-cluster-1.ap-northeast-1.routing-control.amazonaws.com/v1",
    "https://example-cluster-1.eu-west-1.routing-control.amazonaws.com/v1",
    "https://example-cluster-1.ap-southeast-2.routing-control.amazonaws.com/v1",
]
PRIMARY_RC_ARN = "..."
SECONDARY_RC_ARN = "..."


def update_routing_controls(primary_state, secondary_state):
    """Try each cluster endpoint until one succeeds (3-of-5 quorum)."""
    for endpoint in CLUSTER_ENDPOINTS:
        try:
            client = boto3.client(
                "route53-recovery-cluster",
                endpoint_url=endpoint,
                region_name=endpoint.split(".")[1],
            )
            client.update_routing_control_states(
                UpdateRoutingControlStateEntries=[
                    {"RoutingControlArn": PRIMARY_RC_ARN, "RoutingControlState": primary_state},
                    {"RoutingControlArn": SECONDARY_RC_ARN, "RoutingControlState": secondary_state},
                ],
            )
            print(f"Failover successful via {endpoint}")
            return
        except Exception as e:
            print(f"Endpoint {endpoint} failed: {e}; trying next...")
    raise RuntimeError("All cluster endpoints unreachable — manual intervention required")


if __name__ == "__main__":
    target = sys.argv[1]   # "primary" or "secondary"
    if target == "secondary":
        update_routing_controls("Off", "On")
    else:
        update_routing_controls("On", "Off")
```

---

## 4. Zonal Shift + Zonal Autoshift (single-AZ failover)

For AZ-level issues (one AZ degraded, regional traffic still served by other AZs):

```bash
# Manual zonal shift — temporarily remove an AZ from a load balancer / NLB
aws arc-zonal-shift start-zonal-shift \
  --resource-identifier alb/abc-1234 \
  --away-from us-east-1a \
  --expires-in PT8H \
  --comment "AZ-a degraded; investigating"

# Monitor status
aws arc-zonal-shift list-zonal-shifts --status ACTIVE

# Cancel when AZ recovers
aws arc-zonal-shift cancel-zonal-shift --zonal-shift-id <id>
```

```python
# Zonal Autoshift — auto-shift on AWS-detected AZ issues
arc_zone.CfnAutoshiftObserver(self, "AutoshiftObserver",
    name="prod-alb-autoshift",
    aws_account_id=self.account,
    enabled=True,
    practice_run_configuration=arc_zone.CfnAutoshiftObserver.PracticeRunConfigurationProperty(
        outcome_alarms=[<CW alarm ARN>],
        blocking_alarms=[<CW alarm ARN>],
        blocked_dates=["2026-12-25"],         # holidays
        blocked_windows=["MON-15:00-15:30"],   # release windows
    ),
)
```

---

## 5. Production Variant — multi-workload + automation

```python
# Multiple control panels per workload
# Multiple routing controls per region per workload
# Safety rules per control panel

# Example: 5 workloads × 2 regions = 10 routing controls + 5 control panels
for workload in ["api", "frontend", "ml-inference", "batch", "admin"]:
    cp = arc.CfnControlPanel(self, f"Cp{workload}",
        cluster_arn=cluster.attr_cluster_arn,
        name=f"{workload}-failover",
    )
    primary_rc = arc.CfnRoutingControl(self, f"PrimaryRc{workload}",
        cluster_arn=cluster.attr_cluster_arn,
        control_panel_arn=cp.attr_control_panel_arn,
        name=f"{workload}-primary",
    )
    secondary_rc = arc.CfnRoutingControl(self, f"SecondaryRc{workload}",
        cluster_arn=cluster.attr_cluster_arn,
        control_panel_arn=cp.attr_control_panel_arn,
        name=f"{workload}-secondary",
    )
    # ... safety rules + R53 records + readiness checks ...
```

### 5.1 Game day automation

Build a "failover button" in your operations dashboard:
- Lambda invokes ARC routing control update
- Calls readiness check first (ensure DR ready)
- Logs to audit trail
- Posts to Slack

---

## 6. Common gotchas

- **ARC cluster cost is ~$250/month + $2.50/control/month.** Justify cost vs basic R53 health checks.
- **Cluster endpoints are NOT reachable from regions other than the 5 hosted regions.** Always retry across all 5 endpoints in failover scripts.
- **3-of-5 quorum required** to update routing control state. If 3 endpoints unreachable, you cannot fail over via API. Manual override exists but requires support engagement.
- **Routing control states propagate to R53 health checks within ~30 sec**. R53 DNS TTL adds another 30-60 sec for clients to pick up new resolution.
- **Safety rules can lock you out** — too restrictive gating rules during incident = manual override needed. Test rules during game day.
- **Readiness Checks are EVENTUALLY consistent** — recently-changed resources may not show as READY for 1-3 min.
- **Zonal Autoshift is opt-in per resource.** Add observer + practice runs for 30 days BEFORE enabling production autoshift.
- **R53 RECOVERY_CONTROL health check is FREE** (vs $0.50/regular health check) — but only works with ARC cluster ($250 cluster cost).
- **CDK support for ARC is L1 only** — `aws_route53recoverycontrol` and `aws_route53recoveryreadiness`. Some properties not yet documented.
- **ARC cannot fail over if your IAM/admin is impaired in primary region** — runbook should include break-glass IAM in both regions.
- **DNS-based failover doesn't help long-running connections** (WebSocket, gRPC streams) — clients must reconnect after switch.
- **Cluster ARN is region-scoped to the home region** but data plane is across 5 regions. The control plane is the choke point — ARC team continually expands availability.

---

## 7. Pytest worked example

```python
# tests/test_arc.py
import boto3, pytest

arc_ctrl = boto3.client("route53-recovery-control-config")
arc_ready = boto3.client("route53-recovery-readiness")


def test_cluster_active(cluster_arn):
    cluster = arc_ctrl.describe_cluster(ClusterArn=cluster_arn)["Cluster"]
    assert cluster["Status"] == "DEPLOYED"
    assert len(cluster["ClusterEndpoints"]) == 5


def test_routing_controls_state():
    """Primary should be ON, Secondary should be OFF in steady state."""
    # Use cluster data plane (route53-recovery-cluster) to read states
    for endpoint in CLUSTER_ENDPOINTS:
        try:
            client = boto3.client("route53-recovery-cluster",
                                   endpoint_url=endpoint,
                                   region_name=endpoint.split(".")[1])
            primary = client.get_routing_control_state(
                RoutingControlArn=PRIMARY_RC_ARN,
            )["RoutingControlState"]
            secondary = client.get_routing_control_state(
                RoutingControlArn=SECONDARY_RC_ARN,
            )["RoutingControlState"]
            assert primary == "On"
            assert secondary == "Off"
            return
        except Exception:
            continue
    raise RuntimeError("All cluster endpoints unreachable")


def test_safety_rules_present():
    rules = arc_ctrl.list_safety_rules(ControlPanelArn=cp_arn)["SafetyRules"]
    assertion = [r for r in rules if r.get("ASSERTION")]
    gating = [r for r in rules if r.get("GATING")]
    assert assertion, "No assertion rule (at least one on)"
    assert gating, "No gating rule"


def test_readiness_check_ready(readiness_check_name):
    status = arc_ready.get_readiness_check_status(
        ReadinessCheckName=readiness_check_name,
    )
    # Both cells should report READY
    for cell in status.get("ReadinessChecks", []):
        assert cell["Readiness"] == "READY"


def test_failover_script_works_end_to_end():
    """Game day automation: shift to secondary, verify, shift back."""
    # 1. Run failover script with 'secondary'
    # 2. Wait 60s
    # 3. Hit endpoint; assert response from secondary region (header check)
    # 4. Shift back to primary
    # 5. Verify
    pass
```

---

## 8. Five non-negotiables

1. **5-region cluster** — never trust single-region for DR control plane.
2. **At least one assertion rule** per control panel (prevent dual-OFF outage).
3. **Gating rule on primary OFF** → require secondary ON first.
4. **Readiness Checks for every resource set** — verify standby before failover.
5. **Failover script tested in game day** at least quarterly — un-tested DR is no DR.

---

## 9. References

- [Route 53 ARC — Developer Guide](https://docs.aws.amazon.com/r53recovery/latest/dg/what-is-route-53-recovery.html)
- [Routing Controls](https://docs.aws.amazon.com/r53recovery/latest/dg/routing-control.html)
- [Readiness Checks](https://docs.aws.amazon.com/r53recovery/latest/dg/recovery-readiness.html)
- [Zonal Shift + Autoshift](https://docs.aws.amazon.com/r53recovery/latest/dg/arc-zonal-shift.html)
- [Safety Rules](https://docs.aws.amazon.com/r53recovery/latest/dg/routing-control.safety-rules.html)
- [ARC pricing](https://aws.amazon.com/route53/application-recovery-controller/pricing/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. ARC cluster + control panel + routing controls + readiness checks + safety rules + zonal shift/autoshift + failover script. Wave 14. |
