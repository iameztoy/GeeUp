# SWOTFlow Getting Started

Documentation has moved to a product-family structure. This root copy is kept
for compatibility during migration. The canonical HR Raster guide is
[docs/hr_raster/GETTING_STARTED.md](./docs/hr_raster/GETTING_STARTED.md), and
the platform overview is [README.md](./README.md).

This guide covers installation, first launch, project setup, and the normal GUI workflow. Detailed processing rules are kept in [SWOT_PROCESSING_GUIDE.md](./SWOT_PROCESSING_GUIDE.md).

## Environment Strategy

SWOTFlow uses two Python environments on purpose:

- `.venv`: lightweight environment for the GUI, SWOT downloader, duplicate remover, Earth Engine uploader, and small Earth Engine API utilities.
- GDAL conda environment: processing runtime for NetCDF extraction and mosaicking.

Keep these separate unless you have a specific reason to merge them. GDAL is more reliable from conda-forge on Windows, while the GUI and upload dependencies are simpler in a project-local virtual environment.

## Requirements

You need:

- Windows with Google Chrome installed.
- Python 3.11 or newer.
- A Google account with Earth Engine access.
- NASA Earthdata credentials for PO.DAAC SWOT downloads.
- Miniforge or another conda-compatible installer if you will run Extraction or Mosaic.

## Install Python Packages

Open PowerShell in the SWOTFlow repository folder:

```powershell
cd C:\path\to\SWOTFlow
```

Create and activate the local virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this in the same terminal and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Install The GDAL Processing Environment

This is required for Extraction, Mosaic, and full Automation runs. It is not required if you only use Download, Duplicate Removal, Statistics, or Upload.

From Miniforge Prompt or another conda-capable shell:

```powershell
cd C:\path\to\SWOTFlow
conda env create --prefix .\.conda\swot_gdal --file environment_swot_gdal.yml
```

Verify the environment:

```powershell
.\.conda\swot_gdal\python.exe swot_extract_tool.py --check-gdal
```

Expected output should include the required GDAL drivers:

```text
required_drivers=GTiff,VRT,netCDF
```

If your GDAL environment is outside the repository, use that environment's `python.exe` path in the GUI fields, for example:

```text
D:\SWOT\conda_envs\swot_gdal\python.exe
```

## Start SWOTFlow

Activate `.venv`, then run:

```powershell
python swotflow_gui.py
```

Create or open a project before running workflow actions. SWOTFlow projects keep paths, reports, manifests, presets, and processing history tied to one AOI or production workflow.

SWOTFlow opens on the **Home** tab. Use it for the project summary, selected-tile overview, workflow shortcuts, and GitHub link; use the processing tabs for the actual work.

The **Automation** tab is for unattended tile-by-tile runs inside an open project. Use `Authenticate Earthdata` there, or authenticate from Download, before a run that needs new PO.DAAC downloads. Select tiles, choose the date range, run `Run Preflight`, then start automation only after the preflight passes. When `Include upload` is enabled, preflight first synchronizes the target Earth Engine collection automatically so tile classifications and cleanup decisions use current verified assets. Preflight also warns about pending Windows reboot markers and active-hours restart risk; restart, pause updates, or adjust Windows Update settings before overnight runs when those warnings appear. Use `Upload completion mode` to choose whether Automation waits for active EE ingestion tasks before moving to the next tile or submits and verifies them later. Keep `Prevent computer sleep while automation runs` enabled for overnight work; on Windows this asks the OS to stay awake while Automation is active. Enable `Start automatically after successful preflight` when you want the run to begin without a second click. `Stop After Current Stage` records a resumable run. After reopening an unchanged project, authenticate Earthdata if downloads remain and click `Resume Run`; a new preflight is not required. Run a fresh preflight instead when dates, tiles, settings, project files, uploads, cleanup, or manual processing changed while Automation was stopped. Use `Copy Download Date Range` when you want automation to use the same temporal window as the manual Download tab; SWOTFlow warns when the two date ranges differ. Upload is optional because browser-based Earth Engine upload still needs an untouched Chrome/session.

During execution, Automation reports an aggregate percentage based on completed or skipped tile-stage units, together with the current tile position and stage. This gives a stable overview for large queues while detailed file progress remains in the status message and stage logs. Tiles already complete for the requested date range are skipped on resume; cleanup is not rerun for those complete tiles. If a CMR/Earthdata metadata search times out, the tile is left resumable, later tiles can continue, and Automation retries deferred CMR/download-search failures after the first tile pass.

