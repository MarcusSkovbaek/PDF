"""Global stylesheet generated from design tokens.

Write rules with ``@role`` placeholders (any :class:`~.tokens.Tokens` field or
one of the derived roles below); :func:`build_qss` substitutes them with
``rgba()`` values for the active theme.

Variants are selected with dynamic properties so widgets stay declarative::

    btn.setProperty("variant", "primary")   # primary | secondary | ghost | danger | subtle
    tool.setProperty("variant", "icon")     # square icon button
    label.setProperty("role", "title")      # display | title | heading | muted | subtle | caption
    frame.setProperty("card", True)

Use :func:`pdfstudio.ui.widgets.util.set_prop` to change a property at
runtime (re-polishes the widget).
"""

from __future__ import annotations

import re

from . import tokens as T

QSS_TEMPLATE = r"""
/* ---------- base ---------- */
QWidget { color: @text; }
QMainWindow, QDialog, QStackedWidget#AppStack { background: @bg_app; }
QWidget#AppRoot { background: @bg_app; }
QAbstractScrollArea { background: transparent; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }

QToolTip {
    background: @tooltip_bg; color: @tooltip_text; border: 1px solid @tooltip_bg;
    padding: 5px 8px; border-radius: 6px; font-size: 12px;
}

/* ---------- labels ---------- */
QLabel { background: transparent; }
QLabel[role="display"] { font-size: 28px; font-weight: 600; color: @text; }
QLabel[role="title"] { font-size: 15px; font-weight: 600; color: @text; }
QLabel[role="heading"] { font-size: 13px; font-weight: 600; color: @text; }
QLabel[role="section"] { font-size: 11px; font-weight: 600; color: @text_subtle; }
QLabel[role="muted"] { color: @text_muted; }
QLabel[role="subtle"] { color: @text_subtle; }
QLabel[role="caption"] { color: @text_subtle; font-size: 12px; }
QLabel[role="danger"] { color: @danger; font-size: 12px; }
QLabel[role="kbd"] {
    color: @text_muted; background: @surface_sunken; border: 1px solid @border;
    border-bottom: 2px solid @border_strong;
    border-radius: 5px; padding: 0px 5px; font-size: 11px;
}

/* ---------- buttons ---------- */
QPushButton {
    background: @btn_bg; color: @text; border: 1px solid @border_strong;
    border-radius: 7px; padding: 0px 14px; min-height: 30px; font-weight: 500;
}
QPushButton:hover { background: @btn_hover; }
QPushButton:pressed { background: @btn_pressed; }
QPushButton:focus { border-color: @accent; }
QPushButton:disabled { color: @text_subtle; background: @btn_bg; border-color: @border; }
QPushButton:default { border-color: @border_strong; }
QPushButton[variant="primary"] {
    background: @accent; color: @text_on_accent; border: 1px solid @accent_pressed;
}
QPushButton[variant="primary"]:hover { background: @accent_hover; }
QPushButton[variant="primary"]:pressed { background: @accent_pressed; }
QPushButton[variant="primary"]:focus { border: 1px solid @text_on_accent_ring; }
QPushButton[variant="primary"]:disabled { background: @accent_disabled; color: @text_on_accent; border-color: @accent_disabled; }
QPushButton[variant="danger"] { background: @danger; color: @text_on_accent; border: 1px solid @danger; }
QPushButton[variant="danger"]:hover { background: @danger_hover; }
QPushButton[variant="ghost"] { background: transparent; border: 1px solid transparent; color: @text_muted; }
QPushButton[variant="ghost"]:hover { background: @hover; color: @text; }
QPushButton[variant="ghost"]:pressed { background: @pressed; }
QPushButton[variant="subtle"] { background: @hover; border: 1px solid transparent; color: @text; }
QPushButton[variant="subtle"]:hover { background: @pressed; }
QPushButton[variant="link"] { background: transparent; border: none; color: @accent_text; padding: 0px 2px; min-height: 0px; }
QPushButton[variant="link"]:hover { text-decoration: underline; }

QToolButton {
    background: transparent; border: 1px solid transparent; border-radius: 7px;
    padding: 4px; color: @text_muted;
}
QToolButton:hover { background: @hover; color: @text; }
QToolButton:pressed { background: @pressed; }
QToolButton:checked { background: @selected; color: @accent_text; }
QToolButton:disabled { color: @text_subtle; }
QToolButton[variant="icon"] { padding: 0px; min-width: 32px; min-height: 32px; max-width: 32px; max-height: 32px; }
QToolButton[variant="icon"]:checked { background: @hover; color: @text; }
QToolButton[variant="icon-sm"] { padding: 0px; min-width: 26px; min-height: 26px; max-width: 26px; max-height: 26px; border-radius: 6px; }
QToolButton[variant="tool"] { padding: 0px; min-width: 36px; min-height: 36px; max-width: 36px; max-height: 36px; border-radius: 9px; }
QToolButton[variant="tool"]:checked { background: @accent; color: @text_on_accent; }
QToolButton[variant="pill"] { padding: 0px 10px; min-height: 30px; border-radius: 8px; color: @text; }
QToolButton[variant="text"] { padding: 0px 10px; min-height: 30px; border-radius: 7px; color: @text_muted; font-weight: 500; }
QToolButton[variant="text"]:hover { color: @text; }
QToolButton::menu-indicator { image: none; width: 0px; }

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit {
    background: @surface_sunken; color: @text; border: 1px solid @border_strong;
    border-radius: 7px; selection-background-color: @text_selection; selection-color: @text;
}
QLineEdit { padding: 0px 10px; min-height: 30px; }
QPlainTextEdit, QTextEdit { padding: 6px 8px; }
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover { border-color: @input_hover; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus { border: 1px solid @accent; }
QLineEdit:disabled { color: @text_subtle; background: @bg_app; border-color: @border; }
QLineEdit[variant="bare"] { background: transparent; border: 1px solid transparent; padding: 0px 4px; }
QLineEdit[variant="bare"]:hover { background: @hover; }
QLineEdit[variant="bare"]:focus { background: @surface_sunken; border: 1px solid @accent; }
QLineEdit[variant="search"] { padding-left: 30px; }
QLineEdit[variant="palette"] {
    background: transparent; border: none; border-radius: 0px; font-size: 15px;
    padding: 0px 4px; min-height: 44px;
}
/* QComboBox / QSpinBox frames are painted by FolioStyle (theme/manager.py) */
QAbstractSpinBox QLineEdit, QComboBox QLineEdit {
    background: transparent; border: none; padding: 0px; min-height: 0px; border-radius: 0px;
}
QComboBox QAbstractItemView {
    background: @surface_raised; border: 1px solid @border; border-radius: 8px; padding: 4px;
    outline: 0; selection-background-color: @selected; selection-color: @text;
}

/* ---------- menus ---------- */
QMenuBar { background: transparent; border: none; }
QMenuBar::item { background: transparent; padding: 6px 10px; border-radius: 6px; color: @text_muted; }
QMenuBar::item:selected { background: @hover; color: @text; }
QMenu {
    background: @surface_raised; border: 1px solid @border_strong; border-radius: 10px;
    padding: 5px;
}
QMenu::item {
    background: transparent; color: @text; padding: 7px 24px 7px 10px; border-radius: 6px;
    margin: 0px;
}
QMenu::item:selected { background: @hover_solid_raised; color: @text; }
QMenu::item:disabled { color: @text_subtle; background: transparent; }
QMenu::icon { padding-left: 10px; }
QMenu::separator { height: 1px; background: @divider; margin: 5px 6px; }
QMenu::indicator { width: 14px; height: 14px; left: 6px; }

/* ---------- item views ---------- */
QListView, QTreeView, QTableView, QListWidget, QTreeWidget {
    background: transparent; border: none; outline: 0;
    selection-background-color: @selected; selection-color: @text;
}
QTreeView::item, QListView::item { padding: 5px 4px; border-radius: 6px; border: none; }
QTreeView::item:hover, QListView::item:hover { background: @hover; }
QTreeView::item:selected, QListView::item:selected { background: @selected; color: @text; }
QTreeView::branch { background: transparent; }
QHeaderView::section { background: transparent; border: none; color: @text_subtle; padding: 4px; }

/* ---------- scrollbars ---------- */
QScrollBar:vertical { background: transparent; width: 12px; margin: 0px; border: none; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0px; border: none; }
QScrollBar::handle:vertical { background: @scrollbar; min-height: 36px; border-radius: 3px; margin: 3px 3px 3px 3px; }
QScrollBar::handle:horizontal { background: @scrollbar; min-width: 36px; border-radius: 3px; margin: 3px 3px 3px 3px; }
QScrollBar::handle:hover { background: @scrollbar_hover; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }

/* ---------- misc controls ---------- */
QSplitter::handle { background: @divider; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QSlider::groove:horizontal { height: 4px; background: @border_strong; border-radius: 2px; }
QSlider::sub-page:horizontal { background: @accent; border-radius: 2px; }
QSlider::handle:horizontal {
    background: @slider_knob; border: 1px solid @border_strong; width: 14px; height: 14px;
    margin: -6px 0px; border-radius: 8px;
}
QSlider::handle:horizontal:hover { border-color: @accent; }
QProgressBar {
    background: @hover; border: none; border-radius: 3px; height: 6px; text-align: center;
    color: transparent; max-height: 6px;
}
QProgressBar::chunk { background: @accent; border-radius: 3px; }
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QGroupBox { border: 1px solid @border; border-radius: 10px; margin-top: 18px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 4px; color: @text_muted; font-weight: 600; }
QTabWidget::pane { border: none; }

/* ---------- app chrome ---------- */
QWidget#Header { background: @bg_app; }
QWidget#ModeBar { background: @bg_app; border-bottom: 1px solid @divider; }
QWidget#StatusBar { background: @bg_app; border-top: 1px solid @divider; }
QWidget#Sidebar, QWidget#Inspector { background: @surface; }
QWidget#Sidebar { border-right: 1px solid @divider; }
QWidget#Inspector { border-left: 1px solid @divider; }
QWidget#PanelHeader { background: transparent; }
QWidget#HomeView { background: @bg_app; }
QFrame[card="true"] { background: @surface_raised; border: 1px solid @border; border-radius: 14px; }
QFrame[divider="h"] { background: @divider; max-height: 1px; min-height: 1px; border: none; }
QFrame[divider="v"] { background: @divider; max-width: 1px; min-width: 1px; border: none; }

QTabBar#DocTabs { background: transparent; }
QTabBar#DocTabs::tab {
    background: transparent; color: @text_muted; border: 1px solid transparent;
    border-radius: 8px; padding: 0px 6px 0px 10px; margin: 0px 2px; height: 32px;
    min-width: 60px; max-width: 240px; font-weight: 500;
}
QTabBar#DocTabs::tab:hover:!selected { background: @hover; color: @text; }
QTabBar#DocTabs::tab:selected { background: @surface_raised; color: @text; border: 1px solid @border; }
QTabBar#DocTabs::scroller { width: 28px; }
QTabBar#DocTabs QToolButton { background: @bg_app; border-radius: 6px; }

QTabBar#SideTabs { background: transparent; }
QTabBar#SideTabs::tab {
    background: transparent; color: @text_muted; border: none; border-radius: 6px;
    padding: 0px 10px; margin: 0px 1px; height: 28px; font-weight: 500;
}
QTabBar#SideTabs::tab:hover:!selected { background: @hover; color: @text; }
QTabBar#SideTabs::tab:selected { background: @selected; color: @accent_text; }

QFrame#Badge {
    background: @success_soft; border: 1px solid @success_border; border-radius: 10px;
}
QLabel#BadgeText { color: @success; font-size: 12px; font-weight: 600; }

QFrame#FloatingBar, QFrame#Popover, QFrame#Palette, QFrame#Toast {
    background: @surface_raised; border: 1px solid @border_strong; border-radius: 14px;
}
QFrame#Toast { border-radius: 12px; }
QFrame#DropZone {
    background: @surface_raised; border: 2px dashed @border_strong; border-radius: 16px;
}
QFrame#DropZone[hot="true"] { background: @accent_soft; border: 2px dashed @accent; }
QFrame#ActionCard {
    background: @surface_raised; border: 1px solid @border; border-radius: 14px;
}
QFrame#ActionCard:hover { border: 1px solid @accent_border; background: @card_hover; }
QFrame#IconWell { background: @accent_soft; border: none; border-radius: 10px; }
QFrame#Card { background: @surface_raised; border: 1px solid @border; border-radius: 14px; }
QListWidget#ImageGrid { background: @surface_raised; border: 1px solid @border; border-radius: 14px; padding: 6px; }
QFrame#RecentRow { background: transparent; border-radius: 8px; border: 1px solid transparent; }
QFrame#RecentRow:hover { background: @hover; }
QFrame#EmptyState { background: transparent; }
"""

