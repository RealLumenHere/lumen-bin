"""wq#5133: lumen-pr land requires a repo's configured test suite to PASS on the PR head.

Drives the REAL suite_gate (the function cmd_land calls) through `lumen-pr suite --head REV
--gate`, against a throwaway bare origin + clone, so no forge/broker is needed. The suite
command is a stand-in (a one-line python that prints a pytest-style summary and exits 0/1):
what is under test is the gate, not pytest.

LUMEN_PR_BIN overrides the script under test; default is the copy next to this tests/ dir, so
a worktree tests its own lumen-pr, never the deployed one.
"""
import os
import subprocess
from pathlib import Path

import pytest

LUMEN_PR = os.environ.get("LUMEN_PR_BIN", str(Path(__file__).resolve().parents[1] / "lumen-pr"))
BASE_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "LUMEN_PR_"))},
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "LUMEN_PR_SCOPE_CHECK": "0",
}
PASS_CMD = "python3 -c 'print(\"3 passed, 7 skipped in 0.01s\")'"
FAIL_CMD = "python3 -c 'import sys; print(\"1 failed, 2 passed in 0.01s\"); sys.exit(1)'"


def git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), *args], env=BASE_ENV,
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(tree, name, text, msg):
    (Path(tree) / name).write_text(text)
    git(tree, "add", name)
    git(tree, "commit", "-q", "-m", msg)
    return git(tree, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path):
    """bare origin owner/repo.git on main, a clone with feat/x one commit ahead."""
    origin = tmp_path / "srv" / "owner" / "repo.git"
    origin.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=BASE_ENV, check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], env=BASE_ENV, check=True,
                   capture_output=True)
    commit(seed, "f.txt", "v1\n", "chore: seed")
    git(seed, "push", "-q", "origin", "main")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], env=BASE_ENV, check=True)
    git(repo, "checkout", "-q", "-b", "feat/x")
    head = commit(repo, "branch_only.txt", "b\n", "feat: x")
    conf = tmp_path / "suites.conf"
    e = {**BASE_ENV,
         "LUMEN_PR_SUITES_CONF": str(conf),
         "LUMEN_PR_SUITE_STATE": str(tmp_path / "state"),
         "LUMEN_PR_SUITE_WT_ROOT": str(tmp_path / "wt"),
         "LUMEN_PR_SUITE_INLINE": "1"}
    return {"tmp": tmp_path, "origin": origin, "seed": seed, "repo": repo, "head": head,
            "conf": conf, "env": e}


def set_cmd(env, cmd, slug="owner/repo"):
    env["conf"].write_text(f"# test\n{slug} {cmd}\n")


def gate(env, head=None, extra_env=None):
    p = subprocess.run([LUMEN_PR, "suite", str(env["repo"]), "--head", head or env["head"], "--gate"],
                       capture_output=True, text=True, env={**env["env"], **(extra_env or {})})
    return p.returncode, p.stdout + p.stderr


def stamp(env, head=None):
    return env["tmp"] / "state" / "owner__repo" / f"{head or env['head']}.result"


def fields(path):
    return dict(l.split("=", 1) for l in path.read_text().splitlines() if "=" in l)


def test_unconfigured_repo_is_not_gated(env):
    set_cmd(env, PASS_CMD, slug="other/repo")
    rc, out = gate(env)
    assert rc == 0, out
    assert "no required suite" in out
    assert not (env["tmp"] / "state").exists()


def test_passing_suite_lets_land_through_and_records_head(env):
    set_cmd(env, PASS_CMD)
    rc, out = gate(env)
    assert rc == 0, out
    assert "required suite PASSED" in out and "3 passed, 7 skipped" in out
    f = fields(stamp(env))
    assert f["rc"] == "0" and f["head"] == env["head"]
    assert f["base"] == git(env["repo"], "rev-parse", "origin/main")
    # suite worktree cleaned up, no .running marker left
    assert list((env["tmp"] / "wt").iterdir()) == []
    assert not stamp(env).with_suffix(".running").exists()


