import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_matrix():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    # mark setup as done - otherwise every page redirects to /setup
    _c = m.db()
    m.set_setting(_c, "setup_done", "1")
    _c.commit()
    _c.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    def _add_fixed(con, pattern, cat_name, share=50, kw=""):
        r = con.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone()
        con.execute(
            "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES(?,?,?,?)",
            (pattern, r["id"] if r else None, share, kw),
        )

    con = m.db()
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    # rent cycle as in real life: 04.05., 28.05., 03.07., 28.07. -> June without, July twice
    for d, h in (("2026-05-04", "r1"), ("2026-05-28", "r2"), ("2026-07-03", "r3"), ("2026-07-28", "r4")):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id,effective_amount) VALUES('bank',?,'Hausverwaltung Muster KG','miete',
        -1000.0,'EUR',?,'active',?,-500.0)""",
            (d, h, woh),
        )
    _add_fixed(con, "Hausverwaltung Muster", "Wohnung", 50, "miete")
    con.commit()
    con.close()

    print("1) Smoothing spreads the rents over four months")
    con = m.db()
    fm = m.wohnung_fixed_display_months(con)
    con.close()
    mons = sorted(fm.values())
    check("four months, each exactly once", mons == ["2026-05", "2026-06", "2026-07", "2026-08"], mons)

    print("\n2) Matrix follows the smoothing, not the booking date")
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    import re

    # pull the "Wohnung" row out of the year matrix
    seg = html[html.find("Monat für Monat") :]
    row = re.search(r"Wohnung</a?>?.*?</tr>", seg, re.S)
    nums = re.findall(r">(\d+)<", row.group(0)) if row else []
    check("matrix row found", bool(row))
    check("no month with double rent (1000)", "1000" not in nums, nums)
    check("four months with 500 each", nums.count("500") == 4, nums)

    print("\n3) Drilldown shows the same as the cell")
    for mon in ("2026-06", "2026-08"):
        h = c.get(f"/transactions?month={mon}&cat={woh}").get_data(as_text=True)
        n_rows = h.count("Hausverwaltung Muster KG") - h.count(
            'data-rule="Hausverwaltung Muster KG"'
        )  # the rule suggestion in the select must not be counted
        check(f"{mon}: exactly one rent in the drilldown", n_rows == 1, n_rows)

    print("\n4) Regressions")
    for p in ("/", "/transactions", "/jahr", "/splitwise", "/rules", "/fixkosten"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
