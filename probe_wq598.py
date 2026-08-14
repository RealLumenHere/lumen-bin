#!/usr/bin/env python3
"""probe_wq598 — session-scoped agent scratch paths.

WHAT THIS DOES AND DOES NOT ASSERT, stated first because the sections are not equal:

  DISCRIMINATORS (B, C, D) fail on the pre-change build for a BEHAVIOURAL reason, not because
  a function is missing. B4/B5 are the ones that matter: the old build printed
  "worktree already exists" and proceeded into a sibling's dirty tree, exit 0.

  REGRESSION GUARDS (A, E) pass on BOTH builds by design. They pin premise facts measured for
  this ticket (lumen-pr writes nothing to /tmp; dispatches carry no /tmp convention) and the
  unchanged behaviour of `start` on a fresh worktree. They prove nothing about the change.
  Labelled so nobody reads section A's green as evidence the guard works.

NOTHING HERE TOUCHES THE LIVE WORKTREE ROOT. Every worktree is made in a throwaway git repo
under a mkdtemp, with LUMEN_PR_WORKTREE_ROOT pointed there. Nothing here touches the live wq
DB either: WQ_DB and WQ_SCRATCH_ROOT are redirected for section D.
"""
import os, re, shutil, subprocess, sys, tempfile

BIN = os.path.dirname(os.path.abspath(__file__))
LPR = os.path.join(BIN, "lumen-pr")
WQ = os.path.join(BIN, "wq")
TMP = tempfile.mkdtemp(prefix="wq598-")
PASS = FAIL = 0
FAILED = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name}  {detail}")


def sh(cmd, env=None, cwd=None):
    e = dict(os.environ)
    e.pop("WQ_AGENT", None)
    if env:
        e.update(env)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=e, cwd=cwd)
    return p.returncode, p.stdout + p.stderr


def git(repo, *args):
    return sh("git -C %s %s" % (repo, " ".join(args)))


def make_repo(name):
    """A real git repo with an 'origin' that is a real bare repo, so fetch/origin/HEAD work."""
    bare = os.path.join(TMP, name + ".git")
    work = os.path.join(TMP, name)
    sh(f"git init -q --bare -b main {bare}")
    sh(f"git clone -q {bare} {work}")
    git(work, "config user.email probe@lumen.local")
    git(work, "config user.name probe")
    open(os.path.join(work, "README.md"), "w").write("seed\n")
    git(work, "add -A")
    git(work, "commit -qm 'chore: seed'")
    git(work, "push -q origin main")
    git(work, "remote set-head origin main")
    return work


def start(repo, branch, agent=None, wtroot=None, extra="", build=None):
    # LUMEN_PR_SCOPE_CHECK=0 is the escape lumen-pr itself documents "for the probe ONLY".
    # These repos are throwaways under mkdtemp and are correctly out of publishing scope;
    # agent-scope.conf is NOT edited, which wq#546 forbids in exactly this situation.
    env = {"LUMEN_PR_WORKTREE_ROOT": wtroot or os.path.join(TMP, "wtroot"),
           "LUMEN_PR_SCOPE_CHECK": "0"}
    if agent:
        env["WQ_AGENT"] = agent
    return sh(f"{build or LPR} start {repo} {branch} --worktree {extra}", env=env)


# ---------------------------------------------------------------------------
# Build a copy of the PRE-CHANGE lumen-pr from the newest .bak, next to its
# dependency, so BIN_DIR resolves. The .bak has no LUMEN_PR_WORKTREE_ROOT
# override, so its root is patched in the copy — one line, quoted in the report.
# ---------------------------------------------------------------------------
OLD = None
baks = sorted(f for f in os.listdir(BIN) if f.startswith("lumen-pr.bak."))
if baks:
    olddir = os.path.join(TMP, "oldbuild")
    os.makedirs(olddir, exist_ok=True)
    OLD = os.path.join(olddir, "lumen-pr")
    src = open(os.path.join(BIN, baks[-1])).read()
    patched, n = re.subn(
        r"^WORKTREE_ROOT=/home/lumen/scratch/worktrees$",
        'WORKTREE_ROOT=${LUMEN_PR_WORKTREE_ROOT:-/home/lumen/scratch/worktrees}',
        src, count=1, flags=re.M)
    if n != 1:
        OLD = None
    else:
        open(OLD, "w").write(patched)
        os.chmod(OLD, 0o755)
        shutil.copy2(os.path.join(BIN, "lumen-github-identity.sh"), olddir)

print(f"probe_wq598 — under test: {LPR}\n              pre-change : {OLD or 'UNAVAILABLE (no .bak)'}"
      f"\n              scratch    : {TMP}\n")

