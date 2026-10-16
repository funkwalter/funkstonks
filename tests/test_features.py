"""Import history, restoring a backup, comparison with the previous period
(2026-09-13). Everything against a temp DB; backups/ ends up next to the temp DB."""

import json
import os
import re
import sys
from html import unescape as _unescape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _harness import check, finish  # noqa: E402
from _import_common import count, english_bank, fresh_db, george_bank, m, one, q, upload


def test_features():

    tmp, c = fresh_db()
    BDIR = os.path.join(tmp, "backups")
    assert "moneyflow.db" not in m.DB_PATH

    def cid(name):
        return one("SELECT id FROM categories WHERE name=?", name)["id"]

    def html(url):
        return c.get(url).get_data(as_text=True)

    # ---------------------------------------------------------------- 1) import history
    print("1) Import history")
    check("empty: no history card", "Import-Historie" not in html("/import"))
    h = upload(
        c,
        (
            george_bank([("01.06.2026", "Billa", "Einkauf", "-20,00"), ("02.06.2026", "Spar", "Einkauf", "-30,00")]),
            "konto_2026-06.csv",
        ),
    )
    rows = q("SELECT * FROM imports ORDER BY id")
    check("one row per file", len(rows) == 1, rows)
    r = rows[0]
    check(
        "file/kind/new/known",
        (r["filename"], r["kind"], r["new_rows"], r["skipped"]) == ("konto_2026-06.csv", "bank", 2, 0),
        dict(r),
    )
    check("timestamp set", re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", r["ts"] or ""), r["ts"])
    # The same file again plus one with a new row
    upload(
        c,
        (
            george_bank([("01.06.2026", "Billa", "Einkauf", "-20,00"), ("02.06.2026", "Spar", "Einkauf", "-30,00")]),
            "konto_2026-06.csv",
        ),
        (english_bank([("2026-06-05", "-5.00", "Hofer", "Brot")]), "extra.csv"),
    )
    rows = q("SELECT * FROM imports ORDER BY id")
    check("three rows after second upload", len(rows) == 3, len(rows))
    check("repeat: 0 new, 2 known", (rows[1]["new_rows"], rows[1]["skipped"]) == (0, 2), dict(rows[1]))
    check(
        "new file: 1 new", (rows[2]["new_rows"], rows[2]["skipped"], rows[2]["kind"]) == (1, 0, "bank"), dict(rows[2])
    )
    check("bookings correct (3)", count("source='bank'") == 3)
    # Unknown format is logged
    upload(c, (b"foo;bar\n1;2\n", "kaputt.csv"))
    r = one("SELECT * FROM imports ORDER BY id DESC LIMIT 1")
    check(
        "unrecognized format ends up in the history", r["kind"] == "?" and "Format nicht erkannt" in r["note"], dict(r)
    )
    h = html("/import")
    check("card visible", "Import-Historie" in h)
    check("file names and kind on the page", "konto_2026-06.csv" in h and "extra.csv" in h and "Kontoauszug" in h)
    check(
        "broken file dimmed (mute)",
        re.search(r'class="mute"[^>]*>\s*<td[^>]*>[^<]*</td><td>kaputt.csv', h) is not None,
    )
    check("newest first", h.index("kaputt.csv") < h.index("extra.csv") < h.rindex("konto_2026-06.csv"))

    # ---------------------------------------------------------------- 2) backup: create, restore, upload
    print("2) Backup")
    h = html("/sicherung")
    check(
        "page without backup: note + backup button + CSV note (tooltip)",
        "Noch keine Sicherung" in h
        and "Jetzt sichern" in h
        and "nicht wieder importierbar" in h
        and 'href="/export.csv"' in h,
    )
    # Sidebar since 2026-10-10: the label sits in <span class="nav-label">, the link before it.
    check(
        "navigation is called Sicherung, not Export",
        'href="/sicherung"' in h
        and '<span class="nav-label">Sicherung</span>' in h
        and 'href="/export.csv">Export' not in h,
    )
    check("settings no longer have a backup card", "zurückspielen" not in html("/einstellungen"))
    check("no backup from start/import/categorizing", not os.path.isdir(BDIR) or not os.listdir(BDIR))
    n_before = count("source='bank'")
    r = c.post("/sicherung", data={"action": "backup"}, follow_redirects=True)
    files = sorted(os.listdir(BDIR))
    check(
        "button creates a backup next to the temp DB",
        len(files) == 1 and files[0].startswith("moneyflow_") and "Gesichert" in r.get_data(as_text=True),
        files,
    )
    check("not written to the real backups/", not os.path.abspath(BDIR).startswith(os.path.abspath(m.APP_DIR)))
    snap = files[0]
    h = html("/sicherung")
    check(
        "backup listed as manual, with restore/download/delete",
        snap in h and "manuell" in h and "zurückspielen" in h and f"/sicherung/download/{snap}" in h and "löschen" in h,
    )
    r = c.get(f"/sicherung/download/{snap}")
    check(
        "download delivers the SQLite file",
        r.status_code == 200 and r.get_data()[:15] == b"SQLite format 3",
        r.get_data()[:15],
    )
    # Then change something: new booking, set a category
    upload(c, (english_bank([("2026-06-20", "-99.00", "Neu", "spaeter")]), "neu.csv"))
    check("change present", count("source='bank'") == n_before + 1)
    check("import creates no backup", len(os.listdir(BDIR)) == 1)
    tid = one("SELECT id FROM transactions WHERE partner='Billa'")["id"]
    cat_before = one("SELECT category_id c FROM transactions WHERE id=?", tid)["c"]  # rule "billa" already applies
    c.post(f"/tx/{tid}/update", data={"category_id": cid("Restaurant/Ausgehen"), "ajax": "1"})
    check(
        "category changed",
        one("SELECT category_id c FROM transactions WHERE id=?", tid)["c"] == cid("Restaurant/Ausgehen"),
    )

    # Bad names: no effect, error message
    for bad in ("../t.db", "moneyflow_x.db", "", "moneyflow_20260101_000000.db"):
        r = c.post("/sicherung", data={"action": "restore", "backup": bad}, follow_redirects=True)
        txt = r.get_data(as_text=True)
        check(
            f"rejected: {bad!r}",
            r.status_code == 200 and "Eingabe nicht übernommen" in txt and count("source='bank'") == n_before + 1,
            (r.status_code, count("source='bank'")),
        )

    r = c.post("/sicherung", data={"action": "restore", "backup": snap}, follow_redirects=True)
    txt = r.get_data(as_text=True)
    check("restore flash", "zurückgespielt" in txt, txt[:300])
    check("later booking is gone", count("source='bank'") == n_before, count("source='bank'"))
    check(
        "later category is gone",
        one("SELECT category_id c FROM transactions WHERE id=?", tid)["c"] == cat_before,
    )
    files2 = sorted(os.listdir(BDIR))
    check("safety copy created beforehand (the only automatic one)", len(files2) == 2 and snap in files2, files2)
    other = [f for f in files2 if f != snap][0]
    check("safety copy is named as such", "_vor-wiederherstellung" in other, other)
    check("... and listed as such", "vor Wiederherstellung" in html("/sicherung"))
    import sqlite3

    sc = sqlite3.connect(os.path.join(BDIR, other))
    check(
        "safety copy = state before the restore",
        sc.execute("SELECT COUNT(*) FROM transactions WHERE source='bank'").fetchone()[0] == n_before + 1,
    )
    sc.close()
    r = c.post(f"/tx/{tid}/update", data={"category_id": cid("Restaurant/Ausgehen"), "ajax": "1"})
    check(
        "still writable after restore",
        r.status_code == 204
        and one("SELECT category_id c FROM transactions WHERE id=?", tid)["c"] == cid("Restaurant/Ausgehen"),
    )
    check(
        "restore itself undone: restore the other backup",
        "zurückgespielt"
        in c.post("/sicherung", data={"action": "restore", "backup": other}, follow_redirects=True).get_data(
            as_text=True
        )
        and count("source='bank'") == n_before + 1,
    )
    n_files = len(os.listdir(BDIR))
    a, b = m.backup_db(), m.backup_db()
    check("two backups in the same second: two files", a != b and len(os.listdir(BDIR)) == n_files + 2, (a, b))
    # No rotation any more: 16 backups stay 16
    import shutil

    names = []
    for i in range(16 - len(os.listdir(BDIR))):
        nm = "moneyflow_2025%02d01_000000.db" % (i + 1)
        shutil.copy2(os.path.join(BDIR, snap), os.path.join(BDIR, nm))
        names.append(nm)
    check("16 backups present", len(os.listdir(BDIR)) == 16, len(os.listdir(BDIR)))
    r = c.post("/sicherung", data={"action": "restore", "backup": names[0]}, follow_redirects=True)
    check(
        "oldest backup restored",
        "zurückgespielt" in r.get_data(as_text=True) and count("source='bank'") == n_before,
    )
    check("no rotation: all remain (+1 safety copy)", len(os.listdir(BDIR)) == 17, len(os.listdir(BDIR)))
    # Delete
    r = c.post("/sicherung", data={"action": "delete", "backup": names[0]}, follow_redirects=True)
    check(
        "delete backup",
        not os.path.exists(os.path.join(BDIR, names[0])) and "gelöscht" in r.get_data(as_text=True),
    )
    r = c.post("/sicherung", data={"action": "delete", "backup": "../t.db"}, follow_redirects=True)
    check(
        "delete with a path rejected",
        os.path.exists(m.DB_PATH) and "Eingabe nicht übernommen" in r.get_data(as_text=True),
    )
    # Foreign file with a matching name: not a MoneyFlow DB
    junk = os.path.join(BDIR, "moneyflow_20990101_000000.db")
    j = sqlite3.connect(junk)
    j.execute("CREATE TABLE x(a)")
    j.commit()
    j.close()
    r = c.post("/sicherung", data={"action": "restore", "backup": os.path.basename(junk)}, follow_redirects=True)
    check(
        "not a MoneyFlow DB -> rejected",
        "keine funkSTONKS-Datenbank" in r.get_data(as_text=True) and count("source='bank'") == n_before,
    )
    # Upload a file: the state with the 99 booking from the safety copy
    import io as _io

    r = c.post(
        "/sicherung",
        data={"action": "upload", "dbfile": (open(os.path.join(BDIR, other), "rb"), "alt.db")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    check(
        "upload database file",
        "eingespielt" in r.get_data(as_text=True) and count("source='bank'") == n_before + 1,
        count("source='bank'"),
    )
    r = c.post(
        "/sicherung",
        data={"action": "upload", "dbfile": (_io.BytesIO(b"kein sqlite"), "x.db")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    check("junk file rejected", "keine SQLite" in r.get_data(as_text=True) and count("source='bank'") == n_before + 1)
    # Fresh installation: /sicherung is reachable before setup
    tmp2, c2 = fresh_db()
    con2 = m.db()
    con2.execute("DELETE FROM settings WHERE key='setup_done'")
    con2.commit()
    con2.close()
    check(
        "reachable before setup (move to a new machine)",
        c2.get("/sicherung").status_code == 200 and c2.get("/", follow_redirects=False).status_code == 302,
    )

    # ---------------------------------------------------------------- 3) comparison with the previous period
    print("3) Comparison with the previous period")
    tmp, c = fresh_db()
    con = m.db()

    def tx(d, partner, amount, cat=None, src="bank"):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,"
            "raw_hash,status,category_id) VALUES(?,?,?,?,?,'EUR',?,'active',?)",
            (src, d, partner, "", amount, f"{src}{d}{partner}{amount}", cat),
        )

    def cid2(name):
        return con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()["id"]

    leb, rest, geh, kon = cid2("Lebensmittel"), cid2("Restaurant/Ausgehen"), cid2("Gehalt"), cid2("Konsum")
    # 2026: Jan 10, Feb 20, Mar 60, Apr 20 (restaurant), Jul 100+50, Aug 130+40 - May/June empty
    tx("2026-01-05", "Billa", -10, leb)
    tx("2026-02-05", "Billa", -20, leb)
    tx("2026-03-03", "Billa", -60, leb)
    tx("2026-04-10", "Pizzeria", -20, rest)
    tx("2026-07-03", "Billa", -100, leb)
    tx("2026-07-10", "Pizzeria", -50, rest)
    tx("2026-07-15", "AG", 2000, geh)
    tx("2026-08-03", "Billa", -130, leb)
    tx("2026-08-15", "AG", 2000, geh)
    tx("2026-08-20", "Shop", -40, kon)
    # 2025: the same months as 2026 (for the year comparison), total 220
    tx("2025-01-05", "Billa", -5, leb)
    tx("2025-02-05", "Billa", -5, leb)
    tx("2025-03-03", "Billa", -50, leb)
    tx("2025-04-10", "Pizzeria", -10, rest)
    tx("2025-07-03", "Billa", -70, leb)
    tx("2025-08-03", "Billa", -80, leb)
    tx("2025-08-15", "AG", 1900, geh)
    con.commit()
    con.close()

    h = html("/?month=2026-08")
    VS = r" € vs\. <a[^>]*>"  # since 2026-10-14 the previous period on the tile is a link
    check(
        "no comparison card on the dashboard any more (own page /vergleich)",
        "Vergleich: August 2026" not in h and "Ausgaben gesamt" not in h,
    )
    check("expense delta in the tile (+20.00 vs. July)", re.search(r"\+20\.00" + VS + "Juli 2026", h) is not None)
    check("income delta 0 (mute)", re.search(r'class="kpi-delta mute">\+0\.00' + VS + "Juli 2026", h) is not None)
    check("balance delta -20.00", re.search(r'class="kpi-delta neg">-20\.00' + VS + "Juli 2026", h) is not None)
    check(
        "no 'not categorized' notice box on the dashboard (badge suffices)",
        '<div class="warn">Noch' not in h and "jetzt durchgehen" not in h,
    )
    sec = html("/vergleich?a_month=2026-08&b_month=2026-07")
    check("comparison page: August against July", "August 2026" in sec and "Juli 2026" in sec)

    def row(name):
        mm = re.search(
            r"<td>" + re.escape(name) + r"</td>\s*<td class=\"num\"><a[^>]*>([\d.]+) €</a></td>\s*"
            r"<td class=\"num mute\"><a[^>]*>([\d.]+) €</a></td>\s*<td class=\"num (\w+)\">([+\-][\d.]+) €</td>\s*"
            r"<td class=\"num mute\">([^<]*)</td>",
            sec,
        )
        return mm.groups() if mm else None

    lr = row("Lebensmittel")
    check("Lebensmittel 130 / 100 / +30 / +30 %", lr == ("130.00", "100.00", "neg", "+30.00", "+30 %"), lr)
    rr = row("Restaurant/Ausgehen")
    check("Restaurant 0 / 50 / -50 / -100 %", rr == ("0.00", "50.00", "pos", "-50.00", "-100 %"), rr)
    kr = row("Konsum")
    check("Konsum 40 / 0 / +40 / new", kr == ("40.00", "0.00", "neg", "+40.00", "neu"), kr)
    first = re.search(r"<td>([^<]+)</td>\s*<td class=\"num\"><a", sec).group(1)
    check("largest change first", first == "Restaurant/Ausgehen", first)
    check(
        "total row +20",
        re.search(r"Ausgaben gesamt.*?<b>\s*<span class=\"neg\">\+20\.00 €</span>", sec, re.S) is not None,
    )
    # Drilldown: the links reproduce the number
    mm = re.search(r"<td>Lebensmittel</td>\s*<td class=\"num\"><a href=\"([^\"]+)\">", sec)
    lst = c.get(mm.group(1).replace("&amp;", "&")).get_data(as_text=True)
    check("link current -> list with -130", "-130.00" in lst and "-100.00" not in lst, mm.group(1))
    mm = re.search(
        r"<td>Lebensmittel</td>\s*<td class=\"num\"><a[^>]*>[^<]*</a></td>\s*<td class=\"num mute\"><a href=\"([^\"]+)\">",
        sec,
    )
    lst = c.get(mm.group(1).replace("&amp;", "&")).get_data(as_text=True)
    check("link previous -> list with -100", "-100.00" in lst and "-130.00" not in lst, mm.group(1))
    # Oldest month: nothing before it
    h = html("/?month=2025-01")
    check("oldest month: no comparison", "vs. " not in h)
    # July against June: June has no bookings -> no comparison with an empty month
    h = html("/?month=2026-07")
    check("previous month without bookings: no comparison", "vs. " not in h)
    # Year: the same months of the previous year (all present)
    h = html("/?range=2026")
    prev_label = m.period_label(["2025-01", "2025-02", "2025-03", "2025-04", "2025-07", "2025-08"])
    # August 2026 ends on the 20th (last booking) -> the previous year also only up to August 20
    check(
        "year against the same months of the previous year, up to the cutoff day",
        re.search(VS + re.escape(prev_label) + r" \(bis 20\.\)</a>", h) is not None,
        h[h.find("vs. ") :][:160],
    )
    check("year delta expenses 430-220=+210", re.search(r"\+210\.00" + VS + re.escape(prev_label), h) is not None)
    check("no more note about a shorter comparison period", "hat nur" not in h)
    h = html("/?range=all")
    check("all: no comparison", "vs. " not in h)
    # The real-world case: July-August against May-June (empty) -> NO comparison instead of one with half a period
    h = html("/?from=2026-07&to=2026-08")
    check("range, previous period incomplete: no comparison", "vs. " not in h)
    # April-July (calendar span 4 months): the 4 before = Dec-Mar, Dec missing -> no comparison
    h = html("/?from=2026-04&to=2026-07")
    check("calendar span counts, not months with bookings: no comparison", "vs. " not in h)
    # March-April against Jan-Feb: complete -> comparison
    h = html("/?from=2026-03&to=2026-04")
    jf = m.period_label(["2026-01", "2026-02"])
    check("2 months against the 2 before", f">{jf}</a>" in h, h[h.find("vs. ") :][:160])
    check("delta expenses 80-30=+50", re.search(r"\+50\.00" + VS + re.escape(jf), h) is not None)
    check(
        "period picker: one dropdown with 'Eigener Zeitraum', from/to hidden",
        'value="custom" selected>Eigener Zeitraum' in h
        and '<span class="pp-custom">' in h
        and '<option value="2026-03" selected>' in h,
    )
    check(
        "period picker with a range choice: from/to hidden and disabled",
        re.search(r'<span class="pp-custom" hidden>', html("/?month=2026-08")) is not None
        and 'name="from" id="dash-from" disabled' in html("/?month=2026-08"),
    )
    check(
        "no inline display on the from/to span (it would override hidden)",
        'class="pp-custom" hidden style' not in html("/?month=2026-08"),
    )
    hq = html("/?range=last3")
    check(
        "dashboard: quick picks 1M/3M/6M/1Y/all, 3M active",
        all(f'href="/?range={k}"' in hq for k in ("last1", "last3", "last6", "last12", "all"))
        and re.search(r'href="/\?range=last3" class="on"', hq) is not None,
    )
    # The selection keeps what the user chose (bug 2026-10-14: last3 -> "Eigener Zeitraum", all -> year)
    for rng in ("last3", "last6", "all", "2026", "2026-08"):
        hh = html("/?range=" + rng)
        check(
            f"dropdown shows range={rng} as selected",
            re.findall(r'<option value="([^"]*)" selected', hh)[1:2] == [rng] and 'value="custom" selected' not in hh,
            re.findall(r'<option value="([^"]*)" selected', hh),
        )
    hh = html("/investitionen?range=last1")
    check(
        "investments: last1 (quick pick) gets its own option with a label",
        re.search(r'<option value="last1" selected>[^<]+</option>', hh) is not None,
    )
    check(
        "investments: page header without a surplus </div>",
        hh.count("<div") == hh.count("</div>"),
        (hh.count("<div"), hh.count("</div>")),
    )
    # The previous-period link leads to the dashboard of the previous period
    h = html("/?month=2026-08")
    mm = re.search(r'<a href="/\?([^"]+)"[^>]*>Juli 2026 \(bis 20\.\)</a>', h)
    check("previous period linked", mm and mm.group(1) == "month=2026-07", mm.group(1) if mm else None)
    mm = re.search(r'<a href="/\?([^"]+)"[^>]*>' + re.escape(jf) + "</a>", html("/?from=2026-03&to=2026-04"))
    check(
        "range previous period linked",
        mm and mm.group(1).replace("&amp;", "&") == "from=2026-01&to=2026-02",
        mm.group(1) if mm else None,
    )

    # Partial month: the previous period only counts up to the same day (user
    # 2026-10-14: compare expenses with the previous month only up to this day)
    tmp, c = fresh_db()
    con = m.db()
    kon2 = con.execute("SELECT id FROM categories WHERE name='Konsum'").fetchone()["id"]
    leb2 = con.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()["id"]
    for d, a in (
        ("2026-06-01", -10),
        ("2026-06-10", -20),
        ("2026-06-12", -5),
        ("2026-06-20", -30),
        ("2026-06-30", -40),
        ("2026-07-02", -15),
        ("2026-07-12", -25),
    ):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank',?,'Shop','',?,'EUR',?,'active',?)",
            (d, a, "cut" + d, kon2),
        )
    # Rent twice in May (May 2 -> slot May, May 30 -> slot June): the smoothed installment of May 30 belongs
    # in the June comparison although its booking day (30th) lies after the cutoff day (12th)
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,100,'')",
        (leb2,),
    )
    for d in ("2026-05-02", "2026-05-30"):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank',?,'Hausverwaltung Muster','Miete',-500,'EUR',?,'active',?)",
            (d, "miete" + d, leb2),
        )
    con.commit()
    con.close()
    h = html("/?month=2026-07")
    check(
        "July: expenses 15+25", re.search(r'Ausgaben Juli 2026</span>.*?<b class="neg">40\.00 €', h, re.S) is not None
    )
    check("previous period label 'Juni 2026 (bis 12.)'", "Juni 2026 (bis 12.)" in h)
    # June up to the 12th: 10+20+5 = 35 plus smoothed rent in the June slot 500 = 535 -> delta 40-535 = -495
    check(
        "expense delta against June only up to the 12th: -495.00 (rent stays in the slot)",
        re.search(r"-495\.00 € vs\. <a[^>]*>Juni 2026 \(bis 12\.\)</a>", h) is not None,
        re.findall(r"[+\-][\d.]+ € vs\. <a[^>]*>[^<]*</a>", h),
    )
    check("small note next to the period", '<small class="partial-note"' in h and "Daten bis 12.07." in h)
    h = html("/?month=2026-06")
    check(
        "June as full previous month: no '(bis', delta 605-500 against May",
        "(bis " not in h and re.search(r"\+105\.00 € vs\. <a[^>]*>Mai 2026</a>", h) is not None,
        re.findall(r"[+\-][\d.]+ € vs\. <a[^>]*>[^<]*</a>", h),
    )

    # ---------------------------------------------------------------- 4) import result as a card
    print("4) Import result as a card")
    from _import_common import err, file_rows, fr, splitwise_csv, steps

    tmp, c = fresh_db()
    h = upload(
        c,
        (
            george_bank(
                [("01.06.2026", "Billa", "Einkauf", "-20,00"), ("02.06.2026", "Fremdwas", "Einkauf", "-30,00")]
            ),
            "konto.csv",
        ),
        (b"foo;bar\n1;2\n", "kaputt.csv"),
    )
    check("card present", "Import-Ergebnis" in h)
    rows = file_rows(h)
    check("two file rows", len(rows) == 2, rows)
    check("konto.csv: bank 2/0/0", fr(h, "konto.csv") == ("bank", 2, 0, 0), rows)
    check("kaputt.csv with error text", "Format nicht erkannt" in err(h, "kaputt.csv"), rows)
    check("tile new bookings = 2", re.search(r"Neue Buchungen</span><b>2</b>", h) is not None)
    check(
        "tile to review = 1 (Fremdwas) with link",
        re.search(r"Zum Durchgehen</span>\s*<b>1</b><a href=\"/review\">", h) is not None,
    )
    st = steps(h)
    check("step rules = 1 (Billa)", st.get("rules") == 1, st)
    check("no more flash lines for results", 'class="flash' not in h)
    check(
        "text with bold number",
        re.search(r'<li data-step="rules" data-n="1"[^>]*><b[^>]*>1</b>Zahlung per Regel kategorisiert', h) is not None,
    )
    h2 = html("/import")
    check("card only once (gone on the next call)", "Import-Ergebnis" not in h2 and "Import-Historie" in h2)
    # Nothing new
    h = upload(c, (george_bank([("01.06.2026", "Billa", "Einkauf", "-20,00")]), "konto.csv"))
    check("re-import: 'Nichts Neues'", "Nichts Neues" in h and fr(h, "konto.csv") == ("bank", 0, 1, 0))
    # Singular/plural of the steps
    h = upload(
        c,
        (
            george_bank([("03.06.2026", "Billa", "Einkauf", "-1,00"), ("04.06.2026", "Billa", "Einkauf", "-2,00")]),
            "k2.csv",
        ),
    )
    check("plural", "2</b>Zahlungen per Regel kategorisiert" in h)

    # ---------------------------------------------------------------- 5) close a Splitwise group
    print("5) Close a Splitwise group")
    tmp, c = fresh_db()
    SW_HEAD = "Datum,Beschreibung,Kategorien,Kosten,Währung,Anna Muster,Bob"

    def sw(rows):
        return splitwise_csv(SW_HEAD, rows)

    upload(c, (george_bank([("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50")]), "start.csv"))
    # Group "urlaub23": Bob paid 40 (my share 20), I paid back 20 -> balance 0
    upload(
        c,
        (
            sw(["2026-05-10,essen,Essen,40.00,EUR,-20.00,20.00", "2026-05-20,Zahlung,Zahlung,20.00,EUR,20.00,-20.00"]),
            "urlaub23_2026-05-31.csv",
        ),
    )
    # Group "wg": open balance
    upload(c, (sw(["2026-05-12,bier,Alkohol,10.00,EUR,-5.00,5.00"]), "wg_2026-05-31.csv"))
    h = html("/splitwise")
    check("both groups open", "urlaub23</h2>" in h and "wg</h2>" in h and 'class="closed-group"' not in h)
    check(
        "close button only at balance 0",
        h.count("Gruppe schließen") == 1
        and 'action="/splitwise/urlaub23/close"' in h
        and 'action="/splitwise/wg/close"' not in h,
        h.count("Gruppe schließen"),
    )
    r = c.post("/splitwise/wg/close", follow_redirects=True)
    check(
        "wg with balance: rejected",
        "noch einen Saldo" in r.get_data(as_text=True) and one("SELECT COUNT(*) n FROM closed_groups")["n"] == 0,
    )
    r = c.post("/splitwise/urlaub23/close", follow_redirects=True)
    h = r.get_data(as_text=True)
    check("urlaub23 closed", "geschlossen" in h and one("SELECT partner FROM closed_groups")["partner"] == "urlaub23")
    check(
        "listed at the bottom under Geschlossen, no longer at the top",
        "Geschlossen</h2>" in h and 'class="closed-group" data-group="urlaub23"' in h and "urlaub23</h2>" not in h,
    )
    check("booking still counts (dashboard May: 0.50 bank + 20 essen + 5 bier)", "25.50 €" in html("/?month=2026-05"))
    tid = one("SELECT id FROM transactions WHERE description='essen'")["id"]
    check(
        "origin stays (partner = group)",
        one("SELECT partner FROM transactions WHERE id=?", tid)["partner"] == "urlaub23",
    )
    # Re-import without movement: stays closed
    h = upload(
        c,
        (
            sw(["2026-05-10,essen,Essen,40.00,EUR,-20.00,20.00", "2026-05-20,Zahlung,Zahlung,20.00,EUR,20.00,-20.00"]),
            "urlaub23_2026-05-31.csv",
        ),
    )
    check(
        "re-import without movement: stays closed",
        one("SELECT COUNT(*) n FROM closed_groups")["n"] == 1 and "wieder geöffnet" not in h,
    )
    # New position -> open again + note
    h = upload(
        c,
        (
            sw(
                [
                    "2026-05-10,essen,Essen,40.00,EUR,-20.00,20.00",
                    "2026-05-20,Zahlung,Zahlung,20.00,EUR,20.00,-20.00",
                    "2026-06-01,taxi,Transport,10.00,EUR,-5.00,5.00",
                ]
            ),
            "urlaub23_2026-06-30.csv",
        ),
    )
    check(
        "new movement: automatically reopened",
        one("SELECT COUNT(*) n FROM closed_groups")["n"] == 0 and "wieder geöffnet" in h and "urlaub23" in h,
    )
    check("balance now -5: no close button", 'action="/splitwise/urlaub23/close"' not in html("/splitwise"))
    c.post("/splitwise/urlaub23/close", follow_redirects=True)  # balance -5 -> rejected
    check("stays open at balance != 0", one("SELECT COUNT(*) n FROM closed_groups")["n"] == 0)
    upload(
        c,
        (
            sw(
                [
                    "2026-05-10,essen,Essen,40.00,EUR,-20.00,20.00",
                    "2026-05-20,Zahlung,Zahlung,20.00,EUR,20.00,-20.00",
                    "2026-06-01,taxi,Transport,10.00,EUR,-5.00,5.00",
                    "2026-06-02,Zahlung,Zahlung,5.00,EUR,5.00,-5.00",
                ]
            ),
            "urlaub23_2026-06-30.csv",
        ),
    )
    c.post("/splitwise/urlaub23/close", follow_redirects=True)
    check("closable again after settlement", one("SELECT COUNT(*) n FROM closed_groups")["n"] == 1)
    r = c.post("/splitwise/urlaub23/close", data={"undo": "1"}, follow_redirects=True)
    check(
        "reopen by hand",
        one("SELECT COUNT(*) n FROM closed_groups")["n"] == 0 and "wieder geöffnet" in r.get_data(as_text=True),
    )
    check(
        "unknown group: error message instead of 500",
        c.post("/splitwise/gibtsnicht/close", follow_redirects=True).status_code == 200,
    )

    # ---------------------------------------------------------------- 6) categories freely deletable
    print("6) Categories freely deletable")
    tmp, c = fresh_db()
    con = m.db()

    def cid3(name):
        r = one("SELECT id FROM categories WHERE name=?", name)
        return r["id"] if r else None

    vers = cid3("Versicherung")
    check(
        "default category Versicherung with default rules present",
        vers is not None and con.execute("SELECT COUNT(*) FROM rules WHERE category_id=?", (vers,)).fetchone()[0] >= 3,
    )
    # A payment, a split and a fixed cost depend on it
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
        "VALUES('bank','2026-06-01','UNIQA','Polizze',-30,'EUR','v1','active',?)",
        (vers,),
    )
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id,note) "
        "VALUES('bank','2026-06-02','Mix','x',-50,'EUR','v2','active',NULL,'[aufgeteilt]')"
    )
    mix = con.execute("SELECT id FROM transactions WHERE raw_hash='v2'").fetchone()["id"]
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,-20)", (mix, vers))
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,-30)", (mix, cid3("Lebensmittel")))
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('UNIQA',?,100,'')", (vers,)
    )
    con.commit()
    con.close()
    h = html("/categories")
    check("delete button visible despite usage", re.search(r'name="delete" value="%d"' % vers, h) is not None)
    cf = re.search(r'data-confirm="([^"]*Versicherung[^"]*)"', h)
    cf = _unescape(cf.group(1)) if cf else ""
    check(
        "confirmation names rules and payments",
        "4 Regeln werden mitgelöscht" in cf and "2 Zahlungen werden wieder unkategorisiert" in cf,
        cf,
    )
    r = c.post("/categories", data={"delete": vers}, follow_redirects=True)
    txt = r.get_data(as_text=True)
    check("deleted", one("SELECT COUNT(*) n FROM categories WHERE name='Versicherung'")["n"] == 0)
    check(
        "flash names rules, payments, split",
        "Regeln mitgelöscht" in txt and "1 Zahlung wieder unkategorisiert" in txt and "1 Aufteilung aufgehoben" in txt,
        txt[txt.find("Kategorie Versicherung") :][:200],
    )
    check("rules gone", one("SELECT COUNT(*) n FROM rules WHERE category_id=?", vers)["n"] == 0)
    check(
        "payment uncategorized (review)",
        one("SELECT category_id c FROM transactions WHERE raw_hash='v1'")["c"] is None,
    )
    check(
        "split removed, payment open again",
        one("SELECT COUNT(*) n FROM splits WHERE tx_id=?", mix)["n"] == 0
        and "[aufgeteilt]" not in (one("SELECT note FROM transactions WHERE id=?", mix)["note"] or ""),
    )
    check(
        "fixed cost entry remains, without category",
        one("SELECT category_id c FROM fixed_costs WHERE pattern='UNIQA'")["c"] is None,
    )
    check(
        "restart does NOT bring it back",
        (m.init_db() or True) and one("SELECT COUNT(*) n FROM categories WHERE name='Versicherung'")["n"] == 0,
    )
    check(
        "settings/fixed costs load without the category",
        c.get("/einstellungen").status_code == 200 and c.get("/fixkosten").status_code == 200,
    )
    check(
        "system category still protected",
        "wird vom System benötigt"
        in c.post(
            "/categories",
            data={"delete": one("SELECT id FROM categories WHERE name='PayPal-Verrechnung'")["id"]},
            follow_redirects=True,
        ).get_data(as_text=True),
    )
    check(
        "import afterwards without errors",
        "Import-Ergebnis" in upload(c, (english_bank([("2026-06-05", "-5.00", "Hofer", "Brot")]), "x.csv")),
    )

    # ---------------------------------------------------------------- 7) Sankey: largest branch on top, children follow
    print("7) Sankey order")
    tmp, c = fresh_db()
    con = m.db()
    leb, geh = cid3("Lebensmittel"), cid3("Gehalt")

    def tx2(d, partner, amount, cat):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank',?,?,?,?,'EUR',?,'active',?)",
            (d, partner, "", amount, f"{d}{partner}{amount}", cat),
        )

    tx2("2026-07-05", "AG", 3000, geh)
    tx2("2026-07-06", "Billa", -500, leb)  # credit 2500 > expenses 500
    tx2("2026-08-05", "AG", 1000, geh)
    tx2("2026-08-06", "Billa", -900, leb)  # expenses 900 > credit 100
    con.commit()
    con.close()

    def sankey(month):
        hh = html(f"/?month={month}")
        return json.loads(re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', hh, re.S).group(1))

    def col_order(sn, col):
        return [l.split(" (")[0] for l, cc in zip(sn["labels"], sn["node_col"]) if cc == col]

    sn = sankey("2026-07")
    check("July: Guthaben on top in column 1", col_order(sn, 1)[0] == "Guthaben", col_order(sn, 1))
    check("July: Saldo before the expense categories in column 3", col_order(sn, 3)[0] == "Saldo", col_order(sn, 3))
    sn = sankey("2026-08")
    check("August: Ausgaben on top", col_order(sn, 1)[0] == "Ausgaben", col_order(sn, 1))
    check("August: Saldo AFTER the expense categories", col_order(sn, 3)[-1] == "Saldo", col_order(sn, 3))
    check(
        "Saldo ends in column 3 like the categories",
        all(cc == 3 for l, cc in zip(sn["labels"], sn["node_col"]) if l.startswith("Saldo")),
    )

    # Conservation: sum in == sum out at every intermediate node
    def conserved(sn):
        n = len(sn["labels"])
        inn = [0.0] * n
        out = [0.0] * n
        for s_, t_, v in zip(sn["source"], sn["target"], sn["value"]):
            out[s_] += v
            inn[t_] += v
        return all(abs(inn[i] - out[i]) < 0.02 for i in range(n) if inn[i] and out[i])

    check("conservation July/August", conserved(sankey("2026-07")) and conserved(sankey("2026-08")))
    # Spacer of the credit branch in column 2: keeps the order, unlabeled, not clickable
    sn7 = sankey("2026-07")
    c2 = [x.strip() for x in col_order(sn7, 2)]
    check(
        "July: Guthaben spacer on top in column 2, before Nebenkosten", c2[0] == "Guthaben" and "Nebenkosten" in c2, c2
    )
    check(
        "spacer marked (label off, no filter)",
        sn7["spacers"] and sn7["labels"][sn7["spacers"][0]] == "Guthaben " and sn7["filter"][sn7["spacers"][0]] is None,
        sn7["spacers"],
    )
    c2b = [x.strip() for x in col_order(sn, 2)]
    check("August: spacer below Nebenkosten", c2b[0] == "Nebenkosten" and c2b[-1] == "Guthaben", c2b)
    check("spacer: in == out", conserved(sn7))

    # ---------------------------------------------------------------- 8) /jahr: only subcategories expandable
    print("8) /jahr only subcategories")
    con = m.db()
    urlaub = cid3("Urlaub")
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Bio','#111111','expense',?)", (leb,))
    con.commit()
    bio = cid3("Bio")
    tx2("2026-07-07", "Reisebuero", -300, urlaub)
    tx2("2026-07-08", "Denns", -40, bio)
    con.commit()
    con.close()
    h = html("/jahr?y=2026")
    a_ = h.index("Was kostet mich wieviel")
    sec = h[a_ : h.index("Einnahmen 2026</h2>", a_)]
    rows_ = re.findall(r"<tr[^>]*>(.*?)</tr>", sec, re.S)

    def row_of(name):
        return next((r for r in rows_ if f">{name}</a>" in r), "")

    check(
        "Urlaub without subcategories: no expand button",
        "expand-space" in row_of("Urlaub") and "tgl" not in row_of("Urlaub"),
    )
    check("no more payee rows", "Reisebuero" not in sec and "q=" not in sec)
    check("Lebensmittel with subcategory: expandable", 'class="ghost tgl"' in row_of("Lebensmittel"))
    check(
        "subcategory Bio as child",
        re.search(r'<tr class="sub-\d+" hidden>\s*<td[^>]*>[^<]*<a[^>]*>\s*<i[^>]*></i>Bio</a>', sec) is not None
        and "ohne Unterkategorie" in sec,
    )

    # ---------------------------------------------------------------- 9) recurring costs: series via rules
    print("9) Recurring costs: a rule holds a series together")
    tmp, c = fresh_db()
    con = m.db()
    m.set_setting(con, "fixkosten_privat_cats", '["Abos"]')
    abos = cid3("Abos")
    con.execute("INSERT INTO rules(pattern,category_id) VALUES('blizzard',?)", (abos,))
    from datetime import date as _d
    from datetime import timedelta as _td

    def ago(n):
        return (_d.today() - _td(days=n)).isoformat()

    rows9 = [
        ("paypal", 150, "Blizzard Entertainment Ireland L", "Zahlung", -12.99),
        ("paypal", 120, "Blizzard Entertainment Ireland L", "Zahlung", -12.99),
        ("paypal", 90, "Blizzard Entertainment Ireland L", "Zahlung", -12.99),
        ("bank", 60, "GLOBAL COLLECT BV", "BLIZZARD EU339352308", -14.99),  # price up, different processor
        ("bank", 30, "GLOBAL COLLECT BV", "BLIZZARD EU339352309", -14.99),
        ("paypal", 5, "Netflix.com", "Zahlung", -7.99),
    ]
    for i, (src, n, p, desc, a) in enumerate(rows9):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES(?,?,?,?,?,'EUR',?,'active',?)",
            (src, ago(n), p, desc, a, f"r9{i}", abos),
        )
    con.commit()
    series9 = m.detect_recurring(con)
    bl = [s for s in series9 if s["rule"] == "blizzard"]
    check(
        "one series via the rule 'blizzard' (5 payments, 2 processors, 2 prices)",
        len(bl) == 1 and bl[0]["n"] == 5,
        [(s["partner"], s["n"], s["rule"]) for s in series9],
    )
    check("drilldown term = rule pattern", bl and bl[0]["match"] == ["blizzard"], bl and bl[0]["match"])
    check("name = most frequent payee", bl and bl[0]["partner"].startswith("Blizzard"))
    check("amount/month = last payment 14.99", bl and abs(bl[0]["monthly"] - 14.99) < 0.01, bl and bl[0]["monthly"])
    hf = html("/fixkosten")
    check(
        "row links to q=blizzard, not bold",
        'href="/transactions?q=blizzard" class="series-link"' in hf
        and re.search(r'class="series-link"[^>]*>Blizzard[^<]*</a>', hf) is not None
        and re.search(r"<b>Blizzard", hf) is None,
    )
    check("no 'zahlt jetzt ueber' text", "zahlt jetzt über" not in hf)
    check("rule as tooltip", "(Regel: blizzard)" in hf)
    check(
        "both tables same columns (colgroup)",
        hf.count('<table class="series"><colgroup>') == 1
        or hf.count("<colgroup>") == hf.count('<table class="series">'),
        hf.count("<colgroup>"),
    )
    check("total bold", re.search(r"<b>Summe Privat</b>", hf) is not None)
    lst = c.get("/transactions?q=blizzard").get_data(as_text=True)
    check(
        "drilldown shows all 5",
        "(5 zählende Zeilen" in lst and "BLIZZARD EU339352309" in lst and "Blizzard Entertainment" in lst,
        re.findall(r"\(\d+ zählende Zeilen", lst),
    )
    # Broad rule: subscription + single purchases from the same vendor -> the group fails
    # the rhythm test, then the series must survive via the payee
    con.execute("INSERT INTO rules(pattern,category_id) VALUES('anthropic',?)", (abos,))
    rows9b = [
        ("card", 130, "ANTHROPIC* CLAUDE SUB", "E-COMM", -99.64),
        ("card", 100, "ANTHROPIC* CLAUDE SUB", "E-COMM", -108.0),
        ("card", 70, "ANTHROPIC* CLAUDE SUB", "E-COMM", -108.0),
        ("card", 40, "ANTHROPIC* CLAUDE SUB", "E-COMM", -108.0),
        ("card", 101, "ANTHROPIC IRELAND", "E-COMM API", -24.0),
        ("card", 101, "ANTHROPIC IRELAND", "E-COMM API", -24.0),
        ("card", 100, "ANTHROPIC IRELAND", "E-COMM API", -24.0),
        ("card", 100, "ANTHROPIC IRELAND", "E-COMM API", -24.0),
        ("card", 99, "ANTHROPIC IRELAND", "E-COMM API", -24.0),
    ]
    for i, (src, n, p_, desc, a) in enumerate(rows9b):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES(?,?,?,?,?,'EUR',?,'active',?)",
            (src, ago(n), p_, desc, a, f"r9b{i}", abos),
        )
    con.commit()
    an = [s for s in m.detect_recurring(con) if "ANTHROPIC" in s["partner"]]
    check(
        "broad rule: subscription series survives via the payee (4 payments, without rule key)",
        len(an) == 1 and an[0]["n"] == 4 and an[0]["rule"] is None and an[0]["partner"] == "ANTHROPIC* CLAUDE SUB",
        [(s["partner"], s["n"], s["rule"]) for s in an],
    )
    con.execute("DELETE FROM rules WHERE pattern='blizzard'")
    con.commit()
    series9b = m.detect_recurring(con)
    bl2 = [s for s in series9b if s["partner"].startswith("Blizzard")]
    check(
        "without rule: only the 3 PayPal payments as a series (price/channel change too big for the heuristic)",
        len(bl2) == 1 and bl2[0]["n"] == 3 and bl2[0]["rule"] is None,
        [(s["partner"], s["n"]) for s in series9b],
    )
    con.close()

    # ---------------------------------------------------------------- 10) rule dialog in all payments
    print("10) Rule from all payments with a pattern")
    tmp, c = fresh_db()
    con = m.db()
    leb = cid3("Lebensmittel")
    for i, p in enumerate(("Gusto Lab 05", "Gusto Lab 07", "GUSTO LAB 12", "Anderes")):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status) "
            "VALUES('bank',?,?,'',-9,'EUR',?,'active')",
            (f"2026-06-0{i + 1}", p, f"g{i}"),
        )
    con.commit()
    con.close()
    h = html("/transactions")
    check(
        "suggestion without branch number in the select",
        'data-rule="Gusto Lab"' in h,
        re.findall(r'data-rule="[^"]*"', h)[:4],
    )
    check("hidden pattern field + rule checkbox", 'name="rule_pattern"' in h and 'name="make_rule"' in h)
    tjs = open(os.path.join(ROOT, "static", "js", "transactions.js"), encoding="utf-8").read()
    check(
        "prompt dialog in the JS, already when ticking",
        "askRulePattern" in tjs and "kein * nötig" in tjs and "input[name=make_rule]').forEach" in tjs,
    )
    tid = one("SELECT id FROM transactions WHERE partner='Gusto Lab 05'")["id"]
    r = c.post(
        f"/tx/{tid}/update",
        data={"category_id": leb, "make_rule": "1", "rule_pattern": " *gusto lab* "},
        follow_redirects=True,
        headers={"Referer": "http://localhost/transactions"},
    )
    txt = r.get_data(as_text=True)
    check(
        "rule stored normalized (without asterisks)",
        one("SELECT pattern FROM rules WHERE pattern='gusto lab'") is not None,
        q("SELECT pattern FROM rules ORDER BY id DESC LIMIT 3"),
    )
    check(
        "all three Gusto Lab rows categorized, 'Anderes' not",
        count("category_id=? AND partner LIKE '%usto%'", leb) == 3
        and one("SELECT category_id c FROM transactions WHERE partner='Anderes'")["c"] is None,
    )
    check(
        "flash names pattern and matches",
        "Regel angelegt: gusto lab" in txt and "2 weitere offene Zahlungen" in txt,
        txt[txt.find("Regel angelegt") :][:120],
    )
    tid2 = one("SELECT id FROM transactions WHERE partner='Anderes'")["id"]
    c.post(f"/tx/{tid2}/update", data={"category_id": leb, "make_rule": "1"}, follow_redirects=True)
    check("without pattern: full partner name", one("SELECT pattern FROM rules WHERE pattern='Anderes'") is not None)
    # Rule on a subcategory
    con = m.db()
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Bio','#111111','expense',?)", (leb,))
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status) "
        "VALUES('bank','2026-06-09','Denns Biomarkt','',-5,'EUR','g9','active')"
    )
    con.commit()
    con.close()
    bio = cid3("Bio")
    r = c.post("/rules", data={"pattern": "denns", "category_id": bio}, follow_redirects=True)
    check(
        "rule directly on a subcategory (default rule DENNS is moved over)",
        one("SELECT category_id c FROM rules WHERE lower(pattern)='denns'")["c"] == bio
        and one("SELECT category_id c FROM transactions WHERE partner='Denns Biomarkt'")["c"] == bio,
    )
    check(
        "subcategory selectable in the rule dropdown",
        re.search(r'<optgroup label="Lebensmittel">\s*<option value="%d"' % bio, html("/rules")) is not None,
    )

    # ---------------------------------------------------------------- 11) export
    print("11) Export")
    r = c.get("/export.csv")
    cd = r.headers.get("Content-Disposition", "")
    check("file name with date", re.fullmatch(r"attachment; filename=moneyflow_export_\d{6}\.csv", cd) is not None, cd)
    body = r.get_data(as_text=True)
    check(
        "header row + all bookings",
        body.startswith("﻿Datum;Quelle;Partner;Beschreibung;Betrag;Effektiv;Kategorie;Status") and body.count("\n") >= 6,
        body[:120],
    )
    n_tx = count()
    h = upload(c, (r.get_data(), "moneyflow_export_260915.csv"))
    check(
        "export cannot be re-imported as a bank statement (clear message)",
        "funkSTONKS-Export" in err(h, "moneyflow_export") and count() == n_tx,
        (err(h, "moneyflow_export"), count() - n_tx),
    )

    # ---------------------------------------------------------------- 12) categories: + instead of a dummy row
    print("12) Categories: + instead of a dummy row")
    h = html("/categories")
    check(
        "no visible dummy rows",
        re.search(r'<tr class="cat-add-row" id="add-\d+" hidden', h) is not None and "+ Unterkategorie von" not in h,
    )
    check(
        "+ button per top-level category",
        h.count('class="ghost cat-add-btn"') == len(re.findall(r'<tr class="cat-add-row"', h)),
    )
    r = c.post(
        "/categories",
        data={"add_parent_id": leb, "add_name": "Tiefkuehl", "add_color": "#222222"},
        follow_redirects=True,
    )
    check(
        "creating a subcategory via the row works",
        cid3("Tiefkuehl") is not None and one("SELECT parent_id p FROM categories WHERE name='Tiefkuehl'")["p"] == leb,
    )

    # ---------------------------------------------------------------- 13) own advance: several bookings, later match
    print("13) Advance: split bank booking and later match")
    tmp, c = fresh_db()
    con = m.db()
    barg = cid3("Bargeld")
    SWH = "Datum,Beschreibung,Kategorien,Kosten,Währung,Anna Muster,Bob"

    def bank(d, partner, amount, cat=None, hsh=None):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank',?,?,'',?,'EUR',?,'active',?)",
            (d, partner, amount, hsh or f"{d}{partner}{amount}", cat),
        )

    bank("2026-05-01", "Bank AG", -0.50)  # tracking start
    bank("2026-06-01", "Bankomat", -100.00, barg)  # cash pool
    con.commit()
    con.close()
    # Splitwise first: brush 78.60, I paid, Bob owes 39.30 -> no bank booking yet -> cash pool
    upload(c, (splitwise_csv(SWH, ["2026-06-11,buerste,Allgemein,78.60,EUR,39.30,-39.30"]), "wg_2026-06-30.csv"))
    sw_id = one("SELECT id FROM transactions WHERE description='buerste'")["id"]
    atm = one("SELECT effective_amount e, note FROM transactions WHERE partner='Bankomat'")
    check(
        "without bank booking: cash pool reduced by 39.30",
        atm["e"] is not None and abs(atm["e"] + 60.70) < 0.01,
        dict(atm),
    )
    # Now the bank statement arrives: Amazon in two deliveries, three days later
    h = upload(
        c,
        (
            george_bank(
                [
                    ("14.06.2026", "AMAZON PAYMENTS EUROPE", "AMZN Mktp 1", "-28,24"),
                    ("14.06.2026", "AMAZON PAYMENTS EUROPE", "AMZN Mktp 2", "-50,36"),
                    ("14.06.2026", "Billa", "Einkauf", "-28,24"),
                ]
            ),
            "konto.csv",
        ),
    )  # Billa: same partial amount, different partner - must not interfere
    rows = q(
        "SELECT partner, amount, effective_amount e, note FROM transactions WHERE partner LIKE 'AMAZON%' ORDER BY amount DESC"
    )
    check(
        "both Amazon bookings reduced proportionally (14.12 + 25.18 = 39.30 own share)",
        len(rows) == 2 and abs(rows[0]["e"] + 14.12) < 0.01 and abs(rows[1]["e"] + 25.18) < 0.01,
        [dict(r) for r in rows],
    )
    check(
        "cash pool full again",
        one("SELECT effective_amount e FROM transactions WHERE partner='Bankomat'")["e"] is None,
    )
    check(
        "position is a raw position again (no credit)",
        one("SELECT status, note FROM transactions WHERE id=?", sw_id)["status"] == "info",
    )
    check("two links on the position", one("SELECT COUNT(*) n FROM matches WHERE other_tx=?", sw_id)["n"] == 2)
    check(
        "Billa (same amount) untouched",
        one("SELECT effective_amount e FROM transactions WHERE partner='Billa'")["e"] is None,
    )
    check("import result: 1 advance linked", steps(h).get("linked") == 1, steps(h))
    # Idempotent
    upload(c, (george_bank([("14.06.2026", "AMAZON PAYMENTS EUROPE", "AMZN Mktp 1", "-28,24")]), "konto.csv"))
    check(
        "re-import changes nothing",
        one("SELECT COUNT(*) n FROM matches WHERE other_tx=?", sw_id)["n"] == 2
        and abs(one("SELECT effective_amount e FROM transactions WHERE description='AMZN Mktp 1'")["e"] + 14.12) < 0.01,
    )
    # A manually linked position stays untouched even when an exact match exists
    con = m.db()
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,extra) "
        "VALUES('splitwise','2026-06-20','wg','kino',20,'EUR','swk','info',?)",
        (json.dumps({"cost": 40.0, "bal": 20.0, "members": ["Bob"]}),),
    )
    kino = con.execute("SELECT id FROM transactions WHERE raw_hash='swk'").fetchone()["id"]
    bank("2026-06-20", "Kino", -40.00, hsh="kino40")
    kb = con.execute("SELECT id FROM transactions WHERE raw_hash='kino40'").fetchone()["id"]
    con.execute(
        "UPDATE transactions SET effective_amount=-25, note='manuell mit Splitwise verknüpft (eigener Anteil)' WHERE id=?",
        (kb,),
    )
    con.execute("INSERT INTO matches(bank_tx, other_tx, kind, amount) VALUES(?,?,'splitwise_share',15)", (kb, kino))
    con.commit()
    m.auto_link_own_advances(con)
    con.commit()
    con.close()
    check(
        "manual link stays (effective -25 instead of -20)",
        abs(one("SELECT effective_amount e FROM transactions WHERE id=?", kb)["e"] + 25) < 0.01,
    )

    # Legacy data: a link without amount on a withdrawal that still carries other shares ->
    # unlinking must NOT reset the withdrawal to the full amount
    con = m.db()
    bank("2026-06-25", "Bankomat2", -100.00, barg, hsh="atm2")
    atm2 = con.execute("SELECT id FROM transactions WHERE raw_hash='atm2'").fetchone()["id"]
    con.execute("UPDATE transactions SET effective_amount=0 WHERE id=?", (atm2,))  # fully used up
    for i, h_ in enumerate(("swa", "swb")):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,extra) "
            "VALUES('splitwise','2026-06-26','wg',?,50,'EUR',?,'info',?)",
            (h_, h_, json.dumps({"cost": 100.0, "bal": 50.0, "members": ["Bob"]})),
        )
        sid = con.execute("SELECT id FROM transactions WHERE raw_hash=?", (h_,)).fetchone()["id"]
        con.execute(
            "INSERT INTO matches(bank_tx, other_tx, kind, amount) VALUES(?,?,'splitwise_share',NULL)", (atm2, sid)
        )
    con.commit()
    swa = con.execute("SELECT id FROM transactions WHERE raw_hash='swa'").fetchone()["id"]
    m.unlink_splitwise_position(con, swa)
    con.commit()
    e = con.execute("SELECT effective_amount FROM transactions WHERE id=?", (atm2,)).fetchone()[0]
    check("legacy link without amount + other shares: withdrawal stays as is (no reset to -100)", e == 0, e)
    swb = con.execute("SELECT id FROM transactions WHERE raw_hash='swb'").fetchone()["id"]
    m.unlink_splitwise_position(con, swb)
    con.commit()
    e = con.execute("SELECT effective_amount FROM transactions WHERE id=?", (atm2,)).fetchone()[0]
    check("last share gone: withdrawal full again", e is None, e)
    con.close()

    # ---------------------------------------------------------------- 14) audit 2026-09-15: formats, numbers, texts
    print("14) Audit fixes")
    tmp, c = fresh_db()
    # Sparkasse CAMT: "Betrag" exactly before "Lastschrift Ursprungsbetrag", partner "Beguenstigter/Zahlungspflichtiger"
    camt = (
        "Auftragskonto;Buchungstag;Valutadatum;Buchungstext;Verwendungszweck;Glaeubiger ID;Mandatsreferenz;"
        "Kundenreferenz (End-to-End);Sammlerreferenz;Lastschrift Ursprungsbetrag;Auslagenersatz Ruecklastschrift;"
        "Beguenstigter/Zahlungspflichtiger;Kontonummer/IBAN;BIC (SWIFT-Code);Betrag;Waehrung;Info\n"
        "DE12;03.06.26;03.06.26;KARTENZAHLUNG;REWE SAGT DANKE;;;;;;;REWE Markt GmbH;DE99;ABCD;-23,45;EUR;Umsatz gebucht\n"
    ).encode("cp1252")
    h = upload(c, (camt, "umsaetze-sparkasse.csv"))
    r = one("SELECT partner, amount, booking_date FROM transactions WHERE partner LIKE 'REWE%'")
    check(
        "Sparkasse CAMT: amount/partner recognized correctly",
        fr(h, "sparkasse") == ("bank", 1, 0, 0)
        and r
        and abs(r["amount"] + 23.45) < 0.001
        and r["booking_date"] == "2026-06-03",
        (fr(h, "sparkasse"), r and dict(r)),
    )
    # ING style: preamble before the header row, different separator in the preamble
    ing = (
        "Umsatzanzeige;Datei erstellt am: 15.09.2026\n\nIBAN;DE44 1234\nKontoname;Girokonto\n\n"
        "Buchung;Valuta;Auftraggeber/Empfänger;Buchungstext;Verwendungszweck;Saldo;Währung;Betrag;Währung\n"
        "05.06.2026;05.06.2026;HOFER;Lastschrift;Einkauf;1.000,00;EUR;-12,30;EUR\n"
    ).encode("cp1252")
    h = upload(c, (ing, "Umsatzanzeige.csv"))
    r = one("SELECT partner, amount FROM transactions WHERE partner='HOFER'")
    check(
        "preamble before the header row is skipped (ING style)",
        fr(h, "Umsatzanzeige") == ("bank", 1, 0, 0) and r and abs(r["amount"] + 12.30) < 0.001,
        (fr(h, "Umsatzanzeige"), err(h, "Umsatzanzeige")),
    )
    # Error text in plain words instead of a Python list
    h = upload(c, (b"Foo;Bar;Baz\n1;2;3\n", "x.csv"), force_kind="bank")
    check(
        "header error in plain words",
        "Kopfzeile lautet" in err(h, "x.csv") and "[" not in err(h, "x.csv"),
        err(h, "x.csv"),
    )
    # Card account note
    h = upload(c, (english_bank([("2026-06-07", "-9.99", "Shop", "x")]), "kreditkarte_juni.csv"))
    check("note for a card file read as a bank statement", "Kartenkonto? Format" in h)
    # 'Nichts uebernommen' instead of 'alle Zeilen waren schon da' on error
    h = upload(c, (b"foo;bar\n1;2\n", "kaputt.csv"))
    check("error file: no 'alle Zeilen waren schon da'", "waren schon da" not in h and "nichts übernommen" in h)
    # Rule match after the pipeline: PayPal debit categorized by rule, then netted -> does not count
    con = m.db()
    kon = cid3("Konsum")
    con.execute("INSERT INTO rules(pattern,category_id) VALUES('paypal europe',?)", (kon,))
    con.commit()
    con.close()
    from _import_common import paypal_akt

    h = upload(
        c,
        (george_bank([("10.06.2026", "PayPal Europe S.a.r.l.", "1023456789 PP", "-15,00")]), "pp_bank.csv"),
        (paypal_akt([("10.06.2026", "Steam", "Zahlung", "EUR", "-15,00", "T1")]), "Download.CSV"),
    )
    st14 = steps(h)
    check(
        "rule match does not count when the row was netted afterwards",
        st14.get("paypal") == 1 and not st14.get("rules"),
        st14,
    )
    # Total row without category filter: the transfer part of a split does not count
    con = m.db()
    umb = m.system_cat_id(con, "Umbuchung")
    leb = cid3("Lebensmittel")
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,note) "
        "VALUES('bank','2026-07-01','ATM','',-100,'EUR','sp1','active','[aufgeteilt]')"
    )
    sp = con.execute("SELECT id FROM transactions WHERE raw_hash='sp1'").fetchone()["id"]
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,-70)", (sp, leb))
    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,-30)", (sp, umb))
    con.commit()
    con.close()
    lst = html("/transactions?month=2026-07")
    mm = re.search(r"<b>Summe</b>.*?<b>(-?[\d.]+)</b>", lst, re.S)
    check(
        "balance list: split counts only with its non-transfer part (-70)",
        mm and abs(float(mm.group(1)) + 70.0) < 0.01,
        mm and mm.group(1),
    )
    saldo = re.search(r'Saldo</span>.*?<b class="\w+">\s*(-?[\d.]+) €', html("/?month=2026-07"), re.S)
    check(
        "... and matches the Saldo tile", saldo and abs(float(saldo.group(1)) + 70.0) < 0.01, saldo and saldo.group(1)
    )
    # Comparison: a refund category appears with a minus instead of 0.00
    con = m.db()
    kl = cid3("Kleidung") or leb
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
        "VALUES('bank','2026-08-02','Shop','',-50,'EUR','k1','active',?)",
        (kl,),
    )
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
        "VALUES('bank','2026-08-20','Shop','Retoure',90,'EUR','k2','active',?)",
        (kl,),
    )
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
        "VALUES('bank','2026-07-20','Shop','',-20,'EUR','k0','active',?)",
        (kl,),
    )
    con.commit()
    con.close()
    sec = html("/vergleich?a_month=2026-08&b_month=2026-07")
    klname = one("SELECT name FROM categories WHERE id=?", kl)["name"]
    mm = re.search(r"<td>" + re.escape(klname) + r"</td>\s*<td class=\"num\"><a[^>]*>(-?[\d.]+) €</a>", sec)
    check(
        "comparison shows refund category as -40.00 instead of 0.00",
        mm and abs(float(mm.group(1)) + 40.0) < 0.01,
        mm and mm.group(1),
    )
    # /partner: link via normalized key, transfers not included
    con = m.db()
    for i, p_ in enumerate(("BILLA DANKT 0001", "BILLA DANKT 0002", "BILLA DANKT 0003")):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank','2026-06-1%d',?,'',-10,'EUR',?,'active',?)" % i,
            (p_, f"b{i}", leb),
        )
    for i in range(3):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank','2026-06-2%d','Sparkonto','',-500,'EUR',?,'active',?)" % i,
            (f"u{i}", umb),
        )
    con.commit()
    con.close()
    hp = html("/partner")
    check("/partner: link to all branches (q=billa dankt)", 'href="/transactions?q=billa%20dankt"' in hp)
    check("/partner: transfer bookings (Umbuchung) not listed", "Sparkonto" not in hp)
    lst = html("/transactions?q=billa%20dankt")
    check("... drilldown shows all three", "(3 zählende Zeilen" in lst)
    # Status/source filter
    h = html("/transactions")
    check(
        "status filter: counts / does not count / Splitwise raw data, no matched/pending",
        'value="info"' in h and ">zählt<" in h and "matched" not in h and "pending" not in h,
    )
    # Default rules do not come back after deleting all rules
    con = m.db()
    con.execute("DELETE FROM rules")
    con.commit()
    con.close()
    m.init_db()
    check(
        "all rules deleted: default rules do not come back",
        count("1=1") > 0 and one("SELECT COUNT(*) n FROM rules")["n"] == 0,
    )
    # Umbuchung: deleted stays deleted as long as no settlement payment is netted
    con = m.db()
    uid = one("SELECT id FROM categories WHERE name='Umbuchung'")["id"]
    con.execute("DELETE FROM splits WHERE category_id=?", (uid,))
    con.execute("UPDATE transactions SET category_id=NULL WHERE category_id=?", (uid,))
    con.execute("DELETE FROM categories WHERE id=?", (uid,))
    con.commit()
    con.close()
    upload(c, (english_bank([("2026-06-30", "-1.00", "X", "y")]), "u.csv"))
    check(
        "Umbuchung is not recreated on every import",
        one("SELECT COUNT(*) n FROM categories WHERE name='Umbuchung'")["n"] == 0,
    )
    # Review: digit badges outside the keydown handler
    hr = html("/review")
    rjs = open(os.path.join(ROOT, "static", "js", "review.js"), encoding="utf-8").read()
    check(
        "review: badges on load, not only on keypress",
        "EINMAL beim Laden" in hr or "insertAdjacentHTML" not in rjs.split("addEventListener('keydown'")[-1],
    )
    # Error flash red
    r = c.post("/einstellungen", data={"action": "restore", "backup": "x"}, follow_redirects=True)
    check(
        "error flash with FEHLER prefix (red)",
        'class="flash flash-err"'
        in c.post("/sicherung", data={"action": "restore", "backup": "x"}, follow_redirects=True).get_data(
            as_text=True
        ),
    )

    # ---------------------------------------------------------------- 15) audit fixes II: review, compare, split, rules
    print("15) Audit fixes II")
    tmp, c = fresh_db()
    con = m.db()
    leb, kon, abos = cid3("Lebensmittel"), cid3("Konsum"), cid3("Abos")
    for i in range(5):
        con.execute(
            "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
            "VALUES('bank',?,?,'',-3,'EUR',?,'active',?)",
            (f"2026-06-0{i + 1}", "Trafik", f"t{i}", kon),
        )
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status) "
        "VALUES('bank','2026-06-09','PayPal Europe S.a.r.l. et Cie','1023456 PP .PAYPAL',-20,'EUR','ppc','active')"
    )
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status) "
        "VALUES('bank','2026-06-30','Neu','offen',-7,'EUR','off','active')"
    )
    con.commit()
    con.close()
    h = html("/review")
    first_slot = re.search(r'class="ghost cat-slot[^"]*"[^>]*>\s*(?:<i[^>]*></i>)?([^<]+)', h)
    check(
        "review: most used category (Konsum) on key 1",
        first_slot and first_slot.group(1).strip().startswith("Konsum"),
        first_slot and first_slot.group(1),
    )
    hp = html("/review?tx=%d" % one("SELECT id FROM transactions WHERE raw_hash='ppc'")["id"])
    check(
        "review: PayPal debit gets a note instead of a forced category",
        "PayPal-Abbuchung" in hp and "Überspringen" in hp,
    )
    check(
        "review: normal payment without note",
        "PayPal-Abbuchung" not in html("/review?tx=%d" % one("SELECT id FROM transactions WHERE raw_hash='off'")["id"]),
    )
    check(
        "review: badges on load (in the JS before the keydown handler)",
        (open(os.path.join(ROOT, "static", "js", "review.js"), encoding="utf-8").read()).index("insertAdjacentHTML")
        < (open(os.path.join(ROOT, "static", "js", "review.js"), encoding="utf-8").read()).index(
            "addEventListener('keydown'"
        ),
    )
    # Partial month
    h = html("/?month=2026-06")
    check("no note when the month is complete (last booking on 30.06.)", "unvollständig" not in h)
    con = m.db()
    con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,currency,raw_hash,status,category_id) "
        "VALUES('bank','2026-07-12','X','',-1,'EUR','jul','active',?)",
        (kon,),
    )
    con.commit()
    con.close()
    h = html("/?month=2026-07")
    check(
        "partial month: small note 'Daten bis 12.07.'",
        '<small class="partial-note"' in h and "Daten bis 12.07. – Monat unvollständig" in h,
    )
    # Split editor: a wrong total keeps the inputs
    tid = one("SELECT id FROM transactions WHERE raw_hash='off'")["id"]
    r = c.post(
        f"/tx/{tid}/split",
        data={"cat": [str(leb), str(kon)], "amt": ["-2", "-3"], "back": "/transactions?month=2026-06"},
    )
    hs = r.get_data(as_text=True)
    check(
        "split: error on wrong total, inputs remain",
        r.status_code == 200
        and "FEHLER Summe" in hs
        and 'value="-2.00"' in hs
        and 'value="-3.00"' in hs
        and 'name="back" value="/transactions?month=2026-06"' in hs,
        (r.status_code, re.findall(r'name="amt" value="[^"]*"', hs)),
    )
    check(
        "split: remainder display and save lock in the JS",
        "split-rest" in hs
        and "updateRest()" in open(os.path.join(ROOT, "static", "js", "split.js"), encoding="utf-8").read(),
    )
    check("split: nothing saved", one("SELECT COUNT(*) n FROM splits WHERE tx_id=?", tid)["n"] == 0)
    # /rules: keyword list visible, delete confirmation
    con = m.db()
    m.set_setting(con, "sources", '["bank","splitwise"]')
    con.commit()
    con.close()
    hr = html("/rules")
    check(
        "/rules: built-in Splitwise keyword list listed",
        "Beschreibung eines dieser Wörter enthält" in hr and "miete" in hr,
    )
    check("/rules: deleting a rule asks for confirmation", "löschen?" in hr)
    he = html("/einstellungen")
    check(
        "share card only in the developer checkout",
        ("Weitergabe</h2>" in he) == m.is_dev_checkout(m.APP_DIR),
    )
    check("publish.bat not in the Share folder", "publish.bat" not in m.SHARE_INCLUDE)
    # Setup: note instead of a silent redirect
    tmp2, c2 = fresh_db()
    con2 = m.db()
    con2.execute("DELETE FROM settings WHERE key='setup_done'")
    con2.commit()
    con2.close()
    r = c2.get("/jahr", follow_redirects=True)
    check("before setup: note when redirecting", "Erst die Einrichtung" in r.get_data(as_text=True))
    finish()
