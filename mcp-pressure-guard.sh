#!/usr/bin/env bash
# mcp-pressure-guard — auto-recover desktop-commander-mcp from saturation (wq#380).
# Trips on: box memory-stall PSI (full avg60) OR gateway cgroup bloat.
# Restart is SAFE: gateway is deliberately STATELESS (restart transparent to
# claude.ai sessions between requests — verified live 2026-07-19 05:56Z).
# Runs as root via mcp-pressure-guard.timer (1 min). Cooldown prevents flapping.
set -u
# wq#3805: desktop-commander-mcp was retired on Lumen (unit masked). Never revive it.
[ "$(systemctl is-enabled desktop-commander-mcp 2>/dev/null)" = "masked" ] && { echo "desktop-commander-mcp retired on this host (masked) - nothing to do (wq#3805)"; exit 0; }
THRESH_PSI=${THRESH_PSI:-25}     # percent, /proc/pressure/memory 'full avg60'
THRESH_MEM_G=${THRESH_MEM_G:-11} # GiB, gateway cgroup MemoryCurrent
COOLDOWN=${COOLDOWN:-600}        # seconds between auto-restarts
STAMP=/run/mcp-pressure-guard.last
NTFY_URL=http://127.0.0.1:8080/lumen-fleet  # wq411: CF-Access 302 silently ate publishes; direct local ntfy

psi=$(awk '/^full/{for(i=1;i<=NF;i++)if($i ~ /^avg60=/){sub("avg60=","",$i);print $i;exit}}' /proc/pressure/memory)
mem=$(systemctl show desktop-commander-mcp -p MemoryCurrent --value 2>/dev/null)
case "$mem" in ''|*[!0-9]*) mem=0;; esac
memg=$(( mem / 1073741824 ))

trip=""
awk -v p="${psi:-0}" -v t="$THRESH_PSI" 'BEGIN{exit !(p+0>=t+0)}' && trip="psi_full_avg60=${psi}"
[ "$memg" -ge "$THRESH_MEM_G" ] && trip="${trip:+$trip }gateway_mem=${memg}G"
[ -z "$trip" ] && exit 0

now=$(date +%s); last=$(cat "$STAMP" 2>/dev/null || echo 0)
case "$last" in ''|*[!0-9]*) last=0;; esac
if [ $((now - last)) -lt "$COOLDOWN" ]; then
  logger -t mcp-guard "trip ($trip) suppressed — cooldown ($((now-last))s < ${COOLDOWN}s)"
  exit 0
fi
echo "$now" > "$STAMP"
logger -t mcp-guard "TRIP: $trip — restarting desktop-commander-mcp"
systemctl restart desktop-commander-mcp
sleep 3
hc=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:3100/health || echo 000)
logger -t mcp-guard "post-restart health=$hc"
curl -sf -m 5 -d "mcp-guard: auto-restarted gateway ($trip) health=$hc $(date -u +%H:%M:%SZ)" "$NTFY_URL" >/dev/null 2>&1 || true
exit 0
