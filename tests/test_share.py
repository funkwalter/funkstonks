"""Publishing: build the snapshot and push it as one root commit to the public repo.

Everything runs against throwaway repos in a temp folder: a 'private'
checkout with a database, a bare repo as its origin and a second bare repo as
the 'public' target. The real DB and the real repos are not touched. Sets and
numbers are checked: files in the public tree, commit count, return values.
"""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m

IDENT = ("-c", "user.name=Test", "-c", "user.email=test@example.com")


def git(cwd, *args):
    r = subprocess.run(
        ["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def listing(folder):
    """Relative paths of all files under folder (without .git), using '/'."""
    out = set()
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, fn), folder).replace(os.sep, "/"))
    return out


def commits(bare):
    return int(git(bare, "rev-list", "--all", "--count") or 0)


def public_tree(bare):
    return set(git(bare, "ls-tree", "-r", "--name-only", m.PUBLIC_BRANCH).split())


def setting(key, value):
    con = m.db()
    m.set_setting(con, key, value)
    con.commit()
    con.close()


def publish_temp_dirs():
    base = tempfile.gettempdir()
    return {d for d in os.listdir(base) if d.startswith("funkstonks-publish-")}


def test_share():
    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    setting("share_forbidden", '["geheimname"]')

    # 'private' developer checkout: code, publish.bat as marker, database versioned
    dev = os.path.join(tmp, "dev")
    os.makedirs(dev)
    git(dev, "init", "-q", "-b", "master")
    git(dev, "config", "user.name", "Test")
    git(dev, "config", "user.email", "test@example.com")
    write(dev, ".gitignore", "backups/\n")
    write(dev, "app.py", "print('hi')\n")
    write(dev, "funkstonks/x.py", "X = 1\n")
    write(dev, "README.md", "# Demo\n")
    write(dev, "publish.bat", "@echo off\n")
    write(dev, "templates/notes.html", "Kontakt: GeheimName\n")  # hit, case does not matter
    write(dev, "run.command", "echo GeheimName\n")  # shell scripts are checked (formerly not)
    write(dev, "tests/test_ok.py", "def test_ok():\n    pass\n")
    write(dev, "tests/test_migration.py", "# real DB\n")
    write(dev, "static/a.ico", "GeheimName")  # binary: not checked, but copied
    with open(os.path.join(dev, "moneyflow.db"), "wb") as f:
        f.write(b"\x00" * 64)
    write(dev, "backups/moneyflow_2026.db", "x")
    write(dev, "uploads/konto.csv", "a;b\n")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "First state")
    public = os.path.join(tmp, "public.git")
    git(tmp, "init", "-q", "--bare", "public.git")
    private = os.path.join(tmp, "private.git")
    git(tmp, "init", "-q", "--bare", "private.git")
    git(dev, "remote", "add", "origin", private)
    temp_before = publish_temp_dirs()

    print("1) Snapshot: data, excluded files and hits stay out")
    snap = os.path.join(tmp, "snap")
    leaks = m.build_share_tree(snap, dev)
    check("two files with a term reported", set(leaks) == {"templates/notes.html", "run.command"}, leaks)
    expect = {".gitignore", "app.py", "funkstonks/x.py", "README.md", "tests/test_ok.py", "static/a.ico"}
    check("exactly the allowed files copied", listing(snap) == expect, sorted(listing(snap)))
    gi = set(open(os.path.join(snap, ".gitignore"), encoding="utf-8").read().split())
    check(".gitignore protects the recipient", {"moneyflow.db", "backups/", "uploads/", ".venv/"} <= gi, gi)
    check("publish.bat not in SHARE_INCLUDE", "publish.bat" not in m.SHARE_INCLUDE)

    print("2) Without a clean setup nothing is published")
    ok, text = m.publish_share(app_dir=dev)
    check("no public URL -> FEHLER", not ok and text.startswith("FEHLER") and "Weitergabe" in text, text)
    setting("public_repo_url", private)  # misconfiguration: the private origin as target
    ok, text = m.publish_share(app_dir=dev)
    check("same remote as private -> FEHLER", not ok and "dasselbe Remote" in text, text)
    setting("public_repo_url", public)
    setting("share_forbidden", "[]")
    ok, text = m.publish_share(app_dir=dev)
    check("no terms -> FEHLER", not ok and "Begriffe" in text, text)
    setting("share_forbidden", '["geheimname"]')

    print("3) Hits abort before anything leaves the machine")
    ok, text = m.publish_share(app_dir=dev)
    check("FEHLER names both files", not ok and "templates/notes.html" in text and "run.command" in text, text)
    check("public repo empty", commits(public) == 0, commits(public))

    print("4) A clean state is published as ONE root commit without a body")
    write(dev, "templates/notes.html", "Kontakt: siehe Hilfe\n")
    write(dev, "run.command", "echo start\n")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "Terms removed")
    ok, text = m.publish_share(app_dir=dev)
    check("ok", ok, text)
    check("one commit in the public repo", commits(public) == 1, commits(public))
    expect |= {"templates/notes.html", "run.command"}
    check("tree = allowed files, no data", public_tree(public) == expect, sorted(public_tree(public)))
    check("fixed subject", git(public, "log", "-1", "--pretty=%s", m.PUBLIC_BRANCH) == m.PUBLIC_COMMIT_SUBJECT)
    check("no body", git(public, "log", "-1", "--pretty=%b", m.PUBLIC_BRANCH) == "")
    parents = git(public, "rev-list", "--parents", "-n", "1", m.PUBLIC_BRANCH).split()
    check("root commit without parents", len(parents) == 1, parents)
    check(
        "identity of the private checkout",
        git(public, "log", "-1", "--pretty=%ae", m.PUBLIC_BRANCH) == "test@example.com",
    )

    print("5) Idempotent; the database does not block, uncommitted code does; old state is replaced")
    ok, text = m.publish_share(app_dir=dev)
    check("second run: ok, nothing new", ok and "nichts Neues" in text, text)
    check("still one commit", commits(public) == 1, commits(public))
    with open(os.path.join(dev, "moneyflow.db"), "wb") as f:
        f.write(b"\x01" * 64)
    ok, text = m.publish_share(app_dir=dev)
    check("changed database does not block", ok, text)
    write(dev, "app.py", "print('neu')\n")
    ok, text = m.publish_share(app_dir=dev)
    check("uncommitted code change -> FEHLER with file", not ok and "app.py" in text, text)
    check("nothing published", "neu" not in git(public, "show", f"{m.PUBLIC_BRANCH}:app.py"))
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "Second state")
    first = git(public, "rev-parse", m.PUBLIC_BRANCH)
    ok, text = m.publish_share(app_dir=dev)
    check("second state published", ok, text)
    check("still exactly one commit (old one replaced)", commits(public) == 1, commits(public))
    check("new root commit", git(public, "rev-parse", m.PUBLIC_BRANCH) != first)
    check("fixed subject again", git(public, "log", "-1", "--pretty=%s", m.PUBLIC_BRANCH) == m.PUBLIC_COMMIT_SUBJECT)
    check("app.py current in the public repo", "neu" in git(public, "show", f"{m.PUBLIC_BRANCH}:app.py"))
    check("no Share/ folder in the checkout", not os.path.exists(os.path.join(dev, "Share")))
    check("no temporary snapshot left behind", publish_temp_dirs() == temp_before, publish_temp_dirs() - temp_before)

    print("6) A clone of the public repo is no developer checkout")
    clone = os.path.join(tmp, "clone")
    git(tmp, "clone", "-q", public, "clone")
    check("not a developer checkout", not m.is_dev_checkout(clone))
    check("developer checkout recognised", m.is_dev_checkout(dev))
    finish()
