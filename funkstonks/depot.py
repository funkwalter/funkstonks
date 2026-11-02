"""Depot (Flatex): import trades, compute positions with cost basis."""

import hashlib
import sqlite3

from .db import db
from .money import EPS, q
from .parsing import _depot_number, find_col, parse_date

# Depot trades (buying/selling securities) do NOT go into transactions: the
# money for them has already left the account as a transfer to the broker
# (category Investitionen) - the order only records what it is invested in.
# Separate table depot_tx, separate page /investitionen. Flatex exports
# "Depotumsaetze" as XLSX (one row per order; quantity, amount and price have a
# unit cell next to them) or CSV - both with the same column names.
# User request 2026-10-10.

DEPOT_COLUMNS = {
    "date": ["buchungstag", "buchungsdatum", "datum", "date"],
    "valuta": ["valuta"],
    "name": ["bezeichnung", "wertpapier", "name"],
    "isin": ["isin"],
    "qty": ["nominal (stk.)", "nominal", "stück", "stueck", "stk", "anzahl", "quantity"],
    "amount": ["betrag", "amount"],
    "price": ["kurs", "preis", "price"],
    "ta": ["ta.-nr.", "ta.-nr", "ta-nr", "transaktionsnummer", "order"],
    "info": ["buchungsinformation", "buchungstext", "info", "text", "verwendungszweck"],
}


def depot_hash(ta, d, isin, qty, amount):
    """Separate hash for depot_tx (NOT tx_hash - the table transactions stays
    untouched): the broker's transaction number, falling back to
    date+ISIN+quantity+amount."""
    base = "depot|" + (ta.strip() if ta and ta.strip() else f"{d}|{isin}|{qty:.4f}|{amount:.2f}")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def import_depot(header, rows):
    """Read depot trades -> depot_tx. Returns (new, skipped). Kind from the
    booking info ('Kauf'/'Verkauf'), otherwise buy when quantity and amount
    are positive."""
    cols = {k: find_col(header, v) for k, v in DEPOT_COLUMNS.items()}
    if cols["date"] is None or cols["amount"] is None:
        raise ValueError("Depot-Datei ohne Datums- oder Betragsspalte")
    con = db()
    new = skipped = 0
    for r in rows:

        def cell(k, r=r):
            i = cols.get(k)
            return r[i].strip() if i is not None and i < len(r) else ""

        d = parse_date(cell("date"))
        amount = _depot_number(cell("amount"))
        if d is None or amount is None:
            continue
        qty = _depot_number(cell("qty")) or 0.0
        price = _depot_number(cell("price"))
        info = cell("info")
        low = info.lower()
        if "verkauf" in low or "sell" in low:
            kind = "sell"
        elif "kauf" in low or "buy" in low or (qty > 0 and amount > 0):
            kind = "buy"
        else:
            kind = "other"
        qty, amount = abs(qty), abs(amount)
        isin = cell("isin").upper()
        name = cell("name") or isin or "?"
        h = depot_hash(cell("ta"), d, isin, qty, amount)
        try:
            con.execute(
                "INSERT INTO depot_tx(booking_date,valuta,name,isin,qty,amount,price,kind,"
                "ta_nr,info,raw_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (d, parse_date(cell("valuta")) or "", name, isin, qty, amount, price, kind, cell("ta"), info, h),
            )
            new += 1
        except sqlite3.IntegrityError:
            skipped += 1
    con.commit()
    con.close()
    return new, skipped


