"""Payments entered by hand (source 'manual') and several income sources:
POST /tx/new creates counting rows with the right sign, they show up in the
period API, survive the cutoff cleanup, can be linked as repayment and can be
deleted - imported rows cannot. The income settings take one row per source
with its own income category; each row is an ordinary rule."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402
from _ui_common import post as _post
from _ui_common import setup


def _body(r):
    return r.get_data(as_text=True) if hasattr(r, "get_data") else str(r)


def test_manual_tx():
    m, c, tmp = setup()

    def post(url, data, follow=False):
        return _post(c, url, data, follow)

    def cid(name):
        con = m.db()
        r = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        con.close()
        return r["id"]

    def rows(where="1=1"):
        con = m.db()
        out = [dict(r) for r in con.execute(f"SELECT * FROM transactions WHERE {where} ORDER BY id")]
        con.close()
        return out

    def add_bank(con, day, partner, amount, h):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,raw_hash,status) "
            "VALUES('bank',?,?,'',?,?,'active')",
            (day, partner, amount, h),
        )

    c.post("/setup", data={"src_bank": "1", "theme": "auto", "income_pattern": "Firma AG", "income_category": "Gehalt"})

    print("\n1) Entering by hand")
    html = c.get("/transactions").get_data(as_text=True)
    check("form on the list", 'id="tx-new-form"' in html)
    check("no form on the PayPal list", 'id="tx-new-form"' not in c.get("/paypal").get_data(as_text=True))
    r = post(
        "/tx/new",
        {
            "date": "2026-11-02",
            "kind": "expense",
            "amount": "12,50",
            "partner": "Kiosk",
            "description": "Zeitung",
            "category_id": cid("Lebensmittel"),
        },
    )
    check("expense: redirect", getattr(r, "status_code", r) == 302, r)
    r = post(
        "/tx/new",
        {
            "date": "02.11.2026",
            "kind": "income",
            "amount": "300",
            "partner": "Nebenjob",
            "category_id": cid("Sonstige Einnahmen"),
        },
    )
    check("income: redirect", getattr(r, "status_code", r) == 302, r)
    r = post("/tx/new", {"date": "2026-11-03", "kind": "expense", "amount": "-7", "partner": "Automat"}, follow=True)
    check("without category: WEITER flash", "WEITER" in _body(r), _body(r)[:200])
    man = rows("source='manual'")
    check("three hand-entered rows", len(man) == 3, len(man))
    check("expense negative", man[0]["amount"] == -12.5 and man[0]["status"] == "active", man[0]["amount"])
    check("income positive, date parsed", man[1]["amount"] == 300 and man[1]["booking_date"] == "2026-11-02", man[1])
    check("signed amount kept", man[2]["amount"] == -7 and man[2]["category_id"] is None, man[2]["amount"])
    check("own hashes, all different", len({t["raw_hash"] for t in man}) == 3)
    con = m.db()
    check("uncategorised one waits in the review queue", m.todo_count(con) == 1, m.todo_count(con))
    con.close()
    for bad, data in (
        ("amount 0", {"date": "2026-11-02", "kind": "expense", "amount": "0", "partner": "X"}),
        ("no partner/text", {"date": "2026-11-02", "kind": "expense", "amount": "5"}),
        ("bad date", {"date": "gestern", "kind": "expense", "amount": "5", "partner": "X"}),
        (
            "unknown category",
            {"date": "2026-11-02", "kind": "expense", "amount": "5", "partner": "X", "category_id": 9999},
        ),
    ):
        r = post("/tx/new", data, follow=True)
        check(f"refused: {bad}", "FEHLER" in _body(r) and len(rows("source='manual'")) == 3, _body(r)[:120])

    print("\n2) Counting like imported rows")
    tiles = c.get("/api/period?month=2026-11").get_json()["tiles"]
    check("income tile 300", abs(tiles["inc"]["value"] - 300) < 0.005, tiles["inc"]["value"])
    check(
        "expense tile 19.50 (12.50 + 7 uncategorised)", abs(tiles["exp"]["value"] - 19.5) < 0.005, tiles["exp"]["value"]
    )
    html = c.get("/transactions?src=manual").get_data(as_text=True)
    check("source filter 'manual' lists 3 rows", html.count('<span class="pill">manual</span>') == 3)
    check("delete button on every hand-entered row", html.count("tx-delete") == 3)
    con = m.db()
    add_bank(con, "2026-11-02", "BILLA", -9.99, "hb1")
    con.commit()
    con.close()
    html = c.get("/transactions?month=2026-11").get_data(as_text=True)
    check("bank row has no delete button", html.count("tx-delete") == 3)

    print("\n3) Cleanup before the cutoff keeps hand-entered rows")
    con = m.db()
    m.set_setting(con, "data_cutoff", "2027-01-01")
    con.commit()
    m.cleanup_before_start(con)
    con.commit()
    check("bank row before the cutoff removed", len(rows("source='bank'")) == 0)
    check("hand-entered rows kept", len(rows("source='manual'")) == 3)
    m.set_setting(con, "data_cutoff", "")
    con.commit()
    con.close()

    print("\n4) Repayment against a hand-entered expense")
    exp_id = man[0]["id"]
    post("/tx/new", {"date": "2026-11-04", "kind": "income", "amount": "10", "partner": "Kollege", "category_id": ""})
    inc_id = rows("source='manual' AND partner='Kollege'")[0]["id"]
    con = m.db()
    m.link_repayment(con, inc_id, [exp_id])
    con.commit()
    con.close()
    e = rows(f"id={exp_id}")[0]
    check("expense reduced to 2.50", e["effective_amount"] is not None and abs(e["effective_amount"] + 2.5) < 0.005, e)
    check("repayment itself excluded", rows(f"id={inc_id}")[0]["status"] == "excluded")
    con = m.db()
    m.unlink_repayment(con, inc_id)
    con.commit()
    con.close()

    print("\n5) Deleting")
    r = post(f"/tx/{exp_id}/delete", {})
    check("hand-entered row deleted", getattr(r, "status_code", r) == 302 and len(rows(f"id={exp_id}")) == 0)
    post(f"/tx/{inc_id}/delete", {})
    check("hand-entered income deleted", len(rows(f"id={inc_id}")) == 0)
    con = m.db()
    add_bank(con, "2026-11-05", "BILLA", -9.99, "hb2")
    bank_id = con.execute("SELECT id FROM transactions WHERE raw_hash='hb2'").fetchone()[0]
    con.commit()
    con.close()
    r = post(f"/tx/{bank_id}/delete", {}, follow=True)
    check("imported row refused with FEHLER", "FEHLER" in _body(r) and len(rows(f"id={bank_id}")) == 1)
    r = post("/tx/999999/delete", {}, follow=True)
    check("unknown id refused", "FEHLER" in _body(r))

    print("\n6) Several income sources with their own categories")
    base = {"action": "general", "shared_name": "", "data_cutoff": "", "income_category": "Gehalt"}
    post(
        "/einstellungen",
        {
            **base,
            "income_pattern": ["Firma AG", "Vermieter Muster", ""],
            "income_cat": ["Gehalt", "Miete (Einnahme)", "Gehalt"],
        },
    )
    con = m.db()
    src = m.income_sources(con)
    check(
        "two sources in two categories", src == [("Firma AG", "Gehalt"), ("Vermieter Muster", "Miete (Einnahme)")], src
    )
    check("salary patterns only the salary category", m.income_rule_patterns(con) == ["Firma AG"])
    add_bank(con, "2026-11-06", "Vermieter Muster", 650, "hb3")
    con.commit()
    m.apply_rules(con)
    con.commit()
    cat = con.execute(
        "SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE raw_hash='hb3'"
    ).fetchone()
    check("rent income categorised by its own rule", cat and cat[0] == "Miete (Einnahme)", cat and cat[0])
    con.close()
    html = c.get("/einstellungen").get_data(as_text=True)
    check(
        "settings show both rows plus an empty one",
        html.count('class="income-row') == 3,
        html.count('class="income-row'),
    )
    tiles = c.get("/api/period?month=2026-11").get_json()["tiles"]
    check("income tile now 950 (300 + 650)", abs(tiles["inc"]["value"] - 950) < 0.005, tiles["inc"]["value"])
    # legacy form: one comma-separated field without income_cat -> salary category
    post("/einstellungen", {**base, "income_pattern": "Firma AG, Zweitjob"})
    con = m.db()
    src = m.income_sources(con)
    check(
        "comma-separated field -> salary rules, rent rule gone",
        src == [("Firma AG", "Gehalt"), ("Zweitjob", "Gehalt")],
        src,
    )
    con.close()
    finish()
