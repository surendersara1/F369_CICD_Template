# SOP — AWS Glue Data Quality (DQDL · recommendations · scheduled rules · data contracts · drift detection · CW alarms)

**Version:** 2.0 · **Last-reviewed:** 2026-04-28 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Glue Data Quality (GA Nov 2023) · Data Quality Definition Language (DQDL) · Recommendations engine · Scheduled rule sets · Glue Data Catalog integration · Glue ETL job integration · CloudWatch metrics + alarms · S3-stored ruleset history

---

## 1. Purpose

- Codify **AWS Glue Data Quality** as the canonical AWS-native data quality monitoring + enforcement. Replaces hand-rolled great_expectations / dbt-tests setups for Glue-based data pipelines.
- Codify **DQDL (Data Quality Definition Language)** — declarative rule syntax (e.g., `Completeness "user_id" > 0.95`, `IsUnique "order_id"`).
- Codify **recommendations engine** — auto-generates DQDL rules from a sample dataset; reviewer approves → publishes ruleset.
- Codify **scheduled rule evaluation** — daily/hourly checks; failures publish to EventBridge → SNS / Lambda for remediation.
- Codify **data contracts** — Producer publishes ruleset; Consumer validates via DQDL on every ingest. Enforces "schema + business rules" agreement.
- Codify **drift detection** — compare current dataset stats vs baseline; alarm on drift.
- Codify **integration with Glue ETL jobs** — fail job on quality failure; quarantine bad data; log to dead-letter.
- This is the **data quality specialisation**. Pairs with `DATA_DATAZONE_V2` (governance) + `DATA_MESH_PATTERNS` (org-wide) + `DATA_GLUE_CATALOG` (schema).

When the SOW signals: "data quality program", "DQDL", "data contracts", "validate before downstream", "data drift detection", "data quality dashboard".

---

## 2. Decision tree — what to validate, when

```
Validation timing:
├── Pre-load (ETL job validates before write)        → §3 Inline DQ in Glue job
├── Post-load (scheduled check on table after write)  → §4 Scheduled DQ ruleset
├── Pre-consumption (consumer validates before query) → §5 Data contracts pattern
└── Continuous (CloudWatch + alarms)                   → §6 Drift detection

Rule categories:
├── Completeness (NULL ratios)
├── Uniqueness (PK constraints)
├── Range / boundary (min/max, regex match)
├── Cardinality (distinct counts)
├── Referential integrity (foreign-key checks)
├── Drift (current vs baseline distribution)
└── Custom SQL (arbitrary domain logic)
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single ruleset on 1 table; scheduled hourly | **§3 Inline DQ** |
| Production — 50+ tables, data contracts, drift, integrated with DataZone | **§5 Production** |

---

## 3. Inline DQ in Glue ETL Job

### 3.1 CDK

```python
# stacks/glue_dq_stack.py
from aws_cdk import Stack
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_kms as kms
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_sns as sns
from constructs import Construct


class GlueDqStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 catalog_db_name: str, table_name: str,
                 kms_key: kms.IKey, alert_topic: sns.ITopic, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. DQDL ruleset (declarative) ─────────────────────────────
        # Stored in S3 as text; referenced by ETL job + scheduled run
        ruleset_text = """
Rules = [
    # Completeness (no nulls expected)
    IsComplete "user_id",
    IsComplete "event_timestamp",
    
    # Uniqueness
    IsUnique "event_id",
    
    # Domain values
    ColumnValues "event_type" in ["page_view", "click", "purchase", "signup"],
    ColumnValues "country" matches "^[A-Z]{2}$",
    
    # Range / boundary
    ColumnValues "amount_cents" between 0 and 1000000,
    ColumnLength "user_id" between 8 and 64,
    
    # Referential — foreign keys
    ReferentialIntegrity "user_id" "users.user_id" >= 0.99,
    
    # Cardinality
    DistinctValuesCount "country" between 1 and 250,
    Sum "amount_cents" between 1000 and 100000000,
    
    # Statistical
    Mean "amount_cents" between 100 and 10000,
    StandardDeviation "amount_cents" between 50 and 5000,
    
    # Custom SQL (any logic)
    CustomSql "SELECT COUNT(*) FROM primary WHERE event_timestamp > current_timestamp" = 0
]
"""

        # ── 2. DQ ruleset resource ────────────────────────────────────
        ruleset = glue.CfnDataQualityRuleset(self, "EventsRuleset",
            name=f"{env_name}-events-ruleset",
            description="Quality rules for events table",
            ruleset=ruleset_text,
            target_table=glue.CfnDataQualityRuleset.DataQualityTargetTableProperty(
                database_name=catalog_db_name,
                table_name=table_name,
            ),
            tags={"env": env_name, "table": table_name},
        )

        # ── 3. Glue ETL job role with DQ permissions ──────────────────
        job_role = iam.Role(self, "EtlJobRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
            ],
        )
        job_role.add_to_policy(iam.PolicyStatement(
            actions=["glue:StartDataQualityRuleRecommendationRun",
                     "glue:GetDataQualityRuleset", "glue:ListDataQualityRulesets",
                     "glue:StartDataQualityRulesetEvaluationRun",
                     "glue:GetDataQualityRulesetEvaluationRun",
                     "glue:PublishDataQualityResult"],
            resources=["*"],
        ))

        # ── 4. Glue ETL job (PySpark) — inline DQ ─────────────────────
        job = glue.CfnJob(self, "EtlJob",
            name=f"{env_name}-events-etl-with-dq",
            role=job_role.role_arn,
            command=glue.CfnJob.JobCommandProperty(
                name="glueetl",
                python_version="3",
                script_location=f"s3://{scripts_bucket.bucket_name}/etl/events_with_dq.py",
            ),
            glue_version="4.0",
            number_of_workers=10,
            worker_type="G.1X",
            default_arguments={
                "--enable-glue-datacatalog": "true",
                "--enable-data-quality": "true",                # KEY: enable DQ
                "--data-quality-ruleset": ruleset.attr_name,    # ruleset to apply
                "--data-quality-fail-on-failure": "true",        # fail job if DQ fails
                "--enable-spark-ui": "true",
                "--enable-job-insights": "true",
            },
        )
```

### 3.2 PySpark script with DQ inline

```python
# scripts/etl/events_with_dq.py
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from awsgluedq.transforms import EvaluateDataQuality

args = getResolvedOptions(sys.argv, ["JOB_NAME"])
glueContext = GlueContext(SparkContext.getOrCreate())
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

# 1. Load source data
events_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="prod_raw",
    table_name="events_raw",
    push_down_predicate="day=current_date()",
)

# 2. Apply transformations
events_curated = events_dyf.apply_mapping([
    ("event_id", "string", "event_id", "string"),
    ("user_id", "string", "user_id", "string"),
    ("event_type", "string", "event_type", "string"),
    ("amount_cents", "long", "amount_cents", "long"),
    ("event_timestamp", "string", "event_timestamp", "timestamp"),
    ("country", "string", "country", "string"),
])

# 3. EVALUATE DATA QUALITY inline
ruleset_text = """
Rules = [
    IsComplete "user_id",
    IsComplete "event_timestamp",
    ColumnValues "event_type" in ["page_view", "click", "purchase", "signup"],
    ColumnValues "amount_cents" between 0 and 1000000
]
"""

dq_results = EvaluateDataQuality.apply(
    frame=events_curated,
    ruleset=ruleset_text,
    publishing_options={
        "dataQualityEvaluationContext": "events_etl",
        "enableDataQualityCloudWatchMetrics": True,           # publish to CW
        "enableDataQualityResultsPublishing": True,             # publish to Glue catalog
        "resultsS3Prefix": "s3://dq-results/events/",
    },
)

# 4. If DQ failed, route to quarantine + fail job
# (with --data-quality-fail-on-failure: true, job stops here on failure)

# 5. If passed, write to curated layer
glueContext.write_dynamic_frame.from_catalog(
    frame=events_curated,
    database="prod_curated",
    table_name="events",
    additional_options={"compression": "snappy"},
)

