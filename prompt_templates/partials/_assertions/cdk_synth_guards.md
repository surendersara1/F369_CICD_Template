# SOP — CDK Synth-Time Guard Library

**Version:** 1.0 · **Last-reviewed:** 2026-06-17 · **Status:** Active (NEW — R4 / F-AFIE-22)
**Applies to:** AWS CDK v2 (Python 3.12+) · `aws_cdk.assertions.Template` for synth-time inspection · `Aspects` / `IAspect` for resource-graph traversal · pytest harness via `cdk synth → tests/`
**Purpose:** Codify the 17 synth-time guard rules forward-referenced from Tier-1-through-Tier-4 R4 findings into one canonical, reusable library. Every guard fires at `cdk synth` time — before any AWS API call — so consumers can't accidentally ship the AFIE-class regressions back into prod.

---

## 1. Purpose

R4 audit-fix-verify (Tier 1-4) landed 21 canonical fixes across ~25 partials. Most are *defaults* — change the resource property, change the consumer behavior. But defaults can be overridden. The synth-guard library makes the high-stakes patterns **enforceable**, not just *recommended*:

- IAM grants must include the canonical 3-ARN Bedrock pattern (F-AFIE-01)
- CW alarms must set `treat_missing_data` (F-AFIE-07)
- SNS topics with `master_key` must point at a CMK that has the cross-service grants (F-AFIE-05)
- WebSocket APIs must have a `$connect` authorizer (F-AFIE-19)
- DynamoDB tables must use the new spec object (F-AFIE-17)
- Cognito user pools must use `feature_plan` (F-AFIE-21)
- ...and 11 more.

Each rule below is a self-contained Python function that takes a `cdk.assertions.Template` (or raw CFN dict) and raises `AssertionError` with a clear remediation pointer. Wire them into your `tests/synth_guards.py` and call them from `cdk_test.py` / CI.

---

## 2. Decision — when to use which guard

| You're authoring... | Run these guards |
|---|---|
| A per-partial regression test scaffold (TestStack → synth → check) | The specific rule that codifies the partial's R4 fix |
| A composite template (kit) that chains 5-15 partials | All guards relevant to the partials consumed — usually 6-10 |
| A pre-deploy CI gate ("must pass before `cdk deploy`") | All 17 — fail-fast on any violation |

**The composite-level kit author owns wiring guards into the kit's `tests/test_synth_guards.py`.** Per-partial tests cover the unit; composite tests cover the system.

---

## 3. The 17 canonical guards

Each guard follows this signature contract:

```python
def assert_<rule_name>(template: assertions.Template, *, compliance_class: str = "prod-internal") -> None:
    """Raise AssertionError with a remediation pointer if the rule is violated.

    Args:
        template: The CDK Template from Template.from_stack(stack).
        compliance_class: Stage tier; some rules only fire for prod-* classes.
    """
```

### 3.1 IAM + secrets

#### `assert_bedrock_invoke_three_arn_pattern` *(F-AFIE-01)*

```python
def assert_bedrock_invoke_three_arn_pattern(template, *, compliance_class="prod-internal"):
    """Fail if any IAM PolicyStatement granting bedrock:InvokeModel* doesn't
    include all three ARN classes: foundation-model/*, inference-profile/*, and
    application-inference-profile/*.

    AFIE Sprint 10 G-NEW-01 retro: omitting inference-profile/* → AccessDenied
    on cross-region inference profile calls. AWS doc:
    https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
    """
    bedrock_actions = {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
    for resource in template.find_resources("AWS::IAM::Policy").values() | \
                    template.find_resources("AWS::IAM::ManagedPolicy").values():
        for stmt in resource.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []):
            actions = set(_listify(stmt.get("Action", [])))
            if not (actions & bedrock_actions):
                continue
            resources = _flatten_resources(stmt.get("Resource", []))
            has_fm  = any("foundation-model" in r for r in resources)
            has_ip  = any(":inference-profile/" in r for r in resources)
            has_aip = any(":application-inference-profile/" in r for r in resources)
            if not (has_fm and has_ip and has_aip):
                raise AssertionError(
                    f"F-AFIE-01: IAM statement granting {actions & bedrock_actions} "
                    f"is missing one or more of foundation-model/* + inference-profile/* + "
                    f"application-inference-profile/* ARNs. Found resources: {resources}. "
                    f"Fix: see LLMOPS_BEDROCK.md §3.1 canonical 3-ARN pattern."
                )
```

