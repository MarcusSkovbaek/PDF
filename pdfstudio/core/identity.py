"""Who is commenting, and when: the author name and PDF date strings.

The author of new comments is always the Windows user's display name (read
locally from Windows; falls back to the login name), as decided for
Folio 1.1. Nothing leaves the machine – the name is only written into the
annotations the user creates. :func:`set_author` overrides it (tests, and a
future preference).
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_override: str | None = None
_cached: str | None = None


def _windows_display_name() -> str:
    """``GetUserNameExW(NameDisplay)`` – the full name shown in Windows'
    start menu. Empty when there is none (local accounts often have none)."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        size = ctypes.c_ulong(0)
        secur32 = ctypes.WinDLL("secur32")
        secur32.GetUserNameExW(3, None, ctypes.byref(size))  # NameDisplay = 3
        if not size.value:
            return ""
        buf = ctypes.create_unicode_buffer(size.value)
        if secur32.GetUserNameExW(3, buf, ctypes.byref(size)):
            return buf.value.strip()
    except Exception:
        pass
    return ""


def current_author() -> str:
    """Name written as ``/T`` of new annotations."""
    global _cached
    if _override is not None:
        return _override
    if _cached is None:
        name = _windows_display_name()
        if not name:
            name = os.environ.get("USERNAME") or ""
        if not name:
            try:
                name = getpass.getuser()
            except Exception:
                name = ""
        _cached = name.strip()
    return _cached


def set_author(name: str | None) -> None:
    """Override the author (``None`` restores the Windows name)."""
    global _override
    _override = None if name is None else str(name)


# -- PDF dates ("D:YYYYMMDDHHmmSS+HH'mm'") ------------------------------------------
def pdf_date(when: datetime | None = None) -> str:
    dt = (when or datetime.now()).astimezone()
    off = dt.utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return dt.strftime("D:%Y%m%d%H%M%S") + f"{sign}{minutes // 60:02d}'{minutes % 60:02d}'"


_DATE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*(Z|[+-]\d{2}'?(?:\d{2}'?)?)?")


def parse_pdf_date(value: str | None) -> datetime | None:
    """Timezone-aware datetime of a PDF date string, or ``None``. Missing
    parts default as the PDF spec says; no offset means local time."""
    if not value:
        return None
    m = _DATE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    if tz is None:
        return dt.astimezone()
    if tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    digits = tz[1:].replace("'", "")
    hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
    off = timedelta(hours=hours, minutes=minutes)
    return dt.replace(tzinfo=timezone(off if tz[0] == "+" else -off))


__all__ = ["current_author", "parse_pdf_date", "pdf_date", "set_author"]
