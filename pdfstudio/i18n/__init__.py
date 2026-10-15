"""Minimal i18n: all user facing strings go through :func:`tr`.

Strings live in per-language modules (``da.py``) as a flat dict keyed by a
dotted id. Missing keys fall back to the key itself so a forgotten string is
visible but never crashes. Add a language by creating ``<code>.py`` with a
``STRINGS`` dict and calling :func:`set_language`.
"""

from __future__ import annotations

import importlib

_strings: dict[str, str] = {}
_lang = ""


def set_language(code: str = "da") -> None:
    global _strings, _lang
    mod = importlib.import_module(f"{__name__}.{code}")
    _strings = dict(mod.STRINGS)
    _lang = code


def tr(key: str, **kwargs) -> str:
    if not _lang:
        set_language("da")
    text = _strings.get(key, key)
    return text.format(**kwargs) if kwargs else text


def tr_n(key: str, n: int, **kwargs) -> str:
    """Count-dependent text: ``key + ".one"`` when ``n == 1`` (if that key
    exists), else ``key``; ``n`` is passed to the format string."""
    if not _lang:
        set_language("da")
    one = f"{key}.one"
    return tr(one if n == 1 and one in _strings else key, n=n, **kwargs)


def register(strings: dict[str, str]) -> None:
    """Let a feature module contribute strings (useful for plugins)."""
    if not _lang:
        set_language("da")
    for k, v in strings.items():
        _strings.setdefault(k, v)
