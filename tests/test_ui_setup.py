"""1) Fresh DB / setup / sources, 10) help, 11) dark mode, 12) export, 13) all routes."""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_ui_setup():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    _h = [0]

    def add_tx(con, source, d, partner, desc, amount, status="active", cat=None, extra="{}", eff=None):
        _h[0] += 1
        cid = None
        if cat:
            cid = con.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone()["id"]
        cur = con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,"
            "raw_hash,status,category_id,extra,effective_amount) VALUES(?,?,?,?,?,'EUR',?,?,?,?,?)",
            (source, d, partner, desc, amount, f"h{_h[0]}", status, cid, extra, eff),
        )
        return cur.lastrowid

    print("1) Fresh DB: everything except setup/settings/help/static redirects to /setup")
    ROUTES_GET = [
        "/",
        "/paypal",
        "/karte",
        "/transactions",
        "/import",
        "/review",
        "/splitwise",
        "/partner",
        "/fixkosten",
        "/jahr",
        "/categories",
        "/rules",
        "/export.csv",
        "/tx/1/split",
        "/splitwise/link/1",
    ]
    for p in ROUTES_GET:
        r = c.get(p)
        check(
            f"fresh GET {p} -> 302 /setup",
            r.status_code == 302 and r.headers.get("Location", "").endswith("/setup"),
            (r.status_code, r.headers.get("Location")),
        )
    for p in ["/setup", "/einstellungen", "/hilfe", "/static/style.css"]:
        r = c.get(p)
        check(f"fresh GET {p} -> 200", r.status_code == 200, r.status_code)
    # POST routes are redirected as well
    for p in ["/tx/1/update", "/rules/1/apply", "/splitwise/x/urlaub", "/splitwise/dismiss/1"]:
        r = c.post(p, data={})
        check(
            f"fresh POST {p} -> 302 /setup",
            r.status_code == 302 and "/setup" in r.headers.get("Location", ""),
            (r.status_code, r.headers.get("Location")),
        )

    print("\n1b) Setup with all sources + income pattern")
    r = c.post(
        "/setup",
        data={
            "src_bank": "1",
            "src_card": "1",
            "src_paypal": "1",
            "src_splitwise": "1",
            "data_cutoff": "",
            "shared_name": "Anna",
            "splitwise_name": "Ich Selbst",
            "theme": "auto",
            "income_pattern": "Firma AG",
            "income_category": "Gehalt",
        },
    )
    check(
        "setup POST -> 302 /import",
        r.status_code == 302 and "/import" in r.headers["Location"],
        (r.status_code, r.headers.get("Location")),
    )
    con = m.db()
    check(
        "all sources active", m.enabled_sources(con) == {"bank", "card", "paypal", "splitwise"}, m.enabled_sources(con)
    )
    check("income rule", m.income_rule_patterns(con) == ["Firma AG"], m.income_rule_patterns(con))
    con.close()
    r = c.get("/")
    check("dashboard 200", r.status_code == 200, r.status_code)
    html = r.get_data(as_text=True)
    for link in ("/paypal", "/karte", "/splitwise"):
        check(f"nav shows {link}", f'href="{link}"' in html)
    check("/setup still 200 after setup (callable again)", c.get("/setup").status_code == 200)

    print("\n1c) Disable sources -> links gone, routes still respond")
    r = c.post("/einstellungen", data={"action": "sources", "src_bank": "1"})
    check("sources POST 302", r.status_code == 302, r.status_code)
    con = m.db()
    check("only bank", m.enabled_sources(con) == {"bank"}, m.enabled_sources(con))
    con.close()
    html = c.get("/").get_data(as_text=True)
    for link in ("/paypal", "/karte", "/splitwise"):
        check(f"nav without {link}", f'href="{link}"' not in html)
    for p in ("/paypal", "/karte", "/splitwise"):
        r = c.get(p)
        check(f"disabled source GET {p} responds (no 500)", r.status_code in (200, 302), r.status_code)
        print(f"       (Info: {p} with source disabled -> {r.status_code}, still directly reachable)")
    imp = c.get("/import").get_data(as_text=True)
    check("import does not mention Splitwise when off", "Splitwise-Gruppen" not in imp)
    # all on again
    c.post(
        "/einstellungen",
        data={"action": "sources", "src_bank": "1", "src_card": "1", "src_paypal": "1", "src_splitwise": "1"},
    )

    print("\n10) Help")
    h = c.get("/hilfe").get_data(as_text=True)
    for a in ("start", "monat", "seiten", "exporte", "auto", "einstellungen", "probleme"):
        check(f"help anchor #{a}", f'id="{a}"' in h)
    check("help (all sources) mentions PayPal export", "<td>PayPal</td>" in h)
    check("help (all sources) mentions card export", "<td>Kreditkarte</td>" in h)
    check("help (all sources) mentions Splitwise export", "<td>Splitwise</td>" in h)
    c.post("/einstellungen", data={"action": "sources", "src_bank": "1"})
    h2 = c.get("/hilfe").get_data(as_text=True)
    check("help (bank only) without PayPal export", "<td>PayPal</td>" not in h2)
    check("help (bank only) without card export", "<td>Kreditkarte</td>" not in h2)
    check("help (bank only) without Splitwise export", "<td>Splitwise</td>" not in h2)
    check("help (bank only) without Splitwise balance row", "Splitwise-Saldo stimmt nicht" not in h2)
    c.post(
        "/einstellungen",
        data={"action": "sources", "src_bank": "1", "src_card": "1", "src_paypal": "1", "src_splitwise": "1"},
    )
    imp = c.get("/import").get_data(as_text=True)
    check("import without the old how-it-works block", "So funktioniert" not in imp)
    check("import links /hilfe#auto", 'href="/hilfe#auto"' in imp)

    print("\n11) Dark Mode")
    PAGES = [
        "/",
        "/transactions",
        "/jahr",
        "/fixkosten",
        "/partner",
        "/categories",
        "/rules",
        "/import",
        "/review",
        "/splitwise",
        "/paypal",
        "/karte",
        "/einstellungen",
        "/hilfe",
        "/setup",
    ]
    r = c.post("/einstellungen", data={"action": "theme", "theme": "dark"})
    check("theme dark POST 302", r.status_code == 302, r.status_code)
    for p in PAGES:
        html = c.get(p).get_data(as_text=True)
        check(f"dark: {p} data-theme=dark", 'data-theme="dark"' in html[:300], html[:120])
    c.post("/einstellungen", data={"action": "theme", "theme": "auto"})
    for p in PAGES:
        html = c.get(p).get_data(as_text=True)
        check(f"auto: {p} data-theme=auto", 'data-theme="auto"' in html[:300], html[:120])
    c.post("/einstellungen", data={"action": "theme", "theme": "xyz"})
    con = m.db()
    check("theme xyz -> auto", m.get_setting(con, "theme") == "auto", m.get_setting(con, "theme"))
    con.close()
    c.post("/einstellungen", data={"action": "theme"})
    con = m.db()
    check("theme missing -> auto", m.get_setting(con, "theme") == "auto", m.get_setting(con, "theme"))
    con.close()

    print("\n12) Export CSV + 13) all routes with a populated DB")
    con = m.db()
    t1 = add_tx(con, "bank", "2026-06-03", "BILLA DANKT 1", "Einkauf", -23.50, cat="Lebensmittel")
    t2 = add_tx(con, "bank", "2026-06-28", "Firma AG", "Gehalt", 2500.00, cat="Gehalt")
    t3 = add_tx(con, "paypal", "2026-06-10", "Netflix", "Abo", -13.99, cat="Abos")
    t4 = add_tx(con, "card", "2026-06-12", "AMAZON", "Bestellung", -40.00, cat="Konsum")
    t5 = add_tx(
        con, "bank", "2026-06-15", "PayPal Europe", "PayPal", -13.99, status="excluded", cat="PayPal-Verrechnung"
    )
    t6 = add_tx(con, "bank", "2026-07-02", "Unbekannt GmbH", "irgendwas", -9.90)
    t7 = add_tx(
        con,
        "splitwise",
        "2026-06-20",
        "wg",
        "Pizza",
        -10.0,
        status="info",
        extra=json.dumps({"cost": 20.0, "bal": -10.0, "sw_category": "Essen", "members": ["Bob"]}),
    )
    t8 = add_tx(
        con,
        "splitwise",
        "2026-06-21",
        "wg",
        "Bob zahlte Ich Selbst",
        5.0,
        status="info",
        extra=json.dumps({"is_payment": True, "cost": 5.0, "bal": 5.0}),
    )
    t9 = add_tx(con, "bank", "2026-07-05", "Hausverwaltung", "Miete Juli", -900.00, cat="Wohnung", eff=-450.0)
    con.commit()
    con.close()
    r = c.get("/export.csv")
    check("export 200", r.status_code == 200, r.status_code)
    check("export mimetype text/csv", r.mimetype == "text/csv", r.mimetype)
    body = r.get_data(as_text=True)
    lines = [l for l in body.splitlines() if l.strip()]
    check(
        "export header",
        lines[0].lstrip("\ufeff").startswith("Datum;Quelle;Partner;Beschreibung;Betrag;Effektiv;Kategorie;Status"),
        lines[0],
    )
    con = m.db()
    n_all = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    n_active = con.execute("SELECT COUNT(*) FROM transactions WHERE status='active'").fetchone()[0]
    con.close()
    # The export is deliberately complete (all statuses, with a status column) - a
    # backup, not an excerpt.
    check(
        f"export one row per transaction, all statuses ({n_all})",
        len(lines) - 1 == n_all,
        f"{len(lines) - 1} Datenzeilen, {n_all} gesamt",
    )
    check("export has a status column", "Status" in lines[0])
    check("export Effektiv = own share (450,00)", ";-900,00;-450,00;" in body, "")

    ROUTES = [
        "/",
        "/?month=2026-06",
        "/?range=2026",
        "/?from=2026-06&to=2026-07",
        "/?range=all",
        "/paypal",
        "/karte",
        "/transactions",
        "/transactions?month=2026-06",
        "/import",
        "/review",
        "/review?tx=%d" % t1,
        "/splitwise",
        "/splitwise/link/%d" % t7,
        "/partner",
        "/partner?p=BILLA+DANKT+1",
        "/fixkosten",
        "/tx/%d/split" % t1,
        "/jahr",
        "/jahr?year=2026",
        "/categories",
        "/rules",
        "/setup",
        "/hilfe",
        "/einstellungen",
        "/export.csv",
    ]
    for p in ROUTES:
        r = c.get(p)
        check(f"GET {p} -> 200", r.status_code == 200, r.status_code)
    r = c.get("/tx/99999/split")
    check("GET /tx/99999/split -> 302 (unknown)", r.status_code == 302, r.status_code)
    r = c.get("/splitwise/link/99999")
    check("GET /splitwise/link/99999 no 500", r.status_code in (200, 302, 404), r.status_code)
    finish()
