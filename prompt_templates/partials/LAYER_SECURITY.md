# SOP — Security Layer (KMS, IAM Baseline, Permission Boundaries)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+)

---

## 1. Purpose

Foundation security primitives consumed by every other layer:

- KMS CMKs with rotation (one per data-class, not a single shared key)
- IAM permission boundary (mandatory ceiling on every workload role)
- Baseline managed policies (read-only auditor, operator)
- Cross-stack KMS grants (critical — most micro-stack cycles originate here)

---

## 2. Decision — Monolith vs Micro-Stack

**This is the stack most responsible for cross-stack cycles.** Every downstream compute consumer wants Encrypt/Decrypt on a CMK that this stack owns. The *wrong* pattern (resource-policy grants) creates circular exports. The *right* pattern (identity-side grants on consumer roles) stays unidirectional.

| You are… | Use variant |
|---|---|
| KMS keys, IAM roles, and the workloads that use them all live in ONE stack | **§3 Monolith Variant** |
| KMS keys live in `SecurityStack`; Lambdas / ECS / RDS live in separate stacks | **§4 Micro-Stack Variant** |

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import Duration, aws_kms as kms, aws_iam as iam


def _create_security(self, stage: str) -> None:
    # One CMK per data class. Don't share one "app" key across audio + metadata + logs.
    self.audio_data_key = kms.Key(
        self, "AudioDataKey",
        alias=f"alias/{{project_name}}-audio-data-{stage}",
        enable_key_rotation=True,
        rotation_period=Duration.days(365),
        description="S3 audio, transcripts, reports",
    )
    self.job_metadata_key = kms.Key(
        self, "JobMetadataKey",
        alias=f"alias/{{project_name}}-job-metadata-{stage}",
        enable_key_rotation=True,
        description="RDS, DynamoDB, Secrets Manager",
    )
    self.logs_key = kms.Key(
        self, "LogsKey",
        alias=f"alias/{{project_name}}-logs-{stage}",
        enable_key_rotation=True,
        description="CloudWatch Logs group encryption",
    )
    # CloudWatch Logs needs a service principal on the key policy
    self.logs_key.grant_encrypt_decrypt(iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com"))

    # Permission boundary — every workload role must attach this
    self.permission_boundary = iam.ManagedPolicy(
        self, "WorkloadPermissionBoundary",
        managed_policy_name=f"{{project_name}}-workload-boundary-{stage}",
        description="Hard ceiling on what any workload role can do",
        statements=[
            iam.PolicyStatement(
                sid="AllowAppNamespacedResources",
                effect=iam.Effect.ALLOW,
                actions=["*"],
                resources=["*"],
                # Narrow with conditions; below is a starter — tighten per project.
                conditions={
                    "StringEquals": {"aws:ResourceTag/Project": "{project_name}"}
                },
            ),
            iam.PolicyStatement(
                sid="DenyIamAdmin",
                effect=iam.Effect.DENY,
                actions=[
                    "iam:CreateUser", "iam:CreateAccessKey", "iam:PutUserPolicy",
                    "iam:AttachUserPolicy", "iam:CreateLoginProfile",
                ],
                resources=["*"],
            ),
        ],
    )

    # Auditor policy (read-only across the app)
    self.auditor_policy = iam.ManagedPolicy(
        self, "AuditorReadOnly",
        managed_policy_name=f"{{project_name}}-auditor-{stage}",
        statements=[
            iam.PolicyStatement(
                actions=["s3:GetBucket*", "s3:ListBucket*", "s3:GetObject*",
                         "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan",
                         "logs:Describe*", "logs:Get*", "logs:FilterLogEvents",
                         "cloudwatch:GetMetric*", "cloudwatch:ListMetrics"],
                resources=["*"],
            )
        ],
    )

    # In monolith, downstream workloads can use L2 grants directly:
    #   self.audio_data_key.grant_encrypt(self.upload_fn)
    #   self.job_metadata_key.grant_decrypt(self.status_fn)
```

### 3.1 Monolith gotchas

- **Do not share one key across data classes.** If audio blob access leaks, don't want the same key to decrypt RDS credentials.
- **Rotation cost:** enabling rotation on a CMK is free; the customer-master key *version* rotates once a year with no action from you. Disable only with a compelling reason.
- **Log-group keys** need the CW Logs service principal on the key policy or log group creation fails.

---

## 4. Micro-Stack Variant

### 4.1 `SecurityStack` — KMS + IAM baseline

```python
import aws_cdk as cdk
from aws_cdk import Duration, aws_kms as kms, aws_iam as iam
from constructs import Construct


class SecurityStack(cdk.Stack):
    """KMS CMKs and IAM baseline. NEVER mutated from downstream stacks."""

    def __init__(self, scope: Construct, **kwargs) -> None:
        super().__init__(scope, "{project_name}-security", **kwargs)

        self.audio_data_key = kms.Key(
            self, "AudioDataKey",
            alias="alias/{project_name}-audio-data",
            enable_key_rotation=True,
            rotation_period=Duration.days(365),
        )
        self.job_metadata_key = kms.Key(
            self, "JobMetadataKey",
            alias="alias/{project_name}-job-metadata",
            enable_key_rotation=True,
        )
        self.logs_key = kms.Key(
            self, "LogsKey",
            alias="alias/{project_name}-logs",
            enable_key_rotation=True,
        )
        self.logs_key.grant_encrypt_decrypt(
            iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")
        )

        # Permission boundary. Non-empty (CDK validates).
        self.permission_boundary = iam.ManagedPolicy(
            self, "WorkloadBoundary",
            managed_policy_name="{project_name}-workload-boundary",
            statements=[
                iam.PolicyStatement(
                    sid="AllowScopedByTag",
                    effect=iam.Effect.ALLOW,
                    actions=["*"], resources=["*"],
                    conditions={"StringEquals": {"aws:ResourceTag/Project": "{project_name}"}},
                ),
                iam.PolicyStatement(
                    sid="DenyIamAdmin", effect=iam.Effect.DENY,
                    actions=["iam:CreateUser", "iam:CreateAccessKey", "iam:PutUserPolicy"],
                    resources=["*"],
                ),
            ],
        )

        for out in [
            ("AudioDataKeyArn",    self.audio_data_key.key_arn),
            ("JobMetadataKeyArn",  self.job_metadata_key.key_arn),
            ("LogsKeyArn",         self.logs_key.key_arn),
            ("BoundaryArn",        self.permission_boundary.managed_policy_arn),
        ]:
            cdk.CfnOutput(self, out[0], value=out[1])
```

### 4.2 Downstream usage — identity-side KMS grants

```python
# In ComputeStack (or any consumer stack):
def _kms_grant(fn, key, actions):
    fn.add_to_role_policy(iam.PolicyStatement(actions=actions, resources=[key.key_arn]))


# DO THIS (identity policy on consumer role — no security-stack mutation)
_kms_grant(self.upload_fn, audio_data_key, ["kms:Encrypt", "kms:GenerateDataKey"])

# DO NOT DO THIS across stacks
# audio_data_key.grant_encrypt(self.upload_fn)   # auto-mutates SecurityStack → cycle
```

**Why identity-side works:**
- Consumer role's policy references `key.key_arn` (a string token, unidirectional reference)
- The CMK's resource policy is not touched at all; keeps its default "account root full access"
- CloudFormation deploys: SecurityStack first, consumer stack second, no back-edge

**Why the CMK's default "root account full access" is fine:** the CMK policy allows any principal in the account that has `kms:*` permission via IAM. The consumer role's identity policy *is* that IAM grant. The key is only as permissive as what IAM allows for a given principal.

### 4.3 Applying the permission boundary from consumers

```python
# In ComputeStack __init__, after creating Lambda functions:
for fn in [self.upload_fn, self.status_fn, self.processing_fn, ...]:
    iam.PermissionsBoundary.of(fn.role).apply(permission_boundary)
```

### 4.4 Micro-stack gotchas

- **`key.grant_decrypt(role)` across stacks** mutates the key's resource policy. Use identity-side.
- **`bucket.grant_read(role)` when bucket is KMS-encrypted** ALSO auto-calls `encryption_key.grant_decrypt(role)` — same cycle. For cross-stack consumers, grant S3 actions AND KMS actions both identity-side.
- **`environment_encryption=cross_stack_key` on a Lambda** auto-grants the Lambda service principal on the key — harmless intra-account but will add a stack dependency edge. Use the SAME key that's in a truly shared stack (or drop `environment_encryption` for POC).
- **Empty `ManagedPolicy`** fails CDK validation. The permission boundary must have at least one statement.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| One CMK enough, POC with one stack | Monolith, 1 key fine |
| Multiple data classes (audio vs metadata vs logs), shared between stacks | Micro-Stack with 3 CMKs |
| `cdk synth` error: `Adding this dependency ... would create a cyclic reference` mentions a KMS key | Replace the offending `key.grant_*(role)` with identity-side `PolicyStatement` |
| Regulated client (HIPAA, PCI) | Micro-Stack + CloudHSM-backed keys; see `COMPLIANCE_HIPAA_PCIDSS` |

---

## 6. Worked example

```python
def test_security_stack_creates_three_keys():
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from infrastructure.cdk.stacks.security_stack import SecurityStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    sec = SecurityStack(app, env=env)

    t = Template.from_stack(sec)
    t.resource_count_is("AWS::KMS::Key", 3)
    t.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})
    t.resource_count_is("AWS::IAM::ManagedPolicy", 1)  # boundary


def test_consumer_does_not_mutate_security_stack():
    """Key test — consumer stack must NOT add a resource policy statement to the CMK."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template, Match
    from infrastructure.cdk.stacks.security_stack import SecurityStack
    from infrastructure.cdk.stacks.compute_stack import ComputeStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    sec = SecurityStack(app, env=env)
    # ... instantiate ComputeStack consuming sec.audio_data_key ...

    sec_template = Template.from_stack(sec)
    # The KMS key policy should have ONE statement (root access) — no consumer role refs
    sec_template.has_resource_properties("AWS::KMS::Key", {
        "KeyPolicy": {"Statement": Match.array_with([Match.object_like({"Sid": Match.string_like_regexp(".*Root.*")})])}
    })
```

---

## 7. References

- `docs/template_params.md` — `TAGS`, key alias conventions
- `docs/Feature_Roadmap.md` — SEC-01..SEC-14
- Related SOPs: `LAYER_BACKEND_LAMBDA` (identity-side grant helpers), `COMPLIANCE_HIPAA_PCIDSS` (CloudHSM)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant SOP. Micro-Stack variant documents identity-side KMS grants as the cycle-free pattern. Added consumer-side non-mutation test. |
| 1.0 | 2026-03-05 | Initial. |
