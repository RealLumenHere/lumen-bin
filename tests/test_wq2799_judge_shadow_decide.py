"""wq#2799 -- shadow-mode judge observation on `wq decide` (the AUTONOMY POLICY's
"judgment call goes to the orchestrator session" moment, concretely). Same
zero-quota subprocess pattern as test_wq_lease_fence.py: drives the real `wq`
binary against a scratch DB under tmp_path.

The real judge / lumen-run are never invoked here: WQ_JUDGE_RUNNER points at a
fake script that only proves it was (or wasn't) launched with the right shape,
since a unit test must never depend on live systemd/hathor being reachable.
"""
import json
import os
import re
import subprocess
import textwrap
import time


def _wait_for(path, timeout=5.0):
    """Popen launches detached and does not wait -- poll briefly for the fake
    runner's own (near-instant) startup rather than assuming it has already run
    by the time wq_ok() returns."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))


def make_env(tmp_path, agent="test-decide-agent"):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    # wq#912: scratch lane names are never real dispatcher lanes -- point the lane
    # guard at a path that does not exist so it fails open instead of consulting
    # the live config.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env["WQ_SCRATCH_ROOT"] = str(tmp_path / "scratch")
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def wq_ok(env, *args):
    p = wq(env, *args)
    assert p.returncode == 0, f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


def add_frank_gated(env, title="a risky judgment call"):
    p = wq_ok(env, "add", "--lane", "user", "--pri", "1", "--title", title,
             "--detail", "needs a judgment call this ticket should not make blind",
             "--frank-gated", "--no-probe", "policy sign-off, not machine-checkable")
    m = re.search(r"added task (\d+)", p.stdout)
    assert m, p.stdout
    return int(m.group(1))


FAKE_RUNNER_TOUCH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    # mirrors `lumen-run start --name N -- judge --actor ... --command ... --context ...`
    with open(os.environ["FAKE_RUNNER_LOG"], "w") as f:
        json.dump(sys.argv[1:], f)
    sys.exit(0)
""")


def _fake_runner(tmp_path):
    p = tmp_path / "fake_lumen_run.py"
    p.write_text(FAKE_RUNNER_TOUCH)
    p.chmod(0o755)
    return str(p)


def test_decide_fires_shadow_judge_via_detached_runner(tmp_path):
    env = make_env(tmp_path)
    tid = add_frank_gated(env)
    logfile = tmp_path / "runner_call.json"
    env["WQ_JUDGE_RUNNER"] = _fake_runner(tmp_path)
    env["FAKE_RUNNER_LOG"] = str(logfile)
    env["FOREMAN_JUDGE_SHADOW"] = "1"
    p = wq_ok(env, "decide", tid, "--prompt", "should we rotate this key now?",
             "--rec", "yes, backup already taken", "--no-probe", "not machine-checkable")
    assert f"decided wq#{tid}: frank_gated -> pending_review" in p.stdout
    assert _wait_for(logfile), "shadow judge runner was never invoked"
    args = json.loads(logfile.read_text())
    assert args[:2] == ["start", "--name"], args
    assert args[2] == f"wq{tid}-judge-shadow"
    assert "--" in args
    judge_args = args[args.index("--") + 1:]
    assert judge_args[0].endswith("judge")
    assert "--gate-verdict" in judge_args and "AMBER" in judge_args
    assert "--kind" in judge_args and "advice" in judge_args
    ctx = judge_args[judge_args.index("--context") + 1]
    assert "should we rotate this key now?" in ctx
    assert "yes, backup already taken" in ctx


def test_decide_disabled_skips_the_shadow_call(tmp_path):
    env = make_env(tmp_path)
    tid = add_frank_gated(env)
    logfile = tmp_path / "runner_call.json"
    env["WQ_JUDGE_RUNNER"] = _fake_runner(tmp_path)
    env["FAKE_RUNNER_LOG"] = str(logfile)
    env["FOREMAN_JUDGE_SHADOW"] = "0"
    wq_ok(env, "decide", tid, "--prompt", "p", "--rec", "r", "--no-probe", "not machine-checkable")
    assert not _wait_for(logfile, timeout=1.0), "shadow judge fired despite FOREMAN_JUDGE_SHADOW=0"


def test_decide_state_identical_with_shadow_on_or_off(tmp_path):
    """The DB row `wq decide` writes must not depend on the shadow flag at all."""
    rows = {}
    for flag in ("0", "1"):
        env = make_env(tmp_path, agent=f"agent-{flag}")
        tid = add_frank_gated(env, title=f"ticket for flag {flag}")
        env["WQ_JUDGE_RUNNER"] = _fake_runner(tmp_path)
        env["FAKE_RUNNER_LOG"] = str(tmp_path / f"log_{flag}.json")
        env["FOREMAN_JUDGE_SHADOW"] = flag
        p = wq_ok(env, "decide", tid, "--prompt", "same prompt", "--rec", "same rec",
                 "--no-probe", "not machine-checkable")
        assert p.returncode == 0
        show = wq_ok(env, "show", tid, "--json")
        rows[flag] = json.loads(show.stdout)
    for k in ("status", "decision_prompt", "orch_recommendation", "gate_type"):
        assert rows["0"][k] == rows["1"][k], k


def test_decide_survives_a_broken_runner(tmp_path):
    env = make_env(tmp_path)
    tid = add_frank_gated(env)
    env["WQ_JUDGE_RUNNER"] = str(tmp_path / "no-such-runner-binary")
    env["FOREMAN_JUDGE_SHADOW"] = "1"
    p = wq_ok(env, "decide", tid, "--prompt", "p", "--rec", "r",
             "--no-probe", "not machine-checkable")
    assert f"decided wq#{tid}: frank_gated -> pending_review" in p.stdout
