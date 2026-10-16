import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_urlaub_pay():

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
    lm = con.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()["id"]
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-02','Start','x',-1,'EUR','st','active')""")
    # promoted expense of the group
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id,extra) VALUES('splitwise','2026-06-01','trip','pizza',-10,'EUR','e1','active',?,?)""",
        (lm, json.dumps({"cost": 20, "bal": -10, "sw_category": "Essen"})),
    )
    # settlement payment of the SAME group, info
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-06-24','trip','X zahlte Anna K.',-50,'EUR','p1','info',?)""",
        (json.dumps({"cost": 50, "bal": -50, "sw_category": "Zahlung", "is_payment": True}),),
    )
    # and a settlement payment that is wrongly active already (legacy data)
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id,extra) VALUES('splitwise','2026-06-25','trip','Y zahlte Anna K.',-70,'EUR','p2','active',?,?)""",
        (lm, json.dumps({"cost": 70, "bal": -70, "sw_category": "Zahlung", "is_payment": True})),
    )
    con.commit()
    con.close()

    def get(h, col="category_id"):
        con = m.db()
        v = con.execute(f"SELECT {col} FROM transactions WHERE raw_hash=?", (h,)).fetchone()[col]
        con.close()
        return v

    print("1) Mark group as vacation")
    r = c.post("/splitwise/trip/urlaub", data={"on": "1"})
    check("response ok", r.status_code in (200, 204, 302), r.status_code)
    con = m.db()
    url = con.execute("SELECT id FROM categories WHERE name='Urlaub'").fetchone()["id"]
    con.close()
    check("normal expense becomes vacation", get("e1") == url, get("e1"))
    check(
        "info settlement payment stays untouched",
        get("p1") is None and get("p1", "status") == "info",
        (get("p1"), get("p1", "status")),
    )
    check("active settlement payment is NOT set to vacation", get("p2") != url, get("p2"))

    print("\n2) Untick vacation again")
    c.post("/splitwise/trip/urlaub", data={})
    check("info settlement payment still untouched", get("p1") is None and get("p1", "status") == "info")
    check("active settlement payment still not vacation", get("p2") != url, get("p2"))

    print("\n3) Rules do not touch the vacation group")
    con = m.db()
    m.upsert_rule(con, "zahlte", lm)
    con.commit()
    rid = con.execute("SELECT id FROM rules WHERE lower(pattern)='zahlte'").fetchone()["id"]
    con.close()
    c.post("/splitwise/trip/urlaub", data={"on": "1"})
    import json as _j

    d = _j.loads(c.post(f"/rules/{rid}/apply", data={"dry": "1"}).get_data(as_text=True))
    check("rule matches no row of the vacation group", d["matched"] == 0, d)

    print("\n4) Regressions")
    for p in ("/", "/transactions", "/splitwise", "/jahr", "/rules", "/fixkosten"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