# ---------------------------------------------------------------------------
print("A. PREMISE PINS — REGRESSION GUARDS, green on both builds, prove nothing about the change")
lpr_src = open(LPR).read()
check("A1 lumen-pr still writes nothing under /tmp (the --body-file surface is caller-owned)",
      not re.search(r"/tmp/", lpr_src))
dispdir = "/home/lumen/agents/work-queue"
disp = [f for f in os.listdir(dispdir) if f.endswith(".md")]
withtmp = [f for f in disp if "/tmp/" in open(os.path.join(dispdir, f), errors="ignore").read()
           and "boards" not in f]
check("A2 no dispatch document prescribes a /tmp path (nothing to rewrite, per the ticket)",
      not withtmp, f"found in {withtmp}")
probes = [f for f in os.listdir(BIN) if re.match(r"probe_wq\d+\.py$", f)]
bad = [f for f in probes
       if re.search(r'^TMP\s*=\s*["\']/tmp/', open(os.path.join(BIN, f), errors="ignore").read(), re.M)]
check("A3 every probe already uses mkdtemp, not a fixed /tmp path", not bad, f"fixed-path: {bad}")
check("A4 branch names still forbid ticket ids and agent names — the reason the path collides",
      "no ticket IDs, no agent names" in lpr_src)

# ---------------------------------------------------------------------------
print("\nB. WORKTREE OWNERSHIP — DISCRIMINATORS")
repo = make_repo("alpha")
WTR = os.path.join(TMP, "wtroot")

rc, out = start(repo, "fix/thing", agent="sessA")
check("B1 fresh --worktree still succeeds", rc == 0 and "created worktree" in out, out[-200:])
wt = os.path.join(WTR, "alpha-fix-thing")
owner = os.path.join(WTR, ".owners", "alpha-fix-thing")
check("B2 an owner record is written, naming the creating session",
      os.path.isfile(owner) and "agent=sessA" in open(owner).read())
check("B3 the owner record is OUTSIDE the worktree — the repo's own status stays clean",
      git(wt, "status --porcelain")[1].strip() == "")

rc, out = start(repo, "fix/thing", agent="sessA")
check("B4 the SAME session reuses its own worktree silently (idempotent resume still works)",
      rc == 0 and "is yours" in out, out[-200:])

rc, out = start(repo, "fix/thing", agent="sessB")
check("B5 a DIFFERENT session adopts a CLEAN worktree already on that branch, and says whose",
      rc == 0 and "sessA" in out and "CLEAN" in out, out[-300:])
check("B6 adoption rewrites the owner record to the adopting session",
      "agent=sessB" in open(owner).read())

open(os.path.join(wt, "half-done.txt"), "w").write("a sibling's uncommitted work\n")
rc, out = start(repo, "fix/thing", agent="sessC")
check("B7 *** a DIFFERENT session REFUSES a DIRTY worktree *** (old build walked straight in)",
      rc != 0 and "refusing to reuse worktree" in out, f"rc={rc} {out[-300:]}")
check("B8 the refusal names the owner and the uncommitted count, not just 'in use'",
      "sessB" in out and "uncommitted: 1" in out, out[-300:])
check("B9 the refusal did NOT disturb the sibling's uncommitted file",
      os.path.isfile(os.path.join(wt, "half-done.txt")))

rc, out = start(repo, "fix/thing", agent="sessC", extra="--adopt")
check("B10 --adopt is the deliberate override and takes it over", rc == 0 and "ADOPTING" in out,
      out[-300:])
check("B11 --adopt still did not destroy the sibling's file (takeover, not a clean slate)",
      os.path.isfile(os.path.join(wt, "half-done.txt")))

git(wt, "checkout -q -b fix/elsewhere")
rc, out = start(repo, "fix/thing", agent="sessD")
check("B12 a worktree parked on ANOTHER branch is refused even though it is the right path",
      rc != 0 and "you asked for fix/thing" in out, f"rc={rc} {out[-300:]}")

rc, out = start(repo, "feat/unregistered", agent=None)
wt2root = os.path.join(WTR, ".owners", "alpha-feat-unregistered")
check("B13 an UNREGISTERED caller creates a worktree but claims no ownership",
      rc == 0 and not os.path.isfile(wt2root), f"rc={rc} owner-file={os.path.isfile(wt2root)}")

# ---------------------------------------------------------------------------
print("\nC. wq scratch — DISCRIMINATORS")
SCR = os.path.join(TMP, "scratchroot")
os.makedirs(SCR, exist_ok=True)
rc, out = sh(f"{WQ} scratch", env={"WQ_AGENT": "sessX", "WQ_SCRATCH_ROOT": SCR})
p = out.strip().splitlines()[0] if out.strip() else ""
check("C1 prints a path under the session id and creates it", rc == 0 and p == os.path.join(SCR, "sessX")
      and os.path.isdir(p), out)
