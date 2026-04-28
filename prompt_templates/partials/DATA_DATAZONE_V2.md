# SOP — Amazon DataZone v2 (governed data sharing · business glossary · domains/projects/environments · subscriptions · lineage)

**Version:** 2.0 · **Last-reviewed:** 2026-04-28 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon DataZone (GA Sept 2023; v2 enhancements 2024) · Domains + Projects + Environments + Data products · Business Glossary · Subscription requests workflow · Asset publishing + lineage · Lake Formation integration · IAM Identity Center federation

---

## 1. Purpose

- Codify **DataZone** as the canonical AWS-native data governance + sharing platform. Replaces ad-hoc Confluence-based data catalogs with a governed marketplace.
- Codify the **3-tier hierarchy**: **Domain** (organizational boundary) → **Project** (team workspace) → **Environment** (data + compute + tooling).
- Codify **data products** — published assets (tables, models, dashboards) with metadata + glossary terms + ownership.
- Codify **subscription workflow** — consumer requests access → producer approves → AWS LF + IAM grants applied automatically.
- Codify **business glossary** — domain-level terms + relationships (PII, PHI, customer-data, financial-data tags).
- Codify **automated lineage** — DataZone tracks upstream/downstream from Glue jobs, Spark scripts, and Athena queries.
- Codify **IDC integration** — single sign-on via existing IAM Identity Center.
- Pairs with `DATA_LAKE_FORMATION` (auth backend), `DATA_GLUE_CATALOG` (table metadata), `DATA_GLUE_QUALITY` (DQ on assets), `DATA_MESH_PATTERNS` (org-wide).

When the SOW signals: "data governance", "data marketplace", "governed self-service", "business glossary", "data subscriptions", "data mesh on AWS".

---

## 2. Decision tree — DataZone vs Lake Formation alone

| Need | DataZone | Lake Formation alone |
|---|:---:|:---:|
| Self-service catalog UI for non-tech users | ✅ | ❌ |
| Subscription workflow (request → approve → grant) | ✅ | ⚠️ manual via tickets |
| Business glossary | ✅ | ❌ |
| Producer-consumer separation | ✅ projects | ⚠️ via accounts |
| LF-Tag-based access (TBAC) | ✅ uses LF underneath | ✅ |
| Cross-account sharing | ✅ via LF + RAM | ✅ |
| Metadata enrichment (descriptions, ownership) | ✅ | ❌ |
| Lineage visualization | ✅ auto from sources | ⚠️ manual via lineage tools |
| Cost (governance overhead) | ⚠️ per project monthly | ✅ minimal |

**Recommendation:**
- **DataZone** for orgs > 50 data users + multiple producer/consumer teams.
- **Lake Formation alone** for small/centralized data teams.

```
DataZone hierarchy:

  Root Domain (e.g., acme.example.com)
      │
      ├── Domain: ProductDomain (per-business-unit)
      │     │
      │     ├── Project: orders-team (producers — they OWN data)
      │     │     │
      │     │     ├── Environment: orders-dev (Glue + Athena + Redshift)
      │     │     ├── Environment: orders-prod (Glue + Athena + Redshift)
      │     │     │
      │     │     └── Data Products (published from environments):
      │     │           - prod_orders.orders               (Iceberg table)
      │     │           - prod_orders.order_summary        (materialized view)
      │     │           - prod_orders.daily_revenue        (QuickSight dashboard)
      │     │
      │     └── Project: analytics-team (consumers)
      │           │
      │           ├── Environment: analytics-prod
      │           │
      │           └── Subscriptions (granted assets):
      │                 - prod_orders.orders     (read; LF-grant applied)
      │                 - prod_users.users        (read)
      │
      └── Domain: FinanceDomain (separate boundary)
            │
            └── (own projects + environments + data products)

  Business Glossary (domain-level):
    Customer (term)
      ├── synonyms: Client, Account-holder
      ├── related: User, Subscriber
      ├── tags: PII, customer-data
      ├── governance: must apply LF-Tag PII=true
      └── data products linked: customers, users, ...
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single domain + 2 projects + 5 data products | **§3 Monolith** |
| Production — 5+ domains + 20+ projects + glossary + lineage | **§5 Production** |

---

## 3. Monolith Variant — Domain + Projects + Environments + Data Products

### 3.1 CDK

```python
# stacks/datazone_stack.py
from aws_cdk import Stack
from aws_cdk import aws_datazone as dz
from aws_cdk import aws_iam as iam
from constructs import Construct


class DataZoneStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 idc_instance_arn: str,
                 admin_iam_role_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. DataZone Domain (root) ────────────────────────────────
        domain = dz.CfnDomain(self, "Domain",
            name="acme",
            description="Acme corporate data marketplace",
            domain_execution_role=admin_iam_role_arn,
            single_sign_on=dz.CfnDomain.SingleSignOnProperty(
                type="IAM_IDC",
                user_assignment="MANUAL",                       # or AUTOMATIC
            ),
            kms_key_identifier=kms_key_arn,
        )

        # ── 2. Project (Producer team — orders) ──────────────────────
        orders_project = dz.CfnProject(self, "OrdersProject",
            domain_identifier=domain.attr_id,
            name="orders-team",
            description="Owners of orders data products",
            glossary_terms=[],                                   # link later
        )

        # ── 3. Environment Profile (template for environments) ──────
        # Uses Glue as data source + Athena as compute
        env_profile = dz.CfnEnvironmentProfile(self, "OrdersEnvProfile",
            domain_identifier=domain.attr_id,
            name="orders-glue-athena",
            project_identifier=orders_project.attr_id,
            environment_blueprint_identifier="DataLake",        # AWS-managed blueprint
            description="Glue + Athena for orders",
            aws_account_id=self.account,
            aws_account_region=self.region,
            user_parameters=[
                dz.CfnEnvironmentProfile.EnvironmentParameterProperty(
                    name="glueDbName", value="prod_orders",
                ),
                dz.CfnEnvironmentProfile.EnvironmentParameterProperty(
                    name="dataLakeBucket", value=data_bucket.bucket_name,
                ),
            ],
        )

        # ── 4. Environment (instance from profile) ──────────────────
        orders_env = dz.CfnEnvironment(self, "OrdersProdEnv",
            domain_identifier=domain.attr_id,
            project_identifier=orders_project.attr_id,
            environment_profile_identifier=env_profile.attr_id,
            name="orders-prod",
            description="Production orders environment",
        )

        # ── 5. Glossary (domain-level) ───────────────────────────────
        glossary = dz.CfnGlossary(self, "BusinessGlossary",
            domain_identifier=domain.attr_id,
            name="Business Glossary",
            description="Acme business terms",
            owning_project_identifier=orders_project.attr_id,
            status="ENABLED",
        )

        # Glossary terms
        customer_term = dz.CfnGlossaryTerm(self, "CustomerTerm",
            domain_identifier=domain.attr_id,
            glossary_identifier=glossary.attr_id,
            name="Customer",
            short_description="A person or entity who purchases from Acme",
            long_description="An entity that has at least one Order with Acme. Synonyms: Client, Account-holder. Includes individuals, companies, and partners. PII applies.",
            term_relations=dz.CfnGlossaryTerm.TermRelationsProperty(
                classifies=["PII", "customer-data"],
                is_a=["Person"],
            ),
        )

        order_term = dz.CfnGlossaryTerm(self, "OrderTerm",
            domain_identifier=domain.attr_id,
            glossary_identifier=glossary.attr_id,
            name="Order",
            short_description="A purchase request from a Customer",
            term_relations=dz.CfnGlossaryTerm.TermRelationsProperty(
                classifies=["financial-data", "customer-data"],
            ),
        )

        # ── 6. Project (Consumer team — analytics) ───────────────────
        analytics_project = dz.CfnProject(self, "AnalyticsProject",
            domain_identifier=domain.attr_id,
            name="analytics-team",
            description="Cross-functional analytics team",
        )

        # ── 7. Data Product (publishing an asset) ────────────────────
        # Note: Data products are typically published via DataZone UI
        # OR via API after data exists. CDK pre-provisions the project +
        # environment; data products are runtime concept.
        # 
        # For automation, use boto3 in CI/CD:
        # boto3.client("datazone").create_asset(
        #     domainIdentifier=domain_id,
        #     name="orders",
        #     typeIdentifier="amazon.datazone.GlueTableAssetType",
        #     formsInput=[{...}],
        #     glossaryTerms=[order_term.attr_id, customer_term.attr_id],
        #     ...
        # )
