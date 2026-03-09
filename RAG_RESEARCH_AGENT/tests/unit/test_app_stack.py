"""
RAG Research Agent — Unit Tests for AppStack
Validates all 12 CDK layers synthesize correctly with expected resources,
security configurations, and environment-specific behavior.
"""
import json
import pytest
from aws_cdk.assertions import Template, Match, Capture


# =============================================================================
# LAYER 0: NETWORKING
# =============================================================================
class TestNetworking:
    def test_vpc_created(self, dev_template):
        dev_template.resource_count_is("AWS::EC2::VPC", 1)

    def test_vpc_flow_logs_enabled(self, dev_template):
        dev_template.has_resource_properties("AWS::EC2::FlowLog", {
            "TrafficType": "ALL",
        })

    def test_vpc_endpoints_created(self, dev_template):
        """S3 and DynamoDB gateway endpoints plus interface endpoints."""
        dev_template.resource_count_is("AWS::EC2::VPCEndpoint", Match.any_value())

    def test_security_groups_created(self, dev_template):
        dev_template.has_resource_properties("AWS::EC2::SecurityGroup", {
            "GroupDescription": Match.any_value(),
            "VpcId": Match.any_value(),
        })


# =============================================================================
# LAYER 1: SECURITY
# =============================================================================
class TestSecurity:
    def test_kms_keys_created(self, dev_template):
        dev_template.has_resource_properties("AWS::KMS::Key", {
            "EnableKeyRotation": True,
        })

    def test_cloudtrail_enabled(self, dev_template):
        dev_template.has_resource_properties("AWS::CloudTrail::Trail", {
            "EnableLogFileValidation": True,
            "IncludeGlobalServiceEvents": True,
        })

    def test_cloudtrail_bucket_encrypted(self, dev_template):
        dev_template.has_resource_properties("AWS::S3::Bucket", Match.object_like({
            "BucketEncryption": Match.object_like({
                "ServerSideEncryptionConfiguration": Match.any_value(),
            }),
        }))


# =============================================================================
# LAYER 2: DATA
# =============================================================================
class TestDataLayer:
    def test_documents_bucket_created(self, dev_template):
        dev_template.has_resource_properties("AWS::S3::Bucket", Match.object_like({
            "BucketEncryption": Match.any_value(),
            "VersioningConfiguration": {"Status": "Enabled"},
        }))

    def test_documents_bucket_blocks_public_access(self, dev_template):
        dev_template.has_resource_properties("AWS::S3::Bucket", Match.object_like({
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        }))

    def test_agent_session_table_created(self, dev_template):
        dev_template.has_resource_properties("AWS::DynamoDB::Table", Match.object_like({
            "KeySchema": Match.array_with([
                Match.object_like({"AttributeName": "session_id", "KeyType": "HASH"}),
                Match.object_like({"AttributeName": "turn_id", "KeyType": "RANGE"}),
            ]),
            "BillingMode": "PAY_PER_REQUEST",
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        }))

    def test_eval_results_table_created(self, dev_template):
        dev_template.has_resource_properties("AWS::DynamoDB::Table", Match.object_like({
            "KeySchema": Match.array_with([
                Match.object_like({"AttributeName": "eval_run_id", "KeyType": "HASH"}),
                Match.object_like({"AttributeName": "test_case_id", "KeyType": "RANGE"}),
            ]),
        }))

    def test_opensearch_serverless_collection(self, dev_template):
        dev_template.has_resource_properties("AWS::OpenSearchServerless::Collection", {
            "Type": "VECTORSEARCH",
        })

    def test_opensearch_encryption_policy(self, dev_template):
        dev_template.has_resource_properties("AWS::OpenSearchServerless::SecurityPolicy", {
            "Type": "encryption",
        })

    def test_opensearch_network_policy(self, dev_template):
        dev_template.has_resource_properties("AWS::OpenSearchServerless::SecurityPolicy", {
            "Type": "network",
        })


