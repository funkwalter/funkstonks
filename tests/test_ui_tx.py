"""5) /review, 6) /transactions filters, 7) /tx/<id>/update, 8) /tx/<id>/split."""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402
from _ui_common import get as _get
from _ui_common import post as _post
from _ui_common import setup


def test_ui_tx():

    m, c, tmp = setup()

    def post(url, data, follow=False):
        return _post(c, url, data, follow)

    def cid(name):
        con = m.db()
        r = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        con.close()
        return r["id"]

    _h = [0]

    def add_tx(con, source, d, partner, desc, amount, status="active", cat=None, extra="{}", eff=None):
        _h[0] += 1
        cidv = cid(cat) if cat else None
        return con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id,extra,effective_amount) "
            "VALUES(?,?,?,?,?,'EUR',?,?,?,?,?)",
            (source, d, partner, desc, amount, f"h{_h[0]}", status, cidv, extra, eff),
        ).lastrowid

    def tx(i):
        con = m.db()
        r = con.execute("SELECT * FROM transactions WHERE id=?", (i,)).fetchone()
        con.close()
        return dict(r)

    c.post(
        "/setup",
        data={
            "src_bank": "1",
            "src_paypal": "1",
            "src_card": "1",
            "src_splitwise": "1",
            "theme": "auto",
            "income_pattern": "Firma AG",
            "income_category": "Gehalt",
            "splitwise_name": "Ich",
        },
    )
    con = m.db()
    con.execute("DELETE FROM rules WHERE pattern!='Firma AG'")
    con.commit()
    con.close()
    lm = cid("Lebensmittel")
    ko = cid("Konsum")
    ls = cid("Lifestyle")
    ge = cid("Gehalt")
    ur = cid("Urlaub")
    ba = cid("Bargeld")
    con = m.db()
    # subcategories for Lifestyle
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Gaming','#111111','expense',?)", (ls,))
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Kleidung','#222222','expense',?)", (ls,))
    con.commit()
    gam = cid("Gaming")
    kle = cid("Kleidung")
    u1 = add_tx(con, "bank", "2026-06-03", "BILLA DANKT 1", "Einkauf", -23.50)
    u2 = add_tx(con, "bank", "2026-06-04", "Unbekannt GmbH", "irgendwas", -9.90)
    k1 = add_tx(con, "bank", "2026-06-05", "SPAR", "Einkauf", -12.00, cat="Lebensmittel")
    k2 = add_tx(con, "paypal", "2026-06-10", "Steam", "Spiel", -30.00, cat="Gaming")
    k3 = add_tx(con, "card", "2026-07-12", "AMAZON", "Bestellung", -40.00, cat="Konsum")
    inc = add_tx(con, "bank", "2026-06-28", "Firma AG", "Gehalt", 2500.00, cat="Gehalt")
    ex1 = add_tx(
        con, "bank", "2026-06-15", "PayPal Europe", "PayPal", -30.00, status="excluded", cat="PayPal-Verrechnung"
    )
    sw1 = add_tx(
        con,
        "splitwise",
        "2026-06-20",
        "wg",
        "Pizza",
        -10.0,
        status="info",
        extra=json.dumps({"cost": 20.0, "bal": -10.0, "sw_category": "Essen", "members": ["Bob"]}),
    )
    swp = add_tx(
        con,
        "splitwise",
        "2026-06-21",
        "wg",
        "Bob zahlte Ich",
        5.0,
        status="info",
        extra=json.dumps({"is_payment": True, "cost": 5.0, "bal": 5.0}),
    )
    sp = add_tx(con, "bank", "2026-07-01", "Bankomat", "Abhebung", -100.00)
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (sp, ba, -50))
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (sp, ko, -50))
    con.execute("UPDATE transactions SET note='[aufgeteilt]' WHERE id=?", (sp,))
    a1 = add_tx(con, "bank", "2026-08-02", "Hotel", "Urlaub", -200.00, cat="Urlaub")
    con.commit()
    con.close()

    print("5) /review")
    r = c.get("/review")
    html = r.get_data(as_text=True)
    check("review 200", r.status_code == 200, r.status_code)
    check("review shows newest uncategorized (Unbekannt GmbH)", "Unbekannt GmbH" in html)
    check("review shows open count 2", re.search(r"\b2\b", html) is not None)
    check("cat-slot buttons present", html.count('class="ghost cat-slot"') >= 5, html.count('class="ghost cat-slot"'))
    check(
        "Lifestyle (with children) = expand button type=button, no submit",
        re.search(
            r'<button type="button" class="ghost cat-slot cat-parent-btn"[^>]*>\s*<i[^>]*></i>Lifestyle ▾</button>',
            html,
        )
        is not None,
    )
    check("Lifestyle itself NOT as submit button", re.search(r'name="category_id" value="%d"' % ls, html) is None)
    check(
        "children Gaming/Kleidung as submit in cat-children",
        'name="category_id" value="%d"' % gam in html and 'name="category_id" value="%d"' % kle in html,
    )
    check("income category not as button", 'name="category_id" value="%d"' % ge not in html)
    check(
        "keyboard shortcut script on .cat-slot",
        "querySelectorAll('.cat-slot')"
        in open(os.path.join(ROOT, "static", "js", "review.js"), encoding="utf-8").read(),
    )
    # the suggestion is the partner name WITHOUT digits (_norm_partner) so the rule matches every branch
    check("rule_pattern prefilled with partner without digits", 'name="rule_pattern" value="Unbekannt GmbH"' in html)
    check("make_rule unchecked", re.search(r'name="make_rule"[^>]*checked', html) is None)
    # POST categorize with rule + custom pattern
    r = post(
        "/review",
        {
            "tx_id": str(u2),
            "o": "0",
            "correction": "",
            "category_id": str(ko),
            "make_rule": "1",
            "rule_pattern": "irgendwas",
        },
    )
    check("review POST -> 302", not isinstance(r, str) and r.status_code == 302, r)
    check("categorized", tx(u2)["category_id"] == ko, tx(u2)["category_id"])
    con = m.db()
    rr = con.execute("SELECT pattern, category_id FROM rules WHERE pattern='irgendwas'").fetchone()
    con.close()
    check("rule with custom pattern 'irgendwas' created", rr and rr["category_id"] == ko, rr and dict(rr))
    r = post(
        "/review",
        {"tx_id": str(u1), "o": "0", "correction": "", "category_id": str(lm), "make_rule": "1", "rule_pattern": ""},
    )
    con = m.db()
    rr = con.execute("SELECT pattern FROM rules WHERE pattern='BILLA DANKT 1'").fetchone()
    con.close()
    check("empty rule_pattern -> fallback partner name", rr is not None)
    r = c.get("/review")
    html = r.get_data(as_text=True)
    # The flash names the pattern just created, so the partner name alone proves nothing;
    # the list itself is empty when no category button is left.
    check(
        "review empty: nothing open anymore",
        "Unbekannt GmbH" not in html and 'name="category_id"' not in html and r.status_code == 200,
    )
    r = c.get(f"/review?tx={u1}")
    html = r.get_data(as_text=True)
    check("correction mode shows the payment", "BILLA DANKT 1" in html and "Zurück zur Warteschlange" in html)
    r = post("/review", {"tx_id": str(u1), "o": "0", "correction": "1", "exclude": "1"})
    check("review exclude", tx(u1)["status"] == "excluded" and "ausgeschlossen" in tx(u1)["note"], tx(u1))
    con = m.db()
    con.execute("UPDATE transactions SET status='active', note='' WHERE id=?", (u1,))
    con.commit()
    con.close()
    r = post("/review", {"tx_id": "99999", "o": "0", "category_id": str(ko)})
    check("review unknown tx no 500", not isinstance(r, str), r)
    r = post("/review", {"tx_id": "abc", "category_id": str(ko)})
    check("review tx_id garbage no 500", not isinstance(r, str), r)
    r = post("/review", {"tx_id": str(u1), "category_id": "abc"})
    check("review category_id garbage no 500", not isinstance(r, str), r)
    r = _get(c, "/review?o=abc")
    check("review o=abc no 500", not isinstance(r, str), r)
    r = _get(c, "/review?o=-5")
    check("review o=-5 200", not isinstance(r, str) and r.status_code == 200, r)
    r = _get(c, "/review?tx=99999")
    check("review tx=99999 no 500", not isinstance(r, str) and r.status_code == 200, r)

    print("\n6) /transactions filters")

    def rows_of(url):
        r = _get(c, url)
        if isinstance(r, str):
            return r, ""
        html = r.get_data(as_text=True)
        ids = [int(x) for x in re.findall(r'class="tx-cat" data-tx-id="(\d+)"', html)]
        ids += [int(x) for x in re.findall(r'href="/tx/(\d+)/split\?back=/transactions" class="mute"', html)]
        return sorted(set(ids)), html

    ids, html = rows_of("/transactions")
    check("without filter: all except splitwise-info", ids == sorted([u1, u2, k1, k2, k3, inc, ex1, sp, a1]), ids)
    check("row hint 'N Zeilen'", re.search(r"9 Zeilen\.", html) is not None)
    check("no period clear link without period", "Zeitraum" not in html or "×</a>" not in html)
    ids, html = rows_of("/transactions?month=2026-06")
    check("month=2026-06", ids == sorted([u1, u2, k1, k2, inc, ex1]), ids)
    check("month: no hidden from/to", 'name="from"' not in html)
    ids, html = rows_of("/transactions?from=2026-06&to=2026-07")
    check("from/to 06-07", ids == sorted([u1, u2, k1, k2, k3, inc, ex1, sp]), ids)
    check(
        "from/to: hidden from+to in the filter form",
        '<input type="hidden" name="from" value="2026-06">' in html
        and '<input type="hidden" name="to" value="2026-07">' in html,
    )
    check(
        "from/to: 'Zeitraum ...' clear link",
        re.search(r"Zeitraum Juni – Juli 2026 ×</a>", html) is not None,
        re.findall(r"Zeitraum[^<]*", html)[:3],
    )
    check("from/to: month dropdown empty option = period text", ">Juni – Juli 2026</option>" in html)
    ids, html = rows_of("/transactions?from=2026-06&to=2026-07&month=2026-07")
    check("month beats from/to", ids == sorted([k3, sp]), ids)
    check("month+from/to: hidden from NOT rendered", 'name="from"' not in html)
    ids, html = rows_of("/transactions?from=2026-07")
    check("only from -> single month", ids == sorted([k3, sp]), ids)
    ids, html = rows_of("/transactions?from=2026-07&to=2026-06")
    print(f"       (Info: from>to -> {ids})")
    check("from>to no 500", not isinstance(ids, str), ids)
    ids, html = rows_of(f"/transactions?cat={lm}")
    check("cat single", ids == sorted([u1, k1]), ids)
    ids, html = rows_of(f"/transactions?cat={ko}")
    check("cat Konsum incl. split parent", ids == sorted([u2, k3, sp]), ids)
    check(
        "split share shown (davon -50.00 EUR hier)",
        re.search(r"davon\s+-50\.00 € hier", html) is not None,
        re.findall(r"aufgeteilt[^<]{0,40}", html)[:2],
    )
    ids, html = rows_of(f"/transactions?cat={lm},{gam}")
    check("cat list", ids == sorted([u1, k1, k2]), ids)
    check("cat list: dropdown 'mehrere Kategorien'", ">mehrere Kategorien<" in html)
    ids, html = rows_of(f"/transactions?cat={lm},none")
    check("cat list + none", ids == sorted([u1, k1]), ids)  # u2 is categorized by now
    con = m.db()
    con.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (u2,))
    con.commit()
    con.close()
    ids, html = rows_of("/transactions?cat=none")
    check("cat=none: only uncategorized active without splits", ids == [u2], ids)
    ids, html = rows_of(f"/transactions?cat={lm},none")
    check("cat list + none includes uncategorized", ids == sorted([u1, k1, u2]), ids)
    ids, html = rows_of("/transactions?cat=ausgaben")
    check("cat=ausgaben (legacy value)", ids == sorted([u1, k1, k2, k3, sp, a1]), ids)
    ids, html = rows_of("/transactions?cat=abc")
    check("cat=abc no 500, same as without filter", not isinstance(ids, str) and len(ids) == 9, ids)
    ids, html = rows_of("/transactions?kind=income")
    check("kind=income", ids == [inc], ids)
    ids, html = rows_of("/transactions?kind=expense")
    check("kind=expense incl. split", ids == sorted([u1, k1, k2, k3, sp, a1]), ids)
    check("kind hidden in the form", '<input type="hidden" name="kind" value="expense">' in html)
    ids, html = rows_of("/transactions?src=paypal")
    check("src=paypal", ids == [k2], ids)
    ids, html = rows_of("/paypal")
    check("/paypal = src paypal", ids == [k2], ids)
    ids, html = rows_of("/karte")
    check("/karte = src card", ids == [k3], ids)
    ids, html = rows_of("/transactions?status=excluded")
    check("status=excluded", ids == [ex1], ids)
    ids, html = rows_of("/transactions?status=info")
    check("status=info shows raw Splitwise", ids == sorted([sw1, swp]), ids)
    ids, html = rows_of("/transactions?q=billa")
    check("q partner (case-insens.)", ids == [u1], ids)
    ids, html = rows_of("/transactions?q=Bestell")
    check("q description", ids == [k3], ids)
    ids, html = rows_of("/transactions?q=%25")
    print(f"       (Info: q='%' -> {len(ids)} rows; LIKE wildcard is not escaped)")
    ids, html = rows_of("/transactions?year=2026")
    check("year=2026", len(ids) == 9, ids)
    # year= has been treated as the period January-December since 2026-09-11 (fixed-cost smoothing)
    check(
        "year as from/to hidden in the form",
        '<input type="hidden" name="from" value="2026-01">' in html and 'name="to" value="2026-12"' in html,
    )
    ids, html = rows_of("/transactions?year=2026&cat=%d&src=bank&status=active&q=SPAR" % lm)
    check("combination year+cat+src+status+q", ids == [k1], ids)
    ids, html = rows_of("/transactions?month=2026-06&cat=%d&kind=expense&src=bank" % lm)
    check("combination month+cat+kind+src", ids == sorted([u1, k1]), ids)
    ids, html = rows_of("/transactions?month=abc")
    check("month=abc no 500", not isinstance(ids, str), ids)
    ids, html = rows_of("/transactions?from=abc&to=xyz")
    check("from/to garbage no 500", not isinstance(ids, str), ids)
    check(
        "split row without dropdown, with 'aufgeteilt' marker",
        'data-tx-id="%d"' % sp not in rows_of("/transactions")[1].split("tx-toggle")[0]
        or "÷ aufgeteilt" in rows_of("/transactions")[1],
    )
    # fixed-cost smoothing in the month filter
    con = m.db()
    woh = cid("Wohnung")
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'')", (woh,)
    )
    f1 = add_tx(con, "bank", "2026-05-30", "Hausverwaltung", "Miete Juni", -900, cat="Wohnung", eff=-450)
    f2 = add_tx(con, "bank", "2026-07-01", "Hausverwaltung", "Miete Juli", -900, cat="Wohnung", eff=-450)
    con.commit()
    con.close()
    wm = None
    con = m.db()
    wm = m.wohnung_fixed_display_months(con)
    con.close()
    print(f"       (Info: smoothing {wm})")
    # smoothing: the first entry keeps its month (May), the next one (01.07.) moves to June
    check("smoothing map: f1->May, f2->June", wm.get(f1) == "2026-05" and wm.get(f2) == "2026-06", wm)
    ids, html = rows_of("/transactions?month=2026-06")
    check(
        "month filter June shows the rent smoothed to June (01.07.), not the one from 30.05.",
        f2 in ids and f1 not in ids,
        ids,
    )
    ids, html = rows_of("/transactions?month=2026-07")
    check("July without the rent moved to June", f2 not in ids, ids)
    ids, html = rows_of("/transactions?month=2026-05")
    check("May with the rent from 30.05.", f1 in ids, ids)
    # the dashboard table uses the same smoothing
    r = c.get("/?month=2026-06")
    h6 = r.get_data(as_text=True)
    check("dashboard June shows Wohnung 450 (smoothed)", "Wohnung" in h6 and "450.00" in h6)

    print("\n6b) Row limit (TX_LIST_LIMIT)")
    con = m.db()
    for i in range(m.TX_LIST_LIMIT + 50):
        add_tx(con, "bank", "2025-%02d-%02d" % (1 + i % 12, 1 + i % 28), "Massen %d" % i, "x", -1.0, cat="Konsum")
    con.commit()
    con.close()
    r = c.get("/transactions")
    html = r.get_data(as_text=True)
    n = len(re.findall(r'class="tx-cat" data-tx-id="(\d+)"', html)) + len(
        re.findall(r'href="/tx/(\d+)/split\?back=/transactions" class="mute"', html)
    )
    check("max TX_LIST_LIMIT rows without period", n == m.TX_LIST_LIMIT, n)
    check("hint 'Nur die N neuesten'", f"Nur die {m.TX_LIST_LIMIT} neuesten Zeilen" in html)
    r = c.get("/transactions?year=2025")
    html = r.get_data(as_text=True)
    check("year: hint above the limit", f"Nur die {m.TX_LIST_LIMIT} neuesten Zeilen" in html)
    r = c.get("/transactions?from=2025-01&to=2025-12")
    html = r.get_data(as_text=True)
    n = len(re.findall(r'class="tx-cat" data-tx-id="(\d+)"', html))
    check(
        "from/to: limit + hint as well",
        n == m.TX_LIST_LIMIT and f"Nur die {m.TX_LIST_LIMIT} neuesten Zeilen" in html,
        n,
    )
    r = c.get("/transactions?month=2025-01")
    html = r.get_data(as_text=True)
    check("month below the limit: 'N Zeilen'", re.search(r"\d+ Zeilen\.", html) is not None and "Nur die" not in html)
    con = m.db()
    con.execute("DELETE FROM transactions WHERE partner LIKE 'Massen %'")
    con.commit()
    con.close()

    print("\n7) /tx/<id>/update")
    r = post(f"/tx/{u2}/update", {"category_id": str(lm)})
    check("update category 302", not isinstance(r, str) and r.status_code == 302 and tx(u2)["category_id"] == lm, r)
    r = post(f"/tx/{u2}/update", {"category_id": str(ko), "ajax": "1"})
    check(
        "update category ajax -> 204",
        not isinstance(r, str) and r.status_code == 204 and tx(u2)["category_id"] == ko,
        r,
    )
    r = post(f"/tx/{u2}/update", {"category_id": ""})
    check("update category empty -> NULL", tx(u2)["category_id"] is None)
    r = post(f"/tx/{u2}/update", {"comment": "  Notiz hier  ", "ajax": "1"})
    check(
        "comment ajax -> 204, trimmed",
        not isinstance(r, str) and r.status_code == 204 and tx(u2)["comment"] == "Notiz hier",
        (r, tx(u2)["comment"]),
    )
    r = post(f"/tx/{u2}/update", {"comment": "", "ajax": "1"})
    check("empty comment deletes", tx(u2)["comment"] == "")
    html = c.get("/transactions").get_data(as_text=True)
    post(f"/tx/{u2}/update", {"comment": "Sichtbar", "ajax": "1"})
    html = c.get("/transactions").get_data(as_text=True)
    check("comment in HTML", "Sichtbar" in html and "tx-comment-view" in html)
    check("note untouched by the comment", tx(u2)["note"] == "")
    # make_rule
    con = m.db()
    u3 = add_tx(con, "bank", "2026-07-20", "Unbekannt GmbH", "nochmal", -5)
    con.commit()
    con.close()
    r = post(f"/tx/{u2}/update", {"category_id": str(ko), "make_rule": "1"})
    con = m.db()
    rr = con.execute("SELECT * FROM rules WHERE pattern='Unbekannt GmbH'").fetchone()
    con.close()
    check("make_rule creates partner rule", rr and rr["category_id"] == ko, rr and dict(rr))
    check("make_rule applies rule to others", tx(u3)["category_id"] == ko, tx(u3)["category_id"])
    r = post(f"/tx/{u2}/update", {"category_id": "", "make_rule": "1"})
    check("make_rule without category no 500", not isinstance(r, str), r)
    # toggle
    r = post(f"/tx/{k1}/update", {"toggle": "1", "ajax": "1"})
    check(
        "toggle -> excluded, 204", not isinstance(r, str) and r.status_code == 204 and tx(k1)["status"] == "excluded", r
    )
    r = post(f"/tx/{k1}/update", {"toggle": "1"})
    check("toggle back -> active", tx(k1)["status"] == "active")
    r = post(f"/tx/{sw1}/update", {"toggle": "1"})
    print(f"       (Info: toggle on splitwise status='info' -> {tx(sw1)['status']!r})")
    con = m.db()
    con.execute("UPDATE transactions SET status='info' WHERE id=?", (sw1,))
    con.commit()
    con.close()
    # splitwise info -> active
    r = post(f"/tx/{sw1}/update", {"category_id": str(lm)})
    check(
        "splitwise info + category -> active", tx(sw1)["status"] == "active" and tx(sw1)["category_id"] == lm, tx(sw1)
    )
    r = post(f"/tx/{sw1}/update", {"category_id": ""})
    check(
        "removing the category keeps active", tx(sw1)["status"] == "active" and tx(sw1)["category_id"] is None, tx(sw1)
    )
    r = post(f"/tx/{swp}/update", {"category_id": str(lm)})
    check(
        "settlement payment gets no category",
        tx(swp)["category_id"] is None and tx(swp)["status"] == "info",
        tx(swp),
    )
    r = post(f"/tx/{swp}/update", {"category_id": str(lm), "ajax": "1"})
    check("settlement payment ajax -> 204", not isinstance(r, str) and r.status_code == 204, r)
    r = post(f"/tx/{k1}/update", {"effective": "-6,00"})
    check("set effective", tx(k1)["effective_amount"] == -6.0, tx(k1)["effective_amount"])
    r = post(f"/tx/{k1}/update", {"effective": ""})
    check("effective empty -> NULL", tx(k1)["effective_amount"] is None)
    r = post(f"/tx/{k1}/update", {"effective": "abc"})
    check("effective garbage no 500 -> NULL", not isinstance(r, str) and tx(k1)["effective_amount"] is None, r)
    r = post(f"/tx/{k1}/update", {"category_id": "99999"})
    check("category_id unknown no 500", not isinstance(r, str), r)
    r = post(f"/tx/{k1}/update", {"category_id": "abc"})
    check("category_id garbage no 500", not isinstance(r, str), r)
    con = m.db()
    con.execute("UPDATE transactions SET category_id=? WHERE id=?", (lm, k1))
    con.commit()
    con.close()
    r = post("/tx/99999/update", {"category_id": str(lm)})
    check("unknown tx no 500", not isinstance(r, str) and r.status_code == 302, r)
    r = post(f"/tx/{k1}/update", {})
    check("empty POST no 500", not isinstance(r, str), r)
    # category on a split parent via update (no dropdown in the UI, but the route is open)
    r = post(f"/tx/{sp}/update", {"category_id": str(lm)})
    print(
        f"       (Info: category_id on split parent via POST -> {tx(sp)['category_id']}; splits remain, route allows it)"
    )
    con = m.db()
    con.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (sp,))
    con.commit()
    con.close()

    print("\n8) /tx/<id>/split")
    r = _get(c, f"/tx/{k3}/split")
    check("split GET 200", not isinstance(r, str) and r.status_code == 200, r)
    html = r.get_data(as_text=True)
    check("split GET shows amount", "-40.00" in html or "40,00" in html or "-40" in html)
    r = post(f"/tx/{k3}/split", {"cat": [str(lm), str(ko)], "amt": ["-15,00", "-25,00"], "back": "/transactions"})
    check(
        "split POST correct -> 302 to back",
        not isinstance(r, str) and r.status_code == 302 and r.headers["Location"].endswith("/transactions"),
        r,
    )
    t = tx(k3)
    con = m.db()
    sp_rows = con.execute("SELECT category_id, amount FROM splits WHERE tx_id=? ORDER BY category_id", (k3,)).fetchall()
    con.close()
    check("category_id NULL after split", t["category_id"] is None, t["category_id"])
    check("note has [aufgeteilt] exactly once", t["note"].count("[aufgeteilt]") == 1, t["note"])
    check(
        "2 splits stored",
        [(x[0], x[1]) for x in sp_rows] == sorted([(lm, -15.0), (ko, -25.0)]),
        [tuple(x) for x in sp_rows],
    )
    r = post(f"/tx/{k3}/split", {"cat": [str(lm), str(ko)], "amt": ["-10", "-30"]})
    t = tx(k3)
    check("save again: [aufgeteilt] still exactly once", t["note"].count("[aufgeteilt]") == 1, t["note"])
    con = m.db()
    sp_rows = con.execute("SELECT category_id, amount FROM splits WHERE tx_id=? ORDER BY category_id", (k3,)).fetchall()
    con.close()
    check(
        "save again replaces splits",
        [(x[0], x[1]) for x in sp_rows] == sorted([(lm, -10.0), (ko, -30.0)]),
        [tuple(x) for x in sp_rows],
    )
    r = post(f"/tx/{k3}/split", {"cat": [str(lm), str(ko)], "amt": ["-10", "-20"]})
    check(
        "wrong sum -> no redirect, error message",
        not isinstance(r, str) and r.status_code == 200 and "muss dem Betrag" in r.get_data(as_text=True),
        r,
    )
    con = m.db()
    sp_rows = con.execute("SELECT category_id, amount FROM splits WHERE tx_id=? ORDER BY category_id", (k3,)).fetchall()
    con.close()
    check(
        "wrong sum: DB unchanged",
        [(x[0], x[1]) for x in sp_rows] == sorted([(lm, -10.0), (ko, -30.0)]),
        [tuple(x) for x in sp_rows],
    )
    check("wrong sum: note unchanged", tx(k3)["note"].count("[aufgeteilt]") == 1)
    html = c.get(f"/tx/{k3}/split").get_data(as_text=True)
    check("split GET shows existing splits", "Lebensmittel" in html and "Konsum" in html)
    html = c.get("/transactions").get_data(as_text=True)
    check(
        "/transactions shows the 'aufgeteilt' link instead of a dropdown",
        re.search(r'href="/tx/%d/split\?back=/transactions" class="mute"[^>]*>\s*÷ aufgeteilt' % k3, html) is not None
        and 'class="tx-cat" data-tx-id="%d"' % k3 not in html,
    )
    # split on an entry with effective_amount: the sum must match the RAW amount
    r = post(f"/tx/{f1}/split", {"cat": [str(woh), str(ko)], "amt": ["-450", "-450"]})
    check(
        "split must match raw amount (-900), not effective (-450)",
        not isinstance(r, str) and r.status_code == 302,
        r,
    )
    r = post(f"/tx/{f2}/split", {"cat": [str(woh)], "amt": ["-450"]})
    check(
        "split with effective sum rejected",
        not isinstance(r, str) and r.status_code == 200 and "muss dem Betrag" in r.get_data(as_text=True),
        r,
    )
    r = post(f"/tx/{k1}/split", {"cat": [str(lm)], "amt": ["abc"]})
    check("split amt garbage no 500 (-> rejected)", not isinstance(r, str) and r.status_code == 200, r)
    r = post(f"/tx/{k1}/split", {"cat": ["abc"], "amt": ["-12"]})
    check("split cat garbage no 500", not isinstance(r, str), r)
    r = post(f"/tx/{k1}/split", {"cat": ["99999"], "amt": ["-12"]})
    check("split cat unknown no 500", not isinstance(r, str), r)
    r = post(f"/tx/{k1}/split", {})
    check("split empty no 500 (sum 0 != amount)", not isinstance(r, str) and r.status_code == 200, r)
    r = post(f"/tx/{k1}/split", {"cat": [str(lm), str(lm)], "amt": ["-6", "-6"]})
    check("split same category twice no 500", not isinstance(r, str), r)
    r = post(f"/tx/{k1}/split", {"cat": [str(lm)], "amt": ["-12"], "back": "http://evil.example/x"})
    print(f"       (Info: back=external URL -> Location {r.headers.get('Location') if not isinstance(r, str) else r})")
    r = post("/tx/99999/split", {"cat": [str(lm)], "amt": ["-12"]})
    check("split unknown tx -> 302", not isinstance(r, str) and r.status_code == 302, r)
    finish()
