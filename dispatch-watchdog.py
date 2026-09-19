#!/usr/bin/env python3
# dispatch-watchdog — detect a STALLED foreman dispatcher and auto-recover, loudly.
# Built 2026-09-04 (orch) as the durable end to a week of silent nightly total-failures.
# Distinguishes a real stall (runnable work exists but nothing spawns) from a
# legitimately-empty queue (only structural skips). On a real stall it runs a
# recovery ladder and PAGES via ntfy with exactly what it did. Fail-safe + rate-limited.
import subprocess, json, re, time, os, sys

STATE = "/home/lumen/.cache/dispatch-watchdog.json"
COOLDOWN_S = 900          # don't run recovery more than once per 15 min
STALL_PASSES = 2          # spawned=0 across at least this many recent passes
DISP_SVC = "lumen-foreman-dispatch.service"
DISP_TIMER = "lumen-foreman-dispatch.timer"
PREFLIGHT = "/home/lumen/foreman/harness/harness-preflight"

def sh(cmd, t=120):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 99, "", str(e)

def ntfy(title, msg, prio="high", topic="lumen-alerts"):
    sh(f'/home/lumen/bin/ntfy-send -t {topic} -p {prio} -T {json.dumps(title)} {json.dumps(msg)}', t=30)

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}
def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"))

def recent_passes(n=4):
    rc, out, _ = sh(f'journalctl -u {DISP_SVC} --no-pager -n 400 2>/dev/null | grep pass_end | tail -{n}')
    ps = []
    for line in out.splitlines():
        m = re.search(r'\{.*\}', line)
        if m:
            try: ps.append(json.loads(m.group(0)))
            except Exception: pass
    return ps

def workers_running():
    rc, out, _ = sh("systemctl list-units 'lumen-job-fworker-*' --state=running --no-pager 2>/dev/null | grep -c fworker")
    try: return int(out.strip() or "0")
    except Exception: return 0

def recent_signal(pattern, mins=15):
    rc, out, _ = sh(f'journalctl --since "-{mins} min" 2>/dev/null | grep -cE {json.dumps(pattern)}')
    try: return int(out.strip() or "0")
    except Exception: return 0

def launch_failure_loopers(mins=20):
    rc, out, _ = sh(f'journalctl --since "-{mins} min" 2>/dev/null | grep -oE "CITED_PATH_UNREACHABLE\\] wq#[0-9]+" | grep -oE "[0-9]+" | sort | uniq -c | sort -rn')
    loopers = []
    for line in out.splitlines():
        m = re.match(r'\s*(\d+)\s+(\d+)', line)
        if m and int(m.group(1)) >= 1:  # CITED_PATH is deterministic, block on first fail
            loopers.append(m.group(2))
    return loopers


STRUCTURAL_SKIPS = ("lane_unmapped","deps_unmet","runaway_stuck","frank_gated",
                    "route_unassigned","repo_mismatch","node_unknown","capability_denied_by_policy")
def dispatchable():
    rc,out,_ = sh(f'journalctl -u {DISP_SVC} --no-pager -n 80 2>/dev/null | grep -oE \'"reason": "[a-z_]+"\' | sort | uniq -c')
    structural=0
    for line in out.splitlines():
        parts=line.split()
        if not parts or not parts[0].isdigit(): continue
        reason = line.split('"')[-2] if line.count('"')>=2 else ""
        if reason in STRUCTURAL_SKIPS: structural += int(parts[0])
    ps=recent_passes(1); cand = ps[-1].get("candidates",0) if ps else 0
    return max(0, cand - structural)

