# SWOT Processing Guide

This document keeps the processing-specific behavior that is too detailed for the main README. For installation, first launch, and day-to-day GUI usage, see [GETTING_STARTED.md](./GETTING_STARTED.md).

## Processing Order

The current GUI workflow is:

1. `Download`
2. `Duplicate Removal`
3. `Extraction`
4. `Mosaic`
5. `Upload`

Default working folders:

- `<LOCAL_PROCESSING_ROOT>\01_raw_downloads`
- `<LOCAL_PROCESSING_ROOT>\02_extracted_geotiffs`
- `<LOCAL_PROCESSING_ROOT>\03_mosaics`
- `<LOCAL_PROCESSING_ROOT>\00_logs`

When a GeeUp project is open, `<LOCAL_PROCESSING_ROOT>` is the project root and the same folder names are created inside that project. The active `config.yaml` remains a mirror of the open project so CLI tools still use the same workflow settings.

The detailed sections below prioritize the main processing path: download, extraction, mosaicking, upload compatibility, and runtime setup. Optional cleanup and maintenance utilities are documented at the end.

## Projects And Spatial Presets

Projects are intended for reusable AOI workflows, such as `Okavango Delta` and `Africa Full`.

Each project stores:

- `project.yaml` with the current processing settings
- `profiles/*.json` tile presets saved from the Download tab
- download history used by `Prepare Update`
- intermediate files in project-specific raw, extracted, mosaic, and log folders
- upload debug screenshots and HTML dumps in `00_logs/upload_artifacts`
- stage manifests in `00_logs`, plus a shared `workflow_manifest.csv` ledger for future statistics

The GUI requires an active project before saving config, previewing downloads, downloading, or running processing steps. `config.yaml` remains a session mirror for CLI compatibility and can contain paths from the previous session; if it points to a folder that contains `project.yaml`, the GUI auto-opens that project on startup. Otherwise, create or open a project first so the output folders, reports, manifest, and Earth Engine target are explicit.

The Download tab's selected UTM token list remains the source of truth. Built-in continent presets and project presets only populate that same selected-token state, so manual editing and paste-style workflows still work.

The visual selector opened from `Open UTM Map Selector` draws continent outlines and the UTM grid from precomputed JSON. It shows UTM zone numbers and latitude-band letters for orientation. It is a tile-selection aid, not a precision GIS editor: click a tile to toggle it, then apply the selection back to the Download tab.

The current background geometry is a continent layer, not a country/admin-boundary layer. Use a country boundary GeoPackage later if internal country borders are needed.

Built-in continent presets are read from `spatial_presets/continent_utm_tiles.json`, and the visual selector reads `spatial_presets/utm_display_geometries.json`. Both files are generated offline from the UTM grid and continent GeoPackages with:

```powershell
python build_spatial_presets.py --utm-grid C:\path\to\World_UTM_Grid.gpkg --continents C:\path\to\World_Continents.gpkg --output spatial_presets\continent_utm_tiles.json --display-output spatial_presets\utm_display_geometries.json
```

## Download Inputs

The Download tab searches PO.DAAC through `earthaccess` and writes matched SWOT L2 HR Raster 100 m NetCDF files to `01_raw_downloads` by default.

Phase 1 download filtering uses:

- collection short name, default `SWOT_L2_HR_Raster_100m_D`
- start and end date
- one or more UTM/MGRS tile tokens embedded in filenames, such as `UTM30R`

Tiles can be typed manually, selected in the searchable list, populated from a built-in continent preset, loaded from a project tile preset, or toggled in the visual UTM selector. The text/list selection remains editable after applying any preset or map selection.

The preview report records matched filenames, UTM tokens, date metadata, known file sizes, local paths, status, raw-file presence, manifest-known status, and Earthdata links. After a successful download, the launcher points Duplicate Removal and Extraction at the same raw-download folder.

The `Product version filter` is applied after the Earthdata search and before downloading. `Best product version only` groups remote matches that represent the same SWOT observation and selects the highest-ranked CRID/product-counter file for download. Older versions remain in `download_preview.csv`, `download_manifest.csv`, and `workflow_manifest.csv` with `EXCLUDED_OLDER_VERSION` so the project still records that they existed and were intentionally not downloaded. Use `All matching files` when you want every remote file and prefer to let Duplicate Removal move older versions later.

For project time-series updates, `Prepare Update` uses the last successful project download end date as the next inclusive start date and sets the end date to today. Existing-file skipping, manifest-known skipping, and duplicate cleanup are still responsible for handling overlap safely.

