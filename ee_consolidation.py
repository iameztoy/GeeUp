"""Plan and export paired SWOT mosaics; never replace or delete source assets.

Python 3.11+. Planning is offline, O(n log n), and needs no EE/GDAL runtime.
Only submit_groups writes to EE, and only to a separate destination collection.
"""
from __future__ import annotations

import csv
import copy
import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from swot_metadata import parse_swot_l2_hr_raster_metadata

ALGORITHM = "swot-ee-consolidation-v2"
MAX_BATCH = 10
MAX_PIXELS = 10_000_000_000
# Explicit EPSG:6933 definition (GDAL/PROJ registry). The EE service does not
# resolve this EPSG code in every API path; preserve the definition in the plan.
COMMON_CRS_WKT = ('PROJCS["WGS 84 / NSIDC EASE-Grid 2.0 Global",'
                  'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
                  'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
                  'PROJECTION["Cylindrical_Equal_Area"],PARAMETER["standard_parallel_1",30],'
                  'PARAMETER["central_meridian",0],PARAMETER["false_easting",0],'
                  'PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","6933"]]')
MODES = {
    "native": "Native UTM zone + hemisphere (default)",
    "tile": "Keep UTM latitude-band tiles separate",
    "common": "Common 100 m grid (EPSG:6933, experimental)",
}
RULES = {"lowest_quality": "Lowest quality code", "first_valid": "First valid source (paired comparison)"}


def utc_now() -> str:
    """Return an unambiguous UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def ee_crs(code: str) -> str:
    """Resolve the common-grid definition explicitly for EE projection requests."""
    return COMMON_CRS_WKT if code == "EPSG:6933" else code


def digest(value: Any) -> str:
    """Hash a canonical JSON value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Atomically replace a local consolidation record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class Options:
    mode: str = "native"
    separate_hemispheres: bool = True
    separate_versions: bool = False
    overlap: str = "lowest_quality"
    start: str = ""
    end: str = ""
    max_quality: int = 3

    def validate(self) -> None:
        """Reject ambiguous modes, quality filters, and date windows."""
        if self.mode not in MODES or self.overlap not in RULES:
            raise ValueError("Unknown consolidation option")
        if self.max_quality not in (0, 1, 2, 3):
            raise ValueError("Quality limit must be 0, 1, 2 or 3")
        for value in (self.start, self.end):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError("Use YYYY-MM-DD dates")
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date must not follow end date")


