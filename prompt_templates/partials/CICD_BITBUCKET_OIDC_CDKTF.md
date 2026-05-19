# CICD_BITBUCKET_OIDC_CDKTF

**Status:** Authored 2026-05-19. Sibling to [`CICD_GITHUB_OIDC_CDKTF.md`](CICD_GITHUB_OIDC_CDKTF.md) — same shape (13 sections), BitBucket-specific syntax.

## 1. Purpose

Pipelines for CDKTF Python deployments using **BitBucket Pipelines** with **AWS OIDC** (no long-lived keys). Mirrors the GitHub Actions sibling partial; pick whichever your engagement runs.

**When to pick BitBucket over GitHub Actions:**
- Customer already uses BitBucket for source control (most common reason)
- Atlassian-suite enterprise (Jira / Confluence integration)
- Customer's CI budget is on BitBucket plan
- Otherwise GitHub Actions is the F369 default — see GitHub sibling partial.

## 2. End-state shape

```
<repo>/
├── bitbucket-pipelines.yml           # the ONE pipeline file (BitBucket convention)
├── infra/                            # CDKTF Python project
│   ├── pyproject.toml
│   ├── cdktf.json
│   └── ...
├── src/                              # Glue / Lambda / dbt
└── tests/
```

**Three logical pipelines, all in one `bitbucket-pipelines.yml`:**

1. **PR pipeline** — runs on every pull request → lint + type + unit tests + `cdktf synth` + plan summary as PR comment.
2. **Dev deploy** — runs on push to `main` → `cdktf deploy` against the Dev account.
3. **Promote** — runs on tag push (`qa-YYYY.WW` or `prod-YYYY.WW`) → `cdktf deploy` against QA / Prod with manual approval.

## 3. OIDC trust setup (one-time per AWS account)

BitBucket's OIDC issuer URL is **workspace-scoped**, unlike GitHub's repo-scoped issuer.

### 3.1 Get your Workspace OIDC config

In BitBucket: **Workspace settings → OpenID Connect**. Copy:
- **Identity provider URL:** `https://api.bitbucket.org/2.0/workspaces/<workspace-slug>/pipelines-config/identity/oidc`
- **Audience:** `ari:cloud:bitbucket::workspace/<workspace-uuid>`
- **Workspace UUID** (will be needed in the trust policy)

### 3.2 Create the OIDC identity provider in AWS IAM (per AWS account)

```python
# infra/global/oidc/main.py — bootstrap CDKTF
from cdktf_cdktf_provider_aws.iam_openid_connect_provider import IamOpenidConnectProvider

provider = IamOpenidConnectProvider(
    self, "bitbucket_oidc",
    url=f"https://api.bitbucket.org/2.0/workspaces/{cfg.workspace_slug}/pipelines-config/identity/oidc",
    client_id_list=[f"ari:cloud:bitbucket::workspace/{cfg.workspace_uuid}"],
    thumbprint_list=[cfg.bitbucket_oidc_thumbprint],  # see §3.3
)
```

### 3.3 Thumbprint

BitBucket's OIDC certificate thumbprint changes when their certs rotate. **Don't hardcode** — fetch at deploy time:

```python
# Run once and cache; refresh only when AWS reports thumbprint mismatch in CloudTrail
THUMBPRINT_PROVIDER_LOOKUP = "openssl s_client -showcerts -servername api.bitbucket.org -connect api.bitbucket.org:443 < /dev/null 2>/dev/null | openssl x509 -fingerprint -noout | sed 's/://g' | awk -F= '{print tolower($2)}'"
```

### 3.4 Three IAM roles — one per environment, scoped trust policy

```python
# infra/global/oidc/roles.py
def make_bitbucket_role(env: str, branch_pattern: str, repo_full_name: str):
    return IamRole(
        ...,
        name=f"tamimi-dlh-bitbucket-deploy-{env}",
        assume_role_policy=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Federated": provider.arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"api.bitbucket.org/2.0/workspaces/{cfg.workspace_slug}/pipelines-config/identity/oidc:aud": f"ari:cloud:bitbucket::workspace/{cfg.workspace_uuid}",
                    },
                    # Subject claim format: {repository_uuid}:{branch}:{step_uuid}
                    # Use StringLike for branch + step wildcards
                    "StringLike": {
                        f"api.bitbucket.org/2.0/workspaces/{cfg.workspace_slug}/pipelines-config/identity/oidc:sub": f"{{{cfg.repo_uuid}}}:{branch_pattern}:*",
                    },
                },
            }],
        }),
    )

# Per-env trust:
make_bitbucket_role("dev",  branch_pattern="main",        repo_full_name=cfg.repo)   # main → Dev
make_bitbucket_role("qa",   branch_pattern="qa-*",        repo_full_name=cfg.repo)   # qa-*  tags → QA
make_bitbucket_role("prod", branch_pattern="prod-*",      repo_full_name=cfg.repo)   # prod-* tags → Prod
```

