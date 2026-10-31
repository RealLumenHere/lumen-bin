"""session-briefing (wq#4787): sections, owner grouping, 400-line cap trim order, masking,
fail-open on a missing DB, ingest-chats parsing/merge. Synthetic fixtures only — never the
live queue.db / minerva.db."""
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-briefing")
FAKE_PAT = "github_pat_" + "Q9" * 22          # built at runtime: never a literal token in the repo
NOW = datetime.now(timezone.utc)


def iso(delta_h):
    return (NOW - timedelta(hours=delta_h)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def wq_ts(delta_h):
    return (NOW - timedelta(hours=delta_h)).strftime("%Y-%m-%d %H:%M:%S")


def make_wq(path, tasks, grants=()):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, lane TEXT, pri INTEGER, title TEXT, status TEXT,"
              " claimed_by TEXT, claimed_at TEXT, block_reason TEXT, decision_prompt TEXT, notes TEXT, created_at TEXT)")
    c.execute("CREATE TABLE grant_requests(id INTEGER PRIMARY KEY, ticket_id INTEGER, class TEXT, target TEXT,"
              " state TEXT, requested_at TEXT, expires_at TEXT)")
    for t in tasks:
        row = dict(lane="build", pri=2, claimed_by=None, claimed_at=None, block_reason=None,
                   decision_prompt=None, notes=None, created_at=wq_ts(1))
        row.update(t)
        c.execute("INSERT INTO tasks(id,lane,pri,title,status,claimed_by,claimed_at,block_reason,decision_prompt,notes,created_at)"
                  " VALUES(:id,:lane,:pri,:title,:status,:claimed_by,:claimed_at,:block_reason,:decision_prompt,:notes,:created_at)", row)
    for g in grants:
        c.execute("INSERT INTO grant_requests(id,ticket_id,class,target,state,requested_at,expires_at) VALUES(?,?,?,?,?,?,?)", g)
    c.commit()
    c.close()


def make_minerva(path, notes=(), facts=(), decisions=()):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE archival_notes(id TEXT PRIMARY KEY, kind TEXT, title TEXT, content TEXT, source_ref TEXT,"
              " created_at TEXT, origin TEXT, revoked_at TEXT, superseded_by TEXT)")
    c.execute("CREATE TABLE state_facts(subject TEXT, predicate TEXT, value TEXT, ingested_at TEXT,"
              " source_kind TEXT, invalidated_at TEXT)")
    c.execute("CREATE TABLE fleet_decisions(id INTEGER PRIMARY KEY, decision TEXT, status TEXT, created_at TEXT)")
    for i, n in enumerate(notes):
        row = dict(id=f"n{i}", kind="session", content="", source_ref=None, origin="claude-session",
                   revoked_at=None, superseded_by=None)
        row.update(n)
        c.execute("INSERT INTO archival_notes VALUES(:id,:kind,:title,:content,:source_ref,:created_at,:origin,:revoked_at,:superseded_by)", row)
    for f in facts:
        c.execute("INSERT INTO state_facts VALUES(?,?,?,?,?,?)", f)
    for d in decisions:
        c.execute("INSERT INTO fleet_decisions VALUES(?,?,?,?)", d)
    c.commit()
    c.close()


MEMORY = """# Claude Extended Memory
## PERMANENT DECISIONS (never re-ask)
- NO Surface Pro refurbishment business integration. Strictly off-limits.
- Plain decision that is not an order.
## STANDING RULES
- note: Target /scratch or /data — NEVER root
- Target /scratch or /data — NEVER root
## FLEET FACTS
- description: Old worker pool is RETIRED (fact, not an order section)
## HAND PROSE (preserved across regen, wq#4537)
### 2026-10-20 old block
- STANDING ORDER: older order.
### 2026-10-27 M80 DEAD — final
- nothing imperative here
### 2026-10-28 newest block
- STANDING ORDER: newest order.
"""


@pytest.fixture
def env(tmp_path):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (tmp_path / "inbox").mkdir()
    mem = tmp_path / "claude_memory.md"
    mem.write_text(MEMORY)
    e = dict(os.environ, WQ_DB=str(tmp_path / "queue.db"), MINERVA_DB=str(tmp_path / "minerva.db"),
             BRIEFING_MEMORY_MD=str(mem), BRIEFING_CTX=str(ctx),
             BRIEFING_CHATS=str(ctx / "recent_chats.json"), BRIEFING_INBOX=str(tmp_path / "inbox"))
    return tmp_path, e


def run(e, *args, stdin=None):
    return subprocess.run([sys.executable, BIN, *args], env=e, input=stdin, capture_output=True, text=True, timeout=60)


