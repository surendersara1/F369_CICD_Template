# SOP — Data Mesh Patterns (domain-oriented data products · federated governance · ABAC · cross-domain sharing · self-service platform)

**Version:** 2.0 · **Last-reviewed:** 2026-04-28 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Data Mesh principles (Zhamak Dehghani) · DataZone for catalog + governance · Lake Formation + LF-Tags for ABAC · Multi-account architecture (per-domain accounts) · OpenLineage for cross-domain lineage · Data product SLOs

---

## 1. Purpose

- Codify **AWS-native data mesh patterns** — the org-wide architecture that enables domain-oriented data ownership at scale.
- Codify the **4 data mesh principles** + their AWS implementations:
  1. **Domain-oriented decentralized data ownership** → per-domain AWS account
  2. **Data as a product** → DataZone data products with SLOs
  3. **Self-serve data infrastructure** → Service Catalog blueprints + DataZone environment profiles
  4. **Federated computational governance** → centrally-defined policies (LF tags, naming conventions) + per-domain enforcement
- Codify the **multi-account architecture** — Hub-and-spoke with central catalog account + per-domain producer accounts + cross-account consumer access.
- Codify **federated governance** patterns — central data council defines policies; domains enforce.
- Codify **cross-domain sharing** mechanics — RAM + Lake Formation + DataZone subscriptions.
- Codify **data product SLOs** — freshness, completeness, availability, latency.
- Pairs with `DATA_DATAZONE_V2` (catalog + UI), `DATA_LAKE_FORMATION` (auth backend), `DATA_GLUE_QUALITY` (DQ on products), `ENTERPRISE_CONTROL_TOWER` (multi-account foundation).

When the SOW signals: "data mesh", "domain-oriented data ownership", "federated data governance", "data products at scale", "decentralized data architecture".

---

## 2. Decision tree — when data mesh fits

| Org state | Data mesh? |
|---|---|
| Single team, < 10 data sources | ❌ overkill; central data lake fine |
| 5+ teams produce data; central team can't keep up | ✅ data mesh helps |
| Regulated industry with domain isolation requirements | ✅ |
| Mature data engineering capability across domains | ✅ |
| Domains lack data eng capacity | ⚠️ mesh fails; need investment first |

```
Data mesh maturity stages:

  Stage 0: Centralized lake/warehouse
      Single team owns everything; bottleneck.
      
  Stage 1: Domain alignment (no mesh yet)
      Producers in domains; central platform team operates infra.
      Catalog: shared Glue Catalog with consistent prefixes.
      
  Stage 2: Federated catalog
      DataZone introduced; domains publish products to shared catalog.
      Central platform still operates infra.
      
  Stage 3: Self-serve infrastructure
      Service Catalog blueprints; domains self-provision Glue/Athena/RDS.
      Central platform owns blueprints + governance.
      
  Stage 4: Domain-owned accounts
      Per-domain AWS accounts; central account = catalog + governance.
      Cross-account RAM + LF for sharing.
      Domains operate their own data products.
      
  Stage 5: Full mesh
      4 principles applied; SLO-driven; computational governance automated.
      Central council = standards; domains = execution.
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — 2 domains + central catalog (Stage 2-3) | **§3 Federated Catalog** |
| Production — 5+ domains + per-domain accounts (Stage 4-5) | **§5 Multi-Account** |

---

## 3. Federated Catalog (Stage 2-3)

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────┐
   │ Central Catalog Account (data-platform)                       │
   │   - DataZone domain                                            │
   │   - Glue Data Catalog (shared via LF + RAM)                    │
   │   - LF-Tags (PII, customer-data, financial-data, ...)           │
   │   - Service Catalog products (data eng blueprints)              │
   │   - Central data council artifacts                              │
   └──────────────────────────────────────────────────────────────┘
              ▲                                          ▲
              │ catalog access                            │
              │                                          │
   ┌──────────────────────────┐         ┌──────────────────────────┐
   │ Domain: Product Account   │         │ Domain: Finance Account   │
   │   - Glue ETL jobs           │         │   - Glue ETL jobs           │
   │   - Aurora source DBs       │         │   - Salesforce ingestion    │
   │   - S3 data lake (own)       │         │   - S3 data lake (own)        │
   │   - Athena workgroup         │         │   - Athena workgroup          │
   │   - Glue tables registered  │         │   - Glue tables registered    │
   │     to central catalog       │         │     to central catalog        │
   │   - Data products published  │         │   - Data products published    │
   │     to DataZone               │         │     to DataZone                │
   └──────────────────────────┘         └──────────────────────────┘
              │                                          │
              ▼                                          ▼
      Cross-account RAM share + LF cross-account permissions
              │                                          │
              ▼                                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ Consumer Account (Analytics)                                  │
   │   - QuickSight                                                  │
   │   - Athena (queries cross-domain)                                │
   │   - SageMaker (ML training on cross-domain)                       │
   │   - Subscriptions to data products via DataZone                   │
   └──────────────────────────────────────────────────────────────┘
```

