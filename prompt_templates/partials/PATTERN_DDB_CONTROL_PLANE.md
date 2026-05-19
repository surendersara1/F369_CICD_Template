# PATTERN_DDB_CONTROL_PLANE

**Status:** Authored 2026-05-19. Surfaced by the NorthBay Tamimi DLH engagement — the customer wants source-to-Bronze mapping, operational runtime state, and frontend dashboard data all in DynamoDB. Promoted to the library per CTO direction (fix the library, not the project).

## 1. Purpose

A **DynamoDB-backed operational control plane** for data-platform engagements: hot-reconfigurable source endpoints, watermarks, run history, lineage, and operator-dashboard state. Sits in front of the spec-driven Glue engine and behind the operator UI.

**Why DDB and not RDS / Aurora:**
- Single-digit-ms reads from operator UI + Glue jobs
- Auto-scaling, no idle cost
- No connection-pool plumbing from Glue
- TTL handles run-history retention without cron

**What this is NOT:**
- Not a replacement for the YAML in `specs/` (those stay in Git for code review and rollback — see PATTERN_SOURCE_AGNOSTIC_INGESTION §7 for the hybrid split)
- Not a transactional store (we use single-table DDB, no cross-table txns)
- Not a metrics store (CloudWatch + Athena handle that)

## 2. The 6 tables (canonical set)

Naming: `<slug>-dlh-<table>-<env>` (e.g. `tamimi-dlh-source-catalog-prod`).

| Table | PK | SK | Purpose | Read pattern |
|---|---|---|---|---|
| **`source_catalog`** | `source_id` (str) | — | Registry of every source system (SAP OData, NCR RDS, Redshift) | Glue jobs resolve `source_id` → endpoint at run start |
| **`bronze_mapping`** | `bronze_table` (str, e.g. `sap.zsdcc`) | `source_id` (str) | Maps each Bronze table to its source + spec path in Git | Engine driver enumerates ALL rows nightly to know what to run |
| **`watermarks`** | `bronze_table` (str) | `partition_key` (str, e.g. site code or date bucket) | Last-pulled watermark per (table, partition) for incremental loads | Glue job reads at start of run, writes at end |
| **`runs`** | `run_id` (ULID) | — | Run history: status, rows, errors, run_at, duration. TTL = 90 days. | Operator UI lists recent; alarms query failures |
| **`lineage_edges`** | `downstream` (str) | `upstream` (str) | DAG edges (Bronze → Silver → Gold model) | Lineage UI walks the graph; reconciliation maps source row → Gold cell |
| **`pipeline_state`** | `entity` (str, polymorphic — `pipeline:<id>`, `alarm:<id>`, `freshness:<table>`) | — | Current operational state: which jobs are running, open alarms, last-success-at per table | Operator dashboard refreshes every 10s |

**Cardinality estimates** (Tamimi point):
- `source_catalog`: 3–6 rows
- `bronze_mapping`: 4–20 rows (one per Bronze table)
- `watermarks`: 50–500 rows (per-table-per-partition)
- `runs`: ~50 / day × 90-day TTL = ~4500 rows steady-state
- `lineage_edges`: ~50–200 edges
- `pipeline_state`: 30–100 active records

DDB on-demand pricing is **trivial** at this scale (< $5/month per env).

## 3. Schemas — Pydantic models that mirror DDB items

Single source of truth for both Glue job code and operator API:

```python
# src/control_plane/models.py
from typing import Literal
from pydantic import BaseModel, Field
from datetime import datetime

class SourceCatalogItem(BaseModel):
    source_id: str                                      # e.g. "sap-prod", "ncr-rds", "redshift-finance"
    source_type: Literal["sap_odata", "rds_jdbc", "redshift_unload", "s3_landing"]
    endpoint: str                                       # OData URL / RDS host:port / Redshift workgroup ARN / S3 prefix
    secrets_arn: str                                    # always indirection through Secrets Manager
    network_config: dict                                # subnet_id, security_group_id, VPC endpoint hints
    status: Literal["active", "paused", "deprecated"]
    notes: str = ""

class BronzeMappingItem(BaseModel):
    bronze_table: str                                   # e.g. "sap.zsdcc"
    source_id: str                                      # FK into source_catalog
    source_object: str                                  # e.g. SAP CDS name "I_ZSDCC" or RDS schema.table
    spec_path: str                                      # path in Git: "src/glue/specs/bronze/sap_zsdcc.yaml"
    cadence_cron: str                                   # e.g. "0 * * * ?" hourly
    enabled: bool = True
    last_success_at: datetime | None = None

class WatermarkItem(BaseModel):
    bronze_table: str
    partition_key: str                                  # often a site / region / date bucket
    last_watermark_value: str                           # str-encoded; could be ISO timestamp or sequence number
    last_run_id: str | None = None
    updated_at: datetime

class RunItem(BaseModel):
    run_id: str                                         # ULID — sort-friendly + unique
    pipeline_id: str
    bronze_table: str
    status: Literal["started", "running", "succeeded", "failed", "cancelled"]
    started_at: datetime
    ended_at: datetime | None = None
    rows_in: int = 0
    rows_out: int = 0
    error_message: str | None = None
    ttl: int                                            # unix epoch; set to started_at + 90 days

class LineageEdgeItem(BaseModel):
    downstream: str                                     # e.g. "gold.unified_sales"
    upstream: str                                       # e.g. "silver.sap.zsdcc"
    edge_type: Literal["bronze_to_silver", "silver_to_gold", "gold_to_view"]
    transform_id: str                                   # e.g. dbt model name or Glue job arn
    last_observed_at: datetime

class PipelineStateItem(BaseModel):
    entity: str                                         # polymorphic — see PK in §2
    state: dict                                         # arbitrary JSON for the entity type
    updated_at: datetime
```

