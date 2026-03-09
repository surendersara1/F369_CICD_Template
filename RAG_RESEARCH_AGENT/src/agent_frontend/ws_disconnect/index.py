"""WebSocket $disconnect handler — clean up connection record."""
import boto3
import os

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["CONNECTION_TABLE"])


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    table.delete_item(Key={"connection_id": connection_id})
    return {"statusCode": 200, "body": "Disconnected"}
