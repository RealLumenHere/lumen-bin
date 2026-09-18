#!/usr/bin/env python3
"""lumen_verify.py — wq#1613.

Verifier registry + sweep (MINERVA_STALENESS_REVIEW.md §2.2, §3.3, §3.4). No
probe existed for `lan_ip`, `ssh_user`, `role`, `status`, `hardware`, `ram`,
`storage`, `power_plug`, `kernel`, `os`, `*_version` or any `*_status`
predicate; the oldest status-class facts sat 67/67/61/61/58 days unverified.

Seven seed probes wrap tools that already exist (tailscale, fleet-inv,
systemctl, lsblk+mdstat+dmidecode, uname/pkg, plug-ctl, HTTP). A fact is due
when `evidence_age > COALESCE(fact.verify_interval_days, registry predicate
interval, class default)` (status 3d, config 14d, identity 90d). Probes run
batched per node, never per fact.

Reconciliation contract (§3.2, §3.4), owned here because wq#1608's
reconfirm-on-match reconciler had not landed at write time:
  match     -> reconfirmed: bump last_confirmed_at, clear contradicted_at.
  mismatch  -> superseded: close the old fact, open a new observed one --
               UNLESS the open fact is `frank_stated`, which is never
               auto-superseded. That case writes a `fact_verifications` row
               with outcome=unknown and files an aq row for a human.
  error/absent -> unknown. Never mutates the fact (fail-open).

  lumen_verify.py sweep [--db PATH] [--registry PATH] [--dry-run] [--json]
  lumen_verify.py sweep --now ISO8601   # deterministic for tests
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import yaml

MINERVA_DB = os.environ.get("MINERVA_DB", "/home/lumen/minerva/minerva.db")
REGISTRY_PATH = os.environ.get(
    "PREDICATE_REGISTRY", "/home/lumen/minerva/verify/registry.yaml"
)
AQ = "/home/lumen/bin/aq"
WQ = "/home/lumen/bin/wq"

CLASS_DEFAULTS = {"status": 3, "config": 14, "identity": 90}

MIGRATION_SQL = (
    ("state_facts", "source_kind", "TEXT"),
    ("state_facts", "verify_recipe", "TEXT"),
    ("state_facts", "verify_interval_days", "REAL"),
    ("state_facts", "contradicted_at", "TEXT"),
    ("state_facts", "derived_from", "TEXT"),
)

FACT_VERIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS fact_verifications (
  id TEXT PRIMARY KEY, fact_id TEXT NOT NULL, subject TEXT, predicate TEXT,
  probe TEXT NOT NULL, observed TEXT, outcome TEXT NOT NULL,
  ts TEXT NOT NULL, detail TEXT
);
"""
FACT_VERIFICATIONS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fv_fact ON fact_verifications(fact_id, ts);"
)

FRANK_STATED = "frank_stated"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 255, "", str(e)


# ------------------------------------------------------------------ registry

class Registry:
    """Predicate + probe registry (shared predicate list with wq#1632)."""

    def __init__(self, path):
        self.path = path
        with open(path) as f:
            self.doc = yaml.safe_load(f) or {}
        self.predicates = self.doc.get("predicates", {})
        self.patterns = self.doc.get("patterns", [])
        self.probes = self.doc.get("probes", {})
        self.class_defaults = self.doc.get("class_defaults", CLASS_DEFAULTS)

    def _entry(self, predicate):
        if predicate in self.predicates:
            return self.predicates[predicate] or {}
        for pat in self.patterns:
            if re.fullmatch(pat.get("match", "").replace("*", ".*"), predicate):
                return pat
        return {}

    def probe_id(self, predicate):
        return self._entry(predicate).get("probe")

    def predicate_class(self, predicate):
        return self._entry(predicate).get("class")

    def interval_days(self, predicate):
        entry = self._entry(predicate)
        if entry.get("verify_interval_days") is not None:
            return entry["verify_interval_days"]
        return self.class_defaults.get(entry.get("class"))

    def source_kind_default(self, predicate):
        return self._entry(predicate).get("source_kind")


