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

## Yearly throughput checkpoints — v0.8.15

Submit and continue may leave a year `upload_pending` while later years run.
The pending-upload CSV includes `kind`: `verification_pending` for submitted or
active tasks, `retry_pending` for failed/unsubmitted outputs, and `outcome_unknown`
for uncertain submissions. These are separate from completed years and verified
source totals. An awaiting-verification task is not reported as file corruption.

The yearly backlog is a snapshot of the last year-boundary reconciliation;
refreshing Statistics alone does not poll EE or rewrite run checkpoints. An EE
sync can update cumulative upload/source totals before the earlier yearly
checkpoint is revisited. Resume reconciles that checkpoint and skips currently
active or verified assets. Cleanup never subtracts retained filename/source
history; local disk counts and historical processing counts remain distinct.


## Mosaic cleanup policy — v0.8.16

Overview includes **Mosaic cleanup enabled**, **Mosaic cleanup policy**, and its
verification scope. Confirmed-ingestion cleanup is not full pixel certification.
Local mosaic/byte counts decrease when files are removed; historical filenames,
source memberships, processing versions and uploaded counts remain. Pending and
failed files stay local and remain in their existing backlog categories. Cleanup
stage JSON/CSV records its policy, deleted-file count and bytes. Refresh Statistics
after manual cleanup; selecting a policy alone does not change processing totals.
See [mosaic cleanup and Resume](MOSAIC_CLEANUP.md).


v0.8.17 aligns cleanup with the existing Statistics exclusion for
`REPLACED_INVALID_ASSET_ID`: archived rejected names remain historical records,
not extra active uploads. Removing an eligible mosaic decreases disk counts, not
uploaded totals or source history. A one-time cleanup-rule recheck on Resume adds
a cleanup attempt to the run record without counting the imagery again. Refresh
Statistics to update a previously saved snapshot after cleanup.
