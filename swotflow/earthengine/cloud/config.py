"""Validated, opt-in settings. Browser upload remains the default."""
from dataclasses import dataclass
import re

BACKENDS = {"Browser (existing uploader)": "browser", "Google Cloud Storage": "gcs"}

@dataclass(frozen=True)
class CloudConfig:
    project: str = ""
    bucket: str = ""
    prefix: str = "swotflow-staging"
    staging_limit_gib: float = 50.0
    delete_after_verification: bool = True
    chunk_mib: int = 16

    @classmethod
    def from_dict(cls, data):
        values = data or {}
        return cls(str(values.get("project", "")).strip(), str(values.get("bucket", "")).strip(),
                   str(values.get("prefix", "swotflow-staging")).strip().strip("/"),
                   float(values.get("staging_limit_gib", 50)), bool(values.get("delete_after_verification", True)),
                   int(values.get("chunk_mib", 16)))

    def validate(self):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", self.project):
            raise ValueError("Cloud Upload: enter the Google Cloud project ID (not its display name).")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", self.bucket):
            raise ValueError("Cloud Upload: enter a bucket name without gs:// or a folder path.")
        if not self.prefix or not re.fullmatch(r"[A-Za-z0-9/_-]+", self.prefix) or ".." in self.prefix:
            raise ValueError("Cloud Upload: use a dedicated, nonempty staging prefix.")
        if not 0 < self.staging_limit_gib <= 100000:
            raise ValueError("Cloud Upload: staged storage limit must be positive (GiB).")
        if not 1 <= self.chunk_mib <= 128:
            raise ValueError("Cloud Upload: chunk size must be between 1 and 128 MiB.")
