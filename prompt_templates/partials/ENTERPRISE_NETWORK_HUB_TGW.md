# SOP — Network Hub (Transit Gateway · Network Firewall · centralized egress · RAM share · VPC endpoints · DNS resolver)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · AWS Transit Gateway (TGW) · TGW route tables + propagation + association · Network Firewall · Centralized egress VPC + NAT · AWS RAM cross-account share · VPC Reachability Analyzer · Route 53 Resolver inbound/outbound endpoints · PrivateLink + interface endpoints

---

## 1. Purpose

- Codify the **hub-and-spoke network architecture** for multi-account AWS — Transit Gateway in Infrastructure account; spoke VPCs in workload accounts attach to it.
- Codify **TGW route table strategy**: separate isolated, shared, and inspection route tables; per-OU routing.
- Codify **AWS Network Firewall** centralized in Inspection VPC for L7 deep packet inspection of east-west and egress traffic.
- Codify **centralized egress** — single NAT Gateway in Egress VPC saves $$$ vs NAT-per-VPC.
- Codify **AWS RAM** sharing of TGW + private hosted zones across accounts.
- Codify **Route 53 Resolver** inbound/outbound endpoints for hybrid DNS (on-prem ↔ AWS).
- Codify **VPC interface endpoints** (PrivateLink) shared centrally to reduce per-VPC endpoint cost.
- This is the **enterprise network specialisation**. Built on `ENTERPRISE_CONTROL_TOWER` + `ENTERPRISE_ORG_SCPS_RCPS`. Deployed in Infrastructure account, shared via RAM.

When the SOW signals: "multi-VPC architecture", "centralized egress", "Network Firewall", "hybrid connectivity", "DX / VPN to AWS", "compliance requires inspect all egress".

---

## 2. Decision tree — TGW vs VPC Peering vs PrivateLink

| Need | TGW | VPC Peering | PrivateLink |
|---|:---:|:---:|:---:|
| 3+ VPCs need any-to-any | ✅ | ❌ scales O(N²) | ❌ |
| 2 VPCs, low cost, no transit | ⚠️ overkill | ✅ | ❌ |
| Expose service A → service B (one-way) | — | — | ✅ |
| Multi-account + cross-region | ✅ | ⚠️ pairwise mesh | ✅ |
| Centralized egress / inspection | ✅ | ❌ | ❌ |
| Per-tenant network isolation | ✅ (route tables) | ⚠️ pairwise | ✅ |

**Recommendation: TGW for any 3+ VPC architecture.** VPC Peering for tiny 2-VPC setups. PrivateLink for service exposure.

```
Hub-and-spoke topology:

  ┌─────────────────────────────────────────────────────┐
  │  Infrastructure account                             │
  │                                                      │
  │  ┌──────────────────┐   ┌────────────────────────┐  │
  │  │ Egress VPC       │   │ Inspection VPC         │  │
  │  │  - NAT GW        │   │  - Network Firewall    │  │
  │  │  - IGW           │   │  - VPC endpoint shared │  │
  │  └────────┬─────────┘   └──────────┬─────────────┘  │
  │           │                          │               │
  │           └──────────┬───────────────┘               │
  │                      ▼                                │
  │             ┌─────────────────┐                       │
  │             │ Transit Gateway │                       │
  │             └────────┬────────┘                       │
  │                      │  RAM share to org              │
  └──────────────────────┼────────────────────────────────┘
                         │
        ┌────────────────┼────────────────────┐
        ▼                ▼                    ▼
  ┌──────────┐    ┌──────────┐         ┌──────────┐
  │ Prod Acct│    │ Stage    │         │ Dev      │
  │ Spoke VPC│    │ Spoke VPC│         │ Spoke VPC│
  │ no NAT,  │    │ no NAT,  │         │ no NAT,  │
  │ no IGW   │    │ no IGW   │         │ no IGW   │
  └──────────┘    └──────────┘         └──────────┘
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — TGW + 2 spoke VPCs + centralized egress | **§3 Monolith** |
| Production — TGW + Network Firewall + multi-region + R53 Resolver + DX | **§5 Production** |

---

## 3. Monolith Variant — TGW + centralized egress + RAM share

### 3.1 CDK in Infrastructure account

```python
# stacks/network_hub_stack.py
from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ram as ram
from aws_cdk import aws_route53 as r53
from constructs import Construct


