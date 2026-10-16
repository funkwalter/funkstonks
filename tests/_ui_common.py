"""Shared helper for the UI suites: test DB, check(), post() with cleanup after
unhandled exceptions (a connection left open with a running transaction would
otherwise lock the DB for all following writes)."""

import gc
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as m


def setup():
    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    return m, m.app.test_client(), tmp


from _harness import check, finish  # noqa: E402,F401


def _release_leaked():
    gc.collect()
    for o in list(gc.get_objects()):
        if isinstance(o, sqlite3.Connection):
            try:
                o.close()
            except Exception:
                pass
    gc.collect()


def post(c, url, data, follow=False):
    """Returns the response or 'EXC <type>: <text>' (string)."""
    try:
        return c.post(url, data=data, follow_redirects=follow)
    except Exception as e:
        msg = f"EXC {type(e).__name__}: {e}"
        e = None
        _release_leaked()
        return msg


def get(c, url):
    try:
        return c.get(url)
    except Exception as e:
        msg = f"EXC {type(e).__name__}: {e}"
        e = None
        _release_leaked()
        return msg
