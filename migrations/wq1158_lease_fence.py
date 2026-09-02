#!/usr/bin/env python3
"""wq#1158 — add the lease/fence columns to an existing work-queue DB.

WHY THIS IS A SCRIPT AND NOT ANOTHER LINE IN `wq init()`
Every other column on `tasks` was added by init() on the next invocation of wq, and that is
right for a column that is inert storage: nothing reads it until something writes it. These
four are not inert. The moment they exist, `wq claim` switches to the fenced write path and
the meaning of every in-flight claim on a queue that five live sessions are holding tickets
in changes underneath them. That is an operator's decision, taken once, with a backup behind
it — not a consequence of whichever agent happens to run `wq list` first after a deploy.

So: wq FEATURE-DETECTS the columns and behaves exactly as it did before if they are absent.
This script is the only thing that creates them, and the deploy order is
    merge the code  ->  run this  ->  the hardening turns on.

SAFETY
  * takes a consistent backup (VACUUM INTO) BEFORE touching anything, every run;
  * --db is REQUIRED and has no default, so nobody points it at the live queue by omission;
  * writes nothing without --yes (bare invocation is a report);
  * idempotent — every ALTER and CREATE is guarded, a second run is a no-op;
  * reversible — --rollback drops the four columns and the indexes.

USAGE
    python3 migrations/wq1158_lease_fence.py --db /path/to/queue.db            # report only
    python3 migrations/wq1158_lease_fence.py --db /path/to/queue.db --yes      # apply
    python3 migrations/wq1158_lease_fence.py --db /path/to/queue.db --rollback --yes

EXIT CODES
    0 applied / already applied / clean report      2 usage      3 environment unfit
"""
import argparse
import datetime
import os
import sqlite3
import sys

FMT = "%Y-%m-%d %H:%M:%S"
# ALTER TABLE ... DROP COLUMN landed in 3.35; without it --rollback cannot honour its name.
MIN_SQLITE = (3, 35, 0)
COLUMNS = (
    # name, type + default, why
    ("lease_owner",      "TEXT",              "who the live lease belongs to, alongside claimed_by"),
    ("lease_expires_at", "INTEGER",           "epoch mirror of lease_expires, for the atomic claim predicate"),
    ("fence",            "INTEGER DEFAULT 0", "monotonic per-ticket claim counter — the zombie guard"),
    ("attempt",          "INTEGER DEFAULT 0", "how many times this ticket has been claimed"),
)
INDEXES = (
    ("idx_tasks_claimable", "CREATE INDEX IF NOT EXISTS idx_tasks_claimable "
                            "ON tasks(lane, status, pri, id)"),
    ("idx_tasks_lease_at",  "CREATE INDEX IF NOT EXISTS idx_tasks_lease_at "
                            "ON tasks(status, lease_expires_at)"),
)


def sqlite_ok():
    return tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:3]) >= MIN_SQLITE


def cols_of(c, table="tasks"):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def indexes_of(c):
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'")}


