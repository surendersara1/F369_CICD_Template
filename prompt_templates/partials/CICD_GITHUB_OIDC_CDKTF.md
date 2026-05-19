# SOP — GitHub Actions + OIDC + CDKTF deploy workflow

**Version:** 1.0 · **Last-reviewed:** 2026-05-12 · **Status:** Active
**Applies to:** GitHub Actions · `aws-actions/configure-aws-credentials@v4+` · CDKTF 0.20+ · Python 3.12+ · `terraform >= 1.9.0`

---

## 1. Purpose

Codify the **CI/CD pipeline** that backs a CDKTF Python project. No long-lived AWS keys. Plan-on-PR. Auto-deploy to Dev on `main`. Tag-driven promotion to QA / Prod. Two-approver manual gate on Prod.

Pair with `IAC_CDKTF_PYTHON` (the IaC SOP). This partial covers only the pipeline.

**When the SOW signals:** Python+CDKTF stack, GitHub-hosted repo, AWS deployment targets. For other CI platforms (GitLab, Bitbucket, CodePipeline, Jenkins) the same pattern translates — sections 3–7 are platform-specific; sections 2 and 8+ are universal.

---

## 2. End-state shape

```
.github/
└── workflows/
    ├── lint-and-test.yml         # every PR: ruff + mypy + pytest + cdktf synth + cdktf diff
    ├── deploy-dev.yml            # push to main: lint-and-test → cdktf deploy <dev-stack>
    └── deploy-promote.yml        # tag qa-* or prod-*: lint-and-test → cdktf deploy <env-stack>
                                  # Prod: GitHub `production` environment approval (2 approvers)
```

```
infra/
├── .terraform-version            # >= 1.9.0
├── .python-version               # 3.12
└── ...
```

**Branch + tag strategy:**

| Branch / Tag | Deploys to | Approval |
|---|---|---|
| `feat/*` | nothing | PR review (≥1 reviewer) |
| `main` | **Dev** (auto) | Pre-merge PR review |
| Tag `qa-YYYY.WW` | **QA** (auto on tag) | Tech Lead tags |
| Tag `prod-YYYY.WW` | **Prod** (manual approval) | Tag **+** GitHub `production` environment gate (2 approvers) |

Promotion is one-way: a `prod-*` tag can only point at a commit already deployed to QA.

---

## 3. OIDC trust setup (one-time per AWS account)

Create the OIDC provider + per-env IAM role in `global/iam_roles/` (CDKTF Python). The OIDC provider URL is `https://token.actions.githubusercontent.com`. Trust the role to `repo:<org>/<repo>:ref:refs/heads/main` (Dev), `refs/tags/qa-*` (QA), `refs/tags/prod-*` (Prod).

```python
# global/iam_roles/infra/constructs/github_oidc_role.py (excerpt)
trust_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": oidc_provider.arn},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
            "StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            },
            "StringLike": {
                "token.actions.githubusercontent.com:sub": [
                    f"repo:{org}/{repo}:ref:refs/heads/main",        # Dev only
                    # Or for QA/Prod, the per-env role has only:
                    # f"repo:{org}/{repo}:ref:refs/tags/qa-*",
                    # f"repo:{org}/{repo}:ref:refs/tags/prod-*",
                ]
            }
        }
    }]
}
```

**Three distinct IAM roles** — one per env. Each role's policy is the **minimum** CDKTF needs to manage that env (e.g. `s3:*`, `glue:*`, `lakeformation:*`, `iam:PassRole` for the deploy targets — but scoped to the project's resources, not `*`).

---

## 4. `lint-and-test.yml` (runs on every PR)

```yaml
# .github/workflows/lint-and-test.yml
name: lint-and-test

on:
  pull_request:
    paths:
      - "infra/**"
      - "src/**"
      - ".github/workflows/**"

permissions:
  id-token: write          # OIDC
  contents: read
  pull-requests: write     # post plan summary

env:
  AWS_REGION: eu-west-1

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      matrix:
        env: [dev]         # only diff against Dev on PR; QA/Prod diff in promote workflows
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version-file: infra/.python-version

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: $(cat infra/.terraform-version)
          terraform_wrapper: false

      - name: Install uv
        run: pipx install uv

      - name: Install deps
        working-directory: infra
        run: uv sync --dev

      - name: ruff check
        working-directory: infra
        run: uv run ruff check .

      - name: ruff format check
        working-directory: infra
        run: uv run ruff format --check .

      - name: mypy strict
        working-directory: infra
        run: uv run mypy --strict .

      - name: pytest unit
        working-directory: infra
        run: uv run pytest tests/unit/ -v --tb=short

      - name: Configure AWS credentials (dev)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_DEV }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Install CDKTF CLI
        run: npm install -g cdktf-cli@0.20

      - name: cdktf get
        working-directory: infra
        run: cdktf get

      - name: cdktf synth
        working-directory: infra
        run: cdktf synth

      - name: tfsec on synthesized JSON
        run: |
          for stack in infra/cdktf.out/stacks/*/; do
            tfsec --tfvars-file /dev/null "$stack" || true
          done

      - name: cdktf diff (dev)
        working-directory: infra
        run: cdktf diff lakehouse-dev 2>&1 | tee plan-dev.txt

      - name: Upload plan
        uses: actions/upload-artifact@v4
        with:
          name: plan-dev
          path: infra/plan-dev.txt

      - name: Post plan summary to PR
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const plan = fs.readFileSync('infra/plan-dev.txt', 'utf8');
            const summary = plan.split('\n').slice(-50).join('\n');
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: '## CDKTF plan (dev)\n```\n' + summary + '\n```'
            });
```

