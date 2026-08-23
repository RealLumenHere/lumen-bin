"""`wq reap` — RELIABILITY_SPEC.md §E / wq#647 (fq#614 fix): an independent sweep for
expired claimed-ticket leases, decoupled from any `wq claim` call. wq's own lazy
reclaim only runs as a side effect of somebody claiming in the SAME lane; these tests
force a lease into the past directly in the scratch DB (real 240m expiry is too slow
to wait out) and drive `wq reap` as its own subprocess, same zero-quota pattern as
test_fworker_integration.py.
"""
import datetime
import os
import sqlite3
import subprocess

# wq#886: overridable so the pre-commit hook can point this at the staged copy of `wq`
# instead of the live deployed binary — that's the whole point of gating on it.
WQ_BIN = os.environ.get("WQ_BIN", "/home/lumen/bin/wq")
FMT = "%Y-%m-%d %H:%M:%S"


def make_env(tmp_path, agent="test-reap-agent"):
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


def wq_show(env, ticket_id):
    p = wq(env, "show", str(ticket_id))
    out = {}
    for line in p.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def force_expire(db_path, ticket_id, minutes_ago=5):
    now = datetime.datetime.now(datetime.timezone.utc)
    past = (now - datetime.timedelta(minutes=minutes_ago)).strftime(FMT)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (past, ticket_id))
    c.commit()
    c.close()


def test_reap_reclaims_an_expired_claim_across_any_lane(tmp_path):
    env = make_env(tmp_path)
    ticket_id = wq_add(env, "scratch reap ticket")
    claim = wq(env, "claim", "--id", str(ticket_id))
    assert claim.returncode == 0, claim.stdout + claim.stderr
    assert wq_show(env, ticket_id)["status"] == "claimed"

    force_expire(env["WQ_DB"], ticket_id)

    result = wq(env, "reap")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REAPED #{ticket_id}" in result.stdout
    assert "reaped 1 ticket(s)" in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "open"
    assert after["claimed_by"] in (None, "", "None")


def test_reap_dry_run_reports_without_mutating(tmp_path):
    env = make_env(tmp_path)
    ticket_id = wq_add(env, "scratch dry-run reap ticket")
    wq(env, "claim", "--id", str(ticket_id))
    force_expire(env["WQ_DB"], ticket_id)

    result = wq(env, "reap", "--dry-run")
    assert f"REAPED #{ticket_id}" in result.stdout
    assert "dry-run" in result.stdout

    still_claimed = wq_show(env, ticket_id)
    assert still_claimed["status"] == "claimed"


def test_reap_lane_filter_leaves_other_lanes_alone(tmp_path):
    env = make_env(tmp_path)
    a_id = wq_add(env, "scratch lane-a ticket", lane="lane-a")
    b_id = wq_add(env, "scratch lane-b ticket", lane="lane-b")
    wq(env, "claim", "--id", str(a_id))
    wq(env, "claim", "--id", str(b_id))
    force_expire(env["WQ_DB"], a_id)
    force_expire(env["WQ_DB"], b_id)

    result = wq(env, "reap", "--lane", "lane-a")
    assert f"REAPED #{a_id}" in result.stdout
    assert f"REAPED #{b_id}" not in result.stdout

    assert wq_show(env, a_id)["status"] == "open"
    assert wq_show(env, b_id)["status"] == "claimed"


def test_reap_is_a_noop_on_a_live_lease(tmp_path):
    env = make_env(tmp_path)
    ticket_id = wq_add(env, "scratch live-lease ticket")
    wq(env, "claim", "--id", str(ticket_id))

    result = wq(env, "reap")
    assert "reaped 0 ticket(s)" in result.stdout
    assert wq_show(env, ticket_id)["status"] == "claimed"
