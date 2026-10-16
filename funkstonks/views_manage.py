"""Routes: categories, rules, settings, setup, help, backup, demo data."""

import json
import os

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for

from .backup import BACKUP_NAME_RE, _backups_dir, backup_db, list_backups, restore_backup, restore_from_file
from .categories import assign_category_colors, category_choice_groups, category_context
from .config import ALL_SOURCES, SETTING_DEFAULTS, SPLITWISE_CAT_MAP, SYSTEM_CATS, THEME_KEYS, BadInput
from .db import (
    db,
    disabled_automations,
    enabled_sources,
    fixed_cost_rows,
    get_setting,
    get_setting_list,
    invalidate_fixed_cost_cache,
    parse_cutoff,
    set_setting,
)
from .demo import load_demo_data, remove_demo_data
from .repay import auto_link_repayments
from .rules import (
    URLAUB_PROTECTED_SQL,
    apply_rules,
    income_rule_patterns,
    normalize_pattern,
    rule_target_txs,
    set_income_rules,
    upsert_rule,
)
from .web import app, form_category_id, form_int


@app.route("/categories", methods=["GET", "POST"])
def categories_page():
    """Categories, since 2026-08-07 with exactly one level of subcategories
    (user request: "unter kategorien auch noch die moeglichkeit subkategorien
    zu erstellen ... zB nach lifestyle noch eine ebene nach hinten", i.e. one
    more level below a category such as lifestyle). A subcategory cannot have
    children of its own - keeps the model simple and covers exactly the
    described use case. As soon as a category has subcategories, it is no
    longer selectable itself when categorising (see category_choice_groups) -
    only its children are."""
    con = db()
    if request.method == "POST":
        if request.form.get("recolor"):
            # Redistribute colors systematically (user request 2026-09-01).
            # Overwrites manually set colors - hence a separate button with a
            # confirmation and not automatically on every start.
            n = assign_category_colors(con)
            con.commit()
            flash(f"{n} Kategorien neu eingefärbt")
            con.close()
            return redirect(url_for("categories_page"))
        if request.form.get("add_name"):
            name = request.form["add_name"].strip()
            if not name:
                raise BadInput("Der Name darf nicht leer sein.")
            parent_id = form_int("add_parent_id", default=0) or None
            kind = request.form.get("add_kind") or "expense"
            if kind not in ("expense", "income", "transfer"):
                raise BadInput("Unbekannter Kategorietyp.")
            if parent_id:
                parent = con.execute("SELECT kind, parent_id FROM categories WHERE id=?", (parent_id,)).fetchone()
                if not parent:
                    raise BadInput("Die Oberkategorie gibt es nicht (mehr).")
                if parent["parent_id"]:
                    # exactly ONE level - a subcategory gets no children
                    raise BadInput("Unterkategorien können keine eigenen Unterkategorien haben.")
                kind = parent["kind"]
            if con.execute("SELECT 1 FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                raise BadInput(f"„{name}“ existiert bereits (Groß-/Kleinschreibung zählt nicht).")
            con.execute(
                "INSERT INTO categories(name,color,kind,parent_id) VALUES(?,?,?,?)",
                (name, request.form.get("add_color") or "#888888", kind, parent_id),
            )
            flash("Kategorie angelegt")
        elif request.form.get("save"):
            cid = form_int("save")
            row = con.execute("SELECT parent_id, kind FROM categories WHERE id=?", (cid,)).fetchone()
            if not row:
                raise BadInput("Die Kategorie gibt es nicht (mehr).")
            name = (request.form.get(f"name_{cid}") or "").strip()
            if not name:
                raise BadInput("Der Name darf nicht leer sein.")
            dup = con.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE AND id!=?", (name, cid)).fetchone()
            if dup:
                raise BadInput(f"„{name}“ existiert bereits.")
            color = request.form.get(f"color_{cid}") or "#888888"
            if row["parent_id"]:
                con.execute("UPDATE categories SET name=?, color=? WHERE id=?", (name, color, cid))
            else:
                kind = request.form.get(f"kind_{cid}") or row["kind"]
                if kind not in ("expense", "income", "transfer"):
                    raise BadInput("Unbekannter Kategorietyp.")
                con.execute("UPDATE categories SET name=?, color=?, kind=? WHERE id=?", (name, color, kind, cid))
                # Subcategories ALWAYS inherit the kind - also on a change
                con.execute("UPDATE categories SET kind=? WHERE parent_id=?", (kind, cid))
        elif request.form.get("delete"):
            cid = form_int("delete")
            row = con.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
            if row is None:
                flash("FEHLER Diese Kategorie gibt es nicht (mehr)")
                con.commit()
                con.close()
                return redirect(url_for("categories_page"))
            if row["name"] in SYSTEM_CATS:
                flash(
                    f"FEHLER Kategorie {row['name']} wird vom System benötigt "
                    "(PayPal-/Karten-Verrechnung) und kann nicht gelöscht werden"
                )
                con.commit()
                con.close()
                return redirect(url_for("categories_page"))
            has_children = con.execute("SELECT 1 FROM categories WHERE parent_id=?", (cid,)).fetchone()
            if has_children:
                flash("FEHLER Nicht löschbar – hat noch Unterkategorien (die erst löschen oder umhängen)")
                con.commit()
                con.close()
                return redirect(url_for("categories_page"))
            # Freely deletable (user request 2026-09-15: "danach sollen sie frei
            # loeschbar sein" - they should be freely deletable; before, the
            # three default rules on "Versicherung" already blocked deletion,
            # and the category seemed to "keep coming back"). Whatever hangs
            # on it is detached cleanly: rules gone, transactions back to the
            # review queue, splits with this category undone (transaction open
            # again), fixed costs keep their pattern without a category.
            n_tx = con.execute("SELECT COUNT(*) FROM transactions WHERE category_id=?", (cid,)).fetchone()[0]
            n_rules = con.execute("SELECT COUNT(*) FROM rules WHERE category_id=?", (cid,)).fetchone()[0]
            split_parents = [r[0] for r in con.execute("SELECT DISTINCT tx_id FROM splits WHERE category_id=?", (cid,))]
            con.execute("DELETE FROM rules WHERE category_id=?", (cid,))
            con.execute("UPDATE transactions SET category_id=NULL WHERE category_id=?", (cid,))
            for tx_id in split_parents:
                con.execute("DELETE FROM splits WHERE tx_id=?", (tx_id,))
                con.execute(
                    "UPDATE transactions SET category_id=NULL, note=REPLACE(note, '[aufgeteilt]', '') WHERE id=?",
                    (tx_id,),
                )
            con.execute("UPDATE fixed_costs SET category_id=NULL WHERE category_id=?", (cid,))
            invalidate_fixed_cost_cache()
            con.execute("DELETE FROM categories WHERE id=?", (cid,))
            parts = []
            if n_rules:
                parts.append(f"{n_rules} Regel{'n' if n_rules != 1 else ''} mitgelöscht")
            if n_tx:
                parts.append(f"{n_tx} Zahlung{'en' if n_tx != 1 else ''} wieder unkategorisiert")
            if split_parents:
                parts.append(f"{len(split_parents)} Aufteilung{'en' if len(split_parents) != 1 else ''} aufgehoben")
            flash(f"Kategorie {row['name']} gelöscht" + (" – " + ", ".join(parts) if parts else ""))
        con.commit()
        con.close()
        return redirect(url_for("categories_page"))
    cats = con.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM transactions t WHERE t.category_id=c.id) "
        "+ (SELECT COUNT(*) FROM splits s WHERE s.category_id=c.id) AS used, "
        "(SELECT COUNT(*) FROM rules r WHERE r.category_id=c.id) AS n_rules "
        "FROM categories c ORDER BY c.kind, c.name"
    ).fetchall()
    top = [c for c in cats if not c["parent_id"]]
    children = {}
    for c in cats:
        if c["parent_id"]:
            children.setdefault(c["parent_id"], []).append(c)
    con.close()
    return render_template("categories.html", top=top, children=children, system_cats=SYSTEM_CATS)


