# SWOT Processing Tools

This project is a local desktop tool written in Python. It has separated responsibilities:

- **Extraction:** uses the GDAL conda runtime to convert SWOT NetCDF files into two-band GeoTIFFs.
- **Mosaic:** uses the GDAL conda runtime to reduce GeoTIFF counts before Earth Engine upload.
- **Upload:** uses Selenium and your local Google Chrome browser to upload GeoTIFF files into Google Earth Engine through the Earth Engine web interface.
- **Optional cleanup:** includes local raw-file duplicate removal and Earth Engine ImageCollection cleanup utilities.

For detailed processing behavior, overlap rules, naming conventions, and workflow notes, see [SWOT_PROCESSING_GUIDE.md](./SWOT_PROCESSING_GUIDE.md).

The upload tool does **not** use Google Cloud Storage buckets. It simulates a normal user working in the Earth Engine Code Editor / Asset Manager UI.

## What This Tool Does

The script can:

### Extraction

1. Read cleaned `SWOT_L2_HR_Raster_100m` NetCDF files with GDAL.
2. Open the `wse` and `wse_qual` subdatasets.
3. Build a two-band VRT with band 1 `wse` and band 2 `wse_qual`.
4. Write one GeoTIFF per input NetCDF.
5. Keep original CRS by default, or optionally reproject to Africa LAEA or WGS84 using the same GDAL workflow as the notebook.
6. Write extraction manifest and errors CSVs.

### Mosaic

1. Scan SWOT GeoTIFF files before upload.
2. Group files by SWOT descriptor, cycle ID, pass ID, start date, and exact UTM token.
3. Optionally group reprojected common-CRS outputs by whole pass/date, ignoring the original UTM token.
4. Build GDAL VRT mosaics and export upload-ready GeoTIFFs.
5. Copy singleton groups into the mosaic output folder so that folder can be uploaded as a complete replacement.
6. Write `reports/mosaic_report.csv`.

### Upload

1. Open normal Chrome with a dedicated reusable profile folder and attach Selenium to it.
2. Open the Earth Engine Code Editor.
3. Pause for manual login if needed.
4. Open the image upload dialog.
5. Send local GeoTIFF file paths directly into the page's file input.
6. Fill the destination asset ID.
   If the UI uses separate destination collection and image name fields, the script now fills those instead.
7. Optionally apply pyramiding policy settings.
8. Optionally add SWOT metadata properties such as `system:time_start`, `system:time_end`, cycle, pass, scene, CRID, and product counter.
9. Submit uploads in batches.
10. Watch the Tasks panel for ingestion progress.
11. Wait for the queue to drain before starting the next batch.
12. Save logs, screenshots, HTML dumps, and a CSV report.

### Optional Local Raw File Duplicate Removal

1. Scan raw downloaded SWOT files whose names end in a numeric version suffix such as `_01`, `_02`, or `_05`.
2. Keep the preferred file version in place.
3. Move older versions into a `moved` subfolder inside the selected input folder.
4. Write a timestamped text log under the configured local logs folder.

## Limitations and Warnings

- This is **unofficial UI automation**. If Google changes the Earth Engine interface, selectors may need to be updated.
- Earth Engine uploads through the web UI are slower and more fragile than API-based imports.
- This tool intentionally uses Selenium and Chrome only, because that was the design requirement.
- Heavy geospatial processing intentionally runs in a separate GDAL conda environment. Do not install pip GDAL into `.venv` unless you know exactly why you need it.
- The script tries to detect asset-exists cases from the UI. That detection is best-effort because it depends on the current Earth Engine interface.
- Earth Engine's UI has upload limits. Google documents UI GeoTIFF uploads in the Code Editor and notes that larger files may require the command line instead of the UI. See the official guide: [Importing raster data](https://developers.google.com/earth-engine/guides/image_upload).
- You still need a valid Google account and Earth Engine access.

## Project Files