def backup(db, tag):
    """VACUUM INTO, not a file copy: the DB runs in WAL mode, so a cp of the .db alone can
    miss committed pages that are still in the -wal. This writes one consistent file."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{db}.bak-wq1158-{tag}-{stamp}"
    # The stamp is second-resolution and two runs can land inside one second (apply straight
    # after a rollback). Never overwrite a backup to resolve that — take the next name.
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


def epoch_of(ts):
    try:
        return int(datetime.datetime.strptime(ts, FMT)
                   .replace(tzinfo=datetime.timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def report(c):
    have = cols_of(c)
    missing = [n for n, _t, _w in COLUMNS if n not in have]
    present = [n for n, _t, _w in COLUMNS if n in have]
    idx_missing = [n for n, _sql in INDEXES if n not in indexes_of(c)]
    total = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    claimed = c.execute("SELECT COUNT(*) FROM tasks WHERE status='claimed'").fetchone()[0]
    everclaimed = c.execute(
        "SELECT COUNT(*) FROM tasks WHERE claimed_at IS NOT NULL").fetchone()[0]
    print(f"tasks: {total} rows, {claimed} currently claimed, {everclaimed} ever claimed")
    print(f"columns present : {', '.join(present) or 'none'}")
    print(f"columns missing : {', '.join(missing) or 'none'}")
    print(f"indexes missing : {', '.join(idx_missing) or 'none'}")
    return missing, idx_missing, claimed


def apply(db, yes):
    c = sqlite3.connect(db, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    missing, idx_missing, claimed = report(c)
    if not missing and not idx_missing:
        print("ALREADY APPLIED — nothing to do.")
        c.close()
        return
    if not yes:
        print("\nreport only. Re-run with --yes to apply:")
        for n in missing:
            print(f"  ALTER TABLE tasks ADD COLUMN {n} ...")
        for n in idx_missing:
            print(f"  CREATE INDEX {n} ...")
        print(f"  backfill lease_owner/lease_expires_at on {claimed} claimed row(s)")
        c.close()
        return
    c.close()
    backup(db, "pre")

    c = sqlite3.connect(db, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        have = cols_of(c)
        for name, decl, why in COLUMNS:
            if name in have:
                continue
            c.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
            print(f"added  tasks.{name} {decl}  -- {why}")
        # Backfill. Safe defaults only, and NOTHING is invented:
        #  * lease_owner mirrors the holder wq already recorded, and ONLY on a live claim — a
        #    done ticket has no lease, and carrying its last holder there forever would make
        #    lease_owner mean two different things depending on the row;
        #  * lease_expires_at is computed from the lease already on the row — never a fresh
        #    clock reading, which would silently EXTEND a lease that had already run out;
        #  * fence starts at 0 for every row, so the next claim mints 1. A pre-migration
        #    holder has no fence to present; --fence is opt-in, so those claims keep working
        #    exactly as they do today (see MIGRATION.md on the one-generation window);
        #  * attempt is reconstructed from what the row already knows — one claim, plus one
        #    per recorded reopen (wq#841's reopened_count) — rather than guessed at.
        c.execute("UPDATE tasks SET lease_owner=claimed_by "
                  "WHERE lease_owner IS NULL AND claimed_by IS NOT NULL AND status='claimed'")
        rows = c.execute("SELECT id, lease_expires FROM tasks "
                         "WHERE lease_expires IS NOT NULL AND lease_expires_at IS NULL").fetchall()
        for tid, exp in rows:
            e = epoch_of(exp)
            if e is not None:
                c.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (e, tid))
        c.execute("UPDATE tasks SET fence=0 WHERE fence IS NULL")
        c.execute("UPDATE tasks SET attempt=1+COALESCE(reopened_count,0) "
                  "WHERE (attempt IS NULL OR attempt=0) AND claimed_at IS NOT NULL")
        c.execute("UPDATE tasks SET attempt=0 WHERE attempt IS NULL")
        print(f"backfilled: lease_expires_at on {len(rows)} row(s), "
              f"lease_owner/fence/attempt across the table")
        for name, sql in INDEXES:
            c.execute(sql)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        c.close()
        raise
    verify(c)
    c.close()
    print("APPLIED.")


def verify(c):
    missing = [n for n, _t, _w in COLUMNS if n not in cols_of(c)]
    if missing:
        print(f"VERIFY FAILED: still missing {missing}")
        sys.exit(1)
    bad = c.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='claimed' AND lease_expires IS NOT NULL "
        "AND lease_expires_at IS NULL").fetchone()[0]
    if bad:
        print(f"VERIFY FAILED: {bad} claimed row(s) have a lease_expires with no epoch mirror")
        sys.exit(1)
    nulls = c.execute("SELECT COUNT(*) FROM tasks WHERE fence IS NULL OR attempt IS NULL").fetchone()[0]
    if nulls:
        print(f"VERIFY FAILED: {nulls} row(s) with NULL fence/attempt")
        sys.exit(1)
    print("verify: columns present, mirrors consistent, no NULL fence/attempt")


def rollback(db, yes):
    c = sqlite3.connect(db, timeout=30)
    present = [n for n, _t, _w in COLUMNS if n in cols_of(c)]
    idx_present = [n for n, _sql in INDEXES if n in indexes_of(c)]
    c.close()
    if not present and not idx_present:
        print("NOTHING TO ROLL BACK — wq#1158 columns are not present.")
        return
    print(f"would drop columns: {', '.join(present) or 'none'}")
    print(f"would drop indexes: {', '.join(idx_present) or 'none'}")
    if not yes:
        print("\nreport only. Re-run with --yes to roll back.")
        return
    backup(db, "prerollback")
    c = sqlite3.connect(db, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        # Indexes first: idx_tasks_lease_at names lease_expires_at, and SQLite refuses to drop
        # a column an index still references.
        for name in idx_present:
            c.execute(f"DROP INDEX IF EXISTS {name}")
        for name in present:
            c.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
            print(f"dropped tasks.{name}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        c.close()
        raise
    c.close()
    print("ROLLED BACK. wq reverts to its pre-wq#1158 behaviour on the next invocation.")


def main():
    ap = argparse.ArgumentParser(description="wq#1158 lease/fence migration")
    ap.add_argument("--db", required=True,
                    help="path to the queue DB. REQUIRED and deliberately has no default — "
                         "the live queue is never a fallback.")
    ap.add_argument("--yes", action="store_true", help="actually write (default: report only)")
    ap.add_argument("--rollback", action="store_true", help="drop the wq#1158 columns/indexes")
    a = ap.parse_args()
    if not sqlite_ok():
        print(f"REFUSED: sqlite {sqlite3.sqlite_version} < "
              f"{'.'.join(str(x) for x in MIN_SQLITE)} — ALTER TABLE DROP COLUMN is unavailable, "
              f"so --rollback could not honour its name. Upgrade before migrating.")
        sys.exit(3)
    if not os.path.exists(a.db):
        print(f"REFUSED: no such DB: {a.db}")
        sys.exit(2)
    c = sqlite3.connect(a.db, timeout=30)
    has_tasks = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()[0]
    c.close()
    if not has_tasks:
        print(f"REFUSED: {a.db} has no `tasks` table — this is not a wq queue DB.")
        sys.exit(2)
    print(f"db: {a.db}  (sqlite {sqlite3.sqlite_version})")
    if a.rollback:
        rollback(a.db, a.yes)
    else:
        apply(a.db, a.yes)


if __name__ == "__main__":
    main()