def _rules_post(con):
    """The POST actions of the rules page. Returns a response (AJAX 204) or
    None for the normal redirect; the caller commits."""
    f = request.form
    if f.get("delete"):
        con.execute("DELETE FROM rules WHERE id=?", (f["delete"],))
        con.commit()
        if f.get("ajax"):
            # No redirect => no scroll reset on /rules; JS removes the row.
            return ("", 204)
    elif f.get("edit"):
        # Change an existing rule (pattern and/or category); priority stays
        # untouched (no UI field).
        pattern = normalize_pattern(f.get("pattern", ""))
        if pattern:
            cid = form_category_id(con)
            con.execute("UPDATE rules SET pattern=?, category_id=? WHERE id=?", (pattern, cid, form_int("edit")))
        con.commit()
        if f.get("ajax"):
            return ("", 204)
    elif f.get("pattern"):
        # Several patterns at once (comma-separated) = several rules of the same
        # category; apply immediately and say what happened.
        patterns = [normalize_pattern(p) for p in f["pattern"].split(",")]
        cid = form_category_id(con)
        made = [p for p in patterns if p]
        for p in made:
            upsert_rule(con, p, cid)
        if made:
            hit = apply_rules(con)
            flash(
                f"Regel{'n' if len(made) > 1 else ''} angelegt: {', '.join(made)} – "
                f"{hit} offene Zahlung{'en' if hit != 1 else ''} gleich zugeordnet"
            )
    elif f.get("repay_add"):
        ip = (f.get("in_pattern") or "").strip()
        op = (f.get("out_pattern") or "").strip()
        if not ip or not op:
            raise BadInput("Eingang und Ausgabe dürfen nicht leer sein.")
        win = form_int("window_days", default=45, lo=1, hi=400)
        con.execute("INSERT INTO repay_rules(in_pattern,out_pattern,window_days) VALUES(?,?,?)", (ip, op, win))
        n = auto_link_repayments(con)
        flash(f"Rückzahlungs-Regel angelegt: {ip} → {op} – {n} Zahlung{'en' if n != 1 else ''} gleich verrechnet")
    elif f.get("repay_delete"):
        con.execute("DELETE FROM repay_rules WHERE id=?", (form_int("repay_delete"),))
        flash("Rückzahlungs-Regel gelöscht – bestehende Verknüpfungen bleiben")
    elif f.get("repay_apply"):
        n = auto_link_repayments(con)
        flash(f"{n} Rückzahlung{'en' if n != 1 else ''} verrechnet")
    elif f.get("auto_delete") or f.get("auto_restore"):
        # Switch built-in automation off / back on
        # (settings.disabled_automations; everything is on at first start).
        key = (f.get("auto_delete") or f.get("auto_restore") or "").strip()
        dis = disabled_automations(con)
        if f.get("auto_delete"):
            dis.add(key)
            flash("Automatik abgeschaltet – schon kategorisierte Zahlungen bleiben, wie sie sind")
        else:
            dis.discard(key)
            flash("Automatik wieder eingeschaltet")
        set_setting(con, "disabled_automations", json.dumps(sorted(dis), ensure_ascii=False))
    if f.get("apply"):
        n = apply_rules(con)
        flash(f"{n} Transaktionen kategorisiert")
    return None