Use this restart rule:

- **Stopped with `Stop After Current Stage`:** reopen the same project, authenticate Earthdata if needed, then click `Resume Run`.
- **Complete update campaign:** keep the saved campaign for Statistics. Set the next date range, run a new preflight, then start a new Automation run.
- **Power loss or unexpected shutdown:** reopen the same project and normally use `Resume Run`. If upload was active, wait for Earth Engine tasks to settle, run a fresh preflight so its automatic EE sync verifies submitted assets, then click `Start Automation`.

## Create Or Open A Project

Use one project per main AOI or workflow, for example:

- `Okavango_Delta`
- `Africa_Full`
- `Lake_Tanganyika`

The project folder contains:

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

`project.yaml` stores settings and lightweight metadata. `swotflow.sqlite3` stores the indexed processing history used for resume, QA, Statistics, Cleanup, and upload verification. Large NetCDF, GeoTIFF, mosaic, log, report, and debug files stay as normal files in the project folders.

Existing projects are migrated automatically when opened. Their CSV manifests are imported once and retained; after migration, SQLite is authoritative and CSV reports are compatibility exports rather than the live record updated after every file.

`config.yaml` remains a local session mirror for command-line compatibility. It is not the main project record.

## Chrome Profile For Earth Engine Upload

SWOTFlow uploads through the Earth Engine web interface using Google Chrome and Selenium.

The default Chrome profile folder is:

```text
.\chrome-profile
```

Use a dedicated profile for SWOTFlow. This keeps the Earth Engine login separate from your normal browser profile and avoids profile-lock conflicts. If Chrome says the profile is locked, close other Chrome windows that may be using the same profile.

## Earthdata Authentication

The Download tab uses `earthaccess` for NASA Earthdata / PO.DAAC access.

Recommended options:

- Use an existing `%USERPROFILE%\_netrc` file.
- Use `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` environment variables for a session.
- Click `Authenticate` in the GUI and follow the interactive prompt.

SWOTFlow does not write Earthdata passwords to `config.yaml` or `project.yaml`.

## Normal GUI Workflow

### 1. Download

Use the Download tab to search and retrieve SWOT L2 HR Raster 100 m NetCDF files.

Typical steps:

1. Click `Authenticate`.
2. Choose the collection. The default is active Version D: `SWOT_L2_HR_Raster_100m_D`.
3. Enter start and end dates.
4. Select UTM tiles manually, with a continent preset, or with the visual UTM selector.
5. Click `Preview Search`.
6. Review file count, size estimate, product-version filtering, raw-file status, and manifest-known status.
7. Click `Download Matches`.

The raw files go to `01_raw_downloads` by default. Download records are stored in the project database, with preview and manifest CSV exports under `00_logs`.

For large searches, the status text may say `Searching CMR`. CMR is NASA's Common Metadata Repository, the metadata service used by PO.DAAC/Earthdata. Large tile/date searches can spend time listing metadata before the first download starts; SWOTFlow reports paged CMR progress such as total matching granules and metadata retrieved so the window does not look idle. In Automation, stalled CMR page requests are bounded by `download.search_request_timeout_seconds`; a tile that cannot complete its metadata search is marked failed/resumable so the run can continue or be retried later.

For recurring tile updates, keep the same project open, select the already processed tiles, set the new date window, and run `Preview Search` before downloading. The latest preview becomes the expected remote set for the Statistics > Status Map `Update Coverage` mode.

### 2. Duplicate Removal

Run Duplicate Removal when repeated raw product versions are present or when you want a conservative cleanup pass before extraction.

The tool keeps the preferred version in place and moves older versions into a `moved` subfolder. See [SWOT_PROCESSING_GUIDE.md](./SWOT_PROCESSING_GUIDE.md) for the CRID/product-counter ranking rules.

### 3. Extraction

Use Extraction to convert NetCDF files into two-band GeoTIFFs.

Set **GDAL Python** to a valid GDAL conda Python executable, then choose:

- input NetCDF folder
- output GeoTIFF folder
- CRS mode
- optional year or file limits
- skip-existing behavior
- parallel workers; use `1` for the original one-by-one behavior

Click `Plan Extraction`, then `Run Extraction`.

### 4. Mosaic

Use Mosaic when you want fewer GeoTIFFs before upload.

