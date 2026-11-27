# Lossless mosaic compression

Choose **Uncompressed**, **ZSTD (lossless, recommended)** or **DEFLATE (lossless)** before creating new mosaics. ZSTD uses level 3; DEFLATE uses level 1. Both use tiled GeoTIFFs and lossless predictors. No rounding, bit truncation, reprojection, quality averaging or gap filling is introduced. Sparse blocks are not exposed: local tests alone do not certify interoperability.

The selected quality pixel and its elevation stay paired. Original source selection, acquisition times, processing-version history, masks and native grids follow the existing workflow. File compression is recorded in local provenance. Different encoded bytes get their own file checksum; upload verification and cleanup continue to require the correct current bytes and source lineage.

## Controls

Full app v0.9.2: **Mosaic -> Mosaic compression** and **Automation -> Mosaic compression** edit one shared value. This applies to ordinary and year-by-year Automation and to both Browser and Cloud uploads. Existing/new template behavior remains Uncompressed until selected.

## Existing projects and Resume

Existing projects lacking this setting use Uncompressed. A codec change applies to files newly created afterward. Already verified local files and confirmed EE assets are reused, including after raw/extracted cleanup; changing a codec does not redownload, reprocess, re-upload, or rewrite old receipts. Creating a differently encoded replacement of an existing output requires the normal explicit overwrite/replacement workflow. Do not overwrite a file while it is uploading. Scientific configuration changes still require the existing review/Resume safeguards.

Compressed TIFFs use the same Browser or Cloud upload path and metadata mechanism. Cloud bucket copies are removed only after confirmed ingestion and matching provenance. A mosaic larger than the selected staging cap is retained, with its filename, size and cap reported. The app never silently raises that cap. Lowering a cap does not interrupt already admitted transfers.

## Storage and interpretation

The staging cap controls tracked work in the current project; it is not a bucket/account-wide free-tier budget. Manual source copies and other projects can add storage. Google's eligible regional Standard storage allowance is 5 GB-months, with separate request/transfer limits. Smaller GeoTIFF uploads reduce local/temporary bucket bytes; do not assume identical savings in EE's internal storage. No new paid services or billing exports are enabled.

Completeness means all preferred available NASA originals for the chosen tiles, dates, cycle and pass are accounted for in the outputs. It does not mean every pixel across a UTM zone has data. Empty catalogue windows and failed/missing processing remain distinct. NoData gaps remain masked.

## Validation

Actual GDAL regression fixtures compare all decoded values and masks for singletons and overlapping mosaics, both hemispheres, numeric/NaN NoData, valid zero/negative elevations, quality ties, separated gaps and integer types. Resume tests cover yearly and ordinary scheduling and already-cleaned intermediate files. Codec support is checked before output creation; unsupported codecs fail explicitly rather than silently falling back. One-off research files, screenshots and live-pilot receipts remain outside the repository.

References: [GDAL GeoTIFF options](https://gdal.org/en/stable/drivers/raster/gtiff.html), [EE raster imports](https://developers.google.com/earth-engine/guides/image_upload), [Cloud Storage pricing](https://cloud.google.com/storage/pricing).

Release validation: the complete application suite ran 780 tests successfully (25 environment/opt-in skips), and the separately configured GDAL runtime passed 56 raster/planning checks, including the new compression matrix. GUI codec persistence and shared controls passed. Python 3.11 was used here; Python 3.12 support and dependencies are unchanged, and a fresh 3.12 installation was not repeated.

A separate two-mosaic live pilot (north/south Zone 35) passed with 23 exact retained NASA inputs. The actual new writer produced full decoded-array/mask equality against the baseline; both codecs ingested successfully into an isolated EE test collection, with exact sampled cloud equality. Metadata, source accounting, duplicate-free Resume, staging deletion and local TIFF/world-file cleanup passed. Statistics retained 4/4 tiles and 23/23 inputs after cleanup. Production assets were unchanged. These release checks are not enabled during ordinary runs.
