"""
Generate the CONTROLLED experimental workload for the diversified
real corpus (Phase W-2).

Input:  data/real/diversified/manifest.json  (500 real public
        legal documents, multiple GovInfo collections)

Output: data/real/diversified/workload_500.json -- one record per
        manifest document carrying the controlled experimental
        optimizer inputs:
            - current_storage_class  (controlled variable)
            - document_state         (controlled variable)
            - upload_timestamp       (document age, controlled)
            - access_count           (30-day window, controlled)
            - days_since_last_access (recency, controlled)
            - access_frequency       (DERIVED: access_count / 30,
              exactly as the existing access.py and the
              AggregatesFunction compute it)

Methodology:
    - Access-count bands reuse the EXACT thresholds from the
      existing synthetic workload generator (workload.py
      random_access_count), so distributions stay comparable:
        20% zero | 30% low (1-5) | 30% moderate (6-30)
        | 15% high (31-100) | 5% very high (101-300)
    - Access-frequency is never invented independently: it is
      always access_count / 30 (OBSERVATION_PERIOD_DAYS).
    - STATE distribution reuses the existing
      workload.py random_document_state proportions
      (50% ACTIVE / 25% CLOSED / 15% ARCHIVED / 10% none).
    - Recency (very recent / recent / moderately stale / stale)
      and document-age (new / medium / old) bands are the two NEW
      controlled variables requested for this experiment. Raw
      event materialization stays with the existing
      generate_synthetic_data.build_access_events.
    - Current storage class is assigned per state with realistic
      placements plus a small share of policy-violating ones (e.g.
      an ACTIVE document stranded in GLACIER_DEEP_ARCHIVE) so the
      STATE_ELIGIBILITY rules are genuinely exercised. It is
      deliberately NOT the optimizer's own recommendation and
      nothing tunes toward a savings target.

Realism statement: the PDF bytes are real public legal documents;
document_state, current_storage_class, upload_timestamp and the
access history are controlled experimental variables -- "real
corpus + controlled experimental metadata and access workload",
the same methodology as the first real experiment.

Feasibility constraints honoured per document:
    - upload_timestamp cannot predate the document's true public
      issue date (dateIssued);
    - upload_timestamp must predate the OLDEST materialized event
      (build_access_events places the newest event at
      anchor - days_since and spreads older events across half the
      remaining room, SPAN_FRACTION of the 29-day residual);
    - days_since_last_access < 30 (the event must survive the
      30-day observation window at aggregation time).

Deterministic: single fixed seed; regenerating reproduces the
same file.
"""

import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_FILE = (
    REPO_ROOT / "data" / "real" / "diversified" / "manifest.json"
)
WORKLOAD_FILE = (
    REPO_ROOT / "data" / "real" / "diversified"
    / "workload_500.json"
)

WORKLOAD_SEED = 20261005

# Fixed anchor so the generated file is bit-for-bit reproducible
# (same convention as the synthetic workload's fixed
# reference_timestamp). Event timestamps are materialized later by
# build_access_events relative to the LOADER's anchor, so a later
# load keeps every event inside its live 30-day window.
WORKLOAD_ANCHOR = "2026-10-05T18:00:00Z"

OBSERVATION_PERIOD_DAYS = 30
SPAN_FRACTION = 0.5  # generate_synthetic_data.SPAN_FRACTION

STATE_SHARES = [
    ("ACTIVE", 0.50),
    ("CLOSED", 0.25),
    ("ARCHIVED", 0.15),
    (None, 0.10),
]

CURRENT_CLASS_SHARES = {
    "ACTIVE": [
        ("STANDARD", 0.45),
        ("STANDARD_IA", 0.25),
        ("GLACIER_INSTANT_RETRIEVAL", 0.20),
        ("GLACIER_FLEXIBLE_RETRIEVAL", 0.05),
        ("GLACIER_DEEP_ARCHIVE", 0.05),
    ],
    "CLOSED": [
        ("STANDARD_IA", 0.35),
        ("GLACIER_INSTANT_RETRIEVAL", 0.20),
        ("GLACIER_FLEXIBLE_RETRIEVAL", 0.30),
        ("STANDARD", 0.10),
        ("GLACIER_DEEP_ARCHIVE", 0.05),
    ],
    "ARCHIVED": [
        ("GLACIER_FLEXIBLE_RETRIEVAL", 0.35),
        ("GLACIER_DEEP_ARCHIVE", 0.30),
        ("GLACIER_INSTANT_RETRIEVAL", 0.25),
        ("STANDARD_IA", 0.10),
    ],
    None: [
        ("STANDARD", 0.35),
        ("STANDARD_IA", 0.20),
        ("GLACIER_INSTANT_RETRIEVAL", 0.15),
        ("GLACIER_FLEXIBLE_RETRIEVAL", 0.20),
        ("GLACIER_DEEP_ARCHIVE", 0.10),
    ],
}

RECENCY_BANDS = [
    ("very recent", 0.0, 1.0, 0.30),
    ("recent", 1.0, 7.0, 0.35),
    ("moderately stale", 7.0, 14.0, 0.20),
    ("stale", 14.0, 29.0, 0.15),
]

AGE_BANDS = [
    ("new", 1, 30, 0.15),
    ("medium", 31, 180, 0.35),
    ("old", 181, 1095, 0.50),
]


def draw_from_shares(rng, shares):
    """Draw one item from weighted shares."""

    pick = rng.random()
    cumulative = 0.0

    for item, share in shares:
        cumulative += share

        if pick < cumulative:
            return item

    return shares[-1][0]


def draw_access_category(rng):
    """Exact workload.py access-band thresholds."""

    category = rng.random()

    if category < 0.20:
        return "zero", 0

    if category < 0.50:
        return "low", rng.randint(1, 5)

    if category < 0.80:
        return "moderate", rng.randint(6, 30)

    if category < 0.95:
        return "high", rng.randint(31, 100)

    return "very high", rng.randint(101, 300)


def draw_days_since_last_access(rng):
    """Draw recency from the controlled bands, capped at 29 days."""

    (label, low, high) = draw_from_shares(
        rng,
        [
            ((band[0], band[1], band[2]), band[3])
            for band in RECENCY_BANDS
        ],
    )

    return label, rng.uniform(low, high)


def draw_upload_age(rng, min_days, max_days):
    """
    Draw the document age from the controlled bands, intersected
    with the feasible window [min_days, max_days]. Falls back to
    the feasible span when the band lies entirely outside it.
    """

    pick = rng.random()
    cumulative = 0.0

    chosen = AGE_BANDS[-1]

    for band in AGE_BANDS:
        cumulative += band[3]

        if pick < cumulative:
            chosen = band
            break

    band_low = max(
        chosen[1],
        int(math.ceil(min_days)),
    )
    band_high = min(chosen[2], int(max_days))

    if band_low > band_high:
        return chosen[0], min_days

    return chosen[0], rng.randint(band_low, band_high)


