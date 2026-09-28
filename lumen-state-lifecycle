#!/usr/bin/env python3
"""wq#1632 — lifecycle predicate, sibling closure on decommission, predicate registry gate.

Gated write path for the minerva `state_facts` store (MINERVA_PIPELINE_MAP.md §4.3).
Deployed as `lumen-state-lifecycle`; registry file shared with wq#1613.

Env: MINERVA_DB (default /home/lumen/minerva/minerva.db)
     PREDICATE_REGISTRY (default /home/lumen/minerva/verify/registry.yaml)
"""

import argparse
import difflib
import fnmatch
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import yaml

MINERVA_DB = os.environ.get("MINERVA_DB", "/home/lumen/minerva/minerva.db")
REGISTRY_PATH = os.environ.get(
    "PREDICATE_REGISTRY", "/home/lumen/minerva/verify/registry.yaml"
)

LIFECYCLE = "lifecycle"
LIFECYCLE_ENUM = (
    "provisioning", "active", "degraded", "offline", "decommissioned", "retired",
)
CLOSING_STATES = ("decommissioned", "retired")

# Conservative backfill map, first match wins (DESIGN.md §1).
BACKFILL_MAP = (
    (("decommission",), "decommissioned"),
    (("retired",), "retired"),
    (("offline", "unreachable", "dead", "no remote shell"), "offline"),
    (("degraded", "flaky"), "degraded"),
    (("provisioning", "bring-up", "bringup"), "provisioning"),
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PredicateGateError(Exception):
    pass


class Registry:
    """Predicate registry (shared file with wq#1613). Unknown keys are tolerated."""

    def __init__(self, path):
        self.path = path
        with open(path) as f:
            self.doc = yaml.safe_load(f) or {}
        self.predicates = self.doc.setdefault("predicates", {})
        self.patterns = self.doc.get("patterns", [])

    def _pattern_entry(self, predicate):
        for pat in self.patterns:
            if fnmatch.fnmatch(predicate, pat.get("match", "")):
                return pat
        return None

    def lookup(self, predicate):
        if predicate in self.predicates:
            return self.predicates[predicate] or {}
        return self._pattern_entry(predicate)

    def gate(self, predicate, new_predicate_reason=None):
        """Refuse a predicate not in the registry unless a reason is given (DESIGN.md §3)."""
        if self.lookup(predicate) is not None:
            return
        if new_predicate_reason:
            self.predicates[predicate] = {
                "class": "config",
                "closes_on_decommission": False,
                "added_by": "state_lifecycle --new-predicate",
                "why": new_predicate_reason,
                "added_at": now_iso(),
            }
            self.save()
            return
        near = difflib.get_close_matches(
            predicate, list(self.predicates), n=3, cutoff=0.5
        )
        hint = f" Nearest existing: {', '.join(near)}." if near else ""
        raise PredicateGateError(
            f"predicate '{predicate}' is not in the registry ({self.path}).{hint}"
            " Supersede an existing predicate, or pass --new-predicate '<why>'"
            " to add a new axis deliberately."
        )

    def closes_on_decommission(self, predicate):
        entry = self.lookup(predicate)
        return bool(entry and entry.get("closes_on_decommission"))

    def predicate_class(self, predicate):
        entry = self.lookup(predicate)
        return (entry or {}).get("class")

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(self.doc, f, sort_keys=True)
        os.replace(tmp, self.path)


def open_fact(conn, subject, predicate):
    return conn.execute(
        "SELECT * FROM state_facts WHERE subject=? AND predicate=?"
        " AND valid_to IS NULL",
        (subject, predicate),
    ).fetchone()


def close_siblings(conn, registry, subject, lifecycle_fact_id, valid_from):
    """Close every open closes_on_decommission fact on subject (DESIGN.md §2).

    valid_to = the lifecycle fact's valid_from; identity/hardware rows stay open.
    """
    closed = []
    rows = conn.execute(
        "SELECT id, predicate FROM state_facts WHERE subject=?"
        " AND valid_to IS NULL AND predicate != ?",
        (subject, LIFECYCLE),
    ).fetchall()
    for row in rows:
        if not registry.closes_on_decommission(row["predicate"]):
            continue
        conn.execute(
            "UPDATE state_facts SET valid_to=?, invalidated_at=?, source_ref=?"
            " WHERE id=?",
            (valid_from, now_iso(), f"lifecycle-closure:{lifecycle_fact_id}", row["id"]),
        )
        closed.append(row["predicate"])
    return closed


def ingest(conn, registry, subject, predicate, value, source_ref,
           confidence=0.9, valid_from=None, fact_class=None,
           new_predicate_reason=None):
    """Bi-temporal ingest with registry gate, lifecycle enum, and sibling closure.

    Supersession stays keyed on (subject, predicate); same value reconfirms.
    Returns {action, id, closed:[...]}.
    """
    registry.gate(predicate, new_predicate_reason)
    if predicate == LIFECYCLE and value not in LIFECYCLE_ENUM:
        raise PredicateGateError(
            f"lifecycle is a closed enum {LIFECYCLE_ENUM}; got '{value}'"
        )
    valid_from = valid_from or now_iso()
    fact_class = fact_class or registry.predicate_class(predicate) or "config"
    ts = now_iso()

    cur = open_fact(conn, subject, predicate)
    if cur is not None and cur["value"] == value:
        conn.execute(
            "UPDATE state_facts SET last_confirmed_at=? WHERE id=?", (ts, cur["id"])
        )
        return {"action": "reconfirmed", "id": cur["id"], "closed": []}
    if cur is not None:
        conn.execute(
            "UPDATE state_facts SET valid_to=?, invalidated_at=? WHERE id=?",
            (valid_from, ts, cur["id"]),
        )
    new_id = conn.execute(
        "INSERT INTO state_facts (subject, predicate, value, valid_from,"
        " ingested_at, source_ref, confidence, fact_class, last_confirmed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (subject, predicate, value, valid_from, ts, source_ref, confidence,
         fact_class, ts),
    ).lastrowid

    closed = []
    if predicate == LIFECYCLE and value in CLOSING_STATES:
        closed = close_siblings(conn, registry, subject, new_id, valid_from)
    return {"action": "superseded" if cur else "inserted", "id": new_id,
            "closed": closed}


