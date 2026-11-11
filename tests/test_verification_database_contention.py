"""Real SQLite contention tests for parallel verification receipts."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from swotflow.verification import pipeline as p


def _write_receipts(logs: str, worker: int, count: int) -> int:
    store = p.VerificationStore(Path(logs))
    for index in range(count):
        with store.connection(True) as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                       (f"{worker}-{index}", p.utc_now(), "mosaic", json.dumps({"worker": worker})))
    return count


class VerificationDatabaseContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = p.VerificationStore(Path(folder.name))
        with self.store.connection(True):
            pass

    def test_legacy_receipts_survive_wal_migration(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("INSERT INTO runs VALUES ('old','then','mosaic','{}')")
            db.commit()
        with self.store.connection(True) as db:
            db.execute("INSERT INTO runs VALUES ('new','now','mosaic','{}')")
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)

    def test_reader_snapshot_does_not_block_new_receipt(self) -> None:
        with self.store.connection() as reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            with mock.patch.object(p, "SQLITE_TIMEOUT_SECONDS", 0.01):
                with self.store.connection(True) as writer:
                    writer.execute("INSERT INTO runs VALUES ('new','now','mosaic','{}')")
            self.assertEqual(reader.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        with self.store.connection() as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_transient_writer_lock_retries_without_replaying_body(self) -> None:
        blocker = sqlite3.connect(self.store.path)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        executed = []
        with mock.patch.object(p, "SQLITE_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(p.time, "sleep", side_effect=lambda _: blocker.commit()) as delay, \
                self.assertLogs(p._LOG, level="WARNING") as messages:
            with self.store.connection(True) as db:
                executed.append(True)
                db.execute("INSERT INTO runs VALUES ('once','now','mosaic','{}')")
        self.assertEqual(executed, [True])
        self.assertEqual(delay.call_count, 1)
        self.assertIn("writer acquisition", " ".join(messages.output))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_exhausted_lock_is_bounded_and_does_not_claim_success(self) -> None:
        blocker = sqlite3.connect(self.store.path)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        executed = []
        with mock.patch.object(p, "SQLITE_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(p.time, "sleep") as delay, \
                self.assertRaises(sqlite3.OperationalError):
            with self.store.connection(True):
                executed.append(True)
        blocker.rollback()
        self.assertEqual(executed, [])
        self.assertEqual(delay.call_count, p.SQLITE_LOCK_ATTEMPTS - 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_body_failure_rolls_back_and_releases_writer(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid evidence"):
            with self.store.connection(True) as db:
                db.execute("INSERT INTO runs VALUES ('bad','now','mosaic','{}')")
                raise ValueError("invalid evidence")
        with self.store.connection(True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            db.execute("INSERT INTO runs VALUES ('good','now','mosaic','{}')")

    def test_non_contention_errors_are_not_retried(self) -> None:
        for code, message in [(sqlite3.SQLITE_IOERR, "disk I/O error"),
                              (sqlite3.SQLITE_FULL, "database or disk is full"),
                              (sqlite3.SQLITE_READONLY, "attempt to write a readonly database")]:
            with self.subTest(code=code):
                error = sqlite3.OperationalError(message)
                error.sqlite_errorcode = code
                operation = mock.Mock(side_effect=error)
                with mock.patch.object(p.time, "sleep") as delay, self.assertRaises(sqlite3.OperationalError):
                    p._retry_database_lock(operation, "test")
                operation.assert_called_once()
                delay.assert_not_called()

    def test_parallel_workers_preserve_every_receipt(self) -> None:
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            jobs = [pool.submit(_write_receipts, str(self.store.logs), worker, 12) for worker in range(4)]
            self.assertEqual(sum(job.result(timeout=30) for job in jobs), 48)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 48)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
