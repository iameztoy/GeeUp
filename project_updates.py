"""Persistent date-window update campaigns for SWOTFlow projects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from project_database import ProjectDatabase, database_path_for, read_project_rows, upsert_project_rows


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
        },
    }


def update_paths(config: Any) -> tuple[Path, Path]:
    mapped = config_mapping(config)
    processing = mapped.get("processing", {})
    logs = Path(str(processing.get("logs", "") or "00_logs"))
    return logs / "update_campaigns.csv", logs / "update_expected.csv"


def campaign_fields(config: Any) -> dict[str, str]:
    download = config_mapping(config).get("download", {})
    if not isinstance(download, Mapping):
        download = {}
    return {
        "collection_short_name": str(download.get("collection_short_name", "") or "").strip(),
        "product_version_filter": str(download.get("product_version_filter", "best") or "best").strip(),
        "start_date": str(download.get("start_date", "") or "").strip()[:10],
        "end_date": str(download.get("end_date", "") or "").strip()[:10],
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
        "campaign_id": campaign_id,
        "status": "ACTIVE",
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
    return campaign_id


def preview_rows(preview: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for granule in getattr(preview, "granules", []) or []:
        rows.append(
            {
                "status": "MATCHED" if getattr(granule, "selected_for_download", True) else "EXCLUDED_OLDER_VERSION",
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
