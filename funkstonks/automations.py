"""Automations of the import pipeline: PayPal/card netting, foreign POS,
shared fixed costs, reconciliation of fixed-cost positions, hints (health_checks)."""

import json
import re
from datetime import date

from .db import disabled_automations, enabled_sources, fixed_cost_group, fixed_cost_rows, get_setting, system_cat_id
from .importers import (
    demote_splitwise_position,
    is_auto_promoted,
    is_shared_fixed_cost_position,
    unlink_splitwise_position,
)
from .money import eq, q
from .splitwise import splitwise_amount_mismatches


def reconcile_fixed_cost_positions(con):
    """Splitwise positions that (by now) count as shared fixed costs must be
    neither promoted nor linked - they count through the reduced bank booking.
    If someone sets up the fixed costs ONLY AFTER the first import (the help
    page recommended exactly that order), the pipeline had long since netted
    the rent positions against the cash pool or booked them as a credit - and
    that stayed that way forever (journey test 2026-09-13: 2250 instead of
    1650 EUR). This is rolled back here; afterwards auto_split_wohnung_fixed
    reduces the bank booking as intended."""
    if not fixed_cost_group(con):
        return 0
    n = 0
    for t in con.execute(
        "SELECT id, partner, description, status, note FROM transactions WHERE source='splitwise'"
    ).fetchall():
        if not is_shared_fixed_cost_position(con, t):
            continue
        linked = con.execute("SELECT 1 FROM matches WHERE other_tx=? AND kind='splitwise_share'", (t["id"],)).fetchone()
        if linked:
            unlink_splitwise_position(con, t["id"])
            n += 1
        if is_auto_promoted(t):
            demote_splitwise_position(con, t["id"])
            n += 1
    return n


def detect_splitwise_anomalies(con, new_ids):
    """Checks freshly imported Splitwise rows for suspicious near-duplicates:
    same date+description as an existing row of the same group, but a
    different amount/share. Splitwise CSV exports have no stable row ID per
    expense - if an expense is edited later in the real app, the tx_hash
    dedup cannot tell (different values = different hash = new row instead of
    an update) and both versions end up side by side in the DB. User request
    2026-08-06: "kannst du mich darauf hinweisen, wenn irgendwelche Zahlungen
    die ich auf Splitwise eintrage nicht stimmen sollten" (point me to
    payments I entered in Splitwise that look wrong). Pure heuristic (may
    misfire on genuine repeated expenses with the same name on the same day,
    see real "Maut" (toll) examples) - returns warnings, changes nothing in
    the DB."""
    if not new_ids:
        return []
    placeholders = ",".join("?" * len(new_ids))
    new_rows = con.execute(
        f"SELECT id, partner, booking_date, description, extra FROM transactions WHERE id IN ({placeholders})", new_ids
    ).fetchall()
    warnings = []
    for nr in new_rows:
        ex = json.loads(nr["extra"] or "{}")
        others = con.execute(
            "SELECT extra FROM transactions WHERE source='splitwise' AND partner=? "
            "AND booking_date=? AND description=? AND id!=?",
            (nr["partner"], nr["booking_date"], nr["description"], nr["id"]),
        ).fetchall()
        for o in others:
            oex = json.loads(o["extra"] or "{}")
            cost_diff = abs((ex.get("cost") or 0) - (oex.get("cost") or 0))
            bal_diff = abs((ex.get("bal") or 0) - (oex.get("bal") or 0))
            if cost_diff > 0.01 or bal_diff > 0.01:
                warnings.append(
                    f'{nr["partner"]}/{nr["booking_date"]}: "{nr["description"]}" '
                    f"neu mit {ex.get('cost')}€ importiert, aber es gibt schon eine Zeile "
                    f"mit {oex.get('cost')}€ am selben Tag - evtl. in Splitwise bearbeitet "
                    "oder ein echtes zweites Mal ausgegeben?"
                )
                break  # one warning per new row is enough
    return warnings


def is_paypal_clearing(tx):
    hay = ((tx["partner"] or "") + " " + (tx["description"] or "")).lower()
    return "paypal" in hay


def matched_ids(con):
    ids = set()
    for m in con.execute("SELECT bank_tx, other_tx FROM matches"):
        ids.add(m["bank_tx"])
        ids.add(m["other_tx"])
    return ids


