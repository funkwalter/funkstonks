import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_three():

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
    urlaub = con.execute("SELECT id FROM categories WHERE name='Urlaub'").fetchone()["id"]
    transport = con.execute("SELECT id FROM categories WHERE name LIKE 'Transport%'").fetchone()["id"]

    # --- A) vacation protection -------------------------------------------------
    con.execute("INSERT INTO urlaub_groups(partner) VALUES('festival')")
    # Splitwise position of the vacation group, labelled "uber", sits on Urlaub
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
               raw_hash,status,category_id) VALUES('splitwise','2026-07-01','festival','uber taxi',
               -20,'EUR','u1','active',?)""",
        (urlaub,),
    )
    # Splitwise position of a group that is NOT marked, also "uber"
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
               raw_hash,status,category_id) VALUES('splitwise','2026-07-02','freunde','uber taxi',
               -20,'EUR','u2','active',?)""",
        (urlaub,),
    )
    # normal bank booking "uber"
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
               raw_hash,status,category_id) VALUES('bank','2026-07-03','UBER *TRIP','x',
               -20,'EUR','u3','active',NULL)""")
    m.upsert_rule(con, "uber", transport)
    rid = con.execute("SELECT id FROM rules WHERE lower(pattern)='uber'").fetchone()["id"]
    con.commit()
    con.close()

    print("A) Vacation group is immune to rules")
    d = json.loads(c.post(f"/rules/{rid}/apply", data={"dry": "1"}).get_data(as_text=True))
    check("preview does NOT count the vacation group", d["matched"] == 2, d)
    c.post(f"/rules/{rid}/apply", data={})
    con = m.db()

    def cat(h):
        return con.execute("SELECT category_id FROM transactions WHERE raw_hash=?", (h,)).fetchone()["category_id"]

    check("festival position stays Urlaub", cat("u1") == urlaub, cat("u1"))
    check("freunde position is recategorized", cat("u2") == transport, cat("u2"))
    check("bank booking is recategorized", cat("u3") == transport, cat("u3"))
    # the normal import path must not touch it either
    con.execute("UPDATE transactions SET category_id=NULL WHERE raw_hash='u1'")
    con.commit()
    m.apply_rules(con)
    con.commit()
    check("apply_rules skips the vacation group even with an empty category", cat("u1") is None, cat("u1"))
    con.close()

    # (section B removed: the hide feature was dropped again at the user's request
    #  once /fixkosten filters strictly on Abos/Investitionen. test_fk.py covers
    #  that now.)

    # --- C) year view with subcategories ----------------------------------------
    print("\nC) Year view shows subcategories indented")
    con = m.db()
    parent = con.execute("SELECT id FROM categories WHERE name='Restaurant/Ausgehen'").fetchone()
    if not parent:
        con.execute("INSERT INTO categories(name,color,kind) VALUES('Restaurant/Ausgehen','#888','expense')")
        parent = con.execute("SELECT id FROM categories WHERE name='Restaurant/Ausgehen'").fetchone()
    con.execute(
        "INSERT INTO categories(name,color,kind,parent_id) VALUES('Mittagessen Arbeit','#999','expense',?)",
        (parent["id"],),
    )
    child = con.execute("SELECT id FROM categories WHERE name='Mittagessen Arbeit'").fetchone()["id"]
    for i, (cid, amt) in enumerate([(parent["id"], -50), (child, -30), (child, -20)]):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
                   raw_hash,status,category_id) VALUES('bank','2026-07-05','X','y',?,'EUR',?,'active',?)""",
            (amt, f"j{i}", cid),
        )
    con.commit()
    con.close()
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    check("year page loads", "Mittagessen Arbeit" in html)
    check("subcategory indented", "pl-34" in html)
    i_par = html.index("Restaurant/Ausgehen")
    check("subcategory comes AFTER the parent row", html.index("Mittagessen Arbeit", i_par) > i_par)
    # parent row = total (100), below it indented both components:
    # "Mittagessen Arbeit" (50) and the direct bookings on the parent category (50).
    # Exactly the same rendering as in the dashboard table.
    check("parent row shows the total 100.00", "100.00" in html.replace(",", "."), None)
    # since 2026-09-01 the year view also renders payee rows with the same
    # indentation - a plain row count therefore says nothing any more.
    # Instead we check that BOTH subcategory rows are there.
    check("subcategory row present", "Mittagessen Arbeit" in html)
    check("row for direct bookings of the parent category", "ohne Unterkategorie" in html)
    check(
        "direct bookings of the parent category as their own child row",
        html.count("Restaurant/Ausgehen") >= 2,
        html.count("Restaurant/Ausgehen"),
    )

    print("\nD) Regressions")
    for p in ("/", "/transactions", "/partner", "/rules", "/fixkosten", "/jahr"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
