import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_tabak():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    # mark setup as done - otherwise every page redirects to /setup
    _c = m.db()
    m.set_setting(_c, "setup_done", "1")
    m.set_setting(_c, "fixed_cost_group", "wohnung")
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

    _con = m.db()
    _add_fixed(_con, "Wien Energie", "Wohnung", 50, "wien energie")
    _add_fixed(_con, "Hausverwaltung Muster", "Wohnung", 50, "miete")
    _add_fixed(_con, "T-Mobile", "Wohnung", 50, "internet")
    _con.commit()
    print("1) Mapping: description beats category")
    check(
        "Tabak/Elektrizitaet -> Konsum",
        m.map_splitwise_category("Tabak") == "Konsum",
        m.map_splitwise_category("Tabak"),
    )
    # "wien energie" has no keyword of its own in the list - what matters is
    # that it counts as a fixed cost via the configured fixed-cost keyword.
    check("wien energie counts as a fixed-cost position", m.is_fixed_cost_position(_con, "wien energie"))
    # Splitwise categories are no longer consulted at all (user decision
    # 2026-09-11) - without a hit in rules/keyword list it stays uncategorized.
    check(
        "Splitwise category is ignored",
        m.splitwise_category_name(_con, "bier") is None,
        m.splitwise_category_name(_con, "bier"),
    )
    check(
        "description decides",
        m.splitwise_category_name(_con, "Tabak") == "Konsum",
        m.splitwise_category_name(_con, "Tabak"),
    )
    check("miete/Miete -> Wohnung", m.map_splitwise_category("miete") == "Wohnung")

    print("\n2) Fixed-cost detection")
    check("Tabak is NOT a fixed-cost position", not m.is_fixed_cost_position(_con, "Tabak"))
    check("wien energie IS one", m.is_fixed_cost_position(_con, "wien energie"))
    check("rent IS one", m.is_fixed_cost_position(_con, "miete"))
    check("internet IS one", m.is_fixed_cost_position(_con, "Internet"))
    check("groceries not affected at all", not m.is_fixed_cost_position(_con, "spar"))

    print("\n3) Promotion: Tabak now counts")
    con = m.db()
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-05-02','Start','x',-1,'EUR','st','active')""")
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-07-15','wohnung','Tabak',-4.00,'EUR','t1','info',?)""",
        (json.dumps({"cost": 8.0, "bal": -4.0, "sw_category": "Elektrizität"}),),
    )
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-07-10','wohnung','wien energie',-90.0,'EUR','t2','info',?)""",
        (json.dumps({"cost": 180.0, "bal": -90.0, "sw_category": "Elektrizität"}),),
    )
    con.commit()
    n = m.promote_splitwise_shares(con)
    con.commit()

    def row(h):
        return con.execute(
            "SELECT t.status, c.name AS cat FROM transactions t "
            "LEFT JOIN categories c ON c.id=t.category_id WHERE t.raw_hash=?",
            (h,),
        ).fetchone()

    check("Tabak was promoted", row("t1")["status"] == "active", dict(row("t1")))
    check("Tabak sits on Konsum", row("t1")["cat"] == "Konsum", dict(row("t1")))
    check("wien energie stays info (fixed cost)", row("t2")["status"] == "info", dict(row("t2")))
    con.close()

    print("\n4) Amount check no longer reports Tabak")
    con = m.db()
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-07-10','WIEN ENERGIE Vertrieb','strom',-170.40,'EUR','b1','active')""")
    con.commit()
    mm = m.splitwise_amount_mismatches(con)
    con.close()
    w = mm.get("wohnung", [])
    check("no hint for Tabak", not any("Tabak" in x["description"] for x in w), [x["description"] for x in w])
    check(
        "hint for wien energie stays",
        any("wien energie" in x["description"] for x in w),
        [x["description"] for x in w],
    )

    print("\n5) Regressions")
    for p in ("/", "/transactions", "/splitwise", "/jahr", "/rules", "/fixkosten"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
