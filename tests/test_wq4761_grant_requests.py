"""wq#4761 — grant_requests table, awaiting_grant status, wq await-grant / wq grant verbs.

agent-authority DESIGN.md §2.3 steps 3-5, §2.6 schema, §2.7 F4/F5/F8: a worker's held tool
call becomes a grant_requests row; fworker parks the claimed ticket (awaiting_grant — lease
released, dispatcher never claims it); approve re-opens it with resume_session kept; deny
blocks it with 'grant denied: <reason>'; `wq reap` expires whatever nobody decided in time;
`wq grant consume` is the atomic one-shot hold_gate calls on resume (two racers, one winner).

Same zero-quota subprocess pattern as test_wq_reap.py / test_wq4659_add_claim.py: every
assertion drives the real `wq` binary against a scratch DB under tmp_path.
"""
import datetime
import hashlib
import os
import sqlite3
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))
FMT = "%Y-%m-%d %H:%M:%S"
APPROVER = "orch-test-4761"


def fp(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def make_env(tmp_path, agent=APPROVER):
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


def with_agent(env, agent):
    e = dict(env)
    e["WQ_AGENT"] = agent
    return e


def add(env, title="grant fixture ticket"):
    p = wq(env, "add", "--lane", "auto", "--pri", "1", "--title", title,
           "--no-probe", "wq#4761 test fixture")
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


def task_row(env, tid):
    con = sqlite3.connect(env["WQ_DB"])
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    finally:
        con.close()


def grant_row(env, rid):
    con = sqlite3.connect(env["WQ_DB"])
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM grant_requests WHERE id=?", (rid,)).fetchone())
    finally:
        con.close()


def file_request(env, tid, seed, session=None):
    args = ["grant", "request", tid, "--fingerprint", fp(seed)]
    if session:
        args += ["--session", session]
    p = wq(env, *args)
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.split("id=")[1].split()[0])


def parked(env, tid, worker, seed="held-call"):
    """Claim as the worker, file a request, park the ticket. Returns the request id."""
    we = with_agent(env, worker)
    p = wq(we, "claim", "--id", tid)
    assert p.returncode == 0 and f"CLAIMED {tid}" in p.stdout, p.stdout + p.stderr
    rid = file_request(we, tid, seed, session=worker)
    p = wq(we, "await-grant", tid, "--req", rid, "--session", f"sess-{seed}")
    assert p.returncode == 0, p.stdout + p.stderr
    return rid


def backdate_grant(env, rid, minutes_ago=5):
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago)).strftime(FMT)
    con = sqlite3.connect(env["WQ_DB"])
    con.execute("UPDATE grant_requests SET expires_at=? WHERE id=?", (past, rid))
    con.commit()
    con.close()


