# wq#1158 — lease hardening: atomic claim, fencing tokens, idempotency

## What this changes

`wq` gains four columns on `tasks` and three behaviours that depend on them:

| column | meaning |
| --- | --- |
| `lease_owner` | who holds the live lease (set only while `status='claimed'`) |
| `lease_expires_at` | epoch mirror of `lease_expires`, for the atomic claim predicate |
| `fence` | monotonic per-ticket claim counter — the zombie guard |
| `attempt` | how many times the ticket has been claimed |

1. **Atomic claim.** The claim write-back is one guarded `UPDATE ... RETURNING id, fence`.
   Zero rows back means another claimer won; wq prints `RACE-LOST` and takes nothing.
2. **Fencing.** `wq claim` prints `FENCE=<n>`. Passing `--fence <n>` to
   `heartbeat`/`done`/`release`/`block` refuses the write (exit 5) if the ticket has been
   claimed again since. A resumed zombie becomes inert.
3. **Idempotency.** `wq idem branch|path|pr <id>` gives ticket-keyed names, so a duplicate
   worker converges on the same branch, the same output directory and the *same* PR instead
   of opening a second one.

## Why it is not auto-applied

Every other column on `tasks` is added by `wq init()` on the next invocation. That is right
for inert storage. These four are not inert: the moment they exist, the claim protocol changes
for every in-flight claim on a queue several live sessions are holding tickets in. So `wq`
**feature-detects** them and behaves exactly as it did before if they are absent.

Deploy order is therefore: **merge the code → run the migration → the hardening turns on.**
Merging the code alone is a no-op on an unmigrated queue.

## Deploy

```sh
# 0. know what you are about to change (writes nothing)
python3 migrations/wq1158_lease_fence.py --db /home/lumen/agents/work-queue/queue.db

# 1. apply. Takes its own VACUUM INTO backup first, every run.
python3 migrations/wq1158_lease_fence.py --db /home/lumen/agents/work-queue/queue.db --yes

# 2. confirm — a fresh claim should print FENCE=1 or higher
wq claim --lane <lane> --ttl 5
```

`--db` is required and has no default: the live queue is never a fallback. The script is
idempotent — a second `--yes` prints `ALREADY APPLIED` and exits 0.

### Backups

Both `--yes` and `--rollback --yes` write `queue.db.bak-wq1158-{pre,prerollback}-<UTC>` next to
the DB before touching anything, via `VACUUM INTO` (a plain `cp` of a WAL-mode DB can miss
committed pages still in the `-wal`). Nothing prunes these; delete them by hand once you are
satisfied.

## Rollback

```sh
python3 migrations/wq1158_lease_fence.py --db <db> --rollback          # report
python3 migrations/wq1158_lease_fence.py --db <db> --rollback --yes    # drop the columns
```

Drops the indexes, then the four columns (`ALTER TABLE ... DROP COLUMN`, SQLite ≥ 3.35 — the
script refuses to run at all below that, so `--rollback` can always honour its name). `wq`
returns to its pre-wq#1158 behaviour on the next invocation with no code change. If something
worse has happened, the `.bak-wq1158-pre-*` file is a complete queue.

## The one transition window

`--fence` is **opt-in**. A worker that predates it passes no fence, and its writes are accepted
exactly as they are today — that is what makes this deployable without draining the queue.

The consequence is one generation of exposure. A worker that claimed *before* the migration
holds no fence token, so if its lease expires and its ticket is re-claimed, its late `wq done`
is still accepted. Backfilled rows start at `fence=0` so the next claim mints 1.

Two ways to close it, in order of preference:

- **Let it drain.** Every ticket claimed after the migration mints a fence. The window closes
  on its own as pre-migration claims finish or expire; `wq list --status claimed` shows what is
  left.
- **Drain first.** Run the migration when `wq list --status claimed` is empty, and there is no
  window at all.

Either way the exposure is strictly smaller than today's, where *no* claim has a fence.

## Follow-ups deliberately not in this change

Foreman one-worker-per-node systemd, and the `slock` audit. Separate tickets; the ticket scopes
this one to the wq claim/lease/fence core.
