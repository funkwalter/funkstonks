"""Prices for depot positions: symbol choice (euro listing before London),
conversion (USD, GBp), storage in prices, display of price/value/gain and the
depot value tile, errors per position without abort, manual price, symbol
remembered - and: a page view never touches the network. http_get is swapped
out, no test goes online."""

import json
import re

from _harness import check, finish  # noqa: E402
from _import_common import *

from funkstonks import quotes


def test_quotes():

    tmp, c = fresh_db()
    con = m.db()
    for i, (d, name, isin, qty, amount, kind) in enumerate(
        (
            ("2026-05-02", "World ETF", "IE00TEST0001", 10, 1000.0, "buy"),
            ("2026-06-02", "US Tech AG", "US0000TEST02", 5, 800.0, "buy"),
            ("2026-06-10", "UK Fund", "GB00TEST0003", 100, 1500.0, "buy"),
            ("2026-07-01", "Unbekannt AG", "XX00TEST0004", 3, 300.0, "buy"),
        )
    ):
        con.execute(
            "INSERT INTO depot_tx(booking_date,valuta,name,isin,qty,amount,price,kind,ta_nr,info,raw_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d, d, name, isin, qty, amount, amount / qty, kind, str(i), "", "q" + str(i)),
        )
    con.commit()
    con.close()

    calls = []

    def fake_get(url):
        calls.append(url)
        if "finance/search" in url:
            q = url.split("q=")[1].split("&")[0]
            if q == "IE00TEST0001":
                return json.dumps(
                    {
                        "quotes": [
                            {"symbol": "IWDA.L", "exchange": "LSE", "score": 20000},
                            {"symbol": "EUNL.DE", "exchange": "GER", "score": 15000},
                            {"symbol": "EUNL.F", "exchange": "FRA", "score": 16000},
                        ]
                    }
                )
            if q == "US0000TEST02":
                return json.dumps({"quotes": [{"symbol": "USTK", "exchange": "NMS", "score": 1}]})
            if q == "GB00TEST0003":
                return json.dumps({"quotes": [{"symbol": "UKF.L", "exchange": "LSE", "score": 1}]})
            return json.dumps({"quotes": []})
        import urllib.parse

        sym = urllib.parse.unquote(url.split("chart/")[1].split("?")[0])
        meta = {
            "EUNL.DE": (132.1, "EUR"),
            "USTK": (200.0, "USD"),
            "UKF.L": (1500.0, "GBp"),
            "USDEUR=X": (0.9, "EUR"),
            "GBPEUR=X": (1.2, "EUR"),
        }.get(sym)
        if not meta:
            return json.dumps({"chart": {"result": None, "error": {"description": "No data"}}})
        return json.dumps(
            {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": meta[0],
                                "currency": meta[1],
                                "regularMarketTime": 1791900000,
                                "symbol": sym,
                            }
                        }
                    ]
                }
            }
        )

    quotes.http_get = fake_get

    print("1) Page view without network, button present")

    def boom(url):
        raise AssertionError("Netzzugriff beim Seitenaufruf: " + url)

    quotes.http_get = boom
    h = c.get("/investitionen").get_data(as_text=True)
    check(
        "page loads without network, with refresh button; depot value tile shows a dash without price",
        'action="/investitionen/kurse"' in h and "noch kein Kurs" in h,
    )
    quotes.http_get = fake_get

    print("\n2) Fetch prices")
    r = c.post("/investitionen/kurse", follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "flash: 3 prices, 1 without (Unbekannt AG)",
        "WARNUNG 3 Kurse aktualisiert" in h and "Unbekannt AG" in h and "nicht gefunden" in h,
        re.findall(r'class="flash[^"]*">([^<]*)', h),
    )
    rows = {r_["isin"]: dict(r_) for r_ in q("SELECT * FROM prices")}
    check(
        "euro listing before London: EUNL.DE",
        rows["IE00TEST0001"]["symbol"] == "EUNL.DE" and abs(rows["IE00TEST0001"]["price_eur"] - 132.1) < 0.001,
        rows.get("IE00TEST0001"),
    )
    check(
        "USD converted to euro (200 * 0.9)",
        rows["US0000TEST02"]["currency"] == "USD" and abs(rows["US0000TEST02"]["price_eur"] - 180.0) < 0.001,
        rows.get("US0000TEST02"),
    )
    check(
        "GBp -> GBP -> euro (1500p = 15 GBP * 1.2)",
        rows["GB00TEST0003"]["currency"] == "GBP"
        and abs(rows["GB00TEST0003"]["price"] - 15.0) < 0.001
        and abs(rows["GB00TEST0003"]["price_eur"] - 18.0) < 0.001,
        rows.get("GB00TEST0003"),
    )
    check("unknown ISIN without row", "XX00TEST0004" not in rows)
    check("exchange rate fetched only once per refresh", sum(1 for u in calls if "USDEUR%3DX" in u) == 1)

    check(
        "price history recorded (3 ISINs, 1 trading day)",
        len(q("SELECT * FROM price_history")) == 3,
        q("SELECT * FROM price_history"),
    )
    con = m.db()
    byv = m.depot_value_by_month(con, ["2026-05", "2026-06", "2026-07", "2026-10"])
    con.close()
    check(
        "depot value per month: May only World ETF (10 * current price 132.1 = 1321), June all three (4021), "
        "October None (Unbekannt AG without any price)",
        byv["2026-05"] == 1321.0 and byv["2026-06"] == 4021.0 and byv["2026-10"] is None,
        byv,
    )
    con = m.db()
    ser = m.depot_series_by_month(con, ["2026-05", "2026-06", "2026-10"])
    con.close()
    check(
        "depot cost basis per month: 1000 / 3300 / 3600; May marked with current price",
        ser["2026-05"]["cost"] == 1000.0
        and ser["2026-06"]["cost"] == 3300.0
        and ser["2026-10"]["cost"] == 3600.0
        and ser["2026-05"]["cur"] is True,
        ser,
    )
    r = c.post(
        "/investitionen/kurs",
        data={"isin": "XX00TEST0004", "price": "100", "date": "2026-10-13"},
        follow_redirects=True,
    )
    con = m.db()
    byv = m.depot_value_by_month(con, ["2026-10"])
    con.close()
    check("... with price for all positions: 4021 + 3*100 = 4321", byv["2026-10"] == 4321.0, byv)
    c.post("/investitionen/kurs", data={"isin": "XX00TEST0004", "price": "0"}, follow_redirects=True)
    con = m.db()
    con.execute("DELETE FROM price_history WHERE isin='XX00TEST0004'")
    con.commit()
    con.close()

    print("\n3) Display")

    def row(h, isin):
        mm = re.search(
            r'<tr class="pos-row" data-isin="' + isin + r'"[^>]*data-value="([^"]*)" data-gain="([^"]*)">', h
        )
        return (mm.group(1), mm.group(2)) if mm else None

    check(
        "World ETF: value 10*132.1 = 1321.00, gain +321.00",
        row(h, "IE00TEST0001") == ("1321.0", "321.0"),
        row(h, "IE00TEST0001"),
    )
    check("US Tech: value 5*180 = 900, gain +100", row(h, "US0000TEST02") == ("900.0", "100.0"), row(h, "US0000TEST02"))
    check(
        "UK Fund: value 100*18 = 1800, gain +300",
        row(h, "GB00TEST0003") == ("1800.0", "300.0"),
        row(h, "GB00TEST0003"),
    )
    check("Unbekannt: no value", row(h, "XX00TEST0004") == ("", ""), row(h, "XX00TEST0004"))
    check(
        "depot value tile 4021.00, +721.00, 3 of 4 positions",
        re.search(r"Depotwert</span>.*?4021\.00 €.*?\+721\.00 €.*?3 von 4 Positionen", h, re.S) is not None,
    )
    check(
        "total row value/gain",
        re.search(r"<b>Summe</b>.*?<b>4021\.00 €</b>.*?<b>\+721\.00 €</b>", h, re.S) is not None,
    )
    check("foreign currency in the tooltip", "200.00 USD" in h and "15.00 GBP" in h)

    print("\n4) Manual price and symbol remembered")
    r = c.post(
        "/investitionen/kurs",
        data={"isin": "XX00TEST0004", "price": "110,5", "date": "2026-10-10"},
        follow_redirects=True,
    )
    h = r.get_data(as_text=True)
    check(
        "set manually: flash, pill, value 3*110.5",
        "Kurs für XX00TEST0004 gesetzt: 110.50 €" in h
        and ">manuell</span>" in h
        and row(h, "XX00TEST0004") == ("331.5", "31.5"),
        row(h, "XX00TEST0004"),
    )
    check("tile now 4 of 4 -> date", "Kurse vom 2026-10-10" in h or "Kurse vom 2026-10-1" in h)
    r = c.post("/investitionen/kurs", data={"isin": "XX00TEST0004", "price": "abc"}, follow_redirects=True)
    check("no price -> FEHLER", "FEHLER" in r.get_data(as_text=True))
    r = c.post("/investitionen/kurs", data={"isin": "XX00TEST0004", "price": "0"}, follow_redirects=True)
    check(
        "0 removes the price",
        "entfernt" in r.get_data(as_text=True)
        and count("1") >= 0
        and not q("SELECT * FROM prices WHERE isin='XX00TEST0004'"),
    )
    calls.clear()
    c.post("/investitionen/kurse", follow_redirects=True)
    check(
        "second refresh: no search for known symbols",
        not any("finance/search?q=IE00TEST0001" in u for u in calls) and any("chart/EUNL.DE" in u for u in calls),
    )

    print("\n5) Automatically at start: once a day")
    con = m.db()
    st = m.maybe_refresh_prices(con, today="2099-01-01")
    con.close()
    check("nothing fetched today (2099) yet -> fetches", st[0] == "fetched" and st[1]["ok"] == 3, st)
    con = m.db()
    today = one("SELECT substr(MAX(fetched_at),1,10) d FROM prices")["d"]
    st2 = m.maybe_refresh_prices(con, today=today)
    con.close()
    check("again on the same day -> skipped", st2 == ("skipped", "heute schon"), st2)
    con = m.db()
    m.set_setting(con, "auto_prices", "")
    con.commit()
    st3 = m.maybe_refresh_prices(con, today="2099-01-02")
    m.set_setting(con, "auto_prices", "1")
    con.commit()
    con.close()
    check("setting off -> skipped", st3 == ("skipped", "aus"), st3)
    h = c.get("/einstellungen").get_data(as_text=True)
    check("checkbox in the settings", 'name="auto_prices" value="1" checked' in h)
    r = c.post(
        "/einstellungen",
        data={"action": "general", "data_cutoff": "", "auto_prices_present": "1"},
        follow_redirects=True,
    )
    check("checkbox removed -> setting empty", one("SELECT value FROM settings WHERE key='auto_prices'")["value"] == "")
    d = c.get("/").get_data(as_text=True)
    check("dashboard tile Investiert shows the depot value", 'Depotwert <b class="invest">' in d)

    print("\n6) Service down: error per position, old prices stay")

    def down(url):
        raise OSError("timed out")

    quotes.http_get = down
    r = c.post("/investitionen/kurse", follow_redirects=True)
    h = r.get_data(as_text=True)
    check(
        "FEHLER flash with reason, page present",
        "FEHLER 0 Kurse aktualisiert" in h and "timed out" in h and "<h1>Investitionen" in h,
    )
    check(
        "old prices unchanged",
        abs(one("SELECT price_eur FROM prices WHERE isin='IE00TEST0001'")["price_eur"] - 132.1) < 0.001,
    )
    finish()
