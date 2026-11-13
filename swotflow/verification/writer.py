"""Cross-process admission for short verification database writes.

Raster processing and hashing happen before this gate. Readers do not take it.
The OS releases the byte/file lock if its owner exits, including after a crash;
the persistent lock file is never deleted to avoid splitting waiting writers.
"""
from __future__ import annotations

import errno
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

WRITER_WAIT_SECONDS = 300.0
WRITER_POLL_SECONDS = 0.05
WRITER_NOTICE_SECONDS = 30.0
_LOG = logging.getLogger(__name__)


def _lock(handle: BinaryIO) -> None:
    """Attempt one nonblocking lock; the caller classifies contention."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    """Release only this handle's lock; close also releases it on failure."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def verification_writer(database: Path) -> Iterator[None]:
    """Admit one verification writer before it opens a SQLite transaction.

    Wait only for OS-lock contention, for a bounded five minutes by default.
    Permissions, filesystem and other lock failures propagate immediately.
    Timeout retains SQLite's existing lock-error classification, so failed
    receipts stay incomplete and the narrow mosaic Resume recovery still applies.
    """
    path = database.resolve().with_name(".verification-writer.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        started = time.monotonic()
        deadline = started + WRITER_WAIT_SECONDS
        next_notice = started + WRITER_NOTICE_SECONDS
        while True:
            try:
                _lock(handle)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                now = time.monotonic()
                if now >= deadline:
                    _LOG.error("Verification writer remained busy for %.1f seconds; "
                               "no success receipt was written. Resume retains the affected files.", now - started)
                    raise sqlite3.OperationalError("database is locked") from exc
                if now >= next_notice:
                    _LOG.warning("Waiting for another verification writer (%.1f seconds); "
                                 "no database transaction is open in this waiting worker.", now - started)
                    next_notice = now + WRITER_NOTICE_SECONDS
                time.sleep(min(WRITER_POLL_SECONDS, deadline - now))
        try:
            yield
        finally:
            _unlock(handle)