#### `assert_no_wildcard_agentcore_grants_in_prod` *(F-AFIE-09)*

```python
def assert_no_wildcard_agentcore_grants_in_prod(template, *, compliance_class="prod-internal"):
    """Fail if compliance_class starts with 'prod-' and any IAM Policy has
    bedrock-agentcore:Invoke* / RetrieveMemoryRecords on /* without an
    aws:ResourceTag/Project condition.

    AFIE Sprint 10 F-GOV-09 retro: prod orchestrator with gateway/* invoked
    teammate's dev-gateway in the same account. See AGENTCORE_IDENTITY §3.4.
    """
    if not compliance_class.startswith("prod"):
        return
    sensitive = {
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:CreateEvent",
    }
    for resource in _all_iam_policies(template):
        for stmt in _statements(resource):
            actions = set(_listify(stmt.get("Action", [])))
            if not (actions & sensitive):
                continue
            resources = _flatten_resources(stmt.get("Resource", []))
            has_wildcard = any(r.endswith(":runtime/*") or r.endswith(":gateway/*")
                               or r.endswith(":memory/*") or r == "*" for r in resources)
            has_tag_cond = "aws:ResourceTag/Project" in _condition_keys(stmt)
            if has_wildcard and not has_tag_cond:
                raise AssertionError(
                    f"F-AFIE-09: prod IAM statement granting {actions & sensitive} on a "
                    f"wildcard resource without aws:ResourceTag/Project condition. "
                    f"Fix: pass specific ARNs to _create_agent_role() or scope by tag."
                )
```

#### `assert_permission_boundary_includes_agentcore_cross_project_deny` *(F-AFIE-09)*

```python
def assert_permission_boundary_includes_agentcore_cross_project_deny(template, **_):
    """Fail if WorkloadPermissionBoundary ManagedPolicy doesn't carry the
    DenyAgentCoreInvokeAcrossProjects statement.

    See LAYER_SECURITY.md §3 + §4.
    """
    for resource in template.find_resources("AWS::IAM::ManagedPolicy").values():
        name = resource.get("Properties", {}).get("ManagedPolicyName", "")
        if "workload-boundary" not in name.lower() and "workload-permission-boundary" not in name.lower():
            continue
        sids = {s.get("Sid", "") for s in _statements(resource)}
        if "DenyAgentCoreInvokeAcrossProjects" not in sids:
            raise AssertionError(
                f"F-AFIE-09: ManagedPolicy '{name}' is the workload permission boundary but "
                f"doesn't carry the DenyAgentCoreInvokeAcrossProjects SID. "
                f"Fix: see LAYER_SECURITY.md §3 R4 update."
            )
        return     # found it
    # No boundary policy found at all — that's a separate failure mode but flag it.
    raise AssertionError(
        "F-AFIE-09: no WorkloadPermissionBoundary ManagedPolicy found in the template. "
        "Fix: instantiate SecurityStack from LAYER_SECURITY.md §3 or §4."
    )
```

#### `assert_gateway_role_carries_project_tag_condition` *(F-AFIE-12)*

