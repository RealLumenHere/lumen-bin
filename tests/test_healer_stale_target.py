#!/usr/bin/env python3
"""Test-to-failure for the wq#1112 stale-target guard
(_unit_load_state / _file_stale_unit_ticket, wired into check_all()'s unit
loop) in lumen_self_healer.py. Offline, no real systemctl/wq/sudo calls:
subprocess.run and the wq-ticket-filing call are both monkeypatched. Same
pattern as bin/tests/test_probe_target_liveness.py (sentinel's wq#1092
guard) -- this is the healer-side twin for the same failure class:
lumen-vip-api/lumen-vip-watcher/lumen-prompt-engine were left in
healer_watch_table.yaml after the 2026-08-28 research-mill retirement, so
the healer would burn its 3-attempt restart ladder against units that
structurally cannot come back.

Run: python3 tests/test_healer_stale_target.py
"""
import os, sys, importlib.util, json, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HEALER_MOD_PATH = os.path.join(HERE, "..", "..", "agent", "lumen_self_healer.py")

_fails = []
def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        _fails.append(name)

_counter = [0]
def load_fresh():
    """Import lumen_self_healer.py fresh per scenario -- unique module name
    each time so re-import isn't skipped and module-level dedup state
    (_stale_alerted) doesn't leak between scenarios."""
    _counter[0] += 1
    spec = importlib.util.spec_from_file_location(f"lumen_self_healer_{_counter[0]}", HEALER_MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

def systemctl_show_stub(load_state):
    def fake_run(cmd, *a, **kw):
        class R: pass
        r = R(); r.returncode = 0; r.stderr = ""
        if isinstance(cmd, list) and cmd[:2] == ["systemctl", "show"]:
            r.stdout = f"LoadState={load_state}\n"
        else:
            r.stdout = ""
        return r
    return fake_run

def capture_wq_add(sink):
    def fake_run(cmd, *a, **kw):
        class R: pass
        r = R(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == "add":
            title = cmd[cmd.index("--title") + 1] if "--title" in cmd else None
            sink.append(title)
        return r
    return fake_run

def scenario_not_found_returns_not_found():
    print("[not-found] a unit whose file was removed classifies as not-found")
    mod = load_fresh()
    mod.subprocess.run = systemctl_show_stub("not-found")
    state = mod._unit_load_state("lumen-vip-api")
    check("LoadState == not-found", state == "not-found", state)

def scenario_masked_returns_masked():
    print("[masked] a deliberately masked unit classifies as masked")
    mod = load_fresh()
    mod.subprocess.run = systemctl_show_stub("masked")
    state = mod._unit_load_state("lumen-agent")
    check("LoadState == masked", state == "masked", state)

def scenario_loaded_returns_loaded():
    print("[loaded] a real, currently-loaded unit classifies as loaded")
    mod = load_fresh()
    mod.subprocess.run = systemctl_show_stub("loaded")
    state = mod._unit_load_state("lumen-messenger")
    check("LoadState == loaded", state == "loaded", state)

def scenario_ticket_filed_once_deduped():
    print("[dedup] repeated calls for the same retired unit file exactly one ticket")
    mod = load_fresh()
    sink = []
    mod.subprocess.run = capture_wq_add(sink)
    mod._file_stale_unit_ticket("lumen-vip-api", "not-found")
    mod._file_stale_unit_ticket("lumen-vip-api", "not-found")
    mod._file_stale_unit_ticket("lumen-vip-api", "not-found")
    check("exactly 1 ticket filed across 3 calls", len(sink) == 1, sink)
    if sink:
        check("ticket names the unit", "lumen-vip-api" in sink[0], sink[0])

def scenario_different_units_file_separate_tickets():
    print("[dedup-scope] two different retired units each get their own ticket")
    mod = load_fresh()
    sink = []
    mod.subprocess.run = capture_wq_add(sink)
    mod._file_stale_unit_ticket("lumen-vip-api", "not-found")
    mod._file_stale_unit_ticket("lumen-vip-watcher", "not-found")
    check("2 distinct tickets for 2 distinct units", len(sink) == 2, sink)

def scenario_check_all_skips_retired_unit_never_restarts():
    print("[check_all] a retired unit in the watch table is skipped -- ticket filed, restart NEVER attempted")
    mod = load_fresh()
    restart_calls = []
    mod.restart_systemd = lambda svc: restart_calls.append(svc) or True
    mod.restart_docker = lambda name: restart_calls.append(name) or True
    mod.handle_failure = lambda name, spec: restart_calls.append(("handle_failure", name))
    mod.load_table = lambda: {
        "units": {"lumen-vip-api": {"kind": "systemd", "policy": "restart", "owner": "product"}},
        "ports": {}, "stall": {}, "ram": {"crit_pct": 90},
    }
    mod.holddown_lib.shielded_units = lambda: set()
    mod.holddown_lib.flagged_units = lambda: set()
    mod.check_stall = lambda rules: None
    mod.check_ram = lambda cfg: None
    mod.check_foreman_workers = lambda: None
    mod.check_holddowns = lambda held: None
    ticket_sink = []
    def fake_run(cmd, *a, **kw):
        class R: pass
        r = R(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        if isinstance(cmd, list) and cmd[:2] == ["systemctl", "show"]:
            r.stdout = "LoadState=not-found\n"
        elif isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == "add":
            title = cmd[cmd.index("--title") + 1] if "--title" in cmd else None
            ticket_sink.append(title)
        return r
    mod.subprocess.run = fake_run
    mod.check_all()
    check("restart/handle_failure never called for the retired unit", restart_calls == [], restart_calls)
    check("exactly 1 stale-target ticket filed", len(ticket_sink) == 1, ticket_sink)

def main():
    for fn in (scenario_not_found_returns_not_found,
               scenario_masked_returns_masked,
               scenario_loaded_returns_loaded,
               scenario_ticket_filed_once_deduped,
               scenario_different_units_file_separate_tickets,
               scenario_check_all_skips_retired_unit_never_restarts):
        fn()
    print()
    if _fails:
        print(f"FAILED {len(_fails)} check(s): {_fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0

if __name__ == "__main__":
    sys.exit(main())
