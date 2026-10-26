"""Optional calendar-year scheduling around the existing tile pipeline.

Scientific workers and cumulative project manifests are shared. Only scheduling,
date ownership, progress/checkpoint identity and storage gates differ.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil
from typing import Any, Mapping

from swotflow.processing import automation as engine


def yearly_config(parent: engine.AutomationConfig, start: str, end: str, tile: str) -> engine.AutomationConfig:
    """Build a child window without changing project data/manifest locations."""
    base = deepcopy(parent.base_config)
    for section in ("processing", "download"):
        base.setdefault(section, {}).update(acquisition_start_date=start, acquisition_end_date=end)
    base["download"].update(start_date=start, end_date=end, utm_tiles=[tile])
    base.setdefault("extract", {})["year_selection"] = "all"
    base.setdefault("automation", {}).update(yearly_batch=True, parent_run_id=parent.run_id)
    if parent.include_upload:
        base.setdefault("upload", {})["completion_mode"] = "wait_for_ee"
    return replace(parent, base_config=base, start_date=start, end_date=end,
                   process_by_year=False, refresh_cmr=True, utm_tiles=[tile],
                   run_id=f"{parent.run_id}_{tile}_{start[:4]}",
                   run_dir=Path(parent.run_dir) / "tiles" / tile / "years" / start[:4])


def settings_identity(config: engine.AutomationConfig) -> dict:
    """Protect completed checkpoints against source/output/processing setting changes."""
    keys = ("processing", "download", "extract", "mosaic", "metadata", "destination_parent", "input_folder")
    result = {key: deepcopy(config.base_config.get(key)) for key in keys}
    result['upload'] = {key: deepcopy(value) for key, value in config.base_config.get('upload', {}).items()
                        if key in {'pyramiding_policy', 'prefix', 'suffix', 'replacement_rules',
                                   'invalid_char_pattern', 'invalid_char_replacement'}}
    return result


def storage_reserve_failure(
    state: engine.AutomationRunState, config: Mapping[str, Any], tile: str, stage: str,
) -> engine.AutomationStageResult | None:
    """Pause before a consuming stage when its actual output volume lacks reserve.

    This is a between-stage reserve check, not a prediction of expansion size or
    a per-file quota. Cleanup can still run when space is low.
    """
    locations = {"download": ("download", "output_folder"),
                 "extract": ("extract", "output_folder"),
                 "mosaic": ("mosaic", "output_folder")}
    if stage not in locations:
        return None
    section, key = locations[stage]
    target = Path(str(config.get(section, {}).get(key) or state.config.project_root))
    if not target.is_absolute():
        target = state.config.project_root / target
    volume = target
    while not volume.exists() and volume != volume.parent:
        volume = volume.parent
    try:
        free_gb = shutil.disk_usage(volume).free / 1024 ** 3
        if free_gb >= state.config.min_free_space_gb:
            return None
        message = (f"Storage reserve reached before {stage}: {free_gb:.1f} GiB free at {target}; "
                   f"{state.config.min_free_space_gb:.1f} GiB required. Free space and resume this run. "
                   "No later year will start; retained files and verification history remain available.")
    except OSError as exc:
        message = f"Cannot check storage at {target}: {exc}. Yearly run paused before {stage}."
    stamp = engine.now_text()
    return engine.AutomationStageResult(state.run_id, tile, stage, "failed", stamp, stamp,
                                        return_code=75, message=message)


def merge_child_results(parent: engine.AutomationRunState, child: engine.AutomationRunState) -> None:
    """Replace one window's displayed history while retaining real UTM identities."""
    start, end = child.config.start_date, child.config.end_date
    tile = child.config.utm_tiles[0]
    parent.stage_results = [r for r in parent.stage_results
                            if (r.tile, r.period_start, r.period_end) != (tile, start, end)]
    parent.stage_results.extend(replace(r, run_id=parent.run_id, period_start=start, period_end=end,
                                       tile=tile if r.tile in {'ALL', 'REPAIR'} else r.tile)
                                for r in child.stage_results)
    engine.write_run_state(parent)


def child_outcome(child: engine.AutomationRunState) -> str:
    """Never advance beyond unresolved failures, uploads or interrupted stages."""
    latest = engine.latest_stage_results_by_key(child)
    if any(r.return_code == 75 for r in latest.values()):
        return "low_space"
    if child.stopped:
        return "stopped"
    if (not child.preflight_ok or child.errors or child.unresolved_upload_count
            or any(r.status == "failed" for r in latest.values())):
        return "incomplete"
    required = {"download", "duplicates", "extract", "raw_cleanup", "mosaic", "extracted_cleanup"}
    if child.config.include_upload:
        required.update({"upload", "ee_sync", "mosaic_cleanup"})
    if any((tile, stage) not in latest for tile in child.config.utm_tiles for stage in required):
        return "incomplete"
    return "complete"


def refresh_parent_expectations(parent: engine.AutomationRunState, child: engine.AutomationRunState) -> None:
    """Refresh only a successfully searched tile/year; retain all other expectations."""
    from swotflow.processing.automation_accounting import read_window_previews
    from swotflow.project.updates import synchronize_yearly_expectations

    tile = child.config.utm_tiles[0]
    latest = engine.latest_stage_results_by_key(child).get((tile, 'download'))
    if latest is None or latest.status not in {'success', 'skipped'}:
        return
    stamp = engine.now_text()
    try:
        previews = read_window_previews(child)
        summary = synchronize_yearly_expectations(
            parent.config, child.config, preview_rows=previews[tile], download_complete=True)
        message = f"Full-range campaign refreshed for this tile/year: {summary['selected_count']} selected inputs."
        status, code = 'success', 0
    except Exception as exc:
        message = f"Annual campaign accounting could not be refreshed: {exc}"
        child.errors.append(message)
        status, code = 'failed', 2
    engine.append_stage_result(child, engine.AutomationStageResult(
        child.run_id, tile, 'campaign_refresh', status, stamp, engine.now_text(),
        return_code=code, message=message, period_start=child.config.start_date,
        period_end=child.config.end_date))


