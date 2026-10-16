"""Rules: tx_hash (NEVER change), apply_rules, rule maintenance, income rules,
Splitwise keyword mapping."""

import hashlib
import json
import re

from .config import SPLITWISE_CAT_MAP
from .db import disabled_automations, income_category_name


def income_rule_patterns(con):
    """The patterns of all rules pointing at the income category.

    Deliberately NO separate setting: the income source is an ordinary rule
    and should stay visible and editable under /rules. /einstellungen reads
    it directly from the rules here and writes back via set_income_rules() -
    so the two places cannot drift apart in the first place.
    """
    return [
        r["pattern"]
        for r in con.execute(
            "SELECT r.pattern FROM rules r JOIN categories c ON c.id=r.category_id WHERE c.name=? ORDER BY r.id",
            (income_category_name(con),),
        )
    ]


def set_income_rules(con, patterns):
    """Bring the income category's rules to exactly this pattern list.

    What is new gets created; what has disappeared gets deleted - but only
    within the income category, other rules stay untouched. If the category
    does not exist (yet), nothing happens: a setting must never create a
    category out of nothing.
    """
    row = con.execute("SELECT id FROM categories WHERE name=?", (income_category_name(con),)).fetchone()
    if row is None:
        return
    want = []
    for p in patterns:
        p = p.strip()
        if p and p not in want:
            want.append(p)
    have = income_rule_patterns(con)
    for p in have:
        if p not in want:
            con.execute("DELETE FROM rules WHERE pattern=? AND category_id=?", (p, row["id"]))
    for p in want:
        if p not in have:
            con.execute("INSERT INTO rules(pattern,category_id,priority) VALUES(?,?,100)", (p, row["id"]))


def tx_hash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def hash_with_occurrence(seen, *parts):
    """Hash for a row whose combination may occur several times in ONE export
    (two identical tickets on the same day).

    The FIRST occurrence keeps exactly the previous hash - existing data stays
    recognizable, nothing is created twice. Only from the second identical row
    of the same file on is a counter added. Before, such a row silently fell
    through ("1 new, 1 already present") - found in the handover test
    2026-09-11.
    """
    key = tx_hash(*parts)
    occ = seen.get(key, 0)
    seen[key] = occ + 1
    return key if occ == 0 else tx_hash(*parts, occ)


# Positions of a Splitwise group marked as vacation are IMMUNE to rules - user
# request 2026-09-01: "diese eintraege darin sollen aber auf jedenfall IMMER
# urlaub bleiben und alles ueberschreiben, solange der haken gesetzt ist. das
# soll auch nicht unter regeln aenderbar sein." (these entries must ALWAYS stay
# vacation and override everything while the box is ticked; not changeable
# under rules either). Happened for real: a rule "uber" -> Transport/Mobilitaet
# rebooked the group's vacation positions when applied retroactively. The
# group marker is the stronger statement and wins - it deliberately applies to
# ALL positions of the group (see promote_splitwise_shares); individual cases
# are corrected by hand via the category dropdown.
URLAUB_PROTECTED_SQL = "NOT (source='splitwise' AND partner IN (SELECT partner FROM urlaub_groups))"
_RULE_HITS = []  # ids that apply_rules() categorized during the running import


def apply_rules(con, tx_id=None):
    """Apply rules to uncategorized transactions. Returns the count.
    Raw Splitwise rows (status='info') are deliberately excluded - otherwise
    this function silently sets a category_id on positions that are not (yet)
    promoted/booked at all (bal>=0 expenses, settlements without a bank match),
    which looks like a choice already made on /splitwise although the row
    counts nowhere (status stays 'info')."""
    rules = con.execute("SELECT * FROM rules ORDER BY priority, id").fetchall()
    q = (
        "SELECT id, partner, description FROM transactions WHERE category_id IS NULL "
        "AND NOT (source='splitwise' AND status='info') "
        "AND " + URLAUB_PROTECTED_SQL
    )
    args = ()
    if tx_id:
        q += " AND id=?"
        args = (tx_id,)
    n = 0
    for tx in con.execute(q, args).fetchall():
        hay = ((tx["partner"] or "") + " " + (tx["description"] or "")).lower()
        for r in rules:
            if r["pattern"].lower() in hay:
                con.execute("UPDATE transactions SET category_id=? WHERE id=?", (r["category_id"], tx["id"]))
                n += 1
                _RULE_HITS.append(tx["id"])
                break
    return n


