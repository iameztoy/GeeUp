# SWOTFlow v0.9.0 - Google Cloud Upload setup

This guide is for the first-time setup of the optional Cloud Upload route. You prepare mosaics locally as before. SWOTFlow stages each TIFF in your private Cloud Storage bucket, starts an ordinary Earth Engine image ingestion, verifies the resulting asset and its metadata, then optionally removes the staging copy. It does not create a cloud-backed external image or change the mosaic projection.

**Validation status:** the new route has automated offline tests. A small live pilot in your own bucket and a separate EE collection is required before a continental upload. No paid resources or account changes are made simply by opening this tab.

## 1. Understand the three names

- **Cloud project ID:** the Google Cloud project used for API calls and billing, for example `my-swot-project`. It is not the project's display name or number. Normally use the same project that owns your EE collection.
- **Bucket name:** a globally unique storage container, for example `my-swot-project-staging-2026`. A bucket name contains no `gs://` prefix or folder path in the SWOTFlow form.
- **EE collection:** the destination for completed images, such as `projects/my-swot-project/assets/SWOTFlow_cloud_pilot`. Bucket storage and EE assets are separate copies with separate quotas.

A "prefix" is a folder-like part of an object name inside a bucket. Keep the suggested `swotflow-staging` prefix. SWOTFlow adds its own project owner and revision identifiers below it.

## 2. Select a project and set up billing

