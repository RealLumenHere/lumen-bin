#!/usr/bin/env python3
"""probe_wq562 — red/green for the LPP watchdog's freeze, defer and attribution.

Usage: probe_wq562.py [PATH-TO-lpp-watchdog.sh]
       defaults to sbin/lpp-watchdog.sh beside this file.

WHAT THIS PROBE EXISTS TO CATCH, and why it runs sections as ROOT rather than as lumen:
the watchdog runs as root under systemd, and git REFUSES to read the lumen-owned pokemon
repo as root ("detected dubious ownership"). Testing by hand hides this completely,
because sudo sets SUDO_UID and git then accepts the repo. So a hand-run sandbox prints
real SHAs while the timer prints `unknown`. Section E therefore runs the script under
`systemd-run` as root with no SUDO_UID — the timer's actual environment — and asserts the
SHAs against ground truth from lpp_frontier.deploy_drift.

SAFETY, and it is load-bearing: every restart-capable section points UNIT at a
non-existent dummy unit AND sets DRYRUN=1 — two independent guards, because a bug in the
script under test must not be able to restart the live service. The one section that uses
the REAL unit (E) drives the FREEZE path, which is structurally incapable of restarting.
"""
import os, re, subprocess, sys, tempfile, shutil, time, json

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "sbin", "lpp-watchdog.sh")
REPO = os.environ.get("LPP_PROBE_REPO", "/home/lumen/pokemon")
REAL_UNIT = "lumen-pokemon-live"
DUMMY_UNIT = "lpp-watchdog-probe-nonexistent.service"
TMP = tempfile.mkdtemp(prefix="wq562-")
PASS, FAIL, SKIP = [], [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def skip(cid, why):
    SKIP.append(cid)
    print("  SKIP " + cid + "  " + why)


def have_sudo():
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def sandbox(name, freeze=None, age=0, events=True):
    """A throwaway events/logs/runtime tree. Returns (dir, logpath)."""
    d = os.path.join(TMP, name)
    os.makedirs(os.path.join(d, "events"), exist_ok=True)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    if events:
        ev = os.path.join(d, "events", "live-agent-probe.jsonl")
        with open(ev, "w") as f:
            f.write("{}\n")
        t = time.time() - age
        os.utime(ev, (t, t))
    if freeze is not None:
        with open(os.path.join(d, "FREEZE"), "w") as f:
            f.write(freeze)
    return d, os.path.join(d, "logs", "watchdog.log")


def env_for(d, unit=DUMMY_UNIT, freeze=True, slock=None, repo=REPO):
    e = {
        "LPP_WATCHDOG_EVENTS": os.path.join(d, "events"),
        "LPP_WATCHDOG_FREEZE": os.path.join(d, "FREEZE") if freeze else os.path.join(d, "NO_SUCH_FREEZE"),
        "LPP_WATCHDOG_UNIT": unit,
        "LPP_WATCHDOG_REPO": repo,
        "LPP_WATCHDOG_DRYRUN": "1",
        "LPP_WEDGE_SECS": "300",
    }
    if slock is not None:
        e["LPP_WATCHDOG_SLOCK"] = slock
    return e


def run(d, env, as_root=False, tag="x"):
    """Run the script; LOGDIR/WATCHLOG are compiled in, so redirect via a wrapper."""
    logf = os.path.join(d, "logs", "watchdog.log")
    wrapper = os.path.join(d, f"run-{tag}.sh")
    lines = ["#!/bin/bash"] + [f"export {k}={v!r}" for k, v in env.items()]
    # WATCHLOG is not overridable in the script; bind-redirect by running a copy whose
    # LOGDIR line is rewritten. This edits only the sandbox copy, never the build.
    copy = os.path.join(d, "under-test.sh")
    src = open(SCRIPT).read()
    src = re.sub(r"^LOGDIR=.*$", f"LOGDIR={os.path.join(d, 'logs')}", src, count=1, flags=re.M)
    with open(copy, "w") as f:
        f.write(src)
    os.chmod(copy, 0o755)
    lines.append(f"exec {copy}")
    with open(wrapper, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(wrapper, 0o755)
    if as_root:
        cmd = ["sudo", "-n", "systemd-run", "--quiet", "--pipe", "--wait",
               f"--unit=wq562-{tag}-{os.getpid()}", "/bin/bash", wrapper]
    else:
        cmd = ["/bin/bash", wrapper]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    log = open(logf).read() if os.path.exists(logf) else ""
    return p.returncode, log, (p.stdout or "") + (p.stderr or "")


print(f"probe_wq562 — script under test: {SCRIPT}\n")

# ---- GUARD: refuse a script that does not honour the sandbox overrides -------
# The Aug-8 live watchdog hardcodes /home/lumen/pokemon and lumen-pokemon-live. Running it
# here would operate on the REAL events dir and issue a REAL restart. Refuse, loudly.
src = open(SCRIPT).read() if os.path.exists(SCRIPT) else ""
overridable = all(k in src for k in ("LPP_WATCHDOG_EVENTS", "LPP_WATCHDOG_UNIT", "LPP_WATCHDOG_DRYRUN"))
if not overridable:
    print("REFUSING TO RUN: the script under test does not honour LPP_WATCHDOG_* overrides.")
    print("  It would operate on the REAL events dir and could restart the LIVE service.")
    print("  This is the expected result for the pre-wq#562 build; that is a RED, not a pass.")
    print("\nprobe_wq562: 0 passed / 1 failed (refused-unsafe)")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)

# ---- A. healthy stream: no restart, just an ok line --------------------------
print("A. a fresh event file logs ok and does not restart")
d, _ = sandbox("a", age=10)
rc, log, out = run(d, env_for(d, freeze=False), tag="a")
check("A1", "ok age=" in log, "logged the ok line")
check("A2", "WEDGE" not in log, "did not wedge on a fresh file")

# ---- B. the wedge still fires (deliverable 4: unchanged behaviour) ----------
print("\nB. a stale event file still wedges — the watchdog's job is NOT rolled back")
d, _ = sandbox("b", age=900)
rc, log, out = run(d, env_for(d, freeze=False), tag="b")
check("B1", "WEDGE:" in log, "wedge fired on a 900s-old file")
check("B2", "DRYRUN: restart suppressed" in log, "restart suppressed under DRYRUN")

# ---- C. FREEZE blocks it, VISIBLY (deliverable 1) ---------------------------
print("\nC. the freeze flag blocks the restart with a visible skip, not silence")
d, _ = sandbox("c", freeze="wq#562 probe freeze\n", age=900)
rc, log, out = run(d, env_for(d, freeze=True), tag="c")
check("C1", "FREEZE:" in log, "freeze skip is logged")
check("C2", "WEDGE:" not in log, "no wedge line while frozen")
check("C3", "DRYRUN: restart suppressed" not in log, "never reached the restart call at all")
check("C4", "wq#562 probe freeze" in log, "freeze contents logged for the reader")

# ---- D. DEFER to a live agent's lock (deliverable 2a) -----------------------
print("\nD. an agent holding the service lock defers the restart")
fake_held = os.path.join(TMP, "slock-held")
with open(fake_held, "w") as f:
    f.write("#!/bin/bash\necho 'lumen-pokemon-live held by rabbit99-abc123 since ...'\n")
os.chmod(fake_held, 0o755)
fake_free = os.path.join(TMP, "slock-free")
with open(fake_free, "w") as f:
    f.write("#!/bin/bash\necho '(no active locks)'\n")
os.chmod(fake_free, 0o755)

d, _ = sandbox("d1", age=900)
rc, log, out = run(d, env_for(d, freeze=False, slock=fake_held), tag="d1")
check("D1", "DEFER:" in log, "deferred while the lock was held")
check("D2", "rabbit99-abc123" in log, "named the holder")
check("D3", "WEDGE:" not in log, "did not restart under a held lock")

# NEGATIVE CONTROL: the defer must not fire when nobody holds the lock, or it is
# just a disguised off-switch for the watchdog.
d, _ = sandbox("d2", age=900)
rc, log, out = run(d, env_for(d, freeze=False, slock=fake_free), tag="d2")
check("D4", "DEFER:" not in log, "negative control: no defer when the lock is free")
check("D5", "WEDGE:" in log, "negative control: wedge still fires when free")

# ---- E. ATTRIBUTION, AS ROOT, AGAINST GROUND TRUTH (deliverable 2b) --------
print("\nE. attribution names the RUNNING build — run as root, the timer's real environment")
sys.path.insert(0, os.path.join(REPO, "tools"))
truth = None
try:
    import lpp_frontier as F
    truth = F.deploy_drift(base=REPO, unit=REAL_UNIT)
except Exception as ex:
    truth = None
    print(f"  (ground truth unavailable: {ex})")

if truth is None or not truth.get("ok"):
    skip("E1", "could not compute ground truth from lpp_frontier.deploy_drift")
elif not have_sudo():
    skip("E1", "no passwordless sudo; cannot reproduce the root/systemd environment")
else:
    head7, run7 = truth["head"][:7], truth["running"][:7]
    ahead = truth["commitsTreeAheadOfRunning"]
    print(f"  ground truth: tree={head7} running={run7} via={truth['runningFrom']} ahead={ahead}")
    # FREEZE path with the REAL unit: it logs attribution and CANNOT restart.
    d, _ = sandbox("e", freeze="wq#562 attribution probe\n", age=900)
    rc, log, out = run(d, env_for(d, unit=REAL_UNIT, freeze=True), as_root=True, tag="e")
    check("E1", "FREEZE:" in log, "ran as root and reached the attribution line")
    # E0 IS NOT DECORATION. The first draft of this probe asserted "no 'unknown' in the
    # line" and "running != tree" — and BOTH passed against a line reading
    # `attribution=UNAVAILABLE reason=resolver-failed`, because a total failure contains
    # neither "unknown" nor a tree-shaped sha. Two assertions that pass when the feature
    # is entirely absent. Everything below is now gated on attribution having produced
    # something at all, and the negative checks are scoped to the attribution SUBSTRING
    # rather than the whole log.
    attrib = ""
    for line in log.splitlines():
        if "tree=" in line or "attribution=" in line:
            attrib = line[line.find("tree=") if "tree=" in line else line.find("attribution="):]
            break
    check("E0", attrib != "" and "UNAVAILABLE" not in attrib,
          f"attribution produced a result at all  [{attrib[:90]}]")
    check("E2", f"tree={head7}" in attrib, f"tree resolved correctly (expected {head7})")
    check("E3", f"running={run7}" in attrib, f"RUNNING sha resolved correctly (expected {run7})")
    check("E4", "via=" in attrib and "via=unknown" not in attrib, "provenance of the running sha is printed")
    check("E5", "unknown" not in attrib and attrib.count("=") >= 4,
          "no 'unknown' field in the attribution line (root git-ownership regression)")
    if ahead > 0:
        check("E6", "running=" in attrib and f"running={head7}" not in attrib,
              f"running != tree while the tree is {ahead} ahead — the L831 bug reports them equal")
    else:
        skip("E6", "tree and running are identical right now, so this check cannot discriminate "
                   "the L831 equal-shas bug; E0/E2/E3/E5 still do")

# ---- G. DETERMINISTIC L831 DISCRIMINATOR — fixture repo, injected start -----
# E6 can only discriminate the equal-shas bug while the live fleet happens to be
# mid-drift; it SKIPs otherwise, so the probe's grip on the HEADLINE bug depended on
# fleet state. This section makes the property provable EVERY run: a throwaway repo
# with two commits at KNOWN reflog times, and a start time injected BETWEEN them via
# LPP_WATCHDOG_STARTED_MS -> deploy_drift(started_ms=...) — the injection path
# deploy_drift's own docstring documents. Correct resolution picks the OLDER commit
# (at-or-before start); the L831 bug family (a no-op awk taking the newest reflog
# entry, always == tree) picks the NEWER and reports no drift.
# Runs UNPRIVILEGED on purpose: the root/ownership axis is section E's job, and
# conflating the two here would let one bug mask the other. The fixture gets a
# tools -> <repo>/tools symlink because the script imports lpp_frontier from
# $REPO/tools, and $REPO here IS the fixture.
print("\nG. deterministic L831 discriminator — injected start time between two known commits")

def _g(cmd, cwd, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=30)

fx = os.path.join(TMP, "fixture-repo")
os.makedirs(fx)
_g(["git", "init", "-q", "-b", "main"], fx)
_g(["git", "config", "user.email", "probe@wq562"], fx)
_g(["git", "config", "user.name", "probe-wq562"], fx)
T0 = 1700000000  # fixed epoch; reflog entry times come from GIT_COMMITTER_DATE
for i, t in ((1, T0), (2, T0 + 100)):
    with open(os.path.join(fx, "f.txt"), "w") as f:
        f.write(f"v{i}\n")
    _g(["git", "add", "f.txt"], fx)
    _g(["git", "commit", "-q", "-m", f"c{i}"], fx,
       env={"GIT_COMMITTER_DATE": f"{t} +0000", "GIT_AUTHOR_DATE": f"{t} +0000"})
_g(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], fx)
os.symlink(os.path.join(REPO, "tools"), os.path.join(fx, "tools"))
with open(os.path.join(fx, ".gitignore"), "w") as f:
    f.write("tools\n")
