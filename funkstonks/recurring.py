"""Recurring costs: detect series (detect_recurring), overrides (hide/force/merge),
helpers for the series page."""

import json
import re
from collections import Counter
from datetime import date

from .categories import top_category_names
from .config import VARIABLE_CATS
from .db import fixed_cost_like_sql, fixed_cost_patterns, get_setting_list
from .money import EPS, q
from .stats import eff


def _norm_partner(p):
    """Normalize the payee: strip digits/branch numbers."""
    p = re.sub(r"\d+", "", (p or "").lower())
    return re.sub(r"\s+", " ", p).strip()


# Words that appear in almost every payment reference and therefore say NOTHING
# about the provider - they must never merge two series. Without this list every
# PayPal row ("Zahlung im Einzugsverfahren mit Zahlungsrechnung") would match
# every other.
SERIES_STOPWORDS = {
    # payment boilerplate
    "zahlung",
    "zahlungen",
    "zahlungsrechnung",
    "einzugsverfahren",
    "lastschrift",
    "rechnung",
    "abbuchung",
    "dauerauftrag",
    "ueberweisung",
    "überweisung",
    "gutschrift",
    "bezahlung",
    "mandat",
    "referenz",
    "verwendungszweck",
    "einzug",
    "sepa",
    "ecomm",
    "comm",
    "kauf",
    "onlinekauf",
    "karte",
    # legal forms and generic parts of company names
    "gmbh",
    "limited",
    "ltd",
    "sarl",
    "holdings",
    "holding",
    "group",
    "company",
    "ireland",
    "europe",
    "european",
    "international",
    "austria",
    "deutschland",
    "germany",
    "wien",
    "vienna",
    "global",
    "collect",
    "payments",
    "payment",
    "services",
    "service",
    "technologies",
    "technology",
    "entertainment",
    "media",
    "digital",
    "online",
    "solutions",
    "systems",
    "operations",
    "commerce",
    "store",
    "shop",
    "www",
    "http",
    "https",
}


def _series_tokens(t):
    """Meaningful words of a booking (payee + text).

    Only letter sequences of 4+ characters, without the boilerplate from
    SERIES_STOPWORDS - what normally remains is the brand name ("blizzard",
    "spotify").
    """
    hay = ((t["partner"] or "") + " " + (t["description"] or "")).lower()
    return {w for w in re.findall(r"[a-zäöüß]{4,}", hay) if w not in SERIES_STOPWORDS}


def _merge_related_series(series):
    """Merge series that are the same contract under a different payment route.

    User request 2026-09-11: "die blizzard abos haben sich in der zahlung
    geaendert. die neue zahlung hat anderen zahlauftrag, aber im text steht
    trotzdem blizzard ... wenn betrag ungefaehr gleich bleibt und text irgendwo
    matched sollten beide in der gleichen position ... aufscheinen" (the
    Blizzard subscription changed its payment route but the text still says
    Blizzard; if the amount stays about the same and the text matches, both
    should appear in the same row).

    Real case: 3x PayPal to "Blizzard Entertainment Ireland L" (12,99 EUR),
    then bank to "GLOBAL COLLECT BV" with text "BLIZZARD EU339352308" - via
    _norm_partner() two entirely different keys that would never be detected
    as one series individually (the new one would have only ONE payment).

    Conditions, all of which must hold:
      * a common meaningful word that occurs in EVERY booking of both groups
        (intersection, not union - otherwise a group with many different
        texts drags others along at random)
      * nearly the same typical amount (at most 1 EUR or 10 % difference)
      * one group REPLACES the other, they do not run in parallel - exactly
        that distinguishes a payment-route change from two different
        providers that happen to share a word (e.g. several "CAFE ..." with
        similar amounts alternating over time)
    """
    keys = list(series)
    toks, amts, span = {}, {}, {}
    for k in keys:
        sets = [_series_tokens(t) for t in series[k]]
        toks[k] = set.intersection(*sets) if sets else set()
        amts[k] = Counter(q(-eff(t)) for t in series[k]).most_common(1)[0][0]
        ds = sorted(t["booking_date"] for t in series[k])
        span[k] = (ds[0], ds[-1])
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if not (toks[a] & toks[b]):
                continue
            hi = max(amts[a], amts[b])
            if abs(amts[a] - amts[b]) > max(1.0, 0.1 * hi):
                continue
            first, second = sorted((span[a], span[b]))
            if second[0] < first[1]:
                continue  # run in parallel -> two different things
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    merged = {}
    for members in groups.values():
        # If a rule is attached to one of the parts, it stays the key of the
        # merged series (drilldown and display run through it).
        rep_key = next((k for k in members if k.startswith("rule:")), members[0])
        ts = [t for k in members for t in series[k]]
        ts.sort(key=lambda t: t["booking_date"])
        merged[rep_key] = ts
    return merged