1. Open [Google Cloud Console](https://console.cloud.google.com/) with the Google account you use for Earth Engine.
2. Use the project selector at the top. Select your existing EE project, or create a new project if your institution requires separation. Write down the **project ID**.
3. Open **Billing**. If there is no account, choose the option to create a billing account, enter the requested country/business and payment details, and link it to the selected project. A personal self-service account normally needs a supported payment method, often a credit/debit card; available methods depend on your country. Your organisation may provide an existing billing account instead.
4. Enter payment information only on Google's website. SWOTFlow does not need, store or request your card details.

[Billing account documentation](https://docs.cloud.google.com/billing/docs/how-to/manage-billing-account) and [payment methods](https://docs.cloud.google.com/billing/docs/how-to/payment-methods).

## 3. Set a budget alert before testing

Open **Billing > Budgets & alerts**, create a small budget scoped to this project, and enable email notifications at thresholds such as 50%, 90% and 100%. Choose an amount you are comfortable monitoring. **A budget alert is not a hard spending limit**, and billing information can arrive later than usage. SWOTFlow's staging limit controls its own queued bytes, not your total Google bill or other applications.

[Google's budget instructions](https://docs.cloud.google.com/billing/docs/how-to/budgets).

## 4. Enable APIs and confirm Earth Engine access

In **APIs & Services > Library**, enable **Cloud Storage API** and **Earth Engine API** for your selected project if they are not already enabled. Confirm that the project is registered for Earth Engine and that the account can write to the intended collection. Retain your legitimate noncommercial registration if applicable; paying for Cloud Storage does not by itself require buying an EE commercial plan. Noncommercial eligibility and quotas still apply.

[EE project registration](https://developers.google.com/earth-engine/guides/access) and [noncommercial use](https://developers.google.com/earth-engine/guides/noncommercial_tiers).

## 5. Create a dedicated temporary bucket

In the Console, open **Cloud Storage > Buckets > Create**. This bucket should contain only SWOTFlow staging copies.

- Choose a globally unique, non-sensitive name and note it.
- Choose **Region**, initially `us-central1` (Iowa) if your institution permits US storage. Confirm regional transfer pricing before large uploads; the app does not assume that every bucket-to-EE transfer is free.
- Select **Standard** storage. Do not use Nearline, Coldline, Archive or Autoclass for short-lived staging.
- Keep **Public access prevention** enabled and choose **Uniform** access. Do not grant access to `allUsers`.
- Keep object versioning **off**. Do not configure a retention lock, default object holds or automatic age-based deletion rules. Such rules could remove inputs before a queued ingestion finishes, or prevent prompt removal.
- Review **soft delete** explicitly. Google commonly enables a seven-day retention policy for deleted objects. For this dedicated disposable staging bucket, you can disable it to avoid paying for retained deleted copies. Only do this if you accept that deleted staging objects cannot be recovered from the bucket. Local pending/failed TIFFs remain protected by SWOTFlow.

The Cloud check rejects incompatible storage/retention/lifecycle settings and warns about soft delete/versioning. It does not change these bucket settings for you.

[Bucket creation](https://docs.cloud.google.com/storage/docs/creating-buckets), [soft delete](https://docs.cloud.google.com/storage/docs/soft-delete), [storage pricing](https://cloud.google.com/storage/pricing).

## 6. Check permissions

For a personally owned project, your account may already have the required permissions. For an institutional project, ask its administrator for scoped access:

- On the staging bucket: create, read and delete objects, plus `storage.buckets.get` so SWOTFlow can inspect bucket settings. One possible combination is **Storage Object User** plus **Storage Legacy Bucket Reader**, limited to this bucket. An administrator can instead use a custom role with only the required permissions.
- On the EE project: **Earth Engine Resource Writer** (`roles/earthengine.writer`) and **Service Usage Consumer** (`roles/serviceusage.serviceUsageConsumer`), or equivalent existing permissions. The target collection must allow this account to create images.
- Billing setup/API enablement may require separate administrator permissions; ordinary daily uploads do not need project-wide Owner access.

Use the same account for Cloud and EE. Do not create or download a service-account key for this initial desktop setup. ChromeDriver is not used by the Cloud backend; your organisation's proxy/network policies still apply.

[EE access roles](https://developers.google.com/earth-engine/guides/access_control) and [Cloud Storage roles](https://docs.cloud.google.com/storage/docs/access-control/iam-roles).

## 7. Sign in on the computer that runs SWOTFlow

Install the official [Google Cloud CLI](https://docs.cloud.google.com/sdk/docs/install) for your operating system. Open a **new PowerShell window** after installation. These commands can run from any folder; replace `YOUR_PROJECT_ID`:

```powershell
gcloud init
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

A browser sign-in opens. Choose the authorised account. `application-default login` is essential: signing in to the website or only running `gcloud auth login` is not the same thing. Google stores a refresh credential locally for applications; it is not an Earthdata username/password. Never copy this credential into a project folder, report, GitHub repository or chat. To revoke it later, use `gcloud auth application-default revoke`.

If your organisation uses a proxy, have IT configure the Google Cloud CLI/Python environment using its approved instructions. Do not disable TLS checks or paste proxy passwords into project YAML.

[Application Default Credentials (ADC)](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment).

## 8. Prepare a small EE destination and configure SWOTFlow

For the first live pilot, create an empty **ImageCollection** in the EE Assets panel, for example `SWOTFlow_cloud_pilot`. Copy its complete asset ID. Use a separate small pilot project with a few verified mosaics; keep your production destination unchanged.

In SWOTFlow:

1. Open the project containing your prepared mosaics and their saved source/verification history. Loose TIFFs with no matching provenance are not sufficient for this Cloud backend.
2. In **Upload**, check the origin mosaic folder, destination collection, UTM selection, metadata and pyramiding settings. These settings are shared with Cloud Upload. Keep your existing scientific choices.
3. Open **Cloud Upload** and select **Google Cloud Storage** as the upload method. Enter the project ID and bucket name. Keep the dedicated prefix, choose a staged-storage limit, and choose whether verified staging copies should be removed.
4. For a small pilot, use **Wait for EE verification before next tile**. Start with one or two active ingestions. If the shared active-ingestion limit is zero, Cloud uses a bounded limit of two. It transfers one TIFF at a time while EE processes earlier TIFFs.
5. Click **Check / preview (no uploads)**. This checks authentication, bucket permissions/settings, current EE inventory and eligible local files. It does not upload, submit or delete Cloud objects. It may refresh the project's ordinary inventory/report records.
6. If the preview is correct, click **Upload / resume mosaics**. This starts transfers and can incur Cloud charges. Do not delete pending bucket objects manually.

SWOTFlow builds the ingestion metadata automatically: acquisition start/end times, configured SWOT properties, processing summaries and the existing revision/source digests. All original filenames and full processing history remain in the project. It uses bands `b1` and `b2` and the configured pyramid policy; it does not resample or reproject during upload.

[Ordinary manifest ingestion](https://developers.google.com/earth-engine/guides/image_manifest).

## 9. Review the pilot before scaling

Check the Cloud queue and Statistics. "In bucket" or "INGESTING" is not EE completion. An image becomes verified only after EE returns the expected asset, bands, times, metadata and provenance. Confirm a few images in the Code Editor/GIS at their native analysis scale, including elevation/quality masks. Metadata/byte provenance verification is not a full scientific source-to-asset pixel audit.

Review **Billing > Reports** for actual storage, operations and any regional data-transfer charges after they appear. Check whether deleted objects are soft-deleted or retained by versioning. Only then expand to a larger batch.

## 10. Run new tiles and date updates through Automation

Select the same **Upload method** in **Automation**, leave **Include upload** enabled, and use your existing preflight/Start/Resume procedure. Both ordinary and tile-by-tile yearly runs use this choice. NASA catalogue tracking, extraction, mosaicking, duplicate checks, history and date-window completeness remain shared.

Choose **Submit and continue** if you want later workflow stages to continue after confirmed task submission. Pending local mosaics remain retained. Later EE sync/reconciliation promotes successful ingestions and cleans verified Cloud copies. For a final batch, use **Sync EE / clean staging** after EE completes; otherwise its final pending staging copies can remain billable while the app is closed.

Local TIFF removal is independent: select **Remove uploaded mosaics** under the existing cleanup options if desired. Cloud copy removal does not imply local removal. Failed or unverified local mosaics remain available for recovery. A tile does not become complete merely because a file reached the bucket.

## 11. Resume after an interruption

Keep the project folder, especially `00_logs`, and reopen the same project. Select the same backend/bucket and Resume. The app retains resumable transfer information, object checksums/generations, exact ingestion request IDs, task names and provenance. A lost response is reconciled rather than treated as a successful upload or an excuse to overwrite an asset. An expired Cloud upload session may require retransferring that unfinished TIFF; completed matching objects are reused.

Do not run Browser and Cloud workers simultaneously for this project. Unresolved submissions must be reconciled with their original backend before switching. A file lock prevents concurrent workers, including workers launched in separate app windows.

On Windows, resumable session secrets are encrypted for your OS user under `%LOCALAPPDATA%/SWOTFlow/cloud-sessions/`, outside the project. On other supported systems they use a private user directory/file. Moving a project to another computer preserves its history but can require fresh authentication and retransmission of an unfinished object. Do not share session secrets.

## 12. What stays on disk and how charges are reduced

The project keeps `cloud_uploads.sqlite3` next to the existing upload report. It records transfer, ingestion and cleanup events without storing credentials. The existing project upload report/database and verification receipts remain the source for Resume, Statistics and local cleanup. Back up the project's logs and databases after processing.

The app deletes only the exact staged object generation it recorded and owns, after a fresh EE metadata/provenance check. It never deletes the EE asset. It does not list and purge unrelated bucket contents. Failed copies are retained and reported, and may consume storage until the problem is resolved. The staged-byte limit counts this project's tracked copies/reservations, not unrelated projects, soft-deleted generations, taxes or data transfer.

Storage cost is approximately the sum of each file's GiB multiplied by its hours in storage and the regional hourly rate. Continuous per-file ingestion lowers residence time compared with staging the entire continent first. Standard storage has no minimum storage duration, but requests, retention features and location-dependent transfers can add costs. Use [current Google pricing](https://cloud.google.com/storage/pricing) and the pilot bill rather than treating an estimate as a cap.

## Troubleshooting

- **Credentials not found / unauthorised:** run ADC login on this computer using the correct account; confirm EE project registration.
- **403 / permission denied:** check the roles in step 6, billing, APIs, collection access and institutional policies. Repeated retries cannot repair missing permissions.
- **Staging cap reached:** first reconcile completed EE tasks and clean their owned staging copies. Review failures, or increase the cap deliberately if the cost/storage tradeoff is acceptable. The app retains files rather than deleting pending inputs to make space.
- **EE task failed:** the file is not automatically assumed corrupted. Inspect the task error. Resume can reuse the staged object and create a new ingestion request only after a definitive failed operation.
- **Metadata/checksum/generation mismatch (BLOCKED):** retain both copies and review the discrepancy. Automatic overwrite is deliberately disabled. Do not repeatedly retry an unexplained mismatch.
- **Verified but staging retained:** check Cloud cleanup selection, object permissions, holds/retention and the cleanup detail. EE completion remains recorded even if bucket deletion fails.

For a first-time setup, complete steps 1-8 before requesting the live pilot. No Cloud billing account, bucket or production migration has been created by the development work.
