"""Recognition from the imported data: the own Splitwise column is guessed
from matching bank bookings, a PayPal export switches the PayPal source on,
a two-member group with rent/electricity items is proposed as the fixed-cost
group (with the partner), and bank payees whose bookings equal those items
are proposed as shared fixed costs - applied or dismissed on the settings
page. Made-up names and amounts; numbers are checked."""

import os
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _import_common import check, count, finish, fresh_db, george_bank, m, one, paypal_akt, splitwise_csv, upload  # noqa: E402


def day(months_ago, d=1):
    t = date.today().replace(day=1) - timedelta(days=1)
    for _ in range(months_ago):
        t = t.replace(day=1) - timedelta(days=1)
    return t.replace(day=min(d, 28)).strftime("%d.%m.%Y")


def sw_rows(me_first):
    """Splitwise group 'wohnung': rent and electricity paid by me every month,
    split 50/50, plus takeaway orders of varying amounts (no fixed cost)."""
    rows = []
    orders = (("25.94", "12.97"), ("18.03", "9.02"), ("31.50", "15.75"))
    for k in range(3):
        items = (("Miete", "985.28", "492.64"), ("Strom Abschlag", "170.40", "85.20"), ("Bowls", *orders[k]))
        for desc, cost, half in items:
            mine, other = (half, "-" + half) if me_first else ("-" + half, half)
            rows.append(f"{day(k, 15 if desc == 'Bowls' else 3)},{desc},Haushalt,{cost},EUR,{mine},{other}")
    return rows


def test_suggest():
    tmp, c = fresh_db(sources='["bank"]', sw_name="")

    print("\n1) Bank bookings first (George), then a PayPal export switches PayPal on")
    bank = [(day(k, 2), "Hausverwaltung Muster", "Miete", "-985,28") for k in range(3)]
    bank += [(day(k, 4), "Energie Muster", "Strom", "-170,40") for k in range(3)]
    bank += [(day(0, 10), "BILLA DANKT 1", "Einkauf", "-12,50")]
    # shared takeaway orders of varying amounts: equal Splitwise items, but no fixed cost
    bank += [(day(k, 15), "Lieferdienst Muster", "Bestellung", a) for k, a in enumerate(("-25,94", "-18,03", "-31,50"))]
    upload(c, (george_bank(bank), "konto.csv"))
    check("10 bank rows", count("source='bank'") == 10, count("source='bank'"))
    html = upload(c, (paypal_akt([(day(0, 12), "Shop Muster", "Zahlung", "EUR", "-9,99", "PP1")]), "paypal.csv"))
    con = m.db()
    srcs = m.enabled_sources(con)
    con.close()
    check("PayPal switched on by the import", "paypal" in srcs, srcs)
    check("result says so", 'data-enabled="PayPal"' in html)

    print("\n2) Splitwise without a name: the own column is guessed")
    head = "Datum,Beschreibung,Kategorien,Kosten,Währung,Bob Beispiel,Ich Muster"
    csv = splitwise_csv(head, sw_rows(me_first=False))
    html = upload(c, (csv, "wohnung_2026-01-01.csv"))
    con = m.db()
    name = m.get_setting(con, "splitwise_name")
    srcs = m.enabled_sources(con)
    con.close()
    check("name guessed from the matching bank amounts", name == "Ich Muster", name)
    check("result reports the name", 'data-sw-name="Ich Muster"' in html)
    check("Splitwise switched on", "splitwise" in srcs, srcs)
    check("9 Splitwise rows imported", count("source='splitwise'") == 9, count("source='splitwise'"))

    print("\n3) Sharing suggestion: group wohnung with Bob")
    con = m.db()
    s = m.suggest_sharing(con)
    check("group and partner recognised", s and s["group"] == "wohnung" and s["partner"] == "Bob Beispiel", s)
    check("six fixed-cost items counted", s and s["n"] == 6, s)
    fx = m.suggest_fixed_costs(con)
    con.close()
    pats = {x["pattern"]: x for x in fx}
    check(
        "two fixed-cost suggestions, takeaway orders not among them",
        set(pats) == {"Hausverwaltung Muster", "Energie Muster"},
        pats.keys(),
    )
    check(
        "rent: 50 %, keyword miete, 3 months",
        pats.get("Hausverwaltung Muster", {}).get("share_percent") == 50
        and pats["Hausverwaltung Muster"]["sw_keyword"] == "miete"
        and pats["Hausverwaltung Muster"]["n"] == 3,
        pats.get("Hausverwaltung Muster"),
    )
    check(
        "electricity: keyword strom",
        pats.get("Energie Muster", {}).get("sw_keyword") == "strom",
        pats.get("Energie Muster"),
    )
    check("import result counted the suggestions", 'data-suggest="2"' in html and 'data-sharing="wohnung"' in html)
    html = c.get("/einstellungen").get_data(as_text=True)
    check("settings show the sharing suggestion", 'id="sharing-suggest" data-group="wohnung"' in html)
    check(
        "settings show both fixed-cost suggestions",
        html.count('class="fc-suggest"') == 2,
        html.count('class="fc-suggest"'),
    )

    print("\n4) Apply and dismiss")
    c.post("/einstellungen", data={"action": "sharing_apply", "group": "wohnung", "partner": "Bob Beispiel"})
    con = m.db()
    check("fixed_cost_group set", m.get_setting(con, "fixed_cost_group") == "wohnung")
    check("shared_name set", m.get_setting(con, "shared_name") == "Bob Beispiel")
    check("no sharing suggestion once set", m.suggest_sharing(con) is None)
    con.close()
    c.post(
        "/einstellungen",
        data={
            "action": "fixed_add",
            "pattern": "Hausverwaltung Muster",
            "category_id": "",
            "share_percent": "50",
            "sw_keyword": "miete",
        },
    )
    c.post("/einstellungen", data={"action": "fixed_dismiss", "pattern": "Energie Muster"})
    con = m.db()
    fx = m.suggest_fixed_costs(con)
    rows = m.fixed_cost_rows(con)
    con.close()
    check("applied one covers the payee, dismissed one is gone", fx == [], fx)
    check(
        "fixed_costs row created",
        any(r["pattern"] == "Hausverwaltung Muster" and r["share_percent"] == 50 for r in rows),
    )
    html = c.get("/einstellungen").get_data(as_text=True)
    check("no suggestions left on the page", 'class="fc-suggest"' not in html and 'id="sharing-suggest"' not in html)

    print("\n5) Next import halves the rent via the new fixed cost")
    upload(c, (george_bank([(day(0, 25), "Hausverwaltung Muster", "Miete", "-985,28")]), "konto2.csv"))
    t = one("SELECT effective_amount FROM transactions WHERE partner='Hausverwaltung Muster' ORDER BY id DESC LIMIT 1")
    check(
        "effective amount halved",
        t and t["effective_amount"] is not None and abs(t["effective_amount"] + 492.64) < 0.005,
        t and dict(t),
    )
    finish()
