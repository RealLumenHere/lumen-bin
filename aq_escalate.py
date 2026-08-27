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

Env (tests/extension): AQ_DB, WQ_DB, AQ_ESCALATE_THRESHOLD, AQ_ESCALATE_SOURCE,
AQ_ESCALATE_PRIORITY, AQ_ESCALATE_NTFY_URL, AQ_ESCALATE_NTFY_CAPTURE (file: no
network), AQ_WQ_BIN.
"""
import os, sys, json, re, sqlite3, subprocess, urllib.request, datetime

AQ_DB     = os.environ.get("AQ_DB", "/home/lumen/agents/alert-queue/alerts.db")
WQ_BIN    = os.environ.get("AQ_WQ_BIN", "/home/lumen/bin/wq")
THRESHOLD = int(os.environ.get("AQ_ESCALATE_THRESHOLD", "3"))
CRIT_SOURCE   = os.environ.get("AQ_ESCALATE_SOURCE", "probe")
CRIT_PRIORITY = os.environ.get("AQ_ESCALATE_PRIORITY", "high")
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
        print(f"  ntfy page FAILED for {probe} (aq#{row['id']}): {e}")
        return False

def run():
    c = db()
    rows = c.execute(
        "SELECT * FROM alerts WHERE source=? AND priority=? AND status='open' AND count>=? "
        "ORDER BY count DESC",
        (CRIT_SOURCE, CRIT_PRIORITY, THRESHOLD)).fetchall()
    n_ticket = n_paged = n_suppressed = 0
    for row in rows:
        esc = c.execute("SELECT * FROM escalations WHERE aq_id=?", (row["id"],)).fetchone()
        if esc and esc["paged_at"]:
            n_suppressed += 1
            continue  # already escalated AND paged -> suppress further repeats
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
            ticket_id = seed_ticket(row)
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
            if page(row, ticket_id):
                c.execute("UPDATE escalations SET paged_at=? WHERE aq_id=?", (now(), row["id"]))
                c.commit()
                n_paged += 1
    c.close()
    print(f"aq-escalate: {len(rows)} eligible critical row(s); "
          f"{n_ticket} ticket(s) seeded, {n_paged} page(s) sent, {n_suppressed} already-suppressed")
    return 0

if __name__ == "__main__":
    sys.exit(run())
