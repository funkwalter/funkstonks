"""Routes: all transactions, PayPal/card lists, edit a transaction, repayment,
split, review queue, CSV export."""

import csv
import io
import json
from datetime import datetime

from flask import Response, flash, redirect, render_template, request, url_for

from .automations import is_paypal_clearing, looks_like_card_settlement
from .categories import category_choice_groups, category_context
from .config import TX_LIST_LIMIT, BadInput
from .db import db, enabled_sources, like_escape
from .money import EPS, q
from .parsing import parse_amount
from .periods import months_between, period_label, valid_month, wohnung_fixed_display_months
from .repay import is_repayment_linked, link_repayment, repayment_candidates_sql, unlink_repayment
from .rules import apply_rules, is_splitwise_settlement, normalize_pattern, rule_suggestion_for, upsert_rule
from .splitwise import save_splits, settle_composition
from .stats import eff, todo_count
from .web import app, form_category_id, form_int, local_path


@app.route("/paypal")
def paypal_page():
    """Separate tab for categorising PayPal payments - the same list/editing
    as /transactions?src=paypal, only with its own nav entry instead of the
    generic filter."""
    return transactions(forced_src="paypal")


@app.route("/karte")
def card_page():
    """Like /paypal, only for credit-card positions (source='card') - user
    request 2026-08-02: "i want a tab there like paypal that matches the
    credit card payments". The individual card positions are the "real"
    purchases behind the monthly bulk debit from the current account
    (auto_match_card nets the bank side against them, see the core model
    in docs/REFERENCE.md)."""
    return transactions(forced_src="card")


def _tx_filters(args, forced_src=None):
    """The filters of the transaction list from the query parameters,
    normalised. `cat` is "none", the legacy value "ausgaben" or a list of
    category ids (an aggregate node in the chart sends several, also with
    "none"). A year becomes the period January-December so that it runs
    through the same fixed-cost smoothing as dashboard and matrix; a single
    month wins over from/to."""
    f = {
        "month": args.get("month", ""),
        "year": args.get("year", ""),
        "cat": args.get("cat", ""),
        "src": forced_src or args.get("src", ""),
        "q": args.get("q", ""),
        "status": args.get("status", ""),
        "kind": args.get("kind", ""),
        "from": args.get("from", ""),
        "to": args.get("to", ""),
    }
    if not valid_month(f["month"]):
        f["month"] = ""
    if f["year"] and not (len(f["year"]) == 4 and f["year"].isdigit()):
        f["year"] = ""
    period = [f["month"]] if f["month"] else months_between(f["from"] or f["to"], f["to"] or f["from"])
    if not period and f["year"]:
        f["from"], f["to"] = f["year"] + "-01", f["year"] + "-12"
        period = months_between(f["from"], f["to"])
        f["year"] = ""
    parts = [x.strip() for x in (f["cat"] or "").split(",") if x.strip()]
    f["cat_ids"] = [x for x in parts if x.isdigit()]
    f["cat_none"] = len(parts) > 1 and "none" in parts
    f["period"] = period
    return f


def _tx_query(f):
    """SQL + arguments for the transaction list (without the period, which
    _tx_rows adds). Category filters include split transactions (their
    category_id is NULL, a split part points to the category) - otherwise a
    click on a dashboard cell showed less than the cell stated."""
    q = (
        "SELECT t.*, c.name AS cat_name, c.color AS cat_color FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.category_id WHERE 1=1"
    )
    args = []
    if f["year"]:
        q += " AND substr(t.booking_date,1,4)=?"
        args.append(f["year"])
    if f["kind"]:
        q += (
            " AND (t.category_id IN (SELECT id FROM categories WHERE kind=?) "
            "OR t.id IN (SELECT tx_id FROM splits WHERE category_id IN "
            "(SELECT id FROM categories WHERE kind=?)))"
        )
        args += [f["kind"], f["kind"]]
    if f["cat"] == "none" or (f["cat_none"] and not f["cat_ids"]):
        # split transactions are assigned even though category_id is NULL
        q += " AND t.category_id IS NULL AND t.status='active' AND t.id NOT IN (SELECT tx_id FROM splits)"
    elif f["cat"] == "ausgaben":
        # Legacy special value of the Sankey "Ausgaben" node (until 2026-09-11),
        # kept for saved links.
        q += (
            " AND (t.category_id IN (SELECT id FROM categories WHERE kind='expense' "
            "AND name!='Investitionen') OR t.id IN (SELECT tx_id FROM splits "
            "WHERE category_id IN (SELECT id FROM categories WHERE kind='expense' "
            "AND name!='Investitionen')))"
        )
    elif f["cat_ids"]:
        ph = ",".join("?" * len(f["cat_ids"]))
        cond = "t.category_id IN (" + ph + ") OR t.id IN (SELECT tx_id FROM splits WHERE category_id IN (" + ph + "))"
        args += f["cat_ids"] + f["cat_ids"]
        if f["cat_none"]:
            cond += " OR (t.category_id IS NULL AND t.status='active' AND t.id NOT IN (SELECT tx_id FROM splits))"
        q += " AND (" + cond + ")"
    if f["src"]:
        q += " AND t.source=?"
        args.append(f["src"])
    if f["status"]:
        q += " AND t.status=?"
        args.append(f["status"])
    else:
        # Raw Splitwise "info" rows are background data for the balance
        # cross-check, not payments of the user - hidden by default.
        q += " AND NOT (t.source='splitwise' AND t.status='info')"
    if f["q"]:
        q += " AND (t.partner LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\')"
        args += [f"%{like_escape(f['q'])}%", f"%{like_escape(f['q'])}%"]
    return q, args


