"""Money amounts. SQLite has no DECIMAL, so amounts are stored as REAL (euro)
in the database - but every rounding and every comparison in the code goes
through these helpers, never a hand-written `round(x, 2)` or
`abs(a - b) < 0.004` (review 2026-10-14). Every amount written is quantized
to cents (commercial rounding, not banker's rounding); sums over quantized
values differ at most in the nanocent range and are quantized again before
display/storage. Comparisons use EPS (below half a cent) so that 0.1 + 0.2 is
never "unequal to 0.3"."""

from decimal import ROUND_HALF_UP, Decimal

EPS = 0.004  # less than half a cent - amounts below this count as zero
_CENT = Decimal("0.01")


def q(x):
    """Rounded to cents (commercial: 0.005 -> 0.01), as float."""
    return float(Decimal(repr(float(x))).quantize(_CENT, rounding=ROUND_HALF_UP))


def cents(x):
    """Amount as whole cents (int) - for exact comparisons and sums."""
    return int((Decimal(repr(float(x))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def eq(a, b):
    """Equal to the cent?"""
    return abs(float(a) - float(b)) < EPS


def is_zero(x):
    return abs(float(x)) < EPS
