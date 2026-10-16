"""Importers per source (bank, card, PayPal, Splitwise) and Splitwise reconciliation
on re-import, tracking_start/cleanup_before_start."""

import json
import re
import sqlite3
from datetime import date

from .db import db, fixed_cost_patterns, fixed_cost_sw_keywords, get_setting, is_fixed_cost_group, splitwise_name
from .money import is_zero, q
from .parsing import bank_columns, find_col, parse_amount, parse_date, read_csv_flexible
from .rules import apply_rules, hash_with_occurrence, tx_hash

# The cutoff date ("from when on is evaluated") has lived in the settings
# (data_cutoff) since 2026-09-11 instead of hard-coded here. Everything before
# it - bank/paypal/card AND Splitwise - is ignored in lists and evaluations,
# regardless of the actual account start. Does NOT affect the Splitwise balance
# cross-check (which deliberately stays on the full history, see
# splitwise_overview) - "show nowhere" refers to individual payments/lists, not
# to the aggregated balance comparison against the real Splitwise app. Empty =
# no cutoff, everything counts.


def tracking_start(con):
    """Start of the tracked period: the earliest bank booking date, but never
    before the configured cutoff (settings -> "Auswertung ab").

    The cutoff is optional: without it, simply everything from the first
    booking counts. If set, it acts as a lower bound, not a fixed value - if
    the first real booking is later, that one wins.
    """
    cutoff = get_setting(con, "data_cutoff") or ""
    real_min = con.execute("SELECT MIN(booking_date) FROM transactions WHERE source='bank'").fetchone()[0]
    if not cutoff:
        return real_min or "1900-01-01"
    return max(real_min, cutoff) if real_min else cutoff


def cleanup_before_start(con):
    """Remove counting bookings (bank/paypal/card) before tracking_start() -
    tombstones (status='deleted') are kept as dedup anchors.
    Splitwise is NOT deleted HERE: splitwise_overview() sums the ENTIRE
    history of a group for the balance cross-check against the real Splitwise
    app - deleting history before tracking_start makes that balance wrong
    (tried once, was a mistake)."""
    start = tracking_start(con)
    if not start:
        return 0
    cur = con.execute(
        "DELETE FROM transactions WHERE source IN ('bank','paypal','card') AND booking_date<? AND status!='deleted'",
        (start,),
    )
    # Undo orphaned settlements: a bank debit AFTER the cutoff whose PayPal/card
    # receipt lay BEFORE the cutoff and was just deleted was until now "hidden,
    # counts via the receipt" - the receipt is gone, so it is an ordinary
    # expense again. Otherwise it vanishes for good (happened for real:
    # 95.94 EUR skatedeluxe, repaired by hand back then). The matches row is
    # already gone via ON DELETE CASCADE, so "hidden with a settlement category
    # but without a match" suffices.
    # Undo promoted Splitwise shares before the tracking start: promotion only
    # applies from tracking_start() on, but an earlier cutoff may have left
    # them behind (audit 2026-09-13: 4 rows, 59.57 EUR). Manually categorized
    # ones (without an automation note) stay.
    con.execute(
        "UPDATE transactions SET status='info', category_id=NULL, note='' "
        "WHERE source='splitwise' AND status='active' AND booking_date<? "
        "AND (note LIKE 'bezahlt %' OR note LIKE 'Anteil-Gutschrift%')",
        (start,),
    )
    con.execute(
        "UPDATE transactions SET status='active', category_id=NULL, note='' "
        "WHERE status='excluded' AND source IN ('bank','paypal','card') "
        "AND category_id IN (SELECT id FROM categories "
        "    WHERE name IN ('PayPal-Verrechnung','Kreditkarten-Verrechnung')) "
        "AND id NOT IN (SELECT bank_tx FROM matches WHERE kind IN ('paypal','card'))"
    )
    return cur.rowcount


def row_amount(cell, c_amt):
    """Amount of a row: one signed column or (column assignment) a debit/credit
    pair - credit positive, debit negative as an expense, no matter whether the
    bank writes the debit with or without a minus sign."""
    if isinstance(c_amt, (tuple, list)):
        deb = parse_amount(cell(c_amt[0])) if cell(c_amt[0]) else None
        cred = parse_amount(cell(c_amt[1])) if cell(c_amt[1]) else None
        if deb is None and cred is None:
            return None
        return q((cred or 0.0) - abs(deb or 0.0))
    return parse_amount(cell(c_amt))


