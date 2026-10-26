"""Durable stage proofs and exact-input accounting, separate from legacy manifests.

No cloud mutation is performed here. A local hash proves file identity, not pixel
correctness; only an explicit native-pixel receipt can certify an EE comparison.
"""
from __future__ import annotations

from swotflow.paths import APP_ROOT

import hashlib
import json
import platform
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ALGORITHM = "pipeline-proof-v1"


def utc_now() -> str:
    """Return a timestamp with an explicit UTC offset."""
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    """Hash deterministic JSON, including pathlib settings."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    """Hash bytes once and reject a file changing during the read."""
    before = path.stat()
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError(f"File changed during verification: {path.name}")
    return h.hexdigest()


def product_name(value: str | Path) -> str:
    """Preserve the exact version while normalizing extracted extensions."""
    from swotflow.catalog.history import canonical_name
    return canonical_name(str(value))


def auxiliary_hashes(path: Path) -> dict[str, str]:
    """Capture sidecars that can change masks/georeferencing without TIFF changes."""
    candidates = [Path(str(path)+s) for s in (".aux.xml", ".msk")]
    candidates += [path.with_suffix(s) for s in (".tfw", ".tifw", ".wld")]
    return {str(p.resolve()): file_hash(p) for p in candidates if p.is_file()}


def settings_key(stage: str, config: Any) -> dict:
    """Only pixel-affecting settings invalidate reuse; worker count is recorded separately."""
    names = ("target_crs_mode", "resampling_alg") if stage == "extract" else (
        "grouping_mode", "target_crs_label", "resampling_alg", "nodata", "require_all_inputs")
    return {"algorithm": ALGORITHM,
            **({"overlap_rule": "paired-lowest-quality-v2"} if stage == "mosaic" else {}),
            **{k: getattr(config, k, None) for k in names}}


class VerificationStore:
    """Append-only receipts with indexed latest-state reads and short transactions."""

    def __init__(self, logs: Path) -> None:
        self.logs = Path(logs)
        self.path = self.logs / "workflow_verification.sqlite3"

    @contextmanager
    def connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open read-only unless an explicit operation creates evidence."""
        if write:
            self.logs.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path if write else self.path.resolve().as_uri() + "?mode=ro",
                             uri=not write, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, utc TEXT, stage TEXT, payload TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS artifacts (seq INTEGER PRIMARY KEY, path TEXT, stage TEXT, utc TEXT, payload TEXT)")
                db.execute("CREATE INDEX IF NOT EXISTS artifact_path ON artifacts(path,seq)")
                db.execute("CREATE TABLE IF NOT EXISTS cloud (seq INTEGER PRIMARY KEY, asset TEXT, utc TEXT, payload TEXT)")
                db.execute("CREATE INDEX IF NOT EXISTS cloud_asset ON cloud(asset,seq)")
                db.execute("CREATE TABLE IF NOT EXISTS ingestions (seq INTEGER PRIMARY KEY, asset TEXT, payload TEXT)")
                db.execute("CREATE INDEX IF NOT EXISTS ingestion_asset ON ingestions(asset,seq)")
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    def begin_run(self, stage: str, config: Any) -> str:
        """Record actual runtime/settings once per stage, never credentials."""
        from swotflow.catalog.history import safe_metadata
        values = asdict(config) if is_dataclass(config) else dict(config)
        # Explicit whitelist: authentication, browser profiles and environment are omitted.
        allowed = {k: values[k] for k in ("workers", "requested_workers", "threads", "batch_size", "target_crs_mode",
                   "resampling_alg", "grouping_mode", "target_crs_label", "utm_tiles", "start_date",
                   "end_date", "verified_downloads", "require_all_inputs", "selection_scan_id") if k in values}
        code = APP_ROOT
        source_names = ("swotflow/verification/pipeline.py", "swotflow/core/metadata.py", "swotflow/processing/extract.py",
                        "swotflow/processing/mosaic.py", "swotflow/processing/download.py", "swotflow/verification/download.py",
                        "swotflow/verification/native.py", "swotflow/verification/source.py")
        runtime = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                   "settings": safe_metadata(allowed), "code_sha256": {n: file_hash(code / n) for n in source_names}}
        if "osgeo.gdal" in sys.modules:
            runtime["gdal"] = sys.modules["osgeo.gdal"].VersionInfo("RELEASE_NAME")
        run_id = uuid.uuid4().hex
        with self.connection(True) as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, utc_now(), stage, json.dumps(runtime)))
        return run_id

    def artifacts(self) -> dict[str, dict]:
        """Read latest file evidence in one query; missing history is unknown."""
        if not self.path.exists():
            return {}
        with self.connection() as db:
            return {r["path"]: mapped_receipt(db, json.loads(r["payload"])) for r in db.execute(
                "SELECT a.path,a.payload FROM artifacts a JOIN (SELECT path,MAX(seq) seq FROM artifacts GROUP BY path) b ON a.seq=b.seq")}

    def artifact(self, path: Path) -> dict | None:
        """Read the last receipt for this exact path."""
        if not self.path.exists():
            return None
        with self.connection() as db:
            r = db.execute("SELECT payload FROM artifacts WHERE path=? ORDER BY seq DESC LIMIT 1", (str(path.resolve()),)).fetchone()
            return mapped_receipt(db, json.loads(r[0])) if r else None

    def record_artifact(self, stage: str, path: Path, inputs: list[Path], config: Any, run_id: str, grid: dict) -> dict:
        """Bind output bytes to exact input bytes, source versions and runtime."""
        sources: dict[str, dict] = {}
        input_hashes = {}
        input_auxiliaries = {}
        for source in inputs:
            if stage == "extract":
                proof = download_proof(self.logs, product_name(source))
                if not proof or not proof["valid"] or not source.is_file():
                    raise ValueError(f"No verified NASA bytes for {source.name}")
                state = proof["state"]
                if source.stat().st_size != state[5] or file_hash(source, state[6]) != state[7]:
                    raise ValueError(f"Source differs from verified NASA bytes: {source.name}")
                sources[product_name(source)] = {"algorithm": state[6], "hash": state[7], "size": state[5]}
                input_hashes[str(source.resolve())] = state[7]
            else:
                proof = self.artifact(source)
                if not proof or proof["stage"] != "extract" or not artifact_current(proof):
                    raise ValueError(f"Missing or changed extraction proof: {source.name}")
                input_hashes[str(source.resolve())] = proof["sha256"]
                input_auxiliaries[str(source.resolve())] = proof.get("auxiliary_hashes", {})
                for name, value in proof["sources"].items():
                    if name in sources and sources[name] != value:
                        raise ValueError("Conflicting source checksum: " + name)
                    sources[name] = value
        proof = {"stage": stage, "path": str(path.resolve()), "sha256": file_hash(path), "size": path.stat().st_size,
                 "inputs": input_hashes, "sources": sources, "settings": settings_key(stage, config),
                 "auxiliary_hashes": auxiliary_hashes(path), "input_auxiliaries": input_auxiliaries,
                 "run_id": run_id, "grid": grid, "utc": utc_now(), "verification": "structural_and_file_identity"}
        if stage == "mosaic":
            proof["selection_scan_id"] = getattr(config, "selection_scan_id", "")
        with self.connection(True) as db:
            db.execute("INSERT INTO artifacts(path,stage,utc,payload) VALUES (?,?,?,?)",
                       (proof["path"], stage, proof["utc"], json.dumps(proof)))
        return proof

    def reusable(self, stage: str, output: Path, inputs: list[Path], config: Any) -> bool:
        """Never skip on a manifest flag or file size alone."""
        proof = self.artifact(output)
        if not proof or proof["stage"] != stage or proof["settings"] != settings_key(stage, config):
            return False
        if set(proof["inputs"]) != {str(p.resolve()) for p in inputs} or not artifact_current(proof):
            return False
        for path in inputs:
            if not path.is_file():
                return False
            if stage == "extract":
                value = proof["sources"].get(product_name(path), {})
                if not value or file_hash(path, value["algorithm"]) != value["hash"]:
                    return False
                current = download_proof(self.logs, product_name(path))
                if not current or not current["valid"] or current["state"][7] != value["hash"]:
                    return False
            else:
                current = self.artifact(path)
                if (not current or not artifact_current(current)
                        or file_hash(path) != proof["inputs"][str(path.resolve())]
                        or auxiliary_hashes(path) != proof.get("input_auxiliaries", {}).get(str(path.resolve()), {})):
                    return False
        return True

    def cloud_receipts(self) -> dict[str, dict]:
        """Return latest comparisons, including failures that invalidate old passes."""
        if not self.path.exists():
            return {}
        with self.connection() as db:
            return {r["asset"]: mapped_receipt(db, json.loads(r["payload"])) for r in db.execute(
                "SELECT a.asset,a.payload FROM cloud a JOIN (SELECT asset,MAX(seq) seq FROM cloud GROUP BY asset) b ON a.seq=b.seq")}

    def record_cloud(self, receipt: dict) -> None:
        """Persist a comparison result; failed/sampled receipts cannot certify full pixels."""
        required = {"asset_id", "asset_fingerprint", "local_sha256", "sources", "mode", "passed", "utc"}
        if not required <= receipt.keys() or receipt["mode"] not in {"sampled", "full"}:
            raise ValueError("Invalid native-pixel receipt")
        with self.connection(True) as db:
            db.execute("INSERT INTO cloud(asset,utc,payload) VALUES (?,?,?)",
                       (receipt["asset_id"], receipt["utc"], json.dumps(receipt)))

    def ingestions(self) -> dict[str, dict]:
        """Read immutable submission/revision events without inventing legacy evidence."""
        if not self.path.exists():
            return {}
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ingestions'").fetchone():
                return {}
            return {r["asset"]: mapped_receipt(db, json.loads(r["payload"])) for r in db.execute(
                "SELECT a.asset,a.payload FROM ingestions a JOIN (SELECT asset,MAX(seq) seq FROM ingestions GROUP BY asset) b ON a.seq=b.seq")}

    def record_ingestion(self, receipt: dict) -> None:
        """Append submission/binding evidence; never overwrite an earlier revision."""
        with self.connection(True) as db:
            db.execute("INSERT INTO ingestions(asset,payload) VALUES (?,?)", (receipt["asset_id"], json.dumps(receipt)))

    def latest_asset_event(self, table: str, asset_id: str) -> dict:
        """Indexed per-asset lookup; bulk history reads belong only in reconciliation."""
        if table not in {"cloud", "ingestions"}:
            raise ValueError("Unknown asset evidence table")
        if not self.path.exists():
            return {}
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                return {}
            row = db.execute(f"SELECT payload FROM {table} WHERE asset=? ORDER BY seq DESC LIMIT 1", (asset_id,)).fetchone()
            return mapped_receipt(db, json.loads(row[0])) if row else {}

    def prepare_ingestion(self, asset_id: str, proof: dict) -> dict:
        """Freeze exact local bytes and source checksums before opening an upload."""
        if proof.get("stage") != "mosaic" or not artifact_current(proof):
            raise ValueError("Upload requires current mosaic bytes")
        receipt = {"asset_id": asset_id, "local_sha256": proof["sha256"], "sources": proof["sources"],
                   "auxiliary_hashes": proof.get("auxiliary_hashes", {}), "token": uuid.uuid4().hex,
                   "phase": "prepared", "utc": utc_now(), "task_id": ""}
        self.record_ingestion(receipt)
        return receipt


