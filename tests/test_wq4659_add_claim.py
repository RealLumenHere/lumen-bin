"""wq#4659 -- `wq add --claim` and the created_by/released_at columns.

An orchestrator filed wq#4645 and wq#4658 with `wq add` and built them itself; the dispatcher
spawned a duplicate fworker 60 s and 16 s after each add. `add --claim` writes the caller's hold
in the INSERT and then claims by id, so no pass ever sees the ticket open and unheld.
created_by feeds the dispatcher's orch-filed grace window (foreman dispatcher/gates.py
OrchFresh); `wq release` stamps released_at, which lifts that window.

Same zero-quota subprocess pattern as test_wq4113_dep_id_guard.py: every assertion drives the
real `wq` binary against a scratch DB under tmp_path.
"""
import os
import sqlite3
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))
ORCH = "orch-test-4659"


def make_env(tmp_path, agent=ORCH):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env.pop("WQ_AGENT", None)
    if agent:
        env["WQ_AGENT"] = agent
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def add(env, title, *extra):
    return wq(env, "add", "--lane", "auto", "--pri", "1", "--title", title,
              "--no-probe", "wq#4659 test fixture", *extra)


def added_id(p):
    return int(p.stdout.strip().split()[2])


def row(env, tid):
    con = sqlite3.connect(env["WQ_DB"])
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    finally:
        con.close()


def count(env):
    con = sqlite3.connect(env["WQ_DB"])
    try:
        return con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        con.close()


def test_add_claim_lands_claimed_by_caller(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "orch builds this itself", "--claim")
    assert p.returncode == 0, p.stdout + p.stderr
    r = row(env, added_id(p))
    assert (r["status"], r["claimed_by"], r["created_by"]) == ("claimed", ORCH, ORCH)
    assert r["hold_by"] is None and r["lease_expires"]


def test_add_claim_by_flag_overrides_env(tmp_path):
    env = make_env(tmp_path, agent=None)
    p = add(env, "claimed via --by", "--claim", "--by", "orch-other-4659")
    assert p.returncode == 0, p.stdout + p.stderr
    r = row(env, added_id(p))
    assert (r["claimed_by"], r["created_by"]) == ("orch-other-4659", "orch-other-4659")


def test_add_claim_without_identity_refused_before_insert(tmp_path):
    env = make_env(tmp_path, agent=None)
    add(make_env(tmp_path), "seed so the table exists")
    before = count(env)
    p = add(env, "no identity", "--claim")
    assert p.returncode == 1 and "--claim needs an identity" in p.stdout
    assert count(env) == before


def test_add_claim_frank_gated_refused(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "contradiction", "--claim", "--frank-gated")
    assert p.returncode == 1 and "contradict" in p.stdout


def test_plain_add_records_creator_and_leaves_ticket_open(tmp_path):
    env = make_env(tmp_path)
    r = row(env, added_id(add(env, "for a worker")))
    assert (r["status"], r["created_by"], r["hold_by"], r["released_at"]) == (
        "open", ORCH, None, None)


def test_plain_add_without_identity_has_no_creator(tmp_path):
    env = make_env(tmp_path, agent=None)
    r = row(env, added_id(add(env, "anonymous")))
    assert r["created_by"] is None


def test_refused_claim_keeps_the_insert_hold(tmp_path):
    """deps unmet: claim --id refuses (wq#582), but the hold written by the INSERT stays, so
    the dispatcher's Hold gate and every other claimer still keep off the ticket."""
    env = make_env(tmp_path)
    dep = added_id(add(env, "the dependency"))
    p = add(env, "blocked on the dep", "--claim", "--deps", str(dep))
    assert p.returncode == 1 and "REFUSED" in p.stdout
    tid = added_id(p)
    r = row(env, tid)
    assert (r["status"], r["hold_by"]) == ("open", ORCH) and r["hold_expires"]
    wq(env, "done", dep, "--notes", "fixture dep closed")
    other = wq(make_env(tmp_path, agent="fworker-wq4659-a1"), "claim", "--id", tid)
    assert other.returncode == 1 and f"held by '{ORCH}'" in other.stdout


def test_release_stamps_released_at(tmp_path):
    env = make_env(tmp_path)
    tid = added_id(add(env, "hand off to a worker", "--claim"))
    assert row(env, tid)["released_at"] is None
    p = wq(env, "release", tid)
    assert p.returncode == 0, p.stdout + p.stderr
    r = row(env, tid)
    assert r["status"] == "open" and r["released_at"]
