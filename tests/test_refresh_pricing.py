"""Tests for scripts/refresh_pricing.py.

The refresher maps AWS Price List rows onto the pricing.json schema.
These tests feed hand-built price-list pages to build_fresh_rates
with a stub boto3 and pin the mapping rules:

  - anchored patterns must NOT match sibling lines (S3 Tables rates
    share the TimedStorage-INT-FA-ByteHrs suffix but bill more);
  - first usage bracket only (TimedStorage-INT-FA publishes 3
    volume brackets);
  - per-request lines convert pricePerUnit x 1000;
  - the monitoring fee line is unit "Objects" and converts the
    same way (0.0000025/object-mo -> 0.0025/1,000 object-mo);
  - every expected field that the API does not publish is reported
    as unmatched rather than written as None.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SPEC = importlib.util.spec_from_file_location(
    "refresh_pricing",
    REPO_ROOT / "scripts" / "refresh_pricing.py",
)

refresh_pricing = importlib.util.module_from_spec(SPEC)

sys.modules["refresh_pricing"] = refresh_pricing

SPEC.loader.exec_module(refresh_pricing)


def make_product_row(usagetype, unit, price, operation="", begin="0"):
    """Build one price-list JSON row in list_region_rates' shape."""

    dimension = {
        "unit": unit,
        "pricePerUnit": {"USD": str(price)},
    }

    if begin is not None:
        dimension["beginRange"] = begin

    return {
        "product": {
            "attributes": {
                "usagetype": f"APS3-{usagetype}",
                "operation": operation,
            }
        },
        "terms": {
            "OnDemand": {
                "offer": {
                    "priceDimensions": {"dim": dimension}
                }
            }
        },
    }


def make_pages():
    """A price-list page set carrying every field the refresher
    expects for one region, with rates matching the committed
    snapshot (so a refresh keeps the snapshot unchanged)."""

    rows = [
        # Storage (first brackets of possibly several)
        make_product_row("TimedStorage-ByteHrs", "GB-Mo", 0.025),
        make_product_row(
            "TimedStorage-SIA-ByteHrs", "GB-Mo", 0.0138
        ),
        make_product_row(
            "TimedStorage-GIR-ByteHrs", "GB-Mo", 0.005
        ),
        make_product_row(
            "TimedStorage-GlacierByteHrs", "GB-Mo", 0.0045
        ),
        # Deep Archive storage deliberately omitted: not exposed
        # by the API (KNOWN_UNREFRESHABLE).
        make_product_row(
            "TimedStorage-INT-FA-ByteHrs", "GB-Mo", 0.025
        ),
        make_product_row(
            "TimedStorage-INT-FA-ByteHrs", "GB-Mo", 0.024,
            begin="51200",
        ),
        make_product_row(
            "TimedStorage-INT-FA-ByteHrs", "GB-Mo", 0.023,
            begin="512000",
        ),
        make_product_row(
            "TimedStorage-INT-IA-ByteHrs", "GB-Mo", 0.0138
        ),
        make_product_row(
            "TimedStorage-INT-AIA-ByteHrs", "GB-Mo", 0.005
        ),
        # S3 Tables sibling: must NOT match the IT FA pattern.
        make_product_row(
            "Tables-TimedStorage-INT-FA-ByteHrs",
            "GB-Mo",
            0.0288,
        ),
        # Monitoring fee, per OBJECT-month.
        make_product_row(
            "Monitoring-Automation-INT", "Objects", 0.0000025
        ),
        # GET requests
        make_product_row("Requests-Tier2", "Requests", 0.0000004),
        make_product_row(
            "Requests-INT-Tier2", "Requests", 0.0000004
        ),
        make_product_row(
            "Requests-SIA-Tier2", "Requests", 0.000001
        ),
        make_product_row(
            "Requests-GIR-Tier2", "Requests", 0.00001
        ),
        make_product_row(
            "Requests-GLACIER-Tier2", "Requests", 0.0000004
        ),
        make_product_row(
            "Requests-GDA-Tier2", "Requests", 0.0000004
        ),
        # Also a Tables GET line with the same suffix pattern as
        # IT's GET: the anchored match must skip it, so it bills a
        # Distinguishably different rate.
        make_product_row(
            "Tables-Requests-INT-Tier2",
            "Requests",
            0.0005,
        ),
        # Transition requests (pricePerUnit per request -> x1000)
        make_product_row(
            "Requests-Tier4", "Requests", 0.00001,
            operation="S3-INTTransition",
        ),
        make_product_row(
            "Requests-Tier3", "Requests", 0.00001,
            operation="S3-SIATransition",
        ),
        make_product_row(
            "Requests-Tier3", "Requests", 0.00002,
            operation="S3-GIRTransition",
        ),
        make_product_row(
            "Requests-Tier3", "Requests", 0.000036,
            operation="S3-GlacierTransition",
        ),
        make_product_row(
            "Requests-Tier3", "Requests", 0.00007,
            operation="S3-GDATransition",
        ),
        # Retrieval fees (per GB)
        make_product_row("Retrieval-SIA", "GB", 0.01),
        make_product_row("Retrieval-GIR", "GB", 0.03),
        make_product_row(
            "Standard-Retrieval-Bytes", "GB", 0.012,
            operation="RestoreObject",
        ),
        make_product_row(
            "Standard-Retrieval-Bytes", "GB", 0.024,
            operation="DeepArchiveRestoreObject",
        ),
        # Flexible Retrieval restore requests (Tier3 + operation)
        make_product_row(
            "Requests-Tier3", "Requests", 0.00006,
            operation="RestoreObject",
        ),
    ]

    return [{"PriceList": rows}]


