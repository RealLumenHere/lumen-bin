"""wq#1121: the wq#771/wq#792 target-tree guard must not refuse the PR that carries the work.

The guard refuses a land when the target (primary) tree holds work origin does not, because a
squash branched from origin/main would supersede it silently — the 2026-08-21 dispatcher stall.
For an UN-PUSHED COMMIT that reasoning has a hole: when the branch being landed already contains
that commit, the squash carries it to origin instead of superseding it. The guard counted it as a
collision anyway, so the one action its own refusal prescribes ("land it as its own PR, then
this") was the action it blocked. wq#1108 died there with a branch that was foreman main 035bedb
plus one commit.

These tests drive the REAL guard through `dirtycheck --head` (wq#1121 added the flag so the
head-aware narrowing is checkable without a forge) and the REAL ff through `ffprimary`, against
a throwaway bare origin + primary clone + linked worktree. They assert both directions: the
superset clears, and everything else still refuses.

LUMEN_PR_BIN overrides the script under test; default is the copy next to this tests/ dir, so a
worktree tests its own lumen-pr, never the deployed one.
"""
import os
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


def commit(tree, content, path="f", msg=None):
    (Path(tree) / path).write_text(content)
    git(tree, "add", path)
    git(tree, "commit", "-qm", msg or f"set {path}={content.strip()}")
    return git(tree, "rev-parse", "HEAD").stdout.strip()


def dirtycheck(repo, *args, timeout=60):
    return subprocess.run([LUMEN_PR, "dirtycheck", str(repo), *args],
                          env=GIT_ENV, capture_output=True, text=True, timeout=timeout)


def ffprimary(repo, timeout=60):
    return subprocess.run([LUMEN_PR, "ffprimary", str(repo)],
                          env=GIT_ENV, capture_output=True, text=True, timeout=timeout)


def ahead(primary):
    return int(git(primary, "rev-list", "--count", "origin/main..main").stdout.strip())


@pytest.fixture
def repo(tmp_path):
    """bare origin on main + primary clone with one pushed commit (f=a)."""
    origin = tmp_path / "origin.git"
    primary = tmp_path / "primary"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   env=GIT_ENV, check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)],
                   env=GIT_ENV, check=True, capture_output=True)
    commit(primary, "a\n", msg="init")
    git(primary, "push", "-q", "origin", "main")
    return tmp_path, primary


# --- the guard ---------------------------------------------------------------

def test_branch_containing_the_unpushed_commit_is_not_a_collision(repo, tmp_path):
    """wq#1108's exact shape: primary main + one more commit. Landing supersedes nothing."""
    _, primary = repo
    commit(primary, "b\n")                      # un-pushed on the target tree
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x", "main")
    head = commit(wt, "c\n")                    # branch = primary main + 1
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "f")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wq#1121" in r.stdout
    assert "superset of the target tree" in r.stdout
    assert "clean: none of the 1 path(s)" in r.stdout
    assert "TARGET TREE HAS WORK" not in r.stderr


def test_branch_that_does_not_contain_the_unpushed_commit_still_refuses(repo, tmp_path):
    """The wq#792 hazard itself: branched from origin/main, so the local commit IS superseded."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/y", "origin/main")
    commit(primary, "b\n")                      # un-pushed, and NOT on the branch
    head = commit(wt, "c\n")
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "f")
    assert r.returncode == 1
    assert "TARGET TREE HAS WORK ORIGIN DOES NOT" in r.stderr
    assert "in a local commit on main never pushed" in r.stderr
    assert "wq#1121" not in r.stdout


def test_ancestry_never_clears_an_uncommitted_path(repo, tmp_path):
    """Superset covers COMMITS. Work in no commit is vouched for by no ancestry."""
    _, primary = repo
    commit(primary, "b\n")
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/z", "main")
    head = commit(wt, "c\n")
    (primary / "f").write_text("b-dirty\n")     # uncommitted in the target tree
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "f")
    assert r.returncode == 1
    assert "wq#1121" in r.stdout                # the un-pushed hit was dropped
    assert "uncommitted there" in r.stderr      # ...and the uncommitted one was not
    assert "TARGET TREE HAS WORK ORIGIN DOES NOT" in r.stderr


def test_guard_without_head_is_unchanged(repo, tmp_path):
    """No --head means no ancestry claim to check: the pre-wq#1121 verdict stands."""
    _, primary = repo
    commit(primary, "b\n")
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/w", "main")
    commit(wt, "c\n")
    r = dirtycheck(wt, "--mode", "refuse", "f")
    assert r.returncode == 1
    assert "TARGET TREE HAS WORK ORIGIN DOES NOT" in r.stderr
    assert "wq#1121" not in r.stdout


