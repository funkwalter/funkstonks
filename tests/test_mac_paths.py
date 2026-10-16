"""Packaged build: where the database is created. Windows: next to the EXE. macOS:
next to the .app bundle (sys.executable lives INSIDE funkSTONKS.app/Contents/MacOS/,
nothing may be written there and the user would never find the file).
Also: build_mac.sh and run.command are valid shell scripts."""

import os
import subprocess

from funkstonks import config as cfg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402


def test_mac_paths():
    f = cfg.app_dir_for_executable
    check(
        "macOS: folder next to the .app bundle",
        f("/Users/anna/Programme/funkSTONKS.app/Contents/MacOS/funkSTONKS") == "/Users/anna/Programme",
    )
    check("macOS: bundle directly in the volume root", f("/funkSTONKS.app/Contents/MacOS/funkSTONKS") == "/")
    check(
        "macOS: folder name with .app but no bundle layout -> normal",
        f("/Users/anna/my.app/bin/funkSTONKS") == os.path.dirname(os.path.abspath("/Users/anna/my.app/bin/funkSTONKS")),
    )
    check(
        "Windows: next to the EXE",
        f(r"C:\Programme\funkSTONKS\funkSTONKS.exe")
        == os.path.dirname(os.path.abspath(r"C:\Programme\funkSTONKS\funkSTONKS.exe")),
    )
    check(
        "development: APP_DIR is the project folder",
        os.path.abspath(cfg.APP_DIR) == ROOT and os.path.exists(os.path.join(cfg.APP_DIR, "run.bat")),
    )
    bash = None
    for cand in (r"C:\Program Files\Git\bin\bash.exe", "/bin/bash", "/usr/bin/bash"):
        if os.path.exists(cand):
            bash = cand
            break
    if bash:
        for script in ("build_mac.sh", "run.command"):
            r = subprocess.run([bash, "-n", os.path.join(ROOT, script)], capture_output=True, text=True)
            check(f"{script}: bash -n without errors", r.returncode == 0, r.stderr[:200])
        for script in ("build_mac.sh", "run.command"):
            with open(os.path.join(ROOT, script), "rb") as fh:
                raw = fh.read()
            check(
                f"{script}: LF line endings and shebang (otherwise 'bad interpreter' on the Mac)",
                b"\r\n" not in raw and raw.startswith(b"#!/bin/bash"),
            )
    else:
        print("  (no bash found - scripts not checked)")
    finish()