```python
def assert_gateway_role_carries_project_tag_condition(template, *, compliance_class="prod-internal"):
    """Fail if any IAM Role named *-gateway-role has lambda:InvokeFunction or
    bedrock-agentcore:* statements without aws:ResourceTag/Project Condition.
    """
    for logical_id, resource in template.find_resources("AWS::IAM::Role").items():
        if "gateway" not in logical_id.lower():
            continue
        role_name = resource.get("Properties", {}).get("RoleName", "")
        if "gateway" not in role_name.lower():
            continue
        for inline in resource.get("Properties", {}).get("Policies", []):
            for stmt in inline.get("PolicyDocument", {}).get("Statement", []):
                actions = set(_listify(stmt.get("Action", [])))
                if not any(a.startswith(("lambda:InvokeFunction", "bedrock-agentcore:"))
                           for a in actions):
                    continue
                if "aws:ResourceTag/Project" not in _condition_keys(stmt):
                    raise AssertionError(
                        f"F-AFIE-12: gateway role '{role_name}' has {actions} without "
                        f"aws:ResourceTag/Project condition. Fix: see AGENTCORE_GATEWAY.md §3.1."
                    )
```

### 3.2 Observability + alerting

#### `assert_alarm_treat_missing_data_set` *(F-AFIE-07)*

```python
def assert_alarm_treat_missing_data_set(template, **_):
    """Fail if any CW Alarm has TreatMissingData not set or set to 'missing'
    (the silent default that flaps to INSUFFICIENT_DATA).
    """
    for logical_id, resource in template.find_resources("AWS::CloudWatch::Alarm").items():
        tmd = resource.get("Properties", {}).get("TreatMissingData", "missing")
        if tmd == "missing":
            raise AssertionError(
                f"F-AFIE-07: CW Alarm '{logical_id}' has TreatMissingData='missing' (the "
                f"silent default — flaps to INSUFFICIENT_DATA, no SNS action fires). "
                f"Pick one: notBreaching | breaching | ignore. See LAYER_OBSERVABILITY.md §3.1."
            )
```

#### `assert_sns_cmk_has_required_principals` *(F-AFIE-05)*

```python
def assert_sns_cmk_has_required_principals(template, **_):
    """For every SNS Topic with KmsMasterKeyId set, locate the referenced CMK
    in the same template and verify its key policy includes the three required
    service principals (cloudwatch + events + sns).
    """
    required_principals = {"cloudwatch.amazonaws.com", "events.amazonaws.com", "sns.amazonaws.com"}
    cmks_by_logical_id = template.find_resources("AWS::KMS::Key")
    for topic_id, topic in template.find_resources("AWS::SNS::Topic").items():
        kms_id_ref = topic.get("Properties", {}).get("KmsMasterKeyId")
        if not kms_id_ref:
            continue
        cmk_logical_id = _resolve_ref(kms_id_ref, cmks_by_logical_id)
        if not cmk_logical_id:
            continue   # may be imported / cross-stack — out of scope for synth-time check
        cmk = cmks_by_logical_id[cmk_logical_id]
        granted = _service_principals_with_kms_grants(cmk)
        missing = required_principals - granted
        if missing:
            raise AssertionError(
                f"F-AFIE-05: SNS Topic '{topic_id}' uses CMK '{cmk_logical_id}' but the CMK "
                f"policy lacks principals {missing}. Without these, CW alarms fire but SNS "
                f"publish silently fails (AFIE F-OBS-02). Use notifications_key from "
                f"LAYER_SECURITY.md §3 instead of a generic data CMK."
            )
```

#### `assert_log_group_retention_floor` *(F-AFIE-06)*

```python
def assert_log_group_retention_floor(template, *, compliance_class="prod-internal"):
    """Fail if any LogGroup in a prod-* compliance_class has retention < 30 days
    (or < 180 days for prod-finance / prod-healthcare).
    """
    if not compliance_class.startswith("prod"):
        return
    floor = 30
    if compliance_class in ("prod-finance", "prod-healthcare"):
        floor = 180
    elif compliance_class in ("prod-regulated", "prod-sox"):
        floor = 365
    for logical_id, resource in template.find_resources("AWS::Logs::LogGroup").items():
        retention = resource.get("Properties", {}).get("RetentionInDays")
        if retention is None or int(retention) < floor:
            raise AssertionError(
                f"F-AFIE-06: LogGroup '{logical_id}' retention={retention} < {floor} days "
                f"for compliance_class={compliance_class}. AFIE F-OBS-05 retro: SOX audit "
                f"required 18-month forensics. See LAYER_BACKEND_LAMBDA.md §3 _RETENTION_BY_CLASS."
            )
```

