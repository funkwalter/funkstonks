"""The income source is configurable, not hard-wired."""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_income():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    print("1) No employer in the factory state")
    check(
        "no sample company in the default rules",
        not any("Verfassungsgericht" in p for p, _ in m.DEFAULT_RULES),
        [p for p, _ in m.DEFAULT_RULES if "Verfassungs" in p],
    )
    con = m.db()
    check("no income rule created", m.income_rule_patterns(con) == [], m.income_rule_patterns(con))
    check("default category is Gehalt", m.income_category_name(con) == "Gehalt")
    con.close()

    print("\n2) Setup asks for it")
    html = c.get("/setup").get_data(as_text=True)
    check("field in the form", 'name="income_pattern"' in html)
    check("category selectable", 'name="income_category"' in html and ">Gehalt<" in html)
    c.post(
        "/setup", data={"src_bank": "1", "income_pattern": "Muster AG", "income_category": "Gehalt", "theme": "auto"}
    )
    con = m.db()
    check("rule created", m.income_rule_patterns(con) == ["Muster AG"], m.income_rule_patterns(con))
    check(
        "points to the income category",
        con.execute(
            "SELECT c.name FROM rules r JOIN categories c ON c.id=r.category_id WHERE r.pattern='Muster AG'"
        ).fetchone()["name"]
        == "Gehalt",
    )
    con.close()
    check("shows up under rules", "Muster AG" in c.get("/rules").get_data(as_text=True))

    print("\n3) The rule applies on import")
    con = m.db()
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-06-28','Muster AG','Gehalt Juni',2500,'EUR','g1','active')""")
    con.execute("""INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status) VALUES('bank','2026-07-28','Muster AG','Gehalt Juli',2500,'EUR','g2','active')""")
    con.commit()
    m.apply_rules(con)
    con.commit()
    cat = con.execute(
        "SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.raw_hash='g1'"
    ).fetchone()
    check("salary receipt categorized", cat and cat["name"] == "Gehalt", cat and cat["name"])
    con.close()
    html = c.get("/jahr?year=2026").get_data(as_text=True)
    check("KPI carries the category name", "Ø Gehalt / Monat" in html)

    print("\n4) Renamed income category")
    con = m.db()
    con.execute("UPDATE categories SET name='Honorar' WHERE name='Gehalt'")
    m.set_setting(con, "income_category", "Honorar")
    con.commit()
    check("helper follows the setting", m.income_category_name(con) == "Honorar")
    check("rule still found", m.income_rule_patterns(con) == ["Muster AG"], m.income_rule_patterns(con))
    con.close()
    html = c.get("/jahr?year=2026").get_data(as_text=True)
    check("KPI is now called Honorar", "Ø Honorar / Monat" in html)
    kpi = re.search(r"Ø Honorar / Monat</span><b>([\d.]+)", html)
    check(
        "KPI keeps calculating (2500 = 5000/2 months)",
        kpi and abs(float(kpi.group(1)) - 2500.0) < 0.01,
        kpi and kpi.group(1),
    )

    print("\n5) Change and delete via the settings")
    form = {
        "action": "general",
        "income_pattern": "Muster AG, Nebenjob GmbH",
        "income_category": "Honorar",
        "shared_name": "",
        "data_cutoff": "",
        "splitwise_name": "",
        "urlaub_excluded": "",
    }
    c.post("/einstellungen", data=form)
    con = m.db()
    check(
        "second source created",
        m.income_rule_patterns(con) == ["Muster AG", "Nebenjob GmbH"],
        m.income_rule_patterns(con),
    )
    con.close()
    c.post("/einstellungen", data={**form, "income_pattern": "Nebenjob GmbH"})
    con = m.db()
    check("removed source deleted", m.income_rule_patterns(con) == ["Nebenjob GmbH"], m.income_rule_patterns(con))
    n_other = con.execute(
        "SELECT COUNT(*) FROM rules r JOIN categories c ON c.id=r.category_id WHERE c.kind!='income'"
    ).fetchone()[0]
    con.close()
    check("other rules untouched", n_other == len(m.DEFAULT_RULES), n_other)
    c.post("/einstellungen", data={**form, "income_pattern": ""})
    con = m.db()
    check("empty field deletes all income rules", m.income_rule_patterns(con) == [], m.income_rule_patterns(con))
    con.close()

    print("\n6) Everything keeps working without an income rule")
    for p in ("/", "/jahr", "/rules", "/einstellungen", "/hilfe", "/transactions"):
        check(f"GET {p}", c.get(p).status_code == 200, c.get(p).status_code)
    html = c.get("/einstellungen").get_data(as_text=True)
    check("field on the settings page", 'name="income_pattern"' in html)
    finish()