### 3.2 LF-Tags as governance ABAC

```python
# stacks/data_mesh_governance_stack.py — Central Catalog account
from aws_cdk import aws_lakeformation as lf
from aws_cdk import aws_iam as iam

# Central LF-Tag definitions
# These are the org-wide vocabulary
lf.CfnTag(self, "PiiTag",
    catalog_id=self.account, tag_key="PII",
    tag_values=["true", "false"],
)
lf.CfnTag(self, "DomainTag",
    catalog_id=self.account, tag_key="Domain",
    tag_values=["product", "finance", "hr", "marketing", "engineering"],
)
lf.CfnTag(self, "ClassificationTag",
    catalog_id=self.account, tag_key="Classification",
    tag_values=["public", "internal", "confidential", "restricted"],
)
lf.CfnTag(self, "DataClassTag",
    catalog_id=self.account, tag_key="DataClass",
    tag_values=["customer", "financial", "operational", "health", "marketing"],
)

# ABAC policy via LF-Tag — apply per role
# Admin role: gets all LF-Tags
# Analytics-team role: gets all PII=false + Classification IN [public, internal]
# Finance-team role: gets DataClass=financial OR Domain=finance

lf.CfnPrincipalPermissions(self, "AnalyticsAccessNonPii",
    catalog="default",
    permissions=["DESCRIBE", "SELECT"],
    permissions_with_grant_option=[],
    principal=lf.CfnPrincipalPermissions.DataLakePrincipalProperty(
        data_lake_principal_identifier=analytics_role_arn,
    ),
    resource=lf.CfnPrincipalPermissions.ResourceProperty(
        lf_tag_policy=lf.CfnPrincipalPermissions.LFTagPolicyResourceProperty(
            catalog_id=self.account,
            resource_type="TABLE",
            expression=[
                {"TagKey": "PII", "TagValues": ["false"]},
                {"TagKey": "Classification",
                 "TagValues": ["public", "internal"]},
            ],
        ),
    ),
)
```

### 3.3 Cross-account Glue Catalog access (per-domain accounts)

```python
# Producer domain account (Product) registers Glue table to central Catalog
# Via cross-account Glue Catalog reference

# In Product account:
glue.CfnDatabase(self, "ProductDb",
    catalog_id=central_account_id,                       # CENTRAL catalog
    database_input=glue.CfnDatabase.DatabaseInputProperty(
        name="prod_orders",
        location_uri=f"s3://{product_data_bucket}/prod_orders/",
    ),
)

# Now central catalog has the database; central LF can grant on it.
```

---

## 4. Service Catalog blueprints — self-serve infrastructure

```python
# stacks/data_eng_blueprints_stack.py — Central account
from aws_cdk import aws_servicecatalog as sc

portfolio = sc.Portfolio(self, "DataEngPortfolio",
    display_name="Data Engineering",
    provider_name="Platform Team",
    description="Self-serve data infrastructure blueprints",
)

# Blueprint 1: Standard data domain (Glue + Athena + S3 bucket)
domain_product = sc.CloudFormationProduct(self, "DataDomainProduct",
    product_name="Data Domain Setup",
    owner="Platform Team",
    product_versions=[sc.CloudFormationProductVersion(
        cloud_formation_template=sc.CloudFormationTemplate.from_url(...),
        product_version_name="v1.0",
    )],
)
portfolio.add_product(domain_product)

# Blueprint 2: ETL pipeline (Glue job + crawler + DQ)
etl_product = sc.CloudFormationProduct(self, "EtlPipelineProduct",
    product_name="ETL Pipeline",
    owner="Platform Team",
    product_versions=[sc.CloudFormationProductVersion(
        cloud_formation_template=sc.CloudFormationTemplate.from_url(...),
        product_version_name="v1.0",
    )],
)
portfolio.add_product(etl_product)

# Constrained: domain teams can only deploy via Service Catalog (not raw CFN)
# This enforces governance — every deploy includes required LF-Tags, KMS, monitoring.

# Share portfolio to domain accounts
for domain_account_id in domain_account_ids:
    sc.CfnPortfolioShare(self, f"Share{domain_account_id}",
        accept_language="en",
        account_id=domain_account_id,
        portfolio_id=portfolio.portfolio_id,
        share_tag_options=True,
    )
```

