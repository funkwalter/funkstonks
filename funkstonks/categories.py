"""Categories: choice groups, colors, top-level category, drilldown query strings."""

import colorsys
import math


def category_context(con):
    """(cats, cat_groups, cat_by_id) - the trio every template with a category
    dropdown needs. Used to be spelled out six times."""
    cats = con.execute("SELECT * FROM categories ORDER BY kind, name").fetchall()
    return cats, category_choice_groups(con), {c["id"]: c for c in cats}


def category_choice_groups(con, kind=None, keep_id=None):
    """For every category dropdown in the program: a category with subcategories
    is itself NO LONGER selectable, only its subcategories are (user request
    2026-08-07: once subcategories exist, choosing one of them is mandatory).
    Returns a list of {"label": None|parent name, "options": [category, ...]}
    - "label": None means a standalone category without children (a single
    <option> as before), otherwise it becomes an <optgroup> with the children
    as options. Existing payments booked to the parent category BEFORE this
    feature stay untouched (this only affects the choice for NEW
    categorizations) - `keep_id` makes sure a currently assigned category_id
    that has since become a parent still shows up as an option (otherwise the
    <select> wrongly shows the first option as "selected" on load although the
    DB holds something else)."""
    q = "SELECT * FROM categories"
    args = ()
    if kind:
        q += " WHERE kind=?"
        args = (kind,)
    q += " ORDER BY kind, name"
    cats = con.execute(q, args).fetchall()
    children = {}
    for c in cats:
        if c["parent_id"]:
            children.setdefault(c["parent_id"], []).append(c)
    groups = []
    for c in cats:
        if c["parent_id"]:
            continue
        kids = children.get(c["id"])
        if kids:
            groups.append({"label": c["name"], "options": kids})
            if keep_id and c["id"] == keep_id:
                groups.append({"label": None, "options": [c]})
        else:
            groups.append({"label": None, "options": [c]})
    return groups


def group_expenses_by_parent(exp, top_ancestor_name):
    """Groups expense rows by top-level category (subcategories are collected
    under their parent instead of appearing as independent rows of equal rank
    - user request 2026-08-07: "Subkategorien sollten dann auch dementsprechend
    unter restaurant/ausgehen angezeigt werden im dashboard" - subcategories
    should be shown under restaurant/going out on the dashboard). Returns a
    list sorted by total amount descending of
    {"name": group name, "amount": sum, "color": color, "children": [...]}
    - "children" is empty if this group has no actually used subcategories
    (yet) (stays a simple leaf as before); otherwise a list sorted by amount of
    {"name", "amount", "color"} per contributing raw category (including the
    parent itself if bookings still sit directly on it, e.g. from before the
    subdivision)."""
    groups = {}
    for n, (v, c) in exp.items():
        gname = top_ancestor_name.get(n, n)
        g = groups.setdefault(gname, {"amount": 0.0, "color": c, "children": []})
        g["amount"] += v
        g["children"].append({"name": n, "amount": v, "color": c})
        if n == gname:
            g["color"] = c
    out = []
    for gname, g in groups.items():
        children = g["children"]
        needs_breakdown = any(c["name"] != gname for c in children)
        children.sort(key=lambda c: -c["amount"])
        out.append(
            {"name": gname, "amount": g["amount"], "color": g["color"], "children": children if needs_breakdown else []}
        )
    out.sort(key=lambda g: -g["amount"])
    return out