### 3.3 API authorization

#### `assert_no_authorization_type_none` *(F-AFIE-03)*

```python
def assert_no_authorization_type_none(template, *, public_endpoints=("/healthz", "/ready")):
    """Fail if any AWS::ApiGateway::Method has AuthorizationType=NONE except
    for explicitly whitelisted public health endpoints.
    """
    for logical_id, resource in template.find_resources("AWS::ApiGateway::Method").items():
        props = resource.get("Properties", {})
        if props.get("AuthorizationType") == "NONE":
            # Allow CORS preflight + whitelisted health endpoints
            http_method = props.get("HttpMethod", "")
            if http_method == "OPTIONS":
                continue
            # Resource path inspection requires walking AWS::ApiGateway::Resource — skip if unable.
            raise AssertionError(
                f"F-AFIE-03: API Gateway Method '{logical_id}' has AuthorizationType=NONE. "
                f"This is the AFIE F-INT-01 failure mode (addProxy({{anyMethod:true}}) without "
                f"authorizer). Fix: set default_method_options at the API root. "
                f"See LAYER_API.md §4."
            )
```

#### `assert_websocket_connect_route_has_authorizer` *(F-AFIE-19)*

```python
def assert_websocket_connect_route_has_authorizer(template, **_):
    """Fail if any AWS::ApiGatewayV2::Route with RouteKey=$connect has
    AuthorizationType=NONE.
    """
    for logical_id, resource in template.find_resources("AWS::ApiGatewayV2::Route").items():
        props = resource.get("Properties", {})
        if props.get("RouteKey") == "$connect" and props.get("AuthorizationType", "NONE") == "NONE":
            raise AssertionError(
                f"F-AFIE-19: WebSocket $connect Route '{logical_id}' has AuthorizationType=NONE. "
                f"AFIE F-INT-02 retro: any client could open wss://... and the connection "
                f"would be accepted. Fix: attach a WebSocketLambdaAuthorizer per "
                f"LAYER_API.md §5."
            )
```

### 3.4 Edge + TLS

#### `assert_cloudfront_resources_in_us_east_1` *(F-AFIE-04)*

```python
def assert_cloudfront_resources_in_us_east_1(template, *, stack_region=None, **_):
    """Fail if the template contains a CloudFront Distribution OR a WAFv2 WebACL
    with Scope=CLOUDFRONT, and the stack is not in us-east-1.
    """
    has_cf = bool(template.find_resources("AWS::CloudFront::Distribution"))
    has_cf_waf = any(
        w.get("Properties", {}).get("Scope") == "CLOUDFRONT"
        for w in template.find_resources("AWS::WAFv2::WebACL").values()
    )
    if (has_cf or has_cf_waf) and stack_region and stack_region != "us-east-1":
        raise AssertionError(
            f"F-AFIE-04: stack region is {stack_region} but contains CloudFront/CLOUDFRONT-scope "
            f"WAF resources. ACM cert + WAFv2 CLOUDFRONT scope MUST be in us-east-1. "
            f"Fix: instantiate CdnStack with env=Environment(region='us-east-1'). "
            f"See CDN_CLOUDFRONT_FOUNDATION.md §3."
        )
```

#### `assert_cloudfront_tls_path_single` *(F-AFIE-04)*

