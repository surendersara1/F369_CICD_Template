# SOP — Amazon DataZone (domains, projects, data products, subscriptions, data mesh)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `aws_cdk.aws_datazone` L1 · Amazon DataZone domain-centric model (2024 GA) · SSO / IAM Identity Center integration for user sign-in · Glue Catalog federation via `DataSource(GlueCatalog)` · Redshift + Athena subscription targets · Business glossary + classification · Amazon Q Generative integrations for NL search over data products

---

## 1. Purpose

- Provide the deep-dive for **Amazon DataZone** — AWS's managed **data mesh** service. Answers the enterprise question: "how do we enable multiple business units to own, publish, and subscribe to each other's data WITHOUT the central data team becoming a bottleneck?" Federates ownership, enforces governance, and exposes a Q-powered NL search UI across every product.
- Codify the **four-level hierarchy**: **Domain** (a DataZone tenant, usually one per org or per major business line) → **Environments** (runtime configurations for different compute: Redshift, Athena, EMR) → **Projects** (team workspaces — "Q3 Analytics", "Fraud Detection") → **Data Products** (curated, governed data assets — subsets of lakehouse tables, SQL views, doc links, APIs). Plus **Subscriptions** (cross-project requests with approval workflow).
- Codify the **five CDK constructs** — `CfnDomain`, `CfnEnvironmentBlueprintConfiguration`, `CfnProject`, `CfnDataSource`, `CfnSubscriptionTarget`. Everything else (assets, glossary terms, subscriptions) is managed via DataZone APIs at runtime, not CDK — because assets are data-team-owned artifacts, not infrastructure.
- Codify the **Glue Catalog bootstrap** — a DataZone `DataSource(type="GLUE")` scans a Glue database + tables on a schedule, auto-creates DataZone asset definitions, applies business glossary terms based on tags. This is how the lakehouse (governed in `DATA_LAKE_FORMATION`) becomes discoverable products.
- Codify the **subscription workflow** — a project requests access to another project's published product; the product owner approves (or auto-approves based on policy); DataZone provisions cross-account LF grants + RAM shares + Redshift datashares as appropriate. Subscription grants are LIVE — revoking the subscription removes the grant.
- Codify the **business glossary** — a taxonomy of terms (`Revenue`, `Customer`, `PII`) with definitions and synonyms; assets tag themselves with terms; the DataZone UI uses terms for faceted search. **Glossary terms are the semantic layer** that bridges technical schemas to business language; they're a better product-centric complement to `PATTERN_CATALOG_EMBEDDINGS`'s technical column comments.
- Codify the **integration with Waves 1–3 of this kit** — DataZone assets are **the consumer-facing face** of the lakehouse. Technical catalog (Glue) → governance (LF-Tags) → **business products (DataZone)** → AI access (Catalog embeddings / text-to-SQL / enterprise chat). A subscription approval can trigger LF-Tag assignment (automation pattern).
- Codify the **dual-mode identity** — DataZone requires IAM Identity Center (successor to AWS SSO) for user identity, NOT Cognito. Users are SSO users; project membership is SSO-group-based. Cognito-backed web apps consume DataZone APIs with IAM, not via native DataZone UI.
- Codify the **cost model** — DataZone is billed per domain per month (~$300/month for the free tier allowance in some regions); per subscription-request; per-Q-query via the Q integration. Start with one domain per account; scale to one per org.
- Include when the SOW signals: "data mesh", "federated data ownership", "data products", "business domain catalog", "DataZone", "data marketplace", "subscription-based data access", "multi-BU data sharing", "Q over data products".
- This partial is the **OPTIONAL BOLT-ON** for the AI-native lakehouse kit — default the kit to single-domain; enable DataZone when multi-domain/mesh is a requirement. Pairs with `DATA_LAKE_FORMATION` (grant enforcement), `DATA_GLUE_CATALOG` (asset source), `DATA_ATHENA` + `DATA_ICEBERG_S3_TABLES` (environments for subscribed compute).

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the domain + two projects + one Glue data source + one subscription target (Athena) | **§3 Monolith Variant** |
| `DataZoneStack` owns the domain + env-blueprint configs + default policies; per-domain `ProjectStack`s own projects + data sources; per-project Lambdas handle subscription workflow | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Domain creation is idempotent but destructive on delete.** A `CfnDomain` holds the asset + glossary + subscription state; deleting it wipes everything. Owners MUST be one team, one stack.
2. **Environment blueprints are account + region global.** An "AthenaQueryBlueprint" registered in one stack + re-registered in another fails with `AlreadyExistsException`. One owner.
3. **Projects + data sources evolve per-team**. Finance team owns `fin_project`; HR owns `hr_project`. Spreading across stacks lets each team iterate independently.
4. **Subscription approval Lambdas** live close to the project they gate. If `fin_project` has a custom auto-approval rule ("any request from `hr_project` → approve"), it's a per-project Lambda.
5. **Glossary terms are a shared taxonomy**. Owner: central governance team, likely the `DataZoneStack` owner.

