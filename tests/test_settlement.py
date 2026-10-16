import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_settlement():

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
    cat = con.execute("SELECT id FROM categories WHERE name='Urlaub'").fetchone()["id"]
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-02','Start','x',-1,'EUR','st','active')""")
    # settlement payment: someone pays Anna -> bal negative
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-06-24','freunde','Markus W. zahlte Anna K.',
    -20.57,'EUR','p1','info',?)""",
        (json.dumps({"cost": 20.57, "bal": -20.57, "sw_category": "Zahlung", "is_payment": True}),),
    )
    # normal promoted position as the counter-check
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-06-25','freunde','pizza',-10.00,'EUR','p2','active',?)""",
        (json.dumps({"cost": 20.0, "bal": -10.0, "sw_category": "Essen"}),),
    )
    con.commit()
    pay = con.execute("SELECT id FROM transactions WHERE raw_hash='p1'").fetchone()["id"]
    exp = con.execute("SELECT id FROM transactions WHERE raw_hash='p2'").fetchone()["id"]
    con.close()

    print("1) Settlement payment cannot be categorized")
    r = c.post(f"/tx/{pay}/update", data={"category_id": str(cat), "ajax": "1"})
    check("response without error", r.status_code in (204, 302), r.status_code)
    con = m.db()
    row = con.execute("SELECT status, category_id FROM transactions WHERE id=?", (pay,)).fetchone()
    con.close()
    check("status stays 'info'", row["status"] == "info", row["status"])
    check("no category set", row["category_id"] is None, row["category_id"])

    print("\n2) Normal position stays categorizable")
    c.post(f"/tx/{exp}/update", data={"category_id": str(cat), "ajax": "1"})
    con = m.db()
    row = con.execute("SELECT status, category_id FROM transactions WHERE id=?", (exp,)).fetchone()
    con.close()
    check("category set", row["category_id"] == cat, row["category_id"])

    print("\n3) Display on /splitwise")
    html = c.get("/splitwise").get_data(as_text=True)
    check("hint instead of dropdown", "gleicht Salden aus" in html)
    check("no dropdown for the payment", f'data-tx-id="{pay}"' not in html)
    check("dropdown for the normal position", f'data-tx-id="{exp}"' in html)

    print("\n4) Does the payment count in the statistics?")
    con = m.db()
    txs = m.stats_transactions(con)
    con.close()
    check("settlement payment is dropped", not any(t["id"] == pay for t in txs))

    print("\n5) Regressions")
    for p in ("/", "/transactions", "/partner", "/rules", "/fixkosten", "/jahr", "/splitwise"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
