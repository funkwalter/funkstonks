"""Evaluations: counting bookings, summarize() with netting, year matrix,
comparison, Sankey nodes."""

from .categories import _family_shades, _shade, category_qs, top_category_names
from .money import EPS, is_zero, q
from .periods import period_args, period_label, wohnung_fixed_display_months


def _smoothed_transactions(con, wohnung_map, key, **stats_kwargs):
    """Counting bookings of a month (key 'YYYY-MM') or year ('YYYY') by
    ECONOMIC month: fixed-cost bank bookings follow the slot from
    wohnung_fixed_display_months(), not the booking date - a rent of 30.12.
    that belongs to January counts there. Dashboard, /jahr, matrix and
    drilldown all use this one set (otherwise they differ by exactly one
    rent, fuzz test 2026-09-11)."""
    n = len(key)
    txs = []
    for t in stats_transactions(con, **stats_kwargs):
        tid = t["id"] if "id" in t.keys() else None
        if tid in wohnung_map and wohnung_map[tid][:n] != key:
            continue
        txs.append(t)
    have = {t["id"] for t in txs if "id" in t.keys()}
    extra = [tid for tid, mm in wohnung_map.items() if mm[:n] == key and tid not in have]
    txs += stats_transactions(con, ids=extra)
    return txs


def dashboard_month_transactions(con, month, wohnung_map):
    return _smoothed_transactions(con, wohnung_map, month, month=month)


def year_transactions(con, year, wohnung_map=None):
    if wohnung_map is None:
        wohnung_map = wohnung_fixed_display_months(con)
    return _smoothed_transactions(con, wohnung_map, year, year=year)


def dashboard_period_transactions(con, months, wohnung_map):
    """Several months in one go (user request 2026-09-11: "die moeglichkeit
    einblenden mehrere monate zu verknuepfen oder ganze jahre anzuzeigen" -
    an option to combine several months or show whole years).

    Deliberately runs through the same month function instead of its own
    query: the fixed-cost month smoothing has to be decided per month, and
    the display months do not overlap.
    """
    out = []
    for m in months:
        out.extend(dashboard_month_transactions(con, m, wohnung_map))
    return out


def eff(t):
    return t["effective_amount"] if t["effective_amount"] is not None else t["amount"]


def stats_transactions(con, month=None, year=None, ids=None):
    """Countable transactions; split payments are counted per part.

    `ids` fetches specific individual bookings (for the fixed-cost month
    smoothing) - also with resolved splits, otherwise the parent row would
    come in raw and uncategorized and the parts would additionally be counted
    in the booking month (real double-counting case, review 2026-09-13).
    Split parts carry the `id` of the parent booking so the smoothing can
    move them just like whole bookings."""
    q = (
        "SELECT t.*, c.name AS cat_name, c.color AS cat_color, c.kind AS cat_kind "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id "
        "WHERE t.status='active'"
    )
    args = []
    if ids is not None:
        if not ids:
            return []
        q += " AND t.id IN (%s)" % ",".join("?" * len(ids))
        args += list(ids)
    elif month:
        q += " AND substr(t.booking_date,1,7)=?"
        args.append(month)
    elif year:
        q += " AND substr(t.booking_date,1,4)=?"
        args.append(year)
    q += " ORDER BY t.booking_date"
    txs = con.execute(q, args).fetchall()
    split_map = {}
    for s in con.execute(
        "SELECT s.*, c.name AS cat_name, c.color AS cat_color, "
        "c.kind AS cat_kind FROM splits s "
        "JOIN categories c ON c.id=s.category_id"
    ):
        split_map.setdefault(s["tx_id"], []).append(s)
    out = []
    for t in txs:
        if t["id"] in split_map:
            for s in split_map[t["id"]]:
                out.append(
                    {
                        "id": t["id"],
                        "booking_date": t["booking_date"],
                        "partner": t["partner"],
                        "description": t["description"],
                        "amount": s["amount"],
                        "effective_amount": s["amount"],
                        "cat_name": s["cat_name"],
                        "cat_color": s["cat_color"],
                        "cat_kind": s["cat_kind"],
                    }
                )
        else:
            out.append(t)
    return out


