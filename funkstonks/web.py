"""Flask app, form helpers (form_int, BadInput handler), connection teardown,
template globals (inject_globals), setup guard, main()."""

import os
import secrets
import sqlite3

from flask import Flask, abort, flash, redirect, request, url_for

from .automations import health_checks
from .config import APP_VERSION, RES_DIR, THEME_KEYS, THEMES, BadInput
from .db import db, enabled_sources, get_setting, init_db, set_setting
from .share import is_dev_checkout
from .stats import todo_count

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


# Routes that must be reachable even BEFORE setup is complete - otherwise one
# is stuck in an endless redirect.
SETUP_EXEMPT = {"setup_page", "settings_page", "help_page", "static", "sicherung_page", "sicherung_download"}


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
    """Start from run.bat / app.py: create the DB, open the browser, depot
    quotes in the background (once a day)."""
    init_db()
    load_secret_key()
    import threading
    import webbrowser

    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5010")).start()
    threading.Timer(3.0, _auto_prices).start()
    print("funkSTONKS läuft auf http://127.0.0.1:5010  (Beenden: Strg+C / Fenster schließen)")
    app.run(host="127.0.0.1", port=5010, debug=False)
