@echo off
REM Builds dist\FolioPDF\FolioPDF.exe on Windows.
REM Requirements: Python 3.11+ (python.org) on PATH. Internet is only needed
REM once, to install the build dependencies – the built program itself never
REM uses the network.
setlocal
cd /d "%~dp0\.."

if not exist .venv (
    python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt -r requirements-dev.txt || goto :error

python -m pytest || goto :error
pyinstaller packaging\folio.spec --noconfirm --clean || goto :error

echo.
echo Faerdig: dist\FolioPDF\FolioPDF.exe
echo (Valgfrit) Kør packaging\block_network_windows.ps1 som administrator for
echo en ekstra firewall-regel, der blokerer programmet fra nettet.
goto :eof

:error
echo Build fejlede.
exit /b 1
