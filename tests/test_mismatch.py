import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_mismatch():

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

    def sw(con, date, desc, cat, cost, bal, h, extra_more=None):
        e = {"cost": cost, "bal": bal, "sw_category": cat}
        if extra_more:
            e.update(extra_more)
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,extra) VALUES('splitwise',?,'wohnung',?,?,'EUR',?,'info',?)""",
            (date, desc, -abs(bal), h, json.dumps(e)),
        )

    con = m.db()
    # the bank needs an early booking so that tracking_start fits
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-02','Start','x',-1,'EUR','start','active')""")
    # rent: account 1000, Splitwise 900 -> WARNING
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-28','Hausverwaltung Muster KG','miete',-1000.00,'EUR','b1','active')""")
    sw(con, "2026-05-01", "miete", "Miete", 900.00, 450.00, "s1")
    # electricity: account 170.40, Splitwise 170.40 -> OK
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-10','WIEN ENERGIE Vertrieb','strom',-170.40,'EUR','b2','active')""")
    sw(con, "2026-05-10", "wien energie", "Elektrizität", 170.40, 85.20, "s2")
    # insurance: 28.55 vs 29.03 -> below the 1 EUR tolerance, NO warning
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-04','UNIQA Oesterreich','vers',-29.03,'EUR','b3','active')""")
    sw(con, "2026-05-01", "Versicherung", "Versicherung", 28.55, 14.27, "s3")
    # position WITHOUT fixed-cost category -> not checked at all
    sw(con, "2026-05-06", "spar einkauf", "Lebensmittel", 12.80, 6.40, "s4")
    _add_fixed(con, "Hausverwaltung Muster", "Wohnung", 50, "miete")
    _add_fixed(con, "WIEN ENERGIE", "Wohnung", 50, "wien energie")
    _add_fixed(con, "UNIQA", "Wohnung", 50, "versicherung")
    con.commit()
    con.close()

    print("1) Detection")
    con = m.db()
    mm = m.splitwise_amount_mismatches(con)
    con.close()
    w = mm.get("wohnung", [])
    check("exactly one warning", len(w) == 1, [(x["description"], x["cost"]) for x in w])
    check("it is the rent", w and w[0]["description"] == "miete", w)
    check("Splitwise amount 900", w and abs(w[0]["cost"] - 900) < 0.01, w)
    check("account amount 1000", w and abs(w[0]["bank_amount"] - 1000) < 0.01, w)
    check("difference +100", w and abs(w[0]["diff"] - 100) < 0.01, w and w[0]["diff"])
    check("matching amount produces NO warning", not any(x["description"] == "wien energie" for x in w))
    check("deviation below 1 EUR produces NO warning", not any(x["sw_category"] == "Versicherung" for x in w))
    check("non-fixed cost is ignored", not any("spar" in x["description"] for x in w))

    print("\n2) Display")
    html = c.get("/splitwise").get_data(as_text=True)
    check("red block appears", "Beträge prüfen" in html)
    check("shows both amounts", "900.00" in html and "1000.00" in html)
    check("looks-fine button present", "mm-ok" in html)

    print("\n3) Confirm and undo")
    tx = w[0]["id"]
    r = c.post(f"/splitwise/amount-ok/{tx}", data={"ajax": "1"})
    check("204", r.status_code == 204, r.status_code)
    con = m.db()
    mm2 = m.splitwise_amount_mismatches(con)
    con.close()
    check("counts as confirmed", mm2["wohnung"][0]["confirmed"] is True, mm2["wohnung"][0])
    con = m.db()
    e = json.loads(con.execute("SELECT extra FROM transactions WHERE id=?", (tx,)).fetchone()["extra"])
    con.close()
    check("cost unchanged", abs(float(e["cost"]) - 900) < 0.01, e)
    check("bal unchanged", abs(float(e["bal"]) - 450) < 0.01, e)
    check("flag set", e.get("amount_ok") is True, e)
    c.post(f"/splitwise/amount-ok/{tx}", data={"ajax": "1", "undo": "1"})
    con = m.db()
    mm3 = m.splitwise_amount_mismatches(con)
    con.close()
    check("undo works", mm3["wohnung"][0]["confirmed"] is False, mm3["wohnung"][0])

    print("\n4) Regressions")
    for p in ("/", "/transactions", "/partner", "/rules", "/fixkosten", "/jahr", "/splitwise"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
