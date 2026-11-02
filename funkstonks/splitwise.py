"""Splitwise model: balance per group, promoting shares, linking own advances,
cash pool, settlement payments, amount reconciliation, splits."""

import json
from datetime import date, timedelta

from .db import (
    fixed_cost_like_sql,
    fixed_cost_patterns,
    fixed_cost_rows,
    get_setting,
    is_fixed_cost_group,
    system_cat_id,
)
from .importers import (
    demote_splitwise_position,
    is_fixed_cost_position,
    is_shared_fixed_cost_position,
    tracking_start,
    unlink_splitwise_position,
)
from .money import EPS, eq, is_zero, q
from .recurring import _norm_partner
from .rules import splitwise_category_name

# ONE rule for "account booking <-> Splitwise settlement payment", shared by the
# reconciliation on /splitwise and the automation (user 2026-10-15). Before, the
# page checked +-4 days cent-exact and the automation +-5 days with +-2 %, so a
# settlement could be cleared automatically but still be shown as "nicht
# gefunden". The tolerance is the deliberate exception to "no tolerance
# matches": settle-up transfers are rounded by hand (139.73 -> 140.00 and
# 126.45 -> 126.00 in the real data, one of them netted manually by the user).
SETTLEMENT_WINDOW_DAYS = 5
SETTLEMENT_TOLERANCE = 0.02  # of the Splitwise amount, at least 1 cent


def settlement_fits(bank_amount, bank_day, expected_amount, settlement_day):
    """Does an account booking fit a settlement payment? Signed amounts (same
    direction), +-SETTLEMENT_TOLERANCE, +-SETTLEMENT_WINDOW_DAYS; the days are dates."""
    if (bank_amount > 0) != (expected_amount > 0):
        return False
    tol = max(0.011, SETTLEMENT_TOLERANCE * abs(expected_amount))
    return abs(bank_amount - expected_amount) <= tol and abs((bank_day - settlement_day).days) <= SETTLEMENT_WINDOW_DAYS


def splitwise_overview(con):
    """Per group: balance + reconciliation of the settlement payments with the
    bank. saldo/n/last ALWAYS sum the full history (cross-check against the
    real Splitwise app) - regardless of which slice of the payments list is
    displayed later."""
    groups = {}
    bank = con.execute("SELECT * FROM transactions WHERE source IN ('bank','paypal','card')").fetchall()
    for t in con.execute("SELECT * FROM transactions WHERE source='splitwise' ORDER BY booking_date"):
        ex = json.loads(t["extra"] or "{}")
        g = groups.setdefault(
            t["partner"],
            {"saldo": 0.0, "payments": [], "n": 0, "members": ex.get("members", []), "last": t["booking_date"]},
        )
        g["saldo"] += t["amount"]
        g["n"] += 1
        g["last"] = max(g["last"], t["booking_date"])
        if ex.get("is_payment"):
            # bal==0: settlement between two OTHER group members (e.g. "A paid
            # B" in a big group) - does not concern me, per the user not of
            # interest, do not show.
            if is_zero(t["amount"]):
                continue
            # bal>0: I paid (money out) | bal<0: I received (money in)
            expected = -ex.get("cost", 0) if t["amount"] > 0 else ex.get("cost", 0)
            sd = date.fromisoformat(t["booking_date"])
            found = None
            for b in bank:
                if settlement_fits(b["amount"], date.fromisoformat(b["booking_date"]), expected, sd):
                    found = b
                    break
            g["payments"].append(
                {
                    "date": t["booking_date"],
                    "desc": t["description"],
                    "amount": expected,
                    "bank": found,
                    "id": t["id"],
                    "category_id": t["category_id"],
                }
            )
    for g in groups.values():
        # a floating-point remainder like -0.004 must neither display "-0.00 EUR"
        # nor prevent closing the group (saldo == 0)
        g["saldo"] = q(g["saldo"]) + 0.0
    return groups


