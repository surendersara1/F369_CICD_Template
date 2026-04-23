# SOP — AWS Lake Formation governance (LF-TBAC, row/column security, cross-account)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2.238+ (Python 3.12+) · `aws_cdk.aws_lakeformation` L1 (no L2 in stable; alpha L2 `@aws-cdk/aws-lakeformation-alpha` exists but is churny) · Lake Formation data lake admin model · LF-Tags (LF-TBAC) · `CfnPrincipalPermissions` for third-generation grants · `CfnDataCellsFilter` for row/column filtering · Cross-account sharing via RAM · Glue Data Catalog + S3 Tables + self-managed Iceberg as governed resources

---

## 1. Purpose

- Provide the deep-dive for **AWS Lake Formation** as the **governance plane** over a lakehouse — the layer that turns "who can read `fact_revenue`?" from a tangle of S3 bucket policies + Glue catalog resource policies + IAM role policies into **one place**: LF-Tags attached to databases/tables/columns and `CfnPrincipalPermissions` granting principals tag-based access.
- Codify the **LF-TBAC (Tag-Based Access Control)** pattern — define `domain=finance`, `sensitivity=pii`, `environment=prod`; tag catalog resources; grant roles "SELECT on LF-Tag (domain=finance AND sensitivity!=pii)". Single source of truth. No per-resource role ARN mutation — scales to 1000s of tables.
- Codify **row-level and column-level filtering** via `CfnDataCellsFilter` — `"WHERE region = 'EMEA'"`, `excluded_column_names=["ssn","dob"]`. These are enforced by the query engine (Athena, Redshift Spectrum, EMR) at read time; users never see data they cannot access.
- Codify **cross-account sharing via RAM + LF** — share a database or table to a consumer account without copying data. Consumer account uses Athena/EMR to query the shared resource; all audit logs stay in the producer account's CloudTrail + LF access log.
- Codify the **registration contract** — a Lake Formation-governed S3 location requires `CfnResource` (register) + `CfnPermissions` or `CfnPrincipalPermissions` (grant). S3 Tables buckets require slightly different registration (`use_service_linked_role=True` + ResourceArn is the table bucket ARN). Self-managed Iceberg buckets use the data-lake-location registration.
- Codify the **3 grant-generation pattern** — Lake Formation has evolved through three permission models: **Gen-1** (IAM-plus-LF), **Gen-2** (`CfnPermissions`, tag-free), **Gen-3** (`CfnPrincipalPermissions` with LF-Tag expressions). This partial uses **Gen-3 exclusively** — `CfnPrincipalPermissions` is the only CDK construct that supports both resource-name grants AND tag-expression grants. Do NOT mix with `CfnPermissions`; the two models override each other's audit behavior unpredictably.
- Include when the SOW signals: "Lake Formation", "LF-Tags", "data governance", "column masking", "row-level security", "PII redaction", "cross-account data sharing", "multi-domain lake", "finance / HR / legal data separation", "data mesh governance", "audit-grade data access".
- This partial is the **governance overlay** for `DATA_ICEBERG_S3_TABLES`, `DATA_LAKEHOUSE_ICEBERG`, and `DATA_GLUE_CATALOG`. It owns the WHO and WHAT-CAN-SEE-WHAT decisions; the underlying data partials own the storage + format.

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| Single-domain lake — one `cdk.Stack` owns the S3 buckets + Glue databases + LF admin + LF-Tags + all grants + consumer role definitions | **§3 Monolith Variant** |
| Dedicated `GovernanceStack` owns LF admin config + LF-Tags + grants; separate `TableBucketStack` / `LakehouseStack` own storage + catalog; `ComputeStack` owns consumer roles that are **referenced by ARN** from Governance | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Lake Formation admin is an account-level singleton.** `CfnDataLakeSettings` mutates the Lake Formation service's admin list for the account. Running it in two stacks is a race — last-deploy-wins, audit nightmare. Keep it in exactly one place (Governance).
2. **`CfnPrincipalPermissions` references three ARNs**: the catalog resource (table / database / LF-tag expression), the grantee (IAM role or IAM user), and — for data-cell filters — the filter ARN. If all three are in different stacks, CDK naturally handles it (no cycles) **only if the principal ARN is a string/token, not an L2 `iam.Role` import**. Always pass role ARNs via SSM or pass them in via construct props.
3. **LF-Tags are account-global, not stack-scoped.** `CfnTag(tag_key="domain", tag_values=["finance","hr","legal"])` creates an account-level tag. Creating the same tag in two stacks makes the second deploy fail with "TagAlreadyExists". Owner: Governance stack.
4. **Cross-account sharing emits AWS RAM invitations.** The producer account shares via LF → LF creates a RAM share → consumer account must **accept** via the console or `ram:AcceptResourceShareInvitation`. Auto-accept is possible via the organization's RAM setting, but only for accounts in the same AWS Organization. For external accounts, the accept step is manual + out-of-band.
5. **Registered S3 locations are reference-counted implicitly.** Registering the same S3 prefix twice (e.g. `s3://my-bucket/` from Stack A and `s3://my-bucket/path/` from Stack B) leaves the broader one active and the narrower one shadowed. Register at the bucket root from exactly one place.

