"""Flask app, form helpers (form_int, BadInput handler), connection teardown,
template globals (inject_globals), setup guard, main()."""

import os
import secrets
import sqlite3
import time

from flask import Flask, abort, flash, redirect, request, url_for

from .automations import health_checks
from .config import APP_VERSION, RES_DIR, THEME_KEYS, THEMES, BadInput
from .db import db, enabled_sources, get_setting, init_db, set_setting
from .share import is_dev_checkout
from .stats import todo_count
from .sync import STATE as SYNC_STATE
from .sync import checkout_mode, restart_app, startup_sync

# Since 2026-09-11 the user's own name in the Splitwise export lives in the
# settings (splitwise_name), no longer here - see SETTING_DEFAULTS.

app = Flask(__name__, template_folder=os.path.join(RES_DIR, "templates"), static_folder=os.path.join(RES_DIR, "static"))
# The real session key lives in the database (setting secret_key, generated
# once per database by load_secret_key) - nothing secret in the code (user
# 2026-10-15). This placeholder only marks "not loaded yet".
_UNSET_SECRET = "funkstonks-unset"
app.secret_key = _UNSET_SECRET
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # upload limit (CSV exports are KB-sized)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


def load_secret_key(con=None):
    """Session key from the settings table; generated on first use. Tests set
    app.secret_key themselves before the first request and are left alone."""
    if app.secret_key != _UNSET_SECRET:
        return
    own = con is None
    if own:
        con = db()
    try:
        key = get_setting(con, "secret_key", "")
        if not key:
            key = secrets.token_hex(32)
            set_setting(con, "secret_key", key)
            con.commit()
        app.secret_key = key
    finally:
        if own:
            con.close()


@app.teardown_request
def _close_leftover_connections(exc):
    from flask import g

    for con in g.pop("_db_cons", []):
        try:
            if exc is not None:
                con.rollback()
            con.close()
        except Exception:
            pass


def form_int(name, default=None, lo=None, hi=None):
    """Integer from the form, with an understandable error message instead
    of a ValueError crash; lo/hi are inclusive."""
    raw = (request.form.get(name) or "").strip()
    if raw == "":
        if default is None:
            raise BadInput(f"Feld „{name}“ fehlt.")
        return default
    try:
        v = int(float(raw))
    except ValueError as e:
        raise BadInput(f"„{raw}“ ist keine Zahl.") from e
    if lo is not None and v < lo or hi is not None and v > hi:
        raise BadInput(f"{v} liegt ausserhalb von {lo}–{hi}.")
    return v


