# SWOTFlow

![SWOTFlow workflow banner](assets/swotflow_home_banner.png)

SWOTFlow is evolving from a single-product desktop workflow into a local SWOT
product-family processing platform. The current production workflow is the SWOT
L2 HR Raster 100 m tool. A new SWOT L2 HR Pixel Cloud / PIXC workflow is being
introduced as a separate product family.

## Product Launcher

The product-family launcher is:

```powershell
python swotflow_platform.py
```

It currently offers:

- **Process HR Raster 100 m:** opens the existing raster workflow for
  Earthdata download, duplicate cleanup, NetCDF extraction to GeoTIFF,
  mosaicking, optional Earth Engine upload, statistics, cleanup, and automation.
- **Process Pixel Cloud / PIXC:** opens the new PIXC workflow shell for future
  NetCDF point-cloud download, inspection, statistics, QA, and export tools.

The legacy raster entry point remains available and intentionally unchanged:

```powershell
python swotflow_gui.py
```

Use this direct entry point when you want to run the current HR Raster workflow
without going through the product selector.

## HR Raster Workflow Features

- **Projects:** one folder per AOI or workflow, with project-specific raw downloads, extracted GeoTIFFs, mosaics, logs, presets, and settings.
- **Home:** a visual landing tab with project status, workflow shortcuts, GitHub access, and selected-tile summary.
- **Automation:** runs the project workflow tile by tile after a required preflight, with resumable manifests and verified-stage cleanup.
- **Download:** searches and downloads PO.DAAC SWOT L2 HR Raster 100 m data through `earthaccess`, with date and UTM tile filtering.
- **Duplicate Removal:** moves older local raw granule versions when several CRID/product-counter versions exist.
- **Extraction:** converts SWOT NetCDF files into two-band GeoTIFFs with `wse` and `wse_qual`, with optional worker-based parallelism.
- **Mosaic:** reduces GeoTIFF counts before upload, while keeping SWOT-compatible naming and metadata parsing, with optional cautious group parallelism.
- **Upload:** uploads GeoTIFFs to Google Earth Engine through Chrome/Selenium, with optional UTM/source-tile filtering and Earth Engine asset verification.
- **Statistics:** summarizes project coverage, update coverage, processing status, file counts, dates, UTM tiles, uploads, and QA tables.
- **Cleanup:** previews and deletes safe intermediate-file cleanup candidates with downstream project-record proof.

## Product Documentation

- [HR Raster 100 m README](./docs/hr_raster/README.md): current production
  raster workflow overview.
- [HR Raster Getting Started](./docs/hr_raster/GETTING_STARTED.md):
  installation, first launch, project setup, and normal raster GUI usage.
- [HR Raster Processing Guide](./docs/hr_raster/PROCESSING_GUIDE.md):
  detailed processing behavior, manifests, extraction, mosaicking, upload
  selection, statistics, and cleanup rules.
- [HR Raster Troubleshooting](./docs/hr_raster/TROUBLESHOOTING.md): common
  raster workflow failures and fixes.
- [PIXC README](./docs/pixc/README.md): initial Pixel Cloud workflow scope.
- [PIXC Getting Started](./docs/pixc/GETTING_STARTED.md): early setup and
  project-separation guidance.
- [PIXC Inspection Guide](./docs/pixc/INSPECTION_GUIDE.md): planned NetCDF
  inspection outputs and QA summaries.
- [PIXC Roadmap](./docs/pixc/ROADMAP.md): staged download, AOI picker, and
  satellite-basemap plan.
- [PIXC Troubleshooting](./docs/pixc/TROUBLESHOOTING.md): early PIXC workflow
  troubleshooting notes.

## Current Status

The HR Raster workflow is the stable, feature-complete workflow. It keeps its
existing scripts, tests, configuration shape, project database behavior, and
direct GUI entry point.

The PIXC workflow is intentionally early. It now has a product shell and a first
single-file NetCDF inspection backend. The first useful capabilities are:

- Inspect one PIXC NetCDF-4 file.
- Summarize NetCDF groups, dimensions, variables, shapes, dtypes, and key
  attributes.
- Report point counts, classification distributions, quality-flag
  distributions, coordinate and height ranges, and missing/fill values.
- Use those summaries to define later filtering and export rules.

PIXC search/download and batch inspection are planned next.

