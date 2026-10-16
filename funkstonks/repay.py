"""Repayments: offset an incoming payment against an expense (manually and via
repay_rules), undo."""

import json
from datetime import date, timedelta

from .config import BadInput
from .db import system_cat_id
from .money import EPS, eq, q
from .stats import eff

# An incoming payment (a friend transfers his share, an insurer reimburses, a
# merchant refunds) is linked to one or more expenses: the expense then counts
# only with the remainder (effective_amount), the credit itself becomes
# 'excluded' and gets the system category "Rueckzahlung". Before, the only
# option was to put the credit into the same category - correct in total, but
# neither per booking nor in "Laufende Kosten" (Spotify showed 20.99 there
# instead of the own share). User request 2026-10-10. Same delta model as
# splitwise_share: matches.amount = what this link changed on the expense, so
# that several repayments (and a Splitwise share) can stack on the same expense
# and be undone individually.

REPAY_NOTE = "Rückzahlung verknüpft"


def repayment_candidates_sql():
    """Expenses a repayment may be booked against: counting bank/PayPal/card
    bookings with a negative (remaining) amount, no split payments (their
    amount lives in the split parts, effective_amount would be ignored there)."""
    return (
        "status='active' AND source IN ('bank','paypal','card') "
        "AND COALESCE(effective_amount, amount) < -0.004 "
        "AND id NOT IN (SELECT tx_id FROM splits)"
    )


def link_repayment(con, in_id, tx_ids, manual=True):
    """Distribute the incoming payment `in_id` over the expenses `tx_ids`.

    Distribution is proportional to each expense's open remainder (the last
    one gets the rounding remainder); no expense is pushed below 0. If the
    open remainder of all chosen expenses does not cover the whole credit,
    NOTHING happens and a BadInput is raised - better the user picks one more
    expense than a remainder silently vanishing somewhere.
    Returns the list (expense_id, delta)."""
    inc = con.execute("SELECT * FROM transactions WHERE id=?", (in_id,)).fetchone()
    if not inc or inc["source"] not in ("bank", "paypal", "card") or inc["amount"] <= EPS:
        raise BadInput("Nur eine eingehende Konto-, PayPal- oder Kartenzahlung kann eine Rückzahlung sein.")
    if inc["status"] != "active":
        raise BadInput("Diese Zahlung zählt gerade nicht (ausgeschlossen oder schon verknüpft).")
    if con.execute("SELECT 1 FROM matches WHERE other_tx=? AND kind='repayment'", (in_id,)).fetchone():
        raise BadInput("Diese Zahlung ist schon als Rückzahlung verknüpft – erst aufheben.")
    ids = []
    for i in tx_ids:
        if str(i).strip().isdigit() and int(i) not in ids:
            ids.append(int(i))
    if not ids:
        raise BadInput("Keine Ausgabe gewählt.")
    ph = ",".join("?" * len(ids))
    exps = con.execute(
        "SELECT * FROM transactions WHERE id IN ("
        + ph
        + ") AND "
        + repayment_candidates_sql()
        + " ORDER BY booking_date, id",
        ids,
    ).fetchall()
    if len(exps) != len(ids) or in_id in ids:
        raise BadInput(
            "Mindestens eine gewählte Zahlung ist keine offene Ausgabe (aufgeteilt, "
            "ausgeschlossen oder schon komplett zurückgezahlt)."
        )
    rest = {e["id"]: q(-eff(e)) for e in exps}
    total_rest = q(sum(rest.values()))
    amt = q(inc["amount"])
    if amt > total_rest + EPS:
        raise BadInput(
            f"Die Rückzahlung ({amt:.2f} €) ist höher als der offene Betrag der gewählten "
            f"Ausgabe{'n' if len(exps) > 1 else ''} ({total_rest:.2f} €) – eine Ausgabe mehr auswählen."
        )
    parts, given = [], 0.0
    for i, e in enumerate(exps):
        if i == len(exps) - 1:
            part = q(amt - given)
        else:
            part = min(q(amt * rest[e["id"]] / total_rest), rest[e["id"]])
        given = q(given + part)
        parts.append([e, part])
    # The rounding remainder must not push the last expense below 0 - then
    # shift it to an expense with room (with a single expense the check above
    # already covers this).
    over = q(parts[-1][1] - rest[parts[-1][0]["id"]])
    if over > EPS:
        parts[-1][1] = rest[parts[-1][0]["id"]]
        for p in parts[:-1]:
            room = q(rest[p[0]["id"]] - p[1])
            take = min(room, over)
            p[1] = q(p[1] + take)
            over = q(over - take)
            if over <= EPS:
                break
    how = "manuell" if manual else "automatisch"
    done = []
    for e, part in parts:
        if part <= EPS:
            continue
        new_eff = q(eff(e) + part)
        tag = f"{REPAY_NOTE} ({how}, −{part:.2f} €)"
        note = tag if not (e["note"] or "") else (e["note"] + " · " + tag)
        con.execute("UPDATE transactions SET effective_amount=?, note=? WHERE id=?", (new_eff, note[:300], e["id"]))
        con.execute(
            "INSERT INTO matches(bank_tx, other_tx, kind, amount) VALUES(?,?,'repayment',?)", (e["id"], in_id, part)
        )
        done.append((e, part))
    ex = json.loads(inc["extra"] or "{}")
    ex["repay_prev"] = {"category_id": inc["category_id"]}
    targets = ", ".join((e["partner"] or e["description"] or "")[:30] for e, _ in done)
    con.execute(
        "UPDATE transactions SET status='excluded', category_id=?, note=?, extra=? WHERE id=?",
        (
            system_cat_id(con, "Rückzahlung"),
            f"Rückzahlung für: {targets}"[:300],
            json.dumps(ex, ensure_ascii=False),
            in_id,
        ),
    )
    return [(e["id"], p) for e, p in done]