- `ee_uploader_gui.py`: beginner-friendly desktop launcher with Duplicate Removal, Extraction, Mosaic, and Upload tabs
- `swot_duplicate_remover.py`: duplicate-cleanup script for raw downloaded SWOT files
- `swot_extract_tool.py`: GDAL-backed SWOT NetCDF-to-GeoTIFF extraction script
- `ee_ui_uploader.py`: Earth Engine upload automation script
- `ee_mosaic_tool.py`: GDAL-backed SWOT GeoTIFF mosaic script
- `gdal_runtime.py`: helper for launching GDAL scripts through the conda runtime
- `swot_metadata.py`: shared SWOT filename parser
- `selectors.py`: all UI locators in one place
- `config.example.yaml`: sample configuration
- `requirements.txt`: upload/UI `.venv` dependencies, plus the optional Earth Engine API utility dependency
- `environment_swot_gdal.yml`: GDAL conda environment definition for extraction and mosaicking
- `SWOT_PROCESSING_GUIDE.md`: detailed processing rules, naming behavior, and overlap logic
- `TROUBLESHOOTING.md`: common failures and fixes
- `Utils/delete_ee_collection_children.py`: optional utility to empty an Earth Engine ImageCollection without Code Editor confirmation popups

## Prerequisites

You need:

1. Windows with Google Chrome installed.
2. Python 3.11 or newer.
3. A Google account that can access Earth Engine.
4. A folder on disk that contains your GeoTIFF files.
5. A Python virtual environment for the launcher and uploader. This tool is designed to refuse running from your global Python installation.
6. Miniforge or another conda-compatible installer if you want to run the Extraction or Mosaic tabs.

## Environment Strategy

GeeUp intentionally uses two Python environments:

- `.venv`: local project environment for the launcher, duplicate remover, Earth Engine uploader, and small Earth Engine API utilities. It contains Selenium, PyYAML, and `earthengine-api`.
- GDAL conda environment: processing runtime for mosaicking now and NetCDF extraction later. It contains GDAL, `libgdal-netcdf`, NumPy, and tqdm.

Do not try to merge these by default. GDAL on Windows is more reliable from conda-forge, while the uploader is simpler and safer in a lightweight `.venv`.

Recommended project-local GDAL runtime:

```text
C:\path\to\GeeUp\.conda\swot_gdal\python.exe
```

Alternative external GDAL runtime:

```text
C:\path\to\conda_envs\swot_gdal\python.exe
```

The `.conda/` folder is ignored by Git. The environment can live inside the project folder for portability, but the environment binaries themselves should not be committed.

## Step 1: Install Python

If Python is not installed:

1. Go to [python.org/downloads](https://www.python.org/downloads/).
2. Download Python 3.11 or newer.
3. Run the installer.
4. On the first installer screen, enable **Add Python to PATH**.
5. Finish the installation.

To verify Python, open PowerShell and run:

```powershell
python --version
```

You should see something like `Python 3.11.x` or newer.

## Step 2: Install Google Chrome

Install regular Google Chrome if it is not already installed.

To verify, open Chrome normally and make sure it starts without errors.

## Step 3: Open the Project Folder

Open PowerShell in this project folder:

```powershell
cd C:\path\to\GeeUp
```

## Step 4: Create and Activate a Virtual Environment

This project requires an isolated Python environment. The script exits on purpose if you try to run it from your global Python installation.

Create the environment:

```powershell
python -m venv .venv
```

Activate it in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

When activation works, your PowerShell prompt usually starts with `(.venv)`.

Keep that environment activated for all remaining commands in this README.

If PowerShell blocks the activation script, run this first in the same terminal window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Then run the activation command again.

## Step 5: Install Python Packages

Make sure the virtual environment is activated first. Then run:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Selenium 4 includes Selenium Manager, which usually handles ChromeDriver automatically.

## Step 5A: Install the GDAL Processing Environment

This step is required for the **Extraction** and **Mosaic** tabs. It is not required if you only use duplicate removal or the Earth Engine uploader.

Recommended current setup:

1. Install Miniforge if it is not already installed.
2. Open **Miniforge Prompt**.
3. Go to this project folder:

```powershell
cd C:\path\to\GeeUp
```

4. Create a local GDAL environment inside GeeUp:

```powershell
conda env create --prefix .\.conda\swot_gdal --file environment_swot_gdal.yml
```

If you use `mamba`, this equivalent command is usually faster:

```powershell
mamba env create --prefix .\.conda\swot_gdal --file environment_swot_gdal.yml
```

5. Verify that GDAL and the required drivers are available:

```powershell
.\.conda\swot_gdal\python.exe ee_mosaic_tool.py --check-gdal
```

Expected output should include:

```text
required_drivers=GTiff,VRT,netCDF
```

If you prefer an external GDAL environment, set the launcher field to that local Python path, for example:

```text
C:\path\to\conda_envs\swot_gdal\python.exe
```

Either location is valid. The launcher has a **GDAL Python** field in the Extraction and Mosaic tabs. Set it to whichever GDAL Python executable you want to use.

Important:

- Keep `.venv` for upload/UI dependencies.
- Keep `.conda\swot_gdal` or another local conda GDAL environment for GDAL processing.
- Do not commit `.conda/`; it is machine-specific and can be recreated from `environment_swot_gdal.yml`.

## Step 6: Prepare a Dedicated Chrome Profile for This Tool

There is no extra command to run in this step.

This step only means: let the uploader use a separate Chrome data folder so your Earth Engine login for this tool stays separate from your normal browsing.

Using a dedicated profile is important. It keeps your Earth Engine login persistent and avoids interfering with your normal Chrome session.

Recommended approach:

1. Leave the config value `chrome.user_data_dir` set to `./chrome-profile`.
2. Leave `chrome.connection_mode` set to `attach`.
3. Let the script create that folder automatically.
4. Use the same folder every time you run the uploader.
5. On a real run, the tool starts a normal Chrome window with that profile and then attaches Selenium to it.
6. If login is needed, you log in manually in that normal Chrome window.

Alternative manual approach:

1. Create a folder such as `C:\ee-uploader-profile`.
2. Put that path in `chrome.user_data_dir`.
3. Run the tool once.
4. Log in when the browser opens.

Important:

- Do not point the tool at a Chrome profile that is currently open in another Chrome window.
- If Chrome says the profile is locked, close all Chrome windows that use the same profile.

If you use the new desktop launcher, you will see this field as **Chrome profile folder**. You can usually leave it at the default value.

## Step 7: What Will Happen on Your First Real Upload Later

You do **not** perform this step right now.

Right now, after finishing step 6, continue to **step 8**.

This section is only explaining what will happen later, when you eventually start a real upload from:

- the desktop launcher with `Save And Run Real Upload`, or
- the command `python ee_ui_uploader.py --config config.yaml`

On that first real upload:

1. The tool starts a normal Chrome window using your dedicated profile folder.
2. Selenium attaches to that Chrome window.
3. If you are not logged in, it tells you to log in manually.
4. Complete the Google sign-in flow in that Chrome window.
5. Keep the Chrome window open.
6. Wait until the Earth Engine Code Editor is visible.
7. Return to PowerShell and press Enter.

After that, the dedicated profile should usually keep you signed in for later runs.

## Step 8: Choose How You Want to Fill the Settings

You now have two ways to set up the uploader.

### Option A: Use the Desktop Launcher

This is the easiest option for most users.

Run:

```powershell
python ee_uploader_gui.py
```

Default processing folders:

- `<LOCAL_PROCESSING_ROOT>\01_raw_downloads`: raw manually downloaded SWOT files
- `<LOCAL_PROCESSING_ROOT>\02_extracted_geotiffs`: future NetCDF extraction outputs
- `<LOCAL_PROCESSING_ROOT>\03_mosaics`: upload-ready mosaic GeoTIFFs
- `<LOCAL_PROCESSING_ROOT>\00_logs`: processing reports and logs

The window lets you fill:

Extraction tab:

- GDAL Python executable
- input NetCDF folder
- output GeoTIFF folder
- CRS mode
- year selection
- optional file limit
- skip existing outputs
- manifest CSV
- errors CSV

For extraction, click:

- `Save Config`
- `Plan Extraction`
- `Run Extraction`

Mosaic tab:

- GDAL Python executable
- mosaic input folder
- mosaic output folder
- grouping mode
- target CRS label for common-CRS mosaics
- mosaic report CSV
- recursive scan
- overwrite existing outputs
- write `.tfw` world files

Upload tab:

- origin folder with GeoTIFF files
- destination image collection
- batch size
- max active ingestions
- prefix / suffix
- pyramiding policy
- SWOT metadata properties from the filename
- resume mode
- dry-run mode
- Chrome profile folder

Optional Duplicate Removal tab:

- raw SWOT download input folder
- moved subfolder name
- duplicate-removal log folder
- recursive scan toggle

Use this tab only when the raw download folder contains repeated SWOT granule versions.

For mosaicking, click:

- `Save Config`
- `Plan Mosaics`
- `Run Mosaic`

After a successful mosaic run, the launcher can set the Upload tab's origin folder to the mosaic output folder.

For uploading, click:

- `Save Config`
- `Open Chrome For Manual Login`
- `Save And Run Dry Run`
- or `Save And Run Real Upload`

The launcher writes `config.yaml` for you and starts the uploader in a separate console window.
The two run buttons override the run type for that launch, so `Save And Run Dry Run` always performs a dry run and `Save And Run Real Upload` always performs a real upload.
Before clicking either run button, make sure **Origin folder** points to a real folder on your machine that actually contains `.tif` or `.tiff` files. If it still points to the example folder, the run may finish immediately with "No matching GeoTIFF files were found".

If you previously saw Google's "This browser or app may not be secure" warning, use `Open Chrome For Manual Login` first:

1. Click `Open Chrome For Manual Login`
2. Sign in to Earth Engine in that normal Chrome window
3. Confirm Earth Engine opens correctly
4. Close that Chrome window
5. Return to the launcher and run the uploader

### Option B: Edit config.yaml Manually

If you prefer editing YAML, follow the manual method below.

Copy the example file:

```powershell
Copy-Item .\config.example.yaml .\config.yaml
```

Then edit `config.yaml`.

`config.yaml` is intended to stay local and is ignored by Git. Keep `config.example.yaml` as the tracked template.

Minimum values to change:

- `input_folder`
- `destination_parent`

Common values to review:

- `gdal.python`
- `mosaic.input_folder`
- `mosaic.output_folder`
- `extract.input_folder`
- `extract.output_folder`
- `extract.target_crs_mode`
- `extract.year_selection`
- `mosaic.grouping_mode`
- `mosaic.target_crs_label`
- `mosaic.overwrite`
- `mosaic.report_csv`
- `mosaic.mixed_crid_report_csv`
- `upload.batch_size`
- `upload.max_active_ingestions`
- `upload.prefix`
- `upload.suffix`
- `metadata.enabled`
- `metadata.require_match`
- `metadata.add_end_time`
- `execution.dry_run`
- `execution.resume`

## SWOT Extraction Workflow

Use the Extraction tab to convert cleaned SWOT NetCDF files into GeoTIFF inputs for mosaicking.

The extraction tool follows the working notebook logic: it opens `wse` and `wse_qual` with GDAL, validates georeferencing, builds a two-band VRT, and writes one GeoTIFF per input NetCDF. It does not add GeoTIFF compression options in v1. Mosaic outputs also avoid GeoTIFF compression by default so the output files follow the same uncompressed GeoTIFF approach.

Output filename convention:

- `original`: `<netcdf_stem>.tif`
- `africa_laea`: `<netcdf_stem>_africa_laea.tif`
- `wgs84`: `<netcdf_stem>_wgs84.tif`

Recommended workflow:

1. Keep cleaned NetCDF files in your configured raw downloads folder.
2. Open `python ee_uploader_gui.py`.
3. In the Extraction tab, set **GDAL Python** to the GDAL conda Python executable.
4. Set **Input NetCDF folder** and **Output GeoTIFF folder**.
5. Choose CRS mode: `original`, `africa_laea`, or `wgs84`.
6. Click `Plan Extraction`.
7. Click `Run Extraction`.
8. After a successful run, the launcher points Mosaic input to the extraction output folder and sets Mosaic grouping based on the CRS mode.

Manual dry run:

```powershell
.\.conda\swot_gdal\python.exe swot_extract_tool.py --config config.yaml --dry-run
```

Manual extraction run:

```powershell
.\.conda\swot_gdal\python.exe swot_extract_tool.py --config config.yaml
```

## Example Config Choices

If your GeoTIFFs are in:

```text
C:\path\to\rasters
```

and your Earth Engine parent is:

```text
projects/MY_PROJECT/assets/MY_COLLECTION
```

then each file uploads as:

```text
projects/MY_PROJECT/assets/MY_COLLECTION/<generated_name>
```

By default, `<generated_name>` is the file stem after cleanup.

## SWOT Mosaic Workflow

Use the Mosaic tab before upload when the number of GeoTIFFs is too high for Earth Engine asset/task limits.

The mosaic tool groups files by:

- full SWOT descriptor, for example `100m_UTM36M_N_x_x_x`
- cycle ID
- pass ID
- start date from `RangeBeginningDateTime`
- exact UTM token, for example `UTM36M`

This is the default `utm_zone` grouping mode and should be used when extraction keeps the original SWOT/UTM projection.

If extraction reprojects all outputs to one common CRS, use `pass_date_common_crs` grouping mode. In that mode, the mosaic tool ignores the original UTM token in the filename and groups the whole cycle/pass/date together, but still validates the actual rasters with GDAL before merging. Set `mosaic.target_crs_label` to `LAEA` or `WGS84` so output filenames clearly describe the common CRS.

If extraction keeps the original projection but you want fewer files than exact-token grouping produces, use `utm_zone_hemisphere`. This groups filename tokens such as `UTM30P`, `UTM30Q`, `UTM30R`, and `UTM30S` as `UTM30N`, while keeping southern-hemisphere latitude bands separate as `UTM30S`. GDAL compatibility validation still runs before a mosaic is written.

Each output filename remains compatible with the SWOT metadata parser, using `MOSA` as the scene ID and `_mosaic` as the suffix.

Mosaic outputs are written as uncompressed GDAL GeoTIFFs. When `mosaic.write_world_file` is true, the tool also writes a `.tfw` world file next to each output GeoTIFF; the GeoTIFF still keeps embedded georeferencing.

Mosaic outputs keep two bands named `wse` and `wse_qual`, matching the extraction outputs.

If all source files in one mosaic group share the same CRID, that CRID is kept in the mosaic filename. If source CRIDs differ, the mosaic filename uses `MIXD` so the uploader can still parse it without pretending the mosaic came from a single product version. The standard mosaic report includes mixed-CRID diagnostics, and `mixed_crid_mosaics.csv` lists only those groups for review.

Recommended workflow:

1. Use the extraction workflow or another source to create SWOT GeoTIFFs.
2. Open `python ee_uploader_gui.py`.
3. In the Mosaic tab, set **GDAL Python** to a valid conda GDAL Python executable.
4. Set **Mosaic input folder** to the folder containing original GeoTIFF tiles.
5. Set **Mosaic output folder** to a different folder, such as `.\mosaics`.
6. Click `Plan Mosaics`.
7. Review `reports/mosaic_report.csv`.
8. Click `Run Mosaic`.
9. Use the mosaic output folder as the Upload tab origin folder.

Manual dry run:

```powershell
.\.conda\swot_gdal\python.exe ee_mosaic_tool.py --config config.yaml --dry-run
```

Manual mosaic run:

```powershell
.\.conda\swot_gdal\python.exe ee_mosaic_tool.py --config config.yaml
```

If your GDAL environment is outside the project, use its Python path instead, for example:

```powershell
C:\path\to\conda_envs\swot_gdal\python.exe ee_mosaic_tool.py --config config.yaml
```

The launcher sets the required GDAL environment variables automatically when it starts the mosaic process. If you run the mosaic script manually, prefer doing it from Miniforge Prompt or with an activated conda environment.

## SWOT Metadata Properties

Metadata parsing is enabled by default for SWOT L2 HR Raster filenames like:

```text
SWOT_L2_HR_Raster_100m_UTM01C_N_x_x_x_034_287_002F_20250618T233535_20250618T233556_PID0_01.tif
```

The uploader parses the two timestamp fields and fills:

- `system:time_start`: `2025-06-18 23:35:35`
- `system:time_end`: `2025-06-18 23:35:56`

It also adds custom string properties such as:

- `swot_cycle_id`: `034`
- `swot_pass_id`: `287`
- `swot_scene_id`: `002F`
- `swot_crid`: `PID0`
- `swot_product_counter`: `01`

The values stay as strings so leading zeros are preserved. If your files do not use the SWOT naming pattern, either turn metadata off with `metadata.enabled: false` or set `metadata.require_match: false` to upload unmatched files without metadata.

## Step 9: Run a Dry Run

If you are using the desktop launcher, click `Save And Run Dry Run`.

If you are using the manual CLI method, run:

```powershell
python ee_ui_uploader.py --config config.yaml --dry-run
```

Dry run is the safest first test. It scans files, builds asset IDs, writes the CSV report, and does **not** click the final upload button.

What to expect:

1. The script scans the input folder.
2. It shows planned file-to-asset mappings in the console.
3. It previews parsed SWOT metadata in the console and CSV report when filenames match.
4. It writes `reports/upload_report.csv`.
5. No browser upload is submitted.

## Step 10: Run a Real Upload

If you are using the desktop launcher, click `Save And Run Real Upload`.

If you are using the manual CLI method:

When you are ready:

1. Edit `config.yaml` and set `execution.dry_run: false`, or keep it false in the file.
2. Run:

```powershell
python ee_ui_uploader.py --config config.yaml
```

The script asks for confirmation before the first real upload.

If Google asks you to sign in at that moment, follow the instructions described earlier in **step 7**.

If you want to skip that confirmation:

```powershell
python ee_ui_uploader.py --config config.yaml --yes
```

## Interactive Mode

If you do not want to prepare a config file first, you can run:

```powershell
python ee_ui_uploader.py
```

The script prompts you for:

- local folder
- destination asset path
- batch size
- prefix
- suffix
- pyramiding policy
- resume mode

Interactive mode is useful for quick tests. A config file is better for repeatable runs.

For most users, the desktop launcher is easier than interactive terminal prompts.

## How Resume Mode Works

Resume mode reads the CSV report file and skips assets already recorded as:

- `SUBMITTED`
- `READY`
- `RUNNING`
- `COMPLETED`
- `SKIPPED_ALREADY_EXISTS`

This means:

- if the script stops halfway, you can rerun it
- files already submitted in a previous run are skipped
- failed items are not automatically protected from retry unless they already resolved into a skip/completed state

Default report path:

```text
reports/upload_report.csv
```

## Logs, Screenshots, and Reports

The script creates:

- `logs/`: run logs
- `artifacts/`: screenshots and HTML dumps when something fails
- `reports/upload_report.csv`: upload manifest and result report
- `reports/mosaic_report.csv`: mosaic plan/result report

The CSV includes:

- `local_file`
- `asset_id`
- `batch_number`
- `submit_time`
- `detected_task_name`
- `final_status`
- `error_message`
- `metadata_start_time`
- `metadata_end_time`
- `metadata_properties`
- `metadata_status`

## How Selectors Are Organized

All major Earth Engine UI selectors are centralized in `selectors.py`.

That file contains fallbacks for:

- Assets tab
- Tasks tab
- NEW button
- Image upload button
- file input
- asset ID field
- destination collection field
- image name field
- pyramiding policy controls
- metadata Properties controls
- upload button
- task rows

If Google changes the UI, `selectors.py` is the first place to edit.

## How to Inspect Selectors in Chrome

If a button stops being found:

1. Open Earth Engine manually in Chrome.
2. Press `F12` to open DevTools.
3. Click the element picker icon.
4. Click the button or field you care about.
5. Look for stable attributes such as:
   - `aria-label`
   - `role`
   - placeholder text
   - button text
6. Update the matching section in `selectors.py`.
7. Run a dry run again first.

Good selector strategy:

1. Prefer stable attributes.
2. Then use visible button text.
3. Keep generic XPath fallbacks last.

## What to Do If Google Changes the UI

If the upload flow changes:

1. Open `selectors.py`.
2. Adjust the locator list for the broken element.
3. Keep the most reliable selector first.
4. Run dry-run mode.
5. Only after that, run a real upload.

If the workflow itself changes substantially, you may also need to update logic in `ee_ui_uploader.py`.

## Common Errors and Fixes

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for detailed guidance.

Typical examples:

- element not found
- stale element reference
- upload dialog not opening
- login not detected
- task stuck in READY or RUNNING
- asset already exists
- Chrome profile locked
- Chrome/driver mismatch

## Notes on File Uploads

This project does **not** try to automate the native Windows file picker. That is intentional.

Instead, it finds the file input element in the web page and sends local file paths directly using Selenium. This is more reliable for automation and avoids OS dialog handling problems.

## How to Use This Project in 5 Minutes

1. Install Python 3.11+ and Google Chrome.
2. Create and activate the local environment with `python -m venv .venv` and `.\.venv\Scripts\Activate.ps1`.
3. Run `python -m pip install --upgrade pip` and `python -m pip install -r requirements.txt`.
4. Run `python ee_uploader_gui.py`.
5. If you only need upload, choose your origin folder and destination image collection in the Upload tab.
6. Click `Save And Run Dry Run`.
7. If the plan looks right, click `Save And Run Real Upload`.

If you need extraction or mosaics first:

1. Install Miniforge.
2. Create the GDAL environment with `conda env create --prefix .\.conda\swot_gdal --file environment_swot_gdal.yml`.
3. Verify it with `.\.conda\swot_gdal\python.exe swot_extract_tool.py --check-gdal`.
4. Open `python ee_uploader_gui.py`.
5. In the Extraction tab, set **GDAL Python** to `C:\path\to\GeeUp\.conda\swot_gdal\python.exe` or to another local GDAL conda Python executable.
6. Click `Plan Extraction`, then `Run Extraction`.
7. Click `Plan Mosaics`, then `Run Mosaic`.
8. Upload the mosaic output folder from the Upload tab.

## Official Earth Engine References

- [Importing raster data](https://developers.google.com/earth-engine/guides/image_upload)
- [Managing assets](https://developers.google.com/earth-engine/guides/manage_assets)

## Unofficial Tool Warning

This project is an unofficial browser automation helper. It may break if Earth Engine changes the interface, HTML structure, labels, or task display behavior.

Use dry-run mode first, keep an eye on the browser during early tests, and expect occasional selector maintenance over time.

## Optional Cleanup and Maintenance

These are not the main processing path, but they are available when needed.

### Local Raw File Duplicate Removal

The `Duplicate Removal` tab can clean a raw download folder before extraction when repeated versions of the same SWOT granule exist. It keeps the preferred version in place and moves older versions to a `moved` subfolder. See `SWOT_PROCESSING_GUIDE.md` for the CRID/product-counter ranking rules.

### Earth Engine Collection Cleanup Utility

`Utils/delete_ee_collection_children.py` can empty an Earth Engine ImageCollection after a mistaken or test upload without clicking thousands of Code Editor confirmation popups. It uses the Earth Engine Python API, runs as a dry-run by default, keeps the parent ImageCollection unless explicitly told otherwise, and shows a compact progress bar during deletion. See `SWOT_PROCESSING_GUIDE.md` for the full command sequence and safety options.
