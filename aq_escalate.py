#!/usr/bin/env python3
"""aq escalation responder (wq#965, R2 report section 6 RESPONDER / T2).

The missing tier between DETECTION and RESPONSE. Sentinel CRITICAL probes ingest
FAILs into aq as source='probe', priority='high' and page ntfy every 30m, but
nothing OWNS answering them (probe-pokemon-liveness fired 152x/3d, zero action).

This responder turns a persistent, unacked CRITICAL into a claimable unit of work
plus exactly one page:

  any alerts row with source='probe' AND priority='high' AND status='open'
  AND count>=THRESHOLD, not already escalated
    -> (1) seed ONE P0 fleet-fires wq ticket naming the runbook
       (2) page Frank ONCE (ntfy urgent, lumen-alerts) referencing the ticket
       (3) suppress: record it; never re-page while that open row persists.

The alert row is left status='open' so the sentinel still edge-clears it on
recovery (wq#548); a later re-fire is a NEW aq id => a NEW escalation, because a
new outage deserves a new ticket. At-most-once side effects and idempotency are
enforced by the escalations table (UNIQUE aq_id + step-wise ticket_id/paged_at),
so a transient wq/ntfy failure retries WITHOUT double-paging.

Run by lumen-aq-escalate.timer every 10m (< the 30m sentinel cadence), so a 3rd
consecutive fail escalates within the 3rd->4th sentinel cycle.

wq#1410 (research/NIGHTLY_DISPATCH_RESILIENCE.md §3.3.3) adds a second rule for the
dispatcher's own stall page: dispatch_health.py already detects "DISPATCHER STALLED"
and pages, but this responder only escalated source='probe' rows — row aq#794 sat
open with count=145 since 08-23 because nothing else ever looked at it. The new rule
matches any open alert whose title contains "DISPATCHER STALLED" (dispatch_health.py
prefixes it "[DISPATCH] DISPATCHER STALLED", wq#1410 §3.3.4; LIKE-match so an older
unprefixed row still escalates too) with count>=AQ_ESCALATE_DISPATCH_THRESHOLD. This
reuses the exact same open/count/escalations-table mechanism the probe rule uses, so
"count_since_last_recovery" needs no new schema: dispatcher/dispatch_recover.py (the
recovery ladder, same wq) resolves the row itself the moment a healthy pass lands, so
a fresh open row's count is already scoped to one incident, not lifetime.

Env (tests/extension): AQ_DB, WQ_DB, AQ_ESCALATE_THRESHOLD, AQ_ESCALATE_SOURCE,
AQ_ESCALATE_PRIORITY, AQ_ESCALATE_DISPATCH_THRESHOLD, AQ_ESCALATE_NTFY_URL,
AQ_ESCALATE_NTFY_CAPTURE (file: no network), AQ_WQ_BIN.
"""
import os, sys, json, re, sqlite3, subprocess, urllib.request, datetime

AQ_DB     = os.environ.get("AQ_DB", "/home/lumen/agents/alert-queue/alerts.db")
WQ_BIN    = os.environ.get("AQ_WQ_BIN", "/home/lumen/bin/wq")
THRESHOLD = int(os.environ.get("AQ_ESCALATE_THRESHOLD", "3"))
CRIT_SOURCE   = os.environ.get("AQ_ESCALATE_SOURCE", "probe")
CRIT_PRIORITY = os.environ.get("AQ_ESCALATE_PRIORITY", "high")
DISPATCH_TITLE_LIKE = "%DISPATCHER STALLED%"
DISPATCH_THRESHOLD = int(os.environ.get("AQ_ESCALATE_DISPATCH_THRESHOLD", "3"))
DISPATCH_RUNBOOK = ("dispatcher/dispatch_recover.py status (recovery ladder history); "
                     "dispatcher.log for the stalled pass shape; runtime/recover_state.json; "
                     "wq#1410")
NTFY_URL  = os.environ.get("AQ_ESCALATE_NTFY_URL", "http://localhost:8080/lumen-alerts")
NTFY_CAP  = os.environ.get("AQ_ESCALATE_NTFY_CAPTURE")  # test: append pages to a file, no network
LANE      = "fleet-fires"

