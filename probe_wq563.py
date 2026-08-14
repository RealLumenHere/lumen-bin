#!/usr/bin/env python3
"""probe_wq563 — red/green for the forge-aware pre-merge mergeability guard.

The defect: cmd_land switched on .mergeable_state unconditionally. That is a GITHUB
field; Gitea returns null, so both arms were dead and the guard silently did nothing.

Measured against the live Gitea (1.26.4) rather than assumed:
  conflicted PR -> mergeable=false, stable and immediate at +0s..+8s
  clean PR      -> mergeable=true,  same
  mergeable_state -> null in every case
  merging a conflicted PR -> HTTP 405 {"message":"Please try again later"}

That 405 is why this guard matters. It reads as a TRANSIENT fault for a PERMANENT
conflict, so an agent obeying it retries forever.

Section C is mandatory per the ticket: lumen-pr is how every lane lands, so the GitHub
arm must be provably unchanged.

Usage: probe_wq563.py [DIR]
"""
import os, sys, subprocess

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
LPR = os.path.join(D, "lumen-pr")
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def guard(forge, body, tool=None):
    """Run mergeability_guard in isolation. Returns (rc, output)."""
    t = tool or LPR
    script = (
        f'. <(sed -n "/^die()/,/^redact()/p" {t})\n'
        f'eval "$(sed -n "/^mergeability_guard()/,/^}}/p" {t})"\n'
        'declare -f mergeability_guard >/dev/null || exit 90\n'
        'mergeability_guard 42\n'
    )
    e = dict(os.environ, FORGE=forge, API_BODY=body, DEFAULT_BRANCH="main")
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=60)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


print(f"probe_wq563 — tool under test: {LPR}\n")

print("A. GITEA — the arm that was dead")
rc, out = guard("gitea", '{"mergeable":false}')
check("A1", rc != 0,
      "*** a conflicted gitea PR is REFUSED before the merge call, not after it ***")
check("A2", "mergeable=false" in out and "gitea" in out.lower(),
      "and the reason names the field it actually read")
check("A3", "405" in out and "try again later" in out.lower(),
      "*** and warns off the retry: gitea answers 405 'Please try again later' for a "
      "PERMANENT conflict ***")
check("A4", "conflict" in out.lower() and "branch-protection" in out.lower(),
      "*** BOTH candidate causes named — gitea's boolean cannot distinguish them, so the "
      "guard refuses to guess (a confident wrong diagnosis is worse than silence) ***")
rc, out = guard("gitea", '{"mergeable":true}')
check("A5", rc == 0 and out.strip() == "", "a clean gitea PR passes silently")

print("\nB. UNKNOWN is never turned into a diagnosis")
for cid, body in (("B1", '{"mergeable":null}'), ("B2", "{}")):
    rc, out = guard("gitea", body)
    check(cid, rc == 0 and "WARNING" in out and "conflict" not in out.lower(),
          f"gitea {body}: warns and proceeds — never claims a conflict it cannot see")
rc, out = guard("github", '{"mergeable_state":"unknown"}')
check("B3", rc == 0 and "WARNING" in out,
      "github's 'still computing' warns and proceeds rather than dying")

print("\nC. GITHUB REGRESSION — mandatory: lumen-pr is how every lane lands")
OLD = os.path.join(D, "lumen-pr.bak.20260814_172115")
rc, out = guard("github", '{"mergeable_state":"dirty"}')
check("C1", rc != 0 and "merge conflicts" in out,
      "dirty still DIES with the same wording")
rc, out = guard("github", '{"mergeable_state":"blocked"}')
check("C2", rc == 0 and "blocked" in out and "attempting merge anyway" in out,
      "blocked still WARNS and proceeds")
rc, out = guard("github", '{"mergeable_state":"clean"}')
check("C3", rc == 0 and out.strip() == "", "clean still passes silently")
rc, out = guard("github", '{"mergeable_state":"behind"}')
check("C4", rc == 0 and out.strip() == "",
      "behind still passes silently — this ticket did not retune github's thresholds")
# C1-C4 are REGRESSION GUARDS, not discriminators: on the pre-change build they fail
# because mergeability_guard does not exist there, which proves nothing about behaviour.
# C5/C6 are the real proof — the github messages must be the OLD ones, VERBATIM, copied
# from lumen-pr.bak.20260814_172115. If a future edit reworks them, these go red.
OLD_DIRTY = "lumen-pr: PR #42 has merge conflicts with main — rebase the branch first"
OLD_BLOCKED = ("lumen-pr: WARNING: mergeable_state=blocked (ruleset requirement unmet) "
               "— attempting merge anyway")
rc, out = guard("github", '{"mergeable_state":"dirty"}')
check("C5", out.strip() == OLD_DIRTY,
      f"*** github's dirty message is byte-identical to the pre-change build ***")
rc, out = guard("github", '{"mergeable_state":"blocked"}')
check("C6", out.strip() == OLD_BLOCKED,
      "*** and so is blocked's — the github arm was preserved, not rewritten ***")

print("\nD. the guard is actually WIRED IN, not merely defined")
src = open(LPR).read()
code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
check("D1", any("mergeability_guard " in l and "()" not in l for l in code),
      "cmd_land calls it")
check("D2", not any('jq -r .mergeable_state' in l or "jq -r '.mergeable_state'" in l
                    for l in code if "mergeability_guard" not in l and "ms=" not in l),
      "and the old inline mergeable_state case is gone from cmd_land")
check("D3", "need_repo" in src.split("cmd_land() {")[1][:200],
      "cmd_land still calls need_repo — the guard edit did not eat it")

print()
print(f"probe_wq563: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
