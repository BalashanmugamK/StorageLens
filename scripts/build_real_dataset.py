"""
Build a real-world public legal document corpus (Phase A).

Defaults to the USCOURTS corpus used by the Phase B real-world
test, but is dataset-agnostic: any GovInfo collection with
document PDFs (USCOURTS from any court code, CFR, USC, FR, ...)
works by passing --collection / --court-code / --query. A wholly
non-GovInfo legal corpus works too by writing a manifest with
the same schema (document_id, file_name, file_size_bytes,
content_type, sha256) next to its pdfs/ directory and loading it
with load_real_data.py --dataset-dir.

Prerequisites:
    A free GovInfo API key from https://www.govinfo.gov/api-signup
    exported as the GOVINFO_API_KEY environment variable (required
    only when building from GovInfo).
    The key is read from the environment only and is never
    written to disk by this script.

What it does:
    1. Discovers document granules for the configured collection
       (and optional court code) via the GovInfo Search API
       (paginated).
    2. Performs final date filtering CLIENT-SIDE on the displayed
       dateIssued field, because GovInfo's server-side
       dateIssued:[.. TO ..] range filter can operate on a different
       internal date field than the displayed one (observed live).
    3. Inspects one candidate PDF per entry with HTTP HEAD only:
       requires HTTP 200, Content-Type application/pdf, and a known
       Content-Length before anything is downloaded.
    4. Selects exactly 500 documents deterministically with a
       size-representative stratified selection.
    5. Rejects any single document > 3 MB and enforces a cumulative
       corpus limit of 300 MB. Neither limit is ever silently
       exceeded: if the selection cannot fit, the script errors out.
    6. Downloads the selected PDFs individually from
       https://www.govinfo.gov/content/pkg/{packageId}/pdf/{name}.pdf
    7. Validates the corpus and writes manifest.json.

Usage:
    python scripts/build_real_dataset.py --plan     # discovery + HEAD + selection plan only
    python scripts/build_real_dataset.py            # full build (reuses plan caches)
    python scripts/build_real_dataset.py --refresh  # ignore caches, rebuild from scratch
    # Another public legal corpus:
    python scripts/build_real_dataset.py --collection CFR \
        --dataset-dir cfr_2024 --plan

No AWS resources are touched by this script.
"""

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
import urllib.request
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

API_BASE = "https://api.govinfo.gov"
CONTENT_BASE = "https://www.govinfo.gov/content/pkg"

# Defaults build the Phase B USCOURTS corpus; --collection /
# --court-code / --query / --dataset-dir / --target-count select
# any other public legal corpus. main() reassigns these module
# globals from the CLI arguments before any discovery runs.
TARGET_COUNT = 500
COLLECTION = "USCOURTS"
COURT_CODE = "dcd"

CORPUS_DIR = REPO_ROOT / "data" / "real" / "us_courts"
DOWNLOAD_DIR = CORPUS_DIR / "pdfs"
DISCOVERY_CACHE = CORPUS_DIR / "discovery_cache.json"
SIZE_CACHE = CORPUS_DIR / "candidates_sized.json"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

HEAD_CANDIDATE_COUNT = 1200
MAX_SINGLE_FILE_BYTES = 3_000_000
MAX_TOTAL_BYTES = 300_000_000

PAGE_SIZE = 1000
REQUEST_TIMEOUT = 60
DOWNLOAD_WORKERS = 5
RETRIES = 3
PDF_MAGIC = b"%PDF"


# ---------------------------------------------------------------------------
# GovInfo API discovery
# ---------------------------------------------------------------------------

def get_api_key():
    """Read the GovInfo API key from the environment."""

    import os

    api_key = os.environ.get("GOVINFO_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "GOVINFO_API_KEY is not set. Register a free key at "
            "https://www.govinfo.gov/api-signup and export it:\n"
            "  export GOVINFO_API_KEY=<your key>"
        )

    return api_key


