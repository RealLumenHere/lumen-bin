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


# --- wq#2063: a land must not report success while a path the move carried is off disk ---

def _land_adding(wt, paths):
    """Simulate a forge merge that ADDS new files, and push it straight to origin main."""
    for p, content in paths.items():
        f = wt / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        git(wt, "add", p)
    git(wt, "commit", "-qm", "add " + " ".join(paths))
    git(wt, "push", "-q", "origin", "HEAD:main")


def test_refusal_does_not_leave_byte_identical_paths_staged(repo):
    """wq#2063: stage_identical_overlap() `git add`s before the ff has decided it can move, so a
    refusal used to return with the index mutated — a clean untracked file came back as a staged
    `A` entry the operator never created. Follow lumen-pr's own remedy and delete it and the entry
    becomes `AD`, which overlaps the ff forever and re-refuses about a path they cannot see.
    A refusal must leave the tree exactly as it found it."""
    primary, wt = repo
    # incoming adds two files AND modifies an existing one, so part of the overlap is
    # byte-identical (staged by wq#1490) and part is not (the refusal)
    _land_adding(wt, {"retrieval/a.py": "new-a\n", "retrieval/b.py": "new-b\n", "f": "upstream\n"})
    (primary / "retrieval").mkdir(parents=True, exist_ok=True)
    (primary / "retrieval/a.py").write_text("new-a\n")   # untracked, byte-identical
    (primary / "retrieval/b.py").write_text("new-b\n")   # untracked, byte-identical
    (primary / "f").write_text("my own local edit\n")    # differs -> forces the refusal
    before = git(primary, "status", "--short").stdout

    r = ffprimary(primary)
    assert r.returncode != 0, "expected a refusal on the conflicting path"

    after = git(primary, "status", "--short").stdout
    assert after == before, f"refusal mutated the index\nbefore:\n{before}\nafter:\n{after}"
    assert " A  retrieval/a.py" not in " " + after
    assert "A  retrieval/b.py" not in after


def test_ff_that_leaves_a_carried_path_off_disk_is_not_reported_as_success(repo):
    """wq#2063: `git merge --ff-only` returning 0 is not proof the files are on disk — git updates
    HEAD and the index and skips materialising a path whose index entry already matches the target
    blob. A land reported 'fast-forwarded 1 commit(s)' while 2 of 7 new files were absent, showing
    ` D`. Drive the verifier at that exact state and require it to notice and restore."""
    primary, wt = repo
    _land_adding(wt, {"retrieval/a.py": "new-a\n", "retrieval/b.py": "new-b\n", "c.py": "new-c\n"})
    old = git(primary, "rev-parse", "HEAD").stdout.strip()
    r = ffprimary(primary)
    assert r.returncode == 0, r.stdout + r.stderr

    # reproduce the symptom: HEAD and index carry the files, the working tree does not
    for p in ("retrieval/a.py", "retrieval/b.py"):
        (primary / p).unlink()
    assert " D retrieval/a.py" in git(primary, "status", "--short").stdout

    out = subprocess.run(
        ["bash", "-c",
         'note() { printf "lumen-pr: %s\\n" "$*"; }\n'
         + re.search(r"^verify_ff_restored\(\).*?^\}", Path(LUMEN_PR).read_text(),
                     re.S | re.M).group(0)
         + '\nverify_ff_restored "$1" "$2"',
         "_", str(primary), old],
        env=GIT_ENV, capture_output=True, text=True)

    assert out.returncode == 0, out.stdout + out.stderr
    assert "NOT on disk" in out.stdout, out.stdout
    for p, want in (("retrieval/a.py", "new-a\n"), ("retrieval/b.py", "new-b\n")):
        assert (primary / p).exists(), f"{p} was not restored"
        assert (primary / p).read_text() == want


def test_verifier_leaves_an_operators_own_unrelated_deletion_alone(repo):
    """The verifier is scoped to paths the move touched. A file the operator deleted themselves,
    that the incoming commits never touched, must survive — one that DID overlap would have been
    refused upstream, so scoping to the moved set cannot mask a real conflict."""
    primary, wt = repo
    (primary / "unrelated.py").write_text("old\n")
    git(primary, "add", "unrelated.py")
    git(primary, "commit", "-qm", "add unrelated")
    git(primary, "push", "-q", "origin", "main")
    git(wt, "fetch", "-q", "origin")
    git(wt, "merge", "-q", "origin/main")
    _land_adding(wt, {"retrieval/a.py": "new-a\n"})
    old = git(primary, "rev-parse", "HEAD").stdout.strip()
    assert ffprimary(primary).returncode == 0
    (primary / "unrelated.py").unlink()          # the operator's own deletion

    out = subprocess.run(
        ["bash", "-c",
         'note() { printf "lumen-pr: %s\\n" "$*"; }\n'
         + re.search(r"^verify_ff_restored\(\).*?^\}", Path(LUMEN_PR).read_text(),
                     re.S | re.M).group(0)
         + '\nverify_ff_restored "$1" "$2"',
         "_", str(primary), old],
        env=GIT_ENV, capture_output=True, text=True)

    assert out.returncode == 0, out.stdout + out.stderr
    assert not (primary / "unrelated.py").exists(), \
        "verifier reverted a deletion the move never touched"


def test_both_ff_success_paths_verify_before_reporting(repo):
    """Structural guard: every path in ff_primary_after_land that reports a successful move must
    call verify_ff_restored first. A future edit that adds a success path without it would
    reintroduce the silent success-that-wasn't."""
    src = Path(LUMEN_PR).read_text()
    m = re.search(r"^ff_primary_after_land\(\)\s*\{(.*?)^\}", src, re.S | re.M)
    assert m, "ff_primary_after_land not found"
    body = m.group(1)
    # Only the paths that move a WORKING TREE. The "local main REF (checked out nowhere)"
    # branch moves a ref for a branch no tree has checked out, and says so — there is no
    # working tree there for a file to go missing from, so it is deliberately exempt.
    for marker in ('primary $tree fast-forwarded $behind commit(s) to',
                   'primary $tree re-pointed to origin/$DEFAULT_BRANCH ('):
        idx = body.find(marker)
        assert idx > 0, f"success message not found: {marker}"
        window = body[max(0, idx - 400):idx]
        assert "verify_ff_restored" in window, \
            f"success path {marker!r} reports success without verify_ff_restored (wq#2063)"
