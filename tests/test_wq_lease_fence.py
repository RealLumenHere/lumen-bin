"""wq#1158 — atomic claim, fencing tokens, lease reclaim, and the idempotency helper.

Same zero-quota subprocess pattern as test_wq_reap.py: every assertion drives the real `wq`
binary against a scratch DB under tmp_path, so what is tested is the shipped CLI surface and
not an in-process reimplementation of it. The live queue is never opened.

The migration is exercised as its own script, both directions, because "reversible and
idempotent" is a claim about the script and not about the columns.
"""
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# wq#886: overridable so the pre-commit hook can point this at the staged copy of `wq`.
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))
MIGRATION = os.path.join(REPO, "migrations", "wq1158_lease_fence.py")
FMT = "%Y-%m-%d %H:%M:%S"


def make_env(tmp_path, agent="test-fence-agent"):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    # wq#912: scratch lane names are never real dispatcher lanes — point the lane guard at a
    # path that does not exist so it fails open instead of consulting the live config.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env["WQ_SCRATCH_ROOT"] = str(tmp_path / "scratch")
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def wq_ok(env, *args):
    p = wq(env, *args)
    assert p.returncode == 0, f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


def add(env, title, lane="fence-lane", pri=2):
    p = wq_ok(env, "add", "--lane", lane, "--pri", pri, "--title", title)
    return int(p.stdout.strip().split()[2])


def migrate(env, *extra):
    return subprocess.run([sys.executable, MIGRATION, "--db", env["WQ_DB"]] + list(extra),
                          capture_output=True, text=True, env=env)


def db(env):
    return sqlite3.connect(env["WQ_DB"], timeout=30)


def row(env, tid, *cols):
    c = db(env)
    try:
        return c.execute(f"SELECT {','.join(cols)} FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        c.close()


def fence_from(stdout):
    for line in stdout.splitlines():
        if line.startswith("FENCE="):
            return int(line.split("=", 1)[1])
    return None


def expire(env, tid, minutes_ago=5):
    """Force a lease into the past. A real 240m expiry is too slow to wait out, and BOTH
    columns have to move or the claim predicate (which requires both to agree the lease is
    dead) correctly declines to reclaim."""
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    c = db(env)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        if "lease_expires_at" in cols:
            c.execute("UPDATE tasks SET lease_expires=?, lease_expires_at=? WHERE id=?",
                      (past.strftime(FMT), int(past.timestamp()), tid))
        else:
            c.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (past.strftime(FMT), tid))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def q(tmp_path):
    """A migrated scratch queue."""
    env = make_env(tmp_path)
    wq_ok(env, "list")                       # init() creates the DB
    p = migrate(env, "--yes")
    assert p.returncode == 0, p.stdout + p.stderr
    return env


# ---------------------------------------------------------------- migration


def test_migration_is_idempotent_and_reports_before_it_writes(tmp_path):
    env = make_env(tmp_path)
    wq_ok(env, "list")
    dry = migrate(env)
    assert dry.returncode == 0
    assert "report only" in dry.stdout
    assert "lease_owner" in dry.stdout
    # a report must not have written anything
    c = db(env)
    assert "lease_owner" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    c.close()

    first = migrate(env, "--yes")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "APPLIED." in first.stdout
    second = migrate(env, "--yes")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "ALREADY APPLIED" in second.stdout


def test_migration_backfills_an_existing_claim(tmp_path):
    env = make_env(tmp_path)
    tid = add(env, "pre-migration ticket")
    wq_ok(env, "claim", "--lane", "fence-lane")
    before = row(env, tid, "claimed_by", "lease_expires")
    assert before[0] and before[1]

    assert migrate(env, "--yes").returncode == 0
    owner, exp_at, fence, attempt = row(env, tid, "lease_owner", "lease_expires_at",
                                        "fence", "attempt")
    assert owner == before[0], "lease_owner must mirror the holder already on the row"
    expect = int(datetime.datetime.strptime(before[1], FMT)
                 .replace(tzinfo=datetime.timezone.utc).timestamp())
    assert exp_at == expect, "the epoch mirror is computed from the lease already on the row"
    assert fence == 0, "a pre-migration claim starts at 0 so the next claim mints 1"
    assert attempt == 1


