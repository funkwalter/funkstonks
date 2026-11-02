"""Payments entered by hand (user 2026-11-02): an income or expense that is
in no export - cash, a second account, a side income. Source 'manual', its
own hash (never an import hash), so no import ever creates, touches or
removes such a row; cleanup_before_start skips the source as well."""

import uuid
from datetime import date

from .config import BadInput
from .money import is_zero, q
from .parsing import parse_date
from .rules import tx_hash

MANUAL_SOURCE = "manual"


def add_manual_tx(con, booking_date, partner, description, amount, category_id=None):
    """Insert one hand-entered row and return its id. `amount` is signed
    (negative = expense); the date may come in any of the import formats.
    Without a category the row lands in the review queue like an imported
    one. Rules are NOT applied - the user chose the category just now."""
    day = parse_date(booking_date) if booking_date else date.today().isoformat()
    if not day:
        raise BadInput("Datum nicht lesbar (z.B. 2026-11-02 oder 02.11.2026).")
    if amount is None or is_zero(amount):
        raise BadInput("Betrag fehlt oder ist 0.")
    partner = (partner or "").strip()
    description = (description or "").strip()
    if not partner and not description:
        raise BadInput("Partner oder Beschreibung angeben.")
    cur = con.execute(
        "INSERT INTO transactions(source,booking_date,partner,description,amount,category_id,status,raw_hash,extra)"
        " VALUES(?,?,?,?,?,?,'active',?,'{}')",
        (MANUAL_SOURCE, day, partner, description, q(amount), category_id, tx_hash(MANUAL_SOURCE, uuid.uuid4().hex)),
    )
    return cur.lastrowid


def delete_manual_tx(con, tx_id):
    """Remove a hand-entered row for good (imported rows are only excluded,
    never deleted - the next import would bring them back). Splits and
    matches go with it (ON DELETE CASCADE)."""
    row = con.execute("SELECT source FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise BadInput("Diese Zahlung gibt es nicht (mehr).")
    if row["source"] != MANUAL_SOURCE:
        raise BadInput("Nur von Hand erfasste Zahlungen lassen sich löschen – importierte mit ✕ ausschließen.")
    con.execute("DELETE FROM matches WHERE bank_tx=? OR other_tx=?", (tx_id, tx_id))
    con.execute("DELETE FROM splits WHERE tx_id=?", (tx_id,))
    con.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
