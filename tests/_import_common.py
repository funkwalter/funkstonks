"""Shared helpers for the import suites: isolated temp DB, test client,
check(), multipart upload to /import and CSV building blocks."""

import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as m


def fresh_db(sources='["bank","card","paypal","splitwise"]', sw_name="Anna Muster"):
    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    assert "moneyflow.db" not in m.DB_PATH
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    m.set_setting(con, "sources", sources)
    m.set_setting(con, "splitwise_name", sw_name)
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    return tmp, m.app.test_client()


from _harness import check, finish  # noqa: E402,F401 - every suite takes check/finish from here


def upload(c, *files, force_kind=None):
    """files: (bytes, filename). Returns the HTML text of the follow-up page (with flashes)."""
    data = {"files": [(io.BytesIO(b), name) for b, name in files]}
    if force_kind:
        data["force_kind"] = force_kind
    r = c.post("/import", data=data, content_type="multipart/form-data", follow_redirects=True)
    return r.get_data(as_text=True)


# ------------------------------------------------ result card after the upload
_ROW = re.compile(
    r'<tr class="imp-file" data-file="([^"]*)" data-group="([^"]*)" data-kind="([^"]*)" '
    r'data-new="(\d+)" data-skipped="(\d+)" data-removed="(\d+)">(.*?)</tr>',
    re.S,
)


def file_rows(html):
    """All file rows of the "Import-Ergebnis" card:
    [(name, group, kind, new, skipped, removed, error)]."""
    out = []
    for name, group, kind, new, skip, rem, body in _ROW.findall(html):
        m = re.search(r'class="neg">(.*?)</td>', body, re.S)
        out.append(
            (name, group, kind, int(new), int(skip), int(rem), re.sub(r"\s+", " ", m.group(1)).strip() if m else "")
        )
    return out


def fr(html, name_part):
    """(kind, new, skipped, removed) of the file whose name contains name_part."""
    for r in file_rows(html):
        if name_part in r[0]:
            return r[2:6]
    return None


def gr(html, group):
    """(new, skipped, removed) of the Splitwise group."""
    for r in file_rows(html):
        if r[1] == group and r[2] == "splitwise":
            return r[3:6]
    return None


def has(html, kind, new, skipped):
    return any(r[2] == kind and r[3] == new and r[4] == skipped for r in file_rows(html))


def err(html, name_part):
    for r in file_rows(html):
        if name_part in r[0]:
            return r[6]
    return ""


def steps(html):
    """Automation steps of the card: {key: n}."""
    return {k: int(n) for k, n in re.findall(r'<li data-step="([a-z]+)" data-n="(\d+)"', html)}


def result_lines(html, needle=""):
    """Readable summary of the result card (for diagnostic output)."""
    out = [
        "%s [%s%s] %d neu, %d bekannt, %d entfernt %s" % (n, k, "/" + g if g else "", nw, sk, rm, e)
        for n, g, k, nw, sk, rm, e in file_rows(html)
    ]
    out += ["%s=%d" % kv for kv in steps(html).items()]
    return [l for l in out if needle in l]


def q(sql, *args):
    con = m.db()
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


def one(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def count(where="1", *args):
    return one("SELECT COUNT(*) n FROM transactions WHERE " + where, *args)["n"]


# ---------------------------------------------------------------- CSV building blocks
GEORGE_HEAD = "Buchungsdatum;Partnername;Partner IBAN;Buchungs-Details;Betrag;Währung;Eigene IBAN"
MY_IBAN = "AT611904300234573201"


def george_bank(rows, crlf=True):
    """rows: (date, partner, details, amount) -> UTF-16 with BOM, semicolon."""
    nl = "\r\n" if crlf else "\n"
    lines = [GEORGE_HEAD] + [f"{d};{p};AT00;{t};{a};EUR;{MY_IBAN}" for d, p, t, a in rows]
    return (nl.join(lines) + nl).encode("utf-16")  # utf-16 => BOM (LE)


def george_card(rows):
    """Card account: partner name empty, merchant in the booking details, own IBAN empty."""
    lines = [GEORGE_HEAD] + [f"{d};;;{t};{a};EUR;" for d, t, a in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-16")


def english_bank(rows):
    """rows: (date, amount, name, description) -> UTF-8, comma."""
    out = io.StringIO()
    import csv as _csv

    w = _csv.writer(out, lineterminator="\n")
    w.writerow(["Date", "Amount", "Name", "Description"])
    for r in rows:
        w.writerow(list(r))
    return out.getvalue().encode("utf-8")


PP_AKT_HEAD = [
    "Datum",
    "Uhrzeit",
    "Zeitzone",
    "Name",
    "Typ",
    "Status",
    "Währung",
    "Brutto",
    "Gebühr",
    "Netto",
    "Absender E-Mail-Adresse",
    "Empfänger E-Mail-Adresse",
    "Transaktionscode",
]


def paypal_akt(rows):
    """Activities download: rows (date DE, name, type, currency, gross, code)."""
    import csv as _csv

    out = io.StringIO()
    w = _csv.writer(out, quoting=_csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(PP_AKT_HEAD)
    for d, name, typ, cur, gross, code in rows:
        w.writerow(
            [d, "10:00:00", "CEST", name, typ, "Abgeschlossen", cur, gross, "0,00", gross, "a@b.c", "x@y.z", code]
        )
    return ("\ufeff" + out.getvalue()).encode("utf-8")


PP_CSR_HEAD = [
    "Datum",
    "Uhrzeit",
    "Zeitzone",
    "Name",
    "Beschreibung",
    "Währung",
    "Brutto",
    "Gebühr",
    "Netto",
    "Saldo",
    "Transaktionscode",
]


def paypal_csr(rows):
    """CSR transaction report: NO type column, the type sits in the description, US date."""
    import csv as _csv

    out = io.StringIO()
    w = _csv.writer(out, quoting=_csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(PP_CSR_HEAD)
    for d, name, desc, cur, gross, code in rows:
        w.writerow([d, "10:00:00", "PDT", name, desc, cur, gross, "0,00", gross, "0,00", code])
    return out.getvalue().encode("utf-8")


def splitwise_csv(head, rows, total=None):
    """head: header line without newline, rows: finished row strings."""
    body = head + "\n" + "".join(r + "\n" for r in rows)
    if total:
        body += total + "\n"
    return body.encode("utf-8")


SW_DE_HEAD = "Datum,Beschreibung,Kategorien,Kosten,Währung,Anna Muster,Bob"
SW_EN_HEAD = "Date,Description,Category,Cost,Currency,Anna Muster (removed),Bob"
