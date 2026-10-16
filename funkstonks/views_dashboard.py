"""Routes: dashboard, comparison, investments, year, partners."""

from collections import Counter
from datetime import date, timedelta
from datetime import datetime as _dt
from urllib.parse import urlencode

from flask import flash, jsonify, redirect, render_template, request, url_for

from .automations import health_checks
from .categories import category_context, category_qs, expense_rows, top_category_names
from .config import BadInput
from .db import db, get_setting_list, income_category_name
from .depot import depot_positions, depot_series_by_month, depot_summary
from .money import EPS, q
from .parsing import parse_amount
from .periods import (
    _dashboard_period,
    _period_name,
    period_args,
    period_label,
    previous_period,
    wohnung_fixed_display_months,
)
from .quotes import fetch_prices, upsert_price
from .recurring import _norm_partner, recurring_groups
from .stats import (
    _period_totals,
    build_sankey_nodes,
    compare_periods,
    dashboard_month_transactions,
    dashboard_period_transactions,
    eff,
    hidden_positive_categories,
    summarize,
    year_matrix_groups,
    year_transactions,
)
from .web import app


def _partial_cutoff(con, sel_months, months):
    """Partial month: if the last selected month is the newest one and the
    last booking lies before the end of the month, returns (note, day)."""
    data_end = (
        con.execute(
            "SELECT MAX(booking_date) FROM transactions WHERE status='active' AND source IN ('bank','paypal','card')"
        ).fetchone()[0]
        or ""
    )
    if sel_months and months and sel_months[-1] == months[0] and data_end[:7] == months[0]:
        y, mo = int(data_end[:4]), int(data_end[5:7])
        last_day = (date(y + (mo == 12), (mo % 12) + 1, 1) - timedelta(days=1)).day
        if int(data_end[8:10]) < last_day:
            return f"Daten bis {data_end[8:10]}.{data_end[5:7]}. – Monat unvollständig", data_end[8:10]
    return "", None


def _previous_period_txs(con, prev_months, wohnung_map, cutoff_day):
    """Transactions of the previous period. If the current month is partial,
    the last month of the previous period only counts up to the same day;
    smoothed fixed costs (slot month != booking month) stay in, since in the
    current month they are present from the first day as well."""
    txs = []
    for i, pm in enumerate(prev_months):
        mt = dashboard_month_transactions(con, pm, wohnung_map)
        if cutoff_day and i == len(prev_months) - 1:
            mt = [
                t
                for t in mt
                if t["booking_date"][8:10] <= cutoff_day
                or (wohnung_map.get(t["id"]) == pm and t["booking_date"][:7] != pm)
            ]
        txs += mt
    return txs


