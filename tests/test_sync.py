"""Start-up sync and updates (funkstonks/sync.py), everything against
throwaway repos in a temp folder - the real checkout and GitHub are never
touched, nothing goes online (the release check gets a stub for http_get).

Two 'devices' A and B share a bare origin: A's changed database travels to
B at the next start, code arriving at B asks for a restart, a database
changed on both sides is NOT merged (local state kept, WARNUNG, nothing
pushed). A clone of the public repo sees a newer tree, takes it over with
reset --hard and keeps its gitignored database. The frozen build compares
the latest release tag with APP_VERSION and prepares the helper script."""

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

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


def write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode, enc = ("wb", None) if isinstance(data, bytes) else ("w", "utf-8")
    with open(path, mode, encoding=enc, newline=None if enc is None else "\n") as f:
        f.write(data)


def read(root, rel, binary=False):
    with open(os.path.join(root, rel), "rb" if binary else "r", encoding=None if binary else "utf-8") as f:
        return f.read()


def subjects(cwd, ref="HEAD"):
    return git(cwd, "log", "--format=%s", ref).splitlines()


def make_private(tmp):
    """bare origin + developer checkouts A and B (publish.bat marker, a DB, one code file)."""
    origin = os.path.join(tmp, "origin.git")
    git(tmp, "init", "-q", "--bare", "-b", "master", origin)
    a = os.path.join(tmp, "A")
    git(tmp, "clone", "-q", origin, a)
    git(a, "config", "user.name", "Test")
    git(a, "config", "user.email", "test@example.com")
    write(a, "publish.bat", "rem marker\n")
    write(a, "moneyflow.db", b"db v1")
    write(a, "funkstonks/x.py", "V = 1\n")
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "initial")
    git(a, "push", "-q", "-u", "origin", "master")
    b = os.path.join(tmp, "B")
    git(tmp, "clone", "-q", origin, b)
    git(b, "config", "user.name", "Test")
    git(b, "config", "user.email", "test@example.com")
    return origin, a, b