**Critical:** the subject claim in BitBucket's JWT is `{REPO_UUID}:{BRANCH_OR_TAG}:{STEP_UUID}`, NOT a path like GitHub. The `{REPO_UUID}` curly braces are literal — they're part of the issued claim.

## 4. `bitbucket-pipelines.yml` — lint, type, test on every PR

```yaml
# bitbucket-pipelines.yml (top of file)
image: python:3.12-slim

definitions:
  caches:
    uv: ~/.cache/uv
    cdktf: infra/.gen
  services:
    docker:
      memory: 2048
  steps:
    - step: &lint-and-test
        name: Lint, type, unit tests
        caches:
          - uv
          - cdktf
        script:
          - pip install -q uv
          - cd infra && uv pip install --system -e ".[dev]"
          - uv run ruff check infra/ src/ tests/
          - uv run mypy --strict infra/ src/
          - uv run pytest tests/unit/ -q
          - cd infra && uv run cdktf get && uv run cdktf synth

pipelines:
  pull-requests:
    '**':
      - step: *lint-and-test

  branches:
    main:
      - step: *lint-and-test
      - step:
          name: Deploy to Dev
          oidc: true                                    # enables OIDC token issuance
          script:
            - export AWS_REGION=eu-west-1
            - export AWS_ROLE_ARN=$AWS_ROLE_DEV_ARN     # set in Repository Variables
            - export AWS_WEB_IDENTITY_TOKEN_FILE=$(pwd)/web-identity-token
            - echo $BITBUCKET_STEP_OIDC_TOKEN > $AWS_WEB_IDENTITY_TOKEN_FILE
            - pip install -q uv
            - cd infra && uv pip install --system -e .
            - uv run cdktf deploy lakehouse-dev --auto-approve
```

## 5. Promote to QA / Prod via tag pipelines

```yaml
  tags:
    'qa-*':
      - step: *lint-and-test
      - step:
          name: Deploy to QA
          oidc: true
          deployment: qa                                # BitBucket "deployments" group; sets env vars
          script:
            - export AWS_ROLE_ARN=$AWS_ROLE_QA_ARN
            - export AWS_WEB_IDENTITY_TOKEN_FILE=$(pwd)/web-identity-token
            - echo $BITBUCKET_STEP_OIDC_TOKEN > $AWS_WEB_IDENTITY_TOKEN_FILE
            - pip install -q uv
            - cd infra && uv pip install --system -e .
            - uv run cdktf deploy lakehouse-qa --auto-approve

    'prod-*':
      - step: *lint-and-test
      - step:
          name: Plan against Prod
          oidc: true
          script:
            - export AWS_ROLE_ARN=$AWS_ROLE_PROD_ARN
            - export AWS_WEB_IDENTITY_TOKEN_FILE=$(pwd)/web-identity-token
            - echo $BITBUCKET_STEP_OIDC_TOKEN > $AWS_WEB_IDENTITY_TOKEN_FILE
            - pip install -q uv
            - cd infra && uv pip install --system -e .
            - uv run cdktf diff lakehouse-prod
      - step:
          name: Deploy to Prod (manual approval)
          oidc: true
          trigger: manual                               # BitBucket gates this step
          deployment: production                        # uses "production" deployment env
          script:
            - export AWS_ROLE_ARN=$AWS_ROLE_PROD_ARN
            - export AWS_WEB_IDENTITY_TOKEN_FILE=$(pwd)/web-identity-token
            - echo $BITBUCKET_STEP_OIDC_TOKEN > $AWS_WEB_IDENTITY_TOKEN_FILE
            - pip install -q uv
            - cd infra && uv pip install --system -e .
            - uv run cdktf deploy lakehouse-prod --auto-approve
```

## 6. Two-approver gate for Prod

BitBucket Pipelines doesn't have GitHub-style "required reviewers" on deployments natively. Achieve the gate by:

1. **Branch restrictions:** in `Repository settings → Branch restrictions`, set `prod-*` tag creation to require 2 PR reviewers on the underlying commit (before tag is allowed).
2. **Manual trigger** on the Prod deploy step (above) — at least one human clicks "Run".
3. **Deployment-level audit** in `Deployments → production` records who approved.

Combine with a Jira gate (workflow status = "Approved for Prod") for the harder enterprise audit story.

## 7. Workspace / Repository / Deployment variables

Three variable scopes in BitBucket. Use the **most-scoped** that satisfies the need:

| Variable | Scope | Why |
|---|---|---|
| `AWS_ROLE_DEV_ARN` | Repository | Specific to one repo, dev only |
| `AWS_ROLE_QA_ARN` | Deployment (qa) | Used only by the `qa` deployment |
| `AWS_ROLE_PROD_ARN` | Deployment (production) | Used only by Prod |
| `SLACK_WEBHOOK_URL` | Workspace | Shared across all repos in the workspace |
| Database passwords | NEVER here. Always Secrets Manager → fetched at job run time. | — |

