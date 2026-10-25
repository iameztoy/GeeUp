"""Offline, reversible relocation of a copied SWOTFlow project.

Only metadata is rewritten. Raster files, credentials and Earth Engine assets
are never moved or modified. Close all project writers before using this tool.
"""

from __future__ import annotations

from swotflow.paths import APP_ROOT

import argparse
import csv
import json
import ntpath
import os
import posixpath
import shutil
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping

import yaml

from swotflow.project.database import DATABASE_FILE_NAME, DATASET_BY_FILENAME


PENDING_FILE = ".swotflow-relocation-pending.json"
LOCK_FILE = ".swotflow-relocation-lock"
VALIDATION_FILE = ".swotflow-relocation-validation.json"
BACKUP_FOLDER = ".swotflow-relocation"
Progress = Callable[[str], None]
EXTRACT_COMPLETE = {"written", "rewritten_invalid_existing", "skipped_existing", "skipped_manifest", "local_complete"}
MOSAIC_COMPLETE = {"MOSAIC_CREATED", "MOSAIC_CREATED_WITH_EXCLUSIONS", "COPIED_SINGLETON", "SKIPPED_EXISTS", "SKIPPED_MANIFEST"}
UPLOAD_COMPLETE = {"COMPLETED", "SKIPPED_ALREADY_EXISTS", "EE_VERIFIED_EXISTS"}
# Runtime paths belong to the new machine, not to the scientific project.
RUNTIME_KEYS = {"python", "chrome_binary", "binary_location", "driver_path", "chromedriver_path", "user_data_dir"}


class RelocationRequired(ValueError):
    """Opening this location would use stale or partly migrated paths."""


def _pure(value: str) -> PureWindowsPath | PurePosixPath:
    return PureWindowsPath(ntpath.normpath(value)) if PureWindowsPath(value).drive else PurePosixPath(posixpath.normpath(value))


def same_path(left: str | Path, right: str | Path) -> bool:
    return _pure(str(left)) == _pure(str(right))


def _path_key(value: str) -> str:
    path = _pure(value)
    return str(path).casefold() if isinstance(path, PureWindowsPath) else str(path)


def _under(value: str, parent: str | Path) -> bool:
    try:
        _pure(value).relative_to(_pure(str(parent)))
        return True
    except ValueError:
        return False


def project_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    return candidate.parent if candidate.name == "project.yaml" else candidate


def read_document(root: Path) -> dict[str, Any]:
    with (root / "project.yaml").open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict) or not isinstance(document.get("config"), dict):
        raise ValueError("Invalid project.yaml: project configuration is missing.")
    return document


def assert_project_location(path: Path, document: Mapping[str, Any], *, check_validation: bool = True) -> None:
    """Fail before project loading creates folders, imports CSVs or writes state."""
    root = path.resolve().parent
    if (root / PENDING_FILE).exists() or (root / LOCK_FILE).exists():
        raise RelocationRequired("An interrupted relocation needs recovery. Use Home > Relocate / Validate Project > Restore Backup.")
    stored = str(document.get("project", {}).get("root", ""))
    if stored and _pure(stored).is_absolute() and not same_path(stored, root):
        raise RelocationRequired(
            f"This project was copied or moved from {stored} to {root}. "
            "Use Home > Relocate / Validate Project before opening it. The original project has not been changed."
        )
    gate = root / VALIDATION_FILE
    if check_validation and gate.exists():
        with gate.open(encoding="utf-8") as handle:
            if not json.load(handle).get("ready", False):
                raise RelocationRequired("Relocation needs file validation. Use Home > Relocate / Validate Project, restore any missing files, and click Validate.")


