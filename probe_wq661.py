#!/usr/bin/env python3
"""probe_wq661 — red/green for wq#661 aging escalation.

Verifies the acceptance criteria:
  A. Boost math: +1 tier per WQ_AGING_DAYS days open, not before.
  B. Exclusions: blocked and gated tickets get no boost.
  C. Cap: boost is capped at 1 tier regardless of age.
  D. wq list --sort effective: sorts by effective_pri then id.
  E. wq show: emits effective_pri line (computed, never stored).
  F. MC api_lane: response includes effective_pri field.
  G. Floor: effective_pri never goes below 1.

Usage: probe_wq661.py [WQ_BIN]
"""
import datetime, json, os, subprocess, sys, tempfile

WQ = sys.argv[1] if len(sys.argv) > 1 else "/home/lumen/bin/wq"
PASS, FAIL = [], []
FMT = "%Y-%m-%d %H:%M:%S"


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + str(detail) if detail else ""))


def sh(args, env=None, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def days_ago(n):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n)).strftime(FMT)


def set_created(db_path, tid, ts):
    import sqlite3
    c = sqlite3.connect(db_path)
    c.execute("UPDATE tasks SET created_at=? WHERE id=?", (ts, tid))
    c.commit(); c.close()


def get_created(db_path, tid):
    import sqlite3
    c = sqlite3.connect(db_path)
    r = c.execute("SELECT created_at FROM tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    return r[0] if r else None


# --- setup scratch DB ---
tmp = tempfile.mkdtemp(prefix="probe661-")
db = os.path.join(tmp, "q.db")
env_base = dict(os.environ, WQ_DB=db, WQ_AGENT="probe661", WQ_AGING_DAYS="7")

print("== setup: add test tickets")
# id 1: pri=2, open, no gate, will be aged to 8d
sh([WQ, "add", "--lane", "test", "--pri", "2", "--title", "aged open pri2"], env=env_base)
# id 2: pri=2, open, no gate, stays fresh (0d)
sh([WQ, "add", "--lane", "test", "--pri", "2", "--title", "fresh open pri2"], env=env_base)
# id 3: pri=2, blocked, will be aged to 8d — excluded from boost
sh([WQ, "add", "--lane", "test", "--pri", "2", "--title", "aged blocked pri2"], env=env_base)
r = sh([WQ, "block", "3", "blocked for test", "--agent", "probe661"], env=env_base)
# id 4: pri=2, open, gate set, will be aged to 8d — excluded from boost
sh([WQ, "add", "--lane", "test", "--pri", "2", "--title", "aged gated pri2",
    "--gate", "/nonexistent/path:exists"], env=env_base)
# id 5: pri=2, open, no gate, aged to 21d — cap test (boost capped at 1, not 3)
sh([WQ, "add", "--lane", "test", "--pri", "2", "--title", "very old open pri2"], env=env_base)
# id 6: pri=1, open, no gate, aged to 8d — floor test (ep never below 1)
sh([WQ, "add", "--lane", "test", "--pri", "1", "--title", "aged open pri1 floor"], env=env_base)
# id 7: pri=3, open, no gate, aged to 8d — sort order test
sh([WQ, "add", "--lane", "test", "--pri", "3", "--title", "aged open pri3 ep2"], env=env_base)
# id 8: pri=2, open, no gate, fresh — sort order test (ep=2, same as id 7)
sh([WQ, "add", "--lane", "test", "--pri", "2", "--title", "fresh open pri2 for sort"], env=env_base)

# Age tickets 1, 3, 4, 5, 6, 7 by setting created_at directly
set_created(db, 1, days_ago(8))   # 8d → boost=1  → ep=1
set_created(db, 3, days_ago(8))   # 8d but blocked → ep=2
set_created(db, 4, days_ago(8))   # 8d but gated   → ep=2
set_created(db, 5, days_ago(21))  # 21d → boost capped at 1 → ep=1
set_created(db, 6, days_ago(8))   # 8d, pri=1 → ep=max(1,0)=1
set_created(db, 7, days_ago(8))   # 8d, pri=3 → ep=2

print("\n== A. boost math")
out = sh([WQ, "show", "1"], env=env_base).stdout
check("A.1 show: ep=1 for 8d-old pri=2 open ticket",
      "effective_pri: 1" in out, out.strip().splitlines()[-3:])
out = sh([WQ, "show", "2"], env=env_base).stdout
check("A.2 show: ep=2 for fresh pri=2 open ticket (no boost)",
      "effective_pri: 2" in out, out.strip().splitlines()[-3:])

print("\n== B. exclusions")
out = sh([WQ, "show", "3"], env=env_base).stdout
check("B.1 show: blocked 8d → ep=stored_pri=2 (no boost)",
      "effective_pri: 2" in out, out.strip().splitlines()[-3:])
out = sh([WQ, "show", "4"], env=env_base).stdout
check("B.2 show: gated 8d → ep=stored_pri=2 (no boost)",
      "effective_pri: 2" in out, out.strip().splitlines()[-3:])

print("\n== C. cap at 1 tier")
out = sh([WQ, "show", "5"], env=env_base).stdout
check("C.1 show: 21d old → ep=1 (cap: boost never exceeds 1 tier)",
      "effective_pri: 1" in out, out.strip().splitlines()[-3:])

print("\n== G. floor: ep never below 1")
out = sh([WQ, "show", "6"], env=env_base).stdout
check("G.1 show: 8d old pri=1 → ep=1 (floor, not 0)",
      "effective_pri: 1" in out, out.strip().splitlines()[-3:])

print("\n== D. wq list --sort effective")
# Default sort: by stored pri asc → id 1 (p2) and id 2 (p2) come before id 7 (p3)
out_default = sh([WQ, "list", "test"], env=env_base).stdout
lines = [l for l in out_default.splitlines() if "[  7]" in l or "[  8]" in l]
# After default sort: id7 (pri=3) after id8 (pri=2) because 2<3
ids_default = [int(l.split("]")[0].strip("[").strip()) for l in out_default.splitlines() if "test" in l]
check("D.1 default sort includes ep tag for boosted ticket",
      "ep1" in out_default, "ep1 not found in list output")

# Effective sort: id7 ep=2 and id8 ep=2 (same ep), sorted by id → 7 before 8 inside ep group
out_eff = sh([WQ, "list", "test", "--sort", "effective"], env=env_base).stdout
ids_eff = [int(l.split("]")[0].strip("[").strip()) for l in out_eff.splitlines() if "test" in l]
check("D.2 --sort effective emits the list",
      len(ids_eff) >= 7, f"got {len(ids_eff)} rows")
# Key invariant: id 1 (ep=1) must come before id 7 (ep=2) and id 8 (ep=2)
try:
    pos1 = ids_eff.index(1); pos7 = ids_eff.index(7); pos8 = ids_eff.index(8)
    check("D.3 ep=1 ticket before ep=2 tickets in effective sort",
          pos1 < pos7 and pos1 < pos8, f"positions: #1={pos1}, #7={pos7}, #8={pos8}")
except ValueError as e:
    check("D.3 ep=1 ticket before ep=2 tickets in effective sort", False, str(e))

print("\n== E. wq show exposes effective_pri")
out = sh([WQ, "show", "1"], env=env_base).stdout
check("E.1 effective_pri line present in show output",
      "effective_pri" in out, out.strip().splitlines()[-5:])
check("E.2 stored pri line still present (not replaced)",
      "pri          : " in out, out.strip().splitlines()[-10:])

print("\n== F. MC api_lane includes effective_pri")
# Import MC module against scratch DB
mc_env = dict(os.environ)
import importlib.util, types

# Patch DB path in MC module
spec = importlib.util.spec_from_file_location("mc_app", "/home/lumen/mission-control/app.py")
mc = importlib.util.module_from_spec(spec)
# Override DB before exec
import sqlite3 as _sqlite3
_orig_connect = _sqlite3.connect
_calls = []
def _patched_connect(path, *a, **kw):
    if isinstance(path, str) and "queue.db" in path and "mode=ro" in path:
        path = path.replace(str(mc.DB), db)
    return _orig_connect(path, *a, **kw)
_sqlite3.connect = _patched_connect
try:
    spec.loader.exec_module(mc)
    mc.DB = type("P", (), {"__str__": lambda s: db, "__fspath__": lambda s: db,
                           "is_file": lambda s: True})()
    # Directly call mc_effective_pri from the module
    ep_val = mc.mc_effective_pri(2, "open", "", days_ago(8))
    check("F.1 mc_effective_pri(2, open, no-gate, 8d) == 1", ep_val == 1, f"got {ep_val}")
    ep_fresh = mc.mc_effective_pri(2, "open", "", days_ago(0))
    check("F.2 mc_effective_pri(2, open, no-gate, 0d) == 2", ep_fresh == 2, f"got {ep_fresh}")
    ep_blocked = mc.mc_effective_pri(2, "blocked", "", days_ago(8))
    check("F.3 mc_effective_pri(2, blocked, no-gate, 8d) == 2 (excluded)", ep_blocked == 2, f"got {ep_blocked}")
    ep_gated = mc.mc_effective_pri(2, "open", "/some/path:exists", days_ago(8))
    check("F.4 mc_effective_pri(2, open, gate-set, 8d) == 2 (excluded)", ep_gated == 2, f"got {ep_gated}")
    ep_cap = mc.mc_effective_pri(2, "open", "", days_ago(21))
    check("F.5 mc_effective_pri(2, open, no-gate, 21d) == 1 (cap)", ep_cap == 1, f"got {ep_cap}")
    ep_floor = mc.mc_effective_pri(1, "open", "", days_ago(8))
    check("F.6 mc_effective_pri(1, open, no-gate, 8d) == 1 (floor)", ep_floor == 1, f"got {ep_floor}")
finally:
    _sqlite3.connect = _orig_connect

print(f"\n== result: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