def map_prose(prose):
    low = (prose or "").lower()
    for needles, state in BACKFILL_MAP:
        if any(n in low for n in needles):
            return state
    return None


def backfill(conn, registry, apply=False):
    """Conservative lifecycle backfill from open status/role prose (DESIGN.md §1),
    then sibling closure for subjects already decommissioned/retired."""
    report = {"backfilled": {}, "ambiguous": [], "skipped_existing": [], "closed": {}}
    subjects = [
        r["subject"] for r in conn.execute(
            "SELECT DISTINCT subject FROM state_facts WHERE valid_to IS NULL"
            " AND predicate IN ('status','role')"
        )
    ]
    for subject in subjects:
        if open_fact(conn, subject, LIFECYCLE) is not None:
            report["skipped_existing"].append(subject)
            continue
        state, src = None, None
        for pred in ("status", "role"):
            row = open_fact(conn, subject, pred)
            if row is None:
                continue
            src = src or row
            mapped = map_prose(row["value"])
            if mapped:
                state, src = mapped, row
                break
        if state is None:
            state = "active"
            report["ambiguous"].append(subject)
        report["backfilled"][subject] = state
        if apply:
            ingest(conn, registry, subject, LIFECYCLE, state,
                   source_ref=f"lifecycle-backfill:wq1632 fact:{src['id']}",
                   confidence=0.6, valid_from=src["valid_from"],
                   fact_class="status")

    # Subjects whose lifecycle was already decommissioned/retired before this run
    # (nephthys): the ingest above never fired for them, so close siblings here.
    for row in conn.execute(
        "SELECT id, subject, valid_from FROM state_facts WHERE valid_to IS NULL"
        " AND predicate=? AND value IN (?,?)",
        (LIFECYCLE, *CLOSING_STATES),
    ).fetchall():
        if apply:
            closed = close_siblings(conn, registry, row["subject"], row["id"],
                                    row["valid_from"])
        else:
            closed = [
                r["predicate"] for r in conn.execute(
                    "SELECT predicate FROM state_facts WHERE subject=?"
                    " AND valid_to IS NULL AND predicate != ?",
                    (row["subject"], LIFECYCLE),
                ) if registry.closes_on_decommission(r["predicate"])
            ]
        if closed:
            report["closed"][row["subject"]] = sorted(set(
                report["closed"].get(row["subject"], []) + closed))
    return report


