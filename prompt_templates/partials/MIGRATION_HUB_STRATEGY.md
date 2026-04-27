# SOP — AWS Migration Hub + Strategy Recommendations + Refactor Spaces (6R framework · portfolio assessment · wave planning)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Migration Hub (org-wide migration tracker) · AWS Migration Hub Strategy Recommendations · AWS Migration Hub Refactor Spaces (Strangler Fig orchestration) · 6R framework (Rehost / Replatform / Refactor / Repurchase / Retain / Retire) · Application Discovery Service · TCO calculator

---

## 1. Purpose

- Codify **AWS Migration Hub** as the **portfolio-wide control plane** for migration projects — single pane of glass across MGN (servers), DMS (DBs), DataSync (storage), Refactor Spaces (modernization).
- Codify the **6R framework**: Rehost / Replatform / Refactor / Repurchase / Retain / Retire — applied per application, with decision tree.
- Codify **Strategy Recommendations** — analyzes app inventory + binaries + DBs and recommends migration strategy per app with rationale.
- Codify **Refactor Spaces** — orchestrates Strangler Fig pattern, lets new microservices coexist with monolith during migration.
- Codify **Application Discovery Service** — agent-based or agentless inventory of source data center.
- Codify **TCO calculator** + business case generation.
- Codify **wave planning** discipline — assessment → plan → execute → optimize.
- This is the **migration program-management specialisation**. Composes `MIGRATION_MGN`, `MIGRATION_SCHEMA_CONVERSION`, `MIGRATION_DATASYNC`. Required for any migration > 50 servers.

When the SOW signals: "data center exit", "AWS migration program", "100+ apps to migrate", "modernization roadmap", "6R assessment", "portfolio rationalization".

---

## 2. The 6R framework

```
For each application in the source environment, choose ONE strategy:

┌─────────────┬───────────────────────────────────────────────────────┐
│ 1. Rehost   │ Lift-and-shift. Source EC2/VM → AWS EC2 unchanged.    │
│ (Lift+Shift)│ Tool: MGN (this engagement)                            │
│             │ Effort: low. Cost savings: 10-15%.                     │
│             │ Use when: time-pressure (DC exit), no app expertise    │
├─────────────┼───────────────────────────────────────────────────────┤
│ 2. Replatform│ Lift-tinker-shift. Same app, AWS-managed services.   │
│ (Lift+Tinker)│ Tools: MGN + RDS / ElastiCache / OpenSearch / EFS    │
│             │ Effort: medium. Cost savings: 30-50%.                  │
│             │ Use when: self-managed DB / message queue → managed    │
├─────────────┼───────────────────────────────────────────────────────┤
│ 3. Refactor │ Re-architect to cloud-native (Lambda/Fargate/serverless)│
│             │ Tools: Refactor Spaces + Q Developer + custom code     │
│             │ Effort: HIGH. Cost savings: 50-70%. Engagement: months │
│             │ Use when: app needs major changes anyway; future-proof │
├─────────────┼───────────────────────────────────────────────────────┤
│ 4. Repurchase│ Replace with SaaS.                                   │
│             │ Tools: none (procurement engagement)                   │
│             │ Effort: low. Cost: subscription replaces capex/opex   │
│             │ Use when: SaaS exists for the function (CRM, HR, ITSM) │
├─────────────┼───────────────────────────────────────────────────────┤
│ 5. Retain   │ Keep in source environment (for now or forever)       │
│             │ Tools: hybrid (DX, Outposts, VPN) for AWS connectivity│
│             │ Use when: on-prem dependency, regulation, lifecycle   │
├─────────────┼───────────────────────────────────────────────────────┤
│ 6. Retire   │ Decommission; not migrating.                           │
│             │ Use when: orphan, duplicate, deprecated, low usage     │
└─────────────┴───────────────────────────────────────────────────────┘

In a typical 100-app portfolio (rough breakdown):
  Retire:      5-15%   (often surprises stakeholders)
  Retain:      5-10%
  Repurchase:  10-20%  (HR / CRM / ticketing → SaaS)
  Rehost:      40-60%  (lift-and-shift the bulk)
  Replatform:  10-20%
  Refactor:    5-10%   (saved for crown jewels worth investment)
```

---

## 3. Discovery → Recommendations → Plan workflow

### 3.1 Application Discovery Service

