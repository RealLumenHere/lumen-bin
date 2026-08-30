#!/usr/bin/env python3
"""probe_wq617 — red/green for the T3 orchestrator tier (BUILD_PLAN.md §T3). Zero
Anthropic quota. Runs the orchestrator pytest suite + the harness suite (fworker glue),
then the ticket's three NAMED verify criteria against a scratch wq db using the LIVE
/home/lumen/bin/wq (so it also proves the lumen-bin PR landed):

  A. manifest self-check + 5 sample tickets across archetypes -> correct (model, effort)
  B. a dominated tuple is never selected (exhaustive sweep, live manifest)
  C. verify-pass fires on a shipped diff (and not on a never_for archetype)
  D. wq / agent-prompt carry the tuple (schema + header)

Usage: probe_wq617.py [FOREMAN_ROOT]   (default /home/lumen/foreman)
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/home/lumen/foreman"
ORCH = os.path.join(ROOT, "orchestrator")
WQ = os.environ.get("FOREMAN_WQ_BIN", "/home/lumen/bin/wq")
AGENT_PROMPT = "/home/lumen/bin/agent-prompt"
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def sh(cmd, cwd=None, timeout=600, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


sys.path.insert(0, ORCH)
try:
    import router, judgment  # noqa
except Exception as e:  # pragma: no cover
    print(f"cannot import orchestrator from {ORCH}: {e}")
    sys.exit(2)

print("== 0. suites")
p = sh([sys.executable, "-m", "pytest", "orchestrator/tests", "-q"], cwd=ROOT, env=dict(os.environ, FOREMAN_WQ_BIN=WQ))
check("0.1 orchestrator pytest", p.returncode == 0, p.stdout.strip().splitlines()[-1] if p.stdout else p.stderr[-300:])
p = sh([sys.executable, "-m", "pytest", "tests", "-q"], cwd=os.path.join(ROOT, "harness"))
check("0.2 harness pytest (fworker glue)", p.returncode == 0, p.stdout.strip().splitlines()[-1] if p.stdout else p.stderr[-300:])

print("== A. manifest + sample routing")
m = router.load_manifest(os.path.join(ROOT, "research", "models.yaml"), force=True)
check("A.1 models.yaml self-check", m.check())
SAMPLES = [
    ("wire standing checks to systemd timers", "fleet", "mechanical-edit", ("claude-sonnet-5", "low")),
    ("probe: CI hygiene for lumen-pr", "build", "probe-build", ("claude-sonnet-5", "medium")),
    ("add conflict guard to lumen-pr", "build", "standard-engineering", ("claude-sonnet-5", "high")),
    ("audit: why does lpp-watchdog bypass the guard", "pokemon", "rca-investigation", ("claude-opus-5", "xhigh")),
    ("design lumen-memoryd new service", "memory", "novel-design", ("claude-fable-5", "high")),
]
tmp = tempfile.mkdtemp(prefix="probe617-")
env = dict(os.environ, WQ_DB=os.path.join(tmp, "q.db"), WQ_AGENT="probe617", FOREMAN_WQ_BIN=WQ,
           FOREMAN_RUNS_ROOT=os.path.join(tmp, "runs"), FOREMAN_STATE_JSON=os.path.join(tmp, "state.json"),
           FOREMAN_JUDGMENT_STATE=os.path.join(tmp, "runs", "judgment.json"))
ids = []
for title, lane, arch, exp in SAMPLES:
    out = sh([WQ, "add", "--lane", lane, "--title", title], env=env).stdout
    tid = int(out.split()[2]); ids.append((tid, arch, exp))
    d = json.loads(sh([sys.executable, os.path.join(ORCH, "router.py"), "assign", str(tid)], env=env).stdout or "{}")
    got = (d.get("rung") or {}).get("model"), (d.get("rung") or {}).get("effort")
    show = sh([WQ, "show", str(tid)], env=env).stdout
    check(f"A.2 wq#{tid} {arch} -> {exp[0]}@{exp[1]}",
          d.get("archetype") == arch and got == exp and d.get("written")
          and f"model        : {exp[0]}" in show and f"effort       : {exp[1]}" in show,
          f"got {d.get('archetype')} {got} written={d.get('written')}")

print("== B. dominated never selected")
models = ["claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
bad, n = [], 0
for arch in router.ARCHETYPES + ["orchestrator"]:
    for combo in itertools.product(("ok", "tight", "deny"), repeat=len(models)):
        table = dict(zip(models, combo))
        for fails, cross, contra in itertools.product((0, 1, 2), (False, True), (False, True)):
            r, _ = router.select(m, arch, pressure=lambda mm: table.get(mm, "ok"), fails=fails,
                                 cross_subsystem=cross, contradiction=contra)
            n += 1
            if r and r.model != "DECOMPOSE" and m.is_dominated(r.model, r.effort, arch):
                bad.append((arch, table, r.spec()))
check("B.1 exhaustive sweep emits no dominated tuple", not bad, f"{n} selections, {len(bad)} bad")
check("B.2 opus-4-8 keep_entitled + LIVE + unrouted",
      "claude-opus-4-8" in m.keep_entitled() and str(m.models["claude-opus-4-8"]["status"]).startswith("LIVE")
      and not [r for r in m.roster() if r["id"] == "claude-opus-4-8"][0]["routed"])
check("B.3 effort-first: standard-engineering tight -> sonnet-5@medium",
      router.select(m, "standard-engineering", pressure=lambda x: "tight")[0].key() == ("claude-sonnet-5", "medium"))
check("B.4 fable never downgrades into dominated effort",
      router.select(m, "novel-design", pressure=lambda x: "tight")[0].key() == ("claude-fable-5", "high"))

print("== C. verify-pass fires on a shipped diff")
se = [t for t, a, _ in ids if a == "standard-engineering"][0]
nd = [t for t, a, _ in ids if a == "novel-design"][0]
sh([WQ, "claim", "--id", str(se)], env=env); sh([WQ, "done", str(se), "--notes", "PR https://example/x/pull/1"], env=env)
J = [sys.executable, os.path.join(ORCH, "judgment.py")]
d = json.loads(sh(J + ["verify-pass", "--for", str(se)], env=env).stdout or "{}")
vt = sh([WQ, "show", str(d.get("filed") or 0)], env=env).stdout if d.get("filed") else ""
check("C.1 shipped standard-engineering diff -> verify ticket filed",
      bool(d.get("required")) and bool(d.get("filed")), json.dumps(d)[:200])
check("C.2 verifier tuple sonnet-5@high, archetype verify-pass, deps on shipped",
      "model        : claude-sonnet-5" in vt and "effort       : high" in vt
      and "archetype    : verify-pass" in vt and f"deps         : {se}" in vt)
d2 = json.loads(sh(J + ["verify-pass", "--for", str(se)], env=env).stdout or "{}")
check("C.3 idempotent (second call reuses ticket)", d2.get("filed") == d.get("filed"))
d3 = json.loads(sh(J + ["verify-pass", "--for", str(nd)], env=env).stdout or "{}")
check("C.4 novel-design (never_for) not verified", not d3.get("required") and d3.get("filed") is None)
dj = json.loads(sh(J + ["due", "--no-scan"], env=env).stdout or "{}")
check("C.5 judgment due -> fable-5@high (no session recorded yet)", dj.get("due") and dj.get("tuple") == "claude-fable-5@high")

print("== D. wq schema + agent-prompt header")
p = sh([WQ, "add", "-h"])
check("D.1 live wq add/edit accept --model/--effort/--archetype", "--model" in p.stdout and "--archetype" in p.stdout)
p = sh([WQ, "edit", str(nd), "--effort", "3"], env=env)
check("D.2 wq refuses integer effort", p.returncode != 0 and "REFUSED" in p.stdout)
sh([WQ, "add", "--lane", "github", "--title", "probe617 gh sample", "--model", "claude-opus-5", "--effort", "xhigh"], env=env)
p = sh([AGENT_PROMPT, "github"], env=env)
check("D.3 agent-prompt header carries tuple from ticket", "model=claude-opus-5@xhigh (wq#" in p.stdout, p.stdout.splitlines()[1] if p.stdout else "")
p = sh([AGENT_PROMPT, "github", "--model", "claude-sonnet-5", "--effort", "low"], env=env)
check("D.4 agent-prompt --model/--effort flag wins", "model=claude-sonnet-5@low (flag)" in p.stdout)
p = sh([AGENT_PROMPT, "github", "--fable"], env=env)
check("D.5 agent-prompt --fable -> claude-fable-5@high", "model=claude-fable-5@high (fable)" in p.stdout)

print(f"\nPASS {len(PASS)}  FAIL {len(FAIL)}" + (f"  -> {FAIL}" if FAIL else ""))
print("PROBE_RESULT: " + ("GREEN" if not FAIL else "RED"))
sys.exit(0 if not FAIL else 1)
