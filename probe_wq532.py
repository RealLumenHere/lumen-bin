#!/usr/bin/env python3
"""probe_wq532 — red/green guard for coordination rules moved from prose into tooling.

Each section reproduces a coordination failure recorded on 2026-08-13 and asserts the tooling
now catches it. Runs against a throwaway sqlite file; refuses to start near the real queue.

Usage: probe_wq532.py [DIR]   where DIR holds the wq / slock / lumen-pr under test
"""
import os, sys, subprocess, sqlite3, tempfile, shutil, datetime

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
WQ, SLOCK, LPR = os.path.join(D, "wq"), os.path.join(D, "slock"), os.path.join(D, "lumen-pr")
FMT = "%Y-%m-%d %H:%M:%S"
PROD = "/home/lumen/agents/work-queue/queue.db"
TMP = tempfile.mkdtemp(prefix="wq532-")
DB = os.path.join(TMP, "queue.db")
if os.path.abspath(DB).startswith(os.path.dirname(PROD)):
    sys.exit("REFUSED: temp DB resolved inside the production queue directory")

PASS, FAIL = [], []
def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))

def run(argv, agent=None, env=None):
    e = dict(os.environ); e["WQ_DB"] = DB
    e.pop("WQ_AGENT", None); e.pop("WQ_STRICT", None)
    if agent: e["WQ_AGENT"] = agent
    if env: e.update(env)
    p = subprocess.run([str(x) for x in argv], capture_output=True, text=True, env=e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def add(lane, title, pri=2, subs=None):
    argv = [WQ, "add", "--lane", lane, "--pri", pri, "--title", title]
    if subs: argv += ["--subsystems", subs, "--force"]
    rc, out = run(argv)
    return int(out.strip().split()[2])

print(f"probe_wq532 — tools under test: {D}\n            — throwaway db  : {DB}\n")

# ---- A. --ttl 5m was rejected by argparse, and the caller carried on unlocked (breach 1)
print("A. `--ttl 5m` must work — argparse rejecting it is how lpp-git was breached the first time")
rc, out = run([SLOCK, "acquire", "zzA", "--ttl", "5m", "--holder", "zz-a"])
check("A1", rc == 0 and "ACQUIRED" in out, "--ttl with an 'm' suffix is accepted")
check("A2", "(ttl 5m)" in out, f"5m is read as 5 minutes, not 5 of something else")
rc, out = run([SLOCK, "acquire", "zzB", "--ttl", "2h", "--holder", "zz-a"])
check("A3", rc == 0 and "(ttl 120m)" in out, "2h is read as 120 minutes")
rc, out = run([SLOCK, "acquire", "zzC", "--ttl", "banana", "--holder", "zz-a"])
check("A4", rc != 0, "an unparseable ttl is a hard error, never a silent default")
rc, out = run([SLOCK, "acquire", "zzD", "--holder", "zz-a"])
check("A5", "(ttl 30m)" in out, "the default ttl is sized to an operation, not to a session")

# ---- B. DENIED must be non-zero, and the piped form that hid it (breach 2)
print("\nB. DENIED is the safety signal")
# Deliberately seeded with a PLAIN INTEGER ttl so this section is independent of section A.
# Chaining it off zzA made B1 go red on the old build only because the 5m acquire had failed —
# a cascade, reported as a defect in code that does not have one.
run([SLOCK, "acquire", "zzB1", "--ttl", "30", "--holder", "zz-a"])
rc, out = run([SLOCK, "acquire", "zzB1", "--holder", "zz-other"])
check("B1", rc != 0 and "DENIED" in out, "a contested acquire exits non-zero (true on BOTH builds)")
p = subprocess.run(f"{SLOCK} acquire zzB1 --holder zz-other | tail -2", shell=True,
                   capture_output=True, text=True, env={**os.environ, "WQ_DB": DB})
check("B2", p.returncode == 0,
      "PIPING an acquire still hides DENIED (documented, unfixable from inside slock)")

# ---- C. the fix for B2: no shell idiom left to get wrong
print("\nC. lumen-pr owns the critical section itself (wq#532a)")
rc, out = run([LPR, "gitlock", "/home/lumen/pokemon", "scope"])
check("C1", out.strip() == "lpp-git", f"the pokemon repo resolves to the established scope (got {out.strip()!r})")
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "scope"])
check("C2", out.strip() == "lumen-bin-git", f"other repos get <repo>-git (got {out.strip()!r})")
wt = "/home/lumen/scratch/worktrees"
rc, out = run(["bash", "-c", f"ls -d {wt}/* 2>/dev/null | head -1"])
sib = out.strip().splitlines()[0] if out.strip() else None
if sib:
    rc, o2 = run([LPR, "gitlock", sib, "scope"])
    rc2, o3 = run([LPR, "gitlock", "/home/lumen/bin", "scope"])
    check("C3", o2.strip() == o3.strip(),
          f"a linked worktree resolves to the SAME scope as its primary tree ({o2.strip()})")