Micro-Stack variant fixes all of this by: (a) owning `CfnDataLakeSettings` + all `CfnTag` + all `CfnResource` (registered locations) + all `CfnPrincipalPermissions` in `GovernanceStack`; (b) consumer role ARNs come in via SSM or construct props (the grantee is a string); (c) resource ARNs come in via SSM (the table bucket, the Glue database, self-managed S3 paths); (d) LF-Tags are the contract — add one value to the `domain` LF-Tag and every principal that has tag-expression `domain IN (..., newvalue)` picks it up automatically at next query, no redeploy.

---

## 3. Monolith Variant

**Use when:** a single `cdk.Stack` holds storage + catalog + governance + one or two consumer roles. POC, single-domain pilot.

### 3.1 Architecture

```
  ┌──────────────────────────────────────────────────────────────────┐
  │   Lake Formation Data Lake Admin                                 │
  │     - Admin principals: CI deploy role, platform-owner role      │
  │     - CreateDatabaseDefaultPermissions = []                      │
  │     - CreateTableDefaultPermissions    = []   ← disables Gen-1   │
  │                                                                  │
  │   Registered S3 locations (governance enforced):                 │
  │     - arn:aws:s3tables:...:bucket/lakehouse-prod                 │
  │     - arn:aws:s3:::selfmanaged-lake-prod/iceberg/                │
  │                                                                  │
  │   LF-Tags (account-global):                                      │
  │     - domain:        [finance, hr, legal, product]               │
  │     - sensitivity:   [public, internal, confidential, pii]       │
  │     - environment:   [dev, stage, prod]                          │
  │                                                                  │
  │   Tagged catalog resources:                                      │
  │     fact_revenue       → domain=finance, sensitivity=internal    │
  │     dim_customer       → domain=finance, sensitivity=confidential│
  │     dim_customer.ssn   → sensitivity=pii  (column-level tag)     │
  │                                                                  │
  │   DataCellsFilters:                                              │
  │     finance_analyst_emea = row_filter(region='EMEA') +           │
  │                            excluded_column_names=['ssn']         │
  │                                                                  │
  │   Principal grants (Gen-3 CfnPrincipalPermissions):              │
  │     FinanceAnalystRole → SELECT on LF-Tag expression             │
  │                          (domain=finance AND sensitivity!=pii)   │
  │     DataEngineerRole   → ALL  on LF-Tag expression               │
  │                          (environment=dev)                       │
  │     CrossAcctRole      → SELECT on database/fact_revenue         │
  │                          via RAM share                           │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 CDK — `_create_lf_governance()` method body

```python
from aws_cdk import (
    CfnOutput, Stack,
    aws_iam as iam,
    aws_lakeformation as lf,
    aws_s3 as s3,
)