def assess():
    """Return (stalled: bool, reason: str)."""
    ps = recent_passes(STALL_PASSES + 1)
    if not ps:
        # no passes at all recently -> dispatch not running
        rc, active, _ = sh(f"systemctl is-active {DISP_TIMER}")
        return (active != "active", "timer not active / no recent passes")
    if workers_running() > 0:
        return (False, "workers running")
    zero = [p for p in ps[-STALL_PASSES:] if p.get("spawned", 0) == 0]
    if len(zero) < STALL_PASSES:
        return (False, "recent pass spawned > 0")
    # spawned=0 across recent passes AND no workers. Real stall ONLY if there is
    # runnable-but-failing signal; pure lane_unmapped/deps = empty queue, not a stall.
    MIN_LF_FOR_STALL = 8
    lf = recent_signal("LAUNCH_FAILURE|CITED_PATH_UNREACHABLE", 15)
    ns = recent_signal("no_fresh_stamp|reprove_failed|reprove.*fail", 15)
    rc,dstat,_ = sh("systemctl show %s -p ExecMainStatus --value"%DISP_SVC); crash = (dstat.strip() not in ("0",""))
    if lf >= MIN_LF_FOR_STALL and dispatchable() > 0: return (True, f"LAUNCH_FAILURE loop ({lf} in 15m)")
    if ns and dispatchable() > 0: return (True, "stale stamp blocking runnable work")
    if crash: return (True, "dispatch pass crashing")
    return (False, "empty runnable queue (nothing sandboxed workers can run)")

def _cause():
    """wq#1694: name the cause on the page. Fail-open, never raises."""
    try:
        import importlib.util
        p = "/home/lumen/sentinel/probes/credential_expiry.py"
        spec = importlib.util.spec_from_file_location("_wq1694_ce", p)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        return m, m.cause_line()
    except Exception:
        return None, ""

def recover(reason):
    steps = []
    # 1) clear LAUNCH_FAILURE loopers (they burn all slots)
    loopers = launch_failure_loopers()
    for t in loopers[:10]:
        rc,_,_ = sh(f'cd /home/lumen && WQ_AGENT=dispatch-watchdog wq block {t} "auto-blocked by dispatch-watchdog: CITED_PATH_UNREACHABLE loop burning slots" 2>/dev/null')
    if loopers: steps.append(f"blocked {len(loopers[:10])} launch-fail loopers: {','.join(loopers[:10])}")
    # 2) re-stamp all repos (fix stale/failed stamps) — needs claude on PATH
    env = "PATH=/home/lumen/.npm-global/bin:/home/lumen/bin:/usr/local/bin:/usr/bin:/bin HOME=/home/lumen"
    rc, out, _ = sh(f'env {env} {PREFLIGHT} --all 2>&1 | grep -oE "\\"ok\\": (true|false)" | sort | uniq -c', t=240)
    steps.append("re-stamped repos: " + (out.replace(chr(10)," ") or "done"))
    # 3) restart dispatch + kick a pass
    sh(f"sudo -n systemctl restart {DISP_TIMER}")
    sh(f"sudo -n systemctl start {DISP_SVC}", t=60)
    steps.append("restarted dispatch timer + kicked a pass")
    time.sleep(20)
    # 4) verify
    ps = recent_passes(1)
    spawned = ps[-1].get("spawned", 0) if ps else 0
    w = workers_running()
    ok = spawned > 0 or w > 0
    return ok, steps, spawned, w

def main():
    st = load_state()
    stalled, reason = assess()
    if not stalled:
        if st.get("stalled"):  # recovered since last time
            st["stalled"] = False; save_state(st)
        return
    now = time.time()
    if now - st.get("last_recover", 0) < COOLDOWN_S and st.get("stalled"):
        return  # already acting, within cooldown
    was_stalled = st.get("stalled", False)
    last_page = st.get("last_page", 0)
    # RECOVER
    ok, steps, spawned, w = recover(reason)
    st.update({"stalled": not ok, "last_recover": now, "last_reason": reason})
    # page only on a NEW stall (transition) or once per 2h — never flood
    should_page = (not was_stalled) or (now - last_page > 7200)
    if should_page: st["last_page"] = now
    save_state(st)
    if not should_page:
        return
    ce, cause = _cause()
    if cause and not ok and ce is not None and ce.attach_symptom("dispatch-watchdog", f"{reason}; recovery failed"):
        return  # wq#1694 item 4: the credential alarm already owns this incident
    body = (cause + "\n" if cause else "") + f"DISPATCH STALLED: {reason}\nRecovery ladder:\n- " + "\n- ".join(steps) + \
           f"\nResult: {'RECOVERED (spawned=%d, workers=%d)'%(spawned,w) if ok else 'RECOVERY FAILED — needs a human'}"
    if ok:
        ntfy("[DISPATCH] auto-recovered", body, prio="default", topic="lumen-fleet")
    else:
        ntfy("[DISPATCH] STALLED — recovery FAILED, needs you", body, prio="urgent", topic="lumen-alerts")

if __name__ == "__main__":
    main()