# Raw Splitwise categories (sw_category text) that are already counted via the
# halved fixed-cost bookings on the bank account (the configured fixed costs) -
# they must NOT show up again in the settlement composition, otherwise the same
# euro is counted twice.
# The mapping "Splitwise category -> bank payee" now comes from the configured
# fixed costs (field sw_keyword). Basis for the amount reconciliation - only
# where this mapping is unambiguous can one say at all whether Splitwise holds
# the wrong amount.
def sw_fixed_to_partner(con):
    out = {}
    for r in fixed_cost_rows(con):
        for k in (r["sw_keyword"] or "").split(","):
            k = k.strip().lower()
            if k and (r["pattern"] or "").strip():
                out[k] = r["pattern"]
    return out


SW_AMOUNT_TOLERANCE = 1.00  # up to 1 EUR deviation = rounding/small change, no hint
SW_AMOUNT_WINDOW_DAYS = 31  # the booking day drifts (see wohnung_fixed_display_months)


def splitwise_amount_mismatches(con):
    """Splitwise positions whose entered amount matches no bank booking.

    User request 2026-09-01: "wenn ich eine zahlung am konto erhalte und diese
    dann zB falsch in Splitwise eingetragen wurde. zb miete tatsaechlich
    bezahlt: 1000 EUR, aber in splitwise wurden nur 900 EUR eingetragen - dann
    haette ich gerne irgend eine rote kennzeichnung" (rent actually paid 1000,
    but only 900 entered in Splitwise - I want some red marker). Otherwise
    such a typo is never noticed: the Splitwise page only shows what was
    entered, the account only what was debited - nobody compares the two.

    Comparison deliberately happens ONLY where the mapping is unambiguous:
    the four flat fixed costs (field sw_keyword of the settings). Arbitrary
    positions have no reliable anchor - a "Spar 12,80" cannot be safely
    assigned to one particular bank booking, and guessed warnings would be
    worse than none.

    A warning is raised only if within a window of +/-31 days NO booking of
    this payee with a matching amount exists. That covers the known cycle
    drift (rent sometimes at the end, sometimes at the start of the month,
    occasionally twice in the same calendar month) without false alarms.
    """
    # Lower bound deliberately extended backwards by the comparison window: the
    # fixed costs sit in Splitwise on the 1st of the month, the bank booking
    # often arrives days later - if tracking_start lies in between (it is the
    # earliest BANK date), the first position would silently drop out.
    # Lower bound is the configured cutoff, otherwise the tracking window.
    # Without it, positions BEFORE the evaluated period were reported, for which
    # there naturally is no bank data ("nothing found") - a hint nobody can
    # resolve (real case: a position of 10.04. with cutoff 01.05.).
    cutoff = get_setting(con, "data_cutoff") or ""
    start = tracking_start(con)
    scan_from = (date.fromisoformat(start) - timedelta(days=SW_AMOUNT_WINDOW_DAYS)).isoformat()
    if cutoff:
        scan_from = max(scan_from, cutoff)
    bank = con.execute(
        "SELECT booking_date, partner, amount FROM transactions "
        "WHERE source IN ('bank','paypal','card') AND status!='deleted' "
        "AND booking_date >= date(?, '-40 days')",
        (scan_from,),
    ).fetchall()
    rows = con.execute(
        "SELECT id, partner, booking_date, description, extra FROM transactions "
        "WHERE source='splitwise' AND booking_date >= ? ORDER BY booking_date",
        (scan_from,),
    ).fetchall()
    sw_to_partner = sw_fixed_to_partner(con)
    out = {}
    for r in rows:
        try:
            extra = json.loads(r["extra"] or "{}")
        except ValueError:
            continue
        if extra.get("is_payment"):
            continue
        # Matching goes by the DESCRIPTION, not by the Splitwise category
        # (which is not maintained).
        desc = (r["description"] or "").lower()
        if not is_fixed_cost_position(con, r["description"]):
            continue
        pattern = next((p for k, p in sw_to_partner.items() if k in desc), None)
        if not pattern:
            continue
        try:
            cost = abs(float(extra.get("cost") or 0))
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        try:
            sw_day = date.fromisoformat(r["booking_date"])
        except ValueError:
            continue
        cands = []
        for b in bank:
            if pattern.lower() not in ((b["partner"] or "").lower()):
                continue
            try:
                bd = date.fromisoformat(b["booking_date"])
            except ValueError:
                continue
            if abs((bd - sw_day).days) <= SW_AMOUNT_WINDOW_DAYS:
                cands.append(b)
        if any(abs(abs(b["amount"]) - cost) <= SW_AMOUNT_TOLERANCE for b in cands):
            continue
        nearest = min(cands, key=lambda b: abs(abs(b["amount"]) - cost)) if cands else None
        out.setdefault(r["partner"], []).append(
            {
                "id": r["id"],
                "date": r["booking_date"],
                "description": r["description"],
                "sw_category": extra.get("sw_category") or "",
                "cost": cost,
                "partner": pattern,
                "bank_date": nearest["booking_date"] if nearest else None,
                "bank_amount": abs(nearest["amount"]) if nearest else None,
                "diff": (abs(nearest["amount"]) - cost) if nearest else None,
                "confirmed": bool(extra.get("amount_ok")),
            }
        )
    return out