# Names the runbook per probe (wq#965 requirement). Unknown probes get the generic pointer.
RUNBOOKS = {
 "probe-pokemon-liveness":     "lpp-restart (slock-guarded), then confirm a turn_summary lands in pokemon/events; POKEMON_DISPATCH.md, wq#908",
 "probe-storage-integrity":    "storage_probe.py --run (auto-fix tier); agents/storage-audit/, wq#421",
 "probe-litellm-auth-monitor": "systemctl restart lumen-litellm-auth-monitor (Restart=always, wq#897); check docker litellm recreation",
 "probe-synthetic-llm":        "LiteLLM gateway :4000 content/health + rung; playbooks/llm-jobs.md",
 "probe-dead-references":      "phantom-table / missing-model sweep: audits/tools/tbl_sweep2.py + LiteLLM /v1/models",
 "probe-backup-freshness":     "restic locks/snapshots: clear stale lock, run lumen-backup-verify",
 "probe-dc-health":            "desktop-commander stdio roundtrip / instance count / node_killer log",
 "probe-output-rate":          "research pipeline dead or runaway: check scout + fleet workers",
 "probe-log-repeats":          "swallowed-error loop: journalctl the named unit, find the repeated WARNING",
 "probe-kb-hygiene":           "stale KB refs: agents/truth_table.yaml + flagged files",
 "probe-state-drift":          "map/tailnet/unit drift: reconcile FLEET_MAP + must-be-active list",
 "probe-minerva-rates":        "zombie-gate: minerva proposals all-blocked; check eval gate",
 "probe-duplicate-rate":       "dedup regression: lumen_scout_graph GAP_REFRESH_DAYS window",
 "probe-queue-health":         "research prompt queue: stale claims / unconsumed prompts",
}
DEFAULT_RUNBOOK = "investigate in sentinel/probes/sentinel_probes.py; playbooks/fleet-ops.md"

ESC_SCHEMA = """
CREATE TABLE IF NOT EXISTS escalations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  aq_id INTEGER NOT NULL UNIQUE,
  source TEXT, key TEXT,
  count_at_escalation INTEGER,
  ticket_id INTEGER,
  ticket_lane TEXT DEFAULT 'fleet-fires',
  paged_at TEXT,
  created_at TEXT NOT NULL,
  note TEXT);
"""

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def db():
    c = sqlite3.connect(AQ_DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(ESC_SCHEMA)
    return c

def _dur(first_ts, last_ts):
    try:
        a = datetime.datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
        mins = (b - a).total_seconds() / 60
        return f"{mins/60:.1f}h" if mins >= 120 else f"{mins:.0f}m"
    except Exception:
        return "?"

def seed_ticket(row):
    """wq add one fleet-fires P0 ticket naming the runbook. Returns ticket id, or
    None on hard failure. Reuses the existing id if wq refuses a duplicate title."""
    probe = row["key"]
    runbook = RUNBOOKS.get(probe, DEFAULT_RUNBOOK)
    dur = _dur(row["first_ts"], row["last_ts"])
    title = f"[fleet-fires] {probe} CRITICAL unacked x{row['count']} (aq#{row['id']})"
    detail = (
        f"Auto-escalated by aq_escalate (wq#965 responder).\n\n"
        f"Probe: {probe}\n"
        f"aq row: #{row['id']} (source={row['source']}, priority={row['priority']})\n"
        f"Repeats: {row['count']} consecutive CRITICAL fails, no ack, no recovery\n"
        f"First seen: {row['first_ts']}   Last: {row['last_ts']}   ({dur} unanswered)\n\n"
        f"Latest detail: {(row['message'] or '')[:600]}\n\n"
        f"RUNBOOK: {runbook}\n\n"
        f"Acceptance: root cause fixed, probe returns PASS (sentinel edge-clears aq#{row['id']}),"
        f" this ticket closed. If this is a false positive, fix the probe, do not just ack.\n"
        f"The responder never changes aq status; the sentinel PASS clears the row."
    )
    cmd = [WQ_BIN, "add", "--lane", LANE, "--pri", "0", "--title", title, "--detail", detail]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ), timeout=30)
    except Exception as e:
        print(f"  seed_ticket EXC for {probe} (aq#{row['id']}): {e}")
        return None
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"added task (\d+)", out)
    if m:
        return int(m.group(1))
    m = re.search(r"DUPLICATE .* task (\d+)", out)  # open ticket with this title already exists
    if m:
        return int(m.group(1))
    print(f"  seed_ticket FAILED for {probe} (aq#{row['id']}): {out.strip()[:200]}")
    return None

def page(row, ticket_id):
    """Page Frank once, distinct from the raw sentinel alert. Test mode
    (AQ_ESCALATE_NTFY_CAPTURE) appends the payload to a file with no network."""
    probe = row["key"]
    dur = _dur(row["first_ts"], row["last_ts"])
    title = f"FLEET FIRE: {probe} unanswered x{row['count']} -> wq#{ticket_id}"
    body = (f"{probe} CRITICAL open {row['count']}x, no ack for {dur}.\n"
            f"Auto-filed fleet-fires wq#{ticket_id}. Claim: wq claim --id {ticket_id}\n"
            f"{(row['message'] or '')[:300]}")
    return _send_page(row, ticket_id, title, body)

