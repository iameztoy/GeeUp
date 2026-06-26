"""PIXC-specific project helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_FILE_NAME = "project.yaml"
PRODUCT_FAMILY = "pixc"
PROJECT_SCHEMA_VERSION = 1
DEFAULT_PIXC_PROJECT_PARENT = Path("D:/SWOTFlow_Projects")

PROJECT_FOLDERS = {
    "raw_downloads": "01_raw_downloads",
    "logs": "00_logs",
    "inspection": "00_logs/inspection",
    "processed_points": "02_processed_points",
    "qa": "03_qa",
}


@dataclass
class PixcProject:
    """A saved PIXC project rooted at one local folder."""

    name: str
    root: Path
    settings: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def project_file(self) -> Path:
        """Return the project.yaml path."""
        return self.root / PROJECT_FILE_NAME


def now_iso() -> str:
    """Return a seconds-resolution timestamp for project metadata."""
    return datetime.now().replace(microsecond=0).isoformat()


def pixc_project_paths(root: str | Path) -> dict[str, Path]:
    """Return canonical folders/files for one PIXC project root."""
    root_path = Path(root)
    paths = {key: root_path / folder for key, folder in PROJECT_FOLDERS.items()}
    paths["project_file"] = root_path / PROJECT_FILE_NAME
    paths["download_report"] = paths["logs"] / "pixc_download_preview.csv"
    paths["download_manifest"] = paths["logs"] / "pixc_download_manifest.csv"
    paths["download_events"] = paths["logs"] / "pixc_download_events.csv"
    paths["reference_imagery_log"] = paths["logs"] / "pixc_reference_imagery.csv"
    return paths


def ensure_pixc_project_structure(root: str | Path) -> dict[str, Path]:
    """Create canonical PIXC project folders and return their paths."""
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    paths = pixc_project_paths(root_path)
    for key, path in paths.items():
        if key == "project_file" or path.suffix:
            continue
        path.mkdir(parents=True, exist_ok=True)
    return paths


def pixc_project_file_path(path: str | Path) -> Path:
    """Return the project.yaml path for a PIXC project root or file."""
    candidate = Path(path)
    return candidate if candidate.name == PROJECT_FILE_NAME else candidate / PROJECT_FILE_NAME


def save_pixc_project(project: PixcProject) -> Path:
    """Write a PIXC project YAML file."""
    ensure_pixc_project_structure(project.root)
    created_at = project.created_at or now_iso()
    updated_at = now_iso()
    document = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "project": {
            "name": project.name,
            "root": str(project.root),
            "product_family": PRODUCT_FAMILY,
            "created_at": created_at,
            "updated_at": updated_at,
        },
        "settings": deepcopy(project.settings),
    }
    with project.project_file.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False, allow_unicode=False)
    project.created_at = created_at
    project.updated_at = updated_at
    return project.project_file


def create_pixc_project(
    root: str | Path,
    name: str,
    settings: Mapping[str, Any] | None = None,
) -> PixcProject:
    """Create a PIXC project structure and initial project.yaml."""
    root_path = Path(root)
    project = PixcProject(
        name=name.strip() or root_path.name or "PIXC Project",
        root=root_path,
        settings=deepcopy(dict(settings or {})),
        created_at=now_iso(),
    )
    save_pixc_project(project)
    return project


def load_pixc_project(path: str | Path) -> PixcProject:
    """Load a PIXC project from a project root or project.yaml path."""
    project_file = pixc_project_file_path(path)
    with project_file.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    family = str(
        document.get("product_family")
        or (document.get("project", {}) or {}).get("product_family")
        or ""
    ).strip().lower()
    if family != PRODUCT_FAMILY:
        raise ValueError("This project.yaml is not a PIXC project.")

    project_data = document.get("project", {}) or {}
    root = Path(project_data.get("root") or project_file.parent)
    if not root.is_absolute():
        root = (project_file.parent / root).resolve()
    if not root.exists() and project_file.parent.exists():
        root = project_file.parent
    project = PixcProject(
        name=str(project_data.get("name") or root.name or "PIXC Project"),
        root=root,
        settings=deepcopy(dict(document.get("settings", {}) or {})),
        created_at=str(project_data.get("created_at") or ""),
        updated_at=str(project_data.get("updated_at") or ""),
    )
    ensure_pixc_project_structure(project.root)
    return project