After a preview or download run, the report is written with `downloaded`, `raw_exists`, and `known_from_manifest` columns. `downloaded=yes` means the matched granule is accounted for either by a complete raw file or by `download_manifest.csv`. `raw_exists=no` and `known_from_manifest=yes` means the project has already downloaded that granule before, even if the raw NetCDF has since been deleted. `no` means the granule is still missing, failed, or was cancelled before it was attempted. The GUI summary reports accounted-for granules versus matched granules.

GeeUp downloads granules in batches. `Download threads` is passed to `earthaccess.download()` as the number of parallel workers for a batch, while `Batch size` controls how many granules are submitted to one earthaccess call. Use `Batch size = 1` for the old one-file-at-a-time behavior. For large runs, start with `threads = 6` and `batch_size = 25`; increase carefully if the network and PO.DAAC responses remain stable.

`Stop Download` is cooperative. It requests the download loop to stop after the current batch finishes or fails, writes the report with remaining files marked `CANCELLED` or `MISSING`, and leaves already downloaded files in place. Restart the same search with `skip_existing` and `skip_manifest_existing` enabled to continue.

The visual UTM selector uses two different ideas: blue tiles are active query selections for the next preview/download, green tiles have at least one granule recorded in the project download manifest, and teal tiles are both selected and already covered. Keeping previously downloaded covered tiles selected is safe: GeeUp will skip manifest-known granules and only download new matching granules unless manifest skipping is disabled.

Extraction, mosaic, and upload now also contribute to cumulative stage tracking:

- `extract_manifest.csv` records NetCDF inputs that were written, skipped because the GeoTIFF exists, or skipped because they were already recorded.
- `mosaic_manifest.csv` records mosaic outputs and the source-file signature used to create or accept them.
- `upload_report.csv` remains the Earth Engine upload ledger.
- `ee_asset_inventory.csv` records the latest Earth Engine destination listing used to verify already-uploaded assets.
- `workflow_manifest.csv` combines download, extract, mosaic, and upload rows into one project-level table.

Use the `Statistics` tab after any major run to audit the active project. It reads the project manifests and local folders to summarize total project files and size, file counts per processing level, duplicate files moved, files per UTM tile, dates covered, recorded downloads, remote matches excluded as older versions, completed extractions, mosaics, upload status, EE-verified existing assets, UTM-filtered upload rows, known cumulative download size, and observed SWOT cycles, passes, scenes, CRIDs, and product counters. The Processing Levels view treats the processing level as a future-proof `CRID_productCounter` label such as `PGD0_01`; it reports remote matches, selected downloads, accounted downloads, completed extractions, mosaic source participation, and uploaded/verified rows per level, plus the same breakdown by UTM tile. The Uploaded view reports upload status counts, uploaded/verified assets by source tile, date, processing level, and output grid, plus QA tables for pipeline completeness by UTM tile, ready mosaics not yet uploaded/verified, and grouped upload failures or warnings. It also draws lightweight bar plots for stage counts and top UTM tiles. The Mosaics statistics view reports completed mosaics by output tile/grid and completed mosaic participation by source UTM tile.

Statistics refresh automatically after completed Download, Duplicate Removal, Extraction, Mosaic, and Cleanup actions. Upload runs in a separate console window, so GeeUp watches the configured `upload_report.csv` and refreshes statistics when that report changes. You can still click `Refresh Statistics` at any time. Each refresh saves the latest statistics under `00_logs/statistics` as `project_statistics_snapshot.json` plus CSV tables for metrics, stage statuses, UTM tiles, dates, processing levels, processing levels by tile, mosaic output grids, mosaic source tiles, upload statuses, uploaded tiles/dates/levels/grids, upload QA by tile, ready-not-uploaded mosaics, and upload errors. When a project is reopened, the GUI loads the latest saved snapshot immediately; refresh again to recompute from current files and manifests.

The separate `Cleanup` tab provides conservative cleanup controls. `Preview Cleanup` lists only files with downstream manifest proof: raw NetCDFs that have completed extraction rows, extracted GeoTIFFs that belong to completed non-stale mosaic rows, and mosaic GeoTIFFs that are recorded as uploaded or already present in Earth Engine. Delete selected rows when you want fine control, or delete all previewed candidates when the project stage is complete and storage is the priority.

For common-CRS whole pass/date mosaics, a newly added tile can change an existing mosaic group. GeeUp therefore compares the current source signature with the recorded mosaic manifest. If an output file already exists but the source set changed, the mosaic report marks that row `STALE_EXISTS`; enable overwrite or remove the stale output before rebuilding that mosaic group.

