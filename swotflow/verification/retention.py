"""Explicit mosaic retention policy; provenance is separate from pixel verification."""
from pathlib import Path
from typing import Any, Mapping

FULL_PIXELS = "full_pixels"
CONFIRMED_INGESTION = "confirmed_ingestion"
# Version only the cleanup eligibility rule; never invalidate scientific checkpoints.
INGESTION_CLEANUP_RULE_VERSION = 1
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


def _cleanup_history_signature(logs: Path, collection: str, report_path: str) -> tuple | None:
    """Identify evidence changes without SQLite's mutable reader coordination files.

    The main database and nonempty WAL contain the durable data. SHM contains
    no database content and changes on ordinary reads. Empty WAL creation or
    removal also cannot change evidence. Track file identity and true change
    metadata, including Windows ChangeTime; unsupported metadata disables reuse.
    """
    from swotflow.consolidation.register import default_path
    from swotflow.project.database import database_path_for
    from swotflow.verification.file_cache import content_signature

    paths = {p for p in (logs.iterdir() if logs.exists() else []) if p.is_file() and
             (p.suffix in {".sqlite3", ".db", ".csv", ".json"} or p.name.endswith("-wal"))}
    paths.add(logs.parent / "project.yaml")
    databases = {Path(default_path(collection)), logs.parent / "swotflow.sqlite3"}
    if report_path:
        paths.add(Path(report_path))
        databases.add(database_path_for(Path(report_path)))
    for database in databases:
        paths.update([database, Path(str(database) + "-wal")])
    signatures = []
    for path in sorted(paths):
        try:
            signature = content_signature(path)
        except FileNotFoundError:
            continue
        except OSError:
            return None  # Read evidence freshly; never reuse an uncertain cache key.
        if signature is None:
            return None
        if path.name.endswith("-wal") and signature[2] == 0:
            continue
        signatures.append((str(path), signature))
    return tuple(signatures)


def current_cleanup_evidence(logs: Path, collection: str, policy: str,
                             report_path: str = "", cache: dict | None = None) -> tuple[dict, dict]:
    """Reuse stable evidence within one deletion batch, invalidating real changes.

    No remote raster requests are made. Database/WAL changes, file replacement,
    upload history, inventory freshness and provenance still invalidate reuse.
    If evidence changes while being assembled, retry once, then retain files.
    """
    if not collection:
        return {}, {}
    from swotflow.verification.pipeline import VerificationStore, active_proofs, load_inventory
    from swotflow.catalog.history import recent
    from swotflow.project.database import read_project_rows
    from swotflow.core.metadata import REPLACED_INVALID_ASSET_ID_STATUS

    store = VerificationStore(logs)
    for _attempt in range(2):
        signature = _cleanup_history_signature(logs, collection, report_path)
        key = (str(logs), collection, policy, report_path, signature)
        if (signature is not None and cache is not None and cache.get("key") == key
                and recent(cache.get("inventory_time", ""))):
            return cache["active"], cache["uploads"]
        active = active_proofs(store, collection, include_ingestion=policy == CONFIRMED_INGESTION)
        uploads: dict[str, dict] = {}
        if report_path:
            for row in read_project_rows(Path(report_path), "upload_report"):
                # Keep archived rejected names as history; unresolved owners still block deletion.
                if row.get("final_status") == REPLACED_INVALID_ASSET_ID_STATUS:
                    continue
                if row.get("local_file"):
                    path = str(Path(row["local_file"]).resolve())
                    uploads[path] = {} if path in uploads else row
        inventory_time = load_inventory(store, collection).get("finished_utc", "")
        after = _cleanup_history_signature(logs, collection, report_path)
        if cache is not None:
            cache.clear()
        if signature is not None and after is not None and signature != after:
            continue  # Do not authorize deletion from a changing evidence snapshot.
        if signature is not None and signature == after and cache is not None:
            cache.update(key=key, active=active, uploads=uploads, inventory_time=inventory_time)
        return active, uploads
    return {}, {}  # Concurrent changes persisted; keep targets for a later pass.


def ingestion_cleanup_matches(proof: dict, active: dict, uploads: dict) -> bool:
    """Require this exact local revision and its unambiguous terminal upload owner.

    Both cleanup policies use this binding; active evidence selects whether the
    additional full-pixel certificate is required.
    """
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