else:
    check("C3", False, "no worktree available to test scope stability")
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "status"])
check("C4", "(unheld)" in out, "gitlock status reports an unheld scope")

print("\nD. the lock is taken and dropped by the tool, and a hand-taken lock is respected")
E_LPR = {"LUMEN_PR_SLOCK": SLOCK}
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "acquire"], agent="zz-lpr", env=E_LPR)
check("D1", rc == 0 and "ACQUIRED" in out, "gitlock acquire takes the derived scope")
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "status"], agent="zz-lpr", env=E_LPR)
check("D2", "zz-lpr" in out, "the holder is the agent identity, not a per-process pid")
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "acquire"], agent="zz-other", env=E_LPR)
check("D3", rc != 0 and "another agent is mid-merge" in out,
      "a contested git lock REFUSES with a readable reason instead of proceeding")
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "acquire"], agent="zz-lpr", env=E_LPR)
check("D4", rc == 0 and "already held by" in out,
      "a lock the caller already holds is left alone, not double-taken")
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "release"], agent="zz-lpr", env=E_LPR)
rc, out = run([LPR, "gitlock", "/home/lumen/bin", "status"], agent="zz-lpr", env=E_LPR)
check("D5", "(unheld)" in out, "release actually drops it")

print("\nE. a lock names its ticket, and a priority inversion is loud (failure 7: p4 blocked p1)")
t_p1 = add("pokemon", "p1 gameplay work that needs the repo", pri=1, subs="lpp-repo")
t_p4 = add("github", "p4 housekeeping", pri=4, subs="lpp-repo")
run([WQ, "whoami", "--register", "zz-p4"], agent=None)
rc, out = run([WQ, "claim", "--lane", "github"], agent="zz-p4")
check("E1", rc == 0 and str(t_p4) in out, "the low-priority ticket claims and takes lpp-repo")
rc, out = run([SLOCK, "acquire", "lpp-repo", "--holder", "zz-p4", "--note", f"wq#{t_p4}"])
check("E2", "PRIORITY INVERSION WARNING" in out and f"wq#{t_p1}" in out,
      "acquiring names the higher-priority ticket in the other lane that declares this subsystem")
rc, out = run([SLOCK, "who", "lpp-repo"])
check("E3", f"wq#{t_p4}" in out and "[github/p4" in out,
      "slock who shows the holding ticket, its lane and its priority")
rc, out = run([SLOCK, "acquire", "zzE", "--holder", "zz-p4", "--note", f"wq#{t_p4}"])
check("E4", "PRIORITY INVERSION" not in out,
      "no warning on a scope nobody higher-priority declares — the check is scope-specific")