def generate_workload(
    manifest_file=None, workload_file=None
):
    """Assign controlled optimizer inputs to every manifest document."""

    raw = json.loads(
        (manifest_file or MANIFEST_FILE).read_text(
            encoding="utf-8"
        )
    )

    documents = raw["documents"]

    anchor = datetime.fromisoformat(
        WORKLOAD_ANCHOR.replace("Z", "+00:00")
    )

    rng = random.Random(WORKLOAD_SEED)

    records = []

    for document in documents:
        date_issued = datetime.fromisoformat(
            document["date_issued"].replace("Z", "+00:00")
        )

        if date_issued.tzinfo is None:
            date_issued = date_issued.replace(
                tzinfo=timezone.utc
            )

        # The experimental upload cannot predate public issuance.
        max_public_age_days = max(
            1,
            int(
                (anchor - date_issued).total_seconds()
                // 86400
            ),
        )

        state = draw_from_shares(rng, STATE_SHARES)

        current_class = draw_from_shares(
            rng, CURRENT_CLASS_SHARES[state]
        )

        access_label, access_count = draw_access_category(rng)

        if access_count > 0:
            (
                recency_label,
                days_since,
            ) = draw_days_since_last_access(rng)

            # Room the raw materialization will use backward from
            # the newest event.
            event_span_room = (
                (OBSERVATION_PERIOD_DAYS - 1) - days_since
            ) * SPAN_FRACTION

            min_upload_age = days_since + event_span_room
            last_accessed_timestamp = (
                anchor
                - timedelta(days=days_since)
            ).replace(microsecond=0).isoformat()
        else:
            recency_label = "none"
            days_since = None
            min_upload_age = 0.0
            last_accessed_timestamp = None

        # Clamp impossible recency for barely-public documents.
        if access_count > 0 and max_public_age_days < 30:
            recency_room = max_public_age_days * 0.9

            if days_since > recency_room:
                days_since = rng.uniform(
                    0.0, max(recency_room, 0.5)
                )
                min_upload_age = days_since
                last_accessed_timestamp = (
                    anchor
                    - timedelta(days=days_since)
                ).replace(
                    microsecond=0
                ).isoformat()

        age_label, upload_age_days = draw_upload_age(
            rng,
            min_upload_age,
            max_public_age_days,
        )

        upload_age_days = max(upload_age_days, min_upload_age)

        upload_timestamp = (
            anchor - timedelta(days=upload_age_days)
        ).replace(microsecond=0).isoformat()

        records.append(
            {
                "document_id": document["document_id"],
                "collection": document["collection"],
                "document_type": document["document_type"],
                "file_size_bytes": document["file_size_bytes"],
                "current_storage_class": current_class,
                "document_state": state,
                "upload_timestamp": upload_timestamp,
                "last_accessed_timestamp": (
                    last_accessed_timestamp
                ),
                "access_count": access_count,
                "access_frequency": access_count
                / OBSERVATION_PERIOD_DAYS,
                "days_since_last_access": (
                    None
                    if days_since is None
                    else round(days_since, 6)
                ),
                "access_band": access_label,
                "recency_band": (
                    recency_label
                    if access_count > 0
                    else "none"
                ),
                "age_band": age_label,
            }
        )

    # Feasibility self-checks (no silent violations).
    for record in records:
        if record["days_since_last_access"] is not None:
            assert (
                record["days_since_last_access"] < 30
            ), record["document_id"]

        age_days = (
            anchor
            - datetime.fromisoformat(
                record["upload_timestamp"]
            )
        ).total_seconds() / 86400

        assert (
            age_days >= 0 and age_days <= 1200
        ), record["document_id"]

        assert (
            record["access_frequency"]
            == record["access_count"]
            / OBSERVATION_PERIOD_DAYS
        )

    records.sort(key=lambda r: r["document_id"])

    output_file = workload_file or WORKLOAD_FILE

    output_file.parent.mkdir(parents=True, exist_ok=True)

    output_file.write_text(
        json.dumps(
            {
                "dataset": "diversified real corpus",
                "source_manifest": (
                    "data/real/diversified/manifest.json"
                ),
                "methodology": (
                    "Real legal-document corpus + controlled "
                    "experimental metadata and access workload"
                ),
                "random_seed": WORKLOAD_SEED,
                "reference_timestamp": anchor.isoformat(),
                "document_count": len(records),
                "documents": records,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"Generated {len(records)} controlled workload records."
    )
    print(f"Seed: {WORKLOAD_SEED}")
    print(f"Anchor: {WORKLOAD_ANCHOR}")
    print(f"Workload: {output_file}")


if __name__ == "__main__":
    generate_workload()