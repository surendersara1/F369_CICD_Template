# SOP — Bedrock AgentCore Code Interpreter (sandboxed Python / JS / TS execution for agents)

**Version:** 2.0 · **Last-reviewed:** 2026-04-22 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · `@aws-cdk/aws-bedrock-agentcore-alpha` (L2 alpha, Python `aws_cdk.aws_bedrock_agentcore_alpha`) · `aws-cdk-lib.aws_bedrockagentcore` (L1 `CfnCodeInterpreter`) · `bedrock-agentcore` SDK (`bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter`) · boto3 control plane `bedrock-agentcore-control` + data plane `bedrock-agentcore` · Strands Agents ≥ 1.34

---

## 1. Purpose

- Provision and invoke the **AgentCore Code Interpreter** — a fully managed, session-isolated sandbox for running **Python / JavaScript / TypeScript** on behalf of an agent, with sandbox file I/O round-tripped through S3 and chart / binary outputs served as presigned URLs.
- Codify the two-interpreter topology: the **system interpreter** (AWS-managed default, identifier `aws.codeinterpreter.v1` — verified from AWS tutorial) for most cases; and a **custom interpreter** provisioned via `create_code_interpreter` when private-VPC egress or custom network mode is required.
- Codify the **session lifecycle** — `start_code_interpreter_session` → `invoke_code_interpreter(name="executeCode" | "executeCommand" | "readFiles" | "writeFiles" | "listFiles" | "removeFiles" | "startCommandExecution" | "getTask" | "stopTask")` → `stop_code_interpreter_session` — and the **streaming response** shape (`for event in resp["stream"]: event["result"]["content"]`).
- Codify the **two-client split**: `bedrock-agentcore-control` for `create_code_interpreter` / `get_code_interpreter` / `list_code_interpreters`; `bedrock-agentcore` (data plane) for `start_code_interpreter_session` / `invoke_code_interpreter` / `stop_code_interpreter_session`.
- Codify the **Strands `@tool` integration** — a single wrapper that starts + invokes + stops on each call, uploads input files from S3, executes code, downloads output files back to S3, and returns presigned chart URLs to the agent. Reference the existing shim in `STRANDS_TOOLS §3.3` and go deeper here.
- Include when the SOW mentions: "code execution agent", "data analysis agent", "chart generation", "Python sandbox", "Jupyter-in-an-agent", "sandboxed numeric compute", "Deep Research with code", "pandas on demand", "matplotlib chart reply".

---

## 2. Decision — Monolith vs Micro-Stack

| You are… | Use variant |
|---|---|
| POC — single `cdk.Stack` owns the files bucket, consumer Lambda / agent, and (optionally) a custom CI; most POCs use the system `aws.codeinterpreter.v1` with no CI resource | **§3 Monolith Variant** |
| `CodeInterpreterStack` owns the custom `CodeInterpreter` + files bucket + local CMK + exec role; `ComputeStack` / per-agent `AgentcoreRuntime` stack owns the consumer | **§4 Micro-Stack Variant** |

**Why the split matters.**

1. **For the system interpreter (`aws.codeinterpreter.v1`) no CI resource is needed.** The interpreter exists service-side. All you own is the S3 files bucket + the consumer role's identity-side grants. Most Deep-Research-style kits land here — there is no micro-stack case until you need custom network mode.
2. **Files bucket = the gravity well.** S3 is the only durable side of the session — every chart, every output file, every input CSV moves through it. Sharing the bucket with app data conflates lifecycle policies (output files want 7-day expiry; app data wants months). Micro-stack owns the bucket.
3. **Custom interpreter exec role.** Only relevant for custom CIs. The role is assumed by the managed microVM — needs identity-side perms to read any S3 objects the user pre-loaded (if the container itself does the fetch) or to write audit logs. Custom CIs also need `iam:PassRole` on the deploy role.
4. **Streaming response must not cross stack boundaries.** `invoke_code_interpreter` returns an event-stream; consumers must exhaust it in the same process. Don't try to return the `stream` object across a Lambda boundary — it is a boto3 generator, not a pickled value.
5. **`sessionTimeoutSeconds` is billed to the session owner.** Leaked sessions accrue cost until the timeout elapses. `try/finally` is a non-negotiable pattern — micro-stack hardens this in the `@tool` wrapper.

Micro-stack variant fixes all of this by: (a) owning the custom `CodeInterpreter` (or none, if system) + files bucket + CMK in `CodeInterpreterStack`; (b) publishing `CodeInterpreterIdentifier`, `FilesBucketName`, `CodeInterpreterArn` (for custom CIs), and `KmsArn` via SSM; (c) consumer agents grant themselves `bedrock-agentcore:StartCodeInterpreterSession` / `StopCodeInterpreterSession` / `InvokeCodeInterpreter` on the specific CI ARN — identity-side only.

---

## 3. Monolith Variant

### 3.1 Architecture

