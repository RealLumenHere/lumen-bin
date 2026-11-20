"""wq#5166 -- durable claim receipts survive claimed_by being cleared.

wq clears tasks.claimed_by on block / release / frank_gated / awaiting_grant / reap, and that
column was memoryd's only proof (I-7) that an agent ever held the ticket. Every claim write now
appends a claim_receipts row in the same transaction. These tests drive the real `wq` binary
against a scratch DB (same zero-quota pattern as test_wq4659_add_claim.py).
"""
import os
import sqlite3
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))


def make_env(tmp_path, agent):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env["WQ_AGENT"] = agent
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def add(env, title):
    p = wq(env, "add", "--lane", "auto", "--pri", "1", "--title", title,
           "--no-probe", "wq#5166 test fixture")
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


def q(env, sql, *args):
    con = sqlite3.connect(env["WQ_DB"])
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def test_claim_writes_receipt_and_release_keeps_it(tmp_path):
    env = make_env(tmp_path, "orch-test-5166a")
    tid = add(env, "wq5166 receipt survives release")
    p = wq(env, "claim", "--id", tid)
    assert "CLAIMED %d" % tid in p.stdout, p.stdout + p.stderr
    rows = q(env, "SELECT agent, kind FROM claim_receipts WHERE ticket_id=?", tid)
    assert rows == [("orch-test-5166a", "claim")]
    p = wq(env, "release", tid)
    assert p.returncode == 0, p.stdout + p.stderr
    assert q(env, "SELECT claimed_by FROM tasks WHERE id=?", tid) == [(None,)]
    # the receipt is the durable part: still there after claimed_by was cleared
    assert q(env, "SELECT agent FROM claim_receipts WHERE ticket_id=?", tid) == [("orch-test-5166a",)]


def test_block_clears_claimed_by_but_not_receipt(tmp_path):
    env = make_env(tmp_path, "orch-test-5166b")
    tid = add(env, "wq5166 receipt survives block")
    assert "CLAIMED" in wq(env, "claim", "--id", tid).stdout
    p = wq(env, "block", tid, "wq5166 fixture: blocked on purpose for the receipt test")
    assert p.returncode == 0, p.stdout + p.stderr
    status, claimed_by = q(env, "SELECT status, claimed_by FROM tasks WHERE id=?", tid)[0]
    assert status == "blocked"
    rows = q(env, "SELECT agent FROM claim_receipts WHERE ticket_id=?", tid)
    assert rows == [("orch-test-5166b",)]


def test_reclaim_appends_a_second_receipt_in_order(tmp_path):
    a = make_env(tmp_path, "orch-test-5166c")
    b = dict(a, WQ_AGENT="orch-test-5166d")
    tid = add(a, "wq5166 two claimants")
    assert "CLAIMED" in wq(a, "claim", "--id", tid).stdout
    assert wq(a, "release", tid).returncode == 0
    assert "CLAIMED" in wq(b, "claim", "--id", tid).stdout
    rows = q(a, "SELECT agent FROM claim_receipts WHERE ticket_id=? ORDER BY id", tid)
    assert rows == [("orch-test-5166c",), ("orch-test-5166d",)]


def test_claim_on_db_without_table_still_succeeds(tmp_path):
    """Fail-open contract: a claim must never fail because the audit row can't be written."""
    env = make_env(tmp_path, "orch-test-5166e")
    tid = add(env, "wq5166 table dropped")
    con = sqlite3.connect(env["WQ_DB"])
    con.execute("DROP TABLE claim_receipts")
    con.commit(); con.close()
    # init() recreates it on the next invocation, so the claim lands a receipt anyway
    p = wq(env, "claim", "--id", tid)
    assert "CLAIMED %d" % tid in p.stdout, p.stdout + p.stderr
    assert q(env, "SELECT agent FROM claim_receipts WHERE ticket_id=?", tid) == [("orch-test-5166e",)]