def _rule_hits(con, rules):
    """Hit count per rule against the same set that rule_target_txs() would
    touch retroactively (active, no split parent, no holiday group). `off` =
    of those, currently in a DIFFERENT category than the rule prescribes -
    the cases where applying retroactively pays off."""
    eligible = con.execute(
        "SELECT id, category_id, "
        "lower(COALESCE(partner,'') || ' ' || COALESCE(description,'')) AS hay "
        "FROM transactions WHERE status='active' "
        "AND id NOT IN (SELECT tx_id FROM splits) "
        "AND " + URLAUB_PROTECTED_SQL
    ).fetchall()
    hits = {}
    for r in rules:
        pat = (r["pattern"] or "").lower()
        n = off = 0
        if pat:
            for t in eligible:
                if pat in t["hay"]:
                    n += 1
                    if t["category_id"] != r["category_id"]:
                        off += 1
        hits[r["id"]] = {"n": n, "off": off}
    return hits


def _group_rules_by_top_category(rules):
    """One card per TOP-LEVEL category, subcategories nested inside:
    `own` = rules on the parent category itself, `subs` = per subcategory."""
    grouped = {}
    for r in rules:
        top = r["parent_name"] or r["cat_name"]
        g = grouped.setdefault(top, {"color": None, "own": [], "subs": {}, "n": 0})
        g["color"] = r["parent_color"] if r["parent_name"] else r["cat_color"]
        g["n"] += 1
        if r["parent_name"]:
            s = g["subs"].setdefault(r["cat_name"], {"color": r["cat_color"], "rules": []})
            s["rules"].append(r)
        else:
            g["own"].append(r)
    return grouped


