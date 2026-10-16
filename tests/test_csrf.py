"""Local-server security: session key generated per database (not in the
code) and cross-site POSTs rejected by the Origin / Sec-Fetch-Site check."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m
from funkstonks import web


def _fresh():
    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    return tmp


def test_csrf():
    print("1) Session key lives in the database")
    _fresh()
    web.app.secret_key = web._UNSET_SECRET
    m.load_secret_key()
    key1 = web.app.secret_key
    check("key generated (64 hex)", len(key1) == 64 and all(ch in "0123456789abcdef" for ch in key1), key1)
    con = m.db()
    check("key stored as setting", m.get_setting(con, "secret_key", "") == key1)
    con.close()
    web.app.secret_key = web._UNSET_SECRET
    m.load_secret_key()
    check("second load keeps the key", web.app.secret_key == key1)
    _fresh()
    web.app.secret_key = web._UNSET_SECRET
    m.load_secret_key()
    check("new database -> new key", web.app.secret_key != key1)
    web.app.secret_key = web._UNSET_SECRET
    c = m.app.test_client()
    c.get("/einstellungen")
    check("first request loads the key lazily", web.app.secret_key != web._UNSET_SECRET)

    print("2) Cross-site POSTs are rejected")
    m.app.secret_key = "t"
    c = m.app.test_client()
    data = {"action": "theme", "theme": "dark"}
    r = c.post("/einstellungen", data=data, headers={"Origin": "http://evil.example"})
    check("foreign Origin -> 403", r.status_code == 403, r.status_code)
    check("403 text is German", "fremden Seiten" in r.get_data(as_text=True))
    r = c.post("/einstellungen", data=data, headers={"Sec-Fetch-Site": "cross-site"})
    check("Sec-Fetch-Site cross-site -> 403", r.status_code == 403, r.status_code)
    r = c.post("/einstellungen", data=data, headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-site"})
    check("same-site from another port -> 403 (Origin wins)", r.status_code == 403, r.status_code)
    con = m.db()
    check("rejected POST changed nothing", m.get_setting(con, "theme") != "dark", m.get_setting(con, "theme"))
    con.close()
    r = c.post("/einstellungen", data=data, headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
    check("own Origin + same-origin -> accepted", r.status_code in (200, 204, 302), r.status_code)
    r = c.post("/einstellungen", data=data, headers={"Sec-Fetch-Site": "none"})
    check("user-initiated (none) -> accepted", r.status_code in (200, 204, 302), r.status_code)
    r = c.post("/einstellungen", data=data)
    check("no headers (curl/tests) -> accepted", r.status_code in (200, 204, 302), r.status_code)
    con = m.db()
    check("accepted POST applied", m.get_setting(con, "theme") == "dark", m.get_setting(con, "theme"))
    con.close()
    r = c.get("/einstellungen", headers={"Origin": "http://evil.example"})
    check("GET is never blocked", r.status_code == 200, r.status_code)
    r = c.get("/")
    cookie = r.headers.get("Set-Cookie", "")
    check(
        "session cookie SameSite=Lax + HttpOnly",
        (not cookie) or ("SameSite=Lax" in cookie and "HttpOnly" in cookie),
        cookie,
    )
    finish()
