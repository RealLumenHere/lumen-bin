"""wq#3033 -- `wq add`/`wq edit` refuse a --gate clause that isn't an absolute or ~ path.

wq gate is an enforced file-path precondition (gate_met(): comma list of
path[:exists|missing]), but add/edit used to accept any string. Acceptance prose typed
into --gate (wq#2844, wq#2484) parses as a path that never exists, so `claim --id` is
refused forever with no way out short of a manual `wq edit --gate ''`. This guard is the
producer-side fix: refuse the bad value at the point it's written, same fail-closed
(force-overridable) shape as the existing wq#912 lane guard and wq#1368 archetype guard
(test_wq_archetype_guard.py).

Same zero-quota subprocess pattern as test_wq_archetype_guard.py: every assertion drives
the real `wq` binary against a scratch DB under tmp_path.
"""
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-gate-path-guard"
    # wq#912: fail the lane guard open, same as the other CLI tests in this dir.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def wq_ok(env, *args):
    p = wq(env, *args)
    assert p.returncode == 0, f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


def add(env, title, **kw):
    args = ["add", "--lane", "auto", "--pri", "1", "--title", title]
    for k, v in kw.items():
        flag = f"--{k.replace('_', '-')}"
        args += [flag] if v is True else [flag, v]
    return wq(env, *args)


def test_add_refuses_prose_masquerading_as_a_gate(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "wq2844 repro", gate="netdata bundle confirmed live on both Mac Pros")
    assert p.returncode != 0
    assert "not an absolute or ~ path" in p.stdout
    assert "--detail" in p.stdout


def test_add_accepts_an_absolute_path_gate(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "real path gate", gate="/home/lumen/kb/ISSUE-LOG.md")
    assert p.returncode == 0


def test_add_accepts_a_tilde_path_gate(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "tilde path gate", gate="~/kb/ISSUE-LOG.md:exists")
    assert p.returncode == 0


def test_add_accepts_a_multi_clause_path_gate(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "multi clause gate",
            gate="/home/lumen/kb/ISSUE-LOG.md,/tmp/does-not-exist:missing")
    assert p.returncode == 0


def test_add_without_gate_is_unaffected(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "no gate at all")
    assert p.returncode == 0


def test_add_force_overrides_the_refusal(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "forced through anyway", gate="acceptance prose, not a path", force=True)
    assert p.returncode == 0


def test_edit_refuses_prose_masquerading_as_a_gate(tmp_path):
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1",
                    "--title", "edit target").stdout.strip().split()[2])
    p = wq(env, "edit", str(tid), "--gate", "some free-text acceptance note")
    assert p.returncode != 0
    assert "not an absolute or ~ path" in p.stdout


def test_edit_accepts_an_absolute_path_gate(tmp_path):
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1",
                    "--title", "edit target").stdout.strip().split()[2])
    p = wq(env, "edit", str(tid), "--gate", "/home/lumen/kb/ISSUE-LOG.md:exists")
    assert p.returncode == 0


def test_edit_empty_string_clears_the_gate(tmp_path):
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "edit target",
                    "--gate", "/home/lumen/kb/ISSUE-LOG.md").stdout.strip().split()[2])
    p = wq(env, "edit", str(tid), "--gate", "")
    assert p.returncode == 0


def test_edit_force_overrides_the_refusal(tmp_path):
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1",
                    "--title", "edit target").stdout.strip().split()[2])
    p = wq(env, "edit", str(tid), "--gate", "prose again", "--force")
    assert p.returncode == 0
