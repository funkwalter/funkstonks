"""Reading file formats: CSV with preamble, XLSX without dependencies, date and
amount parsers, column detection, format detection (detect_kind)."""

import csv
import io
import re
from datetime import date, datetime, timedelta


def parse_amount(s):
    """'-1.234,56' / '-1,234.56' / '-1234.56' (also with a unicode minus) -> float"""
    if s is None:
        return None
    s = str(s).strip().replace("\u2212", "-").replace("€", "").replace(" ", "").replace("\xa0", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s):
    """A slash date is always US format (PayPal CSR export) - %m/%d/%Y MUST
    come before %d/%m/%Y, otherwise day/month are silently swapped for dates
    valid both ways (e.g. 3/1/2026)."""
    s = (s or "").strip().split(" ")[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def read_csv_flexible(file_storage):
    """Reads the upload, detects encoding + delimiter, returns (header, rows)."""
    raw = file_storage.read()
    text = None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 BOM (George export)
        text = raw.decode("utf-16")
    else:
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    if text is None:
        raise ValueError("Encoding nicht erkennbar")

    def parse(delim):
        return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]

    def looks_like_header(r):
        cells = [c.strip().lower() for c in r]
        if sum(1 for c in cells if c) < 3:
            return False
        has_date = any(k in c for c in cells for k in ("datum", "date", "buchung"))
        has_amt = any(
            k in c for c in cells for k in ("betrag", "amount", "umsatz", "kosten", "cost", "brutto", "gross", "value")
        )
        return has_date and has_amt

    # Some banks (ING, comdirect, Sparkasse) put account data and blank lines
    # before the actual header row, partly with a different delimiter. The
    # header is the first row (within the first 30) with at least three fields
    # that contains a date AND an amount keyword; of the three delimiters the
    # one with the most fields in that row wins. Without a recognizable header
    # as before: first row, delimiter by frequency (detect_kind then says
    # "format not recognized").
    best = None
    for delim in (";", ",", "\t"):
        rows = parse(delim)
        for i, r in enumerate(rows[:30]):
            if looks_like_header(r):
                cand = (sum(1 for c in r if c.strip()), delim, i, rows)
                if best is None or cand[0] > best[0]:
                    best = cand
                break
    if best:
        _n, _d, i, rows = best
        return rows[i], rows[i + 1 :]
    sample = text[:4096]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    if sample.count("\t") > max(sample.count(";"), sample.count(",")):
        delim = "\t"
    rows = parse(delim)
    if not rows:
        raise ValueError("Datei ist leer")
    return rows[0], rows[1:]


def find_col(header, keywords):
    """Column index for a keyword list. An exact match beats a substring -
    otherwise "Lastschrift Ursprungsbetrag" won against "Betrag" in a
    Sparkasse CAMT file (audit 2026-09-15: 0 rows imported)."""
    hl = [h.lower().strip().lstrip("\ufeff") for h in header]
    for kw in keywords:
        for i, h in enumerate(hl):
            if h == kw:
                return i
    for kw in keywords:
        for i, h in enumerate(hl):
            if kw in h:
                return i
    return None


# Column names that bank and card exports use for the same information
# (German and English). Order = priority when searching.
BANK_COLUMNS = {
    "date": ["buchungsdatum", "buchung", "datum", "date"],
    "amount": ["betrag", "amount"],
    "partner": [
        "partnername",
        "partner",
        "beguenstigter/zahlungspflichtiger",
        "begünstigter/zahlungspflichtiger",
        "empfänger",
        "empfaenger",
        "zahlungsempfänger",
        "zahlungsempfaenger",
        "begünstigter",
        "beguenstigter",
        "auftraggeber",
        "zahlungspflichtiger",
        "payee",
        "counterparty",
        "name",
    ],
    "desc": [
        "verwendungszweck",
        "buchungsdetails",
        "details",
        "buchungstext",
        "referenz",
        "beschreibung",
        "zahlungsreferenz",
        "description",
        "memo",
        "narrative",
        "purpose",
        "reference",
    ],
    "cur": ["währung", "waehrung", "currency"],
}


def header_signature(header):
    """Identifier of a header row for remembered column mappings
    (settings.bank_column_maps, page /import/zuordnen)."""
    return "|".join(h.strip().lstrip("\ufeff").lower() for h in header)


def bank_columns(header, what, mapping=None):
    """Column indices (date, amount, partner, desc, cur) of a bank/card header;
    without date or amount the file is unusable. `mapping` is a mapping
    remembered by the user (column assignment): then its indices apply;
    `amount` may be a pair (debit, credit) there."""
    if mapping:
        amt = mapping.get("amount")
        if amt is None and mapping.get("debit") is not None and mapping.get("credit") is not None:
            amt = (mapping["debit"], mapping["credit"])
        if mapping.get("date") is None or amt is None:
            raise ValueError(f"{what}: gemerkte Spaltenzuordnung unvollständig – unter Einstellungen löschen.")
        return mapping["date"], amt, mapping.get("partner"), mapping.get("desc"), mapping.get("cur")
    cols = {k: find_col(header, kws) for k, kws in BANK_COLUMNS.items()}
    if cols["date"] is None or cols["amount"] is None:
        raise ValueError(
            f"{what}: keine Spalte für Datum bzw. Betrag gefunden – die Kopfzeile lautet "
            f"„{'; '.join(h.strip() for h in header if h.strip())[:120]}“. Datei in der Bank als "
            f"CSV mit Spaltenüberschriften exportieren oder unten das Format wählen."
        )
    return cols["date"], cols["amount"], cols["partner"], cols["desc"], cols["cur"]