def import_bank(file_storage, mapping=None):
    header, rows = read_csv_flexible(file_storage)
    c_date, c_amt, c_part, c_desc, c_cur = bank_columns(header, "Bank-CSV", mapping)
    con = db()
    new = skipped = 0
    _seen = {}  # same row several times in ONE file (hash_with_occurrence)
    for r in rows:

        def cell(i, r=r):
            return r[i].strip() if i is not None and i < len(r) else ""

        d = parse_date(cell(c_date))
        a = row_amount(cell, c_amt)
        if d is None or a is None:
            continue
        partner, desc = cell(c_part), cell(c_desc)
        cur = cell(c_cur) or "EUR"
        h = hash_with_occurrence(_seen, "bank", d, a, partner, desc)
        try:
            con.execute(
                """INSERT INTO transactions(source,booking_date,partner,description,
                           amount,currency,raw_hash) VALUES('bank',?,?,?,?,?,?)""",
                (d, partner, desc, a, cur, h),
            )
            new += 1
        except sqlite3.IntegrityError:
            skipped += 1
    n_rules = apply_rules(con)
    con.commit()
    con.close()
    return new, skipped, n_rules


CARD_SKIP_DESC = ("ihre zahlung vormonat",)  # card bill credit = transfer, not a transaction


def import_card(file_storage, filename=""):
    header, rows = read_csv_flexible(file_storage)
    c_date, c_amt, c_part, c_desc, c_cur = bank_columns(header, "Karten-CSV")
    m = re.search(r"(\d{4})_\d{4}-\d{2}-\d{2}", filename)
    label = f"Karte •{m.group(1)}" if m else "Karte"
    con = db()
    new = skipped = 0
    _seen = {}  # same row several times in ONE file (hash_with_occurrence)
    for r in rows:

        def cell(i, r=r):
            return r[i].strip() if i is not None and i < len(r) else ""

        d = parse_date(cell(c_date))
        a = parse_amount(cell(c_amt))
        if d is None or a is None:
            continue
        desc = cell(c_desc)
        if any(s in desc.lower() for s in CARD_SKIP_DESC):
            continue
        partner = cell(c_part) or desc or label
        cur = cell(c_cur) or "EUR"
        h = hash_with_occurrence(_seen, "card", label, d, a, desc)
        try:
            con.execute(
                """INSERT INTO transactions(source,booking_date,partner,description,
                           amount,currency,raw_hash) VALUES('card',?,?,?,?,?,?)""",
                (d, partner, desc, a, cur, h),
            )
            new += 1
        except sqlite3.IntegrityError:
            skipped += 1
    n_rules = apply_rules(con)
    con.commit()
    con.close()
    return new, skipped, n_rules


PAYPAL_SKIP_TYPES = (
    "bankgutschrift",
    "allgemeine abbuchung",
    "allgemeine einzahlung",
    "guthaben",
    "währungsumrechnung",
    "rückbuchung von bank",
    "von nutzer eingeleitete abbuchung",
)


