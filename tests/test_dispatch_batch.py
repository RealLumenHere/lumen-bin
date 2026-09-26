"""wq#1255 / ISSUE-SOLUTIONS-fable.md #18 — dispatch-batch: spawns run detached, the
UNIT of each gets logged, each ticket's claim gets verified, and none of it dies with
the caller's shell.

Same zero-quota subprocess pattern as test_wq_lease_fence.py: every assertion drives
the real `dispatch-batch` and `wq` binaries against a scratch DB under tmp_path, never
the live queue, and never a real fworker/systemd-run (unavailable/undesirable in a
test — see DISPATCH_BATCH_SPAWN_CMD below).

The production worker-launch path (orchestrator/router.py's `dispatch-cmd` piped into
`lumen-run start`, i.e. a real systemd transient unit) needs `sudo systemd-run`, which
is neither available nor safe to invoke from a test. DISPATCH_BATCH_SPAWN_CMD is the
seam dispatch-batch exposes for this: a stand-in "worker" that must behave like
lumen-run in the one way dispatch-batch depends on (print `UNIT=<name>` on its last
stdout line), and is otherwise free to fake the rest. `stub_spawn` below is that
stand-in; recipes/marker files under tmp_path steer its per-ticket behavior.
"""
import os
import re
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCH_BATCH_BIN = os.environ.get("DISPATCH_BATCH_BIN", os.path.join(REPO, "dispatch-batch"))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))
FMT = "%Y-%m-%d %H:%M:%S"

STUB_SPAWN = """#!/usr/bin/env bash
set -uo pipefail
id="$1"
recipe=$(cat "$SPAWN_RECIPE_DIR/$id" 2>/dev/null || echo claim_immediate)
case "$recipe" in
  fail)
    echo "stub: forced failure for $id" >&2
    exit 1
    ;;
  never_claim)
    :
    ;;
  claim_immediate)
    "$WQ_BIN" claim --id "$id" --lane test-lane "stub-agent-$id" >/dev/null 2>&1
    ;;
  survive)
    "$WQ_BIN" claim --id "$id" --lane test-lane "stub-agent-$id" >/dev/null 2>&1
    printf '%s\\n' "$$" > "$MARKER_DIR/$id.spawner_pid"
    ( sleep "${SPAWN_SURVIVE_SLEEP:-2}"
      set -- $(cat /proc/self/stat)
      printf '%s\\n' "$4" > "$MARKER_DIR/$id.ppid"
      echo done > "$MARKER_DIR/$id.done"
    ) </dev/null >/dev/null 2>&1 &
    disown
    ;;
esac
echo "UNIT=stub-$id"
"""


def make_env(tmp_path, **overrides):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "dispatch-batch-test"
    # wq#912: scratch lane is never a real dispatcher lane — point the guard at a path
    # that doesn't exist so it fails open instead of consulting the live config.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    # wq#2631/wq#2647: same fail-open pattern as WQ_DISPATCHER_CONFIG above -- a test that
    # doesn't care about admission shouldn't be at the mercy of the real box's live CPU
    # load/concurrency state. Point at a path that doesn't exist so admission_check()
    # always admits; tests that DO care override DISPATCH_BATCH_ADMISSION_CMD instead
    # (which takes priority over this path entirely -- see admission_check() itself).
    env["DISPATCH_BATCH_ADMISSION_GATE"] = str(tmp_path / "no-such-admission-gate.py")
    env["WQ_BIN"] = WQ_BIN
    env["DISPATCH_BATCH_RUN_DIR"] = str(tmp_path / "runs")
    env["SPAWN_RECIPE_DIR"] = str(tmp_path / "recipes")
    env["MARKER_DIR"] = str(tmp_path / "runs")
    env["DISPATCH_BATCH_CLAIM_WAIT_S"] = "5"
    env["DISPATCH_BATCH_CLAIM_POLL_S"] = "1"
    env.update(overrides)
    return env


