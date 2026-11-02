"""Load demo data for the first start and remove it again."""

import json
import random
from datetime import date

from .config import BadInput
from .db import set_setting
from .money import q

# For a first impression without own exports (user request 2026-10-13: "hilfe
# seite ... mit dummy daten" - help page with dummy data): four months of
# neutral bookings, marked as demo (extra.demo) and completely removable with
# one click.


def load_demo_data(con):
    """Creates the demo data; only into an empty DB (and never twice)."""
    if con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]:
        raise BadInput("Beispieldaten gibt es nur in einer leeren Datenbank.")
    cats = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM categories")}
    today = date.today()
    first = date(today.year, today.month, 1)
    months = []
    for back in (4, 3, 2, 1):
        y, mo = first.year, first.month - back
        while mo <= 0:
            y, mo = y - 1, mo + 12
        months.append(date(y, mo, 1))
    rng = random.Random(42)
    rows = []  # (date, partner, description, amount, category)
    for mth in months:

        def d(day, mth=mth):
            return date(mth.year, mth.month, min(day, 28)).isoformat()

        rows += [
            (d(1), "Arbeitgeber Muster AG", "Gehalt", 2850.00, "Gehalt"),
            (d(2), "Hausverwaltung Muster", "Miete", -890.00, "Wohnung"),
            (d(5), "Energie Muster", "Strom", -62.00, "Wohnung"),
            (d(6), "Netz Muster", "Internet", -29.90, "Abos"),
            (d(8), "Streaming Muster", "Abo", -12.99, "Abos"),
            (d(10), "Mobilfunk Muster", "Tarif", -18.00, "Abos"),
            (d(3), "Verkehrsbetriebe Muster", "Monatskarte", -49.90, "Transport/Mobilität"),
            (d(15), "Broker Muster", "Sparplan", -200.00, "Investitionen"),
            (d(12), "", "ATM 100,00 AT K1", -100.00, "Bargeld"),
        ]
        for i in range(7):
            rows.append((d(2 + i * 4), "Supermarkt Muster", "Einkauf", -q(rng.uniform(12, 65)), "Lebensmittel"))
        for i in range(3):
            rows.append(
                (
                    d(5 + i * 8),
                    rng.choice(["Café Muster", "Pizzeria Muster", "Bistro Muster"]),
                    "POS",
                    -q(rng.uniform(8, 42)),
                    "Restaurant/Ausgehen",
                )
            )
        rows.append((d(20), "Apotheke Muster", "POS", -q(rng.uniform(5, 30)), "Gesundheit"))
    rows.append(
        (date(months[-1].year, months[-1].month, 18).isoformat(), "Shop Muster", "Rückerstattung", 25.00, "Konsum")
    )
    rows.append(
        (date(months[-2].year, months[-2].month, 9).isoformat(), "Shop Muster", "Online-Kauf", -79.00, "Konsum")
    )
    n = 0
    for i, (d_, partner, desc, amount, cat) in enumerate(rows):
        con.execute(
            "INSERT OR IGNORE INTO transactions(source,booking_date,partner,description,amount,currency,"
            "category_id,raw_hash,extra) VALUES('bank',?,?,?,?,'EUR',?,?,?)",
            (d_, partner, desc, amount, cats.get(cat), "demo:%d" % i, json.dumps({"demo": True})),
        )
        n += 1
    con.execute(
        "INSERT OR IGNORE INTO depot_tx(booking_date,valuta,name,isin,qty,amount,price,kind,ta_nr,info,raw_hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            months[-1].isoformat(),
            months[-1].isoformat(),
            "Welt-ETF Muster",
            "XX0000000001",
            3,
            600.0,
            200.0,
            "buy",
            "demo",
            "Beispieldaten",
            "demo:depot:1",
        ),
    )
    set_setting(con, "demo_data", "1")
    return n


def remove_demo_data(con):
    n = con.execute("DELETE FROM transactions WHERE json_extract(extra,'$.demo')=1").rowcount
    con.execute("DELETE FROM depot_tx WHERE raw_hash LIKE 'demo:%'")
    con.execute(
        "DELETE FROM matches WHERE bank_tx NOT IN (SELECT id FROM transactions) "
        "OR other_tx NOT IN (SELECT id FROM transactions)"
    )
    con.execute("DELETE FROM splits WHERE tx_id NOT IN (SELECT id FROM transactions)")
    con.execute("DELETE FROM settings WHERE key='demo_data'")
    return n