job.commit()
```

---

## 4. Scheduled DQ Ruleset (post-load monitoring)

### 4.1 EventBridge schedule + Lambda triggers DQ run

```python
# Schedule daily evaluation
events.Rule(self, "DailyDqRun",
    schedule=events.Schedule.cron(hour="3", minute="0"),       # 3 AM UTC daily
    targets=[targets.LambdaFunction(dq_runner_fn)],
)
# dq_runner_fn:
#   start_data_quality_ruleset_evaluation_run(
#       DataSource={"GlueTable": {"DatabaseName": ..., "TableName": ...}},
#       Role=role_arn,
#       NumberOfWorkers=5,
#       Timeout=120,                                            # min
#       AdditionalRunOptions={"CloudWatchMetricsEnabled": "true"},
#       RulesetNames=[ruleset_name],
#   )

# DQ failures → EventBridge event
events.Rule(self, "DqFailedRule",
    event_pattern={
        "source": ["aws.glue"],
        "detail-type": ["Glue Data Quality Evaluation Results Available"],
        "detail": {
            "context": {"runState": ["FAILED"]},
        },
    },
    targets=[targets.SnsTopic(alert_topic)],
)
```

---

## 5. Production Variant — recommendations + data contracts + drift

### 5.1 Auto-generate rules via Recommendations

```python
import boto3
glue = boto3.client("glue")

# Run recommendations on a table — auto-suggests DQDL rules
resp = glue.start_data_quality_rule_recommendation_run(
    DataSource={"GlueTable": {"DatabaseName": "prod_curated", "TableName": "events"}},
    Role=role_arn,
    NumberOfWorkers=5,
    Timeout=60,
)
run_id = resp["RunId"]

# After ~5 min:
result = glue.get_data_quality_rule_recommendation_run(RunId=run_id)
recommended_ruleset = result["RecommendedRuleset"]
print(recommended_ruleset)
# Sample output:
# Rules = [
#   IsComplete "user_id",
#   IsUnique "event_id",
#   ColumnValues "amount_cents" between 0 and 9876543,
#   StandardDeviation "amount_cents" between 100 and 1500,
#   ColumnValues "event_type" in ["click", "page_view", "purchase", "signup"]
# ]

# Reviewer approves → save as ruleset
glue.create_data_quality_ruleset(
    Name="events-recommended",
    Ruleset=recommended_ruleset,
    TargetTable={"DatabaseName": "prod_curated", "TableName": "events"},
)
```

### 5.2 Data contracts pattern

```yaml
# Producer team publishes contract via Git PR
# data-contracts/orders-table.yaml
apiVersion: data-contract/v1
producer: orders-team
consumer: [analytics-team, finance-team, ml-team]
table: prod_curated.orders

schema:
  columns:
    - name: order_id
      type: string
      constraints: [unique, not_null]
    - name: user_id
      type: string
      constraints: [not_null, foreign_key(prod_curated.users.user_id)]
    - name: total_cents
      type: bigint
      constraints: [not_null, range(0, 100000000)]
    - name: status
      type: string
      constraints: [in_set(["pending", "confirmed", "shipped", "delivered", "cancelled"])]

quality_rules: |
  IsComplete "order_id",
  IsUnique "order_id",
  ColumnValues "total_cents" between 0 and 100000000,
  ColumnValues "status" in ["pending", "confirmed", "shipped", "delivered", "cancelled"],
  ReferentialIntegrity "user_id" "users.user_id" >= 0.99

slo:
  freshness: 1h         # data must be < 1h stale
  completeness: 0.99    # ≥ 99% non-null
  availability: 99.9    # 99.9% of scheduled DQ runs pass
