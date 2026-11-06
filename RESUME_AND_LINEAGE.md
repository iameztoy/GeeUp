# Resume, cleanup and processing versions — v0.8.8

## Resume after a shutdown

Use Automation's **Resume** for the saved run. In verified mode, completed download, duplicate-selection and extraction stages can be reused when all selected source checksums, extraction settings and current output hashes match the saved NASA selection. If extraction cleanup has already run, reuse additionally requires the completed mosaic stage and current mosaic proofs. This applies to ordinary runs and tile/year batches.

The unfinished mosaic stage runs again; its existing temporary-output handling recreates unfinished outputs and validates completed ones. Checkpoint JSON is now written atomically in both scheduling modes, so an interrupted write preserves the previous checkpoint. A later failed attempt overrides an earlier success.

Resume finishes the saved selection. Start a new preflight/update to discover newly published NASA versions. Missing files, altered bytes, changed settings or missing evidence trigger normal verification; a download-stage message can indicate catalogue/local-file checking rather than a new transfer. Existing raw files still pass provider checksum checks before being skipped. Historical manifest flags alone cannot establish successful present-day file reuse. Keep the project logs/databases with the project when moving computers.

## GeoTIFF sidecars

From v0.8.8, enable **Cleanup after verified stages** before **Resume Run** to
apply cleanup to the continuing run and eligible files in completed years whose
cleanup was previously disabled. Changing the checkbox while a worker runs only
affects the next Start/Resume. Scientific and upload checkpoints are retained.
Installing the release or selecting the checkbox alone deletes nothing.

Raw files require current extracted-file proof; extracted TIFFs and their
companions require current mosaic proof. Final mosaics remain local until the
existing full EE pixel-verification requirement is satisfied. An asset listed in
EE or a completed ingestion task is not sufficient for that last deletion.

Cleanup changes files present and disk-space Statistics. Downloaded-file counts,
all original filenames, version histories, mosaic membership and completion
receipts remain recorded. Use **Cleanup → Sync EE Assets + Preview Cleanup** for
a manual review while Automation is stopped.

Verified Cleanup and Automation can remove `.tif.aux.xml`, `.tif.aux`, masks, overviews and supported world files belonging to a safely removed TIFF. The parent is checked and removed first; sidecars are checked immediately before deletion. A failed parent deletion leaves its sidecars intact. Already orphaned companions can be removed only when the recorded parent and current downstream evidence identify them unambiguously. Unknown/unproven files remain for review; there is no wildcard purge.

Run Cleanup again in the existing project to remove eligible leftovers. Logs, source filenames, manifests and verification receipts remain available. Installing the update alone deletes nothing.

## Processing-version range

New mosaic filenames retain their existing SWOT-compatible stem and append a range, for example:

`..._MIXD_01_PVPIC0-01_to_PGD0-02.tif`

The range includes both CRID and product counter, ordered by the app's established release/minor/fidelity/counter priority, not alphabetically. A homogeneous mosaic repeats its version: `..._PID0_01_PVPID0-02_to_PID0-02.tif`. The original mosaic `01` token is a synthetic mosaic counter, not a claim that its sources used counter 01. `MIXD` still denotes mixed CRIDs.

The range describes contributing input files; it does not assign a processing version to every output pixel or list intermediate versions. The cumulative mosaic manifest preserves all input filenames and adds `processing_min`, `processing_max` and `processing_levels`. With upload metadata enabled, Earth Engine receives `swot_processing_min` and `swot_processing_max`. From v0.8.7 the complete `processing_levels` list stays in the local manifests rather than adding another upload-dialog row. Acquisition start/end times remain the earliest/latest source times. Quality/elevation selection is unchanged.

From v0.8.7, a recognized mosaic whose cloud name exceeds 100 characters omits the optional `_PV..._to_...` suffix from its **Earth Engine name only**. The local filename remains unchanged, and the upload report connects it to the shorter cloud ID. NASA reconciliation and consolidation resolve this mapping. See [upload recovery](UPLOAD_RECOVERY.md).

Previously recorded mosaic filenames are preserved to avoid duplicate outputs/assets caused solely by upgrading. Their version summaries can still be recovered from the local manifest for future uploads. Existing EE assets are not renamed or edited by this release. Export the manifest alongside mosaics to retain complete lineage.

Validation is automated local regression coverage, including interrupted-run checkpoints, sidecar safety, filename parsing and upload metadata. This release does not claim a new live NASA-to-EE acceptance run.
