"""Fresh installation: setup, optional sources, configurable fixed costs,
help page, dark mode."""

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_fresh():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    print("1) Fresh DB starts in setup")
    con = m.db()
    check("setup still open", not m.get_setting(con, "setup_done"), m.get_setting(con, "setup_done"))
    check("no fixed costs preset", m.fixed_cost_patterns(con) == [], m.fixed_cost_patterns(con))
    check("only bank active", m.enabled_sources(con) == {"bank"}, m.enabled_sources(con))
    check("no cutoff date", m.get_setting(con, "data_cutoff") == "", m.get_setting(con, "data_cutoff"))
    con.close()
    r = c.get("/")
    check(
        "dashboard redirects to /setup",
        r.status_code == 302 and "/setup" in r.headers["Location"],
        (r.status_code, r.headers.get("Location")),
    )
    check("/setup itself reachable", c.get("/setup").status_code == 200)
    check("/hilfe reachable before setup", c.get("/hilfe").status_code == 200)

    print("\n2) Setup without Splitwise/PayPal/card")
    r = c.post(
        "/setup", data={"src_bank": "1", "data_cutoff": "", "shared_name": "", "splitwise_name": "", "theme": "dark"}
    )
    check("redirects", r.status_code == 302, r.status_code)
    con = m.db()
    check("setup done", m.get_setting(con, "setup_done") == "1")
    check("theme saved", m.get_setting(con, "theme") == "dark")
    con.close()
    check("dashboard now reachable", c.get("/").status_code == 200)

    print("\n3) Disabled sources vanish from the menu")
    html = c.get("/").get_data(as_text=True)
    check("no Splitwise link", 'href="/splitwise"' not in html)
    check("no PayPal link", 'href="/paypal"' not in html)
    check("no card link", 'href="/karte"' not in html)
    check("settings linked", 'href="/einstellungen"' in html)
    check("help linked", 'href="/hilfe"' in html)
    imp = c.get("/import").get_data(as_text=True)
    check("import does not mention Splitwise", "Splitwise-Gruppen" not in imp)

    print("\n4) Enable sources later")
    c.post("/einstellungen", data={"action": "sources", "src_paypal": "1", "src_splitwise": "1"})
    html = c.get("/").get_data(as_text=True)
    check("PayPal link appears", 'href="/paypal"' in html)
    check("Splitwise link appears", 'href="/splitwise"' in html)
    check("card stays off", 'href="/karte"' not in html)
    con = m.db()
    check("bank always stays active", "bank" in m.enabled_sources(con), m.enabled_sources(con))
    con.close()

    print("\n5) Splitwise import without a stored name fails with a clear message")
    csv = (
        "Datum,Beschreibung,Kategorien,Kosten,Waehrung,Anna Muster,Bob\n2026-06-01,Pizza,Essen,20.00,EUR,10.00,-10.00\n"
    )
    try:
        m.import_splitwise(io.BytesIO(csv.encode("utf-8")), "wg")
        check("error is reported", False, "kein Fehler ausgeloest")
    except ValueError as e:
        check("error mentions the settings", "Einstellungen" in str(e), str(e))

    print("\n6) With a name the import works")
    con = m.db()
    m.set_setting(con, "splitwise_name", "Anna Muster")
    con.commit()
    con.close()
    new, skipped, ids, removed = m.import_splitwise(io.BytesIO(csv.encode("utf-8")), "wg")
    check("one row imported", new == 1, (new, skipped))
    con = m.db()
    row = con.execute("SELECT * FROM transactions WHERE source='splitwise'").fetchone()
    check("own share detected", abs(row["amount"] - 10.0) < 0.01, row["amount"])
    con.close()

    print("\n7) Fixed costs are freely configurable")
    con = m.db()
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-06-01','Hausverwaltung Mueller','miete',-900,'EUR','h1','active')""")
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-06-02','Stadtwerke','strom',-80,'EUR','h2','active')""")
    con.commit()
    con.close()
    # create via the UI: one shared at 50%, one at 100% (category only)
    c.post(
        "/einstellungen",
        data={
            "action": "fixed_add",
            "pattern": "Hausverwaltung",
            "category_id": str(woh),
            "share_percent": "50",
            "sw_keyword": "miete",
        },
    )
    c.post(
        "/einstellungen",
        data={
            "action": "fixed_add",
            "pattern": "Stadtwerke",
            "category_id": str(woh),
            "share_percent": "100",
            "sw_keyword": "",
        },
    )
    c.post(
        "/einstellungen",
        data={
            "action": "general",
            "shared_name": "Anna",
            "data_cutoff": "",
            "splitwise_name": "Anna Muster",
            "urlaub_excluded": "",
            "fixkosten_cats": ["Wohnung"],
            "fixkosten_privat_cats": ["Abos"],
        },
    )
    con = m.db()
    n = m.auto_split_wohnung_fixed(con)
    con.commit()
    r1 = con.execute("SELECT * FROM transactions WHERE raw_hash='h1'").fetchone()
    r2 = con.execute("SELECT * FROM transactions WHERE raw_hash='h2'").fetchone()
    check("shared booking halved", abs((r1["effective_amount"] or 0) + 450) < 0.01, r1["effective_amount"])
    check("category set", r1["category_id"] == woh, r1["category_id"])
    check("note mentions the name", "Anna" in (r1["note"] or ""), r1["note"])
    check("100% entry keeps its amount untouched", r2["effective_amount"] is None, r2["effective_amount"])
    check("100% entry still gets the category", r2["category_id"] == woh, r2["category_id"])
    check(
        "Splitwise keyword lands in the mapping",
        m.sw_fixed_to_partner(con).get("miete") == "Hausverwaltung",
        m.sw_fixed_to_partner(con),
    )
    check("monthly smoothing uses the new patterns", isinstance(m.wohnung_fixed_display_months(con), dict))
    con.close()

    print("\n8) Edit and delete fixed costs")
    con = m.db()
    fid = con.execute("SELECT id FROM fixed_costs WHERE pattern='Stadtwerke'").fetchone()["id"]
    con.close()
    c.post(
        "/einstellungen",
        data={
            "action": "fixed_save",
            "fixed_id": str(fid),
            "pattern": "Stadtwerke Energie",
            "category_id": str(woh),
            "share_percent": "70",
            "sw_keyword": "strom",
        },
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE id=?", (fid,)).fetchone()
    check("pattern changed", row["pattern"] == "Stadtwerke Energie", row["pattern"])
    check("share changed", row["share_percent"] == 70, row["share_percent"])
    con.close()
    c.post("/einstellungen", data={"action": "fixed_delete", "fixed_id": str(fid)})
    con = m.db()
    check("deleted", con.execute("SELECT COUNT(*) FROM fixed_costs").fetchone()[0] == 1)
    con.close()

    print("\n9) Everything keeps working with no fixed costs at all")
    con = m.db()
    con.execute("DELETE FROM fixed_costs")
    con.commit()
    con.close()
    for p in (
        "/",
        "/transactions",
        "/jahr",
        "/fixkosten",
        "/rules",
        "/partner",
        "/transactions?month=2026-06",
        "/einstellungen",
        "/hilfe",
    ):
        check(f"GET {p}", c.get(p).status_code == 200, c.get(p).status_code)
    con = m.db()
    check("no patterns, no crashes", m.fixed_cost_patterns(con) == [])
    check("smoothing empty", m.wohnung_fixed_display_months(con) == {})
    check("recurring detection runs", isinstance(m.detect_recurring(con), list))
    con.close()

    print("\n10) Dark mode")
    html = c.get("/").get_data(as_text=True)
    check("theme is in the HTML", "data-theme=" in html, html[:200])
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    check("CSS knows the dark mode", '[data-theme="dark"]' in css)
    check("CSS also follows the system setting", "prefers-color-scheme: dark" in css)
    check("toggle present", 'id="theme-toggle"' in html)
    r = c.post("/einstellungen", data={"action": "theme", "theme": "light"})
    con = m.db()
    check("theme saved per click", m.get_setting(con, "theme") == "light")
    con.close()
    finish()
