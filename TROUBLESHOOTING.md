# Troubleshooting

This file covers the most common problems when automating Earth Engine uploads through the browser UI.

## How To Stop a Running Upload

What to do:

1. Click the upload console window.
2. Press `Ctrl+C`.
3. Wait a moment for Python to stop cleanly.

What happens:

- the script exits
- the current CSV report stays saved
- the log file stays saved

Closing the terminal window also stops the run, but `Ctrl+C` is better because it gives the script a chance to exit cleanly.

## Script Says a Python Environment Is Required

Symptoms:

- the script exits immediately with a message saying it must be run from an activated Python environment

What it means:

- you are using the global Python interpreter instead of the project's virtual environment

What to do:

1. Open PowerShell in the project folder.
2. Create the environment if it does not exist yet:

```powershell
python -m venv .venv
```

3. Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

4. Install the packages:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

5. Run the uploader again.

## PowerShell Blocks Activate.ps1

Symptoms:

- PowerShell says running scripts is disabled
- `.\.venv\Scripts\Activate.ps1` fails before the environment activates

What to do:

1. In the same PowerShell window, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

2. Then activate the environment again:

```powershell
.\.venv\Scripts\Activate.ps1
```

3. Confirm the prompt now starts with `(.venv)`.

## Element Not Found

Symptoms:

- the script times out while looking for a button, tab, input, or dialog
- logs mention a selector such as `new_button`, `image_upload_button`, or `asset_id_field`

What it usually means:

- Earth Engine changed the HTML
- the page is still loading
- you are on the wrong panel
- the selector in `ee_selectors.py` needs updating

What to do:

1. Run again with console debug logging if needed:

```powershell
python ee_ui_uploader.py --config config.yaml --verbose
```

2. Open the generated screenshot and HTML file in the configured artifact folder. In project mode this is `<project_root>/00_logs/upload_artifacts/`; older non-project runs may use `artifacts/`.
3. Check whether the browser really reached the expected Earth Engine page.
4. Inspect the page with Chrome DevTools.
5. Update the relevant selector in `ee_selectors.py`.

## Stale Element Reference

Symptoms:

- Selenium raises `StaleElementReferenceException`

What it usually means:

- Earth Engine re-rendered the UI after Selenium found the element

What to do:

1. Rerun the script. The uploader already retries transient UI failures.
2. Increase `upload.retry_attempts` if needed.
3. Increase `execution.short_ui_wait_seconds` slightly if menus are animating slowly.
4. If the same control keeps rerendering, refine its selector in `ee_selectors.py`.

## Upload Dialog Not Opening

Symptoms:

- the script clicks around but the image upload dialog never appears

What it usually means:

- the upload path changed
- the script hit the wrong `NEW` control
- the upload action is behind a different menu

What to do:

1. Verify the Earth Engine UI manually:
   - open the Assets tab
   - use the `NEW` button under the Assets tab, not the one under the Scripts tab
   - find the control that opens GeoTIFF image upload
2. Check the shadow-DOM selectors in `ee_selectors.py`, especially `SHADOW_SELECTORS`.
3. If needed, update `new_button` and `image_upload_button` in `ee_selectors.py` for the generic fallbacks too.
4. Re-run in dry-run mode first.

## Earth Engine Is Visible But The Console Reports a Page-Load Timeout

Symptoms:

- the console fails while opening `https://code.earthengine.google.com/`
- the traceback mentions `ReadTimeoutError`, `TimeoutException`, or `driver.get`
- the saved screenshot shows the Earth Engine Code Editor is actually visible

What it usually means:

- Chrome loaded enough of Earth Engine for a human to see it, but Selenium did not receive a clean page-load completion signal before the timeout
- Chrome or the Earth Engine page was temporarily slow or overloaded

What SWOTFlow does:

- recent versions check whether the Earth Engine UI is usable after a page-load timeout
- if the Assets/New controls are visible, the uploader continues
- if the UI is not usable or Chrome disconnects, the run stops and writes debug artifacts

What to do:

