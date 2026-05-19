# SOP — CDK for Terraform (CDKTF) in Python

**Version:** 1.0 · **Last-reviewed:** 2026-05-12 · **Status:** Active
**Applies to:** CDKTF 0.20+ · Python 3.12+ · Terraform 1.9+ · `cdktf-cdktf-provider-aws ~> 19.x` · `constructs ~> 10.x`

---

## 1. Purpose

- Codify how the team writes Terraform-compatible IaC **in Python**, using HashiCorp's CDK for Terraform (CDKTF).
- Distinct from `LAYER_BACKEND_LAMBDA` (Lambda runtime) and from AWS CDK (which generates CloudFormation, not Terraform).
- The output of `cdktf synth` is **Terraform JSON** (`cdk.tf.json`) that `terraform plan/apply` consumes. CDKTF reuses every Terraform provider, the S3+DynamoDB state ecosystem, and the entire `terraform` CLI surface.
- This partial is the **construct-anatomy + project-layout + pitfalls** SOP. Pair with `CICD_GITHUB_OIDC_CDKTF` for the CI/CD half.

**When the SOW signals:** "Terraform required" + "team is Python-first" + "we don't want HCL" → use this partial. If "team prefers HCL" or "existing HCL Terraform repo" → use the HCL templates (`iac/01`, `iac/03`) instead.

---

## 2. Decision tree — CDKTF vs alternatives

| Constraint | Tool |
|---|---|
| Terraform-compatible **and** Python team | **CDKTF Python** (this partial) |
| Terraform-compatible **and** team prefers HCL | Plain Terraform HCL (`iac/01`, `iac/03`) |
| AWS-only **and** CloudFormation acceptable **and** Python team | AWS CDK Python (`iac/02`, `iac/04`) |
| AWS-only **and** non-CDKTF non-CFN | Pulumi (not currently a template) |

CDKTF's main trade-offs vs HCL Terraform:
- ✅ Single language with the app code (Glue PySpark, Lambda Python).
- ✅ Type-checking, refactoring, pytest unit tests on synth JSON.
- ✅ Composition via Python classes / inheritance.
- ⚠️ Smaller community than HCL Terraform.
- ⚠️ Generated JSON is harder to eyeball than HCL.
- ⚠️ Some new Terraform features land in HCL first.

---

## 3. Project layout (canonical)

```
infra/                                  # CDKTF Python project root
├── pyproject.toml                      # uv- or poetry-managed; ruff, mypy, pytest config
├── cdktf.json                          # CDKTF app config (entrypoint, provider versions)
├── .python-version                     # 3.12
├── .terraform-version                  # >= 1.9.0
├── main.py                             # cdktf App; instantiates one Stack per (env × workstream)
│
├── infra/                              # importable Python package
│   ├── constructs/                     # reusable building blocks (≈ Terraform modules)
│   │   ├── <name>.py                   # one Construct class per file
│   │   └── ...
│   ├── stacks/                         # one Stack class per workstream
│   │   ├── <workstream>_stack.py
│   │   └── ...
│   ├── config/                         # frozen dataclasses (NOT tfvars)
│   │   ├── base.py                     # EnvConfig with defaults
│   │   ├── dev.py
│   │   ├── qa.py
│   │   └── prod.py                     # NON-NEGOTIABLE safety flags
│   └── lib/                            # cross-cutting helpers (tags, naming, ARNs)
│
└── tests/
    ├── conftest.py
    └── unit/
        ├── test_prod_safety_flags.py   # pin Prod invariants
        ├── test_<construct>.py
        └── ...

global/                                 # account-level, applied once
├── tf_state_bootstrap/                 # PLAIN HCL (chicken-and-egg)
│   └── main.tf
└── iam_roles/                          # CDKTF Python (OIDC trust + per-env deploy roles)

.github/workflows/                      # see CICD_GITHUB_OIDC_CDKTF partial
src/                                    # application code (Glue, Lambda, Fargate)
```

**Stack-per-env, not workspace-per-env.** Workspaces share root code paths and state backends — too coupled for three separate AWS accounts that may concurrently hold different CDKTF revisions. Stacks are independent Python objects with their own `S3Backend` and `cdktf.out/stacks/<name>/cdk.tf.json`.

---