class NetworkHubStack(Stack):
    """Deployed in Infrastructure account."""

    def __init__(self, scope: Construct, id: str, *,
                 org_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── 1. Transit Gateway ────────────────────────────────────────
        self.tgw = ec2.CfnTransitGateway(self, "Tgw",
            description="Org-wide TGW",
            amazon_side_asn=64512,                          # private ASN
            auto_accept_shared_attachments="enable",        # auto-accept spokes
            default_route_table_association="disable",       # we manage tables explicitly
            default_route_table_propagation="disable",
            dns_support="enable",
            multicast_support="disable",
            vpn_ecmp_support="enable",
            tags=[{"key": "Name", "value": "org-tgw"}],
        )

        # ── 2. TGW Route Tables (segmented routing) ───────────────────
        spoke_rt = ec2.CfnTransitGatewayRouteTable(self, "SpokeRT",
            transit_gateway_id=self.tgw.ref,
            tags=[{"key": "Name", "value": "spoke-rt"}],
        )

        egress_rt = ec2.CfnTransitGatewayRouteTable(self, "EgressRT",
            transit_gateway_id=self.tgw.ref,
            tags=[{"key": "Name", "value": "egress-rt"}],
        )

        inspection_rt = ec2.CfnTransitGatewayRouteTable(self, "InspectionRT",
            transit_gateway_id=self.tgw.ref,
            tags=[{"key": "Name", "value": "inspection-rt"}],
        )

        # ── 3. Egress VPC (single NAT for entire org) ─────────────────
        egress_vpc = ec2.Vpc(self, "EgressVpc",
            ip_addresses=ec2.IpAddresses.cidr("100.64.0.0/22"),
            max_azs=3,
            nat_gateways=3,                                   # 1 per AZ for HA
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=26,
                ),
                ec2.SubnetConfiguration(
                    name="tgw-attach", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=28,
                ),
            ],
        )

        # Attach Egress VPC to TGW
        egress_attachment = ec2.CfnTransitGatewayAttachment(self, "EgressAttach",
            transit_gateway_id=self.tgw.ref,
            vpc_id=egress_vpc.vpc_id,
            subnet_ids=[s.subnet_id for s in egress_vpc.isolated_subnets],
            tags=[{"key": "Name", "value": "egress-vpc"}],
        )

        # Associate to Egress RT
        ec2.CfnTransitGatewayRouteTableAssociation(self, "EgressAssoc",
            transit_gateway_attachment_id=egress_attachment.attr_id,
            transit_gateway_route_table_id=egress_rt.attr_id,
        )

        # In Egress VPC, route private subnet 0.0.0.0/0 → NAT GW (auto via CDK)
        # Route TGW-attach subnet 0.0.0.0/0 → NAT (so TGW return traffic exits via NAT)

        # Add default route in Spoke RT pointing 0.0.0.0/0 → Egress attachment
        ec2.CfnTransitGatewayRoute(self, "SpokeDefaultRoute",
            transit_gateway_route_table_id=spoke_rt.attr_id,
            destination_cidr_block="0.0.0.0/0",
            transit_gateway_attachment_id=egress_attachment.attr_id,
        )

        # ── 4. Share TGW with the org via RAM ─────────────────────────
        ram.CfnResourceShare(self, "TgwShare",
            name="org-tgw-share",
            resource_arns=[
                f"arn:aws:ec2:{self.region}:{self.account}:transit-gateway/{self.tgw.ref}",
            ],
            principals=[org_arn],                       # whole org
            allow_external_principals=False,
            tags=[{"key": "Purpose", "value": "TGW share"}],
        )

        # ── 5. Centralized PrivateLink endpoints (S3 / DDB gateway free; others $$) ──
        # Interface endpoints created here, shared via RAM
        for service in ["secretsmanager", "kms", "ssm", "logs", "monitoring",
                        "sts", "ec2", "ecr.api", "ecr.dkr", "sqs", "sns"]:
            ec2.InterfaceVpcEndpoint(self, f"VpcE{service.replace('.', '_')}",
                vpc=egress_vpc,
                service=ec2.InterfaceVpcEndpointAwsService(service),
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                private_dns_enabled=True,
                # Restrict via endpoint policy
            )

        # ── 6. Centralized Route 53 Resolver inbound + outbound ──────
        # Inbound — on-prem can resolve AWS PHZs
        inbound_endpoint = r53.CfnResolverEndpoint(self, "ResolverInbound",
            direction="INBOUND",
            ip_addresses=[
                {"subnetId": egress_vpc.isolated_subnets[i].subnet_id}
                for i in range(2)
            ],
            security_group_ids=[resolver_sg.security_group_id],
        )

        # Outbound — AWS resolves on-prem zones
        outbound_endpoint = r53.CfnResolverEndpoint(self, "ResolverOutbound",
            direction="OUTBOUND",
            ip_addresses=[
                {"subnetId": egress_vpc.isolated_subnets[i].subnet_id}
                for i in range(2)
            ],
            security_group_ids=[resolver_sg.security_group_id],
        )

        # Forwarding rule for on-prem corp.example.com → on-prem DNS
        r53.CfnResolverRule(self, "OnpremForward",
            domain_name="corp.example.com",
            rule_type="FORWARD",
            resolver_endpoint_id=outbound_endpoint.attr_resolver_endpoint_id,
            target_ips=[{"ip": "10.99.1.10", "port": 53},
                        {"ip": "10.99.2.10", "port": 53}],
        )
        # Then share the rule via RAM so spokes can attach to their VPCs