def _dashboard_context(con, args):
    """Everything the dashboard page AND /api/period need - one calculation,
    two renderings. The tile links (`urls`) are built here so that the page
    and the API name the same drilldown targets."""
    wohnung_map = wohnung_fixed_display_months(con)
    # The month dropdown shows ONLY months with real transactions.
    months = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT substr(booking_date,1,7) FROM transactions WHERE status='active' ORDER BY 1 DESC"
        )
    ]
    sel_months = _dashboard_period(args, months, set(wohnung_map.values()))
    sel_range = _period_name(sel_months, months, args.get("range", ""))
    txs = dashboard_period_transactions(con, sel_months, wohnung_map)
    inc, exp = summarize(txs)
    hidden_pos = hidden_positive_categories(txs)
    total_exp = sum(v for v, _ in exp.values())
    total_inc = sum(v for v, _ in inc.values())
    partial_note, cutoff_day = _partial_cutoff(con, sel_months, months)
    prev_months = previous_period(sel_months, months, sel_range)
    prev_txs = _previous_period_txs(con, prev_months, wohnung_map, cutoff_day)
    prev_inc, prev_exp = summarize(prev_txs)
    prev_hidden = hidden_positive_categories(prev_txs)
    cat_id_by_name = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM categories")}
    top_ancestor_name = top_category_names(con)
    # Tile "Investiert": category Investitionen including its subcategories;
    # tile "Ausgaben" and the table WITHOUT investments (user decision 2026-10-13).
    invest_names = [n for n in exp if top_ancestor_name.get(n, n) == "Investitionen"]
    invest_total = sum(exp[n][0] for n in invest_names)
    prev_invest = sum(v for n, (v, _) in prev_exp.items() if top_ancestor_name.get(n, n) == "Investitionen")
    exp_only = {n: v for n, v in exp.items() if n not in invest_names}
    spend_total = total_exp - invest_total
    p_inc = sum(v for v, _ in prev_inc.values())
    p_exp = sum(v for v, _ in prev_exp.values())
    prev_totals = {
        "inc": p_inc,
        "exp": p_exp - prev_invest,
        "invest": prev_invest,
        "saldo": p_inc - p_exp + sum(v for v, _ in prev_hidden.values()),
        "label": period_label(prev_months) + (f" (bis {int(cutoff_day)}.)" if cutoff_day and prev_months else ""),
        "n": len(prev_months),
        "qs": period_args(prev_months) if prev_months else "",
    }
    period_qs = period_args(sel_months)
    inc_qs = category_qs(inc.keys(), cat_id_by_name)
    exp_qs = category_qs(exp_only.keys(), cat_id_by_name)
    invest_qs = category_qs(invest_names, cat_id_by_name)
    saldo = total_inc - total_exp + sum(v for v, _ in hidden_pos.values())
    _w, _p, _b, _sums = recurring_groups(con)  # the same selection as /fixkosten
    return {
        "months": months,
        "sel": sel_months[-1] if sel_months else None,
        "sel_months": sel_months,
        "sel_range": sel_range,
        # a whole year is still called "2026" even if only May to August have transactions
        "period": (sel_range if sel_range.isdigit() and len(sel_range) == 4 else period_label(sel_months)),
        "period_qs": period_qs,
        "n_months": len(sel_months),
        "total_exp": spend_total,
        "total_inc": total_inc,
        # balance = income - expenses + categories in which money came back on net
        "saldo": saldo,
        "invest_total": invest_total,
        "prev_invest": prev_invest,
        "prev": prev_totals,
        "partial_note": partial_note,
        "fix_monthly": _sums["total"],
        "urls": {
            "inc": "/transactions?" + period_qs + "&" + ("cat=" + inc_qs if inc_qs else "kind=income"),
            "exp": "/transactions?" + period_qs + "&" + ("cat=" + exp_qs if exp_qs else "kind=expense"),
            "invest": ("/transactions?" + period_qs + "&cat=" + invest_qs) if invest_qs else "/investitionen",
            "saldo": "/transactions?" + period_qs,
            "fix": "/fixkosten",
        },
        "inc_qs": inc_qs,
        "exp_qs": exp_qs,
        "invest_qs": invest_qs,
        "table": expense_rows(exp_only, top_ancestor_name, cat_id_by_name, spend_total),
        "sankey": build_sankey_nodes(
            inc,
            exp,
            total_inc,
            total_exp,
            cat_id_by_name,
            hidden_pos,
            top_ancestor_name,
            get_setting_list(con, "fixkosten_cats"),
        ),
        "years": sorted({m[:4] for m in months}, reverse=True),
    }


@app.route("/")
def dashboard():
    """Tiles, money-flow Sankey and category table for a freely selectable
    period (month=, from/to, range=); the tiles compare against the equally
    long period before. The calculation lives in _dashboard_context."""
    con = db()
    ctx = _dashboard_context(con, request.args)
    ctx["hints"] = health_checks(con)
    ctx["depot"] = depot_summary(con)
    con.close()
    return render_template("dashboard.html", **ctx)