def test_request_files_pending_row(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = file_request(env, tid, "r1", session="fworker-wq999-abc123")
    g = grant_row(env, rid)
    assert g["state"] == "pending" and g["ticket_id"] == tid
    assert g["fingerprint"] == fp("r1") and g["expires_at"]


def test_request_duplicate_fingerprint_returns_existing(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = file_request(env, tid, "dup")
    p = wq(env, "grant", "request", tid, "--fingerprint", fp("dup"))
    assert p.returncode == 0 and f"id={rid}" in p.stdout and "DUPLICATE" in p.stdout
    con = sqlite3.connect(env["WQ_DB"])
    n = con.execute("SELECT COUNT(*) FROM grant_requests").fetchone()[0]
    con.close()
    assert n == 1


def test_request_rejects_bad_fingerprint(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    p = wq(env, "grant", "request", tid, "--fingerprint", "not-a-sha256")
    assert p.returncode != 0 and "64-hex" in p.stderr


def test_await_grant_parks_ticket(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    worker = f"fworker-wq{tid}"
    rid = parked(env, tid, worker, seed="park")
    t = task_row(env, tid)
    assert t["status"] == "awaiting_grant"
    assert t["claimed_by"] is None and t["lease_expires"] is None
    assert t["resume_session"] == "sess-park" and t["grant_req"] == rid


def test_await_grant_requires_claimed_ticket(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = file_request(env, tid, "unclaimed")
    p = wq(env, "await-grant", tid, "--req", rid, "--session", "sess-x")
    assert p.returncode != 0 and "CLAIMED" in p.stderr


def test_awaiting_grant_never_claimed(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    parked(env, tid, f"fworker-wq{tid}", seed="noclaim")
    # lane scan: the parked ticket is the only one in the lane and must not be handed out
    p = wq(with_agent(env, "other-agent"), "claim", "--lane", "auto")
    assert f"CLAIMED {tid}" not in p.stdout
    assert task_row(env, tid)["status"] == "awaiting_grant"
    # direct claim --id: explicit refusal
    p = wq(with_agent(env, "other-agent"), "claim", "--id", tid)
    assert p.returncode != 0 and "AWAITING_GRANT" in p.stdout + p.stderr


def test_self_approval_refused(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    worker = f"fworker-wq{tid}"
    rid = parked(env, tid, worker, seed="selfok")
    # F8: the requesting fworker-wq<N> holder may not approve its own request
    p = wq(with_agent(env, worker), "grant", "approve", rid)
    assert p.returncode != 0 and "self-approval" in p.stderr
    assert grant_row(env, rid)["state"] == "pending"
    assert task_row(env, tid)["status"] == "awaiting_grant"


def test_approve_reopens_with_resume_session(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="approve")
    p = wq(env, "grant", "approve", rid)  # APPROVER identity, not the worker
    assert p.returncode == 0, p.stdout + p.stderr
    g = grant_row(env, rid)
    assert g["state"] == "approved" and g["decided_by"] == APPROVER and g["decided_at"]
    t = task_row(env, tid)
    assert t["status"] == "open"
    assert t["resume_session"] == "sess-approve" and t["grant_req"] == rid


def test_deny_blocks_with_reason(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="deny")
    p = wq(env, "grant", "deny", rid, "--reason", "target outside policy")
    assert p.returncode == 0, p.stdout + p.stderr
    assert grant_row(env, rid)["state"] == "denied"
    t = task_row(env, tid)
    assert t["status"] == "blocked"
    assert t["block_reason"] == "grant denied: target outside policy"


def test_deny_requires_reason(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="noreason")
    p = wq(env, "grant", "deny", rid)
    assert p.returncode != 0 and "--reason" in p.stderr
    assert grant_row(env, rid)["state"] == "pending"


def test_decided_request_cannot_be_redecided(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="redecide")
    assert wq(env, "grant", "deny", rid, "--reason", "no").returncode == 0
    p = wq(env, "grant", "approve", rid)
    assert p.returncode != 0 and "denied" in p.stderr


def test_reap_expires_stale_request_and_blocks_parked_ticket(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="reap")
    backdate_grant(env, rid)
    p = wq(env, "reap")
    assert p.returncode == 0, p.stdout + p.stderr
    assert f"GRANT_EXPIRED id={rid}" in p.stdout
    assert grant_row(env, rid)["state"] == "expired"
    t = task_row(env, tid)
    assert t["status"] == "blocked" and "expired" in t["block_reason"]


def test_reap_dry_run_mutates_nothing(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="dry")
    backdate_grant(env, rid)
    p = wq(env, "reap", "--dry-run")
    assert f"GRANT_EXPIRED id={rid}" in p.stdout and "dry-run" in p.stdout
    assert grant_row(env, rid)["state"] == "pending"
    assert task_row(env, tid)["status"] == "awaiting_grant"


def test_expired_approval_cannot_be_consumed(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="lateconsume")
    assert wq(env, "grant", "approve", rid).returncode == 0
    backdate_grant(env, rid)
    p = wq(env, "grant", "consume", "--fingerprint", fp("lateconsume"), "--lease", "lease-1")
    assert p.returncode == 3 and "expired" in p.stderr
    assert grant_row(env, rid)["state"] == "approved"  # reap, not consume, flips it


def test_consume_is_one_shot(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="oneshot")
    assert wq(env, "grant", "approve", rid).returncode == 0
    p1 = wq(env, "grant", "consume", "--fingerprint", fp("oneshot"), "--lease", "lease-a")
    assert p1.returncode == 0 and "GRANT_CONSUMED" in p1.stdout
    g = grant_row(env, rid)
    assert g["state"] == "consumed" and g["consumed_lease"] == "lease-a" and g["consumed_at"]
    p2 = wq(env, "grant", "consume", "--fingerprint", fp("oneshot"), "--lease", "lease-b")
    assert p2.returncode == 4 and "already consumed" in p2.stderr
    assert grant_row(env, rid)["consumed_lease"] == "lease-a"


def test_double_consume_race_exactly_one_winner(tmp_path):
    """F5: two resumes race for one approval — the atomic UPDATE lets exactly one win."""
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="race")
    assert wq(env, "grant", "approve", rid).returncode == 0
    procs = [subprocess.Popen(
        [WQ_BIN, "grant", "consume", "--fingerprint", fp("race"), "--lease", f"racer-{i}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for i in range(2)]
    rcs = [pr.wait(timeout=60) for pr in procs]
    assert sorted(rcs).count(0) == 1, f"exactly one racer must win, got rcs={rcs}"
    g = grant_row(env, rid)
    assert g["state"] == "consumed" and g["consumed_lease"] in ("racer-0", "racer-1")


def test_pending_request_not_consumable(tmp_path):
    env = make_env(tmp_path)
    tid = add(env)
    file_request(env, tid, "pending-consume")
    p = wq(env, "grant", "consume", "--fingerprint", fp("pending-consume"))
    assert p.returncode == 2 and "not approved" in p.stderr


def test_grant_list_filters(tmp_path):
    env = make_env(tmp_path)
    t1, t2 = add(env, "t1"), add(env, "t2")
    r1 = file_request(env, t1, "l1")
    file_request(env, t2, "l2")
    p = wq(env, "grant", "list", "--ticket", t1)
    assert f"id={r1} ticket={t1}" in p.stdout and "1 grant request(s)" in p.stdout
    p = wq(env, "grant", "list", "--state", "pending")
    assert "2 grant request(s)" in p.stdout


def test_release_clears_resume_session(tmp_path):
    """F7: a released ticket returns to the pool for a FRESH attempt — the stored resume
    session and grant pointer must not survive into it."""
    env = make_env(tmp_path)
    tid = add(env)
    rid = parked(env, tid, f"fworker-wq{tid}", seed="f7")
    assert wq(env, "grant", "approve", rid).returncode == 0
    assert task_row(env, tid)["resume_session"] == "sess-f7"
    p = wq(with_agent(env, "fworker-wq%d" % tid), "release", tid)
    assert p.returncode == 0, p.stdout + p.stderr
    t = task_row(env, tid)
    assert t["status"] == "open" and t["resume_session"] is None and t["grant_req"] is None