```
  Agent / Lambda (consumer role)
      │  start_code_interpreter_session(
      │      codeInterpreterIdentifier="aws.codeinterpreter.v1" (system)
      │                               | "{project_name}_ci_dev" (custom),
      │      sessionTimeoutSeconds=900)
      ▼
  ┌──────────────────────────────────────────────────────────┐
  │  CodeInterpreter (system or custom)                      │
  │    Per-session microVM (session-isolated sandbox)        │
  │    Runtime: Python / JavaScript / TypeScript             │
  │    Writable sandbox FS (cleared on session stop)         │
  │    Libraries: numpy, pandas, matplotlib, scipy, seaborn, │
  │               node 20, tsx (TS via tsx)                  │
  │                                                          │
  │  Invocations (data plane) — name =                       │
  │    executeCode        (python/js/ts code)                │
  │    executeCommand     (shell command)                    │
  │    readFiles          (fetch from sandbox)               │
  │    writeFiles         (upload to sandbox)                │
  │    listFiles                                             │
  │    removeFiles                                           │
  │    startCommandExecution / getTask / stopTask            │
  └──────────────────────────────────────────────────────────┘
      ▲                               │
      │ upload inputs (S3 -> sandbox) │ output charts / files (sandbox -> S3)
      │                               ▼
  Files bucket: s3://{project_name}-ci-files-{stage}/
      inputs/<session>/<file>         outputs/<session>/<file>
      (presigned URLs returned to agent for display)
```

### 3.2 CDK — `_create_code_interpreter()` method body (alpha L2 primary, L1 fallback)

```python
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
# Primary shape — alpha L2.
from aws_cdk.aws_bedrock_agentcore_alpha import (
    CodeInterpreter, CodeInterpreterNetworkMode,
)
# Fallback shape — L1.
from aws_cdk import aws_bedrockagentcore as agentcore_l1


def _create_code_interpreter(self, stage: str) -> None:
    """Monolith variant — files bucket + (optional) custom CI + exec role.

    If you only need the AWS-managed system interpreter, the CodeInterpreter
    resource is NOT needed — consumers pass `aws.codeinterpreter.v1` directly.
    Create the files bucket anyway; the consumer uses it for I/O.
    """

    # A) Files bucket — inputs/ and outputs/ prefixes.
    self.ci_files_bucket = s3.Bucket(
        self, "CiFilesBucket",
        bucket_name=f"{{project_name}}-ci-files-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=False,
        lifecycle_rules=[s3.LifecycleRule(
            id="ExpireCiFiles",
            enabled=True,
            expiration=Duration.days(7 if stage != "prod" else 30),
        )],
        removal_policy=(
            RemovalPolicy.RETAIN if stage == "prod" else RemovalPolicy.DESTROY
        ),
        auto_delete_objects=(stage != "prod"),
    )

    # B) Optional — CUSTOM CI (skip this block for the system interpreter).
    use_custom_ci = False
    if use_custom_ci:
        ci_exec_role = iam.Role(
            self, "CiExecRole",
            role_name=f"{{project_name}}-ci-exec-{stage}",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        # Custom CIs that need to reach back into the account (for audit,
        # additional S3 paths, etc.) get their grants here. The system CI
        # runs in an AWS-owned account and does NOT assume any role of yours.

        self.code_interpreter = CodeInterpreter(
            self, "ResearchCi",
            code_interpreter_name=f"{{project_name}}_ci_{stage}",
            description="Research agent code interpreter — Python / JS / TS",
            execution_role=ci_exec_role,
            network_mode=CodeInterpreterNetworkMode.using_public_network(),
            # VPC alternative:
            # network_mode=CodeInterpreterNetworkMode.using_vpc(
            #     self, vpc=self.vpc,
            #     vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            # ),
        )
        # TODO(verify): exact property name / helper signature for
        # `CodeInterpreterNetworkMode` on the alpha L2 — API has shifted
        # release-to-release; the helper is either `usingPublicNetwork()` /
        # `usingVpc(...)` or a `network_configuration=...` prop.

        self.ci_identifier = self.code_interpreter.code_interpreter_identifier
        self.ci_arn        = self.code_interpreter.code_interpreter_arn
    else:
        # System interpreter — no resource to create.
        self.ci_identifier = "aws.codeinterpreter.v1"
        self.ci_arn        = (
            f"arn:aws:bedrock-agentcore:{Stack.of(self).region}:aws:code-interpreter/"
            f"aws.codeinterpreter.v1"
        )
        # TODO(verify): the exact ARN format for the SYSTEM code
        # interpreter — `arn:aws:bedrock-agentcore:<region>:aws:...` is the
        # canonical AWS-owned shape, but consult the AgentCore devguide
        # before relying on it in an IAM resource restriction. If the ARN
        # cannot be scoped, fall back to `resources=["*"]` on
        # bedrock-agentcore:StartCodeInterpreterSession / InvokeCodeInterpreter
        # and lean on the `codeInterpreterIdentifier` parameter for scoping.

    CfnOutput(self, "CiIdentifier", value=self.ci_identifier)
    CfnOutput(self, "CiFilesBucket", value=self.ci_files_bucket.bucket_name)


def _grant_code_interpreter(self, consumer_role: iam.IRole) -> None:
    """Grant a consumer role to start / invoke / stop CI sessions + read/write
    the files bucket. Hand-written — the alpha L2 does not expose a L2 grant."""
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "bedrock-agentcore:StartCodeInterpreterSession",
            "bedrock-agentcore:StopCodeInterpreterSession",
            "bedrock-agentcore:InvokeCodeInterpreter",
        ],
        resources=[self.ci_arn],   # may be "*" for system CI if ARN not scoped
    ))
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources=[f"{self.ci_files_bucket.bucket_arn}/*"],
    ))
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=["s3:ListBucket"],
        resources=[self.ci_files_bucket.bucket_arn],
    ))
    consumer_role.add_to_policy(iam.PolicyStatement(
        actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[self.kms_key.key_arn],
    ))
```

