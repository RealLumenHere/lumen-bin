# wq#1702 done-gate: hold a close to what the ticket named

Design, written before code. Source audit: foreman/research/TICKET_CLOSURE_AUDIT.md (wq#1668).
Prior art already live: wq#759 (notes must cite something), wq#899 (a cited PR must be merged),
wq#1673 (path evidence canonicalized, linked-worktree citations refused, `--deliverable` column).

## 1. What is still broken (evidence graded)

| id | defect | evidence (grade A = measured on queue.db / box, 2026-09-10) |
|----|--------|------|
| D1 | The wq#1673 deliverable gate covers nothing. It only fires when `wq add --deliverable` was passed, and no caller passes it. | A: `deliverable` column set on 0 of 1119 tickets created since 08-15. |
| D2 | A ticket that says how it must land (`via lumen-pr`, `lumen-pr land`) closes on any evidence at all, including `--no-artifact`. | A: 38 such tickets closed since 08-15; 18 closed with no PR URL cited (3 on `--no-artifact`). |
| D3 | A path in a PRIMARY checkout counts as durable even when it was never committed. That is the live-only-hunk class (wq#1653, wq#1676 right now). | A: 3 of 227 path citations are untracked in their primary tree today (#1133 bin/github-token-check, #1153, #1607 MINERVA_STALENESS_REVIEW.md). 11 more are gitignored on purpose. |
| D4 | The merge gate is blind for github.com: `_forge_token` reads only `$GITHUB_TOKEN`, which fworker does not export. `unknown` passes with a stdout warning nobody keeps. | A: audit F1, 13 LPP false-completes + PR #108/#112 (wq#1672 notes). wq#1672 is blocked on a harness capability defect, not on the code. |

## 2. Class

The gate tests evidence the closer chose, against a check that is either opt-in (D1),
blind to the ticket's own terms (D2), too generous about "landed" (D3), or silently
fail-open (D4). Class: **the ticket's stated terms are not the subject under test, and a
verifier that cannot see is indistinguishable from one that passed.**

Where else it lives: token lookup is duplicated in python (`wq`) and bash
(`lumen-github-identity.sh`), and they already drifted once (wq#1581).

## 3. Options

- A. Require `--deliverable` at `add` for every build/research ticket. Rejected: callers are
  prompts and timers; moves the failure to filing and still trusts a free-text choice.
- B. Derive the ticket's terms from its own text at `done`, high-precision markers only,
  and check them. **Chosen.** No migration, no caller change, applies to every open ticket.
- C. LLM judge on detail vs notes. Rejected: this gate is mechanical by doctrine; judging
  whether the work did what was asked is the acceptance lane (wq#1260).

## 4. Chosen design

1. **Derived deliverable (D1).** Column empty -> extract the first path after an explicit
   output marker in title+detail: `Deliverable:` / `Output:` (plural ok, any case) or
   `-> <path.ext>`. Bare mentions and `Report:` are NOT markers (backtest: `Report:` names
   inputs, e.g. #1037). Resolution: as given, cwd, /home/lumen, the queue's own directory
   (`reports/x.md` means work-queue/reports), each repo checkout, each primary checkout one or
   two levels under /home/lumen, then any notes token whose path ends in the named relative
   path. A `YYYYMMDD` placeholder globs to the dated file. Verdicts reuse wq#1673:
   missing/ephemeral -> rc 7, `--unlanded` overrides. A RELATIVE path that resolves under no
   base is ambiguous, not missing: it closes with a visible warning (an absolute one is a fact
   and refuses).
2. **Landing demand (D2).** Title+detail containing `via lumen-pr` or `lumen-pr land` means the
   ticket must land. Notes must prove it: a PR URL the merge gate accepts, a sha reachable
   from a main ref in a repo on this box, or a path committed on main. Anything else,
   including `--no-artifact`, -> rc 8 unless `--unlanded "<reason>"`.
3. **Primary checkouts (D3).** `evidence_durability` runs the same `cat-file <main>:<rel>` test
   for primary checkouts; untracked and not ignored -> ephemeral. Gitignored -> durable
   (on-box state by design). Tracked but modified -> durable (content drift out of scope).
4. **Forge token (D4).** `_forge_token` for github.com calls the canonical
   `lumen-github-identity.sh` `resolve_github_token <owner/repo>` in a subprocess and reads
   the token from its stdout (never printed). `$GITHUB_TOKEN` stays as the last rung.
   One resolver, so the two copies cannot drift again.
5. **Fail-open stays, silence goes.** `unknown` on a PR still closes, but stamps
   `[UNVERIFIED-PR: <why>]` into notes so `closure-quality` and the next audit count it.
6. The numeric-agent claim refusal names `wq claim --id <ticket>`.

## 5. Test-to-failure plan

New `tests/test_wq_done_gate_terms.py`, real binary, scratch DB, throwaway repos: derived
deliverable missing -> 7, present on main -> 0; `Report:` not a deliverable; suffix match via
notes -> 0; landing demand + `--no-artifact` -> 8, + `--unlanded` -> 0 stamped; + sha on
main -> 0; + sha only on a branch -> 8; + path committed on main -> 0; untracked file in a
primary -> 6; ignored file -> 0; unknown PR -> 0 with `[UNVERIFIED-PR`; github token comes
from the identity lib (fake lib via env). Then the full bin suite, then replay the backtest
through the new binary against a read-only copy of queue.db.

### Results

- `tests/test_wq_done_gate_terms.py`: 26 cases. 11 of the first 22 fail against origin/main
  (7eaa059), all pass on this branch. Full bin suite 311 passed (baseline 283).
- Replay of the new gate code (exec'd from this branch, `_finish` never called) over a copy of
  queue.db, from /home/lumen, against today's box state:

  | window | closes | refused | false refusals |
  |---|---|---|---|
  | since 08-15 | 919 | 8 | 0 |
  | since 06-01 | 1493 | 13 | 0 |

  Real catches, files cited as done but never committed: #1112, #1153, #1607, #405 (-> wq#1726).
  The rest are stale/duplicate/no-code closes on lumen-pr tickets that now need `--unlanded`
  on the record (#494, #510, #541, #557, #656, #1315, #1325, #1356, #1701).
- Test to failure: the first replay refused 66 good closes. #1455's `-> www/fleet.html` lives
  two levels down (fleet/inventory), and 65 older tickets name `...-YYYYMMDD.md` templates or
  `reports/x.md` relative to the queue dir or a pack dir. Fixed with the two-level primary
  roots, placeholder globbing, the queue dir as a base, and fail-open for unanchored relative
  paths. Second replay: 0 false refusals in both windows.

## 6. Rollback

One PR on lumen-bin; revert it. No schema change; the gate writes only notes stamps.

## 7. Out of scope (owned elsewhere)

Re-adjudicating the 13 LPP closes + #108/#112 (wq#1672). Harness capability defect
(wq#1697, wq#1719). Detectors filed as tickets (F5, wq#1674). Read-only locks (F7, wq#1675).
