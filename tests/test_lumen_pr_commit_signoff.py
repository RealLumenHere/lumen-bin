"""wq#5138 phase 1: lumen-pr commit appends a Signed-off-by trailer after Assisted-by, once.

LUMEN_PR_BIN overrides the script under test; default is the copy next to this tests/ dir.
"""
import subprocess

from test_lumen_pr_commit_coauthor import LUMEN_PR, GIT_ENV, _repo, git

SIGNOFF = "Signed-off-by: Lumen Industries <lumen@lumennode.uk>"


def _msg(tmp_path, body=None):
    r = _repo(tmp_path)
    args = [LUMEN_PR, "commit", str(r), "feat(x): add y", "--refs", "wq#1"]
    if body is not None:
        bf = tmp_path / "body.md"
        bf.write_text(body)
        args += ["--body-file", str(bf)]
    p = subprocess.run(args, capture_output=True, text=True, env=GIT_ENV)
    assert p.returncode == 0, p.stdout + p.stderr
    return git(r, "log", "-1", "--format=%B").stdout


def test_commit_appends_signoff_last(tmp_path):
    lines = [l for l in _msg(tmp_path).splitlines() if l]
    assert lines[-1] == SIGNOFF
    assert lines[-2].startswith("Assisted-by:")
    assert lines[-3] == "Refs: wq#1"


def test_commit_does_not_duplicate_callers_signoff(tmp_path):
    own = "Signed-off-by: Frank <frank@example.invalid>"
    msg = _msg(tmp_path, f"body text\n\n{own}\n")
    assert msg.count("Signed-off-by:") == 1
    assert own in msg