def _series_ended(days_since, med_gap):
    """Is the series still running, or has it been cancelled?

    User request 2026-09-11: "patreon scheint immer noch auf unter laufende
    kosten, obwohl die letzte zahlung 2 monate zurueckliegt - weil gekuendigt."
    (Patreon still shows under recurring costs although the last payment was
    2 months ago - because it was cancelled.)

    Criterion: a whole due date was missed, plus a buffer for a late debit
    (1.5 cycles + 10 days). Monthly that is ~57 days, quarterly ~146. Patreon
    (last 07.07., data as of 03.09. = 58 days) drops out, Blizzard (9 days)
    stays.
    """
    return days_since > med_gap * 1.5 + 10


def _series_entry(
    ts,
    last,
    interval,
    factor,
    n,
    typical,
    med_gap,
    data_end,
    partner=None,
    also=None,
    match=None,
    group=None,
    rule=None,
    monthly_amount=None,
    key=None,
):
    """The record of a detected series - identical for the generic branch and
    the fixed-cost branch of detect_recurring().

    `monthly` is normally the LAST payment (a price increase should show up
    in the monthly sum immediately). If the amounts fluctuate strongly
    (`monthly_amount` set, see evaluate()), the typical amount counts instead
    - otherwise a single roaming bill as the last payment would have tripled
    the monthly sum."""
    last_amt = -eff(last)
    days_since = (data_end - date.fromisoformat(last["booking_date"])).days
    return {
        "partner": partner or last["partner"],
        "also": also or [],
        "cat_name": last["cat_name"] or "Unkategorisiert",
        "cat_color": last["cat_color"] or "#C4C4C4",
        "group": group or ("wohnen" if last["cat_name"] == "Wohnung" else "privat"),
        "interval": interval,
        "n": n,
        "amount": typical,
        "avg_all": q(sum(-eff(t) for t in ts) / len(ts)),
        "monthly": q((last_amt if monthly_amount is None else monthly_amount) * factor),
        "full_amount": q(-last["amount"]),
        # reduced (shared fixed costs, refund)? Only then does the page show
        # "von <full amount>" - for a fluctuating bill it otherwise said
        # "von 44.94" (user 2026-10-14: "das teile ich mir doch mit gar
        # niemanden" - I do not share that with anyone)
        "shared": abs(-last["amount"] - last_amt) > EPS,
        "last_date": last["booking_date"],
        "last_amount": last_amt,
        "days_since": days_since,
        "ended": _series_ended(days_since, med_gap),
        "match": match or [],
        "rule": rule,  # rule pattern that holds the series together
        "key": key,  # series key (series_overrides: hide/force)
        "forced": False,
        "ids": [t["id"] for t in ts],  # bookings of the series (assign refunds, incoming payments)
    }


def _forced_entry(ts, key, data_end, rule=None):
    """Series the user declared recurring costs by hand (series_overrides
    mode='force'): no rhythm or amount check, rhythm estimated from the median
    gap, typical amount = median. User request 2026-10-10: "kann man auch
    zahlungen auswaehlen die bereits laufende kosten sind? zB wurde ja drei
    anfangs nie erkannt" (can one also pick payments that already are
    recurring costs? e.g. the mobile provider was never detected at first)."""
    ts = sorted(ts, key=lambda t: t["booking_date"])
    ds = [date.fromisoformat(t["booking_date"]) for t in ts]
    gaps = [g for g in ((ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)) if g > 0]
    med = _median(gaps) if gaps else 31
    interval, factor = ("quartalsweise", 1 / 3) if 70 <= med <= 110 else ("monatlich", 1.0)
    typical = _median([q(-eff(t)) for t in ts])
    names = Counter((t["partner"] or "").strip() for t in ts if (t["partner"] or "").strip())
    main_name = names.most_common(1)[0][0] if names else (ts[-1]["partner"] or key)
    common = sorted(set.intersection(*[_series_tokens(t) for t in ts]) or {""})
    e = _series_entry(
        ts,
        ts[-1],
        interval,
        factor,
        len(ts),
        typical,
        med if gaps else 31,
        data_end,
        partner=main_name,
        also=[n for n, _ in names.most_common()[1:]],
        match=[rule] if rule else common,
        rule=rule,
        key=key,
    )
    e["forced"] = True
    return e


