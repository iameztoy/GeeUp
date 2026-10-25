"""Conservative, opt-in cleanup of disposable uploader diagnostics only."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from swotflow.project.lock import automation_lock
from swotflow.project.relocation import assert_project_location, read_document


CATEGORIES = {
    "screenshots": "Upload screenshots",
    "html": "Upload HTML snapshots",
    "logs": "Uploader text logs",
}
DEFAULT_CATEGORIES = ("screenshots", "html")
DEFAULT_KEEP_DAYS = 7
AUDIT_NAME = "diagnostic_cleanup.jsonl"
ARTIFACT_NAME = re.compile(r".+_\d{8}_\d{6}\.(png|html)$", re.IGNORECASE)
LOG_NAME = re.compile(r"ee_ui_uploader_\d{8}_\d{6}\.log$", re.IGNORECASE)
Progress = Callable[[str], None]


@dataclass(frozen=True)
class DiagnosticFile:
    relative_path: str
    category: str
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


@dataclass
class DiagnosticPreview:
    project_root: Path
    keep_days: int
    candidates: list[DiagnosticFile] = field(default_factory=list)
    recent_files: int = 0
    recent_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.candidates)


@dataclass
class DiagnosticDeletion:
    deleted_files: int = 0
    deleted_bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    audit_path: Path | None = None


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _checked_path(root: Path, relative: str) -> Path:
    """Reject traversal, links and junctions at every component below the root."""
    name = Path(relative)
    if name.is_absolute() or name.drive or ".." in name.parts or not name.parts:
        raise ValueError("Path is outside the diagnostic cleanup scope.")
    current = root
    for part in name.parts:
        current = current / part
        if _is_link(current.lstat()):
            raise ValueError(f"Links/junctions are protected: {relative}")
    current.resolve().relative_to(root)
    return current


def _project_root(path: Path) -> Path:
    root = Path(path).expanduser().absolute()
    # Reject aliases instead of allowing a copied preview to target another tree.
    for component in (root, *root.parents):
        if _is_link(component.lstat()):
            raise ValueError("Diagnostic cleanup does not follow project links/junctions.")
    root = root.resolve()
    _checked_path(root, "project.yaml")
    _checked_path(root, "00_logs")
    assert_project_location(root / "project.yaml", read_document(root))
    return root


def _category(relative: str) -> str | None:
    parts = Path(relative).parts
    if len(parts) == 3 and parts[:2] == ("00_logs", "upload_artifacts"):
        match = ARTIFACT_NAME.fullmatch(parts[2])
        if match:
            return "screenshots" if match[1].lower() == "png" else "html"
    if len(parts) == 2 and parts[0] == "00_logs" and LOG_NAME.fullmatch(parts[1]):
        return "logs"
    return None


def _file_record(root: Path, relative: str) -> DiagnosticFile:
    category = _category(relative)
    if category is None:
        raise ValueError("Not a recognized disposable diagnostic file.")
    info = _checked_path(root, relative).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Only regular, non-hard-linked diagnostic files are eligible.")
    return DiagnosticFile(relative, category, info.st_size, info.st_mtime_ns,
                          info.st_ctime_ns, info.st_dev, info.st_ino)


def _retention_cutoff(keep_days: int, now: float | None = None) -> int:
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ValueError("Keep at least the most recent 1 day of diagnostics.")
    return int(((time.time() if now is None else now) - keep_days * 86400) * 1_000_000_000)


def preview_diagnostics(
    project_root: Path, *, keep_days: int = DEFAULT_KEEP_DAYS,
    categories: Iterable[str] = DEFAULT_CATEGORIES, progress: Progress | None = None,
) -> DiagnosticPreview:
    """Read only known diagnostic locations; never scan/delete project history."""
    cutoff = _retention_cutoff(keep_days)
    selected = set(categories)
    if not selected or not selected.issubset(CATEGORIES):
        raise ValueError("Select at least one supported diagnostic category.")
    root = _project_root(project_root)
    preview = DiagnosticPreview(root, keep_days)
    folders = []
    if selected & {"screenshots", "html"}:
        folders.append("00_logs/upload_artifacts")
    if "logs" in selected:
        folders.append("00_logs")
    checked = 0
    for folder in folders:
        try:
            directory = _checked_path(root, folder)
            with os.scandir(directory) as entries:
                for entry in entries:
                    relative = (Path(folder) / entry.name).as_posix()
                    if _category(relative) not in selected:
                        continue
                    try:
                        item = _file_record(root, relative)
                    except (OSError, ValueError) as exc:
                        preview.warnings.append(f"{relative}: {exc}")
                        continue
                    checked += 1
                    if item.mtime_ns >= cutoff:
                        preview.recent_files += 1
                        preview.recent_bytes += item.size_bytes
                    else:
                        preview.candidates.append(item)
                    if progress and checked % 250 == 0:
                        progress(f"Inspected {checked:,} diagnostic files")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            preview.warnings.append(f"{folder}: {exc}")
    preview.candidates.sort(key=lambda item: (item.category, item.mtime_ns, item.relative_path))
    return preview


def delete_diagnostics(
    preview: DiagnosticPreview, selected: Iterable[DiagnosticFile], *,
    confirmed_stopped: bool = False, progress: Progress | None = None,
) -> DiagnosticDeletion:
    """Delete only unchanged previewed diagnostics, leaving all history intact."""
    if not confirmed_stopped:
        raise ValueError("Stop all processing/upload processes and confirm before deleting diagnostics.")
    root = _project_root(preview.project_root)
    cutoff = _retention_cutoff(preview.keep_days)
    allowed = {item.relative_path: item for item in preview.candidates}
    requested = list(dict.fromkeys(selected))
    if any(allowed.get(item.relative_path) != item for item in requested):
        raise ValueError("Deletion selection does not match the preview. Preview again.")
    result = DiagnosticDeletion()
    if not requested:
        return result
    lock_path = root / "00_logs" / ".automation.lock"
    if lock_path.exists() or lock_path.is_symlink():
        _checked_path(root, "00_logs/.automation.lock")
        if not lock_path.is_file() or lock_path.stat().st_nlink != 1:
            raise ValueError("Unsafe project lock path.")
    # This also excludes Automation in other app instances. Manual upload terminals
    # are not covered by that lock, hence the separate explicit stopped confirmation.
    with automation_lock(root):
        _project_root(root)
        audit = root / "00_logs" / AUDIT_NAME
        if audit.exists() or audit.is_symlink():
            _checked_path(root, f"00_logs/{AUDIT_NAME}")
            if not audit.is_file() or audit.stat().st_nlink != 1:
                raise ValueError("Unsafe diagnostic cleanup audit path.")
        result.audit_path = audit
        with audit.open("a", encoding="utf-8") as handle:
            def record(item: DiagnosticFile, status: str, message: str = "") -> None:
                handle.write(json.dumps({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "file": item.relative_path, "category": item.category,
                    "size_bytes": item.size_bytes, "status": status, "message": message,
                }) + "\n")
                handle.flush()

            for index, item in enumerate(requested, 1):
                try:
                    current = _file_record(root, item.relative_path)
                    if current != item or current.mtime_ns >= cutoff:
                        raise ValueError("File changed since preview or is inside the retention window.")
                except (OSError, ValueError) as exc:
                    result.skipped.append(f"{item.relative_path}: {exc}")
                    record(item, "skipped", str(exc))
                    continue
                record(item, "delete_requested")
                try:
                    _checked_path(root, item.relative_path).unlink()
                except (OSError, ValueError) as exc:
                    result.skipped.append(f"{item.relative_path}: {exc}")
                    record(item, "failed", str(exc))
                else:
                    result.deleted_files += 1
                    result.deleted_bytes += item.size_bytes
                    record(item, "deleted")
                if progress and (index % 50 == 0 or index == len(requested)):
                    progress(f"Checked {index:,}/{len(requested):,}; deleted {result.deleted_files:,}")
    return result