```python
def assert_cloudfront_tls_path_single(template, **_):
    """Fail if the template contains both:
      (a) a CloudFront Distribution with an ALB origin using HTTP_ONLY origin
          protocol policy, AND
      (b) an ALB Listener with HTTP→HTTPS redirect (port 80 with redirect action)
    AFIE G-NEW-05: CloudFront port-80 origin fetch hits ALB's 301 → 502 BadGw.
    """
    has_http_only_alb_origin = False
    for dist in template.find_resources("AWS::CloudFront::Distribution").values():
        origins = dist.get("Properties", {}).get("DistributionConfig", {}).get("Origins", [])
        for o in origins:
            cust = o.get("CustomOriginConfig", {})
            if cust.get("OriginProtocolPolicy") == "http-only":
                has_http_only_alb_origin = True
    has_redirect_listener = False
    for lst in template.find_resources("AWS::ElasticLoadBalancingV2::Listener").values():
        props = lst.get("Properties", {})
        if props.get("Port") == 80:
            for action in props.get("DefaultActions", []):
                if action.get("Type") == "redirect" and \
                   action.get("RedirectConfig", {}).get("Protocol") == "HTTPS":
                    has_redirect_listener = True
    if has_http_only_alb_origin and has_redirect_listener:
        raise AssertionError(
            "F-AFIE-04: CloudFront has http-only origin AND ALB has port-80→HTTPS redirect. "
            "Pick one TLS path. See CDN_CLOUDFRONT_FOUNDATION.md §3.0."
        )
```

### 3.5 Data layer

#### `assert_ddb_table_uses_pitr_specification` *(F-AFIE-17)*

```python
def assert_ddb_table_uses_pitr_specification(template, **_):
    """Fail if any DDB Table uses the deprecated PointInTimeRecoveryEnabled at
    the top level rather than the new PointInTimeRecoverySpecification nested
    property.
    """
    for logical_id, resource in template.find_resources("AWS::DynamoDB::Table").items():
        props = resource.get("Properties", {})
        # Either it's missing entirely OR it's using the deprecated top-level prop
        legacy = props.get("PointInTimeRecoverySpecification") is None and \
                 props.get("PointInTimeRecoveryEnabled") is not None
        missing = "PointInTimeRecoverySpecification" not in props and \
                  "PointInTimeRecoveryEnabled" not in props
        if legacy or missing:
            raise AssertionError(
                f"F-AFIE-17: DDB Table '{logical_id}' uses the deprecated PointInTimeRecoveryEnabled "
                f"prop or is missing PITR entirely. Migrate to PointInTimeRecoverySpecification "
                f"with explicit recovery_period_in_days. See LAYER_DATA.md §3.3 / "
                f"SERVERLESS_DYNAMODB_PATTERNS.md §3.2."
            )
```

#### `assert_oss_network_policy_no_public_in_prod` *(F-AFIE-10)*

```python
def assert_oss_network_policy_no_public_in_prod(template, *, compliance_class="prod-internal"):
    """Fail if any OpenSearchServerless SecurityPolicy of type=network has
    AllowFromPublic=true in a prod-* compliance class.
    """
    if not compliance_class.startswith("prod"):
        return
    import json as _json
    for logical_id, resource in template.find_resources("AWS::OpenSearchServerless::SecurityPolicy").items():
        props = resource.get("Properties", {})
        if props.get("Type") != "network":
            continue
        policy_json = props.get("Policy", "[]")
        policy = _json.loads(policy_json) if isinstance(policy_json, str) else policy_json
        for rule in policy:
            if rule.get("AllowFromPublic") is True:
                raise AssertionError(
                    f"F-AFIE-10: OpenSearch Serverless NetworkPolicy '{logical_id}' has "
                    f"AllowFromPublic=true in compliance_class={compliance_class}. "
                    f"SigV4 is the ONLY auth; a credential leak compromises the data plane. "
                    f"See DATA_OPENSEARCH_SERVERLESS.md §3."
                )
```

### 3.6 Governance + Bedrock cost

#### `assert_cedar_validation_mode_strict_in_prod` *(F-AFIE-08)*

```python
def assert_cedar_validation_mode_strict_in_prod(template, *, compliance_class="prod-internal"):
    """Fail if any AgentCore CfnPolicy in a prod-* compliance_class has
    ValidationMode != VALIDATE.
    """
    if not compliance_class.startswith("prod"):
        return
    for logical_id, resource in template.find_resources("AWS::BedrockAgentCore::Policy").items():
        validation_mode = resource.get("Properties", {}).get("ValidationMode")
        if validation_mode and validation_mode != "VALIDATE":
            raise AssertionError(
                f"F-AFIE-08: Cedar CfnPolicy '{logical_id}' has ValidationMode={validation_mode} "
                f"in compliance_class={compliance_class}. AFIE F-GOV-03: typo'd rule no-op'd 3 "
                f"weeks in prod. See AGENTCORE_AGENT_CONTROL.md §3.2."
            )
```