---

## 5. `deploy-dev.yml` (runs on push to main)

```yaml
name: deploy-dev

on:
  push:
    branches: [main]
    paths:
      - "infra/**"
      - "src/**"

permissions:
  id-token: write
  contents: read

env:
  AWS_REGION: eu-west-1

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: development          # GitHub environment (no manual approval for dev)
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with: { python-version-file: infra/.python-version }

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: $(cat infra/.terraform-version)
          terraform_wrapper: false

      - run: pipx install uv && cd infra && uv sync --dev

      - name: Lint & test (gate)
        working-directory: infra
        run: |
          uv run ruff check . && \
          uv run ruff format --check . && \
          uv run mypy --strict . && \
          uv run pytest tests/unit/

      - name: Configure AWS (dev)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_DEV }}
          aws-region: ${{ env.AWS_REGION }}

      - run: npm install -g cdktf-cli@0.20

      - name: cdktf deploy dev
        working-directory: infra
        run: cdktf deploy lakehouse-dev askbaba-delta-dev --auto-approve

      - name: Smoke test
        run: ./scripts/smoke-test-dev.sh
```

---

## 6. `deploy-promote.yml` (runs on tag push)

```yaml
name: deploy-promote

on:
  push:
    tags:
      - "qa-*"
      - "prod-*"

permissions:
  id-token: write
  contents: read

env:
  AWS_REGION: eu-west-1

jobs:
  detect-env:
    runs-on: ubuntu-latest
    outputs:
      env: ${{ steps.set.outputs.env }}
    steps:
      - id: set
        run: |
          if [[ "${GITHUB_REF_NAME}" == qa-* ]]; then
            echo "env=qa" >> $GITHUB_OUTPUT
          elif [[ "${GITHUB_REF_NAME}" == prod-* ]]; then
            echo "env=prod" >> $GITHUB_OUTPUT
          else
            echo "Unknown tag prefix" >&2
            exit 1
          fi

  deploy:
    needs: detect-env
    runs-on: ubuntu-latest
    environment: ${{ needs.detect-env.outputs.env }}    # GitHub manual-approval gate for `production`
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python + Terraform + uv (same as deploy-dev)
        # ... abbreviated

      - name: Lint & test (gate)
        working-directory: infra
        run: |
          uv run ruff check . && \
          uv run mypy --strict . && \
          uv run pytest tests/unit/

      - name: Configure AWS (env-specific)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ needs.detect-env.outputs.env == 'prod'
                              && secrets.AWS_DEPLOY_ROLE_PROD
                              || secrets.AWS_DEPLOY_ROLE_QA }}
          aws-region: ${{ env.AWS_REGION }}

      - run: npm install -g cdktf-cli@0.20

      - name: cdktf deploy
        working-directory: infra
        run: |
          ENV="${{ needs.detect-env.outputs.env }}"
          cdktf deploy "lakehouse-$ENV" "askbaba-delta-$ENV" --auto-approve

      - name: Smoke test
        run: ./scripts/smoke-test.sh ${{ needs.detect-env.outputs.env }}
```

GitHub `production` environment is configured with:
- **Required reviewers:** 2 (one must be Tech Lead).
- **Wait timer:** 0 (no forced delay).
- **Deployment branches and tags:** only `prod-*` tags.

---

## 7. Promotion checklist (manual, paste into the approving PR/tag comment)

When tagging `prod-YYYY.WW`, the tagger and approvers verify:

- [ ] QA has run the same `cdktf.out/` JSON for ≥ 3 days.
- [ ] No active incidents or open critical alarms on Prod.
- [ ] `cdktf diff lakehouse-prod` reviewed manually before approving.
- [ ] Rollback plan documented (typically: revert to previous `prod-*` tag and re-deploy).
- [ ] Customer-side change-management ticket exists (if customer is part of the org).

---

## 8. Secrets / variables in GitHub