1. If the run stopped, close the controlled Chrome window.
2. Reopen SWOTFlow and the same project.
3. Run `Sync EE Assets` if any uploads may have been submitted before the failure.
4. Rerun the real upload with `Resume previous run` enabled.
5. If it repeats often, increase `execution.page_load_timeout_seconds` and reduce upload batch pressure.

## Upload Button Disabled Or "Please Provide an Asset ID"

Symptoms:

- the Earth Engine upload dialog opens
- the file appears selected
- the final `UPLOAD` button stays disabled
- the dialog says `Please provide an asset ID`

What it usually means:

- Earth Engine did not bind the Asset Name field even though Selenium filled it
- the upload dialog re-rendered while the field was being populated
- less commonly, the destination collection/root is invalid or unavailable

What SWOTFlow does:

- if the dialog reports a missing asset ID, SWOTFlow retries the Asset Name field using keyboard input
- if the browser recovery step fails, the current row is written as `ERROR` instead of remaining `PLANNED_UPLOAD`

What to do:

1. Run `Sync EE Assets` after checking whether any task was submitted.
2. Rerun with `Resume previous run` enabled.
3. If the same file fails repeatedly, upload only that tile or a small batch and inspect the saved screenshot in `<project_root>\00_logs\upload_artifacts`.
4. Confirm the destination ImageCollection path exists and that the account can write to it.

## User Not Logged In

Symptoms:

- the script keeps waiting for login
- Chrome opens a Google sign-in page

What to do:

1. Complete login manually in the Chrome window opened by the script.
2. Wait until the Earth Engine Code Editor is fully visible.
3. Return to PowerShell and press Enter.
4. Keep using the same dedicated Chrome profile folder for later runs.

If login does not persist:

- make sure `chrome.user_data_dir` always points to the same folder
- do not delete that folder between runs
- do not use a profile currently open in another Chrome instance

## Google Says "This Browser or App May Not Be Secure"

Symptoms:

- Google sign-in shows "This browser or app may not be secure"
- sign-in works in your normal Chrome, but fails when the uploader opens the browser

What it usually means:

- Google is rejecting a directly automated login flow

What the project now does:

- the recommended default browser mode is `chrome.connection_mode: attach`
- in that mode, the tool starts a normal Chrome window with your dedicated profile and Selenium attaches to it afterward

What to do:

1. Make sure your `config.yaml` contains:

```yaml
chrome:
  connection_mode: attach
  remote_debugging_port: 9222
```

2. Run the uploader again.
3. If Chrome opens and asks you to sign in, sign in manually in that normal Chrome window.
4. Keep that window open while the uploader continues.

If you still see the warning:

1. Close all Chrome windows using the same dedicated profile.
2. Re-run the uploader.
3. If needed, delete the dedicated profile folder and let the tool create a fresh one.

## Earth Engine Task Stuck in READY or RUNNING

Symptoms:

- tasks stay in `READY` or `RUNNING` for a long time

What it usually means:

- Earth Engine ingestion is queued or slow
- your account has multiple ingestion tasks already active
- the task is genuinely slow because of file size or server load

What to do:

1. Open the Tasks panel manually and inspect the task details.
2. Wait longer.
3. Reduce `upload.batch_size`.
4. Reduce `upload.max_active_ingestions`.
5. Increase `execution.wait_timeout_minutes` if the tasks are healthy but slow.

## Asset Already Exists

Symptoms:

- the upload dialog reports `already exists`
- the task or dialog says the asset ID is already present

What the script does:

- it records `SKIPPED_ALREADY_EXISTS` in the CSV report

What to do:

1. Confirm the existing asset is the one you want.
2. If you intended to overwrite, note that this UI automation does not delete or replace assets automatically.
3. Change the naming rule with `prefix`, `suffix`, or replacement rules if needed.

## Chrome Profile Lock Problems

Symptoms:

- Chrome refuses to start
- the script cannot attach to the profile
- Chrome says the profile is in use

What to do:

1. Close all Chrome windows using the same profile.
2. Check Task Manager for leftover `chrome.exe` processes.
3. Try again.
4. If needed, use a different `chrome.user_data_dir` dedicated only to this project.