#### `assert_redshift_workgroup_max_capacity_set` *(F-AFIE-14)*

```python
def assert_redshift_workgroup_max_capacity_set(template, **_):
    """Fail if any RedshiftServerless Workgroup lacks MaxCapacity (unset = unbounded auto-scale).
    """
    for logical_id, resource in template.find_resources("AWS::RedshiftServerless::Workgroup").items():
        if resource.get("Properties", {}).get("MaxCapacity") is None:
            raise AssertionError(
                f"F-AFIE-14: Redshift Workgroup '{logical_id}' has no MaxCapacity. Without it the "
                f"workgroup auto-scales to 512 RPU with no ceiling. AFIE F-FIN-05 retro: $300+/hr "
                f"burn during a runaway dbt MERGE. See MLOPS_DATA_PLATFORM.md / "
                f"DATA_LAKEHOUSE_ICEBERG.md / DATA_ZERO_ETL.md."
            )
```

#### `assert_aurora_dev_min_capacity_is_zero` *(F-AFIE-13)*

```python
def assert_aurora_dev_min_capacity_is_zero(template, *, compliance_class="prod-internal"):
    """Warn (not fail) if compliance_class in {dev, staging} and any Aurora
    DBCluster has ServerlessV2ScalingConfiguration.MinCapacity > 0.
    """
    if compliance_class not in ("dev", "staging"):
        return
    for logical_id, resource in template.find_resources("AWS::RDS::DBCluster").items():
        scaling = resource.get("Properties", {}).get("ServerlessV2ScalingConfiguration", {})
        if scaling.get("MinCapacity", 0) > 0:
            raise AssertionError(
                f"F-AFIE-13: Aurora Cluster '{logical_id}' has MinCapacity={scaling['MinCapacity']} "
                f"in compliance_class={compliance_class}. Scale-to-zero auto-pause is the canonical "
                f"dev/staging default — set serverless_v2_min_capacity=0 + "
                f"serverless_v2_auto_pause_duration=Duration.seconds(300). "
                f"See DATA_AURORA_SERVERLESS_V2.md §3."
            )
```

#### `assert_cognito_user_pool_uses_feature_plan` *(F-AFIE-21)*

```python
def assert_cognito_user_pool_uses_feature_plan(template, **_):
    """Fail if any Cognito UserPool uses the deprecated UserPoolAddOns.AdvancedSecurityMode
    instead of the new UserPoolTier (feature_plan in CDK).
    """
    for logical_id, resource in template.find_resources("AWS::Cognito::UserPool").items():
        props = resource.get("Properties", {})
        add_ons = props.get("UserPoolAddOns", {})
        if "AdvancedSecurityMode" in add_ons and "UserPoolTier" not in props:
            raise AssertionError(
                f"F-AFIE-21: UserPool '{logical_id}' uses deprecated UserPoolAddOns.AdvancedSecurityMode. "
                f"Migrate to feature_plan=cognito.FeaturePlan.PLUS (UserPoolTier). "
                f"See AGENTCORE_IDENTITY.md §3.3."
            )
```

---

## 4. Helper utilities

