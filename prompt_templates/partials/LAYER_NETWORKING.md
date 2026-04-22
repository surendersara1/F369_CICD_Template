# SOP — Networking Layer (VPC, Subnets, Endpoints, Security Groups)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+)

---

## 1. Purpose

Foundation networking for private, AWS-only workloads:

- VPC with public + private-with-egress + isolated subnets (optional)
- NAT gateway (single or per-AZ)
- Interface VPC endpoints for AWS services used privately
- Security groups scoped per workload tier
- VPC Flow Logs (Phase 2+)

---

## 2. Decision — Monolith vs Micro-Stack

Networking is the one layer where the split is **not architectural** — it's **ownership**. The VPC always lives in exactly one stack. Consumers receive it by interface (`ec2.IVpc`). Both variants produce the same CloudFormation shape; the difference is how downstream stacks read subnet selections.

| You are… | Use variant |
|---|---|
| One stack owns the VPC and also creates all Lambdas/RDS/ECS inside it | **§3 Monolith Variant** |
| A `NetworkingStack` creates the VPC and exports it via `self.vpc`, `self.lambda_sg`, `self.rds_sg` to other stacks | **§4 Micro-Stack Variant** |

No cross-stack cycle risk in this layer (VPC and SGs are always referenced, never mutated by consumers). Still — document the contract explicitly so downstream stacks consume consistently.

---

## 3. Monolith Variant

```python
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_logs as logs


def _create_networking(self, stage: str) -> None:
    self.vpc = ec2.Vpc(
        self, "Vpc",
        vpc_name=f"{{project_name}}-vpc-{stage}",
        ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
        max_azs=2,
        nat_gateways=1 if stage != "prod" else 2,
        subnet_configuration=[
            ec2.SubnetConfiguration(name="Public",   subnet_type=ec2.SubnetType.PUBLIC,               cidr_mask=24),
            ec2.SubnetConfiguration(name="Private",  subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,  cidr_mask=24),
            ec2.SubnetConfiguration(name="Isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,     cidr_mask=24),
        ],
    )

    self.lambda_sg = ec2.SecurityGroup(
        self, "LambdaSg", vpc=self.vpc,
        description="Lambda — egress only", allow_all_outbound=True,
    )
    self.rds_sg = ec2.SecurityGroup(
        self, "RdsSg", vpc=self.vpc,
        description="RDS — ingress from Lambda only", allow_all_outbound=False,
    )
    self.rds_sg.add_ingress_rule(
        peer=ec2.Peer.security_group_id(self.lambda_sg.security_group_id),
        connection=ec2.Port.tcp(5432),
    )

    self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
    for name, svc in [
        ("BedrockRuntime", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
        ("Transcribe",     ec2.InterfaceVpcEndpointAwsService.TRANSCRIBE),
        ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
        ("SSM",            ec2.InterfaceVpcEndpointAwsService.SSM),
        ("KMS",            ec2.InterfaceVpcEndpointAwsService.KMS),
        ("CloudWatchLogs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
        ("STS",            ec2.InterfaceVpcEndpointAwsService.STS),
    ]:
        self.vpc.add_interface_endpoint(
            f"{name}Endpoint", service=svc, private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

    if stage == "prod":
        flow_log_group = logs.LogGroup(
            self, "VpcFlowLogs",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        self.vpc.add_flow_log("FlowLogsCw",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )
```

### 3.1 Monolith gotchas

- `nat_gateways=0` is legal only if every outbound call is via VPC endpoint (rare).
- `max_azs=2` for POC; production should run 3.
- Setting `vpc_name=` plus a separate `Name` tag clashes — pick one.

---

## 4. Micro-Stack Variant

### 4.1 `NetworkingStack`

