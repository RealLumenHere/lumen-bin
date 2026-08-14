#!/usr/bin/env python3
"""probe_wq582 — red/green probe for `wq claim --id <n>` (claim a SPECIFIC ticket).
Usage: probe_wq582.py [wq_path] [slock_path]   (defaults: alongside this script)
Isolated: own tmpdir queue.db via WQ_DB; never touches production.
RED on a pre-582 build: argparse rejects --id outright (rc=2), so every --id section fails.
GREEN sections on BOTH builds by design: D (lane-scan path unchanged)."""
import os, sys, subprocess, tempfile, shutil, sqlite3, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WQ = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "wq")
SL = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "slock")
TMP = tempfile.mkdtemp(prefix="wq582-")
DB = os.path.join(TMP, "queue.db")

BASE = {k: v for k, v in os.environ.items() if k != "WQ_AGENT"}
BASE["WQ_DB"] = DB
BASE["WQ_NTFY_BIN"] = "/bin/true"

P = F = 0
def check(name, ok, detail=""):
    global P, F
    P += ok; F += (not ok)
    print(("  ok  " if ok else "  FAIL") + f" {name}" + (f"  [{detail}]" if detail and not ok else ""))

def run(tool, args, agent=None):
    e = dict(BASE)
    if agent is not None: e["WQ_AGENT"] = agent
    return subprocess.run([sys.executable, tool] + args, capture_output=True, text=True, env=e)

def wq(args, agent=None): return run(WQ, args, agent)
def sl(args, agent=None): return run(SL, args, agent)

def reg(base):
    r = wq(["whoami", "--register", base])
    for ln in r.stdout.splitlines():
        if ln.startswith("export WQ_AGENT="):
            return ln.split("=", 1)[1].strip()
    sys.exit(f"register failed for {base}: {r.stdout} {r.stderr}")

def add(title, lane="fleet", pri=2, extra=None):
    r = wq(["add", "--lane", lane, "--pri", str(pri), "--title", title] + (extra or []))
    if "added task " not in r.stdout:
        sys.exit(f"add failed: {r.stdout} {r.stderr}")
    return int(r.stdout.split("added task ")[1].split()[0])

def q(sql, *a):
    c = sqlite3.connect(DB); r = c.execute(sql, a).fetchall(); c.close(); return r

print("A. claim --id lands a NON-HEAD ticket, with the out-of-order NOTICE")
sid1 = reg("p582a")
t_head = add("A head p2")
t_mid  = add("A mid p2")
t_tail = add("A tail p3", pri=3)
r = wq(["claim", "--id", str(t_tail)], agent=sid1)
check("A1 claim --id succeeds", r.returncode == 0 and f"CLAIMED {t_tail}" in r.stdout,
      f"rc={r.returncode} {r.stdout[:160]} {r.stderr[:120]}")
check("A2 NOTICE names the lane head", f"head is #{t_head}" in r.stdout and "BY ID" in r.stdout, r.stdout)
check("A3 DB shows the claim", q("SELECT status,claimed_by FROM tasks WHERE id=?", t_tail)
      == [("claimed", sid1)])
check("A4 head untouched", q("SELECT status FROM tasks WHERE id=?", t_head) == [("open",)])

print("B. claim --id of the head itself: no notice")
r = wq(["claim", "--id", str(t_head)], agent=sid1)
check("B1 head claim by id ok", r.returncode == 0 and f"CLAIMED {t_head}" in r.stdout, r.stdout)
check("B2 no out-of-order NOTICE", "out-of-order" not in r.stdout, r.stdout)

print("C. EVERY guard holds — none relaxed, no --force")
sid2 = reg("p582b")
r = wq(["claim", "--id", str(t_head)], agent=sid2)
check("C1 live claim of another session REFUSED rc=1",
      r.returncode == 1 and "held by" in r.stdout and sid1 in r.stdout, r.stdout)
