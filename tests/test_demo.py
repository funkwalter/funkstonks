"""Sample data: first-steps card on an empty dashboard, loading only into an
empty DB, banner with remove button, reports work with it, removal without
leftovers (user request 2026-10-13: onboarding for new users)."""

import re

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_demo():

    tmp, c = fresh_db()
    print("1) Empty dashboard shows first steps")
    h = c.get("/").get_data(as_text=True)
    check("'first steps' card with sample-data button", 'class="card first-steps"' in h and 'action="/demo"' in h)
    check("no demo banner", "demo-banner" not in h)

    print("\n2) Load sample data")
    r = c.post("/demo", data={}, follow_redirects=True)
    h = r.get_data(as_text=True)
    n = count("json_extract(extra,'$.demo')=1")
    check("transactions created (> 60, all marked as demo)", n > 60 and count("1") == n, (n, count("1")))
    check("flash + banner", "Beispielbuchungen geladen" in h and "demo-banner" in h)
    check("first-steps card gone", 'class="card first-steps"' not in h)
    check("setting demo_data set", one("SELECT value FROM settings WHERE key='demo_data'")["value"] == "1")
    check("everything categorized (nothing in review)", count("status='active' AND category_id IS NULL") == 0)
    check("depot order present", one("SELECT COUNT(*) n FROM depot_tx WHERE raw_hash LIKE 'demo:%'")["n"] == 1)
    for path in ("/", "/investitionen", "/jahr", "/fixkosten", "/transactions", "/partner", "/vergleich"):
        check("GET %s works with sample data" % path, c.get(path).status_code == 200)
    h = c.get("/?range=all").get_data(as_text=True)
    check(
        "dashboard calculates: invested 800 (4 x 200)",
        re.search(r'Investiert[^<]*</span>\s*<a[^>]*>\s*<b class="invest">800\.00 €', h) is not None,
    )
    h = c.get("/fixkosten").get_data(as_text=True)
    check("recurring costs detect streaming/internet/mobile", "Streaming Muster" in h and "Mobilfunk Muster" in h)
    r = c.post("/demo", data={}, follow_redirects=True)
    check("second load rejected (DB not empty)", "FEHLER" in r.get_data(as_text=True) and count("1") == n)

    print("\n3) Remove")
    r = c.post("/demo", data={"remove": "1"}, follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "everything gone",
        count("1") == 0
        and one("SELECT COUNT(*) n FROM depot_tx")["n"] == 0
        and one("SELECT COUNT(*) n FROM settings WHERE key='demo_data'")["n"] == 0,
    )
    check("redirect to import with flash", "Beispieldaten entfernt" in h and 'name="files"' in h)
    check("dashboard shows first steps again", 'class="card first-steps"' in c.get("/").get_data(as_text=True))

    print("\n4) Real data present -> no loading")
    upload(c, (george_bank([("02.05.2026", "Arbeitgeber AG", "Gehalt", "3000,00")]), "b.csv"))
    r = c.post("/demo", data={}, follow_redirects=True)
    check("rejected", "FEHLER" in r.get_data(as_text=True) and count("1") == 1)
    finish()