def write_stub(tmp_path):
    stub = tmp_path / "stub_spawn.sh"
    stub.write_text(STUB_SPAWN)
    stub.chmod(0o755)
    return str(stub)


def add_ticket(env, title, lane="test-lane"):
    p = subprocess.run([WQ_BIN, "add", "--lane", lane, "--title", title],
                        capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"wq add failed: {p.stdout}\n{p.stderr}"
    m = re.search(r"added task (\d+)", p.stdout)
    assert m, f"could not parse ticket id from: {p.stdout}"
    return int(m.group(1))


def set_recipe(tmp_path, ticket_id, recipe):
    d = tmp_path / "recipes"
    d.mkdir(exist_ok=True)
    (d / str(ticket_id)).write_text(recipe)


def wait_for(predicate, timeout_s=10, interval_s=0.2):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def run_dispatch_batch(env, *ticket_ids):
    """Invokes dispatch-batch the way the acceptance test does: `sh -c '... TICKET...'`
    so the immediate parent (`sh`) exits the instant dispatch-batch's own foreground
    process returns — before any per-ticket work has necessarily finished."""
    args = " ".join(str(t) for t in ticket_ids)
    return subprocess.run(["sh", "-c", f"{DISPATCH_BATCH_BIN} {args}"],
                           capture_output=True, text=True, env=env, timeout=15)


def batch_logs(tmp_path):
    run_dir = tmp_path / "runs"
    if not run_dir.exists():
        return []
    return sorted(run_dir.glob("batch-*.log"))


def test_returns_immediately_even_though_workers_keep_running(tmp_path):
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path))
    ids = [add_ticket(env, f"dispatch-batch survive {i}") for i in range(5)]
    for i in ids:
        set_recipe(tmp_path, i, "survive")

    start = time.time()
    p = run_dispatch_batch(env, *ids)
    elapsed = time.time() - start

    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert "dispatching 5 ticket(s) detached" in p.stdout
    # The launcher must return almost instantly — it must not have waited on any
    # per-ticket claim-check or on the ~2s survive-sleep below.
    assert elapsed < 2.0, f"dispatch-batch blocked its caller for {elapsed:.2f}s"

    marker_dir = tmp_path / "runs"

    def all_done():
        return all((marker_dir / f"{i}.done").exists() for i in ids)

    assert wait_for(all_done, timeout_s=10), "not all workers completed after parent exited"
    # Not asserting a literal PPID of 1: this test itself runs nested inside a sandboxed
    # tool call with its own PID namespace, so even a genuinely orphaned process reparents
    # to *that* namespace's local reaper rather than literal init. What's checkable and
    # what actually matters is that each worker outlived and was reparented away from its
    # immediate spawner (the stub process that backgrounded it and exited) — proof the
    # kernel orphaned it rather than it being a still-attached child that got lucky on
    # timing.
    for i in ids:
        ppid = (marker_dir / f"{i}.ppid").read_text().strip()
        spawner_pid = (marker_dir / f"{i}.spawner_pid").read_text().strip()
        assert ppid != spawner_pid, (
            f"wq#{i} worker still parented to its exited spawner ({spawner_pid}) "
            f"instead of being reparented — not actually detached")


def test_logs_unit_and_verifies_claim(tmp_path):
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path))
    tid = add_ticket(env, "dispatch-batch claim-immediate")
    set_recipe(tmp_path, tid, "claim_immediate")

    p = run_dispatch_batch(env, tid)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    assert f"wq#{tid} UNIT=stub-{tid}" in text
    assert f"wq#{tid} OK unit=stub-{tid} claimed_by=stub-agent-{tid}" in text
    assert "done rc=0" in text

    ticket_log = (tmp_path / "runs" / f"{tid}.log").read_text()
    assert f"UNIT=stub-{tid}" in ticket_log


