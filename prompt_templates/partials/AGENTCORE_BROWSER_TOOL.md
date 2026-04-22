# SOP — Bedrock AgentCore Browser Tool (headless Chrome sandbox for agents)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `@aws-cdk/aws-bedrock-agentcore-alpha` (L2 alpha, Python `aws_cdk.aws_bedrock_agentcore_alpha`) · `aws-cdk-lib.aws_bedrockagentcore` (L1 `CfnBrowser`) · `bedrock-agentcore` SDK (`bedrock_agentcore.tools.browser_client.BrowserClient`) · boto3 control plane `bedrock-agentcore-control` + data plane `bedrock-agentcore` · Playwright ≥ 1.44 · Nova Act SDK

---

## 1. Purpose

- Provision and invoke the **AgentCore Browser Tool** — a fully managed, session-isolated headless Chrome/Chromium sandbox that agents drive via **Chrome DevTools Protocol (CDP)** (Playwright-compatible) and the **OS-level `InvokeBrowser` API** (for print dialogs, JS alerts, context menus, OS key events — things CDP cannot do).
- Codify the two-browser topology: the **system browser** (AWS-managed default, identifier `aws.browser.v1`) for most cases, and a **custom browser** provisioned via `create_browser` when the agent needs private-VPC egress, stricter recording retention, or a custom execution role.
- Codify the **session lifecycle** — session = microVM per agent invocation, billing by session-second, live view + replay artifacts to S3 — and the **safe-scraping guardrails** (robots.txt respect, domain allowlist, per-run page cap, login-wall TOS warning).
- Codify the **two-client split**: `bedrock-agentcore-control` for `create_browser` / `get_browser` / `list_browsers`; `bedrock-agentcore` (data plane) for `invoke_browser` / `start_browser_session` / `stop_browser_session`.
- Codify the **Strands `@tool` integration** — wrap browser session start + Playwright-over-CDP + OS-level `invoke_browser` fallbacks as a single agent-friendly tool that respects an allowlist + page-cap.
- Include when the SOW mentions: "web browsing agent", "web research", "deep research", "agentic scraping", "screenshot capture", "PDF extraction via browser", "captcha-walled site", "headless browser on AWS", "Playwright in an agent", "Nova Act".

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — one `cdk.Stack` owns the custom browser, the recording bucket, the browser execution role, and the agent/Lambda that calls `invoke_browser` | **§3 Monolith Variant** |
| `BrowserToolStack` owns the `Browser` + recording bucket + local CMK + execution role; `ComputeStack` / per-agent `AgentcoreRuntime` stack owns the consumer that starts sessions and invokes | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **Two distinct IAM surfaces.** The browser's own `executionRoleArn` (what the managed microVM assumes — needs write access to the recording bucket + any per-session VPC perms) is owned by the browser resource; the *consumer* role (what the agent/Lambda uses to call `StartBrowserSession` / `InvokeBrowser`) is a separate identity-side grant. Monolith hides this — micro-stack forces the clarity that both roles exist and have disjoint resource policies.
2. **Session replay bucket is the gravity well.** `recording.s3Location.bucket` is baked into the `Browser` resource at create time. Moving it later means replacing the browser (which invalidates the `browserIdentifier` clients have cached). Micro-stack owns bucket + browser in the same stack, identity grants to consumers via SSM.
3. **`executionRoleArn` requires `iam:PassRole`.** Whoever calls `create_browser` (CDK deploy role, or if you re-create at runtime, the agent role) needs `iam:PassRole` on the browser exec role ARN with a `iam:PassedToService` condition on `bedrock-agentcore.amazonaws.com`. Easy to miss across stacks.
4. **VPC wiring is asymmetric.** The `networkConfiguration.networkMode` on a custom browser controls whether the *browser microVM* has public egress or VPC-scoped egress — independent of the *consumer* Lambda/agent's network config. Both can mismatch silently.
5. **Dev/prod split on recording retention.** `dev` typically keeps 7-day S3 lifecycle for replays; `prod` may need 90 d + WORM / Object Lock for compliance. Baking both into the same bucket risks data-lifecycle bugs. Micro-stack scopes the bucket to `BrowserToolStack` where the lifecycle policy lives.

