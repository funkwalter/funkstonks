"""Cash pool (promote_own_advance_credits): only withdrawals form the pool.
A credit in the cash category (own transfer, deposit) must never be "consumed" -
it would grow instead of shrink."""

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_bargeld_pool():

    SW = splitwise_csv(
        SW_DE_HEAD, ["2026-05-10,cam,Allgemein,70.00,EUR,35.00,-35.00"], total=",Gesamtsaldo,,,EUR,35.00,-35.00"
    )

    print("1) Only a cash CREDIT present -> no pool, share is promoted as a credit")
    tmp, c = fresh_db()
    con = m.db()
    bar = con.execute("SELECT id FROM categories WHERE name='Bargeld'").fetchone()["id"]
    con.commit()
    con.close()
    html = upload(
        c,
        (
            george_bank(
                [
                    ("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50"),
                    ("05.05.2026", "Anna Muster", "Uebertrag Sparkonto", "400,00"),
                ]
            ),
            "b.csv",
        ),
    )
    dep = one("SELECT id FROM transactions WHERE amount=400")
    c.post("/tx/%d/update" % dep["id"], data={"category_id": bar})
    html = upload(c, (SW, "wg_2026-05-31.csv"))
    d2 = one("SELECT * FROM transactions WHERE id=?", dep["id"])
    check(
        "credit unchanged (no effective_amount, no note)",
        d2["effective_amount"] is None and not (d2["note"] or ""),
        (d2["effective_amount"], d2["note"]),
    )
    check("no matches on the credit", count("id IN (SELECT bank_tx FROM matches)") == 0)
    sw = one("SELECT * FROM transactions WHERE source='splitwise' AND description='cam'")
    check(
        "position promoted as share credit (35)",
        sw["status"] == "active"
        and (sw["note"] or "").startswith("Anteil-Gutschrift")
        and abs((sw["effective_amount"] if sw["effective_amount"] is not None else sw["amount"]) - 35) < 0.005,
        (sw["status"], sw["note"], sw["effective_amount"]),
    )
    check(
        "step credited=1, nothing linked",
        steps(html).get("credited") == 1 and "linked" not in steps(html),
        steps(html),
    )

    print("\n2) Withdrawal AND credit -> only the withdrawal is consumed")
    tmp, c = fresh_db()
    con = m.db()
    bar = con.execute("SELECT id FROM categories WHERE name='Bargeld'").fetchone()["id"]
    con.commit()
    con.close()
    html = upload(
        c,
        (
            george_bank(
                [
                    ("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50"),
                    ("05.05.2026", "Anna Muster", "Uebertrag Sparkonto", "400,00"),
                    ("09.05.2026", "", "AUTOMAT 00001 K1 09.05.", "-100,00"),
                ]
            ),
            "b.csv",
        ),
    )
    for r in q("SELECT id FROM transactions WHERE amount IN (400, -100)"):
        c.post("/tx/%d/update" % r["id"], data={"category_id": bar})
    html = upload(c, (SW, "wg_2026-05-31.csv"))
    dep = one("SELECT * FROM transactions WHERE amount=400")
    atm = one("SELECT * FROM transactions WHERE amount=-100")
    check(
        "withdrawal reduced to -65 (100 - 35)",
        atm["effective_amount"] is not None and abs(atm["effective_amount"] + 65) < 0.005,
        atm["effective_amount"],
    )
    check(
        "credit unchanged",
        dep["effective_amount"] is None and not (dep["note"] or ""),
        (dep["effective_amount"], dep["note"]),
    )
    sw = one("SELECT * FROM transactions WHERE source='splitwise' AND description='cam'")
    check("position stays raw data (settled via the pool)", sw["status"] == "info", sw["status"])
    mt = [(r["bank_tx"], r["amount"]) for r in q("SELECT bank_tx, amount FROM matches WHERE kind='splitwise_share'")]
    check("one match on the withdrawal with delta 35", mt == [(atm["id"], 35.0)], (mt, atm["id"]))
    finish()
