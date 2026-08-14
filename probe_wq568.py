#!/usr/bin/env python3
"""probe_wq568 — red/green for STOP-MEANS-STOP session retirement (wq + slock).

Usage: probe_wq568.py [WQ_PATH [SLOCK_PATH]]   (defaults: ./wq ./slock beside this file)

Fully isolated: WQ_DB points at a throwaway queue.db, WQ_NTFY_BIN at a stub that appends to a
file — so the notification is PROVED to fire, not asserted from the source. Never touches the
production DB, never sends a real ntfy. Each section claims from its OWN lane so a ticket a
retirement reverted to open cannot be picked up by a later section's claim.

RED baseline: run this against the pre-wq#568 build — `retire` is an unknown subcommand there,
so section A dies immediately and everything downstream cascades. That is the red, on purpose.
"""
import os, re, sqlite3, subprocess, sys, tempfile, shutil, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WQ = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "wq")
SLOCK = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "slock")
TMP = tempfile.mkdtemp(prefix="wq568-")
DB = os.path.join(TMP, "queue.db")
NTFY_LOG = os.path.join(TMP, "ntfy.log")
NTFY_STUB = os.path.join(TMP, "ntfy-stub")
with open(NTFY_STUB, "w") as f:
    f.write("#!/bin/bash\necho \"$@\" >> " + NTFY_LOG + "\n")
os.chmod(NTFY_STUB, 0o755)
BANNER = "STOP — SESSION RETIRED"
PASS, FAIL = [], []

def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))

def run(tool, args, agent=None, extra_env=None):
    e = dict(os.environ, WQ_DB=DB, WQ_NTFY_BIN=NTFY_STUB)
    e.pop("WQ_AGENT", None)
    if agent is not None: e["WQ_AGENT"] = agent
    if extra_env: e.update(extra_env)
    p = subprocess.run(["python3", tool] + args, capture_output=True, text=True,
                       env=e, timeout=60)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def wq(args, agent=None, extra_env=None): return run(WQ, args, agent, extra_env)
def slock(args, agent=None): return run(SLOCK, args, agent)

def register(base):
    rc, out = wq(["whoami", "--register", base])
    m = re.search(r"export WQ_AGENT=(\S+)", out)
    if rc != 0 or not m:
        print(f"SETUP FAILURE: could not register '{base}' (rc={rc}):\n{out}")
        shutil.rmtree(TMP, ignore_errors=True); sys.exit(1)
    return m.group(1)

def add(lane, title):
    rc, out = wq(["add", "--lane", lane, "--title", title])
    m = re.search(r"added task (\d+)", out)
    if not m:
        print(f"SETUP FAILURE: add failed (rc={rc}): {out}")
        shutil.rmtree(TMP, ignore_errors=True); sys.exit(1)
    return int(m.group(1))

def db(q, args=()):
    c = sqlite3.connect(DB); r = c.execute(q, args).fetchall(); c.close(); return r

def db1(q, args=(), default=None):
    """First cell or default. Tolerant of a missing table so the RED baseline against the
    pre-wq#568 build COUNTS its failures instead of crashing on 'no such table'."""
    try:
        r = db(q, args)
        return r[0][0] if r else default
    except sqlite3.Error:
        return default

def dbrow(q, args=(), default=()):
    try:
        r = db(q, args)
        return r[0] if r else default
    except sqlite3.Error:
        return default

def ntfy_len():
    try: return os.path.getsize(NTFY_LOG)
    except OSError: return 0

print(f"probe_wq568 — wq under test: {WQ}\n" + " " * 14 + f"slock under test: {SLOCK}\n")

# ---- A. manual retire releases claims + slocks atomically (deliverables 1+2) --
print("A. `wq retire` reverts the session's claims to open, drops its slocks, plants the marker")
sidA = register("probeA")
tA = add("pA", "probe A ticket")
rc, out = wq(["claim", "--lane", "pA"], agent=sidA)
check("A0", rc == 0 and f"CLAIMED {tA}" in out, "setup: session A holds a claim")
rc, out = slock(["acquire", "probeA-scope", "--note", "probe"], agent=sidA)
check("A1", rc == 0 and "ACQUIRED" in out, "setup: session A holds a slock")
rc, out = wq(["retire", sidA, "--reason", "probe manual retire"])
check("A2", rc == 0 and "RETIRED" in out and str(tA) in out,
      "retire succeeded and named the reverted ticket")
