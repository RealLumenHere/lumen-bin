"""wq#3265: `lumen-run wait` is clamped so it answers inside a Commander call window.

systemctl and sleep are stubbed on PATH so no unit runs and no time passes: the unit is
reported active forever, which is exactly the shape of a long job being waited on.
"""
import os
import stat
import subprocess

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lumen-run")


@pytest.fixture
def stubs(tmp_path):
    d = tmp_path / "stubs"
    d.mkdir()
    ticks = tmp_path / "ticks"
    # systemctl is-active --quiet <unit>: active, forever (or until the unit file says done)
    (d / "systemctl").write_text(
        '#!/bin/sh\n[ -e "%s/finished" ] && exit 3\nexit 0\n' % tmp_path)
    # sleep counts calls instead of sleeping
    (d / "sleep").write_text('#!/bin/sh\necho x >> "%s"\n' % ticks)
    (d / "journalctl").write_text("#!/bin/sh\necho LUMEN_JOB_EXIT=0\n")
    (d / "hostname").write_text("#!/bin/sh\necho lumen\n")
    for f in d.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return d, ticks


def run_wait(stubs, *args, env_extra=None):
    d, ticks = stubs
    env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
    env.pop("LUMEN_RUN_WAIT_CAP", None)
    env.update(env_extra or {})
    p = subprocess.run(["bash", BIN, "wait", *args], capture_output=True, text=True,
                       env=env, timeout=30)
    n = len(ticks.read_text().split()) if ticks.exists() else 0
    return p, n


def test_a_long_budget_is_clamped_to_55s_and_says_how_to_re_poll(stubs):
    p, ticks = run_wait(stubs, "lumen-job-x", "840")
    assert p.returncode == 124
    assert "clamped to 55s" in p.stdout
    assert "still RUNNING), re-poll with: lumen-run wait lumen-job-x" in p.stdout
    assert ticks * 3 <= 57          # never waited past the cap (3s per tick)


def test_the_default_budget_is_clamped_too(stubs):
    p, ticks = run_wait(stubs, "lumen-job-x")
    assert p.returncode == 124 and "clamped to 55s" in p.stdout
    assert ticks * 3 <= 57


def test_a_budget_under_the_cap_is_untouched(stubs):
    p, ticks = run_wait(stubs, "lumen-job-x", "9")
    assert p.returncode == 124
    assert "clamped" not in p.stdout and "re-poll" not in p.stdout
    assert "TIMEOUT after 9s (still RUNNING)" in p.stdout and ticks == 3


def test_the_cap_is_overridable_for_callers_outside_a_commander_call(stubs):
    p, ticks = run_wait(stubs, "lumen-job-x", "840", env_extra={"LUMEN_RUN_WAIT_CAP": "12"})
    assert "clamped to 12s" in p.stdout and ticks == 4


def test_a_finished_unit_still_reports_its_result(stubs, tmp_path):
    (tmp_path / "finished").write_text("")
    p, _ = run_wait(stubs, "lumen-job-x", "840")
    assert p.returncode == 0 and p.stdout.strip().endswith("DONE:0")