def _series_skipped(t):
    """Did the user remove this booking from the recurring costs
    (`extra.series_skip` = key of the series, series page)? User 2026-10-14:
    "einzelne zahlungen rausnehmen koennen, welche bereits zusammengefasst
    waren" (be able to remove individual payments that were already grouped)."""
    ex = t["extra"] or ""
    return "series_skip" in ex and bool(json.loads(ex).get("series_skip"))


def _rejected_group(ts, key):
    """Candidate for "als laufende Kosten aufnehmen" (add as recurring cost):
    a booking group the detection did NOT accept as a series."""
    ts = sorted(ts, key=lambda t: t["booking_date"])
    names = Counter((t["partner"] or "").strip() for t in ts if (t["partner"] or "").strip())
    last = ts[-1]
    return {
        "key": key,
        "partner": names.most_common(1)[0][0] if names else key,
        "n": len(ts),
        "last_date": last["booking_date"],
        "last_amount": q(-eff(last)),
        "cat_name": last["cat_name"] or "Unkategorisiert",
        "total": q(sum(-eff(t) for t in ts)),
    }


def _one_per_month(ts):
    """Exactly ONE payment per calendar month, no month skipped, over at least
    three months? Then it is a monthly series - no matter how much the
    amounts fluctuate (mobile bill with roaming, electricity back payment,
    insurance with deductible). User request 2026-09-24: "falls als abo
    verbucht und es kommt monatlich genau einmal, dann koennen die Betraege
    natuerlich auch stark variieren" (if booked as a subscription and it
    comes exactly once a month, the amounts may of course vary strongly)."""
    months = [t["booking_date"][:7] for t in ts]
    if len(months) < 3 or len(set(months)) != len(months):
        return False  # two payments in one month -> not "exactly once"
    ms = sorted(months)
    cur = ms[0]
    for nxt in ms[1:]:
        y, mo = int(cur[:4]), int(cur[5:7]) + 1
        if mo == 13:
            y, mo = y + 1, 1
        cur = "%04d-%02d" % (y, mo)
        if nxt != cur:
            return False  # a month is missing -> gap, not "every month"
    return True


def _median(values):
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else q((vals[mid - 1] + vals[mid]) / 2)


def _gaps_fit(gaps, lo, hi, med):
    """Does the gap sequence fit the grid?

    ONE outlier is allowed as long as it is no longer than two cycles and
    enough other gaps stand against it - exactly that happens on a provider
    change: Blizzard was debited on the 8th, after the change on the 25th,
    the transition gap is 48 instead of ~31 days. Without this tolerance the
    whole series would have failed.
    """
    bad = [g for g in gaps if not (lo <= g <= hi)]
    if not bad:
        return True
    return len(gaps) >= 3 and len(bad) == 1 and bad[0] <= 2 * med


def _data_end(con):
    """The reference point for "is it still running?" is the state of the
    DATA, not today - whoever does not import for two months should not
    suddenly see all subscriptions as cancelled."""
    r = con.execute(
        "SELECT MAX(booking_date) FROM transactions WHERE status='active' AND source IN ('bank','paypal','card')"
    ).fetchone()
    return date.fromisoformat(r[0]) if r and r[0] else date.today()