def test_sync():
    tmp = tempfile.mkdtemp(prefix="funkstonks-sync-")

    print("\n1) Modes")
    origin, a, b = make_private(tmp)
    check("A is a developer checkout", m.checkout_mode(a) == "dev", m.checkout_mode(a))
    plain = os.path.join(tmp, "plain")
    os.makedirs(plain)
    check("folder without .git -> none", m.checkout_mode(plain) == "none")

    print("\n2) Data from A reach B at the next start")
    res = m.sync_dev(a)
    check("nothing to do -> ok, 'aktuell'", res["ok"] and res["text"] == "aktuell" and not res["restart"], res)
    write(a, "moneyflow.db", b"db v2 from A")
    res = m.sync_dev(a)
    check("A: ok, data committed and pushed", res["ok"] and "gesichert" in res["text"], res)
    check("A: no restart for data", not res["restart"])
    subs = subjects(origin, "master")
    check("origin: one data commit on top", len(subs) == 2 and subs[0].startswith(m.DATA_SUBJECT + " "), subs)
    check("A: nothing left uncommitted", git(a, "status", "--porcelain") == "")
    res = m.sync_dev(b)
    check("B: ok, pulled", res["ok"] and "geholt" in res["text"], res)
    check("B: no restart (only data arrived)", not res["restart"], res)
    check("B: database is A's", read(b, "moneyflow.db", True) == b"db v2 from A")

    print("\n3) Code arriving asks for a restart")
    write(a, "funkstonks/x.py", "V = 2\n")
    git(a, "commit", "-q", "-am", "code change")
    git(a, "push", "-q", "origin", "master")
    res = m.sync_dev(b)
    check("B: restart requested", res["ok"] and res["restart"], res)
    check("B: new code on disk", read(b, "funkstonks/x.py") == "V = 2\n")
    res = m.sync_dev(b)
    check("B: second start -> aktuell, no restart", res["text"] == "aktuell" and not res["restart"], res)

    print("\n4) Data changed on BOTH devices: no automatic merge")
    write(a, "moneyflow.db", b"db v3 from A")
    check("A pushes v3", m.sync_dev(a)["ok"])
    write(b, "moneyflow.db", b"db v3 from B")
    res = m.sync_dev(b)
    check("B: WARNUNG, not ok", not res["ok"] and res["text"].startswith("WARNUNG"), res)
    check("B: local database kept", read(b, "moneyflow.db", True) == b"db v3 from B")
    check("B: own data commit exists locally", subjects(b)[0].startswith(m.DATA_SUBJECT + " "), subjects(b)[:2])
    check("B: no rebase left behind", not os.path.exists(os.path.join(b, ".git", "rebase-merge")))
    check("origin unchanged (A's v3 on top)", len(subjects(origin, "master")) == 4, subjects(origin, "master"))
    # resolved by hand (keep B's): B's commit on top of origin
    git(b, "fetch", "-q", "origin")
    git(b, "reset", "-q", "--soft", "origin/master")
    git(b, "commit", "-q", "-m", "Data merged by hand", "--", "moneyflow.db")
    res = m.sync_dev(b)
    check("after resolving by hand: pushed", res["ok"] and "gesichert" in res["text"], res)
    res = m.sync_dev(a)
    check("A gets B's database", read(a, "moneyflow.db", True) == b"db v3 from B", res)

    print("\n5) Offline: a warning, nothing breaks")
    git(b, "remote", "set-url", "origin", os.path.join(tmp, "does-not-exist.git"))
    write(b, "moneyflow.db", b"db v4 offline")
    res = m.sync_dev(b)
    check("WARNUNG offline", not res["ok"] and res["text"].startswith("WARNUNG"), res)
    check("database untouched", read(b, "moneyflow.db", True) == b"db v4 offline")
    check("nothing committed while offline", git(b, "status", "--porcelain", "--", "moneyflow.db") != "")

    print("\n6) Code and data in one go")
    git(b, "remote", "set-url", "origin", origin)
    write(a, "funkstonks/x.py", "V = 3\n")
    git(a, "commit", "-q", "-am", "more code")
    git(a, "push", "-q", "origin", "master")
    res = m.sync_dev(b)
    check("B: data committed, code pulled, pushed, restart", res["ok"] and res["restart"], res)
    check("B: code v3", read(b, "funkstonks/x.py") == "V = 3\n")
    check(
        "B: data v4 on origin",
        subjects(origin, "master")[0].startswith(m.DATA_SUBJECT + " "),
        subjects(origin, "master")[:2],
    )

    print("\n7) Clone of the public repo")
    pub = os.path.join(tmp, "pub.git")
    git(tmp, "init", "-q", "--bare", "-b", m.PUBLIC_BRANCH, pub)
    work = os.path.join(tmp, "work")
    git(tmp, "init", "-q", "-b", m.PUBLIC_BRANCH, work)
    write(work, "app.py", "print(1)\n")
    write(work, ".gitignore", "moneyflow.db\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", m.PUBLIC_COMMIT_SUBJECT)
    git(work, "push", "-q", pub, m.PUBLIC_BRANCH)
    c = os.path.join(tmp, "C")
    git(tmp, "clone", "-q", pub, c)
    write(c, "moneyflow.db", b"user db")
    check("C is a clone (no publish.bat)", m.checkout_mode(c) == "clone", m.checkout_mode(c))
    res = m.check_clone(c)
    check("C: aktuell, no update", res["ok"] and res["update"] is None and res["text"] == "aktuell", res)
    # a new publication = a NEW root commit force-pushed (no shared history)
    write(work, "app.py", "print(2)\n")
    write(work, "funkstonks/new.py", "# new\n")
    git(work, "add", "-A")
    tree = git(work, "write-tree")
    commit = git(work, "commit-tree", tree, "-m", m.PUBLIC_COMMIT_SUBJECT)
    git(work, "update-ref", "refs/heads/" + m.PUBLIC_BRANCH, commit)
    git(work, "push", "-q", "--force", pub, m.PUBLIC_BRANCH)
    res = m.check_clone(c)
    check("C: update offered", res["ok"] and res["update"] and res["update"]["kind"] == "clone", res)
    check("C: label counts 2 files", res["update"] and "2 Dateien" in res["update"]["label"], res["update"])
    check("C: nothing changed yet", read(c, "app.py") == "print(1)\n")
    res = m.apply_clone(c)
    check("C: applied, restart", res["ok"] and res["restart"], res)
    check(
        "C: new files on disk",
        read(c, "app.py") == "print(2)\n" and os.path.exists(os.path.join(c, "funkstonks/new.py")),
    )
    check("C: database kept (gitignored)", read(c, "moneyflow.db", True) == b"user db")
    res = m.check_clone(c)
    check("C: aktuell again", res["update"] is None and res["text"] == "aktuell", res)
    res = m.apply_clone(c)
    check("C: apply without update is a no-op", not res["restart"])

    print("\n8) Stand-alone build: release check with a stub")
    fz = os.path.join(tmp, "frozen")
    os.makedirs(fz)
    calls = []

    def fake_get(version, asset="https://example.invalid/funkSTONKS.zip"):
        def get(url):
            calls.append(url)
            if url.endswith(".zip"):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    z.writestr("funkSTONKS/funkSTONKS.exe", b"exe")
                    z.writestr("funkSTONKS/_internal/a.dll", b"dll")
                return buf.getvalue()
            data = {
                "tag_name": "v" + version,
                "html_url": f"https://github.com/owner/repo/releases/tag/v{version}",
                "assets": [{"name": "funkSTONKS-v" + version + "-windows.zip", "browser_download_url": asset}],
            }
            return json.dumps(data).encode()

        return get

    res = m.check_frozen(fz, get=fake_get("9.9.9"))
    check(
        "without update.json: no source, no call", not res["ok"] and calls == [] and "update.json" in res["text"], res
    )
    write(fz, m.UPDATE_INFO, json.dumps({"repo": "owner/repo"}))
    res = m.check_frozen(fz, get=fake_get(m.APP_VERSION))
    check("same version: aktuell", res["ok"] and res["update"] is None, res)
    check("API url carries the repo", calls[-1] == m.RELEASES_URL.format(slug="owner/repo"), calls[-1])
    res = m.check_frozen(fz, get=fake_get("1.0.0"))
    check("older release: no update", res["update"] is None, res)
    res = m.check_frozen(fz, get=fake_get("9.9.9"))
    upd = res["update"]
    check("newer release offered", upd and upd["version"] == "9.9.9" and upd["kind"] == "frozen", upd)
    check("asset and page", upd and upd["asset"].endswith(".zip") and "releases/tag/v9.9.9" in upd["url"], upd)
    check("self-update only on Windows", bool(upd and upd["self_update"]) == (os.name == "nt"), upd)

    def broken(url):
        raise OSError("no network")

    res = m.check_frozen(fz, get=broken)
    check("offline: WARNUNG", not res["ok"] and res["text"].startswith("WARNUNG"), res)
    if os.name == "nt":
        script = m.prepare_frozen_update(upd, root=fz, get=fake_get("9.9.9"))
        text = read(os.path.dirname(script), os.path.basename(script))
        check(
            "ZIP unpacked next to the EXE",
            os.path.exists(os.path.join(fz, "update_tmp", "funkSTONKS", "funkSTONKS.exe")),
        )
        check(
            "script copies with robocopy and keeps the database", "robocopy" in text and '"moneyflow.db"' in text, text
        )
        check(
            "script keeps backups/uploads and restarts the EXE",
            "/XD backups uploads" in text and "funkSTONKS.exe" in text,
        )
        os.remove(script)

    print("\n9) ZIP download of the public repo")
    zd = os.path.join(tmp, "zipcopy")
    os.makedirs(os.path.join(zd, "backups"))
    write(zd, "app.py", "print(1)\n")
    write(zd, "moneyflow.db", b"zip user db")
    write(zd, "backups/moneyflow_2026.db", b"old backup")
    check("folder without update.json -> none", m.checkout_mode(zd) == "none")
    write(zd, m.UPDATE_INFO, json.dumps({"repo": "owner/repo", "source": "aaaa", "version": "1.1.0"}))
    check("with update.json -> zip", m.checkout_mode(zd) == "zip", m.checkout_mode(zd))
    zcalls = []

    def zip_get(source, version="1.2.0"):
        def get(url):
            zcalls.append(url)
            if url.endswith(".zip"):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    z.writestr("repo-main/app.py", "print(2)\n")
                    z.writestr("repo-main/funkstonks/new.py", "# new\n")
                    z.writestr("repo-main/moneyflow.db", "must not land")
                    z.writestr("repo-main/backups/x.db", "must not land")
                    z.writestr("repo-main/" + m.UPDATE_INFO, json.dumps({"repo": "owner/repo", "source": source}))
                return buf.getvalue()
            return json.dumps({"repo": "owner/repo", "source": source, "version": version}).encode()

        return get

    res = m.check_zip(zd, get=zip_get("aaaa"))
    check("same source hash: aktuell", res["ok"] and res["update"] is None, res)
    check(
        "raw file asked on GitHub",
        zcalls[-1] == m.RAW_URL.format(slug="owner/repo", branch=m.PUBLIC_BRANCH, name=m.UPDATE_INFO),
        zcalls[-1],
    )
    res = m.check_zip(zd, get=zip_get("bbbb"))
    check(
        "other hash: update with version",
        res["update"] and res["update"]["kind"] == "zip" and "1.2.0" in res["update"]["label"],
        res,
    )
    res = m.apply_zip(zd, get=zip_get("bbbb"))
    check("applied, restart", res["ok"] and res["restart"] and "2 " not in res["text"], res)
    check("archive asked", zcalls[-1] == m.ARCHIVE_URL.format(slug="owner/repo", branch=m.PUBLIC_BRANCH), zcalls[-1])
    check(
        "code replaced and added",
        read(zd, "app.py") == "print(2)\n" and os.path.exists(os.path.join(zd, "funkstonks/new.py")),
    )
    check(
        "database and backups untouched",
        read(zd, "moneyflow.db", True) == b"zip user db"
        and read(zd, "backups/moneyflow_2026.db", True) == b"old backup",
    )
    check("no stray file from the archive", not os.path.exists(os.path.join(zd, "backups", "x.db")))
    res = m.check_zip(zd, get=zip_get("bbbb"))
    check("after the update: aktuell", res["update"] is None, res)
    res = m.check_zip(zd, get=broken)
    check("offline: WARNUNG", not res["ok"] and res["text"].startswith("WARNUNG"), res)

    print("\n10) Start-up switch")
    con = m.db() if os.path.exists(m.DB_PATH) else None
    if con is not None:
        con.close()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    check("default: on", m.startup_enabled())
    con = m.db()
    m.set_setting(con, "startup_sync", "")
    con.commit()
    con.close()
    check("switched off", not m.startup_enabled())
    check("startup_sync() does nothing when off", m.startup_sync(a) is False)
    finish()
