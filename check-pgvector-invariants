#!/usr/bin/env python3
"""check_chunk_doc_invariants.py — wq#1776.

Enforces and alarms the two chunk/doc coherence invariants stated in
research/PGVECTOR_INVALIDATION_CONTRACTS.md §4.7 (pgvector `research_docs` /
`research_chunks`):

  I1 (chunk-hash coherence) — a chunk row's doc_hash must equal its parent
     document's current content_hash. A mismatch means the chunk's text was
     cut from a version of the document that no longer exists on disk; a
     retrieval that returns that chunk is serving a paragraph attributed to a
     document whose current content says something else.
  I2 (chunk/doc referential integrity) — a chunk row's doc_id must resolve to
     a live research_docs row. Measured live 2026-09-11: already 0, and
     structurally guaranteed rather than conventionally absent —
     `research_chunks_doc_id_fkey` already carries `ON DELETE CASCADE`
     (confirmed via `pg_constraint`, see README). This script re-checks that
     fact on every run and alarms if it is ever no longer true, since a
     constraint can be dropped by a future migration nobody told this ticket
     about.

Both counts (and the cascade flag) are printed on every run, per wq#1611:
"a zero nobody prints is indistinguishable from a check that never ran."
`--raise-aq` additionally persists the last-seen counts and raises one `aq`
row per calendar day for each invariant that grew, or for the cascade
disappearing — `aq` itself dedups on (source, key), so re-running the same
day is a no-op.

Read-only: every statement here is a SELECT/pg_catalog lookup, and the
connection sets `default_transaction_read_only = on` before running any of
them (belt-and-braces alongside the DB user's own grants).

Usage:
  python3 check_chunk_doc_invariants.py              # human report, exit 0
  python3 check_chunk_doc_invariants.py --json
  python3 check_chunk_doc_invariants.py --raise-aq    # what the daily cadence runs
  python3 check_chunk_doc_invariants.py --state PATH  # override state file location

Postgres unavailable is reported, not raised — this must never take a caller
(handoff verify, a daily scan) down with it (§4.3).
"""
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys

AQ = os.environ.get("AQ_BIN", "/home/lumen/bin/aq")
ENV_FILE = os.environ.get("LUMEN_AGENT_ENV", "/home/lumen/agent/.env")
STATE_PATH_DEFAULT = os.environ.get(
    "PGVECTOR_INVARIANTS_STATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)

INVARIANT_SQL = """
select
  (select count(*) from research_chunks c join research_docs d on d.id = c.doc_id
     where c.doc_hash is distinct from d.content_hash)                as i1_stale_chunks,
  (select count(distinct c.doc_id) from research_chunks c join research_docs d
     on d.id = c.doc_id
     where c.doc_hash is distinct from d.content_hash)                as i1_docs_affected,
  (select count(*) from research_chunks c left join research_docs d
     on d.id = c.doc_id where d.id is null)                           as i2_orphan_chunks
"""

FK_SQL = """
select conname, pg_get_constraintdef(oid)
  from pg_constraint
 where conrelid = 'research_chunks'::regclass
   and contype = 'f'
   and confrelid = 'research_docs'::regclass
"""


def _load_env(path=ENV_FILE):
    p = pathlib.Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def connect():
    import psycopg2
    _load_env()
    conn = psycopg2.connect(
        host=os.environ["LUMEN_PGVECTOR_HOST"],
        port=int(os.environ["LUMEN_PGVECTOR_PORT"]),
        dbname=os.environ["LUMEN_PGVECTOR_DB"],
        user=os.environ["LUMEN_PGVECTOR_USER"],
        password=os.environ["LUMEN_PGVECTOR_PASSWORD"],
    )
    cur = conn.cursor()
    cur.execute("SET default_transaction_read_only = on")
    conn.commit()
    return conn


def measure(conn):
    cur = conn.cursor()
    cur.execute(INVARIANT_SQL)
    i1_stale_chunks, i1_docs_affected, i2_orphan_chunks = cur.fetchone()
    cur.execute(FK_SQL)
    fks = [{"name": n, "def": d, "cascades": "ON DELETE CASCADE" in d}
           for n, d in cur.fetchall()]
    return {
        "i1_stale_chunks": i1_stale_chunks,
        "i1_docs_affected": i1_docs_affected,
        "i2_orphan_chunks": i2_orphan_chunks,
        "doc_id_fk_cascades": any(fk["cascades"] for fk in fks),
        "doc_id_fks": fks,
    }


def line(counts):
    return (f"pgvector chunk/doc invariants: "
            f"I1 stale-chunks={counts['i1_stale_chunks']} "
            f"(docs affected={counts['i1_docs_affected']}) · "
            f"I2 orphan-chunks={counts['i2_orphan_chunks']} · "
            f"doc_id FK cascades={counts['doc_id_fk_cascades']}")


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, counts, now):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"checked_at": now.isoformat(), **counts}, f, indent=2, sort_keys=True)


def raise_aq(prev, counts, now):
    """One aq row per day per condition that regressed; aq dedups on (source, key)."""
    if prev is None:
        return []
    raised = []
    day = now.strftime("%Y-%m-%d")

    for key, label in (("i1_stale_chunks", "I1 stale chunks"),
                        ("i2_orphan_chunks", "I2 orphan chunks")):
        delta = counts[key] - prev.get(key, 0)
        if delta > 0:
            aq_key = f"pgvector-{key}-{day}"
            title = f"{label} grew by {delta} (now {counts[key]}, was {prev.get(key, 0)})"
            raised.append(_aq_add(aq_key, title, line(counts)))

    if prev.get("doc_id_fk_cascades", True) and not counts["doc_id_fk_cascades"]:
        aq_key = f"pgvector-fk-cascade-missing-{day}"
        title = "research_chunks.doc_id FK no longer has ON DELETE CASCADE"
        raised.append(_aq_add(aq_key, title, line(counts)))

    return [r for r in raised if r]


def _aq_add(key, title, msg):
    try:
        subprocess.run([AQ, "add", "pgvector", key, title, msg],
                       capture_output=True, text=True, timeout=20, check=False)
        return key
    except (OSError, subprocess.SubprocessError) as e:
        print(f"# aq add failed: {e}", file=sys.stderr)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raise-aq", action="store_true",
                     help="persist state and raise an aq row on regression")
    ap.add_argument("--state", default=STATE_PATH_DEFAULT)
    args = ap.parse_args(argv)

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        conn = connect()
    except Exception as e:
        print(f"pgvector chunk/doc invariants: UNAVAILABLE ({e})")
        return 0

    try:
        counts = measure(conn)
    finally:
        conn.close()

    raised = []
    if args.raise_aq:
        prev = load_state(args.state)
        raised = raise_aq(prev, counts, now)
        save_state(args.state, counts, now)

    if args.json:
        print(json.dumps({**counts, "aq_raised": raised}, indent=2))
    else:
        print(line(counts))
        if raised:
            print(f"  aq rows raised: {', '.join(raised)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
