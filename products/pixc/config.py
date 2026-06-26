"""Configuration constants for the SWOT PIXC product family."""

from __future__ import annotations


PRODUCT_NAME = "SWOT L2 HR Pixel Cloud / PIXC"
DEFAULT_COLLECTION_SHORT_NAME = "SWOT_L2_HR_PIXC_D"
DEFAULT_COLLECTION_LABEL = "Version D active"

COLLECTION_LABELS = {
    "Version D active": "SWOT_L2_HR_PIXC_D",
    "Version C superseded": "SWOT_L2_HR_PIXC_2.0",
}
COLLECTION_LABEL_BY_SHORT_NAME = {
    value: label for label, value in COLLECTION_LABELS.items()
}

