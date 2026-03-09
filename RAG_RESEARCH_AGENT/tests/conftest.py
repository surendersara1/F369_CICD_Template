"""
RAG Research Agent — Shared Pytest Fixtures
Provides CDK app/stack fixtures, mocked AWS services, and test helpers.
"""
import json
import os
import pytest
import boto3
from unittest.mock import patch, MagicMock
from moto import mock_aws

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infrastructure.app_stack import AppStack


# ---------------------------------------------------------------------------
# CDK Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def cdk_app():
    """Session-scoped CDK App for template synthesis."""
    return cdk.App()


@pytest.fixture(scope="session")
def dev_stack(cdk_app):
    """Synthesized dev AppStack — session-scoped for speed."""
    stack = AppStack(cdk_app, "TestDevStack", stage_name="dev",
        env=cdk.Environment(account="123456789012", region="us-east-1"))
    return stack


@pytest.fixture(scope="session")
def dev_template(dev_stack):
    """CloudFormation template assertions for dev stack."""
    return Template.from_stack(dev_stack)


@pytest.fixture(scope="session")
def prod_stack(cdk_app):
    """Synthesized prod AppStack — session-scoped for speed."""
    stack = AppStack(cdk_app, "TestProdStack", stage_name="prod",
        env=cdk.Environment(account="123456789012", region="us-east-1"))
    return stack


@pytest.fixture(scope="session")
def prod_template(prod_stack):
    """CloudFormation template assertions for prod stack."""
    return Template.from_stack(prod_stack)
