# SWOT Processing Guide

This document keeps the processing-specific behavior that is too detailed for the main README.

## Processing Order

The current GUI workflow is:

1. `Duplicate Removal`
2. `Extraction`
3. `Mosaic`
4. `Upload`

Default working folders:

- `<LOCAL_PROCESSING_ROOT>\01_raw_downloads`
- `<LOCAL_PROCESSING_ROOT>\02_extracted_geotiffs`
- `<LOCAL_PROCESSING_ROOT>\03_mosaics`
- `<LOCAL_PROCESSING_ROOT>\00_logs`

The detailed sections below prioritize the main processing path: extraction, mosaicking, upload compatibility, and runtime setup. Optional cleanup and maintenance utilities are documented at the end.

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
- suffix `_mosaic`

Example:

```text
SWOT_L2_HR_Raster_100m_UTM28P_N_x_x_x_034_266_MOSA_20250618T055612_20250618T055713_PID0_01_mosaic.tif
```

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
