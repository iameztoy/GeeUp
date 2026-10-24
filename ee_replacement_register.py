"""Local, transactional consolidation lineage and approval register.

No EE writes, pipeline database writes, pixel processing or raster deletion.
Coverage is over registered input observations, not the NASA catalogue. Metadata
snapshots expire after 24 hours; task success never supplies scientific approval.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import ee_consolidation as c
import ee_consolidation_update as updates
from app_storage import consolidation_root
from swot_metadata import product_identity_key, swot_product_rank

SCHEMA = 1
MAX_INVENTORY_AGE = timedelta(hours=24)


def default_path(collection: str) -> Path:
    """Keep registers isolated by the full source collection identity."""
    return consolidation_root() / "registers" / (c.digest(c.collection_id(collection))[:24] + ".sqlite3")


def timestamp(value: str) -> datetime:
    """Require an explicit timezone rather than guessing the machine's timezone."""
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Inventory/evidence time needs an explicit timezone")
    return result.astimezone(timezone.utc)


def members(sources: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """Flatten original observations; versions are attributes, not new observations."""
    result, errors = {}, []
    for source in sources:
        if not source["lineage_complete"]:
            errors.append("Incomplete original-input lineage: " + source["asset"]["id"])
        for item in source["original_inputs"]:
            key = product_identity_key(item["filename"])
            if not key:
                errors.append("Unrecognized original input: " + item["filename"])
                continue
            product = item.get("crid", "") + "_" + item.get("product_counter", "")
            entry = {"identity": key, "product": product,
                     "scene_identity": "|".join(item.get(k, "") for k in
                                                ("descriptor", "cycle_id", "pass_id", "scene_id", "filename_suffix")),
                     "rank": list(swot_product_rank(item.get("crid", ""), item.get("product_counter", ""))),
                     "filename": item["filename"], "tile": item.get("coordinate_system", source["tile"]),
                     "date": source["date"], "source": source["asset"]["id"]}
            if key in result:
                errors.append("Repeated original observation within output: " + key)
            result[key] = entry
    return result, sorted(set(errors))


class ReplacementRegister:
    """Persist immutable planned revisions, snapshots, evidence and active choices."""

    def __init__(self, path: Path, collection: str) -> None:
        self.path = Path(path)
        self.collection = c.collection_id(collection)

    @contextmanager
    def connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Use SQLite transactions; read-only refresh never creates a register."""
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=15)
        else:
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
                for sql in (
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS inventories (collection TEXT PRIMARY KEY, observed TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS live (id TEXT PRIMARY KEY, collection TEXT NOT NULL, payload TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS active (id TEXT PRIMARY KEY)",
                    "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, utc TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL)",
                ):
                    db.execute(sql)
                db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
                db.execute("INSERT OR IGNORE INTO meta VALUES ('collection', ?)", (self.collection,))
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("schema") != str(SCHEMA) or meta.get("collection") != self.collection:
                raise ValueError("Register schema or source collection does not match this project")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, action: str, payload: dict) -> None:
        """Append an audit event in the same transaction as the change."""
        db.execute("INSERT INTO events(utc, action, payload) VALUES (?, ?, ?)",
                   (c.utc_now(), action, json.dumps(payload, sort_keys=True, allow_nan=False)))

    @staticmethod
    def _put(db: sqlite3.Connection, aid: str, kind: str, payload: dict) -> None:
        """Idempotent import; never silently rewrite an earlier source history."""
        old = db.execute("SELECT kind, payload FROM records WHERE id=?", (aid,)).fetchone()
        if old:
            previous = json.loads(old["payload"])
            if old["kind"] != kind:
                raise ValueError("Asset role changed: " + aid)
            if previous == payload:
                return
            if kind == "original" and previous["fingerprint"] == payload["fingerprint"]:
                old_members, _ = members([previous])
                new_members, _ = members([payload])
                if old_members == new_members or (not previous["lineage_complete"] and payload["lineage_complete"]):
                    db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), aid))
                    return
            raise ValueError("Conflicting stored history; review rather than replace: " + aid)
        db.execute("INSERT INTO records VALUES (?, ?, ?)", (aid, kind, json.dumps(payload, allow_nan=False)))

    def import_plan(self, plan: dict, destination: str) -> None:
        """Register candidates and flattened sources, without approving any output."""
        if plan["collection"] != self.collection:
            raise ValueError("Plan belongs to a different source collection")
        destination = c.validate_destination(self.collection, destination)
        compact = {k: plan[k] for k in ("algorithm", "options", "collection")}
        with self.connection(True) as db:
            for group in plan["groups"]:
                updates.check_saved_group(group, plan)
                for source in group["sources"]:
                    if source["asset"]["id"].rsplit("/", 1)[0] != self.collection:
                        raise ValueError("Plan source outside the registered collection")
                    self._put(db, source["asset"]["id"], "original", source)
                aid = destination + "/" + group["id"]
                stable_group = {k: v for k, v in group.items() if k != "action" or v != "create"}
                self._put(db, aid, "consolidated", {"group": stable_group, "plan": compact})
            self._event(db, "import_plan", {"destination": destination, "groups": [g["id"] for g in plan["groups"]]})

    def sync_inventory(self, inventory: dict, lineage: dict | None = None) -> None:
        """Atomically replace one complete inventory; unknown timestamps stay unknown.

        Call only with a full paginated collection snapshot, never a filtered page.
        A failed fetch must not be supplied as an empty inventory.
        """
        collection = c.collection_id(inventory["collection"])
        observed = inventory.get("finished_utc", "")
        if observed:
            when = timestamp(observed)
            if when > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("Inventory is dated in the future")
        assets = inventory["assets"]
        if not isinstance(assets, list) or inventory.get("nextPageToken") or inventory.get("partial"):
            raise ValueError("A complete collection inventory is required")
        seen = set()
        for asset in assets:
            aid = asset["id"]
            if aid in seen or aid.rsplit("/", 1)[0] != collection:
                raise ValueError("Duplicate or out-of-collection inventory asset")
            seen.add(aid)
        with self.connection(True) as db:
            old = db.execute("SELECT observed FROM inventories WHERE collection=?", (collection,)).fetchone()
            if old and old[0] and (not observed or timestamp(observed) < timestamp(old[0])):
                raise ValueError("An older/undated inventory cannot replace a newer snapshot")
            db.execute("DELETE FROM live WHERE collection=?", (collection,))
            db.executemany("INSERT INTO live VALUES (?, ?, ?)",
                           [(a["id"], collection, json.dumps(c.snapshot_asset(a))) for a in assets])
            db.execute("INSERT OR REPLACE INTO inventories VALUES (?, ?)", (collection, observed))
            if collection == self.collection:
                for asset in assets:
                    try:
                        source = c.source_record(asset, lineage or {})
                    except (ValueError, KeyError, TypeError):
                        continue  # Untracked assets remain explicit in coverage, not silently complete.
                    old_source = db.execute("SELECT payload FROM records WHERE id=?", (asset["id"],)).fetchone()
                    if old_source and json.loads(old_source[0])["fingerprint"] != source["fingerprint"]:
                        continue  # Preserve old proof; coverage reports the changed live asset.
                    self._put(db, asset["id"], "original", source)
            self._event(db, "inventory", {"collection": collection, "observed": observed, "asset_count": len(assets)})

    @staticmethod
    def _load(db: sqlite3.Connection) -> dict:
        """Read a consistent snapshot for checks; linear in registered source history."""
        return {"records": {r["id"]: {"kind": r["kind"], "data": json.loads(r["payload"])}
                            for r in db.execute("SELECT * FROM records")},
                "live": {r["id"]: json.loads(r["payload"]) for r in db.execute("SELECT * FROM live")},
                "inventories": dict(db.execute("SELECT collection, observed FROM inventories").fetchall()),
                "evidence": {r["id"]: json.loads(r["payload"]) for r in db.execute("SELECT * FROM evidence")},
                "active": {r[0] for r in db.execute("SELECT id FROM active")}}

    @staticmethod
    def _facts(aid: str, state: dict, now: datetime) -> dict:
        """Keep availability, structural validity, lineage and verification separate."""
        record = state["records"][aid]
        data, kind = record["data"], record["kind"]
        sources = [data] if kind == "original" else data["group"]["sources"]
        observations, errors = members(sources)
        if kind == "consolidated":
            errors += updates.original_conflicts(sources)
        observed = state["inventories"].get(aid.rsplit("/", 1)[0], "")
        fresh = bool(observed and timedelta(minutes=-5) <= now - timestamp(observed) <= MAX_INVENTORY_AGE)
        live = state["live"].get(aid)
        availability = ("present" if live else "absent") if fresh else "unknown/stale"
        if live:
            try:
                if kind == "consolidated":
                    updates.prior_record(live, data["group"], data["plan"])
                elif c.digest(c.snapshot_asset(live)) != data["fingerprint"]:
                    raise ValueError("Source changed since its recorded history")
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
        proof = state["evidence"].get(aid, {})
        expected = data["fingerprint"] if kind == "original" else data["group"]["fingerprint"]
        current_proof = bool(live and proof.get("asset_fingerprint") == c.digest(c.snapshot_asset(live)) and
                             proof.get("record_fingerprint") == expected)
        verified = bool(proof.get("result") == "pass" and current_proof)
        if current_proof and proof.get("result") == "fail":
            errors.append("Comparison failed; output cannot be approved")
        return {"asset_id": aid, "kind": kind, "members": observations, "sources": sources,
                "date": sources[0]["date"], "availability": availability, "observed_utc": observed,
                "errors": sorted(set(errors)), "verification": proof.get("level", "not checked") if verified else "FAILED" if current_proof else "not checked/current proof invalid",
                "verified": verified, "role": "active" if aid in state["active"] else "candidate" if kind == "consolidated" else "original"}

    def verification_template(self, aid: str) -> dict:
        """Produce an explicitly unperformed comparison template for an audit tool."""
        with self.connection() as db:
            db.execute("BEGIN")
            state = self._load(db)
        fact = self._facts(aid, state, datetime.now(timezone.utc))
        if fact["availability"] != "present":
            raise ValueError("Refresh the full inventory before creating a comparison template")
        data = state["records"][aid]["data"]
        dims = state["live"][aid]["bands"][0]["grid"]["dimensions"]
        return {"schema": 1, "asset_id": aid,
                "asset_fingerprint": c.digest(c.snapshot_asset(state["live"][aid])),
                "record_fingerprint": data["fingerprint"] if fact["kind"] == "original" else data["group"]["fingerprint"],
                "result": "not_run", "level": "full_pixels", "checked_utc": "", "method": "", "scope": "",
                "evidence_file": "", "evidence_sha256": "", "pixels_compared": 0,
                "expected_output_cells": dims["width"] * dims["height"], "masks_compared": False, "mismatches": None,
                "compared_bands": ["b1", "b2", "source_index"] if fact["kind"] == "consolidated" else ["b1", "b2"],
                "instructions": "Populate from an actual independent full-output comparison, including masks. This template is not evidence."}

    def record_verification(self, receipt: dict) -> None:
        """Import comparison evidence tied to exact output and source-history hashes.

        This records an external comparison; it does not execute a pixel audit.
        Only full comparisons can promote a revision. Sampled/structural results
        remain visible but cannot be mistaken for full scientific verification.
        """
        with self.connection(True) as db:
            state = self._load(db)
            aid = receipt["asset_id"]
            fact = self._facts(aid, state, datetime.now(timezone.utc))
            structural_errors = [e for e in fact["errors"] if not e.startswith("Comparison failed;")]
            if fact["availability"] != "present" or structural_errors:
                raise ValueError("Refresh metadata and resolve lineage/structural issues before attaching evidence")
            data = state["records"][aid]["data"]
            expected = data["fingerprint"] if fact["kind"] == "original" else data["group"]["fingerprint"]
            if (receipt.get("schema") != 1 or receipt.get("record_fingerprint") != expected or
                    receipt.get("asset_fingerprint") != c.digest(c.snapshot_asset(state["live"][aid]))):
                raise ValueError("Evidence does not match this exact asset and saved input history")
            if receipt.get("result") not in {"pass", "fail"} or receipt.get("level") not in {"structural", "sampled_pixels", "full_pixels"}:
                raise ValueError("Evidence needs an explicit result and verification level")
            if not receipt.get("method") or not receipt.get("scope") or not receipt.get("evidence_sha256"):
                raise ValueError("Evidence must include method, tested scope and evidence-file SHA256")
            if len(receipt["evidence_sha256"]) != 64 or any(ch not in "0123456789abcdef" for ch in receipt["evidence_sha256"]):
                raise ValueError("Invalid evidence SHA256")
            checked = timestamp(receipt["checked_utc"])
            if checked > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("Verification is dated in the future")
            if receipt.get("result") == "pass" and receipt.get("level") == "full_pixels":
                bands = ["b1", "b2", "source_index"] if fact["kind"] == "consolidated" else ["b1", "b2"]
                grid = state["live"][aid]["bands"][0]["grid"]["dimensions"]
                full_count = grid["width"] * grid["height"]
                if (receipt.get("compared_bands") != bands or receipt.get("mismatches") != 0 or
                        receipt.get("pixels_compared") != full_count or receipt.get("masks_compared") is not True):
                    raise ValueError("Full comparison requires every output cell, paired bands/masks, source_index and zero mismatches")
            db.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?)", (aid, json.dumps(receipt)))
            self._event(db, "verification", receipt)

    def import_receipt_file(self, path: Path) -> None:
        """Check the attached evidence file's digest before recording its receipt."""
        receipt = json.loads(path.read_text(encoding="utf-8"))
        evidence_path = Path(receipt["evidence_file"])
        if not evidence_path.is_absolute():
            evidence_path = path.parent / evidence_path
        with evidence_path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != receipt.get("evidence_sha256"):
            raise ValueError("Evidence file has changed or does not match its receipt")
        self.record_verification(receipt)

    def activate(self, aid: str, replace_ids: list[str], reason: str) -> None:
        """Atomically approve a local analysis choice, rejecting duplicate observations.

        No cloud publication or deletion occurs. Keep prior approved choices until
        this transaction succeeds. Replacement must preserve their product coverage.
        """
        if not reason.strip():
            raise ValueError("Record a reason for changing the active selection")
        with self.connection(True) as db:
            state = self._load(db)
            now = datetime.now(timezone.utc)
            facts = {k: self._facts(k, state, now) for k in state["records"]}
            chosen = facts[aid]
            if chosen["availability"] != "present" or chosen["errors"] or not chosen["verified"] or chosen["verification"] != "full_pixels":
                raise ValueError("Activation requires fresh availability, complete lineage and a passing full-pixel comparison")
            if aid in replace_ids or len(set(replace_ids)) != len(replace_ids) or not set(replace_ids) <= state["active"]:
                raise ValueError("Replacement list must contain distinct currently active predecessors")
            for old in replace_ids:
                for key, item in facts[old]["members"].items():
                    new = chosen["members"].get(key)
                    if not new or new["rank"] < item["rank"]:
                        raise ValueError("Replacement loses an existing observation or downgrades its processing version")
            selected = (state["active"] - set(replace_ids)) | {aid}
            seen, scenes = {}, {}
            for candidate in sorted(selected):
                for key, item in facts[candidate]["members"].items():
                    if key in seen:
                        raise ValueError("Duplicate observation in active selection: " + key)
                    scene = item["scene_identity"]
                    if scene in scenes and scenes[scene] != key:
                        raise ValueError("Revised acquisition timestamps for the same original scene; review before approval")
                    scenes[scene] = key
                    seen[key] = candidate
            # Parallel/alternative revisions with the same group cannot both be active.
            groups = set()
            for candidate in selected:
                rec = state["records"][candidate]
                if rec["kind"] == "consolidated":
                    d = rec["data"]
                    key = c.digest(list(c.grouping_key(d["group"]["sources"][0], c.Options(**d["plan"]["options"]))))
                    if key in groups:
                        raise ValueError("Conflicting active revisions for one acquisition/grid group")
                    groups.add(key)
            db.executemany("DELETE FROM active WHERE id=?", [(k,) for k in replace_ids])
            db.execute("INSERT OR IGNORE INTO active VALUES (?)", (aid,))
            self._event(db, "activate", {"asset_id": aid, "replaces": sorted(replace_ids), "reason": reason})

    def coverage(self, start: str = "", end: str = "", now: datetime | None = None) -> dict:
        """Return source-tile accounting in O(total registered input memberships).

        Different processing versions share a denominator; best registered version
        follows the app's product priority. Missing catalogue observations outside
        this register are deliberately not inferred.
        """
        c.Options(start=start, end=end).validate()
        if not self.path.exists():
            return {"collection": self.collection, "tiles": [], "products": [], "conflicts": [], "summary": {},
                    "inventories": {}, "scope": "No register yet. Import plans and complete inventories from EE Consolidation."}
        with self.connection() as db:
            db.execute("BEGIN")
            state = self._load(db)
        now = now or datetime.now(timezone.utc)
        facts = {aid: self._facts(aid, state, now) for aid in state["records"]}
        eligible = {aid: fact for aid, fact in facts.items() if (not start or fact["date"] >= start) and (not end or fact["date"] <= end)}
        expected, owners, conflicts = {}, defaultdict(list), []
        for aid, fact in eligible.items():
            for key, item in fact["members"].items():
                if key not in expected or item["rank"] > expected[key]["rank"]:
                    expected[key] = item
                owners[key].append(aid)
            for error in fact["errors"]:
                conflicts.append({"asset_id": aid, "reason": error})
        active_members = defaultdict(list)
        for aid in state["active"]:
            for key in facts[aid]["members"]:
                active_members[key].append(aid)
        duplicate_keys = {k for k, values in active_members.items() if len(values) > 1}
        for key in duplicate_keys:
            conflicts.append({"asset_id": ", ".join(active_members[key]), "reason": "Duplicate active observation: " + key})
        tiles = defaultdict(lambda: {"expected": 0, "uploaded": 0, "active": 0, "pending": 0,
                                     "awaiting": 0, "missing": 0, "unknown": 0, "conflicts": 0,
                                     "outdated": 0, "unresolved_assets": 0})
        for key, item in expected.items():
            row = tiles[item["tile"]]
            row["expected"] += 1
            candidates = [facts[aid] for aid in owners[key]]
            good = [f for f in candidates if f["availability"] == "present" and not f["errors"] and
                    f["members"][key]["rank"] == item["rank"]]
            original = any(f["kind"] == "original" for f in good)
            active = [f for f in good if f["role"] == "active" and f["verified"] and f["verification"] == "full_pixels"]
            row["uploaded"] += int(original)
            row["active"] += int(bool(active) and key not in duplicate_keys)
            row["outdated"] += int(any(facts[a]["members"][key]["rank"] < item["rank"] for a in active_members[key]))
            if key in duplicate_keys or any(f["errors"] for f in candidates):
                row["conflicts"] += 1
            elif active:
                continue
            elif any(facts[a]["availability"] == "absent" for a in active_members[key]):
                row["missing"] += 1
            elif any(f["availability"] == "unknown/stale" for f in candidates):
                row["unknown"] += 1
            elif any(f["kind"] == "consolidated" for f in good):
                row["awaiting"] += 1
            elif original:
                row["pending"] += 1
            else:
                row["missing"] += 1
        for aid, fact in eligible.items():
            if fact["errors"]:
                for tile in {s["tile"] for s in fact["sources"]}:
                    tiles[tile]["unresolved_assets"] += 1
        for tile, row in tiles.items():
            row["tile"] = tile
            row["status"] = ("conflict" if row["conflicts"] or row["unresolved_assets"] else
                             "missing" if row["missing"] else "unknown" if row["unknown"] else
                             "pending" if row["pending"] or row["outdated"] else
                             "awaiting" if row["awaiting"] else "complete" if row["expected"] else "unknown")
        active_sources = {s["asset"]["id"] for aid in state["active"] if facts[aid]["availability"] == "present" and
                          facts[aid]["verified"] and not facts[aid]["errors"] for s in facts[aid]["sources"]}
        replaced = {p for aid in state["active"] if state["records"][aid]["kind"] == "consolidated"
                    for p in state["records"][aid]["data"]["group"].get("predecessors", [])}
        # Transitive predecessor roles are only informational, never deletion authority.
        pending = list(replaced)
        while pending:
            old = pending.pop()
            if old in state["records"] and state["records"][old]["kind"] == "consolidated":
                for predecessor in state["records"][old]["data"]["group"].get("predecessors", []):
                    if predecessor not in replaced:
                        replaced.add(predecessor); pending.append(predecessor)
        for aid, fact in eligible.items():
            if aid not in state["active"] and (aid in active_sources or aid in replaced):
                fact["role"] = "represented original" if fact["kind"] == "original" else "superseded revision"
        untracked = sorted(set(state["live"]) - set(state["records"]))
        duplicates = []
        for key, ids in owners.items():
            originals = [aid for aid in ids if facts[aid]["kind"] == "original" and aid in state["live"]]
            if len(originals) > 1:
                versions = {facts[aid]["members"][key]["product"] for aid in originals}
                duplicates.append({"asset_id": ", ".join(sorted(originals)),
                                   "reason": ("Processing-version alternatives (one observation; best registered version expected): "
                                              if len(versions) > 1 else "Duplicate original observation in stored source assets: ") + key})
        summary = {key: sum(row[key] for row in tiles.values()) for key in
                   ("expected", "uploaded", "active", "pending", "awaiting", "missing", "unknown", "conflicts", "outdated")}
        summary.update(stored_images=sum(a.get("type") == "IMAGE" for a in state["live"].values()),
                       active_assets=len(state["active"]), untracked_assets=len(untracked), stored_duplicate_observations=len(duplicates),
                       represented_copies=sum(f["role"] in {"represented original", "superseded revision"} and
                                             f["availability"] == "present" for f in eligible.values()))
        return {"collection": self.collection, "tiles": [tiles[t] for t in sorted(tiles)],
                "products": [{k: v for k, v in f.items() if k not in {"members", "sources"}} |
                             {"observations": len(f["members"]), "tiles": sorted({m["tile"] for m in f["members"].values()}),
                              "details": state["records"][aid]["data"], "evidence": state["evidence"].get(aid)} for aid, f in sorted(eligible.items())],
                "conflicts": conflicts + duplicates, "untracked": untracked, "summary": summary,
                "inventories": state["inventories"],
                "scope": f"Registered-input coverage, {start or 'first registered date'} to {end or 'last registered date'}; "
                         "not NASA catalogue completeness or valid-pixel coverage. Inventories expire after 24 hours. "
                         "Stored-image count uses whole inventory snapshots; active selection is local only."}
