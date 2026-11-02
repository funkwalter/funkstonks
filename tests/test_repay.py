"""Repayments: link an incoming payment with expenses (manually via
/tx/<id>/repay, automatically via repayment rules in the import pipeline),
unlink, capacity limit, stacking, effect on evaluation and recurring costs."""

import json

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_repay():

    tmp, c = fresh_db()
    con = m.db()
    m.set_setting(con, "fixkosten_privat_cats", json.dumps(["Abos"]))
    urlaub = con.execute("SELECT id FROM categories WHERE name='Urlaub'").fetchone()["id"]
    abos = con.execute("SELECT id FROM categories WHERE name='Abos'").fetchone()["id"]
    con.commit()
    con.close()

    print("1) Manual: a friend's share on a card booking")
    BANK = george_bank(
        [
            ("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50"),
            ("03.05.2026", "Max Muster", "Anteil Kopenhagen", "100,00"),
            ("20.05.2026", "Anna Muster", "Anteil Kopenhagen", "150,00"),
        ]
    )
    CARD = george_card([("02.05.2026", "AIRBNB * XYZ", "-400,00"), ("02.05.2026", "RYANAIR ABC", "-80,00")])
    html = upload(c, (BANK, "Umsaetze_2026-05-31.csv"), (CARD, "Kartenkonto_1234_2026-05-31.csv"))
    air = one("SELECT * FROM transactions WHERE partner='AIRBNB * XYZ'")
    rya = one("SELECT * FROM transactions WHERE partner='RYANAIR ABC'")
    inc1 = one("SELECT * FROM transactions WHERE partner='Max Muster'")
    inc2 = one("SELECT * FROM transactions WHERE partner='Anna Muster'")
    check("test data present", air and rya and inc1 and inc2)
    c.post("/tx/%d/update" % air["id"], data={"category_id": urlaub})
    c.post("/tx/%d/update" % rya["id"], data={"category_id": urlaub})
    c.post("/tx/%d/update" % inc1["id"], data={"category_id": urlaub})  # as the user used to do it
    h = c.get("/transactions").get_data(as_text=True)
    check("repay button on the incoming payment", "/tx/%d/repay" % inc1["id"] in h)
    check("no repay button on the expense", "/tx/%d/repay" % air["id"] not in h)
    h = c.get("/tx/%d/repay?back=/transactions" % inc1["id"]).get_data(as_text=True)
    check("page shows candidates (Airbnb, Ryanair)", "AIRBNB * XYZ" in h and "RYANAIR ABC" in h)
    check("Kontofuehrung (still open, -0,50) is a candidate", "Kontofuehrung" in h)
    r = c.post(
        "/tx/%d/repay" % inc1["id"], data={"tx_ids": [str(air["id"])], "back": "/transactions"}, follow_redirects=True
    )
    h = r.get_data(as_text=True)
    check("flash: linked", "Rückzahlung verknüpft" in h, h[:300])
    a2 = one("SELECT * FROM transactions WHERE id=?", air["id"])
    i2 = one(
        "SELECT t.*, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?",
        inc1["id"],
    )
    check(
        "Airbnb now counts only 300",
        a2["effective_amount"] is not None and abs(a2["effective_amount"] + 300) < 0.005,
        a2["effective_amount"],
    )
    check("note on the expense", "Rückzahlung verknüpft (manuell, −100.00 €)" in (a2["note"] or ""), a2["note"])
    check(
        "incoming excluded + system category Rueckzahlung",
        i2["status"] == "excluded" and i2["cat"] == "Rückzahlung",
        (i2["status"], i2["cat"]),
    )
    check("previous category remembered", json.loads(i2["extra"]).get("repay_prev", {}).get("category_id") == urlaub)
    mt = q("SELECT * FROM matches WHERE kind='repayment'")
    check(
        "one matches row with delta 100",
        len(mt) == 1
        and mt[0]["bank_tx"] == air["id"]
        and mt[0]["other_tx"] == inc1["id"]
        and abs(mt[0]["amount"] - 100) < 0.005,
        mt,
    )
    h = c.get("/?month=2026-05").get_data(as_text=True)
    import re

    urlaub_val = re.search(r'Urlaub</a>\s*</td>\s*<td class="num">([\d.]+) €', h)
    check(
        "dashboard: Urlaub = 300 + 80 = 380 (credit no longer counts separately)",
        urlaub_val and abs(float(urlaub_val.group(1)) - 380) < 0.01,
        urlaub_val and urlaub_val.group(1),
    )
    h = c.get("/transactions").get_data(as_text=True)
    check("row shows the repayment check mark", "Rückzahlung ✓" in h)

    print("\n2) Protection: a linked credit cannot be toggled")
    r = c.post("/tx/%d/update" % inc1["id"], data={"toggle": "1"}, follow_redirects=True)
    check("FEHLER flash on toggle", "FEHLER" in r.get_data(as_text=True) and "Rückzahlung" in r.get_data(as_text=True))
    check(
        "status unchanged excluded",
        one("SELECT status FROM transactions WHERE id=?", inc1["id"])["status"] == "excluded",
    )

    print("\n3) Capacity: 150 on an expense with 80 remaining -> error, nothing happens")
    r = c.post(
        "/tx/%d/repay" % inc2["id"], data={"tx_ids": [str(rya["id"])], "back": "/transactions"}, follow_redirects=True
    )
    h = r.get_data(as_text=True)
    check(
        "FEHLER flash with amounts",
        "FEHLER" in h and "150.00" in h and "80.00" in h,
        re.findall(r'class="flash[^"]*">([^<]*)', h),
    )
    check(
        "Ryanair unchanged",
        one("SELECT effective_amount FROM transactions WHERE id=?", rya["id"])["effective_amount"] is None,
    )
    check(
        "incoming 150 still active", one("SELECT status FROM transactions WHERE id=?", inc2["id"])["status"] == "active"
    )
    check("no second matches row", count("1") and len(q("SELECT * FROM matches WHERE kind='repayment'")) == 1)

    print("\n4) Distribution over two expenses (150 on Airbnb remainder 300 + Ryanair 80)")
    r = c.post(
        "/tx/%d/repay" % inc2["id"],
        data={"tx_ids": [str(air["id"]), str(rya["id"])], "back": "/transactions"},
        follow_redirects=True,
    )
    a3 = one("SELECT * FROM transactions WHERE id=?", air["id"])
    r3 = one("SELECT * FROM transactions WHERE id=?", rya["id"])
    d_air = round(a3["effective_amount"] + 300, 2)
    d_rya = round(r3["effective_amount"] + 80, 2)
    check("sum of deltas = 150", abs(d_air + d_rya - 150) < 0.005, (d_air, d_rya))
    check(
        "proportional to remainder (300:80 -> ~118.42 : ~31.58)",
        abs(d_air - 118.42) < 0.02 and abs(d_rya - 31.58) < 0.02,
        (d_air, d_rya),
    )
    check(
        "two repayments stacked on Airbnb (two matches)",
        len(q("SELECT * FROM matches WHERE bank_tx=? AND kind='repayment'", air["id"])) == 2,
    )

    print("\n5) Unlinking the first repayment leaves the second in place")
    r = c.post("/tx/%d/repay/unlink" % inc1["id"], data={"back": "/transactions"}, follow_redirects=True)
    a4 = one(
        "SELECT t.*, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?",
        air["id"],
    )
    i4 = one(
        "SELECT t.*, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?",
        inc1["id"],
    )
    check(
        "Airbnb: only the second reduction left (-400 + 118.42)",
        abs(a4["effective_amount"] - (-400 + d_air)) < 0.005,
        a4["effective_amount"],
    )
    check(
        "note of the first gone, second stays",
        "−100.00" not in (a4["note"] or "") and "−%.2f" % d_air in (a4["note"] or ""),
        a4["note"],
    )
    check(
        "incoming active again with old category Urlaub",
        i4["status"] == "active" and i4["cat"] == "Urlaub",
        (i4["status"], i4["cat"]),
    )
    check("repay_prev removed", "repay_prev" not in json.loads(i4["extra"] or "{}"))
    r = c.post(
        "/tx/%d/repay" % inc2["id"], data={"back": "/transactions"}, follow_redirects=True
    )  # empty selection = unlink
    a5 = one("SELECT * FROM transactions WHERE id=?", air["id"])
    r5 = one("SELECT * FROM transactions WHERE id=?", rya["id"])
    check(
        "all unlinked: effective_amount NULL again",
        a5["effective_amount"] is None and r5["effective_amount"] is None and (a5["note"] or "") == "",
        (a5["effective_amount"], r5["effective_amount"], a5["note"]),
    )
    check("no matches left", len(q("SELECT * FROM matches WHERE kind='repayment'")) == 0)

    print("\n6) Rule: the friends' Spotify shares, three months")
    BANK2 = george_bank(
        [
            ("29.05.2026", "SPOTIFY P1", "E-COMM 20,99 SE K1 SPOTIFY", "-20,99"),
            ("01.06.2026", "Max Beispiel", "Spotify", "3,00"),
            ("01.06.2026", "Lea Beispiel", "Spotify Lea", "3,50"),
            ("29.06.2026", "SPOTIFY P2", "E-COMM 20,99 SE K1 SPOTIFY", "-20,99"),
            ("01.07.2026", "Max Beispiel", "Spotify", "3,00"),
            ("02.07.2026", "Lea Beispiel", "Spotify Lea", "3,50"),
            ("29.07.2026", "SPOTIFY P3", "E-COMM 20,99 SE K1 SPOTIFY", "-20,99"),
            ("01.08.2026", "Max Beispiel", "Spotify", "3,00"),
            ("01.08.2026", "Lea Beispiel", "Spotify Lea", "3,50"),
        ]
    )
    html = upload(c, (BANK2, "Umsaetze_2026-08-05.csv"))
    check("without rule: nothing netted", "repaid" not in steps(html), steps(html))
    r = c.post(
        "/rules",
        data={"repay_add": "1", "in_pattern": "Spotify", "out_pattern": "SPOTIFY P", "window_days": "45"},
        follow_redirects=True,
    )
    h = r.get_data(as_text=True)
    check(
        "flash: rule created, 6 netted",
        "6 Zahlungen gleich verrechnet" in h,
        re.findall(r'class="flash[^"]*">([^<]*)', h),
    )
    check("rule in the table with 6x", "<code>Spotify</code>" in h and "6×" in h)
    check(
        "rule shows its latest example (incoming -> debit, now counts 14.49)",
        re.search(
            r'class="repay-example">.*?zuletzt: \d{4}-\d\d-\d\d <b>[^<]*Beispiel</b> \+[\d.]+ € → abgezogen von \d{4}-\d\d-\d\d <b>SPOTIFY P[^<]*</b>\s*20\.99 €, die zählt jetzt mit 14\.49 €',
            h,
            re.S,
        )
        is not None,
        h[h.find("repay-example") :][:300],
    )
    sp = q(
        "SELECT partner, effective_amount, note FROM transactions WHERE partner LIKE 'SPOTIFY P%' ORDER BY booking_date"
    )
    check(
        "every debit counts as 14.49",
        len(sp) == 3 and all(abs(s["effective_amount"] + 14.49) < 0.005 for s in sp),
        [(s["partner"], s["effective_amount"]) for s in sp],
    )
    check("note says automatic", all("automatisch" in (s["note"] or "") for s in sp))
    check("six incoming payments excluded", count("partner LIKE '%Beispiel' AND status='excluded'") == 6)
    check(
        "incoming does NOT match the Spotify debit itself (SPOTIFY P is no incoming payment)",
        count("partner LIKE 'SPOTIFY P%' AND status='excluded'") == 0,
    )
    con = m.db()
    c.post(
        "/tx/%d/update" % one("SELECT id FROM transactions WHERE partner='SPOTIFY P1'")["id"],
        data={"category_id": abos},
    )
    for pid in q("SELECT id FROM transactions WHERE partner LIKE 'SPOTIFY P%'"):
        con.execute("UPDATE transactions SET category_id=? WHERE id=?", (abos, pid["id"]))
    con.commit()
    wohnen, privat, beendet, sums = m.recurring_groups(con)
    con.close()
    spo = next((s for s in privat if "SPOTIFY" in s["partner"].upper()), None)
    check(
        "recurring costs: Spotify with 14.49 / month, not 20.99",
        spo is not None and abs(spo["monthly"] - 14.49) < 0.005,
        spo and (spo["partner"], spo["monthly"]),
    )
    check(
        "incoming payments do not appear as a series of their own",
        not any("Beispiel" in s["partner"] for s in privat + wohnen + beendet),
    )

    print("\n7) Pipeline: the next import nets new incoming payments automatically (idempotent)")
    BANK3 = george_bank(
        [
            ("29.08.2026", "SPOTIFY P4", "E-COMM 20,99 SE K1 SPOTIFY", "-20,99"),
            ("01.09.2026", "Max Beispiel", "Spotify", "3,00"),
            ("01.09.2026", "Lea Beispiel", "Spotify Lea", "3,50"),
            (
                "10.09.2026",
                "Fremder Eingang",
                "Spotify Gutschein",
                "50,00",
            ),  # fits the pattern, but no remainder is large enough (14.49)
        ]
    )
    html = upload(c, (BANK3, "Umsaetze_2026-09-12.csv"))
    check("step repaid = 2 (the 50 stays because no remainder suffices)", steps(html).get("repaid") == 2, steps(html))
    check(
        "P4 counts as 14.49",
        abs(one("SELECT effective_amount FROM transactions WHERE partner='SPOTIFY P4'")["effective_amount"] + 14.49)
        < 0.005,
    )
    check(
        "50 incoming still active",
        one("SELECT status FROM transactions WHERE partner='Fremder Eingang'")["status"] == "active",
    )
    html = upload(c, (BANK3, "Umsaetze_2026-09-12.csv"))
    check(
        "re-import: nothing twice (no repaid step, P4 unchanged)",
        "repaid" not in steps(html)
        and abs(one("SELECT effective_amount FROM transactions WHERE partner='SPOTIFY P4'")["effective_amount"] + 14.49)
        < 0.005,
        steps(html),
    )
    check("8 repayment matches in total", len(q("SELECT * FROM matches WHERE kind='repayment'")) == 8)

    print("\n8) Deleting the rule leaves the links in place; category Rueckzahlung not deletable")
    rid = one("SELECT id FROM repay_rules")["id"]
    c.post("/rules", data={"repay_delete": str(rid)}, follow_redirects=True)
    check("rule gone", count("1") and one("SELECT COUNT(*) n FROM repay_rules")["n"] == 0)
    check("matches stay", len(q("SELECT * FROM matches WHERE kind='repayment'")) == 8)
    rz = one("SELECT id FROM categories WHERE name='Rückzahlung'")
    r = c.post("/categories", data={"action": "delete", "id": str(rz["id"])}, follow_redirects=True)
    check(
        "system category Rueckzahlung stays",
        one("SELECT COUNT(*) n FROM categories WHERE name='Rückzahlung'")["n"] == 1,
    )
    finish()