print("\nF. [DRAFT] tickets are not claimable (failure 5)")
t_draft = add("zzF", "[DRAFT] not ready to be worked", pri=1)
t_real  = add("zzF", "actual work", pri=3)
rc, out = run([WQ, "claim", "--lane", "zzF"], agent="zz-claimer")
check("F1", rc == 0 and f"CLAIMED {t_real}" in out,
      "claim skips the p1 [DRAFT] and hands out the real p3 instead")
check("F2", str(t_draft) in out and "DRAFT" in out,
      "and it SAYS the draft was skipped rather than silently passing over it")
rc, out = run([WQ, "list", "--lane", "zzF"])
check("F3", str(t_draft) in out, "the draft is still visible in the queue, just not claimable")

print("\nG. the restart freeze is enforced, not requested (failure 3)")
print("   NB: lpp-restart is a NEW FILE, so every G assertion is red on the old build purely by")
print("   non-existence. G is a SPECIFICATION of the gate, not a discrimination test of it.")
RESTART = os.path.join(D, "lpp-restart")
FZ = os.path.join(TMP, "RESTART_FREEZE")
EV = os.path.join(TMP, "events"); os.makedirs(EV, exist_ok=True)
RENV = {"LPP_RESTART_FREEZE": FZ, "LPP_RESTART_EVENTS": EV,
        "LPP_RESTART_SLOCK": SLOCK, "LPP_RESTART_WQ": WQ}

rc, out = run([RESTART, "--dry-run"], agent="zz-r", env=RENV)
check("G1", rc == 0 and "DRY RUN" in out, "no freeze: the gates pass and the lock round-trips")
rc, out = run([SLOCK, "who", "lumen-pokemon-live"], env=RENV)
check("G2", "(no active locks)" in out or "lumen-pokemon-live" not in out,
      "the service lock is RELEASED on exit, not leaked")

open(FZ, "w").write("frozen for the overnight soak\n")
rc, out = run([RESTART, "--dry-run"], agent="zz-r", env=RENV)
check("G3", rc != 0 and "names no licensed ticket" in out,
      "a freeze that licenses nobody stops everyone, including the caller")

t_lic = add("zzG", "the licensed ticket", pri=1)
open(FZ, "w").write(f"overnight soak, licensed: wq#{t_lic}\n")
rc, out = run([RESTART, "--dry-run"], agent="zz-r", env=RENV)
check("G4", rc != 0 and f"licenses wq#{t_lic} only" in out,
      "a licensed freeze still refuses a caller who does not name the ticket")
rc, out = run([RESTART, "--dry-run", "--ticket", "wq#999999"], agent="zz-r", env=RENV)
check("G5", rc != 0, "naming the WRONG ticket is refused")

rc, out = run([RESTART, "--dry-run", "--ticket", f"wq#{t_lic}"], agent="zz-r", env=RENV)
check("G6", rc != 0 and "claimed by" in out,
      "naming the RIGHT ticket is not enough — the claim is the proof, and anyone can pass --ticket")

run([WQ, "whoami", "--register", "zz-r"])
rc, out = run([WQ, "claim", "--lane", "zzG"], agent="zz-r")
rc, out = run([RESTART, "--dry-run", "--ticket", f"wq#{t_lic}"], agent="zz-r", env=RENV)
check("G7", rc == 0 and "DRY RUN" in out,
      "the session that actually holds the licensed ticket gets through")
rc, out = run([RESTART, "--dry-run", "--ticket", f"wq#{t_lic}"], agent="zz-impostor", env=RENV)
check("G8", rc != 0, "a different session, same ticket number, is still refused")

run([SLOCK, "acquire", "lumen-pokemon-live", "--holder", "zz-someone-else", "--ttl", "5"])
rc, out = run([RESTART, "--dry-run", "--ticket", f"wq#{t_lic}"], agent="zz-r", env=RENV)
check("G9", rc != 0 and "owns the live service" in out,
      "a contested service lock refuses instead of restarting on top of another session")

print(f"\nprobe_wq532: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL: print("FAILED: " + ", ".join(FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
