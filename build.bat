@echo off
chcp 65001 >nul
cd /d "%~dp0"
title funkSTONKS – fertige Version bauen

rem Builds a version that runs without Python: dist\funkSTONKS\funkSTONKS.exe
rem plus folder. Hand over the whole "dist\funkSTONKS" folder (as a ZIP);
rem the recipient starts funkSTONKS.exe, the browser opens, the database is
rem created next to the EXE. None of the builder's data is included.

set "PY=py"
where py >nul 2>nul || set "PY=python"
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv || (echo Python fehlt - von python.org installieren. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet flask pyinstaller || (echo PyInstaller konnte nicht installiert werden. & pause & exit /b 1)
if exist "dist\funkSTONKS" rmdir /s /q "dist\funkSTONKS"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --noconsole --name funkSTONKS ^
    --add-data "templates;templates" --add-data "static;static" --icon "static\funkstonks.ico" app.py || (echo Build fehlgeschlagen. & pause & exit /b 1)
echo.
echo Fertig: dist\funkSTONKS\funkSTONKS.exe  (den Ordner dist\funkSTONKS als ZIP weitergeben)
pause
