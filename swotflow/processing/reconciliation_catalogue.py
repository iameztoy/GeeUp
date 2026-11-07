"""Refresh aged catalogue observations at a verified upload boundary."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from swotflow.catalog.history import CatalogHistory, history_path, recent
from swotflow.core.temporal import acquisition_scope
from swotflow.processing.download import build_download_preview, parse_config


def refresh_stale_catalogue(
    config: Mapping[str, Any], project_root: Path, tile: str,
    start: str, end: str, scan: dict, ledger: CatalogHistory,
) -> tuple[dict, bool]:
    """Return current official metadata without rerunning image processing.

    A fresh retained observation needs no network call. An aged observation is
    refreshed once through the ordinary verified dual-catalogue search. Its
    immutable predecessor, manifests and image files remain intact. Callers must
    reconcile the returned selection/checksums before declaring completeness;
    new inputs or versions must not be silently treated as already processed.

    Raises:
        RuntimeError: The new observation is unavailable, partial or out of scope.
        ValueError: The request does not use verified mode or the same ledger.
    """
    if recent(scan.get("finished", "")):
        return scan, False
    scoped = deepcopy(dict(config))
    scoped.setdefault("download", {}).update(start_date=start, end_date=end, utm_tiles=[tile])
    download = parse_config(scoped, project_root)
    if not download.verified_downloads or history_path(download.manifest_csv).resolve() != ledger.path.resolve():
        raise ValueError("Catalogue refresh requires verified mode and this project's history ledger.")
    try:
        preview = build_download_preview(download)
        refreshed = ledger.snapshot(preview.history_scan_id)
    except Exception as exc:
        # Do not include response URLs or authentication details in stage messages.
        raise RuntimeError(
            "Automatic NASA metadata refresh could not finish "
            f"({type(exc).__name__}). Completed files and histories are retained; "
            "review NASA History, then resume/reconcile."
        ) from exc
    scope = refreshed.get("scope", {})
    ownership = acquisition_scope(config)
    if (refreshed.get("status") != "complete" or not recent(refreshed.get("finished", ""))
            or scope.get("start") != start or scope.get("end") != end or scope.get("tiles") != [tile]
            or any(scope.get(k, "") != ownership.get(k, "")
                   for k in ("acquisition_start_date", "acquisition_end_date"))):
        raise RuntimeError("Automatic NASA metadata refresh is incomplete or outside this tile/date scope; completed work is retained.")
    return refreshed, True
