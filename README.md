# funkSTONKS

Personal expense tracking for a single user. Flask + SQLite, runs locally in
the browser, keeps all data on your machine.

**What it does:** imports bank statements, credit card, PayPal, Splitwise and
brokerage exports; nets out collective debits, Splitwise shares and
repayments; categorises by rules and lets you sort the rest by hand. Shows
dashboards, period comparisons, investments with quotes, yearly overview,
recurring costs and shared expenses with a partner. The user interface is in
German.

## Download (Windows, no Python needed)

The [latest release](../../releases/latest) has a ZIP with a stand-alone
build. Unzip it anywhere and start `funkSTONKS.exe`; the browser opens and
the database is created next to the EXE. The file is not code-signed, so
Windows SmartScreen may warn on first start: choose "More info" and "Run
anyway".

## Run from source

- **Windows:** double-click `run.bat`. It installs Python if needed, creates
  `.venv`, puts a shortcut `funkSTONKS.lnk` next to it and opens
  `http://127.0.0.1:5010`.
- **macOS:** double-click `run.command`.
- **Build it yourself:** `build.bat` (Windows) or `build_mac.sh` (macOS)
  produces the stand-alone app in `dist/funkSTONKS`.

The database `moneyflow.db` is created next to the app on first start. The
only network access is the optional quote lookup for your own brokerage
positions.

## Tests

```
.venv\Scripts\python.exe tests\run_all.py
```

Every suite runs against a temporary database.
