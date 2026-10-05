"""
Build the DIVERSIFIED real legal corpus (Phase A-2).

Multiple GovInfo collections, combined into one 500-document
corpus under data/real/diversified/. Reuses the HTTP, search,
discovery, and download helpers from build_real_dataset.py
(Phase A) instead of a parallel downloader; only the per-source
orchestration (quotas, edition-year date rule, extended manifest)
is new.

Target quotas:
    150  U.S. District Court opinions        (ilnd court code)
    100  U.S. Courts of Appeals opinions     (ca9 court code)
     75  U.S. Courts of Appeals opinions     (ca2 court code)
    100  U.S. Code sections                  (USCODE collection)
     75  Code of Federal Regulations         (CFR collection)

LIMITATION (documented, per the fallback rule): GovInfo does not
publish Supreme Court (SCOTUS) opinions -- probes of courtcode:(scotus)
and the author field return no SCOTUS packages -- so the planned
75-slot Supreme Court quota is filled with U.S. Courts of Appeals
(Second Circuit) opinions, the closest public legal collection in
the same workflow. Final distribution: 150 district + 175 appellate
(100 ca9 + 75 ca2) + 100 USC + 75 CFR = 500.

Reuse: search_page / content_url / head_pdf / sha256_file /
download_one / PDF_MAGIC / size limits come from build_real_dataset.

Data quality checks (per candidate and per built document):
    - HTTP 200 HEAD only, Content-Type exactly application/pdf,
    - known Content-Length before download,
    - PDF magic bytes re-validated after download,
    - HEAD size == downloaded size,
    - unique document ids,
    - SHA-256 over every stored file,
    - manifest/file consistency,
    - per-source + global byte budgets (3 MB single, 300 MB total)
      enforced with errors, never silently exceeded.

Usage:
    python scripts/build_diversified_dataset.py --plan
    python scripts/build_diversified_dataset.py
    python scripts/build_diversified_dataset.py --refresh

No AWS resources are touched by this script.
"""

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_real_dataset as bd  # noqa: E402 - reused Phase A helpers


DATASET_DIR = REPO_ROOT / "data" / "real" / "diversified"
DOWNLOAD_DIR = DATASET_DIR / "pdfs"
MANIFEST_PATH = DATASET_DIR / "manifest.json"

# GovInfo search pages walked per source before the walk stops.
# USCOURTS sources with bounded totals (6k-60k granules) fully
# walk; the CFR collection advertises 240k granules, so the walk
# is capped deterministically at a fixed page count.
CFR_MAX_WALK_PAGES = 40

MAX_PAGE_SIZE = 1000

# Sources are keyed by GovInfo court code for USCOURTS entries
# and by collection code otherwise. date_rule:
#   completed_year -> Phase A rule (most frequent year at least
#       two years old, so a continuously-ingested pool stays
#       stable across rebuilds)
#   edition_year    -> annual editions (USCODE/CFR granules are
#       published complete, never ingested incrementally), so the
#       most frequent edition year with >= quota granules is
#       chosen; ties break to the earliest year for determinism.
SOURCE_QUOTAS = [
    {
        "key": "ilnd",
        "collection": "USCOURTS",
        "court_code": "ilnd",
        "quota": 150,
        "date_rule": "completed_year",
        "granule_filter": None,
        "max_walk_pages": None,
        "collection_label": (
            "United States District Court, N.D. Illinois "
            "(Opinions and Orders)"
        ),
        "document_type": "district_court_opinion",
    },
    {
        "key": "ca9",
        "collection": "USCOURTS",
        "court_code": "ca9",
        "quota": 100,
        "date_rule": "completed_year",
        "granule_filter": None,
        "max_walk_pages": None,
        "collection_label": (
            "United States Court of Appeals, Ninth Circuit "
            "(Opinions)"
        ),
        "document_type": "appellate_court_opinion",
    },
    {
        "key": "ca2",
        "collection": "USCOURTS",
        "court_code": "ca2",
        "quota": 75,
        "date_rule": "completed_year",
        "granule_filter": None,
        "max_walk_pages": None,
        "collection_label": (
            "United States Court of Appeals, Second Circuit "
            "(Opinions)"
        ),
        # SCOTUS substitution; see module docstring.
        "document_type": "appellate_court_opinion",
        "fills_quota_of": "us_supreme_court_opinion (unavailable)",
    },
    {
        "key": "uscode",
        "collection": "USCODE",
        "court_code": None,
        "quota": 100,
        "date_rule": "edition_year",
        # Keep only numbered section text granules; the USCODE
        # packages also publish front-matter/index granules that
        # are not sections themselves.
        "granule_filter": lambda g: "-sec" in (g.get("granuleId") or ""),
        "max_walk_pages": None,
        "collection_label": (
            "United States Code (sections)"
        ),
        "document_type": "statute_usc_section",
    },
    {
        "key": "cfr",
        "collection": "CFR",
        "court_code": None,
        "quota": 75,
        "date_rule": "edition_year",
        # Actual CFR section/volume granules only: excludes the
        # alphabetical index book (GPO-CFR-INDEX-*) and the
        # package-level granule the CFR collection also publishes.
        "granule_filter": lambda g: (g.get("granuleId") or "").startswith("CFR-"),
        "max_walk_pages": CFR_MAX_WALK_PAGES,
        "collection_label": "Code of Federal Regulations (sections)",
        "document_type": "cfr_regulation",
    },
]

