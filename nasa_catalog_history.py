"""Append-only NASA catalogue provenance and read-only processing-version checks.

This database is separate from swotflow.sqlite3. No EE mutations, raster
downloads, project migrations, or credential persistence are performed here.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout

from swot_metadata import parse_swot_l2_hr_raster_metadata, product_identity_key, processing_level_rank

SCHEMA = 1
POLICY = "swot_metadata.crid_then_product_counter_v1"
MAX_METADATA_AGE = timedelta(hours=24)
PAGE_SIZE = 2000


def utc_now() -> str:
    """Return an explicit UTC observation time."""
    return datetime.now(timezone.utc).isoformat()


def canonical_name(value: str) -> str:
    """Match NetCDF and extracted raster names without losing product suffixes."""
    name = PureWindowsPath(value).name
    return str(Path(name).with_suffix(".nc")) if Path(name).suffix.lower() in (".tif", ".tiff", ".nc") else name + ".nc"


def safe_metadata(value: Any) -> Any:
    """Retain public metadata, removing credential fields and URL queries."""
    if isinstance(value, dict):
        return {str(k): safe_metadata(v) for k, v in value.items()
                if not any(s in str(k).lower() for s in ("password", "authorization", "cookie", "access_token", "refresh_token", "secret"))}
    if isinstance(value, (list, tuple)):
        return [safe_metadata(v) for v in value]
    if isinstance(value, str) and value.startswith(("https://", "http://", "s3://")):
        u = urlsplit(value)
        return urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, "", ""))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def encoded(value: Any) -> bytes:
    """Encode deterministic, sanitized JSON for receipts and compression."""
    return json.dumps(safe_metadata(value), sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def history_path(manifest_csv: Path) -> Path:
    """Keep the ledger beside the project's download manifest."""
    return Path(manifest_csv).parent / "nasa_catalog_history.sqlite3"


