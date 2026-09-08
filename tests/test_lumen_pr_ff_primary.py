"""wq#1536: `lumen-pr land` must leave the primary tree at origin/main, or say exactly why not.

On 2026-09-08 two --worktree lands (fleet-inventory PR #6, foreman PR #362) merged on Gitea
and left their primary trees one commit behind origin/main: cmd_land's linked-worktree branch
returned before the fast-forward step ever ran. The --primary path did run an ff, but behind
2>/dev/null with a guessed "uncommitted work?" message.

These tests drive the REAL function (ff_primary_after_land, via the `ffprimary` subcommand)
against a throwaway bare origin + primary clone + linked worktree, and assert both halves of
the contract: the primary moves when it can, and every refusal names its reason and the fix.

LUMEN_PR_BIN overrides the script under test; default is the copy next to this tests/ dir,
so a worktree tests its own lumen-pr, never the deployed one.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

LUMEN_PR = os.environ.get("LUMEN_PR_BIN", str(Path(__file__).resolve().parents[1] / "lumen-pr"))
# Drop every inherited GIT_* variable: under a git hook (the pre-commit gate runs this file)
# git exports GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE for the repo being committed, and any
# `git -C <tmp>` here would silently act on THAT repo instead of the fixture.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(tree, *args, check=True):
    return subprocess.run(["git", "-C", str(tree), *args], env=GIT_ENV,
                          capture_output=True, text=True, check=check)


def ffprimary(tree):
    return subprocess.run([LUMEN_PR, "ffprimary", str(tree)], env=GIT_ENV,
                          capture_output=True, text=True)


def behind(primary):
    return int(git(primary, "rev-list", "--count", "main..origin/main").stdout.strip())


@pytest.fixture
def repo(tmp_path):
    """bare origin on main, primary clone with one pushed commit, linked worktree on fix/x."""
    origin = tmp_path / "origin.git"
    primary = tmp_path / "primary"
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=GIT_ENV, check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)], env=GIT_ENV,
                   check=True, capture_output=True)
    (primary / "f").write_text("a\n")
    git(primary, "add", "f")
    git(primary, "commit", "-qm", "init")
    git(primary, "push", "-q", "origin", "main")
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x")
    return primary, wt


def land_on_origin(wt, content, path="f"):
    """Simulate a forge merge: commit in the worktree and push it straight to origin main."""
    (wt / path).write_text(content)
    git(wt, "add", path)
    git(wt, "commit", "-qm", f"change {content.strip()}")
    git(wt, "push", "-q", "origin", "HEAD:main")


def test_worktree_land_fast_forwards_primary(repo):
    primary, wt = repo
    land_on_origin(wt, "b\n")
    git(primary, "fetch", "-q")
    assert behind(primary) == 1
    r = ffprimary(wt)  # invoked from the LINKED worktree, the wq#1536 shape
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fast-forwarded 1 commit(s)" in r.stdout
    assert behind(primary) == 0
    assert (primary / "f").read_text() == "b\n"


def test_already_current_is_a_noop_that_says_so(repo):
    primary, wt = repo
    r = ffprimary(wt)
    assert r.returncode == 0
    assert "already at origin/main" in r.stdout


def test_wrong_branch_is_refused_with_reason_and_never_moves_head(repo):
    primary, wt = repo
    land_on_origin(wt, "b\n")
    git(primary, "checkout", "-q", "-b", "parked")
    r = ffprimary(wt)
    assert r.returncode == 1
    assert "NOT fast-forwarded" in r.stdout
    assert "on 'parked', not main" in r.stdout
    assert f"git -C {primary} checkout main" in r.stdout
    assert git(primary, "branch", "--show-current").stdout.strip() == "parked"
    # ISSUE-LOG #237: main is checked out nowhere, so its REF is forwarded (no file moves),
    # which is what the repo sweep compares against origin. Working tree stays parked.
    assert "local main REF (checked out nowhere) fast-forwarded 1 commit(s)" in r.stdout
    assert behind(primary) == 0
    assert (primary / "f").read_text() == "a\n"
    git(primary, "checkout", "-q", "main")
    assert (primary / "f").read_text() == "b\n"


def test_wrong_branch_leaves_the_ref_alone_when_main_is_checked_out_elsewhere(repo, tmp_path):
    """A second worktree holding main means moving the ref WOULD move files there — refuse."""
    primary, wt = repo
    git(primary, "checkout", "-q", "-b", "parked")
    main_wt = tmp_path / "main_wt"
    git(primary, "worktree", "add", "-q", str(main_wt), "main")
    land_on_origin(wt, "b\n")
    r = ffprimary(wt)
    assert r.returncode == 1
    assert "on 'parked', not main" in r.stdout
    assert "REF (checked out nowhere)" not in r.stdout
    git(primary, "fetch", "-q")
    assert behind(primary) == 1
    assert (main_wt / "f").read_text() == "a\n"


def test_git_lock_is_named(repo):
    primary, wt = repo
    land_on_origin(wt, "b\n")
    lock = primary / ".git" / "index.lock"
    lock.touch()
    r = ffprimary(wt)
    assert r.returncode == 1
    assert "git lock present" in r.stdout
    assert str(lock) in r.stdout
    lock.unlink()
    assert behind(primary) == 1


def test_in_progress_operation_is_named(repo):
    primary, wt = repo
    land_on_origin(wt, "b\n")
    (primary / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    r = ffprimary(wt)
    assert r.returncode == 1
    assert "operation is in progress" in r.stdout
    assert "MERGE_HEAD" in r.stdout
    (primary / ".git" / "MERGE_HEAD").unlink()


def test_dirty_overlap_is_refused_and_lists_the_paths(repo):
    primary, wt = repo
    land_on_origin(wt, "b\n")
    (primary / "f").write_text("local edit\n")
    r = ffprimary(wt)
    assert r.returncode == 1
    assert "1 uncommitted path(s) there overlap" in r.stdout
    assert re.search(r"^\s+f$", r.stdout, re.M)
    assert (primary / "f").read_text() == "local edit\n"  # nothing clobbered
    assert behind(primary) == 1


def test_unrelated_dirt_does_not_block_the_ff(repo):
    """The wq#771 guard is about overlap. Dirt elsewhere in the tree must not stop the primary
    catching up — that over-refusal is how trees drift behind for days."""
    primary, wt = repo
    land_on_origin(wt, "b\n")
    (primary / "unrelated").write_text("scratch\n")
    r = ffprimary(wt)
    assert r.returncode == 0, r.stdout + r.stderr
    assert behind(primary) == 0
    assert (primary / "unrelated").read_text() == "scratch\n"


def test_diverged_local_main_is_refused_with_the_commits_listed(repo):
    primary, wt = repo
    (primary / "g").write_text("local\n")
    git(primary, "add", "g")
    git(primary, "commit", "-qm", "local-only commit")
    land_on_origin(wt, "b\n")
    r = ffprimary(wt)
    assert r.returncode == 1
    assert "diverged" in r.stdout
    assert "local-only commit" in r.stdout
    assert "push origin main" in r.stdout


def test_primary_path_uses_the_same_function(repo):
    primary, wt = repo
    land_on_origin(wt, "b\n")
    r = ffprimary(primary)
    assert r.returncode == 0, r.stdout + r.stderr
    assert behind(primary) == 0


def test_cmd_land_linked_worktree_branch_calls_the_ff_before_returning():
    """Structural guard for the exact defect: the linked-worktree branch of cmd_land must call
    ff_primary_after_land before its `return 0`. A future edit that reorders or drops it would
    reintroduce wq#1536 without any forge-backed test noticing."""
    src = Path(LUMEN_PR).read_text()
    m = re.search(r"cmd_land\(\)\s*\{(.*?)\n\}\n", src, re.S)
    assert m, "cmd_land not found"
    body = m.group(1)
    linked = re.search(
        r'note "linked worktree — leaving it on(.*?)return 0', body, re.S)
    assert linked, "linked-worktree branch not found in cmd_land"
    assert "ff_primary_after_land" in linked.group(1), \
        "cmd_land's linked-worktree branch returns without fast-forwarding the primary (wq#1536)"
    # and the primary path must not have regressed to the old silent 2>/dev/null ff
    assert re.search(r"merge --ff-only.*2>/dev/null", body) is None, \
        "cmd_land runs a silent ff-only merge again — reasons must be printed (wq#1536)"