SCOPUS_LIMITATION = (
    "GovInfo does not publish U.S. Supreme Court opinions; the "
    "75-document SCOTUS quota was filled with U.S. Courts of "
    "Appeals (Second Circuit) opinions instead, and SCOTUS is "
    "documented as an unavailable source rather than fabricated."
)


# ---------------------------------------------------------------------------
# Discovery (per source, bounded)
# ---------------------------------------------------------------------------

def discover_source(api_key, source, refresh=False):
    """
    Walk the GovInfo search results for one source and cache them.

    Mirrors Phase A's page walk (opaque offsetMark cursor), with a
    deterministic page cap for the very large CFR collection.
    """

    cache_path = DATASET_DIR / f"discovery_{source['key']}.json"

    if cache_path.exists() and not refresh:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return cached["granules"], cached["pages_walked"]

    query = f"collection:({source['collection']})"

    if source["court_code"]:
        query += f" AND courtcode:({source['court_code']})"

    print(f"  [{source['key']}] walking pages ({query}) ...")

    granules = []
    seen = set()
    offset_mark = "*"
    pages_walked = 0

    while offset_mark:
        if (
            source["max_walk_pages"] is not None
            and pages_walked >= source["max_walk_pages"]
        ):
            print(
                f"    stopped at page cap {source['max_walk_pages']} "
                f"(collection larger than this bound)"
            )
            break

        page = bd.search_page(api_key, query, offset_mark, MAX_PAGE_SIZE)
        pages_walked += 1

        batch = page.get("results", [])

        for granule in batch:
            granule_id = granule.get("granuleId") or granule.get(
                "packageId"
            )

            if not granule_id:
                continue

            if source["granule_filter"] and not source["granule_filter"](granule):
                continue

            if granule_id in seen:
                continue

            seen.add(granule_id)
            granule["_source_key"] = source["key"]
            granules.append(granule)

        print(
            f"    page {pages_walked}: kept {len(granules)} granules",
            end="\r",
            flush=True,
        )

        offset_mark = page.get("offsetMark")

        if offset_mark and not batch:
            break

    print()

    DATASET_DIR.mkdir(parents=True, exist_ok=True)

    cache_path.write_text(
        json.dumps(
            {
                "query": query,
                "page_cap_pages": source["max_walk_pages"],
                "pages_walked": pages_walked,
                "granule_count": len(granules),
                "granules": granules,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    return granules, pages_walked


# ---------------------------------------------------------------------------
# Date-window selection (per source, reusing Phase A's rule)
# ---------------------------------------------------------------------------

def choose_year(results, quota):
    """
    Pick the target year for one source with Phase A's
    completed-year rule (continuously-ingested USCOURTS granules).
    """

    dated = [r for r in results if r.get("dateIssued")]

    without_date = len(results) - len(dated)

    years = Counter(r["dateIssued"][:4] for r in dated)

    latest_completed = max(int(y) for y in years) - 2

    eligible = {
        y: n
        for y, n in years.items()
        if int(y) <= latest_completed and n >= quota
    }

    if not eligible:
        raise RuntimeError(
            f"No completed year has >= {quota} granules. "
            f"Year histogram: {dict(sorted(years.items()))}"
        )

    target_year = min(eligible, key=lambda y: (-eligible[y], y))

    chosen = sorted(
        (
            r
            for r in dated
            if r["dateIssued"].startswith(target_year)
        ),
        key=lambda r: r["granuleId"],
    )

    return target_year, chosen, without_date, dict(
        sorted(years.items())
    )


def choose_edition_year(results, quota):
    """
    Annual editions (USCODE/CFR): all granules of an edition carry
    the same dateIssued and the edition is published complete, so
    the most frequent year is already stable. Ties break to the
    earliest edition year for determinism.
    """

    dated = [r for r in results if r.get("dateIssued")]

    without_date = len(results) - len(dated)

    years = Counter(r["dateIssued"][:4] for r in dated)

    eligible = {
        y: n for y, n in years.items() if n >= quota
    }

    if not eligible:
        raise RuntimeError(
            f"No edition year has >= {quota} granules. "
            f"Year histogram: {dict(sorted(years.items()))}"
        )

    target_year = min(eligible, key=lambda y: (-eligible[y], y))

    chosen = sorted(
        (
            r
            for r in dated
            if r["dateIssued"].startswith(target_year)
        ),
        key=lambda r: r["granuleId"],
    )

    return target_year, chosen, without_date, dict(
        sorted(years.items())
    )


# ---------------------------------------------------------------------------
# HEAD sizing (no downloads)
# ---------------------------------------------------------------------------

def head_candidates(candidates, source_key, refresh=False):
    """
    HEAD one candidate PDF per date-window entry and keep only
    downloadable, valid-sized documents. Same keep/reject rules
    and limits as Phase A's size_candidates, but keeps the full
    granule metadata (title, authors) for the extended manifest,
    and samples the same 1200-candidate stride cap.
    """

    cache_path = DATASET_DIR / f"sized_{source_key}.json"

    if cache_path.exists() and not refresh:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return cached["sized"], cached["rejected"]

    step = max(
        1,
        len(candidates) // bd.HEAD_CANDIDATE_COUNT,
    )

    sample = candidates[::step][: bd.HEAD_CANDIDATE_COUNT]

    sized = []
    rejected = []
    done = 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                bd.head_pdf,
                bd.content_url(r["packageId"], r.get("granuleId")),
            ): r
            for r in sample
        }

        for future in futures:
            candidate = futures[future]
            status, content_type, content_length = future.result()
            done += 1

            if done % 100 == 0:
                print(
                    f"    sized {done} / {len(sample)}",
                    end="\r",
                    flush=True,
                )

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
                "title": candidate.get("title", ""),
                "source_url": bd.content_url(
                    candidate["packageId"],
                    candidate.get("granuleId"),
                ),
                "http_status": status,
                "content_type": content_type,
                "file_size_bytes": size_bytes,
            }

            if not valid:
                entry["reject_reason"] = (
                    f"HEAD status={status} type={content_type} "
                    f"len={content_length}"
                )
                rejected.append(entry)
                continue

            if size_bytes > bd.MAX_SINGLE_FILE_BYTES:
                entry["reject_reason"] = (
                    f"size {size_bytes} > MAX_SINGLE_FILE_BYTES "
                    f"{bd.MAX_SINGLE_FILE_BYTES}"
                )
                rejected.append(entry)
                continue

            sized.append(entry)

    print()

    sized.sort(key=lambda e: e["granule_id"])

    cache_path.write_text(
        json.dumps(
            {"sized": sized, "rejected": rejected},
            indent=1,
        ),
        encoding="utf-8",
    )

    return sized, rejected