def todo_count(con):
    return con.execute(
        "SELECT COUNT(*) FROM transactions WHERE status='active' AND category_id IS NULL "
        "AND id NOT IN (SELECT tx_id FROM splits)"
    ).fetchone()[0]


def summarize(txs):
    inc, exp, uncat = {}, {}, {}
    for t in txs:
        v = eff(t)
        if abs(v) < EPS:
            continue
        kind = t["cat_kind"]
        name = t["cat_name"] or "Unkategorisiert"
        color = t["cat_color"] or "#C4C4C4"
        if kind == "transfer":
            continue
        if kind == "income":
            inc.setdefault(name, [0.0, color])[0] += v
        elif kind == "expense":
            # refunds (v>0) reduce the category
            exp.setdefault(name, [0.0, color])[0] += -v
        else:
            # Uncategorized: ONE pot, net - exactly like a category. Before,
            # positive rows ended up as "income" and negative ones as "expense"
            # under the same name, while the click (cat=none) listed everything
            # together net - tile and list contradicted each other.
            uncat.setdefault(name, [0.0, color])[0] += -v
    for name, (net, color) in uncat.items():
        if net > EPS:
            exp.setdefault(name, [0.0, color])[0] += net
        elif net < -EPS:
            inc.setdefault(name, [0.0, color])[0] += -net
    # categories that are net <= 0 due to refunds are not shown as negative
    exp = {k: v for k, v in exp.items() if v[0] > EPS}
    return inc, exp


def hidden_positive_categories(txs):
    """Expense categories whose net is POSITIVE due to refunds (e.g. "Urlaub":
    flight refund > actual travel costs) - summarize() hides them completely
    (see the comment there) so they never falsely appear as income. For the
    Sankey display (user request 2026-08-02) make them visible anyway: this
    money was not really spent either, it came back (net) to the account -
    just like the balance. Separate function instead of changing summarize()
    itself, to leave its established return value (used in many places)
    untouched."""
    sums = {}
    for t in txs:
        if t["cat_kind"] != "expense":
            continue
        v = eff(t)
        if abs(v) < EPS:
            continue
        name = t["cat_name"] or "Unkategorisiert"
        color = t["cat_color"] or "#C4C4C4"
        sums.setdefault(name, [0.0, color])
        sums[name][0] += v
    return {k: (v, c) for k, (v, c) in sums.items() if v > EPS}


# Colour families for the functional Sankey colouring (user request 2026-08-02):
# expenses reddish, fixed costs orange-ish, income/investments/credit (balance +
# positive categories) greenish - replaces the individual category colour from
# the categories table ONLY in the Sankey (it stays unchanged everywhere else
# in the app, e.g. chips on /transactions, /categories, /fixkosten).
# HUE_AUSGABEN (the "Ausgaben" collector node itself, parent of
# Fixkosten+Nebenkosten) deliberately lies between red and orange - a blend,
# because it combines both families.
HUE_GREEN, HUE_ORANGE, HUE_RED, HUE_AUSGABEN = 145, 32, 6, 18
HUE_BLUE = 212  # separate branch "Investitionen" (user request 2026-09-01)