check("C2 the scratch dir is private (0700), not world-readable like bare /tmp files",
      os.path.isdir(p) and oct(os.stat(p).st_mode)[-3:] == "700",
      oct(os.stat(p).st_mode)[-3:] if os.path.isdir(p) else "missing")
rc, out = sh(f"{WQ} scratch --path-only", env={"WQ_AGENT": "sessY", "WQ_SCRATCH_ROOT": SCR})
check("C3 --path-only prints without creating", rc == 0 and not os.path.isdir(os.path.join(SCR, "sessY")))
rc, out = sh(f"{WQ} scratch --path-only", env={"WQ_SCRATCH_ROOT": SCR})
check("C4 an unregistered caller is WARNED that its path changes every call, not silently served",
      "WQ_AGENT is not set" in out, out)
rc1, o1 = sh(f"{WQ} scratch --path-only", env={"WQ_AGENT": "sessX", "WQ_SCRATCH_ROOT": SCR})
rc2, o2 = sh(f"{WQ} scratch --path-only", env={"WQ_AGENT": "sessZ", "WQ_SCRATCH_ROOT": SCR})
check("C5 two sessions never get the same directory", o1.strip() != o2.strip())

# ---------------------------------------------------------------------------
print("\nD. CLAIM-TIME NOTICE — DISCRIMINATORS (scratch DB; the live queue is never opened)")
DB = os.path.join(TMP, "q.db")
SCR2 = os.path.join(TMP, "scratchroot2")
os.makedirs(SCR2, exist_ok=True)
E = {"WQ_DB": DB, "WQ_SCRATCH_ROOT": SCR2, "WQ_AGENT": "claimer1"}
sh(f"{WQ} add --lane probe --title 'first ticket' --detail x", env=E)
sh(f"{WQ} add --lane probe --title 'second ticket' --detail x", env=E)
sh(f"{WQ} add --lane probe --title 'third ticket' --detail x", env=E)
rc, out1 = sh(f"{WQ} claim --lane probe", env=E)
check("D1 the FIRST claim of a session names its scratch dir",
      "SCRATCH (wq#598)" in out1 and os.path.join(SCR2, "claimer1") in out1, out1[-300:])
rc, out2 = sh(f"{WQ} claim --lane probe", env=E)
check("D2 *** the SECOND claim stays quiet — guidance once, not nagging every claim ***",
      "SCRATCH (wq#598)" not in out2, out2[-300:])
check("D3 the claim itself still worked (the notice is never allowed to break a claim)",
      "CLAIMED" in out2, out2[-200:])
rc, out3 = sh(f"{WQ} claim --lane probe", env={**E, "WQ_SCRATCH_ROOT": "/proc/nonwritable"})
# MUST be CLAIMED, never "or NONE". The first version of this assertion accepted NONE, and the
# lane was empty by this point, so the claim returned before the scratch block ever ran — a
# section whose whole job is testing a failure path, passing without reaching it.
check("D4 an unwritable scratch root does not fail a claim (the notice degrades, silently)",
      "CLAIMED" in out3 and "SCRATCH (wq#598)" not in out3, f"rc={rc} {out3[-200:]}")

# ---------------------------------------------------------------------------
print("\nE. PRE-CHANGE BUILD — proves B7/B12 discriminate rather than merely pass")
if not OLD:
    print("  SKIP no usable .bak — cannot demonstrate the old behaviour")
else:
    repo2 = make_repo("beta")
    WTR2 = os.path.join(TMP, "wtroot-old")
    rc, out = start(repo2, "fix/thing", agent="oldA", wtroot=WTR2, build=OLD)
    wt3 = os.path.join(WTR2, "beta-fix-thing")
    open(os.path.join(wt3, "half-done.txt"), "w").write("sibling work\n")
    rc, out = start(repo2, "fix/thing", agent="oldB", wtroot=WTR2, build=OLD)
    check("E1 OLD build walks into a foreign DIRTY worktree, exit 0, one bland note",
          rc == 0 and "worktree already exists" in out, f"rc={rc} {out[-200:]}")
    git(wt3, "checkout -q -b fix/elsewhere")
    rc, out = start(repo2, "fix/thing", agent="oldC", wtroot=WTR2, build=OLD)
    check("E2 OLD build also accepts a worktree parked on a DIFFERENT branch, exit 0",
          rc == 0, f"rc={rc} {out[-200:]}")
    check("E3 OLD build wrote no owner record anywhere",
          not os.path.isdir(os.path.join(WTR2, ".owners")))

# ---------------------------------------------------------------------------
print(f"\nprobe_wq598: {PASS} passed / {FAIL} failed")
if FAILED:
    print("failed: " + ", ".join(FAILED))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