**Secrets that need encryption at rest** (e.g. tokens for non-AWS systems): mark "Secured" when adding — BitBucket masks them in logs and stores encrypted.

## 8. Caching for `uv` / dbt / Python deps

```yaml
definitions:
  caches:
    uv: ~/.cache/uv               # uv's binary + wheel cache — biggest speedup
    pip-wheel: ~/.cache/pip       # if pip is used elsewhere
    dbt-target: dbt_project/target  # incremental dbt parse cache
    cdktf-gen: infra/.gen         # cdktf's generated provider bindings — saves 30-60s per run
```

Reference them in any step: `caches: [uv, cdktf-gen]`. BitBucket evicts caches > 7 days old automatically.

## 9. Branch / tag restrictions (declarative in `bitbucket-pipelines.yml`)

```yaml
pipelines:
  pull-requests:
    '**':                          # all PRs run lint+test
      - step: *lint-and-test

  branches:
    main:                          # only main can trigger Dev deploy
      - step: *lint-and-test
      - step: ...

  tags:
    'qa-*':                        # qa-YYYY.WW tag triggers QA promote
      - ...
    'prod-*':                      # prod-YYYY.WW tag triggers Prod promote
      - ...
```

Anything not matched here doesn't run. Tag patterns use shell-glob syntax (not regex).

## 10. Pre-apply gates (paste-into-tag-or-PR-comment checklist)

```
- [ ] Linked Jira ticket: <key>
- [ ] All PRs feeding this tag are reviewed + merged
- [ ] `cdktf diff` posted in this comment (no surprises)
- [ ] All `pytest tests/unit/` and `tests/invariants/` green on the SHA
- [ ] Prod safety flags untouched (`enable_deletion_protection=True`, `force_destroy_buckets=False`, `lake_formation_strict=True`)
- [ ] Backout plan: `git revert` + redeploy previous tag → ETA <N> min
- [ ] On-call paged: <name>
- [ ] Approvers (2, one must be Tech Lead):
  - Approver 1: <name>
  - Approver 2: <name>
```

## 11. Pitfalls (every team using BitBucket Pipelines hits these)

| Pitfall | Symptom | Fix |
|---|---|---|
| OIDC token never read into env | `Unable to locate credentials` | Make sure `oidc: true` is set on the step AND `AWS_WEB_IDENTITY_TOKEN_FILE` is exported before AWS SDK runs |
| Wrong subject-claim format in trust policy | `AccessDenied` from STS | Subject is `{REPO_UUID}:{BRANCH}:{STEP_UUID}` — curly braces are literal characters in the claim |
| Caches not honored between runs | Each run takes 5+ min on `uv pip install` | Cache key must match a path or hash — see §8 definitions |
| Tag deploy runs from default branch SHA | Tag was created from a feature branch but deploy uses `main`'s code | Set `BITBUCKET_TAG`-aware checkout: `git checkout $BITBUCKET_TAG` at start of the step |
| Manual gate auto-skipped on retry | Re-running a failed pipeline skips the manual step | BitBucket re-prompts; if your team uses `--retry`, audit who clicked |
| Memory exhaustion on `cdktf synth` for large projects | OOMKilled at synth time | `services.docker.memory: 4096` in definitions |
| Tag deletion does NOT cleanup | A botched tag stays in history | `git push --delete origin qa-2026.20` — and remove the auto-created BitBucket deployment artifact manually |

## 12. Composes with

- **`IAC_CDKTF_PYTHON`** — the CDKTF Python project this pipeline deploys
- **`LAYER_SECURITY`** — IAM roles and trust policies for the 3 deploy roles
- **`PATTERN_DDB_CONTROL_PLANE`** — operational dashboard data lives in DDB; pipeline can write run metadata there as a finishing step
- Choose this OR **`CICD_GITHUB_OIDC_CDKTF`** — never both in one repo

## 13. Acceptance criteria

- [ ] Workspace OIDC identity provider registered in each AWS account
- [ ] 3 deploy roles (Dev / QA / Prod) with correctly-scoped trust policies (workspace UUID + repo UUID + branch/tag pattern)
- [ ] `bitbucket-pipelines.yml` lints clean (`bitbucket-pipelines validate` via `pipelines:debug` step)
- [ ] PR pipeline runs lint + type + test + synth on every PR, posts plan summary
- [ ] Push to `main` deploys Dev; `cdktf diff` against Dev returns "no changes"
- [ ] Tag `qa-YYYY.WW` deploys QA with no Dev-tier flags set
- [ ] Tag `prod-YYYY.WW` requires manual step trigger; deploy log records who clicked
- [ ] Workspace audit (`Deployments` view) shows full deploy history
- [ ] All caches (`uv`, `cdktf-gen`) restored on second run — pipeline duration drops 60s+
- [ ] No long-lived AWS keys anywhere — `git grep "AKIA"` returns empty
