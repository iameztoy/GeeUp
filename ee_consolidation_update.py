"""Incremental consolidation planning; no cloud writes or production state changes.

Saved plans retain original source records after raster retirement. A prior
source_index is remapped into the union's canonical order, making additive
updates equivalent to one-shot selection under unchanged selection rules.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import ee_consolidation as c
from swot_metadata import product_identity_parts

SUPPORTED = {"swot-ee-consolidation-v1", c.ALGORITHM}
CONTRACT_FIELDS = ("mode", "separate_hemispheres", "separate_versions", "overlap", "max_quality")


def contract(options: c.Options) -> tuple:
    """Dates select groups; all pixel/grouping rules must remain unchanged."""
    return tuple(getattr(options, field) for field in CONTRACT_FIELDS)


def read_plans(path: Path) -> list[dict]:
    """Read one saved plan or a dedicated folder of saved consolidation plans."""
    paths = [path] if path.is_file() else sorted(path.rglob("plan.json"))
    plans = []
    for file in paths:
        data = json.loads(file.read_text(encoding="utf-8"))
        if data.get("algorithm") in SUPPORTED:
            plans.append(data)
    if not plans:
        raise ValueError("No supported consolidation plan.json files found; select the saved plan or its folder")
    return plans


def revision_group(sources: list[dict], inputs: list[dict], predecessors: list[str], options: c.Options) -> dict:
    """Fingerprint both flattened source history and the actual revision inputs."""
    group = c.make_group(sources, options)
    group["contributors"] = sorted(copy.deepcopy(inputs), key=lambda s: s["asset"]["id"])
    group["predecessors"] = sorted(predecessors)
    fingerprint = c.digest({"base": group["fingerprint"], "contributors": group["contributors"],
                            "predecessors": group["predecessors"]})
    group["id"] = group["id"].rsplit("_", 1)[0] + "_" + fingerprint[:12]
    group["fingerprint"] = fingerprint
    group["savings"] = len(inputs) - 1
    group["action"] = "extend"
    return group


def check_saved_group(group: dict, plan: dict) -> None:
    """Reject damaged history instead of guessing source-index meanings."""
    if plan.get("algorithm") not in SUPPORTED:
        raise ValueError("Unsupported consolidation algorithm")
    options = c.Options(**plan["options"])
    options.validate()
    sources = group["sources"]
    if len(sources) < 2 or len({s["asset"]["id"] for s in sources}) != len(sources):
        raise ValueError("Saved plan has missing or duplicate original source records")
    for source in sources:
        name = source["asset"]["id"].rsplit("/", 1)[-1]
        rebuilt = c.source_record(source["asset"], {name: source["manifest"]})
        if rebuilt != source:
            raise ValueError("Saved source metadata or lineage is inconsistent")
    if "contributors" in group:
        if plan["algorithm"] != c.ALGORITHM:
            raise ValueError("Unsupported incremental plan version")
        expected = revision_group(sources, group["contributors"], group["predecessors"], options)
        represented = []
        for source in group["contributors"]:
            if source["fingerprint"] != c.digest(source["asset"]):
                raise ValueError("Saved contributor fingerprint does not match its metadata")
            represented.extend(source.get("source_ids", [source["asset"]["id"]]))
        if sorted(represented) != sorted(s["asset"]["id"] for s in sources):
            raise ValueError("Saved revision does not account for every original source exactly once")
    else:
        expected = c.make_group(sources, options, algorithm=plan["algorithm"])
    if any(group.get(k) != value for k, value in expected.items()):
        raise ValueError("Saved group fingerprint, source order or metadata has changed")


def prior_record(asset: dict, group: dict, plan: dict) -> dict:
    """Bind a saved source-index mapping to an actual consolidated asset."""
    check_saved_group(group, plan)
    asset = c.snapshot_asset(asset)
    p, options = asset.get("properties", {}), c.Options(**plan["options"])
    expected = {"consolidation_fingerprint": group["fingerprint"], "consolidation_algorithm": plan["algorithm"],
                "consolidation_rule": options.overlap, "consolidation_max_quality": options.max_quality,
                "consolidation_tie_break": "earliest_start_then_asset_id",
                "consolidation_sources": [s["asset"]["id"] for s in group["sources"]],
                "swot_acquisition_date_utc": group["date"], "swot_cycle_id": group["cycle"], "swot_pass_id": group["pass"]}
    if asset.get("type") != "IMAGE" or asset["id"].rsplit("/", 1)[-1] != group["id"]:
        raise ValueError("Existing mosaic does not match the saved group ID")
    if any(p.get(k) != value for k, value in expected.items()):
        raise ValueError("Existing mosaic properties do not match the saved plan")
    if p.get("consolidation_predecessors", []) != group.get("predecessors", []):
        raise ValueError("Existing mosaic revision history differs from the saved plan")
    for key in ("start", "end"):
        value = asset.get(key + "Time")
        if not value or c.datetime.fromisoformat(value.replace("Z", "+00:00")) != c.datetime.fromisoformat(group[key]):
            raise ValueError("Existing mosaic acquisition interval differs from the saved plan")
    bands = {b["id"]: b for b in asset.get("bands", [])}
    if set(bands) != {"b1", "b2", "source_index"}:
        raise ValueError("Existing mosaic needs paired b1/b2 and the original source_index band")
    grid = bands["b1"]["grid"]
    if any(b["grid"] != grid or b.get("pyramidingPolicy", "").upper() != "SAMPLE" for b in bands.values()):
        raise ValueError("Existing mosaic requires identical grids and SAMPLE pyramids for all bands")
    t = grid["affineTransform"]
    if (t["scaleX"], t["scaleY"], t.get("shearX", 0), t.get("shearY", 0)) != (100, -100, 0, 0):
        raise ValueError("Existing mosaic is not an unrotated 100 m grid")
    if (grid.get("crsCode") != group["crs"] and grid.get("crsWkt") != group["crs_definition"]):
        raise ValueError("Existing mosaic CRS differs from the plan")
    for field, index in (("translateX", 2), ("translateY", 5)):
        delta = (t[field] - group["transform"][index]) / 100
        if abs(delta - round(delta)) > 1e-7:
            raise ValueError("Existing mosaic pixel alignment differs from the plan")
    if group["bounds"]:
        b = group["bounds"]
        actual = [t["translateX"], t["translateY"] - 100 * grid["dimensions"]["height"],
                  t["translateX"] + 100 * grid["dimensions"]["width"], t["translateY"]]
        if actual != b:
            raise ValueError("Existing native mosaic extent differs from the saved plan")
    return {"asset": asset, "fingerprint": c.digest(asset), "grid": grid,
            "kind": "consolidated", "source_ids": expected["consolidation_sources"]}


def original_conflicts(sources: list[dict]) -> list[str]:
    """Detect duplicates/revisions embedded in different stored mosaic sources."""
    seen, scenes, conflicts = {}, {}, set()
    for source in sources:
        if not source["lineage_complete"]:
            conflicts.add("Original granule lineage is incomplete; rebuild with complete source history")
            continue
        for original in source["original_inputs"]:
            key = product_identity_parts(original["filename"])
            scene = tuple(original.get(k, "") for k in ("descriptor", "cycle_id", "pass_id", "scene_id", "filename_suffix"))
            if scene in scenes and scenes[scene] != key:
                conflicts.add("Same original scene has revised acquisition timestamps; review and rebuild affected sources")
            scenes[scene] = key
            previous = seen.get(key)
            if key is None:
                conflicts.add("Unrecognized original granule identity")
            elif previous and previous[0] != source["asset"]["id"]:
                conflicts.add("Original acquisition occurs in different source assets (duplicate or processing-version replacement); rebuild affected sources")
            elif previous and previous[1] != original["filename"]:
                conflicts.add("Multiple processing versions of an observation inside one source; review its original processing")
            seen[key] = (source["asset"]["id"], original["filename"])
    return sorted(conflicts)


def plan_updates(assets: list[dict], collection: str, existing: list[dict], existing_collection: str,
                 plans: list[dict], options: c.Options, lineage: dict | None = None) -> dict:
    """Plan additive updates in O(n log n); reject ambiguous revisions and replacements.