Micro-stack variant fixes all of this by: (a) owning the custom `Browser` + recording bucket + CMK + execution role in `BrowserToolStack`; (b) publishing `BrowserIdentifier`, `BrowserArn`, `RecordingBucketName`, and the exec-role ARN via SSM; (c) consumer agents grant themselves `bedrock-agentcore:StartBrowserSession` / `StopBrowserSession` / `UpdateBrowserStream` / `InvokeBrowser` on the specific browser ARN — identity-side only.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Agent / Lambda (consumer role)
      │  StartBrowserSession(browserIdentifier=..., sessionTimeoutSeconds=1800)
      ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Browser (custom, or system "aws.browser.v1")            │
  │    networkConfiguration: PUBLIC | VPC                    │
  │    executionRoleArn:       BrowserExecRole (S3 write)    │
  │    recording.enabled:      true                          │
  │    recording.s3Location:   s3://<bucket>/sessionreplay/  │
  │                                                          │
  │  Per-session microVM (session-isolated sandbox)          │
  │    headless Chrome/Chromium                              │
  │    CDP WebSocket (for Playwright / Nova Act)             │
  │    live-view endpoint (presigned)                        │
  └──────────────────────────────────────────────────────────┘
      ▲                               │
      │ CDP WebSocket                 │ InvokeBrowser (OS-level actions)
      │ Playwright / Nova Act         │ — print dialogs, JS alerts,
      │   page.goto, click, fill,     │   context menus, OS key events
      │   screenshot, pdf             │   that CDP does not expose
      │                               │
  Agent tool wrapper ─────────────────┘
      (enforces robots.txt, domain allowlist, page cap)
```

### 3.2 CDK — `_create_browser_tool()` method body (alpha L2 primary, L1 fallback)

```python
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
# Primary shape — alpha L2. Pin minor version (e.g. 2.160.0a0).
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Browser, BrowserNetworkConfiguration, BrowserRecordingConfig,
)
# Fallback shape — L1 (used inside conditional, shown in §3.2b).
from aws_cdk import aws_bedrockagentcore as agentcore_l1


def _create_browser_tool(self, stage: str) -> None:
    """Monolith variant — custom browser + recording bucket + exec role + CMK.

    Assumes self.{vpc, kms_key} exist. If you only need the AWS-managed
    system browser, skip this method entirely — just reference
    `aws.browser.v1` in the consumer code (see §3.4).
    """

    # A) Session-replay bucket. Dedicated bucket — never share with app data.
    self.browser_replay_bucket = s3.Bucket(
        self, "BrowserReplayBucket",
        bucket_name=f"{{project_name}}-browser-replay-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=False,                                  # replays are single-shot
        lifecycle_rules=[s3.LifecycleRule(
            id="ExpireReplays",
            enabled=True,
            expiration=Duration.days(7 if stage != "prod" else 90),
        )],
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
        auto_delete_objects=(stage != "prod"),
    )

    # B) Execution role the browser microVM assumes.
    browser_exec_role = iam.Role(
        self, "BrowserExecRole",
        role_name=f"{{project_name}}-browser-exec-{stage}",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        description="Assumed by the AgentCore-managed browser microVM for recording writes",
    )
    browser_exec_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "s3:PutObject",
            "s3:PutObjectAcl",            # replay artifact finalization
            "s3:AbortMultipartUpload",
        ],
        resources=[f"{self.browser_replay_bucket.bucket_arn}/sessionreplay/*"],
    ))
    browser_exec_role.add_to_policy(iam.PolicyStatement(
        actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
        resources=[self.kms_key.key_arn],
    ))

    # C) Custom browser — alpha L2.
    self.browser = Browser(
        self, "ResearchBrowser",
        browser_name=f"{{project_name}}_research_browser_{stage}",
        description="Research agent browser — headless Chrome, session-isolated",
        execution_role=browser_exec_role,
        network_configuration=BrowserNetworkConfiguration.using_public_network(),
        # For VPC egress instead:
        # network_configuration=BrowserNetworkConfiguration.using_vpc(
        #     self, vpc=self.vpc,
        #     vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        # ),
        recording=BrowserRecordingConfig(
            enabled=True,
            s3_bucket=self.browser_replay_bucket,
            s3_prefix="sessionreplay/",
        ),
    )
    # `connections` is exposed on the alpha L2 for VPC security-group rules —
    # e.g. self.browser.connections.allow_to(other, ec2.Port.tcp(443), "https egress").
    # TODO(verify): exact `connections` property surface on the alpha L2 —
    # the alpha API has been shifting release-to-release.

    # D) Expose the identifier + ARN for consumer code + SSM.
    #    `browser_identifier` is the short string consumers pass to
    #    StartBrowserSession; `browser_arn` is what IAM policies reference.
    self.browser_identifier = self.browser.browser_identifier
    self.browser_arn        = self.browser.browser_arn

    CfnOutput(self, "BrowserIdentifier", value=self.browser_identifier)
    CfnOutput(self, "BrowserArn",        value=self.browser_arn)
    CfnOutput(self, "BrowserReplayBucket", value=self.browser_replay_bucket.bucket_name)