@app.route("/api/period")
def api_period():
    """The dashboard tile numbers as JSON (same parameters as /): value and
    drilldown link per tile. For tests and scripts - no HTML regex needed
    any more to check a number."""
    con = db()
    ctx = _dashboard_context(con, request.args)
    con.close()
    u = ctx["urls"]
    return jsonify(
        {
            "period": ctx["period"],
            "months": ctx["sel_months"],
            "n_months": ctx["n_months"],
            "partial_note": ctx["partial_note"],
            "tiles": {
                "inc": {"value": q(ctx["total_inc"]), "url": u["inc"]},
                "exp": {"value": q(ctx["total_exp"]), "url": u["exp"]},
                "invest": {"value": q(ctx["invest_total"]), "url": u["invest"]},
                "saldo": {"value": q(ctx["saldo"]), "url": u["saldo"]},
                "fix": {"value": q(ctx["fix_monthly"]), "url": u["fix"]},
            },
            "prev": {k: (q(v) if isinstance(v, float) else v) for k, v in ctx["prev"].items()},
        }
    )


@app.route("/vergleich")
def vergleich_page():
    """Two arbitrary periods side by side (user idea 2026-10-13: "2 monate
    direkt vergleichen in ausgaben", i.e. compare the expenses of two months
    directly). Both sides take the same parameters as the dashboard, only
    prefixed with a_/b_ (a_range, a_from, a_to, ...). Default: newest month
    against the one before."""
    con = db()
    wohnung_map = wohnung_fixed_display_months(con)
    months = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT substr(booking_date,1,7) FROM transactions WHERE status='active' ORDER BY 1 DESC"
        )
    ]

    def side(prefix, default):
        args = {k[2:]: v for k, v in request.args.items() if k.startswith(prefix + "_") and v}
        if not args:
            args = default
        sel = _dashboard_period(args, months, set(wohnung_map.values()))
        return sel, _period_name(sel, months, args.get("range", ""))

    a_months, a_range = side("a", {"month": months[0] if months else ""})
    b_months, b_range = side("b", {"month": months[1] if len(months) > 1 else (months[0] if months else "")})
    top = top_category_names(con)
    cat_id_by_name = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM categories")}
    A = _period_totals(con, a_months, wohnung_map, top)
    B = _period_totals(con, b_months, wohnung_map, top)
    rows = compare_periods(A["exp_only"], B["exp_only"], top, cat_id_by_name, A["hidden"], B["hidden"])
    con.close()

    # Changing one side keeps the other: its parameters go into hidden
    # fields or into the query string of the quick picks.
    def raw(prefix, rng):
        got = [(k, v) for k, v in request.args.items() if k.startswith(prefix + "_") and v]
        return got or ([(prefix + "_range", rng)] if rng else [])

    a_keep, b_keep = raw("a", a_range), raw("b", b_range)
    return render_template(
        "vergleich.html",
        A=A,
        B=B,
        rows=rows,
        months=months,
        a_range=a_range,
        b_range=b_range,
        a_months=a_months,
        b_months=b_months,
        a_keep=a_keep,
        b_keep=b_keep,
        a_qs=(urlencode(b_keep) + "&") if b_keep else "",
        b_qs=(urlencode(a_keep) + "&") if a_keep else "",
        years=sorted({m[:4] for m in months}, reverse=True),
    )


