r"""Run all suites - since 2026-10-14 through pytest (parallel with xdist).

    .venv\Scripts\python.exe tests\run_all.py           # everything, in parallel
    .venv\Scripts\python.exe tests\run_all.py netting    # only suites whose name contains this (pytest -k)
    .venv\Scripts\python.exe tests\run_all.py -j 1       # one after another (e.g. for debugging)
    .venv\Scripts\python.exe -m pytest tests -n auto     # the same, directly

Each suite is a pytest function test_<name>() with its own temp database
(fresh_db()/setup() redirect m.DB_PATH BEFORE init_db()) - the real moneyflow.db
is never touched. check() collects, finish() makes ONE assertion at the end with
all failures (tests/_harness.py). Exit code 1 as soon as anything is red.
TEST_JOBS or -j sets the number of parallel workers (default: CPU cores, max. 8).
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
args = sys.argv[1:]
jobs = None
if "-j" in args:
    i = args.index("-j")
    jobs = int(args[i + 1]) if i + 1 < len(args) else 1
    del args[i : i + 2]
pattern = args[0] if args else ""
if jobs is None:
    jobs = int(os.environ.get("TEST_JOBS") or min(os.cpu_count() or 2, 8))
cmd = [sys.executable, "-m", "pytest", HERE, "-q", "-p", "no:cacheprovider", "-n", str(max(1, jobs))]
if pattern:
    cmd += ["-k", pattern]
sys.exit(subprocess.call(cmd))
