"""Shared cumulative workflow ledger for GeeUp project stages."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence


WORKFLOW_COLUMNS = [
    "stage",
    "record_id",
    "record_type",
    "status",
    "source_path",
    "output_path",
    "auxiliary_path",
    "utm_tile",
    "date",
    "start_time",
    "end_time",
    "cycle_id",
    "pass_id",
    "scene_id",
    "coordinate_system",
    "grouping_mode",
    "source_signature",
    "input_count",
    "raw_exists",
    "output_exists",
    "known_from_stage_manifest",
    "message",
    "updated_at",
]


def workflow_manifest_path(report_or_log_path: str | Path) -> Path:
    """Return the shared project workflow manifest beside a stage report."""
    return Path(report_or_log_path).with_name("workflow_manifest.csv")


def timestamp_text() -> str:
    """Return a seconds-resolution timestamp for manifest updates."""
    return datetime.now().replace(microsecond=0).isoformat()


def normalize_cell(value: Any) -> str:
    """Normalize manifest values to CSV-safe text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def row_key(row: Mapping[str, str]) -> str:
    """Return the unique key for one workflow row."""
    return f"{row.get('stage', '')}\t{row.get('record_id', '')}"


def read_workflow_manifest(path: str | Path) -> Dict[str, Dict[str, str]]:
    """Load workflow rows keyed by stage and record id."""
    manifest_path = Path(path)
    if not manifest_path.exists() or not manifest_path.is_file():
        return {}
    rows: Dict[str, Dict[str, str]] = {}
    try:
        with manifest_path.open("r", encoding="utf-8", newline="") as handle:
            for raw_row in csv.DictReader(handle):
                row = {column: raw_row.get(column, "") for column in WORKFLOW_COLUMNS}
                key = row_key(row)
                if key.strip():
                    rows[key] = row
    except OSError:
        return {}
    return rows


def upsert_workflow_manifest(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """Merge rows into the shared workflow manifest."""
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_workflow_manifest(manifest_path)
    now = timestamp_text()
    for raw_row in rows:
        row = {column: normalize_cell(raw_row.get(column, "")) for column in WORKFLOW_COLUMNS}
        row["updated_at"] = row.get("updated_at") or now
        key = row_key(row)
        if not row.get("stage") or not row.get("record_id"):
            continue
        previous = existing.get(key, {column: "" for column in WORKFLOW_COLUMNS})
        previous.update(row)
        existing[key] = previous

    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKFLOW_COLUMNS)
        writer.writeheader()
        for key in sorted(existing):
            writer.writerow({column: existing[key].get(column, "") for column in WORKFLOW_COLUMNS})
    return manifest_path


def source_signature(paths: Sequence[str | Path]) -> str:
    """Return a stable signature for a planned source-file set."""
    names = sorted(str(Path(path).name) for path in paths)
    payload = json.dumps(names, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
