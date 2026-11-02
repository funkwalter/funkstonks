import colorsys
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_final():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    # mark setup as done - otherwise every page redirects to /setup
    _c = m.db()
    m.set_setting(_c, "setup_done", "1")
    _c.commit()
    _c.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    con = m.db()

    def cat_id(name, kind="expense", parent=None):
        r = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        if r:
            return r["id"]
        con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES(?,'#888888',?,?)", (name, kind, parent))
        return con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()["id"]

    woh = cat_id("Wohnung")
    rest = cat_id("Restaurant/Ausgehen")
    inv = cat_id("Investitionen")
    geh = cat_id("Gehalt", "income")
    fg = cat_id("Fortgehen", "expense", rest)
    ma = cat_id("Mittagessen Arbeit", "expense", rest)

    def tx(d, p, a, cid, h, effa=None):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id,effective_amount) VALUES('bank',?,?,'x',?,'EUR',?,'active',?,?)""",
            (d, p, a, h, cid, effa),
        )

    tx("2026-05-01", "Gehalt AG", 3000, geh, "i1")
    tx("2026-05-04", "Hausverwaltung Muster KG", -1000, woh, "w1", -500)
    tx("2026-06-10", "WIEN ENERGIE Vertrieb", -170.40, woh, "w2", -85.20)
    tx("2026-06-15", "T-Mobile Austria GmbH", -35.64, woh, "w3", -17.82)
    tx("2026-05-20", "Beisl Zur Post", -40, rest, "r1")
    tx("2026-05-21", "Cocktailbar", -60, fg, "r2")
    tx("2026-06-02", "ETF Sparplan", -100, inv, "v1")
    con.commit()
    con.close()

    print("1) Year view: sub-rows collapsed, expandable")
    html = c.get("/jahr?y=2026").get_data(as_text=True)
    check("expand button present", 'class="ghost tgl"' in html)
    subs = re.findall(r'<tr class="sub-\d+" hidden>', html)
    check("hidden sub-rows only for subcategories (restaurant: 2)", len(subs) == 2, len(subs))
    check("no sub-row without hidden", len(re.findall(r'<tr class="sub-\d+">', html)) == 0)
    check(
        "toggle JS present",
        "tr.hidden = open" in open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read(),
    )

    print("\n2) NO payees under Wohnung (since 2026-09-15: only subcategories expandable)")
    i = html.find(">Wohnung</a>")
    seg = html[i : i + 2500] if i > 0 else html
    check("no landlord as sub-row", "Muster" not in seg, seg[:200])
    check("no electricity payee", "WIEN ENERGIE" not in seg)
    check("no internet payee", "T-Mobile" not in seg)
    wrow = html[html.rfind("<tr", 0, i) : i]
    check("Wohnung without expand button", "tgl" not in wrow, wrow[-200:])

    print("\n3) Under restaurant are the subcategories, no payees")
    j = html.find("Restaurant/Ausgehen")
    seg2 = html[j : j + 2500]
    check("subcategory Fortgehen", "Fortgehen" in seg2)
    check("row 'ohne Unterkategorie'", "ohne Unterkategorie" in seg2)
    check("no payee name", "Beisl" not in seg2 and "Cocktailbar" not in seg2)

    print("\n4) Investitionen blue in the Sankey")
    import json as _json

    d = c.get("/?month=2026-06").get_data(as_text=True)
    mS = re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', d, re.S)
    check("Sankey data in the HTML", mS is not None)
    if mS:
        S = _json.loads(mS.group(1))
        idx = [k for k, lab in enumerate(S["labels"]) if lab.startswith("Investitionen")]
        check("Investitionen node present", len(idx) == 1, S["labels"])
        if idx:
            hexv = S["colors"][idx[0]]
            r_, g_, b_ = (int(hexv[k : k + 2], 16) for k in (1, 3, 5))
            check(f"color is blue ({hexv})", b_ > r_ + 15 and b_ > g_ + 15, (r_, g_, b_))
            # counter-check: the credit branch stays green, so it is clearly distinguishable
            gi = [k for k, lab in enumerate(S["labels"]) if lab.startswith("Guthaben")]
            if gi:
                gh = S["colors"][gi[0]]
                gr, gg, gb = (int(gh[k : k + 2], 16) for k in (1, 3, 5))
                check(f"Guthaben stays green ({gh})", gg > gb and gg > gr, (gr, gg, gb))

    print("\n5) Color assignment: top-level categories distinct, children similar")
    con = m.db()
    n = m.assign_category_colors(con)
    con.commit()
    rows = {r["name"]: (r["color"], r["parent_id"]) for r in con.execute("SELECT name,color,parent_id FROM categories")}
    con.close()
    check("all categories colored", n == len(rows), (n, len(rows)))
    tops = [v[0] for v in rows.values() if v[1] is None]
    check(
        "top-level categories pairwise distinct",
        len(set(tops)) == len(tops),
        f"{len(set(tops))} verschiedene von {len(tops)}",
    )

    def hue_of(hexs):
        r_, g_, b_ = (int(hexs[k : k + 2], 16) / 255 for k in (1, 3, 5))
        return colorsys.rgb_to_hls(r_, g_, b_)[0] * 360

    p_hue = hue_of(rows["Restaurant/Ausgehen"][0])
    for kid in ("Fortgehen", "Mittagessen Arbeit"):
        dh = abs(hue_of(rows[kid][0]) - p_hue)
        dh = min(dh, 360 - dh)
        check(f"{kid}: similar hue (delta {dh:.0f} degrees)", dh <= 45, dh)
        check(f"{kid}: not identical to the parent color", rows[kid][0] != rows["Restaurant/Ausgehen"][0])
    check("children distinct from each other", rows["Fortgehen"][0] != rows["Mittagessen Arbeit"][0])
    # stability: a second run yields the same colors
    con = m.db()
    m.assign_category_colors(con)
    con.commit()
    again = {r["name"]: r["color"] for r in con.execute("SELECT name,color FROM categories")}
    con.close()
    check("color assignment is stable (same result on a second run)", all(again[k] == v[0] for k, v in rows.items()))
    check("button on /categories", "Farben neu verteilen" in c.get("/categories").get_data(as_text=True))
    check("recolor route", c.post("/categories", data={"recolor": "1"}).status_code in (200, 302))

    print("\n6) Regressions")
    for p in ("/", "/transactions", "/jahr", "/splitwise", "/rules", "/fixkosten", "/categories", "/partner"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