def auto_match_paypal(con):
    """Automatically nets bank PayPal bookings against the real PayPal
    transactions: same amount, at most 3 days apart."""
    done = matched_ids(con)
    bank = [
        t
        for t in con.execute("SELECT * FROM transactions WHERE source='bank' AND status='active'")
        if t["id"] not in done and is_paypal_clearing(t)
    ]
    paypal = [t for t in con.execute("SELECT * FROM transactions WHERE source='paypal'") if t["id"] not in done]
    pv = system_cat_id(con, "PayPal-Verrechnung")
    used, n = set(), 0

    def link(b, p):
        nonlocal n
        used.add(p["id"])
        con.execute("INSERT OR IGNORE INTO matches(bank_tx,other_tx,kind) VALUES(?,?,'paypal')", (b["id"], p["id"]))
        con.execute(
            "UPDATE transactions SET status='excluded', category_id=?, "
            "note='automatisch mit PayPal verrechnet' WHERE id=?",
            (pv, b["id"]),
        )
        # a split created manually before would otherwise become invisible
        # (status!='active' -> splits count nowhere) instead of being deleted -
        # a dead record that would suddenly reappear if the status were later
        # switched back to active
        con.execute("DELETE FROM splits WHERE tx_id=?", (b["id"],))
        n += 1

    linked = set()
    # 1st pass: same amount, at most 3 days
    for b in bank:
        bd = date.fromisoformat(b["booking_date"])
        best = None
        for p in paypal:
            if p["id"] in used or not eq(p["amount"], b["amount"]):
                continue
            dd = abs((date.fromisoformat(p["booking_date"]) - bd).days)
            if dd <= 3 and (best is None or dd < best[1]):
                best = (p, dd)
        if best:
            link(b, best[0])
            linked.add(b["id"])
    # 2nd pass: up to 7 days, but only if the amount is UNAMBIGUOUS
    # (the debit sometimes arrives days after the payment)
    for b in bank:
        if b["id"] in linked:
            continue
        bd = date.fromisoformat(b["booking_date"])
        cands = [
            p
            for p in paypal
            if p["id"] not in used
            and eq(p["amount"], b["amount"])
            and abs((date.fromisoformat(p["booking_date"]) - bd).days) <= 7
        ]
        same_amt_clearing = [x for x in bank if x["id"] not in linked and eq(x["amount"], b["amount"])]
        if len(cands) == 1 and len(same_amt_clearing) == 1:
            link(b, cands[0])
            linked.add(b["id"])
    return n


CARD_SETTLEMENT_WORDS = (
    "kreditkarte",
    "kartenrechnung",
    "kartenabrechnung",
    "credit card",
    "card statement",
    "visa",
    "mastercard",
)


def looks_like_card_settlement(t):
    hay = ((t["partner"] or "") + " " + (t["description"] or "")).lower()
    return any(w in hay for w in CARD_SETTLEMENT_WORDS)


def auto_match_card(con):
    """Credit card statement: sum of the card items in one statement period
    == a single checking-account debit, window -45..+5 days around the debit.

    The export often already contains items AFTER the statement cutoff but
    BEFORE the debit (real case 2026-10-10: September statement debited on
    05.10., export with items from 29./30.09. that belong to the October
    statement). Therefore the open items in the window are sorted by date and
    all prefixes "up to cutoff X" are tried, from the longest (= whole window,
    the old behaviour) to the shortest. A statement always contains ALL card
    bookings up to its cutoff, so this is no free subset-sum guessing
    (deliberately no arbitrary combinations)."""
    done = matched_ids(con)
    # Only debits that LOOK like a card statement. Without this filter every
    # bank booking whose amount happened to equal the sum of the open card
    # items was netted (review 2026-09-13: a Spotify subscription became a
    # "Kreditkarten-Verrechnung").
    bank = [
        t
        for t in con.execute("SELECT * FROM transactions WHERE source='bank' AND status='active' AND amount<0")
        if t["id"] not in done and looks_like_card_settlement(t)
    ]
    card = [t for t in con.execute("SELECT * FROM transactions WHERE source='card'") if t["id"] not in done]
    cc = system_cat_id(con, "Kreditkarten-Verrechnung")
    used, n = set(), 0
    for b in sorted(bank, key=lambda t: t["booking_date"]):
        bd = date.fromisoformat(b["booking_date"]).toordinal()
        cands = [
            c
            for c in card
            if c["id"] not in used and bd - 45 <= date.fromisoformat(c["booking_date"]).toordinal() <= bd + 5
        ]
        if not cands:
            continue
        cands.sort(key=lambda c: (c["booking_date"], c["id"]))
        hit = None
        for cut in range(len(cands), 0, -1):
            # prefix up to and including a cutoff day - never cut in the
            # middle of a day (same day = same statement).
            if cut < len(cands) and cands[cut]["booking_date"] == cands[cut - 1]["booking_date"]:
                continue
            if abs(q(sum(c["amount"] for c in cands[:cut])) - b["amount"]) <= 0.01:
                hit = cands[:cut]
                break
        if hit is None:
            continue
        for c in hit:
            used.add(c["id"])
            con.execute("INSERT OR IGNORE INTO matches(bank_tx,other_tx,kind) VALUES(?,?,'card')", (b["id"], c["id"]))
        con.execute(
            "UPDATE transactions SET status='excluded', category_id=?, "
            "note='automatisch mit Kreditkarte verrechnet' WHERE id=?",
            (cc, b["id"]),
        )
        con.execute("DELETE FROM splits WHERE tx_id=?", (b["id"],))  # see auto_match_paypal
        n += 1
    return n


