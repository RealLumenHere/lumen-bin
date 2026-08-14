#!/usr/bin/env python3
"""probe_wq565 — red/green for wiring the standing checks to timers.

The ticket's own warning is the load-bearing assertion here: DO NOT PIPE EITHER CHECK. A
pipeline exits with its LAST command's status, so `check | tail` reports success no matter
what the check found. Section B asserts that property directly rather than trusting that
whoever wrote the unit remembered.

Section C covers the defect found while wiring: pat-scan's docstring says
/home/lumen/.secrets/*.token is canonical, but CANONICAL was a hardcoded two-filename set,
so wq#524's github-frank.token was STRAY the moment it was created in the correct place by
the fleet convention — the allowlist-someone-must-extend failure this tool's own docstring
claims it avoids by construction.

Usage: probe_wq565.py [DIR]   where DIR holds the checks under test
"""
import os, sys, subprocess, tempfile, shutil, stat

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
PAT = os.path.join(D, "wq523_pat_residue.py")
LINT = os.path.join(D, "dispatch-lint")
UNITS = "/etc/systemd/system"
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def sh(cmd, timeout=600):
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def unit(name):
    try:
        return open(os.path.join(UNITS, name)).read()
    except OSError:
        return ""


print(f"probe_wq565 — under test: {D}\n")

svc_pat = unit("lumen-pat-scan.service")
svc_lint = unit("lumen-dispatch-lint.service")
tmr_pat = unit("lumen-pat-scan.timer")
tmr_lint = unit("lumen-dispatch-lint.timer")

print("A. the units exist, are installed, and are scheduled")
check("A1", bool(svc_pat) and bool(tmr_pat), "pat-scan service + timer units on disk")
check("A2", bool(svc_lint) and bool(tmr_lint), "dispatch-lint service + timer units on disk")
rc, out = sh("systemctl list-timers 'lumen-pat-scan*' 'lumen-dispatch-lint*' --no-pager")
check("A3", "lumen-pat-scan.timer" in out and "lumen-dispatch-lint.timer" in out,
      "both timers are ENABLED and scheduled — a unit file nobody enabled is a file, not a timer")
check("A4", "OnCalendar=daily" in tmr_pat and "OnCalendar=daily" in tmr_lint,
      "daily, as the ticket asked")
check("A5", "Persistent=true" in tmr_pat and "Persistent=true" in tmr_lint,
      "Persistent — a check that silently skips while the box was off is not a standing check")

print("\nB. *** THE ONE THING THAT WILL BITE YOU: neither check is PIPED ***")
for cid, name, body in (("B1", "pat-scan", svc_pat), ("B2", "dispatch-lint", svc_lint)):
    execs = [l for l in body.splitlines() if l.strip().startswith("ExecStart=")]
    piped = [l for l in execs if "|" in l]
    check(cid, execs and not piped,
          f"{name} ExecStart invokes the binary directly, no pipeline  {execs}")
def directives(body):
    """Unit-file directive lines only. Both units EXPLAIN in a comment why they omit
    SuccessExitStatus=, and a naive substring search matches that explanation — the same
    self-matching-comment bug that bit probe_wq524 C1. Read directives, not prose."""
    return [l.strip() for l in body.splitlines()
            if l.strip() and not l.strip().startswith("#")]


_ses = [l for l in directives(svc_pat) + directives(svc_lint)
        if l.startswith("SuccessExitStatus")]
check("B3", not _ses,
      f"neither unit whitelists exit 1 — exit 1 IS the finding and must mark the unit failed  {_ses}")
check("B4", "OnFailure=" in svc_pat and "OnFailure=" in svc_lint,
      "and a failed unit actually notifies (ntfy + aq via the wq#392 template)")

