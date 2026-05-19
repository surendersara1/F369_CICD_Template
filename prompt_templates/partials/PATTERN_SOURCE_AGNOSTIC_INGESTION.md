# PATTERN_SOURCE_AGNOSTIC_INGESTION

**Status:** Authored 2026-05-19. Surfaced by the NorthBay Tamimi DLH engagement. Promoted to library per CTO direction.

## 1. Purpose

A **pluggable source-connector architecture** for the spec-driven Glue engine. Adding a new source (SAP OData / RDS JDBC / Redshift UNLOAD / S3 landing / future SaaS) becomes:

1. Write a new connector class implementing one Python protocol
2. Insert one row in DDB `source_catalog`
3. Insert one row in DDB `bronze_mapping` per Bronze table the source feeds
4. Add the spec YAML in `src/glue/specs/bronze/`

**No engine code changes. No CDKTF changes. No deploys to add a mapping.**

This pattern sits on top of `PATTERN_DDB_CONTROL_PLANE` (the DDB tables) and extends the `data/14_dynamic_glue_pyspark_medallion.md` template (the engine).

## 2. The contract every source connector implements

One protocol. Glue jobs depend on the protocol, not concrete implementations.

```python
# src/glue/glue_engine/sources/protocol.py
from typing import Protocol, Iterator, runtime_checkable
from pyspark.sql import SparkSession, DataFrame

@runtime_checkable
class SourceConnector(Protocol):
    """Every source plugin implements this. No exceptions."""

    source_type: str                                # "sap_odata" | "rds_jdbc" | "redshift_unload" | "s3_landing"

    def configure(self, source_row: dict, spec: dict) -> None:
        """Called once per Glue run; passed the source_catalog row + the spec YAML.
        Resolves endpoint, fetches credentials from Secrets Manager, sets up auth.
        """
        ...

    def read_incremental(
        self, spark: SparkSession, watermark: str | None, run_id: str
    ) -> tuple[DataFrame, str]:
        """Read rows since `watermark`. Return (DataFrame, new_watermark_value).
        If watermark is None, treat as full pull.
        Implementation is free to choose: delta token / cursor pagination / last_modified column / etc.
        """
        ...

    def read_full(self, spark: SparkSession, run_id: str) -> DataFrame:
        """Sunday-night full snapshot for drift detection. Same return shape minus watermark."""
        ...

    def emit_metrics(self) -> dict:
        """Optional. Per-connector metrics — rows read, bytes downloaded, retries, throttles."""
        return {}
```

The contract is intentionally small. Watermark semantics, auth, retries, throttle handling are all the connector's job — the engine treats it as a black box that returns a DataFrame.

## 3. Plugin registry — decorator-based, no imports in the engine

```python
# src/glue/glue_engine/sources/__init__.py
from .protocol import SourceConnector

_REGISTRY: dict[str, type[SourceConnector]] = {}

def register(source_type: str):
    """Mark a class as a source connector. Engine looks it up at runtime by `source_type`."""
    def _wrap(cls):
        if source_type in _REGISTRY:
            raise ValueError(f"Source type '{source_type}' already registered by {_REGISTRY[source_type].__name__}")
        _REGISTRY[source_type] = cls
        return cls
    return _wrap

def get_connector(source_type: str) -> type[SourceConnector]:
    if source_type not in _REGISTRY:
        raise KeyError(f"No connector registered for source_type='{source_type}'. Registered: {list(_REGISTRY)}")
    return _REGISTRY[source_type]
```

## 4. Reference connectors (4 starters; add more by writing a new `@register` class)

### 4.1 SAP OData