def collection_id(value: str) -> str:
    """Accept cloud-project collection paths only, with no path traversal."""
    value = value.strip().rstrip("/")
    if not re.fullmatch(r"projects/[A-Za-z0-9_-]+/assets/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value):
        raise ValueError("Use a full collection ID: projects/PROJECT/assets/COLLECTION")
    return value


def validate_destination(source: str, destination: str) -> str:
    """Forbid writing into the source collection or its ancestors/children."""
    source, destination = collection_id(source), collection_id(destination)
    if source == destination or source.startswith(destination + "/") or destination.startswith(source + "/"):
        raise ValueError("Choose a separate destination collection, outside the source collection")
    return destination


def snapshot_asset(asset: dict) -> dict:
    """Retain the metadata needed to detect source replacement and reproduce grids."""
    return copy.deepcopy({k: asset[k] for k in ("id", "type", "updateTime", "startTime", "endTime", "properties", "bands", "sizeBytes") if k in asset})


def read_lineage(path: Path | None) -> dict[str, dict]:
    """Read a mosaic manifest or an exported lineage JSON without updating it."""
    if path is None:
        return {}
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return {g["asset_id"].rsplit("/", 1)[-1]: g.get("manifest", {}) for g in data["groups"]}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = PureWindowsPath(row.get("output_file", "")).stem
            if name:
                rows[name] = row
    return rows


def source_record(asset: dict, lineage: dict[str, dict]) -> dict:
    """Validate native grids and metadata, preserving original input records."""
    asset = snapshot_asset(asset)
    parsed = parse_swot_l2_hr_raster_metadata(asset["id"].rsplit("/", 1)[-1])
    if parsed is None or asset.get("type") != "IMAGE":
        raise ValueError("Not a recognized original SWOT image")
    bands = {b["id"]: b for b in asset.get("bands", [])}
    if set(bands) != {"b1", "b2"}:
        raise ValueError("Expected original elevation b1 and categorical quality b2")
    grid = bands["b1"]["grid"]
    if grid != bands["b2"]["grid"]:
        raise ValueError("Elevation and quality grids differ")
    crs = grid.get("crsCode", "")
    if not re.fullmatch(r"EPSG:32[67](0[1-9]|[1-5][0-9]|60)", crs):
        raise ValueError("Expected a native UTM CRS")
    t = grid["affineTransform"]
    if (t["scaleX"], t["scaleY"], t.get("shearX", 0), t.get("shearY", 0)) != (100, -100, 0, 0):
        raise ValueError("Expected an unrotated native 100 m grid")
    if not all(math.isfinite(t[k]) for k in ("translateX", "translateY")):
        raise ValueError("Invalid grid origin")
    if min(grid["dimensions"].values()) <= 0:
        raise ValueError("Empty grid")
    start = datetime.strptime(parsed.fields["range_beginning"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    end = datetime.strptime(parsed.fields["range_ending"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    if end < start:
        raise ValueError("Acquisition ends before it starts")
    for key, expected in (("startTime", start), ("endTime", end)):
        if asset.get(key) and datetime.fromisoformat(asset[key].replace("Z", "+00:00")) != expected:
            raise ValueError(f"Filename and {key} disagree")
    for key in ("cycle_id", "pass_id"):
        recorded = asset.get("properties", {}).get("swot_" + key)
        if recorded is not None and int(recorded) != int(parsed.fields[key]):
            raise ValueError(f"Filename and {key} disagree")
    row = lineage.get(asset["id"].rsplit("/", 1)[-1], {})
    originals, versions = [], set()
    inputs = json.loads(row.get("input_files") or "[]")
    if not isinstance(inputs, list) or any(not isinstance(file, str) for file in inputs):
        raise ValueError("Malformed original-input lineage list")
    complete = bool(inputs)
    if row.get("input_count"):
        complete = complete and int(row["input_count"]) == len(inputs)
    for file in inputs:
        name = PureWindowsPath(file).name
        original = parse_swot_l2_hr_raster_metadata(name)
        item = {"recorded_file": file, "filename": name}
        if original:
            item.update(original.fields)
            versions.add(original.fields["crid"] + "_" + original.fields["product_counter"])
            # Do not move any part of a previously cross-midnight mosaic silently.
            complete = complete and original.fields["range_beginning"][:8] == start.strftime("%Y%m%d")
        else:
            complete = False
        originals.append(item)
    if any(o.get("range_beginning", "")[:8] not in ("", start.strftime("%Y%m%d")) for o in originals):
        raise ValueError("Recorded original inputs start on different UTC dates; requires a source-level split")
    if not versions:
        versions.add(parsed.fields["crid"] + "_" + parsed.fields["product_counter"])
    return {"asset": asset, "fingerprint": digest(asset), "grid": grid,
            "cycle": int(parsed.fields["cycle_id"]), "pass": int(parsed.fields["pass_id"]),
            "date": start.date().isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "tile": parsed.fields["coordinate_system"], "hemisphere": "N" if crs.startswith("EPSG:326") else "S",
            "versions": sorted(versions), "lineage_complete": complete,
            "original_inputs": originals, "manifest": row}


def grouping_key(source: dict, options: Options) -> tuple:
    """Separate cycles, passes and UTC start dates, then grid/version partitions."""
    grid = source["grid"]
    t = grid["affineTransform"]
    phase = tuple(round(t[k] % 100, 6) % 100 for k in ("translateX", "translateY"))
    spatial = (grid["crsCode"], *phase)
    if options.mode == "tile":
        spatial += (source["tile"],)
    elif options.mode == "common":
        spatial = ("EPSG:6933", source["hemisphere"] if options.separate_hemispheres else "both")
    versions = tuple(source["versions"]) if options.separate_versions else ()
    # Unknown lineage is not evidence of processing compatibility.
    if options.separate_versions and (not source["lineage_complete"] or len(versions) != 1 or "MIXD" in str(versions)):
        versions += (source["asset"]["id"],)
    return (source["date"], source["cycle"], source["pass"], *spatial, versions)


def native_extent(sources: list[dict]) -> tuple[list[float], list[float]]:
    """Return the exact union bounding rectangle and aligned native transform."""
    boxes = []
    for source in sources:
        g = source["grid"]
        t, d = g["affineTransform"], g["dimensions"]
        boxes.append((t["translateX"], t["translateY"] - 100 * d["height"],
                      t["translateX"] + 100 * d["width"], t["translateY"]))
    box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    return box, [100, 0, box[0], 0, -100, box[3]]


def make_group(sources: list[dict], options: Options, algorithm: str = ALGORITHM) -> dict:
    """Build a canonical group, preserving original source order across revisions."""
    sources = sorted(sources, key=lambda s: (s["start"], s["asset"]["id"]))
    key = grouping_key(sources[0], options)
    if any(grouping_key(s, options) != key for s in sources):
        raise ValueError("Sources do not share a date/pass/grid and processing partition")
    if options.mode == "common":
        crs, transform, bounds = "EPSG:6933", [100, 0, 0, 0, -100, 0], None
    else:
        crs = sources[0]["grid"]["crsCode"]
        bounds, transform = native_extent(sources)
    versions = sorted({v for s in sources for v in s["versions"]})
    fingerprint = digest({"algorithm": algorithm, "options": asdict(options),
                          "sources": sources, "crs": crs, "crs_definition": ee_crs(crs), "transform": transform})
    return {"id": f"SWOTC_{key[0].replace('-', '')}_C{key[1]:03}_P{key[2]:03}_{crs.replace(':', '')}_{fingerprint[:12]}",
            "date": key[0], "cycle": key[1], "pass": key[2], "crs": crs,
            "transform": transform, "bounds": bounds, "crs_definition": ee_crs(crs), "fingerprint": fingerprint,
            "start": min(s["start"] for s in sources), "end": max(s["end"] for s in sources),
            "versions": versions, "mixed_processing": len(versions) > 1 or any("MIXD" in v for v in versions),
            "unknown_lineage": sum(not s["lineage_complete"] for s in sources),
            "sources": sources, "savings": len(sources) - 1}


def contributors(group: dict) -> list[dict]:
    """Return only assets actually read, including a previous consolidated raster."""
    return group.get("contributors", group["sources"])


def validate_source_identities(sources: list[dict]) -> None:
    """Refuse ambiguous duplicate observations before any consolidation export."""
    from swot_metadata import product_identity_key
    seen = set()
    for source in sources:
        for original in source["original_inputs"]:
            identity = product_identity_key(original["filename"])
            if identity and identity in seen:
                raise ValueError("Duplicate acquisition/processing versions require an explicit replacement: " + original["filename"])
            if identity:
                seen.add(identity)


def plan_assets(assets: list[dict], collection: str, options: Options,
                lineage: dict[str, dict] | None = None) -> dict:
    """Build a reproducible plan; excluded/unselected/singleton assets stay in place."""
    options.validate()
    collection = collection_id(collection)
    grouped: dict[tuple, list] = defaultdict(list)
    seen, excluded, outside = set(), [], []
    for asset in assets:
        asset_id = asset.get("id", "")
        if asset_id in seen:
            raise ValueError(f"Duplicate asset in inventory: {asset_id}")
        seen.add(asset_id)
        if asset_id.rsplit("/", 1)[0] != collection:
            raise ValueError("Inventory includes assets outside the selected collection")
        try:
            source = source_record(asset, lineage or {})
            if ((options.start and source["date"] < options.start) or
                    (options.end and source["date"] > options.end)):
                outside.append(asset_id)
                continue
            grouped[grouping_key(source, options)].append(source)
        except (ValueError, KeyError, TypeError) as exc:
            excluded.append({"asset_id": asset_id, "reason": str(exc)})
    validate_source_identities([source for sources in grouped.values() for source in sources])
    groups, singletons = [], []
    for key, sources in sorted(grouped.items()):
        sources.sort(key=lambda s: (s["start"], s["asset"]["id"]))
        if len(sources) == 1:
            singletons.append(sources[0]["asset"]["id"])
            continue
        groups.append(make_group(sources, options))
    savings = sum(g["savings"] for g in groups)
    summary = {"inventory_assets": len(assets), "mergeable_inputs": sum(len(g["sources"]) for g in groups),
               "new_mosaics": len(groups), "singletons": len(singletons), "excluded": len(excluded),
               "outside_dates": len(outside), "potential_reduction": savings,
               "reduction_percent": round(100 * savings / len(assets), 2) if assets else 0,
               "eventual_assets": len(assets) - savings,
               "temporary_assets_with_originals": len(assets) + len(groups),
               "mixed_groups": sum(g["mixed_processing"] for g in groups),
               "unknown_lineage_groups": sum(g["unknown_lineage"] > 0 for g in groups)}
    return {"algorithm": ALGORITHM, "created_utc": utc_now(), "collection": collection,
            "options": asdict(options), "summary": summary, "groups": groups,
            "export_settings": {"pyramidingPolicy": {".default": "sample"}, "maxPixels": MAX_PIXELS,
                                "priority": 50, "overwrite": False, "resampling": "nearest", "resolution_m": 100},
            "singletons": singletons, "excluded": excluded, "outside_dates": outside,
            "notes": ["Savings require separate verification and retirement of originals; this tool never deletes them.",
                      "Counts do not estimate bytes or valid coverage, or repair shifts, missing granules, or inherited band mispairing.",
                      "UTC start dates remain separate; a source ending after midnight retains its true end time.",
                      "Quality ties: earliest source start, then lexicographic asset ID. No elevation averaging.",
                      "b1, b2 and source_index use SAMPLE pyramids; analyse/filter at the explicit 100 m output grid.",
                      "source_index is one-based within the ordered source list; original granule identity is not inferred per pixel."]}


def option_comparison(assets: list[dict], collection: str, options: Options, lineage: dict) -> list[dict]:
    """Compare grouping choices under the same date, quality and version settings."""
    options.validate()
    collection = collection_id(collection)
    buckets = {mode: defaultdict(int) for mode in MODES}
    seen = set()
    for asset in assets:
        asset_id = asset.get("id", "")
        if asset_id in seen or asset_id.rsplit("/", 1)[0] != collection:
            raise ValueError("Duplicate or out-of-collection inventory asset")
        seen.add(asset_id)
        try:
            source = source_record(asset, lineage)
        except (ValueError, KeyError, TypeError):
            continue  # Detailed exclusions are carried by the selected plan.
        if ((options.start and source["date"] < options.start) or (options.end and source["date"] > options.end)):
            continue
        for mode in MODES:
            buckets[mode][grouping_key(source, replace(options, mode=mode))] += 1
    result = []
    for mode, counts in buckets.items():
        savings = sum(n - 1 for n in counts.values())
        result.append({"mode": mode, "potential_reduction": savings,
                       "new_mosaics": sum(n > 1 for n in counts.values()),
                       "eventual_assets": len(assets) - savings,
                       "reduction_percent": round(100 * savings / len(assets), 2) if assets else 0})
    return result


def connect(project: str) -> Any:
    """Use the existing EE API login; never request/save Earthdata credentials."""
    import ee
    ee.Initialize(project=project)
    ee.data.setDeadline(120000)
    return ee


def fetch_inventory(ee: Any, collection: str, progress: Callable[[str], None] = lambda s: None) -> dict:
    """Read paginated FULL metadata; a moving inventory is rechecked before exports."""
    collection = collection_id(collection)
    if ee.data.getAsset(collection)["type"] != "IMAGE_COLLECTION":
        raise ValueError("Source must be an Earth Engine image collection")
    assets, tokens = [], set()
    query = {"parent": collection, "pageSize": 1000, "view": "FULL"}
    started = utc_now()
    while True:
        page = ee.data.listAssets(query)
        assets.extend(page.get("assets", []))
        progress(f"Read metadata for {len(assets):,} assets...")
        token = page.get("nextPageToken")
        if not token:
            break
        if token in tokens:
            raise ValueError("EE inventory repeated a page token; refresh the inventory")
        tokens.add(token)
        query["pageToken"] = token
    quota, quota_error = None, ""
    try:
        quota = ee.data.getAssetRootQuota("/".join(collection.split("/")[:3]) + "/")
    except Exception as exc:  # API may not expose cloud-project quota to this user.
        quota_error = str(exc)
    return {"collection": collection, "assets": [snapshot_asset(a) for a in assets],
            "started_utc": started, "finished_utc": utc_now(), "quota": quota, "quota_error": quota_error}


def build_image(ee: Any, group: dict, options: Options) -> Any:
    """Select all output bands from one source with an integer, tie-free score."""
    options.validate()
    images, count = [], len(group["sources"])
    indices = {s["asset"]["id"]: i for i, s in enumerate(group["sources"], 1)}
    for source in contributors(group):
        grid = source["grid"]
        t = grid["affineTransform"]
        native = [100, 0, t["translateX"], 0, -100, t["translateY"]]
        # Pin native sampling before any common-grid resampling: existing b2
        # MEAN overviews must not be used as categorical observations.
        names = ["b1", "b2", "source_index"] if source.get("kind") == "consolidated" else ["b1", "b2"]
        image = ee.Image(source["asset"]["id"]).select(names).reproject(ee_crs(grid.get("crsCode") or grid["crsWkt"]), native)
        wse, quality = image.select("b1"), image.select("b2")
        valid = (wse.mask().And(quality.mask()).And(wse.neq(-9999)).And(wse.abs().lt(1e30))
                 .And(quality.gte(0)).And(quality.lte(options.max_quality)).And(quality.eq(quality.round())))
        image = image.unmask(-9999).updateMask(valid)
        # Default resampling is nearest neighbour for both bands and their mask.
        if options.mode == "common":
            image = image.reproject(ee_crs(group["crs"]), group["transform"])
        quality = image.select("b2")
        if source.get("kind") == "consolidated":
            # Remap the previous pixel's actual winner, never the old mosaic's
            # earliest overall time. This preserves one-shot ties across batches.
            source_index = image.select("source_index").remap(
                list(range(1, len(source["source_ids"]) + 1)),
                [indices[s] for s in source["source_ids"]]).rename("source_index")
            image = image.updateMask(source_index.mask())
        else:
            source_index = quality.multiply(0).add(indices[source["asset"]["id"]]).rename("source_index")
        tie = source_index.multiply(-1).add(count)
        score = quality.multiply(-1).add(3).multiply(count + 1).add(tie) if options.overlap == "lowest_quality" else tie
        images.append(image.select(["b1", "b2"]).toFloat().addBands(source_index.toFloat()).addBands(score.toInt64().rename("selection_score")))
    return (ee.ImageCollection.fromImages(images).qualityMosaic("selection_score")
            .select(["b1", "b2", "source_index"]).toFloat()
            .setDefaultProjection(ee_crs(group["crs"]), group["transform"]))


def export_parameters(ee: Any, group: dict, plan: dict, destination: str) -> dict:
    """Construct explicit export parameters without submitting any job."""
    options = Options(**plan["options"])
    image = build_image(ee, group, options)
    sources = [s["asset"]["id"] for s in group["sources"]]
    properties = {"system:time_start": int(datetime.fromisoformat(group["start"]).timestamp() * 1000),
                  "system:time_end": int(datetime.fromisoformat(group["end"]).timestamp() * 1000),
                  "swot_cycle_id": group["cycle"], "swot_pass_id": group["pass"],
                  "swot_acquisition_date_utc": group["date"], "consolidation_algorithm": ALGORITHM,
                  "consolidation_fingerprint": group["fingerprint"], "consolidation_sources": sources,
                  "consolidation_source_count": len(sources), "consolidation_processing_versions": group["versions"],
                  "consolidation_mixed_processing": group["mixed_processing"],
                  "consolidation_unknown_lineage": group["unknown_lineage"],
                  "consolidation_rule": options.overlap, "consolidation_tie_break": "earliest_start_then_asset_id",
                  "consolidation_max_quality": options.max_quality,
                  "consolidation_status": "UNVERIFIED_REQUIRES_COMPARISON"}
    if group.get("predecessors"):
        properties["consolidation_predecessors"] = group["predecessors"]
        properties["consolidation_operation"] = "incremental_update"
    if group["bounds"] is not None:
        region = ee.Geometry.Rectangle(group["bounds"], group["crs"], False)
    else:
        region = ee.ImageCollection.fromImages([ee.Image(s["asset"]["id"]) for s in contributors(group)]).geometry().bounds(100, ee.Projection(ee_crs(group["crs"])))
    return {"image": image.set(properties), "description": group["id"],
            "assetId": validate_destination(plan["collection"], destination) + "/" + group["id"],
            "region": region, "crs": ee_crs(group["crs"]), "crsTransform": group["transform"],
            "pyramidingPolicy": {".default": "sample"}, "maxPixels": MAX_PIXELS, "overwrite": False,
            "priority": 50}


def save_plan(folder: Path, plan: dict) -> Path:
    """Save an immutable plan and a compact group table outside processing logs."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "plan.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != plan:
            raise FileExistsError("This folder already contains a different plan; choose a new folder")
        return path
    write_json(path, plan)
    with (folder / "groups.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        columns = ["id", "date", "cycle", "pass", "crs", "inputs", "savings", "start", "end", "versions", "unknown_lineage"]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for group in plan["groups"]:
            writer.writerow({k: len(group["sources"]) if k == "inputs" else group[k] for k in columns})
    return path


def submit_groups(ee: Any, plan: dict, group_ids: list[str], destination: str, folder: Path,
                  progress: Callable[[str], None] = lambda s: None) -> dict:
    """Recheck sources, then submit at most ten exports; journal ambiguous outcomes.

    Never retry a submitted/uncertain task automatically. Saved task IDs allow
    status recovery after app closure. Only a confirmed 404 permits creation.
    """
    destination = validate_destination(plan["collection"], destination)
    if not 1 <= len(group_ids) <= MAX_BATCH or len(set(group_ids)) != len(group_ids):
        raise ValueError(f"Select between 1 and {MAX_BATCH} distinct groups per submission")
    groups = {g["id"]: g for g in plan["groups"]}
    selected = [groups[g] for g in group_ids]
    save_plan(folder, plan)
    receipt_path = folder / "tasks.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {
        "destination": destination, "algorithm": ALGORITHM, "ee_version": ee.__version__, "tasks": {}}
    if receipt["destination"] != destination:
        raise ValueError("This plan folder already has a different export destination")
    if any(g in receipt["tasks"] for g in group_ids):
        raise ValueError("A selected group already has a task record. Refresh status before any manual retry.")
    # List the parent to distinguish a missing destination from permissions/network failures.
    parent, leaf = destination.rsplit("/", 1)
    children, token = [], None
    while True:
        query = {"parent": parent, "pageSize": 1000, "view": "BASIC"}
        if token:
            query["pageToken"] = token
        page = ee.data.listAssets(query)
        children.extend(page.get("assets", []))
        token = page.get("nextPageToken")
        if not token:
            break
    existing = next((a for a in children if a["id"] == destination), None)
    if existing and existing["type"] != "IMAGE_COLLECTION":
        raise ValueError("Destination exists and is not an image collection")
    # Verify every selected source before creating anything. Membership changes
    # elsewhere do not invalidate a bounded export from explicit source IDs.
    for group in selected:
        for source in contributors(group):
            current = snapshot_asset(ee.data.getAsset(source["asset"]["id"]))
            if digest(current) != source["fingerprint"]:
                raise ValueError("A selected source changed since preview. Refresh the inventory and plan.")
    if plan.get("operation") == "update_existing":
        previous = {aid for group in selected for aid in group.get("predecessors", [])}
        fresh = fetch_inventory(ee, plan["existing_collection"])
        if any(previous.intersection(a.get("properties", {}).get("consolidation_predecessors", [])) for a in fresh["assets"]):
            raise ValueError("A newer revision of a selected mosaic exists. Refresh both inventories and preview.")
    if existing:
        check = fetch_inventory(ee, destination)
        if set(g["id"] for g in selected) & {a["id"].rsplit("/", 1)[-1] for a in check["assets"]}:
            raise ValueError("An output already exists; it will not be overwritten")
    quota = ee.data.getAssetRootQuota("/".join(destination.split("/")[:3]) + "/")
    counts = quota["asset_count"]
    needed = len(selected) + int(existing is None)
    if not counts.get("limit") or counts["limit"] - counts["usage"] < needed:
        raise ValueError("Destination asset-count quota is unavailable or insufficient; check project quota")
    # Respect other work in this EE project; do not flood an existing task queue.
    tasks = ee.data.getTaskList()
    active = sum(t.get("state") in ("READY", "RUNNING") for t in tasks)
    if active + len(selected) > 100:
        raise ValueError("At least 100 active/queued EE tasks; wait before adding consolidation exports")
    for task in tasks:
        if task.get("state") in ("READY", "RUNNING") and task.get("description") in group_ids:
            uris = task.get("destination_uris", [])
            if not uris or any(destination in uri for uri in uris):
                raise ValueError("An export for this group is already active; do not submit it again")
    prepared = [(group, ee.batch.Export.image.toAsset(**export_parameters(ee, group, plan, destination))) for group in selected]
    if not existing:
        ee.data.createAsset({"type": "IMAGE_COLLECTION"}, destination)
    for group, task in prepared:
        # Reserve a request ID before submitting, so uncertain network responses
        # can be reconciled without duplicating jobs.
        request_id = ee.data.newTaskId()[0]
        record = {"request_id": request_id, "task_id": request_id, "description": group["id"], "asset_id": destination + "/" + group["id"],
                  "state": "SUBMITTING", "submitted_utc": utc_now(), "fingerprint": group["fingerprint"]}
        receipt["tasks"][group["id"]] = record
        write_json(receipt_path, receipt)
        try:
            operation = ee.data.exportImage(request_id, task.config)
            record.update(task_id=operation["name"].rsplit("/", 1)[-1], operation_name=operation["name"], state="SUBMITTED")
        except Exception as exc:
            record.update(state="SUBMISSION_UNCERTAIN", error=str(exc))
            write_json(receipt_path, receipt)
            raise RuntimeError("Submission response uncertain. Refresh saved task status; do not resubmit blindly.") from exc
        write_json(receipt_path, receipt)
        progress(f"Submitted {group['id']}")
    return receipt


def refresh_tasks(ee: Any, folder: Path) -> dict:
    """Refresh only this tab's recorded tasks; never cancel or change any task."""
    path = folder / "tasks.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    for record in receipt["tasks"].values():
        if record.get("operation_name"):
            operation = ee.data.getOperation(record["operation_name"])
            metadata = operation.get("metadata", {})
            status = {"state": metadata.get("state", "UNKNOWN"), "operation": operation}
            if operation.get("error"):
                status.update(state="FAILED", error_message=operation["error"].get("message", ""))
        else:
            status = ee.data.getTaskStatus(record["task_id"])[0]
            if status["state"] == "UNKNOWN":
                # A lost submission response does not prove that no task started.
                # Keep this distinction visible and never enable a blind retry.
                status["state"] = "SUBMISSION_UNCERTAIN"
        record.update(state=status["state"], last_checked_utc=utc_now(), status=status)
    write_json(path, receipt)
    return receipt