def local_path(url):
    """Accept only paths inside the app as a return target - a
    `back=https://...` must not redirect to the outside."""
    url = (url or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        return url
    return ""


def form_category_id(con, name="category_id", required=True):
    """Category ID from the form, checked against the table - a stale browser
    tab can send an ID that was deleted long ago."""
    raw = (request.form.get(name) or "").strip()
    if raw == "":
        if required:
            raise BadInput("Keine Kategorie gewählt.")
        return None
    if not raw.isdigit() or con.execute("SELECT 1 FROM categories WHERE id=?", (int(raw),)).fetchone() is None:
        raise BadInput("Diese Kategorie gibt es nicht (mehr) – Seite neu laden.")
    return int(raw)


def _back():
    """Back to the calling page - but only if it lives on this server (a
    foreign referer must not become the redirect target)."""
    ref = request.referrer or ""
    if ref.startswith(request.host_url):
        return redirect(ref)
    return redirect(url_for("dashboard"))


@app.errorhandler(BadInput)
def _bad_input(e):
    flash(f"FEHLER Eingabe nicht übernommen: {e}")
    return _back()


@app.errorhandler(sqlite3.IntegrityError)
def _integrity(e):
    # e.g. a foreign key to a deleted category or a duplicate name
    flash(
        "FEHLER Eingabe nicht übernommen: sie passt nicht zum aktuellen Datenbestand "
        "(Kategorie gelöscht? Name schon vergeben?). Seite neu laden und nochmal."
    )
    return _back()


@app.errorhandler(ValueError)
def _value_error(e):
    # An internal ValueError is no user error - show no Python message, but
    # do not crash either.
    flash("FEHLER Eingabe nicht übernommen: ungültiger Wert.")
    return _back()


@app.context_processor
def inject_globals():
    """Values EVERY page needs: which data sources are active (navigation)
    and which colour scheme applies. One query per page view - completely
    harmless for a local single-user app and much simpler than passing it
    through every single route."""
    try:
        con = db()
        try:
            theme = get_setting(con, "theme") or "auto"
            done = bool(get_setting(con, "setup_done"))
            alerts = {}
            if done:
                for h in health_checks(con):
                    alerts[h["nav"]] = alerts.get(h["nav"], 0) + 1
            return {
                "nav_sources": enabled_sources(con),
                "ui_theme": theme if theme in THEME_KEYS else "auto",
                "themes": THEMES,
                # counter at the menu item "Durchgehen" and hints at Import/Splitwise
                "nav_todo": todo_count(con) if done else 0,
                "nav_alerts": alerts,
                "demo_active": bool(get_setting(con, "demo_data", "")),
                "setup_done": bool(get_setting(con, "setup_done")),
                # distribution settings only in the developer checkout (publishes to the public repo)
                "is_dev_checkout": is_dev_checkout(),
                "app_version": APP_VERSION,
                # banner "Neue Version" on every page (sync.check_* at startup or on request)
                "update_info": SYNC_STATE.get("update"),
                "sync_state": SYNC_STATE,
                "checkout_mode": checkout_mode(),
            }
        finally:
            con.close()
    except sqlite3.Error:
        # Before the very first init_db() the tables do not exist yet - then
        # neutral values are better than an error page.
        return {
            "nav_sources": {"bank"},
            "ui_theme": "auto",
            "themes": THEMES,
            "nav_todo": 0,
            "nav_alerts": {},
            "demo_active": False,
            "setup_done": False,
            "app_version": APP_VERSION,
            "update_info": None,
            "sync_state": SYNC_STATE,
            "checkout_mode": checkout_mode(),
        }


@app.before_request
def csrf_guard():
    """Cross-site request forgery: the app listens on localhost, so any web
    page open in the same browser could POST here. Browsers attach Origin and
    Sec-Fetch-Site to every cross-site POST - such a request is rejected
    with 403. Requests without either header (curl, the test client) are not
    browser-borne and pass. The session cookie is SameSite=Lax as well."""
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        abort(403)
    site = request.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        abort(403)
    return None


@app.errorhandler(403)
def _forbidden(e):
    return "Abgelehnt: Anfragen von fremden Seiten nimmt funkSTONKS nicht an.", 403


@app.before_request
def _secret_key_ready():
    """The first request loads (or creates) the session key from the DB."""
    if app.secret_key == _UNSET_SECRET:
        try:
            load_secret_key()
        except sqlite3.Error:
            pass  # before init_db() - no session needed yet
    return None


# ------------------------------------------------------------------ browser heartbeat
# User 2026-11-02: closing the browser window shall close the console window
# too - a layperson must not have to know that a server is running. Every
# page pings every HEARTBEAT_EVERY seconds (static/js/app.js) and sends /bye
# when it is left; the watchdog thread in main() ends the process when no
# page has pinged for HEARTBEAT_TIMEOUT seconds (a tab in the background
# pings at most once a minute, hence the long timeout) or when the last page
# said goodbye and no page pinged within BYE_GRACE seconds (navigating to
# another page pings again at once, so that never triggers).
HEARTBEAT_EVERY = 10
HEARTBEAT_TIMEOUT = 150
BYE_GRACE = 15
HEARTBEAT = {"ping": None, "bye": None}


@app.route("/ping")
def ping():
    HEARTBEAT["ping"] = time.monotonic()
    return ("", 204)


@app.route("/bye", methods=["POST"])
def bye():
    HEARTBEAT["bye"] = time.monotonic()
    return ("", 204)


def browser_gone(now, hb=None):
    """True when the watchdog should end the process (see HEARTBEAT).
    Never before the first ping - a start without a browser stays up."""
    hb = HEARTBEAT if hb is None else hb
    if hb["ping"] is None:
        return False
    if now - hb["ping"] > HEARTBEAT_TIMEOUT:
        return True
    return hb["bye"] is not None and hb["bye"] >= hb["ping"] and now - hb["bye"] > BYE_GRACE


def _watchdog():
    while True:
        time.sleep(5)
        if browser_gone(time.monotonic()):
            print("Browser geschlossen – funkSTONKS beendet sich.")
            os._exit(0)


def close_with_browser():
    """Setting close_with_browser ("1" by default)."""
    con = db()
    try:
        return get_setting(con, "close_with_browser", "1") == "1"
    finally:
        con.close()


# Routes that must be reachable even BEFORE setup is complete - otherwise one
# is stuck in an endless redirect.
SETUP_EXEMPT = {
    "setup_page",
    "settings_page",
    "help_page",
    "static",
    "sicherung_page",
    "sicherung_download",
    "ping",
    "bye",
}


@app.before_request
def require_setup():
    """On the very first start, lead through the setup first. Once it has been
    completed (or an existing database was migrated), nothing happens here
    any more."""
    if request.endpoint in SETUP_EXEMPT or request.endpoint is None:
        return None
    try:
        con = db()
        try:
            done = get_setting(con, "setup_done")
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not done:
        # Whoever lands on the start page at the very first start needs no
        # warning - only someone deliberately opening another page (tour 2026-10-14).
        if request.path != "/":
            flash("WARNUNG Erst die Einrichtung abschließen – danach sind alle Seiten erreichbar")
        return redirect(url_for("setup_page"))
    return None


def _repair_at_start():
    """Rows an older version imported with the whole booking text as partner
    (meinELBA via column mapping) get the extracted counterparty once at
    start-up - the user may never import that file again."""
    from .importers import repair_embedded_partners

    con = db()
    try:
        n = repair_embedded_partners(con)
        con.commit()
    finally:
        con.close()
    if n:
        print(f"{n} alte Buchungen bereinigt (Empfänger aus dem Buchungstext gelesen)")


def _auto_prices():
    """Background at startup: depot quotes once a day (quotes.maybe_refresh_prices)."""
    try:
        from .quotes import maybe_refresh_prices

        con = db()
        try:
            status, res = maybe_refresh_prices(con)
        finally:
            con.close()
        if status == "fetched":
            print(
                f"Depotkurse: {res['ok']} aktualisiert"
                + (f", ohne Kurs: {len(res['errors'])}" if res["errors"] else "")
            )
    except Exception as e:  # must never disturb startup
        print("Depotkurse nicht geholt:", str(e)[:120])


def main():
    """Start from run.bat / app.py: sync with GitHub (data up, code down -
    before the DB is opened, the sync may replace it), create the DB, open
    the browser, depot quotes in the background (once a day). If newer code
    arrived, the process starts itself again with the new files."""
    if startup_sync():
        print("Neuer Code von GitHub – funkSTONKS startet neu ...")
        raise SystemExit(restart_app(wait=True))
    init_db()
    load_secret_key()
    _repair_at_start()
    import threading
    import webbrowser

    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5010")).start()
    threading.Timer(3.0, _auto_prices).start()
    if close_with_browser():
        threading.Thread(target=_watchdog, daemon=True).start()
        print("funkSTONKS läuft auf http://127.0.0.1:5010  (Browserfenster schließen beendet die App)")
    else:
        print("funkSTONKS läuft auf http://127.0.0.1:5010  (Beenden: Strg+C / Fenster schließen)")
    app.run(host="127.0.0.1", port=5010, debug=False)
