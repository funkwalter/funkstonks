"""4) /rules: multiple patterns, inline edit/delete (ajax=1 -> 204), apply retroactively, hit counts."""

import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_ui_rules():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    from _ui_common import post as _post

    def post(url, data, follow=False):
        return _post(c, url, data, follow)

    def rules():
        con = m.db()
        r = [
            dict(x)
            for x in con.execute(
                "SELECT r.id, r.pattern, r.category_id, c.name AS cat FROM rules r JOIN categories c ON c.id=r.category_id ORDER BY r.id"
            )
        ]
        con.close()
        return r

    def cid(name):
        con = m.db()
        r = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        con.close()
        return r["id"]

    _h = [0]

    def add_tx(con, source, d, partner, desc, amount, status="active", cat=None, extra="{}"):
        _h[0] += 1
        cidv = cid(cat) if cat else None
        return con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id,extra) "
            "VALUES(?,?,?,?,?,'EUR',?,?,?,?)",
            (source, d, partner, desc, amount, f"h{_h[0]}", status, cidv, extra),
        ).lastrowid

    c.post(
        "/setup",
        data={
            "src_bank": "1",
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

    print("4a) Income rule from the settings appears")
    html = c.get("/rules").get_data(as_text=True)
    check("'Firma AG' on /rules", "Firma AG" in html)
    check("in the Gehalt card", re.search(r"Gehalt.*?Firma AG", html, re.S) is not None)

    print("4b) Multiple patterns")
    lm = cid("Lebensmittel")
    ko = cid("Konsum")
    r = post("/rules", {"pattern": " Spar , Billa,*Interspar*,, ,  ", "category_id": str(lm)})
    check("add 302", not isinstance(r, str) and r.status_code == 302, r)
    pats = [x["pattern"] for x in rules() if x["cat"] == "Lebensmittel"]
    check("3 rules, trimmed, asterisks removed, empty ignored", pats == ["Spar", "Billa", "Interspar"], pats)
    r = post("/rules", {"pattern": "billa", "category_id": str(ko)})
    pats = [(x["pattern"], x["cat"]) for x in rules() if x["pattern"].lower() == "billa"]
    check("same pattern (case-insens.) -> upsert instead of duplicate", pats == [("Billa", "Konsum")], pats)
    post("/rules", {"pattern": "Billa", "category_id": str(lm)})
    r = post("/rules", {"pattern": "", "category_id": str(lm)})
    check("empty pattern no 500", not isinstance(r, str), r)
    r = post("/rules", {"pattern": "Muell", "category_id": "99999"})
    check("unknown category_id no 500", not isinstance(r, str), r)
    r = post("/rules", {"pattern": "Muell2"})
    check("missing category_id no 500", not isinstance(r, str), r)
    r = post("/rules", {"pattern": "***", "category_id": str(lm)})
    check("only asterisks -> nothing created", not any(x["pattern"] == "" for x in rules()), rules())

    print("4c) Inline edit (ajax=1 -> 204)")
    bid = [x for x in rules() if x["pattern"] == "Billa"][0]["id"]
    r = post("/rules", {"edit": str(bid), "pattern": "BILLA DANKT", "category_id": str(lm), "ajax": "1"})
    check(
        "edit ajax -> 204", not isinstance(r, str) and r.status_code == 204, r if isinstance(r, str) else r.status_code
    )
    check("edit applied", [x for x in rules() if x["id"] == bid][0]["pattern"] == "BILLA DANKT", rules())
    r = post("/rules", {"edit": str(bid), "pattern": "BILLA DANKT", "category_id": str(ko)})
    check("edit without ajax -> 302", not isinstance(r, str) and r.status_code == 302, r)
    check("edit category changed", [x for x in rules() if x["id"] == bid][0]["cat"] == "Konsum")
    r = post("/rules", {"edit": str(bid), "pattern": "", "category_id": str(ko), "ajax": "1"})
    check(
        "edit empty pattern -> 204, unchanged",
        not isinstance(r, str)
        and r.status_code == 204
        and [x for x in rules() if x["id"] == bid][0]["pattern"] == "BILLA DANKT",
        r,
    )
    r = post("/rules", {"edit": "99999", "pattern": "x", "category_id": str(ko), "ajax": "1"})
    check("edit unknown id no 500", not isinstance(r, str), r)
    r = post("/rules", {"edit": str(bid), "pattern": "x", "category_id": "99999", "ajax": "1"})
    check("edit unknown category_id no 500", not isinstance(r, str), r)
    r = post("/rules", {"edit": str(bid), "pattern": "x", "ajax": "1"})
    check("edit without category_id no 500", not isinstance(r, str), r)
    post("/rules", {"edit": str(bid), "pattern": "BILLA DANKT", "category_id": str(lm), "ajax": "1"})

    print("4d) Delete (ajax=1 -> 204)")
    sid = [x for x in rules() if x["pattern"] == "Spar"][0]["id"]
    r = post("/rules", {"delete": str(sid), "ajax": "1"})
    check("delete ajax -> 204", not isinstance(r, str) and r.status_code == 204, r)
    check("rule gone", not any(x["id"] == sid for x in rules()))
    iid = [x for x in rules() if x["pattern"] == "Interspar"][0]["id"]
    r = post("/rules", {"delete": str(iid)})
    check("delete without ajax -> 302", not isinstance(r, str) and r.status_code == 302, r)
    r = post("/rules", {"delete": "99999", "ajax": "1"})
    check("delete unknown -> 204", not isinstance(r, str) and r.status_code == 204, r)
    r = post("/rules", {"delete": "abc", "ajax": "1"})
    check("delete garbage no 500", not isinstance(r, str), r)

    print("4e) Hit counts + apply retroactively")
    con = m.db()
    t1 = add_tx(con, "bank", "2026-06-01", "BILLA DANKT 001", "Einkauf", -10)  # uncategorised
    t2 = add_tx(con, "bank", "2026-06-02", "BILLA DANKT 002", "Einkauf", -20, cat="Konsum")  # wrong
    t3 = add_tx(con, "bank", "2026-06-03", "BILLA DANKT 003", "Einkauf", -30, cat="Lebensmittel")  # matches
    t4 = add_tx(
        con, "bank", "2026-06-04", "BILLA DANKT 004", "Einkauf", -40, status="excluded", cat="Konsum"
    )  # not active
    t5 = add_tx(con, "bank", "2026-06-05", "BILLA DANKT 005", "Einkauf", -50, cat="Konsum")  # gets split
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (t5, ko, -50))
    con.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (t5,))
    t6 = add_tx(
        con,
        "splitwise",
        "2026-06-06",
        "reise",
        "billa dankt",
        -15,
        cat="Urlaub",
        extra=json.dumps({"cost": 30, "bal": -15}),
    )  # holiday group -> protected
    con.execute("INSERT INTO urlaub_groups(partner) VALUES('reise')")
    con.commit()
    con.close()
    html = c.get("/rules").get_data(as_text=True)
    row = re.search(r'data-rule-id="%d".*?</tr>' % bid, html, re.S).group(0)
    check(
        "hit count (3x, 2 deviating: uncategorised + wrong) shown",
        "(3×" in row and "<b>2 abweichend</b>" in row,
        re.sub(r"\s+", " ", row)[:300],
    )
    r = post(f"/rules/{bid}/apply", {"dry": "1"})
    check("apply dry -> 200 JSON", not isinstance(r, str) and r.status_code == 200 and r.is_json, r)
    d = r.get_json()
    check(
        "dry: matched=3 changed=2 category=Lebensmittel",
        d.get("matched") == 3 and d.get("changed") == 2 and d.get("category") == "Lebensmittel",
        d,
    )
    con = m.db()
    before = {x[0]: x[1] for x in con.execute("SELECT id, category_id FROM transactions")}
    con.close()
    r = post(f"/rules/{bid}/apply", {"dry": "1"})
    con = m.db()
    after = {x[0]: x[1] for x in con.execute("SELECT id, category_id FROM transactions")}
    con.close()
    check("dry changes nothing", before == after)
    r = post(f"/rules/{bid}/apply", {})
    d = r.get_json()
    check("apply -> changed=2", d.get("changed") == 2 and d.get("category") == "Lebensmittel", d)
    con = m.db()
    cats = {x[0]: x[1] for x in con.execute("SELECT id, category_id FROM transactions")}
    con.close()
    check("t1 (uncat.) now Lebensmittel", cats[t1] == lm)
    check("t2 (Konsum) -> Lebensmittel", cats[t2] == lm)
    check("t3 stays Lebensmittel", cats[t3] == lm)
    check("t4 (excluded) untouched", cats[t4] == ko)
    check("t5 (split) untouched, category_id NULL", cats[t5] is None)
    check("t6 (holiday group) untouched", cats[t6] == cid("Urlaub"))
    check(
        "no automatic backup any more (2026-09-15)",
        not os.path.isdir(os.path.join(tmp, "backups")) or not os.listdir(os.path.join(tmp, "backups")),
        os.listdir(tmp),
    )
    check("no backup in the real project folder", not os.path.exists(os.path.join(ROOT, "backups", "_agent_marker")))
    r = post(f"/rules/{bid}/apply", {})
    check("apply again -> changed=0", r.get_json().get("changed") == 0, r.get_json())
    r = post("/rules/99999/apply", {"dry": "1"})
    check("apply unknown rule -> 404", not isinstance(r, str) and r.status_code == 404, r)
    html = c.get("/rules").get_data(as_text=True)
    row = re.search(r'data-rule-id="%d".*?</tr>' % bid, html, re.S).group(0)
    check("hit count after apply (3x, 0 deviating)", "(3×)" in row, re.sub(r"\s+", " ", row)[:300])
    check(
        "page has rule-apply-btn and dry flow in the JS",
        "rule-apply-btn" in html
        and "dry.append('dry', '1')" in open(os.path.join(ROOT, "static", "js", "rules.js"), encoding="utf-8").read(),
    )

    print("4f) 'apply' button (all rules on uncategorised)")
    con = m.db()
    t7 = add_tx(con, "bank", "2026-06-07", "billa dankt 7", "x", -7)
    con.commit()
    con.close()
    r = post("/rules", {"apply": "1"}, follow=True)
    check("apply button flash", "kategorisiert" in r.get_data(as_text=True))
    con = m.db()
    v = con.execute("SELECT category_id FROM transactions WHERE id=?", (t7,)).fetchone()[0]
    con.close()
    check("apply button categorises", v == lm, v)

    print("4g) GET after everything")
    check("GET /rules 200", c.get("/rules").status_code == 200)
    html = c.get("/rules").get_data(as_text=True)
    check(
        "automation block (foreign POS)",
        "Terminal im Ausland" in html and 'name="auto_delete" value="foreign_pos"' in html,
    )
    check("rule dropdown uses category groups", 'name="category_id"' in html)
    finish()
