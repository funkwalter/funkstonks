@echo off
chcp 65001 >nul
cd /d "%~dp0"
title funkSTONKS

rem ============================================================
rem  Start funkSTONKS. First time: check for Python (and install it
rem  automatically on request), create the virtual environment,
rem  install Flask, create the "funkSTONKS" shortcut in the folder.
rem  Then: the browser opens at http://127.0.0.1:5010
rem ============================================================

call :find_python
if not defined PY (
    echo.
    echo Python ist auf diesem Rechner nicht installiert. funkSTONKS braucht es
    echo ^(kostenlos, ca. 100 MB, von python.org^).
    echo.
    choice /c JN /n /m "Jetzt automatisch installieren? [J/N] "
    if errorlevel 2 (
        echo.
        echo Dann bitte Python von https://www.python.org/downloads/ installieren
        echo ^("Add python.exe to PATH" anhaken^) und run.bat nochmal starten.
        pause
        exit /b 1
    )
    call :install_python
    call :find_python
    if not defined PY (
        echo.
        echo Python ist installiert, aber dieses Fenster kennt es noch nicht.
        echo Bitte das Fenster schliessen und run.bat nochmal starten.
        pause
        exit /b 1
    )
)

rem ---- Create the virtual environment on first start ----
set "FIRST="
if not exist ".venv\Scripts\python.exe" (
    echo Erster Start: richte die Umgebung ein ^(dauert eine Minute^) ...
    "%PY%" -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet flask
    set "FIRST=1"
)

rem ---- Repair a half-finished setup (e.g. installation aborted without internet) ----
".venv\Scripts\python.exe" -c "import flask" >nul 2>nul || ".venv\Scripts\python.exe" -m pip install --quiet flask
".venv\Scripts\python.exe" -c "import flask" >nul 2>nul || (
    echo Flask konnte nicht installiert werden - Internetverbindung pruefen und run.bat nochmal starten.
    pause
    exit /b 1
)
rem "abgeschlossen" only once Flask really imports (Sandbox test 2026-10-15: the
rem line used to appear although pip had just failed on a broken connection)
if defined FIRST echo Einrichtung abgeschlossen.

call :make_shortcut
if exist ".git" git config core.hooksPath .githooks >nul 2>nul

rem ---- Start (opens the browser automatically) ----
".venv\Scripts\python.exe" app.py
pause
exit /b

rem ------------------------------------------------------------ Subroutines

:find_python
rem Sets PY to a Python command (3.9 or newer), otherwise PY stays empty.
rem The Microsoft Store placeholder "python" exits with an error and falls through.
set "PY="
for %%C in (py python3 python) do (
    if not defined PY (
        %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=%%C"
    )
)
rem Freshly installed, but this window's PATH is still the old one: try the known locations
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY if exist "%WINDIR%\py.exe" set "PY=%WINDIR%\py.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
exit /b

:install_python
rem First via winget (bundled with Windows 10/11), otherwise download the installer from python.org.
echo.
echo Installiere Python ... ^(ein Windows-Fenster kann nach Erlaubnis fragen^)
rem --source winget: without it winget also queries the Microsoft Store, and if that is not
rem reachable the whole installation aborts before it has begun (observed 2026-10-15).
where winget >nul 2>nul
if not errorlevel 1 (
    winget install -e --id Python.Python.3.12 --source winget --scope user --accept-package-agreements --accept-source-agreements --silent
    if not errorlevel 1 exit /b
    echo winget hat nicht geklappt - versuche den Download von python.org ...
)
set "SETUP=%TEMP%\python-setup.exe"
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%SETUP%'"
if not exist "%SETUP%" (
    echo Download fehlgeschlagen - Internetverbindung pruefen.
    exit /b
)
"%SETUP%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%SETUP%" >nul 2>nul
exit /b

:make_shortcut
rem Shortcut "funkSTONKS" with icon in the folder - for starting by double-click.
rem Rewritten on every start so it stays correct after the folder has been moved.
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%~dp0funkSTONKS.lnk'); $s.TargetPath='%~dp0run.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0static\funkstonks.ico'; $s.Description='funkSTONKS starten'; $s.Save()" >nul 2>nul
exit /b
