# PATTERN_CUSTOMER_MAINTAINED_DIM_DDB

**Status:** Authored 2026-05-19. Surfaced by the NorthBay Tamimi DLH engagement — dim tables like `dim_site`, `dim_dept`, `dim_area_mgr`, `dim_site_status`, `dim_flyer` are edited by the customer manually today (in Excel). Migrating that ownership to DDB with an operator UI is cleaner than keeping CSVs in S3.

## 1. Purpose

A pattern for **customer-maintained dimension tables** (master data the customer edits rather than receiving from a source system): DDB-backed, edited via the operator UI, synced to `silver.dim_*` on a schedule.

**Use this when:**
- Dim has < 10K rows (DDB sweet spot)
- Customer edits it more than monthly (an Excel-based workflow is friction)
- Mapping changes need an audit trail
- The dim drives downstream business rules (Include flag, Clubbing Dept mapping, Vertical, etc.)

**Don't use this when:**
- Dim comes from a source system (SAP master tables, IAM users) — use the regular Bronze→Silver pipeline
- Dim is generated (e.g. `dim_date`) — use a dbt seed
- Dim is > 100K rows — costs and write throughput become a discussion

## 2. The DDB schema (one table per dim, NOT polymorphic)

Each customer-maintained dim gets its own DDB table. **Don't** stuff multiple dims into one table — different access patterns, different schemas, different IAM scopes.

```
<slug>-dlh-dim-<name>-<env>
   PK: dim_id (str)
   Attributes match the Silver dim's column set + audit fields
```

Example for `dim_site` (Tamimi):

```python
# src/control_plane/models/dim_site.py
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal

class DimSiteItem(BaseModel):
    dim_id: str                                                 # PK — same as Site code, e.g. "S116"
    site: str                                                   # business key (= dim_id for sites)
    company_code: str
    description: str
    region: Literal["Central", "Eastern", "Western"]
    format: Literal["Super Market", "Express", "E Com", "Bulk", "Tabak Bakery", "Warehouse"]
    vertical: Literal["Retail", "Wholesale", "B2B"]
    opening_date: datetime
    status: Literal["Active", "Closed", "Refurb"]

    # Audit fields — NEVER edited by customer; written by API
    created_at: datetime
    created_by: str                                             # Azure AD principal
    last_modified_at: datetime
    last_modified_by: str
    version: int = 1                                            # optimistic concurrency
```

For dims with controlled vocabularies (`Region`, `Format`, `Vertical`, `Status`), use `Literal[...]` so the API rejects bad inputs at the boundary.

## 3. Operator UI — CRUD against an API Gateway + Lambda

| Endpoint | Method | What |
|---|---|---|
| `GET /dims/site` | List all sites with filters (region, format, status) |
| `GET /dims/site/{dim_id}` | One row |
| `POST /dims/site` | Create new (e.g. new store opens) |
| `PUT /dims/site/{dim_id}` | Update (with `If-Match` version header — optimistic locking) |
| `DELETE /dims/site/{dim_id}` | Soft-delete (set `status="Closed"`, NEVER hard-delete) |
| `GET /dims/site/{dim_id}/history` | Versioned change log (see §5) |

**Authorization model:** Lake Formation tag on the row → operator's Azure AD group → allowed actions. Tag values: `dim:site`, `dim:dept`, `dim:flyer`. Group "Dim Editors — Sites" gets R/W on `dim:site` only.

```python
# src/lambdas/dim_api/handler.py
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.utilities.parser import event_parser

app = APIGatewayRestResolver()

@app.put("/dims/site/<dim_id>")
def update_site(dim_id: str):
    # Pydantic validates the body matches DimSiteItem
    body = DimSiteItem.model_validate_json(app.current_event.body)

    # Optimistic concurrency
    current = ddb_get("dim-site", {"dim_id": dim_id})
    if_match = int(app.current_event.headers["If-Match"])
    if current["version"] != if_match:
        raise ConflictError("Row was modified by another editor")

    # Stamp audit fields
    body.last_modified_at = datetime.utcnow()
    body.last_modified_by = app.current_event.request_context.authorizer["principalId"]
    body.version = current["version"] + 1

    # Write the new version
    ddb_put("dim-site", body.model_dump())

    # Append history record (separate table or DDB Streams → S3)
    write_history_event("dim_site_updated", dim_id, body.model_dump(), if_match)

    return body.model_dump(), 200
```

## 4. Sync mechanism — DDB → `silver.dim_*` on a schedule

Two viable approaches; pick based on freshness requirements:

### 4.1 Approach A: scheduled Glue (recommended for daily-or-slower)

```python
# src/glue/jobs/dim_sync.py — driven by spec
def sync_dim(spec):
    """Read entire DDB dim table, write as Silver Iceberg table."""
    df = spark.read.format("dynamodb") \
        .option("tableName", f"{cfg.slug}-dlh-dim-{spec['dim_name']}-{cfg.env}") \
        .load()

    # Drop audit fields, project to the Silver schema
    silver_df = df.drop("created_at", "created_by", "last_modified_at",
                        "last_modified_by", "version")

    # Apply any final canonicalisation (lowercase, trim, etc.)
    silver_df = apply_transforms(silver_df, spec["transforms"])

    # OVERWRITE — dim is full snapshot, not incremental
    silver_df.writeTo(f"silver.dim_{spec['dim_name']}").overwritePartitions()
```

Run nightly (or on operator-UI "Sync now" button). Cheap: dim tables are tiny.

### 4.2 Approach B: DDB Streams → Lambda → Iceberg MERGE (for near-real-time)

If freshness < 1 hour matters (e.g. customer wants Power BI to reflect a new store within minutes of opening):

```python
# src/lambdas/dim_stream_to_silver/handler.py
@app.on_dynamodb_stream("dim-site")
def handle_change(event):
    for record in event["Records"]:
        new = record["dynamodb"].get("NewImage", {})
        old = record["dynamodb"].get("OldImage", {})

        match record["eventName"]:
            case "INSERT" | "MODIFY":
                iceberg_merge("silver.dim_site", new, key="site")
            case "REMOVE":
                # Soft-delete only — set status=Closed, don't physically delete from Silver
                pass
```

Costs more (Lambda invocations + Iceberg merge overhead). Worth it only if the customer explicitly asks.

**Default = Approach A** for cost and simplicity.

## 5. Audit trail — versioned history

DDB Streams on each dim table → Lambda → S3 (Parquet) → Athena queryable.

```
s3://<slug>-dlh-audit-<env>/dim_history/<dim_name>/year=YYYY/month=MM/day=DD/<run_id>.parquet
```

Athena query example:

```sql
SELECT dim_id, change_type, modified_by, old_value, new_value, modified_at
FROM audit.dim_history
WHERE dim_name = 'site' AND dim_id = 'S116'
ORDER BY modified_at DESC;
```

This is critical for the customer's compliance team — "who renamed Store 116 last quarter?" must have an answer.

## 6. CDKTF construct shape

```python
# infra/constructs/customer_dim.py
class CustomerMaintainedDim(Construct):
    """One dim table + API Gateway routes + history stream + sync Glue job."""

    def __init__(self, scope, id, *, cfg, kms_key, dim_name, schema_model):
        super().__init__(scope, id)

        self.table = DynamodbTable(
            self, f"dim-{dim_name}",
            name=f"{cfg.slug}-dlh-dim-{dim_name}-{cfg.env}",
            hash_key="dim_id",
            attribute=[{"name": "dim_id", "type": "S"}],
            billing_mode="PAY_PER_REQUEST",
            server_side_encryption={"enabled": True, "kms_key_arn": kms_key.arn},
            point_in_time_recovery={"enabled": cfg.env == "prod"},
            deletion_protection_enabled=cfg.env == "prod",
            stream_enabled=True,
            stream_view_type="NEW_AND_OLD_IMAGES",
            tags=cfg.tags | {"dim_name": dim_name, "lf_tag:dim": dim_name},
        )

        # API GW routes mounted under /dims/<dim_name>
        # IAM scoped to lf_tag:dim=<dim_name>
        # Glue sync job spec at specs/silver/dim_<name>.yaml
        # History stream Lambda
        ...
```

## 7. Operator UI integration

Each dim becomes a route in the operator dashboard:

```
/dims
  ├── /sites          (table view, filters, "Add Store" button)
  ├── /departments    (same)
  ├── /area-managers  (same)
  ├── /flyers         (specialised — has "Promote to current" action)
  └── /site-status    (specialised — drives like-for-like classification)
```

Each route is a CRUD table generated from the Pydantic model + a route-specific Action button for any non-CRUD operations (Promote flyer, Bulk-import sites from CSV).

## 8. Bulk import for migration day

The customer has a 188-row `Sites database` in Excel today. One-time import:

```python
# src/scripts/import_dim_from_excel.py
import openpyxl
from src.control_plane.models.dim_site import DimSiteItem

wb = openpyxl.load_workbook("Sites database.xlsx", data_only=True)
ws = wb["Sites database"]

with ddb_batch_writer("dim-site") as batch:
    for row in ws.iter_rows(min_row=2, values_only=True):
        item = DimSiteItem(
            dim_id=row[0],
            site=row[0],
            company_code=row[1],
            description=row[2],
            region=row[3],
            format=row[4],
            vertical=row[5],
            opening_date=row[6],
            status=row[7] or "Active",
            created_at=datetime.utcnow(),
            created_by="migration:2026-05",
            last_modified_at=datetime.utcnow(),
            last_modified_by="migration:2026-05",
        )
        batch.put_item(item.model_dump())
```

Run once at cutover. Validates against Pydantic schema; rejects bad rows.

## 9. Patterns

- **One table per dim, not polymorphic.** Different IAM scopes + different schemas.
- **Soft-delete only.** Set `status="Closed"`; never `DeleteItem`. Historical fact-table rows still need to join to the dim.
- **Audit fields stamped by the API**, never by the user. Pydantic doesn't expose them in input models.
- **Optimistic concurrency** via `If-Match` header on `PUT` — `version` field.
- **DDB Streams ALWAYS on** — even if you start with Approach A sync, you'll want the audit log.
- **Lake Formation tag per dim** → Azure AD group → fine-grained editing rights.
- **Bulk import is a one-time script**, not part of the API. Don't enable bulk POST.

## 10. Anti-patterns

- ❌ **Don't put facts in this table.** Customer-maintained tables are dimensions only. Facts come from sources.
- ❌ **Don't allow customer to edit `dim_date`.** Calendar is generated.
- ❌ **Don't sync DDB → Silver more often than once an hour** unless you've measured a real freshness need.
- ❌ **Don't let the operator UI write directly to Silver.** Always: UI → DDB → sync job → Silver. Otherwise rollback / audit / history break.
- ❌ **Don't store PII here** unless the dim is genuinely PII (e.g. employee table). Customer-maintained != PII-free — apply usual PII discipline.
- ❌ **Don't share KMS keys with the fact tables.** Dim has different access scope.

## 11. Composes with

- **`PATTERN_DDB_CONTROL_PLANE`** — shares the same DDB infrastructure pattern but separate tables.
- **`LAYER_API`** + **`LAYER_FRONTEND`** — the operator UI.
- **`SERVERLESS_LAMBDA_POWERTOOLS`** — the CRUD Lambda + history stream Lambda.
- **`data/14_dynamic_glue_pyspark_medallion.md`** — the sync Glue job follows the same spec engine pattern (one YAML per dim).
- **`DATA_LAKE_FORMATION`** — LF tag per dim for fine-grained editor RBAC.

## 12. Pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| Customer edits dim mid-cycle, breaks historical comparison | Power BI numbers change retroactively | Implement SCD2 in Silver — keep `valid_from` / `valid_to`; sync job UPSERTs vs OVERWRITES |
| Operator deletes a row referenced by historical facts | Power BI shows NULL site name on old transactions | Soft-delete only; sync preserves historical rows with `status="Closed"` |
| Sync job races with mid-flight Bronze → Silver run | Silver fact join to dim returns stale rows | Run sync job FIRST in the daily DAG (before fact Silver jobs) |
| DDB row version drifts from Silver row | Manual DDB edit bypasses API | API is the only writer; revoke DDB write IAM from human IAM principals — only the Lambda role |
| New dim attribute added → sync job ignores it | Operator sees the new column in UI but Power BI doesn't | Spec-driven sync — adding to schema YAML auto-includes in sync |

## 13. Acceptance criteria

- [ ] One CDKTF construct per customer-maintained dim — instantiated once per dim
- [ ] Pydantic model defines the schema + uses `Literal` for controlled vocabularies
- [ ] CRUD API Lambda with PUT optimistic-locking via `If-Match`
- [ ] Sync Glue job overwrites `silver.dim_<name>` on schedule (default: nightly)
- [ ] DDB Streams enabled; history Lambda writes Parquet to S3 audit prefix
- [ ] Soft-delete only — `status` field enforced; no `DeleteItem` permission on the Lambda role
- [ ] LF tag (`lf_tag:dim=<name>`) on each row → fine-grained Azure AD group RBAC
- [ ] Bulk-import script tested with the migration-day Excel file (e.g. 188 sites for Tamimi)
- [ ] Operator UI shows version, last-modified-by, history link per row
- [ ] Athena query against `audit.dim_history` returns the full version chain for any row
