"""Inspect browser-only EE transfer cards; expand failed details but never retry."""
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# Observed EE task-pane DOM. Each record is one local upload card, never the
# aggregate Tasks panel (whose text can mix successes and unrelated failures).
BROWSER_UPLOAD_ROWS_SCRIPT = r"""
const pane = document.querySelector('ee-task-pane');
if (!pane || !pane.shadowRoot) return null;
const expandFailures = arguments[0] === true;
return Array.from(pane.shadowRoot.querySelectorAll('.upload-task-list .task.type-UPLOAD')).map(row => {
  if (expandFailures && row.classList.contains('failed') && !row.querySelector('.assetId b')
      && !row.querySelector('.toggle-indicator.expanded')) {
    row.querySelector('.content.info-toggle')?.click();
  }
  return ({
  element: row,
  asset_name: ((row.querySelector('.content.info-toggle')?.textContent || '').trim().match(/^(?:\([\d.]+%\)\s*)?(.*?)\s+upload\s*$/s) || [,''])[1].trim(),
  asset_id: (row.querySelector('.assetId b')?.textContent || '').trim(),
  failed: row.classList.contains('failed'),
  settled: /^(completed|succeeded|cancelled|canceled)$/i.test((row.querySelector('.phase b')?.textContent ||
    (row.querySelector('.phase')?.textContent || '').replace(/^Phase:\s*/i, '')).trim()) ||
    ['completed', 'succeeded', 'cancelled', 'canceled'].some(c => row.classList.contains(c)),
  message: (row.querySelector('.info')?.textContent || row.textContent || '').trim().slice(0, 2000)
});
});
"""

@dataclass(frozen=True)
class BrowserUploadRow:
    """A single rendered transfer card, bound to its browser element identity."""
    element_id: str
    asset_id: str
    failed: bool
    message: str
    asset_name: str = ""
    settled: bool = False


def normalize_browser_upload_rows(records: Any) -> list[BrowserUploadRow] | None:
    """Return explicit upload cards; None means the panel could not be read."""
    if not isinstance(records, list):
        return None
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        element_id = str(getattr(record.get('element'), 'id', '') or '')
        asset_id = str(record.get('asset_id', '') or '').strip().lstrip('/')
        if not element_id:
            continue
        if not asset_id.startswith('projects/'):
            asset_id = ''  # Keep identity of collapsed cards for the pre-click baseline.
        rows.append(BrowserUploadRow(element_id, asset_id, record.get('failed') is True,
                                     str(record.get('message', '') or '')[:2000],
                                     str(record.get('asset_name', '') or '').strip(),
                                     record.get('settled') is True))
    return rows


def current_transfer_failures(
    rows: Iterable[BrowserUploadRow],
    pending: Iterable[str],
    baselines: Mapping[str, set[str] | None],
) -> dict[str, str]:
    """Select new failed cards for exact pending assets; ignore old/ambiguous attempts."""
    rows = list(rows)
    grouped: dict[str, list[BrowserUploadRow]] = {}
    for row in rows:
        grouped.setdefault(row.asset_id, []).append(row)
    failures = {}
    for asset_id in pending:
        baseline = baselines.get(asset_id)
        if baseline is None:
            continue
        # Collapsed active cards have no destination details yet. A new card
        # with the same exact name vetoes failure until its destination is known.
        if any(not row.asset_id and not row.failed and row.asset_name == asset_id.rsplit('/', 1)[-1]
               and row.element_id not in baseline for row in rows):
            continue
        current = [row for row in grouped.get(asset_id, []) if row.element_id not in baseline]
        if current and all(row.failed for row in current):
            failures[asset_id] = current[-1].message or 'Browser file upload failed before EE ingestion.'
    return failures
