# SWOTFlow Pixel Cloud / PIXC

This documentation covers the planned SWOT L2 HR Pixel Cloud product-family
workflow inside SWOTFlow.

PIXC is a NetCDF-4 point-cloud product, not a raster grid. It contains
geolocated water detections and variables such as latitude, longitude, height,
classification, quality flags, backscatter/sig0, and water fraction. The active
PO.DAAC collection currently targeted by the workflow shell is
`SWOT_L2_HR_PIXC_D`; Version C remains available as `SWOT_L2_HR_PIXC_2.0`.

## Current Scope

The PIXC module has an initial product shell, a cautious CMR preview/download
tool, and a sampled point visualizer. Its practical tools focus on finding
small PIXC test sets and understanding the files before defining filters or
export formats.

Initial capabilities:

- Preview PIXC granules from PO.DAAC/CMR by collection, date range, and optional
  bounding box.
- Open a browser AOI picker from the Download tab, switch between satellite
  imagery and OpenStreetMap, draw a rectangle, and return the bbox to the GUI.
- Use a direct year/month/day date picker that writes `YYYY-MM-DD`.
- Optionally filter previews by SWOT PIXC cycle, pass, and tile metadata.
- Select SWOT reference tile polygons from the browser AOI map using compact
  generated presets stored under `spatial_presets`.
- Show preview granule footprints on the browser map when CMR provides spatial
  geometry, then select one or more preview granules for download.
- Convert one UTM tile to an approximate bounding box as a convenience search
  helper.
- Create, open, save, and reopen PIXC projects with `project.yaml`.
- Download only the selected preview granules, skip already complete files, and
  write standalone CSV reports inside the active PIXC project folder.
- Track project downloads in a standalone manifest and expose local/downloaded
  PIXC files in the visualizer after reopening a project.
- Open one or more downloaded PIXC NetCDF files in **Visualize Points**, review
  variable details, choose an attribute plus coordinate variables, optionally
  select specific attribute values, and view sampled points on a browser map
  with satellite imagery and OpenStreetMap basemaps.
- Optionally add dated Earth Engine reference overlays from inside the browser
  point viewer. The viewer can search Sentinel-2, Sentinel-1, Landsat 8, and
  Landsat 9 images by date range and the full PIXC coordinate bbox, then add
  selected images with different band combinations.
- Color points by categorical or continuous NetCDF variables, with a map legend
  and a configurable max-point-per-file cap. Multi-file maps expose each file as
  a loadable/unloadable browser layer. Reference imagery overlays are temporary
  map tiles only, not local downloads or Earth Engine uploads.
- The lower **Variable Details** panel in **Visualize Points** is the current
  place to inspect available variables before mapping/filtering.

Planned next capabilities:

- Add user-defined filter previews for selected point variables.
- Inspect batches of local PIXC files.
- Add optional pass-line visual overlays from the generated reference-pass
  cache.

## Deferred Decisions

The first PIXC implementation should not assume Earth Engine upload. Likely
future export targets include GeoPackage, GeoParquet/Parquet, CSV, small
GeoJSON samples, and later rasterized GeoTIFF derivatives.

Filtering rules for high-quality water points should be based on inspection
results from real files, not hard-coded before the data is reviewed.

## Optional NetCDF And Earth Engine Dependencies

Real NetCDF point visualization and dated Earth Engine reference imagery require
optional dependencies:

```powershell
python -m pip install -r requirements-pixc.txt
```

The main HR Raster environment remains unchanged unless you install this
optional file. Earth Engine overlays also require prior Earth Engine
authentication and, for some accounts, a valid Cloud project id.

## Guides

- [Getting Started](./GETTING_STARTED.md)
- [Roadmap](./ROADMAP.md)
- [Troubleshooting](./TROUBLESHOOTING.md)
