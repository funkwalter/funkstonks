import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_rules():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "test.db")
    m.init_db()
    # mark setup as done - otherwise every page redirects to /setup
    _c = m.db()
    m.set_setting(_c, "setup_done", "1")
    _c.commit()
    _c.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "test"
    c = m.app.test_client()

    print("1) normalize_pattern")
    check("*Radatz* -> Radatz", m.normalize_pattern("*Radatz*") == "Radatz", m.normalize_pattern("*Radatz*"))
    check("'  *x*  ' -> x", m.normalize_pattern("  *x*  ") == "x")
    check("normal text unchanged", m.normalize_pattern("BILLA DANKT") == "BILLA DANKT")

    con = m.db()
    cA = con.execute("SELECT id FROM categories WHERE kind='expense' LIMIT 1").fetchone()["id"]
    cB = con.execute("SELECT id FROM categories WHERE kind='expense' AND id!=? LIMIT 1", (cA,)).fetchone()["id"]

    # test data: 3 active Radatz bookings (one already on cB), 1 excluded, 1 split
    rows = [
        ("RADATZ FLEISCHEREI 24", "RADATZ 1010 K1 01.08.", -5.0, "active", None, "h1"),
        ("RADATZ FLEISCHEREI 24", "RADATZ 1010 K1 02.08.", -6.0, "active", cB, "h2"),
        ("RADATZ FLEISCHEREI 12", "RADATZ 1090 K1 03.08.", -7.0, "active", None, "h3"),
        ("RADATZ FLEISCHEREI 24", "RADATZ 1010 K1 04.08.", -8.0, "excluded", cB, "h4"),
        ("RADATZ FLEISCHEREI 24", "RADATZ 1010 K1 05.08.", -9.0, "active", None, "h5"),
    ]
    for p, d, a, st, cid, h in rows:
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
                   currency,raw_hash,status,category_id) VALUES('bank','2026-08-01',?,?,?,'EUR',?,?,?)""",
            (p, d, a, h, st, cid),
        )
    split_tx = con.execute("SELECT id FROM transactions WHERE raw_hash='h5'").fetchone()["id"]
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (split_tx, cA, -9.0))
    con.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (split_tx,))
    con.commit()
    con.close()

    print("\n2) upsert_rule instead of duplicate")
    con = m.db()
    m.upsert_rule(con, "*Radatz*", cA)
    m.upsert_rule(con, "radatz", cB)  # same pattern, different spelling
    con.commit()
    rs = con.execute("SELECT id,pattern,category_id FROM rules WHERE lower(pattern)='radatz'").fetchall()
    check("only ONE Radatz rule", len(rs) == 1, [dict(x) for x in rs])
    check("pattern stored without asterisks", rs and rs[0]["pattern"] == "Radatz", rs and rs[0]["pattern"])
    check("category updated instead of shadowed", rs and rs[0]["category_id"] == cB)
    rule_id = rs[0]["id"]
    con.execute("UPDATE rules SET category_id=? WHERE id=?", (cA, rule_id))
    con.commit()
    con.close()

    print("\n3) Preview (dry) changes nothing")
    r = c.post(f"/rules/{rule_id}/apply", data={"dry": "1"})
    d = json.loads(r.get_data(as_text=True))
    check("status 200", r.status_code == 200, r.status_code)
    check("3 active hits (excluded + split left out)", d["matched"] == 3, d)
    check("all 3 to change (h1 empty, h2 other cat, h3 empty)", d["changed"] == 3, d)
    con = m.db()
    unchanged = con.execute("SELECT COUNT(*) n FROM transactions WHERE category_id=?", (cA,)).fetchone()["n"]
    con.close()
    check("nothing written yet", unchanged == 0, unchanged)

    print("\n4) Applying really writes")
    r = c.post(f"/rules/{rule_id}/apply", data={})
    d = json.loads(r.get_data(as_text=True))
    check("3 changed reported", d["changed"] == 3, d)
    con = m.db()

    def cat(h):
        return con.execute("SELECT category_id FROM transactions WHERE raw_hash=?", (h,)).fetchone()["category_id"]

    check("h1 (was empty) -> cA", cat("h1") == cA, cat("h1"))
    check("h2 (was cB) -> cA overwritten", cat("h2") == cA, cat("h2"))
    check("h3 (other branch name) -> cA", cat("h3") == cA, cat("h3"))
    check("h4 EXCLUDED untouched", cat("h4") == cB, cat("h4"))
    check("h5 SPLIT stays NULL", cat("h5") is None, cat("h5"))
    check(
        "splits intact",
        con.execute("SELECT COUNT(*) n FROM splits WHERE tx_id=?", (split_tx,)).fetchone()["n"] == 1,
    )
    con.close()

    print("\n5) No more automatic backup (since 2026-09-15 only via /sicherung)")
    bdir = os.path.join(tmp, "backups")
    check(
        "no backups/ from applying a rule",
        not os.path.isdir(bdir) or not os.listdir(bdir),
        os.listdir(bdir) if os.path.isdir(bdir) else "fehlt",
    )

    print("\n6) Create rule via /rules with asterisks")
    c.post("/rules", data={"pattern": "*Billa*, *Hofer*", "category_id": str(cB)})
    con = m.db()
    rs = con.execute("SELECT pattern,category_id FROM rules WHERE lower(pattern) IN ('billa','hofer')").fetchall()
    n_billa = con.execute("SELECT COUNT(*) n FROM rules WHERE lower(pattern)='billa'").fetchone()["n"]
    con.close()
    check(
        "both patterns present, without asterisks",
        sorted(x["pattern"].lower() for x in rs) == ["billa", "hofer"],
        [x["pattern"] for x in rs],
    )
    check("existing BILLA default rule updated instead of duplicated", n_billa == 1, n_billa)
    check(
        "BILLA now points to the new category",
        all(x["category_id"] == cB for x in rs),
        [(x["pattern"], x["category_id"]) for x in rs],
    )

    print("\n7) Pages render")
    for p in ("/rules", "/transactions", "/partner", "/"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
