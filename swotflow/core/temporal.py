"""Opt-in acquisition-start ownership for bounded processing batches."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping

from swotflow.core.metadata import parse_swot_l2_hr_raster_metadata


def validate_acquisition_window(start: str = "", end: str = "") -> tuple[str, str]:
    """Validate inclusive ISO dates; both absent preserves legacy selection."""
    start, end = str(start or "").strip(), str(end or "").strip()
    if not start and not end:
        return "", ""
    if not start or not end:
        raise ValueError("Both acquisition_start_date and acquisition_end_date are required.")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("Acquisition date bounds must use YYYY-MM-DD.") from exc
    if first.isoformat() != start or last.isoformat() != end or first > last:
        raise ValueError("Acquisition date bounds must be ordered YYYY-MM-DD dates.")
    return start, end


def acquisition_window(config: Mapping[str, Any]) -> tuple[str, str]:
    """Read a batch window from processing, download, or a subsection mapping."""
    values: list[tuple[str, str]] = []
    for section in (config.get("processing", {}), config.get("download", {}), config):
        if not isinstance(section, Mapping):
            continue
        start, end = section.get("acquisition_start_date", ""), section.get("acquisition_end_date", "")
        if start or end:
            values.append(validate_acquisition_window(start, end))
    if values and any(value != values[0] for value in values):
        raise ValueError("Processing and download acquisition date bounds disagree.")
    return values[0] if values else ("", "")


def acquisition_scope(config: Mapping[str, Any]) -> dict[str, str]:
    """Return enabled scope identity fields, keeping legacy identifiers unchanged."""
    start, end = acquisition_window(config)
    return {"acquisition_start_date": start, "acquisition_end_date": end} if start else {}


def within_acquisition_window(filename: str | Path, start: str = "", end: str = "") -> bool:
    """Assign each observation to its filename start date, including midnight spans.

    Enabled scopes reject unknown filenames. Callers validate the window once
    before a file loop so filtering adds only one metadata parse per candidate.
    """
    if not start and not end:
        return True
    if not start or not end:
        raise ValueError("Both acquisition date bounds are required.")
    try:
        metadata = parse_swot_l2_hr_raster_metadata(filename)
    except ValueError:
        return False
    if metadata is None:
        return False
    value = metadata.fields["range_beginning"][:8]
    return start.replace("-", "") <= value <= end.replace("-", "")
