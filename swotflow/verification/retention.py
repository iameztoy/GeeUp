"""Explicit mosaic retention policy; provenance is separate from pixel verification."""
from pathlib import Path
from typing import Any, Mapping

FULL_PIXELS = "full_pixels"
CONFIRMED_INGESTION = "confirmed_ingestion"
POLICY_LABELS = {
    "Full pixel verification (strict)": FULL_PIXELS,
    "Confirmed EE ingestion + matching provenance": CONFIRMED_INGESTION,
}
TERMINAL_UPLOAD_STATUSES = {"EE_VERIFIED_EXISTS", "COMPLETED", "SKIPPED_ALREADY_EXISTS"}


def mosaic_cleanup_policy(config: Mapping[str, Any], *, default: str = FULL_PIXELS) -> str:
    """Default to the existing strict rule; reject unknown deletion policies."""
    policy = str(config.get("cleanup", {}).get("mosaic_policy", default))
    if policy not in POLICY_LABELS.values():
        raise ValueError("Unknown mosaic cleanup policy: " + policy)
    return policy


def mosaic_cleanup_enabled(config: Mapping[str, Any], *, legacy_enabled: bool = True) -> bool:
    """Honor explicit mosaic opt-in; old non-GUI configurations retain their rule."""
    enabled = config.get("cleanup", {}).get("mosaics_enabled", legacy_enabled)
    if not isinstance(enabled, bool):
        raise ValueError("cleanup.mosaics_enabled must be true or false")
    return enabled


def source_proof_matches(proof: dict, active: dict) -> bool:
    """Require every source; ingestion evidence also binds this exact local revision."""
    if not proof.get("sources"):
        return False
    for name, checksum in proof["sources"].items():
        evidence = active.get(name, {})
        if evidence.get("checksum") != checksum:
            return False
        if evidence.get("evidence") == CONFIRMED_INGESTION and (
            evidence.get("local_sha256") != proof["sha256"]
            or evidence.get("auxiliary_hashes", {}) != proof.get("auxiliary_hashes", {})
        ):
            return False
    return True


def current_cleanup_evidence(logs: Path, collection: str, policy: str,
                             report_path: str = "", cache: dict | None = None) -> tuple[dict, dict]:
    """Reuse metadata within one deletion batch, invalidating on history changes.

    No remote raster requests are made. Checks remain O(files + memberships)
    for an unchanged snapshot rather than rereading all history per raster.
    """
    if not collection:
        return {}, {}
    from swotflow.verification.pipeline import VerificationStore, active_proofs, load_inventory
    from swotflow.catalog.history import recent
    from swotflow.consolidation.register import default_path
    from swotflow.project.database import read_project_rows, database_path_for
    paths = {p for p in (logs.iterdir() if logs.exists() else []) if p.is_file() and
             (p.suffix in {".sqlite3", ".db", ".csv", ".json"} or p.name.endswith(("-wal", "-shm")))}
    paths.add(logs.parent / "project.yaml")
    register = Path(default_path(collection))
    databases = {register, logs.parent / "swotflow.sqlite3"}
    if report_path:
        paths.add(Path(report_path))
        databases.add(database_path_for(Path(report_path)))
    for database in databases:
        paths.update([database, Path(str(database) + "-wal"), Path(str(database) + "-shm")])
    signatures = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(paths) if p.is_file())
    key = (str(logs), collection, policy, report_path, signatures)
    if cache is not None and cache.get("key") == key and recent(cache.get("inventory_time", "")):
        return cache["active"], cache["uploads"]
    store = VerificationStore(logs)
    active = active_proofs(store, collection, include_ingestion=policy == CONFIRMED_INGESTION)
    uploads: dict[str, dict] = {}
    if report_path:
        for row in read_project_rows(Path(report_path), "upload_report"):
            if row.get("local_file"):
                path = str(Path(row["local_file"]).resolve())
                uploads[path] = {} if path in uploads else row  # Ambiguous destinations retain the file.
    if cache is not None:
        cache.update(key=key, active=active, uploads=uploads,
                     inventory_time=load_inventory(store, collection).get("finished_utc", ""))
    return active, uploads


def ingestion_cleanup_matches(proof: dict, active: dict, uploads: dict) -> bool:
    """Retain pending/failed rows even when a same-named asset is present."""
    if not source_proof_matches(proof, active):
        return False
    if any(active[n].get("local_sha256") != proof["sha256"] or
           active[n].get("auxiliary_hashes", {}) != proof.get("auxiliary_hashes", {})
           for n in proof["sources"]):
        return False
    row = uploads.get(str(Path(proof["path"]).resolve()), {})
    owners = {active[n].get("asset_id") for n in proof["sources"]}
    return (str(row.get("final_status", "")).upper() in TERMINAL_UPLOAD_STATUSES
            and owners == {row.get("asset_id")} and bool(row.get("asset_id")))
