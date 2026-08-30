#!/usr/bin/env python3
"""probe_wq625 — red/green for the T2a worker harness (BUILD_PLAN.md §T2a,
design/T2A_WORKER_HARNESS_DESIGN.md). Zero Anthropic quota: runs the harness's own
pytest suite (guard corpus, classifier, settings-lint, fworker E2E against a fake
claude shim) plus a handful of artifact/wiring checks the tests don't directly assert
(file layout, gh-lane-worker retrofit, interim file removed).

The ticket's two NAMED adversarial criteria get their own top-level sections (C, D)
so a reader can see them pass without cross-referencing the pytest output.

Usage: probe_wq625.py [FOREMAN_ROOT]   (default /home/lumen/foreman)
"""
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/home/lumen/foreman"
HARNESS = os.path.join(ROOT, "harness")
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def sh(cmd, cwd=None, timeout=120, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                        env=env, shell=isinstance(cmd, str))
    return p.returncode, (p.stdout or "") + (p.stderr or "")


print(f"probe_wq625 — under test: {ROOT}\n")

print("A. file layout matches the design (design/T2A_WORKER_HARNESS_DESIGN.md §2)")
for cid, path in [
    ("A1", os.path.join(HARNESS, "fworker.py")),
    ("A2", os.path.join(HARNESS, "worker-settings.json")),
    ("A3", os.path.join(HARNESS, "guard.sh")),
    ("A4", os.path.join(HARNESS, "classifier.py")),
    ("A5", os.path.join(HARNESS, "prompts", "worker.md")),
]:
    check(cid, os.path.isfile(path), path)
check("A6", os.access(os.path.join(HARNESS, "guard.sh"), os.X_OK), "guard.sh executable")
check("A7", not os.path.exists(os.path.join(ROOT, "worker-settings-interim.json")),
      "worker-settings-interim.json removed (superseded)")

print("\nB. harness pytest suite (guard corpus, classifier, settings-lint, fworker E2E "
      "against a fake claude shim — zero quota)")
rc, out = sh([sys.executable, "-m", "pytest", "tests/", "-q"], cwd=HARNESS, timeout=180)
last_lines = "\n".join(out.strip().splitlines()[-15:])
check("B1", rc == 0, f"pytest exit {rc}\n{last_lines}")

print("\nC. *** NAMED CRITERION: denied verb blocked at the hook *** "
      "(BUILD_PLAN.md §T2a verify)")
guard = os.path.join(HARNESS, "guard.sh")
payload = json.dumps({"tool_name": "Bash",
                       "tool_input": {"command": "docker compose --remove-orphans up -d"},
                       "session_id": "probe625", "cwd": "/tmp"})
p = subprocess.run([guard], input=payload, capture_output=True, text=True)
check("C1", p.returncode == 2, f"docker compose --remove-orphans -> exit {p.returncode} (want 2)")
payload_evasion = json.dumps({"tool_name": "Bash",
                               "tool_input": {"command": 'bash -c "docker compose --remove-orphans"'},
                               "session_id": "probe625", "cwd": "/tmp"})
p2 = subprocess.run([guard], input=payload_evasion, capture_output=True, text=True)
check("C2", p2.returncode == 2, f"bash -c evasion -> exit {p2.returncode} (want 2)")
payload_allow = json.dumps({"tool_name": "Bash", "tool_input": {"command": "docker ps -a"},
                             "session_id": "probe625", "cwd": "/tmp"})
p3 = subprocess.run([guard], input=payload_allow, capture_output=True, text=True)
check("C3", p3.returncode == 0, f"docker ps -a (allowed neighbor) -> exit {p3.returncode} (want 0)")

print("\nD. *** NAMED CRITERION: LIMIT != CRASH on a synthetic rejected frame *** "
      "(CLAUDE_CLI_INTERNALS.md §E)")
sys.path.insert(0, HARNESS)
from classifier import RunState, classify, LIMIT, CRASH  # noqa: E402
st = RunState()
for frame in [
    {"type": "system", "subtype": "init", "session_id": "probe625"},
    {"type": "rate_limit_event", "status": "rejected", "rateLimitType": "five_hour",
     "resetsAt": 9999999999, "utilization": 1.0},
    {"type": "result", "subtype": "error_during_execution", "is_error": True,
     "errors": ["usage limit reached"]},
]:
    st.observe(frame)
verdict = classify(st)
check("D1", verdict == LIMIT, f"classify(synthetic rejected frame) = {verdict} (want LIMIT)")
check("D2", verdict != CRASH, f"classify(synthetic rejected frame) = {verdict} (must not be CRASH)")

print("\nE. gh-lane-worker retrofit off bypassPermissions (design §11)")
glw = "/home/lumen/bin/gh-lane-worker"
src = open(glw).read() if os.path.exists(glw) else ""
check("E1", bool(src), f"{glw} exists")
check("E2", "bypassPermissions" not in src and "dangerously-skip-permissions" not in src,
      "no bypass mode left in gh-lane-worker")
check("E3", "harness/worker-settings.json" in src and "acceptEdits" in src,
      "gh-lane-worker now uses the T2a settings pack in acceptEdits mode")

print("\nF. worker-settings.json is well-formed and carries the guard hook")
try:
    with open(os.path.join(HARNESS, "worker-settings.json")) as f:
        settings = json.load(f)
    check("F1", settings["permissions"]["defaultMode"] == "acceptEdits", "defaultMode acceptEdits")
    pre = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    check("F2", pre == guard, f"PreToolUse hook points at {guard}")
except Exception as e:
    check("F1", False, f"settings.json malformed: {e}")
    check("F2", False, "skipped")

print()
print(f"probe_wq625: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
