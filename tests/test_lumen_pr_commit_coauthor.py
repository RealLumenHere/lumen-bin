"""wq#4798: lumen-pr commit must refuse a Co-authored-by trailer (wq4796 follow-up).

wq#4796 banned Co-authored-by at the pr-merge verb (token-engine) and in the fworker prompt
template, but could not touch bin/lumen-pr because that file lives in the write-frozen
lumen-bin PRIMARY_TREES wall (wq#900/wq#1719) and wq4796's dispatch only granted a foreman
worktree. This exercises the gap directly: `lumen-pr commit` must refuse any commit subject
or --body-file text carrying a `co-authored-by:` line, case-insensitively, the same way it
already refuses lint_secrets/lint_subject violations before the message is assembled.

LUMEN_PR_BIN overrides the script under test; default is the copy next to this tests/ dir, so
a worktree tests its own lumen-pr, never the deployed one.
"""
import os
import subprocess
from pathlib import Path

LUMEN_PR = os.environ.get("LUMEN_PR_BIN", str(Path(__file__).resolve().parents[1] / "lumen-pr"))
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "LUMEN_PR_SCOPE_CHECK": "0",
}


def git(tree, *args, check=True):
    return subprocess.run(["git", "-C", str(tree), *args], env=GIT_ENV,
                           capture_output=True, text=True, check=check)


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    o = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(o)], env=GIT_ENV, check=True)
    git(r, "remote", "add", "origin", str(o))
    (r / "f.txt").write_text("v1\n")
    git(r, "add", "f.txt")
    git(r, "commit", "-q", "-m", "chore: seed")
    git(r, "checkout", "-q", "-b", "feat/x")
    (r / "f.txt").write_text("v2\n")
    git(r, "add", "f.txt")
    return r


def _commit(tmp_path, subject, body=None):
    r = _repo(tmp_path)
    args = [LUMEN_PR, "commit", str(r), subject]
    if body is not None:
        bf = tmp_path / "body.md"
        bf.write_text(body)
        args += ["--body-file", str(bf)]
    p = subprocess.run(args, capture_output=True, text=True, env=GIT_ENV)
    return p.returncode, p.stdout + p.stderr


def test_commit_refuses_coauthor_trailer_in_subject():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rc, out = _commit(Path(td), "feat(x): add y\n\nCo-authored-by: Someone <s@example.com>")
        assert rc != 0 and "Co-authored-by" in out


def test_commit_refuses_coauthor_trailer_in_body_file():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rc, out = _commit(Path(td), "feat(x): add y", body="Co-authored-by: Someone <s@example.com>\n")
        assert rc != 0 and "Co-authored-by" in out


def test_commit_refuses_coauthor_case_insensitively():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rc, out = _commit(Path(td), "feat(x): add y", body="CO-AUTHORED-BY: Someone <s@example.com>\n")
        assert rc != 0 and "Co-authored-by" in out


def test_commit_clean_message_is_not_blocked_by_the_lint():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rc, out = _commit(Path(td), "feat(x): add y", body="## What\nx\n")
        assert "Co-authored-by" not in out
        assert rc == 0


def test_commit_lints_coauthor_before_the_commit_is_made():
    """The lint must run before `git commit`, not after: a refused commit must not land."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        r = _repo(Path(td))
        pre = git(r, "rev-parse", "HEAD").stdout.strip()
        bf = Path(td) / "bad.md"
        bf.write_text("Co-authored-by: Someone <s@example.com>\n")
        p = subprocess.run([LUMEN_PR, "commit", str(r), "feat(x): add y",
                             "--body-file", str(bf)], capture_output=True, text=True, env=GIT_ENV)
        post = git(r, "rev-parse", "HEAD").stdout.strip()
        assert p.returncode != 0
        assert post == pre  # refused lint must not create a commit
