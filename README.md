# SWOTFlow

Current application version: **v0.8.0** (25 October 2026). The Home page and application window title display this version on the next normal launch.

New in v0.8.0: organize implementation modules into the `swotflow/` package by function. The existing `python swotflow_gui.py` launcher remains; `python -m swotflow` also launches the GUI. Worker commands and resource paths use the new package layout. The v0.7.3 processing, version-selection, verification and cleanup rules are unchanged. See [development layout and command migration](DEVELOPMENT.md).

New in v0.7.1: durable download-to-mosaic evidence, exact-input reconciliation and a repair queue in Statistics, guarded resume/cleanup, and manually launched original-source and EE pixel checks. Paired mosaics now preserve both no-data masks in a standalone GeoTIFF. See [the verification guide](WORKFLOW_VERIFICATION.md) and [rebuild gates](WORKFLOW_VALIDATION.md). A small acceptance pilot is required before choosing a large rebuild scope.

Added in v0.7.0: verified dual-catalogue downloads with exact NASA checksums, mandatory history in verified mode, reset-project protection, and paired lowest-quality local mosaics. New projects retain source files during validation.

Added in v0.6.1: application source and regression tests are separated from local investigations. New consolidation histories use the user data folder; existing histories remain discoverable. See [development and storage rules](DEVELOPMENT.md).

Added in v0.6.0: **Statistics → NASA History** preserves catalogue filenames, processing versions and query history, and checks historical dates for versions newer than the inputs recorded in EE. It includes transfer-batch checkpoints and a read-only legacy-history import. Existing runs keep their behaviour; future automatic recording is enabled explicitly. See [NASA history and version checks](NASA_HISTORY.md) and [release history](CHANGELOG.md).

New: **Statistics → Consolidation** and **Status Map → Consolidation Coverage** use a separate local replacement register, with original-observation duplicate checks and explicit comparison/approval records. No cloud deletion or automatic analysis switch. See [the replacement register guide](EE_REPLACEMENT_REGISTER.md).

![SWOTFlow workflow banner](assets/swotflow_home_banner.png)

SWOTFlow is a local desktop workflow for SWOT L2 HR Raster 100 m data. It helps you download SWOT raster NetCDF files from NASA Earthdata / PO.DAAC, clean repeated product versions, extract GeoTIFFs, optionally build mosaics, upload outputs to Google Earth Engine, and audit the project afterwards.

The main entry point is:

```powershell
python swotflow_gui.py
```

## Versioning

Use all three components of `MAJOR.MINOR.PATCH`, with changes grouped into releases rather than a version bump for every edit:

- **Patch**, for example `0.5.0` → `0.5.1`: compatible bug fixes, small UI adjustments such as tab ordering, and maintenance without new functionality.
- **Minor**, for example `0.4.x` → `0.5.0`: new functionality, resetting the patch number. The replacement register and consolidation coverage justified the `0.5.0` increment.
- **Major**, once a stable `1.0.0` contract exists: incompatible changes to supported behaviour or data formats. During `0.x` development, incompatible changes require a minor increment and explicit compatibility/migration notes; they must not be hidden in a patch release.

For each release, keep the Home-page version, README and relevant release documentation consistent, include `vMAJOR.MINOR.PATCH` in both the GitHub release title and description, describe the changes and validation, and preserve earlier release history. Documentation-only edits need not increment the application version. Do not restart an ongoing processing run merely to display a new version.

## What SWOTFlow Does