### 3.2b CDK — L1 fallback (custom CI only)

```python
import uuid

ci_l1 = agentcore_l1.CfnCodeInterpreter(
    self, "ResearchCiL1",
    name=f"{{project_name}}_ci_{stage}",
    description="Research CI (L1)",
    execution_role_arn=ci_exec_role.role_arn,
    network_configuration={"networkMode": "PUBLIC"},     # or VPC config
    client_token=str(uuid.uuid4()),
)
# TODO(verify): L1 property names — `name` / `description` / `executionRoleArn`
# / `networkConfiguration` follow the create_code_interpreter shape; confirm
# against CloudFormation reference for `AWS::BedrockAgentCore::CodeInterpreter`.
```

### 3.3 Consumer handler — `lambda/ci_analysis/index.py`

```python
"""AgentCore Code Interpreter — session lifecycle + file round-trip.

Flow:
  1. Upload input files from caller's S3 prefix -> sandbox via writeFiles
  2. Execute the analysis code via executeCode (streaming response)
  3. Download output files from sandbox -> S3 outputs prefix via readFiles
  4. Stop the session
  5. Return presigned URLs for output files + stdout/stderr

Event shape:
{
  "code":     "import pandas as pd; df = pd.read_csv('input.csv'); ...",
  "language": "python",
  "input_files": [
    {"s3_key": "inputs/sess-123/data.csv", "sandbox_name": "input.csv"}
  ],
  "output_filenames": ["report.pdf", "chart.png"],
  "session_name": "sess-123"
}
"""
import json
import logging
import os
import time
import uuid

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

bac = boto3.client(
    "bedrock-agentcore",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    config=Config(retries={"max_attempts": 3, "mode": "standard"}),
)
s3 = boto3.client("s3")

CI_IDENTIFIER    = os.environ["CI_IDENTIFIER"]          # "aws.codeinterpreter.v1" or custom
FILES_BUCKET     = os.environ["CI_FILES_BUCKET"]
SESSION_TIMEOUT  = int(os.environ.get("CI_SESSION_TIMEOUT_S", "900"))   # 15 min default
PRESIGN_TTL_S    = int(os.environ.get("CI_PRESIGN_TTL_S", "3600"))


def lambda_handler(event, _ctx):
    code            = event["code"]
    language        = event.get("language", "python")           # python|javascript|typescript
    input_files     = event.get("input_files", []) or []
    output_names    = event.get("output_filenames", []) or []
    session_name    = event.get("session_name") or f"sess-{uuid.uuid4().hex[:8]}"

    sess = bac.start_code_interpreter_session(
        codeInterpreterIdentifier=CI_IDENTIFIER,
        name=session_name,
        sessionTimeoutSeconds=SESSION_TIMEOUT,
    )
    session_id = sess["sessionId"]
    logger.info("started CI session id=%s identifier=%s", session_id, CI_IDENTIFIER)

    try:
        # 1) Upload inputs S3 -> sandbox.
        for f in input_files:
            content = s3.get_object(Bucket=FILES_BUCKET, Key=f["s3_key"])["Body"].read()
            _invoke(bac, session_id, "writeFiles", {
                "content": [
                    {
                        "path":         f["sandbox_name"],
                        "text":         None,
                        "blob":         content,   # raw bytes accepted for binary
                    },
                ],
            })
            # TODO(verify): exact writeFiles payload shape — whether text +
            # blob are separate keys or a single `data` field with base64.
            # Consult invoke_code_interpreter API reference examples.

        # 2) Execute.
        exec_resp = _invoke(bac, session_id, "executeCode", {
            "language": language,
            "code":     code,
        })
        stdout_text, stderr_text = _collect_stdio(exec_resp)

        # 3) Download outputs sandbox -> S3.
        out_urls = []
        for name in output_names:
            read_resp = _invoke(bac, session_id, "readFiles", {
                "paths": [name],
            })
            content = _extract_file_bytes(read_resp, name)
            if content is None:
                continue
            key = f"outputs/{session_id}/{name}"
            s3.put_object(Bucket=FILES_BUCKET, Key=key, Body=content)
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": FILES_BUCKET, "Key": key},
                ExpiresIn=PRESIGN_TTL_S,
            )
            out_urls.append({"name": name, "s3_key": key, "presigned_url": url})

    finally:
        # Always stop — session-seconds bill until sessionTimeoutSeconds.
        bac.stop_code_interpreter_session(
            codeInterpreterIdentifier=CI_IDENTIFIER,
            sessionId=session_id,
        )
        logger.info("stopped CI session id=%s", session_id)

    return {
        "session_id": session_id,
        "stdout":     stdout_text,
        "stderr":     stderr_text,
        "outputs":    out_urls,
        "success":    not stderr_text,
    }


def _invoke(bac, session_id: str, op_name: str, arguments: dict) -> dict:
    """Invoke and materialise the full streaming response.

    Operation names (verified from AWS docs):
      executeCode, executeCommand,
      readFiles,   writeFiles,   listFiles,   removeFiles,
      startCommandExecution, getTask, stopTask.
    """
    resp = bac.invoke_code_interpreter(
        codeInterpreterIdentifier=CI_IDENTIFIER,
        sessionId=session_id,
        name=op_name,                      # operation name, NOT code
        arguments=arguments,
    )
    events = []
    for event in resp.get("stream", []) or []:
        result = event.get("result") or {}
        events.append(result)
    return {"events": events}


def _collect_stdio(exec_resp: dict) -> tuple[str, str]:
    """Walk the streamed events and pull stdout + stderr text blocks out.

    The stream emits `result.content` arrays of `{type, text|data|...}`. Text
    content comes through `type == "text"` for stdout; `type == "error"`
    (or similar) for stderr. TODO(verify): the exact content-type enum —
    confirm against the CI streaming response schema.
    """
    stdout_parts, stderr_parts = [], []
    for ev in exec_resp["events"]:
        for c in ev.get("content", []) or []:
            if c.get("type") == "text":
                stdout_parts.append(c.get("text", ""))
            elif c.get("type") in ("error", "stderr"):
                stderr_parts.append(c.get("text", "") or c.get("error", ""))
    return "\n".join(stdout_parts), "\n".join(stderr_parts)


def _extract_file_bytes(read_resp: dict, name: str) -> bytes | None:
    """Walk readFiles response events and extract the bytes for `name`."""
    for ev in read_resp["events"]:
        for c in ev.get("content", []) or []:
            if c.get("type") == "file" and c.get("name") == name:
                # content is typically base64-encoded; decode if so.
                data = c.get("data") or c.get("blob")
                if isinstance(data, str):
                    import base64
                    return base64.b64decode(data)
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
    return None
```