def find_splitwise_settlement(con, tx):
    """Does a bank/PayPal booking match a Splitwise settlement payment?
    (settlement_fits: direction, +/-2 %, +/-5 days). Pure search function
    without side effects - shared by settle_composition() (manual "So
    aufteilen" preview) and auto_umbuchung_splitwise_settlements()
    (automatic marking as Umbuchung on import)."""
    if tx["source"] not in ("bank", "paypal") or abs(tx["amount"]) < 0.01:
        return None  # settlements via PayPal (friends & family) count too
    td = date.fromisoformat(tx["booking_date"])
    best = None
    for p in con.execute("SELECT * FROM transactions WHERE source='splitwise' ORDER BY booking_date"):
        ex = json.loads(p["extra"] or "{}")
        # bal==0 is a settlement between two OTHER members - never mine
        if not ex.get("is_payment") or is_zero(p["amount"]):
            continue
        # bal>0: I paid (money out) | bal<0: I received (money in)
        expected = -ex.get("cost", 0) if p["amount"] > 0 else ex.get("cost", 0)
        pd = date.fromisoformat(p["booking_date"])
        if not settlement_fits(tx["amount"], td, expected, pd):
            continue
        dd = abs((pd - td).days)
        if best is None or dd < best[1]:
            best = (p, dd)
    return best[0] if best else None


def promote_splitwise_shares(con):
    """Individual Splitwise expenses (not settlement payments) with a negative
    balance (= the user owes his share, someone else paid up front) are
    promoted to a counting expense in "Alle Zahlungen": date = original
    purchase (not the settlement), amount = own share (amount/bal), note
    "bezahlt von <member(s)>". A positive/zero balance means the user paid
    himself and it already counts via bank/PayPal/card - do NOT promote,
    otherwise double counting. Flat group: positions with the configured
    Splitwise categories are excluded (already counted via the halved bank
    fixed costs, see auto_split_wohnung_fixed). status='info' as condition
    makes this idempotent AND leaves manually recategorized (then
    status='active') positions alone on future imports.
    Only positions from tracking_start() on are promoted - as for bank/
    PayPal/card (cleanup_before_start) - the full Splitwise history before
    that is kept only for the balance cross-check (splitwise_overview()
    deliberately sums everything regardless of status) but is NOT pulled into
    Alle Zahlungen/dashboard.
    Note "bezahlt von <name>" only for 2-person groups (e.g. "wohnung") -
    there the one other member is clearly the payer. In bigger groups (e.g. a
    friends group of 19 people) the Splitwise data does NOT tell who actually
    paid this one position (only who is in the group) - "bezahlt von Alice,
    Bob, Carl, ..." would falsely suggest everyone chipped in. Instead the
    neutral "bezahlt in Gruppe <name>". Groups in `urlaub_groups` (see the
    /splitwise checkbox "Als Urlaub markieren") ALWAYS get "Urlaub" instead
    of the normal `map_splitwise_category()` mapping - user request
    2026-08-06: a whole travel group should not have to be sorted position
    by position."""
    start = tracking_start(con)
    urlaub_partners = {r["partner"] for r in con.execute("SELECT partner FROM urlaub_groups")}
    n = 0
    for t in con.execute(
        "SELECT * FROM transactions WHERE source='splitwise' AND status='info' AND booking_date>=?", (start,)
    ):
        ex = json.loads(t["extra"] or "{}")
        if ex.get("is_payment"):
            continue
        if t["amount"] >= -EPS:
            continue
        if is_shared_fixed_cost_position(con, t):
            continue
        if t["partner"] in urlaub_partners:
            cat_name = "Urlaub"
        else:
            cat_name = splitwise_category_name(con, t["description"])
        cat = con.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone() if cat_name else None
        members = ex.get("members") or []
        if len(members) == 1:
            note = f"bezahlt von {members[0]}"
        else:
            note = f"bezahlt in Gruppe {t['partner']}"
        con.execute(
            "UPDATE transactions SET status='active', category_id=?, note=? WHERE id=?",
            (cat["id"] if cat else None, note, t["id"]),
        )
        n += 1
    return n