class _SankeyBuilder:
    """Builds nodes and links for the money-flow Sankey. One object per call of
    build_sankey_nodes(); the methods correspond to the columns: income
    (column 0) -> main branches (1) -> Fixkosten/Nebenkosten or pass-through
    (2) -> categories, balance, "+" categories (3). Pure data structure
    without layout coordinates - ECharts lays out by itself using node_col as
    "depth" with layoutIterations 0, so the insertion order is the drawing
    order."""

    def __init__(self, inc, exp, total_inc, total_exp, cat_id_by_name, hidden_pos, top_ancestor_name, fixkosten_cats):
        self.inc, self.exp = inc, exp
        self.total_inc, self.total_exp = total_inc, total_exp
        self.cat_id_by_name = cat_id_by_name
        self.hidden_pos = hidden_pos or {}
        self.top_ancestor_name = top_ancestor_name or {}
        self.fixkosten_cats = set(fixkosten_cats or ())
        self.labels, self.colors, self.node_col, self.node_filter = [], [], [], []
        self.src, self.tgt, self.val, self.lcol = [], [], [], []
        self.spacers = []  # unlabelled nodes (a branch passing through an intermediate column)
        self.inc_ids, self.income_ids, self.refund_id = [], [], None
        self.hidden_sum = sum(v for v, c in self.hidden_pos.values())
        self.inflow_total = total_inc + max(self.hidden_sum, 0.0)
        rows = sorted(exp.items(), key=lambda kv: -kv[1][0])
        self.fix_rows = [(n, v, c) for n, (v, c) in rows if self.is_fixkosten(n)]
        self.neben_rows = [
            (n, v, c) for n, (v, c) in rows if not self.is_fixkosten(n) and self.top_of(n) != "Investitionen"
        ]
        self.inv_total = sum(v for n, (v, c) in exp.items() if self.top_of(n) == "Investitionen")
        self.fix_total = sum(v for _, v, _ in self.fix_rows)
        self.neben_total = sum(v for _, v, _ in self.neben_rows)
        self.saldo = total_inc - total_exp
        self.guthaben_total = max(self.saldo, 0) + self.hidden_sum
        self.top, self.top_id, self.group_node, self.col2_branches = [], {}, {}, []
        self.orange_shades = self.red_shades = []

    # ---- helpers
    def top_of(self, n):
        return self.top_ancestor_name.get(n, n)

    def is_fixkosten(self, n):
        # via the topmost ancestor category so that subcategories of
        # "Wohnung"/"Abos" still count as fixed costs
        return self.top_of(n) in self.fixkosten_cats

    def node(self, name, color, col, cat_name=None, special=None):
        """Create a node (or return the index of an existing one). cat_name =>
        the node stands for exactly one category and is clickable; `special`
        is a raw filter value for collector nodes (e.g. the IDs of the
        children)."""
        if name not in self.labels:
            self.labels.append(name)
            self.colors.append(color)
            self.node_col.append(col)
            self.node_filter.append(
                special if special is not None else (self.cat_id_by_name.get(cat_name, "none") if cat_name else None)
            )
        return self.labels.index(name)

    def link(self, a, b, v, color):
        self.src.append(a)
        self.tgt.append(b)
        self.val.append(q(v))
        self.lcol.append(color + "66")

    @staticmethod
    def pct_of(v, total):
        return round(100 * v / total, 1) if total else 0

    @staticmethod
    def label(name, v, pct, sign=""):
        # absolute amount without abbreviation plus percent, the same %.2f convention as everywhere
        return f"{name} ({sign}{v:.2f} € · {sign}{pct}%)"

    # ---- column 0: income and refunds
    def emit_income(self):
        inc_rows = sorted(self.inc.items(), key=lambda kv: -kv[1][0])
        shades = _family_shades(len(inc_rows), HUE_GREEN, light_lo=0.24, light_hi=0.42)
        for (n, (v, c)), color in zip(inc_rows, shades):
            nid = self.node(self.label(n, v, self.pct_of(v, self.total_inc)), color, 0, cat_name=n)
            self.inc_ids.append((nid, v))
        self.income_ids = list(self.inc_ids)  # real income only
        # Categories in which money net CAME BACK are a real inflow: source on
        # the left, "+" children under Guthaben on the right. With the
        # categories in the name, otherwise the node does not tell where it
        # comes from.
        if self.hidden_sum > EPS:
            ids = [str(self.cat_id_by_name[n]) for n in self.hidden_pos if n in self.cat_id_by_name]
            top_names = [n for n, _ in sorted(self.hidden_pos.items(), key=lambda kv: -kv[1][0])]
            who = ", ".join(top_names[:3]) + (" …" if len(top_names) > 3 else "")
            nid = self.node(
                self.label("Rückzahlungen: " + who, self.hidden_sum, self.pct_of(self.hidden_sum, self.total_inc), "+"),
                _shade(HUE_GREEN, 0.40),
                0,
                special=",".join(ids) or None,
            )
            self.inc_ids.append((nid, self.hidden_sum))
            self.refund_id = nid

    def link_from_income(self, target_id, target_total, color, is_guthaben=False):
        """Each income source branches proportionally straight to the main
        branch (no intermediate account node). Refunds flow ONLY into the
        Guthaben branch (since 2026-10-13) - conservation: Guthaben = balance
        + refunds, the balance part comes from income. Without income
        proportionally over all sources, otherwise a branch without income
        would have no link."""
        if target_total <= EPS:
            return
        if self.refund_id is None or self.total_inc <= EPS:
            sources, source_total = self.inc_ids, self.inflow_total
        elif is_guthaben:
            self.link(self.refund_id, target_id, self.hidden_sum, color)
            target_total = target_total - self.hidden_sum
            sources, source_total = self.income_ids, self.total_inc
        else:
            sources, source_total = self.income_ids, self.total_inc
        if target_total <= EPS or source_total <= EPS:
            return
        for nid, v in sources:
            amt = v * target_total / source_total
            if amt > EPS:
                self.link(nid, target_id, amt, color)

    # ---- column 1: main branches
    def emit_main_branches(self):
        """Ausgaben, Investitionen (separate blue branch, not under Guthaben)
        and Guthaben, sorted by amount. "Ausgaben" gets the IDs of the
        categories that really flow into it (net positive ones sit under
        Guthaben)."""
        top = []
        if self.fix_total + self.neben_total > EPS:
            top.append(("Ausgaben", self.fix_total + self.neben_total, _shade(HUE_AUSGABEN, 0.34), None))
        if self.inv_total > EPS:
            top.append(("Investitionen", self.inv_total, _shade(HUE_BLUE, 0.34), "Investitionen"))
        if self.guthaben_total > EPS:
            top.append(("Guthaben", self.guthaben_total, _shade(HUE_GREEN, 0.30), None))
        top.sort(key=lambda b: -b[1])
        self.top = top
        inv_ids = ",".join(
            str(self.cat_id_by_name[n])
            for n, (v, c) in self.exp.items()
            if self.top_of(n) == "Investitionen" and n in self.cat_id_by_name
        )
        ausgaben_ids = [
            str(self.cat_id_by_name[n]) if n in self.cat_id_by_name else ("none" if n == "Unkategorisiert" else None)
            for n, _, _ in self.fix_rows + self.neben_rows
        ]
        ausgaben_filter = ",".join(i for i in ausgaben_ids if i)
        for name, total, color, cat_name in top:
            special = None
            if name == "Ausgaben":
                special = ausgaben_filter or "ausgaben"
            elif name == "Investitionen":
                special = inv_ids or None
            nid = self.node(self.label(name, total, self.pct_of(total, self.total_inc)), color, 1, cat_name, special)
            self.link_from_income(nid, total, color, is_guthaben=(name == "Guthaben"))
            self.top_id[name] = nid
        # Shades per family: index 0 (strongest) for the collector node, the
        # children get the lighter ones after it in amount order.
        self.orange_shades = _family_shades(1 + len(self.fix_rows), HUE_ORANGE)
        self.red_shades = _family_shades(1 + len(self.neben_rows), HUE_RED)
        if self.fix_total > EPS:
            self.col2_branches.append(("Fixkosten", self.fix_total, self.orange_shades[0]))
        if self.neben_total > EPS:
            self.col2_branches.append(("Nebenkosten", self.neben_total, self.red_shades[0]))
        self.col2_branches.sort(key=lambda b: -b[1])

    # ---- columns 2 and 3 under Ausgaben
    def emit_ausgaben_children(self):
        ausgaben_id = self.top_id.get("Ausgaben")
        for name, total, color in self.col2_branches:
            n_id = self.node(self.label(name, total, self.pct_of(total, self.total_exp)), color, 2)
            self.link(ausgaben_id, n_id, total, color)
            self.group_node[name] = n_id
        # Column 3: ONE shared pass over Fixkosten AND Nebenkosten children,
        # globally sorted by amount (user request 2026-08-02); with
        # layoutIterations 0 the rendered order is exactly this one.
        orange_child = iter(self.orange_shades[1:])
        red_child = iter(self.red_shades[1:])
        for n, v, c in sorted(self.fix_rows + self.neben_rows, key=lambda x: -x[1]):
            is_fix = self.is_fixkosten(n)
            parent_id = self.group_node.get("Fixkosten" if is_fix else "Nebenkosten")
            if parent_id is None:
                continue
            color = next(orange_child) if is_fix else next(red_child)
            self.link(
                parent_id, self.node(self.label(n, v, self.pct_of(v, self.total_exp)), color, 3, cat_name=n), v, color
            )

    # ---- columns 2 and 3 under Guthaben
    def emit_guthaben_children(self):
        """Balance + categories with positive net - none of it is "spending".
        They end in column 3 at the same level as the expense categories; if
        the Ausgaben branch has an intermediate column, an unlabelled
        pass-through node in column 2 holds the place of the green branch
        (otherwise Fixkosten/Nebenkosten slipped above it, 2026-09-15)."""
        guthaben_id = self.top_id.get("Guthaben")
        if guthaben_id is None:
            return
        from_id = guthaben_id
        if self.col2_branches:
            sp = self.node("Guthaben ", _shade(HUE_GREEN, 0.30), 2)
            self.spacers.append(sp)
            self.link(guthaben_id, sp, self.guthaben_total, _shade(HUE_GREEN, 0.30))
            from_id = sp
        green_pool = [(n, v, n) for n, (v, c) in self.hidden_pos.items()]
        if self.saldo > EPS:
            green_pool.append(("Saldo", self.saldo, None))
        green_pool.sort(key=lambda x: -x[1])
        green_shades = _family_shades(len(green_pool), HUE_GREEN, light_lo=0.38, light_hi=0.60)
        for (name, v, cat_name), color in zip(green_pool, green_shades):
            sign = "+" if name != "Saldo" else ""
            nid = self.node(self.label(name, v, self.pct_of(v, self.total_inc), sign), color, 3, cat_name=cat_name)
            self.link(from_id, nid, v, color)

    def build(self):
        self.emit_income()
        self.emit_main_branches()
        # Create the children in the same order as the main branches (ECharts
        # draws in data order): if Guthaben is the biggest branch and sits on
        # top, its balance has to sit on top as well (2026-09-15).
        for name, _total, _color, _cat in self.top:
            if name == "Ausgaben":
                self.emit_ausgaben_children()
            elif name == "Guthaben":
                self.emit_guthaben_children()
        return {
            "labels": self.labels,
            "colors": self.colors,
            "node_col": self.node_col,
            "filter": self.node_filter,
            "source": self.src,
            "target": self.tgt,
            "value": self.val,
            "linkcolors": self.lcol,
            "spacers": self.spacers,
        }


