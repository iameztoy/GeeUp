"""Crash-released local locks for project automation operations."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def automation_lock(project_root: Path) -> Iterator[None]:
    """Reject concurrent preflight/execution, including from another app instance."""
    path = project_root / "00_logs" / ".automation.lock"
    with _operation_lock(path, "Another preflight or automation run is using this project. Wait for it to finish."):
        yield


@contextmanager
def download_folder_lock(folder: Path) -> Iterator[None]:
    """Exclude downloads and partial-file deletion across threads/app instances."""
    path = folder / ".swotflow-download.lock"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("Unsafe download-folder lock file.")
    with _operation_lock(
        path,
        "A download or partial-file cleanup is using this folder. Wait for it to finish.",
    ):
        yield


@contextmanager
def _operation_lock(path: Path, busy_message: str) -> Iterator[None]:
    """Hold a nonblocking OS lock that is released even after a process crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(busy_message) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
