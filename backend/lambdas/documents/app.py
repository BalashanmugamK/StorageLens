import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from decimal import Decimal
s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "ap-south-1"),
    endpoint_url="https://s3.ap-south-1.amazonaws.com"
)
dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ["DOCUMENTS_BUCKET"]
TABLE_NAME = os.environ["DOCUMENTS_TABLE"]

table = dynamodb.Table(TABLE_NAME)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=lambda value: int(value) if isinstance(value, Decimal) else str(value))
    }

def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    if method == "POST" and path == "/documents":
        return create_document(event)

    if method == "GET" and path == "/documents":
        return list_documents()

    if method == "GET" and path.startswith("/documents/"):
        document_id = path.split("/")[-1]
        return get_document(document_id)

    return response(
        404,
        {
            "error": "Not found"
        }
    )


def create_document(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(
            400,
            {
                "error": "Request body must be valid JSON"
            }
        )

    required_fields = ["file_name", "file_size", "content_type"]

    for field in required_fields:
        if field not in body:
            return response(
                400,
                {
                    "error": f"Missing required field: {field}"
                }
            )

    file_name = body["file_name"]
    file_size = body["file_size"]
    content_type = body["content_type"]

    if not isinstance(file_name, str) or not file_name.strip():
        return response(
            400,
            {
                "error": "file_name must be a non-empty string"
            }
        )

    if not isinstance(file_size, int) or file_size <= 0:
        return response(
            400,
            {
                "error": "file_size must be a positive integer"
            }
        )

    if not isinstance(content_type, str) or not content_type.strip():
        return response(
            400,
            {
                "error": "content_type must be a non-empty string"
            }
        )

    document_id = str(uuid.uuid4())
    object_key = f"documents/{document_id}/{file_name}"

    upload_timestamp = datetime.now(timezone.utc).isoformat()

    try:
        upload_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": BUCKET_NAME,
                "Key": object_key,
                "ContentType": content_type
            },
            ExpiresIn=900
        )

        item = {
            "document_id": document_id,
            "object_key": object_key,
            "file_name": file_name,
            "file_size": file_size,
            "content_type": content_type,
            "upload_timestamp": upload_timestamp,
            "current_storage_class": "STANDARD"
        }

        table.put_item(Item=item)

    except Exception:
        return response(
            500,
            {
                "error": "Failed to prepare document upload"
            }
        )

    return response(
        201,
        {
            "document_id": document_id,
            "upload_url": upload_url,
            "object_key": object_key
        }
    )


def list_documents():
    try:
        result = table.scan()

        return response(
            200,
            {
                "documents": result.get("Items", [])
            }
        )

    except Exception:
        return response(
            500,
            {
                "error": "Failed to retrieve documents"
            }
        )


def get_document(document_id):
    if not document_id:
        return response(
            400,
            {
                "error": "document_id is required"
            }
        )

    try:
        result = table.get_item(
            Key={
                "document_id": document_id
            }
        )

        item = result.get("Item")

        if not item:
            return response(
                404,
                {
                    "error": "Document not found"
                }
            )

        return response(
            200,
            item
        )

    except Exception:
        return response(
            500,
            {
                "error": "Failed to retrieve document"
            }
        )