def _create_lf_governance(self, stage: str) -> None:
    """Monolith variant. Assumes self.{table_bucket, selfmanaged_lake_bucket,
    glue_db_name, ci_deploy_role_arn} already exist (or pass them in).

    Sets: data-lake admins, registered locations, LF-Tags, tag assignments,
    a data-cells filter, and Gen-3 principal permissions."""

    # A) Data-lake settings — this is the ACCOUNT-LEVEL singleton.
    #    Admins listed here can do anything in LF. CI deploy role must be in
    #    this list or every subsequent CfnPrincipalPermissions will fail with
    #    "AccessDeniedException: Principal is not a Lake Formation admin".
    #    CreateDatabase/TableDefaultPermissions = [] turns OFF the legacy
    #    "IAM-plus-LF" fallback — anyone without an explicit LF grant sees
    #    nothing. This is the only sane setting for governance-enforced lakes.
    self.lf_settings = lf.CfnDataLakeSettings(
        self, "DataLakeSettings",
        admins=[
            lf.CfnDataLakeSettings.DataLakePrincipalProperty(
                data_lake_principal_identifier=self.ci_deploy_role_arn,
            ),
            lf.CfnDataLakeSettings.DataLakePrincipalProperty(
                data_lake_principal_identifier=(
                    f"arn:aws:iam::{Stack.of(self).account}:role/PlatformOwner"
                ),
            ),
        ],
        create_database_default_permissions=[],
        create_table_default_permissions=[],
        # If you use the "hybrid" mode (IAMAllowedPrincipals fallback on for
        # unregistered locations), set mutation_type="REPLACE" on a first
        # deploy and "APPEND" on subsequent to avoid admin-list drift across
        # pipelines. Here we default to REPLACE — explicit ownership.
        mutation_type="REPLACE",
    )

    # B) Register the S3 locations that LF governs.
    #    For S3 Tables: resource ARN is the table-bucket ARN; service-linked
    #    role is Lake Formation's own (`AWSServiceRoleForLakeFormation`).
    #    For self-managed S3: use_service_linked_role=True unless you have a
    #    custom role for cross-region replication, in which case pass role_arn.
    self.lf_reg_tables = lf.CfnResource(
        self, "RegisterTableBucket",
        resource_arn=self.table_bucket_arn,           # arn:aws:s3tables:...
        use_service_linked_role=True,
    )
    self.lf_reg_selfmanaged = lf.CfnResource(
        self, "RegisterSelfManagedLake",
        resource_arn=f"{self.selfmanaged_lake_bucket.bucket_arn}/iceberg/",
        use_service_linked_role=True,
    )

    # C) LF-Tags. Creating a tag with values ["finance","hr"] creates the
    #    value set; adding "legal" later is a separate update that mutates
    #    the existing tag. Don't shadow a tag from another stack.
    self.tag_domain = lf.CfnTag(
        self, "TagDomain",
        tag_key="domain",
        tag_values=["finance", "hr", "legal", "product"],
    )
    self.tag_sensitivity = lf.CfnTag(
        self, "TagSensitivity",
        tag_key="sensitivity",
        tag_values=["public", "internal", "confidential", "pii"],
    )
    self.tag_environment = lf.CfnTag(
        self, "TagEnvironment",
        tag_key="environment",
        tag_values=["dev", "stage", "prod"],
    )

    # D) Assign tags to catalog resources.
    #    Association is via CfnTagAssociation. The resource_key identifies
    #    the target — database / table / column.
    lf.CfnTagAssociation(
        self, "TagFactRevenueDomain",
        lf_tags=[lf.CfnTagAssociation.LFTagPairProperty(
            catalog_id=Stack.of(self).account,
            tag_key="domain", tag_values=["finance"],
        )],
        resource=lf.CfnTagAssociation.ResourceProperty(
            table=lf.CfnTagAssociation.TableResourceProperty(
                catalog_id=Stack.of(self).account,
                database_name=self.glue_db_name,
                name="fact_revenue",
            ),
        ),
    )
    lf.CfnTagAssociation(
        self, "TagFactRevenueSensitivity",
        lf_tags=[lf.CfnTagAssociation.LFTagPairProperty(
            catalog_id=Stack.of(self).account,
            tag_key="sensitivity", tag_values=["internal"],
        )],
        resource=lf.CfnTagAssociation.ResourceProperty(
            table=lf.CfnTagAssociation.TableResourceProperty(
                catalog_id=Stack.of(self).account,
                database_name=self.glue_db_name,
                name="fact_revenue",
            ),
        ),
    )
    # Column-level tag: dim_customer.ssn → sensitivity=pii
    lf.CfnTagAssociation(
        self, "TagDimCustomerSsnPii",
        lf_tags=[lf.CfnTagAssociation.LFTagPairProperty(
            catalog_id=Stack.of(self).account,
            tag_key="sensitivity", tag_values=["pii"],
        )],
        resource=lf.CfnTagAssociation.ResourceProperty(
            table_with_columns=lf.CfnTagAssociation.TableWithColumnsResourceProperty(
                catalog_id=Stack.of(self).account,
                database_name=self.glue_db_name,
                name="dim_customer",
                column_names=["ssn"],
            ),
        ),
    )

    # E) DataCellsFilter — row + column filtering as a reusable object.
    self.filter_finance_emea = lf.CfnDataCellsFilter(
        self, "FilterFinanceEmea",
        table_catalog_id=Stack.of(self).account,
        database_name=self.glue_db_name,
        table_name="fact_revenue",
        name="finance_analyst_emea",
        row_filter=lf.CfnDataCellsFilter.RowFilterProperty(
            filter_expression="region = 'EMEA'",
        ),
        column_wildcard=lf.CfnDataCellsFilter.ColumnWildcardProperty(
            excluded_column_names=["ssn", "dob"],
        ),
    )

    # F) Gen-3 principal permissions.
    #    CfnPrincipalPermissions is the single, current way to grant.
    #    (i) Tag-expression grant — the scales-to-1000-tables pattern.
    finance_analyst_role_arn = (
        f"arn:aws:iam::{Stack.of(self).account}:role/FinanceAnalystRole"
    )
    lf.CfnPrincipalPermissions(
        self, "GrantFinanceAnalyst",
        principal=lf.CfnPrincipalPermissions.DataLakePrincipalProperty(
            data_lake_principal_identifier=finance_analyst_role_arn,
        ),
        resource=lf.CfnPrincipalPermissions.ResourceProperty(
            lf_tag_policy=lf.CfnPrincipalPermissions.LFTagPolicyResourceProperty(
                catalog_id=Stack.of(self).account,
                resource_type="TABLE",
                expression=[
                    lf.CfnPrincipalPermissions.LFTagProperty(
                        tag_key="domain", tag_values=["finance"],
                    ),
                ],
            ),
        ),
        permissions=["SELECT", "DESCRIBE"],
        permissions_with_grant_option=[],
    )

    # (ii) Named-resource grant — on the specific DataCellsFilter.
    lf.CfnPrincipalPermissions(
        self, "GrantFinanceAnalystEmeaFilter",
        principal=lf.CfnPrincipalPermissions.DataLakePrincipalProperty(
            data_lake_principal_identifier=finance_analyst_role_arn,
        ),
        resource=lf.CfnPrincipalPermissions.ResourceProperty(
            data_cells_filter=lf.CfnPrincipalPermissions.DataCellsFilterResourceProperty(
                catalog_id=Stack.of(self).account,
                database_name=self.glue_db_name,
                table_name="fact_revenue",
                name="finance_analyst_emea",
            ),
        ),
        permissions=["SELECT"],
        permissions_with_grant_option=[],
    )

    # (iii) Grant the data-engineer role ALL on LF-Tag environment=dev.
    data_engineer_role_arn = (
        f"arn:aws:iam::{Stack.of(self).account}:role/DataEngineerRole"
    )
    lf.CfnPrincipalPermissions(
        self, "GrantDataEngineerDevAll",
        principal=lf.CfnPrincipalPermissions.DataLakePrincipalProperty(
            data_lake_principal_identifier=data_engineer_role_arn,
        ),
        resource=lf.CfnPrincipalPermissions.ResourceProperty(
            lf_tag_policy=lf.CfnPrincipalPermissions.LFTagPolicyResourceProperty(
                catalog_id=Stack.of(self).account,
                resource_type="DATABASE",
                expression=[
                    lf.CfnPrincipalPermissions.LFTagProperty(
                        tag_key="environment", tag_values=["dev"],
                    ),
                ],
            ),
        ),
        permissions=["ALL"],
        permissions_with_grant_option=[],
    )

    # G) Outputs — the tag keys + filter ARNs are the cross-stack contract
    #    if governance ever splits out.
    CfnOutput(self, "TagDomainKey",      value="domain")
    CfnOutput(self, "TagSensitivityKey", value="sensitivity")
    CfnOutput(self, "FilterEmeaArn",     value=self.filter_finance_emea.ref)
