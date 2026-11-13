"""Real OS-lock regression coverage for the verification writer gate."""
from __future__ import annotations

import errno
import hashlib
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from swotflow.verification import pipeline as p
from swotflow.verification import writer as w


def _slow_writer(logs: str, number: int, start: Any, ready: Any, results: Any) -> None:
    """Compete with a timeout too short for another worker's transaction."""
    p.SQLITE_TIMEOUT_SECONDS = 0.01
    p.SQLITE_LOCK_ATTEMPTS = 1
    w.WRITER_WAIT_SECONDS = 10
    w.WRITER_POLL_SECONDS = 0.01
    store = p.VerificationStore(Path(logs))
    ready.put(number)
    if not start.wait(15):
        results.put((number, "start timeout"))
        return
    try:
        for attempt in range(2):
            with store.connection(True) as db:
                db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                           (f"{number}-{attempt}", p.utc_now(), "mosaic", "{}"))
                time.sleep(0.15)
        results.put((number, "ok"))
    except (OSError, sqlite3.Error) as exc:
        results.put((number, str(exc)))


def _crashed_writer(logs: str, ready: Any) -> None:
    """Exit inside an uncommitted transaction, bypassing Python cleanup."""
    store = p.VerificationStore(Path(logs))
    with store.connection(True) as db:
        db.execute("INSERT INTO runs VALUES ('interrupted','then','mosaic','{}')")
        ready.set()
        os._exit(37)


class VerificationWriterQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = p.VerificationStore(Path(folder.name))
        with self.store.connection(True):
            pass

    def test_slow_parallel_writers_wait_outside_sqlite(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        start, ready, results = ctx.Event(), ctx.Queue(), ctx.Queue()
        workers = [ctx.Process(target=_slow_writer, args=(str(self.store.logs), n, start, ready, results))
                   for n in range(4)]
        try:
            for worker in workers:
                worker.start()
            self.assertEqual({ready.get(timeout=30) for _ in workers}, set(range(4)))
            start.set()
            self.assertEqual(sorted(results.get(timeout=30) for _ in workers), [(n, "ok") for n in range(4)])
            for worker in workers:
                worker.join(15)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(15)
            ready.close()
            results.close()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 8)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_crash_releases_gate_and_rolls_back_uncommitted_receipt(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        worker = ctx.Process(target=_crashed_writer, args=(str(self.store.logs), ready))
        worker.start()
        try:
            self.assertTrue(ready.wait(20))
            worker.join(20)
            self.assertEqual(worker.exitcode, 37)
            with mock.patch.object(w, "WRITER_WAIT_SECONDS", 0.1):
                with self.store.connection(True) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                    db.execute("INSERT INTO runs VALUES ('resumed','now','mosaic','{}')")
            with self.store.connection() as db:
                self.assertEqual(db.execute("SELECT id FROM runs").fetchone()[0], "resumed")
        finally:
            if worker.is_alive():
                worker.terminate()
            worker.join(15)

    def test_gate_timeout_never_opens_waiters_database_connection(self) -> None:
        real_connect = sqlite3.connect
        with w.verification_writer(self.store.path):
            with mock.patch.object(w, "WRITER_WAIT_SECONDS", 0.02), \
                    mock.patch.object(w, "WRITER_POLL_SECONDS", 0.005), \
                    mock.patch.object(p.sqlite3, "connect", wraps=real_connect) as connect, \
                    self.assertLogs(w._LOG, level="ERROR"), \
                    self.assertRaisesRegex(sqlite3.OperationalError, "^database is locked$"):
                with self.store.connection(True):
                    self.fail("Timed-out writer entered the transaction body")
            connect.assert_not_called()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_reader_ignores_writer_gate_and_other_projects_remain_independent(self) -> None:
        with w.verification_writer(self.store.path), mock.patch.object(w, "WRITER_WAIT_SECONDS", 0.02):
            with self.store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            other = p.VerificationStore(self.store.logs / "other-project")
            with other.connection(True) as db:
                db.execute("INSERT INTO runs VALUES ('other','now','mosaic','{}')")

    def test_non_contention_lock_failure_is_not_retried(self) -> None:
        with mock.patch.object(w, "_lock", side_effect=OSError(errno.EIO, "disk I/O error")), \
                mock.patch.object(w.time, "sleep") as sleep, self.assertRaises(OSError):
            with self.store.connection(True):
                self.fail("Failed lock entered transaction")
        sleep.assert_not_called()

    def test_hashing_runs_before_writer_gate_and_keeps_source_binding(self) -> None:
        raw = self.store.logs / "source.nc"
        output = self.store.logs / "source.tif"
        raw.write_bytes(b"raw bytes")
        output.write_bytes(b"extracted bytes")
        expected = hashlib.md5(raw.read_bytes()).hexdigest()
        state = [None] * 5 + [raw.stat().st_size, "md5", expected]
        real_hash = p.file_hash
        def hashing(path: Path, algorithm: str = "sha256") -> str:
            if path == raw:
                with self.store.connection(True) as db:
                    db.execute("INSERT INTO runs VALUES ('during-hash','now','extract','{}')")
            return real_hash(path, algorithm)
        with mock.patch.object(p, "download_proof", return_value={"valid": True, "state": state}), \
                mock.patch.object(p, "file_hash", side_effect=hashing), \
                mock.patch.object(w, "WRITER_WAIT_SECONDS", 0.02):
            proof = self.store.record_artifact("extract", output, [raw],
                SimpleNamespace(target_crs_mode="original", resampling_alg="near"), "fixture", {})
        self.assertEqual(proof["sources"]["source.nc"]["hash"], expected)
        self.assertEqual(self.store.artifact(output), proof)
        self.assertTrue(p.artifact_current(proof))


if __name__ == "__main__":
    unittest.main()
