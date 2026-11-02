"""Keep every device on the same state (user 2026-11-02: the data go to the
private repo at every start, the code comes from GitHub when a newer one
exists, "so dass ich von jedem geraet immer den aktuellen stand habe" - so
that I always have the current state from any device).

Three kinds of installation, told apart by checkout_mode():

  dev     the developer checkout (private repo): at every start newer
          commits are fetched and merged (data AND code), the database is
          committed and pushed; if code arrived, the app restarts itself.
          The pre-push hook skips the test suite and the publication for
          data-only pushes, so a start takes seconds.
  zip     a ZIP download of the public repo ("Code -> Download ZIP", then
          run.bat; no .git, but UPDATE_INFO from the snapshot): at every
          start the `source` hash in UPDATE_INFO is compared with the one on
          GitHub (raw file); the update downloads the repo ZIP and copies
          it over the folder (database, backups, uploads, .venv stay).
  clone   a git clone of the public repo: at every start the remote tree
          is compared with the local one; a newer one is offered in the app
          (banner + Einstellungen -> Updates) and applied with
          `git reset --hard origin/main` plus restart. The database and
          everything gitignored stay where they are.
  frozen  the stand-alone build: the latest GitHub release is compared
          with APP_VERSION (UPDATE_INFO next to the EXE names the repo, written
          by build_release()); the update downloads the ZIP and swaps the
          files with a helper script after the app has quit (Windows).

Besides quotes.py this is the only module that goes online (user decision
2026-11-02); the setting startup_sync switches the start-up check off.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime

from . import config
from .config import APP_DIR, APP_VERSION
from .db import get_setting
from .share import PUBLIC_BRANCH, UPDATE_INFO, _git, _git_error, _git_out, _read_settings, is_dev_checkout

DATA_FILES = ("moneyflow.db",)
DATA_SUBJECT = "Data"  # subject of the automatic data commits ("Data 2026-11-02 08:15")
RELEASES_URL = "https://api.github.com/repos/{slug}/releases/latest"
RAW_URL = "https://raw.githubusercontent.com/{slug}/{branch}/{name}"
ARCHIVE_URL = "https://github.com/{slug}/archive/refs/heads/{branch}.zip"
# never overwritten by a zip update: the user's data and local environment
KEEP_DIRS = ("backups", "uploads", ".venv", "__pycache__", ".git", "update_tmp")
TIMEOUT = 8
RESTART_ENV = "FUNKSTONKS_RESTARTED"  # set in the restarted process: never restart twice in a row

# What the pages show (inject_globals): mode, last result, offered update.
STATE = {"mode": "none", "text": "", "update": None, "when": ""}


def checkout_mode(root=None):
    """'frozen' (built EXE/app), 'dev' (developer checkout), 'clone' (git
    checkout of the public repo), 'zip' (ZIP download of the public repo,
    UPDATE_INFO present) or 'none' (plain copy of the source)."""
    root = root or APP_DIR
    if getattr(sys, "frozen", False):
        return "frozen"
    if is_dev_checkout(root):
        return "dev"
    if os.path.isdir(os.path.join(root, ".git")):
        return "clone"
    if os.path.isfile(os.path.join(root, UPDATE_INFO)):
        return "zip"
    return "none"


def _set(mode, ok, text, update=None, restart=False):
    STATE.update(mode=mode, text=text, update=update, when=datetime.now().strftime("%Y-%m-%d %H:%M"))
    return {"ok": ok, "text": text, "update": update, "restart": restart}


# ------------------------------------------------------------------ dev checkout


def _ahead_behind(root, branch):
    """(local sha, remote sha, merge base) - remote/base None without an upstream."""
    local = _git_out(root, "rev-parse", "HEAD")
    remote = _git_out(root, "rev-parse", f"origin/{branch}")
    base = _git_out(root, "merge-base", "HEAD", f"origin/{branch}") if remote else None
    return local, remote, base


def sync_dev(root=None):
    """Developer checkout: fetch, commit the data, merge what is newer on
    GitHub, push. Returns {"ok", "text", "restart"}; restart is True when
    code (anything but DATA_FILES) arrived, so the caller starts the app
    again with the new files. A database changed on BOTH devices is NOT
    merged automatically (binary, real money): the local state stays, the
    text starts with WARNUNG and nothing is pushed until it is resolved by
    hand."""
    root = root or APP_DIR
    branch = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD") or "master"
    lines = []
    r = _git(root, "fetch", "-q", "origin")
    if r is None or r.returncode != 0:
        return _set("dev", False, "WARNUNG kein Abgleich mit GitHub (offline?): " + _git_error(r))
    if _git_out(root, "status", "--porcelain", "--", *DATA_FILES):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if _git_out(root, "add", "--", *DATA_FILES) is None:
            return _set("dev", False, "FEHLER git add der Daten fehlgeschlagen.")
        r = _git(root, "commit", "-q", "-m", f"{DATA_SUBJECT} {stamp}", "--", *DATA_FILES)
        if r is None or r.returncode != 0:
            return _set("dev", False, "FEHLER Daten nicht committet: " + _git_error(r))
        lines.append("Daten gesichert")
    local, remote, base = _ahead_behind(root, branch)
    restart = False
    if remote and remote != local:
        arriving = (_git_out(root, "diff", "--name-only", local, remote) or "").splitlines()
        if base == local:
            if _git_out(root, "merge", "-q", "--ff-only", f"origin/{branch}") is None:
                return _set("dev", False, "FEHLER Stand von GitHub konnte nicht übernommen werden.")
            lines.append(f"{len(arriving)} Datei(en) von GitHub geholt")
        elif base != remote:
            r = _git(root, "rebase", "-q", "--autostash", f"origin/{branch}")
            if r is None or r.returncode != 0:
                _git(root, "rebase", "--abort")
                return _set(
                    "dev",
                    False,
                    "WARNUNG Daten auf beiden Geräten geändert – lokaler Stand bleibt, nichts gepusht. "
                    "Sicherung anlegen und die Datenbank von Hand zusammenführen (git rebase origin/" + branch + ").",
                )
            lines.append(f"{len(arriving)} Datei(en) von GitHub geholt und zusammengeführt")
        restart = any(f not in DATA_FILES for f in arriving)
    local, remote, _ = _ahead_behind(root, branch)
    if remote != local:
        r = _git(root, "push", "-q", "-u", "origin", branch)
        if r is None or r.returncode != 0:
            return _set("dev", False, "WARNUNG Push nach GitHub fehlgeschlagen: " + _git_error(r), restart=restart)
        lines.append("auf GitHub gesichert")
    return _set("dev", True, ", ".join(lines) or "aktuell", restart=restart)


# ------------------------------------------------------------------ clone of the public repo


def _public_ref(root):
    """The remote branch of the public repo ('origin/main', or what origin/HEAD points to)."""
    if _git_out(root, "rev-parse", "--verify", "-q", f"origin/{PUBLIC_BRANCH}"):
        return f"origin/{PUBLIC_BRANCH}"
    head = _git_out(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    return head or f"origin/{PUBLIC_BRANCH}"


def check_clone(root=None):
    """Compare the local tree with the public repo's (its history is rewritten
    at every publication, so commits say nothing - trees do)."""
    root = root or APP_DIR
    r = _git(root, "fetch", "-q", "origin")
    if r is None or r.returncode != 0:
        return _set("clone", False, "WARNUNG keine Verbindung zu GitHub (offline?): " + _git_error(r))
    ref = _public_ref(root)
    local = _git_out(root, "rev-parse", "HEAD^{tree}")
    remote = _git_out(root, "rev-parse", f"{ref}^{{tree}}")
    if not remote:
        return _set("clone", False, "WARNUNG kein Stand auf GitHub gefunden.")
    if local == remote:
        return _set("clone", True, "aktuell")
    n = len((_git_out(root, "diff", "--name-only", "HEAD", ref) or "").splitlines())
    update = {"kind": "clone", "label": f"neuer Stand auf GitHub ({n} Datei{'en' if n != 1 else ''})", "ref": ref}
    return _set("clone", True, update["label"], update=update)


def apply_clone(root=None):
    """Take over the public repo's state; local changes to tracked files are
    discarded (a clone has none), gitignored files stay. Restart afterwards."""
    root = root or APP_DIR
    res = check_clone(root)
    if not res["update"]:
        return res
    if _git_out(root, "reset", "-q", "--hard", res["update"]["ref"]) is None:
        return _set("clone", False, "FEHLER Aktualisierung fehlgeschlagen (git reset).")
    return _set("clone", True, "aktualisiert – Neustart", restart=True)


# ------------------------------------------------------------------ stand-alone build


def http_get(url):
    """One GET (GitHub API or release asset); swapped out in the tests."""
    req = urllib.request.Request(url, headers={"User-Agent": "funkSTONKS/" + APP_VERSION, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _version_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def update_info(root=None):
    """UPDATE_INFO as dict ({} if missing or unreadable)."""
    try:
        with open(os.path.join(root or APP_DIR, UPDATE_INFO), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_repo(root=None):
    """'owner/repo' from UPDATE_INFO, '' if missing."""
    return str(update_info(root).get("repo", "")).strip()


# ------------------------------------------------------------------ ZIP download of the public repo


def check_zip(root=None, get=None):
    """Compare the `source` hash of the local UPDATE_INFO with the one in the
    public repo (raw file on GitHub) - the hash of the published code tree."""
    root = root or APP_DIR
    get = get or http_get
    info = update_info(root)
    slug = str(info.get("repo", "")).strip()
    if not slug:
        return _set("zip", False, "keine Update-Quelle hinterlegt (update.json fehlt oder ohne Repo).")
    try:
        remote = json.loads(
            get(RAW_URL.format(slug=slug, branch=PUBLIC_BRANCH, name=UPDATE_INFO)).decode("utf-8", "replace")
        )
    except Exception as e:
        return _set("zip", False, "WARNUNG keine Verbindung zu GitHub (offline?): " + str(e)[:120])
    if str(remote.get("source", "")) == str(info.get("source", "")):
        return _set("zip", True, f"aktuell (Version {APP_VERSION})")
    version = str(remote.get("version", "")).strip()
    update = {
        "kind": "zip",
        "label": "neuer Stand auf GitHub" + (f" (Version {version})" if version else ""),
        "url": f"https://github.com/{slug}",
        "slug": slug,
    }
    return _set("zip", True, update["label"], update=update)


def apply_zip(root=None, get=None):
    """Download the public repo as ZIP and copy it over the folder: every
    file of the snapshot is replaced or added, nothing of the user's is
    touched (DATA_FILES, KEEP_DIRS). Python does not keep its source files
    open, so this works while the app runs; the restart loads the new code."""
    root = root or APP_DIR
    get = get or http_get
    res = check_zip(root, get)
    if not res["update"]:
        return res
    slug = res["update"]["slug"]
    tmp = tempfile.mkdtemp(prefix="funkstonks-update-")
    try:
        zip_path = os.path.join(tmp, "repo.zip")
        with open(zip_path, "wb") as f:
            f.write(get(ARCHIVE_URL.format(slug=slug, branch=PUBLIC_BRANCH)))
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            top = names[0].split("/")[0] if names and "/" in names[0] else ""
            n = 0
            for name in names:
                rel = name[len(top) + 1 :] if top else name
                parts = rel.split("/")
                if not rel or rel in DATA_FILES or parts[0] in KEEP_DIRS or parts[0] == "moneyflow.db-journal":
                    continue
                dst = os.path.join(root, *parts)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(name) as src, open(dst, "wb") as out:
                    out.write(src.read())
                n += 1
    except Exception as e:
        return _set("zip", False, "FEHLER Aktualisierung fehlgeschlagen: " + str(e)[:120])
    finally:
        _rmtree(tmp)
    if n == 0:
        return _set("zip", False, "FEHLER das ZIP von GitHub war leer.")
    return _set("zip", True, f"aktualisiert ({n} Dateien) – Neustart", restart=True)


def check_frozen(root=None, get=None):
    """Newest GitHub release vs APP_VERSION. The offered update carries the
    version, the release page and the ZIP asset (if any)."""
    get = get or http_get
    slug = update_repo(root)
    if not slug:
        return _set("frozen", False, "keine Update-Quelle hinterlegt (update.json fehlt neben der EXE).")
    try:
        data = json.loads(get(RELEASES_URL.format(slug=slug)).decode("utf-8", "replace"))
    except Exception as e:
        return _set("frozen", False, "WARNUNG keine Verbindung zu GitHub (offline?): " + str(e)[:120])
    tag = str(data.get("tag_name", "")).strip()
    if _version_tuple(tag) <= _version_tuple(APP_VERSION):
        return _set("frozen", True, f"aktuell (Version {APP_VERSION})")
    asset = next(
        (a.get("browser_download_url") for a in data.get("assets", []) if str(a.get("name", "")).endswith(".zip")), ""
    )
    update = {
        "kind": "frozen",
        "label": f"Version {tag.lstrip('v')} verfügbar (installiert: {APP_VERSION})",
        "version": tag.lstrip("v"),
        "url": data.get("html_url", ""),
        "asset": asset,
        # self-update only on Windows (helper script swaps the files after the app has quit)
        "self_update": bool(asset) and os.name == "nt",
    }
    return _set("frozen", True, update["label"], update=update)


def prepare_frozen_update(update, root=None, get=None):
    """Download the release ZIP, unpack it next to the EXE and write the
    helper script that copies the new files over the old ones once the app
    has quit, keeps the database/backups/uploads and starts the EXE again.
    Returns the script path; the caller launches it and exits."""
    root = root or APP_DIR
    get = get or http_get
    tmp = os.path.join(root, "update_tmp")
    if os.path.isdir(tmp):
        _rmtree(tmp)
    os.makedirs(tmp)
    zip_path = os.path.join(tmp, "update.zip")
    with open(zip_path, "wb") as f:
        f.write(get(update["asset"]))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    src = os.path.join(tmp, "funkSTONKS") if os.path.isdir(os.path.join(tmp, "funkSTONKS")) else tmp
    exe = os.path.join(root, "funkSTONKS.exe")
    keep_files = " ".join(f'"{n}"' for n in (*DATA_FILES, "moneyflow.db-journal", UPDATE_INFO))
    script = os.path.join(tempfile.gettempdir(), "funkstonks-update.bat")
    with open(script, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "title funkSTONKS wird aktualisiert\r\n"
            "echo funkSTONKS wird aktualisiert ...\r\n"
            "timeout /t 3 /nobreak >nul\r\n"
            f'robocopy "{src}" "{root}" /E /NFL /NDL /NJH /NJS /XF {keep_files} /XD backups uploads update_tmp >nul\r\n'
            f'rmdir /s /q "{tmp}" >nul 2>nul\r\n'
            f'start "" "{exe}"\r\n'
        )
    return script


def _rmtree(path):
    import shutil
    import stat

    def on_error(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)

    shutil.rmtree(path, onerror=on_error)


# ------------------------------------------------------------------ start-up and restart


def startup_enabled():
    """Setting startup_sync ("1" by default); without a database: on."""
    v = _read_settings(lambda con: get_setting(con, "startup_sync"))
    return v is None or v == "1"


def check_now(root=None):
    """What the mode calls for: dev syncs, clone and frozen only look."""
    mode = checkout_mode(root)
    if mode == "dev":
        return sync_dev(root)
    if mode == "clone":
        return check_clone(root)
    if mode == "zip":
        return check_zip(root)
    if mode == "frozen":
        return check_frozen(root)
    return _set("none", True, "keine Update-Quelle (weder Git-Checkout noch ZIP von GitHub noch fertige Version).")


def startup_sync(root=None):
    """Called by main() BEFORE the database is opened (the sync may replace
    it). Prints the result; returns True when the app must restart to run
    the code that just arrived."""
    if not startup_enabled():
        STATE.update(mode=checkout_mode(root), text="Abgleich beim Start ausgeschaltet")
        return False
    res = check_now(root)
    print("Abgleich:", res["text"])
    if res["update"] and not res["restart"]:
        print("Update verfügbar:", res["update"]["label"], "– in der App unter Einstellungen → Updates")
    return bool(res["restart"]) and not os.environ.get(RESTART_ENV)


def restart_command():
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, *sys.argv]


def restart_app(wait):
    """Start the app again with the current files. wait=True (from main(),
    before the server runs): the new process runs in this console and its
    exit code is returned. wait=False (from a route, server running): the new
    process is detached and this one exits right after the response."""
    env = {**os.environ, RESTART_ENV: "1"}
    cmd = restart_command()
    if wait:
        return subprocess.call(cmd, cwd=APP_DIR, env=env)
    kwargs = {"cwd": APP_DIR, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    return 0


def launch_script(script):
    """Run the frozen-update helper detached (it waits for this process to quit)."""
    subprocess.Popen(["cmd", "/c", "start", "", "/min", script], cwd=config.APP_DIR, creationflags=0x00000008)
