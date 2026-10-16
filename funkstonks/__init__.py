"""funkSTONKS (formerly MoneyFlow) - private expense tracking, fully local.

Package layout (layers from bottom to top, no import cycles):
  config -> db -> parsing/categories/rules -> importers/repay/automations/splitwise
  -> periods/stats/recurring/depot/demo/backup/share -> web -> views_*
The entry point stays app.py in the project folder (startup, re-exports for tests).
"""

from . import (  # noqa: F401 - register the routes
    views_dashboard,
    views_fixkosten,
    views_import,
    views_manage,
    views_splitwise,
    views_transactions,
)
from .web import app, main

__all__ = ["app", "main"]
