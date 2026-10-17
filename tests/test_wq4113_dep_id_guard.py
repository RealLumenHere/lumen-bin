"""wq#4113 -- `wq add`/`wq edit` refuse a --deps clause deps_met() can never resolve.

deps_met() (wq:1065) does `SELECT status FROM tasks WHERE id=?` against the int-keyed
`id` column. A wq-prefixed dep ("wq3941") or one that doesn't exist never matches, so
`claim --id` refuses forever with a plausible-looking "deps not met" even once the real
dependency is done (found live on wq#3950/#3980/#4071, iso-build-f85923). This guard is
the producer-side fix, same fail-closed (force-overridable) shape as the wq#3033 --gate
guard (test_wq3033_gate_path_guard.py): refuse the bad value at write time, and normalize
a wq/wq#-prefixed id to the bare id instead of refusing it outright, since that form is
unambiguous and worth accepting.

Same zero-quota subprocess pattern as test_wq3033_gate_path_guard.py: every assertion
drives the real `wq` binary against a scratch DB under tmp_path.
"""
import os
import sqlite3
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-dep-id-guard"
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


def added_id(p):
    return int(p.stdout.strip().split()[2])


def show_field(env, tid, field):
    out = wq_ok(env, "show", str(tid)).stdout
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == field:
            return v.strip()
    return None


def test_add_refuses_a_wq_prefixed_dep_on_a_nonexistent_task(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "depends on wq-prefixed ghost", deps="wq9999")
    assert p.returncode != 0
    assert "deps_met() can never resolve" in p.stdout
    assert "no such task" in p.stdout


def test_add_normalizes_a_wq_prefixed_dep_on_a_real_task(tmp_path):
    env = make_env(tmp_path)
    dep_id = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "the dep"))
    p = add(env, "depends on wq-prefixed real task", deps=f"wq{dep_id}")
    assert p.returncode == 0
    child_id = added_id(p)
    assert show_field(env, child_id, "deps") == str(dep_id)


def test_add_normalizes_a_hash_prefixed_dep(tmp_path):
    env = make_env(tmp_path)
    dep_id = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "the dep"))
    p = add(env, "depends on hash-prefixed real task", deps=f"wq#{dep_id}")
    assert p.returncode == 0


def test_add_refuses_non_numeric_dep(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "depends on prose", deps="the previous ticket")
    assert p.returncode != 0
    assert "not an id" in p.stdout


def test_add_accepts_a_bare_numeric_dep_on_a_real_task(tmp_path):
    env = make_env(tmp_path)
    dep_id = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "the dep"))
    p = add(env, "depends on bare numeric real task", deps=str(dep_id))
    assert p.returncode == 0


def test_add_force_overrides_the_refusal(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "forced through anyway", deps="wq9999", force=True)
    assert p.returncode == 0


def test_add_without_deps_is_unaffected(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "no deps at all")
    assert p.returncode == 0


def test_edit_refuses_a_wq_prefixed_dep_on_a_nonexistent_task(tmp_path):
    env = make_env(tmp_path)
    tid = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "edit target"))
    p = wq(env, "edit", str(tid), "--deps", "wq9999")
    assert p.returncode != 0
    assert "no such task" in p.stdout


def test_edit_normalizes_a_wq_prefixed_dep_on_a_real_task(tmp_path):
    env = make_env(tmp_path)
    dep_id = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "the dep"))
    tid = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "edit target"))
    p = wq(env, "edit", str(tid), "--deps", f"wq{dep_id}")
    assert p.returncode == 0
    assert show_field(env, tid, "deps") == str(dep_id)


def test_edit_force_overrides_the_refusal(tmp_path):
    env = make_env(tmp_path)
    tid = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "edit target"))
    p = wq(env, "edit", str(tid), "--deps", "wq9999", "--force")
    assert p.returncode == 0


def test_deps_met_resolves_the_normalized_dep_after_it_is_done(tmp_path):
    """The actual bug: before this fix a wq-prefixed dep never resolved even once done,
    so claim --id refused forever with a plausible-looking 'deps not met'."""
    env = make_env(tmp_path)
    dep_id = added_id(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "the dep"))
    child_id = added_id(add(env, "depends on wq-prefixed real task", deps=f"wq{dep_id}"))
    blocked = wq(env, "claim", "tester", "--id", str(child_id))
    assert blocked.returncode != 0

    db = sqlite3.connect(env["WQ_DB"])
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (dep_id,))
    db.commit(); db.close()

    unblocked = wq(env, "claim", "tester", "--id", str(child_id))
    assert unblocked.returncode == 0, f"{unblocked.returncode}\n{unblocked.stdout}\n{unblocked.stderr}"