# Eurozone: country code (AT, IT, GR, IE); outside it the CURRENCY appears (DKK,
# GBP) - "POS 78,40 DKK K1" slipped past the automation until 2026-10-14
# (Copenhagen, 12 payments stayed uncategorized).
FOREIGN_POS_RE = re.compile(r"POS\s+[\d.,]+\s+([A-Z]{2,3})\s+K\d", re.IGNORECASE)


def categorize_foreign_pos(con):
    """Automatically mark terminal payments (POS ... <country code> K1 ...)
    outside Austria as Urlaub (vacation) - only uncategorized ones, only real
    POS rows (no E-COMM bookings, whose country code only shows the company
    seat)."""
    row = con.execute("SELECT id FROM categories WHERE name='Urlaub'").fetchone()
    if not row or "foreign_pos" in disabled_automations(con):
        return 0
    urlaub_id = row["id"]
    n = 0
    for tx in con.execute(
        "SELECT id, description FROM transactions WHERE category_id IS NULL "
        "AND source IN ('bank','card') AND status='active'"
    ):
        m = FOREIGN_POS_RE.search(tx["description"] or "")
        if m and m.group(1).upper() not in ("AT", "EUR"):
            con.execute("UPDATE transactions SET category_id=? WHERE id=?", (urlaub_id, tx["id"]))
            n += 1
    return n


# Fixed costs of the flat that the user pays in full up front and shares 50/50
# with the flatmate (substring on partner+description). Deviations from the
# 50/50 split (e.g. when she pays more because of additional shared expenses)
# are shown by the Splitwise cross-check (/splitwise), not by this automation.
# Since 2026-09-11 the fixed-cost patterns live in the table `fixed_costs` and
# can be maintained under settings (pattern, category, share in percent,
# associated Splitwise category). Formerly this was a hard-coded constant;
# now it is the configured fixed costs - see fixed_cost_rows()/fixed_cost_patterns().
# A private mobile subscription is NOT the flat but a separate mobile
# subscription (easy to confuse with T-Mobile, which actually is the flat's
# internet connection) - deliberately NOT part of the configured fixed costs.
# Netflix may be a shared account (also kept in the Splitwise export of group
# "wohnung" as its own position with bal=cost/2, user decision 2026-08-02) -
# unlike the other three, its debit never runs through the bank but through
# PayPal/credit card (see the source filter below).

# Groups that NEVER get "Als Urlaub markieren" (mark as vacation) as a whole
# (user request 2026-08-07: "Dauergruppen sollten diesen Urlaubsbutton
# nicht haben. nur alle anderen." - permanent groups should not have the
# vacation button, only all the others). "wohnung" is the shared flat, not a
# trip. A big, long-lived friends group (e.g. 19 members) with mixed everyday
# expenses is no single trip like a festival group - the blanket whole-group
# automation would wrongly colour genuine non-vacation expenses there.
# Which Splitwise groups get NO "Als Urlaub markieren" switch is now in the
# settings (urlaub_excluded_groups) - typically the flat-share group and big
# permanent groups with mixed everyday expenses.


def auto_split_wohnung_fixed(con):
    """Configured fixed costs (settings, table fixed_costs): the user pays the
    full amount from his own account, but only his share (share_percent)
    really counts. Sets effective_amount=amount*share/100 and the category of
    the fixed-cost row. Runs over all three counting sources
    (bank/paypal/card) - a subscription may run via PayPal or credit card."""
    rules = [r for r in fixed_cost_rows(con) if (r["pattern"] or "").strip()]
    if not rules:
        return 0
    shared = get_setting(con, "shared_name") or ""
    n = 0
    # effective_amount IS NULL is the only condition (not restricted to category_id IS NULL)
    # - otherwise a more general rule (e.g. "UNIQA"->Versicherung) grabs the booking first
    # and the split never applies. Once split (effective_amount set) a booking is never
    # touched again, not even on future imports.
    for tx in con.execute(
        "SELECT id, partner, description, amount FROM transactions "
        "WHERE source IN ('bank','paypal','card') AND status='active' "
        "AND effective_amount IS NULL AND amount<0"
    ):
        hay = ((tx["partner"] or "") + " " + (tx["description"] or "")).lower()
        rule = next((r for r in rules if r["pattern"].lower() in hay), None)
        if not rule:
            continue
        share = max(1, min(100, rule["share_percent"] or 100))
        if share >= 100:
            # Not a shared item: only set the category, leave the amount alone.
            # effective_amount would otherwise equal amount and merely mark the
            # booking as "corrected" for no reason.
            if rule["category_id"]:
                con.execute(
                    "UPDATE transactions SET category_id=? WHERE id=? AND category_id IS NULL",
                    (rule["category_id"], tx["id"]),
                )
            continue
        who = f"{shared}s Anteil" if shared else "fremder Anteil"
        note = (
            f"automatisch auf {share}% gekürzt ({who} laut Einstellungen)"
            if share != 50
            else f"automatisch halbiert ({who})"
        )
        con.execute(
            "UPDATE transactions SET category_id=COALESCE(?,category_id), effective_amount=?, note=? WHERE id=?",
            (rule["category_id"], q(tx["amount"] * share / 100.0), note, tx["id"]),
        )
        n += 1
    return n


