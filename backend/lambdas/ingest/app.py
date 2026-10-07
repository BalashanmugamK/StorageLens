"""Bucket-ingest Lambda: the product's bridge to an OUTSIDE bucket.

The documents API only sees objects uploaded through its own
presigned-PUT flow — the gap that made this unusable against an
existing customer's storage. Two routes close it:

  POST /imports/inventory    — an S3 Inventory CSV (optionally .gz),
                               streamed row by row into DocumentsTable.
  POST /imports/access-logs  — S3 server access logs under a prefix,
                               streamed GET records into
                               AccessHistoryTable as DOWNLOAD events.

Every route reports exactly what it did (imported / skipped / why /
truncated) — no silent clipping. Both are idempotent document ids
(uuid5 of bucket+key), so re-running an import overwrites the same
items instead of duplicating them.
"""

import csv
import gzip
import io
import json
import logging
import os
import uuid
import zlib
from datetime import datetime, timezone
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
ACCESS_HISTORY_TABLE = os.environ["ACCESS_HISTORY_TABLE"]

documents_table = dynamodb.Table(DOCUMENTS_TABLE)
access_history_table = dynamodb.Table(ACCESS_HISTORY_TABLE)

# A single ingest invocation is bounded — a customer listing millions
# of objects splits the request rather than timing out silently.
MAX_INVENTORY_ROWS = 200_000
MAX_LOG_FILES = 200

# Streaming reads are byte-budgeted: a manifest or log object larger
# than this aborts the import instead of ballooning a 512 MB Lambda
# into an OOM. (The row caps bound OUTPUT; this bounds INPUT.)
MAX_INGEST_BYTES = 512 * 1024**2
INGEST_CHUNK_SIZE = 1024 * 1024

# Storage-class names S3 Inventory uses vs. the class names the
# engine's pricing snapshot uses. GLACIER is ambiguous (flexible vs
# instant retrieval is not in the inventory column), so it maps to
# flexible retrieval — the conservative (cheaper-to-predict)
# interpretation — and never silently pretends otherwise.
INVENTORY_CLASS_MAP = {
    "GLACIER": "GLACIER_FLEXIBLE_RETRIEVAL",
    "DEEP_ARCHIVE": "GLACIER_DEEP_ARCHIVE",
    "STANDARD": "STANDARD",
    "STANDARD_IA": "STANDARD_IA",
    "INTELLIGENT_TIERING": "INTELLIGENT_TIERING",
    "ONEZONE_IA": "ONEZONE_IA",
    "REDUCED_REDUNDANCY": "REDUCED_REDUNDANCY",
}

KNOWN_CLASSES = set(INVENTORY_CLASS_MAP.values())

# The preflight check reads / lists a BOUNDED slice only — it answers
# "will the import work and what will it find", never the import itself.
CHECK_SNIFF_BYTES = 16_384       # plain-CSV header sniff (a Range read)
CHECK_GZ_MAX_BYTES = 4_000_000   # whole-read cap for .gz manifest sniffing
CHECK_INVENTORY_PAGES = 3        # discovery depth for schema.csv candidates
CHECK_LOG_PAGES = 2              # access-log listing depth before "truncated"
CHECK_LOG_SAMPLE_FILES = 3       # objects sampled for the format probe
CHECK_CANDIDATE_CAP = 10

# Inventory manifest candidates: the Hive-style schema object S3
# Inventory writes. A full data manifest (data-*.csv.gz) is huge and
# never a candidate.
INVENTORY_SCHEMAS = ("schema.csv", "schema.csv.gz")

# Aggregate-recognised access types; a GET that served bytes to a
# human or app is a DOWNLOAD on this system's ledger.
ACCESS_TYPE_DOWNLOAD = "DOWNLOAD"


