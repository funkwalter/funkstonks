import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_rulesview():

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

    con = m.db()
    parent = con.execute("SELECT id,name FROM categories WHERE name='Restaurant/Ausgehen'").fetchone()
    if not parent:
        con.execute("INSERT INTO categories(name,color,kind) VALUES('Restaurant/Ausgehen','#888','expense')")
        parent = con.execute("SELECT id,name FROM categories WHERE name='Restaurant/Ausgehen'").fetchone()
    con.execute(
        "INSERT INTO categories(name,color,kind,parent_id) VALUES('Mittagessen Arbeit','#999','expense',?)",
        (parent["id"],),
    )
    child = con.execute("SELECT id FROM categories WHERE name='Mittagessen Arbeit'").fetchone()["id"]
    other = con.execute(
        "SELECT id FROM categories WHERE kind='expense' AND id NOT IN (?,?) LIMIT 1", (parent["id"], child)
    ).fetchone()["id"]

    # 4 active Radatz hits: 1x correct category, 2x other, 1x empty
    # + 1 excluded + 1 split -> must NOT be counted
    data = [
        ("RADATZ 24", "x", "active", child),
        ("RADATZ 24", "x", "active", other),
        ("RADATZ 24", "x", "active", other),
        ("RADATZ 24", "x", "active", None),
        ("RADATZ 24", "x", "excluded", other),
        ("RADATZ 24", "x", "active", None),
    ]
    for i, (p, d, st, cid) in enumerate(data):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
                   raw_hash,status,category_id) VALUES('bank','2026-08-01',?,?,-1,'EUR',?,?,?)""",
            (p, d, f"h{i}", st, cid),
        )
    sp = con.execute("SELECT id FROM transactions WHERE raw_hash='h5'").fetchone()["id"]
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,-1)", (sp, child))
    m.upsert_rule(con, "Radatz", child)
    con.commit()
    con.close()

    print("1) Hit count")
    html = c.get("/rules").get_data(as_text=True)
    check("page loads", "Radatz" in html)
    check(
        "shows 4 hits (excluded + split left out)",
        "(4×" in html,
        [l for l in html.split("\n") if "Radatz" in l][:2],
    )
    check("shows 3 deviating", "3 abweichend" in html, [l for l in html.split("\n") if "abweichend" in l][:2])

    print("\n2) Subcategory with parent name")
    check("parent name visible", "Restaurant/Ausgehen" in html and "Mittagessen Arbeit" in html)
    check("subcategory rendered indented", "border-left:3px solid" in html)
    # only the top-level category gets its own card with <h2>; the subcategory
    # is nested inside it, not a second card on the same level.
    import re as _re

    h2s = _re.findall(r'<h2 class="m-0">.*?</h2>', html, _re.S)
    check("no separate <h2> for the subcategory", not any("Mittagessen Arbeit" in x for x in h2s), h2s)
    check(
        "top-level category has exactly one card",
        sum(1 for x in h2s if "Restaurant/Ausgehen" in x) == 1,
        [x for x in h2s if "Restaurant" in x],
    )
    # the subcategory must be INSIDE the restaurant block
    i_parent = html.index("Restaurant/Ausgehen")
    i_sub = html.index("Mittagessen Arbeit", i_parent)
    i_next_card = html.find('<div class="card">', i_parent + 1)
    check(
        "subcategory lies within the block of the top-level category",
        i_next_card == -1 or i_sub < i_next_card,
        (i_sub, i_next_card),
    )

    print("\n3) Rule without hits shows a (0x) count")
    con = m.db()
    m.upsert_rule(con, "GIBTESNICHT", other)
    con.commit()
    con.close()
    html2 = c.get("/rules").get_data(as_text=True)
    check("(0x) count present", "(0×)" in html2)

    print("\n4) Number matches the retroactive preview")
    con = m.db()
    rid = con.execute("SELECT id FROM rules WHERE lower(pattern)='radatz'").fetchone()["id"]
    con.close()
    import json

    d = json.loads(c.post(f"/rules/{rid}/apply", data={"dry": "1"}).get_data(as_text=True))
    check("matched == displayed hit count", d["matched"] == 4, d)
    check("changed == displayed deviations", d["changed"] == 3, d)

    print("\n5) Other pages unchanged")
    for p in ("/", "/transactions", "/partner"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