### 3.4 SDK convenience client (`bedrock_agentcore.tools.code_interpreter_client`)

```python
"""The bedrock-agentcore Python SDK wraps start/invoke/stop in a single
client. Useful for short scripts; less flexible for production streaming."""
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

ci = CodeInterpreter(region="us-east-1")
ci.start()     # starts a session with the system identifier by default
res = ci.invoke("executeCode", {"language": "python", "code": "print('hi')"})
for event in res["stream"]:
    print(event["result"]["content"])
ci.stop()      # stops + releases resources

# TODO(verify): whether the SDK's `CodeInterpreter()` constructor accepts a
# custom `identifier="{project_name}_ci_dev"` kwarg for routing to a custom
# CI, or whether custom CIs require direct boto3 (as in §3.3).
```

### 3.5 Strands `@tool` wrapper — cross-reference `STRANDS_TOOLS §3.3`

```python
"""Code Interpreter as a Strands @tool — canonical production shape.

The STRANDS_TOOLS §3.3 shim shows the minimal `invoke_code_interpreter` call.
This wrapper goes deeper: full session lifecycle, S3 input round-trip,
chart presigned URLs for the agent's reply, robust error envelope.
"""
import base64, json, os, uuid
import boto3
from strands import tool

_bac            = boto3.client("bedrock-agentcore")
_s3             = boto3.client("s3")
CI_IDENTIFIER   = os.environ["CI_IDENTIFIER"]             # "aws.codeinterpreter.v1" default
FILES_BUCKET    = os.environ["CI_FILES_BUCKET"]
SESSION_TIMEOUT = int(os.environ.get("CI_SESSION_TIMEOUT_S", "900"))
PRESIGN_TTL     = int(os.environ.get("CI_PRESIGN_TTL_S", "3600"))


@tool
def run_code_analysis(
    code: str,
    language: str = "python",
    input_s3_keys: list[str] | None = None,
    output_filenames: list[str] | None = None,
) -> str:
    """Execute Python / JavaScript / TypeScript in the AgentCore sandbox.

    Available Python libraries: numpy, pandas, matplotlib, scipy, seaborn.

    Input files (listed by S3 key under CI_FILES_BUCKET) are placed in the
    sandbox before execution; their basenames become the sandbox filenames.
    Requested output files are uploaded to S3 after execution and returned as
    1-hour presigned URLs — never return raw bytes.

    Args:
        code:              Complete program text to execute.
        language:          "python" (default), "javascript", or "typescript".
        input_s3_keys:     Optional list of S3 keys under CI_FILES_BUCKET to
                           stage into the sandbox as /<basename>.
        output_filenames:  Optional list of sandbox filenames to download
                           after execution (e.g. ["chart.png", "report.pdf"]).
    Returns:
        JSON: { success, stdout, stderr, outputs: [{name, s3_key, presigned_url}] }
    """
    session_id = None
    try:
        sess = _bac.start_code_interpreter_session(
            codeInterpreterIdentifier=CI_IDENTIFIER,
            name=f"strands-{uuid.uuid4().hex[:8]}",
            sessionTimeoutSeconds=SESSION_TIMEOUT,
        )
        session_id = sess["sessionId"]

        # Stage inputs — read from caller S3, writeFiles into sandbox.
        for s3_key in (input_s3_keys or []):
            body = _s3.get_object(Bucket=FILES_BUCKET, Key=s3_key)["Body"].read()
            name = s3_key.rsplit("/", 1)[-1]
            _bac.invoke_code_interpreter(
                codeInterpreterIdentifier=CI_IDENTIFIER,
                sessionId=session_id,
                name="writeFiles",
                arguments={"content": [
                    {"path": name, "blob": base64.b64encode(body).decode()},
                ]},
            )

        # Execute.
        exec_resp = _bac.invoke_code_interpreter(
            codeInterpreterIdentifier=CI_IDENTIFIER,
            sessionId=session_id,
            name="executeCode",
            arguments={"language": language, "code": code},
        )
        stdout_parts, stderr_parts = [], []
        for event in exec_resp.get("stream", []) or []:
            result = event.get("result") or {}
            for c in result.get("content", []) or []:
                if c.get("type") == "text":
                    stdout_parts.append(c.get("text", ""))
                elif c.get("type") in ("error", "stderr"):
                    stderr_parts.append(c.get("text", "") or c.get("error", ""))

        # Collect outputs.
        outputs = []
        for name in (output_filenames or []):
            read_resp = _bac.invoke_code_interpreter(
                codeInterpreterIdentifier=CI_IDENTIFIER,
                sessionId=session_id,
                name="readFiles",
                arguments={"paths": [name]},
            )
            content = None
            for ev in read_resp.get("stream", []) or []:
                r = ev.get("result") or {}
                for c in r.get("content", []) or []:
                    if c.get("type") == "file" and c.get("name") == name:
                        data = c.get("data") or c.get("blob")
                        if isinstance(data, str):
                            content = base64.b64decode(data)
                        elif isinstance(data, (bytes, bytearray)):
                            content = bytes(data)
            if content is None:
                continue
            key = f"outputs/{session_id}/{name}"
            _s3.put_object(Bucket=FILES_BUCKET, Key=key, Body=content)
            url = _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": FILES_BUCKET, "Key": key},
                ExpiresIn=PRESIGN_TTL,
            )
            outputs.append({"name": name, "s3_key": key, "presigned_url": url})

        return json.dumps({
            "success": not stderr_parts,
            "stdout":  "\n".join(stdout_parts),
            "stderr":  "\n".join(stderr_parts),
            "outputs": outputs,
        })
    except Exception as e:
        # Rule 3 — tools MUST NOT raise out of the agent loop.
        return json.dumps({"error": str(e), "success": False})
    finally:
        if session_id:
            try:
                _bac.stop_code_interpreter_session(
                    codeInterpreterIdentifier=CI_IDENTIFIER,
                    sessionId=session_id,
                )
            except Exception:
                pass   # best-effort; session will expire on sessionTimeoutSeconds
```