def test_unclaimed_ticket_times_out_and_is_reported(tmp_path):
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path))
    tid = add_ticket(env, "dispatch-batch never-claims")
    set_recipe(tmp_path, tid, "never_claim")

    run_dispatch_batch(env, tid)

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    assert f"wq#{tid} NOT-CLAIMED unit=stub-{tid}" in text
    assert "done rc=1" in text


def test_one_spawn_failure_does_not_abort_the_rest_of_the_batch(tmp_path):
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path))
    bad = add_ticket(env, "dispatch-batch fails to spawn")
    good = add_ticket(env, "dispatch-batch claims fine")
    set_recipe(tmp_path, bad, "fail")
    set_recipe(tmp_path, good, "claim_immediate")

    run_dispatch_batch(env, bad, good)

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    assert f"wq#{bad}: spawn failed" in text
    assert f"wq#{good} OK unit=stub-{good}" in text
    assert "done rc=1" in text


# wq#2526: 'dispatch-batch --help' reached default_spawn(), which built a command line
# from router.py's own `dispatch-cmd --help` usage output and blindly eval'd it — 3
# occurrences in production (_runs/dispatch, 2026-09-24 23:33-23:35Z). Any non-numeric
# argv reaches the same eval, since default_spawn() has no idea an id is well-formed.
# These three tests cover the parent-side reject-before-detach path and the
# defense-in-depth check inside default_spawn itself.

def run_dispatch_batch_argv(env, *argv):
    """Like run_dispatch_batch, but passes argv verbatim (not necessarily numeric ids) and
    runs dispatch-batch directly rather than through `sh -c` — for exercising the
    parent-side validation that must reject before any detach/log dir is created."""
    return subprocess.run([DISPATCH_BATCH_BIN, *argv], capture_output=True, text=True,
                           env=env, timeout=15)


def test_help_flag_prints_usage_and_exits_0_without_detaching(tmp_path):
    env = make_env(tmp_path)
    for flag in ("-h", "--help"):
        p = run_dispatch_batch_argv(env, flag)
        assert p.returncode == 0, f"{flag}: {p.stdout}\n{p.stderr}"
        assert "usage: dispatch-batch" in p.stderr
        assert p.stdout == ""
    assert not (tmp_path / "runs").exists(), "--help must never create the run dir / detach"


def test_non_numeric_arg_rejected_before_detach(tmp_path):
    env = make_env(tmp_path)
    p = run_dispatch_batch_argv(env, "not-a-number")
    assert p.returncode == 64, f"{p.stdout}\n{p.stderr}"
    assert "refusing non-numeric ticket id" in p.stderr
    assert not (tmp_path / "runs").exists(), "an invalid id must be rejected before detaching"

    # one bad id anywhere in the batch rejects the whole invocation, before any ticket runs
    p = run_dispatch_batch_argv(env, "123", "--also-bad")
    assert p.returncode == 64, f"{p.stdout}\n{p.stderr}"
    assert not (tmp_path / "runs").exists()


def test_default_spawn_refuses_to_eval_non_lumen_run_output(tmp_path):
    # router.py returning rc=0 with usage/help-shaped stdout (argparse's own --help
    # handling is exactly this shape) must not reach eval just because rc was 0.
    fake_router = tmp_path / "fake_router.py"
    fake_router.write_text(
        "import sys\nprint('echo not-a-lumen-run-invocation')\nsys.exit(0)\n")
    env = make_env(tmp_path, DISPATCH_BATCH_ROUTER=str(fake_router))
    tid = add_ticket(env, "dispatch-batch bad router output")

    run_dispatch_batch(env, tid)

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    assert f"wq#{tid}: spawn failed" in text
    assert "done rc=1" in text
    ticket_log = (tmp_path / "runs" / f"{tid}.log").read_text()
    assert "refusing to eval non-lumen-run output" in ticket_log