Mosaic-per-tile statistics depend on the output CRS. When extraction keeps the original SWOT tile/grid CRS, the mosaic output grid is a UTM tile token and the output-grid count is the direct "mosaics per tile" count. When extraction reprojects to a common CRS such as `LAEA` or `WGS84`, the mosaic output no longer belongs to one UTM tile. In those cases, use the source UTM tile participation table: it parses each completed mosaic row's `input_files` from `mosaic_manifest.csv` and counts which original SWOT tiles contributed to each common-CRS mosaic.

## Extraction Outputs

Extraction converts each SWOT NetCDF into one 2-band GeoTIFF:

- band 1: `wse`
- band 2: `wse_qual`

Output naming:

- original CRS: `<netcdf_stem>.tif`
- Africa LAEA: `<netcdf_stem>_africa_laea.tif`
- WGS84: `<netcdf_stem>_wgs84.tif`

The extraction step follows the notebook logic and writes uncompressed GeoTIFFs.

## Mosaic Grouping

Default grouping mode is `utm_zone`.

Groups are built from:

- SWOT descriptor
- cycle ID
- pass ID
- start date from `RangeBeginningDateTime`
- exact coordinate-system token such as `UTM28P`

Optional original-projection grouping mode is `utm_zone_hemisphere`.

That mode keeps original-projection processing, but collapses MGRS latitude-band tokens into UTM zone plus hemisphere. For example, `UTM30P`, `UTM30Q`, `UTM30R`, and `UTM30S` become `UTM30N`. Southern latitude bands such as `UTM30M` become `UTM30S`. GDAL validation still checks actual CRS compatibility before writing.

Common-CRS mode is `pass_date_common_crs`.

That mode is intended for already reprojected extraction outputs and groups by:

- descriptor family with the configured common CRS label
- cycle ID
- pass ID
- start date

It ignores the original UTM token in the filename, but still validates actual raster compatibility before merging.

In common-CRS mode, do not interpret mosaic outputs as per-UTM-tile products. The output grid is the configured common CRS, while the source-tile statistics and `input_files` manifest column preserve the UTM provenance needed to audit coverage.

## Upload Selection And Verification

Upload defaults to scanning every GeoTIFF in the configured origin folder. When `upload.scope` is `selected_utm`, only files whose output UTM tile or mosaic source UTM tiles intersect `upload.utm_tiles` are eligible. Original-CRS products are matched from the filename coordinate token. Common-CRS mosaics such as `LAEA` and `WGS84` use `mosaic_manifest.csv` `input_files` to recover the original source UTM tiles. The Upload tab's tile list prefers tiles that are actually represented by files in the current origin folder and are not already recorded as completed, skipped-existing, or EE-verified in `upload_report.csv`; if it cannot derive any local upload-ready tiles, it falls back to the global UTM list.

Before upload planning, GeeUp can list the destination Earth Engine collection with `ee.data.listAssets`. Existing asset IDs are written as `EE_VERIFIED_EXISTS` in `upload_report.csv` and skipped. This is more reliable than trusting the local upload report alone after an interrupted run. The latest listing is cached in `ee_asset_inventory.csv`. The Upload tab's tile list shows local upload-ready source tiles after filtering out files already recorded as `COMPLETED`, `SKIPPED_ALREADY_EXISTS`, or `EE_VERIFIED_EXISTS`; the status line also summarizes tiles already completed or verified in the upload report.

## Mosaic Output Naming

Mosaic output names remain upload-compatible. They use:

- the SWOT-compatible descriptor
- cycle ID
- pass ID
- scene ID `MOSA`
- minimum start timestamp across the group
- maximum end timestamp across the group
- original CRID when consistent, otherwise `MIXD`
- product counter `01`

Example:

```text
SWOT_L2_HR_Raster_100m_UTM28P_N_x_x_x_034_266_MOSA_20250618T055612_20250618T055713_PID0_01.tif
```

`MOSA` is the scene ID and is the only mosaic marker in new output names. Older outputs with an extra `_mosaic` suffix remain parseable, but GeeUp no longer creates that suffix.

When a mosaic combines different source CRIDs, the filename uses `MIXD` in the CRID position. The main mosaic report records the source CRIDs, source product counters, dominant CRID, preferred CRID by rank, and a `mixed_crid` flag. The focused `mixed_crid_mosaics.csv` report contains only mixed-CRID groups.

## Mosaic Raster Behavior

Mosaic outputs are written as uncompressed GeoTIFFs.

When `mosaic.write_world_file` is enabled, a `.tfw` file is written beside each output `.tif`.

Mosaic outputs keep the extraction-style band names:

