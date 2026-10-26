"""Persistent date-window update campaigns for SWOTFlow projects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from swotflow.project.database import ProjectDatabase, database_path_for, read_project_rows, upsert_project_rows
from swotflow.project.lock import automation_lock
from swotflow.core.temporal import acquisition_scope


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def config_mapping(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    manifest_csv = Path(str(getattr(config, "manifest_csv", "") or "00_logs/download_manifest.csv"))
    return {
        "processing": {"logs": str(manifest_csv.parent)},
        "download": {
            "collection_short_name": str(getattr(config, "collection_short_name", "") or ""),
            "product_version_filter": str(getattr(config, "product_version_filter", "best") or "best"),
            "start_date": str(getattr(config, "start_date", "") or ""),
            "end_date": str(getattr(config, "end_date", "") or ""),
            "utm_tiles": list(getattr(config, "utm_tiles", []) or []),
            **acquisition_scope({"acquisition_start_date": getattr(config, "acquisition_start_date", ""),
                                 "acquisition_end_date": getattr(config, "acquisition_end_date", "")}),
        },
    }


def update_paths(config: Any) -> tuple[Path, Path]:
    mapped = config_mapping(config)
    processing = mapped.get("processing", {})
    logs = Path(str(processing.get("logs", "") or "00_logs"))
    return logs / "update_campaigns.csv", logs / "update_expected.csv"


def update_runs_path(config: Any) -> Path:
    mapped = config_mapping(config)
    processing = mapped.get("processing", {})
    logs = Path(str(processing.get("logs", "") or "00_logs"))
    return logs / "update_runs.csv"


def campaign_fields(config: Any) -> dict[str, str]:
    mapped = config_mapping(config)
    download = mapped.get("download", {})
    if not isinstance(download, Mapping):
        download = {}
    return {
        "collection_short_name": str(download.get("collection_short_name", "") or "").strip(),
        "product_version_filter": str(download.get("product_version_filter", "best") or "best").strip(),
        "start_date": str(download.get("start_date", "") or "").strip()[:10],
        "end_date": str(download.get("end_date", "") or "").strip()[:10],
        **acquisition_scope(mapped),
    }


def update_campaign_id(config: Any) -> str:
    fields = campaign_fields(config)
    identity = "|".join(
        (
            fields["collection_short_name"],
            fields["product_version_filter"],
            fields["start_date"],
            fields["end_date"],
        )
    )
    if fields.get("acquisition_start_date"):
        identity += "|acquisition_start|" + fields["acquisition_start_date"] + "|" + fields["acquisition_end_date"]
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def campaign_label(fields: Mapping[str, str]) -> str:
    start_date = str(fields.get("start_date", "") or "?")
    end_date = str(fields.get("end_date", "") or "?")
    collection = str(fields.get("collection_short_name", "") or "SWOT")
    product_filter = str(fields.get("product_version_filter", "") or "best")
    return f"{start_date} to {end_date} | {collection} | {product_filter}"


def normalized_tiles(values: Iterable[Any]) -> list[str]:
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


def campaign_rows(config: Any) -> list[dict[str, str]]:
    campaigns_path, _expected_path = update_paths(config)
    return read_project_rows(campaigns_path, "update_campaigns")


def expected_rows(config: Any) -> list[dict[str, str]]:
    _campaigns_path, expected_path = update_paths(config)
    return read_project_rows(expected_path, "update_expected")


def run_rows(config: Any) -> list[dict[str, str]]:
    return read_project_rows(update_runs_path(config), "update_runs")


def campaign_is_archived(row: Mapping[str, Any]) -> bool:
    """Return whether a window is hidden without deleting its provenance."""
    return str(row.get("archived", "")).lower() in {"yes", "true", "1"}


def set_campaign_archived(config: Any, campaign_id: str, archived: bool, reason: str = "") -> None:
    """Archive/restore a window; leave expected products and stage history intact."""
    path, _ = update_paths(config)
    with automation_lock(database_path_for(path).parent):
        _set_campaign_archived(config, campaign_id, archived, reason)


def _set_campaign_archived(config: Any, campaign_id: str, archived: bool, reason: str) -> None:
    row = next((r for r in campaign_rows(config) if r.get("campaign_id") == campaign_id), None)
    if row is None:
        raise ValueError("Update window no longer exists. Refresh statistics.")
    row.update(archived="yes" if archived else "no", archived_at=now_iso() if archived else "",
               archive_reason=reason if archived else "")
    path, _ = update_paths(config)
    runs_root = path.parent / "automation_runs"
    markers = []
    if runs_root.exists():
        for run_path in runs_root.glob("*/automation_run.json"):
            with run_path.open(encoding="utf-8") as handle:
                saved = json.load(handle)
            auto = saved.get("config", {})
            mapped = dict(auto.get("base_config", {}))
            mapped["download"] = {**mapped.get("download", {}), "start_date": auto.get("start_date", ""), "end_date": auto.get("end_date", "")}
            if update_campaign_id(mapped) != campaign_id:
                continue
            markers.append(run_path.parent / "archived.json")
    upsert_project_rows(path, [row], dataset="update_campaigns", export_csv=True)
    for marker in markers:
        if archived:
            marker.write_text(json.dumps({"campaign_id": campaign_id, "archived_at": now_iso(), "reason": reason}, indent=2), encoding="utf-8")
        elif marker.exists():
            marker.unlink()


def _date_text(value: Any) -> str:
    return str(value or "").strip()[:10]


def _latest_end_date(campaigns: Iterable[Mapping[str, Any]], selected_tiles: set[str]) -> str:
    end_dates: list[str] = []
    for campaign in campaigns:
        try:
            tiles = normalized_tiles(json.loads(str(campaign.get("tiles", "") or "[]")))
        except (json.JSONDecodeError, TypeError):
            tiles = []
        if selected_tiles and not (set(tiles) & selected_tiles):
            continue
        end_date = _date_text(campaign.get("end_date", ""))
        if end_date:
            end_dates.append(end_date)
    return max(end_dates, default="")


def classify_update_run(
    *,
    selected_tiles: Sequence[str],
    existing_campaigns: Sequence[Mapping[str, Any]],
    end_date: str,
) -> tuple[str, list[str], list[str], str]:
    """Classify one requested update run against prior project campaigns."""
    selected = set(normalized_tiles(selected_tiles))
    previous_tiles: set[str] = set()
    for campaign in existing_campaigns:
        try:
            previous_tiles.update(normalized_tiles(json.loads(str(campaign.get("tiles", "") or "[]"))))
        except (json.JSONDecodeError, TypeError):
            continue
    new_tiles = sorted(selected - previous_tiles)
    existing_tiles = sorted(selected & previous_tiles)
    previous_end = _latest_end_date(existing_campaigns, selected)
    date_extended = bool(previous_end and _date_text(end_date) > previous_end)
    if new_tiles and date_extended:
        run_type = "mixed_update"
    elif new_tiles:
        run_type = "new_tile_coverage"
    elif date_extended:
        run_type = "date_extension"
    else:
        run_type = "overlap_refresh"
    return run_type, new_tiles, existing_tiles, previous_end


def update_run_id(source: str, timestamp: str, campaign_id: str) -> str:
    safe_time = timestamp.replace(":", "").replace("-", "").replace("+", "_")
    nonce = datetime.now().strftime("%f")
    digest = hashlib.sha256(f"{source}|{timestamp}|{campaign_id}|{nonce}".encode("utf-8")).hexdigest()[:8]
    return f"{safe_time}_{digest}"


def record_update_expected_rows(
    config: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    campaign_tiles: Iterable[Any] = (),
) -> str:
    """Persist selected expected granules under a stable date-window campaign."""
    fields = campaign_fields(config)
    if not fields["start_date"] or not fields["end_date"]:
        return ""
    campaign_id = update_campaign_id(config)
    campaigns_path, expected_path = update_paths(config)
    existing_campaigns = {
        str(row.get("campaign_id", "") or ""): row
        for row in read_project_rows(campaigns_path, "update_campaigns")
    }
    prior_campaign_rows = [row for row in existing_campaigns.values() if not campaign_is_archived(row)]
    existing = existing_campaigns.get(campaign_id, {})
    existing_tiles: list[str] = []
    try:
        parsed_tiles = json.loads(str(existing.get("tiles", "") or "[]"))
        if isinstance(parsed_tiles, list):
            existing_tiles = [str(value) for value in parsed_tiles]
    except json.JSONDecodeError:
        existing_tiles = []

    selected_rows: list[dict[str, Any]] = []
    row_tiles: list[str] = []
    for row in rows:
        selected = str(row.get("selected_for_download", "yes") or "yes").strip().lower()
        status = str(row.get("status", "") or "").strip().upper()
        duplicate_status = str(row.get("duplicate_filter_status", "") or "").strip().lower()
        if selected == "no" or status == "EXCLUDED_OLDER_VERSION" or duplicate_status == "excluded_older_version":
            continue
        file_name = str(row.get("file_name", "") or "").strip()
        granule_id = str(row.get("granule_id", "") or "").strip()
        tile = str(row.get("utm_tile", "") or "").strip().upper()
        if not file_name:
            continue
        record_identity = granule_id or file_name
        selected_rows.append(
            {
                "record_id": f"{campaign_id}|{record_identity}",
                "campaign_id": campaign_id,
                "status": "EXPECTED",
                "file_name": file_name,
                "granule_id": granule_id,
                "utm_tile": tile,
                "start_time": str(row.get("start_time", "") or ""),
                "end_time": str(row.get("end_time", "") or ""),
                "size_mb": str(row.get("size_mb", "") or ""),
                "downloaded": str(row.get("downloaded", "") or ""),
                "source": source,
                "updated_at": now_iso(),
            }
        )
        if tile:
            row_tiles.append(tile)

    tiles = normalized_tiles([*existing_tiles, *campaign_tiles, *row_tiles])
    timestamp = now_iso()
    if selected_rows:
        upsert_project_rows(
            expected_path,
            selected_rows,
            dataset="update_expected",
            export_csv=False,
        )
    expected_count = ProjectDatabase(
        database_path_for(expected_path)
    ).dataset_prefix_count("update_expected", f"{campaign_id}|")
    campaign_row = {
        **existing,
        "campaign_id": campaign_id,
        "status": "ACTIVE",
        "archived": "no",
        "archived_at": "",
        "archive_reason": "",
        "label": campaign_label(fields),
        **fields,
        "tiles": json.dumps(tiles),
        "expected_granules": str(expected_count),
        "source": source,
        "created_at": str(existing.get("created_at", "") or timestamp),
        "updated_at": timestamp,
    }
    upsert_project_rows(
        campaigns_path,
        [campaign_row],
        dataset="update_campaigns",
        export_csv=True,
    )
    run_type, new_tiles, existing_tiles, previous_end = classify_update_run(
        selected_tiles=tiles,
        existing_campaigns=prior_campaign_rows,
        end_date=fields["end_date"],
    )
    upsert_project_rows(
        update_runs_path(config),
        [
            {
                "run_id": update_run_id(source, timestamp, campaign_id),
                "campaign_id": campaign_id,
                "status": "RECORDED",
                "source": source,
                "run_type": run_type,
                **fields,
                "tiles": json.dumps(tiles),
                "tile_count": str(len(tiles)),
                "new_tiles": json.dumps(new_tiles),
                "new_tile_count": str(len(new_tiles)),
                "existing_tiles": json.dumps(existing_tiles),
                "existing_tile_count": str(len(existing_tiles)),
                "tiles_changed": "yes" if new_tiles else "no",
                "date_extended": "yes" if run_type in {"date_extension", "mixed_update"} else "no",
                "previous_end_date": previous_end,
                "expected_granules": str(expected_count),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        ],
        dataset="update_runs",
        export_csv=True,
    )
    return campaign_id


def preview_rows(preview: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for granule in getattr(preview, "granules", []) or []:
        rows.append(
            {
                "status": ("MATCHED" if getattr(granule, "selected_for_download", True) else
                           "EXCLUDED_ACQUISITION_WINDOW" if getattr(granule, "duplicate_filter_status", "") == "excluded_acquisition_window"
                           else "EXCLUDED_OLDER_VERSION"),
                "file_name": str(getattr(granule, "file_name", "") or ""),
                "utm_tile": str(getattr(granule, "utm_tile", "") or ""),
                "start_time": str(getattr(granule, "start_time", "") or ""),
                "end_time": str(getattr(granule, "end_time", "") or ""),
                "size_mb": "" if getattr(granule, "size_mb", None) is None else str(granule.size_mb),
                "granule_id": str(getattr(granule, "identity", "") or ""),
                "selected_for_download": "yes" if getattr(granule, "selected_for_download", True) else "no",
                "duplicate_filter_status": str(getattr(granule, "duplicate_filter_status", "") or ""),
            }
        )
    return rows


def record_update_preview(
    config: Any,
    preview: Any,
    *,
    source: str,
    campaign_tiles: Iterable[Any] = (),
) -> str:
    return record_update_expected_rows(
        config,
        preview_rows(preview),
        source=source,
        campaign_tiles=campaign_tiles,
    )


def synchronize_yearly_expectations(
    parent_config: Any, child_config: Any, *,
    preview_rows: Sequence[Mapping[str, Any]] | None = None, download_complete: bool = False,
) -> dict[str, Any]:
    """Replace one parent tile/window with authoritative current NASA selections.

    A complete exact-scope catalogue snapshot is preferred. Legacy callers must
    supply the fresh child preview and evidence that its download stage completed.
    Replaced rows and replacements are retained atomically as audit history; other
    tiles/windows and immutable NASA snapshots remain unchanged. This is called
    by the running pipeline, never by opening Statistics.
    """
    from swotflow.catalog.history import CatalogHistory, canonical_name, history_path
    from swotflow.core.metadata import parse_swot_l2_hr_raster_metadata
    from swotflow.core.temporal import within_acquisition_window

    def mapping(value: Any) -> dict[str, Any]:
        if hasattr(value, "base_config"):
            result = dict(value.base_config)
            result["download"] = {**result.get("download", {}), "start_date": value.start_date,
                                  "end_date": value.end_date, "utm_tiles": list(value.utm_tiles)}
            return result
        return dict(config_mapping(value))

    parent, child = mapping(parent_config), mapping(child_config)
    parent_fields, child_fields = campaign_fields(parent), campaign_fields(child)
    start, end = child_fields["start_date"], child_fields["end_date"]
    tiles = normalized_tiles(child.get("download", {}).get("utm_tiles", []))
    parent_tiles = normalized_tiles(parent.get("download", {}).get("utm_tiles", []))
    if (len(tiles) != 1 or tiles[0] not in parent_tiles or not start or not end
            or not parent_fields["start_date"] <= start <= end <= parent_fields["end_date"]
            or any(parent_fields[key] != child_fields[key]
                   for key in ("collection_short_name", "product_version_filter"))):
        raise ValueError("Annual expectation refresh requires one tile/window inside the parent request.")
    tile = tiles[0]
    ownership = acquisition_scope(child)
    if ownership != {"acquisition_start_date": start, "acquisition_end_date": end}:
        raise ValueError("Annual expectation refresh requires exact acquisition-start ownership.")
    campaigns_path, expected_path = update_paths(parent)
    child_campaigns_path, _ = update_paths(child)
    if database_path_for(expected_path).resolve() != database_path_for(child_campaigns_path).resolve():
        raise ValueError("Parent and annual child must share the project history database.")
    manifest = Path(str(child.get("download", {}).get("manifest_csv", "") or
                        child_campaigns_path.parent / "download_manifest.csv"))
    ledger = CatalogHistory(history_path(manifest))
    matching = []
    for row in ledger.scans():
        scope = json.loads(row["scope"])
        if (scope.get("start") == start and scope.get("end") == end
                and scope.get("tiles") == tiles
                and scope.get("filter") == child_fields["product_version_filter"]
                and all(scope.get(key, "") == value for key, value in ownership.items())
                and child_fields["collection_short_name"] in scope.get("collections", [])):
            matching.append(row)
    # Do not silently fall back to an older successful snapshot after a failed refresh.
    latest = matching[0] if matching else None
    scan_id = ""
    if latest is not None and latest["status"] == "complete":
        snapshot = ledger.snapshot(latest["id"])
        scan_id = str(snapshot["id"])
        candidates = {canonical_name(row["filename"]): row for row in snapshot["candidates"]}
        source_rows = []
        for selected in snapshot["selections"]:
            if selected["selected"]:
                name = canonical_name(selected["filename"])
                if name not in candidates:
                    raise ValueError("Selected NASA product has no retained catalogue metadata.")
                candidate = candidates[name]
                source_rows.append({"file_name": name, "granule_id": candidate.get("concept_id", ""),
                                    "selected_for_download": "yes"})
        origin = "catalogue_snapshot"
    elif preview_rows is not None and download_complete:
        source_rows = list(preview_rows)
        origin = "completed_download_preview"
    else:
        raise ValueError("No complete current annual NASA selection or successful fresh download preview.")

    parent_id, child_id = update_campaign_id(parent), update_campaign_id(child)
    timestamp = now_iso()
    selected_rows: dict[str, dict[str, str]] = {}
    for row in source_rows:
        if (str(row.get("selected_for_download", "yes")).lower() == "no"
                or str(row.get("status", "")).upper().startswith("EXCLUDED_")
                or str(row.get("duplicate_filter_status", "")).lower().startswith("excluded_")):
            continue
        name = canonical_name(str(row.get("file_name", "")))
        parsed = parse_swot_l2_hr_raster_metadata(name)
        if (parsed is None or parsed.fields["coordinate_system"] != tile
                or not within_acquisition_window(name, start, end)):
            raise ValueError("Fresh annual selection contains an unknown or out-of-scope filename.")
        fields = parsed.fields
        beginning = datetime.strptime(fields["range_beginning"], "%Y%m%dT%H%M%S").isoformat() + "Z"
        ending = datetime.strptime(fields["range_ending"], "%Y%m%dT%H%M%S").isoformat() + "Z"
        selected_rows[name] = {
            "record_id": f"{parent_id}|{name}", "campaign_id": parent_id, "status": "EXPECTED",
            "file_name": name, "granule_id": str(row.get("granule_id", "")), "utm_tile": tile,
            "start_time": beginning, "end_time": ending, "size_mb": str(row.get("size_mb", "")),
            "downloaded": str(row.get("downloaded", "")), "source": "yearly_scope_refresh",
            "origin_scan_id": scan_id, "origin_campaign_id": child_id, "origin_kind": origin,
            "updated_at": timestamp,
        }
    database = ProjectDatabase(database_path_for(expected_path))
    database.ensure_imported("update_campaigns", campaigns_path)
    database.ensure_imported("update_expected", expected_path)
    with database.session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stored = connection.execute(
            "SELECT record_key,payload_json FROM records WHERE dataset='update_expected' "
            "AND utm_tile IN (?, '') AND record_key LIKE ?",
            (tile, f"{parent_id}|%")).fetchall()
        replaced = []
        for row in stored:
            payload = json.loads(row["payload_json"])
            name = payload.get("file_name", "")
            parsed = parse_swot_l2_hr_raster_metadata(name)
            if (parsed is not None and parsed.fields["coordinate_system"] == tile
                    and within_acquisition_window(name, start, end)):
                replaced.append((row["record_key"], payload))
        history_payload = {"parent_campaign_id": parent_id, "child_campaign_id": child_id,
                           "tile": tile, "start_date": start, "end_date": end,
                           "origin_scan_id": scan_id, "origin_kind": origin,
                           "previous_rows": [row for _, row in replaced],
                           "selected_rows": list(selected_rows.values()), "status": "APPLIED"}
        history_id = hashlib.sha256(json.dumps(history_payload, sort_keys=True).encode()).hexdigest()

        def write_record(dataset: str, key: str, value: Mapping[str, Any]) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO records(dataset,record_key,status,utm_tile,event_date,source_path,"
                "output_path,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (dataset, key, str(value.get("status", "")), str(value.get("utm_tile", value.get("tile", ""))),
                 str(value.get("start_time", value.get("start_date", "")))[:10], "", "", timestamp,
                 json.dumps(value, sort_keys=True)))

        for key, _ in replaced:
            connection.execute("DELETE FROM records WHERE dataset='update_expected' AND record_key=?", (key,))
        for row in selected_rows.values():
            write_record("update_expected", row["record_id"], row)
        write_record("yearly_expectation_history", history_id, {**history_payload, "updated_at": timestamp})
        old_campaign = connection.execute(
            "SELECT payload_json FROM records WHERE dataset='update_campaigns' AND record_key=?", (parent_id,)).fetchone()
        campaign = json.loads(old_campaign[0]) if old_campaign else {}
        expected_count = connection.execute(
            "SELECT COUNT(*) FROM records WHERE dataset='update_expected' AND record_key LIKE ?", (f"{parent_id}|%",)).fetchone()[0]
        campaign.update(campaign_id=parent_id, status="ACTIVE", **parent_fields,
                        label=campaign_label(parent_fields), tiles=json.dumps(parent_tiles),
                        expected_granules=str(expected_count), source="yearly_scope_refresh",
                        created_at=campaign.get("created_at", timestamp), updated_at=timestamp)
        write_record("update_campaigns", parent_id, campaign)
        database.mark_dataset_initialized("update_expected", connection=connection)
        database.mark_dataset_initialized("update_campaigns", connection=connection)
        database._bump_revision(connection)
        connection.commit()
    database.export_csv("update_campaigns", campaigns_path)
    return {"campaign_id": parent_id, "tile": tile, "start_date": start, "end_date": end,
            "selected_count": len(selected_rows), "replaced_count": len(replaced),
            "expected_count": expected_count, "origin_scan_id": scan_id,
            "origin_kind": origin, "history_id": history_id}