## 4. Access patterns (who reads, who writes)

| Component | Reads | Writes |
|---|---|---|
| **Glue Bronze job** | `source_catalog` (resolve endpoint), `watermarks` (last pulled), `bronze_mapping` (find own spec) | `runs` (start/end), `watermarks` (advance), `pipeline_state` (`freshness:<table>` → now) |
| **Silver Spark job (data/14 engine)** | `bronze_mapping`, `lineage_edges` (read upstream), `pipeline_state` (block on upstream not-ready) | `runs`, `lineage_edges` (record new edge), `pipeline_state` |
| **dbt Gold runner (Lambda or ECS)** | `lineage_edges` | `runs`, `lineage_edges`, `pipeline_state` |
| **Operator UI (Next.js + API GW)** | All 6 tables (RBAC-scoped) | `source_catalog` (add new source), `bronze_mapping` (enable/disable), `pipeline_state` (acknowledge alarms) |
| **Reconciliation Lambda** | `lineage_edges`, `runs` | `pipeline_state` (`reconciliation:<gold_table>` → pass/fail + drift) |

**IAM principle:** each consumer gets a role with **only the read/write actions it needs**, scoped to specific table ARNs. The operator UI gets the most; Glue jobs get the least.

## 5. CDKTF construct shape

```python
# infra/constructs/control_plane_ddb.py
from cdktf_cdktf_provider_aws.dynamodb_table import DynamodbTable, DynamodbTableAttribute

class ControlPlaneDdb(Construct):
    """The 6-table DynamoDB control plane for the lakehouse."""

    def __init__(self, scope, id, *, cfg, kms_key):
        super().__init__(scope, id)

        self.source_catalog = self._make_table(
            cfg, kms_key, "source-catalog",
            hash_key="source_id", hash_key_type="S",
        )
        self.bronze_mapping = self._make_table(
            cfg, kms_key, "bronze-mapping",
            hash_key="bronze_table", hash_key_type="S",
            range_key="source_id", range_key_type="S",
        )
        self.watermarks = self._make_table(
            cfg, kms_key, "watermarks",
            hash_key="bronze_table", hash_key_type="S",
            range_key="partition_key", range_key_type="S",
        )
        self.runs = self._make_table(
            cfg, kms_key, "runs",
            hash_key="run_id", hash_key_type="S",
            ttl_attribute="ttl",
            gsi=[
                {"name": "by_pipeline", "hash_key": "pipeline_id", "range_key": "started_at"},
                {"name": "by_status", "hash_key": "status", "range_key": "started_at"},
            ],
        )
        self.lineage_edges = self._make_table(
            cfg, kms_key, "lineage-edges",
            hash_key="downstream", hash_key_type="S",
            range_key="upstream", range_key_type="S",
            gsi=[{"name": "by_upstream", "hash_key": "upstream", "range_key": "downstream"}],
        )
        self.pipeline_state = self._make_table(
            cfg, kms_key, "pipeline-state",
            hash_key="entity", hash_key_type="S",
        )

    def _make_table(self, cfg, kms_key, name, **kwargs):
        return DynamodbTable(
            self, name,
            name=f"{cfg.project_slug}-dlh-{name}-{cfg.env}",
            billing_mode="PAY_PER_REQUEST",
            server_side_encryption={"enabled": True, "kms_key_arn": kms_key.arn},
            point_in_time_recovery={"enabled": cfg.env == "prod"},
            deletion_protection_enabled=cfg.env == "prod",
            stream_enabled=name in ("runs", "pipeline_state"),     # streams for change-driven downstreams
            stream_view_type="NEW_AND_OLD_IMAGES" if name in ("runs", "pipeline_state") else None,
            tags=cfg.tags,
            ...
        )
```

## 6. Operational dashboards — pipeline_state is the hot table

The operator UI polls `pipeline_state` every 10s and renders 3 widgets:

1. **Currently running** — entities matching `pipeline:*` with `state.status == "running"` → live count, age, retry button.
2. **Open alarms** — `alarm:*` with `state.acknowledged == false` → list with click-to-ack.
3. **Freshness leaderboard** — `freshness:<table>` rows, sorted by `state.last_success_at` ascending → worst table at top.