```bash
# ── Option A: Agent-based discovery (Linux + Windows) ──────────────
# Install agent on each source server; collects:
#   - Process inventory + ports + connections
#   - Performance metrics (CPU, RAM, disk, network)
#   - Network dependencies (which servers talk to which)

# Linux:
curl -O https://s3-us-west-2.amazonaws.com/aws-discovery-agent.us-west-2/linux/latest/aws-discovery-agent.tar.gz
tar -xzf aws-discovery-agent.tar.gz
sudo bash install -r us-east-1 -k <discovery-key> -s <discovery-secret>

# Windows:
# Download AWSDiscoveryAgentInstaller.exe → run with /quiet flag

# ── Option B: Agentless via vCenter (VMware only) ──────────────────
# Deploy Agentless Discovery Connector OVA → registers vCenter →
# discovers all VMs without per-VM install
```

```python
# CDK isn't typically used for ADS; it's a console/CLI workflow.
# But we can ingest discovered apps into Migration Hub and trigger
# Strategy Recommendations.
import boto3

mh_strategy = boto3.client("migrationhubstrategy", region_name="us-east-1")

# Start a portfolio assessment (data sources: ADS agent inventory,
# IT Asset Management imports, source code repos)
mh_strategy.start_assessment(
    s3bucketForAnalysisData=f"s3://migration-hub-strategy-{account_id}/analysis/",
    s3bucketForReportData=f"s3://migration-hub-strategy-{account_id}/reports/",
    assessmentDataSourceType="StrategyRecommendationsApplicationDataCollector",
    assessmentTargets=[{
        "condition": "EQUALS",
        "name": "WORKLOAD_TYPE",
        "values": ["DotNetFramework", "JavaApplication", "PhpApplication"],
    }],
)
```

### 3.2 Strategy Recommendations output

For each app, recommendations include:
- **Strategy**: one of 6Rs
- **Tools**: MGN, DMS, App2Container, etc.
- **Target service**: EC2, ECS, Lambda, Aurora, etc.
- **Migration anti-patterns**: e.g., "Uses .NET Framework 3.5 — replatform to .NET 6 on Linux"
- **Code refactor effort**: lines-of-change estimate

### 3.3 Wave plan output

```
Wave 1 (weeks 1-4): low-risk + dependencies clear
  - 5 apps, all REHOST
  - 2 apps, all REPURCHASE (SaaS swap during)

Wave 2 (weeks 5-8): medium-risk
  - 8 apps, mostly REHOST
  - 3 apps REPLATFORM (DB → RDS)

Wave 3 (weeks 9-16): higher-risk + interdependent
  - 12 apps, mix of REHOST + REPLATFORM
  - 1 app REFACTOR (start; complete in Wave 4)
```

---

## 4. Migration Hub setup + project tracking

### 4.1 Set Migration Hub home region

```bash
# One-time: pick the home region for all migration metadata
aws migrationhub-config create-home-region-control \
  --home-region us-east-1 \
  --target Type=ACCOUNT,Id=<management-account-id>
```

### 4.2 Migration tracking via Migration Hub

Migration Hub aggregates state from:
- **MGN** (server replication state, cutover progress)
- **DMS** (replication tasks, validation status)
- **DataSync** (storage transfer progress)
- **App2Container** (containerization progress)

```python
import boto3
mh = boto3.client("migrationhub", region_name="us-east-1")

# Group servers/DBs into logical applications
mh.create_progress_update_stream(ProgressUpdateStreamName="prod-migration")

# Associate discovered server with a logical application
mh.associate_discovered_resource(
    ProgressUpdateStream="prod-migration",
    MigrationTaskName="checkout-app-migration",
    DiscoveredResource={
        "ConfigurationId": "d-server-XXXXX",     # ADS server config ID
        "Description": "checkout-app-web-1",
    },
)

# Update task progress (MGN/DMS post automatically, custom tools manually)
mh.notify_migration_task_state(
    ProgressUpdateStream="prod-migration",
    MigrationTaskName="checkout-app-migration",
    Task={"Status": "IN_PROGRESS", "ProgressPercent": 60},
    UpdateDateTime=datetime.utcnow(),
    NextUpdateSeconds=300,
)

# Console: Migration Hub → Dashboards → Wave progress + status
```

---

## 5. Refactor Spaces — Strangler Fig orchestration

When refactoring monolith → microservices, Refactor Spaces sets up routing infra so new microservices live BEHIND the monolith URL, intercepting paths gradually.

### 5.1 Architecture

