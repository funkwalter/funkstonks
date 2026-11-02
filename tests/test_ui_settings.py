"""2) /einstellungen: every action with valid and garbage input."""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_ui_settings():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    c.post("/setup", data={"src_bank": "1", "theme": "auto", "income_pattern": "", "income_category": "Gehalt"})
    con = m.db()
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    con.close()

    from _ui_common import post as _post

    def post(data):
        r = _post(c, "/einstellungen", data)
        return r if isinstance(r, str) else r.status_code

    def settings_ok(name, data):
        code = post(data)
        check(name, code in (200, 302), code)
        return code

    print("2a) general: valid")
    settings_ok(
        "general valid",
        {
            "action": "general",
            "data_cutoff": "2026-05-01",
            "shared_name": "Anna",
            "splitwise_name": "Ich",
            "urlaub_excluded": "wg, freunde",
            "fixkosten_cats": ["Wohnung", "Abos"],
            "fixkosten_privat_cats": ["Abos"],
            "income_pattern": "Firma AG",
            "income_category": "Gehalt",
        },
    )
    con = m.db()
    check("cutoff stored", m.get_setting(con, "data_cutoff") == "2026-05-01")
    check(
        "urlaub_excluded as JSON list",
        m.get_setting_list(con, "urlaub_excluded_groups") == ["wg", "freunde"],
        m.get_setting_list(con, "urlaub_excluded_groups"),
    )
    check("fixkosten_cats", m.get_setting_list(con, "fixkosten_cats") == ["Wohnung", "Abos"])
    con.close()
    print("2b) general: garbage")
    settings_ok("general all empty", {"action": "general"})
    settings_ok(
        "general empty strings",
        {
            "action": "general",
            "data_cutoff": "",
            "shared_name": "",
            "splitwise_name": "",
            "urlaub_excluded": "",
            "income_pattern": "",
        },
    )
    settings_ok("general cutoff garbage", {"action": "general", "data_cutoff": "keindatum"})
    con = m.db()
    print("       (Info: data_cutoff after garbage input = %r)" % m.get_setting(con, "data_cutoff"))
    check("garbage cutoff does not break the dashboard", c.get("/").status_code == 200)
    check("garbage cutoff does not break tracking_start", isinstance(m.tracking_start(con), str), "")
    con.close()
    settings_ok(
        "general income_category unknown",
        {"action": "general", "income_category": "GibtsNicht", "income_pattern": "X AG"},
    )
    con = m.db()
    ic = m.get_setting(con, "income_category")
    print(f"       (Info: income_category after unknown value = {ic!r}; patterns = {m.income_rule_patterns(con)})")
    check("unknown income category: page still loads", c.get("/einstellungen").status_code == 200)
    check("unknown income category: /jahr still loads", c.get("/jahr").status_code == 200)
    # reset
    m.set_setting(con, "income_category", "Gehalt")
    con.commit()
    con.close()
    settings_ok("general fixkosten_cats unknown category", {"action": "general", "fixkosten_cats": ["Nix"]})
    check("dashboard with unknown fixkosten_cat", c.get("/").status_code == 200)
    check("fixkosten with unknown fixkosten_cat", c.get("/fixkosten").status_code == 200)
    settings_ok("general urlaub_excluded only commas", {"action": "general", "urlaub_excluded": ", , ,"})
    con = m.db()
    check(
        "only commas -> empty list",
        m.get_setting_list(con, "urlaub_excluded_groups") == [],
        m.get_setting_list(con, "urlaub_excluded_groups"),
    )
    con.close()

    print("2c) income: dedupe / empty / dropdown")
    post({"action": "general", "income_pattern": "A, B ,A", "income_category": "Gehalt"})
    con = m.db()
    pats = m.income_rule_patterns(con)
    con.close()
    check("income 'A, B ,A' deduplicated to [A, B]", pats == ["A", "B"], pats)
    post({"action": "general", "income_pattern": "a, A", "income_category": "Gehalt"})
    con = m.db()
    pats = m.income_rule_patterns(con)
    con.close()
    print(f"       (Info: 'a, A' -> {pats}; kept apart case-sensitively, but apply_rules matches case-insensitively)")
    post({"action": "general", "income_pattern": "", "income_category": "Gehalt"})
    con = m.db()
    pats = m.income_rule_patterns(con)
    n_income_rules = con.execute(
        "SELECT COUNT(*) FROM rules r JOIN categories c ON c.id=r.category_id WHERE c.kind='income'"
    ).fetchone()[0]
    con.close()
    check("income empty -> all income rules gone", pats == [] and n_income_rules == 0, (pats, n_income_rules))
    html = c.get("/einstellungen").get_data(as_text=True)
    sel = re.search(r'<select name="income_category">(.*?)</select>', html, re.S).group(1)
    opts = re.findall(r'value="([^"]+)"', sel)
    con = m.db()
    inc = [
        r["name"]
        for r in con.execute("SELECT name FROM categories WHERE kind='income' AND parent_id IS NULL ORDER BY name")
    ]
    non_inc = [r["name"] for r in con.execute("SELECT name FROM categories WHERE kind!='income'")]
    con.close()
    check("income dropdown = exactly the income categories", opts == inc, (opts, inc))
    check("income dropdown without expense/transfer categories", not any(o in non_inc for o in opts), opts)
    # an income subcategory does NOT appear in the dropdown (only parent_id IS NULL)
    con = m.db()
    gid = con.execute("SELECT id FROM categories WHERE name='Gehalt'").fetchone()["id"]
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Bonus','#111111','income',?)", (gid,))
    con.commit()
    con.close()
    html = c.get("/einstellungen").get_data(as_text=True)
    sel = re.search(r'<select name="income_category">(.*?)</select>', html, re.S).group(1)
    check("income subcategory not in dropdown", "Bonus" not in sel)
    check("parent category with subcategory still in dropdown", ">Gehalt<" in sel)
    # income pattern on a category with children -> rule points to the parent (not in the normal dropdown)
    post({"action": "general", "income_pattern": "Firma AG", "income_category": "Gehalt"})
    con = m.db()
    pats = m.income_rule_patterns(con)
    con.close()
    check("income rule created on parent category with children", pats == ["Firma AG"], pats)
    con = m.db()
    con.execute("DELETE FROM categories WHERE name='Bonus'")
    con.commit()
    con.close()

    print("2d) sources")
    settings_ok("sources without fields", {"action": "sources"})
    con = m.db()
    check("sources without fields -> bank stays", m.enabled_sources(con) == {"bank"}, m.enabled_sources(con))
    con.close()
    settings_ok("sources unknown source", {"action": "sources", "src_foo": "1", "src_bank": "1"})
    con = m.db()
    check("unknown source ignored", m.enabled_sources(con) == {"bank"}, m.enabled_sources(con))
    con.close()
    settings_ok("sources bank unchecked", {"action": "sources", "src_paypal": "1"})
    con = m.db()
    check("bank enforced", m.enabled_sources(con) == {"bank", "paypal"}, m.enabled_sources(con))
    con.close()

    print("2e) theme")
    for t in ("dark", "light", "auto", "xyz", "", None):
        d = {"action": "theme"}
        if t is not None:
            d["theme"] = t
        settings_ok(f"theme={t!r}", d)
        con = m.db()
        v = m.get_setting(con, "theme")
        con.close()
        check(f"theme={t!r} -> stored {v}", v == (t if t in ("dark", "light", "auto") else "auto"), v)

    print("2f) fixed_add valid + garbage")
    settings_ok(
        "fixed_add valid",
        {
            "action": "fixed_add",
            "pattern": "Hausverwaltung",
            "category_id": str(woh),
            "share_percent": "50",
            "sw_keyword": "miete",
        },
    )
    con = m.db()
    n0 = con.execute("SELECT COUNT(*) FROM fixed_costs").fetchone()[0]
    con.close()
    check("fixed_add created", n0 == 1, n0)
    settings_ok("fixed_add without pattern", {"action": "fixed_add", "pattern": "", "category_id": str(woh)})
    settings_ok("fixed_add whitespace-only pattern", {"action": "fixed_add", "pattern": "   "})
    con = m.db()
    n1 = con.execute("SELECT COUNT(*) FROM fixed_costs").fetchone()[0]
    con.close()
    check("empty pattern not created", n1 == 1, n1)
    settings_ok("fixed_add without category_id", {"action": "fixed_add", "pattern": "OhneKat", "share_percent": "100"})
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='OhneKat'").fetchone()
    con.close()
    check("without category_id -> NULL", row is not None and row["category_id"] is None, row and dict(row))
    code = settings_ok(
        "fixed_add share_percent non-numeric 'abc' (no 500)",
        {"action": "fixed_add", "pattern": "Abc", "category_id": str(woh), "share_percent": "abc"},
    )
    code = settings_ok(
        "fixed_add share_percent '50.5' (no 500)",
        {"action": "fixed_add", "pattern": "Halb", "category_id": str(woh), "share_percent": "50.5"},
    )
    settings_ok(
        "fixed_add share_percent empty",
        {"action": "fixed_add", "pattern": "Leer", "category_id": str(woh), "share_percent": ""},
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='Leer'").fetchone()
    con.close()
    check("share_percent empty -> 100", row and row["share_percent"] == 100, row and dict(row))
    settings_ok(
        "fixed_add share_percent negative",
        {"action": "fixed_add", "pattern": "Neg", "category_id": str(woh), "share_percent": "-20"},
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='Neg'").fetchone()
    con.close()
    check(
        "share_percent negative rejected or clamped (>=0)",
        row is None or 0 <= row["share_percent"] <= 100,
        row and dict(row),
    )
    settings_ok(
        "fixed_add share_percent 150",
        {"action": "fixed_add", "pattern": "Ueber", "category_id": str(woh), "share_percent": "150"},
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='Ueber'").fetchone()
    con.close()
    check(
        "share_percent >100 rejected or clamped (<=100)",
        row is None or 0 <= row["share_percent"] <= 100,
        row and dict(row),
    )
    settings_ok(
        "fixed_add share_percent 0",
        {"action": "fixed_add", "pattern": "Null", "category_id": str(woh), "share_percent": "0"},
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='Null'").fetchone()
    con.close()
    print(f"       (Info: share_percent=0 -> {row and dict(row)})")
    settings_ok("fixed_add category_id garbage 'x'", {"action": "fixed_add", "pattern": "KatMuell", "category_id": "x"})
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='KatMuell'").fetchone()
    con.close()
    print(f"       (Info: category_id='x' -> {row and dict(row)})")
    settings_ok("fixed_add category_id 99999", {"action": "fixed_add", "pattern": "KatUnbek", "category_id": "99999"})
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE pattern='KatUnbek'").fetchone()
    con.close()
    print(f"       (Info: category_id=99999 -> {row and dict(row)})")
    # page and automation after garbage entries
    check("/einstellungen loads after garbage entries", c.get("/einstellungen").status_code == 200)
    check("/ loads after garbage entries", c.get("/").status_code == 200)
    check("/fixkosten loads after garbage entries", c.get("/fixkosten").status_code == 200)
    check("/rules loads after garbage entries", c.get("/rules").status_code == 200)
    con = m.db()
    try:
        m.auto_split_wohnung_fixed(con)
        con.commit()
        check("auto_split_wohnung_fixed runs with garbage entries", True)
    except Exception as e:
        check("auto_split_wohnung_fixed runs with garbage entries", False, repr(e))
    try:
        m.detect_recurring(con)
        check("detect_recurring runs with garbage entries", True)
    except Exception as e:
        check("detect_recurring runs with garbage entries", False, repr(e))
    con.close()

    print("2g) fixed_save valid + garbage")
    con = m.db()
    fid = con.execute("SELECT id FROM fixed_costs WHERE pattern='Hausverwaltung'").fetchone()["id"]
    con.close()
    settings_ok(
        "fixed_save valid",
        {
            "action": "fixed_save",
            "fixed_id": str(fid),
            "pattern": "Hausverwaltung Neu",
            "category_id": str(woh),
            "share_percent": "60",
            "sw_keyword": "miete",
        },
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE id=?", (fid,)).fetchone()
    con.close()
    check("fixed_save applied", row["pattern"] == "Hausverwaltung Neu" and row["share_percent"] == 60, dict(row))
    settings_ok(
        "fixed_save empty pattern (no 500)",
        {"action": "fixed_save", "fixed_id": str(fid), "pattern": "", "category_id": str(woh), "share_percent": "60"},
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE id=?", (fid,)).fetchone()
    con.close()
    check(
        "fixed_save empty pattern NOT applied (an empty pattern would match EVERYTHING)",
        row["pattern"] != "",
        dict(row),
    )
    if row["pattern"] == "":
        con = m.db()
        like_sql, args = m.fixed_cost_like_sql(m.fixed_cost_patterns(con))
        print(f"       (Info: fixed_cost_patterns={m.fixed_cost_patterns(con)}, like_sql={like_sql!r})")
        con.execute("UPDATE fixed_costs SET pattern='Hausverwaltung Neu' WHERE id=?", (fid,))
        con.commit()
        con.close()
    settings_ok(
        "fixed_save share_percent 'abc' (no 500)",
        {
            "action": "fixed_save",
            "fixed_id": str(fid),
            "pattern": "HV",
            "category_id": str(woh),
            "share_percent": "abc",
        },
    )
    settings_ok(
        "fixed_save share_percent 500 (no 500)",
        {
            "action": "fixed_save",
            "fixed_id": str(fid),
            "pattern": "HV",
            "category_id": str(woh),
            "share_percent": "500",
        },
    )
    con = m.db()
    row = con.execute("SELECT * FROM fixed_costs WHERE id=?", (fid,)).fetchone()
    con.close()
    check("fixed_save 500 -> rejected or clamped", 0 <= row["share_percent"] <= 100, dict(row))
    settings_ok(
        "fixed_save unknown fixed_id",
        {"action": "fixed_save", "fixed_id": "99999", "pattern": "X", "category_id": str(woh), "share_percent": "50"},
    )
    settings_ok(
        "fixed_save fixed_id missing",
        {"action": "fixed_save", "pattern": "X", "category_id": str(woh), "share_percent": "50"},
    )
    settings_ok(
        "fixed_save fixed_id garbage",
        {"action": "fixed_save", "fixed_id": "abc", "pattern": "X", "category_id": str(woh)},
    )
    settings_ok(
        "fixed_save without category_id",
        {"action": "fixed_save", "fixed_id": str(fid), "pattern": "HV2", "share_percent": "50"},
    )
    con = m.db()
    n = con.execute("SELECT COUNT(*) FROM fixed_costs WHERE pattern='X'").fetchone()[0]
    con.close()
    check("fixed_save on unknown id creates nothing", n == 0, n)

    print("2h) fixed_delete")
    settings_ok("fixed_delete unknown", {"action": "fixed_delete", "fixed_id": "99999"})
    settings_ok("fixed_delete missing", {"action": "fixed_delete"})
    settings_ok("fixed_delete garbage", {"action": "fixed_delete", "fixed_id": "abc"})
    con = m.db()
    n_before = con.execute("SELECT COUNT(*) FROM fixed_costs").fetchone()[0]
    con.close()
    settings_ok("fixed_delete valid", {"action": "fixed_delete", "fixed_id": str(fid)})
    con = m.db()
    n_after = con.execute("SELECT COUNT(*) FROM fixed_costs").fetchone()[0]
    con.close()
    check("fixed_delete removes exactly one", n_after == n_before - 1, (n_before, n_after))

    print("2i) unknown action / no action")
    settings_ok("action unknown", {"action": "foo"})
    settings_ok("no action", {})
    settings_ok("GET after everything", {"action": "theme", "theme": "auto"})
    check("GET /einstellungen 200", c.get("/einstellungen").status_code == 200)
    finish()