- band 1: `wse`
- band 2: `wse_qual`

Singleton groups are written to the mosaic output folder with the mosaic output name. Original files are never moved or deleted.

## Overlap Rule

### `wse`

`wse` keeps the existing deterministic source-priority rule:

- sources are sorted by filename
- in overlaps, the first sorted source keeps priority when it has valid data

This behavior was left unchanged.

### `wse_qual`

`wse_qual` uses a more specific overlap rule to avoid seam artifacts from edge class `3`.

Per pixel, the mosaic chooses:

1. the first source, in current priority order, where `wse` is valid and `wse_qual` is `0`, `1`, or `2`
2. otherwise, the first source where `wse` is valid and `wse_qual` is `3`
3. otherwise, band-2 nodata

This means:

- `wse_qual=3` no longer overwrites a better `0/1/2` value from another overlapping tile
- `wse_qual=3` is still preserved when it is the only available quality class for a valid `wse` pixel
- the current filename-based priority rule is still the tie-breaker when both sources have valid `0/1/2` values

## Upload Compatibility

The uploader reads only `.tif` and `.tiff` inputs. Sidecar files such as `.tfw` are ignored.

Both original extracted files and mosaic files are compatible with the shared SWOT metadata parser.

Uploaded metadata includes:

- `system:time_start`
- `system:time_end`
- `swot_descriptor`
- `swot_grid_resolution`
- `swot_coordinate_system`
- `swot_granule_overlap`
- `swot_cycle_id`
- `swot_pass_id`
- `swot_scene_id`
- `swot_crid`
- `swot_product_counter`

For mosaic outputs, `swot_scene_id` is `MOSA`.

For original extracted files, `swot_scene_id` remains the original scene token such as `065F`.

## Environment Split

The project intentionally uses two environments:

- `.venv` for the GUI, duplicate removal, uploader, and small Earth Engine API utilities
- the GDAL conda environment for extraction and mosaicking

This split is deliberate. Heavy geospatial processing stays in the GDAL runtime, while the Selenium uploader stays lightweight and isolated.

## Optional Local Raw File Duplicate Removal

This section refers to the `Duplicate Removal` GUI tab. It is for local raw downloaded SWOT files before extraction. It is not the same as deleting images from an Earth Engine ImageCollection.

Use it only when the raw download folder contains repeated versions of the same SWOT granule.

For generic filenames, duplicate removal keeps the highest final `_NN` suffix.

For SWOT L2 HR Raster filenames, duplicate removal groups the same granule while ignoring only `CRID` and `ProductCounter`. The preferred file is selected by:

1. newer major CRID release, for example `D` over `C`
2. higher minor release inside the same major release, for example `PIC2` over `PIC0`
3. higher fidelity inside the same major/minor release, for example `PGD0` over `PID0`
4. highest product counter inside the same CRID, for example `PID0_02` over `PID0_01`

Moved-file logs include the kept and moved CRID/counter values and the reason for each move.

## Optional Earth Engine Collection Cleanup Utility

This utility removes many image assets from an Earth Engine ImageCollection after a mistaken or test upload. It is not part of the GUI because it is an occasional maintenance action, not a normal SWOT processing step.

Script:

```text
Utils/delete_ee_collection_children.py
```

Dependency:

- `earthengine-api` is included in `requirements.txt`, so a fresh `.venv` installation gets it with `python -m pip install -r requirements.txt`.
- The script can also run outside GeeUp if that Python environment has `earthengine-api` installed and Earth Engine authentication configured.

Recommended workflow:

1. Authenticate the Earth Engine API once.
2. Run a dry-run and review the CSV report.
3. Run with `--execute --yes` only after the planned delete list is correct.

Commands:

```powershell
.\.venv\Scripts\earthengine.exe authenticate
.\.venv\Scripts\python.exe Utils\delete_ee_collection_children.py --asset projects/YOUR_PROJECT/assets/YOUR_COLLECTION
.\.venv\Scripts\python.exe Utils\delete_ee_collection_children.py --asset projects/YOUR_PROJECT/assets/YOUR_COLLECTION --execute --yes
```

Default behavior:

- dry-run unless `--execute --yes` is provided
- deletes child `IMAGE` assets only
- keeps the parent ImageCollection
- prints progress during listing and deletion
- writes a timestamped CSV report under `reports/`

Use `--count-only` from a second PowerShell window to check how many child assets currently remain. The deletion run shows a compact progress bar by default; use `--verbose` only if you want one console line per asset. Use `--limit 10` for a small deletion test before deleting thousands of assets. Use `--delete-parent` only if the parent ImageCollection itself should also be removed.
