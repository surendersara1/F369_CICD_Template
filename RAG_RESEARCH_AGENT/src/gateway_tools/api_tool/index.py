"""Gateway Tool: External API — exposed via AgentCore Gateway MCP endpoint."""
import json
import urllib.request
import urllib.error


def handler(event, context):
    """Handle MCP tool invocation for external API calls."""
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    url = body.get("url", "")
    method = body.get("method", "GET").upper()
    headers = body.get("headers", {})
    payload = body.get("payload")

    if not url:
        return {"statusCode": 400, "body": json.dumps({"error": "URL required"})}

    try:
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        if data:
            req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=25) as resp:
            response_body = resp.read().decode("utf-8")
            return {"statusCode": resp.status, "body": response_body}
    except urllib.error.HTTPError as e:
        return {"statusCode": e.code, "body": json.dumps({"error": str(e)})}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
