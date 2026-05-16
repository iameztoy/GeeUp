"""Centralized Selenium selectors for the Earth Engine UI uploader.

If Google changes the Earth Engine interface, this is the first file to edit.
Each selector key contains multiple fallbacks, ordered from most specific to
most generic. The uploader tries them in sequence until one works.
"""

from __future__ import annotations

import importlib.util
import sysconfig
from typing import Dict, List, Tuple
from pathlib import Path

from selenium.webdriver.common.by import By

Locator = Tuple[str, str]


def _load_stdlib_selectors() -> None:
    """Expose the real stdlib selectors symbols to avoid module shadowing issues.

    This project intentionally uses the filename selectors.py because the user
    asked for it. Python also has a standard-library module with the same name.
    Selenium itself imports the stdlib version before this module is loaded, but
    other libraries may import it later. Re-exporting the stdlib symbols here
    keeps those later imports working.
    """

    stdlib_path = Path(sysconfig.get_paths()["stdlib"]) / "selectors.py"
    spec = importlib.util.spec_from_file_location("_stdlib_selectors", stdlib_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in dir(module):
        if name.startswith("__"):
            continue
        globals().setdefault(name, getattr(module, name))


_load_stdlib_selectors()


def _ci_contains(text: str, target: str = ".") -> str:
    """Return a case-insensitive XPath contains() expression."""
    return (
        f"contains(translate(normalize-space({target}), "
        "'abcdefghijklmnopqrstuvwxyz', "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), "
        f"'{text.upper()}')"
    )


def _button_text_xpath(text: str) -> str:
    """Return a broad XPath for buttons or button-like elements."""
    return (
        "//*[self::button or @role='button' or @role='menuitem' or "
        "self::span or self::div]"
        f"[{_ci_contains(text)}]"
    )


DEFAULT_CODE_EDITOR_URL = "https://code.earthengine.google.com/"


# Shadow-DOM selectors for the current Earth Engine Code Editor UI.
# These are kept separate from the generic fallback selectors below because
# the Code Editor currently exposes two different "NEW" controls:
# - `ee-new-script-menu` under the Scripts tab
# - `ee-new-asset-menu` under the Assets tab
# The uploader must explicitly target the Assets-side path for uploads.
SHADOW_SELECTORS: Dict[str, str | int] = {
    "left_tab_panel_index": 0,
    "right_tab_panel_index": 1,
    "tab_button": "button",
    "assets_tab_label": "Assets",
    "tasks_tab_label": "Tasks",
    "new_asset_menu_host": "ee-new-asset-menu",
    "new_asset_button": "ee-button",
    "new_asset_menu_component": "ee-menu-button",
    "new_asset_menu_item": "paper-item",
    "upload_dialog_host": "ee-upload-dialog",
    "upload_dialog_file_list_host": "file-list",
    "upload_dialog_file_input": "input[type=file]",
    "upload_dialog_root_dropdown_host": "ee-dropdown-menu",
    "upload_dialog_root_dropdown": "paper-dropdown-menu-light",
    "upload_dialog_asset_name_host": "#asset-id-trailer",
    "upload_dialog_asset_name_input": "input",
    "upload_dialog_pyramiding_host": ".pyramiding-policy",
    "upload_dialog_pyramiding_dropdown": "paper-dropdown-menu-light",
    "upload_dialog_properties_label": "Properties",
    "upload_dialog_add_start_time_label": "Add start time",
    "upload_dialog_add_end_time_label": "Add end time",
    "upload_dialog_add_property_label": "Add property",
    "upload_dialog_property_name_input": "input",
    "upload_dialog_property_value_input": "input",
    "upload_dialog_upload_button": ".ok-button",
    "upload_dialog_cancel_button": ".cancel-button",
}


