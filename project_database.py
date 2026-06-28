"""SQLite-backed project records for SWOTFlow."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


DATABASE_FILE_NAME = "swotflow.sqlite3"
SCHEMA_VERSION = 1
_INITIALIZED_PATHS: set[Path] = set()
_INITIALIZE_LOCK = threading.Lock()

DATASET_BY_FILENAME = {
    "download_preview.csv": "download_preview",
    "download_manifest.csv": "download_manifest",
    "extract_manifest.csv": "extract_manifest",
    "extract_errors.csv": "extract_errors",
    "mosaic_report.csv": "mosaic_report",
    "mosaic_manifest.csv": "mosaic_manifest",
    "mixed_crid_mosaics.csv": "mixed_crid_mosaics",
    "upload_report.csv": "upload_report",
    "ee_asset_inventory.csv": "ee_asset_inventory",
    "workflow_manifest.csv": "workflow_manifest",
    "update_campaigns.csv": "update_campaigns",
    "update_expected.csv": "update_expected",
    "update_runs.csv": "update_runs",
}

KEY_FIELDS_BY_DATASET = {
    "download_preview": ("granule_id", "file_name"),
    "download_manifest": ("granule_id", "file_name"),
    "extract_manifest": ("record_id", "input_nc", "output_tif"),
    "extract_errors": ("input_nc", "record_id", "error"),
    "mosaic_report": ("output_file",),
    "mosaic_manifest": ("output_file",),
    "mixed_crid_mosaics": ("output_file",),
    "upload_report": ("asset_id",),
    "ee_asset_inventory": ("asset_id", "id", "name"),
    "workflow_manifest": ("stage", "record_id"),
    "update_campaigns": ("campaign_id",),
    "update_expected": ("record_id",),
    "update_runs": ("run_id",),
}

STATUS_FIELDS_BY_DATASET = {
    "download_preview": "status",
    "download_manifest": "last_status",
    "extract_manifest": "status",
    "extract_errors": "status",
    "mosaic_report": "status",
    "mosaic_manifest": "status",
    "mixed_crid_mosaics": "status",
    "upload_report": "final_status",
    "ee_asset_inventory": "asset_type",
    "workflow_manifest": "status",
    "update_campaigns": "status",
    "update_expected": "status",
    "update_runs": "run_type",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def dataset_for_path(path: str | Path) -> str | None:
    return DATASET_BY_FILENAME.get(Path(path).name.lower())


def database_path_for(path: str | Path) -> Path:
    """Find the project database associated with a report or manifest path."""
    candidate = Path(path)
    directory = candidate if candidate.is_dir() else candidate.parent
    for current in (directory, *directory.parents):
        if current.name.lower() == "00_logs":
            return current.parent / DATABASE_FILE_NAME
    return directory / DATABASE_FILE_NAME


def database_path_from_config(config: Mapping[str, Any]) -> Path:
    processing = config.get("processing", {})
    if isinstance(processing, Mapping):
        configured = str(processing.get("database", "") or "").strip()
        if configured:
            return Path(configured)
        root = str(processing.get("root", "") or "").strip()
        if root:
            return Path(root) / DATABASE_FILE_NAME
        logs = str(processing.get("logs", "") or "").strip()
        if logs:
            return database_path_for(Path(logs) / "workflow_manifest.csv")
    raise ValueError("Project database path cannot be derived from the configuration.")


def _normalize_row(row: Mapping[str, Any]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for key, value in row.items():
        if value is None:
            normalized[str(key)] = ""
        elif isinstance(value, (list, tuple, set, dict)):
            normalized[str(key)] = json.dumps(value, sort_keys=True)
        else:
            normalized[str(key)] = str(value)
    return normalized


def _record_key(dataset: str, row: Mapping[str, str], index: int = 0) -> str:
    fields = KEY_FIELDS_BY_DATASET.get(dataset, ())
    if dataset == "workflow_manifest":
        stage = row.get("stage", "").strip()
        record_id = row.get("record_id", "").strip()
        if stage and record_id:
            return f"{stage}\t{record_id}"
    if dataset == "extract_manifest":
        record_id = row.get("record_id", "").strip()
        if record_id:
            return record_id
        input_name = Path(row.get("input_nc", "")).name
        mode = row.get("target_crs_mode", "").strip()
        if input_name and mode:
            return f"{input_name}|{mode}"
    for field in fields:
        value = row.get(field, "").strip()
        if value:
            return value
    payload = json.dumps(dict(sorted(row.items())), separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"row-{index}-{digest}"


def _utm_tile(row: Mapping[str, str]) -> str:
    direct = row.get("utm_tile", "").strip()
    if direct:
        return direct
    zone = row.get("utm_zone", "").strip()
    band = row.get("mgrs_band", "").strip()
    if zone and band:
        return f"UTM{zone}{band}"
    return row.get("output_grid", "").strip()


def _event_date(row: Mapping[str, str]) -> str:
    for field in ("date", "start_date", "start_time", "metadata_start_time", "range_beginning"):
        value = row.get(field, "").strip()
        if value:
            return value[:10]
    return ""


@dataclass(frozen=True)
class MigrationResult:
    database_path: Path
    imported_rows: Dict[str, int]
    skipped_rows: Dict[str, int]


class ProjectDatabase:
    """Small indexed record store used by every project processing stage."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _INITIALIZE_LOCK:
            if self.path not in _INITIALIZED_PATHS or not self.path.exists():
                self._initialize()
                _INITIALIZED_PATHS.add(self.path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=60000")
        return connection

    @contextmanager
    def session(self) -> Iterable[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS records (
                    dataset TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    utm_tile TEXT NOT NULL DEFAULT '',
                    event_date TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (dataset, record_key)
                );

                CREATE INDEX IF NOT EXISTS idx_records_dataset_status
                    ON records(dataset, status);
                CREATE INDEX IF NOT EXISTS idx_records_dataset_tile
                    ON records(dataset, utm_tile);
                CREATE INDEX IF NOT EXISTS idx_records_dataset_date
                    ON records(dataset, event_date);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('revision', '0')"
            )

    def _bump_revision(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES('revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('updated_at', ?)",
            (now_iso(),),
        )

    def revision(self) -> int:
        with self.session() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'revision'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def integrity_check(self) -> str:
        with self.session() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def dataset_count(self, dataset: str) -> int:
        with self.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM records WHERE dataset = ?",
                (dataset,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def dataset_prefix_count(self, dataset: str, record_key_prefix: str) -> int:
        """Count rows whose indexed record key begins with a stable prefix."""
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM records
                WHERE dataset = ? AND record_key LIKE ?
                """,
                (dataset, f"{record_key_prefix}%"),
            ).fetchone()
        return int(row["count"]) if row else 0

    def has_dataset_marker(self, dataset: str) -> bool:
        with self.session() as connection:
            row = connection.execute(
                "SELECT 1 FROM metadata WHERE key = ?",
                (f"dataset_initialized:{dataset}",),
            ).fetchone()
        return row is not None

    def mark_dataset_initialized(
        self,
        dataset: str,
        *,
        source: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> None:
        owns_connection = connection is None
        active = connection or self.connect()
        try:
            active.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (f"dataset_initialized:{dataset}", source or now_iso()),
            )
            if owns_connection:
                active.commit()
        finally:
            if owns_connection:
                active.close()

    def read_rows(self, dataset: str) -> List[Dict[str, str]]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM records WHERE dataset = ? ORDER BY record_key",
                (dataset,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def status_counts(self, dataset: str) -> Dict[str, int]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT CASE WHEN status = '' THEN 'UNKNOWN' ELSE UPPER(status) END AS status,
                       COUNT(*) AS count
                FROM records
                WHERE dataset = ?
                GROUP BY CASE WHEN status = '' THEN 'UNKNOWN' ELSE UPPER(status) END
                """,
                (dataset,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def workflow_stage_status_counts(self) -> List[tuple[str, str, int]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(json_extract(payload_json, '$.stage'), '') AS stage,
                       CASE WHEN status = '' THEN 'UNKNOWN' ELSE status END AS status,
                       COUNT(*) AS count
                FROM records
                WHERE dataset = 'workflow_manifest'
                GROUP BY stage, status
                ORDER BY stage, status
                """
            ).fetchall()
        return [
            (str(row["stage"]), str(row["status"]), int(row["count"]))
            for row in rows
            if row["stage"]
        ]

    def upsert_rows(
        self,
        dataset: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        replace_dataset: bool = False,
        mark_initialized: bool = True,
    ) -> int:
        normalized_rows = [_normalize_row(row) for row in rows]
        timestamp = now_iso()
        with self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if replace_dataset:
                connection.execute("DELETE FROM records WHERE dataset = ?", (dataset,))
            for index, row in enumerate(normalized_rows):
                key = _record_key(dataset, row, index)
                status_field = STATUS_FIELDS_BY_DATASET.get(dataset, "status")
                status = row.get(status_field, "")
                source_path = row.get("source_path", "") or row.get("local_file", "") or row.get("input_nc", "")
                output_path = row.get("output_path", "") or row.get("output_file", "") or row.get("asset_id", "")
                connection.execute(
                    """
                    INSERT INTO records(
                        dataset, record_key, status, utm_tile, event_date,
                        source_path, output_path, updated_at, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset, record_key) DO UPDATE SET
                        status = excluded.status,
                        utm_tile = excluded.utm_tile,
                        event_date = excluded.event_date,
                        source_path = excluded.source_path,
                        output_path = excluded.output_path,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    (
                        dataset,
                        key,
                        status,
                        _utm_tile(row),
                        _event_date(row),
                        source_path,
                        output_path,
                        row.get("updated_at", "") or timestamp,
                        json.dumps(row, separators=(",", ":"), sort_keys=True),
                    ),
                )
            if mark_initialized:
                self.mark_dataset_initialized(
                    dataset,
                    source=f"sqlite:{timestamp}",
                    connection=connection,
                )
            self._bump_revision(connection)
            connection.commit()
        return len(normalized_rows)

    def import_csv(
        self,
        dataset: str,
        csv_path: str | Path,
        *,
        replace_dataset: bool = False,
    ) -> int:
        path = Path(csv_path)
        count = 0
        first_chunk = True
        if path.exists() and path.is_file():
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                chunk: List[Dict[str, str]] = []
                for row in csv.DictReader(handle):
                    chunk.append(dict(row))
                    if len(chunk) >= 5000:
                        count += self.upsert_rows(
                            dataset,
                            chunk,
                            replace_dataset=replace_dataset and first_chunk,
                            mark_initialized=False,
                        )
                        first_chunk = False
                        chunk = []
                if chunk:
                    count += self.upsert_rows(
                        dataset,
                        chunk,
                        replace_dataset=replace_dataset and first_chunk,
                        mark_initialized=False,
                    )
                    first_chunk = False
        if first_chunk:
            self.upsert_rows(
                dataset,
                [],
                replace_dataset=replace_dataset,
                mark_initialized=False,
            )
        with self.session() as connection:
            self.mark_dataset_initialized(
                dataset,
                source=f"csv:{path}",
                connection=connection,
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (f"csv_source:{dataset}", str(path)),
            )
            connection.commit()
        return count

    def ensure_imported(self, dataset: str, csv_path: str | Path) -> int:
        if self.has_dataset_marker(dataset):
            return 0
        return self.import_csv(dataset, csv_path)

    def export_csv(
        self,
        dataset: str,
        csv_path: str | Path,
        fieldnames: Sequence[str] | None = None,
    ) -> Path:
        path = Path(csv_path)
        columns = list(fieldnames or [])
        if not columns:
            rows = self.read_rows(dataset)
            seen: set[str] = set()
            for row in rows:
                for key in row:
                    if key not in seen:
                        seen.add(key)
                        columns.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            if fieldnames:
                with self.session() as connection:
                    cursor = connection.execute(
                        "SELECT payload_json FROM records WHERE dataset = ? ORDER BY record_key",
                        (dataset,),
                    )
                    for stored in cursor:
                        row = json.loads(stored["payload_json"])
                        writer.writerow({column: row.get(column, "") for column in columns})
            else:
                for row in rows:
                    writer.writerow({column: row.get(column, "") for column in columns})
        os.replace(temporary, path)
        return path


def read_project_rows(path: str | Path, dataset: str | None = None) -> List[Dict[str, str]]:
    csv_path = Path(path)
    dataset_name = dataset or dataset_for_path(csv_path)
    if dataset_name is None:
        if not csv_path.exists():
            return []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    database = ProjectDatabase(database_path_for(csv_path))
    database.ensure_imported(dataset_name, csv_path)
    return database.read_rows(dataset_name)


def upsert_project_rows(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    dataset: str | None = None,
    replace_dataset: bool = False,
    export_csv: bool = False,
    fieldnames: Sequence[str] | None = None,
) -> int:
    csv_path = Path(path)
    dataset_name = dataset or dataset_for_path(csv_path)
    if dataset_name is None:
        raise ValueError(f"No project dataset is registered for {csv_path.name}")
    database = ProjectDatabase(database_path_for(csv_path))
    database.ensure_imported(dataset_name, csv_path)
    count = database.upsert_rows(dataset_name, rows, replace_dataset=replace_dataset)
    if export_csv:
        database.export_csv(dataset_name, csv_path, fieldnames)
    return count


def export_project_dataset(
    path: str | Path,
    *,
    dataset: str | None = None,
    fieldnames: Sequence[str] | None = None,
) -> Path:
    csv_path = Path(path)
    dataset_name = dataset or dataset_for_path(csv_path)
    if dataset_name is None:
        raise ValueError(f"No project dataset is registered for {csv_path.name}")
    database = ProjectDatabase(database_path_for(csv_path))
    return database.export_csv(dataset_name, csv_path, fieldnames)


def project_status_counts(path: str | Path, dataset: str | None = None) -> Dict[str, int]:
    csv_path = Path(path)
    dataset_name = dataset or dataset_for_path(csv_path)
    if dataset_name is None:
        return {}
    database = ProjectDatabase(database_path_for(csv_path))
    database.ensure_imported(dataset_name, csv_path)
    return database.status_counts(dataset_name)


def migrate_project_csvs(project_root: str | Path) -> MigrationResult:
    root = Path(project_root)
    logs = root / "00_logs"
    database = ProjectDatabase(root / DATABASE_FILE_NAME)
    imported: Dict[str, int] = {}
    skipped: Dict[str, int] = {}
    for filename, dataset in DATASET_BY_FILENAME.items():
        csv_path = logs / filename
        if database.has_dataset_marker(dataset):
            skipped[dataset] = database.dataset_count(dataset)
            continue
        imported[dataset] = database.import_csv(dataset, csv_path)
    with database.session() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('migration_completed_at', ?)",
            (now_iso(),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('project_root', ?)",
            (str(root),),
        )
        connection.commit()
    return MigrationResult(database.path, imported, skipped)