def document_id_for(bucket: str, key: str) -> str:
    """Deterministic per (bucket, key): the same object always maps
    to the same document row, so re-imports are true upserts."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"s3:{bucket}/{key}"))


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def lambda_handler(event, context):
    route = f"{event.get('requestContext', {}).get('http', {}).get('method')} {event.get('requestContext', {}).get('http', {}).get('path')}"

    if route == "POST /imports/inventory":
        return import_inventory(event)
    if route == "POST /imports/access-logs":
        return import_access_logs(event)
    if route == "POST /imports/check":
        return check_import(event)

    return response(404, {"error": f"Route not found: {route}"})


def parse_body(event):
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return None


def actor_from_event(event):
    """Identity of the signed-in caller as recorded by the JWT
    authorizer — stamped on imports so every inbound row is
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


class IngestTooLarge(Exception):
    """Raised when a streamed object exceeds MAX_INGEST_BYTES."""


def _iter_lines(bucket, key, max_bytes=MAX_INGEST_BYTES):
    """Stream one object as decoded text lines, byte-budgeted.

    Reads Body.iter_chunks(1 MB) instead of whole-read(): a customer
    manifest is only bounded by their bucket, and loading it whole
    is an OOM waiting for an invocation. `.gz` streams through an
    incremental zlib(31) decompressor held across chunks, so a line
    split at ANY chunk boundary (compressed or not) is reassembled
    before yield — only complete lines ever leave this generator,
    with the final unterminated line flushed at EOF.

    Budget is enforced per raw chunk BEFORE decompressing, so the
    abort happens while memory holds at most one chunk. Multi-member
    gzip (a .gz of concatenated archives) single-member streams end
    with unused data members ignored — S3 Inventory writes one
    member per manifest, which is the supported shape.
    """

    body = s3.get_object(Bucket=bucket, Key=key)["Body"]

    decompressor = (
        zlib.decompressobj(31) if key.endswith(".gz") else None
    )
    buffer = b""
    total = 0

    try:
        for chunk in body.iter_chunks(chunk_size=INGEST_CHUNK_SIZE):
            total += len(chunk)

            if total > max_bytes:
                raise IngestTooLarge(
                    f"s3://{bucket}/{key} exceeds the ingest byte "
                    f"budget ({max_bytes})"
                )

            text_chunk = (
                decompressor.decompress(chunk)
                if decompressor is not None
                else chunk
            )

            if not text_chunk:
                continue

            # The buffer stays BYTES until the newline, so a UTF-8
            # sequence split across chunks (or archives) decodes whole.
            buffer += text_chunk

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace")
    finally:
        body.close()

    if decompressor is not None:
        buffer += decompressor.flush()

    if buffer:
        yield buffer.decode("utf-8", errors="replace")


