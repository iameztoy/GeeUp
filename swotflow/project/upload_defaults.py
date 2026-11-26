"""Per-user upload presets, applied only when creating a GUI project."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


def defaults_path() -> Path:
    """Keep personal destinations outside checkouts, projects and source releases."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    return base / "SWOTFlow" / "new_project_upload_defaults.json"


def upload_defaults_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only non-secret upload choices; never copy scope, paths or histories."""
    from swotflow.earthengine.cloud.config import CloudConfig
    from swotflow.earthengine.uploader import validate_ee_asset_path

    upload = config.get("upload", {})
    backend = upload.get("backend", "browser")
    if backend not in {"browser", "gcs"}:
        raise ValueError("Choose Browser or Google Cloud Storage for the new-project preset.")
    destination = str(config.get("destination_parent", "")).strip().rstrip("/")
    if not destination.startswith("projects/") or "/assets/" not in destination:
        raise ValueError("Enter the full destination ImageCollection ID before saving defaults.")
    validate_ee_asset_path(destination)
    cloud = CloudConfig.from_dict(config.get("cloud_upload", {}))
    if backend == "gcs":
        cloud.validate()
    completion = upload.get("completion_mode", "submit_and_continue")
    if completion not in {"wait_for_ee", "submit_and_continue"}:
        raise ValueError("Invalid upload completion mode in the new-project preset.")
    limit = upload.get("max_active_ingestions", 0)
    if type(limit) is not int or limit < 0:
        raise ValueError("Maximum active ingestions must be a nonnegative integer.")
    dry_run = config.get("execution", {}).get("dry_run", True)
    if type(dry_run) is not bool:
        raise ValueError("Dry run must be enabled or disabled.")
    policy = deepcopy(upload.get("pyramiding_policy") or {"default": None, "per_band": {}})
    allowed = {"MEAN", "SAMPLE", "MODE", "MIN", "MAX"}
    if not isinstance(policy, dict) or set(policy) - {"default", "per_band"}:
        raise ValueError("Invalid pyramiding settings in the new-project preset.")
    if policy.get("default") is not None and policy["default"] not in allowed:
        raise ValueError("Invalid default pyramiding policy.")
    bands = policy.get("per_band", {})
    if not isinstance(bands, dict) or set(bands) - {"b1", "b2"} or any(v not in allowed for v in bands.values()):
        raise ValueError("Invalid per-band pyramiding policy.")
    return {"destination_parent": destination, "cloud_upload": asdict(cloud),
            "upload": {"backend": backend, "completion_mode": completion,
                       "max_active_ingestions": limit, "pyramiding_policy": policy},
            "execution": {"dry_run": dry_run}}


def load_upload_defaults(path: Path | None = None) -> dict[str, Any] | None:
    """Load a personal preset, failing visibly rather than selecting a wrong target."""
    path = path or defaults_path()
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("settings"), dict):
            raise ValueError("Unsupported preset format")
        return upload_defaults_from_config(document["settings"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Cannot read new-project upload defaults at {path}. Save them again from Cloud Upload. {exc}") from exc


def save_upload_defaults(config: Mapping[str, Any], path: Path | None = None) -> Path:
    """Atomically save explicit personal choices without changing the open project."""
    settings = upload_defaults_from_config(config)
    path = path or defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="upload-defaults-", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump({"schema_version": 1, "settings": settings}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path