```

### 3.3 Cross-account share — producer side

```python
def _share_fact_revenue_to_consumer_account(self, consumer_account_id: str) -> None:
    """Add a Gen-3 grant to a cross-account principal. LF emits a RAM share;
    the consumer account must accept it before queries work."""
    lf.CfnPrincipalPermissions(
        self, "CrossAcctGrantFactRevenue",
        principal=lf.CfnPrincipalPermissions.DataLakePrincipalProperty(
            # Cross-account principals are the ACCOUNT ID (root), not a role ARN.
            # The consumer account then sub-grants to its own roles.
            data_lake_principal_identifier=consumer_account_id,
        ),
        resource=lf.CfnPrincipalPermissions.ResourceProperty(
            table=lf.CfnPrincipalPermissions.TableResourceProperty(
                catalog_id=Stack.of(self).account,
                database_name=self.glue_db_name,
                name="fact_revenue",
            ),
        ),
        permissions=["SELECT", "DESCRIBE"],
        # grant_option=["SELECT"] would let the consumer account sub-grant;
        # omit for tight control.
        permissions_with_grant_option=[],
    )
```

### 3.4 Consumer role — the IAM side of LF

Lake Formation grants **data** access; the consumer role still needs IAM permissions to **invoke** the query engine. Both are required.

```python
def _build_finance_analyst_role(self) -> iam.Role:
    role = iam.Role(
        self, "FinanceAnalystRole",
        role_name="FinanceAnalystRole",           # must match the LF grant ARN
        assumed_by=iam.AccountRootPrincipal(),    # or SSO, or a federated IdP
    )
    # IAM side — engine invocation. LF side — data reads.
    role.add_to_policy(iam.PolicyStatement(
        actions=[
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:StopQueryExecution",
        ],
        resources=[
            f"arn:aws:athena:{Stack.of(self).region}:"
            f"{Stack.of(self).account}:workgroup/lakehouse-analyst",
        ],
    ))
    role.add_to_policy(iam.PolicyStatement(
        actions=[
            "glue:GetDatabase", "glue:GetDatabases",
            "glue:GetTable",    "glue:GetTables",
            "glue:GetPartitions",
            "lakeformation:GetDataAccess",            # this is the LF handshake
        ],
        resources=["*"],
    ))
    # Athena results bucket + KMS (if encrypted) — role-side grants.
    role.add_to_policy(iam.PolicyStatement(
        actions=["s3:GetBucketLocation", "s3:GetObject", "s3:PutObject",
                 "s3:ListBucket"],
        resources=[
            self.athena_results_bucket.bucket_arn,
            f"{self.athena_results_bucket.bucket_arn}/*",
        ],
    ))
    return role
