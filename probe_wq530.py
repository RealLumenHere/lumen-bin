#!/usr/bin/env python3
"""probe_wq530 — red/green guard for wq session liveness (wq#530).

Runs the wq CLI under test against a THROWAWAY sqlite file in a temp dir. It refuses to
start if WQ_DB would resolve anywhere near the production queue, because a probe about
losing work must not be able to lose work (wq#551's rule, applied to this lane).

Usage:  probe_wq530.py [/path/to/wq]      default: the copy next to this file
Exit 0 iff every assertion passes. Assertion ids are stable; cite them in reports.
"""
import os, sys, subprocess, sqlite3, tempfile, shutil, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WQ = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "wq")
FMT = "%Y-%m-%d %H:%M:%S"
PROD = "/home/lumen/agents/work-queue/queue.db"

TMP = tempfile.mkdtemp(prefix="wq530-")
DB = os.path.join(TMP, "queue.db")
if os.path.abspath(DB).startswith(os.path.dirname(PROD)):
    sys.exit("REFUSED: temp DB resolved inside the production queue directory")

PASS, FAIL = [], []
def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))

def wq(*args, agent=None, env=None, stdin=None):
    e = dict(os.environ)
    e["WQ_DB"] = DB
    e.pop("WQ_AGENT", None); e.pop("WQ_STRICT", None)
    if agent: e["WQ_AGENT"] = agent
    if env: e.update(env)
    p = subprocess.run([WQ] + [str(a) for a in args], capture_output=True, text=True, env=e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def db():
    c = sqlite3.connect(DB, timeout=15); c.execute("PRAGMA busy_timeout=15000"); return c

def backdate_session(sid, minutes):
    t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime(FMT)
    c = db(); c.execute("UPDATE agent_sessions SET last_seen=? WHERE session_id=?", (t, sid)); c.commit(); c.close()

def backdate_lease(tid, minutes):
    t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime(FMT)
    c = db(); c.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (t, tid)); c.commit(); c.close()

def lease_min(tid):
    c = db(); r = c.execute("SELECT lease_expires FROM tasks WHERE id=?", (tid,)).fetchone(); c.close()
    if not r or not r[0]: return None
    exp = datetime.datetime.strptime(r[0], FMT).replace(tzinfo=datetime.timezone.utc)
    return (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60.0

def sid_of(out):
    for line in out.splitlines():
        if line.startswith("REGISTERED session:"): return line.split(":", 1)[1].strip()
    return None

def add(lane, title, pri=2):
    rc, out = wq("add", "--lane", lane, "--pri", pri, "--title", title)
    return int(out.strip().split()[2])

print(f"probe_wq530 — wq under test: {WQ}\n            — throwaway db : {DB}\n")

# ---------------------------------------------------------------- A. staleness gates NAME reuse
print("A. stale sessions must not park a base name (deliverable 1)")
rc, out = wq("whoami", "--register", "zz530a"); A_a = sid_of(out)
rc, out = wq("whoami", "--register", "zz530a")
check("A1", rc != 0 and "REFUSED" in out, "fresh duplicate base name still refused (wq#537 preserved)")

rc, out = wq("whoami", "--register", "zz530b"); A_b = sid_of(out)
backdate_session(A_b, 90)                      # idle 90m, holds nothing
rc, out = wq("whoami", "--register", "zz530b"); A_b2 = sid_of(out)
check("A2", rc == 0 and A_b2 and A_b2 != A_b, "idle-90m session holding nothing frees its base name")

rc, out = wq("whoami", "--register", "zz530c"); A_c = sid_of(out)
t_held = add("zz530", "held by a quiet but working session")
rc, out = wq("claim", "--lane", "zz530", agent=A_c)
backdate_session(A_c, 90)                      # idle 90m BUT holds a live lease
rc, out = wq("whoami", "--register", "zz530c")
check("A3", rc != 0 and "REFUSED" in out,
      "idle session STILL HOLDING a live lease is never stale — this is the rabbit17 case")

rc, out = wq("whoami", "--register", "zz530b2")
b2 = sid_of(out)
backdate_session(b2, 90)
rc, out = wq("whoami", "--register", "zz530b2")
check("A4", rc == 0 and "STALE" in out and b2 in out,
      "reuse over a stale row SUCCEEDS and names the row it stepped over")

# a claim held under the BARE base name is the one residual wq#502 inversion path
rc, out = wq("whoami", "--register", "zz530d"); A_d = sid_of(out)
t_bare = add("zz530d", "claimed under a bare name")
wq("claim", "--lane", "zz530d", agent="zz530d")
backdate_session(A_d, 90)
rc, out = wq("whoami", "--register", "zz530d")
check("A5", rc != 0, "stale row is NOT freed while a claim exists under its bare base name")

# ---------------------------------------------------------------- B. provisional lease
print("\nB. a claim that never comes back must recycle in minutes, not 240 (deliverable 2)")
rc, out = wq("whoami", "--register", "zz530e"); B_e = sid_of(out)
t1 = add("zz530e", "claimed then abandoned")
rc, out = wq("claim", "--lane", "zz530e", agent=B_e)
lm = lease_min(t1)
check("B1", lm is not None and lm < 120, f"first lease is PROVISIONAL, not 240m (got {lm:.0f}m)")
check("B2", "PROVISIONAL" in out.upper(), "claim output says the lease is provisional and how to extend")

before = lease_min(t1)
rc, out = wq("heartbeat", t1, agent=B_e)
after = lease_min(t1)
check("B3", before is not None and before < 120 and after is not None and after > 200,
      f"heartbeat PROMOTES provisional -> full ttl ({before:.0f}m -> {after:.0f}m)")

rc, out = wq("whoami", "--register", "zz530f"); B_f = sid_of(out)
t2 = add("zz530f", "held by an agent that runs wq but not heartbeat")
wq("claim", "--lane", "zz530f", agent=B_f)
before = lease_min(t2)
wq("list", "--lane", "zz530f", agent=B_f)      # ANY wq call by the holder counts as life
after = lease_min(t2)
check("B4", before is not None and before < 120 and after is not None and after > 200,
      f"any holder wq call PROMOTES the provisional lease ({before:.0f}m -> {after:.0f}m)")

# a long-running lease that already exists must never be retroactively shortened
c = db(); c.execute("UPDATE tasks SET lease_expires=? WHERE id=?",
    ((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=235)).strftime(FMT), t2))
