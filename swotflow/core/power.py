"""Small cross-platform helpers for keeping long local workflows awake."""

from __future__ import annotations

import ctypes
import platform
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

try:
    import winreg
except ImportError:  # pragma: no cover - exercised only on non-Windows platforms.
    winreg = None  # type: ignore[assignment]


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


@contextmanager
def keep_system_awake(
    *,
    enabled: bool = True,
    keep_display_awake: bool = False,
) -> Iterator[None]:
    """Ask the operating system not to sleep while a critical workflow runs.

    On Windows this prevents automatic system sleep for the current process
    thread until the context exits. It cannot prevent manual shutdown, forced
    restart, power loss, or all vendor-specific network power-saving behavior.
    Non-Windows platforms currently use a no-op context so the workflow remains
    portable without adding platform-specific dependencies.
    """
    if not enabled or platform.system().lower() != "windows":
        yield
        return

    kernel32 = ctypes.windll.kernel32
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if keep_display_awake:
        flags |= ES_DISPLAY_REQUIRED

    try:
        kernel32.SetThreadExecutionState(flags)
        yield
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def windows_automation_reboot_warnings(now: datetime | None = None) -> list[str]:
    """Return Windows reboot/update warnings relevant to unattended automation.

    The checks are intentionally read-only. They help the user decide whether to
    restart, pause updates, or adjust active hours before an overnight run.
    """
    if platform.system().lower() != "windows":
        return []
    if winreg is None:
        return []

    warnings: list[str] = []
    if registry_key_exists(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    ):
        warnings.append(
            "Windows Update reports a pending reboot. Restart or pause updates before starting an unattended run."
        )
    if registry_key_exists(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
    ):
        warnings.append(
            "Windows component servicing reports a pending reboot. Restart before starting an unattended run."
        )

    active_hours = windows_update_active_hours()
    if active_hours is not None:
        start_hour, end_hour = active_hours
        current_hour = (now or datetime.now()).hour
        if not hour_inside_active_hours(current_hour, start_hour, end_hour):
            warnings.append(
                (
                    f"Current time is outside Windows Update active hours "
                    f"({start_hour:02d}:00-{end_hour:02d}:00). Windows may restart after updates."
                )
            )
        elif active_hours_leave_overnight_unprotected(start_hour, end_hour):
            warnings.append(
                (
                    f"Windows Update active hours are {start_hour:02d}:00-{end_hour:02d}:00. "
                    "Overnight automation outside that window may be restarted after updates."
                )
            )
    return warnings


def registry_key_exists(root: int, path: str) -> bool:
    """Return True when a registry key exists and is readable."""
    try:
        with winreg.OpenKey(root, path):
            return True
    except OSError:
        return False


def registry_dword(root: int, path: str, name: str) -> int | None:
    """Read a registry DWORD-like value, returning None if unavailable."""
    try:
        with winreg.OpenKey(root, path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def windows_update_active_hours() -> tuple[int, int] | None:
    """Read configured Windows Update active hours as start/end hours."""
    path = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    start = registry_dword(winreg.HKEY_LOCAL_MACHINE, path, "ActiveHoursStart")
    end = registry_dword(winreg.HKEY_LOCAL_MACHINE, path, "ActiveHoursEnd")
    if start is None or end is None:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    return start, end


def hour_inside_active_hours(hour: int, start_hour: int, end_hour: int) -> bool:
    """Return True when an hour is inside Windows Update active hours."""
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def active_hours_leave_overnight_unprotected(start_hour: int, end_hour: int) -> bool:
    """Return True when active hours do not cover a typical overnight run."""
    return not all(hour_inside_active_hours(hour, start_hour, end_hour) for hour in (0, 1, 2, 3, 4, 5))
