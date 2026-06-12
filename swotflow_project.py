"""Project and tile-preset helpers for SWOTFlow workflows."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from project_database import DATABASE_FILE_NAME, ProjectDatabase, migrate_project_csvs
from swot_download_tool import normalize_utm_tiles


PROJECT_FILE_NAME = "project.yaml"
PROJECT_SCHEMA_VERSION = 2
PRESET_SCHEMA_VERSION = 1
BUILTIN_PRESET_PATH = Path(__file__).resolve().parent / "spatial_presets" / "continent_utm_tiles.json"
PROJECT_FOLDERS = {
    "raw_downloads": "01_raw_downloads",
    "extracted_geotiffs": "02_extracted_geotiffs",
    "mosaics": "03_mosaics",
    "logs": "00_logs",
    "upload_artifacts": "00_logs/upload_artifacts",
    "profiles": "profiles",
}


@dataclass
class SWOTFlowProject:
    """A saved SWOTFlow project rooted at one local folder."""

    name: str
    root: Path
    config: Dict[str, Any] = field(default_factory=dict)
    download_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE_NAME


@dataclass
class TilePreset:
    """Reusable group of UTM tile tokens."""

    name: str
    tiles: List[str]
    description: str = ""
    source: str = "project"
    path: Optional[Path] = None


def now_iso() -> str:
    """Return a seconds-resolution UTC-ish timestamp for YAML/JSON metadata."""
    return datetime.now().replace(microsecond=0).isoformat()


def slugify_name(value: str, fallback: str = "project") -> str:
    """Return a stable filesystem-safe slug for project/profile names."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return slug or fallback


def project_file_path(path: str | Path) -> Path:
    """Return the project.yaml path for a project root or project file path."""
    candidate = Path(path)
    return candidate if candidate.name == PROJECT_FILE_NAME else candidate / PROJECT_FILE_NAME


def project_paths(root: str | Path) -> Dict[str, Path]:
    """Return the canonical folders for one SWOTFlow project root."""
    root_path = Path(root)
    return {key: root_path / folder for key, folder in PROJECT_FOLDERS.items()}


def ensure_project_structure(root: str | Path) -> Dict[str, Path]:
    """Create the canonical project folders and return their paths."""
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    paths = project_paths(root_path)
    for folder in paths.values():
        folder.mkdir(parents=True, exist_ok=True)
    return paths


def _as_text(path: Path) -> str:
    return str(path)


def config_for_project(base_config: Mapping[str, Any], root: str | Path) -> Dict[str, Any]:
    """Return a copy of a config with processing paths pointed at a project root."""
    config = deepcopy(dict(base_config))
    root_path = Path(root)
    paths = project_paths(root_path)
    raw = _as_text(paths["raw_downloads"])
    extracted = _as_text(paths["extracted_geotiffs"])
    mosaics = _as_text(paths["mosaics"])
    logs = _as_text(paths["logs"])

    processing = dict(config.get("processing", {}))
    processing.update(
        {
            "root": _as_text(root_path),
            "database": _as_text(root_path / DATABASE_FILE_NAME),
            "raw_downloads": raw,
            "extracted_geotiffs": extracted,
            "mosaics": mosaics,
            "logs": logs,
        }
    )
    config["processing"] = processing

    download = dict(config.get("download", {}))
    download.setdefault("collection_short_name", "SWOT_L2_HR_Raster_100m_D")
    download.setdefault("collection_version_label", "Version D active")
    download["output_folder"] = raw
    download["report_csv"] = str(paths["logs"] / "download_preview.csv")
    download["manifest_csv"] = str(paths["logs"] / "download_manifest.csv")
    config["download"] = download

    duplicates = dict(config.get("duplicates", {}))
    duplicates["input_folder"] = raw
    duplicates["log_folder"] = logs
    config["duplicates"] = duplicates

    extract = dict(config.get("extract", {}))
    extract["input_folder"] = raw
    extract["output_folder"] = extracted
    extract["manifest_csv"] = str(paths["logs"] / "extract_manifest.csv")
    extract["errors_csv"] = str(paths["logs"] / "extract_errors.csv")
    config["extract"] = extract

    mosaic = dict(config.get("mosaic", {}))
    mosaic["input_folder"] = extracted
    mosaic["output_folder"] = mosaics
    mosaic["report_csv"] = str(paths["logs"] / "mosaic_report.csv")
    mosaic["manifest_csv"] = str(paths["logs"] / "mosaic_manifest.csv")
    mosaic["mixed_crid_report_csv"] = str(paths["logs"] / "mixed_crid_mosaics.csv")
    config["mosaic"] = mosaic

    artifacts = dict(config.get("artifacts", {}))
    artifacts["logs_dir"] = logs
    artifacts["artifacts_dir"] = str(paths["upload_artifacts"])
    artifacts["report_csv"] = str(paths["logs"] / "upload_report.csv")
    artifacts["ee_asset_inventory_csv"] = str(paths["logs"] / "ee_asset_inventory.csv")
    config["artifacts"] = artifacts

    config["input_folder"] = mosaics
    return config


def save_project(project: SWOTFlowProject) -> Path:
    """Write a SWOTFlow project YAML file."""
    ensure_project_structure(project.root)
    created_at = project.created_at or now_iso()
    updated_at = now_iso()
    document = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "project": {
            "name": project.name,
            "root": str(project.root),
            "created_at": created_at,
            "updated_at": updated_at,
        },
        "config": project.config,
        "download_history": project.download_history,
    }
    path = project.project_file
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False, allow_unicode=False)
    project.created_at = created_at
    project.updated_at = updated_at
    return path


