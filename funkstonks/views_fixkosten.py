"""Routes: recurring costs, overrides, series page."""

import json

from flask import flash, redirect, render_template, request, url_for

from .config import BadInput
from .db import db, get_setting_list
from .recurring import _series_repayments, _series_senders, _series_term, detect_recurring, recurring_groups
from .repay import auto_link_repayments, unlink_repayment
from .web import app, form_int


@app.route("/fixkosten")
def fixkosten():
    con = db()
    series, rejected = detect_recurring(con, with_rejected=True)
    _series_repayments(con, series)
    wohnen, privat, beendet, sums, hidden = recurring_groups(con, with_hidden=True, series=series)
    allowed = get_setting_list(con, "fixkosten_privat_cats")
    # Search "Von Hand aufnehmen" (add by hand): all rejected groups, regardless
    # of category and whether dismissed (user 2026-10-14: "ich verstehe nicht,
    # wie ich hier eine neue zahlung aufnehmen kann" - he could not see how to
    # add a new payment here).
    suche = (request.args.get("suche") or "").strip()
    found = []
    if suche:
        _, alle = detect_recurring(con, with_rejected=True, all_rejected=True)
        found = [g for g in alle if suche.lower() in g["partner"].lower() or suche.lower() in g["key"].lower()]
    con.close()
    return render_template(
        "fixkosten.html",
        wohnen=wohnen,
        privat=privat,
        beendet=beendet,
        sums=sums,
        hidden=hidden,
        candidates=rejected,
        allowed_cats=allowed,
        suche=suche,
        found=found,
    )


@app.route("/fixkosten/override", methods=["POST"])
def fixkosten_override():
    """Hide a series by hand (mode=hide), force it as recurring cost
    (mode=force), dismiss a suggestion (mode=dismiss) or take the decision
    back (mode=clear). User request 2026-10-10 - replaces the earlier stance
    "no hiding, the category filter is the criterion"."""
    key = (request.form.get("key") or "").strip()
    mode = request.form.get("mode") or ""
    label = (request.form.get("label") or "").strip()[:80]
    target = (request.form.get("target") or "").strip()
    back = request.form.get("back") or ""
    if not key or mode not in ("hide", "force", "clear", "dismiss", "merge"):
        raise BadInput("Ungültige Auswahl.")
    if mode == "merge" and (not target or target == key):
        raise BadInput("Zielserie fehlt.")
    con = db()
    if mode == "clear":
        con.execute("DELETE FROM series_overrides WHERE key=?", (key,))
        flash("„" + (label or key) + "“ wird wieder automatisch beurteilt")
    else:
        con.execute(
            "INSERT OR REPLACE INTO series_overrides(key, mode, label, target) VALUES(?,?,?,?)",
            (key, mode, label, target if mode == "merge" else ""),
        )
        flash(
            "„"
            + (label or key)
            + "“ "
            + {
                "hide": "aus den laufenden Kosten ausgeblendet",
                "force": "als laufende Kosten aufgenommen",
                "dismiss": "wird nicht mehr vorgeschlagen",
                "merge": "zur Serie dazugenommen",
            }[mode]
        )
    con.commit()
    con.close()
    if back.startswith("/fixkosten/serie"):
        return redirect(back)
    return redirect(url_for("fixkosten"))