def base_tasks():
    return [
        dict(id=10, title="gated thing", status="frank_gated", pri=1, decision_prompt="approve X?"),
        dict(id=11, title="orch work", status="claimed", claimed_by="orch-foo-123", claimed_at=wq_ts(2)),
        dict(id=12, title="worker work", status="claimed", claimed_by="fworker-wq12-abc", claimed_at=wq_ts(1)),
        dict(id=13, title="low blocked", status="blocked", pri=3, block_reason="CAPABILITY_DENIED low"),
        dict(id=14, title="high blocked", status="blocked", pri=0, block_reason="needs Frank"),
        dict(id=15, title="done thing", status="done", notes="handoff complete"),
        dict(id=16, title="open with handoff", status="open", notes="line one\nSESSION incomplete: step 3 not done"),
    ]


def test_sections_grouping_and_order(env):
    tmp, e = env
    make_wq(e["WQ_DB"], base_tasks(), grants=[(1, 11, "dir", "/home/lumen/x", "pending", wq_ts(1), None),
                                               (2, 11, "dir", "/old", "denied", wq_ts(1), None)])
    make_minerva(e["MINERVA_DB"], notes=[
        dict(title="recent ep", created_at=iso(1), content="## What changed\n- a.py\n\n## What failed\n- the probe\n"),
        dict(title="clean ep", created_at=iso(3), content="## What failed\n- (none)\n"),
        dict(title="ancient ep", created_at=iso(100)),
        dict(title="mirror noise", created_at=iso(1), origin="state-mirror"),
    ], facts=[("m80", "status", "DEAD", iso(5), "frank_stated", None),
              ("x", "y", "agent fact", iso(5), "agent_written", None)],
       decisions=[(7, "Thor is the only hard rule", "active", wq_ts(10))])
    r = run(e, "--stdout")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    heads = [out.index(f"## {n}.") for n in range(1, 6)]
    assert heads == sorted(heads)
    assert "wq#10 P1" in out and "approve X?" in out
    assert "1 pending grant request" in out and "grant#1 for wq#11" in out and "/old" not in out
    assert "orchestrator `orch-foo-123`: wq#11" in out
    assert "fworker `fworker-wq12-abc`: wq#12" in out
    assert out.index("wq#14 P0") < out.index("wq#13 P3")          # highest pri first
    assert "**FAILED:** the probe" in out and "changed: a.py" in out
    s3 = out[out.index("## 3."):out.index("## 4.")]
    assert "ancient ep" not in s3 and "mirror noise" not in s3         # 48h window; state-mirror excluded
    assert "m80.status = DEAD" in out and "agent fact" not in out
    assert "decision#7" in out
    assert "wq#16 P2 open" in out and "not done" in out
    assert "wq#15" not in out                                       # done tickets never listed as handoffs


def test_standing_orders_order_and_dedup(env):
    tmp, e = env
    make_wq(e["WQ_DB"], [])
    make_minerva(e["MINERVA_DB"])
    out = run(e, "--stdout").stdout
    sec = out[out.index("## 4."):out.index("## 5.")]
    assert sec.index("newest order") < sec.index("[2026-10-27 M80 DEAD — final]") < sec.index("older order")
    assert sec.count("NEVER root") == 1                              # note:/plain duplicate collapsed
    assert "Surface Pro" in sec
    assert "Plain decision" not in sec and "Old worker pool" not in sec   # non-orders / non-order sections


def test_line_cap_trims_oldest_episodes_and_lowest_pri_blocked_first(env):
    tmp, e = env
    tasks = [dict(id=1000 + i, title=f"blk{i}", status="blocked", pri=i % 5) for i in range(600)]
    make_wq(e["WQ_DB"], tasks)
    notes = [dict(title=f"ep{i:03d}", created_at=iso(i * 0.1)) for i in range(300)]
    make_minerva(e["MINERVA_DB"], notes=notes)
    out = run(e, "--stdout").stdout
    lines = out.splitlines()
    assert len(lines) <= 400
    assert "ep000" in out                                            # newest episode survives
    assert "ep299" not in out                                        # oldest trimmed
    blk = out[out.index("### Blocked"):out.index("## 3.")]
    assert " P0 " in blk and " P4 " not in blk                       # lowest pri trimmed first


def test_masking_and_fail_open_missing_db(env):
    tmp, e = env
    make_wq(e["WQ_DB"], [dict(id=1, title=f"leaked {FAKE_PAT} here", status="frank_gated")])
    e["MINERVA_DB"] = str(tmp / "absent" / "minerva.db")
    r = run(e)
    assert r.returncode == 0, r.stderr
    out = (tmp / "ctx" / "SESSION-BRIEFING.md").read_text()
    assert FAKE_PAT not in out and "<redacted>" in out
    assert "episodes unavailable" in out and "## 5." in out


