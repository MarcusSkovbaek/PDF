# PyInstaller spec for Folio PDF (one-folder build).
# Build:  pyinstaller packaging/folio.spec --noconfirm
# Result: dist/FolioPDF/FolioPDF.exe  (then packaging/installer.iss -> Setup .exe)
# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (SPECPATH injected by PyInstaller)
sys.path.insert(0, ROOT)
from pdfstudio import APP_NAME, __version__  # noqa: E402


def package_modules(package: str) -> list[str]:
    """Every module of ``package`` found on disk. Tools, dialogs and the i18n
    string tables are imported dynamically, and collect_submodules() silently
    skips modules it cannot import at build time – a missing string table
    would crash the app at start-up."""
    base = os.path.join(ROOT, package)
    mods = []
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(folder, ROOT).replace(os.sep, ".")
        for f in files:
            if f.endswith(".py"):
                mods.append(rel if f == "__init__.py" else f"{rel}.{f[:-3]}")
    return sorted(mods)


hiddenimports = package_modules("pdfstudio") + collect_submodules("qtawesome")
datas = collect_data_files("qtawesome") + collect_data_files("pymupdf")
icon_file = os.path.join(ROOT, "assets", "folio.ico")

# Not used by the app. Leaving them out keeps the bundle small and also keeps
# Qt's network library (a dependency of QML/Quick) out of the program.
excludes = [
    "PySide6.QtNetwork", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtHttpServer", "PySide6.QtRemoteObjects", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.Qt3DCore",
    "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.QtDataVisualization", "PySide6.QtCharts", "PySide6.QtGraphs",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "tkinter", "unittest", "pytest", "requests", "urllib3",
    "numpy", "pandas", "lxml", "matplotlib", "scipy", "IPython",
]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)


def _keep(dest: str) -> bool:
    d = dest.replace("\\", "/").lower()
    if "/translations/" in d and d.endswith(".qm"):
        name = d.rsplit("/", 1)[-1]
        return name.endswith(("_da.qm", "_en.qm"))  # Danish UI; English fallback
    return True


a.datas = [entry for entry in a.datas if _keep(entry[0])]

# Qt plugins (virtual keyboard, QML tooling, …) drag in Qt's QML/Quick and
# network libraries. Folio uses none of them – and the bundle must not even
# contain Qt's networking code.
_DROP_BINARIES = ("qt6network", "qt6qml", "qt6quick", "virtualkeyboard", "qt6webengine", "qt6pdf",
                  "qt6datavisualization", "qt6opengl", "qt6charts", "qt6graphs", "qt6sql", "qt6test",
                  "qt6waylandcompositor", "/qmltooling/", "/platforminputcontexts/", "/networkinformation/",
                  "/tls/", "/sqldrivers/", "/position/", "/multimedia/")


def _keep_binary(dest: str) -> bool:
    d = dest.replace("\\", "/").lower()
    return not any(tag in d for tag in _DROP_BINARIES)


a.binaries = [entry for entry in a.binaries if _keep_binary(entry[0])]
a.datas = [entry for entry in a.datas if _keep_binary(entry[0])]  # (Linux) symlinks to dropped libs

version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    nums = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("040604B0", [
                StringStruct("CompanyName", "Folio"),
                StringStruct("FileDescription", f"{APP_NAME} – lokal PDF-editor"),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", "FolioPDF"),
                StringStruct("OriginalFilename", "FolioPDF.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0406, 1200])]),
        ],
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
    version=version_info,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FolioPDF")  # noqa: F821
