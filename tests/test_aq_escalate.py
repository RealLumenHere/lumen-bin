#!/usr/bin/env python3
"""Test-to-failure for aq_escalate.py (wq#965). Zero-quota: every scenario uses a
scratch AQ_DB + scratch WQ_DB and captures pages to a file (no ntfy, no real
tickets, no page to Frank). Run: python3 tests/test_aq_escalate.py

Rows are inserted with the exact alerts schema and shape that sentinel_probes.py
ingests for a CRITICAL fail (source='probe', priority='high'), verified against
live row aq#836 (the probe-pokemon-liveness 152-repeat case)."""
import os, sys, json, sqlite3, subprocess, tempfile, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "aq_escalate.py")
REAL_WQ = os.environ.get("REAL_WQ", "/home/lumen/bin/wq")

ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(
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

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def fresh():
    d = tempfile.mkdtemp(prefix="aqesc_")
    aq = os.path.join(d, "alerts.db")
    wq = os.path.join(d, "queue.db")
    cap = os.path.join(d, "pages.jsonl")
    c = sqlite3.connect(aq); c.executescript(ALERTS_SCHEMA); c.commit(); c.close()
    return d, aq, wq, cap

def add_alert(aq, source="probe", key="probe-pokemon-liveness", priority="high",
              count=1, status="open", message="NO TURNS in 60m though ActiveState=active"):
    c = sqlite3.connect(aq)
    c.execute("INSERT INTO alerts(first_ts,last_ts,source,key,title,message,priority,count,status) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (_now(), _now(), source, key, f"probe FAIL: {key}", message, priority, count, status))
    c.commit(); rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.close()
    return rid

def run_esc(aq, wq, cap, threshold=3, wq_bin=None):
    env = dict(os.environ)
    env.update({"AQ_DB": aq, "WQ_DB": wq, "AQ_ESCALATE_NTFY_CAPTURE": cap,
                "AQ_ESCALATE_THRESHOLD": str(threshold), "AQ_WQ_BIN": wq_bin or REAL_WQ})
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")

def n_tickets(wq):
    if not os.path.exists(wq):
        return 0
    c = sqlite3.connect(wq)
    try:
        n = c.execute("SELECT COUNT(*) FROM tasks WHERE lane='fleet-fires'").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    c.close(); return n

def n_pages(cap):
    if not os.path.exists(cap):
        return 0
    return sum(1 for l in open(cap) if l.strip())

def esc_rows(aq):
    c = sqlite3.connect(aq); c.row_factory = sqlite3.Row
    rs = c.execute("SELECT * FROM escalations").fetchall(); c.close(); return rs


def scenario_boundary():
    print("[boundary] count<threshold no-op; count>=threshold escalates")
    d, aq, wq, cap = fresh()
    rid = add_alert(aq, count=2)               # below threshold
    run_esc(aq, wq, cap)
    check("count=2 -> 0 tickets", n_tickets(wq) == 0)
    check("count=2 -> 0 pages", n_pages(cap) == 0)
    # bump same open row to 3 (simulate the next sentinel fail)
    c = sqlite3.connect(aq); c.execute("UPDATE alerts SET count=3 WHERE id=?", (rid,)); c.commit(); c.close()
    run_esc(aq, wq, cap)
    check("count=3 -> 1 ticket", n_tickets(wq) == 1)
    check("count=3 -> 1 page", n_pages(cap) == 1)

def scenario_replay_152():
    print("[replay] probe-pokemon-liveness 152-repeat -> exactly 1 ticket + 1 page, then suppressed")
    d, aq, wq, cap = fresh()
    add_alert(aq, count=152)
    run_esc(aq, wq, cap)
    check("152 -> exactly 1 ticket", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    check("152 -> exactly 1 page", n_pages(cap) == 1, f"got {n_pages(cap)}")
    er = esc_rows(aq)
    check("escalation recorded with count_at_escalation=152",
          len(er) == 1 and er[0]["count_at_escalation"] == 152)
    check("escalation has ticket_id + paged_at", bool(er[0]["ticket_id"]) and bool(er[0]["paged_at"]))
    # re-run: suppression
    run_esc(aq, wq, cap)
    check("re-run -> still 1 ticket (no dup)", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    check("re-run -> still 1 page (suppressed)", n_pages(cap) == 1, f"got {n_pages(cap)}")

def scenario_acked_and_resolved():
    print("[acked/resolved] a row that is acked or resolved never escalates")
    d, aq, wq, cap = fresh()
    add_alert(aq, count=50, status="acked")
    add_alert(aq, count=50, status="resolved")
    run_esc(aq, wq, cap)
    check("acked/resolved -> 0 tickets", n_tickets(wq) == 0)
    check("acked/resolved -> 0 pages", n_pages(cap) == 0)

def scenario_scoping():
    print("[scoping] non-critical high-count rows are ignored (only source=probe priority=high)")
    d, aq, wq, cap = fresh()
    add_alert(aq, source="ntfy:lumen-updates", key="Homelab: models", priority="5", count=119)
    add_alert(aq, source="probe", key="probe-state-drift", priority="default", count=41)
    add_alert(aq, source="repo-sentinel", key="RealLumenHere/questfree", priority="default", count=339)
    run_esc(aq, wq, cap)
    check("noise rows -> 0 tickets", n_tickets(wq) == 0, f"got {n_tickets(wq)}")
    check("noise rows -> 0 pages", n_pages(cap) == 0, f"got {n_pages(cap)}")

def scenario_two_distinct():
    print("[multi] two distinct critical probes -> two separate tickets + pages")
    d, aq, wq, cap = fresh()
    add_alert(aq, key="probe-pokemon-liveness", count=5)
    add_alert(aq, key="probe-storage-integrity", count=8)
    run_esc(aq, wq, cap)
    check("two probes -> 2 tickets", n_tickets(wq) == 2, f"got {n_tickets(wq)}")
    check("two probes -> 2 pages", n_pages(cap) == 2, f"got {n_pages(cap)}")

def scenario_transient_ticket_failure():
    print("[transient] wq failure -> no page; next run completes ticket + pages once")
    d, aq, wq, cap = fresh()
    add_alert(aq, count=7)
    fail_wq = os.path.join(d, "wq_fail")
    open(fail_wq, "w").write("#!/bin/bash\necho 'boom' >&2\nexit 1\n")
    os.chmod(fail_wq, 0o755)
    run_esc(aq, wq, cap, wq_bin=fail_wq)
    check("wq fails -> 0 tickets", n_tickets(wq) == 0)
    check("wq fails -> 0 pages (never page without a ticket)", n_pages(cap) == 0)
    er = esc_rows(aq)
    check("escalation row claimed with ticket_id NULL", len(er) == 1 and er[0]["ticket_id"] is None)
    # retry with the real wq
    run_esc(aq, wq, cap)
    check("retry -> 1 ticket", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    check("retry -> exactly 1 page", n_pages(cap) == 1, f"got {n_pages(cap)}")

def add_dispatch_alert(aq, title="[DISPATCH] DISPATCHER STALLED", count=1, status="open",
                       message="Dispatcher STALLED: last 2 passes all unhealthy."):
    c = sqlite3.connect(aq)
    c.execute("INSERT INTO alerts(first_ts,last_ts,source,key,title,message,priority,count,status) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (_now(), _now(), "ntfy:lumen-alerts", title, title, message, "5", count, status))
    c.commit(); rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.close()
    return rid

def scenario_dispatch_boundary():
    print("[dispatch-boundary] wq#1410: DISPATCHER STALLED below threshold no-op; at threshold escalates")
    d, aq, wq, cap = fresh()
    rid = add_dispatch_alert(aq, count=2)
    run_esc(aq, wq, cap)
    check("count=2 -> 0 tickets", n_tickets(wq) == 0)
    check("count=2 -> 0 pages", n_pages(cap) == 0)
    c = sqlite3.connect(aq); c.execute("UPDATE alerts SET count=3 WHERE id=?", (rid,)); c.commit(); c.close()
    run_esc(aq, wq, cap)
    check("count=3 -> 1 ticket", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    check("count=3 -> 1 page", n_pages(cap) == 1, f"got {n_pages(cap)}")

def scenario_dispatch_unprefixed_title_still_matches():
    print("[dispatch-legacy] a pre-wq#1410 unprefixed 'DISPATCHER STALLED' row still escalates (LIKE match)")
    d, aq, wq, cap = fresh()
    add_dispatch_alert(aq, title="DISPATCHER STALLED", count=145)  # the real aq#794 shape
    run_esc(aq, wq, cap)
    check("legacy title -> 1 ticket", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    check("legacy title -> 1 page", n_pages(cap) == 1, f"got {n_pages(cap)}")

def scenario_dispatch_and_probe_rules_are_independent():
    print("[dispatch-independence] a DISPATCHER STALLED row and a probe row escalate separately, "
          "neither rule double-counts the other's row")
    d, aq, wq, cap = fresh()
    add_alert(aq, key="probe-pokemon-liveness", count=5)
    add_dispatch_alert(aq, count=5)
    run_esc(aq, wq, cap)
    check("probe + dispatch -> 2 tickets", n_tickets(wq) == 2, f"got {n_tickets(wq)}")
    check("probe + dispatch -> 2 pages", n_pages(cap) == 2, f"got {n_pages(cap)}")

def scenario_dispatch_resolve_gives_fresh_per_incident_count():
    print("[dispatch-recover] wq#1410 §3.3.3: dispatch_recover.py resolving the row on a healthy "
          "pass means a new incident starts its count from zero, not lifetime")
    d, aq, wq, cap = fresh()
    rid = add_dispatch_alert(aq, count=10)
    run_esc(aq, wq, cap)
    check("first incident -> 1 ticket", n_tickets(wq) == 1)
    # dispatch_recover.reset() resolves the row the moment a healthy pass lands
    c = sqlite3.connect(aq); c.execute("UPDATE alerts SET status='resolved' WHERE id=?", (rid,)); c.commit(); c.close()
    run_esc(aq, wq, cap)
    check("after recovery -> no new ticket yet", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    # a NEW stall is a NEW aq row with its own count, not the old lifetime 145
    add_dispatch_alert(aq, count=3)
    run_esc(aq, wq, cap)
    check("new incident (new aq id) -> new ticket", n_tickets(wq) == 2, f"got {n_tickets(wq)}")
    check("new incident -> new page", n_pages(cap) == 2, f"got {n_pages(cap)}")

def scenario_refire_after_resolve():
    print("[refire] resolve then a NEW aq row -> a NEW escalation (new outage, new ticket)")
    d, aq, wq, cap = fresh()
    rid = add_alert(aq, count=10)
    run_esc(aq, wq, cap)
    check("first outage -> 1 ticket", n_tickets(wq) == 1)
    # sentinel PASS edge-clears the row
    c = sqlite3.connect(aq); c.execute("UPDATE alerts SET status='resolved' WHERE id=?", (rid,)); c.commit(); c.close()
    run_esc(aq, wq, cap)
    check("after resolve -> no new ticket", n_tickets(wq) == 1, f"got {n_tickets(wq)}")
    # brand new failure episode = new aq row
    add_alert(aq, count=3)
    run_esc(aq, wq, cap)
    check("re-fire (new aq id) -> new ticket", n_tickets(wq) == 2, f"got {n_tickets(wq)}")
    check("re-fire -> new page", n_pages(cap) == 2, f"got {n_pages(cap)}")


def main():
    for fn in (scenario_boundary, scenario_replay_152, scenario_acked_and_resolved,
               scenario_scoping, scenario_two_distinct, scenario_transient_ticket_failure,
               scenario_refire_after_resolve, scenario_dispatch_boundary,
               scenario_dispatch_unprefixed_title_still_matches,
               scenario_dispatch_and_probe_rules_are_independent,
               scenario_dispatch_resolve_gives_fresh_per_incident_count):
        fn()
    print()
    if _fails:
        print(f"FAILED {len(_fails)} check(s): {_fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0

if __name__ == "__main__":
    sys.exit(main())