st = dbrow("SELECT status, claimed_by FROM tasks WHERE id=?", (tA,), ("MISSING", "?"))
check("A3", st[0] == "open" and (st[1] is None or st[1] == ""),
      f"ticket reverted to open with no holder  [status={st[0]} claimed_by={st[1]!r}]")
check("A4", db1("SELECT COUNT(*) FROM slocks WHERE holder=?", (sidA,), -1) == 0,
      "session A's slocks are gone")
check("A5", db1("SELECT kind FROM retired_sessions WHERE session_id=?", (sidA,), "ABSENT") == "manual",
      "retired marker persisted with kind=manual")
rc, out = wq(["whoami"])
check("A6", "RETIRED (manual)" in out, "whoami shows the RETIRED row distinctly (deliverable 5)")

# ---- B. retired session refused on claim AND on read-only list (deliverable 1) --
print("\nB. a retired session is refused everywhere, read-only included, and the refusal is logged")
rc, out = wq(["list"], agent=sidA)
check("B1", rc == 3 and BANNER in out, "read-only `list` refused with the STOP banner, exit 3")
rc, out = wq(["claim", "--lane", "pA"], agent=sidA)
check("B2", rc == 3 and BANNER in out and "CLAIMED" not in out, "`claim` refused — nothing handed out")
n = db1("SELECT COUNT(*) FROM agent_activity WHERE session_id=? AND cmd LIKE 'RETIRED-REFUSED%'",
        (sidA,), 0)
check("B3", n >= 2, f"refusals logged in agent_activity — the metric Frank asked for  [{n} rows]")

# ---- C. slock refuses a retired session, fail-closed (deliverable 3) ---------
print("\nC. slock acquire/extend DENIED for a retired session; release deliberately stays open")
rc, out = slock(["acquire", "probeC-scope"], agent=sidA)
check("C1", rc == 1 and "DENIED" in out and "RETIRED" in out,
      "acquire denied, names the retirement — this is what stops a respawn landing code")
rc, out = slock(["extend", "probeC-scope"], agent=sidA)
check("C2", rc == 1 and "RETIRED" in out,
      "extend denied on the RETIRED gate specifically (not the ordinary not-held refusal)")
rc, out = slock(["release", "probeC-scope"], agent=sidA)
check("C3", rc == 0, "release NOT gated — a retired identity shedding state is desirable")

# ---- D. deregistered -> auto-retire on next call (deliverable 4, the metric) --
print("\nD. the auto-continue signature: any wq call after a clean deregistration auto-retires")
sidD = register("probeD")
rc, out = wq(["whoami", "--release"], agent=sidD)
check("D0", rc == 0 and "DEREGISTERED" in out, "setup: clean exit via whoami --release")
before = ntfy_len()
rc, out = wq(["list"], agent=sidD)
check("D1", rc == 3 and BANNER in out and "auto-continue signature" in out,
      "first call after deregistration hits the wall and says WHY")
check("D2", db1("SELECT kind FROM retired_sessions WHERE session_id=?", (sidD,), "ABSENT") == "auto-respawn",
      "marker upgraded deregistered -> auto-respawn")
log = open(NTFY_LOG).read() if os.path.exists(NTFY_LOG) else ""
check("D3", ntfy_len() > before and "respawn" in log and sidD in log,
      "ntfy FIRED on the auto-retire (stub called; deliverable 5) — proved, not asserted")
rc, out = wq(["show", "1"], agent=sidD)
check("D4", rc == 3 and BANNER in out, "second call still refused (now the already-retired path)")