def _send_page(row, ticket_id, title, body):
    if NTFY_CAP:
        with open(NTFY_CAP, "a") as fh:
            fh.write(json.dumps({"ts": now(), "title": title, "body": body,
                                 "priority": "urgent", "aq_id": row["id"],
                                 "ticket": ticket_id}) + "\n")
        return True
    try:
        req = urllib.request.Request(
            NTFY_URL, data=body.encode(),
            headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light,fire"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ntfy page FAILED for {row['key']} (aq#{row['id']}): {e}")
        return False

# wq#1410: the dispatcher-stall rule reuses seed_ticket/page's shape (probe wording
# fits well enough: "DISPATCHER STALLED CRITICAL unacked x5" reads fine) with one
# difference — the runbook always points at the recovery ladder, not RUNBOOKS.get().

def seed_dispatch_ticket(row):
    dur = _dur(row["first_ts"], row["last_ts"])
    title = f"[fleet-fires] dispatcher stalled, ladder exhausted x{row['count']} (aq#{row['id']})"
    detail = (
        f"Auto-escalated by aq_escalate (wq#1410 dispatcher-stall rule).\n\n"
        f"aq row: #{row['id']} (source={row['source']}, priority={row['priority']}, "
        f"title={row['title']!r})\n"
        f"Repeats: {row['count']} consecutive DISPATCHER STALLED pages, recovery ladder "
        f"did not clear it\n"
        f"First seen: {row['first_ts']}   Last: {row['last_ts']}   ({dur} unanswered)\n\n"
        f"Latest detail: {(row['message'] or '')[:600]}\n\n"
        f"RUNBOOK: {DISPATCH_RUNBOOK}\n\n"
        f"Acceptance: dispatcher backlog moving again (spawned>0 or dispatchable=0 on the "
        f"next pass), this ticket closed. dispatch_recover.py's own reset() resolves aq#"
        f"{row['id']} the moment a healthy pass lands — do not resolve it by hand.\n"
    )
    cmd = [WQ_BIN, "add", "--lane", LANE, "--pri", "0", "--title", title, "--detail", detail]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ), timeout=30)
    except Exception as e:
        print(f"  seed_dispatch_ticket EXC (aq#{row['id']}): {e}")
        return None
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"added task (\d+)", out)
    if m:
        return int(m.group(1))
    m = re.search(r"DUPLICATE .* task (\d+)", out)
    if m:
        return int(m.group(1))
    print(f"  seed_dispatch_ticket FAILED (aq#{row['id']}): {out.strip()[:200]}")
    return None

def page_dispatch(row, ticket_id):
    dur = _dur(row["first_ts"], row["last_ts"])
    title = f"FLEET FIRE: dispatcher stalled x{row['count']} -> wq#{ticket_id}"
    body = (f"Dispatcher stalled {row['count']}x, recovery ladder exhausted, "
            f"no ack for {dur}.\nAuto-filed fleet-fires wq#{ticket_id}. "
            f"Claim: wq claim --id {ticket_id}\n{(row['message'] or '')[:300]}")
    return _send_page(row, ticket_id, title, body)

# wq#1487 (2026-09-08): STATE-BEFORE-ALERT. An open probe row with count>=3 is a
# HISTORY of failure, not proof the condition is live right now (a probe can pass at
# 19:05 while its row from 18:35 still sits open until the next edge-clear cycle, and
# the escalate timer runs every 10 min in between). Before seeding a fleet-fires
# ticket and paging Frank, re-run THAT probe live. PASS -> resolve the row here (same
# note shape as the sentinel edge-clear) and skip. FAIL or can't-run -> escalate as
# before (fail-closed: an unrunnable probe never suppresses a page).
LIVE_REPROBE = os.environ.get("AQ_ESCALATE_LIVE_REPROBE", "1") == "1"
SENTINEL_DIR = os.environ.get("AQ_ESCALATE_SENTINEL_DIR", "/home/lumen/sentinel/probes")

def live_probe(probe_name):
    """Returns (verdict, detail): verdict in {'PASS','FAIL','UNKNOWN'}."""
    if not LIVE_REPROBE:
        return "UNKNOWN", "live re-probe disabled"
    try:
        if SENTINEL_DIR not in sys.path:
            sys.path.insert(0, SENTINEL_DIR)
        import sentinel_probes as sp  # noqa: WPS433
        fn = next((f for n, f, _g in sp.PROBES if n == probe_name), None)
        if fn is None:
            return "UNKNOWN", f"no probe named {probe_name}"
        ok, detail, _crit = fn()
        return ("PASS" if ok else "FAIL"), str(detail)[:300]
    except Exception as e:  # noqa: BLE001 — fail closed toward escalating
        return "UNKNOWN", f"live re-probe crashed: {e!r}"[:300]