## 4. Construct anatomy (rules)

Every construct under `infra/constructs/` follows this shape:

```python
from constructs import Construct
from cdktf_cdktf_provider_aws.s3_bucket import S3Bucket

from infra.lib.tags import standard_tags
from infra.config.base import EnvConfig


class LakehouseBuckets(Construct):
    """One-line purpose statement.

    Owns: <what AWS resources>.
    Expects: <inputs as kwargs>.
    Exposes: <self.* attributes other constructs consume>.
    Depends on: <other constructs that must run before this>.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,                            # everything after id is keyword-only
        cfg: EnvConfig,
        kms_key_arn: str,
    ) -> None:
        super().__init__(scope, id)

        self.bronze = self._make_bucket("bronze", cfg.bronze_lifecycle_days, cfg, kms_key_arn)
        self.silver = self._make_bucket("silver", cfg.silver_lifecycle_days, cfg, kms_key_arn)
        self.gold   = self._make_bucket("gold",   cfg.gold_lifecycle_days,   cfg, kms_key_arn)

    def _make_bucket(self, layer: str, days: int, cfg: EnvConfig, kms: str) -> S3Bucket:
        return S3Bucket(
            self, f"{layer}_bucket",
            bucket=f"tamimi-dlh-{layer}-{cfg.env}-{cfg.region}",
            force_destroy=cfg.force_destroy_buckets,
            tags=standard_tags(cfg, workstream="lakehouse"),
        )
```

### Mandatory conventions

| Rule | Why |
|---|---|
| Constructors take `(scope, id, *, ...)` — all other args keyword-only | Prevents positional drift across refactors |
| Module docstring states: Owns / Expects / Exposes / Depends on | Future agents and humans understand boundaries in seconds |
| Public attributes (`self.bronze`) — no returning tuples or dicts | Easier composition; IDE autocomplete works |
| One construct = one responsibility | Resist bundling; refactor before composing 3+ unrelated resources |
| Tags applied via `standard_tags(cfg, workstream=...)` | Single source of truth; tag drift is impossible |
| Type-hint every parameter, attribute, and return | mypy strict; safety from refactors |
| `_private` methods for internals | Public surface is the contract |
| No `count`/`for_each` magic numbers in logic | Derive collections from `cfg` or explicit lists |

### IAM least-privilege

- Use `cdktf_cdktf_provider_aws.iam_policy_document.DataAwsIamPolicyDocument` for policies.
- No `Action: "*"`. No `Resource: "*"` outside whitelisted Sids (e.g. `STSCallerIdentityRead`).
- Test enforces this in CI: synth → parse → fail-if `*:*`.

---

## 5. Stack anatomy

```python
from constructs import Construct
from cdktf import TerraformStack, S3Backend
from cdktf_cdktf_provider_aws.provider import AwsProvider

from infra.config.base import EnvConfig
from infra.constructs.kms_keys import KmsKeys
from infra.constructs.lakehouse_buckets import LakehouseBuckets
# ... other imports


class LakehouseStack(TerraformStack):
    """One Stack class per workstream; instantiated once per env in main.py."""

    def __init__(self, scope: Construct, id: str, *, cfg: EnvConfig) -> None:
        super().__init__(scope, id)

        AwsProvider(self, "aws", region=cfg.region)
        S3Backend(
            self,
            bucket=f"tamimi-tfstate-{cfg.env}",
            key=f"lakehouse/{cfg.env}/terraform.tfstate",
            region=cfg.region,
            encrypt=True,
            dynamodb_table="tamimi-tflock",
        )

        kms = KmsKeys(self, "kms", cfg=cfg)
        buckets = LakehouseBuckets(self, "buckets", cfg=cfg, kms_key_arn=kms.layer_key_arn)
        # ... compose remaining constructs in dependency order
```

### Why backend is declared in the stack

- Same Python module = same backend wiring. No "where does the state live?" confusion.
- Cross-stack data sharing is **explicit** via `DataTerraformRemoteStateS3` — never via Python imports of resources from another stack.

---

## 6. Per-env config (frozen dataclasses)

Per-env values live in `infra/config/<env>.py` as `frozen=True` dataclasses, **NOT** in tfvars. The same Python code path runs for every environment; what changes is the `EnvConfig` instance.

