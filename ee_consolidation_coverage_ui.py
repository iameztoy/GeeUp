"""Statistics view for the separate, local replacement register."""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ee_replacement_register as registry


class ConsolidationCoveragePanel(ttk.Frame):
    """Read metadata off the UI thread; change only the dedicated local register."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent, padding=6)
        self.app = app
        self.collection = ""
        self.path: Path | None = None
        self.report: dict = {}
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.poll_id: str | None = None
        self.start, self.end = tk.StringVar(), tk.StringVar()
        self.status = tk.StringVar(value="Use EE Consolidation > Update coverage register to import inventories and saved plans.")
        self.summary = tk.StringVar()
        self.grid(row=0, column=0, sticky="nsew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        controls = ttk.Frame(self)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="Refresh register", command=self.refresh).pack(side="left")
        ttk.Label(controls, text="From UTC date").pack(side="left", padx=5)
        ttk.Entry(controls, textvariable=self.start, width=12).pack(side="left")
        ttk.Label(controls, text="Through").pack(side="left", padx=5)
        ttk.Entry(controls, textvariable=self.end, width=12).pack(side="left")
        ttk.Button(controls, text="Export coverage JSON", command=self.export).pack(side="left", padx=6)
        self.summary_label = ttk.Label(self, textvariable=self.summary, wraplength=700, justify="left")
        self.summary_label.grid(row=1, column=0, sticky="ew", pady=6)
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew")
        for label, command in (("Selected details", self.details), ("Import comparison receipt", self.import_receipt),
                               ("Approve selected revision locally", self.approve), ("Comparison template", self.template)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 6))
        book = ttk.Notebook(self)
        book.grid(row=3, column=0, sticky="nsew", pady=6)
        self.tiles = self.table(book, "Source tile coverage", (
            ("tile", "Source tile", 95), ("status", "Status", 105), ("expected", "Registered", 85),
            ("uploaded", "Original in EE", 90), ("active", "Approved current", 105), ("pending", "Pending addition", 105),
            ("awaiting", "Awaiting approval", 115), ("missing", "Missing", 65), ("unknown", "Unknown/stale", 95),
            ("outdated", "Older version active", 120), ("conflicts", "Conflicts", 70), ("unresolved_assets", "Unresolved assets", 110)))
        self.products = self.table(book, "Replacement register", (
            ("asset_id", "Asset", 400), ("role", "Role", 130), ("availability", "EE availability", 110),
            ("verification", "Verification", 175), ("date", "UTC date", 95), ("observations", "Observations", 85)))
        self.conflicts = self.table(book, "Conflicts / untracked", (("asset_id", "Asset", 400), ("reason", "Reason", 650)))
        self.products.bind("<Double-1>", lambda event: self.details())
        self.status_label = ttk.Label(self, textvariable=self.status, wraplength=700, justify="left")
        self.status_label.grid(row=4, column=0, sticky="ew")
        self.bind("<Configure>", lambda event: self.resize(event.width) if event.widget == self else None, add="+")
        self.bind("<Destroy>", self.destroyed, add="+")

    @staticmethod
    def table(book: ttk.Notebook, title: str, columns: tuple) -> ttk.Treeview:
        """Create a scrollable table usable on small application windows."""
        frame = ttk.Frame(book)
        book.add(frame, text=title)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=55)
        tree.grid(row=0, column=0, sticky="nsew")
        y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        y.grid(row=0, column=1, sticky="ns")
        x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        x.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        return tree

    def resize(self, width: int) -> None:
        """Keep long scope labels inside the available statistics panel width."""
        for label in (self.summary_label, self.status_label):
            label.configure(wraplength=max(250, width - 24))

    def destroyed(self, event: tk.Event) -> None:
        """Cancel only this view's polling, never other jobs."""
        if event.widget == self and self.poll_id:
            self.after_cancel(self.poll_id)
            self.poll_id = None

    def work(self, job: Callable, done: Callable) -> None:
        """Keep potentially large local-register work off Tk's event loop."""
        if self.busy:
            return
        self.busy = True
        def run() -> None:
            try:
                self.events.put((done, job(), None))
            except Exception as exc:
                self.events.put((done, None, str(exc)))
        threading.Thread(target=run, daemon=True, name="consolidation-coverage").start()
        self.poll_id = self.after(100, self.poll)

    def poll(self) -> None:
        """Dispatch results only on the Tk thread."""
        self.poll_id = None
        try:
            done, value, error = self.events.get_nowait()
        except queue.Empty:
            self.poll_id = self.after(100, self.poll)
            return
        self.busy = False
        if error:
            self.report = {}
            self.summary.set("Coverage unavailable. Resolve the register error and refresh; previous counts are not current.")
            for tree in (self.tiles, self.products, self.conflicts):
                tree.delete(*tree.get_children())
            self.status.set("Register check stopped: " + error)
            self.app.stats_status_map.set_consolidation_coverage([], "Register check failed: " + error)
        else:
            done(value)

    def use_register(self, path: Path, collection: str) -> None:
        """Explicitly select the collection/register used by this statistics view."""
        self.path, self.collection = Path(path), collection
        self.refresh()

    def clear(self) -> None:
        """Remove old project coverage when the project changes."""
        self.collection, self.path, self.report = "", None, {}
        self.show({"tiles": [], "products": [], "conflicts": [], "summary": {}, "inventories": {},
                   "scope": "No register selected for this project."})

    def refresh(self, project_collection: str | None = None) -> None:
        """Read only; a normal statistics refresh creates no files or cloud calls."""
        try:
            if project_collection and project_collection != self.collection:
                self.collection = project_collection
                self.path = registry.default_path(project_collection)
            if not self.path:
                self.collection = self.app.destination_var.get().strip()
                self.path = registry.default_path(self.collection)
            register = registry.ReplacementRegister(self.path, self.collection)
            start, end, context = self.start.get().strip(), self.end.get().strip(), (self.path, self.collection)
            self.status.set("Reading replacement register...")
            self.work(lambda: register.coverage(start, end),
                      lambda value: self.show(value) if context == (self.path, self.collection) else self.refresh())
        except ValueError as exc:
            self.status.set(str(exc))

    def show(self, report: dict) -> None:
        """Render source counts and pass the same filtered rows to the status map."""
        self.report = report
        for tree, rows in ((self.tiles, report["tiles"]), (self.products, report["products"]),
                           (self.conflicts, report["conflicts"] + [{"asset_id": a, "reason": "Untracked inventory asset: import its plan/lineage"} for a in report.get("untracked", [])])):
            tree.delete(*tree.get_children())
            for row in rows:
                tree.insert("", "end", values=[row.get(k, "") for k in tree["columns"]])
        s = report["summary"]
        self.summary.set(f"Source collection: {report.get('collection', self.collection)}\n"
                         f"Registered observations: {s.get('expected', 0):,}; approved/current: {s.get('active', 0):,}; "
                         f"pending additions: {s.get('pending', 0):,}; awaiting approval: {s.get('awaiting', 0):,}; "
                         f"missing: {s.get('missing', 0):,}; unknown/stale: {s.get('unknown', 0):,}; conflicts: {s.get('conflicts', 0):,}.\n"
                         f"Stored images (whole snapshots): {s.get('stored_images', 0):,}; locally active assets: {s.get('active_assets', 0):,}; "
                         f"represented copies retained: {s.get('represented_copies', 0):,}; untracked: {s.get('untracked_assets', 0):,}; "
                         f"repeated observations in original assets: {s.get('stored_duplicate_observations', 0):,}.")
        snapshots = "; ".join(f"{name.rsplit('/', 1)[-1]}: {time or 'undated'}" for name, time in report["inventories"].items())
        caption = report["scope"] + " Inventory UTC: " + (snapshots or "not checked")
        self.status.set(caption)
        self.app.stats_status_map.set_consolidation_coverage(report["tiles"], caption)

    def selected(self) -> dict:
        """Resolve selection by asset ID, never by display ordering."""
        chosen = self.products.selection()
        if not chosen:
            raise ValueError("Select an asset in Replacement register first")
        aid = self.products.item(chosen[0], "values")[0]
        return next(p for p in self.report["products"] if p["asset_id"] == aid)

    def details(self) -> None:
        """Show exact history, errors, acquisition intervals and evidence."""
        try:
            value = self.selected()
        except ValueError:
            return
        window = tk.Toplevel(self)
        window.title("Replacement history and verification")
        text = tk.Text(window, wrap="word", width=110, height=30)
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(value, indent=2))
        text.configure(state="disabled")

    def import_receipt(self) -> None:
        """Import explicit comparison results; never infer them from export status."""
        if self.busy or not self.path:
            return
        path = filedialog.askopenfilename(parent=self, title="Comparison receipt JSON", filetypes=[("JSON", "*.json")])
        if path:
            register = registry.ReplacementRegister(self.path, self.collection)
            self.work(lambda: register.import_receipt_file(Path(path)), lambda _: self.refresh())

    def template(self) -> None:
        """Save current fingerprints for an external comparison, never mark it run."""
        if self.busy or not self.path:
            return
        try:
            product = self.selected()
        except ValueError as exc:
            messagebox.showinfo("Comparison template", str(exc), parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="Save unperformed comparison template", defaultextension=".json")
        if path:
            register = registry.ReplacementRegister(self.path, self.collection)
            self.work(lambda: register.verification_template(product["asset_id"]),
                      lambda value: Path(path).write_text(json.dumps(value, indent=2), encoding="utf-8"))

    def approve(self) -> None:
        """Explicit local approval with a concrete replacement list and duplicate check."""
        if self.busy or not self.path:
            return
        try:
            product = self.selected()
            if product["kind"] != "consolidated":
                raise ValueError("Select a consolidated revision to approve")
            if not product["verified"] or product["verification"] != "full_pixels" or product["availability"] != "present" or product["errors"]:
                raise ValueError("Import a passing full-pixel comparison receipt and refresh EE metadata first")
        except ValueError as exc:
            messagebox.showinfo("Approval", str(exc), parent=self)
            return
        # Infer only the exact approved predecessor and original source IDs. All
        # other overlapping selections remain blocked by the backend transaction.
        group = product["details"]["group"]
        possible = set(group.get("predecessors", [])) | {s["asset"]["id"] for s in group["sources"]}
        replace_ids = [p["asset_id"] for p in self.report["products"] if p["role"] == "active" and p["asset_id"] in possible]
        if not messagebox.askyesno("Approve local analysis selection", f"Approve:\n{product['asset_id']}\n\nReplace these local active entries:\n" +
                                  ("\n".join(replace_ids) or "None") + "\n\nCloud assets and external analysis scripts are unchanged. Continue?", parent=self):
            return
        register = registry.ReplacementRegister(self.path, self.collection)
        self.work(lambda: register.activate(product["asset_id"], replace_ids, "Explicit approval in Statistics after full comparison"), lambda _: self.refresh())

    def export(self) -> None:
        """Export reviewable coverage and lineage; not an automatic analysis switch."""
        if not self.report:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Save coverage report", defaultextension=".json")
        if path:
            Path(path).write_text(json.dumps(self.report, indent=2), encoding="utf-8")