def auto_link_own_advances(con):
    """Mirror image of promote_splitwise_shares: there bal<0 (someone else
    paid up front, his share is still missing). Here bal>0 - the user himself
    paid more than his own share (e.g. a whole group accommodation up front)
    - the associated bank/PayPal/card booking so far counts with the FULL
    amount as his expense although only `cost - bal` really is his share (the
    rest comes back via the group's general settlement payment, see
    auto_umbuchung_splitwise_settlements).
    Links automatically only if EXACTLY ONE booking matches to the cent AND
    lies within +/-3 days - or (since 2026-09-15) exactly one combination of
    two to three bookings of the SAME payee that together match to the cent
    (Amazon splits orders into shipments); the share is then distributed by
    amount weight as in manual linking. If the position was previously
    resolved only via cash pool/credit (the bank booking arrived later), that
    is rolled back and the exact match takes over. Calibrated on real
    account data (2026-08-02): with a larger tolerance/time window there
    were proven false matches on round amounts (20/50 EUR collide too easily
    with independent bookings in real expenses); with exact cent amount + a
    3-day window none were observed on the same data (more positions stay
    unlinked instead - deliberate trade-off: better touch nothing than
    correct wrongly).
    Runs for ALL Splitwise groups (not only "wohnung", e.g. also bigger
    friends groups - a group Airbnb paid up front works the same way).
    Positions of the configured fixed costs (pattern or Splitwise keyword,
    see is_shared_fixed_cost_position) are excluded - they run entirely via
    auto_split_wohnung_fixed. Sets ONLY effective_amount, never category_id
    (the user keeps categorizing these bookings himself as usual).
    The link is recorded in `matches` (kind='splitwise_share') - makes it
    idempotent, a future import never touches a once-linked booking again."""
    from itertools import combinations

    n = 0
    start = tracking_start(con)
    bank_pool = list(
        con.execute(
            "SELECT * FROM transactions WHERE source IN ('bank','paypal','card') "
            "AND status='active' AND effective_amount IS NULL AND amount<0"
        )
    )
    for t in con.execute(
        "SELECT * FROM transactions WHERE source='splitwise' AND booking_date>=? ORDER BY booking_date", (start,)
    ):
        ex = json.loads(t["extra"] or "{}")
        if ex.get("is_payment") or ex.get("dismissed") or t["amount"] <= EPS:
            continue
        if is_shared_fixed_cost_position(con, t):
            continue
        # Already resolved? A manual or exact automatic link stays. A solution
        # via cash pool/credit is only second best - if the bank booking
        # arrives after the Splitwise entry (Amazon books days later, often in
        # two parts), from now on the exact match wins and the stopgap is
        # rolled back (real case 2026-09-15: "elektrische buerste",
        # 78,60 = 28,24 + 50,36 three days later).
        link_notes = [
            r["note"] or ""
            for r in con.execute(
                "SELECT b.note FROM matches m JOIN transactions b ON b.id=m.bank_tx "
                "WHERE m.other_tx=? AND m.kind='splitwise_share'",
                (t["id"],),
            )
        ]
        credited = t["status"] == "active" and (t["note"] or "").startswith("Anteil-Gutschrift")
        if link_notes and not all(nt.startswith("automatisch mit Bargeld-Abhebung") for nt in link_notes):
            continue
        if t["status"] == "active" and not credited:
            continue  # booked by the user himself
        cost = ex.get("cost", 0)
        sd = date.fromisoformat(t["booking_date"])
        # Card bookings appear in the export only days after the purchase
        # (booking instead of purchase date) - for them +/-7 days apply,
        # otherwise +/-3. Real case 2026-10-10: flights 532,08 bought by card
        # on 25.09., booked on 30.09., Splitwise position of 25.09. - went into
        # the cash pool instead of to the card booking. Cent-exact stays mandatory.
        near = [
            b
            for b in bank_pool
            if abs((date.fromisoformat(b["booking_date"]) - sd).days) <= (7 if b["source"] == "card" else 3)
        ]
        exact = [b for b in near if eq(abs(b["amount"]), cost)]
        combo = None
        if len(exact) == 1:
            combo = exact
        elif not exact:
            # partial bookings of the SAME payee that together match exactly
            # (Amazon splits orders into shipments) - only with exactly one
            # possible combination, otherwise better touch nothing.
            by_partner = {}
            for b in near:
                by_partner.setdefault(_norm_partner(b["partner"]), []).append(b)
            found = []
            for group in by_partner.values():
                for r in (2, 3):
                    for cmb in combinations(group, r):
                        if abs(sum(abs(x["amount"]) for x in cmb) - cost) < 0.005:
                            found.append(list(cmb))
            if len(found) == 1:
                combo = found[0]
        if not combo:
            continue
        if link_notes or credited:
            # roll back the stopgap: cash shares back to the withdrawal, the
            # credit row back to a raw position
            unlink_splitwise_position(con, t["id"])
            if credited:
                demote_splitwise_position(con, t["id"])
        total = q(sum(abs(b["amount"]) for b in combo))
        bal = t["amount"]
        given = 0.0
        for i, b in enumerate(combo):
            # own share = cost - bal; distribute bal over the partial bookings
            # by amount weight, rounding remainder onto the last one
            part = q(bal - given) if i == len(combo) - 1 else q(bal * abs(b["amount"]) / total)
            given += part
            con.execute(
                "UPDATE transactions SET effective_amount=?, note=? WHERE id=?",
                (q(b["amount"] + part), "automatisch mit Splitwise verknüpft (eigener Anteil)", b["id"]),
            )
            con.execute(
                "INSERT INTO matches(bank_tx, other_tx, kind, amount) VALUES(?,?,'splitwise_share',?)",
                (b["id"], t["id"], part),
            )
        used = {b["id"] for b in combo}
        bank_pool = [x for x in bank_pool if x["id"] not in used]
        n += 1
    return n


