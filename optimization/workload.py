import json
import random
from datetime import datetime, timedelta
from pathlib import Path


RANDOM_SEED = 20260912
WORKLOAD_SIZE = 500

OUTPUT_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "synthetic"
    / "workload_500.json"
)

REFERENCE_TIMESTAMP = "2026-09-12T00:00:00Z"


STORAGE_CLASSES = [
    "STANDARD",
    "STANDARD_IA",
    "GLACIER_INSTANT_RETRIEVAL",
    "GLACIER_FLEXIBLE_RETRIEVAL",
    "GLACIER_DEEP_ARCHIVE",
]


def random_file_size_bytes(
    rng: random.Random,
) -> int:
    """Generate a synthetic document size."""

    size_mb = rng.randint(1, 500)

    return size_mb * 1_000_000


def random_document_age_days(
    rng: random.Random,
) -> int:
    """Generate document age from 1 day to 5 years."""

    return rng.randint(1, 1825)


def random_access_count(
    rng: random.Random,
) -> int:
    """
    Generate DOWNLOAD accesses during the previous
    30 days.

    The distribution includes zero, low, medium,
    and high access workloads.
    """

    category = rng.random()

    if category < 0.20:
        return 0

    if category < 0.50:
        return rng.randint(1, 5)

    if category < 0.80:
        return rng.randint(6, 30)

    if category < 0.95:
        return rng.randint(31, 100)

    return rng.randint(101, 300)


def random_document_state(
    rng: random.Random,
) -> str | None:
    """Generate a synthetic document state."""

    category = rng.random()

    if category < 0.50:
        return "ACTIVE"

    if category < 0.75:
        return "CLOSED"

    if category < 0.90:
        return "ARCHIVED"

    return None


def random_current_storage_class(
    rng: random.Random,
) -> str:
    """Generate the current storage class."""

    return rng.choice(STORAGE_CLASSES)


def create_document(
    rng: random.Random,
    document_number: int,
) -> dict:
    """Create one synthetic aggregate document."""

    age_days = random_document_age_days(rng)

    reference_time = datetime.fromisoformat(
        REFERENCE_TIMESTAMP.replace("Z", "+00:00")
    )

    upload_time = (
        reference_time
        - timedelta(days=age_days)
    )

    upload_timestamp = (
        upload_time
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    access_count = random_access_count(rng)

    if access_count > 0:
        days_since_last_access = rng.uniform(
            0,
            min(age_days, 30),
        )

        last_accessed_time = (
            reference_time
            - timedelta(
                days=days_since_last_access
            )
        )

        last_accessed_timestamp = (
            last_accessed_time
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    else:
        days_since_last_access = None
        last_accessed_timestamp = None

    file_size_bytes = random_file_size_bytes(rng)

    document_state = random_document_state(rng)

    current_storage_class = (
        random_current_storage_class(rng)
    )

    return {
        "document_id": (
            f"synthetic-{document_number:04d}"
        ),
        "file_size_bytes": file_size_bytes,
        "current_storage_class": current_storage_class,
        "upload_timestamp": upload_timestamp,
        "last_accessed_timestamp": (
            last_accessed_timestamp
        ),
        "access_count": access_count,
        "days_since_last_access": (
            days_since_last_access
        ),
        "access_frequency": (
            access_count / 30
        ),
        "aggregation_timestamp": (
            REFERENCE_TIMESTAMP
        ),
        "document_state": document_state,
    }


def generate_workload() -> list[dict]:
    """Generate the complete reproducible workload."""

    rng = random.Random(RANDOM_SEED)

    documents = []

    for number in range(
        1,
        WORKLOAD_SIZE + 1,
    ):
        document = create_document(
            rng,
            number,
        )

        documents.append(document)

    return documents


def save_workload(
    documents: list[dict],
) -> None:
    """Save the generated workload as JSON."""

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            {
                "reference_timestamp": (
                    REFERENCE_TIMESTAMP
                ),
                "random_seed": RANDOM_SEED,
                "document_count": len(documents),
                "documents": documents,
            },
            file,
            indent=2,
        )


def main() -> None:
    documents = generate_workload()

    save_workload(documents)

    print(
        f"Generated {len(documents)} synthetic documents."
    )

    print(
        f"Random seed: {RANDOM_SEED}"
    )

    print(
        f"Output: {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
