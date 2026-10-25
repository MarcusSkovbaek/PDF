"""Design tokens – the single source of truth for colors, radii and sizes.

Widgets never hard-code colors: they ask for a *role* (``tokens.qcolor("accent")``)
or use the global stylesheet generated from these tokens (:mod:`.qss`).
Colors are written as ``#RRGGBB`` or ``#RRGGBBAA`` (CSS order).

Use :func:`current` to get the active :class:`Tokens` and :func:`qcolor` /
:func:`css` for convenient conversion. The active set is switched by
:class:`pdfstudio.ui.theme.manager.ThemeManager`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# --- typography / geometry (theme independent) ------------------------------
FONT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Inter", "sans-serif"]
FONT_SIZE = 13          # px, base
FONT_SIZE_SMALL = 12
FONT_SIZE_XS = 11
FONT_SIZE_TITLE = 15
FONT_SIZE_DISPLAY = 28

RADIUS_S = 6
RADIUS_M = 10
RADIUS_L = 14

SPACE = 8               # 8 px grid
HEADER_H = 48
MODEBAR_H = 48
STATUSBAR_H = 34
SIDEBAR_W = 264
INSPECTOR_W = 300
ANIM_MS = 150


@dataclass(frozen=True)
class Tokens:
    name: str
    # surfaces
    bg_app: str             # window chrome (header, status bar)
    surface: str            # panels, sidebars
    surface_raised: str     # cards, menus, popovers, dialogs
    surface_sunken: str     # inputs, wells
    canvas: str             # backdrop behind pages
    page: str               # paper colour placeholder before render
    # interaction overlays (usually translucent)
    hover: str
    pressed: str
    selected: str
    # lines
    border: str
    border_strong: str
    divider: str
    # text
    text: str
    text_muted: str
    text_subtle: str
    text_on_accent: str
    # accent
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    accent_text: str
    focus_ring: str
    # status
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    # misc
    shadow: str
    shadow_strong: str
    scrim: str
    tooltip_bg: str
    tooltip_text: str
    scrollbar: str
    scrollbar_hover: str
    text_selection: str
    search_hit: str
    search_hit_current: str
    extra: dict = field(default_factory=dict)

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


LIGHT = Tokens(
    name="light",
    bg_app="#F6F6F8",
    surface="#FCFCFD",
    surface_raised="#FFFFFF",
    surface_sunken="#FFFFFF",
    canvas="#ECECF0",
    page="#FFFFFF",
    hover="#1C1C3A0D",
    pressed="#1C1C3A17",
    selected="#5B5BD61C",
    border="#E3E3E8",
    border_strong="#D2D2DA",
    divider="#E8E8ED",
    text="#1B1B21",
    text_muted="#5E5E6B",
    text_subtle="#90909C",
    text_on_accent="#FFFFFF",
    accent="#5B5BD6",
    accent_hover="#5050CC",
    accent_pressed="#4646B8",
    accent_soft="#5B5BD61F",
    accent_text="#4C4CC4",
    focus_ring="#5B5BD659",
    success="#23946A",
    success_soft="#23946A1F",
    warning="#C27A12",
    warning_soft="#C27A1221",
    danger="#D23F4C",
    danger_soft="#D23F4C1C",
    shadow="#14143214",
    shadow_strong="#1414322E",
    scrim="#0E0E1A33",
    tooltip_bg="#1D1D24",
    tooltip_text="#F3F3F6",
    scrollbar="#1C1C3A33",
    scrollbar_hover="#1C1C3A5C",
    text_selection="#5B5BD64D",
    search_hit="#F5B7004D",
    search_hit_current="#5B5BD680",
)

DARK = Tokens(
    name="dark",
    bg_app="#131316",
    surface="#18181C",
    surface_raised="#1F1F24",
    surface_sunken="#141417",
    canvas="#0E0E11",
    page="#FFFFFF",
    hover="#FFFFFF0D",
    pressed="#FFFFFF17",
    selected="#7C7CF029",
    border="#26262D",
    border_strong="#34343D",
    divider="#232329",
    text="#ECECF1",
    text_muted="#A3A3B0",
    text_subtle="#6C6C79",
    text_on_accent="#FFFFFF",
    accent="#6464E0",
    accent_hover="#7171E8",
    accent_pressed="#5757CF",
    accent_soft="#7C7CF029",
    accent_text="#A5A4FA",
    focus_ring="#7C7CF066",
    success="#3DBE8B",
    success_soft="#3DBE8B26",
    warning="#E5A33B",
    warning_soft="#E5A33B26",
    danger="#F0606C",
    danger_soft="#F0606C26",
    shadow="#00000059",
    shadow_strong="#0000008C",
    scrim="#00000073",
    tooltip_bg="#2A2A31",
    tooltip_text="#F1F1F4",
    scrollbar="#FFFFFF2E",
    scrollbar_hover="#FFFFFF52",
    text_selection="#7C7CF059",
    search_hit="#F5B70059",
    search_hit_current="#7C7CF08C",
)

THEMES = {"light": LIGHT, "dark": DARK}

_current: Tokens = LIGHT


def current() -> Tokens:
    return _current


def set_current(name_or_tokens) -> Tokens:
    global _current
    _current = THEMES[name_or_tokens] if isinstance(name_or_tokens, str) else name_or_tokens
    return _current


def with_accent(tokens: Tokens, accent: str) -> Tokens:
    """Return a copy using another accent colour (for future personalisation)."""
    return replace(tokens, accent=accent)


#: Avatar circles of comment authors (picked by a stable hash of the name).
AVATARS = {
    "light": ("#5B5BD6", "#23946A", "#C2410C", "#B4237A", "#0E7490", "#7C3AED", "#A16207", "#BE123C"),
    "dark": ("#7C7CF0", "#3DBE8B", "#F07A3E", "#E25CA8", "#38B2CF", "#A57CF5", "#D7A533", "#F0607C"),
}


def avatar_color(name: str, tokens: Tokens | None = None):
    import zlib

    from PySide6.QtGui import QColor

    pal = AVATARS["dark" if (tokens or _current).is_dark else "light"]
    r, g, b, a = rgba(pal[zlib.crc32((name or "?").encode("utf-8")) % len(pal)])
    return QColor(r, g, b, a)


# --- conversion helpers -----------------------------------------------------
def rgba(value: str) -> tuple[int, int, int, int]:
    """Parse ``#RRGGBB`` / ``#RRGGBBAA`` into an (r, g, b, a) tuple 0..255."""
    v = value.lstrip("#")
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    a = int(v[6:8], 16) if len(v) >= 8 else 255
    return r, g, b, a