def _cash_pool(con, start):
    """Cash pool: all withdrawals (category Bargeld, negative remainder) since
    tracking start with their still available remainder. A credit in the
    category Bargeld is no pool (2026-10-10)."""
    bargeld = con.execute("SELECT id FROM categories WHERE name='Bargeld'").fetchone()
    pool = []
    if not bargeld:
        return pool
    for b in con.execute(
        "SELECT id, booking_date, amount, effective_amount FROM transactions "
        "WHERE source IN ('bank','paypal','card') AND status='active' AND category_id=? AND booking_date>=?",
        (bargeld["id"], start),
    ):
        cur = b["effective_amount"] if b["effective_amount"] is not None else b["amount"]
        if cur < -EPS:
            pool.append({"id": b["id"], "date": b["booking_date"], "cap": -cur})
    return pool


def _take_from_pool(con, pool, t):
    """Stage 1: cover the share of position `t` from the withdrawals closest
    in time (across several if necessary). Writes effective_amount and
    matches (kind='splitwise_share', amount = delta). Returns (rest, used)."""
    remaining = q(t["amount"])
    pool.sort(key=lambda p: abs((date.fromisoformat(p["date"]) - date.fromisoformat(t["booking_date"])).days))
    used = []
    for p in pool:
        if remaining <= EPS:
            break
        if p["cap"] <= EPS:
            continue
        take = q(min(remaining, p["cap"]))
        used.append((p, take))
        remaining -= take
    for p, take in used:
        bank_row = con.execute("SELECT amount, effective_amount FROM transactions WHERE id=?", (p["id"],)).fetchone()
        cur = bank_row["effective_amount"] if bank_row["effective_amount"] is not None else bank_row["amount"]
        con.execute(
            "UPDATE transactions SET effective_amount=?, note=? WHERE id=?",
            (q(cur + take), "automatisch mit Bargeld-Abhebung verrechnet (Splitwise-Anteil)", p["id"]),
        )
        con.execute(
            "INSERT INTO matches(bank_tx, other_tx, kind, amount) VALUES(?,?,'splitwise_share',?)",
            (p["id"], t["id"], q(take)),
        )
        p["cap"] = q(p["cap"] - take)
    return remaining, used


