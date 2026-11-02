"""Suggestions drawn from the imported data (user 2026-11-02: "koennen auch
sachen wie der splitwise name der person mit der man teilt, gruppen, geteilte
und automatische fixkosten etc automatisch erkennen aus den importfiles" -
can the Splitwise name of the person one shares with, groups, shared and
automatic fixed costs be recognised from the import files?).

Two suggestions, both read-only - nothing is applied without a click under
Einstellungen (the import result only counts them):

  suggest_sharing()      which two-member Splitwise group carries the shared
                         fixed costs (rent, electricity, ...) and with whom.
  suggest_fixed_costs()  which payees of the own account are shared fixed
                         costs: a counting booking equals a Splitwise item of
                         that group to the cent within a week, in at least
                         two different months; the own share comes from the
                         Splitwise split, the keyword from the descriptions.
The own Splitwise column is guessed in importers.guess_splitwise_name().
"""

import json
import re
from collections import Counter
from datetime import date

from .db import fixed_cost_group, fixed_cost_rows, get_setting, get_setting_list
from .money import EPS, cents
from .rules import rule_suggestion_for

# words in a Splitwise description that mark a housing fixed cost
FIXED_WORDS = (
    "miete",
    "strom",
    "gas",
    "internet",
    "versicherung",
    "betriebskosten",
    "wasser",
    "heizung",
    "fernwärme",
    "rundfunk",
    "orf",
    "haushalt",
)
LOOKBACK_DAYS = 400
MATCH_DAYS = 7


def _days(a, b):
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _members(extra):
    try:
        m = json.loads(extra or "{}").get("members") or []
    except ValueError:
        m = []
    return [str(x).strip() for x in m if str(x).strip()]


def suggest_sharing(con):
    """{"group", "partner"} or None - only while the setting fixed_cost_group
    is empty. The group needs exactly one other member and at least two
    items with a fixed-cost word in the last LOOKBACK_DAYS."""
    if (get_setting(con, "fixed_cost_group") or "").strip():
        return None
    groups = {}
    for r in con.execute(
        "SELECT partner, description, extra FROM transactions WHERE source='splitwise' "
        "AND booking_date >= date('now', ?)",
        (f"-{LOOKBACK_DAYS} days",),
    ):
        g = groups.setdefault(r["partner"], {"hits": 0, "members": set()})
        g["members"].update(_members(r["extra"]))
        if any(w in (r["description"] or "").lower() for w in FIXED_WORDS):
            g["hits"] += 1
    best = None
    for name, g in groups.items():
        if len(g["members"]) == 1 and g["hits"] >= 2 and (best is None or g["hits"] > best[1]):
            best = (name, g["hits"], next(iter(g["members"])))
    return {"group": best[0], "partner": best[2], "n": best[1]} if best else None


def suggest_fixed_costs(con, group=None):
    """Shared fixed costs the data suggest, newest first by hit count:
    [{"pattern", "share_percent", "sw_keyword", "category_id", "n"}].
    `pattern` is the payee without digits (as the rule dialog proposes);
    payees a fixed_costs row already covers and dismissed ones (setting
    fixed_suggest_dismissed) are left out."""
    group = group or fixed_cost_group(con) or (suggest_sharing(con) or {}).get("group", "")
    if not group:
        return []
    by_cents = {}
    for r in con.execute(
        "SELECT booking_date, description, extra FROM transactions WHERE source='splitwise' "
        "AND lower(partner)=? AND booking_date >= date('now', ?)",
        (group.lower(), f"-{LOOKBACK_DAYS} days"),
    ):
        try:
            x = json.loads(r["extra"] or "{}")
            cost, bal = float(x.get("cost") or 0), float(x.get("bal") or 0)
        except (ValueError, TypeError):
            continue
        if cost <= EPS or bal <= EPS:  # only items the user paid (column value = paid - own share > 0)
            continue
        by_cents.setdefault(cents(cost), []).append((r["booking_date"], r["description"] or "", cost - bal, cost))
    if not by_cents:
        return []
    existing = [(f["pattern"] or "").strip().lower() for f in fixed_cost_rows(con) if (f["pattern"] or "").strip()]
    dismissed = {str(p).strip().lower() for p in get_setting_list(con, "fixed_suggest_dismissed")}
    cands = {}
    for t in con.execute(
        "SELECT * FROM transactions WHERE source IN ('bank','paypal','card') AND status='active' "
        "AND amount < 0 AND booking_date >= date('now', ?)",
        (f"-{LOOKBACK_DAYS} days",),
    ):
        items = by_cents.get(cents(-t["amount"]))
        if not items:
            continue
        pattern = rule_suggestion_for(t)
        if not pattern:
            continue
        key = pattern.lower()
        hay = ((t["partner"] or "") + " " + (t["description"] or "")).lower()
        if key in dismissed or any(p in hay or p in key for p in existing):
            continue
        for d, desc, share, cost in items:
            if _days(d, t["booking_date"]) > MATCH_DAYS:
                continue
            c = cands.setdefault(
                key,
                {
                    "pattern": pattern,
                    "months": set(),
                    "amounts": set(),
                    "pct": Counter(),
                    "words": Counter(),
                    "cats": Counter(),
                },
            )
            c["months"].add(t["booking_date"][:7])
            c["amounts"].add(cents(cost))
            c["pct"][max(1, min(100, round(100.0 * share / cost)))] += 1
            for w in re.findall(r"[a-zäöüß]{4,}", desc.lower()):
                c["words"][w] += 1
            if t["category_id"]:
                c["cats"][t["category_id"]] += 1
            break
    out = []
    for c in cands.values():
        fixed_word = next((w for w, _ in c["words"].most_common() if w in FIXED_WORDS), None)
        # A fixed cost names itself (rent, electricity, ...) or recurs with the
        # same amount month after month; shared takeaway orders of varying
        # amounts in the same group are not one (seen in the owner's data).
        if len(c["months"]) < 2 or not (fixed_word or (len(c["months"]) >= 3 and len(c["amounts"]) <= 2)):
            continue
        out.append(
            {
                "pattern": c["pattern"],
                "share_percent": c["pct"].most_common(1)[0][0],
                "sw_keyword": fixed_word or (c["words"].most_common(1)[0][0] if c["words"] else ""),
                "category_id": c["cats"].most_common(1)[0][0] if c["cats"] else None,
                "n": len(c["months"]),
            }
        )
    out.sort(key=lambda s: (-s["n"], s["pattern"].lower()))
    return out
