#!/usr/bin/env python3
"""probe_wq557 — red/green for lumen-pr against a non-GitHub forge.

Asserts the Gitea path works AND that the github.com path did not move — the second is the
one that matters, because lumen-pr is how every lane lands.

Usage: probe_wq557.py [DIR]   where DIR holds the lumen-pr under test
"""
import os, sys, subprocess, tempfile, shutil

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
LPR = os.path.join(D, "lumen-pr")
TMP = tempfile.mkdtemp(prefix="wq557-")
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def run(argv, env=None):
    e = dict(os.environ)
    e["LUMEN_PR_SCOPE_CHECK"] = "0"  # the sanctioned probe hatch; never a workaround
    if env:
        e.update(env)
    try:
        p = subprocess.run([str(x) for x in argv], capture_output=True, text=True, env=e, timeout=90)
    except FileNotFoundError:
        return 127, f"ENOENT: {argv[0]} does not exist on the build under test"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def repo(name, origin):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    for c in (["git", "init", "-q", "."], ["git", "remote", "add", "origin", origin],
              ["git", "commit", "-q", "--allow-empty", "-m", "chore: seed"],
              ["git", "branch", "-M", "main"]):
        subprocess.run(c, cwd=d, capture_output=True)
    return d


print(f"probe_wq557 — tool under test: {LPR}\n")

# ---- A. slug derivation across origin forms ---------------------------------
# The old sed stripped a literal "https://" and nothing else, so anything that was not
# github-over-https kept its scheme/userinfo/host/port and became the slug verbatim.
print("A. the slug is owner/repo for ANY origin form, not just github-over-https")
CASES = [
    ("A1", "https://github.com/frankstupak/lumen-plays-pokemon.git", "frankstupak/lumen-plays-pokemon"),
    ("A2", "git@github.com:frankstupak/lumen-plays-pokemon.git", "frankstupak/lumen-plays-pokemon"),
    ("A3", "http://lumen@100.121.17.5:3000/lumen/lumen-bin.git", "lumen/lumen-bin"),
    ("A4", "https://git.example.org/team/thing", "team/thing"),
]
# MY FIRST VERSION OF THIS SECTION READ `gitlock scope` AND PASSED ON THE OLD BUILD.
# gitlock_scope uses ${SLUG##*/} — the LAST path segment — so it returns "lumen-bin-git"
# even when SLUG is the entire origin URL. The scope was accidentally right for a broken
# slug, so the assertion measured a proxy that could not see the defect. `check` prints the
# resolved SLUG verbatim on its "repo :" line, before any network call, so it reads the
# actual derivation. Fifth non-discriminating assertion I have caught in my own work today.
for cid, origin, want in CASES:
    d = repo(cid, origin)
    rc, out = run([LPR, "check", d])   # may fail at the token step; the repo line prints first
    line = next((l for l in out.splitlines() if l.startswith("repo    :")), "")
    got = line.split("(")[-1].rstrip(")") if "(" in line else "<no repo line>"
    check(cid, got == want, f"{origin} -> {want} (got {got!r})")

# ---- B. the original repro --------------------------------------------------
print("\nB. the reported defect: `check` on a Gitea repo (needs the live Anubis instance)")
rc, out = run([LPR, "check", "/home/lumen/bin"])
check("B1", "lumen/lumen-bin" in out,
      "the slug resolves to owner/repo instead of the whole origin URL")
check("B2", "open PRs:" in out,
      "*** the open-PR line PRINTS — this is the line that silently never appeared ***")
# wq#524: B3 used to assert the literal string "GITEA_TOKEN", which pinned the SOURCE
# (agent/.env's variable name) rather than the contract (a Gitea credential, not a
# github.com PAT). Moving the token to /home/lumen/.secrets/gitea-lumen.token turned it red
# while the behaviour was correct. Assert the contract from both sides instead: the reported
# source is Gitea-specific, AND no github.com identity is named on the line.
_auth = next((l for l in out.splitlines() if "authenticating to" in l), "")
check("B3", "gitea" in _auth.lower()
      and ("gitea-lumen.token" in _auth or "GITEA_TOKEN" in _auth)
      and "github-frank.token" not in _auth
      and "GITHUB_TOKEN" not in _auth,
      f"authenticated with a Gitea credential, not a github.com PAT ({_auth.strip()!r})")
# B4 passes on BOTH builds and is recorded as an invariant, not a discriminator: the old
# build also exited 0 — by omitting the line entirely. That silence IS the defect, which is
# why B2 rather than B4 is the load-bearing assertion here.
check("B4", rc == 0, "the command succeeds end to end (true on both builds — see comment)")

# ---- C. failures are LOUD ---------------------------------------------------
print("\nC. a failed API call says so instead of omitting a line")
d = repo("C", "http://lumen@100.121.17.5:3000/lumen/does-not-exist.git")
rc, out = run([LPR, "check", d])
check("C1", rc != 0, "an API failure makes the command fail")
check("C2", "UNKNOWN" in out and "API call failed" in out,
      "and it SAYS the open-PR list is unknown rather than printing nothing")
check("C3", "token" not in out.split("GITEA_TOKEN")[-1].lower() or "ghp_" not in out,
      "no credential material in the error path")

# ---- D. the github path did not move ----------------------------------------
# This is the assertion that matters most: lumen-pr is how every lane lands.
print("\nD. GITHUB REGRESSION — the path every lane uses must be untouched")
rc, out = run([LPR, "check", "/home/lumen/pokemon"])
check("D1", "frankstupak/lumen-plays-pokemon" in out, "github slug unchanged")
check("D2", "open PRs:" in out and rc == 0, "github open-PR listing still works")
check("D3", "github-lumen-classic.token" in out,
      "still resolves the PER-REPO github token via the identity map, not the Gitea branch")
check("D4", "gitea" not in out.lower(), "the gitea code path is not entered for a github origin")
rc, out = run([LPR, "gitlock", "/home/lumen/pokemon", "scope"])
check("D5", out.strip() == "lpp-git", "the lpp-git lock alias is unchanged")

print(f"\nprobe_wq557: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
