"""Cloud staging settings and queue, sharing the existing manual upload controls."""
from dataclasses import asdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
from swotflow.earthengine.cloud.config import BACKENDS, CloudConfig
from swotflow.earthengine.cloud.journal import read_rows, cloud_metrics
from swotflow.paths import APP_ROOT


class CloudUploadTab:
    def __init__(self, parent, app):
        self.app, self.parent = app, parent
        from swotflow.ui.scrolling import VerticalScrolledFrame
        parent.columnconfigure(0, weight=1); parent.rowconfigure(0, weight=1)
        self.scroller = VerticalScrolledFrame(parent)
        self.scroller.grid(row=0, column=0, sticky="nsew")
        parent = self.scroller.content
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(11, weight=1)
        ttk.Label(parent, text="Cloud Upload - pilot candidate", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(parent, text="Start with a small test collection. Transfer a mosaic, start its EE ingestion, and upload the next while EE works. "
                  "Verified staging copies can be removed automatically. Browser is the default; select one upload method for the run.",
                  wraplength=900).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(parent, text="Upload method (also used by Automation)").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Combobox(parent, textvariable=app.upload_backend_var, values=list(BACKENDS), state="readonly", width=35).grid(
            row=2, column=1, sticky="w")
        self.project = tk.StringVar(); self.bucket = tk.StringVar(); self.prefix = tk.StringVar()
        self.limit = tk.StringVar(); self.remove = tk.BooleanVar()
        for row, label, variable in ((3, "Cloud project ID", self.project), (4, "Bucket name (without gs://)", self.bucket),
                                      (5, "Dedicated staging prefix", self.prefix), (6, "Staging storage limit (GiB)", self.limit)):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(parent, textvariable=variable, width=48).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Checkbutton(parent, text="Remove Cloud staging copies after verified EE ingestion and matching provenance",
                        variable=self.remove).grid(row=7, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(parent, text="Origin folder, destination collection, UTM selection, acquisition metadata, pyramid policy and "
                  "completion mode are shared with Upload. Local mosaic cleanup remains a separate option. "
                  "One local transfer overlaps the configured maximum active EE ingestions. Keep the app/computer running "
                  "for transfers and cleanup; EE tasks already submitted can continue in the cloud.", wraplength=900).grid(
                  row=8, column=0, columnspan=3, sticky="w", pady=4)
        actions = ttk.Frame(parent); actions.grid(row=9, column=0, columnspan=3, sticky="w", pady=8)
        for label, command in (("Setup guide", self.guide), ("Check / preview (no uploads)", lambda: self.run("preview")),
                               ("Upload / resume mosaics", lambda: self.run("upload")), ("Sync EE / clean staging", lambda: self.run("sync")),
                               ("Refresh queue", self.refresh)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 6))
        self.summary = tk.StringVar(value="No Cloud upload history yet.")
        ttk.Label(parent, textvariable=self.summary, wraplength=900).grid(row=10, column=0, columnspan=3, sticky="w", pady=4)
        frame = ttk.Frame(parent); frame.grid(row=11, column=0, columnspan=3, sticky="nsew")
        frame.columnconfigure(0, weight=1); frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("asset", "state", "staging", "detail"), show="headings", height=9)
        for col, title, width in (("asset", "Mosaic", 400), ("state", "EE / transfer state", 125),
                                   ("staging", "Cloud copy", 90), ("detail", "Last detail", 300)):
            self.tree.heading(col, text=title); self.tree.column(col, width=width)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns"); self.tree.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew"); self.tree.configure(xscrollcommand=horizontal.set)
        self.load(app.data)
        self._timer = self.parent.after(5000, self.tick)
        self.parent.bind("<Destroy>", self.dispose, add="+")

    def load(self, config):
        c = CloudConfig.from_dict(config.get("cloud_upload", {}))
        self.project.set(c.project); self.bucket.set(c.bucket); self.prefix.set(c.prefix)
        self.limit.set(str(c.staging_limit_gib)); self.remove.set(c.delete_after_verification)
        self._chunk_mib = c.chunk_mib

    def values(self):
        return asdict(CloudConfig(self.project.get().strip(), self.bucket.get().strip(), self.prefix.get().strip(),
                                 float(self.limit.get() or "50"), self.remove.get(), self._chunk_mib))

    def validate(self):
        CloudConfig.from_dict(self.values()).validate()

    def guide(self):
        webbrowser.open((APP_ROOT / "docs" / "cloud-upload-setup.html").as_uri())

    def run(self, action):
        if self.app.automation_running or self.app.automation_preflight_running:
            messagebox.showinfo("Automation active", "Wait until the active stage/run finishes. It keeps its original upload method.")
            return
        if BACKENDS.get(self.app.upload_backend_var.get()) != "gcs":
            messagebox.showinfo("Select Cloud upload", "Choose Google Cloud Storage above to use this queue. Browser remains available in Upload.")
            return
        try:
            self.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Cloud settings", str(exc)); return
        if action == "preview":
            self.app.save_and_run_dry_run()
        elif action == "upload":
            self.app.save_and_run_real()
        else:
            self.app.sync_ee_assets()

    def refresh(self):
        path = self.app.current_upload_report_path()
        destination = self.app.destination_var.get().strip().rstrip("/")
        try:
            rows = [r for r in read_rows(Path(path).parent / "cloud_uploads.sqlite3") if r.get("collection") == destination]
            metrics = cloud_metrics(path, destination)
            self.summary.set(" | ".join(f"{k.replace('Cloud ', '')}: {v}" for k, v in metrics.items()) or "No Cloud upload history yet.")
            self.tree.delete(*self.tree.get_children())
            for row in sorted(rows, key=lambda r: (r["state"] == "VERIFIED", r.get("updated_at", "")))[:300]:
                self.tree.insert("", "end", values=(row["asset_id"].rsplit("/", 1)[-1], row["state"],
                    "removed" if row.get("deleted_at") else "retained", row.get("cleanup_error") or row.get("error", "")))
        except Exception as exc:
            self.summary.set(f"Cloud history could not be read ({type(exc).__name__}); retained records unchanged.")

    def tick(self):
        if self.parent.winfo_exists():
            # Refresh only when this tab is visible; Statistics reads the same durable journal.
            if self.app.notebook.select() == str(self.parent):
                self.refresh()
            self._timer = self.parent.after(5000, self.tick)

    def dispose(self, event):
        if event.widget == self.parent:
            try:
                self.parent.after_cancel(self._timer)
            except tk.TclError:
                pass