def _grant_browser_invoke(self, consumer_role: iam.IRole) -> None:
    """Grant a consumer (Lambda exec role, AgentCore Runtime exec role)
    the right to start/stop/invoke sessions on THIS browser only.

    There is no L2 grant_invoke helper on the alpha construct (yet) — written
    as identity-side statements to match the template-wide pattern.
    """
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "bedrock-agentcore:StartBrowserSession",
            "bedrock-agentcore:StopBrowserSession",
            "bedrock-agentcore:UpdateBrowserStream",
            "bedrock-agentcore:InvokeBrowser",
        ],
        resources=[self.browser_arn],
    ))
    # Read the live-view / replay presigned artifacts.
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=["s3:GetObject"],
        resources=[f"{self.browser_replay_bucket.bucket_arn}/sessionreplay/*"],
    ))
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:DescribeKey"],
        resources=[self.kms_key.key_arn],
    ))
```

### 3.2b CDK — L1 fallback

Use the L1 shape if the alpha L2 API has drifted or you need properties the L2 has not surfaced yet.

```python
from aws_cdk import aws_bedrockagentcore as agentcore_l1
import uuid

browser_l1 = agentcore_l1.CfnBrowser(
    self, "ResearchBrowserL1",
    name=f"{{project_name}}_research_browser_{stage}",
    description="Research agent browser (L1)",
    execution_role_arn=browser_exec_role.role_arn,
    network_configuration={"networkMode": "PUBLIC"},         # or VPC config
    client_token=str(uuid.uuid4()),                          # idempotency
    recording={
        "enabled": True,
        "s3Location": {
            "bucket": self.browser_replay_bucket.bucket_name,
            "prefix": "sessionreplay/",
        },
    },
)
# L1 does NOT expose a `.connections` port-list helper — if VPC mode is used
# you need to manage ingress/egress SG rules on the consumer side manually.
```

### 3.3 Consumer handler — `lambda/browser_research/index.py`

```python
"""AgentCore Browser — CDP + Playwright + OS-level InvokeBrowser wrapper.

Flow:
  1. start_browser_session(browserIdentifier) -> {sessionId, cdpEndpointUrl, liveViewUrl}
  2. Connect Playwright via CDP WebSocket
  3. Drive pages with Playwright (page.goto, click, screenshot, pdf)
  4. For OS-level actions (print dialog, JS alert, context menu), call
     bedrock-agentcore:InvokeBrowser on the same sessionId
  5. stop_browser_session(sessionId)

Guardrails (agent-tool-level, NOT IAM):
  - robots.txt respect (urllib.robotparser)
  - domain allowlist (DOMAIN_ALLOWLIST env var, comma-sep)
  - MAX_PAGES_PER_SESSION cap
  - login-wall TOS warning in docstring — upstream prompt must refuse
    LinkedIn / Facebook / gated-SaaS scraping
"""
import json
import logging
import os
import urllib.robotparser
import uuid
from urllib.parse import urlparse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Data-plane client. Control-plane (create_browser) is NOT called at runtime
# in the monolith — the browser is provisioned by CDK.
bac = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))

BROWSER_IDENTIFIER   = os.environ["BROWSER_IDENTIFIER"]       # e.g. custom: "{project_name}_research_browser_dev"
SESSION_TIMEOUT_S    = int(os.environ.get("BROWSER_SESSION_TIMEOUT_S", "1800"))
MAX_PAGES            = int(os.environ.get("MAX_PAGES_PER_SESSION", "20"))
DOMAIN_ALLOWLIST     = [d.strip().lower() for d in os.environ.get("DOMAIN_ALLOWLIST", "").split(",") if d.strip()]
RESPECT_ROBOTS       = os.environ.get("RESPECT_ROBOTS_TXT", "true").lower() == "true"


def lambda_handler(event, _ctx):
    urls    = event["urls"]
    task    = event.get("task", "screenshot")

    _validate_urls(urls)

    sess = bac.start_browser_session(
        browserIdentifier=BROWSER_IDENTIFIER,
        name=f"research-{uuid.uuid4().hex[:8]}",
        sessionTimeoutSeconds=SESSION_TIMEOUT_S,
    )
    session_id       = sess["sessionId"]
    cdp_endpoint_url = sess.get("cdpEndpointUrl")    # WebSocket URL for Playwright
    live_view_url    = sess.get("liveViewUrl")       # presigned HTTPS for replay
    logger.info("started browser session id=%s cdp=%s", session_id, bool(cdp_endpoint_url))

    try:
        results = _drive_with_playwright(cdp_endpoint_url, urls[:MAX_PAGES], task)
    finally:
        # Always stop — leaking sessions bills per-second until the
        # sessionTimeoutSeconds elapses.
        bac.stop_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session_id,
        )
        logger.info("stopped browser session id=%s", session_id)

    return {
        "session_id":   session_id,
        "live_view":    live_view_url,
        "results":      results,
    }


