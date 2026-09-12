ALL_STORAGE_CLASSES = [
    "STANDARD",
    "STANDARD_IA",
    "GLACIER_INSTANT_RETRIEVAL",
    "GLACIER_FLEXIBLE_RETRIEVAL",
    "GLACIER_DEEP_ARCHIVE",
]


STATE_ELIGIBILITY = {
    "ACTIVE": [
        "STANDARD",
        "STANDARD_IA",
        "GLACIER_INSTANT_RETRIEVAL",
    ],
    "CLOSED": [
        "STANDARD_IA",
        "GLACIER_INSTANT_RETRIEVAL",
        "GLACIER_FLEXIBLE_RETRIEVAL",
    ],
    "ARCHIVED": [
        "GLACIER_INSTANT_RETRIEVAL",
        "GLACIER_FLEXIBLE_RETRIEVAL",
        "GLACIER_DEEP_ARCHIVE",
    ],
}


def get_eligible_storage_classes(document_state: str | None) -> list[str]:
    """Return storage classes allowed for the document's state."""

    if document_state is None:
        return ALL_STORAGE_CLASSES.copy()

    state = document_state.upper()

    if state not in STATE_ELIGIBILITY:
        raise ValueError(
            f"Unsupported document state: {document_state}"
        )

    return STATE_ELIGIBILITY[state].copy()
