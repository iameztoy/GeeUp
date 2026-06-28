"""Move older duplicate SWOT file versions out of the active input folder."""

from __future__ import annotations

import argparse
import re
import shutil
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from swot_metadata import (
    ParsedMetadata,
    parse_swot_l2_hr_raster_metadata,
    product_identity_parts,
    swot_product_rank,
)
from swot_download_tool import normalize_utm_tiles


DEFAULT_PROCESSING_ROOT = "./SWOT_Processing"
DEFAULT_PROCESSING_PATHS = {
    "root": DEFAULT_PROCESSING_ROOT,
    "raw_downloads": f"{DEFAULT_PROCESSING_ROOT}/01_raw_downloads",
    "extracted_geotiffs": f"{DEFAULT_PROCESSING_ROOT}/02_extracted_geotiffs",
    "mosaics": f"{DEFAULT_PROCESSING_ROOT}/03_mosaics",
    "logs": f"{DEFAULT_PROCESSING_ROOT}/00_logs",
}
DEFAULT_CONFIG: Dict[str, Any] = {
    "processing": DEFAULT_PROCESSING_PATHS,
    "duplicates": {
        "input_folder": DEFAULT_PROCESSING_PATHS["raw_downloads"],
        "moved_folder_name": "moved",
        "log_folder": DEFAULT_PROCESSING_PATHS["logs"],
        "recursive": False,
        "utm_tiles": [],
    },
}

VERSION_RE = re.compile(r"^(?P<core>.+)_(?P<version>\d{2,})$")


@dataclass(frozen=True)
class CandidateFile:
    """One file whose stem ends in a numeric SWOT version suffix."""

    path: Path
    core: str
    version: int
    extension: str
    relative_parent: Path = Path(".")
    metadata: Optional[ParsedMetadata] = None


@dataclass(frozen=True)
class DuplicateAction:
    """One planned move from an older duplicate version to a moved folder."""

    source: Path
    destination: Path
    kept: Path
    core: str
    extension: str
    moved_version: int
    kept_version: int
    moved_crid: str = ""
    moved_counter: str = ""
    kept_crid: str = ""
    kept_counter: str = ""
    reason: str = ""


@dataclass
class DuplicatePlan:
    """Duplicate scan result before optional filesystem changes."""

    actions: List[DuplicateAction] = field(default_factory=list)
    kept_files: List[Path] = field(default_factory=list)
    unmatched_files: List[Path] = field(default_factory=list)
    candidate_count: int = 0
    duplicate_group_count: int = 0


@dataclass
class DuplicateConfig:
    """Runtime settings for duplicate removal."""

    input_folder: Path
    moved_folder_name: str = "moved"
    log_folder: Path = Path(DEFAULT_PROCESSING_PATHS["logs"])
    recursive: bool = False
    utm_tiles: List[str] = field(default_factory=list)
    base_dir: Path = Path.cwd()


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge two dictionaries."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_path(value: str | Path | None, base_dir: Path) -> Path:
    """Resolve a path against the config file directory."""
    if value in (None, ""):
        raise ValueError("A required duplicate-removal path value was empty.")
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def load_config_file(config_path: Path) -> DuplicateConfig:
    """Load duplicate-removal settings from YAML config."""
    with config_path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    merged = deep_merge(DEFAULT_CONFIG, user_config)
    return parse_config(merged, config_path.parent.resolve())


def parse_config(data: Dict[str, Any], base_dir: Path) -> DuplicateConfig:
    """Convert raw config data into a DuplicateConfig."""
    duplicate_data = data.get("duplicates", {})
    processing_data = data.get("processing", {})
    input_folder = (
        duplicate_data.get("input_folder")
        or processing_data.get("raw_downloads")
        or DEFAULT_PROCESSING_PATHS["raw_downloads"]
    )
    log_folder = (
        duplicate_data.get("log_folder")
        or processing_data.get("logs")
        or DEFAULT_PROCESSING_PATHS["logs"]
    )
    config = DuplicateConfig(
        input_folder=resolve_path(input_folder, base_dir),
        moved_folder_name=str(duplicate_data.get("moved_folder_name", "moved")).strip(),
        log_folder=resolve_path(log_folder, base_dir),
        recursive=bool(duplicate_data.get("recursive", False)),
        utm_tiles=normalize_utm_tiles(duplicate_data.get("utm_tiles", [])),
        base_dir=base_dir,
    )
    validate_config(config)
    return config


def validate_config(config: DuplicateConfig) -> None:
    """Raise ValueError when duplicate-removal settings are unsafe."""
    if not config.moved_folder_name:
        raise ValueError("duplicates.moved_folder_name cannot be empty.")
    moved_name_path = Path(config.moved_folder_name)
    if moved_name_path.name != config.moved_folder_name or config.moved_folder_name in {".", ".."}:
        raise ValueError("duplicates.moved_folder_name must be a folder name, not a path.")
    if config.input_folder.exists() and not config.input_folder.is_dir():
        raise ValueError(f"Duplicate input path is not a directory: {config.input_folder}")
    if config.log_folder.exists() and not config.log_folder.is_dir():
        raise ValueError(f"Duplicate log path is not a directory: {config.log_folder}")