# =============================================================================
# LAYER 6: OBSERVABILITY
# =============================================================================
class TestObservability:
    def test_alert_topic_created(self, dev_template):
        dev_template.has_resource_properties("AWS::SNS::Topic", {
            "TopicName": Match.string_like_regexp(".*alerts.*"),
        })


# =============================================================================
# LAYER 7: LLMOPS (Bedrock Knowledge Base + Guardrails)
# =============================================================================
class TestLLMOps:
    def test_knowledge_base_created(self, dev_template):
        dev_template.has_resource_properties("AWS::Bedrock::KnowledgeBase", {
            "KnowledgeBaseConfiguration": Match.object_like({
                "Type": "VECTOR",
            }),
        })

    def test_knowledge_base_data_source(self, dev_template):
        dev_template.has_resource_properties("AWS::Bedrock::DataSource", {
            "DataSourceConfiguration": Match.object_like({
                "Type": "S3",
            }),
        })

    def test_guardrail_created(self, dev_template):
        dev_template.has_resource_properties("AWS::Bedrock::Guardrail", {
            "ContentPolicyConfig": Match.any_value(),
            "SensitiveInformationPolicyConfig": Match.any_value(),
            "TopicPolicyConfig": Match.any_value(),
        })

    def test_guardrail_blocks_pii(self, dev_template):
        dev_template.has_resource_properties("AWS::Bedrock::Guardrail", Match.object_like({
            "SensitiveInformationPolicyConfig": Match.object_like({
                "PiiEntitiesConfig": Match.array_with([
                    Match.object_like({"Type": "SSN", "Action": "BLOCK"}),
                ]),
            }),
        }))


# =============================================================================
# LAYER 8: STRANDS AGENT RUNTIME
# =============================================================================
class TestStrandsAgentRuntime:
    def test_agent_lambda_created(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*strands-agent.*"),
            "Runtime": "python3.12",
            "Architectures": ["arm64"],
            "Timeout": 900,
            "TracingConfig": {"Mode": "Active"},
        }))

    def test_agent_lambda_environment(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*strands-agent.*"),
            "Environment": Match.object_like({
                "Variables": Match.object_like({
                    "DEFAULT_MODEL_ID": Match.string_like_regexp(".*claude.*"),
                    "MAX_TURNS": "30",
                }),
            }),
        }))

    def test_agent_lambda_in_vpc(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*strands-agent.*"),
            "VpcConfig": Match.any_value(),
        }))

    def test_ecs_cluster_created(self, dev_template):
        dev_template.has_resource_properties("AWS::ECS::Cluster", {
            "ClusterSettings": Match.array_with([
                Match.object_like({"Name": "containerInsights", "Value": "enabled"}),
            ]),
        })

    def test_fargate_task_definition(self, dev_template):
        dev_template.has_resource_properties("AWS::ECS::TaskDefinition", Match.object_like({
            "Cpu": "1024",
            "Memory": "2048",
            "RequiresCompatibilities": ["FARGATE"],
        }))

    def test_agent_role_has_bedrock_permissions(self, dev_template):
        dev_template.has_resource_properties("AWS::IAM::Policy", Match.object_like({
            "PolicyDocument": Match.object_like({
                "Statement": Match.array_with([
                    Match.object_like({
                        "Action": Match.array_with(["bedrock:InvokeModel"]),
                        "Effect": "Allow",
                    }),
                ]),
            }),
        }))


# =============================================================================
# LAYER 4: API (REST + Cognito)
# =============================================================================
class TestAPILayer:
    def test_cognito_user_pool_created(self, dev_template):
        dev_template.has_resource_properties("AWS::Cognito::UserPool", Match.object_like({
            "AutoVerifiedAttributes": ["email"],
        }))

    def test_cognito_password_policy(self, dev_template):
        dev_template.has_resource_properties("AWS::Cognito::UserPool", Match.object_like({
            "Policies": Match.object_like({
                "PasswordPolicy": Match.object_like({
                    "MinimumLength": 12,
                    "RequireLowercase": True,
                    "RequireUppercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": True,
                }),
            }),
        }))

    def test_rest_api_created(self, dev_template):
        dev_template.has_resource_properties("AWS::ApiGateway::RestApi", {
            "Name": Match.string_like_regexp(".*api.*"),
        })

    def test_api_tracing_enabled(self, dev_template):
        dev_template.has_resource_properties("AWS::ApiGateway::Stage", Match.object_like({
            "TracingEnabled": True,
        }))

    def test_document_upload_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*doc-ingestion.*"),
            "Runtime": "python3.12",
        }))

    def test_prod_cognito_mfa_required(self, prod_template):
        prod_template.has_resource_properties("AWS::Cognito::UserPool", Match.object_like({
            "MfaConfiguration": "ON",
        }))


