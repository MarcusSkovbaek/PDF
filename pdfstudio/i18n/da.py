"""Danish UI strings.

Strings are split per feature area (``da_<area>.py``, each exposing a
``STRINGS`` dict) so features can be developed independently. Keys are
dotted ids grouped by area prefix (``shell.``, ``edit.``, ``pages.`` ...).
"""

import importlib
import pkgutil

from .. import i18n as _pkg

STRINGS: dict[str, str] = {
    "app.offline_badge": "100% lokalt · offline",
    "app.untitled": "Unavngivet.pdf",
}

for _m in sorted(pkgutil.iter_modules(_pkg.__path__), key=lambda m: m.name):
    if _m.name.startswith("da_"):
        STRINGS.update(importlib.import_module(f"{_pkg.__name__}.{_m.name}").STRINGS)