Micro-Stack fixes by: (a) `DataZoneStack` owns the domain + environment blueprints + root glossary + IAM Identity Center association; (b) `ProjectStack`s read the domain ID via SSM + create their own projects + data sources + custom subscription rules; (c) consumers (agent, text-to-SQL, chat router) read product ARNs via SSM after runtime publish.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  Domain: "LakehouseCo" (CfnDomain)                               │
  │    identity_center_instance: arn:aws:sso:::instance/ssoins-...   │
  │    kms_key_identifier: local CMK                                 │
  │                                                                  │
  │  Environment Blueprints (account-global):                        │
  │    - AthenaBlueprint        (DataLakeEnvironment)                │
  │    - RedshiftBlueprint      (DataWarehouseEnvironment)           │
  │                                                                  │
  │  Environment Configurations (per-domain):                        │
  │    - FinAthenaEnv   (Athena workgroup + Glue db for fin project) │
  │    - HrAthenaEnv    (Athena workgroup + Glue db for hr  project) │
  │                                                                  │
  │  Business Glossary (root):                                       │
  │    Revenue       "Money from settled orders"   synonyms: [...]   │
  │    Customer      "Paying entity"               synonyms: [...]   │
  │    PII           "Personal data"               synonyms: [...]   │
  │                                                                  │
  │  Projects:                                                       │
  │    - fin_project                                                 │
  │        members: finance-team                                     │
  │        environments: [FinAthenaEnv]                              │
  │        data sources:                                             │
  │          - fin_glue (CfnDataSource — scans lakehouse_prod)       │
  │        published products:                                       │
  │          - "Revenue Analytics" (fact_revenue + dim_customer)     │
  │                                                                  │
  │    - hr_project                                                  │
  │        members: hr-team                                          │
  │        environments: [HrAthenaEnv]                               │
  │        data sources:                                             │
  │          - hr_glue                                               │
  │        published products:                                       │
  │          - "Headcount Metrics" (dim_employee + fact_payroll)     │
  │                                                                  │
  │  Subscriptions (runtime, not CDK):                               │
  │    - fin_project subscribes to hr_project."Headcount Metrics"    │
  │      → approval workflow → LF grant provisioned                  │
  └──────────────────────────────────────────────────────────────────┘
                                │
                                │  DataZone Portal UI + APIs
                                ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Users (IAM Identity Center)                                     │
  │    alice@corp  — fin-team                                        │
  │    bob@corp    — hr-team                                         │
  │    charlie@corp — platform (multi-project admin)                 │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_datazone()` method body

```python
from pathlib import Path
from aws_cdk import (
    CfnOutput, RemovalPolicy, Stack,
    aws_datazone as dz,
    aws_iam as iam,
    aws_kms as kms,
)