PIXC Earth Engine upload is not assumed as an initial feature. Offline
inspection and point-data exports come first.

## Development Structure

The first platform layer is intentionally additive:

```text
swotflow_platform.py
products/
  hr_raster/
    app.py
  pixc/
    app.py
    config.py
    inspect.py
```

The existing raster modules remain at the repository root for compatibility
while the platform shell matures. Shared `core/` modules can be introduced later
only where both product families need the same behavior, such as Earthdata
helpers, project management, database access, runtime utilities, and common
SWOT product-version ranking.

## Project Data Separation

Git branches and worktrees separate code. SWOTFlow project folders separate
data and processing records. Each project keeps its own `project.yaml`,
`swotflow.sqlite3`, logs, and outputs.

Use separate project roots for production HR Raster processing and PIXC
experiments so their databases and intermediate files do not interfere.

## Requirements

The current HR Raster workflow requires:

- Windows with Google Chrome installed
- Python 3.11 or newer
- NASA Earthdata credentials for PO.DAAC downloads
- A Google account with Earth Engine access when using raster uploads
- Miniforge or another conda-compatible installer for GDAL extraction and
  mosaicking

The early PIXC shell uses the same lightweight GUI environment. PIXC file
inspection will add optional NetCDF/data-analysis dependencies when implemented.

## Repository Map

- `swotflow_platform.py`: product-family selector.
- `swotflow_gui.py`: HR Raster desktop workflow with Home, Automation, Download, Duplicate Removal, Extraction, Mosaic, Upload, Statistics, and Cleanup tools.
- `products/hr_raster/`: wrapper around the current HR Raster workflow.
- `swotflow_automation.py`: tile-by-tile unattended workflow orchestration.
- `swotflow_project.py`: project metadata, project folders, history, and tile profile helpers.
- `project_database.py`: SQLite project store, legacy CSV migration, indexed status queries, and CSV exports.
- `project_updates.py`: persistent date-window update campaigns and expected-granule records.
- `swot_download_tool.py`: Earthdata / PO.DAAC search, preview, manifest, and download logic.
- `swot_duplicate_remover.py`: local raw-file duplicate cleanup.
- `swot_extract_tool.py`: GDAL-backed SWOT NetCDF to GeoTIFF extraction.
- `ee_mosaic_tool.py`: GDAL-backed GeoTIFF mosaic creation.
- `ee_ui_uploader.py`: Earth Engine browser upload automation and asset verification.
- `project_insights.py`: project statistics and cleanup candidate logic.
- `utm_map_selector.py`: pure Tkinter visual UTM tile selector.
- `products/pixc/`: PIXC workflow shell, project helpers, preview/download, AOI/reference tile tools, NetCDF inspection, and point visualization.
- `build_spatial_presets.py`: offline builder for continent and UTM display preset JSON.
- `build_pixc_reference_tiles.py`: offline builder for compact SWOT PIXC tile/pass reference presets.
- `swot_metadata.py`: shared SWOT filename parser.
- `ee_selectors.py`: Earth Engine web UI selectors used by the uploader.
- `config.example.yaml`: tracked configuration template.
- `environment_swot_gdal.yml`: conda environment definition for GDAL processing.
- `requirements.txt`: `.venv` dependencies for the launcher, downloader, uploader, and Earth Engine utilities.
- `requirements-pixc.txt`: optional dependencies for real PIXC NetCDF inspection, visualization, and reference imagery.
- `assets/swotflow_home_banner.png`: original SWOTFlow home banner displayed in the desktop app.
- `Utils/delete_ee_collection_children.py`: optional Earth Engine ImageCollection cleanup utility.
- `Utils/generate_home_banner.py`: standard-library utility that regenerates the bundled home banner asset.

## Official References

- [PO.DAAC SWOT L2 HR Raster 100 m Version D](https://podaac.jpl.nasa.gov/dataset/SWOT_L2_HR_Raster_100m_D)
- [PO.DAAC SWOT L2 HR PIXC Version D](https://podaac.jpl.nasa.gov/dataset/SWOT_L2_HR_PIXC_D)
- [Earth Engine raster uploads](https://developers.google.com/earth-engine/guides/image_upload)
- [Earth Engine asset management](https://developers.google.com/earth-engine/guides/manage_assets)
