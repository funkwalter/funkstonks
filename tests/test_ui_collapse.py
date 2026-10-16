"""9) Expand/collapse states in the rendered HTML: dashboard open, /jahr closed, .tgl script once."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402
from _ui_common import setup


def test_ui_collapse():

    m, c, tmp = setup()

    def cid(name):
        con = m.db()
        r = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        con.close()
        return r["id"]

    _h = [0]

    def add_tx(con, source, d, partner, desc, amount, cat=None):
        _h[0] += 1
        return con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES(?,?,?,?,?,'EUR',?,'active',?)",
            (source, d, partner, desc, amount, f"h{_h[0]}", cid(cat) if cat else None),
        ).lastrowid

    c.post("/setup", data={"src_bank": "1", "theme": "auto", "income_pattern": "Firma AG", "income_category": "Gehalt"})
    con = m.db()
    ls = cid("Lifestyle")
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Gaming','#111111','expense',?)", (ls,))
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Kleidung','#222222','expense',?)", (ls,))
    con.commit()
    add_tx(con, "bank", "2026-06-03", "BILLA", "Einkauf", -23.50, "Lebensmittel")
    add_tx(con, "bank", "2026-06-04", "Steam", "Spiel", -30.00, "Gaming")
    add_tx(con, "bank", "2026-06-05", "H&M", "Hose", -50.00, "Kleidung")
    add_tx(con, "bank", "2026-06-06", "Altbestand", "direkt auf Lifestyle", -5.00, "Lifestyle")
    add_tx(con, "bank", "2026-07-04", "Steam", "Spiel", -20.00, "Gaming")
    add_tx(con, "bank", "2026-07-07", "SPAR", "Einkauf", -10.00, "Lebensmittel")
    add_tx(con, "bank", "2026-06-28", "Firma AG", "Gehalt", 2500.00, "Gehalt")
    add_tx(con, "bank", "2026-06-29", "Unbekannt", "x", -7.00)
    con.commit()
    con.close()

    print("9a) app.js: .tgl script exactly once")
    for p in ("/", "/jahr", "/transactions", "/rules"):
        html = c.get(p).get_data(as_text=True)
        check(f"{p}: loads js/app.js exactly 1x", html.count("js/app.js") == 1, html.count("js/app.js"))
        check(f"{p}: no inline .tgl script", "querySelectorAll('.tgl')" not in html)
    base = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    check("app.js contains the .tgl script once", base.count("querySelectorAll('.tgl')") == 1)
    others = [
        f
        for f in os.listdir(os.path.join(ROOT, "templates"))
        + ["js/" + j for j in os.listdir(os.path.join(ROOT, "static", "js"))]
        if f != "js/app.js"
        and "querySelectorAll('.tgl')"
        in open(os.path.join(ROOT, "static" if f.startswith("js/") else "templates", f), encoding="utf-8").read()
    ]
    check("no second .tgl script in other templates or scripts", others == [], others)
    check("script: hidden toggle via tr.hidden", "tr.hidden = open" in base and "aria-expanded" in base)

    print("9b) Dashboard 'Ausgaben nach Kategorie': open (down marker, aria-expanded=true, children not hidden)")
    html = c.get("/?from=2026-06&to=2026-07").get_data(as_text=True)
    sec = html[html.index("Ausgaben nach Kategorie") :]
    sec = sec[: sec.index("</table>")]
    btns = re.findall(
        r'<button type="button" class="ghost tgl" data-key="(t\d+)"\s+aria-expanded="(\w+)"[^>]*>([^<]*)</button>', sec
    )
    check("dashboard: exactly 1 expand button (Lifestyle)", len(btns) == 1, btns)
    if btns:
        key, aexp, txt = btns[0]
        check("dashboard: button text is the down marker", txt.strip() == "▾", repr(txt))
        check("dashboard: aria-expanded=true", aexp == "true", aexp)
        kids = re.findall(r'<tr class="sub-%s"([^>]*)>' % key, sec)
        check("dashboard: 3 child rows (Gaming, Kleidung, ohne Unterkategorie)", len(kids) == 3, kids)
        check("dashboard: child rows WITHOUT hidden", all("hidden" not in k for k in kids), kids)
        check("dashboard: 'ohne Unterkategorie' for legacy bookings", "ohne Unterkategorie" in sec)
        kid_block = sec[sec.index('class="sub-%s"' % key) :]
        check(
            "dashboard: child rows link period + cat",
            'href="/transactions?from=2026-06&amp;to=2026-07&cat=' in kid_block,
        )
    # since 2026-09-13 the parent row links ALL children at once (cat=id,id,...)
    check(
        "dashboard: parent row Lifestyle links the child IDs",
        re.search(
            r'</button>\s*<a href="/transactions\?[^"]*cat=\d+,\d+[^"]*"[^>]*>\s*<i class="chip"[^>]*></i>Lifestyle',
            sec,
        )
        is not None,
        "",
    )
    check(
        "dashboard: Lebensmittel (no children) has a placeholder instead of a button",
        re.search(
            r'class="expand-space"></span><a href="[^"]*cat=\d+"[^>]*>\s*<i class="chip"[^>]*></i>Lebensmittel',
            sec.replace("\n", " "),
        )
        is not None,
    )
    check(
        "dashboard: total Lifestyle = 105.00 (50+50+5)",
        re.search(r"Lifestyle</a>\s*</td>\s*<td class=\"num\">105\.00 €", sec) is not None,
    )

    print("9c) /jahr category list: closed (right marker, children hidden)")
    html = c.get("/jahr?year=2026").get_data(as_text=True)
    sec = html[html.index("Was kostet mich wieviel") : html.index("Monat für Monat")]
    btns = re.findall(r'<button type="button" class="ghost tgl"\s+data-key="(\d+)"([^>]*)>([^<]*)</button>', sec)
    check(
        "/jahr list: exactly one button (Lifestyle has children; Lebensmittel without children has none)",
        len(btns) == 1,
        btns,
    )
    check(
        "/jahr list: all buttons show the right marker",
        all(t.strip() == "▸" for _, _, t in btns),
        [t for _, _, t in btns],
    )
    print(
        "       (info: /jahr list button has aria-expanded attribute: %s)"
        % any("aria-expanded" in a for _, a, _ in btns)
    )
    check(
        '/jahr list: aria-expanded="false" in the initial state (consistent with dashboard/matrix)',
        all('aria-expanded="false"' in a for _, a, _ in btns),
        [a for _, a, _ in btns],
    )
    for key, _, _ in btns:
        kids = re.findall(r'<tr class="sub-%s"([^>]*)>' % key, sec)
        check(
            f"/jahr list: sub-{key} rows present and hidden",
            len(kids) >= 1 and all("hidden" in k for k in kids),
            kids,
        )
    check(
        "/jahr list: Lifestyle children Gaming/Kleidung/ohne Unterkategorie",
        "Gaming" in sec and "Kleidung" in sec and "ohne Unterkategorie" in sec,
    )
    check(
        "/jahr list: no more payee rows (since 2026-09-15 only subcategories)",
        "BILLA" not in sec and "SPAR" not in sec,
    )

    print("9d) /jahr 'Monat fuer Monat': closed (right marker, aria-expanded=false, sub-mN hidden)")
    sec = html[html.index("Monat für Monat") :]
    sec = sec[: sec.index("</table>")]
    btns = re.findall(
        r'<button type="button" class="ghost tgl"\s+data-key="(m\d+)"\s+aria-expanded="(\w+)"[^>]*>([^<]*)</button>',
        sec,
    )
    check("matrix: exactly 1 button (Lifestyle)", len(btns) == 1, btns)
    if btns:
        key, aexp, txt = btns[0]
        check("matrix: right marker", txt.strip() == "▸", repr(txt))
        check("matrix: aria-expanded=false", aexp == "false", aexp)
        kids = re.findall(r'<tr class="sub-%s"([^>]*)>' % key, sec)
        check("matrix: 3 child rows hidden", len(kids) == 3 and all("hidden" in k for k in kids), kids)
        par = sec[: sec.index('class="sub-%s"' % key)]
        par = par[par.rindex("<tr>") :]
        check(
            "matrix: parent cell (group with children) does NOT link to cat=none",
            "cat=none" not in par,
            re.findall(r'href="[^"]*"', par)[:3],
        )
        links = re.findall(r'href="([^"]*)"', par)
        if links:
            d = c.get(links[0].replace("&amp;", "&")).get_data(as_text=True)
            check(
                "matrix: drilldown of the parent cell shows Lifestyle payments (Steam), not uncategorized",
                "Steam" in d and "Unbekannt" not in d,
                ("Steam" in d, "Unbekannt" in d, links[0]),
            )

    print("9e) No subcategories -> no buttons, no sub-")
    con = m.db()
    con.execute("DELETE FROM transactions WHERE category_id IN (SELECT id FROM categories WHERE parent_id IS NOT NULL)")
    con.commit()
    con.close()
    html = c.get("/?from=2026-06&to=2026-07").get_data(as_text=True)
    sec = html[html.index("Ausgaben nach Kategorie") :]
    check("dashboard without used subcategories: no tgl button", 'class="ghost tgl"' not in sec)
    html = c.get("/jahr?year=2026").get_data(as_text=True)
    sec = html[html.index("Monat für Monat") :]
    check("matrix without subcategories: no tgl button", 'class="ghost tgl"' not in sec)
    finish()
