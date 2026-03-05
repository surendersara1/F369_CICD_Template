# PARTIAL: Networking Layer CDK Constructs

**Usage:** Referenced by `02A_APP_STACK_GENERATOR.md` for the `_create_networking()` method body.

---

## CDK Code Block — Networking Layer

```python
def _create_networking(self, stage_name: str) -> None:
    """
    Layer 0: VPC, Subnets, Security Groups, VPC Endpoints

    Subnet design:
      PUBLIC   — NAT Gateways, Application Load Balancers
      PRIVATE  — Lambda functions, ECS Fargate tasks (egress via NAT)
      ISOLATED — Aurora, ElastiCache (NO internet path, ever)

    VPC Endpoints ensure AWS service traffic stays inside the AWS network
    (no internet path for S3, DynamoDB, Secrets Manager, SSM, STS)
    """

    # =========================================================================
    # VPC — Multi-AZ with 3 subnet tiers
    # =========================================================================
    az_count = 2 if stage_name == "dev" else 3

    self.vpc = ec2.Vpc(
        self, "VPC",
        vpc_name=f"{{project_name}}-vpc-{stage_name}",

        # IP address space
        ip_addresses=ec2.IpAddresses.cidr(
            "10.10.0.0/16" if stage_name == "prod" else "10.20.0.0/16"
        ),

        # Availability zones
        max_azs=az_count,

        # Subnet configuration
        subnet_configuration=[
            ec2.SubnetConfiguration(
                name="Public",
                subnet_type=ec2.SubnetType.PUBLIC,
                cidr_mask=24,
            ),
            ec2.SubnetConfiguration(
                name="Private",
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                cidr_mask=24,
            ),
            ec2.SubnetConfiguration(
                name="Isolated",
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                cidr_mask=24,
            ),
        ],

        # NAT Gateways: 1 per AZ in prod, 1 total in dev (cost optimization)
        nat_gateways=az_count if stage_name == "prod" else 1,

        # Disable IPv6 (simplify security groups unless needed)
        enable_dns_hostnames=True,
        enable_dns_support=True,

        # Flow logs (HIPAA/SOC2 compliance)
        flow_logs={
            "VPCFlowLogs": ec2.FlowLogOptions(
                destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                    log_group=logs.LogGroup(
                        self, "VPCFlowLogs",
                        log_group_name=f"/{{project_name}}/{stage_name}/vpc-flow-logs",
                        retention=logs.RetentionDays.ONE_MONTH if stage_name != "prod" else logs.RetentionDays.ONE_YEAR,
                        removal_policy=RemovalPolicy.DESTROY,
                    ),
                    iam_role=iam.Role(
                        self, "VPCFlowLogsRole",
                        assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
                        managed_policies=[
                            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchLogsFullAccess")
                        ],
                    ),
                ),
                traffic_type=ec2.FlowLogTrafficType.ALL,
            )
        },
    )

    # =========================================================================
    # VPC ENDPOINTS — Keep AWS service traffic off the internet
    # =========================================================================

    # Gateway endpoints (free)
    self.vpc.add_gateway_endpoint(
        "S3Endpoint",
        service=ec2.GatewayVpcEndpointAwsService.S3,
    )
    self.vpc.add_gateway_endpoint(
        "DynamoDBEndpoint",
        service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    )

    # Interface endpoints (paid, but required for VPC-isolated services)
    interface_endpoint_sg = ec2.SecurityGroup(
        self, "VPCEndpointSG",
        vpc=self.vpc,
        description="Security group for VPC Interface Endpoints",
        allow_all_outbound=False,
    )
    interface_endpoint_sg.add_ingress_rule(
        ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
        ec2.Port.tcp(443),
        "Allow HTTPS from within VPC",
    )

    interface_services = [
        ("SecretsManagerEndpoint", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
        ("SsmEndpoint", ec2.InterfaceVpcEndpointAwsService.SSM),
        ("SsmMessagesEndpoint", ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES),
        ("Ec2MessagesEndpoint", ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES),
        ("EcrApiEndpoint", ec2.InterfaceVpcEndpointAwsService.ECR),
        ("EcrDkrEndpoint", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
        ("CloudWatchEndpoint", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
        ("StsEndpoint", ec2.InterfaceVpcEndpointAwsService.STS),
    ]

    for endpoint_id, service in interface_services:
        self.vpc.add_interface_endpoint(
            endpoint_id,
            service=service,
            security_groups=[interface_endpoint_sg],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            private_dns_enabled=True,
        )

    # =========================================================================
    # SECURITY GROUPS
    # =========================================================================

    # Lambda Security Group
    self.lambda_sg = ec2.SecurityGroup(
        self, "LambdaSG",
        vpc=self.vpc,
        description="Security group for Lambda functions",
        allow_all_outbound=False,
        security_group_name=f"{{project_name}}-lambda-sg-{stage_name}",
    )
    # Lambda needs HTTPS outbound for AWS services and external APIs
    self.lambda_sg.add_egress_rule(
        ec2.Peer.any_ipv4(),
        ec2.Port.tcp(443),
        "Lambda HTTPS egress for AWS services and external APIs",
    )

    # ECS Security Group
    self.ecs_sg = ec2.SecurityGroup(
        self, "EcsSG",
        vpc=self.vpc,
        description="Security group for ECS Fargate tasks",
        allow_all_outbound=False,
        security_group_name=f"{{project_name}}-ecs-sg-{stage_name}",
    )
    self.ecs_sg.add_egress_rule(
        ec2.Peer.any_ipv4(),
        ec2.Port.tcp(443),
        "ECS HTTPS egress",
    )

    # Aurora Security Group
    self.aurora_sg = ec2.SecurityGroup(
        self, "AuroraSG",
        vpc=self.vpc,
        description="Security group for Aurora cluster — only Lambda/ECS access",
        allow_all_outbound=False,
        security_group_name=f"{{project_name}}-aurora-sg-{stage_name}",
    )
    self.aurora_sg.add_ingress_rule(
        self.lambda_sg,
        ec2.Port.tcp(5432),
        "Lambda to Aurora PostgreSQL",
    )
    self.aurora_sg.add_ingress_rule(
        self.ecs_sg,
        ec2.Port.tcp(5432),
        "ECS to Aurora PostgreSQL",
    )

    # Redis Security Group
    self.redis_sg = ec2.SecurityGroup(
        self, "RedisSG",
        vpc=self.vpc,
        description="Security group for ElastiCache Redis",
        allow_all_outbound=False,
        security_group_name=f"{{project_name}}-redis-sg-{stage_name}",
    )
    self.redis_sg.add_ingress_rule(
        self.lambda_sg,
        ec2.Port.tcp(6379),
        "Lambda to Redis",
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================

    CfnOutput(self, "VpcId",
        value=self.vpc.vpc_id,
        description="VPC ID",
        export_name=f"{{project_name}}-vpc-id-{stage_name}",
    )
    CfnOutput(self, "PrivateSubnetIds",
        value=",".join([s.subnet_id for s in self.vpc.private_subnets]),
        description="Private subnet IDs (for Lambda/ECS)",
        export_name=f"{{project_name}}-private-subnets-{stage_name}",
    )
    CfnOutput(self, "IsolatedSubnetIds",
        value=",".join([s.subnet_id for s in self.vpc.isolated_subnets]),
        description="Isolated subnet IDs (for Aurora/Redis)",
        export_name=f"{{project_name}}-isolated-subnets-{stage_name}",
    )
```
