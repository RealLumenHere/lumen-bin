#!/usr/bin/env python3
"""probe_wq546 — red/green guard for the publishing scope fence.

Each section reproduces a step of the 2026-08-13 winforge incident and asserts the tooling
now refuses it. Runs against throwaway repos and a throwaway scope config; touches no
GitHub API and no real repo.

Usage: probe_wq546.py [DIR]   where DIR holds lumen-repo-scope / lumen-pr / lumen-ruleset
"""
import os, sys, subprocess, tempfile, shutil

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
SCOPE = os.path.join(D, "lumen-repo-scope")
LPR = os.path.join(D, "lumen-pr")
RULESET = os.path.join(D, "lumen-ruleset")
CONF = "/home/lumen/infra/github/agent-scope.conf"
SENTINEL_DIR = "/home/lumen/agents/repo-visibility-sentinel"
TMP = tempfile.mkdtemp(prefix="wq546-")

PASS, FAIL = [], []
def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))

def run(argv, env=None):
    e = dict(os.environ)
    if env: e.update(env)
    try:
        p = subprocess.run([str(x) for x in argv], capture_output=True, text=True, env=e, timeout=60)
    except FileNotFoundError:
        # A tool absent from the build under test must read RED, not crash the run.
        # (Lesson paid for in wq#532: a red half that aborts is a green tick.)
        return 127, f"ENOENT: {argv[0]} does not exist on the build under test"
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def fake_repo(name, origin):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    for c in (["git", "init", "-q", "."], ["git", "remote", "add", "origin", origin],
              ["git", "commit", "-q", "--allow-empty", "-m", "chore: seed"],
              ["git", "branch", "-M", "main"]):
        subprocess.run(c, cwd=d, capture_output=True)
    return d

print(f"probe_wq546 — tools under test: {D}\n            — scratch       : {TMP}\n")

WINFORGE = fake_repo("winforge", "https://github.com/frankstupak/winforge.git")
LPP      = fake_repo("lpp", "https://github.com/frankstupak/lumen-plays-pokemon.git")
NEWREPO  = fake_repo("newthing", "https://github.com/frankstupak/brand-new-thing.git")
BLOG     = fake_repo("blog", "https://github.com/frankstupak/lumen-blog.git")

# ---- A. the config exists and is the ONE table ------------------------------
print("A. there is a single machine-readable scope table (deliverable 1)")
check("A1", os.path.exists(CONF), f"{CONF} exists")
rc, out = run([SCOPE, "list", "allow"])
check("A2", rc == 0 and "frankstupak/lumen-plays-pokemon" in out,
      "the one Class A repo is ALLOWed")
rc, out = run([SCOPE, "list", "never"])
check("A3", rc == 0 and "frankstupak/winforge" in out,
      "winforge is NEVER — the incident repo is on the deny side, not the allow side")

# ---- B. default deny --------------------------------------------------------
print("\nB. DEFAULT DENY — absence from the table is a refusal, not a permission")
rc, out = run([SCOPE, "check", "frankstupak/brand-new-thing"])
check("B1", rc == 3, "a repo nobody has listed is DENIED")
check("B2", "DEFAULT IS DENY" in out, "and the refusal says WHY, so nobody has to guess")
rc, out = run([SCOPE, "check", "frankstupak/lumen-plays-pokemon"])
check("B3", rc == 0, "the in-scope repo is permitted — the fence is not a wall")

# ---- C. PUBLIC is not ALLOW -------------------------------------------------
print("\nC. 'known public' and 'agents may write' are DIFFERENT questions")
rc, out = run([SCOPE, "check", "frankstupak/lumen-blog"])
check("C1", rc == 3, "a PUBLIC-listed repo is still not writable")
check("C2", "confers" in out.lower() or "NO write permission" in out,
      "and the message explains the distinction rather than just refusing")
check("C3", run([SCOPE, "class", "frankstupak/lumen-blog"])[1].strip() == "PUBLIC",
      "it classifies as PUBLIC, not as an agent-writable class")