def _promote_rest_as_credit(con, t, ex, remaining, used, urlaub_partners):
    """Stage 2: promote what the cash pool does not cover as its own credit
    row in the matching category - only the REMAINDER (the covered part
    already counts at the withdrawal, otherwise double; audit 2026-09-13)."""
    cat_name = "Urlaub" if t["partner"] in urlaub_partners else splitwise_category_name(con, t["description"])
    cat = con.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone() if cat_name else None
    cost = ex.get("cost", 0)
    if used:
        note = (
            f"Anteil-Gutschrift (selbst vorgestreckt, {t['amount']:.2f}€ von {cost:.2f}€ "
            f"zurück; Rest {remaining:.2f}€ nach Bargeld-Verrechnung)"
        )
        rest = q(abs(remaining)) * (1 if t["amount"] > 0 else -1)
        con.execute(
            "UPDATE transactions SET status='active', category_id=?, note=?, effective_amount=? WHERE id=?",
            (cat["id"] if cat else None, note, rest, t["id"]),
        )
    else:
        note = f"Anteil-Gutschrift (selbst vorgestreckt, {t['amount']:.2f}€ von {cost:.2f}€ zurück)"
        con.execute(
            "UPDATE transactions SET status='active', category_id=?, note=? WHERE id=?",
            (cat["id"] if cat else None, note, t["id"]),
        )


def promote_own_advance_credits(con):
    """Make own advanced Splitwise positions (bal>0) WITHOUT an exact bank
    match countable (user request 2026-08-14) - catches what
    auto_link_own_advances and manual linking did not find: stage 1 cash
    pool (_take_from_pool), stage 2 remainder as own credit
    (_promote_rest_as_credit). Linked, dismissed and shared fixed-cost
    positions are skipped; status='info' as condition makes it idempotent.
    Groups marked as vacation ALWAYS get Urlaub."""
    start = tracking_start(con)
    linked_sw_ids = {m["other_tx"] for m in con.execute("SELECT other_tx FROM matches WHERE kind='splitwise_share'")}
    urlaub_partners = {r["partner"] for r in con.execute("SELECT partner FROM urlaub_groups")}
    pool = _cash_pool(con, start)
    n = 0
    for t in con.execute(
        "SELECT * FROM transactions WHERE source='splitwise' AND status='info' AND booking_date>=? ORDER BY booking_date",
        (start,),
    ):
        if t["id"] in linked_sw_ids:
            continue
        ex = json.loads(t["extra"] or "{}")
        if ex.get("is_payment") or ex.get("dismissed") or t["amount"] <= EPS:
            continue
        if is_shared_fixed_cost_position(con, t):
            continue
        remaining, used = _take_from_pool(con, pool, t)
        if remaining > EPS:
            _promote_rest_as_credit(con, t, ex, remaining, used, urlaub_partners)
        n += 1
    return n