def search_page(api_key, query, offset_mark, page_size):
    """Run one GovInfo Search API page and return the parsed body."""

    url = f"{API_BASE}/search?api_key={api_key}"
    body = json.dumps(
        {
            "query": query,
            "pageSize": page_size,
            "offsetMark": offset_mark,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Search API returned HTTP {response.status}"
                    )

                return json.load(response)

        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            last_error = error
            time.sleep(2 * attempt)

    raise RuntimeError(f"Search API failed after {RETRIES} attempts: {last_error}")


def build_default_query():
    """
    Default GovInfo query for the configured collection. The
    courtcode clause applies only to the USCOURTS collection;
    other collections pass --query for a freeform filter.
    """

    query = f"collection:({COLLECTION})"

    if COURT_CODE:
        query += f" AND courtcode:({COURT_CODE})"

    return query


def discover_granules(api_key, refresh=False, query=None):
    """
    Collect all granules matching the configured query.

    The query intentionally has no server-side date filter: GovInfo's
    dateIssued range filter does not reliably match the displayed
    dateIssued field, so date filtering happens client-side in
    filter_by_date(). The full page walk is cached so plan/build rerun
    cheaply and the candidate pool is identical inside one build.

    Entries without a granuleId (package-level collections) are
    keyed by packageId, so collections that publish one PDF per
    package are discovered too.
    """

    if query is None:
        query = build_default_query()

    if DISCOVERY_CACHE.exists() and not refresh:
        cached = json.load(DISCOVERY_CACHE.open("r", encoding="utf-8"))

        if cached.get("query") == f"collection:({COLLECTION}) AND courtcode:({COURT_CODE})":
            return cached["results"]

    query = f"collection:({COLLECTION}) AND courtcode:({COURT_CODE})"

    results = []
    offset_mark = "*"

    while offset_mark:
        page = search_page(api_key, query, offset_mark, PAGE_SIZE)

        results.extend(page.get("results", []))

        print(
            f"  discovered {len(results)} / {page.get('count', '?')} granules",
            end="\r",
            flush=True,
        )

        # GovInfo search returns the pagination cursor in the
        # response's opaque "offsetMark" token (not nextOffsetMark);
        # when it is missing/null all pages have been fetched.
        offset_mark = page.get("offsetMark")

        if not page.get("results"):
            break

    print()

    # Guard against an entry sliding across cursor-page boundaries.
    unique = {}

    for granule in results:
        key = granule.get("granuleId") or granule.get(
            "packageId"
        )

        if key:
            unique[key] = granule

    results = list(unique.values())

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    with DISCOVERY_CACHE.open("w", encoding="utf-8") as file:
        json.dump({"query": query, "results": results}, file)

    return results


def filter_by_date(results):
    """
    Client-side date filtering on the DISPLAYED dateIssued field.

    Returns the chosen target year and the filtered candidates.
    The year is chosen deterministically: the most frequent year is
    only eligible if it is a completed year (at least two years old),
    so the candidate pool stays stable as GovInfo continues to ingest
    new opinions. Ties break to the earliest eligible year.
    """

    dated = [r for r in results if r.get("dateIssued")]

    without_date = len(results) - len(dated)

    years = Counter(r["dateIssued"][:4] for r in dated)

    latest_completed = max(int(y) for y in years) - 2

    eligible = {
        y: n for y, n in years.items()
        if int(y) <= latest_completed and n >= TARGET_COUNT
    }

    if not eligible:
        raise RuntimeError(
            f"No completed year has >= {TARGET_COUNT} granules. "
            f"Year histogram: {dict(sorted(years.items()))}"
        )

    target_year = min(
        eligible,
        key=lambda y: (-eligible[y], y),
    )

    chosen = sorted(
        (r for r in dated if r["dateIssued"].startswith(target_year)),
        key=lambda r: r["granuleId"],
    )

    return target_year, chosen, without_date, dict(sorted(years.items()))


# ---------------------------------------------------------------------------
# HEAD sizing (no downloads)
# ---------------------------------------------------------------------------

def content_url(package_id, granule_id):
    """
    Permanent no-auth direct PDF URL for one entry. Granule-based
    collections use the granule PDF; collections that publish a
    single package-level PDF use the package as its own granule.
    """

    if not granule_id:
        granule_id = package_id

    return f"{CONTENT_BASE}/{package_id}/pdf/{granule_id}.pdf"


def head_pdf(url):
    """
    HEAD one PDF URL.

    Returns (status, content_type, content_length). Any missing or
    non-PDF response is reported, never downloaded.
    """

    request = urllib.request.Request(url, method="HEAD")

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length")

        return str(status), content_type.split(";")[0].strip(), content_length

    except (urllib.error.URLError, urllib.error.HTTPError) as error:
        return str(getattr(error, "code", "ERR")), None, None


def size_candidates(candidates, refresh=False):
    """
    HEAD every candidate PDF and keep only downloadable, valid-sized
    documents. Deterministic stride across the granuleId-sorted pool
    picks the HEAD sample.
    """

    if SIZE_CACHE.exists() and not refresh:
        cached = json.load(SIZE_CACHE.open("r", encoding="utf-8"))
        return cached["sized"], cached["rejected"]

    step = max(1, len(candidates) // HEAD_CANDIDATE_COUNT)

    sample = candidates[::step][:HEAD_CANDIDATE_COUNT]

    sized = []
    rejected = []
    done = 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(head_pdf, content_url(r["packageId"], r["granuleId"])): r  # noqa: E501
            for r in sample
        }

        for future in futures:
            candidate = futures[future]
            status, content_type, content_length = future.result()
            done += 1

            if done % 100 == 0:
                print(f"  sized {done} / {len(sample)}", end="\r", flush=True)

            size_bytes = None

            if content_length is not None:
                try:
                    size_bytes = int(content_length)
                except ValueError:
                    size_bytes = None

            valid = (
                status == "200"
                and content_type == "application/pdf"
                and size_bytes is not None
                and size_bytes > 0
            )

            entry = {
                "package_id": candidate["packageId"],
                "granule_id": (
                    candidate.get("granuleId")
                    or candidate["packageId"]
                ),
                "date_issued": candidate["dateIssued"],
                "court": (candidate.get("governmentAuthor") or [""])[0],
                "source_url": content_url(candidate["packageId"], candidate["granuleId"]),  # noqa: E501
                "http_status": status,
                "content_type": content_type,
                "file_size_bytes": size_bytes,
            }

            if not valid:
                entry["reject_reason"] = (
                    f"HEAD status={status} type={content_type} len={content_length}"
                )
                rejected.append(entry)
                continue

            if size_bytes > MAX_SINGLE_FILE_BYTES:
                entry["reject_reason"] = (
                    f"size {size_bytes} > MAX_SINGLE_FILE_BYTES "
                    f"{MAX_SINGLE_FILE_BYTES}"
                )
                rejected.append(entry)
                continue

            sized.append(entry)

    print()

    sized.sort(key=lambda e: e["granule_id"])

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    with SIZE_CACHE.open("w", encoding="utf-8") as file:
        json.dump({"sized": sized, "rejected": rejected}, file)

    return sized, rejected


# ---------------------------------------------------------------------------
# Deterministic size-representative selection
# ---------------------------------------------------------------------------

def select_documents(sized):
    """
    Select exactly TARGET_COUNT documents.

    Method: split the sized pool into TARGET_COUNT equal-frequency
    quantile bands over file size, then take band quotas in
    granuleId order. This reproduces the pool's size distribution
    (small orders through long opinions) instead of taking the first
    500 smallest, while every step is deterministic. If the selection
    exceeds the cumulative limit, the largest selected documents are
    swapped for the smallest unselected ones; if it still cannot fit,
    the build stops with an error rather than silently exceeding.
    """

    pool = sorted(sized, key=lambda e: e["file_size_bytes"])
    pool_count = len(pool)

    if pool_count < TARGET_COUNT:
        raise RuntimeError(
            f"Only {pool_count} valid candidates available, "
            f"need {TARGET_COUNT}. Broaden the query or date "
            f"window."
        )

    band_size = pool_count / TARGET_COUNT

    chosen = []
    used = set()

    for band in range(TARGET_COUNT):
        start = math.floor(band * band_size)
        end = math.floor((band + 1) * band_size)

        end = max(end, start + 1)

        band_items = sorted(
            pool[start:end],
            key=lambda e: e["granule_id"],
        )

        # Deterministic pick inside the band: middle item in
        # granuleId order (stable, avoids always taking the first).
        pick = band_items[len(band_items) // 2]

        while pick["granule_id"] in used and band_items:
            band_items.remove(pick)
            pick = band_items[len(band_items) // 2]

        used.add(pick["granule_id"])
        chosen.append(pick)

    # Enforce cumulative budget: swap largest selected for smallest
    # unused until the total fits.
    def total():
        return sum(e["file_size_bytes"] for e in chosen)

    available = [
        e for e in pool
        if e["granule_id"] not in used
        and e["file_size_bytes"] <= MAX_SINGLE_FILE_BYTES
    ]

    available.sort(key=lambda e: (e["file_size_bytes"], e["granule_id"]))

    swaps = 0

    while total() > MAX_TOTAL_BYTES and available:
        largest = max(chosen, key=lambda e: e["file_size_bytes"])
        smallest = available.pop(0)

        if smallest["file_size_bytes"] >= largest["file_size_bytes"]:
            break

        chosen.remove(largest)
        used.discard(largest["granule_id"])
        used.add(smallest["granule_id"])
        chosen.append(smallest)
        swaps += 1

    if total() > MAX_TOTAL_BYTES:
        raise RuntimeError(
            f"Cannot fit {TARGET_COUNT} documents under "
            f"{MAX_TOTAL_BYTES} bytes ({total() / 1e6:.1f} MB). "
            "Stopping instead of silently exceeding the limit."
        )

    if len(chosen) != TARGET_COUNT:
        raise RuntimeError(
            f"Selection produced {len(chosen)} documents, "
            f"expected {TARGET_COUNT}"
        )

    chosen.sort(key=lambda e: e["granule_id"])

    return chosen, swaps


# ---------------------------------------------------------------------------
# Download + validation
# ---------------------------------------------------------------------------

def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)

    return digest.hexdigest()


def download_one(entry):
    """Download one PDF to the corpus directory and verify it is a PDF."""

    file_name = (
        f"{entry['granule_id'] or entry['package_id']}.pdf"
    )
    path = DOWNLOAD_DIR / file_name

    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            request = urllib.request.Request(entry["source_url"])

            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")

                data = response.read()

            if not data.startswith(PDF_MAGIC):
                raise RuntimeError("Response does not start with %PDF magic")

            path.write_bytes(data)

            if len(data) != entry["file_size_bytes"]:
                raise RuntimeError(
                    f"Downloaded {len(data)} bytes, HEAD reported "
                    f"{entry['file_size_bytes']}"
                )

            return file_name, None

        except Exception as error:  # noqa: BLE001 - download loop
            last_error = error
            time.sleep(2 * attempt)

    return file_name, str(last_error)


def build_corpus(chosen):
    """Download all selected PDFs with modest concurrency."""

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    failures = []
    done = 0

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(download_one, e): e for e in chosen}

        for future in futures:
            file_name, error = future.result()
            done += 1

            if done % 25 == 0:
                print(f"  downloaded {done} / {len(chosen)}", end="\r", flush=True)

            if error:
                failures.append((file_name, error))

    print()

    return failures


def write_manifest(chosen):
    """Write the SHA-256 manifest for the downloaded corpus."""

    manifest = []

    for entry in chosen:
        file_name = (
            f"{entry['granule_id'] or entry['package_id']}.pdf"
        )
        path = DOWNLOAD_DIR / file_name

        manifest.append(
            {
                "document_id": entry["granule_id"]
                or entry["package_id"],
                "package_id": entry["package_id"],
                "granule_id": entry["granule_id"],
                "file_name": file_name,
                "source_url": entry["source_url"],
                "court": entry["court"],
                "date_issued": entry["date_issued"],
                "file_size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "content_type": "application/pdf",
            }
        )

    manifest.sort(key=lambda m: m["document_id"])

    dataset_label = f"govinfo {COLLECTION}"

    if COURT_CODE:
        dataset_label += f" ({COURT_CODE.upper()})"

    with MANIFEST_PATH.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "dataset": dataset_label,
                "source": (
                    "https://www.govinfo.gov/app/collection/"
                    f"{COLLECTION.lower()}"
                ),
                "court_code": COURT_CODE,
                "license": "Public domain (17 U.S.C. § 105; GPO no-rights policy)",  # noqa: E501
                "license_url": "https://www.govinfo.gov/lib/search-policies.html",
                "document_count": len(manifest),
                "documents": manifest,
            },
            file,
            indent=1,
        )