@dataclass
class PathMapper:
    """Component-aware mappings, including Windows paths read on a Mac."""

    mappings: list[tuple[str, str]]

    def __post_init__(self) -> None:
        for old, new in self.mappings:
            if not _pure(old).is_absolute() or not Path(new).is_absolute():
                raise ValueError("Both sides of a relocation mapping must be absolute paths.")
        self.mappings.sort(key=lambda pair: len(_pure(pair[0]).parts), reverse=True)

    def text(self, value: str) -> str:
        for prefix in ("csv:",):
            if value.startswith(prefix):
                return prefix + self.text(value[len(prefix):])
        if "\t" in value:
            return "\t".join(self.text(part) for part in value.split("\t"))
        if not value or "\n" in value or not _pure(value).is_absolute():
            return value
        source = _pure(value)
        for old, new in self.mappings:
            try:
                relative = source.relative_to(_pure(old))
            except ValueError:
                continue
            return str(Path(new).joinpath(*relative.parts))
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if not isinstance(value, str):
            return value
        if value.lstrip().startswith(("[", "{")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                mapped = self.value(parsed)
                return json.dumps(mapped, ensure_ascii=True) if mapped != parsed else value
        return self.text(value)


def _local_paths(value: Any, key: str = "") -> Iterable[str]:
    """Read structured local paths, not URLs, diagnostic prose or EE asset IDs."""
    if key in RUNTIME_KEYS:
        return
    if isinstance(value, dict):
        for name, item in value.items():
            yield from _local_paths(item, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from _local_paths(item, key)
    elif isinstance(value, str) and value:
        if value.lstrip().startswith(("[", "{")):
            try:
                yield from _local_paths(json.loads(value), key)
            except ValueError:
                return
        elif "\n" not in value and _pure(value).is_absolute():
            yield value


def _readonly_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"The authoritative database is missing: {path}. Copy it before continuing; do not reconstruct it from CSV exports.")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _backup(source: Path, destination: Path, progress: Progress = lambda _: None) -> None:
    """Snapshot committed WAL contents, not just the main SQLite file."""
    with closing(_readonly_database(source)) as source_db, closing(sqlite3.connect(destination)) as dest_db:
        source_db.backup(dest_db, pages=2048, progress=lambda _status, remaining, total: progress(
            f"Copying SQLite snapshot: {100 * (total - remaining) / max(total, 1):.0f}%"
        ))
        _check_database(dest_db, progress)


def _rows(connection: sqlite3.Connection, dataset: str) -> Iterable[dict[str, Any]]:
    for row in connection.execute("SELECT payload_json FROM records WHERE dataset=?", (dataset,)):
        yield json.loads(row[0])


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in connection.execute("SELECT dataset, count(*) FROM records GROUP BY dataset")}


def _check_database(connection: sqlite3.Connection, progress: Progress) -> None:
    """Check SQLite structures in linear time, with a heartbeat on slow disks."""
    started = last_update = time.monotonic()
    def heartbeat() -> int:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update >= 2:
            progress(f"Checking database structure... {int(now - started)} seconds elapsed")
            last_update = now
        return 0
    connection.set_progress_handler(heartbeat, 100000)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Project database structural integrity check failed. Preserve the source copy.")
    finally:
        connection.set_progress_handler(None, 0)


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@dataclass
class ValidationReport:
    root: str
    dataset_counts: dict[str, int] = field(default_factory=dict)
    issue_counts: dict[str, int] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)
    present_files: int = 0
    absent_with_downstream_proof: int = 0
    missing_unresolved_files: int = 0
    error_count: int = 0
    backup: str = ""

    def issue(self, code: str, message: str, path: str = "", severity: str = "warning") -> None:
        self.issue_counts[code] = self.issue_counts.get(code, 0) + 1
        self.error_count += int(severity == "error")
        if len(self.issues) < 500:
            self.issues.append(dict(code=code, message=message, path=path, severity=severity))

    @property
    def valid(self) -> bool:
        return self.error_count == 0

    def summary(self) -> str:
        lines = ["Validation passed." if self.valid else "Validation needs attention.",
                 f"History records: {sum(self.dataset_counts.values()):,}",
                 f"Recorded files present: {self.present_files:,}",
                 f"Absent files accounted for by downstream/QA/duplicate records: {self.absent_with_downstream_proof:,}",
                 f"Missing files without downstream proof: {self.missing_unresolved_files:,}"]
        lines.extend(f"{key}: {count:,}" for key, count in self.issue_counts.items())
        lines.extend(f"{item['message']} {item['path']}" for item in self.issues[:12])
        if self.backup:
            lines.append(f"Rollback backup: {self.backup}")
        return "\n".join(lines)


