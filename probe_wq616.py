#!/usr/bin/env python3
"""probe_wq616 — red/green for T2b concurrency hardening (BUILD_PLAN.md §T2b). Zero
Anthropic quota. Runs the harness pytest suite, then re-checks the ticket's own named
adversarial criteria directly against the live concurrency.py/spawner.py/fworker.py
(not just via pytest, so this also proves the installed harness at ROOT behaves, same
pattern as probe_wq617.py/probe_wq625.py):

  A. cloud workers get CLAUDE_CODE_OAUTH_TOKEN as a static bearer (present when the
     token file exists, absent+WARN when it doesn't; ANTHROPIC_* scrubbed either way)
  B. concurrency cap: never spawns past --ceiling; a slot freeing mid-wait unblocks it
  C. governor deny skips a spawn without ever calling lumen-run start
  D. the new secrets dir is denied at both guard.sh (fail-closed hook) and the
     settings-pack deny list (defense-in-depth)

Usage: probe_wq616.py [FOREMAN_ROOT]   (default /home/lumen/foreman)
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/home/lumen/foreman"
HARNESS = os.path.join(ROOT, "harness")
TESTS = os.path.join(HARNESS, "tests")
WQ = os.environ.get("FOREMAN_WQ_BIN", "/home/lumen/bin/wq")
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def sh(cmd, cwd=None, timeout=120, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


print("== 0. suites")
p = sh([sys.executable, "-m", "pytest", "tests", "-q"], cwd=HARNESS, timeout=300)
check("0.1 harness pytest (fworker/concurrency/spawner/guard/settings)", p.returncode == 0,
      p.stdout.strip().splitlines()[-1] if p.stdout else p.stderr[-300:])

sys.path.insert(0, HARNESS)
import importlib  # noqa: E402
import fworker  # noqa: E402
import concurrency  # noqa: E402

tmp = tempfile.mkdtemp(prefix="probe616-")

print("== A. static oauth token injection")
importlib.reload(fworker)
token_file = os.path.join(tmp, "token")
with open(token_file, "w") as f:
    f.write("sk-ant-oat01-probe616-dummy\n")
fworker.OAUTH_TOKEN_FILE = token_file
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-be-scrubbed"
env = fworker.build_env("probe616-worker", "high", os.path.join(tmp, "wt"))
del os.environ["ANTHROPIC_API_KEY"]
check("A.1 token file present -> CLAUDE_CODE_OAUTH_TOKEN set",
      env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-probe616-dummy")
check("A.2 ANTHROPIC_API_KEY/BASE_URL/AUTH_TOKEN never carried to the child",
      not any(k in env for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")))
fworker.OAUTH_TOKEN_FILE = os.path.join(tmp, "no-such-token")
env2 = fworker.build_env("probe616-worker", "high", os.path.join(tmp, "wt"))
check("A.3 token file absent -> falls back without crashing (no fake token set)",
      "CLAUDE_CODE_OAUTH_TOKEN" not in env2)

print("== B. concurrency cap")
lumen_run_dir = os.path.join(tmp, "bin")
os.makedirs(lumen_run_dir, exist_ok=True)
lumen_run = os.path.join(lumen_run_dir, "lumen-run")
fake_shim = os.path.join(TESTS, "fake_lumen_run.py")
with open(lumen_run, "w") as f:
    f.write(f"#!/usr/bin/env bash\nexec python3 {fake_shim} \"$@\"\n")
os.chmod(lumen_run, 0o755)
state_file = os.path.join(tmp, "units.json")
with open(state_file, "w") as f:
    json.dump(["fworker-wq901", "fworker-wq902", "fworker-wq903"], f)
os.environ["FAKE_LUMEN_RUN_STATE"] = state_file
concurrency.LUMEN_RUN_BIN = lumen_run
avail, units = concurrency.slots_available(ceiling=3)
check("B.1 ceiling=3, 3 running -> 0 slots available", avail == 0 and len(units) == 3)
with open(state_file, "w") as f:
    json.dump(["fworker-wq901"], f)
avail2, _ = concurrency.slots_available(ceiling=3)
check("B.2 one unit freed -> slot reopens", avail2 == 2)
del os.environ["FAKE_LUMEN_RUN_STATE"]

print("== C. governor deny skips spawn (no lumen-run start call)")
os.environ["WQ_DB"] = os.path.join(tmp, "q.db")
os.environ["WQ_AGENT"] = "probe616"
p = sh([WQ, "add", "--title", "probe616 governor-deny sample", "--model", "claude-sonnet-5",
        "--effort", "high"], env=os.environ.copy())
tid = int(p.stdout.strip().split()[2])
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
import spawner  # noqa: E402
fake_governor = os.path.join(TESTS, "fake_governor.py")
spawner.GOVERNOR_CLI = fake_governor
os.environ["FAKE_GOVERNOR_DENY"] = "always"
call_log = os.path.join(tmp, "calls.jsonl")
os.environ["FAKE_LUMEN_RUN_LOG"] = call_log
os.environ["PATH"] = lumen_run_dir + os.pathsep + os.environ.get("PATH", "")
results = spawner.dispatch_batch([tid], ROOT, ceiling=3)
calls = []
if os.path.exists(call_log):
    with open(call_log) as f:
        calls = [json.loads(l) for l in f if l.strip()]
check("C.1 governor deny -> ticket skipped, no lumen-run start call",
      results.get(tid) is None and not calls)
del os.environ["FAKE_GOVERNOR_DENY"]

print("== D. secrets path denied (guard.sh + settings pack)")
guard = os.path.join(HARNESS, "guard.sh")
payload = json.dumps({"tool_name": "Bash",
                       "tool_input": {"command": "cat /home/lumen/.secrets/claude-oat-worker.token"},
                       "session_id": "probe616", "cwd": "/tmp", "effort": {"level": "high"}})
p = sh([guard], cwd=HARNESS, env=dict(os.environ, FOREMAN_WORKER="probe616"))
gp = subprocess.run([guard], input=payload, capture_output=True, text=True,
                     env=dict(os.environ, FOREMAN_WORKER="probe616", FOREMAN_EVENTS_DIR=tmp))
check("D.1 guard.sh blocks Bash cat of the secrets dir", gp.returncode == 2)
with open(os.path.join(HARNESS, "worker-settings.json")) as f:
    settings = json.load(f)
check("D.2 worker-settings.json denies the secrets dir too",
      any(".secrets" in d for d in settings["permissions"]["deny"]))

print(f"\nPASS {len(PASS)}  FAIL {len(FAIL)}" + (f"  -> {FAIL}" if FAIL else ""))
print("PROBE_RESULT: " + ("GREEN" if not FAIL else "RED"))
sys.exit(0 if not FAIL else 1)