def _read_text(bucket: str, key: str) -> str:
    """One object's full text, transparently .gz-decompressed.

    Kept only for the BOUNDED preflight sniffs (Range reads and the
    CHECK_GZ_MAX_BYTES cap above make these whole-reads safe). The
    imports themselves stream through _iter_lines instead.
    """
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if key.endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def import_inventory(event):
    """Stream an S3 Inventory CSV into DocumentsTable.

    body: {"bucket": "...", "key": "manifest.csv" | "manifest.csv.gz"}
    Inventory lists one row per object VERSION; for versioned buckets
    the latest version is listed first per object, so seen-first wins.
    """
    body = parse_body(event)
    if body is None:
        return response(400, {"error": "Request body must be valid JSON"})

    bucket = (body.get("bucket") or "").strip()
    key = (body.get("key") or "").strip()
    document_state = (body.get("document_state") or "").strip().upper() or None

    if not bucket or not key:
        return response(
            400,
            {"error": "Both 'bucket' and 'key' (the inventory CSV object) are required"},
        )

    imported = 0
    skipped_unsupported = 0
    skipped_invalid = 0
    unsupported_examples = []
    truncated = False
    actor = actor_from_event(event)

    # Compensating control for the intentional cross-bucket read
    # (Resource "*" on this function): every ingest is logged with
    # who asked for WHICH bucket's data.
    logger.info(
        "import_inventory actor=%s bucket=%s key=%s", actor, bucket, key
    )

    try:
        reader = csv.DictReader(
            _iter_lines(bucket, key, max_bytes=MAX_INGEST_BYTES)
        )

        with documents_table.batch_writer() as writer:
            for row in reader:
                if imported >= MAX_INVENTORY_ROWS:
                    truncated = True
                    break

                object_key = (row.get("Key") or "").strip()
                if not object_key:
                    skipped_invalid += 1
                    continue

                raw_class = (row.get("StorageClass") or "").strip() or "STANDARD"
                storage_class = INVENTORY_CLASS_MAP.get(raw_class.upper())
                if storage_class is None:
                    skipped_unsupported += 1
                    if len(unsupported_examples) < 5:
                        unsupported_examples.append(raw_class)
                    # Still importable to the pipeline — the engine
                    # prices known classes only, and unknown ones stay
                    # import-at-STANDARD only with an honest report. A
                    # skipped row keeps the fleet projection's tier
                    # allocation truthful, so it is skipped outright.
                    continue

                try:
                    file_size = int((row.get("Size") or "0").strip())
                except ValueError:
                    skipped_invalid += 1
                    continue

                raw_ts = (row.get("LastModifiedDate") or "").strip()
                try:
                    upload_timestamp = (
                        datetime.strptime(
                            raw_ts, "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        .replace(tzinfo=timezone.utc)
                        .isoformat()
                        if raw_ts
                        else datetime.now(timezone.utc).isoformat()
                    )
                except ValueError:
                    upload_timestamp = datetime.now(timezone.utc).isoformat()

                item = {
                    "document_id": document_id_for(bucket, object_key),
                    "object_key": object_key,
                    "file_name": object_key.rsplit("/", 1)[-1] or object_key,
                    "file_size": file_size,
                    "content_type": "application/octet-stream",
                    "upload_timestamp": upload_timestamp,
                    "current_storage_class": storage_class,
                    "imported_from": f"s3://{bucket}/{key}",
                    # Every inbound row carries the operator who ran
                    # the import — the import trail is attributable.
                    "imported_by": actor,
                }
                if document_state:
                    item["document_state"] = document_state

                writer.put_item(Item=item)
                imported += 1
    except s3.exceptions.NoSuchKey:
        return response(404, {"error": f"No such inventory object: s3://{bucket}/{key}"})
    except IngestTooLarge:
        return response(
            413,
            {
                "error": (
                    f"{key} exceeds the ingest byte budget "
                    f"({MAX_INGEST_BYTES} bytes) — split or filter the "
                    "manifest and import it in parts"
                ),
                "row_cap": MAX_INVENTORY_ROWS,
            },
        )
    except Exception as exc:
        logger.exception("inventory import failed")
        return response(500, {"error": f"Inventory import failed: {exc}"})

    return response(
        200,
        {
            "documents_imported": imported,
            "documents_skipped_unsupported_class": skipped_unsupported,
            "documents_skipped_invalid": skipped_invalid,
            "unsupported_class_examples": unsupported_examples,
            "truncated": truncated,
            "row_cap": MAX_INVENTORY_ROWS,
            "imported_by": actor,
            "basis": (
                "Rows came straight from the S3 Inventory manifest "
                f"s3://{bucket}/{key}; timestamps are the objects' own "
                "LastModifiedDate. Access history is NOT imported by "
                "this route — run POST /imports/access-logs against the "
                "bucket's logs before trusting recency features."
            ),
        },
    )


# S3 server access log GET record: the quoted request is
# "GET /bucket/key HTTP/1.1" and the numeric status follows it.


def import_access_logs(event):
    """Stream S3 server access logs under a prefix into
    AccessHistoryTable DOWNLOAD events.

    body: {"bucket": "...", "prefix": "logs/"}
    Only successful object GETs (200/206/304, non-empty key) become
    access events; each file is scanned in listing order, capped.
    """
    body = parse_body(event)
    if body is None:
        return response(400, {"error": "Request body must be valid JSON"})

    bucket = (body.get("bucket") or "").strip()
    prefix = (body.get("prefix") or "").strip()

    if not bucket:
        return response(400, {"error": "'bucket' (the bucket holding the access logs) is required"})

    # The bounded listing loop, structured for clarity:
    listing_args = {"Bucket": bucket, "MaxKeys": 1000}
    if prefix:
        listing_args["Prefix"] = prefix

    files_scanned = 0
    events_recorded = 0
    files_failed = []
    files_skipped_too_large = 0
    truncated = False
    actor = actor_from_event(event)

    # Compensating control for the intentional cross-bucket read:
    # every ingest is logged with who asked for WHICH bucket's logs.
    logger.info(
        "import_access_logs actor=%s bucket=%s prefix=%s",
        actor,
        bucket,
        prefix,
    )

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(**listing_args):
            for obj in page.get("Contents", []):
                if files_scanned >= MAX_LOG_FILES:
                    truncated = True
                    break
                files_scanned += 1
                log_key = obj["Key"]

                try:
                    events_recorded += _ingest_one_log_file(bucket, log_key)
                except IngestTooLarge:
                    files_skipped_too_large += 1
                    logger.warning(
                        "skipped %s: exceeds the ingest byte budget", log_key
                    )
                except Exception as exc:
                    logger.warning("failed to parse %s: %s", log_key, exc)
                    files_failed.append(log_key)
            if truncated:
                break

        if files_scanned and not events_recorded and not files_failed:
            basis_tail = "no successful object GETs were found in the log format"
        else:
            basis_tail = f"scanned {files_scanned} log objects"

        return response(
            200,
            {
                "events_recorded": events_recorded,
                "files_scanned": files_scanned,
                "files_failed": files_failed,
                "files_skipped_too_large": files_skipped_too_large,
                "truncated": truncated,
                "file_cap": MAX_LOG_FILES,
                "imported_by": actor,
                "basis": (
                    "Access events were parsed from S3 server access "
                    f"logs under s3://{bucket}/{prefix}: successful GETs "
                    "become DOWNLOAD events with the log's own "
                    "timestamps. " + basis_tail + "."
                ),
            },
        )
    except Exception as exc:
        logger.exception("access-log import failed")
        return response(500, {"error": f"Access-log import failed: {exc}"})


def _ingest_one_log_file(bucket: str, log_key: str) -> int:
    """Parse one access-log object into DOWNLOAD events."""
    recorded = 0
    with access_history_table.batch_writer() as writer:
        for line in _iter_lines(
            bucket, log_key, max_bytes=MAX_INGEST_BYTES
        ):
            parsed = _parse_access_log_line(line)
            if parsed is None:
                continue
            ts, doc_key = parsed
            writer.put_item(
                Item={
                    "document_id": document_id_for(bucket, doc_key),
                    "access_timestamp": ts,
                    "access_type": ACCESS_TYPE_DOWNLOAD,
                }
            )
            recorded += 1
    return recorded


def _parse_access_log_line(line: str):
    """One S3 access-log line → (iso_timestamp, object_key) for a
    successful object GET, else None.

    The format is space-separated with the request and referrer/agent
    quoted; the pieces this needs are the bracketed timestamp, the
    quoted "METHOD /path HTTP/x" block and the status right after it.
    """
    try:
        ts_start = line.index("[") + 1
        ts_end = line.index("]", ts_start)
        raw_ts = line[ts_start:ts_end]  # 06/Oct/2026:22:51:38 +0000
        timestamp_iso = (
            datetime.strptime(raw_ts, "%d/%b/%Y:%H:%M:%S %z")
            .astimezone(timezone.utc)
            .isoformat()
        )

        req_start = line.index('"GET ') + len('"GET ')
        req_end = line.index('"', req_start)
        request = line[req_start:req_end]
        # "GET /bucket/key HTTP/1.1" → path "/bucket/key"
        path = request.split(" ")[0]
        if not path.startswith("/"):
            return None

        # The status is the first token after the closing quote:
        # ... "GET /b-x/key HTTP/1.1" 200 - 1131 82 ...
        status_token = line[req_end + 1 :].split()[0]
        if status_token not in ("200", "206", "304"):
            return None

        # "/bucket/key/with/slashes" → key after the FIRST slash
        # segment (bucket names never contain '/').
        _, bucket_name, key = path.lstrip("/").partition("/")
        if not bucket_name or not key:
            return None

        return timestamp_iso, unquote(key)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------
# Preflight: POST /imports/check
# ---------------------------------------------------------------
#
# The wizard's "does this bucket actually work here?" step. It never
# imports anything — it answers three questions against the smallest
# possible set of reads and reports what it found:
#   1. is the bucket readable with this deployment's grants
#   2. is there an inventory manifest (a given key, or discovered
#      schema.csv candidates), and does it carry the columns the
#      import needs
#   3. are there access-log objects under a prefix, and does a
#      sampled chunk parse like an S3 access log


def check_import(event):
    body = parse_body(event)
    if body is None:
        return response(400, {"error": "Request body must be valid JSON"})

    bucket = (body.get("bucket") or "").strip()
    manifest_key = (body.get("key") or "").strip()
    log_prefix = (body.get("prefix") or "").strip()

    if not bucket:
        return response(
            400, {"error": "'bucket' is required for the preflight check"}
        )

    checks: dict = {
        "bucket": bucket,
        "reachable": None,
        "inventory": {"notes": []},
        "access_logs": {"prefix": log_prefix, "notes": []},
        "ready": False,
    }
    inventory = checks["inventory"]
    access_logs = checks["access_logs"]

    # 1) Reachability: the same grants the imports will use, so a fail
    #    here predicts the import's own failure.
    try:
        s3.head_bucket(Bucket=bucket)
        checks["reachable"] = True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        checks["reachable"] = False
        checks["notes"] = [
            f"Bucket check failed ({code}) — the import role cannot "
            "read this bucket here; the customer's copy of the stack "
            "needs its own read grant on this bucket."
        ]
        return response(200, {**checks, "basis": _check_basis(checks)})

    # 2) Inventory manifest: a given key is verified directly; without
    #    one, bounded discovery lists schema.csv candidates.
    if manifest_key:
        inventory["mode"] = "checked"
        inventory["key"] = manifest_key
        try:
            head = s3.head_object(Bucket=bucket, Key=manifest_key)
            inventory["exists"] = True
            inventory["size_bytes"] = head.get("ContentLength")
            columns_ok, missing, extra = _sniff_inventory_columns(
                bucket, manifest_key, head.get("ContentLength") or 0
            )
            inventory["columns_ok"] = columns_ok
            if missing:
                inventory["missing_columns"] = missing
            for reason in extra.values():
                inventory["notes"].append(f"Column sniff incomplete: {reason}")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            inventory["exists"] = False
            inventory["notes"].append(
                f"The manifest object is not readable ({code}) — "
                "the inventory import will fail the same way."
            )
    else:
        inventory["mode"] = "discovered"
        candidates, truncated = _discover_manifests(bucket)
        inventory["candidates"] = candidates
        inventory["discovery_limited"] = truncated
        if not candidates:
            inventory["notes"].append(
                "No schema.csv found within the first listing pages — "
                "inventory manifests can live anywhere; give the "
                "manifest's exact key to check it."
            )

    inventory_usable = (
        (manifest_key and inventory.get("exists") and inventory.get("columns_ok"))
        or (not manifest_key and inventory.get("candidates"))
    )

    # 3) Access logs: bounded listing + a small parseable-chunk sample.
    try:
        listing_args: dict = {"Bucket": bucket, "MaxKeys": 1000}
        if log_prefix:
            listing_args["Prefix"] = log_prefix
        paginator = s3.get_paginator("list_objects_v2")
        files_found = 0
        listing_truncated = False
        listed_keys: list[str] = []
        for page in paginator.paginate(**listing_args):
            for obj in page.get("Contents", []):
                files_found += 1
                if len(listed_keys) < CHECK_LOG_SAMPLE_FILES:
                    listed_keys.append(obj["Key"])
            if page.get("IsTruncated") and files_found >= CHECK_LOG_PAGES * 1000:
                listing_truncated = True
                break

        access_logs["files_found"] = files_found
        access_logs["listing_truncated"] = listing_truncated

        if files_found == 0:
            access_logs["notes"].append(
                "No objects under this prefix — without access logs the "
                "recency/frequency features degrade to upload-time assumptions."
            )
        else:
            # Sample a few objects and stop at the first that parses:
            # listing order is lexicographic, so without a prefix the
            # first key is as likely to be the inventory schema as a
            # log. A sample that finds nothing is still reported.
            sampled_files, sample_success = _sample_log_files(bucket, listed_keys)
            access_logs["sampled_files"] = sampled_files
            access_logs["sample_parse_success"] = sample_success
            if not sample_success:
                access_logs["notes"].append(
                    "None of the sampled objects' heads parsed as S3 "
                    "access-log lines — possibly a different format, a "
                    "wrong prefix, or .gz files; the import itself "
                    "reports per-file parsing results."
                )
    except Exception as exc:
        access_logs["notes"].append(f"Listing the log prefix failed: {exc}")

    checks["ready"] = bool(inventory_usable)
    return response(200, {**checks, "basis": _check_basis(checks)})


def _check_basis(checks: dict) -> str:
    ready_note = "READY — run POST /imports/inventory" if checks.get("ready") else (
        "NOT ready — see the individual checks"
        if checks.get("reachable")
        else "bucket unreadable"
    )
    return (
        "Preflight only — nothing was imported and no bucket state was "
        f"changed. {ready_note}. Discovery is capped "
        f"({CHECK_INVENTORY_PAGES} listing pages for the manifest, "
        f"{CHECK_LOG_PAGES} for logs): an empty result is not proof of "
        "absence beyond that page budget."
    )


def _discover_manifests(bucket: str):
    """schema.csv candidates within a bounded listing budget."""
    candidates: list[str] = []
    truncated = False

    for _prefix in ("inventory/", ""):
        paginator = s3.get_paginator("list_objects_v2")
        pages_traversed = 0
        for page in paginator.paginate(
            Bucket=bucket, Prefix=_prefix, MaxKeys=1000
        ):
            pages_traversed += 1
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(INVENTORY_SCHEMAS):
                    candidates.append(key)
                    if len(candidates) >= CHECK_CANDIDATE_CAP:
                        return candidates, truncated or page.get("IsTruncated")
            if pages_traversed >= CHECK_INVENTORY_PAGES:
                truncated = truncated or bool(page.get("IsTruncated"))
                break
        if candidates:
            break

    return candidates, truncated


def _sniff_inventory_columns(bucket: str, key: str, size_bytes: int):
    """Read a bounded slice of the manifest and check the columns the
    importer consumes. Returns (columns_ok, missing_columns, extra)."""
    missing = []
    try:
        if key.endswith(".gz"):
            if size_bytes > CHECK_GZ_MAX_BYTES:
                return None, [], {
                    "size_bytes_exceeds_sniff_cap": CHECK_GZ_MAX_BYTES
                }
            text = _read_text(bucket, key)[:CHECK_SNIFF_BYTES]
        else:
            data = s3.get_object(
                Bucket=bucket,
                Key=key,
                Range=f"bytes=0-{CHECK_SNIFF_BYTES - 1}",
            )["Body"].read()
            text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        return None, [], {"sniff_failed": str(exc)}

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    lookup = {name.strip().lower(): name for name in fieldnames if name}
    for required in ("key", "size", "storageclass"):
        if required not in lookup:
            missing.append(required)

    if fieldnames:
        return (len(missing) == 0), missing, {}
    return None, [], {"sniff_empty": True}


def _sample_log_files(bucket: str, sample_keys: list[str]):
    """Parse the FIRST kilobyte of each of a few listed objects as a
    format sample, stopping at the first that yields parseable lines.

    A Range cut may split the final line — count only successful
    lines, so the sample says what it did and does not pretend a
    truncated file is a bad format.
    """
    sampled: list[str] = []
    for key in sample_keys:
        sampled.append(key)
        try:
            data = s3.get_object(
                Bucket=bucket, Key=key, Range="bytes=0-4095"
            )["Body"].read()
            text = data.decode("utf-8", errors="replace")
            successes = sum(
                1 for line in text.splitlines() if _parse_access_log_line(line)
            )
            if successes:
                return sampled, successes
        except Exception:
            continue
    return sampled, 0