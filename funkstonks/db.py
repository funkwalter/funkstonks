"""Database access and settings: db(), init_db() with migrations, get/set_setting,
fixed-cost helpers, system_cat_id()."""

import json
import sqlite3
from datetime import date

from . import config
from .config import DEFAULT_CATEGORIES, DEFAULT_RULES, SETTING_DEFAULTS, BadInput

# Deliberately NO income rule here: how the employer appears on the bank
# statement differs for everyone. It is asked during setup and stays editable
# under /einstellungen (user request 2026-09-11: one's own employer does not
# belong in the code as a default rule).


def db():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    # Every connection opened within a request is guaranteed to be closed at
    # the end of the request (see _close_leftover_connections) - even if the
    # route aborts with an exception. Without this, a single 500 left a
    # connection with an open transaction hanging and EVERY further write ran
    # into "database is locked" (reproduced for real, 2026-09-11).
    try:
        from flask import g, has_request_context

        if has_request_context():
            g.setdefault("_db_cons", []).append(con)
    except Exception:
        pass
    return con


def parse_cutoff(raw):
    raw = (raw or "").strip()
    if raw:
        try:
            date.fromisoformat(raw)
        except ValueError as e:
            raise BadInput("Stichtag muss ein Datum sein (JJJJ-MM-TT).") from e
    return raw