def close_on_up(conn, registry, subject, source_ref, observed_at=None):
    """wq#1608 hook (DESIGN.md §4): node observed up — close open status-class
    facts at the observation time and supersede lifecycle -> active."""
    observed_at = observed_at or now_iso()
    closed = []
    for row in conn.execute(
        "SELECT id, predicate FROM state_facts WHERE subject=?"
        " AND valid_to IS NULL AND predicate != ?",
        (subject, LIFECYCLE),
    ).fetchall():
        if registry.predicate_class(row["predicate"]) != "status":
            continue
        conn.execute(
            "UPDATE state_facts SET valid_to=?, invalidated_at=?, source_ref=?"
            " WHERE id=?",
            (observed_at, now_iso(), source_ref, row["id"]),
        )
        closed.append(row["predicate"])
    res = ingest(conn, registry, subject, LIFECYCLE, "active",
                 source_ref=source_ref, valid_from=observed_at,
                 fact_class="status")
    return {"closed": closed, "lifecycle": res}


def seed_registry(conn, registry):
    """Merge live DISTINCT predicates into the registry (deploy step; never
    overwrites an existing entry, so wq#1613 fields survive)."""
    added = []
    for row in conn.execute("SELECT DISTINCT predicate FROM state_facts"):
        pred = row["predicate"]
        if registry.lookup(pred) is not None:
            continue
        cls = conn.execute(
            "SELECT fact_class FROM state_facts WHERE predicate=?"
            " GROUP BY fact_class ORDER BY COUNT(*) DESC LIMIT 1",
            (pred,),
        ).fetchone()
        registry.predicates[pred] = {
            "class": (cls["fact_class"] if cls and cls["fact_class"] else "config"),
            "closes_on_decommission": False,
            "review": "wq1613",
        }
        added.append(pred)
    if added:
        registry.save()
    return added


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=MINERVA_DB)
    p.add_argument("--registry", default=REGISTRY_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="gated bi-temporal ingest")
    pi.add_argument("subject")
    pi.add_argument("predicate")
    pi.add_argument("value")
    pi.add_argument("--source-ref", required=True)
    pi.add_argument("--confidence", type=float, default=0.9)
    pi.add_argument("--valid-from")
    pi.add_argument("--fact-class")
    pi.add_argument("--new-predicate", metavar="WHY",
                    help="reason for introducing a predicate not in the registry")

    pb = sub.add_parser("backfill", help="conservative lifecycle backfill + closure")
    pb.add_argument("--apply", action="store_true", help="write (default: dry-run)")

    pu = sub.add_parser("close-on-up", help="wq#1608 hook: node observed up")
    pu.add_argument("subject")
    pu.add_argument("--source-ref", required=True)
    pu.add_argument("--observed-at")

    sub.add_parser("seed-registry", help="merge live predicates into the registry")

    args = p.parse_args(argv)
    registry = Registry(args.registry)
    conn = connect(args.db)
    try:
        if args.cmd == "ingest":
            out = ingest(conn, registry, args.subject, args.predicate, args.value,
                         source_ref=args.source_ref, confidence=args.confidence,
                         valid_from=args.valid_from, fact_class=args.fact_class,
                         new_predicate_reason=args.new_predicate)
        elif args.cmd == "backfill":
            out = backfill(conn, registry, apply=args.apply)
            out["mode"] = "apply" if args.apply else "dry-run"
        elif args.cmd == "close-on-up":
            out = close_on_up(conn, registry, args.subject, args.source_ref,
                              observed_at=args.observed_at)
        else:
            out = {"added": seed_registry(conn, registry)}
        conn.commit()
    except PredicateGateError as e:
        conn.rollback()
        print(json.dumps({"error": str(e)}))
        return 2
    finally:
        conn.close()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
