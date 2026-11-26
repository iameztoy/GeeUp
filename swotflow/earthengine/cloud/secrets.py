"""OS-user protected resumable URLs, separate from project/history exports."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path


def protect(data: bytes, decrypt=False):
    if os.name != "nt":
        return data  # POSIX secret file is mode 0600 inside a mode 0700 directory.
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("Could not protect/unprotect the resumable session for this OS user.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


class SessionSecrets:
    def __init__(self, project_key):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
        self.root = base / "SWOTFlow" / "cloud-sessions" / hashlib.sha256(str(project_key).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)
    def path(self, key):
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".session")
    def load(self, key):
        p = self.path(key)
        return protect(p.read_bytes(), True).decode() if p.exists() else ""
    def save(self, key, value):
        p = self.path(key)
        temp = p.with_suffix(".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(protect(value.encode()))
            stream.flush(); os.fsync(stream.fileno())
        temp.replace(p)
    def remove(self, key):
        self.path(key).unlink(missing_ok=True)
