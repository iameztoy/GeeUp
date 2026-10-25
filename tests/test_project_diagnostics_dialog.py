import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from swotflow.project.diagnostics import DiagnosticFile, DiagnosticPreview
from swotflow.project.diagnostics_dialog import DiagnosticCleanupDialog


class DiagnosticCleanupDialogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.dialog = DiagnosticCleanupDialog(self.root, Path(self.folder.name))
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.close_dialog)

    def close_dialog(self):
        if self.dialog.winfo_exists():
            self.dialog.busy = False
            self.dialog.close()

    def preview(self, count=2):
        return DiagnosticPreview(Path(self.folder.name), 7, [
            DiagnosticFile(f"00_logs/upload_artifacts/error_20260101_{index:06d}.png", "screenshots", 100, 0, 0, 1, index)
            for index in range(count)
        ])

    def test_preview_runs_in_background_and_displays_size(self):
        with mock.patch("swotflow.project.diagnostics_dialog.threading.Thread") as thread:
            self.dialog.start_preview()
            thread.return_value.start.assert_called_once()
        self.assertTrue(self.dialog.busy)
        self.dialog.after_cancel(self.dialog.after_id)
        self.dialog.events.put(("preview", self.preview()))
        self.dialog.poll()
        self.assertFalse(self.dialog.busy)
        self.assertEqual(len(self.dialog.table.get_children()), 2)
        self.assertIn("200", self.dialog.summary.get())
        self.dialog.table.selection_set("0")
        self.dialog.update_selection()
        self.assertIn("100", self.dialog.selection_summary.get())

    def test_delete_requires_confirmation_stopped_and_idle(self):
        self.dialog.display_preview(self.preview())
        with mock.patch("swotflow.project.diagnostics_dialog.messagebox.showwarning"), mock.patch("swotflow.project.diagnostics_dialog.threading.Thread") as thread:
            self.dialog.start_delete(True)
            thread.assert_not_called()
            self.dialog.stopped.set(True)
            self.dialog.project_busy = lambda: True
            self.dialog.start_delete(True)
            thread.assert_not_called()
        self.dialog.project_busy = lambda: False
        with mock.patch("swotflow.project.diagnostics_dialog.messagebox.askyesno", return_value=False), mock.patch("swotflow.project.diagnostics_dialog.threading.Thread") as thread:
            self.dialog.start_delete(True)
            thread.assert_not_called()

    def test_selected_deletion_only_sends_selected_rows_to_worker(self):
        preview = self.preview()
        self.dialog.display_preview(preview)
        self.dialog.stopped.set(True)
        self.dialog.table.selection_set("1")
        with mock.patch("swotflow.project.diagnostics_dialog.messagebox.askyesno", return_value=True), mock.patch("swotflow.project.diagnostics_dialog.threading.Thread") as thread:
            self.dialog.start_delete(False)
            self.assertEqual(thread.call_args.kwargs["args"], (preview, [preview.candidates[1]]))

    def test_filters_invalidate_preview_and_pages_are_bounded(self):
        preview = self.preview(600)
        self.dialog.display_preview(preview)
        self.assertEqual(len(self.dialog.table.get_children()), 250)
        self.dialog.change_page(1)
        self.assertEqual(self.dialog.table.get_children()[0], "250")
        self.dialog.keep_days.set("30")
        self.assertIsNone(self.dialog.preview)
        self.assertTrue(self.dialog.delete_all.instate(["disabled"]))

    def test_controls_fit_minimum_size(self):
        self.root.deiconify()
        self.root.update()
        self.dialog.geometry("700x560")
        self.dialog.display_preview(self.preview())
        self.dialog.update()
        for control in self.dialog.controls:
            self.assertLessEqual(control.winfo_rootx() + control.winfo_width(), self.dialog.winfo_rootx() + self.dialog.winfo_width())
        self.assertGreater(self.dialog.table.winfo_height(), 50)
        self.assertLessEqual(self.dialog.progress.winfo_rooty() + self.dialog.progress.winfo_height(), self.dialog.winfo_rooty() + self.dialog.winfo_height())


if __name__ == "__main__":
    unittest.main()