def _system_rules(con):
    """Built-in automation (not in the rules table), so that it is not
    invisible on the page: foreign POS -> Urlaub and the Splitwise keyword
    list, each row switchable off (settings.disabled_automations)."""
    urlaub = con.execute("SELECT color FROM categories WHERE name='Urlaub'").fetchone()
    disabled = disabled_automations(con)
    out = []
    if urlaub:
        out.append(
            {
                "key": "foreign_pos",
                "disabled": "foreign_pos" in disabled,
                "desc": "Zahlung mit der Karte an einem Terminal im Ausland",
                "cat_name": "Urlaub",
                "color": urlaub["color"],
                "note": "Erkannt am Text „POS … <Land> K1“ mit einem Länder- oder Währungscode außer AT/EUR (z.B. IT, DKK). "
                "Greift nur, wenn die Zahlung noch keine Kategorie hat; Online-Käufe (E-COMM) nicht, dort steht nur der Firmensitz.",
            }
        )
    if "splitwise" in enabled_sources(con):
        by_cat = {}
        for kw, cat_name in SPLITWISE_CAT_MAP:
            by_cat.setdefault(cat_name, []).append(kw)
        colors = {r["name"]: r["color"] for r in con.execute("SELECT name, color FROM categories")}
        for cat_name, kws in by_cat.items():
            out.append(
                {
                    "key": "sw:" + cat_name,
                    "disabled": ("sw:" + cat_name) in disabled,
                    "desc": "Splitwise-Anteil, dessen Beschreibung eines dieser Wörter enthält: " + ", ".join(kws),
                    "cat_name": cat_name,
                    "color": colors.get(cat_name, "#C4C4C4"),
                    "note": "Nur für Anteile, auf die keine deiner eigenen Regeln passt – deine Regeln gehen immer vor."
                    + (
                        ""
                        if cat_name in colors
                        else " Die Kategorie gibt es nicht (mehr), die Zeile greift deshalb nicht."
                    ),
                }
            )
    return out


def _repay_rules_view(con):
    """Repayment rules with hit count, the matched senders and the most recent
    concrete pair "incoming <-> debit" - the rule explains itself by example
    (user decision 2026-10-14)."""
    repaid = con.execute(
        "SELECT lower(COALESCE(t.partner,'') || ' ' || COALESCE(t.description,'')) AS hay "
        "FROM transactions t WHERE t.id IN (SELECT other_tx FROM matches WHERE kind='repayment')"
    ).fetchall()
    pairs = con.execute(
        "SELECT m.amount AS delta, i.booking_date AS in_date, i.partner AS in_partner, i.amount AS in_amount, "
        "lower(COALESCE(i.partner,'') || ' ' || COALESCE(i.description,'')) AS in_hay, "
        "o.booking_date AS out_date, o.partner AS out_partner, o.amount AS out_amount, "
        "COALESCE(o.effective_amount, o.amount) AS out_eff, "
        "lower(COALESCE(o.partner,'') || ' ' || COALESCE(o.description,'')) AS out_hay "
        "FROM matches m JOIN transactions i ON i.id=m.other_tx JOIN transactions o ON o.id=m.bank_tx "
        "WHERE m.kind='repayment' ORDER BY i.booking_date DESC"
    ).fetchall()
    out = []
    for r in con.execute("SELECT * FROM repay_rules ORDER BY id"):
        ip = (r["in_pattern"] or "").lower()
        op = (r["out_pattern"] or "").lower()
        mine = [x for x in pairs if ip and ip in x["in_hay"] and op and op in x["out_hay"]]
        out.append(
            {
                "id": r["id"],
                "in_pattern": r["in_pattern"],
                "out_pattern": r["out_pattern"],
                "window_days": r["window_days"],
                "n": sum(1 for t in repaid if ip and ip in t["hay"]),
                "example": mine[0] if mine else None,
                "senders": sorted({x["in_partner"] for x in mine if x["in_partner"]}),
            }
        )
    return out