```

### 3.2 Spoke VPC stack (runs in workload account)

```python
# stacks/spoke_vpc_stack.py
class SpokeVpcStack(Stack):
    def __init__(self, scope, id, *,
                 cidr: str, tgw_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Spoke VPC — NO IGW, NO NAT (egress via TGW → centralized NAT)
        vpc = ec2.Vpc(self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(cidr),
            max_azs=3,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=22,
                ),
                ec2.SubnetConfiguration(
                    name="tgw-attach", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=28,
                ),
            ],
        )

        # Attach to shared TGW
        tgw_attach = ec2.CfnTransitGatewayAttachment(self, "TgwAttach",
            transit_gateway_id=tgw_id,
            vpc_id=vpc.vpc_id,
            subnet_ids=[s.subnet_id for s in vpc.isolated_subnets[-3:]],
        )

        # Add 0.0.0.0/0 → TGW route in private subnet route tables
        for i, subnet in enumerate(vpc.isolated_subnets[:3]):
            ec2.CfnRoute(self, f"DefaultRoute{i}",
                route_table_id=subnet.route_table.route_table_id,
                destination_cidr_block="0.0.0.0/0",
                transit_gateway_id=tgw_id,
            )
```

---

## 4. Network Firewall in Inspection VPC

Inspection VPC sits between TGW and Egress VPC; all egress traffic flows TGW → Inspection VPC → NAT → IGW.

```python
from aws_cdk import aws_networkfirewall as nfw

# Stateful rule group — block known-bad domains
domain_block_rg = nfw.CfnRuleGroup(self, "DomainBlock",
    capacity=100,
    rule_group_name="block-known-bad-domains",
    type="STATEFUL",
    rule_group=nfw.CfnRuleGroup.RuleGroupProperty(
        rules_source=nfw.CfnRuleGroup.RulesSourceProperty(
            rules_source_list=nfw.CfnRuleGroup.RulesSourceListProperty(
                generated_rules_type="DENYLIST",
                target_types=["TLS_SNI", "HTTP_HOST"],
                targets=[
                    "*.tor.com", "*.cryptominer.example",
                    "raw.githubusercontent.com",     # block direct script pulls
                ],
            ),
        ),
    ),
)

# Firewall policy
firewall_policy = nfw.CfnFirewallPolicy(self, "FwPolicy",
    firewall_policy_name="org-policy",
    firewall_policy=nfw.CfnFirewallPolicy.FirewallPolicyProperty(
        stateless_default_actions=["aws:forward_to_sfe"],
        stateless_fragment_default_actions=["aws:forward_to_sfe"],
        stateful_rule_group_references=[
            nfw.CfnFirewallPolicy.StatefulRuleGroupReferenceProperty(
                resource_arn=domain_block_rg.attr_rule_group_arn,
            ),
        ],
        stateful_default_actions=["aws:drop_strict", "aws:alert_strict"],
    ),
)

# Firewall — deployed in Inspection VPC subnets
firewall = nfw.CfnFirewall(self, "Firewall",
    firewall_name="org-firewall",
    firewall_policy_arn=firewall_policy.attr_firewall_policy_arn,
    vpc_id=inspection_vpc.vpc_id,
    subnet_mappings=[
        nfw.CfnFirewall.SubnetMappingProperty(subnet_id=s.subnet_id)
        for s in inspection_vpc.isolated_subnets
    ],
    delete_protection=True,
    firewall_policy_change_protection=True,
    subnet_change_protection=True,
)
```

Routing — TGW Inspection RT directs spoke→0.0.0.0/0 traffic via Inspection VPC attachment; Inspection VPC routes through firewall endpoints to Egress.

---

## 5. Common gotchas

- **TGW attachments cost $0.05/hr each** ($36/mo). For tiny VPCs, count the cost.
- **TGW data transfer = $0.02/GB** between attachments (in addition to inter-AZ if cross-AZ). Centralized NAT can be cheaper than NAT-per-VPC even with TGW data transfer cost.
- **TGW route tables don't propagate by default** with `default_route_table_association/propagation: disable` (recommended). You must explicitly create associations + propagations.
- **Spoke VPC needs `RAM share accepted` before TGW attachment can be created** in the spoke account. Auto-accept (`auto_accept_shared_attachments: enable`) helps.
- **Network Firewall is expensive** (~$1.50/hr per AZ + data processing). Use only in regulated environments.
- **R53 Resolver outbound endpoint forwarding rules must be associated to VPCs.** Just creating the rule isn't enough — share via RAM, then associate in spoke account.
- **Route 53 PrivateLink resolver charges $0.40/hr per ENI** — 2 inbound + 2 outbound = $1.60/hr × 730 = ~$1,170/mo. Plan for it.
- **VPC interface endpoints in Egress/Hub VPC require `private_dns_enabled: true` in the spoke** to resolve AWS service hostnames. Without that, spoke calls hit public AWS endpoints.
- **TGW max bandwidth per VPC attachment: 50 Gbps** (24× ENIs × 2 Gbps). For higher, spread across multiple attachments.
- **Don't use BGP peering between TGW and on-prem DX/VPN with same ASN** as your spokes — causes loop. Use a separate ASN (64513 etc).
- **VPC Reachability Analyzer is the canonical debug tool** for "why can't A reach B over TGW?"
- **TGW peering between regions** is supported but routes must be explicit (no propagation across peers).

---

## 6. Pytest worked example

```python
# tests/test_network_hub.py
import boto3, pytest

