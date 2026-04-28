# SOP — Amazon Q Developer (IDE assistant · CLI · agentic features · subscription tiers · Pro setup · usage governance)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Amazon Q Developer (rebranded CodeWhisperer, GA Apr 2024) · Q Developer Pro / Free tiers · IDE plugins (VSCode, JetBrains, Visual Studio, Cloud9, SageMaker Studio) · Q Developer CLI · Agentic capabilities (/dev, /test, /review, /transform) · Customizations (your-codebase-aware completions) · Inline chat + chat panel · Multi-line code suggestions

---

## 1. Purpose

- Codify **Amazon Q Developer** as the AI-augmented engineering productivity tool. Pairs with developer IDEs to provide multi-line code suggestions, in-IDE chat, agentic feature dev, automated testing, code review, and transformation.
- Codify the **subscription tiers** — Free (limited) vs Pro ($19/user/mo).
- Codify **agentic commands** — `/dev` for feature implementation, `/test` for test generation, `/review` for code review, `/transform` for codebase modernization.
- Codify **Customizations** (Pro-only) — your private codebase indexed → Q suggests using your conventions, internal libraries, naming patterns.
- Codify **CLI** — Q for terminal (translate natural language to bash/git/docker, explain errors, suggest fixes).
- Codify **AppRoles + IAM Identity Center governance** — manage who has access, what features.
- Codify **usage telemetry + cost controls** — track per-user adoption + ROI.
- Pairs with `AI_DEV_Q_TRANSFORMATIONS` (legacy modernization) and `AI_DEV_AGENTIC_REFACTOR` (CI/CD integration).

When the SOW signals: "AI coding assistant", "developer productivity", "Q Developer rollout", "GitHub Copilot alternative on AWS", "code modernization with AI".

---

## 2. Decision tree — Q Developer vs alternatives

| Need | Q Developer Pro | GitHub Copilot | Cursor / Claude Code |
|---|:---:|:---:|:---:|
| AWS-deep awareness (CDK, SDK suggestions) | ✅ best | ⚠️ generic | ⚠️ generic |
| Customizations (your codebase) | ✅ Pro | ⚠️ Copilot Enterprise | ⚠️ |
| Code transformations (Java upgrade, .NET) | ✅ /transform | ❌ | ⚠️ via prompts |
| Inline + chat | ✅ | ✅ | ✅ |
| CLI integration | ✅ | ❌ | ✅ |
| Agentic /dev mode | ✅ | ⚠️ Workspace | ✅ |
| Pricing | $19/user/mo | $19/user/mo (Business) | varies |
| Free tier | ✅ Free with limits | ❌ | varies |
| AWS Q Business integration | ✅ same identity | ❌ | ❌ |

**Recommendation:**
- **Q Developer** for AWS-first orgs (best AWS API completions + CDK + service-aware).
- **GitHub Copilot** if org is heavily Azure / Microsoft.
- **Multi-tool stacks** are common — Q Developer for AWS work + Copilot for general OSS.