def _tx_rows(con, f):
    """The rows of the list. With a period, the same fixed-cost smoothing as
    on the dashboard applies (wohnung_fixed_display_months): a transaction
    the smoothing moves into another month is missing; one it pulls in is
    added. The REAL booking date is always what is displayed."""
    q, args = _tx_query(f)
    period = f["period"]
    if not period:
        q += " ORDER BY t.booking_date DESC, t.id DESC LIMIT %d" % TX_LIST_LIMIT
        return con.execute(q, args).fetchall()
    wmap = wohnung_fixed_display_months(con)
    want = set(period)
    out_ids = [tid for tid, m in wmap.items() if m not in want]
    in_ids = [tid for tid, m in wmap.items() if m in want]

    def ph(xs):
        return ",".join("?" * len(xs)) or "-1"

    q += (
        " AND ((substr(t.booking_date,1,7) IN (" + ph(period) + ")"
        " AND t.id NOT IN (" + ph(out_ids) + ")) OR t.id IN (" + ph(in_ids) + "))"
        " ORDER BY t.booking_date DESC, t.id DESC LIMIT %d" % TX_LIST_LIMIT
    )
    return con.execute(q, args + list(period) + out_ids + in_ids).fetchall()


def _split_shares(con, f):
    """Which PART of a split transaction counts in this list? With a category
    filter the share of those categories; without a filter the sum of the
    NON-transfer parts - exactly like stats_transactions(), otherwise the
    total row did not match the balance tile (audit 2026-09-15)."""
    if f["cat_ids"]:
        ph = ",".join("?" * len(f["cat_ids"]))
        rows = con.execute(
            "SELECT tx_id, SUM(amount) AS amount FROM splits WHERE category_id IN (" + ph + ") GROUP BY tx_id",
            f["cat_ids"],
        )
        return {r["tx_id"]: r["amount"] for r in rows}
    if f["cat"] == "ausgaben":
        rows = con.execute(
            "SELECT tx_id, SUM(amount) AS amount FROM splits WHERE category_id IN "
            "(SELECT id FROM categories WHERE kind='expense' AND name!='Investitionen') "
            "GROUP BY tx_id"
        )
        return {r["tx_id"]: r["amount"] for r in rows}
    rows = con.execute(
        "SELECT s.tx_id, SUM(CASE WHEN c.kind='transfer' THEN 0 ELSE s.amount END) AS amount "
        "FROM splits s JOIN categories c ON c.id=s.category_id GROUP BY s.tx_id"
    )
    return {r["tx_id"]: (r["amount"] or 0.0) for r in rows}