def validate_project(path: str | Path, progress: Progress = lambda _: None) -> ValidationReport:
    """Inspect history and downstream proof without importing or modifying records."""
    root = project_root(path)
    document = read_document(root)
    report = ValidationReport(str(root))
    try:
        assert_project_location(root / "project.yaml", document, check_validation=False)
    except RelocationRequired as exc:
        report.issue("relocation_required", str(exc), severity="error")
        return report
    config = document["config"]
    moved_names: set[str] = set()
    for log in (root / "00_logs").glob("swot_duplicate_removal_*.txt"):
        with log.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MOVED: "):
                    moved_names.add(_pure(line[len("MOVED: "):].strip()).name)
    progress("Checking SQLite integrity and history counts...")
    with closing(_readonly_database(root / DATABASE_FILE_NAME)) as connection:
        connection.execute("BEGIN")
        _check_database(connection, progress)
        report.dataset_counts = _counts(connection)
        # Sets contain only paths/IDs, not entire payloads: bounded memory for large projects.
        extracted = {_path_key(str(row.get("input_nc", ""))) for row in _rows(connection, "extract_manifest")
                     if str(row.get("status", "")).lower() in EXTRACT_COMPLETE}
        mosaicked: set[str] = set()
        for row in _rows(connection, "mosaic_manifest"):
            if row.get("status") == "SKIPPED_INCOMPATIBLE":
                # This is the existing QA cleanup rule, not a new success state.
                from swotflow.project.insights import qa_excluded_mosaic_rows_without_raw_repair
                raw_folder = Path(config.get("processing", {}).get("raw_downloads", root / "01_raw_downloads"))
                for excluded in qa_excluded_mosaic_rows_without_raw_repair([row], raw_folder):
                    mosaicked.update(json.loads(excluded.get("input_files") or "[]"))
            if row.get("status") not in MOSAIC_COMPLETE or str(row.get("stale", "false")).lower() == "true":
                continue
            for key in ("input_files", "excluded_input_files"):
                value = row.get(key) or "[]"
                mosaicked.update(json.loads(value) if isinstance(value, str) else value)
        mosaicked = {_path_key(value) for value in mosaicked}
        assets = {str(row.get("asset_id") or row.get("id") or row.get("name") or "")
                  for row in _rows(connection, "ee_asset_inventory")
                  if str(row.get("asset_type", "IMAGE")).upper() == "IMAGE"}
        verified: set[str] = set()
        for row in _rows(connection, "upload_report"):
            if row.get("final_status") in UPLOAD_COMPLETE or row.get("asset_id") in assets:
                verified.add(_path_key(str(row.get("local_file", ""))))
        seen: set[str] = set()
        checks = 0
        for dataset, field_name, proof in (("download_manifest", "local_path", extracted),
                                          ("extract_manifest", "output_tif", mosaicked),
                                          ("mosaic_manifest", "output_file", verified),
                                          ("upload_report", "local_file", verified)):
            progress(f"Checking {dataset} files and downstream completion proof...")
            for row in _rows(connection, dataset):
                if dataset == "download_manifest" and str(row.get("downloaded", "")).lower() not in {"yes", "true", "1"}:
                    continue
                if dataset == "extract_manifest" and str(row.get("status", "")).lower() not in EXTRACT_COMPLETE:
                    continue
                if dataset == "mosaic_manifest" and row.get("status") not in MOSAIC_COMPLETE:
                    continue
                value = str(row.get(field_name, ""))
                key = _path_key(value)
                if not value or key in seen:
                    continue
                seen.add(key)
                checks += 1
                if checks % 5000 == 0:
                    progress(f"Checked {checks:,} recorded files...")
                local = Path(value)
                if local.is_file():
                    report.present_files += 1
                    if local.stat().st_size == 0:
                        report.issue("empty_file", "Empty file; inspect before continuing:", value, "error")
                elif key in proof or (dataset == "download_manifest" and _pure(value).name in moved_names):
                    report.absent_with_downstream_proof += 1
                else:
                    report.missing_unresolved_files += 1
                    report.issue("missing_without_proof", "Missing file without downstream proof; recover or investigate:", value, "error")
        connection.rollback()
    gdal = str(config.get("gdal", {}).get("python", ""))
    runtime_path = Path(gdal) if Path(gdal).is_absolute() else APP_ROOT / gdal
    if not gdal or not runtime_path.is_file():
        report.issue("gdal_runtime", "Configure the new computer's GDAL Python executable.", gdal)
    report.issue("machine_setup", "Authenticate Earthdata and Earth Engine on this computer; then run a fresh Automation preflight. No credentials were transferred.")
    return report


