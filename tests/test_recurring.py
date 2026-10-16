"""Recurring costs: merge provider changes, detect cancelled subscriptions.

Two real cases (2026-09-11):
  * Blizzard switched from PayPal ("Blizzard Entertainment Ireland L") to a
    bank debit via "GLOBAL COLLECT BV" - the text still says BLIZZARD. Before:
    two separate items, the new one with only ONE payment.
  * Patreon was cancelled but still appeared under recurring costs.
"""

import os
import re
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_recurring():

    tmp = tempfile.mkdtemp()
    m.DB_PATH = os.path.join(tmp, "t.db")
    m.init_db()
    con = m.db()
    m.set_setting(con, "setup_done", "1")
    m.set_setting(con, "fixkosten_privat_cats", '["Abos"]')
    abos = con.execute("SELECT id FROM categories WHERE name='Abos'").fetchone()["id"]
    rest = con.execute("SELECT id FROM categories WHERE name='Restaurant/Ausgehen'").fetchone()["id"]
    con.execute("INSERT INTO categories(name,color,kind,parent_id) VALUES('Fortgehen','#888','expense',?)", (rest,))
    fort = con.execute("SELECT id FROM categories WHERE name='Fortgehen'").fetchone()["id"]
    # Data as of today. Everything is booked relative to it so the test does not
    # flip over as time passes.
    TODAY = date.today()

    def d(days_ago):
        return (TODAY - timedelta(days=days_ago)).isoformat()

    ROWS = [
        # Blizzard: 3x PayPal, then switch to bank via a different processor
        ("paypal", 129, "Blizzard Entertainment Ireland L", "Zahlung im Einzugsverfahren", -12.99, abos),
        ("paypal", 98, "Blizzard Entertainment Ireland L", "Zahlung im Einzugsverfahren", -12.99, abos),
        ("paypal", 68, "Blizzard Entertainment Ireland L", "Zahlung im Einzugsverfahren", -12.99, abos),
        ("bank", 20, "GLOBAL COLLECT BV", "BLIZZARD EU339352308", -12.99, abos),
        # Patreon: runs cleanly monthly, but the last payment is 2 months ago
        ("paypal", 127, "Patreon Ireland Limited", "Zahlung im Einzugsverfahren", -5.40, abos),
        ("paypal", 96, "Patreon Ireland Limited", "Zahlung im Einzugsverfahren", -5.40, abos),
        ("paypal", 66, "Patreon Ireland Limited", "Zahlung im Einzugsverfahren", -5.40, abos),
        # Microsoft: unremarkable, keeps running
        ("paypal", 122, "Microsoft Payments", "Zahlung", -10.00, abos),
        ("paypal", 92, "Microsoft Payments", "Zahlung", -10.00, abos),
        ("paypal", 61, "Microsoft Payments", "Zahlung", -10.00, abos),
        ("paypal", 31, "Microsoft Payments", "Zahlung", -10.00, abos),
        ("paypal", 2, "Microsoft Payments", "Zahlung", -10.00, abos),
        # Two different venues with a similar amount and the word "CAFE": they must
        # NOT be merged (they run in parallel, and the top category
        # Restaurant/Ausgehen is never a recurring cost item anyway)
        ("bank", 120, "CAFE CENTRAL", "POS 26,00 AT K1 CAFE CENTRAL", -26.00, fort),
        ("bank", 90, "CAFE VOTIV", "POS 25,00 AT K1 CAFE VOTIV", -25.00, fort),
        ("bank", 60, "CAFE CENTRAL", "POS 26,00 AT K1 CAFE CENTRAL", -26.00, fort),
        ("bank", 30, "CAFE VOTIV", "POS 25,00 AT K1 CAFE VOTIV", -25.00, fort),
        ("bank", 5, "CAFE CENTRAL", "POS 26,00 AT K1 CAFE CENTRAL", -26.00, fort),
    ]
    for i, (src, ago, p, desc, a, cat) in enumerate(ROWS):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES(?,?,?,?,?,'EUR',?,'active',?)""",
            (src, d(ago), p, desc, a, "h%d" % i, cat),
        )
    con.commit()
    con.close()
    m.app.config["TESTING"] = True
    m.app.secret_key = "t"
    c = m.app.test_client()

    con = m.db()
    series = {s["partner"]: s for s in m.detect_recurring(con)}
    wohnen, privat, beendet, sums = m.recurring_groups(con)
    con.close()

    print("1) Provider change is detected as ONE item")
    bl = series.get("Blizzard Entertainment Ireland L")
    check("Blizzard found", bl is not None, sorted(series))
    check("all four payments in one series", bl and bl["n"] == 4, bl and bl["n"])
    check("new processor listed as alternative", bl and "GLOBAL COLLECT BV" in bl["also"], bl and bl["also"])
    check("name stays the established one", bl and bl["partner"].startswith("Blizzard"))
    check("shared word is the brand name", bl and "blizzard" in bl["match"], bl and bl["match"])
    check("keeps running", bl and not bl["ended"], bl and bl["days_since"])
    check("no second Blizzard row", sum(1 for k in series if "GLOBAL COLLECT" in k) == 0, sorted(series))

    print("2) Cancelled subscription drops out of recurring costs")
    pat = series.get("Patreon Ireland Limited")
    check("Patreon still detected", pat is not None)
    check("but marked as ended", pat and pat["ended"], pat and pat["days_since"])
    check(
        "no longer under private",
        "Patreon Ireland Limited" not in [s["partner"] for s in privat],
        [s["partner"] for s in privat],
    )
    check(
        "listed under 'no longer active'",
        "Patreon Ireland Limited" in [s["partner"] for s in beendet],
        [s["partner"] for s in beendet],
    )
    check("does not count towards the sum", abs(sums["privat"] - (12.99 + 10.00)) < 0.01, sums)

    print("3) Running subscription stays untouched")
    ms = series.get("Microsoft Payments")
    check("Microsoft active", ms and not ms["ended"], ms and ms["days_since"])
    check("in the private list", "Microsoft Payments" in [s["partner"] for s in privat])

    print("4) Two different venues are NOT lumped together")
    check(
        "no cafe in the recurring costs",
        not any("CAFE" in s["partner"] for s in privat + wohnen),
        [s["partner"] for s in privat + wohnen],
    )
    cafes = [s for k, s in series.items() if "CAFE" in k]
    check(
        "and not merged into one series either",
        all(not s["also"] for s in cafes),
        [(s["partner"], s["also"]) for s in cafes],
    )

    print("4b) A single high invoice does not tear the series apart")
    con = m.db()
    # Mobile phone bill: three normal months, one roaming invoice in between
    for i, (ago_, amt) in enumerate(((120, -18.62), (90, -18.20), (60, -88.24), (30, -17.94))):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Mobilfunk Muster GmbH','Rechnung',?,
        'EUR',?,'active',?)""",
            (d(ago_), amt, "mob%d" % i, abos),
        )
    con.commit()
    series2 = {s["partner"]: s for s in m.detect_recurring(con)}
    mob = series2.get("Mobilfunk Muster GmbH")
    check("series detected despite outlier", mob is not None and mob["n"] == 4, sorted(series2))
    check("monthly", mob and mob["interval"] == "monatlich", mob and mob["interval"])
    check(
        "amount/month = typical amount 18.20, not the outlier invoice",
        mob and abs(mob["monthly"] - 18.20) < 0.01,
        mob and mob["monthly"],
    )
    check(
        "last still shows the real last payment",
        mob and abs(mob["last_amount"] - 17.94) < 0.01,
        mob and mob["last_amount"],
    )
    check(
        "average so far includes the high invoice", mob and abs(mob["avg_all"] - 35.75) < 0.01, mob and mob["avg_all"]
    )
    # Two outliers (subscription + parallel one-off purchases): the main cluster still decides
    for i, (ago_, amt) in enumerate(((100, -24.0), (99, -24.0), (98, -24.0))):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Doppel Muster GmbH','API',?,
        'EUR',?,'active',?)""",
            (d(ago_), amt, "dop%d" % i, abos),
        )
    for i, (ago_, amt) in enumerate(((120, -108.0), (90, -108.0), (60, -108.0), (30, -108.0))):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Doppel Muster GmbH','Abo',?,
        'EUR',?,'active',?)""",
            (d(ago_), amt, "dab%d" % i, abos),
        )
    con.commit()
    dop = {s["partner"]: s for s in m.detect_recurring(con)}.get("Doppel Muster GmbH")
    check(
        "subscription beside one-off purchases: rhythm and amount come from the subscription",
        dop is not None
        and dop["interval"] == "monatlich"
        and abs(dop["amount"] - 108.0) < 0.01
        and abs(dop["monthly"] - 108.0) < 0.01,
        dop and (dop["interval"], dop["amount"], dop["monthly"]),
    )

    # Every invoice a different amount, but exactly one per month: a category
    # declared as recurring costs -> series (user rule 2026-09-24)
    for i, (ago_, amt) in enumerate(((118, -10.0), (88, -40.0), (58, -90.0), (28, -200.0))):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Wild Muster GmbH','Rechnung',?,
        'EUR',?,'active',?)""",
            (d(ago_), amt, "wil%d" % i, abos),
        )
    # The same amounts in a category NOT declared -> no series
    for i, (ago_, amt) in enumerate(((118, -10.0), (88, -40.0), (58, -90.0), (28, -200.0))):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Zufall Muster GmbH','Rechnung',?,
        'EUR',?,'active',?)""",
            (d(ago_), amt, "zuf%d" % i, fort),
        )
    # Two payments in one month -> not "exactly once a month"
    for i, (ago_, amt) in enumerate(((118, -10.0), (88, -40.0), (86, -45.0), (58, -90.0), (28, -200.0))):
        con.execute(
            """INSERT INTO transactions(source,booking_date,partner,description,amount,
        currency,raw_hash,status,category_id) VALUES('bank',?,'Doppelt Muster GmbH','Rechnung',?,
        'EUR',?,'active',?)""",
            (d(ago_), amt, "dpp%d" % i, abos),
        )
    con.commit()
    series3 = {s["partner"]: s for s in m.detect_recurring(con)}
    wild = series3.get("Wild Muster GmbH")
    check(
        "strongly varying amounts, one payment per month -> series",
        wild is not None and wild["n"] == 4 and wild["interval"] == "monatlich",
        wild and (wild["n"], wild["interval"]),
    )
    check(
        "amount/month = median 65.00 (not the last invoice 200)",
        wild and abs(wild["monthly"] - 65.0) < 0.01,
        wild and wild["monthly"],
    )
    check(
        "same amounts in undeclared category -> no series",
        "Zufall Muster GmbH" not in series3,
        sorted(series3),
    )
    check("two payments in one month -> no series", "Doppelt Muster GmbH" not in series3, sorted(series3))
    check(
        "_one_per_month: a gap month fails",
        not m._one_per_month([{"booking_date": x} for x in ("2026-01-05", "2026-02-05", "2026-04-05")]),
    )
    check(
        "_one_per_month: three months in a row passes",
        m._one_per_month([{"booking_date": x} for x in ("2026-01-05", "2026-02-28", "2026-03-01")]),
    )

    # Refetch the sums after the new test bookings (section 5 compares them with the tile)
    wohnen, privat, beendet, sums = m.recurring_groups(con)
    con.close()

    print("5) Dashboard tile and /fixkosten show the same number")
    h = c.get("/").get_data(as_text=True)
    kach = re.search(r"Laufende Kosten / Monat</span>\s*<b>([\d.]+) €", h)
    check("tile present", bool(kach), h[:200])
    check(
        "tile == page sum",
        kach and abs(float(kach.group(1)) - sums["total"]) < 0.01,
        (kach and kach.group(1), sums["total"]),
    )
    hf = c.get("/fixkosten").get_data(as_text=True)
    check("/fixkosten loads", c.get("/fixkosten").status_code == 200)
    check("sum is also on the page", "%.2f" % sums["total"] in hf)
    check("section 'no longer active'", "Nicht mehr aktiv" in hf)
    check("no explanatory text about the payment route any more (2026-09-15)", "zahlt jetzt über" not in hf)

    print("6) A resumed payment brings the item back")
    # Back in rhythm (31 days after the last one) - after a LONGER pause the series
    # deliberately drops out completely and has to re-establish itself first.
    con = m.db()
    con.execute(
        """INSERT INTO transactions(source,booking_date,partner,description,amount,
    currency,raw_hash,status,category_id) VALUES('paypal',?,'Patreon Ireland Limited',
    'Zahlung im Einzugsverfahren',-5.40,'EUR','pat-neu','active',?)""",
        (d(35), abos),
    )
    con.commit()
    w2, p2, b2, s2 = m.recurring_groups(con)
    con.close()
    check(
        "Patreon back under private",
        "Patreon Ireland Limited" in [s["partner"] for s in p2],
        [s["partner"] for s in p2],
    )
    check("and no longer under ended", "Patreon Ireland Limited" not in [s["partner"] for s in b2])
    finish()
