#!/usr/bin/env bash
# alert-deadman-beat.sh (wq#1039)
# Beats the off-fleet dead man's switch (deadman.lumennode.uk) ONLY when the
# page-delivery path is healthy. If the self-hosted ntfy will not accept a
# publish, we do NOT beat -> the off-fleet watcher alarms Frank on silence.
# If Lumen is down entirely, this never runs -> same result. That is the point.
set -uo pipefail

TOKF=/home/lumen/.secrets/deadman-beat.token
URL=https://deadman.lumennode.uk/beat
HB_TOPIC=lumen-heartbeat
LOG=/home/lumen/agents/alert-queue/deadman-beat.log

log(){ echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# 1. Prove the self-hosted ntfy path accepts a publish (primary=200). min
#    priority + dedicated topic = invisible, no OOB rate-budget spend, no pages.
OUT=$(/home/lumen/bin/ntfy-send -p min -t "$HB_TOPIC" -T deadman-hb "hb $(date -u +%FT%TZ)" 2>&1)
if ! grep -q 'primary=200' <<<"$OUT"; then
  log "SKIP beat: ntfy path unhealthy [$OUT]"
  exit 0   # deliberately no beat -> deadman will fire
fi

# 2. Path healthy -> beat the off-fleet watcher.
TOK=$(tr -d '\n' < "$TOKF" 2>/dev/null)
if [ -z "${TOK:-}" ]; then log "SKIP beat: missing beat token"; exit 0; fi
CODE=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST -H "X-Beat-Token: $TOK" "$URL" 2>/dev/null)
if [ "$CODE" = "200" ]; then
  log "beat ok (ntfy primary=200, worker=200)"
else
  # Worker unreachable is NOT a fleet-alert-path failure; log and move on. The
  # watcher's own silence-detection covers a truly-dead worker path separately.
  log "beat POST http=$CODE (ntfy path was healthy)"
fi
exit 0