class CatalogHistory:
    """Store immutable search pages, selections, and transfer events."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Create only on explicit writes; reads never migrate project databases."""
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=30)
        else:
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
                for sql in (
                    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS scans (id TEXT PRIMARY KEY, started TEXT NOT NULL, scope TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS pages (id INTEGER PRIMARY KEY, scan TEXT NOT NULL REFERENCES scans(id), query TEXT NOT NULL, ordinal INTEGER NOT NULL, observed TEXT NOT NULL, hits INTEGER, count INTEGER NOT NULL, sha256 TEXT NOT NULL, document BLOB NOT NULL, receipt TEXT NOT NULL, UNIQUE(scan, query, ordinal))",
                    "CREATE TABLE IF NOT EXISTS candidates (page INTEGER NOT NULL REFERENCES pages(id), ordinal INTEGER NOT NULL, scan TEXT NOT NULL REFERENCES scans(id), filename TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(page, ordinal))",
                    "CREATE INDEX IF NOT EXISTS candidate_scan ON candidates(scan)",
                    "CREATE INDEX IF NOT EXISTS candidate_filename ON candidates(filename)",
                    "CREATE INDEX IF NOT EXISTS candidate_identity ON candidates(identity)",
                    "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, scan TEXT NOT NULL REFERENCES scans(id), utc TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS event_scan ON events(scan)",
                    "CREATE TABLE IF NOT EXISTS selections (scan TEXT NOT NULL REFERENCES scans(id), filename TEXT NOT NULL, identity TEXT NOT NULL, selected INTEGER NOT NULL, preferred TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(scan, filename))",
                    "CREATE TABLE IF NOT EXISTS completions (scan TEXT PRIMARY KEY REFERENCES scans(id), finished TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL)",
                ):
                    db.execute(sql)
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema', ?)", (str(SCHEMA),))
            if dict(db.execute("SELECT key,value FROM metadata")).get("schema") != str(SCHEMA):
                raise ValueError("Unsupported NASA history schema")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def begin(self, scope: dict) -> str:
        """Checkpoint the exact query scope before contacting the catalogue."""
        scan = uuid.uuid4().hex
        with self.connection(True) as db:
            db.execute("INSERT INTO scans VALUES (?,?,?)", (scan, utc_now(), encoded(scope).decode()))
        return scan

    def page(self, scan: str, query: str, ordinal: int, document: dict, receipt: dict, products: list[dict]) -> None:
        """Commit a page and all candidates atomically before requesting another."""
        raw = encoded(document)
        with self.connection(True) as db:
            if db.execute("SELECT 1 FROM completions WHERE scan=?", (scan,)).fetchone():
                raise ValueError("A finished catalogue snapshot is immutable")
            cur = db.execute("INSERT INTO pages(scan,query,ordinal,observed,hits,count,sha256,document,receipt) VALUES (?,?,?,?,?,?,?,?,?)",
                             (scan, query, ordinal, utc_now(), receipt.get("hits"), len(products),
                              hashlib.sha256(raw).hexdigest(), gzip.compress(raw), encoded(receipt).decode()))
            db.executemany("INSERT INTO candidates VALUES (?,?,?,?,?,?)",
                           [(cur.lastrowid, i, scan, p["filename"], p["identity"], encoded(p).decode()) for i, p in enumerate(products)])

    def finish(self, scan: str, granules: list[Any], status: str, detail: str = "") -> None:
        """Append the terminal state and every version decision in one transaction."""
        if status not in {"complete", "limited", "unverified", "failed", "legacy"}:
            raise ValueError("Invalid catalogue completion state")
        with self.connection(True) as db:
            unique = {g.file_name: g for g in granules}
            db.executemany("INSERT INTO selections VALUES (?,?,?,?,?,?)", [
                (scan, name, product_identity_key(name), int(g.selected_for_download), g.preferred_file_name,
                 g.duplicate_filter_status + ": " + g.duplicate_reason) for name, g in unique.items()])
            db.execute("INSERT INTO completions VALUES (?,?,?,?)", (scan, utc_now(), status, detail))

    def transfer(self, scan: str, granules: list[Any], statuses: dict[str, tuple[str, str]], stage: str) -> None:
        """Checkpoint batch intent/results; retain interrupted attempts as pending."""
        rows = [{"filename": g.file_name, "concept_id": g.identity,
                 "status": statuses.get(g.identity, ("PENDING", ""))[0],
                 "local_path": statuses.get(g.identity, ("", ""))[1]} for g in granules]
        with self.connection(True) as db:
            db.execute("INSERT INTO events(scan,utc,action,payload) VALUES (?,?,?,?)",
                       (scan, utc_now(), stage, encoded(rows).decode()))

    def scans(self) -> list[dict]:
        """List snapshots without opening the project database or contacting NASA."""
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT s.*, c.finished, COALESCE(c.status,'interrupted/running') AS status, c.detail, "
                "(SELECT COUNT(*) FROM candidates WHERE scan=s.id) AS candidates "
                "FROM scans s LEFT JOIN completions c ON c.scan=s.id ORDER BY s.started DESC")]

    def snapshot(self, scan: str) -> dict:
        """Return a search's candidates and immutable selection decisions."""
        with self.connection() as db:
            row = db.execute("SELECT s.*,c.finished,c.status,c.detail FROM scans s LEFT JOIN completions c ON s.id=c.scan WHERE s.id=?", (scan,)).fetchone()
            if row is None:
                raise ValueError("Unknown catalogue snapshot")
            result = dict(row)
            result["scope"] = json.loads(result["scope"])
            result["candidates"] = [json.loads(r[0]) for r in db.execute("SELECT payload FROM candidates WHERE scan=? ORDER BY page,ordinal", (scan,))]
            result["selections"] = [dict(r) for r in db.execute("SELECT * FROM selections WHERE scan=? ORDER BY filename", (scan,))]
            result["transfers"] = [dict(r) for r in db.execute("SELECT utc,action,payload FROM events WHERE scan=? ORDER BY seq", (scan,))]
            return result

    def observation_history(self, identity: str) -> list[dict]:
        """Retrieve every observed version/revision for an acquisition, including legacy records."""
        with self.connection() as db:
            return [{"checked_utc": r[0], "scan_id": r[1], "catalogue_state": r[2] or "interrupted/running",
                     **json.loads(r[3])} for r in db.execute(
                "SELECT s.started,s.id,f.status,c.payload FROM candidates c JOIN scans s ON s.id=c.scan "
                "LEFT JOIN completions f ON f.scan=s.id WHERE c.identity=? ORDER BY s.started,c.page,c.ordinal", (identity,))]


