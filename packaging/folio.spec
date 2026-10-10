# PyInstaller spec for Folio PDF (Windows, one-folder build).
# Build:  pyinstaller packaging/folio.spec --noconfirm
# Result: dist/FolioPDF/FolioPDF.exe
# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (SPECPATH injected by PyInstaller)

hiddenimports = (
    collect_submodules("pdfstudio")      # tools/dialogs/i18n tables are imported dynamically
    + collect_submodules("qtawesome")
)
datas = collect_data_files("qtawesome") + collect_data_files("pymupdf")
icon_file = os.path.join(ROOT, "assets", "folio.ico")

# Everything that could talk to the network is excluded from the bundle.
excludes = [
    "PySide6.QtNetwork", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtHttpServer", "PySide6.QtRemoteObjects", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml",
    "tkinter", "unittest", "pytest", "requests", "urllib3",
]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FolioPDF",
    console=False,
    icon=icon_file if os.path.exists(icon_file) else None,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FolioPDF")  # noqa: F821