def create_project(root: str | Path, name: str, base_config: Mapping[str, Any]) -> SWOTFlowProject:
    """Create a project structure and initial project.yaml from a base config."""
    root_path = Path(root)
    ensure_project_structure(root_path)
    project = SWOTFlowProject(
        name=name.strip() or root_path.name or "SWOTFlow Project",
        root=root_path,
        config=config_for_project(base_config, root_path),
        download_history=[],
        created_at=now_iso(),
    )
    ProjectDatabase(root_path / DATABASE_FILE_NAME)
    save_project(project)
    return project


def load_project(path: str | Path) -> SWOTFlowProject:
    """Load a project from a project root or project.yaml path."""
    project_file = project_file_path(path)
    with project_file.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    project_data = document.get("project", {})
    root = Path(project_data.get("root") or project_file.parent)
    if not root.is_absolute():
        root = (project_file.parent / root).resolve()
    if not root.exists() and project_file.parent.exists():
        root = project_file.parent
    project = SWOTFlowProject(
        name=str(project_data.get("name") or root.name or "SWOTFlow Project"),
        root=root,
        config=document.get("config", {}) or {},
        download_history=list(document.get("download_history", []) or []),
        created_at=str(project_data.get("created_at") or ""),
        updated_at=str(project_data.get("updated_at") or ""),
    )
    ensure_project_structure(project.root)
    processing = dict(project.config.get("processing", {}))
    processing.setdefault("database", str(project.root / DATABASE_FILE_NAME))
    project.config["processing"] = processing
    migrate_project_csvs(project.root)
    return project


def save_project_config(
    root: str | Path,
    name: str,
    config: Mapping[str, Any],
    download_history: Iterable[Mapping[str, Any]] | None = None,
    created_at: str = "",
) -> Path:
    """Save current config/history into an existing or new project root."""
    project = SWOTFlowProject(
        name=name.strip() or Path(root).name or "SWOTFlow Project",
        root=Path(root),
        config=deepcopy(dict(config)),
        download_history=[dict(item) for item in (download_history or [])],
        created_at=created_at,
    )
    return save_project(project)


def load_builtin_tile_presets(path: str | Path = BUILTIN_PRESET_PATH) -> Dict[str, TilePreset]:
    """Load built-in tile presets from JSON, if present."""
    preset_path = Path(path)
    if not preset_path.exists():
        return {}
    with preset_path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    presets: Dict[str, TilePreset] = {}
    for name, raw_preset in (document.get("presets", {}) or {}).items():
        tiles = normalize_utm_tiles(raw_preset.get("tiles", []))
        preset_name = str(raw_preset.get("name") or name)
        presets[preset_name] = TilePreset(
            name=preset_name,
            tiles=tiles,
            description=str(raw_preset.get("description", "")),
            source=str(raw_preset.get("source", "built-in")),
            path=preset_path,
        )
    return presets


def profile_path(project_root: str | Path, name: str) -> Path:
    """Return the JSON path for one project tile profile."""
    return project_paths(project_root)["profiles"] / f"{slugify_name(name, 'tile_profile')}.json"


def save_tile_profile(
    project_root: str | Path,
    name: str,
    tiles: Iterable[str],
    description: str = "",
) -> Path:
    """Save a named tile profile under a project's profiles folder."""
    normalized_tiles = normalize_utm_tiles(list(tiles))
    if not normalized_tiles:
        raise ValueError("Cannot save an empty tile preset.")
    path = profile_path(project_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "name": name.strip() or path.stem,
        "description": description,
        "source": "project",
        "updated_at": now_iso(),
        "tiles": normalized_tiles,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    return path


def load_project_tile_profiles(project_root: str | Path) -> Dict[str, TilePreset]:
    """Load all saved tile profiles for one project."""
    profiles_dir = project_paths(project_root)["profiles"]
    presets: Dict[str, TilePreset] = {}
    if not profiles_dir.exists():
        return presets
    for path in sorted(profiles_dir.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            name = str(document.get("name") or path.stem)
            presets[name] = TilePreset(
                name=name,
                tiles=normalize_utm_tiles(document.get("tiles", [])),
                description=str(document.get("description", "")),
                source=str(document.get("source", "project")),
                path=path,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return presets


def append_download_history(project_root: str | Path, entry: Mapping[str, Any]) -> Path:
    """Append a download history row to project.yaml."""
    project = load_project(project_root)
    project.download_history.append(dict(entry))
    return save_project(project)


def successful_download_history(history: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Return download history rows that completed without failed files."""
    return [
        row
        for row in history
        if str(row.get("status", "")).lower() in {"success", "completed"}
        and int(row.get("failed_count", 0) or 0) == 0
        and row.get("end_date")
    ]


def last_successful_download_end(history: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """Return the latest end date from successful download history."""
    successful = successful_download_history(history)
    if not successful:
        return None
    return max(str(row["end_date"]) for row in successful)


def prepare_update_dates(
    history: Iterable[Mapping[str, Any]],
    today: date | None = None,
) -> Tuple[Optional[str], str]:
    """Return inclusive update start date and end date from project history."""
    end = (today or date.today()).isoformat()
    return last_successful_download_end(history), end
