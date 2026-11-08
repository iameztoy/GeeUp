"""Bounded hashes for one cleanup pass, invalidated by filesystem change metadata."""
from __future__ import annotations

from collections import OrderedDict
import os
from pathlib import Path


def content_signature(path: Path) -> tuple[int, ...] | None:
    """Identify an open file revision, including Windows' true change timestamp.

    Windows st_ctime is creation time, so size/mtime/ctime alone cannot detect a
    same-size rewrite followed by restoring mtime. Unsupported change metadata
    disables reuse; callers still hash the file normally.
    """
    with path.open("rb") as stream:
        state = os.fstat(stream.fileno())
        changed = state.st_ctime_ns
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            class FileBasicInfo(ctypes.Structure):
                _fields_ = [(name, ctypes.c_longlong) for name in
                            ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")] + [
                                ("FileAttributes", wintypes.DWORD)]

            query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
            query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            query.restype = wintypes.BOOL
            info = FileBasicInfo()
            if not query(msvcrt.get_osfhandle(stream.fileno()), 0, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            changed = info.ChangeTime
            if not changed:
                return None
        return (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns,
                state.st_ctime_ns, changed)


class CleanupHashCache:
    """Reuse hashes only within one preview/deletion batch; keep at most 128 files.

    Every lookup checks file identity and change metadata. Sidecar membership is
    rescanned by auxiliary_hashes, and proof/inventory validation remains outside
    this byte cache. Never serialize or share this cache between operations.
    """

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("Hash cache must retain at least one entry")
        self.max_entries = max_entries
        self.entries: OrderedDict[tuple[str, str], tuple[tuple[int, ...], str]] = OrderedDict()

    def hash(self, path: Path, algorithm: str = "sha256") -> str:
        """Hash a changed file or reuse its exact unchanged revision's digest."""
        from swotflow.verification.pipeline import file_hash
        key = (str(path.resolve()), algorithm)
        before = content_signature(path)
        previous = self.entries.pop(key, None)
        if before is not None and previous is not None and previous[0] == before:
            self.entries[key] = previous
            return previous[1]
        value = file_hash(path, algorithm)
        after = content_signature(path)
        if before != after:
            raise ValueError(f"File changed during verification: {path.name}")
        if after is not None:
            self.entries[key] = (after, value)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
        return value