### 3.6 Monolith gotchas

- **`aws.codeinterpreter.v1` is the verified system identifier** (AWS tutorial). Do not guess — it is a literal string, not a lookup pattern.
- **`invoke_code_interpreter.name` is the OPERATION NAME, not the code.** Supported values: `executeCode`, `executeCommand`, `readFiles`, `writeFiles`, `listFiles`, `removeFiles`, `startCommandExecution`, `getTask`, `stopTask`. Passing the code in `name=` returns `ValidationException`.
- **Streaming response — exhaust or leak.** `resp["stream"]` is a boto3 event generator; not iterating it leaves the HTTP connection hanging and the server-side session running. `for event in resp["stream"]` once, then the session can stop cleanly.
- **Session leaks bill per-second.** Identical pattern to Browser Tool — `try/finally stop_code_interpreter_session`. Do not rely on `sessionTimeoutSeconds` as cleanup.
- **`language` parameter is case-sensitive** — `"python"` | `"javascript"` | `"typescript"`. `"Python"` or `"py"` returns `ValidationException`.
- **Sandbox is wiped on session stop.** File I/O that needs to outlive the session MUST round-trip to S3 via `readFiles` + `put_object`. Putting chart files directly into the sandbox and never reading them back is the #1 lost-work bug.
- **Control vs data plane clients.** `boto3.client("bedrock-agentcore-control")` for `create_code_interpreter` / `list_code_interpreters`; `boto3.client("bedrock-agentcore")` for `start_code_interpreter_session` / `invoke_code_interpreter` / `stop_code_interpreter_session`. Mixing them returns `UnknownOperationError`.
- **System-CI ARN scoping is awkward.** The system CI lives in an AWS-owned account; the ARN you'd put in an IAM resource restriction is not trivially constructible. Most projects use `resources=["*"]` on `StartCodeInterpreterSession` + `InvokeCodeInterpreter` when using the system CI; for custom CIs, scope to the specific `code_interpreter_arn`. `# TODO(verify): scoped-ARN pattern for the system interpreter — confirm with the IAM condition-key section of the AgentCore devguide.`
- **File-content encoding in `writeFiles` / `readFiles`.** `# TODO(verify): whether the payload is raw bytes (blob), base64-encoded string (data), or one or the other depending on operation` — consult API-reference examples. Using the wrong shape returns `InvalidRequestException` with a misleading message.
- **No GPU in the managed CI.** Pure CPU sandbox. Numeric workloads that need CUDA fall back to SageMaker / Batch; the CI is not a Tesla replacement.

---

## 4. Micro-Stack Variant