_PLACEHOLDER = re.compile(r"@([a-z_]+)")


def derived(t: T.Tokens) -> dict[str, str]:
    """Extra roles computed from the base tokens."""
    return {
        "btn_bg": T.value("surface_raised", t),
        "btn_hover": T.blend("surface_raised", "text", 0.045, t),
        "btn_pressed": T.blend("surface_raised", "text", 0.09, t),
        "input_hover": T.blend("border_strong", "text", 0.18, t),
        "hover_solid_raised": T.blend("surface_raised", "text", 0.07 if t.is_dark else 0.055, t),
        "accent_disabled": T.value("accent", t)[:7] + "73",
        "accent_border": T.value("accent", t)[:7] + "73",
        "card_hover": T.blend("surface_raised", "accent", 0.035, t),
        "danger_hover": T.blend("danger", "text", 0.12, t),
        "success_border": T.value("success", t)[:7] + "40",
        "text_on_accent_ring": T.value("text_on_accent", t)[:7] + "73",
        "slider_knob": "#DADAE2" if t.is_dark else "#FFFFFF",
    }


def build_qss(t: T.Tokens | None = None) -> str:
    t = t or T.current()
    extra = derived(t)

    def sub(m: re.Match) -> str:
        role = m.group(1)
        if role in extra:
            r, g, b, a = T.rgba(extra[role])
            return f"rgba({r}, {g}, {b}, {a})"
        return T.css(role, t)

    return _PLACEHOLDER.sub(sub, QSS_TEMPLATE)


