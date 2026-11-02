"""Import pipeline: every file format via POST /import, idempotency, force_kind."""

import io
import json
import os

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_import_formats():

    tmp, c = fresh_db()

    flashes = result_lines

    # ------------------------------------------------------------ 1) George bank statement (UTF-16, ;)
    print("1) George bank statement (UTF-16 BOM, semicolon)")
    BANK1 = george_bank(
        [
            ("01.12.2025", "Bank AG", "Kontofuehrung", "-0,50"),  # earliest bank entry = tracking_start
            ("01.06.2026", "BILLA DANKT 0001234", "POS 12,34 AT K1 01.06. 10:11", "-12,34"),
            ("02.06.2026", "Hausverwaltung Müller", "Miete Juni", "-1.234,56"),
            ("2026-06-03", "ACME Corp", "Gehalt 06/2026", "2500,00"),
            ("04.06.2026", "Amazon", "E-COMM 99,99 LU K1", "-99.99"),
            ("05.06.26", "Kaffee", "Espresso", "-3,20"),
        ]
    )
    html = upload(c, (BANK1, "Umsaetze_Girokonto_2026-06-30.csv"))
    check("response reports bank statement 6 new", has(html, "bank", 6, 0), flashes(html))
    check("6 bank rows in DB", count("source='bank'") == 6, count("source='bank'"))
    rows = {r["partner"]: r for r in q("SELECT * FROM transactions WHERE source='bank'")}
    check("umlaut in partner preserved", "Hausverwaltung Müller" in rows, list(rows))
    check(
        "amount '-1.234,56' -> -1234.56",
        abs(rows["Hausverwaltung Müller"]["amount"] + 1234.56) < 0.001,
        rows["Hausverwaltung Müller"]["amount"],
    )
    check("amount '-99.99' -> -99.99", abs(rows["Amazon"]["amount"] + 99.99) < 0.001, rows["Amazon"]["amount"])
    check("amount '2500,00' -> 2500", abs(rows["ACME Corp"]["amount"] - 2500) < 0.001)
    check("date dd.mm.yyyy", rows["BILLA DANKT 0001234"]["booking_date"] == "2026-06-01")
    check("date yyyy-mm-dd", rows["ACME Corp"]["booking_date"] == "2026-06-03")
    check("date dd.mm.yy", rows["Kaffee"]["booking_date"] == "2026-06-05", rows["Kaffee"]["booking_date"])
    check(
        "booking details stored as description",
        rows["Hausverwaltung Müller"]["description"] == "Miete Juni",
        rows["Hausverwaltung Müller"]["description"],
    )
    check("currency EUR", rows["Amazon"]["currency"] == "EUR")
    check(
        "rule BILLA -> Lebensmittel applies",
        one("SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.partner LIKE 'BILLA%'")[
            "name"
        ]
        == "Lebensmittel",
    )
    check(
        "hash basis bank=(d,a,partner,desc)",
        rows["Amazon"]["raw_hash"] == m.tx_hash("bank", "2026-06-04", -99.99, "Amazon", "E-COMM 99,99 LU K1"),
    )

    print("\n1b) English bank statement (UTF-8, comma)")
    BANK_EN = english_bank(
        [
            ("2026-06-10", "-45.10", "Tesco Store", "groceries weekly"),
            ("2026-06-11", "-1,234.56", "Landlord Ltd", "rent june"),
            ("12.06.2026", "1000", "Employer", "salary"),
        ]
    )
    html = upload(c, (BANK_EN, "statement.csv"))
    check(
        "English file detected as bank statement",
        fr(html, "statement.csv")[:2] == ("bank", 3),
        flashes(html, "statement.csv"),
    )
    en = {r["partner"]: r for r in q("SELECT * FROM transactions WHERE source='bank' AND booking_date>='2026-06-10'")}
    check("Name column used as partner", "Tesco Store" in en and "Landlord Ltd" in en, list(en))
    check(
        "'-1,234.56' -> -1234.56",
        "Landlord Ltd" in en and abs(en["Landlord Ltd"]["amount"] + 1234.56) < 0.001,
        en.get("Landlord Ltd", {}).get("amount"),
    )
    check(
        "Description column taken over (English)",
        "Tesco Store" in en and en["Tesco Store"]["description"] == "groceries weekly",
        repr(en.get("Tesco Store", {}).get("description")),
    )

    # ------------------------------------------------------------ 2) Card account
    print("\n2) George card account (own IBAN empty)")
    CARD1 = george_card(
        [
            ("20.05.2026", "SPOTIFY AB", "-10,99"),
            ("25.05.2026", "AMZN Mktp DE", "-15,50"),
            ("28.05.2026", "Ihre Zahlung Vormonat - Danke", "400,00"),
        ]
    )
    html = upload(c, (CARD1, "Kartenkonto_1234_2026-06-01.csv"))
    check("detected as credit card, 2 new", has(html, "card", 2, 0), flashes(html, "Kartenkonto"))
    card = q("SELECT * FROM transactions WHERE source='card' ORDER BY booking_date")
    check("2 card rows (previous-month credit skipped)", len(card) == 2, len(card))
    check(
        "merchant from booking details as partner",
        bool(card) and card[0]["partner"] == "SPOTIFY AB",
        card and card[0]["partner"],
    )
    check(
        "card label with digits 1234 in hash",
        bool(card) and card[0]["raw_hash"] == m.tx_hash("card", "Karte •1234", "2026-05-20", -10.99, "SPOTIFY AB"),
    )
    check("is_card_account direct", m.is_card_account(GEORGE_HEAD.split(";"), [["x", "", "", "y", "-1", "EUR", ""]]))
    check(
        "is_card_account False with filled IBAN",
        not m.is_card_account(GEORGE_HEAD.split(";"), [["x", "", "", "y", "-1", "EUR", MY_IBAN]]),
    )
    n_new = m.import_card(io.BytesIO(george_card([("01.05.2026", "X", "-1,00")])), "karte.csv")[0]
    check(
        "card label without digits in name = 'Karte'",
        n_new == 1
        and one("SELECT raw_hash FROM transactions WHERE source='card' AND partner='X'")["raw_hash"]
        == m.tx_hash("card", "Karte", "2026-05-01", -1.0, "X"),
    )

    # ------------------------------------------------------------ 3) PayPal, both formats
    print("\n3) PayPal activities (type column, DE date)")
    PP1 = paypal_akt(
        [
            ("01.06.2026", "Netflix", "Zahlung", "EUR", "-29,99", "TX0001"),
            ("02.06.2026", "", "Bankgutschrift auf PayPal-Konto", "EUR", "29,99", "TX0002"),
            ("03.06.2026", "Steam", "Website-Zahlung", "EUR", "-5,00", "TX0003"),
            ("03.06.2026", "Steam", "Website-Zahlung", "EUR", "0,00", "TX0004"),
        ]
    )
    html = upload(c, (PP1, "Download.CSV"))
    check(
        "PayPal detected, 2 new (credit + zero amount skipped)",
        fr(html, "Download.CSV") == ("paypal", 2, 0, 0),
        flashes(html, "Download.CSV"),
    )
    pp = {r["partner"]: r for r in q("SELECT * FROM transactions WHERE source='paypal'")}
    check("Netflix row with DE date", pp.get("Netflix", {}).get("booking_date") == "2026-06-01")
    check("type lands in description", pp.get("Netflix", {}).get("description") == "Zahlung")
    check("hash over transaction code", pp["Netflix"]["raw_hash"] == m.tx_hash("paypal", "TX0001"))
    check("extra.code stored", json.loads(pp["Netflix"]["extra"]).get("code") == "TX0001")

    print("\n3b) PayPal CSR (no type column, US date M/D/Y)")
    PP2 = paypal_csr(
        [
            ("3/1/2026", "Adobe", "Abbuchung", "EUR", "-12,99", "CSR001"),
            ("12/25/2025", "Epic Games", "Abbuchung", "EUR", "-19,99", "CSR002"),
            ("3/2/2026", "", "Bankgutschrift auf PayPal-Konto", "EUR", "12,99", "CSR003"),
        ]
    )
    html = upload(c, (PP2, "TransactionReport.csv"))
    check(
        "CSR detected as PayPal, 2 new",
        fr(html, "TransactionReport.csv")[:2] == ("paypal", 2),
        flashes(html, "TransactionReport"),
    )
    adobe = one("SELECT * FROM transactions WHERE partner='Adobe'")
    check(
        "3/1/2026 = March 1 (not January 3)",
        adobe is not None and adobe["booking_date"] == "2026-03-01",
        adobe and adobe["booking_date"],
    )
    epic = one("SELECT * FROM transactions WHERE partner='Epic Games'")
    check(
        "12/25/2025 = December 25",
        epic is not None and epic["booking_date"] == "2025-12-25",
        epic and epic["booking_date"],
    )
    check("description serves as type", adobe["description"] == "Abbuchung")
    check("parse_date('3/1/2026') direct", m.parse_date("3/1/2026") == "2026-03-01")
    check("parse_date('1/3/2026') = January 3", m.parse_date("1/3/2026") == "2026-01-03")
    check("parse_date with time of day", m.parse_date("01.06.2026 10:00:00") == "2026-06-01")

    print("\n3c) PayPal English CSR header (Transaction ID / Gross)")
    PP3 = (
        b"Transaction ID,Date,Name,Description,Currency,Gross\nEN001,6/15/2026,Dropbox,PreApproved Payment,EUR,-11.99\n"
    )
    html = upload(c, (PP3, "en_report.csv"))
    check("English PayPal header detected", fr(html, "en_report.csv")[:2] == ("paypal", 1), flashes(html, "en_report"))
    dbx = one("SELECT * FROM transactions WHERE partner='Dropbox'")
    check(
        "6/15/2026 -> 2026-06-15", dbx is not None and dbx["booking_date"] == "2026-06-15", dbx and dbx["booking_date"]
    )

    # ------------------------------------------------------------ 4) Splitwise DE + EN
    print("\n4) Splitwise DE header")
    SW_DE = splitwise_csv(
        SW_DE_HEAD,
        [
            "2026-06-01,miete,Miete,900.00,EUR,450.00,-450.00",
            "2026-06-05,bier,Alkohol,20.00,EUR,-10.00,10.00",
            "2026-06-07,Spar,Lebensmittel,30.00,EUR,-15.00,15.00",
            "2026-06-08,Bob zahlte Anna Muster,Zahlung,25.00,EUR,-25.00,25.00",
        ],
        total=",Gesamtsaldo,,,EUR,400.00,-400.00",
    )
    html = upload(c, (SW_DE, "wg_2026-06-30.csv"))
    check("Splitwise group from filename = 'wg', 4 new", gr(html, "wg") == (4, 0, 0), flashes(html, "wg_2026"))
    sw = {r["description"]: r for r in q("SELECT * FROM transactions WHERE source='splitwise' AND partner='wg'")}
    check("Gesamtsaldo row not imported", "Gesamtsaldo" not in sw and len(sw) == 4, list(sw))
    check("own share (bal) as amount", abs(sw["bier"]["amount"] + 10) < 0.001)
    ex = json.loads(sw["bier"]["extra"])
    check("extra: cost/bal/members", ex["cost"] == 20 and ex["bal"] == -10 and ex["members"] == ["Bob"], ex)
    check("settlement payment detected (is_payment)", json.loads(sw["Bob zahlte Anna Muster"]["extra"])["is_payment"])
    check(
        "hash basis splitwise=(group,d,cost,desc,cat,bal,occ)",
        sw["bier"]["raw_hash"] == m.tx_hash("splitwise", "wg", "2026-06-05", 20.0, "bier", "Alkohol", -10.0, 1),
    )
    check("bal<0 is promoted (active)", sw["bier"]["status"] == "active", sw["bier"]["status"])
    check("note 'bezahlt von Bob' (2-person group)", sw["bier"]["note"] == "bezahlt von Bob", sw["bier"]["note"])
    check(
        "bal>0 without cash pool -> share credit (active, +450, documented behavior)",
        sw["miete"]["status"] == "active"
        and sw["miete"]["note"].startswith("Anteil-Gutschrift")
        and abs(sw["miete"]["amount"] - 450) < 0.001,
        (sw["miete"]["status"], sw["miete"]["note"]),
    )

    print("\n4b) Splitwise EN header with 'Anna Muster (removed)'")
    SW_EN = splitwise_csv(
        SW_EN_HEAD,
        [
            "2026-07-01,pizza,Food and drink,24.00,EUR,-12.00,12.00",
            "2026-07-02,taxi,Transportation,18.00,EUR,9.00,-9.00",
        ],
        total=",Total balance,,,EUR,-3.00,3.00",
    )
    html = upload(c, (SW_EN, "trip_2026-07-31.csv"))
    check("EN header detected, group 'trip', 2 new", gr(html, "trip") == (2, 0, 0), flashes(html, "trip_2026"))
    tr = {r["description"]: r for r in q("SELECT * FROM transactions WHERE source='splitwise' AND partner='trip'")}
    check("Total balance not imported", set(tr) == {"pizza", "taxi"}, list(tr))
    check(
        "(removed) column recognized as own: pizza bal=-12",
        abs(tr["pizza"]["amount"] + 12) < 0.001,
        tr["pizza"]["amount"],
    )
    check(
        "members without me",
        json.loads(tr["pizza"]["extra"])["members"] == ["Bob"],
        json.loads(tr["pizza"]["extra"])["members"],
    )
    check("detect_kind EN Splitwise", m.detect_kind(SW_EN_HEAD.split(",")) == "splitwise")
    check("detect_kind DE Splitwise", m.detect_kind(SW_DE_HEAD.split(",")) == "splitwise")
    check("detect_kind George", m.detect_kind(GEORGE_HEAD.split(";")) == "bank")
    check("detect_kind PayPal activities", m.detect_kind(PP_AKT_HEAD) == "paypal")
    check("detect_kind PayPal CSR", m.detect_kind(PP_CSR_HEAD) == "paypal")
    check("detect_kind English bank", m.detect_kind(["Date", "Amount", "Name", "Description"]) == "bank")

    # ------------------------------------------------------------ 5) Idempotency
    print("\n5) Re-import of every file: 0 new, N known")
    before = count()
    html = upload(
        c,
        (BANK1, "Umsaetze_Girokonto_2026-06-30.csv"),
        (BANK_EN, "statement.csv"),
        (CARD1, "Kartenkonto_1234_2026-06-01.csv"),
        (PP1, "Download.CSV"),
        (PP2, "TransactionReport.csv"),
        (PP3, "en_report.csv"),
        (SW_DE, "wg_2026-06-30.csv"),
        (SW_EN, "trip_2026-07-31.csv"),
    )
    for label, cond in (
        ("Kontoauszug 0 neu / 6 bekannt", has(html, "bank", 0, 6)),
        ("statement.csv 0/3", fr(html, "statement.csv") == ("bank", 0, 3, 0)),
        ("Kreditkarte 0/2", has(html, "card", 0, 2)),
        ("Download.CSV 0/2", fr(html, "Download.CSV") == ("paypal", 0, 2, 0)),
        ("TransactionReport.csv 0/2", fr(html, "TransactionReport.csv") == ("paypal", 0, 2, 0)),
        ("en_report.csv 0/1", fr(html, "en_report.csv") == ("paypal", 0, 1, 0)),
        ("Gruppe wg 0/4", gr(html, "wg") == (0, 4, 0)),
        ("Gruppe trip 0/2", gr(html, "trip") == (0, 2, 0)),
    ):
        check("Re-Import: " + label, cond, flashes(html))
    check("no row added", count() == before, (before, count()))
    check("no 'removed' message for Splitwise", "gelöschte Position" not in html)
    check(
        "Splitwise status unchanged (bier stays active)",
        one("SELECT status FROM transactions WHERE description='bier'")["status"] == "active",
    )

    print("\n5b) Overlapping periods (bank)")
    BANK_OVL = george_bank(
        [
            ("04.06.2026", "Amazon", "E-COMM 99,99 LU K1", "-99.99"),  # identical to BANK1
            ("05.06.2026", "Kaffee", "Espresso", "-3,20"),  # identical (different date format!)
            ("06.06.2026", "Neu GmbH", "Erst im zweiten Export", "-7,00"),
        ]
    )
    html = upload(c, (BANK_OVL, "Umsaetze_Girokonto_2026-07-15.csv"))
    check("overlapping: 1 new, 2 known", has(html, "bank", 1, 2), flashes(html, "2026-07-15"))
    check(
        "Kaffee row not duplicated (dd.mm.yy vs dd.mm.yyyy same hash)",
        count("partner='Kaffee'") == 1,
        count("partner='Kaffee'"),
    )

    print("\n5c) Overlapping card exports with the same label")
    CARD_OVL = george_card([("25.05.2026", "AMZN Mktp DE", "-15,50"), ("02.06.2026", "UBER", "-8,00")])
    html = upload(c, (CARD_OVL, "Kartenkonto_1234_2026-07-01.csv"))
    check("card: 1 new, 1 known", has(html, "card", 1, 1), flashes(html, "2026-07-01"))
    CARD_OTHER = george_card([("25.05.2026", "AMZN Mktp DE", "-15,50")])
    html = upload(c, (CARD_OTHER, "Kartenkonto_9999_2026-07-01.csv"))
    check("other card (9999): same item is NEW (label in hash)", has(html, "card", 1, 0), flashes(html, "9999"))

    # ------------------------------------------------------------ 6) force_kind
    print("\n6) force_kind")
    UNK = b"Foo,Bar,Baz\n1,2,3\n"
    html = upload(c, (UNK, "unknown.csv"))
    check(
        "unknown header -> FEHLER 'Format nicht erkannt'",
        "Format nicht erkannt" in err(html, "unknown.csv"),
        file_rows(html),
    )
    html = upload(c, (UNK, "unknown.csv"), force_kind="bank")
    check(
        "force_kind=bank on unparsable file -> FEHLER with column hint",
        "keine Spalte" in err(html, "unknown.csv"),
        file_rows(html),
    )
    # detection fails (neither 'datum' nor 'betrag'), but the importer can still read it
    SEMI = b"Buchung;Amount;Name;Text\n01.07.2026;-4,50;Baeckerei;Semmeln\n"
    check(
        "detect_kind for 'Buchung;Amount' = None",
        m.detect_kind(["Buchung", "Amount", "Name", "Text"]) is None,
        m.detect_kind(["Buchung", "Amount", "Name", "Text"]),
    )
    html = upload(c, (SEMI, "semi.csv"))
    check("without force_kind: format not detected", "Format nicht erkannt" in err(html, "semi.csv"))
    check("not imported yet", count("partner='Baeckerei'") == 0)
    html = upload(c, (SEMI, "semi.csv"), force_kind="bank")
    check("imported with force_kind=bank", fr(html, "semi.csv") == ("bank", 1, 0, 0), flashes(html, "semi.csv"))
    check("Baeckerei as bank in DB", count("partner='Baeckerei' AND source='bank'") == 1)
    # English card export of another bank: detected as bank, force_kind=card overrides
    CARD_EN = english_bank([("2026-06-20", "-22.00", "H&M", "clothes")])
    html = upload(c, (CARD_EN, "creditcard_5678_2026-06-30.csv"), force_kind="card")
    check(
        "force_kind=card on English file",
        fr(html, "creditcard_5678_2026-06-30.csv")[:2] == ("card", 1),
        flashes(html, "creditcard"),
    )
    hm = one("SELECT * FROM transactions WHERE partner='H&M'")
    check(
        "as source=card with label from filename",
        hm is not None
        and hm["source"] == "card"
        and hm["raw_hash"] == m.tx_hash("card", "Karte •5678", "2026-06-20", -22.0, hm["description"]),
        hm and (hm["source"], hm["raw_hash"], hm["description"]),
    )
    # a PayPal file without transaction code/gross would be detected as bank -> force paypal
    PP_MIN = 'Datum,Name,Typ,Währung,Betrag\n01.07.2026,Patreon,Zahlung,EUR,"-3,00"\n'.encode()
    check(
        "detect_kind 'Datum,Name,Typ,Betrag' = bank",
        m.detect_kind(["Datum", "Name", "Typ", "Währung", "Betrag"]) == "bank",
    )
    html = upload(c, (PP_MIN, "pp_min.csv"), force_kind="paypal")
    check(
        "force_kind=paypal imports as PayPal",
        fr(html, "pp_min.csv") == ("paypal", 1, 0, 0),
        flashes(html, "pp_min"),
    )
    check("Patreon source=paypal", one("SELECT source FROM transactions WHERE partner='Patreon'")["source"] == "paypal")
    html = upload(c, (UNK, "unknown.csv"), force_kind="bogus")
    check(
        "invalid force_kind is ignored -> format not detected",
        "Format nicht erkannt" in err(html, "unknown.csv"),
    )
    r = c.post("/import", data={}, content_type="multipart/form-data", follow_redirects=True)
    check("no upload -> 'no files chosen' message", "Keine Dateien gewählt" in r.get_data(as_text=True))

    check(
        "no archiv/ folder anymore (feature removed 2026-09-15)",
        not os.path.isdir(os.path.join(tmp, "archiv")),
        os.listdir(tmp),
    )
    finish()
