import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_fk():

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
    abos = con.execute("SELECT id FROM categories WHERE name='Abos'").fetchone()["id"]
    rest = con.execute("SELECT id FROM categories WHERE name LIKE 'Restaurant%'").fetchone()["id"]
    con.execute("INSERT OR IGNORE INTO categories(name,color,kind) VALUES('Investitionen','#123456','expense')")
    inv = con.execute("SELECT id FROM categories WHERE name='Investitionen'").fetchone()["id"]
    for i in range(4):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id) VALUES('bank',?,'PATREON IRELAND','abo',-5.40,'EUR',?,'active',?)""",
            (f"2026-0{i + 4}-05", f"a{i}", abos),
        )
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id) VALUES('bank',?,'MCDONALDS 102','essen',-9.90,'EUR',?,'active',?)""",
            (f"2026-0{i + 4}-07", f"b{i}", rest),
        )
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id) VALUES('bank',?,'ETF SPARPLAN','x',-100,'EUR',?,'active',?)""",
            (f"2026-0{i + 4}-11", f"c{i}", inv),
        )
    # subcategory of Abos
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Streaming','#777','expense',?)", (abos,))
    sub = con.execute("SELECT id FROM categories WHERE name='Streaming'").fetchone()["id"]
    for i in range(4):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
        raw_hash,status,category_id) VALUES('bank',?,'SPOTIFY AB','abo',-10.99,'EUR',?,'active',?)""",
            (f"2026-0{i + 4}-09", f"s{i}", sub),
        )
    con.commit()
    con.close()

    print("1) Recurring costs show only Abos/Investitionen")
    html = c.get("/fixkosten").get_data(as_text=True)
    check("subscription appears", "PATREON" in html)
    check("investment appears", "ETF SPARPLAN" in html)
    check("subcategory of Abos appears", "SPOTIFY" in html)
    # series rows (tr.series-row) must not contain the restaurant series; in the
    # "add by hand" selection list it may appear as a candidate (wanted since
    # 2026-10-10: the user can add what fell through the filter himself).
    import re as _re

    series_html = "".join(_re.findall(r'<tr class="series-row"[^>]*>.*?</tr>', html, _re.S))
    check("restaurant series does NOT appear as a series", "MCDONALDS" not in series_html, "wrongly listed")
    check(
        "... and not as a suggestion either (restaurant is not an allowed category, since 2026-10-13)",
        "MCDONALDS" not in html,
    )

    print("\n2) Hiding/adding goes through series_overrides (the old variant stays gone)")
    check("button to hide each series", 'action="/fixkosten/override"' in html and 'name="mode" value="hide"' in html)
    check("no old 'permanently hidden' block", "Dauerhaft ausgeblendet" not in html)
    check(
        "route /fixkosten/ignore no longer exists",
        c.post("/fixkosten/ignore", data={"partner": "x"}).status_code == 404,
    )
    check(
        "route /fixkosten/unignore no longer exists",
        c.post("/fixkosten/unignore", data={"key": "x"}).status_code == 404,
    )
    con = m.db()
    t = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fixkosten_ignored'").fetchone()
    t2 = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='series_overrides'").fetchone()
    con.close()
    check("old table is no longer created, new one is", t is None and t2 is not None, (t, t2))

    print("\n3) Direct bookings of the parent category are labelled")
    con = m.db()
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Fortgehen','#666','expense',?)", (rest,))
    fg = con.execute("SELECT id FROM categories WHERE name='Fortgehen'").fetchone()["id"]
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,currency,
    raw_hash,status,category_id) VALUES('bank','2026-05-11','BAR','x',-40,'EUR','f1','active',?)""",
        (fg,),
    )
    con.commit()
    con.close()
    for path in ("/jahr?y=2026", "/?month=2026-05"):
        h = c.get(path).get_data(as_text=True)
        check(f"{path}: 'ohne Unterkategorie' label", "ohne Unterkategorie" in h)

    print("\n4) Regressions")
    for p in ("/", "/transactions", "/partner", "/rules", "/fixkosten", "/jahr", "/categories"):
        check(f"GET {p}", c.get(p).status_code == 200)
    finish()