def _validate_urls(urls: list[str]) -> None:
    for u in urls:
        parsed = urlparse(u)
        host   = (parsed.hostname or "").lower()

        if DOMAIN_ALLOWLIST and not any(host == d or host.endswith("." + d)
                                        for d in DOMAIN_ALLOWLIST):
            raise ValueError(f"Domain {host} not in allowlist {DOMAIN_ALLOWLIST}")

        if RESPECT_ROBOTS:
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.set_url(robots_url)
                rp.read()
                if not rp.can_fetch("*", u):
                    raise PermissionError(f"robots.txt disallows {u}")
            except PermissionError:
                raise
            except Exception as e:
                # Robots fetch failure is soft-fail; log + proceed.
                logger.warning("robots fetch failed for %s: %s", host, e)


def _drive_with_playwright(cdp_url: str, urls: list[str], task: str) -> list[dict]:
    """Connect Playwright over CDP WebSocket, drive pages, return results.

    Playwright's `chromium.connect_over_cdp(cdp_url)` is the canonical
    entrypoint. The default context is created per session by AgentCore.
    """
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        ctx     = browser.contexts[0] if browser.contexts else browser.new_context()
        for url in urls:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
                if task == "screenshot":
                    png = page.screenshot(full_page=True)
                    results.append({"url": url, "bytes_png": len(png)})
                elif task == "pdf":
                    pdf = page.pdf()
                    results.append({"url": url, "bytes_pdf": len(pdf)})
                elif task == "text":
                    results.append({"url": url, "text": page.inner_text("body")[:5000]})
                else:
                    results.append({"url": url, "title": page.title()})
            finally:
                page.close()
        browser.close()
    return results
```

### 3.4 System browser — no CDK resource needed

```python
# No control-plane create_browser call. Consumers pass the system identifier
# directly to StartBrowserSession. Use this when you do NOT need custom
# network mode, custom recording config, or a custom execution role.
BROWSER_IDENTIFIER = "aws.browser.v1"   # TODO(verify): system browser identifier exact string
# Follows the `aws.codeinterpreter.v1` convention. AWS tutorials reference
# this pattern but the exact string should be confirmed against the
# BrowserTool dev guide before shipping. See gotcha in §3.6.
```

### 3.5 OS-level `invoke_browser` — actions CDP cannot do

```python
# Some actions are OUTSIDE the DOM — print dialogs, OS-native JS alerts,
# context menus, function keys, IME input. These are invoked via the
# data-plane bedrock-agentcore:InvokeBrowser action on the same sessionId.
bac.invoke_browser(
    browserIdentifier=BROWSER_IDENTIFIER,
    sessionId=session_id,
    action={
        "type": "keyboard",
        "keys": ["PrintScreen"],
    },
)
# TODO(verify): exact `action` payload schema (type names + fields) —
# consult the bedrock-agentcore InvokeBrowser API reference for the
# current enum values.
```

### 3.6 Monolith gotchas

- **`aws.browser.v1` identifier string is the canonical guess.** Pattern matches `aws.codeinterpreter.v1` (verified from AWS tutorial). `# TODO(verify): system browser identifier exact string` — if the docs show a different string (e.g. `aws.browser.chromium.v1`), swap this env-var default and update §3.4 before shipping.
- **Session leaks are silent and expensive.** `start_browser_session` without a matching `stop_browser_session` bills per-second until `sessionTimeoutSeconds` elapses. Always put `stop_browser_session` in a `finally` block. Consider a Step Functions heartbeat if you hand the session ID across process boundaries.
- **CDP endpoint URL is short-lived.** The `cdpEndpointUrl` returned by `StartBrowserSession` is valid for the session lifetime only — don't cache it across invocations, don't stash it in a database. Same for `liveViewUrl`.
- **Playwright version must match the Chromium in the browser.** Playwright pins a specific Chromium build per release; the AgentCore-managed browser runs its own version. Mismatch shows up as "protocol not supported" errors on advanced APIs. `# TODO(verify): confirm which Playwright minor range AWS recommends for the current AgentCore Chromium build.`
- **`robots.txt` respect is at the agent-tool layer, not IAM.** There is no IAM action that blocks fetching a disallowed URL. The wrapper in §3.3 enforces it. If you skip it, the agent will happily fetch `/admin/` on sites that disallow crawlers.
- **Login-walled sites (LinkedIn, Facebook, gated SaaS) routinely forbid automation in their ToS.** Tooling can reach those pages but doing so exposes the client to legal risk. The upstream agent prompt and the Cedar policy engine should refuse those domains — codify this in `AGENTCORE_AGENT_CONTROL` policies.
- **Recording bucket retention mismatch.** Dev vs prod lifecycle rules differ; dropping `prod` below 30 d can violate compliance. Bake the retention days into the stack prop, not a magic number inside the method.
- **Control vs data plane clients are different.** `boto3.client("bedrock-agentcore-control")` is for `create_browser` / `get_browser` / `list_browsers`. `boto3.client("bedrock-agentcore")` is for `start_browser_session` / `invoke_browser`. Mixing them returns `UnknownOperationError` not an obvious `AccessDenied`.
- **`iam:PassRole` on the browser exec role ARN.** The deploy role needs `iam:PassRole` with condition `iam:PassedToService = bedrock-agentcore.amazonaws.com`. Default pipeline roles rarely include this — add it to the CDK deploy role's policy or the alpha construct's synthesized permission.
- **`InvokeBrowser` action schema is evolving.** `# TODO(verify): the `action` payload shape (`type`, `keys`, `coordinates`, `dialog_action` etc.) — consult the `bedrock-agentcore:InvokeBrowser` API reference monthly; it's changed between alpha and GA.

---

## 4. Micro-Stack Variant

**Use when:** `BrowserToolStack` owns the custom `Browser` + recording bucket + CMK + browser execution role; consumer Lambdas / AgentCore Runtimes in `ComputeStack` / `AgentcoreRuntimeStack` call `StartBrowserSession` on it.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA §4.1`)

