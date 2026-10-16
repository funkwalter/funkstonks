"""Constants and defaults: paths (APP_DIR, RES_DIR, DB_PATH), default categories
and rules, system categories, Splitwise keyword list, initial setting values,
themes. No logic, no imports from the package."""

import os
import sys


def app_dir_for_executable(exe_path):
    """Folder where the built version creates its database: next to the EXE
    (Windows) or next to the .app bundle (macOS - sys.executable lives in
    funkSTONKS.app/Contents/MacOS/, nothing may be written there and the user
    would never find the file; until 2026-10-14 it would have ended up there).
    Bundle detection runs on the raw path (abspath would prepend a drive
    letter on Windows - the tests run there)."""
    parts = str(exe_path).replace(chr(92), "/").split("/")
    for i in range(len(parts) - 3):
        if parts[i].endswith(".app") and parts[i + 1] == "Contents" and parts[i + 2] == "MacOS":
            return "/".join(parts[:i]) or "/"
    return os.path.dirname(os.path.abspath(exe_path))


if getattr(sys, "frozen", False):
    # Built version (build.bat / build_mac.sh, PyInstaller): templates and
    # static live in the unpacked bundle, the database is created NEXT TO the
    # EXE/app - writing is allowed there and the user can find it again.
    APP_DIR = app_dir_for_executable(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project folder (one above the package)
    RES_DIR = APP_DIR
DB_PATH = os.path.join(APP_DIR, "moneyflow.db")

# Version of the stand-alone build: release.bat tags the GitHub release v<APP_VERSION>
# and refuses to overwrite an existing one - bump here (and in CHANGELOG.md) first.
APP_VERSION = "1.0.0"
DEFAULT_CATEGORIES = [
    # (name, color, kind)  kind: expense | income | transfer
    ("Wohnung", "#0F4C5C", "expense"),
    ("Lebensmittel", "#5F8B4C", "expense"),
    ("Restaurant/Ausgehen", "#8F6B3D", "expense"),
    ("Urlaub", "#2E86AB", "expense"),
    ("Lifestyle", "#B0713F", "expense"),
    ("Konsum", "#9B5B4C", "expense"),
    ("Abos", "#54627B", "expense"),
    ("Versicherung", "#3D5A6C", "expense"),
    ("Transport/Mobilität", "#6C7A3D", "expense"),
    ("Gesundheit", "#4C8B7A", "expense"),
    ("Haushalt", "#8B7A5C", "expense"),
    ("Technik", "#5C6E8B", "expense"),
    ("Geschenke", "#A05C7A", "expense"),
    ("Gebühren/Bank", "#777777", "expense"),
    ("Bargeld", "#999999", "expense"),
    ("Sonstiges", "#AAAAAA", "expense"),
    ("Gehalt", "#2F6B4F", "income"),
    ("Miete (Einnahme)", "#3F7B5F", "income"),
    ("Sonstige Einnahmen", "#4F8B6F", "income"),
    ("Investitionen", "#2E5C8A", "expense"),  # its own blue branch in the diagram
    ("Umbuchung", "#555555", "transfer"),
    ("PayPal-Verrechnung", "#666666", "transfer"),
    ("Splitwise-Ausgleich", "#606060", "transfer"),
    ("Kreditkarten-Verrechnung", "#676767", "transfer"),
    # Incoming payment that was linked to an expense (a friend pays back his
    # share) - the expense is reduced by the amount, the credit itself counts
    # nowhere (see link_repayment).
    ("Rückzahlung", "#6B6B6B", "transfer"),
]
# Categories the system itself needs (PayPal/card matching) - not deletable,
# silently recreated on demand (system_cat_id).
SYSTEM_CATS = ("PayPal-Verrechnung", "Splitwise-Ausgleich", "Kreditkarten-Verrechnung", "Rückzahlung")
# Splitwise category -> own category (substring, case-insensitive)
SPLITWISE_CAT_MAP = [
    # Description keywords that must override a wrong Splitwise category are
    # deliberately at the VERY TOP - the list is scanned in order, the first
    # hit wins. "Tabak" (tobacco) has sat under the Splitwise category
    # "Elektrizitaet" (electricity) since 2021 (chosen wrongly once, passed on
    # by Splitwise as a suggestion ever since, 81 rows affected) - without this
    # precedence it ends up as a housing fixed cost and disappears completely
    # from the evaluation. User request 2026-09-01: "tabak ist konsum und aus."
    # (tobacco is consumption, period).
    ("tabak", "Konsum"),
    ("auswärts", "Restaurant/Ausgehen"),
    ("restaurant", "Restaurant/Ausgehen"),
    ("alkohol", "Restaurant/Ausgehen"),
    ("lebensmittel", "Lebensmittel"),
    ("groceries", "Lebensmittel"),
    ("miete", "Wohnung"),
    ("nebenkosten", "Wohnung"),
    ("strom", "Wohnung"),
    ("elektrizität", "Wohnung"),
    ("heizung", "Wohnung"),
    ("gas", "Wohnung"),
    ("wasser", "Wohnung"),
    ("müll", "Wohnung"),
    ("fernsehen/telefon/internet", "Abos"),
    ("dienstleistungen", "Abos"),
    ("haushalt", "Haushalt"),
    ("möbel", "Haushalt"),
    ("reinigung", "Haushalt"),
    ("reise", "Urlaub"),
    ("hotel", "Urlaub"),
    ("flug", "Urlaub"),
    ("unterhaltung", "Lifestyle"),
    ("filme", "Lifestyle"),
    ("spiele", "Lifestyle"),
    ("musik", "Lifestyle"),
    ("sport", "Lifestyle"),
    ("kleidung", "Lifestyle"),
    ("bildung", "Lifestyle"),
    ("transport", "Transport/Mobilität"),
    ("taxi", "Transport/Mobilität"),
    ("benzin", "Transport/Mobilität"),
    ("treibstoff", "Transport/Mobilität"),
    ("parken", "Transport/Mobilität"),
    ("auto", "Transport/Mobilität"),
    ("bus/zug", "Transport/Mobilität"),
    ("fahrrad", "Transport/Mobilität"),
    ("geschenk", "Geschenke"),
    ("medizin", "Gesundheit"),
    ("versicherung", "Versicherung"),
    ("haustiere", "Sonstiges"),
]
# Predefined auto rules (substring on partner+text), created on first start
DEFAULT_RULES = [
    ("BANKOMAT", "Bargeld"),
    ("BILLA", "Lebensmittel"),
    ("SPAR DANKT", "Lebensmittel"),
    ("HOFER DANKT", "Lebensmittel"),
    ("LIDL", "Lebensmittel"),
    ("PENNY", "Lebensmittel"),
    ("EUROSPAR", "Lebensmittel"),
    ("INTERSPAR", "Lebensmittel"),
    ("DENNS", "Lebensmittel"),
    ("Netflix", "Abos"),
    ("Spotify", "Abos"),
    ("Disney", "Abos"),
    ("Amazon Prime", "Abos"),
    ("YouTube", "Abos"),
    ("Wiener Linien", "Transport/Mobilität"),
    ("OEBB", "Transport/Mobilität"),
    ("ÖBB", "Transport/Mobilität"),
    ("Uber", "Transport/Mobilität"),
    ("WESTBAHN", "Transport/Mobilität"),
    ("Bolt", "Transport/Mobilität"),
    ("Shell", "Transport/Mobilität"),
    ("OMV", "Transport/Mobilität"),
    ("BP ", "Transport/Mobilität"),
    ("JET ", "Transport/Mobilität"),
    ("UNIQA", "Versicherung"),
    ("Wiener Städtische", "Versicherung"),
    ("Generali", "Versicherung"),
    ("Allianz", "Versicherung"),
    ("APOTHEKE", "Gesundheit"),
    ("DM FIL", "Konsum"),
    ("BIPA", "Konsum"),
    ("MUELLER", "Konsum"),
    ("IKEA", "Haushalt"),
    # Statement rows of the bank itself (interest, quarterly closing, account
    # fees) - cent amounts that would otherwise land in the review every
    # quarter (user request 2026-10-10).
    ("Habenzinsen", "Gebühren/Bank"),
    ("Sollzinsen", "Gebühren/Bank"),
    ("Abschlussbuchung", "Gebühren/Bank"),
    ("Kontoführung", "Gebühren/Bank"),
    ("Kontofuehrung", "Gebühren/Bank"),
    ("Verwahrentgelt", "Gebühren/Bank"),
]


class BadInput(ValueError):
    """Invalid user input - shown as a hint, not as a 500."""


# Everything that used to be a constant in the code now lives in the `settings`
# table and can be changed under /einstellungen (user request 2026-09-11:
# "es sollte nichts statisches sein, was man nicht auch aendern kann" -
# nothing should be static that cannot also be changed).
# The values here are pure STARTING VALUES for a fresh database - an existing
# DB gets its previous values written in by migrate_settings(), so nothing
# changes for it.
SETTING_DEFAULTS = {
    "sources": '["bank"]',  # active data sources: bank | card | paypal | splitwise
    "splitwise_name": "",  # own name as column header in the Splitwise export
    "data_cutoff": "",  # ISO date from which on things count ("" = from the first booking)
    "shared_name": "",  # name of the person fixed costs are shared with
    "theme": "auto",  # auto | light | dark
    "setup_done": "",  # "1" once the setup has been completed
    "urlaub_excluded_groups": "[]",  # Splitwise groups without the "Als Urlaub" (as vacation) switch
    "fixkosten_cats": '["Wohnung","Abos"]',  # count as fixed costs in the Sankey
    "fixkosten_privat_cats": '["Abos","Investitionen"]',  # allowed under "Laufende Kosten" (recurring costs)
    "income_category": "Gehalt",  # which income category is the salary
    "fixed_cost_group": "",  # Splitwise group in which the shared fixed costs run
    "share_forbidden": "[]",  # terms (names etc.) that must never end up in the public repo
    "public_repo_url": "",  # the public repo publish_share() force-pushes to; empty = no publishing
    "auto_prices": "1",  # fetch depot prices once a day at startup (quotes.maybe_refresh_prices)
}
TX_LIST_LIMIT = 2500  # rows in "Alle Zahlungen" (all payments) - a whole year fits
# Categories in which a regular rhythm says NOTHING about a contract - spending
# the same amount at the supermarket every month does not make it a
# subscription. The TOP-LEVEL category is checked so that subcategories
# ("Fortgehen" under "Restaurant/Ausgehen") are caught as well.
VARIABLE_CATS = {"Lebensmittel", "Restaurant/Ausgehen", "Bargeld", "Konsum"}
# Color schemes (data-theme on <html>, variables in static/style.css). "auto"
# follows the system setting. User request 2026-10-10: "verschiedene
# farbthemes die professioneller aussehen" (several color themes that look
# more professional).
THEMES = [
    ("auto", "Automatisch (wie das System)"),
    ("light", "Hell"),
    ("slate", "Slate – hell, dunkle Leiste"),
    ("sand", "Sand – hell, warm"),
    ("forest", "Forest – hell, grün"),
    ("dark", "Dunkel"),
    ("midnight", "Midnight – dunkelblau"),
    ("ocean", "Ocean – dunkel, petrol"),
    ("graphite", "Graphite – dunkel, neutral"),
]
THEME_KEYS = {k for k, _ in THEMES}
ALL_SOURCES = [
    (
        "bank",
        "Bankkonto",
        "Der Kontoauszug deines Girokontos als CSV. Die Grundlage – "
        "ohne das ergibt die App keinen Sinn, deshalb nicht abschaltbar.",
    ),
    (
        "card",
        "Kreditkarte",
        "Ein separater Kartenkonto-Export. Nur nötig, wenn deine "
        "Karte NICHT schon im Girokonto-Auszug einzeln auftaucht, sondern als "
        "monatliche Sammelabbuchung.",
    ),
    (
        "paypal",
        "PayPal",
        "Der PayPal-Aktivitätsexport. Nur nötig, wenn du über PayPal "
        "zahlst – sonst siehst du am Konto nur „PayPal“ und weißt nicht, wofür.",
    ),
    (
        "splitwise",
        "Splitwise",
        "Gruppenexporte aus Splitwise. Nur nötig, wenn du "
        "Ausgaben mit anderen teilst und wissen willst, was davon wirklich deins ist.",
    ),
]
