"""Shared check harness for all suites under pytest.

check(name, cond, extra) collects instead of aborting right away - a suite
always runs to the end and reports ALL failed checks at once (like the old
home-grown mini framework did). finish() at the end of the suite turns them
into exactly one assertion with the list of failures. The OK/FAIL lines are
visible with `pytest -s`."""

_fails: list[str] = []
_count = [0]


def check(name, cond, extra=""):
    _count[0] += 1
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else "  -> " + str(extra)))
    if not cond:
        _fails.append(name + "  -> " + str(extra)[:300])


def finish():
    """At the end of each suite: print a summary and, on failures, raise ONE
    AssertionError with all names. Resets the collector so the next suite in
    the same process (xdist worker) starts clean."""
    fails, n = list(_fails), _count[0]
    _fails.clear()
    _count[0] = 0
    print(f"\n{n} checks, {len(fails)} FAIL" if fails else f"\nall {n} checks OK")
    assert not fails, f"{len(fails)} of {n} checks failed:\n" + "\n".join("  - " + f for f in fails)
