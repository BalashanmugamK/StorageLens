"""Tests for the bucket-ingest Lambda (S3 Inventory + access logs)."""

import gzip
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

INGEST_LAMBDA_DIR = REPO_ROOT / "backend" / "lambdas" / "ingest"
LAMBDA_PATH = INGEST_LAMBDA_DIR / "app.py"


def make_s3_stub(objects: dict[str, bytes]):
    """A boto3 s3 client stub whose get_object/body reads in-memory
    bytes the way the StreamingBody does, with iter_lines()."""
    s3 = MagicMock()

    class Body:
        def __init__(self, data: bytes):
            self.data = data

        def read(self):
            return self.data

        def iter_chunks(self, chunk_size=1024):
            for i in range(0, len(self.data), chunk_size):
                yield self.data[i:i + chunk_size]

        def iter_lines(self, _chunk_size=1024):
            for line in self.data.split(b"\n"):
                if line:
                    yield line

        def close(self):
            pass

    def get_object(Bucket, Key):
        if Key not in objects:
            err = KeyError(Key)
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": Body(objects[Key])}

    s3.get_object.side_effect = get_object
    s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    # NoSuchKey is matched by the real client's exception attr — the
    # handler checks s3.exceptions.NoSuchKey; make get_object raising
    # a KeyError pass anyway by mapping at handler level is simpler:
    # tests below do not rely on the 404 path.
    return s3


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DOCUMENTS_TABLE", "DocumentsTable")
    monkeypatch.setenv("ACCESS_HISTORY_TABLE", "AccessHistoryTable")

    dynamodb = MagicMock()
    documents = MagicMock()
    access = MagicMock()
    dynamodb.Table.side_effect = lambda name: {
        "DocumentsTable": documents,
        "AccessHistoryTable": access,
    }[name]
    # batch_writer() used as a context manager → expose the owning
    # table mock as the writer, so put_item assertions read it directly
    documents.batch_writer.return_value.__enter__.return_value = documents
    documents.batch_writer.return_value.__exit__.return_value = False
    access.batch_writer.return_value.__enter__.return_value = access
    access.batch_writer.return_value.__exit__.return_value = False

    spec = importlib.util.spec_from_file_location(
        "ingest_app", LAMBDA_PATH,
        submodule_search_locations=[str(INGEST_LAMBDA_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_app"] = module

    with patch("boto3.resource", return_value=dynamodb), patch("boto3.client", return_value=MagicMock()):
        spec.loader.exec_module(module)

    module._test_tables = {"documents": documents, "access": access}
    return module


def post(app, body, path="/imports/inventory"):
    return app.lambda_handler(
        {
            "requestContext": {"http": {"method": "POST", "path": path}},
            "body": json.dumps(body),
        },
        {},
    )


# ---------- inventory ----------

INVENTORY_CSV = b"\n".join(
    [
        # S3 Inventory CSV: schema header row, then rows
        b"version,bucket,Key,Size,LastModifiedDate,ETag,StorageClass",
        b'1,b-x,"docs/report.pdf",123456,2026-01-02T03:04:05.678Z,abc,STANDARD',
        b'1,b-x,"docs/old.pdf",900,2025-01-02T03:04:05.678Z,abc,GLACIER',
        b'1,b-x,"docs/deep.pdf",7000,2025-05-01T00:00:00.000Z,abc,DEEP_ARCHIVE',
        b'1,b-x,"docs/weird.pdf",100,2025-06-01T00:00:00.000Z,abc,BOGUS_TIER',
        b'1,b-x,"",100,2025-06-01T00:00:00.000Z,abc,STANDARD',
    ]
)


def test_inventory_import_maps_and_counts(app):
    app.s3 = make_s3_stub({"inv.csv": INVENTORY_CSV})
    result = post(app, {"bucket": "b-x", "key": "inv.csv"})

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["documents_imported"] == 3
    assert body["documents_skipped_unsupported_class"] == 1
    assert body["documents_skipped_invalid"] == 1
    assert body["unsupported_class_examples"] == ["BOGUS_TIER"]

    written = app._test_tables["documents"].put_item.call_args_list
    classes = {c.kwargs["Item"]["object_key"]: c.kwargs["Item"]["current_storage_class"] for c in written}
    assert classes["docs/report.pdf"] == "STANDARD"
    # GLACIER maps to flexible retrieval, DEEP_ARCHIVE keeps its name
    assert classes["docs/old.pdf"] == "GLACIER_FLEXIBLE_RETRIEVAL"
    assert classes["docs/deep.pdf"] == "GLACIER_DEEP_ARCHIVE"


def test_inventory_ids_are_deterministic_and_match_access_logs_ids(app):
    app.s3 = make_s3_stub({"inv.csv": INVENTORY_CSV, "logs.txt": LOG_TXT})
    pag = MagicMock()
    pag.paginate.return_value = [{"Contents": [{"Key": "logs.txt"}]}]
    app.s3.get_paginator.return_value = pag
    post(app, {"bucket": "b-x", "key": "inv.csv"})
    post(app, {"bucket": "b-x", "prefix": "logs/"}, path="/imports/access-logs")

    docs = {c.kwargs["Item"]["document_id"] for c in app._test_tables["documents"].put_item.call_args_list}
    events = {c.kwargs["Item"]["document_id"] for c in app._test_tables["access"].put_item.call_args_list}

    # The log GET for docs/report.pdf binds to the SAME document id
    # the inventory row created — the two imports meet on identity.
    assert app.document_id_for("b-x", "docs/report.pdf") in docs
    assert app.document_id_for("b-x", "docs/report.pdf") in events


def test_inventory_deterministic_ids_across_calls(app):
    app.s3 = make_s3_stub({"inv.csv": INVENTORY_CSV})
    post(app, {"bucket": "b-x", "key": "inv.csv"})
    ids1 = {c.kwargs["Item"]["document_id"] for c in app._test_tables["documents"].put_item.call_args_list}

    app._test_tables["documents"].put_item.reset_mock()
    post(app, {"bucket": "b-x", "key": "inv.csv"})  # re-import is idempotent
    ids2 = {c.kwargs["Item"]["document_id"] for c in app._test_tables["documents"].put_item.call_args_list}

    assert ids1 == ids2
    assert len(ids1) == 3
    assert all(d == str(uuid.UUID(d)) for d in ids1)


# ---------- access logs ----------

# One real S3 server access log line (GET with quoted path), one POST,
# one failed GET (403), one HEAD, one bad line. The quoted referrer
# contains "GET /" to prove parsing is anchored on the request field.
LOG_TXT = b"\n".join(
    [
        b'79a5 owner b-x [01/Mar/2026:00:00:00 +0000] 1.2.3.4 requester ARN - "GET /b-x/docs/report.pdf HTTP/1.1" 200 - 1131 82 - "-" "agent" - sig -',
        b'79a5 owner b-x [02/Mar/2026:10:00:00 +0000] 1.2.3.4 requester ARN - "POST /b-x/docs/report.pdf?uploads HTTP/1.1" 200 - 10 82 - "-" "agent" - sig -',
        b'79a5 owner b-x [03/Mar/2026:00:00:00 +0000] 1.2.3.4 requester ARN - "GET /b-x/docs/report.pdf HTTP/1.1" 403 - 0 82 - "-" "agent" - sig -',
        b'79a5 owner b-x [04/Mar/2026:00:00:00 +0000] 1.2.3.4 requester ARN - "HEAD /b-x/docs/report.pdf HTTP/1.1" 200 - 0 82 - "-" "agent" - sig -',
        b"not a log line",
        b'79a5 owner b-x [05/Mar/2026:12:30:00 +0000] 1.2.3.4 requester ARN - "GET /b-x/prefix%20dir/nested%20name.pdf HTTP/1.1" 200 - 20 82 - "-" "agent" - sig -',
    ]
)


def test_access_log_import_records_successful_gets_only(app):
    app.s3 = make_s3_stub({"logs/a.txt": LOG_TXT})
    listed_page = {"Contents": [{"Key": "logs/a.txt"}]}
    pag = MagicMock()
    pag.paginate.return_value = [listed_page]
    app.s3.get_paginator.return_value = pag

    result = post(app, {"bucket": "b-x", "prefix": "logs/", "_force": 1}, path="/imports/access-logs")

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["files_scanned"] == 1
    assert body["events_recorded"] == 2  # the two 200/206/304 GETs

    written = app._test_tables["access"].put_item.call_args_list
    items = [c.kwargs["Item"] for c in written]
    types = {i["access_type"] for i in items}
    assert types == {"DOWNLOAD"}

    ids = {i["document_id"] for i in items}
    assert app.document_id_for("b-x", "docs/report.pdf") in ids
    assert app.document_id_for("b-x", "docs/report.pdf") in ids  # stable
    # URL-decoding ran on log keys
    assert app.document_id_for("b-x", "prefix dir/nested name.pdf") in ids
    # timestamps come from the log, not from now()
    stamps = sorted(i["access_timestamp"] for i in items)
    assert stamps[0].startswith("2026-03-01")
    assert stamps[1].startswith("2026-03-05")


def test_access_log_import_gz(app):
    gz = gzip.compress(LOG_TXT)
    app.s3 = make_s3_stub({"logs/a.txt.gz": gz})
    pag = MagicMock()
    pag.paginate.return_value = [{"Contents": [{"Key": "logs/a.txt.gz"}]}]
    app.s3.get_paginator.return_value = pag

    result = post(app, {"bucket": "b-x", "prefix": "logs/", "_force": 1}, path="/imports/access-logs")
    assert json.loads(result["body"])["events_recorded"] == 2


def test_access_log_bad_input(app):
    app.s3 = MagicMock()
    result = app.lambda_handler(
        {"requestContext": {"http": {"method": "POST", "path": "/imports/access-logs"}}, "body": "{}"}, {}
    )
    assert result["statusCode"] == 400


def test_unknown_route_404(app):
    app.s3 = MagicMock()
    result = app.lambda_handler(
        {"requestContext": {"http": {"method": "GET", "path": "/imports/nope"}}, "body": None}, {}
    )
    assert result["statusCode"] == 404


# ---------- preflight check ----------

# Range reads in the check route are partial bodies on the real API —
# the stub returns a Body that can read, matching _read_text, while
# get_object records whether a Range was requested.


def make_s3_check_stub(
    objects: dict[str, bytes] | None = None,
    listing: dict[str, list] | None = None,
):
    """Stub for the preflight route: head_bucket succeeds, `objects`
    serves head_object/get_object (Range-aware), `listing` maps a
    prefix to paginator pages ([{Contents: [...]}] shaped)."""

    from botocore.exceptions import ClientError

    s3 = MagicMock()
    objects = objects or {}
    listing = listing or {}

    class Body:
        def __init__(self, data: bytes):
            self.data = data

        def read(self):
            return self.data

        def iter_chunks(self, chunk_size=1024):
            for i in range(0, len(self.data), chunk_size):
                yield self.data[i:i + chunk_size]

        def close(self):
            pass

    def get_object(Bucket, Key, Range=None, **_):
        # Range reads ("bytes=0-N") slice like the real API, with the
        # last byte offset INCLUDED in what comes back.
        content = objects[Key] if Key in objects else None
        if content is None:
            raise RuntimeError(f"unexpected object {Key}")
        if Range:
            last = Range.partition("=")[2].partition("-")[2]
            if last:
                content = content[: int(last) + 1]
        return {"Body": Body(content)}

    s3.get_object.side_effect = get_object

    def head_object(Bucket, Key):
        if Key not in objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
            )
        return {"ContentLength": len(objects[Key])}

    s3.head_object.side_effect = head_object

    s3.head_bucket.return_value = {}

    paginator = MagicMock()
    paginator.paginate.side_effect = lambda **kw: iter(
        listing.get(kw.get("Prefix", ""), [])
    )
    s3.get_paginator.return_value = paginator

    return s3


def check(app, body):
    return app.lambda_handler(
        {
            "requestContext": {"http": {"method": "POST", "path": "/imports/check"}},
            "body": json.dumps(body),
        },
        {},
    )


def test_check_unreachable_bucket_reports_and_stops(app):
    from botocore.exceptions import ClientError

    s3 = make_s3_check_stub()
    s3.head_bucket.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
    )
    app.s3 = s3

    result = check(app, {"bucket": "secret-bucket"})

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["reachable"] is False
    assert body["ready"] is False
    assert "404" in " ".join(body["notes"])
    # The listing/discovery work never happens on an unreadable bucket.
    assert "mode" not in body["inventory"]
    s3.get_paginator.assert_not_called()


def test_check_with_explicit_manifest_key_sniffs_columns(app):
    app.s3 = make_s3_check_stub(objects={"inv/schema.csv": INVENTORY_CSV})

    result = check(app, {"bucket": "b-x", "key": "inv/schema.csv"})

    body = json.loads(result["body"])
    assert body["reachable"] is True
    inv = body["inventory"]
    assert inv["exists"] is True
    assert inv["columns_ok"] is True
    assert "missing_columns" not in inv
    assert body["ready"] is True
    # The header sniff was a bounded Range read, not the whole file.
    range_kwargs = app.s3.get_object.call_args.kwargs
    assert range_kwargs.get("Range", "").startswith("bytes=0-")
    # Nothing was imported by the check.
    app._test_tables["documents"].put_item.assert_not_called()


def test_check_manifest_key_missing_is_reported_not_raised(app):
    app.s3 = make_s3_check_stub(objects={})

    result = check(app, {"bucket": "b-x", "key": "inv/absent.csv"})

    body = json.loads(result["body"])
    assert body["inventory"]["exists"] is False
    assert body["ready"] is False


def test_check_discovery_finds_only_schema_objects(app):
    app.s3 = make_s3_check_stub(
        listing={
            "inventory/": [
                {
                    "Contents": [
                        {"Key": "inventory/site/Hive/schema.csv"},
                        {"Key": "inventory/site/data-1.csv.gz"},
                        {"Key": "inventory/site/logs/file.log"},
                    ]
                }
            ]
        }
    )

    result = check(app, {"bucket": "b-x"})

    body = json.loads(result["body"])
    inv = body["inventory"]
    assert inv["candidates"] == ["inventory/site/Hive/schema.csv"]
    assert body["ready"] is True


def test_check_access_logs_sampled_and_reported(app):
    app.s3 = make_s3_check_stub(
        objects={"logs/2026-09-15.log": LOG_TXT[:512]},
        listing={
            "logs/": [
                {
                    "Contents": [
                        {"Key": "logs/a-schema.csv"},  # sorts first, parses 0
                        {"Key": "logs/2026-09-15.log"},
                        {"Key": "logs/2026-09-16.log"},
                    ]
                }
            ]
        },
    )
    # The sampler only probes what the listing returned.
    app.s3.get_object.side_effect = None
    bodies = {
        "logs/a-schema.csv": b"version,bucket,Key,Size\n",
        "logs/2026-09-15.log": LOG_TXT[:512],
    }

    class Body:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

    def get_object(Bucket, Key, Range=None, **_):
        return {"Body": Body(bodies[Key])}

    app.s3.get_object.side_effect = get_object

    result = check(app, {"bucket": "b-x", "prefix": "logs/"})

    body = json.loads(result["body"])
    logs = body["access_logs"]
    assert logs["files_found"] == 3
    # The probe walks past the unparseable schema file and stops at
    # the file that DOES parse — a lexicographic first key is not
    # treated as a bad format.
    assert logs["sampled_files"] == [
        "logs/a-schema.csv", "logs/2026-09-15.log"
    ]
    assert logs["sample_parse_success"] >= 1
    assert "Preflight only" in body["basis"]


def test_check_access_log_prefix_empty_is_a_reported_gap(app):
    app.s3 = make_s3_check_stub(
        objects={"inv/schema.csv": INVENTORY_CSV},
        listing={"": []},
    )

    result = check(app, {"bucket": "b-x", "key": "inv/schema.csv", "prefix": "logs/"})

    body = json.loads(result["body"])
    assert body["access_logs"]["files_found"] == 0
    assert any("recency" in note for note in body["access_logs"]["notes"])
    # Inventory still makes the check ready; logs are a recorded gap.
    assert body["ready"] is True


def test_check_requires_bucket(app):
    app.s3 = make_s3_check_stub()
    result = check(app, {})
    assert result["statusCode"] == 400

# ---------- streaming reads (added hardening) ----------

class _ChunkedBody:
    """StreamingBody stand-in delivering fixed-size chunks."""

    def __init__(self, data):
        self.data = data

    def iter_chunks(self, chunk_size=1024):
        for i in range(0, len(self.data), chunk_size):
            yield self.data[i:i + chunk_size]

    def close(self):
        pass


def test_inventory_lines_split_across_chunks_reassemble(app, monkeypatch):
    """A CSV line/UTF-8 char cut in the middle by a chunk boundary
    must never yield half a line or a mangled row — the importer
    receives complete records even at pathological chunk sizes."""

    monkeypatch.setattr(app, "INGEST_CHUNK_SIZE", 7)
    app.s3 = MagicMock()
    app.s3.get_object.return_value = {"Body": _ChunkedBody(INVENTORY_CSV)}

    result = post(app, {"bucket": "b-x", "key": "inv.csv"})

    assert result["statusCode"] == 200

    body = json.loads(result["body"])

    assert body["documents_imported"] == 3
    assert body["documents_skipped_unsupported_class"] == 1

    written = app._test_tables["documents"].put_item.call_args_list

    # The record row survived its many splits intact.
    assert {
        c.kwargs["Item"]["object_key"] for c in written
    } == {"docs/report.pdf", "docs/old.pdf", "docs/deep.pdf"}


def test_access_logs_scan_across_gz_chunk_boundaries(app, monkeypatch):
    """A .gz stream decompressed incrementally yields the SAME events
    regardless of where the compressed chunks fall — the decompressor
    is held across chunks and the tail is flushed at EOF."""

    monkeypatch.setattr(app, "INGEST_CHUNK_SIZE", 3)
    app.s3 = MagicMock()
    app.s3.get_object.return_value = {
        "Body": _ChunkedBody(gzip.compress(LOG_TXT))
    }
    pag = MagicMock()
    pag.paginate.return_value = [{"Contents": [{"Key": "logs/a.txt.gz"}]}]
    app.s3.get_paginator.return_value = pag

    result = post(
        app, {"bucket": "b-x", "prefix": "logs/", "_force": 1},
        path="/imports/access-logs",
    )

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["events_recorded"] == 2


def test_oversize_manifest_is_413_not_an_oom(app, monkeypatch):
    """An object over the byte budget aborts with 413 before the
    pipeline attempts to hold it, and before any row is written."""

    app.s3 = MagicMock()
    big = b"Key,Size,StorageClass\n" + b"x" * 3_000_000
    app.s3.get_object.return_value = {"Body": _ChunkedBody(big)}
    # Route-level exception clauses reference s3.exceptions.NoSuchKey;
    # give the mock a real exception class like make_s3_stub does.
    app.s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    # Shrink the budget so the test stays tiny.
    monkeypatch.setattr(app, "MAX_INGEST_BYTES", 1024)

    result = post(app, {"bucket": "b-x", "key": "inv.csv"})

    assert result["statusCode"] == 413
    assert "byte budget" in json.loads(result["body"])["error"]
    assert app._test_tables["documents"].put_item.call_count == 0


def test_oversize_log_file_is_skipped_and_reported(app, monkeypatch):
    monkeypatch.setattr(app, "MAX_INGEST_BYTES", 1024)

    gz = gzip.compress(b"Key,Size\n" + b"x" * 3_000_000)
    app.s3 = MagicMock()
    app.s3.get_object.return_value = {"Body": _ChunkedBody(gz)}
    pag = MagicMock()
    pag.paginate.return_value = [{"Contents": [{"Key": "logs/big.gz"}]}]
    app.s3.get_paginator.return_value = pag

    result = post(
        app, {"bucket": "b-x", "prefix": "logs/", "_force": 1},
        path="/imports/access-logs",
    )

    assert result["statusCode"] == 200

    body = json.loads(result["body"])

    assert body["files_scanned"] == 1
    assert body["events_recorded"] == 0
    assert body["files_skipped_too_large"] == 1
    assert not body["files_failed"]  # too-large is its own outcome


def test_imports_stamp_the_calling_actor(app):
    app.s3 = make_s3_stub({"inv.csv": INVENTORY_CSV, "logs.txt": LOG_TXT})

    claims = {"email": "operator@company.io", "sub": "u-123"}

    # actor stamps come from the JWT claims; post() carries none, so
    # emulate the API surface with claims-bearing events.
    event = {
        "requestContext": {
            "http": {"method": "POST", "path": "/imports/inventory"},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "body": json.dumps({"bucket": "b-x", "key": "inv.csv"}),
    }
    result = app.lambda_handler(event, {})

    body = json.loads(result["body"])

    assert body["imported_by"] == "operator@company.io"

    written = app._test_tables["documents"].put_item.call_args_list

    assert all(
        c.kwargs["Item"]["imported_by"] == "operator@company.io"
        for c in written
    )