1. **Anchor Lambda `code=from_asset(...)` to `Path(__file__)`** — `_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"`.
2. **Never call `browser.grant_invoke(other_role)` across stacks.** Publish `browser_arn` via SSM; consumer grants itself `bedrock-agentcore:InvokeBrowser` etc. identity-side on the ARN token.
3. **Never share the KMS CMK across stacks via object reference.** Own the CMK in `BrowserToolStack`; publish `key_arn` via SSM; consumer grants itself `kms:Decrypt` identity-side.
4. **`iam:PassRole` with `iam:PassedToService` condition** on whichever role passes the browser exec role (the CDK deploy role, for `create_browser`).
5. **PermissionsBoundary on every role** — browser exec role + every consumer role.

### 4.2 Dedicated `BrowserToolStack`

```python
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws, CfnOutput, Duration, RemovalPolicy, Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from aws_cdk.aws_bedrock_agentcore_alpha import (
    Browser, BrowserNetworkConfiguration, BrowserRecordingConfig,
)
from constructs import Construct

# stacks/browser_tool_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class BrowserToolStack(cdk.Stack):
    """Owns the AgentCore custom Browser, session-replay bucket, CMK, and
    browser execution role. Publishes identifier + ARN + bucket via SSM so
    downstream compute stacks can grant themselves Invoke rights without
    cross-stack construct imports.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        permission_boundary: iam.IManagedPolicy,
        vpc: ec2.IVpc | None = None,
        replay_retention_days: int | None = None,
        network_mode: str = "PUBLIC",                      # or "VPC"
        **kwargs,
    ) -> None:
        super().__init__(
            scope, f"{{project_name}}-browser-tool-{stage_name}", **kwargs,
        )
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # A) Local CMK — never imported from SecurityStack (non-negotiable #3).
        cmk = kms.Key(
            self, "BrowserKey",
            alias=f"alias/{{project_name}}-browser-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
        )

        # B) Replay bucket.
        retention = replay_retention_days or (90 if stage_name == "prod" else 7)
        replay_bucket = s3.Bucket(
            self, "ReplayBucket",
            bucket_name=f"{{project_name}}-browser-replay-{stage_name}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(
                id="ExpireReplays", enabled=True,
                expiration=Duration.days(retention),
            )],
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
            auto_delete_objects=(stage_name != "prod"),
        )

        # C) Browser execution role — assumed by the managed browser microVM.
        browser_exec_role = iam.Role(
            self, "BrowserExecRole",
            role_name=f"{{project_name}}-browser-exec-{stage_name}",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        browser_exec_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:PutObjectAcl", "s3:AbortMultipartUpload"],
            resources=[f"{replay_bucket.bucket_arn}/sessionreplay/*"],
        ))
        browser_exec_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
            resources=[cmk.key_arn],
        ))
        iam.PermissionsBoundary.of(browser_exec_role).apply(permission_boundary)

        # D) Custom browser.
        if network_mode == "VPC":
            if vpc is None:
                raise ValueError("vpc required when network_mode='VPC'")
            net = BrowserNetworkConfiguration.using_vpc(
                self, vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                ),
            )
        else:
            net = BrowserNetworkConfiguration.using_public_network()

        browser = Browser(
            self, "ResearchBrowser",
            browser_name=f"{{project_name}}_research_browser_{stage_name}",
            description=f"Research agent browser {stage_name}",
            execution_role=browser_exec_role,
            network_configuration=net,
            recording=BrowserRecordingConfig(
                enabled=True,
                s3_bucket=replay_bucket,
                s3_prefix="sessionreplay/",
            ),
        )

        # E) Publish for consumer stacks.
        ssm.StringParameter(
            self, "BrowserIdentifierParam",
            parameter_name=f"/{{project_name}}/{stage_name}/browser/identifier",
            string_value=browser.browser_identifier,
        )
        ssm.StringParameter(
            self, "BrowserArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/browser/arn",
            string_value=browser.browser_arn,
        )
        ssm.StringParameter(
            self, "BrowserReplayBucketParam",
            parameter_name=f"/{{project_name}}/{stage_name}/browser/replay_bucket",
            string_value=replay_bucket.bucket_name,
        )
        ssm.StringParameter(
            self, "BrowserKmsArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/browser/kms_arn",
            string_value=cmk.key_arn,
        )

        self.browser             = browser
        self.browser_arn         = browser.browser_arn
        self.browser_identifier  = browser.browser_identifier
        self.replay_bucket       = replay_bucket
        self.cmk                 = cmk
        self.permission_boundary = permission_boundary

        CfnOutput(self, "BrowserIdentifier", value=browser.browser_identifier)
        CfnOutput(self, "BrowserArn",        value=browser.browser_arn)
```

