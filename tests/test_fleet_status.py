"""wq#1292 — fleet-status: whole fleet state in one call.

Two layers:
  1. Classifier regression fixtures — real block_reason text pulled verbatim from the
     live queue.db on 2026-09-02 (`sqlite3 queue.db "select block_reason from tasks
     where status='blocked'"`), frozen here so the classifier's behavior is pinned
     against real fleet vocabulary instead of invented examples. No ruleset existed
     anywhere in kb/ to reuse (verified: ISSUE-LOG.md's PERMISSION_LOOP/FALSE_COMPLETE/
     etc. mentions are narrative, not a mapping table) — these rules and their expected
     labels were derived by hand from that same 42-row sweep.
  2. A full-script smoke test against scratch WQ_DB/AQ_DB/logs/events/preflight
     fixtures (same env-override pattern as wq's WQ_DB / aq's AQ_DB), driving the real
     `fleet-status` binary via subprocess — this repo's convention for extension-less
     scripts (see test_instincts_render.py, test_aq_escalate.py) — never imported.
"""
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("FLEET_STATUS_BIN", os.path.join(HERE, "..", "fleet-status"))

TASKS_SCHEMA = """
CREATE TABLE tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, lane TEXT DEFAULT 'auto',
        pri INTEGER DEFAULT 2, title TEXT, detail TEXT, deps TEXT DEFAULT '',
        status TEXT DEFAULT 'open', claimed_by TEXT, claimed_at TEXT,
        done_at TEXT, notes TEXT DEFAULT '', created_at TEXT, lease_expires TEXT,
        subsystems TEXT DEFAULT '', gate TEXT DEFAULT '', lease_ttl INTEGER, model TEXT,
        effort TEXT, archetype TEXT, gate_type TEXT, decision_prompt TEXT,
        orch_recommendation TEXT, frank_response TEXT, response_ts TEXT,
        review_verdict TEXT, review_reason TEXT, block_reason TEXT, blocked_at TEXT,
        reopened_count INTEGER DEFAULT 0, reopened_at TEXT, lease_owner TEXT,
        lease_expires_at INTEGER, fence INTEGER DEFAULT 0, attempt INTEGER DEFAULT 0);
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

# --- 1. classifier regression fixtures (real text, verbatim) ---------------

CLASSIFY_CASES = [
    ("PERMISSION_LOOP",
     "HARNESS_DEFECT: PERMISSION_LOOP after 4 attempts; "
     "transcript=~/.claude/projects/*/33db1d69-2b5b-4aaf-8555-59d9e70049a1.jsonl"),
    ("crash-hang",
     "WORK_BLOCKED: HANG after 4 attempts; "
     "transcript=~/.claude/projects/*/421eeb75-e750-4d92-913f-d25be7180fe9.jsonl"),
    ("crash-hang",
     "WORK_BLOCKED: CRASH after 5 attempts; "
     "transcript=~/.claude/projects/*/6cf3c809-c38e-4220-8xxxxxxxxxxxxxx.jsonl"),
    ("FRANK",
     "FRANK-GATED: Cerebras 402 Payment Required (live probe 2026-08-27; last "
     "successful serve 2026-08-18 per SpendLogs)."),
    ("FRANK",
     "hardware-blocked: watch8 wireless debug ports closed (42533+34253 unreachable "
     "from Thor, ping OK). Need Frank to re-enable wireless debugging on watch to proceed."),
    ("FRANK",
     "CONTENT_DIVERGED needs a ref move on the PRIMARY tree, which is Frank-only. "
     "Verified: local main's only unique commit is byte-identical to origin/main's."),
    ("FRANK",
     "Needs Frank's physical hands: M70s has no OS/disks/tailnet node yet "
     "('sentinel' absent from tailscale status)."),
    ("FRANK",
     "Replacement fan ORDERED by Frank 2026-08-28; blocked on part arrival + physical "
     "swap. Thor stays on reduced airflow until then."),
    ("FALSE_COMPLETE",
     "Re-verified after orchestrator claimed PR#182/#189/#190 fixed the harness gap: "
     "lane/repo routing IS fixed ... adb Bash-allowlist gap is NOT fixed."),
    ("FALSE_COMPLETE",
     "The orchestrator's claim that PR#182/#189/#190 fixed 'the harness gap' does not "
     "apply here ... This is the 3rd confirmed instance of that false-fixed pattern."),
    ("sandbox",
     "Structurally impossible for a headless build worker: ticket needs (1) ssh to "
     "Isis+Nephthys to install afterg3-auto-boot unit, (2) actual power-down/power-up "
     "command execution (denied verbs), (3) smart-plug off/on cycling."),
    ("sandbox",
     "Confirmed hard blocker as documented: 'systemctl restart lumen-self-healer' "
     "returns 'This command requires approval', guard.sh denies systemctl restart "
     "outside the allowlist."),
    ("done-unlanded",
     "Fix is complete, verified and pushed as PR#12 "
     "(http://100.121.17.5:3000/lumen/questfree/pulls/12), but it cannot land "
     "independently. android/ does not exist on main."),
    ("infra",
     "Diagnosed, not fixable by this worker: qbit.lumennode.uk ORIGIN_DOWN is not "
     "dashboard/ingress drift -- cloudflared config.yml and glance.yml both already correct."),
    ("infra",
     "BGB-box candidates Mirror + PBox are BOTH GONE from the fleet (Frank 2026-08-28); "
     "no Windows box remains to run the emulator."),
    ("other", "duplicate of wq#1257 (same spec item: kb/ISSUE-SOLUTIONS-fable.md sec3 #7)."),
    ("other", ""),
    ("infra", "no hardware yet connected"),
]


@pytest.mark.parametrize("expected,text", CLASSIFY_CASES)
def test_classify(expected, text):
    p = subprocess.run([BIN, "--classify", text], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == expected, f"text={text!r} got={p.stdout!r}"


# --- 2. full-script smoke test against scratch fixtures ---------------------

STUB_LUMEN_RUN = """#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "list" ]; then
  echo "  lumen-job-fworker-wq42-1788360000.service loaded active running [systemd-run] /bin/bash -c \\"python3 fworker.py run --ticket 42\\""
