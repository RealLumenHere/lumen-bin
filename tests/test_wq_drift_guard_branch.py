"""wq#1653 — wq-drift-guard reports a repo parked off its default branch (BRANCH)
distinctly from a clobbered file (CLOBBER).

On 2026-09-10 /home/lumen/bin sat on feat/wq1374-... and dirty. The guard compared the
deployed `wq` against HEAD, saw a difference, and fired "wq DRIFT DETECTED — a lane
likely clobbered it" every 5 minutes. The fault was the whole tree being off main, which
primary_tree_guard.py owns; `--fix` would have deployed the *feature branch's* `wq`.

Zero-quota subprocess pattern, same as test_wq_archetype_guard.py: each test builds a
throwaway git repo under tmp_path and drives the real script against it via the
WQ_DRIFT_GUARD_* env overrides, with a stub ntfy-send on PATH recording alert titles.
"""
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.environ.get("WQ_DRIFT_GUARD_BIN", os.path.join(REPO, "wq-drift-guard"))

CLEAN, CLOBBER, USAGE, REFUSED, BRANCH = 0, 1, 2, 3, 4


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   capture_output=True, text=True)


def make_repo(tmp_path, branch=None, clobber=False):
    """A git repo with a committed `wq` on main, optionally parked on a branch
    and/or with the deployed file overwritten."""
    repo = tmp_path / "bin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@lumen.local")
    git(repo, "config", "user.name", "test")
    (repo / "wq").write_text("#!/usr/bin/env python3\nprint('reviewed')\n")
    git(repo, "add", "wq")
    git(repo, "commit", "-qm", "wq")
    if branch:
        git(repo, "checkout", "-q", "-b", branch)
        (repo / "wq").write_text("#!/usr/bin/env python3\nprint('branch work')\n")
        git(repo, "commit", "-qam", "branch work")
    if clobber:
        (repo / "wq").write_text("#!/usr/bin/env python3\nprint('clobbered')\n")
    return repo


def make_env(tmp_path, repo):
    """Env pointing the guard at the scratch repo, with ntfy-send stubbed to a log."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    log = tmp_path / "ntfy.log"
    stub = stub_dir / "ntfy-send"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$NTFY_LOG"\n')
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["NTFY_LOG"] = str(log)
    env["WQ_DRIFT_GUARD_REPO"] = str(repo)
    env["WQ_DRIFT_GUARD_STATE"] = str(tmp_path / "guard.state")
    env["WQ_DRIFT_GUARD_LOCK"] = str(tmp_path / "guard.lock")
    return env, log


def guard(env, *args):
    return subprocess.run([GUARD] + list(args), capture_output=True, text=True, env=env)


def alerts(log):
    return log.read_text() if log.exists() else ""


# ── BRANCH: the repo itself is parked off its default branch ──

def test_status_reports_branch_not_clobber(tmp_path):
    repo = make_repo(tmp_path, branch="feat/wq1374-orch")
    env, _ = make_env(tmp_path, repo)
    r = guard(env, "--status")
    assert r.returncode == BRANCH, r.stdout + r.stderr
    assert "BRANCH" in r.stdout
    assert "feat/wq1374-orch" in r.stdout and "main" in r.stdout
    assert "DRIFTED" not in r.stdout


def test_check_alerts_under_its_own_title(tmp_path):
    repo = make_repo(tmp_path, branch="feat/wq1374-orch")
    env, log = make_env(tmp_path, repo)
    r = guard(env, "--check")
    assert r.returncode == BRANCH, r.stdout + r.stderr
    sent = alerts(log)
    assert "wq REPO OFF DEFAULT BRANCH" in sent
    assert "wq DRIFT DETECTED" not in sent
    assert "clobber" not in sent.lower() or "NOT a wq clobber" in sent


def test_check_dedups_per_branch_episode(tmp_path):
    repo = make_repo(tmp_path, branch="feat/wq1374-orch")
    env, log = make_env(tmp_path, repo)
    assert guard(env, "--check").returncode == BRANCH
    assert guard(env, "--check").returncode == BRANCH
    assert alerts(log).count("wq REPO OFF DEFAULT BRANCH") == 1
    # a *different* wrong branch is a new episode and alerts again
    git(repo, "checkout", "-q", "-b", "feat/other")
    assert guard(env, "--check").returncode == BRANCH
    assert alerts(log).count("wq REPO OFF DEFAULT BRANCH") == 2


def test_branch_state_clears_on_return_to_main(tmp_path):
    repo = make_repo(tmp_path, branch="feat/wq1374-orch")
    env, log = make_env(tmp_path, repo)
    assert guard(env, "--check").returncode == BRANCH
    git(repo, "checkout", "-q", "main")
    assert guard(env, "--check").returncode == CLEAN
    assert not os.path.exists(env["WQ_DRIFT_GUARD_STATE"] + ".branch")
    # drifting off again re-alerts rather than staying muted by the stale record
    git(repo, "checkout", "-q", "feat/wq1374-orch")
    assert guard(env, "--check").returncode == BRANCH
    assert alerts(log).count("wq REPO OFF DEFAULT BRANCH") == 2


def test_branch_takes_precedence_over_file_drift(tmp_path):
    """The wq#1653 incident exactly: off main AND the deployed file modified."""
    repo = make_repo(tmp_path, branch="feat/wq1374-orch", clobber=True)
    env, log = make_env(tmp_path, repo)
    r = guard(env, "--check")
    assert r.returncode == BRANCH, r.stdout + r.stderr
    assert "wq DRIFT DETECTED" not in alerts(log)
    s = guard(env, "--status")
    assert s.returncode == BRANCH
    assert "also differs" in s.stdout


def test_fix_refuses_on_branch_and_leaves_the_file_alone(tmp_path):
    repo = make_repo(tmp_path, branch="feat/wq1374-orch", clobber=True)
    env, _ = make_env(tmp_path, repo)
    before = (repo / "wq").read_text()
    r = guard(env, "--fix")
    assert r.returncode == BRANCH, r.stdout + r.stderr
    assert "REFUSING --fix" in r.stdout
    assert (repo / "wq").read_text() == before


def test_detached_head_counts_as_branch(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "--detach")
    env, _ = make_env(tmp_path, repo)
    assert guard(env, "--status").returncode == BRANCH


# ── CLOBBER: on the default branch, one file overwritten (wq#886, unchanged) ──

def test_clobber_on_main_still_reports_clobber(tmp_path):
    repo = make_repo(tmp_path, clobber=True)
    env, log = make_env(tmp_path, repo)
    r = guard(env, "--check")
    assert r.returncode == CLOBBER, r.stdout + r.stderr
    assert "wq DRIFT DETECTED" in alerts(log)
    assert "wq REPO OFF DEFAULT BRANCH" not in alerts(log)


def test_clean_on_main_is_silent(tmp_path):
    repo = make_repo(tmp_path)
    env, log = make_env(tmp_path, repo)
    r = guard(env, "--check")
    assert r.returncode == CLEAN, r.stdout + r.stderr
    assert alerts(log) == ""


def test_status_on_main_clean(tmp_path):
    repo = make_repo(tmp_path)
    env, _ = make_env(tmp_path, repo)
    r = guard(env, "--status")
    assert r.returncode == CLEAN
    assert "CLEAN" in r.stdout