def test_migration_rollback_drops_the_columns_and_re_migrates(q):
    back = migrate(q, "--rollback", "--yes")
    assert back.returncode == 0, back.stdout + back.stderr
    c = db(q)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    c.close()
    assert not {"lease_owner", "lease_expires_at", "fence", "attempt"} & cols
    # and wq still works with the columns gone — that is the whole point of feature detection
    tid = add(q, "after rollback")
    p = wq_ok(q, "claim", "--lane", "fence-lane")
    assert f"CLAIMED {tid}" in p.stdout
    assert fence_from(p.stdout) is None
    assert migrate(q, "--yes").returncode == 0


def test_migration_refuses_a_non_wq_db(tmp_path):
    junk = tmp_path / "not-a-queue.db"
    c = sqlite3.connect(junk)
    c.execute("CREATE TABLE unrelated(x INTEGER)")
    c.commit()
    c.close()
    env = make_env(tmp_path)
    env["WQ_DB"] = str(junk)
    p = migrate(env, "--yes")
    assert p.returncode == 2
    assert "not a wq queue DB" in p.stdout


# ---------------------------------------------------------------- atomic claim


def test_claim_mints_a_monotonic_fence(q):
    tid = add(q, "fence minting")
    first = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    assert first == 1
    wq_ok(q, "release", tid)
    second = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    assert second == 2, "release must not reset the fence — that is what makes it a fence"
    assert row(q, tid, "attempt")[0] == 2


def test_concurrent_claimers_never_double_claim(q):
    """Eight claimers, one ticket. Exactly one CLAIMED, and nobody else ends up recorded as
    the holder. Run against distinct agent names because wq#502 refuses a second live session
    sharing a base name before the claim protocol is even reached."""
    tid = add(q, "the only ticket in the lane")

    def claimer(n):
        env = dict(q)
        env["WQ_AGENT"] = f"racer-{n}"
        return wq(env, "claim", "--lane", "fence-lane")

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(claimer, range(8)))

    winners = [r for r in results if f"CLAIMED {tid}" in r.stdout]
    assert len(winners) == 1, "\n\n".join(f"rc={r.returncode}\n{r.stdout}" for r in results)
    holder, fence, attempt = row(q, tid, "claimed_by", "fence", "attempt")
    assert holder.startswith("racer-")
    assert f"CLAIMED {tid} (pri 2) by {holder}" in winners[0].stdout
    assert fence == 1, "exactly one claim landed, so exactly one fence bump"
    assert attempt == 1


def test_claim_by_id_races_resolve_to_one_winner(q):
    tid = add(q, "contended by id")

    def claimer(n):
        env = dict(q)
        env["WQ_AGENT"] = f"idracer-{n}"
        return wq(env, "claim", "--id", tid)

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(claimer, range(6)))
    assert len([r for r in results if f"CLAIMED {tid}" in r.stdout]) == 1
    assert row(q, tid, "fence")[0] == 1


def test_expired_lease_is_reclaimed_and_bumps_the_fence(q):
    tid = add(q, "abandoned by its holder")
    first = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    expire(q, tid)

    env = dict(q)
    env["WQ_AGENT"] = "successor"
    p = wq_ok(env, "claim", "--lane", "fence-lane")
    assert f"RECLAIMED #{tid}" in p.stdout
    assert f"CLAIMED {tid}" in p.stdout
    assert fence_from(p.stdout) == first + 1
    owner, holder = row(q, tid, "lease_owner", "claimed_by")
    assert owner == "successor" == holder


def test_a_live_lease_is_never_reclaimed(q):
    tid = add(q, "held and healthy")
    wq_ok(q, "claim", "--lane", "fence-lane")
    env = dict(q)
    env["WQ_AGENT"] = "interloper"
    p = wq(env, "claim", "--lane", "fence-lane")
    assert f"CLAIMED {tid}" not in p.stdout
    assert row(q, tid, "fence")[0] == 1


def test_the_epoch_mirror_tracks_lease_expires_through_a_heartbeat(q):
    """The claim predicate reads lease_expires_at; every writer of lease_expires must move it
    too, or a live ticket eventually looks reclaimable."""
    tid = add(q, "mirror tracking")
    f = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    wq_ok(q, "heartbeat", tid, "--fence", f)
    exp, exp_at = row(q, tid, "lease_expires", "lease_expires_at")
    assert exp_at == int(datetime.datetime.strptime(exp, FMT)
                         .replace(tzinfo=datetime.timezone.utc).timestamp())


