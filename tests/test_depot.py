"""Depot (Flatex): XLSX and CSV import of depot transactions, positions with
average purchase price, page /investitionen (monthly series, totals, savings
rate) and the "Investiert" tile on the dashboard incl. drilldown."""

import io
import os
import re
import zipfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402
from _import_common import *


def test_depot():

    def make_xlsx(header, rows, date_cols=()):
        """Minimal XLSX like the Flatex export: numbers as numbers, date cells as
        Excel serial with number format dd.MM.yyyy (style 1), text inline."""

        def col(i):
            s = ""
            i += 1
            while i:
                i, r = divmod(i - 1, 26)
                s = chr(65 + r) + s
            return s

        def cell(r, c, v):
            ref = col(c) + str(r)
            if c in date_cols and isinstance(v, date):
                return '<c r="%s" s="1"><v>%d</v></c>' % (ref, (v - date(1899, 12, 30)).days)
            if isinstance(v, (int, float)):
                return '<c r="%s"><v>%r</v></c>' % (ref, v)
            return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (
                ref,
                str(v).replace("&", "&amp;").replace("<", "&lt;"),
            )

        xml_rows = ['<row r="1">' + "".join(cell(1, i, h) for i, h in enumerate(header) if h != "") + "</row>"]
        for ri, r in enumerate(rows, start=2):
            xml_rows.append(
                '<row r="%d">' % ri + "".join(cell(ri, i, v) for i, v in enumerate(r) if v != "") + "</row>"
            )
        sheet = (
            '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>" + "".join(xml_rows) + "</sheetData></worksheet>"
        )
        styles = (
            '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd.MM.yyyy"/></numFmts>'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>'
        )
        wb = (
            '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Depotumsätze" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        )
        wbrels = (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
        )
        ct = (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", ct)
            z.writestr("_rels/.rels", rels)
            z.writestr("xl/workbook.xml", wb)
            z.writestr("xl/_rels/workbook.xml.rels", wbrels)
            z.writestr("xl/styles.xml", styles)
            z.writestr("xl/worksheets/sheet1.xml", sheet)
        return buf.getvalue()

    HEAD = [
        "Buchungstag",
        "Valuta",
        "Bezeichnung",
        "ISIN",
        "Nominal (Stk.)",
        "",
        "Betrag",
        "",
        "Kurs",
        "",
        "Devisenkurs",
        "TA.-Nr.",
        "Buchungsinformation",
    ]
    XLSX = make_xlsx(
        HEAD,
        [
            [
                date(2026, 9, 29),
                date(2026, 10, 1),
                "VAN FTSE ALL USD-AC",
                "IE00BK5BQT80",
                11,
                "Stück",
                1870.44,
                "€",
                170.04,
                "€",
                1,
                "9000000001",
                "Ausführung ORDER Kauf IE00BK5BQT80 1",
            ],
            [
                date(2026, 10, 5),
                date(2026, 10, 7),
                "VAN FTSE ALL USD-AC",
                "IE00BK5BQT80",
                2,
                "Stück",
                350.00,
                "€",
                175.00,
                "€",
                1,
                "9000000002",
                "Ausführung ORDER Kauf IE00BK5BQT80 2",
            ],
        ],
        date_cols=(0, 1),
    )

    tmp, c = fresh_db()
    con = m.db()
    inv = con.execute("SELECT id FROM categories WHERE name='Investitionen'").fetchone()["id"]
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    geh = con.execute("SELECT id FROM categories WHERE name='Gehalt'").fetchone()["id"]
    con.commit()
    con.close()

    print("1) XLSX parser directly")
    header, rows = m.read_xlsx_rows(io.BytesIO(XLSX))
    check(
        "header aligned (Betrag in column G = index 6)",
        header[6] == "Betrag" and header[4] == "Nominal (Stk.)",
        header,
    )
    check("serial date -> ISO", rows[0][0] == "2026-09-29" and rows[0][1] == "2026-10-01", rows[0][:2])
    check(
        "numbers as text, quantity integer",
        rows[0][4] == "11" and rows[0][6] == "1870.44" and rows[0][8] == "170.04",
        rows[0],
    )
    check("unit cells without header are kept", rows[0][5] == "Stück" and rows[0][7] == "€")
    check("detect_kind = depot (despite column Betrag)", m.detect_kind(header) == "depot")

    print("\n2) Upload XLSX via /import")
    html = upload(c, (XLSX, "Depotumsätze.xlsx"))
    check("detected as depot, 2 new", fr(html, "Depot") == ("depot", 2, 0, 0), result_lines(html))
    d = q("SELECT * FROM depot_tx ORDER BY booking_date")
    check(
        "two rows, buy, amounts",
        len(d) == 2
        and all(x["kind"] == "buy" for x in d)
        and abs(d[0]["amount"] - 1870.44) < 0.005
        and d[0]["qty"] == 11,
        [dict(x) for x in d],
    )
    check(
        "price, TA no, ISIN",
        d[0]["price"] == 170.04 and d[0]["ta_nr"] == "9000000001" and d[0]["isin"] == "IE00BK5BQT80",
    )
    check("value date", d[0]["valuta"] == "2026-10-01")
    html = upload(c, (XLSX, "Depotumsätze.xlsx"))
    check("re-import: 0 new, 2 known", fr(html, "Depot") == ("depot", 0, 2, 0), result_lines(html))
    check("import history knows depot", "Depot" in c.get("/import").get_data(as_text=True))
    check("transactions untouched (no account row from the depot)", count("1") == 0)

    print("\n3) CSV variant with units in the text and German number format")
    CSV = (
        "Buchungstag;Valuta;Bezeichnung;ISIN;Nominal (Stk.);Betrag;Kurs;Devisenkurs;TA.-Nr.;Buchungsinformation\r\n"
        "12.10.2026;14.10.2026;MSCI WORLD ETF;IE00B4L5Y983;3 Stk.;303,30 €;101,10 €;1;9000000003;Ausführung ORDER Kauf IE00B4L5Y983 1\r\n"
        "20.10.2026;22.10.2026;VAN FTSE ALL USD-AC;IE00BK5BQT80;3 Stk.;540,00 €;180,00 €;1;9000000004;Ausführung ORDER Verkauf IE00BK5BQT80 2\r\n"
    ).encode()
    html = upload(c, (CSV, "depotumsaetze.csv"))
    check("CSV as depot, 2 new", fr(html, "depotumsaetze") == ("depot", 2, 0, 0), result_lines(html))
    s = one("SELECT * FROM depot_tx WHERE ta_nr='9000000004'")
    check(
        "sell detected, quantity 3, amount 540",
        s and s["kind"] == "sell" and s["qty"] == 3 and abs(s["amount"] - 540) < 0.005,
        s and dict(s),
    )
    con = m.db()
    pos = m.depot_positions(con)
    con.close()
    van = next(p for p in pos if p["isin"] == "IE00BK5BQT80")
    msci = next(p for p in pos if p["isin"] == "IE00B4L5Y983")
    # cost basis: (1870.44 + 350) / 13 = 170.80 per unit; selling 3 units -> cost basis -512.41,
    # realised 540 - 512.41 = 27.59
    check("Vanguard: 10 units left", abs(van["qty"] - 10) < 1e-6, van["qty"])
    check(
        "Vanguard: invested 1708.03, avg 170.80",
        abs(van["invested"] - 1708.03) < 0.01 and abs(van["avg_price"] - 170.80) < 0.01,
        (van["invested"], van["avg_price"]),
    )
    check("Vanguard: realised +27.59", abs(van["realized"] - 27.59) < 0.01, van["realized"])
    check(
        "MSCI: 3 units, 303.30, avg 101.10",
        msci["qty"] == 3 and abs(msci["invested"] - 303.30) < 0.005 and abs(msci["avg_price"] - 101.10) < 0.005,
    )
    check("room for prices (price/value None)", van["price"] is None and van["value"] is None)

    print("\n4) Page /investitionen: monthly series from the account data")
    BANK = george_bank(
        [
            ("02.05.2026", "Arbeitgeber AG", "Gehalt Mai", "3000,00"),
            ("03.05.2026", "Hausverwaltung Muster", "Miete", "-900,00"),
            ("05.05.2026", "flatex", "Depot", "-500,00"),
            ("02.06.2026", "Arbeitgeber AG", "Gehalt Juni", "3000,00"),
            ("03.06.2026", "Hausverwaltung Muster", "Miete", "-900,00"),
            ("05.06.2026", "flatex", "Depot", "-1000,00"),
            ("20.06.2026", "Broker Bonus", "Gutschrift", "50,00"),
        ]
    )
    html = upload(c, (BANK, "Umsaetze_2026-06-30.csv"))
    con = m.db()
    con.execute("UPDATE transactions SET category_id=? WHERE partner='Arbeitgeber AG'", (geh,))
    con.execute("UPDATE transactions SET category_id=? WHERE partner='Hausverwaltung Muster'", (woh,))
    con.execute("UPDATE transactions SET category_id=? WHERE partner IN ('flatex','Broker Bonus')", (inv,))
    con.commit()
    con.close()
    h = c.get("/investitionen").get_data(as_text=True)
    api = c.get("/api/investitionen").get_json()
    rows = {r["month"]: (float(r["inc"]), float(r["exp"]), float(r["invest"]), float(r["saldo"])) for r in api["rows"]}
    check("default = all months (May + June)", set(rows) == {"2026-05", "2026-06"}, rows)
    check("May: 3000 / 900 / 500 / 1600", rows.get("2026-05") == (3000.0, 900.0, 500.0, 1600.0), rows.get("2026-05"))
    check(
        "June: 3000 / 900 / 950 (1000 - 50 credit net) / 1150",
        rows.get("2026-06") == (3000.0, 900.0, 950.0, 1150.0),
        rows.get("2026-06"),
    )
    check("savings rate total 24.2 % (1450 of 6000)", "24.2 %" in h)
    ijs = open(os.path.join(ROOT, "static", "js", "investitionen.js"), encoding="utf-8").read()
    check(
        "chart: three views (cashflow/shares/cumulative), lines default, share axis to 100 %, no smoothing",
        all(('id="view-' + k + '"') in h for k in ("cash", "share", "cum"))
        and "var view = 'cash'" in ijs
        and 'id="mode-' not in h
        and "min: 0, max: 100, interval: 25" in ijs
        and "smooth" not in ijs
        and all(("--chart" + k) in ijs for k in ("saldo", "cuminv", "cumsaldo")),
    )
    check(
        "partial month marked as data-partial, inflows and depot value as attributes",
        re.search(r'data-partial="[01]"', h) is not None
        and re.search(r'data-depot="[^"]*" data-depotcost="[-\d.]+" data-depotcur="[01]"', h) is not None,
    )
    check("depot positions on the page", "VAN FTSE ALL USD-AC" in h and "1708.03" in h)
    check(
        "tiles: Investiert 1450.00",
        re.search(
            r'Investiert</span>\s*<a href="/transactions\?from=2026-05&(?:amp;)?to=2026-06&(?:amp;)?cat=%d"[^>]*><b class="invest">1450.00 €'
            % inv,
            h,
        )
        is not None,
        re.findall(r"Investiert</span>.{0,160}", h, re.S)[:1],
    )
    h3 = c.get("/investitionen?range=last3").get_data(as_text=True)
    check("range=last3 works (both months)", h3.count('class="inv-row"') == 2)
    hm = c.get("/investitionen?month=2026-05").get_data(as_text=True)
    check("single month", hm.count('class="inv-row"') == 1 and "500.00 €" in hm)

    print("\n5) Dashboard tile 'Investiert' + drilldown")
    h = c.get("/?month=2026-06").get_data(as_text=True)
    mo = re.search(r'Investiert Juni 2026</span>\s*<a href="([^"]+)"[^>]*>\s*<b class="invest">([\d.]+) €', h)
    check("tile present with 950.00", mo and abs(float(mo.group(2)) - 950) < 0.005, mo and mo.groups())
    check("delta to previous month +450.00", re.search(r"\+450\.00 € vs\. <a[^>]*>Mai 2026", h) is not None)
    check("share 31.7 % of income", "31.7 % der Einnahmen" in h)
    url = mo.group(1).replace("&amp;", "&")
    d = c.get(url).get_data(as_text=True)
    tot = float(re.search(r'<b>Summe</b>.*?<td class="num (?:neg|pos)"><b>(-?[\d.]+)</b>', d, re.S).group(1))
    check("drilldown list sums to -950", abs(tot + 950) < 0.005, tot)
    check("link to /investitionen", 'href="/investitionen"' in h)
    check(
        "expenses tile WITHOUT investments (900 instead of 1850, since 2026-10-13)",
        re.search(r'Ausgaben Juni 2026</span>.*?<b class="neg">900.00 €', h, re.S) is not None,
    )
    check(
        "category table without Investitionen row, total 900",
        "900.00 €</b>" in h and not re.search(r'chip" style="background:[^"]*"></i>Investitionen</a>', h),
    )

    print("\n6) Error cases")
    html = upload(c, (b"PK\x03\x04kaputt", "x.xlsx"))
    check(
        "broken XLSX -> error text instead of crash",
        "XLSX" in err(html, "x.xlsx") or "Fehler" in err(html, "x.xlsx") or err(html, "x.xlsx"),
        result_lines(html),
    )
    html = upload(c, (b"ISIN;Nominal;Kurs\r\nX;1;2\r\n", "d.csv"), force_kind="depot")
    check(
        "depot without date/amount -> error",
        "Betrag" in err(html, "d.csv") or "Datum" in err(html, "d.csv"),
        result_lines(html),
    )
    finish()