```
   Client ──► API Gateway (Refactor Space proxy)
                      │
         ┌────────────┴─────────────┐
         │ Routes:                   │
         │   /api/users/*  → new ECS service (microservice)
         │   /api/orders/* → new Lambda  (microservice)
         │   /*            → monolith on EC2 (default)
         │ As you "strangle" paths, route them to new service.
         └──────────────────────────┘
```

### 5.2 CDK

```python
from aws_cdk import aws_refactorspaces as rs

# Environment = the umbrella for an app being modernized
rs_env = rs.CfnEnvironment(self, "RefactorEnv",
    name="checkout-modernization",
    network_fabric_type="TRANSIT_GATEWAY",
    description="Strangler Fig migration of checkout monolith",
)

# Application = the public-facing entry point (API Gateway proxy)
rs_app = rs.CfnApplication(self, "RefactorApp",
    name="checkout-app",
    environment_identifier=rs_env.attr_environment_identifier,
    proxy_type="API_GATEWAY",
    api_gateway_proxy=rs.CfnApplication.ApiGatewayProxyInputProperty(
        endpoint_type="REGIONAL",
    ),
    vpc_id=vpc.vpc_id,
)

# Service A: existing monolith (the default route)
monolith_svc = rs.CfnService(self, "MonolithService",
    name="monolith",
    environment_identifier=rs_env.attr_environment_identifier,
    application_identifier=rs_app.attr_application_identifier,
    endpoint_type="URL",
    url_endpoint=rs.CfnService.UrlEndpointInputProperty(
        url=f"https://monolith.internal.example.com",
    ),
    vpc_id=vpc.vpc_id,
)

# Service B: new users microservice (Lambda)
users_svc = rs.CfnService(self, "UsersService",
    name="users-microservice",
    environment_identifier=rs_env.attr_environment_identifier,
    application_identifier=rs_app.attr_application_identifier,
    endpoint_type="LAMBDA",
    lambda_endpoint=rs.CfnService.LambdaEndpointInputProperty(
        arn=users_lambda.function_arn,
    ),
    vpc_id=vpc.vpc_id,
)

# Default route → monolith
rs.CfnRoute(self, "DefaultRoute",
    environment_identifier=rs_env.attr_environment_identifier,
    application_identifier=rs_app.attr_application_identifier,
    service_identifier=monolith_svc.attr_service_identifier,
    route_type="DEFAULT",
)

# Per-path route → new microservice (strangler step)
rs.CfnRoute(self, "UsersRoute",
    environment_identifier=rs_env.attr_environment_identifier,
    application_identifier=rs_app.attr_application_identifier,
    service_identifier=users_svc.attr_service_identifier,
    route_type="URI_PATH",
    uri_path_route=rs.CfnRoute.UriPathRouteInputProperty(
        source_path="/api/users",
        activation_state="ACTIVE",
        methods=["GET", "POST", "PUT", "DELETE"],
        include_child_paths=True,
    ),
)
```

### 5.3 Strangler workflow

1. Day 0: monolith handles all requests
2. Add new users microservice as Refactor Space service
3. Activate `/api/users/*` route → microservice now handles this path; monolith handles everything else
4. Validate behavior parity (canary, A/B test)
5. Repeat for `/api/orders/*`, `/api/cart/*`, etc.
6. Final: monolith routes empty → decommission

---

## 6. TCO + business case generation

### 6.1 AWS Pricing Calculator + Migration Hub TCO

```bash
# Migration Hub Strategy outputs cost estimates per app:
aws migrationhubstrategy get-application-component-strategies \
  --application-component-id <comp-id>
# Returns: targetDestination, monthlyCost, annualCost, oneTimeCost
```

### 6.2 6R rough cost savings (vs source costs)

| Strategy | Compute savings | License savings | Ops savings | Total typical |
|---|---|---|---|---|
| Rehost | 10-15% | 0% (BYOL) or 5-10% (License Included) | 5% | 15-25% |
| Replatform | 25-40% | 20-50% (managed services drop self-managed Oracle/SQL Server) | 30-40% | 30-50% |
| Refactor | 40-60% (serverless / right-sized) | 100% (no DB licenses) | 50-70% | 50-75% |
| Repurchase | varies | typically replaces capex with opex | 80%+ | varies (SaaS pricing) |

### 6.3 Common business case lines

- Server/VM compute reduction (right-sizing in AWS)
- Software license drop (Oracle, SQL Server BYOL → managed = no license)
- DC operating cost (power, cooling, real estate, hands-on staff)
- Hardware refresh avoidance ($X/yr capex)
- Productivity gains (faster provisioning, dev velocity)
- Resilience improvements (multi-AZ vs single-DC)