def test_unresolvable_head_is_an_error_not_a_silent_skip(repo):
    """Fail closed and loudly: a typo'd --head must not read as 'nothing to narrow'."""
    _, primary = repo
    r = dirtycheck(primary, "--head", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "f")
    assert r.returncode == 1
    assert "does not resolve to a commit" in r.stderr


# --- the primary tree after the land -----------------------------------------

def test_ffprimary_repoints_when_local_commits_are_already_on_origin_by_content(repo, tmp_path):
    """The tail of the same loop: the squash landed that work, so 'ahead' is not divergence."""
    _, primary = repo
    commit(primary, "b\n")                      # the un-pushed commit
    assert ahead(primary) == 1
    squash = tmp_path / "squash"                # the forge's squash of that same content
    git(primary, "worktree", "add", "-q", str(squash), "-b", "landed", "origin/main")
    commit(squash, "b\n", msg="squashed")
    git(squash, "push", "-q", "origin", "HEAD:main")
    r = ffprimary(primary)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "re-pointed to origin/main" in r.stdout
    assert "identical tree" in r.stdout
    assert ahead(primary) == 0
    assert (primary / "f").read_text() == "b\n"


def test_ffprimary_keeps_uncommitted_work_when_it_repoints(repo, tmp_path):
    """--keep, not --hard: equal trees mean no tracked file differs, so local edits survive."""
    _, primary = repo
    commit(primary, "b\n")
    squash = tmp_path / "squash"
    git(primary, "worktree", "add", "-q", str(squash), "-b", "landed", "origin/main")
    commit(squash, "b\n", msg="squashed")
    git(squash, "push", "-q", "origin", "HEAD:main")
    (primary / "scratch").write_text("mine\n")  # untracked
    (primary / "f").write_text("b+edit\n")      # tracked, modified
    r = ffprimary(primary)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (primary / "scratch").read_text() == "mine\n"
    assert (primary / "f").read_text() == "b+edit\n"


def test_ffprimary_still_refuses_when_the_local_commit_differs_in_content(repo, tmp_path):
    """Real divergence keeps the wq#792 refusal, whose whole job is to not lose that commit."""
    _, primary = repo
    commit(primary, "b\n")
    other = tmp_path / "other"
    git(primary, "worktree", "add", "-q", str(other), "-b", "elsewhere", "origin/main")
    commit(other, "c\n")
    git(other, "push", "-q", "origin", "HEAD:main")
    r = ffprimary(primary)
    assert r.returncode == 1
    assert "diverged, wq#792" in r.stdout
    assert "re-pointed" not in r.stdout
    assert (primary / "f").read_text() == "b\n"


# --- the flag ----------------------------------------------------------------

def test_land_accepts_allow_dirty_and_says_it_does_nothing(repo):
    """wq#1121: workers hit 'unknown flag' at one door and an unreachable env var at the other.

    The flag is taken and inert. `land` goes on to die on the forge (the fixture's origin is a
    local path), which is fine — what matters is that it got past argument parsing and said why
    the flag buys nothing.
    """
    _, primary = repo
    env = {**GIT_ENV, "LUMEN_PR_SCOPE_CHECK": "0"}
    r = subprocess.run([LUMEN_PR, "land", str(primary), "1", "--allow-dirty"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert "unknown flag for land" not in r.stderr
    assert "--allow-dirty is accepted but does NOTHING" in r.stderr
    assert "no override since wq#1282" in r.stderr


def test_land_still_rejects_a_genuinely_unknown_flag(repo):
    _, primary = repo
    env = {**GIT_ENV, "LUMEN_PR_SCOPE_CHECK": "0"}
    r = subprocess.run([LUMEN_PR, "land", str(primary), "1", "--yolo"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "unknown flag for land: --yolo" in r.stderr
