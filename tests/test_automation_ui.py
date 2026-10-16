"""User requests 2026-10-13: suggestions in recurring costs only from allowed
categories and dismissable, "assign incoming payments" per series (creates
repayment rules), built-in automation can be switched off, import history and
rule list collapsed, source settings only after ticking the source."""

import json
import re

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_automation_ui():

    tmp, c = fresh_db()
    con = m.db()
    m.set_setting(con, "fixkosten_privat_cats", json.dumps(["Abos"]))
    abos = con.execute("SELECT id FROM categories WHERE name='Abos'").fetchone()["id"]
    leb = con.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()["id"]
    con.commit()
    con.close()

    print("1) Suggestions: only allowed categories, as a list, dismissable")
    BANK = george_bank(
        [
            ("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50"),
            ("10.06.2026", "Mobilfunk GmbH", "Rechnung", "-18,20"),
            ("10.07.2026", "Mobilfunk GmbH", "Rechnung", "-88,24"),
            ("11.06.2026", "Supermarkt Muster", "Einkauf", "-30,00"),
            ("11.07.2026", "Supermarkt Muster", "Einkauf", "-31,00"),
        ]
    )
    upload(c, (BANK, "b.csv"))
    con = m.db()
    con.execute("UPDATE transactions SET category_id=? WHERE partner='Mobilfunk GmbH'", (abos,))
    con.execute("UPDATE transactions SET category_id=? WHERE partner='Supermarkt Muster'", (leb,))
    con.commit()
    con.close()
    h = c.get("/fixkosten").get_data(as_text=True)
    check(
        "Mobilfunk (Abos) as suggestion row",
        'class="cand-row"' in h and "Mobilfunk GmbH" in h[h.find("Vorschläge") :],
    )
    check(
        "Supermarkt (Lebensmittel, not allowed) NOT suggested",
        "Supermarkt Muster" not in h[h.find("Vorschläge") :],
    )
    check("no dropdown any more", 'id="force-key"' not in h)
    key = re.search(r'<tr class="cand-row" data-key="([^"]+)">\s*<td><a[^>]*>Mobilfunk', h).group(1)
    r = c.post(
        "/fixkosten/override", data={"key": key, "mode": "dismiss", "label": "Mobilfunk GmbH"}, follow_redirects=True
    )
    h = r.get_data(as_text=True)
    check(
        "dismissed: flash + no longer suggested",
        "nicht mehr vorgeschlagen" in h and "Mobilfunk GmbH" not in h[h.find("Vorschläge") :],
        h[h.find("Vorschläge") :][:300],
    )
    c.post("/fixkosten/override", data={"key": key, "mode": "clear"}, follow_redirects=True)
    h = c.get("/fixkosten").get_data(as_text=True)
    check("cleared: back again", "Mobilfunk GmbH" in h[h.find("Vorschläge") :])

    print("\n2) Assign incoming payments: series + sender -> rule + netting")
    BANK2 = george_bank(
        [
            ("29.05.2026", "STREAMING AB", "Abo", "-20,99"),
            ("01.06.2026", "Max Beispiel", "Anteil", "3,00"),
            ("01.06.2026", "Lea Beispiel", "Streaming Lea", "3,50"),
            ("29.06.2026", "STREAMING AB", "Abo", "-20,99"),
            ("01.07.2026", "Max Beispiel", "Anteil", "3,00"),
            ("02.07.2026", "Lea Beispiel", "Streaming Lea", "3,50"),
            ("29.07.2026", "STREAMING AB", "Abo", "-20,99"),
            ("01.08.2026", "Max Beispiel", "Anteil", "3,00"),
            ("01.08.2026", "Lea Beispiel", "Streaming Lea", "3,50"),
            ("05.08.2026", "Arbeitgeber AG", "Gehalt", "3000,00"),
        ]
    )
    upload(c, (BANK2, "b2.csv"))
    con = m.db()
    con.execute("UPDATE transactions SET category_id=? WHERE partner='STREAMING AB'", (abos,))
    con.commit()
    con.close()
    h = c.get("/fixkosten").get_data(as_text=True)
    mo = re.search(r'<tr class="series-row" data-key="([^"]+)">\s*<td><a[^>]*>STREAMING AB', h)
    check("streaming series detected", bool(mo))
    skey = mo.group(1)
    check(
        "pencil on the series leads to the series page, no text link any more",
        "/fixkosten/serie?key=" in h and 'class="series-edit"' in h and "Eingänge zuordnen…" not in h,
    )
    check(
        "series page: assign incoming payments + add payments",
        "Eingänge zuordnen" in c.get("/fixkosten/serie?key=" + skey).get_data(as_text=True)
        and "Zahlungen dazunehmen" in c.get("/fixkosten/serie?key=" + skey).get_data(as_text=True),
    )
    h = c.get("/fixkosten/eingaenge?key=" + skey).get_data(as_text=True)
    check(
        "page lists the senders with count",
        'data-partner="Max Beispiel"' in h
        and 'data-partner="Lea Beispiel"' in h
        and 'data-partner="Arbeitgeber AG"' in h,
    )
    r = c.post(
        "/fixkosten/eingaenge", data={"key": skey, "partners": ["Max Beispiel", "Lea Beispiel"]}, follow_redirects=True
    )
    h = r.get_data(as_text=True)
    check(
        "flash: 2 senders added, 6 netted",
        "2 Absender dazu" in h and "6 Zahlungen verrechnet" in h,
        re.findall(r'class="flash[^"]*">([^<]*)', h),
    )
    rules = q("SELECT in_pattern, out_pattern FROM repay_rules ORDER BY id")
    check(
        "two repayment rules on the series term",
        {(r_["in_pattern"], r_["out_pattern"].lower()) for r_ in rules}
        == {("Max Beispiel", "streaming"), ("Lea Beispiel", "streaming")},
        [(r_["in_pattern"], r_["out_pattern"]) for r_ in rules],
    )
    sp = q("SELECT effective_amount FROM transactions WHERE partner='STREAMING AB' ORDER BY booking_date")
    check(
        "every debit counts as 14.49",
        all(abs(x["effective_amount"] + 14.49) < 0.005 for x in sp),
        [x["effective_amount"] for x in sp],
    )
    h = c.get("/fixkosten").get_data(as_text=True)
    check(
        "series shows inflows 19.50 and 14.49/month",
        "Rückflüsse −19.50 €" in h
        and re.search(r'STREAMING AB.*?<b title="[^"]*nach Rückzahlungen">14\.49 €', h, re.S) is not None,
    )
    h = c.get("/fixkosten/eingaenge?key=" + skey).get_data(as_text=True)
    check(
        "senders pre-ticked with counter",
        re.search(r'value="Max Beispiel" id="p\d+" checked', h) is not None and "3×" in h,
    )
    r = c.post("/fixkosten/eingaenge", data={"key": skey, "partners": ["Lea Beispiel"]}, follow_redirects=True)
    check(
        "Max removed: rule gone, links undone",
        count("1")
        and len(q("SELECT * FROM repay_rules")) == 1
        and count("partner='Max Beispiel' AND status='active'") == 3
        and all(
            abs(x["effective_amount"] + 17.49) < 0.005
            for x in q("SELECT effective_amount FROM transactions WHERE partner='STREAMING AB'")
        ),
        (
            [tuple(r_) for r_ in q("SELECT in_pattern FROM repay_rules")],
            count("partner='Max Beispiel' AND status='active'"),
        ),
    )
    r = c.get("/fixkosten/eingaenge?key=gibtsnicht", follow_redirects=True)
    check("unknown series -> FEHLER", "FEHLER" in r.get_data(as_text=True))
    # Shared pattern: two senders write "Streaming" in the text, ONE rule "Streaming" hits both
    BANK3 = george_bank(
        [
            ("03.06.2026", "Tom Beispiel", "Streaming Anteil", "2,00"),
            ("03.07.2026", "Tom Beispiel", "Streaming Anteil", "2,00"),
            ("03.06.2026", "Ida Beispiel", "mein Streaming", "2,00"),
            ("03.07.2026", "Ida Beispiel", "mein Streaming", "2,00"),
        ]
    )
    upload(c, (BANK3, "b3x.csv"))
    c.post(
        "/rules",
        data={"repay_add": "1", "in_pattern": "Streaming", "out_pattern": "streaming", "window_days": "45"},
        follow_redirects=True,
    )
    h = c.get("/fixkosten/serie?key=" + skey).get_data(as_text=True)
    check(
        "both senders ticked, with 'via rule Streaming'",
        re.search(r'value="Tom Beispiel" id="p\d+" checked', h) is not None
        and re.search(r'value="Ida Beispiel" id="p\d+" checked', h) is not None
        and h.count("über Regel „Streaming“") == 2,
        h.count("über Regel"),
    )
    n_rules = len(q("SELECT * FROM repay_rules"))
    r = c.post(
        "/fixkosten/eingaenge",
        data={"key": skey, "partners": ["Lea Beispiel", "Tom Beispiel", "Ida Beispiel"]},
        follow_redirects=True,
    )
    check(
        "saving without change: no rule removed, none added",
        len(q("SELECT * FROM repay_rules")) == n_rules and "0 Absender dazu, 0 entfernt" in r.get_data(as_text=True),
    )
    r = c.post(
        "/fixkosten/eingaenge", data={"key": skey, "partners": ["Lea Beispiel", "Tom Beispiel"]}, follow_redirects=True
    )
    h = r.get_data(as_text=True)
    check(
        "Ida unticked, Tom hangs on the same rule -> WARNUNG, rule stays",
        "WARNUNG" in h
        and "Regel „Streaming“" in h
        and len(q("SELECT * FROM repay_rules WHERE in_pattern='Streaming'")) == 1,
    )
    r = c.post("/fixkosten/eingaenge", data={"key": skey, "partners": ["Lea Beispiel"]}, follow_redirects=True)
    check(
        "both unticked -> rule 'Streaming' gone, their inflows active again",
        len(q("SELECT * FROM repay_rules WHERE in_pattern='Streaming'")) == 0
        and count("partner IN ('Tom Beispiel','Ida Beispiel') AND status='active'") == 4,
    )
    h = c.get("/rules").get_data(as_text=True)
    check("rules page names the matched senders per rule", "Lea Beispiel" in h and 'class="repay-example"' in h)

    print("\n3) Switch off built-in automation")
    h = c.get("/rules").get_data(as_text=True)
    check(
        "automation card collapsed with delete button",
        'data-persist="rules-auto"' in h and 'name="auto_delete" value="foreign_pos"' in h,
    )
    check("understandable description", "Terminal im Ausland" in h and "deine Regeln gehen immer vor" in h)
    r = c.post("/rules", data={"auto_delete": "foreign_pos"}, follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "switched off: listed under disabled with a re-enable button",
        "Abgeschaltet" in h and 'name="auto_restore" value="foreign_pos"' in h,
    )
    upload(c, (george_bank([("10.08.2026", "Lokal Bratislava", "POS 25,00 SK K1 10.08. 12:00", "-25,00")]), "b3.csv"))
    check(
        "foreign POS stays uncategorised (automation off)",
        one("SELECT category_id FROM transactions WHERE partner='Lokal Bratislava'")["category_id"] is None,
    )
    c.post("/rules", data={"auto_restore": "foreign_pos"}, follow_redirects=True)
    upload(c, (george_bank([("11.08.2026", "Lokal Praha", "POS 25,00 CZ K1 11.08. 12:00", "-25,00")]), "b4.csv"))
    urlaub = one("SELECT id FROM categories WHERE name='Urlaub'")["id"]
    check(
        "on again: next import categorises Urlaub",
        one("SELECT category_id FROM transactions WHERE partner='Lokal Praha'")["category_id"] == urlaub,
    )
    check(
        "setting stored as JSON list",
        json.loads(one("SELECT value FROM settings WHERE key='disabled_automations'")["value"]) == [],
    )
    c.post("/rules", data={"auto_delete": "sw:Wohnung"}, follow_redirects=True)
    con = m.db()
    check(
        "Splitwise keyword 'miete' yields nothing without automation",
        m.splitwise_category_name(con, "miete april") is None and m.map_splitwise_category("miete april") == "Wohnung",
    )
    con.close()

    print("\n4) Collapsed cards and dynamic source settings")
    h = c.get("/import").get_data(as_text=True)
    check(
        "import history as collapsed card",
        'data-persist="import-history"' in h
        and re.search(r'<details class="card card-toggle" data-persist="import-history">', h) is not None,
    )
    h = c.get("/rules").get_data(as_text=True)
    check("rule list as one expandable card", 'data-persist="rules-list"' in h)
    h = c.get("/einstellungen").get_data(as_text=True)
    check(
        "Splitwise block present, visible (source on)",
        'data-source-block="splitwise" >' in h or 'data-source-block="splitwise">' in h,
    )
    c.post("/einstellungen", data={"action": "sources", "src_bank": "1"})
    h = c.get("/einstellungen").get_data(as_text=True)
    check(
        "Splitwise off: block hidden but in the HTML (JS shows it when ticked)",
        'data-source-block="splitwise" hidden' in h,
    )
    finish()
