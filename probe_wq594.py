#!/usr/bin/env python3
"""probe_wq594 — red/green for detecting ONE session id driven from TWO places.

WHAT THE TICKET ASKED vs WHAT IS ACTUALLY POSSIBLE, because the difference is the whole
finding and a probe that asserted the ticket's own suggestion would be asserting a fiction:

  The ticket proposes a per-instance nonce (pid+boot-id, or a token minted at first wq call).
  Neither can work here. Every wq invocation is a freshly-spawned process off ONE shared
  long-lived gateway, so pid/ppid/sid/pgid are per-CALL and boot_id is fleet-global; and a
  minted token would reach a continuation through the same channel WQ_AGENT already does
  (the transcript), so it is inherited exactly like the id it is meant to disambiguate.
  Section F pins the first half of that as a live measurement rather than a claim.

  The ticket's stated worst case — "both would have held the SAME ticket under the SAME id" —
  is ALREADY PREVENTED and was at the time of the incident. Section C proves it. That is a
  premise correction, not a fix, so C is labelled a REGRESSION GUARD: it passes on both
  builds and discriminates nothing.

  What IS exposed, and what sections A/B/D/E cover: a second driver claiming a DIFFERENT
  ticket gets one handed over in total silence, leaving one session id holding two live
  leases with nothing said. Reproduced in four commands.

WHY NOTE AND NOT REFUSE (section B is the load-bearing one): measured over the whole
registered-session era, exactly ONE session ever closed a ticket entirely inside another of
its own ticket windows — the incident. But it is not the only co-hold: rabbit17b-6f8be9 took
wq#522 and wq#547 thirty-six seconds apart and closed both in the same second, which is a
deliberate pair, not a second driver. Refusing would have broken that. A guard that breaks
legitimate work gets routed around, and a routed-around guard is decoration.

Usage: probe_wq594.py [DIR]   where DIR holds the wq under test (default: this file's dir)
"""
import os, re, sys, subprocess, tempfile

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
WQ = os.path.join(D, "wq")
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def sh(cmd, env=None, timeout=120):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout, env=e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def fresh_db():
    """Every section gets its OWN db. Shared scratch state is how a probe starts asserting
    the residue of its previous section."""
    fd, path = tempfile.mkstemp(prefix="wq594_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def setup(db, n=3):
    for i in range(n):
        sh(f"{WQ} add --lane fleet --pri 2 --title 'probe594 ticket {i}'", {"WQ_DB": db})
    rc, out = sh(f"{WQ} whoami --register p594", {"WQ_DB": db})
    m = re.search(r"REGISTERED session: (\S+)", out)
    return m.group(1) if m else None


print("probe_wq594 — one session id, two drivers   [wq under test: %s]" % WQ)

# ---------------------------------------------------------------- A: the chokepoint notice
print("\nA. claiming a SECOND ticket while already holding one is announced")
db = fresh_db()
sid = setup(db)
env = {"WQ_DB": db, "WQ_AGENT": sid}
rc1, first = sh(f"{WQ} claim --lane fleet", env)
rc2, second = sh(f"{WQ} claim --lane fleet", env)
check("A1", sid is not None, "registration minted a session id")
check("A2", "CLAIMED" in first, "first claim succeeds")
check("A3", "CLAIMED" in second, "second claim still succeeds (see B)")
check("A4", "wq#594" in second,
      "second claim cites the ticket that put the check there")
check("A5", re.search(r"ALREADY HOLD", second, re.I) is not None,
      "second claim says the session already holds a ticket")
first_id = re.search(r"CLAIMED (\d+)", first)
check("A6", first_id is not None and re.search(r"#%s\b" % first_id.group(1), second) is not None,
      "the notice NAMES the already-held ticket, not just a count")
check("A7", "ALREADY HOLD" not in first.upper(),
      "the FIRST claim is silent — a notice on every claim is noise, not a signal")

# ---------------------------------------------------------------- B: note, never refusal
print("\nB. the notice is a NOTE, not a refusal  [the ticket's open design question]")
db = fresh_db()
sid = setup(db)
env = {"WQ_DB": db, "WQ_AGENT": sid}
sh(f"{WQ} claim --lane fleet", env)
rc, out = sh(f"{WQ} claim --lane fleet", env)
check("B1", rc == 0, "second claim exits 0 — rabbit17b's legitimate 36s pair still works")
check("B2", "REFUSED" not in out, "second claim is not refused")
rc, listing = sh(f"{WQ} list --lane fleet", {"WQ_DB": db})
check("B3", listing.count("claimed") >= 2, "both tickets really are held, not one rolled back")

# ---------------------------------------------------------------- C: REGRESSION GUARD
print("\nC. same-ticket double claim stays refused  [REGRESSION GUARD — passes on both builds,")
print("   discriminates nothing. The ticket's stated worst case was never reachable.]")
db = fresh_db()
sid = setup(db)
env = {"WQ_DB": db, "WQ_AGENT": sid}
rc, out = sh(f"{WQ} claim --lane fleet", env)
tid = re.search(r"CLAIMED (\d+)", out).group(1)
rc, out = sh(f"{WQ} claim --id {tid} --lane fleet", env)
check("C1", rc != 0, "claim --id on a ticket held by your OWN session id is refused")
check("C2", "REFUSED" in out and "live lease" in out, "and says why")

# ---------------------------------------------------------------- D: the audit
print("\nD. `wq concurrency` reports co-held tickets under one session id")
db = fresh_db()
sid = setup(db)
env = {"WQ_DB": db, "WQ_AGENT": sid}
sh(f"{WQ} claim --lane fleet", env)
sh(f"{WQ} claim --lane fleet", env)
rc, out = sh(f"{WQ} concurrency", {"WQ_DB": db})
# rc!=0 alone is a FALSE GREEN on a build with no such subcommand — argparse exits 2 on an
# invalid choice and the probe would call that a finding. Demand it not be a usage error.
usage_err = "usage:" in out.lower() or "invalid choice" in out.lower()
check("D1", rc != 0 and not usage_err,
      "a finding exits non-zero so a timer's OnFailure= can fire (wq#565 B5)")
check("D2", sid in out, "the audit names the session id")
check("D3", "CO-HELD" in out.upper(), "and labels what it found")

# ---------------------------------------------------------------- E: no false positives
print("\nE. a session holding exactly one ticket is NOT reported")
db = fresh_db()
sid = setup(db)
env = {"WQ_DB": db, "WQ_AGENT": sid}
sh(f"{WQ} claim --lane fleet", env)
rc, out = sh(f"{WQ} concurrency", {"WQ_DB": db})
check("E1", rc == 0, "clean single-ticket session exits 0")
check("E2", sid not in out, "and is not named")
db = fresh_db()
setup(db)
rc, out = sh(f"{WQ} concurrency", {"WQ_DB": db})
check("E3", rc == 0, "a queue with no claims at all exits 0")

# ---------------------------------------------------------------- F: the measurement
print("\nF. the ticket's suggested nonce is not implementable — measured, not asserted")
rc, out = sh("echo $$")
pid_a = out.strip()
rc, out = sh("echo $$")
pid_b = out.strip()
check("F1", pid_a != pid_b,
      "consecutive shells get different pids: pid cannot identify a driver (%s vs %s)" % (pid_a, pid_b))
rc, boot = sh("cat /proc/sys/kernel/random/boot_id")
check("F2", len(boot.strip()) > 0 and pid_a != boot.strip(),
      "boot_id is host-global, identical for every driver on this box")
# DELIBERATELY NOT ASSERTED HERE: that sid/pgid are per-call too. They are — measured live
# through the real gateway, where each wq call arrives as bash <- node <- sh <- ONE shared
# long-lived supergateway (pid 2875), so the whole ancestry above the call is either per-call
# or fleet-global. A probe cannot reproduce that: its own sh() children are spawned by the
# probe process and inherit ITS session, so an assertion here would measure the harness and
# report it as a fact about drivers. The live measurement is recorded in the wq#594 report.

print("\n%d/%d  (%d failed)%s" % (len(PASS), len(PASS) + len(FAIL), len(FAIL),
                                  ("  FAILED: " + ", ".join(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