def _fmt_day(d):
    return f"{d[8:10]}.{d[5:7]}." if d and len(d) >= 10 else (d or "")


def health_checks(con):
    """Hints nobody would see otherwise (user 2026-10-13: "was wenn Auszuege
    fehlen? wird das irgendwo angezeigt? ... notification zeichen wie bei
    durchgehen" - what if statements are missing, is that shown anywhere, a
    notification badge like on the review page): stale or missing exports per
    active source, PayPal debits without receipt, unnetted card statements,
    Splitwise positions that do not match the bank booking. Each hint carries
    `nav` (menu item where the counter appears) and `href` (where the click
    leads). Display only, changes nothing."""
    out = []
    srcs = enabled_sources(con)
    latest = {
        r["source"]: r["d"]
        for r in con.execute(
            "SELECT source, MAX(booking_date) d FROM transactions WHERE status!='deleted' GROUP BY source"
        )
    }
    labels = {"bank": "Kontoauszug", "paypal": "PayPal-Export", "card": "Kartenexport", "splitwise": "Splitwise-Export"}
    today = date.today()
    bank_latest = latest.get("bank")
    for s in ("bank", "paypal", "card", "splitwise"):
        if s not in srcs:
            continue
        d = latest.get(s)
        if not d:
            out.append(
                {
                    "key": "missing:" + s,
                    "nav": "import",
                    "level": "warn",
                    "text": f"{labels[s]} noch nie importiert",
                    "href": "/import",
                }
            )
            continue
        if s == "bank":
            if (today - date.fromisoformat(d)).days > 45:
                out.append(
                    {
                        "key": "stale:bank",
                        "nav": "import",
                        "level": "warn",
                        "text": f"Kontoauszug reicht nur bis {_fmt_day(d)} – neuer Export fällig",
                        "href": "/import",
                    }
                )
        elif bank_latest and (date.fromisoformat(bank_latest) - date.fromisoformat(d)).days > 21:
            out.append(
                {
                    "key": "stale:" + s,
                    "nav": "import",
                    "level": "warn",
                    "text": f"{labels[s]} reicht nur bis {_fmt_day(d)}, der Kontoauszug bis "
                    f"{_fmt_day(bank_latest)} – {labels[s]} nachziehen",
                    "href": "/import",
                }
            )
    if "paypal" in srcs and latest.get("paypal"):
        n = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='bank' AND status='active' AND amount<0 "
            "AND lower(partner) LIKE 'paypal%' AND booking_date>?",
            (latest["paypal"],),
        ).fetchone()[0]
        if n:
            out.append(
                {
                    "key": "paypal_open",
                    "nav": "import",
                    "level": "info",
                    "text": f"{n} PayPal-Abbuchung{'en' if n != 1 else ''} ohne Beleg seit "
                    f"{_fmt_day(latest['paypal'])} – PayPal-Export importieren",
                    "href": "/transactions?q=paypal",
                }
            )
    if "card" in srcs:
        n = sum(
            1
            for t in con.execute("SELECT * FROM transactions WHERE source='bank' AND status='active' AND amount<0")
            if looks_like_card_settlement(t)
        )
        if n:
            out.append(
                {
                    "key": "card_open",
                    "nav": "import",
                    "level": "warn",
                    "text": f"{n} Kreditkartenrechnung{'en' if n != 1 else ''} nicht mit Kartenposten "
                    "verrechnet – Kartenexport mit allen Posten importieren",
                    "href": "/transactions?q=kreditkarte",
                }
            )
    if "splitwise" in srcs and latest.get("splitwise"):
        mm = splitwise_amount_mismatches(con)
        n = sum(1 for lst in mm.values() for x in lst if not x.get("confirmed"))
        if n:
            out.append(
                {
                    "key": "sw_mismatch",
                    "nav": "splitwise",
                    "level": "warn",
                    "text": f"{n} Splitwise-Position{'en' if n != 1 else ''} passen nicht zur Kontobuchung",
                    "href": "/splitwise",
                }
            )
    return out
