"""Provider-checksum verification and explicit protection of reset projects."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def assert_project_not_reset(logs: Path) -> None:
    """Reject operational reuse of history belonging to a deleted collection."""
    logs = Path(logs)
    candidates = [logs / "collection_reset.json"]
    candidates.extend(p / "00_logs" / "collection_reset.json" for p in logs.parents
                      if (p / "project.yaml").is_file())
    marker = next((p for p in candidates if p.exists()), None)
    if marker is not None:
        raise ValueError(
            f"This project's collection was marked for reset ({marker}). "
            "Keep its historical records and create a separate project for validation/rebuilding."
        )


def provider_integrity(granule: Any) -> tuple[int, str, str]:
    """Read the exact NetCDF byte count and checksum; fail on ambiguous metadata."""
    source = granule.source
    umm = source.get("umm", {}) if isinstance(source, dict) else {}
    entries = umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation", [])
    matches = [item for item in entries if item.get("Name") == granule.file_name]
    if len(matches) != 1:
        raise ValueError(f"Missing or ambiguous NASA file verification metadata: {granule.file_name}")
    item = matches[0]
    checksum = item.get("Checksum", {})
    algorithm = str(checksum.get("Algorithm", "")).lower().replace("-", "")
    expected = str(checksum.get("Value", "")).lower()
    size = int(item.get("SizeInBytes", 0))
    if algorithm not in {"md5", "sha256", "sha512"} or size <= 0:
        raise ValueError(f"Unsupported NASA checksum or missing byte count: {granule.file_name}")
    if len(expected) != hashlib.new(algorithm).digest_size * 2 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError(f"Invalid NASA checksum: {granule.file_name}")
    return size, algorithm, expected


def verified_file(path: Path, granule: Any, logs: Path) -> bool:
    """Verify exact bytes/hash now, retaining an append-only receipt after cleanup.

    Persisted receipts never bypass a new check: Windows can preserve identical
    timestamps across quick same-size rewrites. The downloader separately avoids
    duplicate checks within a single controlled transfer run.
    """
    size, algorithm, expected = provider_integrity(granule)
    if not path.is_file():
        return False
    stat = path.stat()
    key = json.dumps([str(path.resolve()), granule.file_name, stat.st_size,
                      stat.st_mtime_ns, stat.st_ctime_ns, size, algorithm, expected])
    logs.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(logs / "download_verification.sqlite3", timeout=30)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY, file_state TEXT, utc TEXT, "
                   "filename TEXT, actual_hash TEXT, valid INTEGER)")
        db.execute("CREATE INDEX IF NOT EXISTS checks_filename_id ON checks(filename,id)")
        actual = ""
        if stat.st_size == size:
            digest = hashlib.new(algorithm)
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            after = path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError(f"File changed during verification: {path.name}")
            actual = digest.hexdigest()
        valid = stat.st_size == size and actual == expected
        db.execute("INSERT INTO checks(file_state,utc,filename,actual_hash,valid) VALUES (?,?,?,?,?)", (key, datetime.now(timezone.utc).isoformat(),
                   granule.file_name, actual, int(valid)))
    return valid
