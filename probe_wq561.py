#!/usr/bin/env python3
"""probe_wq561 — red/green for dispatch-lint --universal.

wq#561 asked for six dispatch files GENERATED from one canonical source. Measured
first and the premise did not survive: ZERO lines are common to all six, the longest
identical block anywhere is 4 lines, peak pairwise overlap is 10%. There is no shared
substrate to factor out, and "generate" would mean rewriting all six live agent prompts
to common wording — the mass rewrite the ticket's own constraint forbids.

What the measurement DID find is that the six already contradict each other, by
omitting safety rules. This is the check for that.

Section E is the load-bearing one. My first regexes produced FIVE false positives:
"`git add -A` is banned" did not match a pattern without backticks, and POKEMON's
precheck rule is line-wrapped between "lumen_episode.py" and "precheck". A checker
that reds on compliant files is noise, and noise is how a guard becomes decoration.

Usage: probe_wq561.py [DIR]   where DIR holds the dispatch-lint under test
"""
import os, sys, subprocess, tempfile, shutil
from pathlib import Path

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(D, "dispatch-lint")
TMP = tempfile.mkdtemp(prefix="wq561-")
PASS, FAIL = [], []


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


RETIRED_MIN = "RETIRE :: dummy-rule :: zzz-never-appears-zzz :: ALL :: placeholder\n"

UNIVERSAL_FIXTURE = """# fixture

Documented format, INDENTED — must be ignored, not parsed as rules:

    REQUIRE :: <id> :: <regex> :: <scope> :: <guidance>
    AGREE :: <id> :: <regex with one capture group> :: <scope> :: <guidance>

REQUIRE :: no-add-all :: git add -A`?\\s+is banned :: ALL :: stage only what you touched
REQUIRE :: precheck :: lumen_episode\\.py\\s+precheck :: ALL :: change-safety gate
AGREE :: heartbeat-ttl :: heartbeat <id> --ttl (\\d+) :: ALL :: one lease TTL, not four
"""

# Two files that STATE both rules, in the two awkward real-world shapes that broke my
# first pass: backticked, and wrapped across a newline.
GOOD_BACKTICK = """# A QUEUE - DISPATCH PROMPT
- Stage only what you touched: `git add <paths>`. `git add -A` is banned - siblings edit too.
- Run `python3 /home/lumen/minerva/lumen_episode.py precheck "<plan>"` first.
- Heartbeat: wq heartbeat <id> --ttl 240
"""
GOOD_WRAPPED = """# B QUEUE - DISPATCH PROMPT
      git add <ONLY the paths you touched>       # git add -A is banned,
  sibling before editing any file; `python3 /home/lumen/minerva/lumen_episode.py
  precheck "<plan>"` before state-changing work; never reboot anything.
- Heartbeat: wq heartbeat <id> --ttl 240
"""
BAD_MISSING = """# C QUEUE - DISPATCH PROMPT
Claim a ticket and go. Nothing here mentions staging or the safety gate.
- Heartbeat: wq heartbeat <id> --ttl 240
"""
BAD_DISAGREE = """# D QUEUE - DISPATCH PROMPT
- `git add -A` is banned
- python3 /home/lumen/minerva/lumen_episode.py precheck "<plan>"
- Heartbeat: wq heartbeat <id> --ttl 90
"""


def make_env(name, files, universal=UNIVERSAL_FIXTURE, retired=RETIRED_MIN):
    """A private work-queue dir + a dispatch-lint repointed at it."""
    wq = Path(TMP) / name
    wq.mkdir(parents=True, exist_ok=True)
    (wq / "RETIRED_RULES.md").write_text(retired)
    if universal is not None:
        (wq / "UNIVERSAL_RULES.md").write_text(universal)
    for fn, body in files.items():
        (wq / fn).write_text(body)
    tool = wq / "dispatch-lint"
    src = Path(SRC).read_text()
    src = src.replace('WQ_DIR = Path("/home/lumen/agents/work-queue")',
                      f'WQ_DIR = Path("{wq}")')
    tool.write_text(src)
    tool.chmod(0o755)
    return tool


