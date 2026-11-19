"""wq#5132: `wq done` must not accept edits that never landed.

Class from wq#5029: wq#4905 cited /home/lumen/foreman/harness/action_gate.py and wq#4912 cited
.../claude_settings_audit.py. Both files were TRACKED on origin/main, so the wq#1673 durability
gate called them 'durable ... on origin/main' and the wq#1702 landing gate called them landed,
while the actual edits sat uncommitted in the primary tree (and failed 4 tests once landed).

These drive the real `wq` binary (this checkout's copy) against a scratch DB and throwaway repos:
  * a dirty tracked file cited as evidence            -> rc 6  (durability: bytes not on main)
  * a dirty tracked file the TICKET names             -> rc 11 (unlanded-edit gate)
  * a ticket-named file cited but untouched since filing -> rc 11; --unlanded overrides
  * a ticket-named file changed on main after filing  -> closes
  * a commit on main naming wq#<id>                   -> closes
  * a primary tree merely BEHIND main                 -> closes (old bytes did land)
  * a linked-worktree branch edit of a landed file    -> rc 6
"""
import os
import subprocess
import time

import pytest

WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")

OLD = "2020-01-01T00:00:00+00:00"


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-wq5132-agent"
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    return env


def wq(env, *args, cwd=None):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env,
                          cwd=str(cwd) if cwd else None)


def git(cwd, *args, date=None):
    env = dict(os.environ)
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True,
                       env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def open_ticket(env, title, detail=""):
    extra = ["--detail", detail] if detail else []
    p = wq(env, "add", "--lane", "auto", "--title", title, *extra)
    assert p.returncode == 0, p.stdout + p.stderr
    tid = str(int(p.stdout.strip().split()[2]))
    claim = wq(env, "claim", "--id", tid)
    assert claim.returncode == 0, claim.stdout + claim.stderr
    return tid


@pytest.fixture
def repo(tmp_path):
    """Primary checkout on main whose gate.py was committed long before any ticket."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "wq5132 test")
    (r / "harness").mkdir()
    (r / "harness" / "gate.py").write_text("GREEN = True\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "seed", date=OLD)
    git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    return r


def _out(p):
    return f"rc={p.returncode}\n{p.stdout}{p.stderr}"


def test_wq4905_shape_dirty_tracked_evidence_refuses(tmp_path, repo):
    """The exact wq#4905 close: cite a tracked file whose edits are uncommitted."""
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    f.write_text("GREEN = False  # default-RED, never committed\n")
    tid = open_ticket(env, "wq5132 fixture: labgrid gate")
    p = wq(env, "done", tid, "--notes", f"{f} — added default-RED. 10/10 tests pass.")
    assert p.returncode == 6, _out(p)
    assert "uncommitted edits" in p.stderr and "wq#5132" in p.stderr, _out(p)


def test_dirty_ticket_named_file_refuses_rc11_even_when_citing_elsewhere(tmp_path, repo):
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    f.write_text("GREEN = False\n")
    other = tmp_path / "report.md"
    other.write_text("a report outside git\n")
    tid = open_ticket(env, "wq5132 fixture: named dirty", f"FIX: edit {f} (~L917-945).")
    p = wq(env, "done", tid, "--notes", f"wrote {other}")
    assert p.returncode == 11, _out(p)
    assert "unlanded-edit gate" in p.stderr and str(f) in p.stderr, _out(p)


def test_named_file_cited_untouched_since_filing_refuses_then_unlanded_overrides(tmp_path, repo):
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    tid = open_ticket(env, "wq5132 fixture: untouched", f"add a class to {f}")
    p = wq(env, "done", tid, "--notes", f"{f} done")
    assert p.returncode == 11, _out(p)
    assert "not touched it since the ticket was filed" in p.stderr, _out(p)
    p = wq(env, "done", tid, "--notes", f"{f} verified, no change", "--unlanded",
           "verify-only ticket: no edit required")
    assert p.returncode == 0, _out(p)


def test_bare_basename_named_ticket_matches_cited_path_wq4912_shape(tmp_path, repo):
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    tid = open_ticket(env, "wq5132 fixture: basename", "Then fix gate.py to flag user scope.")
    p = wq(env, "done", tid, "--notes", f"{f} patched")
    assert p.returncode == 11, _out(p)


def test_named_file_changed_on_main_after_filing_closes(tmp_path, repo):
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    tid = open_ticket(env, "wq5132 fixture: landed", f"edit {f}")
    time.sleep(1.1)
    f.write_text("GREEN = False\n")
    git(repo, "commit", "-qam", "fix(gate): default red")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    p = wq(env, "done", tid, "--notes", f"{f} landed")
    assert p.returncode == 0, _out(p)


def test_commit_naming_the_ticket_satisfies_the_gate(tmp_path, repo):
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    tid = open_ticket(env, "wq5132 fixture: grep", f"edit {f}")
    (repo / "harness" / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"fix(gate): wq#{tid} moved the class", date=OLD)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    p = wq(env, "done", tid, "--notes", f"{f} landed")
    assert p.returncode == 0, _out(p)


def test_wq_id_prefix_is_not_a_reference(tmp_path, repo):
    """wq#12 must not be satisfied by a commit naming wq#123."""
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    tid = open_ticket(env, "wq5132 fixture: prefix", f"edit {f}")
    (repo / "harness" / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"fix: wq#{tid}0 unrelated", date=OLD)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    p = wq(env, "done", tid, "--notes", f"{f} landed")
    assert p.returncode == 11, _out(p)


def test_primary_tree_behind_main_is_not_unlanded(tmp_path, repo):
    env = make_env(tmp_path)
    f = repo / "harness" / "gate.py"
    old_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    f.write_text("GREEN = False\n")
    git(repo, "commit", "-qam", "newer main")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "-q", "--hard", old_head)          # primary tree + local main behind
    tid = open_ticket(env, "wq5132 fixture: behind")
    p = wq(env, "done", tid, "--notes", f"see {f}")
    assert p.returncode == 0, _out(p)


def test_linked_worktree_branch_edit_of_landed_file_refuses(tmp_path, repo):
    env = make_env(tmp_path)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
    (wt / "harness" / "gate.py").write_text("GREEN = False\n")
    git(wt, "commit", "-qam", "branch only")
    tid = open_ticket(env, "wq5132 fixture: branch-only")
    p = wq(env, "done", tid, "--notes", f"see {wt / 'harness' / 'gate.py'}")
    assert p.returncode == 6, _out(p)
    assert "committed only off main" in p.stderr, _out(p)


def test_clean_landed_file_still_closes(tmp_path, repo):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq5132 fixture: control")
    p = wq(env, "done", tid, "--notes", f"see {repo / 'harness' / 'gate.py'}")
    assert p.returncode == 0, _out(p)