@app.route("/fixkosten/serie", methods=["GET", "POST"])
@app.route("/fixkosten/eingaenge", methods=["GET", "POST"])
def fixkosten_serie():
    """Edit one series (pencil on /fixkosten, since 2026-10-14): assign
    incoming payments, add further transaction groups (merge), hide.
    Assigning incoming payments to a series (user 2026-10-13: "wenn ich das
    ganze Abo zahle und dann hier Geld bekomme, moechte ich diese Posten
    kombinieren" - when he pays the whole subscription and gets money back,
    he wants to combine those items). The page shows incoming payments grouped
    by sender; a tick creates a repayment rule "sender -> series" and nets
    immediately, an unticked box deletes the rule and undoes the links of this
    series. The rule stays visible under rules but never has to be touched
    there."""
    key = (request.values.get("key") or "").strip()
    con = db()
    series, alle = detect_recurring(con, with_rejected=True, all_rejected=True)
    s = next((x for x in series if x.get("key") == key), None)
    if not s:
        con.close()
        flash("FEHLER Serie nicht gefunden.")
        return redirect(url_for("fixkosten"))
    term = _series_term(s)
    ids = set(s.get("ids", []))
    rows, rules = _series_senders(con, term, ids)
    if request.method == "POST":
        wanted = {p.strip().lower() for p in request.form.getlist("partners") if p.strip()}
        by_rule = {}
        for g in rows:
            if g["rule"]:
                by_rule.setdefault(g["rule"].lower(), []).append(g)
        added = removed = 0
        warn = []
        for g in rows:
            want = g["partner"].lower() in wanted
            if want and not g["rule"]:
                con.execute(
                    "INSERT INTO repay_rules(in_pattern,out_pattern,window_days) VALUES(?,?,45)", (g["partner"], term)
                )
                added += 1
            elif g["rule"] and not want:
                others = [
                    o["partner"]
                    for o in by_rule.get(g["rule"].lower(), [])
                    if o is not g and o["partner"].lower() in wanted
                ]
                if others:
                    # shared pattern ("Spotify" in the purpose text) - the rule
                    # is also attached to ticked senders, so it stays
                    warn.append(
                        f"„{g['partner']}“ hängt an der Regel „{g['rule']}“, die auch "
                        + ", ".join(others)
                        + " trifft – zum Trennen das Muster unter Regeln ändern."
                    )
                    continue
                con.execute(
                    "DELETE FROM repay_rules WHERE lower(in_pattern)=lower(?) AND lower(out_pattern)=lower(?)",
                    (g["rule"], term),
                )
                for inc in g["linked_ids"]:
                    unlink_repayment(con, inc)
                removed += 1
        n = auto_link_repayments(con)
        con.commit()
        con.close()
        flash(
            f"Eingänge für „{s['partner']}“ gespeichert: {added} Absender dazu, {removed} entfernt, "
            f"{n} Zahlung{'en' if n != 1 else ''} verrechnet"
        )
        for w in warn:
            flash("WARNUNG " + w)
        return redirect(url_for("fixkosten"))
    payments = con.execute(
        "SELECT id, booking_date, partner, amount, effective_amount FROM transactions "
        "WHERE id IN (%s) ORDER BY booking_date DESC" % (",".join("?" * len(ids)) or "-1"),
        list(ids),
    ).fetchall()
    # Transactions removed from this series by hand (extra.series_skip = key)
    skipped = [
        t
        for t in con.execute(
            "SELECT id, booking_date, partner, amount, effective_amount, extra FROM transactions "
            "WHERE extra LIKE '%series_skip%' ORDER BY booking_date DESC"
        )
        if json.loads(t["extra"] or "{}").get("series_skip") == key
    ]
    # Merge candidates: rejected groups (all categories) and other series;
    # already merged sources get a "wieder trennen" (split again) action.
    merged = con.execute("SELECT key, label FROM series_overrides WHERE mode='merge' AND target=?", (key,)).fetchall()
    others = [x for x in series if x.get("key") != key and not x.get("key", "").startswith("fixed:")]
    mode = con.execute("SELECT mode FROM series_overrides WHERE key=?", (key,)).fetchone()
    con.close()
    return render_template(
        "fixkosten_serie.html",
        s=s,
        term=term,
        rows=rows,
        payments=payments,
        candidates=alle,
        others=others,
        merged=merged,
        skipped=skipped,
        mode=mode["mode"] if mode else "",
    )


@app.route("/fixkosten/serie/skip", methods=["POST"])
def fixkosten_serie_skip():
    """Take a single transaction out of a series (undo=1: put it back).
    Remembers the series key in `extra.series_skip`; detect_recurring skips
    such transactions in both branches."""
    key = (request.form.get("key") or "").strip()
    tx_id = form_int("tx")
    undo = request.form.get("undo") == "1"
    if not key:
        raise BadInput("Serie fehlt.")
    con = db()
    t = con.execute("SELECT id, partner, extra FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not t:
        con.close()
        raise BadInput("Zahlung nicht gefunden.")
    ex = json.loads(t["extra"] or "{}")
    if undo:
        ex.pop("series_skip", None)
    else:
        ex["series_skip"] = key
    con.execute("UPDATE transactions SET extra=? WHERE id=?", (json.dumps(ex), tx_id))
    con.commit()
    still = any(x.get("key") == key for x in detect_recurring(con))
    con.close()
    if undo:
        flash(f"Zahlung von „{t['partner']}“ zählt wieder zur Serie")
    elif still:
        flash(f"Zahlung von „{t['partner']}“ aus der Serie genommen")
    else:
        flash(
            f"WARNUNG Zahlung von „{t['partner']}“ herausgenommen – die Serie hat jetzt zu wenige "
            "Zahlungen und steht nicht mehr in den laufenden Kosten (Vorschläge/Suche nehmen sie wieder auf)."
        )
        return redirect(url_for("fixkosten"))
    return redirect(url_for("fixkosten_serie", key=key))