def unlink_repayment(con, in_id):
    """Completely undo the repayment link of an incoming payment: every expense
    gets its delta back (without further links and without a fixed-cost
    reduction it lands at amount again -> effective_amount NULL), the credit
    becomes active again with its former category."""
    inc = con.execute("SELECT * FROM transactions WHERE id=?", (in_id,)).fetchone()
    if not inc:
        return 0
    n = 0
    for m in con.execute(
        "SELECT id, bank_tx, amount FROM matches WHERE other_tx=? AND kind='repayment'", (in_id,)
    ).fetchall():
        e = con.execute("SELECT * FROM transactions WHERE id=?", (m["bank_tx"],)).fetchone()
        if e is None:
            continue
        new_eff = q(eff(e) - (m["amount"] or 0))
        others = con.execute("SELECT COUNT(*) FROM matches WHERE bank_tx=? AND id!=?", (e["id"], m["id"])).fetchone()[0]
        # remove exactly this link's note part, the others stay
        parts = [p for p in (e["note"] or "").split(" · ") if p]
        tag_amt = f"−{(m['amount'] or 0):.2f} €"
        idx = next(
            (i for i, p in enumerate(parts) if p.startswith(REPAY_NOTE) and tag_amt in p),
            next((i for i, p in enumerate(parts) if p.startswith(REPAY_NOTE)), None),
        )
        if idx is not None:
            parts.pop(idx)
        note = " · ".join(parts)
        if not others and eq(new_eff, e["amount"]):
            con.execute("UPDATE transactions SET effective_amount=NULL, note=? WHERE id=?", (note, e["id"]))
        else:
            con.execute("UPDATE transactions SET effective_amount=?, note=? WHERE id=?", (new_eff, note, e["id"]))
        n += 1
    con.execute("DELETE FROM matches WHERE other_tx=? AND kind='repayment'", (in_id,))
    if n:
        ex = json.loads(inc["extra"] or "{}")
        prev = ex.pop("repay_prev", None) or {}
        con.execute(
            "UPDATE transactions SET status='active', category_id=?, note='', extra=? WHERE id=?",
            (prev.get("category_id"), json.dumps(ex, ensure_ascii=False), in_id),
        )
    return n


def is_repayment_linked(con, tx_id):
    return con.execute("SELECT 1 FROM matches WHERE other_tx=? AND kind='repayment'", (tx_id,)).fetchone() is not None


def auto_link_repayments(con):
    """Apply repayment rules (import pipeline, idempotent): "incoming from
    <in_pattern>" is offset against the closest open expense that contains
    <out_pattern> and lies within the window [incoming - window, incoming + 7
    days] (Spotify bills on the 29th, the friends transfer on the 1st/2nd of
    the following month). Only if the expense's open remainder covers the
    WHOLE credit - never partially. On a tie the older expense wins (the
    current bill, not already the next one)."""
    rules = con.execute("SELECT * FROM repay_rules ORDER BY id").fetchall()
    if not rules:
        return 0
    taken = {r[0] for r in con.execute("SELECT other_tx FROM matches WHERE kind='repayment'")}
    incs = [
        t
        for t in con.execute(
            "SELECT * FROM transactions WHERE status='active' AND source IN ('bank','paypal','card') "
            "AND amount > 0.004 ORDER BY booking_date, id"
        )
        if t["id"] not in taken
    ]
    n = 0
    for r in rules:
        ip = (r["in_pattern"] or "").lower().strip()
        op = (r["out_pattern"] or "").lower().strip()
        if not ip or not op:
            continue
        for inc in incs:
            if inc["id"] in taken:
                continue
            if ip not in ((inc["partner"] or "") + " " + (inc["description"] or "")).lower():
                continue
            d = date.fromisoformat(inc["booking_date"])
            lo = (d - timedelta(days=r["window_days"] or 45)).isoformat()
            hi = (d + timedelta(days=7)).isoformat()
            cands = [
                e
                for e in con.execute(
                    "SELECT * FROM transactions WHERE "
                    + repayment_candidates_sql()
                    + " AND booking_date BETWEEN ? AND ?",
                    (lo, hi),
                )
                if op in ((e["partner"] or "") + " " + (e["description"] or "")).lower()
                and -eff(e) >= inc["amount"] - EPS
            ]
            if not cands:
                continue
            cands.sort(key=lambda e: (abs((date.fromisoformat(e["booking_date"]) - d).days), e["booking_date"]))
            link_repayment(con, inc["id"], [cands[0]["id"]], manual=False)
            taken.add(inc["id"])
            n += 1
    return n