# ---------------------------------------------------------------------------
# Deterministic size-representative selection (per source quota)
# ---------------------------------------------------------------------------

def select_band_quota(pool, quota):
    """
    Select exactly `quota` documents from one source's sized pool
    with Phase A's quantile-band method: equal-frequency size bands,
    middle-in-granuleId-order pick inside each band, deterministic
    budget swap for the smallest unused candidates when needed.

    Mirrors build_real_dataset.select_documents but with the
    per-source quota instead of the module-level TOTAL count.
    """

    if len(pool) < quota:
        raise RuntimeError(
            f"Source pool has only {len(pool)} valid candidates, "
            f"needs {quota}."
        )

    ordered = sorted(pool, key=lambda e: e["file_size_bytes"])

    band_size = len(pool) / quota

    chosen = []
    used = set()

    for band in range(quota):
        start = math.floor(band * band_size)
        end = math.floor((band + 1) * band_size)

        end = max(end, start + 1)

        band_items = sorted(
            ordered[start:end],
            key=lambda e: e["granule_id"],
        )

        pick = band_items[len(band_items) // 2]

        while pick["granule_id"] in used and band_items:
            band_items.remove(pick)
            pick = band_items[len(band_items) // 2]

        used.add(pick["granule_id"])
        chosen.append(pick)

    def total_bytes(entries):
        return sum(e["file_size_bytes"] for e in entries)

    available = [
        e
        for e in pool
        if e["granule_id"] not in used
    ]

    available.sort(
        key=lambda e: (e["file_size_bytes"], e["granule_id"])
    )

    swaps = 0

    while total_bytes(chosen) > bd.MAX_TOTAL_BYTES and available:
        largest = max(
            chosen, key=lambda e: e["file_size_bytes"]
        )
        smallest = available.pop(0)

        if smallest["file_size_bytes"] >= largest["file_size_bytes"]:
            break

        chosen.remove(largest)
        used.discard(largest["granule_id"])
        used.add(smallest["granule_id"])
        chosen.append(smallest)
        swaps += 1

    if total_bytes(chosen) > bd.MAX_TOTAL_BYTES:
        raise RuntimeError(
            f"Source selection cannot fit its quota "
            f"({total_bytes(chosen) / 1e6:.1f} MB)."
        )

    chosen.sort(key=lambda e: e["granule_id"])

    return chosen, swaps


def fit_global_budget(source_choices, sized_by_key):
    """
    Enforce the 300 MB cumulative budget across ALL sources: swap
    the globally largest selected document for its source's
    smallest unselected candidate until the total fits. Stops with
    an error rather than silently exceeding the limit.
    """

    def global_total():
        return sum(
            e["file_size_bytes"]
            for entries in source_choices.values()
            for e in entries
        )

    swaps = 0

    while global_total() > bd.MAX_TOTAL_BYTES:
        biggest_entry = None
        biggest_source = None

        for key, entries in source_choices.items():
            for entry in entries:
                if (
                    biggest_entry is None
                    or entry["file_size_bytes"]
                    > biggest_entry["file_size_bytes"]
                ):
                    biggest_entry = entry
                    biggest_source = key

        pool = sized_by_key[biggest_source]

        available = [
            e
            for e in pool
            if e["granule_id"]
            not in {
                c["granule_id"]
                for c in source_choices[biggest_source]
            }
        ]

        if not available:
            raise RuntimeError(
                "Global 300 MB budget cannot be met: the "
                f"'{biggest_source}' pool has no smaller "
                "replacement candidates. Stopping instead of "
                "silently exceeding the limit."
            )

        available.sort(
            key=lambda e: (e["file_size_bytes"], e["granule_id"])
        )

        smallest = available[0]

        if smallest["file_size_bytes"] >= biggest_entry["file_size_bytes"]:
            raise RuntimeError(
                "Global 300 MB budget cannot be met: no smaller "
                f"replacement exists for '{biggest_source}'. "
                "Stopping instead of silently exceeding."
            )

        source_choices[biggest_source].remove(biggest_entry)
        source_choices[biggest_source].append(smallest)
        swaps += 1

    return swaps


# ---------------------------------------------------------------------------
# Download + validate + manifest
# ---------------------------------------------------------------------------

def download_corpus(chosen_all):
    """Download every selected PDF (Phase A helper, redirected)."""

    failures = bd.build_corpus(chosen_all)

    return failures


def write_manifest(source_choices, source_by_key):
    """
    Write the extended manifest with per-collection metadata
    (collection, document_type, jurisdiction, court, agency).
    """

    manifest = []

    for key, entries in source_choices.items():
        source = source_by_key[key]

        for entry in entries:
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
                    "collection": source["collection"],
                    "document_type": source["document_type"],
                    "jurisdiction": (
                        "United States"
                        if source["collection"] in ("USCOURTS", "USCODE")
                        else "United States (federal agencies)"
                    ),
                    "court": (
                        entry["court"]
                        if source["collection"] == "USCOURTS"
                        else ""
                    ),
                    "agency": (
                        entry["court"]
                        if source["collection"] != "USCOURTS"
                        else ""
                    ),
                    "collection_label": source["collection_label"],
                    "title": entry["title"],
                    "date_issued": entry["date_issued"],
                    "file_size_bytes": path.stat().st_size,
                    "sha256": bd.sha256_file(path),
                    "content_type": "application/pdf",
                }
            )

    manifest.sort(key=lambda m: m["document_id"])

    with MANIFEST_PATH.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "dataset": "GovInfo diversified public legal corpus",
                "source": "https://www.govinfo.gov",
                "collections": sorted(
                    source_by_key,
                    key=lambda k: SOURCE_QUOTAS.index(
                        next(
                            q
                            for q in SOURCE_QUOTAS
                            if q["key"] == k
                        )
                    ),
                ),
                "description": SCOPUS_LIMITATION,
                "license": "Public domain (17 U.S.C. sec. 105; GPO no-rights policy)",
                "license_url": "https://www.govinfo.gov/lib/search-policies.html",
                "document_count": len(manifest),
                "documents": manifest,
            },
            file,
            indent=1,
        )

    return manifest


