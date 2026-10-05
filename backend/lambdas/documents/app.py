import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3


# Region comes from the Lambda runtime's AWS_REGION environment
# variable (resolved by boto3), so the deployment works in any
# region without a hard-coded regional endpoint.
s3 = boto3.client("s3")

dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ["DOCUMENTS_BUCKET"]
TABLE_NAME = os.environ["DOCUMENTS_TABLE"]
ACCESS_HISTORY_TABLE_NAME = os.environ["ACCESS_HISTORY_TABLE"]

table = dynamodb.Table(TABLE_NAME)
access_history_table = dynamodb.Table(ACCESS_HISTORY_TABLE_NAME)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            body,
            default=lambda value: int(value)
            if isinstance(value, Decimal)
            else str(value)
        )
    }


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    if method == "POST" and path == "/documents":
        return create_document(event)

    if method == "GET" and path == "/documents":
        return list_documents()

    if method == "GET" and path.startswith("/documents/") and path.endswith("/download"):
        document_id = path.split("/")[-2]
        return download_document(document_id)
    
    if method == "GET" and path.startswith("/documents/") and path.endswith("/access-stats"):
        document_id = path.split("/")[-2]
        return get_access_stats(document_id)

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


def download_document(document_id):
    if not document_id:
        return response(
            400,
            {
                "error": "document_id is required"
            }
        )

    try:
        # Verify that the document exists.
        result = table.get_item(
            Key={
                "document_id": document_id
            }
        )

        document = result.get("Item")

        if not document:
            return response(
                404,
                {
                    "error": "Document not found"
                }
            )

        # Record the access event.
        access_timestamp = datetime.now(timezone.utc).isoformat()

        access_history_table.put_item(
            Item={
                "document_id": document_id,
                "access_timestamp": access_timestamp,
                "access_type": "DOWNLOAD"
            }
        )

        # Generate a temporary download URL.
        download_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": BUCKET_NAME,
                "Key": document["object_key"]
            },
            ExpiresIn=900
        )

        return response(
            200,
            {
                "document_id": document_id,
                "download_url": download_url,
                "access_timestamp": access_timestamp,
                "access_type": "DOWNLOAD"
            }
        )

    except Exception:
        return response(
            500,
            {
                "error": "Failed to prepare document download"
            }
        )
def get_access_stats(document_id):
    if not document_id:
        return response(
            400,
            {
                "error": "document_id is required"
            }
        )

    try:
        # Verify that the document exists.
        document_result = table.get_item(
            Key={
                "document_id": document_id
            }
        )

        document = document_result.get("Item")

        if not document:
            return response(
                404,
                {
                    "error": "Document not found"
                }
            )

        # Retrieve all access events for this document.
        history_result = access_history_table.query(
            KeyConditionExpression="document_id = :document_id",
            ExpressionAttributeValues={
                ":document_id": document_id
            }
        )

        events = history_result.get("Items", [])

        access_count = len(events)

        if events:
            last_accessed = max(
                event["access_timestamp"]
                for event in events
            )
        else:
            last_accessed = None

        # Count accesses during the last 30 days.
        now = datetime.now(timezone.utc)
        recent_cutoff = now - timedelta(days=30)

        recent_access_count = 0

        for event in events:
            timestamp = datetime.fromisoformat(
                event["access_timestamp"]
            )

            if timestamp >= recent_cutoff:
                recent_access_count += 1

        return response(
            200,
            {
                "document_id": document_id,
                "access_count": access_count,
                "last_accessed": last_accessed,
                "recent_access_count": recent_access_count
            }
        )

    except Exception:
        return response(
            500,
            {
                "error": "Failed to calculate access statistics"
            }
        )