# ---------------------------------------------------------------- fencing


def test_stale_fence_is_refused_on_every_write_back(q):
    """The zombie case end to end, and deliberately the version wq#1116's ownership guard
    CANNOT see: the successor runs under the SAME agent name, because that is what a respawned
    fworker is. owner_ok compares names, both processes answer to the same one, so it waves the
    zombie straight through. The fence is the only thing left standing between them."""
    tid = add(q, "the ticket a zombie wakes up holding")
    stale = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    expire(q, tid)
    live = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)   # same WQ_AGENT
    assert live == stale + 1

    for args in (["heartbeat", tid],
                 ["done", tid, "--notes", "zombie close", "--no-artifact", "zombie"],
                 ["release", tid],
                 ["block", tid, "zombie block"]):
        p = wq(q, *(args + ["--fence", stale]))
        assert p.returncode == 5, f"{args[0]} should refuse a stale fence:\n{p.stdout}{p.stderr}"
        assert "stale fence" in p.stdout
    # ...and the ticket is exactly as the successor left it
    status, holder, fence = row(q, tid, "status", "claimed_by", "fence")
    assert (status, holder, fence) == ("claimed", q["WQ_AGENT"], live)


def test_without_a_fence_the_same_zombie_write_lands(q):
    """The counterfactual that makes the test above mean something. Identical setup, fence
    omitted: every ownership guard passes and the stale close overwrites the live claim. This
    is the bug wq#1158 exists to close, pinned so a future refactor cannot quietly reopen it
    by making --fence a no-op."""
    tid = add(q, "unfenced zombie")
    wq_ok(q, "claim", "--lane", "fence-lane")
    expire(q, tid)
    wq_ok(q, "claim", "--lane", "fence-lane")
    p = wq(q, "done", tid, "--notes", "zombie close", "--no-artifact", "zombie")
    assert p.returncode == 0, p.stdout + p.stderr
    assert row(q, tid, "status")[0] == "done"


def test_the_current_fence_is_accepted(q):
    tid = add(q, "happy path")
    f = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    wq_ok(q, "heartbeat", tid, "--fence", f)
    wq_ok(q, "done", tid, "--notes", "closed", "--no-artifact", "unit test")
    assert row(q, tid, "status")[0] == "done"


def test_a_zombie_cannot_extend_a_lease_on_its_way_to_being_refused(q):
    """mark_alive PROMOTES leases as a side effect of any wq call, so the fence check has to
    land before it — otherwise a refused heartbeat still lengthens the successor's lease."""
    tid = add(q, "promotion ordering")
    stale = fence_from(wq_ok(q, "claim", "--lane", "fence-lane").stdout)
    expire(q, tid)
    wq_ok(q, "claim", "--lane", "fence-lane")
    before = row(q, tid, "lease_expires")[0]
    assert wq(q, "heartbeat", tid, "--fence", stale).returncode == 5
    assert row(q, tid, "lease_expires")[0] == before


def test_fence_is_refused_rather_than_ignored_on_an_unmigrated_db(tmp_path):
    """Silently ignoring a fence is the worst option available: the caller believes it is
    protected and is not."""
    env = make_env(tmp_path)
    tid = add(env, "unmigrated")
    p = wq_ok(env, "claim", "--lane", "fence-lane")
    assert fence_from(p.stdout) is None
    r = wq(env, "heartbeat", tid, "--fence", 1)
    assert r.returncode == 5
    assert "has NOT had the wq#1158 migration applied" in r.stdout


def test_omitting_the_fence_still_works(q):
    """--fence is opt-in. A worker that predates it keeps working, which is what makes the
    migration deployable without draining the queue first."""
    tid = add(q, "no fence passed")
    wq_ok(q, "claim", "--lane", "fence-lane")
    wq_ok(q, "heartbeat", tid)
    wq_ok(q, "done", tid, "--notes", "closed", "--no-artifact", "unit test")
    assert row(q, tid, "status")[0] == "done"