c.commit(); c.close()
wq("list", "--lane", "zz530f", agent=B_f)
lm = lease_min(t2)
check("B5", lm is not None and lm > 200, f"an existing long lease is never shortened (got {lm:.0f}m)")

rc, out = wq("whoami", "--register", "zz530g"); B_g = sid_of(out)
t3 = add("zz530g", "abandoned in its first minute")
wq("claim", "--lane", "zz530g", agent=B_g)
backdate_lease(t3, 5)                          # provisional lease expired 5m ago
rc, out = wq("whoami", "--register", "zz530h"); B_h = sid_of(out)
rc, out = wq("claim", "--lane", "zz530g", agent=B_h)
check("B6", rc == 0 and f"CLAIMED {t3}" in out, "the expired provisional lease is recycled")
check("B7", B_g in out, "the recycle is LOUD — it names the holder it took the ticket from")

# ---------------------------------------------------------------- C. explicit deregister
print("\nC. a clean exit must be able to say so (deliverable 3)")
rc, out = wq("whoami", "--register", "zz530i"); C_i = sid_of(out)
rc, out = wq("whoami", "--release", agent=C_i)
check("C1", rc == 0, "wq whoami --release deregisters the calling session")
c = db(); gone = c.execute("SELECT 1 FROM agent_sessions WHERE session_id=?", (C_i,)).fetchone(); c.close()
check("C2", gone is None, "the registry row is actually removed")
rc, out = wq("whoami", "--register", "zz530i")
check("C3", rc == 0, "the base name is immediately reusable after a clean release")
rc, out = wq("whoami", "--release", agent=C_i)
check("C4", rc == 0, "releasing an already-released session is idempotent, not an error")

rc, out = wq("whoami", "--register", "zz530j"); C_j = sid_of(out)
t4 = add("zz530j", "still held at exit time")
wq("claim", "--lane", "zz530j", agent=C_j)
rc, out = wq("whoami", "--release", agent=C_j)
# NB: `str(t4) in out` ALONE is not discriminating — a bare ticket id is a short digit string
# that argparse's own "unrecognized arguments" error can match by coincidence, which made this
# assertion flip between runs on the unfixed build. Require the refusal wording too.
check("C5", rc != 0 and "REFUSED" in out and str(t4) in out,
      "releasing while still holding a ticket REFUSES and names it — a clean exit cannot orphan work")
rc, out = wq("whoami", "--release", "--force", agent=C_j)
check("C6", rc == 0, "--force still allows deregistering a session that holds work")

