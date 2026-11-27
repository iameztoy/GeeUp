"""Continuous optional Cloud uploader using the existing planning/report contract."""
from pathlib import Path
import time
import uuid
from swotflow.earthengine.uploader import EarthEngineUIUploader, UploadItem, now_iso
from swotflow.earthengine.cloud.config import CloudConfig
from swotflow.earthengine.cloud.journal import CloudJournal, utc
from swotflow.earthengine.cloud.manifest import build_manifest, timestamp
from swotflow.earthengine.cloud.secrets import SessionSecrets
from swotflow.earthengine.cloud.transport import GoogleTransport, file_checksums, CloudHTTPError, CloudServiceError
from swotflow.verification.pipeline import VerificationStore, artifact_current, ingestion_properties, digest
from swotflow.consolidation.core import snapshot_asset

PENDING = {"PREPARED", "TRANSFERRING", "STAGED", "SUBMITTING", "INGESTING"}


class CloudUploader(EarthEngineUIUploader):
    """Reuse source selection, asset naming, metadata and the established reports.

    No browser methods are called. One resumable transfer overlaps bounded EE
    ingestion. The durable journal is the authority for uncertainty and cleanup.
    """
    def __init__(self, *args, transport=None, secrets=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cloud = self.config.cloud
        self.cloud.validate()
        if not self.config.verified_workflow or not self.config.mosaic_manifest_csv:
            raise ValueError("Cloud Upload requires verified mosaic/source history. Use the project's verified extraction/mosaic workflow first.")
        self.store = VerificationStore(self.config.mosaic_manifest_csv.parent)
        self.journal = CloudJournal(self.config.artifacts.report_csv.parent / "cloud_uploads.sqlite3")
        self.transport = transport
        self.secrets = secrets or SessionSecrets(self.journal.path.resolve())
        self.owner = self.journal.owner
        self.connection_checked = False
        self._terminal_reviewed = set()
        self._initial_reconcile_done = False

    def connect(self):
        if self.transport is None:
            self.transport = GoogleTransport(self.cloud)
        self.ee_client = self.transport.ee
        if not self.connection_checked:
            target = self.transport.asset(self.config.destination_parent)
            if not target or target.get("type") != "IMAGE_COLLECTION":
                raise ValueError("Cloud Upload destination must be an existing writable EE ImageCollection. Create/select it first.")
            self.connection_checked = True

    def sync_earth_engine_inventory(self):
        self.connect()
        records = self.list_earth_engine_assets(self.ee_client)
        self.write_asset_inventory(records)
        self.ee_existing_asset_ids = {r.asset_id for r in records if r.asset_type in {"IMAGE", ""}}
        self.ee_inventory_synced = True
        self.ee_operations_by_asset = self.list_earth_engine_operations(self.ee_client)
        self.reconcile_unconfirmed_report_rows()
        for row in self.rows():
            if row["state"] == "VERIFIED":
                asset = getattr(self, "_verified_inventory", {}).get(row["asset_id"])
                try:
                    if asset is None:
                        raise ValueError("Previously verified EE asset is absent from the current inventory. Explicit recovery/replacement review required.")
                    self.validate_asset(row, asset)
                except ValueError as exc:
                    row.update(state="BLOCKED", error=str(exc)); self.save(row)

    def rows(self, pending_only=False):
        return self.journal.rows(self.config.destination_parent, PENDING if pending_only else None)

    def item(self, row):
        return UploadItem(local_file=Path(row["local_file"]), asset_name=row["asset_id"].rsplit("/", 1)[-1],
                          asset_id=row["asset_id"], source_utm_tiles=row.get("source_utm_tiles", []),
                          output_grid=row.get("output_grid", ""), product_identity=row.get("product_identity", ""),
                          processing_level=row.get("processing_level", ""), ingestion_revision=row["receipt"])

    def save(self, row):
        if row["state"] == "BLOCKED":
            # Do not let the shared inventory binder approve a revision rejected
            # by stricter Cloud time/band/metadata checks on a later sync.
            self.store.record_ingestion({**row["receipt"], "phase": "rejected",
                                         "rejected_at": utc(), "reason": row.get("error", "")})
        self.journal.put(row)
        status = {"VERIFIED": "EE_VERIFIED_EXISTS", "FAILED": "ERROR", "BLOCKED": "ERROR",
                  "INGESTING": "RUNNING", "SUBMITTING": "SUBMITTED_PENDING_VERIFICATION"}.get(row["state"], "PLANNED_UPLOAD")
        # Preserve the planned metadata even after authorized local cleanup.
        report = dict(row["report"])
        report.update(final_status=status, detected_task_name=row.get("operation", ""),
                      submit_time=row.get("submitted_at", ""), error_message=row.get("error", ""),
                      ee_asset_exists="yes" if row["state"] == "VERIFIED" else "no",
                      ee_verified_at=row.get("verified_at", ""),
                      verification_source="ee.data.getAsset + revision/metadata" if row["state"] == "VERIFIED" else "")
        self.report.upsert(report, durable=True)

    def prepare(self, item):
        existing = self.journal.get(item.asset_id)
        proof = self.store.artifact(item.local_file)
        if not proof or proof.get("stage") != "mosaic" or not artifact_current(proof):
            raise ValueError("Missing or changed verified mosaic: " + item.local_file.name)
        if proof.get("grid", {}).get("bands") != 2:
            raise ValueError("Cloud upload requires the verified two-band WSE/quality mosaic grid.")
        if existing:
            if existing["sha256"] != proof["sha256"] or existing["receipt"]["sources"] != proof["sources"]:
                raise ValueError("An earlier Cloud upload owns this asset ID with different bytes/sources. Explicit replacement is required.")
            if existing["bucket"] != self.cloud.bucket or not existing["object"].startswith(self.cloud.prefix + "/"):
                raise ValueError("Pending Cloud history uses another bucket/prefix. Restore those settings and reconcile it first.")
            if existing["state"] == "FAILED":
                # Only a definitive terminal operation failure permits a fresh request ID.
                existing.update(state="STAGED", request_id=uuid.uuid4().hex, operation="", error="", submitted_at="")
                self.save(existing)
            return existing
        sha, md5 = file_checksums(item.local_file)
        if sha != proof["sha256"]:
            raise ValueError("Mosaic changed while preparing Cloud upload.")
        receipt = self.store.prepare_ingestion(item.asset_id, proof)
        item.ingestion_revision = receipt
        row = {"asset_id": item.asset_id, "collection": self.config.destination_parent, "owner": self.owner,
               "local_file": str(item.local_file), "sha256": sha, "md5": md5, "size": item.local_file.stat().st_size,
               "receipt": receipt, "bucket": self.cloud.bucket, "object": f"{self.cloud.prefix}/{self.owner}/{receipt['token']}/{item.asset_name}.tif",
               "secret_id": uuid.uuid4().hex, "request_id": uuid.uuid4().hex, "state": "PREPARED",
               "source_utm_tiles": item.source_utm_tiles, "output_grid": item.output_grid,
               "product_identity": item.product_identity, "processing_level": item.processing_level,
               "created_at": utc(), "report": self.make_report_row(item, "", "", "PLANNED_UPLOAD", "")}
        row["manifest"] = build_manifest(self.config, item, self.parse_metadata_for_item(item, required=True), row)
        self.save(row)
        return row

    def validate_asset(self, row, asset):
        expected = row["manifest"]
        if asset.get("id", asset.get("name")) != row["asset_id"]:
            raise ValueError("EE returned a different asset identity.")
        if asset.get("type") != "IMAGE" or [b.get("id") for b in asset.get("bands", [])] != ["b1", "b2"]:
            raise ValueError("EE asset is not the expected two-band image.")
        if any(asset.get("properties", {}).get(k) != v for k, v in expected["properties"].items()):
            raise ValueError("EE asset metadata/revision differs from this submitted mosaic.")
        for field in ("startTime", "endTime"):
            if field in expected and (not asset.get(field) or timestamp(asset[field]) != timestamp(expected[field])):
                raise ValueError("EE acquisition time differs from the ingestion manifest.")
        previous = self.store.latest_asset_event("cloud", row["asset_id"])
        if previous.get("passed") is False and previous.get("asset_fingerprint") == digest(snapshot_asset(asset)):
            raise ValueError("An independent pixel check failed for this EE revision; retain it for review.")

    def reconcile(self):
        """Read current assets/operations, including rows whose local TIFF was cleaned."""
        self.connect()
        for row in self.rows(pending_only=self._initial_reconcile_done):
            if row["asset_id"] in getattr(self, "_transfers_in_flight", set()):
                continue  # Only the owning scheduler may settle an active local transfer.
            if row["state"] == "BLOCKED":
                continue
            if row["state"] == "VERIFIED" and row.get("deleted_at"):
                continue
            if row["state"] in {"VERIFIED", "FAILED"} and row["asset_id"] in self._terminal_reviewed:
                continue
            try:
                asset = self.transport.asset(row["asset_id"])
                if asset:
                    self.validate_asset(row, asset)
                    self.store.record_ingestion({**row["receipt"], "phase": "confirmed", "task_id": row.get("operation", ""),
                        "observed_utc": utc(), "asset_fingerprint": digest(snapshot_asset(asset))})
                    row.update(state="VERIFIED", verified_at=utc(), error="")
                    self.save(row)
                    self.cleanup(row)
                    self._terminal_reviewed.add(row["asset_id"])
                elif row.get("operation") and row["state"] in {"SUBMITTING", "INGESTING"}:
                    operation = self.transport.operation(row["operation"])
                    if operation.get("done") and operation.get("error"):
                        row.update(state="FAILED", error="EE ingestion failed: " + str(operation["error"].get("message", "unknown error")))
                        self.save(row)
                        self._terminal_reviewed.add(row["asset_id"])
                    # A succeeded operation without a visible matching asset remains pending.
            except ValueError as exc:
                row.update(state="BLOCKED", error=str(exc)); self.save(row)
            except Exception as exc:
                # Read failure is not proof of missing data or of task failure.
                self.logger.warning("Cloud reconciliation deferred for %s (%s).", row["asset_id"], type(exc).__name__)
        self._initial_reconcile_done = True

    def cleanup(self, row):
        if not self.cloud.delete_after_verification or row.get("deleted_at") or row["state"] != "VERIFIED":
            return
        if row["owner"] != self.owner or not row.get("generation"):
            return
        if self.journal.other_pending_reference(row):
            return
        try:
            # Re-read immediately before deletion: a historical successful task is insufficient.
            asset = self.transport.asset(row["asset_id"])
            if not asset:
                raise ValueError("EE asset no longer present; retain staging copy.")
            self.validate_asset(row, asset)
            self.transport.delete(row)
            row.update(deleted_at=utc(), cleanup_error="")
            self.save(row)
        except Exception as exc:
            # Cloud cleanup failure must not turn verified ingestion into a failed upload.
            row["cleanup_error"] = f"Staging cleanup deferred ({type(exc).__name__}); check permissions/retention and retry sync."
            self.save(row)
            self.logger.warning("%s: %s", row["asset_id"], row["cleanup_error"])

    def prepare_transfer(self, row):
        """Validate and persist ownership on the main writer before sending bytes."""
        if not Path(row["local_file"]).is_file():
            root = self.config.input_folder
            matches = list(root.rglob(Path(row["local_file"]).name)) if self.config.upload.recursive else [root / Path(row["local_file"]).name]
            candidates = [path for path in matches if path.is_file() and
                          (self.store.artifact(path) or {}).get("sha256") == row["sha256"]]
            if len(candidates) == 1:
                row["local_file"] = str(candidates[0])
                row["report"]["local_file"] = row["local_file"]
                self.save(row)
        proof = self.store.artifact(Path(row["local_file"]))
        if not proof or proof["sha256"] != row["sha256"] or not artifact_current(proof):
            raise ValueError("Retained local mosaic changed; transfer cannot resume.")
        row["state"] = "TRANSFERRING"; self.save(row)

    def advance(self, row):
        if row["state"] in {"PREPARED", "TRANSFERRING"}:
            self.prepare_transfer(row)
            last_monitor = time.monotonic()
            def progress(offset):
                nonlocal last_monitor
                now = time.monotonic()
                if now - last_monitor >= self.config.execution.task_poll_seconds:
                    # Keep verifying/deleting earlier ingestions while a large TIFF
                    # is still transferring, without concurrent report writers.
                    self.logger.info("Cloud transfer %.1f%%: %s", 100 * offset / row["size"], row["asset_id"])
                    self.reconcile()
                    last_monitor = now
            generation = self.transport.upload(row, Path(row["local_file"]), self.secrets, progress)
            row.update(generation=generation, state="STAGED", staged_at=utc(), error=""); self.save(row)
        if row["state"] in {"STAGED", "SUBMITTING"}:
            obj = self.transport.object(row)
            if obj is None:
                raise ValueError("Staged object is absent; retain local mosaic and review bucket lifecycle rules.")
            self.transport.check_object(row, obj)
            asset = self.transport.asset(row["asset_id"])
            if asset:
                self.validate_asset(row, asset)
                self.reconcile(); return
            # Persist intent and exact request ID BEFORE the remote call. Repeating this ID
            # after a lost response does not create another ingestion operation.
            row.update(state="SUBMITTING", submitted_at=row.get("submitted_at") or utc())
            self.save(row)
            self.store.record_ingestion({**row["receipt"], "phase": "attempted", "request_id": row["request_id"]})
            name = self.transport.submit(row["request_id"], row["manifest"])
            if not name:
                raise RuntimeError("No EE operation identifier returned; saved request ID retained for reconciliation.")
            row.update(state="INGESTING", operation=name, error=""); self.save(row)
            self.store.record_ingestion({**row["receipt"], "phase": "submitted", "task_id": name})

    def sync_existing_assets_to_report(self):
        self.reconcile()
        return super().sync_existing_assets_to_report()

    def _run(self):
        self.connect()
        for row in self.rows():
            if row["state"] in PENDING and (row["bucket"] != self.cloud.bucket or
                    not row["object"].startswith(self.cloud.prefix + "/")):
                raise ValueError("Unfinished Cloud history uses another bucket/prefix. Restore its settings before Resume.")
        info = self.transport.bucket_info()
        for setting, enabled in (("soft delete", int(info.get("softDeletePolicy", {}).get("retentionDurationSeconds", 0)) > 0),
                                 ("versioning", info.get("versioning", {}).get("enabled", False))):
            if enabled:
                self.logger.warning("Bucket %s is enabled: removed objects can remain billable. See the setup guide.", setting)
        if info.get("lifecycle", {}).get("rule"):
            raise ValueError("Use a dedicated staging bucket without age-based lifecycle deletion; it can remove pending ingestion inputs.")
        if self.config.execution.dry_run:
            items = self.build_upload_plan()
            self.logger.info("Cloud dry run: %s eligible mosaics. No objects uploaded, ingestions submitted or staging copies deleted.", len(items))
            return 0
        self.reconcile()
        previous_rows = {key: dict(value) for key, value in self.report.rows.items()}
        items = self.build_upload_plan()
        # Never transfer a retained browser uncertainty via another backend.
        for item in items:
            prior = previous_rows.get(item.asset_id, {})
            if not self.journal.get(item.asset_id) and prior.get("submit_time") and prior.get("final_status") in {
                    "UNKNOWN_AFTER_CLICK", "SUBMITTED", "SUBMITTED_PENDING_VERIFICATION", "RUNNING", "READY"}:
                raise ValueError("Unresolved browser upload exists. Reconcile it using Browser before switching backend: " + item.asset_name)
        if not self.assume_yes and self.config.execution.require_confirmation:
            if input(f"Upload {len(items)} mosaic(s) through paid Cloud Storage? Type yes: ").strip().lower() != "yes":
                return 0
        deadline = time.monotonic() + self.config.execution.wait_timeout_minutes * 60
        if self.cloud.concurrent_uploads > 1:
            from swotflow.earthengine.cloud.parallel import run_parallel
            remaining = run_parallel(self, items, deadline)
            return self.finish_queue(remaining)
        attempts = {}
        remaining = list(items)
        max_tasks = self.config.upload.max_active_ingestions or 2
        while True:
            if time.monotonic() >= deadline:
                self.logger.warning("Cloud monitoring deadline reached; queue saved for Resume.")
                break
            self.reconcile()
            rows = self.rows(pending_only=True)
            active = sum(r["state"] in {"SUBMITTING", "INGESTING"} for r in rows)
            # Continue saved transfers even if the current yearly scope excludes their local file.
            ready = [r for r in rows if r["state"] in {"PREPARED", "TRANSFERRING", "STAGED", "SUBMITTING"}
                     and attempts.get(r["asset_id"], 0) < self.config.upload.retry_attempts]
            row = next((r for r in ready if r["state"] == "SUBMITTING" or active < max_tasks), None)
            if row is None and remaining and active < max_tasks:
                item = remaining[0]
                old = self.journal.get(item.asset_id)
                size = item.local_file.stat().st_size
                if old or self.journal.live_bytes(self.cloud.bucket) + size <= self.cloud.staging_limit_gib * 2**30:
                    remaining.pop(0)
                    row = self.prepare(item)
                    if row["state"] in {"VERIFIED", "BLOCKED", "INGESTING"}:
                        continue
                elif not active:
                    self.logger.error(self.staging_cap_message(item))
                    break
            if row:
                remaining = [item for item in remaining if item.asset_id != row["asset_id"]]
                try:
                    self.logger.info("Cloud %s: %s", row["state"].lower(), row["asset_id"])
                    self.advance(row)
                except ValueError as exc:
                    row.update(state="BLOCKED", error=str(exc)); self.save(row)
                    if self.config.upload.fail_fast:
                        break
                except Exception as exc:
                    attempts[row["asset_id"]] = attempts.get(row["asset_id"], 0) + 1
                    row["error"] = (str(exc) if isinstance(exc, (CloudHTTPError, CloudServiceError)) else
                                    f"Cloud attempt interrupted ({type(exc).__name__}); retained for resumable retry.")
                    self.save(row)
                    self.logger.warning("%s: %s", row["asset_id"], row["error"])
                    if self.config.upload.fail_fast:
                        break
                    time.sleep(self.config.upload.retry_wait_seconds)
                continue
            rows = self.rows(pending_only=True)
            unresolved = [r for r in rows if r["state"] in PENDING]
            if not remaining and (not unresolved or
                    (self.config.upload.completion_mode == "submit_and_continue" and all(r["state"] == "INGESTING" for r in unresolved))):
                break
            monitorable = any(r["state"] == "INGESTING" and r.get("operation") for r in rows)
            if not active or (not ready and not monitorable) or time.monotonic() >= deadline:
                self.logger.warning("Cloud queue retained for Resume; pending tasks are not resubmitted or cleaned.")
                break
            time.sleep(self.config.execution.task_poll_seconds)
        return self.finish_queue(remaining)

    def staging_cap_message(self, item):
        """Explain a blocked new transfer without changing a user-selected limit."""
        size=item.local_file.stat().st_size
        cap=self.cloud.staging_limit_gib
        if size > cap * 2**30:
            return (f"Mosaic {item.local_file.name} is {size/2**30:.3f} GiB, larger than the "
                    f"{cap:g} GiB staging cap. No new transfer started for this file. "
                    "Retain it and explicitly raise the cap after review, or prepare a losslessly compressed replacement. "
                    "Resume does not recompress existing verified mosaics.")
        return (f"Cloud staging cap {cap:g} GiB reached; the next mosaic needs {size/2**30:.3f} GiB. "
                "Reconcile pending/failed staging copies or review the cap. Files remain available for Resume.")

    def finish_queue(self, remaining):
        """Use the same terminal accounting for serial and concurrent transfers."""
        self.reconcile()
        self.sync_earth_engine_inventory()
        rows = self.rows()
        failed = any(r["state"] in {"FAILED", "BLOCKED"} or r.get("error") for r in rows)
        pending = any(r["state"] in PENDING for r in rows)
        self.logger.info("Cloud queue: %s verified, %s pending, %s failed/blocked. Local cleanup remains a separate option.",
            sum(r["state"] == "VERIFIED" for r in rows), sum(r["state"] in PENDING for r in rows),
            sum(r["state"] in {"FAILED", "BLOCKED"} for r in rows))
        allowed_pending = self.config.upload.completion_mode == "submit_and_continue" and all(
            r["state"] == "INGESTING" for r in rows if r["state"] in PENDING)
        return 1 if failed or remaining or (pending and not allowed_pending) else 0