def run_yearly_automation(
    config: engine.AutomationConfig, *, preflight_state: engine.AutomationRunState | None = None,
    progress_callback: engine.ProgressCallback | None = None, stop_event: Any = None,
) -> engine.AutomationRunState:
    """Finish each tile's calendar years before advancing to the next tile."""
    state = preflight_state if preflight_state is not None else engine._preflight_automation(
        config, engine.build_download_preview, progress_callback)
    if not state.preflight_ok:
        return state
    state.config = config
    # Preflight can allocate the run id even when the caller supplied neither id nor path.
    config.run_id, config.run_dir = state.run_id, state.run_dir
    state.started_at, state.finished_at, state.stopped = engine.now_text(), "", False
    state.errors = []
    periods = engine.automation_periods(config)
    old = {(b.get('tile'), b["start_date"], b["end_date"]): b for b in state.year_batches}
    state.year_batches = [dict(old.get((tile, start, end), {}), tile=tile, year=int(start[:4]), start_date=start,
                               end_date=end, run_dir=str(state.run_dir / 'tiles' / tile / "years" / start[:4]),
                               status=old.get((tile, start, end), {}).get("status", "pending"))
                          for tile in config.utm_tiles for start, end in periods]
    engine.write_run_state(state)
    failed_tiles: set[str] = set()
    unresolved_tiles: set[str] = set()
    state.unresolved_upload_count = 0
    state.unresolved_upload_tiles = []
    for batch in state.year_batches:
        tile = batch['tile']
        if tile in failed_tiles:
            continue
        if stop_event is not None and stop_event.is_set():
            state.stopped = True
            break
        child_config = yearly_config(config, batch["start_date"], batch["end_date"], tile)
        child_file = Path(child_config.run_dir) / "automation_run.json"
        child = engine.load_automation_run_state(child_file) if child_file.exists() else None
        if child is not None:
            a, b = child.config, child_config
            if (a.start_date, a.end_date, a.utm_tiles, a.include_upload,
                a.base_config.get("destination_parent")) != (
                    b.start_date, b.end_date, b.utm_tiles, b.include_upload,
                    b.base_config.get("destination_parent")) or settings_identity(a) != settings_identity(b):
                raise ValueError("Saved annual scope differs from this request. Run a new preflight.")
        if batch["status"] == "complete" and child is not None and child_outcome(child) == "complete":
            merge_child_results(state, child)
            if progress_callback:
                for result in engine.latest_stage_results_by_key(child).values():
                    progress_callback(tile, f"{batch['year']}/{result.stage}", result.status,
                                      "Saved year checkpoint: " + result.message)
            continue
        batch["status"] = "running"
        engine.write_run_state(state)

        def report(reported_tile: str, stage: str, status: str, message: str) -> None:
            if child is not None and status != "running":
                merge_child_results(state, child)
            if progress_callback:
                progress_callback(tile if reported_tile in {'ALL', 'REPAIR'} else reported_tile,
                                  f"{batch['year']}/{stage}", status,
                                  f"{batch['start_date']} to {batch['end_date']}: {message}")

        if child is None or not child.preflight_ok:
            child = engine._preflight_automation(child_config, engine.build_download_preview, report)
        else:
            child.config = child_config
            # Historical failures remain in stage_results; fresh reconciliation replaces active errors.
            child.errors = []
        # A new annual child must establish a successful, scoped download/search
        # receipt even if cumulative legacy counts suggest it is already done.
        if not any(r.stage == 'download' and r.status == 'success' for r in child.stage_results):
            for plan in child.tile_plans:
                if plan.classification == 'already complete':
                    plan.classification = 'partial/resumable'
                    plan.message = 'Revalidate this tile/year scope against the retained project history.'
        if child.preflight_ok:
            child = engine._run_automation_normalized(child_config, preflight_state=child,
                                                      progress_callback=report, stop_event=stop_event)
            refresh_parent_expectations(state, child)
            if not child.stopped:
                from swotflow.processing.automation_accounting import verify_window_inputs
                verify_window_inputs(child)
        merge_child_results(state, child)
        batch["status"] = child_outcome(child)
        batch["finished_at"] = child.finished_at
        state.unresolved_upload_count += child.unresolved_upload_count
        unresolved_tiles.update(child.unresolved_upload_tiles)
        state.unresolved_upload_tiles = sorted(unresolved_tiles)
        for warning in child.warnings:
            warning = f"{batch['year']}: {warning}"
            if warning not in state.warnings:
                state.warnings.append(warning)
        if batch["status"] != "complete":
            state.stopped = child.stopped
            message = (f"{tile} / {batch['year']} is {batch['status']}; this tile's later years remain pending. "
                       "Resolve the recorded issue and resume this run.")
            state.errors.extend([f"{batch['year']}: {error}" for error in child.errors])
            if message not in state.errors:
                state.errors.append(message)
            stamp = engine.now_text()
            engine.append_stage_result(state, engine.AutomationStageResult(
                state.run_id, tile, "year_gate", "failed", stamp, stamp, return_code=2,
                message=message, period_start=batch["start_date"], period_end=batch["end_date"]))
            if progress_callback:
                progress_callback(tile, f"{batch['year']}/year_gate", "failed", message)
            failed_tiles.add(tile)
            if child.stopped or not config.continue_on_tile_failure:
                break
        engine.write_run_state(state)
    state.finished_at = engine.now_text()
    engine.write_run_state(state)
    return state
