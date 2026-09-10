"""wq#1673 self-test: `wq done` must canonicalize path evidence and refuse a citation that
only exists inside a linked worktree.

TICKET_CLOSURE_AUDIT.md F4: find_artifact resolved a bare token against the closer's cwd and
stored it as typed, so 10 of 419 closes sampled since 2026-09-01 cite a path that resolved
inside a worktree that has since been deleted. These drive the real `wq` binary against a
scratch DB and a throwaway git repo + linked worktree -- same subprocess pattern as
test_wq_merge_state_gate.py -- and assert the three gates the fix adds: canonicalize (notes
get the absolute path), durability (rc 6 unless the file is on main), deliverable (rc 7 when
the output the ticket named at `add` is not there).
"""
import os
import subprocess

import pytest

# default to the wq in the checkout this test file lives in, so a worktree run tests its own
# copy rather than whatever is deployed at /home/lumen/bin.
WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")


def make_env(tmp_path, agent="test-evidence-durability-agent"):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    # wq#912: scratch lane names are never real dispatcher lanes -- point the lane guard at a
    # path that doesn't exist so it fails open instead of refusing against the live config.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    return env


def wq(env, *args, cwd=None):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env,
                          cwd=str(cwd) if cwd else None)


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def open_ticket(env, title, extra=()):
    p = wq(env, "add", "--lane", "auto", "--title", title, *extra)
    assert p.returncode == 0, p.stdout + p.stderr
    tid = str(int(p.stdout.strip().split()[2]))
    claim = wq(env, "claim", "--id", tid)
    assert claim.returncode == 0, claim.stdout + claim.stderr
    return tid


def notes_of(env, tid):
    show = wq(env, "show", tid, "--json")
    assert show.returncode == 0, show.stdout + show.stderr
    import json
    return json.loads(show.stdout)["notes"] or ""


@pytest.fixture
def repo_and_worktree(tmp_path):
    """A repo whose main carries landed.md, plus a linked worktree on another branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "wq1673 test")
    (repo / "landed.md").write_text("evidence that is on main\n")
    git(repo, "add", "landed.md")
    git(repo, "commit", "-qm", "seed")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
    return repo, wt


def test_relative_path_is_rewritten_to_the_path_it_resolved_to(tmp_path):
    env = make_env(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "report.md").write_text("a report outside any git repo\n")
    tid = open_ticket(env, "wq1673 self-test: canonicalize")

    done = wq(env, "done", tid, "--notes", "wrote report.md", cwd=plain)
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(plain / "report.md") in notes_of(env, tid), (
        "the token must be rewritten to the absolute path find_artifact stat()ed")


def test_line_locator_survives_canonicalization(tmp_path):
    env = make_env(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "wq.py").write_text("x = 1\n")
    tid = open_ticket(env, "wq1673 self-test: locator")

    done = wq(env, "done", tid, "--notes", "see wq.py:88-104", cwd=plain)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{plain / 'wq.py'}:88-104" in notes_of(env, tid)


def test_worktree_only_evidence_refuses_rc6(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    _, wt = repo_and_worktree
    (wt / "scratch-report.md").write_text("only ever existed in this worktree\n")
    tid = open_ticket(env, "wq1673 self-test: durability refusal")

    done = wq(env, "done", tid, "--notes", "see scratch-report.md", cwd=wt)
    assert done.returncode == 6, (
        f"expected rc=6 (wq#1673 durability gate), got {done.returncode}: "
        f"{done.stdout}{done.stderr}")
    assert "wq#1673" in done.stdout + done.stderr
    assert notes_of(env, tid) == "", "a refused close must not write notes"


def test_worktree_only_evidence_accepts_with_unlanded_flag(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    _, wt = repo_and_worktree
    (wt / "scratch-report.md").write_text("only ever existed in this worktree\n")
    tid = open_ticket(env, "wq1673 self-test: durability escape hatch")

    done = wq(env, "done", tid, "--notes", "see scratch-report.md",
              "--unlanded", "test: deliberately not on main", cwd=wt)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[UNLANDED: test: deliberately not on main]" in notes_of(env, tid)


def test_evidence_on_main_from_a_linked_worktree_closes(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    _, wt = repo_and_worktree
    tid = open_ticket(env, "wq1673 self-test: landed evidence")

    done = wq(env, "done", tid, "--notes", "see landed.md", cwd=wt)
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(wt / "landed.md") in notes_of(env, tid)


def test_unresolvable_sha_is_not_evidence(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    repo, _ = repo_and_worktree
    tid = open_ticket(env, "wq1673 self-test: fabricated sha")

    done = wq(env, "done", tid, "--notes", "landed as deadbeefcafe1234", cwd=repo)
    assert done.returncode == 2, (
        f"expected rc=2 (wq#759 gate, sha resolves nowhere), got {done.returncode}: "
        f"{done.stdout}{done.stderr}")


def test_real_sha_still_closes(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    repo, _ = repo_and_worktree
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    tid = open_ticket(env, "wq1673 self-test: real sha")

    done = wq(env, "done", tid, "--notes", f"landed as {sha}", cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
    assert sha in notes_of(env, tid)


def test_missing_deliverable_refuses_rc7(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    repo, _ = repo_and_worktree
    tid = open_ticket(env, "wq1673 self-test: deliverable missing",
                      extra=("--deliverable", str(repo / "never-written.md")))

    done = wq(env, "done", tid, "--notes", "see landed.md", cwd=repo)
    assert done.returncode == 7, (
        f"expected rc=7 (wq#1673 deliverable gate), got {done.returncode}: "
        f"{done.stdout}{done.stderr}")
    assert "never-written.md" in done.stdout + done.stderr


def test_missing_deliverable_not_excused_by_no_artifact(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    repo, _ = repo_and_worktree
    tid = open_ticket(env, "wq1673 self-test: deliverable vs no-artifact",
                      extra=("--deliverable", str(repo / "never-written.md")))

    done = wq(env, "done", tid, "--no-artifact", "verify-pass, nothing to cite", cwd=repo)
    assert done.returncode == 7, done.stdout + done.stderr


def test_deliverable_on_main_closes(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    repo, _ = repo_and_worktree
    tid = open_ticket(env, "wq1673 self-test: deliverable landed",
                      extra=("--deliverable", str(repo / "landed.md")))

    done = wq(env, "done", tid, "--notes", "see landed.md", cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr


def test_deliverable_only_in_a_worktree_refuses(tmp_path, repo_and_worktree):
    env = make_env(tmp_path)
    repo, wt = repo_and_worktree
    (wt / "deliverable.md").write_text("written, never landed\n")
    tid = open_ticket(env, "wq1673 self-test: deliverable unlanded",
                      extra=("--deliverable", str(wt / "deliverable.md")))

    done = wq(env, "done", tid, "--notes", "see landed.md", cwd=repo)
    assert done.returncode == 7, done.stdout + done.stderr

    done = wq(env, "done", tid, "--notes", "see landed.md",
              "--unlanded", "test: shipping without landing it", cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[UNLANDED: test: shipping without landing it]" in notes_of(env, tid)