def _create_datazone(self, stage: str) -> None:
    """Monolith variant. Assumes self.{glue_db_name, athena_workgroup_name,
    identity_center_instance_arn} exist. IAM Identity Center MUST be set up
    account-wide before this runs (cannot be CDK'd cleanly)."""

    aws_account_id = Stack.of(self).account
    region         = Stack.of(self).region

    # A) Local CMK for DataZone encryption (assets + audit events).
    self.dz_cmk = kms.Key(
        self, "DzCmk",
        alias=f"alias/{{project_name}}-datazone-{stage}",
        enable_key_rotation=True,
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
    )

    # B) Domain execution role — DataZone assumes this to provision
    #    resources (LF grants, Redshift datashares, Glue asset reads).
    dz_exec_role = iam.Role(
        self, "DzDomainExecRole",
        assumed_by=iam.ServicePrincipal("datazone.amazonaws.com"),
        role_name=f"{{project_name}}-dz-exec-{stage}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonDataZoneDomainExecutionRolePolicy"
            ),
        ],
    )
    dz_exec_role.add_to_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.dz_cmk.key_arn],
    ))

    # C) Domain.
    self.dz_domain = dz.CfnDomain(
        self, "DzDomain",
        name=f"{{project_name}}-{stage}",
        domain_execution_role=dz_exec_role.role_arn,
        kms_key_identifier=self.dz_cmk.key_arn,
        single_sign_on=dz.CfnDomain.SingleSignOnProperty(
            type="IAM_IDC",                        # Identity Center
            user_assignment="AUTOMATIC",           # IDC group ⇒ DataZone member
        ),
        description=(
            "LakehouseCo data mesh domain — finance, hr, product analytics."
        ),
        tags=[
            {"key": "environment", "value": stage},
            {"key": "owner",       "value": "platform-data-team"},
        ],
    )
    self.dz_domain.node.add_dependency(dz_exec_role)

    # D) Environment Blueprint Configuration — activate the Athena
    #    blueprint for this domain. Blueprints are AWS-owned templates;
    #    the configuration binds the blueprint to the domain with
    #    environment-creation parameters.
    athena_blueprint = dz.CfnEnvironmentBlueprintConfiguration(
        self, "DzAthenaBlueprint",
        domain_identifier=self.dz_domain.attr_id,
        environment_blueprint_identifier="DefaultDataLake",   # AWS-owned
        enabled_regions=[region],
        manage_access_role_arn=dz_exec_role.role_arn,
        provisioning_role_arn=dz_exec_role.role_arn,
        regional_parameters=[
            dz.CfnEnvironmentBlueprintConfiguration.RegionalParameterProperty(
                region=region,
                parameters={
                    "S3Location": f"s3://{self.lake_bucket_name}/datazone/",
                },
            ),
        ],
    )
    athena_blueprint.add_dependency(self.dz_domain)

    redshift_blueprint = dz.CfnEnvironmentBlueprintConfiguration(
        self, "DzRedshiftBlueprint",
        domain_identifier=self.dz_domain.attr_id,
        environment_blueprint_identifier="DefaultDataWarehouse",
        enabled_regions=[region],
        manage_access_role_arn=dz_exec_role.role_arn,
        provisioning_role_arn=dz_exec_role.role_arn,
    )
    redshift_blueprint.add_dependency(self.dz_domain)

    # E) Finance project.
    self.fin_project = dz.CfnProject(
        self, "FinProject",
        domain_identifier=self.dz_domain.attr_id,
        name=f"fin_project_{stage}",
        description="Finance analytics + revenue products.",
    )
    self.fin_project.add_dependency(self.dz_domain)

    # F) Glue data source under fin_project — scans lakehouse_prod.
    #    The data source creates DataZone assets from Glue tables.
    self.fin_glue_ds = dz.CfnDataSource(
        self, "FinGlueDataSource",
        domain_identifier=self.dz_domain.attr_id,
        project_identifier=self.fin_project.attr_id,
        name="fin_glue_catalog",
        type="GLUE",
        description=(
            "Glue data catalog ingest — finance lakehouse tables"
        ),
        configuration=dz.CfnDataSource.DataSourceConfigurationInputProperty(
            glue_run_configuration=dz.CfnDataSource.GlueRunConfigurationInputProperty(
                data_access_role=dz_exec_role.role_arn,
                relational_filter_configurations=[
                    dz.CfnDataSource.RelationalFilterConfigurationProperty(
                        database_name=f"lakehouse_{stage}",
                        schema_name="",
                        filter_expressions=[
                            # Only ingest tables that start with fact_ or dim_.
                            dz.CfnDataSource.FilterExpressionProperty(
                                type="INCLUDE",
                                expression="fact_*",
                            ),
                            dz.CfnDataSource.FilterExpressionProperty(
                                type="INCLUDE",
                                expression="dim_*",
                            ),
                        ],
                    ),
                ],
            ),
        ),
        enable_setting="ENABLED",
        publish_on_import=True,            # auto-publish assets on creation
        recommendation=dz.CfnDataSource.RecommendationConfigurationProperty(
            enable_business_name_generation=True,   # Q suggests friendly names
        ),
        schedule=dz.CfnDataSource.ScheduleConfigurationProperty(
            schedule="cron(0 2 * * ? *)",   # daily 02:00 UTC
            timezone="UTC",
        ),
    )
    self.fin_glue_ds.add_dependency(self.fin_project)

    # G) HR project + data source (abbreviated — same shape).
    self.hr_project = dz.CfnProject(
        self, "HrProject",
        domain_identifier=self.dz_domain.attr_id,
        name=f"hr_project_{stage}",
        description="HR analytics + headcount products.",
    )

    self.hr_glue_ds = dz.CfnDataSource(
        self, "HrGlueDataSource",
        domain_identifier=self.dz_domain.attr_id,
        project_identifier=self.hr_project.attr_id,
        name="hr_glue_catalog",
        type="GLUE",
        configuration=dz.CfnDataSource.DataSourceConfigurationInputProperty(
            glue_run_configuration=dz.CfnDataSource.GlueRunConfigurationInputProperty(
                data_access_role=dz_exec_role.role_arn,
                relational_filter_configurations=[
                    dz.CfnDataSource.RelationalFilterConfigurationProperty(
                        database_name=f"hr_lakehouse_{stage}",
                        schema_name="",
                        filter_expressions=[
                            dz.CfnDataSource.FilterExpressionProperty(
                                type="INCLUDE", expression="dim_*",
                            ),
                            dz.CfnDataSource.FilterExpressionProperty(
                                type="INCLUDE", expression="fact_*",
                            ),
                        ],
                    ),
                ],
            ),
        ),
        enable_setting="ENABLED",
        publish_on_import=True,
    )

    # H) Subscription target — "where this project can deliver data TO".
    #    A project subscribing to another project's product needs a target
    #    (an Athena environment, a Redshift DB, etc.) where the data is
    #    exposed. This L1 binds the subscription target Athena environment.
    #    (Environments are typically created post-deploy in the DataZone
    #    portal; an L1 is available but verbose. Abbreviated here.)

    # I) Outputs.
    CfnOutput(self, "DzDomainId",      value=self.dz_domain.attr_id)
    CfnOutput(self, "DzDomainArn",     value=self.dz_domain.attr_arn)
    CfnOutput(self, "FinProjectId",    value=self.fin_project.attr_id)
    CfnOutput(self, "HrProjectId",     value=self.hr_project.attr_id)
    CfnOutput(self, "DzPortalUrl",     value=self.dz_domain.attr_portal_url)