def _investitionen_context(con, args):
    """Monthly series and depot for the investments page and /api/investitionen.
    "Investiert" = category Investitionen including subcategories (what left
    the account towards the depot); income is ONLY real income, refunds
    (net-positive categories) reduce the expenses - so that
    balance = income - expenses - invested holds exactly (user decision 2026-10-14).
    Default period: everything."""
    wohnung_map = wohnung_fixed_display_months(con)
    months = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT substr(booking_date,1,7) FROM transactions WHERE status='active' ORDER BY 1 DESC"
        )
    ]
    eff_args = args if any(k in args for k in ("month", "from", "to", "range")) else {"range": "all"}
    sel_months = _dashboard_period(eff_args, months, set(wohnung_map.values()))
    sel_range = _period_name(sel_months, months, args.get("range", ""))
    top = top_category_names(con)
    cat_id_by_name = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM categories")}
    invest_names = [n for n, t in top.items() if t == "Investitionen"]
    # Partial month (as on the dashboard): last booking before the end of the month
    data_end = (
        con.execute(
            "SELECT MAX(booking_date) FROM transactions WHERE status='active' AND source IN ('bank','paypal','card')"
        ).fetchone()[0]
        or ""
    )
    partial_month = ""
    if data_end:
        y_, mo_ = int(data_end[:4]), int(data_end[5:7])
        last_day = (date(y_ + (mo_ == 12), (mo_ % 12) + 1, 1) - timedelta(days=1)).day
        if int(data_end[8:10]) < last_day:
            partial_month = data_end[:7]
    depot_by_month = depot_series_by_month(con, sel_months)
    rows = []
    for mth in sel_months:
        txs = dashboard_month_transactions(con, mth, wohnung_map)
        inc, exp = summarize(txs)
        hid = hidden_positive_categories(txs)
        t_inc = sum(v for v, _ in inc.values())
        refund = sum(v for v, _ in hid.values())
        t_exp = sum(v for v, _ in exp.values())
        inv = sum(v for n, (v, _) in exp.items() if top.get(n, n) == "Investitionen")
        ds = depot_by_month.get(mth) or {"cost": 0.0, "value": None, "cur": False}
        rows.append(
            {
                "month": mth,
                "label": period_label([mth]),
                "inc": q(t_inc),
                "exp": q(t_exp - inv - refund),
                "invest": q(inv),
                "saldo": q(t_inc - t_exp + refund),
                "pct": round(100 * inv / t_inc, 1) if t_inc > EPS else None,
                "depot": ds["value"],
                "depot_cost": ds["cost"],
                "depot_cur": ds["cur"],
                "partial": mth == partial_month,
            }
        )
    tot = {k: q(sum(r[k] for r in rows)) for k in ("inc", "exp", "invest", "saldo")}
    n = len(rows) or 1
    positions = depot_positions(con)
    depot = depot_summary(con) or {
        "value": 0.0,
        "gain": 0.0,
        "cost": 0.0,
        "n": 0,
        "held": 0,
        "oldest": None,
        "gain_pct": None,
    }
    return {
        "rows": rows,
        "tot": tot,
        "avg": {k: q(v / n) for k, v in tot.items()},
        "pct": round(100 * tot["invest"] / tot["inc"], 1) if tot["inc"] > EPS else None,
        "n_months": len(rows),
        "depot": depot,
        "held": [p_ for p_ in positions if p_["qty"] > 1e-9],
        "months": months,
        "sel_range": sel_range,
        "sel_months": sel_months,
        "period": (sel_range if sel_range.isdigit() and len(sel_range) == 4 else period_label(sel_months)),
        "period_qs": period_args(sel_months),
        "invest_qs": category_qs(invest_names, cat_id_by_name),
        "years": sorted({m[:4] for m in months}, reverse=True),
        "positions": positions,
        "depot_total": q(sum(p["invested"] for p in positions)),
        "has_invest_cat": "Investitionen" in cat_id_by_name,
    }


@app.route("/investitionen")
def investitionen_page():
    con = db()
    ctx = _investitionen_context(con, request.args)
    ctx["depot_rows"] = con.execute("SELECT * FROM depot_tx ORDER BY booking_date DESC, id DESC LIMIT 300").fetchall()
    con.close()
    return render_template("investitionen.html", **ctx)


@app.route("/api/investitionen")
def api_investitionen():
    """Monthly series, totals and depot of the investments page as JSON
    (same period parameters)."""
    con = db()
    ctx = _investitionen_context(con, request.args)
    con.close()
    return jsonify(
        {
            "period": ctx["period"],
            "months": ctx["sel_months"],
            "rows": ctx["rows"],
            "tot": ctx["tot"],
            "avg": ctx["avg"],
            "pct": ctx["pct"],
            "depot": ctx["depot"],
            "positions": [
                {
                    k: p[k]
                    for k in (
                        "isin",
                        "name",
                        "qty",
                        "invested",
                        "avg_price",
                        "price",
                        "value",
                        "gain",
                        "gain_pct",
                        "price_date",
                    )
                }
                for p in ctx["positions"]
            ],
        }
    )