```

### 3.2 Publish a data product (after asset exists)

```python
import boto3
dz_client = boto3.client("datazone")

# 1. Discover Glue table → register as DataZone asset
asset = dz_client.create_asset(
    domainIdentifier=domain_id,
    owningProjectIdentifier=orders_project_id,
    name="orders",
    typeIdentifier="amazon.datazone.GlueTableAssetType",
    formsInput=[{
        "formName": "GlueTableForm",
        "content": json.dumps({
            "tableArn": f"arn:aws:glue:us-east-1:123:table/prod_orders/orders",
            "tableName": "orders",
            "databaseName": "prod_orders",
        }),
    }],
    description="Production orders table — one row per order",
    glossaryTerms=[order_term_id, customer_term_id],
)

# 2. Publish asset (makes it discoverable)
dz_client.create_listing_change_set(
    domainIdentifier=domain_id,
    entityIdentifier=asset["id"],
    entityType="ASSET",
    action="PUBLISH",
)

# Now visible in DataZone catalog UI
```

### 3.3 Subscription workflow (consumer requests access)

```bash
# Consumer (analytics-team) browses catalog → finds orders → "Subscribe"
# Workflow:
# 1. Consumer creates subscription request
aws datazone create-subscription-request \
  --domain-identifier $DOMAIN_ID \
  --request-reason "Build daily revenue dashboard" \
  --subscribed-listings ListingId=$ASSET_LISTING_ID \
  --subscribed-principals project=$ANALYTICS_PROJECT_ID

# 2. Producer (orders-team owner) reviews + approves
aws datazone accept-subscription-request \
  --domain-identifier $DOMAIN_ID \
  --identifier $REQUEST_ID

# 3. DataZone auto-grants:
#    - LF permissions on the Glue table
#    - IAM permissions in the consumer project's environment
#    - Subscription becomes ACTIVE
```

---

## 4. Lineage (automatic via DataZone)

DataZone auto-captures lineage from Glue jobs + Spark + Athena queries:

```python
# When a Glue ETL job runs:
#   Source: prod_raw.events
#   Transformation: Glue script
#   Target: prod_curated.events_aggregated
# DataZone:
#   - Detects via Glue job metadata
#   - Adds upstream/downstream edges in catalog
#   - Visualizes in Asset → Lineage tab
```

For custom (non-Glue) sources, OpenLineage emitters can publish events to DataZone API.

---

## 5. Production Variant — multi-domain + cross-account + LF integration

```python
# Multiple domains for org boundaries:
# - acme.product (product analytics)
# - acme.finance (finance + accounting)
# - acme.hr (people data, restricted)

# Cross-domain sharing — possible via subscription
# Cross-account sharing — DataZone uses LF + RAM under hood

