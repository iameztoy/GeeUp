# Statistics accounting — v0.8.9

Statistics combines retained project manifests, upload events, yearly checkpoints, the recorded EE inventory and current local file presence. Refresh Statistics after restarting an updated application. Local statistics refresh uses the recorded inventory; use the explicit EE inventory/reconciliation action when a fresh cloud check is needed.

| View | What its numbers mean |
| --- | --- |
| Downloaded/extracted | Original input records that reached those stages. Cleanup does not erase this history. |
| Files on disk | Files currently present, which can decrease after cleanup. |
| Processing versions: remote/selected | Versions recorded in the download manifest and those selected by processing priority. These are not a fresh count of the entire NASA catalogue. |
| Processing versions: mosaics/EE assets | Outputs containing each contributing source version. A mixed-version output appears in each of its versions, once per output; these columns must not be summed to obtain unique asset totals. |
| Tile/date stage records | Recorded appearances across the named stages, not distinct acquisitions. |
| Sent to upload | Recorded submission evidence, including retained events for assets subsequently verified. An inventory-only asset is not automatically counted as an application submission. |
| Uploaded/verified | Recorded successful/already-existing outputs, supplemented by inventory. Asset presence is not full pixel verification; verification and cleanup guards remain separate. |
| Pending mosaics | Outputs not yet recorded as uploaded/verified, not proof of absent NASA inputs. The ready-to-upload list additionally requires a local file. |
| Yearly Automation | Unique tiles, years and tile/year batches across the full saved run. Finishing one year of a tile does not finish that tile's other requested years. |

Source processing versions come from retained mosaic input filenames where available. Legacy records without that lineage use their filename fallback; this cannot reconstruct an unknown mixed processing history. Empty exclusion lists mean no exclusions. Deliberately excluded lower processing versions remain visible as such, separate from omitted required inputs.

Inventory refreshes retain submission timestamps and task names. Older verified rows whose fields were blanked can recover them from the same asset's retained upload events. A new upload attempt does not inherit a prior task, and no missing evidence is invented.

Snapshots retain compatible existing fields and add `count_definitions` to their JSON export. All source filenames and processing history remain available after verified cleanup. Statistics does not download, upload, replace or delete imagery; explicit Cleanup retains its existing current-proof checks.

Input accounting establishes which recorded sources reached outputs. It does not prove that all observations currently available from NASA have been selected: run catalogue reconciliation for the intended full tile/date scope. Pixel shifts, spatial no-data holes and pixel agreement require their separate verification tools.

## Upload exceptions — v0.8.14

Annual `upload_pending` batches have complete local source accounting but missing
EE uploads. They may allow later work; they do not count as completed batches,
years or tiles. Statistics shows their count separately and exports the pending
file paths, asset IDs, last errors and dates in
`project_statistics_deferred_uploads.csv`. Source checksums and filenames remain
in the run JSON and verification reports. These are upload exceptions, not missing
NASA acquisitions or proof of file corruption. Resume/reconciliation refreshes
the current list after recovery; historical failed attempts remain in upload events.
