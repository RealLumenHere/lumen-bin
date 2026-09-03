#!/usr/bin/env python3
"""wq#1331 — add the `needs` column to an existing work-queue DB.

WHY A SCRIPT (the wq#1158 pattern) AND NOT ANOTHER LINE IN `wq init()`
The column itself is inert storage — nothing reads it until something writes it — but
the moment it exists, foreman's resolver starts honouring per-ticket needs:repo/
needs:dir tags (WORKER_EXEC_CAPABILITY_GAP.md §2.3/§3.2, claude-foreman
orchestrator/router.py + harness/fworker.py) and the spawn-time worktree assertion
goes from log-only-capable to the live routing input. Turning that on is an
operator's decision taken once, with a backup behind it — not a consequence of
whichever agent happens to run `wq list` first after a deploy. wq FEATURE-DETECTS
the column: `--needs` on add/edit is REFUSED (with a pointer here) until this has
run, and show simply doesn't have the field.

SAFETY — same contract as migrations/wq1158_lease_fence.py:
  * consistent backup (VACUUM INTO — the DB runs WAL; cp can miss -wal pages) first;
  * --db REQUIRED, no default;
  * writes nothing without --yes (bare invocation is a report);
  * idempotent — the ALTER is guarded, a second run is a no-op;
  * reversible — --rollback drops the column.

USAGE
    python3 migrations/wq1331_needs.py --db /path/to/queue.db            # report only
    python3 migrations/wq1331_needs.py --db /path/to/queue.db --yes      # apply
    python3 migrations/wq1331_needs.py --db /path/to/queue.db --rollback --yes

EXIT CODES
    0 applied / already applied / clean report      2 usage      3 environment unfit
"""
import argparse
import datetime
import os
import sqlite3
import sys

# ALTER TABLE ... DROP COLUMN landed in 3.35; without it --rollback cannot honour its name.
MIN_SQLITE = (3, 35, 0)
COLUMN = ("needs", "TEXT DEFAULT ''",
          "wq#1331: capability tags (§2.3 closed set), e.g. repo(/path), dir(/path)")


def cols_of(c):
    return {r[1] for r in c.execute("PRAGMA table_info(tasks)")}


def backup(db, tag):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{db}.bak-wq1331-{tag}-{stamp}"
    path, n = base, 0
    while os.path.exists(path):
        n += 1
        path = f"{base}.{n}"
        if n > 99:
            print(f"REFUSED: cannot find a free backup name next to {base}")
            sys.exit(2)
    c = sqlite3.connect(db, timeout=30)
    try:
        c.execute("VACUUM INTO ?", (path,))
    finally:
        c.close()
    print(f"backup: {path} ({os.path.getsize(path)} bytes)")
    return path


def report(c):
    name = COLUMN[0]
    have = name in cols_of(c)
    total = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    print(f"tasks: {total} rows")
    print(f"column tasks.{name}: {'present' if have else 'MISSING'}")
    if have:
        tagged = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE needs IS NOT NULL AND needs != ''"
        ).fetchone()[0]
        print(f"rows with needs tags: {tagged}")
    return have


def apply(db, yes):
    c = sqlite3.connect(db, timeout=30)
    have = report(c)
    if have:
        print("ALREADY APPLIED — nothing to do.")
        c.close()
        return
    if not yes:
        print("\nreport only. Re-run with --yes to apply.")
        c.close()
        return
    c.close()
    backup(db, "preapply")
    c = sqlite3.connect(db, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        name, decl, why = COLUMN
        if name not in cols_of(c):
            c.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
            print(f"added  tasks.{name} {decl}  -- {why}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        c.close()
        raise
    if COLUMN[0] not in cols_of(c):
        print("VERIFY FAILED: column still missing")
        sys.exit(1)
    print("verify: column present")
    c.close()
    print("APPLIED.")


def rollback(db, yes):
    name = COLUMN[0]
    c = sqlite3.connect(db, timeout=30)
    present = name in cols_of(c)
    c.close()
    if not present:
        print("NOTHING TO ROLL BACK — tasks.needs is not present.")
        return
    print(f"would drop column: tasks.{name}")
    if not yes:
        print("\nreport only. Re-run with --yes to roll back.")
        return
    backup(db, "prerollback")
    c = sqlite3.connect(db, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        c.close()
        raise
    c.close()
    print("ROLLED BACK.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to the queue DB (no default, on purpose)")
    ap.add_argument("--yes", action="store_true", help="actually write (bare run reports only)")
    ap.add_argument("--rollback", action="store_true", help="drop the column instead")
    a = ap.parse_args()
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        print(f"ENVIRONMENT UNFIT: sqlite {sqlite3.sqlite_version} < "
              f"{'.'.join(map(str, MIN_SQLITE))} (need DROP COLUMN for --rollback)")
        sys.exit(3)
    if not os.path.exists(a.db):
        print(f"ENVIRONMENT UNFIT: no such DB: {a.db}")
        sys.exit(3)
    if a.rollback:
        rollback(a.db, a.yes)
    else:
        apply(a.db, a.yes)


if __name__ == "__main__":
    main()
