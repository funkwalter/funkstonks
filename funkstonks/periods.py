"""Periods: month lists, labels, previous period, dropdown value,
fixed-cost month smoothing."""

import re

from .db import fixed_cost_like_sql, fixed_cost_patterns


def wohnung_fixed_display_months(con):
    """Assigns each bank booking of a fixed-cost pattern a consecutive calendar
    month (chronologically, one booking per month; several bookings on the
    same day share one slot) - banks book the rent sometimes on the 28th,
    sometimes on the 3rd, economically it is still ONE monthly instalment.
    Amounts are left alone, only the display month changes. A partner with a
    single booking appears with its real month. Returns {tx_id: 'YYYY-MM'};
    used by the dashboard, /jahr, the matrix and /transactions."""
    like_sql, like_args = fixed_cost_like_sql(fixed_cost_patterns(con))
    rows = con.execute(
        "SELECT id, partner, booking_date, amount, effective_amount FROM transactions "
        "WHERE status='active' AND source='bank' AND partner IS NOT NULL AND (" + like_sql + ") ORDER BY booking_date",
        like_args,
    ).fetchall()
    by_partner = {}
    for r in rows:
        by_partner.setdefault(r["partner"], []).append(r)
    mapping = {}
    for partner, txs in by_partner.items():
        by_date = {}
        for t in txs:
            by_date.setdefault(t["booking_date"], []).append(t)
        dates = sorted(by_date)
        if len(dates) < 2:
            # Nothing to smooth - still add it to the mapping (with the real
            # month) so that /transactions finds the row again in the month
            # filter: the filter removes fixed-cost bank rows via SQL and
            # brings them back through this mapping. A position with only ONE
            # booking had thereby vanished from every month list while still
            # being counted on the dashboard (fuzz test 2026-09-11).
            for t in by_date[dates[0]]:
                mapping[t["id"]] = dates[0][:7]
            continue
        y, m = int(dates[0][:4]), int(dates[0][5:7])
        for d in dates:
            for t in by_date[d]:
                mapping[t["id"]] = f"{y:04d}-{m:02d}"
            m += 1
            if m > 12:
                m = 1
                y += 1
    return mapping


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def valid_month(m):
    return bool(m) and bool(MONTH_RE.match(m.strip()))


def months_between(a, b):
    """All months from a to b inclusive, as a 'YYYY-MM' list. Invalid input
    (?from=abc) yields an empty list instead of a crash - broken parameters
    arrive via bookmarks and typos."""
    if not valid_month(a) or not valid_month(b):
        return []
    a, b = a.strip(), b.strip()
    if a > b:
        a, b = b, a
    y, mo = int(a[:4]), int(a[5:7])
    out = []
    while "%04d-%02d" % (y, mo) <= b:
        out.append("%04d-%02d" % (y, mo))
        mo += 1
        if mo > 12:
            y, mo = y + 1, 1
        if len(out) > 600:  # emergency brake against broken input
            break
    return out