class FakePricingClient:
    def __init__(self, pages):
        self._pages = pages

    def get_products(self, **kwargs):
        page = self._pages.pop(0)
        return {
            "PriceList": page["PriceList"],
            "NextToken": None,
        }


class FakeBoto3:
    def __init__(self, pages):
        self._pages = pages

    def client(self, *args, **kwargs):
        return FakePricingClient(self._pages)


@pytest.fixture()
def refreshed():
    pages = make_pages()

    return refresh_pricing.build_fresh_rates(
        FakeBoto3(pages), "ap-south-1"
    )


def test_all_refreshable_fields_map_to_expected_rates(refreshed):
    _, values, unmatched = refreshed

    it = values["INTELLIGENT_TIERING"]

    # Layer rates from the FIRST usage bracket only (the 0.024 /
    # 0.023 brackets above 50 TB must be ignored).
    assert it["storage_per_gb_month_frequent"] == 0.025
    assert it["storage_per_gb_month_infrequent"] == 0.0138
    assert it["storage_per_gb_month_archive_instant"] == 0.005

    # 0.0000025 per object-month x 1000.
    assert (
        it["monitoring_automation_fee_per_1000_object_month"]
        == 0.0025
    )

    assert it["get_request_per_1000"] == 0.0004
    assert it["transition_per_1000"] == 0.01

    standard = values["STANDARD"]

    assert standard["storage_per_gb_month"] == 0.025
    assert standard["get_request_per_1000"] == 0.0004

    sia = values["STANDARD_IA"]

    assert sia["storage_per_gb_month"] == 0.0138
    assert sia["retrieval_per_gb"] == 0.01

    flexible = values["GLACIER_FLEXIBLE_RETRIEVAL"]

    assert flexible["retrieval_per_gb"] == 0.012
    assert flexible["retrieval_request_per_1000"] == 0.06

    deep = values["GLACIER_DEEP_ARCHIVE"]

    # Storage is known-unrefreshable; retrieval is mapped via the
    # DeepArchiveRestoreObject operation discriminator.
    assert deep["retrieval_per_gb"] == 0.024
    assert deep["transition_per_1000"] == 0.07

    # The only unmatched field must be the known-unrefresheable
    # Deep Archive storage rate.
    assert unmatched == [
        "storage_per_gb_month/GLACIER_DEEP_ARCHIVE"
    ]


def test_tables_sibling_lines_do_not_pollute_matches(refreshed):
    _, values, _ = refreshed

    it = values["INTELLIGENT_TIERING"]

    # The Tables- FA byte-hrs line bills 0.0288; the anchored
    # pattern must have matched only the plain IT line.
    assert it["storage_per_gb_month_frequent"] != 0.0288


def test_missing_lines_report_as_unmatched_not_written():
    """Drop the monitoring + IT-FA lines; build_fresh_rates must
    report them unmatched (and keep them out of the schema keys
    mapping to real rates)."""

    pages = make_pages()

    pages[0]["PriceList"] = [
        row
        for row in pages[0]["PriceList"]
        if row["product"]["attributes"]["usagetype"]
        not in (
            "APS3-Monitoring-Automation-INT",
            "APS3-TimedStorage-INT-FA-ByteHrs",
        )
    ]

    _rates, values, unmatched = refresh_pricing.build_fresh_rates(
        FakeBoto3(pages), "ap-south-1"
    )

    it = values["INTELLIGENT_TIERING"]

    assert it["monitoring_automation_fee_per_1000_object_month"] is None
    assert it["storage_per_gb_month_frequent"] is None

    assert (
        "monitoring_automation_fee_per_1000_object_month"
        "/INTELLIGENT_TIERING" in unmatched
    )
    assert (
        "storage_per_gb_month_frequent/INTELLIGENT_TIERING"
        in unmatched
    )


def test_per_1000_rate_requires_the_expected_unit():
    """A monitoring-shaped line with the wrong unit must not
    match (the unit= knob guards against unit changes)."""

    rates = [
        {
            "usagetype": "APS3-Monitoring-Automation-INT",
            "operation": "",
            "unit": "GB-Mo",
            "begin": "0",
            "rate": "0.0000025",
        }
    ]

    assert (
        refresh_pricing.per_1000_rate(
            rates,
            r"^(?:[A-Z0-9]{2,6}-)?Monitoring-Automation-INT$",
            unit="Objects",
        )
        is None
    )

    # And the correct unit converts: 0.0000025 x 1000 = 0.0025.
    rates[0]["unit"] = "Objects"

    assert (
        refresh_pricing.per_1000_rate(
            rates,
            r"^(?:[A-Z0-9]{2,6}-)?Monitoring-Automation-INT$",
            unit="Objects",
        )
        == 0.0025
    )