def run(tool, *args):
    p = subprocess.run([sys.executable, str(tool), *args], capture_output=True,
                       text=True, timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


print(f"probe_wq561 — tool under test: {SRC}\n")

print("A. the existing default mode is untouched (wq#533 must not regress)")
t = make_env("a", {"X_DISPATCH.md": BAD_MISSING})
rc, out = run(t)
check("A1", rc == 0, "default run still exits 0 on a file the retired table does not flag")
check("A2", "UNIVERSAL" not in out,
      "and says NOTHING about universal rules unless asked — the mode is opt-in (wq#565's "
      "timer must not go red on arrival)")

print("\nB. --universal catches a rule that is never stated")
t = make_env("b", {"C_DISPATCH.md": BAD_MISSING})
rc, out = run(t, "--universal")
check("B1", rc == 1, "a missing universal rule FAILS instead of reporting clean")
check("B2", "no-add-all" in out and "precheck" in out, "and names which rules are absent")
check("B3", "C_DISPATCH.md" in out, "and which file is missing them")

print("\nC. --universal catches the same rule stated with DIFFERENT values")
t = make_env("c", {"A_DISPATCH.md": GOOD_BACKTICK, "D_DISPATCH.md": BAD_DISAGREE})
rc, out = run(t, "--universal")
check("C1", rc == 1 and "INCONSISTENTLY" in out,
      "*** 240 in one dispatch and 90 in another is a contradiction, not a preference ***")
check("C2", "'240'" in out and "'90'" in out, "both values are printed, so the fix is obvious")
check("C3", "A_DISPATCH.md" in out and "D_DISPATCH.md" in out, "attributed to their files")

print("\nD. it fails CLOSED, and does not parse its own documentation")
t = make_env("d", {"A_DISPATCH.md": GOOD_BACKTICK}, universal=None)
rc, out = run(t, "--universal")
check("D1", rc == 2, "an unreadable universal table exits 2 — it never prints 'clean'")
t = make_env("d2", {"A_DISPATCH.md": GOOD_BACKTICK}, universal="# doc only\n\n    REQUIRE :: <id> :: <rx> :: ALL :: x\n")
rc, out = run(t, "--universal")
check("D2", rc == 2 and "no REQUIRE/AGREE" in out,
      "*** INDENTED format examples are documentation, not rules — parsing them made the "
      "checker reject its own table on the example's zero capture groups ***")

print("\nE. NO FALSE POSITIVES — the failure that made the first version worthless")
t = make_env("e", {"A_DISPATCH.md": GOOD_BACKTICK, "B_DISPATCH.md": GOOD_WRAPPED})
rc, out = run(t, "--universal")
check("E1", "NEVER STATED" not in out,
      "*** a file that states the rule is NOT flagged — backticked (`git add -A` is banned) "
      "and newline-wrapped (lumen_episode.py \\n precheck) both count ***")
check("E2", rc == 0, f"two compliant dispatches pass cleanly (rc={rc})")


print("\nF. the LIVE fleet state this ticket was filed about")
rc, out = run(SRC, "--universal") if os.path.exists(SRC) else (9, "")
check("F1", rc == 1,
      "the six real dispatches do NOT currently satisfy the universal rules — the "
      "violations ARE the finding, which is why the mode ships opt-in")
check("F2", "UNIFIED_DISPATCH.md" in out and "no-add-all" in out,
      "*** the MASTER dispatch never states the `git add -A` ban ***")
check("F3", "FABLE_QUEUE_DISPATCH.md" in out and "ARTICLE_RESEARCH_DISPATCH.md" in out,
      "and two lanes are never told about the change-safety precheck gate")
rc0, out0 = run(SRC)
check("F4", rc0 == 0,
      "while the DEFAULT lint stays green on the same files — this change adds a check, "
      "it does not break the existing one")

print()
shutil.rmtree(TMP, ignore_errors=True)
print(f"probe_wq561: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