**Use when:** `CodeInterpreterStack` owns the custom `CodeInterpreter` (or none, if system) + files bucket + CMK + (optional) exec role; consumer Lambdas / AgentCore Runtimes in `ComputeStack` / per-agent stacks call the data plane.

### 4.1 The five non-negotiables (cite `LAYER_BACKEND_LAMBDA §4.1`)

1. **Anchor Lambda `code=from_asset(...)` to `Path(__file__)`** — `_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"`.
2. **Never call `ci.grant_invoke(other_role)` across stacks.** Publish `ci_arn` + `ci_identifier` via SSM; consumer grants itself `bedrock-agentcore:StartCodeInterpreterSession` / `InvokeCodeInterpreter` / `StopCodeInterpreterSession` identity-side on the ARN token.
3. **Never share the KMS CMK across stacks via object reference.** Own the CMK in `CodeInterpreterStack`; publish `key_arn` via SSM; consumer grants itself `kms:Decrypt` / `kms:GenerateDataKey` identity-side.
4. **`iam:PassRole` with `iam:PassedToService` condition** on the deploy role for the custom CI exec role (only applies when `create_code_interpreter` is used).
5. **PermissionsBoundary on every role** — CI exec role (if custom) + every consumer role.

### 4.2 Dedicated `CodeInterpreterStack`

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
    CodeInterpreter, CodeInterpreterNetworkMode,
)
from constructs import Construct

# stacks/code_interpreter_stack.py -> stacks/ -> cdk/ -> infrastructure/ -> <repo root>
_LAMBDAS_ROOT: Path = Path(__file__).resolve().parents[3] / "lambda"


class CodeInterpreterStack(cdk.Stack):
    """Owns the CI files bucket, local CMK, and optionally a custom CI.
    Publishes identifier + ARN + files bucket + KMS via SSM so consumer
    stacks can wire identity-side grants without cross-stack imports.
    """

    def __init__(
        self,
        scope: Construct,
        stage_name: str,
        permission_boundary: iam.IManagedPolicy,
        vpc: ec2.IVpc | None = None,
        files_retention_days: int | None = None,
        use_custom_ci: bool = False,
        network_mode: str = "PUBLIC",                 # or "VPC"
        **kwargs,
    ) -> None:
        super().__init__(
            scope, f"{{project_name}}-code-interpreter-{stage_name}", **kwargs,
        )
        for k, v in {"Project": "{project_name}", "ManagedBy": "cdk"}.items():
            cdk.Tags.of(self).add(k, v)

        # A) Local CMK.
        cmk = kms.Key(
            self, "CiKey",
            alias=f"alias/{{project_name}}-ci-{stage_name}",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
        )

        # B) Files bucket.
        retention = files_retention_days or (30 if stage_name == "prod" else 7)
        files_bucket = s3.Bucket(
            self, "FilesBucket",
            bucket_name=f"{{project_name}}-ci-files-{stage_name}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(
                id="ExpireCiFiles", enabled=True,
                expiration=Duration.days(retention),
            )],
            removal_policy=(
                RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY
            ),
            auto_delete_objects=(stage_name != "prod"),
        )

        # C) Custom CI (optional). Most projects use the system CI.
        ci_arn:        str
        ci_identifier: str

        if use_custom_ci:
            ci_exec_role = iam.Role(
                self, "CiExecRole",
                role_name=f"{{project_name}}-ci-exec-{stage_name}",
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            )
            iam.PermissionsBoundary.of(ci_exec_role).apply(permission_boundary)

            if network_mode == "VPC":
                if vpc is None:
                    raise ValueError("vpc required when network_mode='VPC'")
                net = CodeInterpreterNetworkMode.using_vpc(
                    self, vpc=vpc,
                    vpc_subnets=ec2.SubnetSelection(
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    ),
                )
            else:
                net = CodeInterpreterNetworkMode.using_public_network()

            ci = CodeInterpreter(
                self, "ResearchCi",
                code_interpreter_name=f"{{project_name}}_ci_{stage_name}",
                description=f"Research CI {stage_name}",
                execution_role=ci_exec_role,
                network_mode=net,
            )
            ci_arn        = ci.code_interpreter_arn
            ci_identifier = ci.code_interpreter_identifier
        else:
            ci_identifier = "aws.codeinterpreter.v1"
            ci_arn        = "*"
            # Scoped-ARN for system CI is not trivially constructible —
            # see gotcha 7 in §3.6. Use "*" and rely on the identifier
            # parameter for routing.

        # D) Publish for consumer stacks.
        ssm.StringParameter(
            self, "CiIdentifierParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ci/identifier",
            string_value=ci_identifier,
        )
        ssm.StringParameter(
            self, "CiArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ci/arn",
            string_value=ci_arn,
        )
        ssm.StringParameter(
            self, "CiFilesBucketParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ci/files_bucket",
            string_value=files_bucket.bucket_name,
        )
        ssm.StringParameter(
            self, "CiKmsArnParam",
            parameter_name=f"/{{project_name}}/{stage_name}/ci/kms_arn",
            string_value=cmk.key_arn,
        )

        self.ci_identifier       = ci_identifier
        self.ci_arn              = ci_arn
        self.files_bucket        = files_bucket
        self.cmk                 = cmk
        self.permission_boundary = permission_boundary

        CfnOutput(self, "CiIdentifier",  value=ci_identifier)
        CfnOutput(self, "CiFilesBucket", value=files_bucket.bucket_name)
