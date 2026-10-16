import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_ajax():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "test.db")  # redirect BEFORE init_db
    m.init_db()
    # mark setup as done - otherwise every page redirects to /setup
    _c = m.db()
    m.set_setting(_c, "setup_done", "1")
    _c.commit()
    _c.close()
    m.app.config["TESTING"] = True
    c = m.app.test_client()

    con = m.db()
    cat = con.execute("SELECT id FROM categories WHERE kind='expense' LIMIT 1").fetchone()["id"]
    cat2 = con.execute("SELECT id FROM categories WHERE kind='expense' AND id!=? LIMIT 1", (cat,)).fetchone()["id"]
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status)
               VALUES('bank','2026-08-20','TESTPARTNER GMBH','Testzweck',-12.34,'EUR','h-ajax-1','active')""")
    con.commit()
    tx = con.execute("SELECT id FROM transactions WHERE raw_hash='h-ajax-1'").fetchone()["id"]
    con.close()

    def get(col):
        con = m.db()
        r = con.execute(f"SELECT {col} FROM transactions WHERE id=?", (tx,)).fetchone()[col]
        con.close()
        return r

    print("1) Category via AJAX")
    r = c.post(f"/tx/{tx}/update", data={"category_id": str(cat), "ajax": "1"})
    check("204 instead of redirect", r.status_code == 204, r.status_code)
    check("category set", get("category_id") == cat, get("category_id"))

    print("2) Category without AJAX (old path still works)")
    r = c.post(f"/tx/{tx}/update", data={"category_id": str(cat2)})
    check("302 redirect", r.status_code == 302, r.status_code)
    check("category changed", get("category_id") == cat2, get("category_id"))

    print("3) Does-not-count toggle via AJAX")
    r = c.post(f"/tx/{tx}/update", data={"toggle": "1", "ajax": "1"})
    check("204", r.status_code == 204, r.status_code)
    check("status=excluded", get("status") == "excluded", get("status"))
    r = c.post(f"/tx/{tx}/update", data={"toggle": "1", "ajax": "1"})
    check("back to active", get("status") == "active", get("status"))

    print("4) Comment via AJAX (regression)")
    r = c.post(f"/tx/{tx}/update", data={"comment": "Notiz", "ajax": "1"})
    check("204", r.status_code == 204, r.status_code)
    check("comment saved", get("comment") == "Notiz", get("comment"))

    print("5) Remembering a rule still goes through a reload (no ajax)")
    con = m.db()
    con.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (tx,))
    con.commit()
    con.close()
    r = c.post(f"/tx/{tx}/update", data={"category_id": str(cat), "make_rule": "1"})
    check("302 redirect", r.status_code == 302, r.status_code)
    con = m.db()
    rules = con.execute("SELECT pattern,category_id FROM rules WHERE pattern='TESTPARTNER GMBH'").fetchall()
    con.close()
    check("rule created", len(rules) == 1 and rules[0]["category_id"] == cat, [dict(x) for x in rules])

    print("6) Pages still render")
    for path in ("/transactions", "/partner", "/"):
        r = c.get(path)
        check(f"GET {path} -> 200", r.status_code == 200, r.status_code)

    print("7) Markup contains the new hooks")
    html = c.get("/transactions").get_data(as_text=True)
    check("select.tx-cat present", 'class="tx-cat"' in html)
    check("no more onchange submit in rows", 'name="category_id" onchange' not in html)
    check("tx-toggle present", "tx-toggle" in html)
    finish()
