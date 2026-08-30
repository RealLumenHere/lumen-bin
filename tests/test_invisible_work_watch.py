#!/usr/bin/env python3
"""Test-to-failure for probe_invisible_work_watch (wq#973). Zero-quota: uses
a scratch WQ_DB + AQ_DB (real wq/aq schema) and captures the digest ntfy to a
file (no network, no real tickets, no page to Frank). Same pattern as
bin/tests/test_aq_escalate.py.

Run: python3 tests/test_invisible_work_watch.py
"""
import os, sys, sqlite3, tempfile, datetime, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_MOD_PATH = os.path.join(HERE, "..", "..", "sentinel", "probes", "sentinel_probes.py")

TASKS_SCHEMA = """
CREATE TABLE tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, lane TEXT DEFAULT 'auto',
    pri INTEGER DEFAULT 2, title TEXT, detail TEXT, deps TEXT DEFAULT '',
    status TEXT DEFAULT 'open', claimed_by TEXT, claimed_at TEXT,
    done_at TEXT, notes TEXT DEFAULT '', created_at TEXT, lease_expires TEXT,
    subsystems TEXT DEFAULT '', gate TEXT DEFAULT '', lease_ttl INTEGER,
    model TEXT, effort TEXT, archetype TEXT, gate_type TEXT,
    decision_prompt TEXT, orch_recommendation TEXT, frank_response TEXT,
    response_ts TEXT, review_verdict TEXT, review_reason TEXT,
    block_reason TEXT, blocked_at TEXT, reopened_count INTEGER DEFAULT 0,
    reopened_at TEXT);
"""
ALERTS_SCHEMA = """
CREATE TABLE alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  first_ts TEXT NOT NULL, last_ts TEXT NOT NULL,
  source TEXT NOT NULL, key TEXT NOT NULL,
  title TEXT, message TEXT, priority TEXT DEFAULT 'default',
  count INTEGER DEFAULT 1, status TEXT DEFAULT 'open',
  owner TEXT DEFAULT 'unassigned', note TEXT, resolved_ts TEXT);
"""

_fails = []
def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        _fails.append(name)

def _ts(hours_ago):
    dt = datetime.datetime.utcnow() - datetime.timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def fresh():
    d = tempfile.mkdtemp(prefix="iwwatch_")
    wq = os.path.join(d, "queue.db")
    aq = os.path.join(d, "alerts.db")
    cap = os.path.join(d, "digest.jsonl")
    c = sqlite3.connect(wq); c.executescript(TASKS_SCHEMA); c.commit(); c.close()
    c = sqlite3.connect(aq); c.executescript(ALERTS_SCHEMA); c.commit(); c.close()
    return d, wq, aq, cap

def add_task(wq, lane="auto", status="open", claimed_by=None, hours_ago=72,
             title="synthetic ticket"):
    c = sqlite3.connect(wq)
    c.execute("INSERT INTO tasks(lane,pri,title,detail,status,claimed_by,created_at) "
              "VALUES(?,?,?,?,?,?,?)",
              (lane, 3, title, "detail", status, claimed_by, _ts(hours_ago)))
    c.commit(); c.close()

def add_alert(aq, n=1):
    c = sqlite3.connect(aq)
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(n):
        c.execute("INSERT INTO alerts(first_ts,last_ts,source,key,title,message,status) "
                  "VALUES(?,?,?,?,?,?,'open')",
                  (now, now, "probe", f"probe-x-{i}", "t", "m"))
    c.commit(); c.close()