```

### 3.5 Monolith gotchas

1. **The CI deploy role MUST be an LF admin.** Every `CfnPrincipalPermissions`, `CfnTag`, `CfnResource` is authorized against the caller's LF admin status. If you forget, the first post-`CfnDataLakeSettings` deploy fails on the *next* resource with "Principal is not a data lake administrator" — and if that was the first deploy, the stack is stuck in `ROLLBACK_FAILED` because the rollback also runs as the same non-admin principal. Recovery: console-add the deploy role as admin, then `cdk deploy` again.
2. **`CreateDatabaseDefaultPermissions=[]` blocks IAM-only fallback.** After this setting, any role that previously queried via IAM-only now gets zero results. You MUST create explicit `CfnPrincipalPermissions` for every role that needs access. Before enabling, inventory existing IAM users of the lake.
3. **LF-Tag updates are not Gen-3-aware retroactively.** Adding `legal` to the `domain` tag propagates instantly. But removing a tag value (or deleting a tag) fails if any `CfnPrincipalPermissions` still references that value — CFN gives a cryptic dependency error. Delete the grants first, then the tag.
4. **Column-level tags propagate through DDL.** Tagging `dim_customer.ssn` with `sensitivity=pii` survives table ALTERs; but if the column is dropped and re-added with the same name, the tag must be re-applied. Add a post-schema-change job that re-tags via `glue:UpdateTable` + `lakeformation:AddLFTagsToResource`.
5. **DataCellsFilters are Athena-/Spark-/Redshift-aware but NOT Glue-ETL-aware.** A Glue ETL job reading the table with the LF-assumed role IGNORES the filter (row filter applies at query engines only). If Glue ETL must be filtered, read via Spark SQL + `USING catalog lakeformation`, not via the DataFrame reader against the S3 path.
6. **`mutation_type="REPLACE"` wipes out-of-band admin additions.** Console-added admins (common for break-glass) get removed on the next deploy. Either lock `mutation_type="APPEND"` (but then admin removal becomes hard) or add all break-glass admins to CDK.
7. **Cross-account `data_lake_principal_identifier` is the ACCOUNT ID, not a role ARN.** Sub-delegation is the consumer account's responsibility.
8. **Gen-3 does not coexist cleanly with Gen-2.** If any `CfnPermissions` exists in the same account for the same resource, the union is applied but audit logs misattribute — you cannot tell which generation granted a specific permission. Migrate all Gen-2 to Gen-3 at once; do not run both.

---

## 4. Micro-Stack Variant

**Use when:** data mesh / multi-domain lake / enterprise-grade separation where `GovernanceStack` is a shared horizontal managed by a platform team while `TableBucketStack` / `LakehouseStack` are per-domain vertical stacks.

### 4.1 The 5 non-negotiables

1. **`Path(__file__)` anchoring** — N/A here (no `PythonFunction` in GovernanceStack unless you also bundle a custom-resource for DataCellsFilter creation; if you do, anchor its entry).
2. **Identity-side grants only for IAM side, RESOURCE-side for LF side.** The LF grant IS the resource policy. IAM side (engine invocation) is identity-side on consumer roles.
3. **`CfnRule` cross-stack EventBridge** — applies if you wire LF audit events to alerts (e.g. "principal X was granted ALL on database Y"). The `events.CfnRule` lives in GovernanceStack; the target Lambda in `ObservabilityStack` is referenced by ARN via SSM.
4. **Same-stack bucket + OAC** — does not apply. LF does not serve over CloudFront.
5. **KMS ARNs as strings** — if the Glue catalog or table bucket encrypts catalog metadata with a CMK, the CMK ARN is SSM-published by the owner stack. GovernanceStack reads it via `value_for_string_parameter`, grants the LF service-linked role `kms:Decrypt` on the string ARN.

### 4.2 GovernanceStack — the single source of truth

```python
# stacks/governance_stack.py
from typing import List
from aws_cdk import (
    CfnOutput, Stack,
    aws_lakeformation as lf,
    aws_ssm as ssm,
)
from constructs import Construct


class GovernanceStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *,
        stage: str,
        admin_role_arns: List[str],          # CI deploy + platform-owner
        **kw,
    ) -> None:
        super().__init__(scope, construct_id, **kw)
        self.stage = stage

        # A) Resolve cross-stack contract — storage ARNs arrive via SSM.
        #    These are tokens; use directly in `resource_arn=` and ARN f-strings.
        self.table_bucket_arn = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lakehouse/table_bucket_arn"
        )
        self.table_bucket_name = ssm.StringParameter.value_for_string_parameter(
            self, f"/{{project_name}}/{stage}/lakehouse/table_bucket_name"
        )

        # B) Data-lake settings (account singleton — owned here).
        lf.CfnDataLakeSettings(
            self, "DataLakeSettings",
            admins=[
                lf.CfnDataLakeSettings.DataLakePrincipalProperty(
                    data_lake_principal_identifier=arn,
                ) for arn in admin_role_arns
            ],
            create_database_default_permissions=[],
            create_table_default_permissions=[],
            mutation_type="REPLACE",
        )

        # C) Register storage locations. S3 Tables bucket — service-linked role.
        lf.CfnResource(
            self, "RegisterTableBucket",
            resource_arn=self.table_bucket_arn,
            use_service_linked_role=True,
        )

        # D) LF-Tags — the taxonomy.
        lf.CfnTag(self, "TagDomain",
                  tag_key="domain",
                  tag_values=["finance", "hr", "legal", "product"])
        lf.CfnTag(self, "TagSensitivity",
                  tag_key="sensitivity",
                  tag_values=["public", "internal", "confidential", "pii"])
        lf.CfnTag(self, "TagEnvironment",
                  tag_key="environment",
                  tag_values=["dev", "stage", "prod"])

        # E) Publish the tag keys + the Governance stack's own outputs via SSM.
        #    Downstream "domain" stacks consume these to know WHAT tag values
        #    are legal (they do not themselves create CfnTag — that would
        #    collide with the Governance creation).
        ssm.StringParameter(
            self, "TagKeysParam",
            parameter_name=f"/{{project_name}}/{stage}/lf/tag_keys",
            string_value="domain,sensitivity,environment",
        )

    def grant_role_on_tag_expression(
        self, logical_id: str, *,
        role_arn: str,
        resource_type: str,                  # "TABLE" | "DATABASE" | "VIEW"
        expression: dict[str, List[str]],    # {"domain": ["finance"], "sensitivity": [...]}
        permissions: List[str],
    ) -> lf.CfnPrincipalPermissions:
        """Thin helper: grant Gen-3 tag-expression permission.
        Keeps all grants in GovernanceStack; consumers call this with their
        role ARN + desired expression."""
        return lf.CfnPrincipalPermissions(
            self, logical_id,
            principal=lf.CfnPrincipalPermissions.DataLakePrincipalProperty(
                data_lake_principal_identifier=role_arn,
            ),
            resource=lf.CfnPrincipalPermissions.ResourceProperty(
                lf_tag_policy=lf.CfnPrincipalPermissions.LFTagPolicyResourceProperty(
                    catalog_id=self.account,
                    resource_type=resource_type,
                    expression=[
                        lf.CfnPrincipalPermissions.LFTagProperty(
                            tag_key=k, tag_values=v,
                        )
                        for k, v in expression.items()
                    ],
                ),
            ),
            permissions=permissions,
            permissions_with_grant_option=[],
        )

    def tag_table(
        self, logical_id: str, *,
        database_name: str, table_name: str,
        tag_key: str, tag_values: List[str],
    ) -> lf.CfnTagAssociation:
        """Apply a tag to a Glue table. Called by domain stacks via cross-stack
        construct reference (they pass database + table name as plain strings
        read from their own SSM contract)."""
        return lf.CfnTagAssociation(
            self, logical_id,
            lf_tags=[lf.CfnTagAssociation.LFTagPairProperty(
                catalog_id=self.account,
                tag_key=tag_key, tag_values=tag_values,
            )],
            resource=lf.CfnTagAssociation.ResourceProperty(
                table=lf.CfnTagAssociation.TableResourceProperty(
                    catalog_id=self.account,
                    database_name=database_name,
                    name=table_name,
                ),
            ),
        )
```

### 4.3 Per-domain stack — pass tables + roles to Governance

```python
# stacks/finance_domain_stack.py
from aws_cdk import Stack, aws_ssm as ssm
from constructs import Construct
from stacks.governance_stack import GovernanceStack


class FinanceDomainStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *,
        stage: str, governance: GovernanceStack, **kw,
    ) -> None:
        super().__init__(scope, construct_id, **kw)

        # A) This stack owns the table bucket + Iceberg tables +
        #    FinanceAnalystRole. Details omitted — see
        #    DATA_ICEBERG_S3_TABLES §4.2.
        # ... (glue_db_name, table_name, finance_analyst_role.role_arn)

        # B) Ask Governance to tag this table + grant our analyst role.
        #    This runs at synth-time — the resulting CfnTagAssociation lives
        #    in GovernanceStack's template, not ours.
        governance.tag_table(
            logical_id=f"Tag{self.stack_name}FactRevenueDomain",
            database_name=self.glue_db_name,
            table_name="fact_revenue",
            tag_key="domain", tag_values=["finance"],
        )
        governance.tag_table(
            logical_id=f"Tag{self.stack_name}FactRevenueSensitivity",
            database_name=self.glue_db_name,
            table_name="fact_revenue",
            tag_key="sensitivity", tag_values=["internal"],
        )
        governance.grant_role_on_tag_expression(
            logical_id=f"Grant{self.stack_name}AnalystFinance",
            role_arn=self.finance_analyst_role.role_arn,
            resource_type="TABLE",
            expression={"domain": ["finance"]},
            permissions=["SELECT", "DESCRIBE"],
        )