def _group_series(txs, fixed_pats, rule_pats, merges, merge_targets):
    """Group bookings by series key. Series identity: first the RULE that
    matches the booking (same resolution as apply_rules: priority, then age)
    - a rule "blizzard" combines "Blizzard Entertainment" and "GLOBAL COLLECT
    BV ... blizzard" into one series without heuristics (user request
    2026-09-15). Without a matching rule, the payee without digits. Keys
    merged by hand (merges) bypass the rule key, otherwise source and target
    end up in two groups. Returns (series, series_rule)."""
    series, series_rule = {}, {}
    for t in txs:
        v = eff(t)
        if v >= 0 or t["cat_kind"] in ("income", "transfer") or _series_skipped(t):
            continue
        # the configured fixed costs run in their own branch (grouped by pattern
        # instead of partner string) - exclude them here, otherwise a change of
        # payment method counts as an additional series (real bug).
        hay = ((t["partner"] or "") + " " + (t["description"] or "")).lower()
        if any(p.lower() in hay for p in fixed_pats):
            continue
        rule = next((p for p in rule_pats if p in hay), None)
        pk = _norm_partner(t["partner"])
        if pk in merges or pk in merge_targets:
            key = merges.get(pk, pk)
        elif rule:
            key = "rule:" + rule
            series_rule[key] = rule
        else:
            key = pk
            if len(key) < 3:
                continue
        key = merges.get(key, key)
        if key.startswith("rule:"):
            series_rule[key] = key[5:]
        series.setdefault(key, []).append(t)
    return series, series_rule


def _amount_clusters(ts):
    """Amount clusters: split off outliers (back payments, one-offs to the
    same payee), keep the dominant series. Sorted ASCENDING (-eff is the
    positive amount); an amount belongs to the current cluster as long as it
    is at most 25 % above its smallest value. Returns (clusters, main) with
    main sorted chronologically."""
    clusters = []
    for t in sorted(ts, key=lambda t: -eff(t)):
        a = -eff(t)
        if clusters and a <= -eff(clusters[-1][0]) * 1.25:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    main = max(clusters, key=len)
    main.sort(key=lambda t: t["booking_date"])
    return clusters, main