def load_probe(wq, aq, cap, hours=48):
    """Import sentinel_probes fresh with env pointed at scratch DBs, isolated
    per-call via a unique module name (avoids caching stale env into a
    previously-imported module — sentinel_probes reads its env at import time)."""
    os.environ["SENTINEL_WQ_DB"] = wq
    os.environ["AQ_DB"] = aq
    os.environ["SENTINEL_NTFY_UPDATES_CAPTURE"] = cap
    os.environ["SENTINEL_INVISIBLE_WORK_HOURS"] = str(hours)
    # sentinel_probes.py reads a litellm key file at import time for an
    # unrelated probe; make it a harmless no-op if absent on the test host.
    os.environ.setdefault("LITELLM_MASTER_KEY", "test-key-unused")
    # aq.py caches its DB path at import time (module-level `DB = ...`); since
    # each scenario points AQ_DB at a NEW scratch db, force a fresh import or
    # a later scenario silently reads the previous scenario's aq.db.
    for modname in ("aq", "sentinel_probes"):
        sys.modules.pop(modname, None)
    spec = importlib.util.spec_from_file_location(f"sentinel_probes_test_{id(cap)}", PROBE_MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def n_digest(cap):
    if not os.path.exists(cap):
        return 0
    return sum(1 for l in open(cap) if l.strip())


def scenario_synthetic_flag():
    print("[flag] a synthetic >48h-undispatched ticket IS flagged")
    d, wq, aq, cap = fresh()
    add_task(wq, hours_ago=72)  # 3 days old, unclaimed, agent-claimable lane
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("ok=False (flagged)", ok is False, detail)
    check("detail mentions 1 ticket", "1 wq ticket" in detail, detail)
    check("never critical (no page)", critical is False)
    check("digest posted", n_digest(cap) == 1, f"got {n_digest(cap)}")

def scenario_fresh_not_flagged():
    print("[fresh] a fresh ticket (created now) does NOT get flagged")
    d, wq, aq, cap = fresh()
    add_task(wq, hours_ago=1)  # 1h old
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("ok=True (nothing flagged)", ok is True, detail)
    check("detail mentions 0 tickets", "0 wq ticket" in detail, detail)
    check("digest still posted (weekly rollup happens regardless)", n_digest(cap) == 1)

def scenario_claimed_not_flagged():
    print("[claimed] an old but CLAIMED ticket is not undispatched")
    d, wq, aq, cap = fresh()
    add_task(wq, hours_ago=100, claimed_by="some-session")
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("ok=True (claimed, not invisible)", ok is True, detail)

def scenario_user_lane_excluded():
    print("[user-lane] old unclaimed user-lane tickets are Frank action items, not flagged")
    d, wq, aq, cap = fresh()
    add_task(wq, lane="user", hours_ago=200)
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("ok=True (user lane excluded)", ok is True, detail)

def scenario_done_not_flagged():
    print("[done] an old ticket that is already done/claimed-status is not open, not flagged")
    d, wq, aq, cap = fresh()
    add_task(wq, hours_ago=200, status="done")
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("ok=True (done, not open)", ok is True, detail)

def scenario_boundary_hours():
    print("[boundary] exactly at the threshold behaves consistently either side")
    d, wq, aq, cap = fresh()
    add_task(wq, hours_ago=47, title="just under")
    add_task(wq, hours_ago=49, title="just over")
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("only the >48h one flags", "1 wq ticket" in detail, detail)

def scenario_aq_backlog_rollup():
    print("[aq-rollup] aq open backlog count is reported in the weekly digest")
    d, wq, aq, cap = fresh()
    add_alert(aq, n=7)
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("aq backlog=7 in detail", "aq open backlog=7" in detail, detail)
    body = open(cap).read()
    check("aq backlog=7 in digest body", "aq open backlog: 7" in body, body)

def scenario_never_pages():
    print("[no-page] a large flagged count still never sets critical=True")
    d, wq, aq, cap = fresh()
    for i in range(15):
        add_task(wq, hours_ago=200 + i, title=f"old {i}")
    mod = load_probe(wq, aq, cap)
    ok, detail, critical = mod.probe_invisible_work_watch()
    check("ok=False", ok is False)
    check("critical=False even with 15 flagged (never the page path)", critical is False)
    check("digest lists top 10 + overflow note", "and 5 more" in open(cap).read())


def main():
    for fn in (scenario_synthetic_flag, scenario_fresh_not_flagged,
               scenario_claimed_not_flagged, scenario_user_lane_excluded,
               scenario_done_not_flagged, scenario_boundary_hours,
               scenario_aq_backlog_rollup, scenario_never_pages):
        fn()
    print()
    if _fails:
        print(f"FAILED {len(_fails)} check(s): {_fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0

if __name__ == "__main__":
    sys.exit(main())