def value(role: str, tokens: Tokens | None = None) -> str:
    t = tokens or _current
    if hasattr(t, role):
        return getattr(t, role)
    return t.extra[role]


def css(role: str, tokens: Tokens | None = None, alpha: float | None = None) -> str:
    """Stylesheet colour for *role*, e.g. ``rgba(91, 91, 214, 255)``."""
    r, g, b, a = rgba(value(role, tokens))
    if alpha is not None:
        a = int(round(a * alpha))
    return f"rgba({r}, {g}, {b}, {a})"


def qcolor(role: str, tokens: Tokens | None = None, alpha: float | None = None):
    """QColor for *role* (imports Qt lazily so this module stays importable)."""
    from PySide6.QtGui import QColor

    r, g, b, a = rgba(value(role, tokens))
    if alpha is not None:
        a = int(round(a * alpha))
    return QColor(r, g, b, a)


def blend(role_a: str, role_b: str, t: float, tokens: Tokens | None = None) -> str:
    """Mix two roles (0 -> a, 1 -> b); returns ``#RRGGBBAA``."""
    a = rgba(value(role_a, tokens))
    b = rgba(value(role_b, tokens))
    m = [round(x + (y - x) * t) for x, y in zip(a, b)]
    return "#" + "".join(f"{c:02X}" for c in m)


def swatch_outline(fill, tokens: Tokens | None = None):
    """Outline colour for a colour dot/swatch showing ``fill`` (a QColor):
    the subtle border normally, a lighter ring where a dark fill would melt
    into a dark theme's surfaces (black ink, dark arrows)."""
    t = tokens or current()
    if t.is_dark and fill.lightnessF() < 0.4:
        return qcolor("text_subtle", t)
    return qcolor("border_strong", t)
