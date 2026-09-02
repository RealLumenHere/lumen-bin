"""wq#1182 — slock audit: fence tokens on subject-level locks.

slock was already DB-backed (WAL + BEGIN IMMEDIATE, not a bare file lock) with a TTL and a
session-unique holder — two of the three properties wq#1158 hardened `wq` leases with. The
missing one was a fence: `acquire`/`extend`/`release` keyed on the holder STRING alone, and a
holder string is exactly what gets reused across an expire-then-regrant cycle, same zombie
shape wq#1158 closed for ticket claims.

Same zero-quota subprocess pattern as test_wq_lease_fence.py: every assertion drives the real
`slock` binary against a scratch DB under tmp_path, never the live queue.
"""
import datetime
import os
import sqlite3
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOCK_BIN = os.environ.get("SLOCK_BIN", os.path.join(REPO, "slock"))
FMT = "%Y-%m-%d %H:%M:%S"


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_slock.db")
    return env


def slock(env, *args):
    return subprocess.run([SLOCK_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def slock_ok(env, *args):
    p = slock(env, *args)
    assert p.returncode == 0, f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


def fence_from(stdout):
    for tok in stdout.split():
        if tok.startswith("FENCE="):
            return int(tok.split("=", 1)[1])
    return None


def expire(env, scope, minutes_ago=1):
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago)).strftime(FMT)
    c = sqlite3.connect(env["WQ_DB"])
    try:
        c.execute("UPDATE slocks SET expires_at=? WHERE scope=?", (past, scope))
        c.commit()
    finally:
        c.close()


def test_fresh_acquire_mints_fence_one(tmp_path):
    env = make_env(tmp_path)
    p = slock_ok(env, "acquire", "vault-ha", "--holder", "alice", "--ttl", "5m")
    assert fence_from(p.stdout) == 1


def test_same_holder_refresh_keeps_the_fence(tmp_path):
    env = make_env(tmp_path)
    p1 = slock_ok(env, "acquire", "vault-ha", "--holder", "alice", "--ttl", "5m")
    p2 = slock_ok(env, "acquire", "vault-ha", "--holder", "alice", "--ttl", "5m")
    assert "REFRESHED" in p2.stdout
    assert fence_from(p1.stdout) == fence_from(p2.stdout) == 1


def test_reap_then_regrant_bumps_the_fence_and_does_not_reset_it(tmp_path):
    """The counter must survive its own row's deletion. Resetting to 1 on regrant would
    erase the exact history a stale zombie's --fence needs refused against."""
    env = make_env(tmp_path)
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")
    expire(env, "vault-ha")
    p = slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")
    assert "ACQUIRED" in p.stdout
    assert fence_from(p.stdout) == 2


def test_stale_fence_refuses_release_exit_5(tmp_path):
    env = make_env(tmp_path)
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")
    expire(env, "vault-ha")
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")  # fence now 2
    p = slock(env, "release", "vault-ha", "--holder", "zombie", "--fence", "1")
    assert p.returncode == 5, p.stdout + p.stderr
    assert "stale fence" in p.stdout
    # the live lock must survive the refused release
    who = slock_ok(env, "who", "vault-ha")
    assert "vault-ha" in who.stdout


def test_stale_fence_refuses_extend_exit_5(tmp_path):
    env = make_env(tmp_path)
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")
    expire(env, "vault-ha")
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")  # fence now 2
    p = slock(env, "extend", "vault-ha", "--holder", "zombie", "--ttl", "5m", "--fence", "1")
    assert p.returncode == 5, p.stdout + p.stderr
    assert "stale fence" in p.stdout


def test_current_fence_release_succeeds(tmp_path):
    env = make_env(tmp_path)
    p = slock_ok(env, "acquire", "vault-ha", "--holder", "alice", "--ttl", "5m")
    fence = fence_from(p.stdout)
    slock_ok(env, "release", "vault-ha", "--holder", "alice", "--fence", str(fence))
    who = slock_ok(env, "who")
    assert "no active locks" in who.stdout


def test_current_fence_extend_succeeds(tmp_path):
    env = make_env(tmp_path)
    p = slock_ok(env, "acquire", "vault-ha", "--holder", "alice", "--ttl", "5m")
    fence = fence_from(p.stdout)
    p2 = slock_ok(env, "extend", "vault-ha", "--holder", "alice", "--ttl", "10m",
                  "--fence", str(fence))
    assert fence_from(p2.stdout) == fence


def test_omitted_fence_is_fully_backward_compatible(tmp_path):
    """No existing fleet caller passes --fence. Every legacy call shape must behave exactly
    as it did before this change, fence column present or not."""
    env = make_env(tmp_path)
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")
    expire(env, "vault-ha")
    slock_ok(env, "acquire", "vault-ha", "--holder", "zombie", "--ttl", "5m")  # fence now 2
    # no --fence given anywhere: extend and release both still just work
    slock_ok(env, "extend", "vault-ha", "--holder", "zombie", "--ttl", "5m")
    slock_ok(env, "release", "vault-ha", "--holder", "zombie")
    who = slock_ok(env, "who")
    assert "no active locks" in who.stdout


def test_who_reports_the_live_fence(tmp_path):
    env = make_env(tmp_path)
    slock_ok(env, "acquire", "vault-ha", "--holder", "alice", "--ttl", "5m")
    who = slock_ok(env, "who")
    assert "FENCE=1" in who.stdout
