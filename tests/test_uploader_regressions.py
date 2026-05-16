import unittest

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from ee_ui_uploader import (
    RESUME_SKIP_STATUSES,
    TERMINAL_STATUSES,
    UNKNOWN_AFTER_CLICK_STATUS,
    active_upload_dialog_helper_js,
    is_invalid_browser_session,
    normalize_dialog_error_messages,
    EarthEngineUIUploader,
)


class UploaderDialogRegressionTests(unittest.TestCase):
    def test_active_upload_dialog_helper_filters_visible_dialogs(self) -> None:
        script = active_upload_dialog_helper_js()

        self.assertIn("findActiveUploadDialog", script)
        self.assertIn("getClientRects", script)
        self.assertIn("getBoundingClientRect", script)
        self.assertIn("uploadDialogLooksOpen", script)

    def test_current_ui_metadata_scripts_use_active_dialog_helper(self) -> None:
        click_script = EarthEngineUIUploader.current_ui_metadata_click_script()
        fill_script = EarthEngineUIUploader.current_ui_metadata_fill_script()

        self.assertIn("findActiveUploadDialog", click_script)
        self.assertIn("findActiveUploadDialog", fill_script)
        self.assertNotIn("dialogs[dialogs.length - 1]", click_script)
        self.assertNotIn("dialogs[dialogs.length - 1]", fill_script)

    def test_asset_tree_name_is_not_treated_as_dialog_error(self) -> None:
        message = normalize_dialog_error_messages(["testtiles-461713"])

        self.assertEqual(message, "")

    def test_explicit_dialog_error_is_preserved(self) -> None:
        message = normalize_dialog_error_messages(
            ["Asset already exists: projects/example/assets/testtiles-461713"]
        )

        self.assertIn("already exists", message)

    def test_resume_skips_submitted_but_not_error_or_unknown_after_click(self) -> None:
        self.assertIn("SUBMITTED", RESUME_SKIP_STATUSES)
        self.assertIn("READY", RESUME_SKIP_STATUSES)
        self.assertIn("RUNNING", RESUME_SKIP_STATUSES)
        self.assertIn("COMPLETED", RESUME_SKIP_STATUSES)
        self.assertIn("SKIPPED_ALREADY_EXISTS", RESUME_SKIP_STATUSES)
        self.assertNotIn("ERROR", RESUME_SKIP_STATUSES)
        self.assertNotIn(UNKNOWN_AFTER_CLICK_STATUS, RESUME_SKIP_STATUSES)
        self.assertIn(UNKNOWN_AFTER_CLICK_STATUS, TERMINAL_STATUSES)

    def test_invalid_session_is_fatal(self) -> None:
        self.assertTrue(is_invalid_browser_session(InvalidSessionIdException()))
        self.assertTrue(is_invalid_browser_session(WebDriverException("invalid session id")))
        self.assertFalse(is_invalid_browser_session(WebDriverException("stale element reference")))


if __name__ == "__main__":
    unittest.main()