```

### 3.3 Runtime — business glossary + asset publish + subscription

Most DataZone product-lifecycle actions are runtime (via `datazone` boto3 client), NOT CDK. Representative flows:

```python
# lambda/dz_glossary_bootstrap/handler.py
"""
Create the root business glossary + top-level terms. Run once at bootstrap,
or on every deploy (idempotent via PutGlossary with the same ID).
"""
import os, boto3

dz = boto3.client("datazone")
DOMAIN_ID = os.environ["DOMAIN_ID"]


def _get_or_create_glossary(project_id: str, name: str) -> str:
    # Search existing — DataZone returns a paginated list.
    paginator = dz.get_paginator("search")
    for page in paginator.paginate(
        domainIdentifier=DOMAIN_ID,
        searchScope="GLOSSARY",
        searchText=name,
    ):
        for item in page.get("items", []):
            if item["glossaryItem"]["name"] == name:
                return item["glossaryItem"]["id"]
    resp = dz.create_glossary(
        domainIdentifier=DOMAIN_ID,
        owningProjectIdentifier=project_id,
        name=name,
        description=f"Business glossary for {name}",
        status="ENABLED",
    )
    return resp["id"]


def _get_or_create_term(glossary_id: str, name: str,
                         short_desc: str, long_desc: str) -> str:
    try:
        resp = dz.create_glossary_term(
            domainIdentifier=DOMAIN_ID,
            glossaryIdentifier=glossary_id,
            name=name,
            shortDescription=short_desc,
            longDescription=long_desc,
            status="ENABLED",
        )
        return resp["id"]
    except dz.exceptions.ConflictException:
        # Term exists — fetch via search.
        for page in dz.get_paginator("search").paginate(
            domainIdentifier=DOMAIN_ID,
            searchScope="GLOSSARY_TERM",
            searchText=name,
        ):
            for item in page.get("items", []):
                g = item["glossaryTermItem"]
                if g["name"] == name and g["glossaryId"] == glossary_id:
                    return g["id"]
        raise


