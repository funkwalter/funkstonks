"""Does dark mode hold on EVERY page?

Checked in stages:
  A) the template SOURCES for hard-wired light colours - rendered category
     colours (from the DB) are allowed and excluded
  B) every route is actually rendered and must carry data-theme="dark"
  C) every CSS variable used in the templates must be defined in style.css for
     LIGHT AND DARK - otherwise it falls back to the light default in the dark
"""

import glob
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_darkmode():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    m.set_setting(con, "theme", "dark")
    m.set_setting(con, "sources", '["bank","card","paypal","splitwise"]')
    m.set_setting(con, "splitwise_name", "Anna Muster")
    # some content so the pages do not render empty
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'miete')",
        (woh,),
    )
    for i, (d, p, a) in enumerate(
        [
            ("2026-06-01", "Hausverwaltung Mueller", -900),
            ("2026-06-05", "BILLA 0815", -42.10),
            ("2026-06-07", "Gehalt AG", 2500),
        ]
    ):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,?,'x',?,'EUR',?,'active',?)""",
            (d, p, a, f"h{i}", woh if a < 0 else None),
        )
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    print("A) Template sources without hard-wired light colours")
    # Allowed: colours that come from the database (category colours) - they always
    # sit in a Jinja expression. Forbidden: literal light hex values/rgba in the markup.
    LIT = re.compile(
        r"(?:background(?:-color)?|color|border(?:-color)?)\s*:\s*"
        r"(#[0-9A-Fa-f]{3,8}|rgba?\([^)]*\)|white)"
    )

    def strip_jinja(text):
        return re.sub(r"\{\{.*?\}\}", "JINJA", text, flags=re.S)

    def brightness(col):
        col = col.strip()
        mrgb = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", col)
        if mrgb:
            r, g, b = (int(x) for x in mrgb.groups())
        elif col.startswith("#") and len(col) >= 7:
            r, g, b = (int(col[i : i + 2], 16) for i in (1, 3, 5))
        elif col == "white":
            r = g = b = 255
        else:
            return None
        return (r * 299 + g * 587 + b * 114) / 1000

    for f in sorted(glob.glob(os.path.join(ROOT, "templates", "*.html"))):
        src = strip_jinja(open(f, encoding="utf-8").read())
        bad = []
        for mo in LIT.finditer(src):
            col = mo.group(1)
            b = brightness(col)
            # Light literals are the problem: they stay light in dark mode.
            if b is not None and b > 180:
                bad.append(col)
        check(f"{os.path.basename(f)} without light literal colours", not bad, bad)

    print("\nB) Every page carries the dark scheme")
    ROUTES = [
        "/",
        "/transactions",
        "/jahr",
        "/fixkosten",
        "/partner",
        "/splitwise",
        "/rules",
        "/categories",
        "/import",
        "/review",
        "/paypal",
        "/karte",
        "/einstellungen",
        "/hilfe",
        "/setup",
    ]
    for r in ROUTES:
        resp = c.get(r)
        ok = resp.status_code == 200
        html = resp.get_data(as_text=True) if ok else ""
        check(f"{r} loads", ok, resp.status_code)
        if ok:
            check(f"{r} sets data-theme=dark", 'data-theme="dark"' in html, html[:120])

    print("\nC) All used CSS variables are defined light AND dark")
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    light = css.split('[data-theme="dark"]')[0]
    dark = css[css.index('[data-theme="dark"]') :]
    used = set()
    for f in glob.glob(os.path.join(ROOT, "templates", "*.html")):
        used |= set(re.findall(r"var\((--[a-z]+)", open(f, encoding="utf-8").read()))
    used |= set(re.findall(r"var\((--[a-z]+)", css))
    for v in sorted(used):
        check(f"{v} defined light", (v + ":") in light, v)
        check(f"{v} defined dark", (v + ":") in dark, v)

    print("\nD) Chart follows the scheme")
    dash = c.get("/?month=2026-06").get_data(as_text=True)
    djs = open(os.path.join(ROOT, "static", "js", "dashboard.js"), encoding="utf-8").read()
    ajs = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    check("dashboard loads its script", "js/dashboard.js" in dash)
    check("chart reads the text colour from the theme", "themeVar('--ink'" in djs)
    check("chart reads the label background", "themeVar('--halo'" in djs)
    check("chart reacts to the toggle", "moneyflow:themechange" in djs)
    check(
        "toggle announces the change",
        "moneyflow:themechange" in ajs and "js/app.js" in c.get("/hilfe").get_data(as_text=True),
    )

    print("\nE) Toggle stays saved")
    c.post("/einstellungen", data={"action": "theme", "theme": "light"})
    check("light saved", 'data-theme="light"' in c.get("/").get_data(as_text=True))
    c.post("/einstellungen", data={"action": "theme", "theme": "dark"})
    check("dark saved", 'data-theme="dark"' in c.get("/").get_data(as_text=True))
    check(
        "auto possible", (c.post("/einstellungen", data={"action": "theme", "theme": "auto"}).status_code in (200, 302))
    )

    print("\nF) Readability: contrast of the most important colour pairs")

    def _lum(h):
        def f(c):
            c = c / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (int(h[i : i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def _ratio(a, b):
        la, lb = _lum(a), _lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    def _vars(block):
        return dict(re.findall(r"(--[a-z]+)\s*:\s*(#[0-9A-Fa-f]{6})", block))

    # Each theme on its own: ":root{...}" is "light", every ":root[data-theme=x]{...}"
    # another one (eight schemes since 2026-10-10, sidebar with its own colours).
    themes = {"light": _vars(re.search(r":root\{(.*?)\}", css, re.S).group(1))}
    for mo in re.finditer(r':root\[data-theme="(\w+)"\]\{(.*?)\}', css, re.S):
        themes[mo.group(1)] = _vars(mo.group(2))
    check(
        "eight themes in style.css",
        set(themes) >= {"light", "dark", "slate", "sand", "forest", "midnight", "graphite", "ocean"},
        sorted(themes),
    )
    check(
        "every theme defines all variables of the light theme",
        all(set(themes["light"]) <= set(v) for v in themes.values()),
        {k: sorted(set(themes["light"]) - set(v)) for k, v in themes.items() if set(themes["light"]) - set(v)},
    )
    # Text colour -> background, per theme. Catches exactly the case that really
    # happened: ghost buttons and links in --petrol on a dark card (1.15).
    PAIRS = [
        ("--ink", "--card", "normaler Text"),
        ("--ink", "--bg", "Text auf Seitenhintergrund"),
        ("--mute", "--card", "gedaempfter Text"),
        ("--accent", "--card", "Knopf-/Linktext"),
        ("--accent", "--bg", "Linktext auf Seite"),
        ("--mute", "--soft", "Text in Pills"),
        ("--err", "--errbg", "Fehlertext"),
        ("--pos", "--card", "gruene Betraege"),
        ("--neg", "--card", "rote Betraege"),
        ("--invest", "--card", "blaue Betraege (Investitionen)"),
        ("--navink", "--navbg", "Menuetext"),
        ("--navtitle", "--navbg", "Menue-Gruppentitel"),
        ("--onaccent", "--navactive", "aktiver Menuepunkt"),
        ("--onaccent", "--petrol", "Knopftext"),
    ]
    for mode, vv in themes.items():
        for fg, bg, label in PAIRS:
            if fg in vv and bg in vv:
                r = _ratio(vv[fg], vv[bg])
                check(f"{mode}: {label} readable ({r:.1f})", r >= 4.5, f"{vv[fg]} auf {vv[bg]}")

    print("\nG) Every theme can be selected and renders")
    for key in ("slate", "sand", "forest", "midnight", "graphite", "ocean"):
        c.post("/einstellungen", data={"action": "theme", "theme": key})
        check(f"{key} saved and in the HTML", f'data-theme="{key}"' in c.get("/").get_data(as_text=True))
    c.post("/einstellungen", data={"action": "theme", "theme": "kaputt"})
    check("unknown theme -> auto", 'data-theme="auto"' in c.get("/").get_data(as_text=True))
    html = c.get("/").get_data(as_text=True)
    check(
        "sidebar with all themes in the toggle",
        html.count('<option value="') >= 8 and 'id="theme-toggle"' in html,
    )
    check(
        "sidebar: groups and active entry",
        'class="nav-title">Auswerten' in html and 'class="nav-item active"' in html,
    )
    finish()