def assign_category_colors(con):
    """Reassign colors systematically (user request 2026-09-01: "fuer jede
    kategorie eine grundlegend andere farbe ... alle restaurants eine hauptfarbe
    und alle kategorien darunter eine aehnliche, aber nicht gleiche" - a
    fundamentally different color per category, one main color for all
    restaurants and a similar but not identical one for everything below).

    Every TOP-LEVEL category gets its own hue, spread evenly around the color
    wheel - so two main categories can never be confused. Subcategories inherit
    their parent's hue and are only shifted slightly (hue +/-, lightness
    stepped): recognizable as a family, yet clearly distinguishable. Sorting is
    by name so that the same category gets the same color on a re-run (stable,
    not random).

    Affects ONLY the color stored in `categories` (chips on /transactions,
    /categories, /jahr, /fixkosten, table below the Sankey). The Sankey itself
    keeps coloring by function (red/orange/green/blue, see build_sankey_nodes)
    - that is intentional and stays untouched.
    """
    rows = con.execute("SELECT id, name, parent_id, kind FROM categories ORDER BY name").fetchall()
    # Transfer categories (PayPal/credit card/Splitwise settlement, Umbuchung)
    # get neutral greys instead of a hue of their own: they mean "does not
    # count", rarely show up - and would otherwise take space on the color
    # wheel away from the real categories (with 26 hues the minimum distance
    # would drop from 16 to 13 degrees).
    tops = [r for r in rows if r["parent_id"] is None and r["kind"] != "transfer"]
    greys = [r for r in rows if r["parent_id"] is None and r["kind"] == "transfer"]
    kids = {}
    for r in rows:
        if r["parent_id"] is not None:
            kids.setdefault(r["parent_id"], []).append(r)
    n = max(len(tops), 1)
    # Spread hues exactly evenly around the wheel - that gives the largest
    # guaranteed minimum distance (360/n). The golden-ratio jump often seen for
    # this was worse here: with 18 categories two hues were only 8 degrees
    # apart. The order is additionally shuffled with a stride coprime to n so
    # that alphabetically adjacent categories are not adjacent in color too.
    stride = max(1, int(n * 0.382))
    while stride > 1 and math.gcd(stride, n) != 1:
        stride -= 1
    updates = []
    for i, t in enumerate(tops):
        hue = ((i * stride) % n) * (360.0 / n)
        updates.append((_shade(hue, 0.40, 0.55), t["id"]))
        children = sorted(kids.get(t["id"], []), key=lambda r: r["name"])
        for k, ch in enumerate(children):
            # Swing the hue only slightly and ALTERNATELY around the parent hue
            # (+7, -7, +13, -13, ...), at most ~20 degrees. A one-directional
            # offset added up to 80 degrees with five children - "Sport" turned
            # blue although "Lifestyle" is green. The real distinction comes
            # from lightness and saturation.
            step = 7 + 6 * (k // 2)
            ch_hue = (hue + (step if k % 2 == 0 else -step)) % 360
            light = min(0.68, 0.50 + 0.045 * k)
            sat = max(0.32, 0.48 - 0.03 * k)
            updates.append((_shade(ch_hue, light, sat), ch["id"]))
    for j, g in enumerate(greys):
        updates.append((_shade(210, 0.55 + 0.05 * j, 0.05), g["id"]))
    con.executemany("UPDATE categories SET color=? WHERE id=?", updates)
    return len(updates)


def _shade(hue, light, sat=0.52):
    """A hex color from HSL (hue in degrees, light/sat 0..1)."""
    r, g, b = colorsys.hls_to_rgb(hue / 360, light, sat)
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def _family_shades(n, hue, light_lo=0.30, light_hi=0.66, sat=0.52):
    """n distinguishable shades of ONE color family (fixed hue, varying
    lightness) - dark/strong first. Used for the functional Sankey coloring
    (see build_sankey_nodes): the returned order is meant to be assigned by
    size descending (largest item = darkest/strongest shade), not by
    insertion order."""
    if n <= 0:
        return []
    if n == 1:
        return [_shade(hue, (light_lo + light_hi) / 2, sat)]
    return [_shade(hue, light_lo + (light_hi - light_lo) * i / (n - 1), sat) for i in range(n)]


def expense_rows(exp, top_ancestor_name, cat_id_by_name, total, n_months=1):
    """Expense table: top-level categories with indented subcategories, amount,
    share, monthly average and click target. Dashboard and /jahr build the
    same rows - one place for it."""

    def cid(name, leaf):
        if not leaf:
            return None
        return cat_id_by_name.get(name, "none" if name == "Unkategorisiert" else None)

    rows = []
    for g in group_expenses_by_parent(exp, top_ancestor_name):
        children = [
            {
                "name": c["name"],
                "amount": c["amount"],
                "color": c["color"],
                "avg": c["amount"] / n_months,
                "cat_id": cat_id_by_name.get(c["name"]),
                "pct": (100.0 * c["amount"] / total) if total else 0,
            }
            for c in g["children"]
        ]
        rows.append(
            {
                "name": g["name"],
                "amount": g["amount"],
                "color": g["color"],
                "avg": g["amount"] / n_months,
                "cat_id": cid(g["name"], not g["children"]),
                # parent row: all children at once (as in the yearly matrix)
                "cat_ids": ",".join(str(c["cat_id"]) for c in children if c["cat_id"]),
                "pct": (100.0 * g["amount"] / total) if total else 0,
                "children": children,
            }
        )
    return rows


def category_qs(names, cat_id_by_name):
    """`cat=` parameter for /transactions from category names: the ids, plus
    `none` if "Unkategorisiert" (uncategorized) is among them. Tiles and
    aggregate nodes thereby link EXACTLY the categories their number is made of."""
    teile = [str(cat_id_by_name[n]) for n in names if n in cat_id_by_name]
    if "Unkategorisiert" in names:
        teile.append("none")
    return ",".join(teile)


def top_category_names(con):
    """Category name -> name of its top-level category (itself if none).

    Four places needed the same loop (dashboard, yearly view, recurring costs,
    series detection) - one of them had forgotten the resolution, which only
    became a bug with the subcategories.
    """
    rows = con.execute("SELECT id, name, parent_id FROM categories").fetchall()
    name_by_id = {r["id"]: r["name"] for r in rows}
    parent_by_id = {r["id"]: r["parent_id"] for r in rows}
    out = {}
    for r in rows:
        cid = r["id"]
        while parent_by_id.get(cid):
            cid = parent_by_id[cid]
        out[r["name"]] = name_by_id[cid]
    return out
