"""Hints (health_checks): missing/stale exports, PayPal debits without receipt,
unsettled card statement - as a card on the dashboard and as a counter in the
sidebar (user request 2026-10-13)."""

import re

from _harness import check, finish  # noqa: E402
from _import_common import *


def test_health():

    tmp, c = fresh_db()
    con = m.db()
    # old data (May 2026) -> the bank statement counts as stale today (October 2026 or later)
    con.commit()
    con.close()
    upload(
        c,
        (
            george_bank(
                [
                    ("02.05.2026", "Arbeitgeber AG", "Gehalt", "3000,00"),
                    ("04.05.2026", "PayPal Europe S.a.r.l. et Cie S.C.A", "1234/PAYPAL", "-29,99"),
                    ("06.05.2026", "Erste Bank", "s Kreditkartenrechnung Apr. 2026 Premiumcard", "-120,00"),
                ]
            ),
            "b.csv",
        ),
    )

    print("1) Hints on the dashboard and counter in the sidebar")
    con = m.db()
    hints = m.health_checks(con)
    con.close()
    keys = {h["key"] for h in hints}
    check("bank statement stale", "stale:bank" in keys, keys)
    check(
        "PayPal/card/Splitwise active but never imported",
        {"missing:paypal", "missing:card", "missing:splitwise"} <= keys,
        keys,
    )
    check("card statement not settled", "card_open" in keys, keys)
    check(
        "every hint has text, link and menu entry",
        all(h["text"] and h["href"] and h["nav"] in ("import", "splitwise") for h in hints),
    )
    h = c.get("/").get_data(as_text=True)
    check(
        "'hints' card with all entries",
        h.count('<li class="hint"') == len(hints) and "neuer Export fällig" in h,
    )
    n_imp = sum(1 for x in hints if x["nav"] == "import")
    check(
        "counter at the import menu entry = %d" % n_imp,
        re.search(r'href="/import"[^>]*>.*?<span class="nav-badge">%d</span>' % n_imp, h, re.S) is not None,
        re.findall(r'href="/import"[^>]*>.*?</a>', h, re.S)[:1],
    )
    check(
        "tooltip mentions hints, not categories",
        ("%d Hinweise zu den Exporten" % n_imp) in h or ("%d Hinweis zu den Exporten" % n_imp) in h,
    )

    print("\n2) Disable a source -> its hint disappears; PayPal receipt gap")
    c.post("/einstellungen", data={"action": "sources", "src_bank": "1", "src_paypal": "1"})
    con = m.db()
    hints = m.health_checks(con)
    con.close()
    keys = {h["key"] for h in hints}
    check(
        "card/Splitwise hints gone, PayPal stays",
        "missing:card" not in keys and "missing:splitwise" not in keys and "missing:paypal" in keys,
        keys,
    )
    check("no card statement warning without card source", "card_open" not in keys)
    upload(
        c, (paypal_akt([("03.05.2026", "Netflix", "Zahlung", "EUR", "-9,99", "TX0001")]), "Download.CSV")
    )  # after tracking_start (02.05.)
    con = m.db()
    hints = m.health_checks(con)
    con.close()
    pp = next((x for x in hints if x["key"] == "paypal_open"), None)
    check(
        "PayPal debit of 04.05. without receipt (export up to 03.05.)",
        pp is not None and "1 PayPal-Abbuchung ohne Beleg" in pp["text"],
        pp,
    )
    check(
        "PayPal export stale relative to the account? (only 3 days -> no)",
        "stale:paypal" not in {x["key"] for x in hints},
    )

    print("\n3) Fresh data -> no hints")
    tmp2, c2 = fresh_db(sources='["bank"]')
    from datetime import date

    today = date.today().isoformat()
    upload(
        c2, (george_bank([(f"{today[8:10]}.{today[5:7]}.{today[:4]}", "Arbeitgeber AG", "Gehalt", "3000,00")]), "b.csv")
    )
    con = m.db()
    hints = m.health_checks(con)
    con.close()
    check("no hints", hints == [], hints)
    h = c2.get("/").get_data(as_text=True)
    check(
        "no hints card, no counter at import",
        'class="warn hints"' not in h and not re.search(r'href="/import"[^>]*>(?:(?!</a>).)*nav-badge', h, re.S),
    )
    finish()