r = wq(["claim", "--id", "9999"], agent=sid2)
check("C2 nonexistent id REFUSED rc=1", r.returncode == 1 and "no such task" in r.stdout, r.stdout)
t_draft = add("[DRAFT] C draft")
r = wq(["claim", "--id", str(t_draft)], agent=sid2)
check("C3 [DRAFT] REFUSED rc=1", r.returncode == 1 and "[DRAFT]" in r.stdout, r.stdout)
t_blk = add("C to-block")
wq(["block", str(t_blk), "probe"], agent=sid2)
r = wq(["claim", "--id", str(t_blk)], agent=sid2)
check("C4 blocked REFUSED rc=1", r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout)
t_gate = add("C gated", extra=["--gate", os.path.join(TMP, "never-exists")])
r = wq(["claim", "--id", str(t_gate)], agent=sid2)
check("C5 unmet gate REFUSED rc=1", r.returncode == 1 and "gate:" in r.stdout, r.stdout)
t_dep = add("C dep-blocked", extra=["--deps", str(t_head)])
r = wq(["claim", "--id", str(t_dep)], agent=sid2)
check("C6 unmet deps REFUSED rc=1", r.returncode == 1 and "deps not met" in r.stdout, r.stdout)
t_sub = add("C sub-locked", extra=["--subsystems", "wq-tooling"])
sl(["acquire", "wq-tooling"], agent=sid1)
r = wq(["claim", "--id", str(t_sub)], agent=sid2)
check("C7 foreign-held subsystem REFUSED rc=1",
      r.returncode == 1 and "subsystem wq-tooling held by" in r.stdout, r.stdout)
wq(["done", str(t_head), "--notes", "probe"], agent=sid1)
r = wq(["claim", "--id", str(t_head)], agent=sid2)
check("C8 done ticket REFUSED rc=1", r.returncode == 1 and "already done" in r.stdout, r.stdout)
r = wq(["claim", "--id", str(t_mid), "--force"], agent=sid2)
check("C9 --force does not exist (argparse rc=2)",
      r.returncode == 2 and "unrecognized" in r.stderr, f"rc={r.returncode} {r.stderr[:120]}")
t_user = add("C user-lane ticket", lane="user")
r = wq(["claim", "--id", str(t_user)], agent=sid2)
check("C10 user-lane ticket by id REFUSED rc=1",
      r.returncode == 1 and "Frank-only" in r.stdout, r.stdout)

print("D. lane-scan path unchanged (green on BOTH builds)")
r = wq(["claim", "--lane", "fleet"], agent=sid2)
check("D1 plain claim still hands the lane head", r.returncode == 0 and f"CLAIMED {t_mid}" in r.stdout,
      f"rc={r.returncode} {r.stdout[:160]}")
check("D2 no NOTICE on a lane-scan claim", "out-of-order" not in r.stdout, r.stdout)

print("E. retirement gate composes: a retired session cannot claim --id")
sid3 = reg("p582c")
wq(["whoami", "--release"], agent=sid3)
t_e = add("E open ticket")
r = wq(["claim", "--id", str(t_e)], agent=sid3)
check("E1 retired claim --id refused rc=3", r.returncode == 3 and "STOP" in r.stdout,
      f"rc={r.returncode} {r.stdout[:160]}")
check("E2 ticket untouched", q("SELECT status FROM tasks WHERE id=?", t_e) == [("open",)])

print("F. target with an EXPIRED lease: reclaimed loudly, then claimed, one call")
t_f = add("F expired-lease target")
past = (datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
c = sqlite3.connect(DB)
c.execute("UPDATE tasks SET status='claimed', claimed_by='ghost', claimed_at=?, "
          "lease_expires=?, lease_ttl=240 WHERE id=?", (past, past, t_f))
c.commit(); c.close()
r = wq(["claim", "--id", str(t_f)], agent=sid2)
check("F1 reclaim named the dead holder", f"RECLAIMED #{t_f} from ghost" in r.stdout, r.stdout)
check("F2 then claimed in the same call", f"CLAIMED {t_f}" in r.stdout and r.returncode == 0, r.stdout)
check("F3 DB agrees", q("SELECT status,claimed_by FROM tasks WHERE id=?", t_f) == [("claimed", sid2)])

print("G. --lane mismatch: ticket's lane governs, with a NOTICE")
t_g = add("G pokemon-lane ticket", lane="pokemon")
r = wq(["claim", "--lane", "fleet", "--id", str(t_g)], agent=sid2)
check("G1 claim succeeds in the ticket's own lane",
      r.returncode == 0 and f"CLAIMED {t_g}" in r.stdout, f"rc={r.returncode} {r.stdout[:160]}")
check("G2 mismatch NOTICE printed", "ticket's lane governs" in r.stdout, r.stdout)

print(f"\n{'='*44}\nprobe_wq582: {P} passed / {F} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
