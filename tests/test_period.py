"""Dashboard with a freely selectable period instead of a single month only, and
the month matrix on /jahr instead of the dashboard (user request 2026-09-11)."""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_period():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    leb = con.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()["id"]
    geh = con.execute("SELECT id FROM categories WHERE name='Gehalt'").fetchone()["id"]
    # Jan-Apr 2025 and May-Aug 2026: 100 expense and 1000 income per month each
    MONTHS = ["2025-0%d" % i for i in range(1, 5)] + ["2026-0%d" % i for i in range(5, 9)]
    for i, mo in enumerate(MONTHS):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Supermarkt','x',-100,'EUR',?,
        'active',?)""",
            (mo + "-10", "e%d" % i, leb),
        )
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Arbeitgeber','x',1000,'EUR',?,
        'active',?)""",
            (mo + "-15", "i%d" % i, geh),
        )
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    def kpi(url):
        """(income, expenses, period label) via /api/period."""
        d = c.get("/api/period" + (url[1:] if url.startswith("/?") else "")).get_json()
        return float(d["tiles"]["inc"]["value"]), float(d["tiles"]["exp"]["value"]), d["period"]

    print("1) Single month behaves as before")
    inc, exp, head = kpi("/?month=2026-06")
    check("income 1000", abs(inc - 1000) < 0.01, inc)
    check("expenses 100", abs(exp - 100) < 0.01, exp)
    check("heading names the month", head == "Juni 2026", head)
    check("no average for a single month", "€ / Monat" not in c.get("/?month=2026-06").get_data(as_text=True))

    print("2) Free range from-to")
    inc, exp, head = kpi("/?from=2026-05&to=2026-07")
    check("three months of income", abs(inc - 3000) < 0.01, inc)
    check("three months of expenses", abs(exp - 300) < 0.01, exp)
    check("heading names the range", head == "Mai – Juli 2026", head)
    check("reversed input gets sorted", kpi("/?from=2026-07&to=2026-05")[:2] == (3000.0, 300.0))
    h3 = c.get("/?from=2026-05&to=2026-07").get_data(as_text=True)
    check("average income 1000/month", "1000.00 € / Monat" in h3)
    check("average expenses 100/month", "100.00 € / Monat" in h3)
    check("average balance 900/month", "900.00 € / Monat" in h3)

    print("3) Whole year")
    inc, exp, head = kpi("/?range=2026")
    check("four months 2026", abs(inc - 4000) < 0.01, inc)
    check("heading is the year number", head == "2026", head)
    inc25, _, head25 = kpi("/?range=2025")
    check("2025 separate from it", abs(inc25 - 4000) < 0.01 and head25 == "2025", (inc25, head25))

    print("4) Everything and the last N months")
    inc, exp, head = kpi("/?range=all")
    check("all eight months", abs(inc - 8000) < 0.01, inc)
    check("heading across the turn of the year", head == "Jänner 2025 – August 2026", head)
    inc, _, head = kpi("/?range=last3")
    check("last 3 months", abs(inc - 3000) < 0.01, inc)
    check("that is June-August 2026", head == "Juni – August 2026", head)
    inc, _, _ = kpi("/?range=last12")
    check("last12 takes only existing months", abs(inc - 8000) < 0.01, inc)

    print("5) Nonsensical input falls back to the last month")
    for bad in ("/?month=2099-01", "/?range=quatsch", "/?from=&to=", "/?range="):
        inc, exp, head = kpi(bad)
        check("%s -> August 2026" % bad, head == "August 2026", head)

    print("6) The dropdown shows the active selection")
    h = c.get("/?range=2026").get_data(as_text=True)
    check("year selected", '<option value="2026" selected>2026</option>' in h)
    h = c.get("/?from=2026-05&to=2026-07").get_data(as_text=True)
    check("free range recognizable as such", 'value="custom" selected>Eigener Zeitraum' in h)
    check(
        "from/to preselected",
        '<option value="2026-05" selected>2026-05</option>' in h
        and '<option value="2026-07" selected>2026-07</option>' in h,
    )
    h = c.get("/?month=2026-06").get_data(as_text=True)
    check("single month selected", '<option value="2026-06" selected>2026-06</option>' in h)
    check("no 'custom period' needed", "eigener Zeitraum" not in h)

    print("7) Drilldown shows the same slice")
    h = c.get("/?from=2026-05&to=2026-07").get_data(as_text=True)
    check("tiles link with from/to", "from=2026-05&amp;to=2026-07" in h)
    d = c.get("/transactions?from=2026-05&to=2026-07&cat=%d" % leb).get_data(as_text=True)
    betraege = re.findall(r'<td class="num (?:neg|pos)">(-?[\d.]+)</td>', d)
    check("three grocery bookings", len(betraege) == 3, betraege)
    check("sum matches the tile", abs(sum(float(x) for x in betraege) + 300) < 0.01, betraege)
    check("period is displayed", "Zeitraum Mai – Juli 2026" in d)
    d = c.get("/transactions?from=2026-05&to=2026-07&month=2026-06&cat=%d" % leb).get_data(as_text=True)
    check("single month beats from/to", len(re.findall(r'<td class="num (?:neg|pos)">(-?[\d.]+)</td>', d)) == 1)

    print("8) Month matrix lives on /jahr, no longer on the dashboard")
    dash = c.get("/").get_data(as_text=True)
    jahr = c.get("/jahr?y=2026").get_data(as_text=True)
    check("dashboard without matrix", "Jahresübersicht" not in dash and "Monat für Monat" not in dash)
    check("/jahr has the matrix", "Monat für Monat – 2026" in jahr)
    row = re.search(r"Lebensmittel</td>(.*?)</tr>", jahr, re.S)
    check("matrix row present", bool(row))
    zellen = re.findall(r">(\d+)</a>", row.group(1)) if row else []
    check("four months with 100 each", zellen[:4] == ["100"] * 4, zellen)
    check("matrix still links to the month", "month=2026-05&cat=" in jahr)

    print("9) All pages still load")
    for p in (
        "/",
        "/?range=all",
        "/jahr",
        "/transactions",
        "/fixkosten",
        "/partner",
        "/rules",
        "/categories",
        "/review",
        "/import",
        "/hilfe",
        "/einstellungen",
    ):
        check("GET %s" % p, c.get(p).status_code == 200, c.get(p).status_code)
    finish()
