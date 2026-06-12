"""Shared cumulative workflow ledger for SWOTFlow project stages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from project_database import read_project_rows, upsert_project_rows


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
    rows: Dict[str, Dict[str, str]] = {}
    for raw_row in read_project_rows(path, "workflow_manifest"):
        row = {column: raw_row.get(column, "") for column in WORKFLOW_COLUMNS}
        key = row_key(row)
        if key.strip():
            rows[key] = row
    return rows


def upsert_workflow_manifest(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    export_csv: bool = False,
) -> Path:
    """Merge rows into the shared workflow manifest."""
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    now = timestamp_text()
    normalized_rows = []
    for raw_row in rows:
        row = {column: normalize_cell(raw_row.get(column, "")) for column in WORKFLOW_COLUMNS}
        row["updated_at"] = row.get("updated_at") or now
        if not row.get("stage") or not row.get("record_id"):
            continue
        normalized_rows.append(row)
    upsert_project_rows(
        manifest_path,
        normalized_rows,
        dataset="workflow_manifest",
        export_csv=export_csv,
        fieldnames=WORKFLOW_COLUMNS,
    )
    return manifest_path


def source_signature(paths: Sequence[str | Path]) -> str:
    """Return a stable signature for a planned source-file set."""
    names = sorted(str(Path(path).name) for path in paths)
    payload = json.dumps(names, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
