"""Import pipeline: edge cases (empty file, encodings, amounts, dates, income rule)."""

import io
import json
import os

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_import_edge():

    tmp, c = fresh_db()

    flashes = result_lines

    # earliest bank entry so that cleanup_before_start removes nothing
    upload(c, (george_bank([("01.01.2025", "Bank AG", "Kontofuehrung", "-0,50")]), "start.csv"))
    base = count()

    print("1) Empty file / header only / blank lines")
    html = upload(c, (b"", "empty.csv"))
    check("empty file -> FEHLER 'Datei ist leer'", "Datei ist leer" in err(html, "empty.csv"), flashes(html, "empty"))
    html = upload(c, (b"\r\n\r\n   \r\n", "blank.csv"))
    check(
        "only blank lines -> FEHLER 'Datei ist leer'",
        "Datei ist leer" in err(html, "blank.csv"),
        flashes(html, "blank"),
    )
    html = upload(c, (george_bank([]), "headonly.csv"))
    check(
        "header only -> bank statement 0 new", fr(html, "headonly.csv") == ("bank", 0, 0, 0), flashes(html, "headonly")
    )
    raw = george_bank([("01.02.2025", "A", "x", "-1,00"), ("02.02.2025", "B", "y", "-2,00")])
    txt = raw.decode("utf-16").replace("\r\n", "\r\n\r\n;;;;;;\r\n", 1) + "\r\n\r\n   \r\n"
    html = upload(c, (txt.encode("utf-16"), "blanklines.csv"))
    check(
        "blank lines/empty separator lines in between -> 2 new",
        fr(html, "blanklines.csv")[:2] == ("bank", 2),
        flashes(html, "blanklines"),
    )
    check("DB: A and B present", count("partner IN ('A','B')") == 2)

    print("\n2) Amount 0, missing date, short row")
    html = upload(
        c,
        (
            george_bank(
                [
                    ("03.02.2025", "Null", "Betrag null", "0,00"),
                    ("", "OhneDatum", "kein Datum", "-5,00"),
                    ("kein datum", "KaputtesDatum", "x", "-5,00"),
                    ("04.02.2025", "OhneBetrag", "kein Betrag", ""),
                    ("05.02.2025", "abc", "Text statt Zahl", "abc"),
                ]
            ),
            "zero.csv",
        ),
    )
    check(
        "rows without date/amount are skipped silently (neither new nor known)",
        fr(html, "zero.csv") == ("bank", 1, 0, 0),
        flashes(html, "zero"),
    )
    check(
        "bank row with amount 0 is imported (no skip as with PayPal)",
        count("partner='Null'") == 1 and one("SELECT amount FROM transactions WHERE partner='Null'")["amount"] == 0,
    )
    check("rows without date not in DB", count("partner IN ('OhneDatum','KaputtesDatum','OhneBetrag','abc')") == 0)
    short = (GEORGE_HEAD + "\r\n06.02.2025;Kurz;AT00;nur vier Spalten\r\n07.02.2025;Kurz2;AT00;text;-3,00\r\n").encode(
        "utf-16"
    )
    html = upload(c, (short, "short.csv"))
    check(
        "too-short row (no amount column) skipped, the other one arrives",
        fr(html, "short.csv")[:2] == ("bank", 1),
        flashes(html, "short"),
    )
    check(
        "Kurz2 without currency column -> EUR",
        one("SELECT currency FROM transactions WHERE partner='Kurz2'")["currency"] == "EUR",
    )

    print("\n3) Encodings")
    lat = (
        "Buchungsdatum;Partnername;Partner IBAN;Buchungs-Details;Betrag;W\xe4hrung;Eigene IBAN\r\n"
        "10.02.2025;B\xe4ckerei M\xfcller;AT00;Semmeln f\xfcr 3,50 €;-3,50;EUR;AT1\r\n"
    ).encode("cp1252")
    html = upload(c, (lat, "latin.csv"))
    check("cp1252/latin-1 file is imported", fr(html, "latin.csv")[:2] == ("bank", 1), flashes(html, "latin"))
    r = one("SELECT * FROM transactions WHERE partner LIKE 'B%ckerei%'")
    check(
        "umlauts decoded correctly (Baeckerei Mueller)",
        r is not None and r["partner"] == "Bäckerei Müller",
        r and r["partner"],
    )
    check("euro sign (0x80 in cp1252) correct", r is not None and "€" in r["description"], r and r["description"])
    bom8 = ("﻿" + GEORGE_HEAD + "\r\n11.02.2025;BomTest;AT00;utf-8 mit BOM;-1,10;EUR;AT1\r\n").encode("utf-8")
    html = upload(c, (bom8, "bom8.csv"))
    check(
        "UTF-8 with BOM: header column detected despite BOM",
        fr(html, "bom8.csv")[:2] == ("bank", 1),
        flashes(html, "bom8"),
    )
    check(
        "BomTest date correct (BOM not in date field)",
        one("SELECT booking_date FROM transactions WHERE partner='BomTest'")["booking_date"] == "2025-02-11",
    )
    be = ("﻿" + GEORGE_HEAD + "\r\n12.02.2025;BigEndian;AT00;utf-16-be;-1,20;EUR;AT1\r\n").encode("utf-16-be")
    html = upload(c, (be, "utf16be.csv"))
    check("UTF-16 BE with BOM", fr(html, "utf16be.csv")[:2] == ("bank", 1), flashes(html, "utf16be"))
    tsv = (
        "Buchungsdatum\tPartnername\tBuchungs-Details\tBetrag\tWährung\n13.02.2025\tTabTest\tmit Tabs\t-1,30\tEUR\n"
    ).encode()
    html = upload(c, (tsv, "tabs.tsv"))
    check("tab-separated is detected", fr(html, "tabs.tsv")[:2] == ("bank", 1), flashes(html, "tabs"))
    check(
        "TabTest amount -1.30",
        abs(one("SELECT amount FROM transactions WHERE partner='TabTest'")["amount"] + 1.3) < 0.001,
    )
    quoted = b'Date,Amount,Name,Description\n2025-02-14,-2.00,Quoted,"Zeile mit\nUmbruch, Komma und ""Anfuehrung"""\n'
    html = upload(c, (quoted, "quoted.csv"))
    check(
        "field with line break/comma inside quotes",
        fr(html, "quoted.csv")[:2] == ("bank", 1),
        flashes(html, "quoted"),
    )

    print("\n4) Amounts")
    html = upload(
        c,
        (
            george_bank(
                [
                    ("15.02.2025", "Gross1", "de", "-1.234.567,89"),
                    ("15.02.2025", "Gross2", "us", "-1,234,567.89"),
                    ("15.02.2025", "Gross3", "plain", "-1234567.89"),
                    ("15.02.2025", "Minus", "unicode minus", "−12,00"),
                    ("15.02.2025", "Euro", "mit Euro", "-12,00 €"),
                    ("15.02.2025", "Space", "Leerzeichen", "- 1 234,50"),
                    ("15.02.2025", "Plus", "explizites Plus", "+12,00"),
                ]
            ),
            "amounts.csv",
        ),
    )
    check("7 amount variants imported", fr(html, "amounts.csv")[:2] == ("bank", 7), flashes(html, "amounts"))
    am = {
        r["partner"]: r["amount"] for r in q("SELECT partner, amount FROM transactions WHERE booking_date='2025-02-15'")
    }
    check("'-1.234.567,89' -> -1234567.89", abs(am.get("Gross1", 0) + 1234567.89) < 0.001, am.get("Gross1"))
    check("'-1,234,567.89' -> -1234567.89", abs(am.get("Gross2", 0) + 1234567.89) < 0.001, am.get("Gross2"))
    check("'-1234567.89' -> -1234567.89", abs(am.get("Gross3", 0) + 1234567.89) < 0.001, am.get("Gross3"))
    check("Unicode minus", abs(am.get("Minus", 0) + 12) < 0.001, am.get("Minus"))
    check("'-12,00' with euro sign", abs(am.get("Euro", 0) + 12) < 0.001, am.get("Euro"))
    check("'- 1 234,50'", abs(am.get("Space", 0) + 1234.5) < 0.001, am.get("Space"))
    check("'+12,00'", abs(am.get("Plus", 0) - 12) < 0.001, am.get("Plus"))
    check(
        "parse_amount('') None / parse_amount('1,5') 1.5 / parse_amount('1.5') 1.5",
        m.parse_amount("") is None and m.parse_amount("1,5") == 1.5 and m.parse_amount("1.5") == 1.5,
    )
    check(
        "parse_amount('1.234') = 1234? (thousands dot without decimals)",
        m.parse_amount("1.234") == 1.234,
        "INFO: '1.234' wird als 1.234 gelesen (Punkt = Dezimaltrenner), nicht 1234 - bei George immer mit ',xx', daher harmlos",
    )
    check("dashboard overview copes with a million-euro amount", c.get("/").status_code == 200)

    print("\n5) Identical rows in ONE bank file")
    html = upload(
        c,
        (
            george_bank(
                [
                    ("16.02.2025", "Wiener Linien", "E-COMM 2,40 AT K1 Ticket", "-2,40"),
                    ("16.02.2025", "Wiener Linien", "E-COMM 2,40 AT K1 Ticket", "-2,40"),
                ]
            ),
            "dup.csv",
        ),
    )
    check(
        "two identical bank rows in the same export are BOTH kept (as with Splitwise via occurrence)",
        fr(html, "dup.csv")[:2] == ("bank", 2) and count("partner='Wiener Linien'") == 2,
        (flashes(html, "dup.csv"), count("partner='Wiener Linien'")),
    )

    print("\n6) PayPal edge cases")
    pp = paypal_akt(
        [
            ("17.02.2025", "Shop UK", "Zahlung", "GBP", "-36,99", "GB1"),
            ("17.02.2025", "", "Währungsumrechnung", "GBP", "36,99", "GB2"),
            ("17.02.2025", "", "Währungsumrechnung", "EUR", "-42,10", "GB3"),
            ("18.02.2025", "Nullbetrag", "Zahlung", "EUR", "0,00", "Z1"),
            ("", "OhneDatum", "Zahlung", "EUR", "-1,00", "Z2"),
            ("19.02.2025", "", "Allgemeine Abbuchung", "EUR", "-50,00", "Z3"),
            ("19.02.2025", "Freund", "Zahlung", "EUR", "20,00", "Z4"),
        ]
    )
    html = upload(c, (pp, "pp_edge.csv"))
    check(
        "PayPal: 2 new (GBP converted + Freund), rest skipped",
        fr(html, "pp_edge.csv") == ("paypal", 2, 0, 0),
        flashes(html, "pp_edge"),
    )
    uk = one("SELECT * FROM transactions WHERE partner='Shop UK'")
    check(
        "GBP payment converted to EUR (-42.10 EUR)",
        uk is not None and uk["currency"] == "EUR" and abs(uk["amount"] + 42.10) < 0.001,
        uk and (uk["currency"], uk["amount"]),
    )
    check("conversion note", uk is not None and "GBP umgerechnet" in uk["note"], uk and uk["note"])
    check("conversion rows themselves not imported", count("partner='PayPal' AND source='paypal'") == 0)
    check("zero amount and missing date not imported", count("partner IN ('Nullbetrag','OhneDatum')") == 0)
    check("Allgemeine Abbuchung skipped", count("description LIKE 'Allgemeine Abbuchung%'") == 0)
    check(
        "incoming PayPal payment (+20) imported",
        abs(one("SELECT amount FROM transactions WHERE partner='Freund'")["amount"] - 20) < 0.001,
    )
    # same transaction code, changed values -> hash is over the code, so 'already present'
    pp2 = paypal_akt([("19.02.2025", "Freund", "Zahlung", "EUR", "21,00", "Z4")])
    html = upload(c, (pp2, "pp_edge2.csv"))
    check(
        "same transaction code with different amount -> already present (no duplicate)",
        fr(html, "pp_edge2.csv") == ("paypal", 0, 1, 0),
        flashes(html, "pp_edge2"),
    )
    ppn = (
        'Datum,Name,Typ,Währung,Brutto\n20.02.2025,OhneCode,Zahlung,EUR,"-4,00"\n20.02.2025,OhneCode,Zahlung,EUR,"-4,00"\n'
    ).encode()
    check(
        "detect_kind Brutto+Typ without code = paypal",
        m.detect_kind(["Datum", "Name", "Typ", "Währung", "Brutto"]) == "paypal",
    )
    html = upload(c, (ppn, "pp_nocode.csv"))
    check(
        "without transaction code: identical rows are BOTH kept (occurrence)",
        fr(html, "pp_nocode.csv") == ("paypal", 2, 0, 0),
        flashes(html, "pp_nocode"),
    )
    html = upload(c, (ppn, "pp_nocode.csv"))
    check(
        "... and the re-import is idempotent",
        fr(html, "pp_nocode.csv") == ("paypal", 0, 2, 0),
        flashes(html, "pp_nocode"),
    )

    print("\n7) Splitwise edge cases")
    sw = splitwise_csv(
        SW_DE_HEAD,
        [
            "2025-03-01,kaffee,Sonstiges,4.00,EUR,-2.00,2.00",
            "2025-03-01,kaffee,Sonstiges,4.00,EUR,-2.00,2.00",  # genuine repeated expense
            "2025-03-02,nicht dabei,Sonstiges,10.00,EUR,,10.00",  # own column empty
            ",ohne datum,Sonstiges,10.00,EUR,-5.00,5.00",
            "2025-03-03,ohne kosten,Sonstiges,,EUR,-5.00,5.00",
        ],
        total=",Gesamtsaldo,,,EUR,-4.00,4.00",
    )
    html = upload(c, (sw, "edge_2025-03-31.csv"))
    check(
        "2 identical rows = 2 new (occurrence), rest skipped",
        gr(html, "edge") == (2, 0, 0),
        flashes(html, "edge_2025"),
    )
    check("both kaffee rows in DB", count("source='splitwise' AND description='kaffee'") == 2)
    html = upload(c, (sw, "edge_2025-03-31.csv"))
    check("re-import: 0 new, 2 known, nothing removed", gr(html, "edge") == (0, 2, 0), flashes(html, "edge_2025"))
    sw_ci = splitwise_csv(
        "Datum,Beschreibung,Kategorien,Kosten,Währung,ANNA MUSTER,Bob",
        ["2025-03-05,gross,Sonstiges,8.00,EUR,-4.00,4.00"],
    )
    n, s, ids, rem = m.import_splitwise(io.BytesIO(sw_ci), "ci")
    check(
        "own name case-insensitive in header",
        n == 1 and abs(one("SELECT amount FROM transactions WHERE partner='ci'")["amount"] + 4) < 0.001,
        (n, s),
    )
    sw_ascii = splitwise_csv(
        "Datum,Beschreibung,Kategorien,Kosten,Waehrung,Anna Muster,Bob",
        ["2025-03-06,ascii,Sonstiges,8.00,EUR,-4.00,4.00"],
    )
    n, s, ids, rem = m.import_splitwise(io.BytesIO(sw_ascii), "ascii")
    mem = json.loads(one("SELECT extra FROM transactions WHERE partner='ascii'")["extra"])["members"]
    print(
        "  INFO header 'Waehrung' (no umlaut): currency column not found -> members=%r (instead of ['Bob']); "
        "the note then reads 'bezahlt in Gruppe' instead of 'bezahlt von Bob'" % (mem,)
    )
    # own name is a substring of another member that comes BEFORE me in the header
    con = m.db()
    m.set_setting(con, "splitwise_name", "Anna")
    con.commit()
    con.close()
    sw_sub = splitwise_csv(
        "Datum,Beschreibung,Kategorien,Kosten,Währung,Annabelle,Anna",
        ["2025-03-07,substring,Sonstiges,10.00,EUR,5.00,-5.00"],
    )
    n, s, ids, rem = m.import_splitwise(io.BytesIO(sw_sub), "sub")
    row = one("SELECT amount, extra FROM transactions WHERE partner='sub'")
    check(
        "Splitwise name 'Anna' does NOT match column 'Annabelle' (own share must be -5)",
        row is not None and abs(row["amount"] + 5) < 0.001,
        "amount=%s members=%s (Spalte 'Annabelle' wurde als eigene genommen: Teilstring-Match nimmt ersten Treffer)"
        % (row and row["amount"], row and json.loads(row["extra"])["members"]),
    )
    con = m.db()
    m.set_setting(con, "splitwise_name", "Anna Muster")
    con.commit()
    con.close()
    html = upload(c, (splitwise_csv(SW_DE_HEAD, ["2025-03-08,x,Sonstiges,2.00,EUR,-1.00,1.00"]), "nodate.csv"))
    check("filename without date -> group 'nodate'", gr(html, "nodate")[0] == 1, flashes(html, "nodate"))

    print("\n8) Income rule from the settings")
    con = m.db()
    m.set_income_rules(con, ["ACME Corp", " ACME Corp ", "", "Zweitjob"])
    con.commit()
    check(
        "income_rule_patterns deduplicates/trims",
        m.income_rule_patterns(con) == ["ACME Corp", "Zweitjob"],
        m.income_rule_patterns(con),
    )
    con.close()
    html = upload(
        c,
        (
            george_bank(
                [
                    ("25.02.2025", "ACME Corp", "Gehalt 02/2025", "2.500,00"),
                    ("26.02.2025", "Nebenbei", "Zweitjob Honorar", "300,00"),
                    ("27.02.2025", "Oma", "Geburtstag", "50,00"),
                ]
            ),
            "income.csv",
        ),
    )
    inc = {
        r["partner"]: r
        for r in q(
            "SELECT t.partner, c.name AS cat, c.kind FROM transactions t "
            "LEFT JOIN categories c ON c.id=t.category_id WHERE t.booking_date BETWEEN '2025-02-25' AND '2025-02-27'"
        )
    }
    check(
        "ACME Corp -> Gehalt (kind income)",
        inc["ACME Corp"]["cat"] == "Gehalt" and inc["ACME Corp"]["kind"] == "income",
        dict(inc["ACME Corp"]),
    )
    check(
        "pattern in the purpose text (Zweitjob) applies too", inc["Nebenbei"]["cat"] == "Gehalt", dict(inc["Nebenbei"])
    )
    check("other income stays uncategorized", inc["Oma"]["cat"] is None, dict(inc["Oma"]))
    con = m.db()
    m.set_income_rules(con, ["ACME Corp"])
    con.commit()
    check(
        "removing a pattern deletes only the income rule",
        m.income_rule_patterns(con) == ["ACME Corp"]
        and con.execute("SELECT COUNT(*) FROM rules WHERE pattern='BILLA'").fetchone()[0] == 1,
    )
    con.close()
    check("settings page shows the pattern", "ACME Corp" in c.get("/einstellungen").get_data(as_text=True))
    r = c.post(
        "/einstellungen",
        data={
            "action": "general",
            "income_pattern": "ACME Corp, Firma XY",
            "shared_name": "",
            "data_cutoff": "",
            "income_category": "Gehalt",
        },
    )
    con = m.db()
    pats = m.income_rule_patterns(con)
    con.close()
    check("POST /einstellungen income_pattern -> two rules", pats == ["ACME Corp", "Firma XY"], (pats, r.status_code))

    print("\n9) Date edge cases")
    html = upload(
        c,
        (
            george_bank(
                [
                    ("2025-02-28 ", "Trail", "Leerzeichen am Datum", "-1,00"),
                    ("31.02.2025", "Invalid", "31. Februar", "-1,00"),
                    ("28/02/2025", "SlashDMY", "dd/mm/yyyy nur wenn US unmoeglich", "-1,00"),
                    ("01/02/2025", "SlashAmbig", "01/02 -> US: 2. Jaenner", "-1,00"),
                ]
            ),
            "dates.csv",
        ),
    )
    d = {
        r["partner"]: r["booking_date"]
        for r in q(
            "SELECT partner, booking_date FROM transactions WHERE partner IN ('Trail','Invalid','SlashDMY','SlashAmbig')"
        )
    }
    check("date with whitespace", d.get("Trail") == "2025-02-28", d)
    check("31.02. invalid -> skipped", "Invalid" not in d, d)
    check("28/02/2025 -> dd/mm fallback applies", d.get("SlashDMY") == "2025-02-28", d)
    check("01/02/2025 -> US format wins (January 2, documented)", d.get("SlashAmbig") == "2025-01-02", d)

    check("no archiv/ folder anymore (feature removed 2026-09-15)", not os.path.isdir(os.path.join(tmp, "archiv")))
    finish()