# -------------------------------------------------------------------- schema

def migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(state_facts)")}
    for table, col, coltype in MIGRATION_SQL:
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.execute(FACT_VERIFICATIONS_DDL)
    conn.execute(FACT_VERIFICATIONS_IDX)
    conn.commit()


# ---------------------------------------------------------------- selection

def evidence_age_days(fact, now):
    basis = fact["last_confirmed_at"] or fact["ingested_at"] or fact["valid_from"]
    if not basis:
        return None
    age = now - datetime.fromisoformat(basis)
    return age.total_seconds() / 86400.0


def due_facts(conn, registry, now):
    """Open facts whose evidence_age exceeds their resolved interval, and
    which have a probe assigned. Grouped by (probe_id, subject) for batching."""
    batches = {}
    for row in conn.execute(
        "SELECT * FROM state_facts WHERE valid_to IS NULL"
    ):
        fact = dict(row)
        probe_id = registry.probe_id(fact["predicate"])
        if not probe_id or probe_id not in PROBES:
            continue
        interval = fact.get("verify_interval_days")
        if interval is None:
            interval = registry.interval_days(fact["predicate"])
        if interval is None:
            continue
        age = evidence_age_days(fact, now)
        if age is None or age <= interval:
            continue
        batches.setdefault((probe_id, fact["subject"]), []).append(fact)
    return batches


# -------------------------------------------------------------------- probes
#
# Each probe takes (subject, [fact dicts]) and returns
# {(subject, predicate): (observed_value_or_None, error_or_None)}.
# A raised exception is caught by the caller and treated as an error for
# every fact in the batch -- fail-open, per probe.

def probe_tailscale_ip(subject, facts):
    rc, out, err = run(["tailscale", "status", "--json"])
    if rc != 0:
        return {(subject, f["predicate"]): (None, f"tailscale rc={rc}: {err[:120]}")
                for f in facts}
    data = json.loads(out)
    peers = {p.get("HostName", "").lower(): p for p in data.get("Peer", {}).values()}
    self_peer = data.get("Self") or {}
    if subject.lower() == (self_peer.get("HostName") or "").lower():
        ips = self_peer.get("TailscaleIPs") or []
    else:
        ips = (peers.get(subject.lower()) or {}).get("TailscaleIPs") or []
    observed = ips[0] if ips else None
    return {(subject, f["predicate"]): (observed, None if observed else "not in tailscale status")
            for f in facts}


def probe_fleet_inventory(subject, facts):
    rc, out, err = run(["fleet-inv", "get", subject, "--json"])
    if rc != 0:
        return {(subject, f["predicate"]): (None, f"fleet-inv rc={rc}: {err[:120]}")
                for f in facts}
    try:
        data = json.loads(out)
    except ValueError as e:
        return {(subject, f["predicate"]): (None, f"fleet-inv non-json: {e}")
                for f in facts}
    result = {}
    for f in facts:
        val = data.get(f["predicate"])
        result[(subject, f["predicate"])] = (
            (str(val) if val is not None else None),
            None if val is not None else f"{f['predicate']} absent from fleet-inv",
        )
    return result


def _ssh_argv(node, remote_argv):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            node, "--", *remote_argv]


def probe_service_state(subject, facts):
    result = {}
    for f in facts:
        unit = f["value"]
        argv = ["systemctl", "is-active", unit]
        rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
        out = out.strip()
        if rc in (255, 99):
            result[(subject, f["predicate"])] = (None, f"ssh {subject}: {err[:120]}")
        elif out:
            result[(subject, f["predicate"])] = (out, None)
        else:
            result[(subject, f["predicate"])] = (None, f"rc={rc} no output")
    return result