@app.route("/rules", methods=["GET", "POST"])
def rules_page():
    con = db()
    if request.method == "POST":
        resp = _rules_post(con)
        if resp is not None:
            return resp
        con.commit()
        return redirect(url_for("rules_page"))
    # Carry the parent name along so a subcategory is recognisable as
    # "Restaurant/Ausgehen -> Mittagessen Arbeit"; sorted by the top-level name.
    rules = con.execute(
        "SELECT r.*, c.name AS cat_name, c.color AS cat_color, "
        "p.name AS parent_name, p.color AS parent_color "
        "FROM rules r JOIN categories c ON c.id=r.category_id "
        "LEFT JOIN categories p ON p.id=c.parent_id "
        "ORDER BY c.kind, COALESCE(p.name, c.name), c.parent_id IS NOT NULL, c.name, "
        "r.priority, r.id"
    ).fetchall()
    hits = _rule_hits(con, rules)
    grouped = _group_rules_by_top_category(rules)
    cats, cat_groups, cat_by_id = category_context(con)
    system_rules = _system_rules(con)
    repay_rules = _repay_rules_view(con)
    con.close()
    return render_template(
        "rules.html",
        grouped=grouped,
        cats=cats,
        cat_groups=cat_groups,
        cat_by_id=cat_by_id,
        system_rules=system_rules,
        hits=hits,
        repay_rules=repay_rules,
    )


@app.route("/rules/<int:rule_id>/apply", methods=["POST"])
def rule_apply(rule_id):
    """Apply a rule RETROACTIVELY to already booked transactions.

    User request 2026-09-01: "es erkennt alle buchungen mit dem automatisch und
    dann werden alle buchungen dorthingeschoben wo ich will" - it should match
    all transactions automatically and move them where he wants. apply_rules()
    itself deliberately touches only uncategorised rows - this is the
    explicit, user-confirmed special case that overwrites existing categories.

    With dry=1 only the count is returned and nothing is changed; the frontend
    uses it to ask beforehand how many transactions would be affected. Only
    the second call writes (retroactive data corrections used to create a
    backup first; since 2026-09-15 they no longer do, see below).
    """
    con = db()
    r = con.execute(
        "SELECT r.*, c.name AS cat_name FROM rules r LEFT JOIN categories c ON c.id=r.category_id WHERE r.id=?",
        (rule_id,),
    ).fetchone()
    if not r:
        con.close()
        return jsonify({"error": "Regel nicht gefunden"}), 404
    targets = rule_target_txs(con, r["pattern"])
    changed = [t for t in targets if t["category_id"] != r["category_id"]]
    if request.form.get("dry"):
        con.close()
        return jsonify(
            {"matched": len(targets), "changed": len(changed), "pattern": r["pattern"], "category": r["cat_name"]}
        )
    if changed:
        # No automatic backup any more (user request 2026-09-15) - the preview
        # shows beforehand what happens; backups can be made under /sicherung.
        con.executemany(
            "UPDATE transactions SET category_id=? WHERE id=?", [(r["category_id"], t["id"]) for t in changed]
        )
        con.commit()
    con.close()
    return jsonify({"changed": len(changed), "category": r["cat_name"]})