def validate_corpus():
    """Full validation pass over the downloaded corpus and manifest."""

    problems = []

    pdfs = sorted(DOWNLOAD_DIR.glob("*.pdf"))

    if len(pdfs) != TARGET_COUNT:
        problems.append(
            f"{len(pdfs)} PDFs on disk, expected {TARGET_COUNT}"
        )

    with MANIFEST_PATH.open("r", encoding="utf-8") as file:
        manifest = json.load(file)["documents"]

    if len(manifest) != TARGET_COUNT:
        problems.append(
            f"{len(manifest)} manifest entries, expected {TARGET_COUNT}"
        )

    manifest_by_file = {m["file_name"]: m for m in manifest}

    if len(manifest_by_file) != len(manifest):
        problems.append("Duplicate file_name in manifest")

    pairs = [(m["package_id"], m["granule_id"]) for m in manifest]

    if len(set(pairs)) != len(pairs):
        problems.append("Duplicate package_id + granule_id pair in manifest")

    total_bytes = 0
    sizes = []

    for entry in manifest:
        path = DOWNLOAD_DIR / entry["file_name"]

        if not path.exists():
            problems.append(f"Missing file: {entry['file_name']}")
            continue

        size = path.stat().st_size
        total_bytes += size
        sizes.append(size)

        if size != entry["file_size_bytes"]:
            problems.append(f"Size mismatch: {entry['file_name']}")

        if size > MAX_SINGLE_FILE_BYTES:
            problems.append(f"Exceeds single-file limit: {entry['file_name']}")

        with path.open("rb") as file:
            if file.read(4) != PDF_MAGIC:
                problems.append(f"Not a PDF: {entry['file_name']}")

        if sha256_file(path) != entry["sha256"]:
            problems.append(f"SHA-256 mismatch: {entry['file_name']}")

    on_disk = {p.name for p in pdfs}

    for extra in on_disk - set(manifest_by_file):
        problems.append(f"File on disk not in manifest: {extra}")

    if total_bytes > MAX_TOTAL_BYTES:
        problems.append(
            f"Corpus {total_bytes} bytes exceeds {MAX_TOTAL_BYTES} limit"
        )

    return problems, total_bytes, sizes


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def size_stats(sizes):
    return {
        "min": min(sizes),
        "median": statistics.median(sizes),
        "mean": statistics.mean(sizes),
        "max": max(sizes),
    }


