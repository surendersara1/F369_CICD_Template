# SOP — CI/CD Pipeline (GitHub Actions + CDK + OIDC)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** GitHub Actions · AWS CDK v2 (Python) · OIDC-federated role assumption (no long-lived keys)

---

## 1. Purpose

Source-controlled delivery pipeline:

- Branch strategy: `main` → prod, `develop` → staging, PR → synth-only
- OIDC federation (no AWS access keys in GitHub)
- Lint + type-check + unit tests + cdk-nag → cdk diff → manual approval → deploy
- Per-environment accounts (poc, stage, prod)
- Optional: CDK Pipelines (self-mutating) for multi-account

This SOP covers the GitHub Actions path (primary) and CDK Pipelines (alternative). Not about stack-internal CI — that's the workloads' problem.

---

## 2. Decision — Monolith vs Micro-Stack

N/A for pipelines. They're configuration, not CDK constructs. The pipeline deploys EITHER variant. Two sections below:

- §3 — GitHub Actions workflow YAML (recommended)
- §4 — CDK Pipelines CDK code (alternative, self-mutating, multi-account-friendly)

---

## 3. GitHub Actions (recommended)

### 3.1 Repository setup

```
.github/
  workflows/
    ci.yml          # runs on every PR: lint, types, tests, cdk synth
    deploy-poc.yml  # runs on push to develop: deploy to poc account
    deploy-prod.yml # runs on push to main + manual approval: deploy to prod
```

### 3.2 `ci.yml`

```yaml
name: CI
on:
  pull_request:
    branches: [main, develop]
  push:
    branches: [main, develop]

permissions:
  contents: read
  id-token: write    # required for OIDC

jobs:
  lint-test-synth:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - uses: actions/setup-node@v4
        with: { node-version: '20' }

      - name: Install deps
        run: |
          pip install -r infrastructure/cdk/requirements.txt
          pip install -r requirements-dev.txt
          npm install -g aws-cdk

      - name: Lint
        run: |
          black --check infrastructure backend tests
          ruff check infrastructure backend tests

      - name: Types
        run: mypy infrastructure backend

      - name: Unit tests
        run: pytest tests/unit -v --cov=infrastructure --cov=backend --cov-report=term

      # Offline synth — no AWS credentials used
      - name: CDK synth (offline)
        env:
          CDK_DISABLE_VERSION_CHECK: "1"
          DEPLOY_ENV: poc
        run: |
          cd infrastructure/cdk
          cdk synth --all --no-lookups -q

      - name: cdk-nag
        run: |
          cd infrastructure/cdk
          cdk synth --all --no-lookups --validation
```

### 3.3 `deploy-poc.yml` — OIDC role assumption

```yaml
name: Deploy POC
on:
  push:
    branches: [develop]

permissions:
  contents: read
  id-token: write

env:
  AWS_REGION: us-east-1
  DEPLOY_ENV: poc

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: poc   # GitHub Environment with protection rules

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - uses: actions/setup-node@v4
        with: { node-version: '20' }

      # OIDC federation — no access keys required
      - name: Assume AWS role via OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{ secrets.POC_ACCOUNT_ID }}:role/github-actions-deploy
          role-session-name: gh-actions-${{ github.run_id }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Install deps
        run: |
          pip install -r infrastructure/cdk/requirements.txt
          npm install -g aws-cdk

      - name: Diff
        run: cd infrastructure/cdk && cdk diff --all

      - name: Deploy
        run: cd infrastructure/cdk && cdk deploy --all --require-approval never
```

### 3.4 OIDC trust role (one-time setup per account)

```yaml
# In a bootstrap CDK stack or Terraform, create:
# - OIDC provider for token.actions.githubusercontent.com
# - IAM role 'github-actions-deploy' with trust policy:

{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::ACCT:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike": { "token.actions.githubusercontent.com:sub": "repo:ORG/REPO:ref:refs/heads/develop" }
    }
  }]
}
```

Permissions: least-privilege CloudFormation + the minimum to bootstrap-and-deploy your CDK stacks.

### 3.5 Prod pipeline with approval gate

```yaml
jobs:
  approve:
    runs-on: ubuntu-latest
    environment: prod   # configure GitHub Environment with required reviewers
    steps:
      - run: echo "Approved"

  deploy:
    needs: approve
    runs-on: ubuntu-latest
    # ... same OIDC + deploy as POC but using PROD_ACCOUNT_ID
```

### 3.6 GitHub Actions gotchas

- **`id-token: write`** permission must be set at the job level for OIDC to work.
- **OIDC role trust** must scope `sub` to specific branch/PR or tags. Open-ended `*` lets any workflow in any repo assume it.
- **`cdk deploy --require-approval never`** disables the interactive prompt — safe only with pre-deploy diff review.
- **Environment protection rules** (GitHub) give per-env required reviewers without inventing a custom approval system.

---

## 4. CDK Pipelines (alternative, self-mutating)

```python
from aws_cdk import pipelines


class DeliveryPipelineStack(cdk.Stack):
    def __init__(self, scope, **kwargs):
        super().__init__(scope, "{project_name}-pipeline", **kwargs)

        pipeline = pipelines.CodePipeline(
            self, "Pipeline",
            pipeline_name="{project_name}",
            synth=pipelines.ShellStep(
                "Synth",
                input=pipelines.CodePipelineSource.connection(
                    "ORG/REPO", "main",
                    connection_arn="arn:aws:codeconnections:us-east-1:ACCT:connection/UUID",
                ),
                commands=[
                    "pip install -r infrastructure/cdk/requirements.txt",
                    "npm install -g aws-cdk",
                    "cd infrastructure/cdk && cdk synth --all --no-lookups",
                ],
                primary_output_directory="infrastructure/cdk/cdk.out",
            ),
            cross_account_keys=True,
        )

        # POC wave
        pipeline.add_stage(AppStage(self, "Poc",
            env=cdk.Environment(account="POC_ACCT", region="us-east-1"),
        ))

        # Prod wave with manual approval
        prod_stage = AppStage(self, "Prod",
            env=cdk.Environment(account="PROD_ACCT", region="us-east-1"),
        )
        pipeline.add_stage(prod_stage, pre=[pipelines.ManualApprovalStep("ProdApproval")])
```

### 4.1 CDK Pipelines gotchas

- **`cross_account_keys=True`** required for multi-account stages.
- **CodeStar connections** must be set up once manually (GitHub OAuth).
- **Self-mutation** means the pipeline updates itself before deploying apps. First run usually needs a manual `cdk deploy` to bootstrap.

---

## 5. Stage matrix

| Stage | Triggers | Account | Approval |
|---|---|---|---|
| PR | pull_request | none (synth only) | none |
| dev | push to `develop` | dev account | none |
| staging | push to `develop` + optional tag `staging-*` | stage account | required reviewer |
| prod | push to `main` | prod account | required reviewer(s) |

---

## 6. References

- `docs/Feature_Roadmap.md` — CI-00..CI-18
- Related SOPs: `LAYER_BACKEND_LAMBDA` (tested artifacts), `LAYER_OBSERVABILITY` (post-deploy alarms), `LAYER_SECURITY` (least-privilege pipeline role)

---

## 7. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | OIDC-first. GitHub Actions as primary path, CDK Pipelines as alternative. |
| 1.0 | 2026-03-05 | Initial. |