def validate_corpus(expected_count):
    """Full validation pass over the corpus and extended manifest."""

    problems = []

    pdfs = sorted(DOWNLOAD_DIR.glob("*.pdf"))

    if len(pdfs) != expected_count:
        problems.append(
            f"{len(pdfs)} PDFs on disk, expected {expected_count}"
        )

    raw = json.loads(
        MANIFEST_PATH.read_text(encoding="utf-8")
    )

    manifest = raw["documents"]

    if len(manifest) != expected_count:
        problems.append(
            f"{len(manifest)} manifest entries, expected "
            f"{expected_count}"
        )

    ids = [m["document_id"] for m in manifest]

    if len(set(ids)) != len(ids):
        problems.append("Duplicate document_id in manifest")

    manifest_by_file = {m["file_name"]: m for m in manifest}

    if len(manifest_by_file) != len(manifest):
        problems.append("Duplicate file_name in manifest")

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

        if size > bd.MAX_SINGLE_FILE_BYTES:
            problems.append(
                f"Exceeds single-file limit: {entry['file_name']}"
            )

        with path.open("rb") as file:
            if file.read(4) != bd.PDF_MAGIC:
                problems.append(f"Not a PDF: {entry['file_name']}")

        if bd.sha256_file(path) != entry["sha256"]:
            problems.append(f"SHA-256 mismatch: {entry['file_name']}")

    on_disk = {p.name for p in pdfs}

    for extra in on_disk - set(manifest_by_file):
        problems.append(f"File on disk not in manifest: {extra}")

    if total_bytes > bd.MAX_TOTAL_BYTES:
        problems.append(
            f"Corpus {total_bytes} bytes exceeds "
            f"{bd.MAX_TOTAL_BYTES} limit"
        )

    return problems, total_bytes, sizes, manifest


