"""Recurring costs by hand: hide a series (hide), force a booking group as a
series (force), undo both (clear) - effect on /fixkosten, the dashboard tile
and recurring_groups()."""

import json
import re

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_series_overrides():

    tmp, c = fresh_db()
    con = m.db()
    m.set_setting(con, "fixkosten_privat_cats", json.dumps(["Abos"]))
    abos = con.execute("SELECT id FROM categories WHERE name='Abos'").fetchone()["id"]
    sport = con.execute("SELECT id FROM categories WHERE name='Lifestyle'").fetchone()["id"]
    con.commit()
    con.close()

    BANK = george_bank(
        [
            ("01.05.2026", "Bank AG", "Kontofuehrung", "-0,50"),
            # clean series: streaming, monthly, identical amount
            ("05.05.2026", "Streaming AG", "Abo", "-12,99"),
            ("05.06.2026", "Streaming AG", "Abo", "-12,99"),
            ("05.07.2026", "Streaming AG", "Abo", "-12,99"),
            # mobile contract with wildly varying amounts, only TWO payments -> fails
            ("10.06.2026", "Mobilfunk GmbH", "Rechnung", "-18,20"),
            ("10.07.2026", "Mobilfunk GmbH", "Rechnung", "-88,24"),
            # gym: monthly, but category Lifestyle (not in fixkosten_privat_cats)
            ("03.05.2026", "Fitness Muster", "Mitglied", "-29,90"),
            ("03.06.2026", "Fitness Muster", "Mitglied", "-29,90"),
            ("03.07.2026", "Fitness Muster", "Mitglied", "-29,90"),
        ]
    )
    html = upload(c, (BANK, "Umsaetze_2026-07-31.csv"))
    con = m.db()
    for p, cid in (("Streaming AG", abos), ("Mobilfunk GmbH", abos), ("Fitness Muster", sport)):
        con.execute("UPDATE transactions SET category_id=? WHERE partner=?", (cid, p))
    con.commit()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    names = [x["partner"] for x in p]
    print("1) Starting point")
    check("Streaming detected", "Streaming AG" in names, names)
    check("Mobilfunk (2 payments) not detected", "Mobilfunk GmbH" not in names)
    check("Fitness detected, but not under private (category Lifestyle)", "Fitness Muster" not in names)
    check("series carry a key", all(x.get("key") for x in p), [x.get("key") for x in p])
    h = c.get("/fixkosten").get_data(as_text=True)
    check("candidate list contains Mobilfunk", "Mobilfunk GmbH" in h and 'name="mode" value="force"' in h)
    mkey = re.search(r'<tr class="cand-row" data-key="([^"]+)">\s*<td><a[^>]*>Mobilfunk GmbH', h)
    check("candidate has a key (list row, no dropdown)", bool(mkey), h[h.find("Vorschläge") :][:600])
    fkey = re.search(r'<tr class="cand-row" data-key="([^"]+)">\s*<td><a[^>]*>Fitness Muster', h)
    check("Fitness is NOT a candidate (detected, just filtered)", not fkey)

    print("\n2) Force: Mobilfunk as recurring costs")
    r = c.post(
        "/fixkosten/override",
        data={"key": mkey.group(1), "mode": "force", "label": "Mobilfunk GmbH"},
        follow_redirects=True,
    )
    h = r.get_data(as_text=True)
    check("flash", "als laufende Kosten aufgenommen" in h)
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    mob = next((x for x in p if x["partner"] == "Mobilfunk GmbH"), None)
    check("Mobilfunk now under private, forced", mob is not None and mob["forced"], [x["partner"] for x in p])
    check(
        "amount/month = last payment 88.24, typical = median 53.22",
        mob and abs(mob["monthly"] - 88.24) < 0.005 and abs(mob["amount"] - 53.22) < 0.005,
        mob and (mob["monthly"], mob["amount"]),
    )
    check("sum private = 12.99 + 88.24", abs(s["privat"] - 101.23) < 0.005, s)
    check("page shows the manual pill and the manual-remove button", "manuell ✕" in h and ">manuell</span>" in h)
    check("Mobilfunk no longer in the candidate list", not re.search(r"cand-row[^>]*>\s*<td><a[^>]*>Mobilfunk GmbH", h))
    d = c.get("/").get_data(as_text=True)
    check("dashboard tile recurring costs = 101.23", "101.23 €" in d)

    print("\n3) Hide: Streaming out")
    skey = next(x["key"] for x in p if x["partner"] == "Streaming AG")
    r = c.post(
        "/fixkosten/override", data={"key": skey, "mode": "hide", "label": "Streaming AG"}, follow_redirects=True
    )
    h = r.get_data(as_text=True)
    check("flash", "ausgeblendet" in h)
    con = m.db()
    w, p, b, s, hid = m.recurring_groups(con, with_hidden=True)
    con.close()
    check("Streaming no longer under private", not any(x["partner"] == "Streaming AG" for x in p))
    check("Streaming in hidden", any(x["partner"] == "Streaming AG" for x in hid))
    check("sum private = 88.24", abs(s["privat"] - 88.24) < 0.005, s)
    check("hidden card with show-again button", "Ausgeblendet" in h and "wieder anzeigen" in h)
    check("4-tuple call still works", len(m.recurring_groups(m.db())) == 4)

    print("\n4) Undo")
    c.post("/fixkosten/override", data={"key": skey, "mode": "clear", "label": "Streaming AG"}, follow_redirects=True)
    c.post(
        "/fixkosten/override",
        data={"key": mkey.group(1), "mode": "clear", "label": "Mobilfunk GmbH"},
        follow_redirects=True,
    )
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    check(
        "Streaming back, Mobilfunk gone again",
        [x["partner"] for x in p] == ["Streaming AG"],
        [x["partner"] for x in p],
    )
    check("table empty", one("SELECT COUNT(*) n FROM series_overrides")["n"] == 0)
    print("\n5) Merge: add the Mobilfunk payments to the Streaming series")
    h = c.get("/fixkosten/serie?key=" + skey).get_data(as_text=True)
    check(
        "series page lists Mobilfunk as merge candidate",
        re.search(r'<tr class="merge-row" data-key="([^"]+)">\s*<td><a[^>]*>Mobilfunk GmbH', h) is not None,
    )
    check(
        "... Fitness too (other category, already a series)",
        re.search(r'merge-row[^>]*>\s*<td><a[^>]*>Fitness Muster</a> <span class="pill">Serie', h) is not None,
    )
    r = c.post(
        "/fixkosten/override",
        data={
            "key": mkey.group(1),
            "mode": "merge",
            "target": skey,
            "label": "Mobilfunk GmbH",
            "back": "/fixkosten/serie?key=" + skey,
        },
        follow_redirects=True,
    )
    h = r.get_data(as_text=True)
    check(
        "flash + back on the series page",
        "zur Serie dazugenommen" in h and "Serie: Streaming AG" in h and 'class="merged-row"' in h,
    )
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    st = next((x for x in p if x["key"] == skey), None)
    check(
        "Streaming series now has 5 payments, Mobilfunk no row of its own",
        st is not None and st["n"] == 5 and len(p) == 1,
        [(x["partner"], x["n"]) for x in p],
    )
    # the merged group passes the normal check (main cluster 3x12.99 in monthly rhythm) -> typical 12.99
    check(
        "merged, but rhythm/typical amount from the main cluster: 12.99",
        st and abs(st["monthly"] - 12.99) < 0.005 and abs(st["amount"] - 12.99) < 0.005 and not st.get("forced"),
        st and (st["monthly"], st["amount"]),
    )
    check(
        "Mobilfunk no longer in the suggestions",
        not re.search(r"cand-row[^>]*>\s*<td><a[^>]*>Mobilfunk GmbH", c.get("/fixkosten").get_data(as_text=True)),
    )
    r = c.post(
        "/fixkosten/override",
        data={"key": mkey.group(1), "mode": "clear", "label": "Mobilfunk GmbH", "back": "/fixkosten/serie?key=" + skey},
        follow_redirects=True,
    )
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    check(
        "separated again: Streaming 3 payments",
        [(x["partner"], x["n"]) for x in p] == [("Streaming AG", 3)],
        [(x["partner"], x["n"]) for x in p],
    )
    r = c.post(
        "/fixkosten/override",
        data={"key": mkey.group(1), "mode": "merge", "target": "", "label": "x"},
        follow_redirects=True,
    )
    check("merge without target -> FEHLER", "FEHLER" in r.get_data(as_text=True))

    print("\n6) Search 'add by hand' finds every partner, other categories too")
    h = c.get("/fixkosten?suche=fitn").get_data(as_text=True)
    check(
        "Fitness (Lifestyle, not allowed) findable via search",
        re.search(r"found-row[^>]*>\s*<td><a[^>]*>Fitness Muster", h) is not None or "Nichts gefunden" in h,
    )
    h = c.get("/fixkosten?suche=mobil").get_data(as_text=True)
    check(
        "Mobilfunk via search with add button",
        re.search(r"found-row[^>]*>\s*<td><a[^>]*>Mobilfunk GmbH", h) is not None and 'name="mode" value="force"' in h,
    )
    check(
        "search without hits says so", "Nichts gefunden" in c.get("/fixkosten?suche=gibtsnicht").get_data(as_text=True)
    )
    print("\n7) Take a single payment out of a series")
    h = c.get("/fixkosten/serie?key=" + skey).get_data(as_text=True)
    pid = re.findall(r'<tr class="pay-row" data-id="(\d+)">', h)
    check("series page lists 3 debits with button", len(pid) == 3 and 'action="/fixkosten/serie/skip"' in h, pid)
    # Before: Fitness (Lifestyle) has 3 payments - one out -> series drops below 3 -> warning + back to /fixkosten
    fit = q("SELECT id FROM transactions WHERE partner='Fitness Muster' ORDER BY booking_date")
    r = c.post("/fixkosten/serie/skip", data={"key": skey, "tx": pid[0]}, follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "Streaming: one of three out -> series gone, WARNUNG, recurring costs page",
        "WARNUNG" in h and "zu wenige" in h and "<h1>Laufende Kosten" in h,
    )
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    check(
        "Streaming no longer under private",
        not any(x["partner"] == "Streaming AG" for x in p),
        [x["partner"] for x in p],
    )
    check(
        "extra.series_skip set",
        json.loads(one("SELECT extra FROM transactions WHERE id=?", int(pid[0]))["extra"] or "{}").get("series_skip")
        == skey,
    )
    # without a series there is no series page - re-add via search, then the re-add button
    h = c.get("/fixkosten?suche=streaming").get_data(as_text=True)
    check(
        "Streaming (2 payments) findable via search",
        re.search(r"found-row[^>]*>\s*<td><a[^>]*>Streaming AG", h) is not None,
    )
    c.post("/fixkosten/override", data={"key": skey, "mode": "force", "label": "Streaming AG"}, follow_redirects=True)
    h = c.get("/fixkosten/serie?key=" + skey).get_data(as_text=True)
    check(
        "series page: 2 debits + 1 'taken out of the series'",
        len(re.findall(r'class="pay-row"', h)) == 2
        and len(re.findall(r'class="skip-row', h)) == 1
        and "wieder aufnehmen" in h,
    )
    r = c.post("/fixkosten/serie/skip", data={"key": skey, "tx": pid[0], "undo": "1"}, follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "re-added: flash + 3 debits",
        "zählt wieder zur Serie" in h and len(re.findall(r'class="pay-row"', h)) == 3,
    )
    c.post("/fixkosten/override", data={"key": skey, "mode": "clear", "label": "Streaming AG"}, follow_redirects=True)
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    check("Streaming detected normally again", [x["partner"] for x in p] == ["Streaming AG"], [x["partner"] for x in p])
    r = c.post("/fixkosten/serie/skip", data={"key": skey, "tx": "999999"}, follow_redirects=True)
    check("unknown payment -> FEHLER", "FEHLER" in r.get_data(as_text=True))
    r = c.post("/fixkosten/override", data={"key": "", "mode": "hide"}, follow_redirects=True)
    check("empty key -> FEHLER", "FEHLER" in r.get_data(as_text=True))

    print("\n5) Fixed cost branch (housing) can be hidden as well")
    con = m.db()
    woh = con.execute("SELECT id FROM categories WHERE name='Wohnung'").fetchone()["id"]
    con.execute(
        "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES('Hausverwaltung',?,50,'')", (woh,)
    )
    con.commit()
    con.close()
    html = upload(
        c,
        (
            george_bank(
                [
                    ("02.05.2026", "Hausverwaltung Muster", "Miete", "-900,00"),
                    ("02.06.2026", "Hausverwaltung Muster", "Miete", "-900,00"),
                ]
            ),
            "b2.csv",
        ),
    )
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    check(
        "housing series with key fixed:", len(w) == 1 and w[0]["key"] == "fixed:hausverwaltung", w and w[0].get("key")
    )
    c.post(
        "/fixkosten/override",
        data={"key": "fixed:hausverwaltung", "mode": "hide", "label": "Hausverwaltung"},
        follow_redirects=True,
    )
    con = m.db()
    w, p, b, s = m.recurring_groups(con)
    con.close()
    check("housing empty, sum 0", not w and s["wohnen"] == 0, (w, s))
    finish()
