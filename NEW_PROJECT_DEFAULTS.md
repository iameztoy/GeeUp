# New-project defaults — v0.8.20

**New Project** starts with the following editable settings. A fresh installation
uses the same defaults from `config.example.yaml`. **Open Project**, **Resume**,
**Save Project As** and relocation preserve saved choices; installing this release
does not migrate existing projects or change a running worker.

| Setting | New-project choice |
|---|---|
| NASA discovery and history | Verified downloads, persistent filename/version history and fresh remote catalogue checks enabled |
| Source selection | Preferred processing version (`best`), no download/extraction count limit |
| Scheduling | Finish each tile year by year before moving to the next tile |
| Local processing | Original projection, separate exact UTM tiles, nearest neighbour if needed; no forced reprojection |
| Parallel work | 10 download threads; download batches of 50; 4 extraction workers; 4 mosaic workers |
| Upload | Included in Automation; Submit and continue; at most 3 browser transfers; no additional app cap on EE ingestions |
| Recovery | One immediate upload attempt plus the existing two Automation recovery rounds; continue with the next independent tile after failure |
| Cleanup | Download/extraction cleanup and mosaic removal enabled; confirmed EE ingestion + matching provenance for mosaics |
| Resume | Reuse verified existing files and recorded work; no blanket mosaic overwrite |

Review tiles, dates, destination, available disk space and the settings before
starting. New Project retains the supplied dates/tiles, destination, browser and
GDAL runtime configuration while resetting the workflow choices in this table.
It repoints data and log paths beneath the chosen project folder. Change any
setting in its existing tab and save: reopening does not reset it. Use **Save
Project As** if you want a new project using your current customized settings.
New Project refuses to overwrite an existing `project.yaml`; use Open Project.

The choices are a practical starting point, not a guarantee of fastest performance
on every computer or network. Reduce workers/transfers if resources are limited.
Yearly/per-tile cleanup can remove inputs needed for later local cross-tile
mosaics; retain those inputs or use EE Consolidation for that workflow.

Nothing starts when a project is created. Preflight and Start are still required;
automatic start after preflight is off. Existing upload confirmation and dry-run
controls remain available. No pyramiding-policy change is made by the preset.

Cleanup still checks downstream proof. Pending, failed, changed and unproven
mosaics remain local until a later EE sync and cleanup confirms eligibility.
Filename/version history, original checksums, mosaic membership and upload
receipts remain in the project database and logs. Statistics continues to separate
historical processing from files currently on disk and from pending ingestion.
Continue after failure does not label incomplete data complete, bypass safety
checks or prevent a user stop. See [yearly Automation](YEARLY_AUTOMATION.md),
[mosaic cleanup](MOSAIC_CLEANUP.md) and [verification](WORKFLOW_VERIFICATION.md).