Set **GDAL Python**, input folder, output folder, grouping mode, and parallel workers. Keep Mosaic workers at `1` for the original cautious one-by-one behavior; try `2` only when disk space, RAM, and disk speed are comfortable. Then click `Plan Mosaics` and `Run Mosaic`.

If extraction keeps the original SWOT UTM/grid CRS, per-output UTM tile statistics are meaningful. If extraction reprojects everything to a common CRS such as `LAEA` or `WGS84`, use the Statistics tab's source UTM tile table instead.

### 5. Upload

Use Upload to send GeoTIFFs to a Google Earth Engine ImageCollection.

Typical steps:

1. Set the origin folder.
2. Set the destination ImageCollection asset path.
3. Choose upload scope: all files, or selected UTM/source tiles only.
4. If selected-tile scope is active, choose upload tiles from the list, or type/paste tile IDs and optionally click `Validate Typed Tiles`.
5. In the `Execution` box, run `Run Dry Run`.
6. Review the Upload summary or the exported `00_logs\upload_report.csv`.
7. In the `Execution` box, run `Run Real Upload`.

List clicks update the optional UTM filter immediately. `Validate Typed Tiles` only checks typed or pasted tile IDs and refreshes the list highlighting. It does not start an upload.

Dry run is recommended when you changed the origin folder, destination collection, upload scope, selected tiles, naming prefix/suffix, or metadata settings. If only retrying the same checked setup, you can run the real upload directly. The dry-run console prints only a short preview; the full per-file plan is saved in the project database and exported to `upload_report.csv`. The Upload tab progress bar reads indexed project status counts and summarizes planned, submitted, completed, failed, and filtered rows.

If a real upload was accepted by the browser but the task or final asset has not yet been verified, project records may show `SUBMITTED_PENDING_VERIFICATION` or `SUBMITTED`. After the Earth Engine assets have appeared in the target collection, use `Sync EE Assets` in the Upload tab. It lists the destination collection and marks matching local mosaics as `EE_VERIFIED_EXISTS`, which also updates Statistics and Cleanup eligibility.

For the current Earth Engine dialog, SWOTFlow waits for the browser-side upload dialog to close, records `SUBMITTED_PENDING_VERIFICATION`, and immediately prepares the next file. It does not switch to Tasks after every submission. Visible percentage/progress changes keep the dialog wait alive; an open dialog with no detectable progress is recovered and retried. `Upload completion mode` controls tile handoff: `Wait for EE verification before next tile` waits for active EE tasks, while `Submit and continue; verify later` runs a non-blocking task/inventory reconciliation and lets Automation move to later tiles. EE sync verifies successful assets, while unverified mosaics remain on disk and are retryable in a later upload run. In Automation, each upload-enabled mosaic cleanup is a global verified-mosaic sweep, so a later tile can remove mosaics from an earlier tile once a later EE sync confirms those assets.

Before planning uploads, SWOTFlow can list existing Earth Engine assets and mark matching files as `EE_VERIFIED_EXISTS`, so already uploaded images are skipped even if the exported upload report is incomplete. The Upload tile list also excludes local files already recorded as `COMPLETED`, `SKIPPED_ALREADY_EXISTS`, or `EE_VERIFIED_EXISTS`; the status text shows which source tiles are already completed or verified.

If the Earth Engine page appears loaded but the upload console reports a page-load timeout, rerun with `Resume previous run` enabled. Recent SWOTFlow versions check whether the Earth Engine UI is already usable after a timeout and continue when possible. If Earth Engine rejects the dialog with `Please provide an asset ID`, SWOTFlow retries the Asset Name field with keyboard input and records unrecoverable browser failures as `ERROR` rows so they can be retried.

### 6. Statistics

The Statistics tab reads the SQLite project record and local folders. It summarizes:

- total files and size by processing level
- processing levels such as `PGD0_01` or any future CRID/product-counter level, including how many remote matches, downloads, extractions, mosaic sources, and uploaded/verified assets are recorded for each level
- raw downloads and manifest-known files
- older remote product versions excluded from download
- duplicate files moved
- files and date coverage by UTM tile
- processing levels by UTM tile, useful for checking whether some tiles are dominated by older or newer product versions
- extraction and mosaic counts
- upload status, UTM-filtered rows, and EE-verified existing assets
- uploaded/verified asset counts by status, source UTM tile, date, processing level, and output grid
- upload QA by UTM tile, comparing downloaded, extracted, mosaicked, and uploaded/verified counts
- update coverage by UTM tile, grouped into persistent collection/date-window campaigns
- local mosaics that appear ready but are not yet uploaded or verified in Earth Engine
- grouped upload failures and warning messages
- observed SWOT cycles, passes, scenes, CRIDs, and product counters

