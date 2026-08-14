#!/bin/bash
# lpp-watchdog — un-wedge the live LPP stream when the event file goes stale.
#
# SOURCE OF RECORD for the root-owned /usr/local/sbin/lpp-watchdog.sh. It lives in
# sbin/ rather than the repo root DELIBERATELY: /home/lumen/bin is on the lumen
# user's PATH, and a root script that ends in `systemctl restart` must not be one
# tab-completion away from being run by hand.
#
# wq#562. WHAT CHANGED AND WHAT DID NOT:
#   UNCHANGED — WEDGE_SECS, the 60s cadence, and the wedge behaviour itself. Un-wedging
#   a dead stream is legitimate and stays. This is not a rollback of the watchdog.
#   ADDED — (1) it honours runtime/RESTART_FREEZE with a VISIBLE skip, (2) it defers to
#   an agent holding the lumen-pokemon-live lock, (3) every restart it performs records
#   the tree SHA vs the RUNNING SHA, so a watchdog deploy is attributable in its own log.
#
# WHY: this script is root, on a timer, and predates every coordination mechanism the
# lane built. It ignored the freeze flag it never read, ignored slock, and did not use
# /home/lumen/bin/lpp-restart, which wq#532 made the only sanctioned restart path. A
# graceful restart also resets NRestarts, so the counter looked innocent afterwards.
#
# WHY NOT ROUTE THROUGH lpp-restart, since wq#532 made it the sanctioned path: lpp-restart
# is built for an AGENT — under a freeze it demands a wq ticket CLAIMED BY the caller, and
# the watchdog has no wq identity and must never acquire one. It also blocks up to 120s
# waiting to observe a fresh event, which a 60s timer would stack. So the watchdog keeps
# its own restart call and instead adopts the two properties that actually matter: it
# refuses during a freeze, and it defers to a live agent's lock.
#
# *** WHY ATTRIBUTION RUNS AS THE lumen USER AND NOT AS ROOT — READ BEFORE "SIMPLIFYING". ***
# This script runs as root under systemd. git REFUSES to read /home/lumen/pokemon as root:
# the repo is owned by lumen, so git's safe.directory check aborts with "detected dubious
# ownership" and every rev-parse/reflog returns EMPTY. That failure is INVISIBLE when you
# test by hand, because `sudo` sets SUDO_UID and git then accepts the repo — so a hand-run
# sandbox prints real SHAs while the timer prints `unknown`. Verified both ways, wq#562.
# Running it through `runuser -u lumen` is also what keeps root from writing root-owned
# __pycache__ into a lumen-owned repo. The slock check below already used this pattern.
set -uo pipefail

LOGDIR=/home/lumen/pokemon/logs-24h
WATCHLOG="$LOGDIR/watchdog.log"
WEDGE_SECS=${LPP_WEDGE_SECS:-300}
REPO=${LPP_WATCHDOG_REPO:-/home/lumen/pokemon}
EVENTS=${LPP_WATCHDOG_EVENTS:-/home/lumen/pokemon/events}
FREEZE=${LPP_WATCHDOG_FREEZE:-/home/lumen/pokemon/runtime/RESTART_FREEZE}
SLOCK=${LPP_WATCHDOG_SLOCK:-/home/lumen/bin/slock}
UNIT=${LPP_WATCHDOG_UNIT:-lumen-pokemon-live}
RUNAS=${LPP_WATCHDOG_RUNAS:-lumen}
# Absolute: /usr/sbin is on systemd's PATH but NOT on the lumen user's, so a bare
# `runuser` works from the timer and breaks under the probe. Absolute works in both.
RUNUSER=${LPP_WATCHDOG_RUNUSER:-/usr/sbin/runuser}
DRYRUN=${LPP_WATCHDOG_DRYRUN:-0}

log() { echo "$(date -u) $*" >> "$WATCHLOG"; }

# Run a command as the repo-owning user when we are root; run it directly when we are
# already that user (which is how the probe exercises this file without sudo).
#
# THE TIMEOUT LIVES HERE, NOT AT THE CALL SITE. `timeout 25 as_repo_user ...` looks
# correct and is not: timeout execs a BINARY and cannot see a shell function, so it fails
# with ENOENT and the caller silently gets an empty result. That defect shipped in this
# file's first draft and was caught by probe_wq562 section E going red. Wrapping runuser
# (a real binary) is the form that actually works.
as_repo_user() {
  local t=${LPP_WATCHDOG_TIMEOUT:-25}
  if [ "$(id -u)" = 0 ] && [ -x "$RUNUSER" ]; then
    timeout "$t" "$RUNUSER" -u "$RUNAS" -- "$@"
  else
    timeout "$t" "$@"
  fi
}

