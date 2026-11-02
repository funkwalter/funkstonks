"""Raiffeisen meinELBA: a bank CSV WITHOUT a header row (booking date; text;
value date; amount; currency; timestamp) whose counterparty sits inside the
text as "Empfänger: ..." / "Auftraggeber: ..." / "Zahlungsempfänger: ..." or
as the merchant of a card payment. Checks the synthetic header, the partner
extraction, counts, rules and idempotence - with made-up names."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _import_common import check, count, finish, fr, fresh_db, m, one, upload  # noqa: E402

CARD = "Kartenzahlung mit Kartenfolge-Nr.: 2"


def row(booked, text, valuta, amount):
    return f'{booked};"{text}";{valuta};{amount};EUR;{valuta} 10:00:00:000'


ROWS = [
    row(
        "02.01.2026",
        f"Verwendungszweck: BILLA DANKT 0000826 WIEN 1080 Zahlungsreferenz: POS           6,32 AT  K2   01.01. 14:22 {CARD}",
        "01.01.2026",
        "-6,32",
    ),
    row("05.01.2026", f"Zahlungsreferenz: SPAR         2361  K2   05.01. 17:11 {CARD}", "05.01.2026", "-48,56"),
    row(
        "07.01.2026",
        "ONLINE BANKING VOM 06.01 UM 18:23 Empfänger: Anna Muster Zahlungsreferenz: Wohnung Jänner IBAN Empfänger: AT000000000000000000 BIC Empfänger: XXXXATWWXXX",
        "07.01.2026",
        "-800,00",
    ),
    row(
        "09.01.2026",
        "Zahlungsempfänger: PayPal Europe S.a.r.l. et Cie S.C.A Verwendungszweck: 1047458188582/PAYPAL IBAN Zahlungsempfänger: LU000000000000000000 BIC Zahlungsempfänger: PPLXLUL2XXX Empfänger-Kennung: LU96ZZZ0000000000000000058 Auftraggeberreferenz: 1047458188582 Mandat: 5GL0000000000, 21.03.2023",
        "09.01.2026",
        "-19,98",
    ),
    row(
        "29.01.2026",
        "Auftraggeber: Firma Muster GmbH Verwendungszweck: BEZUG 01/2026 IBAN Auftraggeber: AT000000000000000001 BIC Auftraggeber: BKAUATWWXXX",
        "29.01.2026",
        "1906,54",
    ),
    row(
        "08.01.2026",
        "KONFORM 32990-0000000-1-20260108 Empfänger: Bob Beispiel Zahlungsreferenz: Spotify IBAN Empfänger: AT000000000000000002 BIC Empfänger: RLNWATWWOWS Empfänger: Musterweg 1 1234 Musterstadt",
        "08.01.2026",
        "-3,50",
    ),
    row("31.03.2026", "Zahlungsreferenz: Kontoführung", "01.04.2026", "-5,19"),
    row("31.03.2026", "Zahlungsreferenz: 0,01 % p.a. Habenzinsen      ab 01.01.26", "01.04.2026", "0,25"),
    row(
        "14.10.2026",
        f"Verwendungszweck: SPESEN: 0,00  KURS: 11,1242 VOM:12.10.2026 PAYPAL_*BAKERY A MALMO 211 41 Zahlungsreferenz: POS          93,00 SEK K2   13.10. 10:41 {CARD}",
        "13.10.2026",
        "-8,36",
    ),
    row(
        "02.02.2026",
        "Kartenentgelt 2026 Kundendaten: Debitkarte 2 Zahlungsreferenz: Kartenentgelt 2026 IBAN Transaktionsteilnehmer: 82405 BIC Transaktionsteilnehmer: 32990000900",
        "02.02.2026",
        "-19,44",
    ),
    row("04.02.2026", f"Zahlungsreferenz: BANKOMAT  00010897 K2   04.02. 14:43 {CARD}", "04.02.2026", "-400,00"),
    # the same card payment twice in one file = two real payments
    row("09.03.2026", f"Zahlungsreferenz: MCDONALDS 2  2360  K2   08.03. 01:42 {CARD}", "08.03.2026", "-7,60"),
    row("09.03.2026", f"Zahlungsreferenz: MCDONALDS 2  2360  K2   08.03. 01:42 {CARD}", "08.03.2026", "-7,60"),
]


def test_import_elba():
    tmp, c = fresh_db(sources='["bank"]')

    print("\n1) Header synthesis and partner extraction (unit)")
    hdr = m.headerless_bank_header([r.split(";") for r in ROWS[:3]])
    check("synthetic header found", hdr is not None, hdr)
    check(
        "date, text, amount, currency placed",
        hdr and hdr[0] == "Buchungsdatum" and hdr[1] == "Buchungstext" and hdr[3] == "Betrag" and hdr[4] == "Währung",
        hdr,
    )
    check(
        "a real header is left alone",
        m.headerless_bank_header([["Buchungsdatum", "Partnername", "Betrag"], ["01.01.2026", "X", "-1,00"]]) is None,
    )
    check("detect_kind on the synthetic header = bank", m.detect_kind(hdr) == "bank")
    cases = {
        "Empfänger wins, address ignored": (
            "ONLINE BANKING VOM 06.01 UM 18:23 Empfänger: Anna Muster Zahlungsreferenz: Wohnung IBAN Empfänger: AT00",
            "Anna Muster",
        ),
        "Auftraggeber (incoming)": (
            "Auftraggeber: Firma Muster GmbH Verwendungszweck: BEZUG 01/2026 IBAN Auftraggeber: AT00",
            "Firma Muster GmbH",
        ),
        "Zahlungsempfänger (direct debit)": (
            "Zahlungsempfänger: PayPal Europe S.a.r.l. et Cie S.C.A Verwendungszweck: 123/PAYPAL IBAN Zahlungsempfänger: LU00",
            "PayPal Europe S.a.r.l. et Cie S.C.A",
        ),
        "card payment: merchant from Verwendungszweck": (
            "Verwendungszweck: BILLA DANKT 0000826 WIEN 1080 Zahlungsreferenz: POS           6,32 AT  K2   01.01. 14:22 "
            + CARD,
            "BILLA DANKT 0000826 WIEN 1080",
        ),
        "foreign card payment: currency note stripped": (
            "Verwendungszweck: SPESEN: 0,00  KURS: 11,1242 VOM:12.10.2026 PAYPAL_*BAKERY A MALMO 211 41 Zahlungsreferenz: POS          93,00 SEK K2   13.10. 10:41 "
            + CARD,
            "PAYPAL_*BAKERY A MALMO 211 41",
        ),
        "reference with terminal: merchant": ("Zahlungsreferenz: SPAR         2361  K2   05.01. 17:11 " + CARD, "SPAR"),
        "reference with terminal: merchant with code": (
            "Zahlungsreferenz: DM-FIL. A0HG 3151  K2   10.01. 16:48 " + CARD,
            "DM-FIL. A0HG",
        ),
        "ATM": ("Zahlungsreferenz: BANKOMAT  00010897 K2   04.02. 14:43 " + CARD, "BANKOMAT"),
        "bank fee: the reference itself": ("Zahlungsreferenz: Kontoführung", "Kontoführung"),
        "lead text before the first label": (
            "Kartenentgelt 2026 Kundendaten: Debitkarte 2 IBAN Transaktionsteilnehmer: 82405",
            "Kartenentgelt 2026",
        ),
        "no labels -> empty": ("BILLA DANKT 0000826", ""),
    }
    for name, (text, want) in cases.items():
        got = m.embedded_partner(text)
        check(name, got == want, got)

    print("\n2) Rows an older version imported with the text as partner are repaired")
    # the rent transfer, the PayPal debit and the two identical card payments, as a
    # column mapping (partner = text = column 2) stored them before 2026-11-02
    con = m.db()
    seen = {}
    for i in (2, 3, 11, 12):
        f = ROWS[i].split(";")
        text, d, a = f[1].strip('"'), m.parse_date(f[0]), m.parse_amount(f[3])
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash) "
            "VALUES('bank',?,?,?,?,'EUR',?)",
            (d, text, text, a, m.hash_with_occurrence(seen, "bank", d, a, text, text)),
        )
    con.commit()
    n = m.repair_embedded_partners(con)
    con.commit()
    check("four rows repaired", n == 4, n)
    check("nothing left to repair", m.repair_embedded_partners(con) == 0)
    con.close()
    check(
        "rent row: partner from the text",
        one("SELECT partner FROM transactions WHERE amount=-800")["partner"] == "Anna Muster",
    )
    check(
        "text kept as description",
        "Zahlungsreferenz: Wohnung" in one("SELECT description FROM transactions WHERE amount=-800")["description"],
    )
    check(
        "identical card payments got distinct hashes",
        count("partner='MCDONALDS 2'") == 2
        and one("SELECT COUNT(DISTINCT raw_hash) n FROM transactions WHERE partner='MCDONALDS 2'")["n"] == 2,
    )

    print("\n3) Import of the headerless file: repaired rows are recognised, the rest is new")
    body = ("\n".join(ROWS) + "\n").encode("utf-8")
    html = upload(c, (body, "meinElba_umsaetze_suche.csv"))
    r = fr(html, "meinElba")
    check("recognised as bank, 9 new, 4 skipped (the repaired ones)", r == ("bank", 9, 4, 0), r)
    check("13 bank rows", count("source='bank'") == 13)
    p = {t["description"][:30]: t for t in [dict(x) for x in m.db().execute("SELECT * FROM transactions").fetchall()]}
    rent = one("SELECT * FROM transactions WHERE partner='Anna Muster'")
    check(
        "transfer: partner from Empfänger, amount, booking date",
        rent and rent["amount"] == -800 and rent["booking_date"] == "2026-01-07",
        rent and dict(rent),
    )
    sal = one("SELECT * FROM transactions WHERE partner='Firma Muster GmbH'")
    check("salary: partner from Auftraggeber, positive", sal and sal["amount"] == 1906.54, sal and dict(sal))
    check(
        "PayPal debit carries 'PayPal' in the partner",
        one("SELECT partner FROM transactions WHERE amount=-19.98")["partner"].startswith("PayPal"),
    )
    check("two identical card payments both imported", count("partner='MCDONALDS 2'") == 2)
    check("currency column read", count("currency='EUR'") == 13)
    bil = one("SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.partner LIKE 'BILLA%'")
    check("default rule BILLA -> Lebensmittel on the extracted partner", bil and bil["name"] == "Lebensmittel", bil)
    fee = one(
        "SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.partner='Kontoführung'"
    )
    check("default rule Kontoführung -> Bank", fee and fee["name"] == "Bank", fee)
    atm = one("SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.partner='BANKOMAT'")
    check("default rule BANKOMAT -> Bargeld", atm and atm["name"] == "Bargeld", atm)
    check("partner page sees a name on every row", count("source='bank' AND partner=''") == 0)

    print("\n4) Idempotent")
    html = upload(c, (body, "meinElba_umsaetze_suche.csv"))
    r = fr(html, "meinElba")
    check("second upload: 0 new, 13 skipped", r == ("bank", 0, 13, 0), r)
    check("still 13 rows", count("source='bank'") == 13)
    del p
    finish()