def product_record(granule: Any, query: str) -> dict:
    """Normalize exact filenames, catalogue revisions, and provider checksums."""
    from swot_download_tool import normalize_granule, mapping_value
    g = normalize_granule(granule)
    meta, umm = mapping_value(granule, "meta") or {}, mapping_value(granule, "umm") or {}
    parsed = parse_swot_l2_hr_raster_metadata(g.file_name)
    fields = parsed.fields if parsed else {}
    return {"filename": g.file_name, "identity": product_identity_key(g.file_name),
            "concept_id": g.identity, "revision_id": meta.get("revision-id", ""),
            "revision_date": meta.get("revision-date", ""), "created": meta.get("created-at", ""),
            "provider": meta.get("provider-id", ""), "collection": query,
            "tile": g.utm_tile, "start": g.start_time, "end": g.end_time,
            "crid": fields.get("crid", ""), "counter": fields.get("product_counter", ""),
            "data_granule": safe_metadata(umm.get("DataGranule", {})), "links": safe_metadata(g.links)}


def request_page(session: Any, url: str, headers: dict, timeout: int) -> Any:
    """Retry bounded transient failures; never save authentication headers."""
    for attempt in range(3):
        try:
            response = session.get(url, headers=headers, params={"page_size": PAGE_SIZE}, timeout=timeout)
            if getattr(response, "status_code", 200) in (429, 500, 502, 503, 504) and attempt < 2:
                retry = response.headers.get("Retry-After", "2")
                delay = float(retry) if str(retry).isdigit() else 2 ** (attempt + 1)
                if delay > 60:
                    response.raise_for_status()
                time.sleep(max(1, delay))
                continue
            response.raise_for_status()
            return response
        except (TimeoutError, ConnectionError, RequestsTimeout, RequestsConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("NASA request retry limit reached")


def search_with_history(config: Any, earthaccess: Any = None, progress: Callable | None = None,
                        collections: list[str] | None = None) -> Any:
    """Harvest public metadata with page accounting, preserving excluded versions.

    No raster downloads or legacy manifest writes. A failed/inconsistent page
    leaves durable partial evidence and raises; it cannot become a complete scan.
    """
    import swot_download_tool as d
    d.validate_config(config)
    earthaccess = earthaccess or d.import_earthaccess()
    names = list(dict.fromkeys(collections or [config.collection_short_name]))
    ledger = CatalogHistory(history_path(config.manifest_csv))
    scope = {"collections": names, "start": config.start_date, "end": config.end_date,
             "tiles": d.normalize_utm_tiles(config.utm_tiles), "max_granules": config.max_granules,
             "filter": config.product_version_filter, "policy": POLICY, "provider": "POCLOUD",
             "ranking_code_sha256": hashlib.sha256(Path(__file__).with_name("swot_metadata.py").read_bytes()).hexdigest(),
             "endpoint": "https://cmr.earthdata.nasa.gov/search/granules.umm_json"}
    scan = ledger.begin(scope)
    results, status = [], "complete"
    try:
        for name in names:
            query = d.build_download_query(replace(config, collection_short_name=name))
            if not hasattr(earthaccess, "granule_query"):
                raw = d.run_search(earthaccess, query, query.granule_patterns, config.max_granules or -1)
                products = [product_record(g, name) for g in raw]
                ledger.page(scan, name, 1, {"items": list(raw)}, {"hits": None, "page_accounting": "unavailable"}, products)
                results.extend(raw)
                status = "unverified"
                continue
            q = d.build_earthaccess_granule_query(earthaccess, query, query.granule_patterns)
            headers = dict(getattr(q, "headers", {}) or {})
            total, received, page, seen_tokens, seen_concepts = None, 0, 0, set(), set()
            while True:
                response = request_page(q.session, q._build_url(), headers, config.search_request_timeout_seconds)
                document = response.json()
                if not isinstance(document, dict) or not isinstance(document.get("items"), list):
                    raise ValueError("NASA response lacks the expected items array")
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                hits = d.cmr_hits_from_document(document)
                if hits is None and response_headers.get("cmr-hits") is not None:
                    hits = int(response_headers["cmr-hits"])
                raw = d.wrap_cmr_page_items(earthaccess, document["items"])
                products = [product_record(g, name) for g in raw]
                page += 1
                token = response_headers.get("cmr-search-after", "")
                ledger.page(scan, name, page, document, {"hits": hits, "search_after": token,
                            "request_id": response_headers.get("cmr-request-id", ""),
                            "timed_out": response_headers.get("cmr-timed-out", "false")}, products)
                if response_headers.get("cmr-timed-out", "false").lower() == "true":
                    raise ValueError("NASA reported an incomplete timed-out response")
                if total is not None and hits != total:
                    raise ValueError("NASA hit count changed during pagination; repeat the search")
                total = hits
                for p in products:
                    if p["concept_id"] in seen_concepts:
                        raise ValueError("NASA repeated a catalogue concept during pagination")
                    seen_concepts.add(p["concept_id"])
                received += len(raw)
                results.extend(raw)
                if progress:
                    progress(received, total or received, f"NASA history: {name}, page {page}, {received} records saved")
                if config.max_granules is not None and len(results) >= config.max_granules:
                    if total is None or received < total or len(results) > config.max_granules or len(names) > 1:
                        status = "limited"
                    break
                if not raw or not token:
                    if total is not None and received != total:
                        raise ValueError("NASA page count does not reconcile with its hit count")
                    if total is None:
                        status = "unverified"
                    break
                if total is not None and received > total:
                    raise ValueError("NASA returned more records than its hit count")
                if token in seen_tokens:
                    raise ValueError("NASA repeated a pagination token")
                seen_tokens.add(token)
                headers["cmr-search-after"] = token
            if config.max_granules is not None and len(results) >= config.max_granules:
                break
        # Preserve every returned alias in pages, but choose once per exact filename.
        granules = {}
        for raw in results:
            g = d.normalize_granule(raw)
            parsed = parse_swot_l2_hr_raster_metadata(g.file_name)
            if (not parsed or not g.file_name.startswith("SWOT_L2_HR_Raster_100m_")
                    or g.utm_tile not in scope["tiles"]):
                raise ValueError("NASA returned an unrecognized or out-of-scope product; inspect saved page")
            granules.setdefault(g.file_name, g)
        returned = list(granules.values())
        eligible = returned if config.max_granules is None else returned[:config.max_granules]
        d.apply_product_version_filter(eligible, config.product_version_filter)
        for g in returned[len(eligible):]:
            g.selected_for_download = False
            g.duplicate_filter_status = "excluded_search_limit"
            g.duplicate_reason = "Outside the requested result limit; not a complete version comparison"
        ordered = sorted(returned, key=lambda g: g.file_name.lower())
        ledger.finish(scan, ordered, status)
        return d.DownloadPreview(query=d.build_download_query(config), granules=sorted(eligible, key=lambda g: g.file_name.lower()), history_scan_id=scan)
    except Exception as exc:
        # Persist a safe classification, not response text that might contain credentials.
        ledger.finish(scan, [], "failed", type(exc).__name__ + ": search did not pass accounting/validation")
        raise


def read_project_snapshot(root: Path) -> dict[str, list[dict]]:
    """Read a consistent legacy history snapshot without migration or write access."""
    datasets = ("download_manifest", "extract_manifest", "mosaic_manifest")
    result = {name: [] for name in datasets}
    path = Path(root) / "swotflow.sqlite3"
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            for name in datasets:
                result[name] = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM records WHERE dataset=?", (name,))]
        finally:
            db.close()
    else:
        for name in datasets:
            file = Path(root) / "00_logs" / (name + ".csv")
            if file.exists():
                with file.open(encoding="utf-8-sig", newline="") as stream:
                    result[name] = list(csv.DictReader(stream))
    return result


def import_legacy(ledger: CatalogHistory, snapshot: dict[str, list[dict]]) -> str:
    """Retain existing filenames with honest legacy origin, never fabricated CMR dates."""
    products = {}
    for dataset, rows in snapshot.items():
        for row in rows:
            names = ([row.get("file_name", "")] if dataset == "download_manifest" else
                     [row.get("input_nc", "")] if dataset == "extract_manifest" else json.loads(row.get("input_files") or "[]"))
            for value in names:
                if not value:
                    continue
                name = canonical_name(value)
                p = products.setdefault(name, {"filename": name, "identity": product_identity_key(name), "legacy_records": []})
                p["legacy_records"].append({"dataset": dataset, **{k: row.get(k, "") for k in
                    ("granule_id", "downloaded", "first_seen", "last_seen", "duplicate_filter_status", "preferred_file_name", "output_file")}})
    fingerprint = hashlib.sha256(encoded(products)).hexdigest()
    for scan in ledger.scans():
        if scan["status"] == "legacy" and json.loads(scan["scope"]).get("fingerprint") == fingerprint:
            return scan["id"]
    scan = ledger.begin({"origin": "legacy_project_records", "fingerprint": fingerprint,
                         "note": "Imported now; original catalogue snapshots/revisions are unknown"})
    rows = list(products.values())
    for offset in range(0, len(rows), PAGE_SIZE):
        page = rows[offset:offset + PAGE_SIZE]
        ledger.page(scan, "legacy", offset // PAGE_SIZE + 1, {"items": page}, {"hits": len(rows)}, page)
    ledger.finish(scan, [], "legacy")
    return scan


def represented_inputs(inventory: dict, mosaic_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Join EE existence metadata to recorded lineage; never infer input QA/pixel validity."""
    import ee_consolidation as c
    collection = c.collection_id(inventory["collection"])
    if inventory.get("partial") or inventory.get("nextPageToken") or not isinstance(inventory.get("assets"), list):
        raise ValueError("Supply a complete EE collection inventory")
    lineage = {PureWindowsPath(row.get("output_file", "")).stem: row for row in mosaic_rows}
    represented, unresolved, seen = [], [], set()
    for asset in inventory["assets"]:
        aid = asset["id"]
        if aid in seen or aid.rsplit("/", 1)[0] != collection:
            raise ValueError("Duplicate or out-of-collection EE inventory asset")
        seen.add(aid)
        try:
            source = c.source_record(asset, lineage)
            if not source["lineage_complete"]:
                raise ValueError("Original granule lineage is incomplete")
            represented.extend({"filename": canonical_name(p["filename"]), "asset_id": aid} for p in source["original_inputs"])
        except (ValueError, KeyError, TypeError) as exc:
            unresolved.append({"asset_id": aid, "reason": str(exc)})
    return represented, unresolved


def recent(value: str | None) -> bool:
    """Only dated, recent metadata may support a current-status label."""
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and -timedelta(minutes=5) <= datetime.now(timezone.utc) - stamp <= MAX_METADATA_AGE
    except ValueError:
        return False


def approved_consolidated_inputs(collection: str, start: str, end: str,
                                 register_path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Read approved, present consolidated lineage without changing the register.

    Candidate exports and stale/failed approvals cannot establish representation.
    The register already checks fingerprints, full comparison evidence and freshness.
    """
    from ee_replacement_register import ReplacementRegister, default_path
    report = ReplacementRegister(register_path or default_path(collection), collection).coverage(start, end)
    represented, unresolved = [], []
    conflicts = {aid for row in report.get("conflicts", []) for aid in row.get("asset_id", "").split(", ")}
    for product in report["products"]:
        if product["kind"] != "consolidated" or product["role"] != "active":
            continue
        if (product["availability"] != "present" or product["errors"] or product["asset_id"] in conflicts
                or not product["verified"] or product["verification"] != "full_pixels"):
            unresolved.append({"asset_id": product["asset_id"], "reason": "Approved consolidation needs current inventory/evidence review"})
            continue
        for source in product["details"]["group"]["sources"]:
            represented.extend({"filename": canonical_name(p["filename"]), "asset_id": product["asset_id"],
                                "basis": "approved_consolidation_register", "observed_utc": product["observed_utc"]}
                               for p in source["original_inputs"])
    return represented, unresolved


def compare_versions(snapshot: dict, represented: list[dict], inventory_time: str = "",
                     downloaded: list[str] | None = None) -> dict:
    """Compare observation identities in O(n) grouping plus per-group ranking.

    Versions are not separate required acquisitions. Changed timestamps, unknown
    lineage and stale/partial scopes never silently become complete coverage.
    """
    scope = snapshot["scope"]
    if not all(k in scope for k in ("tiles", "start", "end")):
        raise ValueError("Select a catalogue search, not a legacy import")
    candidates, stored, scenes = defaultdict(dict), defaultdict(dict), defaultdict(set)
    aliases = Counter(p["identity"] for p in snapshot["candidates"])
    for p in snapshot["candidates"]:
        name = canonical_name(p["filename"])
        parsed = parse_swot_l2_hr_raster_metadata(name)
        if not parsed:
            continue
        fields = parsed.fields
        date = datetime.strptime(fields["range_beginning"], "%Y%m%dT%H%M%S").date().isoformat()
        if fields["coordinate_system"] in scope["tiles"] and scope["start"] <= date <= scope["end"]:
            candidates[product_identity_key(name)][name] = p
    for p in represented:
        name = canonical_name(p["filename"])
        key = product_identity_key(name)
        if key:
            stored[key].setdefault(name, []).append(p["asset_id"])
            f = parse_swot_l2_hr_raster_metadata(name).fields
            scene = tuple(f.get(k, "") for k in ("descriptor", "cycle_id", "pass_id", "scene_id", "filename_suffix"))
            scenes[scene].add(key)
    downloaded_set = {canonical_name(n) for n in downloaded or []}
    rows = []
    # Include recorded inputs within the exact scope even if NASA no longer returns them.
    keys = set(candidates)
    for key, versions in stored.items():
        f = parse_swot_l2_hr_raster_metadata(next(iter(versions))).fields
        date = datetime.strptime(f["range_beginning"], "%Y%m%dT%H%M%S").date().isoformat()
        if f["coordinate_system"] in scope["tiles"] and scope["start"] <= date <= scope["end"]:
            keys.add(key)
    for key in sorted(keys):
        nasa, ee = candidates.get(key, {}), stored.get(key, {})
        rank = lambda n: (processing_level_rank(n), n.lower())
        preferred = max(nasa, key=rank) if nasa else ""
        present = max(ee, key=rank) if ee else ""
        name = preferred or present
        f = parse_swot_l2_hr_raster_metadata(name).fields
        scene = tuple(f.get(k, "") for k in ("descriptor", "cycle_id", "pass_id", "scene_id", "filename_suffix"))
        if not preferred:
            status = "not_returned_in_search"
        elif not present:
            status = "timestamp_variant_review" if scenes.get(scene) else "not_in_loaded_ee_lineage"
        elif preferred == present:
            status = "preferred_version_recorded"
        elif processing_level_rank(preferred) > processing_level_rank(present):
            status = "newer_version_available"
        elif processing_level_rank(preferred) < processing_level_rank(present):
            status = "recorded_version_ranks_higher"
        else:
            status = "equal_rank_variant_review"
        rows.append({"identity": key, "tile": f["coordinate_system"], "date": f["range_beginning"][:8],
                     "status": status, "preferred_nasa": preferred, "best_recorded_ee": present,
                     "catalogue_versions": sorted(nasa), "recorded_versions": sorted(ee),
                     "asset_ids": sorted({a for ids in ee.values() for a in ids}),
                     "preferred_downloaded": preferred in downloaded_set,
                     "catalogue_aliases": aliases[key]})
    return {"generated_utc": utc_now(), "scan_id": snapshot["id"], "catalogue_status": snapshot["status"] or "interrupted/running",
            "catalogue_time": snapshot.get("finished") or snapshot["started"], "inventory_time": inventory_time,
            "scope": scope, "current_complete_metadata": snapshot["status"] == "complete" and recent(snapshot.get("finished")) and recent(inventory_time),
            "summary": dict(Counter(r["status"] for r in rows)), "rows": rows,
            "limitations": ["Recorded input lineage and EE presence do not verify pixels or water coverage.",
                            "No deletion, replacement, download, or catalogue withdrawal is inferred automatically.",
                            "Completeness applies only to the selected search scope; pagination is not an atomic NASA snapshot."]}
