"""Routes: import with the complete pipeline and the result card; column
mapping for unknown bank CSVs (remembered per header row)."""

import io
import json
import os
import re
import secrets
import time
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for

from . import config
from .automations import (
    auto_match_card,
    auto_match_paypal,
    auto_split_wohnung_fixed,
    categorize_foreign_pos,
    detect_splitwise_anomalies,
    reconcile_fixed_cost_positions,
)
from .config import SYSTEM_CATS, BadInput
from .db import db, get_setting, set_setting
from .depot import import_depot
from .importers import cleanup_before_start, import_bank, import_card, import_paypal, import_splitwise, tracking_start
from .parsing import (
    BANK_COLUMNS,
    IMPORT_KIND_LABEL,
    detect_kind,
    find_col,
    header_signature,
    is_card_account,
    is_own_export,
    parse_amount,
    parse_date,
    read_csv_flexible,
    read_xlsx_rows,
)
from .repay import auto_link_repayments
from .rules import _RULE_HITS, apply_rules
from .splitwise import (
    auto_link_own_advances,
    auto_umbuchung_splitwise_settlements,
    promote_own_advance_credits,
    promote_splitwise_shares,
)
from .stats import todo_count
from .web import app


def _uploads_dir():
    """Holding area for unrecognised CSVs (until the user has mapped the
    columns) - next to the DB like backups/, so that tests stay in the temp
    folder. Files older than two days are deleted on the next import."""
    d = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def _stash_upload(name, raw):
    """Store a file for "Spalten zuordnen" (column mapping), returns the token."""
    d = _uploads_dir()
    cutoff = time.time() - 2 * 86400
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass
    token = secrets.token_hex(8)
    with open(os.path.join(d, token + ".csv"), "wb") as fh:
        fh.write(raw)
    with open(os.path.join(d, token + ".name"), "w", encoding="utf-8") as fh:
        fh.write(os.path.basename(name)[:120])
    return token


def bank_column_maps(con):
    """Remembered column mappings: header signature -> mapping (date,
    amount | debit+credit, partner, desc) plus header/name for display."""
    try:
        maps = json.loads(get_setting(con, "bank_column_maps", "") or "{}")
    except ValueError:
        maps = {}
    return maps if isinstance(maps, dict) else {}


def _import_one_file(filename, raw, forced_kind, maps, state):
    """Detect and import one file. `state` accumulates across all files:
    total_new, new_splitwise_ids, touched_groups, n_rules. Returns the result
    record for the card/history."""
    kind = "?"
    new = skip = removed = 0
    group = ""
    error = ""
    token = ""
    is_csv = not filename.lower().endswith((".xlsx", ".xlsm"))
    try:
        buf = io.BytesIO(raw)
        header, rows = read_csv_flexible(buf) if is_csv else read_xlsx_rows(buf)  # XLSX = Flatex depot transactions
        buf.seek(0)
        if is_own_export(header):
            raise ValueError(
                "Das ist der funkSTONKS-Export (nur zum Anschauen). Einen früheren Stand spielst du unter Sicherung zurück."
            )
        kind = detect_kind(header)
        # A header the user mapped (column mapping) counts like a recognised
        # bank statement, with the remembered columns.
        mapping = maps.get(header_signature(header)) if is_csv else None
        if mapping and kind in (None, "bank"):
            kind = "bank"
        if kind == "bank" and not mapping and is_card_account(header, rows):
            kind = "card"
        # A manual format choice beats detection (there is no standard for bank
        # CSVs, and credit-card detection only knows the George export).
        if forced_kind:
            kind = forced_kind
        if kind == "bank":
            new, skip, nr = import_bank(buf, mapping)
            state["n_rules"] += nr
        elif kind == "card":
            new, skip, nr = import_card(buf, filename)
            state["n_rules"] += nr
        elif kind == "paypal":
            new, skip, nr = import_paypal(buf)
            state["n_rules"] += nr
        elif kind == "splitwise":
            group = re.sub(r"_\d{4}-\d{2}-\d{2}.*$", "", filename).replace(".csv", "")
            group = re.sub(r"\s*\(\d+\)$", "", group).strip()  # "wg (1).csv" -> "wg"
            new, skip, ids, removed = import_splitwise(buf, group)
            state["new_splitwise_ids"] += ids
            tg = state["touched_groups"].setdefault(group, [0, 0])
            tg[0] += new
            tg[1] += removed
        elif kind == "depot":
            new, skip = import_depot(header, rows)
        else:
            kind = "?"
            error = "Format nicht erkannt"
        state["total_new"] += new
    except Exception as e:
        error = str(e)[:200]
    # Unknown header or bank CSV without a date/amount column: keep the file
    # and offer "Spalten zuordnen" (column mapping).
    if is_csv and error and (kind == "?" or "keine Spalte für Datum" in error):
        try:
            token = _stash_upload(filename, raw)
        except OSError:
            token = ""
    return {
        "name": filename,
        "kind": kind,
        "label": IMPORT_KIND_LABEL.get(kind, "–"),
        "group": group,
        "new": new,
        "skipped": skip,
        "removed": removed,
        "error": error,
        "token": token,
        # A card account of another bank is read as a bank statement - at least warn
        "card_hint": kind == "bank"
        and not error
        and re.search(r"kredit|karte|card|visa|master", filename, re.I) is not None,
    }


