"""Pluggable commands.

Every module in this package that defines ``register(window)`` is imported
and called once when the main window is built (after the built-in actions,
before the menus are assembled). Use it to add actions, e.g.::

    # pdfstudio/ui/commands/split.py
    from ...i18n import tr

    def register(window):
        window.actions.register(
            "pages.split", tr("pages.split"), icon="scissors",
            handler=lambda: ..., enabled=lambda: window.current_pdf() is not None,
            menu="tools", mode="pages", category=tr("shell.cat.tools"))

A failing module is reported on stderr and skipped.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback


def discover(window) -> list[str]:
    loaded = []
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
            fn = getattr(mod, "register", None)
            if callable(fn):
                fn(window)
                loaded.append(m.name)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[folio] command module {m.name!r} failed: {exc}", file=sys.stderr)
            traceback.print_exc()
    return loaded
