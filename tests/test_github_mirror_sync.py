"""wq#3166: github-mirror-sync pushes branches and tags, never `--mirror`.

`git clone --mirror` fetches `+refs/*:refs/*`, so the bare clone holds Gitea's refs/pull/*
PR refs too. GitHub hides refs/pull/* and rejects any push touching them ("deny updating a
hidden ref"), which failed every repo's push from 2026-09-28 on. The fake GitHub below is a
bare repo with `receive.hideRefs=refs/pull`, which makes receive-pack refuse exactly like
GitHub does.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "github-mirror-sync"


def git(*args, cwd=None, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, check=check,
    )


def rev(repo, ref):
    r = git("-C", str(repo), "rev-parse", "--verify", "--quiet", ref, check=False)
    return r.stdout.strip() or None


def build(tmp_path, github_url=None):
    """origin (Gitea) with main, a tag and a PR ref; a hide-refs github; a primary tree."""
    work = tmp_path / "work"
    git("init", "-q", str(work))
    (work / "f").write_text("1\n")
    git("-C", str(work), "add", "f")
    git("-C", str(work), "commit", "-qm", "one")
    git("-C", str(work), "tag", "v1")
    (work / "f").write_text("2\n")
    git("-C", str(work), "commit", "-qam", "two")

    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(origin))
    git("-C", str(work), "push", "-q", str(origin), "main", "v1", "HEAD~1:refs/pull/1/head")

    github = tmp_path / "github.git"
    git("init", "-q", "--bare", str(github))
    git("-C", str(github), "config", "receive.hideRefs", "refs/pull")
    git("-C", str(work), "push", "-q", str(github), "HEAD~1:refs/heads/stale")

    primary = tmp_path / "primary"
    git("init", "-q", str(primary))
    git("-C", str(primary), "remote", "add", "origin", str(origin))
    git("-C", str(primary), "remote", "add", "github", github_url or str(github))
    return origin, github, primary


def run_sync(tmp_path, primary):
    aq_log = tmp_path / "aq.log"
    aq = tmp_path / "aq"
    aq.write_text('#!/bin/sh\nprintf "%s|" "$@" >> "$AQ_LOG"; echo >> "$AQ_LOG"\n')
    aq.chmod(0o755)
    env = dict(
        os.environ,
        GITHUB_MIRROR_REPOS=f"demo={primary}",
        GITHUB_MIRROR_ROOT=str(tmp_path / "mirrors"),
        GITHUB_MIRROR_AQ=str(aq),
        AQ_LOG=str(aq_log),
    )
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return r, (aq_log.read_text() if aq_log.exists() else "")


def test_fixture_rejects_a_mirror_push_like_github(tmp_path):
    """The fake GitHub must reproduce the bug, or the fix test below proves nothing."""
    origin, github, _ = build(tmp_path)
    bare = tmp_path / "m.git"
    git("clone", "-q", "--mirror", str(origin), str(bare))
    assert rev(bare, "refs/pull/1/head"), "clone --mirror should carry the PR ref"
    r = git("-C", str(bare), "push", "--mirror", str(github), check=False)
    assert r.returncode != 0
    assert "deny updating a hidden ref" in r.stderr


def test_sync_mirrors_heads_and_tags_past_pr_refs(tmp_path):
    origin, github, primary = build(tmp_path)
    for _ in range(2):  # first run clones, second run takes the fetch path
        r, aq = run_sync(tmp_path, primary)
        assert r.returncode == 0, r.stderr
        assert "demo: mirrored to GitHub" in r.stdout
        assert aq == ""
    assert rev(github, "refs/heads/main") == rev(origin, "refs/heads/main")
    assert rev(github, "refs/tags/v1") == rev(origin, "refs/tags/v1")
    assert rev(github, "refs/heads/stale") is None, "--prune should drop branches gone from origin"
    assert rev(github, "refs/pull/1/head") is None


def test_push_failure_reports_stderr_tail_and_owner(tmp_path):
    _, _, primary = build(tmp_path, github_url=str(tmp_path / "no-such-github.git"))
    r, aq = run_sync(tmp_path, primary)
    assert r.returncode == 1
    assert "demo: FAIL push heads+tags to github:" in r.stderr
    assert "demo-push-fail|" in aq
    assert "--owner|github-mirror-sync|" in aq
    # the git error text itself, not just "push failed"
    assert "does not appear to be a git repository" in aq