ec2 = boto3.client("ec2", region_name="us-east-1")
ram = boto3.client("ram", region_name="us-east-1")


def test_tgw_exists():
    tgws = ec2.describe_transit_gateways()["TransitGateways"]
    assert any(t["State"] == "available" for t in tgws)


def test_tgw_route_tables_three(): 
    tgws = ec2.describe_transit_gateways()["TransitGateways"]
    tgw_id = tgws[0]["TransitGatewayId"]
    rts = ec2.describe_transit_gateway_route_tables(
        Filters=[{"Name": "transit-gateway-id", "Values": [tgw_id]}],
    )["TransitGatewayRouteTables"]
    names = {next((t["Value"] for t in rt.get("Tags", []) if t["Key"] == "Name"), None)
             for rt in rts}
    assert {"spoke-rt", "egress-rt", "inspection-rt"}.issubset(names)


def test_tgw_shared_with_org():
    shares = ram.get_resource_shares(resourceOwner="SELF")["resourceShares"]
    tgw_share = [s for s in shares if "org-tgw" in s["name"].lower()]
    assert tgw_share, "No TGW resource share"
    associations = ram.get_resource_share_associations(
        associationType="PRINCIPAL",
        resourceShareArns=[tgw_share[0]["resourceShareArn"]],
    )["resourceShareAssociations"]
    org_arns = [a["associatedEntity"] for a in associations
                if "organizations" in a["associatedEntity"]]
    assert org_arns, "TGW not shared with org"


def test_egress_vpc_has_nat_gateway():
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": ["EgressVpc"]}])["Vpcs"]
    assert vpcs
    nats = ec2.describe_nat_gateways(Filters=[
        {"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]},
        {"Name": "state", "Values": ["available"]},
    ])["NatGateways"]
    assert len(nats) >= 3, f"Expected ≥3 NAT GWs (1/AZ), got {len(nats)}"
```

---

## 7. Five non-negotiables

1. **TGW with explicit route tables** (no default association / propagation).
2. **Centralized egress** — NO NAT Gateway in spoke VPCs; egress via TGW → Egress VPC.
3. **TGW shared via RAM to whole org** — `auto_accept_shared_attachments: enable`.
4. **Network Firewall in Inspection VPC** for prod / regulated environments.
5. **Centralized VPC interface endpoints** for `secretsmanager`, `kms`, `ssm`, `logs`, `sts`, `ec2`, `ecr.*` — saves ~$50/mo per spoke per endpoint.

---

## 8. References

- [AWS Transit Gateway User Guide](https://docs.aws.amazon.com/vpc/latest/tgw/what-is-transit-gateway.html)
- [Centralized egress with TGW + NAT](https://docs.aws.amazon.com/whitepapers/latest/building-scalable-secure-multi-vpc-network-infrastructure/centralized-egress-to-internet.html)
- [AWS Network Firewall](https://docs.aws.amazon.com/network-firewall/latest/developerguide/what-is-aws-network-firewall.html)
- [AWS RAM — sharing TGW](https://docs.aws.amazon.com/vpc/latest/tgw/tgw-transit-gateways.html#tgw-sharing)
- [Hybrid DNS with R53 Resolver](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver.html)
- [VPC Reachability Analyzer](https://docs.aws.amazon.com/vpc/latest/reachability/what-is-reachability-analyzer.html)

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial. TGW hub + Egress VPC + Inspection VPC w/ Network Firewall + RAM share + R53 Resolver + centralized PrivateLink. Wave 11. |
