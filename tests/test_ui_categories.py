"""3) /categories: create, subcategory, rename, recolour, redistribute colours, delete."""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_ui_categories():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    from _ui_common import post as _post

    def post(data, follow=False):
        return _post(c, "/categories", data, follow)

    def cat(name):
        con = m.db()
        r = con.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
        con.close()
        return dict(r) if r else None

    def flash_of(r):
        return r.get_data(as_text=True) if not isinstance(r, str) else r

    c.post("/setup", data={"src_bank": "1", "theme": "auto"})

    print("3a) Create top level")
    r = post({"add_name": "Haustier", "add_kind": "expense", "add_color": "#123456"})
    check("add -> 302", not isinstance(r, str) and r.status_code == 302, r)
    h = cat("Haustier")
    check(
        "Haustier created expense #123456",
        h and h["kind"] == "expense" and h["color"] == "#123456" and h["parent_id"] is None,
        h,
    )
    r = post({"add_name": "Nebenjob", "add_kind": "income"})
    n = cat("Nebenjob")
    check("income created with default colour", n and n["kind"] == "income" and n["color"] == "#888888", n)
    r = post({"add_name": "  Getrimmt  ", "add_kind": "expense"})
    check("name trimmed", cat("Getrimmt") is not None, cat("  Getrimmt  "))
    r = post({"add_name": "Muell", "add_kind": "wasauchimmer"})
    k = cat("Muell")
    print(f"       (Info: add_kind='wasauchimmer' -> stored kind={k and k['kind']!r})")
    check("add_kind garbage no 500", not isinstance(r, str), r)

    print("3b) Duplicates")
    r = post({"add_name": "Haustier", "add_kind": "expense"}, follow=True)
    check("duplicate no 500", not isinstance(r, str), r)
    check("duplicate: flash 'already exists'", "existiert bereits" in flash_of(r), flash_of(r)[:0])
    con = m.db()
    n_dup = con.execute("SELECT COUNT(*) FROM categories WHERE name='Haustier'").fetchone()[0]
    con.close()
    check("duplicate not created twice", n_dup == 1, n_dup)
    r = post({"add_name": "haustier", "add_kind": "expense"}, follow=True)
    print(f"       (Info: 'haustier' (only case differs) -> {'angelegt' if cat('haustier') else 'abgewiesen'})")

    print("3c) Subcategory inherits kind")
    hid = cat("Haustier")["id"]
    nid = cat("Nebenjob")["id"]
    r = post({"add_name": "Futter", "add_parent_id": str(hid), "add_kind": "income", "add_color": "#abcdef"})
    f = cat("Futter")
    check("subcategory created", f is not None and f["parent_id"] == hid, f)
    check("subcategory inherits kind=expense despite add_kind=income", f and f["kind"] == "expense", f and f["kind"])
    r = post({"add_name": "Trinkgeld", "add_parent_id": str(nid)})
    t = cat("Trinkgeld")
    check("subcategory of income = income", t and t["kind"] == "income", t)
    r = post({"add_name": "Enkel", "add_parent_id": str(f["id"])})
    e = cat("Enkel")
    print(
        f"       (Info: subcategory of a subcategory -> {e and ('angelegt, parent_id=%s' % e['parent_id'])} - second level is NOT prevented server-side)"
        if e
        else "       (Info: second level rejected)"
    )
    check("sub-sub no 500", not isinstance(r, str), r)
    if e:
        check(
            "sub-sub: pages still load",
            all(c.get(p).status_code == 200 for p in ("/categories", "/transactions", "/review", "/rules", "/")),
        )
        con = m.db()
        con.execute("DELETE FROM categories WHERE name='Enkel'")
        con.commit()
        con.close()
    r = post({"add_name": "Waise", "add_parent_id": "99999"})
    check("subcategory with unknown parent no 500", not isinstance(r, str), r)
    check("subcategory with unknown parent not created", cat("Waise") is None, cat("Waise"))
    r = post({"add_name": "Waise2", "add_parent_id": "abc"})
    check("add_parent_id garbage no 500", not isinstance(r, str), r)
    r = post({"add_name": "Duplikat Sub", "add_parent_id": str(hid)})
    r = post({"add_name": "Duplikat Sub", "add_parent_id": str(nid)}, follow=True)
    check("duplicate subcategory -> flash", "existiert bereits" in flash_of(r))

    print("3d) Rename / recolour / kind")
    r = post({"save": str(hid), f"name_{hid}": "Haustiere", f"color_{hid}": "#654321", f"kind_{hid}": "expense"})
    h = cat("Haustiere")
    check("renamed + recoloured", h and h["color"] == "#654321", h)
    hid = h["id"]
    r = post(
        {"save": str(hid), f"name_{hid}": "Lebensmittel", f"color_{hid}": "#654321", f"kind_{hid}": "expense"},
        follow=True,
    )
    check("rename to existing name no 500", not isinstance(r, str), r)
    check("rename to existing name -> flash", "existiert bereits" in flash_of(r))
    check("rename to existing name -> unchanged", cat("Haustiere") is not None)
    r = post({"save": str(hid), f"name_{hid}": "Haustiere", f"color_{hid}": "#654321", f"kind_{hid}": "income"})
    h = cat("Haustiere")
    check("kind of top category changeable", h["kind"] == "income", h["kind"])
    f = cat("Futter")
    print(f"       (Info: child 'Futter' after parent kind change: kind={f['kind']!r} - children are NOT updated)")
    check("child follows parent kind change", f["kind"] == "income", f["kind"])
    post({"save": str(hid), f"name_{hid}": "Haustiere", f"color_{hid}": "#654321", f"kind_{hid}": "expense"})
    fid = f["id"]
    r = post({"save": str(fid), f"name_{fid}": "Futter2", f"color_{fid}": "#000000", f"kind_{fid}": "income"})
    f = cat("Futter2")
    check("subcategory renamed, kind ignored", f and f["kind"] == "expense" and f["color"] == "#000000", f)
    r = post({"save": str(hid)})
    check("save without fields no 500", not isinstance(r, str), r)
    r = post({"save": "abc"})
    check("save with garbage id no 500", not isinstance(r, str), r)
    r = post({"save": "99999", "name_99999": "x", "color_99999": "#000000", "kind_99999": "expense"})
    check("save unknown id no 500", not isinstance(r, str), r)
    r = post({"save": str(hid), f"name_{hid}": "", f"color_{hid}": "#654321", f"kind_{hid}": "expense"})
    check("empty name no 500", not isinstance(r, str), r)
    h = cat("")
    print(f"       (Info: empty name -> {'GESPEICHERT als leere Kategorie' if h else 'abgewiesen'})")
    check("empty name is not saved", h is None, h)
    if h:
        post({"save": str(hid), f"name_{hid}": "Haustiere", f"color_{hid}": "#654321", f"kind_{hid}": "expense"})

    print("3e) Redistribute colours")
    con = m.db()
    before = {r["name"]: r["color"] for r in con.execute("SELECT name,color FROM categories")}
    con.close()
    r = post({"recolor": "1"}, follow=True)
    check("recolor no 500", not isinstance(r, str), r)
    check("recolor flash", "neu eingefärbt" in flash_of(r))
    con = m.db()
    rows = [dict(r) for r in con.execute("SELECT * FROM categories")]
    con.close()
    after = {r["name"]: r["color"] for r in rows}
    check("recolor changes colours", before != after)
    check("all colours valid hex", all(re.fullmatch(r"#[0-9A-Fa-f]{6}", v) for v in after.values()), after)

    def grey(hexs):
        r_, g_, b_ = int(hexs[1:3], 16), int(hexs[3:5], 16), int(hexs[5:7], 16)
        return max(r_, g_, b_) - min(r_, g_, b_) < 25

    transfers = [r for r in rows if r["kind"] == "transfer"]
    check(
        "transfer categories grey",
        all(grey(r["color"]) for r in transfers),
        [(r["name"], r["color"]) for r in transfers],
    )
    tops = [r for r in rows if r["kind"] != "transfer" and r["parent_id"] is None]
    check(
        "non-transfer top categories not grey",
        all(not grey(r["color"]) for r in tops),
        [(r["name"], r["color"]) for r in tops if grey(r["color"])],
    )
    check("top category colours pairwise distinct", len({r["color"] for r in tops}) == len(tops))
    r2 = post({"recolor": "1"}, follow=True)
    con = m.db()
    again = {r["name"]: r["color"] for r in con.execute("SELECT name,color FROM categories")}
    con.close()
    check("recolor stable (2nd run identical)", again == after)

    print("3f) Delete")
    r = post({"delete": str(hid)}, follow=True)
    check(
        "delete with subcategory blocked",
        "Unterkategorien" in flash_of(r) and cat("Haustiere") is not None,
        flash_of(r)[:0],
    )
    con = m.db()
    lm = con.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()["id"]
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
        "VALUES('bank','2026-06-01','BILLA','x',-5,'EUR','h1','active',?)",
        (lm,),
    )
    con.commit()
    con.close()
    # Freely deletable since 2026-09-15: payments become uncategorised, rules are deleted
    con = m.db()
    n_rules_lm = con.execute("SELECT COUNT(*) FROM rules WHERE category_id=?", (lm,)).fetchone()[0]
    con.close()
    r = post({"delete": str(lm)}, follow=True)
    check(
        "delete with transaction: deleted, payment uncategorised",
        cat("Lebensmittel") is None
        and "1 Zahlung wieder unkategorisiert" in flash_of(r)
        and m.db().execute("SELECT category_id FROM transactions WHERE raw_hash='h1'").fetchone()[0] is None,
        flash_of(r),
    )
    check(
        "delete with rule: rules deleted along",
        n_rules_lm > 0
        and f"{n_rules_lm} Regel" in flash_of(r)
        and m.db().execute("SELECT COUNT(*) FROM rules WHERE category_id=?", (lm,)).fetchone()[0] == 0,
        flash_of(r),
    )
    gid = cat("Getrimmt")["id"]
    con = m.db()
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status) "
        "VALUES('bank','2026-06-02','X','y',-10,'EUR','h2','active')"
    )
    tid = con.execute("SELECT id FROM transactions WHERE raw_hash='h2'").fetchone()[0]
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (tid, gid, -10))
    con.commit()
    con.close()
    r = post({"delete": str(gid)}, follow=True)
    check(
        "delete with split: deleted, split undone",
        cat("Getrimmt") is None
        and "1 Aufteilung aufgehoben" in flash_of(r)
        and m.db().execute("SELECT COUNT(*) FROM splits WHERE tx_id=?", (tid,)).fetchone()[0] == 0,
        flash_of(r),
    )
    con = m.db()
    con.execute("INSERT INTO categories(name,color,kind) VALUES('Frei','#123456','expense')")
    con.commit()
    con.close()
    r = post({"delete": str(cat("Frei")["id"])}, follow=True)
    check("delete unused -> deleted", cat("Frei") is None and "Kategorie Frei gelöscht" in flash_of(r), flash_of(r))
    for sname in m.SYSTEM_CATS:
        sid = cat(sname)["id"]
        r = post({"delete": str(sid)}, follow=True)
        check(f"SYSTEM_CAT {sname} not deletable", cat(sname) is not None and "System" in flash_of(r))
    um = cat("Umbuchung")
    r = post({"delete": str(um["id"])}, follow=True)
    print(
        f"       (Info: 'Umbuchung' (transfer, not in SYSTEM_CATS) -> {'geloescht' if cat('Umbuchung') is None else 'blockiert'})"
    )
    r = post({"delete": "99999"}, follow=True)
    check("delete unknown no 500", not isinstance(r, str) and "gibt es nicht" in flash_of(r), r)
    r = post({"delete": "abc"})
    check("delete garbage id no 500", not isinstance(r, str), r)
    # delete the subcategories, then the parent
    f2 = cat("Futter2")
    ds = cat("Duplikat Sub")
    post({"delete": str(f2["id"])})
    post({"delete": str(ds["id"])})
    check("subcategories deleted", cat("Futter2") is None and cat("Duplikat Sub") is None)
    r = post({"delete": str(hid)}, follow=True)
    check("parent deletable after children", cat("Haustiere") is None)

    print("3g) Page renders")
    html = c.get("/categories").get_data(as_text=True)
    check("GET /categories 200 + recolor button", 'name="recolor"' in html)
    check("subcategory form per top category", 'name="add_parent_id"' in html)
    check("delete button missing for used category", f'name="delete" value="{lm}"' not in html)
    finish()
