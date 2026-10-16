"""Routes: Splitwise overview and all actions per position/group."""

import json
from datetime import date

from flask import flash, jsonify, redirect, render_template, request, url_for

from .categories import category_context
from .config import BadInput
from .db import db, get_setting_list, like_escape
from .importers import is_shared_fixed_cost_position, tracking_start, unlink_splitwise_position
from .money import EPS, q
from .rules import splitwise_category_name
from .splitwise import link_own_advance, splitwise_amount_mismatches, splitwise_overview
from .web import app


def _promoted_rows(con, groups):
    """Per group the rows of the position list: promoted shares (with category
    dropdown), settlements without a bank match (settlement=True: NO dropdown
    - received money otherwise ended up as an expense, happened for real
    2026-09-01) and ones netted against a bank transaction (matched=True,
    display only). is_payment rows of the promoted positions are excluded (a
    manually booked settlement otherwise looked like a wrong minus)."""
    out = {}
    promoted = con.execute(
        "SELECT * FROM transactions WHERE source='splitwise' AND status='active' ORDER BY booking_date DESC"
    ).fetchall()
    for r in promoted:
        if json.loads(r["extra"] or "{}").get("is_payment"):
            continue
        out.setdefault(r["partner"], []).append(
            {
                "id": r["id"],
                "booking_date": r["booking_date"],
                "description": r["description"],
                "amount": r["amount"],
                "note": r["note"],
                "category_id": r["category_id"],
                "matched": False,
                "settlement": False,
            }
        )
    for name, g in groups.items():
        for p in g["payments"]:
            if p["bank"] is None:
                out.setdefault(name, []).append(
                    {
                        "id": p["id"],
                        "booking_date": p["date"],
                        "description": p["desc"],
                        "amount": p["amount"],
                        "note": "",
                        "category_id": p["category_id"],
                        "matched": False,
                        "settlement": True,
                    }
                )
            else:
                out.setdefault(name, []).append(
                    {
                        "id": p["id"],
                        "booking_date": p["date"],
                        "description": p["desc"],
                        "amount": p["amount"],
                        "note": f"✓ verrechnet mit Kontobuchung {p['bank']['booking_date']} {p['bank']['partner'][:24]}",
                        "category_id": None,
                        "matched": True,
                    }
                )
    return out


def _own_advance_rows(con, start, into):
    """Own advanced positions (bal>0) - always stay status='info', hence a
    separate query: linked (note with the bank transaction) or open ("bar
    bezahlt?", i.e. paid in cash?, with a link to connect). Shared fixed costs
    run through the halved bank transactions and are deliberately missing here."""
    own_rows = con.execute(
        "SELECT * FROM transactions WHERE source='splitwise' AND status='info' "
        "AND booking_date>=? ORDER BY booking_date DESC",
        (start,),
    ).fetchall()
    for t in own_rows:
        ex = json.loads(t["extra"] or "{}")
        if ex.get("is_payment") or t["amount"] <= EPS or is_shared_fixed_cost_position(con, t):
            continue
        match = con.execute(
            "SELECT bank_tx FROM matches WHERE other_tx=? AND kind='splitwise_share'", (t["id"],)
        ).fetchone()
        cost = ex.get("cost", 0)
        share = q(cost - t["amount"])
        row = {
            "id": t["id"],
            "booking_date": t["booking_date"],
            "description": t["description"],
            "amount": -share,
            "category_id": None,
            "matched": True,
            "own_advance": True,
        }
        if not match:
            row.update({"note": "bar bezahlt?", "linked": False, "dismissed": bool(ex.get("dismissed"))})
        else:
            bank = con.execute(
                "SELECT booking_date, partner FROM transactions WHERE id=?", (match["bank_tx"],)
            ).fetchone()
            row.update(
                {
                    "note": f"✓ verknüpft mit Kontobuchung {bank['booking_date']} {bank['partner'][:24]} "
                    f"(dein Anteil {share:.2f}€ von {cost:.2f}€ vorgestreckt)",
                    "linked": True,
                    "dismissed": False,
                }
            )
        into.setdefault(t["partner"], []).append(row)


