#!/usr/bin/env bash
# deadman-killtest.sh (wq#1039) — quarterly proof the off-fleet dead man's
# switch actually fires. "An untested dead man's switch is worse than none."
# Uses ONLY the beat token (no CF API token). All alarms are drill-marked.
# A trap guarantees the beat timer + flags are restored even on failure.
set -uo pipefail
TOK=$(tr -d '\n' < /home/lumen/.secrets/deadman-beat.token)
U=https://deadman.lumennode.uk
LOG=/home/lumen/agents/alert-queue/deadman-killtest.log
log(){ echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG" >&2; }
api(){ curl -s -m 15 "$@"; }
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

restore(){
  api -X POST -H "X-Beat-Token: $TOK" "$U/grace?clear=1" >/dev/null 2>&1 || true
  api -X POST -H "X-Beat-Token: $TOK" "$U/drill?off=1"  >/dev/null 2>&1 || true
  sudo systemctl start alert-deadman-beat.timer >/dev/null 2>&1 || true
  /home/lumen/bin/alert-deadman-beat.sh >/dev/null 2>&1 || true
  log "restore complete (grace cleared, drill off, beat timer up, beat sent)"
}
trap restore EXIT

log "killtest START"

# Part A — alarm DELIVERY (what rots: email creds, ntfy topic, worker liveness).
ST=$(api -X POST -H "X-Beat-Token: $TOK" "$U/selftest")
EMAIL=$(echo "$ST" | jget 'd.get("alarm",{}).get("email")')
NTFY=$(echo  "$ST" | jget 'd.get("alarm",{}).get("ntfy")')
log "selftest email=$EMAIL ntfy=$NTFY"

# Baseline AFTER selftest, so Part B only passes on a NEW alarm fired during the
# induced-silence window (the cron) — never on the selftest's own alarm.
BEFORE=$(api -H "X-Beat-Token: $TOK" "$U/status" | jget 'd.get("last_alarm_ts") or ""')

# Part B — real SILENCE detection via the production cron path (drill-marked).
api -X POST -H "X-Beat-Token: $TOK" "$U/drill?on=1"  >/dev/null
api -X POST -H "X-Beat-Token: $TOK" "$U/grace?min=2" >/dev/null
sudo systemctl stop alert-deadman-beat.timer
CRON_FIRED=""
for i in $(seq 1 16); do
  sleep 30
  NOW=$(api -H "X-Beat-Token: $TOK" "$U/status" | jget 'd.get("last_alarm_ts") or ""')
  [ -n "$NOW" ] && [ "$NOW" != "$BEFORE" ] && { CRON_FIRED="$NOW"; break; }
done

if [ "$EMAIL" = "ok" ] && [ -n "$CRON_FIRED" ]; then VERDICT=PASS; else VERDICT=FAIL; fi
log "killtest $VERDICT (email=$EMAIL ntfy=$NTFY cron_fired=${CRON_FIRED:-NONE})"

if [ "$VERDICT" = PASS ]; then
  /home/lumen/bin/ntfy-send -p low -t lumen-updates -T "deadman quarterly drill" \
    "PASS — off-fleet alert-path dead man's switch verified. email=$EMAIL ntfy=$NTFY; cron detected induced silence." >/dev/null 2>&1 || true
else
  /home/lumen/bin/ntfy-send -p urgent -t lumen-alerts -T "DEADMAN DRILL FAILED" \
    "The off-fleet alert-path dead man's switch did NOT verify (email=$EMAIL ntfy=$NTFY cron=${CRON_FIRED:-NONE}). The alert-path SAFETY NET may be down — investigate deadman.lumennode.uk + reports/1039." >/dev/null 2>&1 || true
fi
[ "$VERDICT" = PASS ]
