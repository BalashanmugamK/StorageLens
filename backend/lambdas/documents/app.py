import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3

logger = logging.getLogger(__name__)

# The governance states the eligibility policy recognizes. A document
# uploaded without one is treated as unconstrained by the engine —
# never silently assigned one that would narrow its candidates.
# (Aggregation reads document_state verbatim from the documents table,
# so the value the engine sees at forecast time is exactly this.)
DOCUMENT_STATES = frozenset(["ACTIVE", "CLOSED", "ARCHIVED"])


# Region comes from the Lambda runtime's AWS_REGION environment
# variable (resolved by boto3), so the deployment works in any
# region without a hard-coded regional endpoint.
s3 = boto3.client("s3")

dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ["DOCUMENTS_BUCKET"]
TABLE_NAME = os.environ["DOCUMENTS_TABLE"]
ACCESS_HISTORY_TABLE_NAME = os.environ["ACCESS_HISTORY_TABLE"]

# Sign-in bootstrap (GET /config). Optional: when the Lambda runs
# without a Cognito pool (e.g. the test harness), the frontend
# treats the API as unauthenticated and skips sign-in entirely
# instead of guessing pool ids.
USER_POOL_ID = os.environ.get("USER_POOL_ID")
CLIENT_ID = os.environ.get("CLIENT_ID")
AUTH_DOMAIN = os.environ.get("AUTH_DOMAIN")

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

    if method == "GET" and path == "/config":
        return get_config()

    if method == "POST" and path == "/documents":
        return create_document(event)

    if (
        method == "POST"
        and path.startswith("/documents/")
        and path.endswith("/confirm-upload")
    ):
        document_id = path.split("/")[-2]
        return confirm_upload(document_id, event)

    if method == "GET" and path == "/documents":
        return list_documents(event)

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