older = _g(["git", "rev-parse", "--short=7", "HEAD~1"], fx).stdout.strip()
newer = _g(["git", "rev-parse", "--short=7", "HEAD"], fx).stdout.strip()
rl = _g(["git", "reflog", "--date=unix", "--format=%H|%gd"], fx).stdout
gtimes = [int(m.group(1)) for m in re.finditer(r"\{(\d+)\}", rl)]
check("G0", gtimes == [T0 + 100, T0],
      f"fixture reflog carries the two injected times, newest first  [{gtimes}]")

if gtimes == [T0 + 100, T0]:
    def g_run(started_ms, tag):
        d, _ = sandbox(tag, age=900)
        genv = env_for(d, freeze=False, repo=fx)
        genv["LPP_WATCHDOG_STARTED_MS"] = str(started_ms)
        rc, log, out = run(d, genv, tag=tag)
        for line in log.splitlines():
            if "tree=" in line:
                return line[line.find("tree="):]
            if "attribution=" in line:
                return line[line.find("attribution="):]
        return ""
    mid = (T0 + 50) * 1000                          # strictly between the two commits
    attrib = g_run(mid, "g1")
    check("G1", attrib != "" and "UNAVAILABLE" not in attrib,
          f"attribution produced a result against the fixture  [{attrib[:80]}]")
    check("G2", f"tree={newer}" in attrib, f"tree is the NEWER commit (expected {newer})")
    check("G3", f"running={older}" in attrib,
          f"running resolved to the OLDER commit at-or-before start (expected {older}) — "
          f"the L831 bug family resolves the newest and reports no drift")
    check("G4", "via=reflog@process-start" in attrib,
          "provenance says the reflog answered — not a HEAD fallback reported as fact")
    check("G5", "ahead=1" in attrib and "drifted=True" in attrib,
          "drift arithmetic correct on the fixture (1 ahead, drifted)")
    # NEGATIVE CONTROL: move the injected start AFTER the newest commit and the answer
    # must move with it. Without this, G3 could pass on a script that ignores the
    # injection entirely and happens to emit the older sha for an unrelated reason —
    # the non-discriminating-assertion failure this lane has now caught 10+ times.
    attrib2 = g_run((T0 + 160) * 1000, "g2")
    check("G6", f"running={newer}" in attrib2 and "ahead=0" in attrib2,
          f"negative control: start AFTER the newest commit resolves running={newer}, no drift")
else:
    skip("G1", "fixture reflog did not honour injected commit times; substrate problem, sections gated")

# ---- F. missing event file still restarts ----------------------------------
print("\nF. a missing event file is still a wedge")
d, _ = sandbox("f", events=False)
rc, log, out = run(d, env_for(d, freeze=False), tag="f")
check("F1", "NO event file" in log, "missing event file wedges")

# ---- cleanup ---------------------------------------------------------------
if shutil.rmtree(TMP, ignore_errors=True) or os.path.exists(TMP):
    subprocess.run(["sudo", "-n", "rm", "-rf", TMP], capture_output=True)

print(f"\nprobe_wq562: {len(PASS)} passed / {len(FAIL)} failed" +
      (f" / {len(SKIP)} skipped" if SKIP else ""))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