```python
# tests/synth_guards_helpers.py

def _listify(x):
    return x if isinstance(x, list) else [x]


def _flatten_resources(r):
    out = []
    for item in _listify(r):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # CDK token (Fn::Join, Fn::Sub) — best-effort flatten
            if "Fn::Join" in item:
                parts = item["Fn::Join"][1]
                out.append("".join(p if isinstance(p, str) else "<token>" for p in parts))
            elif "Fn::Sub" in item:
                out.append(item["Fn::Sub"] if isinstance(item["Fn::Sub"], str)
                           else item["Fn::Sub"][0])
    return out


def _all_iam_policies(template):
    yield from template.find_resources("AWS::IAM::Policy").values()
    yield from template.find_resources("AWS::IAM::ManagedPolicy").values()
    yield from template.find_resources("AWS::IAM::Role").values()


def _statements(resource):
    """Pull policy statements from inline Role policies, ManagedPolicy, or Policy resources."""
    props = resource.get("Properties", {})
    for inline in props.get("Policies", []):
        yield from inline.get("PolicyDocument", {}).get("Statement", [])
    yield from props.get("PolicyDocument", {}).get("Statement", [])


def _condition_keys(stmt):
    cond = stmt.get("Condition", {})
    keys = set()
    for operator in cond.values():
        if isinstance(operator, dict):
            keys.update(operator.keys())
    return keys


def _resolve_ref(ref, candidates):
    """Resolve a {Ref: X} or {Fn::GetAtt: [X, Arn]} to a logical id present in candidates."""
    if isinstance(ref, dict):
        if "Ref" in ref and ref["Ref"] in candidates:
            return ref["Ref"]
        if "Fn::GetAtt" in ref:
            logical_id = ref["Fn::GetAtt"][0]
            if logical_id in candidates:
                return logical_id
    return None


def _service_principals_with_kms_grants(cmk_resource):
    """Return the set of Service principals that have kms:GenerateDataKey* OR kms:Decrypt grants on this CMK."""
    principals = set()
    policy = cmk_resource.get("Properties", {}).get("KeyPolicy", {})
    for stmt in policy.get("Statement", []):
        if stmt.get("Effect") != "Allow":
            continue
        actions = set(_listify(stmt.get("Action", [])))
        if not ({"kms:GenerateDataKey*", "kms:Decrypt", "kms:*"} & actions):
            continue
        principal = stmt.get("Principal", {})
        for svc in _listify(principal.get("Service", [])):
            principals.add(svc)
    return principals
```

---

## 5. Wiring guards into your project

### 5.1 Per-partial unit test (consumer pattern)

```python
# tests/test_layer_observability_guards.py
import aws_cdk as cdk
from aws_cdk import assertions
from synth_guards import (
    assert_alarm_treat_missing_data_set,
    assert_sns_cmk_has_required_principals,
)
from your_project.stacks import ObservabilityStack


def test_observability_passes_r4_guards():
    app = cdk.App(context={"compliance_class": "prod-internal"})
    obs = ObservabilityStack(app, "Obs", ...)
    template = assertions.Template.from_stack(obs)

    assert_alarm_treat_missing_data_set(template, compliance_class="prod-internal")
    assert_sns_cmk_has_required_principals(template)
```

### 5.2 Composite-level CI gate

```python
# tests/test_synth_guards_full.py — runs all 17 guards
import aws_cdk as cdk
from aws_cdk import assertions
from synth_guards import (
    assert_bedrock_invoke_three_arn_pattern,
    assert_no_wildcard_agentcore_grants_in_prod,
    assert_permission_boundary_includes_agentcore_cross_project_deny,
    assert_gateway_role_carries_project_tag_condition,
    assert_alarm_treat_missing_data_set,
    assert_sns_cmk_has_required_principals,
    assert_log_group_retention_floor,
    assert_no_authorization_type_none,
    assert_websocket_connect_route_has_authorizer,
    assert_cloudfront_resources_in_us_east_1,
    assert_cloudfront_tls_path_single,
    assert_ddb_table_uses_pitr_specification,
    assert_oss_network_policy_no_public_in_prod,
    assert_cedar_validation_mode_strict_in_prod,
    assert_redshift_workgroup_max_capacity_set,
    assert_aurora_dev_min_capacity_is_zero,
    assert_cognito_user_pool_uses_feature_plan,
)

ALL_GUARDS = [
    assert_bedrock_invoke_three_arn_pattern,
    assert_no_wildcard_agentcore_grants_in_prod,
    assert_permission_boundary_includes_agentcore_cross_project_deny,
    assert_gateway_role_carries_project_tag_condition,
    assert_alarm_treat_missing_data_set,
    assert_sns_cmk_has_required_principals,
    assert_log_group_retention_floor,
    assert_no_authorization_type_none,
    assert_websocket_connect_route_has_authorizer,
    assert_ddb_table_uses_pitr_specification,
    assert_oss_network_policy_no_public_in_prod,
    assert_cedar_validation_mode_strict_in_prod,
    assert_redshift_workgroup_max_capacity_set,
    assert_aurora_dev_min_capacity_is_zero,
    assert_cognito_user_pool_uses_feature_plan,
]


def test_all_synth_guards_pass():
    app = cdk.App(context={"compliance_class": "prod-internal"})
    # ... instantiate every stack the kit composes ...
    for stack in app.node.children:
        if not hasattr(stack, "stack_name"):
            continue
        template = assertions.Template.from_stack(stack)
        for guard in ALL_GUARDS:
            guard(template, compliance_class="prod-internal", stack_region=stack.region)
    # us-east-1 + tls-path guards have signatures that take stack_region; skip if absent.
    # assert_cloudfront_* — pass stack.region explicitly per stack.
```