def is_card_account(header, rows):
    """George export: current account vs. card account differ in CONTENT -
    for the card account the column 'Eigene IBAN' is empty in every row."""
    c_iban = find_col(header, ["eigene iban"])
    if c_iban is None:
        return False
    vals = [r[c_iban].strip() for r in rows if c_iban < len(r)]
    return bool(vals) and all(v == "" for v in vals)


EXPORT_HEADER = ["Datum", "Quelle", "Partner", "Beschreibung", "Betrag", "Effektiv", "Kategorie", "Status"]
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_col_index(ref):
    """'AB12' -> 27 (0-based)."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def read_xlsx_rows(buf):
    """First sheet of an XLSX as (header, rows) of strings - without an extra
    library (zipfile + XML from the standard library; the app must run without
    network and without pip). Date cells (number format with day, month and
    year) come back as ISO dates, numbers as '1870.44'. Cells are aligned by
    column letter - Flatex leaves a unit column without a header between
    'Nominal' and 'Betrag'."""
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        z = zipfile.ZipFile(buf)
    except zipfile.BadZipFile as e:
        raise ValueError("Keine gültige XLSX-Datei") from e
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_XLSX_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
    date_styles = set()
    if "xl/styles.xml" in names:
        st = ET.fromstring(z.read("xl/styles.xml"))
        fmts = {nf.get("numFmtId"): (nf.get("formatCode") or "").lower() for nf in st.iter(_XLSX_NS + "numFmt")}
        xfs = st.find(_XLSX_NS + "cellXfs")
        for i, xf in enumerate(list(xfs) if xfs is not None else []):
            fid = xf.get("numFmtId") or "0"
            code = fmts.get(fid, "")
            if fid in {"14", "15", "16", "17", "22"} or ("d" in code and "y" in code and "m" in code):
                date_styles.add(str(i))
    sheet = next((n for n in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), None)
    if not sheet:
        raise ValueError("XLSX ohne Tabellenblatt")
    rows = []
    for row in ET.fromstring(z.read(sheet)).iter(_XLSX_NS + "row"):
        cells = {}
        for c in row.findall(_XLSX_NS + "c"):
            v = c.find(_XLSX_NS + "v")
            t = c.get("t")
            if t == "inlineStr":
                txt = "".join(x.text or "" for x in c.iter(_XLSX_NS + "t"))
            else:
                txt = v.text if v is not None else ""
                if t == "s" and txt:
                    txt = shared[int(txt)]
                elif txt and (c.get("s") or "") in date_styles:
                    try:
                        txt = (date(1899, 12, 30) + timedelta(days=int(float(txt)))).isoformat()
                    except ValueError:
                        pass
                elif txt and t in (None, "n"):
                    try:
                        f = float(txt)
                        txt = str(int(f)) if f.is_integer() else repr(f)
                    except ValueError:
                        pass
            cells[_xlsx_col_index(c.get("r") or "A")] = (txt or "").strip()
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    rows = [r for r in rows if any(x for x in r)]
    if not rows:
        return [], []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return rows[0], rows[1:]


def _depot_number(s):
    """'11' / '11 Stk.' / '1.870,44 EUR' -> float (None if there is nothing in it)."""
    mo = re.search(r"-?[\d.,]+", str(s or "").replace("−", "-"))
    return parse_amount(mo.group(0)) if mo else None


def is_own_export(header):
    """The CSV from /export.csv - looks like a bank statement to detect_kind
    (date + amount) and would create every PayPal/card/Splitwise row as a new
    bank booking. The way back is the backup."""
    return [h.strip().lstrip("\ufeff") for h in header][:8] == EXPORT_HEADER


def detect_kind(header):
    """Detect the format from the file header. Purely by content - there is no
    standard for bank CSVs, so the usual column names are checked in German
    AND English. If nothing is recognized, the user can pick the format by
    hand on import (see import_page)."""
    hl = " | ".join(h.lower() for h in header)
    # Depot transactions (Flatex) also have "Betrag" - hence BEFORE the bank check.
    if "isin" in hl and any(k in hl for k in ("nominal", "stück", "stueck", "stk", "anzahl")):
        return "depot"
    if "transaktionscode" in hl or "transaction id" in hl or ("brutto" in hl and "typ" in hl):
        return "paypal"
    # "cost" in addition to "kosten" - some Splitwise groups export in English
    # (column "Cost" instead of "Kosten"); import_splitwise() itself already
    # knows both languages via find_col(), only this format detection was
    # German-only until now and rejected such exports as "not recognized".
    if ("kosten" in hl or "cost" in hl) and ("beschreibung" in hl or "description" in hl):
        return "splitwise"
    if "buchungsdatum" in hl or "partnername" in hl or "betrag" in hl:
        return "bank"
    # English exports: date AND amount together suffice as a marker. On their
    # own they would be too unspecific (almost every table has a "date").
    if ("date" in hl or "datum" in hl) and ("amount" in hl or "value" in hl):
        return "bank"
    return None


IMPORT_KIND_LABEL = {
    "bank": "Kontoauszug",
    "card": "Kreditkarte",
    "paypal": "PayPal",
    "splitwise": "Splitwise",
    "depot": "Depot",
}
