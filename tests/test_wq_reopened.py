"""`wq` reopened_count — wq#841 (escalated from wq#787's verify-pass of wq#763/PR#53).

Rework used to be measured as `notes LIKE '%REOPENED%'`, and `wq done` REPLACES notes
wholesale on every close. So a ticket that was reopened, marked, and then closed again lost
its marker and silently left the rework population — ~14 of ~18 markers evaporated inside 24h
and dragged lumen_queue_rework_rate_pct 2.43 -> 0.6 with no change in actual rework.

These tests pin the durable replacement: reopened_count is a monotonic column bumped on every
terminal -> in-flight transition, and NOTHING a later `wq done`/`wq edit` writes can erase it.
Same subprocess-against-a-scratch-DB pattern as test_wq_reap.py.
"""
import os
import sqlite3
import subprocess

# wq#886: overridable so the pre-commit hook can point this at the staged copy of `wq`
# instead of the live deployed binary — that's the whole point of gating on it.
WQ_BIN = os.environ.get("WQ_BIN", "/home/lumen/bin/wq")


def make_env(tmp_path, agent="test-reopen-agent"):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env)


def wq_add(env, title, lane="auto"):
    p = wq(env, "add", "--lane", lane, "--title", title)
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


def row(env, ticket_id):
    c = sqlite3.connect(env["WQ_DB"])
    cur = c.execute("SELECT status, notes, reopened_count, reopened_at FROM tasks WHERE id=?",
                    (ticket_id,))
    status, notes, count, at = cur.fetchone()
    c.close()
    return {"status": status, "notes": notes, "reopened_count": count, "reopened_at": at}


def close_it(env, ticket_id, notes):
    p = wq(env, "done", str(ticket_id), "--notes", notes, "--no-artifact", "scratch test")
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def test_fresh_ticket_starts_at_zero(tmp_path):
    env = make_env(tmp_path)
    tid = wq_add(env, "scratch never-reopened ticket")
    assert row(env, tid)["reopened_count"] == 0


def test_edit_status_open_on_a_done_ticket_counts_as_rework(tmp_path):
    env = make_env(tmp_path)
    tid = wq_add(env, "scratch reopen-by-edit ticket")
    wq(env, "claim", "--id", str(tid))
    close_it(env, tid, "first close")

    p = wq(env, "edit", str(tid), "--status", "open")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "REOPENED" in p.stdout, p.stdout        # never a silent event

    after = row(env, tid)
    assert after["status"] == "open"
    assert after["reopened_count"] == 1
    assert after["reopened_at"]


def test_reclose_overwrites_notes_but_never_the_count(tmp_path):
    """The exact wq#841 decay path: notes are wholesale-replaced on re-close, and the old
    substring metric lost the ticket right here."""
    env = make_env(tmp_path)
    tid = wq_add(env, "scratch decay-path ticket")
    wq(env, "claim", "--id", str(tid))
    close_it(env, tid, "first close")
    wq(env, "edit", str(tid), "--status", "open")
    wq(env, "claim", "--id", str(tid))
    close_it(env, tid, "REOPENED and fixed properly this time")

    marked = row(env, tid)
    assert "REOPENED" in marked["notes"]
    assert marked["reopened_count"] == 1

    # A third close with unrelated notes wipes the marker. The column does not move.
    wq(env, "edit", str(tid), "--status", "open")
    wq(env, "claim", "--id", str(tid))
    close_it(env, tid, "ordinary churn, no mention of the r-word")

    final = row(env, tid)
    assert "REOPENED" not in final["notes"]
    assert final["reopened_count"] == 2, "count must survive every later notes rewrite"


def test_release_of_a_done_ticket_counts_but_release_of_a_claim_does_not(tmp_path):
    env = make_env(tmp_path)
    live = wq_add(env, "scratch ordinary release ticket")
    wq(env, "claim", "--id", str(live))
    p = wq(env, "release", str(live))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "REOPENED" not in p.stdout
    assert row(env, live)["reopened_count"] == 0, "claimed -> open is a release, not rework"

    dead = wq_add(env, "scratch release-from-done ticket")
    wq(env, "claim", "--id", str(dead))
    close_it(env, dead, "closed")
    p = wq(env, "release", str(dead))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "REOPENED" in p.stdout
    assert row(env, dead)["reopened_count"] == 1


def test_rejected_to_open_counts_as_rework(tmp_path):
    env = make_env(tmp_path)
    tid = wq_add(env, "scratch rejected ticket")
    wq(env, "edit", str(tid), "--status", "rejected")
    assert row(env, tid)["reopened_count"] == 0, "entering a terminal state is not rework"
    wq(env, "edit", str(tid), "--status", "open")
    assert row(env, tid)["reopened_count"] == 1


def test_editing_other_fields_never_touches_the_count(tmp_path):
    env = make_env(tmp_path)
    tid = wq_add(env, "scratch untouched ticket")
    wq(env, "claim", "--id", str(tid))
    close_it(env, tid, "closed")
    wq(env, "edit", str(tid), "--status", "open")
    wq(env, "edit", str(tid), "--pri", "1")
    wq(env, "edit", str(tid), "--title", "retitled")
    wq(env, "edit", str(tid), "--status", "open")     # open -> open is not a reopen
    assert row(env, tid)["reopened_count"] == 1
