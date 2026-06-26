# SWOTFlow PIXC Getting Started

The PIXC workflow is currently a new product-family shell. Use this guide to
keep early PIXC testing separate from production HR Raster work.

## Launch

Start the product-family selector:

```powershell
python swotflow_platform.py
```

Choose **Process Pixel Cloud / PIXC**.

The direct HR Raster launcher, `python swotflow_gui.py`, remains available for
production raster processing.

## Project Separation

Use a separate project root for PIXC testing. Do not point early PIXC work at a
production HR Raster project folder.

Recommended pattern:

```text
D:\SWOTFlow_Projects\Raster_Production
D:\SWOTFlow_Projects\PIXC_Test
```

Each PIXC project has its own `project.yaml`, raw-download folder, logs, and
future point-processing outputs. PIXC project state is currently YAML plus CSV;
it does not use the HR Raster SQLite database.

Use **New Project**, **Open Project**, **Save Project**, and **Save Project As**
from the PIXC Home tab. Download and inspection actions require an open PIXC
project so files remain inside the project folder.

## Download PIXC Files

Open **Download** and set:

- Collection: `Version D active` unless you explicitly need Version C.
- Start date and end date. Use `YYYY-MM-DD`, for example `2026-01-01`, or use
  the **Pick** calendar buttons. The picker has direct year and month selectors,
  plus Today and Clear actions.
- Spatial mode: use **Bounding box**, **UTM tile (approx bbox)**, or **SWOT
  reference tile(s)** for normal testing.
- For SWOT reference tiles, click **Open SWOT Tile Map**, draw a rectangle or
  polygon on the satellite map, then click intersecting SWOT tiles such as
  `001_164L` before saving the selection back to the GUI.
- Optional SWOT track fields: use **Cycle**, **Pass**, and **Tile(s)** only when
  you want to constrain the CMR search by PIXC reference metadata. Pass filters
  require a cycle, and tile filters require both a cycle and a pass. Tile values
  can be comma-separated, for example `001F,102R`.
- Cycle is optional for selected SWOT reference tiles. The date range remains
  the main temporal filter. If you enter Cycle, SWOTFlow uses exact CMR
  cycle/pass/tile search; if Cycle is blank, it searches by date/spatial filter
  first and then filters parsed PIXC filenames locally by pass/tile/side.
- Max granules: keep the default low until you understand match counts.
- Output folder: managed by the active project as `<project>\01_raw_downloads`.

Use **Open AOI Map** when you want to choose the bounding box visually. The map
opens in your browser, includes a satellite imagery basemap and OpenStreetMap,
and sends the selected rectangle back to the Download tab.

Run **Preview Search** first. Review the table, matched count, known size, and
CSV report before running **Download Selected Files**. Use **Status filter** to
show only `MATCHED` rows when the preview also contains already downloaded or
older-version rows. Click **Select Matched** to select all currently matched
granules for download in one step. The first PIXC downloader writes standalone
CSV state in `<project>\00_logs`; it does not update the HR Raster project
database. Files already present locally or recorded as complete in the manifest
are skipped automatically.

The Download table shows the current live preview only. When you reopen a PIXC
project, use **Visualize Points** to see the persistent project inventory built
from `<project>\00_logs\pixc_download_manifest.csv` and
`<project>\01_raw_downloads`.

After a preview, use **Show Preview On Map** when CMR returned footprint
geometry for the matched granules. The browser map will draw those footprints;
click one or more granules, save the selection, and SWOTFlow will limit
**Download Selected Files** to that selected preview set.

## Visualize Downloaded Points

Open **Visualize Points** after downloading at least one PIXC NetCDF file.

1. Select one or more files from **Project Downloads**. The visualizer lists
   locally present project files and shows them as `Downloaded`.
2. Click **Use Selected Files**, or choose downloaded `.nc` files manually from
   `<project>\01_raw_downloads`.
3. Review **Variable Details** if you need to see the variables inside the
   first selected file.
4. Choose an attribute such as `/pixel_cloud/classification`.
5. Use **Load Values** to list the distinct values for that attribute. Select
   one or more values to filter the map, use **Select All Values** for all
   listed values, or clear the selection to map all values.
6. Confirm the latitude and longitude variables.
7. Keep `Max points/file` conservative for the first view; the default is
   `50000`.
8. Click **Open Point Map**.
9. In the browser viewer, use **Earth Engine Reference** when you want dated
   imagery. Choose start/end dates, use **Use SWOT Date** if appropriate,
   select Sentinel-2, Sentinel-1, Landsat 8, and/or Landsat 9, search images,
   then add selected results with the desired band combination.

The browser map opens with satellite imagery as the default basemap and
OpenStreetMap as an alternate reference layer. Points are sampled if the valid
point count is larger than the max-point cap. The selected attribute controls
point colors and the legend. When more than one file is opened, the map layer
control lets you turn each file on or off independently. If Earth Engine
reference imagery is available, dated Sentinel/Landsat overlays can be added
from the browser viewer and toggled in the same layer control. Searches use the
full valid coordinate bbox of the selected PIXC files, not just one rendered
sample point. No reference imagery is downloaded or uploaded; the map uses
temporary Earth Engine tile URLs. Use **Measure Distance** in the map panel to
click two PIXC points or map locations and draw a distance line.

## Optional NetCDF And Earth Engine Dependencies

The PIXC shell opens without extra dependencies, but real point visualization
and attribute-value filtering require `netCDF4` and `numpy`. Dated reference
imagery additionally requires `earthengine-api` and a working Earth Engine
authentication/project setup.

```powershell
python -m pip install -r requirements-pixc.txt
```

This installs `netCDF4`, `numpy`, and `earthengine-api` into the active Python
environment.

## Early Workflow

The intended first PIXC workflow is:

1. Create or open a PIXC project.
2. Select a small AOI from **Download**, either with bbox fields, one UTM tile,
   **Open AOI Map**, or **Open SWOT Tile Map**.
3. Set dates with `YYYY-MM-DD` text or the calendar picker.
4. Optionally add cycle/pass/tile filters when you know the SWOT reference
   metadata.
5. Preview the PIXC search.
6. Optionally use **Show Preview On Map** to select specific preview footprints.
7. Download only the selected previewed files.
8. Open **Visualize Points**.
9. Choose one or more downloaded files, load variables and attribute values,
   select the values to map, optionally enable dated Earth Engine reference
   imagery, and open the point map.
10. Decide filtering and export rules from the visual results and variable
    metadata shown in the visualizer.

## Dependencies

The shell uses the same lightweight GUI environment as the current raster app.
The optional PIXC dependency file is only needed when visualizing real NetCDF
files or using Earth Engine reference imagery.

The SWOT reference tile layer is stored as compact generated cache files under
`spatial_presets`. The original shapefile zip is not needed at runtime.
