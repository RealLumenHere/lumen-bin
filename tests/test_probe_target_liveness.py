#!/usr/bin/env python3
"""Test-to-failure for the wq#1092 stale-target guard (_stale_or_none /
_unit_load_state / _file_stale_probe_ticket) in sentinel_probes.py. Offline,
no real systemctl/wq calls: subprocess.run and the wq-ticket file path are
both monkeypatched. Same pattern as bin/tests/test_invisible_work_watch.py.

Reproduces the exact aq#899/wq#1064 shape (probe-a5-health's three retired
targets) and proves it now returns STALE instead of CRITICAL.

Run: python3 tests/test_probe_target_liveness.py
"""
import os, sys, importlib.util, subprocess, tempfile, json

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_MOD_PATH = os.path.join(HERE, "..", "..", "sentinel", "probes", "sentinel_probes.py")

_fails = []
def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        _fails.append(name)

_counter = [0]
def load_fresh(cap_path):
    """Import sentinel_probes fresh per scenario (module-level state.json path
    gets monkeypatched after import, and each scenario needs its own capture
    file) -- unique module name each time so re-import isn't skipped."""
    os.environ.setdefault("LITELLM_MASTER_KEY", "test-key-unused")
    os.environ["SENTINEL_WQ_FILE_CAPTURE"] = cap_path
    _counter[0] += 1
    spec = importlib.util.spec_from_file_location(f"sentinel_probes_{_counter[0]}", PROBE_MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    d = os.path.dirname(cap_path)
    mod.STATE = __import__("pathlib").Path(os.path.join(d, "state.json"))
    return mod

def systemctl_show_stub(load_state, active_state="inactive"):
    def fake_run(cmd, *a, **kw):
        class R: pass
        r = R(); r.returncode = 0
        if cmd[:2] == ["systemctl", "show"] and "LoadState" in cmd:
            r.stdout = f"LoadState={load_state}\n"
        elif cmd[:2] == ["systemctl", "show"]:
            r.stdout = f"ActiveState={active_state}\nSubState=dead\nNRestarts=0\n"
        elif cmd[:2] == ["systemctl", "is-active"]:
            r.stdout = f"{active_state}\n"
        else:
            r.stdout = ""
        return r
    return fake_run

def scenario_not_found_is_stale_not_critical():
    print("[not-found] a unit whose file was removed returns STALE, not CRITICAL")
    d = tempfile.mkdtemp(prefix="ptl_")
    mod = load_fresh(os.path.join(d, "cap.jsonl"))
    mod.subprocess.run = systemctl_show_stub("not-found")
    ok, detail, critical = mod._stale_or_none("lumen-planner", "probe-a5-health")
    check("ok=True", ok is True, detail)
    check("critical=False", critical is False, detail)
    check("detail mentions STALE", "STALE" in detail, detail)

def scenario_masked_is_stale_not_critical():
    print("[masked] a deliberately masked unit also returns STALE")
    d = tempfile.mkdtemp(prefix="ptl_")
    mod = load_fresh(os.path.join(d, "cap.jsonl"))
    mod.subprocess.run = systemctl_show_stub("masked")
    ok, detail, critical = mod._stale_or_none("lumen-agent", "probe-x")
    check("ok=True", ok is True, detail)
    check("critical=False", critical is False, detail)

def scenario_loaded_unit_passes_through():
    print("[loaded] a real, currently-loaded unit is NOT intercepted by the guard")
    d = tempfile.mkdtemp(prefix="ptl_")
    mod = load_fresh(os.path.join(d, "cap.jsonl"))
    mod.subprocess.run = systemctl_show_stub("loaded", active_state="active")
    result = mod._stale_or_none("lumen-messenger", "probe-x")
    check("guard returns None (caller proceeds)", result is None, result)

def scenario_ticket_filed_once_deduped():
    print("[dedup] repeated calls for the same (probe, unit) file exactly one ticket")
    d = tempfile.mkdtemp(prefix="ptl_")
    cap = os.path.join(d, "cap.jsonl")
    mod = load_fresh(cap)
    mod.subprocess.run = systemctl_show_stub("not-found")
    mod._stale_or_none("lumen-planner", "probe-a5-health")
    mod._stale_or_none("lumen-planner", "probe-a5-health")
    mod._stale_or_none("lumen-planner", "probe-a5-health")
    lines = open(cap).read().splitlines() if os.path.exists(cap) else []
    check("exactly 1 ticket filed across 3 calls", len(lines) == 1, lines)
    if lines:
        row = json.loads(lines[0])
        check("ticket names the right probe+unit", row["probe"] == "probe-a5-health" and row["unit"] == "lumen-planner", row)

def scenario_different_units_file_separate_tickets():
    print("[dedup-scope] two different units under the same probe each get their own ticket")
    d = tempfile.mkdtemp(prefix="ptl_")
    cap = os.path.join(d, "cap.jsonl")
    mod = load_fresh(cap)
    mod.subprocess.run = systemctl_show_stub("not-found")
    mod._stale_or_none("lumen-planner", "probe-a5-health")
    mod._stale_or_none("lumen-generator", "probe-a5-health")
    lines = open(cap).read().splitlines() if os.path.exists(cap) else []
    check("2 distinct tickets for 2 distinct units", len(lines) == 2, lines)

def scenario_a5_health_reproduces_aq899_now_inert():
    print("[a5-health] the literal aq#899 shape: all 3 A5_SERVICES not-found -> STALE not CRITICAL")
    d = tempfile.mkdtemp(prefix="ptl_")
    mod = load_fresh(os.path.join(d, "cap.jsonl"))
    mod.subprocess.run = systemctl_show_stub("not-found")
    ok, detail, critical = mod.probe_a5_health()
    check("ok=True (was False x47 in aq#899)", ok is True, detail)
    check("critical=False (was True -> ntfy_urgent every cycle)", critical is False, detail)
    check("detail names the stale unit", "lumen-planner" in detail, detail)

def scenario_state_drift_skips_stale_unit_without_flagging_problem():
    print("[state-drift] a retired unit in probe_state_drift's must-be-active list is skipped, not a problem")
    d = tempfile.mkdtemp(prefix="ptl_")
    mod = load_fresh(os.path.join(d, "cap.jsonl"))
    orig = subprocess.run
    def fake_run(cmd, *a, **kw):
        class R: pass
        r = R(); r.returncode = 0
        if cmd[:2] == ["systemctl", "show"] and "LoadState" in cmd and cmd[2] == "lumen-messenger":
            r.stdout = "LoadState=not-found\n"
        elif cmd[:2] == ["systemctl", "is-active"]:
            r.stdout = "active\n"
        elif cmd[:1] == ["tailscale"]:
            r.stdout = json.dumps({"Peer": {}, "Self": {"Online": True, "TailscaleIPs": []}})
        else:
            r.stdout = ""
        return r
    mod.subprocess.run = fake_run
    mod._fleet_map_expects_online = lambda name: False  # isis/hathor out of scope for this scenario
    ok, detail, critical = mod.probe_state_drift()
    check("ok=True (stale unit doesn't count as drift)", ok is True, detail)
    check("lumen-messenger not named as a problem", "lumen-messenger" not in detail, detail)

def main():
    for fn in (scenario_not_found_is_stale_not_critical,
               scenario_masked_is_stale_not_critical,
               scenario_loaded_unit_passes_through,
               scenario_ticket_filed_once_deduped,
               scenario_different_units_file_separate_tickets,
               scenario_a5_health_reproduces_aq899_now_inert,
               scenario_state_drift_skips_stale_unit_without_flagging_problem):
        fn()
    print()
    if _fails:
        print(f"FAILED {len(_fails)} check(s): {_fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0

if __name__ == "__main__":
    sys.exit(main())