@app.route("/demo", methods=["POST"])
def demo_page():
    con = db()
    if request.form.get("remove"):
        n = remove_demo_data(con)
        con.commit()
        con.close()
        flash(f"Beispieldaten entfernt ({n} Buchungen) – jetzt die eigenen Exporte importieren")
        return redirect(url_for("import_page"))
    n = load_demo_data(con)
    con.commit()
    con.close()
    flash(f"{n} Beispielbuchungen geladen – zum Ausprobieren. Entfernen jederzeit über den Hinweis oben")
    return redirect(url_for("dashboard"))


@app.route("/setup", methods=["GET", "POST"])
def setup_page():
    """Setup at first start - asks exactly what the app needs and explains
    what for. User request 2026-09-11: "zuersteinmal muesste ausgewaehlt
    werden koennen was statisch sein soll ... ist splitwise benoetigt?
    kreditkarte? paypal? das sollte alles optional sein" - the sources
    (Splitwise, credit card, PayPal) should all be optional."""
    con = db()
    if request.method == "POST":
        srcs = [s for s, _, _ in ALL_SOURCES if request.form.get("src_" + s)]
        if "bank" not in srcs:
            srcs.append("bank")
        set_setting(con, "sources", json.dumps(sorted(srcs)))
        set_setting(con, "data_cutoff", parse_cutoff(request.form.get("data_cutoff")))
        set_setting(con, "shared_name", (request.form.get("shared_name") or "").strip())
        set_setting(con, "splitwise_name", (request.form.get("splitwise_name") or "").strip())
        set_setting(con, "fixed_cost_group", (request.form.get("fixed_cost_group") or "").strip())
        _t = request.form.get("theme") or "auto"
        set_setting(con, "theme", _t if _t in THEME_KEYS else "auto")
        # Everyone's employer has a different name - hence it is asked here
        # instead of shipping as a default rule. It is stored as an ordinary
        # rule, editable under /rules like any other.
        inc_cat = (request.form.get("income_category") or "").strip()
        if inc_cat:
            set_setting(con, "income_category", inc_cat)
        set_income_rules(con, (request.form.get("income_pattern") or "").split(","))
        set_setting(con, "setup_done", "1")
        con.commit()
        con.close()
        flash("Eingerichtet. Du kannst alles jederzeit unter Einstellungen ändern.")
        return redirect(url_for("import_page"))
    st = {k: get_setting(con, k) for k in SETTING_DEFAULTS}
    inc_cats = con.execute(
        "SELECT name FROM categories WHERE kind='income' AND parent_id IS NULL ORDER BY name"
    ).fetchall()
    con.close()
    return render_template(
        "setup.html",
        st=st,
        sources=ALL_SOURCES,
        enabled=set(json.loads(st["sources"] or '["bank"]')),
        inc_cats=[r["name"] for r in inc_cats],
    )


@app.route("/hilfe", methods=["GET", "POST"])
def help_page():
    """The help adapts to the sources; the checkboxes at the top switch them
    directly (user 2026-10-14: "kann der user auch gleich auf der hilfeseite
    einstellen was er benoetigt?" - can the user set what he needs right on
    the help page?) - the same setting as under Einstellungen -> Datenquellen."""
    con = db()
    if request.method == "POST":
        srcs = sorted({"bank"} | {s for s, _, _ in ALL_SOURCES if request.form.get("src_" + s)})
        set_setting(con, "sources", json.dumps(srcs))
        con.commit()
        con.close()
        labels = [l for k, l, _ in ALL_SOURCES if k in srcs]
        flash("Quellen gespeichert: " + ", ".join(labels))
        return redirect(url_for("help_page"))
    try:
        srcs = enabled_sources(con)
    finally:
        con.close()
    return render_template("hilfe.html", enabled=srcs, sources=ALL_SOURCES)