---

## 5. Multi-Account Variant — full mesh (Stage 4-5)

### 5.1 Account topology

```
Central Catalog Account (data-platform)
  ├── DataZone Domain
  ├── Glue Catalog (shared via RAM)
  ├── LF-Tag definitions
  ├── Service Catalog portfolio
  ├── Data Council artifacts (policies, standards, SLOs)
  └── Operational tooling (CloudWatch dashboards, audit)

Per-Domain Producer Accounts (one per domain):
  ├── product-data        (orders, products, inventory)
  ├── finance-data        (invoices, revenue, GL)
  ├── hr-data             (people, payroll — restricted)
  ├── marketing-data      (campaigns, ads)
  └── engineering-data    (telemetry, deploys, incidents)

Consumer Accounts (per-team):
  ├── analytics           (cross-domain reads)
  ├── ml-platform         (training data)
  ├── reporting           (BI dashboards)
  └── data-science        (ad-hoc analysis)

All federated via IAM Identity Center.
Cross-account access via:
  - Lake Formation grants (per LF-Tag expression)
  - RAM share for Glue Catalog
  - DataZone subscriptions (workflow)
```

### 5.2 Domain account onboarding

When a new domain is onboarded:
1. AFT (Account Factory for Terraform — see ENTERPRISE_CONTROL_TOWER) provisions account
2. Central account creates LF-Tag binding for the domain
3. Service Catalog products deployed to domain (self-serve)
4. DataZone Domain extended (or new sub-domain)
5. Domain team given Permission Set in IDC
6. Onboarding doc + 1-week kickoff

---

## 6. Data product SLOs

```yaml
# data-products/orders.yaml — published with each data product
apiVersion: data-mesh/v1
data_product:
  name: orders
  domain: product
  owner: orders-team@acme.com
  description: All order transactions; updated near-real-time.
  
  schema:
    table: prod_orders.orders
    primary_key: order_id
    columns: ...
  
  classification:
    pii: true
    sensitivity: confidential
    retention: 7-years
  
  slo:
    freshness: 1h           # data must be < 1h old
    completeness: 0.99       # 99% of records non-null
    availability: 99.9       # 99.9% uptime for queries
    latency_p99_ms: 5000     # 5s p99 for typical query
  
  contracts:
    - schema_evolution: backward-compatible only
    - breaking-changes: 90-day deprecation notice
  
  consumers:
    - analytics-team
    - finance-team
    - ml-team
  
  upstream: 
    - prod_orders.orders_raw  (DMS from Aurora)
  downstream:
    - prod_analytics.daily_revenue  (analytics-team derived)
    - ml-features.purchase-history  (ml-team derived)
  
  health_dashboard: https://dashboards.example.com/data-products/orders
  on-call: pagerduty://orders-team-data
```

### 6.1 SLO monitoring

```python
# stacks/data_product_slo_stack.py
# CloudWatch dashboards + alarms per data product
# Metrics:
#   - DataProduct.Freshness (minutes since last update)
#   - DataProduct.Completeness (% non-null)
#   - DataProduct.AvailableForQuery (1/0)
#   - DataProduct.QueryLatencyP99
#
# Alarms:
#   - Freshness > SLO → SNS to producer team
#   - Availability < SLO → page producer + escalate
#   - Completeness < SLO → ETL flag, possibly halt
```

---

## 7. Common gotchas

- **Don't try mesh too early** — < 10 data sources, central lake is fine. Mesh adds overhead.
- **Domains must have data eng capacity** — without staffing, mesh fails (central platform becomes bottleneck again).
- **Federated governance is hard** — central council defines policy; domains must enforce. Without buy-in, mesh devolves to anarchy.
- **Cross-account complexity** — multi-account architecture needs Control Tower + AFT + governance baseline. Not a 1-week project.
- **LF-Tag taxonomy** — define carefully upfront. Renaming tags is painful; restructuring is brutal.
- **Service Catalog blueprint maintenance** — central platform team owns. Without dedicated owner, blueprints rot.
- **Cross-domain dependencies** — cyclic ownership ("orders depends on customer; customer team needs orders for analytics") requires governance.
- **SLO observability cost** — running quality checks per product per hour at scale = $$$. Sample where possible.
- **DataZone vs custom catalog** — DataZone takes 6-12 months to fully adopt; teams resist new tools. Plan change management.
- **Data product retirement** — when a product is deprecated, consumer subscriptions must be wound down. No automatic retirement workflow.
- **Schema evolution** — forward-compatible changes (add column nullable) OK; breaking changes (drop column) require coordination with all consumers.
- **OpenLineage** for cross-domain lineage — standard but requires emitter integration in every job. Phased rollout.