```python
@dataclass(frozen=True)
class EnvConfig:
    env: str
    aws_account_id: str
    region: str = "eu-west-1"

    enable_deletion_protection: bool = False
    force_destroy_buckets: bool = True
    lake_formation_strict: bool = False
    kms_deletion_window_days: int = 7
    # ... many more fields
```

```python
# infra/config/prod.py
PROD = EnvConfig(
    env="prod",
    aws_account_id="...",
    enable_deletion_protection=True,    # NON-NEGOTIABLE
    force_destroy_buckets=False,        # NON-NEGOTIABLE
    lake_formation_strict=True,         # NON-NEGOTIABLE
    kms_deletion_window_days=30,        # NON-NEGOTIABLE (>= 30)
    # ...
)
```

**Prod-only safety flags are enforced by pytest invariants.** A PR that flips them fails CI.

```python
# tests/unit/test_prod_safety_flags.py
def test_prod_deletion_protection_on(): assert PROD.enable_deletion_protection is True
def test_prod_buckets_never_force_destroy(): assert PROD.force_destroy_buckets is False
def test_prod_lake_formation_strict(): assert PROD.lake_formation_strict is True
def test_prod_kms_deletion_window_at_least_30(): assert PROD.kms_deletion_window_days >= 30
```

---

## 7. State management

| Setting | Value |
|---|---|
| Backend | S3 + DynamoDB locking |
| State bucket | `{slug}-tfstate-<env>` in each env's account |
| Lock table | `{slug}-tflock` (DynamoDB, on-demand) |
| Encryption | SSE-KMS, per-account CMK |
| Versioning + lifecycle | Enabled; retain old versions 90 days |
| Cross-env isolation | Each env writes only to its own bucket |
| Cross-env reads | Explicit `DataTerraformRemoteStateS3` only; documented |

`global/tf_state_bootstrap/` is plain HCL Terraform because it cannot depend on the state bucket it creates. Apply once per account, never destroy.

---

## 8. Tags standard

```python
# infra/lib/tags.py
def standard_tags(cfg: EnvConfig, workstream: str) -> dict[str, str]:
    return {
        "project":     f"{CLIENT_SLUG}-{workstream}",
        "workstream":  workstream,
        "env":         cfg.env,
        "owner":       "northbay-data",     # or appropriate team
        "cost-center": "...",
        "managed-by":  "cdktf",
        "repo":        "...",
    }
```

Tested in `tests/unit/test_tags.py` — synth → parse JSON → assert every taggable resource has the 7 tags.

---

## 9. Synthesis + deploy flow

```bash
# install (once)
uv sync                          # or: poetry install
cd infra && cdktf get            # fetch provider bindings into the Python venv

# inspect
cdktf synth                      # writes cdktf.out/stacks/<name>/cdk.tf.json
cdktf diff <stack-name>          # Terraform plan for that stack

# deploy (CI in QA/Prod; local OK in Dev sandbox)
cdktf deploy <stack-name>

# destroy (Dev sandbox only; Prod has deletion-protection)
cdktf destroy <stack-name>
```

`cdktf deploy` invokes `terraform apply` under the hood against the synthesized JSON, using the `S3Backend` configured in the stack.

---

## 10. Testing on synth JSON

```python
import json
from cdktf import Testing
from infra.stacks.lakehouse_stack import LakehouseStack
from infra.config.dev import DEV


def test_all_buckets_have_sse_kms() -> None:
    synth = Testing.synth_scope(lambda app: LakehouseStack(app, "test", cfg=DEV))
    tf = json.loads(synth)
    for bucket in tf["resource"]["aws_s3_bucket"].values():
        bucket_name = bucket["bucket"]
        assert any(
            sse_cfg["bucket"] == bucket_name
            for sse_cfg in tf["resource"]
                .get("aws_s3_bucket_server_side_encryption_configuration", {})
                .values()
        ), f"bucket {bucket_name} missing SSE config"
```

Test patterns the team standardises:
- **Prod safety invariants** — `test_prod_safety_flags.py`.
- **IAM least-privilege** — parse policies, fail on `Action:"*"` or unscoped `Resource:"*"`.
- **Tag conformance** — every taggable resource has the 7-tag standard set.
- **Resource-property correctness** — versioning, encryption, deletion-protection per env.

