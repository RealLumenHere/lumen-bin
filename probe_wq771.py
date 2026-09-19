#!/usr/bin/env python3
"""probe_wq771 — lumen-pr refuses to land over uncommitted work in the target tree.

Section D extends this to wq#792: an un-pushed local COMMIT on the target's default
branch is the same loss class as uncommitted state, and dirty_target_guard now checks
both.

WHAT THIS ASSERTS, and what it does not, stated first because the sections are not equal:

  DISCRIMINATORS (B) drive the REAL `lumen-pr land` end to end against a fake forge and
  fail on the pre-change build for a behavioural reason. B1/B2 are the pair that matters:
  same repo, same dirty file, same PR — the pre-change build reaches the merge endpoint
  and exits 0, the post-change build refuses before it and never calls merge. That IS the
  2026-08-21 dispatcher stall, reproduced: a file edited and never committed in the live
  tree, a worktree branched from origin/main that by definition never saw the edit, and a
  merge that git reports no conflict for because there is nothing committed to conflict
  with.

  REGRESSION GUARDS (A, C) pass on BOTH builds by design. A pins premises the guard is
  built on (git lists the main work tree first; the merge-base diff names the file). C
  pins behaviour the change must not move. Neither proves the guard works.

NOTHING HERE TOUCHES A LIVE TREE, A LIVE FORGE, OR THE LIVE LOCK. Every repo is made
under a mkdtemp; the forge is a throwaway HTTP server on 127.0.0.1; slock is a stub;
LUMEN_PR_WORKTREE_ROOT and GITEA_TOKEN_FILE point into the mkdtemp. The one thing read
from outside is the pre-change lumen-pr, via `git show origin/main:lumen-pr`.
"""
import json, os, re, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BIN = os.path.dirname(os.path.abspath(__file__))
LPR = os.path.join(BIN, "lumen-pr")
TMP = tempfile.mkdtemp(prefix="wq771-")
SLUG = "probe/repo"
PRE = "/api/v1/repos/" + SLUG
PASS = FAIL = 0
FAILED = []
SKIPPED = []


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
    for k in ("WQ_AGENT", "GITEA_URL", "GITHUB_TOKEN"):
        e.pop(k, None)
    if env:
        e.update(env)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=e, cwd=cwd)
    return p.returncode, p.stdout + p.stderr


def git(repo, *args):
    return sh("git -C %s %s" % (repo, " ".join(args)))


# --- fake forge -------------------------------------------------------------
HITS = []
PR = {}
FILES = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        HITS.append(("GET", p))
        if p == PRE + "/pulls/1":
            return self._send(200, PR)
        if p.startswith(PRE + "/commits/") and p.endswith("/statuses"):
            return self._send(200, [])          # no CI configured -> land on review alone
        if p == PRE + "/pulls/1/commits":
            return self._send(200, [])
        if p == PRE + "/pulls/1/files":
            return self._send(200, [{"filename": f} for f in FILES])
        return self._send(404, {"message": "no route " + p})

    def do_POST(self):
        p = urlparse(self.path).path
        HITS.append(("POST", p))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if p == PRE + "/pulls/1/merge":
            return self._send(200, {})
        return self._send(404, {"message": "no route " + p})

    do_PUT = do_POST


SRV = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
ORIGIN = f"http://127.0.0.1:{PORT}/{SLUG}.git"


def merged_yet():
    return any(m == "POST" and p.endswith("/merge") for m, p in HITS)


# --- fixtures ---------------------------------------------------------------
SLOCK = os.path.join(TMP, "slock-stub")
with open(SLOCK, "w") as f:
    f.write('#!/usr/bin/env bash\ncase "${1:-}" in who) echo "(no active locks)";; esac\nexit 0\n')
os.chmod(SLOCK, 0o755)
TOKEN = os.path.join(TMP, "gitea.token")
open(TOKEN, "w").write("probe-not-a-real-token\n")