| Type | Where | Purpose |
|---|---|---|
| `AWS_DEPLOY_ROLE_DEV` | repo secret | Per-env IAM role ARN (OIDC) |
| `AWS_DEPLOY_ROLE_QA` | repo secret | Per-env IAM role ARN (OIDC) |
| `AWS_DEPLOY_ROLE_PROD` | repo secret | Per-env IAM role ARN (OIDC) |
| `AWS_REGION` | env var (workflow `env:` block) | Single region per engagement |

**Never** store AWS access keys / secret keys as GitHub secrets. OIDC removes the need.

---

## 9. Reusable workflows (for multi-repo orgs)

If multiple projects share this pattern, lift the three workflows into a **`.github` repo** as reusable workflows (`workflow_call`). Per-project repos then declare:

```yaml
jobs:
  deploy:
    uses: northbaysolutions/.github/.github/workflows/cdktf-deploy.yml@v1
    with:
      env: dev
      stack-names: lakehouse-dev askbaba-delta-dev
    secrets:
      aws-deploy-role: ${{ secrets.AWS_DEPLOY_ROLE_DEV }}
```

This converges all NBS engagements onto identical pipeline behavior.

---

## 10. Pre-apply gates (checklist)

| Gate | Tool | Blocking? |
|---|---|---|
| Format | `ruff format --check` | Yes |
| Lint | `ruff check` | Yes |
| Type-check | `mypy --strict` | Yes |
| Unit tests | `pytest` | Yes |
| Synthesis | `cdktf synth` | Yes |
| Plan diff visible | `cdktf diff` | Yes (non-zero diff requires reviewer ack on PR) |
| Security static analysis | `tfsec` on synthesized JSON | Yes for High+ |
| Cost estimate | `infracost` against synthesized JSON | Warn-only PR comment |
| Secrets scan | `gitleaks` | Yes |

`tfsec` ignore rules live in `.tfsec/config.yml` and require a justification comment.

---

## 11. Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `Error: configured OIDC issuer …` at `configure-aws-credentials` | OIDC provider not yet created in the AWS account | Run `global/iam_roles/` bootstrap; verify `aws iam get-open-id-connect-provider` |
| `cdktf get` re-fetches every run (slow) | No cache | Add `actions/cache@v4` keyed on `cdktf.json` hash |
| Local `cdktf deploy` works but CI fails | Local uses CLI keys; CI uses OIDC role with fewer perms | Tighten/expand the deploy role policy in `global/iam_roles/` |
| `terraform_wrapper` swallows the exit code | GitHub's `setup-terraform` default | Set `terraform_wrapper: false` |
| Cross-tag promotion deploys wrong commit | `tag` push event uses the tag's commit, not main's HEAD | Verify the tag points at a commit already deployed to the previous env |
| `cdktf deploy` waits on TTY input | Missing `--auto-approve` | Add `--auto-approve` in CI; never use it locally for QA/Prod |

---

## 12. Composes with

- `IAC_CDKTF_PYTHON` — the construct + stack + state side of the same project.
- `LAYER_BACKEND_LAMBDA` — when the pipeline deploys Lambdas.
- `SERVERLESS_LAMBDA_POWERTOOLS` — when those Lambdas use Powertools (they should).
- The Glue artifact deployment is a separate job (or step in `deploy-dev.yml`) that builds the engine wheel and copies it to S3 BEFORE `cdktf deploy`. See `data/14` template for the engine; the wheel-build step is canonical:

  ```yaml
  - name: Build Glue engine wheel
    working-directory: src/glue
    run: uv build --wheel

  - name: Upload Glue artifact
    working-directory: src/glue
    run: |
      VERSION=$(uv run python -c "import tomllib; print(tomllib.loads(open('pyproject.toml','rb').read().decode())['project']['version'])")
      aws s3 cp dist/*.whl s3://${{ secrets.GLUE_ARTIFACTS_BUCKET }}/glue/$VERSION/
  ```

---

## 13. Acceptance criteria

A CI/CD setup built per this SOP passes ALL of:

1. PR triggers `lint-and-test.yml`; plan summary comment posted; merge blocked on red.
2. Push to `main` triggers `deploy-dev.yml`; Dev stack deploys without manual intervention.
3. Tag `qa-YYYY.WW` triggers QA deploy automatically.
4. Tag `prod-YYYY.WW` triggers `deploy-promote.yml`, waits at the GitHub `production` environment approval gate, deploys only after 2 approvers (one Tech Lead) accept.
5. No long-lived AWS keys in GitHub secrets.
6. `gitleaks` runs and is green.
7. `infracost` posts a cost estimate as a PR comment.
8. Rollback path: revert to previous `<env>-*` tag → `git tag -d` → re-tag previous commit → workflow re-runs. Documented in `infra/README.md`.