print("\nB'. the piping hazard, demonstrated rather than asserted")
rc_direct, _ = sh(f"{PAT} --alert >/dev/null 2>&1")
rc_piped, _ = sh(f"{PAT} --alert 2>/dev/null | tail -1 >/dev/null")
check("B5", rc_direct == 1 and rc_piped == 0,
      f"the SAME check reports {rc_direct} direct and {rc_piped} through a pipe — "
      "this is why ExecStart must never pipe")


print("\nC. the canonical-homes allowlist gap (found while wiring)")
src = open(PAT).read()
check("C1", "CANONICAL_GLOBS" in src and ".secrets/*.token" in src,
      "canonical homes are GLOBBED, matching what the docstring always claimed")
rc, out = sh(f"{PAT} --scan")
check("C2", "/home/lumen/.secrets/github-frank.token" not in out,
      "*** wq#524's correctly-placed secret is no longer reported STRAY ***")

TMPTOK = "/home/lumen/.secrets/probe_wq565_fixture.token"
try:
    with open(TMPTOK, "w") as f:
        f.write("ghp_" + "Z" * 36)
    os.chmod(TMPTOK, 0o600)
    rc, out = sh(f"{PAT} --scan")
    check("C3", "probe_wq565_fixture.token" not in out,
          "a BRAND NEW secret in .secrets is canonical immediately — nobody has to remember "
          "to extend a list, which is the whole point")
finally:
    if os.path.exists(TMPTOK):
        os.remove(TMPTOK)
check("C4", not os.path.exists(TMPTOK), "fixture cleaned up — .secrets left exactly as found")

print("\nD. --alert severity: noisy-by-directory replaced by risky-by-property")
rc_alert, out_alert = sh(f"{PAT} --alert")
rc_scan, out_scan = sh(f"{PAT} --scan")
n_scan = len([l for l in out_scan.splitlines() if l.strip().startswith("STRAY")])
n_crit = len([l for l in out_alert.splitlines() if l.strip().startswith("CRITICAL")])
check("D1", n_scan > n_crit,
      f"--alert reports {n_crit} CRITICAL where --scan reports {n_scan} STRAY — a daily "
      "alert listing every known dead-token backup is one nobody reads")
check("D2", "noted" in out_alert and str(n_scan - n_crit) in out_alert,
      "the non-critical remainder is COUNTED, never silently suppressed — the ticket is "
      "explicit that excluding backup dirs is the wrong fix")
check("D3", all(("WORLD-READABLE" in l or "LIVE token" in l)
                for l in out_alert.splitlines() if l.strip().startswith("CRITICAL")),
      "every CRITICAL names why it is critical: world-readable, or still authenticates")
check("D4", "ghp_" not in out_alert and "github_pat_" not in out_alert,
      "and no token VALUE is ever printed — fingerprints only")


print("\nE. the alert chain actually fired, end to end")
rc, out = sh("systemctl show lumen-pat-scan.service -p Result --value")
check("E1", "exit-code" in out,
      "the real unit run FAILED on the real finding (not a simulated failure)")
rc, out = sh("systemctl show lumen-dispatch-lint.service -p Result --value")
check("E2", "success" in out,
      "while the clean check reports success — the two are distinguishable")
rc, out = sh("/home/lumen/bin/aq list open")
check("E3", "svc-fail-lumen-pat-scan.service" in out,
      "*** an aq row persists, so the finding outlives the notification ***")
rc, out = sh("systemctl show 'ntfy-onfailure@lumen-pat-scan.service.service' -p Result --value")
check("E4", "success" in out, "the OnFailure handler itself ran cleanly")

print("\nF. dispatch-lint's timer runs the DEFAULT check, not --universal")
check("F1", "--universal" not in [w for l in svc_lint.splitlines()
                                  if l.strip().startswith("ExecStart=") for w in l.split()],
      "the universal check (wq#561) is red BY DESIGN today; a timer that cries wolf on day "
      "one gets muted, taking the retired-rule check down with it")
rc, out = sh(f"{LINT}")
check("F2", rc == 0, "and the default check the timer runs is currently green")

print()
print(f"probe_wq565: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