def get_setting(con, key, default=None):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if r is not None:
        return r["value"]
    return SETTING_DEFAULTS.get(key, "") if default is None else default


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_setting_list(con, key):
    """Setting stored as a JSON list. Broken values yield an empty list instead
    of a crash - a setting must never make the app unusable."""
    try:
        v = json.loads(get_setting(con, key) or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def enabled_sources(con):
    """Which data sources the user has switched on. 'bank' is always included -
    without account data the app makes no sense."""
    s = set(get_setting_list(con, "sources")) or {"bank"}
    s.add("bank")
    return s


def splitwise_name(con):
    return get_setting(con, "splitwise_name") or ""


def fixed_cost_rows(con):
    """Configured fixed costs from the fixed_costs table - cached per request
    (the Splitwise page and the pipeline query them per row)."""
    try:
        from flask import g, has_request_context

        cache = g.setdefault("_fixed_rows", {}) if has_request_context() else None
    except Exception:
        cache = None
    if cache is not None and "rows" in cache:
        return cache["rows"]
    rows = con.execute(
        "SELECT f.*, c.name AS cat_name FROM fixed_costs f LEFT JOIN categories c ON c.id=f.category_id ORDER BY f.id"
    ).fetchall()
    if cache is not None:
        cache["rows"] = rows
    return rows


def invalidate_fixed_cost_cache():
    try:
        from flask import g, has_request_context

        if has_request_context():
            g.pop("_fixed_rows", None)
    except Exception:
        pass


def fixed_cost_patterns(con):
    return [r["pattern"] for r in fixed_cost_rows(con) if (r["pattern"] or "").strip()]


def like_escape(text):
    """`%` and `_` from user input are wildcards for LIKE - escape them,
    otherwise a search for "%" lists everything and a pattern "A_B" matches "AXB"."""
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fixed_cost_like_sql(patterns, column="partner"):
    """(SQL fragment, parameters) for "column LIKE %pattern%", OR-joined.

    With an EMPTY pattern list "0" comes back, i.e. an always-false condition.
    The patterns used to be a non-empty constant in the code; now the list may
    be empty (someone who has configured no fixed costs), and an assembled
    "AND ()" would be a SQL syntax error.
    """
    if not patterns:
        return "0", []
    return (" OR ".join([f"{column} LIKE ? ESCAPE '\\'"] * len(patterns)), [f"%{like_escape(p)}%" for p in patterns])


def fixed_cost_sw_keywords(con):
    return [k.strip().lower() for r in fixed_cost_rows(con) for k in (r["sw_keyword"] or "").split(",") if k.strip()]


def fixed_cost_group(con):
    """Name of the Splitwise group through which the shared fixed costs run
    (e.g. "wohnung" or "WG"). Positions of this group that match a fixed-cost
    keyword are already counted via the reduced bank booking and must never
    count a second time. The group name used to be hard-coded in five places -
    for any other user (group "WG", "Haus", ...) the exception would never
    have applied."""
    return (get_setting(con, "fixed_cost_group") or "").strip().lower()


def is_fixed_cost_group(con, partner):
    g = fixed_cost_group(con)
    return bool(g) and (partner or "").strip().lower() == g


def income_category_name(con):
    """Which income category counts as "Gehalt" (salary).

    The yearly overview shows the figure "avg. salary/month" based on it. The
    name "Gehalt" used to be hard-coded; anyone naming their income differently
    (Honorar, Lohn, Pension) never got to see the figure.
    """
    return get_setting(con, "income_category") or "Gehalt"


def system_cat_id(con, name):
    """Returns the id of a SYSTEM_CATS category, silently recreating it if needed."""
    row = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    color, kind = next((c, k) for n, c, k in DEFAULT_CATEGORIES if n == name)
    cur = con.execute("INSERT INTO categories(name,color,kind) VALUES(?,?,?)", (name, color, kind))
    return cur.lastrowid


def _create_schema(con):
    """All tables (CREATE TABLE IF NOT EXISTS) - the data model, described in docs/REFERENCE.md."""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS categories(
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        color TEXT NOT NULL DEFAULT '#888888',
        kind TEXT NOT NULL DEFAULT 'expense'   -- expense | income | transfer
    );
    CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,                  -- bank | paypal | card | splitwise
        booking_date TEXT NOT NULL,            -- ISO yyyy-mm-dd
        partner TEXT DEFAULT '',
        description TEXT DEFAULT '',
        amount REAL NOT NULL,                  -- negativ = Ausgabe
        currency TEXT DEFAULT 'EUR',
        category_id INTEGER REFERENCES categories(id),
        effective_amount REAL,                 -- überschreibt amount (z.B. eigener Splitwise-Anteil)
        status TEXT NOT NULL DEFAULT 'active', -- active | excluded | matched
        note TEXT DEFAULT '',
        raw_hash TEXT UNIQUE,
        extra TEXT DEFAULT '{}'                -- JSON (Splitwise-Anteil, PayPal-Typ, ...)
    );
    CREATE TABLE IF NOT EXISTS matches(
        id INTEGER PRIMARY KEY,
        bank_tx INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
        other_tx INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                    -- paypal | card | splitwise_share
        UNIQUE(bank_tx, other_tx)
    );
    CREATE TABLE IF NOT EXISTS splits(
        id INTEGER PRIMARY KEY,
        tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        category_id INTEGER NOT NULL REFERENCES categories(id),
        amount REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS rules(
        id INTEGER PRIMARY KEY,
        pattern TEXT NOT NULL,                 -- Substring, case-insensitive, auf Partner+Text
        category_id INTEGER NOT NULL REFERENCES categories(id),
        priority INTEGER NOT NULL DEFAULT 100
    );
    CREATE TABLE IF NOT EXISTS urlaub_groups(
        partner TEXT PRIMARY KEY               -- Splitwise-Gruppenname, komplett als "Urlaub" markiert
    );
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL                    -- alles als Text, Listen als JSON
    );
    CREATE TABLE IF NOT EXISTS closed_groups(       -- Splitwise-Gruppen, die abgeschlossen sind
        partner TEXT PRIMARY KEY,
        closed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS imports(
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,                      -- Zeitpunkt des Uploads
        filename TEXT NOT NULL,
        kind TEXT NOT NULL,                    -- bank | card | paypal | splitwise | ?
        new_rows INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        removed INTEGER NOT NULL DEFAULT 0,
        note TEXT DEFAULT ''                   -- Fehlermeldung, Gruppenname
    );
    CREATE TABLE IF NOT EXISTS fixed_costs(
        id INTEGER PRIMARY KEY,
        pattern TEXT NOT NULL,                 -- Substring auf Partner+Verwendungszweck
        category_id INTEGER REFERENCES categories(id),
        share_percent INTEGER NOT NULL DEFAULT 100,  -- 100 = voll, 50 = haelftig geteilt
        sw_keyword TEXT NOT NULL DEFAULT ''    -- zugehoerige Splitwise-Kategorie (optional)
    );
    CREATE TABLE IF NOT EXISTS repay_rules(         -- Rueckzahlungs-Regeln (auto_link_repayments)
        id INTEGER PRIMARY KEY,
        in_pattern TEXT NOT NULL,              -- Substring auf Partner+Text der EINGEHENDEN Zahlung
        out_pattern TEXT NOT NULL,             -- Substring auf Partner+Text der Ausgabe
        window_days INTEGER NOT NULL DEFAULT 45 -- wie weit die Ausgabe vor dem Eingang liegen darf
    );
    CREATE TABLE IF NOT EXISTS series_overrides(    -- Laufende Kosten: von Hand ausgeblendet / erzwungen
        key TEXT PRIMARY KEY,                  -- Serien-Schluessel aus detect_recurring (rule:<muster>, Empfaenger, fixed:<muster>)
        mode TEXT NOT NULL,                    -- hide | force | dismiss | merge (target = Zielserie)
        label TEXT DEFAULT ''                  -- Anzeigename zum Zeitpunkt der Entscheidung
    );
    CREATE TABLE IF NOT EXISTS price_history(       -- jeder geholte/gesetzte Kurs mit Handelstag: Depotwert je Monat
        isin TEXT NOT NULL,
        price_date TEXT NOT NULL,
        price_eur REAL NOT NULL,
        PRIMARY KEY(isin, price_date)
    );
    CREATE TABLE IF NOT EXISTS prices(              -- Kurse je ISIN (Yahoo auf Klick oder von Hand), siehe quotes.py
        isin TEXT PRIMARY KEY,
        symbol TEXT DEFAULT '',                -- Yahoo-Symbol (gemerkt, spart die Suche)
        price REAL NOT NULL,                   -- Kurs in der Notierungswaehrung
        currency TEXT DEFAULT 'EUR',
        price_eur REAL NOT NULL,               -- in Euro (Wechselkurs vom Abruf)
        price_date TEXT,                       -- Handelstag ISO
        fetched_at TEXT,                       -- Zeitpunkt des Abrufs / der Eingabe
        source TEXT DEFAULT 'yahoo'            -- yahoo | manual
    );
    CREATE TABLE IF NOT EXISTS depot_tx(            -- Depotumsaetze (Flatex-Export), getrennt von transactions
        id INTEGER PRIMARY KEY,
        booking_date TEXT NOT NULL,            -- ISO
        valuta TEXT DEFAULT '',
        name TEXT NOT NULL,                    -- Bezeichnung des Wertpapiers
        isin TEXT NOT NULL DEFAULT '',
        qty REAL NOT NULL DEFAULT 0,           -- Stueck (positiv)
        amount REAL NOT NULL,                  -- Betrag in EUR (positiv)
        price REAL,                            -- Kurs je Stueck
        kind TEXT NOT NULL DEFAULT 'buy',      -- buy | sell | other
        ta_nr TEXT DEFAULT '',
        info TEXT DEFAULT '',
        raw_hash TEXT UNIQUE
    );
    """)


def _migrate_columns(con):
    """Guarded ALTER TABLE for existing databases created before a feature
    (each column only if it is missing)."""
    # Guarded ALTER TABLE for existing DBs created before this feature (user
    # request 2026-08-07: free comment per transaction) - a separate field
    # instead of reusing `note`, because `note` is filled by various automations
    # (auto_split_wohnung_fixed, auto_match_*, ...) with hard-coded explanatory
    # texts - a user comment would overwrite those or, conversely, be
    # overwritten by the next automation.
    tx_cols = {r["name"] for r in con.execute("PRAGMA table_info(transactions)")}
    if "comment" not in tx_cols:
        con.execute("ALTER TABLE transactions ADD COLUMN comment TEXT DEFAULT ''")
    # Subcategories (user request 2026-08-07: "unter kategorien auch noch die
    # moeglichkeit subkategorien zu erstellen" - the option to create
    # subcategories under categories) - exactly ONE level deep (a subcategory
    # cannot have children of its own; keeps the model simple and covers the
    # described use case exactly). NULL = top level, as before.
    cat_cols = {r["name"] for r in con.execute("PRAGMA table_info(categories)")}
    if "parent_id" not in cat_cols:
        con.execute("ALTER TABLE categories ADD COLUMN parent_id INTEGER REFERENCES categories(id)")
    match_cols = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    if "amount" not in match_cols:
        # How much this link changed the bank side's effective_amount - so that
        # ONE of several links on the same withdrawal (cash pool) can be undone
        # without deleting the others along with it.
        con.execute("ALTER TABLE matches ADD COLUMN amount REAL")
    so_cols = {r["name"] for r in con.execute("PRAGMA table_info(series_overrides)")}
    if "target" not in so_cols:
        # mode='merge': the bookings of this key count towards the series
        # `target` (user decision 2026-10-14: add Anthropic API purchases to the subscription).
        con.execute("ALTER TABLE series_overrides ADD COLUMN target TEXT DEFAULT ''")
    # Seed only into an EMPTY table. This used to run on every start via
    # INSERT OR IGNORE - a renamed default category came back as an empty copy
    # on the next start (journey test 2026-09-13). Categories the code needs
    # by name are created by system_cat_id() itself when needed.


def _seed_defaults(con):
    """Default categories only into an EMPTY table, default rules only ONCE
    per database (marker rules_seeded)."""
    seed_cats = con.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
    for name, color, kind in DEFAULT_CATEGORIES if seed_cats else ():
        con.execute("INSERT OR IGNORE INTO categories(name,color,kind) VALUES(?,?,?)", (name, color, kind))
    # Default rules only ONCE per database (marker) - whoever deletes all rules
    # wants none (the help page promised "deleted ones stay deleted").
    if con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0 and not get_setting(con, "rules_seeded", ""):
        for pattern, cat in DEFAULT_RULES:
            row = con.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone()
            if row:
                con.execute("INSERT INTO rules(pattern,category_id) VALUES(?,?)", (pattern, row["id"]))
    set_setting(con, "rules_seeded", "1")


def init_db():
    con = db()
    _create_schema(con)
    _migrate_columns(con)
    _seed_defaults(con)
    migrate_settings(con)
    migrate_settings_v2(con)
    con.commit()
    con.close()


def migrate_settings(con):
    """Lift an existing database onto the settings table.

    A DB that already holds bookings but no settings yet (state before
    2026-09-11) gets the active sources from the data and counts as set up -
    everything else the user adds under /einstellungen. A fresh DB starts
    with the setup wizard. Runs only ONCE (marker `migrated`) so that later
    changes by the user are not overwritten on every start. The owner's
    personal legacy values (formerly legacy_settings.py) have lived only in
    his DB since 2026-09-15.
    """
    if get_setting(con, "migrated", ""):
        return
    has_data = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 0
    if has_data:
        srcs = sorted({r["source"] for r in con.execute("SELECT DISTINCT source FROM transactions")})
        set_setting(con, "sources", json.dumps(srcs or ["bank"]))
        set_setting(con, "setup_done", "1")
    for k, v in SETTING_DEFAULTS.items():
        if con.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone() is None:
            set_setting(con, k, v)
    set_setting(con, "migrated", "1")


def migrate_settings_v2(con):
    """Second, separately marked stage: the group name of the shared fixed
    costs was hard-wired as "wohnung" until 2026-09-11. A DB that has this
    group gets the value written in - otherwise the double-counting guard
    would silently stop working after the update."""
    if get_setting(con, "migrated_v2", ""):
        return
    if not get_setting(con, "fixed_cost_group", ""):
        has = con.execute(
            "SELECT 1 FROM transactions WHERE source='splitwise' AND lower(trim(partner))='wohnung' LIMIT 1"
        ).fetchone()
        if has:
            set_setting(con, "fixed_cost_group", "wohnung")
    set_setting(con, "migrated_v2", "1")


def disabled_automations(con):
    """Built-in automations the user has deleted under rules (user request
    2026-10-13: "fest eingebaute automatik soll auch loeschbar sein - dann
    gemerkt bleiben" - built-in automation must be deletable too, and then
    stay remembered). Keys: "foreign_pos" (foreign POS -> Urlaub) and
    "sw:<category>" (one row of the Splitwise keyword list)."""
    return set(get_setting_list(con, "disabled_automations"))