# Attribution: which build is on disk, and which build is ACTUALLY RUNNING. A watchdog
# restart DEPLOYS whatever the tree holds, so recording both is what makes an unsoaked
# deploy traceable to this script instead of being discovered days later in a reflog.
#
# This DELEGATES to lpp_frontier.deploy_drift — the same function lpp-sentinel's
# deploy_drift alert uses — rather than reimplementing the resolution here. A second
# implementation of "which sha is running" is exactly how L831 happened: the running sha
# must be resolved from the reflog entry AT-OR-BEFORE the process start time, and a copy
# of that logic in bash is a copy that will drift from the one that is tested.
#
# `via=` is printed and never dropped. deploy_drift falls back to HEAD when the reflog
# cannot answer (via=reflog-exhausted/service-not-running), and a fallback reported as a
# fact is the L831 failure itself. Attribution NEVER blocks a restart: any failure here
# degrades to a string and the wedge still fires.
ATTRIB_PY='
import os, sys
sys.path.insert(0, sys.argv[1] + "/tools")
import lpp_frontier as F
# LPP_WATCHDOG_STARTED_MS is TEST-ONLY injection (wq#562): deploy_drift documents
# started_ms as injectable "so the reflog resolution can be proved offline against a
# fixture repo". Passing it through lets probe_wq562 section G prove, every run, that
# the resolution picks the reflog entry AT-OR-BEFORE process start — the L831 property —
# instead of only when the live fleet happens to be mid-drift. Production never sets it,
# and it can only change this LOG STRING; the restart decision never reads attribution.
_sm = os.environ.get("LPP_WATCHDOG_STARTED_MS") or None
d = F.deploy_drift(base=sys.argv[1], unit=sys.argv[2],
                   started_ms=int(_sm) if _sm else None)
if not d.get("ok"):
    print("attribution=UNAVAILABLE reason=%s" % d.get("reason"))
else:
    print("tree=%s running=%s via=%s ahead=%s drifted=%s" % (
        d["head"][:7], d["running"][:7], d["runningFrom"],
        d["commitsTreeAheadOfRunning"], d["drifted"]))
'
attribution() {
  local out
  out=$(as_repo_user env PYTHONDONTWRITEBYTECODE=1 \
          LPP_WATCHDOG_STARTED_MS="${LPP_WATCHDOG_STARTED_MS:-}" \
          python3 -c "$ATTRIB_PY" "$REPO" "$UNIT" 2>/dev/null | head -1)
  printf '%s' "${out:-attribution=UNAVAILABLE reason=resolver-failed}"
}

restart_now() { # restart_now <reason>
  local reason="$1"
  # THE FREEZE, ENFORCED. Previously this file never read the flag at all.
  if [ -e "$FREEZE" ]; then
    log "FREEZE: would restart ($reason) but $FREEZE exists — skipping. $(attribution)"
    log "FREEZE: contents: $(tr '\n' ' ' < "$FREEZE" 2>/dev/null | cut -c1-200)"
    return 0
  fi
  # DEFER TO A LIVE AGENT. If a session holds the service lock it is mid-restart or
  # mid-verification; restarting under it is how two restarts race. Read-only check, run
  # as the lumen user so root never touches the agents' sqlite and leaves root-owned WAL.
  if [ -x "$SLOCK" ]; then
    local held
    held=$(as_repo_user "$SLOCK" who "$UNIT" 2>/dev/null | grep -v "no active locks" | head -2)
    if [ -n "$held" ]; then
      log "DEFER: would restart ($reason) but an agent holds $UNIT — skipping. held: $(printf '%s' "$held" | tr '\n' ' ' | cut -c1-160)"
      return 0
    fi
  fi
  log "WEDGE: $reason - restarting. $(attribution)"
  if [ "$DRYRUN" = 1 ]; then log "DRYRUN: restart suppressed"; return 0; fi
  systemctl restart "$UNIT"
}

NEW=$(ls -t "$EVENTS"/live-agent-*.jsonl 2>/dev/null | head -1)
if [ -z "$NEW" ]; then
  restart_now "NO event file"
  exit 0
fi
AGE=$(( $(date +%s) - $(stat -c %Y "$NEW") ))
if [ "$AGE" -gt "$WEDGE_SECS" ]; then
  restart_now "no event ${AGE}s file=$NEW"
else
  log "ok age=${AGE}s"
fi
