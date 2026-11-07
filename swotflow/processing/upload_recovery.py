"""Bounded upload recovery and explicit, recoverable annual exceptions."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any

RECOVERY_ROUNDS = 2
RECOVERY_DELAY_SECONDS = 30


def recover_uploads(state: Any, config: Any, gdal_python: Path,
                    progress_callback: Any = None, stop_event: Any = None) -> list[Any]:
    """Reconcile before each retry; checkpoint every result and honor pauses."""
    from swotflow.processing import automation as engine
    if not config.include_upload or state.stopped:
        return []
    results = []
    attempt_dir = state.run_dir / "upload_repair" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    def stopped() -> bool:
        if stop_event is not None and stop_event.is_set():
            state.stopped = True
        return state.stopped

    def execute(stage: str, path: Path, round_number: int) -> Any:
        if progress_callback:
            progress_callback("REPAIR", stage, "running", f"Automatic upload recovery {round_number}/{RECOVERY_ROUNDS}: {stage}.")
        result = engine.execute_command_result(state, "REPAIR", stage, path, gdal_python,
                                               progress_callback=progress_callback)
        result.message = f"Automatic upload recovery {round_number}/{RECOVERY_ROUNDS}. {result.message}"
        engine.append_stage_result(state, result)
        results.append(result)
        if progress_callback:
            progress_callback("REPAIR", stage, result.status, result.message)
        return result

    for number in range(1, RECOVERY_ROUNDS + 1):
        if stopped() or not engine.retryable_upload_candidates(config.base_config, config.utm_tiles):
            break
        if number > 1:
            if stop_event is not None:
                stop_event.wait(RECOVERY_DELAY_SECONDS)
            else:
                time.sleep(RECOVERY_DELAY_SECONDS)
            if stopped():
                break
        path = engine.write_tile_config(engine.build_upload_repair_config(config.base_config, config.utm_tiles),
                                        attempt_dir / f"round_{number}" / "automation_config.yaml")
        # Never resubmit based solely on a stale error, including a timed-out monitor.
        if execute("ee_sync", path, number).status == "failed" or stopped():
            break
        if not engine.retryable_upload_candidates(config.base_config, config.utm_tiles):
            break
        upload = execute("upload", path, number)
        if stopped():
            break
        sync = execute("ee_sync", path, number)
        if upload.status == "failed" or sync.status == "failed":
            break  # System/login failures require attention, not repeated launches.
    return results


def retained_upload_exceptions(report: dict, store: Any, config: dict) -> list[dict]:
    """Allow postponement only for exact sources in unchanged local mosaics.

    EE completeness remains false. Missing sources, changed bytes, duplicate
    ownership and uncertain catalogue/EE provenance cannot use this exception.
    Hash each pending mosaic once, not every source or successful mosaic.
    """
    from swotflow.processing import automation as engine
    from swotflow.verification.pipeline import artifact_current
    from swotflow.core.temporal import acquisition_window, within_acquisition_window
    if report.get("blockers") or report.get("input_accounting_complete"):
        return []
    pending = {}
    for row in report["rows"]:
        if row["state"] == "mosaic_pending_upload" and not row["asset_ids"]:
            pending[row["filename"]] = row["source_checksum"]
        elif not (len(row["asset_ids"]) == 1 and
                  (row["state"] == "verified_active" or row.get("revision_bound_assets"))):
            return []
    if not pending:
        return []
    start, end = acquisition_window(config)
    entries, represented = [], {}
    allowed = engine.RETRYABLE_UPLOAD_STATUSES | {"READY", "RUNNING"}
    for row in engine.read_project_rows(engine.upload_report_path(config), "upload_report"):
        path = Path(row.get("local_file", ""))
        if (row.get("final_status", "").upper() not in allowed or
                not within_acquisition_window(path, start, end)):
            continue
        proof = store.artifact(path)
        if not proof or proof["stage"] != "mosaic":
            continue
        sources = {n: c for n, c in proof["sources"].items() if n in pending}
        if not sources:
            continue
        if any(pending[n] != c or n in represented for n, c in sources.items()) or not artifact_current(proof):
            return []
        represented.update(sources)
        entries.append({"asset_id": row["asset_id"], "local_file": str(path),
                        "status": row["final_status"], "last_error": row.get("error_message", ""),
                        "sha256": proof["sha256"], "sources": sources,
                        "reason": "Upload unresolved; local mosaic matches its processing receipt. Corruption is not established.",
                        "start_date": start, "end_date": end, "scan_id": report["scan_id"]})
    return entries if represented == pending else []