@app.route("/transactions")
def transactions(forced_src=None):
    con = db()
    f = _tx_filters(request.args, forced_src)
    txs = _tx_rows(con, f)
    cats, cat_groups, cat_by_id = category_context(con)
    # Month dropdown without the raw Splitwise "info" rows (full history, only
    # for the balance cross-check) - they otherwise flooded it with old years.
    months = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT substr(booking_date,1,7) FROM transactions "
            "WHERE NOT (source='splitwise' AND status='info') ORDER BY 1 DESC"
        )
    ]
    split_ids = {r[0] for r in con.execute("SELECT DISTINCT tx_id FROM splits")}
    split_share = _split_shares(con, f)
    # Incoming payments linked as repayment - the row gets "verknuepft"
    # (linked) instead of the category/exclude buttons.
    repaid_ids = {r[0] for r in con.execute("SELECT other_tx FROM matches WHERE kind='repayment'")}
    con.close()
    rule_hint = {t["id"]: rule_suggestion_for(t) for t in txs}
    return render_template(
        "transactions.html",
        txs=txs,
        cats=cats,
        cat_groups=cat_groups,
        rule_hint=rule_hint,
        repaid_ids=repaid_ids,
        cat_by_id=cat_by_id,
        months=months,
        f=dict(
            month=f["month"],
            year=f["year"],
            cat=f["cat"],
            src=f["src"],
            q=f["q"],
            status=f["status"],
            kind=f["kind"],
            **{"from": f["from"], "to": f["to"]},
        ),
        period=f["period"],
        period_text=period_label(f["period"]),
        cat_multi=(len(f["cat_ids"]) > 1),
        tx_limit=TX_LIST_LIMIT,
        eff=eff,
        locked_src=forced_src,
        split_ids=split_ids,
        split_share=split_share,
        title=("PayPal" if forced_src == "paypal" else "Kreditkarte" if forced_src == "card" else "Transaktionen"),
    )


@app.route("/tx/<int:tx_id>/update", methods=["POST"])
def tx_update(tx_id):
    con = db()
    if "category_id" in request.form and is_splitwise_settlement(con, tx_id):
        # Settlements get NO category (user request 2026-09-01: "das sollte
        # auch keine kategorie sein sondern einfach nur aufloesen von betraegen
        # in splitwise" - not a category, just settling amounts in Splitwise).
        # They only net balances - the individual items behind them already
        # count. Categorising one would make the code below lift it from
        # 'info' to 'active', and its amount (NEGATIVE for received money,
        # because it mirrors the Splitwise balance) would end up as an expense
        # in the statistics. Server-side guard, in addition to the missing
        # dropdown on /splitwise.
        con.close()
        if request.form.get("ajax"):
            return ("", 204)
        return redirect(request.referrer or url_for("splitwise_page"))
    if ("category_id" in request.form or "toggle" in request.form) and is_repayment_linked(con, tx_id):
        # A linked repayment is 'excluded' WITH an effect on other transactions
        # - switching it back to active or recategorising it would leave the
        # expense reduced AND count the credit (double).
        raise BadInput("Diese Zahlung ist als Rückzahlung verknüpft – zuerst die Verknüpfung aufheben.")
    if "category_id" in request.form:
        cid = form_category_id(con, required=False)
        if cid and con.execute("SELECT 1 FROM splits WHERE tx_id=?", (tx_id,)).fetchone():
            # Split AND a category at the same time would be an inconsistent
            # state (a split parent must have category_id NULL) - the UI shows
            # no dropdown here, but a stale tab or a direct POST must not
            # create it either.
            raise BadInput("Diese Zahlung ist aufgeteilt – Kategorie im Split-Editor ändern.")
        con.execute("UPDATE transactions SET category_id=? WHERE id=?", (cid, tx_id))
        if cid:
            # A raw Splitwise row (status='info', e.g. a settlement without a bank
            # match) is "booked" directly by manual categorisation - otherwise
            # there would be no way to get such a position (paid in cash, other
            # account, ...) into the evaluation at all.
            con.execute("UPDATE transactions SET status='active' WHERE id=? AND status='info'", (tx_id,))
        if request.form.get("make_rule") and cid:
            # The pattern comes from the dialog in the transaction list
            # (rule_pattern); if missing, the full partner name as before.
            pattern = normalize_pattern(request.form.get("rule_pattern", ""))
            if not pattern:
                t = con.execute("SELECT partner FROM transactions WHERE id=?", (tx_id,)).fetchone()
                pattern = normalize_pattern((t["partner"] or "") if t else "")
            if pattern:
                upsert_rule(con, pattern, cid)
                hit = apply_rules(con)
                flash(
                    f"Regel angelegt: {pattern} – {hit} weitere offene Zahlung"
                    f"{'en' if hit != 1 else ''} gleich zugeordnet"
                )
    if "toggle" in request.form:
        # Only counting transactions can be excluded/included; Splitwise raw
        # data (info) stays what it is.
        con.execute(
            "UPDATE transactions SET status=CASE status WHEN 'excluded' "
            "THEN 'active' ELSE 'excluded' END WHERE id=? "
            "AND status IN ('active','excluded')",
            (tx_id,),
        )
    if "effective" in request.form:
        v = parse_amount(request.form["effective"])
        con.execute("UPDATE transactions SET effective_amount=? WHERE id=?", (v, tx_id))
    if "comment" in request.form:
        # Separate field, not `note` - `note` is filled by various automations
        # with hard-coded explanation texts (auto_split_wohnung_fixed,
        # auto_match_*, ...), a free user comment would overwrite them or be
        # overwritten itself (user request 2026-08-07).
        con.execute("UPDATE transactions SET comment=? WHERE id=?", (request.form["comment"].strip(), tx_id))
    con.commit()
    con.close()
    # Every AJAX call gets 204 instead of a redirect - this keeps the scroll
    # position (user request 2026-09-01: "wenn ich ganz unten in der Liste
    # eine Kategorie aendere, muss ich wieder runterscrollen" - changing a
    # category at the bottom of the list forced him to scroll down again).
    # Before, this applied only to the comment branch; now also to the
    # category dropdown and the "Zaehlt nicht" toggle in /transactions and
    # /partner. The exception remains "Regel merken" (remember rule): there
    # apply_rules() runs along and may recategorise OTHER rows of the list,
    # which would stay stale without a reload - the frontend deliberately
    # sends no ajax=1 for that.
    if request.form.get("ajax"):
        return ("", 204)
    return redirect(request.referrer or url_for("transactions"))