@app.route("/splitwise")
def splitwise_page():
    """Per group the balance (full history, cross-check with the app) and ONE
    list of all positions since tracking start - promoted shares,
    settlements, own advances (user request 2026-08-06: no more separate
    tables)."""
    con = db()
    groups = splitwise_overview(con)
    start = tracking_start(con)
    for g in groups.values():
        g["payments"] = [p for p in g["payments"] if p["date"] >= start]
    promoted_by_group = _promoted_rows(con, groups)
    _own_advance_rows(con, start, promoted_by_group)
    for items in promoted_by_group.values():
        items.sort(key=lambda t: t["booking_date"], reverse=True)
    urlaub_partners = {r["partner"] for r in con.execute("SELECT partner FROM urlaub_groups")}
    cats, cat_groups, cat_by_id = category_context(con)
    mismatches = splitwise_amount_mismatches(con)
    excluded = set(get_setting_list(con, "urlaub_excluded_groups"))
    # Closed groups (balance 0) move to a compact list at the bottom.
    closed = {r["partner"]: r["closed_at"] for r in con.execute("SELECT * FROM closed_groups")}
    closed_groups = {n: g for n, g in groups.items() if n in closed}
    groups = {n: g for n, g in groups.items() if n not in closed}
    con.close()
    return render_template(
        "splitwise.html",
        groups=groups,
        promoted_by_group=promoted_by_group,
        cats=cats,
        cat_groups=cat_groups,
        cat_by_id=cat_by_id,
        urlaub_partners=urlaub_partners,
        urlaub_excluded_groups=excluded,
        mismatches=mismatches,
        closed=closed,
        closed_groups=closed_groups,
    )


def _back_to_group(tx_id):
    """After an action on a position, go back to ITS group on /splitwise
    (anchor) instead of the top of a long page."""
    con = db()
    r = con.execute("SELECT partner FROM transactions WHERE id=?", (tx_id,)).fetchone()
    con.close()
    from urllib.parse import quote

    return redirect(url_for("splitwise_page") + ("#g-" + quote(r["partner"]) if r and r["partner"] else ""))


@app.route("/splitwise/<name>/close", methods=["POST"])
def splitwise_close_group(name):
    """Close a group (only at balance 0) or reopen it with `undo=1`.
    User request 2026-09-15: a settled group should be "properly closed" -
    one still sees where a transaction came from, but the group does not sit
    on /splitwise forever with all its positions. A new export with activity
    in the group reopens it automatically (see import_page)."""
    con = db()
    if request.form.get("undo"):
        con.execute("DELETE FROM closed_groups WHERE partner=?", (name,))
        con.commit()
        con.close()
        flash(f"Gruppe {name} wieder geöffnet")
        return redirect(url_for("splitwise_page"))
    g = splitwise_overview(con).get(name)
    if g is None:
        con.close()
        raise BadInput("Diese Gruppe gibt es nicht.")
    if abs(g["saldo"]) >= 0.005:
        con.close()
        raise BadInput(f"Gruppe {name} hat noch einen Saldo von {g['saldo']:.2f} € – erst ausgleichen, dann schließen.")
    con.execute(
        "INSERT OR REPLACE INTO closed_groups(partner, closed_at) VALUES(?,?)", (name, date.today().isoformat())
    )
    con.commit()
    con.close()
    flash(f"Gruppe {name} geschlossen – die Buchungen bleiben, die Gruppe steht unten unter „Geschlossen“")
    return redirect(url_for("splitwise_page"))