MONTH_NAMES = [
    "",
    "Jänner",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def period_label(months):
    """Readable label for a period: "August 2026", "2026",
    "Mai - August 2026" or "Nov 2025 - Aug 2026"."""
    months = [m for m in months if valid_month(m)]
    if not months:
        return ""
    lo, hi = min(months), max(months)

    def nice(m):
        return "%s %s" % (MONTH_NAMES[int(m[5:7])], m[:4])

    if lo == hi:
        return nice(lo)
    if lo[:4] == hi[:4]:
        # Whole year? Then the year number is enough.
        if lo[5:7] == "01" and hi[5:7] == "12":
            return lo[:4]
        return "%s – %s %s" % (MONTH_NAMES[int(lo[5:7])], MONTH_NAMES[int(hi[5:7])], lo[:4])
    return "%s – %s" % (nice(lo), nice(hi))


def period_args(months):
    """Query parameters with which /transactions shows the same period.

    A single month deliberately stays `month=` - plenty of existing links
    point to that, and it is the most common case.
    """
    if not months:
        return ""
    if len(months) == 1:
        return "month=" + months[0]
    return "from=%s&to=%s" % (min(months), max(months))


def previous_period(sel_months, months, sel_range=""):
    """The equally long period immediately before - previous month for a
    month, the same months of the previous year for a year, the N calendar
    months before for a range. Only if EVERY one of those months has
    bookings; otherwise empty (no card, no deltas). A half previous period -
    July to September against May and June because tracking started in May -
    makes no meaningful comparison (user 2026-09-15: "macht gar keinen Sinn"
    - makes no sense at all)."""
    if not sel_months or sel_range == "all":
        return []
    have = set(months)
    if len(sel_range) == 4 and sel_range.isdigit():
        prev = ["%04d%s" % (int(m[:4]) - 1, m[4:]) for m in sel_months]
    else:
        n = len(months_between(sel_months[0], sel_months[-1]))
        prev = []
        cur = sel_months[0]
        for _ in range(n):
            y, mo = int(cur[:4]), int(cur[5:7]) - 1
            if mo == 0:
                y, mo = y - 1, 12
            cur = "%04d-%02d" % (y, mo)
            prev.append(cur)
    if any(m not in have for m in prev):
        return []
    return sorted(prev)


def _period_name(sel_months, months, requested=""):
    """Which entry should the period dropdown show as selected?

    Returns the value `range=` would have - or "" for a freely assembled
    range that has no entry. If the user explicitly chose an entry
    (`requested` = the `range=` parameter), exactly that one stays: "letzte
    3 Monate" otherwise jumped back to "Eigener Zeitraum" and "alles
    zusammen" to the year number (user 2026-10-14: "steht danach wieder der
    alte ... BUG!" - afterwards the old one is shown again).
    """
    if not sel_months:
        return ""
    if requested and re.fullmatch(r"all|last\d+|\d{4}|\d{4}-\d{2}", requested):
        return requested
    if len(sel_months) == 1:
        return sel_months[0]
    # Check the year BEFORE "all": if all data lies in one year both choices
    # coincide - then the year number is the more telling label ("2026"
    # instead of "Mai - September 2026").
    years = {m[:4] for m in sel_months}
    if len(years) == 1:
        y = years.pop()
        if set(sel_months) == {m for m in months if m[:4] == y}:
            return y
    if set(sel_months) == set(months):
        return "all"
    return ""


def _dashboard_period(args, months, slot_months=()):
    """Query parameters -> list of months to display (ascending).

    `months` is the list of months WITH bookings, descending. Only months
    that really exist are returned - otherwise the Sankey would show empty
    columns and the key figures would divide by months without data.
    `slot_months`: months into which the fixed-cost smoothing moves a booking
    (wohnung_fixed_display_months). Those count for ranges even without
    bookings of their own - otherwise the rent of 28.07. (slot August) was
    missing in the period June-September although the linked list shows it
    (fuzz 2026-10-13, drilldown rule). The month dropdown sticks to the real
    months.
    """
    have = set(months)
    reach = have | set(slot_months)

    def only_real(ms):
        return sorted(m for m in ms if m in reach)

    rng = (args.get("range") or "").strip()
    f, t = (args.get("from") or "").strip(), (args.get("to") or "").strip()
    if f or t:
        return only_real(months_between(f or t, t or f))
    if rng:
        if rng == "all":
            # slot months only WITHIN the real span (a slot after the newest
            # real month also lies outside the linked list)
            lo, hi = (min(have), max(have)) if have else ("", "")
            return sorted(m for m in reach if lo <= m <= hi)
        if rng.startswith("last") and rng[4:].isdigit() and int(rng[4:]) >= 1:
            last = sorted(months)[-int(rng[4:]) :] if months else []
            return only_real(months_between(last[0], last[-1])) if last else []
        if len(rng) == 4 and rng.isdigit():
            lo, hi = (min(have), max(have)) if have else ("", "")
            return only_real(m for m in reach if m[:4] == rng and lo <= m <= hi)
        if rng in have:
            return [rng]
    m = (args.get("month") or "").strip()
    if m in have:
        return [m]
    return [months[0]] if months else []
