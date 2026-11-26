"""Durable per-project transfer journal; no credentials or session URLs here."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import uuid


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class CloudJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads(asset TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, utc TEXT, asset TEXT, payload TEXT);""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(uploads)")}
            additions = {"collection": "TEXT", "state": "TEXT", "bucket": "TEXT", "object_name": "TEXT",
                         "size": "INTEGER", "deleted_at": "TEXT"}
            for name, kind in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE uploads ADD COLUMN {name} {kind}")
            # Finish an interrupted schema/index backfill before any pending-state query.
            for asset, payload in list(db.execute("SELECT asset,payload FROM uploads WHERE collection IS NULL OR state IS NULL OR bucket IS NULL OR size IS NULL")):
                row = json.loads(payload)
                db.execute("UPDATE uploads SET collection=?,state=?,bucket=?,object_name=?,size=?,deleted_at=? WHERE asset=?",
                    (row["collection"], row["state"], row["bucket"], row["object"], row["size"], row.get("deleted_at"), asset))
            db.execute("CREATE INDEX IF NOT EXISTS uploads_scope_state ON uploads(collection,state)")
            db.execute("CREATE INDEX IF NOT EXISTS uploads_object ON uploads(bucket,object_name)")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('owner',?)", (uuid.uuid4().hex,))
            db.commit()
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        return closing(db)
    @property
    def owner(self):
        with self.connection() as db:
            return db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()[0]
    def rows(self, collection=None, states=None):
        clauses, values = [], []
        if collection is not None:
            clauses.append("collection=?"); values.append(collection)
        if states is not None:
            if not states:
                return []
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")"); values.extend(states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM uploads" + where + " ORDER BY rowid", values)]
    def get(self, asset):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM uploads WHERE asset=?", (asset,)).fetchone()
            return json.loads(row[0]) if row else None
    def put(self, row):
        # Only opaque secret IDs belong in durable, exportable project records.
        payload = json.dumps({**row, "updated_at": utc()}, sort_keys=True)
        if "upload_id=" in payload or "session_uri" in row:
            raise ValueError("A resumable session URL must not enter the upload journal.")
        with self.connection() as db:
            db.execute("INSERT INTO uploads(asset,payload,collection,state,bucket,object_name,size,deleted_at) VALUES (?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(asset) DO UPDATE SET payload=excluded.payload,collection=excluded.collection,state=excluded.state,"
                       "bucket=excluded.bucket,object_name=excluded.object_name,size=excluded.size,deleted_at=excluded.deleted_at",
                       (row["asset_id"], payload, row["collection"], row["state"], row["bucket"], row["object"], row["size"], row.get("deleted_at")))
            db.execute("INSERT INTO events(utc,asset,payload) VALUES (?,?,?)", (utc(), row["asset_id"], payload))
            db.commit()
    def live_bytes(self, bucket):
        with self.connection() as db:
            return db.execute("SELECT COALESCE(SUM(size),0) FROM uploads WHERE bucket=? AND deleted_at IS NULL", (bucket,)).fetchone()[0]

    def other_pending_reference(self, row):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM uploads WHERE bucket=? AND object_name=? AND asset<>? AND state<>'VERIFIED' LIMIT 1",
                              (row["bucket"], row["object"], row["asset_id"])).fetchone() is not None


def read_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT payload FROM uploads")]


def cloud_metrics(report_path, destination=""):
    rows = read_rows(Path(report_path).parent / "cloud_uploads.sqlite3")
    if destination:
        rows = [r for r in rows if r.get("collection") == destination]
    if not rows:
        return {}
    return {
        "Cloud upload tracked mosaics": str(len(rows)),
        "Cloud awaiting transfer / ingestion": str(sum(r["state"] not in {"VERIFIED", "FAILED", "BLOCKED"} for r in rows)),
        "Cloud EE ingestions verified": str(sum(r["state"] == "VERIFIED" for r in rows)),
        "Cloud failed / blocked (retained)": str(sum(r["state"] in {"FAILED", "BLOCKED"} for r in rows)),
        "Cloud staging copies removed": str(sum(bool(r.get("deleted_at")) for r in rows)),
        "Cloud recorded staging GiB (excludes soft delete)": f"{sum(r['size'] for r in rows if not r.get('deleted_at')) / 2**30:.3f}",
    }
