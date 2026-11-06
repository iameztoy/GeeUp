# Earth Engine upload naming and recovery — v0.8.7

## Asset-name rejection

Earth Engine's image upload dialog accepts 1–100 characters per slash-separated
asset-ID component. The optional processing-range suffix introduced in v0.8.6
could extend a normal mosaic stem to 111 characters. The dialog then disabled
Upload; filling properties did not submit an ingestion task.

The uploader now removes only that optional suffix from an oversized recognized
mosaic's cloud name. A typical 111-character stem becomes 90 characters. The full
local filename and GeoTIFF bytes remain intact. Metadata still records minimum
and maximum contributing versions. The local mosaic manifest retains every input
filename and version; the upload report binds the full local path to the cloud ID.

Names already within the limit are unchanged. Invalid parent components, names
still too long after compaction, and two selected files mapping to the same asset
ID fail before browser submission. Nothing is overwritten or renamed in EE.
Custom naming prefixes/suffixes still apply and must fit within the limit.

## Metadata retained and omitted

For mosaics, the dialog omits redundant default descriptor, overlap and MOSA scene
rows. When the source-version endpoints exist, the synthetic mosaic CRID/counter
rows and full version-list row are also omitted. Custom property values that
differ from the parsed filename are preserved. Local upload reports retain the
complete descriptive metadata and source lineage, including intermediate versions.

The dialog retains acquisition start/end, resolution, coordinate system,
cycle/pass and the contributing version endpoints. Verified uploads also retain:

- `swotflow_revision`: unique submission revision, preventing stale-upload reuse.
- `swotflow_sha256`: identity of the exact local mosaic bytes.
- `swotflow_sources_sha256`: identity of the original-input checksum set.

These three properties support ingestion verification, resume and cleanup. They
are intentionally retained; merely finding an asset with the expected name is
not sufficient evidence that it contains the current file.

## Resume and Statistics

Close the old app normally, reopen v0.8.7, open the existing project, and use
Automation → Resume Run. Authenticate Earthdata when the continuing campaign
requires downloads for later batches. Retained local outputs are checked against
the saved source selection and byte proofs; unchanged mosaics can be uploaded
without downloading or recreating them.

Before retrying, the uploader refreshes EE inventory and operations. Invalid old
names with only unsubmitted/failed attempts become `REPLACED_INVALID_ASSET_ID`.
Their rows and status events remain available as history, with the corrected ID
in the explanation. Contradictory task/asset evidence stops the repair for review.
No existing valid cloud ID is migrated automatically.

Statistics excludes those replaced names from current upload totals and reports
their separate historical count. NASA accounting and EE Consolidation resolve the
short cloud names to full local lineage; ambiguous mappings stay unresolved.
Successful form preparation is never counted as a completed upload.
Planned, dry-run and active tasks are not counted as upload failures.

Validation includes maintained offline regressions and an isolated upload-plan
check against retained local mosaics. A successful new live upload must still be
confirmed by the actual EE task/inventory when the user resumes.