---

## 11. Cross-stack references

Use `DataTerraformRemoteStateS3`, **not** Python imports across stacks:

```python
from cdktf import DataTerraformRemoteStateS3

shared = DataTerraformRemoteStateS3(
    self, "shared_state",
    bucket=f"tamimi-tfstate-{cfg.env}",
    key=f"shared/{cfg.env}/terraform.tfstate",
    region=cfg.region,
)
vpc_id = shared.get_string("vpc_id")
```

Document every cross-stack read in the consuming stack's docstring. Minimize them — they couple deploy order.

---

## 12. Pitfalls (every team hits these)

| Symptom | Cause | Fix |
|---|---|---|
| `cdktf synth` succeeds, `cdktf diff` says "no changes" repeatedly | `S3Backend` not in the stack | Add `S3Backend(self, ...)` in `__init__` |
| Local works, CI fails permissions | Local AWS creds differ from OIDC role | Verify role policy in `global/iam_roles/` matches what the synth touches |
| Cross-stack ref returns `${...}` literal at runtime | Reading the value in Python | Don't read inside Python — pass the token to another construct; CDKTF resolves at synth |
| `mypy --strict` fails on generated provider types | Generated bindings sometimes return `Any` | Wrap in a typed helper; never `# type: ignore` without a comment |
| Resource recreated on every diff | Mutable default in `EnvConfig` (e.g. list) | Use `field(default_factory=list)` |
| `cdktf deploy` fails with "no Terraform binary" | Local PATH missing terraform | Install Terraform >= 1.9.0; CDKTF wraps it but doesn't bundle it |
| Stack name collision in `cdktf.out/` | Two stacks with the same `id` | Stack IDs must be unique within the App — include env in the id |

---

## 13. HCL exception (the only one)

`global/tf_state_bootstrap/` is plain HCL Terraform — **never CDKTF**. Reason: the state bucket cannot depend on the state it stores. The bootstrap is:

```hcl
# global/tf_state_bootstrap/main.tf (excerpt)
resource "aws_s3_bucket" "state" {
  bucket = "${var.client_slug}-tfstate-${var.env}"
}
resource "aws_dynamodb_table" "lock" {
  name         = "${var.client_slug}-tflock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute { name = "LockID"; type = "S" }
}
```

Apply once per AWS account. Never `terraform destroy` it.

---

## 14. Composes with

- `LAYER_BACKEND_LAMBDA` — Lambda non-negotiables (when stack includes Lambdas).
- `SERVERLESS_LAMBDA_POWERTOOLS` — Lambda observability layer.
- `DATA_LAKE_FORMATION`, `DATA_GLUE_CATALOG`, `DATA_DMS_REPLICATION`, `DATA_ICEBERG_S3_TABLES` — when stack includes those services.
- `CICD_GITHUB_OIDC_CDKTF` — the CI/CD partner partial.

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **CDKTF** | CDK for Terraform — HashiCorp's framework for writing Terraform configurations in TS/Python/Go/Java/C# |
| **Construct** | A Python class that owns one or more AWS resources; ≈ Terraform module |
| **Stack** | A `TerraformStack` subclass; one per workstream; one instance per env |
| **App** | The top-level `cdktf.App()`; contains all stacks |
| **Synth** | `cdktf synth` — generates Terraform JSON from Python |
| **Token** | A CDKTF placeholder for a resource attribute resolved at synth time |
| **S3Backend** | The CDKTF construct that wires the state backend; declared inside the stack |

---

## 16. Acceptance criteria

A CDKTF Python project built per this SOP passes ALL of:

1. `uv sync` (or `poetry install`) — deps resolve.
2. `ruff check infra/` and `ruff format --check infra/` clean.
3. `mypy --strict infra/` clean.
4. `pytest tests/unit/` all pass (Prod safety, IAM least-privilege, tags, per-construct asserts).
5. `cdktf get` — providers fetched.
6. `cdktf synth` — every stack produces valid Terraform JSON.
7. `cdktf diff <dev-stack>` — clean against bootstrapped Dev state.
8. Every Prod stack JSON contains `deletion_protection: true` on every protectable resource.
9. README at `infra/` root explains bootstrap → synth → deploy → destroy.
