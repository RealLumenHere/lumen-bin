"""wq-hygiene-reconcile — kb/ISSUE-SOLUTIONS-fable.md #7 (wq#1257).

Three fixtures, per the spec's own test: done+merged (repaired), done+missing-evidence
(FRANK-flagged via ntfy, not touched), genuinely open (untouched). Same scratch-DB /
subprocess pattern as test_wq_reap.py: a real `wq` against a throwaway sqlite DB, plus a
throwaway git repo standing in for a "known repo" so sha-ancestor checks are real, not
mocked. ntfy-send is swapped for a stub that logs its argv instead of publishing.
"""
import json
import os
import sqlite3
import subprocess

WQ_BIN = os.environ.get("WQ_BIN", "/home/lumen/bin/wq")
RECONCILE_BIN = os.environ.get("RECONCILE_BIN", "/home/lumen/bin/wq-hygiene-reconcile")
FMT = "%Y-%m-%d %H:%M:%S"


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def make_repo_with_landed_commit(tmp_path):
    """A bare 'origin' plus a clone with one commit on main — stands in for a
    'known repo' wq-hygiene-reconcile searches for sha evidence. Returns (repo_dir, sha)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")

    repo = tmp_path / "known_repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("v1\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-qm", "landed work")
    git(repo, "branch", "-M", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha


def make_stub_ntfy(tmp_path):
    log = tmp_path / "ntfy.log"
    stub = tmp_path / "ntfy-send-stub"
    stub.write_text(f"#!/bin/bash\necho \"$@\" >> {log}\nexit 0\n")
    stub.chmod(0o755)
    return stub, log


def make_env(tmp_path, repos):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-hygiene-agent"
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env["WQ_HYGIENE_REPOS"] = ",".join(str(r) for r in repos)
    stub, log = make_stub_ntfy(tmp_path)
    env["WQ_HYGIENE_NTFY_BIN"] = str(stub)
    env["WQ_HYGIENE_STATE"] = str(tmp_path / "state.json")
    env["WQ_HYGIENE_STAMP"] = str(tmp_path / "stamp.json")
    env["GITHUB_TOKEN"] = ""
    env["GITEA_TOKEN_FILE"] = str(tmp_path / "no-such-token")
    return env, log


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
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def set_notes(db_path, ticket_id, notes):
    c = sqlite3.connect(db_path)
    c.execute("UPDATE tasks SET notes=? WHERE id=?", (notes, ticket_id))
    c.commit()
    c.close()


def run_reconcile(env, *extra):
    return subprocess.run([RECONCILE_BIN] + list(extra), capture_output=True, text=True,
                          env=env, timeout=60)


def test_open_ticket_with_landed_sha_is_repaired(tmp_path):
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done-but-open: work actually landed")
    set_notes(env["WQ_DB"], ticket_id,
             f"WIP — landed as {sha}, forgot to close the ticket")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REPAIR wq#{ticket_id}: repaired" in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "done"
    assert "reconciled-by=wq-hygiene" in after.get("notes", "")
    assert sha in after.get("notes", "")


def test_done_ticket_with_vanished_evidence_is_flagged_not_touched(tmp_path):
    repo, _sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done citing evidence that later vanished")
    # A sha-shaped citation `wq done` accepts at close time (shape-only gate, wq#759)
    # but that was never actually landed in any known repo — the rot this sweep exists
    # to catch.
    fake_sha = "deadbeefcafe0123456789abcdef0123456789"
    done = wq(env, "done", str(ticket_id), "--notes", f"closed, evidence {fake_sha}")
    assert done.returncode == 0, done.stdout + done.stderr
    before = wq_show(env, ticket_id)

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"FLAG wq#{ticket_id}: flagged" in result.stdout
    assert ntfy_log.exists()
    assert f"wq#{ticket_id}" in ntfy_log.read_text()

    after = wq_show(env, ticket_id)
    assert after["status"] == "done"
    assert after["notes"] == before["notes"]


def test_genuinely_open_ticket_untouched(tmp_path):
    repo, _sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "genuinely still open, no evidence of anything")
    set_notes(env["WQ_DB"], ticket_id, "still working on it, nothing to cite yet")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"wq#{ticket_id}" not in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "open"
    assert not ntfy_log.exists()


def test_flag_is_transition_based_not_repeated_every_run(tmp_path):
    repo, _sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done citing evidence that never verifies")
    fake_sha = "0000000cafe0123456789abcdef0123456789ab"
    wq(env, "done", str(ticket_id), "--notes", f"closed, evidence {fake_sha}")

    first = run_reconcile(env)
    assert f"FLAG wq#{ticket_id}: flagged" in first.stdout
    first_log = ntfy_log.read_text()

    second = run_reconcile(env)
    assert f"FLAG wq#{ticket_id}: flag-suppressed(no-transition)" in second.stdout
    assert ntfy_log.read_text() == first_log  # no new ntfy-send call


def test_dry_run_reports_without_mutating_or_alerting(tmp_path):
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "dry-run repair candidate")
    set_notes(env["WQ_DB"], ticket_id, f"landed as {sha}")

    result = run_reconcile(env, "--dry-run")
    assert f"REPAIR wq#{ticket_id}: would-repair" in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "open"
    assert not ntfy_log.exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "stamp.json").exists()


def test_live_claim_is_not_touched_even_with_landed_evidence(tmp_path):
    """A ticket under an active, unexpired claim is real in-flight work — wq's own
    owner_ok would refuse the close anyway, but the sweep should not even attempt it
    (or alert on it): the fence-guard is defense in depth, not the only guard."""
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "actively claimed, coincidentally sha-shaped note")
    claim = wq(env, "claim", "--id", str(ticket_id))
    assert claim.returncode == 0, claim.stdout + claim.stderr
    set_notes(env["WQ_DB"], ticket_id, f"progress note, not evidence: {sha}")

    result = run_reconcile(env)
    assert f"wq#{ticket_id}" not in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "claimed"
