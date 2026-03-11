"""WebSocket $message handler — invoke Strands agent and stream response."""
import boto3
import os
import json
import time

lambda_client = boto3.client("lambda")
apigw_mgmt = None


def _get_mgmt():
    global apigw_mgmt
    if apigw_mgmt is None:
        apigw_mgmt = boto3.client("apigatewaymanagementapi",
                                   endpoint_url=os.environ["WS_ENDPOINT"])
    return apigw_mgmt


def _post(connection_id: str, data: dict) -> bool:
    try:
        _get_mgmt().post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(data).encode("utf-8"))
        return True
    except Exception:
        return False


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    body = json.loads(event.get("body", "{}"))
    user_message = body.get("message", "")
    session_id = body.get("session_id", connection_id)

    if not user_message:
        _post(connection_id, {"type": "error", "message": "Empty message"})
        return {"statusCode": 400}

    _post(connection_id, {"type": "status", "status": "thinking", "session_id": session_id})

    response = lambda_client.invoke(
        FunctionName=os.environ["AGENT_FUNCTION_NAME"],
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "message": user_message,
            "session_id": session_id,
            "actor_id": body.get("actor_id", "anonymous"),
        }),
    )

    payload = json.loads(response["Payload"].read())
    agent_body = json.loads(payload.get("body", "{}"))

    _post(connection_id, {
        "type": "message",
        "session_id": agent_body.get("session_id", session_id),
        "content": agent_body.get("response", "No response from agent."),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _post(connection_id, {"type": "status", "status": "done"})

    return {"statusCode": 200}