def ingestion_properties(receipt: dict) -> dict[str, str]:
    """Properties echoed by EE bind the submitted bytes and source list cheaply."""
    return {"swotflow_revision": receipt["token"], "swotflow_sha256": receipt["local_sha256"],
            "swotflow_sources_sha256": digest(receipt["sources"])}


def bound_ingestion(receipt: dict, asset: dict) -> bool:
    """Require the same confirmed EE revision, never presence/name alone."""
    from swotflow.consolidation.core import snapshot_asset
    return bool(receipt.get("phase") == "confirmed" and
                receipt.get("asset_fingerprint") == digest(snapshot_asset(asset)) and
                all(asset.get("properties", {}).get(k) == v for k, v in ingestion_properties(receipt).items()))


def revision_matches(store: VerificationStore, proof: dict, asset: dict) -> bool:
    """Match a current local revision using ingestion evidence or full pixel evidence."""
    from swotflow.consolidation.core import snapshot_asset
    receipt = store.latest_asset_event("ingestions", asset["id"])
    if bound_ingestion(receipt, asset) and receipt.get("local_sha256") == proof["sha256"] and receipt.get("sources") == proof["sources"]:
        return receipt.get("auxiliary_hashes", {}) == proof.get("auxiliary_hashes", {})
    cloud = store.latest_asset_event("cloud", asset["id"])
    return bool(cloud.get("passed") is True and cloud.get("mode") == "full" and
                cloud.get("asset_fingerprint") == digest(snapshot_asset(asset)) and
                cloud.get("local_sha256") == proof["sha256"] and cloud.get("sources") == proof["sources"] and
                cloud.get("local_auxiliary_hashes", {}) == proof.get("auxiliary_hashes", {}))