@app.route("/partner")
def partner_page():
    """Overview: how much was spent per partner (payee/company), broken down
    by month/year. Only partners with at least 3 transactions (formerly >5 -
    lowered at the user's request 2026-08-02 because with a short data
    history only a handful of partners would otherwise reach the threshold).
    Grouped via _norm_partner (like the recurring-cost detection) - otherwise
    e.g. every BILLA branch ("BILLA DANKT 0000826" vs. "...0002231") counts
    as its own partner and stays below the minimum count."""
    con = db()
    rows = con.execute(
        "SELECT t.id, t.partner, t.booking_date, t.amount, t.effective_amount, "
        "t.category_id, c.name AS cat_name, c.color AS cat_color FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.category_id "
        "WHERE t.status='active' AND t.source IN ('bank','paypal','card') "
        "AND COALESCE(c.kind,'') != 'transfer' "  # transfers/clearings never count anywhere
        "AND t.partner IS NOT NULL AND t.partner!=''"
    ).fetchall()
    by_key = {}
    for r in rows:
        key = _norm_partner(r["partner"]) or r["partner"].lower()
        by_key.setdefault(key, []).append(r)
    partners = []
    for key, txs in by_key.items():
        if len(txs) < 3:
            continue
        name = Counter(t["partner"] for t in txs).most_common(1)[0][0]
        total = q(sum(eff(t) for t in txs))
        by_month, by_year = {}, {}
        for t in txs:
            v = eff(t)
            by_month[t["booking_date"][:7]] = q(by_month.get(t["booking_date"][:7], 0) + v)
            by_year[t["booking_date"][:4]] = q(by_year.get(t["booking_date"][:4], 0) + v)
        cat_counts = Counter((t["cat_name"] or "Unkategorisiert") for t in txs)
        top_cat = cat_counts.most_common(1)[0][0]
        top_color = next((t["cat_color"] for t in txs if (t["cat_name"] or "Unkategorisiert") == top_cat), None)
        partners.append(
            {
                "name": name,
                "key": key,
                "n": len(txs),
                "total": total,
                "first": min(t["booking_date"] for t in txs),
                "last": max(t["booking_date"] for t in txs),
                "by_month": dict(sorted(by_month.items())),
                "by_year": dict(sorted(by_year.items())),
                "cat_name": top_cat,
                "cat_color": top_color or "#C4C4C4",
                "mixed": len(cat_counts) > 1,
                "txs": sorted(txs, key=lambda t: t["booking_date"], reverse=True),
            }
        )
    partners.sort(key=lambda p: p["total"])
    sel = request.args.get("p", "")
    cats, cat_groups, cat_by_id = category_context(con)
    con.close()
    return render_template(
        "partner.html", partners=partners, sel=sel, cats=cats, cat_groups=cat_groups, cat_by_id=cat_by_id
    )