```
Q Developer feature surface:

  IDE plugin (VSCode / JetBrains / VS / Cloud9 / SM Studio)
      │
      ├── Inline completions (multi-line, context-aware)
      ├── Chat panel
      │     - Ask: "How do I add an SQS DLQ to this Lambda?"
      │     - /dev: "Add user-soft-delete to the User model"
      │     - /test: "Write unit tests for this function"
      │     - /review: "Review changes to this file"
      │     - /transform: (project-level Java 8 → 17 / .NET → modern)
      ├── Customizations (Pro)
      │     - Index private codebase / docs (S3-staged)
      │     - Suggestions match team conventions + use internal libs
      └── Code references (cite source if completion borrows from training)

  CLI (q chat / q translate)
      │
      ├── q translate "find all log files older than 7 days and gzip them"
      │     → returns: find /var/log -mtime +7 -name '*.log' -exec gzip {} \;
      ├── q chat (conversational)
      └── q dashboard (usage stats)

  Subscription
      │
      ├── Free: limited completions/mo + chat + basic /dev
      └── Pro $19/user/mo: unlimited + Customizations + /transform +
                              priority + larger context
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — Free tier for 5-10 devs | **§3 Free Pilot** |
| Production — Pro for 50+ devs + Customizations + governance | **§4 Pro Rollout** |

---

## 3. Free Pilot (5-10 dev pilot)

### 3.1 Setup

```bash
# Each developer:
# 1. Install Q Developer extension in IDE
#    - VSCode: search "Amazon Q" → install
#    - JetBrains: Settings → Plugins → Marketplace → "Amazon Q"
#    - VS 2022: Extensions → Manage Extensions → search "Amazon Q"
# 2. Sign in with Builder ID (free, personal)
#    OR with IAM Identity Center if org has one
# 3. Start using:
#    - Type code → see inline ghost text
#    - Open chat panel: Cmd/Ctrl + I (VSCode)
```

### 3.2 First-week outcomes to measure

```python
# Track adoption + ROI:
# 1. Daily active users (DAU)
# 2. Suggestions accepted/total ratio (target ≥ 30%)
# 3. Time-to-first-commit on new task (compare with baseline)
# 4. Number of /dev, /test, /review invocations per dev
# 5. Self-reported satisfaction (5-pt scale)
```

---

## 4. Pro Rollout — 50+ dev org

### 4.1 IAM Identity Center setup (single source of truth)

```python
# stacks/q_developer_idc_stack.py
from aws_cdk import Stack
from aws_cdk import aws_q as q                            # Q service (API still evolving; use SDK)
from aws_cdk import aws_sso as sso
from constructs import Construct


class QDeveloperIdcStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 idc_instance_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Q Developer Pro subscription is configured at the AWS Q service level
        # Currently via console: Q Developer console → Subscriptions → Add users/groups
        # CDK support is evolving; use AwsCustomResource for now if needed.

        # Create IDC group for Q Developer Pro users
        # (sso.CfnAssignment to grant access)
        # Or sync from Azure AD group "DeveloperTeam-Engineers"

        # Subscribe the group via AWS console or boto3:
        # boto3.client("qbusiness").create_subscription(
        #     applicationId=app_id,
        #     principal={"group": "<idc-group-id>"},
        #     type="Q_DEVELOPER_PRO",
        # )
```

### 4.2 Customizations setup (Pro feature)

Customizations index your private code base to give context-aware completions matching your conventions.

```bash
# 1. Stage source code in S3 (read-only access for Q service)
aws s3 sync ./monorepo s3://q-customizations-source/myapp/main \
  --exclude ".git/*" --exclude "node_modules/*" --exclude "venv/*"

# 2. Console: Q Developer → Customizations → Create
#    - Source: S3 bucket
#    - Repositories included
#    - File extensions to index (.py, .ts, .java, etc.)
#    - Encryption: customer KMS

# 3. Indexing takes 1-4 hours for ~100K LOC

# 4. Activate the customization for users / groups
#    via Q Developer console → Customizations → Activate
```

```python
# CDK approximation — use CfnApplication + custom resource
# (full Customization API in flux as of Apr 2026)

# Permission for Q service to read S3
import boto3
boto3.client("qbusiness").create_data_accessor(
    applicationId=app_id,
    name="customizations-source",
    principal=q_service_principal,
    actionConfigurations=[{...}],
    # ... s3 ARN, KMS, sources ...
)
```

### 4.3 Governance — track + control

```python
# CloudWatch metrics (auto-emitted by Q Developer)
# - Q.SuggestionsCount
# - Q.SuggestionsAcceptedCount
# - Q.ChatInteractionsCount
# - Q.TransformInvocationsCount

# Custom dashboard
from aws_cdk import aws_cloudwatch as cw