```python
# src/glue/glue_engine/sources/sap_odata.py
from . import register
from .protocol import SourceConnector

@register("sap_odata")
class SapODataConnector:
    source_type = "sap_odata"

    def configure(self, source_row, spec):
        self.endpoint = source_row["endpoint"]                  # e.g. https://sap.tamimi.com/sap/opu/odata/sap/
        self.cds_view = spec["source_object"]                   # e.g. ZSDCC_CDS
        self.creds = self._load_secrets(source_row["secrets_arn"])

    def read_incremental(self, spark, watermark, run_id):
        params = {"$skiptoken": watermark} if watermark else {}
        df = spark.read \
            .format("aws-glue-sap-odata") \
            .option("endpoint", self.endpoint) \
            .option("entity", self.cds_view) \
            .option("auth_type", "oauth2") \
            .option("client_id", self.creds["client_id"]) \
            .option("client_secret", self.creds["client_secret"]) \
            .option("delta_token", watermark or "") \
            .load()
        # SAP returns next-skiptoken in metadata; pluck it out
        new_watermark = df.first()["@odata.deltaLink"] if df.head() else watermark
        return df, new_watermark

    def read_full(self, spark, run_id):
        return spark.read.format("aws-glue-sap-odata") \
            .option("endpoint", self.endpoint) \
            .option("entity", self.cds_view) \
            .option("auth_type", "oauth2").load()
```

### 4.2 RDS JDBC (NCR POS, SAP SQL Server push target, any RDS)

```python
@register("rds_jdbc")
class RdsJdbcConnector:
    source_type = "rds_jdbc"

    def configure(self, source_row, spec):
        self.host = source_row["endpoint"]                      # e.g. "ncr-sales.cluster-xxx.eu-west-1.rds.amazonaws.com"
        self.table = spec["source_object"]                      # e.g. "dbo.ncr_sales"
        self.watermark_col = spec["watermark_column"]           # e.g. "last_modified"
        self.creds = self._load_secrets(source_row["secrets_arn"])

    def read_incremental(self, spark, watermark, run_id):
        where = f"{self.watermark_col} > '{watermark}'" if watermark else "1=1"
        df = spark.read.format("jdbc") \
            .option("url", f"jdbc:sqlserver://{self.host}:1433") \
            .option("dbtable", f"(SELECT * FROM {self.table} WHERE {where}) src") \
            .option("user", self.creds["username"]) \
            .option("password", self.creds["password"]) \
            .load()
        new_watermark = df.agg({self.watermark_col: "max"}).first()[0]
        return df, str(new_watermark)
```

### 4.3 Redshift UNLOAD (Redshift-as-source)

```python
@register("redshift_unload")
class RedshiftUnloadConnector:
    source_type = "redshift_unload"

    def configure(self, source_row, spec):
        self.workgroup_arn = source_row["endpoint"]
        self.query = spec["source_query"]                       # SELECT statement to UNLOAD
        self.creds = self._load_secrets(source_row["secrets_arn"])
        self.staging_s3 = source_row["network_config"]["unload_staging_s3"]

    def read_incremental(self, spark, watermark, run_id):
        # Trigger UNLOAD; read resulting Parquet from S3
        from .redshift_helpers import run_unload
        s3_prefix = run_unload(
            workgroup=self.workgroup_arn,
            query=self.query,
            watermark=watermark,
            staging=f"{self.staging_s3}/{run_id}/",
        )
        df = spark.read.parquet(s3_prefix)
        new_watermark = df.agg({self.watermark_col: "max"}).first()[0]
        return df, str(new_watermark)
```

### 4.4 S3 Landing (Excel rollup historical seed, file-drop integrations)

```python
@register("s3_landing")
class S3LandingConnector:
    source_type = "s3_landing"

    def configure(self, source_row, spec):
        self.prefix = source_row["endpoint"]                    # e.g. "s3://tamimi-dlh-landing-prod/excel-rollup/"
        self.format = spec.get("file_format", "parquet")

    def read_incremental(self, spark, watermark, run_id):
        # Watermark = last-seen S3 object key (lexically sortable)
        df = spark.read.format(self.format).load(self.prefix)
        if watermark:
            df = df.where(f"input_file_name() > '{watermark}'")
        new_watermark = df.agg({"input_file_name()": "max"}).first()[0]
        return df, str(new_watermark)
```

## 5. How DDB drives "what to run when"

A single EventBridge cron (one per cadence band) triggers a dispatcher Lambda that walks `bronze_mapping` and starts the right Step Functions execution per row:

```python
# src/lambdas/scheduler/handler.py
def handler(event, _ctx):
    # event["cadence_band"] in {"hourly", "daily", "weekly"}
    cadence = event["cadence_band"]

    mappings = ddb_query(
        "bronze_mapping",
        filter=f"cadence_band = '{cadence}' AND enabled = true",
    )

    for m in mappings:
        # Each mapping → one SFN execution; SFN runs the Glue job, captures success/fail
        sfn.start_execution(
            stateMachineArn=cfg.bronze_sfn_arn,
            name=f"{m['bronze_table'].replace('.','-')}-{ulid()}",
            input=json.dumps({
                "bronze_table": m["bronze_table"],
                "source_id": m["source_id"],
                "spec_path": m["spec_path"],
            }),
        )
```

The Glue job, at run start, fetches:
- The `source_catalog` row for `source_id` → endpoint + secrets ARN
- The `watermarks` row for `(bronze_table, partition_key)` → last watermark
- The YAML spec from S3 (uploaded by CI on every deploy)

Then it calls `get_connector(source_row["source_type"])`, instantiates it, calls `configure()`, then `read_incremental()`, writes the DataFrame to Bronze, updates the watermark + runs tables.

## 6. Adding a new source — 5-minute walkthrough

A customer says "we also have a Shopify SaaS we want to pull from." The flow:

1. **Write the connector** (~30 LOC):
   ```python
   @register("shopify_rest")
   class ShopifyRestConnector:
       source_type = "shopify_rest"
       def configure(self, source_row, spec): ...
       def read_incremental(self, spark, watermark, run_id): ...
       def read_full(self, spark, run_id): ...
   ```
   PR → review → merge → CI builds the engine wheel.

2. **Insert one row in `source_catalog`** (operator UI or one-time DDB write):
   ```json
   { "source_id": "shopify-prod", "source_type": "shopify_rest",
     "endpoint": "https://tamimi.myshopify.com/admin/api/2024-04/",
     "secrets_arn": "arn:aws:secretsmanager:...:shopify-prod",
     "status": "active" }
   ```

3. **Insert N rows in `bronze_mapping`** (one per Shopify entity we want — `orders`, `customers`, etc.):
   ```json
   { "bronze_table": "ecommerce.shopify_orders", "source_id": "shopify-prod",
     "source_object": "orders.json", "spec_path": "src/glue/specs/bronze/shopify_orders.yaml",
     "cadence_cron": "0 */2 * * ? *", "enabled": true }
   ```

4. **Add the spec YAML** (`src/glue/specs/bronze/shopify_orders.yaml`):
   ```yaml
   table: ecommerce.shopify_orders
   schema:
     - {name: order_id, type: string, pii_class: internal}
     - {name: created_at, type: timestamp}
     - ...
   watermark_column: created_at
   ```
   PR → merge → CI uploads to S3.

**That's it.** No CDKTF change. No new Glue job CDKTF code. The dispatcher Lambda picks up the new `bronze_mapping` row on the next cron tick.

## 7. The hybrid: YAML in Git vs DDB at runtime (the canonical split)

This is THE design decision. Get it wrong and you either lose reviewability OR lose operational flexibility.

| Concern | Where it lives | Why |
|---|---|---|
| **Schema** (columns + types + PII classes) | **`specs/` YAML in Git** | Changes are reviewable + rollback-able + audit-friendly |
| **Transform rules** (renames, casts, splits, filters, DQ) | **`specs/` YAML in Git** | Same reason — these are business logic |
| **Source endpoint** (URL / RDS host / Workgroup ARN) | **`source_catalog` in DDB** | Hot-swap during cert rotation / DR / migration without redeploy |
| **Credentials** | **Secrets Manager** | Never in Git, never in plaintext DDB |
| **Watermarks** | **`watermarks` in DDB** | Mutable runtime state — Git would diff every cycle |
| **Run history** | **`runs` in DDB** | Append-heavy, TTL-managed |
| **Enable/disable a mapping** | **`bronze_mapping.enabled` in DDB** | Operator toggles via UI; no PR for "pause SAP pull during quarter-close" |
| **Cadence** | **`bronze_mapping.cadence_cron` in DDB** | Tuneable without code change |
| **The mapping itself** (which Bronze table comes from which source) | **`bronze_mapping` in DDB** | Operator can add new mappings without engineer involvement — within the schema YAMLs already authored |
| **Lineage** | **`lineage_edges` in DDB** | Derived from runs; not something humans author |