# ---- D. fail CLOSED ---------------------------------------------------------
print("\nD. a fence that fails open is not a fence")
rc, out = run([SCOPE, "check", "frankstupak/lumen-plays-pokemon"],
              env={"LUMEN_REPO_SCOPE_CONF": "/nonexistent/scope.conf"})
check("D1", rc == 4, "an unreadable config DENIES even the allowed repo")
bad = os.path.join(TMP, "bad.conf"); open(bad, "w").write("ALOW frankstupak/whatever\n")
rc, out = run([SCOPE, "check", "frankstupak/whatever"], env={"LUMEN_REPO_SCOPE_CONF": bad})
check("D2", rc == 4, "a malformed directive DENIES rather than being skipped silently")
noorigin = os.path.join(TMP, "noorigin"); os.makedirs(noorigin, exist_ok=True)
subprocess.run(["git", "init", "-q", "."], cwd=noorigin, capture_output=True)
rc, out = run([SCOPE, "check", noorigin])
check("D3", rc == 3, "a repo whose owner cannot be determined is DENIED, not assumed safe")

# ---- E. human-only operations ----------------------------------------------
print("\nE. release / tag / visibility are HUMAN decisions (deliverable 3)")
for i, op in enumerate(("release", "publish", "visibility"), 1):
    rc, out = run([SCOPE, "check", "frankstupak/lumen-plays-pokemon", "--op", op])
    check(f"E{i}", rc == 3, f"'{op}' is denied even on the ALLOWED repo — no allowlist grants it")

# ---- F. lumen-pr refuses at the chokepoint ---------------------------------
print("\nF. lumen-pr cannot be pointed at an out-of-scope repo (deliverable 1, enforced)")
rc, out = run([LPR, "start", WINFORGE, "feat/should-never-happen"])
check("F1", rc != 0, "start on winforge is REFUSED")
check("F2", "winforge" in out and ("NEVER" in out or "Class D" in out),
      "and it names the repo and the rule that stopped it")
rc, out = run([LPR, "start", NEWREPO, "feat/should-never-happen"])
# rc != 0 ALONE greens on the old build — the fake repo does not exist on GitHub, so old
# lumen-pr dies at "git fetch failed" for reasons unrelated to scope. A cascade reported as
# a defect in code that does not have one. Require the SCOPE reason, not merely a failure.
check("F3", rc != 0 and ("DEFAULT IS DENY" in out or "not ALLOWed" in out),
      "start on an unlisted repo is refused BY THE FENCE (default deny reaches lumen-pr)")
rc, out = run([LPR, "start", LPP, "feat/scope-probe-noop"])
check("F4", rc == 0, "*** start on the IN-SCOPE repo still works — no regression for the pokemon lane ***")
rc, out = run([LPR, "check", WINFORGE])
check("F5", rc == 0 and "frankstupak/winforge" in out,
      "read-only `check` still inspects an out-of-scope repo (refusing to LOOK pushes agents to raw git)")
rc, out = run([LPR, "start", WINFORGE, "feat/x"], env={"LUMEN_PR_SCOPE": "/nonexistent/scope"})
check("F6", rc != 0 and "DENIED" in out.upper(),
      "a MISSING scope checker denies the operation instead of skipping the check")

# ---- G. lumen-ruleset, the other mutating tool ------------------------------
print("\nG. lumen-ruleset apply is gated too; its read-only verbs are not")
rc, out = run([RULESET, "apply", "frankstupak/winforge"])
check("G1", rc != 0 and "refusing to apply" in out, "apply on winforge is REFUSED before any API call")
# Absence-of-token ALONE is vacuous: neither build ever prints a token. The discriminating
# fact is that the refusal happens INSTEAD OF the token/API path, so require the scope
# message AND the absence of any evidence the tool got as far as GitHub.
check("G2", "refusing to apply" in out and "HTTP" not in out and "no usable token" not in out,
      "and it refuses BEFORE resolving a token or calling the API")