def test_unwritable_out_keeps_previous(env):
    tmp, e = env
    make_wq(e["WQ_DB"], [])
    make_minerva(e["MINERVA_DB"])
    r = run(e, "--out", str(tmp / "nope" / "x.md"))
    assert r.returncode == 1 and "previous file kept" in r.stderr


RAW = (
    "<chat url='https://claude.ai/chat/aaaaaaaa-0000-0000-0000-000000000001' updated_at='2026-10-31T10:00:00+00:00'>\n"
    "Content:\nTitle: Fleet work\n\nH: fix the thing &amp; ship it\n\nA: ok</chat>\n\n"
    "<chat url=\"https://claude.ai/chat/aaaaaaaa-0000-0000-0000-000000000002\" updated_at=\"2026-10-30T10:00:00+00:00\" kind=\"summary\">"
    "Title: Summarised chat\nSummary: **Conversation Overview**\n\nThe session flashed two boards. More detail follows here.</chat>\n"
    "<chat url='https://claude.ai/chat/aaaaaaaa-0000-0000-0000-000000000003' updated_at='2026-10-29T10:00:00+00:00'>"
    "[Content redacted, chat flagged by safety classifier]</chat>\n"
    "<chat url='https://claude.ai/chat/aaaaaaaa-0000-0000-0000-000000000004' updated_at='2026-10-28T10:00:00+00:00'>"
    "Title: Old style\nHuman: token " + FAKE_PAT + " please\nAssistant: no</chat>")


def test_ingest_chats_raw_then_render(env):
    tmp, e = env
    make_wq(e["WQ_DB"], [])
    make_minerva(e["MINERVA_DB"], notes=[dict(title="proxy session", created_at=iso(2))])
    out = run(e, "--stdout").stdout
    assert "PROXY" in out and "proxy session" in out                 # no snapshot -> proxy
    r = run(e, "ingest-chats", stdin=RAW)
    assert r.returncode == 0, r.stderr
    snap_path = tmp / "ctx" / "recent_chats.json"
    assert stat.S_IMODE(os.stat(snap_path).st_mode) == 0o600
    snap = json.loads(snap_path.read_text())
    by = {c["uuid"][-1]: c for c in snap["chats"]}
    assert by["1"]["title"] == "Fleet work" and by["1"]["summary"] == "Frank: fix the thing & ship it"
    assert by["2"]["summary"] == "The session flashed two boards."
    assert by["3"]["title"].startswith("(content redacted")
    assert FAKE_PAT not in snap_path.read_text()
    out = run(e, "--stdout").stdout
    sec = out[out.index("## 1."):out.index("## 2.")]
    assert sec.index("Fleet work") < sec.index("Summarised chat") < sec.index("Old style")
    assert "PROXY (snapshot thin)" in sec                             # 4 fresh chats < 5 -> proxy, labelled thin
    # merge: re-ingest an update to chat 2 via JSON; chat 1 keeps its data
    upd = json.dumps([{"uuid": "aaaaaaaa-0000-0000-0000-000000000002", "title": "Renamed",
                       "updated_at": "2026-10-31T12:00:00+00:00", "summary": "New summary."}])
    assert run(e, "ingest-chats", stdin=upd).returncode == 0
    snap = json.loads(snap_path.read_text())
    assert snap["chats"][0]["title"] == "Renamed" and len(snap["chats"]) == 4


def test_ingest_chats_rejects_empty(env):
    tmp, e = env
    r = run(e, "ingest-chats", stdin="nothing useful here")
    assert r.returncode == 2
    assert not (tmp / "ctx" / "recent_chats.json").exists()


def test_ccc_export_merged(env):
    tmp, e = env
    make_wq(e["WQ_DB"], [])
    make_minerva(e["MINERVA_DB"])
    d = tmp / "inbox" / "bbbbbbbb-0000-0000-0000-000000000009"
    d.mkdir()
    (d / "conversation.json").write_text(json.dumps({
        "uuid": "bbbbbbbb-0000-0000-0000-000000000009", "name": "Exported chat",
        "updated_at": "2026-10-31T09:00:00+00:00", "summary": "**Conversation Overview**\n\nIt exported. Then more.",
        "chat_messages": []}))
    out = run(e, "--stdout").stdout
    assert "Exported chat" in out and "It exported." in out and "1 from CCC exports" in out
