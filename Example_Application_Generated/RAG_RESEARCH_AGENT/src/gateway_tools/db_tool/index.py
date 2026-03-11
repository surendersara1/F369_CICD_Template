"""Gateway Tool: Database Query — exposed via AgentCore Gateway MCP endpoint."""
import boto3
import os
import json
from boto3.dynamodb.conditions import Key

ddb = boto3.resource("dynamodb")
session_table = ddb.Table(os.environ["SESSION_TABLE"])


def handler(event, context):
    """Handle MCP tool invocation for database queries."""
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    action = body.get("action", "query_sessions")

    if action == "query_sessions":
        actor_id = body.get("actor_id", "")
        result = session_table.query(
            IndexName="actor-sessions-idx",
            KeyConditionExpression=Key("actor_id").eq(actor_id),
            ScanIndexForward=False, Limit=body.get("limit", 10))
        return {"statusCode": 200, "body": json.dumps({
            "sessions": [{"session_id": i["session_id"], "created_at": i.get("created_at", "")}
                         for i in result.get("Items", [])]})}

    elif action == "get_session_turns":
        session_id = body.get("session_id", "")
        result = session_table.query(
            KeyConditionExpression=Key("session_id").eq(session_id), ScanIndexForward=True)
        return {"statusCode": 200, "body": json.dumps({
            "turns": [{"user_message": i.get("user_message", ""), "agent_response": i.get("agent_response", "")}
                      for i in result.get("Items", [])]})}

    return {"statusCode": 400, "body": json.dumps({"error": f"Unknown action: {action}"})}