def print_plan(source_plans):
    """Print the plan for every source before any download."""

    print("=" * 64)
    print("DIVERSIFIED CORPUS PLAN (nothing downloaded yet)")
    print("=" * 64)
    print(SCOPUS_LIMITATION)
    print()

    total = 0

    for plan in source_plans:
        chosen = plan["chosen"]

        sizes = [e["file_size_bytes"] for e in chosen]
        source_total = sum(sizes)
        total += source_total

        dates = sorted(e["date_issued"] for e in chosen)

        print(f"source:    {plan['key']}  ({plan['collection_label']})")
        print(
            f"  granules discovered: {plan['granule_count']} "
            f"over {plan['pages_walked']} page(s)"
        )
        print(
            f"  granules w/o date:   {plan['without_date']}"
        )
        print(f"  target year:         {plan['target_year']}")
        print(
            f"  year histogram:      "
            f"({len(plan['years'])} years, top: "
            f"{sorted(plan['years'].items(), key=lambda kv: -kv[1])[:3]})"
        )
        print(
            f"  HEAD-sized pool:     {len(plan['sized'])} valid, "
            f"{len(plan['rejected'])} rejected"
        )
        print(
            f"  selected / quota:    {len(chosen)} / {plan['quota']} "
            f"(swaps: {plan['swaps']})"
        )
        print(
            f"  source total:        "
            f"{source_total:,} bytes = {source_total / 1e6:.1f} MB"
        )
        print(
            f"  size min/median/mean/max: "
            f"{min(sizes):,} / {statistics.median(sizes):,.0f} / "
            f"{statistics.mean(sizes):,.0f} / {max(sizes):,} bytes"
        )
        print(f"  date range:          {dates[0]} .. {dates[-1]}")
        print()

    print(f"TOTAL: {len(source_plans)} sources; "
          f"{total:,} bytes = {total / 1e6:.1f} MB "
          f"(limit {bd.MAX_TOTAL_BYTES / 1e6:.0f} MB)")

    if total > bd.MAX_TOTAL_BYTES:
        raise RuntimeError(
            "Plan exceeds the 300 MB cumulative budget -- "
            "not downloading."
        )


