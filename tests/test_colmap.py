"""Unknown bank CSV: "map columns" link in the import result, mapping remembered
per header row (single amount column or debit/credit), follow-up files import
as bank statements without asking, deletion under settings; plus the source
checkboxes on the help page and the comparison page with the shared period
macro."""

import json
import os
import re

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_colmap():

    tmp, c = fresh_db()

    def csv(text):
        return text.encode("utf-8-sig")

    print("1) Unknown header -> link 'map columns'")
    FREMD = csv(
        "Valuta;Text;Soll;Haben;Wer\n"
        "03.06.2026;Miete Juni;900,00;;Hausverwaltung Muster\n"
        "05.06.2026;Gehalt;;2.500,00;Arbeitgeber AG\n"
        "07.06.2026;Einkauf;23,45;;BILLA DANKT 0001\n"
    )
    h = upload(c, (FREMD, "fremdbank_juni.csv"))
    check("format not recognised", fr(html := h, "fremdbank")[0] == "?", fr(h, "fremdbank"))
    mo = re.search(r'href="/import/zuordnen\?t=([0-9a-f]{16})"', h)
    check("link with token in the result", mo is not None, h[h.find("fremdbank") :][:300])
    check("nothing imported", count("source='bank'") == 0)
    check(
        "file sits in the temp folder next to the DB",
        mo and os.path.exists(os.path.join(os.path.dirname(m.DB_PATH), "uploads", mo.group(1) + ".csv")),
    )
    token = mo.group(1)

    print("\n2) Mapping page")
    h = c.get("/import/zuordnen?t=" + token).get_data(as_text=True)
    check(
        "header as options, preview, debit/credit suggested",
        "<th>Valuta</th>" in h
        and "<th>Soll</th>" in h
        and "Hausverwaltung Muster" in h
        and re.search(r'name="mode" value="split" checked', h) is not None,
        h[:200],
    )
    check(
        "date preselected? (Valuta is no keyword -> empty)",
        re.search(r'id="cm-date"[^>]*>\s*<option value="">–</option>\s*<option value="0" >', h) is not None,
    )
    r = c.get("/import/zuordnen?t=zzzz", follow_redirects=True)
    check("invalid token -> FEHLER", "FEHLER" in r.get_data(as_text=True))
    r = c.post(
        "/import/zuordnen",
        data={"t": token, "date": "0", "mode": "split", "debit": "2", "credit": "2"},
        follow_redirects=True,
    )
    check("debit == credit -> FEHLER", "FEHLER" in r.get_data(as_text=True) and count("source='bank'") == 0)
    r = c.post(
        "/import/zuordnen",
        data={"t": token, "date": "1", "mode": "split", "debit": "2", "credit": "3"},
        follow_redirects=True,
    )
    check(
        "text as date -> FEHLER (no row yields date+amount)",
        "FEHLER" in r.get_data(as_text=True) and count("source='bank'") == 0,
    )

    print("\n3) Map with debit/credit -> imported, remembered")
    r = c.post(
        "/import/zuordnen",
        data={"t": token, "date": "0", "mode": "split", "debit": "2", "credit": "3", "partner": "4", "desc": "1"},
        follow_redirects=True,
    )
    h = r.get_data(as_text=True)
    check("result card: 3 new as bank statement", fr(h, "fremdbank") == ("bank", 3, 0, 0), fr(h, "fremdbank"))
    rows = q(
        "SELECT booking_date, partner, description, amount FROM transactions WHERE source='bank' ORDER BY booking_date"
    )
    check(
        "amounts: debit negative, credit positive",
        [(r_["booking_date"], r_["amount"]) for r_ in rows]
        == [("2026-06-03", -900.0), ("2026-06-05", 2500.0), ("2026-06-07", -23.45)],
        [tuple(r_) for r_ in rows],
    )
    check(
        "partner/purpose mapped",
        rows[0]["partner"] == "Hausverwaltung Muster" and rows[0]["description"] == "Miete Juni",
    )
    maps = json.loads(one("SELECT value FROM settings WHERE key='bank_column_maps'")["value"])
    sig = "valuta|text|soll|haben|wer"
    check(
        "mapping remembered under the header signature",
        sig in maps and maps[sig]["debit"] == 2 and maps[sig]["credit"] == 3 and maps[sig]["date"] == 0,
        list(maps),
    )
    check("temp file gone", not os.path.exists(os.path.join(os.path.dirname(m.DB_PATH), "uploads", token + ".csv")))

    print("\n4) Next file of the same bank: no prompt")
    FREMD2 = csv(
        "Valuta;Text;Soll;Haben;Wer\n"
        "03.07.2026;Miete Juli;900,00;;Hausverwaltung Muster\n"
        "07.06.2026;Einkauf;23,45;;BILLA DANKT 0001\n"
    )
    h = upload(c, (FREMD2, "fremdbank_juli.csv"))
    check(
        "1 new, 1 known, kind bank, no mapping link",
        fr(h, "fremdbank_juli") == ("bank", 1, 1, 0) and "zuordnen" not in h,
        fr(h, "fremdbank_juli"),
    )

    print("\n5) Single amount column + deletion under settings")
    ANDERE = csv(
        "Tag,Wert,Wer,Was\n2026-08-01,-12.50,Cafe Muster,Kaffee\n2026-08-02,100.00,Max Beispiel,Rueckzahlung\n"
    )
    h = upload(c, (ANDERE, "andere.csv"))
    tok = re.search(r'href="/import/zuordnen\?t=([0-9a-f]{16})"', h)
    check("second bank: mapping link again", tok is not None)
    h = c.get("/import/zuordnen?t=" + tok.group(1)).get_data(as_text=True)
    check("single column suggested", re.search(r'name="mode" value="single" checked', h) is not None)
    r = c.post(
        "/import/zuordnen",
        data={"t": tok.group(1), "date": "0", "mode": "single", "amount": "1", "partner": "2", "desc": "3"},
        follow_redirects=True,
    )
    check("2 new", fr(r.get_data(as_text=True), "andere") == ("bank", 2, 0, 0))
    check(
        "signed amounts",
        [
            r_["amount"]
            for r_ in q(
                "SELECT amount FROM transactions WHERE partner IN ('Cafe Muster','Max Beispiel') ORDER BY booking_date"
            )
        ]
        == [-12.5, 100.0],
    )
    h = c.get("/einstellungen").get_data(as_text=True)
    check("settings list both mappings", h.count('class="colmap-row"') == 2 and 'value="colmap_delete"' in h)
    r = c.post("/einstellungen", data={"action": "colmap_delete", "sig": "tag|wert|wer|was"}, follow_redirects=True)
    check(
        "deleted: flash + only one left",
        "Spaltenzuordnung gelöscht" in r.get_data(as_text=True)
        and len(json.loads(one("SELECT value FROM settings WHERE key='bank_column_maps'")["value"])) == 1,
    )
    h = upload(c, (ANDERE, "andere2.csv"))
    check("without mapping 'format not recognised' again", fr(h, "andere2")[0] == "?" and "/import/zuordnen?t=" in h)

    print("\n6) Help page: toggle sources directly")
    h = c.get("/hilfe").get_data(as_text=True)
    check(
        "checkboxes for card/PayPal/Splitwise, bank fixed",
        'name="src_paypal"' in h
        and 'name="src_splitwise"' in h
        and 'name="src_card"' in h
        and 'name="src_bank"' not in h,
    )
    r = c.post("/hilfe", data={"src_paypal": "1"}, follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "saved: flash, only bank+PayPal",
        "Quellen gespeichert" in h
        and json.loads(one("SELECT value FROM settings WHERE key='sources'")["value"]) == ["bank", "paypal"],
    )
    check("help shows PayPal row, no Splitwise row", "<td>PayPal</td>" in h and "<td>Splitwise</td>" not in h)
    check("menu without Splitwise", 'href="/splitwise"' not in h and 'href="/paypal"' in h)
    c.post("/hilfe", data={"src_paypal": "1", "src_splitwise": "1", "src_card": "1"}, follow_redirects=True)
    check(
        "all on again",
        json.loads(one("SELECT value FROM settings WHERE key='sources'")["value"])
        == ["bank", "card", "paypal", "splitwise"],
    )

    print("\n7) Comparison with the shared period macro")
    h = c.get("/vergleich").get_data(as_text=True)
    check(
        "two dropdowns a_range/b_range, from/to hidden",
        h.count('class="pp-range"') == 2
        and 'name="a_range"' in h
        and 'name="b_range"' in h
        and h.count('class="pp-custom" hidden>') == 2,
    )
    check(
        "quick pick per side keeps the other side",
        re.search(r'href="/vergleich\?b_range=[^"&]+&(amp;)?a_range=last3"', h) is not None
        and re.search(r'href="/vergleich\?a_range=[^"&]+&(amp;)?b_range=last3"', h) is not None,
        re.findall(r'href="/vergleich\?[^"]*"', h)[:4],
    )
    h = c.get("/vergleich?a_range=last3&b_range=2026-06").get_data(as_text=True)
    check("selection stays", '<option value="last3" selected>' in h and '<option value="2026-06" selected>' in h)
    check(
        "hidden fields carry the other side",
        'name="b_range" value="2026-06"' in h and 'name="a_range" value="last3"' in h,
    )
    h = c.get("/vergleich?a_range=custom&a_from=2026-06&a_to=2026-07&b_range=2026-08").get_data(as_text=True)
    check(
        "custom period A: from/to visible, B hidden",
        re.search(r'id="a-from"[^>]*>', h) is not None
        and "disabled" not in re.search(r'<select name="a_from"[^>]*>', h).group(0)
        and re.search(r'<select name="b_from"[^>]*disabled', h) is not None
        and "Juni – Juli 2026" in h,
    )
    finish()