Why polled and not push: a 10s poll over 30 rows is ~$0.01/month. WebSocket fanout is harder to debug. Revisit only if customer adds an alarm-fan-out requirement.

## 7. PII consideration

DDB items can contain field-level PII (customer-maintained dim values, source endpoint hints in `notes`). Apply:

- **Server-side encryption with CMK** (always — see §5).
- **No PII in DDB stream payloads** unless the consumer is allowlisted (operator UI is; observability Lambda is NOT — give it filtered events).
- **Audit:** CloudTrail data events on the table → S3 → Athena queries by quarter.

For customer-maintained dims (see `PATTERN_CUSTOMER_MAINTAINED_DIM_DDB`), keep PII OUT of the dim table; use a separate ID-resolution table.

## 8. Patterns

- **One construct, 6 tables, single CMK.** Don't split across stacks; control plane is unitary.
- **ULID for `run_id`.** Sort-friendly + unique + URL-safe. Avoid UUIDs (random — slow secondary indexes).
- **TTL on `runs` only.** Other tables are reference data — no TTL. Hard delete is a deploy decision.
- **GSI sparingly.** Only `runs` and `lineage_edges` justify GSIs at our cardinality. Adding a GSI multiplies write cost.
- **Single-table when feasible.** `pipeline_state` is intentionally polymorphic (entity prefix encodes type) — saves you 3 tables and complex joins.
- **Pydantic at the boundary.** Reading from DDB always goes through `Model.model_validate(item)` — never raw dicts in Glue/Lambda code.

## 9. Anti-patterns

- ❌ **Don't store transform rules in DDB.** Those go in `specs/` YAML (Git, code-reviewed). DDB stores *which* spec to run + *where* to read from, not *how* to transform.
- ❌ **Don't store PII in `notes` fields.** Use Secrets Manager for credentials, separate PII tables (with separate KMS keys) for any customer-identifying data.
- ❌ **Don't write to `runs` from the operator UI.** Run state is owned by the executing job; UI is read-only on this table.
- ❌ **Don't use cross-table transactions** (`TransactWriteItems` across `runs` + `pipeline_state`). DDB transactions are pricey; you don't need them at this scale. Two single-table writes are fine.
- ❌ **Don't add columns ad-hoc.** Pydantic models are the contract — schema change goes through a Git PR.
- ❌ **Don't enable backup-to-S3 for every table.** `point_in_time_recovery=True` is enough for `source_catalog` / `bronze_mapping`; the rest is replayable.

## 10. Composes with

- **`PATTERN_SOURCE_AGNOSTIC_INGESTION`** — the pluggable connector layer that reads from `source_catalog` + `bronze_mapping` and writes to `watermarks` + `runs`.
- **`PATTERN_CUSTOMER_MAINTAINED_DIM_DDB`** — customer-edited dim tables live alongside this control plane.
- **`LAYER_FRONTEND`** — operator UI reads all 6 tables via API Gateway.
- **`LAYER_OBSERVABILITY`** — CloudWatch alarms feed `pipeline_state` (`alarm:*`).
- **`IAC_CDKTF_PYTHON`** — see §5 for the CDKTF construct shape.

## 11. Pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| GSI write throttling at 4000+ items | Random Glue job 400s | Switch to on-demand if not already (PAY_PER_REQUEST) |
| Streams attached to all 6 tables | Lambda invocation cost spikes | Streams only on `runs` and `pipeline_state` — others are reference data |
| `pipeline_state` rows orphaned when a pipeline is deleted | UI shows stale entities | TTL the polymorphic items by setting `state.expires_at` + a sweeper Lambda |
| `last_success_at` stale because failed runs don't update it | Freshness leaderboard wrong | Have failed runs ALSO update `pipeline_state` (`status: "failed"`) — UI distinguishes |
| Operator UI bypasses Pydantic validation on writes | Bad rows poison Glue jobs | API Gateway uses request models; Lambda validates against same Pydantic schema before writing |

## 12. Acceptance criteria

- [ ] 6 CDKTF-managed tables deployed (`<slug>-dlh-{source-catalog,bronze-mapping,watermarks,runs,lineage-edges,pipeline-state}-<env>`)
- [ ] All tables have SSE-KMS with customer-managed CMK
- [ ] Point-in-time recovery enabled on Prod
- [ ] Deletion protection enabled on Prod
- [ ] Streams on `runs` and `pipeline_state` only (cost discipline)
- [ ] Pydantic models in `src/control_plane/models.py` mirror the 6 table schemas exactly
- [ ] IAM roles for: Glue jobs (R/W watermarks + runs, R source_catalog + bronze_mapping), Operator UI (full R/W per role), Reconciliation Lambda (R lineage_edges + runs, W pipeline_state)
- [ ] Operator UI reads `pipeline_state` every 10s and renders the 3 widgets (currently-running, alarms, freshness)
- [ ] `runs.ttl` field set to `started_at + 90 days` on every insert — observed deleting after 90 days in QA
- [ ] No PII in any DDB field — confirmed by scanning each table's attribute names against the PII inventory