def mapped_receipt(db: sqlite3.Connection, proof: Any) -> Any:
    """Resolve relocation paths operationally, leaving immutable saved payloads intact."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='path_mappings'").fetchone():
        from swotflow.project.relocation import PathMapper
        for row in db.execute("SELECT mappings FROM path_mappings ORDER BY seq"):
            proof = PathMapper(json.loads(row[0])).value(proof)
    return proof


def artifact_current(proof: dict) -> bool:
    """Check a local output against its saved content hash."""
    p = Path(proof["path"])
    return (p.is_file() and p.stat().st_size == proof["size"] and file_hash(p) == proof["sha256"]
            and auxiliary_hashes(p) == proof.get("auxiliary_hashes", {}))


def download_proof(logs: Path, filename: str) -> dict | None:
    """Read one latest provider receipt; migrate the lookup index once if needed."""
    path = Path(logs) / "download_verification.sqlite3"
    if not path.exists():
        return None
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("CREATE INDEX IF NOT EXISTS checks_filename_id ON checks(filename,id)")
        db.commit()
        row = db.execute("SELECT file_state,valid,actual_hash FROM checks WHERE filename=? ORDER BY id DESC LIMIT 1",
                         (filename,)).fetchone()
        return {"state": mapped_receipt(db, json.loads(row[0])), "valid": bool(row[1]), "actual_hash": row[2]} if row else None
    finally:
        db.close()


def download_proofs(logs: Path) -> dict[str, dict]:
    """Load each file's latest provider check without relying on legacy success flags."""
    path = Path(logs) / "download_verification.sqlite3"
    if not path.exists():
        return {}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        return {name: {"state": json.loads(state), "valid": bool(valid), "actual_hash": actual} for name, state, valid, actual in db.execute(
            "SELECT c.filename,c.file_state,c.valid,c.actual_hash FROM checks c JOIN (SELECT filename,MAX(id) id FROM checks GROUP BY filename) b ON c.id=b.id")}
    finally:
        db.close()


