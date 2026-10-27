"""Resume an interrupted Automation run from its exact local stage proofs."""
from pathlib import Path
from typing import Any

from swotflow.verification.pipeline import VerificationStore, artifact_current, settings_key


def verified_resume_stages(config: dict[str, Any], saved: dict[str, Any],
                           completed: set[str], base_dir: Path) -> tuple[set[str], str]:
    """Reuse a completed prefix, never a filename/status without current evidence.

    Resume freezes the successful download selection. A new preflight/download is
    still needed for a new update. Missing or changed evidence falls back to the
    ordinary verified workflow, which checks retained raw bytes before transfer.
    """
    from copy import deepcopy
    from swotflow.processing.extract import parse_config as extract_config, build_output_path
    from swotflow.processing.mosaic import parse_config as mosaic_config
    from swotflow.core.temporal import acquisition_scope

    required = {"download", "duplicates", "extract"}
    scan_id = saved.get("mosaic", {}).get("selection_scan_id", "")
    if not required <= completed or not scan_id:
        return set(), ""
    keys = ("start_date", "end_date", "utm_tiles", "product_version_filter", "collection_short_name",
            "collection_version", "collection_version_label", "limit_granules")
    if (any(config.get("download", {}).get(k) != saved.get("download", {}).get(k) for k in keys)
            or acquisition_scope(config) != acquisition_scope(saved)):
        return set(), ""
    bound = deepcopy(config)
    bound.setdefault("mosaic", {})["selection_scan_id"] = scan_id
    ex = extract_config(bound, base_dir)
    mo = mosaic_config(bound, base_dir)
    expected = mo.expected_sources
    if expected is None or any(not checksum for checksum in expected.values()):
        return set(), ""
    store = VerificationStore(mo.manifest_csv.parent)
    artifacts = store.artifacts()
    extracted = []
    for name, checksum in expected.items():
        path = build_output_path(Path(name), ex.output_folder, ex.target_crs_mode).resolve()
        proof = artifacts.get(str(path))
        if (not proof or proof["stage"] != "extract" or proof["sources"] != {name: checksum}
                or proof["settings"] != settings_key("extract", ex)):
            return set(), ""
        extracted.append(proof)
    if all(artifact_current(proof) for proof in extracted):
        return required | ({"raw_cleanup"} & completed), scan_id
    # After extracted cleanup, only a completed mosaic stage with exact input
    # lineage and current outputs can justify reusing the whole local prefix.
    if "mosaic" not in completed:
        return set(), ""
    covered: set[str] = set()
    for proof in artifacts.values():
        if (proof["stage"] != "mosaic" or Path(proof["path"]).parent != mo.output_folder.resolve()
                or proof["settings"] != settings_key("mosaic", mo)
                or not proof["sources"] or not set(proof["sources"]) <= expected.keys()
                or any(expected[n] != h for n, h in proof["sources"].items())):
            continue
        # Link the retained extraction receipts even if their rasters were cleaned.
        if any(not artifacts.get(p) or artifacts[p]["sha256"] != h
               for p, h in proof["inputs"].items()):
            continue
        if artifact_current(proof):
            covered.update(proof["sources"])
    if covered == set(expected):
        return required | ({"raw_cleanup", "mosaic", "extracted_cleanup"} & completed), scan_id
    return set(), ""