def get_config():
    """Public sign-in bootstrap (the one route outside the JWT
    authorizer). auth is null when no pool is wired, so the
    frontend degrades to unauthenticated rather than guessing ids."""

    return response(
        200,
        {
            "auth": None
            if not (USER_POOL_ID and CLIENT_ID and AUTH_DOMAIN)
            else {
                # The token endpoint lives on the hosted-UI domain's
                # own origin - /oauth2/token is reached directly, so
                # no API Gateway involvement in the token exchange.
                "user_pool_id": USER_POOL_ID,
                "client_id": CLIENT_ID,
                "auth_domain": AUTH_DOMAIN,
            }
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

    # Optional governance state (ACTIVE / CLOSED / ARCHIVED). The
    # eligibility policy only engages for documents that carry one;
    # without it the engine treats every storage class as eligible.
    document_state = body.get("document_state")

    if document_state is not None:
        document_state = str(document_state).upper()

        if document_state not in DOCUMENT_STATES:
            return response(
                400,
                {
                    "error": (
                        f"document_state must be one of "
                        f"{sorted(DOCUMENT_STATES)} or omitted"
                    )
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
            "current_storage_class": "STANDARD",
            # The row exists before the PUT has happened; until the
            # presigned upload is confirmed with a HEAD, aggregation
            # and proposal paths skip it as UPLOAD_PENDING so an
            # abandoned upload never leaves a phantom in the fleet.
            "upload_status": "UPLOAD_PENDING",
            "uploaded_by": actor_from_event(event),
        }

        if document_state is not None:
            item["document_state"] = document_state

        table.put_item(Item=item)

    except Exception:
        logger.exception("failed to prepare an upload")
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


def actor_from_event(event):
    """Identity of the signed-in caller as recorded by the JWT
    authorizer — stamped on created documents so orphaned uploads are
    attributable. Claims are absent outside the API (tests), which
    is fine."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )

    email = claims.get("email")
    subject = claims.get("sub")

    return email if email else subject


def confirm_upload(document_id, event=None):
    """POST /documents/{id}/confirm-upload — the presigned PUT has
    completed (or the client says it has); HEAD the object before the
    row is trusted. Verifies the object actually exists and stamps
    UPLOAD_VERIFIED conditionally, so confirming twice or confirming
    a phantom upload both fail loudly."""

    if not document_id:
        return response(400, {"error": "document_id is required"})

    try:
        item = table.get_item(Key={"document_id": document_id}).get("Item")

        if not item:
            return response(404, {"error": "Document not found"})

        object_key = item.get("object_key")

        if not object_key:
            return response(
                409,
                {"error": "document has no object_key to verify"},
            )

        # Legacy/synthetic rows predate the marker (no upload_status)
        # and are treated as verified everywhere in the pipeline; a
        # row already VERIFIED re-confirms as a no-op. Only a row
        # still UPLOAD_PENDING needs the HEAD.
        if item.get("upload_status", "UPLOAD_VERIFIED") != "UPLOAD_PENDING":
            return response(200, {
                **item,
                "upload_status": "UPLOAD_VERIFIED",
            })

        try:
            s3.head_object(Bucket=BUCKET_NAME, Key=object_key)
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get(
                "Code", ""
            ) if hasattr(error, "response") else ""

            if code in ("404", "NoSuchKey", "NotFound"):
                return response(409, {
                    "error": (
                        "object not found in S3 - the upload did not "
                        "complete; nothing was confirmed"
                    )
                })

            logger.exception("could not verify upload of %s", document_id)
            return response(500, {"error": "Failed to verify the upload"})

        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression=(
                "SET upload_status = :status, confirmed_by = :actor, "
                "#confirmed_at = :at"
            ),
            ConditionExpression=(
                "attribute_not_exists(upload_status) "
                "OR upload_status = :pending"
            ),
            ExpressionAttributeNames={"#confirmed_at": "confirmed_at"},
            ExpressionAttributeValues={
                ":status": "UPLOAD_VERIFIED",
                ":actor": actor_from_event(event or {}),
                ":at": datetime.now(timezone.utc).isoformat(),
                ":pending": "UPLOAD_PENDING",
            },
        )

    except Exception as error:
        _code = ""
        if hasattr(error, "response"):
            _code = (
                (error.response or {}).get("Error", {}).get("Code", "")
            )

        if _code == "ConditionalCheckFailedException":
            # Two concurrent confirms: one wins, one reports it.
            logger.warning(
                "confirm-upload race lost for %s", document_id
            )
            return response(
                409,
                {"error": "upload already confirmed by a concurrent request"},
            )

        logger.exception("failed to confirm upload %s", document_id)
        return response(500, {"error": "Failed to confirm the upload"})

    return response(200, {
        "document_id": document_id,
        "upload_status": "UPLOAD_VERIFIED",
        "confirmed_by": actor_from_event(event or {}),
    })


def list_documents(event):
    query = event.get("queryStringParameters") or {}

    try:
        limit, start_key = parse_page_params(query)
    except ValueError as error:
        return response(400, {"error": str(error)})

    try:
        # Page through the full table: a bare scan() returns only the
        # first ~1 MB page and the list would silently truncate at
        # 1,000+ documents. With a client `limit` only ONE page is
        # served and LastEvaluatedKey rides out in `next_token`;
        # without it the historical collect-all behavior is kept.
        documents = []
        scan_kwargs = {}

        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key

        while True:
            if limit:
                scan_kwargs.setdefault("Limit", limit)

            result = table.scan(**scan_kwargs)

            documents.extend(result.get("Items", []))

            last_key = result.get("LastEvaluatedKey")

            if not last_key:
                break

            scan_kwargs["ExclusiveStartKey"] = last_key

            if limit and len(documents) >= limit:
                break

        body = {"documents": documents}

        if limit and documents and last_key:
            body["next_token"] = encode_page_token(last_key)

        return response(200, body)

    except Exception:
        logger.exception("failed to list documents")
        return response(
            500,
            {
                "error": "Failed to retrieve documents"
            }
        )


def parse_page_params(query):
    """Decode the optional `limit` (1-1000) and `next_token` (URL-safe
    base64 of a JSON LastEvaluatedKey) shared by the listing routes.
    Returns (limit, start_key); limit is None when unset, which keeps
    the historical collect-all behavior. Raises ValueError on a
    malformed request."""

    limit_raw = (query or {}).get("limit")

    if limit_raw in (None, ""):
        limit = None
    else:
        limit = int(limit_raw)

        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

    token = (query or {}).get("next_token")

    if token in (None, ""):
        return limit, None

    start_key = json.loads(
        base64.urlsafe_b64decode(token.encode()).decode()
    )

    if not isinstance(start_key, dict):
        raise ValueError("next_token is malformed")

    return limit, start_key


def encode_page_token(last_evaluated_key):
    """URL-safe continuation token for a listing's LastEvaluatedKey."""

    return base64.urlsafe_b64encode(
        json.dumps(last_evaluated_key).encode()
    ).decode()


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
        logger.exception("failed to retrieve a document")
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
        logger.exception("failed to prepare a document download")
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

        # Retrieve all access events for this document (page through —
        # a bare query stops at the first ~1 MB page).
        events = []
        query_kwargs = {
            "KeyConditionExpression": "document_id = :document_id",
            "ExpressionAttributeValues": {
                ":document_id": document_id
            }
        }

        while True:
            history_result = access_history_table.query(**query_kwargs)

            events.extend(history_result.get("Items", []))

            last_key = history_result.get("LastEvaluatedKey")

            if not last_key:
                break

            query_kwargs["ExclusiveStartKey"] = last_key

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
        logger.exception("failed to compute access statistics")
        return response(
            500,
            {
                "error": "Failed to calculate access statistics"
            }
        )