Do not point the script at your daily browsing profile while Chrome is open.

## Chrome / Driver Version Mismatch

Symptoms:

- Selenium cannot start Chrome
- you see messages about driver compatibility

What to do:

1. Update Google Chrome to the latest stable version.
2. Make sure the project's virtual environment is activated.
3. Update Selenium:

```powershell
python -m pip install --upgrade selenium
```

4. Run the script again.

Selenium 4 normally uses Selenium Manager to find or download the right driver.

## Selector Drift After Earth Engine UI Changes

Symptoms:

- a workflow that used to work suddenly stops finding elements

What to do:

1. Open `ee_selectors.py`.
2. Inspect the broken control in DevTools.
3. Add or replace selectors using this order:
   - stable attributes
   - visible text
   - broad fallback XPath
4. Keep the most reliable option first.
5. Test with dry-run mode.

## SWOT Metadata Not Added

Symptoms:

- the upload works, but `system:time_start` or custom `swot_*` properties are missing
- the log says a metadata control could not be clicked or filled
- the CSV shows `METADATA_NOT_PARSED`

What to check:

1. Confirm the filename follows the SWOT L2 HR Raster pattern with cycle, pass, scene, start time, end time, CRID, and product counter.
2. Run a dry run and inspect `metadata_start_time`, `metadata_end_time`, `metadata_properties`, and `metadata_status` in `reports/upload_report.csv`.
3. If your filenames are not SWOT names, set `metadata.require_match: false` or `metadata.enabled: false`.
4. If filenames parse correctly but the browser fields are not filled, inspect the upload dialog's Properties controls and update the metadata selector labels in `ee_selectors.py`.
5. Test one real upload and verify the asset properties manually in Earth Engine before running a large batch.

## Nothing Happens After Clicking UPLOAD

Symptoms:

- the dialog closes but no matching task is detected
- the report stays at `SUBMITTED` or `UNKNOWN_AFTER_CLICK`

What it usually means:

- the task row text changed and matching needs adjustment
- the Tasks panel selector is too broad or too narrow

What to do:

1. Watch the Tasks panel manually during a run.
2. Compare the task text with the asset name and file name.
3. Update the task row selector or matching logic in `ee_ui_uploader.py`.
4. Check the saved HTML dump in the configured artifact folder. In project mode this is `<project_root>/00_logs/upload_artifacts/`.

## Browser Session Ended During Upload

Symptoms:

- the log says `invalid session id`
- the run stops with `Browser/WebDriver session ended`
- Chrome was closed, restarted, refreshed, or disconnected while the uploader was running

What to do:

1. Stop the run.
2. Close the controlled Chrome window if it is still open.
3. Restart Chrome from the launcher or run the upload again with the same configured Chrome profile.
4. Keep `execution.resume: true`.
5. Verify the assets already visible in Earth Engine, then rerun. Resume skips `SUBMITTED`, `READY`, `RUNNING`, `COMPLETED`, and `SKIPPED_ALREADY_EXISTS`; it does not skip `ERROR` or `UNKNOWN_AFTER_CLICK` rows.

During uploads, do not interact with the controlled Chrome/Earth Engine window. You can use the computer for other work, but avoid touching that Chrome profile/window, refreshing the page, closing tabs, signing out, or changing the Earth Engine UI while Selenium is controlling it.

## Dry Run Looks Correct but Real Run Fails

Possible reasons:

- login is missing
- the upload dialog has extra validation in real mode
- Earth Engine rejected the asset ID
- the file input selector drifted

What to do:

1. Run one file only with a very small test folder.
2. Watch the browser.
3. Inspect the configured logs and artifact folders. In project mode these are `<project_root>/00_logs/` and `<project_root>/00_logs/upload_artifacts/`.
4. Adjust selectors or naming rules based on the actual UI response.

## General Advice

When debugging:

1. Start with one small GeoTIFF.
2. Use dry-run first.
3. Make sure the virtual environment is activated before every run.
4. Keep the Chrome window visible.
5. Read the latest log file in `logs/`.
6. Check the latest screenshot and HTML dump in `artifacts/`.