def expected_checksum(candidate: dict) -> dict:
    """Use exact provider checksum metadata; an alias is not an extra observation."""
    entries = candidate.get("data_granule", {}).get("ArchiveAndDistributionInformation", [])
    values = [e for e in entries if e.get("Name") == candidate["filename"]]
    if len(values) != 1:
        return {}
    e = values[0]
    return {"algorithm": e.get("Checksum", {}).get("Algorithm", "").lower().replace("-", ""),
            "hash": e.get("Checksum", {}).get("Value", "").lower(), "size": int(e.get("SizeInBytes", 0))}


def catalogue_selection(logs: Path, config: dict, scan_id: str = "") -> tuple[str, dict[str, dict]] | None:
    """Bind the mosaic planner to the completed NASA selection for this run scope."""
    from swotflow.catalog.history import CatalogHistory
    from swotflow.processing.download import normalize_utm_tiles
    from swotflow.core.temporal import acquisition_scope
    ownership = acquisition_scope(config)
    start, end = config.get("start_date"), config.get("end_date")
    if not start or not end:
        return None  # Direct local workflows still apply processing-rank selection.
    ledger = CatalogHistory(Path(logs) / "nasa_catalog_history.sqlite3")
    tiles = sorted(normalize_utm_tiles(config.get("utm_tiles", [])))
    scans = [s for s in ledger.scans() if (not scan_id or s["id"] == scan_id) and s["status"] == "complete" and
             (scope := json.loads(s["scope"])).get("start") == start and scope.get("end") == end
             and sorted(scope.get("tiles", [])) == tiles
             and {k: scope[k] for k in ("acquisition_start_date", "acquisition_end_date") if scope.get(k)} == ownership]
    if not scans:
        raise ValueError("No complete NASA selection for this exact mosaic tile/date scope; run Download first.")
    snapshot = ledger.snapshot(scans[0]["id"])
    candidates = {product_name(c["filename"]): c for c in snapshot["candidates"]}
    return snapshot["id"], {product_name(s["filename"]): expected_checksum(candidates[product_name(s["filename"])])
                            for s in snapshot["selections"] if s["selected"]}


