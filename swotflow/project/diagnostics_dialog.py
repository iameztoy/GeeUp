"""Background preview and explicit deletion of disposable project diagnostics."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from swotflow.project.diagnostics import (
    CATEGORIES, DEFAULT_CATEGORIES, DEFAULT_KEEP_DAYS, DiagnosticDeletion,
    DiagnosticFile, DiagnosticPreview, delete_diagnostics, preview_diagnostics,
)
from swotflow.project.insights import format_bytes


class DiagnosticCleanupDialog(tk.Toplevel):
    PAGE_SIZE = 250

    def __init__(self, parent: tk.Misc, project_root: Path, *,
                 project_busy: Callable[[], bool] = lambda: False) -> None:
        super().__init__(parent)
        self.title("Diagnostic Cleanup")
        self.geometry("980x650")
        self.minsize(700, 560)
        self.transient(parent)
        self.grab_set()
        self.project_root = project_root
        self.project_busy = project_busy
        self.busy = False
        self.preview: DiagnosticPreview | None = None
        self.page = 0
        self.events: queue.Queue = queue.Queue()
        self.after_id: str | None = None
        self.keep_days = tk.StringVar(value=str(DEFAULT_KEEP_DAYS))
        self.stopped = tk.BooleanVar(value=False)
        self.category_vars = {key: tk.BooleanVar(value=key in DEFAULT_CATEGORIES) for key in CATEGORIES}
        self.status = tk.StringVar(value="No diagnostic preview yet.")
        self.summary = tk.StringVar(value="Eligible: 0 files, 0 B")
        self.selection_summary = tk.StringVar(value="Selected: 0 files, 0 B")
        self.page_summary = tk.StringVar(value="Page 1 of 1")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        options = ttk.Frame(self, padding=12)
        options.grid(row=0, column=0, sticky="ew")
        options.columnconfigure(0, weight=1)
        self.category_buttons = {}
        self.controls = []
        for row, (key, label) in enumerate(CATEGORIES.items()):
            button = ttk.Checkbutton(options, text=label, variable=self.category_vars[key])
            button.grid(row=row, column=0, sticky="w")
            self.controls.append(button)
            self.category_buttons[key] = button
        retention = ttk.Frame(options)
        retention.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(retention, text="Keep most recent days").grid(row=0, column=0)
        self.retention = ttk.Spinbox(retention, from_=1, to=3650, width=6, textvariable=self.keep_days)
        self.retention.grid(row=0, column=1, padx=8)
        self.controls.append(self.retention)

        actions = ttk.Frame(self, padding=(12, 0))
        actions.grid(row=1, column=0, sticky="ew")
        self.preview_button = ttk.Button(actions, text="Preview Diagnostics", command=self.start_preview)
        self.preview_button.grid(row=0, column=0, sticky="w")
        self.delete_selected = ttk.Button(actions, text="Delete Selected", command=lambda: self.start_delete(False))
        self.delete_selected.grid(row=0, column=1, padx=8)
        self.delete_all = ttk.Button(actions, text="Delete All Previewed", command=lambda: self.start_delete(True))
        self.delete_all.grid(row=0, column=2)
        self.controls.extend([self.preview_button, self.delete_selected, self.delete_all])
        self.stopped_button = ttk.Checkbutton(actions, variable=self.stopped,
                                             text="All project processing and upload processes are stopped")
        self.stopped_button.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.controls.append(self.stopped_button)

        totals = ttk.Frame(self, padding=12)
        totals.grid(row=2, column=0, sticky="ew")
        ttk.Label(totals, textvariable=self.summary).grid(row=0, column=0, sticky="w")
        ttk.Label(totals, textvariable=self.selection_summary).grid(row=1, column=0, sticky="w")
        body = ttk.Frame(self, padding=(12, 0))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(body, columns=("category", "size", "modified", "path"),
                                  show="headings", selectmode="extended", height=8)
        for key, label, width in (("category", "Category", 150), ("size", "Size", 85),
                                  ("modified", "Modified", 140), ("path", "Project-relative file", 460)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=70, stretch=key == "path",
                              anchor="e" if key == "size" else "w")
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(body, command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(body, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.bind("<<TreeviewSelect>>", lambda _event: self.update_selection())
        paging = ttk.Frame(self, padding=12)
        paging.grid(row=4, column=0, sticky="ew")
        self.previous = ttk.Button(paging, text="Previous Page", command=lambda: self.change_page(-1))
        self.previous.grid(row=0, column=0)
        ttk.Label(paging, textvariable=self.page_summary, width=18, anchor="center").grid(row=0, column=1)
        self.next = ttk.Button(paging, text="Next Page", command=lambda: self.change_page(1))
        self.next.grid(row=0, column=2)
        self.controls.extend([self.previous, self.next])
        footer = ttk.Frame(self, padding=12, height=92)
        footer.grid(row=5, column=0, sticky="ew")
        footer.grid_propagate(False)
        footer.columnconfigure(0, weight=1)
        status_label = ttk.Label(footer, textvariable=self.status, width=1, wraplength=650)
        status_label.grid(row=0, column=0, sticky="ew")
        footer.bind("<Configure>", lambda event: status_label.configure(wraplength=max(200, event.width - 24)))
        self.progress = ttk.Progressbar(footer, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=8)

        for variable in [self.keep_days, *self.category_vars.values()]:
            variable.trace_add("write", self.invalidate_preview)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.set_busy(False)
        self.after_id = self.after(100, self.poll)

    def invalidate_preview(self, *_args: object) -> None:
        self.preview = None
        self.table.delete(*self.table.get_children())
        self.summary.set("Preview required after changing filters.")
        self.selection_summary.set("Selected: 0 files, 0 B")
        for key, button in self.category_buttons.items():
            button.configure(text=CATEGORIES[key])
        self.set_busy(self.busy)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for control in self.controls:
            control.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self.progress.start()
        else:
            self.progress.stop()
        if not self.preview or not self.preview.candidates:
            self.delete_selected.configure(state=tk.DISABLED)
            self.delete_all.configure(state=tk.DISABLED)

    def start_preview(self) -> None:
        if self.busy:
            return
        try:
            days = int(self.keep_days.get())
            if days < 1:
                raise ValueError()
        except ValueError:
            messagebox.showwarning("Retention", "Keep at least 1 day of recent diagnostics.", parent=self)
            return
        categories = tuple(key for key, value in self.category_vars.items() if value.get())
        if not categories:
            messagebox.showwarning("Categories", "Select at least one diagnostic category.", parent=self)
            return
        self.set_busy(True)
        self.status.set("Scanning diagnostic files...")
        threading.Thread(target=self.scan_worker, args=(days, categories), daemon=True).start()

    def scan_worker(self, days: int, categories: tuple[str, ...]) -> None:
        try:
            result = preview_diagnostics(self.project_root, keep_days=days, categories=categories,
                                         progress=lambda text: self.events.put(("progress", text)))
            self.events.put(("preview", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def display_preview(self, preview: DiagnosticPreview) -> None:
        self.preview = preview
        self.page = 0
        self.summary.set(f"Eligible: {len(preview.candidates):,} files, {format_bytes(preview.size_bytes)}")
        for key, button in self.category_buttons.items():
            items = [item for item in preview.candidates if item.category == key]
            button.configure(text=f"{CATEGORIES[key]}: {len(items):,} files, {format_bytes(sum(item.size_bytes for item in items))}")
        self.render_page()
        self.status.set(f"Protected recent diagnostics: {preview.recent_files:,} files, {format_bytes(preview.recent_bytes)}. "
                        f"Scan warnings: {len(preview.warnings)}. History, backups, browser profiles and rasters excluded.")
        self.set_busy(False)
        if preview.warnings:
            messagebox.showwarning("Some paths were not inspected", "\n".join(preview.warnings[:8]), parent=self)

    def render_page(self) -> None:
        self.table.delete(*self.table.get_children())
        if not self.preview:
            return
        start = self.page * self.PAGE_SIZE
        for index, item in enumerate(self.preview.candidates[start:start + self.PAGE_SIZE], start):
            self.table.insert("", tk.END, iid=str(index), values=(CATEGORIES[item.category],
                format_bytes(item.size_bytes), datetime.fromtimestamp(item.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M"),
                item.relative_path))
        pages = max(1, (len(self.preview.candidates) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_summary.set(f"Page {self.page + 1} of {pages}")
        self.update_selection()

    def change_page(self, direction: int) -> None:
        if not self.busy and self.preview:
            last = max(0, (len(self.preview.candidates) - 1) // self.PAGE_SIZE)
            self.page = max(0, min(last, self.page + direction))
            self.render_page()

    def selected_files(self) -> list[DiagnosticFile]:
        if not self.preview:
            return []
        return [self.preview.candidates[int(index)] for index in self.table.selection()]

    def update_selection(self) -> None:
        items = self.selected_files()
        self.selection_summary.set(f"Selected: {len(items):,} files, {format_bytes(sum(item.size_bytes for item in items))}")

    def start_delete(self, all_previewed: bool) -> None:
        if self.busy or not self.preview:
            return
        if self.project_busy() or not self.stopped.get():
            messagebox.showwarning("Stop project work first",
                "Stop all processing and uploads, including separate terminals and other app instances, then confirm the checkbox.", parent=self)
            return
        items = list(self.preview.candidates) if all_previewed else self.selected_files()
        if not items:
            messagebox.showinfo("No selection", "Select diagnostic file rows first.", parent=self)
            return
        if not messagebox.askyesno("Delete diagnostic files",
            f"Permanently delete {len(items):,} diagnostic files ({format_bytes(sum(item.size_bytes for item in items))})?\n\n"
            f"Project: {self.project_root}\n\n"
            "Only the previewed diagnostics are eligible. Screenshots, HTML and text logs are troubleshooting evidence; "
            "deleting them limits investigation of old errors. Database history, manifests, checkpoints, backups, "
            "browser profiles and rasters will not be deleted. This does not use the Recycle Bin.", parent=self):
            return
        self.set_busy(True)
        self.status.set("Deleting selected diagnostics...")
        threading.Thread(target=self.delete_worker, args=(self.preview, items), daemon=True).start()

    def delete_worker(self, preview: DiagnosticPreview, items: list[DiagnosticFile]) -> None:
        try:
            result = delete_diagnostics(preview, items, confirmed_stopped=True,
                                        progress=lambda text: self.events.put(("progress", text)))
            self.events.put(("deleted", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def poll(self) -> None:
        latest_progress = ""
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "progress":
                latest_progress = value
                continue
            latest_progress = ""
            self.set_busy(False)
            if kind == "preview":
                self.display_preview(value)
            elif kind == "deleted":
                result: DiagnosticDeletion = value
                self.invalidate_preview()
                message = (f"Deleted {result.deleted_files:,} files, {format_bytes(result.deleted_bytes)}. "
                           f"Skipped/failed: {len(result.skipped)}. Preview again for remaining files.")
                self.status.set(message)
                details = message + f"\n\nAudit: {result.audit_path}"
                if result.skipped:
                    messagebox.showwarning("Diagnostic cleanup finished", details + "\n\n" + "\n".join(result.skipped[:8]), parent=self)
                else:
                    messagebox.showinfo("Diagnostic cleanup finished", details, parent=self)
            else:
                self.invalidate_preview()
                self.status.set("Operation failed; preview again before deleting.")
                messagebox.showerror("Diagnostic cleanup", str(value), parent=self)
        if latest_progress:
            self.status.set(latest_progress)
        self.after_id = self.after(100, self.poll)

    def close(self) -> None:
        if self.busy:
            messagebox.showwarning("Operation in progress", "Wait until diagnostic cleanup finishes.", parent=self)
            return
        if self.after_id:
            self.after_cancel(self.after_id)
        self.grab_release()
        self.destroy()
