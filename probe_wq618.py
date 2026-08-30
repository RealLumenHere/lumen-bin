#!/usr/bin/env python3
"""probe_wq618 — red/green for T4 local rung (BUILD_PLAN.md §T4). Zero Anthropic
quota (the scoped LiteLLM key is exercised live against the gateway's /v1/models,
which costs nothing; no `claude -p` call is made against Anthropic here). Runs the
governor + harness pytest suites, then the ticket's named criteria:

  A. scoped 'foreman-local-rung' LiteLLM key exists (0600, not the master key),
     restricted to exactly the two trusted routes.
  B. governor is_local_model() exemption: local dispatch allowed despite exhausted
     floors/stale state/live rejected blocks, still halted by STOP.
  C. fworker wires the gateway env for local dispatch only, never leaks it to cloud,
     restricts local dispatch to mechanical-edit/probe-build, lowers local max-turns.
  D. end-to-end: a mechanical-edit ticket on deepseek-v4-flash runs via the fake_claude
     shim, ships, and gets a mandatory sonnet-5@high verify-pass ticket filed against
     it (models.yaml verify_pass.local_author_always).
  E. a local-pool dispatch outside mechanical-edit/probe-build DEFERs before claim.

Usage: probe_wq618.py [FOREMAN_ROOT]   (default /home/lumen/foreman)
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/home/lumen/foreman"
HARNESS = os.path.join(ROOT, "harness")
GOVERNOR = os.path.join(ROOT, "governor")
WQ = os.environ.get("FOREMAN_WQ_BIN", "/home/lumen/bin/wq")
KEY_FILE = "/home/lumen/.secrets/foreman-litellm.token"
LITELLM_URL = "http://localhost:4000"
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def sh(cmd, cwd=None, timeout=600, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


print("== 0. suites")
p = sh([sys.executable, "-m", "pytest", ".", "-q"], cwd=GOVERNOR)
check("0.1 governor pytest", p.returncode == 0, p.stdout.strip().splitlines()[-1] if p.stdout else p.stderr[-300:])
p = sh([sys.executable, "-m", "pytest", "tests", "-q"], cwd=HARNESS)
check("0.2 harness pytest (fworker + local rung)", p.returncode == 0, p.stdout.strip().splitlines()[-1] if p.stdout else p.stderr[-300:])

print("== A. scoped LiteLLM key")
try:
    st = os.stat(KEY_FILE)
    check("A.1 key file 0600", oct(st.st_mode)[-3:] == "600", oct(st.st_mode))
    with open(KEY_FILE) as f:
        key = f.read().strip()
    check("A.2 key is not the litellm master key",
          key and key != "sk-lumen-litellm-master" and not key.startswith("sk-lumen-litellm"))
    curl = sh(["curl", "-s", f"{LITELLM_URL}/v1/models", "-H", f"Authorization: Bearer {key}"])
    models = sorted(m["id"] for m in (json.loads(curl.stdout or "{}").get("data") or []))
    check("A.3 key scoped to exactly the two trusted routes",
          models == ["deepseek-v4-flash", "qwen36-local"], str(models))
except OSError as e:
    check("A.1 key file readable", False, str(e))
    check("A.2 key is not the litellm master key", False, "skipped")
    check("A.3 key scoped to exactly the two trusted routes", False, "skipped")

print("== B. governor local-rung exemption")
sys.path.insert(0, GOVERNOR)
import governor_core as core  # noqa: E402
import datetime as dt
NOW = dt.datetime(2026, 8, 16, 18, 0, 0, tzinfo=dt.timezone.utc)
FLOORS = dict(core.DEFAULT_FLOORS)
exhausted = {"session": {"percent": 100, "resets_at": "x", "severity": "critical"},
             "weekly_all": {"percent": 100, "resets_at": "x", "severity": "critical"}}
d = core.evaluate_dispatch("deepseek-v4-flash", limits=exhausted, floors=FLOORS, now_utc=NOW,
                            stop_active=False, stale=False)
check("B.1 local allowed despite exhausted cloud floors", d["allow"] is True)
d = core.evaluate_dispatch("deepseek-v4-flash", limits=None, floors=FLOORS, now_utc=NOW,
                            stop_active=False, stale=True)
check("B.2 local allowed despite stale/no-poll state", d["allow"] is True)
d = core.evaluate_dispatch("deepseek-v4-flash", limits=exhausted, floors=FLOORS, now_utc=NOW,
                            stop_active=True, stale=False)
check("B.3 STOP still halts local dispatch", d["allow"] is False and "STOP" in d["reasons"][0])
check("B.4 nephthys is not a trusted local route", core.is_local_model("nephthys-qwen3-14b") is False)

print("== C. fworker gateway wiring + archetype restriction")
sys.path.insert(0, HARNESS)
import fworker  # noqa: E402
check("C.1 is_local_model matches the two trusted routes only",
      fworker.is_local_model("deepseek-v4-flash") and fworker.is_local_model("qwen36-local")
      and not fworker.is_local_model("nephthys-qwen3-14b") and not fworker.is_local_model("claude-sonnet-5"))
allow, _ = fworker.local_rung_gate("deepseek-v4-flash", "mechanical-edit")
check("C.2 local allowed for mechanical-edit", allow is True)
allow, _ = fworker.local_rung_gate("deepseek-v4-flash", "standard-engineering")
check("C.3 local denied for standard-engineering", allow is False)
env_local = fworker.build_env("wid", "deepseek-v4-flash", None, "/tmp/wt")
check("C.4 local dispatch gets ANTHROPIC_BASE_URL + gateway discovery",
      env_local.get("ANTHROPIC_BASE_URL") == LITELLM_URL
      and env_local.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY") == "1"
      and "ANTHROPIC_API_KEY" not in env_local)
env_cloud = fworker.build_env("wid", "claude-sonnet-5", "high", "/tmp/wt")
check("C.5 cloud dispatch never carries gateway vars",
      "ANTHROPIC_BASE_URL" not in env_cloud and "ANTHROPIC_AUTH_TOKEN" not in env_cloud
      and "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in env_cloud)
check("C.6 local --max-turns lower than cloud for both trusted archetypes",
      fworker.LOCAL_ARCHETYPE_MAX_TURNS["mechanical-edit"] < fworker.ARCHETYPE_MAX_TURNS["mechanical-edit"]
      and fworker.LOCAL_ARCHETYPE_MAX_TURNS["probe-build"] < fworker.ARCHETYPE_MAX_TURNS["probe-build"])

print("== D. end-to-end: local mechanical-edit ships, mandatory sonnet verify-pass filed")
tmp = tempfile.mkdtemp(prefix="probe618-")
repo = os.path.join(tmp, "repo")
os.makedirs(repo)
sh(["git", "init", "-q"], cwd=repo)
sh(["git", "config", "user.email", "probe@lumen"], cwd=repo)
sh(["git", "config", "user.name", "probe"], cwd=repo)
with open(os.path.join(repo, "README.md"), "w") as f:
    f.write("scratch\n")
sh(["git", "add", "README.md"], cwd=repo)
sh(["git", "commit", "-q", "-m", "init"], cwd=repo)

key_file = os.path.join(tmp, "litellm.token")
with open(key_file, "w") as f:
    f.write("sk-probe-618-key")
env = dict(os.environ, WQ_DB=os.path.join(tmp, "q.db"), WQ_AGENT="probe618", FOREMAN_WQ_BIN=WQ,
           FOREMAN_RUNS_ROOT=os.path.join(tmp, "runs"), FOREMAN_EVENTS_DIR=os.path.join(tmp, "events"),
           FOREMAN_WORKTREES_ROOT=os.path.join(tmp, "worktrees"),
           FOREMAN_STOP_FILE=os.path.join(tmp, "STOP_nonexistent"),
           FOREMAN_GOVERNOR_CLI=os.path.join(HARNESS, "tests", "fake_governor.py"),
           FOREMAN_LITELLM_KEY_FILE=key_file,
           FOREMAN_LUMEN_PR_BIN="/bin/false", FOREMAN_NTFY_BIN="/bin/true",
           FAKE_CLAUDE_SCENARIO="success", FAKE_CLAUDE_GUARD_SH=os.path.join(HARNESS, "guard.sh"),
           FAKE_CLAUDE_WQ_BIN=WQ)
p = sh([WQ, "add", "--title", "probe618 local mechanical-edit"], env=env)
tid = int(p.stdout.split()[2])
env["FAKE_CLAUDE_TICKET"] = str(tid)
p = sh([sys.executable, os.path.join(HARNESS, "fworker.py"), "run", "--ticket", str(tid), "--repo", repo,
        "--model", "deepseek-v4-flash", "--archetype", "mechanical-edit",
        "--claude-bin", os.path.join(HARNESS, "tests", "fake_claude.py"),
        "--worker-id", f"probe618-wq{tid}"], env=env, timeout=60)
show = sh([WQ, "show", str(tid)], env=env).stdout
check("D.1 local mechanical-edit ships (wq done)", "status       : done" in show, p.stdout[-300:] + p.stderr[-300:])
listing = sh([WQ, "list"], env=env).stdout
vline = next((l for l in listing.splitlines() if f"[verify] wq#{tid}" in l), None)
check("D.2 verify-pass ticket filed for the shipped local ticket", bool(vline), listing)
if vline:
    vid = int(re.match(r"\s*\[\s*(\d+)\]", vline).group(1))
    vshow = sh([WQ, "show", str(vid)], env=env).stdout
    check("D.3 verifier is sonnet-5@high, archetype verify-pass, deps on shipped",
          "model        : claude-sonnet-5" in vshow and "effort       : high" in vshow
          and "archetype    : verify-pass" in vshow and f"deps         : {tid}" in vshow)
else:
    check("D.3 verifier is sonnet-5@high, archetype verify-pass, deps on shipped", False, "no verify ticket")

print("== E. local dispatch outside the trusted archetypes never claims")
p = sh([WQ, "add", "--title", "probe618 local rca (should be rejected)"], env=env)
tid2 = int(p.stdout.split()[2])
env["FAKE_CLAUDE_TICKET"] = str(tid2)
p = sh([sys.executable, os.path.join(HARNESS, "fworker.py"), "run", "--ticket", str(tid2), "--repo", repo,
        "--model", "deepseek-v4-flash", "--archetype", "rca-investigation",
        "--claude-bin", os.path.join(HARNESS, "tests", "fake_claude.py")], env=env, timeout=30)
show2 = sh([WQ, "show", str(tid2)], env=env).stdout
check("E.1 DEFERRED, never claimed", p.returncode == 11 and "status       : open" in show2, show2)

print(f"\nPASS {len(PASS)}  FAIL {len(FAIL)}" + (f"  -> {FAIL}" if FAIL else ""))
print("PROBE_RESULT: " + ("GREEN" if not FAIL else "RED"))
sys.exit(0 if not FAIL else 1)