**The rule of thumb:** if a change requires understanding the code, it goes in Git. If it requires understanding the data + customer's operational rhythm, it goes in DDB.

## 8. Patterns

- **One protocol, many connectors.** Engine never `import`s concrete connectors — `get_connector(source_type)` returns whatever was `@register`'d.
- **Connectors are stateless.** Construct on each Glue run; don't pool across runs.
- **Fail loud on `KeyError`.** A `bronze_mapping` row referencing an unknown `source_type` should crash the dispatcher Lambda — not silently skip.
- **Connectors handle their own retries / throttles.** The engine just `try/except`s and writes the failure to `runs`.
- **Full snapshot is opt-in** — `read_full` is only called by the Sunday job; daily ticks call `read_incremental` only.

## 9. Anti-patterns

- ❌ **Don't put schema in DDB.** `bronze_mapping.schema = {...}` looks tempting; it breaks code review and rollback. Schema lives in `specs/` YAML.
- ❌ **Don't let connectors talk to each other.** A SAP connector that calls a Shopify connector mid-run is a debugging nightmare.
- ❌ **Don't hardcode `source_type` strings in the engine.** Always read from `source_catalog`; the engine should not enumerate types.
- ❌ **Don't store credentials in `source_catalog`** — `secrets_arn` is indirection through Secrets Manager.
- ❌ **Don't skip the watermark write on success.** A successful pull that fails to advance the watermark causes infinite re-reads on the next cycle.

## 10. Composes with

- **`PATTERN_DDB_CONTROL_PLANE`** — provides the 6 tables this pattern reads from / writes to.
- **`data/14_dynamic_glue_pyspark_medallion.md`** — the spec engine that calls connectors.
- **`PATTERN_CUSTOMER_MAINTAINED_DIM_DDB`** — uses the same DDB control plane for a different purpose (dim CRUD).
- **`SERVERLESS_LAMBDA_POWERTOOLS`** — the dispatcher Lambda.
- **`WORKFLOW_STEP_FUNCTIONS`** — orchestrates per-mapping pipeline executions.

## 11. Pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| Two devs `@register` the same `source_type` | Import-time `ValueError` | Decorator already prevents — keep that error loud |
| Connector swallows source-side errors | Watermark advances even on partial pull | Engine inspects connector's emit_metrics() for `rows_with_errors`; if > 0, do NOT advance watermark |
| Operator disables a mapping mid-run | In-flight Glue job hits a deleted `bronze_mapping` row | Glue job copies the mapping row into its run context at start; tolerate `enabled=false` |
| Schema YAML and DDB mapping drift | "I added the spec but the engine didn't pick it up" | CI uploads spec YAML to S3 with a hash; engine refuses to run if hash in DDB ≠ hash in S3 |
| A new connector type breaks the spec validator | Engine crashes on first run with new source | Per-connector validator function; dispatcher refuses to start the run if validator fails |

## 12. Acceptance criteria

- [ ] `SourceConnector` protocol defined in `src/glue/glue_engine/sources/protocol.py`
- [ ] At least 4 reference connectors registered (`sap_odata`, `rds_jdbc`, `redshift_unload`, `s3_landing`)
- [ ] Plugin registry rejects duplicate `source_type` registrations at import time
- [ ] Adding a new source requires: 1 connector class + 1 `source_catalog` row + N `bronze_mapping` rows + N spec YAMLs — NO CDKTF changes
- [ ] Dispatcher Lambda walks `bronze_mapping` filtered by cadence + enabled flag
- [ ] On failure, watermark is NOT advanced
- [ ] On success, run row is written with `rows_in`, `rows_out`, `duration_ms`
- [ ] Spec YAML hash in DDB matches S3 — engine fails loud on mismatch
- [ ] Operator UI can: add a `source_catalog` row, add a `bronze_mapping` row, toggle `enabled`, view recent runs — without filing a PR