```

### 4.4 Micro-stack gotchas

- **Cross-stack construct calls (`governance.tag_table(...)`) create resources in GOVERNANCE's template.** This is what we want (single source of truth for all grants). But: if FinanceDomainStack is deleted, the tag associations in Governance are orphaned — there is no automatic cleanup. Add a `cdk destroy --app ... FinanceDomainStack GovernanceStack` order or an out-of-band cleanup lambda.
- **Admin-role-arn circular dependency.** GovernanceStack needs admin role ARNs to set `CfnDataLakeSettings`. If the admin role itself is defined in another stack (e.g. a `SecurityStack` CI deploy role), the other stack must deploy first. Pass the ARN as a plain string (from config) to avoid a CDK construct reference cycle.
- **Tag-expression grants resolve at query time**, meaning a new table tagged `domain=finance` becomes visible to `FinanceAnalystRole` WITHOUT a Governance redeploy. This is the scale-out pattern. The downside: you cannot easily audit "who can see this table?" without replaying the LF-Tag-expression engine — LF does not expose a reverse index. Build an inventory Lambda (see §6) as a standing utility.
- **DataCellsFilters are scoped to one table.** Cross-table row filters require views in Redshift or Athena CTE patterns; LF's built-in filter is per-table only.
- **RAM share invitations expire after 7 days.** If the consumer account does not accept within 7 days, the invitation is deleted and the share is gone — you must re-emit. Build a CloudWatch alarm on the RAM "InvitationPending > 5 days" metric.

---

## 5. Swap matrix — when to replace or supplement

| Concern | Default | Swap with | Why |
|---|---|---|---|
| Permission model | Gen-3 `CfnPrincipalPermissions` (this partial) | Gen-2 `CfnPermissions` | Legacy account that still uses it. Migration plan: freeze new Gen-2 grants, add Gen-3 equivalents, delete Gen-2 in a second pass. Never long-term; audit fidelity suffers. |
| Access control style | LF-TBAC (tag expressions) | LF named-resource grants | Under 50 tables and static set — named is simpler. Over 50 or if tables churn — TBAC is the only scalable option. |
| Row filtering | `CfnDataCellsFilter` row_filter | Athena VIEW with `WHERE region='EMEA'` | View is simpler and works without LF; but loses cross-engine consistency (Spark / Redshift bypass). Prefer LF filter when multiple engines read the same table. |
| Column masking | `excluded_column_names` on filter | Athena VIEW that SELECTs subset | View requires the engine to see all columns and manually drop — read authorization does not protect at storage layer. LF filter blocks column metadata at planner. |
| Cross-account | `CfnPrincipalPermissions` with account-ID principal + RAM auto-accept | Resource-link + IAM-only | Resource-link is a thin pointer in consumer account; IAM on producer still controls access. Simpler for 1-2 consumer accounts; unmanageable beyond. |
| Cross-region | One LF per region + replicate via Glue Catalog cross-region replication | Single-region LF + cross-region Athena federated query | Replication is complex; federated cross-region is slow. Single-region LF + regional replicas with AWS Glue Catalog Replication (preview) for DR only. |
| Governed locations | `CfnResource(use_service_linked_role=True)` | `role_arn=custom_lf_role_arn` | Custom role only if you need cross-region replication / KMS key-policy additions that the SLR cannot make. Rarely needed. |
| External federation | S3 Tables auto-federated + LF | Iceberg REST catalog (Polaris) + custom governance | Multi-cloud / Databricks portability. LF is AWS-only; if that is a constraint, you need a different governance plane. |

---

## 6. Worked example — offline synth + inventory lambda

```python
# tests/test_governance_synth.py
"""Offline: the Governance stack emits LF admin settings, tags, one
registered location, and one tag-expression principal permission."""
import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from stacks.governance_stack import GovernanceStack


