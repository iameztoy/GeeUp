"""Fail-closed cleanup of identifiable, superseded earthaccess temp files.

Only the fixed-width HDF5 attribute encodings used by HR Raster are decoded.
This is not a general HDF5 reader or a raster-validity check. Unknown, truncated
or ambiguous metadata is retained rather than guessed from a tile/time alone.
Attribute layout: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t3.html
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from swotflow.project.database import ProjectDatabase, database_path_for, dataset_for_path, read_project_rows
from swotflow.project.lock import download_folder_lock


HEADER_LIMIT = 256 * 1024
MIN_IDLE_SECONDS = 3600
PARTIAL_NAME = re.compile(r"partial_[a-z0-9_]{8}$")


@dataclass(frozen=True)
class PartialDownloadProof:
    path: Path
    product_name: str
    download_manifest: Path
    extract_manifest: Path
    signature: tuple[int, int, int, int, int]
    header_sha256: str


def _attribute(header: bytes, name: str, *, integer: bool = False) -> str | int:
    """Decode one unshared v3 attribute, rejecting duplicate/unknown encodings."""
    encoded = name.encode("ascii") + b"\0"
    positions = [m.start() for m in re.finditer(re.escape(encoded), header)]
    if len(positions) != 1 or positions[0] < 9:
        raise ValueError(f"Missing or ambiguous {name} attribute.")
    pos = positions[0]
    version, flags, namesize, typesize, spacesize, charset = struct.unpack(
        "<BBHHHB", header[pos - 9:pos]
    )
    if (version, flags, namesize, charset) != (3, 0, len(encoded), 0):
        raise ValueError("Unsupported attribute encoding.")
    pos += namesize
    dtype = header[pos:pos + typesize]
    space = header[pos + typesize:pos + typesize + spacesize]
    if len(dtype) < 8 or len(space) != spacesize:
        raise ValueError("Incomplete attribute.")
    size = int.from_bytes(dtype[4:8], "little")
    if integer:
        if (typesize != 12 or size not in (1, 2, 4, 8)
                or dtype[:4] not in (b"\x10\x00\x00\x00", b"\x10\x08\x00\x00")
                or dtype[8:] != struct.pack("<HH", 0, size * 8)
                or space not in (b"\x02\x00\x00\x00",
                                 b"\x02\x01\x01\x01" + struct.pack("<QQ", 1, 1))):
            raise ValueError("Unsupported integer attribute.")
    elif (typesize != 8 or dtype[:4] != b"\x13\x00\x00\x00"
          or not 1 <= size <= 128 or space != b"\x02\x00\x00\x00"):
        raise ValueError("Unsupported string attribute.")
    pos += typesize + spacesize
    data = header[pos:pos + size]
    if len(data) != size:
        raise ValueError("Truncated attribute data.")
    if integer:
        return int.from_bytes(data, "little", signed=bool(dtype[1] & 8))
    return data.rstrip(b"\0").decode("ascii")


def partial_product_name(header: bytes) -> str:
    """Recover exact HR Raster identity, including CRID and product counter."""
    if not header.startswith(b"\x89HDF\r\n\x1a\n") or header[8:11] not in (
        b"\x02\x08\x08", b"\x03\x08\x08"
    ):
        raise ValueError("Unsupported or missing HDF5 superblock.")
    descriptor = str(_attribute(header, "descriptor_string"))
    if not re.fullmatch(r"100m_UTM(?:0[1-9]|[1-5]\d|60)[C-HJ-NP-X]_[A-Z]_x_x_x", descriptor):
        raise ValueError("Not a supported HR Raster descriptor.")
    if _attribute(header, "short_name") != "L2_HR_Raster":
        raise ValueError("Not an HR Raster product.")
    crid = str(_attribute(header, "crid"))
    counter = str(_attribute(header, "product_version"))
    if not re.fullmatch(r"[A-Z0-9]{4}", crid) or not re.fullmatch(r"\d{2,}", counter):
        raise ValueError("Unrecognized product version.")
    numbers = [int(_attribute(header, name, integer=True)) for name in
               ("cycle_number", "pass_number", "scene_number")]
    if any(not 0 <= n <= 999 for n in numbers):
        raise ValueError("Invalid orbit/scene identity.")
    dates = []
    for name in ("time_granule_start", "time_granule_end"):
        value = str(_attribute(header, name))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
            raise ValueError("Invalid granule time.")
        dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
    if dates[1] < dates[0]:
        raise ValueError("Reversed granule times.")
    cycle, orbit, scene = numbers
    return (f"SWOT_L2_HR_Raster_{descriptor}_{cycle:03d}_{orbit:03d}_{scene:03d}F_"
            f"{dates[0]:%Y%m%dT%H%M%S}_{dates[1]:%Y%m%dT%H%M%S}_{crid}_{counter}.nc")


def _no_links(path: Path) -> None:
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Links and junctions are protected.")


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    _no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Only regular, non-hard-linked partial files are eligible.")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino


def _read_partial(path: Path) -> tuple[tuple[int, int, int, int, int], bytes]:
    if not PARTIAL_NAME.fullmatch(path.name):
        raise ValueError("Not an earthaccess partial filename.")
    before = _signature(path)
    if before[1] / 1e9 > time.time() - MIN_IDLE_SECONDS:
        raise ValueError("Partial file is too recent; retained for at least one hour.")
    with path.open("rb") as handle:
        header = handle.read(HEADER_LIMIT)
    if _signature(path) != before:
        raise ValueError("Partial file is changing.")
    return before, header


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _completion_times(
    download_manifest: Path, extract_manifest: Path, completed_statuses: set[str],
    names: set[str],
) -> dict[str, float]:
    """Require both download and extraction proof for the exact product version."""
    downloaded: dict[str, float] = {}
    for row in _evidence_rows(download_manifest, names):
        if row.get("downloaded", "").lower() == "yes":
            name = row.get("file_name", "")
            downloaded[name] = max(downloaded.get(name, 0), _timestamp(row.get("last_downloaded", "")))
    completed: dict[str, float] = {}
    for row in _evidence_rows(extract_manifest, names):
        name = Path(row.get("input_nc", "")).name
        if row.get("status", "").lower() in completed_statuses and name in downloaded:
            proof_time = min(downloaded[name], _timestamp(row.get("updated_at", "")))
            completed[name] = max(completed.get(name, 0), proof_time)
    return completed


def _evidence_rows(path: Path, names: set[str]) -> Iterable[Mapping[str, str]]:
    """Use the tile index and stream rows instead of loading project-wide history."""
    dataset = dataset_for_path(path)
    if dataset is None:
        yield from read_project_rows(path)
        return
    database = ProjectDatabase(database_path_for(path))
    database.ensure_imported(dataset, path)
    tiles = sorted({""} | {name.split("_")[5] for name in names})
    with database.session() as connection:
        rows = connection.execute(
            f"SELECT payload_json FROM records WHERE dataset=? AND utm_tile IN ({','.join('?' for _ in tiles)})",
            (dataset, *tiles),
        )
        for record in rows:
            row = json.loads(record[0])
            name = row.get("file_name") or Path(row.get("input_nc", "")).name
            if name in names:
                yield row


def _aliases(name: str) -> set[str]:
    canonical = name.removesuffix("_swot.nc") + ".nc" if name.endswith("_swot.nc") else name
    return {canonical, canonical[:-3] + "_swot.nc"}


def _prove(
    path: Path, download_manifest: Path, extract_manifest: Path, times: Mapping[str, float],
) -> PartialDownloadProof:
    signature, header = _read_partial(path)
    name = partial_product_name(header)
    aliases = [n for n in _aliases(name) if n in times]
    if len(aliases) != 1 or times[aliases[0]] <= signature[1] / 1e9:
        raise ValueError("No unambiguous download AND extraction proof after this partial transfer.")
    return PartialDownloadProof(path, aliases[0], download_manifest, extract_manifest,
                                signature, hashlib.sha256(header).hexdigest())


def plan_partial_downloads(
    folder: Path, download_manifest: Path, extract_manifest: Path, completed_statuses: set[str],
) -> list[PartialDownloadProof]:
    """Preview verified leftovers only; never infer completion from age or size."""
    folder = folder.absolute()
    if not folder.is_dir():
        return []
    paths = sorted(folder.glob("partial_*"))
    if not paths:
        return []
    proofs = []
    try:
        _no_links(folder)
        with download_folder_lock(folder):
            names: set[str] = set()
            readable = []
            for path in paths:
                try:
                    _, header = _read_partial(path)
                    names.update(_aliases(partial_product_name(header)))
                    readable.append(path)
                except (OSError, ValueError):
                    continue
            if not readable:
                return []
            times = _completion_times(download_manifest, extract_manifest, completed_statuses, names)
            for path in readable:
                try:
                    proofs.append(_prove(path, download_manifest, extract_manifest, times))
                except (OSError, ValueError):
                    continue
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # A busy folder or unavailable proof must never make a file eligible.
        return []
    return proofs


def delete_partial_downloads(
    proofs: Iterable[PartialDownloadProof], completed_statuses: set[str],
) -> tuple[int, int, list[str]]:
    """Revalidate preview, history and inactivity under the downloader's lock."""
    groups: dict[tuple[Path, Path, Path], list[PartialDownloadProof]] = defaultdict(list)
    for proof in proofs:
        groups[(proof.path.parent, proof.download_manifest, proof.extract_manifest)].append(proof)
    deleted, size, errors = 0, 0, []
    for (folder, download_manifest, extract_manifest), items in groups.items():
        try:
            _no_links(folder)
            with download_folder_lock(folder):
                names = set().union(*(_aliases(item.product_name) for item in items))
                times = _completion_times(download_manifest, extract_manifest, completed_statuses, names)
                audit = extract_manifest.parent / "partial_download_cleanup.jsonl"
                _no_links(audit.parent)
                if audit.exists() or audit.is_symlink():
                    _signature(audit)
                with audit.open("a", encoding="utf-8") as handle:
                    for item in items:
                        event = {"timestamp": datetime.now(timezone.utc).isoformat(),
                                 "path": str(item.path), "product_name": item.product_name,
                                 "size_bytes": item.signature[0],
                                 "download_manifest": str(download_manifest),
                                 "extract_manifest": str(extract_manifest)}
                        try:
                            if _prove(item.path, download_manifest, extract_manifest, times) != item:
                                raise ValueError("Partial file changed since preview; preview again.")
                            handle.write(json.dumps({**event, "status": "delete_requested"}) + "\n")
                            handle.flush()
                            os.fsync(handle.fileno())
                            item.path.unlink()
                        except (OSError, ValueError) as exc:
                            errors.append(f"{item.path}: {exc}")
                            event.update(status="retained", message=str(exc))
                        else:
                            deleted += 1
                            size += item.signature[0]
                            event.update(status="deleted")
                        handle.write(json.dumps(event) + "\n")
                        handle.flush()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            errors.append(f"{folder}: partial downloads retained: {exc}")
    return deleted, size, errors