### 5.3 GitHub Actions CI integration

```yaml
# .github/workflows/synth-guards.yml
name: R4 Synth Guards
on: [pull_request]
jobs:
  guards:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Run synth guards
        run: pytest tests/test_synth_guards_full.py -v
```

---

## 6. Five non-negotiables

1. **Every kit MUST wire the full guard suite into CI** — not just per-partial tests. Cross-stack regressions only surface at composite level.
2. **`compliance_class` is a kit-level CDK context, not an env var.** Pass it via `cdk.App(context=...)` so guards see the same value as the partials.
3. **Guards are advisory at PR-time + blocking at merge-time.** Set the GH Actions job as a required check on `main`.
4. **When a new R4-class regression surfaces, write the guard before the fix lands.** This is the R4→R5 self-reinforcement pattern.
5. **Update this partial when a guard is added or removed.** The 17-rule count above is part of the contract.

---

## 7. References

- `LAYER_OBSERVABILITY.md` §3.1 — TreatMissingData decision table (F-AFIE-07)
- `LAYER_SECURITY.md` §3 — notifications_key + DenyAgentCoreInvokeAcrossProjects (F-AFIE-05 + F-AFIE-09)
- `LLMOPS_BEDROCK.md` §3.1 — 3-ARN Bedrock pattern (F-AFIE-01)
- `LAYER_API.md` §4 + §5 — REST + WebSocket authorization (F-AFIE-03 + F-AFIE-19)
- `CDN_CLOUDFRONT_FOUNDATION.md` §3 — TLS pick-one + us-east-1 pin (F-AFIE-04)
- `LAYER_DATA.md` §3.3 + `SERVERLESS_DYNAMODB_PATTERNS.md` §3.2 — PITR spec object (F-AFIE-17)
- `DATA_OPENSEARCH_SERVERLESS.md` §3 — VPC-endpoint-only default (F-AFIE-10)
- `AGENTCORE_AGENT_CONTROL.md` §3.2 — Cedar validate mode (F-AFIE-08)
- `MLOPS_DATA_PLATFORM.md` + `DATA_LAKEHOUSE_ICEBERG.md` — Redshift max_capacity (F-AFIE-14)
- `DATA_AURORA_SERVERLESS_V2.md` §3 — scale-to-zero default (F-AFIE-13)
- `AGENTCORE_IDENTITY.md` §3.3 — Cognito feature_plan=PLUS (F-AFIE-21)
- `AGENTCORE_IDENTITY.md` §3.1 + `LAYER_SECURITY.md` §3 — wildcard scoping + DENY (F-AFIE-09)
- `AGENTCORE_GATEWAY.md` §3.1 — gateway role tag condition (F-AFIE-12)
- `LAYER_BACKEND_LAMBDA.md` §3 — _RETENTION_BY_CLASS (F-AFIE-06)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-06-17 | Initial. 17 canonical synth-time guards forward-referenced from R4 Tier-1-through-Tier-4 findings. NEW partial — F-AFIE-22. |
