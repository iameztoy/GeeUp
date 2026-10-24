# SWOTFlow Getting Started

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

During execution, Automation reports `Stage execution`, an aggregate percentage based on completed or skipped tile-stage units, together with the current tile position and stage. This gives a stable overview for large queues while detailed file progress remains in the status message and stage logs. A 100% stage-execution value means all configured stages were attempted; the final message separately reports any mosaics still awaiting upload verification. Tiles already complete for the requested date range are skipped on resume; cleanup is not rerun for those complete tiles. If a CMR/Earthdata metadata search times out, the tile is left resumable, later tiles can continue, and Automation retries deferred CMR/download-search failures after the first tile pass.

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

## Move A Project To Another Computer

Use **Home > Relocate / Validate Project** for a copied project. **Save Project As** copies settings into a new project; it does not transfer processing history or data.

There are two separate things to move:

| Item | What to do on the destination computer |
| --- | --- |
| SWOTFlow application | Get the same code version from GitHub and install fresh Python/GDAL environments. |
| Existing project | Copy the entire project folder, then relocate/validate that copy. Its SQLite database preserves completed work. |
| Machine setup | Sign in again and set the new GDAL executable and Chrome profile paths. Credentials and running browser transfers are not portable. |

To obtain the application with Git, run these commands in a parent folder where you want a new `SWOTFlow` directory:

```powershell
git clone --branch main https://github.com/iameztoy/GeeUp.git SWOTFlow
cd SWOTFlow
```

The repository is still named `GeeUp`; the application is SWOTFlow. Alternatively, download and extract the repository's ZIP from GitHub. Follow [Install Python Packages](#install-python-packages) and [Install The GDAL Processing Environment](#install-the-gdal-processing-environment) on the destination. A GitHub clone/ZIP contains the application, **not** your project data or processing history. There is no one-click portable application/project bundle yet.

For a project such as `D:\SWOT_e2e`:

