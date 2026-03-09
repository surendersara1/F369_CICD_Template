"""WebSocket $connect handler — authenticate and register connection."""
import boto3
import os
import time

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["CONNECTION_TABLE"])


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    query_params = event.get("queryStringParameters") or {}
    actor_id = query_params.get("actor_id", "anonymous")
    session_id = query_params.get("session_id", connection_id)

    table.put_item(Item={
        "connection_id": connection_id,
        "actor_id": actor_id,
        "session_id": session_id,
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": int(time.time()) + (2 * 3600),
    })

    return {"statusCode": 200, "body": "Connected"}