def print_report(manifest, problems, total_bytes, sizes):
    """Concise final report after the corpus is built."""

    stats = {
        "min": min(sizes),
        "median": statistics.median(sizes),
        "mean": statistics.mean(sizes),
        "max": max(sizes),
    }

    by_collection = Counter(m["collection"] for m in manifest)
    by_type = Counter(m["document_type"] for m in manifest)

    print("=" * 64)
    print("DIVERSIFIED CORPUS FINAL REPORT")
    print("=" * 64)
    print(f"documents downloaded:   {len(manifest)}")
    print(f"total bytes:            {total_bytes:,} "
          f"({total_bytes / 1e6:.1f} MB)")
    print(f"size min/median/mean/max: "
          f"{stats['min']:,} / {stats['median']:,.0f} / "
          f"{stats['mean']:,.0f} / {stats['max']:,} bytes")
    print(f"collection distribution: {dict(by_collection)}")
    print(f"document types:          {dict(by_type)}")
    print(f"limitation:              {SCOPUS_LIMITATION}")
    print(f"manifest path:           {MANIFEST_PATH}")
    print(f"validation:              "
          f"{'PASS' if not problems else 'FAIL'}")

    for problem in problems:
        print(f"  PROBLEM: {problem}")

    print("=" * 64)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build the diversified multi-collection real legal "
            "corpus (Phase A-2)."
        )
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help=(
            "Discovery + HEAD sizing + selection plan only; "
            "no downloads."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached discovery/size results and rebuild.",
    )
    args = parser.parse_args()

    api_key = bd.get_api_key()

    source_by_key = {s["key"]: s for s in SOURCE_QUOTAS}
    expected_total = sum(s["quota"] for s in SOURCE_QUOTAS)
    source_plans = []
    source_choices = {}
    sized_by_key = {}

    for source in SOURCE_QUOTAS:
        print(f"[{source['key']}]")
        granules, pages = discover_source(
            api_key, source, refresh=args.refresh
        )
        print(
            f"  discovered {len(granules)} granules "
            f"over {pages} page(s)"
        )

        filter_fn = (
            source["granule_filter"]
            if source["granule_filter"]
            else None
        )
        pool = granules

        target_year, chosen, without_date, years = (
            choose_year(pool, source["quota"])
            if source["date_rule"] == "completed_year"
            else choose_edition_year(pool, source["quota"])
        )
        print(
            f"  target year: {target_year}, "
            f"candidates: {len(chosen)}"
        )

        sized, rejected = head_candidates(
            chosen, source["key"], refresh=args.refresh
        )
        print(
            f"  valid: {len(sized)}, "
            f"rejected: {len(rejected)}"
        )

        quota_chosen, swaps = select_band_quota(
            sized, source["quota"]
        )
        print(
            f"  selected: {len(quota_chosen)} / "
            f"{source['quota']} (budget swaps: {swaps})"
        )

        sized_by_key[source["key"]] = sized
        source_choices[source["key"]] = quota_chosen

        source_plans.append(
            {
                "key": source["key"],
                "collection_label": source["collection_label"],
                "quota": source["quota"],
                "granule_count": len(granules),
                "pages_walked": pages,
                "without_date": without_date,
                "target_year": target_year,
                "years": years,
                "sized": sized,
                "rejected": rejected,
                "chosen": quota_chosen,
                "swaps": swaps,
            }
        )

    global_swaps = fit_global_budget(
        source_choices, sized_by_key
    )
    chosen_all = [
        entry
        for entries in source_choices.values()
        for entry in entries
    ]

    total_expected = sum(
        e["file_size_bytes"] for e in chosen_all
    )

    print_plan(source_plans)

    if args.plan:
        print("Plan mode: not downloading.")
        return

    print("Downloading corpus...")
    # bd.download_one writes into bd.DOWNLOAD_DIR; redirect it to
    # the diversified corpus directory before downloading.
    bd.DOWNLOAD_DIR = DOWNLOAD_DIR
    failures = download_corpus(chosen_all)

    if failures:
        print(f"{len(failures)} downloads failed:")
        for file_name, error in failures[:20]:
            print(f"  {file_name}: {error}")

        raise SystemExit(1)

    print("Writing extended manifest...")
    manifest = write_manifest(source_choices, source_by_key)

    print("Validating corpus...")
    problems, total_bytes, sizes, manifest = validate_corpus(
        expected_total
    )

    print_report(manifest, problems, total_bytes, sizes)

    if problems:
        raise SystemExit(1)

    print("Diversified corpus built and validated successfully.")


if __name__ == "__main__":
    main()