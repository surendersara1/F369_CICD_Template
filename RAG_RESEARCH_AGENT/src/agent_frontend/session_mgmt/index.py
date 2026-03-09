"""Session management REST API — list, get, delete agent conversation sessions."""
import boto3
import os
import json
from boto3.dynamodb.conditions import Key

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["SESSION_TABLE"])


def handler(event, context):
    method = event["httpMethod"]
    path_params = event.get("pathParameters") or {}
    session_id = path_params.get("session_id")
    claims = event["requestContext"]["authorizer"]["claims"]
    actor_id = claims.get("sub", claims.get("email", "anonymous"))

    if method == "GET" and not session_id:
        return _list_sessions(actor_id)
    elif method == "GET" and session_id:
        return _get_session(session_id, actor_id)
    elif method == "DELETE" and session_id:
        return _delete_session(session_id, actor_id)
    return _response(405, {"error": "Method not allowed"})


def _list_sessions(actor_id: str) -> dict:
    result = table.query(
        IndexName="actor-sessions-idx",
        KeyConditionExpression=Key("actor_id").eq(actor_id),
        ScanIndexForward=False, Limit=50)
    seen = {}
    for item in result.get("Items", []):
        sid = item["session_id"]
        if sid not in seen:
            seen[sid] = {"session_id": sid, "created_at": item.get("created_at", ""),
                         "preview": item.get("user_message", "")[:100]}
    return _response(200, {"sessions": list(seen.values())})


def _get_session(session_id: str, actor_id: str) -> dict:
    result = table.query(KeyConditionExpression=Key("session_id").eq(session_id), ScanIndexForward=True)
    items = result.get("Items", [])
    if items and items[0].get("actor_id") != actor_id:
        return _response(403, {"error": "Access denied"})
    turns = [{"turn_id": i["turn_id"], "user_message": i.get("user_message", ""),
              "agent_response": i.get("agent_response", ""), "created_at": i.get("created_at", "")}
             for i in items]
    return _response(200, {"session_id": session_id, "turns": turns})


def _delete_session(session_id: str, actor_id: str) -> dict:
    result = table.query(KeyConditionExpression=Key("session_id").eq(session_id),
                         ProjectionExpression="session_id, turn_id, actor_id")
    items = result.get("Items", [])
    if items and items[0].get("actor_id") != actor_id:
        return _response(403, {"error": "Access denied"})
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"session_id": item["session_id"], "turn_id": item["turn_id"]})
    return _response(200, {"deleted": session_id})


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body, default=str)}
