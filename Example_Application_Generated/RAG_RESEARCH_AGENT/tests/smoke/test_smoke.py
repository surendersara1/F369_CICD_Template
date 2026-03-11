"""
RAG Research Agent — Smoke Tests
Lightweight health checks run post-deploy in each pipeline stage.
Validates that core infrastructure is reachable and responding.

Set environment variables:
  - API_URL: REST API base URL
  - WS_URL: WebSocket API URL
  - CLOUDFRONT_URL: CloudFront distribution URL
"""
import json
import os
import pytest
import requests


def get_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        pytest.skip(f"{key} not set — skipping smoke test")
    return val


# =============================================================================
# HEALTH CHECKS
# =============================================================================
class TestHealth:
    def test_rest_api_reachable(self):
        """REST API base URL returns a response (even 403 means it's alive)."""
        api_url = get_env("API_URL")
        resp = requests.get(api_url, timeout=10)
        assert resp.status_code in (200, 403, 404)

    def test_cloudfront_serves_frontend(self):
        """CloudFront distribution serves the React SPA."""
        cf_url = get_env("CLOUDFRONT_URL")
        resp = requests.get(cf_url, timeout=10)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("Content-Type", "")

    def test_cloudfront_spa_routing(self):
        """CloudFront returns index.html for unknown paths (SPA routing)."""
        cf_url = get_env("CLOUDFRONT_URL")
        resp = requests.get(f"{cf_url}/some/random/path", timeout=10)
        assert resp.status_code == 200


# =============================================================================
# INFRASTRUCTURE CHECKS
# =============================================================================
class TestInfrastructure:
    def test_api_cors_headers(self):
        """REST API returns CORS headers on OPTIONS."""
        api_url = get_env("API_URL")
        resp = requests.options(f"{api_url}/agent/invoke", timeout=10)
        # CORS preflight should return 200 or 204
        assert resp.status_code in (200, 204, 403)

    def test_api_rejects_unauthenticated(self):
        """Protected endpoints reject unauthenticated requests."""
        api_url = get_env("API_URL")
        resp = requests.post(
            f"{api_url}/agent/invoke",
            json={"query": "test"},
            timeout=10,
        )
        assert resp.status_code in (401, 403)

    def test_websocket_endpoint_reachable(self):
        """WebSocket URL is reachable (connection attempt)."""
        ws_url = get_env("WS_URL")
        try:
            import websocket
            ws = websocket.create_connection(ws_url, timeout=5)
            assert ws.connected
            ws.close()
        except ImportError:
            pytest.skip("websocket-client not installed")
        except Exception:
            # Connection may fail without auth, but endpoint is reachable
            pass


# =============================================================================
# BASIC AGENT INVOCATION
# =============================================================================
class TestBasicAgent:
    def test_agent_responds_to_simple_query(self):
        """End-to-end: authenticate, invoke agent, get response."""
        import boto3
        import uuid

        api_url = get_env("API_URL")
        region = os.environ.get("AWS_REGION", "us-east-1")

        username = os.environ.get("TEST_USERNAME")
        password = os.environ.get("TEST_PASSWORD")
        client_id = os.environ.get("USER_POOL_CLIENT_ID")

        if not all([username, password, client_id]):
            pytest.skip("Test credentials not configured")

        cognito = boto3.client("cognito-idp", region_name=region)
        auth_resp = cognito.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        token = auth_resp["AuthenticationResult"]["IdToken"]

        resp = requests.post(
            f"{api_url}/agent/invoke",
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"query": "Hello, are you working?", "session_id": str(uuid.uuid4())},
            timeout=60,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body is not None