SELECTORS: Dict[str, List[Locator]] = {
    "assets_tab": [
        (By.CSS_SELECTOR, "[aria-label*='Assets']"),
        (By.XPATH, "//*[@role='tab' and contains(., 'Assets')]"),
        (By.XPATH, _button_text_xpath("Assets")),
    ],
    "tasks_tab": [
        (By.CSS_SELECTOR, "[aria-label*='Tasks']"),
        (By.XPATH, "//*[@role='tab' and contains(., 'Tasks')]"),
        (By.XPATH, _button_text_xpath("Tasks")),
    ],
    "new_button": [
        (By.CSS_SELECTOR, "[aria-label='NEW']"),
        (By.XPATH, _button_text_xpath("NEW")),
        (By.XPATH, _button_text_xpath("Image")),
    ],
    "image_upload_button": [
        (By.XPATH, _button_text_xpath("Image upload")),
        (By.XPATH, _button_text_xpath("Upload image")),
        (By.XPATH, _button_text_xpath("Image Upload")),
    ],
    "file_input": [
        (By.CSS_SELECTOR, "input[type='file']"),
        (By.CSS_SELECTOR, "input[accept*='.tif']"),
        (By.CSS_SELECTOR, "input[accept*='tif']"),
    ],
    "asset_id_field": [
        (By.CSS_SELECTOR, "input[aria-label*='Asset']"),
        (By.CSS_SELECTOR, "input[placeholder*='Asset']"),
        (By.XPATH, "//label[contains(., 'Asset')]/following::input[1]"),
        (By.XPATH, "//input[contains(@name, 'asset')]"),
    ],
    "destination_collection_field": [
        (By.CSS_SELECTOR, "input[aria-label*='Collection']"),
        (By.CSS_SELECTOR, "input[placeholder*='Collection']"),
        (By.CSS_SELECTOR, "input[aria-label*='Destination']"),
        (By.CSS_SELECTOR, "input[placeholder*='Destination']"),
        (By.XPATH, "//label[contains(., 'Collection')]/following::input[1]"),
        (By.XPATH, "//label[contains(., 'Destination')]/following::input[1]"),
    ],
    "asset_name_field": [
        (By.CSS_SELECTOR, "input[aria-label='Name']"),
        (By.CSS_SELECTOR, "input[placeholder='Name']"),
        (By.CSS_SELECTOR, "input[aria-label*='Image name']"),
        (By.CSS_SELECTOR, "input[placeholder*='Image name']"),
        (By.XPATH, "//label[contains(., 'Name')]/following::input[1]"),
    ],
    "pyramiding_policy_expand_button": [
        (By.XPATH, _button_text_xpath("Pyramiding")),
        (By.XPATH, _button_text_xpath("Advanced")),
        (By.XPATH, _button_text_xpath("More options")),
    ],
    "pyramiding_policy_global_select": [
        (By.CSS_SELECTOR, "select[aria-label*='Pyramiding']"),
        (By.CSS_SELECTOR, "[role='combobox'][aria-label*='Pyramiding']"),
        (
            By.XPATH,
            "//*[contains(., 'Pyramiding')]/following::select[1]",
        ),
    ],
    "properties_expand_button": [
        (By.XPATH, _button_text_xpath("Properties")),
    ],
    "add_start_time_button": [
        (By.XPATH, _button_text_xpath("Add start time")),
    ],
    "add_end_time_button": [
        (By.XPATH, _button_text_xpath("Add end time")),
    ],
    "add_property_button": [
        (By.XPATH, _button_text_xpath("Add property")),
    ],
    "property_name_field": [
        (By.CSS_SELECTOR, "input[aria-label*='Property']"),
        (By.CSS_SELECTOR, "input[placeholder*='Property']"),
        (By.XPATH, "//label[contains(., 'Property')]/following::input[1]"),
    ],
    "property_value_field": [
        (By.CSS_SELECTOR, "input[aria-label*='Value']"),
        (By.CSS_SELECTOR, "input[placeholder*='Value']"),
        (By.XPATH, "//label[contains(., 'Value')]/following::input[1]"),
    ],
    "upload_button": [
        (By.CSS_SELECTOR, "[aria-label='UPLOAD']"),
        (By.XPATH, _button_text_xpath("UPLOAD")),
        (By.XPATH, _button_text_xpath("Upload")),
    ],
    "dialog_close_button": [
        (By.CSS_SELECTOR, "[aria-label='Close']"),
        (By.CSS_SELECTOR, "[aria-label='Cancel']"),
        (By.XPATH, _button_text_xpath("Close")),
        (By.XPATH, _button_text_xpath("Cancel")),
    ],
    "dialog_error_message": [
        (By.CSS_SELECTOR, "[role='alert']"),
        (By.CSS_SELECTOR, ".error"),
        (By.XPATH, "//*[contains(@class, 'error') or contains(., 'already exists')]"),
    ],
    "login_prompt": [
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.XPATH, _button_text_xpath("Sign in")),
        (By.XPATH, _button_text_xpath("Next")),
    ],
    "task_rows": [
        (By.CSS_SELECTOR, "[role='row']"),
        (By.CSS_SELECTOR, "[data-task-id]"),
        (By.XPATH, "//*[contains(., 'Asset ingestion') or contains(., 'Ingestion')]"),
    ],
}


ACTIVE_TASK_KEYWORDS = (
    "ready",
    "running",
    "submitted",
    "pending",
    "ingest",
    "ingestion",
    "uploading",
)

SUCCESS_TASK_KEYWORDS = (
    "completed",
    "complete",
    "succeeded",
    "success",
    "done",
)

FAILURE_TASK_KEYWORDS = (
    "failed",
    "error",
    "cancelled",
    "canceled",
    "already exists",
)