def _record_import_history(con, file_results):
    """Import history: answers "did I already upload July?"."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    for fr in file_results:
        if fr["error"]:
            note = "Format nicht erkannt" if fr["kind"] == "?" else "Fehler: " + fr["error"]
        else:
            note = f"Gruppe {fr['group']}" if fr["group"] else ""
        con.execute(
            "INSERT INTO imports(ts,filename,kind,new_rows,skipped,removed,note) VALUES(?,?,?,?,?,?,?)",
            (ts, fr["name"], fr["kind"], fr["new"], fr["skipped"], fr["removed"], note),
        )


def _reopen_touched_groups(con, touched_groups):
    """A closed Splitwise group with new activity is no longer closed.
    Returns the groups that were reopened."""
    reopened = []
    for group, (g_new, g_removed) in touched_groups.items():
        if (g_new or g_removed) and con.execute("SELECT 1 FROM closed_groups WHERE partner=?", (group,)).fetchone():
            con.execute("DELETE FROM closed_groups WHERE partner=?", (group,))
            reopened.append(group)
    return reopened


def _run_automations(con):
    """The pipeline steps in fixed order (all idempotent). Returns the hit
    counts per step key plus `start` (tracking_start)."""
    start = tracking_start(con)
    n = {
        "clean": cleanup_before_start(con),
        "recon": reconcile_fixed_cost_positions(con),
        "paypal": auto_match_paypal(con),
        "card": auto_match_card(con),
    }
    apply_rules(con)
    n["fixed"] = auto_split_wohnung_fixed(con)
    n["foreign"] = categorize_foreign_pos(con)
    n["promoted"] = promote_splitwise_shares(con)
    n["linked"] = auto_link_own_advances(con)
    n["credited"] = promote_own_advance_credits(con)
    n["settle"] = auto_umbuchung_splitwise_settlements(con)
    n["repaid"] = auto_link_repayments(con)
    con.commit()
    # Count rule hits only NOW: a bank debit that a rule categorised and that
    # was then netted as a PayPal/card settlement no longer counts
    # (audit 2026-09-15: 85 instead of 75).
    n["rules"] = 0
    if _RULE_HITS:
        ph = ",".join("?" * len(set(_RULE_HITS)))
        n["rules"] = con.execute(
            "SELECT COUNT(*) FROM transactions t JOIN categories c ON c.id=t.category_id "
            "WHERE t.id IN (" + ph + ") AND t.status='active' AND c.name NOT IN ('" + "','".join(SYSTEM_CATS) + "')",
            list(set(_RULE_HITS)),
        ).fetchone()[0]
    n["start"] = start
    return n


_STEP_TEXTS = [
    ("rules", "Zahlung per Regel kategorisiert", "Zahlungen per Regel kategorisiert"),
    (
        "fixed",
        "Fixkosten-Buchung zugeordnet bzw. auf deinen Anteil gekürzt",
        "Fixkosten-Buchungen zugeordnet bzw. auf deinen Anteil gekürzt",
    ),
    ("foreign", "Auslandszahlung als Urlaub kategorisiert", "Auslandszahlungen als Urlaub kategorisiert"),
    (
        "paypal",
        "PayPal-Abbuchung mit der echten Zahlung verrechnet",
        "PayPal-Abbuchungen mit den echten Zahlungen verrechnet",
    ),
    (
        "card",
        "Kreditkartenabrechnung mit der Kontoabbuchung verrechnet",
        "Kreditkartenabrechnungen mit der Kontoabbuchung verrechnet",
    ),
    (
        "promoted",
        "Splitwise-Anteil (von anderen vorgestreckt) als Ausgabe übernommen",
        "Splitwise-Anteile (von anderen vorgestreckt) als Ausgaben übernommen",
    ),
    (
        "linked",
        "eigene Vorstreckung mit der Kontobuchung verknüpft – nur dein Anteil zählt",
        "eigene Vorstreckungen mit den Kontobuchungen verknüpft – nur dein Anteil zählt",
    ),
    (
        "credited",
        "eigene Vorstreckung ohne Kontotreffer als Anteils-Gutschrift verbucht",
        "eigene Vorstreckungen ohne Kontotreffer als Anteils-Gutschrift verbucht",
    ),
    (
        "settle",
        "Splitwise-Ausgleichszahlung als Umbuchung verrechnet",
        "Splitwise-Ausgleichszahlungen als Umbuchung verrechnet",
    ),
    (
        "repaid",
        "Rückzahlung per Regel mit der Ausgabe verrechnet – dort zählt nur der Rest",
        "Rückzahlungen per Regel mit den Ausgaben verrechnet – dort zählt nur der Rest",
    ),
    (
        "recon",
        "Splitwise-Fixkostenposition zurückgesetzt – zählt über die gekürzte Kontobuchung",
        "Splitwise-Fixkostenpositionen zurückgesetzt – zählen über die gekürzte Kontobuchung",
    ),
    (
        "clean",
        "Zahlung vor dem Beginn der Auswertung ({start}) entfernt – Stichtag: Einstellungen",
        "Zahlungen vor dem Beginn der Auswertung ({start}) entfernt – Stichtag: Einstellungen",
    ),
]


def _steps_for_card(n):
    """What the automation did - only steps with hits, in the order that
    matters to the user (categories, nettings, cleanup)."""
    out = []
    for key, sing, plur in _STEP_TEXTS:
        if n.get(key):
            text = (sing if n[key] == 1 else plur).replace("{start}", str(n["start"]))
            out.append({"key": key, "n": n[key], "text": text})
    return out


def run_import(files, forced_kind=""):
    """The complete import pipeline for a list of (filename, bytes): detect and
    import each file (_import_one_file), write the history, reopen closed
    Splitwise groups, run all automation steps (_run_automations), build the
    result dict and store it under settings.last_import_result (the GET page
    shows it once). Used by import_page (upload) and import_colmap (after
    mapping)."""
    if forced_kind not in ("bank", "card", "paypal", "splitwise", "depot"):
        forced_kind = ""
    del _RULE_HITS[:]
    con = db()
    maps = bank_column_maps(con)
    con.close()
    state = {"total_new": 0, "new_splitwise_ids": [], "touched_groups": {}, "n_rules": 0}
    file_results = [_import_one_file(name, raw, forced_kind, maps, state) for name, raw in files]
    con = db()
    _record_import_history(con, file_results)
    reopened = _reopen_touched_groups(con, state["touched_groups"])
    con.commit()
    sw_warnings = detect_splitwise_anomalies(con, state["new_splitwise_ids"])
    n = _run_automations(con)
    result = {
        "ts": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "files": file_results,
        "total_new": state["total_new"],
        "steps": _steps_for_card(n),
        "warnings": sw_warnings,
        "todo": todo_count(con),
        "reopened": reopened,
    }
    # Not in the session (cookie, 4 KB) - once in settings, the GET page picks it up.
    set_setting(con, "last_import_result", json.dumps(result, ensure_ascii=False))
    con.commit()
    con.close()
    return result


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "POST":
        files = [(f.filename, f.read()) for f in request.files.getlist("files") if f and f.filename]
        if not files:
            flash("FEHLER Keine Dateien gewählt")
            return redirect(url_for("import_page"))
        run_import(files, request.form.get("force_kind") or "")
        return redirect(url_for("import_page"))
    con = db()
    counts = {r["source"]: r["n"] for r in con.execute("SELECT source, COUNT(*) n FROM transactions GROUP BY source")}
    # Newest transaction per source - so it is clear from when the next export
    # has to be pulled.
    latest = {
        r["source"]: r["d"] for r in con.execute("SELECT source, MAX(booking_date) d FROM transactions GROUP BY source")
    }
    dep = con.execute("SELECT COUNT(*) n, MAX(booking_date) d FROM depot_tx").fetchone()
    counts["depot"], latest["depot"] = dep["n"], dep["d"]
    n_todo = todo_count(con)
    imports = con.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 40").fetchall()
    # Result of the import that just ran - show once, then gone
    # (the history below stays).
    result = None
    raw = get_setting(con, "last_import_result", "")
    if raw:
        try:
            result = json.loads(raw)
        except ValueError:
            result = None
        con.execute("DELETE FROM settings WHERE key='last_import_result'")
        con.commit()
    con.close()
    return render_template("import.html", counts=counts, latest=latest, n_todo=n_todo, imports=imports, result=result)


@app.route("/import/zuordnen", methods=["GET", "POST"])
def import_colmap():
    """Map the columns of an unrecognised bank CSV (date, amount or
    debit/credit, payee, purpose). The mapping is remembered under the header
    signature in settings.bank_column_maps - every further file of this bank
    then runs as a bank statement without asking - and the file is imported
    right away (complete pipeline)."""
    token = (request.values.get("t") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        raise BadInput("Ungültiger Verweis.")
    path = os.path.join(_uploads_dir(), token + ".csv")
    name_path = os.path.join(_uploads_dir(), token + ".name")
    if not os.path.exists(path):
        flash("FEHLER Die Datei liegt nicht mehr bereit – bitte nochmal hochladen.")
        return redirect(url_for("import_page"))
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        name = open(name_path, encoding="utf-8").read().strip() or "Datei.csv"
    except OSError:
        name = "Datei.csv"
    header, rows = read_csv_flexible(io.BytesIO(raw))
    if request.method == "POST":

        def idx(field):
            v = (request.form.get(field) or "").strip()
            return int(v) if v.isdigit() and int(v) < len(header) else None

        mapping = {"date": idx("date"), "partner": idx("partner"), "desc": idx("desc")}
        if mapping["date"] is None:
            raise BadInput("Spalte für das Datum wählen.")
        if request.form.get("mode") == "split":
            mapping["debit"], mapping["credit"] = idx("debit"), idx("credit")
            if mapping["debit"] is None or mapping["credit"] is None or mapping["debit"] == mapping["credit"]:
                raise BadInput("Zwei verschiedene Spalten für Soll und Haben wählen.")
            amt = (mapping["debit"], mapping["credit"])
        else:
            mapping["amount"] = idx("amount")
            if mapping["amount"] is None:
                raise BadInput("Spalte für den Betrag wählen.")
            amt = mapping["amount"]
        if amt == mapping["date"] or mapping["date"] in (amt if isinstance(amt, tuple) else ()):
            raise BadInput("Datum und Betrag können nicht dieselbe Spalte sein.")
        # Probe: does the mapping yield at least one row with date AND amount?
        ok = 0
        for r in rows:

            def cell(i, r=r):
                return r[i].strip() if i is not None and i < len(r) else ""

            d = parse_date(cell(mapping["date"]))
            if isinstance(amt, tuple):
                a = parse_amount(cell(amt[0])) if cell(amt[0]) else None
                b = parse_amount(cell(amt[1])) if cell(amt[1]) else None
                a = None if a is None and b is None else 0.0
            else:
                a = parse_amount(cell(amt))
            if d is not None and a is not None:
                ok += 1
        if not ok:
            raise BadInput(
                "Mit dieser Zuordnung ergibt keine Zeile ein Datum und einen Betrag – andere Spalten wählen."
            )
        con = db()
        maps = bank_column_maps(con)
        maps[header_signature(header)] = {**mapping, "name": name, "header": [h.strip()[:40] for h in header][:30]}
        set_setting(con, "bank_column_maps", json.dumps(maps, ensure_ascii=False))
        con.commit()
        con.close()
        run_import([(name, raw)], "")
        for p in (path, name_path):
            try:
                os.remove(p)
            except OSError:
                pass
        return redirect(url_for("import_page"))
    guess = {k: find_col(header, kws) for k, kws in BANK_COLUMNS.items() if k != "cur"}
    if guess["amount"] is None:
        guess["debit"] = find_col(header, ["soll", "belastung", "ausgang", "debit", "lastschrift"])
        guess["credit"] = find_col(header, ["haben", "gutschrift", "eingang", "credit"])
    cols = [(i, (h.strip() or f"Spalte {i + 1}")[:40]) for i, h in enumerate(header)]
    preview = [[(c or "")[:40] for c in r[: len(header)]] for r in rows[:5]]
    return render_template(
        "import_zuordnen.html",
        token=token,
        name=name,
        cols=cols,
        preview=preview,
        guess=guess,
        split=guess["amount"] is None and guess.get("debit") is not None,
    )
