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
              count=1, status="open", message="NO TURNS in 60m though ActiveState=active",
              title=None):
    c = sqlite3.connect(aq)
    c.execute("INSERT INTO alerts(first_ts,last_ts,source,key,title,message,priority,count,status) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (_now(), _now(), source, key, title or f"probe FAIL: {key}",
               message, priority, count, status))
    c.commit(); rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.close()
    return rid

# wq#1674: the row shape harness/repo_sweep.py writes — the wq title and detail verbatim,
# so the escalation only has to pass them through. The detail's FIRST line is the
# repo_hint contract (wq#1094) the dispatcher reads back to route the 'auto' lane.
SWEEP_REPO = "/home/lumen/bin"
SWEEP_TITLE = "[repo-sweep] bin: BEHIND_ONLY"
SWEEP_DETAIL = (f"Automated sweep detected BEHIND_ONLY in {SWEEP_REPO}.\n"
                f"Output: BEHIND_ONLY {SWEEP_REPO} main ahead=0 behind=3\n\n"
                f"To inspect: python3 repo_reconcile.py status --repo {SWEEP_REPO}")

def add_sweep_alert(aq, count=1, verdict="BEHIND_ONLY", repo=SWEEP_REPO,
                    title=SWEEP_TITLE, message=SWEEP_DETAIL):
    return add_alert(aq, source="repo-sweep", key=f"{repo}:{verdict}", priority="default",
                     count=count, title=title, message=message)

def run_esc(aq, wq, cap, threshold=3, wq_bin=None, sweep_threshold=3):
    env = dict(os.environ)
    env.update({"AQ_DB": aq, "WQ_DB": wq, "AQ_ESCALATE_NTFY_CAPTURE": cap,
                "AQ_ESCALATE_THRESHOLD": str(threshold),
                "AQ_ESCALATE_REPO_SWEEP_THRESHOLD": str(sweep_threshold),
                # wq#1487 added a LIVE re-probe before escalating, and this suite was
                # never updated for it: the scratch rows name real probes, so every
                # scenario silently ran sentinel_probes against the live fleet and got
                # PASS -> row resolved -> 0 tickets. That broke the suite's own
                # zero-quota/no-network contract as well as its assertions. The rule
                # under test here is the escalate/suppress ladder; wq#1487's re-probe
                # gate is exercised by its own knob, off by default in test.
                "AQ_ESCALATE_LIVE_REPROBE": "0",
                "AQ_WQ_BIN": wq_bin or REAL_WQ})
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")

def tickets(wq, lane="fleet-fires"):
    if not os.path.exists(wq):
        return []
    c = sqlite3.connect(wq); c.row_factory = sqlite3.Row
    try:
        rs = c.execute("SELECT * FROM tasks WHERE lane=?", (lane,)).fetchall()
    except sqlite3.OperationalError:
        rs = []
    c.close(); return rs

def n_tickets(wq):
    return len(tickets(wq))

def pages(cap):
    if not os.path.exists(cap):
        return []
    return [json.loads(l) for l in open(cap) if l.strip()]

def n_pages(cap):
    return len(pages(cap))

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


# --- wq#1674: repo-sweep rows become a ticket only once they persist ------------------

def scenario_sweep_threshold():
    print("[sweep] one fire is an alert, not a ticket; the 3rd sweep earns the ticket")
    d, aq, wq, cap = fresh()
    add_sweep_alert(aq, count=2)
    run_esc(aq, wq, cap)
    check("count<threshold -> 0 tickets", len(tickets(wq, "auto")) == 0)
    check("count<threshold -> 0 pages", n_pages(cap) == 0)
    d, aq, wq, cap = fresh()
    add_sweep_alert(aq, count=3)
    run_esc(aq, wq, cap)
    ts = tickets(wq, "auto")
    check("count>=threshold -> 1 ticket in lane auto", len(ts) == 1, f"got {len(ts)}")
    if ts:
        check("title passed through verbatim", ts[0]["title"] == SWEEP_TITLE, ts[0]["title"])
        check("pri 1 (not a fleet fire)", ts[0]["pri"] == 1, str(ts[0]["pri"]))
        check("repo_hint contract: detail still starts with the sweep line",
              ts[0]["detail"].startswith(f"Automated sweep detected BEHIND_ONLY in {SWEEP_REPO}."),
              ts[0]["detail"][:80])
        check("provenance appended, not prepended", "wq#1674 repo-sweep rule" in ts[0]["detail"])
    check("no fleet-fires ticket", n_tickets(wq) == 0)
    ps = pages(cap)
    check("exactly 1 page", len(ps) == 1, f"got {len(ps)}")
    if ps:
        check("page is not urgent", ps[0]["priority"] == "default", ps[0]["priority"])

def scenario_sweep_one_ticket_per_incident():
    print("[sweep] F5: repeat sweeps never refile; only a NEW aq row does")
    d, aq, wq, cap = fresh()
    rid = add_sweep_alert(aq, count=3)
    run_esc(aq, wq, cap)
    check("first escalation -> 1 ticket", len(tickets(wq, "auto")) == 1)
    # The sweep keeps firing: count++ on the SAME row, nine times over nine days.
    c = sqlite3.connect(aq); c.execute("UPDATE alerts SET count=12 WHERE id=?", (rid,))
    c.commit(); c.close()
    run_esc(aq, wq, cap)
    run_esc(aq, wq, cap)
    check("repeat fires -> still 1 ticket", len(tickets(wq, "auto")) == 1,
          f"got {len(tickets(wq, 'auto'))}")
    check("repeat fires -> still 1 page", n_pages(cap) == 1, f"got {n_pages(cap)}")
    # Worker reconciles the repo; the next sweep edge-clears the row. A LATER
    # divergence is a new row, and therefore honestly a new ticket.
    c = sqlite3.connect(aq); c.execute("UPDATE alerts SET status='resolved' WHERE id=?", (rid,))
    c.commit(); c.close()
    run_esc(aq, wq, cap)
    check("resolved row -> no new ticket", len(tickets(wq, "auto")) == 1)
    add_sweep_alert(aq, count=3, title="[repo-sweep] bin: CONTENT_DIVERGED",
                    verdict="CONTENT_DIVERGED",
                    message=f"Automated sweep detected CONTENT_DIVERGED in {SWEEP_REPO}.\nOutput: x")
    run_esc(aq, wq, cap)
    check("new incident -> new ticket", len(tickets(wq, "auto")) == 2,
          f"got {len(tickets(wq, 'auto'))}")

def scenario_sweep_malformed_row():
    print("[sweep] a row that is not a repo-sweep ticket shape files nothing")
    d, aq, wq, cap = fresh()
    add_sweep_alert(aq, count=9, title="hand-edited nonsense")
    add_sweep_alert(aq, count=9, verdict="SAFE_RESET", message="")
    rc, out = run_esc(aq, wq, cap)
    check("malformed -> 0 tickets", len(tickets(wq, "auto")) == 0)
    check("malformed -> 0 pages", n_pages(cap) == 0)
    check("skip is reported, not silent", "SKIPPED" in out, out[-200:])


def main():
    for fn in (scenario_boundary, scenario_replay_152, scenario_acked_and_resolved,
               scenario_scoping, scenario_two_distinct, scenario_transient_ticket_failure,
               scenario_refire_after_resolve, scenario_sweep_threshold,
               scenario_sweep_one_ticket_per_incident, scenario_sweep_malformed_row):
        fn()
    print()
    if _fails:
        print(f"FAILED {len(_fails)} check(s): {_fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0

if __name__ == "__main__":
    sys.exit(main())