# ---------------------------------------------------------------- prior guards, unregressed


def test_wq1116_ownership_guard_still_refuses_a_foreign_write(q):
    tid = add(q, "owned by someone else")
    wq_ok(q, "claim", "--lane", "fence-lane")
    other = dict(q)
    other["WQ_AGENT"] = "not-the-holder"
    other["WQ_STRICT"] = "1"
    p = wq(other, "done", tid, "--notes", "n", "--no-artifact", "x")
    assert p.returncode != 0
    assert row(q, tid, "status")[0] == "claimed"


def test_wq759_evidence_gate_still_refuses_a_bare_close(q):
    tid = add(q, "needs evidence")
    wq_ok(q, "claim", "--lane", "fence-lane")
    p = wq(q, "done", tid, "--notes", "all finished")
    assert p.returncode == 2
    # wq#1479 (wq:490-495): a refusal is an error, so the whole close path prints REFUSED on
    # stderr via eprint(). An orchestrator reading `tail -1` of stdout must see nothing.
    assert "wq#759 evidence gate" in p.stderr, p.stdout + p.stderr
    assert "REFUSED" not in p.stdout, p.stdout


# ---------------------------------------------------------------- idempotency helper


def test_idem_branch_is_deterministic_and_lumen_pr_shaped(q):
    tid = add(q, "[DRAFT] Wq LEASE hardening: atomic claim + fencing!!")
    a = wq_ok(q, "idem", "branch", tid).stdout.strip()
    b = wq_ok(q, "idem", "branch", tid).stdout.strip()
    assert a == b
    assert a.startswith(f"feat/wq{tid}-")
    # lumen-pr BRANCH_RE: ^(<types>)/[a-z0-9][a-z0-9-]*$ — the bracketed prefix, the colon,
    # the plus and the bangs all have to be gone or the branch cannot be pushed.
    import re
    assert re.match(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
                    r"/[a-z0-9][a-z0-9-]*$", a), a
    assert wq_ok(q, "idem", "branch", tid, "--type", "fix").stdout.strip().startswith("fix/")
    assert wq(q, "idem", "branch", tid, "--type", "nope").returncode == 2


def test_idem_path_is_ticket_keyed_not_session_keyed(q):
    tid = add(q, "output dir")
    mine = wq_ok(q, "idem", "path", tid).stdout.strip()
    other = dict(q)
    other["WQ_AGENT"] = "a-completely-different-worker"
    assert wq_ok(other, "idem", "path", tid).stdout.strip() == mine
    assert mine.endswith(f"wq{tid}")


def _gh_stub(tmp_path, payload, rc=0):
    stub = tmp_path / "gh-stub"
    stub.write_text(f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\nexit {rc}\n")
    stub.chmod(0o755)
    return str(stub)


def test_idem_pr_reports_an_existing_pr_instead_of_making_a_second(q, tmp_path):
    tid = add(q, "already has a pr")
    env = dict(q)
    env["WQ_GH_BIN"] = _gh_stub(tmp_path, json.dumps(
        [{"url": "https://github.com/o/r/pull/7", "state": "OPEN", "number": 7}]))
    p = wq(env, "idem", "pr", tid)
    assert p.returncode == 0
    assert "EXISTS https://github.com/o/r/pull/7" in p.stdout
    assert "do NOT open a second one" in p.stdout


def test_idem_pr_reports_absent_so_the_caller_creates_one(q, tmp_path):
    tid = add(q, "no pr yet")
    env = dict(q)
    env["WQ_GH_BIN"] = _gh_stub(tmp_path, "[]")
    p = wq(env, "idem", "pr", tid)
    assert p.returncode == 3
    assert p.stdout.startswith("ABSENT feat/wq")


def test_idem_pr_forge_failure_is_unknown_and_never_absent(q, tmp_path):
    """An unanswered check must not read as 'no PR exists' — that is precisely how the
    duplicate PR gets opened."""
    tid = add(q, "forge is down")
    env = dict(q)
    env["WQ_GH_BIN"] = _gh_stub(tmp_path, "boom", rc=1)
    p = wq(env, "idem", "pr", tid)
    assert p.returncode == 4
    assert p.stdout.startswith("UNKNOWN")