@app.route("/splitwise/amount-ok/<int:tx_id>", methods=["POST"])
def splitwise_amount_ok(tx_id):
    """ "Passt so" (looks right) on an amount hint - user request 2026-09-01:
    "aber ich moechte auch bestaetigen koennen, dass es richtig war" - he also
    wants to be able to confirm that it was correct. Stores a flag in `extra`
    of the Splitwise row (like `dismissed` already), so changes neither amount
    nor category nor status. `undo=1` withdraws the confirmation."""
    con = db()
    r = con.execute("SELECT extra FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if r:
        try:
            extra = json.loads(r["extra"] or "{}")
        except ValueError:
            extra = {}
        if request.form.get("undo"):
            extra.pop("amount_ok", None)
        else:
            extra["amount_ok"] = True
        con.execute("UPDATE transactions SET extra=? WHERE id=?", (json.dumps(extra, ensure_ascii=False), tx_id))
        con.commit()
    con.close()
    if request.form.get("ajax"):
        return ("", 204)
    return redirect(url_for("splitwise_page"))


@app.route("/splitwise/<name>/urlaub", methods=["POST"])
def splitwise_toggle_urlaub(name):
    """Checkbox "Als Urlaub markieren" (mark as holiday) per group (user
    request 2026-08-06: "ich moechte die Moeglichkeit, eine Gruppe komplett
    als Urlaub zu markieren, sodass jede Zahlung dort als Urlaub kategorisiert
    wird" - mark a whole group as holiday so every payment in it is
    categorised as Urlaub). Sets/clears the mark AND applies it immediately
    and retroactively to all currently promoted expenses of this group -
    promote_splitwise_shares() makes sure that newly promoted positions from
    this group also get "Urlaub" automatically instead of the normal category
    assignment. Deliberately affects only expenses (is_payment=False), not the
    settlements themselves - those are not an expense of their own.
    Returns the changed {id, category_id} pairs as JSON so the frontend can
    update the visible dropdowns without a reload (user request: "wenn ich
    als urlaub markieren klicke tut sich nichts bei den kategorien" - nothing
    happened to the categories on click; the plain fetch() call before did
    change the DB, but nobody updated the visible <select> elements).
    Unticking REALLY reverts it (not just stopping the future automation):
    for every expense of this group still set to "Urlaub", the category is
    recomputed from the unchanged stored Splitwise raw data
    (extra.sw_category), exactly as at the original promotion - positions the
    user manually switched to something other than Urlaub stay untouched
    (filter category_id=Urlaub)."""
    con = db()
    if name in set(get_setting_list(con, "urlaub_excluded_groups")):
        # The checkbox does not even exist in the template for these groups
        # (see the excluded groups in the settings) - this guard only defends
        # against a direct POST, it is not a normal UI path.
        con.close()
        return jsonify([])
    urlaub_id = con.execute("SELECT id FROM categories WHERE name='Urlaub'").fetchone()
    affected = []
    if request.form.get("on"):
        con.execute("INSERT OR IGNORE INTO urlaub_groups(partner) VALUES(?)", (name,))
        if urlaub_id:
            for t in con.execute(
                "SELECT id, extra FROM transactions WHERE source='splitwise' AND partner=? AND status='active'", (name,)
            ):
                ex = json.loads(t["extra"] or "{}")
                if ex.get("is_payment"):
                    continue
                con.execute("UPDATE transactions SET category_id=? WHERE id=?", (urlaub_id["id"], t["id"]))
                affected.append({"id": t["id"], "category_id": urlaub_id["id"]})
    else:
        con.execute("DELETE FROM urlaub_groups WHERE partner=?", (name,))
        if urlaub_id:
            for t in con.execute(
                "SELECT id, description, extra FROM transactions WHERE source='splitwise' "
                "AND partner=? AND status='active' AND category_id=?",
                (name, urlaub_id["id"]),
            ):
                ex = json.loads(t["extra"] or "{}")
                if ex.get("is_payment"):
                    continue
                cat_name = splitwise_category_name(con, t["description"])
                cat = (
                    con.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone() if cat_name else None
                )
                new_cat = cat["id"] if cat else None
                con.execute("UPDATE transactions SET category_id=? WHERE id=?", (new_cat, t["id"]))
                affected.append({"id": t["id"], "category_id": new_cat})
    con.commit()
    con.close()
    return jsonify(affected) if request.form.get("ajax") else redirect(url_for("splitwise_page"))


def _link_candidates(con, sw, q_text):
    """GET page: already linked payments (pre-ticked) and further candidates
    - all still unlinked bank/paypal/card transactions, sorted by date
    proximity to the Splitwise position; the search only narrows down
    (deliberately not prefilled with the Splitwise description, 2026-08-08)."""
    linked_ids = [
        m["bank_tx"]
        for m in con.execute("SELECT bank_tx FROM matches WHERE other_tx=? AND kind='splitwise_share'", (sw["id"],))
    ]
    used_elsewhere = {
        m["bank_tx"]
        for m in con.execute("SELECT bank_tx FROM matches WHERE kind='splitwise_share' AND other_tx!=?", (sw["id"],))
    }
    linked_rows = (
        list(con.execute("SELECT * FROM transactions WHERE id IN (%s)" % ",".join("?" * len(linked_ids)), linked_ids))
        if linked_ids
        else []
    )
    q = (
        "SELECT * FROM transactions WHERE status='active' AND source IN ('bank','paypal','card') "
        "AND effective_amount IS NULL"
    )
    args = []
    if q_text:
        q += " AND (partner LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        args += [f"%{like_escape(q_text)}%", f"%{like_escape(q_text)}%"]
    q += " ORDER BY ABS(julianday(booking_date)-julianday(?)) LIMIT 200"
    args.append(sw["booking_date"])
    candidates = [c for c in con.execute(q, args) if c["id"] not in used_elsewhere and c["id"] not in linked_ids]
    return linked_rows, candidates


@app.route("/splitwise/link/<int:sw_id>", methods=["GET", "POST"])
def splitwise_link(sw_id):
    """Link an own advanced Splitwise position (bal>0) by hand with one or
    more bank transactions - for everything the cent-exact automation
    (auto_link_own_advances) deliberately leaves alone. No tolerance, the
    human confirms. POST always dissolves the existing link completely first
    and rebuilds it from the selection (add, remove, deselect all = undo, one
    form); the calculation lives in splitwise.link_own_advance."""
    con = db()
    sw = con.execute("SELECT * FROM transactions WHERE id=? AND source='splitwise'", (sw_id,)).fetchone()
    if not sw:
        con.close()
        return _back_to_group(sw_id)
    ex = json.loads(sw["extra"] or "{}")
    cost = ex.get("cost", 0)
    share = q(cost - sw["amount"])
    if request.method == "POST":
        tx_ids = [int(i) for i in request.form.getlist("tx_ids") if str(i).strip().isdigit()]
        res = link_own_advance(con, sw, tx_ids)
        con.commit()
        con.close()
        if res is None:
            flash("Verknüpfung aufgehoben (keine Zahlung mehr ausgewählt)")
            return _back_to_group(sw_id)
        if not res["ok"]:
            flash("FEHLER Auswahl ungültig (anderweitig schon verknüpft?) – Verknüpfung wurde zurückgesetzt")
            return redirect(url_for("splitwise_link", sw_id=sw_id))
        msg = (
            f"Verknüpft: {res['n']} Zahlung(en), Summe {res['total']:.2f}€ → zählt jetzt mit "
            f"{res['new_total']:.2f}€ (dein Splitwise-Anteil daran: {share:.2f}€ von {cost:.2f}€)"
        )
        if res["diff"] > 1.0:
            msg += f" – Achtung, Summe weicht um {res['diff']:.2f}€ vom Splitwise-Betrag ({cost:.2f}€) ab"
        flash(msg)
        return _back_to_group(sw_id)
    q_text = request.args.get("q", "")
    linked_rows, candidates = _link_candidates(con, sw, q_text)
    con.close()
    return render_template(
        "splitwise_link.html", sw=sw, cost=cost, share=share, q=q_text, linked_rows=linked_rows, candidates=candidates
    )


@app.route("/splitwise/unlink/<int:sw_id>", methods=["POST"])
def splitwise_unlink(sw_id):
    """Undo a link (created manually or automatically): reset
    effective_amount + note on the bank side, delete the matches row. A future
    import may then link the position automatically again if it meets the
    strict criteria of auto_link_own_advances - otherwise it stays open until
    it is linked manually again."""
    con = db()
    unlink_splitwise_position(con, sw_id)
    con.commit()
    con.close()
    flash("Verknüpfung aufgehoben")
    return _back_to_group(sw_id)


@app.route("/splitwise/dismiss/<int:sw_id>", methods=["POST"])
def splitwise_dismiss(sw_id):
    """Hide an own advance WITHOUT marking it as linked or tagging the whole
    group as holiday. Originally (user request 2026-08-08) meant for bank
    transactions that cannot be found - but since promote_own_advance_credits()
    (2026-08-14) the share credit runs automatically on EVERY import, with no
    bank transaction needed at all. So that "Ignorieren" still has an effect
    (not just a pointless flag on a position that was booked automatically
    long ago), it additionally reverts a credit that was already created
    automatically (recognisable by the fixed note "Anteil-Gutschrift ..." - a
    category set by the user is NEVER touched). "dismissed" lives in the
    existing `extra` JSON of the Splitwise row, no new table needed - and
    also prevents the next import from crediting the position automatically
    again."""
    con = db()
    t = con.execute(
        "SELECT extra, status, note FROM transactions WHERE id=? AND source='splitwise'", (sw_id,)
    ).fetchone()
    if t:
        ex = json.loads(t["extra"] or "{}")
        ex["dismissed"] = True
        con.execute("UPDATE transactions SET extra=? WHERE id=?", (json.dumps(ex), sw_id))
        if t["status"] == "active" and (t["note"] or "").startswith("Anteil-Gutschrift"):
            con.execute("UPDATE transactions SET status='info', category_id=NULL, note='' WHERE id=?", (sw_id,))
        con.commit()
        flash(
            "Ignoriert"
            + (
                " – automatische Gutschrift zurückgenommen"
                if t["status"] == "active" and (t["note"] or "").startswith("Anteil-Gutschrift")
                else ""
            )
        )
    con.close()
    return _back_to_group(sw_id)


@app.route("/splitwise/undismiss/<int:sw_id>", methods=["POST"])
def splitwise_undismiss(sw_id):
    """Reverse of splitwise_dismiss() - the position gets its "verknuepfen..."
    (link) action back in its group and the automation may touch it again on
    the next import."""
    con = db()
    t = con.execute("SELECT extra FROM transactions WHERE id=? AND source='splitwise'", (sw_id,)).fetchone()
    if t:
        ex = json.loads(t["extra"] or "{}")
        ex.pop("dismissed", None)
        con.execute("UPDATE transactions SET extra=? WHERE id=?", (json.dumps(ex), sw_id))
        con.commit()
        flash("Wieder in der Übersicht sichtbar")
    con.close()
    return _back_to_group(sw_id)
