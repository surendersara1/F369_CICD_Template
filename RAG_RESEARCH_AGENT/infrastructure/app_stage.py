"""
RAG Research Agent — CDK App Stage
Wraps the AppStack for pipeline deployment to dev/staging/prod.
"""
import aws_cdk as cdk
from constructs import Construct
from infrastructure.app_stack import AppStack


class AppStage(cdk.Stage):
    def __init__(self, scope: Construct, construct_id: str, stage_name: str = "dev", **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.app_stack = AppStack(
            self, "AppStack",
            stage_name=stage_name,
            description=f"RAG Research Agent — {stage_name} environment",
        )