---

## 7. Common gotchas

- **Discovery agent install at scale** — for 1000+ servers, use SCCM / Ansible / Puppet. Manual install is impractical.
- **Application Discovery Service vs Migration Hub Strategy** — ADS is the data plane; Strategy is the analysis layer. Both required.
- **Network dependency mapping** is the highest-value deliverable from discovery — reveals undocumented coupling that informs wave grouping.
- **6R = a snapshot in time** — apps can change strategy mid-program. Don't lock in.
- **Refactor Spaces uses Transit Gateway**. If your VPC isn't TGW-attached, Refactor Spaces creates one. Plan VPC IPs.
- **Strategy Recommendations is best-effort, not gospel** — AWS recommendations skew toward AWS-managed services. Validate with app team.
- **TCO models often miss**: training costs, transition labor, parallel-run period costs (running both source + target simultaneously), data egress during migration.
- **Repurchase requires procurement + change mgmt** — SaaS deal cycles can take 6+ months; account for in roadmap.
- **Retire is the highest-ROI strategy** — every retired app is 100% cost reduction. Push hard during portfolio review.
- **Migration Hub home region cannot be changed** without recreating all metadata. Pick once.
- **Wave plans drift** — re-baseline every 4-6 weeks. Scope creep + new discoveries are normal.
- **BAU outage during migration**: bake testing windows into wave plans; cutover always to weekend/maintenance windows for prod.

---

## 8. Pytest worked example

```python
# tests/test_migration_hub.py
import boto3, pytest

mh = boto3.client("migrationhub", region_name="us-east-1")
ads = boto3.client("discovery", region_name="us-east-1")
mhs = boto3.client("migrationhubstrategy", region_name="us-east-1")


def test_home_region_set():
    cfg = boto3.client("migrationhub-config")
    home = cfg.get_home_region()
    assert home["HomeRegion"] in ["us-east-1", "us-west-2", "eu-west-1"]


def test_assessment_complete():
    asses = mhs.list_application_components()["applicationComponentInfos"]
    assert asses, "No application components — run assessment first"
    # Each component should have a recommendation
    no_rec = [c["id"] for c in asses if not c.get("recommendationSet")]
    assert not no_rec, f"{len(no_rec)} components have no recommendation"


def test_progress_streams_active():
    streams = mh.list_progress_update_streams()["ProgressUpdateStreamSummaryList"]
    assert any(s["ProgressUpdateStreamName"] == "prod-migration" for s in streams)


def test_no_servers_stuck_in_replication_too_long():
    """No MGN source server in REPLICATING > 14 days."""
    mgn = boto3.client("mgn")
    servers = mgn.describe_source_servers()["items"]
    from datetime import datetime, timezone, timedelta
    threshold = datetime.now(timezone.utc) - timedelta(days=14)
    stuck = []
    for s in servers:
        info = s.get("dataReplicationInfo", {})
        if info.get("dataReplicationState") == "REPLICATING":
            initiated = info.get("dataReplicationInitiation", {}).get("startDateTime")
            if initiated and datetime.fromisoformat(initiated) < threshold:
                stuck.append(s["sourceServerID"])
    assert not stuck, f"Stuck servers: {stuck}"
```

---

## 9. Five non-negotiables

1. **Discovery → Strategy → Plan → Execute** — never skip discovery to "just start migrating".
2. **6R per application** — every app explicitly classified, signed off by app owner.
3. **Wave-based execution** — never big-bang for > 50 servers.
4. **Migration Hub as single pane of glass** — every tool emits progress to it.
5. **Re-baseline every 4-6 weeks** — track drift; adapt scope.

---

## 10. References

- [AWS Migration Hub](https://docs.aws.amazon.com/migrationhub/latest/ug/whatishub.html)
- [Migration Hub Strategy Recommendations](https://docs.aws.amazon.com/migrationhub-strategy/latest/userguide/what-is-mhsr.html)
- [Migration Hub Refactor Spaces](https://docs.aws.amazon.com/migrationhub-refactor-spaces/latest/userguide/what-is-mhub-rs.html)
- [Application Discovery Service](https://docs.aws.amazon.com/application-discovery/latest/userguide/what-is-appdiscovery.html)
- [6R framework whitepaper](https://aws.amazon.com/cloud-migration/strategies/)
- [AWS Pricing Calculator](https://calculator.aws/)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Migration Hub + Strategy Recommendations + Refactor Spaces (Strangler Fig) + 6R framework + ADS + wave planning + TCO. Wave 13. |