def pending_local_inputs(store: VerificationStore, snapshot: dict) -> list[str]:
    """Require current local outputs and this snapshot's exact source checksums."""
    candidates = {product_name(c["filename"]): c for c in snapshot["candidates"]}
    expected = {product_name(s["filename"]): expected_checksum(candidates[product_name(s["filename"])])
                for s in snapshot["selections"] if s["selected"]}
    represented = {}
    for proof in store.artifacts().values():
        if proof["stage"] == "mosaic" and artifact_current(proof):
            represented.update(proof["sources"])
    return sorted(n for n, checksum in expected.items() if not checksum or represented.get(n) != checksum)


def reconcile(snapshot: dict, project: dict, inventory: dict, store: VerificationStore,
              consolidated: list[dict] | None = None, excluded_assets: set[str] | None = None) -> dict:
    """Account for exact preferred inputs in O(inputs + memberships).

    Missing inputs and source pixel gaps are deliberately different concepts.
    Stored receipts establish historical byte identity, not present local files.
    """
    import swotflow.catalog.history as history
    from swotflow.consolidation.core import snapshot_asset
    represented, unresolved = history.represented_inputs(inventory, project["mosaic_manifest"])
    represented = [p for p in represented if p["asset_id"] not in (excluded_assets or set())]
    represented.extend(consolidated or [])
    versions = history.compare_versions(snapshot, represented, inventory.get("finished_utc", ""))
    selected = {product_name(r["filename"]) for r in snapshot["selections"] if r["selected"]}
    candidates = {product_name(c["filename"]): c for c in snapshot["candidates"]}
    artifacts, downloaded, clouds = store.artifacts(), download_proofs(store.logs), store.cloud_receipts()
    ingestions = store.ingestions()
    from swotflow.core.metadata import product_identity_key
    owners = defaultdict(list)
    for member in represented:
        owners[product_identity_key(member["filename"])].append(member["asset_id"])
    extracted, mosaicked, memberships = defaultdict(list), defaultdict(list), defaultdict(set)
    live = {a["id"]: a for a in inventory["assets"]}
    for p in represented:
        memberships[product_name(p["filename"])].add(p["asset_id"])
    for proof in artifacts.values():
        target = extracted if proof["stage"] == "extract" else mosaicked
        for name in proof["sources"]:
            target[name].append(proof)
    rows = []
    consolidated_ids = {p["asset_id"] for p in consolidated or []}
    version_rows = {r["preferred_nasa"]: r for r in versions["rows"] if r["preferred_nasa"]}
    for name in sorted(selected):
        expected = expected_checksum(candidates[name])
        dp = downloaded.get(name)
        download_ok = bool(expected and dp and dp["valid"] and dp["state"][5:8] == [expected["size"], expected["algorithm"], expected["hash"]])
        extraction = [p for p in extracted[name] if p["sources"].get(name) == expected and expected]
        mosaic = [p for p in mosaicked[name] if p["sources"].get(name) == expected and expected]
        assets = sorted(memberships[name])
        verified, bound = [], []
        for aid in assets:
            receipt = clouds.get(aid, {})
            if (aid in live and receipt.get("passed") is True and receipt.get("mode") == "full"
                    and receipt.get("asset_fingerprint") == digest(snapshot_asset(live[aid]))
                    and receipt.get("sources", {}).get(name) == expected and expected
                    and any(p["sha256"] == receipt.get("local_sha256") and p.get("auxiliary_hashes", {}) == receipt.get("local_auxiliary_hashes", {}) for p in mosaic)):
                verified.append(aid)
                bound.append(aid)
            ingestion = ingestions.get(aid, {})
            if (aid in live and bound_ingestion(ingestion, live[aid]) and expected
                    and ingestion.get("sources", {}).get(name) == expected
                    and any(p["sha256"] == ingestion.get("local_sha256") and p["sources"] == ingestion.get("sources") for p in mosaic)):
                bound.append(aid)
            # Consolidation membership is visible, but legacy full-pixel receipts without
            # original byte checksums cannot certify a new source-checksum requirement.
        duplicate = len(owners[product_identity_key(name)]) > 1
        state = ("duplicate_active_inputs" if duplicate else "verified_active" if verified else
                 "ee_revision_bound_pixels_pending" if bound else "ee_present_needs_pixel_or_provenance_check" if assets else "mosaic_pending_upload" if mosaic else
                 "extraction_pending_mosaic" if extraction else "download_pending_extraction" if download_ok else "download_missing_or_unverified")
        vr = version_rows.get(name, {})
        rows.append({"filename": name, "identity": candidates[name]["identity"], "tile": candidates[name].get("tile", ""),
                     "state": state, "version_finding": vr.get("status", ""), "asset_ids": assets,
                     "verified_assets": verified, "revision_bound_assets": sorted(set(bound)),
                     "conflicting_asset_ids": sorted(set(owners[product_identity_key(name)])) if duplicate else [], "download_verified": download_ok, "extraction_recorded": bool(extraction),
                     "mosaic_recorded": bool(mosaic), "source_checksum": expected,
                     "consolidated_membership": any(a in consolidated_ids for a in assets)})
    counts = dict(Counter(r["state"] for r in rows))
    blockers = []
    if snapshot.get("status") != "complete" or not history.recent(snapshot.get("finished")):
        blockers.append("NASA search is incomplete or older than 24 hours; refresh before declaring current completeness.")
    if not history.recent(inventory.get("finished_utc")):
        blockers.append("EE inventory is undated or older than 24 hours.")
    if unresolved:
        blockers.append("Some present assets have unresolved source lineage.")
    unknown_versions = [r for r in versions["rows"] if r["status"] in {"not_returned_in_search", "timestamp_variant_review", "equal_rank_variant_review", "recorded_version_ranks_higher"}]
    if unknown_versions:
        blockers.append("Catalogue/recorded version variants require review.")
    report = {"utc": utc_now(), "scan_id": snapshot["id"], "scope": snapshot["scope"], "collection": inventory["collection"],
              "catalogue_finished_utc": snapshot.get("finished"), "inventory_finished_utc": inventory.get("finished_utc"),
              "counts": counts, "expected_preferred_inputs": len(rows), "complete": not blockers and all(r["state"] == "verified_active" for r in rows),
              "blockers": blockers, "unresolved_assets": unresolved, "rows": rows, "version_review": unknown_versions,
              "gap_note": "These are input-accounting gaps. Spatial nodata/source gaps require pixel/footprint investigation; no source absence is inferred from a blank map."}
    report["repair_queue"] = [{"filename": r["filename"], "identity": r["identity"], "reason": r["state"],
                               "version_finding": r["version_finding"], "affected_assets": r.get("conflicting_asset_ids") or version_rows.get(r["filename"], {}).get("asset_ids", [])}
                              for r in rows if r["state"] != "verified_active"]
    report["input_accounting_complete"] = not blockers and all(
        r["state"] != "duplicate_active_inputs" and len(r["asset_ids"]) == 1 and
        (bool(r.get("revision_bound_assets")) or r["state"] == "verified_active") for r in rows)
    report["pixel_verification_complete"] = report["complete"]
    return report