# ---- E. a LIVE registered session is unaffected (the assertion that matters most)
print("\nE. a live registered session sails through everything — the gate must cost the living nothing")
sidE = register("probeE")
tE = add("pE", "probe E ticket")
before = ntfy_len()
rc, out = wq(["claim", "--lane", "pE"], agent=sidE)
check("E1", rc == 0 and f"CLAIMED {tE}" in out and BANNER not in out, "claim works, no banner")
rc, out = wq(["list", "pE"], agent=sidE)
check("E2", rc == 0 and BANNER not in out, "read-only list works")
rc, out = wq(["heartbeat", str(tE)], agent=sidE)
check("E3", rc == 0 and "heartbeat" in out, "heartbeat works")
rc, out = slock(["acquire", "probeE-scope"], agent=sidE)
check("E4", rc == 0 and "ACQUIRED" in out, "slock acquire works")
rc, out = wq(["done", str(tE), "--notes", "probe"], agent=sidE)
check("E5", rc == 0 and "done" in out, "done works")
check("E6", ntfy_len() == before, "no ntfy fired for any of it — alerts are for retirements only")
slock(["release", "probeE-scope"], agent=sidE)

# ---- F. unregistered legacy bare name still works (wq#530 backcompat, Frank-pinned)
print("\nF. an unregistered legacy name is untouched by the gate")
tF = add("pF", "probe F ticket")
rc, out = wq(["claim", "--lane", "pF"], agent="legacy-oldname")
check("F1", rc == 0 and f"CLAIMED {tF}" in out and BANNER not in out,
      "legacy bare name claims fine (with the usual UNREGISTERED warning)")
rc, out = wq(["release", str(tF)], agent="legacy-oldname")
check("F2", rc == 0, "and releases fine — backcompat holds")

# ---- G. budget backstops: age + closed-ticket count, env-tunable (deliverable 4b)
print("\nG. budget backstops fire, release holdings, alert — and the env knobs actually steer them")
sidG = register("probeG")
tG = add("pG", "probe G ticket")
rc, out = wq(["claim", "--lane", "pG"], agent=sidG)
check("G0", rc == 0 and f"CLAIMED {tG}" in out, "setup: G holds a claim before ageing out")
old = (datetime.datetime.now(datetime.timezone.utc)
       - datetime.timedelta(minutes=500)).strftime("%Y-%m-%d %H:%M:%S")
c = sqlite3.connect(DB)
c.execute("UPDATE agent_sessions SET first_seen=? WHERE session_id=?", (old, sidG))
c.commit(); c.close()
before = ntfy_len()
rc, out = wq(["list"], agent=sidG)
check("G1", rc == 3 and BANNER in out and "auto-age" in out,
      "session past WQ_SESSION_MAX_AGE auto-retires on its next call")
st = db1("SELECT status FROM tasks WHERE id=?", (tG,), "MISSING")
check("G2", st == "open", f"its claim reverted to open on the trip  [status={st}]")
log = open(NTFY_LOG).read() if os.path.exists(NTFY_LOG) else ""
check("G3", ntfy_len() > before and "auto-age" in log, "ntfy fired for the age trip")
sidH = register("probeH")
c = sqlite3.connect(DB)
for i in range(8):
    c.execute("INSERT INTO tasks(lane,pri,title,status,claimed_by,created_at,done_at) "
              "VALUES('pH',3,?, 'done', ?, datetime('now'), datetime('now'))",
              (f"probe H done {i}", sidH))
c.commit(); c.close()
rc, out = wq(["list"], agent=sidH)
check("G4", rc == 3 and BANNER in out and "auto-tickets" in out,
      "8 closed claims trips WQ_SESSION_MAX_TICKETS")
sidI = register("probeI")
c = sqlite3.connect(DB)
c.execute("UPDATE agent_sessions SET first_seen=? WHERE session_id=?", (old, sidI))
c.commit(); c.close()
rc, out = wq(["list"], agent=sidI, extra_env={"WQ_SESSION_MAX_AGE": "100000"})
check("G5", rc == 0 and BANNER not in out,
      "NEGATIVE CONTROL: raising WQ_SESSION_MAX_AGE spares the same-aged session — the knob "
      "is read, the trip is threshold-driven, not decoration")

# ---- cleanup ---------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nprobe_wq568: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