def save_validation_report(report: ValidationReport) -> Path:
    """Save an explicit validation result and release the relocation safety gate."""
    root = Path(report.root)
    path = root / "00_logs" / "relocation_validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, vars(report))
    if (root / VALIDATION_FILE).exists():
        _atomic_json(root / VALIDATION_FILE, {"ready": report.valid})
    return path


def _metadata_files(root: Path) -> Iterable[Path]:
    yield root / "project.yaml"
    for folder_name in ("00_logs", "profiles"):
        folder = root / folder_name
        if not folder.exists():
            continue
        for current, directories, files in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories if name not in {"upload_artifacts", "chrome-profile"}]
            for name in files:
                path = Path(current) / name
                if any(part in {"source_pixel_checks", "native_pixel_checks", "verification_reports"} for part in path.relative_to(root).parts):
                    continue  # Immutable scientific reports remain historical evidence.
                if path.suffix.lower() not in {".json", ".yaml", ".yml", ".csv"}:
                    continue
                if not path.resolve().is_relative_to(root):
                    raise ValueError(f"Metadata links outside the copied project: {path}")
                yield path


def _directory_times(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    directories = {root}
    for path in files:
        directories.update(parent for parent in path.parents if parent.is_relative_to(root))
    return [dict(path=str(path.relative_to(root)), atime_ns=path.stat().st_atime_ns, mtime_ns=path.stat().st_mtime_ns)
            for path in sorted(directories, key=lambda item: len(item.parts), reverse=True)]


def _restore_directory_times(root: Path, directories: list[dict[str, Any]]) -> None:
    # Automation chooses its latest checkpoint by directory mtime. A metadata
    # rewrite must not make an older run appear newer than the current run.
    for entry in directories:
        path = root / entry["path"]
        if not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe directory in relocation recovery journal.")
        os.utime(path, ns=(int(entry["atime_ns"]), int(entry["mtime_ns"])))


def _stage_metadata(source: Path, destination: Path, mapper: PathMapper, root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as reader_file, destination.open("w", encoding="utf-8", newline="") as writer_file:
            reader = csv.DictReader(reader_file)
            if not reader.fieldnames:
                return
            writer = csv.DictWriter(writer_file, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                writer.writerow(mapper.value(row))
        return
    with source.open(encoding="utf-8") as handle:
        original = json.load(handle) if source.suffix.lower() == ".json" else yaml.safe_load(handle)
    mapped = mapper.value(original)
    if source.name == "project.yaml":
        mapped["project"]["root"] = str(root)
    if source.name == "automation_run.json":
        mapped["preflight_ok"] = False
        mapped["relocation_requires_preflight"] = True
    with destination.open("w", encoding="utf-8") as handle:
        if source.suffix.lower() == ".json":
            json.dump(mapped, handle, indent=2, ensure_ascii=True)
        else:
            yaml.safe_dump(mapped, handle, sort_keys=False, allow_unicode=False)


def _remap_database(path: Path, mapper: PathMapper, root: Path, progress: Progress) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        schema = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if schema and str(schema[0]) != "1":
            raise ValueError("Unsupported project database schema; relocation was not applied.")
        required = {"dataset", "record_key", "status", "utm_tile", "event_date", "source_path", "output_path", "updated_at", "payload_json"}
        if {row[1] for row in connection.execute("PRAGMA table_info(records)")} != required:
            raise ValueError("Unsupported project records schema; relocation was not applied.")
        counts = _counts(connection)
        connection.execute("BEGIN IMMEDIATE")
        # A separate staging table avoids in-place key collisions and lost history.
        connection.execute("CREATE TABLE relocated_records AS SELECT * FROM records WHERE 0")
        cursor = connection.execute("SELECT * FROM records")
        columns = [column[0] for column in cursor.description]
        insert_sql = f"INSERT INTO relocated_records VALUES ({','.join('?' for _ in columns)})"
        index = 0
        for chunk in iter(lambda: cursor.fetchmany(2000), []):
            updates = []
            for stored in chunk:
                row = dict(stored)
                payload = json.loads(row["payload_json"])
                mapped = mapper.value(payload)
                row["payload_json"] = json.dumps(mapped, separators=(",", ":"), sort_keys=True)
                for key in ("record_key", "source_path", "output_path"):
                    row[key] = mapper.value(row[key])
                # No silent references back to the source computer's data directories.
                for value in _local_paths(mapped):
                    if not any(_under(value, new) for _, new in mapper.mappings):
                        raise ValueError(f"Unmapped historical data path: {value}. Add an old-folder/new-folder mapping and retry.")
                updates.append(tuple(row[column] for column in columns))
            connection.executemany(insert_sql, updates)
            index += len(updates)
            progress(f"Rebasing history: {index:,} / {sum(counts.values()):,} records")
        duplicate = connection.execute("SELECT dataset, record_key FROM relocated_records GROUP BY dataset, record_key HAVING count(*) > 1 LIMIT 1").fetchone()
        if duplicate:
            raise ValueError(f"Relocation would merge distinct history records: {duplicate[0]} / {duplicate[1]}")
        connection.execute("DELETE FROM records")
        connection.execute("INSERT INTO records SELECT * FROM relocated_records")
        connection.execute("DROP TABLE relocated_records")
        for row in connection.execute("SELECT key, value FROM metadata").fetchall():
            mapped = mapper.value(row["value"])
            if mapped != row["value"]:
                connection.execute("UPDATE metadata SET value=? WHERE key=?", (mapped, row["key"]))
        connection.execute("INSERT OR REPLACE INTO metadata VALUES('project_root', ?)", (str(root),))
        connection.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        if _counts(connection) != counts:
            raise ValueError("History counts changed during relocation; changes were rolled back.")
        connection.commit()
        _check_database(connection, progress)
        return counts


def _remap_verification_database(path: Path, mapper: PathMapper) -> None:
    """Migrate operational indexes transactionally; retain original receipt payloads."""
    with closing(sqlite3.connect(path)) as db, db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"artifacts", "runs", "cloud"} if path.name == "workflow_verification.sqlite3" else {"checks"}
        if not required <= tables:
            raise ValueError("Unsupported verification database schema: " + path.name)
        db.execute("CREATE TABLE IF NOT EXISTS path_mappings (seq INTEGER PRIMARY KEY, version INTEGER, mappings TEXT)")
        db.execute("INSERT INTO path_mappings(version,mappings) VALUES (1,?)", (json.dumps(mapper.mappings),))
        if "artifacts" in tables:
            rows = db.execute("SELECT DISTINCT path FROM artifacts").fetchall()
            mapped = [mapper.text(r[0]) for r in rows]
            if len(set(mapped)) != len(rows):
                raise ValueError("Relocation would merge distinct artifact paths")
            # Each original index is rewritten once, including historical revisions.
            db.execute("CREATE TEMP TABLE relocated_paths (old TEXT, new TEXT)")
            db.executemany("INSERT INTO relocated_paths VALUES (?,?)", [(r[0], m) for r, m in zip(rows, mapped)])
            db.execute("UPDATE artifacts SET path=(SELECT new FROM relocated_paths WHERE old=artifacts.path)")
            db.execute("DROP TABLE relocated_paths")
        if "checks" in tables:
            db.execute("CREATE INDEX IF NOT EXISTS checks_filename_id ON checks(filename,id)")


def restore_relocation(path: str | Path, progress: Progress = lambda _: None) -> None:
    """Recover an interrupted metadata installation using its durable backup."""
    root = project_root(path)
    marker = root / PENDING_FILE
    if not marker.exists():
        # A crash during staging cannot have changed any live project metadata.
        if not (root / LOCK_FILE).exists():
            raise ValueError("No interrupted relocation was found. Completed relocation backups remain in .swotflow-relocation; keep the original project as your fallback.")
        (root / LOCK_FILE).unlink()
        return
    with marker.open(encoding="utf-8") as handle:
        state = json.load(handle)
    backup = root / BACKUP_FOLDER / str(state["backup_id"]) / "before"
    if not backup.resolve().is_relative_to(root / BACKUP_FOLDER):
        raise ValueError("Invalid relocation backup location.")
    recovery_files = []
    for relative in state["files"]:
        destination = root / relative
        source = backup / relative
        if not destination.resolve().is_relative_to(root) or not source.resolve().is_relative_to(backup):
            raise ValueError("Unsafe path in relocation recovery journal.")
        if not source.is_file():
            raise ValueError(f"Recovery backup is incomplete: {source}")
        recovery_files.append((source, destination))
    progress("Restoring the pre-relocation database and metadata...")
    _backup(backup / DATABASE_FILE_NAME, root / DATABASE_FILE_NAME, progress)
    for source, destination in recovery_files:
        if destination.suffix == ".sqlite3":
            _backup(source, destination, progress)
            continue
        temporary = destination.with_name(destination.name + ".relocation-tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    marker.unlink()
    (root / LOCK_FILE).unlink(missing_ok=True)
    (root / VALIDATION_FILE).unlink(missing_ok=True)
    _restore_directory_times(root, state.get("directories", []))


def relocate_project(
    path: str | Path,
    extra_mappings: Iterable[tuple[str, str]] = (),
    *,
    confirmed_stopped: bool = False,
    progress: Progress = lambda _: None,
) -> ValidationReport:
    """Relocate only the copied project, with backup, count checks and recovery."""
    if not confirmed_stopped:
        raise ValueError("Stop all processing and upload processes before relocation.")
    root = project_root(path)
    if (root / PENDING_FILE).exists() or (root / LOCK_FILE).exists():
        raise RelocationRequired("Interrupted relocation found. Restore Backup first, then retry relocation.")
    with (root / LOCK_FILE).open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        return _relocate_locked(root, list(extra_mappings), progress)
    finally:
        if not (root / PENDING_FILE).exists():
            (root / LOCK_FILE).unlink(missing_ok=True)


def _relocate_locked(root: Path, extra_mappings: list[tuple[str, str]], progress: Progress) -> ValidationReport:
    document = read_document(root)
    old = str(document.get("project", {}).get("root", ""))
    if not _pure(old).is_absolute():
        raise ValueError("The original project root must be an absolute path.")
    extra_mappings = list(extra_mappings)
    if same_path(old, root) and not extra_mappings:
        (root / LOCK_FILE).unlink()
        return validate_project(root, progress)
    if _under(str(root), old) or _under(old, root):
        raise ValueError("Use a separate copied project folder, not a parent/subfolder of the original project.")
    mapper = PathMapper([(old, str(root)), *extra_mappings])
    mapped_config = mapper.value(document["config"])
    for value in _local_paths(mapped_config):
        if not any(_under(value, new) for _, new in mapper.mappings):
            raise ValueError(f"Unmapped project folder: {value}. Add an explicit old-folder/new-folder mapping.")
    processing = mapped_config.get("processing", {})
    if not same_path(processing.get("database", str(root / DATABASE_FILE_NAME)), root / DATABASE_FILE_NAME) or not same_path(processing.get("logs", str(root / "00_logs")), root / "00_logs"):
        raise ValueError("Copy the database to the project root and logs to its 00_logs folder. External raster folders can use additional mappings.")
    database = root / DATABASE_FILE_NAME
    if not database.is_file():
        raise ValueError(f"Copy the authoritative database first: {database}")
    metadata_files = list(_metadata_files(root))
    directories = _directory_times(root, metadata_files)
    required_space = database.stat().st_size * 6 + sum(path.stat().st_size for path in metadata_files) * 2 + 256 * 1024 * 1024
    required_space += sum(p.stat().st_size * 6 for p in (root / "00_logs").glob("*verification.sqlite3"))
    if shutil.disk_usage(root).free < required_space:
        raise ValueError(f"Relocation needs at least {required_space / 1024**3:.1f} GiB free for database backups and staging.")
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    work = root / BACKUP_FOLDER / backup_id
    before, after = work / "before", work / "after"
    before.mkdir(parents=True)
    after.mkdir()
    progress("Creating a consistent SQLite rollback backup...")
    _backup(database, before / DATABASE_FILE_NAME, progress)
    _backup(before / DATABASE_FILE_NAME, after / DATABASE_FILE_NAME, progress)
    counts = _remap_database(after / DATABASE_FILE_NAME, mapper, root, progress)
    files = []
    for name in ("workflow_verification.sqlite3", "download_verification.sqlite3"):
        relative = Path("00_logs") / name
        source = root / relative
        if source.exists():
            (before / relative).parent.mkdir(parents=True, exist_ok=True)
            (after / relative).parent.mkdir(parents=True, exist_ok=True)
            _backup(source, before / relative, progress)
            _backup(before / relative, after / relative, progress)
            _remap_verification_database(after / relative, mapper)
            files.append(str(relative))
    for source in metadata_files:
        relative = source.relative_to(root)
        progress(f"Rebasing metadata: {relative}")
        saved = before / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, saved)
        _stage_metadata(source, after / relative, mapper, root)
        files.append(str(relative))
    _refresh_exports(after, files)
    state = dict(backup_id=backup_id, files=files, directories=directories, source_root=old, destination_root=str(root), dataset_counts=counts)
    _atomic_json(work / "receipt.json", state)
    _atomic_json(root / PENDING_FILE, state)
    try:
        _atomic_json(root / VALIDATION_FILE, {"ready": False})
        progress("Installing relocated metadata; original project is untouched...")
        _backup(after / DATABASE_FILE_NAME, database, progress)
        for relative in files:
            destination = root / relative
            if destination.suffix == ".sqlite3":
                _backup(after / relative, destination, progress)
                continue
            temporary = destination.with_name(destination.name + ".relocation-tmp")
            shutil.copy2(after / relative, temporary)
            os.replace(temporary, destination)
        _restore_directory_times(root, directories)
        (root / PENDING_FILE).unlink()
    except (OSError, ValueError, sqlite3.Error):
        restore_relocation(root, progress)
        raise
    (root / LOCK_FILE).unlink(missing_ok=True)
    progress("Relocation installed. Validating files and completion history...")
    report = validate_project(root, progress)
    report.backup = str(work)
    if report.dataset_counts != counts:
        raise ValueError("Post-relocation history counts differ. Keep the backup and investigate before processing.")
    _atomic_json(work / "validation.json", vars(report))
    save_validation_report(report)
    return report


def _refresh_exports(staged: Path, files: list[str]) -> None:
    """Prevent stale CSV export statuses from overriding authoritative SQLite rows."""
    with closing(_readonly_database(staged / DATABASE_FILE_NAME)) as connection:
        for relative in files:
            path = staged / relative
            dataset = DATASET_BY_FILENAME.get(path.name.lower())
            if not dataset or Path(relative).parent != Path("00_logs"):
                continue
            initialized = connection.execute("SELECT 1 FROM metadata WHERE key=?", (f"dataset_initialized:{dataset}",)).fetchone()
            if not initialized:
                continue
            with path.open(encoding="utf-8-sig", newline="") as handle:
                columns = list(csv.DictReader(handle).fieldnames or [])
            for row in _rows(connection, dataset):
                columns.extend(key for key in row if key not in columns)
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(_rows(connection, dataset))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--relocate", action="store_true")
    parser.add_argument("--restore", action="store_true", help="Recover an interrupted relocation")
    parser.add_argument("--map", nargs=2, action="append", default=[], metavar=("OLD_FOLDER", "NEW_FOLDER"))
    parser.add_argument("--confirm-stopped", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.restore:
            if not args.confirm_stopped:
                raise ValueError("Stop all project processes and pass --confirm-stopped.")
            restore_relocation(args.project, print)
            return 0
        report = (relocate_project(args.project, args.map, confirmed_stopped=args.confirm_stopped, progress=print)
                  if args.relocate else validate_project(args.project, print))
        print(report.summary())
        if not args.relocate:
            save_validation_report(report)
        if args.report:
            _atomic_json(args.report, vars(report))
        return 0 if report.valid else 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Project relocation/validation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
