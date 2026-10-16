"""Release: build_release() zips the stand-alone build and attaches it as a
GitHub release to the public repo - here with build.bat and gh replaced by a
stub runner, against a throwaway 'developer checkout' in a temp folder. The
refusal conditions and the exact gh command line are checked; nothing is
built or uploaded."""

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


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def setting(key, value):
    con = m.db()
    m.set_setting(con, key, value)
    con.commit()
    con.close()


class Stub:
    """Fake gh / build.bat: records every command, answers as configured."""

    def __init__(self, dev, gh_ok=True, tag_exists=False, build_ok=True, create_ok=True):
        self.dev, self.gh_ok, self.tag_exists, self.build_ok, self.create_ok = (
            dev,
            gh_ok,
            tag_exists,
            build_ok,
            create_ok,
        )
        self.calls = []
        self.zip_entries = None

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        rc = 0
        if isinstance(cmd, str):  # build.bat < nul
            if self.build_ok:
                write(self.dev, "dist/funkSTONKS/funkSTONKS.exe", "exe")
                write(self.dev, "dist/funkSTONKS/_internal/static/js/app.js", "js")
                write(self.dev, "build/x", "x")
                write(self.dev, "funkSTONKS.spec", "spec")
            else:
                rc = 1
        elif cmd[:2] == ["gh", "--version"]:
            rc = 0 if self.gh_ok else 1
        elif cmd[:3] == ["gh", "release", "view"]:
            rc = 0 if self.tag_exists else 1
        elif cmd[:3] == ["gh", "release", "create"]:
            # the ZIP only exists during the call - look inside now
            with zipfile.ZipFile(cmd[4]) as z:
                self.zip_entries = sorted(z.namelist())
            rc = 0 if self.create_ok else 1
        return subprocess.CompletedProcess(cmd, rc, "", "stub error" if rc else "")


def test_release():
    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    dev = os.path.join(tmp, "dev")
    write(dev, "app.py", "print('x')\n")
    write(dev, "publish.bat", "rem dev marker\n")
    write(dev, "build.bat", "rem build\n")
    git(dev, "init", "-q")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "init")
    tag = "v" + m.APP_VERSION

    print("1) Refusals before anything is built")
    plain = os.path.join(tmp, "plain")
    os.makedirs(plain)
    ok, text = m.build_release(app_dir=plain, runner=Stub(plain))
    check("not a developer checkout -> FEHLER", not ok and text.startswith("FEHLER"), text)
    ok, text = m.build_release(app_dir=dev, runner=Stub(dev))
    check("no public repo configured -> FEHLER", not ok and "Repo" in text, text)
    setting("public_repo_url", "https://example.org/not/github.git")
    ok, text = m.build_release(app_dir=dev, runner=Stub(dev))
    check("public repo not on GitHub -> FEHLER", not ok and "GitHub" in text, text)
    setting("public_repo_url", "https://github.com/owner/repo.git")
    write(dev, "app.py", "print('changed')\n")
    stub = Stub(dev)
    ok, text = m.build_release(app_dir=dev, runner=stub)
    check("uncommitted change -> FEHLER, nothing run", not ok and "uncommittete" in text and stub.calls == [], text)
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "change")
    stub = Stub(dev, gh_ok=False)
    ok, text = m.build_release(app_dir=dev, runner=stub)
    check("gh missing -> FEHLER, no build", not ok and "gh" in text and len(stub.calls) == 1, text)
    stub = Stub(dev, tag_exists=True)
    ok, text = m.build_release(app_dir=dev, runner=stub)
    check("tag exists -> FEHLER names the version", not ok and tag in text and len(stub.calls) == 2, text)
    check("tag check asks the public repo", stub.calls[1] == ["gh", "release", "view", tag, "--repo", "owner/repo"])
    stub = Stub(dev, build_ok=False)
    ok, text = m.build_release(app_dir=dev, runner=stub)
    check("build failure -> FEHLER", not ok and "Build" in text, text)
    check("no release attempted after a failed build", len(stub.calls) == 3, stub.calls)

    print("2) Release")
    stub = Stub(dev)
    ok, text = m.build_release(app_dir=dev, runner=stub)
    check("release created", ok, text)
    check("result names tag and URL", tag in text and "github.com/owner/repo/releases/tag/" + tag in text, text)
    create = stub.calls[-1]
    check(
        "gh release create with tag, repo, title",
        create[:4] == ["gh", "release", "create", tag]
        and "--repo" in create
        and create[create.index("--repo") + 1] == "owner/repo"
        and create[create.index("--title") + 1] == f"funkSTONKS {tag}",
        create,
    )
    check(
        "ZIP name carries the version",
        os.path.basename(create[4]) == m.RELEASE_ZIP.format(version=m.APP_VERSION),
        create[4],
    )
    check(
        "ZIP holds the folder funkSTONKS with the exe",
        stub.zip_entries == ["funkSTONKS/_internal/static/js/app.js", "funkSTONKS/funkSTONKS.exe"],
        stub.zip_entries,
    )
    check("notes mention SmartScreen", "SmartScreen" in create[create.index("--notes") + 1])
    check(
        "build/, dist/ and .spec removed afterwards",
        not os.path.exists(os.path.join(dev, "dist"))
        and not os.path.exists(os.path.join(dev, "build"))
        and not os.path.exists(os.path.join(dev, "funkSTONKS.spec")),
    )
    check("ZIP removed afterwards", not os.path.exists(create[4]))
    stub = Stub(dev, create_ok=False)
    ok, text = m.build_release(app_dir=dev, runner=stub)
    check(
        "upload failure -> FEHLER, folders cleaned anyway",
        not ok and "gh release create" in text and not os.path.exists(os.path.join(dev, "dist")),
        text,
    )
    check(
        "version has three parts",
        len(m.APP_VERSION.split(".")) == 3 and all(p.isdigit() for p in m.APP_VERSION.split(".")),
        m.APP_VERSION,
    )
    finish()
