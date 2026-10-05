"""
Refresh optimization/pricing.json storage rates from the live
AWS Price List API (public catalog, no resource access needed).

The S3 pricing page publishes rates per storage class, but a
few values are NOT exposed through the Price List API's AmazonS3
service products for every region. This script refreshes only
what the API exposes, keeps every other field in the snapshot
(fidelity parameters, assumptions, unrefreshable rates) intact,
and never writes unless --write is passed:

    python scripts/refresh_pricing.py           # dry-run diff
    python scripts/refresh_pricing.py --write   # update the file

Refreshed from the API (per region):
  - storage_per_gb_month   first usage bracket (first 50 TB),
                           usagetype TimedStorage-* lines
  - get_request_per_1000   GET request lines (Tier2)
  - retrieval_request_per_1000  restore-request line (Flexible)
  - retrieval_per_gb       SIA/GIR retrieval fees and archive
                           Standard-Retrieval-Bytes fees
  - transition_per_1000    lifecycle transition lines

Not exposed by the API (kept from the snapshot with a note):
  - GLACIER_DEEP_ARCHIVE storage_per_gb_month
  - GLACIER_DEEP_ARCHIVE retrieval_request_per_1000
Exits 0 on success, 4 when the API does not return the expected
lines for a refreshed field (partial updates are never written).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "optimization"))

PRICING_FILE = (
    REPO_ROOT / "optimization" / "pricing.json"
)

# The Price List API is served from dedicated endpoints.
PRICING_API_REGION = "us-east-1"

# usagetypes carry an optional region-code prefix (APS3- in
# Mumbai, nothing in us-east-1). Anchored suffix patterns keep
# sibling lines (TimedStorage-INT-FA, -SmObjects, Tables, ...)
# out of the matches.
STORAGE_PATTERNS = {
    "STANDARD": r"^(?:[A-Z0-9]{2,6}-)?TimedStorage-ByteHrs$",
    "STANDARD_IA": (
        r"^(?:[A-Z0-9]{2,6}-)?TimedStorage-SIA-ByteHrs$"
    ),
    "GLACIER_INSTANT_RETRIEVAL": (
        r"^(?:[A-Z0-9]{2,6}-)?TimedStorage-GIR-ByteHrs$"
    ),
    "GLACIER_FLEXIBLE_RETRIEVAL": (
        r"^(?:[A-Z0-9]{2,6}-)?TimedStorage-GlacierByteHrs$"
    ),
    "GLACIER_DEEP_ARCHIVE": (
        r"^(?:[A-Z0-9]{2,6}-)?TimedStorage-GlacierDeepArchive-ByteHrs$"
    ),
}

GET_PATTERNS = {
    "STANDARD": r"^(?:[A-Z0-9]{2,6}-)?Requests-Tier2$",
    "STANDARD_IA": r"-Requests-SIA-Tier2$",
    "GLACIER_INSTANT_RETRIEVAL": r"-Requests-GIR-Tier2$",
    "GLACIER_FLEXIBLE_RETRIEVAL": r"-Requests-GLACIER-Tier2$",
    "GLACIER_DEEP_ARCHIVE": r"-Requests-GDA-Tier2$",
}

TRANSITION_OPERATIONS = {
    "S3-SIATransition": "STANDARD_IA",
    "S3-GIRTransition": "GLACIER_INSTANT_RETRIEVAL",
    "S3-GlacierTransition": "GLACIER_FLEXIBLE_RETRIEVAL",
    "S3-GDATransition": "GLACIER_DEEP_ARCHIVE",
}

RETRIEVAL_SPEC = {
    "STANDARD_IA": ("usagetype", r"-Retrieval-SIA$"),
    "GLACIER_INSTANT_RETRIEVAL": ("usagetype", r"-Retrieval-GIR$"),
    "GLACIER_FLEXIBLE_RETRIEVAL": (
        "usagetype",
        r"^(?:[A-Z0-9]{2,6}-)?Standard-Retrieval-Bytes$",
        "RestoreObject",
    ),
    "GLACIER_DEEP_ARCHIVE": (
        "usagetype",
        r"^(?:[A-Z0-9]{2,6}-)?Standard-Retrieval-Bytes$",
        "DeepArchiveRestoreObject",
    ),
}

RESTORE_REQUEST_SPEC = {
    "GLACIER_FLEXIBLE_RETRIEVAL": (
        r"^(?:[A-Z0-9]{2,6}-)?Requests-Tier3$",
        "RestoreObject",
    ),
}

# Expected-but-never-exposed combination of field/class: the
# Price List API publishes no Deep Archive storage product
# despite the pricing page listing $0.00099/GB-Mo. These never
# count against the --write refusal.
KNOWN_UNREFRESHABLE = {
    "storage_per_gb_month/GLACIER_DEEP_ARCHIVE",
}


def list_region_rates(boto3, region):
    """Return every OnDemand (product, first price dimension) rate for one region."""

    pricing = boto3.client(
        "pricing", region_name=PRICING_API_REGION
    )

    rates = []

    next_token = None

    while True:
        kwargs = {
            "ServiceCode": "AmazonS3",
            "Filters": [
                {
                    "Field": "regionCode",
                    "Value": region,
                    "Type": "TERM_MATCH",
                }
            ],
        }

        if next_token:
            kwargs["NextToken"] = next_token

        response = pricing.get_products(**kwargs)

        for raw in response["PriceList"]:
            item = (
                json.loads(raw)
                if isinstance(raw, str)
                else raw
            )

            attributes = item.get("product", {}).get(
                "attributes", {}
            )

            for offer in (
                item.get("terms", {})
                .get("OnDemand", {})
                .values()
            ):
                dims = offer.get(
                    "priceDimensions", {}
                )

                for dim in dims.values():
                    rates.append(
                        {
                            "usagetype": attributes.get(
                                "usagetype", ""
                            ),
                            "operation": attributes.get(
                                "operation", ""
                            ),
                            "unit": dim.get("unit", ""),
                            "begin": dim.get(
                                "beginRange"
                            ),
                            "rate": dim.get(
                                "pricePerUnit", {}
                            ).get("USD"),
                        }
                    )

        next_token = response.get("NextToken")

        if not next_token:
            break

    return rates


def first_bracket_rate(rates, pattern):
    """
    Return the first usage bracket's rate for the best storage
    match (beginRange "0"), or None.
    """

    regex = re.compile(pattern)

    candidates = [
        float(r["rate"])
        for r in rates
        if regex.match(r["usagetype"])
        and r["unit"] == "GB-Mo"
        and r["rate"] is not None
        and str(r.get("begin")) == "0"
    ]

    if not candidates:
        return None

    return min(candidates)


def per_1000_rate(rates, pattern, operation=None):
    """
    Convert a per-unit request rate into its per-1,000 value.
    """

    regex = re.compile(pattern)

    for r in rates:
        if not regex.search(r["usagetype"]):
            continue

        if r["unit"] != "Requests":
            continue

        if operation and r["operation"] != operation:
            continue

        if r["rate"] is None:
            continue

        # First matching usage bracket is the standard tier;
        # round away float noise (0.0004 * 1000 -> 0.4).
        return round(float(r["rate"]) * 1000, 10)

    return None


def retrieval_rate(rates, spec):
    """Match one retrieval-per-GB line from RETRIEVAL_SPEC."""

    if spec is None:
        return None

    field, pattern = spec[0], spec[1]

    operation = (
        spec[2] if len(spec) > 2 else None
    )

    regex = re.compile(pattern)

    for r in rates:
        if field == "usagetype" and not regex.search(
            r["usagetype"]
        ):
            continue

        if r["unit"] != "GB":
            continue

        if operation and r["operation"] != operation:
            continue

        if r["rate"] is None:
            continue

        return float(r["rate"])

    return None


def build_fresh_rates(boto3, region):
    """
    Map price-list rates onto the pricing.json schema.
    Returns (values, unmatched) where values is a per-class
    dict of the refreshable rate fields and unmatched lists
    expected-but-missing lines.
    """

    rates = list_region_rates(boto3, region)

    refreshed = {}
    unmatched = []

    for storage_class, pattern in STORAGE_PATTERNS.items():
        rate = first_bracket_rate(rates, pattern)

        if rate is None:
            unmatched.append(
                f"storage_per_gb_month/{storage_class}"
            )

        refreshed.setdefault(storage_class, {})[
            "storage_per_gb_month"
        ] = rate

    for storage_class, pattern in GET_PATTERNS.items():
        rate = per_1000_rate(rates, pattern)

        if rate is None:
            unmatched.append(
                f"get_request_per_1000/{storage_class}"
            )

        refreshed.setdefault(storage_class, {})[
            "get_request_per_1000"
        ] = rate

    for operation, storage_class in (
        TRANSITION_OPERATIONS.items()
    ):
        rate = per_1000_rate(
            rates,
            r".*",
            operation=operation,
        )

        if rate is None:
            unmatched.append(
                "transition_per_1000/"
                f"{storage_class}"
            )

        refreshed.setdefault(storage_class, {})[
            "transition_per_1000"
        ] = rate

    for storage_class, spec in RETRIEVAL_SPEC.items():
        rate = retrieval_rate(rates, spec)

        if rate is None:
            unmatched.append(
                f"retrieval_per_gb/{storage_class}"
            )

        refreshed.setdefault(storage_class, {})[
            "retrieval_per_gb"
        ] = rate

    for storage_class, spec in (
        RESTORE_REQUEST_SPEC.items()
    ):
        pattern, operation = spec

        rate = per_1000_rate(
            rates, pattern, operation=operation
        )

        if rate is None:
            unmatched.append(
                "retrieval_request_per_1000/"
                f"{storage_class}"
            )

        refreshed.setdefault(storage_class, {})[
            "retrieval_request_per_1000"
        ] = rate

    # STANDARD has no transition or data-retrieval charge.
    refreshed["STANDARD"][
        "retrieval_per_gb"
    ] = refreshed["STANDARD"].get(
        "retrieval_per_gb"
    ) or 0.0
    refreshed["STANDARD"][
        "retrieval_request_per_1000"
    ] = refreshed["STANDARD"].get(
        "retrieval_request_per_1000"
    ) or 0.0

    return rates, refreshed, unmatched


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Refresh pricing.json rates from the AWS Price "
            "List API (dry-run by default)."
        )
    )

    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist the refresh to optimization/pricing.json",
    )

    parser.add_argument(
        "--region",
        default=None,
        help=(
            "Target region; defaults to the pricing.json "
            "region (ap-south-1)."
        ),
    )

    args = parser.parse_args()

    import boto3

    snapshot = json.loads(
        PRICING_FILE.read_text(encoding="utf-8")
    )

    region = args.region or snapshot["region"]

    rates, refreshed, unmatched = build_fresh_rates(
        boto3, region
    )

    expected_fields = [
        "storage_per_gb_month",
        "get_request_per_1000",
        "transition_per_1000",
        "retrieval_per_gb",
    ]

    changes = []

    for storage_class, updates in sorted(
        refreshed.items()
    ):
        tier = snapshot["storage_classes"][
            storage_class
        ]

        for field, value in updates.items():
            if (
                value is None
                and field in expected_fields
            ):
                # Unmatched refreshable field: keep snapshot.
                continue

            if value is None:
                # Unrefreshable field (e.g. Deep Archive
                # restore requests): keep snapshot value.
                changes.append(
                    f"{storage_class}.{field}: kept "
                    f"{tier[field]} (not exposed by the "
                    f"Price List API)"
                )
                continue

            if float(value) != float(
                tier[field]
            ):
                changes.append(
                    f"{storage_class}.{field}: "
                    f"{tier[field]} -> {value}"
                )

            tier[field] = value

    snapshot["pricing_checked"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

    snapshot["pricing_source"] = (
        "AWS S3 Pricing (AWS Price List API, "
        f"region {region}; Deep Archive storage and restore "
        "request rates kept from the manual snapshot)"
    )

    print(f"Region:  {region}")
    print(
        f"Price list products scanned: {len(rates)}"
    )

    if changes:
        print()
        print("CHANGES:")
        for change in changes:
            print(f"  {change}")
    else:
        print()
        print(
            "No changes: snapshot already matches the "
            "current Price List API rates."
        )

    known_missing = [
        name
        for name in unmatched
        if name in KNOWN_UNREFRESHABLE
    ]

    unexpected_unmatched = [
        name
        for name in unmatched
        if name not in KNOWN_UNREFRESHABLE
    ]

    if known_missing:
        print()
        print(
            "Not exposed by the API (snapshot values kept):"
        )

        for name in known_missing:
            print(f"  {name}")

    if unexpected_unmatched:
        print()
        print(
            "WARNING: expected lines missing for:",
            file=sys.stderr,
        )

        for name in unexpected_unmatched:
            print(f"  {name}", file=sys.stderr)

        print(
            "Those fields keep their previous snapshot "
            "values; verify them against the S3 pricing "
            "page manually.",
            file=sys.stderr,
        )

    if args.write:
        unexpected_unmatched = [
            name
            for name in unmatched
            if name not in KNOWN_UNREFRESHABLE
        ]

        if unexpected_unmatched:
            print(
                "ERROR: refusing to write with unmatched "
                "refreshable fields.",
                file=sys.stderr,
            )
            return 4

        PRICING_FILE.write_text(
            json.dumps(snapshot, indent=2) + "\n",
            encoding="utf-8",
        )

        print()
        print(f"Written: {PRICING_FILE}")
    else:
        print()
        print("Dry run: pass --write to persist.")

    return 0


if __name__ == "__main__":
    sys.exit(main())