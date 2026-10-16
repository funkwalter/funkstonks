#!/bin/bash
# Builds a Mac version that runs without a Python installation: dist/funkSTONKS.app
# (run on a Mac; Apple Silicon and Intel each need their own build). Hand over
# the dist/funkSTONKS.app folder as a ZIP; on first start right-click -> Open
# (unsigned app). The database is created next to the app.
set -e
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet flask pyinstaller
rm -rf dist/funkSTONKS dist/funkSTONKS.app
.venv/bin/python -m PyInstaller --noconfirm --clean --onedir --windowed --name funkSTONKS \
    --add-data "templates:templates" --add-data "static:static" app.py
echo "Fertig: dist/funkSTONKS.app"
