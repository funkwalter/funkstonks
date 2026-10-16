"""Every clickable node in the flow diagram must show EXACTLY the bookings it
aggregates when clicked - for every period.

Two real bugs (2026-09-11, reported as "when I click these they are often
empty"):
  * The click handler built the URL in JavaScript from '{{period_qs}}'. Jinja
    escapes '&' to '&amp;' there - in an href attribute the browser converts it
    back, in a JS string it does NOT. '?from=..&to=..' became
    '?from=..&amp;to=..', 'to' was no longer a parameter of its own, the period
    shrank to the first month.
  * The aggregate node "Ausgaben" filtered via a fixed special value that also
    included net positive categories - but those hang under "Guthaben" in the
    diagram. List and node showed different totals.
"""

import json
import os
import re
import sys
import tempfile
from html import unescape as _unescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_drilldown():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")

    def cid(name):
        return con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()["id"]

    leb, geh, woh = cid("Lebensmittel"), cid("Gehalt"), cid("Wohnung")
    son, bar = cid("Sonstiges"), cid("Bargeld")
    con.execute(
        "INSERT INTO categories(name,color,kind,parent_id) VALUES('Sport','#8B8','expense',?)", (cid("Lifestyle"),)
    )
    spo = cid("Sport")
    ROWS = [
        # three months with salary and running expenses
        ("2026-05-15", "Arbeitgeber", 3000.0, geh),
        ("2026-06-15", "Arbeitgeber", 3000.0, geh),
        ("2026-07-15", "Arbeitgeber", 3000.0, geh),
        ("2026-05-03", "Supermarkt", -200.0, leb),
        ("2026-06-03", "Supermarkt", -250.0, leb),
        ("2026-07-03", "Supermarkt", -180.0, leb),
        ("2026-05-05", "Vermieter", -600.0, woh),
        ("2026-06-05", "Vermieter", -600.0, woh),
        ("2026-07-05", "Vermieter", -600.0, woh),
        # Sport only in JUNE - when clicking from a multi-month period the row must
        # still show up (the escaped '&' left exactly this kind of thing empty)
        ("2026-06-20", "Sportladen", -300.0, spo),
        # Sonstiges comes out net POSITIVE -> hangs under "Guthaben"
        ("2026-05-10", "Rueckzahlung", 500.0, son),
        ("2026-07-10", "Kleinkram", -120.0, son),
    ]
    for i, (d, p, a, cat) in enumerate(ROWS):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,?,'x',?,'EUR',?,'active',?)""",
            (d, p, a, "h%d" % i, cat),
        )
    # split payment: 100 EUR withdrawal, half Sport and half Bargeld
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,
    currency,raw_hash,status) VALUES('bank','2026-07-12','Bankomat','ATM',-100,'EUR','sp1','active')""")
    sp_id = con.execute("SELECT id FROM transactions WHERE raw_hash='sp1'").fetchone()["id"]
    for c_, a_ in ((spo, -50.0), (bar, -50.0)):
        con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (sp_id, c_, a_))
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    ROW = re.compile(
        r'<td class="num (?:neg|pos)">(-?[\d.]+)</td>\s*'
        r'<td class="num">(?:<b>(-?[\d.]+)</b>)?</td>(.*?)</tr>',
        re.S,
    )
    SHARE = re.compile(r"davon\s*(-?[\d.]+) € hier")

    def drill(url):
        """(row count, total as the page shows it) - split shares count with their
        share, not with the full amount of the parent booking."""
        d = c.get(url).get_data(as_text=True)
        tot = 0.0
        n = 0
        for a, e, rest in ROW.findall(d):
            sh = SHARE.search(rest)
            tot += float(sh.group(1)) if sh else (float(e) if e else float(a))
            n += 1
        return n, round(-tot, 2)

    print("1) The click link contains a real '&', not '&amp;'")
    h = c.get("/?from=2026-05&to=2026-07").get_data(as_text=True)
    js = re.search(r'id="sankey-data" data-period-qs="([^"]*)"', h)
    check(
        "click handler found",
        bool(js)
        and "dataset.periodQs" in open(os.path.join(ROOT, "static", "js", "dashboard.js"), encoding="utf-8").read(),
    )
    check("period unmangled", js and _unescape(js.group(1)) == "from=2026-05&to=2026-07", js and js.group(1))
    check(
        "no &amp; in the JavaScript",
        "&amp;"
        not in re.sub(r"//.*", "", open(os.path.join(ROOT, "static", "js", "dashboard.js"), encoding="utf-8").read()),
    )

    print("2) Category that occurs in only ONE month of the period")
    n, tot = drill("/transactions?from=2026-05&to=2026-07&cat=%d" % spo)
    check("Sport is not empty", n > 0, n)
    check("Sportladen and the split share", abs(tot - 350.0) < 0.01, (n, tot))

    print("3) Every clickable node == its hit list, in every period")
    for period in ("/?from=2026-05&to=2026-07", "/?range=2026", "/?month=2026-06", "/?range=last3"):
        h = c.get(period).get_data(as_text=True)
        qs = _unescape(re.search(r'id="sankey-data" data-period-qs="([^"]*)"', h).group(1))
        sn = json.loads(re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', h, re.S).group(1))
        schlecht = []
        klickbar = 0
        for i, lab in enumerate(sn["labels"]):
            f = sn["filter"][i]
            if not f:
                continue
            klickbar += 1
            val = sum(v for s_, t_, v in zip(sn["source"], sn["target"], sn["value"]) if t_ == i) or sum(
                v for s_, t_, v in zip(sn["source"], sn["target"], sn["value"]) if s_ == i
            )
            n, tot = drill("/transactions?%s&cat=%s" % (qs, f))
            if n == 0 or abs(abs(tot) - val) > 0.02:
                schlecht.append((lab[:34], n, tot, round(val, 2)))
        check("%s: all %d nodes agree" % (period, klickbar), not schlecht, schlecht)

    print("4) The aggregate node 'Ausgaben' shows exactly its children")
    h = c.get("/?from=2026-05&to=2026-07").get_data(as_text=True)
    sn = json.loads(re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', h, re.S).group(1))
    i = next(i for i, l in enumerate(sn["labels"]) if l.startswith("Ausgaben"))
    f = sn["filter"][i]
    check("filter is a category list, not a special word", f and f[0].isdigit(), f)
    check("net positive category NOT included", str(son) not in f.split(","), f)
    n, tot = drill("/transactions?from=2026-05&to=2026-07&cat=%s" % f)
    knoten = sum(v for s_, t_, v in zip(sn["source"], sn["target"], sn["value"]) if t_ == i)
    check("total of the list == node amount", abs(tot - knoten) < 0.02, (tot, knoten))
    d = c.get("/transactions?from=2026-05&to=2026-07&cat=%s" % f).get_data(as_text=True)
    check("refund on Sonstiges does not appear here", "Rueckzahlung" not in d)
    check("hint about multi-selection", "mehrere Kategorien" in d)

    print("5) Net positive category is clickable under 'Guthaben'")
    i = next(i for i, l in enumerate(sn["labels"]) if l.startswith("Sonstiges"))
    n, tot = drill("/transactions?from=2026-05&to=2026-07&cat=%s" % sn["filter"][i])
    check("both Sonstiges bookings", n == 2, n)
    check("net +380 received back", abs(tot + 380.0) < 0.01, tot)

    print("6) Old link cat=ausgaben still works")
    n, tot = drill("/transactions?from=2026-05&to=2026-07&cat=ausgaben")
    check("returns rows", n > 0, n)
    check("splits are included", abs(tot - (200 + 250 + 180 + 600 * 3 + 300 + 120 + 100 - 500)) < 0.01, tot)

    print("7) The tiles at the top link the same slice")
    h = c.get("/?from=2026-05&to=2026-07").get_data(as_text=True)

    # take the link the page really offers - via /api/period, which returns the
    # same tile links as the template (urls).
    tiles = c.get("/api/period?from=2026-05&to=2026-07").get_json()["tiles"]

    def kachel(titel):
        t = tiles[{"Ausgaben": "exp", "Einnahmen": "inc", "Saldo": "saldo"}[titel]]
        return t["url"], float(t["value"])

    url, kpi = kachel("Ausgaben")
    n, tot = drill(url)
    check("expenses tile == list", abs(tot - kpi) < 0.02, (url, tot, kpi))
    url, kpi = kachel("Einnahmen")
    n, tot = drill(url)
    check("income tile == list", abs(tot + kpi) < 0.02, (url, tot, kpi))

    print("8) Truncated list is marked as such")
    d = c.get("/transactions").get_data(as_text=True)
    check("row count is shown", re.search(r"\d+ Zeilen\.", d) is not None)
    finish()
