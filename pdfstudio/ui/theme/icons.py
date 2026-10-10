"""Theme-aware icons (Phosphor via qtawesome), cached per theme.

    from pdfstudio.ui.theme.icons import icon
    btn.setIcon(icon("folder-open"))                      # muted text colour
    btn.setIcon(icon("check", "accent"))                  # any token role
    btn.setIcon(icon("cursor", on_role="text_on_accent")) # checked state colour

Names are Phosphor names with or without the ``ph.`` prefix (``ph.file-pdf``);
other qtawesome prefixes (``mdi6.``) also work. The cache is cleared on theme
change – widgets that keep icons should re-request them on
``theme_manager().changed`` (helpers in :mod:`pdfstudio.ui.widgets.util`).
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap

from . import tokens as T

_cache: dict[tuple, QIcon] = {}
_FALLBACK = "ph.square"


def _qta():
    import qtawesome as qta

    return qta


def full_name(name: str) -> str:
    return name if "." in name else f"ph.{name}"


def icon(
    name: str,
    color_role: str = "text_muted",
    *,
    active_role: str | None = None,
    on_role: str | None = None,
    disabled_role: str = "text_subtle",
    selected_role: str | None = None,
) -> QIcon:
    if not name:
        return QIcon()
    t = T.current()
    key = (full_name(name), color_role, active_role, on_role, disabled_role, selected_role, t.name, id(t))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    opts = {
        "color": T.qcolor(color_role, t),
        "color_active": T.qcolor(active_role or ("text" if color_role in ("text_muted", "text_subtle") else color_role), t),
        "color_disabled": T.qcolor(disabled_role, t),
    }
    if selected_role:
        opts["color_selected"] = T.qcolor(selected_role, t)
    if on_role:
        opts["color_on"] = T.qcolor(on_role, t)
        opts["color_on_active"] = T.qcolor(on_role, t)
        opts["color_on_selected"] = T.qcolor(on_role, t)
        opts["color_off"] = opts["color"]
        opts["color_off_active"] = opts["color_active"]
    qta = _qta()
    try:
        ic = qta.icon(full_name(name), **opts)
    except Exception:
        ic = qta.icon(_FALLBACK, **opts)
    _cache[key] = ic
    return ic


def pixmap(name: str, size: int, color_role: str = "text_muted", dpr: float = 1.0) -> QPixmap:
    ic = icon(name, color_role)
    pm = ic.pixmap(QSize(size, size), dpr)
    return pm


def clear_cache() -> None:
    _cache.clear()