def test_synth_governance_stack():
    app = cdk.App()
    gov = GovernanceStack(
        app, "Gov-dev",
        stage="dev",
        admin_role_arns=[
            "arn:aws:iam::111122223333:role/CiDeployRole",
            "arn:aws:iam::111122223333:role/PlatformOwner",
        ],
    )
    # Simulate a domain-stack call: governance.grant_role_on_tag_expression(...)
    gov.grant_role_on_tag_expression(
        logical_id="TestGrant",
        role_arn="arn:aws:iam::111122223333:role/FinanceAnalystRole",
        resource_type="TABLE",
        expression={"domain": ["finance"]},
        permissions=["SELECT"],
    )
    tpl = Template.from_stack(gov)

    # Admin list = both provided ARNs; default perms = [].
    tpl.has_resource_properties("AWS::LakeFormation::DataLakeSettings", {
        "Admins": Match.array_with([
            Match.object_like({
                "DataLakePrincipalIdentifier": "arn:aws:iam::111122223333:role/CiDeployRole",
            }),
        ]),
        "CreateDatabaseDefaultPermissions": [],
        "CreateTableDefaultPermissions":    [],
        "MutationType": "REPLACE",
    })

    # Three tags created.
    tpl.resource_count_is("AWS::LakeFormation::Tag", 3)
    tpl.has_resource_properties("AWS::LakeFormation::Tag", {
        "TagKey": "sensitivity",
        "TagValues": Match.array_with(["pii"]),
    })

    # One registered S3 Tables location.
    tpl.has_resource_properties("AWS::LakeFormation::Resource", {
        "UseServiceLinkedRole": True,
    })

    # Tag-expression Gen-3 grant present.
    tpl.has_resource_properties("AWS::LakeFormation::PrincipalPermissions", {
        "Permissions": ["SELECT"],
        "Resource": Match.object_like({
            "LFTagPolicy": Match.object_like({
                "ResourceType": "TABLE",
                "Expression":   Match.array_with([
                    Match.object_like({"TagKey": "domain", "TagValues": ["finance"]}),
                ]),
            }),
        }),
    })


# lambda/lf_inventory/handler.py
"""Standing utility: dump "who can read table X via LF?" — a CSV report
read by the platform team. Run weekly.

Input : {"database": "lakehouse", "table": "fact_revenue"}
Output: S3 CSV with columns: principal_arn, permissions, granted_via
"""
import os, csv, io, boto3

lf = boto3.client("lakeformation")
s3 = boto3.client("s3")

REPORT_BUCKET = os.environ["REPORT_BUCKET"]

def lambda_handler(event, _ctx):
    db = event["database"]
    tbl = event["table"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["principal_arn", "permissions", "granted_via"])

    # Named-resource grants on the table.
    paginator = lf.get_paginator("list_permissions")
    for page in paginator.paginate(
        Resource={"Table": {"DatabaseName": db, "Name": tbl}},
    ):
        for p in page.get("PrincipalResourcePermissions", []):
            w.writerow([
                p["Principal"]["DataLakePrincipalIdentifier"],
                "|".join(p["Permissions"]),
                "named",
            ])

    # Tag-expression grants: walk every tag expression that MATCHES this
    # table's current tag set.
    tags = lf.get_resource_lf_tags(
        Resource={"Table": {"DatabaseName": db, "Name": tbl}},
    ).get("LFTagOnDatabase", []) + []   # shortened: also gather table + column tags
    # For each applicable tag-expression grant, see if this table matches.
    for page in paginator.paginate(
        Resource={
            "LFTagPolicy": {
                "ResourceType": "TABLE",
                "Expression":   [],          # empty = list all — paginate + filter
            },
        },
    ):
        for p in page.get("PrincipalResourcePermissions", []):
            # (Matching logic against `tags` omitted for brevity — in
            #  practice, AND all tag_key/tag_values in the expression
            #  against the table's current tag set.)
            w.writerow([
                p["Principal"]["DataLakePrincipalIdentifier"],
                "|".join(p["Permissions"]),
                "tag-expression",
            ])

    key = f"lf-inventory/{db}/{tbl}/{event.get('ts', 'latest')}.csv"
    s3.put_object(Bucket=REPORT_BUCKET, Key=key, Body=out.getvalue().encode())
    return {"report_key": key}
```

Run `pytest tests/test_governance_synth.py -v` offline; deploy + invoke the `lf_inventory` Lambda for a point-in-time "who can read X?" audit.

---

## 7. References

- AWS docs — *Lake Formation developer guide* (LF-Tags, CfnPrincipalPermissions, DataCellsFilter).
- AWS docs — *Cross-account access via Lake Formation + RAM* (producer / consumer roles).
- AWS docs — *Lake Formation DataLakeSettings* (admin list, default permissions).
- `DATA_ICEBERG_S3_TABLES.md` — the S3 Tables partial this governance overlay secures.
- `DATA_LAKEHOUSE_ICEBERG.md` — self-managed Iceberg, also governable via LF.
- `DATA_GLUE_CATALOG.md` — the catalog LF operates against; LF-Tags live on Glue resources.
- `DATA_ATHENA.md` — the query engine that enforces LF row/column filters.
- `LAYER_BACKEND_LAMBDA.md` §4.1 — 5 non-negotiables (IAM/KMS string ARNs echoed here).

---

## 8. Changelog

- **v2.0 — 2026-04-22 — Initial.** Gen-3 `CfnPrincipalPermissions` as the only grant generation; LF-TBAC tag-expression primary; data-cells filter secondary; cross-account via RAM; inventory-lambda worked example. 8 monolith gotchas, 5 micro-stack gotchas, 8-row swap matrix.
