"""Publish a checked snapshot of the code to the public repo.

The private checkout's history contains real data and therefore can never be
the source of the public repo. publish_share() copies only SHARE_INCLUDE into
a temporary directory (every text file checked against the setting
share_forbidden), turns it into ONE parentless commit with a fixed subject and
force-pushes that to the setting public_repo_url. The public repo thus has no
history and no local checkout (user decisions 2026-10-15: no commit comments
there, no Share/ folder in the project).
"""

import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zipfile

from . import config
from .config import APP_DIR, APP_VERSION
from .db import get_setting, get_setting_list

# User request 2026-09-13: "eine kopie welche immer am aktuellen stand ist,
# aber die man immer weitergeben koennte" (a copy that is always current and
# could always be passed on) - today that copy is the public repo itself.
# User request 2026-10-15: only what the code needs - the developer docs
# (CLAUDE.md, docs/, CHANGELOG.md) stay in the private checkout.
SHARE_INCLUDE = [
    "app.py",
    "funkstonks",
    "run.bat",
    "run.command",
    "build.bat",
    "build_mac.sh",
    "pyproject.toml",
    "requirements-dev.txt",
    ".gitattributes",
    "README.md",
    "templates",
    "static",
    "tests",
]
SHARE_EXCLUDE = {
    "moneyflow.db",
    "moneyflow.db-journal",
    "backups",
    "uploads",
    ".venv",
    ".git",
    ".claude",
    "__pycache__",
    "tests/test_migration.py",
}  # test_migration checks the real DB
# Only these files are not checked for terms - everything else counts as text
# (formerly an allowlist that overlooked shell scripts and pyproject.toml).
BINARY_SUFFIXES = (".ico", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".ttf", ".db", ".pyc")
# .gitignore of the public repo: everything that only comes into being at the recipient's.
SHARE_GITIGNORE = [
    ".venv/",
    "__pycache__/",
    "*.pyc",
    "moneyflow.db",
    "moneyflow.db-journal",
    "backups/",
    "uploads/",
    ".claude/",
    "build/",
    "dist/",
    "*.spec",
    "funkSTONKS.lnk",
]
# publish.bat is not passed on - only the developer checkout has it, so a clone
# of the public repo shows no distribution card and never publishes.
# (Until 2026-10-15 share.bat was the marker; with automatic publishing on every
# push it became superfluous.)
DEV_MARKER = "publish.bat"
# Fixed subject of the single public commit (user decision 2026-10-15: no commit comments there).
PUBLIC_COMMIT_SUBJECT = "funkSTONKS"
PUBLIC_BRANCH = "main"


def is_dev_checkout(root=None):
    """Developer checkout = git checkout with publish.bat (a clone of the public repo has none)."""
    root = root or APP_DIR
    return os.path.isdir(os.path.join(root, ".git")) and os.path.isfile(os.path.join(root, DEV_MARKER))


def _read_settings(fn):
    """Run fn(con) against the configured DB; fallback value on missing DB or SQL error."""
    if not os.path.exists(config.DB_PATH):
        return None
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return fn(con)
    except sqlite3.Error:
        return None
    finally:
        con.close()


def forbidden_terms():
    """Terms from the setting share_forbidden, lowercased; [] without DB or setting."""
    terms = _read_settings(lambda con: get_setting_list(con, "share_forbidden"))
    return [str(w).lower() for w in (terms or []) if str(w).strip()]


def public_repo_url():
    """URL of the public repo from the setting public_repo_url; "" without DB or setting."""
    return (_read_settings(lambda con: get_setting(con, "public_repo_url")) or "").strip()


