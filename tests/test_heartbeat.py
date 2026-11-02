"""Browser heartbeat: /ping and /bye answer 204 (even before setup), and
browser_gone() ends the process only when no page is left - never before
the first ping, not while pages ping, after the timeout without pings, and
after a goodbye that no new page followed within the grace period."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402
from _ui_common import setup


def test_heartbeat():
    m, c, tmp = setup()
    r = c.get("/ping")
    check("GET /ping before setup: 204, no redirect", r.status_code == 204, r.status_code)
    r = c.post("/bye")
    check("POST /bye before setup: 204", r.status_code == 204, r.status_code)
    r = c.post("/bye", headers={"Origin": "http://evil.example"})
    check("cross-site /bye rejected", r.status_code == 403, r.status_code)
    check("default: close with the browser", m.close_with_browser())
    con = m.db()
    m.set_setting(con, "close_with_browser", "")
    con.commit()
    con.close()
    check("switched off", not m.close_with_browser())

    t0 = 1000.0
    check("never armed -> stays up", not m.browser_gone(t0 + 9999, {"ping": None, "bye": None}))
    check("fresh ping -> stays up", not m.browser_gone(t0 + 5, {"ping": t0, "bye": None}))
    check("no ping for the timeout -> gone", m.browser_gone(t0 + m.HEARTBEAT_TIMEOUT + 1, {"ping": t0, "bye": None}))
    check("ping at the limit -> stays up", not m.browser_gone(t0 + m.HEARTBEAT_TIMEOUT, {"ping": t0, "bye": None}))
    hb = {"ping": t0, "bye": t0 + 2}
    check("bye, grace not over -> stays up", not m.browser_gone(t0 + 2 + m.BYE_GRACE, hb))
    check("bye, grace over -> gone", m.browser_gone(t0 + 2 + m.BYE_GRACE + 1, hb))
    hb = {"ping": t0 + 3, "bye": t0 + 2}
    check("bye followed by a new page's ping -> stays up", not m.browser_gone(t0 + 2 + m.BYE_GRACE + 1, hb))
    check("live dict armed by the route", m.HEARTBEAT["ping"] is not None)
    finish()
