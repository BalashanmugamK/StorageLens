from .models import OptimizerInput


def from_aggregate_item(item: dict) -> OptimizerInput:
    """
    Convert an aggregate-table item into the optimizer's input model.

    This adapter intentionally does not use boto3 or DynamoDB APIs.
    It only converts a dictionary into OptimizerInput.
    """
    days_since_last_access = item.get("days_since_last_access")

    if days_since_last_access is not None:
        days_since_last_access = float(days_since_last_access)

    document_state = item.get("document_state")

    if document_state is not None:
        document_state = str(document_state).upper()

    return OptimizerInput(
        document_id=str(item["document_id"]),
        file_size_bytes=int(item["file_size_bytes"]),
        current_storage_class=str(item["current_storage_class"]),
        upload_timestamp=str(item["upload_timestamp"]),
        last_accessed_timestamp=item.get("last_accessed_timestamp"),
        access_count=int(item["access_count"]),
        days_since_last_access=days_since_last_access,
        access_frequency=float(item["access_frequency"]),
        aggregation_timestamp=str(item["aggregation_timestamp"]),
        document_state=document_state,
    )