ENV = {"LUMEN_PR_SCOPE_CHECK": "0",           # documented in lumen-pr "for the probe ONLY"
       "LUMEN_PR_SLOCK": SLOCK,               # never take the fleet's real git lock
       "LUMEN_PR_WORKTREE_ROOT": os.path.join(TMP, "wtroot"),
       "GITEA_TOKEN_FILE": TOKEN}


def env(**kw):
    e = dict(ENV)
    e.update({k: str(v) for k, v in kw.items()})
    return e


def forge_repo(name):
    """Primary tree + a linked worktree on a branch cut from origin/main, exactly the
    shape of the incident: the lander is in the worktree, the dirty file is in the tree.
    origin points at the fake forge, so the API resolves and git never needs to reach it."""
    work = os.path.join(TMP, name)
    os.makedirs(work)
    sh(f"git init -q -b main {work}")
    git(work, "config user.email probe@lumen.local")
    git(work, "config user.name probe")
    open(os.path.join(work, "config.json"), "w").write('{"lanes": {}}\n')
    open(os.path.join(work, "README.md"), "w").write("seed\n")
    git(work, "add -A")
    git(work, "commit -qm 'chore: seed'")
    git(work, "update-ref refs/remotes/origin/main HEAD")
    git(work, "symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main")
    git(work, f"remote add origin {ORIGIN}")
    wt = os.path.join(TMP, name + "-wt")
    git(work, f"worktree add -q -b fix/probe {wt} origin/main")
    return work, wt


