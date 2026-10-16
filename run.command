#!/bin/bash
# Start funkSTONKS from source on the Mac: double-click in Finder (first
# time: right-click -> Open, because it is unsigned). Creates .venv, installs
# Flask, opens the browser at http://127.0.0.1:5010.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 fehlt. Bitte von https://www.python.org/downloads/macos/ installieren und nochmal starten."
  read -n 1 -s -r -p "Taste druecken ..."; echo; exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "Erster Start: richte die Umgebung ein ..."
  python3 -m venv .venv || { echo "Konnte .venv nicht anlegen."; read -n 1 -s -r -p "Taste druecken ..."; exit 1; }
fi
.venv/bin/python -c "import flask" 2>/dev/null || .venv/bin/python -m pip install --quiet flask
.venv/bin/python -c "import flask" 2>/dev/null || { echo "Flask konnte nicht installiert werden - Internet pruefen."; read -n 1 -s -r -p "Taste druecken ..."; exit 1; }
.venv/bin/python app.py