# ---- H. the sentinel: the hole this ticket was really about -----------------
print("\nH. repo-visibility-sentinel (deliverable 2)")
print("   NB: the sentinel and the scope config are SINGLE-INSTANCE — pointing this probe at")
print("   an old tools dir does NOT restore them, so H1-H5 pass on both builds and DO NOT")
print("   discriminate. H6/H7 read the pre-change .bak copies directly and are the red proof.")
allow_txt = open(os.path.join(SENTINEL_DIR, "allowlist.txt")).read()
allow_entries = {ln.strip() for ln in allow_txt.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")}
check("H1", "frankstupak/winforge" not in allow_entries,
      "*** winforge is NO LONGER on the sentinel allowlist — a re-exposure now ALERTS ***")
check("H2", "frankstupak/lumen-blog" in allow_entries and "frankstupak/.github" in allow_entries,
      "and the genuinely-public repos are still suppressed (no alert spam)")
sent = open(os.path.join(SENTINEL_DIR, "repo_sentinel.py")).read()
check("H3", "Privatize or allowlist" not in sent,
      "the old 'Privatize or allowlist' text is GONE — it told the agent to remediate")
check("H4", "TELL FRANK, CHANGE NOTHING" in sent,
      "an unexpected public repo is now a question for a human")
check("H5", "NEVER-PUBLISH" in sent and "never_publish" in sent,
      "and a Class D exposure is escalated differently from an ordinary one")
# --- the red proof for section H: the state this ticket actually found on disk ---
import glob
bak_allow = sorted(glob.glob(os.path.join(SENTINEL_DIR, "allowlist.txt.bak.*_pre_wq546")))
bak_sent = sorted(glob.glob(os.path.join(SENTINEL_DIR, "repo_sentinel.py.bak.*_pre_wq546")))
if bak_allow and bak_sent:
    old_allow = open(bak_allow[-1]).read()
    old_entries = {ln.strip() for ln in old_allow.splitlines()
                   if ln.strip() and not ln.strip().startswith("#")}
    check("H6", "frankstupak/winforge" in old_entries,
          "*** RED PROOF: the pre-change allowlist DID list winforge — a re-exposure of the "
          "never-publish repo would have been SILENT ***")
    check("H7", "Privatize or allowlist" in open(bak_sent[-1]).read(),
          "RED PROOF: the pre-change alert text told the reader to remediate")
else:
    check("H6", False, "pre-change .bak not found — cannot prove the defect existed")
    check("H7", False, "pre-change .bak not found — cannot prove the defect existed")

# ---- I. gh-audit no longer calls the incident repo a public product --------
print("\nI. gh-audit's class table (deliverable 5 — the machine copy of the wrong table)")
audit = open(os.path.join(D, "gh-audit")).read()
check("I1", 'CLASS_A = {"lumen-plays-pokemon", "claude-command-center", "winforge"' not in audit,
      "the hardcoded CLASS_A containing winforge is gone")
ns = {"__name__": "ghprobe"}
try:
    exec(compile(audit, "gh-audit", "exec"), ns)
    rc_fn = ns["repo_class"]
    check("I2", rc_fn("winforge") == "D", f"winforge audits as Class D (got {rc_fn('winforge')!r})")
    check("I3", rc_fn("lumen-plays-pokemon") == "A", "the one real Class A is still Class A")
    check("I4", rc_fn("lumen-blog") is None and rc_fn("tiny-whale") is None,
          "repos wrongly listed Class A are now UNCLASSIFIED — not scored against the full standard")
    check("I5", rc_fn("simpill-utils") == "B", "upstream forks are unaffected")
except Exception as e:
    for cid in ("I2", "I3", "I4", "I5"):
        check(cid, False, f"gh-audit did not import: {e}")

print(f"\nprobe_wq546: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL: print("FAILED: " + ", ".join(FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
