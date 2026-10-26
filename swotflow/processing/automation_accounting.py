"""Retained input accounting before advancing an optional annual batch.

This gate checks recorded source membership, not pixels or physical file
presence. Cleanup may have removed correctly completed intermediate files.
"""
from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from swotflow.core.metadata import parse_swot_l2_hr_raster_metadata, product_identity_key
from swotflow.core.temporal import acquisition_window, within_acquisition_window


def _window(state: Any) -> tuple[str, str]:
    bounds = acquisition_window(state.config.base_config)
    if not bounds[0] or bounds != (state.config.start_date, state.config.end_date):
        raise ValueError("Annual input accounting requires the exact acquisition-start date window.")
    return bounds


def _download_receipt(state: Any, tile: str, start: str, end: str) -> Any:
    receipts = [r for r in state.stage_results
                if r.run_id == state.run_id and r.tile == tile and r.stage == "download"]
    if not receipts or receipts[-1].status not in {"success", "skipped"}:
        raise ValueError(f"{tile}: no successful current or saved download stage.")
    receipt = next((r for r in reversed(receipts) if r.status != "skipped"), None)
    if receipt is None or receipt.status != "success" or receipt.return_code:
        raise ValueError(f"{tile}: a skipped download is not proof of a completed search/download.")
    if receipt.period_start and (receipt.period_start, receipt.period_end) != (start, end):
        raise ValueError(f"{tile}: download receipt belongs to another date window.")
    path = Path(receipt.config_path) if receipt.config_path else None
    if path is None or not path.is_file():
        raise ValueError(f"{tile}: successful download has no retained configuration.")
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"{tile}: retained download configuration is invalid.")
    download = config.get("download", {})
    if (acquisition_window(config) != (start, end)
            or download.get("start_date") != start or download.get("end_date") != end
            or download.get("utm_tiles") != [tile]):
        raise ValueError(f"{tile}: retained download configuration differs from this tile/date scope.")
    report = Path(str(download.get("report_csv", ""))).resolve()
    if report != (state.run_dir / tile / "download_preview.csv").resolve():
        raise ValueError(f"{tile}: retained download did not use this batch's preview report.")
    return receipt


def read_window_previews(state: Any) -> dict[str, list[dict[str, str]]]:
    """Return authoritative per-tile rows, or fail closed for unknown searches.

An empty CSV with the actual report header is valid only after a successful
download receipt for the exact retained tile/date configuration.
"""
    start, end = _window(state)
    previews: dict[str, list[dict[str, str]]] = {}
    required = {"file_name", "utm_tile", "start_time", "status", "selected_for_download"}
    best = state.config.base_config.get("download", {}).get("product_version_filter", "best") == "best"
    for tile in state.config.utm_tiles:
        _download_receipt(state, tile, start, end)
        report = state.run_dir / tile / "download_preview.csv"
        if not report.is_file():
            raise ValueError(f"{tile}: completed download preview is missing.")
        with report.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not required.issubset(reader.fieldnames or ()):
                raise ValueError(f"{tile}: download preview is empty or has an invalid header.")
            rows = list(reader)
        seen: set[str] = set()
        selected_identities: set[str] = set()
        for row in rows:
            name = str(row.get("file_name", "") or "")
            if not name or name in seen or None in row or any(v is None for v in row.values()):
                raise ValueError(f"{tile}: duplicate or malformed download preview row.")
            seen.add(name)
            selected = str(row.get("selected_for_download", "")).lower()
            if selected not in {"yes", "no"}:
                raise ValueError(f"{tile}: ambiguous download selection for {name}.")
            if selected == "no":
                if not str(row.get("status", "")).upper().startswith("EXCLUDED"):
                    raise ValueError(f"{tile}: unselected source has no recorded exclusion: {name}.")
                continue
            metadata = parse_swot_l2_hr_raster_metadata(name)
            if (metadata is None or metadata.fields["coordinate_system"] != tile
                    or row["utm_tile"] != tile or not within_acquisition_window(name, start, end)):
                raise ValueError(f"{tile}: selected source falls outside this batch: {name}.")
            source_start = metadata.fields["range_beginning"]
            expected_date = f"{source_start[:4]}-{source_start[4:6]}-{source_start[6:8]}"
            if row["start_time"][:10] != expected_date:
                raise ValueError(f"{tile}: source date differs from the selected filename: {name}.")
            if str(row.get("status", "")).upper().startswith("EXCLUDED"):
                raise ValueError(f"{tile}: selected source has an exclusion status: {name}.")
            identity = product_identity_key(name)
            if best and identity in selected_identities:
                raise ValueError(f"{tile}: conflicting preferred processing versions in the preview.")
            selected_identities.add(identity)
        previews[tile] = rows
    return previews