@app.route("/tx/<int:tx_id>/repay", methods=["GET", "POST"])
def repay_page(tx_id):
    """Link an incoming payment as repayment to expenses (user request
    2026-10-10: a friend outside the Splitwise group transfers his share).
    Same interaction pattern as /splitwise/link: GET shows candidates by date
    proximity, already linked ones pre-ticked; POST always dissolves the
    existing link first and rebuilds it from the selection - empty selection
    = undo."""
    con = db()
    inc = con.execute(
        "SELECT t.*, c.name AS cat_name FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?",
        (tx_id,),
    ).fetchone()
    back = request.values.get("back") or url_for("transactions")
    if not back.startswith("/"):
        back = url_for("transactions")
    if not inc or inc["source"] not in ("bank", "paypal", "card") or inc["amount"] <= EPS:
        con.close()
        flash("FEHLER Nur eine eingehende Konto-, PayPal- oder Kartenzahlung kann eine Rückzahlung sein.")
        return redirect(back)
    if request.method == "POST":
        tx_ids = request.form.getlist("tx_ids")
        unlink_repayment(con, tx_id)
        if not tx_ids:
            con.commit()
            con.close()
            flash("Rückzahlung aufgehoben – die Zahlung zählt wieder normal.")
            return redirect(back)
        parts = link_repayment(con, tx_id, tx_ids)  # BadInput -> rollback + flash
        con.commit()
        con.close()
        flash(
            f"Rückzahlung verknüpft: {inc['amount']:.2f} € auf "
            f"{len(parts)} Ausgabe{'n' if len(parts) != 1 else ''} verteilt – dort zählt nur noch der Rest"
        )
        return redirect(back)
    q_text = request.args.get("q", "")
    linked = con.execute(
        "SELECT t.*, m.amount AS delta FROM matches m JOIN transactions t ON t.id=m.bank_tx "
        "WHERE m.other_tx=? AND m.kind='repayment' ORDER BY t.booking_date",
        (tx_id,),
    ).fetchall()
    linked_ids = {r["id"] for r in linked}
    sql = "SELECT * FROM transactions WHERE " + repayment_candidates_sql()
    args = []
    if q_text:
        sql += " AND (partner LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        args += [f"%{like_escape(q_text)}%", f"%{like_escape(q_text)}%"]
    sql += " ORDER BY ABS(julianday(booking_date)-julianday(?)) LIMIT 200"
    args.append(inc["booking_date"])
    candidates = [c for c in con.execute(sql, args) if c["id"] not in linked_ids]
    con.close()
    return render_template(
        "repay.html", inc=inc, q=q_text, linked_rows=linked, candidates=candidates, back=back, eff=eff
    )


@app.route("/tx/<int:tx_id>/repay/unlink", methods=["POST"])
def repay_unlink(tx_id):
    con = db()
    n = unlink_repayment(con, tx_id)
    con.commit()
    con.close()
    flash("Rückzahlung aufgehoben" if n else "WARNUNG Diese Zahlung war nicht als Rückzahlung verknüpft")
    if request.form.get("ajax"):
        return ("", 204)
    back = request.form.get("back") or request.referrer or url_for("transactions")
    return redirect(back if back.startswith("/") or back.startswith("http") else url_for("transactions"))