def link_own_advance(con, sw, tx_ids):
    """Link an own advanced Splitwise position (bal>0) with the bank bookings
    `tx_ids` (manually, /splitwise/link). First dissolves an existing link of
    this position and rolls back an automatic share credit (otherwise the
    share counts twice). effective_amount = amount + bal_part (NOT -share):
    besides the Splitwise share the booking may contain own expenses (real
    case 2026-08-08, Wolt group order) - only what comes back per Splitwise
    is deducted, with several bookings by amount weight, the last one gets
    the rounding remainder. Returns None (nothing selected = unlinked),
    {"ok": False} (invalid selection, position stays unlinked) or
    {"ok": True, "n", "total", "new_total", "diff"}."""
    sw_id = sw["id"]
    if sw["status"] == "active" and (sw["note"] or "").startswith("Anteil-Gutschrift"):
        con.execute("UPDATE transactions SET status='info', category_id=NULL, note='' WHERE id=?", (sw_id,))
    unlink_splitwise_position(con, sw_id)
    if not tx_ids:
        return None
    used_elsewhere = {m["bank_tx"] for m in con.execute("SELECT bank_tx FROM matches WHERE kind='splitwise_share'")}
    cands = [
        c
        for c in con.execute(
            "SELECT * FROM transactions WHERE status='active' AND source IN ('bank','paypal','card') "
            "AND id IN (%s)" % ",".join("?" * len(tx_ids)),
            tx_ids,
        )
        if c["id"] not in used_elsewhere
    ]
    total = q(sum(c["amount"] for c in cands))
    if not cands or total == 0:
        return {"ok": False}
    bal = sw["amount"]
    running = 0.0
    for i, c in enumerate(cands):
        if i == len(cands) - 1:
            bal_part = q(bal - running)
        else:
            bal_part = q(bal * (c["amount"] / total))
            running += bal_part
        con.execute(
            "UPDATE transactions SET effective_amount=?, note='manuell mit Splitwise verknüpft (eigener Anteil)' WHERE id=?",
            (q(c["amount"] + bal_part), c["id"]),
        )
        con.execute(
            "INSERT INTO matches(bank_tx, other_tx, kind, amount) VALUES(?,?,'splitwise_share',?)",
            (c["id"], sw_id, q(bal_part)),
        )
    cost = json.loads(sw["extra"] or "{}").get("cost", 0)
    return {
        "ok": True,
        "n": len(cands),
        "total": total,
        "new_total": q(total + bal),
        "diff": abs(abs(total) - cost),
    }


def auto_umbuchung_splitwise_settlements(con):
    """Automatically mark bank/PayPal bookings that match a Splitwise
    settlement payment as 'Umbuchung' (status='excluded', like PayPal/credit
    card netting) - the associated individual expenses already count
    individually via promote_splitwise_shares(). Without this the settlement
    would count a second time (as income when received, or as a generic
    expense when paid)."""
    umbuchung = None  # create only on the first hit - otherwise a deleted
    n = 0  # "Umbuchung" comes back with every import
    for tx in con.execute(
        "SELECT * FROM transactions WHERE source IN ('bank','paypal') AND status='active' AND category_id IS NULL"
    ).fetchall():
        pay = find_splitwise_settlement(con, tx)
        if not pay:
            continue
        if umbuchung is None:
            umbuchung = system_cat_id(con, "Umbuchung")
        con.execute(
            "UPDATE transactions SET status='excluded', category_id=?, "
            "note='automatisch mit Splitwise-Ausgleichszahlung verrechnet' WHERE id=?",
            (umbuchung, tx["id"]),
        )
        # tx["category_id"] was IS NULL (filter above) - it may still carry a
        # manual "So aufteilen" split (save_splits() sets category_id=NULL).
        # Without cleanup it lingers as an invisible dead record
        # (status!='active' -> splits count nowhere) until a later status
        # change back to active - really happened.
        con.execute("DELETE FROM splits WHERE tx_id=?", (tx["id"],))
        n += 1
    return n