Use `Refresh Statistics` when you want an immediate update. SWOTFlow also refreshes statistics automatically after major workflow steps.

In the Status Map subtab, use `Pipeline Status` for the cumulative project view and `Update Coverage` for a specific update window. Preview Search and Automation preflight save the expected remote granules as a persistent update campaign identified by collection and date range. Use the `Update window` selector to switch between the current campaign and earlier or later updates. Completed tiles remain green and visible instead of disappearing when their local intermediate files are cleaned. If a campaign is missing, run Preview Search or Automation preflight once for its dates and tiles, then refresh statistics.

Each refresh writes a saved statistics snapshot under:

```text
<project_root>\00_logs\statistics
```

When you reopen a project, SWOTFlow reloads the latest saved snapshot so the Statistics tab is not blank. Click `Refresh Statistics` again whenever files or project records have changed and you want to recompute and resave the statistics.

### 7. Cleanup

Cleanup is a separate tab because it can delete local intermediate files. Click `Preview Cleanup` first; SWOTFlow only offers files with downstream project-record proof, such as raw files that were already extracted or mosaics that were uploaded or verified in Earth Engine. Then delete selected rows, or delete all previewed candidates when you are sure the project stage is complete.

## Manual CLI Commands

The GUI writes `config.yaml` as the active session mirror. You can also run modules from the terminal.

Preview a download:

```powershell
python swot_download_tool.py --config config.yaml --dry-run
```

Run a download:

```powershell
python swot_download_tool.py --config config.yaml
```

Plan extraction:

```powershell
.\.conda\swot_gdal\python.exe swot_extract_tool.py --config config.yaml --dry-run
```

Run extraction:

```powershell
.\.conda\swot_gdal\python.exe swot_extract_tool.py --config config.yaml
```

Plan mosaics:

```powershell
.\.conda\swot_gdal\python.exe ee_mosaic_tool.py --config config.yaml --dry-run
```

Run mosaics:

```powershell
.\.conda\swot_gdal\python.exe ee_mosaic_tool.py --config config.yaml
```

Run an upload dry run:

```powershell
python ee_ui_uploader.py --config config.yaml --dry-run
```

Run a real upload:

```powershell
python ee_ui_uploader.py --config config.yaml
```

## Logs And Reports

In a project workflow, reports and debug output are written under:

```text
<project_root>\00_logs
```

Common files include:

- `download_preview.csv`
- `swotflow.sqlite3` as the authoritative cumulative record
- `download_manifest.csv`, `extract_manifest.csv`, and `mosaic_manifest.csv` as compatibility exports
- `mosaic_report.csv`
- `upload_report.csv`
- `ee_asset_inventory.csv`
- legacy `workflow_manifest.csv` files retained during migration; new workflow records are stored in SQLite
- `statistics\project_statistics_snapshot.json`
- `statistics\project_statistics_*.csv`
- `statistics\project_statistics_upload_qa_by_tile.csv`
- `statistics\project_statistics_ready_not_uploaded.csv`
- `statistics\project_statistics_upload_errors.csv`

Older root-level folders such as `logs`, `artifacts`, or `reports` may exist from pre-project runs or from manual CLI runs that used older defaults.

## Regenerate Spatial Presets

Built-in continent presets and display geometry are generated offline from the UTM grid and continent GeoPackages:

```powershell
python build_spatial_presets.py --utm-grid C:\path\to\World_UTM_Grid.gpkg --continents C:\path\to\World_Continents.gpkg --output spatial_presets\continent_utm_tiles.json --display-output spatial_presets\utm_display_geometries.json
```

The GUI consumes the generated JSON files. It does not require geopandas, shapely, fiona, pyogrio, or a web map runtime.

## More Documentation

- [SWOT_PROCESSING_GUIDE.md](./SWOT_PROCESSING_GUIDE.md): detailed behavior, manifests, processing rules, mosaics, upload filtering, and cleanup logic.
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md): common errors and fixes.
- [config.example.yaml](./config.example.yaml): complete configuration template.