```

### 4.3 Consumer pattern — identity-side grants in `ComputeStack`

```python
# Inside ComputeStack — no CI / files-bucket construct references.
from aws_cdk import aws_ssm as ssm, aws_iam as iam, aws_lambda as _lambda

ci_identifier = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/ci/identifier"
)
ci_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/ci/arn"
)
ci_files_bucket = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/ci/files_bucket"
)
ci_kms_arn = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage_name}/ci/kms_arn"
)

analysis_fn = _lambda.Function(
    self, "CiAnalysisFn",
    # ... standard config ...
    environment={
        "CI_IDENTIFIER":          ci_identifier,
        "CI_FILES_BUCKET":        ci_files_bucket,
        "CI_SESSION_TIMEOUT_S":   "900",
        "CI_PRESIGN_TTL_S":       "3600",
    },
)

analysis_fn.add_to_role_policy(iam.PolicyStatement(
    actions=[
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:StopCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
    ],
    # ci_arn resolves to the custom-CI ARN at deploy time, or to "*" for the
    # system CI (scoping is then by `codeInterpreterIdentifier` at runtime).
    resources=[ci_arn],
))
analysis_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    resources=[f"arn:aws:s3:::{ci_files_bucket}/*"],
))
analysis_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["s3:ListBucket"],
    resources=[f"arn:aws:s3:::{ci_files_bucket}"],
))
analysis_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
    resources=[ci_kms_arn],
))

iam.PermissionsBoundary.of(analysis_fn.role).apply(self.permission_boundary)
```

### 4.4 Micro-stack gotchas

- **`value_for_string_parameter` returns a token.** Use it directly in `resources=[...]` and `environment={...}`. Do not `.split(":")` or compute on it in Python at synth time.
- **Files-bucket ARN is not published; only the bucket NAME.** Consumers build the scoped ARNs with f-strings. Intentional — exposing the bare bucket ARN invites `resources=["*"]` accidents.
- **Cross-stack deletion order**: if `CodeInterpreterStack` is deleted while `ComputeStack` still references `CiArn` via SSM, the param disappears and the consumer stack fails on next deploy. Deploy order: `CodeInterpreterStack` first; delete order: `ComputeStack` first.
- **Streaming responses must be exhausted within the consumer Lambda.** Never return `resp["stream"]` as the Lambda return value (not JSON-serialisable + connection leaks). Materialise into stdout/stderr strings + presigned URLs before returning.
- **`use_custom_ci=False` path is the common case.** The system CI covers 95% of deep-research workloads. Only flip to `True` when you need VPC egress from the sandbox or a custom exec role for non-trivial audit logging.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| POC / single-agent data analysis | §3 Monolith + system CI `aws.codeinterpreter.v1`; files bucket only |
| Production with custom retention or replay | §4 Micro-Stack, system CI, files bucket in `CodeInterpreterStack` with lifecycle + CMK |
| Need private-VPC egress from sandbox (e.g. reach internal APIs from user code) | Switch to `use_custom_ci=True` + `network_mode="VPC"`; consumer stack still grants on the custom CI ARN |
| TypeScript / JavaScript code instead of Python | Same CI — pass `language="javascript"` or `language="typescript"` in `arguments`. No infra change |
| Shell / CLI use case (e.g. run `curl` inside sandbox) | Use `name="executeCommand"` with `{"command": "curl ...", "args": [...]}`. Same session |
| Long-running analysis (> 15 min) | Use `startCommandExecution` + poll `getTask`; increase `sessionTimeoutSeconds`. `# TODO(verify): max sessionTimeoutSeconds — current cap from the CI API reference.` |
| Multiple tenants | System CI + one files-bucket prefix per tenant (`files/{tenant}/...`); identity-side S3 grant scopes per tenant. Custom CI per tenant only if VPC egress differs |
| Need GPU | Not supported — fall back to SageMaker Processing / Batch. Keep CI for orchestration code |
| Chart-heavy replies | Enforce `output_filenames` in the `@tool` signature; never return raw PNG bytes through the agent |

---

## 6. Worked example — pytest offline CDK synth harness

Save as `tests/sop/test_AGENTCORE_CODE_INTERPRETER.py`. Offline; no AWS calls.