# Setup:
# 1. Each domain has dedicated AWS account (recommended)
# 2. Domains federated via IDC
# 3. Cross-domain subscription requires admin approval (additional gate)
# 4. LF grants applied automatically across accounts
```

---

## 6. Common gotchas

- **DataZone is regional** — domains can't span regions. Multi-region orgs need multi-domain.
- **Project = team boundary** — keep boundaries clean; don't put cross-team users in one project.
- **Environment Profile is the template; Environment is the instance** — provision profile once, instances per env (dev/stage/prod).
- **Subscription approval workflow blocks at producer** — projects need active producer reviewer; otherwise requests stall.
- **Glossary terms** are powerful but require curation — start with 20-50 terms; grow from feedback.
- **PII tagging via glossary** — LF auto-applies PII tag on subscription if asset has glossary term marked PII.
- **DataZone uses LF + IAM under hood** — granting works only if Glue Catalog + LF are set up correctly. Pre-req.
- **Cost** — DataZone charges per environment + per project per month. ~$30-80/env/mo. Plan 50+ envs = $2K+/mo.
- **Cross-account** — domain in one account; data in another. Use account associations + LF cross-account.
- **API stability** — DataZone APIs evolving (CDK L1 only); pin SDK versions.
- **Lineage gaps** — Athena queries that don't read tables (e.g., raw S3 reads via SQL_GENERATED_AT_RUNTIME) won't track.
- **OpenLineage emitters** for non-Glue sources require integration; not auto.
- **Asset deletion vs unsubscribe** — deleting an asset breaks active subscriptions. Always unsubscribe consumers first.

---

## 7. Pytest worked example

```python
# tests/test_datazone.py
import boto3, pytest

dz = boto3.client("datazone")


def test_domain_active(domain_id):
    domain = dz.get_domain(identifier=domain_id)
    assert domain["status"] == "AVAILABLE"


def test_glossary_has_terms(domain_id, glossary_id):
    terms = dz.list_glossary_terms(
        domainIdentifier=domain_id, glossaryIdentifier=glossary_id,
    )["items"]
    assert len(terms) >= 5


def test_asset_published(domain_id, asset_id):
    asset = dz.get_asset(domainIdentifier=domain_id, identifier=asset_id)
    assert asset["status"] == "ACTIVE"
    
    # Verify it's listed (publishable)
    listings = dz.list_listings(
        domainIdentifier=domain_id, entityIdentifier=asset_id,
        entityType="ASSET",
    )["items"]
    assert listings


def test_subscription_active(domain_id, subscription_id):
    sub = dz.get_subscription(domainIdentifier=domain_id, identifier=subscription_id)
    assert sub["status"] == "APPROVED"


def test_pii_tagged_assets_have_lf_tag(domain_id, asset_id, kb_id):
    """If an asset has 'PII' glossary term, it must have LF PII tag."""
    asset = dz.get_asset(domainIdentifier=domain_id, identifier=asset_id)
    if "PII" in [t["name"] for t in asset.get("glossaryTerms", [])]:
        # Verify LF tag present on underlying Glue table
        lf = boto3.client("lakeformation")
        tags = lf.get_resource_lf_tags(
            Resource={"Table": {...}},
        )["LFTagOnDatabase"]
        assert any(t["TagKey"] == "PII" for t in tags)
```

---

## 8. Five non-negotiables

1. **IAM Identity Center as identity** — no DataZone-local users in production.
2. **CMK encryption** on domain — never AWS-owned key.
3. **Glossary curated for first 20-50 terms** before opening domain to users.
4. **PII glossary term auto-applies LF PII tag** on subscription — verify automation works.
5. **Subscription approver coverage** — every project must have an active reviewer; rotate.

---

## 9. References

- [Amazon DataZone User Guide](https://docs.aws.amazon.com/datazone/latest/userguide/what-is-datazone.html)
- [DataZone domains, projects, environments](https://docs.aws.amazon.com/datazone/latest/userguide/datazone-concepts.html)
- [Business glossary](https://docs.aws.amazon.com/datazone/latest/userguide/glossaries.html)
- [Subscriptions](https://docs.aws.amazon.com/datazone/latest/userguide/data-subscription-process.html)
- [Lineage](https://docs.aws.amazon.com/datazone/latest/userguide/data-lineage.html)
- [DataZone + Lake Formation](https://docs.aws.amazon.com/datazone/latest/userguide/setup-lake-formation.html)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-28 | Initial. DataZone domain + projects + environments + data products + business glossary + subscriptions + lineage + IDC integration. Wave 19. |