def probe_node_storage(subject, facts):
    ram_argv = ["dmidecode", "--type", "17"]
    lsblk_argv = ["lsblk", "-J"]
    mdstat_argv = ["cat", "/proc/mdstat"]
    result = {}
    for f in facts:
        argv = ram_argv if f["predicate"] == "ram" else lsblk_argv
        rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
        if rc != 0:
            result[(subject, f["predicate"])] = (None, f"ssh {subject}: {err[:120]}")
            continue
        if f["predicate"] == "ram":
            sizes = re.findall(r"Size:\s*(\d+\s*[GM]B)", out)
            result[(subject, f["predicate"])] = (
                (sizes[0].replace(" ", "") if sizes else None),
                None if sizes else "no Size field in dmidecode",
            )
        else:
            try:
                blk = json.loads(out)
                total = sum(int(d.get("size") or 0) for d in blk.get("blockdevices", []))
                result[(subject, f["predicate"])] = (str(total) if total else None,
                                                       None if total else "empty lsblk")
            except ValueError as e:
                result[(subject, f["predicate"])] = (None, f"lsblk non-json: {e}")
    return result


def probe_os_kernel(subject, facts):
    result = {}
    for f in facts:
        argv = ["uname", "-r"] if f["predicate"] == "kernel" else ["uname", "-s"]
        rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
        out = out.strip()
        if rc != 0 or not out:
            result[(subject, f["predicate"])] = (None, f"ssh {subject}: {err[:120] or 'no output'}")
        else:
            result[(subject, f["predicate"])] = (out, None)
    return result


def probe_plug_map(subject, facts):
    rc, out, err = run(["plug-ctl", "status", "--json"])
    if rc != 0:
        return {(subject, f["predicate"]): (None, f"plug-ctl rc={rc}: {err[:120]}")
                for f in facts}
    try:
        data = json.loads(out)
    except ValueError as e:
        return {(subject, f["predicate"]): (None, f"plug-ctl non-json: {e}") for f in facts}
    entry = data.get(subject, {})
    result = {}
    for f in facts:
        val = entry.get(f["predicate"])
        result[(subject, f["predicate"])] = (
            str(val) if val is not None else None,
            None if val is not None else f"{f['predicate']} absent from plug-ctl",
        )
    return result


def probe_endpoint_live(subject, facts):
    import urllib.request
    result = {}
    for f in facts:
        url = f["value"] if f["predicate"] == "url" else f"http://{subject}:{f['value']}/"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                result[(subject, f["predicate"])] = (str(resp.status), None)
        except Exception as e:  # noqa: BLE001 - any transport failure is fail-open
            result[(subject, f["predicate"])] = (None, f"{type(e).__name__}: {e}")
    return result


def probe_node_liveness(subject, facts):
    """wq#1620: khepri/wadjet sat offline for 26d/53d while lumen_state kept

    stale POWERED ON / IDLE+available facts, because no probe ever backed a
    node's own reachability -- unlike probe_service_state, an ssh failure
    here IS the observation (offline), not an inconclusive error. Mirrors
    the manual check that caught the drift: ssh, then tailscale ping
    (app-layer, so it isn't fooled by ICMP filtering that a bare ping would
    miss).
    """
    ssh_rc, _, _ = run(_ssh_argv(subject, ["true"]))
    ts_rc, _, _ = run(["tailscale", "ping", "--c", "1", subject])
    observed = "available" if ssh_rc == 0 or ts_rc == 0 else "offline"
    return {(subject, f["predicate"]): (observed, None) for f in facts}


PROBES = {
    "tailscale_ip": probe_tailscale_ip,
    "fleet_inventory": probe_fleet_inventory,
    "service_state": probe_service_state,
    "node_storage": probe_node_storage,
    "os_kernel": probe_os_kernel,
    "plug_map": probe_plug_map,
    "endpoint_live": probe_endpoint_live,
    "node_liveness": probe_node_liveness,
}