def build_sankey_nodes(
    inc, exp, total_inc, total_exp, cat_id_by_name, hidden_pos=None, top_ancestor_name=None, fixkosten_cats=None
):
    """Nodes/links for the money-flow Sankey: income -> {Ausgaben,
    Investitionen, Guthaben} -> {Fixkosten, Nebenkosten | Saldo, positive
    categories} -> individual categories (every category an equally deep
    leaf, no parent collector node). See _SankeyBuilder."""
    return _SankeyBuilder(
        inc, exp, total_inc, total_exp, cat_id_by_name, hidden_pos, top_ancestor_name, fixkosten_cats
    ).build()


# Year matrix: month x category. Flat fixed costs follow their economic month
# here too, so that a cell and its drilldown show the same bookings.
def year_matrix(con, year):
    """Month x category for one year - the table "Jahresuebersicht".

    Lives on /jahr instead of the dashboard since 2026-09-11 (user request:
    "die jahresuebersicht in dashboard gehoert in die jahresuebersicht unter
    dem anderen reiter verschoben" - move the year overview out of the
    dashboard into its own tab) - the dashboard now shows a freely selectable
    period, a table hard-wired to one year no longer fitted there.
    """
    fixed_months = wohnung_fixed_display_months(con)
    ytx = year_transactions(con, year, fixed_months)
    matrix, ycats = {}, {}
    for t in ytx:
        v = eff(t)
        # Refunds (v>0) REDUCE the category, exactly as in summarize(). Before,
        # they were skipped here - so the matrix showed gross expenses while
        # the table above and the drilldown netted (real bug, reported
        # 2026-09-11: "mir zeigt es zB im Sport unter 05 Monat an, dass ich
        # 1000 EUR ausgegeben haette. tatsaechlich habe ich aber jede menge
        # ueberweisungen bekommen und dort hingebucht" - Sport in month 05
        # showed 1000 EUR spent although many transfers were received and
        # booked there). Concretely: Sport 05/2026 stood at 1085,89 instead of
        # 405,94; summed over 2026 3444,61 EUR too much in 17 cells.
        if t["cat_kind"] in ("transfer", "income") or abs(v) < EPS:
            continue
        tx_id = t["id"] if "id" in t.keys() else None
        m = fixed_months.get(tx_id) or t["booking_date"][:7]
        if m[:4] != year:
            # A December booking can move into January of the following year
            # through the smoothing - then it does not belong in this year matrix.
            continue
        n = t["cat_name"] or "Unkategorisiert"
        ycats[n] = t["cat_color"] or "#C4C4C4"
        matrix.setdefault(m, {})[n] = matrix.setdefault(m, {}).get(n, 0) - v
    # Do not show cells that balance out exactly at all (otherwise a "0" would
    # stand there that looks like "nothing spent" instead of "balanced").
    for mm in matrix:
        for n in [n for n, v in matrix[mm].items() if is_zero(v)]:
            del matrix[mm][n]
    ycats = {n: c for n, c in ycats.items() if any(n in matrix[mm] for mm in matrix)}
    ymonths = sorted(matrix.keys())
    ycat_order = sorted(ycats, key=lambda n: -sum(matrix[m].get(n, 0) for m in ymonths))
    return matrix, ycats, ymonths, ycat_order


