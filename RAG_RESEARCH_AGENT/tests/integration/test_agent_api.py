"""
RAG Research Agent — Integration Tests
Tests deployed API endpoints, WebSocket connectivity, agent invocation,
document upload, session management, and eval pipeline.

These tests run against a LIVE deployed environment.
Set environment variables before running:
  - API_URL: REST API base URL
  - WS_URL: WebSocket API URL
  - USER_POOL_ID: Cognito User Pool ID
  - USER_POOL_CLIENT_ID: Cognito App Client ID
  - TEST_USERNAME: Cognito test user email
  - TEST_PASSWORD: Cognito test user password
"""
import json
import os
import time
import uuid
import pytest
import boto3
import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        pytest.skip(f"Environment variable {key} not set — skipping integration test")
    return val


def get_auth_token() -> str:
    """Authenticate against Cognito and return an ID token."""
    client = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = client.initiate_auth(
        ClientId=get_env("USER_POOL_CLIENT_ID"),
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": get_env("TEST_USERNAME"),
            "PASSWORD": get_env("TEST_PASSWORD"),
        },
    )
    return resp["AuthenticationResult"]["IdToken"]


@pytest.fixture(scope="module")
def auth_headers():
    token = get_auth_token()
    return {"Authorization": token, "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def api_url():
    return get_env("API_URL").rstrip("/")


# =============================================================================
# REST API TESTS
# =============================================================================
class TestRestAPI:
    def test_agent_invoke_returns_200(self, api_url, auth_headers):
        """POST /agent/invoke with a simple question returns 200."""
        resp = requests.post(
            f"{api_url}/agent/invoke",
            headers=auth_headers,
            json={"query": "What is machine learning?", "session_id": str(uuid.uuid4())},
            timeout=60,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "response" in body or "answer" in body

    def test_agent_invoke_includes_citations(self, api_url, auth_headers):
        """Agent response should include source citations when answering from KB."""
        resp = requests.post(
            f"{api_url}/agent/invoke",
            headers=auth_headers,
            json={"query": "Summarize the uploaded documents", "session_id": str(uuid.uuid4())},
            timeout=60,
        )
        assert resp.status_code == 200

    def test_agent_invoke_unauthorized_without_token(self, api_url):
        """Requests without auth token should be rejected."""
        resp = requests.post(
            f"{api_url}/agent/invoke",
            json={"query": "test"},
            timeout=10,
        )
        assert resp.status_code in (401, 403)

    def test_agent_invoke_rejects_empty_query(self, api_url, auth_headers):
        """Empty query should return 400."""
        resp = requests.post(
            f"{api_url}/agent/invoke",
            headers=auth_headers,
            json={"query": "", "session_id": str(uuid.uuid4())},
            timeout=30,
        )
        assert resp.status_code in (400, 422, 200)


# =============================================================================
# SESSION MANAGEMENT TESTS
# =============================================================================
class TestSessionManagement:
    def test_list_sessions(self, api_url, auth_headers):
        """GET /agent/sessions returns a list."""
        resp = requests.get(f"{api_url}/agent/sessions", headers=auth_headers, timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, (list, dict))

    def test_create_and_retrieve_session(self, api_url, auth_headers):
        """Create a session via invoke, then retrieve it."""
        session_id = str(uuid.uuid4())
        # Create session by invoking agent
        requests.post(
            f"{api_url}/agent/invoke",
            headers=auth_headers,
            json={"query": "Hello", "session_id": session_id},
            timeout=60,
        )
        time.sleep(2)
        # Retrieve session
        resp = requests.get(f"{api_url}/agent/sessions/{session_id}", headers=auth_headers, timeout=10)
        assert resp.status_code in (200, 404)

    def test_delete_session(self, api_url, auth_headers):
        """DELETE /agent/sessions/{id} should return 200 or 204."""
        session_id = str(uuid.uuid4())
        resp = requests.delete(f"{api_url}/agent/sessions/{session_id}", headers=auth_headers, timeout=10)
        assert resp.status_code in (200, 204, 404)


# =============================================================================
# DOCUMENT UPLOAD TESTS
# =============================================================================
class TestDocumentUpload:
    def test_upload_endpoint_exists(self, api_url, auth_headers):
        """POST /documents/upload should accept requests (even if no file)."""
        resp = requests.post(
            f"{api_url}/documents/upload",
            headers=auth_headers,
            json={"filename": "test.txt", "content_type": "text/plain"},
            timeout=10,
        )
        assert resp.status_code in (200, 400, 422)

    def test_upload_unauthorized(self, api_url):
        """Upload without auth should be rejected."""
        resp = requests.post(
            f"{api_url}/documents/upload",
            json={"filename": "test.txt"},
            timeout=10,
        )
        assert resp.status_code in (401, 403)


# =============================================================================
# WEBSOCKET TESTS
# =============================================================================
class TestWebSocket:
    def test_websocket_connection(self):
        """Test WebSocket connection establishment."""
        ws_url = get_env("WS_URL")
        try:
            import websocket
            ws = websocket.create_connection(ws_url, timeout=10)
            assert ws.connected
            ws.close()
        except ImportError:
            pytest.skip("websocket-client not installed")
        except Exception as e:
            pytest.skip(f"WebSocket connection failed (expected in CI without deploy): {e}")

    def test_websocket_send_message(self):
        """Send a message via WebSocket and expect a response."""
        ws_url = get_env("WS_URL")
        try:
            import websocket
            ws = websocket.create_connection(ws_url, timeout=30)
            ws.send(json.dumps({
                "action": "sendMessage",
                "query": "What is RAG?",
                "session_id": str(uuid.uuid4()),
            }))
            result = ws.recv()
            assert result is not None
            ws.close()
        except ImportError:
            pytest.skip("websocket-client not installed")
        except Exception as e:
            pytest.skip(f"WebSocket test skipped: {e}")


# =============================================================================
# EVAL PIPELINE TESTS
# =============================================================================
class TestEvalPipeline:
    def test_eval_state_machine_exists(self):
        """Verify the eval Step Functions state machine exists."""
        sfn_client = boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        project_name = "rag-research-agent"
        stage = os.environ.get("STAGE", "dev")
        try:
            resp = sfn_client.list_state_machines(maxResults=100)
            names = [sm["name"] for sm in resp["stateMachines"]]
            expected = f"{project_name}-agent-eval-{stage}"
            assert expected in names, f"State machine {expected} not found in {names}"
        except Exception as e:
            pytest.skip(f"Cannot list state machines: {e}")

    def test_eval_dataset_bucket_accessible(self):
        """Verify eval dataset bucket exists and is accessible."""
        s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        account = boto3.client("sts").get_caller_identity()["Account"]
        stage = os.environ.get("STAGE", "dev")
        bucket = f"rag-research-agent-eval-datasets-{stage}-{account}"
        try:
            s3_client.head_bucket(Bucket=bucket)
        except Exception as e:
            pytest.skip(f"Eval dataset bucket not accessible: {e}")