# ------------------------------------------------------------- reconciliation

def file_conflict(subject, predicate, old_value, observed, probe_id):
    title = (
        f"minerva verify conflict: {subject}.{predicate} frank_stated={old_value!r} "
        f"observed={observed!r} via {probe_id}"
    )
    run([WQ, "add", title, "--lane", "review", "--pri", "2",
         "--detail", f"{subject}.{predicate} is frank_stated and must never be "
         f"auto-superseded (MINERVA_STALENESS_REVIEW.md §3.4). Reconcile by hand.",
         "--archetype", "standard-engineering"])
    run([AQ, "add", "--source", "lumen-verify", "--severity", "warn", "--message", title])


def reconcile(conn, fact, probe_id, observed, error, now):
    vid = str(uuid.uuid4())
    ts = now.isoformat(timespec="seconds")
    if error is not None or observed is None:
        outcome = "unknown"
    elif observed == fact["value"]:
        outcome = "confirmed"
    elif fact["source_kind"] == FRANK_STATED:
        outcome = "unknown"
        file_conflict(fact["subject"], fact["predicate"], fact["value"], observed, probe_id)
    else:
        outcome = "superseded"

    conn.execute(
        "INSERT INTO fact_verifications "
        "(id, fact_id, subject, predicate, probe, observed, outcome, ts, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (vid, fact["id"], fact["subject"], fact["predicate"], probe_id,
         observed, outcome, ts, error),
    )

    if outcome == "confirmed":
        conn.execute(
            "UPDATE state_facts SET last_confirmed_at=?, contradicted_at=NULL "
            "WHERE id=?",
            (ts, fact["id"]),
        )
    elif outcome == "superseded":
        conn.execute(
            "UPDATE state_facts SET valid_to=? WHERE id=?", (ts, fact["id"])
        )
        conn.execute(
            "INSERT INTO state_facts (subject, predicate, value, valid_from, "
            "ingested_at, source_ref, source_kind, verify_recipe, fact_class) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (fact["subject"], fact["predicate"], observed, ts, ts,
             f"verify:{probe_id}", "observed", probe_id, fact["fact_class"]),
        )
    return outcome


# ------------------------------------------------------------------- sweep

def sweep(conn, registry, now=None, dry_run=False):
    now = now or datetime.now(timezone.utc)
    migrate(conn)
    batches = due_facts(conn, registry, now)
    counts = {"confirmed": 0, "superseded": 0, "unknown": 0}
    detail = []
    for (probe_id, subject), facts in batches.items():
        try:
            observations = PROBES[probe_id](subject, facts)
        except Exception as e:  # noqa: BLE001 - fail-open: a broken probe never supersedes
            observations = {(subject, f["predicate"]): (None, f"{type(e).__name__}: {e}")
                             for f in facts}
        for f in facts:
            observed, error = observations.get((subject, f["predicate"]), (None, "no observation"))
            if dry_run:
                outcome = "unknown" if (error or observed is None) else (
                    "confirmed" if observed == f["value"] else "superseded"
                )
            else:
                outcome = reconcile(conn, f, probe_id, observed, error, now)
            counts[outcome] = counts.get(outcome, 0) + 1
            detail.append({
                "subject": subject, "predicate": f["predicate"], "probe": probe_id,
                "value": f["value"], "observed": observed, "outcome": outcome,
                "error": error,
            })
    if not dry_run:
        conn.commit()
    return {"due": sum(len(v) for v in batches.values()), "counts": counts, "detail": detail}


# ---------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sweep")
    sp.add_argument("--db", default=MINERVA_DB)
    sp.add_argument("--registry", default=REGISTRY_PATH)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--now", default=None, help="ISO8601, for deterministic runs")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    registry = Registry(args.registry)
    now = datetime.fromisoformat(args.now) if args.now else None
    result = sweep(conn, registry, now=now, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        log(f"due={result['due']} counts={result['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