# =============================================================================
# LAYER 10: STRANDS AGENT FRONTEND (WebSocket + Session REST)
# =============================================================================
class TestStrandsAgentFrontend:
    def test_websocket_api_created(self, dev_template):
        dev_template.has_resource_properties("AWS::ApiGatewayV2::Api", {
            "ProtocolType": "WEBSOCKET",
            "RouteSelectionExpression": "$request.body.action",
        })

    def test_websocket_stage_created(self, dev_template):
        dev_template.has_resource_properties("AWS::ApiGatewayV2::Stage", Match.object_like({
            "AutoDeploy": True,
        }))

    def test_ws_connect_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*ws-connect.*"),
        }))

    def test_ws_message_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*ws-message.*"),
        }))

    def test_ws_disconnect_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*ws-disconnect.*"),
        }))

    def test_connection_table_created(self, dev_template):
        dev_template.has_resource_properties("AWS::DynamoDB::Table", Match.object_like({
            "KeySchema": Match.array_with([
                Match.object_like({"AttributeName": "connection_id", "KeyType": "HASH"}),
            ]),
        }))

    def test_session_management_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*sessions-api.*"),
        }))

    def test_frontend_config_ssm_parameter(self, dev_template):
        dev_template.has_resource_properties("AWS::SSM::Parameter", Match.object_like({
            "Name": Match.string_like_regexp(".*agent-frontend-config.*"),
        }))


# =============================================================================
# LAYER 9: STRANDS AGENTCORE (Gateway + Memory + OAuth2)
# =============================================================================
class TestStrandsAgentCore:
    def test_agentcore_user_pool_created(self, dev_template):
        dev_template.has_resource_properties("AWS::Cognito::UserPool", Match.object_like({
            "UserPoolName": Match.string_like_regexp(".*agentcore.*"),
        }))

    def test_agentcore_resource_server(self, dev_template):
        dev_template.has_resource_properties("AWS::Cognito::UserPoolResourceServer", Match.object_like({
            "Identifier": Match.string_like_regexp(".*gateway.*"),
        }))

    def test_agentcore_client_secret(self, dev_template):
        dev_template.has_resource_properties("AWS::SecretsManager::Secret", Match.object_like({
            "Name": Match.string_like_regexp(".*agentcore-gateway.*"),
        }))

    def test_gateway_tool_db_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*gateway-tool-db.*"),
        }))

    def test_gateway_tool_api_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*gateway-tool-api.*"),
        }))

    def test_memory_config_ssm(self, dev_template):
        dev_template.has_resource_properties("AWS::SSM::Parameter", Match.object_like({
            "Name": Match.string_like_regexp(".*memory-config.*"),
        }))


# =============================================================================
# LAYER 11: STRANDS AGENT EVAL
# =============================================================================
class TestStrandsAgentEval:
    def test_eval_runner_lambda(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*eval-runner.*"),
        }))

    def test_eval_state_machine(self, dev_template):
        dev_template.has_resource_properties("AWS::StepFunctions::StateMachine", Match.object_like({
            "StateMachineName": Match.string_like_regexp(".*agent-eval.*"),
            "TracingConfiguration": {"Enabled": True},
        }))

    def test_eval_score_alarm(self, dev_template):
        dev_template.has_resource_properties("AWS::CloudWatch::Alarm", Match.object_like({
            "AlarmName": Match.string_like_regexp(".*eval-score.*"),
            "Threshold": 0.85,
            "ComparisonOperator": "LessThanThreshold",
        }))

    def test_eval_cicd_config_ssm(self, dev_template):
        dev_template.has_resource_properties("AWS::SSM::Parameter", Match.object_like({
            "Name": Match.string_like_regexp(".*cicd-config.*"),
        }))

    def test_eval_dataset_bucket(self, dev_template):
        dev_template.has_resource_properties("AWS::S3::Bucket", Match.object_like({
            "BucketName": Match.string_like_regexp(".*eval-datasets.*"),
        }))


