# SOP — Agentic Refactor in CI/CD (Q Developer + CodeBuild + CodePipeline · automated codemods · review gates · canary merges)

**Version:** 2.0 · **Last-reviewed:** 2026-04-28 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Q Developer agentic commands in pipelines · CodeBuild + CodePipeline integration · Automated PR generation · Review gates (CODEOWNERS + required reviewers) · Canary merges + automated rollback · Refactor budget tracking

---

## 1. Purpose

- Codify the **agentic refactor in CI/CD pattern** — Q Developer (or comparable AI) generates refactor PRs at scale, validates via CI/CD, gates with review, merges if all checks pass.
- Codify **automated codemods** — sweep N files for the same pattern; e.g., upgrade Lambda Powertools v2 → v3 across 50 services.
- Codify **review gates** — CODEOWNERS, required reviewers, branch protection rules.
- Codify **canary merges** — merge a small batch first; wait + observe; merge rest if green.
- Codify the **refactor budget** — set scope upfront; never let agentic refactors drift unbounded.
- Codify the **rollback strategy** — Git revert + post-merge issue tracker.
- Pairs with `AI_DEV_Q_DEVELOPER` (base) + `AI_DEV_Q_TRANSFORMATIONS` (Java/.NET upgrade) + CICD partials.

When the SOW signals: "automate refactors", "scale codemods", "Q Developer in CI", "agentic PR generation", "modernization at scale".

---

## 2. Decision tree — agentic refactor pattern

```
Refactor scope?
├── 1-5 files manual edit                       → human + Q Developer chat in IDE
├── 10-50 files same-pattern codemod             → §3 Single-shot codemod pipeline
├── 100+ files cross-cutting refactor             → §4 Batched canary pipeline
├── Whole codebase version upgrade (Java 8→17)   → §AI_DEV_Q_TRANSFORMATIONS
└── Architecture-level refactor (monolith decomp) → §MIGRATION_HUB_STRATEGY (Refactor Spaces)

Review intensity?
├── Low risk (formatting, import sort)           → 1 reviewer + automated checks
├── Medium (API rename, dep upgrade)              → 2 reviewers + integration tests
├── High (security, billing, auth)                 → senior reviewers + manual validation
└── Critical (cryptographic, financial calc)       → block agentic; human-only

CI gate strictness?
├── All tests pass                                ✅ minimum
├── No coverage regression                        ✅ recommended
├── No performance regression (load test)         ✅ for hot paths
└── No security finding regression (Inspector)    ✅ for prod
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single codemod across ~30 files | **§3 Single-shot** |
| Production — batched canary refactor (100s of files) | **§4 Canary** |

---

## 3. Single-Shot Codemod Pipeline

### 3.1 Architecture

```
   Trigger (manual or scheduled)
        │
        ▼
   ┌───────────────────────────────────────────────┐
   │ CodeBuild project: agentic-refactor-runner    │
   │   1. Clone repo                                │
   │   2. Generate refactor plan via Q Developer    │
   │      OR custom codemod tool (jscodeshift, etc.)│
   │   3. Apply changes to feature branch           │
   │   4. Run unit + integration tests              │
   │   5. If green, push branch + open PR           │
   │   6. Tag PR with auto-refactor label           │
   │   7. Tag CODEOWNERS for review                 │
   └───────────────────────────────────────────────┘
        │
        ▼
   ┌───────────────────────────────────────────────┐
   │ GitHub PR (auto-opened)                        │
   │   - Q-generated commit message + change rationale│
   │   - CI runs full test suite                      │
   │   - CODEOWNERS reviewed                          │
   │   - Required: 2 approvals                        │
   │   - Required: all checks passing                  │
   └────────────────────────┬───────────────────────┘
                            │
                            ▼ (on merge)
   ┌───────────────────────────────────────────────┐
   │ Production CI/CD pipeline                      │
   │   - Stage deploy + smoke                        │
   │   - Auto-rollback on alarm                      │
   │   - Prod deploy with blue/green                  │
   └───────────────────────────────────────────────┘
```

### 3.2 CDK — refactor pipeline

```python
# stacks/refactor_pipeline_stack.py
from aws_cdk import Stack, Duration
from aws_cdk import aws_codebuild as cb
from aws_cdk import aws_codepipeline as pipeline
from aws_cdk import aws_codepipeline_actions as actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct


class RefactorPipelineStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str,
                 github_secret_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. CodeBuild project that runs the refactor ──────────────
        refactor_role = iam.Role(self, "RefactorRole",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
        )
        refactor_role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[github_secret_arn],
        ))
        refactor_role.add_to_policy(iam.PolicyStatement(
            actions=["q:StartTransformation", "q:GetTransformation",
                     "q:CreateUploadUrl", "q:StopTransformation"],
            resources=["*"],
        ))

        refactor_build = cb.Project(self, "RefactorBuild",
            project_name=f"{env_name}-agentic-refactor",
            source=cb.Source.git_hub(
                owner="acme",
                repo="myapp",
                webhook=False,
            ),
            environment=cb.BuildEnvironment(
                build_image=cb.LinuxBuildImage.STANDARD_7_0,
                privileged=False,
                compute_type=cb.ComputeType.MEDIUM,
            ),
            environment_variables={
                "GITHUB_TOKEN": cb.BuildEnvironmentVariable(
                    type=cb.BuildEnvironmentVariableType.SECRETS_MANAGER,
                    value=f"{github_secret_arn}:token",
                ),
                "REFACTOR_BRANCH": cb.BuildEnvironmentVariable(value="auto-refactor/${BUILD_NUMBER}"),
            },
            build_spec=cb.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "install": {
                        "runtime-versions": {"python": "3.12", "java": "corretto17"},
                        "commands": [
                            "pip install -U boto3 awscli",
                            "curl -O https://q-cli.amazonaws.com/install.sh && bash install.sh",
                        ],
                    },
                    "pre_build": {
                        "commands": [
                            "git config user.email 'q-refactor@acme.com'",
                            "git config user.name 'Q Refactor Bot'",
                            "git checkout -b $REFACTOR_BRANCH",
                        ],
                    },
                    "build": {
                        "commands": [
                            # Run codemod (Q Developer agentic OR custom tool)
                            "q chat \"Update all uses of LambdaContext.method to LambdaContext.attribute (Powertools v3 migration). Apply to all Python files in src/.\" --auto-apply",
                            # Or use jscodeshift / OpenRewrite for deterministic transforms:
                            # "npx jscodeshift -t codemods/v3-migration.js src/",
                            # Validate
                            "mvn -B clean test || pytest",
                            "git add -A && git commit -m 'Q Refactor: Powertools v2 → v3 migration'",
                        ],
                    },
                    "post_build": {
                        "commands": [
                            "git push -u origin $REFACTOR_BRANCH",
                            # Open PR via GitHub CLI
                            "gh pr create --title 'Q Refactor: Powertools v2 → v3' --body \"$(cat .refactor-rationale.md)\" --label auto-refactor --reviewer @engineering-team",
                        ],
                    },
                },
            }),
            role=refactor_role,
            timeout=Duration.hours(2),
        )
```

### 3.3 GitHub PR template

```markdown
## Q Developer Agentic Refactor

**Type**: Powertools v2 → v3 migration  
**Run ID**: BUILD_NUMBER  
**Files affected**: 47  
**Auto-applied changes**: 213  
**Manual review needed**: 4 (flagged in code comments)

### Rationale
Lambda Powertools v3 has breaking changes in `LambdaContext` API:
- `LambdaContext.get_method()` → `LambdaContext.method` (attribute access)
- Deprecated `idempotent_decorator` → use `@idempotent`

### Changes
- ✅ 47 files updated to v3 API
- ✅ 213 method calls migrated
- ✅ All tests pass locally
- ⚠️ 4 files flagged for manual review (see comments)

### Validation checklist
- [x] Build passes
- [x] Unit tests pass
- [x] Integration tests pass
- [ ] Senior engineer review (CODEOWNERS)
- [ ] Manual flagged sections reviewed
- [ ] Security review (if API changes)

### Rollback
If issues found post-merge: `git revert <merge-sha>`. Auto-revert workflow available via `gh pr revert`.

🤖 Generated by Q Developer agentic refactor pipeline
```

---

## 4. Canary Refactor Pipeline (100s of files)

### 4.1 Strategy: split + canary + observe + roll forward

```
Plan for 500-file refactor:
├── Batch 1: 50 files (canary, low-risk modules)
│     ├── Apply refactor + test
│     ├── Open PR
│     ├── Review + merge
│     ├── Stage deploy
│     ├── 24h soak: monitor errors, latency, cost
│     └── If OK → proceed; if not → revert + investigate
├── Batch 2: 100 files (next-risk tier)
│     └── Same flow
├── Batch 3: 350 files (remainder)
│     └── Same flow
└── Total: ~1 week per 500-file refactor
```

### 4.2 Refactor budget tracking

```python
# Track across batches
refactor_budget = {
    "type": "powertools_v3_migration",
    "total_files": 500,
    "completed_batches": 0,
    "remaining_files": 500,
    "deadline": "2026-05-15",
    "start_date": "2026-04-28",
    "blocking_issues": [],
    "rollbacks": [],
}

# Stored in DDB; updated after each batch
# Dashboard: Cloudwatch metric `RefactorProgress` per type
```

### 4.3 Rollback runbook

```bash
# Rollback procedure if production issue surfaces post-merge
# 1. Identify the merge SHA
git log --oneline | grep "Q Refactor"

# 2. Auto-revert via GitHub
gh pr create --base main \
  --title "Revert: Q Refactor batch 2 (issue #XXX)" \
  --body "Reverts merge SHA abc123. See incident #XXX." \
  --label auto-revert

