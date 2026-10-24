# Replacement register and consolidation coverage — v0.5.0

This feature records the relationship between original uploads, consolidated outputs and later revisions in a **separate local SQLite register**. It adds **Statistics → Consolidation** and **Status Map → Consolidation Coverage**. It does not change EE assets, uploader manifests, project databases, audit queues or external analysis scripts. Restart the app normally after the ongoing update finishes to load the new version; the running instance is unchanged.

## Load and refresh coverage

1. In **EE Consolidation**, set the original **Source collection**, **Existing mosaic collection**, **Mosaic lineage manifest**, and **Previous plans folder**. Keep the original saved plans permanently.
2. **Read EE inventory** loads the full original inventory (and the consolidated inventory in Update mode). In Create mode, use **Read consolidated inventory** once that collection exists. The JSON load buttons support offline snapshots. Supply complete collection inventories, not filtered lists or individual pages.
3. Click **Update coverage register**. This imports saved plans for the selected source collection, the current valid preview if present, original-input history, and loaded inventories. Saved task destinations take precedence over the displayed destination. This operation does not submit tasks or approve products.
4. Open **Statistics → Consolidation**. Use **Refresh register** or the ordinary **Refresh Statistics** action to reread local evidence. Refreshing Statistics alone does not fetch new EE metadata. Repeat steps 2–3 to see later EE uploads/exports.
5. Set the optional UTC acquisition-start date window and refresh. The table and the map use the same filtered source rows. Choose **Consolidation Coverage** in the Status Map's Mode selector. Hover/click a tile for counts; inspect **Replacement register → Selected details** for source assets, original filenames, processing identifiers, acquisition intervals, predecessor history and verification evidence.

Registers are isolated by the full original collection ID under the [durable consolidation data folder](DEVELOPMENT.md#durable-application-data), in `registers/<collection-hash>.sqlite3`. Ordinary Statistics refresh is read-only and does not create an empty database. Back up this database together with the saved plans and comparison evidence. Imports and approvals use SQLite transactions; events retain the replacement decisions. Conflicting history is rejected rather than silently overwritten.

## What the counts mean

| Field | Meaning |
|---|---|
| Registered | Distinct original observation identities found in imported source/plan history, ignoring processing version for the denominator. |
| Original in EE | Observations available through structurally consistent original assets in a fresh snapshot, at the best **registered** processing priority. |
| Approved current | Observations in the explicitly approved local selection, with a current passing full-comparison receipt and fresh matching EE metadata, at the best registered version. |
| Pending addition | Original available, but not yet represented by an approved or available candidate mosaic. |
| Awaiting approval | Represented in a structurally valid available consolidated output, but not approved with current full-comparison evidence. Export success alone is insufficient. |
| Missing | Expected registered representation unavailable; disappearance of an approved output is reported even if old originals remain. |
| Unknown/stale | Collection not checked, undated snapshot or inventory older than 24 hours. No green completeness claim. |
| Conflicts / unresolved assets | Duplicate active observations, invalid/incomplete lineage, changed source metadata, or failed comparisons. A failed candidate leaves the previous active choice in place. |
| Older version active | Acquisition exists in the active selection, but a higher-priority registered product is now expected. |

Coverage, warnings and availability are separate dimensions: an approved old mosaic can remain usable while a failed candidate raises a conflict. Do not sum every column as disjoint categories. Physical stored-image counts use the whole loaded inventories, even when the date filter narrows the source-coverage table. “Represented copies retained” describes extra original/revision rasters still present; it is **not permission to delete them**, and is not realized asset savings.

The denominator is **registered input history, not all NASA catalogue acquisitions**. It cannot discover catalogue granules that were never recorded. The existing collection-completeness audit remains separate. Input accounting also does not establish valid-water-pixel coverage: overlap selection and masks can legitimately select none of an input's pixels. An unknown/untracked asset is listed explicitly; resolve its plan/lineage before claiming collection-wide coverage.

Original observation identity and processing rank use `swot_metadata.product_identity_key` and `swot_product_rank`, matching the existing application policy. Lower-priority variants do not increase the expected count or become additional missing granules. Best registered version is not a claim that the newest NASA catalogue version has been checked.

## Duplicate checks and local approval

Temporary physical copies are expected: A/B/C, M1 and M2 can coexist. The register distinguishes that from duplicated observations in the **active selection**. It reports repeated original observations and version alternatives, rejects duplicate observations within an output, and blocks simultaneous active revisions for one acquisition/grid group.

The approval path is deliberately explicit:

1. Select an available consolidated output and choose **Comparison template**. The saved JSON contains the asset and planned-history fingerprints. Its result is `not_run`; it cannot approve anything.
2. Perform the scientific comparison separately. Populate the receipt from actual evidence, recording `method`, `scope`, `checked_utc`, `result`, `level`, `compared_bands`, `pixels_compared`, `masks_compared` and `mismatches`. Include `evidence_file` (absolute or relative to the receipt) and its `evidence_sha256`. Never edit a template to “pass” without running the comparison.
3. **Import comparison receipt** verifies the evidence-file hash and the exact asset/history fingerprints. A passing `full_pixels` receipt requires the entire output grid, including masks, all of `b1`, `b2`, `source_index`, and zero mismatches. Structural/sample receipts are recorded but cannot approve a revision. The register validates and records an external audit result; it does not itself execute or independently reproduce that audit.
4. Choose **Approve selected revision locally** and review the proposed replacement list. Approval is atomic. It rejects lost observations, processing downgrades, duplicate active observations, conflicting grid-group revisions, stale inventory and missing/changed assets. The earlier approved selection remains until this succeeds.

Approval changes only this local register. **No EE collection is modified or published, and no external script automatically starts using the approved list.** The JSON export is a review report, not an automatic downstream dataset switch. Likewise, checking a consolidated output against its stored inputs does not prove those historical inputs are free of the shifts under investigation.

## Remaining retirement work

Cloud deletion, downstream manifest publication, automatic consumption of the running audit results, NASA-catalogue denominator integration and replacement-aware upload/resume/cleanup remain separate work. Existing pipeline/update map modes keep their current meanings; only the new consolidation mode uses this register. Do not retire production originals until those lifecycle connections are implemented and verified. No retirement button is provided here.

## Validation

`tests/test_ee_replacement_register.py`, the consolidation coverage GUI test in `tests/test_gui_layout.py`, and the new map test in `tests/test_utm_map_selector.py` exercise isolated fixtures with no network/cloud writes. They cover M1 plus a later tile becoming M2, rejecting M1+M2 duplication, incomplete/revised lineage, preferred versions, failed/sampled comparisons, full-grid evidence, stale/absent/changed assets, independent dates/hemispheres, atomic rollback and reopening the register.

One-off demonstrations and investigation results are retained outside the repository. See [the lifecycle design](EE_ASSET_LIFECYCLE.md) for the intended retirement workflow.
