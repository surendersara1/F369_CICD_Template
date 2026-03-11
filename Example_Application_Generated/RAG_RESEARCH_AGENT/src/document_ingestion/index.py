"""
Document Ingestion — handles document upload to S3 and triggers KB sync.
Supports PDF, DOCX, TXT, HTML, Markdown (max 50MB).
"""
import boto3
import os
import json
import time
import base64
import uuid

s3 = boto3.client("s3")
bedrock_agent = boto3.client("bedrock-agent")

BUCKET = os.environ["DOCUMENTS_BUCKET"]
KB_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
MAX_SIZE_MB = 50
ALLOWED_TYPES = {"pdf", "docx", "txt", "html", "md", "markdown"}


def handler(event, context):
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    filename = body.get("filename", f"upload-{uuid.uuid4().hex[:8]}.txt")
    content_b64 = body.get("content_base64", "")
    collection = body.get("collection", "documents")
    metadata = body.get("metadata", {})

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    if ext not in ALLOWED_TYPES:
        return _response(400, {"error": f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_TYPES)}"})

    try:
        content_bytes = base64.b64decode(content_b64) if content_b64 else b""
    except Exception:
        return _response(400, {"error": "Invalid base64 content"})

    if len(content_bytes) > MAX_SIZE_MB * 1024 * 1024:
        return _response(400, {"error": f"File exceeds {MAX_SIZE_MB}MB limit"})

    key = f"{collection}/{time.strftime('%Y/%m/%d')}/{filename}"
    s3.put_object(
        Bucket=BUCKET, Key=key, Body=content_bytes,
        Metadata={
            "upload_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "collection": collection,
            **{k: str(v) for k, v in metadata.items()},
        },
    )

    # Trigger KB data source sync if configured
    ds_id = os.environ.get("DATA_SOURCE_ID")
    if KB_ID and ds_id:
        try:
            bedrock_agent.start_ingestion_job(
                knowledgeBaseId=KB_ID, dataSourceId=ds_id)
        except Exception:
            pass  # Non-blocking — sync will happen on schedule

    return _response(200, {"message": "Document uploaded", "s3_key": key, "bucket": BUCKET})


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body)}
