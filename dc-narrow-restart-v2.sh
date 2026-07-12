#!/usr/bin/env bash
# v2: actually restart desktop-commander onto the (already-installed) narrowed dist.
# Verifies the PROCESS restarted (start-timestamp advances) + healthy + no crash-loop.
# Reverts live dist to broad backup on real failure. Detached-safe.
set -uo pipefail
LOG=/home/lumen/logs/dc-narrow-deploy.log
log(){ echo "[$(date '+%F %T')] v2 $*" | tee -a "$LOG"; }
D=/home/lumen/.npm-global/lib/node_modules/supergateway/dist/gateways
LIVE="$D/stdioToStatelessStreamableHttp.js"
BROADBAK=$(ls -1t "$D"/stdioToStatelessStreamableHttp.js.broadbak.* 2>/dev/null | head -1)
SVC=desktop-commander-mcp.service
log "=== start (pid $$) broadbak=$BROADBAK ==="
sleep 3   # let launcher return before we sever Commander
grep -q reapRequestSession "$LIVE" || { log "live dist NOT narrowed -- abort"; exit 1; }
OLD=$(systemctl show "$SVC" -p ExecMainStartTimestampMonotonic --value)
log "old start-mono=$OLD; restarting $SVC"
sudo -n systemctl restart "$SVC" 2>/tmp/v2_restart.err; rc=$?
[ "$rc" -ne 0 ] && { log "restart exit=$rc stderr=[$(cat /tmp/v2_restart.err)] -- retrying once"; sleep 2; sudo -n systemctl restart "$SVC" 2>/tmp/v2_restart.err; rc=$?; }
log "restart exit=$rc stderr=[$(cat /tmp/v2_restart.err 2>/dev/null)]"
ok=0
for i in $(seq 1 20); do
  sleep 2
  a=$(systemctl is-active "$SVC" 2>/dev/null)
  NEW=$(systemctl show "$SVC" -p ExecMainStartTimestampMonotonic --value)
  h=$(curl -fsS -m3 http://localhost:3100/health >/dev/null 2>&1 && echo OK || echo BAD)
  rstd=$([ "$NEW" != "$OLD" ] && echo yes || echo no)
  log "poll $i: active=$a restarted=$rstd health=$h"
  [ "$a" = active ] && [ "$rstd" = yes ] && [ "$h" = OK ] && { ok=1; break; }
done
n1=$(systemctl show "$SVC" -p NRestarts --value); sleep 4; n2=$(systemctl show "$SVC" -p NRestarts --value)
[ "$n2" -gt "$((n1+1))" ] && { log "CRASH-LOOP nrestarts $n1->$n2"; ok=0; }
if [ "$ok" = 1 ]; then
  log "SUCCESS desktop-commander on narrowed dist, healthy+stable (nrestarts=$n2). DEPLOY_RESULT=SUCCESS_V2"; exit 0
else
  log "FAILURE -- reverting live dist to broad + restart"
  [ -n "$BROADBAK" ] && cp -f "$BROADBAK" "$LIVE" && chown lumen:lumen "$LIVE"
  sudo -n systemctl restart "$SVC"; sleep 3
  a=$(systemctl is-active "$SVC"); h=$(curl -fsS -m3 http://localhost:3100/health >/dev/null 2>&1 && echo OK || echo BAD)
  log "post-revert active=$a health=$h dist=$(grep -q reapRequestSession "$LIVE" && echo narrowed || echo broad). DEPLOY_RESULT=REVERTED_V2"; exit 2
fi
