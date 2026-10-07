#!/bin/bash
# One-shot (wq#3158, final-3 2026-09-29): reopen wq#3158 once the 48h Workers Logs window after the
# wq#2925 deploy (2026-09-29 19:02 UTC) has elapsed. There is no not_before mechanism (ISSUE-LOG #1064),
# so this is the stopgap. Re-checks the clock and the row state itself; does nothing if either is off.
set -u
NOT_BEFORE=$(date -u -d '2026-10-01 19:02:00' +%s)
NOW=$(date -u +%s)
[ "$NOW" -ge "$NOT_BEFORE" ] || { echo "too early ($NOW < $NOT_BEFORE), leaving 3158 blocked"; exit 0; }
export WQ_AGENT=$(/home/lumen/bin/wq whoami --register wq3158-reopen | sed -n 's/^export WQ_AGENT=//p')
ST=$(/home/lumen/bin/wq show 3158 | sed -n 's/^status *: *//p')
[ "$ST" = "blocked" ] || { echo "3158 is '$ST', not blocked; nothing to do"; exit 0; }
/home/lumen/bin/wq edit 3158 --status open && \
/home/lumen/bin/wq note 3158 "wq3158-reopen timer: 48h window after the wq#2925 deploy (2026-09-29 19:02 UTC) has elapsed; reopened at $(date -u +%FT%TZ). Re-verify the elapsed time live before querying."