def year_matrix_groups(con, year):
    """The year matrix grouped by top-level category - for display.

    User request 2026-09-11: "in der aufschluesselung unter jahr sollten die
    unterkategorien auch schoen auf und zu klappbar sein, standardmaessig
    zugeklappt wie in der liste darueber" (subcategories in the year
    breakdown should be collapsible, collapsed by default like the list
    above). The parent row carries per month the sum of its children (plus
    own bookings that sit directly on it - legacy from before subcategories,
    as child "ohne Unterkategorie").
    """
    matrix, ycats, ymonths, _ = year_matrix(con, year)
    top = top_category_names(con)
    cat_id = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM categories")}
    groups = {}
    for n, color in ycats.items():
        parent = top.get(n, n)
        g = groups.setdefault(
            parent,
            {"name": parent, "color": None, "cat_id": cat_id.get(parent), "cells": {}, "total": 0.0, "children": []},
        )
        cells = {m: matrix[m][n] for m in ymonths if n in matrix[m]}
        child = {
            "name": n if n != parent else "ohne Unterkategorie",
            "color": color,
            "cat_id": cat_id.get(n),
            "cells": cells,
            "total": sum(cells.values()),
        }
        for m, v in cells.items():
            g["cells"][m] = g["cells"].get(m, 0) + v
        g["total"] += child["total"]
        if n == parent:
            g["color"] = color
            g["own"] = child  # own bookings of the parent category
        else:
            g["children"].append(child)
    out = []
    for g in groups.values():
        if g["children"]:
            # Only a "real" collector node if there are children - the parent
            # category's own bookings are then listed as a child as well.
            if "own" in g:
                g["children"].append(g.pop("own"))
            g["children"].sort(key=lambda c: -c["total"])
            g["color"] = g["color"] or g["children"][0]["color"]
            # The parent row links to ALL categories of the group - not to
            # "none" (that was the uncategorized bookings, fuzz test
            # 2026-09-11).
            g["cat_ids"] = ",".join(str(c["cat_id"]) for c in g["children"] if c["cat_id"])
        else:
            g.pop("own", None)
            g["cat_ids"] = str(g["cat_id"]) if g["cat_id"] else ("none" if g["name"] == "Unkategorisiert" else "")
        out.append(g)
    out.sort(key=lambda g: -g["total"])
    return out, ymonths