def _rhythm(rows):
    """Does the gap sequence fit the monthly or quarterly grid? Booking days
    drift (weekend, month change) -> generous grid, but ALL gaps must fit
    reasonably (against random series). Returns (interval, factor, med_gap)
    or None."""
    ds = [date.fromisoformat(t["booking_date"]) for t in sorted(rows, key=lambda t: t["booking_date"])]
    gg = [g for g in ((ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)) if g > 0]
    if not gg:
        return None
    med = sorted(gg)[len(gg) // 2]
    if 20 <= med <= 40 and _gaps_fit(gg, 15, 46, med):
        return "monatlich", 1.0, med
    if 80 <= med <= 100 and _gaps_fit(gg, 70, 110, med):
        return "quartalsweise", 1 / 3, med
    return None


def _evaluate_group(ts, rule, key, top_cat, lauf_cats, data_end):
    """Check one booking group: rhythm, amount stability, category. Returns
    the series record or None.

    Rhythm in three stages: (1) on the main amount cluster (protects
    subscriptions from parallel one-off purchases at the same payee); (2)
    with exactly ONE outlier on all payments - a single high bill (mobile
    with roaming) otherwise tore the chain apart (real case 2026-09-24); (3)
    exactly one payment per calendar month in one of the user's fixed-cost
    categories is a monthly series no matter how much the amounts fluctuate."""
    if len(ts) < 3:
        return None
    clusters, main = _amount_clusters(ts)
    hit = (_rhythm(main) if len(main) >= 2 else None) or (_rhythm(ts) if len(ts) - len(main) == 1 else None)
    if (
        not hit
        and _one_per_month(ts)
        and top_cat.get((max(ts, key=lambda t: t["booking_date"])["cat_name"] or ""), "") in lauf_cats
    ):
        hit = ("monatlich", 1.0, 31)
        main = sorted(ts, key=lambda t: t["booking_date"])
    if not hit:
        return None
    interval, factor, med_gap = hit
    # Inherently variable categories are never fixed costs - checked via the
    # TOPMOST category since subcategories exist.
    if top_cat.get(main[-1]["cat_name"] or "", "") in VARIABLE_CATS:
        return None
    # "Typical" = most frequent amount (mode) in the main cluster, rounded to
    # cents; if every amount occurred only once (fluctuating bill), the median.
    cnt = Counter(q(-eff(t)) for t in main)
    typical, seen = cnt.most_common(1)[0]
    if seen == 1:
        typical = _median(cnt)
    if typical <= 0:
        return None
    # If the cluster split kicked in, the last payment is a poor estimator for
    # "per month" (a roaming bill as the last one would be triple).
    monthly_amount = typical if len(clusters) > 1 else None
    # "last" is the REALLY last payment (even as an outlier) - a price increase
    # should be visible immediately. `amount` stays typical.
    last = max(ts, key=lambda t: t["booking_date"])
    # Display name: the most frequent payee string (after a payment-route
    # change the established name, the others go into `also`).
    names = Counter((t["partner"] or "").strip() for t in ts if (t["partner"] or "").strip())
    main_name = names.most_common(1)[0][0] if names else (last["partner"] or "")
    # Search term for the drilldown: rule pattern, else common word, else payee.
    common = sorted(set.intersection(*[_series_tokens(t) for t in ts]) or {""})
    return _series_entry(
        ts,
        last,
        interval,
        factor,
        len(ts),
        typical,
        med_gap,
        data_end,
        partner=main_name,
        also=[n for n, _ in names.most_common()[1:]],
        match=[rule] if rule else common,
        rule=rule,
        monthly_amount=monthly_amount,
        key=key,
    )


def _fixed_cost_series(con, fixed_pats, data_end):
    """The configured fixed costs are already confirmed as fixed costs by
    auto_split_wohnung_fixed - they do not have to prove themselves against
    the generic detection. Grouped by PATTERN (e.g. "Netflix"), not by
    partner string, so that a change of payment method stays ONE series
    (real bug 2026-08-02: 7 EUR twice). No filter on effective_amount: a
    position with a 100 % share has none (docs reconciliation 2026-09-11)."""
    _fx_sql, _fx_args = fixed_cost_like_sql(fixed_pats, "t.partner")
    fixed_rows = con.execute(
        "SELECT t.*, c.name AS cat_name, c.color AS cat_color FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.category_id "
        "WHERE t.status='active' AND t.source IN ('bank','paypal','card') "
        "AND COALESCE(t.effective_amount, t.amount) < 0 "
        "AND (" + _fx_sql + ") "
        "ORDER BY t.booking_date",
        _fx_args,
    ).fetchall()
    by_pattern = {}
    for t in fixed_rows:
        if _series_skipped(t):
            continue
        hay = ((t["partner"] or "") + " " + (t["description"] or "")).lower()
        pat = next((p for p in fixed_pats if p.lower() in hay), None)
        if pat:
            by_pattern.setdefault(pat, []).append(t)
    out = []
    for pat, ts in by_pattern.items():
        typical = Counter(q(-eff(t)) for t in ts).most_common(1)[0][0]
        # A configured fixed-cost position can end too (moving house, provider
        # change) - then it should not sit in the monthly sum forever.
        fdates = [date.fromisoformat(t["booking_date"]) for t in ts]
        fgaps = [g for g in ((fdates[i + 1] - fdates[i]).days for i in range(len(fdates) - 1)) if g > 0]
        fmed = sorted(fgaps)[len(fgaps) // 2] if fgaps else 31
        out.append(
            _series_entry(
                ts,
                ts[-1],
                "monatlich",
                1.0,
                len(ts),
                typical,
                fmed,
                data_end,
                match=[pat.lower()],
                group="wohnen",
                key="fixed:" + pat.lower(),
            )
        )
    return out


def detect_recurring(con, with_rejected=False, all_rejected=False):
    """Detects recurring payments: same payee, regular gap (monthly/
    quarterly), stable amount. Returns series with normalized monthly costs,
    grouped into Wohnen (category Wohnung) and Privat. With
    with_rejected=True additionally the booking groups that failed
    (candidates for "add as recurring cost by hand"); with all_rejected=True
    without category filter and regardless of 'dismiss'.

    Flow: _group_series (rule/payee/merge) -> _merge_related_series
    (payment-route change) -> _evaluate_group per group; if a rule group
    fails, it is split by payee and each subgroup is checked on its own (a
    broad rule may merge, but must never make a series disappear); forced
    and merged series (series_overrides force/merge) count without a check
    (_forced_entry); plus the configured fixed costs (_fixed_cost_series)."""
    fixed_pats = fixed_cost_patterns(con)
    merges = {
        r["key"]: r["target"]
        for r in con.execute("SELECT key, target FROM series_overrides WHERE mode='merge' AND COALESCE(target,'')!=''")
    }
    merge_targets = set(merges.values())
    top_cat = top_category_names(con)
    # Categories the user declared recurring costs - only there does the relaxed
    # rule "exactly one payment per month" apply.
    lauf_cats = set(get_setting_list(con, "fixkosten_privat_cats"))
    txs = con.execute(
        "SELECT t.*, c.name AS cat_name, c.color AS cat_color, c.kind AS cat_kind "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id "
        "WHERE t.status='active' ORDER BY t.booking_date"
    ).fetchall()
    rules = con.execute("SELECT pattern FROM rules ORDER BY priority, id").fetchall()
    rule_pats = [r["pattern"].lower() for r in rules if (r["pattern"] or "").strip()]
    series, series_rule = _group_series(txs, fixed_pats, rule_pats, merges, merge_targets)
    series = _merge_related_series(series)
    data_end = _data_end(con)
    overrides = {r["key"]: r["mode"] for r in con.execute("SELECT key, mode FROM series_overrides")}
    out, rejected = [], []

    def evaluate(ts, rule, key):
        return _evaluate_group(ts, rule, key, top_cat, lauf_cats, data_end)

    def candidate(ts, key):
        # Only offer what MAY count as recurring costs at all (categories from
        # the settings or Wohnung) and what the user has not clicked away (mode
        # 'dismiss'). User 2026-10-13: "wenn sachen also nicht abo sind,
        # sollten sie gar nicht dort aufscheinen" (if things are not a
        # subscription they should not show up there at all).
        if all_rejected:
            rejected.append(_rejected_group(ts, key))
            return
        if overrides.get(key) == "dismiss":
            return
        last = max(ts, key=lambda t: t["booking_date"])
        top = top_cat.get(last["cat_name"] or "", last["cat_name"] or "")
        if top in lauf_cats or top == "Wohnung":
            rejected.append(_rejected_group(ts, key))

    for key, ts in series.items():
        rule = series_rule.get(key)
        entry = evaluate(ts, rule, key)
        if entry is not None:
            out.append(entry)
            continue
        if overrides.get(key) == "force" or key in merge_targets:
            out.append(_forced_entry(ts, key, data_end, rule))
            continue
        if rule:
            by_partner = {}
            for t in ts:
                by_partner.setdefault(_norm_partner(t["partner"]), []).append(t)
            for pkey, pts in by_partner.items():
                sub = evaluate(pts, None, pkey)
                if sub is not None:
                    out.append(sub)
                elif overrides.get(pkey) == "force":
                    out.append(_forced_entry(pts, pkey, data_end))
                else:
                    candidate(pts, pkey)
        else:
            candidate(ts, key)

    out += _fixed_cost_series(con, fixed_pats, data_end)
    out.sort(key=lambda s: (s["group"], -s["monthly"]))
    if with_rejected:
        rejected.sort(key=lambda g: (-g["n"], g["last_date"]), reverse=False)
        return out, rejected
    return out


# What may show up under "Laufende Kosten / Privat" at all. Subcategories of
# these count as well (resolved via the topmost category).
# Which categories may appear under "Laufende Kosten / Privat" is stored in
# the settings (fixkosten_privat_cats).


def recurring_groups(con, with_hidden=False, series=None):
    """Detected series prepared for DISPLAY.

    Since 2026-10-10 `series_overrides` apply: hidden series (mode 'hide')
    are missing from all four lists and come back as a fifth value with
    with_hidden=True; forced ones (mode 'force', `forced`) are always
    included regardless of category - the user explicitly declared them so.
    `series` can be passed in if detect_recurring() has already run
    (/fixkosten needs both).

    One source for both places that show recurring costs: the dashboard tile
    and /fixkosten. Before, the tile bluntly summed ALL detected series, the
    page only Wohnen + the allowed Privat categories - so the same number
    meant something different in two places (real: tile 949,82 EUR, including
    McDonald's and a coffee machine).

    What the detection finds remains a heuristic over rhythm and amount
    stability; which of it counts as "recurring costs" is decided by the
    category (setting `fixkosten_privat_cats`) - a more honest criterion than
    tweaking the heuristic.
    """
    if series is None:
        series = detect_recurring(con)
    top_name = top_category_names(con)
    privat_cats = set(get_setting_list(con, "fixkosten_privat_cats"))
    overrides = {r["key"]: r["mode"] for r in con.execute("SELECT key, mode FROM series_overrides")}
    hidden = [s for s in series if overrides.get(s.get("key")) == "hide"]
    series = [s for s in series if overrides.get(s.get("key")) != "hide"]

    def gehoert_dazu(s):
        if s.get("forced") or s["group"] == "wohnen":
            return True
        return top_name.get(s["cat_name"], s["cat_name"]) in privat_cats

    relevant = [s for s in series if gehoert_dazu(s)]
    wohnen = [s for s in relevant if s["group"] == "wohnen" and not s["ended"]]
    privat = [s for s in relevant if s["group"] == "privat" and not s["ended"]]
    # Cancelled/expired: do not delete, but do not count either (user request
    # 2026-09-11: "patreon scheint immer noch auf unter laufende kosten, obwohl
    # die letzte zahlung 2 monate zurueckliegt - weil gekuendigt" - still listed
    # although cancelled two months ago).
    beendet = sorted([s for s in relevant if s["ended"]], key=lambda s: s["last_date"], reverse=True)
    sums = {
        "wohnen": q(sum(s["monthly"] for s in wohnen)),
        "privat": q(sum(s["monthly"] for s in privat)),
    }
    sums["total"] = q(sums["wohnen"] + sums["privat"])
    if with_hidden:
        return wohnen, privat, beendet, sums, hidden
    return wohnen, privat, beendet, sums


def _series_repayments(con, series):
    """Per series: sum of the repayments attached to its bookings (matches
    kind='repayment') - for the display "nach Rueckzahlungen"."""
    repaid = {}
    for r in con.execute("SELECT bank_tx, SUM(amount) s FROM matches WHERE kind='repayment' GROUP BY bank_tx"):
        repaid[r["bank_tx"]] = r["s"] or 0.0
    for s in series:
        s["repaid"] = q(sum(repaid.get(i, 0.0) for i in s.get("ids", [])))


def _series_term(s):
    """Search term through which the bookings of a series are found (rule
    pattern, common word, otherwise the payee)."""
    if s.get("rule"):
        return s["rule"]
    if s.get("match") and s["match"][0]:
        return s["match"][0]
    return s.get("partner") or ""


def _series_senders(con, term, ids):
    """Senders of incoming payments for the series page, grouped via
    _norm_partner. `rule` is the incoming pattern of the repayment rule that
    matches this sender's transfers (partner + payment reference) - NOT only
    the partner name: whoever writes "Spotify" into the reference hangs on
    the rule "Spotify", not on a rule with his name. Until 2026-10-14 only
    the name counted as a hit, two senders assigned that way showed up
    unchecked, and "Speichern" would have deleted the shared rule.
    `linked_ids` = transfers attached to THIS series."""
    rules = {
        r["in_pattern"].lower(): r["in_pattern"]
        for r in con.execute("SELECT in_pattern FROM repay_rules WHERE lower(out_pattern)=lower(?)", (term,))
    }
    linked_in = {
        r[0]
        for r in con.execute(
            "SELECT other_tx FROM matches WHERE kind='repayment' AND bank_tx IN (%s)"
            % (",".join("?" * len(ids)) or "-1"),
            list(ids),
        )
    }
    taken = {r[0] for r in con.execute("SELECT other_tx FROM matches WHERE kind='repayment'")} - linked_in
    groups = {}
    for t in con.execute(
        "SELECT * FROM transactions WHERE source IN ('bank','paypal','card') AND amount>0.004 "
        "AND status IN ('active','excluded') ORDER BY booking_date DESC"
    ):
        if t["id"] in taken:
            continue  # belongs to another series
        if t["status"] == "excluded" and t["id"] not in linked_in:
            continue
        p = (t["partner"] or "").strip() or (t["description"] or "").strip()[:40]
        if not p:
            continue
        g = groups.setdefault(
            _norm_partner(p) or p.lower(),
            {
                "partner": p,
                "n": 0,
                "amounts": [],
                "last": t["booking_date"],
                "linked": 0,
                "linked_ids": [],
                "rule": None,
            },
        )
        g["n"] += 1
        g["amounts"].append(q(t["amount"]))
        if t["id"] in linked_in:
            g["linked"] += 1
            g["linked_ids"].append(t["id"])
        hay = ((t["partner"] or "") + " " + (t["description"] or "")).lower()
        if g["rule"] is None:
            g["rule"] = next((orig for low, orig in rules.items() if low and low in hay), None)
    rows = []
    for g in groups.values():
        g["typical"] = Counter(g["amounts"]).most_common(1)[0][0]
        g["active"] = g["rule"] is not None
        rows.append(g)
    rows.sort(key=lambda g: (not g["active"], -g["n"], g["partner"]))
    return rows, rules
