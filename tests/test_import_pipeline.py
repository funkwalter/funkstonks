"""Import pipeline: automations after the upload (rules, fixed costs, PayPal/card
netting, foreign POS, Splitwise promotion/transfer) and data_cutoff."""

import io

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_import_pipeline():

    flashes = result_lines

    # ============================================================ A) Automations
    tmp, c = fresh_db()
    con = m.db()
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'miete')",
        (woh,),
    )
    abo = con.execute("SELECT id FROM categories WHERE name='Abos'").fetchone()["id"]
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Handyvertrag',?,100,'')", (abo,)
    )
    m.set_setting(con, "shared_name", "Anna")
    con.commit()
    con.close()

    BANK = george_bank(
        [
            ("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50"),
            ("01.06.2026", "BILLA DANKT 0001234", "POS 12,34 AT K1 01.06. 10:11", "-12,34"),
            ("02.06.2026", "Hausverwaltung Müller", "Miete Juni", "-900,00"),
            ("02.06.2026", "Handyvertrag GmbH", "Tarif Juni", "-19,90"),
            ("03.06.2026", "PayPal Europe S.a.r.l. et Cie S.C.A", "Lastschrift 1234567", "-29,99"),
            ("05.06.2026", "Erste Bank", "Abrechnung Kreditkarte 05/2026", "-26,49"),
            ("06.06.2026", "Lokal Bratislava", "POS 25,00 SK K1 06.06. 12:00", "-25,00"),
            (
                "06.06.2026",
                "NETTO KOEBENHAVN",
                "POS 78,40 DKK K1 05.06. 18:23 NETTO KOEBENHAVN S 2300 208 SPESEN: 1,17 KURS: 7,440427",
                "-11,71",
            ),
            ("07.06.2026", "Anthropic", "E-COMM 20,00 US K1 07.06.", "-20,00"),
            ("08.06.2026", "Bob Beispiel", "Splitwise Ausgleich", "25,00"),
        ]
    )
    PAYPAL = paypal_akt([("01.06.2026", "Netflix", "Zahlung", "EUR", "-29,99", "TX0001")])
    CARD = george_card([("20.05.2026", "SPOTIFY AB", "-10,99"), ("25.05.2026", "AMZN Mktp DE", "-15,50")])
    SW = splitwise_csv(
        SW_DE_HEAD,
        [
            "2026-06-05,bier,Alkohol,20.00,EUR,-10.00,10.00",
            "2026-06-06,BILLA einkauf,Lebensmittel,30.00,EUR,-15.00,15.00",
            "2026-06-08,Bob zahlte Anna Muster,Zahlung,25.00,EUR,-25.00,25.00",
        ],
        total=",Gesamtsaldo,,,EUR,0.00,0.00",
    )

    print("A1) One upload with bank + PayPal + card + Splitwise")
    html = upload(
        c,
        (BANK, "Umsaetze_2026-06-30.csv"),
        (PAYPAL, "Download.CSV"),
        (CARD, "Kartenkonto_1234_2026-06-01.csv"),
        (SW, "wg_2026-06-30.csv"),
    )
    st = steps(html)
    for label, cond in (
        ("Kontoauszug 10 neu", fr(html, "Umsaetze")[:2] == ("bank", 10)),
        ("PayPal 1 neu", fr(html, "Download.CSV")[:2] == ("paypal", 1)),
        ("Kreditkarte 2 neu", fr(html, "Kartenkonto")[:2] == ("card", 2)),
        ("Gruppe wg 3 neu", gr(html, "wg")[0] == 3),
        ("1 PayPal verrechnet", st.get("paypal") == 1),
        ("1 Kartenabrechnung verrechnet", st.get("card") == 1),
        ("1 Fixkosten-Buchung", st.get("fixed") == 1),
        ("2 Auslandszahlungen (Laendercode SK, Waehrungscode DKK)", st.get("foreign") == 2),
        ("2 Splitwise-Anteile uebernommen", st.get("promoted") == 2),
        ("1 Ausgleichszahlung", st.get("settle") == 1),
    ):
        check("Ergebnis: " + label, cond, flashes(html))
    check(
        "step 'per Regel kategorisiert' appears although the importer already rule-categorized BILLA/Netflix/Spotify",
        st.get("rules", 0) > 0,
        "import_page() verwirft n_rules der Importer (new, skip, _ = import_bank(...)); das nachgelagerte apply_rules() findet nichts mehr -> Meldung nie sichtbar",
    )
    check(
        "no 'before tracking start' message (everything from 01.05.)",
        "vor dem Beginn der Auswertung" not in html,
        flashes(html, "Tracking"),
    )

    rows = {
        r["partner"]: r
        for r in q(
            "SELECT t.*, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id "
            "WHERE t.source='bank'"
        )
    }
    print("\nA2) Rules")
    check(
        "BILLA -> Lebensmittel, active",
        rows["BILLA DANKT 0001234"]["cat"] == "Lebensmittel" and rows["BILLA DANKT 0001234"]["status"] == "active",
        dict(rows["BILLA DANKT 0001234"]),
    )
    check(
        "E-COMM abroad stays uncategorized (no Urlaub)",
        rows["Anthropic"]["cat"] is None,
        rows["Anthropic"]["cat"],
    )

    print("\nA3) Fixed costs")
    hv = rows["Hausverwaltung Müller"]
    check("Hausverwaltung -> Wohnung", hv["cat"] == "Wohnung", hv["cat"])
    check(
        "effective_amount = -450 (50%)",
        hv["effective_amount"] is not None and abs(hv["effective_amount"] + 450) < 0.001,
        hv["effective_amount"],
    )
    check("note 'automatisch halbiert (Annas Anteil)'", hv["note"] == "automatisch halbiert (Annas Anteil)", hv["note"])
    check("amount stays raw -900", abs(hv["amount"] + 900) < 0.001)
    hd = rows["Handyvertrag GmbH"]
    check(
        "100% fixed cost: only category Abos, effective_amount stays NULL",
        hd["cat"] == "Abos" and hd["effective_amount"] is None,
        (hd["cat"], hd["effective_amount"]),
    )

    print("\nA4) PayPal netting")
    pe = rows["PayPal Europe S.a.r.l. et Cie S.C.A"]
    check(
        "bank PayPal debit excluded + PayPal-Verrechnung",
        pe["status"] == "excluded" and pe["cat"] == "PayPal-Verrechnung",
        (pe["status"], pe["cat"]),
    )
    nf = one(
        "SELECT t.*, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.partner='Netflix'"
    )
    check(
        "PayPal side stays active, category Abos (rule Netflix)",
        nf["status"] == "active" and nf["cat"] == "Abos",
        (nf["status"], nf["cat"]),
    )
    mp = q("SELECT * FROM matches WHERE kind='paypal'")
    check(
        "exactly one matches entry kind=paypal (bank->paypal)",
        len(mp) == 1 and mp[0]["bank_tx"] == pe["id"] and mp[0]["other_tx"] == nf["id"],
        mp,
    )

    print("\nA5) Credit card netting")
    kk = rows["Erste Bank"]
    check(
        "collective debit excluded + Kreditkarten-Verrechnung",
        kk["status"] == "excluded" and kk["cat"] == "Kreditkarten-Verrechnung",
        (kk["status"], kk["cat"]),
    )
    mc = q("SELECT m.*, t.partner FROM matches m JOIN transactions t ON t.id=m.other_tx WHERE m.kind='card'")
    check(
        "two matches kind=card on the collective debit",
        len(mc) == 2
        and all(x["bank_tx"] == kk["id"] for x in mc)
        and {x["partner"] for x in mc} == {"SPOTIFY AB", "AMZN Mktp DE"},
        mc,
    )
    cards = q(
        "SELECT t.status, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.source='card'"
    )
    check(
        "card items stay active (Spotify via rule Abos)",
        all(x["status"] == "active" for x in cards) and any(x["cat"] == "Abos" for x in cards),
        cards,
    )

    print("\nA6) Foreign POS")
    check("POS SK -> Urlaub", rows["Lokal Bratislava"]["cat"] == "Urlaub", rows["Lokal Bratislava"]["cat"])
    check(
        "POS DKK (non-euro: currency instead of country) -> Urlaub",
        rows["NETTO KOEBENHAVN"]["cat"] == "Urlaub",
        rows["NETTO KOEBENHAVN"]["cat"],
    )

    print("\nA7) Splitwise promotion and settlement transfer")
    sw = {
        r["description"]: r
        for r in q(
            "SELECT t.*, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.source='splitwise'"
        )
    }
    check(
        "bier promoted, no category, note 'bezahlt von Bob'",
        sw["bier"]["status"] == "active" and sw["bier"]["cat"] is None and sw["bier"]["note"] == "bezahlt von Bob",
        (sw["bier"]["status"], sw["bier"]["cat"], sw["bier"]["note"]),
    )
    check(
        "'BILLA einkauf' promoted -> Lebensmittel via own rule",
        sw["BILLA einkauf"]["cat"] == "Lebensmittel",
        sw["BILLA einkauf"]["cat"],
    )
    check("settlement payment stays info", sw["Bob zahlte Anna Muster"]["status"] == "info")
    bob = rows["Bob Beispiel"]
    check(
        "bank incoming +25 -> excluded + Umbuchung",
        bob["status"] == "excluded" and bob["cat"] == "Umbuchung",
        (bob["status"], bob["cat"]),
    )
    todo = q(
        "SELECT partner, description FROM transactions WHERE status='active' AND category_id IS NULL "
        "AND id NOT IN (SELECT tx_id FROM splits)"
    )
    # "Bank AG / Kontofuehrung" has fallen under the default rule "Kontofuehrung" -> Gebuehren/Bank
    # since 2026-10-10 and is therefore no longer open.
    check(
        "open: Anthropic, AMZN, bier (3)",
        (lambda con: (m.todo_count(con), con.close())[0])(m.db()) == 3
        and {t["partner"] for t in todo} == {"Anthropic", "AMZN Mktp DE", "wg"},
        todo,
    )
    check(
        "Kontofuehrung via default rule to Gebuehren/Bank",
        one("SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.partner='Bank AG'")[
            "name"
        ]
        == "Gebühren/Bank",
    )

    print("\nA8) Re-import of all four files: idempotent")
    snap = q("SELECT id, status, category_id, effective_amount, note FROM transactions ORDER BY id")
    nm = count("1")
    nmatch = one("SELECT COUNT(*) n FROM matches")["n"]
    html = upload(
        c,
        (BANK, "Umsaetze_2026-06-30.csv"),
        (PAYPAL, "Download.CSV"),
        (CARD, "Kartenkonto_1234_2026-06-01.csv"),
        (SW, "wg_2026-06-30.csv"),
    )
    check(
        "everything 'already present'",
        has(html, "bank", 0, 10)
        and has(html, "paypal", 0, 1)
        and has(html, "card", 0, 2)
        and gr(html, "wg") == (0, 3, 0),
        flashes(html),
    )
    check("no more automation steps", not steps(html), steps(html))
    check(
        "rows/matches unchanged",
        count("1") == nm and one("SELECT COUNT(*) n FROM matches")["n"] == nmatch,
        (nm, count("1"), nmatch, one("SELECT COUNT(*) n FROM matches")["n"]),
    )
    check(
        "status/category/effective_amount/note of all rows identical",
        q("SELECT id, status, category_id, effective_amount, note FROM transactions ORDER BY id") == snap,
    )

    print("\nA9) Direct calls of the import functions (signatures)")
    r1 = m.import_bank(io.BytesIO(george_bank([("10.06.2026", "Direkt", "x", "-1,00")])))
    check("import_bank -> (new, skipped, n_rules)", r1[0] == 1 and r1[1] == 0 and len(r1) == 3, r1)
    r2 = m.import_card(
        io.BytesIO(george_card([("10.06.2026", "DirektKarte", "-1,00")])), "Kartenkonto_1234_2026-07-01.csv"
    )
    check("import_card -> (new, skipped, n_rules)", r2[0] == 1 and len(r2) == 3, r2)
    r3 = m.import_paypal(io.BytesIO(paypal_akt([("10.06.2026", "DirektPP", "Zahlung", "EUR", "-1,00", "D1")])))
    check("import_paypal -> (new, skipped, n_rules)", r3[0] == 1 and len(r3) == 3, r3)
    r4 = m.import_splitwise(
        io.BytesIO(splitwise_csv(SW_DE_HEAD, ["2026-06-10,direkt,Sonstiges,2.00,EUR,-1.00,1.00"])), "wg"
    )
    check(
        "import_splitwise -> (new, skipped, new_ids, removed)",
        r4[0] == 1 and len(r4[2]) == 1 and r4[3] == 0 and len(r4) == 4,
        r4,
    )
    try:
        m.import_bank(io.BytesIO(b"Foo;Bar\n1;2\n"))
        check("import_bank with wrong header raises ValueError", False, "kein Fehler")
    except ValueError as e:
        check("import_bank with wrong header raises ValueError", "Betrag" in str(e) and "Kopfzeile" in str(e), str(e))
    try:
        m.import_paypal(io.BytesIO(b"Foo;Bar\n1;2\n"))
        check("import_paypal with wrong header raises ValueError", False, "kein Fehler")
    except ValueError as e:
        check("import_paypal with wrong header raises ValueError", "PayPal-CSV" in str(e), str(e))
    try:
        m.import_splitwise(
            io.BytesIO(
                splitwise_csv(
                    "Datum,Beschreibung,Kategorien,Kosten,Währung,Fremd,Bob", ["2026-06-10,x,y,2.00,EUR,1,-1"]
                )
            ),
            "wg",
        )
        check("import_splitwise without own column raises ValueError", False, "kein Fehler")
    except ValueError as e:
        check("import_splitwise without own column raises ValueError", "nicht im Header" in str(e), str(e))
    html = upload(
        c,
        (
            splitwise_csv("Datum,Beschreibung,Kategorien,Kosten,Währung,Fremd,Bob", ["2026-06-10,x,y,2.00,EUR,1,-1"]),
            "fremd_2026-06-30.csv",
        ),
    )
    check(
        "via /import: FEHLER with hint about the header",
        "nicht im Header" in err(html, "fremd_2026-06-30.csv"),
        flashes(html, "fremd"),
    )

    # ============================================================ B) data_cutoff
    print("\nB1) data_cutoff: older account entries are removed, Splitwise stays")
    tmp, c = fresh_db()
    con = m.db()
    m.set_setting(con, "data_cutoff", "2026-06-01")
    con.commit()
    con.close()
    BANK2 = george_bank(
        [("15.05.2026", "AltBank", "vor Stichtag", "-5,00"), ("02.06.2026", "NeuBank", "nach Stichtag", "-6,00")]
    )
    PP2 = paypal_akt(
        [
            ("20.05.2026", "AltPP", "Zahlung", "EUR", "-7,00", "P1"),
            ("03.06.2026", "NeuPP", "Zahlung", "EUR", "-8,00", "P2"),
        ]
    )
    CARD2 = george_card([("10.05.2026", "AltKarte", "-9,00"), ("04.06.2026", "NeuKarte", "-10,00")])
    SW2 = splitwise_csv(
        SW_DE_HEAD,
        ["2026-05-05,bier_alt,Alkohol,6.00,EUR,-3.00,3.00", "2026-06-05,bier_neu,Alkohol,8.00,EUR,-4.00,4.00"],
    )
    html = upload(
        c,
        (BANK2, "Umsaetze_2026-06-30.csv"),
        (PP2, "Download.CSV"),
        (CARD2, "Kartenkonto_1234_2026-06-01.csv"),
        (SW2, "wg_2026-06-30.csv"),
    )
    check(
        "message: 3 payments before tracking start (2026-06-01) removed",
        steps(html).get("clean") == 3 and "Auswertung (2026-06-01)" in html,
        flashes(html),
    )
    check(
        "old rows bank/paypal/card gone",
        count("partner IN ('AltBank','AltPP','AltKarte')") == 0,
        q("SELECT partner FROM transactions WHERE partner LIKE 'Alt%'"),
    )
    check("new rows present", count("partner IN ('NeuBank','NeuPP','NeuKarte')") == 3)
    sw2 = {r["description"]: r for r in q("SELECT * FROM transactions WHERE source='splitwise'")}
    check(
        "Splitwise before cutoff stays (status info, not promoted)",
        sw2["bier_alt"]["status"] == "info",
        sw2["bier_alt"]["status"],
    )
    check("Splitwise after cutoff promoted", sw2["bier_neu"]["status"] == "active", sw2["bier_neu"]["status"])
    con = m.db()
    ts = m.tracking_start(con)
    con.close()
    check("tracking_start = max(min bank, cutoff) = 2026-06-02", ts == "2026-06-02", ts)
    html = upload(
        c,
        (BANK2, "Umsaetze_2026-06-30.csv"),
        (PP2, "Download.CSV"),
        (CARD2, "Kartenkonto_1234_2026-06-01.csv"),
        (SW2, "wg_2026-06-30.csv"),
    )
    check(
        "re-import: old rows arrive as 'new' again and are removed right away (same end state)",
        has(html, "bank", 1, 1)
        and "clean" in steps(html)
        and count("partner LIKE 'Alt%'") == 0
        and count("partner LIKE 'Neu%'") == 3,
        flashes(html),
    )
    print("  INFO the re-import reports: %s" % flashes(html))

    print("\nB2) Cutoff as lower bound: first bank entry LATER than the cutoff wins")
    tmp, c = fresh_db()
    con = m.db()
    m.set_setting(con, "data_cutoff", "2026-06-01")
    con.commit()
    con.close()
    html = upload(
        c,
        (george_bank([("10.06.2026", "SpaetBank", "x", "-1,00")]), "b.csv"),
        (
            paypal_akt(
                [
                    ("05.06.2026", "FruehPP", "Zahlung", "EUR", "-2,00", "F1"),
                    ("12.06.2026", "SpaetPP", "Zahlung", "EUR", "-3,00", "F2"),
                ]
            ),
            "Download.CSV",
        ),
    )
    check(
        "PayPal row between cutoff and first bank entry is removed (documented floor semantics)",
        count("partner='FruehPP'") == 0 and count("partner='SpaetPP'") == 1 and "Auswertung (2026-06-10)" in html,
        flashes(html),
    )

    print("\nB3) Setting the cutoff afterwards -> the next import cleans up")
    tmp, c = fresh_db()
    upload(c, (george_bank([("01.04.2026", "April", "x", "-1,00"), ("01.07.2026", "Juli", "x", "-2,00")]), "b.csv"))
    check("without cutoff both present", count("partner IN ('April','Juli')") == 2)
    r = c.post(
        "/einstellungen",
        data={"action": "general", "data_cutoff": "2026-06-01", "shared_name": "", "income_category": "Gehalt"},
    )
    con = m.db()
    check("cutoff stored", m.get_setting(con, "data_cutoff") == "2026-06-01", m.get_setting(con, "data_cutoff"))
    con.close()
    html = upload(c, (george_bank([("02.07.2026", "Juli2", "x", "-3,00")]), "b2.csv"))
    check(
        "April row removed at the next import",
        count("partner='April'") == 0 and count("partner IN ('Juli','Juli2')") == 2,
        flashes(html),
    )
    check(
        "tombstone stays: status=deleted before cutoff is NOT deleted",
        (
            lambda: (
                lambda con: (
                    con.execute(
                        "INSERT INTO transactions(source,booking_date,partner,description,amount,raw_hash,status) "
                        "VALUES('bank','2026-03-01','Tomb','x',-1,'tomb1','deleted')"
                    ),
                    con.commit(),
                    m.cleanup_before_start(con),
                    con.commit(),
                    con.close(),
                )[2]
            )(m.db())
        )()
        == 0
        and count("raw_hash='tomb1'") == 1,
    )

    print("\nB4) Splitwise before the cutoff still counts in the balance")
    con = m.db()
    m.set_setting(con, "data_cutoff", "2026-06-01")
    m.set_setting(con, "splitwise_name", "Anna Muster")
    con.commit()
    n, s, ids, rem = m.import_splitwise(
        io.BytesIO(
            splitwise_csv(
                SW_DE_HEAD,
                ["2025-01-01,alt,Sonstiges,100.00,EUR,50.00,-50.00", "2026-07-01,neu,Sonstiges,10.00,EUR,-5.00,5.00"],
            )
        ),
        "wg",
    )
    ov = m.splitwise_overview(con)
    con.close()
    g = ov.get("wg") if isinstance(ov, dict) else next((x for x in ov if x.get("name") == "wg"), None)
    saldo = g["saldo"] if g else None
    check(
        "group balance includes the old row (50 - 5 = 45)",
        saldo is not None and abs(saldo - 45) < 0.01,
        (type(ov).__name__, saldo, g),
    )

    print("\nB5) Card statement: export already contains items AFTER the statement date")
    tmp, c = fresh_db()
    CARDX = george_card(
        [
            ("17.09.2026", "ADOBE", "-35,99"),
            ("18.09.2026", "NETFLIX", "-13,99"),
            ("28.09.2026", "AIRLINE", "-147,15"),
            ("28.09.2026", "OURA", "-5,99"),
            ("29.09.2026", "WOLT", "-14,03"),  # already belong to the October statement,
            ("30.09.2026", "RYANAIR", "-532,08"),  # but fall within the window of the September debit
        ]
    )
    BANKX = george_bank(
        [
            ("01.09.2026", "Bank AG", "Kontofuehrung", "-0,50"),
            ("05.10.2026", "Erste Bank", "s Kreditkartenrechnung Sep. 2026 Premiumcard", "-203,12"),
        ]
    )
    html = upload(c, (BANKX, "Umsaetze_2026-10-10.csv"), (CARDX, "Kartenkonto_1234_2026-10-10.csv"))
    kk = one(
        "SELECT t.status, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id "
        "WHERE t.partner='Erste Bank'"
    )
    check(
        "prefix up to 28.09. (203,12) netted although 29./30.09. fall within the window",
        kk["status"] == "excluded" and kk["cat"] == "Kreditkarten-Verrechnung",
        (kk, flashes(html)),
    )
    mc = q("SELECT t.partner FROM matches m JOIN transactions t ON t.id=m.other_tx WHERE m.kind='card'")
    check(
        "exactly the four items up to the statement date linked",
        len(mc) == 4 and {x["partner"] for x in mc} == {"ADOBE", "NETFLIX", "AIRLINE", "OURA"},
        mc,
    )
    check(
        "items after the statement date stay open and active",
        count("source='card' AND status='active' AND partner IN ('WOLT','RYANAIR')") == 2,
    )
    check("step card=1", steps(html).get("card") == 1, steps(html))
    html = upload(
        c,
        (
            george_bank([("05.11.2026", "Erste Bank", "s Kreditkartenrechnung Okt. 2026 Premiumcard", "-546,11")]),
            "Umsaetze_2026-11-10.csv",
        ),
    )
    mc2 = q("SELECT t.partner, m.bank_tx FROM matches m JOIN transactions t ON t.id=m.other_tx WHERE m.kind='card'")
    check(
        "October statement (546,11) takes exactly the rest WOLT+RYANAIR",
        len(mc2) == 6
        and len({x["bank_tx"] for x in mc2}) == 2
        and {x["partner"] for x in mc2 if x["partner"] in ("WOLT", "RYANAIR")} == {"WOLT", "RYANAIR"},
        mc2,
    )
    check("both statements excluded", count("partner='Erste Bank' AND status='excluded'") == 2)
    # A day is never split: two items on the same day (10 + 20), statement of 10 -> no match,
    # statement of 30 -> both. No free guessing of partial sums.
    html = upload(
        c,
        (
            george_card([("03.11.2026", "X1", "-10,00"), ("03.11.2026", "X2", "-20,00")]),
            "Kartenkonto_1234_2026-12-01.csv",
        ),
        (
            george_bank(
                [
                    ("05.12.2026", "Erste Bank", "s Kreditkartenrechnung Nov. 2026 Premiumcard", "-10,00"),
                    ("06.12.2026", "Erste Bank", "s Kreditkartenrechnung Nov. 2026 Premiumcard X", "-30,00"),
                ]
            ),
            "Umsaetze_2026-12-10.csv",
        ),
    )
    check(
        "statement 10,00 stays active (day 03.11. is not split)",
        one("SELECT status FROM transactions WHERE partner='Erste Bank' AND booking_date='2026-12-05'")["status"]
        == "active",
        flashes(html),
    )
    check(
        "statement 30,00 takes both items of the day",
        one("SELECT status FROM transactions WHERE partner='Erste Bank' AND booking_date='2026-12-06'")["status"]
        == "excluded"
        and count("id IN (SELECT other_tx FROM matches WHERE kind='card') AND partner IN ('X1','X2')") == 2,
    )
    finish()