def lambda_handler(event, _ctx):
    platform_project_id = event["platform_project_id"]
    gid = _get_or_create_glossary(platform_project_id, "Core Business Terms")

    for name, short, long_ in [
        ("Revenue",  "Money from settled orders.",
                     "Sum of fact_revenue.amount in USD. Excludes tax + shipping."),
        ("Customer", "Paying entity.",
                     "Identifiable via dim_customer.customer_id (UUIDv4)."),
        ("PII",      "Personal identifying information.",
                     "Columns tagged sensitivity=pii in LF. Require explicit subscription."),
        ("Renewal",  "Customer contract renewal event.",
                     "dim_customer.renewal_date within the current quarter."),
    ]:
        _get_or_create_term(gid, name, short, long_)

    return {"glossary_id": gid, "terms_seeded": 4}
```

### 3.4 Runtime — subscription approval automation

When a project requests a product, a `SubscriptionRequest` is created; the product owner must accept/reject. Automate:

```python
# lambda/dz_subscription_auto_approver/handler.py
"""
Event-driven: EB rule matches 'Subscription Request Created' events from
aws.datazone. If the product's auto-approval policy allows, we accept.
"""
import os, boto3

dz = boto3.client("datazone")
AUTO_APPROVE_DOMAINS = set(os.environ["AUTO_APPROVE_DOMAINS"].split(","))


def lambda_handler(event, _ctx):
    """event.detail = {
      'requestIdentifier': '...', 'domainIdentifier': '...',
      'subscribedListings': [{'item':{'listingRevision':{'revisionId':'...', ...}}}],
      'subscribedPrincipals': [{'project':{'id':'...', 'name':'fin_project_prod'}}]
    }"""
    detail = event["detail"]
    req_id     = detail["requestIdentifier"]
    domain_id  = detail["domainIdentifier"]
    principal  = detail["subscribedPrincipals"][0]["project"]["name"]

    # Project-name-based auto-approve: only `fin_project_*` and `hr_project_*`.
    if any(principal.startswith(p) for p in AUTO_APPROVE_DOMAINS):
        dz.accept_subscription_request(
            domainIdentifier=domain_id,
            identifier=req_id,
            decisionComment="Auto-approved by policy.",
        )
        return {"accepted": True, "principal": principal}

    # Otherwise — leave as PENDING for human approval.
    return {"accepted": False, "principal": principal, "reason": "needs-human-review"}