@app.route("/review", methods=["GET", "POST"])
def review():
    con = db()
    if request.method == "POST":
        tx_id = form_int("tx_id")
        if request.form.get("apply_split"):
            parts = [(int(p["category_id"]), p["amount"]) for p in json.loads(request.form["apply_split"])]
            save_splits(con, tx_id, parts)
            flash("Ausgleichszahlung nach Splitwise-Zusammensetzung aufgeteilt")
        elif request.form.get("exclude"):
            con.execute(
                "UPDATE transactions SET status='excluded', note='beim Durchgehen ausgeschlossen' WHERE id=?", (tx_id,)
            )
        elif request.form.get("category_id"):
            cid = form_category_id(con)
            con.execute("UPDATE transactions SET category_id=? WHERE id=?", (cid, tx_id))
            if request.form.get("make_rule"):
                # The pattern is editable (user request 2026-08-07: "ich moechte ...
                # schlagwoerter oder aehnliches verknuepfen zu einer regel" - turn
                # keywords into a rule) - the field is prefilled with the partner
                # name, but the user may replace it before saving with any word
                # from the partner name OR the purpose text. Fallback to the
                # partner name if the field arrives empty (e.g. an old client
                # without the new field).
                pattern = request.form.get("rule_pattern", "").strip()
                if not pattern:
                    t = con.execute("SELECT partner FROM transactions WHERE id=?", (tx_id,)).fetchone()
                    pattern = (t["partner"] or "").strip() if t else ""
                if pattern:
                    upsert_rule(con, pattern, cid)
                    n = apply_rules(con)
                    flash(
                        f"Regel „{pattern}“ angelegt"
                        + (f" – {n} weitere Zahlung{'en' if n != 1 else ''} gleich mitkategorisiert" if n else "")
                    )
                else:
                    flash(
                        "Keine Regel angelegt – die Zahlung hat keinen Empfängernamen. "
                        "Trag ein Wort aus dem Verwendungszweck ins Regel-Feld ein."
                    )
        con.commit()
        was_correction = request.form.get("correction")
        con.close()
        if was_correction:
            return redirect(url_for("review"))
        offset = request.form.get("o", 0)
        return redirect(url_for("review", o=offset))
    offset = max(0, request.args.get("o", 0, type=int) or 0)
    total = todo_count(con)
    if offset >= total:
        offset = 0
    correct_id = request.args.get("tx", type=int)
    if correct_id:
        tx = con.execute("SELECT * FROM transactions WHERE id=?", (correct_id,)).fetchone()
        is_correction = True
    else:
        tx = con.execute(
            "SELECT * FROM transactions WHERE status='active' AND category_id IS NULL "
            "AND id NOT IN (SELECT tx_id FROM splits) "
            "ORDER BY booking_date DESC, id DESC LIMIT 1 OFFSET ?",
            (offset,),
        ).fetchone()
        is_correction = False
    cat_groups = category_choice_groups(con, kind="expense")
    # Keys 1-9 should hit the MOST USED categories, not the alphabetically
    # first ones (audit 2026-09-15: only 3 of the 9 most frequent reachable by
    # key). Counted over all transactions so the assignment stays stable;
    # groups count with their children.
    usage = {
        r["category_id"]: r["n"]
        for r in con.execute(
            "SELECT category_id, COUNT(*) n FROM transactions WHERE category_id IS NOT NULL GROUP BY category_id"
        )
    }

    def group_usage(g):
        return sum(usage.get(c["id"], 0) for c in g["options"])

    cat_groups.sort(key=lambda g: (-group_usage(g), (g["label"] or g["options"][0]["name"]).lower()))
    prop = settle_composition(con, tx) if tx else None
    # Suggestion for "Regel merken": for Splitwise positions `partner` is the
    # group name (a rule on it would hit EVERY position of the group), bank
    # rows carry the branch number - both make bad rules. Hence the
    # description or the name without digits.
    rule_suggestion = rule_suggestion_for(tx) if tx else ""
    # A PayPal bulk debit or card settlement should not be categorised but
    # netted - only the export is missing for that.
    clearing_hint = ""
    if tx and tx["source"] == "bank":
        srcs = enabled_sources(con)
        if "paypal" in srcs and is_paypal_clearing(tx):
            clearing_hint = (
                "Das ist eine PayPal-Abbuchung. Importiere den PayPal-Export – dann wird "
                "sie automatisch mit den echten Zahlungen verrechnet und verschwindet hier."
            )
        elif "card" in srcs and looks_like_card_settlement(tx):
            clearing_hint = (
                "Das sieht nach einer Kreditkartenabrechnung aus. Importiere den "
                "Kartenexport – dann wird sie automatisch verrechnet und verschwindet hier."
            )
    con.close()
    return render_template(
        "review.html",
        tx=tx,
        cat_groups=cat_groups,
        total=total,
        offset=offset,
        prop=prop,
        is_correction=is_correction,
        rule_suggestion=rule_suggestion,
        clearing_hint=clearing_hint,
    )


