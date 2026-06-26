# SWOTFlow HR Raster 100 m

![SWOTFlow workflow banner](../../assets/swotflow_home_banner.png)

SWOTFlow is a local desktop workflow for SWOT L2 HR Raster 100 m data. It helps you download SWOT raster NetCDF files from NASA Earthdata / PO.DAAC, clean repeated product versions, extract GeoTIFFs, optionally build mosaics, upload outputs to Google Earth Engine, and audit the project afterwards.

The direct HR Raster entry point is:

```powershell
python swotflow_gui.py
```

The product-family launcher is also available:

```powershell
python swotflow_platform.py
```

Choose **Process HR Raster 100 m** from the product selector to open this same
workflow.

## What SWOTFlow Does

- **Projects:** one folder per AOI or workflow, with project-specific raw downloads, extracted GeoTIFFs, mosaics, logs, presets, and settings.
- **Home:** a visual landing tab with project status, workflow shortcuts, GitHub access, and selected-tile summary.
- **Automation:** runs the project workflow tile by tile after a required preflight, with resumable manifests and verified-stage cleanup.
- **Download:** searches and downloads PO.DAAC SWOT L2 HR Raster 100 m data through `earthaccess`, with date and UTM tile filtering.
- **Duplicate Removal:** moves older local raw granule versions when several CRID/product-counter versions exist.
- **Extraction:** converts SWOT NetCDF files into two-band GeoTIFFs with `wse` and `wse_qual`, with optional worker-based parallelism.
- **Mosaic:** reduces GeoTIFF counts before upload, while keeping SWOT-compatible naming and metadata parsing, with optional cautious group parallelism.
- **Upload:** uploads GeoTIFFs to Google Earth Engine through Chrome/Selenium, with optional UTM/source-tile filtering and Earth Engine asset verification.
- **Statistics:** summarizes project coverage, processing status, file counts, dates, UTM tiles, uploads, and QA tables.
- **Cleanup:** previews and deletes safe intermediate-file cleanup candidates with downstream project-record proof.

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
.\.conda\swot_gdal\python.exe swot_extract_tool.py --check-gdal
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

- `swotflow_gui.py`: desktop launcher with Home, Automation, Download, Duplicate Removal, Extraction, Mosaic, Upload, Statistics, and Cleanup tools.
- `swotflow_automation.py`: tile-by-tile unattended workflow orchestration.
- `swotflow_project.py`: project metadata, project folders, history, and tile profile helpers.
- `project_database.py`: SQLite project store, legacy CSV migration, indexed status queries, and CSV exports.
- `swot_download_tool.py`: Earthdata / PO.DAAC search, preview, manifest, and download logic.
- `swot_duplicate_remover.py`: local raw-file duplicate cleanup.
- `swot_extract_tool.py`: GDAL-backed SWOT NetCDF to GeoTIFF extraction.
- `ee_mosaic_tool.py`: GDAL-backed GeoTIFF mosaic creation.
- `ee_ui_uploader.py`: Earth Engine browser upload automation and asset verification.
- `project_insights.py`: project statistics and cleanup candidate logic.
- `utm_map_selector.py`: pure Tkinter visual UTM tile selector.
- `build_spatial_presets.py`: offline builder for continent and UTM display preset JSON.
- `swot_metadata.py`: shared SWOT filename parser.
- `ee_selectors.py`: Earth Engine web UI selectors used by the uploader.
- `config.example.yaml`: tracked configuration template.
- `environment_swot_gdal.yml`: conda environment definition for GDAL processing.
- `requirements.txt`: `.venv` dependencies for the launcher, downloader, uploader, and Earth Engine utilities.
- `assets/swotflow_home_banner.png`: original SWOTFlow home banner displayed in the desktop app.
- `Utils/delete_ee_collection_children.py`: optional Earth Engine ImageCollection cleanup utility.
- `Utils/generate_home_banner.py`: standard-library utility that regenerates the bundled home banner asset.

## Documentation

- [GETTING_STARTED.md](./GETTING_STARTED.md): installation, first run, project workflow, and common GUI usage.
- [PROCESSING_GUIDE.md](./PROCESSING_GUIDE.md): detailed processing behavior, manifests, download logic, extraction, mosaicking, upload selection, and cleanup rules.
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md): common failures and fixes.
- [config.example.yaml](../../config.example.yaml): complete configuration template.

Official references:

- [Earth Engine raster uploads](https://developers.google.com/earth-engine/guides/image_upload)
- [Earth Engine asset management](https://developers.google.com/earth-engine/guides/manage_assets)
