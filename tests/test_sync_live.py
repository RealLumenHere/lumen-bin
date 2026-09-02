"""sync-live — kb/ISSUE-SOLUTIONS-fable.md #19 (wq#1256).

Destructive resync of a live tree becomes a verb with a precondition: refuse unless
the expected SHA has actually landed on origin/main, and refuse a dirty tree. Raw
`git checkout`/`git reset --hard`/`git clean -f` in a primary tree is PreToolUse-blocked
for workers (harness/guard.sh wq#900/wq#1256) so this is the only path left standing.
"""
import os
import subprocess

SYNC_LIVE = os.environ.get("SYNC_LIVE_BIN", "/home/lumen/bin/sync-live")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                           capture_output=True, text=True)


def make_origin_and_live(tmp_path):
    """A bare 'origin' plus a live clone tracking it, one commit deep on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "config", "user.email", "test@test")
    git(seed, "config", "user.name", "test")
    (seed / "f.txt").write_text("v1\n")
    git(seed, "add", "f.txt")
    git(seed, "commit", "-qm", "init")
    git(seed, "branch", "-M", "main")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")

    live = tmp_path / "live"
    subprocess.run(["git", "clone", "-q", str(origin), str(live)], check=True,
                   capture_output=True, text=True)
    git(live, "checkout", "-q", "main")
    return origin, live


def head_sha(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def run_sync_live(repo, sha):
    return subprocess.run([SYNC_LIVE, str(repo), sha], capture_output=True, text=True)


def test_sha_not_on_origin_refused_tree_untouched(tmp_path):
    origin, live = make_origin_and_live(tmp_path)
    before_head = head_sha(live)
    before_status = git(live, "status", "--porcelain").stdout

    # A commit that only exists locally in a *different* clone never reached origin/main.
    other = tmp_path / "other_clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True,
                   capture_output=True, text=True)
    git(other, "checkout", "-q", "main")
    git(other, "config", "user.email", "test@test")
    git(other, "config", "user.name", "test")
    (other / "f.txt").write_text("v2 unpublished\n")
    git(other, "commit", "-qam", "unpublished change")
    unpublished_sha = head_sha(other)

    p = run_sync_live(live, unpublished_sha)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "REFUSE" in p.stdout
    assert "not in origin/main" in p.stdout

    assert head_sha(live) == before_head
    assert git(live, "status", "--porcelain").stdout == before_status
    assert (live / "f.txt").read_text() == "v1\n"


def test_dirty_live_tree_refused(tmp_path):
    origin, live = make_origin_and_live(tmp_path)
    expected = head_sha(live)
    (live / "f.txt").write_text("uncommitted local edit\n")

    p = run_sync_live(live, expected)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "REFUSE" in p.stdout
    assert "dirty" in p.stdout
    assert (live / "f.txt").read_text() == "uncommitted local edit\n"


def test_clean_tree_expected_sha_on_origin_resyncs(tmp_path):
    origin, live = make_origin_and_live(tmp_path)

    # Land a second commit via a second clone, pushed to origin/main.
    other = tmp_path / "landing_clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True,
                   capture_output=True, text=True)
    git(other, "checkout", "-q", "main")
    git(other, "config", "user.email", "test@test")
    git(other, "config", "user.name", "test")
    (other / "f.txt").write_text("v2 landed\n")
    git(other, "commit", "-qam", "landed change")
    git(other, "push", "-q", "origin", "main")
    landed_sha = head_sha(other)

    # live is still on the old commit and has no local edits.
    assert head_sha(live) != landed_sha

    p = run_sync_live(live, landed_sha)
    assert p.returncode == 0, p.stdout + p.stderr
    assert head_sha(live) == landed_sha
    assert (live / "f.txt").read_text() == "v2 landed\n"


def test_usage_error_on_missing_args(tmp_path):
    p = subprocess.run([SYNC_LIVE], capture_output=True, text=True)
    assert p.returncode == 1
    assert "usage" in p.stderr.lower()