### 4.3 Consumer pattern — identity-side grants in `ComputeStack`

```python
# Inside ComputeStack — no Browser / replay-bucket construct references.
from aws_cdk import aws_ssm as ssm, aws_iam as iam, aws_lambda as _lambda

browser_identifier = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/browser/identifier"
)
browser_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/browser/arn"
)
replay_bucket_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/browser/replay_bucket"
)
browser_kms_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/browser/kms_arn"
)

research_fn = _lambda.Function(
    self, "BrowserResearchFn",
    # ... standard config ...
    environment={
        "BROWSER_IDENTIFIER":          browser_identifier,
        "BROWSER_SESSION_TIMEOUT_S":   "1800",
        "MAX_PAGES_PER_SESSION":       "20",
        "DOMAIN_ALLOWLIST":            "arxiv.org,example.com",
        "RESPECT_ROBOTS_TXT":          "true",
    },
)

research_fn.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:UpdateBrowserStream",
        "bedrock-agentcore:InvokeBrowser",
    ],
    resources=[browser_arn],
))
research_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["s3:GetObject"],
    # Build the replay-prefix resource pattern explicitly since we only have
    # the bucket NAME (not ARN) from SSM:
    resources=[
        f"arn:aws:s3:::{replay_bucket_name}/sessionreplay/*",
    ],
))
research_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kms:Decrypt", "kms:DescribeKey"],
    resources=[browser_kms_arn],
))

iam.PermissionsBoundary.of(research_fn.role).apply(self.permission_boundary)
```

### 4.4 Strands `@tool` — canonical wrapper (see also `STRANDS_TOOLS §3.5`)

```python
"""Browser Tool wrapped as a Strands @tool.

Enforces the three agent-tool-level guardrails (robots.txt, domain allowlist,
page cap) BEFORE touching AgentCore. The execution-role grants from §4.3 are
the IAM side of the contract; the @tool is the code-side contract.
"""
import json, os, uuid
import boto3
from strands import tool

_bac = boto3.client("bedrock-agentcore")
BROWSER_IDENTIFIER = os.environ["BROWSER_IDENTIFIER"]


@tool
def browse_url_and_extract(urls: list[str], task: str = "text") -> str:
    """Browse URLs in an AgentCore headless browser and extract content.

    Respects robots.txt and an env-var domain allowlist. Will NOT operate on
    login-walled sites (LinkedIn, Facebook, gated SaaS) — those violate the
    sites' ToS and must be refused upstream.

    Args:
        urls: Up to MAX_PAGES_PER_SESSION URLs to visit.
        task: One of "text", "screenshot", "pdf", "title".
    Returns:
        JSON with per-URL results + the live-view URL for replay.
    """
    try:
        # Reuse the validated lambda handler from §3.3 — import
        # _validate_urls + _drive_with_playwright from the shared module.
        from handlers.browser_research import (
            _validate_urls, _drive_with_playwright,
        )
        _validate_urls(urls)

        sess = _bac.start_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            name=f"strands-{uuid.uuid4().hex[:8]}",
            sessionTimeoutSeconds=int(os.environ.get("BROWSER_SESSION_TIMEOUT_S", "1800")),
        )
        session_id       = sess["sessionId"]
        cdp_endpoint_url = sess.get("cdpEndpointUrl")
        live_view_url    = sess.get("liveViewUrl")

        try:
            results = _drive_with_playwright(
                cdp_endpoint_url,
                urls[: int(os.environ.get("MAX_PAGES_PER_SESSION", "20"))],
                task,
            )
        finally:
            _bac.stop_browser_session(
                browserIdentifier=BROWSER_IDENTIFIER,
                sessionId=session_id,
            )

        return json.dumps({
            "session_id":   session_id,
            "live_view":    live_view_url,
            "results":      results,
        })
    except Exception as e:
        # Rule 3 — tools MUST NOT raise out of the agent loop.
        return json.dumps({"error": str(e), "success": False})
```