```

```python
# CDK auto-generates DQDL ruleset from contract
# Consumer can validate before query via DQ run
# Failures → EventBridge → producer + consumer team alerts
```

### 5.3 Drift detection

```python
# Compare current stats to baseline (snapshot from initial deploy)
# DQ ruleset can include drift checks:
ruleset_drift = """
Rules = [
    # Distribution drift on amount_cents
    Mean "amount_cents" between 9000 and 11000,        # baseline ± 10%
    StandardDeviation "amount_cents" between 4500 and 5500,
    
    # Cardinality drift on country
    DistinctValuesCount "country" between 200 and 250,  # expect 220 ± 14%
    
    # Volume drift
    RowCount between 100000 and 1000000,                # expect ~500K daily
]
"""
```

---

## 6. Common gotchas

- **DQDL is whitespace-sensitive** in some constructs. Validate via `glue:StartDataQualityRulesetEvaluationRun --dry-run` before saving.
- **Recommendations engine takes 5-30 min** — schedule async; don't block.
- **Inline DQ in ETL job** can add 10-50% to job runtime (extra Spark work). For huge tables, consider scheduled DQ post-load instead.
- **`--data-quality-fail-on-failure: true`** stops job on rule failure. For warning-only mode, use `false` + alert via CW alarm.
- **Custom SQL rules** can be expensive — runs full table scan. Cap with `LIMIT` or row sampling.
- **CloudWatch metrics** are free per evaluation but at high frequency cost adds up. Default sampling.
- **Ruleset versioning** — each save creates new version; track via Glue API.
- **Performance: 1B-row table DQ takes 30-60 min** with 10 workers. Plan timeouts.
- **ReferentialIntegrity rule** does an anti-join — expensive on huge fact tables. Sample if needed.
- **Data contracts in Git** — version control + reviewable. Without Git, contracts drift.
- **Drift detection** requires baseline — capture once during initial deploy, refresh quarterly.
- **Failure remediation**: alarm-only (just notify) vs auto-quarantine (move bad data) vs auto-rollback (revert ETL output). Pick per-table strategy.

---

## 7. Pytest worked example

```python
# tests/test_glue_dq.py
import boto3, pytest

glue = boto3.client("glue")


def test_ruleset_exists(ruleset_name):
    rs = glue.get_data_quality_ruleset(Name=ruleset_name)
    assert rs["Name"] == ruleset_name
    assert rs["Ruleset"]


def test_recent_evaluation_succeeded(ruleset_name):
    runs = glue.list_data_quality_results()["Results"]
    matching = [r for r in runs if r.get("RulesetName") == ruleset_name]
    assert matching
    latest = matching[0]
    assert latest["Status"] == "SUCCEEDED"
    assert latest.get("Score", 0) >= 0.95              # ≥ 95% rules passed


def test_etl_job_uses_dq(job_name):
    job = glue.get_job(JobName=job_name)["Job"]
    args = job["DefaultArguments"]
    assert args.get("--enable-data-quality") == "true"
    assert args.get("--data-quality-ruleset")


def test_critical_rule_passing(ruleset_name):
    """IsUnique rule on event_id must always be 100%."""
    runs = glue.list_data_quality_results()["Results"]
    matching = [r for r in runs if r.get("RulesetName") == ruleset_name]
    latest_id = matching[0]["ResultId"]
    detail = glue.get_data_quality_result(ResultId=latest_id)
    
    rule_results = detail["RuleResults"]
    unique_rules = [r for r in rule_results if "IsUnique" in r.get("Description", "")]
    for r in unique_rules:
        assert r["Result"] == "PASS", f"{r['Name']}: {r['Result']}"
```

---

## 8. Five non-negotiables

1. **DQDL ruleset stored in Git** — version-controlled, reviewable.
2. **Inline DQ for critical pipelines** + scheduled DQ for monitoring.
3. **CloudWatch metrics enabled** — without metrics, no historical trend.
4. **Data contracts** for any cross-team consumer table — schema + rules + SLO.
5. **DQ failures → SNS + EventBridge** to relevant team owners (not generic ops).

---

## 9. References

- [AWS Glue Data Quality](https://docs.aws.amazon.com/glue/latest/dg/glue-data-quality.html)
- [DQDL reference](https://docs.aws.amazon.com/glue/latest/dg/dqdl.html)
- [EvaluateDataQuality transform](https://docs.aws.amazon.com/glue/latest/dg/glue-etl-data-quality.html)
- [Recommendations engine](https://docs.aws.amazon.com/glue/latest/dg/glue-data-quality-rule-recommendation.html)
- [DQ + EventBridge integration](https://docs.aws.amazon.com/glue/latest/dg/data-quality-eventbridge.html)
- [Data Contracts pattern](https://datacontract.com/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-28 | Initial. Glue Data Quality + DQDL + recommendations + scheduled rules + data contracts + drift detection + CW integration. Wave 19. |