def is_splitwise_settlement(con, tx_id):
    """Is this a Splitwise settlement payment (is_payment)? Such rows are pure
    balance settlement and must never be categorized - see tx_update()."""
    r = con.execute("SELECT source, extra FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not r or r["source"] != "splitwise":
        return False
    try:
        return bool(json.loads(r["extra"] or "{}").get("is_payment"))
    except ValueError:
        return False


def normalize_pattern(p):
    """Normalize a rule pattern: strip surrounding whitespace and asterisks.

    apply_rules() compares by substring anyway, so wildcards are superfluous -
    out of habit they get typed nonetheless (user request 2026-09-01: "dann
    reinschreiben *Radatz*" - then type in *Radatz*). Without this trimming
    the asterisk would be searched literally and the rule could never match.
    """
    return (p or "").strip().strip("*").strip()


def upsert_rule(con, pattern, category_id):
    """Create a rule OR update the existing one with the same pattern.

    Previously rows were inserted blindly. But apply_rules() takes the FIRST
    matching rule (ORDER BY priority, id) and then stops - a newly created,
    corrected rule was therefore silently shadowed by the older one. Happened
    for real (found 2026-09-01): "vollpension generati" and "breadley stein"
    each existed twice, the second (correct) category never took effect. To
    the user it looked as if setting rules simply did not work. Any old
    duplicates are updated along with it so no shadowing leftover remains.
    """
    pat = normalize_pattern(pattern)
    if not pat:
        return None
    ex = con.execute(
        "SELECT id FROM rules WHERE lower(pattern)=lower(?) ORDER BY priority, id LIMIT 1", (pat,)
    ).fetchone()
    if ex:
        con.execute("UPDATE rules SET category_id=? WHERE lower(pattern)=lower(?)", (category_id, pat))
        return ex["id"]
    return con.execute("INSERT INTO rules(pattern,category_id) VALUES(?,?)", (pat, category_id)).lastrowid


def rule_target_txs(con, pattern):
    """Bookings a rule may recategorize RETROACTIVELY.

    Deliberately narrower than apply_rules(): there it is only about still
    uncategorized rows, here an existing category is OVERWRITTEN. Excluded are
    therefore:
    - everything except status='active' - that is where the automations'
      system categories sit (PayPal/credit card settlement, Umbuchung, raw
      Splitwise rows); a rule like "PayPal" would otherwise destroy them
    - split bookings: their category_id MUST stay NULL, otherwise the
      inconsistent state of a category AND splits being set at the same time
      arises (the split parts are the only truth for such a booking)
    - positions from a Splitwise group marked as vacation (see
      URLAUB_PROTECTED_SQL) - the group marker beats every rule
    """
    pat = normalize_pattern(pattern).lower()
    if not pat:
        return []
    rows = con.execute(
        "SELECT id, partner, description, category_id FROM transactions "
        "WHERE status='active' AND id NOT IN (SELECT tx_id FROM splits) "
        "AND " + URLAUB_PROTECTED_SQL
    ).fetchall()
    return [r for r in rows if pat in ((r["partner"] or "") + " " + (r["description"] or "")).lower()]


def map_splitwise_category(description, disabled=()):
    """Word-boundary match (\\b...\\b) instead of a plain substring - a plain
    `in` check would e.g. have wrongly found "sport" (->Lifestyle) inside
    "transport" (->Transport/Mobilitaet), because "sport" is a substring of
    "transport". Found while testing promote_own_advance_credits()
    (2026-08-14) - luckily affected no real booking yet (no Splitwise entry
    had "transport" literally in category/text so far), but a latent bug that
    would have struck sooner or later."""
    # ONLY the description, not the Splitwise category (user request
    # 2026-09-11: "kategorien aus splitwise sollten ohnehin komplett ignoriert
    # werden. diese werden nicht gepflegt." - Splitwise categories should be
    # ignored completely, they are not maintained). Backed by the own data:
    # "Tabak" sat there under "Elektrizitaet" since 2021, 81 rows affected -
    # the descriptions, by contrast, are clean ("miete", "internet", "spar").
    hay = (description or "").lower()
    for kw, cat in SPLITWISE_CAT_MAP:
        if ("sw:" + cat) in disabled:
            continue  # deleted by the user under rules (disabled_automations)
        if re.search(r"\b" + re.escape(kw) + r"\b", hay):
            return cat
    return None


def splitwise_category_name(con, description):
    """Category for a Splitwise position - from the DESCRIPTION alone.

    Order: first the own RULES (they know "billa", "uber", "wolt" and are
    maintained by the user), then the built-in keyword list, otherwise
    NOTHING. The Splitwise category is no longer consulted at all - user
    request 2026-09-11, confirmed on inquiry: "splitwise kategorien sollen
    nicht verwendet werden. ich will alles einmal manuell einpflegen. dafuer
    habe ich ja die regeln in diesem tool." (Splitwise categories must not be
    used; I want to enter everything manually once, that is what the rules in
    this tool are for.)

    Without a hit the position DELIBERATELY stays uncategorized (previously:
    fallback "Sonstiges"): that way it lands under "Durchgehen" (review)
    instead of silently vanishing into a catch-all category - assign exactly
    once, tick "remember rule", automatic from then on.
    """
    hay = (description or "").lower()
    for r in con.execute(
        "SELECT r.pattern, c.name FROM rules r JOIN categories c ON c.id=r.category_id ORDER BY r.priority, r.id"
    ):
        if r["pattern"] and r["pattern"].lower() in hay:
            return r["name"]
    return map_splitwise_category(description, disabled_automations(con))


def rule_suggestion_for(tx):
    """Suggestion for a rule pattern: for Splitwise the description (the
    partner there is the group name), otherwise the payee without digits
    (branch numbers). Used by /review and the rule dialog in /transactions."""
    if tx["source"] == "splitwise":
        return (tx["description"] or "").strip()
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", tx["partner"] or "")).strip() or (tx["partner"] or "").strip()