fi
"""


def _now(offset_s=0):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + offset_s))


def _ts(offset_s=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


@pytest.fixture
def fleet(tmp_path):
    wq_db = tmp_path / "queue.db"
    aq_db = tmp_path / "alerts.db"
    logs_dir = tmp_path / "logs"
    events_dir = tmp_path / "events"
    preflight_dir = tmp_path / "preflight"
    for d in (logs_dir, events_dir, preflight_dir):
        d.mkdir()

    c = sqlite3.connect(wq_db)
    c.executescript(TASKS_SCHEMA)
    c.execute("INSERT INTO tasks(id,status,model,effort,archetype,attempt,claimed_at) "
              "VALUES(42,'claimed','claude-sonnet-5','high','standard-engineering',1,?)",
              (_now(-300),))
    c.execute("INSERT INTO tasks(id,status,block_reason) "
              "VALUES(99,'blocked','HARNESS_DEFECT: PERMISSION_LOOP after 4 attempts')")
    c.execute("INSERT INTO tasks(id,status,done_at,notes) "
              "VALUES(100,'done',?,'PR merged (squash, abc123)')", (_now(-600),))
    c.execute("INSERT INTO tasks(id,status,done_at,notes) "
              "VALUES(101,'done',?,'closed, no PR needed')", (_now(-600),))
    c.commit()
    c.close()

    c = sqlite3.connect(aq_db)
    c.executescript(ALERTS_SCHEMA)
    c.execute("INSERT INTO alerts(first_ts,last_ts,source,key,title,priority,status) "
              "VALUES(?,?,'probe','k','t','high','open')", (_now(), _now()))
    c.commit()
    c.close()

    (events_dir / "fworker-wq42-a1.jsonl").write_text(
        json.dumps({"ts": _ts(), "worker": "fworker-wq42-a1", "event": "start"}) + "\n")

    for name in (".stamp.wq-health", ".stamp.secret-check", ".stamp.log-watch", "model-check.log"):
        (logs_dir / name).write_text("ok\n")

    (preflight_dir / "testrepo.ok").write_text(json.dumps({
        "repo": "testrepo", "hash": "abc", "cmds": [], "proofed_at": _ts(-60)}))

    stub = tmp_path / "lumen-run"
    stub.write_text(STUB_LUMEN_RUN)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env.update({
        "WQ_DB": str(wq_db),
        "AQ_DB": str(aq_db),
        "FLEET_STATUS_LOGS_DIR": str(logs_dir),
        "FLEET_STATUS_EVENTS_DIR": str(events_dir),
        "FOREMAN_PREFLIGHT_STAMP_DIR": str(preflight_dir),
        "FOREMAN_LUMEN_RUN_BIN": str(stub),
    })
    return env


def test_smoke_json(fleet):
    p = subprocess.run([BIN, "--json"], capture_output=True, text=True, env=fleet, timeout=10)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)

    assert data["elapsed_s"] < 3.0

    workers = data["running"]["workers"]
    assert [w["ticket"] for w in workers] == [42]
    assert workers[0]["model"] == "claude-sonnet-5"
    assert workers[0]["last_event"] == "start"

    assert data["board"]["claimed"] == 1
    assert data["board"]["blocked"] == 1
    assert data["board"]["done"] == 2

    assert data["blocked"]["total"] == 1
    assert data["blocked"]["groups"] == {"PERMISSION_LOOP": [99]}

    assert data["landed"]["landed"] == [100]
    assert data["landed"]["done_unlanded"] == [101]

    assert data["alerts"]["open"] == 1
    assert data["alerts"]["by_priority"] == {"high": 1}

    assert data["day0"]["wq-health"]["state"] == "OK"
    assert data["day0"]["model-check"]["state"] == "OK"

    assert data["preflight"]["testrepo"]["state"] == "OK"

    assert data["pool"]["running_units"] == 1
    assert isinstance(data["load_mem"]["cores"], int)


def test_smoke_text_runs_fast(fleet):
    t0 = time.time()
    p = subprocess.run([BIN], capture_output=True, text=True, env=fleet, timeout=10)
    elapsed = time.time() - t0
    assert p.returncode == 0, p.stderr
    assert elapsed < 3.0
    assert "FLEET STATUS" in p.stdout
    assert "RUNNING" in p.stdout
    assert "POOL" in p.stdout


def test_missing_day0_stamp_reported_as_missing(fleet):
    """A detector that never ran (no stamp/log at all) must show MISSING, not OK —
    silently treating "file absent" as healthy would hide a dead cron job."""
    os.unlink(os.path.join(fleet["FLEET_STATUS_LOGS_DIR"], "model-check.log"))
    p = subprocess.run([BIN, "--json"], capture_output=True, text=True, env=fleet, timeout=10)
    data = json.loads(p.stdout)
    assert data["day0"]["model-check"]["state"] == "MISSING"
