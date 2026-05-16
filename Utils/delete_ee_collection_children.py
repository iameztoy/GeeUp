"""Delete child assets from an Earth Engine collection without UI prompts.

Portable usage note:
- dependencies: Python standard library plus earthengine-api;
- install outside GeeUp with: python -m pip install earthengine-api;
- authenticate once with: earthengine authenticate;
- dry-run with: python delete_ee_collection_children.py --asset ASSET_ID;
- delete with: python delete_ee_collection_children.py --asset ASSET_ID --execute --yes;
- check remaining assets with: python delete_ee_collection_children.py --asset ASSET_ID --count-only.

The script is intentionally conservative:
- dry-run is the default;
- only direct IMAGE children are deleted unless --recursive or --all-types is used;
- the parent collection is kept unless --delete-parent is explicitly set.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_ASSET = "projects/YOUR_PROJECT/assets/YOUR_COLLECTION"
DEFAULT_PAGE_SIZE = 1000
CONTAINER_TYPES = {"FOLDER", "IMAGE_COLLECTION"}
PROGRESS_BAR_WIDTH = 30


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    asset_type: str

    @property
    def depth(self) -> int:
        return self.asset_id.count("/")


def infer_project_from_asset(asset_id: str) -> str | None:
    """Infer the Cloud project from a modern Earth Engine asset id."""

    match = re.match(r"^projects/([^/]+)/assets(?:/|$)", asset_id)
    return match.group(1) if match else None


def normalize_asset_record(asset: dict) -> AssetRecord:
    """Normalize listAssets output across Python API versions."""

    asset_id = str(asset.get("id") or asset.get("name") or "")
    asset_type = str(asset.get("type") or "").upper()
    return AssetRecord(asset_id=asset_id, asset_type=asset_type)


def sort_for_safe_deletion(records: Iterable[AssetRecord]) -> list[AssetRecord]:
    """Delete deeper descendants first so recursive cleanup is safe."""

    return sorted(records, key=lambda record: (record.depth, record.asset_id), reverse=True)


def should_delete_record(record: AssetRecord, all_types: bool) -> bool:
    """Return whether a listed child should be deleted by default policy."""

    return all_types or record.asset_type == "IMAGE"


def import_ee():
    try:
        import ee  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "The Earth Engine Python API is not installed. Install it with:\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
            "Then authenticate with:\n"
            "  .\\.venv\\Scripts\\earthengine.exe authenticate"
        ) from exc
    return ee


def initialize_earth_engine(ee, project: str | None, authenticate: bool) -> None:
    if authenticate:
        ee.Authenticate()
    try:
        if project:
            ee.Initialize(project=project)
        else:
            ee.Initialize()
    except Exception as exc:
        raise SystemExit(
            "Could not initialize Earth Engine. Run authentication first, for example:\n"
            "  .\\.venv\\Scripts\\earthengine.exe authenticate\n"
            "If your account needs an explicit project, pass --project YOUR_PROJECT.\n"
            f"Original error: {exc}"
        ) from exc


def list_child_assets(ee, parent: str, page_size: int = DEFAULT_PAGE_SIZE) -> list[AssetRecord]:
    records: list[AssetRecord] = []
    params: dict[str, str] = {"parent": parent, "pageSize": str(page_size)}

    while True:
        response = ee.data.listAssets(params)
        records.extend(normalize_asset_record(asset) for asset in response.get("assets", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        params["pageToken"] = page_token

    return records


def collect_assets(
    ee,
    parent: str,
    recursive: bool,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[AssetRecord]:
    direct_children = list_child_assets(ee, parent, page_size=page_size)
    if not recursive:
        return direct_children

    collected: list[AssetRecord] = []
    stack = list(direct_children)
    while stack:
        record = stack.pop()
        collected.append(record)
        if record.asset_type in CONTAINER_TYPES:
            stack.extend(list_child_assets(ee, record.asset_id, page_size=page_size))
    return collected


def default_report_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("reports") / f"ee_delete_assets_{timestamp}.csv"


def write_report(report_path: Path, rows: list[dict[str, str]]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asset_id", "asset_type", "status", "error"])
        writer.writeheader()
        writer.writerows(rows)


def render_progress_bar(current: int, total: int, errors: int = 0) -> str:
    if total <= 0:
        return "[------------------------------] 0/0 100.0% errors=0"
    ratio = min(max(current / total, 0.0), 1.0)
    filled = int(round(ratio * PROGRESS_BAR_WIDTH))
    bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
    percent = ratio * 100.0
    return f"[{bar}] {current}/{total} {percent:5.1f}% errors={errors}"


def print_progress_bar(current: int, total: int, errors: int = 0) -> None:
    print(f"\r{render_progress_bar(current, total, errors)}", end="", flush=True)
    if current >= total:
        print("", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete child image assets from an Earth Engine collection without Code Editor popups."
    )
    parser.add_argument("--asset", default=DEFAULT_ASSET, help="Parent folder or image collection asset id.")
    parser.add_argument("--project", default=None, help="Cloud project for ee.Initialize. Inferred for projects/... assets.")
    parser.add_argument("--execute", action="store_true", help="Actually delete assets. Default is dry-run.")
    parser.add_argument("--yes", action="store_true", help="Required with --execute to confirm deletion.")
    parser.add_argument("--recursive", action="store_true", help="Also delete descendants of child folders/collections.")
    parser.add_argument("--all-types", action="store_true", help="Delete non-image children too.")
    parser.add_argument("--delete-parent", action="store_true", help="Delete the parent asset after deleting children.")
    parser.add_argument("--count-only", action="store_true", help="Only list and print current child counts.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of delete candidates, useful for testing.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Earth Engine listAssets page size.")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1,
        help="Refresh progress every N delete candidates.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print one line per asset as it is processed.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages.")
    parser.add_argument("--report-csv", type=Path, default=None, help="CSV report path.")
    parser.add_argument("--authenticate", action="store_true", help="Run ee.Authenticate before initialization.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.execute and not args.yes:
        print("Refusing to delete without --yes. Re-run with --execute --yes after checking dry-run output.")
        return 2

    project = args.project or infer_project_from_asset(args.asset)
    ee = import_ee()
    initialize_earth_engine(ee, project=project, authenticate=args.authenticate)

    if not args.quiet:
        print(f"Listing children under: {args.asset}", flush=True)
    records = collect_assets(ee, args.asset, recursive=args.recursive, page_size=args.page_size)
    candidates = [record for record in sort_for_safe_deletion(records) if should_delete_record(record, args.all_types)]
    skipped = [record for record in sort_for_safe_deletion(records) if not should_delete_record(record, args.all_types)]

    if args.limit is not None:
        candidates = candidates[: args.limit]

    if not args.quiet:
        print(f"Listed children: {len(records)}", flush=True)
        print(f"Delete candidates: {len(candidates)}", flush=True)
        print(f"Skipped by type: {len(skipped)}", flush=True)
    if args.count_only:
        return 0
    if not args.quiet:
        if candidates:
            verb = "Deleting" if args.execute else "Dry-run planning"
            print(f"{verb} {len(candidates)} candidate assets...", flush=True)

    rows: list[dict[str, str]] = []
    action = "DELETE" if args.execute else "WOULD_DELETE"
    total_candidates = len(candidates)
    progress_every = max(1, args.progress_every)
    error_count = 0
    if not args.quiet and total_candidates and not args.verbose:
        print_progress_bar(0, total_candidates, error_count)
    for index, record in enumerate(candidates, start=1):
        status = action
        error = ""
        if args.execute:
            try:
                ee.data.deleteAsset(record.asset_id)
                status = "DELETED"
            except Exception as exc:  # noqa: BLE001 - record and continue after per-asset errors.
                status = "ERROR"
                error = str(exc)
                error_count += 1
        rows.append(
            {
                "asset_id": record.asset_id,
                "asset_type": record.asset_type,
                "status": status,
                "error": error,
            }
        )
        if not args.quiet and (
            index == 1 or index == total_candidates or index % progress_every == 0 or status == "ERROR"
        ):
            if args.verbose:
                print(f"{index}/{total_candidates} {status}: {record.asset_id}", flush=True)
            else:
                print_progress_bar(index, total_candidates, error_count)

    for record in skipped:
        rows.append(
            {
                "asset_id": record.asset_id,
                "asset_type": record.asset_type,
                "status": "SKIPPED_TYPE",
                "error": "Use --all-types to delete non-image children.",
            }
        )

    if args.delete_parent:
        status = "WOULD_DELETE_PARENT"
        error = ""
        if args.execute:
            try:
                ee.data.deleteAsset(args.asset)
                status = "DELETED_PARENT"
            except Exception as exc:  # noqa: BLE001
                status = "ERROR_PARENT"
                error = str(exc)
        rows.append({"asset_id": args.asset, "asset_type": "PARENT", "status": status, "error": error})

    report_path = args.report_csv or default_report_path()
    write_report(report_path, rows)

    errors = sum(1 for row in rows if row["status"].startswith("ERROR"))
    print(f"Parent asset: {args.asset}")
    print(f"Mode: {'execute' if args.execute else 'dry-run'}")
    print(f"Listed children: {len(records)}")
    print(f"Delete candidates: {len(candidates)}")
    print(f"Skipped by type: {len(skipped)}")
    print(f"Errors: {errors}")
    print(f"Report: {report_path}")
    if not args.execute:
        print("No assets were deleted. Re-run with --execute --yes after reviewing the report.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(run())