```

EB rule:

```python
events.Rule(
    self, "DzSubscriptionCreatedRule",
    description="Fire on DataZone subscription requests.",
    event_pattern=events.EventPattern(
        source=["aws.datazone"],
        detail_type=["Subscription Request Created"],
        detail={"domainIdentifier": [self.dz_domain.attr_id]},
    ),
    targets=[targets.LambdaFunction(self.auto_approver_fn)],
)
```

### 3.5 Monolith gotchas

1. **IAM Identity Center is a prerequisite**, NOT CDK-deployable cleanly. An administrator must enable Identity Center in the org, set up user/group sync, and obtain the instance ARN BEFORE running this stack. The CDK synth will fail at apply time if the ARN is invalid.
2. **Domain-execution role is service-linked by convention but not automatic.** Use the managed policy `AmazonDataZoneDomainExecutionRolePolicy` + custom KMS grants. Do NOT reuse the CI deploy role — DataZone's trust policy requires a specific service principal.
3. **`CfnDomain.attr_portal_url` returns the SSO sign-in URL.** Users need IDC group membership that maps to a DataZone role (Owner / Contributor / Viewer) — assign via `AssociateRoleToPermissionSet` at the IDC level or via DataZone's own IAM mapping API.
4. **Glue data source MUST reference a Glue database in the SAME ACCOUNT**. Cross-account Glue data-source ingestion requires creating a DataZone `CrossAccountGlueDataSource` — different CFN shape, extra IAM. Default monolith assumes same-account.
5. **`publish_on_import=True` auto-publishes every imported table as a product.** For a large catalog (500+ tables), this is noisy. Flip to `False` + have project owners selectively publish via the portal UI.
6. **Subscription targets MUST exist before subscriptions are approved.** Creating a DataZone environment for a project is the runtime equivalent of "make this project ready to receive data". Script via `datazone:CreateEnvironment` post-deploy or let the portal auto-prompt users.
7. **Business glossary is scoped to ONE project (the "owning project").** Typically a dedicated `platform_project` owns the root glossary; other projects inherit. Pick this project carefully — it becomes privileged.
8. **Delete is destructive.** `cdk destroy` on DataZoneStack wipes the domain + all assets + subscriptions. Prod domains: `RemovalPolicy.RETAIN` on the `CfnDomain` (CFN will orphan it but keep data).
9. **DataZone Q integration is SEPARATE from QuickSight Q.** Enable via `datazone:UpdateDomainIntegration` with `GENERATIVE_AI` integration; requires Bedrock access + opt-in. Cost: per-query.
10. **Cross-region data sources** are not supported today (April 2026). Pick one region per domain.

---

## 4. Micro-Stack Variant

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** — on auto-approver Lambda + glossary bootstrap Lambda entries.
2. **Identity-side grants** — per-project Lambdas grant themselves `datazone:*` on SSM-read domain ID; NEVER reach into the DataZone domain from consumer stacks.
3. **`CfnRule` cross-stack EventBridge** — subscription-request EB rule lives in the owning ProjectStack (or platform stack); target Lambda same-stack.
4. **Same-stack bucket + OAC** — N/A.
5. **KMS ARNs as strings** — the `DzCmk.key_arn` is SSM-published.

### 4.2 DataZoneStack + ProjectStack split

```python
# stacks/datazone_stack.py
# Owns: CfnDomain, exec role, CMK, environment blueprints, root glossary
# bootstrap Lambda, default IDC association.
# Publishes: domain_id, domain_arn, platform_project_id, dz_cmk_arn.

# stacks/fin_project_stack.py
# Reads: domain_id, platform_project_id, dz_cmk_arn (via SSM).
# Creates: fin_project, fin_glue_data_source, project-specific glossary
# terms, project-specific subscription-approval Lambda + EB rule.

# stacks/hr_project_stack.py
# Same shape as fin_project_stack.

# stacks/agent_consumer_stack.py
# Reads the DZ domain_id and uses datazone APIs to search for assets by
# glossary terms; passes discovered ARNs into text-to-SQL / chat router
# as a supplementary signal.
```

### 4.3 Micro-stack gotchas

- **Domain deletion is account-global**. DataZoneStack ownership ⇒ platform team ONLY. Others submit IaC PRs to that repo.
- **Subscription grants propagate asynchronously.** A project subscribes → DataZone provisions LF grants ~30-90 s later. Consumer apps (chat router) that subscribe programmatically must wait + retry on first queries.
- **Project environment IDs** are needed for subscription targets. Environment creation is typically console-driven; CDK can create them via `CfnEnvironment` but the shape is verbose.
- **Glossary term ownership is sticky**. Terms owned by `fin_project` are visible to all but only editable by finance. Moving a term to platform ownership is not a CDK operation.

---

## 5. Swap matrix

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Data-mesh framework | DataZone (this) | Collibra | Enterprise governance heavyweight; better workflow + stewardship; costs more; not AWS-native. |
| Data-mesh framework | DataZone | Lake Formation alone | Simpler; lose portal UX, glossary, subscription workflow. Use for accounts < 3 domains. |
| Identity | IAM Identity Center | AWS SSO legacy | Legacy; no new features. Migrate. |
| Identity | IAM Identity Center | Cognito | DataZone does NOT support Cognito natively. Access via API only, not portal. |
| Catalog ingest | Glue data source | Redshift data source | Existing Redshift warehouse as source; same shape. |
| Catalog ingest | Glue data source | Custom data source via `datazone:CreateAssetType` + Lambda | Non-Glue sources (Salesforce, Snowflake) — advanced. |
| Publish | `publish_on_import=True` (auto) | Manual publish per asset | Large catalog — reduce noise. |
| Glossary | DataZone native | Import from external (Alation, Collibra) via API | Existing enterprise taxonomy; periodic sync. |
| Subscription approval | Auto-approver Lambda | Human-only via portal | High-sensitivity domains (legal, finance-exec). |
| Q integration | DataZone Q | PATTERN_ENTERPRISE_CHAT_ROUTER delegating to DataZone search API | Custom UX; no per-user DataZone-Q licence cost. |
| Cross-account | Same-account Glue | Cross-account Glue data source | Multi-account org; extra IAM. |
| Multi-region | Single region | One domain per region | No cross-region products; requires domain-to-domain sharing (no native support yet). |

---

## 6. Worked example

```python
# tests/test_datazone_synth.py
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.datazone_stack import DataZoneStack