def build_share_tree(target, app_dir=None):
    """Copy the shareable files (SHARE_INCLUDE minus SHARE_EXCLUDE) into target.

    Every copied text file is checked for the terms from the setting
    `share_forbidden` (the owner's names etc., Einstellungen -> Weitergabe); a
    hit is not copied and is reported - better one file too few than one name
    too many. The list lives in the (never shared) database, not in the code.
    Also writes the recipient's .gitignore. Returns the list of files not copied.
    """
    root = app_dir or APP_DIR
    forbidden = forbidden_terms()
    leaks = []

    def wanted(rel):
        parts = rel.replace(os.sep, "/").split("/")
        return not (
            parts[0] in SHARE_EXCLUDE
            or rel.replace(os.sep, "/") in SHARE_EXCLUDE
            or any(p == "__pycache__" or p.endswith(".pyc") for p in parts)
        )

    def clean(src, rel):
        if not forbidden or src.lower().endswith(BINARY_SUFFIXES):
            return True
        try:
            text = open(src, encoding="utf-8", errors="ignore").read().lower()
        except OSError:
            return True
        if any(w in text for w in forbidden):
            leaks.append(rel.replace(os.sep, "/"))
            return False
        return True

    os.makedirs(target, exist_ok=True)
    for name in SHARE_INCLUDE:
        src = os.path.join(root, name)
        if not os.path.exists(src):
            continue
        if os.path.isfile(src):
            if wanted(name) and clean(src, name):
                shutil.copy2(src, os.path.join(target, name))
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                if not wanted(rel) or not clean(full, rel):
                    continue
                dst = os.path.join(target, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(full, dst)
    with open(os.path.join(target, ".gitignore"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(SHARE_GITIGNORE) + "\n")
    return leaks


def _git(cwd, *args):
    """Run git in cwd; CompletedProcess, or None if git is missing."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


def _git_out(cwd, *args):
    """stdout of a successful git call without surrounding whitespace, otherwise None."""
    r = _git(cwd, *args)
    return r.stdout.strip() if r is not None and r.returncode == 0 else None


def _git_error(r):
    return (r.stderr if r is not None else "git nicht gefunden").strip()[-300:]


def _norm_url(url):
    url = url.strip().replace("\\", "/").rstrip("/").lower()
    return url[:-4] if url.endswith(".git") else url


def _rmtree(path):
    """Remove a temporary git repo; git marks its objects read-only, which rmtree on Windows trips over."""

    def on_error(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)

    shutil.rmtree(path, onerror=on_error)


def publish_share(app_dir=None):
    """Build the public snapshot in a temporary directory and force-push it as ONE root commit.

    Aborts before anything leaves the machine if
      * this is not the developer checkout (no .git or no publish.bat),
      * the setting public_repo_url is empty or equals the private origin
        (so the private history can never end up there),
      * no terms are stored in share_forbidden (no check, no publication),
      * shared files in the private checkout have uncommitted changes (the
        public state should correspond to a private commit; the database
        does not count here),
      * a file contains a term (it would otherwise be silently missing).
    The public repo always consists of exactly one commit with the subject
    PUBLIC_COMMIT_SUBJECT and no body; if the remote already holds the same
    tree, nothing is pushed. Returns (ok, text); on abort the text starts
    with "FEHLER ".
    """
    root = app_dir or APP_DIR
    if not is_dev_checkout(root):
        return False, "FEHLER kein Entwickler-Checkout (kein .git oder keine publish.bat) - nichts veroeffentlicht."
    url = public_repo_url()
    if not url:
        return (
            False,
            "FEHLER kein oeffentliches Repo hinterlegt (Einstellungen -> Weitergabe) - nichts veroeffentlicht.",
        )
    private_url = _git_out(root, "remote", "get-url", "origin") or ""
    if _norm_url(url) == _norm_url(private_url):
        return (
            False,
            "FEHLER das oeffentliche Repo ist dasselbe Remote wie der private Checkout - nichts veroeffentlicht.",
        )
    if not forbidden_terms():
        return False, (
            "FEHLER keine persoenlichen Begriffe hinterlegt (Einstellungen -> Weitergabe)"
            " - ohne Pruefung wird nichts veroeffentlicht."
        )
    paths = [p for p in SHARE_INCLUDE if os.path.exists(os.path.join(root, p))]
    r = _git(root, "status", "--porcelain", "--", *paths)
    if r is None or r.returncode != 0:
        return False, "FEHLER git status im privaten Checkout fehlgeschlagen."
    # porcelain: two status characters, space, path - do NOT strip stdout here
    files = sorted({line[3:].strip() for line in r.stdout.splitlines() if line.strip()})
    if files:
        return False, "FEHLER uncommittete Aenderungen - erst committen: " + ", ".join(files)
    tmp = tempfile.mkdtemp(prefix="funkstonks-publish-")
    try:
        leaks = build_share_tree(tmp, root)
        if leaks:
            return False, "FEHLER persoenliche Begriffe in: " + ", ".join(leaks) + " - nichts veroeffentlicht."
        if (
            _git_out(tmp, "init", "-q") is None
            or _git_out(tmp, "symbolic-ref", "HEAD", f"refs/heads/{PUBLIC_BRANCH}") is None
        ):
            return False, "FEHLER git init fuer den Schnappschuss fehlgeschlagen."
        # The commit carries the identity of the private checkout (noreply address there).
        for key in ("user.name", "user.email"):
            value = _git_out(root, "config", key)
            if value:
                _git(tmp, "config", key, value)
        if _git_out(tmp, "add", "-A") is None:
            return False, "FEHLER git add fuer den Schnappschuss fehlgeschlagen."
        tree = _git_out(tmp, "write-tree")
        if not tree:
            return False, "FEHLER git write-tree fehlgeschlagen."
        remote_tree = None
        if _git_out(tmp, "fetch", "-q", url, PUBLIC_BRANCH) is not None:
            remote_tree = _git_out(tmp, "rev-parse", "FETCH_HEAD^{tree}")
        if remote_tree == tree:
            return True, f"nichts Neues - das oeffentliche Repo hat bereits diesen Stand ({tree[:7]})."
        # User request 2026-10-15: no commit comments in the public repo - always ONE
        # root commit with a fixed subject (commit-tree without parents), the old one
        # is discarded by the force-push. Nothing that once slipped in lingers there.
        commit = _git_out(tmp, "commit-tree", tree, "-m", PUBLIC_COMMIT_SUBJECT)
        if not commit or _git_out(tmp, "update-ref", f"refs/heads/{PUBLIC_BRANCH}", commit) is None:
            return False, "FEHLER git commit fuer den Schnappschuss fehlgeschlagen."
        r = _git(tmp, "push", "-q", "--force", url, f"refs/heads/{PUBLIC_BRANCH}:refs/heads/{PUBLIC_BRANCH}")
        if r is None or r.returncode != 0:
            return False, "FEHLER git push fehlgeschlagen (Netz? Rechte?): " + _git_error(r)
        count = len((_git_out(tmp, "ls-files") or "").splitlines())
        return True, f"veroeffentlicht: {count} Dateien, Commit {commit[:7]} -> {url}"
    finally:
        _rmtree(tmp)


# ------------------------------------------------------------------ releases
# The public repo holds only source; laypeople get the stand-alone build as a
# ZIP attached to a GitHub release (user 2026-10-16: "damit DAU nichts
# installieren muessen"). Binaries never go into the git history.
RELEASE_ZIP = "funkSTONKS-v{version}-windows.zip"
RELEASE_NOTES = (
    "Stand-alone Windows build - no Python needed. Unzip the folder and start funkSTONKS.exe; "
    "the browser opens at http://127.0.0.1:5010 and the database is created next to the EXE."
    "\n\nThe file is not code-signed, so Windows SmartScreen may warn on first start: "
    'click "More info" and "Run anyway".'
)


def _repo_slug(url):
    """'https://github.com/owner/repo.git' -> 'owner/repo'; '' when not a GitHub URL."""
    m = re.search(r"github\.com/([^/]+/[^/]+)$", _norm_url(url))
    return m.group(1) if m else ""


def _run(cmd, cwd=None):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=isinstance(cmd, str)
    )


def build_release(app_dir=None, runner=None):
    """Build dist/funkSTONKS with build.bat, zip it and attach it as GitHub
    release v<APP_VERSION> to the public repo (gh CLI, setting public_repo_url).

    Aborts BEFORE building when this is not the developer checkout, no GitHub
    repo is configured, distributed files have uncommitted changes (the ZIP
    must match a published state), gh is missing or the tag already exists
    (bump APP_VERSION). build/, dist/ and the .spec are removed afterwards
    (the project folder stays clean). runner(cmd, cwd) replaces the external
    tools in tests. Returns (ok, text); text starts with "FEHLER " on abort."""
    root = app_dir or APP_DIR
    run = runner or _run
    if not is_dev_checkout(root):
        return False, "FEHLER kein Entwickler-Checkout (kein .git oder keine publish.bat) - kein Release."
    slug = _repo_slug(public_repo_url())
    if not slug:
        return False, "FEHLER kein oeffentliches GitHub-Repo hinterlegt (Einstellungen -> Weitergabe) - kein Release."
    tag = "v" + APP_VERSION
    paths = [p for p in SHARE_INCLUDE if os.path.exists(os.path.join(root, p))]
    r = _git(root, "status", "--porcelain", "--", *paths)
    if r is None or r.returncode != 0:
        return False, "FEHLER git status im privaten Checkout fehlgeschlagen."
    if r.stdout.strip():
        return False, "FEHLER uncommittete Aenderungen - erst committen und pushen, dann das Release bauen."
    r = run(["gh", "--version"])
    if r is None or r.returncode != 0:
        return False, "FEHLER gh (GitHub CLI) nicht gefunden - https://cli.github.com installieren und 'gh auth login'."
    r = run(["gh", "release", "view", tag, "--repo", slug])
    if r is not None and r.returncode == 0:
        return False, f"FEHLER Release {tag} gibt es schon - APP_VERSION in funkstonks/config.py erhoehen."
    # full path: cmd does not look in the current directory for a bare name when
    # NoDefaultCurrentDirectoryInExePath is set (it is on the owner's machine);
    # stdin from nul so build.bat's pause returns at once
    r = run(f'"{os.path.join(root, "build.bat")}" < nul', cwd=root)
    dist = os.path.join(root, "dist", "funkSTONKS")
    try:
        if r is None or r.returncode != 0 or not os.path.isfile(os.path.join(dist, "funkSTONKS.exe")):
            return False, "FEHLER Build fehlgeschlagen: " + ((r.stdout if r else "") or "")[-300:].strip()
        tmp = tempfile.mkdtemp(prefix="funkstonks-release-")
        try:
            zip_path = os.path.join(tmp, RELEASE_ZIP.format(version=APP_VERSION))
            n = 0
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, _dirs, files in os.walk(dist):
                    for fn in files:
                        full = os.path.join(dirpath, fn)
                        z.write(full, os.path.join("funkSTONKS", os.path.relpath(full, dist)))
                        n += 1
            r = run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    zip_path,
                    "--repo",
                    slug,
                    "--title",
                    f"funkSTONKS {tag}",
                    "--notes",
                    RELEASE_NOTES,
                ]
            )
            if r is None or r.returncode != 0:
                return False, "FEHLER gh release create fehlgeschlagen: " + _git_error(r)
            return True, f"Release {tag} erstellt: {n} Dateien im ZIP -> https://github.com/{slug}/releases/tag/{tag}"
        finally:
            _rmtree(tmp)
    finally:
        for d in ("build", "dist"):
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        spec = os.path.join(root, "funkSTONKS.spec")
        if os.path.exists(spec):
            os.remove(spec)
