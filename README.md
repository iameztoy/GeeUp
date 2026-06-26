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
- `swotflow_gui.py`: existing HR Raster desktop workflow.
- `products/hr_raster/`: wrapper around the current HR Raster workflow.
- `products/pixc/`: initial PIXC workflow shell and product constants.
- `products/pixc/inspect.py`: optional-dependency NetCDF inspection backend.
- `swot_download_tool.py`: current HR Raster Earthdata search/download logic.
- `swot_duplicate_remover.py`: current HR Raster local duplicate cleanup.
- `swot_extract_tool.py`: current HR Raster GDAL NetCDF-to-GeoTIFF extraction.
- `ee_mosaic_tool.py`: current HR Raster GDAL mosaic creation.
- `ee_ui_uploader.py`: current HR Raster Earth Engine browser upload helper.
- `swotflow_project.py`: current project metadata and folder helpers.
- `project_database.py`: SQLite-backed project record store.
- `project_insights.py`: current HR Raster statistics and cleanup planning.
- `utm_map_selector.py`: current HR Raster UTM tile selector.
- `config.example.yaml`: current HR Raster configuration template.
- `environment_swot_gdal.yml`: GDAL processing environment definition.
- `requirements.txt`: lightweight GUI/download/upload environment dependencies.
- `requirements-pixc.txt`: optional dependencies for real PIXC NetCDF inspection.

## Official References

- [PO.DAAC SWOT L2 HR Raster 100 m Version D](https://podaac.jpl.nasa.gov/dataset/SWOT_L2_HR_Raster_100m_D)
- [PO.DAAC SWOT L2 HR PIXC Version D](https://podaac.jpl.nasa.gov/dataset/SWOT_L2_HR_PIXC_D)
- [Earth Engine raster uploads](https://developers.google.com/earth-engine/guides/image_upload)
- [Earth Engine asset management](https://developers.google.com/earth-engine/guides/manage_assets)
