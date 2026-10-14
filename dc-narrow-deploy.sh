#!/usr/bin/env bash
# dc-narrow-deploy.sh — deploy narrowed reap dist to live supergateway services,
# health-check, deadman auto-revert. Runs DETACHED (systemd-run) so it survives
# the desktop-commander gateway restart that severs Commander.
set -uo pipefail
# wq#3805: desktop-commander-mcp was retired on Lumen (unit masked). Never revive it.
[ "$(systemctl is-enabled desktop-commander-mcp 2>/dev/null)" = "masked" ] && { echo "desktop-commander-mcp retired on this host (masked) - nothing to do (wq#3805)"; exit 0; }
LOG=/home/lumen/logs/dc-narrow-deploy.log
mkdir -p /home/lumen/logs
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

D=/home/lumen/.npm-global/lib/node_modules/supergateway/dist/gateways
LIVE="$D/stdioToStatelessStreamableHttp.js"
PRISTINE="$D/stdioToStatelessStreamableHttp.js.prepatch.bak"
PATCH=/home/lumen/uplift/supergateway-campaign/supergateway/lumen-dist-patch/stateless-reliability.dist.patch
TS=$(date +%Y%m%d_%H%M%S)
BK="$D/stdioToStatelessStreamableHttp.js.broadbak.$TS"
CAND=$(mktemp /tmp/cand.XXXXXX.js)

log "=== deploy start (pid $$) ==="
sleep 3   # let the launching Commander call return before we bounce the gateway

# 1) build candidate from pristine + narrow patch, validate (fail-closed)
cp "$PRISTINE" "$CAND"
if ! patch "$CAND" < "$PATCH" >/dev/null 2>&1; then log "PATCH FAILED -- abort, NO CHANGE"; exit 1; fi
if ! node --check "$CAND"; then log "node --check FAILED -- abort, NO CHANGE"; exit 1; fi
grep -q reapRequestSession "$CAND" || { log "candidate missing narrowing -- abort"; exit 1; }
grep -q SUPERGATEWAY_REAP_GRACE_MS "$CAND" || { log "candidate missing grace env -- abort"; exit 1; }
log "candidate validated ($(wc -c <"$CAND") bytes)"

# 2) backup live (broad) + install narrowed
cp -a "$LIVE" "$BK"; log "backup -> $BK"
cp -f "$CAND" "$LIVE"; chown lumen:lumen "$LIVE"; log "installed narrowed dist"

# 3) restart services: memory first (low-risk), then commander (our own path)
systemctl restart memory-mcp.service && log "restarted memory-mcp" || log "memory-mcp restart nonzero"
systemctl restart desktop-commander-mcp.service && log "restarted desktop-commander-mcp" || log "dc restart nonzero"

# 4) deadman health check (~40s)
ok=0
for i in $(seq 1 20); do
  sleep 2
  a=$(systemctl is-active desktop-commander-mcp.service 2>/dev/null)
  h=$(curl -fsS -m 3 http://localhost:3100/health >/dev/null 2>&1 && echo OK || echo BAD)
  hm=$(curl -fsS -m 3 http://localhost:3104/health >/dev/null 2>&1 && echo OK || echo BAD)
  log "poll $i: dc=$a health3100=$h health3104=$hm"
  [ "$a" = active ] && [ "$h" = OK ] && { ok=1; break; }
done

grep -q reapRequestSession "$LIVE" && distok=yes || distok=no
if [ "$ok" = 1 ]; then
  log "HEALTHY -- narrowed dist live (dist_has_narrowing=$distok). DEPLOY_RESULT=SUCCESS"
  rm -f "$CAND"; exit 0
else
  log "UNHEALTHY in deadman window -- REVERTING to broad"
  cp -f "$BK" "$LIVE"; chown lumen:lumen "$LIVE"
  systemctl restart memory-mcp.service
  systemctl restart desktop-commander-mcp.service
  sleep 4
  a=$(systemctl is-active desktop-commander-mcp.service 2>/dev/null)
  h=$(curl -fsS -m 3 http://localhost:3100/health >/dev/null 2>&1 && echo OK || echo BAD)
  log "post-revert: dc=$a health3100=$h. DEPLOY_RESULT=REVERTED"
  rm -f "$CAND"; exit 2
fi
