"""Retain real submission evidence when a later inventory refresh verifies an asset."""
from datetime import datetime, timezone
from typing import Iterable, Mapping


SUBMISSION_FIELDS = ("submit_time", "detected_task_name")


def preserve_verified_submission(row: Mapping[str, str], previous: Mapping[str, str]) -> dict[str, str]:
    """Fill absent submission fields only for a verified row with the same asset ID.

    A new attempt must not inherit an old task. Inventory presence alone also
    cannot establish that this application submitted an upload.
    """
    result = dict(row)
    if (str(row.get("final_status", "")).upper() == "EE_VERIFIED_EXISTS"
            and row.get("asset_id") and row.get("asset_id") == previous.get("asset_id")):
        if any(row.get(field) and previous.get(field) and row[field] != previous[field]
               for field in SUBMISSION_FIELDS):
            return result  # Do not mix two different attempts.
        for field in SUBMISSION_FIELDS:
            if not result.get(field):
                result[field] = previous.get(field, "")
    return result


def restore_verified_submissions(
    rows: Iterable[Mapping[str, str]], events: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Recover blank fields from retained events; never invent a submission time."""
    def event_time(event: Mapping[str, str]) -> datetime:
        try:
            value = datetime.fromisoformat(event.get("event_time", "").replace("Z", "+00:00"))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    latest: dict[str, Mapping[str, str]] = {}
    for event in events:
        asset_id = event.get("asset_id", "")
        if not asset_id or not any(event.get(field) for field in SUBMISSION_FIELDS):
            continue
        if event_time(event) >= event_time(latest.get(asset_id, {})):
            latest[asset_id] = event
    return [preserve_verified_submission(row, latest.get(row.get("asset_id", ""), {})) for row in rows]