cw.Dashboard(self, "QDeveloperDashboard",
    dashboard_name=f"{env_name}-q-developer",
    widgets=[
        [cw.GraphWidget(
            title="Daily active users",
            left=[cw.Metric(
                namespace="AWS/Q",
                metric_name="DailyActiveUsers",
                statistic="Maximum", period=Duration.days(1),
            )],
        )],
        [cw.GraphWidget(
            title="Suggestion acceptance rate",
            left=[cw.MathExpression(
                expression="(accepted / total) * 100",
                using_metrics={
                    "accepted": cw.Metric(namespace="AWS/Q",
                                            metric_name="SuggestionsAcceptedCount"),
                    "total": cw.Metric(namespace="AWS/Q",
                                         metric_name="SuggestionsCount"),
                },
            )],
        )],
        [cw.GraphWidget(
            title="Agentic command usage",
            left=[
                cw.Metric(namespace="AWS/Q", metric_name="DevCommandsCount"),
                cw.Metric(namespace="AWS/Q", metric_name="TestCommandsCount"),
                cw.Metric(namespace="AWS/Q", metric_name="ReviewCommandsCount"),
                cw.Metric(namespace="AWS/Q", metric_name="TransformCommandsCount"),
            ],
        )],
    ],
)
```

### 4.4 Per-team usage policies

```python
# Permissions can be tag-based at IDC group level:
# - "Q-Pro-Engineering" group → full Pro features
# - "Q-Pro-DataScience" group → Pro + customizations on data repo
# - "Q-Free-Sandbox" group → Free tier only

# Optional content controls (preview):
# - Block suggestions matching public open-source (license risk)
# - Block specific languages / repos
# - Anonymous suggestions (no learning from your code)

# These are configured in Q Developer admin console.
```

---

## 5. Agentic command usage patterns

### 5.1 `/dev` — implement a feature

In IDE chat panel:
```
/dev Add a soft-delete field to the User model. Update CRUD endpoints to:
  - GET /users (filter out deleted by default; ?include_deleted=true to include)
  - DELETE /users/:id (sets deleted_at instead of removing row)
  - PUT /users/:id/restore (clear deleted_at)
Update tests accordingly.
```

Q Developer:
1. Reads relevant files (User model, routes, tests)
2. Drafts a change set (multi-file)
3. Shows diffs for review
4. On accept → applies changes
5. (Pro) suggests git commit message

### 5.2 `/test` — generate unit tests

```python
# Select function in IDE; open chat:
/test Generate pytest unit tests for this function. Cover happy path,
empty input, invalid input (raises ValueError), and edge case (n > 1000).
```

### 5.3 `/review` — review pull request

```bash
# In CLI:
q chat "Review the changes in this PR — branch feature/payment-flow"

# Or via GitHub Action (sample):
# - on: pull_request
# - run: q review --pr ${{ github.event.pull_request.number }} --comment
```

### 5.4 `/transform` — codebase upgrade (covered in `AI_DEV_Q_TRANSFORMATIONS`)

---

## 6. Common gotchas

- **Q Developer Pro vs Free**:
  - Free: limited monthly suggestions; no Customizations; no /transform; no priority access.
  - Pro: $19/user/mo; unlimited inline + chat; Customizations; /transform; priority queue.
- **MAU billing surprise** — Pro at $19 × 100 devs = $1900/mo. Run cost projection.
- **Customizations indexing time** — 1-4 hours for typical codebase. Re-index on schedule (weekly) for fresh suggestions.
- **Customizations + private code** — Q does NOT use your code to train models. Anonymous suggestions OFF by default. Review privacy tier.
- **License compliance** — Free + Pro both flag suggestions matching public OSS with `cite source` reference. Always review citations.
- **Code references vs originality** — Q tries hard to be original; ~5-10% of suggestions cite. For strict legal posture, enable "Block suggestions with code references" in admin.
- **CLI requires AWS credentials** — set up via `aws configure sso` for IDC users.
- **Customizations are per-account** — for multi-account orgs, replicate per account or use organization-shared (preview).
- **IDE plugin telemetry** opt-out is per-user setting — admins can't fully disable telemetry but can disable feature opt-ins (e.g., feedback prompts).
- **Q Developer in SageMaker Studio** — built-in (no plugin needed). Available since Apr 2024.
- **Q Developer for Azure / Bitbucket / GitLab** — works in IDEs accessing those repos. Q itself is AWS-bound but doesn't require AWS-hosted code.
- **Browser extension** — for chat-only use (no inline). Useful for non-engineering users using Q via web.
- **Onboarding velocity** — first week: install + sign in. Week 2-3: organic adoption. Week 4+: peak. Plan training accordingly.
- **Adoption baseline** — typical: 30-50% suggestions acceptance after 4 weeks. Below 20% = training/customization issue.

---

## 7. Pytest worked example

```python
# tests/test_q_developer.py
import boto3, pytest