def _resolve_row(c, row, note):
    c.execute("UPDATE alerts SET status='resolved', note=COALESCE(note,'')||?, resolved_ts=? "
              "WHERE id=? AND status='open'", (" " + note, now(), row["id"]))
    c.commit()

def _escalate_rows(c, rows, seed_fn, page_fn, reprobe=True):
    """Shared idempotent escalate/page loop for one rule's eligible rows. Returns
    (n_ticket, n_paged, n_suppressed)."""
    n_ticket = n_paged = n_suppressed = 0
    for row in rows:
        esc = c.execute("SELECT * FROM escalations WHERE aq_id=?", (row["id"],)).fetchone()
        if esc and esc["paged_at"]:
            n_suppressed += 1
            continue  # already escalated AND paged -> suppress further repeats
        if reprobe and row["source"] == CRIT_SOURCE:
            verdict, detail = live_probe(row["key"])
            if verdict == "PASS":
                _resolve_row(c, row, f"[aq_escalate {now()}: live re-probe PASS ({detail[:120]}) "
                                     f"— not escalated, row auto-resolved (wq#1487)]")
                print(f"  re-probe PASS {row['key']} aq#{row['id']}: resolved, no ticket, no page")
                continue
            print(f"  re-probe {verdict} {row['key']} aq#{row['id']}: {detail[:100]} -> escalating")
        if not esc:
            try:
                c.execute("INSERT INTO escalations(aq_id,source,key,count_at_escalation,created_at) "
                          "VALUES(?,?,?,?,?)",
                          (row["id"], row["source"], row["key"], row["count"], now()))
                c.commit()
            except sqlite3.IntegrityError:
                pass  # concurrent insert won the race; fall through to re-read
            esc = c.execute("SELECT * FROM escalations WHERE aq_id=?", (row["id"],)).fetchone()
        ticket_id = esc["ticket_id"]
        if not ticket_id:
            ticket_id = seed_fn(row)
            if ticket_id is None:
                continue  # transient failure: retry next run, never page without a ticket
            c.execute("UPDATE escalations SET ticket_id=?, count_at_escalation=? WHERE aq_id=?",
                      (ticket_id, row["count"], row["id"]))
            # annotate the alert row for `aq list` visibility; status is never touched
            c.execute("UPDATE alerts SET owner='aq-escalation', "
                      "note=COALESCE(note,'')||? WHERE id=? AND status='open'",
                      (f" [escalated {now()} -> fleet-fires wq#{ticket_id}]", row["id"]))
            c.commit()
            n_ticket += 1
        if not esc["paged_at"]:
            if page_fn(row, ticket_id):
                c.execute("UPDATE escalations SET paged_at=? WHERE aq_id=?", (now(), row["id"]))
                c.commit()
                n_paged += 1
    return n_ticket, n_paged, n_suppressed

def run():
    c = db()
    rows = c.execute(
        "SELECT * FROM alerts WHERE source=? AND priority=? AND status='open' AND count>=? "
        "ORDER BY count DESC",
        (CRIT_SOURCE, CRIT_PRIORITY, THRESHOLD)).fetchall()
    n_ticket, n_paged, n_suppressed = _escalate_rows(c, rows, seed_ticket, page)

    # wq#1410 §3.3.3: title LIKE-matched (not source/priority scoped, the dispatcher's
    # own page uses ntfy:lumen-alerts + priority 5 like sentinel noise) so it never
    # collides with the probe rule above.
    dispatch_rows = c.execute(
        "SELECT * FROM alerts WHERE title LIKE ? AND status='open' AND count>=? "
        "ORDER BY count DESC",
        (DISPATCH_TITLE_LIKE, DISPATCH_THRESHOLD)).fetchall()
    dn_ticket, dn_paged, dn_suppressed = _escalate_rows(c, dispatch_rows,
                                                        seed_dispatch_ticket, page_dispatch)
    n_ticket += dn_ticket
    n_paged += dn_paged
    n_suppressed += dn_suppressed
    rows = list(rows) + list(dispatch_rows)

    c.close()
    print(f"aq-escalate: {len(rows)} eligible critical row(s); "
          f"{n_ticket} ticket(s) seeded, {n_paged} page(s) sent, {n_suppressed} already-suppressed")
    return 0

if __name__ == "__main__":
    sys.exit(run())