# =============================================================================
# LAYER 5: FRONTEND (S3 + CloudFront + WAF)
# =============================================================================
class TestFrontend:
    def test_frontend_bucket_created(self, dev_template):
        dev_template.has_resource_properties("AWS::S3::Bucket", Match.object_like({
            "BucketName": Match.string_like_regexp(".*frontend.*"),
        }))

    def test_cloudfront_distribution(self, dev_template):
        dev_template.has_resource_properties("AWS::CloudFront::Distribution", Match.object_like({
            "DistributionConfig": Match.object_like({
                "DefaultRootObject": "index.html",
                "ViewerCertificate": Match.any_value(),
            }),
        }))

    def test_waf_web_acl(self, dev_template):
        dev_template.has_resource_properties("AWS::WAFv2::WebACL", Match.object_like({
            "Scope": "CLOUDFRONT",
            "Rules": Match.array_with([
                Match.object_like({"Name": "AWSManagedRulesCommonRuleSet"}),
                Match.object_like({"Name": "RateLimit"}),
            ]),
        }))

    def test_cloudfront_error_responses(self, dev_template):
        dev_template.has_resource_properties("AWS::CloudFront::Distribution", Match.object_like({
            "DistributionConfig": Match.object_like({
                "CustomErrorResponses": Match.array_with([
                    Match.object_like({"ErrorCode": 404, "ResponseCode": 200}),
                    Match.object_like({"ErrorCode": 403, "ResponseCode": 200}),
                ]),
            }),
        }))


# =============================================================================
# ENVIRONMENT-SPECIFIC TESTS
# =============================================================================
class TestEnvironmentDifferences:
    def test_dev_lambda_memory_512(self, dev_template):
        dev_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*strands-agent-dev.*"),
            "MemorySize": 512,
        }))

    def test_prod_lambda_memory_1024(self, prod_template):
        prod_template.has_resource_properties("AWS::Lambda::Function", Match.object_like({
            "FunctionName": Match.string_like_regexp(".*strands-agent-prod.*"),
            "MemorySize": 1024,
        }))

    def test_prod_opensearch_standby_enabled(self, prod_template):
        prod_template.has_resource_properties("AWS::OpenSearchServerless::Collection", {
            "StandbyReplicas": "ENABLED",
        })

    def test_dev_opensearch_standby_disabled(self, dev_template):
        dev_template.has_resource_properties("AWS::OpenSearchServerless::Collection", {
            "StandbyReplicas": "DISABLED",
        })


# =============================================================================
# STACK OUTPUTS
# =============================================================================
class TestStackOutputs:
    def test_api_url_output(self, dev_template):
        dev_template.has_output("ApiUrl", Match.any_value())

    def test_user_pool_id_output(self, dev_template):
        dev_template.has_output("UserPoolId", Match.any_value())

    def test_cloudfront_url_output(self, dev_template):
        dev_template.has_output("CloudFrontURL", Match.any_value())

    def test_knowledge_base_id_output(self, dev_template):
        dev_template.has_output("KnowledgeBaseId", Match.any_value())

    def test_guardrail_id_output(self, dev_template):
        dev_template.has_output("GuardrailId", Match.any_value())

    def test_agent_lambda_arn_output(self, dev_template):
        dev_template.has_output("StrandsAgentLambdaArn", Match.any_value())

    def test_websocket_url_output(self, dev_template):
        dev_template.has_output("AgentWebSocketURL", Match.any_value())

    def test_eval_sfn_arn_output(self, dev_template):
        dev_template.has_output("AgentEvalSFNArn", Match.any_value())
