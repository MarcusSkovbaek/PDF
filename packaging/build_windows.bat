@echo off
REM Builds the Folio PDF installer on Windows:
REM   dist\FolioPDF\FolioPDF.exe          (the program)
REM   dist\FolioPDF-Setup-<version>.exe   (the installer, needs Inno Setup 6)
REM
REM Requirements: Python 3.11+ (python.org, "Add python.exe to PATH") and
REM Inno Setup 6 (jrsoftware.org/isinfo.php). Internet is only needed once, to
REM install the build tools – the built program never uses the network.
setlocal
cd /d "%~dp0\.."

if not exist .venv (
    python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt -r requirements-dev.txt || goto :error

set QT_QPA_PLATFORM=offscreen
python -m pytest -q -p no:cacheprovider || goto :error
set QT_QPA_PLATFORM=

pyinstaller packaging\folio.spec --noconfirm --clean || goto :error

for /f "delims=" %%v in ('python -c "import pdfstudio; print(pdfstudio.__version__)"') do set APPVER=%%v

set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo.
    echo Programmet er bygget: dist\FolioPDF\FolioPDF.exe
    echo Installer Inno Setup 6 for ogsaa at lave installationsfilen.
    goto :eof
)
%ISCC% /DAppVersion=%APPVER% packaging\installer.iss || goto :error

echo.
echo Faerdig: dist\FolioPDF-Setup-%APPVER%.exe
goto :eof

:error
echo Build fejlede.
exit /b 1