---

## 8. Pytest worked example

```python
# tests/test_data_mesh.py
import boto3, pytest

dz = boto3.client("datazone")
lf = boto3.client("lakeformation")
ram = boto3.client("ram")


def test_lf_tags_defined(catalog_id):
    """Central LF-Tags must include PII, Domain, Classification, DataClass."""
    tags = lf.list_lf_tags(CatalogId=catalog_id)["LFTags"]
    keys = [t["TagKey"] for t in tags]
    required = ["PII", "Domain", "Classification", "DataClass"]
    for k in required:
        assert k in keys


def test_glue_catalog_shared_with_domain_accounts(catalog_id, domain_account_id):
    """Central Glue Catalog must be RAM-shared to domain accounts."""
    shares = ram.get_resource_share_invitations()["resourceShareInvitations"]
    glue_share = [s for s in shares if "glue" in s["resourceShareName"].lower()]
    assert glue_share


def test_domain_can_access_only_own_data(domain_account_id, sample_role_arn):
    """A finance-team role cannot SELECT from product domain tables."""
    # Use IAM simulator
    iam = boto3.client("iam")
    sim = iam.simulate_principal_policy(
        PolicySourceArn=sample_role_arn,
        ActionNames=["lakeformation:GetDataAccess"],
        ResourceArns=[
            "arn:aws:glue:us-east-1:central:database/prod_orders",  # cross-domain
        ],
    )
    # Should be DENIED
    assert sim["EvaluationResults"][0]["EvalDecision"] == "explicitDeny"


def test_data_product_has_slo_defined(product_yaml_path):
    """Every published data product must have SLO YAML."""
    import yaml
    with open(product_yaml_path) as f:
        product = yaml.safe_load(f)
    slo = product["data_product"]["slo"]
    assert slo.get("freshness")
    assert slo.get("availability")
    assert slo.get("completeness")


def test_subscriber_governance_check(domain_id, asset_id):
    """Active subscriptions on PII-tagged assets must have approver record."""
    asset = dz.get_asset(domainIdentifier=domain_id, identifier=asset_id)
    if "PII" in [t["name"] for t in asset.get("glossaryTerms", [])]:
        subs = dz.list_subscriptions(
            domainIdentifier=domain_id,
            subscribedListingId=asset_id,
        )["items"]
        for s in subs:
            assert s.get("approvedBy"), f"Subscription {s['id']} on PII asset has no approver"
```

---

## 9. Five non-negotiables

1. **Per-domain AWS accounts** for Stage 4+ — no shared accounts across domains.
2. **Central LF-Tag taxonomy** locked at start — domain, classification, PII, data-class.
3. **Service Catalog blueprints** for self-serve — domains can't bypass governance.
4. **Data product SLO** defined for every published asset — freshness + availability + completeness.
5. **Cross-domain subscriptions** require approval workflow (DataZone) — no direct LF grants.

---

## 10. References

- [Data Mesh by Zhamak Dehghani](https://www.oreilly.com/library/view/data-mesh/9781492092384/)
- [Data Mesh on AWS — Reference Architecture](https://aws.amazon.com/blogs/big-data/design-a-data-mesh-architecture-using-aws-lake-formation-and-aws-glue/)
- [DataZone for Data Mesh](https://docs.aws.amazon.com/datazone/latest/userguide/data-mesh.html)
- [LF-Tag based access](https://docs.aws.amazon.com/lake-formation/latest/dg/tag-based-access-control.html)
- [Service Catalog blueprints](https://docs.aws.amazon.com/servicecatalog/latest/adminguide/introduction.html)
- [OpenLineage](https://openlineage.io/)

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-28 | Initial. Data mesh principles + 5-stage maturity + federated catalog + multi-account + LF-Tag ABAC + Service Catalog blueprints + data product SLOs. Wave 19. |