def build_palette(t: T.Tokens | None = None):
    """QPalette matching the tokens (Fusion uses it for native-drawn bits)."""
    from PySide6.QtGui import QPalette

    t = t or T.current()
    q = lambda role: T.qcolor(role, t)  # noqa: E731
    pal = QPalette()
    ext = derived(t)
    from PySide6.QtGui import QColor

    def hexq(v: str) -> QColor:
        r, g, b, a = T.rgba(v)
        return QColor(r, g, b, a)

    R = QPalette.ColorRole
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        pal.setColor(group, R.Window, q("bg_app"))
        pal.setColor(group, R.WindowText, q("text"))
        pal.setColor(group, R.Base, q("surface_sunken"))
        pal.setColor(group, R.AlternateBase, q("surface"))
        pal.setColor(group, R.Text, q("text"))
        pal.setColor(group, R.Button, q("surface_raised"))
        pal.setColor(group, R.ButtonText, q("text"))
        pal.setColor(group, R.BrightText, q("text_on_accent"))
        pal.setColor(group, R.Highlight, q("accent"))
        pal.setColor(group, R.HighlightedText, q("text_on_accent"))
        pal.setColor(group, R.ToolTipBase, q("tooltip_bg"))
        pal.setColor(group, R.ToolTipText, q("tooltip_text"))
        pal.setColor(group, R.PlaceholderText, q("text_subtle"))
        pal.setColor(group, R.Link, q("accent_text"))
        pal.setColor(group, R.LinkVisited, q("accent_text"))
        pal.setColor(group, R.Light, hexq(ext["btn_bg"]))
        pal.setColor(group, R.Midlight, q("border"))
        pal.setColor(group, R.Mid, q("border_strong"))
        pal.setColor(group, R.Dark, q("border_strong"))
        pal.setColor(group, R.Shadow, q("shadow_strong"))
    for role in (R.WindowText, R.Text, R.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, q("text_subtle"))
    return pal