def test_synth_domain_projects_glue_sources():
    app = cdk.App()
    stack = DataZoneStack(app, "DZ-dev", stage="dev")
    tpl = Template.from_stack(stack)

    # Domain with IDC sso.
    tpl.has_resource_properties("AWS::DataZone::Domain", {
        "Name": "{project_name}-dev",
        "SingleSignOn": Match.object_like({
            "Type": "IAM_IDC",
            "UserAssignment": "AUTOMATIC",
        }),
    })

    # Two blueprints configured.
    tpl.resource_count_is("AWS::DataZone::EnvironmentBlueprintConfiguration", 2)

    # Fin + HR projects + 2 Glue data sources.
    tpl.resource_count_is("AWS::DataZone::Project", 2)
    tpl.resource_count_is("AWS::DataZone::DataSource", 2)
    tpl.has_resource_properties("AWS::DataZone::DataSource", {
        "Name": "fin_glue_catalog",
        "Type": "GLUE",
        "PublishOnImport": True,
        "Schedule": Match.object_like({"Schedule": "cron(0 2 * * ? *)"}),
    })


# tests/test_integration_glossary.py
"""Integration — bootstrap glossary, search for a term, create a subscription
request, assert auto-approve."""
import os, boto3, pytest


@pytest.mark.integration
def test_glossary_terms_seeded_and_searchable():
    dz = boto3.client("datazone")
    domain_id = os.environ["DZ_DOMAIN_ID"]
    resp = dz.search(
        domainIdentifier=domain_id,
        searchScope="GLOSSARY_TERM",
        searchText="Revenue",
    )
    names = [i["glossaryTermItem"]["name"] for i in resp.get("items", [])]
    assert "Revenue" in names
    assert "Customer" in names
```

---

## 7. References

- AWS docs — *Amazon DataZone user guide* (domain, project, data products, subscriptions).
- AWS docs — *DataZone CFN reference* (`CfnDomain`, `CfnEnvironmentBlueprintConfiguration`, `CfnProject`, `CfnDataSource`).
- AWS docs — *DataZone IAM Identity Center integration*.
- AWS docs — *DataZone environment blueprints (DefaultDataLake, DefaultDataWarehouse)*.
- `DATA_GLUE_CATALOG.md` — source of truth for Glue data sources.
- `DATA_LAKE_FORMATION.md` — provisioning layer DataZone uses for grants.
- `DATA_ATHENA.md` / `DATA_ICEBERG_S3_TABLES.md` — compute environments consumed by DataZone subscription targets.
- `PATTERN_ENTERPRISE_CHAT_ROUTER.md` — can supplement product-level grounding by searching DataZone for glossary matches.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables.

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Dual-variant SOP. Domain → environment blueprints → projects → Glue data sources. IAM Identity Center prerequisite. Business glossary bootstrap Lambda + subscription auto-approver. DataZone-Q integration flagged. Multi-project ownership pattern (fin + hr). 10 monolith gotchas, 4 micro-stack gotchas, 12-row swap matrix, pytest synth + glossary integration harness.
