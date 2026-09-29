#!/usr/bin/env bash
# Detached redeploy of desktop-commander-mcp.service with health-check + rollback.
# Runs as root via systemd-run so it survives the MCP connection drop caused by
# restarting the very gateway we're talking through.
set -u
LOG=/home/lumen/infra/dc-redeploy.log
UNIT=/etc/systemd/system/desktop-commander-mcp.service
NEW=/home/lumen/infra/desktop-commander-mcp.service.new
TS=$(date +%Y%m%d_%H%M%S)
BAK=${UNIT}.bak.${TS}
exec >>"$LOG" 2>&1
echo "===== $(date -u) redeploy start ====="

# Give the calling MCP turn time to return before we cut the connection.
sleep 4

cp -a "$UNIT" "$BAK" && echo "backed up -> $BAK"
cp "$NEW" "$UNIT" && echo "installed new unit"

if ! systemd-analyze verify "$UNIT" ; then
  echo "VERIFY FAILED -> rolling back"
  cp -a "$BAK" "$UNIT"; systemctl daemon-reload
  systemctl restart desktop-commander-mcp
  echo "ROLLED BACK (verify)"; echo "===== done ====="; exit 1
fi

systemctl daemon-reload
systemctl restart desktop-commander-mcp
echo "restarted; waiting for health"

healthy=0
for i in $(seq 1 6); do
  sleep 5
  active=$(systemctl is-active desktop-commander-mcp 2>/dev/null)
  listen=$(ss -ltn 2>/dev/null | grep -c ':3100 ')
  # Health = unit active + port listening + GET /health 200. NOT an anonymous MCP initialize: with the wq#3103
  # Access guard on :3100 that returns 403 by design, and a 2xx requirement would auto-roll-back a healthy unit.
  # (Real end-to-end proof is the mcpw path: mcpw_diag.py / the pool probe, run by whoever is driving.)
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://localhost:3100/health 2>/dev/null)
  echo "try $i: active=$active listen=$listen health_code=$code"
  if [ "$active" = "active" ] && [ "$listen" -ge 1 ] && [ "$code" = "200" ]; then
    healthy=1; break
  fi
done

if [ "$healthy" = "1" ]; then
  echo "HEALTHY on new stateful unit"
  systemctl enable desktop-commander-mcp >/dev/null 2>&1
  dc=$(pgrep -fc '/home/lumen/.npm-global/bin/desktop-commander')
  echo "desktop-commander child count now: $dc"
else
  echo "UNHEALTHY -> rolling back to $BAK"
  cp -a "$BAK" "$UNIT"; systemctl daemon-reload
  systemctl restart desktop-commander-mcp
  echo "ROLLED BACK (health)"
fi
echo "===== $(date -u) done ====="
