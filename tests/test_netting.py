"""Refunds booked into the same category must be netted EVERYWHERE - dashboard
table, year matrix, /jahr, drilldown.

Real case (2026-09-11): Sport in May showed 1085.89 in the year matrix instead
of 405.94 because only negative rows were counted there.
"""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_netting():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    life = con.execute("SELECT id FROM categories WHERE name='Lifestyle'").fetchone()["id"]
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Sport','#8B8','expense',?)", (life,))
    sid = con.execute("SELECT id FROM categories WHERE name='Sport'").fetchone()["id"]
    geh = con.execute("SELECT id FROM categories WHERE name='Gehalt'").fetchone()["id"]
    ges = con.execute("SELECT id FROM categories WHERE name='Gesundheit'").fetchone()["id"]
    # May: one big expense plus refunds from friends into the same category
    rows = [
        ("2026-05-28", "Andreas Fiala", -900.0, sid),
        ("2026-05-04", "PayPal Europe", -95.94, sid),
        ("2026-05-05", "Konstantin Zellner", 70.0, sid),
        ("2026-05-06", "Sebastian Penz", 70.0, sid),
        ("2026-05-07", "Lea Sapp", 50.0, sid),
        ("2026-05-15", "Gehalt AG", 2500.0, geh),
        # category WITHOUT subcategories - only there does /jahr expand payees
        ("2026-05-10", "Apotheke Wien", -120.0, ges),
        ("2026-05-20", "Apotheke Wien", 45.0, ges),
        ("2026-05-21", "Kasse Erstattung", 30.0, ges),
    ]
    for i, (d, p, a, cid) in enumerate(rows):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,?,'x',?,'EUR',?,'active',?)""",
            (d, p, a, "h%d" % i, cid),
        )
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    NET = 995.94 - 190.0  # 805.94

    print("1) summarize() nets (was already correct)")
    con = m.db()
    inc, exp = m.summarize(m.stats_transactions(con, month="2026-05"))
    con.close()
    check("Sport net", abs(exp["Sport"][0] - NET) < 0.01, exp.get("Sport"))
    check("Gesundheit net (120 - 45 - 30)", abs(exp["Gesundheit"][0] - 45.0) < 0.01, exp.get("Gesundheit"))

    print("2) Year matrix on the dashboard now nets too")
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    mrow = re.search(r"Sport</td>\s*(.*?)</tr>", html, re.S)
    check("Sport row present", bool(mrow))
    cells = re.findall(r">([+\-\d]+)</a>", mrow.group(1)) if mrow else []
    check("May cell shows the netted amount", cells and cells[0] == "806", cells)
    check("gross value 1086 no longer appears", "1086" not in (mrow.group(1) if mrow else ""))

    print("3) Cell and drilldown show the same bookings")
    d = c.get("/transactions?month=2026-05&cat=%d" % sid).get_data(as_text=True)
    check("drilldown shows expense AND refund", "Andreas Fiala" in d and "Konstantin Zellner" in d)

    print("4) /jahr: category row net (payee sub-rows no longer exist since 2026-09-15)")
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    body = html[html.index(">Gesundheit</a>") :]
    body = body[: body.index("</tr>")]
    am = re.search(r'<td class="num">(-?\d+\.\d\d) €</td>', body)
    check(
        "Gesundheit row net 45 (120 - 45 refund - 30 pure refunder)",
        am is not None and abs(float(am.group(1)) - 45.0) < 0.02,
        am.group(1) if am else body[:300],
    )
    sub_after = html[html.index(">Gesundheit</a>") :]
    sub_after = sub_after[: sub_after.index("</table>")]
    check(
        "no payee sub-rows under Gesundheit",
        'class="sub-' not in sub_after.split("<tr>", 2)[1]
        if sub_after.count("<tr>") >= 2
        else 'class="sub-' not in sub_after,
    )

    print("5) Category that nets out exactly disappears from the matrix")
    con = m.db()
    for i, (d_, a) in enumerate([("2026-06-01", -40.0), ("2026-06-02", 40.0)]):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'X','x',?,'EUR',?,'active',?)""",
            (d_, a, "q%d" % i, sid),
        )
    con.commit()
    con.close()
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    mrow = re.search(r"Sport</td>\s*(.*?)</tr>", html, re.S)
    juni = re.findall(r"month=2026-06[^>]*>([+\-\d]+)</a>", mrow.group(1)) if mrow else []
    check("June cell stays empty instead of showing 40", juni == [], juni)

    print("6) Month in which money came back net is shown green as a plus")
    con = m.db()
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,
    currency,raw_hash,status,category_id) VALUES('bank','2026-07-02','Z','x',300,'EUR','q3',
    'active',?)""",
        (sid,),
    )
    con.commit()
    con.close()
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    mrow = re.search(r"Sport</td>\s*(.*?)</tr>", html, re.S)
    check("July cell shows +300", mrow and "+300" in mrow.group(1), mrow.group(1)[:200] if mrow else None)
    check("July cell is green", mrow and 'class="num pos"' in mrow.group(1))

    print("7) Year total of the matrix == sum of the category table")
    con = m.db()
    txs = m.stats_transactions(con, year="2026")
    inc, exp = m.summarize(txs)
    con.close()
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    tbl = html[html.index("Monat für Monat") :]
    tbl = tbl[: tbl.index("</table>")]
    # count only the main rows - subcategories (sub rows) are already contained in
    # the total of their parent row.
    tbl_top = re.sub(r'<tr class="sub-\w+" hidden>.*?</tr>', "", tbl, flags=re.S)
    sums = [int(x) for x in re.findall(r"year=2026[^>]*>\s*<b>([+\-]?\d+)</b>", tbl_top)]
    # summarize() drops categories that are net positive - add them back for the
    # comparison here, since the matrix does show them (green).
    net_all = {}
    for t in txs:
        if t["cat_kind"] in ("income", "transfer"):
            continue
        net_all[t["cat_name"] or "Unkategorisiert"] = net_all.get(t["cat_name"] or "Unkategorisiert", 0) - m.eff(t)
    expected = sum(round(v) for v in net_all.values() if abs(v) >= 0.005)
    check("totals agree", abs(sum(sums) - expected) <= len(sums), (sum(sums), expected, sums))
    finish()