def split_filename(file_path: Path) -> Optional[CandidateFile]:
    """Parse a final `_NN` suffix from a file name, preserving full extension text."""
    suffixes = "".join(file_path.suffixes)
    stem_without_suffixes = file_path.name[: -len(suffixes)] if suffixes else file_path.name
    match = VERSION_RE.match(stem_without_suffixes)
    if not match:
        return None
    return CandidateFile(
        path=file_path,
        core=match.group("core"),
        version=int(match.group("version")),
        extension=suffixes,
    )


def is_relative_to(path: Path, parent: Path) -> bool:
    """Return True when path is inside parent, compatible with older Python APIs."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def collect_files(config: DuplicateConfig) -> Tuple[List[CandidateFile], List[Path]]:
    """Return duplicate candidates and unmatched files from the configured folder."""
    input_folder = config.input_folder.resolve()
    if not input_folder.exists():
        return [], []

    globber = input_folder.rglob if config.recursive else input_folder.glob
    candidates: List[CandidateFile] = []
    unmatched: List[Path] = []
    selected_tiles = set(config.utm_tiles)
    for path in sorted(globber("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        relative_parent = resolved.parent.relative_to(input_folder)
        if config.moved_folder_name in relative_parent.parts:
            continue
        candidate = split_filename(resolved)
        if candidate is None:
            if not config.utm_tiles:
                unmatched.append(resolved)
            continue
        try:
            metadata = parse_swot_l2_hr_raster_metadata(resolved)
        except ValueError:
            metadata = None
        if selected_tiles:
            tile = ""
            if metadata is not None:
                tile = str(metadata.fields.get("coordinate_system", "") or "").upper()
            if tile not in selected_tiles:
                continue
        candidates.append(
            CandidateFile(
                path=resolved,
                core=candidate.core,
                version=candidate.version,
                extension=candidate.extension,
                relative_parent=relative_parent if relative_parent.parts else Path("."),
                metadata=metadata,
            )
        )
    return candidates, unmatched


def swot_granule_key(candidate: CandidateFile) -> Optional[Tuple[str, ...]]:
    """Return a duplicate key that ignores only CRID and product counter."""
    if candidate.metadata is None:
        return None
    parts = product_identity_parts(candidate.path)
    if parts is None:
        return None
    return (
        "swot",
        candidate.relative_parent.as_posix(),
        candidate.extension.lower(),
        *parts,
    )


def group_candidates(candidates: Iterable[CandidateFile]) -> Dict[Tuple[str, ...], List[CandidateFile]]:
    """Group files that differ only by final numeric version suffix."""
    groups: Dict[Tuple[str, ...], List[CandidateFile]] = {}
    for candidate in candidates:
        key = swot_granule_key(candidate)
        if key is None:
            key = (
                "legacy",
                candidate.relative_parent.as_posix(),
                candidate.core,
                candidate.extension.lower(),
            )
        groups.setdefault(key, []).append(candidate)
    return groups


def candidate_sort_key(candidate: CandidateFile) -> Tuple[int, int, int, int, int, str]:
    """Return the version preference key for one duplicate candidate."""
    if candidate.metadata is None:
        return (0, -1, -1, -1, candidate.version, candidate.path.name.lower())
    fields = candidate.metadata.fields
    return (
        *swot_product_rank(fields.get("crid", ""), fields.get("product_counter", "")),
        candidate.path.name.lower(),
    )


def candidate_crid(candidate: CandidateFile) -> str:
    """Return the parsed CRID for log output."""
    return "" if candidate.metadata is None else candidate.metadata.fields.get("crid", "")


def candidate_counter(candidate: CandidateFile) -> str:
    """Return the parsed product counter for log output."""
    return "" if candidate.metadata is None else candidate.metadata.fields.get("product_counter", "")


def duplicate_reason(moved: CandidateFile, kept: CandidateFile) -> str:
    """Explain why one candidate was moved."""
    if moved.metadata is None or kept.metadata is None:
        return "higher final numeric suffix"
    if candidate_crid(moved) != candidate_crid(kept):
        if candidate_counter(moved) != candidate_counter(kept):
            return "preferred CRID and product counter"
        return "preferred CRID"
    if candidate_counter(moved) != candidate_counter(kept):
        return "higher product counter"
    return "same SWOT granule; deterministic filename tie-break"


def unique_destination(path: Path) -> Path:
    """Return a destination path that does not overwrite an existing moved file."""
    if not path.exists():
        return path
    suffixes = "".join(path.suffixes)
    stem_without_suffixes = path.name[: -len(suffixes)] if suffixes else path.name
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem_without_suffixes}__moved{index}{suffixes}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free moved-file name for {path}")


def build_duplicate_plan(config: DuplicateConfig) -> DuplicatePlan:
    """Plan duplicate moves without changing the filesystem."""
    candidates, unmatched = collect_files(config)
    groups = group_candidates(candidates)
    plan = DuplicatePlan(
        unmatched_files=unmatched,
        candidate_count=len(candidates),
    )

    for key, files in sorted(groups.items()):
        if len(files) < 2:
            continue
        ordered = sorted(files, key=candidate_sort_key)
        kept = ordered[-1]
        plan.duplicate_group_count += 1
        plan.kept_files.append(kept.path)
        for old_file in ordered[:-1]:
            moved_dir = old_file.path.parent / config.moved_folder_name
            destination = unique_destination(moved_dir / old_file.path.name)
            plan.actions.append(
                DuplicateAction(
                    source=old_file.path,
                    destination=destination,
                    kept=kept.path,
                    core=old_file.core if key[0] == "legacy" else "|".join(key[3:]),
                    extension=old_file.extension,
                    moved_version=old_file.version,
                    kept_version=kept.version,
                    moved_crid=candidate_crid(old_file),
                    moved_counter=candidate_counter(old_file),
                    kept_crid=candidate_crid(kept),
                    kept_counter=candidate_counter(kept),
                    reason=duplicate_reason(old_file, kept),
                )
            )
    return plan


def ensure_parent_folders(config: DuplicateConfig) -> None:
    """Create safe default folders needed by duplicate removal."""
    config.input_folder.mkdir(parents=True, exist_ok=True)
    config.log_folder.mkdir(parents=True, exist_ok=True)


def move_duplicates(config: DuplicateConfig, dry_run: bool = False) -> Tuple[int, DuplicatePlan, Optional[Path]]:
    """Plan and optionally move older duplicate file versions."""
    if not dry_run:
        ensure_parent_folders(config)
    plan = build_duplicate_plan(config)
    log_path: Optional[Path] = None
    if not dry_run:
        for action in plan.actions:
            action.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(action.source), str(action.destination))
        log_path = write_log(config, plan)
    return 0, plan, log_path


def write_log(config: DuplicateConfig, plan: DuplicatePlan) -> Path:
    """Write a timestamped text log listing kept and moved files."""
    config.log_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = config.log_folder / f"swot_duplicate_removal_{timestamp}.txt"
    lines = [
        "SWOT duplicate removal log",
        f"Run time: {datetime.now().isoformat(timespec='seconds')}",
        f"Input folder: {config.input_folder}",
        f"Moved folder name: {config.moved_folder_name}",
        f"Recursive scan: {config.recursive}",
        f"Candidate files: {plan.candidate_count}",
        f"Unmatched files: {len(plan.unmatched_files)}",
        f"Duplicate groups: {plan.duplicate_group_count}",
        f"Files moved: {len(plan.actions)}",
        "",
        "Kept files:",
    ]
    lines.extend(f"KEPT: {path}" for path in sorted(plan.kept_files))
    if not plan.kept_files:
        lines.append("None")
    lines.extend(["", "Moved files:"])
    for action in plan.actions:
        lines.append(f"MOVED: {action.source}")
        lines.append(f"TO: {action.destination}")
        lines.append(f"KEPT: {action.kept}")
        if action.reason:
            lines.append(f"REASON: {action.reason}")
        if action.kept_crid or action.moved_crid:
            lines.append(f"moved_crid: {action.moved_crid}")
            lines.append(f"moved_counter: {action.moved_counter}")
            lines.append(f"kept_crid: {action.kept_crid}")
            lines.append(f"kept_counter: {action.kept_counter}")
        lines.append("")
    if not plan.actions:
        lines.append("None")
    lines.extend(["", "Unmatched files:"])
    lines.extend(f"UNMATCHED: {path}" for path in sorted(plan.unmatched_files))
    if not plan.unmatched_files:
        lines.append("None")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def summarize_plan(plan: DuplicatePlan) -> Dict[str, int]:
    """Return high-level counts for CLI and GUI reporting."""
    return {
        "candidate_files": plan.candidate_count,
        "unmatched_files": len(plan.unmatched_files),
        "duplicate_groups": plan.duplicate_group_count,
        "files_to_move": len(plan.actions),
        "kept_files": len(plan.kept_files),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Move older duplicate SWOT file versions into a moved subfolder."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        type=Path,
        help="Optional input folder override. Otherwise duplicates.input_folder is used.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml. Defaults to ./config.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report planned moves; do not move files or write a log.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Override config and scan subfolders recursively.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = build_arg_parser().parse_args(argv)
    config = load_config_file(args.config.resolve())
    if args.folder is not None:
        config.input_folder = args.folder.resolve()
    if args.recursive:
        config.recursive = True
    exit_code, plan, log_path = move_duplicates(config, dry_run=args.dry_run)
    mode = "dry run" if args.dry_run else "duplicate removal run"
    print(
        textwrap.dedent(
            f"""
            SWOT {mode} complete.
            Input folder: {config.input_folder}
            Moved folder name: {config.moved_folder_name}
            Log path: {log_path or 'not written during dry run'}
            Status counts: {summarize_plan(plan)}
            """
        ).strip()
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
