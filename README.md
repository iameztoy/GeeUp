# Earth Engine UI GeoTIFF Uploader

This project is a local desktop automation tool written in Python. It uses Selenium and your local Google Chrome browser to upload GeoTIFF files into Google Earth Engine through the Earth Engine web interface.

This tool does **not** use Google Cloud Storage buckets. It simulates a normal user working in the Earth Engine Code Editor / Asset Manager UI.

## What This Tool Does

The script can:

1. Open normal Chrome with a dedicated reusable profile folder and attach Selenium to it.
2. Open the Earth Engine Code Editor.
3. Pause for manual login if needed.
4. Open the image upload dialog.
5. Send local GeoTIFF file paths directly into the page's file input.
6. Fill the destination asset ID.
   If the UI uses separate destination collection and image name fields, the script now fills those instead.
7. Optionally apply pyramiding policy settings.
8. Submit uploads in batches.
9. Watch the Tasks panel for ingestion progress.
10. Wait for the queue to drain before starting the next batch.
11. Save logs, screenshots, HTML dumps, and a CSV report.

## Limitations and Warnings

- This is **unofficial UI automation**. If Google changes the Earth Engine interface, selectors may need to be updated.
- Earth Engine uploads through the web UI are slower and more fragile than API-based imports.
- This tool intentionally uses Selenium and Chrome only, because that was the design requirement.
- The script tries to detect asset-exists cases from the UI. That detection is best-effort because it depends on the current Earth Engine interface.
- Earth Engine's UI has upload limits. Google documents UI GeoTIFF uploads in the Code Editor and notes that larger files may require the command line instead of the UI. See the official guide: [Importing raster data](https://developers.google.com/earth-engine/guides/image_upload).
- You still need a valid Google account and Earth Engine access.

## Project Files

- `ee_ui_uploader.py`: main script
- `ee_uploader_gui.py`: beginner-friendly desktop launcher
- `selectors.py`: all UI locators in one place
- `config.example.yaml`: sample configuration
- `requirements.txt`: Python dependencies
- `TROUBLESHOOTING.md`: common failures and fixes

## Prerequisites

You need:

1. Windows with Google Chrome installed.
2. Python 3.11 or newer.
3. A Google account that can access Earth Engine.
4. A folder on disk that contains your GeoTIFF files.
5. A Python virtual environment for this project. This tool is designed to refuse running from your global Python installation.

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
cd C:\Users\ibana\Desktop\GeeUp
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

The window lets you fill:

- origin folder with GeoTIFF files
- destination image collection
- batch size
- max active ingestions
- prefix / suffix
- pyramiding policy
- resume mode
- dry-run mode
- Chrome profile folder

Then click:

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

- `upload.batch_size`
- `upload.max_active_ingestions`
- `upload.prefix`
- `upload.suffix`
- `execution.dry_run`
- `execution.resume`

## Example Config Choices

If your GeoTIFFs are in:

```text
C:\Users\YOUR_NAME\Desktop\rasters
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
3. It writes `reports/upload_report.csv`.
4. No browser upload is submitted.

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

The CSV includes:

- `local_file`
- `asset_id`
- `batch_number`
- `submit_time`
- `detected_task_name`
- `final_status`
- `error_message`

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

## Official Earth Engine References

- [Importing raster data](https://developers.google.com/earth-engine/guides/image_upload)
- [Managing assets](https://developers.google.com/earth-engine/guides/manage_assets)

## Unofficial Tool Warning

This project is an unofficial browser automation helper. It may break if Earth Engine changes the interface, HTML structure, labels, or task display behavior.

Use dry-run mode first, keep an eye on the browser during early tests, and expect occasional selector maintenance over time.

## How to Use This Project in 5 Minutes

1. Install Python 3.11+ and Google Chrome.
2. Create and activate the local environment with `python -m venv .venv` and `.\.venv\Scripts\Activate.ps1`.
3. Run `python -m pip install --upgrade pip` and `python -m pip install -r requirements.txt`.
4. Run `python ee_uploader_gui.py`.
5. Choose your origin folder and destination image collection.
6. Click `Save And Run Dry Run`.
7. If the plan looks right, click `Save And Run Real Upload`.
#   g e e U p  
 