- **Projects:** one folder per AOI or workflow, with project-specific raw downloads, extracted GeoTIFFs, mosaics, logs, presets, and settings.
- **Project relocation:** validate and rebase a copied project's paths while preserving its processing history. See [moving a project](./GETTING_STARTED.md#move-a-project-to-another-computer).
- **Home:** a visual landing tab with project status, workflow shortcuts, GitHub access, and selected-tile summary.
- **Automation:** runs the project workflow tile by tile after a required preflight, with resumable manifests and verified-stage cleanup.
- **Download:** searches and downloads PO.DAAC SWOT L2 HR Raster 100 m data through `earthaccess`, with date and UTM tile filtering.
- **Duplicate Removal:** moves older local raw granule versions when several CRID/product-counter versions exist.
- **Extraction:** converts SWOT NetCDF files into two-band GeoTIFFs with `wse` and `wse_qual`, with optional worker-based parallelism.
- **Mosaic:** reduces GeoTIFF counts before upload, while keeping SWOT-compatible naming and metadata parsing, with optional cautious group parallelism.
- **Upload:** uploads GeoTIFFs to Google Earth Engine through Chrome/Selenium, with optional UTM/source-tile filtering and Earth Engine asset verification.
- **Statistics:** summarizes project coverage, saved update windows, processing status, file counts, dates, UTM tiles, uploads, and QA tables. See [returning to an update](./GETTING_STARTED.md#return-to-an-unfinished-or-mistaken-update).
- **EE Consolidation:** previews asset savings, creates date/pass/grid mosaics, and extends existing consolidated mosaics with later inputs while preserving paired elevation/quality and processing history. Exports create new revisions; originals remain. See [consolidation and incremental updates](./EE_CONSOLIDATION.md).
- **Cleanup:** previews intermediate files with downstream proof, plus optional [old uploader diagnostics](./GETTING_STARTED.md#optional-diagnostic-cleanup), with space estimates and file selection.

## Basic Workflow

1. Create or open a SWOTFlow project.
2. In **Download**, authenticate with Earthdata, choose dates and UTM tiles, preview the search, then download.
3. Run **Duplicate Removal** if you downloaded all product versions or want an extra local cleanup pass.
4. In **Extraction**, convert cleaned raw NetCDF files to GeoTIFFs using the GDAL conda runtime.
5. In **Mosaic**, optionally group GeoTIFFs into upload-ready mosaics.
6. In **Upload**, run a dry run first, then upload to the target Earth Engine ImageCollection.
7. Use **Statistics** to check coverage, project records, uploads, and QA tables.
8. Use **Cleanup** to preview and delete safe intermediate files when you need to recover disk space.

## Important Warnings

- SWOTFlow is an unofficial Earth Engine browser automation helper. If Google changes the Earth Engine web UI, selectors may need maintenance.
- Browser upload mode does **not** use Google Cloud Storage buckets. It uses a normal Chrome profile and sends local GeoTIFF paths to the Earth Engine upload dialog.
- Large Download previews first query NASA CMR metadata before files start downloading; the GUI reports paged search progress for large requests.
- The project intentionally uses two Python environments: `.venv` for the GUI/download/upload utilities, and a GDAL conda environment for extraction and mosaicking.
- Do not store Earthdata or Google credentials in `config.yaml`. Earthdata login is handled by `earthaccess`; Earth Engine login is handled through Chrome and, for asset listing, the Earth Engine Python API authentication.
- Use a SWOTFlow project before previewing, downloading, processing, or uploading. `config.yaml` is only the active session mirror and may contain paths from a previous session.
- Do not delete `swotflow.sqlite3`; it is the authoritative project record. CSV files in `00_logs` are readable exports, snapshots, and compatibility reports.

## Quick Start

Requirements:

- Windows with Google Chrome installed
- Python 3.11 or newer
- A Google account with Earth Engine access
- NASA Earthdata credentials for PO.DAAC downloads
- Miniforge or another conda-compatible installer for Extraction and Mosaic

Install the lightweight GUI/download/upload environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the optional GDAL processing environment:

```powershell
conda env create --prefix .\.conda\swot_gdal --file environment_swot_gdal.yml
.\.conda\swot_gdal\python.exe -m swotflow.processing.extract --check-gdal
```

Start the desktop app:

```powershell
python swotflow_gui.py
```

For full setup and first-run details, see [GETTING_STARTED.md](./GETTING_STARTED.md).

## Project Folder Structure

A SWOTFlow project keeps settings and intermediate files together:

```text
<project_root>\project.yaml
<project_root>\swotflow.sqlite3
<project_root>\01_raw_downloads
<project_root>\02_extracted_geotiffs
<project_root>\03_mosaics
<project_root>\00_logs
<project_root>\00_logs\upload_artifacts
<project_root>\profiles
```

`swotflow.sqlite3` is the authoritative indexed project record for downloads, extractions, mosaics, uploads, Earth Engine inventory, and workflow history. CSV files under `00_logs` remain readable exports and migration snapshots. Large NetCDF, GeoTIFF, mosaic, report, and debug files stay in the project folders.

## Repository Map

- `swotflow_gui.py`: small desktop launcher; `swotflow/ui/main.py` implements the main window and workflow tabs.
- `swotflow/consolidation/`: EE consolidation planning, incremental updates, replacement register and interface.
- `swotflow/catalog/` and `swotflow/verification/`: NASA history, input accounting and independent integrity checks.
- `swotflow/processing/automation.py`: tile-by-tile unattended workflow orchestration.
- `swotflow/project/model.py`: project metadata, project folders, history, and tile profile helpers.
- `swotflow/project/relocation.py` and `swotflow/project/transfer_dialog.py`: copied-project path relocation, validation, and recovery.
- `swotflow/project/database.py`: SQLite project store, legacy CSV migration, indexed status queries, and CSV exports.
- `swotflow/project/updates.py`: persistent date-window update campaigns and expected-granule records.
- `swotflow/processing/download.py`: Earthdata / PO.DAAC search, preview, manifest, and download logic.
- `swotflow/processing/duplicates.py`: local raw-file duplicate cleanup.
- `swotflow/processing/extract.py`: GDAL-backed SWOT NetCDF to GeoTIFF extraction.
- `swotflow/processing/mosaic.py`: GDAL-backed GeoTIFF mosaic creation.
- `swotflow/earthengine/uploader.py`: Earth Engine browser upload automation and asset verification.
- `swotflow/project/insights.py`: project statistics and cleanup candidate logic.
- `swotflow/ui/utm_map.py`: pure Tkinter visual UTM tile selector.
- `swotflow/tools/build_spatial_presets.py`: offline builder for continent and UTM display preset JSON.
- `swotflow/core/metadata.py`: shared SWOT filename parser.
- `swotflow/earthengine/selectors.py`: Earth Engine web UI selectors used by the uploader.
- `config.example.yaml`: tracked configuration template.
- `environment_swot_gdal.yml`: conda environment definition for GDAL processing.
- `requirements.txt`: `.venv` dependencies for the launcher, downloader, uploader, and Earth Engine utilities.
- `assets/swotflow_home_banner.png`: original SWOTFlow home banner displayed in the desktop app.
- `swotflow/tools/delete_ee_collection_children.py`: optional Earth Engine ImageCollection cleanup utility.
- `swotflow/tools/generate_home_banner.py`: standard-library utility that regenerates the bundled home banner asset.

## Documentation

- [GETTING_STARTED.md](./GETTING_STARTED.md): installation, first run, project workflow, and common GUI usage.
- [SWOT_PROCESSING_GUIDE.md](./SWOT_PROCESSING_GUIDE.md): detailed processing behavior, manifests, download logic, extraction, mosaicking, upload selection, and cleanup rules.
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md): common failures and fixes.
- [config.example.yaml](./config.example.yaml): complete configuration template.

Official references:

- [Earth Engine raster uploads](https://developers.google.com/earth-engine/guides/image_upload)
- [Earth Engine asset management](https://developers.google.com/earth-engine/guides/manage_assets)