def compare_periods(cur_exp, prev_exp, top_ancestor_name, cat_id_by_name, cur_hidden=None, prev_hidden=None):
    """Expenses per top-level category: now, before, difference - sorted by
    the biggest change. Categories that occur in only one of the two periods
    stand with 0 on the other side. `qs` links exactly the categories
    (parents + children) the row consists of."""
    sums, members = {}, {}
    # Net positive categories (more back than spent) enter as a negative
    # expense - otherwise 0,00 stood there and the drilldown showed +90.
    sides = (("now", cur_exp, cur_hidden or {}), ("before", prev_exp, prev_hidden or {}))
    for side, exp, hidden in sides:
        for n, (v, _c) in list(exp.items()) + [(n, (-v, c)) for n, (v, c) in hidden.items()]:
            top = top_ancestor_name.get(n, n)
            sums.setdefault(top, {"now": 0.0, "before": 0.0})[side] += v
            members.setdefault(top, set()).update({n, top})
    rows = []
    for name, d in sums.items():
        now, before = d["now"], d["before"]
        rows.append(
            {
                "name": name,
                "now": now,
                "before": before,
                "delta": now - before,
                "pct": (100.0 * (now - before) / before) if before > EPS else None,
                "qs": category_qs(members[name], cat_id_by_name),
            }
        )
    rows.sort(key=lambda r: -abs(r["delta"]))
    return rows


def _period_totals(con, sel_months, wohnung_map, top):
    """Income/expenses (without investments)/invested/balance of a period
    plus the expenses per category - basis for /vergleich."""
    txs = dashboard_period_transactions(con, sel_months, wohnung_map)
    inc, exp = summarize(txs)
    hid = hidden_positive_categories(txs)
    t_inc = sum(v for v, _ in inc.values())
    t_exp = sum(v for v, _ in exp.values())
    inv = sum(v for n, (v, _) in exp.items() if top.get(n, n) == "Investitionen")
    exp_only = {n: v for n, v in exp.items() if top.get(n, n) != "Investitionen"}
    return {
        "inc": q(t_inc),
        "exp": q(t_exp - inv),
        "invest": q(inv),
        "saldo": q(t_inc - t_exp + sum(v for v, _ in hid.values())),
        "exp_only": exp_only,
        "hidden": hid,
        "months": sel_months,
        "label": period_label(sel_months),
        "qs": period_args(sel_months),
    }
