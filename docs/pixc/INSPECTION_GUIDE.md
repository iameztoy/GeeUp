# SWOTFlow PIXC Inspection Guide

The PIXC inspector helps understand real Pixel Cloud NetCDF files before the
workflow defines filtering, classification, export, or rasterization rules.

For a visual check against satellite imagery, use **Visualize Points** first.
The inspector remains the structural/statistical view of groups, variables, and
value distributions.

## How To Run

From the GUI:

1. Open `python swotflow_platform.py`.
2. Choose **Process Pixel Cloud / PIXC**.
3. Open **Inspect NetCDF**.
4. Choose one input NetCDF file.
5. Choose a report folder.
6. Click **Inspect File**.

From the command line:

```powershell
python -m products.pixc.inspect path\to\PIXC_file.nc --output-dir PIXC_Processing\00_logs\inspection
```

## Initial Inspection Outputs

For each inspected file, the first inspector should report:

- file path and file size
- collection/product metadata parsed from filename and/or global attributes
- NetCDF groups and nested groups
- dimensions and lengths
- variables, dtypes, shapes, dimensions, units, long names, and fill values
- point counts for key point-cloud groups
- latitude and longitude ranges
- height/elevation ranges
- classification values and counts
- quality flag values and counts
- missing/fill-value counts for key variables

## Priority Variables

The first pass should look for common PIXC point-cloud variables, including:

- latitude
- longitude
- height or water-surface elevation fields
- classification
- quality or geolocation quality flags
- sig0/backscatter fields
- water fraction fields

Variable names may differ by product version or group, so the inspector should
discover and report available variables rather than assuming one fixed schema.

## Report Formats

Initial reports should be easy to inspect and compare:

- JSON for full nested structure and summary metadata
- CSV tables for variables and value-count summaries

Point-data exports are a later step. Inspection summaries should come first.
