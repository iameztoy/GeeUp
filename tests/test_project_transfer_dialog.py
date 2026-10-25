import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from swotflow.project.relocation import ValidationReport
from swotflow.project.transfer_dialog import ProjectTransferDialog


class ProjectTransferDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.dialog = ProjectTransferDialog(self.root, mock.Mock())
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.close_dialog)

    def close_dialog(self):
        self.dialog.busy = False
        if self.dialog.winfo_exists():
            self.dialog.close()

    def test_actions_fit_minimum_dialog_width(self):
        self.root.deiconify()
        self.root.update()
        self.dialog.deiconify()
        self.dialog.geometry("650x520")
        self.dialog.update()
        for button in [*self.dialog.action_buttons, self.dialog.open_button]:
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), self.dialog.winfo_rootx() + self.dialog.winfo_width())

    def test_requires_stopped_confirmation_before_spawning_worker(self):
        with mock.patch("swotflow.project.transfer_dialog.messagebox.showwarning") as warning, mock.patch("swotflow.project.transfer_dialog.threading.Thread") as thread:
            self.dialog.start("validate")
            warning.assert_called_once()
            thread.assert_not_called()

    def test_validation_is_background_and_report_enables_open(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.dialog.path_var.set(str(folder / "project.yaml"))
            self.dialog.stopped_var.set(True)
            with mock.patch("swotflow.project.transfer_dialog.threading.Thread") as thread:
                self.dialog.start("validate")
                thread.return_value.start.assert_called_once()
            self.assertTrue(self.dialog.busy)
            report = ValidationReport(str(folder), dataset_counts={"download_manifest": 10})
            self.dialog.events.put(("report", (folder, report, folder / "report.json")))
            self.dialog.after_cancel(self.dialog.after_id)
            self.dialog.poll()
            self.assertFalse(self.dialog.busy)
            self.assertEqual(self.dialog.validated_root, folder)
            self.assertFalse(self.dialog.open_button.instate(["disabled"]))


if __name__ == "__main__":
    unittest.main()