def print_plan(target_year, chosen, swaps, sized, rejected, without_date, years):
    """Print the selection plan before any download happens."""

    sizes = [e["file_size_bytes"] for e in chosen]
    total = sum(sizes)
    stats = size_stats(sizes)

    dates = sorted(e["date_issued"] for e in chosen)

    print("=" * 64)
    print("SELECTED CORPUS PLAN (nothing downloaded yet)")
    print("=" * 64)
    print(f"collection:            {COLLECTION}")
    if COURT_CODE:
        print(
            f"court_code:            {COURT_CODE} "
            "(D.C. District, DC Circuit)"
        )
    print(f"discovered granules:   {sum(years.values())}")
    print(f"granules w/o date:     {without_date}")
    print(f"target year (chosen):  {target_year}")
    print(f"HEAD-sized pool:       {len(sized)} valid, {len(rejected)} rejected")
    print(f"selected documents:    {len(chosen)}")
    print(f"budget swaps applied:  {swaps}")
    print(f"total size (HEAD):     {total:,} bytes = {total / 1e6:.1f} MB")
    print(f"single-file limit:     {MAX_SINGLE_FILE_BYTES:,} bytes "
          f"(max selected: {stats['max']:,})")
    print(f"cumulative limit:      {MAX_TOTAL_BYTES:,} bytes "
          f"(headroom: {(MAX_TOTAL_BYTES - total) / 1e6:.1f} MB)")
    print(f"date range:            {dates[0]} .. {dates[-1]}")
    print(f"size min / median / mean / max:")
    print(f"  {stats['min']:,} / {stats['median']:,.0f} / "
          f"{stats['mean']:,.0f} / {stats['max']:,} bytes")
    print("=" * 64)

    if total > MAX_TOTAL_BYTES:
        raise RuntimeError("Plan exceeds 300 MB cumulative limit — not downloading.")