@app.route("/tx/<int:tx_id>/split", methods=["GET", "POST"])
def split_page(tx_id):
    con = db()
    keep_rows = None  # the inputs after a wrong total
    tx = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        con.close()
        return redirect(url_for("transactions"))
    if request.method == "POST":
        if request.form.get("unsplit"):
            # Undo the split: parts gone, transaction open again
            con.execute("DELETE FROM splits WHERE tx_id=?", (tx_id,))
            con.execute(
                "UPDATE transactions SET category_id=NULL, note=REPLACE(note, '[aufgeteilt]', '') WHERE id=?", (tx_id,)
            )
            con.commit()
            flash("Aufteilung aufgehoben – die Zahlung wartet wieder unter Durchgehen")
            con.close()
            return redirect(local_path(request.form.get("back")) or url_for("transactions"))
        parts = []
        valid = {r["id"] for r in con.execute("SELECT id FROM categories")}
        for cid, amt in zip(request.form.getlist("cat"), request.form.getlist("amt")):
            v = parse_amount(amt)
            if cid and v:
                if not str(cid).strip().isdigit() or int(cid) not in valid:
                    raise BadInput("Eine der Kategorien gibt es nicht (mehr) – Seite neu laden.")
                parts.append((int(cid), v))
        total = q(sum(v for _, v in parts))
        if abs(total - tx["amount"]) > 0.01:
            flash(f"FEHLER Summe der Teile ({total:.2f}) muss dem Betrag ({tx['amount']:.2f}) entsprechen")
            # Do not throw the inputs away (audit 2026-09-15)
            keep_rows = [{"category_id": c_, "amount": v_} for c_, v_ in parts]
        else:
            save_splits(con, tx_id, parts)
            con.commit()
            flash("Aufgeteilt")
            con.close()
            return redirect(local_path(request.form.get("back")) or url_for("transactions"))
        con.commit()
    existing = con.execute(
        "SELECT s.category_id, s.amount, c.name FROM splits s JOIN categories c ON c.id=s.category_id WHERE tx_id=?",
        (tx_id,),
    ).fetchall()
    prop = None if existing else settle_composition(con, tx)
    cat_groups = category_choice_groups(con)
    cat_groups_json = [
        {"label": g["label"], "options": [{"id": c["id"], "name": c["name"]} for c in g["options"]]} for g in cat_groups
    ]
    cat_by_id = {c["id"]: c for c in con.execute("SELECT * FROM categories")}
    con.close()
    return render_template(
        "split.html",
        tx=tx,
        cat_groups=cat_groups,
        cat_groups_json=cat_groups_json,
        cat_by_id=cat_by_id,
        existing=existing,
        prop=prop,
        rows=keep_rows,
        back=local_path(request.form.get("back") or request.args.get("back", "")),
    )


@app.route("/export.csv")
def export_csv():
    con = db()
    txs = con.execute(
        "SELECT t.booking_date, t.source, t.partner, t.description, t.amount, "
        "t.effective_amount, t.status, c.name AS cat FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.category_id ORDER BY booking_date"
    ).fetchall()
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Datum", "Quelle", "Partner", "Beschreibung", "Betrag", "Effektiv", "Kategorie", "Status"])
    for t in txs:
        e = t["effective_amount"] if t["effective_amount"] is not None else t["amount"]
        w.writerow(
            [
                t["booking_date"],
                t["source"],
                t["partner"],
                t["description"],
                f"{t['amount']:.2f}".replace(".", ","),
                f"{e:.2f}".replace(".", ","),
                t["cat"] or "",
                t["status"],
            ]
        )
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=moneyflow_export_" + datetime.now().strftime("%y%m%d") + ".csv"
        },
    )