def branch_commit(wt, path, body, subject="fix(probe): change a path"):
    full = os.path.join(wt, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(body)
    git(wt, f"add -- {path}")
    git(wt, f"commit -qm '{subject}'")
    return git(wt, "rev-parse HEAD")[1].strip()


def land(wt, build=None, **kw):
    HITS.clear()
    return sh(f"{build or LPR} land {wt} 1", env=env(**kw))


def real_repo(name):
    """A real bare origin, so `start`/`commit` can fetch. No forge involved."""
    bare = os.path.join(TMP, name + ".git")
    work = os.path.join(TMP, name)
    sh(f"git init -q --bare -b main {bare}")
    sh(f"git clone -q {bare} {work}")
    git(work, "config user.email probe@lumen.local")
    git(work, "config user.name probe")
    open(os.path.join(work, "config.json"), "w").write('{"lanes": {}}\n')
    git(work, "add -A")
    git(work, "commit -qm 'chore: seed'")
    git(work, "push -q origin main")
    git(work, "remote set-head origin main")
    return work


# --- the pre-change build ---------------------------------------------------
# origin/main first (exact, and independent of whether a .bak was ever taken), then the
# newest .bak. Whichever it is, it is REJECTED if it already carries the change: once
# this lands, origin/main is no longer a control, and a control that silently became the
# treatment would turn B2 green while proving nothing.
OLD = None
OLD_SRC = ""
cands = []
rc, out = sh(f"git -C {BIN} show origin/main:lumen-pr")
if rc == 0:
    cands.append(("origin/main:lumen-pr", out))
for d in (BIN, "/home/lumen/bin"):
    try:
        for b in sorted((f for f in os.listdir(d) if f.startswith("lumen-pr.bak.")), reverse=True):
            cands.append((os.path.join(d, b), open(os.path.join(d, b), errors="ignore").read()))
    except OSError:
        pass
for label, src in cands:
    if "wq#771" not in src and "dirty_target_guard" not in src:
        olddir = os.path.join(TMP, "oldbuild")
        os.makedirs(olddir, exist_ok=True)
        OLD = os.path.join(olddir, "lumen-pr")
        open(OLD, "w").write(src)
        os.chmod(OLD, 0o755)
        shutil.copy2(os.path.join(BIN, "lumen-github-identity.sh"), olddir)
        OLD_SRC = label
        break

print(f"probe_wq771 — under test: {LPR}\n              pre-change : {OLD_SRC or 'UNAVAILABLE'}"
      f"\n              fake forge : http://127.0.0.1:{PORT}\n              scratch    : {TMP}\n")

# ---------------------------------------------------------------------------
print("A. PREMISE PINS — REGRESSION GUARDS, green on both builds, prove nothing about the change")

pa, wa = forge_repo("premise")
open(os.path.join(pa, "config.json"), "w").write('{"lanes": {"build": {}}}\n')   # the uncommitted lane
sha_a = branch_commit(wa, "config.json", '{"lanes": {}, "cloud_ceiling": 3}\n')

check("A1 git lists the MAIN work tree first (primary_worktree relies on it)",
      git(wa, "worktree list --porcelain")[1].splitlines()[0] == f"worktree {pa}")
base = git(wa, f"merge-base origin/main {sha_a}")[1].strip()
check("A2 merge-base..head names the PR's paths without a forge round trip",
      git(wa, f"diff --name-only {base} {sha_a}")[1].split() == ["config.json"])
check("A3 the incident's premise: the edit is uncommitted, so git has NOTHING to conflict with",
      "config.json" in git(pa, "diff --name-only")[1]
      and git(pa, "log -S'\"build\"' --oneline")[1].strip() == "")
check("A4 landing from a LINKED worktree never touches the primary tree, so nothing warns today",
      git(pa, "status --porcelain")[1].strip().startswith("M"))

# ---------------------------------------------------------------------------
print("\nB. DISCRIMINATORS — real `lumen-pr land`, real repo, fake forge. RED on the pre-change build")

p1, w1 = forge_repo("incident")
open(os.path.join(p1, "config.json"), "w").write('{"lanes": {"build": {"enabled": true}}}\n')
sha1 = branch_commit(w1, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR.update({"state": "open", "merged": False, "draft": False, "mergeable": True,
           "title": "fix(probe): change a path", "head": {"sha": sha1, "ref": "fix/probe"}})
FILES[:] = ["config.json"]

rc, out = land(w1)
check("B1 post-change: land REFUSES, names the file, cites the ticket",
      rc != 0 and "config.json" in out and "wq#771" in out and "refusing to land" in out,
      f"rc={rc} out={out[-300:]!r}")
check("B1b post-change: the merge endpoint was NEVER called", not merged_yet(),
      str(HITS))
check("B1c post-change: it refuses BEFORE the git lock and the CI wait",
      not any("statuses" in p for _, p in HITS), str(HITS))

if OLD:
    rc_o, out_o = land(w1, build=OLD)
    check("B2 pre-change CONTROL: same repo, same dirt — merges anyway, exit 0",
          rc_o == 0 and merged_yet(), f"rc={rc_o} hits={HITS} out={out_o[-300:]!r}")
else:
    SKIPPED.append("B2 (no pre-change build available)")
    print("  SKIP B2 — no pre-change build to control against")

p2, w2 = forge_repo("clean")
sha2 = branch_commit(w2, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha2, "ref": "fix/probe"}
rc, out = land(w2)
check("B3 no false positive: clean target tree still lands", rc == 0 and merged_yet(),
      f"rc={rc} out={out[-300:]!r}")
HITS_LOCAL_HEAD = list(HITS)

p3, w3 = forge_repo("unrelated")
open(os.path.join(p3, "README.md"), "w").write("edited, uncommitted, unrelated\n")
sha3 = branch_commit(w3, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha3, "ref": "fix/probe"}
rc, out = land(w3)
check("B4 intersection, not 'any dirt': dirty README does not block a config.json PR",
      rc == 0 and merged_yet(), f"rc={rc} out={out[-300:]!r}")

p4, w4 = forge_repo("override")
open(os.path.join(p4, "config.json"), "w").write('{"lanes": {"build": {}}}\n')
sha4 = branch_commit(w4, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha4, "ref": "fix/probe"}
rc, out = land(w4, LUMEN_PR_ALLOW_DIRTY=1)
check("B5 LUMEN_PR_ALLOW_DIRTY=1 no longer lands — retired, refuses and says so (wq#1282)",
      rc != 0 and not merged_yet() and "RETIRED" in out,
      f"rc={rc} out={out[-300:]!r}")

p5, w5 = forge_repo("untracked")
open(os.path.join(p5, "newlane.json"), "w").write("written by the operator, never committed\n")
sha5 = branch_commit(w5, "newlane.json", '{"from": "the branch"}\n')
PR["head"] = {"sha": sha5, "ref": "fix/probe"}
rc, out = land(w5)
check("B6 an UNTRACKED file the PR adds counts too (git itself refuses that merge)",
      rc != 0 and "newlane.json" in out, f"rc={rc} out={out[-300:]!r}")

p6, w6 = forge_repo("apionly")
open(os.path.join(p6, "config.json"), "w").write('{"lanes": {"build": {}}}\n')
PR["head"] = {"sha": "0" * 40, "ref": "fix/probe"}      # a head this clone has never seen
FILES[:] = ["config.json"]
rc, out = land(w6)
check("B7 unknown head sha falls back to the forge's file list and still refuses",
      rc != 0 and "config.json" in out and any(p.endswith("/files") for _, p in HITS),
      f"rc={rc} hits={HITS} out={out[-300:]!r}")

r1 = real_repo("dcheck")
open(os.path.join(r1, "config.json"), "w").write('{"lanes": {"build": {}}}\n')
rc, out = sh(f"{LPR} dirtycheck {r1} config.json", env=env())
check("B8 dirtycheck refuses on a colliding path", rc != 0 and "config.json" in out,
      f"rc={rc} out={out[-200:]!r}")
rc, out = sh(f"{LPR} dirtycheck {r1} --mode warn config.json", env=env())
check("B8b --mode warn reports the collision without the refusal language",
      rc == 1 and "will refuse" in out and "refusing to land" not in out,
      f"rc={rc} out={out[-200:]!r}")
rc, out = sh(f"{LPR} dirtycheck {r1} README.md", env=env())
check("B8c dirtycheck is quiet-green on a path nobody is holding", rc == 0 and "clean" in out,
      f"rc={rc} out={out[-200:]!r}")

r2 = real_repo("warncommit")
rc, out = sh(f"{LPR} start {r2} fix/probe-warn --worktree", env=env(WQ_AGENT="probe-wq771"))
wt2 = out.strip().splitlines()[-1]
open(os.path.join(r2, "config.json"), "w").write('{"lanes": {"build": {}}}\n')   # dirty AFTER the cut
open(os.path.join(wt2, "config.json"), "w").write('{"lanes": {}, "cloud_ceiling": 5}\n')
git(wt2, "add -- config.json")
rc, out = sh(f'{LPR} commit {wt2} "fix(probe): touch a held path"', env=env(WQ_AGENT="probe-wq771"))
check("B9 commit WARNS about the collision and still commits (nothing is destroyed yet)",
      rc == 0 and "TARGET TREE HAS WORK ORIGIN DOES NOT" in out and "config.json" in out,
      f"rc={rc} out={out[-300:]!r}")

r3 = real_repo("startnote")
open(os.path.join(r3, "config.json"), "w").write('{"lanes": {"build": {}}}\n')
rc, out = sh(f"{LPR} start {r3} fix/probe-note --worktree", env=env(WQ_AGENT="probe-wq771"))
check("B10 start names the landmines up front, before any work is done",
      rc == 0 and "wq#771" in out and "config.json" in out, f"rc={rc} out={out[-300:]!r}")

# ---------------------------------------------------------------------------
print("\nD. wq#792 — un-pushed COMMIT on the target's default branch, not just uncommitted state")

p7, w7 = forge_repo("unpushed")
open(os.path.join(p7, "config.json"), "w").write('{"lanes": {"build": {"enabled": true}}}\n')
git(p7, "add -- config.json")
git(p7, "commit -qm 'feat(probe): local-only commit never pushed'")
sha7 = branch_commit(w7, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha7, "ref": "fix/probe"}
FILES[:] = ["config.json"]
rc, out = land(w7)
check("D1 land REFUSES over an un-pushed commit on the target's default branch, cites wq#792",
      rc != 0 and "config.json" in out and "wq#792" in out and "refusing to land" in out,
      f"rc={rc} out={out[-300:]!r}")
check("D1b the merge endpoint was NEVER called", not merged_yet(), str(HITS))

p8, w8 = forge_repo("unpushed-unrelated")
open(os.path.join(p8, "README.md"), "w").write("local-only commit, unrelated path\n")
git(p8, "add -- README.md")
git(p8, "commit -qm 'chore(probe): unrelated local-only commit'")
sha8 = branch_commit(w8, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha8, "ref": "fix/probe"}
rc, out = land(w8)
check("D2 intersection, not 'any un-pushed commit': an unrelated local-only commit does not block",
      rc == 0 and merged_yet(), f"rc={rc} out={out[-300:]!r}")

p9, w9 = forge_repo("unpushed-override")
open(os.path.join(p9, "config.json"), "w").write('{"lanes": {"build": {}}}\n')
git(p9, "add -- config.json")
git(p9, "commit -qm 'feat(probe): another local-only commit'")
sha9 = branch_commit(w9, "config.json", '{"lanes": {}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha9, "ref": "fix/probe"}
rc, out = land(w9, LUMEN_PR_ALLOW_DIRTY=1)
check("D3 LUMEN_PR_ALLOW_DIRTY=1 does not rescue an un-pushed commit either (wq#1282)",
      rc != 0 and not merged_yet() and "RETIRED" in out,
      f"rc={rc} out={out[-300:]!r}")

r5 = real_repo("dcheck-unpushed")
open(os.path.join(r5, "config.json"), "w").write("local-only, real repo\n")
git(r5, "add -- config.json")
git(r5, "commit -qm 'feat(probe): local-only commit, real repo'")
rc, out = sh(f"{LPR} dirtycheck {r5} config.json", env=env())
check("D4 dirtycheck also catches an un-pushed commit, not just uncommitted diffs",
      rc != 0 and "config.json" in out and "wq#792" in out, f"rc={rc} out={out[-200:]!r}")

# ---------------------------------------------------------------------------
print("\nE. wq#1121 — the branch CONTAINS the un-pushed commit, so landing supersedes nothing")

# D1's repo with one thing changed: the branch is cut from the target tree's main instead of
# from origin/main, so it carries the local-only commit forward. wq#1108 died here — the guard
# refused the very PR its own refusal text asks for ("land it as its own PR, then this").
p10, _ = forge_repo("superset")
open(os.path.join(p10, "config.json"), "w").write('{"lanes": {"build": {"enabled": true}}}\n')
git(p10, "add -- config.json")
git(p10, "commit -qm 'feat(probe): local-only commit never pushed'")
w10 = os.path.join(TMP, "superset-wt2")
git(p10, f"worktree add -q -b fix/superset {w10} main")
sha10 = branch_commit(w10, "config.json",
                      '{"lanes": {"build": {"enabled": true}}, "cloud_ceiling": 5}\n')
PR["head"] = {"sha": sha10, "ref": "fix/superset"}
FILES[:] = ["config.json"]
rc, out = land(w10)
check("E1 land PROCEEDS when the branch is a superset of the target tree, and says why",
      rc == 0 and merged_yet() and "wq#1121" in out and "superset of the target tree" in out,
      f"rc={rc} out={out[-300:]!r}")

# The carve-out is about COMMITS. Uncommitted work in the target tree is in no commit, so no
# ancestry vouches for it and the refusal must survive even on a superset branch.
p11, _ = forge_repo("superset-dirty")
open(os.path.join(p11, "config.json"), "w").write('{"lanes": {"build": {"enabled": true}}}\n')
git(p11, "add -- config.json")
git(p11, "commit -qm 'feat(probe): local-only commit never pushed'")
w11 = os.path.join(TMP, "superset-dirty-wt2")
git(p11, f"worktree add -q -b fix/superset {w11} main")
sha11 = branch_commit(w11, "config.json",
                      '{"lanes": {"build": {"enabled": true}}, "cloud_ceiling": 5}\n')
open(os.path.join(p11, "config.json"), "a").write('// operator is mid-edit\n')
PR["head"] = {"sha": sha11, "ref": "fix/superset"}
rc, out = land(w11)
check("E2 a superset branch still refuses over UNCOMMITTED work in the target tree",
      rc != 0 and not merged_yet() and "uncommitted there" in out and "refusing to land" in out,
      f"rc={rc} out={out[-300:]!r}")

HITS.clear()
rc, out = sh(f"{LPR} land {w11} 1 --allow-dirty", env=env())
check("E3 land --allow-dirty parses (no 'unknown flag') and rescues nothing (wq#1121/wq#1282)",
      rc != 0 and not merged_yet() and "unknown flag" not in out
      and "does NOTHING" in out and "refusing to land" in out,
      f"rc={rc} out={out[-300:]!r}")

# ---------------------------------------------------------------------------
print("\nC. REGRESSION GUARDS — behaviour the change must not move")

src = open(LPR).read()
check("C1 bash still parses the script", sh(f"bash -n {LPR}")[0] == 0)
rc, out = sh(f"{LPR} bogus-subcommand", env=env())
check("C2 unknown subcommand still dies, and now lists dirtycheck",
      rc != 0 and "dirtycheck" in out, f"rc={rc} out={out[-200:]!r}")
rc, out = sh(f"{LPR}", env=env())
check("C3 help still lists every subcommand",
      all(s in out for s in ("start", "commit", "open", "land", "check", "gitlock", "dirtycheck")),
      out[-300:])
r4 = real_repo("noregress")
open(os.path.join(r4, "config.json"), "w").write("x\n")
git(r4, "add -- config.json")
rc, out = sh(f'{LPR} commit {r4} "fix(probe): direct to main"', env=env())
check("C4 committing straight to the default branch is still refused (§2, §10.1)",
      rc != 0 and "refusing to commit directly" in out, f"rc={rc} out={out[-200:]!r}")
rc, out = sh(f'{LPR} commit {r4} "not conventional"', env=env())
check("C5 the subject linter still fires first (before any wq#771 output)",
      rc != 0 and "Conventional Commit" in out and "wq#771" not in out, out[-200:])
open(os.path.join(r4, "config.json"), "w").write("y\n")
git(r4, "checkout -q -b fix/probe-primary")
git(r4, "add -- config.json")
rc, out = sh(f'{LPR} commit {r4} "fix(probe): commit in the primary tree itself"', env=env())
check("C6 committing IN the target tree does not warn about the caller's own staged work",
      rc == 0 and "TARGET TREE HAS WORK ORIGIN DOES NOT" not in out, f"rc={rc} out={out[-300:]!r}")
check("C7 the guard prefers the local merge-base diff — no /files call when the head is local",
      not any(p.endswith("/files") for _, p in HITS_LOCAL_HEAD), str(HITS_LOCAL_HEAD))

# ---------------------------------------------------------------------------
print(f"\n{PASS} passed, {FAIL} failed" + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
if FAILED:
    print("FAILED: " + ", ".join(FAILED))
for s in SKIPPED:
    print("SKIPPED: " + s)
SRV.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