# (Q Developer admin APIs evolving; sample structure)
q_client = boto3.client("qbusiness")


def test_idc_subscription_active(app_id, idc_group_id):
    # Verify Q Developer subscription assigned to dev group
    subs = q_client.list_subscriptions(applicationId=app_id)["subscriptions"]
    matching = [s for s in subs if s.get("principal", {}).get("group") == idc_group_id]
    assert matching, f"No Q Developer Pro subscription for group {idc_group_id}"
    assert matching[0]["type"] == "Q_DEVELOPER_PRO"


def test_customizations_active(customization_id):
    cust = q_client.get_customization(customizationId=customization_id)
    assert cust["status"] == "ACTIVE"
    assert cust.get("lastUpdated"), "Customization never updated"


def test_active_user_count_tracked():
    """Daily active users metric should be present."""
    cw = boto3.client("cloudwatch")
    metrics = cw.list_metrics(Namespace="AWS/Q",
                                MetricName="DailyActiveUsers")["Metrics"]
    assert metrics


def test_acceptance_rate_above_threshold():
    """After 30 days, suggestion acceptance rate should be ≥ 25%."""
    cw = boto3.client("cloudwatch")
    accepted = cw.get_metric_statistics(
        Namespace="AWS/Q", MetricName="SuggestionsAcceptedCount",
        Statistics=["Sum"], Period=86400 * 30,
        StartTime=..., EndTime=...,
    )
    total = cw.get_metric_statistics(
        Namespace="AWS/Q", MetricName="SuggestionsCount",
        Statistics=["Sum"], Period=86400 * 30,
        StartTime=..., EndTime=...,
    )
    if total["Datapoints"]:
        rate = accepted["Datapoints"][0]["Sum"] / total["Datapoints"][0]["Sum"]
        assert rate >= 0.25, f"Acceptance rate {rate:.1%} below 25% target"
```

---

## 8. Five non-negotiables

1. **IAM Identity Center as identity** — never personal Builder IDs in production rollouts.
2. **Pro tier for serious dev orgs** — Free tier limits hit fast at scale.
3. **Customizations for any org with > 50 devs** — context-aware suggestions worth far more than per-user cost.
4. **Block suggestions with code references** in admin if legal posture requires originality.
5. **Track DAU + acceptance rate** in CW dashboard — without metrics, no ROI proof.

---

## 9. References

- [Q Developer User Guide](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/what-is.html)
- [Q Developer Pro features](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/q-pro.html)
- [Customizations](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/customizations.html)
- [/dev, /test, /review agentic commands](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/agentic-commands.html)
- [Q Developer CLI](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line.html)
- [IDE plugins](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/installing.html)
- [Q Developer pricing](https://aws.amazon.com/q/developer/pricing/)

---

## 10. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Q Developer Pro/Free + IDE plugins + CLI + agentic commands (/dev /test /review /transform) + Customizations + IDC governance + adoption telemetry. Wave 18. |