Only immediate raster contributors are retirement candidates. Retained originals
already represented by a previous mosaic are skipped, not counted as new savings.
"""
    options.validate()
    c.validate_destination(collection, existing_collection)
    result = c.plan_assets(assets, collection, options, lineage)
    saved = {}
    for plan in plans:
        if plan.get("algorithm") not in SUPPORTED:
            continue
        for group in plan["groups"]:
            old = saved.get(group["id"])
            if old and (old[0] != group or old[1]["options"] != plan["options"]):
                raise ValueError("Conflicting saved histories for " + group["id"])
            saved[group["id"]] = (group, plan)
    records, ignored, outside, seen_existing = {}, [], [], set()
    for asset in existing:
        aid, name = asset["id"], asset["id"].rsplit("/", 1)[-1]
        if aid.rsplit("/", 1)[0] != existing_collection or aid in seen_existing:
            raise ValueError("Duplicate or out-of-collection existing asset")
        seen_existing.add(aid)
        if not name.startswith("SWOTC_"):
            ignored.append(aid)
            continue
        day = asset.get("properties", {}).get("swot_acquisition_date_utc", "")
        if day and ((options.start and day < options.start) or (options.end and day > options.end)):
            outside.append(aid)
            continue
        if name not in saved:
            raise ValueError("Missing saved plan for existing mosaic " + name)
        group, plan = saved[name]
        records[aid] = (group, plan, prior_record(asset, group, plan))
    superseded = {parent for group, _, _ in records.values() for parent in group.get("predecessors", [])}
    active = {aid: record for aid, record in records.items() if aid not in superseded}
    if records and not active:
        raise ValueError("Cyclic or invalid predecessor history")
    previous = {}
    for aid, (group, plan, record) in active.items():
        if contract(c.Options(**plan["options"])) != contract(options):
            raise ValueError("Existing mosaic settings differ; use its saved grouping/quality rules or rebuild: " + aid)
        key = c.grouping_key(group["sources"][0], options)
        if key in previous:
            raise ValueError("Several active mosaic revisions share a group; select an unambiguous existing collection")
        previous[key] = (group, record)
    candidates, excluded, outside_new = defaultdict(list), [], []
    known_keys = {s["asset"]["id"]: key for key, (group, _) in previous.items() for s in group["sources"]}
    for asset in assets:
        try:
            source = c.source_record(asset, lineage or {})
        except (ValueError, KeyError, TypeError) as exc:
            excluded.append({"asset_id": asset["id"], "reason": str(exc)})
            continue
        if (options.start and source["date"] < options.start) or (options.end and source["date"] > options.end):
            outside_new.append(asset["id"])
            continue
        key = c.grouping_key(source, options)
        if source["asset"]["id"] in known_keys and known_keys[source["asset"]["id"]] != key:
            raise ValueError("Previously represented source changed grouping/grid; rebuild: " + source["asset"]["id"])
        candidates[key].append(source)
    groups, blocked, unchanged, represented, singletons = [], [], [], [], []
    for key in sorted(set(candidates) | set(previous)):
        incoming = candidates.get(key, [])
        if key not in previous:
            if len(incoming) > 1:
                problems = original_conflicts(incoming)
                if problems:
                    blocked.append({"group": list(key), "reasons": problems, "source_ids": [s["asset"]["id"] for s in incoming]})
                else:
                    group = c.make_group(incoming, options)
                    group["action"] = "create"
                    groups.append(group)
            else:
                singletons.extend(s["asset"]["id"] for s in incoming)
            continue
        old, record = previous[key]
        known = {s["asset"]["id"]: s for s in old["sources"]}
        new, problems = [], []
        for source in incoming:
            aid = source["asset"]["id"]
            if aid in known:
                if source["fingerprint"] != known[aid]["fingerprint"]:
                    problems.append("Previously represented source changed in place; rebuild: " + aid)
                else:
                    represented.append(aid)
            else:
                new.append(source)
        if new:
            problems.extend(original_conflicts(old["sources"] + new))
        if problems:
            blocked.append({"group": list(key), "existing_asset": record["asset"]["id"], "reasons": sorted(set(problems)),
                            "source_ids": [s["asset"]["id"] for s in new]})
        elif not new:
            unchanged.append(record["asset"]["id"])
        else:
            groups.append(revision_group(old["sources"] + new, [record] + new, [record["asset"]["id"]], options))
    retirement = {s["asset"]["id"] for group in groups for s in c.contributors(group)}
    expected = sum(len(c.contributors(g)) for g in groups)
    if len(retirement) != expected:
        raise ValueError("A contributor belongs to multiple planned outputs; retirement counts would be ambiguous")
    total, savings = len(assets) + len(existing), len(retirement) - len(groups)
    result.update(operation="update_existing", existing_collection=existing_collection, groups=groups,
                  excluded=excluded, singletons=singletons, outside_dates=outside_new + outside,
                  unchanged=unchanged, already_represented=represented, blocked=blocked,
                  superseded_revisions=sorted(superseded & set(records)), ignored_existing=ignored,
                  retirement_candidates=sorted(retirement))
    result["summary"] = {"inventory_assets": total, "mergeable_inputs": len(retirement),
                         "new_mosaics": len(groups), "extended_groups": sum(g.get("action") == "extend" for g in groups),
                         "created_groups": sum(g.get("action") == "create" for g in groups),
                         "singletons": len(singletons), "excluded": len(excluded), "outside_dates": len(outside_new) + len(outside),
                         "potential_reduction": savings, "reduction_percent": round(100 * savings / total, 2) if total else 0,
                         "eventual_assets": total - savings, "temporary_assets_with_originals": total + len(groups),
                         "unchanged_groups": len(unchanged), "already_represented": len(represented), "blocked_groups": len(blocked),
                         "mixed_groups": sum(g["mixed_processing"] for g in groups),
                         "unknown_lineage_groups": sum(g["unknown_lineage"] > 0 for g in groups)}
    result["notes"] += ["Incremental updates preserve original source-index ties; superseded/duplicate observations require a rebuild.",
                        "Old revisions remain stored; downstream analysis must choose the intended revision explicitly.",
                        "Savings count only immediate contributors; already-represented raw assets and older revisions remain retained."]
    return result