# wq#2631/wq#2647: dispatch-batch spawned straight through lumen-run with no admission
# control, bypassing the same cloud-concurrency + CPU-PSI cap foreman_dispatcher.py's own
# pass enforces in-process. N orchestrators pasting batches in parallel could push load
# well past what the box can hold (2026-09-25 04:28Z: load 104 on 44 cores).
# DISPATCH_BATCH_ADMISSION_CMD is the test seam: an executable dispatch-batch invokes with
# no args that must exit 0 (admit) or nonzero (deny) and may print a reason.

def make_admission_stub(tmp_path, verdict, reason="test reason"):
    stub = tmp_path / "admission_stub.sh"
    rc = 0 if verdict == "ok" else 1
    stub.write_text(f'#!/usr/bin/env bash\necho "{reason}"\nexit {rc}\n')
    stub.chmod(0o755)
    return str(stub)


def claimed_by_of_helper(env, tid):
    p = subprocess.run([WQ_BIN, "show", str(tid)], capture_output=True, text=True, env=env)
    m = re.search(r"^claimed_by\s*:\s*(\S+)", p.stdout, re.MULTILINE)
    return m.group(1) if m else None


def test_admission_denied_queues_instead_of_spawning(tmp_path):
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path),
                   DISPATCH_BATCH_ADMISSION_CMD=make_admission_stub(
                       tmp_path, "deny", "cpu_pressure: cpu_some_avg60=42.00%>=30.0%"))
    ids = [add_ticket(env, f"dispatch-batch admission-deny {i}") for i in range(3)]
    for i in ids:
        set_recipe(tmp_path, i, "claim_immediate")

    p = run_dispatch_batch(env, *ids)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    for i in ids:
        assert f"wq#{i} QUEUED admission denied: cpu_pressure: cpu_some_avg60=42.00%>=30.0%" in text
        assert f"wq#{i} UNIT=" not in text, f"wq#{i} must not have been spawned while admission denied"
    # AC: batch spawns 0, queues N, and still exits 0 — a denied admission is backpressure,
    # not a batch failure.
    assert "done rc=0" in text

    # Queued tickets are left untouched (dispatch-batch never claimed them) so the ordinary
    # dispatcher timer can still pick them up once load clears.
    for i in ids:
        assert claimed_by_of_helper(env, i) in (None, "None")


def test_admission_ok_spawns_normally(tmp_path):
    # Regression guard: an admitted ticket must still go through the exact same
    # spawn/claim-verify path as before this ticket existed.
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path),
                   DISPATCH_BATCH_ADMISSION_CMD=make_admission_stub(tmp_path, "ok"))
    tid = add_ticket(env, "dispatch-batch admission-ok")
    set_recipe(tmp_path, tid, "claim_immediate")

    p = run_dispatch_batch(env, tid)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    assert f"wq#{tid} UNIT=stub-{tid}" in text
    assert f"wq#{tid} OK unit=stub-{tid} claimed_by=stub-agent-{tid}" in text
    assert "QUEUED" not in text
    assert "done rc=0" in text


def test_missing_admission_gate_fails_open(tmp_path):
    # A stripped-down checkout without dispatcher/admission_gate.py must not become a new
    # way for the fleet to wedge itself — absence of the gate script admits, it never denies.
    env = make_env(tmp_path, DISPATCH_BATCH_SPAWN_CMD=write_stub(tmp_path),
                   DISPATCH_BATCH_ADMISSION_GATE=str(tmp_path / "no-such-admission-gate.py"))
    tid = add_ticket(env, "dispatch-batch admission-gate-missing")
    set_recipe(tmp_path, tid, "claim_immediate")

    p = run_dispatch_batch(env, tid)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"

    def batch_done():
        logs = batch_logs(tmp_path)
        return logs and "done rc=" in logs[-1].read_text()

    assert wait_for(batch_done, timeout_s=10), "detached batch never finished"
    text = batch_logs(tmp_path)[-1].read_text()
    assert f"wq#{tid} OK unit=stub-{tid}" in text
    assert "done rc=0" in text
