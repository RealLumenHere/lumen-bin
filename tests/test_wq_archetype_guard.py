"""wq#1368 — `wq add`/`wq edit` refuse an archetype with no routing row in models.yaml.

wq#1358/#1359 carried archetype='build' (a LANE name, not an archetype) straight onto a
ticket the dispatcher could never route — orchestrator/router.py's Manifest.chain()
raised ManifestError('no routing row for archetype build') and, uncaught, that crashed
the whole dispatch pass (harness/foreman_dispatcher.py, hardened separately). This guard
is the producer-side half: refuse the bad value at the point it's written, same fail-open
shape as the existing wq#912 lane guard (an unreadable manifest never blocks `wq add`).

Same zero-quota subprocess pattern as test_wq_lease_fence.py: every assertion drives the
real `wq` binary against a scratch DB + a scratch models.yaml under tmp_path.
"""
import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))

SCRATCH_ROUTING = {"routing": {"standard-engineering": [], "mechanical-edit": []}}


def make_env(tmp_path, models_yaml=True):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-archetype-guard"
    # wq#912: fail the lane guard open, same as the other CLI tests in this dir.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    if models_yaml:
        p = tmp_path / "models.yaml"
        p.write_text(json.dumps(SCRATCH_ROUTING))  # valid YAML subset
        env["FOREMAN_MODELS_YAML"] = str(p)
    else:
        env["FOREMAN_MODELS_YAML"] = str(tmp_path / "no-such-models.yaml")
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


def test_add_refuses_a_lane_name_masquerading_as_an_archetype(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "wq1358 repro", archetype="build")
    assert p.returncode != 0
    assert "no routing row" in p.stdout
    assert "standard-engineering" in p.stdout  # known archetypes listed


def test_add_accepts_a_real_archetype(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "valid archetype", archetype="standard-engineering")
    assert p.returncode == 0


def test_add_without_archetype_is_unaffected(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "let the router classify it")
    assert p.returncode == 0


def test_add_force_overrides_the_refusal(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "forced through anyway", archetype="build", force=True)
    assert p.returncode == 0


def test_add_fails_open_when_manifest_unreadable(tmp_path):
    env = make_env(tmp_path, models_yaml=False)
    p = add(env, "manifest missing, never blocks add", archetype="build")
    assert p.returncode == 0


def test_edit_refuses_a_bad_archetype(tmp_path):
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1",
                    "--title", "edit target").stdout.strip().split()[2])
    p = wq(env, "edit", str(tid), "--archetype", "build")
    assert p.returncode != 0
    assert "no routing row" in p.stdout


def test_edit_empty_string_clears_the_archetype(tmp_path):
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "edit target",
                    "--archetype", "standard-engineering").stdout.strip().split()[2])
    p = wq(env, "edit", str(tid), "--archetype", "")
    assert p.returncode == 0