def verify_window_inputs(state: Any) -> None:
    """Require annual input accounting; retain failures without altering old runs."""
    if not state.config.base_config.get("automation", {}).get("yearly_batch", False):
        return
    from swotflow.processing import automation as engine
    from swotflow.project.database import read_project_rows
    from swotflow.project.insights import build_update_coverage_tile_rows

    started = engine.now_text()
    try:
        start, end = _window(state)
    except (ValueError, TypeError) as exc:
        start, end = state.config.start_date, state.config.end_date
        scope_error = str(exc)
    else:
        scope_error = ""
    verified = bool(state.config.base_config.get("download", {}).get("verified_downloads", False))
    previews = None
    preview_error = ""
    if not verified and not scope_error:
        try:
            previews = read_window_previews(state)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            preview_error = str(exc)
    for tile in state.config.utm_tiles:
        try:
            if scope_error or preview_error:
                raise ValueError(scope_error or preview_error)
            if verified:
                receipts = [r for r in state.stage_results if r.run_id == state.run_id
                            and r.tile == tile and r.stage == "reconciliation"]
                receipt = receipts[-1] if receipts else None
                if (receipt is None or receipt.status not in {"success", "warning"} or receipt.return_code
                        or (receipt.period_start, receipt.period_end) != (start, end)):
                    raise ValueError("No successful verified reconciliation for this exact tile/date window.")
                # The verified engine already checked immutable NASA selections,
                # local receipts and, when enabled, bound current EE assets.
                continue
            config = deepcopy(state.config.base_config)
            config.setdefault("download", {}).update(start_date=start, end_date=end, utm_tiles=[tile])
            expected = previews[tile]
            rows = build_update_coverage_tile_rows(
                config=config, download_preview_rows=expected,
                download_rows=read_project_rows(engine.project_manifest_path(config, "download", "manifest_csv"), "download_manifest"),
                extract_rows=read_project_rows(engine.project_manifest_path(config, "extract", "manifest_csv"), "extract_manifest"),
                mosaic_rows=read_project_rows(engine.project_manifest_path(config, "mosaic", "manifest_csv"), "mosaic_manifest"),
                upload_rows=read_project_rows(engine.upload_report_path(config), "upload_report"),
            )
            coverage = next((row for row in rows if row[0] == tile), None)
            if coverage is None:
                raise ValueError("No coverage accounting row for this tile.")
            total, downloaded, extracted, mosaicked, uploaded = coverage[1:6]
            selected = sum(row["selected_for_download"].lower() == "yes" for row in expected)
            if total != selected:
                raise ValueError(f"Only {total} of {selected} selected source records entered accounting.")
            if (min(downloaded, extracted, mosaicked) < total
                    or (state.config.include_upload and uploaded < total)):
                raise ValueError(f"{total} selected inputs; {downloaded} downloaded, {extracted} extracted, "
                                 f"{mosaicked} mosaic sources, {uploaded} uploaded/verified. Retained lineage is incomplete.")
            message = (f"Input accounting complete for {total} selected sources from this tile/date download; "
                       "retained stage lineage checked. Pixel correctness is not certified by this check.")
            if not state.config.include_upload:
                message += " EE upload remains pending."
            status, code = "warning", 0
            warning = f"{tile}: {message}"
            if warning not in state.warnings:
                state.warnings.append(warning)
        except (OSError, ValueError, TypeError) as exc:
            message, status, code = f"Annual input accounting incomplete: {exc}", "failed", 2
            error = f"{tile}: {message}"
            if error not in state.errors:
                state.errors.append(error)
        engine.append_stage_result(state, engine.AutomationStageResult(
            state.run_id, tile, "reconciliation", status, started, engine.now_text(),
            return_code=code, message=message, period_start=start, period_end=end))