```python
"""SOP verification — CodeInterpreterStack synth (system + custom paths)."""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template, Match


def _env() -> cdk.Environment:
    return cdk.Environment(account="000000000000", region="us-east-1")


def test_code_interpreter_stack_system_mode_no_ci_resource():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.code_interpreter_stack import CodeInterpreterStack
    stack = CodeInterpreterStack(
        app, stage_name="dev",
        permission_boundary=boundary,
        use_custom_ci=False,
        env=env,
    )
    t = Template.from_stack(stack)

    # System CI => no CodeInterpreter resource; only bucket + key + 4 params
    t.resource_count_is("AWS::BedrockAgentCore::CodeInterpreter", 0)
    t.resource_count_is("AWS::S3::Bucket", 1)
    t.resource_count_is("AWS::KMS::Key", 1)
    t.resource_count_is("AWS::SSM::Parameter", 4)

    # Files bucket with SSE-KMS + public access fully blocked
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


def test_code_interpreter_stack_custom_mode_creates_ci_resource():
    app = cdk.App()
    env = _env()

    deps = cdk.Stack(app, "Deps", env=env)
    boundary = iam.ManagedPolicy(
        deps, "Boundary",
        statements=[iam.PolicyStatement(actions=["*"], resources=["*"])],
    )

    from infrastructure.cdk.stacks.code_interpreter_stack import CodeInterpreterStack
    stack = CodeInterpreterStack(
        app, stage_name="prod",
        permission_boundary=boundary,
        use_custom_ci=True,
        env=env,
    )
    t = Template.from_stack(stack)

    t.resource_count_is("AWS::BedrockAgentCore::CodeInterpreter", 1)
    t.resource_count_is("AWS::S3::Bucket", 1)
    t.resource_count_is("AWS::KMS::Key", 1)
    # Custom CI adds an exec role on top of the files bucket + CMK +
    # 4 SSM params; count is environment-dependent.


def test_consumer_has_identity_side_grants_only():
    app = cdk.App()
    env = _env()

    from infrastructure.cdk.stacks.compute_stack import ComputeStack
    stack = ComputeStack(app, stage_name="dev", env=env)
    t = Template.from_stack(stack)

    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": {
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": Match.array_with([
                        "bedrock-agentcore:StartCodeInterpreterSession",
                        "bedrock-agentcore:InvokeCodeInterpreter",
                        "bedrock-agentcore:StopCodeInterpreterSession",
                    ]),
                }),
            ]),
        },
    }))
```

---

## 7. References

- `docs/template_params.md` — `CI_IDENTIFIER_SSM_NAME`, `CI_ARN_SSM_NAME`, `CI_FILES_BUCKET_SSM_NAME`, `CI_SESSION_TIMEOUT_S`, `CI_PRESIGN_TTL_S`, `CI_USE_CUSTOM`, `CI_NETWORK_MODE` (`PUBLIC` | `VPC`), `CI_FILES_RETENTION_DAYS`
- `docs/Feature_Roadmap.md` — feature IDs `AC-CI-01` (files bucket + CMK), `AC-CI-02` (custom CI resource, optional), `AC-CI-03` (consumer Lambda with session lifecycle), `AC-CI-04` (Strands @tool wrapper), `AC-CI-05` (chart presigned-URL idiom)
- AWS docs:
  - [Code Interpreter overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-tool.html)
  - [Create a custom Code Interpreter](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-create.html)
  - [Using Code Interpreter directly](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-using-directly.html)
  - [Start a CI session](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-start-session.html)
  - [Code Interpreter API reference examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-api-reference-examples.html)
  - [boto3 `bedrock-agentcore.invoke_code_interpreter`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_code_interpreter.html)
  - [Introducing the AgentCore Code Interpreter (AWS blog)](https://aws.amazon.com/blogs/machine-learning/introducing-the-amazon-bedrock-agentcore-code-interpreter/)
  - [`@aws-cdk/aws-bedrock-agentcore-alpha` L2 alpha](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrock_agentcore_alpha/README.html)
  - [`aws-cdk-lib.aws_bedrockagentcore` L1](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html)
  - [`bedrock-agentcore` SDK for Python](https://github.com/aws/bedrock-agentcore-sdk-python)
  - [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- Related SOPs:
  - `STRANDS_TOOLS` — `@tool` design rules + existing Code Interpreter shim in §3.3 (this SOP is the deep-dive; the shim cross-references here)
  - `AGENTCORE_RUNTIME` — consumer pattern when the CI is driven from an agent runtime
  - `AGENTCORE_BROWSER_TOOL` — paired sandbox tool; same control/data-plane split and session-lifecycle pattern
  - `AGENTCORE_AGENT_CONTROL` — Cedar policies that can gate `executeCommand` operations to read-only shells
  - `LAYER_BACKEND_LAMBDA` — five non-negotiables, identity-side grant helpers, Lambda container image option for Playwright-heavy peers
  - `LAYER_SECURITY` — KMS CMK patterns, S3 SSE-KMS, presigned-URL TTL discipline

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-22 | Initial partial — AgentCore Code Interpreter GA deep-dive. Dual-variant monolith (§3) + micro-stack `CodeInterpreterStack` (§4). Alpha L2 `CodeInterpreter` construct for custom CIs, L1 `CfnCodeInterpreter` fallback, system interpreter via `aws.codeinterpreter.v1` identifier (verified). Two-client split (`bedrock-agentcore-control` for create, `bedrock-agentcore` for start/invoke/stop). Full session lifecycle consumer handler with S3 input round-trip, streaming-response materialisation, chart presigned-URL outputs. `@tool` wrapper goes deeper than the `STRANDS_TOOLS §3.3` shim (full lifecycle, input staging, output download, `try/finally` discipline). Operation-name vocabulary (`executeCode` / `executeCommand` / `readFiles` / `writeFiles` / `listFiles` / `removeFiles` / `startCommandExecution` / `getTask` / `stopTask`) documented. Created to fill gap surfaced by Deep Research Agent kit design, grounded in AgentCore GA documentation. |