def settle_composition(con, tx):
    """For an incoming bank credit: does it match a Splitwise settlement
    payment? If so: composition by category since the last settlement in the
    group, scaled to the bank amount. For the group 'wohnung' the share
    already counted via the halved fixed-cost bookings (rent/electricity/
    insurance/internet) is deducted as well - it appears here as 'Umbuchung'
    (does not count again). Manual fallback view for "Anpassen..."/"So
    aufteilen" (e.g. when auto_umbuchung_splitwise_settlements() did not find
    a payment automatically) - since promote_splitwise_shares() the normal
    case runs via the automatic Umbuchung marking, no longer via this
    composition (otherwise double counting with the promoted individual
    expenses)."""
    pay = find_splitwise_settlement(con, tx)
    if not pay:
        return None
    group = pay["partner"]
    is_wohnung = is_fixed_cost_group(con, group)
    prev = (
        con.execute(
            "SELECT MAX(booking_date) FROM transactions WHERE source='splitwise' "
            "AND partner=? AND booking_date<? AND json_extract(extra,'$.is_payment')=1",
            (group, pay["booking_date"]),
        ).fetchone()[0]
        or "0000-00-00"
    )
    comp = {}
    for t in con.execute(
        "SELECT * FROM transactions WHERE source='splitwise' AND partner=? "
        "AND booking_date>? AND booking_date<=? "
        "AND json_extract(extra,'$.is_payment')=0",
        (group, prev, pay["booking_date"]),
    ):
        if is_wohnung:
            if is_fixed_cost_position(con, t["description"]):
                continue  # already counted via the halved bank fixed costs
        # no blanket "Wohnung" fallback any more: not every position in the
        # group "wohnung" is actually a housing expense.
        cat = splitwise_category_name(con, t["description"]) or "Sonstiges"
        comp[cat] = comp.get(cat, 0.0) + t["amount"]

    rows = []
    remaining = tx["amount"]
    _sc_sql, _sc_args = fixed_cost_like_sql(fixed_cost_patterns(con))
    if is_wohnung:
        fixed_expected = round(
            con.execute(
                "SELECT COALESCE(SUM(-effective_amount),0) FROM transactions "
                "WHERE source IN ('bank','paypal','card') "
                "AND effective_amount IS NOT NULL AND booking_date>? AND booking_date<=? AND (" + _sc_sql + ")",
                (prev, pay["booking_date"], *_sc_args),
            ).fetchone()[0]
            or 0,
            2,
        )
        if fixed_expected >= 0.01:
            sign = 1 if tx["amount"] > 0 else -1
            fixed_amt = q(min(fixed_expected, abs(tx["amount"]))) * sign
            umbuchung = con.execute("SELECT id FROM categories WHERE name='Umbuchung'").fetchone()
            if umbuchung:
                rows.append(
                    {
                        "category_id": umbuchung["id"],
                        "name": "Umbuchung (Wohnungs-Fixkosten bereits gezählt)",
                        "amount": fixed_amt,
                    }
                )
                remaining = q(tx["amount"] - fixed_amt)

    total = sum(comp.values())
    if abs(total) >= 0.01 and abs(remaining) >= 0.01 and (total > 0) == (remaining > 0):
        f = remaining / total
        for cat, v in sorted(comp.items(), key=lambda kv: -abs(kv[1])):
            a = q(v * f)
            if abs(a) >= 0.01:
                r = con.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone()
                if r:
                    rows.append({"category_id": r["id"], "name": cat, "amount": a})
    if not rows:
        return None
    diff = q(tx["amount"] - sum(r["amount"] for r in rows))
    rows[-1]["amount"] = q(rows[-1]["amount"] + diff)
    return {"group": group, "date": pay["booking_date"], "since": prev, "rows": rows}


def save_splits(con, tx_id, parts):
    """Sets category_id to NULL (splits are the source of truth) and marks
    the note with "[aufgeteilt]" - append only ONCE even if a split is saved/
    adjusted repeatedly (otherwise "[aufgeteilt] [aufgeteilt]" on every
    re-save, was a real bug)."""
    con.execute("DELETE FROM splits WHERE tx_id=?", (tx_id,))
    for cid, amt in parts:
        con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (tx_id, cid, amt))
    note = (con.execute("SELECT note FROM transactions WHERE id=?", (tx_id,)).fetchone()["note"] or "").strip()
    if "[aufgeteilt]" not in note:
        note = (note + " [aufgeteilt]").strip()
    con.execute("UPDATE transactions SET category_id=NULL, note=? WHERE id=?", (note, tx_id))