@app.route("/einstellungen", methods=["GET", "POST"])
def settings_page():
    """Everything that used to be hard-coded (user request 2026-09-11: "es sollte
    nichts statisches sein, was man nicht auch aendern kann" - nothing should
    be static that cannot also be changed)."""
    con = db()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "sources":
            srcs = [s for s, _, _ in ALL_SOURCES if request.form.get("src_" + s)]
            if "bank" not in srcs:
                srcs.append("bank")  # without a bank account the app makes no sense
            set_setting(con, "sources", json.dumps(sorted(srcs)))
            flash("Datenquellen gespeichert")
        elif act == "colmap_delete":
            from .views_import import bank_column_maps

            maps = bank_column_maps(con)
            sig = request.form.get("sig") or ""
            if sig in maps:
                del maps[sig]
                set_setting(con, "bank_column_maps", json.dumps(maps, ensure_ascii=False))
                flash("Spaltenzuordnung gelöscht – die nächste Datei dieser Bank fragt wieder nach")
        elif act == "general":
            set_setting(con, "data_cutoff", parse_cutoff(request.form.get("data_cutoff")))
            if request.form.get("auto_prices_present"):
                set_setting(con, "auto_prices", "1" if request.form.get("auto_prices") else "")
            set_setting(con, "shared_name", (request.form.get("shared_name") or "").strip())
            # The Splitwise fields are only in the form when Splitwise is active
            # - otherwise "Speichern" would silently clear the values.
            if "splitwise_name" in request.form:
                set_setting(con, "splitwise_name", request.form["splitwise_name"].strip())
            if "fixed_cost_group" in request.form:
                set_setting(con, "fixed_cost_group", request.form["fixed_cost_group"].strip())
            if "urlaub_excluded" in request.form:
                set_setting(
                    con,
                    "urlaub_excluded_groups",
                    json.dumps([g.strip() for g in request.form["urlaub_excluded"].split(",") if g.strip()]),
                )
            set_setting(con, "fixkosten_cats", json.dumps(request.form.getlist("fixkosten_cats")))
            set_setting(con, "fixkosten_privat_cats", json.dumps(request.form.getlist("fixkosten_privat_cats")))
            inc_cat = (request.form.get("income_category") or "").strip()
            if inc_cat:
                if (
                    con.execute("SELECT 1 FROM categories WHERE name=? AND kind='income'", (inc_cat,)).fetchone()
                    is None
                ):
                    raise BadInput("Die Einkommenskategorie gibt es nicht.")
                set_setting(con, "income_category", inc_cat)
            if "income_pattern" in request.form:
                set_income_rules(con, request.form["income_pattern"].split(","))
            flash("Einstellungen gespeichert")
        elif act == "theme":
            t = request.form.get("theme")
            set_setting(con, "theme", t if t in THEME_KEYS else "auto")
        elif act == "share":
            words = [w.strip() for w in (request.form.get("share_forbidden") or "").split(",")]
            set_setting(con, "share_forbidden", json.dumps([w for w in words if w], ensure_ascii=False))
            set_setting(con, "public_repo_url", (request.form.get("public_repo_url") or "").strip())
            flash("Weitergabe-Begriffe gespeichert")
        elif act in ("fixed_add", "fixed_save"):
            # An empty pattern would be LIKE '%%' and match EVERY transaction; a
            # share outside 0-100 is nonsense - both used to be stored unchecked
            # (fuzz test 2026-09-11).
            pat = (request.form.get("pattern") or "").strip()
            if not pat:
                raise BadInput("Das Muster darf nicht leer sein.")
            share = form_int("share_percent", default=100, lo=1, hi=100)
            cat_id = form_category_id(con, required=False)
            kw = (request.form.get("sw_keyword") or "").strip()
            invalidate_fixed_cost_cache()
            if act == "fixed_add":
                con.execute(
                    "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES(?,?,?,?)",
                    (pat, cat_id, share, kw),
                )
                flash(f"Fixkosten-Eintrag „{pat}“ angelegt")
            else:
                fid = form_int("fixed_id")
                con.execute(
                    "UPDATE fixed_costs SET pattern=?, category_id=?, share_percent=?, sw_keyword=? WHERE id=?",
                    (pat, cat_id, share, kw, fid),
                )
                flash("Fixkosten-Eintrag gespeichert")
        elif act == "fixed_delete":
            invalidate_fixed_cost_cache()
            con.execute("DELETE FROM fixed_costs WHERE id=?", (form_int("fixed_id"),))
            flash("Fixkosten-Eintrag gelöscht")
        con.commit()
        con.close()
        return redirect(url_for("settings_page"))

    st = {k: get_setting(con, k) for k in SETTING_DEFAULTS}
    st["share_forbidden_text"] = ", ".join(get_setting_list(con, "share_forbidden"))
    cats = con.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY kind, name").fetchall()
    cat_groups = category_choice_groups(con)
    cat_by_id = {c["id"]: c for c in con.execute("SELECT * FROM categories")}
    fixed = fixed_cost_rows(con)
    inc_cats = [
        r["name"]
        for r in con.execute("SELECT name FROM categories WHERE kind='income' AND parent_id IS NULL ORDER BY name")
    ]
    income_pattern = ", ".join(income_rule_patterns(con))
    counts = dict(con.execute("SELECT source, COUNT(*) FROM transactions GROUP BY source").fetchall())
    from .views_import import bank_column_maps

    colmaps = [
        {
            "sig": sig,
            "name": m_.get("name", ""),
            "header": ", ".join(m_.get("header", [])[:8]),
            "fields": ", ".join(
                f for f in ("date", "amount", "debit", "credit", "partner", "desc") if m_.get(f) is not None
            ),
        }
        for sig, m_ in sorted(bank_column_maps(con).items())
    ]
    con.close()
    return render_template(
        "einstellungen.html",
        st=st,
        sources=ALL_SOURCES,
        colmaps=colmaps,
        enabled=set(json.loads(st["sources"] or '["bank"]')),
        cats=cats,
        cat_groups=cat_groups,
        cat_by_id=cat_by_id,
        fixed=fixed,
        counts=counts,
        fixkosten_cats=set(json.loads(st["fixkosten_cats"] or "[]")),
        privat_cats=set(json.loads(st["fixkosten_privat_cats"] or "[]")),
        inc_cats=inc_cats,
        income_pattern=income_pattern,
        urlaub_excluded=", ".join(json.loads(st["urlaub_excluded_groups"] or "[]")),
    )