@app.route("/jahr")
def year_page():
    con = db()
    years = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT substr(booking_date,1,4) FROM transactions WHERE status='active' ORDER BY 1 DESC"
        )
    ]
    sel = request.args.get("y") or (years[0] if years else str(date.today().year))
    if not (len(sel) == 4 and sel.isdigit()):
        sel = years[0] if years else str(date.today().year)
    wmap = wohnung_fixed_display_months(con)
    txs = year_transactions(con, sel, wmap)
    inc, exp = summarize(txs)
    # Count months by their ECONOMIC month - a December rent that belongs to
    # January must not create a 13th month.
    n_months = (
        len({(wmap.get(t["id"]) if "id" in t.keys() and t["id"] in wmap else t["booking_date"][:7]) for t in txs}) or 1
    )
    tot_e = sum(v for v, _ in exp.values())
    tot_i = sum(v for v, _ in inc.values())
    cat_rows = con.execute("SELECT id, name, parent_id FROM categories").fetchall()
    cat_id_by_name = {r["name"]: r["id"] for r in cat_rows}
    # Subcategories indented under their parent, exactly as in the
    # "Ausgaben nach Kategorie" table on the dashboard (user request 2026-09-01:
    # "auch in der jahresansicht sollten unterkategorien angezeigt werden",
    # i.e. the year view should show subcategories too). The same helper, so
    # the two pages cannot drift apart.
    top_ancestor_name = top_category_names(con)
    # ONLY subcategories are expandable. The payee rows under categories
    # without subcategories (requested 2026-09-01) are gone again since
    # 2026-09-15: "da geht es nur um kategorien" (this page is only about
    # categories) - transactions and payees live on the dashboard, under
    # Partner and in the full transaction list.
    exp_rows = expense_rows(exp, top_ancestor_name, cat_id_by_name, tot_e, n_months)
    inc_rows = [
        {"name": k, "amount": v, "color": c, "avg": v / n_months, "cat_id": cat_id_by_name.get(k)}
        for k, (v, c) in sorted(inc.items(), key=lambda kv: -kv[1][0])
    ]
    # User request 2026-08-07: "einfach viele daten" (simply lots of data) - next
    # to the existing totals also the monthly averages one would otherwise have
    # to work out in one's head.
    # Balance as on the dashboard: incl. categories in which money came back on net
    hidden_y = sum(v for v, _ in hidden_positive_categories(txs).values())
    saldo_y = tot_i - tot_e + hidden_y
    avg_leftover = saldo_y / n_months
    exp_qs = category_qs(exp.keys(), cat_id_by_name)
    inc_qs = category_qs(inc.keys(), cat_id_by_name)
    inc_name = income_category_name(con)
    avg_gehalt = next((r["avg"] for r in inc_rows if r["name"] == inc_name), 0.0)
    # Month matrix - lived on the dashboard until 2026-09-11, but belongs here.
    ygroups, ymonths = year_matrix_groups(con, sel)
    con.close()
    return render_template(
        "jahr.html",
        years=years,
        sel=sel,
        exp_rows=exp_rows,
        inc_rows=inc_rows,
        tot_e=tot_e,
        tot_i=tot_i,
        n_months=n_months,
        avg_leftover=avg_leftover,
        avg_gehalt=avg_gehalt,
        inc_name=inc_name,
        ygroups=ygroups,
        ymonths=ymonths,
        saldo_y=saldo_y,
        exp_qs=exp_qs,
        inc_qs=inc_qs,
    )


@app.route("/investitionen/kurse", methods=["POST"])
def investitionen_kurse():
    """Fetch prices for the held positions - the app's only network access,
    only on this click (quotes.fetch_prices)."""
    con = db()
    held = [p for p in depot_positions(con) if p["qty"] > 1e-9 and p["isin"]]
    res = fetch_prices(con, held)
    con.commit()
    con.close()
    msg = f"{res['ok']} Kurs{'e' if res['ok'] != 1 else ''} aktualisiert"
    if res["errors"]:
        msg = (
            ("WARNUNG " if res["ok"] else "FEHLER ")
            + msg
            + " – ohne Kurs: "
            + "; ".join(f"{name} ({why})" for _, name, why in res["errors"][:6])
        )
    flash(msg)
    return redirect(url_for("investitionen_page") + "#depot")


@app.route("/investitionen/kurs", methods=["POST"])
def investitionen_kurs_manuell():
    """Manual price (euro) for a position - fallback when Yahoo does not know
    a security. Price 0 or empty deletes the stored price."""
    isin = (request.form.get("isin") or "").strip()
    if not isin:
        raise BadInput("Position wählen.")
    raw = (request.form.get("price") or "").strip()
    price = parse_amount(raw) if raw else 0.0
    if price is None or price < 0:
        raise BadInput(f"„{raw}“ ist kein Kurs.")
    day = (request.form.get("date") or "").strip() or _dt.now().strftime("%Y-%m-%d")
    con = db()
    if price < 0.0001:
        con.execute("DELETE FROM prices WHERE isin=?", (isin,))
        flash(f"Kurs für {isin} entfernt")
    else:
        upsert_price(con, isin, price, "EUR", price, day, "manual", "")
        flash(f"Kurs für {isin} gesetzt: {price:.2f} € ({day})")
    con.commit()
    con.close()
    return redirect(url_for("investitionen_page") + "#depot")