def print_report(manifest, problems, total_bytes, sizes, rejected, without_date, sized):  # noqa: E501
    """Concise final report after the corpus is built and validated."""

    stats = size_stats(sizes)
    dates = sorted(m["date_issued"] for m in manifest)

    print("=" * 64)
    print("PHASE A FINAL REPORT")
    print("=" * 64)
    print(f"documents downloaded:   {len(manifest)}")
    print(f"total bytes:            {total_bytes:,}")
    print(f"total MB:               {total_bytes / 1e6:.1f} MB")
    print(f"min size:               {stats['min']:,} bytes "
          f"({stats['min'] / 1024:.0f} KB)")
    print(f"median size:            {stats['median']:,.0f} bytes "
          f"({stats['median'] / 1024:.0f} KB)")
    print(f"mean size:              {stats['mean']:,.0f} bytes "
          f"({stats['mean'] / 1024:.0f} KB)")
    print(f"max size:               {stats['max']:,} bytes "
          f"({stats['max'] / 1024:.0f} KB)")
    print(f"date range:             {dates[0]} .. {dates[-1]}")
    print(f"HEAD-sized valid pool:  {len(sized)}")
    print(f"rejected candidates:    {len(rejected)} sized-stage + "
          f"{without_date} without dateIssued")

    reasons = Counter()

    for r in rejected:
        for known in ("size", "HEAD status", "type"):
            if known in r.get("reject_reason", ""):
                key = (
                    "oversize >3MB"
                    if known == "size"
                    else ("HEAD failed/non-200" if known == "HEAD status"
                          else "content-type not PDF")
                )
                reasons[key] += 1
                break

    for reason, count in reasons.items():
        print(f"  - {reason}: {count}")

    print(f"output directory:       {DOWNLOAD_DIR}")
    print(f"manifest path:          {MANIFEST_PATH}")
    print(f"validation:             {'PASS' if not problems else 'FAIL'}")

    for problem in problems:
        print(f"  PROBLEM: {problem}")

    print("=" * 64)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build a real-world public legal document corpus "
            "(Phase A)."
        )
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Discovery + HEAD sizing + selection plan only; no downloads.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached discovery/size results and rebuild.",
    )
    parser.add_argument(
        "--collection",
        default="USCOURTS",
        help=(
            "GovInfo collection code (default USCOURTS; e.g. "
            "CFR, USC, FR)."
        ),
    )
    parser.add_argument(
        "--court-code",
        default="dcd",
        help=(
            "GovInfo courtcode filter (default dcd). Pass an "
            "empty string to include every court/entry in the "
            "collection."
        ),
    )
    parser.add_argument(
        "--query",
        default=None,
        help=(
            "Freeform GovInfo search query overriding the "
            "collection/courtcode default."
        ),
    )
    parser.add_argument(
        "--target-count",
        type=int,
        default=500,
        help=(
            "Number of documents to select (default 500). "
            "Lower it for smaller public legal collections."
        ),
    )
    parser.add_argument(
        "--dataset-dir",
        default="us_courts",
        help=(
            "Directory under data/real/ holding the built "
            "corpus and manifest (default us_courts)."
        ),
    )
    args = parser.parse_args()

    global COLLECTION, COURT_CODE, CORPUS_DIR, DOWNLOAD_DIR
    global DISCOVERY_CACHE, SIZE_CACHE, MANIFEST_PATH, TARGET_COUNT

    TARGET_COUNT = args.target_count
    COLLECTION = args.collection
    COURT_CODE = args.court_code

    if args.query:
        effective_query = args.query
    else:
        effective_query = build_default_query()

    CORPUS_DIR = REPO_ROOT / "data" / "real" / args.dataset_dir
    DOWNLOAD_DIR = CORPUS_DIR / "pdfs"
    DISCOVERY_CACHE = CORPUS_DIR / "discovery_cache.json"
    SIZE_CACHE = CORPUS_DIR / "candidates_sized.json"
    MANIFEST_PATH = CORPUS_DIR / "manifest.json"

    api_key = get_api_key()

    print(f"Discovering granules ({effective_query}) ...")
    results = discover_granules(
        api_key,
        refresh=args.refresh,
        query=effective_query,
    )
    print(f"discovered {len(results)} granules total")

    target_year, candidates, without_date, years = filter_by_date(results)
    print(f"target year: {target_year}, candidates: {len(candidates)}")

    print("HEAD-sizing candidates (no downloads)...")
    sized, rejected = size_candidates(candidates, refresh=args.refresh)
    print(f"valid: {len(sized)}, rejected: {len(rejected)}")

    print("Selecting documents deterministically...")
    chosen, swaps = select_documents(sized)

    print_plan(
        target_year, chosen, swaps, sized, rejected, without_date, years
    )

    if args.plan:
        print("Plan mode: not downloading. Re-run without --plan to build.")
        return

    print("Downloading corpus...")
    failures = build_corpus(chosen)

    if failures:
        print(f"{len(failures)} downloads failed:")
        for file_name, error in failures[:20]:
            print(f"  {file_name}: {error}")

        raise SystemExit(1)

    print("Writing manifest...")
    write_manifest(chosen)

    print("Validating corpus...")
    problems, total_bytes, sizes = validate_corpus()

    print_report(
        chosen, problems, total_bytes, sizes, rejected, without_date, sized
    )

    if problems:
        raise SystemExit(1)

    print("Corpus built and validated successfully.")


if __name__ == "__main__":
    main()