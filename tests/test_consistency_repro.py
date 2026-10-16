"""Minimal deterministic repros for the findings of the consistency fuzz suite."""

import html as htmlmod
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import finish  # noqa: E402

import app as m


def test_consistency_repro():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    con.commit()

    def cid(n):
        return con.execute("SELECT id FROM categories WHERE name=?", (n,)).fetchone()["id"]

    woh, geh, leb, inv = cid("Wohnung"), cid("Gehalt"), cid("Lebensmittel"), cid("Investitionen")
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('ETF','#123','expense',?)", (inv,))
    etf = cid("ETF")
    n = [0]

    def add(d, p, a, cat, eff=None, src="bank", status="active"):
        n[0] += 1
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id,effective_amount) VALUES(?,?,?,'x',?,'EUR',?,?,?,?)""",
            (src, d, p, a, "r%d" % n[0], status, cat, eff),
        )
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    for mon in ("2025-11", "2025-12", "2026-01", "2026-02"):
        add(mon + "-15", "Arbeitgeber", 3000.0, geh)
        add(mon + "-10", "Supermarkt", -200.0, leb)
    # rent: 28.11., 02.01. (Dec slot), 28.01., 28.02.  -> slots Nov, Dec, Jan, Feb
    for d in ("2025-11-28", "2026-01-02", "2026-01-28", "2026-02-28"):
        add(d, "Hausverwaltung KG", -1000.0, woh, -500.0)
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'')", (woh,)
    )
    # single-booking fixed partner
    add("2026-01-20", "Energie AG", -120.0, woh, -60.0)
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Energie',?,50,'')", (woh,)
    )
    # Investitionen + sub
    add("2026-01-05", "Broker", -300.0, inv)
    add("2026-01-06", "Broker", -100.0, etf)
    # uncategorized: one negative, one positive
    add("2026-01-07", "Unbekannt", -50.0, None)
    add("2026-01-08", "Unbekannt", 80.0, None)
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    ROW = re.compile(r'<tr class="([^"]*)">(.*?)</tr>', re.S)
    AMT = re.compile(r'<td class="num (?:neg|pos)">(-?[\d.]+)</td>\s*<td class="num">(?:<b>(-?[\d.]+)</b>)?</td>', re.S)

    def drill(url):
        d = c.get(url).get_data(as_text=True)
        tot = 0.0
        names = []
        for st, body in ROW.findall(d):
            am = AMT.search(body)
            if not am or "dim" in st:
                continue
            sh = re.search(r"davon\s*(-?[\d.]+) € hier", body)
            tot += float(sh.group(1)) if sh else (float(am.group(2)) if am.group(2) else float(am.group(1)))
            names.append(re.search(r"<b>(.*?)</b>", body).group(1))
        return round(tot, 2), names

    print("A) Month-by-month: parent row with subcategories links cat=none")
    h = c.get("/jahr?y=2026").get_data(as_text=True)
    seg = h[h.index("Monat für Monat") :]
    row = re.search(r"<tr>\s*<td[^>]*>.*?Investitionen</td>(.*?)</tr>", seg, re.S).group(1)
    print("   Investitionen row links:", sorted(set(re.findall(r'href="([^"]+)"', row))))

    print("B) Sankey node Investitionen contains the subcategory, the filter does not")
    h = c.get("/?month=2026-01").get_data(as_text=True)
    import json

    S = json.loads(re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', h, re.S).group(1))
    for lab, f in zip(S["labels"], S["filter"]):
        if lab.startswith("Investitionen") or lab.startswith("Ausgaben") or lab.startswith("Unkategorisiert"):
            print("   %-40s filter=%-14s list=%s" % (lab, f, drill("/transactions?month=2026-01&cat=%s" % f)[0]))

    print("C) Tiles January 2026")
    for t in ("Einnahmen", "Ausgaben"):
        mo = re.search(
            r'<div class="kpi"><span>' + t + r'[^<]*</span>\s*<a href="([^"]+)"[^>]*>\s*<b[^>]*>\s*(-?[\d.]+) €', h
        )
        url = htmlmod.unescape(mo.group(1))
        print("   %s %s -> %s  list=%s" % (t, mo.group(2), url, drill(url)))

    print("D) Fixed-cost partner with only ONE booking in the month filter")
    print("   /transactions?month=2026-01&cat=Wohnung:", drill("/transactions?month=2026-01&cat=%d" % woh))
    print("   /transactions?q=Energie:", drill("/transactions?q=Energie"))

    print("E) Year boundary: rent 02.02.2026 (slot Dec 2025)")
    for y in ("2025", "2026"):
        h = c.get("/jahr?y=" + y).get_data(as_text=True)
        seg = h[h.index("Monat für Monat") :]
        seg = seg[: seg.index("</table>")]
        row = re.search(r"Wohnung</td>(.*?)</tr>", seg, re.S).group(1)
        print("   %s matrix Wohnung:" % y, re.findall(r">([+\-\d·]+)</a>|(·)</td>", row))
        tbl = h[h.index("Was kostet") :]
        tbl = tbl[: tbl.index("</table>")]
        print(
            "   %s 'Was kostet' Wohnung:" % y,
            re.search(r"Wohnung</a>\s*</td>\s*<td class=\"num\">([\d.]+) €", tbl).group(1),
            " year drilldown:",
            drill("/transactions?year=%s&cat=%d" % (y, woh)),
        )
    print("   /transactions?month=2025-12&cat=Wohnung:", drill("/transactions?month=2025-12&cat=%d" % woh))

    print("F) Month without income -> income tile")
    con = m.db()
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) VALUES('bank','2026-03-03','X','x',-10,'EUR','zz','active',?)",
        (leb,),
    )
    con.commit()
    con.close()
    h = c.get("/?month=2026-03").get_data(as_text=True)
    mo = re.search(r'<div class="kpi"><span>Einnahmen[^<]*</span>\s*<a href="([^"]+)"', h)
    print("   link:", htmlmod.unescape(mo.group(1)), "list:", drill(htmlmod.unescape(mo.group(1))))

    print("G) Adversarial")
    for u in ("/?from=abc&to=xyz", "/transactions?month=abc", "/?range=last0"):
        try:
            r = c.get(u)
            print(
                "   %s -> %s" % (u, r.status_code),
                re.search(r"(\d+) Monate</span>", r.get_data(as_text=True))
                and re.search(r"(\d+) Monate</span>", r.get_data(as_text=True)).group(0),
            )
        except Exception as e:
            print("   %s -> EXCEPTION %r" % (u, e))

    print("H) /jahr KPI tiles (kind=)")
    con = m.db()
    umb = cid("Umbuchung")
    urlaub = cid("Urlaub")
    # refund > cost in Urlaub (net positive), and a split with a transfer part
    add("2026-02-03", "Reise", -100.0, urlaub)
    add("2026-02-04", "Airline Refund", 400.0, urlaub)
    sid = add("2026-02-05", "Bankomat", -100.0, None)
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (sid, leb, -70.0))
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (sid, umb, -30.0))
    con.commit()
    con.close()
    h = c.get("/jahr?y=2026").get_data(as_text=True)
    for t in ("Einnahmen", "Ausgaben"):
        mo = re.search(
            r'<div class="kpi"><span>' + t + r'[^<]*</span>\s*<a href="([^"]+)"[^>]*>\s*<b[^>]*>\s*(-?[\d.]+) €', h
        )
        url = htmlmod.unescape(mo.group(1))
        print("   %s %s -> %s  list=%s" % (t, mo.group(2), url, drill(url)))
    finish()