def test_failing_suite_refuses_land_and_keeps_refusing(env):
    set_cmd(env, FAIL_CMD)
    rc, out = gate(env)
    assert rc != 0
    assert "required suite FAILED" in out and "1 failed, 2 passed" in out
    assert fields(stamp(env))["rc"] == "1"
    # same head again: still refused, from the stamp, without a silent re-run turning it green
    set_cmd(env, PASS_CMD)
    rc, out = gate(env)
    assert rc != 0 and "FAILED" in out


def test_new_head_gets_a_fresh_run(env):
    set_cmd(env, FAIL_CMD)
    assert gate(env)[0] != 0
    set_cmd(env, PASS_CMD)
    new_head = commit(env["repo"], "fix.txt", "f\n", "fix: x")
    rc, out = gate(env, head=new_head)
    assert rc == 0, out
    assert fields(stamp(env, new_head))["rc"] == "0"


def test_suite_runs_on_head_merged_onto_current_main(env):
    # main moves after the branch was cut: the run must see BOTH sides (what the squash lands)
    commit(env["seed"], "main_only.txt", "m\n", "chore: main moves")
    git(env["seed"], "push", "-q", "origin", "main")
    set_cmd(env, "test -f main_only.txt && test -f branch_only.txt && echo '2 passed in 0.01s'")
    rc, out = gate(env)
    assert rc == 0, out


def test_suite_on_branch_alone_would_miss_main(env):
    commit(env["seed"], "main_only.txt", "m\n", "chore: main moves")
    git(env["seed"], "push", "-q", "origin", "main")
    set_cmd(env, "test ! -f main_only.txt")   # passes only if main was NOT merged in
    rc, out = gate(env)
    assert rc != 0, out


def test_merge_conflict_is_refused_without_running(env):
    commit(env["seed"], "branch_only.txt", "conflicting\n", "chore: same path on main")
    git(env["seed"], "push", "-q", "origin", "main")
    set_cmd(env, PASS_CMD)
    rc, out = gate(env)
    assert rc != 0
    assert "does not merge cleanly" in out
    assert fields(stamp(env))["rc"] == "merge-conflict"
    assert list((env["tmp"] / "wt").iterdir()) == []


def _fake_run(env, status):
    fake = env["tmp"] / "fake-lumen-run"
    fake.write_text("#!/bin/bash\n"
                    "case \"$1\" in start) echo UNIT=lumen-job-fake-1 ;; "
                    f"status) echo {status} ;; esac\n")
    fake.chmod(0o755)
    return {"LUMEN_PR_SUITE_INLINE": "0", "LUMEN_PR_SUITE_RUN": str(fake)}


def test_detached_run_is_started_by_land_then_reported_running(env):
    set_cmd(env, PASS_CMD)
    x = _fake_run(env, "RUNNING")
    rc, out = gate(env, extra_env=x)
    assert rc != 0 and "STARTED it now as lumen-job-fake-1" in out
    rc, out = gate(env, extra_env=x)
    assert rc != 0 and "still RUNNING" in out and "lumen-job-fake-1" in out


def test_dead_runner_marker_is_not_mistaken_for_running(env):
    set_cmd(env, PASS_CMD)
    assert "STARTED" in gate(env, extra_env=_fake_run(env, "RUNNING"))[1]
    # the unit died without writing a result: next land must start a new run, not wait forever
    rc, out = gate(env, extra_env=_fake_run(env, "GONE"))
    assert rc != 0 and "STARTED it now" in out and "still RUNNING" not in out


def test_status_and_rerun(env):
    set_cmd(env, FAIL_CMD)
    p = subprocess.run([LUMEN_PR, "suite", str(env["repo"]), "--head", env["head"], "--status"],
                       capture_output=True, text=True, env=env["env"])
    assert p.returncode == 4 and "NONE" in p.stdout
    gate(env)
    set_cmd(env, PASS_CMD)
    p = subprocess.run([LUMEN_PR, "suite", str(env["repo"]), "--head", env["head"], "--rerun"],
                       capture_output=True, text=True, env=env["env"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert gate(env)[0] == 0
