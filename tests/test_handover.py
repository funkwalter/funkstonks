"""Gaps that only surfaced while preparing the handover (documentation review
2026-09-11):
  * "Investitionen" was missing from the defaults although the code looks the
    branch up by name
  * the fixed cost group "wohnung" was hard-wired -> now a setting
  * fixed costs with a 100 % share never appeared under recurring costs
  * a hidden bank debit without receipt (receipt deleted before the cutoff
    date) stayed invisible forever
"""

import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_handover():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    con = m.db()
    m.set_setting(con, "setup_done", "1")
    m.set_setting(con, "sources", '["bank","paypal","splitwise"]')
    m.set_setting(con, "splitwise_name", "Anna Muster")

    def cid(name):
        r = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        return r["id"] if r else None

    print("1) Fresh DB knows 'Investitionen'")
    check("category present", cid("Investitionen") is not None)
    check(
        "as expense",
        con.execute("SELECT kind FROM categories WHERE name='Investitionen'").fetchone()["kind"] == "expense",
    )
    check("'Sparen/Investment' no longer in the defaults", cid("Sparen/Investment") is None)
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id) VALUES('bank','2026-06-10','Broker','ETF',-100,'EUR','inv1','active',?)""",
        (cid("Investitionen"),),
    )
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id) VALUES('bank','2026-06-15','Arbeitgeber','Gehalt',3000,'EUR','g1','active',?)""",
        (cid("Gehalt"),),
    )
    con.commit()
    h = c.get("/?month=2026-06").get_data(as_text=True)
    sn = json.loads(re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', h, re.S).group(1))
    check("Investitionen branch in the chart", any(l.startswith("Investitionen") for l in sn["labels"]), sn["labels"])

    print("2) Fixed cost group is a setting")
    woh = cid("Wohnung")
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'miete')",
        (woh,),
    )
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-06-01','Hausverwaltung','Miete',-900,'EUR','m1','active')""")
    con.commit()

    def sw_row(group, desc, key):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,extra) VALUES('splitwise','2026-06-02',?,?,-450,'EUR',?,'info',?)""",
            (group, desc, key, json.dumps({"cost": 900, "bal": -450, "members": ["Bob"]})),
        )

    sw_row("WG", "miete", "s1")
    con.commit()
    check("without setting: no group counts as fixed cost group", not m.is_fixed_cost_group(con, "WG"))
    n = m.promote_splitwise_shares(con)
    con.commit()
    st = con.execute("SELECT status FROM transactions WHERE raw_hash='s1'").fetchone()["status"]
    check("rent in group 'WG' is promoted WITHOUT the setting (double counted!)", st == "active", st)
    # reset and try again with the setting
    con.execute("UPDATE transactions SET status='info', category_id=NULL, note='' WHERE raw_hash='s1'")
    m.set_setting(con, "fixed_cost_group", "wg")
    con.commit()
    check("setting is case-insensitive", m.is_fixed_cost_group(con, "WG"))
    m.promote_splitwise_shares(con)
    con.commit()
    st = con.execute("SELECT status FROM transactions WHERE raw_hash='s1'").fetchone()["status"]
    check("with the setting the rent stays info (counts via the reduced bank booking)", st == "info", st)
    h = c.get("/einstellungen").get_data(as_text=True)
    check("field in the settings", 'name="fixed_cost_group"' in h and 'value="wg"' in h)
    h = c.get("/setup").get_data(as_text=True)
    check("field in the setup", 'name="fixed_cost_group"' in h)

    print("3) Migration v2 fills in 'wohnung' when the group exists")
    tmp2 = tempfile.mkdtemp()
    p2 = os.path.join(tmp2, "alt.db")
    import sqlite3

    old = sqlite3.connect(p2)
    old.executescript("""CREATE TABLE transactions(id INTEGER PRIMARY KEY, source TEXT, booking_date TEXT,
  partner TEXT, description TEXT, amount REAL, currency TEXT, category_id INTEGER, effective_amount REAL,
  status TEXT, note TEXT, raw_hash TEXT UNIQUE, extra TEXT);
  INSERT INTO transactions(source,booking_date,partner,description,amount,currency,status,raw_hash,extra)
  VALUES('splitwise','2026-05-01','Wohnung','miete',-400,'EUR','info','x1','{}');
  INSERT INTO transactions(source,booking_date,partner,description,amount,currency,status,raw_hash,extra)
  VALUES('bank','2026-05-04','Start','x',-1,'EUR','active','x2','{}');""")
    old.commit()
    old.close()
    saved = m.DB_PATH
    m.DB_PATH = p2
    m.init_db()
    c2 = m.db()
    check(
        "fixed_cost_group = wohnung",
        m.get_setting(c2, "fixed_cost_group") == "wohnung",
        m.get_setting(c2, "fixed_cost_group"),
    )
    check("marker set", m.get_setting(c2, "migrated_v2") == "1")
    c2.close()
    m.DB_PATH = saved
    c3 = m.db()
    check("fresh DB gets no group", m.get_setting(c3, "fixed_cost_group") in ("", "wg"))
    c3.close()

    print("4) Fixed costs with 100 % appear under recurring costs")
    abo = cid("Abos")
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Netflix',?,100,'')", (abo,)
    )
    for i, d in enumerate(("2026-04-05", "2026-05-05", "2026-06-05")):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status) VALUES('paypal',?,'Netflix.com','Abo',-13.99,'EUR',?,'active')""",
            (d, "nf%d" % i),
        )
    con.commit()
    m.auto_split_wohnung_fixed(con)
    con.commit()
    w, p, b, sums = m.recurring_groups(con)
    names = [s["partner"] for s in w + p]
    check("Netflix (100 %) is included", any("Netflix" in n for n in names), names)
    nf = next((s for s in w + p if "Netflix" in s["partner"]), None)
    check("with full amount", nf and abs(nf["monthly"] - 13.99) < 0.01, nf and nf["monthly"])
    check(
        "category set from the fixed cost row",
        con.execute("SELECT category_id FROM transactions WHERE raw_hash='nf0'").fetchone()["category_id"] == abo,
    )

    print("5) Orphaned netting is reverted after the cutoff date")
    pv = m.system_cat_id(con, "PayPal-Verrechnung")
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id,note) VALUES('bank','2026-06-20','PayPal Europe S.a.r.l.','x',-42,'EUR',
    'orph1','excluded',?,'automatisch mit PayPal verrechnet')""",
        (pv,),
    )
    # ... and a second one that still has a real receipt
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id) VALUES('bank','2026-06-21','PayPal Europe S.a.r.l.','x',-7,'EUR',
    'bk2','excluded',?)""",
        (pv,),
    )
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id) VALUES('paypal','2026-06-21','Shop','x',-7,'EUR','pp2','active',?)""",
        (abo,),
    )
    b2 = con.execute("SELECT id FROM transactions WHERE raw_hash='bk2'").fetchone()["id"]
    p2_ = con.execute("SELECT id FROM transactions WHERE raw_hash='pp2'").fetchone()["id"]
    con.execute("INSERT INTO matches(bank_tx,other_tx,kind) VALUES(?,?,'paypal')", (b2, p2_))
    con.commit()
    m.cleanup_before_start(con)
    con.commit()
    r1 = con.execute("SELECT status, category_id FROM transactions WHERE raw_hash='orph1'").fetchone()
    r2 = con.execute("SELECT status FROM transactions WHERE raw_hash='bk2'").fetchone()
    check(
        "without receipt: active again and uncategorised",
        r1["status"] == "active" and r1["category_id"] is None,
        dict(r1),
    )
    check("with receipt: stays netted", r2["status"] == "excluded", dict(r2))
    _exp = m.summarize(m.stats_transactions(con, month="2026-06"))[1]
    check(
        "now counts as expense",
        abs(_exp.get("Unkategorisiert", (0, 0))[0] - 42.0) < 0.01,
        {k: round(v[0], 2) for k, v in _exp.items()},
    )
    con.close()

    print("6) All pages load")
    for p_ in ("/", "/jahr", "/fixkosten", "/splitwise", "/einstellungen", "/hilfe", "/import", "/transactions"):
        check("GET " + p_, c.get(p_).status_code == 200)

    print("7) Create fixed costs AFTER the first import - the pipeline rolls back")
    # (journey test 2026-09-13: before, the cash netting and the credit for the rent
    # items stayed permanently -> counted twice/wrongly)
    tmp3 = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp3, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    m.set_setting(con, "splitwise_name", "Max Muster")
    m.set_setting(con, "sources", '["bank","splitwise"]')
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    bar = con.execute("SELECT id FROM categories WHERE name='Bargeld'").fetchone()["id"]
    rows = [
        ("2026-05-28", "Hausverwaltung Muster", "Miete", -900.0, None),
        ("2026-06-28", "Hausverwaltung Muster", "Miete", -900.0, None),
        ("2026-06-10", "BANKOMAT", "Abhebung", -100.0, bar),
    ]
    for i, (d, p_, desc, a, cid_) in enumerate(rows):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id) VALUES('bank',?,?,?,?,'EUR',?,'active',?)""",
            (d, p_, desc, a, "r%d" % i, cid_),
        )
    # Splitwise: I advanced the rent (bal +450), booked on the 1st -> no auto link (>3 days)
    for i, d in enumerate(("2026-06-01", "2026-07-01")):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,extra) VALUES('splitwise',?,'wg','miete',450,'EUR',?,'info',?)""",
            (d, "sw%d" % i, json.dumps({"cost": 900, "bal": 450, "members": ["Anna"]})),
        )
    con.commit()
    # pipeline without fixed cost configuration
    m.auto_link_own_advances(con)
    m.promote_own_advance_credits(con)
    con.commit()
    atm = con.execute("SELECT effective_amount FROM transactions WHERE raw_hash='r2'").fetchone()["effective_amount"]
    check("without fixed costs: cash pool was tapped", atm is not None and abs(atm) < 0.01, atm)
    cred = con.execute("SELECT status, effective_amount FROM transactions WHERE raw_hash='sw0'").fetchone()
    check(
        "without fixed costs: remainder active as credit (only the remainder!)",
        cred["status"] == "active" and abs((cred["effective_amount"] or 0) - 350.0) < 0.01,
        dict(cred),
    )
    # now create fixed costs and run the pipeline again
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung Muster',?,50,'miete')",
        (woh,),
    )
    m.set_setting(con, "fixed_cost_group", "wg")
    con.commit()
    n = m.reconcile_fixed_cost_positions(con)
    m.auto_split_wohnung_fixed(con)
    m.auto_link_own_advances(con)
    m.promote_own_advance_credits(con)
    con.commit()
    check("reconcile intervened", n >= 2, n)
    atm = con.execute("SELECT effective_amount FROM transactions WHERE raw_hash='r2'").fetchone()["effective_amount"]
    check("withdrawal full again", atm is None, atm)
    sw = [dict(r) for r in con.execute("SELECT status, effective_amount FROM transactions WHERE source='splitwise'")]
    check("rent items info again", all(r["status"] == "info" for r in sw), sw)
    rent = con.execute("SELECT effective_amount, category_id FROM transactions WHERE raw_hash='r0'").fetchone()
    check(
        "rent halved and on Wohnung",
        abs(rent["effective_amount"] + 450) < 0.01 and rent["category_id"] == woh,
        dict(rent),
    )
    inc, exp = m.summarize(m.stats_transactions(con))
    check(
        "expenses = 450 + 450 + 100",
        abs(sum(v for v, _ in exp.values()) - 1000.0) < 0.01,
        {k: round(v[0], 2) for k, v in exp.items()},
    )
    con.close()

    print("8) Renamed default category stays renamed")
    con = m.db()
    con.execute("UPDATE categories SET name='Essen' WHERE name='Lebensmittel'")
    con.commit()
    con.close()
    m.init_db()
    con = m.db()
    check(
        "no new 'Lebensmittel' after restart",
        con.execute("SELECT COUNT(*) FROM categories WHERE name='Lebensmittel'").fetchone()[0] == 0,
    )
    check("'Essen' is there", con.execute("SELECT COUNT(*) FROM categories WHERE name='Essen'").fetchone()[0] == 1)
    con.close()
    finish()