To reduce the copy size first, use [Diagnostic Cleanup](#optional-diagnostic-cleanup) for old uploader screenshots/HTML and optional text logs. Use the normal proof-based Cleanup preview for intermediate rasters. Do not remove the entire `00_logs` folder: it also contains essential update/resume records and recovery backups.

1. Stop Automation with **Stop After Current Stage**, and wait for it to stop. Finish or stop other processing and upload processes. Browser-side file transfers cannot move between computers; let them finish if possible, then use **Sync EE Assets**. Keep any unverified mosaics.
2. Save the project and close SWOTFlow and its separate processing/upload terminals. Copy the **entire project folder** to your external drive, including `project.yaml`, `swotflow.sqlite3`, `00_logs`, `profiles`, and all remaining raw/extracted/mosaic files and sidecars. Do not copy an actively changing database. If SQLite `-wal`/`-shm` companion files remain, keep them with the database; never discard a WAL file manually.
3. Install the same updated SWOTFlow code on the other computer and recreate its Python and GDAL environments as described above. Do not copy `.venv`, the conda environment, Chrome profiles, or credentials. Do not reuse the old application's `config.yaml`; open the transferred project explicitly. With Git, compare `git rev-parse HEAD` on both computers to confirm they have the same code revision before continuing. Include any local fixes that have not yet been pushed to GitHub.
4. Copy the project from the external drive to its **final working location** on the destination computer. You may also work directly from a writable, locally attached external drive, but keep its drive letter/path stable and keep it connected. Do not run the project from a network share or synchronize an active SQLite database between computers.
5. Start SWOTFlow. In **Home > Relocate / Validate Project**, browse to the copied `project.yaml`, confirm all project processes are stopped, and click **Relocate Copy**. Normal Open Project also detects a changed root and directs you to this dialog. The old root is mapped automatically; normally no additional mappings are needed for `D:\SWOT_e2e`.
6. Wait for relocation and validation to finish. Large histories can take time; progress is shown without blocking the dialog. A rollback backup is retained under `.swotflow-relocation`, and the validation report is written to `00_logs/relocation_validation.json`. Files deliberately removed after downstream completion are not treated as lost work. Missing files without proof are listed and prevent opening the relocated project: restore them from the original copy, then click **Validate** again.
7. Click **Open Project**. Check the GDAL Python executable and Chrome configuration, authenticate Earthdata and Earth Engine on this computer, then run a **fresh Automation preflight** with the intended tiles/dates and click **Start Automation**. Do not immediately resume an old machine's preflight: relocated checkpoints require a new one. Preflight and processing retain the original manifests, campaign IDs and verified completion history; moving a project does not reset completed work. Upload-enabled preflight synchronizes EE assets. Refresh Statistics and preview Cleanup before deleting anything.

If the destination uses exactly the same absolute project path, use **Validate**, then still run a fresh preflight after checking the new machine's setup. Keep the original computer's project unchanged until you have successfully continued on the destination. Use only one copy as the working project; this feature does not merge two independently updated copies.

Moving the project does not move or rename its Earth Engine collection. Keep the same destination asset path if you want to continue that collection, and use a Google account with access to it. The GDAL setting must point to the destination machine's `python.exe`, not merely its environment folder. These instructions describe Windows-to-Windows transfer; a full Windows-to-macOS processing/browser migration has not been validated.

If you decide to stay on the original computer, no relocation is needed: open the existing project normally. A copy on an external drive can remain a backup until you actually switch computers.

Validation checks SQLite integrity, history counts, recorded file presence and downstream proof. It does **not** checksum every raster or verify all raster pixels. Use a reliable copy operation and retain the original as a fallback. The external drive must support files larger than 4 GB when your project contains them; FAT32 is unsuitable in that case.

For external data folders, interrupted relocation recovery and the command-line equivalent, see [Project Relocation And Recovery](./SWOT_PROCESSING_GUIDE.md#project-relocation-and-recovery).

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

If a real upload was accepted by the browser but the task or final asset has not yet been verified, project records may show `SUBMITTED_PENDING_VERIFICATION` or `SUBMITTED`. After the Earth Engine assets have appeared in the target collection, use `Sync EE Assets` in the Upload tab. It checks both destination assets and recent ingestion operations. A dialog closure with no corresponding asset or operation becomes retryable instead of remaining pending indefinitely. `Select Retryable Uploads` replaces any older manual tile filter with all tiles represented by unresolved local mosaics; a subsequent real upload skips verified assets and retries only the missing ones. Upload status transitions remain in the project database and are exported to `00_logs/upload_events.csv`.

For the current Earth Engine dialog, SWOTFlow waits for the browser-side upload dialog to close and records `SUBMITTED_PENDING_VERIFICATION`. `Max pending browser uploads` then limits how many clicked files may remain in Chrome before Earth Engine acknowledges them as ingestion operations; use the default `3`, or `1` on a slow or unstable link. Visible percentage/progress changes keep the dialog wait alive, and an open dialog with no detectable progress is recovered and retried. `Upload completion mode` controls tile handoff: `Wait for EE verification before next tile` waits for active EE tasks, while `Submit and continue; verify later` runs a non-blocking task/inventory reconciliation and lets Automation move to later tiles. EE sync verifies successful assets, while unverified mosaics remain on disk. After an upload-enabled Automation queue, one deferred repair pass retries unresolved mosaics without repeating download, extraction, or mosaicking, then runs EE sync and verified cleanup. A later manual Upload run is also safe because Automation and Upload use the same SQLite upload records, asset IDs, Statistics, and Cleanup proof.

Real upload confirmation is shown in the SWOTFlow window. GUI-launched uploads no longer wait for a separate `YES` in the console; the console remains available for logs.

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

#### Return To An Unfinished Or Mistaken Update

1. Open the project and use **Statistics > Update Runs > Update Windows**. Each row is a saved date window, not a running job. **Status Map** also shows the current execution state separately.
2. To finish an older window, select it and click **Continue Selected Update**. This restores its dates and tiles and starts preflight. With `Include upload` enabled, preflight synchronizes Earth Engine first. Leave **Refresh remote catalogue during preflight** enabled when returning after a long gap, to discover late products or new processing versions.
3. Review the queue and click **Start Automation**, unless automatic start is enabled. Completed work is reused; unresolved work is retried. Refresh Statistics afterwards to see the remaining gaps.

For a mistaken date window, select it and click **Archive/Restore Selected**. This hides it from the normal list and map selector, but retains its expected products, files, and processing history. **Show archived windows** makes it accessible again. Archiving does not certify completion and must not be used to dismiss genuine missing files. Overlapping windows share processing evidence, so finishing a file can improve coverage in several windows at once.

`Complete (recorded matches)` means every expected observation from the saved search has downstream proof. It is not a promise that NASA will never publish more products for those dates. Use fresh remote preflight for that check. In best-version mode a processed higher version can satisfy an older expectation; the reverse is not allowed. Upload conflicts requiring replacement remain visible and are not replaced automatically.

#### Start The Next Update

For example, after completing `2026-06-30` to `2026-08-10`, the next window can be `2026-08-10` to `2026-09-30`. Including the previous end date gives a safe one-day overlap; leave existing-file and manifest skipping enabled. In Automation, select the tiles, enter the new dates, authenticate Earthdata, keep **Refresh remote catalogue during preflight** enabled, and run **Preflight**, then **Start Automation**. Do not resume the old completed run. Use **Copy Download Date Range** if you first entered those dates in Download.

A different date window creates a separate campaign. Reusing the exact dates of an archived mistaken window reactivates that window rather than deleting its earlier history. Searches only detect late or reprocessed products inside the dates you request: to check earlier acquisitions again, deliberately use a wider overlap. New product versions may need explicit EE replacement later; SWOTFlow does not silently overwrite older EE assets.

### 7. Cleanup

Cleanup is a separate tab because it can delete local intermediate files. Click `Preview Cleanup` first; SWOTFlow only offers files with downstream project-record proof, such as raw files that were already extracted or mosaics that were uploaded or verified in Earth Engine. Then delete selected rows, or delete all previewed candidates when you are sure the project stage is complete.

Interrupted Earthdata transfers can also leave `partial_*` files in the raw download folder. **Preview Cleanup** includes these under **partial** only when their embedded metadata identifies the exact HR Raster product/version and the project confirms a later successful download and extraction. The complete raw and extracted files may already have been cleaned up; their history remains sufficient proof. Unknown or ambiguous fragments, missing proof, and files modified within the last hour are retained.

To remove these leftovers, stop downloads/Automation and wait for the current stage to finish, then preview and delete the eligible rows. After upgrading SWOTFlow, close older instances and restart before using this cleanup. Deletion rechecks the file and processing records, prevents simultaneous downloads to that folder, and records its actions in `00_logs/partial_download_cleanup.jsonl`. It does not erase processing history or trigger downloads. These fragments are not deleted automatically during Automation or by Diagnostic Cleanup.

#### Earth Engine consolidation (optional post-processing)

The current release is v0.6.1. **Statistics → NASA History** adds independent historical catalogue checks and persistent processing-version records. Choose **Use project scope**, set the older dates to revisit, and **Check historical dates**. Then read/load the original EE inventory and compare the selected search. To record future regular searches and download batches, enable the history checkbox there and save the project. Existing active updates are unchanged. See [NASA_HISTORY.md](NASA_HISTORY.md) for importing retained filenames, comparison meanings, exports and limitations.

The **EE Consolidation** tab, immediately after **Upload**, estimates how many stored images could be replaced by date/cycle/pass mosaics. **Statistics** follows EE Consolidation and precedes **Cleanup**. It is separate from local Mosaic and Automation. Open it on the next normal application launch; do not interrupt an ongoing update just to load this feature.

1. Click **Use current project settings**, check the source collection and local mosaic manifest, then **Read EE inventory**. The existing Earth Engine API login is used; no NASA download or Earthdata login is needed. **Load inventory JSON** also supports an offline estimate from an earlier inventory.
2. Choose native UTM (default), existing latitude-band tiles, or an experimental common 100 m EPSG:6933 grid. Native UTM preserves zone, hemisphere and alignment. The common-grid hemisphere checkbox affects only common-grid grouping. Dates are inclusive UTC start dates; blank dates mean all images. Images starting on different dates or belonging to different cycles/passes never merge.
3. Click **Calculate preview**. The comparison shows potential savings under each grouping choice, with the same date/version filters. It also shows new mosaics, retained singletons, exclusions and the temporary asset count while originals remain. Savings are asset counts, not storage-byte or valid-area estimates. Actual project quota includes assets in other collections and folders.
4. **Save plan** writes a full JSON source/processing history and `groups.csv` in the [durable consolidation data folder](DEVELOPMENT.md#durable-application-data). Double-click a row to inspect source IDs, original tile filenames, CRIDs/counters and acquisition intervals. With no row selected, **View selected details** shows exclusions and method notes.
5. For a pilot, select 1–10 rows and use **Export selected (max 10)**. Review the destination and exact selected groups in the confirmation. Outputs go only to a separate collection, which is created if absent. The tab checks current source fingerprints and quota, never overwrites an output, and never deletes originals. **Refresh export status** reads the saved task IDs; **Open saved task folder** restores tracking after a restart. Submitted jobs run in Earth Engine after the desktop app closes.

Lowest quality code is the default overlap rule. The first-valid-source option is a paired comparison baseline, not the historical local mosaic algorithm. Equal-quality ties use earliest source acquisition start, then asset ID. All output bands (`b1`, `b2`, `source_index`) come from the same stored source pixel; quality 255, fractional/unknown quality, missing band pairs and elevation fill are excluded. `source_index` is one-based in the saved ordered source list. SAMPLE pyramids are used for all three bands; scientific filtering should explicitly use the output 100 m grid. Processing versions may be mixed with full retained lineage, or grouping can require one identical, known CRID/counter across all original inputs.

New exports are marked unverified. Compare them before deciding separately whether to retire any originals. Consolidation cannot recover missing granules, correct displaced source pixels, or undo a band mismatch inherited from an earlier stored mosaic. The common grid also needs seam/resampling and mask-alignment validation before continental production. No automatic replacement or deletion is provided.

**Extend an existing mosaic (v0.4.0):** set **Operation → Update existing mosaics**. Keep **Source collection** as the original/new SWOT inputs, enter the **Existing mosaic collection**, and point **Previous plans folder** to the saved consolidation plans. **Read EE inventory** now reads both collections. For offline planning, load the source inventory and use **Load existing inventory JSON** separately. Select the same grouping, quality and processing-compatibility settings used for the previous mosaics, then calculate the preview.

The preview distinguishes groups to extend or create, unchanged groups, already represented inputs and blocked groups. **Preview accounting / blocked groups** explains conflicts. The tool preserves each pixel's original source tie-break and full source history, including acquisition intervals. Different UTC dates/passes remain separate. Processing-version replacements, missing history and changed rules need a source-level rebuild; they are not silently mixed into the old mosaic.

The export collection may be the existing consolidated collection (new revision IDs only) or another collection separate from the source inputs. No previous asset is overwritten, deleted or automatically promoted to a verified active product. Keep the saved plans with their source-index mappings; they remain necessary after source raster retirement. See [EE_CONSOLIDATION.md](./EE_CONSOLIDATION.md) for the full update workflow, counts and test limits. Launch v0.6.1 after the current update finishes; the running instance is not restarted by this change.

**Consolidation tracking (v0.5.0):** load fresh full source/consolidated inventories and the saved plans, then click **Update coverage register**. In Create mode, **Read consolidated inventory** loads the outputs separately. Open **Statistics → Consolidation** and select **Consolidation Coverage** on the Status Map. The selected UTC date window and source lineage drive both views. Counts distinguish approved observations, pending additions, candidates awaiting approval, missing/stale data, processing-version freshness and conflicts. They cover registered input history, not the NASA catalogue or valid water pixels. See [replacement register operations](EE_REPLACEMENT_REGISTER.md) for evidence import, explicit local approval and remaining retirement limitations.

#### Optional Diagnostic Cleanup

Use **Cleanup > Diagnostic Cleanup...** to reclaim space from old uploader debug files without changing processing history:

1. Choose **Upload screenshots**, **Upload HTML snapshots**, and optionally **Uploader text logs**. Screenshots and HTML are selected by default; text logs are not.
2. Set **Keep most recent days** (default `7`, minimum `1`), then click **Preview Diagnostics**. The preview shows eligible file counts and size by category, total potentially reclaimable size, and recent diagnostics that will be retained. Scanning is read-only and runs in the background.
3. Inspect the paginated file list. Select individual rows to see their combined size, or use all previewed files in the chosen categories. Changing filters requires a fresh preview; changing pages clears the current row selection.
4. Stop project processing and uploads, including separate terminals, then check the stopped confirmation. Click **Delete Selected** or **Delete All Previewed**, and confirm. Deletion is permanent, not a move to the Recycle Bin.

These diagnostics help investigate old browser errors; deleting them loses that evidence, not the record of which products were processed/uploaded. Files changed since preview are skipped. The result reports actual deleted bytes and any skipped/failed files; run Preview again to check what remains. Logical file sizes are estimates of space savings, which can differ from allocated disk space on compressed/deduplicated filesystems.

This option never deletes SQLite files or WAL/SHM files, manifests/CSV reports, update history, automation run folders/configurations, statistics snapshots, profiles, recovery backups, browser profiles, or raw/extracted/mosaic rasters. It does not compact the database. Cleanup decisions are recorded in `00_logs/diagnostic_cleanup.jsonl`. No diagnostics are deleted automatically during processing or relocation.

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
