"""Splitwise: categories are ignored, and deleted positions disappear with
the next export."""

import io as _io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_swsync():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    m.set_setting(con, "sources", '["bank","splitwise"]')
    m.set_setting(con, "splitwise_name", "Anna Muster")
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    leb = con.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()["id"]
    con.execute("INSERT INTO rules(pattern,category_id) VALUES('Spar',?)", (leb,))
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'miete')",
        (woh,),
    )
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-06-01','Start','x',-1,'EUR','st','active')""")
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    HEAD = "Datum,Beschreibung,Kategorien,Kosten,Waehrung,Anna Muster,Bob\n"

    def csv(rows):
        return (HEAD + "".join(rows)).encode("utf-8")

    R_MIETE = "2026-06-01,miete,Miete,900.00,EUR,450.00,-450.00\n"
    R_BIER = "2026-06-05,bier,Alkohol,20.00,EUR,-10.00,10.00\n"
    R_SPAR = "2026-06-07,Spar,Lebensmittel,30.00,EUR,-15.00,15.00\n"
    R_KRAZY = "2026-06-09,krazy,Restaurant,40.00,EUR,-20.00,20.00\n"

    print("1) Splitwise category is ignored")
    con = m.db()
    check(
        "'bier' unassigned despite category Alkohol",
        m.splitwise_category_name(con, "bier") is None,
        m.splitwise_category_name(con, "bier"),
    )
    check(
        "'Spar' matches via the own rule",
        m.splitwise_category_name(con, "Spar") == "Lebensmittel",
        m.splitwise_category_name(con, "Spar"),
    )
    check("'Tabak' via the keyword list", m.splitwise_category_name(con, "Tabak") == "Konsum")
    check(
        "fixed costs solely via the description",
        m.is_fixed_cost_position(con, "miete") and not m.is_fixed_cost_position(con, "bier"),
    )
    con.close()

    print("\n2) Import and promotion")
    new, skipped, ids, removed = m.import_splitwise(_io.BytesIO(csv([R_MIETE, R_BIER, R_SPAR, R_KRAZY])), "wg")
    check("four rows imported", new == 4, (new, skipped, removed))
    con = m.db()
    n = m.promote_splitwise_shares(con)
    con.commit()
    rows = {
        r["description"]: r
        for r in con.execute(
            "SELECT t.description, t.status, c.name AS cat FROM transactions t "
            "LEFT JOIN categories c ON c.id=t.category_id WHERE t.source='splitwise'"
        )
    }
    check("miete stays info (fixed cost)", rows["miete"]["status"] == "info", dict(rows["miete"]))
    check(
        "Spar promoted to Lebensmittel",
        rows["Spar"]["status"] == "active" and rows["Spar"]["cat"] == "Lebensmittel",
        dict(rows["Spar"]),
    )
    check(
        "bier promoted, but WITHOUT category",
        rows["bier"]["status"] == "active" and rows["bier"]["cat"] is None,
        dict(rows["bier"]),
    )
    check("krazy also without category", rows["krazy"]["cat"] is None, dict(rows["krazy"]))
    check("thus ends up in review", m.todo_count(con) >= 2, m.todo_count(con))
    con.close()

    print("\n3) Deleted position disappears with the next export")
    new, skipped, ids, removed = m.import_splitwise(_io.BytesIO(csv([R_MIETE, R_SPAR, R_KRAZY])), "wg")
    check("nothing new, three known", (new, skipped) == (0, 3), (new, skipped))
    check("one position removed", removed == 1, removed)
    con = m.db()
    left = [r["description"] for r in con.execute("SELECT description FROM transactions WHERE source='splitwise'")]
    check("'bier' is gone", "bier" not in left, left)
    check("the others are there", set(left) == {"miete", "Spar", "krazy"}, left)
    con.close()

    print("\n4) Changed amount: old row disappears, new one arrives")
    R_SPAR_NEU = "2026-06-07,Spar,Lebensmittel,36.00,EUR,-18.00,18.00\n"
    new, skipped, ids, removed = m.import_splitwise(_io.BytesIO(csv([R_MIETE, R_SPAR_NEU, R_KRAZY])), "wg")
    check("one new row", new == 1, new)
    check("the old one removed", removed == 1, removed)
    con = m.db()
    r = con.execute("SELECT extra FROM transactions WHERE source='splitwise' AND description='Spar'").fetchall()
    check("only ONE Spar row left", len(r) == 1, len(r))
    check(
        "with the new amount", abs(json.loads(r[0]["extra"])["cost"] - 36.0) < 0.01, json.loads(r[0]["extra"])["cost"]
    )
    con.close()

    print("\n5) What must NOT be deleted")
    con = m.db()
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-06-06','wg','Saldo-Korrektur',5.0,'EUR',
    'manual1','excluded',?)""",
        (json.dumps({"cost": 5, "bal": 5, "manual": True}),),
    )
    # other group, same period
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2026-06-06','reise','pizza',-9.0,'EUR',
    'other1','info',?)""",
        (json.dumps({"cost": 18, "bal": -9}),),
    )
    # outside the imported period
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,extra) VALUES('splitwise','2025-01-01','wg','alt',-3.0,'EUR',
    'old1','info',?)""",
        (json.dumps({"cost": 6, "bal": -3}),),
    )
    con.commit()
    con.close()
    new, skipped, ids, removed = m.import_splitwise(_io.BytesIO(csv([R_MIETE, R_SPAR_NEU, R_KRAZY])), "wg")
    con = m.db()
    have = {r["raw_hash"] for r in con.execute("SELECT raw_hash FROM transactions WHERE source='splitwise'")}
    check("balance correction stays", "manual1" in have)
    check("other group stays", "other1" in have)
    check("older period stays untouched", "old1" in have)
    con.close()

    print("\n6) Link is cleanly undone on deletion")
    con = m.db()
    bank = con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,
    currency,raw_hash,status,effective_amount,note) VALUES('bank','2026-06-09','Lokal','x',
    -40.0,'EUR','bk1','active',-20.0,'verknuepft')""")
    bid = con.execute("SELECT id FROM transactions WHERE raw_hash='bk1'").fetchone()["id"]
    sid = con.execute("SELECT id FROM transactions WHERE source='splitwise' AND description='krazy'").fetchone()["id"]
    con.execute("INSERT INTO matches(bank_tx,other_tx,kind) VALUES(?,?,'splitwise_share')", (bid, sid))
    con.commit()
    con.close()
    new, skipped, ids, removed = m.import_splitwise(_io.BytesIO(csv([R_MIETE, R_SPAR_NEU])), "wg")
    check("krazy removed", removed == 1, removed)
    con = m.db()
    b = con.execute("SELECT effective_amount, note FROM transactions WHERE id=?", (bid,)).fetchone()
    check("bank booking back to the full amount", b["effective_amount"] is None, b["effective_amount"])
    check("note reset", (b["note"] or "") == "", b["note"])
    check("link deleted", con.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0)
    con.close()

    print("\n7) Pages still load")
    for p in ("/", "/splitwise", "/transactions", "/jahr", "/review", "/fixkosten"):
        check(f"GET {p}", c.get(p).status_code == 200, c.get(p).status_code)
    finish()
