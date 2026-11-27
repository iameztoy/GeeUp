"""Bounded GCS transfers with one writer for EE, reports and project history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
from pathlib import Path
from queue import SimpleQueue, Empty
import time

from swotflow.earthengine.cloud.journal import utc
from swotflow.earthengine.cloud.transport import CloudHTTPError, CloudServiceError


def run_parallel(worker, items, deadline):
    """Drain active transfers on deadline/fail-fast; retain all unsettled work.

    Only byte transfer and protected per-file session files run in threads.
    Queue mutation, byte reservations, ingestion submission and deletion stay
    on the owning thread. Every prepared row reserves its full object size.
    """
    remaining = list(items)
    futures = {}
    events = SimpleQueue()
    attempts = {}
    retry_after = {}
    worker._transfers_in_flight = set()
    limit = worker.cloud.concurrent_uploads
    max_tasks = worker.config.upload.max_active_ingestions or 2
    retries = worker.config.upload.retry_attempts
    poll = max(0.05, worker.config.execution.task_poll_seconds)
    next_poll = 0.0
    halted = False

    def eligible(row, phase):
        key = (row["asset_id"], phase)
        return attempts.get(key, 0) < retries and time.monotonic() >= retry_after.get(key, 0)

    def failure(row, phase, exc):
        nonlocal halted
        key = (row["asset_id"], phase)
        if isinstance(exc, ValueError):
            row.update(state="BLOCKED", error=str(exc))
        else:
            attempts[key] = attempts.get(key, 0) + 1
            retry_after[key] = time.monotonic() + worker.config.upload.retry_wait_seconds
            row["error"] = (str(exc) if isinstance(exc, (CloudHTTPError, CloudServiceError)) else
                            f"Cloud attempt interrupted ({type(exc).__name__}); retained for resumable retry.")
        worker.save(row)
        worker.logger.warning("%s: %s", row["asset_id"], row["error"])
        halted = halted or worker.config.upload.fail_fast

    def progress_callback(row):
        last = [0.0]
        def progress(offset):
            now = time.monotonic()
            if now - last[0] >= poll or offset == row["size"]:
                events.put((row["asset_id"], offset, row["size"]))
                last[0] = now
        return progress

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="swot-cloud-transfer") as pool:
        while True:
            now = time.monotonic()
            if now >= deadline and not halted:
                worker.logger.warning("Cloud deadline reached; finishing active transfers and saving pending work.")
                halted = True
            for future in list(futures):
                if not future.done():
                    continue
                row = futures.pop(future)
                worker._transfers_in_flight.discard(row["asset_id"])
                try:
                    generation = future.result()
                    row.update(state="STAGED", generation=generation, staged_at=utc(), error="")
                    worker.save(row)
                except Exception as exc:
                    failure(row, "transfer", exc)
            while True:
                try:
                    aid, offset, size = events.get_nowait()
                except Empty:
                    break
                worker.logger.info("Cloud transfer %.1f%%: %s", 100 * offset / size, aid)
            if now >= next_poll:
                worker.reconcile()
                next_poll = now + poll
            if halted:
                if not futures:
                    break
                wait(futures, timeout=min(poll, 1), return_when=FIRST_COMPLETED)
                continue

            rows = worker.rows(pending_only=True)
            active = sum(r["state"] in {"INGESTING", "SUBMITTING"} for r in rows)
            for row in rows:
                if row["state"] not in {"STAGED", "SUBMITTING"} or not eligible(row, "submit"):
                    continue
                if row["state"] != "SUBMITTING" and active >= max_tasks:
                    continue
                already_active = row["state"] == "SUBMITTING"
                try:
                    worker.advance(row)
                except Exception as exc:
                    failure(row, "submit", exc)
                if not already_active and row["state"] in {"INGESTING", "SUBMITTING"}:
                    active += 1
                if halted:
                    break
            if halted:
                continue

            # Reserve and launch only available slots, respecting aggregate staged bytes.
            candidates = [r for r in worker.rows(pending_only=True)
                          if r["state"] in {"PREPARED", "TRANSFERRING"}
                          and r["asset_id"] not in worker._transfers_in_flight and eligible(r, "transfer")]
            while len(futures) < limit:
                row = candidates.pop(0) if candidates else None
                if row is None:
                    if not remaining:
                        break
                    item = remaining[0]
                    old = worker.journal.get(item.asset_id)
                    if old is None and worker.journal.live_bytes(worker.cloud.bucket) + item.local_file.stat().st_size > worker.cloud.staging_limit_gib * 2**30:
                        if not futures and not active:
                            worker.logger.warning(worker.staging_cap_message(item))
                        break
                    remaining.pop(0)
                    row = worker.prepare(item)
                    if row["state"] not in {"PREPARED", "TRANSFERRING"}:
                        continue
                remaining = [i for i in remaining if i.asset_id != row["asset_id"]]
                if row["asset_id"] in worker._transfers_in_flight:
                    continue
                try:
                    worker.prepare_transfer(row)
                    worker._transfers_in_flight.add(row["asset_id"])
                    future = pool.submit(worker.transport.upload_isolated, deepcopy(row), Path(row["local_file"]),
                                         worker.secrets, progress_callback(row))
                    futures[future] = row
                except Exception as exc:
                    worker._transfers_in_flight.discard(row["asset_id"])
                    failure(row, "transfer", exc)
                    if halted:
                        break
            if futures:
                wait(futures, timeout=min(poll, 1), return_when=FIRST_COMPLETED)
                continue
            pending = worker.rows(pending_only=True)
            if not remaining and (not pending or (worker.config.upload.completion_mode == "submit_and_continue"
                                                  and all(r["state"] == "INGESTING" for r in pending))):
                break
            monitorable = any(r["state"] == "INGESTING" and r.get("operation") for r in pending)
            active_now = sum(r["state"] in {"INGESTING", "SUBMITTING"} for r in pending)
            retryable = any(r["state"] in {"PREPARED", "TRANSFERRING", "STAGED", "SUBMITTING"} and
                            (r["state"] != "STAGED" or active_now < max_tasks) and
                            attempts.get((r["asset_id"], "transfer" if r["state"] in {"PREPARED", "TRANSFERRING"} else "submit"), 0) < retries
                            for r in pending)
            if not monitorable and not retryable:
                worker.logger.warning("Cloud queue/cap requires review; pending or failed copies retained for Resume.")
                break
            time.sleep(min(poll, 1))
    return remaining