# 3. CI validates revert + auto-merges
# 4. Stage + prod deploy
# 5. Post-incident review:
#    - Why did test suite miss this?
#    - Add test case for this scenario
#    - Adjust agentic prompt to avoid similar issue
```

---

## 5. Common gotchas

- **Refactor budget creep** — without scope discipline, agentic refactors expand to "let me also fix this small thing." Lock to predefined diff.
- **Test suite blind spots** — agentic refactors pass tests but introduce subtle bugs in untested paths. Always check coverage on changed files.
- **Hidden API changes** — agentic tool updates a method but misses one call site. Always grep for old API post-refactor.
- **Concurrent agentic refactors** — multiple branches with overlapping changes = merge hell. Serialize batches.
- **PR review fatigue** — flooding reviewers with auto-PRs leads to rubber-stamp reviews. Cap N auto-PRs/week per repo.
- **Static analysis baseline drift** — new idioms introduced by agent may trigger linter/SonarQube warnings. Re-baseline post-refactor.
- **Performance regression** — minor refactors (e.g., `for` → `stream`) can change perf 5-15%. Load-test hot paths.
- **Security regression** — auth/crypto code touched by agent → security review mandatory.
- **Sample size** — first batch should be 5-10% of total to catch issues early.
- **Cost** — Q Developer Pro + CodeBuild minutes + reviewer time. Estimate 2-5× the manual time for the first refactor; subsequent refactors 50-70% the manual time.
- **Cultural buy-in** — auto-PRs feel "imposed." Run pilot + share metrics with team. Don't force.
- **Audit trail** — keep `auto-refactor` + `auto-revert` labels searchable; aggregate metrics quarterly.
- **License/IP** — agent suggestions may reference public OSS code. Configure code-reference filtering in Q Developer admin.

---

## 6. Pytest worked example

```python
# tests/test_agentic_refactor.py
import boto3, pytest, subprocess


def test_refactor_pipeline_completes(refactor_pipeline_name):
    """Trigger pipeline; wait for success."""
    cb = boto3.client("codebuild")
    build = cb.start_build(projectName=refactor_pipeline_name)["build"]
    build_id = build["id"]
    
    # Wait
    import time
    while True:
        status = cb.batch_get_builds(ids=[build_id])["builds"][0]["buildStatus"]
        if status in ["SUCCEEDED", "FAILED", "TIMED_OUT", "STOPPED"]:
            break
        time.sleep(30)
    
    assert status == "SUCCEEDED"


def test_pr_opened_after_refactor():
    """After successful refactor, a PR with auto-refactor label exists."""
    result = subprocess.run(["gh", "pr", "list", "--label", "auto-refactor",
                              "--state", "open", "--limit", "5", "--json", "number"],
                              capture_output=True, text=True)
    import json
    prs = json.loads(result.stdout)
    assert prs


def test_refactor_passes_ci():
    """Latest auto-refactor PR has all checks passing."""
    result = subprocess.run(["gh", "pr", "view", "--json", "statusCheckRollup", "--label",
                              "auto-refactor"], capture_output=True, text=True)
    import json
    pr = json.loads(result.stdout)
    rollup = pr["statusCheckRollup"]
    failed = [c for c in rollup if c["state"] == "FAILURE"]
    assert not failed, f"Failed checks: {failed}"


def test_no_recent_auto_revert():
    """No auto-revert PRs in last 7 days = no recent regressions."""
    result = subprocess.run(["gh", "pr", "list", "--label", "auto-revert",
                              "--state", "merged", "--search", "merged:>=2026-04-21",
                              "--json", "number"], capture_output=True, text=True)
    import json
    reverts = json.loads(result.stdout)
    assert not reverts, f"Recent auto-reverts: {reverts}"
```

---

## 7. Five non-negotiables

1. **Auto-PRs require ≥ 2 reviewer approvals + all CI checks** — never bypass.
2. **Canary first batch ≤ 10% of total** — catch issues before bulk merge.
3. **Refactor budget defined upfront** — locked scope, deadline, blocking criteria.
4. **24h+ soak in stage** between batches — monitor errors, latency.
5. **Auto-revert workflow ready** — rollback should take < 30 min when needed.

---

## 8. References

- [Q Developer + CodeBuild integration](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/cli-codebuild.html)
- [Codemods + jscodeshift](https://github.com/facebook/jscodeshift)
- [OpenRewrite (Java codemod toolkit)](https://docs.openrewrite.org/)
- [GitHub branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [`AI_DEV_Q_DEVELOPER` partial](AI_DEV_Q_DEVELOPER.md)
- [`AI_DEV_Q_TRANSFORMATIONS` partial](AI_DEV_Q_TRANSFORMATIONS.md)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-28 | Initial. Q Developer + CodeBuild + auto-PR + canary batched refactor + review gates + auto-revert + refactor budget. Wave 18. |
