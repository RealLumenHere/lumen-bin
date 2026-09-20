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


def ffprimary(repo, *args, timeout=60):
    return subprocess.run([LUMEN_PR, "ffprimary", str(repo), *args],
                          env=GIT_ENV, capture_output=True, text=True, timeout=timeout)


def squash_onto_origin(primary, head, msg="squashed"):
    """Land <head>'s TREE as one new commit on origin/main, the way a forge squash-merge does.

    commit-tree, not a local merge: the point is that origin gets a commit that carries the
    branch's content and none of its shas, so nothing on the primary tree is an ancestor of it.
    """
    base = git(primary, "rev-parse", "origin/main").stdout.strip()
    sha = git(primary, "commit-tree", f"{head}^{{tree}}", "-p", base, "-m", msg).stdout.strip()
    git(primary, "push", "-q", "origin", f"{sha}:main")
    return sha


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


# --- wq#2099: the superset land, whose trees are NOT identical ----------------

def test_ffprimary_repoints_after_a_superset_land_that_re_edited_the_path(repo, tmp_path):
    """wq#1108's shape end to end: primary main = origin + T, branch = main + C, squash = T+C.

    wq#1121 cleared the guard for this branch but left the primary diverged forever: its proof
    was "identical tree", and C re-edited the very file T touched, so the trees differ. The tree
    then stayed 1 ahead and the wq#771 guard refused the NEXT PR on that path as un-pushed —
    the same refusal wq#1121 existed to end, one land later.
    """
    _, primary = repo
    commit(primary, "b\n")                      # T: un-pushed on the target tree
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x", "main")
    head = commit(wt, "c\n")                    # C: the branch re-edits the same path
    squash_onto_origin(primary, head)           # the forge's squash of T+C
    assert ahead(primary) == 1

    r = ffprimary(primary, "--head", head)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "re-pointed to origin/main" in r.stdout
    assert "wq#2099" in r.stdout
    assert ahead(primary) == 0
    assert (primary / "f").read_text() == "c\n"

    # ...and the next PR from origin/main touching f is no longer a collision.
    nxt = tmp_path / "next"
    git(primary, "worktree", "add", "-q", str(nxt), "-b", "fix/next", "origin/main")
    nxt_head = commit(nxt, "d\n")
    r = dirtycheck(nxt, "--mode", "refuse", "--head", nxt_head, "f")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TARGET TREE HAS WORK" not in r.stderr


def test_superset_repoint_refuses_when_uncommitted_work_overlaps(repo, tmp_path):
    """The re-point rewrites files here, unlike the identical-tree one. Overlap outranks it."""
    _, primary = repo
    commit(primary, "b\n")
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x", "main")
    head = commit(wt, "c\n")
    squash_onto_origin(primary, head)
    (primary / "f").write_text("b+mine\n")      # uncommitted, on the path being rewritten
    r = ffprimary(primary, "--head", head)
    assert r.returncode == 1
    assert "NOT re-pointed" in r.stdout
    assert "uncommitted path(s) there overlap" in r.stdout
    assert (primary / "f").read_text() == "b+mine\n"
    assert ahead(primary) == 1


def test_superset_repoint_keeps_uncommitted_work_it_does_not_touch(repo, tmp_path):
    """--keep semantics survive the wider proof: only the overlapping case is refused."""
    _, primary = repo
    commit(primary, "b\n")
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x", "main")
    head = commit(wt, "c\n")
    squash_onto_origin(primary, head)
    (primary / "scratch").write_text("mine\n")  # untracked, unrelated path
    r = ffprimary(primary, "--head", head)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (primary / "scratch").read_text() == "mine\n"
    assert ahead(primary) == 0


def test_a_head_that_does_not_contain_the_local_commit_still_refuses(repo, tmp_path):
    """Ancestry is the whole claim. A branch off origin/main never carried T, so T is at risk."""
    _, primary = repo
    commit(primary, "b\n")                      # T, on nobody's branch
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/y", "origin/main")
    head = commit(wt, "c\n")
    squash_onto_origin(primary, head)
    r = ffprimary(primary, "--head", head)
    assert r.returncode == 1
    assert "diverged, wq#792" in r.stdout
    assert "re-pointed" not in r.stdout
    assert (primary / "f").read_text() == "b\n"


def test_a_head_that_never_reached_origin_still_refuses(repo, tmp_path):
    """Containment is not arrival: with no squash pushed, origin has none of T's content."""
    _, primary = repo
    commit(primary, "b\n")
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x", "main")
    head = commit(wt, "c\n")                    # landed nowhere
    r = ffprimary(primary, "--head", head)
    assert r.returncode == 1
    assert "diverged, wq#792" in r.stdout
    assert ahead(primary) == 1


def test_ffprimary_repoints_when_every_local_commit_is_upstream_equivalent(repo, tmp_path):
    """No head to go on: a cherry-picked/rebased land is provable by patch-id alone."""
    _, primary = repo
    local = commit(primary, "b\n")              # T
    other = tmp_path / "other"
    git(primary, "worktree", "add", "-q", str(other), "-b", "elsewhere", "origin/main")
    git(other, "cherry-pick", "-x", local)      # T' — same patch, new sha (-x keeps them apart)
    commit(other, "x\n", path="g", msg="unrelated")   # ...and origin moves on, so trees differ
    git(other, "push", "-q", "origin", "HEAD:main")
    r = ffprimary(primary)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "upstream-equivalent" in r.stdout
    assert ahead(primary) == 0
    assert (primary / "f").read_text() == "b\n"
    assert (primary / "g").read_text() == "x\n"