@app.route("/sicherung", methods=["GET", "POST"])
def sicherung_page():
    """Back up, restore, CSV export - one page (user request 2026-09-15:
    "export umbenennen auf sicherung und dort eben die 2 exports
    ermoeglichen, aber auch den import automatisch aus den datenbank-
    sicherungen unter backups" - rename export to backup, offer both exports
    there and also restoring from the database backups under backups/).
    Reachable before setup too, so a fresh installation can load a database
    brought along."""
    if request.method == "POST":
        act = request.form.get("action")
        if act == "backup":
            name = backup_db()
            flash(f"Gesichert: backups/{name}" if name else "FEHLER Keine Datenbank gefunden")
        elif act == "restore":
            name = restore_backup(request.form.get("backup") or "")
            flash(
                f"Sicherung {name} zurückgespielt – der Stand davor liegt als neue "
                f"Sicherung „vor Wiederherstellung“ in backups/"
            )
        elif act == "delete":
            name = request.form.get("backup") or ""
            if not BACKUP_NAME_RE.fullmatch(name):
                raise BadInput("Ungültiger Sicherungsname.")
            path = os.path.join(_backups_dir(), name)
            if os.path.isfile(path):
                os.remove(path)
            flash(f"Sicherung {name} gelöscht")
        elif act == "upload":
            f = request.files.get("dbfile")
            if not f or not f.filename:
                raise BadInput("Keine Datei gewählt.")
            import tempfile

            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                f.save(tmp)
                restore_from_file(tmp)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            flash(
                f"Datenbank {f.filename} eingespielt – der Stand davor liegt als Sicherung "
                f"„vor Wiederherstellung“ in backups/"
            )
        return redirect(url_for("sicherung_page"))
    return render_template("sicherung.html", backups=list_backups())


@app.route("/sicherung/download/<name>")
def sicherung_download(name):
    if not BACKUP_NAME_RE.fullmatch(name or ""):
        raise BadInput("Ungültiger Sicherungsname.")
    path = os.path.join(_backups_dir(), name)
    if not os.path.isfile(path):
        raise BadInput("Diese Sicherung gibt es nicht (mehr).")
    return send_file(path, as_attachment=True, download_name=name)