```python
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class NetworkingStack(cdk.Stack):
    """Owns the VPC, SGs, and endpoints. Nothing mutates these downstream."""

    def __init__(self, scope: Construct, **kwargs) -> None:
        super().__init__(scope, "{project_name}-networking", **kwargs)

        self.vpc = ec2.Vpc(
            self, "Vpc",
            vpc_name="{project_name}-vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
            max_azs=2, nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public",   subnet_type=ec2.SubnetType.PUBLIC,               cidr_mask=24),
                ec2.SubnetConfiguration(name="Private",  subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,  cidr_mask=24),
                ec2.SubnetConfiguration(name="Isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,     cidr_mask=24),
            ],
        )

        self.lambda_sg = ec2.SecurityGroup(self, "LambdaSg", vpc=self.vpc, allow_all_outbound=True)
        self.rds_sg    = ec2.SecurityGroup(self, "RdsSg",    vpc=self.vpc, allow_all_outbound=False)
        self.rds_sg.add_ingress_rule(
            peer=ec2.Peer.security_group_id(self.lambda_sg.security_group_id),
            connection=ec2.Port.tcp(5432),
        )

        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
        for name, svc in [
            ("BedrockRuntime", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("Transcribe",     ec2.InterfaceVpcEndpointAwsService.TRANSCRIBE),
            ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("SSM",            ec2.InterfaceVpcEndpointAwsService.SSM),
            ("KMS",            ec2.InterfaceVpcEndpointAwsService.KMS),
            ("CloudWatchLogs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
            ("STS",            ec2.InterfaceVpcEndpointAwsService.STS),
        ]:
            self.vpc.add_interface_endpoint(f"{name}Endpoint",
                service=svc, private_dns_enabled=True,
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            )

        cdk.CfnOutput(self, "VpcId",      value=self.vpc.vpc_id)
        cdk.CfnOutput(self, "LambdaSgId", value=self.lambda_sg.security_group_id)
        cdk.CfnOutput(self, "RdsSgId",    value=self.rds_sg.security_group_id)
```

### 4.2 Downstream consumer pattern

```python
class DatabaseStack(cdk.Stack):
    def __init__(self, scope, vpc: ec2.IVpc, rds_sg: ec2.ISecurityGroup, **kwargs):
        super().__init__(scope, "{project_name}-database", **kwargs)
        # Use vpc and rds_sg directly. NEVER mutate the VPC from here.
```

### 4.3 Micro-stack gotchas

- **`Vpc.from_lookup`** requires AWS context → breaks offline synth. Pass the VPC by reference from NetworkingStack; never look it up.
- **Adding endpoints downstream** mutates the VPC. Don't — put every endpoint in `NetworkingStack`.
- **Test helpers** that construct minimal VPCs with `ec2.Vpc(scope, "V")` get no isolated subnets by default. RDS in `PRIVATE_ISOLATED` will fail; declare isolated explicitly in the test VPC.

---

## 5. Swap matrix

| Trigger | Action |
|---|---|
| Share VPC across multiple apps (same account) | Micro-Stack; export IDs via `CfnOutput` |
| POC with < 5 resources outside VPC | Monolith |
| Future multi-account (TGW / PrivateLink) | Micro-Stack — a future `TransitGatewayStack` consumes `vpc.vpc_id` |

---

## 6. Worked example

```python
def test_networking_creates_endpoints():
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from infrastructure.cdk.stacks.networking_stack import NetworkingStack

    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    net = NetworkingStack(app, env=env)

    t = Template.from_stack(net)
    t.has_resource_properties("AWS::EC2::VPCEndpoint", {"VpcEndpointType": "Gateway"})
    t.has_resource_properties("AWS::EC2::VPCEndpoint", {"VpcEndpointType": "Interface"})
```

---

## 7. References

- `docs/template_params.md` — `VPC_CIDR`, `AZ_COUNT`, `NAT_STRATEGY`
- `docs/Feature_Roadmap.md` — N-00..N-24
- Related SOPs: `LAYER_SECURITY`, `LAYER_DATA`

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Dual-variant rewrite. Explicit isolated subnet. 7 canonical endpoints. Offline-synth guidance. |
| 1.0 | 2026-03-05 | Initial. |