# ---------------------------------------------------------------- D. staleness is visible
print("\nD. an operator must be able to SEE which registrations are dead (deliverable 4)")
rc, out = wq("whoami", "--register", "zz530k"); D_k = sid_of(out)
backdate_session(D_k, 90)
rc, out = wq("whoami", agent="zz530-observer")
check("D1", "idle=" in out, "whoami prints an idle age per registry row")
row = [l for l in out.splitlines() if D_k in l]
check("D2", bool(row) and "STALE" in row[0], "the idle row is marked STALE, not left for the reader to compute")
rc, out2 = wq("whoami", agent="zz530-observer")
fresh = [l for l in out2.splitlines() if "zz530-observer" in l]
check("D3", not any("STALE" in l for l in fresh) if fresh else True, "a live row is not marked stale")

# ---------------------------------------------------------------- E. bare-name backcompat
print("\nE. bare-name backcompat — every stale-claim release Frank did today went through this")
t5 = add("zz530l", "claimed by an unregistered legacy name")
rc, out = wq("claim", "--lane", "zz530l", agent="zz530-legacy")
check("E1", rc == 0 and "CLAIMED" in out, "an UNREGISTERED bare name can still claim")
rc, out = wq("done", t5, agent="zz530-legacy")
check("E2", rc == 0, "an unregistered bare name can still close its own claim")

t6 = add("zz530m", "claimed bare, adopted by the registered session of the same base")
wq("claim", "--lane", "zz530m", agent="zz530m")
rc, out = wq("whoami", "--register", "zz530m"); E_m = sid_of(out)
rc, out = wq("release", t6, agent=E_m)
check("E3", rc == 0, "a registered session may still adopt+release a claim held under its own bare base name")

t7 = add("zz530n", "claimed by one bare name, released by another")
wq("claim", "--lane", "zz530n", agent="zz530-owner")
rc, out = wq("release", t7, agent="zz530-other")
check("E4", rc == 0, "an unregistered caller may still release another name's claim (documented backcompat)")

t8 = add("zz530o", "same, but under WQ_STRICT")
wq("claim", "--lane", "zz530o", agent="zz530-owner2")
rc, out = wq("release", t8, agent="zz530-other2", env={"WQ_STRICT": "1"})
check("E5", rc != 0, "WQ_STRICT=1 still refuses the cross-name release")

t9 = add("zz530p", "held by a registered session, poached by another registered session")
rc, out = wq("whoami", "--register", "zz530p1"); E_p1 = sid_of(out)
rc, out = wq("whoami", "--register", "zz530p2"); E_p2 = sid_of(out)
wq("claim", "--lane", "zz530p", agent=E_p1)
rc, out = wq("done", t9, agent=E_p2)
check("E6", rc != 0 and "REFUSED" in out, "a registered session still may not close another session's claim")

# ---------------------------------------------------------------- F. the evidence base itself
print("\nF. wq keeps no activity history, so nobody can tune the threshold from data")
c = db()
have = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_activity'").fetchone()
c.close()
check("F1", have is not None, "an append-only agent_activity table exists")
if have:
    rc, out = wq("whoami", "--register", "zz530q"); F_q = sid_of(out)
    t10 = add("zz530q", "activity-logged")
    wq("claim", "--lane", "zz530q", agent=F_q)
    wq("heartbeat", t10, agent=F_q)
    wq("done", t10, agent=F_q)
    c = db()
    cmds = [r[0] for r in c.execute("SELECT cmd FROM agent_activity WHERE session_id=? ORDER BY id", (F_q,))]
    c.close()
    check("F2", {"claim", "heartbeat", "done"} <= set(cmds), f"claim/heartbeat/done are all logged (got {cmds})")
else:
    check("F2", False, "skipped — no agent_activity table")

c = db()
tcols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
scols = {r[1] for r in c.execute("PRAGMA table_info(slocks)")}
c.close()
check("F3", {"id","lane","pri","title","detail","deps","status","claimed_by","claimed_at",
             "done_at","notes","created_at","lease_expires","subsystems","gate"} <= tcols,
      "tasks keeps every column other readers (queue-status, mctl, lumen-sqlutil) select")
check("F4", {"scope","holder","acquired_at","expires_at","note"} <= scols, "slocks schema untouched")

# ---------------------------------------------------------------- summary
print(f"\nprobe_wq530: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL: print("FAILED: " + ", ".join(FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