def import_paypal(file_storage):
    header, rows = read_csv_flexible(file_storage)
    c_date = find_col(header, ["datum", "date"])
    c_name = find_col(header, ["name"])
    c_type = find_col(header, ["typ", "type"])
    if c_type is None:
        # The CSR transaction report has no separate type column - the type is
        # in "Beschreibung" there (e.g. "Bankgutschrift auf PayPal-Konto")
        c_type = find_col(header, ["beschreibung", "description"])
    c_amt = find_col(header, ["brutto", "gross", "betrag", "amount"])
    c_cur = find_col(header, ["währung", "currency"])
    c_code = find_col(header, ["transaktionscode", "transaction id"])
    if c_date is None or c_amt is None:
        raise ValueError(f"PayPal-CSV: Spalten nicht erkannt. Header: {header}")

    def cells(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    # Pass 1: collect currency conversions (date -> foreign amount -> EUR amount).
    # A GBP payment e.g. produces +36.99 GBP and -42.10 EUR on the same day.
    conv = {}
    for r in rows:
        if "währungsumrechnung" not in cells(r, c_type).lower():
            continue
        d = parse_date(cells(r, c_date))
        a = parse_amount(cells(r, c_amt))
        cur = cells(r, c_cur)
        if d is None or a is None:
            continue
        conv.setdefault(d, {"foreign": [], "eur": []})
        (conv[d]["eur"] if cur == "EUR" else conv[d]["foreign"]).append((a, cur))

    def to_eur(d, amount, cur):
        """Finds the matching conversion; returns (eur_amount, note) or (None, '')."""
        c = conv.get(d)
        if not c:
            return None, ""
        for i, (fa, fc) in enumerate(c["foreign"]):
            if fc == cur and is_zero(fa + amount):  # +36.99 GBP against -36.99 GBP
                for j, (ea, ec) in enumerate(c["eur"]):
                    if (ea < 0) == (amount < 0):
                        c["foreign"].pop(i)
                        c["eur"].pop(j)
                        return ea, f"{amount:.2f} {cur} umgerechnet"
        return None, ""

    con = db()
    new = skipped = 0
    _seen = {}  # same row several times in ONE file (hash_with_occurrence)
    for r in rows:

        def cell(i, r=r):
            return cells(r, i)

        typ = cell(c_type)
        # skip funding/transfer rows (otherwise counted twice)
        if any(t in typ.lower() for t in PAYPAL_SKIP_TYPES):
            continue
        d = parse_date(cell(c_date))
        a = parse_amount(cell(c_amt))
        if d is None or a is None or a == 0:
            continue
        name = cell(c_name) or "PayPal"
        cur = cell(c_cur) or "EUR"
        code = cell(c_code)
        note = ""
        if cur != "EUR":
            eur, note = to_eur(d, a, cur)
            if eur is not None:
                a, cur = q(eur), "EUR"
        h = tx_hash("paypal", code) if code else hash_with_occurrence(_seen, "paypal", (d, a, name))
        try:
            con.execute(
                """INSERT INTO transactions(source,booking_date,partner,description,
                           amount,currency,note,raw_hash,extra)
                           VALUES('paypal',?,?,?,?,?,?,?,?)""",
                (d, name, typ, a, cur, note, h, json.dumps({"code": code})),
            )
            new += 1
        except sqlite3.IntegrityError:
            skipped += 1
    n_rules = apply_rules(con)
    con.commit()
    con.close()
    return new, skipped, n_rules


def is_shared_fixed_cost_position(con, t):
    """Splitwise position of the fixed-cost group that already counts via the
    reduced bank booking - must never count a second time. Matches if the
    description contains a fixed-cost keyword OR a fixed-cost pattern (payee
    name). This check used to appear four times in the code."""
    if not is_fixed_cost_group(con, t["partner"]):
        return False
    desc = t["description"] or ""
    if is_fixed_cost_position(con, desc):
        return True
    hay = desc.lower()
    return any(p.lower() in hay for p in fixed_cost_patterns(con))


def is_fixed_cost_position(con, description):
    """Is this Splitwise position a housing fixed-cost position that already
    counts via the halved bank bookings (and is therefore NOT promoted)?

    Decided by the DESCRIPTION alone: if it contains one of the keywords stored
    with the fixed costs ("miete", "internet", ...), it is one. The Splitwise
    category is ignored because experience shows it is not maintained -
    "Tabak" sat there under "Elektrizitaet" since 2021 and thereby counted as
    an already-counted electricity bill; the 4 EUR share vanished from the
    evaluation without a trace (real, found 2026-09-01).

    Deliberately NO additional plausibility check anymore: an earlier attempt
    to cross-check the assignment via the category failed for "Versicherung"
    (description -> category "Versicherung", but the fixed-cost row is set to
    "Wohnung") and would have wrongly promoted the position. The keyword list
    is maintained by the user himself - trusting it is more predictable than
    overruling it.
    """
    # The keyword is searched in the DESCRIPTION, not in the Splitwise
    # category - that one is not maintained (see map_splitwise_category).
    hay = (description or "").lower()
    return any(k in hay for k in fixed_cost_sw_keywords(con))


def import_splitwise(file_storage, group_name=""):
    """Splitwise serves only as a CROSS-CHECK (balances, settlement payments) and
    creates no counting bookings. Column value per person = paid - share."""
    header, rows = read_csv_flexible(file_storage)
    c_date = find_col(header, ["datum", "date"])
    c_desc = find_col(header, ["beschreibung", "description"])
    c_cat = find_col(header, ["kategorie", "category"])
    c_cost = find_col(header, ["kosten", "cost"])
    c_cur = find_col(header, ["währung", "waehrung", "currency"])
    if None in (c_date, c_cost):
        raise ValueError(f"Splitwise-CSV: Spalten nicht erkannt. Header: {header}")
    con = db()
    seen_hashes = set()
    span = [None, None]
    me = splitwise_name(con)
    if not me:
        con.close()
        raise ValueError(
            "Kein Splitwise-Name hinterlegt – bitte unter Einstellungen "
            "eintragen, wie du im Splitwise-Export heißt (Spaltenüberschrift)."
        )

    # Own column: exact match first (also with a "(removed)" suffix), only then
    # substring - otherwise "Annabelle" wins against "Anna", depending on who
    # is further left in the export.
    def _clean(h):
        return re.sub(r"\s*\(removed\)\s*$", "", h.strip(), flags=re.I).lower()

    c_me = next((i for i, h in enumerate(header) if _clean(h) == me.strip().lower()), None)
    if c_me is None:
        c_me = next((i for i, h in enumerate(header) if me.lower() in h.lower()), None)
    if c_me is None:
        con.close()
        raise ValueError(f"'{me}' nicht im Header gefunden: {header}")
    members = (
        [h.strip() for k, h in enumerate(header) if k > c_cur and h.strip() and k != c_me] if c_cur is not None else []
    )
    new = skipped = 0
    new_ids = []
    _sw_seen = {}
    for r in rows:

        def cell(i, r=r):
            return r[i].strip() if i is not None and i < len(r) else ""

        d_low = cell(c_desc).lower()
        if "gesamtsaldo" in d_low or "gesamtbilanz" in d_low or "total balance" in d_low:
            continue
        d = parse_date(cell(c_date))
        cost = parse_amount(cell(c_cost))
        bal = parse_amount(cell(c_me))
        if d is None or cost is None or bal is None:
            continue
        desc, sw_cat = cell(c_desc), cell(c_cat)
        is_payment = "zahlung" in sw_cat.lower() or "payment" in sw_cat.lower()
        # identical rows in the same export = real repeated expenses, not a duplicate
        key = (group_name, d, cost, desc, sw_cat, bal)
        occ = _sw_seen.setdefault(key, 0) + 1
        _sw_seen[key] = occ
        h = tx_hash("splitwise", group_name, d, cost, desc, sw_cat, bal, occ)
        extra = json.dumps(
            {"cost": cost, "bal": bal, "sw_category": sw_cat, "is_payment": is_payment, "members": members}
        )
        seen_hashes.add(h)
        # (span[0] or d) would have been d itself on the first pass - "d < d" is
        # never true, so the span stayed empty and the reconciliation did nothing.
        if span[0] is None or d < span[0]:
            span[0] = d
        if span[1] is None or d > span[1]:
            span[1] = d
        try:
            cur = con.execute(
                """INSERT INTO transactions(source,booking_date,partner,description,
                           amount,currency,status,raw_hash,extra)
                           VALUES('splitwise',?,?,?,?,?,'info',?,?)""",
                (d, group_name or "Splitwise", desc, bal, cell(c_cur) or "EUR", h, extra),
            )
            new += 1
            new_ids.append(cur.lastrowid)
        except sqlite3.IntegrityError:
            skipped += 1
    removed = sync_splitwise_group(con, group_name or "Splitwise", seen_hashes, span)
    con.commit()
    con.close()
    return new, skipped, new_ids, removed


def sync_splitwise_group(con, group, seen_hashes, span):
    """Remove positions deleted in Splitwise here as well.

    User request 2026-09-11: "wenn ich das in splitwise aendere und dann neu
    exportiere - verschwinden die fehlenden eintraege dann? die sind ja dann
    nicht mehr vorhanden." (if I change it in Splitwise and export again, do
    the missing entries disappear? they no longer exist after all). Until now
    they stayed behind as stale records and distorted the balance cross-check.

    A group's export is its complete history, BUT it can be filtered. So
    reconciliation happens only within the period this import actually covers
    - a partial export thus only cleans up its own period and deletes nothing
    outside it.

    Rows with extra.manual (e.g. balance corrections inserted by hand) are
    exempt - they come from no export and would otherwise vanish on every
    import.

    Existing links to bank bookings are cleanly undone before deleting:
    otherwise a reduced amount would remain on the bank side whose
    justification no longer exists.
    """
    lo, hi = span
    if not seen_hashes or not lo or not hi:
        return 0
    # If the NEWEST entry of a group is deleted in Splitwise, the new export
    # ends before it - the deleted one would then lie outside the period and
    # stay forever. But that is exactly the most common case (you delete what
    # you just created). So the upper bound is extended to the group's newest
    # entry, BUT only if this export is current (at most 31 days behind the
    # existing data). An old export imported again thus does not accidentally
    # sweep away everything newer.
    newest = con.execute(
        "SELECT MAX(booking_date) FROM transactions WHERE source='splitwise' AND partner=?", (group,)
    ).fetchone()[0]
    if newest and newest > hi:
        gap = (date.fromisoformat(newest) - date.fromisoformat(hi)).days
        if gap <= 31:
            hi = newest
    stale = [
        r
        for r in con.execute(
            "SELECT id, raw_hash, extra FROM transactions WHERE source='splitwise' "
            "AND partner=? AND booking_date BETWEEN ? AND ? AND status!='deleted'",
            (group, lo, hi),
        )
        if r["raw_hash"] not in seen_hashes
    ]
    n = 0
    for r in stale:
        try:
            if json.loads(r["extra"] or "{}").get("manual"):
                continue
        except ValueError:
            pass
        unlink_splitwise_position(con, r["id"])
        con.execute("DELETE FROM matches WHERE other_tx=? OR bank_tx=?", (r["id"], r["id"]))
        con.execute("DELETE FROM transactions WHERE id=?", (r["id"],))
        n += 1
    return n


def is_auto_promoted(t):
    """Was this Splitwise row promoted by the automation (share or credit) -
    as opposed to one booked by the user himself?"""
    note = t["note"] or ""
    return t["status"] == "active" and (note.startswith("bezahlt ") or note.startswith("Anteil-Gutschrift"))


def demote_splitwise_position(con, tx_id):
    """Splitwise row back to a raw position (status info, no category, no note,
    no effective_amount). Used to appear five times in the code."""
    con.execute(
        "UPDATE transactions SET status='info', category_id=NULL, note='', effective_amount=NULL WHERE id=?", (tx_id,)
    )


def unlink_splitwise_position(con, sw_id):
    """Undo all links of a Splitwise position and revert the bank side. If the
    link knows its amount (matches.amount), only THAT is subtracted - a cash
    withdrawal with other shares still attached keeps those; without an amount
    (legacy data) it is reset to the full amount as before. Used to appear
    three times in the code."""
    for m in con.execute(
        "SELECT id, bank_tx, amount FROM matches WHERE other_tx=? AND kind='splitwise_share'", (sw_id,)
    ).fetchall():
        others = con.execute(
            "SELECT COUNT(*) FROM matches WHERE bank_tx=? AND id!=? AND kind='splitwise_share'", (m["bank_tx"], m["id"])
        ).fetchone()[0]
        if not others:
            con.execute("UPDATE transactions SET effective_amount=NULL, note='' WHERE id=?", (m["bank_tx"],))
        elif m["amount"] is not None:
            con.execute(
                "UPDATE transactions SET effective_amount=ROUND(effective_amount-?, 2) WHERE id=?",
                (m["amount"], m["bank_tx"]),
            )
        # Legacy data without an amount AND other shares on the same booking:
        # nobody knows this share's amount anymore - effective_amount stays as
        # it is. A reset to the full amount would throw away the other shares
        # too (real 2026-09-15: withdrawal 536, 84.20 EUR counted twice).
    con.execute("DELETE FROM matches WHERE other_tx=? AND kind='splitwise_share'", (sw_id,))