def save_report(store: VerificationStore, report: dict) -> Path:
    """Append a dated report and atomically replace the latest pointer."""
    folder = store.logs / "verification_reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8] + ".json")
    text = json.dumps(report, indent=2)
    path.write_text(text, encoding="utf-8")
    tmp = folder / (uuid.uuid4().hex + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(folder / "latest.json")
    return path


def inventory_path(store: VerificationStore) -> Path:
    """Return the full metadata snapshot used by current-proof consumers."""
    return store.logs / "verification_ee_inventory.json"


def save_inventory(store: VerificationStore, inventory: dict) -> None:
    """Replace only a complete inventory snapshot, never an interrupted page list."""
    if inventory.get("partial") or inventory.get("nextPageToken") or not isinstance(inventory.get("assets"), list):
        raise ValueError("Incomplete EE inventory")
    store.logs.mkdir(parents=True, exist_ok=True)
    temp = store.logs / (uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(inventory), encoding="utf-8")
    temp.replace(inventory_path(store))
    from swotflow.consolidation.core import snapshot_asset
    submissions = store.ingestions()
    for asset in inventory["assets"]:
        receipt = submissions.get(asset["id"], {})
        if receipt.get("phase") == "submitted" and all(asset.get("properties", {}).get(k) == v
                                                       for k, v in ingestion_properties(receipt).items()):
            store.record_ingestion({**receipt, "phase": "confirmed", "observed_utc": utc_now(),
                                    "asset_fingerprint": digest(snapshot_asset(asset))})


def load_inventory(store: VerificationStore, collection: str) -> dict:
    """Require the snapshot to belong to this exact destination."""
    path = inventory_path(store)
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return value if value.get("collection") == collection else {}


def active_proofs(store: VerificationStore, collection: str) -> dict[str, dict]:
    """Resolve full-pixel original or approved replacement proofs to source bytes.

    A consolidated replacement must preserve a checksum chain to independently
    checked source assets. Legacy filename-only approvals remain unverified here.
    """
    import swotflow.catalog.history as history
    import swotflow.consolidation.core as c
    from swotflow.consolidation.register import ReplacementRegister, default_path
    inventory = load_inventory(store, collection)
    if not history.recent(inventory.get("finished_utc")):
        return {}
    receipts = store.cloud_receipts()
    coverage = ReplacementRegister(default_path(collection), collection).coverage()
    excluded = {p["asset_id"] for p in coverage["products"] if p["role"] in {"represented original", "superseded revision"}}
    candidates: dict[str, list[dict]] = defaultdict(list)
    for asset in inventory.get("assets", []):
        if asset["id"] in excluded:
            continue
        receipt = receipts.get(asset["id"], {})
        if receipt.get("passed") is True and receipt.get("mode") == "full" and receipt.get("asset_fingerprint") == digest(c.snapshot_asset(asset)):
            for name, checksum in receipt.get("sources", {}).items():
                candidates[name].append({"asset_id": asset["id"], "checksum": checksum})
    conflicts = {aid for row in coverage.get("conflicts", []) for aid in row.get("asset_id", "").split(", ")}
    for product in coverage["products"]:
        if (product["kind"] != "consolidated" or product["role"] != "active" or product["availability"] != "present"
                or product["errors"] or not product["verified"] or product["verification"] != "full_pixels" or product["asset_id"] in conflicts):
            continue
        for source in product["details"]["group"]["sources"]:
            prior = receipts.get(source["asset"]["id"], {})
            if prior.get("passed") is not True or prior.get("mode") != "full" or prior.get("asset_fingerprint") != digest(c.snapshot_asset(source["asset"])):
                continue
            for original in source["original_inputs"]:
                name = product_name(original["filename"])
                if name in prior.get("sources", {}):
                    candidates[name].append({"asset_id": product["asset_id"], "checksum": prior["sources"][name]})
    # Count observation identities as well as exact names: old/new versions cannot
    # both justify an automatic reuse decision.
    from swotflow.core.metadata import product_identity_key
    project = history.read_project_snapshot(store.logs.parent)
    represented, unresolved = history.represented_inputs(inventory, project["mosaic_manifest"])
    represented = [p for p in represented if p["asset_id"] not in excluded]
    # Full receipts can resolve lineage absent from a cleaned legacy manifest.
    known = {(p["asset_id"], product_name(p["filename"])) for p in represented}
    for name, values in candidates.items():
        for value in values:
            if (value["asset_id"], name) not in known:
                represented.append({"asset_id": value["asset_id"], "filename": name})
                known.add((value["asset_id"], name))
    resolved = {aid for aid, _ in known}
    if any(p["asset_id"] not in resolved | excluded for p in unresolved):
        return {}  # An unknown active owner cannot safely justify automatic reuse.
    observations: dict[str, list[str]] = defaultdict(list)
    for member in represented:
        observations[product_identity_key(member["filename"])].append(member["asset_id"])
    return {name: values[0] for name, values in candidates.items()
            if len(values) == 1 and len(observations[product_identity_key(name)]) == 1}


def reusable_download_groups(granules: list[Any], store: VerificationStore, collection: str) -> set[str]:
    """Reuse only wholly represented local grouping keys; additions rebuild their group."""
    from swotflow.verification.download import provider_integrity
    from swotflow.core.metadata import parse_swot_l2_hr_raster_metadata
    active = active_proofs(store, collection) if collection else {}
    groups: dict[tuple, list[tuple[str, bool]]] = defaultdict(list)
    for g in granules:
        fields = parse_swot_l2_hr_raster_metadata(g.file_name).fields
        key = tuple(fields[k] for k in ("descriptor", "cycle_id", "pass_id")) + (fields["range_beginning"][:8],)
        size, algorithm, hash_value = provider_integrity(g)
        wanted = {"size": size, "algorithm": algorithm, "hash": hash_value}
        groups[key].append((g.file_name, active.get(g.file_name, {}).get("checksum") == wanted))
    return {name for rows in groups.values() if all(ok for _, ok in rows) for name, _ in rows}


def project_report(root: Path, store: VerificationStore, scan_id: str, inventory: dict) -> dict:
    """Join a chosen NASA snapshot, current EE metadata, stages and replacement history."""
    import swotflow.catalog.history as history
    from swotflow.consolidation.register import ReplacementRegister, default_path
    ledger = history.CatalogHistory(store.logs / "nasa_catalog_history.sqlite3")
    coverage = ReplacementRegister(default_path(inventory["collection"]), inventory["collection"]).coverage()
    excluded = {p["asset_id"] for p in coverage["products"] if p["role"] in {"represented original", "superseded revision"}}
    consolidated, warnings = history.approved_consolidated_inputs(inventory["collection"], "", "")
    report = reconcile(ledger.snapshot(scan_id), history.read_project_snapshot(root), inventory, store, consolidated, excluded)
    certified = active_proofs(store, inventory["collection"])
    for row in report["rows"]:
        proof = certified.get(row["filename"], {})
        if (row["consolidated_membership"] and row["state"] != "duplicate_active_inputs"
                and proof.get("checksum") == row["source_checksum"] and proof.get("asset_id") in row["asset_ids"]):
            row["state"] = "verified_active"
            row["verified_assets"] = [proof["asset_id"]]
    if warnings:
        report["blockers"].append("Consolidation availability or approval evidence requires refresh.")
    report["counts"] = dict(Counter(r["state"] for r in report["rows"]))
    report["complete"] = not report["blockers"] and all(r["state"] == "verified_active" for r in report["rows"])
    report["pixel_verification_complete"] = report["complete"]
    report["input_accounting_complete"] = not report["blockers"] and all(
        r["state"] != "duplicate_active_inputs" and len(r["asset_ids"]) == 1 and
        (bool(r.get("revision_bound_assets")) or r["state"] == "verified_active") for r in report["rows"])
    pending = {r["filename"]: r for r in report["rows"] if r["state"] != "verified_active"}
    report["repair_queue"] = [{**r, "reason": pending[r["filename"]]["state"]} for r in report["repair_queue"] if r["filename"] in pending]
    return report


def cleanup_guards(config: dict, candidates: list[Any]) -> list[Any]:
    """Filter legacy proposals through current downstream proofs in verified mode."""
    from dataclasses import replace
    store = VerificationStore(Path(config["download"]["manifest_csv"]).parent)
    artifacts = store.artifacts()
    downstream: dict[str, list[dict]] = defaultdict(list)
    for proof in artifacts.values():
        for path in proof["inputs"]:
            downstream[path].append(proof)
    active = active_proofs(store, config.get("destination_parent", ""))
    approved = []
    for item in candidates:
        path = item.path.resolve()
        # Sidecars are retained. They are small and cannot independently prove the
        # identity of their parent raster once that raster has been removed.
        if item.stage not in {"raw", "extracted", "mosaic"} or path.suffix.lower() not in {".nc", ".tif", ".tiff"}:
            continue
        if item.stage == "mosaic":
            own = artifacts.get(str(path))
            good = own and artifact_current(own) and own["sources"] and all(active.get(n, {}).get("checksum") == h for n, h in own["sources"].items())
            proof = own if good else None
        else:
            stage = "extract" if item.stage == "raw" else "mosaic"
            proof = next((p for p in downstream.get(str(path), []) if p["stage"] == stage and artifact_current(p)), None)
        if proof and path.is_file():
            if item.stage == "raw":
                source = proof["sources"].get(product_name(path), {})
                if not source or file_hash(path, source["algorithm"]) != source["hash"]:
                    continue
            elif item.stage == "extracted" and (file_hash(path) != proof["inputs"].get(str(path))
                    or auxiliary_hashes(path) != proof.get("input_auxiliaries", {}).get(str(path), {})):
                continue
            guard = {"logs": str(store.logs), "collection": config.get("destination_parent", ""),
                     "target": str(path), "target_sha256": file_hash(path), "stage": item.stage,
                     "downstream": proof, "target_auxiliary_hashes": auxiliary_hashes(path)}
            approved.append(replace(item, verification_guard=guard, reason="Current downstream file/EE evidence; rechecked immediately before deletion."))
    return approved


def check_cleanup_guard(guard: dict) -> bool:
    """Fail closed when a preview's target or its downstream proof has changed."""
    target = Path(guard["target"])
    if (not target.is_file() or file_hash(target) != guard["target_sha256"]
            or auxiliary_hashes(target) != guard.get("target_auxiliary_hashes", {})):
        return False
    if guard["stage"] == "mosaic":
        active = active_proofs(VerificationStore(Path(guard["logs"])), guard["collection"])
        return all(active.get(n, {}).get("checksum") == h for n, h in guard["downstream"]["sources"].items())
    return artifact_current(guard["downstream"])