### 4.5 Nova Act integration

```python
# Nova Act is AWS's browser-specialized model. It consumes the same CDP
# endpoint the Playwright wrapper uses but applies a higher-level action
# vocabulary ("click the primary CTA", "fill the form with X").
#
# The integration surface is the NovaAct SDK's `act` client constructed
# against the browser session's CDP URL. Provide the same
# StartBrowserSession output as the endpoint.
# TODO(verify): exact Nova Act SDK package name + import path + the
# constructor argument that accepts a CDP URL — consult
# `browser-quickstart-nova-act.html` before shipping.
```

### 4.6 Micro-stack gotchas

- **`value_for_string_parameter` returns a token.** Use it directly in `resources=[...]` and `environment={...}`. Do not `.split(":")` or string-concat against it in Python — CloudFormation resolves the `{{resolve:ssm:...}}` at deploy time.
- **Replay-bucket ARN is not published; only the bucket NAME.** This is deliberate — consumers build the prefix-scoped ARN with an f-string. Safer than exposing the bucket ARN and risking `resources=["*"]` accidents.
- **Cross-stack deletion order**: if `BrowserToolStack` is deleted while `ComputeStack` still references `BrowserArn` via SSM, the SSM param disappears and the consumer stack updates fail on next deploy. Deploy order: `BrowserToolStack` first; delete order: `ComputeStack` first.
- **Playwright dependency weight on Lambda**: Playwright + Chromium driver does not fit in a vanilla Lambda zip (250 MB uncompressed cap). Use a Lambda container image (10 GB ECR cap) or — preferred — put the consumer in AgentCore Runtime where Playwright lives in the container image and the browser itself is remote.
- **`network_mode="VPC"` requires the `Browser` L2 to own a security group on the microVM side.** The consumer's VPC security groups are separate — the two connect only over the AWS-managed CDP endpoint, which is a service URL, not a VPC-internal address. Don't try to peer them.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / single-agent demo using only public web pages | §3 Monolith + system browser `aws.browser.v1`; skip custom `Browser` entirely |
| Production with session-replay audit requirement | §4 Micro-Stack, custom `Browser` with `recording.enabled=true`, bucket in `BrowserToolStack` with lifecycle policy |
| Need private-VPC targets (internal wikis, auth-walled internal apps) | `BrowserNetworkConfiguration.using_vpc(...)` + `PRIVATE_WITH_EGRESS` subnets; consumer Lambda in the same VPC |
| OS-level keyboard / dialog automation (print to PDF via Ctrl-P, accept OS alerts) | Use `bedrock-agentcore:InvokeBrowser` with keyboard/dialog actions on the same `sessionId` — CDP cannot drive these |
| Using Nova Act instead of Playwright | Keep the `Browser` resource; swap the `_drive_with_playwright` call with the Nova Act SDK's `act()` loop against the CDP URL |
| Parallel agent sessions (N agents → N browsers) | Same `Browser` resource — sessions are independent microVMs. Billing is per session-second; concurrency is capped by the regional service quota. `# TODO(verify): current per-account concurrent browser session quota.` |
| Compliance requires WORM replays | Add Object Lock to the replay bucket (bucket must be created with `object_lock_enabled_for_bucket=True`); governance-mode retention via bucket default + per-object override |
| Short-lived ad-hoc analyses, no replay needed | `recording.enabled=false`, skip the replay bucket entirely — simpler + cheaper |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_AGENTCORE_BROWSER_TOOL.py`. Offline; no AWS calls.

```python
"""SOP verification — BrowserToolStack synth contains the expected
resources and the browser + replay bucket + exec role are wired correctly."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_browser_tool_stack_synthesizes_public_mode():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.browser_tool_stack import BrowserToolStack
    stack = BrowserToolStack(
        app, stage_name="dev",
        permission_boundary=boundary,
        env=env,
    )
    t = Template.from_stack(stack)

    # 1 browser + 1 replay bucket + 1 CMK + 1 exec role + 4 SSM params
    t.resource_count_is("AWS::BedrockAgentCore::Browser", 1)
    t.resource_count_is("AWS::S3::Bucket", 1)
    t.resource_count_is("AWS::KMS::Key", 1)
    t.resource_count_is("AWS::SSM::Parameter", 4)

    # Recording enabled + SSE-KMS on the bucket
    t.has_resource_properties("AWS::S3::Bucket", Match.object_like({
        "BucketEncryption": {
            "ServerSideEncryptionConfiguration": Match.array_with([
                Match.object_like({
                    "ServerSideEncryptionByDefault": {
                        "SSEAlgorithm":   "aws:kms",
                        "KMSMasterKeyID": Match.any_value(),
                    },
                }),
            ]),
        },
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls":       True,
            "BlockPublicPolicy":     True,
            "IgnorePublicAcls":      True,
            "RestrictPublicBuckets": True,
        },
    }))

    # Browser has an exec role + recording config
    t.has_resource_properties("AWS::BedrockAgentCore::Browser", Match.object_like({
        "ExecutionRoleArn":        Match.any_value(),
        "NetworkConfiguration":    Match.any_value(),
        "Recording":               Match.object_like({"Enabled": True}),
    }))


def test_consumer_has_identity_side_grants_only():
    """No cross-stack resource policy — the consumer's IAM statement
    should target the browser ARN via SSM token."""
    app = cdk.App()
    env = _env()

    from infrastructure.cdk.stacks.compute_stack import ComputeStack
    stack = ComputeStack(app, stage_name="dev", env=env)
    t = Template.from_stack(stack)

    # Any Lambda role in the compute stack should have InvokeBrowser +
    # StartBrowserSession + StopBrowserSession actions.
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": {
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": Match.array_with([
                        "bedrock-agentcore:StartBrowserSession",
                        "bedrock-agentcore:InvokeBrowser",
                    ]),
                }),
            ]),
        },
    }))
```

---

## 7. References

- `docs/template_params.md` — `BROWSER_IDENTIFIER_SSM_NAME`, `BROWSER_ARN_SSM_NAME`, `BROWSER_SESSION_TIMEOUT_S`, `MAX_PAGES_PER_SESSION`, `DOMAIN_ALLOWLIST`, `RESPECT_ROBOTS_TXT`, `BROWSER_NETWORK_MODE` (`PUBLIC` | `VPC`), `BROWSER_REPLAY_RETENTION_DAYS`
- `docs/Feature_Roadmap.md` — feature IDs `AC-BRW-01` (custom browser resource), `AC-BRW-02` (replay bucket + lifecycle), `AC-BRW-03` (consumer Lambda with Playwright), `AC-BRW-04` (Strands @tool wrapper), `AC-BRW-05` (Nova Act integration)
- AWS docs:
  - [Browser Tool overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-using-tool.html)
  - [Create a custom browser](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-create.html)
  - [Browser quickstart](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart.html)
  - [Browser quickstart — Nova Act](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart-nova-act.html)
  - [Building agents with the browser tool](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-building-agents.html)
  - [`InvokeBrowser` API reference](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeBrowser.html)
  - [boto3 `bedrock-agentcore-control.create_browser`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_browser.html)
  - [boto3 `bedrock-agentcore.invoke_browser`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_browser.html)
  - [Introducing the AgentCore Browser Tool (AWS blog)](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-agentcore-browser-tool/)
  - [`@aws-cdk/aws-bedrock-agentcore-alpha` L2 alpha](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrock_agentcore_alpha/README.html)
  - [`aws-cdk-lib.aws_bedrockagentcore` L1](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html)
  - [`bedrock-agentcore` SDK for Python](https://github.com/aws/bedrock-agentcore-sdk-python)
  - [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- Related SOPs:
  - `AGENTCORE_RUNTIME` — consumer pattern when the browser is driven from an agent runtime (not a Lambda)
  - `AGENTCORE_CODE_INTERPRETER` — paired sandbox tool; same control/data-plane split
  - `AGENTCORE_AGENT_CONTROL` — Cedar policies that refuse login-walled domains + tighten the DOMAIN_ALLOWLIST at run time
  - `STRANDS_TOOLS` — `@tool` wrapping conventions, six design rules, presigned-URL idiom
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers
  - `LAYER_SECURITY` — KMS CMK policy patterns, S3 Object Lock for WORM replays

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — AgentCore Browser Tool GA deep-dive. Dual-variant monolith (§3) + micro-stack `BrowserToolStack` (§4). Alpha L2 `Browser` construct as primary shape, L1 `CfnBrowser` fallback, custom browser vs system `aws.browser.v1`, two-client split (`bedrock-agentcore-control` for create, `bedrock-agentcore` for invoke), Playwright-over-CDP consumer handler, OS-level `invoke_browser` for print-dialog / JS-alert actions, Nova Act integration pointer, session-replay S3 bucket with dev/prod lifecycle, `robots.txt` + domain-allowlist + `MAX_PAGES_PER_SESSION` agent-tool guardrails. Created to fill gap surfaced by Deep Research Agent kit design, grounded in AgentCore GA documentation. |
