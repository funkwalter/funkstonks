"""pytest configuration: makes the project folder importable (import app as m),
forces UTF-8 on the console, and every suite gets its own temp folder via
fresh_db()/setup() in the helpers - the real moneyflow.db is never touched."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass
