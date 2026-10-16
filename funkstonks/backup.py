"""Backups: create, list, restore, import an external file."""

import os
import re
import sqlite3
from datetime import datetime

from . import config
from .config import BadInput


def _backups_dir():
    return os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")


BACKUP_TAGS = {"": "manuell", "regel": "vor „Regel anwenden“ (alt)", "vor-wiederherstellung": "vor Wiederherstellung"}


def list_backups():
    """Backups in the backups/ folder, newest first: (file name, time, size)."""
    bdir = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")
    if not os.path.isdir(bdir):
        return []
    out = []
    for f in sorted(os.listdir(bdir), reverse=True):
        m = BACKUP_NAME_RE.fullmatch(f)
        if not m:
            continue
        out.append(
            {
                "name": f,
                "when": f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}:{m.group(5)}",
                "tag": BACKUP_TAGS.get(m.group(7) or "", ""),
                "kb": os.path.getsize(os.path.join(bdir, f)) // 1024,
            }
        )
    return out


def restore_backup(name):
    """Restore a backup into the running database.

    Beforehand the current state is written out as a backup of its own (so
    the restore itself can be undone). Copying goes through the SQLite backup
    API into the existing file - no file swap, no restart needed. Only file
    names from backups/ are allowed.

    The chosen backup is first read completely into memory before the safety
    copy is created - so the source can never be touched in the process.
    """
    bdir = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")
    if not BACKUP_NAME_RE.fullmatch(name or ""):
        raise BadInput("Ungültiger Sicherungsname.")
    src_path = os.path.join(bdir, name)
    if not os.path.isfile(src_path):
        raise BadInput("Diese Sicherung gibt es nicht (mehr).")
    restore_from_file(src_path)
    return name


def restore_from_file(src_path):
    """Core of the restore: read the file completely, check it, back up the
    current state, copy into the running DB via the backup API. Used for
    backups from backups/ AND for uploaded .db files."""
    try:
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        mem = sqlite3.connect(":memory:")
        try:
            src.backup(mem)
        finally:
            src.close()
    except sqlite3.DatabaseError as e:
        raise BadInput("Die Datei ist keine SQLite-Datenbank.") from e
    if mem.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='transactions'").fetchone()[0] != 1:
        mem.close()
        raise BadInput("Die Datei ist keine funkSTONKS-Datenbank.")
    backup_db("vor-wiederherstellung")
    dst = sqlite3.connect(config.DB_PATH)
    try:
        mem.backup(dst)
    finally:
        dst.close()
        mem.close()


# moneyflow_20260913_111533.db, optionally with a tag (_vor-wiederherstellung)
# and a counter (-2) if the same second was already taken
BACKUP_NAME_RE = re.compile(r"moneyflow_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:_([a-z-]+))?(?:-\d+)?\.db")


def backup_db(tag=""):
    """Back up the DB to backups/ (next to config.DB_PATH, not APP_DIR -
    otherwise a test with a redirected config.DB_PATH writes into the real
    backups/; exactly that happened once with the monthly archive). Since
    2026-09-15 ONLY on user request (/sicherung) and as a safety copy before
    a restore - no longer on every start, no rotation: what the user creates
    stays until he deletes it. `tag` is part of the file name so the list can
    say where a backup came from."""
    if not os.path.exists(config.DB_PATH):
        return None
    bdir = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")
    os.makedirs(bdir, exist_ok=True)
    stem = "moneyflow_" + datetime.now().strftime("%Y%m%d_%H%M%S") + (("_" + tag) if tag else "")
    name = stem + ".db"
    n = 1
    while os.path.exists(os.path.join(bdir, name)):
        # two backups in the same second: never overwrite an existing one
        n += 1
        name = f"{stem}-{n}.db"
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(os.path.join(bdir, name))
    try:
        src.backup(dst)  # consistent even while the app is running (no raw file copy)
    finally:
        src.close()
        dst.close()
    return name