def test_ffprimary_rejects_an_unresolvable_head(repo):
    """Same door as dirtycheck --head: a typo must not read as 'no head given'."""
    _, primary = repo
    r = ffprimary(primary, "--head", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert r.returncode == 1
    assert "does not resolve to a commit" in r.stderr


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


# --- wq#1490: uncommitted work that is byte-identical to what lands --------------

def test_uncommitted_path_identical_to_head_is_not_a_collision(repo, tmp_path):
    """lumen-bin PR#75's shape: a script patched in place in the primary, and the PR carries
    exactly those bytes. Landing changes nothing on disk, so there is nothing to supersede."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/x", "origin/main")
    head = commit(wt, "c\n")
    (primary / "f").write_text("c\n")            # uncommitted, same bytes as the PR head
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "f")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wq#1490" in r.stdout
    assert "clean: none of the 1 path(s)" in r.stdout
    assert "TARGET TREE HAS WORK" not in r.stderr


def test_uncommitted_path_that_differs_from_head_still_refuses(repo, tmp_path):
    """The wq#771 hazard itself: live bytes the PR does not carry would be superseded."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/y", "origin/main")
    head = commit(wt, "c\n")
    (primary / "f").write_text("c-but-different\n")
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "f")
    assert r.returncode == 1
    assert "uncommitted there" in r.stderr
    assert "TARGET TREE HAS WORK ORIGIN DOES NOT" in r.stderr
    assert "wq#1490" not in r.stdout


def test_uncommitted_identical_path_without_head_is_still_a_hit(repo, tmp_path):
    """No head, no blob to compare against: the pre-wq#1490 verdict stands (fails closed)."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/z", "origin/main")
    commit(wt, "c\n")
    (primary / "f").write_text("c\n")
    r = dirtycheck(wt, "--mode", "refuse", "f")
    assert r.returncode == 1
    assert "TARGET TREE HAS WORK ORIGIN DOES NOT" in r.stderr


def test_untracked_file_identical_to_head_is_not_a_collision(repo, tmp_path):
    """An untracked file the PR adds with the same bytes is the same non-collision."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/u", "origin/main")
    head = commit(wt, "new\n", path="g")
    (primary / "g").write_text("new\n")          # untracked in the primary
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "g")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wq#1490" in r.stdout


def test_symlink_never_reads_as_identical(repo, tmp_path):
    """A symlink's blob is its target text; a regular file with the same text is not it."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/s", "origin/main")
    head = commit(wt, "f", path="g")             # PR adds a regular file g containing "f"
    (primary / "g").symlink_to("f")              # primary has g as a symlink to f
    r = dirtycheck(wt, "--mode", "refuse", "--head", head, "g")
    assert r.returncode == 1
    assert "TARGET TREE HAS WORK ORIGIN DOES NOT" in r.stderr


def test_ffprimary_fast_forwards_over_an_identical_uncommitted_overlap(repo, tmp_path):
    """After the guard lets that PR land, the ff must not refuse for the same non-reason:
    the live file is already the incoming blob, so stage it and fast-forward — no byte moves."""
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/ff", "origin/main")
    commit(wt, "c\n")
    git(wt, "push", "-q", "origin", "HEAD:main")
    (primary / "f").write_text("c\n")            # patched in place, identical to what landed
    (primary / "other").write_text("mine\n")     # unrelated untracked work must survive
    r = ffprimary(primary)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wq#1490" in r.stdout
    assert "fast-forwarded 1 commit(s)" in r.stdout
    assert (primary / "f").read_text() == "c\n"
    assert (primary / "other").read_text() == "mine\n"
    assert git(primary, "status", "--porcelain", "--", "f").stdout == ""
    assert git(primary, "rev-parse", "main").stdout == git(primary, "rev-parse", "origin/main").stdout


def test_ffprimary_still_refuses_a_divergent_uncommitted_overlap(repo, tmp_path):
    _, primary = repo
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/ff2", "origin/main")
    commit(wt, "c\n")
    git(wt, "push", "-q", "origin", "HEAD:main")
    (primary / "f").write_text("c-but-different\n")
    r = ffprimary(primary)
    assert r.returncode == 1
    assert "uncommitted path(s) there overlap" in r.stdout
    assert "wq#1490" not in r.stdout
    assert (primary / "f").read_text() == "c-but-different\n"


def test_superset_repoint_stages_an_identical_uncommitted_overlap(repo, tmp_path):
    """Same narrowing on the wq#2099 re-point path: T un-pushed, branch T+C squashed onto
    origin, and the primary's live f already holds C's bytes."""
    _, primary = repo
    commit(primary, "b\n")                      # T
    wt = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", str(wt), "-b", "fix/sup", "main")
    head = commit(wt, "c\n")                    # C
    squash_onto_origin(primary, head)
    (primary / "f").write_text("c\n")           # live bytes already C's
    r = ffprimary(primary, "--head", head)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wq#1490" in r.stdout
    assert "re-pointed to origin/main" in r.stdout
    assert ahead(primary) == 0
    assert (primary / "f").read_text() == "c\n"
    assert git(primary, "status", "--porcelain", "--", "f").stdout == ""
