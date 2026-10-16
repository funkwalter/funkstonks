"""Quotes for the depot positions - ONLY on click (Investitionen -> "Kurse
aktualisieren"), never automatically, and only for ISINs that are really
held in the depot (user 2026-10-14: "es reicht wenn nur meine kurse
angezeigt werden, die ich auch wirklich besitze" - showing only the quotes
of what I actually own is enough). Source: the unofficial Yahoo Finance
endpoints without a key - the ISIN search yields the symbol (euro listing
preferred), the chart endpoint the last quote including currency; foreign
currencies are converted to euro via <CUR>EUR=X. The result lives in
`prices` (one quote per ISIN, symbol remembered). If the service fails, the
manually entered quote stays (source='manual'). This is the ONLY place where
the app goes online - everywhere else the rule remains: no network."""

import json
import urllib.parse
import urllib.request
from datetime import datetime

SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=10&newsCount=0"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
# exchanges with euro listing - Yahoo's symbol suffix
EUR_SUFFIXES = (
    ".DE",
    ".F",
    ".SG",
    ".MU",
    ".DU",
    ".BE",
    ".HM",
    ".HA",
    ".VI",
    ".AS",
    ".PA",
    ".MI",
    ".MC",
    ".BR",
    ".LS",
    ".IR",
    ".HE",
)
TIMEOUT = 8


def http_get(url):
    """One GET with a browser identity (Yahoo rejects empty user agents).
    Swapped out in the tests - otherwise no test goes online."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (funkSTONKS)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def search_symbol(isin):
    """ISIN -> (Yahoo symbol, exchange). Euro listing before anything else,
    then XETRA (.DE) before the regional exchanges, then Yahoo's own score."""
    data = json.loads(http_get(SEARCH_URL.format(q=urllib.parse.quote(isin))))
    quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
    if not quotes:
        raise LookupError("bei Yahoo nicht gefunden")

    def rank(q):
        sym = q["symbol"].upper()
        return (0 if sym.endswith(EUR_SUFFIXES) else 1, 0 if sym.endswith(".DE") else 1, -(q.get("score") or 0))

    quotes.sort(key=rank)
    q = quotes[0]
    return q["symbol"], (q.get("exchDisp") or q.get("exchange") or "")


def quote(symbol):
    """Symbol -> (quote, currency, trading day ISO). London pence (GBp)
    become pounds."""
    data = json.loads(http_get(CHART_URL.format(sym=urllib.parse.quote(symbol))))
    res = (data.get("chart") or {}).get("result") or []
    meta = (res[0].get("meta") or {}) if res else {}
    price = meta.get("regularMarketPrice")
    if price is None:
        raise LookupError("kein Kurs für " + symbol)
    cur = (meta.get("currency") or "EUR").strip()
    price = float(price)
    if cur == "GBp":
        price, cur = price / 100.0, "GBP"
    ts = meta.get("regularMarketTime")
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")
    return price, cur, day


def eur_rate(cur, cache):
    """Exchange rate CUR -> EUR (1.0 for EUR), cached per fetch run."""
    if cur == "EUR":
        return 1.0
    if cur not in cache:
        rate, _, _ = quote(f"{cur}EUR=X")
        cache[cur] = rate
    return cache[cur]


def upsert_price(con, isin, price, currency, price_eur, price_date, source, symbol=""):
    con.execute(
        "INSERT INTO prices(isin, symbol, price, currency, price_eur, price_date, fetched_at, source) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(isin) DO UPDATE SET symbol=excluded.symbol, "
        "price=excluded.price, currency=excluded.currency, price_eur=excluded.price_eur, "
        "price_date=excluded.price_date, fetched_at=excluded.fetched_at, source=excluded.source",
        (isin, symbol, price, currency, price_eur, price_date, datetime.now().strftime("%Y-%m-%d %H:%M"), source),
    )
    # History: one quote per ISIN and trading day - over time this yields the
    # depot value per month (cumulative view) without price history from the net.
    con.execute(
        "INSERT OR REPLACE INTO price_history(isin, price_date, price_eur) VALUES(?,?,?)", (isin, price_date, price_eur)
    )


def fetch_prices(con, positions):
    """Fetch quotes for the given positions (dicts with isin/name) and write
    them to `prices`. Returns {"ok": n, "errors": [(isin, name, reason)]}.
    A known symbol is reused (no second search); if the quote fails with it,
    the symbol is searched once more."""
    known = {r["isin"]: r["symbol"] for r in con.execute("SELECT isin, symbol FROM prices WHERE symbol!=''")}
    rates = {}
    ok, errors = 0, []
    for p in positions:
        isin = (p.get("isin") or "").strip()
        if not isin:
            continue
        try:
            sym = known.get(isin)
            if sym:
                try:
                    price, cur, day = quote(sym)
                except Exception:
                    sym = None
            if not sym:
                sym, _ = search_symbol(isin)
                price, cur, day = quote(sym)
            rate = eur_rate(cur, rates)
            upsert_price(con, isin, price, cur, round(price * rate, 4), day, "yahoo", sym)
            ok += 1
        except Exception as e:  # report per position, do not abort
            errors.append((isin, p.get("name") or isin, str(e)[:120] or e.__class__.__name__))
    return {"ok": ok, "errors": errors}


def maybe_refresh_prices(con, today=None):
    """Once a day at startup (web.main, background thread): fetch quotes if
    none were fetched today, positions are held and the setting `auto_prices`
    is on. Returns ("skipped", reason) or ("fetched", result). User
    2026-10-14: "wenn an diesem tag noch keine daten bezogen worden sind ...
    beim start im hintergrund automatisch" (if no data was fetched that day
    yet, automatically in the background at startup)."""
    from .db import get_setting
    from .depot import depot_positions

    today = today or datetime.now().strftime("%Y-%m-%d")
    if get_setting(con, "auto_prices", "1") != "1":
        return "skipped", "aus"
    last = con.execute("SELECT MAX(fetched_at) FROM prices WHERE source='yahoo'").fetchone()[0] or ""
    if last[:10] == today:
        return "skipped", "heute schon"
    held = [p for p in depot_positions(con) if p["qty"] > 1e-9 and p["isin"]]
    if not held:
        return "skipped", "kein Bestand"
    res = fetch_prices(con, held)
    con.commit()
    return "fetched", res