def depot_positions(con):
    """Holdings per ISIN from the depot trades: quantity, invested sum (cost
    basis, average cost method), average purchase price, realized gain from
    sales. Since 2026-10-14 with the quote from `prices` (Yahoo on click or
    entered by hand): `price` = quote in euro, `value`, `gain`, `gain_pct`,
    `price_date`, `price_src`, `price_native`/`currency`; without a quote
    everything stays None."""
    pos = {}
    for r in con.execute("SELECT * FROM depot_tx ORDER BY booking_date, id"):
        key = r["isin"] or r["name"]
        p = pos.setdefault(
            key,
            {
                "isin": r["isin"],
                "name": r["name"],
                "qty": 0.0,
                "invested": 0.0,
                "realized": 0.0,
                "n": 0,
                "first": r["booking_date"],
                "last": r["booking_date"],
                "price": None,
                "value": None,
            },
        )
        p["n"] += 1
        p["last"] = r["booking_date"]
        if r["name"]:
            p["name"] = r["name"]
        if r["kind"] == "buy":
            p["qty"] += r["qty"]
            p["invested"] += r["amount"]
        elif r["kind"] == "sell":
            avg = p["invested"] / p["qty"] if p["qty"] > 1e-9 else 0.0
            cost = avg * min(r["qty"], p["qty"])
            p["qty"] = max(0.0, p["qty"] - r["qty"])
            p["invested"] -= cost
            p["realized"] += r["amount"] - cost
    prices = {r["isin"]: r for r in con.execute("SELECT * FROM prices")}
    out = []
    for p in pos.values():
        p["qty"] = round(p["qty"], 4)
        p["invested"] = q(p["invested"])
        p["realized"] = q(p["realized"])
        p["avg_price"] = q(p["invested"] / p["qty"]) if p["qty"] > 1e-9 else None
        p.update(
            {
                "gain": None,
                "gain_pct": None,
                "price_date": None,
                "price_src": None,
                "price_native": None,
                "currency": None,
            }
        )
        pr = prices.get(p["isin"]) if p["isin"] else None
        if pr and p["qty"] > 1e-9:
            p["price"] = round(pr["price_eur"], 4)
            p["value"] = q(p["qty"] * pr["price_eur"])
            p["gain"] = q(p["value"] - p["invested"])
            p["gain_pct"] = round(100.0 * p["gain"] / p["invested"], 1) if p["invested"] > EPS else None
            p["price_date"], p["price_src"] = pr["price_date"], pr["source"]
            p["price_native"], p["currency"] = pr["price"], pr["currency"]
        out.append(p)
    out.sort(key=lambda p: -p["invested"])
    return out


def depot_series_by_month(con, months):
    """Per month (month end): cumulative depot cost basis (average cost of the
    held shares, from depot_tx) and depot value (quantity x quote). Quote =
    last entry from price_history up to month end; if there is none, the
    CURRENT quote from prices (`cur`=True in the result) - otherwise the line
    stayed empty until months of history accumulated (user 2026-10-14:
    "depotwert kurse wird immer noch nicht angezeigt" - depot value is still
    not shown). If a held position has no quote at all, `value` is None.
    Returns {month: {"cost": x, "value": y|None, "cur": bool}}."""
    tx = con.execute(
        "SELECT booking_date, isin, qty, amount, kind FROM depot_tx WHERE isin!='' ORDER BY booking_date, id"
    ).fetchall()
    hist = con.execute("SELECT isin, price_date, price_eur FROM price_history ORDER BY price_date").fetchall()
    now = {r["isin"]: r["price_eur"] for r in con.execute("SELECT isin, price_eur FROM prices")}
    out = {}
    for mth in months:
        end = mth + "-31"
        qty, cost = {}, {}
        for r in tx:
            if r["booking_date"] > end:
                continue
            i = r["isin"]
            if r["kind"] == "buy":
                qty[i] = qty.get(i, 0.0) + r["qty"]
                cost[i] = cost.get(i, 0.0) + r["amount"]
            elif r["kind"] == "sell" and qty.get(i, 0.0) > 1e-9:
                avg = cost[i] / qty[i]
                sold = min(r["qty"], qty[i])
                qty[i] -= sold
                cost[i] -= avg * sold
        held = {i: q for i, q in qty.items() if q > 1e-9}
        entry = {"cost": q(sum(cost[i] for i in held)), "value": None, "cur": False}
        if held:
            last = {}
            for h in hist:
                if h["price_date"] <= end and h["isin"] in held:
                    last[h["isin"]] = h["price_eur"]
            used_now = False
            for i in held:
                if i not in last and i in now:
                    last[i] = now[i]
                    used_now = True
            if all(i in last for i in held):
                entry["value"] = q(sum(q * last[i] for i, q in held.items()))
                entry["cur"] = used_now
        out[mth] = entry
    return out


def depot_value_by_month(con, months):
    """Only the depot value per month (see depot_series_by_month)."""
    return {m_: e["value"] for m_, e in depot_series_by_month(con, months).items()}


def depot_summary(con):
    """Depot value/gain over all held positions with a quote (tile on the
    dashboard and on /investitionen). None without holdings."""
    pos = [p for p in depot_positions(con) if p["qty"] > 1e-9]
    if not pos:
        return None
    priced = [p for p in pos if p.get("value") is not None]
    d = {
        "held": len(pos),
        "n": len(priced),
        "value": q(sum(p["value"] for p in priced)),
        "gain": q(sum(p["gain"] for p in priced)),
        "cost": q(sum(p["invested"] for p in priced)),
        "oldest": min((p["price_date"] for p in priced if p["price_date"]), default=None),
    }
    d["gain_pct"] = round(100.0 * d["gain"] / d["cost"], 1) if d["cost"] > EPS else None
    return d
